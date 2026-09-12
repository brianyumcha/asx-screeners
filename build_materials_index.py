"""Rebuilds materials-index.html from scratch: reads the commodity
classification HH SCREENER already maintains, fetches a fresh name/market
cap/revenue snapshot for every ticker via yfinance, and renders the static
page from materials_index_template.html.

Runs weekly (see .github/workflows/materials_index.yml), not intraday, so a
straightforward sequential fetch with a small per-request delay is fine here
-- it doesn't share the same run's rate-limit budget with the daily
OBV/Pullback/HH screener fetches (see rs_utils.py's save_benchmark_cache
docstring for why that distinction matters on this codebase).
"""
import datetime
import json
import time

import yfinance as yf

COMMODITY_CACHE = "materials_commodity_cache.json"
TEMPLATE = "materials_index_template.html"
OUTPUT = "materials-index.html"
REVENUE_PRODUCER_THRESHOLD = 100_000  # AUD; below this treated as pre-revenue


def fetch_one(ticker):
    try:
        info = yf.Ticker(ticker + ".AX").get_info()
    except Exception:
        return {"name": None, "mcap": None, "revenue": None}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "mcap": info.get("marketCap"),
        "revenue": info.get("totalRevenue"),
    }


def stage_for(info):
    rev = info.get("revenue")
    resolved = info.get("name") or info.get("mcap")
    if rev is not None and rev > REVENUE_PRODUCER_THRESHOLD:
        return "Producer"
    if resolved:
        return "Explorer / Developer"
    return "Unresolved"


def esc_js(s):
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    with open(COMMODITY_CACHE) as f:
        commodity = json.load(f)

    rows = []
    total = len(commodity)
    for i, ticker in enumerate(sorted(commodity), 1):
        label = commodity[ticker]
        info = fetch_one(ticker)
        stage = stage_for(info)
        mcap = info.get("mcap")
        rows.append((ticker, info.get("name"), label, stage, mcap))
        if i % 50 == 0:
            print(f"  ...{i}/{total} fetched")
        time.sleep(0.1)

    print(f"Fetched {total} tickers.")
    resolved = sum(1 for r in rows if r[4] is not None)
    print(f"Resolved market cap for {resolved}/{total}.")

    data_lines = []
    for ticker, name, label, stage, mcap in rows:
        mcap_str = str(int(mcap)) if mcap else "null"
        data_lines.append(
            f'["{esc_js(ticker)}","{esc_js(name)}","{esc_js(label)}","{esc_js(stage)}",{mcap_str}]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    build_date = datetime.date.today().strftime("%-d %b %Y")
    html = html.replace("<<<BUILD_DATE>>>", build_date)

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
