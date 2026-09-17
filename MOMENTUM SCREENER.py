"""
ASX Momentum Screener
======================
Built from Craig Tapping's TradingView Momentum Screener spec. Flags ASX
stocks already in a confirmed uptrend with a genuine volume pickup behind
them - the opposite intent of Pre-Breakout (OBV), which looks for
strength building BEFORE a move.

Criteria (a stock must pass ALL of these):
  1. 30-day average (mean) daily volume >= 500,000 shares - literally
     Craig's spec, confirmed against real TradingView hits 2026-09-17:
     an earlier median-based version (matching HH/OBV's anti-spike
     convention) excluded CDA and DDR, whose 30d MEDIAN volume sits just
     under 500k but whose 30d MEAN clears it comfortably
  2. Price above the 20-day SMA, which is itself above the 50-day SMA
     ("stacked" moving averages)
  3. RSI (14-period) between 50 and 65 - healthy momentum, not yet
     overbought
  4. 1-week change between +2% and +10%
  5. 1-month change > 0%
  6. Market cap >= $20M (site-wide floor, see HH SCREENER.py)

Three filters from Craig's original spec were NOT built as hard gates:

- Price > $2. Built as a UI toggle ("Price >= $2 only", default off) in
  dashboard_template.py instead of a scan-time filter, per the site
  owner 2026-09-17: several real TradingView "micro cap" hits (ENR, AL3,
  IVZ, TAM, NH3, EWC, PAR, TER, WZR) trade well under $2, so TV's own
  micro-cap tier apparently doesn't enforce this floor even though
  Craig's spec text states it - gating on it here would have hidden
  real matches in that tier. Sub-$2 tickers now show by default; the
  toggle is there for whoever wants Craig's original, stricter view.
- Relative volume (today's volume / 20-day average) >= 1.5. Checked
  against 6 real TradingView Momentum Screener hits (2026-09-17: BVS,
  CCL, ACL, ASB, NUF, LGF) - every one passed every other criterion above
  cleanly, but landed well under 1.5x on my calc across every averaging
  window tried (5/10/20/30-day). TradingView's screener column isn't
  reproducible from daily close/volume bars alone, so gating on it would
  silently drop real setups. Still computed and shown per card, and used
  as the default sort key - just not a pass/fail gate.
- Short float % (5-15%). yfinance returns shortPercentOfFloat=None for
  every ASX ticker tested (BHP, CBA, CSL, PLS, ZIP and others - including
  stocks known to carry heavy short interest), so this field is simply
  not populated for the ASX market via this data source. Building it as
  a working filter would mean shipping a checkbox that silently never
  fires - so it's left out rather than faked.

The other optional filter (EPS/revenue growth > 0%) IS implemented, but
as a confluence-signal filter chip rather than a hard scan-time gate -
matching Pullback's "confluence scored, not gated" approach elsewhere on
this site. It's checked once per ticker that already passed every
technical filter above (a much smaller set than the full universe), via
one extra yfinance .info call per hit.

Nothing is ever hidden: every stock that qualifies shows up every run,
tagged with a "Since" stat (how many trading days it's been continuously
flagging). Tracked in seen_tickers_momentum.json next to this script - a
gap of more than STREAK_GAP_DAYS trading days between appearances resets
the streak, since that's a new setup, not a continuation of the old one.

Usage:
    python "MOMENTUM SCREENER.py"                 # full ASX scan
    python "MOMENTUM SCREENER.py" --workers 20    # faster
    python "MOMENTUM SCREENER.py" --tickers BHP,CBA,RIO
"""

import argparse
import csv
import importlib.util
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import yfinance as yf

import price_cache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_WORKERS  = 15
HISTORY_PERIOD   = "1y"
CHART_TRIM_BARS  = 260

MIN_AVG_VOLUME   = 500_000
RSI_PERIOD       = 14
RSI_MIN          = 50
RSI_MAX          = 65
SMA_SHORT        = 20
SMA_LONG         = 50
WEEK_CHANGE_MIN  = 2.0
WEEK_CHANGE_MAX  = 10.0
MONTH_CHANGE_MIN = 0.0

HISTORY_FILENAME = 'seen_tickers_momentum.json'


def calc_rsi(closes, period=RSI_PERIOD):
    """Standard Wilder's RSI - same implementation as OBV SCREENER.py."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def analyse_ticker(ticker, frame, market_cap, min_market_cap):
    if market_cap and market_cap < min_market_cap:
        return None
    min_needed = max(SMA_LONG + 5, 30)
    if frame is None or len(frame) < min_needed:
        return None

    closes  = frame['close'].tolist()
    opens   = frame['open'].tolist()
    highs   = frame['high'].tolist()
    lows    = frame['low'].tolist()
    volumes = frame['volume'].tolist()
    dates   = [d.strftime('%Y-%m-%d') for d in frame['date'].tolist()]

    price = closes[-1]

    # Mean, not median - unlike HH/OBV's anti-spike-manipulation floor,
    # Craig's own spec is literally "avg vol 30D > 500k" (confirmed by the
    # site owner 2026-09-17), and median was verified to exclude real
    # TradingView hits over it: CDA's 30d median volume is 485k (fails)
    # but its mean is 550k (passes) - same pattern on DDR (452k vs 569k).
    avg_vol = sum(volumes[-30:]) / min(30, len(volumes))
    if avg_vol < MIN_AVG_VOLUME:
        return None

    # Relative volume is shown and sorted on, but NOT gated on - checked
    # against 6 real TradingView Momentum Screener hits (2026-09-17: BVS,
    # CCL, ACL, ASB, NUF, LGF) and every one of them cleared every other
    # criterion below cleanly while landing well under 1.5x on every
    # averaging window tried (5/10/20/30-day, all from the same cached
    # OHLCV data used elsewhere on this site). TradingView's screener
    # column isn't reproducible from daily close/volume bars alone - so
    # gating on it would silently drop real setups rather than just rank
    # them lower.
    vol_today = volumes[-1]
    vol_avg20 = sum(volumes[-20:]) / min(20, len(volumes))
    rel_vol = vol_today / (vol_avg20 + 1)

    sma20 = calc_sma(closes, SMA_SHORT)
    sma50 = calc_sma(closes, SMA_LONG)
    if sma20 is None or sma50 is None:
        return None
    if not (price > sma20 > sma50):
        return None

    rsi = calc_rsi(closes, period=RSI_PERIOD)
    if rsi is None or not (RSI_MIN <= rsi <= RSI_MAX):
        return None

    price_1wk_ago = closes[-6] if len(closes) >= 6 else closes[0]
    change_1wk = (price - price_1wk_ago) / price_1wk_ago * 100 if price_1wk_ago else 0.0
    if not (WEEK_CHANGE_MIN <= change_1wk <= WEEK_CHANGE_MAX):
        return None

    price_1mo_ago = closes[-22] if len(closes) >= 22 else closes[0]
    change_1mo = (price - price_1mo_ago) / price_1mo_ago * 100 if price_1mo_ago else 0.0
    if change_1mo <= MONTH_CHANGE_MIN:
        return None

    prev_close = closes[-2]
    change_1d = (price - prev_close) / prev_close * 100 if prev_close else 0.0

    trim = slice(-CHART_TRIM_BARS, None)

    return {
        'ticker': ticker,
        'price': round(price, 4),
        'market_cap': int(market_cap) if market_cap else 0,
        'change_1d': round(change_1d, 2),
        'change_1wk': round(change_1wk, 2),
        'change_1mo': round(change_1mo, 2),
        'rsi': round(rsi, 1),
        'sma20': round(sma20, 4),
        'sma50': round(sma50, 4),
        'rel_vol': round(rel_vol, 2),
        'avg_vol': int(avg_vol),
        'dates': dates[trim],
        'opens': [round(v, 4) for v in opens[trim]],
        'highs': [round(v, 4) for v in highs[trim]],
        'lows': [round(v, 4) for v in lows[trim]],
        'closes': [round(v, 4) for v in closes[trim]],
        'volumes': [int(v) for v in volumes[trim]],
    }


def check_growth(ticker):
    """Second pass, only called for tickers that already passed every
    technical filter - a per-hit .info call, matching the insider
    tracker's two-pass discipline rather than hitting the full universe."""
    try:
        info = yf.Ticker(ticker + '.AX').get_info()
        eps_g = info.get('earningsGrowth')
        rev_g = info.get('revenueGrowth')
        return bool((eps_g is not None and eps_g > 0) or (rev_g is not None and rev_g > 0))
    except Exception:
        return False


def run_scan(universe, tickers, workers=DEFAULT_WORKERS, min_market_cap=20_000_000):
    print(f"Refreshing shared price cache for {len(tickers)} tickers...")
    cache, fetched_ok, total = price_cache.refresh_cache(tickers, workers=workers, max_history=HISTORY_PERIOD)
    print(f"  Cache refresh: {fetched_ok}/{total} fresh this run")

    results = []
    t0 = time.time()
    for i, t in enumerate(tickers, 1):
        mcap = universe.get(t, {}).get('market_cap') or 0
        frame = price_cache.get_ticker_frame(cache, t, min_days=SMA_LONG + 5)
        r = analyse_ticker(t, frame, mcap, min_market_cap)
        if r:
            r['sector'] = universe.get(t, {}).get('sector') or 'Other'
            results.append(r)
        if i % 300 == 0:
            print(f"  ...{i}/{len(tickers)} scanned ({time.time()-t0:.0f}s), {len(results)} passing so far")

    usable = price_cache.count_usable(cache, tickers, min_days=SMA_LONG + 5)
    print(f"Scan complete: {len(results)} pass every filter.")

    if results:
        print(f"  Checking earnings/revenue growth for {len(results)} passing ticker(s)...")
        for r in results:
            if check_growth(r['ticker']):
                r['signals_present'] = ['earnings_growth']
            time.sleep(0.05)

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
            'score': int(round(min(r['rel_vol'] / 5, 1.0) * 100)),
            'first_seen_ts': first_seen_ts,
            'stats': [
                {'label': 'RSI', 'value': f"{r['rsi']:.0f}"},
                {'label': '1wk', 'value': f"{r['change_1wk']:+.1f}%"},
                {'label': 'Rel Vol', 'value': f"{r['rel_vol']:.1f}×"},
                {'label': 'Since', 'value': since_label},
            ],
            'signals_present': r.get('signals_present', []),
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        total_scanned=total_scanned,
        title='🚀 ASX Momentum Screener',
        subtitle='ASX stocks with a confirmed uptrend and healthy RSI — not already overbought. Sorted by relative volume.',
        footer_note=(
            'ASX Momentum Screener · Data via Yahoo Finance (yfinance), ticker universe via SeaBee<br>'
            'Criteria: 30d average volume &ge; 500,000 AND price above 20d SMA above 50d SMA AND '
            'RSI 50-65 AND 1wk change +2% to +10% AND 1mo change &gt; 0% AND market cap &ge; $20M. Relative volume '
            'is shown and sorted on, not gated on (TradingView\'s exact formula isn\'t reproducible from daily bars). '
            'Earnings/revenue growth is shown as a filter chip, not a hard gate. Price &ge; $2 is a toggle above '
            '(default off) - real TradingView micro-cap hits trade well under $2, so it isn\'t enforced by default.'
        ),
        out_path=out_path + '.html',
    )
    print(f"  📄 HTML report → {out_path}.html")


def build_csv(results, out_path):
    array_fields = {'dates', 'opens', 'highs', 'lows', 'closes', 'volumes', 'signals_present'}
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
# A stock that keeps qualifying run after run is more interesting, not
# less - persistence is part of the momentum signal itself. So this no
# longer hides recently-seen tickers; it tracks how long each one has
# been continuously flagging (a "Since" stat on the card) instead. A gap
# of more than STREAK_GAP_DAYS trading days between appearances resets
# the streak - that's a new setup, not a continuation of the old one.

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
    parser = argparse.ArgumentParser(description='ASX Momentum Screener')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--out', type=str, default='asx_momentum_results')
    parser.add_argument('--tickers', type=str, default='')
    args = parser.parse_args()

    spec = importlib.util.spec_from_file_location("hh", "HH SCREENER.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)

    universe = hh.get_asx_universe()

    print("=" * 60)
    print("  ASX MOMENTUM SCREENER")
    print("=" * 60)

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
        print(f"  {'TICKER':<8} {'RELVOL':>7} {'RSI':>5} {'1WK%':>6}")
        for r in results[:20]:
            print(f"  {r['ticker']:<8} {r['rel_vol']:>6.1f}× {r['rsi']:>5.1f} {r['change_1wk']:>5.1f}%")
        if len(results) > 20:
            print(f"  ... and {len(results)-20} more in the report")

    out_base = os.path.join(SCRIPT_DIR, 'asx_momentum_results')
    build_html_report(results, len(tickers), out_base)
    build_csv(results, out_base)
    build_tradingview_watchlist(results, out_base)


if __name__ == '__main__':
    main()
