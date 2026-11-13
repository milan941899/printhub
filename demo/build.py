"""Baut die statische Demo (GitHub Pages) aus app/static + demo/.

Aufruf:  python3 demo/build.py <Zielordner> [/basis-pfad/]      z. B.  python3 demo/build.py site /printhub/
Der echte Code bleibt unverändert; nur Pfade werden auf den Basis-Pfad umgestellt und demo.js beantwortet die API.
"""
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "..", "app", "static")
OUT = sys.argv[1] if len(sys.argv) > 1 else "site"
BASE = (sys.argv[2] if len(sys.argv) > 2 else "/printhub/").rstrip("/") + "/"

PAGES = {"index.html": "index.html", "lager.html": "lager.html", "history.html": "verlauf.html", "statistik.html": "statistik.html",
         "settings.html": "einstellungen.html", "etiketten.html": "etiketten.html", "assign.html": "assign.html"}
ABS = re.compile(r"""([\"'`(])/(api|static|media|lager|verlauf|statistik|einstellungen|etiketten|assign|login)\b""")


def rewrite(txt):
    txt = ABS.sub(r"\1\2", txt)
    txt = txt.replace('href="/"', 'href="./"')
    txt = re.sub(r'<link rel="manifest"[^>]*>', "", txt)
    return txt


shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(os.path.join(OUT, "demo", "img"))
shutil.copytree(STATIC, os.path.join(OUT, "static"))
for js in ("common.js", "ui.js"):
    p = os.path.join(OUT, "static", js)
    t = rewrite(open(p, encoding="utf-8").read())
    if js == "common.js":
        t = t.replace('const path = location.pathname.replace(/\\/$/, "") || "/";',
                      'const path = ("/" + location.pathname.slice(new URL(document.baseURI).pathname.length)).replace(/\\.html$/, "").replace(/\\/$/, "") || "/";')
        t = t.replace('const h = a.getAttribute("href").replace(/\\/$/, "") || "/";',
                      'const h = ("/" + a.getAttribute("href").replace(/^\\.?\\//, "")).replace(/\\/$/, "") || "/";')
        t = re.sub(r'navigator\.serviceWorker\.register\([^)]*\)', "Promise.resolve()", t)
    open(p, "w", encoding="utf-8").write(t)
os.remove(os.path.join(OUT, "static", "sw.js"))

head = (f'<base href="{BASE}">\n<script src="demo/fixtures.js"></script>\n<script src="demo/demo.js"></script>\n'
        f'<meta name="robots" content="noindex">\n')
for src, dst in PAGES.items():
    t = rewrite(open(os.path.join(STATIC, src), encoding="utf-8").read())
    t = t.replace("<head>", "<head>\n" + head, 1)
    open(os.path.join(OUT, dst), "w", encoding="utf-8").write(t)
    os.remove(os.path.join(OUT, "static", src))

fx = json.load(open(os.path.join(ROOT, "fixtures.json"), encoding="utf-8"))
open(os.path.join(OUT, "demo", "fixtures.js"), "w", encoding="utf-8").write("window.PH_DEMO=" + json.dumps(fx, ensure_ascii=False) + ";")
shutil.copy(os.path.join(ROOT, "demo.js"), os.path.join(OUT, "demo", "demo.js"))
for f in os.listdir(os.path.join(ROOT, "img")):
    shutil.copy(os.path.join(ROOT, "img", f), os.path.join(OUT, "demo", "img", f))
# Bilder, die per <img src> geladen werden (nicht über fetch): als Datei unter ihrem Pfad ablegen
for key, v in fx["fix"].items():
    if isinstance(v, dict) and v.get("img"):
        path = key[4:].split("?")[0].lstrip("/")
        dst = os.path.join(OUT, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(os.path.join(ROOT, "img", v["img"]), dst)
open(os.path.join(OUT, ".nojekyll"), "w").close()
print("Demo gebaut:", OUT, "Basis", BASE)
