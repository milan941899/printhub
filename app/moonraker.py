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

    def shutdown_host(self, timeout=60):
        """Vor dem Stromabschalten: Linux im Drucker sauber herunterfahren und warten, bis Moonraker weg ist.
        Gibt eine kurze Beschreibung zurück. Ist der Drucker schon nicht erreichbar, passiert nichts."""
        try:
            self.s.get(self.base + "/server/info", headers=self.headers, timeout=3).raise_for_status()
        except Exception:
            return "war schon aus"
        try:
            self.s.post(self.base + "/machine/shutdown", headers=self.headers, timeout=5)
        except Exception:
            pass            # Verbindung reißt beim Herunterfahren oft schon ab – zählt nicht als Fehler
        t0 = time.time()
        time.sleep(3)
        while time.time() - t0 < timeout:
            try:
                self.s.get(self.base + "/server/info", headers=self.headers, timeout=2)
            except Exception:
                time.sleep(8)          # Netz ist weg – kurz warten, bis das System ganz unten ist
                return f"heruntergefahren ({int(time.time() - t0)} s)"
            time.sleep(2)
        return f"nach {timeout} s noch erreichbar – trotzdem ausgeschaltet"

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

    def _slicer_estimate(self, fn):
        """Geschätzte Druckzeit des Slicers (s) aus den Datei-Metadaten – einmal je Datei, im Hintergrund geholt."""
        if not fn:
            return None
        c = getattr(self, "_est", None)
        if c and c[0] == fn:
            return c[1]
        self._est = (fn, None)

        def fetch():
            try:
                r = self.s.get(self.base + "/server/files/metadata", params={"filename": fn}, headers=self.headers, timeout=10)
                est = (r.json().get("result") or {}).get("estimated_time")
                if est:
                    self._est = (fn, float(est))
            except Exception as e:
                log.debug("[%s] Slicer-Schätzung: %s", self.id, e)
        threading.Thread(target=fetch, daemon=True).start()
        return None

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
        if state == "printing":
            # höchste Z-Höhe des Drucks merken: so weit darf das Bett bei der Plattenprüfung nie hochfahren
            z = ((st.get("gcode_move") or {}).get("gcode_position") or [0, 0, 0])[2]
            mz = self.store.data.setdefault("max_z", {})
            prog = (st.get("virtual_sdcard") or {}).get("progress") or 0
            if z and z < 400 and 0.01 < prog < 0.995 and z > (mz.get(self.id) or 0) + 0.5:   # ohne Start-/End-G-Code
                mz[self.id] = round(z, 1)
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
            if job is None and prev not in ACTIVE and prev is not None and self.start_gate \
                    and not self.start_gate(self, ps.get("filename") or ""):
                # Start ohne Freigabe (z. B. aus der Snapmaker-App, die an SDCARD_PRINT_FILE vorbeigeht) → sofort abbrechen
                fn = ps.get("filename") or ""
                threading.Thread(target=self._gate_cancel, args=(fn,), daemon=True).start()
            elif job is None and prev not in ACTIVE:
                hm = (st.get("print_task_config") or {}).get("extruder_map_table")
                extra = {"filename": ps.get("filename", "")}
                if isinstance(hm, list) and hm:
                    extra["head_map"] = hm[:32]
                self.store.data.setdefault("max_z", {})[self.id] = 0     # neue Höhe messen
                self.fan_override = {}                                     # neuer Druck: Automatik gilt wieder
                self.fan_auto_off = False
                self.job_started(ps.get("filename") or "Druck", extra)
            elif job is not None:
                pd = ps.get("print_duration") or 0
                rem = pd / progress - pd if progress and progress > 0.02 and pd > 60 else None
                self.update_progress(progress, rem)
                self.track_layer((ps.get("info") or {}).get("current_layer"))
                self._grab_end_shot(progress)
        elif job is not None and state in ("complete", "cancelled", "error", "standby"):
            self.job_ended(state == "complete", progress)

    def _grab_end_shot(self, progress):
        """Kamera sitzt beim U1 am Druckkopf: ab 90 % regelmäßig ein Bild merken, solange der Kopf noch überm Teil ist."""
        # ab 99,5 % nichts mehr nachfotografieren: dann läuft meist schon der End-G-Code (Kopf parkt, Bett fährt runter)
        if not self.camera or not progress or progress < 0.9 or progress >= 0.995 or getattr(self, "_shot_busy", False):
            return
        last = getattr(self, "_last_shot", None)
        if last and time.time() - last[0] < (10 if progress >= 0.98 else 15 if progress >= 0.97 else 45):
            return
        self._shot_busy = True

        def run():
            try:
                jpg = self.camera.snapshot()
                if jpg and self.status_data.get("print_stats", {}).get("state") == "printing":
                    self._last_shot = (time.time(), jpg)
            except Exception as e:
                log.debug("[%s] Bild kurz vor Schluss: %s", self.id, e)
            finally:
                self._shot_busy = False
        threading.Thread(target=run, daemon=True).start()

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
        # Restzeit: Hochrechnen aus dem Datei-Fortschritt ist am Anfang viel zu hoch (Aufheizen, Kalibrieren, Spülen
        # kosten Zeit, aber kaum Bytes – z. B. 23 h statt 3 h). Daher anfangs die Schätzung des Slicers, dann
        # allmählich (10 → 60 %) auf die Hochrechnung überblenden, die gegen Ende genauer ist.
        if active and progress is not None:
            est = self._slicer_estimate(ps.get("filename") or "")
            pd = ps.get("print_duration") or 0
            file_rem = pd * (1 - progress) / progress if progress > 0.01 and pd else None
            sl_rem = est * (1 - progress) if est else None
            if sl_rem is not None and file_rem is not None:
                w = min(1.0, max(0.0, (progress - 0.10) / 0.50))
                remaining = round((sl_rem * (1 - w) + file_rem * w) / 60)
            elif sl_rem is not None:
                remaining = round(sl_rem / 60)
            elif file_rem is not None:
                remaining = round(file_rem / 60)
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
            top = max(t for t, _ in tgt)
            # mehrere Köpfe auf Drucktemperatur (z. B. Farbwechsel vorgeheizt): dann gilt toolhead.extruder
            if round((st.get(th) or {}).get("target", 0)) != top if th else True:
                i = next(i for t, i in tgt if t == top)
                th = "extruder" + (str(i) if i else "")
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

    CHAMBER_FANS = {"Abluft": "exhaust_fan", "Umluft": "circulation_fan", "Bauraum-Lüfter": "cavity_fan"}

    def set_chamber_fan(self, label, pct, auto=False):
        """Bauraum-Lüfter von Hand stellen (SET_FAN_SPEED). Die Firmware stellt beim nächsten Druckstart wieder selbst ein."""
        fan = self.CHAMBER_FANS.get(label)
        if not fan:
            raise ValueError("Unbekannter Lüfter")
        pct = max(0, min(100, int(pct)))
        self.gcode(f"SET_FAN_SPEED FAN={fan} SPEED={pct / 100:.2f}", timeout=10)
        self.fan_override = {**getattr(self, "fan_override", {}), label: pct}
        if label == "Abluft" and not auto:
            self.fan_auto_off = True          # von Hand gestellt → Automatik bis zum nächsten Druck aus

    def heated_slots(self):
        """Köpfe, die gerade auf Drucktemperatur sind oder dahin heizen (Standby ~70 °C zählt nicht)."""
        with self.lock:
            st = dict(self.status_data)
        out = []
        for i, ex in enumerate(self._extruders()):
            if round((st.get(ex) or {}).get("target", 0)) >= 150:
                out.append(f"T{i}")
        return out

    start_gate = None        # vom Druckwächter gesetzt: (printer, filename) -> darf starten?
    on_gate_block = None     # (printer, filename) – abgefangener Start

    def _gate_cancel(self, fn):
        log.warning("[%s] Druckstart ohne Freigabe (%s) – breche ab", self.id, fn)
        try:
            self._api("POST", "/printer/gcode/script", params={"script": 'RESPOND TYPE=error MSG="PrintHub: Druckstart ohne '
                                                                     'Freigabe abgebrochen – kommt in die Warteschlange"'}, timeout=5)
        except Exception:
            pass
        try:
            self._api("POST", "/printer/print/cancel", timeout=15)
        except Exception as e:
            log.error("[%s] Abbrechen fehlgeschlagen: %s", self.id, e)
        if self.on_gate_block:
            try:
                self.on_gate_block(self, fn)
            except Exception as e:
                log.error("[%s] Abgefangenen Start nicht eingereiht: %s", self.id, e)

    # --- Warteschlange (Moonraker [job_queue]) und Startsperre ----------------------------------------
    def _api(self, method, path, timeout=10, **kw):
        r = self.s.request(method, self.base + path, headers=self.headers, timeout=timeout, **kw)
        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except Exception:
                msg = r.text
            raise RuntimeError(f"Moonraker {r.status_code}: {str(msg)[:200]}")
        return (r.json() or {}).get("result")

    def queue_status(self):
        """{"jobs": [{"job_id", "filename", "time_added"}], "state": "paused"|"ready"|…} – None = keine Warteschlange."""
        try:
            res = self._api("GET", "/server/job_queue/status", timeout=5)
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise
        return {"jobs": res.get("queued_jobs") or [], "state": res.get("queue_state")}

    def queue_add(self, filenames):
        return self._api("POST", "/server/job_queue/job", json={"filenames": list(filenames), "reset": False})

    def queue_remove(self, job_ids):
        return self._api("DELETE", "/server/job_queue/job", params={"job_ids": ",".join(job_ids)})

    def queue_pause(self):
        return self._api("POST", "/server/job_queue/pause")

    def lock_info(self):
        """Startsperre installiert? (Makro PRINTHUB_SPERRE) → {"lock", "version", "off"}."""
        objs = self._api("GET", "/printer/objects/list", timeout=5).get("objects") or []
        if "gcode_macro PRINTHUB_SPERRE" not in objs or "gcode_macro PRINTHUB_FREIGABE" not in objs:
            return {"lock": False}
        st = self._api("GET", "/printer/objects/query?gcode_macro%20PRINTHUB_SPERRE", timeout=5)
        m = (st.get("status") or {}).get("gcode_macro PRINTHUB_SPERRE") or {}
        return {"lock": True, "version": m.get("version"), "off": bool(m.get("aus"))}

    def gcode_messages(self, count=100):
        """Letzte Konsolenzeilen [{"message", "time", "type"}]."""
        return self._api("GET", "/server/gcode_store", params={"count": count}, timeout=5).get("gcode_store") or []

    def file_meta(self, fn):
        try:
            return self._api("GET", "/server/files/metadata", params={"filename": fn}, timeout=10) or {}
        except RuntimeError:
            return {}

    def file_thumb(self, fn):
        """Größtes Vorschaubild einer G-Code-Datei (bytes) oder None."""
        meta = self.file_meta(fn)
        th = sorted(meta.get("thumbnails") or [], key=lambda t: t.get("width") or 0)
        if not th:
            return None
        folder = fn.rsplit("/", 1)[0] + "/" if "/" in fn else ""
        r = self.s.get(self.base + "/server/files/gcodes/" + quote(folder + th[-1]["relative_path"]), headers=self.headers, timeout=10)
        return r.content if r.ok else None

    def gcode(self, script, timeout=30):
        return self._api("POST", "/printer/gcode/script", params={"script": script}, timeout=timeout)

    def release_start(self, fn):
        """Startsperre für 60 s freigeben und die Datei starten."""
        self.gcode("PRINTHUB_FREIGABE", timeout=10)
        self._api("POST", "/printer/print/start", params={"filename": fn}, timeout=15)

    def plate_pose(self, x, y, z, min_z=None):
        """Prüfposition fürs Plattenfoto anfahren: X/Y referenzieren falls nötig, Bett auf Höhe z (nie unter min_z),
        Kopf nach x/y. Gibt die tatsächlich angefahrene Höhe zurück (None = Z nicht referenziert, Bett bleibt stehen)."""
        with self.lock:
            homed = ((self.status_data.get("toolhead") or {}).get("homed_axes") or "").lower()
        if "z" not in homed and z is not None:
            # Ganz referenzieren: der U1 misst Z vorne links in der Ecke (X≈5 Y≈5), nicht in der Plattenmitte
            self.gcode("G28", timeout=180)
            homed = "xyz"
        elif "x" not in homed or "y" not in homed:
            self.gcode("G28 X Y", timeout=120)
        cmds = ["G90"]
        zz = None
        if "z" in homed and z is not None:
            zz = max(float(z), float(min_z or 0))
            cmds.append(f"G1 Z{zz:.1f} F600")
        cmds += [f"G1 X{float(x):.1f} Y{float(y):.1f} F6000", "M400"]
        self.gcode("\n".join(cmds), timeout=120)
        return zz

    def control(self, action):
        path = {"pause": "/printer/print/pause", "resume": "/printer/print/resume", "stop": "/printer/print/cancel"}.get(action)
        if not path:
            raise ValueError("Unbekannte Aktion")
        r = self.s.post(self.base + path, headers=self.headers, timeout=10)
        r.raise_for_status()
