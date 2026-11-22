"""Druckwächter für Klipper-Drucker (Snapmaker U1 & Co.) – läuft in PrintHub.

Gegenstück zum Bambuddy-Druckwächter (nachtruhe_engine.py), mit denselben Einstellungen (Druckfenster,
Plattenprüfung, Testmodus). Statt Bambuddys Warteschlange nimmt er die Warteschlange von Moonraker.

Ablauf:
  1. Startsperre (Makro printhub-startsperre.cfg) weist jeden Druckstart ab, der nicht freigegeben ist –
     vom Display, aus Orca, Fluidd oder der Snapmaker-App. PrintHub liest die Meldung aus der Konsole
     und legt die Datei in die Moonraker-Warteschlange.
  2. Die Warteschlange bleibt pausiert; der Wächter startet den ersten fälligen Auftrag (Druckfenster,
     geplante Zeit, „Jetzt starten“): Strom an, warten bis online, Licht an, Prüfposition anfahren,
     Platte prüfen, Startsperre für 60 s freigeben, Datei starten.
  3. Platte belegt / Kamera ohne Bild: Meldung mit Foto und Knöpfen [Platte frei – prüfen] [Trotzdem drucken]
     [Abbrechen]; ohne Antwort nach PLATTE_WARTE_MIN erneut prüfen; nach PLATTE_VERSUCHE Auftrag raus,
     Licht aus, Drucker aus (wenn der Wächter ihn eingeschaltet hat).
"""
import logging
import os
import re
import threading
import time
from datetime import datetime

from . import schedule as sched
from .queuecheck import color_dist, same_material

log = logging.getLogger("waechter.klipper")

BLOCK_RE = re.compile(r"Start gesperrt.*\((.*)\)\s*$")
FILE_RE = re.compile(r'FILENAME=(?:"([^"]+)"|\'([^\']+)\'|(\S+))', re.I)


def _short(fn):
    n = (fn or "").rsplit("/", 1)[-1]
    return re.sub(r"\.gcode$", "", n, flags=re.I)


class KlipperGuard:
    def __init__(self, printers, plate, power, notifier, store, nachtruhe, read_env, plans_get, plans_save, wcfg, qlog):
        self.printers, self.plate, self.power, self.notifier, self.store = printers, plate, power, notifier, store
        self.nachtruhe, self.read_env = nachtruhe, read_env
        self.plans_get, self.plans_save, self.wcfg, self.qlog = plans_get, plans_save, wcfg, qlog
        self.st = {}
        self.meta = {}           # Dateiname -> {"seconds", "grams", "tools": [...], "t"}
        self.lock = threading.Lock()

    # ---------- Hilfen ----------
    def _env(self):
        try:
            return self.read_env(self.nachtruhe.config)
        except Exception:
            return {}

    def _on(self):
        return bool(self.nachtruhe.enabled)

    def _dry(self):
        return self._env().get("DRY_RUN", "1") == "1"

    def _s(self, p):
        return self.st.setdefault(p.id, {"jobs": [], "qstate": None, "queue": None, "lock": None, "lock_t": 0,
                                         "seen_t": None, "busy": False, "active": None, "waiting": "",
                                         "force": set(), "answer": None, "told": set(), "blocked": "", "backoff": 0,
                                         "dry_done": set()})

    def _log(self, p, msg):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{p.name}] {msg}"
        log.info("[%s] %s", p.id, msg)
        try:
            with open(self.nachtruhe.logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _klipper(self):
        return [p for p in self.printers.values() if p.type == "moonraker"]

    def file_meta(self, p, fn):
        m = self.meta.get(fn)
        if m and time.time() - m["t"] < 3600:
            return m
        md = p.file_meta(fn) if p.online else {}
        if not md and m:
            return m
        w = md.get("filament_weights") or []
        types = [t.strip() for t in str(md.get("filament_type") or "").split(";")]
        cols = md.get("filament_colors") or []
        tools = []
        for i, g in enumerate(w if isinstance(w, list) else []):
            try:
                g = float(g)
            except (TypeError, ValueError):
                continue
            if g > 0:
                tools.append({"i": i, "grams": round(g, 1), "type": types[i] if i < len(types) else "",
                              "color": cols[i] if i < len(cols) else ""})
        m = {"seconds": int(md.get("estimated_time") or 0) or None,
             "grams": round(float(md.get("filament_weight_total") or 0) or sum(t["grams"] for t in tools)) or None,
             "tools": tools, "t": time.time()}
        self.meta[fn] = m
        return m

    def _check(self, p, m):
        """Filament-Check je Kopf wie bei Bambuddy-Aufträgen (Material, Farbe, reicht die Spule?)."""
        if not m["tools"]:
            return None
        slots = {s["key"]: s for s in p.slots()}
        lines, worst = [], "ok"
        rank = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}
        for t in m["tools"]:
            key = f"T{t['i']}"
            s = slots.get(key)
            sid = self.store.get_slot(p.id, key)
            sp = None
            if sid:
                try:
                    sp = next((x for x in p.spoolman.spools() if x["id"] == sid), None)
                except Exception:
                    sp = None
            mat = (sp or {}).get("material") or (s or {}).get("material")
            col = (sp or {}).get("color") or (s or {}).get("color")
            have = (sp or {}).get("remaining")
            state, msg = "ok", ""
            if not s or (not mat and not col):
                state, msg = "warn", f"Kopf {t['i'] + 1}: kein Filament erkannt"
            elif same_material(t["type"], mat) is False:
                state, msg = "bad", f"Kopf {t['i'] + 1}: {mat} statt {t['type']}"
            elif t["color"] and col and (color_dist(t["color"], col) or 0) > 90:
                state, msg = "warn", f"Farbe in Kopf {t['i'] + 1} weicht ab"
            if have is not None and have < t["grams"] + 5 and state != "bad":
                state, msg = "bad", f"Kopf {t['i'] + 1}: Spule reicht nicht ({round(have)} g da)"
            if rank[state] > rank[worst]:
                worst = state
            lines.append({"type": t["type"], "color": t["color"], "grams": t["grams"], "slot": (s or {}).get("label") or key,
                          "have": have, "state": state, "msg": msg})
        bad = [l["msg"] for l in lines if l["msg"]]
        text = bad[0] if bad else f"Filament passt ({len(lines)} Köpfe)" if len(lines) > 1 else "Filament passt"
        return {"state": worst, "text": text, "lines": lines}

    # ---------- für Oberfläche / Status ----------
    def info(self, p):
        s = self._s(p)
        if s["queue"] is None and s["lock"] is None:
            return None
        return {"src": "moonraker", "state": s["qstate"], "pending": len(s["jobs"]), "lock": bool((s["lock"] or {}).get("lock")),
                "waiting": s["waiting"]}

    def klipper_list(self):
        out = []
        for p in self._klipper():
            s = self._s(p)
            lk = s["lock"] or {}
            out.append({"id": p.id, "name": p.name, "online": p.online, "queue": bool(s["queue"]), "queue_state": s["qstate"],
                        "lock": bool(lk.get("lock")), "lock_version": lk.get("version"), "lock_off": lk.get("off"),
                        "blocked": s["blocked"], "pending": len(s["jobs"]), "waiting": s["waiting"]})
        return out

    def items(self):
        out = []
        for p in self._klipper():
            s = self._s(p)
            for i, j in enumerate(s["jobs"]):
                m = self.file_meta(p, j["filename"])
                qid = f"{p.id}:{j['job_id']}"
                it = {"id": qid, "src": "moonraker", "name": _short(j["filename"]), "filename": j["filename"],
                      "status": "pending", "printer": p.name, "pid": p.id, "seconds": m["seconds"], "grams": m["grams"],
                      "position": i + 1, "manual": True,
                      "thumb_url": f"/api/printers/{p.id}/gcode-thumb?fn={j['filename']}",
                      "waiting": s["waiting"] if s["active"] == j["job_id"] else ""}
                try:
                    it["check"] = self._check(p, m)
                except Exception as e:
                    it["check"] = {"state": "unknown", "lines": [], "text": f"Prüfung nicht möglich: {e}"}
                pl = self.plans_get().get(qid) or {}
                if pl.get("when"):
                    it["scheduled"] = pl["when"]
                out.append(it)
        return out

    # ---------- Aktionen ----------
    def _find(self, pid, jid):
        p = self.printers.get(pid)
        if not p:
            raise LookupError("Drucker unbekannt")
        s = self._s(p)
        j = next((x for x in s["jobs"] if x["job_id"] == jid), None)
        if not j:
            raise LookupError("Auftrag nicht (mehr) in der Warteschlange")
        return p, s, j

    def action(self, qid, action, by="PrintHub"):
        pid, jid = qid.split(":", 1)
        p, s, j = self._find(pid, jid)
        name = _short(j["filename"])
        if action == "jetzt":
            if self._on():
                s["force"].add(jid)
                s["backoff"] = 0
                self.qlog(qid, name, f"„Jetzt starten“ ({by}) – Druckwächter startet mit Plattenprüfung")
                self._log(p, f"„Jetzt starten“ für {name} ({by})")
                return "Druckwächter startet den Auftrag (Einschalten, Licht, Plattenprüfung, Freigabe) …"
            if not p.online:
                raise RuntimeError(f"{p.name} ist nicht erreichbar")
            self._start_file(p, j)
            self.qlog(qid, name, f"direkt gestartet ({by}, Druckwächter aus)")
            return "Gestartet ✓"
        if action == "cancel":
            p.queue_remove([jid])
            s["jobs"] = [x for x in s["jobs"] if x["job_id"] != jid]
            s["force"].discard(jid)
            self.qlog(qid, name, f"aus der Warteschlange genommen ({by}) – Datei bleibt auf dem Drucker")
            self._log(p, f"{name} aus der Warteschlange genommen ({by})")
            return "Entfernt ✓"
        if action == "top":
            self.reorder(pid, [jid] + [x["job_id"] for x in s["jobs"] if x["job_id"] != jid])
            self.qlog(qid, name, f"nach vorne geholt ({by})")
            return "Nach vorne geholt ✓"
        raise ValueError("Unbekannte Aktion")

    def reorder(self, pid, jids):
        """Moonraker kann nicht umsortieren → Aufträge in neuer Reihenfolge neu anlegen (Pläne wandern mit)."""
        p = self.printers.get(pid)
        s = self._s(p)
        if s["busy"]:
            raise RuntimeError("Der Druckwächter startet gerade einen Auftrag – gleich nochmal versuchen")
        by_id = {j["job_id"]: j for j in s["jobs"]}
        order = [by_id[j] for j in jids if j in by_id] + [j for j in s["jobs"] if j["job_id"] not in jids]
        if [j["job_id"] for j in order] == [j["job_id"] for j in s["jobs"]]:
            return
        pl = self.plans_get()
        moved = {j["filename"]: pl.pop(f"{pid}:{j['job_id']}", None) for j in order}
        forced = {j["filename"] for j in order if j["job_id"] in s["force"]}
        p.queue_remove([j["job_id"] for j in order])
        p.queue_add([j["filename"] for j in order])
        self._refresh(p, s)
        for j in s["jobs"]:
            if moved.get(j["filename"]):
                pl[f"{pid}:{j['job_id']}"] = moved[j["filename"]]
            if j["filename"] in forced:
                s["force"].add(j["job_id"])
        self.plans_save(pl)

    def button(self, pid, act, jid):
        """Telegram-Knopf: ph:kw:<pid>:<act>:<job_id>."""
        p = self.printers.get(pid)
        if not p:
            return "Drucker unbekannt"
        s = self._s(p)
        if act == "now":
            try:
                return self.action(f"{pid}:{jid}", "jetzt", "Telegram")
            except Exception as e:
                return str(e)
        if act == "del" and s["active"] != jid:
            try:
                return self.action(f"{pid}:{jid}", "cancel", "Telegram")
            except Exception as e:
                return str(e)
        if s["active"] != jid:
            return "Dieser Auftrag wartet gerade nicht auf eine Antwort"
        s["answer"] = act
        if act == "chk" and s.get("asking") == "plate":
            self.clear_height(p)      # „Platte frei“ von dir bestätigt → Bett darf wieder auf Prüfhöhe
        return {"chk": "Prüfe nochmal …", "force": "Starte trotz Platte …", "del": "Auftrag wird abgebrochen …"}.get(act, "?")

    # ---------- Hauptschleife ----------
    def start(self):
        for p in self._klipper():
            p.start_gate = self._gate
            p.on_gate_block = lambda pr, fn: self._enqueue(pr, self._s(pr), fn.lstrip("/"))
        threading.Thread(target=self._loop, daemon=True, name="klipper-waechter").start()

    def _loop(self):
        while True:
            for p in self._klipper():
                try:
                    self._tick(p)
                except Exception as e:
                    log.debug("[%s] Wächter-Takt: %s", p.id, e)
            time.sleep(5)

    def _refresh(self, p, s):
        q = p.queue_status()
        s["queue"] = q is not None
        if q is None:
            s["jobs"], s["qstate"] = [], None
            return
        s["jobs"], s["qstate"] = q["jobs"], q["state"]
        ids = {j["job_id"] for j in s["jobs"]}
        s["force"] &= ids

    def _tick(self, p):
        s = self._s(p)
        if p.online:
            self._refresh(p, s)
            if time.time() - s["lock_t"] > 60 or s["lock"] is None:
                s["lock_t"] = time.time()
                try:
                    s["lock"] = p.lock_info()
                except Exception:
                    s["lock"] = s["lock"] or {"lock": False}
            locked = bool((s["lock"] or {}).get("lock"))
            if s["queue"] and locked and s["qstate"] not in ("paused", None):
                p.queue_pause()            # Moonraker soll nichts selbst starten – das macht der Wächter
                self._log(p, "Moonraker-Warteschlange pausiert (startet nur noch über den Druckwächter)")
            if locked and s["queue"]:
                self._intercept(p, s)
        if s["busy"] or not self._on() or not s["queue"]:
            return
        st = p.status() if p.online else {}
        if st.get("printing") or st.get("paused"):
            s["waiting"] = ""
            return
        if time.time() < s["backoff"]:
            return
        job, why = self._due(p, s)
        if not job:
            if why is not None:
                s["waiting"] = why
            return
        s["busy"], s["active"], s["answer"] = True, job["job_id"], None
        threading.Thread(target=self._run, args=(p, s, job), daemon=True).start()

    def _intercept(self, p, s):
        """Abgewiesene Druckstarts (Startsperre) aus der Konsole lesen und in die Warteschlange legen."""
        msgs = p.gcode_messages(60)
        if s["seen_t"] is None:          # beim ersten Mal nur merken (Uhr des Druckers kann abweichen)
            s["seen_t"] = max([m.get("time") or 0 for m in msgs] or [0])
            return
        newest = s["seen_t"]
        for m in msgs:
            t = m.get("time") or 0
            if t <= s["seen_t"]:
                continue
            newest = max(newest, t)
            mm = BLOCK_RE.search(m.get("message") or "")
            if not mm:
                continue
            fm = FILE_RE.search(mm.group(1))
            fn = next((g for g in fm.groups() if g), None) if fm else None
            if not fn:
                continue
            self._enqueue(p, s, fn.lstrip("/"))
        s["seen_t"] = newest

    def _enqueue(self, p, s, fn):
        """Abgewiesenen Start in die Warteschlange legen und melden (einmal pro Datei)."""
        with self.lock:
            s["blocked"] = f"{_short(fn)} ({datetime.now():%d.%m. %H:%M})"
            self._refresh(p, s)
            if any(j["filename"] == fn for j in s["jobs"]):
                self._log(p, f"Start von {_short(fn)} abgewiesen – steht schon in der Warteschlange")
                return
            p.queue_add([fn])
            self._refresh(p, s)
            self._log(p, f"Start von {_short(fn)} abgewiesen → in die Warteschlange gelegt")
            jid = next((j["job_id"] for j in s["jobs"] if j["filename"] == fn), "")
            self.qlog(f"{p.id}:{jid}", _short(fn), "Druckstart abgefangen → Warteschlange")
            m2 = self.file_meta(p, fn)
            dur = f" · ⏱ {m2['seconds'] // 3600} h {m2['seconds'] % 3600 // 60:02d} min" if m2.get("seconds") else ""
            self.notifier.send("queue_add", f"{p.name}: Auftrag angenommen 📥", f"{_short(fn)}{dur}\nDer Druckwächter startet ihn "
                               f"(Druckfenster, Licht, Plattenprüfung).", "inbox_tray",
                               buttons=[[("▶ Jetzt starten", f"ph:kw:{p.id}:now:{jid}"), ("🗑 Entfernen", f"ph:kw:{p.id}:del:{jid}")]])

    def _gate(self, p, fn):
        """Darf ein gerade beginnender Druck laufen? Nur mit Freigabe vom Wächter (oder Sperre aus/nicht installiert)."""
        s = self._s(p)
        lk = s["lock"] or {}
        if not lk.get("lock") or lk.get("off"):
            return True
        return time.time() < s.get("released_until", 0)

    def _due(self, p, s):
        """Ersten fälligen Auftrag finden – mit denselben Regeln wie die Vorschau (schedule.plan)."""
        if not s["jobs"]:
            return None, ""
        for j in s["jobs"]:
            if j["job_id"] in s["force"]:
                return j, None
        win, tz = self.wcfg()
        now = datetime.now(tz).replace(tzinfo=None) if tz else datetime.now()
        pl = self.plans_get()
        items, forced = [], set()
        for i, j in enumerate(s["jobs"]):
            qid = f"{p.id}:{j['job_id']}"
            e = pl.get(qid) or {}
            sch = None
            if e.get("when"):
                try:
                    d = datetime.fromisoformat(e["when"].replace("Z", "+00:00"))
                    sch = d.astimezone(tz).replace(tzinfo=None) if tz else d.astimezone().replace(tzinfo=None)
                except ValueError:
                    sch = None
            if e.get("force"):
                forced.add(qid)
            items.append({"id": qid, "seconds": self.file_meta(p, j["filename"])["seconds"], "position": i, "manual": True,
                          "scheduled": sch, "job": j})
        out, _ = sched.plan(items, win, now, None, forced)
        for it in items:
            o = out.get(it["id"]) or {}
            if o.get("start") and o["start"] <= now.timestamp() + 30:
                return it["job"], None
        first = out.get(items[0]["id"]) or {}
        txt = first.get("text") or ""
        if first.get("start"):
            txt = f"wartet aufs Druckfenster – Start {txt.split(' → ')[0]}"
        jid = items[0]["job"]["job_id"]
        if jid not in s["told"] and first.get("note") in ("", "zu_lang", "unbekannt", None):
            s["told"].add(jid)
            self.notifier.send("waiting", f"{p.name}: Auftrag wartet ⏳", f"{_short(items[0]['job']['filename'])}\n{txt}", "hourglass",
                               buttons=[[("▶ Jetzt starten", f"ph:kw:{p.id}:now:{jid}"), ("🗑 Entfernen", f"ph:kw:{p.id}:del:{jid}")]])
        return None, txt

    # ---------- Startablauf ----------
    def _run(self, p, s, job):
        name, jid = _short(job["filename"]), job["job_id"]
        try:
            self._start_sequence(p, s, job, name, jid)
        except Exception as e:
            self._log(p, f"⚠ Start von {name} fehlgeschlagen: {e}")
            self.notifier.send("error", f"{p.name}: Start fehlgeschlagen ⚠", f"{name}\n{e}", "warning", "high")
            s["backoff"] = time.time() + 600
        finally:
            s["busy"], s["active"], s["answer"] = False, None, None
            s["force"].discard(jid)

    def _start_sequence(self, p, s, job, name, jid):
        env = self._env()
        dry = self._dry()
        self._log(p, f"Starte {name}" + (" (Testmodus – es wird nichts geschaltet oder gestartet)" if dry else ""))
        if dry:
            if jid in s["dry_done"]:
                return
            s["dry_done"].add(jid)
            s["backoff"] = time.time() + 600
            self._log(p, f"Testmodus: würde jetzt einschalten, Licht an, Platte prüfen, Startsperre freigeben und {name} starten")
            return
        switched = False
        # 1. Strom
        if not p.online:
            if not self.power.available(p):
                s["waiting"] = "Drucker ist aus – bitte einschalten (keine Steckdose eingetragen)"
                self._log(p, s["waiting"])
                s["backoff"] = time.time() + 300
                return
            s["waiting"] = "schaltet den Drucker ein …"
            self.power.switch(p, True, "Druckwächter")
            switched = True
            t0 = time.time()
            while not p.online and time.time() - t0 < 300:
                time.sleep(5)
            if not p.online:
                raise RuntimeError("Drucker nach 5 min immer noch nicht erreichbar")
            time.sleep(15)              # Klipper fertig starten lassen
            self._log(p, "eingeschaltet und erreichbar")
        # 2. Licht
        if getattr(p, "light_pin", None):
            try:
                p.set_light(True)
                time.sleep(3)
            except Exception as e:
                self._log(p, f"Licht: {e}")
        # 2b. Filament je Kopf (Material, Farbe, reicht die Spule?) – bei ⛔ nachfragen
        if not self._filament_ok(p, s, job, name, jid, switched):
            return
        # 3. Platte
        if env.get("PLATTE_PRUEFEN", "1") == "1" and p.camera:
            tries = int(float(env.get("PLATTE_VERSUCHE") or 3))
            wait_min = float(env.get("PLATTE_WARTE_MIN") or 5)
            n = 0
            while True:
                s["waiting"] = "prüft die Platte …"
                res, jpg, msg = self.check_plate(p)
                if res is None:
                    self._log(p, f"Plattenprüfung übersprungen: {msg}")
                    break
                if res is True:
                    self.clear_height(p)
                    self._log(p, f"Platte leer ✓ ({msg})")
                    break
                n += 1
                belegt = res is False
                was = "Platte belegt" if belegt else "Platte nicht prüfbar" if jpg else "Kamera ohne Bild"
                self._log(p, f"{was} ({msg}) – Rückfrage {n}/{tries}")
                if n > tries:
                    self._abort(p, s, jid, name, switched, f"nach {tries} Rückfragen ohne freie Platte")
                    return
                s["waiting"] = f"wartet: {was}?"
                s["answer"], s["asking"] = None, "plate"
                self.notifier.send("plate", f"{p.name}: {was}? 🟥",
                                   f"{name}\n{msg}\nOhne Antwort prüfe ich in {wait_min:g} min nochmal ({n}/{tries}).",
                                   "warning", "high", force=False, photo=(lambda b=jpg: b) if jpg else None,
                                   buttons=[[("✅ Platte frei – prüfen", f"ph:kw:{p.id}:chk:{jid}"),
                                             ("▶ Trotzdem drucken", f"ph:kw:{p.id}:force:{jid}")],
                                            [("🗑 Abbrechen", f"ph:kw:{p.id}:del:{jid}")]])
                t0 = time.time()
                while s["answer"] is None and time.time() - t0 < wait_min * 60:
                    time.sleep(2)
                a = s["answer"]
                if a == "force":
                    self._log(p, "„Trotzdem drucken“ – starte ohne freie Platte")
                    break
                if a == "del":
                    self._abort(p, s, jid, name, switched, "von Hand abgebrochen")
                    return
                if a == "chk":
                    n = max(0, n - 1)          # ausdrücklich nachprüfen zählt nicht als Versuch
        # 4. Freigabe + Start
        s["waiting"] = "startet …"
        self._start_file(p, job)
        t0 = time.time()
        while time.time() - t0 < 90:
            time.sleep(3)
            if p.status().get("printing"):
                break
        else:
            raise RuntimeError("Drucker hat nach der Freigabe nicht angefangen zu drucken")
        try:
            p.queue_remove([jid])
        except Exception as e:
            self._log(p, f"Auftrag nicht aus der Warteschlange entfernt: {e}")
        pl = self.plans_get()
        if pl.pop(f"{p.id}:{jid}", None) is not None:
            self.plans_save(pl)
        s["waiting"] = ""
        self.qlog(f"{p.id}:{jid}", name, "vom Druckwächter gestartet")
        self._log(p, f"{name} gestartet ✓")

    def _filament_ok(self, p, s, job, name, jid, switched):
        """⛔ (falsches Material, Spule reicht nicht) → Rückfrage; ⚠ (Farbe weicht ab, nichts erkannt) → nur Log.
        Annahme: Filament n im Slicer = Kopf n (Standard bei Snapmaker Orca)."""
        env = self._env()
        tries = int(float(env.get("PLATTE_VERSUCHE") or 3))
        wait_min = float(env.get("PLATTE_WARTE_MIN") or 5)
        n = 0
        while True:
            self.meta.pop(job["filename"], None)          # frisch lesen
            try:
                chk = self._check(p, self.file_meta(p, job["filename"]))
            except Exception as e:
                self._log(p, f"Filament-Check nicht möglich: {e}")
                return True
            if not chk or chk["state"] in ("ok", "unknown"):
                if chk:
                    self._log(p, f"Filament ✓ ({chk['text']})")
                return True
            if chk["state"] == "warn":
                self._log(p, f"Filament ⚠ {chk['text']} – starte trotzdem")
                return True
            n += 1
            msgs = "\n".join("• " + l["msg"] for l in chk["lines"] if l["msg"])
            self._log(p, f"Filament ⛔ {chk['text']} – Rückfrage {n}/{tries}")
            if n > tries:
                self._abort(p, s, jid, name, switched, "Filament passt nicht")
                return False
            s["waiting"], s["answer"], s["asking"] = "wartet: Filament prüfen", None, "filament"
            self.notifier.send("plate", f"{p.name}: Filament passt nicht ⛔", f"{name}\n{msgs}\nSpule tauschen und „Nochmal prüfen“ – "
                               f"ohne Antwort prüfe ich in {wait_min:g} min nochmal ({n}/{tries}).", "warning", "high",
                               buttons=[[("🔄 Nochmal prüfen", f"ph:kw:{p.id}:chk:{jid}"), ("▶ Trotzdem drucken", f"ph:kw:{p.id}:force:{jid}")],
                                        [("🗑 Abbrechen", f"ph:kw:{p.id}:del:{jid}")]])
            t0 = time.time()
            while s["answer"] is None and time.time() - t0 < wait_min * 60:
                time.sleep(2)
            a, s["answer"] = s["answer"], None
            if a == "force":
                self._log(p, "„Trotzdem drucken“ trotz Filament-Warnung")
                return True
            if a == "del":
                self._abort(p, s, jid, name, switched, "von Hand abgebrochen")
                return False
            if a == "chk":
                n = max(0, n - 1)

    def _start_file(self, p, job):
        self._s(p)["released_until"] = time.time() + 180     # dieser Start ist erlaubt (Prüfung beim Druckbeginn)
        lk = (self._s(p)["lock"] or {})
        if lk.get("lock"):
            p.release_start(job["filename"])
        else:
            p._api("POST", "/printer/print/start", params={"filename": job["filename"]}, timeout=15)

    def _abort(self, p, s, jid, name, switched, why):
        self._log(p, f"{name}: Start abgebrochen – {why}")
        try:
            p.queue_remove([jid])
        except Exception:
            pass
        if getattr(p, "light_pin", None):
            try:
                p.set_light(False)
            except Exception:
                pass
        if switched:
            try:
                self.power.switch(p, False, "Druckwächter (Start abgebrochen)", force=False)
            except Exception as e:
                self._log(p, f"Ausschalten: {e}")
        self.qlog(f"{p.id}:{jid}", name, f"Start abgebrochen – {why}")
        self.notifier.send("plate", f"{p.name}: Auftrag abgebrochen", f"{name}\n{why}. Die Datei bleibt auf dem Drucker.", "x")

    # ---------- Plattenprüfung ----------
    def clear_height(self, p):
        """Platte ist nachweislich leer → Sicherheitsabstand (letzte Druckhöhe) aufheben."""
        mz = self.store.data.setdefault("max_z", {})
        if mz.get(p.id):
            mz[p.id] = 0
            try:
                with self.store.lock:
                    self.store._save()
            except Exception:
                pass

    def pose(self, p):
        """Prüfposition anfahren (wenn eingestellt). Rückgabe: (Text, abweichend, Referenz-Höhe).
        Sicherheitsabstand: das Bett kommt dem Kopf nie näher als letzte Druckhöhe + 20 mm. Reicht die eingestellte
        Höhe dafür nicht, fährt es auf die nächsthöhere Stufe, für die es ein Referenzbild gibt (Beschriftung „80mm“).
        abweichend = keine passende Referenz für die angefahrene Höhe → nachfragen statt vergleichen."""
        cfg = getattr(p, "plate_pose_cfg", None) or {}
        if not cfg.get("on") or not p.online:
            return "", False, None
        st = p.status()
        if st.get("printing") or st.get("paused"):      # niemals während eines Drucks bewegen
            return "", False, None
        last = (self.store.data.get("max_z") or {}).get(p.id) or 0
        safe = last + 20 if last else 0
        want = max(5.0, float(cfg.get("z", 30) or 30))
        try:
            heights = self.plate.ref_heights(p.id) if self.plate else []
        except Exception:
            heights = []
        target = want
        if want < safe:
            up = [h for h in heights if h >= safe]
            target = float(up[0]) if up else safe
        zz = p.plate_pose(cfg.get("x", 135), cfg.get("y", 10), target, safe)
        time.sleep(2)
        if zz is None:
            return f"Kopf X{cfg.get('x')} Y{cfg.get('y')}, Z nicht referenziert – Bett bleibt stehen", True, None
        h = int(round(zz)) if heights and abs(zz - round(zz)) < 0.05 and int(round(zz)) in heights else None
        txt = f"Kopf X{cfg.get('x')} Y{cfg.get('y')}, Bett auf {zz:g} mm"
        if zz > want + 0.05:
            txt += f" statt {want:g} mm (Sicherheitsabstand: letzter Druck {last:g} mm hoch + 20 mm)"
        abw = zz > want + 0.05 and h is None
        txt += f" – Vergleich mit Referenzbild {h} mm" if h is not None else (" – kein Referenzbild für diese Höhe" if abw else "")
        return txt, abw, h

    def check_plate(self, p):
        """(True leer / False belegt / 'fehler' / None nicht kalibriert, JPEG, Text)."""
        jpg = None
        abw, href = False, None
        try:
            self._log(p, "fahre in die Prüfposition (ggf. erst Home All) …")
            pos, abw, href = self.pose(p)
            if pos:
                self._log(p, f"Prüfposition: {pos}")
        except Exception as e:
            abw = True          # Position unbekannt → Bild nicht mit den Referenzen vergleichbar
            self._log(p, f"Prüfposition nicht angefahren: {e}")
        for versuch in range(3):
            try:
                jpg = p.camera.snapshot()
                break
            except Exception as e:
                if versuch == 2:
                    return "fehler", None, f"Kamera: {e}"
                time.sleep(5)
        if abw:   # Bild aus anderer Höhe → mit den Referenzbildern nicht vergleichbar: nachfragen statt raten
            return "fehler", jpg, "Bett konnte nicht auf eine Höhe mit Referenzbild (Sicherheitsabstand zum letzten Druck) – bitte Foto ansehen"
        r = self.plate.check(p.id, jpg, height=href)
        if r.get("needs_calibration"):
            return None, jpg, r.get("message") or "nicht kalibriert"
        if r.get("is_empty") is None:
            return "fehler", jpg, r.get("message") or "Vergleich fehlgeschlagen"
        if not r["is_empty"]:
            time.sleep(8)               # zweiter Blick gegen Belichtungs-Fehlalarme
            try:
                jpg2 = p.camera.snapshot()
                r2 = self.plate.check(p.id, jpg2, height=href)
                if r2.get("is_empty") is not None:
                    r, jpg = r2, jpg2
            except Exception:
                pass
        return bool(r["is_empty"]), jpg, r.get("message") or ""
