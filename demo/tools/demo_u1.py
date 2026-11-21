"""Snapmaker U1 + gemeinsame Warteschlange (Bambuddy + Moonraker) in die Demo-Daten einbauen.

Aufruf:  python3 demo/tools/demo_u1.py          (ändert demo/fixtures.json + demo/img/, mehrfach ausführbar)
Läuft automatisch am Ende von record.py. Alles erfundene Daten – keine echten Adressen.
"""
import hashlib
import io
import json
import os
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FX = os.path.join(ROOT, "fixtures.json")
IMG = os.path.join(ROOT, "img")
MACRO = os.path.join(ROOT, "..", "app", "klipper", "printhub-startsperre.cfg")
NAME = "Snapmaker U1"
U1_SLOTS = [(8, "JAYO Black PETG"), (11, "JAYO Gray PETG"), (14, "JAYO Brick Red PETG"), (15, "JAYO Electric Blue PETG")]


def walk(o, fn):
    """Alle Strings ersetzen, „vo“-Kürzel → „u1“."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            k2 = "u1" if k == "vo" else fn(k)
            out[k2] = "u1" if k in ("id", "printer_id") and v == "vo" else walk(v, fn)
        return out
    if isinstance(o, list):
        return [walk(x, fn) for x in o]
    if isinstance(o, str):
        return fn(o)
    return o


def rename(s):
    s = s.replace("Voron 2.4 (Klipper)", NAME).replace("Voron – Extruder", "U1 – Kopf 1").replace("/printers/vo/", "/printers/u1/")
    s = s.replace("/plate/vo", "/plate/u1")
    return "Kopf 1 (T0)" if s == "Extruder" else s


def cam_image():
    """Erfundenes Kamerabild: leere Druckplatte im U1 (für Plattenerkennung/Kamera)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1280, 720), (24, 26, 31))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1280, 120], fill=(36, 38, 44))
    for i, c in enumerate([(31, 41, 55), (156, 163, 175), (155, 58, 46), (43, 59, 255)]):   # 4 Werkzeugköpfe im Parkplatz
        x = 260 + i * 210
        d.rounded_rectangle([x, 20, x + 150, 110], 10, fill=(58, 60, 68))
        d.rectangle([x + 60, 100, x + 90, 130], fill=c)
    d.polygon([(200, 330), (1080, 330), (1230, 700), (50, 700)], fill=(62, 66, 58))      # PEI-Platte
    for k in range(0, 900, 30):
        d.line([(200 + k, 330), (50 + k * 1.31, 700)], fill=(58, 62, 54))
    d.text((40, 680), "Snapmaker U1 · Demo-Kamera", fill=(140, 150, 160))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


def save_img(data, ext="jpg"):
    name = hashlib.sha1(data).hexdigest()[:12] + "." + ext
    with open(os.path.join(IMG, name), "wb") as f:
        f.write(data)
    return name


def u1_printer(old, spools):
    slots = []
    for i, (sid, _) in enumerate(U1_SLOTS):
        sp = dict(spools.get(sid) or {})
        if sp:
            sp["location"] = f"U1 – Kopf {i + 1}"
            sp["slot"] = f"{NAME} – Kopf {i + 1} (T{i})"
        slots.append({"key": f"T{i}", "label": f"Kopf {i + 1} (T{i})", "material": (sp.get("material") or "") if sp else "",
                      "color": (sp.get("color") or "") if sp else "", "vendor": (sp.get("vendor") or "") if sp else "",
                      "rfid": False, "printer_spool": sid or 0, "spool_id": sid, "spool": sp or None})
    p = dict(old)
    p.update({
        "id": "u1", "name": NAME, "type": "moonraker", "online": True, "state": "Bereit", "printing": False, "paused": False,
        "temps": [{"label": f"T{i}", "actual": 27 + i % 2, "target": 0} for i in range(4)] + [{"label": "Bett", "actual": 26, "target": 0},
                                                                                    {"label": "Bauraum", "actual": 24, "target": 0}],
        "fans": [{"label": "Bauteil", "pct": 0}, {"label": "Hotend", "pct": 0}],
        "chamber_fans": {"Abluft": 0, "Umluft": 0, "Bauraum-Lüfter": 0},
        "slots": slots, "camera": True, "light": False, "shelly": True, "maint_due": 0,
        "power": {**(old.get("power") or {}), "available": True, "on": True, "watts": 9.4, "auto": True, "mode": "shelly", "plug": "Shelly U1"},
        "energy": {**(old.get("energy") or {}), "watts": 9.4, "plug_on": True, "plug_online": True, "today_kwh": 0.21, "today_cost": 0.07},
        "queue": {"src": "moonraker", "state": "paused", "pending": 2, "lock": True},
    })
    return p


def main():
    data = json.load(open(FX, encoding="utf-8"))
    data = walk(data, rename)
    F = data["fix"]
    now = int(time.time())

    # --- Spulen in den 4 Köpfen ------------------------------------------------
    loc = {sid: (f"U1 – Kopf {i + 1}", f"{NAME} – Kopf {i + 1} (T{i})") for i, (sid, _) in enumerate(U1_SLOTS)}
    for key in ("GET /api/spools", "GET /api/spools?fresh=1"):
        for s in F.get(key) or []:
            if s.get("id") in loc:
                s["location"], s["slot"] = loc[s["id"]]
    for fil in (F.get("GET /api/lager?archived=1") or {}).get("filaments", []):
        for s in fil.get("spools", []):
            if s.get("id") in loc:
                s["location"], s["in_printer"] = loc[s["id"]][1], True
    spools = {s["id"]: s for s in F.get("GET /api/spools") or []}

    # --- Drucker-Kachel ----------------------------------------------------------
    st = F["GET /api/state"]
    st["printers"] = [u1_printer(p, spools) if p["id"] == "u1" else p for p in st["printers"]]
    st["queue"] = True

    # --- Kamera / Plattenerkennung ----------------------------------------------
    cam = save_img(cam_image())
    for k in ("GET /api/printers/u1/camera.jpg", "GET /api/plate/u1/snapshot.jpg"):
        F[k] = {"img": cam, "type": "image/jpeg"}
    F["GET /api/plate/u1"] = {"roi": [0.1, 0.46, 0.8, 0.5], "threshold": 2.0, "min_obj": 0.08, "refs": [], "camera": True}

    # --- Drucker-Einstellungen ----------------------------------------------------
    pc = F["GET /api/printer-config"]
    for p in pc["printers"]:
        if p["id"] == "u1":
            p.update(host="10.0.0.21", port=7125, channels=4, camera=True, light_pin="", shelly="10.0.0.31")

    # --- Gemeinsame Warteschlange -------------------------------------------------
    chk_ok = {"state": "ok", "text": "Filament passt (4 Köpfe)", "lines": [
        {"type": "PETG", "color": "#1F2937", "grams": 41.2, "slot": "Kopf 1 (T0)", "have": 690, "state": "ok", "msg": ""},
        {"type": "PETG", "color": "#9CA3AF", "grams": 22.5, "slot": "Kopf 2 (T1)", "have": 1100, "state": "ok", "msg": ""},
        {"type": "PETG", "color": "#9B3A2E", "grams": 12.8, "slot": "Kopf 3 (T2)", "have": 1100, "state": "ok", "msg": ""},
        {"type": "PETG", "color": "#2B3BFF", "grams": 9.6, "slot": "Kopf 4 (T3)", "have": 1100, "state": "ok", "msg": ""}]}
    chk_warn = {"state": "warn", "text": "Kopf 3: Farbe weicht ab (Rot statt Weiß)", "lines": [
        {"type": "PETG", "color": "#1F2937", "grams": 118.0, "slot": "Kopf 1 (T0)", "have": 690, "state": "ok", "msg": ""},
        {"type": "PETG", "color": "#F5F5F5", "grams": 34.0, "slot": "Kopf 3 (T2)", "have": 1100, "state": "warn", "msg": "Farbe in Kopf 3 (T2) weicht ab"}]}
    chk_a1 = {"state": "ok", "text": "Filament passt", "lines": [
        {"type": "PLA", "color": "#FFFFFF", "grams": 38.0, "slot": "AMS Slot 2", "have": 388, "state": "ok", "msg": ""}]}
    items = [
        {"id": 41, "src": "bambuddy", "name": "Schachfiguren Set", "status": "printing", "printer": "Bambu A1", "seconds": 14470, "grams": 193,
         "position": 0, "manual": True},
        {"id": "u1:0000C4", "src": "moonraker", "name": "Kabelclip 4-farbig", "status": "pending", "printer": NAME, "seconds": 5280, "grams": 86,
         "position": 1, "manual": True, "waiting": "Druckwächter prüft die Platte", "check": chk_ok,
         "plan": {"text": "startet jetzt – Druckwächter: Einschalten, Licht, Plattenprüfung, Freigabe", "start": now}},
        {"id": 42, "src": "bambuddy", "name": "Kabelhalter", "status": "pending", "printer": "Bambu A1", "seconds": 3300, "grams": 38,
         "position": 2, "manual": True, "check": chk_a1, "plan": {"text": "Start nach dem laufenden Druck, ca. in 1 h 15 min", "start": now + 4500}},
        {"id": "u1:0000C5", "src": "moonraker", "name": "Gehäuse Raspberry Pi", "status": "pending", "printer": NAME, "seconds": 15600, "grams": 152,
         "position": 3, "manual": True, "check": chk_warn, "scheduled": None,
         "plan": {"text": "morgen 07:00 – passt heute nicht mehr ins Druckfenster", "start": now + 15 * 3600}},
    ]
    F["GET /api/queue"] = {"enabled": True, "items": items, "url": "http://10.0.0.10:8000", "waechter": True,
                           "schedule": {"printing_until": "in 1 h 14 min", "end": "morgen 11:20", "window": "heute 07:00–22:30"}}
    ev = [
        (-60, "Kabelclip 4-farbig", "Platte leer ✓ – Start am U1 freigegeben", "Druckwächter"),
        (-150, "Kabelclip 4-farbig", "eingeschaltet, Licht an, prüfe Platte …", "Druckwächter"),
        (-240, "Kabelclip 4-farbig", "in die Warteschlange übernommen (aus Orca hochgeladen)", "Moonraker"),
        (-1500, "Gehäuse Raspberry Pi", "Start am Display abgewiesen (Startsperre) – liegt in der Warteschlange", "Moonraker"),
        (-1560, "Gehäuse Raspberry Pi", "in die Warteschlange übernommen (aus Orca hochgeladen)", "Moonraker"),
        (-2 * 3600, "Schachfiguren Set", "gestartet", "Druckwächter"),
        (-2 * 3600 - 90, "Schachfiguren Set", "Platte leer ✓", "Druckwächter"),
        (-5 * 3600, "Wandhaken", "fertig", "Moonraker"),
        (-8 * 3600, "Wandhaken", "gestartet", "Druckwächter"),
        (-8 * 3600 - 300, "Wandhaken", "wartet – Platte belegt (Rückfrage 1 von 3)", "Druckwächter"),
        (-26 * 3600, "Kabelhalter", "nach vorne geholt (Telegram)", "PrintHub"),
        (-27 * 3600, "Vase Spirale", "fertig", "Bambuddy"),
    ]
    F["GET /api/queue/history"] = {"enabled": True, "entries": [
        {"time": now + dt, "id": None, "name": n, "text": t, "src": s} for dt, n, t, s in ev]}

    # --- Druckwächter läuft, kennt den U1 -------------------------------------------
    nr = F["GET /api/nachtruhe"]
    nr.update(enabled=True, running=True, since=now - 3 * 86400, config_exists=True)
    nr["config"].update(DRY_RUN="0", PLATTE_QUELLE="printhub", BAMBUDDY_URL="http://10.0.0.10:8000", BAMBUDDY_API_KEY_set=True)
    nr["klipper"] = [{"id": "u1", "name": NAME, "online": True, "queue": True, "queue_state": "pausiert – wartet auf den Druckwächter",
                      "lock": True, "lock_version": 1,
                      "blocked": "„Gehäuse Raspberry Pi“ vom Display, vor 25 min"}]
    nr["klipper_macro"] = open(MACRO, encoding="utf-8").read()
    t = lambda dt: time.strftime("%d.%m. %H:%M:%S", time.localtime(now + dt))
    nr["log"] = [f"{t(dt)} {m}" for dt, m in [
        (-1560, "[U1] Neue Datei „Gehäuse Raspberry Pi.gcode“ → Moonraker-Warteschlange"),
        (-1500, "[U1] Startsperre: Start vom Display abgewiesen – Auftrag bleibt in der Warteschlange"),
        (-1490, "[U1] „Gehäuse Raspberry Pi“: 4 h 20 min, passt heute nicht mehr ins Druckfenster → morgen 07:00"),
        (-240, "[U1] Neue Datei „Kabelclip 4-farbig.gcode“ → Moonraker-Warteschlange"),
        (-235, "[U1] „Kabelclip 4-farbig“ passt ins Druckfenster (fertig ca. 14:35)"),
        (-150, "[U1] Shelly eingeschaltet, Licht an"),
        (-90, "[U1] Plattenprüfung (PrintHub): 0,4 % Abweichung → leer"),
        (-60, "[U1] Freigabe: PRINTHUB_FREIGABE → Start „Kabelclip 4-farbig.gcode“"),
    ]]

    # --- Dienste-Leiste -------------------------------------------------------------
    for s in F["GET /api/services"]["services"]:
        if s["id"] == "bambuddy":
            s.update(online=True, lines=["Version 0.9 · Warteschlange 2"], url="http://10.0.0.10:8000", version="0.9", ms=14)
        if s["id"] == "nachtruhe":
            s.update(online=True, lines=["läuft · A1 + U1"])
    if not any(s["id"] == "moonraker-u1" for s in F["GET /api/services"]["services"]):
        F["GET /api/services"]["services"].insert(2, {"id": "moonraker-u1", "name": "Moonraker (U1)", "icon": "🟧", "containers": None,
                                                     "online": True, "lines": ["Warteschlange 2 · Startsperre aktiv"],
                                                     "url": "http://10.0.0.21", "ms": 9})
    F["GET /api/settings"]["settings"]["bambuddy_url"] = "http://10.0.0.10:8000"

    txt = json.dumps(data, ensure_ascii=False)
    open(FX, "w", encoding="utf-8").write(txt)
    print("U1-Demo eingebaut:", len(F), "Antworten,", len(txt) // 1024, "KB")


if __name__ == "__main__":
    main()
