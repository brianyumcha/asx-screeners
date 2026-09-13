"""Rebuilds healthcare-index.html from scratch: reads the indication
classification HH SCREENER already maintains, fetches a fresh name/market
cap/price/revenue/business-summary snapshot for every ticker via yfinance,
and renders the static page from healthcare_index_template.html.

Runs on the same schedule as build_materials_index.py (see
.github/workflows/materials_index.yml) as its own isolated job.
"""
import datetime
import json
import re
import time

import yfinance as yf

INDICATION_CACHE = "healthcare_indication_cache.json"
TEMPLATE = "healthcare_index_template.html"
OUTPUT = "healthcare-index.html"

# Hand-verified corrections the keyword/regex stage classifier gets wrong -
# checked against the real company, not guessed. Empty for now; add entries
# here the same way MANUAL_INDICATION_OVERRIDES works in HH SCREENER.py,
# once a specific ticker is confirmed wrong.
MANUAL_STAGE_OVERRIDES = {}

# Labels that come from HEALTHCARE_BUSINESS_TYPE_MAP's fallback path (or a
# disease-adjacent-but-non-pharma category) rather than a real drug/device
# indication - these don't run a Phase 1/2/3 clinical program by the
# nature of the business, so they get "-" instead of a guess once every
# other signal (explicit phase mention, revenue) has already been checked
# and came up empty.
NON_CLINICAL_FALLBACK_LABELS = {
    "Digital Health / Health IT", "Diagnostics & Pathology", "Medical Devices (General)",
    "Medical Distribution", "Care Facilities & Services", "Consumer Health & Wellness",
    "Pharmaceutical Manufacturing", "Diversified Healthcare", "Aged Care",
    "Animal Health", "Medicinal Cannabis", "Healthcare",
}

# Checked highest-phase-first so a company that mentions multiple programs
# at different phases ("completed Phase I ... now in Phase III") reports
# its most advanced one. Deliberately NOT \b-anchored after the numeral -
# a \b between a digit and a following letter never fires (both are word
# characters), so "Phase1a/1b" or "Phase 1b" (extremely common sub-phase
# notation - see PYC/PYC Therapeutics) would otherwise never match at all.
# Known residual limitation: a slash-combined trial like "Phase 1/2" only
# registers as Phase 1, since the "2" isn't immediately adjacent to
# "phase\s*" - undercounts to the earlier phase rather than guessing the
# later one, consistent with this classifier's conservative-floor default
# elsewhere. Confirmed 2026-09-14 that dropping the boundary check doesn't
# cause "phase iii" to register as anything other than 3 as its max: the
# phase-1/phase-2 alternatives ("i"/"ii") DO also incidentally match inside
# "iii", but since only the maximum matched phase is ever returned, that
# false extra low-phase match never changes the final answer.
PHASE_PATTERNS = {
    3: r"phase\s*(?:3|iii)",
    2: r"phase\s*(?:2|ii)",
    1: r"phase\s*(?:1|i)",
}
APPROVED_SIGNALS = [
    r"commercial-stage", r"commercialised its", r"commercialized its",
    r"fda-approved", r"tga-approved", r"approved by (?:the )?fda", r"approved by (?:the )?tga",
    r"marketed (?:product|products|therapy)",
]
# Large enough that it can't be incidental grant/interest income, but far
# below Materials' $10M threshold - a real but modest device/diagnostic
# product revenue stream (e.g. a single approved test or device line) is
# common at this scale for ASX healthcare companies well before they'd
# reach Materials-scale production economics.
HEALTHCARE_REVENUE_THRESHOLD = 5_000_000


def classify_clinical_stage(summary, revenue, resolved, label, ticker):
    if ticker in MANUAL_STAGE_OVERRIDES:
        return MANUAL_STAGE_OVERRIDES[ticker]
    if not resolved:
        return "Unresolved"
    s = (summary or "").lower()

    found_phases = [n for n, pat in PHASE_PATTERNS.items() if re.search(pat, s)]
    if found_phases:
        return f"Phase {max(found_phases)}"
    if re.search(r"pre-?clinical", s):
        return "Preclinical"
    if any(re.search(p, s) for p in APPROVED_SIGNALS):
        return "Approved / Commercial"
    if revenue is not None and revenue >= HEALTHCARE_REVENUE_THRESHOLD:
        return "Approved / Commercial"
    if label in NON_CLINICAL_FALLBACK_LABELS:
        return "—"
    # Described as "clinical-stage" but no specific phase number disclosed -
    # a conservative floor guess (at least dosing patients, not "Preclinical"
    # which implies no human trials yet at all).
    if "clinical-stage" in s or "clinical stage" in s:
        return "Phase 1"
    return "Preclinical"


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
    with open(INDICATION_CACHE) as f:
        indication = json.load(f)

    rows = []
    total = len(indication)
    for i, ticker in enumerate(sorted(indication), 1):
        label = indication[ticker]
        info = fetch_one(ticker)
        resolved = bool(info.get("name") or info.get("mcap"))
        stage = classify_clinical_stage(info.get("summary"), info.get("revenue"), resolved, label, ticker)
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
