"""ASX Wave 2 Scanner: finds tickers currently sitting in a validated
Elliott Wave "Wave 2" pullback zone - a real 5-wave impulse followed by
an ABC correction retracing 38.2%-78.6% of that impulse - rather than
Pullback (Zag Zone)'s much looser "retracing 38.2%-61.8% of its latest
swing" with no structure check at all.

This is the same zigzag-pivot + wave-validity logic already backtested
earlier (thin-but-real edge: ~28-35% win rate vs a 25% breakeven for the
3R target used here), adapted from backtesting historical outcomes to
reporting each ticker's CURRENT state:

  - Forming: the impulse + ABC are both complete, but price hasn't yet
    closed back above the ABC's point-C high (the entry trigger). This
    is a watchlist, not a signal - nothing has fired yet.
  - Triggered: price closed above C's high within the last
    TRIGGER_FRESHNESS_DAYS trading days, and the stop (C's low) hasn't
    been hit since. This is the actual entry-worthy signal.

A ticker whose trigger fired longer ago, or whose stop has since been
hit, is dropped entirely - "Wave 2 scanner" should mean "still live right
now", not "here's what would have worked last month" (that's what the
backtest is for).

Reuses the site's existing shared infrastructure: price_cache.py (same
warm OHLCV cache as HH/OBV/Pullback), HH SCREENER.py's get_asx_universe()
and liquidity constants (dynamically imported - see build_insider_index.py
for the same pattern, needed because of the space in that filename), and
dashboard_template.py's card-grid renderer (same as OBV/Pullback).

NOT yet linked from the site nav - this is a first cut to review before
deciding whether/how to ship it.
"""
import datetime
import importlib.util
import os
import sys
import time

import price_cache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The zigzag %% threshold trades off noise-filtering vs missing genuine
# smaller-degree waves. 8% and 12% were both backtested with a similar
# thin-but-real edge; 10% is a reasonable middle ground for a first cut
# across the whole ASX universe (small/volatile names arguably want a
# wider threshold than large caps, but that's a refinement, not this).
ZIGZAG_PCT = 0.10

# A trigger older than this is no longer "live" - the setup already
# played out one way or another, this isn't a backtest.
TRIGGER_FRESHNESS_DAYS = 10

HISTORY_PERIOD = "2y"  # enough room for a multi-month impulse + correction
DEFAULT_WORKERS = 15


def zigzag(dates, closes, pct):
    """Collapses a close-price series into alternating swing pivots
    ([bar_index, date, price, 'H'|'L']), each at least `pct` away from
    the prior pivot. Pure geometry - no wave-counting here."""
    n = len(closes)
    if n < 2:
        return []
    pivots = []
    trend = 0
    last_i, last_p = 0, closes[0]
    for i in range(1, n):
        p = closes[i]
        if trend >= 0 and p >= last_p:
            last_i, last_p = i, p
            if trend == 0:
                trend = 1
        elif trend <= 0 and p <= last_p:
            last_i, last_p = i, p
            if trend == 0:
                trend = -1
        else:
            change = abs(p - last_p) / last_p
            if change >= pct:
                kind = 'H' if trend == 1 else 'L'
                pivots.append([last_i, dates[last_i], last_p, kind])
                trend = 1 if p > last_p else -1
                last_i, last_p = i, p
    if trend != 0:
        kind = 'H' if trend == 1 else 'L'
        pivots.append([last_i, dates[last_i], last_p, kind])
    if pivots:
        start_kind = 'L' if pivots[0][3] == 'H' else 'H'
    else:
        start_kind = 'L'
    pivots.insert(0, [0, dates[0], closes[0], start_kind])
    return pivots


def find_current_setup(df, pct):
    """Scans for every valid 5-wave-impulse + ABC-correction setup in the
    ticker's price history, keeps the MOST RECENT one, and reports its
    current state relative to today's price. Returns None if there's no
    valid setup at all, or if the most recent one has already gone stale
    (triggered too long ago, or stopped out since)."""
    dates = df['date'].values
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    pivots = zigzag(dates, closes, pct)

    best = None
    for i in range(len(pivots) - 5):
        p0, p1, p2, p3, p4, p5 = pivots[i:i + 6]
        if [p0[3], p1[3], p2[3], p3[3], p4[3], p5[3]] != ['L', 'H', 'L', 'H', 'L', 'H']:
            continue
        wave1 = p1[2] - p0[2]
        wave3 = p3[2] - p2[2]
        wave5 = p5[2] - p4[2]
        if wave1 <= 0 or wave3 <= 0 or wave5 <= 0:
            continue
        if p2[2] <= p0[2]:
            continue  # wave2 retraced 100%+ of wave1
        if p4[2] <= p1[2]:
            continue  # wave4 overlaps wave1 territory
        if wave3 <= wave1 and wave3 <= wave5:
            continue  # wave3 is the shortest - invalid
        j = i + 5
        if j + 3 >= len(pivots):
            continue
        qA, qB, qC = pivots[j + 1], pivots[j + 2], pivots[j + 3]
        if [qA[3], qB[3], qC[3]] != ['L', 'H', 'L']:
            continue
        impulse_range = p5[2] - p0[2]
        if impulse_range <= 0:
            continue
        retrace_frac = (p5[2] - qC[2]) / impulse_range
        if not (0.382 <= retrace_frac <= 0.786):
            continue
        if qC[2] <= p0[2]:
            continue  # breached start of wave1 - fully invalidated
        best = (p0, p1, p2, p3, p4, p5, qA, qB, qC, retrace_frac)  # keep overwriting -> last = most recent

    if best is None:
        return None
    p0, p1, p2, p3, p4, p5, qA, qB, qC, retrace_frac = best
    wave_points = [
        {'idx': int(pt[0]), 'price': float(pt[2]), 'label': lbl}
        for pt, lbl in zip([p0, p1, p2, p3, p4, p5, qA, qB, qC],
                            ['0', '1', '2', '3', '4', '5', 'A', 'B', 'C'])
    ]
    qC_idx = qC[0]
    qC_high = highs[qC_idx]
    qC_low = lows[qC_idx]

    entry_idx = None
    for k in range(qC_idx + 1, n):
        if closes[k] > qC_high:
            entry_idx = k
            break

    last_idx = n - 1
    current_price = closes[-1]

    if entry_idx is None:
        return {
            'state': 'Forming', 'p0_idx': int(p0[0]), 'wave_points': wave_points,
            'p0_date': str(p0[1])[:10], 'qC_date': str(qC[1])[:10],
            'qC_high': float(qC_high), 'qC_low': float(qC_low),
            'retrace_pct': float(retrace_frac * 100),
            'dist_to_trigger_pct': float((qC_high - current_price) / current_price * 100),
        }

    days_since_trigger = last_idx - entry_idx
    if days_since_trigger > TRIGGER_FRESHNESS_DAYS:
        return None  # already played out, not "live" anymore

    entry_price = closes[entry_idx]
    stop = qC_low
    risk = entry_price - stop
    if risk <= 0:
        return None
    for k in range(entry_idx + 1, n):
        if lows[k] <= stop:
            return None  # already stopped out

    target = entry_price + 3 * risk
    return {
        'state': 'Triggered', 'p0_idx': int(p0[0]), 'wave_points': wave_points,
        'p0_date': str(p0[1])[:10], 'qC_date': str(qC[1])[:10],
        'entry_date': str(dates[entry_idx])[:10], 'entry_price': float(entry_price),
        'stop': float(stop), 'target_3r': float(target),
        'retrace_pct': float(retrace_frac * 100),
        'current_r': float((current_price - entry_price) / risk),
        'days_since_trigger': int(days_since_trigger),
    }


def main():
    sys.path.insert(0, ".")
    spec = importlib.util.spec_from_file_location("hh", "HH SCREENER.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)

    universe = hh.get_asx_universe()
    tickers = sorted(t for t in universe if len(t) == 3)
    print(f"Universe: {len(tickers)} tickers")

    print("Refreshing shared price cache...")
    cache, fetched_ok, total = price_cache.refresh_cache(tickers, workers=DEFAULT_WORKERS, max_history=HISTORY_PERIOD)
    print(f"Cache refresh: {fetched_ok}/{total} fresh this run")

    forming, triggered = [], []
    t0 = time.time()
    for i, t in enumerate(tickers, 1):
        u = universe[t]
        mcap = u.get("market_cap") or 0
        if mcap and mcap < hh.MIN_MARKET_CAP:
            continue
        df = price_cache.get_ticker_frame(cache, t, min_days=120)
        if df is None or len(df) < 120:
            continue
        price = float(df['close'].iloc[-1])
        if price < hh.MIN_PRICE:
            continue
        avg_vol = float(df['volume'].tail(30).mean())
        if avg_vol < hh.MIN_AVG_VOLUME:
            continue

        result = find_current_setup(df, ZIGZAG_PCT)
        if result is None:
            continue

        prev_close = float(df['close'].iloc[-2]) if len(df) >= 2 else price
        change_1d = (price - prev_close) / prev_close * 100 if prev_close else 0.0

        # Export enough trailing history that wave 0 (the start of the
        # impulse) is always in frame, with a little padding for context -
        # a plain 130-bar tail (matching OBV/Pullback's cards) would often
        # crop off the earlier waves of a multi-month structure.
        tail_len = min(max(260, len(df) - result['p0_idx'] + 20), len(df))
        offset = len(df) - tail_len
        wave_points = [{**p, 'idx': p['idx'] - offset} for p in result.pop('wave_points')]

        card_base = {
            'ticker': t, 'sector': u.get('sector') or 'Other',
            'market_cap': int(mcap), 'price': price, 'change_1d': round(change_1d, 2),
            'dates': [str(d)[:10] for d in df['date'].tail(tail_len).values],
            'opens': df['open'].tail(tail_len).tolist(), 'highs': df['high'].tail(tail_len).tolist(),
            'lows': df['low'].tail(tail_len).tolist(), 'closes': df['close'].tail(tail_len).tolist(),
            'volumes': df['volume'].tail(tail_len).tolist(),
            'wave_points': wave_points,
        }
        card_base.update(result)
        if result['state'] == 'Forming':
            forming.append(card_base)
        else:
            triggered.append(card_base)

        if i % 200 == 0:
            print(f"  ...{i}/{len(tickers)} scanned ({time.time()-t0:.0f}s) - "
                  f"{len(triggered)} triggered, {len(forming)} forming")

    print(f"Done: {len(triggered)} triggered, {len(forming)} forming (of {len(tickers)} scanned).")
    build_html_report(triggered, forming, len(tickers))


def build_html_report(triggered, forming, total_scanned):
    from dashboard_template import render_dashboard_html

    def to_card(r, score):
        if r['state'] == 'Triggered':
            stats = [
                {'label': 'Retrace', 'value': f"{r['retrace_pct']:.1f}%"},
                {'label': 'R now', 'value': f"{r['current_r']:.1f}R"},
                {'label': 'Since', 'value': f"{r['days_since_trigger']}d"},
            ]
        else:
            stats = [
                {'label': 'Retrace', 'value': f"{r['retrace_pct']:.1f}%"},
                {'label': 'To trigger', 'value': f"{r['dist_to_trigger_pct']:.1f}%"},
                {'label': 'C formed', 'value': r['qC_date']},
            ]
        return {
            'ticker': r['ticker'], 'sector': r['sector'], 'market_cap': r['market_cap'],
            'price': r['price'], 'change_1d': r['change_1d'], 'score': score,
            'stats': stats,
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
            'wave_points': r['wave_points'],
        }

    # Triggered ones sort ahead of Forming ones by giving them a higher
    # score band (the card grid's own "Ranked" view sorts by score) -
    # a live signal is more actionable right now than a watchlist item.
    triggered_cards = [to_card(r, round(80 + min(max(r['current_r'], 0), 3) * 6)) for r in triggered]
    forming_cards = [to_card(r, round(max(40 - min(r['dist_to_trigger_pct'], 20) * 1.5, 5))) for r in forming]

    build_date = datetime.date.today().strftime("%-d %b %Y")
    render_dashboard_html(
        cards=triggered_cards + forming_cards,
        excluded_cards=[],
        total_scanned=total_scanned,
        title='🌊 ASX Wave 2 Scanner',
        subtitle='ASX stocks currently in a validated Elliott Wave 2 pullback - a real 5-wave impulse '
                  'followed by an ABC correction, not just any retracement.',
        footer_note=(
            f'ASX Wave 2 Scanner · Data via Yahoo Finance (yfinance), ticker universe via SeaBee/Market Index · rebuilt {build_date}<br>'
            'Method: zigzag pivots (10% threshold) form a 5-wave impulse (wave 2 &lt; wave1 start, wave 4 above wave 1, wave 3 not shortest), '
            'then an ABC correction retracing 38.2%-78.6% of the impulse. "Triggered" = price closed back above the ABC\'s point-C high within '
            'the last 10 trading days and hasn\'t hit the stop (point-C low) since. "Forming" = the setup is valid but hasn\'t triggered yet - a '
            'watchlist, not a signal. Backtested on real ASX data with a thin-but-real edge (~28-35% win rate vs a 25% breakeven for the 3R target '
            'used here) - not financial advice, and this scanner is not yet linked from site navigation while it\'s being reviewed.'
        ),
        out_path="wave2-scanner.html",
    )


if __name__ == "__main__":
    main()
