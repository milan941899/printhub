"""Etikett mit einer Bild-KI lesen (Google Gemini, kostenloses Kontingent) – optional, in den Einstellungen abschaltbar."""
import base64
import io
import json
import logging
import re
import threading
import time

import requests

log = logging.getLogger("printhub")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PROMPT = """Das Foto zeigt das Etikett einer 3D-Drucker-Filamentrolle (oder deren Karton).
Lies es und antworte NUR mit JSON in dieser Form (fehlende Werte = null, nichts erfinden):
{"brand": Hersteller, "material": z.B. "PLA", "PLA+", "PETG", "ABS", "TPU",
 "color": Farbname genau wie aufgedruckt (z.B. "Matte Fossil Grey", "Brick Red"),
 "color_hex": aufgedruckter Farbcode ohne # oder null,
 "weight": Nettogewicht in Gramm (Zahl), "diameter": mm (Zahl),
 "extruder_temp": empfohlene Düsentemperatur Mitte in °C (Zahl), "bed_temp": Bett °C (Zahl),
 "sku": Artikelnummer oder null, "batch": Chargen-/Batch-Nummer oder null,
 "model": Modellnummer hinter „Model:“ (z.B. "A01-K1-1.75-1000-SPL") oder null,
 "filament_code": Nummer hinter „Filament Code:“ oder null,
 "spool_weight": Gewicht der leeren Spule in Gramm („Empty Weight“, „Spool Weight“) oder null}
Lies nur das Etikett der vorderen/größten Rolle, nicht Etiketten im Hintergrund."""


MAXPX = 1280          # lange Seite fürs KI-Bild: Etikettschrift bleibt lesbar, Google rechnet weniger Bildkacheln (schneller)


def _jpeg(img, maxpx=MAXPX, info=None):
    im = img.convert("RGB")
    im.thumbnail((maxpx, maxpx))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    if info is not None:
        info["size"] = f"{im.width}×{im.height}"
        info["kb"] = round(len(b.getvalue()) / 1024)
    return b.getvalue()


_OPEN = []                    # laufende KI-Anfragen (ihre info-Dicts) – Stapel schickt bis zu 2 gleichzeitig
_OPEN_LOCK = threading.Lock()


_PAUSE = {"until": 0, "why": ""}
_COUNT = {"day": "", "n": 0, "err": 0}


def _gday():
    """Kalendertag bei Google (Kontingent wird um Mitternacht Pazifikzeit zurückgesetzt)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def _count(ok):
    d = _gday()
    if _COUNT["day"] != d:
        _COUNT.update(day=d, n=0, err=0)
    _COUNT["n"] += 1
    if not ok:
        _COUNT["err"] += 1


def today():
    """Anfragen heute (Google-Tag): {n, err, paused}"""
    if _COUNT["day"] != _gday():
        return {"n": 0, "err": 0, "paused": paused()}
    return {"n": _COUNT["n"], "err": _COUNT["err"], "paused": paused()}


def paused():
    """Tageskontingent aufgebraucht → bis zum Rücksetzen (Mitternacht Pazifikzeit) gar nicht erst fragen."""
    return _PAUSE["why"] if time.time() < _PAUSE["until"] else ""


def _next_reset():
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp() + 60
    except Exception:
        return time.time() + 6 * 3600


def _quota(r):
    """429 auswerten: (Tageslimit?, Wartezeit in s)"""
    try:
        e = r.json().get("error") or {}
    except ValueError:
        return False, 0
    txt = json.dumps(e)
    wait = 0
    m = re.search(r'"retryDelay":\s*"(\d+)(?:\.\d+)?s"', txt)
    if m:
        wait = int(m.group(1))
    return ("PerDay" in txt or "per day" in txt.lower()), wait


def read(img, key, model="gemini-flash-lite-latest", timeout=20, info=None):
    """→ (dict mit Etikett-Werten oder None, Sekunden, Fehlertext oder None)
    info (optional): wird gefüllt mit size, kb, open (KI-Anfragen gleichzeitig offen), retries – fürs Protokoll"""
    if info is None:
        info = {}
    if paused():
        return None, 0, paused()
    with _OPEN_LOCK:
        _OPEN.append(info)
        for i in _OPEN:                       # höchste Zahl gleichzeitig offener Anfragen während dieser Anfrage
            i["open"] = max(i.get("open", 0), len(_OPEN))
    try:
        return _read(img, key, model, timeout, info)
    finally:
        with _OPEN_LOCK:
            _OPEN.remove(info)


def _read(img, key, model, timeout, info):
    t = time.time()
    info["retries"] = 0
    body = {
        "contents": [{"parts": [{"text": PROMPT},
                                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(_jpeg(img, info=info)).decode()}}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    try:
        for attempt in (1, 2):                 # Google kurz überlastet (5xx) oder Verbindung weg → einmal neu versuchen
            try:
                r = requests.post(URL.format(model=model or "gemini-flash-lite-latest"), json=body, timeout=timeout,
                                  headers={"x-goog-api-key": key})
                _count(r.status_code == 200)
            except requests.RequestException:
                if attempt == 2:
                    raise
                info["retries"] += 1
                time.sleep(0.5)
                continue
            if r.status_code == 429 and attempt == 1:
                day, wait = _quota(r)
                if not day and wait <= 30:               # Minutenlimit: Google sagt, wie lange warten → einmal nachholen
                    info["retries"] += 1
                    info["waited"] = max(wait, 2)
                    time.sleep(max(wait, 2))
                    continue
            if r.status_code < 500 or attempt == 2:
                break
            info["retries"] += 1
            time.sleep(0.5)
        if r.status_code >= 500:
            return None, time.time() - t, f"Google gerade überlastet/gestört ({r.status_code}) – eigene Erkennung verwendet"
        if r.status_code == 429:
            day, wait = _quota(r)
            if day:
                _PAUSE.update(until=_next_reset(), why="Gratis-Tageskontingent aufgebraucht – KI pausiert bis morgen 9 Uhr, nur Tower-Erkennung")
                log.info("KI: Tageskontingent aufgebraucht – pausiert bis zum Rücksetzen")
                return None, time.time() - t, _PAUSE["why"]
            return None, time.time() - t, f"Gratis-Minutenlimit erreicht – kurz warten ({wait or '?'} s), nur Tower-Erkennung"
        if r.status_code in (400, 401, 403):
            msg = (r.json().get("error") or {}).get("message", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            return None, time.time() - t, f"Schlüssel/Modell abgelehnt ({r.status_code}) {msg[:120]}"
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        d = json.loads(txt)
        if isinstance(d, list):
            d = d[0] if d else {}
    except requests.RequestException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        return None, time.time() - t, f"nicht erreichbar ({code or type(e).__name__})"
    except (ValueError, KeyError, IndexError) as e:
        return None, time.time() - t, f"Antwort unverständlich ({type(e).__name__})"
    out = {}
    for k in ("brand", "material", "color", "sku", "batch"):
        if isinstance(d.get(k), str) and d[k].strip() and d[k].strip().lower() not in ("null", "none", "unknown"):
            out[k] = d[k].strip()
    for k in ("weight", "diameter", "extruder_temp", "bed_temp", "spool_weight"):
        try:
            v = float(str(d.get(k)).replace(",", ".").rstrip("gm°C ").strip())
            out[k] = v if k == "diameter" else int(v)
        except (TypeError, ValueError):
            pass
    for k in ("brand", "color"):              # Etiketten in GROSSBUCHSTABEN → „Matte Fossil Grey“
        if out.get(k) and out[k].isupper() and len(out[k]) > 4:
            out[k] = " ".join(w.capitalize() for w in out[k].split())
    mo = str(d.get("model") or "").strip().upper().replace(" ", "")
    if re.fullmatch(r"[A-Z][A-Z0-9]{1,3}-[A-Z0-9]{1,3}-1\.75-\d{3,4}-[A-Z]{2,8}", mo):   # Bambu-Modellnummer, pro Farbe eindeutig
        sk = mo.split("-")
        sk[0] = sk[0][0] + sk[0][1:].replace("O", "0")
        out["model"] = "-".join(sk)
    fc = re.sub(r"\s", "", str(d.get("filament_code") or ""))
    if re.fullmatch(r"\d{5}", fc):
        out["filament_code"] = fc
    hx = str(d.get("color_hex") or "").lstrip("#").upper()
    if len(hx) == 6 and all(c in "0123456789ABCDEF" for c in hx):
        out["hex_print"] = hx
    if out.get("material"):
        out["material"] = out["material"].upper().replace(" PLUS", "+").replace("PLUS", "+").replace(" ", "")
    return out, time.time() - t, None


def as_text(d):
    """KI-Werte als Etikett-Text, damit der übliche Abgleich (eigene Sorten, SpoolmanDB) greift."""
    return "\n".join(str(x) for x in [d.get("brand"), d.get("material"), d.get("color"),
                                      f"{d['weight']}g" if d.get("weight") else None] if x)
