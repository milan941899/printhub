"""Klipper/Moonraker-Drucker (Snapmaker U1 mit 4 Werkzeugköpfen)."""
import logging
import re
import threading
import time
from collections import deque
from urllib.parse import quote

import requests

from .camera import SnapshotCamera, UrlCamera
from .printer_base import BasePrinter

log = logging.getLogger("moonraker")

STATE_DE = {
    "standby": "Bereit", "printing": "Druckt", "paused": "Pausiert",
    "complete": "Fertig", "cancelled": "Abgebrochen", "error": "Fehler",
}
ACTIVE = {"printing", "paused"}
RE_GRAMS = re.compile(r";\s*filament used \[g\]\s*=\s*([0-9.,\s]+)")


def parse_gcode_grams(text: str):
    """Liest '; filament used [g] = 1.2, 0.0, 3.4' (OrcaSlicer/Snapmaker Orca/PrusaSlicer)."""
    m = None
    for m in RE_GRAMS.finditer(text):
        pass
    if not m:
        return None
    vals = []
    for v in m.group(1).split(","):
        v = v.strip()
        if v:
            try:
                vals.append(float(v))
            except ValueError:
                vals.append(0.0)
    return vals


class MoonrakerPrinter(BasePrinter):
    type = "moonraker"

    def __init__(self, cfg, store, spoolman):
        super().__init__(cfg, store, spoolman)
        port = cfg.get("port", 7125)
        self.base = f"http://{cfg['host']}:{port}"
        self.channels = int(cfg.get("channels", 4))
        self.headers = {"X-Api-Key": cfg["api_key"]} if cfg.get("api_key") else {}
        self.s = requests.Session()
        self.status_data = {}
        self.last_state = None
        self._head_t = None
        self._head_cur = None
        self._head_saved = 0
        self.light_pin = cfg.get("light_pin")   # z. B. "caselight" (output_pin) oder "led cavity_led"
        web = f"http://{cfg['host']}:{cfg.get('web_port', 80)}"
        if cfg.get("camera_url"):
            self.camera = UrlCamera(cfg["camera_url"])
        elif cfg.get("camera", True):
            self.camera = SnapshotCamera(web, self.base, self.headers)
        else:
            self.camera = None

    def _extruders(self):
        return ["extruder"] + [f"extruder{i}" for i in range(1, self.channels)]

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        objs = ["print_stats", "virtual_sdcard", "heater_bed", "toolhead", "fan", "gcode_move", "motion_report",
                "display_status"] + self._extruders()
        if self.light_pin:
            objs.append(self._light_obj())
        # Snapmaker U1 & Co.: Filament je Kopf (RFID), Lüfter je Kopf, Bauraum – nur abfragen, was es gibt
        self._extra = self._find_extras()
        objs += self._extra
        url = self.base + "/printer/objects/query?" + "&".join(quote(o, safe="_") for o in objs)
        while True:
            try:
                r = self.s.get(url, headers=self.headers, timeout=5)
                r.raise_for_status()
                self.handle_status(r.json()["result"]["status"])
                self.online = True
                self.last_error = ""
                time.sleep(2)
            except Exception as e:
                if self.online:
                    log.warning("[%s] nicht erreichbar: %s", self.id, e)
                self.online = False
                self.last_error = f"Nicht erreichbar: {e.__class__.__name__}"
                time.sleep(10)

    def _find_extras(self):
        try:
            r = self.s.get(self.base + "/printer/objects/list", headers=self.headers, timeout=5)
            have = set(r.json()["result"]["objects"])
        except Exception:
            return []
        want = ["print_task_config", "temperature_sensor cavity", "fan_generic exhaust_fan", "fan_generic circulation_fan",
                "fan_generic cavity_fan", "purifier"]
        for i in range(self.channels):
            want += [f"heater_fan e{i}_nozzle_fan", f"fan_generic e{i}_fan"]
        return [o for o in want if o in have]

    def _part_fan(self, st, n):
        """Bauteillüfter von Kopf n: Kopf 0 = „fan“, sonst fan_generic eN_fan (Snapmaker U1)."""
        o = st.get("fan") if n == 0 else st.get(f"fan_generic e{n}_fan")
        if o is None and n != 0:
            o = st.get("fan")
        return round((o or {}).get("speed", 0) * 100) if o else None

    def _hot_fan(self, st, n):
        o = st.get(f"heater_fan e{n}_nozzle_fan")
        return round(o.get("speed", 0) * 100) if o else None

    def _track_heads(self, st, state):
        """Druckzeit und Werkzeugwechsel pro Kopf mitzählen (für Statistik/Wartung)."""
        now = time.time()
        th = (st.get("toolhead") or {}).get("extruder")
        cur = f"T{(th or 'extruder').replace('extruder', '') or '0'}" if th else None
        heads = self.store.data.setdefault("heads", {}).setdefault(self.id, {})
        if state == "printing" and cur:
            h = heads.setdefault(cur, {"sec": 0.0, "changes": 0})
            if self._head_t and self._head_cur:
                heads.setdefault(self._head_cur, {"sec": 0.0, "changes": 0})["sec"] += min(30, now - self._head_t)
            if self._head_cur and cur != self._head_cur:
                h["changes"] += 1
            self._head_cur, self._head_t = cur, now
        else:
            self._head_t = None
        if now - self._head_saved > 60:
            self._head_saved = now
            with self.store.lock:
                self.store._save()

    def handle_status(self, st):
        with self.lock:
            self.status_data = st
        ps = st.get("print_stats", {})
        state = ps.get("state")
        try:
            self._track_heads(st, state)
        except Exception as e:
            log.debug("Kopfzeiten: %s", e)
        prev = self.last_state
        self.last_state = state
        progress = st.get("virtual_sdcard", {}).get("progress")
        if state == "paused" and prev is not None and prev != "paused":
            self.notify("pause", "Druck pausiert ⏸", ps.get("filename", ""), "pause_button", "high")
        if state == "error" and prev != "error":
            self.notify("error", "Druckerfehler ⚠", ps.get("message") or "Klipper meldet einen Fehler", "rotating_light", "urgent")
            self.note_job_error((ps.get("message") or "Klipper-Fehler")[:120])
        job = self.store.get_job(self.id)
        if state in ACTIVE:
            if job is None and prev not in ACTIVE:
                hm = (st.get("print_task_config") or {}).get("extruder_map_table")
                extra = {"filename": ps.get("filename", "")}
                if isinstance(hm, list) and hm:
                    extra["head_map"] = hm[:32]
                self.job_started(ps.get("filename") or "Druck", extra)
            elif job is not None:
                pd = ps.get("print_duration") or 0
                rem = pd / progress - pd if progress and progress > 0.02 and pd > 60 else None
                self.update_progress(progress, rem)
                self.track_layer((ps.get("info") or {}).get("current_layer"))
        elif job is not None and state in ("complete", "cancelled", "error", "standby"):
            self.job_ended(state == "complete", progress)

    # --- Filamentbedarf aus der G-Code-Datei ------------------------------------
    def load_filament_usage(self, job):
        fn = job.get("filename") or job.get("name")
        grams = None
        # 1) Moonraker-Metadaten (falls die Version Einzelgewichte liefert)
        try:
            r = self.s.get(self.base + "/server/files/metadata", params={"filename": fn}, headers=self.headers, timeout=10)
            if r.ok:
                md = r.json().get("result", {})
                thumbs = sorted(md.get("thumbnails") or [], key=lambda t: t.get("width", 0) * t.get("height", 0))
                if thumbs and thumbs[-1].get("relative_path"):
                    folder = fn.rsplit("/", 1)[0] + "/" if "/" in fn else ""
                    tr = self.s.get(self.base + "/server/files/gcodes/" + quote(folder + thumbs[-1]["relative_path"]),
                                    headers=self.headers, timeout=10)
                    if tr.ok:
                        job["thumb"] = self.save_media("modell", tr.content, "png")
                fw = md.get("filament_weights")
                if isinstance(fw, list) and fw:
                    grams = [float(x or 0) for x in fw]
        except Exception:
            pass
        # 2) Ende der G-Code-Datei lesen (dort steht die Slicer-Zusammenfassung)
        if grams is None:
            grams = self._grams_from_gcode(fn)
        if grams is None:
            raise ValueError("Keine Angabe 'filament used [g]' im G-Code gefunden")
        # Snapmaker U1: der Drucker ordnet Slicer-Filament → Kopf frei zu (print_task_config.extruder_map_table,
        # z. B. [1, 3, 2, 0] = Filament 1 auf T1, Filament 2 auf T3 …). Ohne Tabelle: Filament 1 → T0 usw.
        m = self._map_table(job)
        out = {}
        for i, g in enumerate(grams):
            if g <= 0:
                continue
            h = m[i] if m and i < len(m) and isinstance(m[i], int) and 0 <= m[i] < self.channels else i
            if h < self.channels:
                out[f"T{h}"] = out.get(f"T{h}", 0) + g
        return list(out.items())

    def _map_table(self, job):
        """Zuordnung Filament → Kopf; beim Druckstart gemerkt, damit sie zum Druck passt."""
        if job.get("head_map"):
            return job["head_map"]
        with self.lock:
            m = (self.status_data.get("print_task_config") or {}).get("extruder_map_table")
        if isinstance(m, list) and m:
            job["head_map"] = m[:self.channels * 8]
            return job["head_map"]
        return None

    def _grams_from_gcode(self, fn):
        url = self.base + "/server/files/gcodes/" + quote(fn)
        tail_size = 400_000
        r = self.s.get(url, headers={**self.headers, "Range": f"bytes=-{tail_size}"}, stream=True, timeout=30)
        r.raise_for_status()
        tail = deque(maxlen=tail_size // 8192 + 2)
        for chunk in r.iter_content(8192):
            tail.append(chunk)
        text = b"".join(tail).decode("utf-8", "ignore")
        grams = parse_gcode_grams(text)
        if grams is None and r.status_code == 206:
            # Zusammenfassung steht evtl. im Kopf (PrusaSlicer-Stil)
            r2 = self.s.get(url, headers={**self.headers, "Range": "bytes=0-200000"}, timeout=30)
            grams = parse_gcode_grams(r2.content.decode("utf-8", "ignore"))
        return grams

    # --- Slots / Status --------------------------------------------------------
    def slots(self):
        """Ein Slot je Kopf; Material/Farbe/Hersteller meldet der U1 selbst (RFID oder am Display eingestellt)."""
        with self.lock:
            tc = self.status_data.get("print_task_config") or {}
        def at(k, i, d=None):
            v = tc.get(k)
            return v[i] if isinstance(v, list) and i < len(v) else d
        out = []
        for i in range(self.channels):
            exist = at("filament_exist", i, True)
            rgba = at("filament_color_rgba", i) or ""
            mat = " ".join(x for x in (at("filament_type", i) or "", at("filament_sub_type", i) or "") if x and x != "NONE")
            out.append({"key": f"T{i}", "label": f"Kopf {i + 1} (T{i})",
                        "material": mat if exist else "", "color": ("#" + rgba[:6]) if exist and len(rgba) >= 6 else "",
                        "vendor": (at("filament_vendor", i) or "") if exist else "",
                        "rfid": bool(at("filament_official", i)) if exist else False,
                        "printer_spool": at("filament_spool_id", i) or 0})
        return out

    def set_slot_filament(self, key, spool):
        """Spoolman-Spule am Kopf hinterlegen (Makro SET_SPOOL_ID; 0 = keine). Material/Farbe kommen vom RFID-Chip."""
        n = int(key[1:])
        sid = int((spool or {}).get("id") or 0)
        r = self.s.post(self.base + "/printer/gcode/script", params={"script": f"SET_SPOOL_ID CHANNEL={n} SPOOL_ID={sid}"},
                        headers=self.headers, timeout=10)
        r.raise_for_status()
        log.info("[%s] Kopf %s: Spoolman-Spule %s am Drucker hinterlegt", self.id, key, sid or "–")
        return True

    def status(self):
        with self.lock:
            st = self.status_data
        ps = st.get("print_stats", {})
        state = ps.get("state", "")
        active = state in ACTIVE
        progress = st.get("virtual_sdcard", {}).get("progress")
        remaining = None
        if active and progress and progress > 0.01 and ps.get("print_duration"):
            remaining = round(ps["print_duration"] * (1 - progress) / progress / 60)
        temps = []
        for i, ex in enumerate(self._extruders()):
            e = st.get(ex)
            if e:
                temps.append({"label": f"T{i}", "actual": round(e.get("temperature", 0)), "target": round(e.get("target", 0))})
        bed = st.get("heater_bed")
        if bed:
            temps.append({"label": "Bett", "actual": round(bed.get("temperature", 0)), "target": round(bed.get("target", 0))})
        cur = None
        th = st.get("toolhead", {}).get("extruder")
        # Snapmaker U1: wartende Köpfe stehen auf Bereitschafts-Temperatur (z. B. 70 °C) – der druckende Kopf ist
        # der mit dem höchsten Soll; „toolhead.extruder“ hinkt beim Kopfwechsel manchmal hinterher
        tgt = [(round((st.get(ex) or {}).get("target", 0)), i) for i, ex in enumerate(self._extruders()) if st.get(ex)]
        if active and tgt and max(tgt)[0] > 0:
            th = "extruder" + (str(max(tgt)[1]) if max(tgt)[1] else "")
        if active and th:
            n = th.replace("extruder", "") or "0"
            cur = f"T{n}"
        info = ps.get("info") or {}
        gm = st.get("gcode_move") or {}
        n_act = int((th or "extruder").replace("extruder", "") or 0)
        part, hot = self._part_fan(st, n_act), self._hot_fan(st, n_act)
        fans = []
        if part is not None:
            fans.append({"label": "Bauteil", "pct": part})
        if hot is not None:
            fans.append({"label": "Hotend", "pct": hot})
        cav = st.get("temperature_sensor cavity")
        if cav and cav.get("temperature") is not None:
            temps.append({"label": "Bauraum", "actual": round(cav["temperature"]), "target": 0})
        chamber = {k: round((st.get(o) or {}).get("speed", 0) * 100) for k, o in
                   (("Abluft", "fan_generic exhaust_fan"), ("Umluft", "fan_generic circulation_fan"), ("Bauraum-Lüfter", "fan_generic cavity_fan"))
                   if st.get(o) is not None}
        mr = st.get("motion_report") or {}
        live_v = round(mr["live_velocity"]) if mr.get("live_velocity") is not None else None
        ev = mr.get("live_extruder_velocity")
        flow = round(ev * 2.405, 1) if ev is not None else None  # mm/s Filament (1,75 mm) -> mm³/s
        return {
            "id": self.id, "name": self.name, "type": self.type,
            "online": self.online,
            "state": STATE_DE.get(state, state or "Unbekannt"),
            "printing": active,
            "paused": state == "paused",
            "job": ps.get("filename", "") if active else "",
            "progress": round(progress * 100) if active and progress is not None else None,
            "remaining_min": remaining,
            "temps": temps,
            "current_slot": cur,
            "error": self.last_error,
            "layer": info.get("current_layer") if active else None,
            # ohne Layer-Angabe vom Slicer: wenigstens die Höhe (Z) zeigen
            "z": round(gm["gcode_position"][2], 2) if active and gm.get("gcode_position") else None,
            "total_layers": info.get("total_layer") if active else None,
            "speed": f"{live_v} mm/s" if active and live_v is not None else None,
            "speed_pct": round(gm.get("speed_factor", 1) * 100) if gm else None,
            "flow": flow if active else None,
            "fans": fans,
            "chamber_fans": chamber,
            "light": self.light_state(),
            "stage": self._stage(active, state, progress, temps, st) if active else None,
            "stage_steps": [],
        }

    @staticmethod
    def _stage(active, state, progress, temps, st):
        """Klipper kennt keine Arbeitsschritte – „heizt“ aus den Temperaturen, sonst die Display-Meldung."""
        if state == "paused":
            return {"code": -2, "icon": "⏸", "text": "Pausiert", "short": "Pausiert", "pause": True}
        if (progress or 0) < 0.01:
            for t in temps:
                if t["target"] and t["actual"] < t["target"] - 5:
                    was = "Bett heizt" if t["label"] == "Bett" else "Düse heizt"
                    return {"code": -1, "icon": "🔥", "text": was, "short": "Heizt", "pause": False}
        msg = ((st.get("display_status") or {}).get("message") or "").strip()
        if msg:
            return {"code": -3, "icon": "ℹ️", "text": msg[:60], "short": "", "pause": False}
        return None

    def _light_obj(self):
        lp = self.light_pin.strip()
        return lp if " " in lp else f"output_pin {lp}"

    def light_state(self):
        if not self.light_pin:
            return None
        with self.lock:
            o = self.status_data.get(self._light_obj()) or {}
        v = o.get("value")
        if v is None and o.get("color_data"):
            v = max(max(c[:3]) for c in o["color_data"])
        return None if v is None else v > 0

    def set_light(self, on):
        if not self.light_pin:
            raise ValueError("Für diesen Drucker ist kein Licht (light_pin) eingestellt")
        lp = self._light_obj()
        if lp.startswith("output_pin "):
            g = f"SET_PIN PIN={lp.split(' ', 1)[1]} VALUE={1 if on else 0}"
        else:
            # alle Kanäle setzen (RGB + Weiß); Leuchten ohne Weiß-Kanal lehnen WHITE ab → dann nur RGB
            v = 1 if on else 0
            led = lp.split(" ", 1)[1]
            g = f"SET_LED LED={led} RED={v} GREEN={v} BLUE={v} WHITE={v} SYNC=0"   # SYNC=0: sofort, nicht erst nach den Druckbewegungen
        r = self.s.post(self.base + "/printer/gcode/script", params={"script": g}, headers=self.headers, timeout=10)
        if not r.ok and not lp.startswith("output_pin "):
            log.info("[%s] Licht mit WHITE abgelehnt (%s) – versuche nur RGB", self.id, r.text[:200])
            g = f"SET_LED LED={led} RED={v} GREEN={v} BLUE={v} SYNC=0"
            r = self.s.post(self.base + "/printer/gcode/script", params={"script": g}, headers=self.headers, timeout=10)
        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except Exception:
                msg = r.text
            raise ValueError(f"Klipper lehnt das Licht ab: {str(msg)[:160]}")

    def control(self, action):
        path = {"pause": "/printer/print/pause", "resume": "/printer/print/resume", "stop": "/printer/print/cancel"}.get(action)
        if not path:
            raise ValueError("Unbekannte Aktion")
        r = self.s.post(self.base + path, headers=self.headers, timeout=10)
        r.raise_for_status()
