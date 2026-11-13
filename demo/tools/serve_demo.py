"""PrintHub mit erfundenen Demo-Daten starten (für die Aufnahme der Demo-Antworten). Nur lokal."""
import sys, types, os, time, random, sqlite3, io
D = os.environ.get("DEMO_DIR", "/tmp/demo")
os.environ["CONFIG"] = os.path.join(os.path.dirname(__file__), "config.demo.yaml")
os.environ["DATA"] = os.path.join(D, "data/state.json")
for n in ["paho", "paho.mqtt", "paho.mqtt.client"]:
    sys.modules[n] = types.ModuleType(n)
import app.main as M
from PIL import Image, ImageDraw
random.seed(3)
a1, vo = M.printers["a1"], M.printers["vo"]
for p in (a1, vo):
    p.start = lambda: None


class FakeCam:
    def snapshot(self, wait=10):
        im = Image.new("RGB", (1280, 720), (28, 30, 36)); d = ImageDraw.Draw(im)
        d.polygon([(220, 380), (1060, 380), (1220, 720), (60, 720)], fill=(70, 72, 80))
        for i in range(0, 1280, 48): d.line([(i, 380), (i - 120 + i // 6, 720)], fill=(62, 64, 72))
        for k in range(8):   # Druckteil wächst (Schachfigur-artig)
            w = 120 - k * 9; y = 600 - k * 26
            d.rounded_rectangle([640 - w, y - 26, 640 + w, y], 6, fill=(31 + k * 3, 41 + k * 3, 55 + k * 3))
        d.ellipse([600, 340, 680, 420], fill=(40, 52, 68))
        d.rectangle([560, 0, 720, 230], fill=(50, 50, 56)); d.polygon([(610, 230), (670, 230), (640, 280)], fill=(210, 160, 40))
        b = io.BytesIO(); im.save(b, "JPEG", quality=80); return b.getvalue()


a1.camera = FakeCam(); a1.online = True; a1.last_msg = time.time() + 1e9
a1.state = {"gcode_state": "RUNNING", "mc_percent": 62, "mc_remaining_time": 74, "subtask_name": "Schachfiguren Set", "layer_num": 184,
            "total_layer_num": 296, "nozzle_temper": 219, "nozzle_target_temper": 220, "bed_temper": 65, "bed_target_temper": 65,
            "cooling_fan_speed": "12", "spd_lvl": 2, "spd_mag": 100, "lights_report": [{"node": "chamber_light", "mode": "on"}],
            "ams": {"tray_now": "0", "ams": [{"id": "0", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "36393FFF"}, {"id": "1", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                {"id": "2", "tray_type": "PLA", "tray_color": "4A4A4CFF"}, {"id": "3", "tray_type": "PLA", "tray_color": "F7D959FF"}]}]}}
a1.set_light = lambda on: a1.state.update(lights_report=[{"node": "chamber_light", "mode": "on" if on else "off"}])
vo.online = True
vo.status_data = {"print_stats": {"state": "standby"}, "extruder": {"temperature": 31, "target": 0},
                  "heater_bed": {"temperature": 27, "target": 0}, "output_pin caselight": {"value": 1}}
for key, sid in [("ams0-0", 1), ("ams0-1", 3), ("ams0-2", 6), ("ams0-3", 7)]:
    M.store.set_slot("a1", key, sid)
try:
    M.store.set_slot("vo", vo.slots()[0]["key"], 8)
except Exception as e:
    print("voron slot", e)
M.store.set_job("a1", {"name": "Schachfiguren Set", "started": int(time.time()) - 2 * 3600, "filaments": [], "max_progress": 0.62})
M.store.data["min_stock"] = {"2": 3, "6": 2, "9": 2, "12": 2}

now = time.time()
with M.temps.lock:
    from collections import deque
    for i in range(180):
        t = now - 900 + i * 5
        M.temps.fine.setdefault("a1", deque(maxlen=1440)).append((int(t), {"Düse": [218 + 3 * random.random(), 220], "Bett": [64.5 + random.random(), 65]}))
        M.temps.fine.setdefault("vo", deque(maxlen=1440)).append((int(t), {"Düse": [31 + random.random(), 0], "Bett": [27, 0]}))
db = sqlite3.connect(M.HISTORY_DB); h0 = int(now // 3600 * 3600)
for i in range(24 * 120):
    hh = h0 - i * 3600
    for pid, scale in (("a1", 1), ("vo", 2.2)):
        wh = random.choice([0, 0, 0, 4, 90, 110, 130]) * scale if 7 < (i % 24) < 23 else 3
        db.execute("INSERT OR REPLACE INTO energy VALUES(?,?,?)", (hh, pid, wh))
db.commit(); db.close()
jobs = ["Schachfiguren Set", "Kabelhalter", "Handyhalter", "Vase Spirale", "Zahnrad Ersatzteil", "Gehäuse Raspberry Pi", "Wandhaken",
        "Pflanztopf", "Schlüsselanhänger", "Werkzeughalter", "Benchy", "Lampenschirm", "Halterung Kamera", "Briefständer"]
sp = {1: ("PLA", "#36393F", "Bambu Lab Matte Charcoal"), 3: ("PLA", "#FFFFFF", "Bambu Lab Jade White"), 8: ("PETG", "#1F2937", "JAYO Black"),
      12: ("PETG", "#9CA3AF", "JAYO Gray"), 16: ("PLA+", "#000000", "Sunlu Black")}
for i in range(70):
    pr = "Bambu A1" if i % 3 else "Voron 2.4 (Klipper)"
    s = random.choice([1, 3, 16]) if pr == "Bambu A1" else random.choice([8, 12])
    g = random.randint(12, 260); dur = int(g * random.uniform(55, 80)); kwh = round(dur / 3600 * (0.11 if pr == "Bambu A1" else 0.24), 2)
    ok = random.random() > 0.1
    fc = round(g / 1000 * 18, 2); ec = round(kwh * 0.32, 2)
    M.history.add({"time": int(now - i * 86400 * 1.3 - random.randint(0, 40000)), "printer": pr, "job": random.choice(jobs),
                   "result": "fertig" if ok else "abgebrochen bei %d %%" % random.randint(10, 80),
                   "usage": [{"slot": "AMS Slot 1" if pr == "Bambu A1" else "Extruder", "grams": g, "spool": s, "booked": True,
                              "material": sp[s][0], "color": sp[s][1], "spool_name": sp[s][2], "cost": fc}],
                   "estimate": dur, "duration": dur if ok else dur // 2, "kwh": kwh, "energy_cost": ec, "filament_cost": fc, "total_cost": round(fc + ec, 2)})
M.dockerctl.SOCK = "/nonexistent.sock"
M.settings.update({"price_kwh": 0.32, "label_host": "", "ai_enabled": False, "public_url": "https://milan941899.github.io/printhub"})
M._apply_services()
import uvicorn
uvicorn.run(M.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8798")), log_level="warning")
