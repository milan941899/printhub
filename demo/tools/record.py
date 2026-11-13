"""Nimmt die API-Antworten einer laufenden Demo-Instanz (serve_demo.py) auf → demo/fixtures.json + demo/img/.

Aufruf:  python3 demo/tools/record.py http://127.0.0.1:8798 [Etikett-Fotos …]
Die Fotos werden nur ausgewertet; gespeichert wird nur das Ergebnis (kein Bild).
"""
import hashlib
import json
import os
import re
import sys
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
PHOTOS = sys.argv[2:]
OUT = os.path.join(os.path.dirname(__file__), "..")
IMG = os.path.join(OUT, "img")
os.makedirs(IMG, exist_ok=True)
fix, scans = {}, []
# Sicherheitsnetz: keine echten Heimnetz-Adressen in die Demo; weitere Begriffe über DEMO_SECRETS="a|b" (nicht ins Repo!)
SECRET = re.compile(r"192\.168\.\d+\.\d+" + ("|" + os.environ["DEMO_SECRETS"] if os.environ.get("DEMO_SECRETS") else ""), re.I)


def keep(resp):
    u = resp.url
    if not u.startswith(BASE):
        return
    path = u[len(BASE):]
    if not (path.startswith("/api/") or path.startswith("/media/")):
        return
    m = resp.request.method
    if path.startswith("/api/lager/scan"):
        return
    if m != "GET":
        return
    path = unquote(re.sub(r"([?&])(t|_)=\d+", r"\1", path).rstrip("?&").replace("?&", "?").replace("&&", "&"))
    ct = resp.headers.get("content-type", "")
    try:
        if ct.startswith("image/"):
            body = resp.body()
            name = hashlib.sha1(body).hexdigest()[:12] + (".png" if "png" in ct else ".svg" if "svg" in ct else ".jpg")
            open(os.path.join(IMG, name), "wb").write(body)
            fix["GET " + path] = {"img": name, "type": ct}
        elif "json" in ct:
            fix["GET " + path] = resp.json()
        elif "text" in ct:
            fix["GET " + path] = {"text": resp.text(), "type": ct}
    except Exception as e:
        print("skip", path, e)


with sync_playwright() as p:
    b = p.chromium.launch()
    for w, h in ((1400, 1000), (400, 850)):
        pg = b.new_context(viewport={"width": w, "height": h}).new_page()
        pg.on("response", keep)
        pg.on("dialog", lambda d: d.accept())
        for u in ("/", "/lager", "/lager?ansicht=archiv", "/verlauf", "/verlauf#warteschlange", "/statistik", "/etiketten?spools=1,3,8",
                  "/assign?spool=2"):
            pg.goto(BASE + u)
            pg.wait_for_timeout(3500)
        pg.goto(BASE + "/einstellungen")
        pg.wait_for_timeout(2500)
        for t in ("allgemein", "drucker", "dienste", "wartung", "waechter", "spulen", "kosten", "meldungen", "zugriff", "daten", "diagnose", "system"):
            pg.evaluate(f"location.hash = '{t}'")
            pg.wait_for_timeout(1500)
        pg.goto(BASE + "/lager")
        pg.wait_for_timeout(2500)
        for js in ("openFil()", "closeM(); openFil(5)", "closeM(); openMulti()", "closeM(); openAdd(5)", "closeM(); openShop()", "closeM()"):
            pg.evaluate(js)
            pg.wait_for_timeout(1200)
        for v, m, c in (("JAYO", "PETG", "Brick Red"), ("Bambu Lab", "PLA", "Matte Charcoal"), ("Sunlu", "PLA+", "Black"),
                        ("Polymaker", "PLA", "HT-PLA Pro Blue"), ("GEEETECH", "PETG", "Green"), ("JAYO", "PETG", "Vibrant Orange")):
            for q in (f"vendor={v}&material={m}&color={c}",):
                pg.evaluate(f"fetch('/api/lager/defaults?{q}')")
        pg.wait_for_timeout(1500)
        if w == 1400:
            for ph in PHOTOS:
                pg.evaluate("scanStart()")
                pg.set_input_files("#scan-file", ph)
                pg.wait_for_selector("input[name=scf]", timeout=90000)
                pg.wait_for_timeout(500)
                scans.append(pg.evaluate("JSON.parse(JSON.stringify(scanCtx))"))
                pg.evaluate("closeM()")
    b.close()

# Bilder, deren Inhalt der Browser schon verworfen hatte (z. B. Kamera), direkt nachladen
import urllib.request
for key, v in list(fix.items()):
    if isinstance(v, dict) and v.get("img") and not os.path.getsize(os.path.join(IMG, v["img"])):
        body = urllib.request.urlopen(BASE + key[4:].replace(" ", "%20"), timeout=30).read()
        name = hashlib.sha1(body).hexdigest()[:12] + ".jpg"
        open(os.path.join(IMG, name), "wb").write(body)
        v["img"] = name
for f in os.listdir(IMG):
    if not os.path.getsize(os.path.join(IMG, f)):
        os.remove(os.path.join(IMG, f))
txt = json.dumps({"fix": fix, "scans": scans}, ensure_ascii=False)
if SECRET.search(txt):
    sys.exit("Abbruch: echte IP/Geheimnis in den Aufnahmen gefunden: " + SECRET.search(txt).group(0))
open(os.path.join(OUT, "fixtures.json"), "w").write(txt)
print(len(fix), "Antworten,", len(scans), "Scans,", len(txt) // 1024, "KB")
