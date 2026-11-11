"""Push-Benachrichtigungen über ntfy – zentral für PrintHub und den Druckwächter."""
import base64
import json
import logging
import os
import threading
import time

import requests

log = logging.getLogger("notify")

# Alle Ereignisse: Schlüssel -> (Gruppe, Beschriftung, Standard an?, Foto möglich?)
CATALOG = {
    "start": ("Druck", "Druck gestartet", True, True),
    "first_layer": ("Druck", "Erste Schicht fertig", False, True),
    "progress": ("Druck", "Fortschritt", False, True),
    "finish": ("Druck", "Druck fertig (mit Kosten)", True, True),
    "fail": ("Druck", "Druck abgebrochen / fehlgeschlagen", True, True),
    "pause": ("Druck", "Druck pausiert (z. B. Gadget, Filament)", True, True),
    "error": ("Druck", "Druckerfehler (HMS)", True, True),
    "bed_cooled": ("Druck", "Bett abgekühlt – Teil entnehmen", False, False),
    "offline": ("Druck", "Drucker während eines Drucks offline", True, False),
    "waiting": ("Druckwächter", "Auftrag wartet aufs Druckfenster", True, False),
    "plate": ("Druckwächter", "Platte belegt – mit Antwort-Knöpfen", True, True),
    "power": ("Druckwächter", "Drucker ausgeschaltet", True, False),
    "cfg": ("Druckwächter", "Einstellungen übernommen", False, False),
    "low_spool": ("Spulen & Lager", "Spule fast leer", True, False),
    "short": ("Spulen & Lager", "Filament reicht nicht für den Druck", True, False),
    "stock_low": ("Spulen & Lager", "Lagerbestand unter Minimum", False, False),
    "dry": ("Spulen & Lager", "Spule im Drucker sollte getrocknet werden", True, False),
    "runout": ("Spulen & Lager", "Spule leer gelaufen – Lager prüfen / Leergewicht korrigieren", True, False),
    "maintenance": ("Wartung", "Wartung fällig", True, False),
    "service": ("Dienste", "Spoolman / Bambuddy / OctoEverywhere nicht erreichbar", True, False),
    "queue_add": ("Warteschlange", "Neuer Auftrag in der Bambuddy-Warteschlange", False, False),
    "queue_done": ("Warteschlange", "Warteschlange abgearbeitet", False, False),
    "queue_check": ("Warteschlange", "Neuer Auftrag mit Filament-Problem (falsches Material, Spule reicht nicht)", True, False),
}
EVENTS = list(CATALOG)
# Dringlichkeit (ntfy-Priorität 1–5) je Meldung. Ab 4 („hoch“) stellt ntfy.sh über Firebase mit hoher Priorität zu –
# die Meldung durchbricht dann den Android-Ruhemodus, auch ohne „Sofortnachrichten“ in der App.
PRIO_DEFAULT = {
    "fail": 5, "error": 5, "pause": 5, "offline": 5, "waiting": 5, "plate": 5, "power": 5,
    "finish": 4, "low_spool": 4, "runout": 4, "dry": 3, "short": 4, "bed_cooled": 4, "queue_check": 4,
    "start": 3, "first_layer": 3, "progress": 3, "cfg": 3, "stock_low": 3, "maintenance": 3, "service": 3,
    "queue_add": 3, "queue_done": 3,
}
PRIO_NAMES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def prio_for(settings, event, fallback=3):
    """Priorität aus den Einstellungen (sonst Standard der Meldung)."""
    try:
        v = int(((settings.get("notify_prio") or {}) if settings else {}).get(event) or 0)
    except (TypeError, ValueError):
        v = 0
    if 1 <= v <= 5:
        return v
    return PRIO_DEFAULT.get(event, fallback)
DEFAULT_EVENTS = [k for k, v in CATALOG.items() if v[2]]
PHOTO_EVENTS = [k for k, v in CATALOG.items() if v[3]]
DEDUP_S = 180          # gleiches Ereignis innerhalb dieser Zeit nur einmal (PrintHub + Druckwächter)
DEDUP_LONG = {"start": 900}   # Druckstart: der Druckwächter meldet schon die Freigabe
DEFER = {"start", "pause", "fail", "error"}   # wartet kurz, ob der Druckwächter die ausführlichere Meldung schickt


def sent_file(data_dir):
    return os.path.join(data_dir, "nachtruhe", "sent.json")


def mark_sent(path, event):
    try:
        d = json.load(open(path)) if os.path.exists(path) else {}
    except Exception:
        d = {}
    d[event] = time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def recently_sent(path, event, window=None):
    try:
        return time.time() - json.load(open(path)).get(event, 0) < (window or DEDUP_LONG.get(event, DEDUP_S))
    except Exception:
        return False


class Notifier:
    """Liest Adresse/Ereignisse bei jedem Versand aus den Einstellungen – Änderungen gelten sofort."""

    def __init__(self, settings, data_dir=None):
        self.settings = settings
        self.sent_path = sent_file(data_dir) if data_dir else None
        self.waechter_running = lambda: False
        self.telegram = None        # wird in main gesetzt (Telegram-Bot)

    @property
    def url(self):
        return (self.settings.get("ntfy_url") or "").strip()

    @property
    def token(self):
        return self.settings.get("ntfy_token") or ""

    @property
    def events(self):
        return set(self.settings.get("notify_events") or [])

    @property
    def low_spool_g(self):
        return float(self.settings.get("low_spool_g") or 0)

    @property
    def click(self):
        return self.settings.get("click_url") or ""

    @property
    def tg_on(self):
        return bool(self.telegram and self.telegram.enabled)

    @property
    def enabled(self):
        return bool(self.url) or self.tg_on

    def channels(self, event, force=False):
        """Wohin diese Meldung geht: (an ntfy?, an Telegram?)."""
        nt = bool(self.url) and (force or event in self.events)
        tg = self.tg_on and (force or event in set(self.settings.get("tg_events") or []))
        return nt, tg

    def send(self, event, title, message, tags="", priority="default", force=False, photo=None, only=None,
             buttons=None, click=None):
        """photo: Funktion, die ein JPEG liefert (nur aufgerufen, wenn für das Ereignis Fotos an sind).
        only: "ntfy" / "telegram" = nur über diesen Weg (Testnachricht)."""
        nt, tg = self.channels(event, force)
        if only == "ntfy":
            tg = False
        elif only == "telegram":
            nt = False
        if not (nt or tg):
            return False
        threading.Thread(target=self._run, args=(event, title, message, tags, priority, force, photo, nt, tg, buttons, click),
                         daemon=True).start()
        return True

    def _run(self, event, title, message, tags, priority, force, photo, nt=True, tg=False, buttons=None, click=None):
        if not force and self.sent_path and event in DEFER:
            if self.waechter_running():
                time.sleep(25)
            if recently_sent(self.sent_path, event):
                log.info("Meldung '%s' übersprungen – kam gerade schon (Druckwächter)", event)
                return
            mark_sent(self.sent_path, event)
        img = None
        if photo and event in (self.settings.get("notify_photo") or []):
            try:
                img = photo()
            except Exception as e:
                log.debug("Foto für Meldung: %s", e)
        fb = priority if isinstance(priority, int) else PRIO_NAMES.get(priority, 3)
        prio = prio_for(self.settings, event, fb) if event in CATALOG else fb
        if nt:
            self._post(title, message, tags, prio, img, click)
        if tg:
            self.telegram.send(title, message, prio, img, buttons=buttons, tags=tags)

    def _post(self, title, message, tags, priority, img=None, click=None):
        server, topic = self.url.rstrip("/").rsplit("/", 1)
        prio = priority if isinstance(priority, int) else PRIO_NAMES.get(priority, 3)
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            if img:
                enc = lambda t: t if t.isascii() else "=?UTF-8?B?" + base64.b64encode(t.encode()).decode() + "?="
                h = {"Title": enc(title), "Message": enc(message.replace("\n", "\\n")), "Priority": str(prio),
                     "Filename": "drucker.jpg", "Content-Type": "image/jpeg", **auth}
                if tags:
                    h["Tags"] = tags
                if click or self.click:
                    h["Click"] = click or self.click
                r = requests.put(f"{server}/{topic}", data=img, headers=h, timeout=30)
                if r.ok:
                    return
            body = {"topic": topic, "title": title, "message": message, "priority": prio}
            if tags:
                body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            if click or self.click:
                body["click"] = click or self.click
            r = requests.post(server, json=body, headers={**auth, "Priority": str(prio)}, timeout=10)
            r.raise_for_status()
        except Exception as e:
            log.warning("ntfy fehlgeschlagen: %s", e)
