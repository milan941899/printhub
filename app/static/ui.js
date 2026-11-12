// PrintHub: interaktive Diagramme (Tooltip, Fadenkreuz, Klick) und Layout (Drag & Drop, Größe ziehen)
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  // ---------------- Tooltip ----------------
  // Elemente mit data-tip='{"t":"Titel","r":[[farbe,label,wert],…],"h":"/verlauf?…"}' zeigen eine Info-Box.
  // data-x / data-ys (SVG-Koordinaten) bewegen ein Fadenkreuz (<g class="xh">) im selben SVG.
  const tip = document.createElement("div");
  tip.className = "ph-tip";
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tip));
  let cur = null, touch = false;

  function tipAttr(title, rows, href) {
    return ` data-tip="${esc(JSON.stringify({t: title, r: rows || [], h: href || ""}))}"`;
  }
  function show(el, x, y) {
    let d;
    try { d = JSON.parse(el.dataset.tip); } catch { return; }
    tip.innerHTML = `<b>${esc(d.t)}</b>` + d.r.map(r => `<div class="tr"><i style="background:${r[0] || "transparent"}"></i><span>${esc(r[1])}</span><b>${esc(r[2])}</b></div>`).join("")
      + (d.h ? (touch ? `<a class="tl" href="${esc(d.h)}">Im Verlauf öffnen →</a>` : `<div class="th">Klicken: im Verlauf öffnen</div>`) : "");
    tip.classList.add("on");
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let left = x + 14, top = y - h - 12;
    if (left + w > innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 16;
    tip.style.left = Math.max(8, left) + "px"; tip.style.top = top + "px";
    // Fadenkreuz
    const svg = el.ownerSVGElement;
    const xh = svg && svg.querySelector("g.xh");
    if (xh && el.dataset.x) {
      xh.style.display = "";
      const line = xh.querySelector("line");
      line.setAttribute("x1", el.dataset.x); line.setAttribute("x2", el.dataset.x);
      const ys = (el.dataset.ys || "").split(";").filter(Boolean);
      const dots = xh.querySelectorAll("circle");
      dots.forEach((c, i) => {
        if (i < ys.length) { const [yv, col] = ys[i].split("|"); c.setAttribute("cx", el.dataset.x); c.setAttribute("cy", yv); c.setAttribute("stroke", col); c.style.display = ""; }
        else c.style.display = "none";
      });
    }
    if (cur && cur !== el) cur.classList.remove("hov");
    cur = el; el.classList.add("hov");
  }
  function hide() {
    tip.classList.remove("on");
    if (cur) {
      cur.classList.remove("hov");
      const xh = cur.ownerSVGElement && cur.ownerSVGElement.querySelector("g.xh");
      if (xh) xh.style.display = "none";
    }
    cur = null;
  }
  document.addEventListener("pointermove", e => {
    if (e.pointerType === "touch") return;
    touch = false;
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) show(el, e.clientX, e.clientY); else if (cur) hide();
  });
  // Touch: Info erst beim Antippen (Finger ohne Wischen wieder weg) – Wischen über ein Diagramm scrollt nur die Seite
  let tStart = null;
  document.addEventListener("pointerdown", e => {
    if (e.pointerType !== "touch") return;
    touch = true;
    tStart = {x: e.clientX, y: e.clientY, t: Date.now()};
  });
  document.addEventListener("pointercancel", () => { tStart = null; });      // Browser hat aufs Scrollen umgeschaltet
  document.addEventListener("pointerup", e => {
    if (e.pointerType !== "touch" || !tStart) return;
    const moved = Math.hypot(e.clientX - tStart.x, e.clientY - tStart.y), long = Date.now() - tStart.t;
    tStart = null;
    if (moved > 10 || long > 600) return;
    if (e.target.closest(".ph-tip")) return;
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) show(el, e.clientX, e.clientY); else hide();
  });
  document.addEventListener("click", e => {
    if (touch) return;
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (!el) return;
    try { const d = JSON.parse(el.dataset.tip); if (d.h) location.href = d.h; } catch {}
  });
  addEventListener("scroll", () => hide(), {passive: true});

  // ---------------- Drag & Drop (Maus und Touch) ----------------
  // Delegiert: funktioniert auch, wenn der Inhalt neu gezeichnet wird.
  // root: Bereich, in dem gezogen wird; items: Selektor; handle: Griff; onDrop(keys, container)
  const UI = {busy: false};
  function sortable(root, {items, handle, key, onDrop, axis}) {
    root.addEventListener("pointerdown", e => {
      const h = e.target.closest(handle);
      if (!h || !root.contains(h)) return;
      const item = h.closest(items);
      if (!item) return;
      const container = item.parentNode;
      if (container.classList.contains("ff")) return;      // freier Modus: freeform() übernimmt
      e.preventDefault(); e.stopPropagation();
      UI.busy = true;
      const r = item.getBoundingClientRect();
      const ghost = item.cloneNode(true);
      ghost.classList.add("dnd-ghost");
      Object.assign(ghost.style, {width: r.width + "px", height: Math.min(r.height, 220) + "px", left: r.left + "px", top: r.top + "px"});
      document.body.appendChild(ghost);
      const ox = e.clientX - r.left, oy = e.clientY - r.top;
      item.classList.add("dnd-src");
      let target = null, after = false;
      const clear = () => container.querySelectorAll(".dnd-before,.dnd-after").forEach(x => x.classList.remove("dnd-before", "dnd-after"));
      const move = ev => {
        ghost.style.left = (ev.clientX - ox) + "px"; ghost.style.top = (ev.clientY - oy) + "px";
        ghost.style.display = "none";
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        ghost.style.display = "";
        let t = under && under.closest(items);
        while (t && t.parentNode !== container) t = t.parentNode.closest && t.parentNode.closest(items);
        clear();
        if (t && t !== item) {
          const tr = t.getBoundingClientRect();
          after = axis === "y" ? ev.clientY > tr.top + tr.height / 2
            : (ev.clientX > tr.left + tr.width / 2 || ev.clientY > tr.bottom - 10);
          t.classList.add(after ? "dnd-after" : "dnd-before");
          target = t;
        } else target = null;
        if (ev.clientY < 60) scrollBy(0, -14); else if (ev.clientY > innerHeight - 60) scrollBy(0, 14);
      };
      const up = () => {
        removeEventListener("pointermove", move); removeEventListener("pointerup", up); removeEventListener("pointercancel", up);
        ghost.remove(); item.classList.remove("dnd-src"); clear();
        UI.busy = false;
        if (target && target.isConnected) {
          if (after) target.after(item); else target.before(item);
          onDrop([...container.children].filter(x => x.matches(items)).map(x => x.dataset[key]), container);
        }
      };
      addEventListener("pointermove", move); addEventListener("pointerup", up); addEventListener("pointercancel", up);
    });
  }

  // ---------------- Größe ziehen (delegiert) ----------------
  // handle: Griff im Element; item: Selektor des Elements; cols(): Spalten im Raster (0 = Breite fest);
  // heightEl: Selektor des Teils, dessen Höhe gezogen wird. onResize(item, {span, height, dh})
  function resizable(root, {handle, item: itemSel, cols = () => 0, heightEl, minH = 80, maxH = 900, onResize}) {
    root.addEventListener("pointerdown", e => {
      const h = e.target.closest(handle);
      if (!h || !root.contains(h)) return;
      const item = h.closest(itemSel);
      if (!item || item.parentNode.classList.contains("ff")) return;
      e.preventDefault(); e.stopPropagation();
      UI.busy = true;
      const nCols = cols(item);
      const grid = item.parentNode, gr = grid.getBoundingClientRect();
      const gap = parseFloat(getComputedStyle(grid).columnGap) || 0;
      const colW = nCols ? (gr.width - gap * (nCols - 1)) / nCols : 0;
      const ir = item.getBoundingClientRect();
      const hel = heightEl ? (h.closest(heightEl) || item.querySelector(heightEl)) : null;
      const h0 = hel ? hel.getBoundingClientRect().height : 0;
      const sx = e.clientX, sy = e.clientY;
      let span = null, height = null;
      item.classList.add("rsz");
      const move = ev => {
        if (nCols) {
          const w = ir.width + ev.clientX - sx;
          span = Math.max(1, Math.min(nCols, Math.round((w + gap) / (colW + gap))));
          item.style.gridColumn = `span ${span}`;
        }
        if (hel) {
          height = Math.max(minH, Math.min(maxH, Math.round((h0 + ev.clientY - sy) / 10) * 10));
          hel.style.height = height + "px"; hel.style.overflow = "hidden";
        }
      };
      const up = () => {
        removeEventListener("pointermove", move); removeEventListener("pointerup", up); removeEventListener("pointercancel", up);
        item.classList.remove("rsz");
        UI.busy = false;
        onResize(item, {span, height, dh: height != null ? Math.round(height - h0) : 0});
      };
      addEventListener("pointermove", move); addEventListener("pointerup", up); addEventListener("pointercancel", up);
    });
  }

  // ---------------- Freie Anordnung (wie in einem Zeichenprogramm) ----------------
  // Positionen: x und w als Anteil der Containerbreite (0..1), y und h in Pixeln.
  // Beim Ziehen und Größe ändern rasten Kanten und Größen an Nachbarn ein (Alt gedrückt = ohne Einrasten).
  // Überlappungen löst die Anzeige auf, indem sie Bausteine nach unten schiebt.
  const SNAP = 8, GAP = 12;
  function freeLayout(cont, items, autoH) {
    // items: [{el, pos:{x,y,w,h}}] -> Anzeige-Positionen ohne Überlappung
    const cw = cont.clientWidth;
    const rects = items.map(it => ({it, x: Math.max(0, Math.min(1, it.pos.x)) * cw, w: Math.min(1, it.pos.w) * cw, y: it.pos.y, h: it.pos.h}));
    rects.sort((a, b) => a.y - b.y || a.x - b.x);
    const placed = [];
    for (const r of rects) {
      let y = r.y, moved = true;
      while (moved) {
        moved = false;
        for (const p of placed) {
          const ph = p.hh;
          if (r.x < p.x + p.w - 1 && r.x + r.w > p.x + 1 && y < p.y + ph + GAP - 1 && y + r.h > p.y + 1) {
            y = p.y + ph + GAP; moved = true;
          }
        }
      }
      r.y = y;
      Object.assign(r.it.el.style, {left: r.x + "px", top: y + "px", width: r.w + "px"});
      if (!(autoH && autoH(r.it.el))) r.it.el.style.height = r.h + "px"; else r.it.el.style.height = "";
      r.hh = autoH && autoH(r.it.el) ? r.it.el.offsetHeight : r.h;   // tatsächliche Höhe (nach Breite)
      placed.push(r);
    }
    // Höhe autoH-Elemente nachmessen
    cont.style.height = Math.max(0, ...rects.map(r => r.y + r.hh)) + "px";
  }
  function guides(cont) {
    let g = cont.querySelector(":scope > .ff-guides");
    if (!g) { g = document.createElement("div"); g.className = "ff-guides"; cont.appendChild(g); }
    return g;
  }
  // root: Bereich; items: Selektor der Bausteine; handle/resize: Griffe; get(el)->pos; set(el,pos)
  function freeform(root, {items, handle, resize, container, get, set, minW = 80, minH = 50, heightOf}) {
    root.addEventListener("pointerdown", e => {
      const hMove = e.target.closest(handle), hRes = resize && e.target.closest(resize);
      const h = hMove || hRes;
      if (!h || !root.contains(h)) return;
      const el = h.closest(items);
      if (!el) return;
      const cont = el.parentNode;
      if (!cont.classList.contains("ff")) return;          // nur im freien Modus (sonst Raster-Variante)
      e.preventDefault(); e.stopPropagation();
      UI.busy = true;
      const cw = cont.clientWidth, cr = cont.getBoundingClientRect();
      const r0 = {x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight};
      const others = [...cont.children].filter(x => x !== el && x.matches(items)).map(x => ({x: x.offsetLeft, y: x.offsetTop, w: x.offsetWidth, h: x.offsetHeight}));
      const vx = [0, cw, ...others.flatMap(o => [o.x, o.x + o.w])], vy = [0, ...others.flatMap(o => [o.y, o.y + o.h, o.y + o.h + GAP, o.y - GAP])];
      const hx = [...others.flatMap(o => [o.x - GAP, o.x + o.w + GAP])];
      const widths = others.map(o => o.w), heights = others.map(o => o.h);
      const gl = guides(cont);
      const sx = e.clientX, sy = e.clientY;
      let cur = {...r0};
      el.classList.add("ff-active");
      const near = (v, list) => { let best = null; for (const c of list) if (Math.abs(c - v) <= SNAP && (best == null || Math.abs(c - v) < Math.abs(best - v))) best = c; return best; };
      const move = ev => {
        const dx = ev.clientX - sx, dy = ev.clientY - sy, free = ev.altKey;
        const lines = [];
        if (hMove) {
          let x = r0.x + dx, y = Math.max(0, r0.y + dy);
          if (!free) {
            const cands = [[x, 0], [x + r0.w, r0.w]];
            for (const [edge, off] of cands) { const n = near(edge, [...vx, ...hx]); if (n != null) { x = n - off; lines.push(["v", n]); break; } }
            for (const [edge, off] of [[y, 0], [y + r0.h, r0.h]]) { const n = near(edge, vy); if (n != null) { y = n - off; lines.push(["h", n]); break; } }
          }
          x = Math.max(0, Math.min(cw - r0.w, x));
          cur = {...r0, x, y};
        } else {
          let w = Math.max(minW, r0.w + dx), hh = Math.max(minH, r0.h + dy);
          if (!free) {
            let n = near(r0.x + w, [...vx, ...hx]); if (n != null) { w = n - r0.x; lines.push(["v", n]); }
            else { n = near(w, widths); if (n != null) { w = n; lines.push(["w", n]); } }
            n = near(r0.y + hh, vy); if (n != null) { hh = n - r0.y; lines.push(["h", n]); }
            else { n = near(hh, heights); if (n != null) { hh = n; lines.push(["hh", n]); } }
          }
          w = Math.min(cw - r0.x, w);
          cur = {...r0, w, h: hh};
        }
        Object.assign(el.style, {left: cur.x + "px", top: cur.y + "px", width: cur.w + "px", height: cur.h + "px"});
        cont.style.height = Math.max(cont.offsetHeight, cur.y + cur.h) + "px";
        gl.innerHTML = lines.map(([t, v]) => t === "v" ? `<i class="gv" style="left:${v}px"></i>` : t === "h" ? `<i class="gh" style="top:${v}px"></i>`
          : `<i class="gs">${t === "w" ? "gleiche Breite" : "gleiche Höhe"}</i>`).join("");
        if (ev.clientY < 60) scrollBy(0, -14); else if (ev.clientY > innerHeight - 60) scrollBy(0, 14);
      };
      const up = () => {
        removeEventListener("pointermove", move); removeEventListener("pointerup", up); removeEventListener("pointercancel", up);
        el.classList.remove("ff-active"); gl.innerHTML = "";
        UI.busy = false;
        set(el, {x: cur.x / cw, y: Math.round(cur.y), w: cur.w / cw, h: Math.round(cur.h)}, !!hRes);
      };
      addEventListener("pointermove", move); addEventListener("pointerup", up); addEventListener("pointercancel", up);
    });
  }
  // Raster-Positionen einmal messen (Startwert für den freien Modus)
  function measure(cont, items) {
    const cw = cont.clientWidth;
    const out = {};
    for (const el of cont.children) {
      if (!el.matches(items)) continue;
      out[el.dataset.b] = {x: el.offsetLeft / cw, y: el.offsetTop, w: el.offsetWidth / cw, h: el.offsetHeight};
    }
    return out;
  }

  window.PHUI = Object.assign(UI, {tipAttr, hideTip: hide, sortable, resizable, esc, freeform, freeLayout, measure});
})();
