"""Rebuilds tech-index.html from scratch: reads the category classification
HH SCREENER already maintains, fetches a fresh name/market cap/price/
revenue/business-summary snapshot for every ticker via yfinance, and
renders the static page from tech_index_template.html.

Runs on the same schedule as build_materials_index.py/
build_healthcare_index.py/build_energy_index.py (see
.github/workflows/materials_index.yml) as its own isolated job.

Unlike Materials/Healthcare/Energy, Info Tech companies don't have a
natural explorer/producer or clinical-trial lifecycle - nearly all of them
(130/138 in the initial universe) already report some real revenue, just
across a huge range ($11K to $2.75B). "Stage" here is a data-driven
revenue tier instead of a keyword-inferred lifecycle stage - a more
honest signal for this sector than trying to force a mining/pharma-style
ladder onto it.
"""
import datetime
import json
import time

import yfinance as yf
from dashboard_template import render_nav

CATEGORY_CACHE = "tech_category_cache.json"
TEMPLATE = "tech_index_template.html"
OUTPUT = "tech-index.html"

REVENUE_TIERS = [
    (1_000_000, "Pre-Revenue (<$1M)"),
    (10_000_000, "Early Revenue ($1-10M)"),
    (50_000_000, "Scaling ($10-50M)"),
]
REVENUE_TIER_TOP = "Established ($50M+)"


def classify_revenue_tier(revenue, resolved):
    if not resolved:
        return "Unresolved"
    if revenue is None:
        return "Unresolved"
    for threshold, label in REVENUE_TIERS:
        if revenue < threshold:
            return label
    return REVENUE_TIER_TOP


def fetch_one(ticker):
    try:
        info = yf.Ticker(ticker + ".AX").get_info()
    except Exception:
        return {"name": None, "mcap": None, "revenue": None, "change1d": None,
                "high52w": None, "price": None, "summary": None}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "mcap": info.get("marketCap") or info.get("nonDilutedMarketCap"),
        "revenue": info.get("totalRevenue"),
        "change1d": info.get("regularMarketChangePercent"),
        "high52w": info.get("fiftyTwoWeekHigh"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "summary": info.get("longBusinessSummary"),
    }


def esc_js(s):
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    with open(CATEGORY_CACHE) as f:
        category = json.load(f)

    rows = []
    total = len(category)
    for i, ticker in enumerate(sorted(category), 1):
        label = category[ticker]
        info = fetch_one(ticker)
        resolved = bool(info.get("name") or info.get("mcap"))
        stage = classify_revenue_tier(info.get("revenue"), resolved)
        mcap = info.get("mcap")
        change1d = info.get("change1d")
        price = info.get("price")
        high52w = info.get("high52w")
        rows.append((ticker, info.get("name"), label, stage, mcap, price, change1d, high52w))
        if i % 50 == 0:
            print(f"  ...{i}/{total} fetched")
        time.sleep(0.1)

    print(f"Fetched {total} tickers.")
    resolved_count = sum(1 for r in rows if r[4] is not None)
    stage_counts = {}
    for r in rows:
        stage_counts[r[3]] = stage_counts.get(r[3], 0) + 1
    print(f"Resolved market cap for {resolved_count}/{total}.")
    print(f"Revenue tier breakdown: {stage_counts}")

    data_lines = []
    for ticker, name, label, stage, mcap, price, change1d, high52w in rows:
        mcap_s = str(int(mcap)) if mcap else "null"
        price_s = str(price) if price is not None else "null"
        chg_s = str(round(change1d, 1)) if change1d is not None else "null"
        high52w_s = str(high52w) if high52w is not None else "null"
        data_lines.append(
            f'["{esc_js(ticker)}","{esc_js(name)}","{esc_js(label)}","{esc_js(stage)}",{mcap_s},{price_s},{chg_s},{high52w_s}]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    build_date = datetime.date.today().strftime("%-d %b %Y")
    html = html.replace("<<<BUILD_DATE>>>", build_date)
    html = html.replace("<<<NAV_HTML>>>", render_nav(OUTPUT))

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
