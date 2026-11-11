"""Anbindung an die Spoolman-REST-API (v1)."""
import logging
import threading
import time

import requests

log = logging.getLogger("spoolman")


class Spoolman:
    def __init__(self, url: str, timeout: float = 10):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self._cache = None
        self._cache_time = 0
        self._lock = threading.Lock()

    def _api(self, path):
        return f"{self.url}/api/v1{path}"

    def spools(self, max_age=10):
        """Alle nicht archivierten Spulen (kurz gecacht)."""
        with self._lock:
            if self._cache is not None and time.time() - self._cache_time < max_age:
                return self._cache
            r = self.s.get(self._api("/spool"), params={"allow_archived": "false"}, timeout=self.timeout)
            r.raise_for_status()
            self._cache = [self.simplify(sp) for sp in r.json()]
            self._cache_time = time.time()
            return self._cache

    def vendors_by_spool(self):
        """Spulen-ID -> Hersteller (auch archivierte), für die Statistik."""
        r = self.s.get(self._api("/spool"), params={"allow_archived": "true"}, timeout=self.timeout)
        r.raise_for_status()
        return {sp["id"]: ((sp.get("filament") or {}).get("vendor") or {}).get("name") or "ohne Hersteller" for sp in r.json()}

    def invalidate(self):
        with self._lock:
            self._cache = None

    def spool(self, spool_id):
        for sp in self.spools():
            if sp["id"] == spool_id:
                return sp
        r = self.s.get(self._api(f"/spool/{spool_id}"), timeout=self.timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self.simplify(r.json())

    def use(self, spool_id, grams: float):
        r = self.s.put(self._api(f"/spool/{spool_id}/use"), json={"use_weight": round(grams, 2)}, timeout=self.timeout)
        r.raise_for_status()
        self.invalidate()
        return r.json()

    def set_location(self, spool_id, location: str):
        r = self.s.patch(self._api(f"/spool/{spool_id}"), json={"location": location}, timeout=self.timeout)
        r.raise_for_status()
        self.invalidate()

    # --- Lager: Hersteller, Sorten, Spulen ----------------------------------------
    def _get(self, path, **params):
        r = self.s.get(self._api(path), params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _send(self, method, path, body):
        r = self.s.request(method, self._api(path), json=body, timeout=self.timeout)
        if r.status_code >= 400:
            try:
                msg = r.json()
            except Exception:
                msg = r.text
            raise RuntimeError(f"Spoolman {r.status_code}: {msg}")
        self.invalidate()
        return r.json() if r.content else None

    def inventory(self, archived=False):
        """Rohdaten für die Lager-Seite (auf Wunsch inkl. archivierter Spulen)."""
        return {
            "vendors": self._get("/vendor"),
            "filaments": self._get("/filament"),
            "spools": self._get("/spool", allow_archived="true" if archived else "false"),
        }

    _ext_cache = None
    _ext_time = 0

    def external_filaments(self, max_age=3600):
        """Filamente aus der SpoolmanDB (Spoolman hält sie lokal vor)."""
        if self._ext_cache is not None and time.time() - self._ext_time < max_age:
            return self._ext_cache
        r = self.s.get(self._api("/external/filament"), timeout=30)
        if r.status_code == 404:
            raise RuntimeError("Diese Spoolman-Version bietet keine externe Datenbank (bitte Spoolman aktualisieren)")
        r.raise_for_status()
        Spoolman._ext_cache = r.json()
        Spoolman._ext_time = time.time()
        return self._ext_cache

    def external_manufacturers(self):
        counts = {}
        for f in self.external_filaments():
            m = (f.get("manufacturer") or "").strip()
            if m:
                counts[m] = counts.get(m, 0) + 1
        return sorted(({"name": k, "count": v} for k, v in counts.items()), key=lambda x: x["name"].lower())

    def search_external(self, q="", manufacturer="", material="", limit=200):
        """Gibt (Treffer, Gesamtzahl, Materialien des Herstellers) zurück."""
        words = [w for w in q.lower().replace(",", " ").split() if w]
        man, mat = manufacturer.strip().lower(), material.strip().upper()
        if not words and not man:
            return [], 0, []
        hits, mats = [], set()
        for f in self.external_filaments():
            if man and (f.get("manufacturer") or "").strip().lower() != man:
                continue
            hay = " ".join(str(f.get(k) or "") for k in ("manufacturer", "name", "material", "finish")).lower()
            if not all(w in hay for w in words):
                continue
            m = (f.get("material") or "").upper()
            if m:
                mats.add(m)
            if mat and m != mat:
                continue
            hits.append(f)
        hits.sort(key=lambda f: ((f.get("material") or ""), (f.get("name") or "").lower()))
        return hits[:limit], len(hits), sorted(mats)

    def create_vendor(self, name, empty_spool_weight=None):
        body = {"name": name}
        if empty_spool_weight:
            body["empty_spool_weight"] = float(empty_spool_weight)
        return self._send("POST", "/vendor", body)

    def create_filament(self, data):
        return self._send("POST", "/filament", data)

    def update_filament(self, fid, data):
        return self._send("PATCH", f"/filament/{fid}", data)

    def create_spools(self, filament_id, count, location="", initial_weight=None, price=None):
        made = []
        for _ in range(int(count)):
            body = {"filament_id": int(filament_id)}
            if location:
                body["location"] = location
            if initial_weight:
                body["initial_weight"] = float(initial_weight)
            if price:
                body["price"] = float(price)
            made.append(self._send("POST", "/spool", body)["id"])
        return made

    def delete_spool(self, spool_id):
        r = self.s.delete(self._api(f"/spool/{int(spool_id)}"), timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Spoolman {r.status_code}: {r.text[:200]}")
        self.invalidate()

    def update_spool(self, spool_id, data):
        return self._send("PATCH", f"/spool/{int(spool_id)}", data)

    def spool_raw(self, spool_id):
        return self._get(f"/spool/{int(spool_id)}")

    @staticmethod
    def empty_weight(sp_raw):
        """Leergewicht: Rolle → Sorte → Hersteller (wie Spoolman selbst)."""
        fil = sp_raw.get("filament") or {}
        return sp_raw.get("spool_weight") or fil.get("spool_weight") or (fil.get("vendor") or {}).get("empty_spool_weight")

    def archive_spool(self, spool_id, archived=True):
        return self._send("PATCH", f"/spool/{int(spool_id)}", {"archived": bool(archived)})

    @staticmethod
    def simplify(sp):
        fil = sp.get("filament") or {}
        vendor = (fil.get("vendor") or {}).get("name", "")
        color = fil.get("color_hex") or (fil.get("multi_color_hexes") or "").split(",")[0] or ""
        fname, mat = fil.get("name") or "", fil.get("material") or ""
        if mat and fname.upper().endswith(mat.upper()):
            mat = ""  # Material steht schon im Namen (z. B. "Black PLA+")
        name = " ".join(x for x in [vendor, fname, mat] if x).strip()
        return {
            "id": sp["id"],
            "name": name or f"Spule {sp['id']}",
            "material": fil.get("material") or "",
            "color": ("#" + color.lstrip("#")) if color else "",
            "remaining": sp.get("remaining_weight"),
            "location": sp.get("location") or "",
            "filament_id": fil.get("id"),
            "used": sp.get("used_weight") or 0,
            "first_used": sp.get("first_used"),
            "initial": sp.get("initial_weight") or fil.get("weight"),
            "price": sp.get("price") or fil.get("price"),
            "extruder_temp": fil.get("settings_extruder_temp"),
        }
