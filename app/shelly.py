"""Shelly-Steckdose (Gen2/Gen3 per RPC, Gen1 als Fallback): Leistung und Zählerstand."""
import logging
import threading
import time

import requests

log = logging.getLogger("shelly")


class Shelly:
    def __init__(self, host, store, key, channel=0, interval=5):
        self.host = host
        self.store = store
        self.key = key            # z. B. Drucker-ID, für den Tageszähler
        self.channel = channel
        self.interval = interval
        self.s = requests.Session()
        self.watts = None
        self.total_wh = None      # Zählerstand der Steckdose in Wh
        self.on = None
        self.online = False
        self.gen1 = False
        self.last_error = ""

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _read(self):
        if not self.gen1:
            try:
                r = self.s.get(f"http://{self.host}/rpc/Switch.GetStatus", params={"id": self.channel}, timeout=4)
                if r.status_code == 404:
                    self.gen1 = True
                else:
                    r.raise_for_status()
                    d = r.json()
                    return d.get("apower"), (d.get("aenergy") or {}).get("total"), d.get("output")
            except requests.RequestException:
                raise
        r = self.s.get(f"http://{self.host}/status", timeout=4)
        r.raise_for_status()
        d = r.json()
        m = (d.get("meters") or [{}])[self.channel]
        relay = (d.get("relays") or [{}])[self.channel]
        total = m.get("total")
        return m.get("power"), (total / 60 if total is not None else None), relay.get("ison")  # Gen1: Wattminuten

    def _loop(self):
        while True:
            try:
                w, total, on = self._read()
                self.watts = round(w, 1) if w is not None else None
                self.total_wh = total
                self.on = on
                self.online = True
                self.last_error = ""
                self._day_baseline()
            except Exception as e:
                if self.online:
                    log.warning("Shelly %s nicht erreichbar: %s", self.host, e)
                self.online = False
                self.last_error = f"Shelly nicht erreichbar: {e.__class__.__name__}"
            time.sleep(self.interval)

    def set(self, on):
        """Direkt schalten (nur als Rückfall, wenn Bambuddy nicht erreichbar ist)."""
        if not self.gen1:
            r = self.s.get(f"http://{self.host}/rpc/Switch.Set", params={"id": self.channel, "on": "true" if on else "false"}, timeout=5)
            if r.status_code != 404:
                r.raise_for_status()
                self.on = bool(on)
                return
        r = self.s.get(f"http://{self.host}/relay/{self.channel}", params={"turn": "on" if on else "off"}, timeout=5)
        r.raise_for_status()
        self.on = bool(on)

    def _day_baseline(self):
        if self.total_wh is None:
            return
        today = time.strftime("%Y-%m-%d")
        with self.store.lock:
            days = self.store.data.setdefault("energy_day", {})
            d = days.get(self.key)
            if not d or d.get("date") != today or self.total_wh < d.get("start_wh", 0):
                days[self.key] = {"date": today, "start_wh": self.total_wh}
                self.store._save()

    def today_wh(self):
        d = self.store.data.get("energy_day", {}).get(self.key)
        if not d or self.total_wh is None or d.get("date") != time.strftime("%Y-%m-%d"):
            return None
        return max(0.0, self.total_wh - d["start_wh"])
