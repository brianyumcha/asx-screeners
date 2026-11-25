"""Rebuilds reversal-rsi.html: a multi-timeframe (daily + weekly) RSI
divergence screener across the full ASX universe.

Background (2026-11-14): regular/classic RSI divergence - price makes a
new swing extreme that RSI doesn't confirm - is a textbook early-reversal
signal (Jamie Bell's mentioned picking up reversals from it). The rule is
mechanical, not a vibe, once you pin down four things we agreed on:

  1. What counts as a swing pivot - a closing-price fractal (3 bars
     confirmed on each side). Originally reused HH/LL SCREENER.py's
     body-based (max/min of open,close) definition for consistency, but
     switched to pure close (2026-11-15, found via AXE's chart): RSI is
     itself computed from closes, so comparing close-based pivots against
     it is the internally consistent, textbook approach for divergence -
     body-based made sense for HH/LL's breakout-TRIGGER use case (did a
     close cross back above a level), not here. Body-based let a single
     sharp reversal candle's OPEN satisfy "lower low" even though that
     bar's CLOSE didn't (AXE, 6 Nov: opened ~$0.16, closed +16% at
     $0.185 - that's not a low on any close-price chart).
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

2026-11-25: converted from a bespoke filterable table to the shared
card-grid renderer (dashboard_template.py's render_dashboard_html(), the
same one OBV/PULLBACK/MOMENTUM/DARVAS use) and renamed user-facing text
to "RSI Divergence" - the old "Reversal (RSI)" table read inconsistently
next to every other screener once there were several of them. A single
ticker can carry up to 4 independent divergences at once (bullish/bearish
x daily/weekly); each becomes its OWN card rather than cramming all four
into one, same "one qualifying result = one card" convention the other
screeners already use. Direction+Timeframe are also still shown as a
single colored "Setup" stat per card (green for bullish, red for
bearish) since that's the quickest-scanned form; the exact pivot
dates/values that used to fill two whole table columns are condensed into
the RSI Gap stat plus the swing dates in each card's footer_note-adjacent
detail; the mini chart is a plain price/RSI context view, not an
annotated pivot overlay (the shared chart-drawing code has no marker
primitive, and adding one risks every other screener that reuses it).

2026-11-25 (later same day): added real Direction/Timeframe toggle
filters and an actual RSI sub-panel on the chart, both requested after
the above tradeoffs were explained. Rather than hardcode either into the
shared renderer, both are generic, opt-in, data-driven additions:
render_dashboard_html()'s new `extra_facets` param renders a single-select
filter row per facet purely from what's passed in (used here for
direction/timeframe; every other screener passes nothing and is
unaffected), and drawChart() draws an RSI sub-panel only when a card sets
`rsi` (same gating pattern as the existing box/wave-point overlays).
"""
import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

import price_cache

RSI_PERIOD = 14
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MIN_PIVOT_SEPARATION = 5
MAX_PIVOT_AGE = 8
MIN_DAILY_HISTORY = 300  # enough for weekly resample + pivots on both timeframes
CHART_TRIM_BARS = 260    # trailing daily bars embedded per card for the mini chart
STREAK_GAP_DAYS = 5      # trading-day gap before a repeat sighting counts as a new streak

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILENAME = 'seen_tickers_reversal.json'
OUTPUT = "reversal-rsi.html"

DIRECTION_LABEL = {"bull": "Bullish", "bear": "Bearish"}
TIMEFRAME_LABEL = {"daily": "Daily", "weekly": "Weekly"}


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


def find_pivots(closes, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    """Every confirmed fractal pivot high/low in the CLOSING price series.
    Returns (pivot_high_idxs, pivot_low_idxs), both ascending."""
    n = len(closes)
    window = left + right + 1
    if n < window:
        return [], []
    highs, lows = [], []
    for i in range(left, n - right):
        if all(closes[i] >= closes[i + off] for off in range(-left, right + 1)):
            highs.append(i)
        if all(closes[i] <= closes[i + off] for off in range(-left, right + 1)):
            lows.append(i)
    return highs, lows


def detect_divergence(pivot_idxs, closes, rsi, direction):
    """direction: 'bullish' (pivot lows) or 'bearish' (pivot highs).
    Pivot value is the bar's CLOSE (see find_pivots()). Returns a dict
    describing the live divergence, or None."""
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

    v1, v2 = closes[p1], closes[p2]
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
    d_highs, d_lows = find_pivots(d_closes)
    d_rsi = rsi_series(d_closes)

    weekly = resample_weekly(frame)
    if len(weekly) < 20:
        weekly = None
    else:
        w_closes = weekly["close"].tolist()
        w_highs, w_lows = find_pivots(w_closes)
        w_rsi = rsi_series(w_closes)

    out = {
        "bull_daily": detect_divergence(d_lows, d_closes, d_rsi, "bullish"),
        "bear_daily": detect_divergence(d_highs, d_closes, d_rsi, "bearish"),
        "bull_weekly": None,
        "bear_weekly": None,
    }
    if weekly is not None:
        out["bull_weekly"] = detect_divergence(w_lows, w_closes, w_rsi, "bullish")
        out["bear_weekly"] = detect_divergence(w_highs, w_closes, w_rsi, "bearish")

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
    out["change_1d"] = round((d_closes[-1] - d_closes[-2]) / d_closes[-2] * 100, 2) if len(d_closes) > 1 and d_closes[-2] else 0.0

    n = len(d_closes)
    start = max(0, n - CHART_TRIM_BARS)
    out["dates"] = [str(d)[:10] for d in frame["date"].iloc[start:].tolist()]
    out["opens"] = [round(float(v), 4) for v in d_opens[start:]]
    out["highs"] = [round(float(v), 4) for v in frame["high"].iloc[start:].tolist()]
    out["lows"] = [round(float(v), 4) for v in frame["low"].iloc[start:].tolist()]
    out["closes"] = [round(float(v), 4) for v in d_closes[start:]]
    out["volumes"] = [int(v) for v in frame["volume"].iloc[start:].tolist()]
    out["rsi"] = [None if v is None else round(float(v), 2) for v in d_rsi[start:]]
    return out


# ─── STREAK HISTORY (first-seen tracking, not a hide/cooldown) ─────────────
# Same convention as every other screener here: persistence is part of the
# signal, so a ticker that keeps flagging isn't hidden, it just accumulates
# a longer "Since" stat. Keyed by ticker+direction+timeframe (not just
# ticker) since one ticker can carry up to 4 independent divergences and
# each should track its own streak - a fresh bearish-weekly signal on a
# ticker that's had a bullish-daily one for weeks is a new setup, not a
# continuation.

def _weekdays_between(d1, d2):
    if d2 < d1:
        d1, d2 = d2, d1
    days = 0
    cur = d1
    while cur < d2:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def load_seen_history():
    path = os.path.join(SCRIPT_DIR, HISTORY_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  ⚠ Could not read {HISTORY_FILENAME}, starting fresh: {e}")
        return {}


def save_seen_history(history):
    path = os.path.join(SCRIPT_DIR, HISTORY_FILENAME)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"  ⚠ Could not save {HISTORY_FILENAME}: {e}")


def first_seen_for(history, key, today, gap_days=STREAK_GAP_DAYS):
    today_str = today.strftime('%Y-%m-%d')
    entry = history.get(key)
    first_seen = today_str
    if entry:
        try:
            last_seen = datetime.strptime(entry['last_seen'], '%Y-%m-%d').date()
            if _weekdays_between(last_seen, today) <= gap_days:
                first_seen = entry['first_seen']
        except Exception:
            pass
    history[key] = {'first_seen': first_seen, 'last_seen': today_str}
    return first_seen


# ─── CARD CONSTRUCTION ──────────────────────────────────────────────────────
# Score formula: a larger RSI gap is a stronger divergence (the whole
# signal IS the size of that gap), and a more recently-confirmed 2nd pivot
# (lower age_bars) is more immediately actionable - both get rewarded.
# Calibrated against real gaps seen in this universe (roughly +5 to +15)
# so a typical result lands in the 55-75 range, not bunched at the ends.

def score_for(rsi_gap, age_bars):
    raw = 45 + rsi_gap * 2.2 + (MAX_PIVOT_AGE - age_bars) * 2.5
    return max(0, min(100, round(raw)))


def build_cards(results, history, today):
    cards = []
    for r in results:
        for slot, (direction, timeframe) in (
            ("bull_daily", ("bull", "daily")),
            ("bear_daily", ("bear", "daily")),
            ("bull_weekly", ("bull", "weekly")),
            ("bear_weekly", ("bear", "weekly")),
        ):
            d = r.get(slot)
            if d is None:
                continue
            key = f"{r['ticker']}_{direction}_{timeframe}"
            first_seen = first_seen_for(history, key, today)
            first_seen_date = datetime.strptime(first_seen, '%Y-%m-%d').date()
            days_flagging = _weekdays_between(first_seen_date, today)
            since_label = 'Today' if days_flagging == 0 else f'{days_flagging}d'
            first_seen_ts = int(datetime.combine(first_seen_date, datetime.min.time()).timestamp())

            cards.append({
                'ticker': r['ticker'],
                'name': r.get('name', r['ticker']),
                'sector': r.get('sector', 'Other'),
                'market_cap': r.get('market_cap', 0),
                'price': r['price'],
                'change_1d': r['change_1d'],
                'score': score_for(d['rsi_gap'], d['age_bars']),
                'first_seen_ts': first_seen_ts,
                'direction': direction,
                'timeframe': timeframe,
                'stats': [
                    {
                        'label': 'Setup',
                        'value': f"{DIRECTION_LABEL[direction]} · {TIMEFRAME_LABEL[timeframe]}",
                        'cls': 'stat-good' if direction == 'bull' else 'stat-bad',
                    },
                    {'label': 'RSI Gap', 'value': f"+{d['rsi_gap']:.1f}"},
                    {'label': 'Since', 'value': since_label},
                ],
                'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
                'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
                'rsi': r['rsi'],
            })
    return cards


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

    print(f"Done: {usable}/{len(tickers)} tickers had enough cached history. {len(results)} tickers flagged.")

    history = load_seen_history()
    today = date.today()
    cards = build_cards(results, history, today)
    save_seen_history(history)
    print(f"Built {len(cards)} cards (one per ticker+direction+timeframe divergence) from {len(results)} flagged tickers.")

    from dashboard_template import render_dashboard_html
    render_dashboard_html(
        cards=cards,
        total_scanned=len(tickers),
        title='↕️ RSI Divergence',
        subtitle="Price prints a new swing extreme that RSI doesn't confirm (daily &amp; weekly) · pivots &ge;5 bars apart · 2nd pivot within the last 8 bars · not yet invalidated by a close back past that level · no minimum RSI-gap filter.",
        footer_note=(
            'RSI Divergence · Data via Yahoo Finance (yfinance), ticker universe via SeaBee/Market Index<br>'
            'One card per independent divergence - a ticker can carry a bullish and a bearish signal at once, '
            'or a daily and a weekly one, each tracked and scored separately. '
            '"Setup" shows direction and timeframe; RSI Gap is the actual divergence size (not gated by a minimum).'
        ),
        out_path=OUTPUT,
        default_sort_key='score',
        default_mode='chart',
        extra_facets=[
            {
                'key': 'direction',
                'label': 'Direction',
                'options': [['bull', 'Bullish'], ['bear', 'Bearish']],
            },
            {
                'key': 'timeframe',
                'label': 'Timeframe',
                'all_label': 'Daily+Weekly',
                'options': [['daily', 'Daily'], ['weekly', 'Weekly']],
            },
        ],
    )
    print(f"Wrote {OUTPUT}.")


if __name__ == "__main__":
    main()
