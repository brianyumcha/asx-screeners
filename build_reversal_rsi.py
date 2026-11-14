"""Rebuilds reversal-rsi.html: a multi-timeframe (daily + weekly) RSI
divergence screener across the full ASX universe.

Background (2026-11-14): regular/classic RSI divergence - price makes a
new swing extreme that RSI doesn't confirm - is a textbook early-reversal
signal (Jamie Bell's mentioned picking up reversals from it). The rule is
mechanical, not a vibe, once you pin down four things we agreed on:

  1. What counts as a swing pivot - reuses the exact same body-based
     fractal definition HH SCREENER.py / LL SCREENER.py already use
     (max/min of open,close, not wicks; 3 bars confirmed on each side),
     generalized here to return every confirmed pivot in the series
     instead of just the latest breakout trigger (see find_pivots()).
  2. Minimum separation between the two compared pivots - 5 bars, so it's
     not comparing near-adjacent noise.
  3. Liveness - the divergence stays "live" as long as price hasn't since
     closed past the 2nd pivot's own level in the invalidating direction
     (which would form a new, superseding pivot), AND the 2nd pivot
     formed within the last 8 bars (an outer backstop so a technically-
     still-valid-but-ancient divergence doesn't hang around forever).
  4. No minimum RSI-gap filter - even a weak/near-double-bottom-ish
     divergence still qualifies, but the actual gap is computed and shown
     so a weak one is visibly weak rather than filtered out or hidden.

Weekly bars are resampled from the SAME daily price_cache data (W-FRI,
OHLC/volume aggregated per calendar week) rather than a second yfinance
pull - the shared cache (kept warm by HH/LL/OBV/PULLBACK's own daily
runs) already has what's needed, so this adds zero extra API load.

First run is a manual/ad-hoc validation pass against whatever's already
in the shared cache (no refresh_cache() call) - see if the rule actually
produces good signals before deciding on a real schedule.
"""
import importlib.util
import json
import sys

import pandas as pd

import price_cache

RSI_PERIOD = 14
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MIN_PIVOT_SEPARATION = 5
MAX_PIVOT_AGE = 8
MIN_DAILY_HISTORY = 300  # enough for weekly resample + pivots on both timeframes

TEMPLATE = "reversal_rsi_template.html"
OUTPUT = "reversal-rsi.html"


def load_universe():
    spec = importlib.util.spec_from_file_location("hh", "HH SCREENER.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)
    return hh.get_asx_universe()


def rsi_series(closes, period=RSI_PERIOD):
    """Wilder's RSI, full series (None for bars before it's defined) - same
    smoothing as MOMENTUM/OBV SCREENER.py's calc_rsi(), just keeping every
    step instead of only the final value."""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def find_pivots(opens, closes, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    """Every confirmed fractal pivot high/low in the series, body-based
    (max/min of open,close) to match HH/LL SCREENER.py exactly. Returns
    (pivot_high_idxs, pivot_low_idxs), both ascending."""
    n = len(closes)
    window = left + right + 1
    if n < window:
        return [], []
    tops = [max(opens[i], closes[i]) for i in range(n)]
    bots = [min(opens[i], closes[i]) for i in range(n)]
    highs, lows = [], []
    for i in range(left, n - right):
        if all(tops[i] >= tops[i + off] for off in range(-left, right + 1)):
            highs.append(i)
        if all(bots[i] <= bots[i + off] for off in range(-left, right + 1)):
            lows.append(i)
    return highs, lows, tops, bots


def detect_divergence(pivot_idxs, pivot_vals, closes, rsi, direction):
    """direction: 'bullish' (pivot lows) or 'bearish' (pivot highs).
    Returns a dict describing the live divergence, or None."""
    if len(pivot_idxs) < 2:
        return None
    n = len(closes)
    p2 = pivot_idxs[-1]
    if (n - 1 - p2) > MAX_PIVOT_AGE:
        return None

    p1 = None
    for idx in reversed(pivot_idxs[:-1]):
        if p2 - idx >= MIN_PIVOT_SEPARATION:
            p1 = idx
            break
    if p1 is None:
        return None

    if rsi[p1] is None or rsi[p2] is None:
        return None

    v1, v2 = pivot_vals[p1], pivot_vals[p2]
    r1, r2 = rsi[p1], rsi[p2]

    if direction == "bullish":
        if not (v2 < v1 and r2 > r1):
            return None
        # invalidated if price has since closed below pivot2's own low -
        # that would be a new, lower low superseding this pair.
        if any(c < v2 for c in closes[p2 + 1:]):
            return None
        rsi_gap = r2 - r1
    else:
        if not (v2 > v1 and r2 < r1):
            return None
        if any(c > v2 for c in closes[p2 + 1:]):
            return None
        rsi_gap = r1 - r2

    return {
        "p1_idx": p1, "p2_idx": p2,
        "p1_val": round(v1, 4), "p2_val": round(v2, 4),
        "p1_rsi": round(r1, 1), "p2_rsi": round(r2, 1),
        "rsi_gap": round(rsi_gap, 1),
        "age_bars": n - 1 - p2,
    }


def resample_weekly(frame):
    df = frame.set_index(pd.to_datetime(frame["date"]))
    weekly = df.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return weekly.reset_index()


def analyse_ticker(ticker, frame):
    if frame is None or len(frame) < MIN_DAILY_HISTORY:
        return None

    d_opens = frame["open"].tolist()
    d_closes = frame["close"].tolist()
    d_highs, d_lows, d_tops, d_bots = find_pivots(d_opens, d_closes)
    d_rsi = rsi_series(d_closes)

    weekly = resample_weekly(frame)
    if len(weekly) < 20:
        weekly = None
    else:
        w_opens = weekly["open"].tolist()
        w_closes = weekly["close"].tolist()
        w_highs, w_lows, w_tops, w_bots = find_pivots(w_opens, w_closes)
        w_rsi = rsi_series(w_closes)

    out = {
        "bull_daily": detect_divergence(d_lows, d_bots, d_closes, d_rsi, "bullish"),
        "bear_daily": detect_divergence(d_highs, d_tops, d_closes, d_rsi, "bearish"),
        "bull_weekly": None,
        "bear_weekly": None,
    }
    if weekly is not None:
        out["bull_weekly"] = detect_divergence(w_lows, w_bots, w_closes, w_rsi, "bullish")
        out["bear_weekly"] = detect_divergence(w_highs, w_tops, w_closes, w_rsi, "bearish")

        def _dateify(d, frame_dates):
            if d is None:
                return None
            d = dict(d)
            d["p1_date"] = str(frame_dates[d["p1_idx"]])[:10]
            d["p2_date"] = str(frame_dates[d["p2_idx"]])[:10]
            return d

        out["bull_weekly"] = _dateify(out["bull_weekly"], weekly["date"].tolist())
        out["bear_weekly"] = _dateify(out["bear_weekly"], weekly["date"].tolist())

    def _dateify_daily(d):
        if d is None:
            return None
        d = dict(d)
        d["p1_date"] = str(frame["date"].iloc[d["p1_idx"]])[:10]
        d["p2_date"] = str(frame["date"].iloc[d["p2_idx"]])[:10]
        return d

    out["bull_daily"] = _dateify_daily(out["bull_daily"])
    out["bear_daily"] = _dateify_daily(out["bear_daily"])

    if not any(out.values()):
        return None

    out["ticker"] = ticker
    out["price"] = round(float(d_closes[-1]), 4)
    return out


def main():
    tickers_arg = None
    for a in sys.argv[1:]:
        if a.startswith("--tickers="):
            tickers_arg = [t.strip().upper() for t in a.split("=", 1)[1].split(",") if t.strip()]

    print("Loading ASX universe...")
    universe = load_universe()
    tickers = tickers_arg or sorted(t for t in universe if len(t) == 3)
    print(f"Scanning {len(tickers)} tickers for daily+weekly RSI divergence...")

    cache = price_cache.load_cache()
    print(f"Price cache loaded: {len(cache)} rows, {cache['ticker'].nunique() if not cache.empty else 0} tickers.")

    results = []
    usable = 0
    for i, t in enumerate(tickers, 1):
        frame = price_cache.get_ticker_frame(cache, t, min_days=MIN_DAILY_HISTORY)
        if frame is not None:
            usable += 1
        r = analyse_ticker(t, frame)
        if r:
            info = universe.get(t, {})
            r["name"] = info.get("name", t)
            r["sector"] = info.get("sector", "Other")
            r["market_cap"] = info.get("market_cap", 0)
            results.append(r)
        if i % 300 == 0:
            print(f"  ...{i}/{len(tickers)} scanned, {usable} usable, {len(results)} hits so far")

    print(f"Done: {usable}/{len(tickers)} tickers had enough cached history. {len(results)} flagged.")

    data = {r["ticker"]: r for r in sorted(results, key=lambda r: r["ticker"])}
    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<NAV_HTML>>>" in template
    assert "<<<DATA_JSON>>>" in template

    from dashboard_template import render_nav, size_bucket
    for r in data.values():
        r["size"] = size_bucket(r.get("market_cap", 0))
    html = template.replace("<<<NAV_HTML>>>", render_nav(OUTPUT))
    html = html.replace("<<<DATA_JSON>>>", json.dumps(data, separators=(",", ":")))

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes, {len(results)} tickers flagged).")


if __name__ == "__main__":
    main()
