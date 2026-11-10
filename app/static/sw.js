// PrintHub als App: immer zuerst das Netz (aktuelle Daten), gespeicherte Kopie nur ohne Verbindung.
const CACHE = "printhub-v1";
const SHELL = ["/static/style.css", "/static/common.js", "/static/ui.js", "/static/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin) return;
  if (u.pathname.startsWith("/api/") || u.pathname.includes("camera") || u.pathname.startsWith("/media/")) return;   // Live-Daten nie aus dem Speicher
  e.respondWith(fetch(r).then(res => {
    if (res.ok && (u.pathname.startsWith("/static/") || r.mode === "navigate")) { const c = res.clone(); caches.open(CACHE).then(x => x.put(r, c)); }
    return res;
  }).catch(() => caches.match(r).then(m => m || new Response(
    `<!DOCTYPE html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0a1220;color:#e6edf7;font-family:system-ui;padding:40px;text-align:center"><h2>PrintHub nicht erreichbar</h2><p>Keine Verbindung zum Server. Im WLAN oder über Tailscale?</p><button onclick="location.reload()" style="padding:10px 18px;border-radius:10px;border:0;background:#22d3ee">Neu laden</button>`,
    {headers: {"Content-Type": "text/html; charset=utf-8"}}))));
});
