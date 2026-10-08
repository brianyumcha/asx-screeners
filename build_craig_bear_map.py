#!/usr/bin/env python3
"""
Rebuilds craig-bear-map.html from the real MtM Live Call Summary Tickers
tracker - the mirror image of build_craig_bullish_map.py (see that
script's own docstring/git history for the full methodology background;
this is the same pipeline with the survival filter flipped to Bearish).

Methodology (identical to the Bull Map's, just Bearish instead of
Bullish):
  - Only rows whose Call Name is one of Craig Dickson's 3 shows count at
    all: "Fundamental Analysis Scan with Craig Dickson" (incl. the
    "(Pre-Recorded)" variant), "Market Mornings", "FAQ Fridays" - a call
    from any other educator/show must never affect a ticker's stance here.
  - A ticker's GATING stance = the bias on its single most-recent row
    across those 3 shows (same-date ties broken toward FA Scan, since
    that's Craig's own deliberate dedicated coverage). Only "Bearish" or
    "Bearish (Unchanged)" survive gating; everything else (Bullish,
    Neutral, Informational, Bullish (Unchanged)) drops the ticker
    entirely.
  - Having survived gating, the DISPLAYED description/date prefers the
    latest FA Scan row for that ticker if one exists AND is itself
    Bearish/Bearish (Unchanged) - FA Scan is Craig's deliberate per-ticker
    coverage, so its writeup is the canonical one shown even if a later
    casual mention happened elsewhere. If the latest FA Scan row isn't
    itself bearish, fall back to the gating row instead, so the map never
    shows a non-bearish writeup under a Bearish badge.
  - Commodity sub-grouping (Materials sector only) is classified from
    CRAIG'S OWN NOTE TEXT via HH SCREENER.py's classify_commodity(), not a
    generic Yahoo business-summary, falling back to
    materials_commodity_cache.json only when his notes don't name a
    commodity explicitly.
  - A surviving ticker not found in the live SeaBee universe is surfaced
    in an "unresolved" callout box instead of silently dropped or
    silently included with guessed data.

Deliberately reuses the Bull Map's exact visual system (same CSS/sector
colors) rather than inventing a new "red" palette for this page - a new
page-wide accent color would collide with the Energy sector's own red/
salmon swatch, and sector hues (Materials/Energy/Health/...) are a fixed
site-wide convention that shouldn't vary page to page.

Usage:
    python3 build_craig_bear_map.py
"""
import importlib.util
import json
import os
import re
from collections import defaultdict
from datetime import datetime

import openpyxl

import rs_utils

MATERIALS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "materials_commodity_cache.json")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKER_PATH = (
    "/Users/briantam/Library/CloudStorage/GoogleDrive-briantam86@gmail.com/My Drive/"
    "MtM Academy - Call Summaries/MtM Live Call Summary Tickers.xlsx"
)
OUT_PATH = os.path.join(SCRIPT_DIR, "craig-bear-map.html")

# HH SCREENER.py has a space in its filename so it can't be `import`ed
# normally - load it by path. We only need its classify_commodity() /
# get_asx_universe() helpers; executing the module just runs its
# function/constant definitions (guarded by `if __name__ == '__main__'`).
_spec = importlib.util.spec_from_file_location("hh_screener", os.path.join(SCRIPT_DIR, "HH SCREENER.py"))
hh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hh)

CRAIG_SHOWS_PREFIXES = ("Fundamental Analysis Scan", "Market Mornings", "FAQ Fridays")
BEARISH_VALUES = {"Bearish", "Bearish (Unchanged)"}

GICS_TO_BUCKET = {
    "Materials": "materials",
    "Energy": "energy",
    "Healthcare": "health",
    "Industrials": "industrials",
    "Info Tech": "it",
    "Consumer Discretionary": "consumer",
}
BUCKET_ORDER = ["materials", "energy", "health", "industrials", "it", "consumer", "other"]
BUCKET_LABELS = {
    "materials": "Materials",
    "energy": "Energy",
    "health": "Health Care",
    "industrials": "Industrials",
    "it": "Information Technology",
    "consumer": "Consumer Discretionary",
    "other": "Other",
}
BUCKET_COLOR_VAR = {
    "materials": "--materials", "energy": "--energy", "health": "--health",
    "industrials": "--industrials", "it": "--it", "consumer": "--consumer", "other": "--unresolved",
}


def title_case(name):
    def fix(w):
        if not w:
            return w
        if len(w) <= 3 and w == w.upper():
            return w
        return w[0] + w[1:].lower()
    return " ".join(fix(w) for w in (name or "").split(" "))


def slugify(label):
    s = (label or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unclassified"


def desc_from_note(note):
    lines = [l.strip() for l in (note or "").split("\n") if l.strip()]
    bullets = [l[2:].strip() if l.startswith("- ") else l for l in lines]
    return " · ".join(bullets)


PAREN_CODE_RE = re.compile(r"\(([A-Z0-9]{1,6})\)")


def resolve_ticker(raw, universe):
    """Tracker rows store the ticker field as either a bare code ("BHM"),
    "Code (Company Name)" ("GLN" -> "Galan Lithium (GLN)" gets written
    the OTHER way round sometimes too - "BHM (Broken Hill Mines)"), or
    occasionally just the company's common name with no code at all
    ("Jade Gas" instead of "JGH"). Validating each candidate against the
    live universe (rather than trusting regex shape alone) matters: naive
    "code is whatever's in parens" on "BHM (Broken Hill Mines)" would
    wrongly extract "Broken" (6 letters, syntactically plausible) instead
    of the real code "BHM" sitting outside the parens. Returns the
    validated code if any candidate actually exists in the universe,
    else the best-effort first guess (so same-ticker rows still group
    together even when unresolved, and the raw text is still visible in
    the unresolved callout for a quick manual fix)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    bare = raw.upper()
    if bare in universe:
        return bare
    m = PAREN_CODE_RE.search(raw)
    if m and m.group(1).upper() in universe:
        return m.group(1).upper()
    pre = raw.split("(")[0].strip().upper()
    if pre and pre in universe:
        return pre
    return (m.group(1).upper() if m else None) or pre or bare


def js_str(s):
    return "'" + (s or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_tracker_records(universe):
    wb = openpyxl.load_workbook(TRACKER_PATH, data_only=True)
    ws = wb["Tickers"]
    headers = [c.value for c in ws[1]]
    idx = {h: i for i, h in enumerate(headers)}
    records = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        call_name = (row[idx["Call Name"]] or "").strip()
        if not any(call_name.startswith(p) for p in CRAIG_SHOWS_PREFIXES):
            continue
        raw_ticker = (row[idx["Ticker"]] or "").strip()
        if not raw_ticker:
            continue
        ticker = resolve_ticker(raw_ticker, universe)
        if not ticker:
            continue
        date_val = row[idx["Date"]]
        date_iso = date_val.isoformat()[:10] if hasattr(date_val, "isoformat") else str(date_val)[:10]
        bias = (row[idx["Bias"]] or "").strip()
        note = row[idx["Note"]] or ""
        records.append({
            "ticker": ticker,
            "date": date_iso,
            "call_name": call_name,
            "bias": bias,
            "note": note,
            "is_fa_scan": call_name.startswith("Fundamental Analysis Scan"),
        })
    return records


def derive_survivors(records):
    by_ticker = defaultdict(list)
    for r in records:
        by_ticker[r["ticker"]].append(r)

    survivors = {}  # ticker -> {display, all_rows}
    for ticker, rows in by_ticker.items():
        rows_sorted = sorted(rows, key=lambda r: (r["date"], r["is_fa_scan"]), reverse=True)
        latest = rows_sorted[0]
        if latest["bias"] not in BEARISH_VALUES:
            continue
        fa_rows = [r for r in rows_sorted if r["is_fa_scan"]]
        if fa_rows and fa_rows[0]["bias"] in BEARISH_VALUES:
            display = fa_rows[0]
        else:
            display = latest
        survivors[ticker] = {"display": display, "all_rows": rows_sorted}
    return survivors


def classify_and_group(survivors, universe):
    groups = defaultdict(list)  # group_key -> [ [ticker, name, desc, date] ]
    bucket_counts = defaultdict(int)
    unresolved = []

    try:
        with open(MATERIALS_CACHE_PATH, encoding="utf-8") as f:
            materials_cache = json.load(f)
    except FileNotFoundError:
        materials_cache = {}

    for ticker, info in survivors.items():
        display = info["display"]
        uni = universe.get(ticker)
        if not uni:
            unresolved.append((ticker, display))
            continue

        industry = uni.get("industry") or "Other"
        gics_sector = rs_utils.SECTOR_MAP.get(industry, "Other")
        bucket = GICS_TO_BUCKET.get(gics_sector, "other")
        name = title_case(uni.get("name") or ticker)
        desc = desc_from_note(display["note"])
        date_iso = display["date"]

        bucket_counts[bucket] += 1

        if bucket == "materials":
            all_notes_text = " ".join(r["note"] or "" for r in info["all_rows"])
            label = hh.classify_commodity(all_notes_text)
            # Craig's own words win when he actually names a commodity; only
            # fall back to the Materials Index's existing (already
            # hand-corrected) Yahoo-summary-derived cache when his notes
            # alone don't mention one explicitly, rather than leaving a
            # ticker fully unclassified.
            if not label:
                label = materials_cache.get(ticker)
            primary = (label.split(" + ")[0] if label else "Unclassified")
            group_key = f"g-{slugify(primary)}"
            group_label = primary
        else:
            group_key = f"g-{bucket}"
            group_label = BUCKET_LABELS[bucket]

        groups[group_key].append({
            "row": [ticker, name, desc, date_iso],
            "bucket": bucket,
            "group_label": group_label,
        })

    for key in groups:
        groups[key].sort(key=lambda g: g["row"][3], reverse=True)

    return groups, bucket_counts, unresolved


def render_html(groups, bucket_counts, unresolved, total_calls, total_tickers_seen):
    total_bearish = sum(bucket_counts.values())
    materials_count = bucket_counts.get("materials", 0)
    non_materials_count = total_bearish - materials_count

    # Materials sub-commodity groups, sorted by count desc then label. Any
    # group key other than a bare sector-bucket key ("g-energy" etc.) is by
    # construction a materials commodity sub-group, since sector buckets are
    # only ever keyed exactly "g-<bucket>" for non-materials buckets.
    non_materials_sector_keys = {f"g-{b}" for b in BUCKET_ORDER if b != "materials"}
    materials_groups = sorted(
        (k for k in groups if k not in non_materials_sector_keys),
        key=lambda k: (-len(groups[k]), groups[k][0]["group_label"]),
    )

    biggest_label, biggest_count = (None, 0)
    if materials_groups:
        top_key = materials_groups[0]
        biggest_label = groups[top_key][0]["group_label"]
        biggest_count = len(groups[top_key])

    # ---- statrow ----
    statrow = f"""  <div class="statrow">
    <div class="statchip"><div class="n">{total_bearish}</div><div class="l">Bearish tickers</div></div>
    <div class="statchip"><div class="n">{materials_count}</div><div class="l">Materials</div></div>
    <div class="statchip"><div class="n">{biggest_count}</div><div class="l">{biggest_label or "—"}, the biggest cluster</div></div>
    <div class="statchip"><div class="n">{non_materials_count}</div><div class="l">Non-Materials names</div></div>
  </div>"""

    # ---- stackbar ----
    stack_parts = []
    for b in BUCKET_ORDER:
        c = bucket_counts.get(b, 0)
        if c == 0:
            continue
        pct = (c / total_bearish * 100) if total_bearish else 0
        stack_parts.append(
            f'    <span style="width:{pct:.2f}%;background:var({BUCKET_COLOR_VAR[b]})" '
            f'title="{BUCKET_LABELS[b]} {c}"></span>'
        )
    stackbar = "  <div class=\"stackbar\">\n" + "\n".join(stack_parts) + "\n  </div>"

    # ---- sector filter chips ----
    sf_parts = []
    for b in BUCKET_ORDER:
        c = bucket_counts.get(b, 0)
        if c == 0:
            continue
        sf_parts.append(
            f'    <button class="sectorchip" data-sector="{b}">'
            f'<i style="background:var({BUCKET_COLOR_VAR[b]})"></i>{BUCKET_LABELS[b]}<span class="cnt">{c}</span></button>'
        )
    sector_filter = '  <div class="sectorfilter" id="sectorFilter">\n' + "\n".join(sf_parts) + "\n  </div>"

    # ---- commodity filter chips (within Materials) ----
    cf_parts = []
    for key in materials_groups:
        label = groups[key][0]["group_label"]
        commodity_slug = key[2:]  # strip "g-"
        cf_parts.append(
            f'      <button class="sectorchip" data-commodity="{commodity_slug}">{label}<span class="cnt">{len(groups[key])}</span></button>'
        )
    commodity_filter = (
        '    <div class="sectorfilter commodityfilter" id="commodityFilter">\n' + "\n".join(cf_parts) + "\n    </div>"
    )

    # ---- commgroup divs (within Materials) ----
    commgroup_parts = []
    for key in materials_groups:
        commodity_slug = key[2:]
        label = groups[key][0]["group_label"]
        commgroup_parts.append(
            f'    <div class="commgroup" data-commodity="{commodity_slug}">'
            f'<div class="clabel"><span class="cname">{label}</span><span class="ccount">{len(groups[key])}</span></div>'
            f'<div class="grouptablewrap" id="{key}"></div></div>'
        )
    commgroups_html = "\n".join(commgroup_parts)

    materials_block = f"""  <div class="sector" data-sector="materials">
    <div class="sechead"><span class="swatch" style="background:var(--materials)"></span><h2>Materials — {materials_count}</h2></div>
    <p style="margin-top:.3rem">Grouped by the commodity Craig is actually discussing right now, derived straight from his own call notes.</p>

{commodity_filter}

{commgroups_html}
  </div>""" if materials_count else ""

    # ---- non-materials sector blocks ----
    sector_blocks = []
    for b in BUCKET_ORDER:
        if b in ("materials", "other"):
            continue
        c = bucket_counts.get(b, 0)
        if c == 0:
            continue
        sector_blocks.append(f"""  <div class="sector" data-sector="{b}">
    <div class="sechead"><span class="swatch" style="background:var({BUCKET_COLOR_VAR[b]})"></span><h2>{BUCKET_LABELS[b]} — {c}</h2></div>
    <div class="grouptablewrap" id="g-{b}"></div>
  </div>""")
    other_count = bucket_counts.get("other", 0)
    if other_count:
        sector_blocks.append(f"""  <div class="sector" data-sector="other">
    <div class="sechead"><span class="swatch" style="background:var(--unresolved)"></span><h2>Other — {other_count}</h2></div>
    <div class="grouptablewrap" id="g-other"></div>
  </div>""")

    # ---- unresolved box ----
    unresolved_html = ""
    if unresolved:
        items = "".join(
            f'<li><span class="mono">{tk}</span> — {desc_from_note(d["note"])[:140]}'
            f'{"…" if len(desc_from_note(d["note"])) > 140 else ""} ({d["date"]})</li>'
            for tk, d in sorted(unresolved, key=lambda x: x[1]["date"], reverse=True)
        )
        unresolved_html = f"""  <div class="unresolvedbox">
    <strong>{len(unresolved)} ticker{"s" if len(unresolved) != 1 else ""} currently bearish per the tracker but not resolvable against the live ASX universe</strong>
    (typo, delisted/renamed code, or ticker never stated on the call) — worth a quick manual check:
    <ul style="margin:.5rem 0 0;padding-left:1.2rem">{items}</ul>
  </div>"""

    # ---- DATA JS object ----
    all_keys = materials_groups + [f"g-{b}" for b in BUCKET_ORDER if b != "materials" and bucket_counts.get(b, 0)]
    data_lines = []
    for key in all_keys:
        rows_js = ",".join("[" + ",".join(js_str(v) for v in g["row"]) + "]" for g in groups[key])
        data_lines.append(f"  '{key}': [ {rows_js} ],")
    data_js = "const DATA = {\n" + "\n".join(data_lines) + "\n};"

    color_lines = []
    for key in materials_groups:
        color_lines.append(f"  '{key}':'var(--materials)',")
    for b in BUCKET_ORDER:
        if b == "materials":
            continue
        color_lines.append(f"  'g-{b}':'var({BUCKET_COLOR_VAR[b]})',")
    color_by_group_js = "const COLOR_BY_GROUP = {\n" + "\n".join(color_lines) + "\n};"

    today_str = datetime.now().strftime("%-d %b %Y")
    footer_text = (
        f"Rebuilt {today_str} from Craig's own call log ({total_calls:,} calls, {total_tickers_seen:,} tickers across his "
        f"FA Scan / Market Mornings / FAQ Fridays shows) via build_craig_bear_map.py - current stance = latest call "
        f"across those 3 shows (same-day ties favouring FA Scan), kept only if Bearish. A ticker's commodity grouping "
        f"reflects how Craig is actually discussing it right now, not necessarily its full historical business."
    )
    subtitle_text = (
        f"{total_bearish} tickers Craig is currently bearish on, grouped by sector and, within Materials, by primary commodity."
    )

    template = CSS_AND_SHELL
    template = template.replace("##SUBTITLE##", subtitle_text)
    template = template.replace("##STATROW##", statrow)
    template = template.replace("##STACKBAR##", stackbar)
    template = template.replace("##SECTORFILTER##", sector_filter)
    template = template.replace("##MATERIALS_BLOCK##", materials_block)
    template = template.replace("##SECTOR_BLOCKS##", "\n\n".join(sector_blocks))
    template = template.replace("##UNRESOLVED_BOX##", unresolved_html)
    template = template.replace("##FOOTER_TEXT##", footer_text)
    template = template.replace("##DATA_JS##", data_js)
    template = template.replace("##COLOR_BY_GROUP_JS##", color_by_group_js)
    return template


CSS_AND_SHELL = r"""<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Craig's Bear Map</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<style>
:root{
  --bg:#faf8f2; --surface:#f0ead8; --surface2:#e6ddc4; --border:#a89b7a;
  --ink:#141209; --ink-soft:#3d3829; --ink-faint:#6b6552;
  --accent:#7a4a0f; --accent-ink:#fdf8ef;
  --materials:#7a4a0f; --energy:#6b241c; --health:#163540; --industrials:#3d5c37; --it:#4a3570; --consumer:#7a2f52; --unresolved:#6b6552;
  --shadow: 0 1px 2px rgba(20,18,9,.06), 0 4px 16px rgba(20,18,9,.05);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#121316; --surface:#1c1f24; --surface2:#262b32; --border:#454b56;
    --ink:#f5f2e8; --ink-soft:#c9c2b0; --ink-faint:#8f8874;
    --accent:#e8b355; --accent-ink:#1c1204;
    --materials:#e8b355; --energy:#f0998a; --health:#8fc4db; --industrials:#a8c99e; --it:#b8a8e8; --consumer:#e0a8c8; --unresolved:#8f8874;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
  }
}
:root[data-theme="dark"]{
  --bg:#121316; --surface:#1c1f24; --surface2:#262b32; --border:#454b56;
  --ink:#f5f2e8; --ink-soft:#c9c2b0; --ink-faint:#8f8874;
  --accent:#e8b355; --accent-ink:#1c1204;
  --materials:#e8b355; --energy:#f0998a; --health:#8fc4db; --industrials:#a8c99e; --it:#b8a8e8; --consumer:#e0a8c8; --unresolved:#8f8874;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"IBM Plex Sans",-apple-system,sans-serif;font-size:16px;line-height:1.6}
::selection{background:var(--accent);color:var(--accent-ink)}
.wrap{max-width:1560px;margin:0 auto;padding:2.4rem 1.5rem 5rem}
h1,h2,h3{font-family:"Fraunces",Georgia,serif;font-weight:600;text-wrap:balance;color:var(--ink)}
h1{font-size:2.4rem;letter-spacing:-.01em;margin:0 0 .2rem}
h2{font-size:1.3rem;margin:0}
h3{font-size:.85rem;font-family:"IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-faint);margin:1.3rem 0 .6rem;font-weight:600}
p{color:var(--ink-soft)}
.mono{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}

.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;margin-bottom:.4rem}
.themebtn{border:1px solid var(--border);background:var(--surface);color:var(--ink);width:2.3rem;height:2.3rem;border-radius:50%;cursor:pointer;font-size:1.05rem;display:flex;align-items:center;justify-content:center;flex:none}
.topbar-right{display:flex;gap:.6rem;align-items:center;flex:none}
.copybtn{background:var(--surface);border:1px solid var(--border);color:var(--ink);font-size:.72rem;padding:.5rem .9rem;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:.4rem;white-space:nowrap;height:fit-content}
.copybtn:hover{border-color:var(--accent)}
.copybtn.copied{border-color:var(--accent);color:var(--accent)}
.subtitle{color:var(--ink-faint);font-size:.95rem;margin:.3rem 0 0;max-width:36rem}
.source{font-family:"IBM Plex Mono",monospace;font-size:.8rem;color:var(--ink-faint);margin-top:.7rem}

.statrow{display:flex;gap:.7rem;flex-wrap:wrap;margin:1.4rem 0}
.statchip{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.6rem 1rem;box-shadow:var(--shadow)}
.statchip .n{font-family:"Fraunces",serif;font-size:1.5rem;font-weight:600}
.statchip .l{font-size:.7rem;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.05em;font-family:"IBM Plex Mono",monospace}

.stackbar{display:flex;height:2rem;border-radius:6px;overflow:hidden;margin:1rem 0;border:1px solid var(--border)}
.stackbar span{display:block;height:100%}
.sectorfilter{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.6rem}
.commodityfilter{margin-top:.9rem;margin-bottom:1.1rem}
.commodityfilter .sectorchip{font-size:.68rem;padding:.3rem .7rem}
.sectorchip{font-family:"IBM Plex Mono",monospace;font-size:.72rem;border:1px solid var(--border);
  background:var(--surface);color:var(--ink-soft);border-radius:999px;padding:.35rem .8rem;cursor:pointer;
  display:inline-flex;align-items:center}
.sectorchip i{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.4rem}
.sectorchip .cnt{opacity:.7;margin-left:.35rem;font-size:.85em}
.sectorchip:hover{border-color:var(--accent);color:var(--accent)}
.sectorchip.active{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}

.sector{margin-top:2.6rem;padding-top:1.4rem;border-top:1px solid var(--border)}
.sector .sechead{display:flex;align-items:baseline;gap:.6rem;margin-bottom:.3rem}
.sector .swatch{width:.85rem;height:.85rem;border-radius:3px;flex:none}
.sector .count{font-family:"IBM Plex Mono",monospace;font-size:.78rem;color:var(--ink-faint);margin-left:auto}

.commgroup{margin-top:1rem}
.commgroup .clabel{margin-bottom:.5rem;display:flex;align-items:baseline;gap:.4rem}
.commgroup .clabel .cname{font-family:"Fraunces",Georgia,serif;font-size:1.25rem;font-weight:600;color:var(--ink)}
.commgroup .clabel .ccount{font-family:"IBM Plex Mono",monospace;font-size:.72rem;color:var(--ink-faint)}
.commgroup .clabel .ccount::before{content:'· '}
.grouptablewrap{overflow-x:auto}
table.grouptable{width:100%;table-layout:fixed;border-collapse:collapse;font-size:.85rem;margin-bottom:.3rem}
table.grouptable th:nth-child(1), table.grouptable td:nth-child(1){width:7%}
table.grouptable th:nth-child(2), table.grouptable td:nth-child(2){width:20%}
table.grouptable th:nth-child(3), table.grouptable td:nth-child(3){width:60%}
table.grouptable th:nth-child(4), table.grouptable td:nth-child(4){width:13%}
table.grouptable th{text-align:left;padding:.4rem .6rem;font-size:.66rem;color:var(--ink-faint);text-transform:uppercase;
  letter-spacing:.05em;font-weight:600;border-bottom:1px solid var(--border);white-space:nowrap}
table.grouptable td{padding:.5rem .6rem;vertical-align:top;border-bottom:1px solid var(--border)}
table.grouptable tr:last-child td{border-bottom:none}
table.grouptable td.tk{font-family:"IBM Plex Mono",monospace;font-weight:600;white-space:nowrap}
table.grouptable td.tk a{color:inherit;text-decoration:none}
table.grouptable td.tk a:hover{color:var(--accent)}
table.grouptable td.cname{color:var(--ink-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
table.grouptable td.desc{color:var(--ink-soft);line-height:1.4;min-width:16rem}
table.grouptable td.desc .point{margin-bottom:.35rem}
table.grouptable td.desc .point:last-child{margin-bottom:0}
table.grouptable td.lastdate{font-family:"IBM Plex Mono",monospace;font-size:.76rem;color:var(--ink-faint);white-space:nowrap}

.unresolvedbox{background:var(--surface);border:1px dashed var(--border);border-radius:8px;padding:.9rem 1.1rem;margin-top:1rem;font-size:.87rem;color:var(--ink-soft)}
.unresolvedbox li{margin:.3rem 0}

.methodbox{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:6px;padding:1.1rem 1.3rem;margin:1.8rem 0;box-shadow:var(--shadow)}
.methodbox p{font-size:.9rem;margin:.4rem 0}
.methodbox ul{margin:.4rem 0;padding-left:1.2rem;font-size:.88rem;color:var(--ink-soft)}
.methodbox li{margin:.35rem 0}

footer{margin-top:3rem;padding-top:1.4rem;border-top:1px solid var(--border);font-size:.8rem;color:var(--ink-faint)}

@media (max-width:640px){h1{font-size:1.9rem}}
</style>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">

<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Craig's Bear Map</h1>
      <p class="subtitle">##SUBTITLE##</p>
    </div>
    <div class="topbar-right">
      <button class="copybtn" id="copyBtn">📋 Copy TradingView list</button>
      <button class="themebtn" id="themeBtn" title="Toggle light/dark">🌙</button>
    </div>
  </div>
##STATROW##

##STACKBAR##
##SECTORFILTER##

##MATERIALS_BLOCK##

##SECTOR_BLOCKS##

##UNRESOLVED_BOX##

  <footer>
    ##FOOTER_TEXT##
  </footer>
</div>

<script>
##DATA_JS##
##COLOR_BY_GROUP_JS##
function fmtDate(iso){
  const d = new Date(iso+'T00:00:00');
  return d.toLocaleDateString('en-AU', {day:'numeric', month:'short', year:'numeric'});
}
Object.entries(DATA).forEach(([id, tickers]) => {
  const el = document.getElementById(id);
  if(!el) return;
  const rows = tickers.map(([tk, name, desc, date]) => {
    const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${encodeURIComponent(tk)}`;
    const points = desc.split(' · ').map(p => p.trim()).filter(Boolean)
      .map(p => `<div class="point">• ${p}</div>`).join('');
    return `<tr>
      <td class="tk"><a href="${tvUrl}" target="_blank" rel="noopener noreferrer">${tk}</a></td>
      <td class="cname">${name || '—'}</td>
      <td class="desc">${points}</td>
      <td class="lastdate">${fmtDate(date)}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table class="grouptable">
    <thead><tr><th>Ticker</th><th>Company</th><th>Comments</th><th>Last Bearish</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
});

// Flat ticker list reused from DATA above, for the copy button.
const TICKER_INFO = {};
Object.entries(DATA).forEach(([id, tickers]) => {
  tickers.forEach(([tk, desc]) => { TICKER_INFO[tk] = {desc}; });
});

document.getElementById('copyBtn').addEventListener('click', () => {
  const tickers = Object.keys(TICKER_INFO);
  const text = tickers.map(tk => `ASX:${tk}`).join(',');
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copyBtn');
    btn.classList.add('copied');
    btn.textContent = `✓ Copied ${tickers.length} tickers`;
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋 Copy TradingView list'; }, 1800);
  });
});

const activeSectors = new Set();
const sectorFilter = document.getElementById('sectorFilter');
function applySectorFilter(){
  document.querySelectorAll('.sector').forEach(sec => {
    sec.style.display = (activeSectors.size === 0 || activeSectors.has(sec.dataset.sector)) ? '' : 'none';
  });
  document.querySelectorAll('#sectorFilter .sectorchip').forEach(c => c.classList.toggle('active', activeSectors.has(c.dataset.sector)));
}
sectorFilter.addEventListener('click', (e) => {
  const btn = e.target.closest('.sectorchip');
  if(!btn) return;
  const s = btn.dataset.sector;
  if(activeSectors.has(s)) activeSectors.delete(s); else activeSectors.add(s);
  applySectorFilter();
});

// Sub-filter within Materials only - lets you narrow the commodity group
// tables (Gold/Copper/Silver/...) down to one or more, the same
// multi-select toggle pattern as the top-level sector filter. Scoped to
// its own #commodityFilter container/".commgroup" elements so it never
// interferes with the sector filter above, even though both reuse the
// same ".sectorchip" pill styling.
const activeCommodities = new Set();
const commodityFilter = document.getElementById('commodityFilter');
if (commodityFilter) {
  function applyCommodityFilter(){
    document.querySelectorAll('.commgroup').forEach(g => {
      g.style.display = (activeCommodities.size === 0 || activeCommodities.has(g.dataset.commodity)) ? '' : 'none';
    });
    document.querySelectorAll('#commodityFilter .sectorchip').forEach(c => c.classList.toggle('active', activeCommodities.has(c.dataset.commodity)));
  }
  commodityFilter.addEventListener('click', (e) => {
    const btn = e.target.closest('.sectorchip');
    if(!btn) return;
    const c = btn.dataset.commodity;
    if(activeCommodities.has(c)) activeCommodities.delete(c); else activeCommodities.add(c);
    applyCommodityFilter();
  });
}

const themeBtn = document.getElementById('themeBtn');
function applyTheme(t){
  if(t){ document.documentElement.setAttribute('data-theme', t); } else { document.documentElement.removeAttribute('data-theme'); }
  themeBtn.textContent = (t==='dark') ? '☀️' : '🌙';
}
try{
  const saved = localStorage.getItem('bullmap-theme');
  if(saved) applyTheme(saved);
}catch(e){}
themeBtn.addEventListener('click', ()=>{
  const current = document.documentElement.getAttribute('data-theme');
  const next = current==='dark' ? 'light' : 'dark';
  applyTheme(next);
  try{ localStorage.setItem('bullmap-theme', next); }catch(e){}
});
</script>
"""


def main():
    print("Fetching ASX universe for ticker resolution/sector/name lookup...")
    universe = hh.get_asx_universe()

    print("Loading tracker...")
    records = load_tracker_records(universe)
    print(f"  {len(records)} rows from Craig's 3 shows")

    print("Deriving current-bearish survivors...")
    survivors = derive_survivors(records)
    print(f"  {len(survivors)} tickers currently Bearish/Bearish (Unchanged)")

    print("Classifying commodities / sectors...")
    groups, bucket_counts, unresolved = classify_and_group(survivors, universe)

    distinct_tickers_seen = len({r["ticker"] for r in records})
    html = render_html(groups, bucket_counts, unresolved, len(records), distinct_tickers_seen)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    total_bearish = sum(bucket_counts.values())
    print(f"\nDone. {total_bearish} tickers across {len(groups)} groups written to {OUT_PATH}")
    if unresolved:
        print(f"  {len(unresolved)} unresolved: {[t for t, _ in unresolved]}")


if __name__ == "__main__":
    main()
