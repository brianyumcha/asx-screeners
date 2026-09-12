"""Rebuilds materials-index.html from scratch: reads the commodity
classification HH SCREENER already maintains, fetches a fresh name/market
cap/price/revenue/business-summary snapshot for every ticker via yfinance,
and renders the static page from materials_index_template.html.

Runs daily after ASX close (see .github/workflows/materials_index.yml) as
its own isolated job, so it doesn't share the same run's rate-limit budget
with the intraday OBV/Pullback/HH screener fetches (see rs_utils.py's
save_benchmark_cache docstring for why that distinction matters here).
"""
import datetime
import json
import re
import time

import yfinance as yf

COMMODITY_CACHE = "materials_commodity_cache.json"
TEMPLATE = "materials_index_template.html"
OUTPUT = "materials-index.html"
REVENUE_OVERRIDE = 10_000_000  # AUD - large enough that it can't be incidental interest/fee income

# Phrases that mean "producer" when a company's own description uses them.
# Built from spot-checking known producers/explorers against real
# longBusinessSummary text (2026-09-13) - see the corrected AAJ case (a
# pure explorer whose $900k incidental revenue alone had wrongly triggered
# "Producer" under an earlier, revenue-only version of this classifier).
PRODUCER_SIGNALS = [
    r"\bproduces\b", r"\bproduction of\b", r"and sale of", r"and sells\b",
    r"markets?,? and (export|sell)", r"mines and (processes|sells)",
    r"is a \w+ producer", r"production company", r"commercial production",
    r"mine development and operation", r"mining and processing (and sale )?of",
    r"mining, smelting,? (and )?refining",
    r"operates (the|a|several|two|three|four) [\w\s\-]{0,40}(mine|mines|plant|smelter|refinery|operation)\b",
]
ASPIRATIONAL_RE = re.compile(r"(towards|toward|targets?|aims? (for|to)|planned|future) production")


def fetch_one(ticker):
    try:
        info = yf.Ticker(ticker + ".AX").get_info()
    except Exception:
        return {"name": None, "mcap": None, "revenue": None, "change1d": None,
                "high52w": None, "price": None, "summary": None}
    return {
        "name": info.get("longName") or info.get("shortName"),
        # Yahoo's own quoteSummary has been intermittently omitting "marketCap"
        # for some tickers (BSL/NUF/ORI/ALK confirmed 2026-09-13) while still
        # returning an equivalent value under "nonDilutedMarketCap" in the
        # same payload - fall back to that rather than a second network call.
        "mcap": info.get("marketCap") or info.get("nonDilutedMarketCap"),
        "revenue": info.get("totalRevenue"),
        "change1d": info.get("regularMarketChangePercent"),
        "high52w": info.get("fiftyTwoWeekHigh"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "summary": info.get("longBusinessSummary"),
    }


def classify_stage(summary, revenue, resolved):
    s = (summary or "").lower()
    if "care and maintenance" in s or "care & maintenance" in s:
        return "Care & Maintenance"
    aspirational = bool(ASPIRATIONAL_RE.search(s))
    keyword_producer = (not aspirational) and any(re.search(sig, s) for sig in PRODUCER_SIGNALS)
    keyword_explorer = bool(re.search(r"exploration|explores|development", s)) and not keyword_producer

    if keyword_producer:
        return "Producer"
    if revenue is not None and revenue >= REVENUE_OVERRIDE:
        # Revenue too large to be incidental - overrides vague/generic
        # summary language (e.g. BHP/PLS-style summaries that never use an
        # explicit "produces X" phrase despite being major producers).
        return "Producer"
    if keyword_explorer:
        return "Explorer / Developer"
    if not resolved:
        return "Unresolved"
    return "Explorer / Developer"


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
        resolved = bool(info.get("name") or info.get("mcap"))
        stage = classify_stage(info.get("summary"), info.get("revenue"), resolved)
        mcap = info.get("mcap")
        change1d = info.get("change1d")
        price = info.get("price")
        high52w = info.get("high52w")
        off_high = None
        if price is not None and high52w:
            off_high = round((price - high52w) / high52w * 100, 1)
        rows.append((ticker, info.get("name"), label, stage, mcap, change1d, off_high))
        if i % 50 == 0:
            print(f"  ...{i}/{total} fetched")
        time.sleep(0.1)

    print(f"Fetched {total} tickers.")
    resolved_count = sum(1 for r in rows if r[4] is not None)
    stage_counts = {}
    for r in rows:
        stage_counts[r[3]] = stage_counts.get(r[3], 0) + 1
    print(f"Resolved market cap for {resolved_count}/{total}.")
    print(f"Stage breakdown: {stage_counts}")

    data_lines = []
    for ticker, name, label, stage, mcap, change1d, off_high in rows:
        mcap_s = str(int(mcap)) if mcap else "null"
        chg_s = str(round(change1d, 1)) if change1d is not None else "null"
        off_s = str(off_high) if off_high is not None else "null"
        data_lines.append(
            f'["{esc_js(ticker)}","{esc_js(name)}","{esc_js(label)}","{esc_js(stage)}",{mcap_s},{chg_s},{off_s}]'
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
