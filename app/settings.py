"""Einstellungen aus der Oberfläche (/data/settings.json). Startwerte kommen aus der config.yaml."""
import json
import os
import threading

from .notify import DEFAULT_EVENTS, EVENTS, PHOTO_EVENTS

ALLOWED = {"notify_events": EVENTS, "photo_events": ["finish", "fail"], "notify_photo": PHOTO_EVENTS, "tg_events": EVENTS}
NEW_IN_V2 = ["offline", "waiting", "plate", "power", "service", "maintenance"]   # neue Standard-Ereignisse
HIDDEN = {"pin_hash", "pin_salt", "auth_secret"}   # nie an den Browser geben
SECRET = {"bambuddy_key", "ntfy_token", "tg_token", "ai_key"}              # setzbar, aber nur als „gesetzt“ zurückgeben

# Schlüssel: (Typ, Standardwert)
SPEC = {
    "theme": (str, "dark"),                 # dark | light | auto
    "price_kwh": (float, 0.30),             # €/kWh
    "wear_per_h": (float, 0.0),             # Verschleiß/Maschinenkosten €/Druckstunde
    "fallback_price_kg": (float, 0.0),      # €/kg, wenn in Spoolman kein Preis steht
    "ntfy_url": (str, ""),
    "ntfy_token": (str, ""),
    "click_url": (str, ""),
    "notify_events": (list, DEFAULT_EVENTS),
    "notify_photo": (list, ["finish", "fail", "pause", "error", "plate"]),
    "notify_prio": (dict, {}),
    "tg_token": (str, ""),                 # Telegram-Bot (von @BotFather)
    "tg_chat": (str, ""),                  # gekoppelter Chat (wird beim „Verbinden“ automatisch gesetzt)
    "tg_events": (list, DEFAULT_EVENTS),   # Meldungen, die (zusätzlich) an Telegram gehen             # Dringlichkeit je Meldung (1–5); fehlt = Standard aus notify.PRIO_DEFAULT
    "progress_steps": (str, "25,50,75"),    # Fortschritt melden bei … %
    "bed_cool_c": (int, 35),                # „Bett abgekühlt“ unter … °C
    "service_alarm_min": (int, 5),          # Dienst so lange nicht erreichbar -> melden
    "notify_v": (int, 1),
    "low_spool_g": (float, 100.0),
    "runout_tol_g": (float, 15.0),
    "dry_days": (str, "PLA=60, PETG=21, ABS=30, ASA=30, TPU=7, PA=3, PC=7, PVA=2, *=30"),   # Tage offen bis „trocknen“
    "oe_restart": (bool, False),
    "oe_container": (str, "octoeverywhere"),
    "oe_delay": (int, 20),
    "sync_slots": (bool, True),
    "storage_location": (str, "Lager"),
    "history_on_dashboard": (int, 5),
    "photo_enabled": (bool, True),
    "photo_events": (list, ["finish", "fail"]),
    "photo_delay": (int, 5),
    "warn_short": (bool, True),
    "short_reserve_g": (float, 10.0),
    "public_url": (str, ""),               # Adresse für QR-Codes/NFC, leer = wie im Browser
    "label_host": (str, ""),               # Brother-QL im Netzwerk (IP), leer = Druck über den Browser
    "label_model": (str, "QL-810W"),
    "label_media": (str, "62x29"),
    "label_len": (float, 30.0),            # Länge bei Endlosband (mm)
    "label_cut": (bool, True),
    "tab_progress": (bool, True),
    "power_auto": (bool, False),           # Strom-Verwaltung: Drucker automatisch ausschalten (statt Bambuddy „Auto Aus“)
    "power_off_temp": (float, 45.0),       # erst aus, wenn die Düse darunter ist (°C)
    "power_after_min": (float, 10.0),      # Wartezeit nach Druckende (min)
    "power_idle_min": (float, 30.0),       # Leerlauf ohne Druck (z. B. von Hand eingeschaltet) (min)          # Fortschritt im Browser-Tab (Titel + Symbol)
    "ai_enabled": (bool, False),           # TEST: Etikett zusätzlich mit Gemini lesen (entfernbar)
    "ai_key": (str, ""),
    "ai_model": (str, "gemini-flash-lite-latest"),
    "ai_mode": (str, "fallback"),          # fallback | always
    "backup_auto": (str, "off"),           # off | daily | weekly
    "backup_hour": (int, 3),
    "backup_keep": (int, 14),
    "backup_media": (bool, False),
    "backup_dir": (str, ""),               # leer = /data/backups
    "pin_mode": (str, "off"),              # off | always | external
    "pin_hash": (str, ""),
    "pin_salt": (str, ""),
    "auth_secret": (str, ""),
    # Dienste (Cockpit)
    "spoolman_url": (str, ""),             # leer = Wert aus der config.yaml
    "spoolman_container": (str, "spoolman"),
    "bambuddy_url": (str, ""),             # z. B. http://192.168.1.10:8000
    "bambuddy_key": (str, ""),             # nur nötig, wenn in Bambuddy die Anmeldung aktiv ist
    "bambuddy_container": (str, "bambuddy"),
    "queue_on_dashboard": (bool, True),
    "oe_url": (str, "https://octoeverywhere.com/dashboard"),
    "nachtruhe_on": (bool, False),
    "bambuddy_import": (bool, False),       # Import-Knöpfe (Verlauf, Wartung) aus Bambuddy anzeigen          # Nachtruhe-Wächter in PrintHub laufen lassen
}


def from_config(cfg):
    """Übernimmt vorhandene Werte aus der config.yaml als Startwerte."""
    v = {}
    en = cfg.get("energy") or {}
    if "price_kwh" in en:
        v["price_kwh"] = en["price_kwh"]
    if "wear_per_h" in en:
        v["wear_per_h"] = en["wear_per_h"]
    n = cfg.get("notify") or {}
    for a, b in (("ntfy_url", "ntfy_url"), ("token", "ntfy_token"), ("click_url", "click_url"),
                 ("events", "notify_events"), ("low_spool_g", "low_spool_g")):
        if n.get(a) not in (None, ""):
            v[b] = n[a]
    if "sync_slots_to_printer" in cfg:
        v["sync_slots"] = cfg["sync_slots_to_printer"]
    for k in ("spoolman_url", "bambuddy_url"):
        if cfg.get(k):
            v[k] = cfg[k]
    if cfg.get("storage_location"):
        v["storage_location"] = cfg["storage_location"]
    for pc in cfg.get("printers", []):
        r = pc.get("restart_on_start")
        if r:
            v["oe_restart"] = True
            v["oe_container"] = r if isinstance(r, str) else ",".join(r)
            v["oe_delay"] = pc.get("restart_delay_s", 20)
            break
    return v


class Settings:
    def __init__(self, path, cfg):
        self.path = path
        self.lock = threading.Lock()
        self.values = {k: d for k, (_, d) in SPEC.items()}
        self.values.update(self._clean(from_config(cfg), strict=False))
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.values.update(self._clean(json.load(f), strict=False))
            except Exception:
                pass
        if int(self.values.get("notify_v") or 1) < 2:   # neue Ereignisse einmalig dazuschalten
            ev = list(self.values.get("notify_events") or [])
            self.values["notify_events"] = ev + [e for e in NEW_IN_V2 if e not in ev]
            self.values["notify_v"] = 2
        if int(self.values.get("notify_v") or 1) < 3:   # v3: Filament-Check der Warteschlange
            for k in ("notify_events", "tg_events"):
                ev = list(self.values.get(k) or [])
                if "queue_check" not in ev:
                    self.values[k] = ev + ["queue_check"]
            self.values["notify_v"] = 3
        if int(self.values.get("notify_v") or 1) < 5:   # v5: Trocknen
            for k in ("notify_events", "tg_events"):
                ev = list(self.values.get(k) or [])
                if "dry" not in ev:
                    self.values[k] = ev + ["dry"]
        if int(self.values.get("notify_v") or 1) < 4:   # v4: Spule leer gelaufen
            for k in ("notify_events", "tg_events"):
                ev = list(self.values.get(k) or [])
                if "runout" not in ev:
                    self.values[k] = ev + ["runout"]
            self.values["notify_v"] = 4
        if int(self.values.get("notify_v") or 1) < 5:
            self.values["notify_v"] = 5
            try:
                self._write()
            except OSError:
                pass

    def __getitem__(self, k):
        return self.values[k]

    def get(self, k, default=None):
        return self.values.get(k, default)

    def all(self):
        """Öffentliche Einstellungen (ohne PIN-Hash und Geheimnisse)."""
        out = {k: v for k, v in self.values.items() if k not in HIDDEN and k not in SECRET}
        out["pin_set"] = bool(self.values.get("pin_hash"))
        for k in SECRET:
            out[k + "_set"] = bool(self.values.get(k))
        return out

    def _clean(self, data, strict=True):
        out = {}
        for k, v in (data or {}).items():
            if k not in SPEC:
                if strict:
                    raise ValueError(f"Unbekannte Einstellung: {k}")
                continue
            typ, _ = SPEC[k]
            try:
                if typ is bool:
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "ja", "on")
                elif typ is float:
                    v = float(str(v).replace(",", "."))
                    if v < 0:
                        raise ValueError
                elif typ is int:
                    v = int(float(str(v).replace(",", ".")))
                    if v < 0:
                        raise ValueError
                elif typ is dict:
                    if not isinstance(v, dict):
                        raise ValueError
                    v = {str(a): int(b) for a, b in v.items() if str(a) in EVENTS and str(b).isdigit() and 1 <= int(b) <= 5}
                elif typ is list:
                    v = [x for x in (v if isinstance(v, list) else str(v).split(",")) if x in ALLOWED.get(k, EVENTS)]
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"Ungültiger Wert für {k}: {v!r}")
                continue
            if k == "theme" and v not in ("dark", "light", "auto"):
                if strict:
                    raise ValueError("Theme muss dark, light oder auto sein")
                continue
            if k == "pin_mode" and v not in ("off", "always", "external"):
                raise ValueError("Ungültiger Zugriffsschutz")
            if k in HIDDEN and strict:
                raise ValueError("Diese Einstellung kann nicht direkt geändert werden")
            if k == "backup_auto" and v not in ("off", "daily", "weekly"):
                raise ValueError("Zeitplan muss off, daily oder weekly sein")
            if k == "backup_dir" and v and not (v.startswith("/") and not v.startswith(("/etc", "/usr", "/bin", "/proc", "/sys", "/srv"))):
                raise ValueError("Backup-Ordner: absoluter Pfad im Container, z. B. /backups (in Unraid als Pfad einbinden)")
            if k == "public_url" and v and not v.startswith(("http://", "https://")):
                raise ValueError("Adresse muss mit http:// oder https:// beginnen")
            if k in ("spoolman_url", "bambuddy_url", "oe_url"):
                v = v.rstrip("/")
                if v and not v.startswith(("http://", "https://")):
                    raise ValueError("Adresse muss mit http:// oder https:// beginnen")
            if k == "ntfy_url" and v and not v.startswith(("http://", "https://")):
                raise ValueError("ntfy-Adresse muss mit http:// oder https:// beginnen")
            out[k] = v
        return out

    def _write(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.values, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # --- PIN ---------------------------------------------------------------
    @staticmethod
    def _hash(pin, salt):
        import hashlib
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()

    def set_pin(self, pin):
        import secrets
        pin = str(pin or "").strip()
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise ValueError("Die PIN muss aus 4 bis 8 Ziffern bestehen")
        salt = secrets.token_hex(16)
        with self.lock:
            self.values.update(pin_salt=salt, pin_hash=self._hash(pin, salt), auth_secret=secrets.token_hex(32))
            self._write()   # neues Geheimnis -> alle Geräte müssen sich neu anmelden

    def check_pin(self, pin):
        import hmac
        h, salt = self.values.get("pin_hash"), self.values.get("pin_salt")
        return bool(h and salt) and hmac.compare_digest(self._hash(str(pin or ""), salt), h)

    def auth_token(self):
        import hashlib, hmac
        sec = self.values.get("auth_secret") or ""
        return hmac.new(sec.encode(), (self.values.get("pin_hash") or "").encode(), hashlib.sha256).hexdigest() if sec else ""

    def update(self, data):
        clean = self._clean(data)
        if clean.get("pin_mode", self.values.get("pin_mode")) != "off" and not self.values.get("pin_hash"):
            raise ValueError("Bitte zuerst eine PIN festlegen")
        with self.lock:
            self.values.update(clean)
            self._write()
        return self.all()
