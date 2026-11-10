#!/usr/bin/env python3
"""
Läuft jetzt als Teil von PrintHub (wird von app/nachtruhe.py gestartet und überwacht).
Bambuddy Nachtruhe- & Licht-Wächter  v11  (API von Bambuddy 1.2.5.7)
=====================================================================

Voraussetzung in Bambuddy:
  Virtueller Drucker -> Modus "Warteschlange", "Automatisch starten" = AUS
  (neue Aufträge warten dann als "manual_start" in der Queue)

Ablauf:
  1. Nachtruhe: Ein wartender Auftrag wird nur freigegeben, wenn
       DRUCK_AB <= jetzt  und  jetzt + Druckzeit + Puffer <= DRUCK_BIS.
     Zeiten pro Wochentag möglich: DRUCK_AB_SA=09:00, DRUCK_BIS_SO=20:00,
     DRUCK_AB_WE / DRUCK_BIS_WE für Sa+So, oder DRUCK_AB_SO=aus (kein Druck).
     Wartende Aufträge lassen sich per Knopf [Jetzt starten] trotzdem starten.
  2. Vor dem Start: Drucker per Smart Plug einschalten, warten bis online,
     Licht AN (bestätigt), Platte per Kamera prüfen, dann starten.
  3. Platte belegt / Kamera ohne Bild:
       Drucker bleibt an, Push mit Knöpfen [Platte frei – prüfen] [Trotzdem drucken] [Abbrechen].
       Ohne Antwort: nach PLATTE_WARTE_MIN erneut prüfen + erneut melden.
       Nach PLATTE_VERSUCHE Hinweisen ohne Erfolg: letzte Prüfung, dann Auftrag abbrechen, Licht + Drucker aus.
  3b. Druck länger als das ganze Druckfenster: Push mit [Jetzt starten] [Abbrechen].
      Solche Aufträge blockieren die Warteschlange nicht.
  4. Licht AN während eines Drucks, Licht AUS LICHT_AUS_NACH s nach Druckende.
  5. Warnung per ntfy, wenn Bambuddy länger als BAMBUDDY_ALARM_MIN nicht erreichbar ist.

Einstellungen: /app/config.env  (KEY=WERT, eine Zeile pro Einstellung).
Änderungen werden automatisch übernommen (ohne Neustart) und per ntfy bestätigt.
Ausnahme: PRINTER_ID, PLUG_ID und TZ_NAME brauchen einen Neustart.
Nur Python-Standardbibliothek.
"""

import base64
import json
import os
import re
import socket
import threading
from collections import deque
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------- Netzwerk: IPv4 bevorzugen ----------------
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_zuerst(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    return sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)


socket.getaddrinfo = _getaddrinfo_ipv4_zuerst

# ---------------- Konfiguration ----------------
CONFIG_FILE = os.environ.get("NACHTRUHE_CONFIG") or os.environ.get("CONFIG_FILE", "/app/config.env")
_UMGEBUNG = dict(os.environ)          # Werte aus docker run -e … (werden von der Datei überschrieben)
_ZEIT_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
GEHEIM = ("BAMBUDDY_API_KEY", "NTFY_TOKEN", "NTFY_TOPIC", "NTFY_ANTWORT_TOPIC")


def lies_datei(pfad):
    """KEY=WERT-Datei einlesen -> dict (ohne Kommentare/Leerzeilen)."""
    werte = {}
    if not os.path.exists(pfad):
        return None
    with open(pfad, encoding="utf-8") as f:
        for nr, zeile in enumerate(f, 1):
            zeile = zeile.strip()
            if not zeile or zeile.startswith("#"):
                continue
            if "=" not in zeile:
                raise ValueError(f"Zeile {nr}: '{zeile}' – es fehlt das '='")
            k, v = zeile.split("=", 1)
            v = v.split(" #")[0].strip()          # Kommentar hinter dem Wert erlauben
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            werte[k.strip()] = v
    return werte


def baue_einstellungen(werte):
    """Aus Umgebung + Datei alle Einstellungen bauen und prüfen. Wirft ValueError bei Fehlern."""
    env = dict(_UMGEBUNG)
    env.update(werte or {})

    def g(name, default):
        return env.get(name, default)

    def ganzzahl(name, default):
        try:
            return int(g(name, default))
        except ValueError:
            raise ValueError(f"{name}={g(name, default)} ist keine Zahl")

    def kommazahl(name):
        v = str(g(name, "")).strip().replace(",", ".").rstrip("%").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            raise ValueError(f"{name}={g(name, '')} ist keine Zahl (z. B. 4 oder 3.5)")

    for k, v in env.items():
        if re.match(r"^DRUCK_(AB|BIS)(_(MO|DI|MI|DO|FR|SA|SO|WE))?$", k):
            if v.strip().lower() not in ("aus", "-", "off") and not _ZEIT_RE.match(v.strip()):
                raise ValueError(f"{k}={v} ist keine gültige Uhrzeit (Format HH:MM oder 'aus')")

    e = {
        "ENV": env,
        "URL": g("BAMBUDDY_URL", "http://localhost:8000").rstrip("/"),
        "API_KEY": g("BAMBUDDY_API_KEY", ""),
        "PRINTER_ID": g("PRINTER_ID", ""),
        "PLUG_ID": g("PLUG_ID", ""),
        "TZ_NAME": g("TZ_NAME", "Europe/Rome"),
        "DRUCK_AB": g("DRUCK_AB", "07:00").strip(),
        "DRUCK_BIS": g("DRUCK_BIS", "22:30").strip(),
        "PUFFER_MIN": ganzzahl("PUFFER_MIN", "15"),
        "LICHT_AUS_NACH": ganzzahl("LICHT_AUS_NACH", "60"),
        "LICHT_BOOT_WARTEN": ganzzahl("LICHT_BOOT_WARTEN", "10"),
        "KAMERA_EINREGELN": ganzzahl("KAMERA_EINREGELN", "10"),
        "PLATTE_PRUEFEN": g("PLATTE_PRUEFEN", "1") == "1",
        "PLATTE_QUELLE": g("PLATTE_QUELLE", "bambuddy").strip().lower(),
        "PLATTE_VERSUCHE": ganzzahl("PLATTE_VERSUCHE", "3"),
        "PLATTE_WARTE_MIN": ganzzahl("PLATTE_WARTE_MIN", "5"),
        "PLATTE_BEI_FEHLER": g("PLATTE_BEI_FEHLER", "warten").lower(),
        "KAMERA_BILD": g("KAMERA_BILD", "1") == "1",
        "AUS_BEI_FEHLER": g("AUS_BEI_FEHLER", "1") == "1",
        "AUS_TEMP": ganzzahl("AUS_TEMP", "45"),
        "PLATTE_SCHWELLE": kommazahl("PLATTE_SCHWELLE"),
        "BAMBUDDY_ALARM_MIN": ganzzahl("BAMBUDDY_ALARM_MIN", "10"),
        "DRY_RUN": g("DRY_RUN", "1") == "1",
        "NTFY_SERVER": (g("NTFY_SERVER", "") or "https://ntfy.sh").rstrip("/"),
        "NTFY_TOPIC": g("NTFY_TOPIC", ""),
        "NTFY_TOKEN": g("NTFY_TOKEN", ""),
    }
    e["NTFY_ANTWORT"] = g("NTFY_ANTWORT_TOPIC", f"{e['NTFY_TOPIC']}-antwort" if e["NTFY_TOPIC"] else "")
    if not e["API_KEY"]:
        raise ValueError("BAMBUDDY_API_KEY fehlt")
    return e


def wende_an(e):
    """Einstellungen als globale Variablen setzen."""
    global ENV
    for k, v in e.items():
        globals()[k] = v
    ENV = e["ENV"]


try:
    _datei = lies_datei(CONFIG_FILE)
    CONFIG_GELADEN = _datei is not None
    _start = baue_einstellungen(_datei)
except ValueError as fehler:
    raise SystemExit(f"Fehler in {CONFIG_FILE}: {fehler}")
wende_an(_start)

TAKT_S       = 3      # Status/Licht-Prüfung
QUEUE_TAKT_S = 30     # Warteschlangen-Prüfung
BOOT_TIMEOUT_S = 240  # max. Wartezeit bis Drucker nach Einschalten online ist
DRUCKER_AUS_SPERRE_MIN = 15  # nach "Drucker kam nicht online"

DRUCK_STATES = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}
ENDE_STATES  = {"FINISH", "FAILED"}
API = "/api/v1"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception as e:
    raise SystemExit(f"Zeitzone '{TZ_NAME}' nicht gefunden ({e}). "
                     f"Beim docker run -v /usr/share/zoneinfo:/usr/share/zoneinfo:ro angeben.")


# ---------------- Hilfsfunktionen ----------------
_letzte_meldung = None


def log(msg, einmalig=False):
    """einmalig=True: gleiche Meldung nicht ständig wiederholen."""
    global _letzte_meldung
    if einmalig and msg == _letzte_meldung:
        return
    _letzte_meldung = msg if einmalig else None
    zeile = f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}"
    print(zeile, flush=True)
    in_datei(zeile)


LOG_MAX_BYTES = 1_000_000   # danach wird nachtruhe.log -> nachtruhe.log.1 (1 alte Datei bleibt)


def in_datei(zeile):
    """Zusätzlich in eine Logdatei neben der config.env schreiben (LOG_DATEI=aus schaltet ab)."""
    pfad = ENV.get("LOG_DATEI", os.path.join(os.path.dirname(CONFIG_FILE), "nachtruhe.log"))
    if not pfad or pfad.lower() in ("aus", "off", "-"):
        return
    try:
        if os.path.exists(pfad) and os.path.getsize(pfad) > LOG_MAX_BYTES:
            os.replace(pfad, pfad + ".1")
        with open(pfad, "a", encoding="utf-8") as f:
            f.write(zeile + "\n")
    except OSError:
        pass


def api(method, path, body=None, params=None):
    url = URL + API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", API_KEY)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _ntfy_headers(req):
    if NTFY_TOKEN:
        req.add_header("Authorization", f"Bearer {NTFY_TOKEN}")


def _kodiert(text):
    """Header-Wert für ntfy: ASCII bleibt, sonst RFC 2047 (UTF-8, Base64)."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def _aktionen_json(aktionen):
    out = []
    for label, antwort in (aktionen or [])[:3]:
        a = {"action": "http", "label": label, "url": f"{NTFY_SERVER}/{NTFY_ANTWORT}",
             "method": "POST", "body": antwort, "clear": True}
        if NTFY_TOKEN:
            a["headers"] = {"Authorization": f"Bearer {NTFY_TOKEN}"}
        out.append(a)
    return out


def melden_mit_bild(titel, text, bild, tags=None, prio=3, aktionen=None):
    """Push mit angehängtem JPEG (Upload als Anhang). True bei Erfolg."""
    req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=bild, method="PUT")
    req.add_header("Content-Type", "image/jpeg")
    req.add_header("Filename", "platte.jpg")
    req.add_header("Title", _kodiert(titel))
    req.add_header("Message", _kodiert(text.replace("\n", "\\n")))
    req.add_header("Priority", str(prio))
    if tags:
        req.add_header("Tags", ",".join(tags))
    if aktionen and NTFY_ANTWORT:
        # Kurzformat: "http, Label, URL, method=POST, body=…, clear=true; …" (JSON geht im Header nicht)
        teile = []
        for a in _aktionen_json(aktionen):
            t = (f"http, {a['label'].replace(',', ' ')}, {a['url']}, method=POST, "
                 f"body={a['body']}, clear=true")
            if NTFY_TOKEN:
                t += f", headers.Authorization=Bearer {NTFY_TOKEN}"
            teile.append(t)
        req.add_header("Actions", _kodiert("; ".join(teile)))
    _ntfy_headers(req)
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:
        log(f"ntfy-Bildversand fehlgeschlagen ({e}) – sende ohne Bild.")
        return False


# ---------------- Abstimmung mit PrintHub (welche Meldungen, keine doppelten) ----------------
PH_SETTINGS = os.environ.get("PRINTHUB_SETTINGS", "")
PH_SENT = os.environ.get("PRINTHUB_SENT", "")
DEDUP_S = {"start": 900}           # Druckstart: Freigabe kommt vor dem eigentlichen Start


def _kategorie(titel, text):
    t = titel
    if t.startswith("Druck startet"):
        return "start"
    if t.startswith("Druck pausiert"):
        return "pause"
    if t.startswith("Druck fehlgeschlagen"):
        return "fail"
    if t.startswith(("Drucker meldet einen Fehler", "Drucker nicht erreichbar")):
        return "error"
    if t.startswith("Drucker ausgeschaltet"):
        return "power"
    if t.startswith(("Bambuddy nicht erreichbar", "Bambuddy wieder erreichbar")):
        return "service"
    if t.startswith(("Nachtruhe – Druck wartet", "Druck wartet aufs nächste", "Druck zu lang")):
        return "waiting"
    if t.startswith("Druck wartet:") or (t.startswith("Druck abgebrochen") and "Prüfungen" in text):
        return "plate"
    if t.startswith("Einstellungen übernommen"):
        return "cfg"
    return None                     # Antworten auf deine Knöpfe kommen immer


# Standard-Dringlichkeit je Meldung (gleich wie in PrintHub, notify.PRIO_DEFAULT); Einstellungen: notify_prio
PRIO_STANDARD = {"start": 3, "pause": 5, "fail": 5, "error": 5, "power": 5, "service": 3, "waiting": 5, "plate": 5, "cfg": 3}


def _prio(kat, prio, aktionen):
    """ntfy-Priorität: Meldungen mit Antwort-Knöpfen immer dringend (5), sonst laut PrintHub-Einstellung."""
    if aktionen:
        return 5
    if not kat:
        return prio
    eigen = {}
    if PH_SETTINGS:
        try:
            with open(PH_SETTINGS, encoding="utf-8") as f:
                eigen = json.load(f).get("notify_prio") or {}
        except Exception:
            eigen = {}
    try:
        v = int(eigen.get(kat) or 0)
    except (TypeError, ValueError):
        v = 0
    return v if 1 <= v <= 5 else PRIO_STANDARD.get(kat, prio)


def _ph_setting(key, default=None):
    """Einstellung aus PrintHub lesen (Datei wird bei jeder Änderung neu geschrieben)."""
    if not PH_SETTINGS:
        return default
    try:
        with open(PH_SETTINGS, encoding="utf-8") as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


WSTATE = os.environ.get("PRINTHUB_WSTATE", "")
QEVENTS = os.environ.get("PRINTHUB_QEVENTS", "")
PLANS = os.environ.get("PRINTHUB_PLANS", "")


def plaene():
    """Von PrintHub geplante Aufträge: {id: {"force": bool}} (force = trotz Druckfenster)."""
    if not PLANS:
        return {}
    try:
        with open(PLANS, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def geplant_um(item):
    """Geplante Startzeit aus Bambuddy (UTC) als lokale Zeit – oder None."""
    v = item.get("scheduled_time")
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None


def ereignis(item, text):
    """Was mit einem Auftrag passiert ist (für den Warteschlange-Verlauf in PrintHub)."""
    if not QEVENTS or not item:
        return
    try:
        with open(QEVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.time(), "id": item.get("id"), "name": auftragsname(item), "text": text},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def zustand_schreiben(**d):
    """Was der Wächter gerade vorhat – PrintHub entscheidet damit über das Ausschalten."""
    if not WSTATE:
        return
    d["time"] = time.time()
    try:
        with open(WSTATE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(WSTATE + ".tmp", WSTATE)
    except OSError:
        pass


def _erlaubt(kat):
    if not kat or not PH_SETTINGS:
        return True
    try:
        with open(PH_SETTINGS, encoding="utf-8") as f:
            ev = json.load(f).get("notify_events")
        return ev is None or kat in ev
    except Exception:
        return True


def _schon_gesendet(kat):
    """True, wenn PrintHub (oder wir) dieselbe Meldung gerade geschickt hat. Sonst als gesendet merken."""
    if kat not in ("start", "pause", "fail", "error") or not PH_SENT:
        return False
    try:
        d = json.load(open(PH_SENT)) if os.path.exists(PH_SENT) else {}
    except Exception:
        d = {}
    if time.time() - d.get(kat, 0) < DEDUP_S.get(kat, 180):
        return True
    d[kat] = time.time()
    try:
        with open(PH_SENT + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(PH_SENT + ".tmp", PH_SENT)
    except OSError:
        pass
    return False


def _an_printhub(kat, titel, text, prio, aktionen, bild, tags=None):
    """Meldung zusätzlich an PrintHub geben – PrintHub schickt sie an Telegram (falls verbunden und gewünscht)."""
    ph = os.environ.get("PRINTHUB_URL")
    if not ph:
        return
    body = {"kat": kat, "title": titel, "text": text, "prio": prio, "tags": tags or [],
            "buttons": [[l, a] for l, a in (aktionen or [])],
            "photo": base64.b64encode(bild).decode() if bild else None}
    req = urllib.request.Request(f"{ph}/api/notify/relay", data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-PrintHub-Token", os.environ.get("PRINTHUB_TOKEN", ""))
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        log(f"Telegram über PrintHub fehlgeschlagen: {e}")


def melden(titel, text, tags=None, prio=3, aktionen=None, bild=None):
    """Push über ntfy (und über PrintHub an Telegram). aktionen: Liste (Beschriftung, Antworttext) für Knöpfe. bild: JPEG-Bytes."""
    kat = _kategorie(titel, text)
    if _schon_gesendet(kat):
        log(f"Meldung „{titel}“ kam gerade schon von PrintHub – übersprungen.")
        return
    if DRY_RUN:
        titel = "[TEST] " + titel
    prio = _prio(kat, prio, aktionen)
    threading.Thread(target=_an_printhub, args=(kat, titel, text, prio, aktionen, bild, tags), daemon=True).start()
    if not NTFY_TOPIC:
        return
    if not _erlaubt(kat):
        log(f"Meldung „{titel}“ ist für ntfy ausgeschaltet.", einmalig=True)
        return
    if bild and melden_mit_bild(titel, text, bild, tags, prio, aktionen):
        return
    body = {"topic": NTFY_TOPIC, "title": titel, "message": text,
            "tags": tags or [], "priority": prio}
    if aktionen and NTFY_ANTWORT:
        body["actions"] = _aktionen_json(aktionen)
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Priority", str(prio))
    _ntfy_headers(req)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"ntfy-Fehler: {e}")


class AntwortEmpfaenger(threading.Thread):
    """Hält eine Dauerverbindung zum ntfy-Antwort-Topic und sammelt Knopfdrücke.
    Statt alle paar Sekunden neu abzufragen (Timeouts!) kommt jede Antwort sofort an."""

    def __init__(self):
        super().__init__(daemon=True)
        self.eingang = deque()
        self.lock = threading.Lock()
        self.seit = str(int(time.time()))
        self.fehler_in_folge = 0
        self.zuletzt = {}           # gleiche Antwort (z. B. ntfy UND Telegram gedrückt) nur einmal

    def neu(self, mid, msg):
        msg = str(msg or "").strip()
        if not msg or msg == "noop":
            return
        with self.lock:
            jetzt = time.time()
            if jetzt - self.zuletzt.get(msg, 0) < 90:
                return
            self.zuletzt[msg] = jetzt
            self.eingang.append((mid, msg))

    def telegram_eingang(self):
        """Knopf-Antworten aus Telegram: PrintHub schreibt sie in eine Datei, wir holen sie alle 2 s ab."""
        pfad = os.environ.get("PRINTHUB_INBOX", "")
        while pfad:
            try:
                if os.path.exists(pfad) and os.path.getsize(pfad) > 0:
                    arbeit = pfad + ".work"
                    os.replace(pfad, arbeit)
                    with open(arbeit, encoding="utf-8") as f:
                        for zeile in f:
                            try:
                                d = json.loads(zeile)
                            except ValueError:
                                continue
                            if time.time() - d.get("time", time.time()) < 3600:
                                log(f"Antwort aus Telegram: {d.get('msg')}")
                                self.neu(d.get("id"), d.get("msg"))
                    os.remove(arbeit)
            except Exception as e:
                log(f"Telegram-Antworten: {e}", einmalig=True)
            time.sleep(2)

    def run(self):
        threading.Thread(target=self.telegram_eingang, daemon=True).start()
        while True:
            topic = NTFY_ANTWORT
            if not topic:
                time.sleep(30)
                continue
            url = f"{NTFY_SERVER}/{topic}/json?since={self.seit}"
            req = urllib.request.Request(url)
            _ntfy_headers(req)
            try:
                # ntfy schickt alle ~45 s ein keepalive – 120 s ohne Daten = Verbindung tot
                with urllib.request.urlopen(req, timeout=120) as r:
                    if self.fehler_in_folge >= 3:
                        log("ntfy-Antworten wieder verbunden.")
                    self.fehler_in_folge = 0
                    for zeile in r:
                        try:
                            ev = json.loads(zeile.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if ev.get("id"):
                            self.seit = ev["id"]
                        if ev.get("event") == "message":
                            self.neu(ev.get("id"), ev.get("message", ""))
                        if NTFY_ANTWORT != topic:      # Topic in config.env geändert -> neu verbinden
                            break
            except Exception as e:
                self.fehler_in_folge += 1
                if self.fehler_in_folge == 3:
                    log(f"ntfy-Antworten: Verbindung klappt gerade nicht ({e}) – versuche weiter.")
                time.sleep(min(60, 5 * self.fehler_in_folge))

    def nimm(self, filter_fn=None):
        """Antworten entnehmen; mit filter_fn nur passende (der Rest bleibt liegen)."""
        with self.lock:
            alle = list(self.eingang)
            if filter_fn is None:
                self.eingang.clear()
                return alle
            treffer = [x for x in alle if filter_fn(x[1])]
            for x in treffer:
                self.eingang.remove(x)
            return treffer


EMPFAENGER = AntwortEmpfaenger()


def schalten(beschreibung, method, path, body=None, params=None):
    """Schreibende Aktion – im DRY_RUN nur loggen."""
    if DRY_RUN:
        log(f"[DRY RUN] {beschreibung}")
        return None
    log(beschreibung)
    return api(method, path, body, params)


def uhrzeit(hhmm, bezug):
    h, m = map(int, hhmm.split(":"))
    return bezug.replace(hour=h, minute=m, second=0, microsecond=0)


TAGE = ["MO", "DI", "MI", "DO", "FR", "SA", "SO"]
TAGNAME = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def zeiten_fuer(datum):
    """Druckfenster (ab, bis) als Text für diesen Wochentag – oder None, wenn der Tag 'aus' ist.
    Reihenfolge: DRUCK_AB_<TAG>  >  DRUCK_AB_WE (Sa/So)  >  DRUCK_AB."""
    tag = TAGE[datum.weekday()]
    we = tag in ("SA", "SO")
    ab = ENV.get(f"DRUCK_AB_{tag}") or (we and ENV.get("DRUCK_AB_WE")) or DRUCK_AB
    bis = ENV.get(f"DRUCK_BIS_{tag}") or (we and ENV.get("DRUCK_BIS_WE")) or DRUCK_BIS
    if ab.strip().lower() in ("aus", "-", "off") or bis.strip().lower() in ("aus", "-", "off"):
        return None
    return ab.strip(), bis.strip()


def fenster(dt):
    """(ab, bis) als datetime für den Tag von dt; leeres Fenster, wenn der Tag 'aus' ist."""
    z = zeiten_fuer(dt)
    if not z:
        leer = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return leer, leer
    return uhrzeit(z[0], dt), uhrzeit(z[1], dt)


def naechster_start(jetzt):
    """Nächster Fensterbeginn als lesbarer Text, z. B. 'morgen (Sa) ab 09:00'."""
    for i in range(8):
        tag = jetzt + timedelta(days=i)
        z = zeiten_fuer(tag)
        if not z:
            continue
        ab = uhrzeit(z[0], tag)
        if ab <= jetzt and i == 0:
            continue
        wort = "heute" if i == 0 else "morgen" if i == 1 else "übermorgen" if i == 2 else ""
        return f"{wort} ({TAGNAME[tag.weekday()]}) ab {z[0]}".strip()
    return "– kein Druckfenster konfiguriert"


def laengstes_fenster():
    basis = datetime.now(TZ)
    laengen = []
    for i in range(7):
        ab, bis = fenster(basis + timedelta(days=i))
        laengen.append(bis - ab)
    return max(laengen)


def fenster_text(dt):
    z = zeiten_fuer(dt)
    return f"{z[0]}–{z[1]}" if z else "kein Druck"


def wochen_text():
    """Mo 07:00–22:30, Di … – gleiche aufeinanderfolgende Tage zusammengefasst."""
    heute = datetime.now(TZ)
    montag = heute - timedelta(days=heute.weekday())
    tage = [(TAGNAME[i], fenster_text(montag + timedelta(days=i))) for i in range(7)]
    gruppen = []
    for name, txt in tage:
        if gruppen and gruppen[-1][2] == txt:
            gruppen[-1][1] = name
        else:
            gruppen.append([name, name, txt])
    return ", ".join(f"{a}{'–' + b if a != b else ''} {t}" for a, b, t in gruppen)


def auftragsname(item):
    return item.get("archive_name") or item.get("library_file_name") or f"Auftrag #{item.get('id')}"


def nicht_erreichbar(e):
    """Netzwerkfehler (Bambuddy down) – im Gegensatz zu HTTP-Fehlern wie 403."""
    return isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, OSError)) \
        and not isinstance(e, urllib.error.HTTPError)


# ---------------- Knopf-Beschriftungen ----------------
K_PRUEFEN    = "🔍 Platte frei – prüfen"
K_TROTZDEM   = "▶️ Trotzdem drucken"
K_ABBRECHEN  = "✖️ Abbrechen"
K_JETZT      = "▶️ Jetzt starten"
K_STOPPEN    = "⏹️ Druck stoppen"
K_FORTSETZEN = "▶️ Fortsetzen"


# ---------------- Bambuddy-Abfragen ----------------
def ermittle_drucker():
    if PRINTER_ID:
        return int(PRINTER_ID)
    drucker = api("GET", "/printers/") or []
    if len(drucker) != 1:
        namen = ", ".join(f"{d.get('id')}={d.get('name')}" for d in drucker)
        raise SystemExit(f"Bitte PRINTER_ID setzen – gefunden: {namen or 'keiner'}")
    log(f"Drucker: {drucker[0].get('name')} (ID {drucker[0]['id']})")
    return drucker[0]["id"]


def ermittle_plug(printer_id):
    if PLUG_ID:
        return int(PLUG_ID)
    try:
        plug = api("GET", f"/smart-plugs/by-printer/{printer_id}")
    except urllib.error.HTTPError:
        plug = None
    if not plug or not plug.get("id"):
        log("Kein Smart Plug mit dem Drucker verknüpft – Einschalten übernimmt dann Bambuddy.")
        return None
    log(f"Smart Plug: {plug.get('name')} (ID {plug['id']})")
    return plug["id"]


def status(printer_id):
    return api("GET", f"/printers/{printer_id}/status") or {}


def hole_roh(url):
    req = urllib.request.Request(url)
    req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), r.headers.get("Content-Type", "")


def kamerabild(printer_id, debug_bild=None):
    """JPEG-Bytes: bevorzugt das Analysebild (rote Markierung), sonst ein normaler Schnappschuss."""
    try:
        if debug_bild and debug_bild.startswith("data:image"):
            return base64.b64decode(debug_bild.split(",", 1)[1])
        token = (api("POST", "/printers/camera/stream-token") or {}).get("token", "")
        kandidaten = []
        if debug_bild:
            u = debug_bild if debug_bild.startswith("http") else URL + debug_bild
            kandidaten.append(u)
        kandidaten.append(f"{URL}{API}/printers/{printer_id}/camera/snapshot")
        for u in kandidaten:
            if token:
                u += ("&" if "?" in u else "?") + "token=" + urllib.parse.quote(token)
            try:
                daten, typ = hole_roh(u)
                if daten[:2] == b"\xff\xd8" or typ.startswith("image/"):
                    return daten
            except Exception as e:
                log(f"Kamerabild nicht abrufbar ({u.split('?')[0]}): {e}")
    except Exception as e:
        log(f"Kamerabild nicht abrufbar: {e}")
    return None


def licht(printer_id, an):
    schalten(f"Licht {'AN' if an else 'AUS'}", "POST",
             f"/printers/{printer_id}/chamber-light", params={"on": "true" if an else "false"})


# ---------------- Ablauf ----------------
class Waechter:
    def __init__(self, printer_id, plug_id):
        self.pid = printer_id
        self.plug = plug_id
        self.letzter_state = None
        self.ende_seit = None
        self.licht_aus_erledigt = True
        self.gesperrt_bis = None        # nach "Drucker kam nicht online"
        self.naechste_queue = 0.0
        self.naechste_abfang = 0.0
        self.abgefangen = set()
        self.dry_gemeldet = set()
        self.wartet_gemeldet = set()
        self.offline_seit = None        # Bambuddy nicht erreichbar seit
        self.offline_gemeldet = False
        self.freigegeben = set()        # per Knopf "Jetzt starten" freigegebene Aufträge
        self.gesehen = set()            # bereits verarbeitete ntfy-Antworten
        self.antwort_cursor = time.time()
        self.naechste_antwort = 0.0
        self.einstellungen = _start
        self.gestartet_um = None
        self.ueb_state = None           # letzter Zustand für die Fehlerüberwachung
        self.hms_gemeldet = set()
        self.selbst_gestoppt = -1e9
        self.aus_geplant = None         # Ausschalten nach Fehlschlag/Abbruch, sobald abgekühlt
        self.ausblick = {"grund": "leer"}   # nächster Auftrag: jetzt / fenster / nachtruhe / zu_lang / ohne_zeit / leer
        self.beschaeftigt = False       # gerade beim Starten (Einschalten, Plattenprüfung, Rückfrage)
        self.cfg_mtime = self._mtime()

    # --- Licht nach Druckzustand ---
    def licht_takt(self, st):
        if not st.get("connected"):
            self.letzter_state = None
            return
        state = (st.get("state") or "").upper()

        if state in DRUCK_STATES and not st.get("chamber_light"):
            licht(self.pid, True)

        # Kurz nach einem Start meldet der Drucker oft noch den Zustand des letzten Jobs
        # (FINISH/FAILED). Bis der neue Druck wirklich läuft: kein Licht-Aus.
        if self.gestartet_um is not None:
            if state in DRUCK_STATES:
                self.gestartet_um = None
            elif time.monotonic() - self.gestartet_um < 300:
                self.letzter_state = state
                return
            else:
                self.gestartet_um = None

        if state in ENDE_STATES and self.letzter_state in DRUCK_STATES:
            self.ende_seit = time.monotonic()
            self.licht_aus_erledigt = False
            log(f"Druck beendet ({state}) – Licht geht in {LICHT_AUS_NACH} s aus.")

        if (not self.licht_aus_erledigt and self.ende_seit is not None
                and state in ENDE_STATES | {"IDLE"}
                and time.monotonic() - self.ende_seit >= LICHT_AUS_NACH):
            if st.get("chamber_light"):
                licht(self.pid, False)
            self.licht_aus_erledigt = True

        self.letzter_state = state

    # --- Warteschlange / Nachtruhe ---
    def passt_ins_fenster(self, item):
        """(passt, fertig, zu_lang)"""
        jetzt = datetime.now(TZ)
        ab, bis = fenster(jetzt)
        dauer = item.get("print_time_seconds") or 0
        fertig = jetzt + timedelta(seconds=dauer, minutes=PUFFER_MIN)
        zu_lang = timedelta(seconds=dauer, minutes=PUFFER_MIN) > laengstes_fenster()
        return (ab <= jetzt < bis and fertig <= bis), fertig, zu_lang

    def abfangen(self):
        """Alles über den Druckwächter: Aufträge, die Bambuddy selbst starten würde, sofort auf
        „manuell starten“ stellen – dann startet nur der Wächter (Druckfenster, Plattenprüfung, Licht)."""
        if str(ENV.get("ALLES_UEBER_WAECHTER", "1")).strip().lower() in ("nein", "aus", "0", "false", "off"):
            return
        items = api("GET", "/queue/", params={"status": "pending"}) or []
        for item in items:
            if item.get("status") != "pending" or item.get("manual_start"):
                continue
            if item.get("printer_id") is not None and str(item.get("printer_id")) != str(self.pid):
                continue
            name = auftragsname(item)
            try:
                schalten(f"'{name}' auf „manuell starten“ gestellt – startet über den Druckwächter.",
                         "PATCH", f"/queue/{item['id']}", {"manual_start": True})
            except Exception as e:
                log(f"'{name}' konnte nicht auf „manuell starten“ gestellt werden: {e}", einmalig=True)
                continue
            if item["id"] not in self.abgefangen:
                self.abgefangen.add(item["id"])
                ereignis(item, "übernommen – startet über den Druckwächter (nicht direkt durch Bambuddy)")
            self.naechste_queue = 0.0     # gleich einplanen

    def queue_takt(self, st):
        state = (st.get("state") or "").upper() if st.get("connected") else "OFFLINE"
        if state in DRUCK_STATES:
            return

        items = api("GET", "/queue/", params={"printer_id": self.pid}) or []
        offen = [i for i in items if i.get("status") in ("pending", "printing")]
        if any(i["status"] == "printing" for i in offen):
            self.ausblick = {"grund": "jetzt"}
            return
        if any(i["status"] == "pending" and not i.get("manual_start") for i in offen):
            self.ausblick = {"grund": "jetzt"}   # Bambuddy startet selbst
            return

        wartend = sorted((i for i in offen if i.get("manual_start")),
                         key=lambda i: (i.get("position", 0), i["id"]))
        if not wartend:
            self.ausblick = {"grund": "leer"}
            return
        self.ausblick = {"grund": "zu_lang", "name": auftragsname(wartend[0])}   # wird unten genauer

        # 0. Geplant „trotz Druckfenster“ und Zeit erreicht → starten wie freigegeben
        pl = plaene()
        for item in wartend:
            um = geplant_um(item)
            if pl.get(item["id"], {}).get("force") and (um is None or datetime.now(TZ) >= um):
                name = auftragsname(item)
                _, fertig, _ = self.passt_ins_fenster(item)
                log(f"'{name}' ist geplant (trotz Druckfenster) – starte.")
                ereignis(item, "geplanter Start erreicht (trotz Druckfenster)")
                self.ausblick = {"grund": "jetzt", "name": name}
                self._starte_markiert(item, name, fertig, st, ohne_zeitpruefung=True)
                return

        # 1. Per Handy freigegebene Aufträge haben Vorrang – ohne Zeitprüfung
        for item in wartend:
            if item["id"] in self.freigegeben:
                self.freigegeben.discard(item["id"])
                name = auftragsname(item)
                _, fertig, _ = self.passt_ins_fenster(item)
                log(f"'{name}' wurde per Handy freigegeben – Zeitfenster wird ignoriert.")
                self.ausblick = {"grund": "jetzt", "name": name}
                self._starte_markiert(item, name, fertig, st, ohne_zeitpruefung=True)
                return

        jetzt = datetime.now(TZ)
        ab, bis = fenster(jetzt)
        warte_knopf = lambda iid: [(K_JETZT, f"jetzt:{iid}"), (K_ABBRECHEN, f"abbrechen:{iid}")]

        for item in wartend:
            name = auftragsname(item)
            um = geplant_um(item)
            if um and jetzt < um:                 # geplant: nicht vorher – die nächsten dürfen
                if self.ausblick.get("grund") in ("leer", "zu_lang"):
                    self.ausblick = {"grund": "geplant", "name": name, "start": f"{um:%d.%m. %H:%M}"}
                log(f"'{name}' ist für {um:%d.%m. %H:%M} geplant.", einmalig=True)
                continue
            dauer = item.get("print_time_seconds")
            if not dauer:
                log(f"'{name}': keine Druckzeit bekannt – bitte manuell starten.", einmalig=True)
                self.ausblick = {"grund": "ohne_zeit", "name": name}
                continue
            passt, fertig, zu_lang = self.passt_ins_fenster(item)

            # 2. Zu lange Drucke blockieren die Warteschlange nicht – nur melden
            if zu_lang:
                if item["id"] not in self.wartet_gemeldet:
                    self.wartet_gemeldet.add(item["id"])
                    ereignis(item, f"wartet auf Freigabe – {dauer/3600:.1f} h, passt in kein Druckfenster")
                    log(f"'{name}' ({dauer/3600:.1f} h) passt nie ins Druckfenster – warte auf Freigabe.")
                    melden("Druck zu lang – manuell starten?",
                           f"'{name}' dauert {dauer/3600:.1f} h und passt in kein Druckfenster. "
                           f"Jetzt gestartet wäre er fertig um ca. {fertig:%H:%M}.",
                           ["warning"], 4, aktionen=warte_knopf(item["id"]))
                continue

            # 3. Nachtruhe
            if not (ab <= jetzt < bis):
                self.ausblick = {"grund": "nachtruhe", "name": name, "start": naechster_start(jetzt)}
                if item["id"] not in self.wartet_gemeldet:
                    self.wartet_gemeldet.add(item["id"])
                    ereignis(item, f"wartet – Nachtruhe, Start {naechster_start(jetzt)}")
                    melden("Nachtruhe – Druck wartet",
                           f"'{name}' ({dauer/3600:.1f} h) startet {naechster_start(jetzt)}. "
                           f"Jetzt gestartet wäre er fertig um ca. {fertig:%H:%M}.",
                           ["zzz"], aktionen=warte_knopf(item["id"]))
                return

            if self.gesperrt_bis and jetzt < self.gesperrt_bis:
                self.ausblick = {"grund": "gesperrt", "name": name}
                return

            # 4. Nicht mehr bis Fensterende fertig -> bis morgen warten (Reihenfolge bleibt)
            if not passt:
                start_txt = naechster_start(jetzt)
                self.ausblick = {"grund": "fenster", "name": name, "start": start_txt}
                log(f"'{name}' ({dauer/3600:.1f} h) wäre erst {fertig:%H:%M} fertig – wartet bis {start_txt}.",
                    einmalig=True)
                if item["id"] not in self.wartet_gemeldet:
                    self.wartet_gemeldet.add(item["id"])
                    ereignis(item, f"wartet – wäre erst {fertig:%H:%M} fertig, Start {start_txt}")
                    melden("Druck wartet aufs nächste Druckfenster",
                           f"'{name}' ({dauer/3600:.1f} h) wäre erst {fertig:%H:%M} fertig "
                           f"(Druck bis {bis:%H:%M}). Start {start_txt}.",
                           ["zzz"], aktionen=warte_knopf(item["id"]))
                return

            self.ausblick = {"grund": "jetzt", "name": name}
            if DRY_RUN:
                if item["id"] in self.dry_gemeldet:
                    return
                self.dry_gemeldet.add(item["id"])
            self._starte_markiert(item, name, fertig, st)
            return

    # --- Antworten aus der ntfy-App (außerhalb der Plattenrückfrage) ---
    def antworten_pollen(self):
        if not NTFY_ANTWORT and not os.environ.get("PRINTHUB_INBOX"):
            return
        for mid, msg in EMPFAENGER.nimm():
            if mid in self.gesehen:
                continue
            self.gesehen.add(mid)
            aktion, _, iid = msg.partition(":")
            if not iid.isdigit():
                continue
            iid = int(iid)
            if aktion in ("fortsetzen", "druckstopp"):
                self.drucker_antwort(aktion)
                continue
            if aktion == "jetzt":
                log(f"Antwort: Auftrag #{iid} jetzt starten.")
                self.freigegeben.add(iid)
                self.naechste_queue = 0.0          # sofort verarbeiten
            elif aktion == "stoppen":
                try:
                    item = api("GET", f"/queue/{iid}") or {}
                except Exception:
                    item = {}
                name = auftragsname(item) if item else f"Auftrag #{iid}"
                if item.get("status") == "printing":
                    log(f"Antwort: '{name}' stoppen.")
                    try:
                        self.selbst_gestoppt = time.monotonic()
                        schalten(f"Druck '{name}' gestoppt (per Handy)", "POST", f"/queue/{iid}/stop")
                        ereignis(item, "Druck per Knopf gestoppt")
                        melden("Druck gestoppt", f"'{name}' wurde auf deinen Wunsch gestoppt.", ["stop_sign"])
                    except Exception as e:
                        log(f"Stoppen fehlgeschlagen: {e}")
                        melden("Stoppen fehlgeschlagen",
                               f"'{name}' konnte nicht gestoppt werden ({e}). Bitte in Bambuddy stoppen.",
                               ["warning"], 4)
                else:
                    log(f"Antwort: '{name}' stoppen – läuft aber nicht (Status {item.get('status')}).")
                    melden("Druck läuft nicht mehr",
                           f"'{name}' konnte nicht gestoppt werden – Status: {item.get('status') or 'unbekannt'}.",
                           ["information_source"])
            elif aktion == "abbrechen":
                try:
                    item = api("GET", f"/queue/{iid}") or {}
                except Exception:
                    item = {}
                if item.get("status") == "pending":
                    ereignis(item, "per Knopf aus der Warteschlange genommen")
                    schalten(f"Auftrag '{auftragsname(item)}' abgebrochen (per Handy)", "POST",
                             f"/queue/{iid}/cancel")
                    melden("Druck abgebrochen",
                           f"'{auftragsname(item)}' wurde auf deinen Wunsch abgebrochen.", ["x"])

    def _starte_markiert(self, *a, **k):
        """starte() kann lange dauern (Einschalten, Plattenprüfung, Rückfrage) – PrintHub soll dann nichts ausschalten."""
        self.beschaeftigt = True
        self._zustand()
        try:
            self.starte(*a, **k)
        finally:
            self.beschaeftigt = False
            self._zustand()

    def _zustand(self):
        zustand_schreiben(printer_id=self.pid, plug_id=self.plug, beschaeftigt=self.beschaeftigt,
                          ausblick=self.ausblick, dry_run=DRY_RUN)

    def drucker_aus(self, selbst_eingeschaltet):
        licht(self.pid, False)
        if selbst_eingeschaltet and self.plug is not None:
            schalten("Drucker ausschalten", "POST",
                     f"/smart-plugs/{self.plug}/control", body={"action": "off"})

    def starte(self, item, name, fertig, st, ohne_zeitpruefung=False):
        log(f"Freigabe für '{name}' – voraussichtlich fertig {fertig:%H:%M}.")
        self.debug_bild = None
        selbst_eingeschaltet = False

        # 1. Drucker einschalten, falls offline
        if not st.get("connected"):
            if self.plug is None:
                schalten(f"Starte '{name}'", "POST", f"/queue/{item['id']}/start")
                self.druck_gestartet()
                self.gestartet_melden(item, name, fertig)
                return
            schalten("Drucker einschalten (Smart Plug)", "POST",
                     f"/smart-plugs/{self.plug}/control", body={"action": "on"})
            selbst_eingeschaltet = True
            if DRY_RUN:
                log("[DRY RUN] würde jetzt warten, Licht einschalten, Platte prüfen und starten.")
                return
            t0 = time.monotonic()
            while time.monotonic() - t0 < BOOT_TIMEOUT_S:
                time.sleep(5)
                st = status(self.pid)
                if st.get("connected") and st.get("state"):
                    break
            else:
                log("Drucker kam nicht online – breche ab und schalte wieder aus.")
                ereignis(item, "nicht gestartet – Drucker kam nach dem Einschalten nicht online")
                melden("Drucker nicht erreichbar",
                       f"'{name}' konnte nicht starten: Der Drucker kam nach dem Einschalten "
                       f"nicht online. Neuer Versuch in {DRUCKER_AUS_SPERRE_MIN} min.",
                       ["rotating_light"], 4)
                self.gesperrt_bis = datetime.now(TZ) + timedelta(minutes=DRUCKER_AUS_SPERRE_MIN)
                schalten("Drucker ausschalten", "POST",
                         f"/smart-plugs/{self.plug}/control", body={"action": "off"})
                return
            log("Drucker ist online.")

        # 2. Licht an (für Kamera) – mit Bestätigung
        if not self.licht_sicher_an(st, frisch_gebootet=selbst_eingeschaltet):
            log("Licht ließ sich nicht einschalten – Plattenprüfung evtl. unzuverlässig.")

        # 3. Platte prüfen – mit Rückfrage per ntfy
        if PLATTE_PRUEFEN:
            ergebnis = self.platte_mit_rueckfrage(item, name, ohne_zeitpruefung)
            if ergebnis == "abbrechen":
                ereignis(item, "abgebrochen – Platte belegt bzw. keine Freigabe")
                schalten(f"Auftrag '{name}' abgebrochen", "POST", f"/queue/{item['id']}/cancel")
                self.drucker_aus(True)
                return
            if ergebnis == "zu_spaet":
                ereignis(item, "zurückgestellt – passt nicht mehr ins Druckfenster")
                self.drucker_aus(True)
                return

        # 4. Starten – Foto VOR dem Start holen (danach fährt das Bett los)
        bild = self.start_bild()
        _, fertig, _ = self.passt_ins_fenster(item)
        schalten(f"Starte '{name}'", "POST", f"/queue/{item['id']}/start")
        self.druck_gestartet()
        self.gesperrt_bis = None
        ereignis(item, "gestartet (Druckwächter)" + (" – per Knopf freigegeben" if ohne_zeitpruefung else "")
                 + (" · Platte geprüft" if PLATTE_PRUEFEN else ""))
        self.gestartet_melden(item, name, fertig, bild)

    def platte_mit_rueckfrage(self, item, name, ohne_zeitpruefung=False):
        """Gibt 'start', 'abbrechen' oder 'zu_spaet' zurück."""
        versuch = 0
        while True:
            versuch += 1
            frei = self.platte_frei(item)
            if frei is True or frei is None:
                return "start"
            if frei == "fehler" and PLATTE_BEI_FEHLER == "starten":
                return "start"
            grund = "Kamera liefert kein Bild" if frei == "fehler" else "Etwas liegt auf der Platte"

            if DRY_RUN:
                log(f"[DRY RUN] {grund} – würde melden und {PLATTE_WARTE_MIN} min auf Antwort warten.")
                return "abbrechen" if versuch > PLATTE_VERSUCHE else "start"

            if versuch > PLATTE_VERSUCHE:
                log(f"{grund} – {versuch}. Prüfung erfolglos, Auftrag wird abgebrochen.")
                melden("Druck abgebrochen",
                       f"'{name}': {grund} – auch nach {versuch} Prüfungen. "
                       f"Auftrag wurde abgebrochen, Drucker ist aus.", ["x"], 4,
                       bild=self.bild_fuer_meldung(frei))
                return "abbrechen"

            log(f"{grund} – Hinweis {versuch}/{PLATTE_VERSUCHE}, warte {PLATTE_WARTE_MIN} min auf Antwort.")
            seit = time.time() - 2
            melden(f"Druck wartet: {grund}",
                   f"'{name}' startet nicht (Hinweis {versuch}/{PLATTE_VERSUCHE}). "
                   + (f"Ohne Antwort neue Prüfung in {PLATTE_WARTE_MIN} min."
                      if versuch < PLATTE_VERSUCHE else
                      f"Letzter Hinweis: Ist die Platte in {PLATTE_WARTE_MIN} min noch belegt, "
                      f"wird der Auftrag abgebrochen."),
                   ["no_entry"], 4, bild=self.bild_fuer_meldung(frei),
                   aktionen=[(K_PRUEFEN, f"pruefen:{item['id']}"),
                             (K_TROTZDEM, f"starten:{item['id']}"),
                             (K_ABBRECHEN, f"abbrechen:{item['id']}")])

            antwort = self.warte_auf_antwort(item["id"], seit, PLATTE_WARTE_MIN * 60)
            if antwort == "abbrechen":
                log("Antwort: Abbrechen.")
                melden("Druck abgebrochen", f"'{name}' wurde auf deinen Wunsch abgebrochen.", ["x"])
                return "abbrechen"
            if antwort == "starten":
                log("Antwort: Trotzdem drucken – Plattenprüfung übersprungen.")
                if not ohne_zeitpruefung and not self.passt_ins_fenster(item)[0]:
                    return self.zu_spaet(item, name)
                return "start"
            if antwort == "pruefen":
                log("Antwort: Platte frei – prüfe sofort erneut.")
            # sonst: Zeit abgelaufen -> nächste Prüfung

            if not ohne_zeitpruefung and not self.passt_ins_fenster(item)[0]:
                return self.zu_spaet(item, name)
            # Licht ist evtl. inzwischen aus (z. B. manuell) – sicherstellen
            self.licht_sicher_an(status(self.pid))

    def bild_fuer_meldung(self, frei):
        if not KAMERA_BILD or frei == "fehler":     # ohne Kamerabild gibt's auch kein Foto
            return None
        bild = kamerabild(self.pid, getattr(self, "debug_bild", None))
        if not bild:
            log("Kein Kamerabild für die Meldung verfügbar – sende ohne Bild.")
        return bild

    def zu_spaet(self, item, name):
        jetzt = datetime.now(TZ)
        _, bis = fenster(jetzt)
        start_txt = naechster_start(jetzt)
        log(f"'{name}' passt inzwischen nicht mehr bis {bis:%H:%M} – wartet bis {start_txt}.")
        self.wartet_gemeldet.add(item["id"])
        melden("Druck wartet aufs nächste Druckfenster",
               f"'{name}' würde jetzt nicht mehr bis {bis:%H:%M} fertig. Start {start_txt}.",
               ["zzz"], aktionen=[(K_JETZT, f"jetzt:{item['id']}"),
                                  (K_ABBRECHEN, f"abbrechen:{item['id']}")])
        return "zu_spaet"

    def warte_auf_antwort(self, item_id, seit, sekunden):
        ende = time.monotonic() + sekunden
        while time.monotonic() < ende:
            passend = lambda m: (m.partition(":")[2] == str(item_id)
                                 and m.partition(":")[0] in ("pruefen", "starten", "abbrechen"))
            for mid, msg in EMPFAENGER.nimm(passend):
                if mid in self.gesehen:
                    continue
                self.gesehen.add(mid)
                return msg.partition(":")[0]
            if int(time.monotonic()) % 60 < 2:      # PrintHub regelmäßig zeigen: wir warten noch
                self._zustand()
            time.sleep(2)
        return None

    def licht_sicher_an(self, st, frisch_gebootet=False):
        """Licht einschalten und warten, bis der Drucker es bestätigt."""
        if st.get("chamber_light"):
            return True
        if DRY_RUN:
            licht(self.pid, True)
            return True
        if frisch_gebootet:
            time.sleep(LICHT_BOOT_WARTEN)
        for versuch in range(4):
            licht(self.pid, True)
            for _ in range(5):
                time.sleep(2)
                if status(self.pid).get("chamber_light"):
                    time.sleep(KAMERA_EINREGELN)
                    return True
            if versuch < 3:
                log(f"Licht noch aus – Versuch {versuch + 2}")
        return False

    def druck_gestartet(self):
        """Alten Licht-Aus-Timer verwerfen – ein neuer Druck läuft an."""
        self.gestartet_um = time.monotonic()
        self.ende_seit = None
        self.licht_aus_erledigt = True
        self.aus_geplant = None

    def start_bild(self):
        if not KAMERA_BILD or DRY_RUN:
            return None
        return kamerabild(self.pid, getattr(self, "debug_bild", None))

    def gestartet_melden(self, item, name, fertig, bild=None):
        dauer = (item.get("print_time_seconds") or 0) / 3600
        text = f"'{name}' ({dauer:.1f} h) – voraussichtlich fertig um {fertig:%H:%M}."
        if bild:
            text += " Foto: Platte direkt vor dem Start."
        melden("Druck startet", text, ["printer"], bild=bild,
               aktionen=[(K_STOPPEN, f"stoppen:{item['id']}")])

    def platte_frei(self, item):
        """True = leer, False = belegt, 'fehler' = kein Kamerabild, None = nicht kalibriert."""
        params = {"include_debug_image": "true"}
        if item.get("bed_type"):
            params["plate_type"] = item["bed_type"]
        self.debug_bild = None
        r, kamera_fehler = {}, True
        for versuch in range(3):
            try:
                r = self._check_plate(params)
            except Exception as e:
                r = {"message": f"Fehler: {e}"}
            msg = str(r.get("message") or "").lower()
            kamera_fehler = ("is_empty" not in r or "fail" in msg or "error" in msg
                             or "fehler" in msg or "could not" in msg or "unable" in msg)
            if not kamera_fehler or r.get("needs_calibration"):
                break
            log(f"Plattenprüfung Versuch {versuch + 1}: {r.get('message')}")
            if not DRY_RUN:
                time.sleep(10)
        if r.get("needs_calibration"):
            log("Plattenerkennung nicht kalibriert – Prüfung übersprungen.", einmalig=True)
            return None
        if kamera_fehler:
            return "fehler"
        log(f"Plattenprüfung: {r.get('message')} (Abweichung {r.get('difference_percent')} %)")
        self.debug_bild = self._debug_url(r)
        if not self._leer(r) and not DRY_RUN:
            time.sleep(KAMERA_EINREGELN)    # Zweitprüfung gegen Belichtungs-Fehlalarme
            try:
                r2 = self._check_plate(params)
                log(f"Plattenprüfung (2. Blick): {r2.get('message')} "
                    f"(Abweichung {r2.get('difference_percent')} %)")
                if "is_empty" in r2 and "fail" not in str(r2.get("message", "")).lower():
                    self.debug_bild = self._debug_url(r2) or self.debug_bild
                    return self._leer(r2)
            except Exception as e:
                log(f"Zweitprüfung fehlgeschlagen: {e}")
        return self._leer(r)

    def _check_plate(self, params):
        """Plattenprüfung: in PrintHub (eigene Referenzbilder) oder über Bambuddy."""
        ph = os.environ.get("PRINTHUB_URL")
        if PLATTE_QUELLE == "printhub" and ph and os.environ.get("PRINTHUB_PLATE_PID"):
            q = {"plate_type": params.get("plate_type", "")}   # Schwelle: die aus PrintHub
            url = f"{ph}/api/plate/{os.environ['PRINTHUB_PLATE_PID']}/check?" + urllib.parse.urlencode(q)
            req = urllib.request.Request(url)
            req.add_header("X-PrintHub-Token", os.environ.get("PRINTHUB_TOKEN", ""))
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    return json.loads(e.read() or b"{}")
                except Exception:
                    return {"message": f"Fehler: PrintHub {e.code}"}
        return api("GET", f"/printers/{self.pid}/camera/check-plate", params=params) or {}

    @staticmethod
    def _leer(r):
        """Eigene Schwelle (PLATTE_SCHWELLE in %) statt Bambuddys Urteil, falls gesetzt."""
        diff = r.get("difference_percent")
        if r.get("source") == "printhub":       # PrintHub hat seine eigene Schwelle schon angewendet
            return bool(r.get("is_empty"))
        if PLATTE_SCHWELLE is not None and isinstance(diff, (int, float)):
            leer = diff <= PLATTE_SCHWELLE
            if leer != bool(r.get("is_empty")):
                log(f"Abweichung {diff:.1f} % – mit eigener Schwelle {PLATTE_SCHWELLE:g} % gilt die Platte als "
                    f"{'leer' if leer else 'belegt'}.")
            return leer
        return bool(r.get("is_empty"))

    @staticmethod
    def _debug_url(r):
        """Feld mit dem Analysebild in der Antwort finden (Name je nach Version)."""
        for k, v in (r or {}).items():
            if isinstance(v, str) and "debug" in k.lower() and (
                    v.startswith("/") or v.startswith("http") or v.startswith("data:image")):
                return v
        return None

    # --- Erreichbarkeit von Bambuddy ---
    def erreichbar(self):
        if self.offline_gemeldet:
            melden("Bambuddy wieder erreichbar", "Die Verbindung zu Bambuddy steht wieder.",
                   ["white_check_mark"])
            log("Bambuddy wieder erreichbar.")
        self.offline_seit = None
        self.offline_gemeldet = False

    def unerreichbar(self, e):
        if self.offline_seit is None:
            self.offline_seit = time.monotonic()
        log(f"Bambuddy nicht erreichbar: {e}", einmalig=True)
        if (not self.offline_gemeldet
                and time.monotonic() - self.offline_seit >= BAMBUDDY_ALARM_MIN * 60):
            self.offline_gemeldet = True
            melden("Bambuddy nicht erreichbar",
                   f"Seit über {BAMBUDDY_ALARM_MIN} min keine Verbindung zu {URL}. "
                   f"Warteschlange und Nachtruhe sind solange außer Betrieb.",
                   ["rotating_light"], 4)

    # --- Fehlerüberwachung (Pause durch Gadget/Drucker, Fehlschlag, HMS) ---
    def ueberwachung(self, st):
        if not st.get("connected"):
            self.ueb_state = None
            return
        state = (st.get("state") or "").upper()
        alt = self.ueb_state
        self.ueb_state = state
        name = st.get("subtask_name") or st.get("current_print") or "Aktueller Druck"

        # HMS-Fehler des Druckers (je Code einmal)
        for e in st.get("hms_errors") or []:
            code = str(e.get("full_code") or e.get("code") or e)
            if code in self.hms_gemeldet:
                continue
            self.hms_gemeldet.add(code)
            text = e.get("description") or f"Fehlercode {code}"
            schwer = e.get("severity") in (1, 2, "fatal", "serious")
            log(f"HMS-Meldung: {text} ({code})")
            melden("Drucker meldet einen Fehler",
                   f"'{name}': {text} (HMS {code})", ["warning"], 5 if schwer else 4,
                   bild=self.fehler_bild())
        if not st.get("hms_errors"):
            self.hms_gemeldet.clear()

        if alt is None or alt == state:
            return

        # Druck wurde pausiert (Gadget, Drucker, Bambuddy oder von Hand)
        if state == "PAUSE" and alt in ("RUNNING", "PREPARE"):
            grund = ""
            if st.get("hms_errors"):
                grund = " Grund laut Drucker: " + "; ".join(
                    str(e.get("description") or e.get("full_code")) for e in st["hms_errors"])
            log(f"Druck pausiert: '{name}'.{grund}")
            melden("Druck pausiert",
                   f"'{name}' wurde angehalten – evtl. hat die KI (OctoEverywhere Gadget) einen Fehler "
                   f"erkannt.{grund} Bitte Foto prüfen.",
                   ["pause_button"], 5, bild=self.fehler_bild(),
                   aktionen=[(K_FORTSETZEN, f"fortsetzen:{self.pid}"),
                             (K_STOPPEN, f"druckstopp:{self.pid}")])

        # Druck fehlgeschlagen / abgebrochen (aber nicht, wenn du selbst gestoppt hast)
        elif state == "FAILED" and alt in DRUCK_STATES:
            if _ph_setting("power_auto"):
                log("Druck abgebrochen/fehlgeschlagen – Ausschalten übernimmt PrintHub (Strom-Verwaltung).")
            elif AUS_BEI_FEHLER and self.plug is not None:
                self.aus_geplant = time.monotonic()
                log(f"Druck abgebrochen/fehlgeschlagen – Drucker geht aus, sobald die Düse unter {AUS_TEMP} °C ist.")
            if time.monotonic() - self.selbst_gestoppt < 300:
                return
            log(f"Druck fehlgeschlagen: '{name}'.")
            melden("Druck fehlgeschlagen",
                   f"'{name}' wurde abgebrochen bzw. ist fehlgeschlagen (z. B. durch OctoEverywhere, "
                   f"den Drucker oder in Bambuddy).", ["x"], 4, bild=self.fehler_bild())

    @staticmethod
    def duesentemp(st):
        """Aktuelle Düsentemperatur aus dem Status (Feldnamen je nach Version verschieden)."""
        t = st.get("temperatures") or {}
        if not isinstance(t, dict):
            return None
        for k, v in t.items():
            kl = k.lower()
            if ("nozzle" in kl or "hotend" in kl or "extruder" in kl) and "target" not in kl:
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, dict):
                    for kk in ("current", "temp", "actual", "value"):
                        if isinstance(v.get(kk), (int, float)):
                            return float(v[kk])
        for n in st.get("nozzles") or []:          # Fallback: Liste der Düsen
            if isinstance(n, dict):
                for kk in ("temp", "current_temp", "temperature"):
                    if isinstance(n.get(kk), (int, float)):
                        return float(n[kk])
        return None

    def aus_takt(self, st):
        """Nach Fehlschlag/Abbruch: Drucker per Plug aus, sobald abgekühlt."""
        if self.aus_geplant is None:
            return
        if not st.get("connected"):
            self.aus_geplant = None                 # schon aus (z. B. Bambuddy oder von Hand)
            return
        state = (st.get("state") or "").upper()
        if state in DRUCK_STATES:
            log("Neuer Druck läuft – Ausschalten nach Fehlschlag entfällt.")
            self.aus_geplant = None
            return
        temp = self.duesentemp(st)
        wartezeit = time.monotonic() - self.aus_geplant
        if temp is None:
            if wartezeit < 600:                     # keine Temperatur lesbar -> 10 min warten
                return
            grund = "nach 10 min (Temperatur nicht lesbar)"
        elif temp >= AUS_TEMP:
            if wartezeit > 3600:                    # Notbremse: nach 1 h trotzdem aus
                grund = f"nach 1 h (Düse noch {temp:.0f} °C)"
            else:
                return
        else:
            grund = f"Düse {temp:.0f} °C"
        # Wartet schon ein freigegebener Auftrag? Dann anlassen.
        try:
            items = api("GET", "/queue/", params={"printer_id": self.pid}) or []
            if any(i.get("status") == "printing" or
                   (i.get("status") == "pending" and not i.get("manual_start")) for i in items):
                log("Nächster Auftrag steht bereit – Drucker bleibt an.")
                self.aus_geplant = None
                return
        except Exception:
            pass
        self.aus_geplant = None
        if st.get("chamber_light"):
            licht(self.pid, False)
        schalten(f"Drucker ausschalten nach Fehlschlag/Abbruch ({grund})", "POST",
                 f"/smart-plugs/{self.plug}/control", body={"action": "off"})
        melden("Drucker ausgeschaltet",
               f"Nach dem abgebrochenen/fehlgeschlagenen Druck abgekühlt ({grund}) – Drucker ist aus.",
               ["electric_plug"])

    def fehler_bild(self):
        if not KAMERA_BILD or DRY_RUN:
            return None
        return kamerabild(self.pid)

    def drucker_antwort(self, aktion):
        st = status(self.pid)
        state = (st.get("state") or "").upper()
        name = st.get("subtask_name") or st.get("current_print") or "Druck"
        try:
            if aktion == "fortsetzen":
                if state != "PAUSE":
                    melden("Fortsetzen nicht möglich", f"'{name}' ist nicht pausiert (Status {state}).",
                           ["information_source"])
                    return
                schalten(f"Druck '{name}' fortgesetzt (per Handy)", "POST", f"/printers/{self.pid}/print/resume")
                melden("Druck läuft weiter", f"'{name}' wird fortgesetzt.", ["arrow_forward"])
            else:
                if state not in DRUCK_STATES:
                    melden("Druck läuft nicht mehr", f"'{name}' – Status {state}.", ["information_source"])
                    return
                self.selbst_gestoppt = time.monotonic()
                schalten(f"Druck '{name}' gestoppt (per Handy)", "POST", f"/printers/{self.pid}/print/stop")
                melden("Druck gestoppt", f"'{name}' wurde auf deinen Wunsch gestoppt.", ["stop_button"])
        except Exception as e:
            log(f"{aktion} fehlgeschlagen: {e}")
            melden("Aktion fehlgeschlagen", f"{aktion} für '{name}' ging nicht ({e}). Bitte in Bambuddy prüfen.",
                   ["warning"], 4)

    # --- config.env beobachten ---
    @staticmethod
    def _mtime():
        try:
            return os.path.getmtime(CONFIG_FILE)
        except OSError:
            return None

    def config_pruefen(self):
        mt = self._mtime()
        if mt is None or mt == self.cfg_mtime:
            return
        if time.time() - mt < 2:          # Editor schreibt evtl. noch
            return
        self.cfg_mtime = mt
        try:
            neu = baue_einstellungen(lies_datei(CONFIG_FILE))
        except (ValueError, OSError) as fehler:
            log(f"config.env fehlerhaft: {fehler} – alte Einstellungen bleiben aktiv.")
            melden("Fehler in config.env",
                   f"{fehler}\nDie bisherigen Einstellungen bleiben aktiv, bis der Fehler behoben ist.",
                   ["warning"], 4)
            return

        alt_env, neu_env = self.einstellungen["ENV"], neu["ENV"]
        aenderungen = []
        for k in sorted(set(alt_env) | set(neu_env)):
            if k in _UMGEBUNG and alt_env.get(k) == neu_env.get(k):
                continue
            a, b = alt_env.get(k), neu_env.get(k)
            if a == b:
                continue
            if k in GEHEIM:
                aenderungen.append(f"{k} geändert")
            else:
                aenderungen.append(f"{k}: {a if a is not None else '–'} → {b if b is not None else '–'}")
        if not aenderungen:
            log("config.env gespeichert – keine Änderungen.")
            return

        wende_an(neu)
        self.einstellungen = neu
        self.wartet_gemeldet.clear()       # wartende Aufträge mit neuen Zeiten neu bewerten/melden
        self.naechste_queue = 0.0
        neustart = [k for k in ("PRINTER_ID", "PLUG_ID", "TZ_NAME")
                    if alt_env.get(k) != neu_env.get(k)]
        for z in aenderungen:
            log(f"Einstellung geändert: {z}")
        text = "\n".join(aenderungen) + f"\n\nDruckfenster: {wochen_text()}"
        if neustart:
            text += f"\n\n⚠ {', '.join(neustart)} wirkt erst nach Neustart des Containers."
        melden("Einstellungen übernommen", text, ["gear"])

    # --- Hauptschleife ---
    def lauf(self):
        while True:
            try:
                self.config_pruefen()
                st = status(self.pid)
                self.erreichbar()
                self.ueberwachung(st)
                self.aus_takt(st)
                self.licht_takt(st)
                if time.monotonic() >= self.naechste_antwort:
                    self.naechste_antwort = time.monotonic() + 2
                    self.antworten_pollen()
                if time.monotonic() >= self.naechste_abfang:
                    self.naechste_abfang = time.monotonic() + 4
                    self.abfangen()
                if time.monotonic() >= self.naechste_queue:
                    self.naechste_queue = time.monotonic() + QUEUE_TAKT_S
                    self.queue_takt(st)
                self._zustand()
            except urllib.error.HTTPError as e:
                log(f"API-Fehler {e.code} bei {e.url}: {e.read()[:200]!r}", einmalig=True)
            except Exception as e:
                if nicht_erreichbar(e):
                    self.unerreichbar(e)
                else:
                    log(f"Fehler: {e}", einmalig=True)
            time.sleep(TAKT_S)


def main():
    if not API_KEY:
        raise SystemExit("BAMBUDDY_API_KEY fehlt (config.env prüfen).")
    log(f"Konfiguration: {CONFIG_FILE if CONFIG_GELADEN else 'nur Umgebungsvariablen'}")

    # Beim Hochfahren des Servers startet Bambuddy evtl. später – geduldig warten
    wartezeit = 0
    while True:
        try:
            pid = ermittle_drucker()
            break
        except urllib.error.HTTPError as e:
            raise SystemExit(f"API-Fehler {e.code} – URL/Key/Rechte prüfen.")
        except Exception as e:
            if wartezeit == 0:
                log(f"Bambuddy noch nicht erreichbar ({e}) – warte …")
            time.sleep(15)
            wartezeit += 15
    plug = ermittle_plug(pid)
    EMPFAENGER.start()
    log(f"Druckfenster: {wochen_text()}")
    log(f"Wächter aktiv: Puffer {PUFFER_MIN} min, "
        f"Platte: {PLATTE_VERSUCHE} Prüfungen à {PLATTE_WARTE_MIN} min, "
        f"ntfy {'an' if NTFY_TOPIC else 'aus'}, DRY_RUN={'an' if DRY_RUN else 'aus'}")
    Waechter(pid, plug).lauf()


if __name__ == "__main__":
    main()
