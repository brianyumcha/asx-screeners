"""
ASX Higher-High Screener (Sector Grid)
==============================
Python/HTML port of the "HH Indicator - ASX <sector> (BT)" family of
TradingView Pine scripts - one table per sector, every ticker shown, "NEW HH"
when the sector's structural-breakout pattern fires.

Signal logic (matches the FIXED Pine version, applied to Materials on
TradingView 2026-09-01 - see that script's changelog for the full writeup):
  - A pivot high is a 7-bar window (3 left, 3 right) local max of the candle
    BODY top (max(open,close), ignoring wicks), confirmed 3 bars after it forms.
  - The trigger fires when price closes back above the last confirmed pivot
    high (a structural break of the last swing high).
  - FIX (the bug Brian found on 2026-09-01): the same pivot could otherwise
    fire twice if price dipped back under it and re-crossed before a new
    pivot had time to confirm. This is guarded by remembering which pivot
    (by bar index, not price) has already fired - so a genuine later retest
    of the same price at a NEW pivot can still fire again, but a stale
    unconfirmed re-cross of the same pivot cannot.

This does NOT try to be a full Elliott Wave / market-structure validator -
same philosophy as the other two screeners: it's a mechanical, reliable
signal, not a claim of certainty about the wave count.

New relative to the Pine version:
  - Covers the FULL ASX (~2000+ tickers via SeaBee), not a hand-picked 40 per
    sector - grouped into the same 8 broad sectors as your TradingView setup.
  - Daily AND Weekly HH status computed for every ticker (switch in the UI).
  - An OBV confirmation column: whether On-Balance Volume is also at/near its
    own high right now (confirming the move), already exceeded its prior
    peak before price did (leading), or is lagging price's new high
    (a volume non-confirmation - the classic bearish-divergence-on-a-new-high
    warning sign).

Usage:
    python "HH SCREENER.py"                  # full ASX scan, daily + weekly
    python "HH SCREENER.py" --tickers BHP,RIO,FMG
    python "HH SCREENER.py" --workers 20

Requirements:
    pip install yfinance pandas requests openpyxl pdfplumber
"""

import argparse
import io
import json
import os
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
import openpyxl
import pdfplumber

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_WORKERS = 15
HISTORY_PERIOD  = "3y"   # daily bars; weekly bars are resampled from this
CHART_TRIM_BARS = 130    # trailing daily bars embedded for the chart-view mini charts (~6M)
MIN_PRICE       = 0.05
MIN_MARKET_CAP  = 20_000_000
MIN_AVG_VOLUME  = 20_000
RATE_LIMIT_SLEEP = 0.05

PIVOT_LEFT  = 3
PIVOT_RIGHT = 3

# SeaBee's `industry` field (GICS industry-group level) mapped onto the same
# 8 broad sector buckets used in the "HH Indicator ... (BT)" Pine scripts, so
# this tool groups stocks the same way your TradingView dashboard already does.
SECTOR_MAP = {
    "Materials": "Materials",
    "Energy": "Energy",
    "Software & Services": "Info Tech",
    "Technology Hardware & Equipment": "Info Tech",
    "Semiconductors & Semiconductor Equipment": "Info Tech",
    "Financial Services": "Financials",
    "Banks": "Financials",
    "Insurance": "Financials",
    "Pharmaceuticals, Biotechnology & Life Sciences": "Healthcare",
    "Health Care Equipment & Services": "Healthcare",
    "Capital Goods": "Property & Industrials",
    "Commercial & Professional Services": "Property & Industrials",
    "Transportation": "Property & Industrials",
    "Equity Real Estate Investment Trusts (REITs)": "Property & Industrials",
    "Real Estate Management & Development": "Property & Industrials",
    "Consumer Services": "Consumer Discretionary",
    "Media & Entertainment": "Essentials",
    "Consumer Discretionary Distribution & Retail": "Consumer Discretionary",
    "Consumer Durables & Apparel": "Consumer Discretionary",
    "Automobiles & Components": "Consumer Discretionary",
    "Food, Beverage & Tobacco": "Essentials",
    "Household & Personal Products": "Essentials",
    "Consumer Staples Distribution & Retail": "Essentials",
    "Utilities": "Essentials",
    "Telecommunication Services": "Essentials",
}
SECTOR_ORDER = [
    "Materials", "Energy", "Financials", "Healthcare", "Info Tech",
    "Consumer Discretionary", "Essentials", "Property & Industrials", "Other",
]

# ─── TICKER UNIVERSE (SeaBee gives us industry + market cap in one call) ──────

def get_asx_universe():
    """
    Returns {ticker: {'industry': str, 'sector': str, 'market_cap': int, 'name': str}}
    for the full ASX, sourced from SeaBee. Falls back to a bare ticker list
    (no sector data - everything lands in "Other") if SeaBee is unreachable.
    """
    print("📋 Fetching ASX universe (tickers + industry) from SeaBee...")
    try:
        api_url = "https://marketdata.seabee.me/api.php?action=asx_companies_list"
        headers = {"X-API-Key": "deeznuts"}
        r = requests.get(api_url, headers=headers, timeout=20)
        if r.status_code == 200:
            json_resp = r.json()
            if json_resp.get("success") and "data" in json_resp and "companies" in json_resp["data"]:
                companies = json_resp["data"]["companies"]
                universe = {}
                for t, info in companies.items():
                    t = str(t).strip().upper()
                    if not (1 <= len(t) <= 5) or not t.isalnum():
                        continue
                    industry = info.get("industry") or "Other"
                    universe[t] = {
                        "industry": industry,
                        "sector": SECTOR_MAP.get(industry, "Other"),
                        "market_cap": info.get("market_cap") or 0,
                        "name": info.get("name") or t,
                    }
                if len(universe) > 500:
                    print(f"  ✓ Fetched {len(universe)} tickers with industry data from SeaBee")
                    return universe
            print("  ⚠ SeaBee API returned an unexpected JSON structure.")
        else:
            print(f"  ⚠ SeaBee API returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ SeaBee API fetch failed: {e}")

    print("  ⚠ Falling back to a bare ticker list with no sector data (everything -> 'Other')")
    # Minimal built-in fallback so the script still runs if SeaBee is down.
    fallback = "BHP,RIO,FMG,CBA,NAB,WBC,ANZ,CSL,WES,WOW,TLS,STO,ORG,WDS,XRO,WTC".split(",")
    return {t: {"industry": "Other", "sector": "Other", "market_cap": 0, "name": t} for t in fallback}


# ─── INDICATORS ───────────────────────────────────────────────────────────────

def calc_obv_series(closes, volumes):
    obv = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def hh_signal(opens, highs, lows, closes):
    """
    Port of the FIXED Pine "HH Indicator (BT)" logic. Returns
    (signal_on_last_bar: bool, last_pivot_price: float|None, last_pivot_idx: int|None).

    tops/btms use the candle BODY (max/min of open,close), matching the Pine
    script exactly - wicks don't count toward pivot formation there.
    """
    n = len(closes)
    L, R = PIVOT_LEFT, PIVOT_RIGHT
    window = L + R + 1
    if n < window + 2:
        return False, None, None

    tops = [max(opens[i], closes[i]) for i in range(n)]

    last_ph_price = None
    last_ph_idx = None
    last_triggered_idx = None
    last_signal = False

    # bar index of the pivot CANDIDATE at position i is (i - R), mirroring
    # Pine's top[3] (3 bars back from the bar currently being evaluated)
    for i in range(window - 1, n):
        cand_idx = i - R
        cand_top = tops[cand_idx]
        is_ph = all(cand_top >= tops[cand_idx + off] for off in range(-L, R + 1))

        if is_ph:
            last_ph_price = cand_top
            last_ph_idx = cand_idx

        raw_cross = (
            last_ph_price is not None
            and i > 0
            and closes[i - 1] <= last_ph_price
            and closes[i] > last_ph_price
        )
        is_new_pivot = last_ph_idx is not None and last_ph_idx != last_triggered_idx
        signal = bool(raw_cross and is_new_pivot)

        if signal:
            last_triggered_idx = last_ph_idx

        last_signal = signal  # only the FINAL bar's value is what we report

    return last_signal, last_ph_price, last_ph_idx


def obv_confirmation(obv, closes, pivot_idx):
    """
    Classify whether OBV is backing up the current price high, relative to
    OBV's own value at the reference pivot (the same swing high price just
    broke above).
      - No signal / no pivot reference -> None (blank in the UI)
      - OBV now clearly above its value at the pivot -> "Confirming"
      - OBV now clearly below its value at the pivot -> "Not confirming"
      - Roughly flat -> "Neutral"
    """
    if pivot_idx is None or pivot_idx >= len(obv):
        return None
    obv_now = obv[-1]
    obv_then = obv[pivot_idx]
    span = max(abs(obv_now), abs(obv_then), 1)
    diff_pct = (obv_now - obv_then) / span
    if diff_pct > 0.03:
        return "Confirming"
    if diff_pct < -0.03:
        return "Not confirming"
    return "Neutral"


def resample_weekly(dates, opens, highs, lows, closes, volumes):
    df = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes}, index=pd.to_datetime(dates))
    wk = df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return wk


# ─── ANALYSE A SINGLE STOCK ───────────────────────────────────────────────────

def analyse_ticker(ticker_raw, info):
    sym = ticker_raw.upper().strip()
    yahoo_sym = sym if sym.endswith(".AX") else sym + ".AX"

    try:
        market_cap = info.get("market_cap") or 0
        if market_cap and market_cap < MIN_MARKET_CAP:
            return None

        ticker_obj = yf.Ticker(yahoo_sym)
        # auto_adjust=False deliberately: adjusted prices retroactively lower
        # every historical bar before an ex-dividend date to make them
        # comparable to today, which can make a pivot level look "broken" by
        # today's raw price when it never actually was (confirmed on ANN,
        # 2026-09-01 - see the fix note in this script's version history).
        # TradingView's own chart/Pine data uses raw prices by default, so
        # this also matches what the live "HH Indicator" dashboard shows.
        df = ticker_obj.history(period=HISTORY_PERIOD, interval="1d", auto_adjust=False)
        if df.empty or len(df) < 40:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 40:
            return None

        closes = df["Close"].tolist()
        opens = df["Open"].tolist()
        highs = df["High"].tolist()
        lows = df["Low"].tolist()
        volumes = df["Volume"].fillna(0).tolist()
        dates = list(df.index)

        price = closes[-1]
        if price < MIN_PRICE:
            return None
        avg_vol = sum(volumes[-30:]) / min(30, len(volumes))
        if avg_vol < MIN_AVG_VOLUME:
            return None

        prev_close = closes[-2]
        change_1d = (price - prev_close) / prev_close * 100

        # --- Daily ---
        sig_d, piv_price_d, piv_idx_d = hh_signal(opens, highs, lows, closes)
        obv_d = calc_obv_series(closes, volumes)
        obv_conf_d = obv_confirmation(obv_d, closes, piv_idx_d) if sig_d else None

        # --- Weekly (resampled from the same daily data) ---
        wk = resample_weekly(dates, opens, highs, lows, closes, volumes)
        sig_w, piv_price_w, piv_idx_w = (False, None, None)
        obv_conf_w = None
        if len(wk) >= 12:
            wo, wh, wl, wc, wv = wk["Open"].tolist(), wk["High"].tolist(), wk["Low"].tolist(), wk["Close"].tolist(), wk["Volume"].tolist()
            sig_w, piv_price_w, piv_idx_w = hh_signal(wo, wh, wl, wc)
            if sig_w:
                obv_w = calc_obv_series(wc, wv)
                obv_conf_w = obv_confirmation(obv_w, wc, piv_idx_w)

        result = {
            "ticker": sym,
            "name": info.get("name", sym),
            "sector": info.get("sector", "Other"),
            "industry": info.get("industry", "Other"),
            "market_cap": int(market_cap) if market_cap else 0,
            "price": round(price, 4),
            "change_1d": round(change_1d, 2),
            "hh_daily": bool(sig_d),
            "hh_weekly": bool(sig_w),
            "obv_daily": obv_conf_d,
            "obv_weekly": obv_conf_w,
        }

        # Daily OHLCV for the chart-view mini candlesticks - only embedded for
        # tickers with a live signal (daily or weekly), to keep the page size
        # sane across a ~1000-ticker universe. Table view doesn't need this at
        # all (it's numbers/text only), so non-signal tickers carry none of it.
        if sig_d or sig_w:
            trim = slice(-CHART_TRIM_BARS, None)
            result.update({
                "dates": [d.strftime("%Y-%m-%d") for d in dates[trim]],
                "opens": [round(v, 4) for v in opens[trim]],
                "highs": [round(v, 4) for v in highs[trim]],
                "lows": [round(v, 4) for v in lows[trim]],
                "closes": [round(v, 4) for v in closes[trim]],
            })

        return result

    except Exception:
        return None


# ─── SCAN ─────────────────────────────────────────────────────────────────────

def run_scan(universe, workers=DEFAULT_WORKERS):
    tickers = list(universe.keys())
    total = len(tickers)
    results = []
    failed = 0
    done = 0
    t0 = time.time()

    print(f"\n🔍 Scanning {total} ASX tickers for Higher-High signals (daily + weekly) | {workers} threads\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyse_ticker, t, universe[t]): t for t in tickers}
        for fut in as_completed(futures):
            done += 1
            tick = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
            except Exception:
                failed += 1

            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            sys.stdout.write(f"\r  [{done:5d}/{total}]  {rate:4.1f}/s  ETA {eta:5.0f}s  Included: {len(results):4d}   ")
            sys.stdout.flush()
            if done % 100 == 0:
                time.sleep(RATE_LIMIT_SLEEP)

    print(f"\n\n✅ Scan complete in {time.time()-t0:.1f}s")
    print(f"   Scanned: {total}  |  Included (passed liquidity filters): {len(results)}  |  Failed: {failed}")
    return results


# ─── HTML REPORT (sector-grouped table, matches the Pine dashboard layout) ────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ASX Higher-High Screener</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Syne:wght@700;800&display=swap');
:root {
  --bg:#0a0c0f; --surface:#111418; --border:#1e2530;
  --accent:#00e5a0; --accent2:#00aaff; --warn:#ffb800; --danger:#ff4455;
  --text:#e8edf2; --muted:#5a6478; --card:#141820;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  font-variant-numeric:tabular-nums;padding:1.6rem;}
.wrap{max-width:1400px;margin:0 auto}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.3rem;flex-wrap:wrap;gap:1rem}
h1{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;letter-spacing:-.03em;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.subtitle{font-size:.78rem;color:var(--muted);margin-top:.3rem}
.session{font-size:.68rem;color:var(--muted);margin:.6rem 0 1.1rem}
.reportnav{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-family:'Inter',sans-serif;font-size:.72rem;padding:.5rem .8rem;border-radius:6px;
  cursor:pointer;outline:none;height:fit-content}
.reportnav:hover{border-color:var(--accent2)}
.notice{background:rgba(255,184,0,.05);border:1px solid rgba(255,184,0,.15);border-radius:4px;
  padding:.7rem .9rem;font-size:.68rem;color:var(--warn);margin-bottom:1rem;line-height:1.6}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:1.2rem}
.pillgroup{display:flex;gap:.3rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.2rem}
.pill{background:transparent;border:none;color:var(--muted);font-size:.72rem;
  padding:.4rem .8rem;border-radius:6px;cursor:pointer;white-space:nowrap}
.pill:hover{color:var(--text)}
.pill.active{background:var(--accent2);color:#04121a;font-weight:600}
input[type=text]{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.75rem;padding:.5rem .8rem;border-radius:8px;outline:none;width:170px}
.checkline{display:flex;align-items:center;gap:.4rem;font-size:.72rem;color:var(--muted);cursor:pointer}
.sector{margin-bottom:1.4rem;border:1px solid var(--border);border-radius:10px;overflow:hidden}
.sector-head{display:flex;justify-content:space-between;align-items:center;padding:.7rem 1rem;
  background:var(--surface);cursor:pointer;user-select:none}
.sector-head h2{font-family:'Syne',sans-serif;font-size:.95rem;font-weight:700}
.sector-head .count{font-size:.68rem;color:var(--muted)}
table.datatable{width:100%;border-collapse:collapse;font-size:.78rem}
table.datatable thead tr{border-bottom:1px solid var(--border)}
table.datatable th{text-align:left;padding:.5rem .8rem;font-size:.62rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;white-space:nowrap;font-weight:600}
table.datatable tbody tr{border-bottom:1px solid rgba(30,37,48,.6)}
table.datatable tbody tr:hover{background:rgba(0,229,160,.03)}
table.datatable td{padding:.55rem .8rem;vertical-align:middle;white-space:nowrap}
td.ticker-cell{font-family:'Syne',sans-serif;font-weight:700}
td.ticker-cell a{color:var(--accent2);text-decoration:none}
.up{color:var(--accent)} .dn{color:var(--danger)} .neutral{color:var(--muted)}
.hh-yes{background:rgba(0,229,160,.14);color:var(--accent);font-weight:700;padding:.2rem .6rem;border-radius:4px;display:inline-block}
.hh-no{color:var(--muted)}
.obv-confirm{color:var(--accent)} .obv-not{color:var(--danger)} .obv-neutral{color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:2rem 0;font-size:.85rem}
footer{margin-top:2rem;font-size:.62rem;color:var(--muted);border-top:1px solid var(--border);padding-top:1rem}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}
.sectionhead{grid-column:1/-1;font-family:'Syne',sans-serif;font-size:.85rem;font-weight:700;
  color:var(--text);margin:1.4rem 0 .2rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.sectionhead:first-child{margin-top:0}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.9rem;
  display:flex;flex-direction:column;gap:.5rem}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start}
.cardhead-left{display:flex;align-items:baseline;gap:.4rem}
.ticker{font-family:'Syne',sans-serif;font-weight:700;font-size:1.02rem;color:var(--text)}
.ticker a{color:inherit;text-decoration:none}
.ticker a:hover{color:var(--accent2)}
.chg{font-size:.72rem;font-weight:600}
.chartwrap{position:relative;width:100%;height:150px}
canvas{width:100%;height:100%;display:block}
.cardfoot{display:flex;justify-content:space-between;font-size:.68rem;color:var(--muted);
  border-top:1px solid var(--border);padding-top:.5rem}
.cardfoot .v{color:var(--text)}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>ASX HIGHER-HIGH SCREENER</h1>
      <div class="subtitle">Structural breakout of the last swing high, by sector — port of the "HH Indicator (BT)" TradingView scripts, full ASX universe.</div>
    </div>
    <select class="reportnav" id="reportNav" onchange="if(this.value) location.href=this.value">
      <option value="pre-breakout.html">📈 Pre-Breakout (OBV)</option>
      <option value="pullback.html">↩️ Pullback (Zag Zone)</option>
      <option value="higher-high.html">⬆️ Higher-High</option>
    </select>
  </div>
  <div class="session">##SESSION_LINE##</div>

  <div class="notice">⚠ Static report from a single scan run — not live. The wave/structure validity is NOT auto-verified. Not financial advice.</div>

  <div class="controls">
    <div class="pillgroup" id="modeToggle">
      <button class="pill" data-mode="chart">📊 Charts</button>
      <button class="pill active" data-mode="table">☰ Table</button>
    </div>
    <div class="pillgroup" id="tfToggle">
      <button class="pill active" data-tf="daily">Daily</button>
      <button class="pill" data-tf="weekly">Weekly</button>
    </div>
    <div class="pillgroup" id="chartTfToggle" style="display:none">
      <button class="pill active" data-ctf="21">1M</button>
      <button class="pill" data-ctf="63">3M</button>
      <button class="pill" data-ctf="126">6M</button>
    </div>
    <input type="text" id="search" placeholder="Search ticker...">
    <label class="checkline"><input type="checkbox" id="onlySignals" checked> Only show NEW HH</label>
  </div>

  <div id="sectors"></div>
  <div id="grid" class="grid" style="display:none"></div>
  <footer>##FOOTER_NOTE##</footer>
</div>

<script>
document.getElementById('reportNav').value = location.pathname.split('/').pop() || 'higher-high.html';

const DATA = ##DATA_JSON##;
const SECTOR_ORDER = ##SECTOR_ORDER_JSON##;

let state = { mode: 'table', tf: 'daily', chartTf: 21, search: '', onlySignals: true };

document.getElementById('modeToggle').addEventListener('click', e => {
  if (!e.target.dataset.mode) return;
  state.mode = e.target.dataset.mode;
  document.querySelectorAll('#modeToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  document.getElementById('chartTfToggle').style.display = state.mode === 'chart' ? '' : 'none';
  render();
});
document.getElementById('tfToggle').addEventListener('click', e => {
  if (!e.target.dataset.tf) return;
  state.tf = e.target.dataset.tf;
  document.querySelectorAll('#tfToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('chartTfToggle').addEventListener('click', e => {
  if (!e.target.dataset.ctf) return;
  state.chartTf = parseInt(e.target.dataset.ctf, 10);
  document.querySelectorAll('#chartTfToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('search').addEventListener('input', e => { state.search = e.target.value.toUpperCase(); render(); });
document.getElementById('onlySignals').addEventListener('change', e => { state.onlySignals = e.target.checked; render(); });

function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function sma(closes, period) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function drawChart(canvas, r, tf) {
  const n = r.closes.length;
  const start = Math.max(0, n - tf);
  const closes = r.closes.slice(start), opens = r.opens.slice(start),
        highs = r.highs.slice(start), lows = r.lows.slice(start);
  if (closes.length < 2) return;

  const sma20 = sma(r.closes, 20).slice(start);
  const sma50 = sma(r.closes, 50).slice(start);

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const allVals = [...highs, ...lows, ...sma20.filter(v=>v!=null), ...sma50.filter(v=>v!=null)];
  const lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = (hi - lo) * 0.08 || 1;
  const yMin = lo - pad, yMax = hi + pad;
  const y = v => H - ((v - yMin) / (yMax - yMin)) * H;
  const n2 = closes.length;
  const cw = W / n2;
  const x = i => i * cw + cw / 2;

  function line(series, color, dashed) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.setLineDash(dashed ? [3, 3] : []);
    let started = false;
    for (let i = 0; i < series.length; i++) {
      if (series[i] == null) continue;
      const px = x(i), py = y(series[i]);
      if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  line(sma50, 'rgba(180,190,200,0.55)', true);
  line(sma20, 'rgba(0,170,255,0.85)', false);

  for (let i = 0; i < n2; i++) {
    const up = closes[i] >= opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? '#00e5a0' : '#ff4455';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(i), y(highs[i]));
    ctx.lineTo(x(i), y(lows[i]));
    ctx.stroke();
    const bodyTop = y(Math.max(opens[i], closes[i]));
    const bodyBot = y(Math.min(opens[i], closes[i]));
    const bw = Math.max(1, cw * 0.6);
    ctx.fillRect(x(i) - bw / 2, bodyTop, bw, Math.max(1, bodyBot - bodyTop));
  }

  const lastPrice = closes[closes.length - 1];
  const up = r.change_1d >= 0;
  ctx.font = '600 10px Inter, sans-serif';
  const label = lastPrice.toFixed(lastPrice < 1 ? 3 : 2);
  const tw = ctx.measureText(label).width;
  const bx = W - tw - 10, by = y(lastPrice);
  ctx.fillStyle = up ? 'rgba(0,229,160,0.9)' : 'rgba(255,68,85,0.9)';
  ctx.fillRect(bx - 4, by - 8, tw + 8, 16);
  ctx.fillStyle = '#04121a';
  ctx.fillText(label, bx, by + 3);
}

function cardHtml(r) {
  const chgClass = r.change_1d > 0 ? 'up' : r.change_1d < 0 ? 'dn' : 'neutral';
  const chgSign = r.change_1d > 0 ? '+' : '';
  const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${r.ticker}`;
  return `<div class="card" data-ticker="${esc(r.ticker)}">
    <div class="cardhead">
      <div class="cardhead-left">
        <span class="ticker"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(r.ticker)} ↗</a></span>
        <span class="chg ${chgClass}">${chgSign}${r.change_1d.toFixed(1)}%</span>
      </div>
      <span class="hh-yes">NEW HH</span>
    </div>
    <div class="chartwrap"><canvas></canvas></div>
    <div class="cardfoot"><span>${esc(r.industry)}</span><span class="v">$${r.price.toFixed(r.price < 1 ? 3 : 2)}</span></div>
  </div>`;
}

function renderTable(visible, sigKey, obvKey) {
  const bySector = {};
  visible.forEach(r => { (bySector[r.sector] = bySector[r.sector] || []).push(r); });

  const sectors = SECTOR_ORDER.filter(s => bySector[s] && bySector[s].length);
  const container = document.getElementById('sectors');

  if (sectors.length === 0) {
    container.innerHTML = '<div class="empty">No stocks match the current filters.</div>';
    return;
  }

  container.innerHTML = sectors.map(sec => {
    const list = [...bySector[sec]].sort((a, b) => a.ticker.localeCompare(b.ticker));
    const rows = list.map(r => {
      const chgClass = r.change_1d > 0 ? 'up' : r.change_1d < 0 ? 'dn' : 'neutral';
      const chgSign = r.change_1d > 0 ? '+' : '';
      const sig = r[sigKey];
      const obv = r[obvKey];
      const obvClass = obv === 'Confirming' ? 'obv-confirm' : obv === 'Not confirming' ? 'obv-not' : 'obv-neutral';
      const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${r.ticker}`;
      return `<tr>
        <td class="ticker-cell"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(r.ticker)}</a></td>
        <td style="color:var(--muted);font-size:.72rem">${esc(r.industry)}</td>
        <td>$${r.price.toFixed(r.price < 1 ? 3 : 2)}</td>
        <td class="${chgClass}">${chgSign}${r.change_1d.toFixed(1)}%</td>
        <td>${sig ? '<span class="hh-yes">NEW HH</span>' : '<span class="hh-no">-</span>'}</td>
        <td class="${obvClass}">${obv ? esc(obv) : '-'}</td>
      </tr>`;
    }).join('');
    return `<div class="sector">
      <div class="sector-head"><h2>${esc(sec)}</h2><span class="count">${list.length} stocks</span></div>
      <table class="datatable">
        <thead><tr><th>Ticker</th><th>Industry</th><th>Price</th><th>1D Chg</th><th>Signal</th><th>OBV</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }).join('');
}

function renderCharts(visible, sigKey) {
  const list = visible.filter(r => r[sigKey] && r.dates);
  const bySector = {};
  list.forEach(r => { (bySector[r.sector] = bySector[r.sector] || []).push(r); });
  const sectors = SECTOR_ORDER.filter(s => bySector[s] && bySector[s].length);
  const grid = document.getElementById('grid');

  if (sectors.length === 0) {
    grid.innerHTML = '<div class="empty">No stocks match the current filters.</div>';
    return;
  }

  let html = '';
  sectors.forEach(sec => {
    const secList = [...bySector[sec]].sort((a, b) => a.ticker.localeCompare(b.ticker));
    html += `<div class="sectionhead">${esc(sec)} (${secList.length})</div>` + secList.map(cardHtml).join('');
  });
  grid.innerHTML = html;

  grid.querySelectorAll('.card').forEach(el => {
    const r = DATA.find(d => d.ticker === el.dataset.ticker);
    const canvas = el.querySelector('canvas');
    requestAnimationFrame(() => drawChart(canvas, r, state.chartTf));
  });
}

function render() {
  const sigKey = state.tf === 'daily' ? 'hh_daily' : 'hh_weekly';
  const obvKey = state.tf === 'daily' ? 'obv_daily' : 'obv_weekly';

  let visible = DATA.filter(r => !state.search || r.ticker.includes(state.search));
  if (state.onlySignals || state.mode === 'chart') visible = visible.filter(r => r[sigKey]);

  const sectorsEl = document.getElementById('sectors');
  const gridEl = document.getElementById('grid');

  if (state.mode === 'chart') {
    sectorsEl.style.display = 'none';
    gridEl.style.display = '';
    renderCharts(visible, sigKey);
  } else {
    gridEl.style.display = 'none';
    sectorsEl.style.display = '';
    renderTable(visible, sigKey, obvKey);
  }
}
render();
</script>
</body>
</html>"""


def build_html_report(results, total_scanned, out_path):
    now = datetime.now()
    n_daily = sum(1 for r in results if r['hh_daily'])
    n_weekly = sum(1 for r in results if r['hh_weekly'])
    session_line = (
        f"Session {now.strftime('%Y-%m-%d %H:%M')} · {total_scanned} scanned · "
        f"{n_daily} daily NEW HH · {n_weekly} weekly NEW HH"
    )
    html = HTML_TEMPLATE
    html = html.replace('##SESSION_LINE##', session_line)
    html = html.replace('##FOOTER_NOTE##',
        'ASX Higher-High Screener · Data via Yahoo Finance (yfinance), universe + industry via SeaBee · '
        'Pivot: 3-left/3-right body-top pivot, confirmed 3 bars after forming. Signal: close crosses back above the last confirmed pivot high, '
        'one-shot-per-pivot guarded (see script docstring for the bug this fixes vs. the original Pine version).')
    html = html.replace('##SECTOR_ORDER_JSON##', json.dumps(SECTOR_ORDER))
    html = html.replace('##DATA_JSON##', json.dumps(results))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  📄 HTML report → {out_path}")


def build_csv(results, out_path):
    import csv
    fieldnames = ['ticker', 'name', 'sector', 'industry', 'market_cap', 'price', 'change_1d',
                  'hh_daily', 'hh_weekly', 'obv_daily', 'obv_weekly']
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"  📊 CSV export  → {out_path}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ASX Higher-High Screener (sector grid)')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--tickers', type=str, default='', help='Comma-separated ticker list (overrides full ASX scan)')
    parser.add_argument('--min-vol', type=int, default=MIN_AVG_VOLUME)
    parser.add_argument('--min-price', type=float, default=MIN_PRICE)
    parser.add_argument('--min-mcap', type=float, default=MIN_MARKET_CAP)
    args = parser.parse_args()

    import __main__ as _m
    _m.MIN_PRICE = args.min_price
    _m.MIN_MARKET_CAP = args.min_mcap
    _m.MIN_AVG_VOLUME = args.min_vol

    print("=" * 60)
    print("  ASX HIGHER-HIGH SCREENER (SECTOR GRID)")
    print("=" * 60)

    universe = get_asx_universe()

    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
        universe = {t: universe.get(t, {"industry": "Other", "sector": "Other", "market_cap": 0, "name": t}) for t in wanted}
        print(f"  Using custom list: {len(universe)} tickers")

    results = run_scan(universe, workers=args.workers)
    results.sort(key=lambda r: r['ticker'])

    out_base = os.path.join(SCRIPT_DIR, 'asx_hh_results')
    html_path = out_base + '.html'
    csv_path = out_base + '.csv'

    print(f"\n💾 Saving to:\n   {html_path}\n   {csv_path}\n")

    try:
        build_html_report(results, len(universe), html_path)
    except Exception as e:
        print(f"  ⚠ HTML save error: {e}")
    try:
        build_csv(results, csv_path)
    except Exception as e:
        print(f"  ⚠ CSV save error: {e}")

    if os.path.isfile(html_path):
        try:
            import webbrowser
            webbrowser.open(html_path)
            print(f"  🌐 Opening HTML report in your browser...")
        except Exception:
            print(f"  Open manually: {html_path}")


def _pause_and_exit(code=0):
    print("\n  Press Enter to exit...")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        _pause_and_exit()
    except Exception as e:
        import traceback
        print("\n" + "="*60)
        print("  ❌ UNEXPECTED ERROR")
        print("="*60)
        traceback.print_exc()
        _pause_and_exit(1)
