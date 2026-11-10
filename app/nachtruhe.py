"""Nachtruhe-Wächter als Teil von PrintHub.

Das bewährte Skript (nachtruhe_engine.py) läuft unverändert als eigener Prozess im PrintHub-Container.
PrintHub startet und überwacht ihn, zeigt Status und Log und schreibt die config.env
(das Skript übernimmt Änderungen selbst, ohne Neustart).
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from . import dockerctl

log = logging.getLogger("nachtruhe")
ENGINE = os.path.join(os.path.dirname(__file__), "nachtruhe_engine.py")
LEGACY_DIRS = ["/nachtruhe", "/legacy/nachtruhe"]          # optional eingebundener alter Ordner
LEGACY_CONTAINER = "bambuddy-nachtruhe"
SECRET = {"BAMBUDDY_API_KEY", "NTFY_TOKEN"}
RESTART_KEYS = {"PRINTER_ID", "PLUG_ID", "TZ_NAME"}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
DAYS = ["MO", "DI", "MI", "DO", "FR", "SA", "SO"]

# Felder, die die Oberfläche kennt: Schlüssel -> (Typ, Standard)
FIELDS = {
    "BAMBUDDY_URL": ("url", "http://localhost:8000"), "BAMBUDDY_API_KEY": ("secret", ""),
    "DRUCK_AB": ("time", "07:00"), "DRUCK_BIS": ("time", "22:30"), "PUFFER_MIN": ("int", "15"),
    "DRUCK_AB_WE": ("timeopt", ""), "DRUCK_BIS_WE": ("timeopt", ""),
    "PLATTE_PRUEFEN": ("bool", "1"), "PLATTE_QUELLE": ("choice", "bambuddy"), "PLATTE_VERSUCHE": ("int", "3"), "PLATTE_WARTE_MIN": ("int", "5"),
    "PLATTE_SCHWELLE": ("float", ""), "KAMERA_BILD": ("bool", "1"),
    "AUS_BEI_FEHLER": ("bool", "1"), "AUS_TEMP": ("int", "45"),
    "LICHT_AUS_NACH": ("int", "60"),
    "ALLES_UEBER_WAECHTER": ("bool", "1"),
    "BAMBUDDY_ALARM_MIN": ("int", "10"), "DRY_RUN": ("bool", "1"),
}
for _d in DAYS:
    FIELDS[f"DRUCK_AB_{_d}"] = ("timeopt", "")
    FIELDS[f"DRUCK_BIS_{_d}"] = ("timeopt", "")


def read_env(path):
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.split(" #")[0].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k.strip()] = v
    return vals


def write_env(path, changes):
    """Ändert nur die betroffenen Zeilen, Kommentare bleiben. changes: {KEY: wert | None (= Zeile entfernen)}."""
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else [
        "# Einstellungen für den Nachtruhe-Wächter (verwaltet von PrintHub → Einstellungen → Nachtruhe)", ""]
    done, out = set(), []
    for line in lines:
        s = line.strip()
        m = re.match(r"^([A-Z][A-Z0-9_]*)\s*=(.*)$", s)
        if not m or m.group(1) not in changes:
            out.append(line)
            continue
        key, rest = m.group(1), m.group(2)
        if key in done or changes[key] is None:
            done.add(key)
            continue
        done.add(key)
        comment = "        #" + rest.split(" #", 1)[1] if " #" in rest else ""
        out.append(f"{key}={changes[key]}{comment}")
    for k, v in changes.items():
        if k not in done and v is not None:
            out.append(f"{k}={v}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")
    os.replace(tmp, path)


def validate(changes):
    clean = {}
    for k, v in changes.items():
        if k not in FIELDS:
            raise ValueError(f"Unbekannte Einstellung: {k}")
        typ, _ = FIELDS[k]
        v = None if v is None else str(v).strip()
        if typ == "secret" and not v:
            continue                                   # leer = unverändert lassen
        if typ == "timeopt" and (v is None or v == ""):
            clean[k] = None
            continue
        if typ in ("time", "timeopt"):
            if v.lower() in ("aus", "-", "off") and typ == "timeopt":
                v = "aus"
            elif not TIME_RE.match(v or ""):
                raise ValueError(f"{k}: „{v}“ ist keine Uhrzeit (HH:MM)")
        elif typ == "int":
            try:
                v = str(int(float(v.replace(",", "."))))
            except (ValueError, AttributeError):
                raise ValueError(f"{k}: „{v}“ ist keine Zahl")
        elif typ == "float":
            if v:
                try:
                    float(v.replace(",", "."))
                except ValueError:
                    raise ValueError(f"{k}: „{v}“ ist keine Zahl")
                v = v.replace(",", ".")
        elif typ == "choice":
            if v not in ("bambuddy", "printhub"):
                raise ValueError(f"{k}: bambuddy oder printhub")
        elif typ == "bool":
            v = "1" if str(v).lower() in ("1", "true", "ja", "on") else "0"
        elif typ == "url":
            if v and not v.startswith(("http://", "https://")):
                raise ValueError(f"{k}: Adresse muss mit http:// oder https:// beginnen")
            v = (v or "").rstrip("/")
        if v is not None and ("\n" in v or "#" in v and typ != "secret"):
            raise ValueError(f"{k}: ungültige Zeichen")
        clean[k] = v
    return clean


class Nachtruhe:
    def __init__(self, data_dir, settings):
        self.dir = os.path.join(data_dir, "nachtruhe")
        os.makedirs(self.dir, exist_ok=True)
        self.config = os.path.join(self.dir, "config.env")
        self.logfile = os.path.join(self.dir, "nachtruhe.log")
        self.settings = settings
        self.proc = None
        self.started = None
        self.last_exit = None
        self.output = deque(maxlen=60)                 # stdout/stderr (auch Startfehler)
        self.legacy = None
        self.lock = threading.Lock()
        self._import_legacy()
        self._migrate_ntfy()

    def _migrate_ntfy(self):
        """ntfy-Adresse einmalig aus der config.env nach PrintHub übernehmen – danach gibt es nur noch eine."""
        vals = read_env(self.config)
        if not any(k in vals for k in ("NTFY_SERVER", "NTFY_TOPIC", "NTFY_TOKEN")):
            return
        try:
            if not self.settings.get("ntfy_url") and vals.get("NTFY_TOPIC"):
                server = (vals.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
                upd = {"ntfy_url": f"{server}/{vals['NTFY_TOPIC']}"}
                if vals.get("NTFY_TOKEN"):
                    upd["ntfy_token"] = vals["NTFY_TOKEN"]
                self.settings.update(upd)
                log.info("ntfy-Adresse aus dem Druckwächter übernommen: %s", upd["ntfy_url"])
            write_env(self.config, {"NTFY_SERVER": None, "NTFY_TOPIC": None, "NTFY_TOKEN": None})
        except Exception as e:
            log.warning("ntfy-Übernahme: %s", e)

    # --- alte Installation übernehmen ------------------------------------------
    def _import_legacy(self):
        """Beim ersten Start die Einstellungen der bisherigen Installation übernehmen:
        aus einem eingebundenen Ordner oder direkt aus dem alten Container (Docker-API, ohne Terminal)."""
        if os.path.exists(self.config):
            return
        for d in LEGACY_DIRS:
            src = os.path.join(d, "config.env")
            if os.path.exists(src):
                shutil.copy(src, self.config)
                log.info("Nachtruhe: config.env aus %s übernommen", d)
                return
        if not os.path.exists(dockerctl.SOCK):
            return
        for path, target in (("/app/config.env", self.config), ("/app/nachtruhe.log", self.logfile)):
            try:
                data = dockerctl.read_file(LEGACY_CONTAINER, path)
                if data:
                    with open(target, "wb") as f:
                        f.write(data)
                    log.info("Nachtruhe: %s aus dem Container %s übernommen", path, LEGACY_CONTAINER)
            except Exception as e:
                log.debug("Nachtruhe-Übernahme %s: %s", path, e)

    @property
    def enabled(self):
        return bool(self.settings.get("nachtruhe_on"))

    def legacy_running(self):
        """Läuft der alte, eigene Container noch? Dann darf PrintHub nicht parallel starten."""
        if not os.path.exists(dockerctl.SOCK):
            return None
        try:
            return dockerctl.info(LEGACY_CONTAINER)["running"]
        except LookupError:
            return False
        except Exception:
            return None

    # --- Prozess -------------------------------------------------------------
    def _start(self):
        env = dict(os.environ)
        env["NACHTRUHE_CONFIG"] = self.config
        # Meldungen laufen über die zentrale ntfy-Adresse von PrintHub
        url = (self.settings.get("ntfy_url") or "").rstrip("/")
        if url and "/" in url.split("://", 1)[-1]:
            server, topic = url.rsplit("/", 1)
            env["NTFY_SERVER"], env["NTFY_TOPIC"] = server, topic
        env["NTFY_TOKEN"] = self.settings.get("ntfy_token") or ""
        env["PRINTHUB_SETTINGS"] = getattr(self.settings, "path", "")
        env["PRINTHUB_SENT"] = os.path.join(self.dir, "sent.json")
        env.update(getattr(self, "extra_env", {}))
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("TZ_NAME", os.environ.get("TZ", "Europe/Rome"))
        self.proc = subprocess.Popen([sys.executable, "-u", ENGINE], env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.started = time.time()
        log.info("Nachtruhe gestartet (PID %s)", self.proc.pid)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.output.append(line)

    def _stop(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            log.info("Nachtruhe gestoppt")
        self.proc = None

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def supervise(self):
        """Läuft im Hintergrund: hält den Prozess am Leben, solange er eingeschaltet ist."""
        backoff_until = 0
        while True:
            try:
                with self.lock:
                    if self.proc and self.proc.poll() is not None:
                        code = self.proc.returncode
                        alive = time.time() - (self.started or 0)
                        self.last_exit = {"code": code, "time": int(time.time()), "after": int(alive),
                                          "lines": list(self.output)[-8:]}
                        log.warning("Nachtruhe beendet (Code %s nach %d s)", code, alive)
                        self.proc = None
                        backoff_until = time.time() + (60 if alive < 30 else 5)
                    if self.enabled and not self.running() and time.time() >= backoff_until:
                        self.legacy = self.legacy_running()
                        if self.legacy:
                            pass                       # alter Container läuft noch – nicht doppelt starten
                        elif not os.path.exists(self.config):
                            pass
                        else:
                            self._start()
                    elif not self.enabled and self.running():
                        self._stop()
            except Exception as e:
                log.warning("Nachtruhe-Überwachung: %s", e)
            time.sleep(5)

    def restart(self):
        with self.lock:
            self._stop()
            self.last_exit = None
        # supervise() startet neu, sobald eingeschaltet

    # --- Daten für die Oberfläche --------------------------------------------
    def tail(self, n=200):
        try:
            with open(self.logfile, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 60000))
                lines = f.read().decode("utf-8", "replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

    def state(self):
        vals = read_env(self.config)
        cfg = {}
        for k, (typ, default) in FIELDS.items():
            if typ == "secret":
                cfg[k + "_set"] = bool(vals.get(k))
            else:
                cfg[k] = vals.get(k, default)
        extra = {k: v for k, v in vals.items() if k not in FIELDS and k not in SECRET}
        return {"enabled": self.enabled, "running": self.running(), "since": self.started if self.running() else None,
                "pid": self.proc.pid if self.running() else None, "last_exit": self.last_exit,
                "legacy_running": self.legacy_running(), "config_exists": os.path.exists(self.config),
                "config": cfg, "extra": extra, "output": list(self.output)[-15:]}

    def save(self, changes):
        clean = validate(changes)
        old = read_env(self.config)
        write_env(self.config, clean)
        if any(k in RESTART_KEYS and old.get(k) != clean.get(k) for k in clean):
            self.restart()
        return clean

    def stop_legacy(self):
        name = dockerctl.find_container(LEGACY_CONTAINER)
        status, body = dockerctl._call("POST", f"/containers/{name}/stop?t=10")
        if status not in (204, 304):
            raise RuntimeError(f"Stoppen fehlgeschlagen: {status}")
        return name
