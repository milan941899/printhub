"""Filament-Check für Aufträge in der Bambuddy-Warteschlange.

Pro Farbe des Drucks (aus der 3MF über Bambuddy): Welcher Slot wird benutzt, passt das Material,
passt die Farbe ungefähr, und reicht die Spule (Restgewicht aus Spoolman)?
"""
import time

RESERVE_G = 5            # so viel soll mindestens übrig bleiben (zusätzlich zur Einstellung)


def _hex(c):
    c = str(c or "").lstrip("#")[:6]
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) if len(c) == 6 else None
    except ValueError:
        return None


def color_dist(a, b):
    a, b = _hex(a), _hex(b)
    if not a or not b:
        return None
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def same_material(a, b):
    n = lambda s: str(s or "").upper().replace("-", " ").split()[0] if str(s or "").strip() else ""
    na, nb = n(a), n(b)
    if not na or not nb:
        return None
    return na == nb or na.startswith(nb) or nb.startswith(na)


class QueueCheck:
    def __init__(self, bambuddy, store, spool_map_fn, reserve_fn=lambda: 10):
        self.bb, self.store, self.spool_map = bambuddy, store, spool_map_fn
        self.reserve = reserve_fn
        self._req = {}            # (archive/library, id, plate) -> (zeit, filaments)

    def requirements(self, item):
        if item.get("archive_id"):
            key = ("a", item["archive_id"], item.get("plate_id"))
            path = f"/archives/{item['archive_id']}/filament-requirements"
        elif item.get("library_file_id"):
            key = ("l", item["library_file_id"], item.get("plate_id"))
            path = f"/library/files/{item['library_file_id']}/filament-requirements"
        else:
            return None
        hit = self._req.get(key)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        params = {"plate_id": item["plate_id"]} if item.get("plate_id") else {}
        try:
            d = self.bb.get(path, timeout=15, **params).json()
            fil = d.get("filaments") if isinstance(d, dict) else d
        except Exception:
            fil = None
        if fil is not None:
            self._req[key] = (time.time(), fil)
        return fil

    def check(self, item, printer):
        """printer: PrintHub-Drucker (oder None). Ergebnis: {"state": ok|warn|bad|unknown, "lines": [...], "text": "..."}"""
        if printer is None:
            return {"state": "unknown", "lines": [], "text": "Drucker nicht zugeordnet"}
        fil = self.requirements(item)
        if not fil:                              # Rückfall: nur Hauptmaterial + Gesamtgewicht
            fil = [{"slot_id": 1, "type": item.get("filament_type") or "", "color": item.get("filament_color") or "",
                    "used_grams": item.get("filament_used_grams") or 0}] if item.get("filament_type") else []
        if not fil:
            return {"state": "unknown", "lines": [], "text": "Filamentbedarf unbekannt"}
        try:
            slots = {s["key"]: s for s in printer.slots()}
        except Exception:
            slots = {}
        spools, _ = self.spool_map()
        mapping = item.get("ams_mapping") or []
        used_keys, lines = set(), []
        reserve = float(self.reserve() or 0) + RESERVE_G
        for f in sorted(fil, key=lambda x: -(x.get("used_grams") or 0)):
            need = float(f.get("used_grams") or 0)
            idx = int(f.get("slot_id") or 1) - 1
            key = None
            if 0 <= idx < len(mapping) and mapping[idx] is not None and int(mapping[idx]) >= 0:
                g = int(mapping[idx])
                key = "ext" if g in (254, 255) else f"ams{g // 4}-{g % 4}"
                how = "zugeordnet"
            else:                                 # Bambuddy ordnet beim Start selbst zu → passenden Slot suchen
                best = None
                for k, s in slots.items():
                    if k in used_keys or not s.get("material"):
                        continue
                    if same_material(s.get("material"), f.get("type")) is False:
                        continue
                    d = color_dist(s.get("color"), f.get("color"))
                    score = d if d is not None else 999
                    if best is None or score < best[0]:
                        best = (score, k)
                key = best[1] if best else None
                how = "passender Slot"
            ln = {"type": f.get("type") or "?", "color": f.get("color") or "", "grams": round(need, 1), "slot": None,
                  "spool": None, "have": None, "state": "ok", "msg": ""}
            if not key or key not in slots:
                ln.update(state="bad", msg="kein passender Slot geladen")
                lines.append(ln)
                continue
            used_keys.add(key)
            s = slots[key]
            ln["slot"] = s.get("label") or key
            mat_ok = same_material(s.get("material"), f.get("type"))
            if mat_ok is False:
                ln.update(state="bad", msg=f"{ln['slot']} hat {s.get('material')}, gebraucht wird {f.get('type')}")
            d = color_dist(s.get("color"), f.get("color"))
            if ln["state"] == "ok" and d is not None and d > 120:
                ln.update(state="warn", msg=f"Farbe in {ln['slot']} weicht ab")
            sid = self.store.get_slot(printer.id, key)
            sp = spools.get(sid) if sid else None
            if sp:
                ln["spool"], ln["have"] = sp.get("name"), sp.get("remaining")
                if sp.get("remaining") is not None and need and sp["remaining"] < need + reserve:
                    ln.update(state="bad", msg=f"Spule reicht nicht: {sp['remaining']:.0f} g da, {need:.0f} g nötig")
            elif ln["state"] == "ok":
                ln.update(state="warn", msg=f"{ln['slot']}: keine Spule aus dem Lager zugeordnet – Rest unbekannt")
            lines.append(ln)
        worst = "bad" if any(l["state"] == "bad" for l in lines) else "warn" if any(l["state"] == "warn" for l in lines) else "ok"
        probs = [l["msg"] for l in lines if l["state"] != "ok" and l["msg"]]
        text = "; ".join(probs) if probs else " · ".join(
            f"{l['type']} {l['grams']:.0f} g ({l['slot']}" + (f", {l['have']:.0f} g da" if l.get("have") is not None else "") + ")"
            for l in lines)
        return {"state": worst, "lines": lines, "text": text}
