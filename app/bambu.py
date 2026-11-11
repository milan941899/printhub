"""Bambu Lab (A1 & Co.) im LAN-Modus: MQTT (Port 8883) für Status/Steuerung, FTPS (Port 990) für die 3MF."""
import ftplib
import io
import json
import logging
import ssl
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

from .camera import BambuCamera, UrlCamera
from .printer_base import BasePrinter

log = logging.getLogger("bambu")

ACTIVE = {"RUNNING", "PREPARE", "PAUSE", "SLICING"}

# Arbeitsschritt des Druckers (MQTT „stg_cur“) → (Symbol, Text, Kurzwort fürs Schild)
STAGE_DE = {
    1: ("📐", "Bett wird vermessen", "Kalibriert"), 2: ("🔥", "Bett heizt vor", "Heizt"),
    3: ("〰️", "Vibrationsausgleich", "Kalibriert"), 4: ("🔄", "Filament wird gewechselt", "Wechselt"),
    5: ("⏸", "Pause (M400)", "Pausiert"), 6: ("⏸", "Pausiert: Filament leer", "Filament leer"),
    7: ("🔥", "Düse heizt", "Heizt"), 8: ("📏", "Extrusion wird kalibriert", "Kalibriert"),
    9: ("🔍", "Bettoberfläche wird gescannt", "Kalibriert"), 10: ("🔍", "Erste Schicht wird geprüft", "Prüft"),
    11: ("🔍", "Druckplatte wird erkannt", "Prüft"), 12: ("📐", "Lidar wird kalibriert", "Kalibriert"),
    13: ("🏠", "Druckkopf fährt auf Startposition", "Referenzfahrt"), 14: ("🧽", "Düse wird gereinigt", "Reinigt"),
    15: ("🌡", "Düsentemperatur wird geprüft", "Prüft"), 16: ("⏸", "Pausiert vom Benutzer", "Pausiert"),
    17: ("⏸", "Pausiert: Frontabdeckung abgefallen", "Pausiert"), 18: ("📐", "Lidar wird kalibriert", "Kalibriert"),
    19: ("📏", "Fluss wird kalibriert", "Kalibriert"), 20: ("⏸", "Pausiert: Düsentemperatur gestört", "Störung"),
    21: ("⏸", "Pausiert: Betttemperatur gestört", "Störung"), 22: ("🔄", "Filament wird entladen", "Entlädt"),
    23: ("⏸", "Pausiert: Schrittverlust", "Störung"), 24: ("🔄", "Filament wird geladen", "Lädt"),
    25: ("🔇", "Motorgeräusch-Kalibrierung", "Kalibriert"), 26: ("⏸", "Pausiert: AMS nicht erreichbar", "Störung"),
    27: ("⏸", "Pausiert: Hotend-Lüfter zu langsam", "Störung"), 28: ("⏸", "Pausiert: Bauraum-Temperatur", "Störung"),
    29: ("❄️", "Bauraum kühlt", "Kühlt"), 30: ("⏸", "Pausiert durch G-Code", "Pausiert"),
    31: ("🔇", "Motorgeräusch-Test", "Kalibriert"), 32: ("⏸", "Pausiert: Filament klebt an der Düse", "Störung"),
    33: ("⏸", "Pausiert: Filamentschneider", "Störung"), 34: ("⏸", "Pausiert: Fehler in der ersten Schicht", "Störung"),
    35: ("⏸", "Pausiert: Düse verstopft", "Störung"), 36: ("📏", "Bewegungsgenauigkeit wird gemessen", "Kalibriert"),
    37: ("📏", "Bewegungsgenauigkeit wird verbessert", "Kalibriert"), 38: ("📏", "Bewegungsgenauigkeit wird gemessen", "Kalibriert"),
    39: ("📏", "Düsenversatz wird kalibriert", "Kalibriert"), 40: ("📐", "Bett wird vermessen (heiß)", "Kalibriert"),
    47: ("📐", "Bett wird vermessen (1/2)", "Kalibriert"), 48: ("📐", "Bett wird vermessen (2/2)", "Kalibriert"),
    49: ("🔥", "Bauraum heizt", "Heizt"), 50: ("❄️", "Bett kühlt ab", "Kühlt"),
    51: ("✏️", "Kalibrierlinien werden gedruckt", "Kalibriert"), 54: ("🔥", "Wartet auf Betttemperatur", "Heizt"),
    63: ("🔥", "Wartet auf Bauraumtemperatur", "Heizt"), 64: ("🔥", "Hotend wird vorbereitet", "Vorbereitung"),
    74: ("⚙️", "Vorbereitung", "Vorbereitung"), 77: ("⚙️", "AMS wird vorbereitet", "Vorbereitung"),
}
PAUSE_STAGES = {5, 6, 16, 17, 20, 21, 23, 26, 27, 28, 30, 32, 33, 34, 35}


def stage_info(code):
    if code is None:
        return None
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    if code in (-1, 0, 255):
        return None
    ic, txt, kurz = STAGE_DE.get(code, ("⚙️", f"Arbeitsschritt {code}", "Vorbereitung"))
    return {"code": code, "icon": ic, "text": txt, "short": kurz, "pause": code in PAUSE_STAGES}
STATE_DE = {
    "IDLE": "Bereit", "RUNNING": "Druckt", "PREPARE": "Vorbereitung", "PAUSE": "Pausiert",
    "FINISH": "Fertig", "FAILED": "Abgebrochen", "SLICING": "Slicing",
}


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Drucker nutzt selbstsigniertes Zertifikat
    return ctx


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTPS mit implizitem TLS + Session-Reuse (nötig für Bambu-Drucker)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def parse_slice_info(data: bytes, plate=None):
    """Liest Metadata/slice_info.config aus einer .3mf. Gibt [{idx,type,color,grams}] zurück."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("Metadata/slice_info.config")
    root = ET.fromstring(xml)
    plates = root.findall("plate")
    chosen = None
    for p in plates:
        idx = None
        for m in p.findall("metadata"):
            if m.get("key") == "index":
                idx = m.get("value")
        if plate is not None and str(idx) == str(plate):
            chosen = p
            break
    if chosen is None:
        chosen = next((p for p in plates if p.findall("filament")), plates[0] if plates else None)
    out = []
    if chosen is None:
        return out
    for f in chosen.findall("filament"):
        try:
            grams = float(f.get("used_g") or 0)
        except ValueError:
            grams = 0.0
        out.append({
            "idx": int(f.get("id", "0")),
            "type": (f.get("type") or "").upper(),
            "color": (f.get("color") or "").lstrip("#")[:6].upper(),
            "grams": grams,
        })
    return out


SPEED_DE = {1: "Leise", 2: "Standard", 3: "Sport", 4: "Ludicrous"}


def _fan_pct(v):
    """Bambu meldet Lüfter als '0'..'15'."""
    try:
        return round(int(v) * 100 / 15)
    except (TypeError, ValueError):
        return None


def sameish(a, b):
    """Gleiches Grundmaterial? PLA+ = PLA, PETG-HF = PETG."""
    def base(m):
        u = "".join(c if c.isalpha() else " " for c in (m or "").upper()).strip()
        for b_ in ("PETG", "PLA", "ABS", "ASA", "TPU", "PVA", "PC", "PA"):
            if u.startswith(b_):
                return b_
        return u.split(" ")[0] if u else ""
    return base(a) == base(b)


def tray_key(global_id):
    """Globale Tray-ID (ams*4+slot, 254/255 = externe Spule) -> Slot-Key."""
    try:
        g = int(global_id)
    except (TypeError, ValueError):
        return None
    if g in (254, 255):
        return "ext"
    if 0 <= g < 64:
        return f"ams{g // 4}-{g % 4}"
    return None


def _color_dist(a, b):
    try:
        ra, ga, ba = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        rb, gb, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    except (ValueError, IndexError):
        return 999
    return ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5


def map_filaments(filaments, mapping, trays, trays_used):
    """Ordnet die Slicer-Filamente den Slots zu.
    1. 'mapping' aus dem Druckerbericht (wenn vorhanden)
    2. nur ein Filament -> der Slot, aus dem gedruckt wurde
    3. sonst: bester Treffer nach Material + Farbe
    """
    used = [f for f in filaments if f["grams"] > 0]
    result = []
    if mapping and len(mapping) >= max((f["idx"] for f in used), default=0):
        for f in used:
            key = tray_key(mapping[f["idx"] - 1])
            if key:
                result.append((key, f["grams"]))
        if result:
            return result
    if len(used) == 1 and len(trays_used) == 1:
        return [(next(iter(trays_used)), used[0]["grams"])]
    for f in used:
        best, best_score = None, None
        for t in trays:
            score = _color_dist(f["color"], t.get("color", "")) + (0 if t.get("material", "").upper() == f["type"] else 300)
            if t["key"] in trays_used:
                score -= 50
            if best_score is None or score < best_score:
                best, best_score = t["key"], score
        if best:
            result.append((best, f["grams"]))
    return result


class BambuPrinter(BasePrinter):
    type = "bambu"

    def __init__(self, cfg, store, spoolman):
        super().__init__(cfg, store, spoolman)
        self.host = cfg["host"]
        self.serial = cfg["serial"]
        self.code = str(cfg["access_code"])
        self.state = {}
        self.client = None
        self.last_gs = None
        self.last_msg = 0
        self.last_print_error = 0
        self._last_learn = 0
        self.stage_log = []          # [[code, seit]] – Ablauf des aktuellen Drucks
        self.stage_job = None
        # camera_url gesetzt -> eigene Webcam (z. B. dieselbe wie in Bambuddy), sonst eingebaute A1-Kamera
        if cfg.get("camera_url"):
            self.camera = UrlCamera(cfg["camera_url"])
        elif cfg.get("camera", True):
            self.camera = BambuCamera(self.host, self.code)
        else:
            self.camera = None

    # --- MQTT ----------------------------------------------------------------
    def start(self):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printhub-{self.id}-{int(time.time())}")
        c.username_pw_set("bblp", self.code)
        c.tls_set_context(_ctx())
        c.tls_insecure_set(True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(2, 60)
        self.client = c
        c.connect_async(self.host, 8883, keepalive=60)
        c.loop_start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self):
        # A1 schickt nur Änderungen; alle 5 Minuten Vollstatus anfordern
        while True:
            time.sleep(300)
            if self.online:
                self._pushall()

    def _pushall(self):
        self._publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    def _publish(self, payload):
        if self.client:
            self.client.publish(f"device/{self.serial}/request", json.dumps(payload))

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.online = True
            self.last_error = ""
            client.subscribe(f"device/{self.serial}/report")
            self._pushall()
            log.info("[%s] verbunden", self.id)
        else:
            self.online = False
            self.last_error = f"MQTT-Verbindung abgelehnt: {reason_code} (Access Code prüfen)"

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.online = False

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return
        self.handle_report(data)

    # --- Auswertung ----------------------------------------------------------
    def handle_report(self, data):
        p = data.get("print")
        if not isinstance(p, dict):
            return
        with self.lock:
            deep_merge(self.state, p)
            self.last_msg = time.time()
            self.online = True
            gs = self.state.get("gcode_state")
            prev = self.last_gs
            self.last_gs = gs
            prog = self.state.get("mc_percent")
            tray_now = tray_key(self.state.get("ams", {}).get("tray_now"))
            perr = self.state.get("print_error") or 0
            self._track_stage(gs)
        if gs == "PAUSE" and prev is not None and prev != "PAUSE":
            if str(self.state.get("stg_cur")) == "6":
                self.note_runout(tray_now)
            self.notify("pause", "Druck pausiert ⏸", self._pause_text(), "pause_button", "high")
        if perr and perr != self.last_print_error:
            code = f"{perr:08X}" if isinstance(perr, int) else str(perr)
            self.notify("error", "Druckerfehler ⚠", f"Fehlercode {code[:4]}_{code[4:]} – Details am Drucker/in Bambu Studio", "rotating_light", "urgent")
            self.note_job_error(f"Fehlercode {code[:4]}_{code[4:]}")
        self.last_print_error = perr
        if time.time() - self._last_learn > 20:
            self._last_learn = time.time()
            try:
                self.learn_profiles()
            except Exception as e:
                log.debug("Profil-Lernen fehlgeschlagen: %s", e)
        job = self.store.get_job(self.id)
        if gs in ACTIVE:
            if job is None and (prev is None or prev not in ACTIVE):
                # Neuer Druck (oder App-Neustart mitten im Druck ohne gespeicherten Job)
                self.job_started(self.state.get("subtask_name") or "Druck", {
                    "gcode_file": self.state.get("gcode_file", ""),
                    "subtask": self.state.get("subtask_name", ""),
                    "trays_used": [],
                })
                job = self.store.get_job(self.id)
            if job is not None:
                changed = False
                if tray_now and tray_now not in job.get("trays_used", []):
                    job.setdefault("trays_used", []).append(tray_now)
                    changed = True
                segs = job.setdefault("segments", [])
                info = job.setdefault("slot_info", {})
                if tray_now and tray_now not in info:
                    sl = next((x for x in self.slots() if x["key"] == tray_now), {})
                    info[tray_now] = [(sl.get("material") or "").upper(), (sl.get("color") or "").upper()[:6]]
                    changed = True
                if tray_now and segs and segs[-1][0] != tray_now and info.get(segs[-1][0]) and \
                        info[segs[-1][0]][0] and info.get(segs[-1][0]) == info.get(tray_now):
                    # AMS wechselt mitten im Druck auf gleiches Material + Farbe = Nachfüllen, alte Spule ist leer
                    job.setdefault("runout", [])
                    if segs[-1][0] not in job["runout"]:
                        job["runout"].append(segs[-1][0])
                        log.info("[%s] Spule in %s leer gelaufen (AMS hat nachgefüllt)", self.id, self.slot_label(segs[-1][0]))
                    changed = True
                if tray_now and (not segs or segs[-1][0] != tray_now):
                    segs.append([tray_now, round((prog or 0) / 100, 4)])
                    changed = True
                for k in ("mapping", "ams_mapping"):
                    if isinstance(self.state.get(k), list) and self.state[k] and job.get("mapping") != self.state[k]:
                        job["mapping"] = self.state[k]
                        changed = True
                if changed:
                    self.store.set_job(self.id, job)
                if isinstance(prog, (int, float)):
                    rem = self.state.get("mc_remaining_time")
                    self.update_progress(prog / 100, rem * 60 if isinstance(rem, (int, float)) and rem > 0 else None)
                    self.track_layer(self.state.get("layer_num"))
        elif gs in ("FINISH", "FAILED") and job is not None:
            self.job_ended(gs == "FINISH", (prog or 0) / 100)
        elif gs == "IDLE" and job is not None and prev in ACTIVE:
            self.job_ended(False, (prog or 0) / 100)

    # --- Arbeitsschritte ------------------------------------------------------
    def _track_stage(self, gs):
        """Ablauf mitschreiben (unter self.lock aufgerufen)."""
        if gs not in ACTIVE:
            return
        key = self.state.get("subtask_name") or self.state.get("gcode_file") or ""
        if key != self.stage_job:
            self.stage_job, self.stage_log = key, []
        code = self.state.get("stg_cur")
        try:
            code = int(code)
        except (TypeError, ValueError):
            return
        if code in (-1, 255):
            code = 0
        if not self.stage_log or self.stage_log[-1][0] != code:
            self.stage_log.append([code, time.time()])
            del self.stage_log[:-40]

    def stage_steps(self):
        """Ablauf mit Dauer: [{icon, text, sec, now}] – „Druckt“ (0) nur als letzter Schritt."""
        out, log_ = [], list(self.stage_log)
        for i, (code, t0) in enumerate(log_):
            t1 = log_[i + 1][1] if i + 1 < len(log_) else time.time()
            info = stage_info(code) or {"icon": "🖨", "text": "Druckt"}
            if code == 0 and i + 1 < len(log_) and t1 - t0 < 20:
                continue        # kurzes „Druckt“ zwischen zwei Vorbereitungsschritten
            out.append({"icon": info["icon"], "text": info["text"], "sec": int(t1 - t0), "now": i == len(log_) - 1})
        return out

    def _pause_text(self):
        s = self.state
        name = s.get("subtask_name", "")
        info = stage_info(s.get("stg_cur"))
        reason = info["text"].replace("Pausiert: ", "").replace("Pausiert ", "") if info and info["pause"] else ""
        lines = [name] + ([f"Grund: {reason}"] if reason else [])
        if info and info["code"] == 6:
            # leerer Slot + Slots mit demselben Material zeigen
            now = tray_key(s.get("ams", {}).get("tray_now"))
            slots = {sl["key"]: sl for sl in self.slots()}
            leer = slots.get(now) or {}
            mat = (leer.get("material") or "").upper()
            if leer:
                lines.append(f"Leer: {leer.get('label', now)}" + (f" ({leer.get('material')})" if leer.get("material") else ""))
            gleich = [sl for k, sl in slots.items() if k != now and mat and (sl.get("material") or "").upper() == mat]
            if gleich:
                lines.append("Gleiches Material in: " + ", ".join(sl.get("label", sl["key"]) for sl in gleich))
            lines.append("Neues Filament einlegen und am Drucker oder in Bambu Studio fortsetzen.")
        return "\n".join(x for x in lines if x)

    def _download_3mf(self, job):
        sub = job.get("subtask") or job.get("name") or ""
        gfile = job.get("gcode_file") or ""
        candidates = []
        if gfile.endswith(".3mf"):
            candidates.append(gfile if gfile.startswith("/") else "/" + gfile)
        for base in (f"/cache/{sub}", f"/{sub}"):
            candidates += [base + ".gcode.3mf", base + ".3mf"]
        ftp = ImplicitFTP_TLS(context=_ctx())
        ftp.connect(self.host, 990, timeout=20)
        try:
            ftp.login("bblp", self.code)
            ftp.prot_p()
            last = None
            for path in candidates:
                buf = io.BytesIO()
                try:
                    ftp.retrbinary(f"RETR {path}", buf.write)
                    return buf.getvalue()
                except ftplib.all_errors as e:
                    last = e
            raise FileNotFoundError(f"3MF nicht gefunden ({', '.join(candidates)}): {last}")
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    def load_filament_usage(self, job):
        data = self._download_3mf(job)
        plate = None
        g = job.get("gcode_file") or ""
        if "plate_" in g:
            try:
                plate = int(g.split("plate_")[1].split(".")[0])
            except ValueError:
                pass
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                want = [f"Metadata/plate_{plate or 1}.png", "Metadata/plate_1.png"]
                want += sorted(n for n in names if n.startswith("Metadata/plate_") and n.endswith(".png")
                               and "small" not in n and "no_light" not in n and "top" not in n)
                pick = next((n for n in want if n in names), None)
                if pick:
                    job["thumb"] = self.save_media("modell", z.read(pick), "png")
        except Exception as e:
            log.debug("[%s] Kein Modellbild in der 3MF: %s", self.id, e)
        return parse_slice_info(data, plate)

    def resolve_usage(self, job):
        # Zuordnung erst am Ende, wenn Mapping und benutzte Slots bekannt sind
        return map_filaments(job.get("filaments") or [], job.get("mapping"), self.slots(), set(job.get("trays_used", [])))

    # --- Slots / Status ------------------------------------------------------
    def slots(self):
        out = []
        with self.lock:
            ams = self.state.get("ams", {}).get("ams", []) or []
            for unit in ams:
                uid = int(unit.get("id", 0))
                for t in unit.get("tray", []) or []:
                    sid = int(t.get("id", 0))
                    out.append({
                        "key": f"ams{uid}-{sid}",
                        "label": f"AMS{'' if len(ams) == 1 else ' ' + str(uid + 1)} Slot {sid + 1}",
                        "material": t.get("tray_type", "") or "",
                        "color": ("#" + t["tray_color"][:6]) if t.get("tray_color") else "",
                        "profile": t.get("tray_info_idx", "") or "",
                        "tmin": t.get("nozzle_temp_min"), "tmax": t.get("nozzle_temp_max"),
                    })
            vt = self.state.get("vt_tray") or {}
            out.append({
                "key": "ext",
                "label": "Externe Spule",
                "material": vt.get("tray_type", "") or "",
                "color": ("#" + vt["tray_color"][:6]) if vt.get("tray_color") else "",
                "profile": vt.get("tray_info_idx", "") or "",
                "tmin": vt.get("nozzle_temp_min"), "tmax": vt.get("nozzle_temp_max"),
            })
        if len(out) == 1:  # noch keine AMS-Daten: Standard-AMS-lite anzeigen
            out = [{"key": f"ams0-{i}", "label": f"AMS Slot {i + 1}", "material": "", "color": ""} for i in range(4)] + out
        return out

    def status(self):
        with self.lock:
            s = self.state
            gs = s.get("gcode_state", "")
            active = gs in ACTIVE
            return {
                "id": self.id, "name": self.name, "type": self.type,
                "online": self.online and time.time() - self.last_msg < 600,
                "state": STATE_DE.get(gs, gs or "Unbekannt"),
                "printing": active,
                "paused": gs == "PAUSE",
                "job": s.get("subtask_name", "") if active else "",
                "progress": s.get("mc_percent", 0) if active else None,
                "remaining_min": s.get("mc_remaining_time") if active else None,
                "temps": [
                    {"label": "Düse", "actual": s.get("nozzle_temper"), "target": s.get("nozzle_target_temper")},
                    {"label": "Bett", "actual": s.get("bed_temper"), "target": s.get("bed_target_temper")},
                ],
                "current_slot": tray_key(s.get("ams", {}).get("tray_now")) if active else None,
                "error": self.last_error,
                "layer": s.get("layer_num") if active else None,
                "total_layers": s.get("total_layer_num") if active else None,
                "speed": SPEED_DE.get(s.get("spd_lvl")),
                "speed_pct": s.get("spd_mag"),
                "fans": [
                    {"label": "Bauteil", "pct": _fan_pct(s.get("cooling_fan_speed"))},
                    {"label": "Hotend", "pct": _fan_pct(s.get("heatbreak_fan_speed"))},
                ],
                "nozzle_diameter": s.get("nozzle_diameter"),
                "light": next((l.get("mode") == "on" for l in (s.get("lights_report") or []) if l.get("node") == "chamber_light"), None),
                "wifi": s.get("wifi_signal"),
                "stage": stage_info(s.get("stg_cur")) if active else None,
                "stage_steps": self.stage_steps() if active else [],
            }

    # --- Slot am Drucker einstellen (wie in Bambu Studio) ---------------------
    # Generische Bambu-Profile: Drucker, Bambuddy und Bambu Studio zeigen dann Material + Farbe.
    GENERIC = {
        "PLA": ("GFL99", 190, 230), "PETG": ("GFG99", 220, 260), "ABS": ("GFB99", 240, 270),
        "ASA": ("GFB98", 240, 270), "TPU": ("GFU99", 200, 240), "PC": ("GFC99", 260, 280),
        "PA": ("GFN99", 260, 290), "PVA": ("GFS99", 190, 210),
    }

    def learn_profiles(self):
        """Merkt sich eigene Bambu-Profile (z. B. '01 Sunlu PLA +') pro Spoolman-Filamentsorte.
        Wer in Bambu Studio/am Drucker einmal ein Profil für einen Slot wählt, bekommt es bei
        der nächsten Zuweisung derselben Sorte automatisch wieder."""
        generic = {v[0] for v in self.GENERIC.values()}
        with self.store.lock:
            known = self.store.data.setdefault("profiles", {})
            for sl in self.slots():
                idx = sl.get("profile")
                sid = self.store.get_slot(self.id, sl["key"])
                if not idx or idx in generic or not sid:
                    continue
                try:
                    sp = self.spoolman.spool(sid)
                except Exception:
                    continue
                if not sp or sp.get("filament_id") is None:
                    continue
                if not sameish(sp.get("material"), sl.get("material")):
                    continue
                fid = str(sp["filament_id"])
                entry = {"idx": idx, "tmin": sl.get("tmin"), "tmax": sl.get("tmax")}
                if known.get(fid) != entry:
                    known[fid] = entry
                    self.store._save()
                    log.info("[%s] Profil gelernt: Filament %s -> %s", self.id, fid, idx)

    def set_slot_filament(self, key, spool):
        """Schreibt Material/Farbe der Spule in den AMS-Slot bzw. die externe Spule."""
        if not spool or not self.online:
            return False
        mat = (spool.get("material") or "").upper()
        base = next((b for b in ("PETG", "PLA", "ABS", "ASA", "TPU", "PVA", "PC", "PA") if mat.startswith(b)), None)
        if not base:
            raise ValueError(f"Material '{spool.get('material')}' wird nicht automatisch an den Drucker übertragen")
        idx, tmin, tmax = self.GENERIC[base]
        t = spool.get("extruder_temp")
        if t:
            tmin, tmax = int(t) - 15, int(t) + 15
        learned = self.store.data.get("profiles", {}).get(str(spool.get("filament_id")))
        if learned:
            idx = learned["idx"]
            tmin = int(learned.get("tmin") or tmin)
            tmax = int(learned.get("tmax") or tmax)
        color = (spool.get("color") or "#FFFFFF").lstrip("#")[:6].upper() + "FF"
        if key == "ext":
            ams_id, tray_id = 255, 254
        else:
            a, t_ = key[3:].split("-")
            ams_id, tray_id = int(a), int(t_)
        self._publish({"print": {
            "sequence_id": "0", "command": "ams_filament_setting",
            "ams_id": ams_id, "tray_id": tray_id, "tray_info_idx": idx,
            "tray_color": color, "nozzle_temp_min": tmin, "nozzle_temp_max": tmax, "tray_type": base,
        }})
        log.info("[%s] Slot %s am Drucker gesetzt: %s %s (Profil %s)", self.id, key, base, color, idx)
        threading.Timer(2, self._pushall).start()
        return True

    def light_state(self):
        with self.lock:
            for l in self.state.get("lights_report") or []:
                if l.get("node") == "chamber_light":
                    return l.get("mode") == "on"
        return None

    def set_light(self, on):
        self._publish({"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
                                  "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
                                  "loop_times": 0, "interval_time": 0}})
        threading.Timer(1.5, self._pushall).start()

    def control(self, action):
        if action not in ("pause", "resume", "stop"):
            raise ValueError("Unbekannte Aktion")
        self._publish({"print": {"sequence_id": "0", "command": action, "param": ""}})
