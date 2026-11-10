"""Protokoll für die Oberfläche: die letzten Einträge im Speicher – mit geschwärzten Zugangsdaten."""
import logging
import re
import threading
import time
from collections import deque

_BOT = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I)
_KEYS = re.compile(r"((?:api[_-]?key|token|access[_-]?code|password|passwort)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{4,}", re.I)


class Redactor:
    """Schwärzt bekannte Geheimnisse (Werte aus Einstellungen/config) und typische Muster."""

    def __init__(self):
        self.secrets_fn = lambda: []

    def __call__(self, text):
        text = str(text)
        text = _BOT.sub("bot•••", text)
        text = _BEARER.sub(r"\1•••", text)
        text = _KEYS.sub(r"\1•••", text)
        for s in self.secrets_fn() or []:
            s = str(s or "")
            if len(s) >= 4:
                text = text.replace(s, "•••")
        return text


redact = Redactor()


class BufferHandler(logging.Handler):
    def __init__(self, n=1500):
        super().__init__(logging.INFO)
        self.buf = deque(maxlen=n)
        self._buf_lock = threading.Lock()   # eigener Name – „lock“ gehört logging.Handler selbst

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += " | " + logging.Formatter().formatException(record.exc_info).splitlines()[-1]
            with self._buf_lock:
                self.buf.append((record.created, record.levelname, record.name, msg))
        except Exception:
            pass

    def entries(self, min_level="INFO", limit=400, source=None):
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        lv = order.get(min_level, 20)
        with self._buf_lock:
            rows = [r for r in self.buf if order.get(r[1], 20) >= lv and (not source or r[2] == source)]
        return [{"time": t, "level": l, "source": n, "msg": redact(m)} for t, l, n, m in rows[-limit:]]


handler = BufferHandler()


class _RedactFilter(logging.Filter):
    """Auch im Docker-Log keine Zugangsdaten."""
    def filter(self, record):
        try:
            msg = record.getMessage()
            clean = redact(msg)
            if clean != msg:
                record.msg, record.args = clean, ()
        except Exception:
            pass
        return True


def install():
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, _RedactFilter) for f in h.filters):
            h.addFilter(_RedactFilter())
    if handler not in root.handlers:
        root.addHandler(handler)
    # uvicorn hat eigene Logger ohne Weitergabe – Fehler davon trotzdem mitnehmen
    for name in ("uvicorn.error",):
        lg = logging.getLogger(name)
        if handler not in lg.handlers:
            lg.addHandler(handler)


def fmt_time(t):
    return time.strftime("%d.%m. %H:%M:%S", time.localtime(t))
