"""Kamerabilder: Bambu A1/P1 (LAN-Stream auf Port 6000) und Moonraker-Webcams (Snapshot-URL)."""
import logging
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urljoin

import requests

log = logging.getLogger("camera")
IDLE_STOP = 30  # Stream beenden, wenn so lange niemand zuschaut


class BambuCamera:
    """A1/A1 mini/P1: JPEG-Frames über TLS auf Port 6000 (Login: bblp + Access Code)."""

    def __init__(self, host, access_code, port=6000):
        self.host, self.code, self.port = host, str(access_code), port
        self.frame = None
        self.frame_time = 0
        self.last_request = 0
        self.thread = None
        self.error = ""
        self.lock = threading.Lock()
        self.new_frame = threading.Condition(self.lock)

    def _auth(self):
        d = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
        d += "bblp".encode().ljust(32, b"\0")
        d += self.code.encode().ljust(32, b"\0")
        return d

    def _recv_exact(self, s, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Kamera-Verbindung geschlossen")
            buf += chunk
        return bytes(buf)

    def _run(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        while time.time() - self.last_request < IDLE_STOP:
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as raw:
                    with ctx.wrap_socket(raw, server_hostname=self.host) as s:
                        s.sendall(self._auth())
                        while time.time() - self.last_request < IDLE_STOP:
                            header = self._recv_exact(s, 16)
                            size = int.from_bytes(header[0:4], "little")
                            if not 0 < size < 10_000_000:
                                raise ValueError(f"Ungültige Framegröße {size}")
                            jpg = self._recv_exact(s, size)
                            if jpg[:2] == b"\xff\xd8":
                                with self.new_frame:
                                    self.frame, self.frame_time, self.error = jpg, time.time(), ""
                                    self.new_frame.notify_all()
            except Exception as e:
                self.error = f"Kamera: {e}"
                log.debug("Bambu-Kamera %s: %s", self.host, e)
                time.sleep(3)
        self.thread = None

    def snapshot(self, wait=10):
        self.last_request = time.time()
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        with self.new_frame:
            if self.frame is None or time.time() - self.frame_time > 5:
                self.new_frame.wait(timeout=wait)
            if self.frame is None:
                raise RuntimeError(self.error or "Kein Kamerabild (Kamera im LAN-Modus aktiv?)")
            return self.frame


def _first_jpeg_from_stream(resp, limit=5_000_000):
    """Erstes JPEG aus einem MJPEG-Stream (multipart/x-mixed-replace) herausschneiden."""
    buf = b""
    for chunk in resp.iter_content(16384):
        buf += chunk
        start = buf.find(b"\xff\xd8")
        if start != -1:
            end = buf.find(b"\xff\xd9", start + 2)
            if end != -1:
                return buf[start:end + 2]
        if len(buf) > limit:
            break
    raise RuntimeError("Kein JPEG im Kamerastream gefunden")


class UrlCamera:
    """Beliebige Kamera-URL: JPEG-Snapshot, MJPEG-Stream oder RTSP (über ffmpeg)."""

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        self.s = requests.Session()
        self.cache, self.cache_time = None, 0
        self.lock = threading.Lock()

    def _grab(self, wait):
        if self.url.lower().startswith(("rtsp://", "rtsps://")):
            import subprocess
            p = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", self.url,
                 "-frames:v", "1", "-q:v", "5", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, timeout=wait + 5)
            if p.returncode != 0 or not p.stdout:
                raise RuntimeError("RTSP-Bild fehlgeschlagen: " + p.stderr.decode(errors="ignore")[-200:])
            return p.stdout
        with self.s.get(self.url, headers=self.headers, stream=True, timeout=wait) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "multipart" in ctype:
                return _first_jpeg_from_stream(r)
            data = r.content
            if data[:2] != b"\xff\xd8":
                raise RuntimeError(f"Kamera liefert kein JPEG ({ctype or 'unbekannter Typ'})")
            return data

    def snapshot(self, wait=10):
        with self.lock:
            if self.cache and time.time() - self.cache_time < 0.8:
                return self.cache  # mehrere Zuschauer teilen sich ein Bild
            self.cache, self.cache_time = self._grab(wait), time.time()
            return self.cache


class SnapshotCamera:
    """Moonraker/Klipper: Snapshot-URL aus Config oder aus /server/webcams/list."""

    def __init__(self, web_base, moonraker_base, headers=None):
        self.web_base = web_base
        self.moonraker_base = moonraker_base
        self.headers = headers or {}
        self.url = None
        self.cam = None
        self.s = requests.Session()
        self.lock = threading.Lock()

    def _resolve(self):
        r = self.s.get(self.moonraker_base + "/server/webcams/list", headers=self.headers, timeout=5)
        r.raise_for_status()
        cams = r.json().get("result", {}).get("webcams", [])
        cams = [c for c in cams if c.get("enabled", True) and c.get("snapshot_url")]
        if not cams:
            raise RuntimeError("Keine Webcam in Moonraker eingerichtet – 'camera_url' in der config.yaml setzen")
        return urljoin(self.web_base + "/", cams[0]["snapshot_url"])

    def snapshot(self, wait=10):
        with self.lock:
            if not self.url:
                self.url = self._resolve()
                self.cam = UrlCamera(self.url)
        return self.cam.snapshot(wait)
