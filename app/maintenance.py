"""Wartung mit mehreren Aufgaben – angelehnt an Bambuddy.

Gespeichert in state.json unter "maintenance2":
  types:    [{id, name, desc, icon, interval, unit ("h"|"d"), system, rails}]
  printers: {pid: {offset: Stunden-Startwert, items: {tid: {interval, enabled, last_hours, last_time, notified}}}}
  log:      [{time, pid, printer, tid, name, hours, note}]
"""
import re
import time
import uuid

# rails: nur für Drucker mit dieser Führung standardmäßig aktiv ("" = alle)
DEFAULT_TYPES = [
    {"id": "clean_plate", "name": "Druckplatte reinigen", "desc": "Gründlich mit Spülmittel oder IPA reinigen",
     "icon": "🧽", "interval": 25, "unit": "h", "rails": ""},
    {"id": "lube_rails", "name": "Linearschienen schmieren", "desc": "Schienen dünn mit Fett bzw. Öl versorgen",
     "icon": "💧", "interval": 50, "unit": "h", "rails": "linear"},
    {"id": "clean_rails", "name": "Linearschienen reinigen", "desc": "Mit trockenem Tuch Staub und Abrieb entfernen",
     "icon": "✨", "interval": 100, "unit": "h", "rails": "linear"},
    {"id": "clean_nozzle", "name": "Düse/Hotend reinigen", "desc": "Düse außen säubern, bei Bedarf Cold Pull",
     "icon": "🔥", "interval": 100, "unit": "h", "rails": ""},
    {"id": "check_belts", "name": "Riemenspannung prüfen", "desc": "X/Y-Riemen prüfen und nachspannen",
     "icon": "📏", "interval": 200, "unit": "h", "rails": ""},
    {"id": "check_ptfe", "name": "PTFE-Schlauch prüfen", "desc": "Auf Abrieb und Verfärbung kontrollieren",
     "icon": "🧵", "interval": 500, "unit": "h", "rails": ""},
    {"id": "clean_carbon", "name": "Carbonstangen reinigen", "desc": "Nur trocken abwischen, nicht schmieren (X1/P1)",
     "icon": "✨", "interval": 100, "unit": "h", "rails": "carbon"},
    {"id": "lube_steel", "name": "Stahlstangen schmieren", "desc": "Stangen dünn schmieren (P2S)",
     "icon": "💧", "interval": 50, "unit": "h", "rails": "steel"},
    {"id": "clean_steel", "name": "Stahlstangen reinigen", "desc": "Mit trockenem Tuch abwischen (P2S)",
     "icon": "✨", "interval": 100, "unit": "h", "rails": "steel"},
]

# Bambuddy-Namen -> unsere IDs (für den Import)
BAMBUDDY_NAMES = {
    "Clean Build Plate": "clean_plate", "Lubricate Linear Rails": "lube_rails", "Clean Linear Rails": "clean_rails",
    "Clean Nozzle/Hotend": "clean_nozzle", "Check Belt Tension": "check_belts", "Check PTFE Tube": "check_ptfe",
    "Clean Carbon Rods": "clean_carbon", "Lubricate Steel Rods": "lube_steel", "Clean Steel Rods": "clean_steel",
}
BAMBUDDY_ICONS = {"Droplet": "💧", "Sparkles": "✨", "Flame": "🔥", "Ruler": "📏", "Square": "🧽", "Cable": "🧵",
                  "Wrench": "🔧", "Fan": "🌀", "Settings": "⚙️"}


def rails_of(printer):
    """Welche Führung hat der Drucker? A1/H2 und Klipper-Drucker: Linearschienen."""
    model = (getattr(printer, "model", "") or getattr(printer, "name", "") or "").upper()
    if re.search(r"\bX1|\bP1", model):
        return "carbon"
    if "P2S" in model:
        return "steel"
    return "linear"


class Maintenance:
    def __init__(self, store):
        self.store = store
        with store.lock:
            d = store.data.get("maintenance2")
            if not d:
                d = {"types": [dict(t, system=True) for t in DEFAULT_TYPES], "printers": {}, "log": []}
                old = store.data.get("maintenance") or {}   # alter Stand: ein allgemeiner Zähler
                for pid, m in old.items():
                    if m.get("reset_time"):
                        d["printers"][pid] = {"offset": 0.0, "items": {
                            t["id"]: {"last_hours": m.get("reset_hours") or 0, "last_time": m["reset_time"]}
                            for t in DEFAULT_TYPES}}
                store.data["maintenance2"] = d
                store._save()

    @property
    def d(self):
        return self.store.data["maintenance2"]

    def _type(self, tid):
        return next((t for t in self.d["types"] if t["id"] == tid), None)

    def _p(self, pid):
        return self.d["printers"].setdefault(pid, {"offset": 0.0, "items": {}})

    def _item(self, printer, t):
        it = self._p(printer.id)["items"].setdefault(t["id"], {})
        if "enabled" not in it:
            if t.get("only"):
                it["enabled"] = t["only"] == printer.id
            else:
                it["enabled"] = not t.get("rails") or t["rails"] == rails_of(printer)
        return it

    # --- Stunden -------------------------------------------------------------
    def offset(self, pid):
        return float(self._p(pid).get("offset") or 0)

    def set_total_hours(self, pid, total, history_hours):
        with self.store.lock:
            self._p(pid)["offset"] = round(max(0.0, float(total) - history_hours), 2)
            self.store._save()

    # --- Status --------------------------------------------------------------
    def item_status(self, printer, t, hours):
        it = self._item(printer, t)
        interval = float(it.get("interval") or t["interval"] or 0)
        now = time.time()
        if t.get("unit") == "d":
            ref = it.get("last_time") or it.get("created") or now
            it.setdefault("created", int(now))
            since = (now - ref) / 86400
        else:
            since = max(0.0, hours - (it.get("last_hours") or 0))
        due = bool(interval) and since >= interval
        return {"id": t["id"], "name": t["name"], "icon": t.get("icon") or "🔧", "desc": t.get("desc") or "",
                "unit": t.get("unit") or "h", "enabled": bool(it.get("enabled")),
                "interval": interval, "default_interval": float(t["interval"]), "custom": it.get("interval") is not None,
                "since": round(since, 1), "remaining": round(interval - since, 1) if interval else None,
                "pct": min(100, round(since / interval * 100)) if interval else 0,
                "due": due, "warn": (not due) and bool(interval) and since >= interval * 0.9,
                "last_time": it.get("last_time")}

    def printer_status(self, printer, hours):
        items = [self.item_status(printer, t, hours) for t in self.d["types"]]
        active = [i for i in items if i["enabled"]]
        active.sort(key=lambda i: (not i["due"], not i["warn"], -(i["pct"] or 0)))
        return {"id": printer.id, "name": printer.name, "hours": round(hours, 1), "offset": self.offset(printer.id),
                "items": active, "all": items,
                "due": sum(i["due"] for i in active), "warn": sum(i["warn"] for i in active)}

    # --- Ändern ----------------------------------------------------------------
    def done(self, printer, tid, hours, note=""):
        t = self._type(tid)
        if not t:
            raise LookupError("Unbekannte Wartungsaufgabe")
        with self.store.lock:
            it = self._item(printer, t)
            it.update(last_hours=round(hours, 2), last_time=int(time.time()), notified=False)
            self.d["log"].insert(0, {"time": int(time.time()), "pid": printer.id, "printer": printer.name,
                                     "tid": tid, "name": t["name"], "hours": round(hours, 1), "note": str(note or "")[:200]})
            del self.d["log"][300:]
            self.store._save()

    def set_item(self, printer, tid, data):
        t = self._type(tid)
        if not t:
            raise LookupError("Unbekannte Wartungsaufgabe")
        with self.store.lock:
            it = self._item(printer, t)
            if "enabled" in data:
                it["enabled"] = bool(data["enabled"])
            if "interval" in data:
                v = data["interval"]
                it["interval"] = None if v in (None, "") else max(0.0, float(str(v).replace(",", ".")))
            it["notified"] = False
            self.store._save()

    def save_type(self, data, tid=None):
        name = str(data.get("name") or "").strip()
        unit = data.get("unit") or "h"
        if unit not in ("h", "d"):
            raise ValueError("Einheit muss h oder d sein")
        with self.store.lock:
            if tid:
                t = self._type(tid)
                if not t:
                    raise LookupError("Unbekannte Wartungsaufgabe")
            else:
                if not name:
                    raise ValueError("Bitte einen Namen eingeben")
                t = {"id": "c_" + uuid.uuid4().hex[:8], "system": False, "rails": ""}
                self.d["types"].append(t)
            if name:
                t["name"] = name[:60]
            if "desc" in data:
                t["desc"] = str(data["desc"] or "")[:200]
            if data.get("icon"):
                t["icon"] = str(data["icon"])[:4]
            t.setdefault("icon", "🔧")
            if "interval" in data or not tid:
                t["interval"] = max(0.0, float(str(data.get("interval") or 0).replace(",", ".")))
            if "unit" in data or not tid:
                t["unit"] = unit
            self.store._save()
            return t

    def delete_type(self, tid):
        with self.store.lock:
            self.d["types"] = [t for t in self.d["types"] if t["id"] != tid]
            for p in self.d["printers"].values():
                p["items"].pop(tid, None)
            self.store._save()

    def restore_defaults(self):
        with self.store.lock:
            have = {t["id"] for t in self.d["types"]}
            for t in DEFAULT_TYPES:
                if t["id"] not in have:
                    self.d["types"].append(dict(t, system=True))
                else:
                    cur = self._type(t["id"])
                    cur.update(name=t["name"], desc=t["desc"], icon=t["icon"], interval=t["interval"], unit=t["unit"])
            order = {t["id"]: i for i, t in enumerate(DEFAULT_TYPES)}
            self.d["types"].sort(key=lambda t: order.get(t["id"], 99))
            self.store._save()

    def log(self, n=50):
        return self.d["log"][:n]

    # --- Benachrichtigen ------------------------------------------------------
    def check(self, printer, hours):
        due = []
        with self.store.lock:
            for t in self.d["types"]:
                st = self.item_status(printer, t, hours)
                it = self._item(printer, t)
                if st["enabled"] and st["due"] and not it.get("notified"):
                    due.append(st)
                    it["notified"] = True
            if due:
                self.store._save()
        if due:
            printer.notify("maintenance", "Wartung fällig 🔧",
                           "\n".join(f"{s['icon']} {s['name']} ({s['since']:g} {'Tage' if s['unit'] == 'd' else 'h'})"
                                     for s in due), "wrench", "default")

    # --- Import aus Bambuddy ---------------------------------------------------
    def import_bambuddy(self, printer, overview, history_hours):
        """overview = ein Eintrag aus GET /api/v1/maintenance/overview (Bambuddy)."""
        total = float(overview.get("total_print_hours") or 0)
        n = 0
        seen = set()
        with self.store.lock:
            p = self._p(printer.id)
            p["offset"] = round(max(0.0, total - history_hours), 2)
            for bi in overview.get("maintenance_items") or []:
                bname = bi.get("maintenance_type_name") or ""
                tid = BAMBUDDY_NAMES.get(bname)
                t = self._type(tid) if tid else next((x for x in self.d["types"] if x["name"] == bname), None)
                unit = "d" if bi.get("interval_type") == "days" else "h"
                if not t:
                    t = {"id": "c_" + uuid.uuid4().hex[:8], "system": False, "rails": "", "name": bname[:60],
                         "desc": "aus Bambuddy übernommen", "icon": BAMBUDDY_ICONS.get(bi.get("maintenance_type_icon"), "🔧"),
                         "interval": float(bi.get("interval_hours") or 0), "unit": unit, "only": printer.id}
                    self.d["types"].append(t)
                    for opid, op in self.d["printers"].items():   # neue Aufgabe nur für diesen Drucker an
                        if opid != printer.id:
                            op["items"].setdefault(t["id"], {})["enabled"] = False
                it = self._item(printer, t)
                seen.add(t["id"])
                it["enabled"] = bool(bi.get("enabled", True))
                iv = float(bi.get("interval_hours") or 0)
                it["interval"] = None if abs(iv - float(t["interval"])) < 0.01 else iv
                if unit == "h" and bi.get("hours_since_maintenance") is not None:
                    it["last_hours"] = round(total - float(bi["hours_since_maintenance"]), 2)
                lp = bi.get("last_performed_at")
                if lp:
                    try:
                        from datetime import datetime
                        it["last_time"] = int(datetime.fromisoformat(str(lp).replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        pass
                it["notified"] = bool(bi.get("is_due"))   # bereits fällige nicht sofort nochmal melden
                n += 1
            for t in self.d["types"]:   # Aufgaben, die Bambuddy nicht kennt: ab jetzt neu zählen
                if t["id"] not in seen and t.get("unit") != "d":
                    it = self._item(printer, t)
                    if it.get("last_hours") is None:
                        it["last_hours"] = round(total, 2)
            self.store._save()
        return {"items": n, "hours": round(total, 1)}
