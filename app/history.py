"""Druckverlauf in SQLite (/data/history.db): unbegrenzt, filterbar, Monatswerte, CSV."""
import csv
import io
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time INTEGER NOT NULL, printer TEXT, job TEXT, result TEXT, success INTEGER,
  duration INTEGER, grams REAL, kwh REAL, energy_cost REAL, filament_cost REAL,
  wear_cost REAL, total_cost REAL
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  print_id INTEGER REFERENCES prints(id) ON DELETE CASCADE,
  slot TEXT, grams REAL, spool INTEGER, spool_name TEXT, material TEXT, color TEXT,
  cost REAL, booked INTEGER, error TEXT, remaining REAL
);
CREATE INDEX IF NOT EXISTS i_prints_time ON prints(time);
CREATE INDEX IF NOT EXISTS i_usage_print ON usage(print_id);
CREATE INDEX IF NOT EXISTS i_usage_spool ON usage(spool);
"""


class History:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)
            cols = {r[1] for r in db.execute("PRAGMA table_info(prints)")}
            for c, typ in (("thumb", "TEXT"), ("photo", "TEXT"), ("outcome", "TEXT"), ("reason", "TEXT"),
                           ("estimate", "INTEGER"), ("source", "TEXT"), ("ext_id", "TEXT"), ("stages", "TEXT")):
                if c not in cols:
                    db.execute(f"ALTER TABLE prints ADD COLUMN {c} {typ}")
            # Ergebnis-Klasse: ok | fail (Druckerfehler) | cancel (abgebrochen)
            db.execute("UPDATE prints SET outcome = CASE WHEN success=1 THEN 'ok' ELSE 'cancel' END WHERE outcome IS NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS i_prints_ext ON prints(ext_id) WHERE ext_id IS NOT NULL")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def count(self):
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]

    def add(self, e):
        usage = e.get("usage") or []
        grams = e.get("grams")
        if grams is None:
            grams = round(sum(u.get("grams") or 0 for u in usage), 1)
        with self.lock, self._db() as db:
            ok = e.get("result") == "fertig"
            outcome = e.get("outcome") or ("ok" if ok else "cancel")
            cur = db.execute(
                "INSERT INTO prints(time,printer,job,result,success,duration,grams,kwh,energy_cost,filament_cost,wear_cost,total_cost,"
                "thumb,photo,outcome,reason,estimate,source,ext_id,stages)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(e.get("time") or time.time()), e.get("printer"), e.get("job"), e.get("result"),
                 1 if ok else 0, e.get("duration"), grams, e.get("kwh"),
                 e.get("energy_cost"), e.get("filament_cost"), e.get("wear_cost"), e.get("total_cost"),
                 e.get("thumb"), e.get("photo"), outcome, e.get("reason"), e.get("estimate"), e.get("source"), e.get("ext_id"),
                 json.dumps(e["stages"], ensure_ascii=False) if e.get("stages") else None))
            pid = cur.lastrowid
            for u in usage:
                db.execute(
                    "INSERT INTO usage(print_id,slot,grams,spool,spool_name,material,color,cost,booked,error,remaining)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, u.get("slot"), u.get("grams"), u.get("spool"), u.get("spool_name"), u.get("material"),
                     u.get("color"), u.get("cost"), 1 if u.get("booked") else 0, u.get("error"), u.get("remaining")))
            return pid

    def has_ext(self, ext_id):
        with self._db() as db:
            return db.execute("SELECT 1 FROM prints WHERE ext_id=?", (ext_id,)).fetchone() is not None

    def near(self, printer, t, tol=900):
        """Gibt es schon einen Druck dieses Druckers, der zur selben Zeit endete?"""
        with self._db() as db:
            return db.execute("SELECT 1 FROM prints WHERE printer=? AND ABS(time-?)<=?", (printer, int(t), tol)).fetchone() is not None

    def rows(self, f):
        """Alle Drucke mit Verbrauch – Grundlage der Statistik."""
        where, args = self._where(f)
        with self._db() as db:
            prints = [dict(r) for r in db.execute(f"SELECT p.* FROM prints p{where} ORDER BY p.time", args)]
            ids = {p["id"]: p for p in prints}
            for p in prints:
                p["usage"] = []
            if ids:
                for u in db.execute(f"SELECT u.* FROM usage u JOIN prints p ON p.id=u.print_id{where}", args):
                    ids[u["print_id"]]["usage"].append(dict(u))
        return prints

    def set_photo(self, pid, name):
        with self.lock, self._db() as db:
            db.execute("UPDATE prints SET photo=? WHERE id=?", (name, pid))

    def media_of(self, pid):
        with self._db() as db:
            r = db.execute("SELECT thumb, photo FROM prints WHERE id=?", (pid,)).fetchone()
        return [x for x in (r or []) if x]

    def delete(self, pid):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM usage WHERE print_id=?", (pid,))
            db.execute("DELETE FROM prints WHERE id=?", (pid,))

    # --- Abfragen ------------------------------------------------------------
    @staticmethod
    def _where(f):
        w, a = [], []
        if f.get("from"):
            w.append("p.time >= ?"); a.append(int(f["from"]))
        if f.get("to"):
            w.append("p.time < ?"); a.append(int(f["to"]))
        if f.get("printer"):
            w.append("p.printer = ?"); a.append(f["printer"])
        if f.get("result") == "ok":
            w.append("p.success = 1")
        elif f.get("result") == "fail":
            w.append("p.success = 0")
        if f.get("spool"):
            w.append("EXISTS (SELECT 1 FROM usage u WHERE u.print_id=p.id AND u.spool=?)"); a.append(int(f["spool"]))
        if f.get("material"):
            w.append("EXISTS (SELECT 1 FROM usage u WHERE u.print_id=p.id AND UPPER(u.material) LIKE ?)")
            a.append(f["material"].upper() + "%")
        if f.get("q"):
            w.append("p.job LIKE ?"); a.append(f"%{f['q']}%")
        return (" WHERE " + " AND ".join(w)) if w else "", a

    def query(self, f, limit=50, offset=0):
        where, args = self._where(f)
        with self._db() as db:
            tot = db.execute(
                f"SELECT COUNT(*) n, SUM(p.grams) g, SUM(p.kwh) k, SUM(p.energy_cost) ec, SUM(p.filament_cost) fc,"
                f" SUM(p.wear_cost) wc, SUM(p.total_cost) tc, SUM(p.duration) d, SUM(p.success) ok FROM prints p{where}", args).fetchone()
            rows = db.execute(f"SELECT p.* FROM prints p{where} ORDER BY p.time DESC LIMIT ? OFFSET ?",
                              args + [int(limit), int(offset)]).fetchall()
            out = []
            for r in rows:
                e = dict(r)
                try:
                    e["stages"] = json.loads(e["stages"]) if e.get("stages") else []
                except (TypeError, ValueError):
                    e["stages"] = []
                e["usage"] = [dict(u) for u in db.execute("SELECT * FROM usage WHERE print_id=? ORDER BY id", (r["id"],))]
                for u in e["usage"]:
                    u["booked"] = bool(u["booked"])
                out.append(e)
        totals = {k: tot[k] for k in tot.keys()}
        return {"entries": out, "totals": totals}

    def recent(self, n=20):
        return self.query({}, n)["entries"]

    def monthly(self, f, months=12):
        where, args = self._where(f)
        with self._db() as db:
            rows = db.execute(
                f"SELECT strftime('%Y-%m', p.time, 'unixepoch', 'localtime') m, COUNT(*) n, SUM(p.grams) g,"
                f" SUM(p.kwh) k, SUM(p.total_cost) tc, SUM(p.filament_cost) fc, SUM(p.energy_cost) ec, SUM(p.wear_cost) wc"
                f" FROM prints p{where} GROUP BY m ORDER BY m DESC LIMIT ?", args + [int(months)]).fetchall()
        return [dict(r) for r in reversed(rows)]

    def facets(self):
        with self._db() as db:
            printers = [r[0] for r in db.execute("SELECT DISTINCT printer FROM prints WHERE printer IS NOT NULL ORDER BY 1")]
            mats = [r[0] for r in db.execute("SELECT DISTINCT UPPER(material) FROM usage WHERE material IS NOT NULL AND material<>'' ORDER BY 1")]
            spools = [dict(r) for r in db.execute(
                "SELECT spool, MAX(spool_name) name, MAX(color) color, SUM(grams) g FROM usage WHERE spool IS NOT NULL GROUP BY spool ORDER BY spool")]
        return {"printers": printers, "materials": mats, "spools": spools}

    def csv(self, f):
        data = self.query(f, limit=1_000_000)["entries"]
        buf = io.StringIO()
        buf.write("﻿")  # Excel erkennt so UTF-8
        w = csv.writer(buf, delimiter=";")
        de = lambda v, n=2: "" if v is None else f"{v:.{n}f}".replace(".", ",")
        w.writerow(["Datum", "Uhrzeit", "Drucker", "Modell", "Ergebnis", "Dauer (min)", "Slot", "Spule", "Spulenname",
                    "Material", "Filament (g)", "Filamentkosten (€)", "Strom (kWh)", "Stromkosten (€)",
                    "Verschleiß (€)", "Gesamtkosten Druck (€)", "Gebucht"])
        for e in data:
            t = time.localtime(e["time"])
            base = [time.strftime("%d.%m.%Y", t), time.strftime("%H:%M", t), e["printer"], e["job"], e["result"],
                    round((e["duration"] or 0) / 60) if e["duration"] is not None else ""]
            tail = [de(e["kwh"], 3), de(e["energy_cost"]), de(e["wear_cost"]), de(e["total_cost"])]
            if not e["usage"]:
                w.writerow(base + ["", "", "", "", "", ""] + tail + [""])
            for i, u in enumerate(e["usage"]):
                # Druckkosten nur in der ersten Zeile, damit Summen in Excel stimmen
                w.writerow(base + [u["slot"], u["spool"] or "", u["spool_name"] or "", u["material"] or "",
                                   de(u["grams"], 1), de(u["cost"])] + (tail if i == 0 else ["", "", "", ""]) +
                           ["ja" if u["booked"] else (u["error"] or "nein")])
        return buf.getvalue()

    def import_old(self, entries):
        """Übernimmt den bisherigen Verlauf aus state.json (einmalig)."""
        for e in reversed(entries):
            self.add(e)
