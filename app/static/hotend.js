// Düse/Hotend: vier Darstellungen, gemeinsam für Übersicht und Einstellungen (Vorschau)
const HX = {
  esc: v => PH.esc(v),
  num: (v, d = "–") => (v === null || v === undefined || v === "") ? d : v,
  deg: v => v == null ? "–" : `${Math.round(v)}°`,
  heat(t) { if (t == null || t < 45) return "#475569"; if (t < 120) return "#f59e0b"; if (t < 200) return "#f97316"; return "#ef4444"; },
  // Lüfter: Dauer je Umdrehung aus der Drehzahl; negative Verzögerung = Drehung läuft nach dem Neuzeichnen
  // (alle 3 s) nahtlos weiter, statt wieder bei 0° zu beginnen
  spin(pct) {
    if (!(pct > 0)) return "";
    const dur = +(0.28 + (1 - Math.min(100, pct) / 100) * 1.1).toFixed(2);
    return `animation-duration:${dur}s;animation-delay:-${((Date.now() / 1000) % dur).toFixed(3)}s`;
  },
  // Lüfterrad mit geschwungenen Flügeln (n Flügel, Nabe r0, außen r1) um (cx, cy)
  blades(cx, cy, n, r0, r1, fill, stroke) {
    const P = (r, a) => `${(cx + r * Math.cos(a)).toFixed(2)} ${(cy + r * Math.sin(a)).toFixed(2)}`;
    let d = "";
    for (let i = 0; i < n; i++) {
      const a = i * 2 * Math.PI / n, w = 2 * Math.PI / n * 0.62, sw = 0.75;   // Flügelbreite, Schwung
      d += `M${P(r0, a)} Q${P((r0 + r1) / 2, a + sw * 0.55)} ${P(r1, a + sw)} A${r1} ${r1} 0 0 1 ${P(r1, a + sw + w * 0.55)} Q${P((r0 + r1) / 2 + 1, a + w + sw * 0.4)} ${P(r0, a + w)} Z `;
    }
    return `<path d="${d}" fill="${fill}" stroke="${stroke}" stroke-width=".5" stroke-linejoin="round"/>`;
  },
  textOn(hex) {
    const h = (hex || "").replace("#", "");
    if (h.length < 6) return "#fff";
    const [r, g, b] = [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16));
    return (r * 0.299 + g * 0.587 + b * 0.114) > 150 ? "#0a1220" : "#fff";
  },
};
// Hotend-Darstellung pro Gerät: schema (Strichzeichnung) | real (schattiert) | gauge (Rundinstrumente) | compact (Liste)
function hotendBody(p, info, style) {
  const st = style || PH.pref("hotend", "schema");
  if (st === "compact") return hotendCompact(p, info);
  if (st === "gauge") return hotendGauge(p, info);
  if (st === "real") return hotendSvg(p, info, true);
  return hotendSvg(p, info);
}
function hotendSide(p) {   // die Werte links/rechts, die je nach Druckertyp wechseln
  return {
    leftMid: p.type === "moonraker" ? {l: "Fluss", v: p.flow != null ? p.flow : "–", u: p.flow != null ? "mm³/s" : ""}
                                    : {l: "Speed-Modus", v: HX.num(p.speed), u: p.speed_pct ? `${p.speed_pct} %` : ""},
    rightMid: p.type === "moonraker" ? {l: "Speed", v: HX.num(p.speed), u: p.speed_pct && p.speed_pct !== 100 ? `${p.speed_pct} %` : ""}
                                     : {l: "Hotend-Lüfter", v: (p.fans || [])[1] && p.fans[1].pct != null ? `${p.fans[1].pct} %` : "–", u: ""},
  };
}
// Kompakt: nur Werte in zwei Spalten – als SVG, damit Schrift und Größe genau wie bei den anderen Darstellungen skalieren
function hotendCompact(p, info) {
  const {nozzle, bed, color, material} = info, fan = (p.fans || [])[0], {leftMid, rightMid} = hotendSide(p);
  const items = [
    ["MATERIAL", HX.esc(material || "–"), "", "", color || "#64748b"],
    ["DÜSE", `<tspan style="fill:${nozzle && nozzle.actual >= 45 ? HX.heat(nozzle.actual) : "var(--fg)"}">${HX.deg(nozzle && nozzle.actual)}</tspan>`, "/ " + HX.deg(nozzle && nozzle.target)],
    ["BETT", HX.deg(bed && bed.actual), "/ " + HX.deg(bed && bed.target)],
    ["LÜFTER", fan && fan.pct != null ? fan.pct + " %" : "–", ""],
    [HX.esc(leftMid.l.toUpperCase()), HX.esc(leftMid.v), HX.esc(leftMid.u)],
    [HX.esc(rightMid.l.toUpperCase()), HX.esc(rightMid.v), HX.esc(rightMid.u)],
    ["LAYER", p.layer != null ? p.layer : "–", p.total_layers != null ? "/ " + p.total_layers : "", "acc"],
  ];
  const cell = ([l, v, u, cls, dot], i) => {
    const x = i % 2 ? 214 : 18, y = 26 + Math.floor(i / 2) * 70;
    return `${dot ? `<circle cx="${x + 5}" cy="${y + 20}" r="5.5" fill="${dot}" stroke="#fff6"/>` : ""}
      <text class="lbl" x="${x}" y="${y}">${l}</text>
      <text class="val ${cls || ""}" x="${dot ? x + 16 : x}" y="${y + 28}">${v} <tspan class="unit">${u}</tspan></text>
      <line x1="${x}" y1="${y + 40}" x2="${x + 168}" y2="${y + 40}" class="lead"/>`;
  };
  return `<svg viewBox="0 0 400 300" role="img" aria-label="Düse">${items.map(cell).join("")}</svg>`;
}
function hotendGauge(p, info) {
  const {nozzle, bed, color, material} = info, fan = (p.fans || [])[0], {leftMid, rightMid} = hotendSide(p);
  const g = (cx, cy, r, val, tgt, max, lbl, unit, col) => {
    const a0 = Math.PI * 0.75, a1 = Math.PI * 2.25, f = v => Math.max(0, Math.min(1, (v || 0) / max));
    const pt = t => [cx + r * Math.cos(a0 + (a1 - a0) * t), cy + r * Math.sin(a0 + (a1 - a0) * t)];
    const arc = t => { const [x0, y0] = pt(0), [x1, y1] = pt(t); return `M${x0.toFixed(1)} ${y0.toFixed(1)} A${r} ${r} 0 ${(a1 - a0) * t > Math.PI ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`; };
    const [tx, ty] = pt(f(tgt)), ticks = [0, .25, .5, .75, 1].map(t => { const [x, y] = pt(t), [x2, y2] = [cx + (r - 9) * Math.cos(a0 + (a1 - a0) * t), cy + (r - 9) * Math.sin(a0 + (a1 - a0) * t)];
      return `<line x1="${x.toFixed(1)}" y1="${y.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" class="gtick"/>`; }).join("");
    return `<path d="${arc(0.9999)}" class="gtrack" stroke-width="10" fill="none" stroke-linecap="round"/>
      ${val ? `<path d="${arc(Math.max(0.005, f(val)))}" stroke="${col}" stroke-width="10" fill="none" stroke-linecap="round"/>` : ""}
      ${ticks}${tgt ? `<circle cx="${tx.toFixed(1)}" cy="${ty.toFixed(1)}" r="4.5" fill="#fff" stroke="${col}" stroke-width="2"/>` : ""}
      <text x="${cx}" y="${cy + 6}" text-anchor="middle" class="gval">${val != null ? Math.round(val) : "–"}<tspan class="unit">${unit}</tspan></text>
      <text x="${cx}" y="${cy + 24}" text-anchor="middle" class="unit">${tgt ? "Ziel " + Math.round(tgt) + unit : "&#160;"}</text>
      <text x="${cx}" y="${cy + r + 14}" text-anchor="middle" class="lbl">${lbl}</text>`;
  };
  const stat = (x, l, v, u = "", cls = "") => `<text class="lbl" x="${x}" y="200" text-anchor="middle">${l}</text><text class="val ${cls}" x="${x}" y="228" text-anchor="middle">${v} <tspan class="unit">${u}</tspan></text>`;
  return `<svg viewBox="0 -26 400 300" role="img" aria-label="Düse">
    ${g(72, 96, 52, nozzle && nozzle.actual, nozzle && nozzle.target, 300, "DÜSE", "°", HX.heat(Math.max(nozzle && nozzle.actual || 0, 46)))}
    ${g(200, 96, 52, bed && bed.actual, bed && bed.target, 120, "BETT", "°", HX.heat(Math.max(bed && bed.actual || 0, 46)))}
    ${g(328, 96, 52, fan && fan.pct, null, 100, "LÜFTER", "%", "var(--acc)")}
    <text class="lbl" x="72" y="200" text-anchor="middle">MATERIAL</text>
    <circle cx="26" cy="196" r="5.5" fill="${color || "#64748b"}" stroke="#fff6"/>
    <text class="val" x="72" y="228" text-anchor="middle">${HX.esc(material || "–")}</text>
    ${stat(200, HX.esc(leftMid.l.toUpperCase()), HX.esc(leftMid.v), HX.esc(leftMid.u))}
    ${stat(328, "LAYER", p.layer != null ? p.layer : "–", p.total_layers != null ? "/ " + p.total_layers : "", "acc")}
  </svg>`;
}
function hotendSvg(p, info, real = false) {
  const {nozzle, bed, color, material, toolNo} = info;
  const c = color || "#64748b";
  const heat = HX.heat(nozzle && nozzle.actual);
  const heating = nozzle && nozzle.target > 0 && nozzle.actual < nozzle.target - 3;
  const bedHeating = bed && bed.target > 0 && bed.actual < bed.target - 2;
  const fan = (p.fans || [])[0];
  const fanOn = fan && fan.pct > 0;
  const layer = p.layer != null ? `${p.layer}` : "–";
  const total = p.total_layers != null ? `/ ${p.total_layers}` : "";
  const prog = p.progress || 0;
  const rows = Math.max(1, Math.round(prog / 100 * 7));
  let layers = "";
  for (let i = 0; i < 7; i++) {
    const y = 250 - i * 5;
    layers += `<rect x="145" y="${y}" width="110" height="3.2" rx="1.6" style="fill:${i < rows && p.printing ? "var(--layer)" : "var(--trk)"}"/>`;
  }
  let fins = "";
  for (let i = 0; i < 8; i++) fins += `<rect x="168" y="${62 + i * 9}" width="64" height="5" rx="1.5" class="metal"/>`;
  const leftMid = p.type === "moonraker"
    ? {l: "Fluss", v: p.flow != null ? p.flow : "–", u: p.flow != null ? "mm³/s" : ""}
    : {l: "Speed-Modus", v: HX.num(p.speed), u: p.speed_pct ? `${p.speed_pct} %` : ""};
  const rightMid = p.type === "moonraker"
    ? {l: "Speed", v: HX.num(p.speed), u: p.speed_pct && p.speed_pct !== 100 ? `${p.speed_pct} %` : ""}
    : {l: "Hotend-Lüfter", v: (p.fans || [])[1] && p.fans[1].pct != null ? `${p.fans[1].pct} %` : "–", u: ""};
  return `<svg viewBox="0 0 400 300" role="img" aria-label="Düse">
    <!-- Material -->
    <text class="lbl" x="18" y="34">MATERIAL</text>
    <text class="val" x="18" y="58">${HX.esc(material || "–")}</text>
    <polyline class="lead" points="18,64 110,64 150,27"/>
    <!-- Lüfter -->
    <text class="lbl" x="18" y="108">LÜFTER</text>
    <text class="val" x="18" y="132">${fan && fan.pct != null ? fan.pct + " %" : "–"}</text>
    <polyline class="lead" points="18,138 92,138 116,112"/>
    <!-- Fluss / Speed-Modus -->
    <text class="lbl" x="18" y="192">${HX.esc(leftMid.l.toUpperCase())}</text>
    <text class="val" x="18" y="216">${HX.esc(leftMid.v)} <tspan class="unit">${HX.esc(leftMid.u)}</tspan></text>
    <polyline class="lead" points="18,222 150,222 190,200"/>
    <!-- Hotend-Lüfter / Speed -->
    <text class="lbl" x="382" y="34" text-anchor="end">${HX.esc(rightMid.l.toUpperCase())}</text>
    <text class="val" x="382" y="58" text-anchor="end">${HX.esc(rightMid.v)} <tspan class="unit">${HX.esc(rightMid.u)}</tspan></text>
    <polyline class="lead" points="382,64 290,64 236,84"/>
    <!-- Düse -->
    <text class="lbl" x="382" y="128" text-anchor="end">DÜSE</text>
    <text class="val" x="382" y="152" text-anchor="end" style="fill:${nozzle && nozzle.actual >= 45 ? heat : "var(--fg)"}">${HX.deg(nozzle && nozzle.actual)}<tspan class="unit"> / ${HX.deg(nozzle && nozzle.target)}</tspan></text>
    <polyline class="lead" points="382,158 290,158 236,158"/>
    <!-- Layer -->
    <text class="lbl" x="382" y="206" text-anchor="end">LAYER</text>
    <text class="val acc" x="382" y="230" text-anchor="end">${layer} <tspan class="unit">${total}</tspan></text>
    <polyline class="lead" points="382,236 300,236 258,244"/>

    ${real ? hotendReal(p, info, {heat, heating, bedHeating, fan, fanOn, rows}) : `
    <!-- Filament -->
    <line x1="200" y1="44" x2="200" y2="206" stroke="${c}" stroke-width="3"/>
    <rect x="150" y="10" width="100" height="34" rx="8" fill="${c}" stroke="#ffffff30"/>
    <text x="200" y="32" text-anchor="middle" font-size="14" font-weight="800" fill="${HX.textOn(c)}">${HX.esc(material || "–")}</text>
    <!-- Kühlkörper + Lüfter -->
    ${fins}
    <rect class="fanbox" x="112" y="62" width="40" height="68" rx="6" fill="none" stroke-width="2"/>
    <circle class="fanc" cx="132" cy="96" r="15" fill="none" stroke-width="1.5"/>
    <g class="${fanOn ? "spin" : ""}" style="${HX.spin(fan && fan.pct)}">
      <circle cx="132" cy="96" r="13.5" fill="none"/>
      ${HX.blades(132, 96, 5, 3.2, 13, "var(--muted)", "none")}
      <circle cx="132" cy="96" r="3.6" class="fanf"/>
    </g>
    ${fanOn && fan.pct > 45 ? `<circle cx="132" cy="96" r="13" fill="var(--muted)" opacity="${(0.08 + fan.pct / 100 * 0.14).toFixed(2)}"/>` : ""}
    <circle cx="200" cy="96" r="19" fill="#e6edf7" stroke="${c}" stroke-width="3"/>
    <text x="200" y="103" text-anchor="middle" font-size="20" font-weight="800" fill="#0a1220">${toolNo}</text>
    <!-- Heizblock + Düse -->
    <rect x="166" y="138" width="68" height="40" rx="5" fill="${heat}" class="${heating ? "heating" : ""}"/>
    <rect x="166" y="138" width="68" height="40" rx="5" fill="url(#shine-${p.id})"/>
    <polygon points="184,178 216,178 205,204 195,204" fill="#eab308"/>
    ${p.printing ? `<polygon points="195,204 205,204 200,214" fill="#facc15"/>` : ""}
    <!-- Druckteil + Bett -->
    ${layers}
    <polygon points="96,262 304,262 326,284 74,284" class="metal ${bedHeating ? "heating" : ""}"/>
    <path d="M186 258 q4 -5 0 -10 M200 258 q4 -5 0 -10 M214 258 q4 -5 0 -10" stroke="${HX.heat(bed && bed.actual)}" stroke-width="2" fill="none" opacity="${bed && bed.actual >= 40 ? 1 : .25}"/>
    <text x="200" y="279" text-anchor="middle" font-size="14" font-weight="800" fill="#fff">BETT ${HX.deg(bed && bed.actual)}${bed && bed.target ? ` / ${HX.deg(bed.target)}` : ""}</text>
    <defs><linearGradient id="shine-${p.id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".25"/><stop offset="1" stop-color="#000" stop-opacity=".15"/></linearGradient></defs>`}
  </svg>`;
}
// Schattierte, möglichst echte Ansicht: Alu-Kühlkörper, Lüfter, Heizblock mit Silikonsocke, Messingdüse (glüht je nach Temperatur), PEI-Platte
function hotendReal(p, info, k) {
  const {nozzle, bed, color, material, toolNo} = info, c = color || "#64748b", id = "hr" + p.id.replace(/\W/g, "");
  const t = nozzle && nozzle.actual || 0, glow = Math.max(0, Math.min(1, (t - 60) / 200)), bt = bed && bed.actual || 0, bglow = Math.max(0, Math.min(1, (bt - 35) / 70));
  let fins = "";
  for (let i = 0; i < 9; i++) fins += `<rect x="164" y="${56 + i * 8.4}" width="72" height="5.2" rx="1" fill="url(#${id}al)"/><rect x="164" y="${61.2 + i * 8.4}" width="72" height="3.2" fill="#0b1020" opacity=".55"/>`;
  let part = "";
  const n = k.rows, lc = (i) => i % 2 ? `url(#${id}fa)` : `url(#${id}fb)`;
  for (let i = 0; i < 7; i++) part += `<rect x="150" y="${248 - i * 5}" width="100" height="4.6" rx="2.3" fill="${i < n && p.printing ? lc(i) : "var(--trk)"}" ${i < n && p.printing ? "" : `opacity=".55"`}/>`;
  return `<defs>
      <linearGradient id="${id}al" x1="0" x2="1"><stop offset="0" stop-color="#5b6472"/><stop offset=".22" stop-color="#e8edf3"/><stop offset=".5" stop-color="#aab3bf"/><stop offset=".8" stop-color="#d7dde5"/><stop offset="1" stop-color="#4b5361"/></linearGradient>
      <linearGradient id="${id}blk" x1="0" x2="1"><stop offset="0" stop-color="#3b4250"/><stop offset=".3" stop-color="#c9d0d9"/><stop offset=".65" stop-color="#8d96a3"/><stop offset="1" stop-color="#3e4552"/></linearGradient>
      <linearGradient id="${id}sock" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3a3f47"/><stop offset=".5" stop-color="#1c1f24"/><stop offset="1" stop-color="#0d0f12"/></linearGradient>
      <linearGradient id="${id}br" x1="0" x2="1"><stop offset="0" stop-color="#6b4a10"/><stop offset=".35" stop-color="#f8dc84"/><stop offset=".6" stop-color="#c8962f"/><stop offset="1" stop-color="#5a3f0c"/></linearGradient>
      <linearGradient id="${id}fan" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2e3440"/><stop offset="1" stop-color="#11151c"/></linearGradient>
      <linearGradient id="${id}fa" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".35"/><stop offset=".4" stop-color="${c}"/><stop offset="1" stop-color="#000" stop-opacity=".45"/></linearGradient>
      <linearGradient id="${id}fb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".25"/><stop offset=".45" stop-color="${c}"/><stop offset="1" stop-color="#000" stop-opacity=".5"/></linearGradient>
      <linearGradient id="${id}pei" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8a6420"/><stop offset=".45" stop-color="#d9b25c"/><stop offset="1" stop-color="#7a5719"/></linearGradient>
      <linearGradient id="${id}tube" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity=".15"/><stop offset=".5" stop-color="#fff" stop-opacity=".55"/><stop offset="1" stop-color="#fff" stop-opacity=".15"/></linearGradient>
      <linearGradient id="${id}bl" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5b6577"/><stop offset=".5" stop-color="#363e4c"/><stop offset="1" stop-color="#232a35"/></linearGradient>
      <radialGradient id="${id}mb"><stop offset=".25" stop-color="#4b5563" stop-opacity=".55"/><stop offset=".85" stop-color="#4b5563" stop-opacity=".35"/><stop offset="1" stop-color="#4b5563" stop-opacity="0"/></radialGradient>
      <radialGradient id="${id}hub" cx=".35" cy=".3"><stop offset="0" stop-color="#9aa3b2"/><stop offset="1" stop-color="#2b323e"/></radialGradient>
      <radialGradient id="${id}glow"><stop offset="0" stop-color="#ffb04a" stop-opacity=".95"/><stop offset=".4" stop-color="#ff5a1f" stop-opacity=".5"/><stop offset="1" stop-color="#ff2a00" stop-opacity="0"/></radialGradient>
      <filter id="${id}n" x="0" y="0" width="1" height="1"><feTurbulence type="fractalNoise" baseFrequency=".9" numOctaves="2" seed="3"/><feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 .35 0"/><feComposite in2="SourceGraphic" operator="in"/></filter>
    </defs>
    <!-- Filament im PTFE-Schlauch -->
    <rect x="193" y="6" width="14" height="52" rx="3" fill="${c}"/><rect x="190" y="6" width="20" height="52" rx="5" fill="url(#${id}tube)" stroke="#ffffff40"/>
    <!-- Kühlkörper -->
    <rect x="162" y="52" width="76" height="80" rx="4" fill="#20262f"/>${fins}
    <circle cx="200" cy="92" r="15" fill="#0b1220" opacity=".8"/><circle cx="200" cy="92" r="13" fill="#e6edf7"/><text x="200" y="98.5" text-anchor="middle" font-size="17" font-weight="800" fill="#0a1220">${toolNo}</text>
    <!-- Lüfter -->
    <!-- Lüftergehäuse mit Schrauben, Rad mit 7 geschwungenen Flügeln, Bewegungsunschärfe je nach Drehzahl -->
    <rect x="105" y="66" width="52" height="52" rx="8" fill="url(#${id}fan)" stroke="#000" stroke-opacity=".55"/>
    <rect x="106.5" y="67.5" width="49" height="49" rx="7" fill="none" stroke="#fff" stroke-opacity=".07"/>
    ${[[111, 72], [151, 72], [111, 112], [151, 112]].map(([x, y]) => `<circle cx="${x}" cy="${y}" r="2.2" fill="#0a0d12"/><circle cx="${x - .4}" cy="${y - .4}" r="1" fill="#6b7280" opacity=".6"/>`).join("")}
    <circle cx="131" cy="92" r="22" fill="#07090d"/>
    <circle cx="131" cy="92" r="22" fill="none" stroke="#000" stroke-width="1.5" opacity=".6"/>
    <g class="${k.fanOn ? "spin" : ""}" style="${HX.spin(k.fan && k.fan.pct)}">
      <circle cx="131" cy="92" r="20.5" fill="none"/>
      ${HX.blades(131, 92, 7, 6, 20, `url(#${id}bl)`, "#7b8496")}
    </g>
    ${k.fanOn && k.fan.pct > 30 ? `<circle cx="131" cy="92" r="20" fill="url(#${id}mb)" opacity="${Math.min(.6, k.fan.pct / 160).toFixed(2)}"/>` : ""}
    <circle cx="131" cy="92" r="6.5" fill="url(#${id}hub)"/>
    <circle cx="131" cy="92" r="3.8" fill="#1f2937" stroke="#9ca3af" stroke-width=".5" opacity=".9"/>
    <!-- Heizblock mit Silikonsocke -->
    <rect x="164" y="136" width="72" height="42" rx="6" fill="url(#${id}blk)"/>
    <rect x="161" y="134" width="78" height="46" rx="10" fill="url(#${id}sock)" opacity=".92"/>
    <rect x="166" y="138" width="68" height="8" rx="4" fill="#fff" opacity=".07"/>
    ${glow ? `<rect x="161" y="134" width="78" height="46" rx="10" fill="${k.heat}" opacity="${(glow * .35).toFixed(2)}" class="${k.heating ? "heating" : ""}"/>` : ""}
    <!-- Messingdüse -->
    <polygon points="186,180 214,180 214,188 186,188" fill="url(#${id}br)"/>
    <polygon points="188,188 212,188 203,207 197,207" fill="url(#${id}br)"/>
    ${glow > .3 ? `<circle cx="200" cy="208" r="${(10 + glow * 14).toFixed(1)}" fill="url(#${id}glow)" opacity="${glow.toFixed(2)}"/>` : ""}
    ${p.printing ? `<line x1="200" y1="207" x2="200" y2="${246 - (n - 1) * 5}" stroke="${c}" stroke-width="2.4"/>` : ""}
    <!-- Druckteil -->
    ${part}
    <!-- PEI-Platte -->
    <polygon points="92,262 308,262 330,286 70,286" fill="url(#${id}pei)"/>
    <polygon points="92,262 308,262 330,286 70,286" fill="#fff" filter="url(#${id}n)" opacity=".5"/>
    ${bglow ? `<polygon points="92,262 308,262 330,286 70,286" fill="#ff5a1f" opacity="${(bglow * .25).toFixed(2)}" class="${k.bedHeating ? "heating" : ""}"/>` : ""}
    <polygon points="92,262 308,262 310,264 90,264" fill="#fff" opacity=".35"/>
    <text x="200" y="280" text-anchor="middle" font-size="14" font-weight="800" fill="#1b1206" opacity=".85">BETT ${HX.deg(bed && bed.actual)}${bed && bed.target ? ` / ${HX.deg(bed.target)}` : ""}</text>`;
}

