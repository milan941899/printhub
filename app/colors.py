"""Farbnamen → Farbwert (ungefähr) und Deutsch → Englisch.

Filament-Farbnamen sind nicht genormt – diese Liste liefert eine brauchbare Näherung für Anzeige und Abgleich.
Genauer ist der Wert aus der Filament-Datenbank (SpoolmanDB) oder die Pipette aus einem Foto.
"""
import re

# englische Namen (wie auf Etiketten üblich), längere zuerst gefunden
NAMES = {
    # Grundfarben
    "black": "000000", "white": "FFFFFF", "grey": "808080", "gray": "808080", "silver": "C0C0C0", "red": "D32F2F",
    "blue": "1E63C9", "green": "2E9E44", "yellow": "F5D300", "orange": "F57C00", "purple": "7B3FA0", "violet": "8A4FBF",
    "pink": "F06292", "brown": "6D4C41", "beige": "D9C7A0", "gold": "C9A227", "bronze": "A0703C", "copper": "B87333",
    "cyan": "00A9C9", "magenta": "C2185B", "turquoise": "1ABC9C", "teal": "00897B", "navy": "1F2D5C", "olive": "6B7A2A",
    "lime": "9BD62A", "mint": "98E0C0", "lavender": "B39DDB", "lilac": "C8A2C8", "ivory": "F3EEDC", "cream": "F2E8CF",
    "khaki": "BDB176", "tan": "D2B48C", "maroon": "7A1F2B", "burgundy": "7D1E2C", "wine": "6E1E2E", "coral": "FF7F6A",
    "salmon": "F49A84", "peach": "FFC8A2", "apricot": "F9B37D", "amber": "F2A900", "mustard": "D4A017", "ochre": "C98B2D",
    "sand": "D8C49A", "stone": "9E9A91", "slate": "5D6B7A", "charcoal": "36393F", "graphite": "4A4E54", "titanium": "8C8E8F",
    "transparent": "E8EEF0", "clear": "E8EEF0", "natural": "EDE6D6", "skin": "E8B89A", "wood": "A0784C", "marble": "D9D9D6",
    "glow": "C8F7A0", "rainbow": "C060C0", "galaxy": "2B2140", "jade": "3AA17E", "emerald": "1E8C5A", "ruby": "B0172F",
    "sapphire": "1F4FA0", "indigo": "3F3D9C", "azure": "2F8FE0", "aqua": "38C6D9", "ice": "D6ECF3", "snow": "FAFAFA",
    "ash": "B2B2B2", "fuchsia": "D61C9A", "rose": "E0729A", "plum": "6E3B6E", "grape": "5E3A80", "cherry": "B3122E",
    "chocolate": "5A3825", "coffee": "6F4E37", "caramel": "B9783F", "brick": "A3482F", "rust": "A0482A", "terracotta": "C4623E",
    "army": "4B5320", "forest": "2E5E3A", "moss": "6B7B3A", "sage": "9CAF88", "pistachio": "A8C686",
    # Kombinationen, wie Hersteller sie benennen
    "sky blue": "87CEEB", "baby blue": "A8D4F2", "light blue": "8EC5F0", "dark blue": "143E8C", "navy blue": "1F2D5C",
    "royal blue": "2E5BD8", "electric blue": "1F75FE", "ice blue": "BFE3F2", "ocean blue": "1F6FA8", "sea blue": "2C7FB8",
    "peacock blue": "006A8E", "klein blue": "002FA7", "cobalt blue": "1F4AAE", "steel blue": "4682B4", "denim blue": "3B5B8C",
    "lake blue": "3C8DBC", "blue grey": "6B7F8F", "blue gray": "6B7F8F", "midnight blue": "191970", "pastel blue": "AEC6E8",
    "light green": "90D68B", "dark green": "1E5E32", "grass green": "4CAF50", "apple green": "8DB600", "lime green": "7ED957",
    "mint green": "98E0C0", "olive green": "6B7A2A", "army green": "4B5320", "forest green": "228B22", "jungle green": "29AB87",
    "bambu green": "00AE42", "neon green": "39FF14", "fluorescent green": "5BFF2A", "pine green": "01796F",
    "tennis court green": "3E8E5B", "turf green": "3D8C40", "pastel green": "A8E6A1", "sage green": "9CAF88",
    "light grey": "C8C8C8", "light gray": "C8C8C8", "dark grey": "4F4F4F", "dark gray": "4F4F4F", "space grey": "5B5F66",
    "space gray": "5B5F66", "cool grey": "8C929A", "warm grey": "9A938A", "nardo grey": "7B7E80", "silver grey": "A8A9AD",
    "brick red": "A3482F", "fire red": "E0281E", "scarlet red": "D9231F", "wine red": "6E1E2E", "dark red": "8B1A1A",
    "cherry red": "B3122E", "signal red": "C1121C", "rose red": "C21E56", "fluorescent red": "FF3B3B", "neon red": "FF3131",
    "lemon yellow": "FFF44F", "sunflower yellow": "FFC512", "light yellow": "FFF3A0", "neon yellow": "E9FF1F",
    "fluorescent yellow": "DFFF11", "pastel yellow": "FDF5A6",
    "neon orange": "FF6A13", "fluorescent orange": "FF7F11", "pumpkin orange": "FF7518", "light orange": "FFB06A",
    "hot pink": "FF69B4", "sakura pink": "F7C5D5", "light pink": "F9C6D0", "pastel pink": "F8C8DC", "neon pink": "FF2E9A",
    "dark purple": "4B1F6F", "light purple": "C3A6E0", "pastel purple": "C8A8E9", "deep purple": "4B2A7A",
    "dark brown": "4A2F25", "light brown": "B07D56", "chocolate brown": "5A3825", "coffee brown": "6F4E37",
    "bone white": "E9E4D4", "ivory white": "F3EEDC", "jade white": "F2F2EC", "pure white": "FFFFFF", "cold white": "F4F8FA",
    "warm white": "F7F1E3", "milky white": "F5F5F0", "pearl white": "F0EDE5", "off white": "F3F1EA",
    "jet black": "0A0A0A", "pure black": "000000", "matte black": "1E1E1E", "charcoal black": "1C1C1E",
    "metallic gold": "D4AF37", "rose gold": "B76E79", "metallic silver": "B8B8B8", "metallic copper": "B87333",
    "glow in the dark": "C8F7A0", "glow green": "C8F7A0", "glow blue": "9DE0F0",
}
MODS = {"light": 0.35, "dark": -0.35, "pastel": 0.5, "deep": -0.25, "bright": 0.15, "pale": 0.45, "soft": 0.25}

# Deutsch → Englisch (für „Namen vereinheitlichen“ und den sprachunabhängigen Abgleich)
DE_EN = {
    "schwarz": "Black", "weiß": "White", "weiss": "White", "grau": "Grey", "silber": "Silver", "rot": "Red", "blau": "Blue",
    "grün": "Green", "gruen": "Green", "gelb": "Yellow", "orange": "Orange", "lila": "Purple", "violett": "Violet",
    "rosa": "Pink", "pink": "Pink", "braun": "Brown", "beige": "Beige", "gold": "Gold", "bronze": "Bronze", "kupfer": "Copper",
    "türkis": "Turquoise", "tuerkis": "Turquoise", "transparent": "Transparent", "natur": "Natural", "elfenbein": "Ivory",
    "hautfarbe": "Skin", "holz": "Wood", "marmor": "Marble", "oliv": "Olive", "mint": "Mint", "flieder": "Lilac",
    "dunkelblau": "Dark Blue", "hellblau": "Light Blue", "himmelblau": "Sky Blue", "marineblau": "Navy Blue",
    "königsblau": "Royal Blue", "koenigsblau": "Royal Blue", "dunkelgrün": "Dark Green", "hellgrün": "Light Green",
    "dunkelgrau": "Dark Grey", "hellgrau": "Light Grey", "dunkelrot": "Dark Red", "weinrot": "Wine Red",
    "ziegelrot": "Brick Red", "hellgelb": "Light Yellow", "dunkelbraun": "Dark Brown", "hellbraun": "Light Brown",
    "olivgrün": "Olive Green", "grasgrün": "Grass Green", "hellrosa": "Light Pink", "dunkellila": "Dark Purple",
    "anthrazit": "Charcoal", "leuchtend": "Neon", "nachtleuchtend": "Glow in the Dark",
}
_DE_HEX = {"schwarz": "000000", "weiß": "FFFFFF", "weiss": "FFFFFF", "grau": "808080", "rot": "D32F2F", "blau": "1E63C9",
           "grün": "2E9E44", "gelb": "F5D300", "lila": "7B3FA0", "rosa": "F06292", "braun": "6D4C41", "silber": "C0C0C0",
           "türkis": "1ABC9C", "natur": "EDE6D6", "anthrazit": "36393F"}


def _mix(hx, amount):
    r, g, b = (int(hx[i:i + 2], 16) for i in (0, 2, 4))
    if amount > 0:
        r, g, b = (round(c + (255 - c) * amount) for c in (r, g, b))
    else:
        r, g, b = (round(c * (1 + amount)) for c in (r, g, b))
    return f"{r:02X}{g:02X}{b:02X}"


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-zäöüß ]", " ", (s or "").lower())).strip()


def from_list(name):
    """Name → (hex, Treffer-Name) aus der eingebauten Liste, oder (None, None)."""
    n = _norm(name)
    if not n:
        return None, None
    for k in sorted(NAMES, key=len, reverse=True):          # „sky blue“ vor „blue“
        if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", n):
            hx = NAMES[k]
            mod = next((m for m in MODS if re.search(r"(?<![a-z])" + m + r"\s" + re.escape(k), n) and m not in k), None)
            return (_mix(hx, MODS[mod]) if mod else hx), (f"{mod} {k}" if mod else k)
    en = to_english(name)
    if en and _norm(en) != n:
        return from_list(en)
    for de, hx in _DE_HEX.items():
        if de in n:
            return hx, de
    return None, None


def to_english(name):
    """„Hellblau“ → „Light Blue“, „Weiß matt“ → „White matt“; unbekannte Wörter bleiben."""
    if not name:
        return name
    out, changed = [], False
    for w in str(name).split():
        en = DE_EN.get(w.lower().strip(".,()"))
        if en:
            out.append(en)
            changed = True
        else:
            out.append(w)
    return " ".join(out) if changed else name


def has_german(name):
    return any(w.lower().strip(".,()") in DE_EN and DE_EN[w.lower().strip(".,()")].lower() != w.lower()
               for w in str(name or "").split())


def find_in_text(text):
    """Farbnamen im (OCR-)Text finden – liefert den Originalnamen wie gedruckt, z. B. „Sky Blue“."""
    low = " " + _norm(text) + " "
    best = None
    for k in sorted(NAMES, key=len, reverse=True):
        m = re.search(r"(?<![a-z])(" + "|".join(MODS) + r")?\s?" + re.escape(k) + r"(?![a-z])", low)
        if m and (best is None or (m.start() < best[0] - 15) or (abs(m.start() - best[0]) <= 15 and len(k) > len(best[1]))):
            best = (m.start(), k, (m.group(1) or "").strip())
    if best:
        name = ((best[2] + " ") if best[2] else "") + best[1]
        return name.title(), from_list(name)[0]
    for de in sorted(DE_EN, key=len, reverse=True):
        if re.search(r"(?<![a-zäöü])" + re.escape(de) + r"(?![a-zäöü])", low):
            return de.capitalize(), from_list(de)[0]
    return None, None
