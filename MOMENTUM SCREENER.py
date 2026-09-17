"""
ASX Momentum Screener
======================
Built from Craig Tapping's TradingView Momentum Screener spec. Flags ASX
stocks already in a confirmed uptrend with a genuine volume pickup behind
them - the opposite intent of Pre-Breakout (OBV), which looks for
strength building BEFORE a move.

Criteria (a stock must pass ALL of these):
  1. Price > $2
  2. 30-day median daily volume >= 500,000 shares (median, not mean - a
     single spike day can drag a 30-day MEAN above threshold on an
     otherwise-dead stock; see HH/OBV SCREENER.py's identical fix)
  3. Relative volume (today's volume / 20-day average) >= 1.5
  4. Price above the 20-day SMA, which is itself above the 50-day SMA
     ("stacked" moving averages)
  5. RSI (14-period) between 50 and 65 - healthy momentum, not yet
     overbought
  6. 1-week change between +2% and +10%
  7. 1-month change > 0%
  8. Market cap >= $20M (site-wide floor, see HH SCREENER.py)

One filter from Craig's original spec was dropped after checking real
data: short float % (5-15%). yfinance returns shortPercentOfFloat=None
for every ASX ticker tested (BHP, CBA, CSL, PLS, ZIP and others -
including stocks known to carry heavy short interest), so this field is
simply not populated for the ASX market via this data source. Building
it as a working filter would mean shipping a checkbox that silently
never fires - so it's left out rather than faked.

The other optional filter (EPS/revenue growth > 0%) IS implemented, but
as a confluence-signal filter chip rather than a hard scan-time gate -
matching Pullback's "confluence scored, not gated" approach elsewhere on
this site. It's checked once per ticker that already passed every
technical filter above (a much smaller set than the full universe), via
one extra yfinance .info call per hit.

Cooldown: a stock that appeared in your results stays hidden from future
runs for 5 trading days. Tracked in seen_tickers_momentum.json next to
this script. Use --fresh to ignore this, or --cooldown 0 to disable it.

Usage:
    python "MOMENTUM SCREENER.py"                 # full ASX scan
    python "MOMENTUM SCREENER.py" --workers 20    # faster
    python "MOMENTUM SCREENER.py" --tickers BHP,CBA,RIO
    python "MOMENTUM SCREENER.py" --fresh
"""

import argparse
import csv
import importlib.util
import json
import os
import statistics
import sys
import time
from datetime import date, timedelta

import yfinance as yf

import price_cache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_WORKERS  = 15
HISTORY_PERIOD   = "1y"
CHART_TRIM_BARS  = 260

MIN_PRICE        = 2.0
MIN_AVG_VOLUME   = 500_000
REL_VOL_MIN      = 1.5
RSI_PERIOD       = 14
RSI_MIN          = 50
RSI_MAX          = 65
SMA_SHORT        = 20
SMA_LONG         = 50
WEEK_CHANGE_MIN  = 2.0
WEEK_CHANGE_MAX  = 10.0
MONTH_CHANGE_MIN = 0.0

COOLDOWN_DAYS    = 5
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
    if price < MIN_PRICE:
        return None

    median_vol = statistics.median(volumes[-30:])
    if median_vol < MIN_AVG_VOLUME:
        return None
    avg_vol = sum(volumes[-30:]) / min(30, len(volumes))

    vol_today = volumes[-1]
    vol_avg20 = sum(volumes[-20:]) / min(20, len(volumes))
    rel_vol = vol_today / (vol_avg20 + 1)
    if rel_vol < REL_VOL_MIN:
        return None

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


def build_html_report(results, excluded, total_scanned, out_path):
    from dashboard_template import render_dashboard_html

    def to_card(r):
        return {
            'ticker': r['ticker'],
            'sector': r['sector'],
            'market_cap': r['market_cap'],
            'price': r['price'],
            'change_1d': r['change_1d'],
            'score': int(round(min(r['rel_vol'] / 5, 1.0) * 100)),
            'stats': [
                {'label': 'RSI', 'value': f"{r['rsi']:.0f}"},
                {'label': '1wk', 'value': f"{r['change_1wk']:+.1f}%"},
                {'label': 'Rel Vol', 'value': f"{r['rel_vol']:.1f}×"},
            ],
            'signals_present': r.get('signals_present', []),
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        excluded_cards=[to_card(r) for r in excluded],
        total_scanned=total_scanned,
        title='🚀 ASX Momentum Screener',
        subtitle='ASX stocks with a confirmed uptrend, healthy RSI and a real relative-volume pickup — not already overbought.',
        footer_note=(
            'ASX Momentum Screener · Data via Yahoo Finance (yfinance), ticker universe via SeaBee<br>'
            'Criteria: price &gt; $2 AND 30d median volume &ge; 500,000 AND relative volume &ge; 1.5&times; AND '
            'price above 20d SMA above 50d SMA AND RSI 50-65 AND 1wk change +2% to +10% AND 1mo change &gt; 0% '
            'AND market cap &ge; $20M. Earnings/revenue growth is shown as a filter chip, not a hard gate.'
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


# ─── COOLDOWN / SEEN-TICKER HISTORY ─────────────────────────────────────────

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


def get_excluded_tickers(history, cooldown_days):
    if cooldown_days <= 0:
        return set()
    today = date.today()
    excluded = set()
    for ticker, last_seen_str in history.items():
        try:
            last_seen = datetime_strptime(last_seen_str)
        except Exception:
            continue
        if _weekdays_between(last_seen, today) < cooldown_days:
            excluded.add(ticker)
    return excluded


def datetime_strptime(s):
    from datetime import datetime
    return datetime.strptime(s, '%Y-%m-%d').date()


def update_seen_history(history, results):
    today_str = date.today().strftime('%Y-%m-%d')
    for r in results:
        history[r['ticker']] = today_str
    return history


# ─── MAIN ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ASX Momentum Screener')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--out', type=str, default='asx_momentum_results')
    parser.add_argument('--tickers', type=str, default='')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--cooldown', type=int, default=COOLDOWN_DAYS)
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
    skipped = []
    if args.fresh:
        print(f"\n  🔄 --fresh used: showing all results, ignoring cooldown history")
    else:
        excluded_tickers = get_excluded_tickers(history, args.cooldown)
        skipped = [r for r in results if r['ticker'] in excluded_tickers]
        results = [r for r in results if r['ticker'] not in excluded_tickers]
        if skipped:
            print(f"\n  🔁 Hid {len(skipped)} stock(s) already seen within the last {args.cooldown} trading days")

    history = update_seen_history(history, results)
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
    build_html_report(results, skipped, len(tickers), out_base)
    build_csv(results + skipped, out_base)
    build_tradingview_watchlist(results, out_base)


if __name__ == '__main__':
    main()
