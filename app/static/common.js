// Gemeinsam für alle PrintHub-Seiten: Theme, Navigation, Formatierung, Hinweise
(function () {
  window.PH = {
    // Inhalt eines Elements auf neues HTML bringen, ohne alles neu zu bauen: gleiche Knoten bleiben erhalten,
    // nur geänderte Attribute/Texte werden angepasst (Animationen wie die Lüfter laufen dadurch ungestört weiter)
    morph(el, html) {
      const t = document.createElement(el.tagName === "svg" ? "div" : el.tagName);
      t.innerHTML = html;
      const sync = (a, b) => {
        if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) { a.replaceWith(b); return; }
        if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
        for (const at of [...a.attributes]) if (!b.hasAttribute(at.name)) a.removeAttribute(at.name);
        for (const at of b.attributes) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
        kids(a, b);
      };
      const kids = (a, b) => {
        const A = [...a.childNodes], B = [...b.childNodes];
        for (let i = 0; i < B.length; i++) { if (i < A.length) sync(A[i], B[i]); else a.appendChild(B[i]); }
        for (let i = B.length; i < A.length; i++) A[i].remove();
      };
      kids(el, t);
    },
    // Gemeinsame Zeitraum-Leiste für Verlauf und Statistik
    RANGES: [["cweek", "Diese Woche"], ["week", "7 Tage"], ["cmonth", "Dieser Monat"], ["lmonth", "Letzter Monat"], ["month", "30 Tage"],
             ["cyear", "Dieses Jahr"], ["year", "12 Monate"], ["all", "Gesamt"], ["custom", "📅 Zeitraum"]],
    // Zeitraum → {from, to} (Unix-Sekunden, to exklusiv); week/month/year rollierend, Wochenstart Mo oder So
    rangeTs(v, from, to) {
      const now = new Date(), ts = d => Math.floor(d.getTime() / 1000), day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const ws = PH.pref("week_start", "mo") === "so" ? 0 : 1;
      if (v === "cweek") { const d = new Date(day0); d.setDate(d.getDate() - ((d.getDay() - ws + 7) % 7)); return {from: ts(d)}; }
      if (v === "week") return {from: Math.floor(Date.now() / 1000) - 7 * 86400};
      if (v === "month") return {from: Math.floor(Date.now() / 1000) - 30 * 86400};
      if (v === "year") return {from: Math.floor(Date.now() / 1000) - 365 * 86400};
      if (v === "cmonth") return {from: ts(new Date(now.getFullYear(), now.getMonth(), 1))};
      if (v === "lmonth") return {from: ts(new Date(now.getFullYear(), now.getMonth() - 1, 1)), to: ts(new Date(now.getFullYear(), now.getMonth(), 1))};
      if (v === "cyear") return {from: ts(new Date(now.getFullYear(), 0, 1))};
      if (v === "custom") {
        const r = {};
        if (from) r.from = ts(new Date(from + "T00:00"));
        if (to) r.to = ts(new Date(to + "T00:00")) + 86400;
        return r;
      }
      return {};
    },
    // Leiste zeichnen: el = Container, st = {v, from, to}, onChange(st)
    rangeBar(el, st, onChange) {
      const draw = () => {
        el.innerHTML = `<div class="seg rbar">${PH.RANGES.map(([v, l]) => `<button type="button" data-v="${v}" class="${st.v === v ? "on" : ""}">${l}</button>`).join("")}</div>
          <div class="rcustom" ${st.v === "custom" ? "" : "hidden"}><label>Von <input type="date" data-k="from" value="${st.from || ""}"></label>
          <label>Bis <input type="date" data-k="to" value="${st.to || ""}"></label></div>`;
        el.querySelectorAll(".rbar button").forEach(b => b.onclick = () => { st.v = b.dataset.v; draw(); if (st.v !== "custom" || st.from) onChange(st); });
        el.querySelectorAll(".rcustom input").forEach(i => i.onchange = () => { st[i.dataset.k] = i.value; onChange(st); });
        const on = el.querySelector(".rbar .on"); if (on) on.scrollIntoView({block: "nearest", inline: "center"});
      };
      draw();
    },
    // Einstellungen, die nur dieses Gerät betreffen (localStorage „ph-pref“)
    pref(key, def, val) {
      let all = {};
      try { all = JSON.parse(localStorage.getItem("ph-pref") || "{}") || {}; } catch {}
      if (val !== undefined) { all[key] = val; try { localStorage.setItem("ph-pref", JSON.stringify(all)); } catch {} return val; }
      return key in all ? all[key] : def;
    },
    // Namen der wählbaren Übersichts-Kacheln (für die Einstellungen)
    TILE_NAMES: {power: "Leistung", job_energy: "Strom Druck", job_filament: "Filament Druck", job_cost: "Kosten Druck",
      today_energy: "Strom heute", spools: "Spulen zugewiesen", wifi: "WLAN", prints_today: "Drucke heute", fil_today: "Filament heute",
      hours_week: "Druckstunden Woche", success: "Erfolgsrate (30 Tage)", next_job: "Nächster Auftrag", stock: "Lager (bald leer / nachkaufen)",
      dry: "Trocknen fällig", maint: "Wartung fällig", guard: "Druckwächter"},
    TILE_DEF: {print: ["power", "job_energy", "job_filament", "job_cost"], idle: ["power", "today_energy", "spools", "wifi"]},
    // Spulen-Darstellung pro Gerät (localStorage „ph-spool“): Stil, Blickwinkel, Deckkraft der vorderen Scheibe
    SPOOL_DIM: {"c": 202, "d": 174, "e": 142, "f": 104},          // Bildbreite je Blickwinkel (Höhe immer 212)
    spoolPref(v) {
      if (v) { try { localStorage.setItem("ph-spool", JSON.stringify(v)); } catch {} return v; }
      let p = {};
      try { p = JSON.parse(localStorage.getItem("ph-spool") || "{}") || {}; } catch {}
      return {s: ["sunlu", "karton", "bambu", "flat", "dot"].includes(p.s) ? p.s : "sunlu",
              a: ["c", "d", "e", "f"].includes(p.a) ? p.a : "d", o: [1, 0.6, 0.3].includes(+p.o) ? +p.o : 1};
    },
    // Spule mit Restmenge (fill 0…1). 3D: hinten b.webp, Wicklung f{k}.webp (Graustufen, per Filter eingefärbt),
    // Glanz s{k}.webp, vordere Scheibe v.webp (Deckkraft einstellbar). pref = Vorschau mit anderen Einstellungen
    spool(color, fill = 1, h = 34, pref) {
      const c = /^#?[0-9a-f]{6}$/i.test(color || "") ? (color[0] === "#" ? color : "#" + color) : "#9aa4b2";
      const id = "sp" + (PH._sp = (PH._sp || 0) + 1), P = pref || PH.spoolPref();
      const f = Math.max(0, Math.min(1, +fill || 0)), k = f < 0.02 ? 0 : Math.max(1, Math.round(f * 10));
      if (P.s === "flat") {
        const L = 2 * Math.PI * 15;
        return `<svg class="spl flat" viewBox="0 0 40 40" width="${h}" height="${h}" aria-hidden="true">
          <circle cx="20" cy="20" r="15" fill="none" stroke="var(--trk, #ffffff22)" stroke-width="7"/>
          <circle cx="20" cy="20" r="15" fill="none" stroke="${c}" stroke-width="7" stroke-dasharray="${(L * f).toFixed(1)} ${L.toFixed(1)}" transform="rotate(-90 20 20)"/>
          <circle cx="20" cy="20" r="18.5" fill="none" stroke="#fff3" stroke-width=".8"/>
          <circle cx="20" cy="20" r="11" fill="${c}" stroke="#fff3" stroke-width=".8"/>
          <circle cx="20" cy="20" r="4.5" fill="var(--well, #0f172a)" stroke="#fff4" stroke-width="1"/></svg>`;
      }
      if (P.s === "dot") {
        return `<svg class="spl dot" viewBox="0 0 40 40" width="${h}" height="${h}" aria-hidden="true">
          <circle cx="20" cy="16" r="12" fill="${c}" stroke="#fff6" stroke-width="1.5"/>
          <rect x="6" y="33" width="28" height="4.5" rx="2.2" fill="var(--trk, #ffffff22)"/>
          <rect x="6" y="33" width="${(28 * f).toFixed(1)}" height="4.5" rx="2.2" fill="${c}" stroke="#fff3" stroke-width=".5"/></svg>`;
      }
      const set = `/static/spool/${P.s}-${P.a}/`, w = PH.SPOOL_DIM[P.a], v = "?v=3";
      return `<svg class="spl" viewBox="0 0 ${w} 212" width="${Math.round(h * w / 212)}" height="${h}" aria-hidden="true">
        <filter id="${id}" color-interpolation-filters="sRGB"><feFlood flood-color="${c}"/><feComposite in2="SourceGraphic" operator="arithmetic" k1="1"/></filter>
        <image href="${set}b.webp${v}" width="${w}" height="212"/>
        ${k ? `<image href="${set}f${k}.webp${v}" width="${w}" height="212" filter="url(#${id})"/><image href="${set}s${k}.webp${v}" width="${w}" height="212"/>` : ""}
        <image href="${set}v.webp${v}" width="${w}" height="212"${P.o < 1 ? ` opacity="${P.o}"` : ""}/></svg>`;
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
  // Einstellungen merken (PH.cfg) – Seiten können damit Funktionen ein-/ausblenden
  PH.cfg = {};
  PH.cfgReady = fetch("/api/settings").then(r => r.json()).then(d => { PH.cfg = d.settings || {}; PH.setTheme(PH.cfg.theme); return PH.cfg; }).catch(() => ({}));
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

