"""Bambuddy-API (v1): Version, Warteschlange, Wartung. Ohne Anmeldung oder mit API-Schlüssel."""
import logging

import requests

log = logging.getLogger("bambuddy")


class Bambuddy:
    def __init__(self, url="", key=""):
        self.s = requests.Session()
        self.configure(url, key)

    def configure(self, url, key=""):
        self.url = (url or "").rstrip("/")
        self.key = key or ""

    @property
    def enabled(self):
        return bool(self.url)

    def _h(self):
        return {"X-API-Key": self.key} if self.key else {}

    def get(self, path, timeout=6, **params):
        r = self.s.get(f"{self.url}/api/v1{path}", params=params or None, headers=self._h(), timeout=timeout)
        if r.status_code in (401, 403):
            raise PermissionError("Bambuddy verlangt eine Anmeldung – API-Schlüssel in den Einstellungen eintragen")
        r.raise_for_status()
        return r

    def post(self, path, timeout=10):
        r = self.s.post(f"{self.url}/api/v1{path}", headers=self._h(), timeout=timeout)
        if r.status_code in (401, 403):
            raise PermissionError("Bambuddy verweigert das (API-Schlüssel ohne Rechte für die Warteschlange?)")
        if r.status_code >= 400:
            try:
                msg = r.json().get("detail")
            except Exception:
                msg = r.text[:200]
            raise RuntimeError(f"Bambuddy: {msg}")
        return r.json() if r.content else {}

    def send(self, method, path, body=None, timeout=10):
        r = self.s.request(method, f"{self.url}/api/v1{path}", json=body, headers=self._h(), timeout=timeout)
        if r.status_code in (401, 403):
            raise PermissionError("Bambuddy verweigert das – API-Schlüssel braucht Rechte für Smart Plugs")
        if r.status_code >= 400:
            try:
                msg = r.json().get("detail")
            except Exception:
                msg = r.text[:200]
            raise RuntimeError(f"Bambuddy {r.status_code}: {msg}")
        return r.json() if r.content else {}

    # --- Steckdosen (Smart Plugs) ---
    def printers(self):
        return self.get("/printers/").json() or []

    def plug_for_printer(self, printer_id):
        r = self.s.get(f"{self.url}/api/v1/smart-plugs/by-printer/{printer_id}", headers=self._h(), timeout=6)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        return d if d and d.get("id") else None

    def plug_status(self, plug_id):
        return self.get(f"/smart-plugs/{plug_id}/status", timeout=8).json()

    def plug_control(self, plug_id, on):
        return self.send("POST", f"/smart-plugs/{plug_id}/control", {"action": "on" if on else "off"}, timeout=15)

    def plug_update(self, plug_id, data):
        return self.send("PATCH", f"/smart-plugs/{plug_id}", data)

    def version(self):
        return self.get("/updates/version", timeout=4).json().get("version")

    def queue(self):
        items = self.get("/queue/", status="pending,printing").json()
        out = []
        for it in items:
            if it.get("archive_id") and it.get("archive_thumbnail"):
                thumb = f"/archives/{it['archive_id']}/thumbnail"
            elif it.get("library_file_id") and it.get("library_file_thumbnail"):
                thumb = f"/library/files/{it['library_file_id']}/thumbnail"
            else:
                thumb = None
            out.append({
                "id": it["id"], "status": it.get("status"), "position": it.get("position"),
                "name": it.get("archive_name") or it.get("library_file_name") or f"Auftrag {it['id']}",
                "printer": it.get("printer_name") or it.get("target_model") or "beliebiger Drucker",
                "seconds": it.get("print_time_seconds"), "grams": it.get("filament_used_grams"),
                "material": it.get("filament_type"), "color": it.get("filament_color"),
                "manual": bool(it.get("manual_start")), "waiting": it.get("waiting_reason"),
                "short": bool(it.get("filament_short")), "scheduled": it.get("scheduled_time"),
                "plate": it.get("bed_type"), "thumb": thumb,
                # für Filament-Check / Planung
                "archive_id": it.get("archive_id"), "library_file_id": it.get("library_file_id"), "plate_id": it.get("plate_id"),
                "ams_mapping": it.get("ams_mapping"), "printer_id": it.get("printer_id"),
                "filament_type": it.get("filament_type"), "filament_color": it.get("filament_color"),
                "filament_used_grams": it.get("filament_used_grams"),
            })
        out.sort(key=lambda x: (x["status"] != "printing", x["position"] or 0))
        return out

    def reorder(self, ids):
        """ids: gewünschte Reihenfolge der wartenden Aufträge."""
        return self.send("POST", "/queue/reorder", {"items": [{"id": int(i), "position": n + 1} for n, i in enumerate(ids)]})

    def history(self, limit=60):
        """Erledigte Aufträge. Neuere Bambuddy: /queue/history – sonst die normale Liste mit Status-Filter."""
        if getattr(self, "_hist_mode", None) != "list":
            for lim in (limit, 50):
                try:
                    d = self.get("/queue/history", timeout=10, limit=lim).json()
                    self._hist_mode = "history"
                    return d.get("items", d) if isinstance(d, dict) else d
                except Exception as e:
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    if code not in (404, 405, 422):
                        raise
            self._hist_mode = "list"
        def lst(**p):
            try:
                d = self.get("/queue/", timeout=10, **p).json()
                return d.get("items", d) if isinstance(d, dict) else (d or [])
            except Exception:
                return []
        items = lst(status="completed,failed,cancelled,skipped")
        if not items:                       # manche Versionen kennen nur einen Status je Abfrage
            for stt in ("completed", "failed", "cancelled", "skipped"):
                items += lst(status=stt)
        if not items:
            items = lst()
        items = [i for i in items if i.get("status") not in ("pending", "printing")]
        items.sort(key=lambda i: i.get("completed_at") or i.get("started_at") or i.get("created_at") or "", reverse=True)
        return items[:limit]

    def thumbnail(self, path):
        r = self.get(path, timeout=8)
        return r.content, r.headers.get("content-type", "image/png")

    def schedule(self, item_id, when_utc_iso):
        """Geplante Startzeit setzen (UTC-ISO) – None löscht sie."""
        body = {"scheduled_time": when_utc_iso}
        if when_utc_iso:
            body["manual_start"] = True          # geplant → immer über den Druckwächter (Plattenprüfung)
        try:
            return self.send("PATCH", f"/queue/{int(item_id)}", body)
        except Exception:
            if len(body) == 1:
                raise
            return self.send("PATCH", f"/queue/{int(item_id)}", {"scheduled_time": when_utc_iso})

    def start(self, item_id):
        return self.post(f"/queue/{int(item_id)}/start")

    def cancel(self, item_id):
        return self.post(f"/queue/{int(item_id)}/cancel")

    def maintenance_overview(self):
        return self.get("/maintenance/overview", timeout=15).json()

    def print_log(self):
        """Alle abgeschlossenen Drucke (Bambuddy-Druckprotokoll), älteste zuerst."""
        out, off = [], 0
        while True:
            d = self.get("/print-log/", timeout=20, limit=500, offset=off, sort_by="date", sort_dir="asc").json()
            items = d.get("items") or []
            out += items
            off += len(items)
            if not items or off >= (d.get("total") or 0):
                return out

    def print_log_thumbnail(self, entry_id):
        r = self.get(f"/print-log/{int(entry_id)}/thumbnail", timeout=10)
        return r.content if r.headers.get("content-type", "").startswith("image/") else None
