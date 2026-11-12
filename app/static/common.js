// Gemeinsam für alle PrintHub-Seiten: Theme, Navigation, Formatierung, Hinweise
(function () {
  window.PH = {
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
  let cur = 0, raf = 0;
  const fix = () => {
    raf = 0;
    const nav = document.querySelector("header .nav");
    if (!nav || getComputedStyle(nav).position !== "fixed") { if (nav && cur) { nav.style.transform = ""; cur = 0; } return; }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
    if (vv.scale > 1.05 || typing) { if (cur) { nav.style.transform = ""; cur = 0; } return; }   // Zoom/Tastatur: nicht schieben
    const bottom = nav.getBoundingClientRect().bottom - cur;           // wo sie ohne Korrektur stünde
    const want = vv.offsetTop + vv.height;                              // sichtbarer unterer Rand
    let d = Math.round(want - bottom);
    if (Math.abs(d) <= 1 || Math.abs(d) > 400) d = 0;                    // nichts zu tun bzw. Unsinn (Tastatur) → nicht verschieben
    if (d !== cur) { cur = d; nav.style.transform = d ? `translateY(${d}px)` : ""; }
  };
  const later = () => { if (!raf) raf = requestAnimationFrame(fix); };
  vv.addEventListener("resize", later); vv.addEventListener("scroll", later);
  addEventListener("scroll", later, {passive: true}); addEventListener("resize", later);
  setTimeout(fix, 300); setInterval(fix, 1500);
})();

// Handy „Zurück“ (Android-Taste/Geste) schließt ein offenes Fenster, statt die Seite zu verlassen.
// Jedes geöffnete Fenster (.modal.open, Kamera-Großansicht #lightbox.open) bekommt einen Verlaufs-Eintrag;
// „Zurück“ verbraucht ihn und schließt das oberste Fenster über seinen eigenen Abbrechen/Schließen-Knopf.
(() => {
  const SEL = ".modal.open, #lightbox.open";
  let depth = 0, skip = 0;
  const openOnes = () => [...document.querySelectorAll(SEL)];
  const closeTop = el => {
    const btn = [...el.querySelectorAll(".mact button, button")].find(b => /^(Abbrechen|Schließen|Schliessen|✕|×)$/.test(b.textContent.trim()));
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
