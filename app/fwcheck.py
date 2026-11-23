"""Neue paxx-Firmware (Snapmaker U1 Extended Firmware) melden.

Einmal am Tag die Releases auf GitHub abfragen und mit der Version auf dem Drucker vergleichen.
Ist eine neuere da, kommt genau einmal eine Meldung (Telegram/ntfy, Ereignis „fw_update“).
"""
import json
import logging
import re
import time

import requests

log = logging.getLogger("fwcheck")

REPO = "paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
VER_RE = re.compile(r"v?(\d+\.\d+\.\d+-paxx\d+-(\d+))")


def build_no(tag):
    """'v2.0.0-paxx12-24' → 24 (laufende Nummer der paxx-Builds); None wenn kein paxx-Tag."""
    m = VER_RE.search(tag or "")
    return int(m.group(2)) if m else None


def installed_version(p):
    """Version auf dem Drucker aus Moonraker lesen (system_info / printer info). None, wenn nicht zu finden."""
    for path, params in (("/machine/system_info", None), ("/printer/info", None), ("/server/info", None)):
        try:
            r = p.s.get(p.base + path, params=params, headers=p.headers, timeout=6)
            if not r.ok:
                continue
            m = VER_RE.search(json.dumps(r.json()))
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


def latest_releases():
    r = requests.get(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "PrintHub"}, timeout=15)
    r.raise_for_status()
    out = []
    for rel in r.json():
        tag = rel.get("tag_name") or ""
        if rel.get("draft") or build_no(tag) is None:
            continue
        out.append({"tag": tag.lstrip("v"), "build": build_no(tag), "pre": bool(rel.get("prerelease")),
                    "url": rel.get("html_url") or f"https://github.com/{REPO}/releases", "name": rel.get("name") or tag})
    return sorted(out, key=lambda x: x["build"], reverse=True)


class FwCheck:
    def __init__(self, store, printers, notifier, settings):
        self.store, self.printers, self.notifier, self.settings = store, printers, notifier, settings

    def _st(self):
        return self.store.data.setdefault("fwcheck", {})

    def check(self, force=False):
        """Rückgabe: Zustand {installed, latest, url, t, err} – meldet bei neuer Version einmal."""
        st = self._st()
        if not force and time.time() - st.get("t", 0) < 20 * 3600:
            return st
        st["t"] = time.time()
        try:
            rels = latest_releases()
        except Exception as e:
            st["err"] = f"GitHub nicht erreichbar: {e}"[:200]
            log.info("paxx-Prüfung: %s", st["err"])
            return st
        st.pop("err", None)
        if not rels:
            return st
        newest = rels[0]
        st["latest"], st["latest_url"] = newest["tag"], newest["url"]
        # installierte Version: vom U1 lesen, sonst die zuletzt bekannte / beim ersten Mal die neueste als Ausgangspunkt
        inst = None
        for p in self.printers.values():
            if p.type == "moonraker" and p.online:
                inst = installed_version(p)
                if inst:
                    break
        if inst:
            st["installed"] = inst
        elif not st.get("installed") and not st.get("baseline"):
            st["baseline"] = newest["tag"]          # nichts erkennbar: ab jetzt nur neuere melden
        ref = st.get("installed") or st.get("baseline")
        ref_b = build_no(ref) or 0
        if newest["build"] > ref_b and st.get("notified") != newest["tag"]:
            st["notified"] = newest["tag"]
            pre = " (Vorabversion)" if newest["pre"] else ""
            msg = f"Neu: {newest['tag']}{pre} · installiert: {st.get('installed') or 'unbekannt'}\n{newest['url']}"
            self.notifier.send("fw_update", "Snapmaker U1: neue paxx-Firmware", msg, "arrow_up")
            log.info("paxx-Firmware %s gemeldet (installiert %s)", newest["tag"], st.get("installed"))
        with self.store.lock:
            self.store._save()
        return st

    def loop(self):
        time.sleep(120)
        while True:
            try:
                if any(p.type == "moonraker" for p in self.printers.values()):
                    self.check()
            except Exception as e:
                log.debug("paxx-Prüfung: %s", e)
            time.sleep(3600)
