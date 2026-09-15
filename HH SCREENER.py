"""
ASX Higher-High Screener (Sector Grid)
==============================
Python/HTML port of the "HH Indicator - ASX <sector> (BT)" family of
TradingView Pine scripts - one table per sector, every ticker shown, "NEW HH"
when the sector's structural-breakout pattern fires.

Signal logic (matches the FIXED Pine version, applied to Materials on
TradingView 2026-09-01 - see that script's changelog for the full writeup):
  - A pivot high is a 7-bar window (3 left, 3 right) local max of the candle
    BODY top (max(open,close), ignoring wicks), confirmed 3 bars after it forms.
  - The trigger fires when price closes back above the last confirmed pivot
    high (a structural break of the last swing high).
  - FIX (the bug Brian found on 2026-09-01): the same pivot could otherwise
    fire twice if price dipped back under it and re-crossed before a new
    pivot had time to confirm. This is guarded by remembering which pivot
    (by bar index, not price) has already fired - so a genuine later retest
    of the same price at a NEW pivot can still fire again, but a stale
    unconfirmed re-cross of the same pivot cannot.

This does NOT try to be a full Elliott Wave / market-structure validator -
same philosophy as the other two screeners: it's a mechanical, reliable
signal, not a claim of certainty about the wave count.

New relative to the Pine version:
  - Covers the FULL ASX (~2000+ tickers via SeaBee), not a hand-picked 40 per
    sector - grouped into the same 8 broad sectors as your TradingView setup.
  - Daily AND Weekly HH status computed for every ticker (switch in the UI).
  - An OBV confirmation column: whether On-Balance Volume is also at/near its
    own high right now (confirming the move), already exceeded its prior
    peak before price did (leading), or is lagging price's new high
    (a volume non-confirmation - the classic bearish-divergence-on-a-new-high
    warning sign).

Usage:
    python "HH SCREENER.py"                  # full ASX scan, daily + weekly
    python "HH SCREENER.py" --tickers BHP,RIO,FMG
    python "HH SCREENER.py" --workers 20

Requirements:
    pip install yfinance pandas requests openpyxl pdfplumber
"""

import argparse
import io
import json
import os
import re
import statistics
import sys
import time
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import openpyxl
import pdfplumber
import yfinance as yf

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

import price_cache
import rs_utils

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_WORKERS = 15
HISTORY_PERIOD  = "3y"   # daily bars; weekly bars are resampled from this
CHART_TRIM_BARS = 130    # trailing daily bars embedded for the chart-view mini charts (~6M)
MIN_PRICE       = 0.05
MIN_MARKET_CAP  = 20_000_000
MIN_AVG_VOLUME  = 20_000
# Below this fraction of tickers actually returning usable price data on a
# full-universe scan (not a --tickers test run), treat the whole run as
# broken (Yahoo Finance throttling the source IP, e.g. a GitHub Actions
# runner) rather than a real "quiet day", and abort before publishing -
# found 2026-09-01: a GitHub Actions run silently got real data for only
# 27/2037 tickers (1.3%) and reported it as a normal "1 NEW HH" result.
MIN_FETCH_RATIO       = 0.5
MIN_UNIVERSE_FOR_CHECK = 500

PIVOT_LEFT  = 3
PIVOT_RIGHT = 3

# SeaBee's `industry` field (GICS industry-group level) mapped onto the 11
# standard GICS sectors (matching e.g. listcorp.com/asx/sectors) rather than
# a smaller custom bucket set - the old 8-bucket scheme (combining Staples/
# Utilities/Comms into one "Essentials" bucket, and REITs/Real Estate with
# Industrials into "Property & Industrials") only existed because the
# TradingView Pine dashboards could only fit 8 indicator panes on screen.
# This report has no such constraint, and each of the 11 sectors below has
# its own dedicated RS benchmark index (see SECTOR_BENCHMARK), so there's no
# reason to keep them blended here.
#
# SECTOR_MAP/SECTOR_ORDER/BENCHMARK_MARKET/RS_EMA_PERIOD/SECTOR_BENCHMARK/
# BENCHMARK_LABELS/fetch_benchmark_series/relative_strength_status now live
# in rs_utils.py, shared with OBV SCREENER.py's RS-new-high section so both
# screeners route to the same benchmark for the same sector.
SECTOR_MAP = rs_utils.SECTOR_MAP
SECTOR_ORDER = rs_utils.SECTOR_ORDER
BENCHMARK_MARKET = rs_utils.BENCHMARK_MARKET
RS_EMA_PERIOD = rs_utils.RS_EMA_PERIOD
SECTOR_BENCHMARK = rs_utils.SECTOR_BENCHMARK
BENCHMARK_LABELS = rs_utils.BENCHMARK_LABELS


def fetch_benchmark_series():
    return rs_utils.get_benchmark_series(SCRIPT_DIR, HISTORY_PERIOD, symbols={BENCHMARK_MARKET})


def relative_strength_status(dates, closes, index_series):
    return rs_utils.relative_strength_status(dates, closes, index_series)

# ─── TICKER UNIVERSE (SeaBee gives us industry + market cap in one call) ──────

def get_asx_universe():
    """
    Returns {ticker: {'industry': str, 'sector': str, 'market_cap': int, 'name': str}}
    for the full ASX, sourced from SeaBee. Falls back to a bare ticker list
    (no sector data - everything lands in "Other") if SeaBee is unreachable.
    """
    print("📋 Fetching ASX universe (tickers + industry) from SeaBee...")
    try:
        api_url = "https://marketdata.seabee.me/api.php?action=asx_companies_list"
        headers = {"X-API-Key": "deeznuts"}
        r = requests.get(api_url, headers=headers, timeout=20)
        if r.status_code == 200:
            json_resp = r.json()
            if json_resp.get("success") and "data" in json_resp and "companies" in json_resp["data"]:
                companies = json_resp["data"]["companies"]
                universe = {}
                for t, info in companies.items():
                    t = str(t).strip().upper()
                    if not (1 <= len(t) <= 5) or not t.isalnum():
                        continue
                    industry = info.get("industry") or "Other"
                    universe[t] = {
                        "industry": industry,
                        "sector": SECTOR_MAP.get(industry, "Other"),
                        "market_cap": info.get("market_cap") or 0,
                        "name": info.get("name") or t,
                    }
                if len(universe) > 500:
                    print(f"  ✓ Fetched {len(universe)} tickers with industry data from SeaBee")
                    return universe
            print("  ⚠ SeaBee API returned an unexpected JSON structure.")
        else:
            print(f"  ⚠ SeaBee API returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ SeaBee API fetch failed: {e}")

    print("  ⚠ Falling back to a bare ticker list with no sector data (everything -> 'Other')")
    # Minimal built-in fallback so the script still runs if SeaBee is down.
    fallback = "BHP,RIO,FMG,CBA,NAB,WBC,ANZ,CSL,WES,WOW,TLS,STO,ORG,WDS,XRO,WTC".split(",")
    return {t: {"industry": "Other", "sector": "Other", "market_cap": 0, "name": t} for t in fallback}


# ─── MATERIALS COMMODITY CLASSIFICATION ────────────────────────────────────
# GICS gives Materials only one Industry Group (matching the Sector name
# itself - see the SECTOR_MAP comment above), so every Materials ticker's
# "industry" column would otherwise just say "Materials" for all ~90 of
# them. Keyword-match each company's Yahoo business summary against known
# commodities instead, once per ticker ever - a company's primary commodity
# doesn't change between runs, so the result is cached to disk
# (materials_commodity_cache.json) and this only costs a Yahoo .info call
# (heavier / more throttle-prone than the OHLCV fetches in price_cache.py)
# for genuinely new Materials-sector listings, not on every scan.
MATERIALS_COMMODITY_CACHE_PATH = os.path.join(SCRIPT_DIR, "materials_commodity_cache.json")

# (label, [regex fragments (word-boundary-wrapped automatically)], needs_context).
# needs_context is for the handful of commodity words that routinely show up
# for unrelated reasons - "zinc"/"lead" in a coating/brand description
# ("zinc/aluminium alloy coated steel"), "lead" as the verb, "tin" as in tin
# cans/tinplate for a packaging company - so those only count as a match if
# a mining-context word (mine/deposit/ore/smelt/refine/...) appears nearby.
COMMODITY_KEYWORDS = [
    ("Gold", ["gold"], False),
    ("Platinum Group Metals", ["platinum", "palladium", "rhodium", "iridium", "ruthenium", "platinum group"], False),
    ("Iron Ore", ["iron ores?"], False),
    ("Lithium", ["lithium"], False),
    ("Copper", ["copper"], False),
    ("Nickel", ["nickel"], False),
    ("Uranium", ["uranium"], False),
    # Negative lookahead so "hydrogen sulfide/sulphide" (an industrial
    # pollutant being removed, not a hydrogen product - see CG1, an
    # activated-carbon maker whose products remove H2S) isn't Hydrogen.
    ("Hydrogen", [r"hydrogen(?!\s*sulph?ide)"], False),
    ("Gas", ["natural gas", "coal\\s*(?:bed|seam)\\s*(?:gas|methane)", r"\bmethane\b", r"\blng\b"], False),
    # Negative lookahead so "coal bed methane"/"coal seam gas" (a gas
    # extraction technique, not coal mining - see JGH) is Gas, not Coal.
    ("Coal", [r"coal(?!\s*(?:bed|seam)\s*(?:gas|methane))"], False),
    ("Rare Earths", ["rare earths?"], False),
    ("Mineral Sands", ["mineral sands?", "zircon", "titanium dioxide", "rutile", "ilmenite"], False),
    ("Niobium", ["niobium"], False),
    ("Antimony", ["antimony"], False),
    ("Molybdenum", [r"molybden\w*"], False),
    ("Tantalum", ["tantalum"], False),
    ("Scandium", ["scandium"], False),
    ("Chromium", ["chromite", "chromium"], False),
    ("Halloysite", ["halloysite"], False),
    ("Boron", ["boron", "borates?"], False),
    ("Zinc / Lead", ["zinc", "lead"], True),
    ("Silver", ["silver"], False),
    ("Gallium", ["gallium"], False),
    ("Indium", ["indium"], False),
    ("Tin", ["tin"], True),
    ("Manganese", ["manganese"], False),
    ("Graphite", ["graphite", "graphene"], False),
    ("Potash / Fertiliser", ["potash", r"fertilis\w*", r"fertiliz\w*", "phosphates?"], False),
    ("Bauxite / Alumina", ["bauxite", "alumina", "high purity alumina", r"\bhpa\b"], False),
    ("Cobalt", ["cobalt"], False),
    ("Vanadium", ["vanadium"], False),
    # Negative lookahead so "diamond drilling"/"diamond coring"/"diamond
    # core" - standard drilling-technique terminology (diamond-tipped drill
    # bits) used constantly by mining SERVICES companies - isn't read as
    # diamond mining (see PRN/Perenti, MSV/Mitchell Services: both pure
    # drilling contractors with zero diamond exposure).
    ("Diamonds", [r"diamonds?(?!\s*(?:drill|cor|bit))"], False),
    ("Steel", ["steel"], False),
    ("Aluminium", [r"alumin[iu]?um"], False),
    ("Tungsten", ["tungsten", "scheelite", "wolframite"], False),
    ("Magnesium", ["magnesium", "magnesite"], False),
    ("Silica", ["silica"], False),
    ("Kaolin", ["kaolin"], False),
    ("Base Metals", ["base metals?", "polymetallic"], False),
    ("Battery Metals", ["battery metals?"], False),
    ("Chemicals", ["chemicals?", "herbicides?", "insecticides?", "fungicides?",
                   "pesticides?", "crop protection", "agrochemicals?"], False),
    ("Building Materials", ["cement", "concrete"], False),
    ("Packaging", ["packaging", "paperboard", "paper and pulp"], False),
]

# Broadened past the literal word "mine" so "producing bauxite..." (South32)
# and "X deposits" (most explorers) both count, without going so generic
# (e.g. "operates"/"produces" alone) that it stops filtering anything out.
MINING_CONTEXT_RE = re.compile(
    r"\b(?:mine|mines|mining|miner|deposit|ore|concentrate|smelt|refin|reserve)\w*", re.IGNORECASE)

# Drilling/mining contractors (GNG, MAH, MSV, MYE, PRN, VYS - confirmed
# 2026-09-13) don't mine any commodity of their own, so they never match
# COMMODITY_KEYWORDS on a real commodity - they'd otherwise fall through to
# the generic "Materials" bucket, or worse, get caught by an incidental
# mention of a commodity-shaped word in their own service offerings (MYE:
# "chemical application" / "chemical products ... to the ... coal mining
# operations" as part of its contracting services, not a chemicals
# business - wrongly matched "Chemicals" before this check existed).
# Checked as explicit phrases (not just mining-context + a generic
# "services" word) specifically to avoid false-triggering on an actual
# producer's summary that happens to mention "customer service" or similar
# incidental phrasing - verified against BHP/FMG/PLS/RIO/S32 (no match).
MINING_SERVICES_RE = re.compile(
    r"mining services|services? to the mining|mine operation,?\s*contracting|"
    r"contract mining|drilling services|mining (?:and|&) (?:mineral processing|support)|"
    r"mining support services|geotechnical drilling|hydrogeological drilling",
    re.IGNORECASE)


def classify_commodity(summary):
    """Keyword-matches a Yahoo longBusinessSummary against COMMODITY_KEYWORDS.
    Two clause types are stripped before matching, because both routinely
    list several commodities that have nothing to do with what the company
    actually produces today: "explores for X, Y and Z" (an early-stage side
    bet alongside the real business - see FMG, which explores for copper/
    lithium/rare earths but is overwhelmingly an iron ore producer) and
    "serves ... markets" (a mining-*services* company's customer industries,
    not its own output - see ORI, an explosives maker that "serves" coal/
    iron ore/metal miners without mining anything itself).

    A company that explicitly calls itself "diversified", or that has 3+
    distinct surviving commodity matches (BHP, RIO, S32 all read this way),
    is reported as its top 3 commodities by earliest mention (e.g. "Copper
    / Iron Ore / Coal") rather than the single generic word "Diversified" -
    majors typically list their segments in that same order, so earliest-
    mentioned doubles as a reasonable proxy for most-significant. Below
    that threshold, the single earliest-occurring match wins, since
    companies typically lead with their primary business before listing
    secondary products/by-products. Returns None if nothing matched
    (summary missing/too vague to classify)."""
    text = summary or ""
    if MINING_SERVICES_RE.search(text):
        return "Mining Services"
    self_described_diversified = bool(re.search(r"\bdiversified\b", text, re.IGNORECASE))

    def find_matches(t):
        earliest_pos = {}
        for label, patterns, needs_context in COMMODITY_KEYWORDS:
            for pat in patterns:
                for m in re.finditer(r"\b" + pat + r"\b", t, re.IGNORECASE):
                    if needs_context and not MINING_CONTEXT_RE.search(t[max(0, m.start() - 60):m.end() + 60]):
                        continue
                    if label not in earliest_pos or m.start() < earliest_pos[label]:
                        earliest_pos[label] = m.start()
                    break
        return earliest_pos

    # These four clause types are never worth falling back into (below) -
    # unlike an "explores for" side-bet, none of them describe the
    # company's own current-or-future business at all, so a match found
    # ONLY inside one of them is pure noise, never a last-resort signal:
    #   - "serves ... markets" - a mining-*services* company's customer
    #     industries, not its own output (ORI, an explosives maker that
    #     "serves" coal/iron ore/metal miners without mining anything).
    #   - "customers who/that ..." - same idea, phrased differently (AAI/
    #     Alcoa: "aluminium ... to customers that produce products for
    #     ... packaging ..." wrongly read as Alcoa making packaging).
    #   - "formerly known as ..." - a stale former name (IMD: "formerly
    #     known as Pilbara Gold NL" - now a drilling-tech company with zero
    #     gold exposure; PMT: "...Patriot Battery Metals..." obscuring its
    #     real lithium project; KGL: "...Kentor Gold..." for a copper
    #     project) is name history, not current business.
    #   - "is/are used in/for ..." - a downstream product-application list
    #     (FGR/First Graphene: "used in composites, coatings ... concrete
    #     ..." wrongly read as a building-materials producer instead of
    #     the graphene manufacturer it actually is).
    no_fallback = re.sub(r"\bserves\b[^.]*\.", " ", text, flags=re.IGNORECASE)
    no_fallback = re.sub(r"\bcustomers?\b[^.]*\.", " ", no_fallback, flags=re.IGNORECASE)
    no_fallback = re.sub(r"\bformerly known as\b[^.]*\.", " ", no_fallback, flags=re.IGNORECASE)
    no_fallback = re.sub(r"\b(?:is|are)\s+used\s+(?:in|for)\b[^.]*\.", " ", no_fallback, flags=re.IGNORECASE)

    # An "explores for X, Y, Z" side bet is different - it DOES describe the
    # company's own (future) business, just not necessarily its current
    # core one. Stripping it is still the right default (see FMG/IGO
    # above), but if that's the ONLY place a company's single real
    # commodity is named (PLS: "The company primarily explores for
    # lithium."), fall back to it rather than reporting nothing - just
    # never fall back INTO the four noise clauses above (see CG1/Carbonxt,
    # an activated-carbon maker with no commodity of its own: without this
    # split, an empty core after stripping explores-for still fell back to
    # the fully unstripped text and resurrected "serves coal-fired power
    # plants, cement plants, ... hydrogen sulfide ..." as if it mined coal,
    # cement, and hydrogen).
    stripped = re.sub(r"\bexplores?\s+for\b[^.]*\.", " ", no_fallback, flags=re.IGNORECASE)

    earliest_pos = find_matches(stripped) or find_matches(no_fallback)

    if self_described_diversified or len(earliest_pos) >= 3:
        if earliest_pos:
            # A generic umbrella label ("Battery Metals", "Base Metals")
            # adds no information once 2+ of its own specific constituents
            # are already separately listed (AUZ: "Battery Metals" alongside
            # Lithium + Cobalt + Nickel, all literally battery metals) - drop
            # it so a real cap slot isn't wasted on a redundant catch-all.
            GENERIC_UMBRELLA_LABELS = {"Battery Metals", "Base Metals"}
            specific_count = sum(1 for l in earliest_pos if l not in GENERIC_UMBRELLA_LABELS)
            if specific_count >= 2:
                for generic in GENERIC_UMBRELLA_LABELS:
                    earliest_pos.pop(generic, None)
            # " + " rather than " / " - a couple of category labels (e.g.
            # "Zinc / Lead") already contain a slash, so joining with the
            # same character would make 3 categories read as 4.
            # Cap at 7, not 5 - AUZ/Australian Mines (rare earths, lithium,
            # niobium, cobalt, nickel, gold, scandium - all 7 explicitly
            # named as exploration targets in one sentence) had scandium
            # silently dropped by a top-5 cap, found only because the user
            # happened to know the real answer, the same way AW1/CRI forced
            # the cap from 3 to 5 earlier. Checked this doesn't make genuine
            # majors unwieldy either - BHP/RIO/S32/FMG/IGO all naturally sit
            # at 4-5 real matches, well under this cap (2026-09-13/14).
            top3 = sorted(earliest_pos, key=earliest_pos.get)[:7]
            return " + ".join(top3)
        return "Diversified"
    # KNOWN LIMITATION (found auditing "Gallium" additions, 2026-09-14):
    # a company with exactly 2 real commodities falls through to the
    # single-earliest-match branch below instead of this join branch,
    # silently dropping the second one even when it's genuinely core (AXL/
    # Axel REE: "Caladão REE-Gallium Project" names both rare earths AND
    # gallium in its own flagship project name, but only "Rare Earths"
    # survives since len(earliest_pos)==2 < 3). Separately, the
    # "explores? for" clause-stripper can remove a pure explorer's ONLY
    # real commodity list when nothing else in the summary restates it
    # (REE/RareX: "explores for rare earths and gallium, niobium, and
    # scandium" is entirely stripped, leaving only a coincidental "Rare
    # Earths" match from its unrelated project name) - the PLS-style
    # re-fallback only fires when stripping leaves ZERO matches, not when
    # it leaves a partial/misleading one. Both AXL and REE were hand-
    # corrected directly in the cache rather than risking a broader logic
    # change here under time pressure - worth a proper fix in a future
    # pass if more cases like this turn up.
    if not earliest_pos:
        return None
    return min(earliest_pos, key=earliest_pos.get)



# Non-mining companies deliberately dropped from the Materials universe
# (2026-09-13): no longer in the Basic Materials GICS sector at all
# (Consumer Defensive/Utilities/Industrials), so keeping them classified
# alongside real miners was actively misleading. Without this list, the
# very next scan.yml run would treat them as "not yet in cache" and
# silently re-add them via the block below - which is exactly what
# happened to every ticker pruned by hand until this list existed.
EXCLUDED_MATERIALS_TICKERS = {"CLV", "FHE", "PWN", "TTT", "ZNO"}


def classify_materials_commodities(universe):
    """Mutates `universe` in place: for every Materials-sector ticker,
    replaces the generic "industry" value with its primary commodity.
    Only fetches Yahoo's .info for tickers not already in the on-disk
    cache - see the module comment above for why that matters."""
    try:
        with open(MATERIALS_COMMODITY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    # ASX ordinary-equity codes are uniformly 3 letters; anything else is a
    # deferred-settlement/options/rights code for a company already tracked
    # under its real code, or junk data - never worth auto-classifying (see
    # the universe cleanup, 2026-09-13: 73 of these were pure noise).
    materials_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Materials" and len(t) == 3 and t not in EXCLUDED_MATERIALS_TICKERS
    ]
    new_tickers = [t for t in materials_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Materials ticker(s) by commodity...")
        for t in new_tickers:
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                summary = yf.Ticker(yahoo_sym).info.get("longBusinessSummary", "")
                cache[t] = classify_commodity(summary) or "Materials"
            except Exception:
                cache[t] = "Materials"
        with open(MATERIALS_COMMODITY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in materials_tickers:
        universe[t]["industry"] = cache.get(t, "Materials")


# ─── HEALTHCARE INDICATION CLASSIFICATION ──────────────────────────────────
# Same problem as Materials: GICS only gives Healthcare two Industry Groups
# ("Pharmaceuticals, Biotechnology & Life Sciences" and "Health Care
# Equipment & Services" - see SECTOR_MAP), so every Healthcare ticker's
# "industry" column would otherwise say one of two things for all ~170 of
# them. Keyword-match each company's Yahoo business summary against what
# disease/condition it's actually trying to treat instead, cached to disk
# (healthcare_indication_cache.json) the same way as the commodity cache.
HEALTHCARE_INDICATION_CACHE_PATH = os.path.join(SCRIPT_DIR, "healthcare_indication_cache.json")

# (label, [regex fragments (word-boundary-wrapped automatically)]).
# Ordered specific-before-generic where one condition's name is a substring
# concern of another (e.g. "genetic diseases" vs "disease" alone isn't a
# pattern used here, so no ordering conflict yet - kept in mind for future
# additions). A company gets its top 2 earliest-mentioned matches (not 1) -
# see AVE (human AND animal health) and ANR (IBS + a second GI condition) -
# capped at 2 rather than Materials' 5 because ASX biotechs are
# overwhelmingly single-lead-asset companies; a genuine 3rd indication this
# early would likely be a side program, not worth diluting the label over.
HEALTHCARE_INDICATION_KEYWORDS = [
    ("Cancer", [r"cancers?", r"oncolog\w*", r"tumou?rs?", "carcinoma", "leukemia", "lymphoma", "melanoma", "sarcoma"]),
    ("Neurological / CNS", ["neurolog\\w*", "neurodegenerat\\w*", "neuroprotect\\w*", "neurodiagnostic\\w*",
                             "parkinson\\w*", "alzheimer\\w*", "epilep\\w*", r"\bstroke\b",
                             "multiple system atrophy", "dementia"]),
    ("Mental Health", ["mental health", "psychiatric", "depression", r"\banxiety\b",
                        "psilocin", "psychotherapy", "behavioural health", "behavioral health"]),
    ("Cardiovascular", ["cardiovascular", "cardiac", r"\bheart\b", "hemodynamic", "cardioprotect\\w*"]),
    ("Respiratory", ["respiratory", r"\basthma\b", r"\bcopd\b", "pulmonary", r"\blung\b"]),
    ("Infectious Disease", ["infectious diseases?", "antiviral", "antibacterial", "anti-?infectives?",
                             "viral diseases?", "antimicrobial"]),
    ("Autoimmune / Inflammatory", ["autoimmune", "inflammatory diseases?", r"\binflammation\b"]),
    ("Rare / Genetic Disease", ["rare diseases?", "genetic diseases?", "orphan drug", "lymphangioleiomyomatosis"]),
    ("Gastrointestinal", ["gastrointestinal", "irritable bowel", "glomerulosclerosis"]),
    # "diabetic" (adjective) is deliberately only matched in these specific
    # compound forms - the name of a diabetes COMPLICATION being itself
    # diagnosed/treated (PIQ/Proteomics International: PromarkerD predicts
    # "diabetic kidney disease", plus diabetic retinopathy/neuropathy
    # programs) - not the bare word, which would also catch a company
    # treating something else IN a diabetic patient (RCE/Recce: its R327
    # antibiotic treats "diabetic foot infections" - the infection is what
    # it treats, not diabetes; confirmed 2026-09-14 this distinction
    # matters via user review).
    ("Metabolic / Diabetes", [r"\bdiabetes\b", "metabolic diseases?", "type\\s*[12]\\s*diabetes",
                               "diabetic (?:kidney disease|nephropathy|retinopathy|neuropathy)"]),
    ("Dermatology / Skin", ["dermatolog\\w*", "skin diseases?", "skin conditions?", "skin infections?"]),
    ("Ophthalmology / Eye", ["ophthalmolog\\w*", r"\bglaucoma\b", r"\bocular\b", "eye diseases?"]),
    ("Women's Health / Fertility", ["women's health", r"\bfertility\b", "reproductive", "obstetric\\w*",
                                     "gynecolog\\w*", "gynaecolog\\w*", r"\bmaternity\b"]),
    ("Men's / Sexual Health", ["erectile dysfunction"]),
    ("Wound Care / Regenerative Medicine", ["wound (?:care|healing)", "tissue repair", "regenerative medicine",
                                             "soft tissue repair", "nerve repair", "dermal matrix", "nerve graft"]),
    ("Pain Management", ["pain management", "pain relief", "analgesic\\w*"]),
    ("Sleep Disorders", ["sleep-?related disorders?", "sleep apnea", r"\bsleep\b disorders?"]),
    ("Hearing", [r"\bhearing\b", "cochlear"]),
    # Negative lookahead so "bone conduction" (a hearing-aid transmission
    # mechanism - Cochlear's Baha/Osia devices, and any similar hearing
    # implant maker - not an orthopaedic product) doesn't match. Flagged
    # by the user 2026-09-14: COH/Cochlear (100% hearing implants, zero
    # orthopaedic business) was wrongly showing "Hearing + Bone /
    # Orthopaedic" purely from "bone conduction systems" in its summary.
    ("Bone / Orthopaedic", [r"\bbone\b(?!\s*conduction)", "orthop(?:a)?edic\\w*", "osteoarthritis"]),
    ("Renal / Kidney", [r"\brenal\b", r"\bkidney\b"]),
    ("Medicinal Cannabis", ["medicinal cannabis", r"\bcannabis\b", "cannabinoid\\w*"]),
    # Bare "veterinary" alone matched too broadly - a full-service
    # pathology lab's brand name ("Gribbles Veterinary Pathology" - ACL), a
    # distributor's one product line among many (EBO, PGC), or a diagnostics
    # device maker's one of several application areas (OIL, NXN) all
    # incidentally contain the word without being an animal-health company.
    # Every current match against "veterinary" alone turned out wrong
    # (confirmed 2026-09-14, all fixed via MANUAL_INDICATION_OVERRIDES
    # below) - requiring the actual phrase "animal health" is how a company
    # whose real business this is actually self-describes.
    ("Animal Health", ["animal health"]),
    ("Aged Care", ["aged care", "retirement villages?", "rest homes?"]),
]

# Last-resort fallback when nothing in HEALTHCARE_INDICATION_KEYWORDS
# matches: Yahoo's own "industry" field is actually informative for
# Healthcare (unlike Materials, where it's useless), so a company that
# genuinely isn't developing a treatment for a named condition - a
# diagnostics lab, a health-IT vendor, a distributor - gets a real
# business-type label off that field instead of a guess.
HEALTHCARE_BUSINESS_TYPE_MAP = {
    "Health Information Services": "Digital Health / Health IT",
    "Software - Infrastructure": "Digital Health / Health IT",
    "Diagnostics & Research": "Diagnostics & Pathology",
    "Medical Devices": "Medical Devices (General)",
    "Medical Instruments & Supplies": "Medical Devices (General)",
    "Scientific & Technical Instruments": "Medical Devices (General)",
    "Medical Distribution": "Medical Distribution",
    "Medical Care Facilities": "Care Facilities & Services",
    "Drug Manufacturers - Specialty & Generic": "Pharmaceutical Manufacturing",
    "Drug Manufacturers - General": "Pharmaceutical Manufacturing",
    "Household & Personal Products": "Consumer Health & Wellness",
    "Shell Companies": "Shell Company",
}


def _drop_enumerated_sentences(text):
    """Drops any sentence containing 5+ commas before indication matching.
    A full-service pathology lab or a multi-category medical distributor
    routinely lists a dozen+ unrelated service/product lines in one
    sentence ("...cardiac testing, gastroenterology, haematology, ...
    veterinary pathology, molecular cancer services..." - see ACL), and a
    single incidental catalog entry among that many otherwise wins the
    earliest-match race despite describing none of the company's actual
    focus - confirmed false positives this way for ANN (glove maker -
    "veterinary clinics" was 1 of 15 customer types), SHL, ACL and PGC
    (2026-09-14). A real, deliberate "therapeutic areas" statement (e.g.
    CSL's "...Immunology, Immunology Haematology, Cardiovascular and
    Renal, and Vaccines" - 4 commas) stays under this threshold and is
    kept. This is a blunt instrument - it can also strip a real secondary
    indication out of a busy multi-drug pipeline biotech's paragraph (see
    TLX/Telix, SPL/Starpharma), pushing them toward a vaguer fallback
    label - a deliberate trade-off, since an overly specific WRONG label
    (Ansell under "Animal Health") is worse than an under-specific one.
    Known residual gaps this doesn't catch (single short sentence, low
    comma count, but still just one of several unrelated product lines):
    OIL/Optiscan ("InSpecta ... for veterinary medicine" - 1 of 4 imaging
    products), NXN/Nexsen (kidney-disease test is 1 of 4 unrelated POC
    diagnostic SKUs), ACL/Australian Clinical Labs (matches "Veterinary"
    only because it's part of a brand name, "Gribbles Veterinary
    Pathology", in a low-comma sentence listing service brands) - all
    confirmed 2026-09-14, left for a future pass."""
    sentences = re.split(r"(?<=[.])\s+", text)
    return " ".join(s for s in sentences if s.count(",") < 5)


def classify_indication(summary, industry=None):
    """Keyword-matches a Yahoo longBusinessSummary against
    HEALTHCARE_INDICATION_KEYWORDS, returning up to 2 earliest-mentioned
    matches joined by " + ". Falls back to a business-type label off Yahoo's
    "industry" field (HEALTHCARE_BUSINESS_TYPE_MAP) when no condition is
    named, and to "Diversified Healthcare" if even that doesn't resolve.
    Returns None only when summary is empty/missing (caller decides the
    unresolved-ticker label)."""
    text = summary or ""
    if not text:
        return None

    cleaned = _drop_enumerated_sentences(text)

    earliest_pos = {}
    for label, patterns in HEALTHCARE_INDICATION_KEYWORDS:
        for pat in patterns:
            m = re.search(r"\b" + pat + r"\b", cleaned, re.IGNORECASE)
            if m and (label not in earliest_pos or m.start() < earliest_pos[label]):
                earliest_pos[label] = m.start()

    if earliest_pos:
        top2 = sorted(earliest_pos, key=earliest_pos.get)[:2]
        return " + ".join(top2)

    return HEALTHCARE_BUSINESS_TYPE_MAP.get(industry, "Diversified Healthcare")


# IVG (Invert Graphite) and NC6 (Nanollose) carry a "Healthcare" sector tag
# from SeaBee but are not healthcare companies at all by real business:
# IVG explores graphite/rare earths in Tanzania (its own Yahoo industry
# field says "Other Industrial Metals & Mining"; formerly Dominion
# Minerals, renamed Jan 2025), NC6 makes microbial-cellulose textile fibre
# and horticultural products (industry "Textile Manufacturing") - a
# GICS-sector staleness issue, same failure mode as PDI/ATM in Materials.
# Confirmed 2026-09-14, prompted by the user asking to properly research
# every "Diversified Healthcare" ticker.
EXCLUDED_HEALTHCARE_TICKERS = {"IVG", "NC6"}

# Hand-corrections for cases the keyword classifier gets wrong even after
# _drop_enumerated_sentences - checked against the real business, not
# guessed.
# - RAD/Radiopharm Theranostics: every one of its ~10 pipeline products
#   (brain metastasis, breast, non-small-cell lung, pancreatic, prostate,
#   glioblastoma) is an oncology diagnostic/therapeutic pair, but the whole
#   product list is one long comma-heavy sentence that the enumeration
#   guard strips as a catalog - confirmed 2026-09-14.
# - ACL/Australian Clinical Labs: a full-service pathology lab (cardiac
#   testing, gastroenterology, haematology, cervical screening, molecular
#   cancer services, etc.) that only matched "Animal Health" because
#   "Veterinary" is part of one of its brand names, "Gribbles Veterinary
#   Pathology", in a low-comma sentence listing service brands - flagged by
#   the user 2026-09-14, verified against ACL's real business.
# - OIL/Optiscan Imaging: sells 4 different imaging devices (InVue for
#   surgery, InForm for pathology, InVivage for oral imaging, ViewnVivo for
#   life-science research) - "InSpecta ... for veterinary medicine" is 1 of
#   the 4, not its primary focus - confirmed 2026-09-14.
# - NXN/Nexsen: a diversified point-of-care diagnostics platform (human GBS
#   testing, kidney disease, bovine mastitis, biosecurity pathogens) -
#   "kidney disease" and "bovine mastitis" are 2 of 4 unrelated product
#   SKUs, not a primary renal or animal-health focus - confirmed
#   2026-09-14.
# - EBO/EBOS Group: one of the largest healthcare/pharma distributors in
#   Australia/NZ ("operates through the Healthcare and Animal Care
#   segments", Healthcare listed first) - matches its own Yahoo industry
#   field, "Medical Distribution", exactly. Animal Care is a real but
#   secondary segment, not the primary business - confirmed 2026-09-14.
# The remaining 5 were the "Diversified Healthcare" tickers researched via
# web search 2026-09-14 at the user's request:
# - PAR/Paradigm Biopharmaceuticals: flagship (and only clinically advanced)
#   program is Zilosul for knee osteoarthritis pain, in a global pivotal
#   Phase 3 trial with topline data due Q1 2027 - the other 5 conditions
#   named in its summary (mucopolysaccharidosis, chikungunya, heart failure,
#   two respiratory diseases) are earlier-stage extensions of the same
#   anti-inflammatory drug, not co-equal programs. Verified via web search.
# - 1AI/Algorae Pharmaceuticals: 2 of its 3 named candidates are CNS disease
#   programs (AI-116 for Alzheimer's/dementia, NTCELL in Phase IIb for
#   Parkinson's - its most clinically advanced program); only AI-168
#   (hypertension) is cardiovascular. Verified via web search.
# - ACR/Acrux: a generic transdermal/topical pharmaceutical manufacturer
#   (confirmed via web search) - its products span dermatology, pain and
#   women's health via one shared delivery-technology platform, not a
#   disease focus, so the business-type label fits better than an
#   indication guess.
# - ADO/AnteoTech: genuinely two unrelated businesses (confirmed via web
#   search) - a life-sciences/diagnostics-reagent division (AnteoBind) and
#   a silicon-anode battery-materials division for EVs/drones, the latter
#   not healthcare at all. Diagnostics & Pathology covers its
#   healthcare-relevant half; the battery half has no home in this index.
# - HXL/Hexima: "does not have significant operations... focuses on
#   exploration of transactions with third parties" per its own summary -
#   a dormant shell (previously did real plant-protein therapeutics R&D).
#   Given its own "Shell Companies" Yahoo industry field.
# - ENP/Entropy Neurodynamics: lead program (psilocin/psilocybin +
#   psychotherapy) targets binge eating disorder (mental health) AND
#   fibromyalgia/IBS/abdominal pain (gastrointestinal/pain) in parallel
#   trials - genuinely both, but the sentence naming psilocin/psychotherapy
#   got dropped by _drop_enumerated_sentences purely because of a trailing
#   "in Australia, Canada, Switzerland, and the United States" geography
#   clause pushing its comma count over threshold, not because it's a real
#   catalog list. Flagged by the user 2026-09-14 (spotted the binge-eating/
#   mental-health angle was missing). NOTE: this trailing-country-list
#   pattern likely causes the same silent loss of an otherwise-clean
#   sentence's signal elsewhere in the dataset - a targeted fix (stripping
#   a trailing geography clause before counting commas, rather than
#   counting the whole sentence) is a good candidate for a future pass.
MANUAL_INDICATION_OVERRIDES = {
    "RAD": "Cancer",
    "ACL": "Diagnostics & Pathology",
    "EBO": "Medical Distribution",
    "PAR": "Bone / Orthopaedic",
    "1AI": "Neurological / CNS",
    "ACR": "Pharmaceutical Manufacturing",
    "ADO": "Diagnostics & Pathology",
    "HXL": "Shell Company",
    "OIL": "Medical Devices (General)",
    "NXN": "Diagnostics & Pathology",
    "ENP": "Mental Health + Gastrointestinal",
    # UBI/Universal Biosensors: its "diabetes" match is from "a license
    # agreement... for the detection and monitoring of diabetes in
    # non-humans" - an animal-health side license, not its core business.
    # Its actual business (INR/coagulation test strips and Xprecia devices
    # for monitoring human anticoagulant therapy, plus unrelated Sentia
    # wine-testing products) has no clean single human indication - a
    # genuine mixed bag, matching its own "Medical Devices" industry field.
    # Flagged by the user 2026-09-14.
    "UBI": "Medical Devices (General)",
}


def classify_healthcare_indications(universe):
    """Mutates `universe` in place: for every Healthcare-sector ticker,
    replaces the generic "industry" value with its primary indication (or
    business-type fallback). Only fetches Yahoo's .info for tickers not
    already in the on-disk cache."""
    try:
        with open(HEALTHCARE_INDICATION_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    healthcare_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Healthcare" and len(t) == 3 and t not in EXCLUDED_HEALTHCARE_TICKERS
    ]
    new_tickers = [t for t in healthcare_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Healthcare ticker(s) by indication...")
        for t in new_tickers:
            if t in MANUAL_INDICATION_OVERRIDES:
                cache[t] = MANUAL_INDICATION_OVERRIDES[t]
                continue
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                summary = info.get("longBusinessSummary", "")
                industry = info.get("industry")
                cache[t] = classify_indication(summary, industry) or "Healthcare"
            except Exception:
                cache[t] = "Healthcare"
        with open(HEALTHCARE_INDICATION_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in healthcare_tickers:
        universe[t]["industry"] = cache.get(t, "Healthcare")


# ─── ENERGY FUEL-TYPE CLASSIFICATION ───────────────────────────────────────
# Unlike Materials/Healthcare, Yahoo's own "industry" field for Energy is
# already fairly granular (Oil & Gas E&P, Uranium, Thermal Coal, Oil & Gas
# Refining & Marketing, Oil & Gas Equipment & Services, Utilities -
# Renewable/Regulated Gas) - so the keyword scan here exists mainly to
# split out fuel types Yahoo lumps into the generic "Oil & Gas E&P" bucket
# (coal seam gas, hydrogen, helium, geothermal), not to replace the
# industry field entirely the way Materials/Healthcare needed to.
ENERGY_FUEL_CACHE_PATH = os.path.join(SCRIPT_DIR, "energy_fuel_cache.json")

# ~17 tickers carry a stale "Energy" sector tag from SeaBee but are
# genuinely metals explorers with zero uranium/oil/gas/coal/hydrogen
# content in their own business description - the same GICS-staleness
# failure mode as PDI/ATM (Materials) and IVG/NC6 (Healthcare). Checked
# individually against real text, not assumed from the generic "Other
# Industrial Metals & Mining" GICS sub-industry alone - several tickers
# that sub-industry label would suggest excluding (CXU, DEV, EPM, MEU,
# MHC, T92, ZEU) turned out to explicitly name uranium as a real
# exploration target and are correctly KEPT, flagged by the user
# 2026-09-14 after an initial overly-broad first pass.
EXCLUDED_ENERGY_TICKERS = {"BLZ", "BTM", "FME", "KLR", "NAE", "TM1", "ALM", "SSH"}

# Checked highest-specificity-first: coal seam gas/CBM before bare "coal"
# (a CSG play isn't a thermal-coal producer), hydrogen with a negative
# lookahead so "hydrogen sulfide" (an industrial pollutant, not a hydrogen
# product) doesn't match, same guard Materials already uses.
ENERGY_FUEL_KEYWORDS = [
    ("Uranium", ["uranium"], False),
    ("Coal Seam Gas / CBM", [r"coal\s*(?:bed|seam)\s*(?:gas|methane)", r"\bcbm\b"], False),
    ("Thermal Coal", [r"\bcoal\b(?!\s*(?:bed|seam)\s*(?:gas|methane))"], False),
    ("Hydrogen", [r"hydrogen(?!\s*sulph?ide)"], False),
    ("Helium", ["helium"], False),
    ("Geothermal", ["geothermal"], False),
    ("Solar / Wind", ["solar power", "wind power", r"\bsolar\b", r"wind farms?"], False),
    ("Oil & Gas", ["oil and gas", "petroleum", "hydrocarbons?", "crude oil", r"\bnatural gas\b", r"\boil\b", r"\bgas\b"], False),
]

# Last-resort fallback off Yahoo's own "industry" field when no fuel
# keyword matches at all (a downstream/services/utility company describing
# its business in terms that don't name a specific fuel).
ENERGY_BUSINESS_TYPE_MAP = {
    "Oil & Gas Refining & Marketing": "Refining & Marketing",
    "Oil & Gas Equipment & Services": "Energy Services",
    "Utilities - Renewable": "Solar / Wind",
    "Utilities - Regulated Gas": "Oil & Gas",
}

# Hand-corrections for real energy-services/technology businesses that
# don't self-describe with any ENERGY_FUEL_KEYWORDS term and whose Yahoo
# industry field is a generic metals/engineering bucket that would
# otherwise misclassify or exclude them - verified against their real
# business, not guessed.
# - GBL/Great Bear Exploration: an oil & gas well-remediation/chemical-
#   technology company ("PhaseShift technology... reliquifies hydrocarbon
#   solids... remediation technology for oil and gas wells"), not a metals
#   explorer despite Yahoo's "Other Precious Metals & Mining" tag.
# - MCE/Matrix Composites & Engineering: makes subsea buoyancy and drill-
#   riser buoyancy systems for offshore oil & gas rigs, despite Yahoo's
#   "Engineering & Construction" tag.
MANUAL_ENERGY_FUEL_OVERRIDES = {
    "GBL": "Energy Services",
    "MCE": "Energy Services",
}


def classify_energy_fuel(summary, industry=None):
    """Keyword-matches a Yahoo longBusinessSummary against
    ENERGY_FUEL_KEYWORDS, returning up to 2 earliest-mentioned matches
    joined by " + ". Falls back to a business-type label off Yahoo's
    "industry" field (ENERGY_BUSINESS_TYPE_MAP) when no fuel is named, and
    to the industry field itself (or "Oil & Gas" as a last resort) if even
    that doesn't resolve. Returns None only when summary is empty/missing."""
    text = summary or ""
    if not text:
        return None

    # Same "serves ... markets" exclusion Materials already needed - a
    # services/equipment company's customer industries, not its own
    # output (SRJ/SRJ Technologies: "serves oil and gas, desalination,
    # mining, utilities, shipping, and power generation industries" - an
    # engineering-services company for containment/leak-repair hardware,
    # not an oil & gas producer - confirmed 2026-09-14).
    no_customers = re.sub(r"\bserves\b[^.]*\.", " ", text, flags=re.IGNORECASE)
    no_customers = re.sub(r"\bcustomers?\b[^.]*\.", " ", no_customers, flags=re.IGNORECASE)

    earliest_pos = {}
    for label, patterns, needs_context in ENERGY_FUEL_KEYWORDS:
        for pat in patterns:
            m = re.search(r"\b" + pat + r"\b", no_customers, re.IGNORECASE)
            if m and (label not in earliest_pos or m.start() < earliest_pos[label]):
                earliest_pos[label] = m.start()

    if earliest_pos:
        top2 = sorted(earliest_pos, key=earliest_pos.get)[:2]
        return " + ".join(top2)

    if industry in ENERGY_BUSINESS_TYPE_MAP:
        return ENERGY_BUSINESS_TYPE_MAP[industry]
    # Any unmapped "Oil & Gas ..." GICS sub-industry (e.g. "Oil & Gas E&P")
    # normalizes to the same "Oil & Gas" label the keyword scan itself
    # produces, rather than leaking Yahoo's raw sub-industry string as a
    # separate, differently-worded bucket.
    if industry and industry.startswith("Oil & Gas"):
        return "Oil & Gas"
    return industry or "Oil & Gas"


def classify_energy_fuels(universe):
    """Mutates `universe` in place: for every Energy-sector ticker,
    replaces the generic "industry" value with its primary fuel type (or
    business-type fallback). Only fetches Yahoo's .info for tickers not
    already in the on-disk cache."""
    try:
        with open(ENERGY_FUEL_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    energy_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Energy" and len(t) == 3 and t not in EXCLUDED_ENERGY_TICKERS
    ]
    new_tickers = [t for t in energy_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Energy ticker(s) by fuel type...")
        for t in new_tickers:
            if t in MANUAL_ENERGY_FUEL_OVERRIDES:
                cache[t] = MANUAL_ENERGY_FUEL_OVERRIDES[t]
                continue
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                summary = info.get("longBusinessSummary", "")
                industry = info.get("industry")
                cache[t] = classify_energy_fuel(summary, industry) or "Energy"
            except Exception:
                cache[t] = "Energy"
        with open(ENERGY_FUEL_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in energy_tickers:
        universe[t]["industry"] = cache.get(t, "Energy")


# ─── TECH CATEGORY CLASSIFICATION ──────────────────────────────────────────
# Same problem as Materials/Healthcare: GICS gives Info Tech only 3 coarse
# Industry values (Software & Services, Semiconductors & Semiconductor
# Equipment, Technology Hardware & Equipment), so every ticker's "industry"
# column would otherwise say one of 3 things for all ~130 of them.
# Keyword-match each company's Yahoo business summary against what the
# software/tech actually DOES instead.
TECH_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "tech_category_cache.json")

# CML/Connected Minerals is a genuine uranium/lead/copper/gold explorer
# with a stale "Info Tech" sector tag - same GICS-staleness failure mode
# as CXU/DEV were nearly wrongly excluded from (Energy) and IVG/NC6
# (Healthcare). Confirmed 2026-09-14.
# DTZ/Dotz Nano (nanotech carbon-capture/authentication, industry
# "Specialty Chemicals") and NVX/Novonix (battery materials/technology,
# industry "Electrical Equipment & Parts") are the same failure mode -
# genuine materials/industrials businesses with a stale "Info Tech" tag,
# not software/tech companies. Confirmed 2026-09-15.
EXCLUDED_TECH_TICKERS = {"CML", "DTZ", "NVX"}

# Ordered specific-before-generic. "Artificial intelligence"/"machine
# learning" require the full phrase rather than bare "AI" - a 2-letter
# token is too prone to incidental matches (company names, other
# acronyms) to trust bare.
TECH_CATEGORY_KEYWORDS = [
    ("Cybersecurity", ["cyber ?security", "cyber risk", "network security", "information security",
                        "secure information sharing", "classified file", "data security", "encryption"], False),
    ("Artificial Intelligence / Machine Learning",
     ["artificial intelligence", "machine learning", "computer vision"], False),
    ("Semiconductors", ["semiconductor"], False),
    ("Data Centers / Cloud Infrastructure",
     ["data center", "data centre", "cloud computing", "cloud infrastructure"], False),
    ("Telecommunications / Connectivity",
     ["telecommunications?", "wireless technology", "satellite", "network interconnection"], False),
    ("Fintech / Payments", ["fintech", "payments?", "digital asset", "cryptocurrency",
                            "wealth management", "financial services industry"], False),
    ("Enterprise Software / ERP",
     ["enterprise resource planning", r"\berp\b", "enterprise (?:business )?software", "billing and customer"], False),
    ("HR / Workforce Software", ["workforce management", "human resources", "recruitment"], False),
    ("Education Technology", ["education", "learning platform", "assessment software"], False),
    ("Healthcare Technology", ["healthcare technology", "medical technology", "cardiac diagnostics"], False),
    ("Advertising / Marketing Technology", ["advertising", "marketing solutions", "affiliate marketing"], False),
    ("Logistics / Supply Chain Technology", ["logistics", "supply chain", "freight",
                                              "fleet management", "telematics", "transport industry",
                                              "procurement", "vendor management"], False),
    ("Gaming / Media / Entertainment", ["gaming", "entertainment", r"\bmedia\b"], False),
    ("IoT / Hardware", ["internet of things", r"\biot\b", "wearables?", "scada", "telemetry"], False),
]

# Last-resort fallback off Yahoo's own "industry" field when no category
# keyword matches at all.
TECH_BUSINESS_TYPE_MAP = {
    "Information Technology Services": "IT Services / Consulting",
    "Electronics & Computer Distribution": "Hardware Distribution",
    "Computer Hardware": "IoT / Hardware",
    "Electronic Components": "IoT / Hardware",
    "Communication Equipment": "Telecommunications / Connectivity",
    "Scientific & Technical Instruments": "IoT / Hardware",
    "Security & Protection Services": "Cybersecurity",
    "Health Information Services": "Healthcare Technology",
    "Electrical Equipment & Parts": "IoT / Hardware",
}


def classify_tech_category(summary, industry=None):
    """Keyword-matches a Yahoo longBusinessSummary against
    TECH_CATEGORY_KEYWORDS, returning up to 2 earliest-mentioned matches
    joined by " + ". Falls back to a business-type label off Yahoo's
    "industry" field (TECH_BUSINESS_TYPE_MAP) when no category is named,
    and to "Software / Technology" as a last resort. Returns None only
    when summary is empty/missing."""
    text = summary or ""
    if not text:
        return None

    earliest_pos = {}
    for label, patterns, needs_context in TECH_CATEGORY_KEYWORDS:
        for pat in patterns:
            m = re.search(r"\b" + pat + r"\b", text, re.IGNORECASE)
            if m and (label not in earliest_pos or m.start() < earliest_pos[label]):
                earliest_pos[label] = m.start()

    if earliest_pos:
        top2 = sorted(earliest_pos, key=earliest_pos.get)[:2]
        return " + ".join(top2)

    return TECH_BUSINESS_TYPE_MAP.get(industry, "Software / Technology")


def classify_tech_categories(universe):
    """Mutates `universe` in place: for every Info Tech-sector ticker,
    replaces the generic "industry" value with its primary category (or
    business-type fallback). Only fetches Yahoo's .info for tickers not
    already in the on-disk cache."""
    try:
        with open(TECH_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    tech_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Info Tech" and len(t) == 3 and t not in EXCLUDED_TECH_TICKERS
    ]
    new_tickers = [t for t in tech_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Tech ticker(s) by category...")
        for t in new_tickers:
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                summary = info.get("longBusinessSummary", "")
                industry = info.get("industry")
                cache[t] = classify_tech_category(summary, industry) or "Info Tech"
            except Exception:
                cache[t] = "Info Tech"
        with open(TECH_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in tech_tickers:
        universe[t]["industry"] = cache.get(t, "Info Tech")


# ─── INDICATORS ───────────────────────────────────────────────────────────────

def calc_obv_series(closes, volumes):
    obv = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def hh_signal(opens, highs, lows, closes):
    """
    Port of the FIXED Pine "HH Indicator (BT)" logic. Returns
    (signal_on_last_bar: bool, last_pivot_price: float|None, last_pivot_idx: int|None).

    tops/btms use the candle BODY (max/min of open,close), matching the Pine
    script exactly - wicks don't count toward pivot formation there.
    """
    n = len(closes)
    L, R = PIVOT_LEFT, PIVOT_RIGHT
    window = L + R + 1
    if n < window + 2:
        return False, None, None

    tops = [max(opens[i], closes[i]) for i in range(n)]

    last_ph_price = None
    last_ph_idx = None
    last_triggered_idx = None
    last_signal = False

    # bar index of the pivot CANDIDATE at position i is (i - R), mirroring
    # Pine's top[3] (3 bars back from the bar currently being evaluated)
    for i in range(window - 1, n):
        cand_idx = i - R
        cand_top = tops[cand_idx]
        is_ph = all(cand_top >= tops[cand_idx + off] for off in range(-L, R + 1))

        if is_ph:
            last_ph_price = cand_top
            last_ph_idx = cand_idx

        raw_cross = (
            last_ph_price is not None
            and i > 0
            and closes[i - 1] <= last_ph_price
            and closes[i] > last_ph_price
        )
        is_new_pivot = last_ph_idx is not None and last_ph_idx != last_triggered_idx
        signal = bool(raw_cross and is_new_pivot)

        if signal:
            last_triggered_idx = last_ph_idx

        last_signal = signal  # only the FINAL bar's value is what we report

    return last_signal, last_ph_price, last_ph_idx


def obv_confirmation(obv, closes, pivot_idx):
    """
    Classify whether OBV is backing up the current price high, relative to
    OBV's own value at the reference pivot (the same swing high price just
    broke above).
      - No signal / no pivot reference -> None (blank in the UI)
      - OBV now clearly above its value at the pivot -> "Confirming"
      - OBV now clearly below its value at the pivot -> "Not confirming"
      - Roughly flat -> "Neutral"
    """
    if pivot_idx is None or pivot_idx >= len(obv):
        return None
    obv_now = obv[-1]
    obv_then = obv[pivot_idx]
    span = max(abs(obv_now), abs(obv_then), 1)
    diff_pct = (obv_now - obv_then) / span
    if diff_pct > 0.03:
        return "Confirming"
    if diff_pct < -0.03:
        return "Not confirming"
    return "Neutral"


def high_tier(closes):
    """Longest of the 1/3/6/12-month windows (in trading days) for which
    today's close is still the highest close in that window. Monotonic - a
    12-month high necessarily makes you a 6-, 3- and 1-month high too, since
    each longer window contains all the shorter ones - so only the longest
    qualifying tier needs to be reported. A tier is skipped (not just
    failed) if there isn't enough history to confirm it, rather than
    trivially "passing" on a too-short window. Used to flag a NEW HH that
    only clears a very recent, low-significance local pivot (see YRL,
    2026-09-02: broke a pivot from 9 days earlier while still ~15% below
    its own 3-month high)."""
    today = closes[-1]
    for label, n in (("12M", 252), ("6M", 126), ("3M", 63), ("1M", 21)):
        if len(closes) < n:
            continue
        if today >= max(closes[-n:]):
            return label
    return None


def resample_weekly(dates, opens, highs, lows, closes, volumes):
    df = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes}, index=pd.to_datetime(dates))
    wk = df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return wk


# ─── ANALYSE A SINGLE STOCK ───────────────────────────────────────────────────

def analyse_ticker(ticker_raw, info, ticker_frame, latest_date=None, index_series=None):
    """Computes signals from an already-fetched price_cache frame (see that
    module - all three screeners share one cache now, refreshed once up
    front, so this function does no network I/O at all). Returns a result
    dict, or None if the ticker doesn't pass the liquidity filters.

    Raw (auto_adjust=False) prices deliberately: adjusted prices
    retroactively lower every historical bar before an ex-dividend date to
    make them comparable to today, which can make a pivot level look
    "broken" by today's raw price when it never actually was (confirmed on
    ANN, 2026-09-01 - see the fix note in this script's version history).
    TradingView's own chart/Pine data uses raw prices by default, so this
    also matches what the live "HH Indicator" dashboard shows."""
    sym = ticker_raw.upper().strip()

    try:
        market_cap = info.get("market_cap") or 0
        if market_cap and market_cap < MIN_MARKET_CAP:
            return None

        if ticker_frame is None or len(ticker_frame) < 40:
            return None

        # If this ticker's cache didn't actually refresh to the most recent
        # session other tickers reached this run (a per-ticker fetch
        # failure quietly falling back to stale cached data - separate
        # from the circuit breaker's universe-wide check), we can't trust
        # "today's" price/signal for it. Found 2026-09-02: LDR's fetch
        # failed, so its cache still ended on Sept 1 - the code used that
        # stale close as if it were live, firing a false "NEW HH" a full
        # day after the real (and correct) Sept 1 breakout, once price had
        # already reversed on Sept 2. Skip rather than show anything
        # unverifiable, same philosophy as the circuit breaker.
        if latest_date is not None and ticker_frame["date"].iloc[-1] < latest_date:
            return None

        closes = ticker_frame["close"].tolist()
        opens = ticker_frame["open"].tolist()
        highs = ticker_frame["high"].tolist()
        lows = ticker_frame["low"].tolist()
        volumes = ticker_frame["volume"].tolist()
        dates = ticker_frame["date"].tolist()

        price = closes[-1]
        if price < MIN_PRICE:
            return None
        # Median, not mean: a single spike day (a stock otherwise dead most
        # of the month) can drag a 30-day AVERAGE above threshold even when
        # it barely trades - found 2026-09-02 on RAU (mean 49,659, cleared
        # the old 20,000 mean filter; median 0, since 16 of its last 30 days
        # had zero volume). Median is naturally immune to that.
        median_vol = statistics.median(volumes[-30:])
        if median_vol < MIN_AVG_VOLUME:
            return None

        prev_close = closes[-2]
        change_1d = (price - prev_close) / prev_close * 100

        # --- Daily ---
        sig_d, piv_price_d, piv_idx_d = hh_signal(opens, highs, lows, closes)
        obv_d = calc_obv_series(closes, volumes)
        obv_conf_d = obv_confirmation(obv_d, closes, piv_idx_d) if sig_d else None

        # --- Weekly (resampled from the same daily data) ---
        wk = resample_weekly(dates, opens, highs, lows, closes, volumes)
        sig_w, piv_price_w, piv_idx_w = (False, None, None)
        obv_conf_w = None
        if len(wk) >= 12:
            wo, wh, wl, wc, wv = wk["Open"].tolist(), wk["High"].tolist(), wk["Low"].tolist(), wk["Close"].tolist(), wk["Volume"].tolist()
            sig_w, piv_price_w, piv_idx_w = hh_signal(wo, wh, wl, wc)
            if sig_w:
                obv_w = calc_obv_series(wc, wv)
                obv_conf_w = obv_confirmation(obv_w, wc, piv_idx_w)

        # Sector-level RS (vs a GICS sector index) is dropped - see
        # rs_utils.py's SECTOR_BENCHMARK comment for why. Only rs_market
        # (the XJO comparison, fetched before the bulk refresh above - see
        # run_scan's comment on that ordering) ships.
        rs_market = None
        if (sig_d or sig_w) and index_series:
            rs_market = relative_strength_status(dates, closes, index_series.get(BENCHMARK_MARKET))

        result = {
            "ticker": sym,
            "name": info.get("name", sym),
            "sector": info.get("sector", "Other"),
            "industry": info.get("industry", "Other"),
            "market_cap": int(market_cap) if market_cap else 0,
            "price": round(price, 4),
            "change_1d": round(change_1d, 2),
            "hh_daily": bool(sig_d),
            "hh_weekly": bool(sig_w),
            "obv_daily": obv_conf_d,
            "obv_weekly": obv_conf_w,
            "high_tier": high_tier(closes) if (sig_d or sig_w) else None,
            "rs_market": rs_market,
        }

        # Daily OHLCV for the chart-view mini candlesticks - only embedded for
        # tickers with a live signal (daily or weekly), to keep the page size
        # sane across a ~1000-ticker universe. Table view doesn't need this at
        # all (it's numbers/text only), so non-signal tickers carry none of it.
        if sig_d or sig_w:
            trim = slice(-CHART_TRIM_BARS, None)
            result.update({
                "dates": [d.strftime("%Y-%m-%d") for d in dates[trim]],
                "opens": [round(v, 4) for v in opens[trim]],
                "highs": [round(v, 4) for v in highs[trim]],
                "lows": [round(v, 4) for v in lows[trim]],
                "closes": [round(v, 4) for v in closes[trim]],
            })

        return result

    except Exception:
        return None


# ─── SCAN ─────────────────────────────────────────────────────────────────────

def run_scan(universe, workers=DEFAULT_WORKERS):
    tickers = list(universe.keys())
    total = len(tickers)
    t0 = time.time()

    # Fetched BEFORE the ~2000-ticker bulk refresh below, not after - found
    # 2026-09-08 that GitHub Actions runs got "Too Many Requests. Rate
    # limited." on the STW.AX benchmark fetch 100% of the time, even after
    # narrowing it to a single liquid .AX equity ticker. Not a symbol-type
    # block after all: the bulk refresh's ~2000 requests exhaust the
    # runner IP's rate-limit budget for the run, and the benchmark fetch
    # used to run only after that. One request while the budget is still
    # fresh has a real chance of succeeding where the same request after
    # 2000 others didn't.
    print("   Fetching relative-strength benchmark index (XJO)...")
    index_series = fetch_benchmark_series()
    print(f"   Got market benchmark: {'yes' if BENCHMARK_MARKET in index_series else 'no'}")

    print(f"\n🔄 Refreshing shared price cache for {total} ASX tickers | {workers} threads\n")
    cache, fetched_ok, _ = price_cache.refresh_cache(tickers, workers=workers, max_history=HISTORY_PERIOD)
    usable = price_cache.count_usable(cache, tickers, min_days=40)
    latest_date = cache["date"].max() if not cache.empty else None
    # "Usable" only checks cache DEPTH (>=40 days), not freshness - a ticker
    # stuck a week stale from repeated Yahoo throttling still counts as
    # usable there. This is the actual "did we get today's session for it"
    # count, surfaced in the report so a heavily-throttled run doesn't look
    # identical to a clean one just because "X scanned" is always the full
    # universe size regardless of how much of it is trustworthy today.
    fresh_today = 0
    if latest_date is not None and not cache.empty:
        last_by_ticker = cache.groupby("ticker")["date"].max()
        fresh_today = sum(1 for t in tickers if last_by_ticker.get(t) == latest_date)
    print(f"   Cache refresh done in {time.time()-t0:.1f}s  |  Fresh this run: {fetched_ok}/{total}  |  "
          f"Usable overall: {usable}/{total}  |  Fresh as of today's session: {fresh_today}/{total}")

    results = []
    for t in tickers:
        try:
            res = analyse_ticker(t, universe[t], price_cache.get_ticker_frame(cache, t), latest_date, index_series)
            if res:
                results.append(res)
        except Exception:
            pass

    print(f"   Scanned: {total}  |  Included (passed liquidity filters): {len(results)}")
    return results, usable, fresh_today


# ─── HTML REPORT (sector-grouped table, matches the Pine dashboard layout) ────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ASX Higher-High Screener</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<script>
// Set before first paint so there's no flash of the wrong theme. Defaults
// to dark (this site's original look) unless the viewer explicitly chose
// light on a previous visit.
try {
  if (localStorage.getItem('theme') === 'light') document.documentElement.setAttribute('data-theme', 'light');
} catch (e) {}
</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
:root {
  --bg:#121316; --surface:#1c1f24; --border:#454b56; --card:#262b32;
  --accent:#e8b355; --accent-ink:#1c1204; --accent2:#8fc4db; --accent2-ink:#0a1f26;
  --good:#a8d491; --bad:#f0998a; --warn:#eec27a;
  --text:#f5f2e8; --muted:#c9c2b0; --rs-dim:#8f8874;
  --row-border:var(--border); --sector-head-bg:var(--surface);
}
[data-theme="light"] {
  --bg:#faf8f2; --surface:#f0ead8; --border:#a89b7a; --card:#e6ddc4;
  --accent:#7a4a0f; --accent-ink:#fdf8ef; --accent2:#163540; --accent2-ink:#eaf5f7;
  --good:#33481f; --bad:#6b241c; --warn:#6b4a0f;
  --text:#141209; --muted:#3d3829; --rs-dim:#6b6552;
  --row-border:var(--border); --sector-head-bg:#dbe6e8;
}
.themebtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  width:2.3rem;height:2.3rem;border-radius:50%;cursor:pointer;font-size:1.05rem;
  display:flex;align-items:center;justify-content:center;flex:none}
.themebtn:hover{border-color:var(--accent2)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,sans-serif;
  font-variant-numeric:tabular-nums;margin:0;}
.wrap{max-width:1560px;margin:0 auto;padding:0 1.6rem 1.6rem}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.3rem;flex-wrap:wrap;gap:1rem}
h1{font-family:"Fraunces",Georgia,serif;font-size:2.2rem;font-weight:600;letter-spacing:-.01em;
  text-wrap:balance;color:var(--text)}
.subtitle{font-size:.92rem;color:var(--muted);margin-top:.3rem;max-width:44rem}
.session{font-size:.68rem;color:var(--muted);margin:.6rem 0 1.1rem}
.topbar-right{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:.5rem .6rem}
.copybtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.72rem;padding:.5rem .9rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.4rem;white-space:nowrap;height:fit-content}
.copybtn:hover{border-color:var(--accent)}
.copybtn.copied{border-color:var(--accent);color:var(--accent)}
.notice{background:rgba(232,179,85,.08);border:1px solid rgba(232,179,85,.25);border-radius:4px;
  padding:.7rem .9rem;font-size:.68rem;color:var(--warn);margin-bottom:1rem;line-height:1.6}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:.7rem}
.sectorrow{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.1rem}
.sectorpill{background:var(--surface);border:1px solid var(--border);color:var(--muted);
  font-size:.68rem;padding:.35rem .7rem;border-radius:20px;cursor:pointer}
.sectorpill:hover{color:var(--text)}
.sectorpill.active{background:rgba(232,179,85,.12);border-color:var(--accent);color:var(--accent)}
.pillgroup{display:flex;gap:.3rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.2rem}
.ctrllabel{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.pill{background:transparent;border:none;color:var(--muted);font-size:.72rem;
  padding:.4rem .8rem;border-radius:6px;cursor:pointer;white-space:nowrap}
.pill:hover{color:var(--text)}
.pill.active{background:var(--accent2);color:var(--accent2-ink);font-weight:600}
input[type=text]{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.75rem;padding:.5rem .8rem;border-radius:8px;outline:none;width:170px}
.sector{margin-bottom:.9rem;border:1px solid var(--border);border-radius:10px;overflow:hidden}
.sector-head{display:flex;justify-content:space-between;align-items:center;padding:.5rem .9rem;
  background:var(--sector-head-bg);cursor:pointer;user-select:none}
.sector-head h2{font-family:"IBM Plex Mono",monospace;font-size:.88rem;font-weight:700}
.sector-head .count{font-size:.68rem;color:var(--muted)}
table.datatable{width:100%;table-layout:fixed;border-collapse:collapse;font-size:.82rem}
table.datatable thead tr{border-bottom:1px solid var(--border)}
table.datatable th{text-align:left;padding:.4rem .6rem;font-size:.66rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;white-space:nowrap;font-weight:600}
table.datatable tbody tr{border-bottom:1px solid var(--row-border)}
table.datatable tbody tr:hover{background:rgba(232,179,85,.05)}
table.datatable td{padding:.38rem .6rem;vertical-align:middle;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* Fixed column widths, identical across every sector's table regardless of
   that sector's own content lengths (table-layout:auto let each sector's
   table size its columns independently, so widths drifted sector to sector -
   e.g. a long Industry name in one sector didn't affect another's table). */
table.datatable th:nth-child(1), table.datatable td:nth-child(1){width:18%}
table.datatable th:nth-child(2), table.datatable td:nth-child(2){width:14%}
table.datatable th:nth-child(3), table.datatable td:nth-child(3){width:11%}
table.datatable th:nth-child(4), table.datatable td:nth-child(4){width:12%}
table.datatable th:nth-child(5), table.datatable td:nth-child(5){width:10%}
table.datatable th:nth-child(6), table.datatable td:nth-child(6){width:12%}
table.datatable th:nth-child(7), table.datatable td:nth-child(7){width:11%}
table.datatable th:nth-child(8), table.datatable td:nth-child(8){width:12%}
/* Mobile: Industry and Mkt Cap are the least essential columns (ticker/
   price/chg/OBV are what you actually need to act on), and table-
   layout:fixed enforces the desktop % widths verbatim regardless of
   viewport - on a narrow phone that squeezed ticker/price/chg down to a
   few px each. Drop both and let the rest breathe instead. */
@media (max-width: 640px){
  h1{font-size:1.8rem}
  .company-name{display:none}
  table.datatable th:nth-child(2), table.datatable td:nth-child(2){display:none}
  table.datatable th:nth-child(3), table.datatable td:nth-child(3){display:none}
  table.datatable th:nth-child(1), table.datatable td:nth-child(1){width:18%}
  table.datatable th:nth-child(4), table.datatable td:nth-child(4){width:16%}
  table.datatable th:nth-child(5), table.datatable td:nth-child(5){width:16%}
  table.datatable th:nth-child(6), table.datatable td:nth-child(6){width:16%}
  table.datatable th:nth-child(7), table.datatable td:nth-child(7){width:17%}
  table.datatable th:nth-child(8), table.datatable td:nth-child(8){width:17%}
  table.datatable th, table.datatable td{padding:.32rem .35rem;font-size:.78rem}
}
td.ticker-cell{font-family:"IBM Plex Mono",monospace;font-weight:700}
td.ticker-cell a{color:var(--accent2);text-decoration:none}
.company-name{font-family:"IBM Plex Sans",sans-serif;font-weight:400;color:var(--muted);font-size:.68rem;margin-left:.5rem}
.up{color:var(--good)} .dn{color:var(--bad)} .neutral{color:var(--muted)}
.hh-yes{background:rgba(155,196,127,.16);color:var(--good);font-weight:700;padding:.2rem .6rem;border-radius:4px;display:inline-block}
.obv-confirm{color:var(--good)} .obv-not{color:var(--bad)} .obv-neutral{color:var(--muted)}
.rs-yes{color:var(--good);font-weight:700} .rs-no{color:var(--rs-dim);font-weight:600} .rs-na{color:var(--rs-dim);opacity:.6}
.tier-12M{color:var(--good);font-weight:700} .tier-6M{color:var(--accent);font-weight:700} .tier-3M{color:var(--warn)} .tier-1M{color:var(--accent2)} .tier-none{color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:2rem 0;font-size:.85rem}
footer{margin-top:2rem;font-size:.62rem;color:var(--muted);border-top:1px solid var(--border);padding-top:1rem}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}
.sectionhead{grid-column:1/-1;font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  color:var(--text);margin:1.4rem 0 .2rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.sectionhead:first-child{margin-top:0}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.9rem;
  display:flex;flex-direction:column;gap:.5rem}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start}
.cardhead-left{display:flex;align-items:baseline;gap:.4rem}
.ticker{font-family:"IBM Plex Mono",monospace;font-weight:700;font-size:1.02rem;color:var(--text)}
.ticker a{color:inherit;text-decoration:none}
.ticker a:hover{color:var(--accent2)}
.chg{font-size:.72rem;font-weight:600}
.chartwrap{position:relative;width:100%;height:150px}
canvas{width:100%;height:100%;display:block}
.cardfoot{display:flex;justify-content:space-between;font-size:.68rem;color:var(--muted);
  border-top:1px solid var(--border);padding-top:.5rem}
.cardfoot .v{color:var(--text)}
.sitenav{position:sticky;top:0;z-index:500;display:flex;align-items:center;gap:1.4rem;
  background:var(--card, var(--surface2, var(--surface)));border-bottom:1px solid var(--border);
  padding:.7rem 1.1rem;margin-bottom:1.5rem;font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,sans-serif;
  box-shadow:0 2px 10px rgba(0,0,0,.12)}
.navicon{display:block;flex:none;width:30px;height:30px;border-radius:6px}
.sitenav-brand{font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  letter-spacing:.02em;color:var(--accent);text-decoration:none;white-space:nowrap}
.sitenav-brand:hover{opacity:.8}
.sitenav-divider{width:1px;height:1.3rem;background:var(--border);flex:none}
.sitenav-links{display:flex;gap:.2rem}
.sitenav-drop{position:relative}
.sitenav-toggle{background:transparent;border:none;color:var(--text, var(--ink));
  font-family:inherit;font-size:.8rem;padding:.5rem .65rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.3rem}
.sitenav-toggle:hover,.sitenav-drop.open .sitenav-toggle{background:var(--bg);color:var(--accent)}
.sitenav-caret{font-size:.85rem;opacity:1;color:var(--accent);display:inline-block;
  transition:transform .15s ease}
.sitenav-drop:hover .sitenav-caret,.sitenav-drop.open .sitenav-caret{transform:rotate(180deg)}
.sitenav-menu{position:absolute;top:100%;left:0;margin-top:.3rem;min-width:190px;
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.25);padding:.3rem;
  opacity:0;visibility:hidden;transform:translateY(-4px);
  transition:opacity .12s ease,transform .12s ease,visibility .12s}
.sitenav-drop:hover .sitenav-menu,.sitenav-drop.open .sitenav-menu{
  opacity:1;visibility:visible;transform:translateY(0)}
.sitenav-menu a{display:block;padding:.5rem .6rem;border-radius:6px;font-size:.78rem;
  color:var(--text, var(--ink));text-decoration:none;white-space:nowrap}
.sitenav-menu a:hover{background:var(--bg);color:var(--accent)}
.sitenav-menu a.active{color:var(--accent);font-weight:600}
.sitenav-toggle.active{background:var(--bg);color:var(--accent)}
@media (max-width:640px){
  .sitenav{padding:.5rem .7rem;gap:.7rem}
  .sitenav-brand{font-size:.7rem}
  .sitenav-toggle{font-size:.74rem;padding:.45rem .5rem}
  .sitenav-menu{min-width:170px}
}
</style>
</head>
<body>
<nav class="sitenav">
<img class="navicon" src="data:image/png;base64,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" alt="">
<a class="sitenav-brand" href="index.html">Brian Yum Cha</a>
<span class="sitenav-divider"></span>
<div class="sitenav-links">
<div class="sitenav-drop"><button class="sitenav-toggle" type="button">Screeners <span class="sitenav-caret">&#9662;</span></button><div class="sitenav-menu">
<a href="higher-high.html" class="active">Higher-High</a>
<a href="pre-breakout.html">Pre-Breakout (OBV)</a>
<a href="pullback.html">Pullback (Zag Zone)</a>
</div></div>
<div class="sitenav-drop"><button class="sitenav-toggle" type="button">ASX Sector Indexes <span class="sitenav-caret">&#9662;</span></button><div class="sitenav-menu">
<a href="energy-index.html">Energy</a>
<a href="healthcare-index.html">Healthcare</a>
<a href="materials-index.html">Materials</a>
<a href="tech-index.html">Tech</a>
</div></div>
<a class="sitenav-toggle" href="insider-index.html">Insider Buying</a>
</div>
</nav>
<script>
document.querySelectorAll('.sitenav-drop').forEach(function(drop){
  var toggle = drop.querySelector('.sitenav-toggle');
  toggle.addEventListener('click', function(e){
    e.stopPropagation();
    var willOpen = !drop.classList.contains('open');
    document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });
    if (willOpen) drop.classList.add('open');
  });
});
document.addEventListener('click', function(){
  document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });
});
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });
});
</script>


<div class="wrap">
  <div class="topbar">
    <div>
      <h1>⬆️ ASX Higher-High Screener</h1>
      <div class="subtitle">Structural breakout of the last swing high, by sector — port of the "HH Indicator (BT)" TradingView scripts, full ASX universe.</div>
      <div class="subtitle">Runs weekdays at 10:30am, 1pm, 3:30pm &amp; 4:30pm intraday, plus 5pm after close — all Sydney time.</div>
    </div>
    <div class="topbar-right">
      <button class="copybtn" id="copyBtn">📋 Copy TradingView list</button>
      <button class="themebtn" id="themeBtn" title="Toggle light/dark">🌙</button>
    </div>
  </div>
  <div class="session">##SESSION_LINE##</div>

  <div class="notice">⚠ Static report from a single scan run — not live. The wave/structure validity is NOT auto-verified. Not financial advice.</div>

  <div class="controls">
    <label class="ctrllabel" for="tierToggle">New High Strength</label>
    <div class="pillgroup" id="tierToggle" title="Only show tickers whose new high reaches back at least this far">
      <button class="pill active" data-tier="0">All</button>
      <button class="pill" data-tier="1">1M+</button>
      <button class="pill" data-tier="2">3M+</button>
      <button class="pill" data-tier="3">6M+</button>
      <button class="pill" data-tier="4">12M+</button>
    </div>
    <label class="ctrllabel" for="chartTfToggle" id="chartTfLabel" style="display:none">Chart Range</label>
    <div class="pillgroup" id="chartTfToggle" style="display:none">
      <button class="pill" data-ctf="21">1M</button>
      <button class="pill" data-ctf="63">3M</button>
      <button class="pill active" data-ctf="126">6M</button>
    </div>
  </div>
  <div class="controls">
    <div class="pillgroup" id="modeToggle">
      <button class="pill" data-mode="chart">📊 Charts</button>
      <button class="pill active" data-mode="table">☰ Table</button>
    </div>
    <div class="pillgroup" id="tfToggle">
      <button class="pill active" data-tf="daily">Daily</button>
      <button class="pill" data-tf="weekly">Weekly</button>
    </div>
    <input type="text" id="search" placeholder="Search ticker...">
  </div>

  <div class="sectorrow" id="sectorRow"></div>

  <div id="sectors"></div>
  <div id="grid" class="grid" style="display:none"></div>
  <footer>##FOOTER_NOTE##</footer>
</div>

<script>
const themeBtn = document.getElementById('themeBtn');
function isLightTheme() { return document.documentElement.getAttribute('data-theme') === 'light'; }
function syncThemeBtn() { themeBtn.textContent = isLightTheme() ? '☀️' : '🌙'; }
syncThemeBtn();
themeBtn.addEventListener('click', () => {
  const next = isLightTheme() ? null : 'light';
  if (next) document.documentElement.setAttribute('data-theme', next);
  else document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('theme', next || 'dark'); } catch (e) {}
  syncThemeBtn();
  render();  // re-run chart-mode canvas drawing with theme-correct colors
});

const DATA = ##DATA_JSON##;
const SECTOR_ORDER = ##SECTOR_ORDER_JSON##;

let state = { mode: 'table', tf: 'daily', chartTf: 126, search: '', sector: null, minTier: 0 };
let currentVisibleTickers = [];

// Monotonic rank matching high_tier()'s own tier order - a 6M high is
// also a 3M and 1M high, so "6M+" means rank >= 3, not "exactly 6M".
const TIER_RANK = { '12M': 4, '6M': 3, '3M': 2, '1M': 1 };
function tierRank(tier) { return TIER_RANK[tier] || 0; }

const sectorsPresent = [...new Set(DATA.map(r => r.sector))];
const sectorRow = document.getElementById('sectorRow');
sectorRow.innerHTML = '<button class="sectorpill active" data-sector="">All sectors</button>' +
  SECTOR_ORDER.filter(s => sectorsPresent.includes(s)).map(s => `<button class="sectorpill" data-sector="${esc(s)}">${esc(s)}</button>`).join('');
sectorRow.addEventListener('click', e => {
  if (!e.target.dataset.hasOwnProperty('sector')) return;
  state.sector = e.target.dataset.sector || null;
  sectorRow.querySelectorAll('.sectorpill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});

document.getElementById('modeToggle').addEventListener('click', e => {
  if (!e.target.dataset.mode) return;
  state.mode = e.target.dataset.mode;
  document.querySelectorAll('#modeToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  document.getElementById('chartTfToggle').style.display = state.mode === 'chart' ? '' : 'none';
  document.getElementById('chartTfLabel').style.display = state.mode === 'chart' ? '' : 'none';
  render();
});
document.getElementById('tfToggle').addEventListener('click', e => {
  if (!e.target.dataset.tf) return;
  state.tf = e.target.dataset.tf;
  document.querySelectorAll('#tfToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('tierToggle').addEventListener('click', e => {
  if (!e.target.dataset.tier) return;
  state.minTier = parseInt(e.target.dataset.tier, 10);
  document.querySelectorAll('#tierToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('chartTfToggle').addEventListener('click', e => {
  if (!e.target.dataset.ctf) return;
  state.chartTf = parseInt(e.target.dataset.ctf, 10);
  document.querySelectorAll('#chartTfToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('search').addEventListener('input', e => { state.search = e.target.value.toUpperCase(); render(); });
document.getElementById('copyBtn').addEventListener('click', () => {
  const text = currentVisibleTickers.map(t => `ASX:${t}`).join(',');
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copyBtn');
    btn.classList.add('copied');
    btn.textContent = `✓ Copied ${currentVisibleTickers.length} tickers`;
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋 Copy TradingView list'; }, 1800);
  });
});

function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// SeaBee names come back ALL CAPS (e.g. "CHALLENGER LIMITED") - title-case
// for readability next to the ticker, but leave short all-caps tokens
// (LTD, NL, plc-style suffixes under 4 chars) alone so they don't look odd.
function titleCase(s){
  return String(s).split(' ').map(w => w.length <= 3 && w === w.toUpperCase() ? w : w.charAt(0) + w.slice(1).toLowerCase()).join(' ');
}

function fmtMcap(v){
  if (!v) return '–';
  if (v >= 1e9) return '$' + (v/1e9).toFixed(1) + 'B';
  if (v >= 1e6) return '$' + (v/1e6).toFixed(0) + 'M';
  return '$' + (v/1e3).toFixed(0) + 'K';
}

function sma(closes, period) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function drawChart(canvas, r, tf) {
  const n = r.closes.length;
  const start = Math.max(0, n - tf);
  const closes = r.closes.slice(start), opens = r.opens.slice(start),
        highs = r.highs.slice(start), lows = r.lows.slice(start);
  if (closes.length < 2) return;

  const sma20 = sma(r.closes, 20).slice(start);
  const sma50 = sma(r.closes, 50).slice(start);

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const allVals = [...highs, ...lows, ...sma20.filter(v=>v!=null), ...sma50.filter(v=>v!=null)];
  const lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = (hi - lo) * 0.08 || 1;
  const yMin = lo - pad, yMax = hi + pad;
  const y = v => H - ((v - yMin) / (yMax - yMin)) * H;
  const n2 = closes.length;
  const cw = W / n2;
  const x = i => i * cw + cw / 2;

  function line(series, color, dashed) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.setLineDash(dashed ? [3, 3] : []);
    let started = false;
    for (let i = 0; i < series.length; i++) {
      if (series[i] == null) continue;
      const px = x(i), py = y(series[i]);
      if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  line(sma50, isLightTheme() ? 'rgba(61,56,41,0.55)' : 'rgba(201,194,176,0.55)', true);
  line(sma20, isLightTheme() ? 'rgba(22,53,64,0.85)' : 'rgba(143,196,219,0.85)', false);

  // Deliberately more saturated than the --good/--bad text tokens (those
  // are tuned to sit quietly inline in prose; a candlestick needs to read
  // as up/down at a glance, so it gets its own punchier pair here).
  const CANDLE_UP   = isLightTheme() ? '#15803d' : '#4ade80';
  const CANDLE_DOWN = isLightTheme() ? '#b91c1c' : '#f87171';

  for (let i = 0; i < n2; i++) {
    const up = closes[i] >= opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? CANDLE_UP : CANDLE_DOWN;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(i), y(highs[i]));
    ctx.lineTo(x(i), y(lows[i]));
    ctx.stroke();
    const bodyTop = y(Math.max(opens[i], closes[i]));
    const bodyBot = y(Math.min(opens[i], closes[i]));
    const bw = Math.max(1, cw * 0.6);
    ctx.fillRect(x(i) - bw / 2, bodyTop, bw, Math.max(1, bodyBot - bodyTop));
  }

  const lastPrice = closes[closes.length - 1];
  const up = r.change_1d >= 0;
  ctx.font = '600 10px "IBM Plex Mono", monospace';
  const label = lastPrice.toFixed(lastPrice < 1 ? 3 : 2);
  const tw = ctx.measureText(label).width;
  const bx = W - tw - 10, by = y(lastPrice);
  ctx.fillStyle = up ? CANDLE_UP : CANDLE_DOWN;
  ctx.fillRect(bx - 4, by - 8, tw + 8, 16);
  ctx.fillStyle = isLightTheme() ? '#fdf8ef' : '#1c1204';
  ctx.fillText(label, bx, by + 3);
}

function cardHtml(r) {
  const chgClass = r.change_1d > 0 ? 'up' : r.change_1d < 0 ? 'dn' : 'neutral';
  const chgSign = r.change_1d > 0 ? '+' : '';
  const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${r.ticker}`;
  const tier = r.high_tier;
  const tierClass = tier ? `tier-${tier}` : 'tier-none';
  const tierTitle = tier ? `New ${tier} high` : 'Not even a 1-month high - a low-significance pivot break';
  return `<div class="card" data-ticker="${esc(r.ticker)}">
    <div class="cardhead">
      <div class="cardhead-left">
        <span class="ticker"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(r.ticker)} ↗</a></span>
        <span class="chg ${chgClass}">${chgSign}${r.change_1d.toFixed(1)}%</span>
      </div>
      <span class="hh-yes ${tierClass}" title="${tierTitle}">${tier || '<1M'} HIGH</span>
    </div>
    <div class="chartwrap"><canvas></canvas></div>
    <div class="cardfoot"><span>${esc(r.industry)}</span><span class="v">$${r.price.toFixed(r.price < 1 ? 3 : 2)}</span></div>
  </div>`;
}

function renderTable(visible, sigKey, obvKey) {
  const bySector = {};
  visible.forEach(r => { (bySector[r.sector] = bySector[r.sector] || []).push(r); });

  const sectors = SECTOR_ORDER.filter(s => bySector[s] && bySector[s].length);
  const container = document.getElementById('sectors');

  if (sectors.length === 0) {
    container.innerHTML = '<div class="empty">No stocks match the current filters.</div>';
    return;
  }

  container.innerHTML = sectors.map(sec => {
    const list = [...bySector[sec]].sort((a, b) => a.ticker.localeCompare(b.ticker));
    const rows = list.map(r => {
      const chgClass = r.change_1d > 0 ? 'up' : r.change_1d < 0 ? 'dn' : 'neutral';
      const chgSign = r.change_1d > 0 ? '+' : '';
      const obv = r[obvKey];
      const obvClass = obv === 'Confirming' ? 'obv-confirm' : obv === 'Not confirming' ? 'obv-not' : 'obv-neutral';
      const obvShort = obv === 'Confirming' ? '✓' : obv === 'Not confirming' ? '✗' : obv === 'Neutral' ? '~' : '-';
      const tier = r.high_tier;
      const tierClass = tier ? `tier-${tier}` : 'tier-none';
      const tierTitle = tier ? `New ${tier} high` : 'Not even a 1-month high - a low-significance pivot break';
      const rsWord = v => v === true ? 'Yes' : v === false ? 'No' : 'N/A';
      const rsClass = v => v === true ? 'rs-yes' : v === false ? 'rs-no' : 'rs-na';
      const rsChar = v => v === true ? '✓' : v === false ? '✗' : '–';
      const rsTitle = `RS vs XJO: ${rsWord(r.rs_market)}`;
      const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${r.ticker}`;
      return `<tr>
        <td class="ticker-cell"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(r.ticker)}</a><span class="company-name">${esc(titleCase(r.name))}</span></td>
        <td style="color:var(--muted);font-size:.72rem">${esc(r.industry)}</td>
        <td style="color:var(--muted);font-size:.72rem">${fmtMcap(r.market_cap)}</td>
        <td>$${r.price.toFixed(r.price < 1 ? 3 : 2)}</td>
        <td class="${chgClass}">${chgSign}${r.change_1d.toFixed(1)}%</td>
        <td class="${obvClass}" title="${obv ? esc(obv) : 'No OBV read'}">${obvShort}</td>
        <td class="${tierClass}" title="${tierTitle}">${tier || '<1M'}</td>
        <td class="${rsClass(r.rs_market)}" title="${rsTitle}">XJO ${rsChar(r.rs_market)}</td>
      </tr>`;
    }).join('');
    return `<div class="sector">
      <div class="sector-head"><h2>${esc(sec)}</h2><span class="count">${list.length} stocks</span></div>
      <table class="datatable">
        <thead><tr><th>Ticker</th><th>Industry</th><th>Mkt Cap</th><th>Price</th><th>1D Chg</th><th>OBV</th><th>High</th><th>RS</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }).join('');
}

function renderCharts(visible, sigKey) {
  const list = visible.filter(r => r[sigKey] && r.dates);
  const bySector = {};
  list.forEach(r => { (bySector[r.sector] = bySector[r.sector] || []).push(r); });
  const sectors = SECTOR_ORDER.filter(s => bySector[s] && bySector[s].length);
  const grid = document.getElementById('grid');

  if (sectors.length === 0) {
    grid.innerHTML = '<div class="empty">No stocks match the current filters.</div>';
    return;
  }

  let html = '';
  sectors.forEach(sec => {
    const secList = [...bySector[sec]].sort((a, b) => a.ticker.localeCompare(b.ticker));
    html += `<div class="sectionhead">${esc(sec)} (${secList.length})</div>` + secList.map(cardHtml).join('');
  });
  grid.innerHTML = html;

  grid.querySelectorAll('.card').forEach(el => {
    const r = DATA.find(d => d.ticker === el.dataset.ticker);
    const canvas = el.querySelector('canvas');
    requestAnimationFrame(() => drawChart(canvas, r, state.chartTf));
  });
}

function render() {
  const sigKey = state.tf === 'daily' ? 'hh_daily' : 'hh_weekly';
  const obvKey = state.tf === 'daily' ? 'obv_daily' : 'obv_weekly';

  let visible = DATA.filter(r => !state.search || r.ticker.includes(state.search));
  if (state.sector) visible = visible.filter(r => r.sector === state.sector);
  visible = visible.filter(r => r[sigKey]);
  if (state.minTier > 0) visible = visible.filter(r => tierRank(r.high_tier) >= state.minTier);
  currentVisibleTickers = visible.map(r => r.ticker);

  const sectorsEl = document.getElementById('sectors');
  const gridEl = document.getElementById('grid');

  if (state.mode === 'chart') {
    sectorsEl.style.display = 'none';
    gridEl.style.display = '';
    renderCharts(visible, sigKey);
  } else {
    gridEl.style.display = 'none';
    sectorsEl.style.display = '';
    renderTable(visible, sigKey, obvKey);
  }
}
render();
</script>
</body>
</html>"""


def build_html_report(results, total_scanned, fresh_today, out_path):
    now = datetime.now(SYDNEY_TZ)
    n_daily = sum(1 for r in results if r['hh_daily'])
    n_weekly = sum(1 for r in results if r['hh_weekly'])
    # "X scanned" alone is just the universe size - it looks identical on a
    # clean run and a heavily Yahoo-throttled one, since every ticker gets
    # attempted either way. Surface how many actually had a fresh (today's)
    # cached session too, so a degraded run is visible in the report itself
    # instead of only discoverable by reading GitHub Actions logs.
    session_line = (
        f"Session {now.strftime('%Y-%m-%d %H:%M')} Sydney time · {total_scanned} scanned "
        f"({fresh_today} fresh today) · {n_daily} daily NEW HH · {n_weekly} weekly NEW HH"
    )
    html = HTML_TEMPLATE
    html = html.replace('##SESSION_LINE##', session_line)
    html = html.replace('##FOOTER_NOTE##',
        'ASX Higher-High Screener · Data via Yahoo Finance (yfinance), universe + industry via SeaBee · '
        'Pivot: 3-left/3-right body-top pivot, confirmed 3 bars after forming. Signal: close crosses back above the last confirmed pivot high, '
        'one-shot-per-pivot guarded (see script docstring for the bug this fixes vs. the original Pine version).')
    html = html.replace('##SECTOR_ORDER_JSON##', json.dumps(SECTOR_ORDER))
    html = html.replace('##DATA_JSON##', json.dumps(results))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  📄 HTML report → {out_path}")


def build_csv(results, out_path):
    import csv
    fieldnames = ['ticker', 'name', 'sector', 'industry', 'market_cap', 'price', 'change_1d',
                  'hh_daily', 'hh_weekly', 'obv_daily', 'obv_weekly', 'high_tier',
                  'rs_market']
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"  📊 CSV export  → {out_path}")


# ─── TELEGRAM NOTIFICATION ─────────────────────────────────────────────────────

TELEGRAM_SEEN_PATH = os.path.join(SCRIPT_DIR, "seen_tickers_hh_telegram.json")


def send_hh_telegram(results):
    """
    Posts every current daily NEW HH signal to Telegram on every run,
    grouped by sector in a monospace table (Telegram has no real table
    support, but a ``` code block preserves fixed-width spacing, which is
    the usual way to fake clean columns in a chat message - bold doesn't
    reliably nest inside one, so a leading "*" column marks tickers
    appearing for the first time since the last run instead). Each row
    also shows the high_tier flag (see high_tier()'s docstring) so a
    quick glance shows whether a signal is breaking real resistance or
    just a recent local wiggle, without needing to open the HTML report.

    No-ops quietly (prints why, doesn't raise) if the bot isn't
    configured, or on a --tickers test run - matches the circuit
    breaker's own "don't do this on a small test slice" philosophy.
    """
    token = os.environ.get("TELEGRAM_HH_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_HH_CHAT_ID")
    if not token or not chat_id:
        print("  ℹ Telegram not configured (TELEGRAM_HH_BOT_TOKEN/TELEGRAM_HH_CHAT_ID unset) - skipping notification")
        return

    daily = [r for r in results if r["hh_daily"]]
    if not daily:
        print("  ℹ No daily NEW HH signals this run - nothing to send")
        return

    try:
        with open(TELEGRAM_SEEN_PATH) as f:
            previously_seen = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        previously_seen = set()

    by_sector = {}
    for r in daily:
        by_sector.setdefault(r["sector"], []).append(r)

    now = datetime.now(SYDNEY_TZ)
    lines = [f"📈 *ASX Higher-High Screener* — {now.strftime('%Y-%m-%d %H:%M')} Sydney time", ""]
    for sector in SECTOR_ORDER:
        rows = sorted(by_sector.get(sector, []), key=lambda r: r["ticker"])
        if not rows:
            continue
        lines.append(f"*{sector}*")
        lines.append("```")
        lines.append(f"{'':<1}{'TICKER':<7}{'CHG':<8}{'HIGH':<7}{'OBV'}")
        for r in rows:
            marker = "*" if r["ticker"] not in previously_seen else " "
            tier = r["high_tier"] or "-"
            chg = f"{r['change_1d']:+.1f}%"
            obv = r.get("obv_daily")
            obv_mark = "Y" if obv == "Confirming" else "N" if obv == "Not confirming" else "~" if obv == "Neutral" else ""
            lines.append(f"{marker:<1}{r['ticker']:<7}{chg:<8}{tier:<7}{obv_mark}")
        lines.append("```")
    message = "\n".join(lines).strip()

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=30,
        )
        resp.raise_for_status()
        if not resp.json().get("ok"):
            print(f"  ⚠ Telegram API error: {resp.json()}")
            return
    except Exception as e:
        print(f"  ⚠ Telegram send failed: {e}")
        return

    with open(TELEGRAM_SEEN_PATH, "w") as f:
        json.dump(sorted({r["ticker"] for r in daily}), f)
    print(f"  📨 Telegram: sent {len(daily)} daily NEW HH signals")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ASX Higher-High Screener (sector grid)')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--tickers', type=str, default='', help='Comma-separated ticker list (overrides full ASX scan)')
    parser.add_argument('--no-open', action='store_true', help="Don't auto-open the HTML report in a browser tab when done")
    parser.add_argument('--min-vol', type=int, default=MIN_AVG_VOLUME)
    parser.add_argument('--min-price', type=float, default=MIN_PRICE)
    parser.add_argument('--min-mcap', type=float, default=MIN_MARKET_CAP)
    args = parser.parse_args()

    import __main__ as _m
    _m.MIN_PRICE = args.min_price
    _m.MIN_MARKET_CAP = args.min_mcap
    _m.MIN_AVG_VOLUME = args.min_vol

    print("=" * 60)
    print("  ASX HIGHER-HIGH SCREENER (SECTOR GRID)")
    print("=" * 60)

    universe = get_asx_universe()

    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
        universe = {t: universe.get(t, {"industry": "Other", "sector": "Other", "market_cap": 0, "name": t}) for t in wanted}
        print(f"  Using custom list: {len(universe)} tickers")

    classify_materials_commodities(universe)
    classify_healthcare_indications(universe)
    classify_energy_fuels(universe)
    classify_tech_categories(universe)

    results, usable, fresh_today = run_scan(universe, workers=args.workers)
    results.sort(key=lambda r: r['ticker'])

    # Circuit breaker: on a full-universe run, if the shared price cache
    # doesn't have usable data for most of the universe - whether from
    # today's fetches or an earlier run's still-recent ones - the pipeline
    # is broken (Yahoo Finance throttling the source IP with nothing decent
    # cached yet), not "a quiet day" - abort instead of publishing a
    # near-empty report. Skipped for --tickers test runs, which are too
    # small for this ratio to mean anything.
    if not args.tickers and len(universe) >= MIN_UNIVERSE_FOR_CHECK:
        usable_ratio = usable / len(universe)
        if usable_ratio < MIN_FETCH_RATIO:
            print(f"\n❌ Only {usable}/{len(universe)} tickers ({usable_ratio:.0%}) have usable cached "
                  f"price data - likely Yahoo Finance throttling this machine/IP with nothing decent "
                  f"cached yet. Aborting WITHOUT writing a report, so the last good one stays live.")
            sys.exit(1)

    out_base = os.path.join(SCRIPT_DIR, 'asx_hh_results')
    html_path = out_base + '.html'
    csv_path = out_base + '.csv'

    print(f"\n💾 Saving to:\n   {html_path}\n   {csv_path}\n")

    try:
        build_html_report(results, len(universe), fresh_today, html_path)
    except Exception as e:
        print(f"  ⚠ HTML save error: {e}")
    try:
        build_csv(results, csv_path)
    except Exception as e:
        print(f"  ⚠ CSV save error: {e}")

    if not args.tickers:
        try:
            send_hh_telegram(results)
        except Exception as e:
            print(f"  ⚠ Telegram notification error: {e}")

    if not args.no_open and os.path.isfile(html_path):
        try:
            import webbrowser
            webbrowser.open(html_path)
            print(f"  🌐 Opening HTML report in your browser...")
        except Exception:
            print(f"  Open manually: {html_path}")


def _pause_and_exit(code=0):
    print("\n  Press Enter to exit...")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        _pause_and_exit()
    except Exception as e:
        import traceback
        print("\n" + "="*60)
        print("  ❌ UNEXPECTED ERROR")
        print("="*60)
        traceback.print_exc()
        _pause_and_exit(1)
