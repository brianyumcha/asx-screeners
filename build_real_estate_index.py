"""Rebuilds real-estate-index.html from scratch: reads the REIT/Real-Estate
category classification HH SCREENER already maintains, fetches a fresh
name/market cap/price/yield snapshot for every ticker via yfinance, and
renders the static page from real_estate_index_template.html.

Runs on the same schedule as build_materials_index.py/
build_healthcare_index.py/build_energy_index.py/build_tech_index.py (see
.github/workflows/materials_index.yml) as its own isolated job.

Unlike Materials/Healthcare/Energy, Real Estate has no natural explorer/
producer or clinical-trial lifecycle - nearly every ticker here is already
a revenue-generating trust or developer. Rather than force an invented
"Stage" onto it (the way Tech's revenue-tier ladder does), this index
skips Stage entirely and shows distribution yield instead - the metric
that actually matters for this sector.
"""
import datetime
import json
import time

import yfinance as yf

CATEGORY_CACHE = "realestate_category_cache.json"
TEMPLATE = "real_estate_index_template.html"
OUTPUT = "real-estate-index.html"


def fetch_one(ticker):
    try:
        info = yf.Ticker(ticker + ".AX").get_info()
    except Exception:
        return {"name": None, "mcap": None, "change1d": None,
                "high52w": None, "price": None, "yield": None}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "mcap": info.get("marketCap") or info.get("nonDilutedMarketCap"),
        "change1d": info.get("regularMarketChangePercent"),
        "high52w": info.get("fiftyTwoWeekHigh"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "yield": info.get("dividendYield"),
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
        mcap = info.get("mcap")
        change1d = info.get("change1d")
        price = info.get("price")
        high52w = info.get("high52w")
        yld = info.get("yield")
        rows.append((ticker, info.get("name"), label, mcap, price, change1d, high52w, yld))
        if i % 50 == 0:
            print(f"  ...{i}/{total} fetched")
        time.sleep(0.1)

    print(f"Fetched {total} tickers.")
    resolved_count = sum(1 for r in rows if r[3] is not None)
    print(f"Resolved market cap for {resolved_count}/{total}.")

    data_lines = []
    for ticker, name, label, mcap, price, change1d, high52w, yld in rows:
        mcap_s = str(int(mcap)) if mcap else "null"
        price_s = str(price) if price is not None else "null"
        chg_s = str(round(change1d, 1)) if change1d is not None else "null"
        high52w_s = str(high52w) if high52w is not None else "null"
        # yfinance's dividendYield is already a percent (e.g. 5.3, not 0.053)
        yield_s = str(round(yld, 2)) if yld is not None else "null"
        data_lines.append(
            f'["{esc_js(ticker)}","{esc_js(name)}","{esc_js(label)}",{mcap_s},{price_s},{chg_s},{high52w_s},{yield_s}]'
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
