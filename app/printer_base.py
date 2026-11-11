"""Gemeinsame Logik für alle Drucker: Slots, Job-Erkennung, Verbrauch buchen."""
import logging
import threading
import time

log = logging.getLogger("printer")


def _de(v, n=2):
    return f"{v:.{n}f}".replace(".", ",")


class BasePrinter:
    type = "base"

    def __init__(self, cfg, store, spoolman):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.store = store
        self.spoolman = spoolman
        self.track_usage = cfg.get("track_usage", True)
        self.online = False
        self.lock = threading.RLock()
        self.last_error = ""
        self.camera = None
        self.notifier = None   # wird in main.py gesetzt
        self.cool_watch = None   # nach Druckende: Bett-Abkühlung beobachten
        self.offline_since = None
        self.shelly = None     # optional: Shelly-Steckdose vor dem Drucker
        self.settings = None   # Settings-Objekt (main.py)
        self.history = None    # History-Objekt (main.py)
        self.media_dir = None  # Ordner für Modellbilder/Fotos (main.py)
        self.on_job_start = []  # Rückrufe bei neuem Druck (z. B. OctoEverywhere neu starten)

    # --- Hilfen ----------------------------------------------------------------
    def save_media(self, prefix, data, ext):
        """Speichert ein Bild im Medienordner und gibt den Dateinamen zurück."""
        if not self.media_dir or not data:
            return None
        import os
        os.makedirs(self.media_dir, exist_ok=True)
        name = f"{self.id}-{prefix}-{int(time.time() * 1000)}.{ext}"
        with open(os.path.join(self.media_dir, name), "wb") as f:
            f.write(data)
        return name

    def take_photo(self, pid, delay):
        """Kamerafoto nach Druckende (läuft im Hintergrund), wird am Verlaufseintrag gespeichert."""
        def run():
            time.sleep(max(0, delay))
            try:
                jpg = self.camera.snapshot()
                name = self.save_media("foto", jpg, "jpg")
                if name and self.history:
                    self.history.set_photo(pid, name)
                    log.info("[%s] Foto vom fertigen Druck gespeichert: %s", self.id, name)
            except Exception as e:
                log.warning("[%s] Foto nach Druckende fehlgeschlagen: %s", self.id, e)
        threading.Thread(target=run, daemon=True).start()

    def notify(self, event, title, message, tags="", priority="default", buttons=None, click=None):
        if self.notifier:
            photo = self.camera.snapshot if self.camera else None
            self.notifier.send(event, f"{self.name}: {title}", message, tags, priority, photo=photo,
                               buttons=buttons, click=click)

    def history_stages(self, job):
        """Vorbereitungsschritte für den Verlauf (nur Bambu kennt sie)."""
        return []

    def note_runout(self, slot_key):
        """Drucker meldet: Spule in diesem Slot ist leer gelaufen (wird am Druckende ausgewertet)."""
        job = self.store.get_job(self.id)
        if not job or not slot_key:
            return
        ro = job.setdefault("runout", [])
        if slot_key not in ro:
            ro.append(slot_key)
            self.store.set_job(self.id, job)
            log.info("[%s] Spule in %s leer gelaufen", self.id, self.slot_label(slot_key))

    def runout_check(self, job, booked):
        """Nach dem Buchen: Lager mit „wirklich leer“ vergleichen und Korrektur vorschlagen."""
        tol = float((self.settings or {}).get("runout_tol_g") or 15) if self.settings else 15.0
        base = ((self.settings or {}).get("click_url") or "").rstrip("/") if self.settings else ""
        for slot_key in job.get("runout") or []:
            label = self.slot_label(slot_key)
            e = next((x for x in booked if x.get("slot") == label and x.get("spool")), None)
            sid = (e or {}).get("spool")
            if not sid:
                continue
            rest = e.get("remaining")
            if rest is None:
                try:
                    rest = (self.spoolman.spool(sid) or {}).get("remaining")
                except Exception:
                    rest = None
            if rest is None:
                continue
            name = e.get("spool_name") or f"Spule #{sid}"
            link = f"{base}/lager?wiegen={sid}" if base else None
            knoepfe = [[("📦 Auf 0 & archivieren", f"ph:sp:zero:{sid}"), ("Lassen", "noop")]]
            if abs(rest) <= tol:
                self.notify("runout", "Spule leer 🧵", f"{name} (#{sid}) in {label} ist leer gelaufen – Lager passt "
                            f"({round(rest)} g Rest).\nIns Archiv verschieben?", "thread", buttons=knoepfe, click=link)
            elif rest > tol:
                self.notify("runout", "Spule leer – Lager stimmt nicht ⚖",
                            f"{name} (#{sid}) in {label} ist leer gelaufen, laut Lager waren aber noch {round(rest)} g drauf.\n"
                            "Häufigste Ursache: Leergewicht der Spule falsch oder weniger Filament drauf als angegeben.\n"
                            "👉 Leere Spule (mit Karton-Kern) wiegen und im Lager unter ⚖ eintragen – dann stimmt es bei der nächsten Rolle."
                            + (f"\n{link}" if link else ""), "thread", buttons=knoepfe, click=link)
            else:
                self.notify("runout", "Spule leer – mehr Filament als gedacht ⚖",
                            f"{name} (#{sid}) in {label} ist leer gelaufen – laut Lager schon {round(-rest)} g unter 0.\n"
                            "Es war mehr Filament drauf als eingetragen (oder das Leergewicht ist zu hoch). "
                            "Beim nächsten Wiegen das Leergewicht prüfen." + (f"\n{link}" if link else ""),
                            "thread", buttons=knoepfe, click=link)

    def track_layer(self, layer):
        """Erste Schicht fertig melden (einmal pro Druck)."""
        job = self.store.get_job(self.id)
        if not job or not layer or job.get("first_layer_sent"):
            return
        if int(layer) >= 2:
            job["first_layer_sent"] = True
            self.store.set_job(self.id, job)
            self.notify("first_layer", "Erste Schicht fertig ✅", f"{job.get('name', '')} – jetzt kurz die Haftung prüfen.", "eyes")

    def _energy_now(self):
        if self.shelly and self.shelly.online and self.shelly.total_wh is not None:
            return self.shelly.total_wh
        return None

    @property
    def price_kwh(self):
        return float(self.settings.get("price_kwh") or 0) if self.settings else 0.0

    @property
    def wear_per_h(self):
        return float(self.settings.get("wear_per_h") or 0) if self.settings else 0.0

    def spool_cost_per_g(self, spool_id):
        """€/g aus Spoolman (Preis der Spule bzw. des Filaments), sonst Ersatzpreis €/kg aus den Einstellungen."""
        sp = None
        try:
            sp = self.spoolman.spool(spool_id) if spool_id else None
        except Exception:
            pass
        if sp and sp.get("price") and sp.get("initial"):
            return float(sp["price"]) / float(sp["initial"])
        fb = float(self.settings.get("fallback_price_kg") or 0) if self.settings else 0.0
        return fb / 1000 if fb > 0 else None

    def energy_status(self):
        """Leistung jetzt, Energie/Kosten des laufenden Drucks und heute."""
        out = {"watts": None, "plug_on": None, "plug_online": False, "job_kwh": None, "job_energy_cost": None,
               "today_kwh": None, "today_cost": None, "filament_g": None, "filament_cost": None, "price_kwh": self.price_kwh,
               "wear_cost": None, "wear_per_h": self.wear_per_h}
        sh = self.shelly
        if sh:
            out.update(watts=sh.watts, plug_on=sh.on, plug_online=sh.online)
            t = sh.today_wh()
            if t is not None:
                out["today_kwh"] = round(t / 1000, 3)
                out["today_cost"] = round(t / 1000 * self.price_kwh, 2)
        job = self.store.get_job(self.id)
        if job:
            now = self._energy_now()
            if now is not None and job.get("energy_start") is not None:
                kwh = max(0.0, now - job["energy_start"]) / 1000
                out["job_kwh"] = round(kwh, 3)
                out["job_energy_cost"] = round(kwh * self.price_kwh, 2)
            try:
                usage = self.resolve_usage(job) if job.get("filaments") else []
            except Exception:
                usage = []
            if usage:
                grams = sum(g for _, g in usage)
                out["filament_g"] = round(grams, 1)
                cost, known = 0.0, False
                for key, g in usage:
                    sid = self.store.get_slot(self.id, key)
                    c = self.spool_cost_per_g(sid) if sid else None
                    if c is not None:
                        cost += c * g
                        known = True
                out["filament_cost"] = round(cost, 2) if known else None
            if self.wear_per_h > 0:
                out["wear_cost"] = round((time.time() - job.get("started", time.time())) / 3600 * self.wear_per_h, 2)
        return out

    # Muss jede Unterklasse liefern
    def slots(self):
        """Liste von dicts: key, label, material, color (Infos vom Drucker selbst)."""
        raise NotImplementedError

    def slot_label(self, key):
        for s in self.slots():
            if s["key"] == key:
                return s["label"]
        return key

    def location_name(self, key):
        return f"{self.name} – {self.slot_label(key)}"

    # --- Job-Verwaltung ------------------------------------------------------
    def job_started(self, name, extra=None):
        job = {"name": name, "started": int(time.time()), "filaments": None, "max_progress": 0.0,
               "energy_start": self._energy_now()}
        if extra:
            job.update(extra)
        self.store.set_job(self.id, job)
        log.info("[%s] Druck gestartet: %s", self.id, name)
        self.notify("start", "Druck gestartet", name, "printer")
        for cb in self.on_job_start:
            try:
                cb(self, job)
            except Exception as e:
                log.warning("[%s] Rückruf bei Druckstart fehlgeschlagen: %s", self.id, e)
        threading.Thread(target=self._load_filaments_safe, args=(job,), daemon=True).start()

    def _load_filaments_safe(self, job):
        try:
            usage = self.load_filament_usage(job)  # Rohdaten, siehe resolve_usage()
            current = self.store.get_job(self.id)
            if not current or current.get("started") != job.get("started"):
                return  # Druck ist inzwischen vorbei; job_ended lädt selbst nach
            current["filaments"] = usage
            if job.get("thumb"):
                current["thumb"] = job["thumb"]
            self.store.set_job(self.id, current)
            self.check_enough(current)
            log.info("[%s] Filamentbedarf laut Slicer: %s", self.id, usage)
        except Exception as e:
            log.warning("[%s] Filamentbedarf konnte nicht ermittelt werden: %s", self.id, e)
            self.last_error = f"Filamentbedarf nicht lesbar: {e}"

    def load_filament_usage(self, job):
        raise NotImplementedError

    # --- Reicht das Filament? / Ersatzspule ------------------------------------
    def _spool_of(self, key):
        sid = self.store.get_slot(self.id, key)
        if not sid:
            return None
        try:
            return self.spoolman.spool(sid)
        except Exception:
            return None

    def backup_slots(self, key):
        """Slots mit einer Spule derselben Filamentsorte (für automatisches Nachfüllen)."""
        sp = self._spool_of(key)
        if not sp or sp.get("filament_id") is None:
            return []
        out = []
        for s in self.slots():
            if s["key"] == key:
                continue
            o = self._spool_of(s["key"])
            if o and o.get("filament_id") == sp["filament_id"]:
                out.append((s["key"], o))
        return out

    def check_enough(self, job):
        st = self.settings
        if not st or not st.get("warn_short"):
            return
        try:
            usage = self.resolve_usage(job)
        except Exception:
            return
        reserve = float(st.get("short_reserve_g") or 0)
        notes, warn = [], []
        for key, grams in usage:
            sp = self._spool_of(key)
            if not sp or sp.get("remaining") is None:
                continue
            rest = float(sp["remaining"])
            if rest >= grams + reserve:
                continue
            backups = self.backup_slots(key)
            extra = sum(float(o.get("remaining") or 0) for _, o in backups)
            item = {"slot": self.slot_label(key), "spool": sp["id"], "need": round(grams), "rest": round(rest),
                    "backup": [self.slot_label(k) for k, _ in backups], "ok": rest + extra >= grams + reserve}
            notes.append(item)
            if item["ok"]:
                warn.append(f"{item['slot']}: Spule #{sp['id']} hat {round(rest)} g, Druck braucht ~{round(grams)} g – "
                            f"automatisches Nachfüllen aus {', '.join(item['backup'])} sollte einspringen.")
            else:
                warn.append(f"{item['slot']}: Spule #{sp['id']} hat nur {round(rest)} g, Druck braucht ~{round(grams)} g!")
        if notes:
            cur = self.store.get_job(self.id)
            if cur and cur.get("started") == job.get("started"):
                cur["short"] = notes
                self.store.set_job(self.id, cur)
            bad = any(not n["ok"] for n in notes)
            self.notify("short", "Filament reicht nicht ⚠" if bad else "Filament knapp – Ersatzspule übernimmt",
                        "\n".join(warn), "warning", "high" if bad else "default")

    def split_backup(self, job, usage, end=None):
        """Hat der Drucker mitten im Druck auf eine gleiche Ersatzspule gewechselt (automatisches Nachfüllen),
        wird der Verbrauch nach Fortschritt auf beide Spulen aufgeteilt."""
        segs = job.get("segments") or []
        if len(segs) < 2:
            return usage
        mapped = {k for k, _ in usage}
        if end is None:
            end = max(job.get("max_progress", 0) or 0, segs[-1][1]) or 1.0
        end = max(end, segs[-1][1])
        out = []
        for key, grams in usage:
            backups = {k for k, _ in self.backup_slots(key)} - mapped
            chain = [(t, p) for t, p in segs if t == key or t in backups]
            if not any(t != key for t, _ in chain):
                out.append((key, grams))
                continue
            shares = {}
            for i, (t, p0) in enumerate(chain):
                p1 = chain[i + 1][1] if i + 1 < len(chain) else end
                shares[t] = shares.get(t, 0) + max(0.0, p1 - p0)
            total = sum(shares.values()) or 1
            for t, v in shares.items():
                if v > 0:
                    out.append((t, grams * v / total))
            log.info("[%s] Spulenwechsel erkannt, Verbrauch aufgeteilt: %s", self.id,
                     {t: round(grams * v / total, 1) for t, v in shares.items()})
        return out

    def resolve_usage(self, job):
        """Macht aus job['filaments'] eine Liste [(slot_key, gramm)]. Standard: schon fertig."""
        return job.get("filaments") or []

    def update_progress(self, progress, remaining_s=None):
        job = self.store.get_job(self.id)
        if not job:
            return
        changed = False
        if progress is not None and progress > job.get("max_progress", 0):
            job["max_progress"] = progress
            changed = True
        # Geschätzte Gesamtdauer einmal früh im Druck merken (für „Zeitgenauigkeit“)
        if "estimate" not in job and remaining_s and progress is not None and 0.01 <= progress <= 0.5:
            job["estimate"] = int(time.time() - job.get("started", time.time()) + remaining_s)
            changed = True
        # Fortschritt melden (z. B. bei 25/50/75 %)
        if progress is not None and self.settings:
            try:
                steps = sorted({int(x) for x in str(self.settings.get("progress_steps") or "").replace(" ", "").split(",") if x})
            except ValueError:
                steps = []
            done = set(job.get("progress_sent", []))
            for st in steps:
                if 0 < st < 100 and progress * 100 >= st and st not in done:
                    done.add(st)
                    job["progress_sent"] = sorted(done)
                    changed = True
                    if progress * 100 < st + 10:   # nicht nachträglich alle auf einmal (z. B. nach Neustart)
                        h, m = int(remaining_s // 3600), int(remaining_s % 3600 // 60)
                        rem = (f" · noch {h} h {m:02d} min" if h else f" · noch {m} min") if remaining_s else ""
                        self.notify("progress", f"Druck bei {st} %", f"{job.get('name', '')}{rem}", "hourglass_flowing_sand", "low")
        if changed:
            self.store.set_job(self.id, job)

    def note_job_error(self, text):
        """Fehler während eines Drucks merken – landet als Grund im Verlauf."""
        job = self.store.get_job(self.id)
        if job and text and job.get("error_reason") != text:
            job["error_reason"] = text
            self.store.set_job(self.id, job)

    def job_ended(self, success: bool, progress=None):
        job = self.store.get_job(self.id)
        if not job:
            return
        self.store.set_job(self.id, None)
        self.cool_watch = time.time() if success else None
        if job.get("filaments") is None:
            # Falls das Laden noch lief oder scheiterte: letzter Versuch
            try:
                job["filaments"] = self.load_filament_usage(job)
            except Exception as e:
                log.warning("[%s] Kein Filamentbedarf verfügbar: %s", self.id, e)
                job["filaments"] = []
        factor = 1.0
        if not success:
            p = max(job.get("max_progress", 0), progress or 0)
            factor = max(0.0, min(1.0, p))
        booked = []
        low = []
        try:
            usage = self.resolve_usage(job)
            usage = self.split_backup(job, usage, 1.0 if success else factor)
        except Exception as e:
            log.warning("[%s] Zuordnung fehlgeschlagen: %s", self.id, e)
            usage = []
        for slot_key, grams in usage:
            g = grams * factor
            if g <= 0:
                continue
            spool_id = self.store.get_slot(self.id, slot_key)
            entry = {"slot": self.slot_label(slot_key), "grams": round(g, 1), "spool": spool_id}
            if spool_id:
                try:
                    sp = self.spoolman.spool(spool_id)
                except Exception:
                    sp = None
                if sp:
                    entry.update(spool_name=sp.get("name"), material=sp.get("material"), color=sp.get("color"))
            cpg = self.spool_cost_per_g(spool_id) if spool_id else None
            if cpg is not None:
                entry["cost"] = round(cpg * g, 2)
            if spool_id and self.track_usage:
                try:
                    res = self.spoolman.use(spool_id, g)
                    entry["booked"] = True
                    rest = (res or {}).get("remaining_weight")
                    if rest is not None:
                        entry["remaining"] = round(rest)
                        if self.notifier and rest < self.notifier.low_spool_g:
                            low.append(f"Spule #{spool_id} ({entry['slot']}): nur noch {max(0, round(rest))} g")
                except Exception as e:
                    entry["booked"] = False
                    entry["error"] = str(e)
            else:
                entry["booked"] = False
                entry["error"] = "Keine Spule zugewiesen" if not spool_id else "Buchung deaktiviert"
            booked.append(entry)
        duration = int(time.time()) - job.get("started", int(time.time()))
        now = self._energy_now()
        kwh = None
        if now is not None and job.get("energy_start") is not None:
            kwh = round(max(0.0, now - job["energy_start"]) / 1000, 3)
        energy_cost = round(kwh * self.price_kwh, 2) if kwh is not None else None
        fil_cost = round(sum(e.get("cost", 0) for e in booked), 2) if any("cost" in e for e in booked) else None
        wear_cost = round(duration / 3600 * self.wear_per_h, 2) if self.wear_per_h > 0 else None
        total = None
        if energy_cost is not None or fil_cost is not None or wear_cost is not None:
            total = round((energy_cost or 0) + (fil_cost or 0) + (wear_cost or 0), 2)
        if success:
            result, outcome, reason = "fertig", "ok", None
        elif job.get("error_reason"):
            result, outcome, reason = f"fehlgeschlagen bei {round(factor * 100)} %", "fail", job["error_reason"]
        else:
            result, outcome, reason = f"abgebrochen bei {round(factor * 100)} %", "cancel", "abgebrochen"
        entry_all = {
            "time": int(time.time()),
            "printer": self.name,
            "job": job.get("name", ""),
            "result": result,
            "usage": booked,
            "duration": duration,
            "kwh": kwh,
            "energy_cost": energy_cost,
            "filament_cost": fil_cost,
            "wear_cost": wear_cost,
            "total_cost": total,
            "thumb": job.get("thumb"),
            "outcome": outcome,
            "reason": reason,
            "estimate": job.get("estimate"),
            "source": "printhub",
            "stages": self.history_stages(job),
        }
        pid = None
        if self.history:
            pid = self.history.add(entry_all)
        else:
            self.store.add_history(entry_all)
        st = self.settings
        if pid and st and self.camera and st.get("photo_enabled") and \
                ("finish" if success else "fail") in (st.get("photo_events") or []):
            self.take_photo(pid, int(st.get("photo_delay") or 0))
        log.info("[%s] Druck beendet (%s), gebucht: %s", self.id, success, booked)
        # Benachrichtigung
        h, m = divmod(duration // 60, 60)
        parts = [job.get("name", ""), f"Dauer: {h} h {m:02d} min" if h else f"Dauer: {m} min"]
        grams = sum(e["grams"] for e in booked)
        if grams:
            parts.append(f"Filament: {_de(grams, 1)} g")
        if kwh is not None:
            parts.append(f"Strom: {_de(kwh)} kWh ({_de(energy_cost)} €)")
        if total is not None:
            parts.append(f"Kosten gesamt: {_de(total)} €")
        if success:
            self.notify("finish", "Druck fertig ✅", "\n".join(parts), "white_check_mark")
        else:
            self.notify("fail", f"Druck {result}", "\n".join(parts), "warning", "high")
        if low:
            ro_labels = {self.slot_label(k) for k in job.get("runout") or []}
            low = [x for x in low if not any(f"({l})" in x for l in ro_labels)]   # leer gelaufene meldet runout_check
            if low:
                self.notify("low_spool", "Spule fast leer", "\n".join(low), "warning")
        if job.get("runout") and self.track_usage:
            try:
                self.runout_check(job, booked)
            except Exception as e:
                log.warning("[%s] Leer-Prüfung fehlgeschlagen: %s", self.id, e)

    # --- Status für die Oberfläche ------------------------------------------
    def status(self):
        raise NotImplementedError

    def control(self, action):
        raise NotImplementedError
