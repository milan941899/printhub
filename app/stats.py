"""Auswertung des Druckverlaufs für die Statistik-Seite."""
import csv
import io
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

RANGES = {"week": 7, "month": 30, "year": 365, "all": None}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
DUR_BUCKETS = [(1800, "<30 min"), (3600, "30–60 min"), (7200, "1–2 h"), (14400, "2–4 h"), (28800, "4–8 h"),
               (43200, "8–12 h"), (86400, "12–24 h"), (None, ">24 h")]


def window(rng, now=None):
    now = now or time.time()
    days = RANGES.get(rng, 30)
    if days is None:
        return None, None, now
    start = now - days * 86400
    return start, start - days * 86400, now   # (Beginn, Beginn Vorperiode, Ende)


def _hex(c):
    c = (c or "").strip().lstrip("#")
    return ("#" + c[:6].upper()) if len(c) >= 6 else ""


def _mat(m):
    return (m or "").strip().upper() or "unbekannt"


def kpis(rows):
    n = len(rows)
    ok = sum(r["outcome"] == "ok" for r in rows)
    fail = sum(r["outcome"] == "fail" for r in rows)
    cancel = n - ok - fail
    s = lambda k: round(sum(r.get(k) or 0 for r in rows), 3)
    grams = sum(r.get("grams") or 0 for r in rows)
    return {
        "prints": n, "ok": ok, "fail": fail, "cancel": cancel,
        "success_rate": round(ok / (ok + fail) * 100, 1) if ok + fail else None,
        "hours": round(sum(r.get("duration") or 0 for r in rows) / 3600, 1),
        "grams": round(grams), "avg_grams": round(grams / n) if n else 0,
        "kwh": s("kwh"), "energy_cost": round(s("energy_cost"), 2), "filament_cost": round(s("filament_cost"), 2),
        "wear_cost": round(s("wear_cost"), 2), "total_cost": round(s("total_cost"), 2),
        "avg_cost": round(s("total_cost") / n, 2) if n else 0,
    }


def _buckets(rng, start, now, first_time):
    """Zeitachse: Tage (Woche/Monat) oder Monate (Jahr/Gesamt)."""
    if rng in ("week", "month"):
        d0 = date.fromtimestamp(start) + timedelta(days=1)
        days = [d0 + timedelta(days=i) for i in range((date.fromtimestamp(now) - d0).days + 1)]
        return [(d.isoformat(), f"{d.day}.{d.month}." if rng == "month" else WEEKDAYS[d.weekday()]) for d in days], \
            lambda t: date.fromtimestamp(t).isoformat()
    d = date.fromtimestamp(start if start else (first_time or now))
    end = date.fromtimestamp(now)
    out = []
    y, m = d.year, d.month
    while (y, m) <= (end.year, end.month):
        out.append((f"{y}-{m:02d}", f"{MONTHS[m - 1]} {str(y)[2:]}" if rng == "all" else MONTHS[m - 1]))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    if rng == "year":
        out = out[-12:]
    return out, lambda t: date.fromtimestamp(t).strftime("%Y-%m")


def compute(rows_all, rng="month", printer="", vendors=None, now=None):
    now = now or time.time()
    vendors = vendors or {}
    if printer:
        rows_all = [r for r in rows_all if r["printer"] == printer]
    start, prev_start, _ = window(rng, now)
    rows = [r for r in rows_all if start is None or r["time"] >= start]
    prev = [r for r in rows_all if start is not None and prev_start <= r["time"] < start]

    out = {"range": rng, "printer": printer, "kpis": kpis(rows), "prev": kpis(prev) if start is not None else None,
           "printers": sorted({r["printer"] for r in rows_all if r["printer"]}), "empty": not rows}

    # --- Erfolg & Fehler -------------------------------------------------------
    reasons = Counter((r.get("reason") or ("Druckerfehler" if r["outcome"] == "fail" else "abgebrochen"))
                      for r in rows if r["outcome"] != "ok")
    last7 = [r for r in rows_all if r["time"] >= now - 7 * 86400]
    k7 = kpis(last7)
    out["failures"] = {"reasons": reasons.most_common(6),
                       "fail_rate": round(out["kpis"]["fail"] / out["kpis"]["prints"] * 100, 1) if rows else None,
                       "fail_rate_7d": round(k7["fail"] / k7["prints"] * 100, 1) if last7 else None,
                       "recent": [{"time": r["time"], "job": r["job"], "result": r["result"], "reason": r.get("reason")}
                                  for r in reversed(rows) if r["outcome"] != "ok"][:5]}

    # --- Kalender (immer die letzten 53 Wochen) -----------------------------------
    cal = defaultdict(lambda: {"n": 0, "g": 0.0})
    for r in rows_all:
        if r["time"] >= now - 371 * 86400:
            c = cal[date.fromtimestamp(r["time"]).isoformat()]
            c["n"] += 1
            c["g"] += r.get("grams") or 0
    out["calendar"] = {k: {"n": v["n"], "g": round(v["g"])} for k, v in cal.items()}

    # --- Rekorde ---------------------------------------------------------------
    def best(key, cond=lambda r: True):
        c = [r for r in rows if r.get(key) and cond(r)]
        if not c:
            return None
        r = max(c, key=lambda x: x[key])
        return {"value": r[key], "job": r["job"], "time": r["time"], "printer": r["printer"]}
    days = Counter(date.fromtimestamp(r["time"]).isoformat() for r in rows)
    streak = cur = 0
    for r in rows:
        if r["outcome"] == "ok":
            cur += 1
            streak = max(streak, cur)
        elif r["outcome"] == "fail":
            cur = 0
    out["records"] = {
        "longest": best("duration", lambda r: r["outcome"] == "ok"), "heaviest": best("grams"),
        "priciest": best("total_cost"),
        "busiest_day": ({"date": days.most_common(1)[0][0], "n": days.most_common(1)[0][1]} if days else None),
        "streak": streak, "streak_now": cur,
    }

    # --- Zeitachse: Filament, Drucke, Strom, Kosten ----------------------------------
    first = rows_all[0]["time"] if rows_all else None
    buckets, keyf = _buckets(rng, start, now, first if start is None else None)
    series = {k: {"g": 0.0, "h": 0.0, "n": 0, "kwh": 0.0, "fc": 0.0, "ec": 0.0, "wc": 0.0, "mats": defaultdict(float)} for k, _ in buckets}
    for r in rows:
        b = series.get(keyf(r["time"]))
        if not b:
            continue
        b["g"] += r.get("grams") or 0
        b["n"] += 1
        b["h"] += (r.get("duration") or 0) / 3600
        b["kwh"] += r.get("kwh") or 0
        b["fc"] += r.get("filament_cost") or 0
        b["ec"] += r.get("energy_cost") or 0
        b["wc"] += r.get("wear_cost") or 0
        for u in r["usage"]:
            b["mats"][_mat(u.get("material"))] += u.get("grams") or 0
    out["timeline"] = [{"key": k, "label": lab, "g": round(series[k]["g"]), "h": round(series[k]["h"], 1), "n": series[k]["n"],
                        "kwh": round(series[k]["kwh"], 3), "fc": round(series[k]["fc"], 2),
                        "ec": round(series[k]["ec"], 2), "wc": round(series[k]["wc"], 2),
                        "mats": {m: round(g) for m, g in series[k]["mats"].items()}} for k, lab in buckets]

    # --- Filament nach Material, Farbe, Hersteller ------------------------------------
    mat_g, mat_h, mat_n, mat_ok, mat_fail = defaultdict(float), defaultdict(float), Counter(), Counter(), Counter()
    col_g, col_name, col_n = defaultdict(float), {}, Counter()
    ven_g, ven_c, ven_n = defaultdict(float), defaultdict(float), Counter()
    for r in rows:
        mats, cols_seen = set(), set()
        for u in r["usage"]:
            g = u.get("grams") or 0
            m = _mat(u.get("material"))
            mat_g[m] += g
            mats.add(m)
            h = _hex(u.get("color"))
            if h:
                col_g[h] += g
                cols_seen.add(h)
                col_name.setdefault(h, u.get("spool_name") or "")
            if u.get("spool"):
                v = vendors.get(u["spool"]) or "unbekannt"
                ven_g[v] += g
                ven_c[v] += u.get("cost") or 0
                ven_n[v] += 1
        tot_g = sum(u.get("grams") or 0 for u in r["usage"])
        if tot_g > 0:   # Druckzeit anteilig nach Gewicht auf die Materialien verteilen
            for u in r["usage"]:
                mat_h[_mat(u.get("material"))] += (r.get("duration") or 0) / 3600 * (u.get("grams") or 0) / tot_g
        for h in cols_seen:
            col_n[h] += 1
        for m in mats:
            mat_n[m] += 1
            mat_ok[m] += r["outcome"] == "ok"
            mat_fail[m] += r["outcome"] == "fail"
    out["materials"] = [{"name": m, "g": round(g), "n": mat_n[m], "h": round(mat_h[m], 1),
                         "success": round(mat_ok[m] / (mat_ok[m] + mat_fail[m]) * 100) if mat_ok[m] + mat_fail[m] else None}
                        for m, g in sorted(mat_g.items(), key=lambda x: -x[1])]
    cols = sorted(col_g.items(), key=lambda x: -x[1])
    out["colors"] = [{"hex": h, "g": round(g), "n": col_n[h], "name": col_name.get(h, "")} for h, g in cols[:10]]
    if len(cols) > 10:
        out["colors"].append({"hex": "", "g": round(sum(g for _, g in cols[10:])), "n": sum(col_n[h] for h, _ in cols[10:]),
                              "name": f"{len(cols) - 10} weitere"})
    out["vendors"] = [{"name": v, "g": round(g), "cost": round(ven_c[v], 2), "n": ven_n[v]} for v, g in sorted(ven_g.items(), key=lambda x: -x[1])]

    # --- Gewohnheiten ----------------------------------------------------------------
    dur = Counter()
    wd_n, wd_g, wd_h = [0] * 7, [0.0] * 7, [0.0] * 7
    hours, hours_bad = [0] * 24, [0] * 24
    for r in rows:
        d = r.get("duration") or 0
        for lim, lab in DUR_BUCKETS:
            if lim is None or d < lim:
                dur[lab] += 1
                break
        st = r["time"] - d
        lt = time.localtime(st)
        wd_n[lt.tm_wday] += 1
        wd_g[lt.tm_wday] += r.get("grams") or 0
        wd_h[lt.tm_wday] += d / 3600
        hours[lt.tm_hour] += 1
        hours_bad[lt.tm_hour] += r["outcome"] != "ok"
    out["habits"] = {"duration": [{"label": lab, "n": dur[lab]} for _, lab in DUR_BUCKETS],
                     "weekday": [{"label": WEEKDAYS[i], "n": wd_n[i], "g": round(wd_g[i]), "h": round(wd_h[i], 1)} for i in range(7)],
                     "hour": [{"label": str(h), "n": hours[h], "ok": hours[h] - hours_bad[h], "bad": hours_bad[h]} for h in range(24)]}

    # --- Zeitgenauigkeit (Schätzung / tatsächliche Dauer) -------------------------
    acc = [r for r in rows if r.get("estimate") and r.get("duration") and r["outcome"] == "ok"]
    per = defaultdict(list)
    for r in acc:
        per[r["printer"]].append(r["estimate"] / r["duration"] * 100)
    allv = [v for vs in per.values() for v in vs]
    out["accuracy"] = {"avg": round(sum(allv) / len(allv)) if allv else None, "n": len(allv),
                       "printers": {p: round(sum(v) / len(v)) for p, v in per.items()},
                       "recent": [{"job": r["job"], "est": r["estimate"], "real": r["duration"]} for r in acc[-6:]][::-1]}

    # --- Pro Drucker -----------------------------------------------------------------
    byp = defaultdict(list)
    for r in rows:
        byp[r["printer"]].append(r)
    out["by_printer"] = [{"name": p, **{k: v for k, v in kpis(rs).items() if k in ("prints", "hours", "grams", "kwh", "total_cost", "success_rate")}}
                         for p, rs in sorted(byp.items())]
    return out


def to_csv(st):
    """Statistik als CSV (Semikolon, Komma als Dezimalzeichen – passt für Excel)."""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    de = lambda v: "" if v is None else (str(v).replace(".", ",") if isinstance(v, float) else v)
    k = st["kpis"]
    names = {"week": "letzte 7 Tage", "month": "letzte 30 Tage", "year": "letzte 12 Monate", "all": "gesamt"}
    w.writerow(["PrintHub-Statistik", names.get(st["range"], st["range"]), st["printer"] or "alle Drucker",
                datetime.now().strftime("%d.%m.%Y %H:%M")])
    w.writerow([])
    w.writerow(["Kennzahl", "Wert"])
    for lab, key in (("Drucke", "prints"), ("Erfolgreich", "ok"), ("Fehlgeschlagen", "fail"), ("Abgebrochen", "cancel"),
                     ("Erfolgsrate (%)", "success_rate"), ("Druckzeit (h)", "hours"), ("Filament (g)", "grams"),
                     ("Strom (kWh)", "kwh"), ("Filamentkosten (€)", "filament_cost"), ("Stromkosten (€)", "energy_cost"),
                     ("Verschleiß (€)", "wear_cost"), ("Gesamtkosten (€)", "total_cost")):
        w.writerow([lab, de(k.get(key))])
    w.writerow([])
    w.writerow(["Zeitraum", "Drucke", "Filament (g)", "Strom (kWh)", "Filamentkosten (€)", "Stromkosten (€)", "Verschleiß (€)"])
    for t in st["timeline"]:
        w.writerow([t["key"], t["n"], t["g"], de(t["kwh"]), de(t["fc"]), de(t["ec"]), de(t["wc"])])
    w.writerow([])
    w.writerow(["Material", "Filament (g)", "Drucke", "Erfolgsrate (%)"])
    for m in st["materials"]:
        w.writerow([m["name"], m["g"], m["n"], de(m["success"])])
    w.writerow([])
    w.writerow(["Hersteller", "Filament (g)", "Filamentkosten (€)"])
    for v in st["vendors"]:
        w.writerow([v["name"], v["g"], de(v["cost"])])
    w.writerow([])
    w.writerow(["Drucker", "Drucke", "Druckzeit (h)", "Filament (g)", "Strom (kWh)", "Kosten (€)", "Erfolgsrate (%)"])
    for p in st["by_printer"]:
        w.writerow([p["name"], p["prints"], de(p["hours"]), p["grams"], de(p["kwh"]), de(p["total_cost"]), de(p["success_rate"])])
    w.writerow([])
    w.writerow(["Fehlergrund", "Anzahl"])
    for r, n in st["failures"]["reasons"]:
        w.writerow([r, n])
    return buf.getvalue()
