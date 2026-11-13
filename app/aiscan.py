"""TEST: Etikett mit einer Bild-KI lesen (Google Gemini, kostenloses Kontingent).

Ganz entfernbar: diese Datei löschen, den Block „KI-Test“ in main._scan, die ai_*-Einstellungen
in settings.py und die Karte „KI-Etiketterkennung (Test)“ in settings.html.
"""
import base64
import io
import json
import logging
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
 "sku": Artikelnummer oder null, "batch": Chargen-/Batch-Nummer oder null}"""


def _jpeg(img, maxpx=1600):
    im = img.convert("RGB")
    im.thumbnail((maxpx, maxpx))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


def read(img, key, model="gemini-flash-lite-latest", timeout=20):
    """→ (dict mit Etikett-Werten oder None, Sekunden, Fehlertext oder None)"""
    t = time.time()
    body = {
        "contents": [{"parts": [{"text": PROMPT},
                                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(_jpeg(img)).decode()}}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    try:
        for attempt in (1, 2):                 # Google kurz überlastet (5xx) oder Verbindung weg → einmal neu versuchen
            try:
                r = requests.post(URL.format(model=model or "gemini-flash-lite-latest"), json=body, timeout=timeout,
                                  headers={"x-goog-api-key": key})
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(0.5)
                continue
            if r.status_code < 500 or attempt == 2:
                break
            time.sleep(0.5)
        if r.status_code >= 500:
            return None, time.time() - t, f"Google gerade überlastet/gestört ({r.status_code}) – eigene Erkennung verwendet"
        if r.status_code == 429:
            return None, time.time() - t, "Gratis-Kontingent für heute aufgebraucht"
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
    for k in ("weight", "diameter", "extruder_temp", "bed_temp"):
        try:
            v = float(str(d.get(k)).replace(",", ".").rstrip("gm°C ").strip())
            out[k] = v if k == "diameter" else int(v)
        except (TypeError, ValueError):
            pass
    for k in ("brand", "color"):              # Etiketten in GROSSBUCHSTABEN → „Matte Fossil Grey“
        if out.get(k) and out[k].isupper() and len(out[k]) > 4:
            out[k] = " ".join(w.capitalize() for w in out[k].split())
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
