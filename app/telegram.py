"""Benachrichtigungen über einen eigenen Telegram-Bot – mit Fotos und Antwort-Knöpfen.

- Senden: sendMessage / sendPhoto (HTML), Knöpfe als Inline-Tastatur.
- Empfangen: ein Hintergrund-Thread fragt getUpdates ab (Long-Polling, kein offener Port nötig).
  Knopfdruck → Antwort an den Druckwächter (Datei-Eingang), Nachricht wird aktualisiert.
- Kopplung: „Verbinden“ in PrintHub, dann dem Bot schreiben – PrintHub merkt sich den Chat.
  Nachrichten aus anderen Chats werden ignoriert.
"""
import html
import json
import logging
import os
import re
import threading
import time

import requests

log = logging.getLogger("telegram")
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")

# ntfy-Tags → Emoji (wie die ntfy-App sie vor den Titel setzt)
EMOJI = {
    "printer": "🖨️", "hourglass_flowing_sand": "⏳", "white_check_mark": "✅", "warning": "⚠️", "rotating_light": "🚨",
    "pause_button": "⏸️", "eyes": "👀", "tada": "🎉", "checkered_flag": "🏁", "shopping_cart": "🛒", "snowflake": "❄️",
    "zzz": "💤", "electric_plug": "🔌", "gear": "⚙️", "information_source": "ℹ️", "no_entry": "⛔", "stop_button": "⏹️",
    "stop_sign": "🛑", "x": "❌", "arrow_forward": "▶️", "inbox_tray": "📥", "wrench": "🔧", "link": "🔗", "fire": "🔥",
    "bulb": "💡", "thread": "🧵",
}
PRIO_MARK = {5: " ⏫", 4: " 🔼", 2: " 🔽", 1: " 🔽"}

# Feste Tastenleiste unter dem Eingabefeld: Beschriftung → Befehl
TASTEN = [[("🖨️ Status", "status"), ("📷 Bild", "bild"), ("🧵 Filament", "filament")],
          [("📥 Aufträge", "warteschlange"), ("🏁 Verlauf", "verlauf"), ("📊 Statistik", "statistik")],
          [("🔧 Wartung", "wartung"), ("💡 Licht", "licht"), ("⚡ Strom", "strom"), ("🛑 Platte", "platte")]]
TASTE_ZU_BEFEHL = {l: c for row in TASTEN for l, c in row}
_ALTE_TASTEN = {"warteschlange": "warteschlange", "aufträge": "warteschlange", "auftraege": "warteschlange",
                "queue": "warteschlange", "status": "status", "bild": "bild", "kamera": "bild", "foto": "bild",
                "filament": "filament", "spulen": "filament", "lager": "filament", "verlauf": "verlauf",
                "statistik": "statistik", "wartung": "wartung", "licht": "licht", "strom": "strom",
                "platte": "platte", "hilfe": "hilfe"}


def _taste_alt(text):
    """Knopf-Text ohne Emoji („📥 Warteschlange“) → Befehl; None, wenn es kein bekannter Knopf ist."""
    if not text or text.startswith("/") or len(text) > 30:
        return None
    wort = re.sub(r"[^\wäöüß ]", "", text.lower()).strip()
    return _ALTE_TASTEN.get(wort)


class Telegram:
    def __init__(self, settings, inbox_path, status_fn=None):
        self.settings = settings
        self.inbox = inbox_path
        self.status_fn = status_fn          # liefert Text für /status
        self.pair_until = 0.0
        self.last_error = None
        self.bot_name = None
        self._offset = 0
        self._token_seen = None
        self.candidate = None              # letzte Nachricht aus einem fremden Chat: (chat, Name, Zeit)
        self.commands = {}                 # "filament": (Beschreibung, fn(args) -> [Nachricht, …])
        self.on_button = None              # fn(data) -> (Kurztext, [Nachricht, …]) für Knöpfe „ph:…“

    # --- Zustand ------------------------------------------------------------
    @property
    def token(self):
        return (self.settings.get("tg_token") or "").strip()

    @property
    def chat(self):
        return str(self.settings.get("tg_chat") or "").strip()

    @property
    def enabled(self):
        return bool(self.token and self.chat)

    def _url(self, method):
        return f"{API}/bot{self.token}/{method}"

    def _call(self, method, _t=20, files=None, **params):
        """_t = Wartezeit der Anfrage (Sekunden); params gehen an Telegram."""
        if files:
            r = requests.post(self._url(method), data=params, files=files, timeout=_t)
        else:
            r = requests.post(self._url(method), json=params, timeout=_t)
        try:
            d = r.json()
        except ValueError:
            r.raise_for_status()
            raise RuntimeError("Antwort von Telegram nicht lesbar")
        if not d.get("ok"):
            raise RuntimeError(d.get("description") or f"Telegram-Fehler {r.status_code}")
        return d.get("result")

    def info(self):
        out = {"token_set": bool(self.token), "chat": self.chat, "pairing": self.pair_until > time.time(),
               "bot": self.bot_name, "error": self.last_error}
        return out

    def start_pairing(self):
        if not self.token:
            raise ValueError("Zuerst den Bot-Token eintragen")
        me = self._call("getMe")
        self.bot_name = me.get("username")
        c = self.candidate
        if c and time.time() - c[2] < 900 and c[0] != self.chat:   # schon vorher geschrieben (z. B. „Starten“)
            self._pair(c[0], c[1])
            return self.bot_name
        self.pair_until = time.time() + 600
        return self.bot_name

    def _pair(self, chat, name):
        self.settings.update({"tg_chat": chat})
        self.pair_until = 0
        self.candidate = None
        log.info("Telegram mit Chat %s verbunden", chat)
        self.send("PrintHub verbunden ✅", f"Hallo {name}! Ab jetzt kommen deine Druckmeldungen hierher.", chat=chat)
        self.send_help(chat=chat)

    # --- Senden -------------------------------------------------------------
    @staticmethod
    def _text(title, message, tags=None, prio=3):
        """Aufbau wie in ntfy: Symbol + fetter Titel (+ Pfeil für die Dringlichkeit), darunter der Text."""
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        icon = next((EMOJI[t] for t in (tags or []) if t in EMOJI), "")
        if icon and icon.rstrip("\ufe0f") in title:     # Symbol steht schon im Titel → nach vorne holen
            title = title.replace(icon, "").replace(icon.rstrip("\ufe0f"), "").replace("  ", " ").strip()
        t = (icon + " " if icon else "") + f"<b>{html.escape(title)}</b>" + PRIO_MARK.get(int(prio or 3), "")
        if message:
            t += "\n" + html.escape(message)
        return t

    def keyboard(self):
        return {"keyboard": [[{"text": l} for l, _ in row] for row in TASTEN], "is_persistent": True,
                "resize_keyboard": True, "input_field_placeholder": "Befehl wählen oder /hilfe"}

    def send(self, title, message, prio=3, photo=None, buttons=None, chat=None, tags=None, keyboard=False, pin=False):
        """buttons: Liste (Beschriftung, Antwort) – Antwort geht beim Drücken an den Druckwächter.
        keyboard=True: feste Tastenleiste mitschicken · pin=True: Nachricht oben im Chat anheften."""
        chat = chat or self.chat
        if not (self.token and chat):
            return False
        now = time.time()                                  # Notbremse: höchstens 20 Nachrichten pro Minute
        self._sent_times = [t for t in getattr(self, "_sent_times", []) if now - t < 60]
        if len(self._sent_times) >= 20:
            if not getattr(self, "_brake_logged", False):
                log.warning("Telegram: mehr als 20 Nachrichten pro Minute – weitere werden kurz zurückgehalten")
                self._brake_logged = True
            return False
        self._brake_logged = False
        self._sent_times.append(now)
        text = self._text(title, message, tags, prio)
        kb = None
        if buttons:
            rows = []
            for b in buttons[:8]:
                row = b if isinstance(b, list) and b and isinstance(b[0], (list, tuple)) else [b]   # [(l, a), (l, a)] = eine Zeile
                rows.append([{"text": str(l)[:60], "callback_data": str(a)[:64]} for l, a in row])
            kb = json.dumps({"inline_keyboard": rows})
        silent = int(prio or 3) <= 2
        try:
            if photo:
                cap = text if len(text) <= 1000 else text[:990] + "…"
                params = {"chat_id": chat, "caption": cap, "parse_mode": "HTML", "disable_notification": str(silent).lower(),
                          "show_caption_above_media": "true"}   # Text über dem Bild – wie in ntfy
                if kb:
                    params["reply_markup"] = kb
                try:
                    self._call("sendPhoto", _t=40, files={"photo": ("drucker.jpg", photo, "image/jpeg")}, **params)
                    self.last_error = None
                    return True
                except Exception as e:
                    log.info("Telegram-Foto fehlgeschlagen (%s) – sende ohne Bild", e)
            params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_notification": silent,
                      "disable_web_page_preview": True}
            if kb:
                params["reply_markup"] = json.loads(kb)
            elif keyboard:
                params["reply_markup"] = self.keyboard()
            res = self._call("sendMessage", **params)
            self.last_error = None
            if pin and res and res.get("message_id"):
                try:
                    self._call("unpinAllChatMessages", chat_id=chat)
                    self._call("pinChatMessage", chat_id=chat, message_id=res["message_id"], disable_notification=True)
                except Exception as e:
                    log.info("Anheften nicht möglich: %s", e)
            return True
        except Exception as e:
            self.last_error = str(e)
            log.warning("Telegram fehlgeschlagen: %s", e)
            return False

    # --- Empfangen ----------------------------------------------------------
    def _answer_to_engine(self, cid, data):
        try:
            os.makedirs(os.path.dirname(self.inbox), exist_ok=True)
            with open(self.inbox, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": f"tg-{cid}", "msg": data, "time": int(time.time())}) + "\n")
        except OSError as e:
            log.warning("Antwort konnte nicht an den Druckwächter übergeben werden: %s", e)

    def _handle(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            msg = cq.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != self.chat:
                return
            data = cq.get("data") or ""
            if data == "noop":
                try:
                    self._call("answerCallbackQuery", callback_query_id=cq["id"], text="Schon erledigt")
                except Exception:
                    pass
                return
            if data.startswith("ph:"):          # Knopf für PrintHub selbst (z. B. Licht) – nicht an den Druckwächter
                kurz, msgs = ("", [])
                try:
                    kurz, msgs = self.on_button(data) if self.on_button else ("", [])
                except Exception as e:
                    kurz = f"Fehler: {e}"[:180]
                try:
                    self._call("answerCallbackQuery", callback_query_id=cq["id"], text=kurz or "✓")
                except Exception:
                    pass
                self._send_all(msgs)
                return
            label = data
            for row in ((msg.get("reply_markup") or {}).get("inline_keyboard") or []):
                for b in row:
                    if b.get("callback_data") == data:
                        label = b.get("text", data)
            self._answer_to_engine(cq.get("id"), data)
            log.info("Telegram-Antwort: %s", data)
            try:
                self._call("answerCallbackQuery", callback_query_id=cq["id"], text=f"✓ {label}")
                self._call("editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"),
                           reply_markup={"inline_keyboard": [[{"text": f"✓ {label} – erledigt", "callback_data": "noop"}]]})
            except Exception:
                pass
            return
        m = upd.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        text = (m.get("text") or "").strip()
        if not chat or (m.get("from") or {}).get("is_bot"):
            return
        if not text or m.get("pinned_message"):      # Systemnachrichten (z. B. „angeheftet“) nie beantworten
            return
        name = (m.get("from") or {}).get("first_name") or ""
        if chat != self.chat:
            if (m.get("chat") or {}).get("type", "private") != "private":
                return
            if self.pair_until > time.time():
                self._pair(chat, name)
            else:
                self.candidate = (chat, name, float(m.get("date") or time.time()))
            return
        alt = _taste_alt(text)
        if text in TASTE_ZU_BEFEHL:
            cmd, args = TASTE_ZU_BEFEHL[text], ""
        elif alt:                      # Knopf einer älteren Tastenleiste → ausführen und neue Leiste schicken
            cmd, args = alt, ""
            self._neue_leiste = True
        else:
            cmd, _, args = text.partition(" ")
            cmd = cmd.lstrip("/").split("@")[0].lower()
        alias = {"stand": "status", "help": "hilfe", "start": "hilfe", "queue": "warteschlange", "lager": "filament",
                 "spulen": "filament", "kamera": "bild", "foto": "bild", "stats": "statistik",
                 "aufträge": "warteschlange", "auftraege": "warteschlange", "auftrag": "warteschlange"}
        cmd = alias.get(cmd, cmd)
        if cmd == "status":
            try:
                body = self.status_fn() if self.status_fn else "–"
            except Exception as e:
                body = f"Status nicht verfügbar: {e}"
            self.send("Druckerstatus", body, tags="printer")
        elif cmd in self.commands:
            try:
                msgs = self.commands[cmd][1](args.strip())
            except Exception as e:
                log.warning("Telegram-Befehl /%s: %s", cmd, e)
                msgs = [{"title": f"/{cmd} hat nicht geklappt", "text": str(e), "tags": "warning"}]
            self._send_all(msgs)
        elif text.startswith("/"):
            self.send_help(pin=False)
        # normaler Text ohne Befehl: nicht antworten
        if getattr(self, "_neue_leiste", False):
            self._neue_leiste = False
            self._help_at = 0
            self.send_help(pin=True)

    def send_help(self, chat=None, pin=True):
        now = time.time()
        if now - getattr(self, "_help_at", 0) < 20:      # Schutz: höchstens alle 20 s
            return
        self._help_at = now
        lines = ["/status – Stand der Drucker"] + [f"/{k} – {v[0]}" for k, v in self.commands.items() if k != "hilfe"]
        lines += ["", "Die Knöpfe unten führen dieselben Befehle aus.", "/hilfe zeigt diese Liste und die Knöpfe wieder an."]
        self.send("PrintHub – Befehle", "\n".join(lines), tags="information_source", keyboard=True, pin=pin, chat=chat)

    def _send_all(self, msgs):
        for m in msgs or []:
            if m.get("help"):
                self.send_help()
                continue
            self.send(m.get("title", ""), m.get("text", ""), m.get("prio", 3), m.get("photo"), m.get("buttons"), tags=m.get("tags"))

    def register_commands(self):
        """Befehle im Telegram-Menü („/“) anzeigen."""
        cmds = [{"command": "status", "description": "Stand der Drucker"}] + \
               [{"command": k, "description": v[0][:60]} for k, v in self.commands.items()]
        try:
            self._call("setMyCommands", commands=cmds)
        except Exception as e:
            log.info("Telegram-Befehlsmenü nicht gesetzt: %s", e)

    def loop(self):
        time.sleep(5)
        fails = 0
        while True:
            tok = self.token
            if not tok:
                time.sleep(20)
                continue
            if tok != self._token_seen:      # neuer Bot: alte Nachrichten überspringen
                self._token_seen, self._offset = tok, 0
                try:
                    old = self._call("getUpdates", _t=15)
                    for u in old or []:               # alte Nachrichten nicht ausführen, aber Absender merken
                        m = u.get("message") or {}
                        if m.get("chat", {}).get("type") == "private" and str(m["chat"]["id"]) != self.chat:
                            self.candidate = (str(m["chat"]["id"]), (m.get("from") or {}).get("first_name") or "", float(m.get("date") or 0))
                    if old:
                        self._offset = old[-1]["update_id"] + 1
                    self.bot_name = (self._call("getMe") or {}).get("username")
                    self.register_commands()
                except Exception as e:
                    self.last_error = str(e)
            try:
                ups = self._call("getUpdates", _t=65, offset=self._offset, timeout=50, allowed_updates=["message", "callback_query"])
                fails = 0
                for u in ups or []:
                    self._offset = u["update_id"] + 1
                    try:
                        self._handle(u)
                    except Exception as e:
                        log.warning("Telegram-Nachricht nicht verarbeitet: %s", e)
            except Exception as e:
                fails += 1
                self.last_error = str(e)
                if fails == 3:
                    log.warning("Telegram nicht erreichbar: %s", e)
                time.sleep(min(60, 5 * fails))
