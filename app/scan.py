"""Rolle per Foto erkennen: Strichcode (EAN / Charge) + Text auf dem Etikett (Material, Farbe, Gewicht …).

Strichcodes über libzbar (ctypes, kein Python-Paket nötig), Text über das Programm „tesseract“.
Beides läuft lokal auf dem Server – nur das optionale Nachschlagen eines EAN geht ins Internet (UPCitemdb).
"""
import ctypes
import ctypes.util
import io
import logging
import re
import subprocess
import time

from PIL import Image, ImageFilter, ImageOps

from . import colors

log = logging.getLogger("printhub.scan")

# ---------------------------------------------------------------- Strichcode
_zbar = None


def _lib():
    global _zbar
    if _zbar is None:
        name = ctypes.util.find_library("zbar") or "libzbar.so.0"
        try:
            z = ctypes.cdll.LoadLibrary(name)
        except OSError:
            _zbar = False
            return None
        z.zbar_image_scanner_create.restype = ctypes.c_void_p
        z.zbar_image_scanner_set_config.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        z.zbar_image_create.restype = ctypes.c_void_p
        z.zbar_image_set_format.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        z.zbar_image_set_size.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        z.zbar_image_set_data.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        z.zbar_scan_image.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        z.zbar_image_first_symbol.argtypes = [ctypes.c_void_p]
        z.zbar_image_first_symbol.restype = ctypes.c_void_p
        z.zbar_symbol_next.argtypes = [ctypes.c_void_p]
        z.zbar_symbol_next.restype = ctypes.c_void_p
        z.zbar_symbol_get_data.argtypes = [ctypes.c_void_p]
        z.zbar_symbol_get_data.restype = ctypes.c_char_p
        z.zbar_symbol_get_type.argtypes = [ctypes.c_void_p]
        z.zbar_symbol_get_loc_size.argtypes = [ctypes.c_void_p]
        z.zbar_symbol_get_loc_x.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        z.zbar_symbol_get_loc_y.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        z.zbar_get_symbol_name.argtypes = [ctypes.c_int]
        z.zbar_get_symbol_name.restype = ctypes.c_char_p
        z.zbar_image_destroy.argtypes = [ctypes.c_void_p]
        z.zbar_image_scanner_destroy.argtypes = [ctypes.c_void_p]
        _zbar = z
    return _zbar or None


def _zbar_scan(img):
    z = _lib()
    if not z:
        return []
    g = img.convert("L")
    w, h = g.size
    data = g.tobytes()
    sc = z.zbar_image_scanner_create()
    z.zbar_image_scanner_set_config(sc, 0, 0, 1)          # alle Symbologien an
    im = z.zbar_image_create()
    z.zbar_image_set_format(im, 0x30303859)               # 'Y800'
    z.zbar_image_set_size(im, w, h)
    buf = ctypes.create_string_buffer(data, len(data))
    z.zbar_image_set_data(im, buf, len(data), None)
    out = []
    try:
        if z.zbar_scan_image(sc, im) > 0:
            sym = z.zbar_image_first_symbol(im)
            while sym:
                n = z.zbar_symbol_get_loc_size(sym)
                xs = [z.zbar_symbol_get_loc_x(sym, i) for i in range(n)]
                ys = [z.zbar_symbol_get_loc_y(sym, i) for i in range(n)]
                out.append({"type": z.zbar_get_symbol_name(z.zbar_symbol_get_type(sym)).decode(),
                            "data": z.zbar_symbol_get_data(sym).decode("utf-8", "ignore"),
                            "box": (min(xs), min(ys), max(xs), max(ys)) if n else None})
                sym = z.zbar_symbol_next(sym)
    finally:
        z.zbar_image_destroy(im)
        z.zbar_image_scanner_destroy(sc)
    return out


def barcodes(img):
    """Mehrere Größen/Drehungen probieren – Handyfotos sind oft schräg oder sehr groß.
    Jeder Treffer bekommt 'crop': den Bereich ums Etikett (im gedrehten Originalbild) für die Texterkennung."""
    seen, out = set(), []
    base = ImageOps.exif_transpose(img)
    for maxw in (1600, 2400, 1000):
        im = base.copy()
        im.thumbnail((maxw, maxw))
        f = base.width / im.width
        for rot in (0, 90):
            for r in _zbar_scan(im.rotate(rot, expand=True) if rot else im):
                if r["data"] not in seen:
                    seen.add(r["data"])
                    if r.get("box"):
                        r["box"] = tuple(int(v * f) for v in r["box"])
                    r["rot"] = rot
                    out.append(r)
        if out:
            break
    return out


def label_crop(img, code):
    """Etikett rund um den Strichcode ausschneiden (Text steht meist darüber und darunter)."""
    base = ImageOps.exif_transpose(img)
    if code.get("rot"):
        base = base.rotate(code["rot"], expand=True)
    x0, y0, x1, y1 = code["box"]
    w = max(x1 - x0, 50)
    h = max(y1 - y0, w // 4)                 # 1D-Code: zbar liefert oft nur eine Linie
    box = (max(0, x0 - int(w * .25)), max(0, y0 - int(h * 1.6)), min(base.width, x1 + int(w * .25)), min(base.height, y1 + int(h * 3.2)))
    return base.crop(box)


def is_ean(code):
    """EAN-13 / UPC-A / EAN-8 mit gültiger Prüfziffer."""
    if not re.fullmatch(r"\d{8}|\d{12,13}", code or ""):
        return False
    d = [int(c) for c in code.zfill(13 if len(code) > 8 else 8)]
    s = sum(x * (3 if i % 2 else 1) for i, x in enumerate(d[:-1])) if len(d) == 13 else \
        sum(x * (1 if i % 2 else 3) for i, x in enumerate(d[:-1]))
    return (10 - s % 10) % 10 == d[-1]


# ---------------------------------------------------------------- Text (OCR)
def _tess(im, psm, timeout=25):
    b = io.BytesIO()
    im.save(b, "PNG")
    try:
        p = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", psm],
                           input=b.getvalue(), capture_output=True, timeout=timeout)
        return p.stdout.decode("utf-8", "ignore")
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("Texterkennung nicht verfügbar: %s", e)
        return ""


def ocr_texts(img, small=False):
    """Mehrere Varianten (normal, schwarz/weiß, Seitenmodi) → Liste von Texten; parse_votes() wählt aus."""
    im = ImageOps.exif_transpose(img).convert("L")
    if im.width < 1600:                      # kleiner Ausschnitt → vergrößern, Tesseract mag ~30 px hohe Schrift
        f = 1600 / im.width
        im = im.resize((1600, int(im.height * f)), Image.LANCZOS)
    im.thumbnail((2400, 2400))
    im = ImageOps.autocontrast(im)
    sharp = im.filter(ImageFilter.SHARPEN)
    bw = im.point(lambda v: 255 if v > 140 else 0)
    variants = [(sharp, "6"), (sharp, "11"), (bw, "6")] if small else [(sharp, "11")]
    return [_tess(i, psm) for i, psm in variants]


def ocr(img, timeout=25):
    return "\n".join(ocr_texts(img))


def parse_votes(texts):
    """Jeden Text einzeln auswerten, pro Feld den häufigsten Wert nehmen (OCR verliest sich mal hier, mal da)."""
    from collections import Counter
    votes = {}
    for t in texts:
        for k, v in parse_text(t).items():
            votes.setdefault(k, Counter())[v] += 1
    out = {k: c.most_common(1)[0][0] for k, c in votes.items()}
    if "color" in out:                       # Hex passend zur gewählten Farbe
        for t in texts:
            p = parse_text(t)
            if p.get("color") == out["color"]:
                out["color_hex"] = p["color_hex"]
                break
    return out


MATERIALS = ["PLA-CF", "PETG-CF", "PA-CF", "PA6-CF", "PA12-CF", "PET-CF", "PETG-HF", "PETG HF", "PLA+", "PLA PLUS", "PLA PRO",
             "SILK PLA", "PLA SILK", "PLA MATTE", "MATTE PLA", "PETG", "PCTG", "PLA", "ABS", "ASA", "TPU", "TPE", "PVA",
             "PA6", "PA12", "PA", "NYLON", "PC", "HIPS", "PET", "PP"]
# Farbname (englisch/deutsch/italienisch) → (Anzeige, Hex)
COLORS = {
    "black": ("Schwarz", "000000"), "schwarz": ("Schwarz", "000000"), "nero": ("Schwarz", "000000"),
    "white": ("Weiß", "FFFFFF"), "weiss": ("Weiß", "FFFFFF"), "weiß": ("Weiß", "FFFFFF"), "bianco": ("Weiß", "FFFFFF"),
    "grey": ("Grau", "808080"), "gray": ("Grau", "808080"), "grau": ("Grau", "808080"), "grigio": ("Grau", "808080"),
    "silver": ("Silber", "C0C0C0"), "silber": ("Silber", "C0C0C0"),
    "red": ("Rot", "D32F2F"), "rot": ("Rot", "D32F2F"), "rosso": ("Rot", "D32F2F"),
    "blue": ("Blau", "1E63C9"), "blau": ("Blau", "1E63C9"), "blu": ("Blau", "1E63C9"),
    "green": ("Grün", "2E9E44"), "grün": ("Grün", "2E9E44"), "gruen": ("Grün", "2E9E44"), "verde": ("Grün", "2E9E44"),
    "yellow": ("Gelb", "F5D300"), "gelb": ("Gelb", "F5D300"), "giallo": ("Gelb", "F5D300"),
    "orange": ("Orange", "F57C00"), "arancione": ("Orange", "F57C00"),
    "purple": ("Lila", "7B3FA0"), "violet": ("Lila", "7B3FA0"), "lila": ("Lila", "7B3FA0"),
    "pink": ("Rosa", "F06292"), "rosa": ("Rosa", "F06292"),
    "brown": ("Braun", "6D4C41"), "braun": ("Braun", "6D4C41"),
    "beige": ("Beige", "D9C7A0"), "gold": ("Gold", "C9A227"), "bronze": ("Bronze", "A0703C"),
    "transparent": ("Transparent", "E8EEF0"), "clear": ("Transparent", "E8EEF0"), "natural": ("Natur", "EDE6D6"),
    "natur": ("Natur", "EDE6D6"), "cyan": ("Cyan", "00A9C9"), "skin": ("Hautfarbe", "E8B89A"), "wood": ("Holz", "A0784C"),
}
_MOD = ["light", "dark", "sky", "navy", "olive", "grass", "army", "mint", "matte", "silk", "hell", "dunkel"]


_OCR_FIX = [(r"(?<![A-Z0-9])PET[C6](?![A-Z0-9])", "PETG"), (r"(?<![A-Z0-9])P[1I|]A(?![A-Z0-9])", "PLA"),
            (r"(?<![A-Z0-9])A8S(?![A-Z0-9])", "ABS"), (r"(?<![A-Z0-9])TP[UV](?![A-Z0-9])", "TPU")]


def parse_text(text):
    """Etikett-/Produkttext → Material, Farbe, Gewicht, Durchmesser, Temperaturen."""
    t = " " + re.sub(r"\s+", " ", text or "") + " "
    for a, b in _OCR_FIX:
        t = re.sub(a, b, t)
    up = t.upper()
    out = {}
    for m in MATERIALS:
        if re.search(r"(?<![A-Z0-9])" + re.escape(m) + r"(?![A-Z0-9])", up):
            out["material"] = {"PLA PLUS": "PLA+", "PLA PRO": "PLA+", "NYLON": "PA", "PETG HF": "PETG-HF",
                               "PLA SILK": "SILK PLA", "MATTE PLA": "PLA MATTE"}.get(m, m)
            break
    low = t.lower()
    name, hx = colors.find_in_text(t)           # Originalname wie gedruckt, z. B. „Sky Blue“
    if name:
        out["color"], out["color_hex"] = name, hx or "999999"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s?(kg|k|g)(?![a-z])", low)
    if m:
        v = float(m.group(1).replace(",", "."))
        g = v * 1000 if m.group(2) in ("kg", "k") else v
        if 100 <= g <= 10000:
            out["weight"] = round(g)
    m = re.search(r"(1[.,]75|2[.,]85|3[.,]00?)\s?mm", low)
    if m:
        out["diameter"] = float(m.group(1).replace(",", "."))
    temps = [(int(a), int(b)) for a, b in re.findall(r"(\d{3})\s?[-–~]\s?(\d{3})\s?°?\s?[c℃]?", t)]
    noz = [x for x in temps if 170 <= x[0] <= 320]
    if noz:
        out["extruder_temp"] = round((noz[0][0] + noz[0][1]) / 2 / 5) * 5
    m = re.search(r"(?:bed|hotbed|plate|platform)\s?temp\w*\s?:?\s?(\d{2,3})\s?[-–~]\s?(\d{2})", low)
    if m and 30 <= int(m.group(1)) <= 130 and 30 <= int(m.group(2)) <= 130:
        out["bed_temp"] = round((int(m.group(1)) + int(m.group(2))) / 2 / 5) * 5
    return out


# ---------------------------------------------------------------- Online (EAN)
_lookups = []


def online_lookup(session, ean):
    """UPCitemdb (kostenlos, ohne Anmeldung, 100 Abfragen/Tag). → {title, brand} oder None"""
    now = time.time()
    _lookups[:] = [x for x in _lookups if now - x < 86400]
    if len(_lookups) >= 90:
        return {"error": "Tageslimit der Online-Suche erreicht"}
    _lookups.append(now)
    try:
        r = session.get("https://api.upcitemdb.com/prod/trial/lookup", params={"upc": ean}, timeout=8,
                        headers={"Accept": "application/json"})
        if r.status_code == 429:
            return {"error": "Tageslimit der Online-Suche erreicht"}
        items = (r.json() or {}).get("items") or []
    except Exception as e:
        log.info("EAN-Suche %s: %s", ean, e)
        return {"error": "Online-Suche nicht erreichbar"}
    if not items:
        return None
    it = items[0]
    return {"title": it.get("title") or "", "brand": it.get("brand") or "", "text": " ".join(
        str(it.get(k) or "") for k in ("title", "description", "color", "size"))}
