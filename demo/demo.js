// PrintHub-Demo: beantwortet alle /api/-Aufrufe im Browser aus aufgenommenen Beispieldaten (fixtures.js).
// Nichts wird gespeichert – nach dem Neuladen ist alles wieder wie vorher.
(() => {
  const D = window.PH_DEMO || {fix: {}, scans: []};
  const BASE = new URL(document.baseURI).pathname;            // z. B. /printhub/
  const t0 = Date.now();
  const mem = {settings: null, scan: 0};
  const realFetch = window.fetch.bind(window);

  // URL → "/api/…?query" (ohne Basis-Pfad und Zeitstempel)
  function norm(u) {
    const url = new URL(u, document.baseURI);
    let p = url.pathname;
    if (p.startsWith(BASE)) p = "/" + p.slice(BASE.length);
    const q = [...url.searchParams].filter(([k]) => k !== "t" && k !== "_").map(([k, v]) => `${k}=${v}`).join("&");
    return {path: p, full: p + (q ? "?" + q : "")};
  }
  function lookup(full, path) {
    const F = D.fix;
    if (F["GET " + full]) return F["GET " + full];
    if (F["GET " + path]) return F["GET " + path];
    const k = Object.keys(F).find(k => k.startsWith("GET " + path + "?"));
    return k ? F[k] : null;
  }
  const json = (o, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // laufender Druck schreitet in der Demo langsam voran
  function liveState(s) {
    s = JSON.parse(JSON.stringify(s));
    const min = (Date.now() - t0) / 60000;
    for (const p of s.printers || []) {
      if (!p.printing) continue;
      const add = Math.min(30, min * 1.5);
      p.progress = Math.min(99, Math.round((p.progress || 0) + add));
      if (p.remaining_min != null) p.remaining_min = Math.max(1, Math.round(p.remaining_min - add * 1.9));
      if (p.layer && p.total_layers) p.layer = Math.min(p.total_layers - 1, Math.round(p.total_layers * p.progress / 100));
    }
    return s;
  }

  let toastAt = 0;
  function demoNote(msg) {
    if (Date.now() - toastAt < 4000) return;
    toastAt = Date.now();
    setTimeout(() => window.PH && PH.toast && PH.toast(msg || "Demo – Änderungen werden nicht gespeichert"), 300);
  }

  async function answer(method, u, body) {
    const {path, full} = norm(u);
    if (!path.startsWith("/api/") && !path.startsWith("/media/")) return null;
    await sleep(120 + Math.random() * 180);
    if (method === "GET") {
      if (path === "/api/settings" && mem.settings) return json(mem.settings);
      const f = lookup(full, path);
      if (f && f.img) return realFetch(BASE + "demo/img/" + f.img);
      if (f && f.text != null) return new Response(f.text, {headers: {"Content-Type": f.type || "text/plain"}});
      if (f) return json(path === "/api/state" ? liveState(f) : f);
      return json(path.endsWith("s") ? [] : {});
    }
    // schreibende Aufrufe: freundlich bestätigen, nichts speichern
    let b = {};
    try { b = typeof body === "string" ? JSON.parse(body) : {}; } catch {}
    if (path === "/api/lager/scan") {
      await sleep(1600);
      const list = D.scans || [];
      if (!list.length) return json({error: "In der Demo gibt es keine Beispiel-Etiketten"}, 400);
      const r = JSON.parse(JSON.stringify(list[mem.scan++ % list.length]));
      if (r.timing) r.timing.upload = 0.3;
      return json(r);
    }
    if (path === "/api/settings") {
      const cur = mem.settings || lookup("/api/settings", "/api/settings") || {settings: {}, info: {}};
      mem.settings = {...cur, settings: {...cur.settings, ...b}};
      demoNote("Demo – Einstellung nur bis zum Neuladen gemerkt");
      return json({ok: true, settings: mem.settings.settings});
    }
    demoNote();
    if (path === "/api/lager/filament") return json({ok: true, id: 1, spools: []});
    if (path === "/api/lager/spools") return json({ok: true, ids: []});
    if (path === "/api/lager/stack" || path === "/api/lager/multi") {
      const rows = b.rows || b.colors || [];
      return json({ok: true, ids: [], spools: [], new: rows.filter(r => !r.fid).length, existing: rows.filter(r => r.fid).length,
        rolls: rows.reduce((a, r) => a + (+r.count || 1), 0)});
    }
    if (path === "/api/labels/print") return json({error: "In der Demo ist kein Etikettendrucker angeschlossen"}, 400);
    return json({ok: true});
  }

  window.fetch = async (input, init = {}) => {
    const u = typeof input === "string" ? input : input.url;
    const r = await answer((init.method || (input.method) || "GET").toUpperCase(), u, init.body);
    return r || realFetch(input, init);
  };

  // XMLHttpRequest (Foto-Upload beim Scannen) nachbilden
  const RealXHR = window.XMLHttpRequest;
  window.XMLHttpRequest = function () {
    const x = this, up = {};
    let method = "GET", url = "";
    x.upload = up; x.readyState = 0; x.status = 0; x.responseText = "";
    x.open = (m, u) => { method = m; url = u; };
    x.setRequestHeader = () => {};
    x.abort = () => {};
    x.send = async body => {
      if (!norm(url).path.startsWith("/api/")) {
        const r = new RealXHR(); r.open(method, url); r.onload = () => { x.status = r.status; x.responseText = r.responseText; x.onload && x.onload(); }; r.send(body); return;
      }
      for (const pct of [30, 70, 100]) { await sleep(120); up.onprogress && up.onprogress({lengthComputable: true, loaded: pct, total: 100}); }
      up.onload && up.onload();
      const r = await answer(method, url, body);
      x.status = r.status; x.readyState = 4; x.responseText = await r.text();
      x.onload && x.onload();
    };
  };

  // Hinweis-Leiste oben
  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.style.cssText = "background:#0e7490;color:#fff;font:600 13px/1.35 system-ui,sans-serif;padding:7px 12px;text-align:center;position:relative;z-index:50";
    bar.innerHTML = `🧪 <b>Demo</b> mit erfundenen Daten – nichts wird gespeichert. Rolle scannen zeigt Beispiel-Etiketten. ` +
      `<a href="https://github.com/milan941899/printhub" target="_blank" style="color:#fff;text-decoration:underline">PrintHub auf GitHub ↗</a>`;
    document.body.prepend(bar);
  });
})();
