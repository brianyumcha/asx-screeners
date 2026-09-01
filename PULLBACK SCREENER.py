"""
ASX Fib Pullback Screener
==============================
Scans every listed ASX stock for a specific setup: an uptrending stock
currently pulling back into the Fibonacci "Zag Zone" (38.2%-61.8% retracement)
of its most recent up-leg, with confluence signals suggesting the pullback is
losing steam rather than reversing the trend.

This does NOT try to auto-validate a full Elliott Wave 5-up structure (with
bearish divergence on waves 3-5) - that call is genuinely subjective, even for
an expert, and this script isn't trying to fake certainty it doesn't have.
What it DOES do reliably:
  1. Auto-detect the most recent significant swing low -> swing high (a simple
     % threshold zigzag), and check whether price is currently retracing that
     swing into the 38.2%-61.8% zone. This is a REQUIRED filter - it's the
     structural core of the setup.
  2. Score (not gate) supporting confluence on top of that, matching the
     "grade by confluence present/missing, don't demand a perfect textbook
     setup" approach:
       - Volume on the pullback leg is LOWER than volume on the impulse leg
         (the down-move isn't being supported by real selling)
       - Hidden bullish divergence on RSI (price makes a higher low, RSI
         makes a lower low, within the pullback)
       - Hidden bullish divergence on Stochastic RSI (same idea)
       - Stochastic RSI %K about to cross back above %D from below
       - OBV still healthy (not confirming the down-move)

Every result gets a confluence_score (0-100) and a plain-English list of
which signals are present/missing, so you can rank and pick your own bar -
this deliberately does NOT hide anything behind a hard AND-gate beyond the
Zag Zone filter itself.

Output includes a TradingView watchlist .txt export (ASX:TICKER,ASX:TICKER,...)
so you can paste the shortlist straight into TradingView and mark up the
chart / confirm the wave count yourself, per your own workflow.

Usage:
    python "PULLBACK SCREENER.py"                    # full ASX scan
    python "PULLBACK SCREENER.py" --workers 20        # faster
    python "PULLBACK SCREENER.py" --tickers BHP,CBA,RIO
    python "PULLBACK SCREENER.py" --fresh              # ignore cooldown
    python "PULLBACK SCREENER.py" --min-score 40        # raise/lower the bar

Requirements:
    pip install yfinance pandas requests openpyxl pdfplumber
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta

import pandas as pd
import requests
import yfinance as yf
import openpyxl  # required by pandas to read .xlsx files (Market Index ticker list)
import pdfplumber  # required to read a locally-supplied ASX company list PDF

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_WORKERS   = 15
HISTORY_PERIOD    = "1y"    # need enough history to find a meaningful swing
CHART_TRIM_BARS   = 260     # trailing bars embedded per result for the dashboard's mini charts
RSI_PERIOD        = 14
STOCH_RSI_PERIOD  = 14
STOCH_SMOOTH_K    = 3
STOCH_SMOOTH_D    = 3

MIN_PRICE         = 0.05
MIN_MARKET_CAP    = 50_000_000
MIN_AVG_VOLUME    = 50_000
RATE_LIMIT_SLEEP  = 0.05

ZIGZAG_THRESHOLD_PCT = 8.0   # min % move to count as a new zigzag swing
MIN_IMPULSE_PCT       = 8.0  # the up-leg itself must be at least this big
ZAG_ZONE_LOW           = 0.382
ZAG_ZONE_HIGH          = 0.618

DEFAULT_MIN_SCORE = 30   # confluence_score floor for a result to be shown

COOLDOWN_DAYS    = 5
HISTORY_FILENAME = 'seen_tickers_pullback.json'

# ─── GET ASX TICKER LIST ──────────────────────────────────────────────────────
# Same multi-source fetch chain as OBV SCREENER.py, kept identical on purpose
# so both screeners see the same universe and share the same fallback logic.

MARKETINDEX_XLSX_URL = (
    "https://files.marketindex.com.au/files/data-downloads/30-june-2025.xlsx"
)
THIRD_PARTY_ASX_LIST_URL = (
    "https://www.asxlistedcompanies.com/uploads/csv/20200501-asx-listed-companies.csv"
)

FALLBACK_TICKERS = """
A2M,ABB,ABC,ABP,ACF,ACL,ADA,ADH,ADI,ADT,AEF,AEI,AEL,AEM,AFL,
AGE,AGF,AGI,AGY,AHG,AHX,AIA,AIM,AIS,AKE,AKP,ALC,ALK,ALL,ALQ,
ALU,ALX,AMB,AMC,AMH,AMP,AMS,AMX,ANF,ANL,ANP,ANZ,AOF,AOG,APA,
APE,APM,APT,APX,AQZ,ARB,ARF,ARG,ARI,ARQ,ARX,ASB,ASG,ASL,AST,
ASX,ATL,ATM,ATO,ATP,ATR,AUB,AUF,AUI,AUP,AUR,AUT,AVH,AVJ,AVN,
AWC,AWF,AX1,AXE,AXP,AZJ,AZL,AZS,BAL,BAP,BFG,BGA,BHP,BIN,BKL,
BKT,BKW,BLD,BLT,BLX,BMN,BNO,BOE,BOQ,BPT,BRL,BRN,BSE,BSL,BTH,
BUB,BVS,BWP,BXB,CAJ,CAR,CBL,CBO,CCX,CDX,CEL,CFO,CGF,CGL,CGS,
CHC,CHL,CIA,CIM,CIP,CIW,CKF,CLH,CLT,CMM,CMP,CMW,CNB,CNU,COE,
COH,COL,COR,CPU,CQR,CRN,CSL,CSV,CTD,CTP,CU6,CUV,CVN,CWN,CWP,
CXL,CXO,CYC,CYL,DAL,DAN,DBI,DBF,DCN,DGL,DHG,DIO,DJW,DLX,DMP,
DNK,DOW,DRE,DSE,DTC,DTL,DUB,DVP,DXC,DXS,ECF,ECX,EDE,EDV,EFG,
EGL,EHE,ELD,ELO,ELS,ELT,EMB,EMN,EML,EMR,ENE,ENL,EOL,EPD,EPW,
EQT,ERG,ERM,ESS,EVN,EVS,EVO,EWC,EXL,EXP,FAR,FBU,FCL,FDV,FEX,
FFI,FHE,FIN,FLC,FLT,FMG,FNP,FOR,FPH,FRI,GBT,GCY,GEM,GFY,GHC,
GIB,GLE,GLN,GMA,GMD,GMG,GNC,GNG,GNX,GOZ,GPT,GQG,GR1,GRR,GTK,
GTN,GUD,GXY,HAV,HCW,HDN,HEX,HFR,HHV,HIT,HLI,HLS,HMC,HMD,HML,
HNG,HOT,HPG,HRL,HRR,HTA,HUB,HVN,HXL,IAG,IBC,IDR,IEL,IFM,IFN,
IGO,ILU,IMD,INA,INR,INS,IOO,IPD,IPH,IRE,IRI,IRM,ISX,ITD,IVC,
JAN,JBH,JDO,JHG,JHX,JIN,JLG,JMS,JRV,KAR,KBC,KGN,KLA,KLL,KMD,
KRM,KSL,LAU,LBL,LCK,LFG,LGI,LKE,LLC,LLL,LNK,LOT,LPE,LRS,LRK,
LSF,LTR,LYC,LYL,MAD,MFG,MGL,MIN,MLD,MLT,MME,MMS,MNY,MOC,MQA,
MQG,MRK,MRM,MRZ,MSB,MSV,MTR,MTS,MVF,MVL,MYD,NAB,NAM,NAR,NBI,
NBL,NCK,NCM,NEC,NEA,NHF,NHT,NIC,NMT,NNW,NOV,NST,NTO,NUF,NVA,
NVX,NWH,NWL,NWS,OBL,OBM,OCA,OCL,OFX,OGC,OML,ONC,ONT,OPH,OPY,
ORG,ORI,ORL,OZL,PAC,PAI,PAN,PAR,PAT,PBH,PBP,PCK,PDL,PEK,PEV,
PGC,PGL,PGH,PHI,PKS,PLT,PLS,PMC,PNI,PNV,POW,PPM,PPT,PRN,PRO,
PTC,PTM,PVS,QAN,QBE,QIN,QOR,QUB,RBL,RDY,REA,REG,REH,RFF,RFT,
RGN,RGP,RHL,RHP,RIC,RIO,RKN,RMD,RMS,RMY,RND,RNT,ROD,ROG,RRL,
RSG,RVA,RWC,RXP,S32,SAP,SAR,SBM,SCG,SCM,SCQ,SDG,SDF,SDI,SEA,
SEK,SFR,SGF,SGH,SGM,SGP,SGR,SHL,SHM,SHO,SHV,SIG,SIP,SIQ,SKC,
SKI,SKO,SLA,SLC,SLK,SLX,SMP,SNA,SNL,SOL,SOM,SPL,SRG,SRL,SRX,
SSG,SSL,STA,STG,STN,STO,STP,SUL,SUN,SVW,SWM,SXE,SXL,SXY,SYA,
TCG,TCL,TDO,TGR,THL,TIE,TIG,TLG,TLS,TMX,TNE,TOY,TPG,TPW,TRS,
TRY,TSI,TUL,TWE,TYR,UMG,URW,VEA,VER,VGI,VHM,VIP,VML,VMY,VOC,
VUL,WAF,WAM,WAR,WBC,WDS,WEB,WES,WHC,WIA,WLD,WOR,WOW,WPL,WRM,
WRR,WTC,XAM,XRO,YAL,Z1P,ZIM
""".replace('\n', '').replace(' ', '')

LOCAL_ASX_CSV_FILENAMES = ['ASXListedCompanies.csv', 'asx_listed_companies.csv']
LOCAL_ASX_PDF_FILENAMES = ['ASX_Companies.pdf', 'asx_listed_companies.pdf', 'ASXListedCompanies.pdf']
_PDF_ROW_PATTERN = re.compile(r'^(\d+)\s*-?\s*([A-Z0-9]{1,6})\b')


def _parse_asx_pdf(pdf_path):
    tickers = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            for line in text.split('\n'):
                line = line.strip()
                m = _PDF_ROW_PATTERN.match(line)
                if m and '$' in line:
                    tickers.append(m.group(2).upper())
    seen, unique = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _parse_asx_csv_text(csv_text):
    lines = csv_text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if 'ASX code' in line or 'Company name' in line:
            start = i
            break
    df = pd.read_csv(io.StringIO('\n'.join(lines[start:])))
    col = [c for c in df.columns if 'code' in c.lower() or 'asx' in c.lower()]
    if not col:
        return []
    tickers = df[col[0]].dropna().astype(str).str.strip().str.upper().tolist()
    return [t for t in tickers if len(t) <= 5 and t.isalpha()]


def _find_ticker_column(columns):
    candidates = ['asx code', 'code', 'ticker', 'asx ticker', 'symbol']
    lower_map = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    for lc, orig in lower_map.items():
        if 'code' in lc or 'ticker' in lc or 'symbol' in lc:
            return orig
    return None


def get_asx_tickers():
    """Same fallback chain as OBV SCREENER.py: local PDF -> local CSV -> SeaBee
    API -> Market Index xlsx -> third-party mirror -> hardcoded fallback."""
    print("📋 Fetching ASX ticker list...")

    for fname in LOCAL_ASX_PDF_FILENAMES:
        local_path = os.path.join(SCRIPT_DIR, fname)
        if os.path.isfile(local_path):
            try:
                tickers = _parse_asx_pdf(local_path)
                if len(tickers) > 500:
                    print(f"  ✓ Loaded {len(tickers)} tickers from local PDF: {fname}")
                    return tickers
            except Exception as e:
                print(f"  ⚠ Failed to read local PDF {fname}: {e}")

    for fname in LOCAL_ASX_CSV_FILENAMES:
        local_path = os.path.join(SCRIPT_DIR, fname)
        if os.path.isfile(local_path):
            try:
                with open(local_path, 'r', encoding='utf-8-sig') as f:
                    tickers = _parse_asx_csv_text(f.read())
                if len(tickers) > 100:
                    print(f"  ✓ Loaded {len(tickers)} tickers from local file: {fname}")
                    return tickers
            except Exception as e:
                print(f"  ⚠ Failed to read local {fname}: {e}")

    # SeaBee Custom API (JSON)
    try:
        api_url = "https://marketdata.seabee.me/api.php?action=asx_companies_list"
        headers = {"X-API-Key": "deeznuts"}
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            json_resp = r.json()
            if json_resp.get("success") and "data" in json_resp and "companies" in json_resp["data"]:
                raw_tickers = json_resp["data"]["companies"].keys()
                tickers = [str(t).strip().upper() for t in raw_tickers
                           if len(str(t).strip()) <= 5 and str(t).strip().isalnum()]
                if len(tickers) > 500:
                    print(f"  ✓ Fetched {len(tickers)} tickers from SeaBee API!")
                    return tickers
            else:
                print("  ⚠ SeaBee API returned an unexpected JSON structure.")
        else:
            print(f"  ⚠ SeaBee API returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ SeaBee API fetch failed: {e}")

    try:
        r = requests.get(MARKETINDEX_XLSX_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            xbytes = io.BytesIO(r.content)
            xls = pd.ExcelFile(xbytes)
            for sheet in xls.sheet_names:
                try:
                    df = xls.parse(sheet)
                except Exception:
                    continue
                col = _find_ticker_column(df.columns)
                if not col:
                    continue
                tickers = df[col].dropna().astype(str).str.strip().str.upper().tolist()
                tickers = [t for t in tickers if 1 <= len(t) <= 5 and t.isalnum()]
                if len(tickers) > 500:
                    print(f"  ✓ Fetched {len(tickers)} tickers from Market Index (30 Jun 2025 data)")
                    return tickers
        else:
            print(f"  ⚠ Market Index file returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ Market Index fetch failed: {e}")

    try:
        r = requests.get(THIRD_PARTY_ASX_LIST_URL, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            lines = r.text.splitlines()
            start = 0
            for i, line in enumerate(lines):
                if line.strip().startswith('Code,'):
                    start = i
                    break
            df = pd.read_csv(io.StringIO('\n'.join(lines[start:])))
            col = [c for c in df.columns if c.strip().lower() == 'code']
            if col:
                tickers = df[col[0]].dropna().astype(str).str.strip().str.upper().tolist()
                tickers = [t for t in tickers if len(t) <= 5 and t.isalnum()]
                if len(tickers) > 500:
                    print(f"  ✓ Fetched {len(tickers)} tickers from third-party ASX list")
                    return tickers
    except Exception as e:
        print(f"  ⚠ Third-party ASX list fetch failed: {e}")

    tickers = [t.strip() for t in FALLBACK_TICKERS.split(',') if t.strip()]
    print(f"  ⚠ Using built-in list of only {len(tickers)} major ASX tickers")
    return tickers


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


def linear_slope(arr):
    n = len(arr)
    if n < 2:
        return 0
    x_mean = (n - 1) / 2
    y_mean = sum(arr) / n
    num = sum((i - x_mean) * (arr[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0


def rsi_series(closes, period=RSI_PERIOD):
    """Wilder's RSI as a full pandas Series (needed for divergence detection,
    not just the latest value)."""
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def stoch_rsi_series(rsi, period=STOCH_RSI_PERIOD, smooth_k=STOCH_SMOOTH_K, smooth_d=STOCH_SMOOTH_D):
    """Standard Stochastic RSI: stochastic oscillator applied to RSI itself,
    then smoothed. Returns (%K, %D) as pandas Series, 0-100 scale."""
    lo = rsi.rolling(period).min()
    hi = rsi.rolling(period).max()
    stoch = ((rsi - lo) / (hi - lo).replace(0, 1e-9)) * 100
    k = stoch.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return k, d


def find_local_minima(values, order=2):
    """Indices where `values` is lower than `order` neighbours on each side.
    Small manual implementation to avoid adding a scipy dependency."""
    n = len(values)
    minima = []
    for i in range(order, n - order):
        window = values[i - order:i + order + 1]
        if values[i] == min(window) and window.count(values[i]) == 1:
            minima.append(i)
    return minima


def zigzag(highs, lows, threshold_pct=ZIGZAG_THRESHOLD_PCT):
    """
    Classic threshold zigzag. Returns a list of (index, price, kind) pivots,
    kind is 'L' (swing low) or 'H' (swing high), alternating, starting with
    whichever kind is opposite the first detected direction (so a pivot at
    bar 0 anchors the very first swing instead of being dropped).
    """
    if len(highs) < 3:
        return []

    thr = threshold_pct / 100.0
    direction = 0  # 0 = undetermined, 1 = tracking a high, -1 = tracking a low
    extreme_idx, extreme_price = 0, lows[0]

    i = 1
    while direction == 0 and i < len(highs):
        if (highs[i] - lows[0]) / lows[0] >= thr:
            direction, extreme_price, extreme_idx = 1, highs[i], i
        elif (highs[0] - lows[i]) / highs[0] >= thr:
            direction, extreme_price, extreme_idx = -1, lows[i], i
        i += 1

    if direction == 0:
        return []  # never moved enough in either direction to establish a swing

    pivots = [(0, lows[0] if direction == 1 else highs[0], 'L' if direction == 1 else 'H')]

    for j in range(i, len(highs)):
        if direction == 1:
            if highs[j] > extreme_price:
                extreme_price, extreme_idx = highs[j], j
            elif (extreme_price - lows[j]) / extreme_price >= thr:
                pivots.append((extreme_idx, extreme_price, 'H'))
                direction, extreme_price, extreme_idx = -1, lows[j], j
        else:
            if lows[j] < extreme_price:
                extreme_price, extreme_idx = lows[j], j
            elif (highs[j] - extreme_price) / extreme_price >= thr:
                pivots.append((extreme_idx, extreme_price, 'L'))
                direction, extreme_price, extreme_idx = 1, highs[j], j

    # the current in-progress extreme, as a provisional pivot (may still extend)
    pivots.append((extreme_idx, extreme_price, 'H' if direction == 1 else 'L'))
    return pivots


def find_latest_impulse_and_pullback(pivots, current_idx, current_price):
    """
    From the zigzag pivot list, find the most recent completed swing LOW ->
    swing HIGH (the "impulse leg"), where price since that high has pulled
    back below it (i.e. we're currently retracing it). Returns
    (low_idx, low_price, high_idx, high_price) or None if no such setup
    exists in the data.
    """
    if len(pivots) < 2:
        return None

    # walk backwards looking for a L then H pair, most recent first
    for i in range(len(pivots) - 1, 0, -1):
        hi_idx, hi_price, hi_kind = pivots[i]
        lo_idx, lo_price, lo_kind = pivots[i - 1]
        if hi_kind == 'H' and lo_kind == 'L' and hi_idx > lo_idx:
            impulse_pct = (hi_price - lo_price) / lo_price * 100
            if impulse_pct < MIN_IMPULSE_PCT:
                continue
            if current_idx > hi_idx and current_price < hi_price:
                return (lo_idx, lo_price, hi_idx, hi_price)
    return None


def confluence_score(signals):
    """
    signals: dict of bool/None flags. Zag Zone membership is the entry
    ticket (already required before this is called) - everything here is
    additive confluence on top, matching "grade by confluence present, don't
    require a perfect textbook setup."
    """
    score = 20  # baseline for being in the Zag Zone at all
    if signals.get('volume_declining'):
        score += 20
    if signals.get('hidden_bull_div_rsi'):
        score += 20
    if signals.get('hidden_bull_div_stochrsi'):
        score += 15
    if signals.get('stochrsi_cross_imminent'):
        score += 15
    if signals.get('obv_healthy'):
        score += 10
    return min(100, score)


# ─── ANALYSE A SINGLE STOCK ───────────────────────────────────────────────────

def analyse_ticker(ticker_raw, min_score=DEFAULT_MIN_SCORE):
    sym = ticker_raw.upper().strip()
    yahoo_sym = sym if sym.endswith('.AX') else sym + '.AX'

    try:
        ticker_obj = yf.Ticker(yahoo_sym)

        try:
            info = ticker_obj.fast_info
            market_cap = getattr(info, 'market_cap', None) or 0
        except Exception:
            market_cap = 0
            info = None

        if market_cap and market_cap < MIN_MARKET_CAP:
            return None

        df = ticker_obj.history(period=HISTORY_PERIOD, interval='1d', auto_adjust=True)
        if df.empty or len(df) < 60:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])
        closes  = df['Close'].tolist()
        opens   = df['Open'].tolist()
        highs   = df['High'].tolist()
        lows    = df['Low'].tolist()
        volumes = df['Volume'].fillna(0).tolist()
        dates   = [d.strftime('%Y-%m-%d') for d in df.index]
        n = len(closes)
        if n < 60:
            return None

        price = closes[-1]
        if price < MIN_PRICE:
            return None

        if market_cap == 0 and info is not None:
            try:
                shares = getattr(info, 'shares', None) or 0
                market_cap = shares * price if shares else 0
            except Exception:
                market_cap = 0
        if market_cap > 0 and market_cap < MIN_MARKET_CAP:
            return None

        avg_vol = sum(volumes[-30:]) / min(30, len(volumes))
        if avg_vol < MIN_AVG_VOLUME:
            return None

        # ── Structural filter: in the Zag Zone of the latest impulse leg ──
        pivots = zigzag(highs, lows)
        setup = find_latest_impulse_and_pullback(pivots, n - 1, price)
        if setup is None:
            return None
        lo_idx, lo_price, hi_idx, hi_price = setup

        retracement = (hi_price - price) / (hi_price - lo_price)
        if not (ZAG_ZONE_LOW <= retracement <= ZAG_ZONE_HIGH):
            return None

        # ── Supporting confluence (scored, not gated) ──────────────────────
        signals = {}

        impulse_vols = volumes[lo_idx:hi_idx + 1]
        pullback_vols = volumes[hi_idx:]
        impulse_avg_vol = sum(impulse_vols) / max(1, len(impulse_vols))
        pullback_avg_vol = sum(pullback_vols) / max(1, len(pullback_vols))
        vol_ratio_pullback_vs_impulse = pullback_avg_vol / (impulse_avg_vol + 1e-9)
        signals['volume_declining'] = vol_ratio_pullback_vs_impulse < 0.9

        rsi = rsi_series(closes, RSI_PERIOD)
        stoch_k, stoch_d = stoch_rsi_series(rsi)

        pullback_lows_window = lows[hi_idx:]
        window_offset = hi_idx
        minima_local = find_local_minima(pullback_lows_window, order=2)
        minima_idx = [m + window_offset for m in minima_local]
        # always consider the most-recent bar as a candidate "low" too, in
        # case the pullback low is still forming right now
        if n - 1 not in minima_idx:
            minima_idx.append(n - 1)
        minima_idx = sorted(set(minima_idx))

        def hidden_bull_div(osc_series):
            if len(minima_idx) < 2:
                return False
            i1, i2 = minima_idx[-2], minima_idx[-1]
            price_higher_low = lows[i2] > lows[i1]
            osc_v1, osc_v2 = osc_series.iloc[i1], osc_series.iloc[i2]
            if pd.isna(osc_v1) or pd.isna(osc_v2):
                return False
            osc_lower_low = osc_v2 < osc_v1
            return bool(price_higher_low and osc_lower_low)

        signals['hidden_bull_div_rsi'] = hidden_bull_div(rsi)
        signals['hidden_bull_div_stochrsi'] = hidden_bull_div(stoch_k)

        k_last, k_prev = stoch_k.iloc[-1], stoch_k.iloc[-2] if n > 1 else None
        d_last, d_prev = stoch_d.iloc[-1], stoch_d.iloc[-2] if n > 1 else None
        cross_imminent = False
        if pd.notna(k_last) and pd.notna(d_last) and pd.notna(k_prev) and pd.notna(d_prev):
            gap_now = d_last - k_last
            gap_prev = d_prev - k_prev
            already_crossed = k_prev <= d_prev and k_last > d_last
            closing_fast = 0 <= gap_now < gap_prev and k_last < 50
            cross_imminent = bool((already_crossed or closing_fast) and k_last < 60)
        signals['stochrsi_cross_imminent'] = cross_imminent

        obv = calc_obv_series(closes, volumes)
        obv_pullback_slope = linear_slope(obv[hi_idx:]) if n - hi_idx >= 2 else 0
        # "healthy" here means OBV isn't falling hard while price pulls back -
        # i.e. sellers aren't really pressing, matching the volume story
        signals['obv_healthy'] = obv_pullback_slope >= 0

        score = confluence_score(signals)
        if score < min_score:
            return None

        prev_close = closes[-2]
        change_1d = (price - prev_close) / prev_close * 100
        lo_date = df.index[lo_idx].strftime('%Y-%m-%d')
        hi_date = df.index[hi_idx].strftime('%Y-%m-%d')

        present = [k for k, v in signals.items() if v]
        missing = [k for k, v in signals.items() if not v]

        # Sector only fetched for tickers that already passed every filter -
        # yfinance's full .info scrape is much slower than fast_info, so this
        # cost is paid only by the handful of actual results, not the bulk scan.
        try:
            sector = ticker_obj.info.get('sector') or 'Other'
        except Exception:
            sector = 'Other'

        trim = slice(-CHART_TRIM_BARS, None)

        return {
            'ticker': sym,
            'sector': sector,
            'price': round(price, 4),
            'market_cap': int(market_cap) if market_cap else 0,
            'change_1d': round(change_1d, 2),
            'confluence_score': score,
            'retracement_pct': round(retracement * 100, 1),
            'swing_low_price': round(lo_price, 4),
            'swing_low_date': lo_date,
            'swing_high_price': round(hi_price, 4),
            'swing_high_date': hi_date,
            'impulse_pct': round((hi_price - lo_price) / lo_price * 100, 1),
            'vol_ratio_pullback_vs_impulse': round(vol_ratio_pullback_vs_impulse, 2),
            'rsi': round(rsi.iloc[-1], 1) if pd.notna(rsi.iloc[-1]) else None,
            'stoch_k': round(k_last, 1) if pd.notna(k_last) else None,
            'stoch_d': round(d_last, 1) if pd.notna(d_last) else None,
            'signals_present': present,
            'signals_missing': missing,
            'avg_vol': int(avg_vol),
            'dates': dates[trim],
            'opens': [round(v, 4) for v in opens[trim]],
            'highs': [round(v, 4) for v in highs[trim]],
            'lows': [round(v, 4) for v in lows[trim]],
            'closes': [round(v, 4) for v in closes[trim]],
            'volumes': [int(v) for v in volumes[trim]],
        }

    except Exception:
        return None


# ─── SCAN ALL TICKERS ─────────────────────────────────────────────────────────

def run_scan(tickers, min_score=DEFAULT_MIN_SCORE, workers=DEFAULT_WORKERS):
    results = []
    failed = 0
    total = len(tickers)
    done = 0
    t0 = time.time()

    print(f"\n🔍 Scanning {total} ASX tickers for Zag Zone pullbacks | {workers} threads | min score {min_score}\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyse_ticker, t, min_score): t for t in tickers}
        for fut in as_completed(futures):
            done += 1
            tick = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    flag = f"  ✅ {tick:8s} score={res['confluence_score']:3d} retr={res['retracement_pct']:5.1f}%"
                else:
                    flag = f"  ·  {tick}"
            except Exception as e:
                failed += 1
                flag = f"  ✗  {tick}  ({e})"

            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            sys.stdout.write(
                f"\r  [{done:4d}/{total}]  {rate:4.1f}/s  ETA {eta:4.0f}s  Signals: {len(results):3d}   "
            )
            sys.stdout.flush()
            if done % 50 == 0:
                time.sleep(RATE_LIMIT_SLEEP)

    print(f"\n\n✅ Scan complete in {time.time()-t0:.1f}s")
    print(f"   Scanned: {total}  |  Signals: {len(results)}  |  Failed: {failed}")
    return results


# ─── HTML DASHBOARD ────────────────────────────────────────────────────────────
# The card-grid/mini-chart dashboard itself lives in dashboard_template.py,
# shared with OBV SCREENER.py. This just adapts this screener's result dicts
# into the shared card schema.

def build_html_report(results, excluded, total_scanned, out_path):
    from dashboard_template import render_dashboard_html

    def to_card(r):
        return {
            'ticker': r['ticker'],
            'sector': r['sector'],
            'market_cap': r['market_cap'],
            'price': r['price'],
            'change_1d': r['change_1d'],
            'score': r['confluence_score'],
            'stats': [
                {'label': 'Retr', 'value': f"{r['retracement_pct']:.1f}%"},
                {'label': 'RSI', 'value': f"{r['rsi']:.0f}" if r['rsi'] is not None else '—'},
                {'label': 'Vol PB/Imp', 'value': f"{r['vol_ratio_pullback_vs_impulse']:.2f}×"},
            ],
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        excluded_cards=[to_card(r) for r in excluded],
        total_scanned=total_scanned,
        title='ASX PULLBACK SCREENER — ZAG ZONE',
        subtitle='ASX stocks pulling back into the 38.2%-61.8% retracement of their latest swing.',
        footer_note=(
            'ASX Pullback Screener · Data via Yahoo Finance (yfinance), ticker universe via SeaBee/Market Index<br>'
            f'Required: latest swing retracement in the Zag Zone (impulse ≥ {MIN_IMPULSE_PCT}%). '
            'Confluence scored, not gated — swing low/high dates shown so you can confirm the wave count on your own chart.'
        ),
        out_path=out_path + '.html',
    )
    print(f"  📄 HTML report → {out_path}.html")


def build_csv(results, out_path):
    array_fields = {'dates', 'opens', 'highs', 'lows', 'closes', 'volumes'}
    fieldnames = list(results[0].keys()) if results else [
        'ticker', 'price', 'confluence_score', 'retracement_pct'
    ]
    fieldnames = [k for k in fieldnames if k not in array_fields]
    with open(out_path + '.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            row = {k: v for k, v in r.items() if k not in array_fields}
            row['signals_present'] = '|'.join(row['signals_present'])
            row['signals_missing'] = '|'.join(row['signals_missing'])
            w.writerow(row)
    print(f"  📊 CSV export  → {out_path}.csv")


def build_tradingview_watchlist(results, out_path):
    tv_tickers = [f"ASX:{r['ticker']}" for r in results]
    with open(out_path + '_tradingview_watchlist.txt', 'w', encoding='utf-8') as f:
        f.write(','.join(tv_tickers))
    print(f"  📥 TradingView watchlist → {out_path}_tradingview_watchlist.txt")


# ─── COOLDOWN / SEEN-TICKER HISTORY ───────────────────────────────────────────

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
            last_seen = datetime.strptime(last_seen_str, '%Y-%m-%d').date()
        except Exception:
            continue
        if _weekdays_between(last_seen, today) < cooldown_days:
            excluded.add(ticker)
    return excluded


def update_seen_history(history, results):
    today_str = date.today().strftime('%Y-%m-%d')
    for r in results:
        history[r['ticker']] = today_str
    return history


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ASX Fib Pullback (Zag Zone) Screener')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--out', type=str, default='asx_pullback_results')
    parser.add_argument('--tickers', type=str, default='',
                        help='Comma-separated ticker list (overrides full ASX scan)')
    parser.add_argument('--min-score', type=int, default=DEFAULT_MIN_SCORE,
                        help=f'Min confluence score 0-100 to show a result (default: {DEFAULT_MIN_SCORE})')
    parser.add_argument('--min-vol', type=int, default=MIN_AVG_VOLUME)
    parser.add_argument('--min-price', type=float, default=MIN_PRICE)
    parser.add_argument('--min-mcap', type=float, default=MIN_MARKET_CAP)
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--cooldown', type=int, default=COOLDOWN_DAYS)
    args = parser.parse_args()

    import __main__ as _m
    _m.MIN_PRICE = args.min_price
    _m.MIN_MARKET_CAP = args.min_mcap
    _m.MIN_AVG_VOLUME = args.min_vol

    print("=" * 60)
    print("  ASX PULLBACK SCREENER — ZAG ZONE (38.2%-61.8%)")
    print("=" * 60)
    print(f"  Filters: price ≥ ${args.min_price:.2f}  |  mkt cap ≥ ${args.min_mcap/1e6:.0f}M  |  avg vol ≥ {args.min_vol:,}")
    print(f"  Required: latest swing retracement in Zag Zone, impulse ≥ {MIN_IMPULSE_PCT}%")
    print(f"  Confluence floor: score ≥ {args.min_score}")
    if args.fresh:
        print(f"  Cooldown: OFF for this run (--fresh)")
    elif args.cooldown <= 0:
        print(f"  Cooldown: disabled (--cooldown 0)")
    else:
        print(f"  Cooldown: hiding stocks seen in the last {args.cooldown} trading days")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
        print(f"  Using custom list: {len(tickers)} tickers")
    else:
        tickers = get_asx_tickers()

    results = run_scan(tickers, min_score=args.min_score, workers=args.workers)
    results.sort(key=lambda x: x['confluence_score'], reverse=True)

    history = load_seen_history()
    skipped = []
    if args.fresh:
        print(f"\n  🔄 --fresh used: showing all results, ignoring cooldown history")
    else:
        excluded_tickers = get_excluded_tickers(history, args.cooldown)
        skipped = [r for r in results if r['ticker'] in excluded_tickers]
        results = [r for r in results if r['ticker'] not in excluded_tickers]
        if skipped:
            print(f"\n  🔁 Hid {len(skipped)} stock(s) already seen within the last {args.cooldown} trading days:")
            print(f"     {', '.join(sorted(r['ticker'] for r in skipped))}")
            print(f"     (use --fresh to include them, or --cooldown 0 to disable this)")

    history = update_seen_history(history, results)
    save_seen_history(history)

    if results:
        print(f"\n{'─'*60}")
        print(f"  TOP SIGNALS\n")
        print(f"  {'TICKER':<8} {'SCORE':>5} {'RETR%':>6} {'RSI':>5}")
        print(f"  {'─'*8} {'─'*5} {'─'*6} {'─'*5}")
        for r in results[:20]:
            print(f"  {r['ticker']:<8} {r['confluence_score']:>5} {r['retracement_pct']:>5.1f}% {r['rsi'] or 0:>5.1f}")
        if len(results) > 20:
            print(f"  ... and {len(results)-20} more in the report")

    out_base = os.path.join(SCRIPT_DIR, 'asx_pullback_results')
    html_path = out_base + '.html'
    csv_path = out_base + '.csv'
    tv_path = out_base + '_tradingview_watchlist.txt'

    print(f"\n💾 Saving to:\n   {html_path}\n   {csv_path}\n   {tv_path}\n")

    try:
        build_html_report(results, skipped, len(tickers), out_base)
    except Exception as e:
        print(f"  ⚠ HTML save error: {e}")
    try:
        build_csv(results, out_base)
    except Exception as e:
        print(f"  ⚠ CSV save error: {e}")
    try:
        build_tradingview_watchlist(results, out_base)
    except Exception as e:
        print(f"  ⚠ TradingView watchlist save error: {e}")

    html_ok = os.path.isfile(html_path)
    csv_ok = os.path.isfile(csv_path)
    tv_ok = os.path.isfile(tv_path)

    print(f"\n{'='*60}")
    print(f"  {'✅' if html_ok else '❌'} HTML → {html_path}")
    print(f"  {'✅' if csv_ok  else '❌'} CSV  → {csv_path}")
    print(f"  {'✅' if tv_ok   else '❌'} TradingView watchlist → {tv_path}")
    print(f"{'='*60}")

    if html_ok:
        try:
            import webbrowser
            webbrowser.open(html_path)
            print(f"\n  🌐 Opening HTML report in your browser...")
        except Exception:
            print(f"\n  Open manually: {html_path}")


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
        print("  ❌ UNEXPECTED ERROR — details below:")
        print("="*60)
        traceback.print_exc()
        print("="*60)
        print("  The report may still have been saved.")
        print("  Copy the error above and send it for help.")
        _pause_and_exit(1)
