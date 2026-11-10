"""Andere Container neu starten – über den Docker-Socket (/var/run/docker.sock)."""
import http.client
import json
import logging
import socket
import threading
import time

log = logging.getLogger("docker")
SOCK = "/var/run/docker.sock"


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.path)
        self.sock = s


def _call(method, url, timeout=60):
    c = _UnixHTTP(SOCK, timeout)
    try:
        c.request(method, url)
        r = c.getresponse()
        body = r.read()
        return r.status, body
    finally:
        c.close()


def find_container(name_part):
    """Erster laufender oder gestoppter Container, dessen Name den Text enthält (ohne Groß/klein)."""
    status, body = _call("GET", "/containers/json?all=1")
    if status != 200:
        raise RuntimeError(f"Docker antwortet {status}")
    want = name_part.lower().lstrip("/")
    names = []
    for c in json.loads(body):
        for n in c.get("Names", []):
            n = n.lstrip("/")
            names.append(n)
            if n.lower() == want:
                return n
    for n in names:
        if want in n.lower():
            return n
    raise LookupError(f"Kein Container mit '{name_part}' gefunden (vorhanden: {', '.join(names)})")


def restart(name_part):
    name = find_container(name_part)
    status, body = _call("POST", f"/containers/{name}/restart?t=10")
    if status not in (204, 200):
        raise RuntimeError(f"Neustart von {name} fehlgeschlagen: {status} {body[:200]!r}")
    return name


def restart_later(names, delay, on_done=None):
    def run():
        time.sleep(delay)
        for n in names:
            try:
                real = restart(n)
                log.info("Container %s neu gestartet (Druckstart)", real)
                if on_done:
                    on_done(real, None)
            except Exception as e:
                log.warning("Container '%s' nicht neu gestartet: %s", n, e)
                if on_done:
                    on_done(n, e)
    threading.Thread(target=run, daemon=True).start()


def info(name_part):
    """Zustand eines Containers: running, Status-Text, Startzeit, Image."""
    name = find_container(name_part)
    status, body = _call("GET", f"/containers/{name}/json", timeout=10)
    if status != 200:
        raise RuntimeError(f"Docker antwortet {status}")
    d = json.loads(body)
    st = d.get("State") or {}
    health = (st.get("Health") or {}).get("Status")
    return {"name": name, "running": bool(st.get("Running")), "state": st.get("Status"), "health": health,
            "started": st.get("StartedAt"), "image": (d.get("Config") or {}).get("Image")}


def start(name_part):
    name = find_container(name_part)
    status, body = _call("POST", f"/containers/{name}/start")
    if status not in (204, 304):
        raise RuntimeError(f"Start von {name} fehlgeschlagen: {status} {body[:200]!r}")
    return name


def read_file(name_part, path):
    """Eine Datei aus einem (auch gestoppten) Container lesen – über das Tar-Archiv der Docker-API."""
    import io
    import tarfile
    import urllib.parse
    name = find_container(name_part)
    status, body = _call("GET", f"/containers/{name}/archive?path={urllib.parse.quote(path)}", timeout=20)
    if status != 200:
        return None
    with tarfile.open(fileobj=io.BytesIO(body)) as t:
        for m in t.getmembers():
            if m.isfile():
                return t.extractfile(m).read()
    return None
