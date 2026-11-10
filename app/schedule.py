"""Druckfenster und Zeitplan-Vorschau der Warteschlange – rechnet mit denselben Regeln wie der Druckwächter.

Druckfenster (aus der Druckwächter-Konfiguration):
  DRUCK_AB / DRUCK_BIS, pro Wochentag DRUCK_AB_MO …, für Sa+So DRUCK_AB_WE; „aus“ = an dem Tag nicht drucken.
  Ein Auftrag startet nur, wenn Start ≥ Fensterbeginn und Start + Dauer + Puffer ≤ Fensterende.
Geplante Startzeit (Bambuddy „scheduled_time“): nicht vorher. „Trotz Druckfenster“ = Fenster egal.
"""
from datetime import datetime, timedelta

TAGE = ["MO", "DI", "MI", "DO", "FR", "SA", "SO"]
TAGNAME = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
ABRAEUMEN_MIN = 10      # zwischen zwei Drucken: Teil abnehmen, Platte prüfen, Aufheizen


class Window:
    def __init__(self, env):
        self.env = env or {}
        self.ab = (self.env.get("DRUCK_AB") or "07:00").strip()
        self.bis = (self.env.get("DRUCK_BIS") or "22:30").strip()
        try:
            self.puffer = int(float(self.env.get("PUFFER_MIN") or 15))
        except ValueError:
            self.puffer = 15

    def zeiten(self, d):
        tag = TAGE[d.weekday()]
        we = tag in ("SA", "SO")
        ab = self.env.get(f"DRUCK_AB_{tag}") or (we and self.env.get("DRUCK_AB_WE")) or self.ab
        bis = self.env.get(f"DRUCK_BIS_{tag}") or (we and self.env.get("DRUCK_BIS_WE")) or self.bis
        if str(ab).strip().lower() in ("aus", "-", "off") or str(bis).strip().lower() in ("aus", "-", "off"):
            return None
        return str(ab).strip(), str(bis).strip()

    @staticmethod
    def _at(hhmm, d):
        h, m = map(int, hhmm.split(":"))
        return d.replace(hour=h, minute=m, second=0, microsecond=0)

    def fenster(self, d):
        z = self.zeiten(d)
        if not z:
            return None
        return self._at(z[0], d), self._at(z[1], d)

    def laengstes(self, ref):
        best = timedelta(0)
        for i in range(7):
            f = self.fenster(ref + timedelta(days=i))
            if f:
                best = max(best, f[1] - f[0])
        return best

    def fruehester_start(self, ab_zeit, dauer_s):
        """Frühester Start ≥ ab_zeit, der samt Puffer ins Fenster passt. None = passt nie."""
        need = timedelta(seconds=dauer_s, minutes=self.puffer)
        if need > self.laengstes(ab_zeit):
            return None
        for i in range(15):
            tag = (ab_zeit + timedelta(days=i)).replace(hour=12, minute=0, second=0, microsecond=0)
            f = self.fenster(tag)
            if not f:
                continue
            start = max(f[0], ab_zeit)
            if start + need <= f[1]:
                return start
        return None

    def im_fenster(self, start, dauer_s):
        f = self.fenster(start)
        return bool(f and f[0] <= start and start + timedelta(seconds=dauer_s, minutes=self.puffer) <= f[1])


def wann(dt, now):
    """„heute 15:20“, „morgen 07:00“, „Do 07:00“."""
    tage = (dt.date() - now.date()).days
    wort = "heute" if tage == 0 else "morgen" if tage == 1 else "übermorgen" if tage == 2 else TAGNAME[dt.weekday()] + f" {dt:%d.%m.}"
    return f"{wort} {dt:%H:%M}"


def plan(items, window, now, printing_left_s=None, forced=None):
    """Genau wie der Druckwächter:
      1. Erzwungene Aufträge („trotz Druckfenster“), deren geplante Zeit erreicht ist, zuerst.
      2. Sonst in Reihenfolge: Aufträge ohne Dauer und solche, die in kein Fenster passen, werden übersprungen;
         geplante Aufträge nicht vor ihrer Zeit (übersprungen); der erste übrige Auftrag startet, wenn er ins
         Fenster passt – sonst wartet ALLES dahinter mit (Reihenfolge bleibt).
      Bambuddy-Aufträge ohne „manuell starten“ startet Bambuddy selbst, sofort (ohne Druckfenster).
    Rückgabe: ({id: {"start", "end", "text", "note"}}, Gesamtende)."""
    forced = forced or set()
    t = now + timedelta(seconds=printing_left_s, minutes=ABRAEUMEN_MIN) if printing_left_s else now
    rest = sorted(items, key=lambda q: (q.get("position") or 0, q["id"]))
    out, ende = {}, None
    for q in [q for q in rest if not q.get("seconds")]:
        out[q["id"]] = {"start": None, "end": None, "note": "unbekannt", "text": "Dauer unbekannt – Start von Hand"}
    rest = [q for q in rest if q.get("seconds")]
    longest = window.laengstes(now)
    for q in list(rest):
        if q["id"] not in forced and q.get("manual", True) and \
                timedelta(seconds=q["seconds"], minutes=window.puffer) > longest:
            out[q["id"]] = {"start": None, "end": None, "note": "zu_lang", "text": "passt in kein Druckfenster – wartet auf Freigabe"}
            rest.remove(q)
    for _ in range(300):
        if not rest:
            break
        chosen, wait = None, None
        due_forced = [q for q in rest if q["id"] in forced and (not q.get("scheduled") or q["scheduled"] <= t)]
        for q in rest:                                  # erzwungene geplante Aufträge wecken zu ihrer Zeit
            if q["id"] in forced and q.get("scheduled") and q["scheduled"] > t:
                wait = min(wait, q["scheduled"]) if wait else q["scheduled"]
        if due_forced:
            chosen = due_forced[0]
        else:
            for q in rest:
                if q.get("scheduled") and q["scheduled"] > t:
                    wait = min(wait, q["scheduled"]) if wait else q["scheduled"]
                    continue
                if q["id"] in forced or not q.get("manual", True) or window.im_fenster(t, q["seconds"]):
                    chosen = q
                else:
                    nxt = window.fruehester_start(t, q["seconds"])
                    if nxt:
                        wait = min(wait, nxt) if wait else nxt
                break
        if chosen:
            e = t + timedelta(seconds=chosen["seconds"])
            note = "geplant" if chosen.get("scheduled") and abs((chosen["scheduled"] - t).total_seconds()) < 90 else ""
            out[chosen["id"]] = {"start": t.timestamp(), "end": e.timestamp(), "note": note,
                                 "text": f"{wann(t, now)} → {e:%H:%M}" + (" (geplant)" if note else "")
                                 + (" · außerhalb Druckfenster" if chosen["id"] in forced and not window.im_fenster(t, chosen["seconds"]) else "")}
            ende = e
            t = e + timedelta(minutes=ABRAEUMEN_MIN)
            rest.remove(chosen)
        elif wait:
            t = wait
        else:
            for q in rest:
                out[q["id"]] = {"start": None, "end": None, "note": "zu_lang", "text": "kein passendes Druckfenster gefunden"}
            break
    return out, (ende.timestamp() if ende else None)


def plan_start_for_end(window, end, dauer_s):
    """„Fertig um“ → Startzeit (ohne Puffer, der ist Sicherheitszuschlag am Ende)."""
    return end - timedelta(seconds=dauer_s)
