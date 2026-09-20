"""
ASX Volume Anomaly Scanner
===========================
Flags ASX stocks trading at an unusual multiple of their own normal volume
today - deliberately with NO price-direction filter. Every other screener
on this site (OBV, Pullback, Momentum, Higher-High) requires a specific
price pattern first; this one exists to catch the thing that sometimes
shows up BEFORE any of those patterns are visible - a stock trading at
5x its normal volume while its price barely moves is exactly the kind of
quiet accumulation/distribution those price-pattern screeners can't see
yet, since there's no pattern to detect until the move actually happens.

Criteria (a stock must pass ALL of these):
  1. Market cap >= $20M (site-wide floor, see HH SCREENER.py)
  2. 20-day average daily volume >= 50,000 shares - a liquidity floor on
     the denominator itself, so a stock that normally trades 500 shares/
     day "spiking" to 5,000 doesn't count (same anti-noise reasoning as
     OBV/HH's own volume floors, just applied to the baseline instead of
     today's volume)
  3. Today's volume >= 100,000 shares - an absolute floor on top of the
     liquidity floor, so a stock a hair above the 50k baseline can't
     register as a "10x anomaly" on a still-thin absolute number
  4. Relative volume (today's volume / trailing 20-day average, EXCLUDING
     today) >= 3.0x - the actual anomaly threshold

Unlike Momentum's relative-volume figure (shown/sorted on but not gated,
because it couldn't be reconciled against TradingView's own screener
column - see that script's docstring), this IS gated on relative volume,
since gating is the entire point of a screener whose only job is finding
volume anomalies. The 3.0x threshold is a starting heuristic, not
verified against any external reference screener the way Craig's
Momentum spec was - there's no equivalent "known-good" TradingView
volume-anomaly list to calibrate against. Treat it as adjustable.

Each result is tagged with a plain-English direction so a quiet spike
(the most interesting case - volume without an obvious price reason yet)
is distinguishable from one riding an already-visible move:
  - "Spike Up"   : today's change > +2%
  - "Spike Down" : today's change < -2%
  - "Quiet Spike": everything in between - unusual volume, no real move

Nothing is ever hidden: every stock that qualifies shows up every run,
tagged with a "Since" stat (how many trading days it's been continuously
flagging). Tracked in seen_tickers_volume.json next to this script - a
gap of more than STREAK_GAP_DAYS trading days between appearances resets
the streak, since that's a new spike, not a continuation of the old one.

Usage:
    python "VOLUME ANOMALY SCREENER.py"                 # full ASX scan
    python "VOLUME ANOMALY SCREENER.py" --workers 20    # faster
    python "VOLUME ANOMALY SCREENER.py" --tickers BHP,CBA,RIO
"""

import argparse
import csv
import importlib.util
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import price_cache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_WORKERS = 15
HISTORY_PERIOD  = "1y"
CHART_TRIM_BARS = 260

MIN_AVG_VOLUME   = 50_000    # liquidity floor on the 20d baseline itself
MIN_TODAY_VOLUME = 100_000   # absolute floor on today's volume
RVOL_THRESHOLD   = 3.0       # today's volume must be >= this multiple of the 20d average
DAY_MOVE_BAND    = 2.0       # +/- this % separates "Spike Up/Down" from "Quiet Spike"

HISTORY_FILENAME = 'seen_tickers_volume.json'


def direction_label(change_1d):
    if change_1d > DAY_MOVE_BAND:
        return 'Spike Up'
    if change_1d < -DAY_MOVE_BAND:
        return 'Spike Down'
    return 'Quiet Spike'


def analyse_ticker(ticker, frame, market_cap, min_market_cap):
    if market_cap and market_cap < min_market_cap:
        return None
    min_needed = 25
    if frame is None or len(frame) < min_needed:
        return None

    closes  = frame['close'].tolist()
    opens   = frame['open'].tolist()
    highs   = frame['high'].tolist()
    lows    = frame['low'].tolist()
    volumes = frame['volume'].tolist()
    dates   = [d.strftime('%Y-%m-%d') for d in frame['date'].tolist()]

    price = closes[-1]
    vol_today = volumes[-1]

    baseline = volumes[-21:-1] if len(volumes) >= 21 else volumes[:-1]
    if not baseline:
        return None
    avg_vol20 = sum(baseline) / len(baseline)
    if avg_vol20 < MIN_AVG_VOLUME:
        return None
    if vol_today < MIN_TODAY_VOLUME:
        return None

    rel_vol = vol_today / avg_vol20 if avg_vol20 else 0.0
    if rel_vol < RVOL_THRESHOLD:
        return None

    prev_close = closes[-2]
    change_1d = (price - prev_close) / prev_close * 100 if prev_close else 0.0

    price_5d_ago = closes[-6] if len(closes) >= 6 else closes[0]
    change_5d = (price - price_5d_ago) / price_5d_ago * 100 if price_5d_ago else 0.0

    trim = slice(-CHART_TRIM_BARS, None)

    return {
        'ticker': ticker,
        'price': round(price, 4),
        'market_cap': int(market_cap) if market_cap else 0,
        'change_1d': round(change_1d, 2),
        'change_5d': round(change_5d, 2),
        'rel_vol': round(rel_vol, 2),
        'vol_today': int(vol_today),
        'avg_vol20': int(avg_vol20),
        'direction': direction_label(change_1d),
        'dates': dates[trim],
        'opens': [round(v, 4) for v in opens[trim]],
        'highs': [round(v, 4) for v in highs[trim]],
        'lows': [round(v, 4) for v in lows[trim]],
        'closes': [round(v, 4) for v in closes[trim]],
        'volumes': [int(v) for v in volumes[trim]],
    }


def run_scan(universe, tickers, workers=DEFAULT_WORKERS, min_market_cap=20_000_000):
    print(f"Refreshing shared price cache for {len(tickers)} tickers...")
    cache, fetched_ok, total = price_cache.refresh_cache(tickers, workers=workers, max_history=HISTORY_PERIOD)
    print(f"  Cache refresh: {fetched_ok}/{total} fresh this run")

    results = []
    t0 = time.time()
    for i, t in enumerate(tickers, 1):
        mcap = universe.get(t, {}).get('market_cap') or 0
        frame = price_cache.get_ticker_frame(cache, t, min_days=25)
        r = analyse_ticker(t, frame, mcap, min_market_cap)
        if r:
            r['sector'] = universe.get(t, {}).get('sector') or 'Other'
            results.append(r)
        if i % 300 == 0:
            print(f"  ...{i}/{len(tickers)} scanned ({time.time()-t0:.0f}s), {len(results)} passing so far")

    usable = price_cache.count_usable(cache, tickers, min_days=25)
    print(f"Scan complete: {len(results)} pass every filter.")
    return results, usable


def build_html_report(results, total_scanned, out_path):
    from dashboard_template import render_dashboard_html

    def to_card(r):
        first_seen_date = datetime.strptime(r['first_seen'], '%Y-%m-%d').date()
        days_flagging = _weekdays_between(first_seen_date, date.today())
        since_label = 'Today' if days_flagging == 0 else f'{days_flagging}d'
        first_seen_ts = int(datetime.combine(first_seen_date, datetime.min.time()).timestamp())
        return {
            'ticker': r['ticker'],
            'sector': r['sector'],
            'market_cap': r['market_cap'],
            'price': r['price'],
            'change_1d': r['change_1d'],
            'score': int(round(min(r['rel_vol'] / 10, 1.0) * 100)),
            'first_seen_ts': first_seen_ts,
            'stats': [
                {'label': 'Rel Vol', 'value': f"{r['rel_vol']:.1f}×"},
                {'label': '5d', 'value': f"{r['change_5d']:+.1f}%"},
                {'label': 'Signal', 'value': r['direction']},
                {'label': 'Since', 'value': since_label},
            ],
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        total_scanned=total_scanned,
        title='🌋 ASX Volume Anomaly Scanner',
        subtitle='ASX stocks trading well above their own normal volume today — no price-direction filter, so quiet accumulation shows up here before it shows up anywhere else on this site.',
        footer_note=(
            'ASX Volume Anomaly Scanner · Data via Yahoo Finance (yfinance), ticker universe via SeaBee<br>'
            f'Criteria: today\'s volume &ge; {RVOL_THRESHOLD:.1f}&times; the trailing 20-day average AND '
            f'20d average volume &ge; {MIN_AVG_VOLUME:,} AND today\'s volume &ge; {MIN_TODAY_VOLUME:,} AND '
            'market cap &ge; $20M. No price-change requirement, unlike every other screener on this site - '
            '"Signal" tags whether the spike came with a real move (&plusmn;2%) or not.'
        ),
        out_path=out_path + '.html',
    )
    print(f"  📄 HTML report → {out_path}.html")


def build_csv(results, out_path):
    array_fields = {'dates', 'opens', 'highs', 'lows', 'closes', 'volumes'}
    fieldnames = [k for k in results[0].keys() if k not in array_fields] if results else []
    with open(out_path + '.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow({k: v for k, v in r.items() if k not in array_fields})
    print(f"  📊 CSV export  → {out_path}.csv")


def build_tradingview_watchlist(results, out_path):
    tv_tickers = [f"ASX:{r['ticker']}" for r in results]
    with open(out_path + '_tradingview_watchlist.txt', 'w', encoding='utf-8') as f:
        f.write(','.join(tv_tickers))
    print(f"  📥 TradingView watchlist → {out_path}_tradingview_watchlist.txt")


# ─── STREAK HISTORY (first-seen tracking, not a hide/cooldown) ─────────────
# Matches every other screener on this site - a stock still spiking run
# after run is more interesting, not less. A gap of more than
# STREAK_GAP_DAYS trading days between appearances resets the streak.

STREAK_GAP_DAYS = 5


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


def apply_streaks(history, results, gap_days=STREAK_GAP_DAYS):
    """Tags each result with 'first_seen' (the date its current streak
    started) and updates `history` in place - no filtering, every result
    stays in the list. A ticker continues its streak if it last appeared
    within `gap_days` trading days; otherwise today counts as a fresh
    first sighting."""
    today = date.today()
    today_str = today.strftime('%Y-%m-%d')
    for r in results:
        ticker = r['ticker']
        entry = history.get(ticker)
        first_seen = today_str
        if entry:
            try:
                last_seen = datetime.strptime(entry['last_seen'], '%Y-%m-%d').date()
                if _weekdays_between(last_seen, today) <= gap_days:
                    first_seen = entry['first_seen']
            except Exception:
                pass
        r['first_seen'] = first_seen
        history[ticker] = {'first_seen': first_seen, 'last_seen': today_str}
    return history


# ─── MAIN ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ASX Volume Anomaly Scanner')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--out', type=str, default='asx_volume_anomaly_results')
    parser.add_argument('--tickers', type=str, default='')
    args = parser.parse_args()

    spec = importlib.util.spec_from_file_location("hh", "HH SCREENER.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)

    universe = hh.get_asx_universe()

    print("=" * 60)
    print("  ASX VOLUME ANOMALY SCANNER")
    print("=" * 60)
    print(f"  Criteria: rel vol >= {RVOL_THRESHOLD}x (20d) · today's vol >= {MIN_TODAY_VOLUME:,} · "
          f"20d avg vol >= {MIN_AVG_VOLUME:,} · market cap >= $20M")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
        print(f"  Using custom list: {len(tickers)} tickers")
    else:
        tickers = sorted(t for t in universe if len(t) == 3)
        print(f"  Full ASX universe: {len(tickers)} tickers")

    results, usable = run_scan(universe, tickers, workers=args.workers, min_market_cap=hh.MIN_MARKET_CAP)

    if not args.tickers and len(tickers) >= 500:
        usable_ratio = usable / len(tickers)
        if usable_ratio < 0.5:
            print(f"\n❌ Only {usable}/{len(tickers)} tickers ({usable_ratio:.0%}) have usable cached "
                  f"price data - aborting WITHOUT writing a report, so the last good one stays live.")
            sys.exit(1)

    results.sort(key=lambda r: r['rel_vol'], reverse=True)

    history = load_seen_history()
    history = apply_streaks(history, results)
    save_seen_history(history)

    if results:
        print(f"\n{'─'*60}")
        print(f"  TOP SIGNALS\n")
        print(f"  {'TICKER':<8} {'RELVOL':>7} {'1D%':>6}  SIGNAL")
        for r in results[:20]:
            print(f"  {r['ticker']:<8} {r['rel_vol']:>6.1f}× {r['change_1d']:>5.1f}%  {r['direction']}")
        if len(results) > 20:
            print(f"  ... and {len(results)-20} more in the report")

    out_base = os.path.join(SCRIPT_DIR, 'asx_volume_anomaly_results')
    build_html_report(results, len(tickers), out_base)
    build_csv(results, out_base)
    build_tradingview_watchlist(results, out_base)


if __name__ == '__main__':
    main()
