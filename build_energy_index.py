"""Rebuilds energy-index.html from scratch: reads the fuel-type
classification HH SCREENER already maintains, fetches a fresh name/market
cap/price/revenue/business-summary snapshot for every ticker via yfinance,
and renders the static page from energy_index_template.html.

Runs on the same schedule as build_materials_index.py/
build_healthcare_index.py (see .github/workflows/materials_index.yml) as
its own isolated job.
"""
import datetime
import json
import re
import time

import yfinance as yf

FUEL_CACHE = "energy_fuel_cache.json"
TEMPLATE = "energy_index_template.html"
OUTPUT = "energy-index.html"
REVENUE_OVERRIDE = 10_000_000  # AUD - same threshold Materials uses

# Hand-verified stage corrections - checked against the real company, not
# guessed.
# - ERA/Energy Resources of Australia: "engages in mine rehabilitation...
#   rehabilitates the Ranger project area, a uranium mine" - explicitly
#   NOT producing (Ranger ceased mining in 2021, now decommissioning). Its
#   real ~$52M revenue (from water-treatment/rehabilitation operations,
#   not ore sales) tripped the revenue-override threshold, the same
#   PDI-style failure mode Materials hit - a large real revenue number
#   with zero keyword-producer signal, from a business that isn't mining.
# - T92/Terra Critical Minerals: matched "production of" purely from
#   generic company-purpose boilerplate ("engages in the exploration and
#   production of mineral projects") - the rest of the summary describes
#   only exploration activity with $0/unresolved revenue. Formerly "Terra
#   Uranium Limited", renamed Sept 2025.
# Both confirmed 2026-09-14.
MANUAL_STAGE_OVERRIDES = {
    "ERA": "Care & Maintenance",
    "T92": "Explorer / Developer",
}

# Labels that don't follow the Explorer -> Producer lifecycle at all -
# downstream/services/utility businesses, not upstream project developers.
NON_LIFECYCLE_LABELS = {
    "Refining & Marketing", "Energy Services", "Energy",
}

# Phrases that mean "producer" when a company's own description uses them -
# same list Materials uses, since the language a mining/energy company
# uses to describe reaching production is essentially identical.
PRODUCER_SIGNALS = [
    r"\bproduces\b", r"\bproduction of\b", r"and sale of", r"and sells\b",
    r"markets?,? and (export|sell)", r"mines and (processes|sells)",
    r"is a \w+ producer", r"production company", r"commercial production",
    r"mine development and operation", r"mining and processing (and sale )?of",
    r"mining, smelting,? (and )?refining",
    r"operates (the|a|several|two|three|four) [\w\s\-]{0,40}(mine|mines|plant|smelter|refinery|operation|field|fields|well|wells)\b",
    r"produces?,? develops?,? and (sells?|markets?) (oil|gas|petroleum)",
    r"oil and gas production",
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
        "mcap": info.get("marketCap") or info.get("nonDilutedMarketCap"),
        "revenue": info.get("totalRevenue"),
        "change1d": info.get("regularMarketChangePercent"),
        "high52w": info.get("fiftyTwoWeekHigh"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "summary": info.get("longBusinessSummary"),
    }


def classify_stage(summary, revenue, resolved, label=None, ticker=None):
    if ticker in MANUAL_STAGE_OVERRIDES:
        return MANUAL_STAGE_OVERRIDES[ticker]
    if label is not None and any(part.strip() in NON_LIFECYCLE_LABELS for part in label.split("+")):
        return "—"
    s = (summary or "").lower()
    if "care and maintenance" in s or "care & maintenance" in s:
        return "Care & Maintenance"
    aspirational = bool(ASPIRATIONAL_RE.search(s))
    keyword_producer = (not aspirational) and any(re.search(sig, s) for sig in PRODUCER_SIGNALS)
    # Same revenue floor Materials uses on the keyword-producer signal -
    # a bare keyword match alone isn't trustworthy at very low revenue.
    MIN_KEYWORD_REVENUE = 2_000_000
    if keyword_producer and revenue is not None and revenue < MIN_KEYWORD_REVENUE:
        keyword_producer = False
    keyword_explorer = bool(re.search(r"exploration|explores|development", s)) and not keyword_producer

    if keyword_producer:
        return "Producer"
    if revenue is not None and revenue >= REVENUE_OVERRIDE:
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
    with open(FUEL_CACHE) as f:
        fuel = json.load(f)

    rows = []
    total = len(fuel)
    for i, ticker in enumerate(sorted(fuel), 1):
        label = fuel[ticker]
        info = fetch_one(ticker)
        resolved = bool(info.get("name") or info.get("mcap"))
        stage = classify_stage(info.get("summary"), info.get("revenue"), resolved, label, ticker)
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
    print(f"Stage breakdown: {stage_counts}")

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

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
