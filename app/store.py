"""Kleiner JSON-Speicher: welche Spule steckt in welchem Slot, laufende Druckjobs, Verlauf."""
import json
import os
import threading
import time


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"slots": {}, "jobs": {}, "history": []}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # --- Slots -------------------------------------------------------------
    @staticmethod
    def _key(printer_id, slot):
        return f"{printer_id}:{slot}"

    def get_slot(self, printer_id, slot):
        with self.lock:
            return self.data["slots"].get(self._key(printer_id, slot))

    def set_slot(self, printer_id, slot, spool_id):
        """Setzt die Spule für einen Slot. Gibt (alte Spule im Slot, Slot-Key wo die Spule vorher war) zurück."""
        with self.lock:
            key = self._key(printer_id, slot)
            old_spool = self.data["slots"].get(key)
            moved_from = None
            if spool_id is not None:
                for k, v in list(self.data["slots"].items()):
                    if v == spool_id and k != key:
                        del self.data["slots"][k]
                        moved_from = k
                self.data["slots"][key] = spool_id
            else:
                self.data["slots"].pop(key, None)
            self._save()
            return old_spool, moved_from

    def slot_of_spool(self, spool_id):
        with self.lock:
            for k, v in self.data["slots"].items():
                if v == spool_id:
                    return k
        return None

    # --- Jobs --------------------------------------------------------------
    def get_job(self, printer_id):
        with self.lock:
            return self.data["jobs"].get(printer_id)

    def set_job(self, printer_id, job):
        with self.lock:
            if job is None:
                self.data["jobs"].pop(printer_id, None)
            else:
                self.data["jobs"][printer_id] = job
            self._save()

    # --- Verlauf -----------------------------------------------------------
    def add_history(self, entry):
        with self.lock:
            entry = {"time": int(time.time()), **entry}
            self.data["history"].insert(0, entry)
            del self.data["history"][100:]
            self._save()

    def history(self):
        with self.lock:
            return list(self.data["history"])
