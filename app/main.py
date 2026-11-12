"""PrintHub – Status, Spulenzuordnung und Verbrauchsbuchung für Bambu- und Klipper-Drucker."""
import base64
import contextlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections import defaultdict, deque

import requests
import yaml
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .bambu import BambuPrinter
from .moonraker import MoonrakerPrinter
from . import dockerctl
from .history import History
from .bambuddy import Bambuddy
from . import backup
from .nachtruhe import Nachtruhe
from .plate import Plate
from .maintenance import Maintenance
from .metrics import EnergyLog, TempHistory, printer_stats
from . import stats as statsmod
from .notify import Notifier
from .qr import QR
from .settings import Settings
from .shelly import Shelly
from . import labelprint
from .spoolman import Spoolman
from .telegram import Telegram
from .power import Power
from .queuecheck import QueueCheck
from . import schedule as sched
from .nachtruhe import read_env
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from .store import Store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from . import logbuf
logbuf.install()
log = logging.getLogger("printhub")


class _QuietAccess(logging.Filter):
    """Blendet die ständigen Abfragen der Oberfläche im Log aus – übrig bleibt das Wichtige."""
    NOISE = ("GET /api/state", "GET /api/spools", "camera.jpg", "GET /static/", "GET /media/",
             "GET /api/history", "GET /api/settings", "GET /api/profiles", "GET / ", "GET /verlauf", "GET /einstellungen",
             "GET /api/lager", "GET /lager", "GET /api/qr.svg", "GET /api/lager/external", "/temps", "/energy", "GET /api/stats",
             "GET /api/services", "GET /api/queue", "GET /api/bambuddy/thumb", "GET /api/maintenance", "GET /statistik", "GET /api/statistics", "GET /api/nachtruhe",
             "GET /api/progress", "GET /sw.js", "GET /manifest.webmanifest")

    def filter(self, record):
        msg = record.getMessage()
        return not any(n in msg for n in self.NOISE)


logging.getLogger("uvicorn.access").addFilter(_QuietAccess())

CONFIG = os.environ.get("CONFIG", "/config/config.yaml")
DATA = os.environ.get("DATA", "/data/state.json")
STATIC = os.path.join(os.path.dirname(__file__), "static")

if not os.path.exists(CONFIG):            # Erststart: leere Konfiguration – Drucker trägt man in der Oberfläche ein
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(CONFIG) or ".", exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write("# PrintHub – Drucker werden unter Einstellungen → Drucker eingetragen\nprinters: []\n")
try:
    with open(CONFIG, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
except OSError:
    cfg = {}

store = Store(DATA)
DATA_DIR = os.path.dirname(DATA) or "."
settings = Settings(os.path.join(DATA_DIR, "settings.json"), cfg)
history = History(os.path.join(DATA_DIR, "history.db"))
MEDIA_DIR = os.path.join(DATA_DIR, "media")
HISTORY_DB = os.path.join(DATA_DIR, "history.db")
temps = TempHistory()
energy = EnergyLog(HISTORY_DB)
autobackup = backup.AutoBackup(settings, DATA_DIR, CONFIG, log)
maint = Maintenance(store)
maint.history = history
nachtruhe = Nachtruhe(DATA_DIR, settings)
plate = Plate(DATA_DIR)
import secrets as _secrets
INTERNAL_TOKEN = _secrets.token_hex(16)   # nur für den Druckwächter im selben Container
maint_due = {}
if history.count() == 0 and store.data.get("history"):
    history.import_old(store.data["history"])
    log.info("Bisherigen Verlauf (%d Drucke) in history.db übernommen", len(store.data["history"]))
spoolman = Spoolman(settings["spoolman_url"] or cfg.get("spoolman_url") or "http://localhost:7912")
bambuddy = Bambuddy(settings["bambuddy_url"], settings["bambuddy_key"])


def _apply_services():
    """Nach dem Speichern der Einstellungen: neue Adressen sofort verwenden."""
    url = (settings["spoolman_url"] or cfg.get("spoolman_url") or "").rstrip("/")
    if url and url != spoolman.url:
        spoolman.url = url
        spoolman.invalidate()
        log.info("Spoolman-Adresse geändert: %s", url)
    bambuddy.configure(settings["bambuddy_url"], settings["bambuddy_key"])
TYPES = {"bambu": BambuPrinter, "moonraker": MoonrakerPrinter}
notifier = Notifier(settings, os.path.dirname(DATA) or ".")
notifier.waechter_running = lambda: nachtruhe.running()


def _restart_on_start(printer, job):
    """OctoEverywhere & Co. bei neuem Druck neu starten (Einstellungen → Automatik)."""
    if printer.type != "bambu" or not settings["oe_restart"]:
        return
    names = [n.strip() for n in settings["oe_container"].split(",") if n.strip()]
    if names:
        dockerctl.restart_later(names, int(settings["oe_delay"]))


printers = {}
for pc in cfg.get("printers", []):
    p = TYPES[pc["type"]](pc, store, spoolman)
    p.notifier = notifier
    p.settings = settings
    p.history = history
    p.media_dir = MEDIA_DIR
    p.on_job_start.append(_restart_on_start)
    if pc.get("shelly"):
        p.shelly = Shelly(pc["shelly"], store, pc["id"], int(pc.get("shelly_channel", 0)))
    printers[pc["id"]] = p

# Druckwächter: Zugang zu PrintHub (Plattenerkennung) – Kamera des (ersten) Bambu-Druckers
_plate_pid = next((p.id for p in printers.values() if p.type == "bambu" and p.camera), "")
TG_INBOX = os.path.join(DATA_DIR, "nachtruhe", "inbox.jsonl")   # Knopf-Antworten aus Telegram → Druckwächter
nachtruhe.extra_env = {"PRINTHUB_URL": "http://127.0.0.1:8095", "PRINTHUB_TOKEN": INTERNAL_TOKEN, "PRINTHUB_PLATE_PID": _plate_pid,
                       "PRINTHUB_INBOX": TG_INBOX, "PRINTHUB_WSTATE": os.path.join(DATA_DIR, "nachtruhe", "zustand.json"),
                       "PRINTHUB_QEVENTS": os.path.join(DATA_DIR, "nachtruhe", "auftraege.jsonl"),
                       "PRINTHUB_PLANS": os.path.join(DATA_DIR, "nachtruhe", "plaene.json")}
PLANS = nachtruhe.extra_env["PRINTHUB_PLANS"]


def _tg_status():
    lines = []
    for p in printers.values():
        try:
            st = p.status()
        except Exception as e:
            lines.append(f"{p.name}: {e}")
            continue
        if not p.online:
            lines.append(f"⚫ {p.name} – offline")
        elif st.get("printing"):
            rem = st.get("remaining_min")
            job = st.get("job") or ""
            sg = st.get("stage")
            lines.append(f"🟢 {p.name} – {st.get('state')} {st.get('progress') or 0} %"
                         + (f"\n{job}" if job else "")
                         + (f"\n{sg['icon']} {sg['text']}" if sg else "")
                         + ((f"\nnoch {int(rem) // 60} h {int(rem) % 60:02d} min" if int(rem) >= 60 else f"\nnoch {int(rem)} min") if rem else ""))
        else:
            lines.append(f"⚪ {p.name} – {st.get('state', 'bereit')}")
    q = ""
    try:
        if bambuddy.enabled:
            n = len(bambuddy.queue() or [])
            q = "\n\n📥 Warteschlange: " + ("leer" if not n else f"{n} Auftrag" if n == 1 else f"{n} Aufträge")
    except Exception:
        pass
    return "\n\n".join(lines) + q


telegram = Telegram(settings, TG_INBOX, _tg_status)
power = Power(settings, printers, bambuddy, notifier, os.path.join(DATA_DIR, "nachtruhe", "zustand.json"))
qcheck = QueueCheck(bambuddy, store, lambda: _spool_map(), lambda: settings.get("short_reserve_g") or 10)
QEVENTS = os.path.join(DATA_DIR, "nachtruhe", "auftraege.jsonl")
notifier.telegram = telegram


def _spool_map():
    try:
        return {s["id"]: s for s in spoolman.spools()}, ""
    except Exception as e:
        return {}, f"Spoolman nicht erreichbar: {e.__class__.__name__}"


async def index(request):
    return page("index.html")


async def history_page(request):
    return page("history.html")


async def stats_page(request):
    return page("statistik.html")


_vendor_cache = {"t": 0, "v": {}}


def _vendors():
    if time.time() - _vendor_cache["t"] > 300:
        try:
            _vendor_cache["v"] = spoolman.vendors_by_spool()
            _vendor_cache["t"] = time.time()
        except Exception as e:
            log.debug("Hersteller aus Spoolman: %s", e)
    return _vendor_cache["v"]


def _statistics(rng, printer):
    if rng not in statsmod.RANGES:
        rng = "month"
    st = statsmod.compute(history.rows({}), rng, printer, _vendors())
    # Stromzähler (Shelly) stundengenau – unabhängig davon, ob gedruckt wurde
    erng = rng if rng in ("week", "month", "year") else "year"
    bars, has = None, []
    for p in printers.values():
        if not p.shelly or (printer and p.name != printer):
            continue
        has.append(p.name)
        ser = energy.series(p.id, erng)
        if bars is None:
            bars = [{"label": b["label"], "kwh": 0.0} for b in ser]
        for i, b in enumerate(ser):
            bars[i]["kwh"] = round(bars[i]["kwh"] + b["kwh"], 3)
    price = float(settings["price_kwh"] or 0)
    tot = sum(b["kwh"] for b in bars or [])
    st["meter"] = {"printers": has, "bars": bars or [], "kwh": round(tot, 3), "cost": round(tot * price, 2), "price": price}
    return st


async def api_statistics(request):
    q = request.query_params
    return JSONResponse(await run_in_threadpool(_statistics, q.get("range", "month"), q.get("printer", "")))


async def api_statistics_csv(request):
    q = request.query_params
    st = await run_in_threadpool(_statistics, q.get("range", "month"), q.get("printer", ""))
    return Response(statsmod.to_csv(st), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="printhub-statistik-{st["range"]}.csv"'})


def _bb_err(e):
    import requests
    if isinstance(e, requests.ConnectionError):
        return "Bambuddy nicht erreichbar – Adresse unter Einstellungen → Dienste prüfen"
    if isinstance(e, requests.Timeout):
        return "Bambuddy antwortet nicht (Zeitüberschreitung)"
    return f"Bambuddy: {str(e)[:160]}"


def _parse_dt(v):
    from datetime import datetime, timezone
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _import_bambuddy_history():
    """Bisherige Drucke aus dem Bambuddy-Druckprotokoll übernehmen (doppelte werden übersprungen)."""
    items = bambuddy.print_log()
    bam = [p for p in printers.values() if p.type == "bambu"]
    names = {p.name.strip().lower(): p for p in printers.values()}
    added = skipped = 0
    for it in items:
        status = (it.get("status") or "").lower()
        if status in ("printing", "skipped", "pending", ""):
            continue
        ext = f"bambuddy:{it['id']}"
        if history.has_ext(ext):
            skipped += 1
            continue
        p = names.get((it.get("printer_name") or "").strip().lower()) or (bam[0] if len(bam) == 1 else None)
        pname = p.name if p else (it.get("printer_name") or "Bambuddy")
        end = _parse_dt(it.get("completed_at")) or _parse_dt(it.get("started_at"))
        if not end:
            continue
        if history.near(pname, end):   # hat PrintHub schon selbst erfasst
            skipped += 1
            continue
        start = _parse_dt(it.get("started_at"))
        dur = it.get("duration_seconds") or (int(end - start) if start else None)
        if status == "completed":
            outcome, result, reason = "ok", "fertig", None
        elif status == "failed":
            outcome, result, reason = "fail", "fehlgeschlagen", it.get("failure_reason") or "Druckerfehler"
        else:
            outcome, result, reason = "cancel", "abgebrochen", it.get("failure_reason") or "abgebrochen"
        grams = it.get("filament_used_grams") or 0
        types = [t.strip() for t in str(it.get("filament_type") or "").replace(";", ",").split(",") if t.strip()]
        colors = [c.strip() for c in str(it.get("filament_color") or "").replace(";", ",").split(",") if c.strip()]
        n = max(1, len(types))
        usage = [{"slot": "aus Bambuddy", "grams": round(grams / n, 1), "material": types[i] if i < len(types) else None,
                  "color": colors[i] if i < len(colors) else (colors[0] if colors else None),
                  "cost": round((it.get("cost") or 0) / n, 2) if it.get("cost") is not None else None,
                  "booked": False, "error": "aus Bambuddy übernommen"} for i in range(n)] if grams else []
        thumb = None
        try:
            img = bambuddy.print_log_thumbnail(it["id"])
            if img:
                os.makedirs(MEDIA_DIR, exist_ok=True)
                thumb = f"bb_{it['id']}.png"
                with open(os.path.join(MEDIA_DIR, thumb), "wb") as f:
                    f.write(img)
        except Exception:
            pass
        kwh = it.get("energy_kwh") or None
        fc, ec, wc = it.get("cost"), it.get("energy_cost") if kwh else None, it.get("wear_cost")
        total = round((fc or 0) + (ec or 0) + (wc or 0), 2) if any(x is not None for x in (fc, ec, wc)) else None
        history.add({"time": int(end), "printer": pname, "job": it.get("print_name") or "Druck", "result": result,
                     "outcome": outcome, "reason": reason, "duration": dur, "grams": grams, "usage": usage,
                     "kwh": kwh, "energy_cost": ec, "filament_cost": fc, "wear_cost": wc, "total_cost": total,
                     "thumb": thumb, "source": "bambuddy", "ext_id": ext,
                     "estimate": it.get("estimate_seconds")})
        added += 1
    return {"added": added, "skipped": skipped, "total": len(items)}


async def api_history_import_bambuddy(request):
    if not settings["bambuddy_import"]:
        return JSONResponse({"error": "Bambuddy-Import ist ausgeschaltet (Einstellungen → Import / Export)"}, status_code=403)
    if not bambuddy.enabled:
        return JSONResponse({"error": "Bambuddy-Adresse fehlt (Einstellungen → Dienste)"}, status_code=400)
    try:
        r = await run_in_threadpool(_import_bambuddy_history)
    except Exception as e:
        return JSONResponse({"error": _bb_err(e)}, status_code=502)
    log.info("Verlauf aus Bambuddy übernommen: %s", r)
    return JSONResponse({"ok": True, **r})


async def settings_page(request):
    return page("settings.html")


def _filters(q):
    return {k: q.get(k) for k in ("from", "to", "printer", "spool", "material", "q", "result") if q.get(k)}


async def api_history(request):
    q = request.query_params
    data = await run_in_threadpool(history.query, _filters(q), int(q.get("limit", 50)), int(q.get("offset", 0)))
    return JSONResponse(data)


async def api_history_stats(request):
    q = request.query_params
    months = await run_in_threadpool(history.monthly, _filters(q), int(q.get("months", 12)))
    facets = await run_in_threadpool(history.facets)
    return JSONResponse({"months": months, "facets": facets})


async def api_history_csv(request):
    text = await run_in_threadpool(history.csv, _filters(request.query_params))
    return Response(text.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="printhub-verlauf.csv"'})


async def api_history_delete(request):
    pid = int(request.path_params["pid"])
    for name in history.media_of(pid):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(MEDIA_DIR, name))
    await run_in_threadpool(history.delete, pid)
    return JSONResponse({"ok": True})


async def api_settings(request):
    if request.method == "PUT":
        try:
            body = await request.json()
            body_keys = set(body)
            vals = settings.update(body)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _apply_services()
        if body_keys & {"ntfy_url", "ntfy_token"} and nachtruhe.running():
            nachtruhe.restart()   # Druckwächter bekommt die neue ntfy-Adresse
        return JSONResponse({"ok": True, "settings": vals})
    info = {
        "spoolman_url": spoolman.url,
        "printers": [{"id": p.id, "name": p.name, "type": p.type, "shelly": p.shelly.host if p.shelly else None}
                     for p in printers.values()],
        "history_count": history.count(),
        "docker": os.path.exists(dockerctl.SOCK),
        "version": os.environ.get("PRINTHUB_VERSION", "dev"),
    }
    return JSONResponse({"settings": settings.all(), "info": info})


async def api_profiles(request):
    if request.method == "DELETE":
        fid = request.path_params.get("fid")
        with store.lock:
            store.data.setdefault("profiles", {}).pop(str(fid), None)
            store._save()
        return JSONResponse({"ok": True})
    names = {}
    try:
        for sp in spoolman.spools():
            if sp.get("filament_id") is not None:
                names[str(sp["filament_id"])] = {"name": sp["name"], "color": sp.get("color"), "material": sp.get("material")}
    except Exception:
        pass
    prof = store.data.get("profiles", {})
    return JSONResponse([{"filament_id": k, **v, **names.get(k, {"name": f"Filament {k}"})} for k, v in prof.items()])


async def api_docker_test(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    raw = b.get("container") or settings["oe_container"]
    names = [n.strip() for n in str(raw).split(",") if n.strip()]
    try:
        found = [dockerctl.find_container(n) for n in names]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "found": found})


MEDIA_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(png|jpg)$")


async def media(request):
    name = request.path_params["name"]
    path = os.path.join(MEDIA_DIR, name)
    if not MEDIA_RE.match(name) or not os.path.isfile(path):
        return Response(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "max-age=31536000, immutable"})


# --- Zugriffsschutz (PIN) ----------------------------------------------------------
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
FAILS = defaultdict(deque)
OPEN_PATHS = ("/login", "/api/login", "/static/style.css", "/static/common.js", "/manifest.webmanifest", "/sw.js",
              "/static/icon.svg", "/static/icon-192.png", "/static/icon-512.png", "/static/icon-maskable.png", "/static/apple-touch-icon.png")


def _is_external(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    if ip in TAILSCALE:
        return True
    return not (ip.is_private or ip.is_loopback)


def _needs_pin(request):
    mode = settings["pin_mode"]
    if mode == "off" or not settings.get("pin_hash"):
        return False
    if mode == "external" and not _is_external(request.client.host if request.client else ""):
        return False
    return True


# ---------- Seiten mit Versionskennung ausliefern ----------
# Nach einem Update holt der Browser so sicher die neuen CSS/JS-Dateien (statt alter Kopien aus dem Zwischenspeicher).
def _asset_version():
    v = os.environ.get("PRINTHUB_VERSION", "")
    if v and v != "dev":
        return v
    try:
        return str(int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC))))
    except Exception:
        return str(int(time.time()))


ASSET_V = _asset_version()
_PAGES = {}
_ASSET_RE = re.compile(r'(/static/[A-Za-z0-9_-]+\.(?:css|js))(?=["\'])')


def page(name):
    html = _PAGES.get(name)
    if html is None or os.environ.get("PRINTHUB_DEV"):
        with open(os.path.join(STATIC, name), encoding="utf-8") as f:
            html = _ASSET_RE.sub(lambda m: f"{m.group(1)}?v={ASSET_V}", f.read())
        _PAGES[name] = html
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


class NoCacheMiddleware(BaseHTTPMiddleware):
    """CSS/JS/Seiten immer beim Server nachfragen (ETag → meist nur „unverändert“, also schnell)."""
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") and path.endswith((".css", ".js", ".html")) and "cache-control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or not _needs_pin(request) or request.headers.get("x-printhub-token") == INTERNAL_TOKEN:
            return await call_next(request)
        tok = settings.auth_token()
        if tok and request.cookies.get("ph_auth") == tok:
            return await call_next(request)
        if path.startswith("/api/") or path.startswith("/media/"):
            return JSONResponse({"error": "Anmeldung nötig"}, status_code=401)
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + nxt, status_code=303)


async def login_page(request):
    return page("login.html")


async def api_login(request):
    host = request.client.host if request.client else "?"
    q = FAILS[host]
    now = time.time()
    while q and now - q[0] > 300:
        q.popleft()
    if len(q) >= 5:
        return JSONResponse({"error": "Zu viele Fehlversuche – bitte 5 Minuten warten"}, status_code=429)
    body = await request.json()
    if not settings.check_pin(body.get("pin")):
        q.append(now)
        log.warning("Falsche PIN von %s", host)
        return JSONResponse({"error": "PIN falsch"}, status_code=401)
    q.clear()
    r = JSONResponse({"ok": True})
    r.set_cookie("ph_auth", settings.auth_token(), max_age=365 * 86400, httponly=True, samesite="lax")
    return r


async def api_logout(request):
    r = JSONResponse({"ok": True})
    r.delete_cookie("ph_auth")
    return r


async def api_pin(request):
    body = await request.json()
    try:
        settings.set_pin(body.get("pin"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    r = JSONResponse({"ok": True, "settings": settings.all()})
    r.set_cookie("ph_auth", settings.auth_token(), max_age=365 * 86400, httponly=True, samesite="lax")
    return r


async def labels_page(request):
    return page("etiketten.html")


async def api_qr(request):
    text = request.query_params.get("text", "")
    if not text or len(text) > 200:
        return Response("Text fehlt oder zu lang", status_code=400)
    svg = QR(text).svg()
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


def _label_spools(ids):
    spools = {s["id"]: s for s in spoolman.spools(0)}
    return [spools[i] for i in ids if i in spools]


async def api_label_preview(request):
    sid = request.query_params.get("spool", "")
    media = request.query_params.get("media") or settings["label_media"]
    try:
        sp = (await run_in_threadpool(_label_spools, [int(sid)]))[0] if sid.isdigit() else \
            {"id": 12, "name": "Jayo PETG Schwarz", "material": "PETG", "vendor": "Jayo", "color": "#111111"}
    except IndexError:
        return JSONResponse({"error": "Spule nicht gefunden"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"Spoolman nicht erreichbar: {e}"}, status_code=502)
    img = await run_in_threadpool(labelprint.render, sp, f"{public_base(request)}/assign?spool={sp['id']}", media, settings["label_len"])
    return Response(labelprint.png(img), media_type="image/png", headers={"Cache-Control": "no-store"})


async def api_label_print(request):
    host = settings["label_host"]
    if not host:
        return JSONResponse({"error": "Etikettendrucker fehlt (Einstellungen → Spulen → Etikettendrucker)"}, status_code=400)
    b = await request.json()
    ids = [int(x) for x in (b.get("spools") or []) if str(x).isdigit()][:100]
    copies = max(1, min(10, int(b.get("copies") or 1)))
    try:
        sps = await run_in_threadpool(_label_spools, ids) if ids else []
    except Exception as e:
        return JSONResponse({"error": f"Spoolman nicht erreichbar: {e}"}, status_code=502)
    if b.get("test"):
        sps = [{"id": 0, "name": "PrintHub Testetikett", "material": "PLA", "vendor": "Test", "color": "#ffffff"}]
    if not sps:
        return JSONResponse({"error": "Keine Spulen ausgewählt"}, status_code=400)
    base, media = public_base(request), settings["label_media"]

    def job():
        imgs = [labelprint.render(sp, f"{base}/assign?spool={sp['id']}", media, settings["label_len"]) for sp in sps for _ in range(copies)]
        labelprint.send(host, labelprint.raster(imgs, media, settings["label_model"], settings["label_cut"]))
        return len(imgs)
    try:
        n = await run_in_threadpool(job)
    except OSError as e:
        return JSONResponse({"error": f"Etikettendrucker {host} nicht erreichbar ({e.__class__.__name__}). Eingeschaltet und im WLAN?"}, status_code=502)
    log.info("%d Etikett(en) an %s gesendet", n, host)
    return JSONResponse({"ok": True, "printed": n})


async def api_label_media(request):
    return JSONResponse({"media": {k: v["t"] for k, v in labelprint.MEDIA.items()}, "models": list(labelprint.MODELS),
                         "host": settings["label_host"], "current": settings["label_media"]})


def public_base(request):
    base = (settings.get("public_url") or "").strip().rstrip("/")
    return base or str(request.base_url).rstrip("/")


async def api_labels(request):
    ids = [int(x) for x in request.query_params.get("spools", "").split(",") if x.strip().isdigit()]
    try:
        spools = {s["id"]: s for s in await run_in_threadpool(spoolman.spools, 0)}
    except Exception as e:
        return JSONResponse({"error": f"Spoolman nicht erreichbar: {e}"}, status_code=502)
    base = public_base(request)
    out = []
    for i in ids:
        sp = spools.get(i)
        if sp:
            out.append({**sp, "url": f"{base}/assign?spool={i}"})
    return JSONResponse({"labels": out, "base": base})


async def lager_page(request):
    return page("lager.html")


def _slot_names():
    """Spule -> Drucker/Slot laut PrintHub."""
    out = {}
    for p in printers.values():
        for sl in p.slots():
            sid = store.get_slot(p.id, sl["key"])
            if sid:
                out[sid] = p.location_name(sl["key"])
    return out


def _release_spool(sid):
    """Spule aus allen PrintHub-Slots austragen (z. B. leer gelaufen und schon gewechselt)."""
    for p in printers.values():
        for sl in p.slots():
            if store.get_slot(p.id, sl["key"]) == sid:
                store.set_slot(p.id, sl["key"], None)
                log.info("Lager: Spule #%s aus %s ausgetragen", sid, p.location_name(sl["key"]))


async def api_printer_swap(request):
    p = printers.get(request.path_params["pid"])
    if not p or not hasattr(p, "swap_slot"):
        return JSONResponse({"error": "Drucker unbekannt"}, status_code=404)
    try:
        b = await request.json()
        msg = await run_in_threadpool(p.swap_slot, b.get("slot"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "message": msg})


# ---------------- Sorten bequemer anlegen ----------------
from . import colors as colormod


def _own_filaments():
    try:
        return spoolman._get("/filament")
    except Exception:
        return []


def _dl(a, b):
    """Damerau-Levenshtein-Abstand (Tippfehler inkl. vertauschter Buchstaben)."""
    d = [[i] + [0] * len(b) for i in range(len(a) + 1)]
    for j in range(1, len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _word_score(w, words, typos=True):
    """0 = Wortanfang, 1 = ohne doppelte Buchstaben, 2 = Tippfehler, None = kein Treffer."""
    if any(x.startswith(w) for x in words):
        return 0
    dd = lambda x: re.sub(r"(.)\1+", r"\1", x)
    if len(w) >= 3 and any(dd(x).startswith(dd(w)) for x in words):
        return 1
    if len(w) < 3 or not typos:
        return None
    tol = 1 if len(w) <= 5 else 2
    for x in words:
        for L in (len(w) - 1, len(w), len(w) + 1):
            if 2 <= L <= len(x) and _dl(w, x[:L]) <= tol:
                return 2
    return None


def _name_score(qw, name, typos=True):
    nw = [w for w in re.split(r"[^a-z0-9äöüß+]+", (name or "").lower()) if w]
    sc = []
    for w in qw:
        x = _word_score(w, nw, typos)
        if x is None:
            return None
        sc.append(x)
    return max(sc)


def _color_suggest(q, vendor="", material="", limit=8, dbman="", others=6):
    """Namensvorschläge beim Tippen: eigene Sorten, Filament-Datenbank (Hersteller zuerst), eingebaute Farbliste,
    zuletzt ein paar Treffer anderer Hersteller (Farbton nur ungefähr). Jedes eingegebene Wort muss ein Wortanfang
    im Namen sein („tennis court“ → „Tennis Court Green“); kleine Tippfehler sind erlaubt."""
    qw = [w for w in re.split(r"[^a-z0-9äöüß+]+", (q or "").lower()) if w]
    if not qw:
        return []
    enw = [w for w in re.split(r"[^a-z0-9+]+", (colormod.to_english(q) or "").lower()) if w]
    def score(name, typos=True):
        r = [x for x in (_name_score(qw, name, typos), _name_score(enw, name, typos) if enw and enw != qw else None) if x is not None]
        return min(r) if r else None
    vend, mat, dman = (vendor or "").strip().lower(), (material or "").strip().upper(), (dbman or "").strip().lower()
    out, seen = [], set()
    def add(name, hx, src, rank, sc):
        k = (name or "").strip().lower()
        if not k or k in seen or not hx:
            return
        seen.add(k)
        out.append({"name": name.strip(), "hex": hx[:6].upper(), "source": src, "rank": rank, "score": sc})
    for f in _own_filaments():
        v = ((f.get("vendor") or {}).get("name") or "").lower()
        sc = score(f.get("name"))
        if sc is not None:
            add(f.get("name"), f.get("color_hex"), "eigene Sorte", 0 if v == vend else 2, sc)
    try:
        ext = spoolman.external_filaments()
    except Exception:
        ext = []
    rest = []
    for f in ext:
        mine = (f.get("manufacturer") or "").lower() == vend
        sc = score(f.get("name"), typos=mine)      # Tippfehler-Suche nur beim eigenen Hersteller (sonst zu langsam)
        if sc is None:
            continue
        if mine:
            same = not mat or (f.get("material") or "").upper() == mat
            add(f.get("name"), f.get("color_hex"), "Datenbank" + ("" if same else f" ({f.get('material')})"), 1 if same else 3, sc)
        else:
            rest.append((sc, f))
    for k, hx in colormod.NAMES.items():
        sc = score(k)
        if sc is not None:
            add(k.title(), hx, "Farbliste", 4, sc)
    out.sort(key=lambda x: (x["score"] > 1, x["rank"], x["score"], not x["name"].lower().startswith(qw[0]), len(x["name"])))
    out = out[:limit]
    # andere Hersteller (oben in der Datenbank-Suche gewählter zuerst), gleiche Namen nur einmal
    rest.sort(key=lambda t: ((t[1].get("manufacturer") or "").lower() != dman, t[0], len(t[1].get("name") or "")))
    n = 0
    for sc, f in rest:
        if n >= others:
            break
        k = (f.get("name") or "").strip().lower()
        if k in seen or not f.get("color_hex"):
            continue
        seen.add(k)
        out.append({"name": f["name"].strip(), "hex": f["color_hex"][:6].upper(), "rank": 9, "score": sc, "other": True,
                    "source": f"≈ {f.get('manufacturer')}" + (f" ({f.get('material')})" if f.get("material") else "")})
        n += 1
    return out


async def api_lager_colorsuggest(request):
    q = request.query_params
    return JSONResponse({"items": await run_in_threadpool(_color_suggest, q.get("q", ""), q.get("vendor", ""), q.get("material", ""), 8, q.get("dbman", ""))})


def _color_lookup(name, vendor="", material=""):
    """Farbname → {hex, source, exact}. Reihenfolge: 1. SpoolmanDB (gleicher Hersteller + Material + Name),
    2. deine Sorte mit genau diesem Namen (selbst gewählte Farben), 3. SpoolmanDB gleicher Hersteller anderes Material,
    4. eingebaute Farbliste, 5. andere Hersteller. Nichts gefunden → hex None (dann Pipette anbieten)."""
    n = (name or "").strip()
    if not n:
        return {"hex": None}
    en = colormod.to_english(n)
    words = [w for w in re.split(r"\s+", en.lower()) if w]
    vend, mat = (vendor or "").strip().lower(), (material or "").strip().upper()
    try:
        ext = spoolman.external_filaments()
    except Exception:
        ext = []
    def hit(f):
        nm = (f.get("name") or "").lower()
        return f.get("color_hex") and all(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", nm) for w in words)
    def db(f, exact=True, prefix="SpoolmanDB"):
        return {"hex": f["color_hex"][:6].upper(), "exact": exact, "source": f"{prefix}: {f.get('manufacturer')} {f.get('material')} {f.get('name')}"}
    t1 = [f for f in ext if hit(f) and (f.get("manufacturer") or "").lower() == vend and (not mat or (f.get("material") or "").upper() == mat)]
    if t1:
        return db(min(t1, key=lambda f: len(f.get("name") or "")))
    own = [f for f in _own_filaments() if (f.get("name") or "").strip().lower() == n.lower() and f.get("color_hex")]
    own.sort(key=lambda f: ((f.get("vendor") or {}).get("name") or "").lower() != vend)
    if own:
        f = own[0]
        return {"hex": f["color_hex"][:6].upper(), "exact": True,
                "source": f"eigene Sorte: {((f.get('vendor') or {}).get('name') or '').strip()} {f.get('material') or ''} {f.get('name')}".replace("  ", " ")}
    t2 = [f for f in ext if hit(f) and (f.get("manufacturer") or "").lower() == vend]
    if t2:
        return db(min(t2, key=lambda f: len(f.get("name") or "")))
    hx, k = colormod.from_list(en)
    if hx:
        return {"hex": hx, "exact": False, "source": f"Farbliste („{k}“) – ungefähr"}
    others = [f for f in ext if hit(f)]
    if others:
        return db(min(others, key=lambda f: len(f.get("name") or "")), False, "SpoolmanDB, anderer Hersteller") | {
            "source": db(min(others, key=lambda f: len(f.get("name") or "")))["source"].replace("SpoolmanDB:", "SpoolmanDB, anderer Hersteller:") + " – ungefähr"}
    sug = _color_suggest(n, vendor, material, limit=1, others=0)
    if sug:
        g = sug[0]
        return {"hex": g["hex"], "exact": False, "suggest": g["name"], "source": f"meintest du „{g['name']}“? ({g['source']})"}
    return {"hex": None, "source": "nicht gefunden"}


async def api_lager_colorhex(request):
    q = request.query_params
    return JSONResponse(await run_in_threadpool(_color_lookup, q.get("name", ""), q.get("vendor", ""), q.get("material", "")))


def _template(vendor_id=None, material="", fid=None):
    """Werte einer vorhandenen Sorte als Vorlage (gleicher Hersteller + Material, zuletzt angelegt)."""
    fils = spoolman.inventory()["filaments"]
    if fid:
        cand = [f for f in fils if f["id"] == int(fid)]
    else:
        mat = (material or "").strip().upper()
        cand = [f for f in fils if (f.get("vendor") or {}).get("id") == (int(vendor_id) if vendor_id else None)
                and (f.get("material") or "").upper() == mat]
    if not cand:
        return None
    f = max(cand, key=lambda f: f["id"])
    v = f.get("vendor") or {}
    return {"id": f["id"], "label": " ".join(x for x in [v.get("name", ""), f.get("material") or "", f.get("name") or ""] if x),
            "vendor_id": v.get("id"), "material": f.get("material"), "weight": f.get("weight"),
            "spool_weight": f.get("spool_weight") or v.get("empty_spool_weight"), "price": f.get("price"),
            "extruder_temp": f.get("settings_extruder_temp"), "bed_temp": f.get("settings_bed_temp"),
            "density": f.get("density"), "diameter": f.get("diameter")}


def _defaults(vendor_id=None, vendor_name="", material="", color=""):
    """Werte für eine neue Sorte mit Herkunft. Reihenfolge: deine Sorte (Hersteller + Material) → SpoolmanDB
    (gleicher Hersteller + Material, gleiche Farbe bevorzugt). Farbton: SpoolmanDB → deine Sorte gleichen Namens."""
    out, src = {}, {}
    vname = (vendor_name or "").strip()
    if vendor_id and not vname:
        vname = next((v.get("name") for v in spoolman._get("/vendor") if v["id"] == int(vendor_id)), "") or ""
    tpl = _template(vendor_id, material) if vendor_id and material else None
    if tpl:
        for k in ("extruder_temp", "bed_temp", "spool_weight", "density", "diameter", "weight", "price"):
            if tpl.get(k) not in (None, ""):
                out[k], src[k] = tpl[k], ("own", tpl["label"])
    db, dbc = None, None
    try:
        ext = [f for f in spoolman.external_filaments() if (f.get("manufacturer") or "").strip().lower() == vname.lower()] if vname else []
    except Exception:
        ext = []
    if ext and material:
        mat = _mat_norm(material)
        same = [f for f in ext if _mat_norm(f.get("material")) == mat]
        cl = (color or "").strip().lower()
        dbc = next((f for f in same if (f.get("name") or "").strip().lower() == cl), None) if cl else None
        db = dbc or (same[0] if same else None)
    if db:
        lab = f"{db.get('manufacturer')} {db.get('material')}" + (f" {db.get('name')}" if dbc else "")
        for k, dk in (("extruder_temp", "extruder_temp"), ("bed_temp", "bed_temp"), ("spool_weight", "spool_weight"),
                      ("density", "density"), ("diameter", "diameter"), ("weight", "weight")):
            if k not in out and db.get(dk) not in (None, ""):
                out[k], src[k] = db[dk], ("db", lab)
    if color:
        if dbc and dbc.get("color_hex"):
            out["color"], src["color"] = dbc["color_hex"][:6].upper(), ("db", f"{dbc.get('manufacturer')} {dbc.get('material')} {dbc.get('name')}")
            out["external_id"] = dbc.get("id")
        else:
            r = _color_lookup(color, vname, material)
            if r.get("hex"):
                out["color"], src["color"] = r["hex"], ("own" if (r.get("source") or "").startswith("eigene") else
                                                        "db" if r.get("exact") else "approx", r.get("source"))
    return {"values": out, "src": {k: {"kind": a, "label": b} for k, (a, b) in src.items()}}


async def api_lager_defaults(request):
    q = request.query_params
    return JSONResponse(await run_in_threadpool(_defaults, q.get("vendor_id") or None, q.get("vendor", ""),
                                                q.get("material", ""), q.get("color", "")))


async def api_lager_template(request):
    q = request.query_params
    return JSONResponse({"template": await run_in_threadpool(_template, q.get("vendor_id"), q.get("material", ""), q.get("id"))})


def _multi(b):
    """Mehrere Farben auf einmal. Zeile: {name, hex, count, lots:[{lot, n}], fid (vorhandene Sorte → nur Rollen)}."""
    vendor_id = b.get("vendor_id")
    rows = [r for r in (b.get("colors") or []) if (r.get("name") or "").strip() or r.get("fid")]
    if not vendor_id and (b.get("vendor_name") or "").strip() and any(not r.get("fid") for r in rows):
        vendor_id = spoolman.create_vendor(b["vendor_name"].strip(), _num(b.get("spool_weight")))["id"]
    made, added, rolls = [], [], 0
    loc = b.get("location") or settings["storage_location"]
    for row in rows:
        if row.get("fid"):
            fid = int(row["fid"])
            added.append(fid)
        else:
            data = _fil_data({**b, "name": row["name"], "color": row.get("hex") or "", "external_id": row.get("external_id")}, vendor_id)
            fid = spoolman.create_filament(data)["id"]
            made.append(fid)
        lots = [(str(x.get("lot") or "").strip(), int(x.get("n") or 0)) for x in (row.get("lots") or [])]
        lots = [(l, n) for l, n in lots if l and n > 0]
        total = max(int(row.get("count") or 0), sum(n for _, n in lots))
        for lot, n in lots:
            spoolman.create_spools(fid, n, loc, None, None, lot)
            _learn_code(lot, fid)
        rest = total - sum(n for _, n in lots)
        if rest > 0:
            spoolman.create_spools(fid, rest, loc)
        rolls += total
    log.info("Lager: %s Sorten neu, %s vorhandene ergänzt, %s Rollen", len(made), len(added), rolls)
    return {"ok": True, "ids": made + added, "new": len(made), "existing": len(added), "rolls": rolls}


async def api_lager_multi(request):
    try:
        return JSONResponse(await run_in_threadpool(_multi, await request.json()))
    except (ValueError, RuntimeError, KeyError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _bulk(b):
    data = {}
    for k, key, conv in (("price", "price", float), ("extruder_temp", "settings_extruder_temp", int),
                         ("bed_temp", "settings_bed_temp", int), ("spool_weight", "spool_weight", float),
                         ("weight", "weight", float), ("density", "density", float), ("diameter", "diameter", float)):
        v = _num(b.get(k))
        if v is not None:
            data[key] = conv(v)
    if b.get("material"):
        data["material"] = str(b["material"]).strip().upper()
    if not data:
        raise ValueError("Nichts zu ändern – leere Felder bleiben, wie sie sind")
    ids = [int(i) for i in b.get("ids") or []]
    for fid in ids:
        spoolman.update_filament(fid, data)
    log.info("Lager: %s Sorten gemeinsam geändert: %s", len(ids), ", ".join(data))
    return {"ok": True, "count": len(ids)}


async def api_lager_bulk(request):
    try:
        return JSONResponse(await run_in_threadpool(_bulk, await request.json()))
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_lager_vendor(request):
    b = await request.json()
    try:
        await run_in_threadpool(spoolman.update_vendor, int(request.path_params["vid"]),
                                {"empty_spool_weight": _num(b.get("empty_spool_weight"))})
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


def _names():
    """Sorten mit deutschem Farbnamen → Vorschlag Originalname (Datenbank, sonst übersetzt)."""
    out = []
    try:
        ext = spoolman.external_filaments()
    except Exception:
        ext = []
    for f in spoolman.inventory()["filaments"]:
        nm = f.get("name") or ""
        if not colormod.has_german(nm):
            continue
        en = colormod.to_english(nm)
        vend = ((f.get("vendor") or {}).get("name") or "").lower()
        # gibt es in der Datenbank beim selben Hersteller genau diese Farbe? → deren Schreibweise
        for e in ext:
            if (e.get("manufacturer") or "").lower() == vend and (e.get("name") or "").lower() == en.lower():
                en = e["name"]
                break
        out.append({"id": f["id"], "old": nm, "new": en, "label": f"{(f.get('vendor') or {}).get('name', '')} {f.get('material', '')}".strip()})
    return out


async def api_lager_names(request):
    if request.method == "POST":
        b = await request.json()
        n = 0
        for r in b.get("renames") or []:
            if (r.get("name") or "").strip():
                await run_in_threadpool(spoolman.update_filament, int(r["id"]), {"name": r["name"].strip()})
                n += 1
        log.info("Lager: %s Sortennamen vereinheitlicht", n)
        return JSONResponse({"ok": True, "count": n})
    return JSONResponse({"items": await run_in_threadpool(_names)})


# ---------------- Rolle per Foto / Strichcode erkennen ----------------
from . import scan as scanmod


def _code_norm(code):
    return re.sub(r"\s+", "", str(code or "")).upper()


def _learn_code(code, fid, quiet=False):
    """Nummer (EAN, Strichcode, Charge vom Etikett) → Sorte merken. Taucht dieselbe Nummer bei einer anderen,
    noch vorhandenen Sorte auf, ist sie keine Sortennummer → nicht mehr zur Erkennung verwenden."""
    code = _code_norm(code)
    if len(code) < 4 or not fid:
        return
    fid = int(fid)
    with store.lock:
        m = store.data.setdefault("ean_map", {})
        bad = store.data.setdefault("code_conflict", [])
        if code in bad or m.get(code) == fid:
            if code in bad:
                ml = store.data.setdefault("code_multi", {}).setdefault(code, [])
                if fid not in ml:
                    ml.append(fid); store._save()
            return
        old = m.get(code)
        if old and old != fid:
            try:
                exists = any(f["id"] == old for f in spoolman._get("/filament"))
            except Exception:
                exists = True
            if exists:
                m.pop(code, None)
                bad.append(code)
                store.data.setdefault("code_multi", {})[code] = [old, fid]
                store._save()
                log.info("Lager: Nummer %s gehört zu mehreren Sorten (#%s, #%s) – wird nicht mehr zur Erkennung genutzt", code, old, fid)
                return
        m[code] = fid
        store._save()
    if not quiet:
        log.info("Lager: Nummer %s gehört jetzt zu Sorte #%s", code, fid)


def _similar_codes(code):
    """Bekannte Nummern, die sich nur in 1–2 Ziffern unterscheiden (gleiche Länge, gleiche Buchstaben) – z. B. Chargen
    04360703AJ ↔ 04360705AJ. Liefert [(nummer, sorte)], nächste zuerst, jede Sorte einmal."""
    code = _code_norm(code)
    if len(code) < 6:
        return []
    out = []
    for c, fid in (store.data.get("ean_map") or {}).items():
        if len(c) != len(code) or c == code or re.sub(r"\d", "", c) != re.sub(r"\d", "", code):
            continue
        diff = [i for i, (a, b) in enumerate(zip(c, code)) if a != b]
        if len(diff) <= 2 and all(c[i].isdigit() and code[i].isdigit() for i in diff) and min(diff) >= len(code) // 2 - 1:
            out.append((len(diff), sum(abs(int(c[i]) - int(code[i])) for i in diff), c, fid))
    out.sort()
    seen, res = set(), []
    for _, _, c, fid in out:
        if fid not in seen:
            seen.add(fid); res.append((c, fid))
    return res[:3]


def _codes_for(fid):
    return sorted(c for c, f in (store.data.get("ean_map") or {}).items() if f == int(fid))


def _backfill_codes():
    """Einmalig: Chargen vorhandener Rollen ihren Sorten zuordnen."""
    if store.data.get("codes_backfilled"):
        return
    try:
        spools = spoolman._get("/spool", allow_archived="true")
    except Exception as e:
        log.info("Lager: Nummern rückwirkend lernen – Spoolman nicht erreichbar (%s), später nochmal", e)
        return
    n = 0
    for sp in spools:
        lot, fid = sp.get("lot_nr"), (sp.get("filament") or {}).get("id")
        if lot and fid:
            _learn_code(lot, fid, quiet=True)
            n += 1
    with store.lock:
        store.data["codes_backfilled"] = 1
        store._save()
    log.info("Lager: %s Chargen vorhandener Rollen gelernt (%s Nummern bekannt)", n, len(store.data.get("ean_map") or {}))


async def api_lager_codes(request):
    """GET ?fid= → Nummern einer Sorte; POST {code, remove:true} → Zuordnung löschen."""
    if request.method == "GET":
        return JSONResponse({"codes": _codes_for(request.query_params.get("fid") or 0)})
    b = await request.json()
    code = _code_norm(b.get("code"))
    if b.get("fid") and b.get("soft"):
        _learn_code(code, int(b["fid"]))          # wie beim Anlegen: gleiche Nummer bei anderer Sorte → mehrdeutig, nicht umhängen
        return JSONResponse({"ok": True})
    if b.get("fid") and not b.get("remove"):
        # von Hand zuordnen (neu oder geändert) – gilt auch für vorher gesperrte Nummern
        new = code
        if len(new) < 4:
            return JSONResponse({"error": "Nummer zu kurz (mind. 4 Zeichen)"}, status_code=400)
        with store.lock:
            m = store.data.setdefault("ean_map", {})
            if b.get("old"):
                m.pop(_code_norm(b["old"]), None)
            if new in store.data.setdefault("code_conflict", []):
                store.data["code_conflict"].remove(new)
            other = m.get(new)
            m[new] = int(b["fid"])
            store._save()
        log.info("Lager: Nummer %s von Hand Sorte #%s zugeordnet%s", new, b["fid"], f" (vorher #{other})" if other and other != int(b["fid"]) else "")
        return JSONResponse({"ok": True, "moved_from": other if other and other != int(b["fid"]) else None})
    with store.lock:
        (store.data.get("ean_map") or {}).pop(code, None)
        if b.get("block") and code not in store.data.setdefault("code_conflict", []):
            store.data["code_conflict"].append(code)
        store._save()
    log.info("Lager: Nummer %s von Hand entfernt", code)
    return JSONResponse({"ok": True})


def _hexdist(a, b):
    try:
        a, b = a.strip("#")[:6], b.strip("#")[:6]
        return sum(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) for i in (0, 2, 4))
    except (ValueError, AttributeError, IndexError):
        return 999


def _mat_norm(m):
    return re.sub(r"[\s_]+", "", (m or "").upper()).replace("PLUS", "+").replace("PRO", "+")


def _scan_match(parsed, brand=""):
    """Sorten aus dem Lager nach Ähnlichkeit: Material, Farbe, Hersteller, Gewicht."""
    inv = spoolman.inventory()
    out = []
    pm, pc = _mat_norm(parsed.get("material")), parsed.get("color_hex")
    for f in inv["filaments"]:
        fm = _mat_norm(f.get("material"))
        sc, why = 0, []
        if pm and fm:
            if pm == fm:
                sc += 5; why.append("Material")
            elif pm.rstrip("+") == fm.rstrip("+") or fm.startswith(pm) or pm.startswith(fm):
                sc += 2; why.append("Material ähnlich")
            else:
                sc -= 8
        if pc and f.get("color_hex"):
            d = _hexdist(pc, f["color_hex"])
            if d < 70:
                sc += 4; why.append("Farbe")
            elif d < 150:
                sc += 1
            else:
                sc -= 2
        if parsed.get("color"):
            fn, pn = _tnorm(f.get("name")).strip(), _tnorm(parsed["color"]).strip()
            if fn == pn:
                sc += 12; why.append("Farbname")
            elif parsed.get("color_src") != "base" and fn and pn and fn not in pn and pn not in fn:
                sc -= 6                          # Etikett nennt eine andere Farbe
        
        vend = ((f.get("vendor") or {}).get("name") or "")
        if brand and vend and (brand.lower() in vend.lower() or vend.lower() in brand.lower()):
            sc += 3; why.append("Hersteller")
        if parsed.get("weight") and f.get("weight"):
            sc += 2 if abs(parsed["weight"] - f["weight"]) <= 20 else -2
        if sc >= 4:
            out.append({"id": f["id"], "label": " ".join(x for x in [vend, f.get("name") or "", f.get("material") or ""] if x),
                        "color": "#" + (f.get("color_hex") or "999999")[:6], "score": sc, "why": ", ".join(why)})
    out.sort(key=lambda x: -x["score"])
    return out[:6]


_MAT_WORDS = {"pla", "pla+", "petg", "abs", "asa", "tpu", "pa", "pc", "pva", "hips", "pctg", "silk", "matte", "plus", "pro",
              "hf", "cf", "high", "speed", "basic", "1.75mm", "175", "1kg"}
_BASE = ["black", "white", "grey", "gray", "silver", "red", "blue", "green", "yellow", "orange", "purple", "violet",
         "pink", "brown", "beige", "gold", "bronze", "transparent", "clear", "natural", "cyan"]


_SYN = {"grey": "gray", "colour": "color", "transparant": "transparent"}


def _tnorm(t):
    """Klein, nur Wörter, Schreibvarianten vereinheitlicht (Grey = Gray)."""
    w = re.sub(r"[^a-z0-9äöüß+]+", " ", (t or "").lower()).split()
    return " " + " ".join(_SYN.get(x, x) for x in w) + " "


def _detect_vendor(text):
    """Herstellername im Etikett-Text (aus der SpoolmanDB-Herstellerliste), längster Treffer."""
    tn = _tnorm(text)
    try:
        mans = {(f.get("manufacturer") or "").strip() for f in spoolman.external_filaments()}
    except Exception:
        mans = set()
    try:
        mans |= {v.get("name") for v in spoolman._get("/vendor") if v.get("name")}
    except Exception:
        pass
    hits = [m for m in mans if m and len(m) >= 3 and _tnorm(m) in tn]
    return max(hits, key=len) if hits else ""


def _db_color(text, vendor, material=""):
    """Farbname aus dem Etikett-Text gegen die SpoolmanDB-Namen dieses Herstellers – längster Treffer gewinnt."""
    vend, mat, tn = (vendor or "").strip().lower(), _mat_norm(material), _tnorm(text)
    if not vend:
        return None
    try:
        ext = spoolman.external_filaments()
    except Exception:
        return None
    best = None
    for f in ext:
        if (f.get("manufacturer") or "").strip().lower() != vend or not f.get("color_hex"):
            continue
        core = " ".join(w for w in _tnorm(f.get("name")).split() if w not in _MAT_WORDS)
        if len(core) < 3 or not (f" {core} " in tn or (core not in _BASE and _fuzzy_in(core, tn))):
            continue
        key = (len(core), _mat_norm(f.get("material")) == mat)
        if not best or key > best[0]:
            best = (key, f)
    if not best:
        return None
    f = best[1]
    return {"name": f["name"].strip(), "hex": f["color_hex"][:6].upper(), "material": f.get("material"), "external_id": f.get("id")}


def _fuzzy_in(core, tn):
    """Alle Wörter von core kommen (in Reihenfolge, mit kleinen Lesefehlern) im normalisierten Text vor."""
    words, toks = core.split(), tn.split()
    for i in range(len(toks) - len(words) + 1):
        ok = True
        for j, w in enumerate(words):
            t = toks[i + j]
            if t == w:
                continue
            if len(w) >= 4 and abs(len(t) - len(w)) <= 1 and _dl(w, t) <= 1:
                continue
            ok = False
            break
        if ok:
            return True
    return False


def _base_ok(word, text):
    """Grundfarbe („White“) zählt nur, wenn davor kein weiteres Farbwort steht („Mystic Red“ ≠ „Red“)."""
    for line in (text or "").splitlines():
        alts = [word] + [k for k, v in _SYN.items() if v == word]
        for m in re.finditer(r"([^\s]*)\s*\b(" + "|".join(map(re.escape, alts)) + r")\b", line, flags=re.I):
            prev = re.sub(r"[^a-zäöü+]", "", m.group(1).lower())
            if not prev or prev in _MAT_WORDS or len(prev) <= 2:
                return True
    return False


def _own_color(text, material="", vendor=""):
    """Eigene Sorte, deren Farbname auf dem Etikett steht. Bei mehreren Treffern: längster Name, gleiches Material,
    gleicher Hersteller bevorzugt."""
    tn, mat, vend = _tnorm(text), _mat_norm(material), (vendor or "").strip().lower()
    best = None
    for f in _own_filaments():
        core = " ".join(w for w in _tnorm(f.get("name")).split() if w not in _MAT_WORDS)
        if len(core) < 3 or (core in _BASE and not _base_ok(core, text)):
            continue
        if mat and _mat_norm(f.get("material")) != mat:
            continue
        fv = ((f.get("vendor") or {}).get("name") or "").strip()
        if vend and fv.lower() != vend:
            continue
        if not (f" {core} " in tn or _fuzzy_in(core, tn)):
            continue
        key = (len(core), f" {core} " in tn)
        if not best or key > best[0]:
            best = (key, f, fv)
    if not best:
        return None
    f, fv = best[1], best[2]
    return {"id": f["id"], "name": f["name"], "hex": (f.get("color_hex") or "999999")[:6].upper(), "vendor": fv}


def _scan(img_bytes=None, code=None, vendor="", material=""):
    from PIL import Image
    res = {"codes": [], "parsed": {}, "matches": [], "learned": None, "online": None}
    alltexts = []
    img = None
    if img_bytes:
        from PIL import ImageOps
        t0 = time.time()
        img = Image.open(io.BytesIO(img_bytes))
        img.draft("RGB", (2600, 2600))       # JPEG gleich verkleinert dekodieren (40-MP-Handyfotos sonst sehr langsam)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((2600, 2600))          # Handy schickt das Original (schneller als dort zu verkleinern)
        t1 = time.time()
        codes = scanmod.barcodes(img)
        res["timing"] = {"bytes": len(img_bytes), "decode": round(t1 - t0, 2), "barcode": round(time.time() - t1, 2)}
        res["codes"] = [{"type": c["type"], "data": c["data"]} for c in codes]
    else:
        codes = []
    if code:
        res["codes"].insert(0, {"type": "manuell", "data": code.strip()})
    eans = [c["data"] for c in res["codes"] if scanmod.is_ean(c["data"])]
    others = [c["data"] for c in res["codes"] if not scanmod.is_ean(c["data"]) and not c["data"].startswith("http")]
    res["ean"] = eans[0] if eans else None
    res["lot"] = others[0] if others and not eans else (others[0] if others else None)
    known = store.data.get("ean_map") or {}
    # Text auf dem Etikett: erst der Bereich um den Strichcode, dann das ganze Foto
    if img is not None:
        texts = []
        for c in codes:
            if c.get("box"):
                texts += scanmod.band_texts(img, c)
                # Farbe schon gefunden? Sonst Stufe 2: gedreht, Schatten ausgleichen, ganzes Etikett
                tj, pv = "\n".join(texts), scanmod.parse_votes(texts)
                if not ((_own_color(tj, pv.get("material", ""), vendor) or pv.get("color")) and pv.get("material") and pv.get("weight")):
                    texts += scanmod.band_texts(img, c, stage=2)
                    texts += scanmod.ocr_texts(scanmod.label_crop(img, c), small=True)
                break
        p = scanmod.parse_votes(texts)
        alltexts += texts
        if not texts or not all(k in p for k in ("material", "color", "weight")):
            ft = scanmod.ocr_texts(img, small=not texts)
            alltexts += ft
            full = scanmod.parse_votes(ft)
            p = {**full, **p}
        res["parsed"] = p
    if res.get("timing"):
        res["timing"]["total_ocr"] = round(time.time() - t1, 2)
    # Nummern vom Etikett (Charge/Artikel, z. B. „04260702AJ“) – auch ohne Strichcode
    res["ocr_codes"] = scanmod.find_codes(alltexts)[:3]
    if not res["lot"] and res["ocr_codes"]:
        res["lot"] = res["ocr_codes"][0]
    cands = [c for c in [res["ean"]] + others + res["ocr_codes"] if c]
    for c in cands:
        if _code_norm(c) in known:
            res["learned"] = known[_code_norm(c)]
            res["code_info"] = {"code": _code_norm(c), "status": "known", "fids": [known[_code_norm(c)]]}
            break
    if not res["learned"] and res["lot"]:
        lot = _code_norm(res["lot"])
        multi = (store.data.get("code_multi") or {}).get(lot)
        sim = _similar_codes(lot)
        if multi:
            res["code_info"] = {"code": lot, "status": "multi", "fids": multi}
        elif sim:
            res["code_info"] = {"code": lot, "status": "similar", "fids": [f for _, f in sim], "near": [c for c, _ in sim]}
        else:
            res["code_info"] = {"code": lot, "status": "unknown", "fids": []}
    # Hersteller + Farbe: SpoolmanDB zuerst, dann eingebaute Farbliste, sonst nur Grundton („Red“) → Vorschläge
    p = res["parsed"]
    text = "\n".join(alltexts)
    vend = (vendor or "").strip() or p.get("brand") or _detect_vendor(text)
    if vend:
        p.setdefault("brand", vend)
    if material and not p.get("material"):
        p["material"] = material
    # 1. eigene Sorte, deren Name auf dem Etikett steht (z. B. „PETG White“ → JAYO PETG White) – auch ohne Hersteller
    own = _own_color(text, p.get("material") or material, vend) if text.strip() else None
    if own:
        p["color"], p["color_hex"], p["color_src"] = own["name"], own["hex"], "own"
        if not vend and own.get("vendor"):
            vend = own["vendor"]; p["brand"] = vend
        res["own_fid"] = own["id"]
    dbc = None if own else (_db_color(text, vend, p.get("material") or material) if text.strip() else None)
    if dbc and dbc["name"].strip().lower() in _BASE:
        dbc = None          # nur „Red“ o. ä. getroffen – könnte „Mystic Red“ sein → Grundton + Vorschläge
    if dbc:
        p["color"], p["color_hex"], p["color_src"] = dbc["name"], dbc["hex"], "db"
        p["external_id"] = dbc["external_id"]
    elif own:
        pass
    elif p.get("color") and p["color"].strip().lower() not in _BASE:
        p["color_src"] = "list"
    else:
        tn = _tnorm(text)
        base = (p.get("color") or "").strip().lower() or next((w for w in _BASE if f" {w} " in tn), None)
        if base not in _BASE:
            base = next((w for w in _BASE if f" {w} " in tn), None)
        if base:
            p["color"], p["color_src"] = base.title(), "base"
    brand = res["parsed"].get("brand") or ""
    if res["ean"] and not res["learned"]:
        on = scanmod.online_lookup(requests, res["ean"])
        res["online"] = on
        if on and not on.get("error"):
            brand = on.get("brand") or ""
            res["parsed"] = {**scanmod.parse_text(on.get("text") or ""), **{k: v for k, v in res["parsed"].items() if v}}
            res["parsed"]["brand"] = brand
    try:
        res["matches"] = _scan_match(res["parsed"], brand)
    except Exception as e:
        log.info("Scan: Abgleich mit dem Lager fehlgeschlagen: %s", e)
    # gleiche Sorte (Hersteller/Material) gibt es, aber nicht in dieser Farbe → „Neue Sorte wie …“
    pm = _mat_norm(res["parsed"].get("material"))
    exact_color = any("Farbe" in (m.get("why") or "") for m in res["matches"])
    if pm and not exact_color and not res["learned"]:
        fils = spoolman.inventory()["filaments"]
        same = [f for f in fils if _mat_norm(f.get("material")) == pm and
                (not brand or brand.lower() in ((f.get("vendor") or {}).get("name") or "").lower())]
        if same:
            tpl = _template(fid=max(same, key=lambda f: f["id"])["id"])
            if tpl:
                res["template"] = tpl
    # Nummer (Charge) ist nur ein schwacher Hinweis: sie zählt nur, wenn Material und Farbe vom Etikett nicht widersprechen
    fils = {f["id"]: f for f in spoolman.inventory()["filaments"]}
    pt, pmn = res["parsed"], _mat_norm(res["parsed"].get("material"))
    tc = _tnorm(pt.get("color")).strip() if pt.get("color") else ""
    def agrees(fid):
        f = fils.get(fid)
        if not f:
            return False
        if pmn and _mat_norm(f.get("material")) != pmn:
            return False
        fn = _tnorm(f.get("name")).strip()
        if tc and fn != tc and not (pt.get("color_src") == "base" and tc in fn.split()):
            return False
        return True
    def label(f):
        return " ".join(x for x in [(f.get("vendor") or {}).get("name", ""), f.get("name") or "", f.get("material") or ""] if x)
    def boost(fid, pts, why):
        m = next((x for x in res["matches"] if x["id"] == fid), None)
        if m:
            m["score"] += pts; m["why"] = (m.get("why") + ", " if m.get("why") else "") + why
        elif fid in fils:
            f = fils[fid]
            res["matches"].append({"id": fid, "label": label(f), "color": "#" + (f.get("color_hex") or "999999")[:6], "score": pts, "why": why})
    ci = res.get("code_info") or {}
    if res["learned"]:
        if agrees(res["learned"]):
            boost(res["learned"], 4, "Charge bekannt")
        else:
            f = fils.get(res["learned"])
            ci.update(status="text", labels=[label(f)] if f else [])
            res["learned"] = None
    elif ci.get("status") in ("similar", "multi"):
        ok = [fid for fid in ci["fids"] if agrees(fid)]
        for fid in ok:
            boost(fid, 2, "ähnliche Charge" if ci["status"] == "similar" else "gleiche Charge")
        ci["labels"] = [label(fils[fid]) for fid in ok if fid in fils]
        if not ok:
            ci["status"] = "unknown"
    res["matches"].sort(key=lambda x: -x["score"])
    # genau diese Sorte schon im Lager? (gelernte Nummer, sonst Hersteller + Material + Name gleich)
    res["exact_fid"] = res.get("own_fid") or res["learned"]
    if not res["exact_fid"] and res["parsed"].get("color") and res["parsed"].get("color_src") != "base":
        pn, pmn, br = _tnorm(res["parsed"]["color"]).strip(), _mat_norm(res["parsed"].get("material")), brand.lower()
        for f in spoolman.inventory()["filaments"]:
            if (_tnorm(f.get("name")).strip() == pn and (not pmn or _mat_norm(f.get("material")) == pmn)
                    and (not br or ((f.get("vendor") or {}).get("name") or "").lower() == br)):
                res["exact_fid"] = f["id"]
                break
    if res.get("timing"):
        log.info("Scan: Foto %.1f MB, dekodiert %.1fs, Strichcode %.1fs, bis Text fertig %.1fs", res["timing"]["bytes"] / 1e6,
                 res["timing"]["decode"], res["timing"]["barcode"], res["timing"].get("total_ocr", 0))
    log.info("Scan: Codes %s, Text %s, Treffer %s", [c["data"] for c in res["codes"]], res["parsed"],
             [(m["id"], m["score"]) for m in res["matches"][:3]])
    return res


async def api_lager_scan(request):
    ct = request.headers.get("content-type", "")
    try:
        if ct.startswith("image/"):
            body = await request.body()
            if len(body) > 15_000_000:
                return JSONResponse({"error": "Foto zu groß"}, status_code=400)
            q = request.query_params
            res = await run_in_threadpool(_scan, body, q.get("code"), q.get("vendor", ""), q.get("material", ""))
        else:
            b = await request.json()
            if not (b.get("code") or "").strip():
                return JSONResponse({"error": "Kein Code"}, status_code=400)
            res = await run_in_threadpool(_scan, None, b["code"], b.get("vendor", ""), b.get("material", ""))
    except Exception as e:
        log.warning("Scan fehlgeschlagen: %s", e)
        return JSONResponse({"error": f"Foto konnte nicht gelesen werden: {e}"}, status_code=400)
    return JSONResponse(res)


# ---------------- Nochmal drucken ----------------
def _norm_name(n):
    n = re.sub(r"\.(gcode\.)?3mf$|\.gcode$", "", str(n or ""), flags=re.I)
    return re.sub(r"[^a-z0-9äöüß]+", "", n.lower())


def _bb_printer_id(name):
    p = next((x for x in printers.values() if x.name == name), None)
    try:
        for bp in bambuddy.printers():
            if (p is not None and getattr(p, "serial", None) and bp.get("serial_number") == p.serial) or bp.get("name") == name:
                return bp.get("id")
    except Exception:
        pass
    return None


def _reprint(hid, by="PrintHub"):
    """Druck aus dem Verlauf nochmal: passendes Bambuddy-Archiv suchen → Warteschlange (manuell, Druckwächter startet)."""
    if not bambuddy.enabled:
        raise ValueError("Bambuddy ist nicht eingerichtet")
    e = history.get(hid)
    if not e:
        raise LookupError("Druck nicht gefunden")
    want = _norm_name(e.get("job"))
    if not want:
        raise ValueError("Druck hat keinen Namen")
    cands = []
    for a in bambuddy.archives(300):
        names = {_norm_name(a.get("print_name")), _norm_name(a.get("filename")), _norm_name(a.get("subtask_name"))}
        if want in names or any(n and (want in n or n in want) and min(len(n), len(want)) >= 6 for n in names):
            t = _iso_ts(a.get("completed_at") or a.get("started_at") or a.get("created_at")) or 0
            cands.append((abs(t - (e.get("time") or 0)), a))
    if not cands:
        raise LookupError(f"„{e.get('job')}“ ist im Bambuddy-Archiv nicht zu finden – dort von Hand in die Warteschlange legen")
    cands.sort(key=lambda x: x[0])
    a = cands[0][1]
    item = bambuddy.enqueue(a["id"], _bb_printer_id(e.get("printer")) or a.get("printer_id"), manual=True)
    _qlog(item.get("id") or 0, e.get("job") or "", f"nochmal gedruckt aus dem Verlauf ({by})")
    log.info("Nochmal drucken: %s → Archiv %s, Auftrag %s (%s)", e.get("job"), a["id"], item.get("id"), by)
    hint = "" if nachtruhe.running() else " – Druckwächter läuft nicht, bitte in Bambuddy starten"
    return f"„{e.get('job')}“ liegt wieder in der Warteschlange ✓{hint}"


async def api_history_reprint(request):
    try:
        msg = await run_in_threadpool(_reprint, request.path_params["hid"])
    except (ValueError, LookupError, RuntimeError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "message": msg})


# ---------------- Trocknen & Feuchte ----------------
def _dry_limits():
    out = {}
    for part in str(settings.get("dry_days") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip().upper()] = float(v.strip().replace(",", "."))
            except ValueError:
                pass
    return out


def _iso_ts(v):
    if not v:
        return None
    try:
        from datetime import datetime as _d
        return _d.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _dry_info(sp, material):
    """Wie lange ist die Rolle offen (seit Öffnen bzw. letztem Trocknen)? → {days, limit, due, dried, opened}"""
    d = (store.data.get("dry") or {}).get(str(sp["id"])) or {}
    opened = d.get("opened") or _iso_ts(sp.get("first_used"))
    if not opened and not d.get("dried"):
        return None                                   # noch verschlossen
    ref = max(x for x in (opened, d.get("dried")) if x)
    lim = _dry_limits()
    mat = (material or "").upper()
    keys = sorted((k for k in lim if k != "*" and mat.startswith(k)), key=len, reverse=True)   # PETG-HF → PETG, PLA+ → PLA
    limit = lim[keys[0]] if keys else lim.get("*", 30)
    days = (time.time() - ref) / 86400
    return {"days": round(days, 1), "limit": limit, "due": bool(limit) and days >= limit,
            "dried": d.get("dried"), "opened": opened}


def _dry_set(sid, what):
    with store.lock:
        d = store.data.setdefault("dry", {}).setdefault(str(int(sid)), {})
        d[what] = int(time.time())
        d.pop("notified", None)
        store._save()


async def api_lager_dried(request):
    sid = int(request.path_params["sid"])
    what = "opened" if request.query_params.get("what") == "opened" else "dried"
    _dry_set(sid, what)
    log.info("Lager: Spule #%s %s", sid, "geöffnet" if what == "opened" else "getrocknet")
    return JSONResponse({"ok": True})


def _watch_dry():
    """Spulen im Drucker, die zu lange offen sind: einmal melden, bis getrocknet."""
    if "dry" not in notifier.events and "dry" not in (settings.get("tg_events") or []):
        return
    in_slot = _slot_names()
    if not in_slot:
        return
    inv = spoolman.inventory()
    fil = {f["id"]: f for f in inv["filaments"]}
    msgs, knoepfe = [], []
    for sp in inv["spools"]:
        if sp["id"] not in in_slot:
            continue
        f = fil.get((sp.get("filament") or {}).get("id")) or sp.get("filament") or {}
        info = _dry_info(sp, f.get("material"))
        d = (store.data.get("dry") or {}).get(str(sp["id"])) or {}
        if info and info["due"] and not d.get("notified"):
            name = " ".join(x for x in [(f.get("vendor") or {}).get("name", ""), f.get("name") or "", f.get("material") or ""] if x)
            msgs.append(f"{name} (#{sp['id']}, {in_slot[sp['id']]}): seit {int(info['days'])} Tagen offen"
                        f"{' bzw. seit dem letzten Trocknen' if info['dried'] else ''} (Grenze {int(info['limit'])} Tage)")
            knoepfe.append([(f"🔥 #{sp['id']} getrocknet", f"ph:dry:{sp['id']}")])
            with store.lock:
                store.data.setdefault("dry", {}).setdefault(str(sp["id"]), {})["notified"] = int(time.time())
                store._save()
    if msgs:
        notifier.send("dry", "Trocknen empfohlen 💧", "\n".join(msgs) +
                      "\nFeuchtes Filament: Fäden, Blasen, raue Oberfläche. Nach dem Trocknen auf „getrocknet“ tippen.",
                      "droplet", buttons=knoepfe + [[("Später", "noop")]])


async def api_lager(request):
    try:
        inv = await run_in_threadpool(spoolman.inventory, request.query_params.get("archived") == "1")
    except Exception as e:
        return JSONResponse({"error": f"Spoolman nicht erreichbar: {e}"}, status_code=502)
    mins = store.data.get("min_stock", {})
    in_slot = _slot_names()
    by_fil, arch_fil = {}, {}
    for sp in inv["spools"]:
        fid = (sp.get("filament") or {}).get("id")
        used = sp.get("used_weight") or 0
        target = arch_fil if sp.get("archived") else by_fil
        target.setdefault(fid, []).append({
            "id": sp["id"], "remaining": sp.get("remaining_weight"), "initial": sp.get("initial_weight"),
            "used": used, "opened": used > 0.5 or bool(sp.get("first_used")),
            "location": in_slot.get(sp["id"]) or sp.get("location") or "", "in_printer": sp["id"] in in_slot,
            "last_used": sp.get("last_used"), "price": sp.get("price"),
            "spool_weight": sp.get("spool_weight"), "empty": spoolman.empty_weight(sp),
            "comment": sp.get("comment") or "", "lot": sp.get("lot_nr") or "",
            "dry": _dry_info(sp, (sp.get("filament") or {}).get("material")),
        })
    fils = []
    for f in inv["filaments"]:
        sp = sorted(by_fil.get(f["id"], []), key=lambda x: (not x["in_printer"], not x["opened"], x["id"]))
        color = f.get("color_hex") or (f.get("multi_color_hexes") or "").split(",")[0] or ""
        fils.append({
            "id": f["id"], "name": f.get("name") or "", "material": f.get("material") or "",
            "vendor": (f.get("vendor") or {}).get("name", ""), "vendor_id": (f.get("vendor") or {}).get("id"),
            "color": ("#" + color.lstrip("#")) if color else "", "weight": f.get("weight"),
            "spool_weight": f.get("spool_weight"), "price": f.get("price"), "density": f.get("density"),
            "empty": f.get("spool_weight") or (f.get("vendor") or {}).get("empty_spool_weight"),
            "diameter": f.get("diameter"), "extruder_temp": f.get("settings_extruder_temp"),
            "bed_temp": f.get("settings_bed_temp"), "spools": sp, "min": int(mins.get(str(f["id"]), 0)),
            "full": sum(1 for x in sp if not x["opened"]), "remaining": round(sum(x["remaining"] or 0 for x in sp)),
            "archived": sorted(arch_fil.get(f["id"], []), key=lambda x: -(x["id"])),
        })
    fils.sort(key=lambda f: (f["vendor"].lower(), f["material"], f["name"].lower()))
    return JSONResponse({"filaments": fils, "vendors": [{"id": v["id"], "name": v["name"],
                         "empty_spool_weight": v.get("empty_spool_weight")} for v in inv["vendors"]],
                         "spoolman_url": cfg.get("spoolman_url"), "storage": settings["storage_location"]})


def _num(v):
    if v in (None, ""):
        return None
    return float(str(v).replace(",", "."))


async def api_lager_manufacturers(request):
    try:
        return JSONResponse(await run_in_threadpool(spoolman.external_manufacturers))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def api_lager_external(request):
    qp = request.query_params
    q, man, mat = qp.get("q", "").strip(), qp.get("manufacturer", "").strip(), qp.get("material", "").strip()
    if len(q) < 2 and not man:
        return JSONResponse({"items": [], "total": 0, "materials": []})
    try:
        res, total, mats = await run_in_threadpool(spoolman.search_external, q, man, mat)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    out = []
    for f in res:
        color = f.get("color_hex") or ((f.get("color_hexes") or [None])[0]) or ""
        out.append({
            "external_id": f.get("id"), "manufacturer": f.get("manufacturer"), "name": f.get("name"),
            "material": f.get("material"), "color": ("#" + color.lstrip("#")) if color else "",
            "weight": f.get("weight"), "spool_weight": f.get("spool_weight"), "density": f.get("density"),
            "diameter": f.get("diameter"), "extruder_temp": f.get("extruder_temp"), "bed_temp": f.get("bed_temp"),
            "finish": f.get("finish"), "multi": bool(f.get("color_hexes")),
        })
    return JSONResponse({"items": out, "total": total, "materials": mats})


async def api_lager_spools(request):
    b = await request.json()
    try:
        count = int(b.get("count") or 1)
        if not 1 <= count <= 50:
            raise ValueError("Anzahl 1–50")
        initial = _num(b.get("initial_weight"))
        gross, empty = _num(b.get("gross")), _num(b.get("empty"))
        if gross:
            if not empty:
                raise ValueError("Für „brutto gewogen“ wird das Leergewicht der Spule gebraucht")
            initial = round(gross - empty, 1)
            if initial <= 0:
                raise ValueError("Brutto ist kleiner als das Leergewicht")
        if empty and b.get("empty_for_filament"):
            await run_in_threadpool(spoolman.update_filament, int(b["filament_id"]), {"spool_weight": empty})
        ids = await run_in_threadpool(spoolman.create_spools, b["filament_id"], count,
                                      b.get("location") or settings["storage_location"],
                                      initial, _num(b.get("price")), b.get("lot") or None)
        for c in (b.get("ean"), b.get("lot")):
            if c:
                _learn_code(c, int(b["filament_id"]))
        if empty and not b.get("empty_for_filament"):
            for i in ids:
                await run_in_threadpool(spoolman.update_spool, i, {"spool_weight": empty})
    except (ValueError, KeyError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    log.info("Lager: %d neue Spule(n) angelegt: %s", len(ids), ids)
    return JSONResponse({"ok": True, "ids": ids})


def _fil_data(b, vendor_id=None):
    name, material = (b.get("name") or "").strip(), (b.get("material") or "").strip().upper()
    if not name or not material:
        raise ValueError("Name und Material sind Pflicht")
    data = {"name": name, "material": material, "vendor_id": int(vendor_id) if vendor_id else None,
            "density": _num(b.get("density")) or {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21}.get(
                material.split("+")[0].split("-")[0], 1.24),
            "diameter": _num(b.get("diameter")) or 1.75,
            "weight": _num(b.get("weight")), "spool_weight": _num(b.get("spool_weight")),
            "price": _num(b.get("price")), "color_hex": (b.get("color") or "").lstrip("#").upper() or None,
            "settings_extruder_temp": int(_num(b["extruder_temp"])) if _num(b.get("extruder_temp")) else None,
            "settings_bed_temp": int(_num(b["bed_temp"])) if _num(b.get("bed_temp")) else None,
            "external_id": b.get("external_id") or None}
    return {k: v for k, v in data.items() if v is not None}


async def api_lager_filament(request):
    b = await request.json()
    try:
        vendor_id = b.get("vendor_id")
        if not vendor_id and (b.get("vendor_name") or "").strip():
            v = await run_in_threadpool(spoolman.create_vendor, b["vendor_name"].strip(), _num(b.get("vendor_spool_weight")))
            vendor_id = v["id"]
        data = _fil_data(b, vendor_id)
        if b.get("id"):
            data.pop("external_id", None)
        if b.get("id"):
            f = await run_in_threadpool(spoolman.update_filament, int(b["id"]), data)
        else:
            f = await run_in_threadpool(spoolman.create_filament, data)
        if b.get("count"):
            await run_in_threadpool(spoolman.create_spools, f["id"], int(b["count"]), settings["storage_location"], None, None,
                                    b.get("lot") or None)
        for c in (b.get("ean"), b.get("lot")):
            if c:
                _learn_code(c, f["id"])
    except (ValueError, KeyError, RuntimeError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "id": f["id"]})


def _weigh(sid, b):
    raw = spoolman.spool_raw(sid)
    gross, empty = _num(b.get("gross")), _num(b.get("empty")) or spoolman.empty_weight(raw)
    if not gross:
        raise ValueError("Bruttogewicht (Waage) fehlt")
    if not empty:
        raise ValueError("Leergewicht der Spule fehlt – leere Spule einmal wiegen")
    rest = round(max(0.0, gross - empty), 1)
    initial = raw.get("initial_weight") or (raw.get("filament") or {}).get("weight") or 1000
    if rest > initial:
        # mehr drauf als das Nenngewicht (z. B. neue Rolle mit 1012 g) → Nenngewicht anpassen
        initial = rest
        spoolman.update_spool(sid, {"initial_weight": initial})
    if b.get("for_filament") and (raw.get("filament") or {}).get("id"):
        spoolman.update_filament(raw["filament"]["id"], {"spool_weight": empty})
    vend = ((raw.get("filament") or {}).get("vendor") or {})
    if b.get("for_vendor") and vend.get("id"):
        spoolman.update_vendor(vend["id"], {"empty_spool_weight": empty})
        log.info("Lager: Leergewicht %s g gilt jetzt für alle Rollen von %s", empty, vend.get("name"))
    spoolman.update_spool(sid, {"remaining_weight": rest, "spool_weight": empty})
    log.info("Lager: Spule #%s gewogen – brutto %s g, leer %s g → %s g Filament", sid, gross, empty, rest)
    return {"ok": True, "remaining": rest, "initial": initial}


async def api_lager_weigh(request):
    sid = int(request.path_params["sid"])
    try:
        res = await run_in_threadpool(_weigh, sid, await request.json())
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(res)


async def api_lager_spool_edit(request):
    sid = int(request.path_params["sid"])
    b = await request.json()
    data = {}
    for k, key in (("initial", "initial_weight"), ("empty", "spool_weight"), ("price", "price")):
        if k in b:
            data[key] = _num(b.get(k))
    for k, key in (("comment", "comment"), ("lot", "lot_nr")):
        if k in b:
            data[key] = (b.get(k) or "").strip() or None
    if "location" in b and (b.get("location") or "").strip() and sid not in _slot_names():
        data["location"] = b["location"].strip()
    try:
        if data:
            await run_in_threadpool(spoolman.update_spool, sid, data)
        if b.get("remaining") not in (None, ""):
            await run_in_threadpool(spoolman.update_spool, sid, {"remaining_weight": max(0.0, _num(b["remaining"]) or 0.0)})
        if (b.get("lot") or "").strip():
            raw = await run_in_threadpool(spoolman.spool_raw, sid)
            _learn_code(b["lot"], (raw.get("filament") or {}).get("id"))
        if b.get("for_filament") and _num(b.get("empty")):
            raw = await run_in_threadpool(spoolman.spool_raw, sid)
            await run_in_threadpool(spoolman.update_filament, raw["filament"]["id"], {"spool_weight": _num(b["empty"])})
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_lager_unarchive(request):
    sid = int(request.path_params["sid"])
    try:
        await run_in_threadpool(spoolman.archive_spool, sid, False)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_lager_archive(request):
    sid = int(request.path_params["sid"])
    if sid in _slot_names():
        if request.query_params.get("force") != "1":
            return JSONResponse({"error": "Spule steckt noch in einem Drucker – erst in PrintHub austragen"}, status_code=400)
        _release_spool(sid)
    try:
        await run_in_threadpool(spoolman.archive_spool, sid)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_lager_delete(request):
    """Löscht Rollen endgültig (z. B. Fehleingaben). Rollen im Drucker werden übersprungen."""
    b = await request.json()
    in_printer = _slot_names()
    done, skipped = [], []
    for sid in [int(x) for x in (b.get("ids") or [])]:
        if sid in in_printer:
            if not b.get("force"):
                skipped.append(sid)
                continue
            _release_spool(sid)
        try:
            await run_in_threadpool(spoolman.delete_spool, sid)
            done.append(sid)
        except RuntimeError as e:
            return JSONResponse({"error": str(e), "deleted": done}, status_code=400)
    if done:
        log.info("Lager: Spule(n) gelöscht: %s", done)
    return JSONResponse({"ok": True, "deleted": done, "skipped": skipped})


async def api_lager_min(request):
    b = await request.json()
    with store.lock:
        store.data.setdefault("min_stock", {})[str(int(b["filament_id"]))] = max(0, int(b.get("min") or 0))
        store._save()
    return JSONResponse({"ok": True})


async def assign_page(request):
    return page("assign.html")


async def api_state(request):
    spools, sp_err = _spool_map()
    out = []
    for p in printers.values():
        st = p.status()
        slots = []
        for s in p.slots():
            sid = store.get_slot(p.id, s["key"])
            slots.append({**s, "spool_id": sid, "spool": spools.get(sid) if sid else None})
        st["slots"] = slots
        st["track_usage"] = p.track_usage
        st["camera"] = p.camera is not None
        st["energy"] = p.energy_status()
        job = store.get_job(p.id)
        st["thumb"] = f"/media/{job['thumb']}" if job and job.get("thumb") else None
        st["short"] = job.get("short") if job else None
        st["shelly"] = bool(p.shelly)
        st["power"] = power.info(p)
        st["maint_due"] = maint_due.get(p.id, 0)
        out.append(st)
    n = int(settings["history_on_dashboard"])
    return JSONResponse({"printers": out, "history": history.recent(n) if n else [], "spoolman_error": sp_err,
                         "notify": notifier.enabled, "theme": settings["theme"],
                         "queue": settings["queue_on_dashboard"] and bambuddy.enabled})


MANIFEST = {
    "name": "PrintHub", "short_name": "PrintHub", "description": "Cockpit für 3D-Drucker, Spulen und Dienste",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "any",
    "background_color": "#0a1220", "theme_color": "#0a1220", "lang": "de",
    "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/static/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    "shortcuts": [{"name": "Lager", "url": "/lager"}, {"name": "Verlauf", "url": "/verlauf"}, {"name": "Statistik", "url": "/statistik"}],
}


async def manifest(request):
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


async def service_worker(request):
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


async def api_progress(request):
    """Kurzer Druckstand für Titel und Symbol im Browser-Tab (alle Seiten)."""
    out = []
    for p in printers.values():
        try:
            st = p.status()
        except Exception:
            continue
        if st.get("printing"):
            out.append({"name": p.name, "progress": st.get("progress") or 0, "remaining_min": st.get("remaining_min"),
                        "paused": "pause" in str(st.get("state", "")).lower(),
                        "stage": (st.get("stage") or {}).get("icon", "") + " " + (st.get("stage") or {}).get("short", "")
                        if st.get("stage") else ""})
    return JSONResponse({"enabled": settings["tab_progress"], "printing": out})


async def api_spools(request):
    try:
        spools = spoolman.spools(max_age=0 if request.query_params.get("fresh") else 10)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    for s in spools:
        key = store.slot_of_spool(s["id"])
        s["slot"] = None
        if key:
            pid, slot = key.split(":", 1)
            if pid in printers:
                s["slot"] = printers[pid].location_name(slot)
    return JSONResponse(spools)


async def api_assign(request):
    body = await request.json()
    pid, slot = body.get("printer"), body.get("slot")
    spool_id = body.get("spool_id")
    spool_id = int(spool_id) if spool_id not in (None, "", 0) else None
    p = printers.get(pid)
    if not p or slot not in {s["key"] for s in p.slots()}:
        return JSONResponse({"error": "Drucker oder Slot unbekannt"}, status_code=400)
    old, _moved = store.set_slot(pid, slot, spool_id)
    warn = ""
    try:
        if old and old != spool_id:
            spoolman.set_location(old, settings["storage_location"])
        if spool_id:
            spoolman.set_location(spool_id, p.location_name(slot))
    except Exception as e:
        warn = f"Zugewiesen, aber Standort in Spoolman nicht gesetzt: {e}"
    # Material + Farbe auch am Drucker einstellen (Bambu), damit Bambuddy/Bambu Studio es sehen
    if spool_id and hasattr(p, "set_slot_filament") and settings["sync_slots"]:
        try:
            p.set_slot_filament(slot, spoolman.spool(spool_id))
        except Exception as e:
            warn = (warn + " " if warn else "") + f"Am Drucker nicht gesetzt: {e}"
    return JSONResponse({"ok": True, "warning": warn})


async def api_control(request):
    p = printers.get(request.path_params["pid"])
    if not p:
        return JSONResponse({"error": "Drucker unbekannt"}, status_code=404)
    try:
        p.control(request.path_params["action"])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


def _sampler():
    """Hintergrund: alle 5 s Temperaturen, jede Minute Strom und Wartung."""
    import threading
    tick = 0
    while True:
        for p in printers.values():
            try:
                job = store.get_job(p.id)
                if p.online:
                    temps.add(p.id, p.status().get("temps", []), job.get("started") if job else None)
                if tick % 12 == 0:
                    if p.shelly and p.shelly.online:
                        energy.sample(p.id, p.shelly.total_wh)
                    _, h = _hours_total(p)
                    maint.check(p, h)
                    ms = maint.printer_status(p, h)
                    maint_due[p.id] = ms["due"]
                _watch_printer(p, job)
            except Exception as e:
                log.debug("Messwerte %s: %s", p.id, e)
        if tick % 12 == 6:
            try:
                _watch_services()
            except Exception as e:
                log.debug("Dienste-Überwachung: %s", e)
        if tick % 120 == 30:
            try:
                _watch_stock()
            except Exception as e:
                log.debug("Lager-Überwachung: %s", e)
        if tick % 720 == 90:            # stündlich
            try:
                _watch_dry()
            except Exception as e:
                log.debug("Trocknen-Überwachung: %s", e)
        tick += 1
        time.sleep(5)


def _watch_printer(p, job):
    """Bett abgekühlt nach dem Druck, Drucker während eines Drucks offline."""
    if p.cool_watch:
        bed = next((t for t in p.status().get("temps", []) if t.get("label") == "Bett"), None)
        cur = bed and bed.get("actual")
        if store.get_job(p.id) or time.time() - p.cool_watch > 3 * 3600:
            p.cool_watch = None
        elif cur is not None and cur < float(settings["bed_cool_c"]):
            p.cool_watch = None
            p.notify("bed_cooled", "Bett abgekühlt ❄", f"Unter {settings['bed_cool_c']} °C – das Teil kann runter.", "snowflake", "low")
    if job and not p.online:
        p.offline_since = p.offline_since or time.time()
        if time.time() - p.offline_since > 90 and not job.get("offline_sent"):
            job["offline_sent"] = True
            store.set_job(p.id, job)
            p.notify("offline", "Drucker offline ⚠", f"Während „{job.get('name', '')}“ keine Verbindung mehr (Strom? WLAN?).", "warning", "high")
    elif p.online:
        p.offline_since = None


_svc_down = {}


def _watch_services():
    """Spoolman, Bambuddy, OctoEverywhere: Ausfall länger als service_alarm_min melden, Rückkehr auch."""
    limit = int(settings["service_alarm_min"]) * 60
    checks = {"Spoolman": lambda: spoolman.s.get(f"{spoolman.url}/api/v1/info", timeout=5).raise_for_status()}
    if bambuddy.enabled:
        checks["Bambuddy"] = bambuddy.version
    if os.path.exists(dockerctl.SOCK) and settings["oe_container"]:
        def oe():
            for n in _container_names("octoeverywhere"):
                if not dockerctl.info(n)["running"]:
                    raise RuntimeError("Container läuft nicht")
        checks["OctoEverywhere"] = oe
    for name, fn in checks.items():
        try:
            fn()
            st = _svc_down.pop(name, None)
            if st and st.get("sent"):
                notifier.send("service", f"{name} wieder erreichbar ✅", f"Nach {int((time.time() - st['since']) / 60)} min wieder da.", "white_check_mark", "low")
        except Exception as e:
            st = _svc_down.setdefault(name, {"since": time.time(), "sent": False})
            if not st["sent"] and time.time() - st["since"] >= limit:
                st["sent"] = True
                notifier.send("service", f"{name} nicht erreichbar ⚠", f"Seit {int((time.time() - st['since']) / 60)} min keine Verbindung ({e.__class__.__name__}).", "warning", "high")
    _watch_queue()


_queue_seen = {"ids": None, "busy": False}


def _watch_queue():
    """Bambuddy-Warteschlange: neue Aufträge und „alles abgearbeitet“."""
    want = {"queue_add", "queue_done", "queue_check"}
    if not bambuddy.enabled or not (want & (notifier.events | set(settings.get("tg_events") or []))):
        return
    items = bambuddy.queue()
    ids = {q["id"] for q in items}
    if _queue_seen["ids"] is not None:
        for q in items:
            if q["id"] not in _queue_seen["ids"] and q["status"] == "pending":
                notifier.send("queue_add", "Neuer Auftrag in der Warteschlange 📥",
                              f"{q['name']} · {q['printer']}" + (f" · {PH_dur(q['seconds'])}" if q.get("seconds") else ""), "inbox_tray", "low")
                try:
                    c = qcheck.check(q, _printer_for_item(q))
                except Exception:
                    c = None
                if c and c["state"] in ("bad", "warn"):
                    probs = [l["msg"] for l in c["lines"] if l["state"] != "ok" and l["msg"]]
                    notifier.send("queue_check", ("Auftrag: Filament reicht nicht/passt nicht ⚠️" if c["state"] == "bad" else "Auftrag: bitte Filament prüfen"),
                                  f"{q['name']}\n" + "\n".join("• " + x for x in probs), "warning")
        if _queue_seen["busy"] and not items:
            notifier.send("queue_done", "Warteschlange abgearbeitet 🏁", "Alle Aufträge sind durch.", "checkered_flag", "low")
    _queue_seen.update(ids=ids, busy=bool(items))


def PH_dur(sec):
    m = int(sec // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _watch_stock():
    """Lagerbestand: Filamente unter dem Mindestbestand (volle Rollen) einmal melden, bis wieder aufgefüllt."""
    if "stock_low" not in notifier.events:
        return
    mins = {k: v for k, v in store.data.get("min_stock", {}).items() if v}
    if not mins:
        return
    inv = spoolman.inventory()
    full = {}
    for sp in inv["spools"]:
        fid = str((sp.get("filament") or {}).get("id"))
        if not ((sp.get("used_weight") or 0) > 0.5 or sp.get("first_used")):
            full[fid] = full.get(fid, 0) + 1
    names = {str(f["id"]): " ".join(x for x in [(f.get("vendor") or {}).get("name", ""), f.get("name") or "", f.get("material") or ""] if x) for f in inv["filaments"]}
    sent = store.data.setdefault("stock_sent", {})
    low = []
    for fid, mn in mins.items():
        if full.get(fid, 0) < mn:
            if not sent.get(fid):
                low.append(f"{names.get(fid, fid)}: {full.get(fid, 0)} von {mn} vollen Rollen")
                sent[fid] = True
        else:
            sent.pop(fid, None)
    store._save()
    if low:
        notifier.send("stock_low", "Nachkaufen 🛒", "\n".join(low), "shopping_cart")


async def api_temps(request):
    pid = request.path_params["pid"]
    if pid not in printers:
        return JSONResponse({"error": "Drucker unbekannt"}, status_code=404)
    return JSONResponse(temps.get(pid, request.query_params.get("range", "15")))


async def api_energy(request):
    pid = request.path_params["pid"]
    p = printers.get(pid)
    if not p:
        return JSONResponse({"error": "Drucker unbekannt"}, status_code=404)
    rng = request.query_params.get("range", "day")
    data = await run_in_threadpool(energy.series, pid, rng)
    price = float(settings.get("price_kwh") or 0)
    total = sum(d["kwh"] for d in data)
    return JSONResponse({"bars": data, "kwh": round(total, 3), "cost": round(total * price, 2), "price": price,
                         "shelly": bool(p.shelly)})


def _history_hours(p):
    st = printer_stats(HISTORY_DB, p.name)
    job = store.get_job(p.id)
    running = (time.time() - job["started"]) / 3600 if job and job.get("started") else 0
    return st, st["hours"] + running


def _hours_total(p):
    """Druckstunden gesamt = Startwert (z. B. aus Bambuddy) + Verlauf in PrintHub + laufender Druck."""
    st, h = _history_hours(p)
    return st, h + maint.offset(p.id)


async def api_stats(request):
    out = {}
    for p in printers.values():
        st, hours = await run_in_threadpool(_hours_total, p)
        heads = store.data.get("heads", {}).get(p.id, {})
        st["hours_total"] = round(hours, 1)
        ms = maint.printer_status(p, hours)
        st["maintenance"] = {"items": ms["items"], "due": ms["due"], "warn": ms["warn"]}
        st["heads"] = {k: {"hours": round(v.get("sec", 0) / 3600, 2), "changes": v.get("changes", 0)} for k, v in heads.items()}
        out[p.id] = st
    return JSONResponse(out)


def _printer_or_404(pid):
    p = printers.get(pid)
    if not p:
        raise LookupError("Drucker unbekannt")
    return p


async def api_maintenance(request):
    """GET: alles für die Wartungsseite."""
    out = []
    for p in printers.values():
        _, hours = await run_in_threadpool(_hours_total, p)
        out.append(maint.printer_status(p, hours))
    return JSONResponse({"types": maint.d["types"], "printers": out, "log": maint.log(40),
                         "bambuddy": bambuddy.enabled})


async def api_maintenance_item(request):
    """POST …/done = erledigt, PUT = Intervall/aktiv für diesen Drucker."""
    try:
        p = _printer_or_404(request.path_params["pid"])
        tid = request.path_params["tid"]
        b = {}
        try:
            b = await request.json()
        except Exception:
            pass
        _, h = await run_in_threadpool(_hours_total, p)
        if request.method == "PUT":
            maint.set_item(p, tid, b)
        else:
            maint.done(p, tid, h, b.get("note", ""))
            log.info("[%s] Wartung erledigt: %s (%.1f h)", p.id, tid, h)
        return JSONResponse({"ok": True, "printer": maint.printer_status(p, h)})
    except (LookupError, ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_maintenance_hours(request):
    try:
        p = _printer_or_404(request.path_params["pid"])
        b = await request.json()
        total = float(str(b.get("hours")).replace(",", "."))
        _, hh = await run_in_threadpool(_history_hours, p)
        maint.set_total_hours(p.id, total, hh)
        return JSONResponse({"ok": True})
    except (LookupError, ValueError, TypeError) as e:
        return JSONResponse({"error": str(e) if not isinstance(e, (ValueError, TypeError)) else "Ungültige Stundenzahl"},
                            status_code=400)


async def api_maintenance_types(request):
    tid = request.path_params.get("tid")
    try:
        if request.method == "DELETE":
            maint.delete_type(tid)
        elif tid == "restore":
            maint.restore_defaults()
        else:
            maint.save_type(await request.json(), tid if request.method == "PUT" else None)
    except (LookupError, ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "types": maint.d["types"]})





# ---------- Dienste-Cockpit: Spoolman, Bambuddy, OctoEverywhere ----------
SERVICES = {
    "spoolman": {"name": "Spoolman", "icon": "🧵", "container": "spoolman_container"},
    "bambuddy": {"name": "Bambuddy", "icon": "🖨️", "container": "bambuddy_container"},
    "octoeverywhere": {"name": "OctoEverywhere", "icon": "🌐", "container": "oe_container"},
    "nachtruhe": {"name": "Druckwächter", "icon": "🛡️", "container": None},
}


def _container_names(svc):
    if not SERVICES[svc]["container"]:
        return []
    raw = settings[SERVICES[svc]["container"]] or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


def _docker_state(svc):
    if not os.path.exists(dockerctl.SOCK):
        return None
    out = []
    for n in _container_names(svc):
        try:
            out.append(dockerctl.info(n))
        except Exception as e:
            out.append({"name": n, "error": str(e)})
    return out


def _service_status(svc):
    d = {"id": svc, **{k: v for k, v in SERVICES[svc].items() if k != "container"},
         "containers": _docker_state(svc), "online": None, "lines": [], "url": None}
    try:
        if svc == "spoolman":
            d["url"] = spoolman.url
            t0 = time.time()
            r = spoolman.s.get(f"{spoolman.url}/api/v1/info", timeout=4)
            r.raise_for_status()
            d["version"] = r.json().get("version")
            d["ms"] = round((time.time() - t0) * 1000)
            sp = spoolman.spools(max_age=30)
            low = [x for x in sp if (x.get("remaining") or 0) < float(settings["low_spool_g"])]
            rest = sum(x.get("remaining") or 0 for x in sp)
            d["lines"] = [f"{len(sp)} Spulen · {rest / 1000:.1f} kg".replace(".", ","),
                          f"{len(low)} fast leer" if low else "keine Spule fast leer"]
            d["warn"] = bool(low)
            d["online"] = True
        elif svc == "bambuddy":
            if not bambuddy.enabled:
                d["online"] = None
                d["lines"] = ["Adresse fehlt – Einstellungen → Dienste"]
            else:
                d["url"] = bambuddy.url
                d["version"] = bambuddy.version()
                d["online"] = True
                try:
                    q = bambuddy.queue()
                    pend = [x for x in q if x["status"] == "pending"]
                    d["lines"] = [f"{len(pend)} in der Warteschlange" if pend else "Warteschlange leer"]
                    if any(x["status"] == "printing" for x in q):
                        d["lines"].append("druckt gerade")
                except PermissionError as e:
                    d["lines"] = [str(e)]
                    d["warn"] = True
        elif svc == "nachtruhe":
            d["containers"] = None
            d["url"] = "/einstellungen#waechter"
            d["internal"] = True
            from .nachtruhe import read_env, DAYS
            v = read_env(nachtruhe.config)
            day = DAYS[time.localtime().tm_wday]
            we = day in ("SA", "SO")
            ab = v.get(f"DRUCK_AB_{day}") or (v.get("DRUCK_AB_WE") if we else None) or v.get("DRUCK_AB", "07:00")
            bis = v.get(f"DRUCK_BIS_{day}") or (v.get("DRUCK_BIS_WE") if we else None) or v.get("DRUCK_BIS", "22:30")
            if not nachtruhe.enabled:
                d["online"] = None
                d["lines"] = ["aus – Einstellungen → Druckwächter"]
            else:
                d["online"] = nachtruhe.running()
                d["lines"] = ["heute kein Druck" if "aus" in (ab.lower(), bis.lower()) else f"Druckfenster heute {ab}–{bis}"]
                if v.get("DRY_RUN", "1") == "1":
                    d["lines"].append("Testmodus (schaltet nichts)")
                    d["warn"] = True
                    d["warn_line"] = 1
                if not d["online"]:
                    d["error"] = "startet nicht – Log ansehen"
        elif svc == "octoeverywhere":
            d["url"] = settings["oe_url"]
            c = d["containers"] or []
            if c and not any(x.get("error") for x in c):
                d["online"] = all(x["running"] for x in c)
            d["lines"] = ["Neustart bei Druckstart: " + (f"an ({settings['oe_delay']} s)" if settings["oe_restart"] else "aus")]
    except Exception as e:
        d["online"] = False
        d["error"] = f"nicht erreichbar ({e.__class__.__name__})"
    return d


async def api_services(request):
    import asyncio
    res = await asyncio.gather(*[run_in_threadpool(_service_status, k) for k in SERVICES])
    return JSONResponse({"services": res, "docker": os.path.exists(dockerctl.SOCK)})


async def api_service_restart(request):
    svc = request.path_params["svc"]
    if svc not in SERVICES:
        return JSONResponse({"error": "Unbekannter Dienst"}, status_code=404)
    if svc == "nachtruhe":
        await run_in_threadpool(nachtruhe.restart)
        return JSONResponse({"ok": True, "containers": ["Nachtruhe"]})
    names = _container_names(svc)
    if not names:
        return JSONResponse({"error": "Kein Container eingetragen (Einstellungen → Dienste)"}, status_code=400)
    done = []
    try:
        for n in names:
            st = await run_in_threadpool(dockerctl.info, n)
            fn = dockerctl.restart if st["running"] else dockerctl.start
            done.append(await run_in_threadpool(fn, n))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    log.info("Dienst %s neu gestartet: %s", svc, ", ".join(done))
    return JSONResponse({"ok": True, "containers": done})


_bbp = {"t": 0, "map": {}}


def _printer_for_item(q):
    """Bambuddy-Auftrag → PrintHub-Drucker (über Seriennummer/Name)."""
    if time.time() - _bbp["t"] > 600:
        _bbp["t"] = time.time()
        try:
            _bbp["map"] = {bp["id"]: bp for bp in bambuddy.printers()}
        except Exception:
            pass
    bp = _bbp["map"].get(q.get("printer_id")) or {}
    for p in printers.values():
        if (getattr(p, "serial", None) and bp.get("serial_number") == p.serial) or (bp.get("name") and bp.get("name") == p.name):
            return p
    bambus = [p for p in printers.values() if p.type == "bambu"]
    return bambus[0] if len(bambus) == 1 else None


def _queue_checked():
    items = bambuddy.queue()
    for q in items:
        if q.get("status") == "pending":
            try:
                q["check"] = qcheck.check(q, _printer_for_item(q))
            except Exception as e:
                q["check"] = {"state": "unknown", "lines": [], "text": f"Prüfung nicht möglich: {e}"}
    return items


# ---------------- Zeitplan (Startzeit planen + Vorschau) ----------------

def _plans():
    try:
        with open(PLANS, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def _plans_save(d):
    os.makedirs(os.path.dirname(PLANS), exist_ok=True)
    tmp = PLANS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in d.items()}, f)
    os.replace(tmp, PLANS)


def _wcfg():
    """Druckfenster-Einstellungen des Druckwächters + Zeitzone."""
    try:
        env = read_env(nachtruhe.config)
    except Exception:
        env = {}
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(env.get("TZ_NAME") or os.environ.get("TZ") or "Europe/Rome")
    except Exception:
        tz = None
    return sched.Window(env), tz


def _local_now(tz):
    return _dt.now(tz).replace(tzinfo=None) if tz else _dt.now()


def _to_local(v, tz):
    """Bambuddy-UTC → naive Ortszeit."""
    if not v:
        return None
    try:
        d = _dt.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz.utc)
        return d.astimezone(tz).replace(tzinfo=None) if tz else d.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _fenster_text(win, d):
    z = win.zeiten(d)
    return f"{z[0]}–{z[1]}" if z else "an dem Tag kein Druck"


def _queue_plan(items):
    """Vorschau an die Aufträge hängen (q['plan']) – Rückgabe: Zusammenfassung."""
    win, tz = _wcfg()
    now = _local_now(tz)
    pl = _plans()
    ids = {q["id"] for q in items if q.get("status") == "pending"}
    if any(k not in ids for k in pl):                  # erledigte/gelöschte Pläne aufräumen
        _plans_save({k: v for k, v in pl.items() if k in ids})
    forced = {k for k, v in pl.items() if v.get("force") and k in ids}
    left, druckt = None, None
    for q in items:
        if q.get("status") == "printing":
            druckt = q
            p = _printer_for_item(q)
            try:
                rem = p.status().get("remaining_min") if p else None
            except Exception:
                rem = None
            left = (rem * 60) if rem else (q.get("seconds") or 0) // 2
    pend, lokal = [], {}
    for q in items:
        if q.get("status") == "pending":
            lokal[q["id"]] = _to_local(q.get("scheduled"), tz)
            q["forced"] = q["id"] in forced
            pend.append({**q, "scheduled": lokal[q["id"]]})
    if not pend:
        out, ende = {}, None
    else:
        out, ende = sched.plan(pend, win, now, left, forced)
    for q in items:
        if q.get("status") == "pending":
            q["plan"] = out.get(q["id"])
            d = lokal.get(q["id"])
            q["sched_text"] = sched.wann(d, now) if d else ""
    summary = {"now": now.timestamp()}
    if druckt and left:
        e = now + _td(seconds=left)
        summary["printing_until"] = f"{sched.wann(e, now)}"
        ende = max(ende or 0, e.timestamp())
    if ende:
        summary["end"] = sched.wann(_dt.fromtimestamp(ende), now)
    summary["window"] = f"heute {_fenster_text(win, now)}"
    return summary


def _parse_when(s, now):
    """„HH:MM“ (nächstes Vorkommen) oder „YYYY-MM-DDTHH:MM“."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        d = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return d if d > now else d + _td(days=1)
    return _dt.fromisoformat(s).replace(second=0, microsecond=0, tzinfo=None)


def _queue_set_plan(qid, b, by="PrintHub"):
    items = {q["id"]: q for q in bambuddy.queue()}
    q = items.get(int(qid))
    if not q or q.get("status") != "pending":
        raise LookupError("Auftrag nicht (mehr) wartend")
    win, tz = _wcfg()
    now = _local_now(tz)
    pl = _plans()
    if b.get("mode") == "clear":
        bambuddy.schedule(qid, None)
        pl.pop(int(qid), None)
        _plans_save(pl)
        _qlog(qid, q["name"], f"Planung entfernt ({by})")
        return {"ok": True, "message": "Planung entfernt ✓"}
    secs = q.get("seconds") or 0
    when = _parse_when(b.get("time"), now)
    if b.get("mode") == "end":
        if not secs:
            raise ValueError("Druckdauer unbekannt – „Fertig um“ geht nur mit bekannter Dauer")
        when = sched.plan_start_for_end(win, when, secs)
    if when < now - _td(minutes=1):
        raise ValueError(f"Start {when:%d.%m. %H:%M} liegt in der Vergangenheit")
    force = bool(b.get("force"))
    end = when + _td(seconds=secs)
    if not force and secs and not win.im_fenster(when, secs):
        return {"ok": False, "needs_confirm": True,
                "message": f"Start {sched.wann(when, now)} → fertig {end:%H:%M} liegt außerhalb des Druckfensters "
                           f"({_fenster_text(win, when)}, {win.puffer} min Puffer). Trotzdem so planen?"}
    utc = (when.replace(tzinfo=tz) if tz else when.astimezone()).astimezone(_tz.utc)
    bambuddy.schedule(qid, utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if force:
        pl[int(qid)] = {"force": True, "by": by, "time": time.time()}
    else:
        pl.pop(int(qid), None)
    _plans_save(pl)
    txt = f"Start {sched.wann(when, now)}" + (f" → fertig {end:%H:%M}" if secs else "")
    _qlog(qid, q["name"], f"geplant: {txt}" + (" (trotz Druckfenster)" if force else "") + f" ({by})")
    hint = "" if nachtruhe.running() or not q.get("manual") else " – Achtung: Druckwächter läuft nicht, er startet den Auftrag."
    return {"ok": True, "message": f"Geplant ✓ {txt}{hint}"}


def _qlog(qid, name, text):
    with store.lock:
        lst = store.data.setdefault("queue_log", [])
        lst.insert(0, {"time": time.time(), "id": qid, "name": name, "text": text})
        del lst[300:]
        store._save()


def _queue_action(qid, action, by="PrintHub"):
    """jetzt (über den Druckwächter) · top · cancel. Gibt Kurztext zurück."""
    items = {q["id"]: q for q in bambuddy.queue()}
    q = items.get(int(qid))
    if not q:
        raise LookupError("Auftrag nicht (mehr) in der Warteschlange")
    if action == "jetzt":
        if nachtruhe.running():
            os.makedirs(os.path.dirname(TG_INBOX), exist_ok=True)
            with open(TG_INBOX, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": f"ph-{qid}-{int(time.time())}", "msg": f"jetzt:{qid}", "time": int(time.time())}) + "\n")
            _qlog(qid, q["name"], f"„Jetzt starten“ ({by}) – Druckwächter startet mit Plattenprüfung")
            return "Druckwächter startet den Auftrag (Einschalten, Licht, Plattenprüfung) …"
        bambuddy.start(qid)
        _qlog(qid, q["name"], f"direkt gestartet ({by}, Druckwächter aus)")
        return "Gestartet ✓"
    if action == "top":
        pend = [x for x in items.values() if x["status"] == "pending"]
        order = [int(qid)] + [x["id"] for x in sorted(pend, key=lambda x: x.get("position") or 0) if x["id"] != int(qid)]
        bambuddy.reorder(order)
        _qlog(qid, q["name"], f"nach vorne geholt ({by})")
        return "Nach vorne geholt ✓"
    if action == "cancel":
        bambuddy.cancel(qid)
        _qlog(qid, q["name"], f"aus der Warteschlange genommen ({by})")
        return "Entfernt ✓"
    raise ValueError("Unbekannte Aktion")


async def api_queue(request):
    if not bambuddy.enabled:
        return JSONResponse({"enabled": False, "items": []})
    try:
        items = await run_in_threadpool(_queue_checked)
    except Exception as e:
        return JSONResponse({"enabled": True, "items": [], "error": str(e) if isinstance(e, PermissionError)
                             else f"Bambuddy nicht erreichbar ({e.__class__.__name__})"})
    try:
        summary = await run_in_threadpool(_queue_plan, items)
    except Exception as e:
        log.warning("Zeitplan-Vorschau: %s", e)
        summary = {}
    return JSONResponse({"enabled": True, "items": items, "url": bambuddy.url, "waechter": nachtruhe.running(),
                         "schedule": summary})


async def api_queue_plan(request):
    qid = request.path_params["qid"]
    try:
        b = await request.json()
        res = await run_in_threadpool(_queue_set_plan, qid, b)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(res)


async def api_queue_action(request):
    qid, action = request.path_params["qid"], request.path_params["action"]
    action = {"start": "jetzt"}.get(action, action)
    if action not in ("jetzt", "top", "cancel"):
        return JSONResponse({"error": "Unbekannte Aktion"}, status_code=404)
    try:
        msg = await run_in_threadpool(_queue_action, qid, action)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    log.info("Warteschlange: Auftrag %s → %s", qid, action)
    return JSONResponse({"ok": True, "message": msg})


async def api_queue_reorder(request):
    b = await request.json()
    ids = [int(x) for x in b.get("ids") or [] if str(x).isdigit()]
    try:
        await run_in_threadpool(bambuddy.reorder, ids)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


def _queue_history():
    """Bambuddy-Verlauf + Ereignisse vom Druckwächter + Aktionen in PrintHub, neueste zuerst."""
    out = []
    try:
        for h in bambuddy.history(80):
            t = h.get("completed_at") or h.get("started_at") or h.get("created_at")
            ts = None
            if t:
                with contextlib.suppress(Exception):
                    from datetime import datetime, timezone
                    ts = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                    ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()
            st = {"completed": "fertig", "failed": "fehlgeschlagen", "skipped": "übersprungen", "cancelled": "abgebrochen"}.get(h.get("status"), h.get("status"))
            out.append({"time": ts, "id": h.get("id"), "name": h.get("archive_name") or h.get("library_file_name") or f"Auftrag {h.get('id')}",
                        "text": st + (f" – {h['error_message']}" if h.get("error_message") else ""), "src": "Bambuddy", "status": h.get("status")})
    except Exception as e:
        out.append({"time": time.time(), "id": None, "name": "Bambuddy", "text": f"Verlauf nicht abrufbar: {e}", "src": "PrintHub"})
    try:
        with open(QEVENTS, encoding="utf-8") as f:
            for line in f.readlines()[-400:]:
                with contextlib.suppress(Exception):
                    e = json.loads(line)
                    out.append({**e, "src": "Druckwächter"})
    except OSError:
        pass
    for e in store.data.get("queue_log", [])[:200]:
        out.append({**e, "src": "PrintHub"})
    out.sort(key=lambda x: x.get("time") or 0, reverse=True)
    return out[:300]


async def api_queue_history(request):
    if not bambuddy.enabled:
        return JSONResponse({"enabled": False, "entries": []})
    return JSONResponse({"enabled": True, "entries": await run_in_threadpool(_queue_history)})


THUMB_RE = re.compile(r"^/(archives|library/files)/\d+/thumbnail$")


async def api_bambuddy_thumb(request):
    path = request.query_params.get("p", "")
    if not THUMB_RE.match(path) or not bambuddy.enabled:
        return Response(status_code=404)
    try:
        data, ctype = await run_in_threadpool(bambuddy.thumbnail, path)
    except Exception:
        return Response(status_code=404)
    return Response(data, media_type=ctype, headers={"Cache-Control": "max-age=3600"})


# ---------- Sicherung ----------
async def api_backup(request):
    media = request.query_params.get("media") == "1"
    blob = await run_in_threadpool(backup.create, DATA_DIR, CONFIG, media)
    name = time.strftime("printhub-sicherung-%Y-%m-%d") + ("-mit-bildern" if media else "") + ".zip"
    log.info("Sicherung erstellt (%d KB%s)", len(blob) // 1024, ", mit Bildern" if media else "")
    return Response(blob, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _restart_self():
    """Nach dem Zurückspielen PrintHub neu starten, damit alles frisch geladen wird."""
    time.sleep(1.5)
    nachtruhe._stop()
    try:
        cid = os.environ.get("HOSTNAME", "")
        status, _ = dockerctl._call("POST", f"/containers/{cid}/restart?t=5")
        if status in (204, 200):
            return
    except Exception as e:
        log.warning("Neustart über Docker nicht möglich: %s", e)
    os._exit(0)


def _parts(q):
    return [x for x in (q.get("parts") or "").split(",") if x in backup.PARTS] or None


async def api_backups(request):
    lst = await run_in_threadpool(autobackup.list)
    return JSONResponse({"backups": lst, "folder": autobackup.folder, "status": autobackup.status(),
                         "parts": {k: v[0] for k, v in backup.PARTS.items()}})


async def api_backups_run(request):
    st = await run_in_threadpool(autobackup.run, "von Hand")
    return JSONResponse(st if st.get("ok") else {"error": st.get("error")}, status_code=200 if st.get("ok") else 500)


async def api_backups_file(request):
    p = autobackup.path(request.path_params["name"])
    if not p:
        return JSONResponse({"error": "Sicherung nicht gefunden"}, status_code=404)
    if request.method == "DELETE":
        os.remove(p)
        return JSONResponse({"ok": True})
    return FileResponse(p, media_type="application/zip", filename=os.path.basename(p))


async def api_backups_restore(request):
    p = autobackup.path(request.path_params["name"])
    if not p:
        return JSONResponse({"error": "Sicherung nicht gefunden"}, status_code=404)
    blob = open(p, "rb").read()
    try:
        written = await run_in_threadpool(backup.restore, blob, DATA_DIR, CONFIG, _parts(request.query_params))
    except (ValueError, OSError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    log.warning("Sicherung %s zurückgespielt (%d Dateien) – PrintHub startet neu", os.path.basename(p), len(written))
    threading.Thread(target=_restart_self, daemon=True).start()
    return JSONResponse({"ok": True, "files": written})


async def api_restore(request):
    blob = await request.body()
    if len(blob) > 500 * 1024 * 1024:
        return JSONResponse({"error": "Datei zu groß"}, status_code=400)
    try:
        written = await run_in_threadpool(backup.restore, blob, DATA_DIR, CONFIG, _parts(request.query_params))
    except (ValueError, OSError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    log.warning("Sicherung zurückgespielt (%d Dateien) – PrintHub startet neu", len(written))
    import threading
    threading.Thread(target=_restart_self, daemon=True).start()
    return JSONResponse({"ok": True, "files": len(written)})


# ---------- Drucker einrichten (config.yaml) ----------
PCFG_FIELDS = {   # Schlüssel -> Typ
    "id": "str", "name": "str", "type": "str", "host": "str", "serial": "str", "access_code": "secret",
    "port": "int", "web_port": "int", "channels": "int", "api_key": "secret", "light_pin": "str",
    "shelly": "str", "shelly_channel": "int", "camera_url": "str", "camera": "bool", "track_usage": "bool",
}
PCFG_SECRET = {"access_code", "api_key"}


def _read_cfg():
    with open(CONFIG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def api_printer_config(request):
    if request.method == "GET":
        c = await run_in_threadpool(_read_cfg)
        out = []
        for pc in c.get("printers", []):
            d = {k: v for k, v in pc.items() if k not in PCFG_SECRET}
            for k in PCFG_SECRET:
                d[k + "_set"] = bool(pc.get(k))
            out.append(d)
        running = [{"id": p.id, "online": p.online} for p in printers.values()]
        return JSONResponse({"printers": out, "running": running, "path": CONFIG})
    body = await request.json()
    try:
        c = await run_in_threadpool(_read_cfg)
        old = {pc.get("id"): pc for pc in c.get("printers", [])}
        new, ids = [], set()
        for raw in body.get("printers", []):
            pc = dict(old.get(raw.get("_old_id") or raw.get("id"), {}))   # unbekannte Felder behalten
            for k, typ in PCFG_FIELDS.items():
                if k not in raw:
                    continue
                v = raw[k]
                if typ == "secret":
                    if v in (None, ""):
                        continue                     # leer = bisherigen Wert behalten
                    pc[k] = str(v).strip()
                elif v in (None, ""):
                    pc.pop(k, None)
                elif typ == "int":
                    pc[k] = int(v)
                elif typ == "bool":
                    pc[k] = bool(v)
                else:
                    pc[k] = str(v).strip()
            pid = str(pc.get("id") or "").strip().lower()
            if not re.match(r"^[a-z0-9_-]{1,20}$", pid):
                raise ValueError(f"Kürzel „{pid}“: nur a–z, 0–9, - und _ (max. 20 Zeichen)")
            if pid in ids:
                raise ValueError(f"Kürzel „{pid}“ ist doppelt")
            ids.add(pid)
            pc["id"] = pid
            if pc.get("type") not in ("bambu", "moonraker"):
                raise ValueError(f"{pid}: Typ muss Bambu oder Klipper/Moonraker sein")
            if not pc.get("host"):
                raise ValueError(f"{pid}: IP-Adresse fehlt")
            if pc["type"] == "bambu" and (not pc.get("serial") or not pc.get("access_code")):
                raise ValueError(f"{pid}: Seriennummer und Zugangscode sind beim Bambu nötig")
            new.append(pc)
        c["printers"] = new
        import shutil
        shutil.copy(CONFIG, CONFIG + ".bak")
        tmp = CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(c, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        os.replace(tmp, CONFIG)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"error": f"config.yaml nicht schreibbar: {e}"}, status_code=500)
    log.info("Drucker-Einstellungen gespeichert (%d Drucker) – Sicherung: config.yaml.bak", len(new))
    return JSONResponse({"ok": True})


async def api_printer_test(request):
    """Verbindung prüfen, bevor gespeichert wird."""
    import socket
    b = await request.json()
    host = str(b.get("host") or "").strip()
    out = []

    def tcp(port, label):
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                out.append([True, f"{label} (Port {port}) erreichbar"])
                return True
        except OSError as e:
            out.append([False, f"{label} (Port {port}): {e.__class__.__name__} – {e}"])
            return False

    def run():
        if not host:
            out.append([False, "IP-Adresse fehlt"])
            return
        if b.get("type") == "bambu":
            tcp(8883, "MQTT")
            tcp(990, "FTPS (Druckdateien)")
            tcp(6000, "Kamera")
        else:
            port = int(b.get("port") or 7125)
            try:
                r = requests.get(f"http://{host}:{port}/server/info", timeout=4)
                out.append([r.ok, f"Moonraker antwortet ({r.status_code})"])
            except Exception as e:
                out.append([False, f"Moonraker: {e.__class__.__name__}"])
        sh = str(b.get("shelly") or "").strip()
        if sh:
            try:
                r = requests.get(f"http://{sh}/shelly", timeout=3)
                d = r.json()
                out.append([True, f"Shelly erreichbar ({d.get('model') or d.get('type') or 'ok'})"])
            except Exception as e:
                out.append([False, f"Shelly {sh}: {e.__class__.__name__}"])
    import requests
    await run_in_threadpool(run)
    return JSONResponse({"results": out})


async def api_restart(request):
    import threading
    threading.Thread(target=_restart_self, daemon=True).start()
    return JSONResponse({"ok": True})


# ---------- Plattenerkennung ----------
def _cam_printer(pid):
    p = printers.get(pid)
    if not p:
        raise LookupError("Drucker unbekannt")
    if not p.camera:
        raise LookupError("Dieser Drucker hat keine Kamera")
    return p


async def api_plate(request):
    pid = request.path_params["pid"]
    try:
        if request.method == "PUT":
            cfg = await run_in_threadpool(plate.set_config, pid, await request.json())
        else:
            cfg = await run_in_threadpool(plate.config, pid)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    p = printers.get(pid)
    cfg["camera"] = bool(p and p.camera)
    return JSONResponse(cfg)


async def api_plate_ref(request):
    pid = request.path_params["pid"]
    name = request.path_params.get("name")
    try:
        if request.method == "GET":
            path = plate.ref_path(pid, name)
            return FileResponse(path, media_type="image/jpeg") if path else Response(status_code=404)
        if request.method == "DELETE":
            return JSONResponse(await run_in_threadpool(plate.delete_ref, pid, name))
        b = {}
        try:
            b = await request.json()
        except Exception:
            pass
        if name:   # Beschriftung oder eigenes Vieleck ändern
            return JSONResponse(await run_in_threadpool(plate.update_ref, pid, name, b))
        p = _cam_printer(pid)
        if b.get("light") and hasattr(p, "set_light"):
            await run_in_threadpool(p.set_light, True)
            await asyncio_sleep(4)
        jpg = await run_in_threadpool(p.camera.snapshot)
        cfg = await run_in_threadpool(plate.add_ref, pid, jpg, b.get("label", ""))
        log.info("[%s] Referenzbild für die Plattenerkennung aufgenommen", pid)
        return JSONResponse(cfg)
    except (LookupError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Kamera: {e}"}, status_code=503)


async def asyncio_sleep(s):
    import asyncio
    await asyncio.sleep(s)


async def api_plate_check(request):
    pid = request.path_params["pid"]
    q = request.query_params
    try:
        p = _cam_printer(pid)
        if q.get("light") == "1" and hasattr(p, "set_light"):
            await run_in_threadpool(p.set_light, True)
            await asyncio_sleep(4)
        jpg = await run_in_threadpool(p.camera.snapshot)
        r = await run_in_threadpool(plate.check, pid, jpg, q.get("threshold"), q.get("plate_type", ""))
    except LookupError as e:
        return JSONResponse({"error": str(e), "is_empty": None, "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Kamera: {e}", "is_empty": None, "message": f"Kamera-Fehler: {e}"}, status_code=503)
    if not r.get("needs_calibration"):
        log.info("[%s] Plattenprüfung: %s", pid, r["message"])
    return JSONResponse(r)


async def api_plate_snapshot(request):
    try:
        p = _cam_printer(request.path_params["pid"])
        jpg = await run_in_threadpool(p.camera.snapshot)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------- Nachtruhe ----------
async def api_nachtruhe(request):
    if request.method == "PUT":
        try:
            clean = await run_in_threadpool(nachtruhe.save, await request.json())
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        log.info("Nachtruhe-Einstellungen gespeichert: %s", ", ".join(k for k in clean if "KEY" not in k and "TOKEN" not in k))
    st = await run_in_threadpool(nachtruhe.state)
    st["log"] = await run_in_threadpool(nachtruhe.tail, int(request.query_params.get("n", 200)))
    return JSONResponse(st)


async def api_nachtruhe_action(request):
    action = request.path_params["action"]
    try:
        if action == "on":
            if await run_in_threadpool(nachtruhe.legacy_running):
                return JSONResponse({"error": "Der alte Container „bambuddy-nachtruhe“ läuft noch – erst stoppen, sonst laufen zwei Wächter gleichzeitig."}, status_code=409)
            settings.update({"nachtruhe_on": True})
        elif action == "off":
            settings.update({"nachtruhe_on": False})
        elif action == "restart":
            await run_in_threadpool(nachtruhe.restart)
        elif action == "stop-legacy":
            name = await run_in_threadpool(nachtruhe.stop_legacy)
            log.info("Alter Nachtruhe-Container %s gestoppt", name)
        else:
            return JSONResponse({"error": "Unbekannte Aktion"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_maintenance_import(request):
    if not settings["bambuddy_import"]:
        return JSONResponse({"error": "Bambuddy-Import ist ausgeschaltet (Einstellungen → Import / Export)"}, status_code=403)
    """Wartungsstand aus Bambuddy übernehmen (Druckstunden, Intervalle, letzte Wartung)."""
    if not bambuddy.enabled:
        return JSONResponse({"error": "Bambuddy-Adresse fehlt (Einstellungen → Dienste)"}, status_code=400)
    try:
        ov = await run_in_threadpool(bambuddy.maintenance_overview)
    except Exception as e:
        return JSONResponse({"error": _bb_err(e)}, status_code=502)
    done, missing = [], []
    for bo in ov:
        bname = (bo.get("printer_name") or "").strip().lower()
        p = next((x for x in printers.values() if x.name.strip().lower() == bname), None)
        if not p:   # sonst: einziger Bambu-Drucker
            bam = [x for x in printers.values() if x.type == "bambu"]
            p = bam[0] if len(bam) == 1 and len(ov) == 1 else None
        if not p:
            missing.append(bo.get("printer_name"))
            continue
        _, hh = await run_in_threadpool(_history_hours, p)
        r = maint.import_bambuddy(p, bo, hh)
        done.append({"printer": p.name, **r})
        log.info("Wartung aus Bambuddy übernommen für %s: %s", p.name, r)
    if not done:
        return JSONResponse({"error": "Kein passender Drucker gefunden" + (f" (Bambuddy: {', '.join(map(str, missing))})" if missing else "")},
                            status_code=400)
    return JSONResponse({"ok": True, "done": done, "missing": missing})


async def api_light(request):
    p = printers.get(request.path_params["pid"])
    if not p or not hasattr(p, "set_light"):
        return JSONResponse({"error": "Licht wird nicht unterstützt"}, status_code=400)
    b = await request.json()
    try:
        await run_in_threadpool(p.set_light, bool(b.get("on")))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


# ---------- Diagnose ----------
STARTED_AT = time.time()


def _secret_values():
    vals = [settings.get(k) for k in ("tg_token", "ntfy_token", "bambuddy_key", "pin_hash")]
    for pc in cfg.get("printers", []):
        vals += [pc.get("access_code"), pc.get("api_key")]
    return [v for v in vals if v]


logbuf.redact.secrets_fn = _secret_values


def _tcp(host, port, timeout=3):
    import socket
    with socket.create_connection((host, port), timeout=timeout):
        return True


def _diagnose():
    """Alle Prüfungen parallel. Ergebnis: Liste (Gruppe, Name, ok|warn|bad|info, Text)."""
    from concurrent.futures import ThreadPoolExecutor
    checks = []

    def add(group, name, fn):
        checks.append((group, name, fn))

    for p in printers.values():
        def pr(p=p):
            st = p.status()
            if not st.get("online"):
                return "bad", "keine Verbindung – Drucker aus oder Netzwerk/Zugangscode prüfen"
            return "ok", f"verbunden · {st.get('state', '')}" + (f" {st.get('progress') or 0} %" if st.get("printing") else "")
        add(p.name, "Verbindung", pr)
        if p.type == "bambu":
            for port, lab in ((8883, "MQTT"), (990, "Dateien (FTPS)"), (6000, "Kamera-Port")):
                def tc(p=p, port=port):
                    _tcp(p.host, port)
                    return "ok", f"{p.host}:{port} erreichbar"
                add(p.name, lab, tc)
        else:
            def mr(p=p):
                r = requests.get(f"{p.base}/server/info", timeout=4)
                r.raise_for_status()
                k = (r.json().get("result") or {}).get("klippy_state", "")
                return ("ok" if k == "ready" else "warn"), f"Moonraker antwortet · Klipper: {k or '?'}"
            add(p.name, "Moonraker", mr)
        if p.camera:
            def cam(p=p):
                t = time.time()
                jpg = p.camera.snapshot()
                dt = time.time() - t
                if not jpg:
                    return "bad", "kein Bild"
                return ("ok" if dt < 6 else "warn"), f"Bild in {dt:.1f} s ({len(jpg) // 1024} KB)"
            add(p.name, "Kamera", cam)
        if p.shelly:
            def sh(p=p):
                if not p.shelly.online:
                    return "bad", p.shelly.last_error or "nicht erreichbar"
                return "ok", f"{p.shelly.host} · {p.shelly.watts or 0:.0f} W · {'an' if p.shelly.on else 'aus'}"
            add(p.name, "Shelly", sh)
        else:
            add(p.name, "Shelly", lambda: ("info", "nicht eingerichtet (Strom wird nicht gemessen)"))

    def sm():
        sp = spoolman.spools(0)
        return "ok", f"{settings['spoolman_url']} · {len(sp)} Spulen"
    add("Dienste", "Spoolman", sm)

    def bb():
        if not bambuddy.enabled:
            return "info", "nicht eingerichtet"
        v = bambuddy.version()
        q = bambuddy.queue()
        return "ok", f"Version {v or '?'} · Warteschlange {len(q)}"
    add("Dienste", "Bambuddy", bb)

    def dk():
        if not os.path.exists(dockerctl.SOCK):
            return "warn", "docker.sock nicht eingebunden – Neustart-Knöpfe und Container-Status fehlen"
        out = []
        bad = False
        for key in ("spoolman_container", "bambuddy_container", "oe_container"):
            n = (settings.get(key) or "").split(",")[0].strip()
            if not n:
                continue
            try:
                i = dockerctl.info(n)
                out.append(f"{i['name']}: {i['state']}")
                bad = bad or not i["running"]
            except Exception as e:
                out.append(f"{n}: {e}")
                bad = True
        return ("warn" if bad else "ok"), " · ".join(out) or "erreichbar"
    add("Dienste", "Docker / Container", dk)

    def wa():
        st = nachtruhe.state()
        if not st["enabled"]:
            return "info", "ausgeschaltet"
        if st.get("legacy_running"):
            return "bad", "alter Container „bambuddy-nachtruhe“ läuft noch parallel"
        if not st["running"]:
            return "bad", f"eingeschaltet, läuft aber nicht (Exit {st.get('last_exit')})"
        dry = str(st["config"].get("DRY_RUN", "0")) in ("1", "true")
        return ("warn" if dry else "ok"), "läuft" + (" – Probelauf ist AN (schaltet nichts)" if dry else "")
    add("Dienste", "Druckwächter", wa)

    def nt():
        url = settings.get("ntfy_url") or ""
        if not url:
            return "info", "nicht eingerichtet"
        server = url.rstrip("/").rsplit("/", 1)[0]
        r = requests.get(f"{server}/v1/health", timeout=5)
        ok = r.ok and (r.json() or {}).get("healthy", True)
        return ("ok" if ok else "warn"), f"{server} erreichbar"
    add("Benachrichtigungen", "ntfy", nt)

    def tg():
        if not telegram.token:
            return "info", "nicht eingerichtet"
        me = telegram._call("getMe")
        if not telegram.chat:
            return "warn", f"Bot @{me.get('username')} erreichbar, aber noch kein Chat verbunden"
        return "ok", f"@{me.get('username')} · Chat verbunden" + (f" · letzter Fehler: {telegram.last_error}" if telegram.last_error else "")
    add("Benachrichtigungen", "Telegram", tg)

    def lbl():
        h = settings.get("label_host") or ""
        if not h:
            return "info", "nicht eingerichtet (Druck über den Browser)"
        host, _, port = h.partition(":")
        _tcp(host, int(port or 9100))
        return "ok", f"{settings['label_model']} unter {h} erreichbar"
    add("Geräte", "Etikettendrucker", lbl)

    def strom():
        if not settings.get("power_auto"):
            bb = []
            for p in printers.values():
                i = power.info(p)
                if i.get("bb_auto_off"):
                    bb.append(p.name)
            return "info", "Strom-Verwaltung aus" + (f" – Bambuddy schaltet aus ({', '.join(bb)})" if bb else "")
        clash = [p.name for p in printers.values() if power.info(p).get("bb_auto_off")]
        if clash:
            return "warn", f"PrintHub UND Bambuddy schalten automatisch aus ({', '.join(clash)}) – in Bambuddy „Auto Aus“ abschalten"
        miss = [p.name for p in printers.values() if not power.available(p)]
        return ("warn" if miss else "ok"), "PrintHub schaltet automatisch aus" + (f" · ohne Steckdose: {', '.join(miss)}" if miss else "")
    add("Geräte", "Strom-Verwaltung", strom)

    def disk():
        import shutil
        u = shutil.disk_usage(DATA_DIR)
        free = u.free / 1e9
        return ("bad" if free < 0.5 else "warn" if free < 2 else "ok"), f"{free:.1f} GB frei von {u.total / 1e9:.0f} GB ({DATA_DIR})"
    add("System", "Speicherplatz", disk)

    def bk():
        st = autobackup.status()
        mode = settings.get("backup_auto") or "off"
        if not st.get("time"):
            return ("warn" if mode == "off" else "info"), "noch keine automatische Sicherung" + (" – Zeitplan ist aus" if mode == "off" else "")
        age = (time.time() - st["time"]) / 86400
        when = time.strftime("%d.%m.%Y %H:%M", time.localtime(st["time"]))
        if not st.get("ok"):
            return "bad", f"letzte Sicherung am {when} fehlgeschlagen: {st.get('error')}"
        limit = 8 if mode == "weekly" else 2
        return ("warn" if mode != "off" and age > limit else "ok"), f"zuletzt {when}" + (" · Zeitplan aus" if mode == "off" else "")
    add("System", "Sicherung", bk)

    def tz():
        tzname = os.environ.get("TZ", "")
        now = time.strftime("%d.%m.%Y %H:%M:%S %Z")
        return ("ok" if tzname else "warn"), f"{now}" + (f" · TZ={tzname}" if tzname else " · TZ nicht gesetzt (UTC?)")
    add("System", "Uhrzeit", tz)

    def ver():
        up = int(time.time() - STARTED_AT)
        return "info", f"Version {os.environ.get('PRINTHUB_VERSION', 'dev')} · läuft seit {up // 86400} T {up % 86400 // 3600} h {up % 3600 // 60} min"
    add("System", "PrintHub", ver)

    def friendly(e):
        m = str(e)
        if isinstance(e, (ConnectionRefusedError,)) or "Connection refused" in m:
            return "nicht erreichbar – Verbindung abgelehnt (Gerät/Dienst aus oder falscher Port)"
        if isinstance(e, (socket.timeout, TimeoutError, requests.Timeout)) or "timed out" in m.lower():
            return "keine Antwort (Zeitüberschreitung) – Adresse prüfen, Gerät im Netz?"
        if "Name or service not known" in m or "getaddrinfo" in m or "nodename" in m:
            return "Adresse unbekannt – Schreibweise prüfen"
        if isinstance(e, requests.HTTPError) and e.response is not None:
            return f"antwortet mit Fehler {e.response.status_code}" + (" – Schlüssel/Token prüfen" if e.response.status_code in (401, 403) else "")
        if isinstance(e, requests.ConnectionError):
            return "nicht erreichbar – Adresse/Port prüfen, läuft der Dienst?"
        return f"{e.__class__.__name__}: {m}"[:300]

    def run(c):
        group, name, fn = c
        t = time.time()
        try:
            status, text = fn()
        except Exception as e:
            status, text = "bad", friendly(e)
        return {"group": group, "name": name, "status": status, "text": logbuf.redact(text), "ms": int((time.time() - t) * 1000)}

    with ThreadPoolExecutor(max_workers=12) as ex:
        return list(ex.map(run, checks))


async def api_diagnose(request):
    res = await run_in_threadpool(_diagnose)
    return JSONResponse({"checks": res, "time": int(time.time())})


async def api_logs(request):
    q = request.query_params
    lvl = {"warn": "WARNING", "error": "ERROR"}.get(q.get("level", ""), "INFO")
    if q.get("source") == "waechter":
        rows = [{"time": None, "level": "INFO", "source": "druckwaechter", "msg": logbuf.redact(l)} for l in list(nachtruhe.output)]
        if lvl != "INFO":
            rows = [r for r in rows if re.search(r"fehl|error|warn|nicht erreichbar|exception|traceback", r["msg"], re.I)]
        return JSONResponse({"entries": rows[-400:]})
    return JSONResponse({"entries": logbuf.handler.entries(lvl, int(q.get("limit", 400)))})


# ---------- Telegram-Befehle ----------
def _de(v, nd=0):
    return f"{v:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dauer(sec):
    m = int((sec or 0) // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _tg_filament(args):
    spools, err = _spool_map()
    if err:
        return [{"title": "Spoolman nicht erreichbar", "text": err, "tags": "warning"}]
    low = float(settings["low_spool_g"] or 0)
    used, lines = set(), []
    for p in printers.values():
        rows = []
        for sl in p.slots():
            sid = store.get_slot(p.id, sl["key"])
            sp = spools.get(sid) if sid else None
            if sp:
                used.add(sid)
                rest = sp.get("remaining")
                warn = " ⚠️" if rest is not None and rest < low else ""
                rows.append(f"{sl['label']}: {sp['name']} · {_de(rest or 0)} g{warn}")
            elif sl.get("material"):
                rows.append(f"{sl['label']}: {sl['material']} (keine Spule zugeordnet)")
        if rows:
            lines.append(f"🖨️ {p.name}\n" + "\n".join(rows))
    rest = sorted((s for i, s in spools.items() if i not in used), key=lambda s: (s.get("remaining") or 0))
    if rest:
        ll = []
        for sp in rest[:30]:
            r = sp.get("remaining") or 0
            ll.append(f"{'⚠️ ' if r < low else ''}#{sp['id']} {sp['name']} · {_de(r)} g" + (f" · {sp['location']}" if sp.get("location") else ""))
        tot = sum((s.get("remaining") or 0) for s in spools.values())
        lines.append(f"🧵 Lager ({len(rest)} Spulen)\n" + "\n".join(ll) + (f"\n… und {len(rest) - 30} weitere" if len(rest) > 30 else ""))
        lines.append(f"Gesamt: {_de(tot / 1000, 1)} kg · ⚠️ = unter {_de(low)} g")
    return [{"title": "Filament", "text": "\n\n".join(lines) or "Keine Spulen in Spoolman.", "tags": "thread"}]


def _tg_bild(args):
    out = []
    for p in printers.values():
        if not p.camera:
            continue
        st = p.status()
        info = f"{st.get('state', '')}" + (f" {st.get('progress') or 0} %" if st.get("printing") else "")
        try:
            out.append({"title": f"{p.name}", "text": info, "photo": p.camera.snapshot(), "tags": "printer"})
        except Exception as e:
            out.append({"title": f"{p.name}: kein Kamerabild", "text": str(e), "tags": "warning"})
    return out or [{"title": "Keine Kamera eingerichtet", "text": "", "tags": "warning"}]


def _tg_queue(args):
    if not bambuddy.enabled:
        return [{"title": "Bambuddy ist nicht eingerichtet", "text": "", "tags": "warning"}]
    items = _queue_checked()
    if not items:
        return [{"title": "Warteschlange leer", "text": "", "tags": "inbox_tray"}]
    try:
        sm = _queue_plan(items)
    except Exception:
        sm = {}
    ic = {"ok": "✅", "warn": "⚠️", "bad": "❌"}
    head = "▶ startet über den Druckwächter (Plattenprüfung), ⬆ holt nach vorne, 🗑 nimmt raus."
    if sm.get("end"):
        head = f"🏁 Alles fertig: {sm['end']} (Druckfenster {sm.get('window', '')})\n" + head
    out = [{"title": f"Aufträge ({len(items)})", "tags": "inbox_tray", "text": head}]
    for n, q in enumerate(items[:8], 1):
        c = q.get("check") or {}
        meta = " · ".join(x for x in [q["printer"], _dauer(q["seconds"]) if q.get("seconds") else "",
                                       f"{_de(q['grams'])} g" if q.get("grams") else ""] if x)
        txt = meta + (f"\n{ic.get(c.get('state'), '')} {c.get('text')}" if c.get("text") else "")
        if q.get("status") == "printing":
            out.append({"title": f"{n}. {q['name']} – druckt", "text": meta, "tags": "printer"})
            continue
        pq = q.get("plan") or {}
        if pq.get("text"):
            txt += f"\n🕘 {pq['text']}"
        if q.get("forced"):
            txt += " · trotz Druckfenster"
        if q.get("waiting"):
            txt += f"\n⏳ {q['waiting']}"
        out.append({"title": f"{n}. {q['name']}", "text": txt, "tags": "inbox_tray",
                    "buttons": [[("▶ Jetzt", f"ph:q:jetzt:{q['id']}"), ("⬆ Vor", f"ph:q:top:{q['id']}"), ("🗑", f"ph:q:del:{q['id']}")]]})
    if len(items) > 8:
        out.append({"title": f"… und {len(items) - 8} weitere", "text": "Alle in PrintHub → Übersicht", "tags": "inbox_tray"})
    return out


def _tg_verlauf(args):
    rows = history.recent(5)
    if not rows:
        return [{"title": "Noch keine Drucke im Verlauf", "text": "", "tags": "information_source"}]
    ic = {"ok": "✅", "fail": "❌"}
    lines = []
    for r in rows:
        t = time.strftime("%d.%m. %H:%M", time.localtime(r["time"]))
        extra = " · ".join(x for x in [_dauer(r.get("duration")) if r.get("duration") else "",
                                        f"{_de(r['grams'])} g" if r.get("grams") else "",
                                        f"{_de(r['total_cost'], 2)} €" if r.get("total_cost") else ""] if x)
        lines.append(f"{ic.get(r.get('outcome'), '⏹️')} {t} · {r.get('job') or '?'}" + (f"\n   {extra}" if extra else ""))
    knoepfe, seen = [], set()
    for r in rows:
        if r.get("job") and r["job"] not in seen and len(knoepfe) < 4:
            seen.add(r["job"])
            knoepfe.append([(f"↻ {r['job'][:28]}", f"ph:re:{r['id']}")])
    out = {"title": "Letzte Drucke", "text": "\n".join(lines), "tags": "checkered_flag"}
    if knoepfe and bambuddy.enabled:
        out["text"] += "\n\n↻ = nochmal in die Warteschlange"
        out["buttons"] = knoepfe
    return [out]


def _tg_statistik(args):
    rng = {"woche": "week", "monat": "month", "jahr": "year", "gesamt": "all"}.get(args.lower(), "month")
    k = _statistics(rng, "")["kpis"]
    name = {"week": "Diese Woche", "month": "Dieser Monat", "year": "Dieses Jahr", "all": "Gesamt"}[rng]
    txt = (f"Drucke: {k['prints']} ({k['ok']} fertig, {k['fail']} Fehler, {k['cancel']} abgebrochen)"
           + (f"\nErfolgsrate: {_de(k['success_rate'], 1)} %" if k.get("success_rate") is not None else "")
           + f"\nDruckzeit: {_de(k['hours'], 1)} h\nFilament: {_de(k['grams'])} g"
           + (f"\nStrom: {_de(k['kwh'], 2)} kWh" if k.get("kwh") else "")
           + f"\nKosten: {_de(k['total_cost'], 2)} € (Ø {_de(k['avg_cost'], 2)} € pro Druck)"
           + "\n\n/statistik woche · jahr · gesamt")
    return [{"title": f"Statistik – {name}", "text": txt, "tags": "information_source"}]


def _tg_wartung(args):
    out = []
    for p in printers.values():
        _, hours = _hours_total(p)
        ms = maint.printer_status(p, hours)
        unit = lambda i: {"d": "Tage", "p": "Drucke"}.get(i["unit"], "h")
        due = [i for i in ms["items"] if i["due"]]
        warn = [i for i in ms["items"] if i["warn"]]
        lines = [f"🔴 {i['name']} – fällig (seit {_de(i['since'], 0)} {unit(i)})" for i in due] + \
                [f"🟠 {i['name']} – bald (noch {_de(i['remaining'], 0)} {unit(i)})" for i in warn]
        if not lines:
            nxt = sorted((i for i in ms["items"] if i.get("remaining") is not None), key=lambda i: i["pct"], reverse=True)[:3]
            lines = ["✅ Nichts fällig"] + [f"   nächste: {i['name']} in {_de(i['remaining'], 0)} {unit(i)}" for i in nxt]
        out.append({"title": f"Wartung – {p.name} ({_de(ms['hours'], 0)} Druckstunden)", "text": "\n".join(lines), "tags": "wrench"})
    return out


def _tg_licht(args):
    out = []
    for p in printers.values():
        if not hasattr(p, "set_light") or not p.online:
            continue
        on = p.status().get("light")
        out.append({"title": f"Licht {p.name}", "text": f"Jetzt: {'an 💡' if on else 'aus' if on is False else 'unbekannt'}", "tags": "bulb",
                    "buttons": [[("💡 An", f"ph:light:{p.id}:1"), ("⚫ Aus", f"ph:light:{p.id}:0")]]})
    return out or [{"title": "Kein Drucker erreichbar", "text": "", "tags": "warning"}]


def _tg_platte(args):
    out = []
    for p in printers.values():
        if not p.camera:
            continue
        if hasattr(p, "set_light") and p.online:
            with contextlib.suppress(Exception):
                p.set_light(True)
                time.sleep(4)
        r = plate.check(p.id, p.camera.snapshot())
        if r.get("needs_calibration"):
            out.append({"title": f"{p.name}: Plattenerkennung nicht eingerichtet", "text": r.get("message", ""), "tags": "warning"})
            continue
        img = None
        if str(r.get("debug_image", "")).startswith("data:image"):
            img = base64.b64decode(r["debug_image"].split(",", 1)[1])
        txt = (f"größtes Teil {_de(r.get('object_percent') or 0, 2)} % (ab {_de(r.get('min_obj') or 0, 2)} %)"
               f" · Fläche {_de(r.get('difference_percent') or 0, 1)} % (ab {_de(r.get('threshold') or 0, 1)} %)")
        out.append({"title": f"{p.name}: {'Platte leer' if r.get('is_empty') else 'Etwas liegt auf der Platte'}",
                    "text": txt, "photo": img, "tags": "white_check_mark" if r.get("is_empty") else "stop_sign"})
    return out or [{"title": "Keine Kamera eingerichtet", "text": "", "tags": "warning"}]


def _tg_strom(args):
    out = []
    for p in printers.values():
        if not power.available(p):
            continue
        i = power.info(p)
        on = power.plug_on(p)
        zust = "an ⚡" if on else "aus" if on is False else "unbekannt"
        txt = f"Jetzt: {zust}" + (f" · {i['watts']:.0f} W" if on and i.get("watts") is not None else "")
        if on and i.get("off_in_s") is not None:
            txt += f"\nGeht automatisch aus in {max(1, i['off_in_s'] // 60)} min"
        elif on and i.get("keep"):
            txt += f"\nBleibt an: {i['keep']}"
        out.append({"title": f"Strom {p.name}", "text": txt, "tags": "electric_plug",
                    "buttons": [[("⚡ An", f"ph:power:{p.id}:1"), ("⏻ Aus", f"ph:power:{p.id}:0")]]})
    return out or [{"title": "Keine Steckdose eingerichtet", "text": "Shelly in PrintHub oder Steckdose in Bambuddy fehlt.", "tags": "warning"}]


def _tg_button(data):
    parts = data.split(":")
    if parts[1] == "q" and len(parts) == 4:
        act, qid = parts[2], parts[3]
        if act == "del":
            return "Sicher?", [{"title": "Auftrag wirklich aus der Warteschlange nehmen?", "text": f"Auftrag #{qid}", "tags": "warning",
                                "buttons": [[("🗑 Ja, rausnehmen", f"ph:q:delok:{qid}"), ("Nein", "noop")]]}]
        try:
            msg = _queue_action(qid, {"delok": "cancel"}.get(act, act), "Telegram")
        except Exception as e:
            return str(e)[:190], []
        return msg[:190], []
    if parts[1] == "power" and len(parts) == 4:
        p = printers.get(parts[2])
        if not p:
            return "Drucker unbekannt", []
        try:
            power.switch(p, parts[3] == "1", "von Hand (Telegram)")
        except ValueError as e:
            return str(e)[:190], []
        return f"{p.name} {'eingeschaltet' if parts[3] == '1' else 'ausgeschaltet'} ✓", []
    if parts[1] == "swap" and len(parts) == 4:
        p = printers.get(parts[2])
        if not p or not hasattr(p, "swap_slot"):
            return "Drucker unbekannt", []
        try:
            return p.swap_slot(parts[3])[:190], []
        except ValueError as e:
            return str(e)[:190], []
    if parts[1] == "re" and len(parts) == 3:
        try:
            return _reprint(parts[2], "Telegram")[:190], []
        except (ValueError, LookupError, RuntimeError, PermissionError) as e:
            return str(e)[:190], []
    if parts[1] == "dry" and len(parts) == 3:
        _dry_set(parts[2], "dried")
        return f"Spule #{parts[2]} als getrocknet gespeichert ✓", []
    if parts[1] == "sp" and len(parts) == 4 and parts[2] == "zero":
        sid = int(parts[3])
        try:
            spoolman.update_spool(sid, {"remaining_weight": 0})
            _release_spool(sid)
            spoolman.archive_spool(sid)
        except Exception as e:
            return f"Nicht geklappt: {e}"[:190], []
        log.info("Lager: Spule #%s leer – auf 0 gesetzt und archiviert (Telegram)", sid)
        return f"Spule #{sid} auf 0 g gesetzt und archiviert ✓", []
    if parts[1] == "light" and len(parts) == 4:
        p = printers.get(parts[2])
        if not p:
            return "Drucker unbekannt", []
        p.set_light(parts[3] == "1")
        return f"Licht {p.name} {'an' if parts[3] == '1' else 'aus'} ✓", []
    return "Unbekannter Knopf", []


telegram.commands = {
    "filament": ("Spulen im Drucker und im Lager", _tg_filament),
    "bild": ("Aktuelles Kamerabild", _tg_bild),
    "warteschlange": ("Aufträge mit Knöpfen (starten, vor, raus)", _tg_queue),
    "verlauf": ("Die letzten 5 Drucke", _tg_verlauf),
    "statistik": ("Drucke, Filament, Kosten (Monat)", _tg_statistik),
    "wartung": ("Fällige Wartungen", _tg_wartung),
    "licht": ("Licht an/aus", _tg_licht),
    "strom": ("Drucker ein-/ausschalten", _tg_strom),
    "platte": ("Plattenprüfung jetzt", _tg_platte),
    "hilfe": ("Alle Befehle + Knöpfe", lambda a: [{"help": True}]),
}
telegram.on_button = _tg_button


async def api_power(request):
    p = printers.get(request.path_params["pid"])
    if not p:
        return JSONResponse({"error": "Drucker unbekannt"}, status_code=404)
    b = await request.json()
    on = bool(b.get("on"))
    try:
        via = await run_in_threadpool(power.switch, p, on, "von Hand (PrintHub)", bool(b.get("force")))
    except ValueError as e:
        return JSONResponse({"error": str(e), "blocked": True}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "via": via})


async def api_power_bambuddy(request):
    """Bambuddys eigenes Auto-Ein/Auto-Aus der Steckdose an/aus (damit nur einer entscheidet)."""
    b = await request.json()
    out = []
    for p in printers.values():
        s = power.st.setdefault(p.id, {})
        pid = await run_in_threadpool(power._plug, p, s)
        if not pid:
            continue
        data = {k: bool(b[k]) for k in ("auto_off", "auto_on") if k in b}
        try:
            await run_in_threadpool(bambuddy.plug_update, pid, data)
            s["plug_t"] = 0          # neu einlesen
            out.append(p.name)
        except Exception as e:
            return JSONResponse({"error": f"Bambuddy: {e}"}, status_code=502)
    if not out:
        return JSONResponse({"error": "Keine Bambuddy-Steckdose gefunden"}, status_code=404)
    return JSONResponse({"ok": True, "printers": out})


async def api_telegram(request):
    info = telegram.info()
    if info.get("error"):
        info["error"] = logbuf.redact(info["error"])
    return JSONResponse({**info, "events": settings["tg_events"]})


async def api_telegram_pair(request):
    try:
        name = await run_in_threadpool(telegram.start_pairing)
    except Exception as e:
        return JSONResponse({"error": f"Bot nicht erreichbar: {e}"}, status_code=400)
    return JSONResponse({"ok": True, "bot": name})


async def api_telegram_unpair(request):
    settings.update({"tg_chat": ""})
    return JSONResponse({"ok": True})


async def api_notify_relay(request):
    """Meldungen des Druckwächters → Telegram (ntfy schickt der Druckwächter selbst)."""
    if request.headers.get("x-printhub-token") != INTERNAL_TOKEN:
        return JSONResponse({"error": "nicht erlaubt"}, status_code=403)
    b = await request.json()
    kat = b.get("kat")
    if not telegram.enabled or (kat and kat not in set(settings["tg_events"] or [])):
        return JSONResponse({"ok": True, "sent": False})
    img = None
    if b.get("photo"):
        try:
            img = base64.b64decode(b["photo"])
        except Exception:
            img = None
    btns = [(str(x[0]), str(x[1])) for x in (b.get("buttons") or []) if len(x) == 2]
    ok = await run_in_threadpool(lambda: telegram.send(str(b.get("title") or ""), str(b.get("text") or ""), int(b.get("prio") or 3), img, btns,
                                                       tags=b.get("tags") or []))
    return JSONResponse({"ok": True, "sent": ok})


async def api_notify_test(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    only = b.get("channel") if b.get("channel") in ("ntfy", "telegram") else None
    if only == "telegram" and not notifier.tg_on:
        return JSONResponse({"error": "Telegram ist noch nicht verbunden"}, status_code=400)
    if only != "telegram" and not notifier.url:
        return JSONResponse({"error": "Keine ntfy-Adresse eingetragen (Einstellungen → Benachrichtigungen)"}, status_code=400)
    prio = max(1, min(5, int(b.get("prio") or 3)))
    names = {5: "dringend", 4: "hoch", 3: "normal", 2: "niedrig", 1: "minimal"}
    notifier.send("test", "PrintHub", f"Testnachricht ({names[prio]}, Priorität {prio}) – Benachrichtigungen funktionieren 👍", "tada", prio, force=True, only=only or "ntfy")
    return JSONResponse({"ok": True})


async def api_camera(request):
    p = printers.get(request.path_params["pid"])
    if not p or not p.camera:
        return JSONResponse({"error": "Keine Kamera"}, status_code=404)
    try:
        jpg = await run_in_threadpool(p.camera.snapshot)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@contextlib.asynccontextmanager
async def lifespan(app):
    for p in printers.values():
        if p.shelly:
            p.shelly.start()
        try:
            p.start()
        except Exception as e:
            log.error("Drucker %s konnte nicht starten: %s", p.id, e)
            p.last_error = str(e)
    import threading
    threading.Thread(target=lambda: (time.sleep(20), _backfill_codes()), daemon=True).start()
    threading.Thread(target=_sampler, daemon=True).start()
    threading.Thread(target=autobackup.loop, daemon=True).start()
    threading.Thread(target=telegram.loop, daemon=True).start()
    threading.Thread(target=power.loop, daemon=True).start()
    threading.Thread(target=nachtruhe.supervise, daemon=True).start()
    yield
    nachtruhe._stop()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/assign", assign_page),
        Route("/verlauf", history_page),
        Route("/einstellungen", settings_page),
        Route("/statistik", stats_page),
        Route("/api/statistics", api_statistics),
        Route("/api/statistics/export.csv", api_statistics_csv),
        Route("/api/history/import/bambuddy", api_history_import_bambuddy, methods=["POST"]),
        Route("/lager", lager_page),
        Route("/etiketten", labels_page),
        Route("/api/qr.svg", api_qr),
        Route("/api/labels", api_labels),
        Route("/api/progress", api_progress),
        Route("/manifest.webmanifest", manifest),
        Route("/sw.js", service_worker),
        Route("/api/labels/preview.png", api_label_preview),
        Route("/api/labels/print", api_label_print, methods=["POST"]),
        Route("/api/labels/media", api_label_media),
        Route("/api/lager", api_lager),
        Route("/api/lager/spools", api_lager_spools, methods=["POST"]),
        Route("/api/lager/external", api_lager_external),
        Route("/api/lager/external/manufacturers", api_lager_manufacturers),
        Route("/api/lager/filament", api_lager_filament, methods=["POST"]),
        Route("/api/lager/spools/{sid:int}/archive", api_lager_archive, methods=["POST"]),
        Route("/api/lager/spools/{sid:int}/weigh", api_lager_weigh, methods=["POST"]),
        Route("/api/lager/spools/{sid:int}/dried", api_lager_dried, methods=["POST"]),
        Route("/api/lager/scan", api_lager_scan, methods=["POST"]),
        Route("/api/lager/colorhex", api_lager_colorhex),
        Route("/api/lager/colorsuggest", api_lager_colorsuggest),
        Route("/api/lager/defaults", api_lager_defaults),
        Route("/api/lager/codes", api_lager_codes, methods=["GET", "POST"]),
        Route("/api/lager/template", api_lager_template),
        Route("/api/lager/multi", api_lager_multi, methods=["POST"]),
        Route("/api/lager/bulk", api_lager_bulk, methods=["POST"]),
        Route("/api/lager/vendor/{vid:int}", api_lager_vendor, methods=["POST"]),
        Route("/api/lager/names", api_lager_names, methods=["GET", "POST"]),
        Route("/api/history/{hid:int}/reprint", api_history_reprint, methods=["POST"]),
        Route("/api/lager/spools/{sid:int}/edit", api_lager_spool_edit, methods=["POST"]),
        Route("/api/lager/spools/{sid:int}/unarchive", api_lager_unarchive, methods=["POST"]),
        Route("/api/lager/min", api_lager_min, methods=["PUT"]),
        Route("/api/lager/delete", api_lager_delete, methods=["POST"]),
        Route("/api/history", api_history),
        Route("/api/history/stats", api_history_stats),
        Route("/api/history/export.csv", api_history_csv),
        Route("/api/history/{pid:int}", api_history_delete, methods=["DELETE"]),
        Route("/api/settings", api_settings, methods=["GET", "PUT"]),
        Route("/api/profiles", api_profiles),
        Route("/api/profiles/{fid}", api_profiles, methods=["DELETE"]),
        Route("/api/docker/test", api_docker_test, methods=["POST"]),
        Route("/media/{name}", media),
        Route("/login", login_page),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
        Route("/api/pin", api_pin, methods=["POST"]),
        Route("/api/state", api_state),
        Route("/api/spools", api_spools),
        Route("/api/assign", api_assign, methods=["POST"]),
        Route("/api/notify/test", api_notify_test, methods=["POST"]),
        Route("/api/notify/relay", api_notify_relay, methods=["POST"]),
        Route("/api/telegram", api_telegram),
        Route("/api/printers/{pid}/swap", api_printer_swap, methods=["POST"]),
        Route("/api/printers/{pid}/power", api_power, methods=["POST"]),
        Route("/api/power/bambuddy", api_power_bambuddy, methods=["POST"]),
        Route("/api/diagnose", api_diagnose),
        Route("/api/logs", api_logs),
        Route("/api/telegram/pair", api_telegram_pair, methods=["POST"]),
        Route("/api/telegram/unpair", api_telegram_unpair, methods=["POST"]),
        Route("/api/stats", api_stats),
        Route("/api/printers/{pid}/temps", api_temps),
        Route("/api/printers/{pid}/energy", api_energy),
        Route("/api/maintenance", api_maintenance),
        Route("/api/maintenance/import", api_maintenance_import, methods=["POST"]),
        Route("/api/maintenance/types", api_maintenance_types, methods=["POST"]),
        Route("/api/maintenance/types/{tid}", api_maintenance_types, methods=["PUT", "DELETE", "POST"]),
        Route("/api/maintenance/{pid}/hours", api_maintenance_hours, methods=["PUT"]),
        Route("/api/maintenance/{pid}/{tid}", api_maintenance_item, methods=["PUT"]),
        Route("/api/maintenance/{pid}/{tid}/done", api_maintenance_item, methods=["POST"]),
        Route("/api/services", api_services),
        Route("/api/nachtruhe", api_nachtruhe, methods=["GET", "PUT"]),
        Route("/api/printer-config", api_printer_config, methods=["GET", "PUT"]),
        Route("/api/printer-config/test", api_printer_test, methods=["POST"]),
        Route("/api/restart", api_restart, methods=["POST"]),
        Route("/api/plate/{pid}", api_plate, methods=["GET", "PUT"]),
        Route("/api/plate/{pid}/check", api_plate_check, methods=["GET", "POST"]),
        Route("/api/plate/{pid}/snapshot.jpg", api_plate_snapshot),
        Route("/api/plate/{pid}/ref", api_plate_ref, methods=["POST"]),
        Route("/api/plate/{pid}/ref/{name}", api_plate_ref, methods=["GET", "PUT", "DELETE"]),
        Route("/api/backup", api_backup),
        Route("/api/backup/restore", api_restore, methods=["POST"]),
        Route("/api/backups", api_backups),
        Route("/api/backups/run", api_backups_run, methods=["POST"]),
        Route("/api/backups/{name}", api_backups_file, methods=["GET", "DELETE"]),
        Route("/api/backups/{name}/restore", api_backups_restore, methods=["POST"]),
        Route("/api/nachtruhe/{action}", api_nachtruhe_action, methods=["POST"]),
        Route("/api/services/{svc}/restart", api_service_restart, methods=["POST"]),
        Route("/api/queue", api_queue),
        Route("/api/queue/reorder", api_queue_reorder, methods=["POST"]),
        Route("/api/queue/history", api_queue_history),
        Route("/api/queue/{qid:int}/plan", api_queue_plan, methods=["POST"]),
        Route("/api/queue/{qid:int}/{action}", api_queue_action, methods=["POST"]),
        Route("/api/bambuddy/thumb", api_bambuddy_thumb),
        Route("/api/printers/{pid}/light", api_light, methods=["POST"]),
        Route("/api/printers/{pid}/camera.jpg", api_camera),
        Route("/api/printers/{pid}/{action}", api_control, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    lifespan=lifespan,
    middleware=[Middleware(NoCacheMiddleware), Middleware(AuthMiddleware)],
)
