// Gemeinsam für alle PrintHub-Seiten: Theme, Navigation, Formatierung, Hinweise
(function () {
  window.PH = {
    // Spule in Seitenansicht: Pappspule mit Wicklung in Filamentfarbe, Dicke der Wicklung = Restmenge (fill 0…1)
    spool(color, fill = 1, h = 34) {
      const c = /^#?[0-9a-f]{6}$/i.test(color || "") ? (color[0] === "#" ? color : "#" + color) : "#9aa4b2";
      const id = "sp" + (PH._sp = (PH._sp || 0) + 1);
      const v = parseInt(c.slice(1), 16), lum = (0.299 * (v >> 16) + 0.587 * (v >> 8 & 255) + 0.114 * (v & 255)) / 255;
      const f = Math.max(0, Math.min(1, +fill || 0)), core = 9, r = core + 18 * f, y0 = 32 - r, X0 = 17, X1 = 53;
      const nl = Math.max(2, Math.round(r / 2.4));
      const lines = Array.from({length: nl}, (_, i) => { const y = (y0 + (i + .5) * 2 * r / nl).toFixed(1); return `<line x1="${X0}" y1="${y}" x2="${X1}" y2="${y}"/>`; }).join("");
      return `<svg class="spl" viewBox="0 0 70 64" width="${Math.round(h * 70 / 64)}" height="${h}" aria-hidden="true">
        <defs>
          <linearGradient id="${id}s" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#000" stop-opacity=".25"/><stop offset=".18" stop-color="#fff" stop-opacity="${lum > .8 ? .25 : .45}"/>
            <stop offset=".4" stop-color="#fff" stop-opacity="0"/><stop offset=".75" stop-color="#000" stop-opacity="0"/>
            <stop offset="1" stop-color="#000" stop-opacity=".5"/></linearGradient>
          <linearGradient id="${id}b" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#b98b55"/><stop offset="1" stop-color="#7c5530"/></linearGradient>
          <radialGradient id="${id}r" cx=".38" cy=".32" r=".8"><stop offset="0" stop-color="#ecc994"/><stop offset=".65" stop-color="#d4a66c"/><stop offset="1" stop-color="#b07f45"/></radialGradient>
          <radialGradient id="${id}h" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#140d07"/><stop offset=".7" stop-color="#36261a"/><stop offset="1" stop-color="#8a6136"/></radialGradient>
        </defs>
        <ellipse cx="${X0}" cy="32" rx="10.5" ry="30" fill="#5e4126"/>
        <ellipse cx="${X0 + 1.2}" cy="32" rx="9.4" ry="28.6" fill="url(#${id}b)"/>
        <rect x="${X0}" y="${32 - core}" width="${X1 - X0}" height="${2 * core}" fill="#8f653c"/>
        <rect x="${X0}" y="${32 - core}" width="${X1 - X0}" height="${2 * core}" fill="url(#${id}s)"/>
        ${f > 0.02 ? `<rect x="${X0}" y="${y0.toFixed(1)}" width="${X1 - X0}" height="${(2 * r).toFixed(1)}" fill="${c}"/>
          <g stroke="${lum > .5 ? "#000" : "#fff"}" stroke-opacity="${lum > .5 ? .09 : .07}" stroke-width=".6">${lines}</g>
          <rect x="${X0}" y="${y0.toFixed(1)}" width="${X1 - X0}" height="${(2 * r).toFixed(1)}" fill="url(#${id}s)"/>` : ""}
        <ellipse cx="${X1 + 1.6}" cy="32" rx="10.5" ry="30" fill="#7c5530"/>
        <ellipse cx="${X1}" cy="32" rx="10" ry="29.4" fill="url(#${id}r)"/>
        <ellipse cx="${X1}" cy="32" rx="7.2" ry="21.5" fill="none" stroke="#9c6f3c" stroke-opacity=".45" stroke-width=".8"/>
        <ellipse cx="${X1}" cy="32" rx="3.6" ry="8.6" fill="url(#${id}h)"/>
        <ellipse cx="${X1 - 3.4}" cy="18" rx="1.8" ry="6.5" fill="#fff" opacity=".2"/></svg>`;
    },
    esc: s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])),
    eur: v => v == null ? "–" : Number(v).toLocaleString("de-DE", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " €",
    kwh: v => v == null ? "–" : Number(v).toLocaleString("de-DE", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " kWh",
    g: v => v == null ? "–" : (v >= 1000 ? (v / 1000).toLocaleString("de-DE", {maximumFractionDigits: 2}) + " kg" : Math.round(v).toLocaleString("de-DE") + " g"),
    dur(sec) {
      if (sec == null) return "–";
      const m = Math.round(sec / 60), h = Math.floor(m / 60);
      return h ? `${h} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;
    },
    setTheme(t) {
      document.documentElement.dataset.theme = t || "dark";
      try { localStorage.setItem("ph-theme", t || "dark"); } catch {}
    },
    toast(msg, bad) {
      let el = document.getElementById("ph-toast");
      if (!el) { el = document.createElement("div"); el.id = "ph-toast"; el.className = "toast"; document.body.appendChild(el); }
      el.textContent = msg; el.classList.toggle("bad", !!bad); el.classList.add("show");
      clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 2600);
    },
  };
  // aktive Seite markieren
  document.addEventListener("DOMContentLoaded", () => {
    const path = location.pathname.replace(/\/$/, "") || "/";
    document.querySelectorAll(".nav a").forEach(a => {
      const h = a.getAttribute("href").replace(/\/$/, "") || "/";
      if (h === path) a.classList.add("on");
    });
  });
  // ---------- Fortschritt im Browser-Tab (Titel + Symbol) ----------
  const baseTitle = document.title;
  let iconEl = null, lastSig = "";
  function iconLink() {
    if (!iconEl) iconEl = document.querySelector('link[rel="icon"]');
    if (!iconEl) { iconEl = document.createElement("link"); iconEl.rel = "icon"; document.head.appendChild(iconEl); }
    return iconEl;
  }
  function ringIcon(pct, paused) {
    const c = document.createElement("canvas"); c.width = c.height = 64;
    const x = c.getContext("2d");
    x.fillStyle = "#0b1220"; x.beginPath(); x.roundRect ? x.roundRect(0, 0, 64, 64, 14) : x.rect(0, 0, 64, 64); x.fill();
    x.lineWidth = 8; x.lineCap = "round";
    x.strokeStyle = "#22324d"; x.beginPath(); x.arc(32, 32, 24, 0, Math.PI * 2); x.stroke();
    x.strokeStyle = paused ? "#f59e0b" : "#22d3ee"; x.beginPath();
    x.arc(32, 32, 24, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.max(0.02, Math.min(1, pct / 100))); x.stroke();
    x.fillStyle = "#e6edf7"; x.font = "bold 22px system-ui, sans-serif"; x.textAlign = "center"; x.textBaseline = "middle";
    x.fillText(paused ? "II" : String(Math.round(pct)), 32, 34);
    return c.toDataURL("image/png");
  }
  async function tabTick() {
    if (location.pathname.startsWith("/login")) return;   // läuft auch im Hintergrund – dafür ist sie da
    let d;
    try { d = await (await fetch("/api/progress")).json(); } catch { return; }
    const list = d && d.enabled ? (d.printing || []) : [];
    const sig = JSON.stringify(list.map(p => [Math.round(p.progress), p.paused, p.stage]));
    if (sig === lastSig) return;
    lastSig = sig;
    if (!list.length) { document.title = baseTitle; iconLink().href = "/static/icon.svg"; return; }
    // vor dem eigentlichen Druck (0 %) den Arbeitsschritt zeigen, z. B. „🔥 Heizt“
    document.title = list.map(p => p.stage && (p.progress < 1 || p.paused) ? p.stage.trim()
      : (p.paused ? "⏸ " : "") + Math.round(p.progress) + " %").join(" · ") + " · " + baseTitle;
    const lead = list.reduce((a, b) => (b.remaining_min ?? 1e9) < (a.remaining_min ?? 1e9) ? b : a);
    iconLink().href = ringIcon(lead.progress, lead.paused);
  }
  PH.tabTick = tabTick;

  // ---------- Datensparen (pro Gerät) ----------
  // Modus: "off" | "cell" (nur im Mobilfunk) | "on". Mobilfunk erkennt Chrome/Vivaldi am Handy über navigator.connection.
  PH.saverMode = () => { try { return localStorage.getItem("ph-saver") || "off"; } catch { return "off"; } };
  PH.cellular = () => { const c = navigator.connection; return !!(c && (c.type === "cellular" || c.saveData)); };
  PH.saving = () => { const m = PH.saverMode(); return m === "on" || (m === "cell" && PH.cellular()); };
  document.addEventListener("DOMContentLoaded", () => { tabTick(); setInterval(tabTick, 30000); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { lastSig = ""; tabTick(); } });

  // ---------- als App (PWA) ----------
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1"))
    navigator.serviceWorker.register("/sw.js").catch(() => {});

  // Theme vom Server holen (gilt dann auf allen Geräten)
  fetch("/api/settings").then(r => r.json()).then(d => PH.setTheme(d.settings.theme)).catch(() => {});
})();

// Handy: beim Zoomen (zwei Finger) die feste Leiste unten ausblenden, sonst wird sie mitvergrößert
(() => {
  const vv = window.visualViewport; if (!vv) return;
  const f = () => document.body && document.body.classList.toggle("zoomed", vv.scale > 1.05);
  vv.addEventListener("resize", f); f();
})();

// Handy: Manche Browser (z. B. Vivaldi) blenden beim Scrollen ihre Leisten aus und richten „fest unten“ dabei
// unterschiedlich aus. Statt zu schätzen: messen, wo die Leiste wirklich steht, und nur die Differenz zum sichtbaren
// unteren Rand ausgleichen – so kann sie weder in der Mitte hängen noch aus dem Bild rutschen.
(() => {
  const vv = window.visualViewport; if (!vv) return;
  let cur = 0, timer = 0, touching = false;
  const navEl = () => document.querySelector("header .nav");
  const clear = () => { const nav = navEl(); if (nav && cur) { nav.style.transform = ""; cur = 0; } };
  // Nur im Ruhezustand korrigieren: Während Scrollen/Zoomen positioniert der Browser die Leiste selbst (Chrome macht das
  // richtig – eine Korrektur mit Zwischenwerten hat sie dort erst verschoben). Bleibt danach eine Lücke (Vivaldi), schließen.
  const fix = () => {
    const nav = navEl();
    if (!nav || touching) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
    if (getComputedStyle(nav).position !== "fixed" || vv.scale > 1.02 || typing) { clear(); return; }
    nav.style.transform = "";
    const d = Math.round(vv.offsetTop + vv.height - nav.getBoundingClientRect().bottom);
    cur = Math.abs(d) <= 2 || Math.abs(d) > 260 ? 0 : d;
    nav.style.transform = cur ? `translateY(${cur}px)` : "";
  };
  const moving = () => { clear(); clearTimeout(timer); timer = setTimeout(fix, 450); };
  vv.addEventListener("resize", moving); vv.addEventListener("scroll", moving);
  addEventListener("scroll", moving, {passive: true}); addEventListener("resize", moving);
  addEventListener("touchstart", () => { touching = true; }, {passive: true});
  addEventListener("touchend", () => { touching = false; moving(); }, {passive: true});
  addEventListener("touchcancel", () => { touching = false; moving(); }, {passive: true});
  setTimeout(fix, 400);
})();

// Handy „Zurück“ (Android-Taste/Geste) schließt ein offenes Fenster, statt die Seite zu verlassen.
// Jedes geöffnete Fenster (.modal.open, Kamera-Großansicht #lightbox.open) bekommt einen Verlaufs-Eintrag;
// „Zurück“ verbraucht ihn und schließt das oberste Fenster über seinen eigenen Abbrechen/Schließen-Knopf.
(() => {
  const SEL = ".modal.open, #lightbox.open";
  let depth = 0, skip = 0;
  const openOnes = () => [...document.querySelectorAll(SEL)];
  const closeTop = el => {
    const bs = [...el.querySelectorAll(".mact button, button")];
    const btn = bs.find(b => /^Später weiter$/.test(b.textContent.trim())) ||          // Stapel: Zurück = behalten, nicht verwerfen
                bs.find(b => /^(Abbrechen|Schließen|Schliessen|✕|×)$/.test(b.textContent.trim()));
    if (btn) btn.click(); else el.classList.remove("open");
    if (el.classList.contains("open")) el.classList.remove("open");
  };
  const sync = () => {
    const n = openOnes().length;
    while (depth < n) { history.pushState({phModal: depth + 1}, ""); depth++; }
    if (depth > n) { const k = depth - n; depth = n; skip += k; history.go(-k); }    // per Knopf geschlossen → Einträge wieder abbauen
  };
  addEventListener("popstate", () => {
    if (skip) { skip--; return; }
    const o = openOnes();
    if (!o.length) return;
    depth = Math.max(0, depth - 1);
    const z = el => parseInt(getComputedStyle(el).zIndex) || 0;
    closeTop(o.reduce((a, b) => (z(b) >= z(a) ? b : a)));          // oberstes Fenster (z. B. Pipette über „Neue Sorte“)
    setTimeout(() => { const n = openOnes().length; if (n < depth) { const k = depth - n; depth = n; skip += k; history.go(-k); } }, 0);
  });
  let queued = false;
  new MutationObserver(() => { if (!queued) { queued = true; setTimeout(() => { queued = false; sync(); }, 0); } }).observe(document.documentElement, {subtree: true, attributes: true, attributeFilter: ["class"], childList: true});
})();

// Handy: Fenster am Griff (oberer Rand) nach unten wischen = schließen (wie Zurück-Taste)
(function () {
  let y0 = null, box = null;
  document.addEventListener("touchstart", e => {
    const b = e.target.closest && e.target.closest(".modal.open .mbox");
    if (!b || innerWidth > 700) return;
    const t = e.touches[0], top = b.getBoundingClientRect().top;
    if (t.clientY - top > 36 || b.scrollTop > 0) return;       // nur am Griff bzw. ganz oben
    y0 = t.clientY; box = b;
  }, {passive: true});
  document.addEventListener("touchmove", e => {
    if (y0 == null) return;
    const d = Math.max(0, e.touches[0].clientY - y0);
    box.style.transform = `translateY(${d}px)`; box.style.transition = "none";
  }, {passive: true});
  document.addEventListener("touchend", e => {
    if (y0 == null) return;
    const d = (e.changedTouches[0].clientY - y0), b = box; y0 = null; box = null;
    b.style.transition = "transform .18s"; b.style.transform = "";
    if (d > 90) {
      const btn = [...b.querySelectorAll(".mact button, button")].find(x => /^(Später weiter)$/.test(x.textContent.trim())) ||
                  [...b.querySelectorAll(".mact button, button")].find(x => /^(Abbrechen|Schließen|Schliessen|✕|×)$/.test(x.textContent.trim()));
      if (btn) btn.click();
    }
  });
})();

