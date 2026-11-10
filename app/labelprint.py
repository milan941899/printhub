"""Etiketten direkt auf einen Brother QL drucken (QL-700/800/810W/820NWB/1100 …) – über das Netzwerk (Port 9100).

Eigene, schlanke Umsetzung des Brother-„Raster“-Protokolls: Etikett als Bild (Pillow) zeichnen,
in 1-Bit-Zeilen umwandeln und mit den Steuerbefehlen an den Drucker schicken. Kein Druckertreiber nötig.
"""
import io
import os
import socket
import struct

from PIL import Image, ImageDraw, ImageFont

from .qr import QR

# Etiketten: Breite/Länge (mm), druckbare Punkte (Breite, Länge; 0 = Endlos), rechter Rand in Punkten
MEDIA = {
    "62x29": {"t": "62 × 29 mm (DK-11209)", "mm": (62, 29), "dots": (696, 271), "rm": 12},
    "62": {"t": "62 mm Endlos (DK-22205)", "mm": (62, 0), "dots": (696, 0), "rm": 12},
    "50": {"t": "50 mm Endlos (DK-22223)", "mm": (50, 0), "dots": (554, 0), "rm": 12},
    "38x90": {"t": "38 × 90 mm (DK-11208)", "mm": (38, 90), "dots": (413, 991), "rm": 12},
    "29x90": {"t": "29 × 90 mm (DK-11201)", "mm": (29, 90), "dots": (306, 991), "rm": 6},
    "29": {"t": "29 mm Endlos (DK-22210)", "mm": (29, 0), "dots": (306, 0), "rm": 6},
}
MODELS = {   # Kopfbreite in Bytes (90 = 720 Punkte), Länge der „Aufwecken“-Folge
    "QL-700": (90, 200), "QL-710W": (90, 200), "QL-720NW": (90, 200),
    "QL-800": (90, 400), "QL-810W": (90, 400), "QL-820NWB": (90, 400),
    "QL-1100": (162, 400), "QL-1110NWB": (162, 400),
}
DPMM = 300 / 25.4      # 300 dpi

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/truetype/liberation"]


def _font(size, bold=False):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"]
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _qr_image(text, px):
    q = QR(text)
    n = q.size + 2
    im = Image.new("1", (n, n), 1)
    for y in range(q.size):
        for x in range(q.size):
            if q.mod[y][x]:
                im.putpixel((x + 1, y + 1), 0)
    return im.resize((px, px), Image.NEAREST)


def _fit(draw, text, width, size, bold, min_size=14):
    """Schrift so lange verkleinern, bis der Text passt (höchstens 2 Zeilen)."""
    while size >= min_size:
        f = _font(size, bold)
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=f) <= width:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) <= 2 and all(draw.textlength(l, font=f) <= width for l in lines):
            return f, lines
        size -= 2
    f = _font(min_size, bold)
    return f, [text[:28]]


def render(spool, url, media="62x29", length_mm=30):
    """Etikett als Bild (weiß = 255, schwarz = 0) in der Größe der druckbaren Fläche."""
    m = MEDIA.get(media) or MEDIA["62x29"]
    w, h = m["dots"]
    if not h:
        h = int(max(20, min(200, float(length_mm or 30))) * DPMM)
    tall = h > w * 1.6                     # hohe Etiketten (29×90, 38×90): quer gestalten und drehen
    W, H = (h, w) if tall else (w, h)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    pad = max(8, H // 18)
    qs = H - 2 * pad
    im.paste(_qr_image(url, qs).convert("L"), (pad, pad))
    x0 = pad * 2 + qs
    tw = W - x0 - pad
    y = pad
    f_id = _font(int(H * 0.26), True)
    d.text((x0, y), f"#{spool.get('id', '')}", font=f_id, fill=0)
    y += int(H * 0.30)
    name = str(spool.get("name") or spool.get("filament") or "").strip()
    f_nm, lines = _fit(d, name, tw, int(H * 0.15), True)
    for ln in lines:
        d.text((x0, y), ln, font=f_nm, fill=0)
        y += int(f_nm.size * 1.15)
    y += int(H * 0.03)
    r = int(H * 0.06)
    if y + 2 * r <= H - pad:
        d.ellipse((x0, y, x0 + 2 * r, y + 2 * r), outline=0, width=3,
                  fill=0 if _dark(spool.get("color")) else 255)
        mat = " · ".join(x for x in [str(spool.get("material") or ""), str(spool.get("vendor") or "")] if x)
        f_m = _font(int(H * 0.12), False)
        d.text((x0 + 2 * r + 10, y - int(H * 0.01)), mat[:30], font=f_m, fill=0)
    if tall:
        im = im.rotate(90, expand=True)
    return im


def _dark(color):
    c = str(color or "").lstrip("#")
    if len(c) < 6:
        return False
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return False
    return (r * 299 + g * 587 + b * 114) / 1000 < 110


def raster(images, media="62x29", model="QL-810W", cut=True):
    """Bilder (druckbare Fläche) → Befehlsfolge für den Drucker."""
    m = MEDIA.get(media) or MEDIA["62x29"]
    row_bytes, wake = MODELS.get(model, (90, 400))
    head = row_bytes * 8
    out = bytearray(b"\x00" * wake)        # Drucker aufwecken / alten Auftrag verwerfen
    out += b"\x1b\x40"                     # Initialisieren
    for i, img in enumerate(images):
        g = img.convert("L").point(lambda v: 0 if v < 128 else 255).convert("1")
        lines = g.size[1]
        canvas = Image.new("1", (head, lines), 1)
        canvas.paste(g, (head - g.size[0] - m["rm"], 0))
        canvas = canvas.transpose(Image.FLIP_LEFT_RIGHT)
        out += b"\x1b\x69\x61\x01"         # Raster-Modus
        endless = m["mm"][1] == 0
        flags = 0x80 | 0x02 | 0x04 | 0x08 | 0x40
        out += b"\x1b\x69\x7a" + bytes([flags, 0x0A if endless else 0x0B, m["mm"][0], m["mm"][1]])
        out += struct.pack("<L", lines) + bytes([0 if i == 0 else 1, 0])
        out += b"\x1b\x69\x4d" + bytes([0x40 if cut else 0])       # automatisch schneiden
        out += b"\x1b\x69\x41\x01"                                  # nach jedem Etikett
        out += b"\x1b\x69\x4b" + bytes([0x08 if cut else 0])       # am Ende schneiden
        out += b"\x1b\x69\x64" + struct.pack("<H", 35 if endless else 0)   # Rand bei Endlosband
        out += b"\x4d\x00"                                          # ohne Kompression
        px = canvas.load()
        for y in range(lines):
            row = bytearray(row_bytes)
            for x in range(head):
                if px[x, y] == 0:          # schwarz
                    row[x >> 3] |= 0x80 >> (x & 7)
            out += b"\x67\x00" + bytes([row_bytes]) + bytes(row)
        out += b"\x1a" if i == len(images) - 1 else b"\x0c"        # drucken (letztes) / Seitenwechsel
    return bytes(out)


def send(host, data, port=9100, timeout=8):
    host = str(host or "").strip()
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(data)


def png(img):
    b = io.BytesIO()
    img.convert("L").save(b, "PNG")
    return b.getvalue()
