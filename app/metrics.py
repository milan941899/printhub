"""Messwerte für die Übersicht: Temperaturverlauf, Stromverbrauch, Statistik und Wartung."""
import logging
import sqlite3
import threading
import time
from collections import deque

log = logging.getLogger("metrics")


# --- Temperaturverlauf --------------------------------------------------------------
class TempHistory:
    """Ringpuffer pro Drucker: alle 5 s eine Messung (2 h), dazu der laufende Druck verdichtet (alle 30 s, max. 48 h)."""

    def __init__(self):
        self.fine = {}    # pid -> deque[(t, {label: [ist, soll]})]
        self.job = {}     # pid -> {"started": t, "points": [...]}
        self.lock = threading.Lock()

    def add(self, pid, temps, job_started=None):
        t = int(time.time())
        point = (t, {x["label"]: [x.get("actual"), x.get("target")] for x in temps if x.get("actual") is not None})
        if not point[1]:
            return
        with self.lock:
            self.fine.setdefault(pid, deque(maxlen=1440)).append(point)
            if job_started:
                j = self.job.get(pid)
                if not j or j["started"] != job_started:
                    j = self.job[pid] = {"started": job_started, "points": []}
                if not j["points"] or t - j["points"][-1][0] >= 30:
                    j["points"].append(point)
                    del j["points"][:-5760]

    def get(self, pid, rng):
        with self.lock:
            if rng == "job":
                j = self.job.get(pid)
                pts = list(j["points"]) if j else []
                if pts and self.fine.get(pid):
                    pts.append(self.fine[pid][-1])
                return pts
            sec = {"5": 300, "15": 900, "60": 3600}.get(str(rng), 900)
            cut = time.time() - sec
            return [p for p in self.fine.get(pid, ()) if p[0] >= cut]


# --- Stromverbrauch (stündlich, in history.db) ---------------------------------------------
class EnergyLog:
    def __init__(self, path):
        self.path = path
        self.last = {}   # pid -> letzter Zählerstand (Wh)
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS energy (hour INTEGER, pid TEXT, wh REAL, PRIMARY KEY(hour, pid))")

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def sample(self, pid, total_wh):
        """Zählerstand der Steckdose -> Differenz in die aktuelle Stunde buchen."""
        if total_wh is None:
            return
        prev = self.last.get(pid)
        self.last[pid] = total_wh
        if prev is None or total_wh < prev:          # erster Wert oder Zähler zurückgesetzt
            return
        delta = total_wh - prev
        if delta <= 0 or delta > 5000:               # Ausreißer ignorieren
            return
        hour = int(time.time() // 3600 * 3600)
        with self.lock, self._db() as db:
            db.execute("INSERT INTO energy(hour, pid, wh) VALUES(?,?,?) ON CONFLICT(hour, pid) DO UPDATE SET wh = wh + excluded.wh",
                       (hour, pid, delta))

    def series(self, pid, rng):
        """Balken für Tag (24 h), Woche (7 Tage), Monat (30 Tage), Jahr (12 Monate)."""
        now = time.localtime()
        if rng == "day":
            start = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
            buckets = [(start + h * 3600, time.strftime("%H", time.localtime(start + h * 3600))) for h in range(24)]
            step = 3600
        elif rng in ("week", "month"):
            days = 7 if rng == "week" else 30
            today = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
            buckets = []
            for i in range(days - 1, -1, -1):
                d = time.localtime(today - i * 86400 + 7200)  # +2 h gegen Sommerzeit-Sprünge
                b = time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 0, 0, 0, 0, 0, -1))
                label = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.tm_wday] if rng == "week" else time.strftime("%d.", d)
                buckets.append((b, label))
            step = None
        else:  # Jahr
            buckets = []
            y, m = now.tm_year, now.tm_mon
            for i in range(11, -1, -1):
                mm, yy = m - i, y
                while mm <= 0:
                    mm += 12
                    yy -= 1
                buckets.append((time.mktime((yy, mm, 1, 0, 0, 0, 0, 0, -1)),
                                ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"][mm - 1]))
            step = None
        starts = [b for b, _ in buckets]
        ends = starts[1:] + [(starts[-1] + step) if step else time.time() + 86400 * 40]
        if rng in ("week", "month"):
            ends = starts[1:] + [starts[-1] + 86400 + 3600]
        with self._db() as db:
            rows = db.execute("SELECT hour, wh FROM energy WHERE pid=? AND hour>=?", (pid, int(starts[0]))).fetchall()
        vals = [0.0] * len(buckets)
        for hour, wh in rows:
            for i, (a, b) in enumerate(zip(starts, ends)):
                if a <= hour < b:
                    vals[i] += wh
                    break
        return [{"label": lab, "kwh": round(v / 1000, 3)} for (_, lab), v in zip(buckets, vals)]


# --- Statistik & Wartung ------------------------------------------------------------------
def printer_stats(history_db, printer_name):
    """Was PrintHub selbst erfasst hat (ohne aus Bambuddy übernommene Drucke – deren Stunden
    stecken schon im Wartungs-Startwert)."""
    with sqlite3.connect(history_db, timeout=10) as db:
        r = db.execute("SELECT COUNT(*), SUM(success), SUM(duration), SUM(grams), SUM(kwh), SUM(total_cost) "
                       "FROM prints WHERE printer=? AND COALESCE(source,'')<>'bambuddy'", (printer_name,)).fetchone()
        slots = db.execute("SELECT u.slot, SUM(u.grams) FROM usage u JOIN prints p ON p.id=u.print_id "
                           "WHERE p.printer=? GROUP BY u.slot", (printer_name,)).fetchall()
    n, ok, dur, g, kwh, cost = r
    return {
        "prints": n or 0, "ok": ok or 0, "success_rate": round((ok or 0) / n * 100) if n else None,
        "hours": round((dur or 0) / 3600, 1), "grams": round(g or 0), "kwh": round(kwh or 0, 2),
        "cost": round(cost or 0, 2), "slot_grams": {s: round(v or 0) for s, v in slots},
    }
