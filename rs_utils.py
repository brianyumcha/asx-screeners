"""
Shared relative-strength (RS) utilities used by both HH SCREENER.py and
OBV SCREENER.py - the GICS sector map, the 11 sector benchmark indices,
and the RS-line calculations built on top of them. Pulled out into its
own module so both screeners route to the same benchmark for the same
sector instead of maintaining two copies that could drift apart.

Requires price_cache.py (for the raw index fetch) alongside it.
"""
import pandas as pd

import price_cache

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

# STW.AX (SPDR S&P/ASX 200 ETF), not the raw "^AXJO" index symbol - GitHub
# Actions runners get 0/12 benchmark fetches on every run (confirmed via
# workflow logs: regular .AX equity tickers succeed ~90% of the time from
# the SAME runner IP, same run, while every single "^"-prefixed index
# symbol fails 100% of the time - Yahoo blocks that endpoint specifically,
# not the runner generally). STW is a large, liquid, physically-replicated
# ETF that tracks the ASX 200 closely - going through the ordinary equity
# endpoint instead of the blocked index endpoint fixes the fetch without
# giving up meaningful tracking accuracy. Still labelled "XJO" in the UI
# (see BENCHMARK_LABELS) since that's the index it's standing in for.
BENCHMARK_MARKET = "STW.AX"
RS_EMA_PERIOD = 21            # matches the Traderlion RS Line indicator's default signal EMA

# Sector benchmarks are still the raw "^AX*J" index symbols and so are
# still subject to the same 0/12 GitHub Actions failure described above -
# unlike the market-wide benchmark, ASX doesn't have a clean, verified
# single-GICS-sector ETF for all 11 sectors to substitute (checked: SPDR's
# entire ASX range is 17 broad-market funds, not a full sector family like
# the US Select Sector SPDRs; only a few sectors - Financials, Real
# Estate, Info Tech - have a solid ASX-domestic single-sector ETF match).
# Sector RS (rs_sector) will keep coming back None on GitHub Actions until
# this gets a real fix; rs_market (the XJO comparison above) works.
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
        df = price_cache._fetch_one(sym, history_period)
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            continue
        s = df["Close"]
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        series[sym] = s
    return series


def _align(dates, closes, index_series, min_len):
    if index_series is None or len(index_series) < min_len:
        return None
    ticker_s = pd.Series(closes, index=pd.to_datetime(dates).tz_localize(None).normalize())
    aligned = pd.concat([ticker_s, index_series], axis=1, join="inner").dropna()
    if len(aligned) < min_len:
        return None
    return aligned


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
