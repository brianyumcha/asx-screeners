"""
US Cash-Secured Put Candidate Screener
===========================
Screens US-listed stocks and ETFs as CANDIDATES for cash-secured-put
entries (the acquisition leg of a wheel-income strategy) - not just
whether they're a good company, and not a specific strike/premium quote.

Originally this read yfinance's live options chain to suggest an exact
~30-delta strike. Dropped that entirely (2026-10-16, user feedback) -
the options chain is only meaningfully fresh during US market hours, and
the user isn't usually awake then, so a chain-dependent screener was
only ever usable for a few hours a day. This version needs nothing but
daily price history, so it works at any time, on any schedule.

What it screens for instead, combining Andy Robertson's liquidity
checklist ("Lazy Formula for Options Income" - see Andy Robertson -
Options Trading Methodology Notes.md) with technical-analysis entry
timing suited specifically to CSPs:
  - Liquidity: 20d avg daily volume (same tiers as before)
  - Trend: price vs 200-day SMA - a CSP means being fine owning the
    stock if assigned, so a structural downtrend (below the 200 SMA) is
    exactly the risk to avoid, not just a bad days'-worth of price action
  - Pullback depth: price vs 50-day SMA - a real "buy the dip in an
    uptrend" entry sits a little below the 50 SMA, not far above it
    (chasing - the put strike you'd want is too far OTM to collect much)
    and not deep in a breakdown
  - RSI(14): a reset in the 30-55 band, not still falling (RSI dropping
    through the 20s is often a falling knife, not a dip) and not
    overbought (>70, same chasing problem as above)
  - Bollinger %B (20, 2 sigma): price near/at the lower band is a
    statistically stretched pullback - also usually when IV (and so
    premium) runs richest, the options-chain-era "IV sweet spot" check's
    nearest equivalent here
  - Realized volatility (20d, annualized): a free stand-in for implied
    volatility - too low and the premium isn't worth it, too high is
    probably distress, not opportunity
  - Earnings clear of the next ~30 days (same caution as before, just
    no longer tied to one specific contract's expiry)

This produces a ranked CANDIDATE list, not an executable trade - pull
the actual strike/premium from your own broker when you're ready to
place it, same as checking Andy's criteria by hand.

Universe: S&P 500 (scraped from Wikipedia, with a hardcoded fallback) plus
a curated list of names/ETFs Andy specifically discussed that aren't S&P
500 members (PLTR-adjacent small/mid caps, commodity ETFs, etc). Written as
two separate reports - <out>-us-csp.html and <out>-us-etf.html - since a
CSP on an ETF (basket, no single-name earnings gap) is a different risk
profile than one on a single stock.

Usage:
    python3 "OPTIONS SCREENER.py"                 # full S&P 500 + extras scan
    python3 "OPTIONS SCREENER.py" --tickers AAPL,MSFT,PLTR
    python3 "OPTIONS SCREENER.py" --workers 20
"""

import argparse
import csv
import io
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_WORKERS = 12
EARNINGS_LOOKAHEAD_DAYS = 30   # roughly one CSP cycle - same caution as before,
                               # just not tied to one specific contract's expiry

MIN_AVG_VOLUME_TIER1 = 5_000_000   # Andy's "tier 1" daily volume floor
MIN_AVG_VOLUME_TIER2 = 2_000_000   # "tier 2" floor - below this, excluded entirely

RSI_PERIOD = 14
RSI_GOOD_LOW, RSI_GOOD_HIGH = 30, 55   # a real reset, not still falling or overbought
RSI_HARD_OVERSOLD = 20                  # below this = possible falling knife, not a dip

BB_PERIOD, BB_STDDEV = 20, 2.0

HV_PERIOD = 20                  # realized/historical volatility lookback
HV_GOOD_LOW, HV_GOOD_HIGH = 0.25, 0.55   # annualized - "worth selling premium on" band
HV_HARD_FLOOR, HV_HARD_CEIL = 0.12, 1.00  # outside this, too quiet or likely distress

CHART_TRIM_BARS = 260

HISTORY_FILENAME = 'seen_tickers_options.json'
STREAK_GAP_DAYS = 5

# Names/ETFs Andy specifically discussed across the methodology notes that
# aren't reliably S&P 500 members (small/mid caps, recent IPOs, commodity
# and leveraged ETFs) - folded into the universe alongside the S&P 500 scrape.
EXTRA_TICKERS = """
PLTR SOFI HOOD RKLB SMCI VRT CRWV NBIS ANET GEV APLD FIG HIMS OSCR DUOL
TME AMPX SMR AGX BE OXY EQT KGC PAAS FCX MOS CEG VST LEU CCJ FRO DHT
NEE XLE GDX SIL URA SPY QQQ IWM TQQQ SOXL NVDL TNA MSTR GOLD WPM
""".split()

ETF_TICKERS = {'XLE', 'GDX', 'SIL', 'URA', 'SPY', 'QQQ', 'IWM', 'TQQQ', 'SOXL', 'NVDL', 'TNA'}

FALLBACK_SP500 = """
AAPL MSFT NVDA AMZN GOOGL GOOG META BRK.B AVGO TSLA LLY JPM V UNH XOM
MA PG COST HD JNJ ABBV WMT NFLX BAC CRM KO MRK CVX AMD PEP ADBE TMO
LIN ACN MCD CSCO ABT WFC ORCL DHR IBM GE TXN VZ PM INTU AMGN CAT NOW
UBER ISRG QCOM SPGI AMAT BKNG NEE RTX HON PFE LOW UNP T LMT COP PLD
AXP SYK ETN BLK MDT GS TJX C BA NKE SCHW ADP CB MMC MU PANW BSX VRTX
LRCX CI ELV MO GILD REGN ADI KLAC SO ZTS SBUX PGR DUK CME SLB BDX
WM EQIX SHW UPS APH ITW CDNS FI TGT CL AON MCO ICE MSI PYPL NXPI
SNPS MMM USB TT WELL CMG EOG HCA EMR PNC CSX APD ROP MAR CTAS FCX
ORLY NSC AJG GD PSA ECL MCK TFC HLT CARR AFL COF SRE SPG RCL ADSK
FTNT PCAR TDG OXY FDX CPRT PSX MPC VLO DXCM MET AEP KMB PAYX MSCI
EW O MNST CMI ALL HES DLR JCI GWW AZO VRSK A XEL IDXX RSG ODFL
CTVA PWR GEHC KVUE EA IQV FAST OTIS KDP CHTR BIIB HPQ GIS ANSS LHX
PEG ED SYY WAB YUM DD KR HSY RMD CDW EXC DOW VMC MLM IT ON CCI
AMP MTB WTW HPE STZ NUE IR MCHP WEC VICI GPN TSCO FICO K DVN
""".split()


def get_universe():
    """S&P 500 constituents (scraped), de-duplicated with the curated extras
    above. Falls back to a hardcoded ~160-name S&P 500 snapshot if Wikipedia
    is unreachable - matches the established pattern in the ASX screeners
    (get_asx_tickers's own multi-source fallback chain)."""
    tickers = set()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        r = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies', headers=headers, timeout=20)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
        symbols = tables[0]['Symbol'].tolist()
        tickers.update(str(s).strip().upper().replace('.', '-') for s in symbols)
        print(f"  Got {len(tickers)} S&P 500 tickers from Wikipedia")
    except Exception as e:
        print(f"  ⚠ Wikipedia S&P 500 fetch failed ({e}), using hardcoded fallback list")
        tickers.update(FALLBACK_SP500)

    tickers.update(EXTRA_TICKERS)
    return sorted(tickers)


# ─── TECHNICAL INDICATORS (pure price-history, no options chain needed) ──────

def compute_rsi(closes, period=RSI_PERIOD):
    """Wilder's smoothed RSI, standard 14-period. `closes` is a pandas Series."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period).mean()
    last_gain, last_loss = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_bollinger_pctb(closes, period=BB_PERIOD, stddev=BB_STDDEV):
    """%B: 0 = at the lower band, 1 = at the upper band, <0/>1 = beyond it."""
    sma = closes.rolling(period).mean().iloc[-1]
    std = closes.rolling(period).std().iloc[-1]
    if pd.isna(sma) or pd.isna(std) or std == 0:
        return None
    upper, lower = sma + stddev * std, sma - stddev * std
    if upper == lower:
        return None
    price = closes.iloc[-1]
    return (price - lower) / (upper - lower)


def compute_realized_vol(closes, period=HV_PERIOD):
    """Annualized realized (historical) volatility from daily log returns -
    the free stand-in for implied volatility now that there's no options
    chain to read IV from."""
    log_ret = (closes / closes.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else None)
    std = log_ret.rolling(period).std().iloc[-1]
    if pd.isna(std):
        return None
    return std * math.sqrt(252)


# ─── PER-TICKER SCAN ───────────────────────────────────────────────────────────

def score_ticker(ticker, sector_override=None):
    t = yf.Ticker(ticker)

    hist = t.history(period='1y', auto_adjust=False)
    if hist is None or len(hist) < 210:   # need a real 200-day SMA
        return None
    closes = hist['Close']
    price = closes.iloc[-1]
    prev_close = closes.iloc[-2]
    change_1d = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    avg_volume = hist['Volume'].tail(20).mean()
    if not avg_volume or avg_volume < MIN_AVG_VOLUME_TIER2:
        return None

    sma50 = closes.rolling(50).mean().iloc[-1]
    sma200 = closes.rolling(200).mean().iloc[-1]
    if pd.isna(sma50) or pd.isna(sma200):
        return None
    above_sma200 = bool(price > sma200)
    pct_vs_sma50 = round((price - sma50) / sma50 * 100, 2)

    rsi14 = compute_rsi(closes)
    bb_pctb = compute_bollinger_pctb(closes)
    hv_pct = compute_realized_vol(closes)
    if rsi14 is None or bb_pctb is None or hv_pct is None:
        return None
    if not (HV_HARD_FLOOR <= hv_pct <= HV_HARD_CEIL):
        return None   # too quiet or too wild to be a real tradable candidate

    try:
        info = t.get_info()
    except Exception:
        info = {}
    is_etf = ticker in ETF_TICKERS or info.get('quoteType') == 'ETF'
    sector = sector_override or info.get('sector') or ('ETF' if is_etf else 'Other')
    market_cap = info.get('marketCap') or (price * (info.get('sharesOutstanding') or 0))
    name = info.get('longName') or info.get('shortName') or ticker

    # Earnings-clear check: only penalises on a CONFIRMED upcoming date
    # inside the lookahead window - an unknown/unavailable date gets the
    # benefit of the doubt, same caution Andy applies by hand.
    earnings_date = None
    earnings_inside_window = False
    try:
        ed = t.get_earnings_dates(limit=4)
        if ed is not None and not ed.empty:
            today = date.today()
            cutoff = today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)
            for ts in ed.index:
                d = ts.date()
                if today <= d <= cutoff:
                    earnings_inside_window = True
                    earnings_date = d.strftime('%Y-%m-%d')
                    break
    except Exception:
        pass

    vol_tier = 1 if avg_volume >= MIN_AVG_VOLUME_TIER1 else 2

    score = 100
    score -= {1: 0, 2: 10}[vol_tier]
    if not above_sma200:
        score -= 20   # structural downtrend - exactly the assignment risk a CSP seller avoids
    if pct_vs_sma50 > 8:
        score -= 8    # extended above the dip zone - chasing, poor premium/entry
    elif pct_vs_sma50 < -15:
        score -= 10   # deep breakdown well below the 50 SMA, not a routine pullback
    if rsi14 < RSI_HARD_OVERSOLD:
        score -= 10   # possible falling knife
    elif rsi14 > 70:
        score -= 10   # overbought - chasing
    elif not (RSI_GOOD_LOW <= rsi14 <= RSI_GOOD_HIGH):
        score -= 4    # outside the sweet reset band but not extreme
    if bb_pctb < -0.1:
        score -= 8    # beyond the lower band - stretched further than a routine pullback
    elif bb_pctb > 0.8:
        score -= 8    # riding the upper band - chasing
    if not (HV_GOOD_LOW <= hv_pct <= HV_GOOD_HIGH):
        score -= 8    # outside the hard floor/ceiling already excluded above;
                       # this is the softer "not quite the sweet spot" band
    if earnings_inside_window:
        score -= 15
    score = max(0, min(100, score))

    dates = hist.index.strftime('%Y-%m-%d').tolist()[-CHART_TRIM_BARS:]
    opens = hist['Open'].tolist()[-CHART_TRIM_BARS:]
    highs = hist['High'].tolist()[-CHART_TRIM_BARS:]
    lows = hist['Low'].tolist()[-CHART_TRIM_BARS:]
    closes_trim = closes.tolist()[-CHART_TRIM_BARS:]
    volumes = hist['Volume'].tolist()[-CHART_TRIM_BARS:]

    return {
        'ticker': ticker,
        'name': name,
        'is_etf': is_etf,
        'sector': sector,
        'market_cap': int(market_cap) if market_cap else 0,
        'price': round(float(price), 2),
        'change_1d': round(change_1d, 2),
        'avg_volume': int(avg_volume),
        'vol_tier': vol_tier,
        'above_sma200': above_sma200,
        'pct_vs_sma50': pct_vs_sma50,
        'rsi14': round(float(rsi14), 1),
        'bb_pctb': round(float(bb_pctb), 3),
        'hv_pct': round(float(hv_pct) * 100, 1),
        'earnings_date': earnings_date, 'earnings_inside_window': earnings_inside_window,
        'score': score,
        'dates': dates, 'opens': opens, 'highs': highs, 'lows': lows,
        'closes': closes_trim, 'volumes': volumes,
    }


def run_scan(tickers, workers=DEFAULT_WORKERS):
    total = len(tickers)
    t0 = time.time()
    results, failed, done = [], 0, 0

    print(f"\n🔍 Scoring {total} US tickers for CSP-candidate suitability | {workers} threads\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(score_ticker, t): t for t in tickers}
        for fut in as_completed(futures):
            done += 1
            tick = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    flag = f"  ✅ {tick:6s} score={res['score']:3d} RSI={res['rsi14']:5.1f} HV={res['hv_pct']:5.1f}%"
                else:
                    flag = f"  ·  {tick}"
            except Exception as e:
                failed += 1
                flag = f"  ✗  {tick} ({e})"

            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            sys.stdout.write(f"\r  [{done:4d}/{total}]  {rate:4.1f}/s  ETA {eta:4.0f}s  Qualified: {len(results):3d}   ")
            sys.stdout.flush()

    print(f"\n\n✅ Scan complete in {time.time()-t0:.1f}s")
    print(f"   Scanned: {total}  |  Qualified: {len(results)}  |  Failed: {failed}")
    return results


# ─── STREAK HISTORY (identical pattern to the ASX screeners - see OBV SCREENER.py) ──

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
    today = date.today()
    today_str = today.strftime('%Y-%m-%d')
    for r in results:
        ticker = r['ticker']
        entry = history.get(ticker)
        first_seen = today_str
        if entry:
            try:
                last_seen_date = datetime.strptime(entry['last_seen'], '%Y-%m-%d').date()
                if _weekdays_between(last_seen_date, today) <= gap_days:
                    first_seen = entry['first_seen']
            except Exception:
                pass
        r['first_seen'] = first_seen
        history[ticker] = {'first_seen': first_seen, 'last_seen': today_str}
    return history


# ─── HTML DASHBOARD ────────────────────────────────────────────────────────────

def build_html_report(results, total_scanned, out_path, universe_label, universe_desc):
    from dashboard_template import render_dashboard_html

    def to_card(r):
        earnings_label = r['earnings_date'] if r['earnings_inside_window'] else 'Clear'
        earnings_cls = 'stat-bad' if r['earnings_inside_window'] else 'stat-good'

        trend_label = 'Above 200SMA' if r['above_sma200'] else 'Below 200SMA'
        trend_cls = 'stat-good' if r['above_sma200'] else 'stat-bad'

        pv = r['pct_vs_sma50']
        pullback_cls = 'stat-good' if -8 <= pv <= 2 else 'stat-warn' if -15 <= pv <= 8 else 'stat-bad'

        rsi = r['rsi14']
        if RSI_GOOD_LOW <= rsi <= RSI_GOOD_HIGH:
            rsi_cls = 'stat-good'
        elif rsi < RSI_HARD_OVERSOLD or rsi > 70:
            rsi_cls = 'stat-bad'
        else:
            rsi_cls = 'stat-warn'

        bb = r['bb_pctb']
        bb_cls = 'stat-good' if 0 <= bb <= 0.35 else 'stat-warn' if -0.1 <= bb <= 0.8 else 'stat-bad'

        hv = r['hv_pct'] / 100.0
        hv_cls = 'stat-good' if HV_GOOD_LOW <= hv <= HV_GOOD_HIGH else 'stat-warn'

        return {
            'ticker': r['ticker'],
            'name': r['name'],
            'sector': r['sector'],
            'market_cap': r['market_cap'],
            'price': r['price'],
            'change_1d': r['change_1d'],
            'score': r['score'],
            'first_seen_ts': int(datetime.strptime(r['first_seen'], '%Y-%m-%d').timestamp()),
            'stats': [
                {'label': 'Trend', 'value': trend_label, 'cls': trend_cls},
                {'label': 'vs 50SMA', 'value': f"{pv:+.1f}%", 'cls': pullback_cls},
                {'label': 'RSI(14)', 'value': f"{rsi:.0f}", 'cls': rsi_cls},
                {'label': 'Boll %B', 'value': f"{bb:.2f}", 'cls': bb_cls},
                {'label': 'HV(20d)', 'value': f"{r['hv_pct']:.0f}%", 'cls': hv_cls},
                {'label': 'Earnings', 'value': earnings_label, 'cls': earnings_cls},
            ],
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        total_scanned=total_scanned,
        title=f'🎯 Cash-Secured Put Candidates — {universe_label}',
        subtitle=(
            f"{universe_desc} screened as cash-secured-put CANDIDATES - a liquidity checklist (volume, a real "
            f"200-day history) plus technical-analysis entry timing aimed at a pullback that's likely to hold, "
            f"not a stock still falling: price above its 200-day SMA (long-term uptrend - you need to be fine "
            f"owning this if assigned) but pulled back toward the 50-day SMA, RSI(14) in a real reset "
            f"(not still falling, not overbought), and price near the lower Bollinger Band where pullbacks "
            f"are statistically stretched and premium tends to run richest. This is a ranked candidate list, "
            f"not an executable quote - pull the actual strike/premium from your own broker when you're ready "
            f"to place it."
        ),
        footer_note=(
            f'Cash-Secured Put Candidates ({universe_label}) · Data via Yahoo Finance (yfinance), universe via Wikipedia '
            'S&amp;P 500 list + curated extras<br>'
            f'Criteria: 20d avg volume ≥ 2M (tier 1 ≥ 5M) AND a 200-day price history exists AND realized volatility '
            f'(20d, annualized) is between {HV_HARD_FLOOR*100:.0f}% and {HV_HARD_CEIL*100:.0f}%.<br>'
            'No options chain is read here - this ranks setup quality for a CSP entry, it does not quote a strike '
            'or premium. Not financial advice.'
        ),
        out_path=out_path + '.html',
        tv_prefix='',
        default_mode='table',
    )


def build_csv(results, out_path):
    array_fields = {'dates', 'opens', 'highs', 'lows', 'closes', 'volumes'}
    fieldnames = [k for k in results[0].keys() if k not in array_fields] if results else []
    with open(out_path + '.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow({k: v for k, v in r.items() if k not in array_fields})
    print(f"  📊 CSV export  → {out_path}.csv")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="US Cash-Secured Put Candidate Screener")
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--out', type=str, default='options', help='Output filename prefix - written as <out>-us-csp.html/.csv and <out>-us-etf.html/.csv')
    parser.add_argument('--tickers', type=str, default='', help='Comma-separated ticker override (skips universe fetch)')
    args = parser.parse_args()

    print("=" * 60)
    print("  US CASH-SECURED PUT CANDIDATE SCREENER")
    print("=" * 60)
    print(f"  Checklist: volume ≥2M(5M) · above 200SMA · RSI {RSI_GOOD_LOW}-{RSI_GOOD_HIGH} · "
          f"near lower Bollinger band · HV {HV_GOOD_LOW*100:.0f}-{HV_GOOD_HIGH*100:.0f}% · earnings clear of ~{EARNINGS_LOOKAHEAD_DAYS}d")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
        print(f"  Using custom list: {len(tickers)} tickers")
    else:
        tickers = get_universe()
        print(f"  Universe: {len(tickers)} tickers")

    results = run_scan(tickers, workers=args.workers)

    history = load_seen_history()
    history = apply_streaks(history, results)
    save_seen_history(history)

    results.sort(key=lambda x: x['score'], reverse=True)

    if results:
        print(f"\n{'─'*60}")
        print("  TOP CASH-SECURED-PUT CANDIDATES\n")
        print(f"  {'TICKER':<6} {'SCORE':>5} {'RSI':>5} {'vs50SMA':>8} {'BollB':>6} {'HV%':>6}")
        for r in results[:20]:
            print(f"  {r['ticker']:<6} {r['score']:>5} {r['rsi14']:>5.1f} {r['pct_vs_sma50']:>7.1f}% {r['bb_pctb']:>6.2f} {r['hv_pct']:>5.1f}%")
        if len(results) > 20:
            print(f"  ... and {len(results)-20} more in the report")

    # Separate pages for stocks vs ETFs - a CSP on an ETF (basket, no
    # single-name earnings gap) is a different risk profile than one on a
    # single stock, and worth screening on its own page rather than
    # interleaved.
    stock_results = [r for r in results if not r['is_etf']]
    etf_results = [r for r in results if r['is_etf']]

    out_base = os.path.join(SCRIPT_DIR, args.out)
    # us-csp / us-etf (not just "stocks"/"etfs") so an eventual ASX version
    # of this screener can sit alongside as options-aus-csp / options-aus-etf
    # without reshuffling these US page names.
    for suffix, label, desc, subset in (
        ('us-csp', 'Stocks', 'S&P 500 + curated individual names', stock_results),
        ('us-etf', 'ETFs', 'Sector and index ETFs', etf_results),
    ):
        page_base = f"{out_base}-{suffix}"
        print(f"\n💾 Saving {label} ({len(subset)} qualified) to:\n   {page_base}.html\n   {page_base}.csv")
        try:
            build_html_report(subset, len(tickers), page_base, universe_label=label, universe_desc=desc)
            print(f"  📄 HTML report → {page_base}.html")
        except Exception as e:
            print(f"  ⚠ HTML save error ({label}): {e}")
        try:
            build_csv(subset, page_base)
        except Exception as e:
            print(f"  ⚠ CSV save error ({label}): {e}")

    print("\n" + "=" * 60)
    print("  ✅ Done")
    print("=" * 60)


if __name__ == '__main__':
    main()
