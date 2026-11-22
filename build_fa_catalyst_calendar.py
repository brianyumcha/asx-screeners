"""Rebuilds fa-catalyst-calendar.html: a calendar of real, still-pending
fundamental-analysis catalysts (resource upgrades, feasibility studies,
capital raises, drilling results, trial/regulatory readouts, M&A
situations, FID/production milestones) across a watchlist of ASX tickers.

Source of the underlying data (fa_catalyst_calendar.json): two merged
sources, both deliberately excluding anything already resolved or whose
own stated timing window has clearly lapsed:
  1. An internal archive of FA-focused call summaries this site already
     tracks for other pages (see build_mtm_archive.py) - each ticker's
     most recent mention for a still-open catalyst, manually judged (not
     keyword-matching alone, which produced real false positives on
     things already resolved by the time a later call rolled around).
  2. bullmap_catalysts.json - mined from each ticker's own latest ASX
     quarterly/presentation (see Instructions/bullmap_catalysts_
     instructions.md), merged in with a keyword-inferred catalyst_type
     and de-duplicated against (1) where the same underlying event was
     independently present in both.
The published page intentionally doesn't name either source - it's
presented as a generic FA catalyst calendar. Company name and sector/
commodity/indication context per ticker comes from a lookup join against
asx_universe_fallback.json plus the materials/healthcare specialty caches
(see load_company_lookup()) - no new research, just joining data this
repo already has.

This is a one-shot build for now (no live re-scan each run) - re-run the
underlying curation/mining by hand to refresh fa_catalyst_calendar.json
when it's worth doing again, then just re-run this script.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from dashboard_template import render_nav

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DATA_PATH = "fa_catalyst_calendar.json"
TEMPLATE = "fa_catalyst_calendar_template.html"
OUTPUT = "fa-catalyst-calendar.html"

TYPE_LABELS = {
    "mre_upgrade": "MRE / Resource Upgrade",
    "dfs_bfs_scoping": "BFS / DFS / Scoping Study",
    "capital_raise": "Capital Raise",
    "metallurgy": "Metallurgical Test Work",
    "drilling_assay": "Drilling / Assay Results",
    "trial_regulatory": "Trial Data / Regulatory Clearance",
    "board_ma": "Activist / Board / M&A",
    "production_fid": "FID / Production",
    "other": "Other",
}

# Mining and biotech catalyst types read as sector-specific; Capital Raise and
# Activist/Board/M&A apply equally to either (and "other" is a true catch-all),
# so those get their own "General" cluster rather than being forced into one
# of the two sector groups.
TYPE_GROUPS = [
    ("Mining", ["mre_upgrade", "dfs_bfs_scoping", "drilling_assay", "metallurgy", "production_fid"]),
    ("Biotech", ["trial_regulatory"]),
    ("General", ["capital_raise", "board_ma", "other"]),
]


def esc_js(s):
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def load_company_lookup():
    """Ticker -> (name, sector_label). Prefers the specific commodity/indication
    caches over the broad GICS sector when one exists, since "Lithium" tells a
    reader more than "Materials" does - falls back to the plain sector if a
    ticker isn't in either specialised cache (most non-resource/non-health
    sectors), and to nothing at all if the ticker is missing from the universe
    file entirely (a ~2100-ticker list, not guaranteed complete)."""
    universe = json.load(open("asx_universe_fallback.json", encoding="utf-8"))
    commodity = json.load(open("materials_commodity_cache.json", encoding="utf-8"))
    indication = json.load(open("healthcare_indication_cache.json", encoding="utf-8"))
    lookup = {}
    for tk, info in universe.items():
        name = info.get("name")
        sector = info.get("sector")
        if tk in commodity:
            label = commodity[tk]
        elif tk in indication:
            label = indication[tk]
        else:
            label = sector
        lookup[tk] = (name, label)
    return lookup


def load_price_lookup():
    """Ticker -> (last_close, as_of_date) from the same daily OHLCV cache the
    rest of this site's screeners already maintain - no new data pull, just
    the most recent row per ticker. Missing from the cache (not guaranteed
    complete) -> absent from the dict, rendered as "-" rather than guessed."""
    df = pd.read_parquet("price_cache.parquet")
    latest = df.sort_values("date").groupby("ticker").tail(1)
    return {
        row["ticker"]: (float(row["close"]), row["date"].strftime("%Y-%m-%d"))
        for _, row in latest.iterrows()
    }


def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        catalysts = json.load(f)
    company_lookup = load_company_lookup()
    price_lookup = load_price_lookup()

    rows = []
    for ticker, items in catalysts.items():
        name, sector = company_lookup.get(ticker, (None, None))
        last_close, close_date = price_lookup.get(ticker, (None, None))
        for it in items:
            rows.append({
                "ticker": ticker,
                "company": name,
                "sector": sector,
                "catalyst": it["catalyst"],
                "catalyst_type": it["catalyst_type"],
                "timing": it.get("timing") or "",
                "sort": it.get("sort"),
                "day_precise": bool(it.get("day_precise")),
                "estimated": bool(it.get("estimated")),
                "last_close": last_close,
                "close_date": close_date,
            })

    # Undated rows sort to the end. Within the same month, an estimated
    # (quarter/half/vague-year) row sorts after a non-estimated one, since a
    # month bucket is a mix of "this is genuinely when it's due" and "this is
    # our best guess at the middle of a wider window" - estimated rows read
    # as the softer, lower-confidence tail of that same month rather than
    # being interleaved as if equally precise.
    rows.sort(key=lambda r: (
        r["sort"] is None,
        (r["sort"] or "")[:7],  # YYYY-MM - groups by month without breaking ties on exact day
        r["estimated"],
        not r["day_precise"],
        r["ticker"],
    ))

    print(f"Loaded {len(rows)} catalysts across {len(catalysts)} tickers.")

    type_counts = {}
    for r in rows:
        type_counts[r["catalyst_type"]] = type_counts.get(r["catalyst_type"], 0) + 1
    group_blocks = []
    for group_label, type_keys in TYPE_GROUPS:
        present = [t for t in type_keys if type_counts.get(t)]
        if not present:
            continue
        chips = "".join(
            f'<button class="commchip" data-type="{t}"><span>{TYPE_LABELS.get(t, t)}</span>'
            f'<span class="cnt">{type_counts[t]}</span></button>'
            for t in sorted(present, key=lambda t: (-type_counts[t], t))
        )
        group_blocks.append(
            f'<div class="chipgroup"><span class="glabel">{group_label}</span>{chips}</div>'
        )
    type_chips_html = "".join(group_blocks)

    data_lines = []
    for r in rows:
        data_lines.append(
            "[" + ",".join([
                f'"{esc_js(r["ticker"])}"',
                f'"{esc_js(r["company"])}"',
                f'"{esc_js(r["sector"])}"',
                f'"{esc_js(r["catalyst"])}"',
                f'"{r["catalyst_type"]}"',
                f'"{esc_js(TYPE_LABELS.get(r["catalyst_type"], r["catalyst_type"]))}"',
                f'"{esc_js(r["timing"])}"',
                "null" if r["sort"] is None else f'"{r["sort"]}"',
                "true" if r["day_precise"] else "false",
                "true" if r["estimated"] else "false",
                "null" if r["last_close"] is None else f'{r["last_close"]:.4f}',
                "null" if r["close_date"] is None else f'"{r["close_date"]}"',
            ]) + "]"
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    for placeholder in ("<<<FULL_DATA>>>", "<<<BUILD_DATE>>>", "<<<TYPE_CHIPS>>>", "<<<NAV_HTML>>>", "<<<TICKER_COUNT>>>"):
        assert placeholder in template, f"template placeholder missing: {placeholder}"

    now_syd = datetime.now(SYDNEY_TZ)
    html_out = template.replace("<<<FULL_DATA>>>", data_block)
    html_out = html_out.replace("<<<BUILD_DATE>>>", now_syd.strftime("%-d %b %Y, %-I:%M%p"))
    html_out = html_out.replace("<<<TYPE_CHIPS>>>", type_chips_html)
    html_out = html_out.replace("<<<NAV_HTML>>>", render_nav(OUTPUT))
    html_out = html_out.replace("<<<TICKER_COUNT>>>", str(len(catalysts)))

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Wrote {OUTPUT} ({len(html_out)} bytes).")


if __name__ == "__main__":
    main()
