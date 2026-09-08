"""
Shared relative-strength (RS) utilities used by both HH SCREENER.py and
OBV SCREENER.py - the GICS sector map, the 11 sector benchmark indices,
and the RS-line calculations built on top of them. Pulled out into its
own module so both screeners route to the same benchmark for the same
sector instead of maintaining two copies that could drift apart.

Requires price_cache.py (for the raw index fetch) alongside it.
"""
import json
import os

import pandas as pd

import price_cache

# A same-run, on-disk relay for the benchmark fetch - see get_benchmark_series's
# docstring for why this exists instead of just calling fetch_benchmark_series
# directly from each screener.
BENCHMARK_CACHE_FILENAME = "benchmark_cache.json"

# SeaBee's `industry` field (GICS industry-group level) mapped onto the 11
# standard GICS sectors (matching e.g. listcorp.com/asx/sectors) - see
# HH SCREENER.py's original comment for why this replaced an earlier
# 8-bucket scheme (that only existed to fit 8 TradingView Pine panes).
SECTOR_MAP = {
    "Materials": "Materials",
    "Energy": "Energy",
    "Capital Goods": "Industrials",
    "Commercial & Professional Services": "Industrials",
    "Transportation": "Industrials",
    "Software & Services": "Info Tech",
    "Technology Hardware & Equipment": "Info Tech",
    "Semiconductors & Semiconductor Equipment": "Info Tech",
    "Financial Services": "Financials",
    "Banks": "Financials",
    "Insurance": "Financials",
    "Pharmaceuticals, Biotechnology & Life Sciences": "Healthcare",
    "Health Care Equipment & Services": "Healthcare",
    "Consumer Services": "Consumer Discretionary",
    "Consumer Discretionary Distribution & Retail": "Consumer Discretionary",
    "Consumer Durables & Apparel": "Consumer Discretionary",
    "Automobiles & Components": "Consumer Discretionary",
    "Food, Beverage & Tobacco": "Consumer Staples",
    "Household & Personal Products": "Consumer Staples",
    "Consumer Staples Distribution & Retail": "Consumer Staples",
    "Media & Entertainment": "Communication Services",
    "Telecommunication Services": "Communication Services",
    "Utilities": "Utilities",
    "Equity Real Estate Investment Trusts (REITs)": "Real Estate",
    "Real Estate Management & Development": "Real Estate",
}
SECTOR_ORDER = [
    "Materials", "Energy", "Industrials", "Financials", "Healthcare", "Info Tech",
    "Consumer Discretionary", "Consumer Staples", "Communication Services",
    "Utilities", "Real Estate", "Other",
]

# STW.AX (SPDR S&P/ASX 200 ETF), not the raw "^AXJO" index symbol.
# GitHub Actions runs got 0/12 benchmark fetches on every run - initially
# looked like Yahoo blocking the "^"-prefixed index endpoint specifically
# (regular .AX equity tickers succeeded ~90% of the time in the same run),
# but narrowing the fetch down to just this one liquid .AX equity ticker
# STILL failed 100% of the time with "Too Many Requests. Rate limited."
# (found 2026-09-08 after adding real error logging - see price_cache.py's
# _fetch_one verbose param). The real cause: the benchmark fetch used to
# run AFTER the ~2000-ticker bulk price_cache refresh, by which point the
# runner IP's rate-limit budget for the run was already exhausted - not a
# symbol-type block at all. Both screeners now fetch this BEFORE their
# bulk refresh instead (see HH SCREENER.py's run_scan and
# OBV SCREENER.py's run_scan, both have a comment at that fetch call).
# STW is a large, liquid, physically-replicated ETF that tracks the ASX
# 200 closely, so the swap away from the raw index symbol itself doesn't
# cost meaningful tracking accuracy even though it turned out not to be
# the actual fix. Still labelled "XJO" in the UI (see BENCHMARK_LABELS)
# since that's the index it's standing in for.
BENCHMARK_MARKET = "STW.AX"
RS_EMA_PERIOD = 21            # matches the Traderlion RS Line indicator's default signal EMA

# Sector benchmarks are still the raw "^AX*J" index symbols. Even with the
# fetch-before-bulk-refresh ordering fix, fetching all 11 of these adds 11x
# the rate-limit exposure right at the point in the run when the budget is
# freshest - and ASX doesn't have a clean, verified single-GICS-sector ETF
# for all 11 sectors to substitute the way the market-wide benchmark was
# (checked: SPDR's entire ASX range is 17 broad-market funds, not a full
# sector family like the US Select Sector SPDRs; only a few sectors -
# Financials, Real Estate, Info Tech - have a solid ASX-domestic single-
# sector ETF match). Sector RS has been dropped from both screeners
# entirely rather than ship something that would still be unreliable.
SECTOR_BENCHMARK = {
    "Materials": "^AXMJ",
    "Energy": "^AXEJ",
    "Industrials": "^AXNJ",
    "Financials": "^AXFJ",
    "Healthcare": "^AXHJ",
    "Info Tech": "^AXIJ",
    "Consumer Discretionary": "^AXDJ",
    "Consumer Staples": "^AXSJ",
    "Communication Services": "^AXTJ",
    "Utilities": "^AXUJ",
    "Real Estate": "^AXPJ",
}
BENCHMARK_LABELS = {
    "STW.AX": "XJO", "^AXMJ": "XMJ", "^AXEJ": "XEJ", "^AXFJ": "XFJ",
    "^AXHJ": "XHJ", "^AXIJ": "XIJ", "^AXDJ": "XDJ", "^AXSJ": "XSJ",
    "^AXPJ": "XPJ", "^AXNJ": "XNJ", "^AXTJ": "XTJ", "^AXUJ": "XUJ",
}


def fetch_benchmark_series(history_period, symbols=None):
    """Fetches daily closes for the given benchmark symbols (default: the
    market-wide benchmark plus every GICS sector index) - fetched directly
    (not through the shared per-ticker price cache, whose ".AX"-suffix
    convention doesn't apply to "^"-prefixed index symbols) once per run,
    reusing price_cache's own retry logic. Returns {yahoo_symbol: pd.Series
    of close, indexed by tz-naive date}.

    Callers that only need the market-wide benchmark (both screeners
    currently do - sector RS is dropped, see SECTOR_BENCHMARK's comment)
    should pass symbols={BENCHMARK_MARKET} rather than the default, since
    the 11 sector index symbols are guaranteed to fail on GitHub Actions
    and fetching them anyway would just be wasted retries/time for data
    nothing uses."""
    if symbols is None:
        symbols = {BENCHMARK_MARKET} | set(SECTOR_BENCHMARK.values())
    series = {}
    for sym in symbols:
        df = price_cache._fetch_one(sym, history_period, verbose=True)
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            continue
        s = df["Close"]
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        series[sym] = s
    return series


def save_benchmark_cache(dir_path, history_period, symbols=None):
    """Fetches once and writes to a small JSON file in `dir_path` - the
    GitHub Actions workflow runs this as its own step, BEFORE any of the
    three screener scripts. Reason: the "fetch benchmark before the bulk
    refresh" ordering inside each screener's own run_scan() only protects
    that ONE screener's fetch - on a full run, OBV and Pullback each burn
    through their own ~2000-ticker bulk price fetch before HH even starts,
    so by the time HH's turn comes (a separate step, same runner, same
    rate-limit budget for the whole job) the budget is already exhausted
    regardless of HH's own internal ordering (found 2026-09-08). Fetching
    once, right at the start of the job before ANY bulk fetching has
    happened, and relaying it to every later step via this file, is the
    only ordering that actually works for every screener in a full run."""
    series = fetch_benchmark_series(history_period, symbols=symbols)
    payload = {sym: {d.strftime("%Y-%m-%d"): float(v) for d, v in s.items()} for sym, s in series.items()}
    with open(os.path.join(dir_path, BENCHMARK_CACHE_FILENAME), "w") as f:
        json.dump(payload, f)
    return series


def load_benchmark_cache(dir_path):
    try:
        with open(os.path.join(dir_path, BENCHMARK_CACHE_FILENAME)) as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    series = {}
    for sym, points in payload.items():
        if not points:
            continue
        s = pd.Series({pd.Timestamp(d): v for d, v in points.items()}).sort_index()
        series[sym] = s
    return series


def get_benchmark_series(dir_path, history_period, symbols=None):
    """What both screeners should actually call: use the on-disk relay
    from save_benchmark_cache if the workflow already populated it this
    run, otherwise fall back to a live fetch (local dev runs, or the
    prefetch step itself failing) - see save_benchmark_cache's docstring
    for why the relay exists at all."""
    cached = load_benchmark_cache(dir_path)
    if cached:
        return cached
    return fetch_benchmark_series(history_period, symbols=symbols)


def _align(dates, closes, index_series, min_len):
    if index_series is None or len(index_series) < min_len:
        return None
    ticker_s = pd.Series(closes, index=pd.to_datetime(dates).tz_localize(None).normalize())
    aligned = pd.concat([ticker_s, index_series], axis=1, join="inner").dropna()
    if len(aligned) < min_len:
        return None
    return aligned


def resample_weekly_close(dates, closes):
    """Friday-anchored weekly close series from daily dates/closes - a
    close-only counterpart to HH SCREENER.py's own full-OHLCV
    resample_weekly (used there for pivot/HH-signal detection, which needs
    open/high/low too). This one's for RS, which only ever needs closes -
    used both for a ticker's own weekly closes and for resampling the
    daily benchmark index series to weekly, so both sides of the RS ratio
    use the same W-FRI week boundaries."""
    s = pd.Series(closes, index=pd.to_datetime(dates))
    return s.resample("W-FRI").last().dropna()


def relative_strength_status(dates, closes, index_series):
    """True if the RS line (ticker close / index close) is currently above
    its own RS_EMA_PERIOD-bar EMA - the Traderlion RS Line indicator's
    "showing relative strength" (blue) signal - False if below, None if
    there isn't enough overlapping data to tell."""
    aligned = _align(dates, closes, index_series, RS_EMA_PERIOD + 5)
    if aligned is None:
        return None
    ratio = aligned.iloc[:, 0] / aligned.iloc[:, 1]
    ema = ratio.ewm(span=RS_EMA_PERIOD, adjust=False).mean()
    return bool(ratio.iloc[-1] > ema.iloc[-1])


def rs_new_high_signal(dates, closes, index_series, lookback=63):
    """The "RS line making a new high while price hasn't" setup (the
    Traderlion RS dot): the stock is quietly outperforming the benchmark
    before it breaks out on price alone. `lookback` bars applies to BOTH
    halves of the check - a shorter window makes the RS-new-high trigger
    fire more often (any N-bar high resets quickly) but also makes "price
    hasn't broken out" easier to satisfy by accident; a longer window makes
    the RS trigger rarer/more significant but also makes "hasn't broken
    out" a stricter, more meaningful condition. Returns True/False, or None
    if there isn't enough overlapping history to tell."""
    aligned = _align(dates, closes, index_series, lookback + 1)
    if aligned is None:
        return None
    price = aligned.iloc[:, 0]
    rs = price / aligned.iloc[:, 1]
    rs_new_high = rs.iloc[-1] >= rs.iloc[-lookback:].max()
    price_new_high = price.iloc[-1] >= price.iloc[-lookback:].max()
    return bool(rs_new_high and not price_new_high)


def fetch_sector_map():
    """Lightweight SeaBee fetch returning just {ticker: sector} (one of the
    11 SECTOR_ORDER buckets, or "Other") - for scripts (like
    OBV SCREENER.py) that need sector routing for RS comparisons but not
    the fuller {industry, market_cap, name} dict HH SCREENER.py's own
    get_asx_universe() builds. One API call for the whole exchange, same
    as that function - never a per-ticker Yahoo .info lookup, which is
    what makes fetching every ticker's sector for a full-universe RS scan
    affordable at all (per-ticker .info is the "much slower" cost this
    script's own analyse_ticker() already avoids for its OBV criteria)."""
    import requests
    try:
        api_url = "https://marketdata.seabee.me/api.php?action=asx_companies_list"
        headers = {"X-API-Key": "deeznuts"}
        r = requests.get(api_url, headers=headers, timeout=20)
        if r.status_code == 200:
            json_resp = r.json()
            if json_resp.get("success") and "data" in json_resp and "companies" in json_resp["data"]:
                companies = json_resp["data"]["companies"]
                out = {}
                for t, info in companies.items():
                    t = str(t).strip().upper()
                    if not (1 <= len(t) <= 5) or not t.isalnum():
                        continue
                    industry = info.get("industry") or "Other"
                    out[t] = SECTOR_MAP.get(industry, "Other")
                if len(out) > 500:
                    return out
    except Exception:
        pass
    return {}
