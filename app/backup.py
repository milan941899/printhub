"""Sicherung und Wiederherstellung aller PrintHub-Daten als ZIP."""
import io
import json
import os
import sqlite3
import tempfile
import time
import zipfile

# Was gesichert wird (relativ zum Datenordner) – Medien optional
FILES = ["settings.json", "state.json", "nachtruhe/config.env"]
DBS = ["history.db"]


def create(data_dir, config_path, media=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("printhub-backup.json", json.dumps({"version": 1, "time": int(time.time()), "media": media}))
        for f in FILES:
            p = os.path.join(data_dir, f)
            if os.path.exists(p):
                z.write(p, "data/" + f)
        for f in DBS:
            p = os.path.join(data_dir, f)
            if os.path.exists(p):
                # konsistente Kopie, auch während PrintHub schreibt
                with tempfile.TemporaryDirectory() as td:
                    tmp = os.path.join(td, f)
                    src, dst = sqlite3.connect(p), sqlite3.connect(tmp)
                    with dst:
                        src.backup(dst)
                    src.close(); dst.close()
                    z.write(tmp, "data/" + f)
        if config_path and os.path.exists(config_path):
            z.write(config_path, "config/config.yaml")
        if media:
            md = os.path.join(data_dir, "media")
            if os.path.isdir(md):
                for name in sorted(os.listdir(md)):
                    if name.endswith((".png", ".jpg")):
                        z.write(os.path.join(md, name), "data/media/" + name)
    return buf.getvalue()


PARTS = {   # Bereiche zum gezielten Zurückspielen
    "verlauf": ("Druckverlauf & Bilder", lambda n: n == "data/history.db" or n.startswith("data/media/")),
    "einstellungen": ("Einstellungen", lambda n: n == "data/settings.json"),
    "zustand": ("Spulen-Zuordnungen, Wartung, Plattenerkennung", lambda n: n == "data/state.json"),
    "waechter": ("Druckwächter", lambda n: n == "data/nachtruhe/config.env"),
    "drucker": ("Drucker (config.yaml)", lambda n: n == "config/config.yaml"),
}


def restore(blob, data_dir, config_path, parts=None):
    """Spielt eine Sicherung zurück (optional nur einzelne Bereiche). Gibt die geschriebenen Dateien zurück."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("Das ist keine PrintHub-Sicherung (keine ZIP-Datei)")
    names = z.namelist()
    if "printhub-backup.json" not in names:
        raise ValueError("Das ist keine PrintHub-Sicherung (Kennung fehlt)")
    allowed = {"data/" + f for f in FILES + DBS} | {"config/config.yaml"}
    written = []
    for n in names:
        if n == "printhub-backup.json" or n.endswith("/"):
            continue
        if parts and not any(PARTS[p][1](n) for p in parts if p in PARTS):
            continue
        if n.startswith("data/media/"):
            base = os.path.basename(n)
            if "/" in n[len("data/media/"):] or not base.endswith((".png", ".jpg")):
                continue
            target = os.path.join(data_dir, "media", base)
        elif n in allowed:
            target = config_path if n == "config/config.yaml" else os.path.join(data_dir, n[len("data/"):])
        else:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".restore"
        with z.open(n) as src, open(tmp, "wb") as dst:
            dst.write(src.read())
        os.replace(tmp, target)
        written.append(n)
    return written


# ---------------- automatische Sicherung nach Zeitplan ----------------
NAME_RE = __import__("re").compile(r"^printhub-auto-\d{4}-\d{2}-\d{2}_\d{4,6}(-bilder)?\.zip$")


class AutoBackup:
    def __init__(self, settings, data_dir, config_path, log):
        self.settings, self.data_dir, self.config_path, self.log = settings, data_dir, config_path, log
        self.last_error = None

    @property
    def folder(self):
        return (self.settings.get("backup_dir") or "").strip() or os.path.join(self.data_dir, "backups")

    def _status_path(self):
        return os.path.join(self.data_dir, "backup-status.json")

    def status(self):
        try:
            return json.load(open(self._status_path(), encoding="utf-8"))
        except Exception:
            return {}

    def list(self):
        d = self.folder
        if not os.path.isdir(d):
            return []
        out = []
        for n in os.listdir(d):
            if NAME_RE.match(n):
                st = os.stat(os.path.join(d, n))
                out.append({"name": n, "size": st.st_size, "time": int(st.st_mtime)})
        return sorted(out, key=lambda x: x["time"], reverse=True)

    def path(self, name):
        if not NAME_RE.match(name or ""):
            return None
        p = os.path.join(self.folder, name)
        return p if os.path.exists(p) else None

    def run(self, reason="Zeitplan"):
        media = bool(self.settings.get("backup_media"))
        d = self.folder
        try:
            os.makedirs(d, exist_ok=True)
            blob = create(self.data_dir, self.config_path, media)
            name = time.strftime("printhub-auto-%Y-%m-%d_%H%M%S") + ("-bilder" if media else "") + ".zip"
            tmp = os.path.join(d, name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, os.path.join(d, name))
            keep = max(1, int(self.settings.get("backup_keep") or 14))
            removed = 0
            for old in self.list()[keep:]:
                try:
                    os.remove(os.path.join(d, old["name"]))
                    removed += 1
                except OSError:
                    pass
            st = {"time": int(time.time()), "ok": True, "name": name, "size": len(blob), "reason": reason, "removed": removed}
            self.log.info("Automatische Sicherung %s (%d KB) in %s%s", name, len(blob) // 1024, d, f", {removed} alte gelöscht" if removed else "")
        except Exception as e:
            st = {"time": int(time.time()), "ok": False, "error": f"{e.__class__.__name__}: {e}", "reason": reason}
            self.log.warning("Automatische Sicherung fehlgeschlagen: %s", e)
        try:
            with open(self._status_path(), "w", encoding="utf-8") as f:
                json.dump(st, f)
        except OSError:
            pass
        return st

    def due(self, now=None):
        mode = self.settings.get("backup_auto") or "off"
        if mode == "off":
            return False
        now = now or time.time()
        lt = time.localtime(now)
        if lt.tm_hour != int(self.settings.get("backup_hour") or 0):
            return False
        last = self.status().get("time") or 0
        gap = 6.5 * 86400 if mode == "weekly" else 20 * 3600
        return now - last >= gap

    def loop(self):
        time.sleep(60)
        while True:
            try:
                if self.due():
                    self.run()
            except Exception as e:
                self.log.warning("Zeitplan-Sicherung: %s", e)
            time.sleep(300)
