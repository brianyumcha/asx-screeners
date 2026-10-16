"""
US Cash-Secured Put Screener
===========================
Screens US-listed stocks and ETFs for whether they're actually suitable
cash-secured-put ENTRIES (the acquisition leg of a wheel-income strategy,
not the covered-call leg that only exists after assignment) - not just
whether they're a good company. Built directly off Andy Robertson's "Lazy
Formula for Options Income" checklist (see Andy Robertson - Options Trading
Methodology Notes.md): daily volume, options-chain open interest, bid/ask
spread, implied volatility, and whether an earnings report falls inside the
contract window.

For every ticker that clears the basic liquidity bar, this computes a
suggested ~30-delta cash-secured put at the nearest ~30-day expiry - strike
(your acquisition price if assigned), premium, annualized cash-on-cash
return, breakeven, and downside protection to that breakeven - the same
"3%, 30 delta, 30 days" screen Andy applies by hand in his sessions, just
run across a few hundred tickers automatically. A covered-call strike is
still computed per ticker (cheap - same options chain already fetched) and
kept in the CSV for later wheel-continuation reference, but it's not an
"entry" and isn't part of the HTML report's primary stats.

Universe: S&P 500 (scraped from Wikipedia, with a hardcoded fallback) plus
a curated list of names/ETFs Andy specifically discussed that aren't S&P
500 members (PLTR-adjacent small/mid caps, commodity ETFs, etc). Written as
two separate reports - <out>-stocks.html and <out>-etfs.html - since an
ETF put (basket, no single-name earnings gap) is a different risk profile
than a single-stock one.

Delta is NOT returned by yfinance's options chain, so it's approximated
here via Black-Scholes using each contract's own implied volatility - a
standard, broadly-accepted approximation, not the broker's own (proprietary,
model-dependent) number. Good enough to rank/select strikes by; don't treat
the exact decimal as gospel.

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
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_WORKERS   = 12
TARGET_DTE        = 30      # Andy's standard cycle length
MIN_DTE           = 20
MAX_DTE           = 45
TARGET_DELTA      = 0.30    # Andy's standard risk target
# Circuit breaker (full-universe runs only, same spirit as the ASX
# screeners' Yahoo-throttling check): if the options chains came back with
# stale/incomplete data - e.g. the scan ran during US off-market hours,
# before near-the-money strikes have traded that session - the delta-
# closest-strike search gets forced out to far-OTM strikes still carrying
# leftover data, and every result's achieved delta clusters near zero
# instead of near TARGET_DELTA. Found 2026-10-16. A real, fully-populated
# run should land close to 0.30 on both legs for most names.
MIN_MEDIAN_DELTA  = 0.15
RISK_FREE_RATE    = 0.045   # flat assumption for the Black-Scholes delta approximation

MIN_AVG_VOLUME_TIER1 = 5_000_000   # Andy's "tier 1" daily volume floor
MIN_AVG_VOLUME_TIER2 = 2_000_000   # "tier 2" floor - below this, excluded entirely
MIN_OI_TIER1       = 1_000         # open interest at the selected strike
MIN_OI_TIER2       = 500
IV_GOOD_LOW        = 0.30          # Andy's "sweet spot" IV band (30-70%)
IV_GOOD_HIGH       = 0.70
IV_HARD_FLOOR      = 0.10          # below/above this, the data's probably junk/illiquid,
IV_HARD_CEIL       = 2.50          # not a real tradable IV - excluded entirely
CHART_TRIM_BARS    = 260

HISTORY_FILENAME = 'seen_tickers_options.json'
STREAK_GAP_DAYS  = 5

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


# ─── BLACK-SCHOLES DELTA (approximation - see module docstring) ───────────────

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, years, iv, is_call, r=RISK_FREE_RATE):
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * years) / (iv * math.sqrt(years))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


# ─── PER-TICKER SCAN ───────────────────────────────────────────────────────────

def _mid_premium(row):
    bid, ask, last = row.get('bid'), row.get('ask'), row.get('lastPrice')
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and ask > 0 else None
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0, (ask - bid)
    if last and last > 0:
        return last, None
    return None, None


def _pick_expiry(options):
    today = date.today()
    best, best_diff = None, None
    for e in options:
        try:
            d = date.fromisoformat(e)
        except ValueError:
            continue
        dte = (d - today).days
        if dte < MIN_DTE or dte > MAX_DTE:
            continue
        diff = abs(dte - TARGET_DTE)
        if best is None or diff < best_diff:
            best, best_diff = e, diff
    return best


def _pick_strike(chain_df, spot, years, is_call):
    """Among the chain's strikes on one side, pick the one whose
    Black-Scholes delta is closest to TARGET_DELTA (0.30), restricted to
    genuinely out-of-the-money strikes (calls above spot, puts below) -
    mirrors scanning outward from the money the way Andy does by hand."""
    best = None
    best_diff = None
    for _, row in chain_df.iterrows():
        strike = row['strike']
        if is_call and strike <= spot:
            continue
        if not is_call and strike >= spot:
            continue
        iv = row.get('impliedVolatility')
        if iv is None or iv <= IV_HARD_FLOOR or iv >= IV_HARD_CEIL:
            continue
        delta = bs_delta(spot, strike, years, iv, is_call)
        if delta is None:
            continue
        diff = abs(abs(delta) - TARGET_DELTA)
        if best is None or diff < best_diff:
            premium, spread = _mid_premium(row)
            if premium is None:
                continue
            best = {
                'strike': float(strike),
                'delta': round(abs(delta), 3),
                'iv': round(float(iv), 4),
                'premium': round(premium, 2),
                'spread': round(spread, 2) if spread is not None else None,
                'oi': int(row.get('openInterest') or 0),
            }
            best_diff = diff
    return best


def score_ticker(ticker, sector_override=None):
    t = yf.Ticker(ticker)

    hist = t.history(period='1y', auto_adjust=False)
    if hist is None or len(hist) < 60:
        return None
    closes = hist['Close'].tolist()
    price = closes[-1]
    prev_close = closes[-2]
    change_1d = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    avg_volume = hist['Volume'].tail(20).mean()
    if not avg_volume or avg_volume < MIN_AVG_VOLUME_TIER2:
        return None

    try:
        info = t.get_info()
    except Exception:
        info = {}
    is_etf = ticker in ETF_TICKERS or info.get('quoteType') == 'ETF'
    sector = sector_override or info.get('sector') or ('ETF' if is_etf else 'Other')
    market_cap = info.get('marketCap') or (price * (info.get('sharesOutstanding') or 0))
    name = info.get('longName') or info.get('shortName') or ticker

    options = t.options
    if not options:
        return None
    expiry = _pick_expiry(options)
    if not expiry:
        return None
    dte = (date.fromisoformat(expiry) - date.today()).days
    years = dte / 365.0

    try:
        chain = t.option_chain(expiry)
    except Exception:
        return None
    if chain.calls.empty or chain.puts.empty:
        return None

    put = _pick_strike(chain.puts, price, years, is_call=False)
    call = _pick_strike(chain.calls, price, years, is_call=True)
    if not put or not call:
        return None

    put['coc_pct'] = round(put['premium'] * 100 / (put['strike'] * 100) * 100, 2)
    call['coc_pct'] = round(call['premium'] * 100 / (price * 100) * 100, 2)

    # CSP-specific entry metrics - this screener's "entries" are the put leg
    # (selling to acquire stock), not the call leg (that's the second half
    # of the wheel, after assignment) - see module docstring.
    put_annualized_pct = round(put['coc_pct'] * 365.0 / dte, 1)
    put_breakeven = round(put['strike'] - put['premium'], 2)
    put_protection_pct = round((price - put_breakeven) / price * 100, 2)
    cash_required = int(round(put['strike'] * 100))

    # Earnings-clear check: only penalises on a CONFIRMED upcoming date
    # inside the contract window - an unknown/unavailable date gets the
    # benefit of the doubt, same caution Andy applies by hand.
    earnings_date = None
    earnings_inside_window = False
    try:
        ed = t.get_earnings_dates(limit=4)
        if ed is not None and not ed.empty:
            today = date.today()
            expiry_date = date.fromisoformat(expiry)
            for ts in ed.index:
                d = ts.date()
                if today <= d <= expiry_date:
                    earnings_inside_window = True
                    earnings_date = d.strftime('%Y-%m-%d')
                    break
    except Exception:
        pass

    vol_tier = 1 if avg_volume >= MIN_AVG_VOLUME_TIER1 else 2
    min_oi = min(put['oi'], call['oi'])
    oi_tier = 1 if min_oi >= MIN_OI_TIER1 else 2 if min_oi >= MIN_OI_TIER2 else 3
    avg_iv = (put['iv'] + call['iv']) / 2.0
    max_spread_pct = max(
        (put['spread'] / put['premium'] * 100) if put['spread'] and put['premium'] else 0,
        (call['spread'] / call['premium'] * 100) if call['spread'] and call['premium'] else 0,
    )

    score = 100
    score -= {1: 0, 2: 10}[vol_tier]
    score -= {1: 0, 2: 8, 3: 18}[oi_tier]
    if not (IV_GOOD_LOW <= avg_iv <= IV_GOOD_HIGH):
        score -= 8 if (0.20 <= avg_iv <= 0.90) else 16
    if max_spread_pct > 20:
        score -= 18
    elif max_spread_pct > 8:
        score -= 8
    if earnings_inside_window:
        score -= 15
    score = max(0, min(100, score))

    dates = hist.index.strftime('%Y-%m-%d').tolist()[-CHART_TRIM_BARS:]
    opens = hist['Open'].tolist()[-CHART_TRIM_BARS:]
    highs = hist['High'].tolist()[-CHART_TRIM_BARS:]
    lows = hist['Low'].tolist()[-CHART_TRIM_BARS:]
    closes_trim = closes[-CHART_TRIM_BARS:]
    volumes = hist['Volume'].tolist()[-CHART_TRIM_BARS:]

    return {
        'ticker': ticker,
        'name': name,
        'is_etf': is_etf,
        'sector': sector,
        'market_cap': int(market_cap) if market_cap else 0,
        'price': round(price, 2),
        'change_1d': round(change_1d, 2),
        'avg_volume': int(avg_volume),
        'vol_tier': vol_tier,
        'expiry': expiry,
        'dte': dte,
        'avg_iv_pct': round(avg_iv * 100, 1),
        'put_strike': put['strike'], 'put_delta': put['delta'], 'put_premium': put['premium'],
        'put_oi': put['oi'], 'put_coc_pct': put['coc_pct'],
        'put_annualized_pct': put_annualized_pct, 'put_breakeven': put_breakeven,
        'put_protection_pct': put_protection_pct, 'cash_required': cash_required,
        'call_strike': call['strike'], 'call_delta': call['delta'], 'call_premium': call['premium'],
        'call_oi': call['oi'], 'call_coc_pct': call['coc_pct'],
        'earnings_date': earnings_date, 'earnings_inside_window': earnings_inside_window,
        'score': score,
        'dates': dates, 'opens': opens, 'highs': highs, 'lows': lows,
        'closes': closes_trim, 'volumes': volumes,
    }


def run_scan(tickers, workers=DEFAULT_WORKERS):
    total = len(tickers)
    t0 = time.time()
    results, failed, done = [], 0, 0

    print(f"\n🔍 Scoring {total} US tickers for options-income suitability | {workers} threads\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(score_ticker, t): t for t in tickers}
        for fut in as_completed(futures):
            done += 1
            tick = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    flag = f"  ✅ {tick:6s} score={res['score']:3d} IV={res['avg_iv_pct']:5.1f}%"
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

        avg_iv = r['avg_iv_pct'] / 100.0
        if IV_GOOD_LOW <= avg_iv <= IV_GOOD_HIGH:
            iv_cls = 'stat-good'
        elif 0.20 <= avg_iv <= 0.90:
            iv_cls = 'stat-warn'
        else:
            iv_cls = 'stat-bad'

        if r['put_oi'] >= MIN_OI_TIER1:
            oi_cls = 'stat-good'
        elif r['put_oi'] >= MIN_OI_TIER2:
            oi_cls = 'stat-warn'
        else:
            oi_cls = 'stat-bad'

        ann = r['put_annualized_pct']
        ann_cls = 'stat-good' if ann >= 25 else 'stat-warn' if ann >= 12 else 'stat-bad'

        prot = r['put_protection_pct']
        prot_cls = 'stat-good' if prot >= 10 else 'stat-warn' if prot >= 5 else 'stat-bad'

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
                {'label': 'IV', 'value': f"{r['avg_iv_pct']:.0f}%", 'cls': iv_cls},
                {'label': 'Strike', 'value': f"${r['put_strike']:.0f} ({r['put_delta']:.2f}Δ)"},
                {'label': 'Premium', 'value': f"${r['put_premium']:.2f}"},
                {'label': 'Annualized', 'value': f"{ann:.1f}%", 'cls': ann_cls},
                {'label': 'Breakeven', 'value': f"${r['put_breakeven']:.2f}"},
                {'label': 'Protection', 'value': f"{prot:.1f}%", 'cls': prot_cls},
                {'label': 'Put OI', 'value': f"{r['put_oi']:,}", 'cls': oi_cls},
                {'label': 'Earnings', 'value': earnings_label, 'cls': earnings_cls},
            ],
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        total_scanned=total_scanned,
        title=f'🎯 Cash-Secured Put Screener — {universe_label}',
        subtitle=(
            f"{universe_desc} screened for cash-secured-put ENTRIES against Andy Robertson's checklist — "
            f"liquidity, open interest, IV, and a clear earnings runway — with a suggested ~{int(TARGET_DELTA*100)}-delta, "
            f"~{TARGET_DTE}-day put: strike (= your acquisition price if assigned), premium, annualized return, "
            f"breakeven, and downside protection to that breakeven. Not a covered-call screen — that's the "
            f"second half of the wheel, after assignment, not an entry."
        ),
        footer_note=(
            f'Cash-Secured Put Screener ({universe_label}) · Data via Yahoo Finance (yfinance), universe via Wikipedia '
            'S&amp;P 500 list + curated extras<br>'
            f'Criteria: 20d avg volume ≥ 2M (tier 1 ≥ 5M) AND a ~{MIN_DTE}-{MAX_DTE}d options chain exists AND a usable '
            f'~{int(TARGET_DELTA*100)}-delta put strike exists.<br>'
            'Delta is approximated via Black-Scholes from each contract\'s own implied volatility (yfinance does not supply '
            'broker-calculated delta) — a reasonable estimate, not a guarantee. Not financial advice.'
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
    parser = argparse.ArgumentParser(description="US Cash-Secured Put Screener")
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--out', type=str, default='options', help='Output filename prefix - written as <out>-us-csp.html/.csv and <out>-us-etf.html/.csv')
    parser.add_argument('--tickers', type=str, default='', help='Comma-separated ticker override (skips universe fetch)')
    args = parser.parse_args()

    print("=" * 60)
    print("  US CASH-SECURED PUT SCREENER")
    print("=" * 60)
    print(f"  Checklist: volume ≥2M(5M) · OI ≥500(1000) · IV 30-70% · earnings clear of ~{TARGET_DTE}d window")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
        print(f"  Using custom list: {len(tickers)} tickers")
    else:
        tickers = get_universe()
        print(f"  Universe: {len(tickers)} tickers")

    results = run_scan(tickers, workers=args.workers)

    if not args.tickers and results:
        all_deltas = [r['put_delta'] for r in results] + [r['call_delta'] for r in results]
        median_delta = statistics.median(all_deltas)
        if median_delta < MIN_MEDIAN_DELTA:
            print(f"\n❌ Median achieved delta across {len(results)} qualified results is only "
                  f"{median_delta:.3f} (target ~{TARGET_DELTA}) - likely stale/incomplete options-chain "
                  f"data (e.g. this ran during US off-market hours), not a real market condition. "
                  f"Aborting WITHOUT writing a report, so the last good one stays live.")
            sys.exit(1)

    history = load_seen_history()
    history = apply_streaks(history, results)
    save_seen_history(history)

    results.sort(key=lambda x: x['score'], reverse=True)

    if results:
        print(f"\n{'─'*60}")
        print("  TOP CASH-SECURED-PUT CANDIDATES\n")
        print(f"  {'TICKER':<6} {'SCORE':>5} {'IV%':>6} {'ANN%':>7} {'PROTECT%':>9}")
        for r in results[:20]:
            print(f"  {r['ticker']:<6} {r['score']:>5} {r['avg_iv_pct']:>5.1f}% {r['put_annualized_pct']:>6.1f}% {r['put_protection_pct']:>8.1f}%")
        if len(results) > 20:
            print(f"  ... and {len(results)-20} more in the report")

    # Separate pages for stocks vs ETFs - a 30-delta put on an ETF (basket,
    # diversified, no single-name earnings gap) is a different risk profile
    # than one on a single stock, and worth screening on its own page rather
    # than interleaved.
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
