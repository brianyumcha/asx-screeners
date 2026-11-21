"""Rebuilds fa-catalyst-calendar.html: a calendar of real, still-pending
fundamental-analysis catalysts (resource upgrades, feasibility studies,
capital raises, drilling results, trial/regulatory readouts, M&A
situations, FID/production milestones) across a watchlist of ASX tickers.

Source of the underlying data (fa_catalyst_calendar.json): mined from an
internal archive of FA-focused call summaries this site already tracks for
other pages (see build_mtm_archive.py), by reading each ticker's most
recent mention for a still-open, forward-looking catalyst and manually
judging - not keyword-matching alone, which produced real false positives
on things already resolved by the time a later call rolled around - which
ones were genuinely still pending as of 2026-11-21. Deliberately excludes
anything that has already happened or whose own stated timing window has
clearly lapsed with no later confirmation, same discipline the rest of
this site's catalyst tracking already follows (see bullmap_catalysts.json/
Instructions/bullmap_catalysts_instructions.md for the sibling pipeline
this one's schema borrows from). The published page intentionally doesn't
name the source - it's presented as a generic FA catalyst calendar.

This is a one-shot build for now (no live re-scan each run) - re-run
_mine_fa_catalysts-style curation by hand to refresh fa_catalyst_calendar.json
when it's worth doing again, then just re-run this script.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

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
}


def esc_js(s):
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        catalysts = json.load(f)

    rows = []
    for ticker, items in catalysts.items():
        for it in items:
            rows.append({
                "ticker": ticker,
                "catalyst": it["catalyst"],
                "catalyst_type": it["catalyst_type"],
                "timing": it.get("timing") or "",
                "sort": it.get("sort"),
                "source": it.get("source", ""),
            })

    # Undated rows sort to the end, newest/soonest-dated first among the rest.
    rows.sort(key=lambda r: (r["sort"] is None, r["sort"] or "", r["ticker"]))

    print(f"Loaded {len(rows)} catalysts across {len(catalysts)} tickers.")

    type_counts = {}
    for r in rows:
        type_counts[r["catalyst_type"]] = type_counts.get(r["catalyst_type"], 0) + 1
    type_chip_lines = []
    for t in sorted(type_counts, key=lambda t: (-type_counts[t], t)):
        label = TYPE_LABELS.get(t, t)
        type_chip_lines.append(
            f'<button class="commchip" data-type="{t}"><span>{label}</span><span class="cnt">{type_counts[t]}</span></button>'
        )
    type_chips_html = "".join(type_chip_lines)

    data_lines = []
    for r in rows:
        data_lines.append(
            "[" + ",".join([
                f'"{esc_js(r["ticker"])}"',
                f'"{esc_js(r["catalyst"])}"',
                f'"{r["catalyst_type"]}"',
                f'"{esc_js(TYPE_LABELS.get(r["catalyst_type"], r["catalyst_type"]))}"',
                f'"{esc_js(r["timing"])}"',
                "null" if r["sort"] is None else f'"{r["sort"]}"',
                f'"{esc_js(r["source"])}"',
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
