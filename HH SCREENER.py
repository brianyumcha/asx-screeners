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


# ─── REAL ESTATE CATEGORY CLASSIFICATION ───────────────────────────────────
# Unlike Materials/Healthcare/Energy/Tech, Real Estate doesn't need a
# business-summary keyword classifier - Yahoo's own "industry" field is
# already a clean, specific taxonomy for most tickers in this sector (REIT
# - Diversified/Retail/Industrial/Office/Residential/Specialty/Healthcare
# Facilities, Real Estate - Development/Diversified, Real Estate Services),
# confirmed by inspecting all 64 Real Estate-sector tickers' real Yahoo
# industry + longBusinessSummary text, 2026-09-16. This mostly just passes
# that field through, with a small manual-override map for the handful of
# tickers Yahoo tags with an unrelated industry (a proptech company tagged
# "Software", a property fund manager tagged "Asset Management", etc).
REALESTATE_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "realestate_category_cache.json")

REALESTATE_MANUAL_OVERRIDES = {
    "PXA": "Real Estate Services",  # PEXA - e-conveyancing/property settlement platform, Yahoo tags "Software - Application"
    "AXI": "Real Estate Services",  # Axtec - property management/workflow software + payments, Yahoo tags "Specialty Industrial Machinery"
    "QAL": "Real Estate Services",  # Qualitas - alternative real estate fund manager (not a REIT itself), Yahoo tags "Asset Management"
    "REP": "REIT - Diversified",    # RAM Essential Services - stapled REIT, healthcare-weighted, Yahoo mistags "Asset Management"
}


def classify_realestate_category(ticker, industry):
    """Passes Yahoo's own "industry" through when it's already a specific
    REIT/Real-Estate category, applies REALESTATE_MANUAL_OVERRIDES for
    known stale/generic tags, and falls back to "Real Estate"
    (unclassified) when industry is missing entirely."""
    if ticker in REALESTATE_MANUAL_OVERRIDES:
        return REALESTATE_MANUAL_OVERRIDES[ticker]
    if industry and (industry.startswith("REIT") or industry.startswith("Real Estate")):
        return industry
    return "Real Estate"


def classify_realestate_categories(universe):
    """Mutates `universe` in place: for every Real Estate-sector ticker,
    replaces the generic "industry" value with its specific category. Only
    fetches Yahoo's .info for tickers not already in the on-disk cache."""
    try:
        with open(REALESTATE_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    re_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Real Estate" and len(t) == 3
    ]
    new_tickers = [t for t in re_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Real Estate ticker(s) by category...")
        for t in new_tickers:
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_realestate_category(t, info.get("industry"))
            except Exception:
                cache[t] = "Real Estate"
        with open(REALESTATE_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in re_tickers:
        universe[t]["industry"] = cache.get(t, "Real Estate")


# ─── FINANCIALS CATEGORY CLASSIFICATION ────────────────────────────────────
# Same approach as Real Estate - Yahoo's own "industry" field is already a
# clean, specific taxonomy for most Financials-sector tickers (Asset
# Management, Credit Services, Capital Markets, Banks - Regional/
# Diversified, Insurance - Property & Casualty/Life/Specialty, Insurance
# Brokers, Financial Conglomerates, Mortgage Finance), confirmed by
# inspecting all 121 Financials-sector tickers' real Yahoo industry +
# longBusinessSummary text, 2026-09-17. The one gap: a real cluster of
# payments/lending fintechs Yahoo tags as generic "Software" industries
# (EML, TYR, CCL, B4P, QFE, RZI, plus Block Inc's ASX CDI listing) - these
# are genuinely financial services, not tech, so get their own category.
FINANCIALS_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "financials_category_cache.json")

FINANCIALS_EXCLUDED_TICKERS = {"EVE", "MPR"}  # EVE Health (health co) and MPR Australia (solar) - stale GICS tags, same failure mode as CML/DTZ/NVX

FINANCIALS_MANUAL_OVERRIDES = {
    "B4P": "Fintech / Payments", "CCA": "Fintech / Payments", "CCL": "Fintech / Payments",
    "EML": "Fintech / Payments", "QFE": "Fintech / Payments", "RZI": "Fintech / Payments",
    "TYR": "Fintech / Payments", "XYZ": "Fintech / Payments",
    "CCV": "Consumer Finance", "FPR": "Consumer Finance",
    "8IH": "Asset Management", "SCP": "Asset Management", "HAL": "Asset Management",
    "HMC": "Asset Management", "NWL": "Asset Management", "IFL": "Asset Management",
    "MQG": "Financial Conglomerates",
}


def classify_financials_category(ticker, industry):
    if ticker in FINANCIALS_MANUAL_OVERRIDES:
        return FINANCIALS_MANUAL_OVERRIDES[ticker]
    if industry and (industry.startswith("Banks") or industry.startswith("Insurance") or industry in (
            "Asset Management", "Credit Services", "Capital Markets", "Financial Conglomerates",
            "Mortgage Finance", "Financial Data & Stock Exchanges")):
        return industry
    return "Financials"


def classify_financials_categories(universe):
    try:
        with open(FINANCIALS_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    fin_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Financials" and len(t) == 3 and t not in FINANCIALS_EXCLUDED_TICKERS
    ]
    new_tickers = [t for t in fin_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Financials ticker(s) by category...")
        for t in new_tickers:
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_financials_category(t, info.get("industry"))
            except Exception:
                cache[t] = "Financials"
        with open(FINANCIALS_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in fin_tickers:
        universe[t]["industry"] = cache.get(t, "Financials")


# ─── CONSUMER STAPLES CATEGORY CLASSIFICATION ──────────────────────────────
# Yahoo's own "industry" field is already clean for Consumer Staples too
# (Packaged Foods, Farm Products, Beverages - Wineries & Distilleries/Non-
# Alcoholic, Household & Personal Products, Grocery Stores, Confectioners,
# Food Distribution) - confirmed by inspecting all 57 Consumer Staples-
# sector tickers' real Yahoo industry + longBusinessSummary text,
# 2026-09-17. A handful of genuinely unrelated businesses (pharma/biotech/
# AI) carry a stale Consumer Staples sector tag, same failure mode as
# CML/DTZ/NVX for Tech.
STAPLES_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "staples_category_cache.json")

STAPLES_EXCLUDED_TICKERS = {"BLS", "DAI", "EXL"}  # BLS Pharmaceuticals, Decidr AI Industries, Elixinol Wellness (drug manufacturer) - stale GICS tags


def classify_staples_category(industry):
    if industry and (industry.startswith("Beverages") or industry in (
            "Packaged Foods", "Farm Products", "Household & Personal Products",
            "Grocery Stores", "Confectioners", "Food Distribution")):
        return industry
    return "Consumer Staples"


def classify_staples_categories(universe):
    try:
        with open(STAPLES_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    staples_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Consumer Staples" and len(t) == 3 and t not in STAPLES_EXCLUDED_TICKERS
    ]
    new_tickers = [t for t in staples_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Consumer Staples ticker(s) by category...")
        for t in new_tickers:
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_staples_category(info.get("industry"))
            except Exception:
                cache[t] = "Consumer Staples"
        with open(STAPLES_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in staples_tickers:
        universe[t]["industry"] = cache.get(t, "Consumer Staples")


# ─── CONSUMER DISCRETIONARY CATEGORY CLASSIFICATION ────────────────────────
# Same pass-through approach as Real Estate: unlike Financials/Staples,
# inspecting all 109 Consumer Discretionary-sector tickers' real Yahoo
# industry + longBusinessSummary text (2026-09-17) found no real cluster of
# systematically mistagged tickers - every industry value present is a
# genuine (if sometimes niche) description of what the company does, just
# narrower than the 11 GICS sectors. So there's no need for a curated
# whitelist here: pass Yahoo's own industry straight through, and only the
# genuine gaps (no industry data at all - usually a thin/inactive ticker)
# fall back to the generic sector-name bucket.
DISCRETIONARY_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "discretionary_category_cache.json")

DISCRETIONARY_MANUAL_OVERRIDES = {}


def classify_discretionary_category(industry):
    if industry:
        return industry
    return "Consumer Discretionary"


def classify_discretionary_categories(universe):
    try:
        with open(DISCRETIONARY_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    disc_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Consumer Discretionary" and len(t) == 3
    ]
    new_tickers = [t for t in disc_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Consumer Discretionary ticker(s) by category...")
        for t in new_tickers:
            if t in DISCRETIONARY_MANUAL_OVERRIDES:
                cache[t] = DISCRETIONARY_MANUAL_OVERRIDES[t]
                continue
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_discretionary_category(info.get("industry"))
            except Exception:
                cache[t] = "Consumer Discretionary"
        with open(DISCRETIONARY_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in disc_tickers:
        universe[t]["industry"] = cache.get(t, "Consumer Discretionary")


# ─── INDUSTRIALS CATEGORY CLASSIFICATION ───────────────────────────────────
# Same pass-through approach - inspecting all 148 Industrials-sector
# tickers' real Yahoo industry + longBusinessSummary text (2026-09-17)
# found no systematic mistagging cluster either, just the usual handful of
# niche businesses with no industry data at all. One deliberate override:
# SGH (Seven Group Holdings, ~$15B mcap - WesTrac/Coates Hire/Boral/Seven
# West Media) comes back from Yahoo with industry=None despite being the
# sector's largest constituent by far, so it would otherwise silently land
# in the generic catch-all bucket.
INDUSTRIALS_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "industrials_category_cache.json")

INDUSTRIALS_MANUAL_OVERRIDES = {
    "SGH": "Conglomerates",
}


def classify_industrials_category(industry):
    if industry:
        return industry
    return "Industrials"


def classify_industrials_categories(universe):
    try:
        with open(INDUSTRIALS_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    ind_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Industrials" and len(t) == 3
    ]
    new_tickers = [t for t in ind_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Industrials ticker(s) by category...")
        for t in new_tickers:
            if t in INDUSTRIALS_MANUAL_OVERRIDES:
                cache[t] = INDUSTRIALS_MANUAL_OVERRIDES[t]
                continue
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_industrials_category(info.get("industry"))
            except Exception:
                cache[t] = "Industrials"
        with open(INDUSTRIALS_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in ind_tickers:
        universe[t]["industry"] = cache.get(t, "Industrials")


# ─── UTILITIES CATEGORY CLASSIFICATION ─────────────────────────────────────
# Pass-through, same as Discretionary/Industrials - inspecting all 20
# Utilities-sector tickers' real Yahoo industry + longBusinessSummary text
# (2026-09-17) found a clean, specific taxonomy already ('Utilities -
# Renewable', 'Utilities - Independent Power Producers', 'Utilities -
# Diversified', 'Utilities - Regulated Gas', 'Utilities - Regulated
# Electric'), no systematic mistagging cluster to whitelist against.
UTILITIES_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "utilities_category_cache.json")

UTILITIES_MANUAL_OVERRIDES = {}


def classify_utilities_category(industry):
    if industry:
        return industry
    return "Utilities"


def classify_utilities_categories(universe):
    try:
        with open(UTILITIES_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    util_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Utilities" and len(t) == 3
    ]
    new_tickers = [t for t in util_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Utilities ticker(s) by category...")
        for t in new_tickers:
            if t in UTILITIES_MANUAL_OVERRIDES:
                cache[t] = UTILITIES_MANUAL_OVERRIDES[t]
                continue
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_utilities_category(info.get("industry"))
            except Exception:
                cache[t] = "Utilities"
        with open(UTILITIES_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in util_tickers:
        universe[t]["industry"] = cache.get(t, "Utilities")


# ─── COMMUNICATION SERVICES CATEGORY CLASSIFICATION ────────────────────────
# Pass-through, same as Discretionary/Industrials/Utilities - inspecting
# all 53 Communication Services-sector tickers' real Yahoo industry +
# longBusinessSummary text (2026-09-17) found no systematic mistagging
# cluster - a handful of digital-marketing/content/fintech-comms names
# carry a generic 'Software' industry from Yahoo, but that's still a
# genuine (if narrow) description of what they do, not a wrong sector the
# way Financials' fintech-as-Software cluster was, so no whitelist needed.
COMMS_CATEGORY_CACHE_PATH = os.path.join(SCRIPT_DIR, "comms_category_cache.json")

COMMS_MANUAL_OVERRIDES = {}


def classify_comms_category(industry):
    if industry:
        return industry
    return "Communication Services"


def classify_comms_categories(universe):
    try:
        with open(COMMS_CATEGORY_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    comms_tickers = [
        t for t, d in universe.items()
        if d.get("sector") == "Communication Services" and len(t) == 3
    ]
    new_tickers = [t for t in comms_tickers if t not in cache]

    if new_tickers:
        print(f"   Classifying {len(new_tickers)} new Communication Services ticker(s) by category...")
        for t in new_tickers:
            if t in COMMS_MANUAL_OVERRIDES:
                cache[t] = COMMS_MANUAL_OVERRIDES[t]
                continue
            try:
                yahoo_sym = t if t.endswith(".AX") else t + ".AX"
                info = yf.Ticker(yahoo_sym).info
                cache[t] = classify_comms_category(info.get("industry"))
            except Exception:
                cache[t] = "Communication Services"
        with open(COMMS_CATEGORY_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)

    for t in comms_tickers:
        universe[t]["industry"] = cache.get(t, "Communication Services")


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
html,body{overflow-x:clip}
.wrap{max-width:1560px;margin:0 auto;padding:0 1.6rem 1.6rem}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.3rem;flex-wrap:wrap;gap:1rem}
@media (min-width:641px){
  .topbar{position:sticky;top:65px;z-index:400;background:var(--bg);padding:.9rem 0 .7rem;border-bottom:1px solid var(--border)}
}
h1{font-family:"Fraunces",Georgia,serif;font-size:2.2rem;font-weight:600;letter-spacing:-.01em;
  text-wrap:balance;color:var(--text)}
.subtitle{font-size:.92rem;color:var(--muted);margin-top:.3rem;max-width:60rem}
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
table.datatable{width:100%;table-layout:fixed;border-collapse:collapse;font-size:.8rem}
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
.navicon{display:block;flex:none;width:42px;height:42px;border-radius:8px}
.sitenav-brand{display:flex;flex-direction:column;gap:.05rem}
.sitenav-brand .brand-main{font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  letter-spacing:.02em;color:var(--accent);white-space:nowrap;text-decoration:none}
.sitenav-brand .brand-tag{font-family:"Fraunces",Georgia,serif;font-style:italic;font-size:.62rem;
  color:var(--muted);white-space:nowrap;text-decoration:none}
.sitenav-brand .brand-main:hover{opacity:.8}
.sitenav-brand .brand-tag:hover{opacity:.8}
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
.navburger{display:none;background:transparent;border:1px solid var(--border);color:var(--text, var(--ink));
  width:2.1rem;height:2.1rem;border-radius:6px;cursor:pointer;font-size:1rem;
  align-items:center;justify-content:center;flex:none;margin-left:auto}
.navburger:hover{border-color:var(--accent);color:var(--accent)}
.cart-egg{font-size:1.15rem;display:flex;align-items:center;padding:0 .3rem;text-decoration:none;opacity:.75;filter:grayscale(.3)}
.cart-egg:hover{opacity:1;filter:none;transform:scale(1.15)}
@media (max-width:640px){
  .sitenav{padding:.5rem .7rem;gap:.5rem .7rem;flex-wrap:wrap}
  .sitenav-brand{font-size:.7rem}
  .sitenav-toggle{font-size:.74rem;padding:.45rem .5rem}
  .sitenav-menu{min-width:170px}
  .navburger{display:flex}
  .sitenav-links{display:none;flex-basis:100%;flex-direction:column;align-items:stretch;gap:.15rem;margin-top:.4rem}
  .sitenav.menu-open .sitenav-links{display:flex}
  .sitenav-drop{width:100%}
  .sitenav-toggle{width:100%}
  .sitenav-menu{position:static;opacity:1;visibility:visible;transform:none;box-shadow:none;
    border:none;background:transparent;margin-top:0;padding:0 0 0 .8rem;
    max-height:0;overflow:hidden;transition:max-height .15s ease}
  .sitenav-drop.open .sitenav-menu{max-height:220px;padding-top:.2rem;padding-bottom:.2rem}
}
</style>
</head>
<body>
<nav class="sitenav">
<img class="navicon" src="data:image/png;base64,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" alt="">
<div class="sitenav-brand">
<a class="brand-main" href="index.html">Brian Yum Cha &mdash; ASX Trading</a>
<a class="brand-tag" href="timing-the-carts.html">Reading the charts. Timing the carts.</a>
</div>
<span class="sitenav-divider"></span>
<button class="navburger" type="button" aria-label="Toggle menu">&#9776;</button>
<div class="sitenav-links">
<div class="sitenav-drop"><button class="sitenav-toggle" type="button">Screeners <span class="sitenav-caret">&#9662;</span></button><div class="sitenav-menu">
<a href="higher-high.html" class="active">Higher-High</a>
<a href="momentum.html">Momentum</a>
<a href="pre-breakout.html">Pre-Breakout (OBV)</a>
<a href="pullback.html">Pullback (Zag Zone)</a>
<a href="volume-anomaly.html">Volume Anomaly</a>
</div></div>
<div class="sitenav-drop"><button class="sitenav-toggle" type="button">ASX Sector Indexes <span class="sitenav-caret">&#9662;</span></button><div class="sitenav-menu">
<a href="comms-index.html">Communication Services</a>
<a href="discretionary-index.html">Consumer Discretionary</a>
<a href="staples-index.html">Consumer Staples</a>
<a href="energy-index.html">Energy</a>
<a href="financials-index.html">Financials</a>
<a href="healthcare-index.html">Healthcare</a>
<a href="industrials-index.html">Industrials</a>
<a href="materials-index.html">Materials</a>
<a href="real-estate-index.html">Real Estate</a>
<a href="tech-index.html">Tech</a>
<a href="utilities-index.html">Utilities</a>
</div></div>
<div class="sitenav-drop"><button class="sitenav-toggle" type="button">Trading Tools <span class="sitenav-caret">&#9662;</span></button><div class="sitenav-menu">
<a href="insider-index.html">Insider Buying</a>
<a href="red-folder-news.html">Red Folder News</a>
<a href="sector-rotation.html">Sector Rotation</a>
<a href="earnings-calendar.html">Earnings Calendar</a>
</div></div>
<a class="cart-egg" href="timing-the-carts.html" title="Timing the Carts">🥟</a>
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
var sitenavEl = document.querySelector('.sitenav');
var navburgerEl = document.querySelector('.navburger');
if (navburgerEl && sitenavEl) {
  navburgerEl.addEventListener('click', function(e){
    e.stopPropagation();
    sitenavEl.classList.toggle('menu-open');
  });
}
document.addEventListener('click', function(){
  document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });
  if (sitenavEl) sitenavEl.classList.remove('menu-open');
});
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') {
    document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });
    if (sitenavEl) sitenavEl.classList.remove('menu-open');
  }
});
</script>


<div class="wrap">
  <div class="topbar">
    <div>
      <h1>⬆️ ASX Higher-High Screener</h1>
      <div class="subtitle">Flags ASX stocks breaking out to a new swing high — a close crossing back above the last confirmed pivot high, by sector, across the full ASX universe.</div>
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
    <label class="ctrllabel" for="modeToggle">Mode</label>
    <div class="pillgroup" id="modeToggle">
      <button class="pill" data-mode="chart">📊 Charts</button>
      <button class="pill active" data-mode="table">☰ Table</button>
    </div>
    <label class="ctrllabel" for="tfToggle">Signal</label>
    <div class="pillgroup" id="tfToggle">
      <button class="pill active" data-tf="daily">Daily</button>
      <button class="pill" data-tf="weekly">Weekly</button>
    </div>
    <input type="text" id="search" placeholder="Search ticker...">
  </div>

  <div class="ctrllabel" style="margin-bottom:.4rem">Sector</div>
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
        <td style="color:var(--muted)">${esc(r.industry)}</td>
        <td style="color:var(--muted)">${fmtMcap(r.market_cap)}</td>
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
    classify_realestate_categories(universe)
    classify_financials_categories(universe)
    classify_staples_categories(universe)
    classify_discretionary_categories(universe)
    classify_industrials_categories(universe)
    classify_utilities_categories(universe)
    classify_comms_categories(universe)

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
