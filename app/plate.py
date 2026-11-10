"""Plattenerkennung in PrintHub: aktuelles Kamerabild gegen beliebig viele Referenzbilder der leeren Platte.

Vorgehen: Prüfbereich ausschneiden, Graustufen, verkleinern, leicht weichzeichnen, Helligkeit angleichen,
Pixel mit deutlicher Abweichung zählen. Ergebnis = kleinste Abweichung über alle (passenden) Referenzbilder,
damit unterschiedliche Lichtverhältnisse oder Platten je ein eigenes Referenzbild haben können.
"""
import base64
import io
import json
import os
import re
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

PIX_DIFF = 28          # ab dieser Helligkeitsdifferenz (0–255) gilt ein Pixel als verändert
WORK_W = 320           # Arbeitsbreite in Pixeln
GLARE = 1.9            # Pixel heller als das 1,9-Fache der Platte (mind. 170) im Referenzbild = Lichtreflex → ignorieren
MIN_OBJ = 0.08         # Standard: ab diesem Flächenanteil (%) gilt ein einzelnes zusammenhängendes Teil als Objekt
NAME_RE = re.compile(r"^ref_\d+\.jpg$")


class Plate:
    def __init__(self, data_dir):
        self.base = os.path.join(data_dir, "plate")
        self.lock = threading.Lock()

    def _dir(self, pid):
        d = os.path.join(self.base, re.sub(r"[^A-Za-z0-9_-]", "", pid))
        os.makedirs(d, exist_ok=True)
        return d

    def config(self, pid):
        p = os.path.join(self._dir(pid), "plate.json")
        cfg = {"roi": [0.18, 0.35, 0.64, 0.6], "threshold": 2.0, "min_obj": MIN_OBJ, "refs": []}
        if os.path.exists(p):
            try:
                cfg.update(json.load(open(p, encoding="utf-8")))
            except Exception:
                pass
        cfg["refs"] = [r for r in cfg["refs"] if os.path.exists(os.path.join(self._dir(pid), r["file"]))]
        return cfg

    def _save(self, pid, cfg):
        p = os.path.join(self._dir(pid), "plate.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)

    def set_config(self, pid, data):
        with self.lock:
            cfg = self.config(pid)
            if "roi" in data:
                x, y, w, h = [min(1.0, max(0.0, float(v))) for v in data["roi"]]
                if w < 0.05 or h < 0.05:
                    raise ValueError("Prüfbereich zu klein")
                cfg["roi"] = [round(x, 4), round(y, 4), round(min(w, 1 - x), 4), round(min(h, 1 - y), 4)]
            if "poly" in data:   # Vieleck: Liste von Punkten [x, y] (0..1); leer = Rechteck verwenden
                pts = [[round(min(1.0, max(0.0, float(a))), 4), round(min(1.0, max(0.0, float(b))), 4)] for a, b in (data["poly"] or [])]
                if pts and len(pts) < 3:
                    raise ValueError("Mindestens 3 Punkte für den Prüfbereich")
                cfg["poly"] = pts
                if pts:   # Rechteck = umschließender Kasten (für die Anzeige)
                    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                    cfg["roi"] = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
            if "min_obj" in data:
                cfg["min_obj"] = max(0.01, min(20.0, float(str(data["min_obj"]).replace(",", "."))))
            if "threshold" in data:
                cfg["threshold"] = max(0.1, min(100.0, float(str(data["threshold"]).replace(",", "."))))
            self._save(pid, cfg)
            return cfg

    def add_ref(self, pid, jpg, label=""):
        with self.lock:
            cfg = self.config(pid)
            n = max([int(r["file"][4:-4]) for r in cfg["refs"]] + [0]) + 1
            name = f"ref_{n}.jpg"
            Image.open(io.BytesIO(jpg)).convert("RGB").save(os.path.join(self._dir(pid), name), "JPEG", quality=90)
            cfg["refs"].append({"file": name, "label": str(label or "")[:40], "time": int(time.time())})
            self._save(pid, cfg)
            return cfg

    @staticmethod
    def _clean_poly(raw):
        pts = [[round(min(1.0, max(0.0, float(a))), 4), round(min(1.0, max(0.0, float(b))), 4)] for a, b in (raw or [])]
        if pts and len(pts) < 3:
            raise ValueError("Mindestens 3 Punkte für den Prüfbereich")
        return pts

    def update_ref(self, pid, name, data):
        """Beschriftung und/oder eigenes Vieleck eines Referenzbilds (leeres Vieleck = allgemeinen Bereich nutzen)."""
        with self.lock:
            cfg = self.config(pid)
            for r in cfg["refs"]:
                if r["file"] == name:
                    if "label" in data:
                        r["label"] = str(data.get("label") or "")[:40]
                    if "poly" in data:
                        pts = self._clean_poly(data["poly"])
                        if pts:
                            r["poly"] = pts
                        else:
                            r.pop("poly", None)
            self._save(pid, cfg)
            return cfg

    def delete_ref(self, pid, name):
        if not NAME_RE.match(name):
            raise ValueError("Ungültiger Name")
        with self.lock:
            cfg = self.config(pid)
            cfg["refs"] = [r for r in cfg["refs"] if r["file"] != name]
            try:
                os.remove(os.path.join(self._dir(pid), name))
            except OSError:
                pass
            self._save(pid, cfg)
            return cfg

    def ref_path(self, pid, name):
        if not NAME_RE.match(name):
            return None
        p = os.path.join(self._dir(pid), name)
        return p if os.path.exists(p) else None

    # --- Vergleich -----------------------------------------------------------
    @staticmethod
    def _prep(img, roi, poly=None):
        """Ausschnitt (umschließender Kasten) + Maske des Prüfbereichs (Rechteck oder Vieleck)."""
        W, H = img.size
        if poly and len(poly) >= 3:
            xs, ys = [q[0] for q in poly], [q[1] for q in poly]
            x, y, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        else:
            x, y, w, h = roi
        box = (int(x * W), int(y * H), max(int(x * W) + 2, int((x + w) * W)), max(int(y * H) + 2, int((y + h) * H)))
        g = img.crop(box).convert("RGB")
        hh = max(20, int(WORK_W * g.size[1] / max(1, g.size[0])))
        small = g.resize((WORK_W, hh), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.6))
        area = Image.new("L", small.size, 255)
        if poly and len(poly) >= 3:
            area = Image.new("L", small.size, 0)
            sx, sy = small.size[0] / max(1, box[2] - box[0]), small.size[1] / max(1, box[3] - box[1])
            ImageDraw.Draw(area).polygon([((q[0] * W - box[0]) * sx, (q[1] * H - box[1]) * sy) for q in poly], fill=255)
        return small, box, area

    @staticmethod
    def _blobs(mask):
        """Zusammenhängende Flächen der Maske: Liste (Pixel, x0, y0, x1, y1), größte zuerst."""
        w, h = mask.size
        px = bytearray(mask.tobytes())
        out = []
        for start in range(w * h):
            if px[start] != 255:
                continue
            px[start] = 1
            stack, n = [start], 0
            x0 = y0 = 10 ** 9
            x1 = y1 = -1
            while stack:
                i = stack.pop()
                n += 1
                x, y = i % w, i // w
                x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
                for j in (i - 1 if x else -1, i + 1 if x < w - 1 else -1, i - w, i + w):
                    if 0 <= j < w * h and px[j] == 255:
                        px[j] = 1
                        stack.append(j)
            out.append((n, x0, y0, x1, y1))
        out.sort(reverse=True)
        return out

    @classmethod
    def _diff(cls, cur, ref, area=None):
        """Vergleich aktuelles Bild ↔ Referenz (Helligkeit vorher angeglichen).
        Farbe zählt mit: maßgeblich ist der stärkste Unterschied in Rot, Grün oder Blau.
        Stellen, die im Referenzbild ein Lichtreflex sind, werden ignoriert (dort glitzert die Platte jedes Mal anders).
        Rückgabe: (veränderte Fläche %, größtes zusammenhängendes Teil %, Maske, Kasten des größten Teils, Reflexmaske)"""
        mc, mr = sum(ImageStat.Stat(cur).mean) / 3, sum(ImageStat.Stat(ref).mean) / 3
        if mr > 1:
            f = mc / mr
            ref = ref.point(lambda v: max(0, min(255, int(v * f))))
        r, g, b = ImageChops.difference(cur, ref).split()
        d = ImageChops.lighter(ImageChops.lighter(r, g), b)
        mask = d.point(lambda v: 255 if v > PIX_DIFF else 0)
        mask = mask.filter(ImageFilter.MinFilter(3))          # einzelne Rauschpixel weg
        # Lichtreflexe: im Referenzbild deutlich heller als die Platte → mit etwas Rand ausblenden
        rl = ref.convert("L")
        base = ImageStat.Stat(rl, area).median[0] if area is not None else ImageStat.Stat(rl).median[0]
        lim = max(170, base * GLARE)
        glare = rl.point(lambda v: 255 if v > lim else 0).filter(ImageFilter.MaxFilter(7))
        zl = max(120, base * 1.45)   # ganze Reflexzone (weichgezeichnet), damit auch der Rand des Glitzerns wegfällt
        zone = rl.filter(ImageFilter.GaussianBlur(6)).point(lambda v: 255 if v > zl else 0).filter(ImageFilter.MaxFilter(9))
        glare = ImageChops.lighter(glare, zone)
        # in der Reflexzone nur zählen, was deutlich dunkler ist als der Reflex (ein Teil wirft dort einen dunklen Fleck)
        dark = ImageChops.subtract(rl, cur.convert("L")).filter(ImageFilter.MinFilter(3)).point(lambda v: 255 if v > 50 else 0)
        mask = ImageChops.lighter(ImageChops.subtract(mask, glare), ImageChops.multiply(dark, glare))
        total = mask.size[0] * mask.size[1]
        if area is not None:                                   # nur innerhalb des Vielecks zählen
            # Rand des Vielecks etwas einrücken: dort liegt die Plattenkante, die bei kleinstem Versatz „verändert“ aussieht
            inner = area.filter(ImageFilter.MinFilter(7))
            if inner.histogram()[255] > area.histogram()[255] * 0.5:
                area = inner
            mask = ImageChops.multiply(mask, area)
            glare = ImageChops.multiply(glare, area)           # Reflex nur innerhalb der Platte anzeigen
            total = max(1, area.histogram()[255])
        changed = mask.histogram()[255]
        # Teile eines Objekts zusammenfassen (Schraube = mehrere Flecken) und größtes Teil bestimmen
        joined = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        if area is not None:
            joined = ImageChops.multiply(joined, area)
        blobs = cls._blobs(joined)
        big, bbox = 0.0, None
        for n, x0, y0, x1, y1 in blobs:
            bw, bh = x1 - x0 + 1, y1 - y0 + 1
            if max(bw, bh) > 8 * min(bw, bh):                 # lange dünne Streifen = Kante/Versatz, kein Objekt
                continue
            dens = mask.crop((x0, y0, x1 + 1, y1 + 1)).histogram()[255] / (bw * bh)
            if dens < 0.4:                                     # lose Glitzerpunkte, kein festes Teil
                continue
            big, bbox = n / total * 100, (x0, y0, x1, y1)
            break
        return changed / total * 100, big, mask, bbox, glare

    def check(self, pid, jpg, threshold=None, plate_type=""):
        cfg = self.config(pid)
        refs = cfg["refs"]
        if plate_type:   # Referenzbilder für genau diese Platte bevorzugen
            pt = plate_type.lower()
            match = [r for r in refs if r.get("label") and (r["label"].lower() in pt or pt in r["label"].lower())]
            refs = match or refs
        if not refs:
            return {"needs_calibration": True, "is_empty": None, "message": "Noch kein Referenzbild der leeren Platte"}
        cur_img = Image.open(io.BytesIO(jpg)).convert("RGB")
        results = []
        best = None
        for r in refs:
            poly = r.get("poly") or cfg.get("poly") or None     # eigenes Vieleck des Referenzbilds hat Vorrang
            try:
                cur, box, area = self._prep(cur_img, cfg["roi"], poly)
                ref_img = Image.open(os.path.join(self._dir(pid), r["file"])).convert("RGB")
                if ref_img.size != cur_img.size:
                    ref_img = ref_img.resize(cur_img.size)
                ref, _, _ = self._prep(ref_img, cfg["roi"], poly)
                pct, big, mask, bbox, glare = self._diff(cur, ref, area)
            except Exception as e:
                results.append({"file": r["file"], "label": r.get("label", ""), "error": str(e)})
                continue
            results.append({"file": r["file"], "label": r.get("label", ""), "diff": round(pct, 2), "obj": round(big, 2)})
            if best is None or pct < best[0]:
                best = (pct, r, mask, box, poly, big, bbox, glare, cur.size)
        if best is None:
            return {"is_empty": None, "message": "Vergleich fehlgeschlagen", "refs": results}
        thr = float(threshold) if threshold not in (None, "") else float(cfg["threshold"])
        pct, ref, mask, box, poly, big, bbox, glare, wsize = best
        min_obj = float(cfg.get("min_obj") or MIN_OBJ)
        empty = pct <= thr and big < min_obj
        # Analysebild: Prüfbereich + veränderte Stellen rot
        over = cur_img.copy()
        m = mask.resize((box[2] - box[0], box[3] - box[1]))
        red = Image.new("RGB", m.size, (239, 68, 68))
        region = over.crop(box)
        region = Image.composite(Image.blend(region, red, 0.65), region, m)
        gm = glare.resize(m.size).point(lambda v: 90 if v else 0)   # ausgeblendete Reflexe leicht gelb
        region = Image.composite(Image.new("RGB", m.size, (250, 204, 21)), region, gm)
        over.paste(region, box[:2])
        dr = ImageDraw.Draw(over)
        col, lw = (34, 197, 94) if empty else (239, 68, 68), max(2, over.size[0] // 320)
        if bbox and big > 0.02:   # größtes Teil einrahmen
            fx, fy = (box[2] - box[0]) / wsize[0], (box[3] - box[1]) / wsize[1]
            pad = 4
            dr.rectangle((box[0] + bbox[0] * fx - pad, box[1] + bbox[1] * fy - pad, box[0] + (bbox[2] + 1) * fx + pad,
                          box[1] + (bbox[3] + 1) * fy + pad), outline=(239, 68, 68) if big >= min_obj else (250, 204, 21), width=lw)
        if poly:
            W, H = over.size
            pts = [(q[0] * W, q[1] * H) for q in poly]
            dr.line(pts + [pts[0]], fill=col, width=lw, joint="curve")
        else:
            dr.rectangle(box, outline=col, width=lw)
        buf = io.BytesIO()
        over.save(buf, "JPEG", quality=82)
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        why = (f"Objekt {big:.2f} % ≥ {min_obj:g} %" if big >= min_obj else f"Fläche {pct:.1f} % > {thr:g} %") if not empty else ""
        return {"is_empty": empty, "difference_percent": round(pct, 2), "threshold": thr, "source": "printhub",
                "object_percent": round(big, 2), "min_obj": min_obj, "reason": why,
                "best_ref": ref["file"], "best_label": ref.get("label", ""), "refs": results,
                "message": f"{'Platte leer' if empty else 'Etwas liegt auf der Platte'} (größtes Teil {big:.2f} % / Grenze {min_obj:g} %, "
                           f"Fläche {pct:.1f} % / Schwelle {thr:g} %, "
                           f"bestes Referenzbild {ref.get('label') or ref['file']})",
                "debug_image": "data:image/jpeg;base64," + img_b64}
