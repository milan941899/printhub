"""Strom-Verwaltung: Drucker-Steckdose automatisch und von Hand schalten.

Entscheidung in PrintHub (kennt Warteschlange, Druckwächter, Temperaturen), geschaltet wird über Bambuddys
Steckdose – so bleibt Bambuddy im Bild (markiert den Drucker als offline). Ist Bambuddy nicht erreichbar,
schaltet PrintHub die Shelly direkt.

Ausschalten nur, wenn ALLES passt:
  - kein Druck läuft / pausiert,
  - Düse unter der Grenze (Standard 45 °C),
  - Wartezeit vorbei: nach Druckende X min, sonst (von Hand an, nichts passiert) Y min Leerlauf,
  - kein Auftrag steht zum Start an (Bambuddy-Warteschlange / Druckwächter will gleich starten),
  - der Druckwächter ist nicht gerade beim Starten (Einschalten, Plattenprüfung, Rückfrage).
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("strom")

KEEP_GRUND = {
    "druck": "Druck läuft", "heiss": "Düse noch heiß", "auftrag": "nächster Auftrag startet gleich",
    "waechter": "Druckwächter startet gerade einen Auftrag", "frisch": "gerade erst eingeschaltet",
    "doppelt": "Steckdose gehört auch zu einem anderen Drucker",
    "fremdlast": "Drucker offline, aber an der Steckdose läuft etwas",
    "update": "Update läuft", "offline": "Drucker antwortet nicht (fährt hoch oder Update?)", "startet": "Klipper startet gerade",
}


class Power:
    def __init__(self, settings, printers, bambuddy, notifier, wstate_path):
        self.settings, self.printers, self.bb, self.notifier = settings, printers, bambuddy, notifier
        self.wstate_path = wstate_path
        self.st = {}            # pro Drucker: Zustand für Anzeige und Zeitmessung
        self.lock = threading.Lock()

    # --- Zuordnung zur Bambuddy-Steckdose ------------------------------------
    def _plug(self, p, s):
        if s.get("plug_id") and time.time() - s.get("plug_t", 0) < 900:
            return s["plug_id"]
        s["plug_t"] = time.time()
        s["plug_id"] = None
        if not self.bb.enabled:
            return None
        try:
            ws = self._wstate()
            bb_pid = None
            serial = getattr(p, "serial", None)
            for bp in self.bb.printers():
                if (serial and bp.get("serial_number") == serial) or bp.get("name") == p.name:
                    bb_pid = bp["id"]
                    break
            if bb_pid is None and ws.get("printer_id") and len(self.printers) == 1:
                bb_pid = ws["printer_id"]
            if bb_pid is not None:
                plug = self.bb.plug_for_printer(bb_pid)
                if plug:
                    s.update(plug_id=plug["id"], plug_name=plug.get("name"), bb_auto_off=plug.get("auto_off"),
                             bb_auto_on=plug.get("auto_on"), bb_printer=bb_pid,
                             plug_ip=str(plug.get("ip_address") or plug.get("ip") or plug.get("host") or "").strip())
        except Exception as e:
            s["plug_err"] = str(e)
        return s.get("plug_id")

    pre_off = None      # wird in main gesetzt: Bett vor dem Abschalten in Fotohöhe parken (Klipper)

    def _shared(self, p, s):
        """Die Bambuddy-Steckdose dieses Druckers ist die Shelly eines anderen Druckers (z. B. umgesteckt)."""
        ip = s.get("plug_ip")
        if not ip:
            return False
        for o in self.printers.values():
            if o is not p and o.shelly and str(getattr(o.shelly, "host", "")).split(":")[0] == ip.split(":")[0]:
                return True
        return False

    def _wstate(self):
        """Zustand des Druckwächters. „Beschäftigt“ gilt bis zu 3 h (er wartet evtl. lange auf deine Antwort),
        alles andere nur, wenn frisch (≤ 3 min)."""
        try:
            with open(self.wstate_path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return {}
        age = time.time() - d.get("time", 0)
        if d.get("beschaeftigt") and age < 3 * 3600:
            return d
        return d if age < 180 else {}

    # --- Steckdose lesen / schalten -------------------------------------------
    def plug_on(self, p):
        """True/False/None (unbekannt). Shelly direkt bevorzugt (schnell), sonst Bambuddy."""
        s = self.st.setdefault(p.id, {})
        if p.shelly and p.shelly.online and p.shelly.on is not None:
            return bool(p.shelly.on)
        pid = self._plug(p, s)
        if pid:
            try:
                d = self.bb.plug_status(pid)
                s["watts"] = ((d.get("energy") or {}).get("power"))
                return {"ON": True, "OFF": False}.get(str(d.get("state") or "").upper())
            except Exception:
                return None
        return None

    def available(self, p):
        s = self.st.setdefault(p.id, {})
        return bool(p.shelly) or bool(self._plug(p, s))

    def switch(self, p, on, reason="von Hand", force=False):
        """Schalten mit Schutz: Aus nur, wenn kein Druck läuft und die Düse kühl ist (außer force)."""
        s = self.st.setdefault(p.id, {})
        if not on and not force:
            ok, why = self.safe_off(p)
            if not ok:
                raise ValueError(f"Ausschalten gesperrt: {why}")
        pid = self._plug(p, s)
        err = None
        down = ""
        steps, t0 = [], time.time()

        def step(txt):
            w = self._watts_now(p)
            steps.append([round(time.time(), 1), round(time.time() - t0), txt, w])
            log.info("[%s] Ausschalten +%ds: %s%s", p.id, time.time() - t0, txt, f" · {w} W" if w is not None else "")
        # Klipper-Drucker (U1): kein Herunterfahren – der U1 ist fürs harte Abschalten gebaut (System schreibgeschützt,
        # Daten auf ext4) und startet nach „Herunterfahren“ sowieso neu. Nur protokollieren.
        if not on and hasattr(p, "busy_reason") and (pid or p.shelly):
            step(f"Ausschalten {reason}")
            why = None if force else p.busy_reason()
            if why == "update":
                step("abgebrochen – Update läuft")
                self._save_log(p, steps, "abgebrochen: Update läuft")
                raise ValueError("Ausschalten gesperrt: im Drucker läuft gerade ein Update")
            if why:
                step(f"Hinweis: {KEEP_GRUND.get(why, why)}")
            if self.pre_off and not force and not why:
                try:
                    r = self.pre_off(p)
                    if r:
                        step(r)
                except Exception as e:
                    step(f"Parken: {e}")
        if pid:
            try:
                self.bb.plug_control(pid, on)
                via = "Bambuddy"
            except Exception as e:
                err = e
                pid = None
        if not pid:
            if not p.shelly:
                raise RuntimeError(f"Keine Steckdose gefunden ({err or 'weder Bambuddy-Steckdose noch Shelly eingetragen'})")
            p.shelly.set(on)
            via = "Shelly direkt" + (f" (Bambuddy: {err})" if err else "")
        now = time.time()
        with self.lock:
            s.update(last_switch=now, off_at=None)
            if on:
                s.update(on_since=now, last_active=now, ended_at=None)
        log.info("[%s] Drucker %s (%s, %s)", p.id, "eingeschaltet" if on else "ausgeschaltet", reason, via)
        if steps:
            step(f"Strom aus ✓ ({via})")
            self._save_log(p, steps, down or "direkt abgeschaltet")
        return via + (f", {down}" if down else "")

    def _watts_now(self, p):
        """Frischer Messwert der Shelly (fürs Abschalt-Protokoll)."""
        if not p.shelly:
            return None
        try:
            w = p.shelly._read()[0]
            return round(w, 1) if w is not None else None
        except Exception:
            return None

    def _log_path(self):
        import os
        return os.path.join(os.path.dirname(self.wstate_path), "abschalt-log.json")

    def _save_log(self, p, steps, result):
        import json
        try:
            data = self.shutdown_log()
            data.insert(0, {"printer": p.id, "name": p.name, "t": steps[0][0], "result": result, "steps": steps})
            import os
            os.makedirs(os.path.dirname(self._log_path()), exist_ok=True)
            with open(self._log_path(), "w") as f:
                json.dump(data[:10], f)
        except Exception as e:
            log.warning("Abschalt-Protokoll: %s", e)

    def shutdown_log(self):
        import json
        try:
            with open(self._log_path()) as f:
                return json.load(f)
        except Exception:
            return []

    def safe_off(self, p):
        try:
            st = p.status()
        except Exception:
            return True, ""
        if st.get("online") and (st.get("printing") or st.get("paused")):
            return False, "es läuft ein Druck"
        if self._shared(p, self.st.get(p.id, {})):
            return False, "diese Bambuddy-Steckdose ist die Shelly eines anderen Druckers – in Bambuddy vom Drucker lösen"
        t = self._nozzle(st)
        lim = float(self.settings.get("power_off_temp") or 45)
        if t is not None and t >= lim:
            return False, f"Düse noch {t:.0f} °C (Grenze {lim:g} °C)"
        return True, ""

    @staticmethod
    def _nozzle(st):
        """Heißeste Düse. Klipper-Drucker mit mehreren Köpfen (U1) melden T0, T1, … – alle zählen."""
        import re as _re
        heads = []
        for t in st.get("temps") or []:
            if _re.fullmatch(r"T\d+", str(t.get("label", ""))):
                try:
                    heads.append(float(t.get("actual")))
                except (TypeError, ValueError):
                    pass
        if heads:
            return max(heads)
        for t in st.get("temps") or []:
            if str(t.get("label", "")).lower().startswith(("düse", "duese", "nozzle", "kopf", "extruder")):
                try:
                    return float(t.get("actual"))
                except (TypeError, ValueError):
                    return None
        return None

    # --- Automatik ------------------------------------------------------------
    def _keep(self, p, st, s):
        """Grund, den Drucker anzulassen – oder None."""
        if st.get("printing") or st.get("paused"):
            return "druck"
        if self._shared(p, s):
            return "doppelt"
        w = p.shelly.watts if (p.shelly and getattr(p.shelly, "watts", None) is not None) else s.get("watts")
        if not st.get("online") and w is not None and w > 15:
            return "fremdlast"
        t = self._nozzle(st)
        if t is not None and t >= float(self.settings.get("power_off_temp") or 45):
            return "heiss"
        ws = self._wstate()
        if ws:
            if ws.get("beschaeftigt"):
                return "waechter"
            if (ws.get("ausblick") or {}).get("grund") == "jetzt" and not ws.get("dry_run"):
                return "auftrag"
        if self.bb.enabled and s.get("bb_printer"):   # Bambuddy startet selbst (ohne „manuell starten“) oder druckt
            try:
                for q in self.bb.queue() or []:
                    if q.get("status") == "printing" or (q.get("status") == "pending" and not q.get("manual")):
                        return "auftrag"
            except Exception:
                pass
        if time.time() - (s.get("last_switch") or 0) < 120:
            return "frisch"
        if hasattr(p, "busy_reason"):            # Klipper: Update, Hochfahren, nicht erreichbar
            try:
                why = p.busy_reason()
                if why:
                    return why
            except Exception:
                pass
        return None

    def tick(self):
        auto = bool(self.settings.get("power_auto"))
        after = float(self.settings.get("power_after_min") or 10) * 60
        idle = float(self.settings.get("power_idle_min") or 30) * 60
        now = time.time()
        for p in self.printers.values():
            s = self.st.setdefault(p.id, {})
            self._plug(p, s)                     # Zuordnung zu Bambuddy (für Warteschlange), zwischengespeichert
            on = self.plug_on(p)
            s["on"] = on
            if on is not True:
                s.update(on_since=None, off_at=None, keep=None, ended_at=None, was_active=False)
                continue
            s.setdefault("on_since", now)
            if not s.get("on_since"):
                s["on_since"] = now
            try:
                st = p.status()
            except Exception:
                continue
            active = bool(st.get("printing") or st.get("paused"))
            if active:
                s.update(last_active=now, was_active=True, ended_at=None, off_at=None, keep="druck")
                continue
            if s.get("was_active"):              # Druck ist gerade zu Ende gegangen
                s.update(was_active=False, ended_at=now)
            base = s["ended_at"] + after if s.get("ended_at") else max(s.get("on_since") or now, s.get("last_active") or 0) + idle
            keep = self._keep(p, st, s)
            s["keep"] = keep
            s["off_at"] = None if keep else base
            s["mode"] = "nach Druck" if s.get("ended_at") else "Leerlauf"
            if not auto or keep or now < base:
                continue
            try:
                via = self.switch(p, False, reason=f"automatisch ({s['mode']})")
                mins = int((now - (s.get('ended_at') or s.get('on_since') or now)) / 60)
                if self.notifier:
                    self.notifier.send("power", f"{p.name}: Drucker ausgeschaltet 🔌",
                                       f"Automatisch {'nach dem Druck' if s.get('ended_at') else 'nach Leerlauf'} "
                                       f"({mins} min, Düse kühl). Über {via}.", "electric_plug")
                s.update(on=False, off_at=None, ended_at=None)
            except Exception as e:
                s["error"] = str(e)
                log.warning("[%s] Automatisches Ausschalten fehlgeschlagen: %s", p.id, e)
                s["off_at"] = now + 300          # in 5 min nochmal versuchen

    def info(self, p):
        s = self.st.get(p.id, {})
        off_at = s.get("off_at")
        return {"available": bool(p.shelly) or bool(s.get("plug_id")), "on": s.get("on"),
                "watts": (p.shelly.watts if p.shelly and p.shelly.online else s.get("watts")),
                "auto": bool(self.settings.get("power_auto")),
                "off_in_s": max(0, int(off_at - time.time())) if (off_at and self.settings.get("power_auto")) else None,
                "keep": KEEP_GRUND.get(s.get("keep")) if s.get("keep") else None, "mode": s.get("mode"),
                "plug": s.get("plug_name"), "bb_auto_off": s.get("bb_auto_off"), "bb_auto_on": s.get("bb_auto_on"),
                "error": s.get("error") or s.get("plug_err")}

    def loop(self):
        time.sleep(20)
        while True:
            try:
                self.tick()
            except Exception as e:
                log.warning("Strom-Verwaltung: %s", e)
            time.sleep(20)
