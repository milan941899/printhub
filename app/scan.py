"""Rolle per Foto erkennen: Strichcode (EAN / Charge) + Text auf dem Etikett (Material, Farbe, Gewicht …).

Strichcodes über libzbar (ctypes, kein Python-Paket nötig), Text über das Programm „tesseract“.
Beides läuft lokal auf dem Server – nur das optionale Nachschlagen eines EAN geht ins Internet (UPCitemdb).
"""
import ctypes
import ctypes.util
import io
import os
import logging
import re
import subprocess
import time

from PIL import ImageChops, Image, ImageFilter, ImageOps

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
    for maxw, rots in ((1600, (0, 90)), (1000, (0,))):    # kein Strichcode? nach 3 statt 6 Versuchen aufgeben
        im = base.copy()
        im.thumbnail((maxw, maxw))
        f = base.width / im.width
        for rot in rots:
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


def band_texts(img, code, stage="a"):
    """Text in Streifen über und unter dem Strichcode lesen – in Originalgröße (Vergrößern verschlechtert schmale
    Etikettenschrift). Oben steht meist Material/Farbe, unten Nummer, Gewicht, Temperaturen."""
    base = ImageOps.exif_transpose(img).convert("L")
    if code.get("rot"):
        base = base.rotate(code["rot"], expand=True)
    x0, y0, x1, y1 = code["box"]
    w = max(x1 - x0, 80)
    # zbar liefert oft nur die Lese-Linie → echte Ober-/Unterkante der Balken suchen (viele Hell/Dunkel-Wechsel)
    px = base.load()
    def trans(y):
        if not 0 <= y < base.height:
            return 0
        row = [px[x, y] for x in range(max(0, x0), min(base.width, x1), 2)]
        if not row:
            return 0
        m = sum(row) / len(row)
        return sum((a < m) != (b < m) for a, b in zip(row, row[1:]))
    ref = max(trans((y0 + y1) // 2), 8)
    top_y = y0
    while top_y > y0 - w * 0.3 and trans(top_y - 2) > ref * 0.45:
        top_y -= 2
    bot_y = y1
    while bot_y < y1 + w * 0.3 and trans(bot_y + 2) > ref * 0.45:
        bot_y += 2
    top_y, bot_y = top_y - int(w * .01), bot_y + int(w * .01)
    top = base.crop((max(0, x0 - int(w * .15)), max(0, top_y - int(w * .13)), min(base.width, x1 + int(w * .15)), max(1, top_y)))
    bot = base.crop((max(0, x0 - int(w * .15)), bot_y, min(base.width, x1 + int(w * .25)), min(base.height, bot_y + int(w * .42))))
    jobs = []
    def go(band, sc, sharp, psm, fast=False):
        if band.width < 20 or band.height < 10:
            return
        b = band.resize((max(1, int(band.width * sc)), max(1, int(band.height * sc))), Image.LANCZOS) if sc != 1 else band
        b = ImageOps.autocontrast(b)
        if sharp:
            b = b.filter(ImageFilter.SHARPEN)
        jobs.append((b, psm, fast))
    # Bereich über der Lese-Linie, Licht/Schatten ausgeglichen – im Test die mit Abstand zuverlässigste Variante
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    def wide_of(img_):
        return img_.crop((max(0, int(x0 - w * .12)), max(0, int(y0 - w * .30)), min(base.width, int(x1 + w * .12)), max(1, int(y0 - w * .02))))
    def flat_of(reg):
        bg = reg.filter(ImageFilter.GaussianBlur(max(10, int(w * .04))))
        return Image.eval(ImageChops.subtract(bg, reg), lambda v: 255 - min(255, v * 3))
    wide = wide_of(base)
    ok = wide.width > 20 and wide.height > 10
    if stage == "a":            # schnell: ausgeglichener Bereich über den Balken + gerade Zeile
        if ok:
            jobs.append((flat_of(wide), "11"))
        go(top, 0.7, True, "11")
        return run_ocr(jobs, "Stufe a (über den Balken)")
    if stage == "b":            # vergrößert + unter den Balken (Gewicht, Nummer, Temperaturen)
        if ok:
            f = flat_of(wide)
            jobs.append((f.resize((int(f.width * 1.5), int(f.height * 1.5)), Image.LANCZOS), "11"))
            go(wide, 0.7, False, "6")
        go(top, 1.0, True, "6")
        go(bot, 1.0, False, "6")
        return run_ocr(jobs, "Stufe b (vergrößert + unten)")
    # Stufe 2 – schräge Etiketten: leicht gedreht, mit und ohne Ausgleich
    for ang in (-3, 3):
        rb = base.rotate(ang, center=(cx, cy), resample=Image.BICUBIC, fillcolor=255)
        wr = wide_of(rb)
        if wr.width > 20 and wr.height > 10:
            jobs.append((flat_of(wr), "11"))
            go(wr, 0.7, False, "6")
    go(top, 1.0, False, "7")
    go(bot, 1.0, True, "6")
    return run_ocr(jobs, "Stufe 2 (gedreht)")


def find_label(img):
    """Weißes Etikett im Foto finden, wenn kein Strichcode gelesen wurde: helle, rechteckige Fläche mit viel
    Schrift-Kanten (bevorzugt vor glattem Hintergrund). Gibt den Ausschnitt in voller Auflösung zurück oder None."""
    g = ImageOps.exif_transpose(img).convert("L")
    sm = g.copy()
    sm.thumbnail((320, 320))
    f = g.width / sm.width
    ac = ImageOps.autocontrast(sm)
    edges = ac.filter(ImageFilter.FIND_EDGES)
    blob = ac.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(3))   # Schrift „zumachen“
    W, H = blob.size
    data = blob.tobytes()
    hist = sorted(data)
    thr = max(170, hist[int(len(hist) * 0.80)] - 25)
    seen = bytearray(W * H)
    best = None
    for y in range(H):
        for x in range(W):
            i = y * W + x
            if seen[i] or data[i] < thr:
                continue
            st, n, x0, x1, y0, y1 = [i], 0, x, x, y, y
            seen[i] = 1
            while st:
                j = st.pop(); n += 1
                cx, cy = j % W, j // W
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for k in ((j + 1) if cx + 1 < W else -1, (j - 1) if cx > 0 else -1, j + W if cy + 1 < H else -1, j - W if cy > 0 else -1):
                    if k >= 0 and not seen[k] and data[k] >= thr:
                        seen[k] = 1; st.append(k)
            bw, bh = x1 - x0 + 1, y1 - y0 + 1
            if n > W * H * 0.01 and n / (bw * bh) > 0.5 and 1.2 < bw / bh < 6:
                e = edges.crop((x0, y0, x1 + 1, y1 + 1)).tobytes()
                sc = sum(e) / len(e)
                if not best or sc > best[0]:
                    best = (sc, (x0, y0, x1, y1))
    if not best or best[0] < 25:
        return None
    x0, y0, x1, y1 = best[1]
    return g.crop((max(0, int((x0 - 3) * f)), max(0, int((y0 - 3) * f)), min(g.width, int((x1 + 3) * f)), min(g.height, int((y1 + 3) * f))))


def label_texts(crop):
    ac = ImageOps.autocontrast(crop)
    if ac.width > 1400:
        ac.thumbnail((1400, 1400))
    return run_ocr([(ac.filter(ImageFilter.SHARPEN), "6"), (ac, "11")], "Etikett gefunden (ohne Strichcode)")


def is_ean(code):
    """EAN-13 / UPC-A / EAN-8 mit gültiger Prüfziffer."""
    if not re.fullmatch(r"\d{8}|\d{12,13}", code or ""):
        return False
    d = [int(c) for c in code.zfill(13 if len(code) > 8 else 8)]
    s = sum(x * (3 if i % 2 else 1) for i, x in enumerate(d[:-1])) if len(d) == 13 else \
        sum(x * (1 if i % 2 else 3) for i, x in enumerate(d[:-1]))
    return (10 - s % 10) % 10 == d[-1]


# ---------------------------------------------------------------- Text (OCR)
_WORKERS = max(2, min(8, os.cpu_count() or 2))
_FAST = None


def _fast_lang():
    """„eng_fast“ (schnelles Tesseract-Modell), falls im Image vorhanden."""
    global _FAST
    if _FAST is None:
        import glob
        _FAST = "eng_fast" if glob.glob("/usr/share/tesseract-ocr/*/tessdata/eng_fast.traineddata") else ""
    return _FAST


TRACE = []        # Zeitmessung für den laufenden Scan (Diagnose): [(name, Läufe, Sekunden, längster Lauf, Pixel)]


def run_ocr(jobs, name="ocr"):
    """[(bild, psm, fast?)] parallel auf allen Kernen."""
    from concurrent.futures import ThreadPoolExecutor
    if not jobs:
        return []
    t0, each = time.time(), []
    def one(j):
        t = time.time()
        r = _tess(j[0], j[1], fast=len(j) > 2 and j[2])
        each.append(time.time() - t)
        return r
    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        out = list(ex.map(one, jobs))
    TRACE.append((name, len(jobs), round(time.time() - t0, 2), round(max(each), 2), sum(j[0].width * j[0].height for j in jobs)))
    return out


def _tess(im, psm, timeout=25, fast=False):
    b = io.BytesIO()
    im.save(b, "PNG")
    try:
        # OMP_THREAD_LIMIT=1: mehrere Tesseract-Läufe parallel, jeder mit einem Kern (sonst bremsen sie sich gegenseitig aus)
        lang = (_fast_lang() or "eng") if fast else "eng"
        p = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", psm],
                           input=b.getvalue(), capture_output=True, timeout=timeout,
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        return p.stdout.decode("utf-8", "ignore")
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("Texterkennung nicht verfügbar: %s", e)
        return ""


def ocr_texts(img, small=False):
    """Mehrere Varianten (normal, schwarz/weiß, Seitenmodi) → Liste von Texten; parse_votes() wählt aus."""
    im = ImageOps.exif_transpose(img).convert("L")
    if im.width < 800:                       # nur sehr kleine Ausschnitte vergrößern (sonst wird schmale Schrift schlechter)
        f = 1000 / im.width
        im = im.resize((1600, int(im.height * f)), Image.LANCZOS)
    im.thumbnail((2400, 2400) if small else (1700, 1700))   # ganzes Foto: Schrift auf Rolle/Karton ist groß genug, halb so viele Pixel
    im = ImageOps.autocontrast(im)
    sharp = im.filter(ImageFilter.SHARPEN)
    if small:
        return run_ocr([(sharp, "6"), (im, "6")], "Etikett gesamt")
    # ganzes Foto: dunkle Schrift, dazu helle Schrift auf buntem/dunklem Grund (Hintergrund abziehen)
    bg = im.filter(ImageFilter.GaussianBlur(max(15, im.width // 80)))
    light = Image.eval(ImageChops.subtract(im, bg), lambda v: 255 - min(255, v * 3))
    dark = Image.eval(ImageChops.subtract(bg, im), lambda v: 255 - min(255, v * 3))
    return run_ocr([(sharp, "11"), (light, "11"), (dark, "11")], "ganzes Foto")


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
             "PA6", "PA12", "PA", "NYLON", "PC", "HIPS", "PP"]
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


_OCR_FIX = [(r"(?<![A-Za-z0-9])[lI|](\s?kg)(?![a-z])", r"1\1"), (r"(\d)\s?k[9gq](?![a-z])", r"\1kg"), (r"(?<![A-Za-z])Whi[l1I]e(?![a-z])", "White"), (r"(?<![A-Za-z])Whte(?![a-z])", "White"),
            (r"(?<![A-Z0-9])PET[C6](?![A-Z0-9])", "PETG"), (r"(?<![A-Z0-9])P[1I|]A(?![A-Z0-9])", "PLA"),
            (r"(?<![A-Z0-9])A8S(?![A-Z0-9])", "ABS"), (r"(?<![A-Z0-9])TP[UV](?![A-Z0-9])", "TPU")]


_WEIGHTS = (250, 330, 500, 600, 750, 800, 900, 1000, 1100, 1200, 1250, 1500, 2000, 2500, 3000, 4500, 5000, 10000)


def plausible_weight(g):
    """Übliche Rollengewichte. Typische Lesefehler (1↔4/7, 0↔8) zurückbiegen: „4.1kg“ → 1100 g."""
    g = round(g)
    if g in _WEIGHTS:
        return g
    s = str(g)
    for a, b in (("4", "1"), ("7", "1"), ("8", "0"), ("6", "0"), ("9", "0")):
        for i, ch in enumerate(s):
            if ch == a:
                t = int(s[:i] + b + s[i + 1:])
                if t in _WEIGHTS:
                    return t
    return None


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
    # Gewicht: „Net weight: 1kg“ zuerst, sonst die erste plausible Angabe (nicht „Spool weight“, keine Skala „800g 600g …“)
    def kg(num, unit):
        v = float(num.replace(",", "."))
        return plausible_weight(v * 1000 if unit in ("kg", "k") else v)
    m = re.search(r"net\s*(?:weight|wt\.?|gewicht)\s*[:;.]?\s*(\d+(?:[.,]\d+)?)\s?(kg|k|g)(?![a-z])", low)
    g = kg(m.group(1), m.group(2)) if m else None
    if not g:
        for m in re.finditer(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s?(kg|k|g)(?![a-z])", low):
            if re.search(r"spool\s*weight\s*[:;.]?\s*~?\s*$", low[max(0, m.start() - 25):m.start()]):
                continue
            g = kg(m.group(1), m.group(2))
            if g:
                break
    if g:
        out["weight"] = g
    m = re.search(r"(?:spool|empty\s*spool|leer\w*)\s*weight\s*[:;.]?\s*~?\s*(\d{2,3})\s?g", low)
    if m and 50 <= int(m.group(1)) <= 400:
        out["spool_weight"] = int(m.group(1))
    m = re.search(r"hex\s*[:;.]?\s*#?\s*([0-9a-f]{6})(?![0-9a-z])", low)
    if m:
        out["hex_print"] = m.group(1).upper()
    m = re.search(r"(?:sku|art(?:ikel)?\.?\s*-?\s*nr\.?|item\s*(?:no|number)\.?|product\s*code)\s*[:;.]?\s*([a-z0-9][a-z0-9-]{3,})", low)
    if m:
        out["sku"] = m.group(1).upper()
    if "sku" not in out:                         # Bambu: „Model: A01-K1-1.75-1000-SPL“ – pro Farbe eindeutig
        m = re.search(r"model\s*[:;.]?\s*([a-z0-9]{2,4}-[a-z0-9]{1,3}-1\.75-\d{3,4}-[a-z]{2,8})", low)
        if m:
            sk = m.group(1).upper().split("-")
            sk[0] = sk[0][0] + sk[0][1:].replace("O", "0")          # „AO1“ → „A01“
            out["sku"] = "-".join(sk)
    if "sku" not in out:                         # Bambu: „Filament Code: 10103“ – pro Sorte+Farbe eindeutig
        m = re.search(r"filament\s*code\s*[:;.]?\s*(\d{5})(?!\d)", low)
        if m:
            out["sku"] = m.group(1)
    m = re.search(r"(?:batch|lot)\s*(?:number|no\.?|nr\.?|#)?\s*[:;.]?\s*([a-z0-9]{5,})", low)
    if m and re.search(r"\d", m.group(1)):
        out["batch"] = m.group(1).upper()
    if "material" not in out and re.search(r"(?<![a-z])ingeo(?![a-z])", low):
        out["material"] = "PLA"              # Ingeo = PLA-Rohstoff (z. B. Polymaker)
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


_CODE_RE = re.compile(r"(?<![A-Z0-9])(\d{6,12}[A-Z]{1,3}|[A-Z]{1,3}\d{6,12})(?![A-Z0-9])")


def find_codes(texts):
    """Artikel-/Chargennummern im Etikett-Text (z. B. „04260702AJ“) – häufigste zuerst."""
    from collections import Counter
    c = Counter()
    for t in texts:
        u = (t or "").upper()
        u = re.sub(r"(\d{6,12}[A-Z]{0,2})[)\]]", r"\1J", u)      # „J“ wird gern als „)“ oder „]“ gelesen
        u = re.sub(r"(\d{6,12}[A-Z]{0,2})[|!]", r"\1I", u)
        for m in _CODE_RE.findall(u):
            c[m] += 1
    return [k for k, _ in c.most_common()]


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
