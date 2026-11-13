# PrintHub-Demo (GitHub Pages)

Die Demo ist die echte Oberfläche aus `app/static`. Statt eines Servers beantwortet `demo.js` alle `/api/`-Aufrufe im Browser
aus aufgenommenen Beispieldaten (`fixtures.json`, Bilder in `img/`). Nichts wird gespeichert.

- **Bauen:** `python3 demo/build.py site /printhub/` – macht die GitHub Action `demo.yml` bei jedem Push automatisch.
- **Daten neu aufnehmen** (nur nötig, wenn sich API-Antworten ändern):
  1. `python3 demo/tools/fakesm_demo.py &` (Mini-Spoolman mit Demo-Lager)
  2. `PRINTHUB_DEV=1 PYTHONPATH=. python3 demo/tools/serve_demo.py &` (PrintHub mit erfundenen Druckern/Verlauf)
  3. `python3 demo/tools/record.py http://127.0.0.1:8798 <Etikett-Fotos …>` (nur die Scan-Ergebnisse werden gespeichert, keine Fotos)

`record.py` bricht ab, wenn eine Heimnetz-Adresse (192.168.x.x) in den Aufnahmen steckt.
