"""
Shared incremental price-history cache used by all three ASX screeners
(OBV SCREENER.py, PULLBACK SCREENER.py, HH SCREENER.py).

Why this exists (2026-09-01): each screener used to independently fetch
full multi-year daily history for the whole ~2000-ticker ASX universe on
EVERY run. With HH now running 5x/day, that's a huge, repeated request
volume from the same source IP - and on GitHub Actions specifically, Yahoo
Finance was silently throttling almost all of it (confirmed: a run got real
data for only 27/2037 tickers while reporting it as a normal result).

Fix: persist a single shared price-history cache (this file's
CACHE_PATH, committed back to the repo by the workflow, same pattern as
seen_tickers.json) across runs. A ticker already in the cache only needs
its trailing few days re-fetched and merged in - not its whole history -
so after the cache is warm, a run's total data pulled drops by roughly
two orders of magnitude. And since all three screeners share ONE cache,
only the first screener to run in a given job does real fetching; the
other two just read what it already refreshed.

Stores RAW (auto_adjust=False) prices only - not both raw and adjusted -
so there's one unambiguous cache format all three screeners read from.
This also matches TradingView's own default (unadjusted) chart data, and
avoids the class of dividend-adjustment phantom-breakout bug found and
fixed in HH SCREENER.py on 2026-09-01 (see that script's docstring) -
OBV/PULLBACK switched from auto_adjust=True to match, for the same reason.
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(SCRIPT_DIR, "price_cache.parquet")

MAX_HISTORY_DAYS = 3 * 365 + 30   # covers HH's 3y need; OBV/Pullback just slice a shorter tail
REFRESH_TAIL_DAYS = 10            # re-fetched/merged for tickers already cached (covers weekends/holidays + late-arriving revisions)
DEPTH_SLACK_DAYS = 30             # calendar-day slack for weekends/holidays when checking cached depth is "enough"
FETCH_RETRIES = 2
FETCH_RETRY_SLEEP = 1.5


def _period_to_days(period):
    """yfinance period strings, e.g. '3y', '1y', '10d', '6mo'."""
    m = re.match(r"^(\d+)(d|mo|y)$", period)
    if not m:
        return 365
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 1, "mo": 31, "y": 366}[unit]

CACHE_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume"]


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            df = pd.read_parquet(CACHE_PATH)
            if set(CACHE_COLUMNS).issubset(df.columns):
                df["date"] = pd.to_datetime(df["date"])
                return df
        except Exception:
            pass
    return pd.DataFrame(columns=CACHE_COLUMNS)


def save_cache(df):
    df.to_parquet(CACHE_PATH, index=False)


def _fetch_one(yahoo_sym, period):
    df = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            df = yf.Ticker(yahoo_sym).history(period=period, interval="1d", auto_adjust=False)
        except Exception:
            df = None
        if df is not None and not df.empty:
            return df
        if attempt < FETCH_RETRIES:
            time.sleep(FETCH_RETRY_SLEEP * (attempt + 1))
    return df


def refresh_cache(tickers, workers=15, max_history="3y"):
    """
    Ensures the on-disk cache has reasonably fresh, sufficiently-deep data
    for every ticker in `tickers`. A ticker not yet cached, OR cached but
    not going back far enough for `max_history` (e.g. OBV/Pullback only
    need 1y and might seed a ticker at that depth before HH needs 3y for
    the same ticker), gets a full `max_history` fetch; a ticker already
    cached deep enough only gets its trailing REFRESH_TAIL_DAYS re-fetched
    and merged in. Saves the updated cache back to disk before returning.

    Returns (cache_df, fetched_ok, total) - fetched_ok is how many tickers
    actually got real data back this call, for the caller's own circuit
    breaker (a healthy run should get real data for nearly all of them,
    since most only need a ~10-day tail fetch, not a full history pull).
    """
    cache = load_cache()
    needed_days = _period_to_days(max_history)
    depth_cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=needed_days - DEPTH_SLACK_DAYS)
    earliest_by_ticker = cache.groupby("ticker")["date"].min() if not cache.empty else pd.Series(dtype="datetime64[ns]")
    latest_by_ticker = cache.groupby("ticker")["date"].max() if not cache.empty else pd.Series(dtype="datetime64[ns]")
    cache_max_date = cache["date"].max() if not cache.empty else None

    def needs_full_fetch(t):
        if t not in earliest_by_ticker.index:
            return True
        return earliest_by_ticker[t] > depth_cutoff

    def already_fresh(t):
        # All three screeners share this one cache and each calls
        # refresh_cache() independently at the start of their own run - if
        # OBV already refreshed a ticker to today's session a minute ago,
        # Pullback and HH re-requesting it too just triples the volume
        # sent to Yahoo within the same job for zero benefit (and likely
        # makes the throttling worse, not better). Skip anything that's
        # already current AND already deep enough for this caller's needs.
        if cache_max_date is None or needs_full_fetch(t) or t not in latest_by_ticker.index:
            return False
        return latest_by_ticker[t] >= cache_max_date

    to_fetch = [t for t in tickers if not already_fresh(t)]

    fetched_ok = 0
    new_frames = []

    def work(t):
        yahoo_sym = t if t.endswith(".AX") else t + ".AX"
        period = max_history if needs_full_fetch(t) else f"{REFRESH_TAIL_DAYS}d"
        return t, _fetch_one(yahoo_sym, period)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, t): t for t in to_fetch}
        for fut in as_completed(futures):
            t, df = fut.result()
            if df is None or df.empty:
                continue
            fetched_ok += 1
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                continue
            frame = pd.DataFrame({
                "ticker": t,
                "date": pd.to_datetime(df.index).tz_localize(None),
                "open": df["Open"].values,
                "high": df["High"].values,
                "low": df["Low"].values,
                "close": df["Close"].values,
                "volume": df["Volume"].fillna(0).values,
            })
            new_frames.append(frame)

    if new_frames:
        incoming = pd.concat(new_frames, ignore_index=True)
        # concat with incoming LAST + keep='last' on the dedupe below means a
        # freshly-fetched row always wins over a stale cached one for the
        # same ticker+date (handles corrections without a separate lookup)
        cache = pd.concat([cache, incoming], ignore_index=True)
        cache = cache.sort_values(["ticker", "date"]).drop_duplicates(["ticker", "date"], keep="last")

    if not cache.empty:
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=MAX_HISTORY_DAYS)
        cache = cache[cache["date"] >= cutoff]

    save_cache(cache)
    return cache, fetched_ok, len(tickers)


def get_ticker_frame(cache, ticker, min_days=40):
    """Returns that ticker's cached rows, sorted oldest-first, or None if
    there isn't enough history yet."""
    sub = cache[cache["ticker"] == ticker]
    if len(sub) < min_days:
        return None
    return sub.sort_values("date").reset_index(drop=True)


def count_usable(cache, tickers, min_days=40):
    """How many of `tickers` have enough cached data to actually be used,
    regardless of whether THIS run's refresh_cache() call freshened them or
    they're relying on an earlier run's still-recent fetch. This - not
    refresh_cache()'s own fetched_ok - is what a caller's circuit breaker
    should check: a run that mostly reused good cached data because today's
    fresh fetches got throttled is still a healthy, publishable run; only a
    run where the cache itself doesn't cover the universe is broken."""
    if cache.empty:
        return 0
    counts = cache[cache["ticker"].isin(tickers)].groupby("ticker").size()
    return int((counts >= min_days).sum())
