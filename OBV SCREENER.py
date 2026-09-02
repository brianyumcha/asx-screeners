"""
ASX Pre-Breakout Screener
==============================
Scans every listed ASX stock for stocks that are quietly building strength
BEFORE they break out — not stocks already breaking out.

Criteria (a stock must pass ALL of these):
  1. Rising OBV over the last 30 trading days (same slope-based logic as
     the original OBV screener, just widened to a 30-day window)
  2. Price has NOT broken the high of the previous 30 trading days
  3. RSI (14-period) between 45 and 70
  4. Price above the 20-day SMA
  5. Price above the 50-day SMA
  6. Price >= 5 cents
  7. Market cap >= $50 million
  8. 30-day average daily volume >= 50,000 shares

Note: distance to the 30-day high (gap_pct) is NOT a screening criterion —
it's calculated purely as an output column so you can sort/rank results
by how "tight" the setup is after the fact.

Cooldown: a stock that appeared in your results stays hidden from future runs
for 5 trading days (so you're not re-reviewing the same charts on TradingView
day after day). This is tracked in a small local file, seen_tickers.json,
saved next to this script. Use --fresh for a one-off run that ignores this
and shows everything, or --cooldown 0 to disable it entirely.

Usage:
    python breakout_screener.py                  # full ASX scan
    python breakout_screener.py --workers 20     # faster (more parallel threads)
    python breakout_screener.py --out results    # custom output filename
    python breakout_screener.py --tickers BHP,CBA,RIO  # scan specific tickers only
    python breakout_screener.py --fresh          # ignore cooldown, show everything
    python breakout_screener.py --cooldown 10    # use a 10-day cooldown instead of 5

Requirements:
    pip install yfinance pandas requests tqdm
"""

import argparse
import csv
import io
import json
import math
import os
import re
import statistics
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

import price_cache

warnings.filterwarnings("ignore")

# Save output files next to this script, not in system32
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_WORKERS     = 15       # parallel threads (increase for speed, risk rate-limits)
OBV_DAYS             = 30      # OBV rising window (trading days)
LOOKBACK_DAYS        = 30      # "N-day high" window for no-breakout check (trading days)
RSI_PERIOD            = 14     # standard RSI period
RSI_MIN               = 45     # RSI floor
RSI_MAX               = 70     # RSI ceiling
SMA_SHORT             = 20     # short moving average
SMA_LONG              = 50     # long moving average
HISTORY_PERIOD       = "1y"    # need enough history for 50-day SMA + 30-day windows, and a 12M chart view
CHART_TRIM_BARS       = 260    # trailing bars embedded per result for the dashboard's mini charts
MIN_PRICE             = 0.05   # minimum price filter (5 cents)
MIN_MARKET_CAP        = 50_000_000  # minimum market cap ($50 million)
MIN_AVG_VOLUME         = 50_000     # filter out illiquid stocks (30-day avg daily volume)
RATE_LIMIT_SLEEP      = 0.05   # seconds between batches to avoid rate limits
# Below this fraction of tickers actually returning usable price data on a
# full-universe scan (not a --tickers test run), treat the whole run as
# broken (Yahoo Finance throttling the source IP, e.g. a GitHub Actions
# runner) rather than a real "quiet day", and abort before publishing -
# found 2026-09-01 on the HH screener's identical fetch pattern: a GitHub
# Actions run silently got real data for only 27/2037 tickers (1.3%).
MIN_FETCH_RATIO       = 0.5
MIN_UNIVERSE_FOR_CHECK = 500

# ─── COOLDOWN / SEEN-TICKER HISTORY ───────────────────────────────────────────
# Avoids re-showing a stock you already reviewed recently. A ticker only gets
# logged here if it actually appeared in your RESULTS (i.e. passed every
# criterion) - not just because the script looked at it while scanning.
COOLDOWN_DAYS    = 5    # trading days a ticker stays excluded after last appearing
HISTORY_FILENAME = 'seen_tickers.json'

# ─── GET ASX TICKER LIST ──────────────────────────────────────────────────────

ASX_CSV_URL = (
    "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
)

# NOTE: as of 2026 the ASX's own site blocks scripted downloads of its
# ASXListedCompanies.csv, so a Market Index data-download file is used as the
# primary automated source instead - it's a real spreadsheet of ASX closing
# prices they publish each financial year-end (30 June), so it's usually only
# a few months stale rather than years. The 2020 third-party CSV mirror below
# is kept as a secondary fallback in case this one is ever unavailable.
MARKETINDEX_XLSX_URL = (
    "https://files.marketindex.com.au/files/data-downloads/30-june-2025.xlsx"
)
THIRD_PARTY_ASX_LIST_URL = (
    "https://www.asxlistedcompanies.com/uploads/csv/20200501-asx-listed-companies.csv"
)

# Hardcoded fallback list of major ASX stocks in case network fetch fails
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


LOCAL_ASX_CSV_FILENAMES = [
    'ASXListedCompanies.csv',
    'asx_listed_companies.csv',
]

LOCAL_ASX_PDF_FILENAMES = [
    'ASX_Companies.pdf',
    'asx_listed_companies.pdf',
    'ASXListedCompanies.pdf',
]

# Matches lines like "1 BHP BHP Group Ltd $63.71 -0.22% ..." or
# "23 VAS $112.52 ..." from a Market Index "List of ASX Companies" PDF export
# (Rank, optional '-' watchlist-star column, then the ticker code).
_PDF_ROW_PATTERN = re.compile(r'^(\d+)\s*-?\s*([A-Z0-9]{1,6})\b')


def _parse_asx_pdf(pdf_path):
    """
    Parse a PDF exported from Market Index's 'List of ASX Companies' page
    (https://www.marketindex.com.au/asx-listed-companies -> browser print to
    PDF). Each row looks like:
        "<rank> [-] <CODE> <Company name...> $<price> <chg%> ... <Sector> <MktCap>"
    Long company/sector names sometimes wrap onto a separate line, but that
    wrapped text never starts with a rank number, so matching on lines that
    start with "<digits> <CODE>" and contain a '$' reliably isolates each
    company row without depending on the wrapped text.
    """
    tickers = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            for line in text.split('\n'):
                line = line.strip()
                m = _PDF_ROW_PATTERN.match(line)
                if m and '$' in line:
                    tickers.append(m.group(2).upper())
    # de-duplicate while preserving order
    seen = set()
    unique = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _parse_asx_csv_text(csv_text):
    """Parse the ASX 'ASXListedCompanies.csv' format and return a ticker list."""
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
    tickers = [t for t in tickers if len(t) <= 5 and t.isalpha()]
    return tickers


def _find_ticker_column(columns):
    """Find the most likely 'ASX code / ticker' column name from a list of
    column names, trying a few common variants."""
    candidates = ['asx code', 'code', 'ticker', 'asx ticker', 'symbol']
    lower_map = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    # fallback: any column containing 'code' or 'ticker' or 'symbol'
    for lc, orig in lower_map.items():
        if 'code' in lc or 'ticker' in lc or 'symbol' in lc:
            return orig
    return None


def get_asx_tickers():
    """
    Get the full ASX ticker list, trying sources in this order:
      1. A local PDF next to this script (see README - export it yourself
         from Market Index's "List of ASX Companies" page via your browser's
         Print > Save as PDF. Since your browser renders the live page, this
         is genuinely the most current source available, and doesn't depend
         on any site allowing scripted downloads.)
      2. A local CSV file next to this script, if you've placed one there
      3. Live fetch from the ASX website (as of 2026 this is blocked by ASX's
         own bot protection, but kept here in case that changes)
      4. A Market Index year-end data file (usually only a few months stale)
      5. A third-party mirror of the ASX company list (dated ~2020 but covers
         the vast majority of tickers - delisted ones are auto-skipped later)
      6. A hardcoded list of ~550 major ASX tickers (last resort - NOT the
         full market)
    """
    print("📋 Fetching ASX ticker list...")

    # 1. Local PDF export (most current - captures live rendered page data)
    for fname in LOCAL_ASX_PDF_FILENAMES:
        local_path = os.path.join(SCRIPT_DIR, fname)
        if os.path.isfile(local_path):
            try:
                tickers = _parse_asx_pdf(local_path)
                if len(tickers) > 500:
                    print(f"  ✓ Loaded {len(tickers)} tickers from local PDF: {fname}")
                    return tickers
                else:
                    print(f"  ⚠ Local PDF {fname} only yielded {len(tickers)} tickers - skipping")
            except Exception as e:
                print(f"  ⚠ Failed to read local PDF {fname}: {e}")

    # 2. Local CSV file (works around ASX's bot blocking)
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

  # 3. SeaBee Custom API (JSON)
    try:
        api_url = "https://marketdata.seabee.me/api.php?action=asx_companies_list"
        headers = {"X-API-Key": "deeznuts"}
        
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            json_resp = r.json()
            
            # Verify the API returned a success flag and has the expected data structure
            if json_resp.get("success") and "data" in json_resp and "companies" in json_resp["data"]:
                # The tickers (like "BHP" or "14D") are the keys in the 'companies' dictionary
                raw_tickers = json_resp["data"]["companies"].keys()
                
                # Clean the list (allows alphanumeric codes up to 5 characters)
                tickers = [str(t).strip().upper() for t in raw_tickers if len(str(t).strip()) <= 5 and str(t).strip().isalnum()]
                
                if len(tickers) > 500:
                    print(f"  ✓ Fetched {len(tickers)} tickers from SeaBee API!")
                    return tickers
            else:
                print("  ⚠ SeaBee API returned an unexpected JSON structure.")
        else:
            print(f"  ⚠ SeaBee API returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ SeaBee API fetch failed: {e}")

    # 4. Market Index year-end xlsx (usually just a few months stale)
    try:
        r = requests.get(MARKETINDEX_XLSX_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            xbytes = io.BytesIO(r.content)
            # Try every sheet in the workbook until one yields a usable ticker column
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
            print(f"  ⚠ Market Index file downloaded but no usable ticker column found")
        else:
            print(f"  ⚠ Market Index file returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ Market Index fetch failed: {e}")

    # 5. Third-party mirror of the ASX company list
    try:
        r = requests.get(THIRD_PARTY_ASX_LIST_URL, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            lines = r.text.splitlines()
            # This file's header row starts with "Code,Company,Sector,..."
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
                    print(f"    (Note: this list is dated - delisted tickers are auto-skipped")
                    print(f"    during scanning, so this just means a few extra 'no data found'")
                    print(f"    lines below, not an error.)")
                    return tickers
        else:
            print(f"  ⚠ Third-party ASX list returned status {r.status_code}")
    except Exception as e:
        print(f"  ⚠ Third-party ASX list fetch failed: {e}")

    tickers = [t.strip() for t in FALLBACK_TICKERS.split(',') if t.strip()]
    print(f"  ⚠ Using built-in list of only {len(tickers)} major ASX tickers")
    print(f"    (This is NOT the full ASX. For a full scan, download the official")
    print(f"    ASX company list from your browser and save it next to this script")
    print(f"    as 'ASXListedCompanies.csv' - see README for instructions.)")
    return tickers


# ─── INDICATORS ───────────────────────────────────────────────────────────────

def calc_obv(closes, volumes):
    """Calculate On-Balance Volume series."""
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
    """Return linear regression slope of array."""
    n = len(arr)
    if n < 2:
        return 0
    x_mean = (n - 1) / 2
    y_mean = sum(arr) / n
    num = sum((i - x_mean) * (arr[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0


def obv_score(obv_series, price_series, window=30):
    """
    Score OBV strength 0-100. Same formula as the original OBV screener,
    just applied over a 30-day window instead of 10.
    """
    if len(obv_series) < window:
        return 0

    obv_w = obv_series[-window:]
    slope = linear_slope(obv_w)

    mean = sum(obv_w) / len(obv_w)
    variance = sum((v - mean) ** 2 for v in obv_w) / len(obv_w)
    std = math.sqrt(variance) if variance > 0 else 1

    norm_slope = (slope / std) * 50

    p_chg = (price_series[-1] - price_series[-window]) / (abs(price_series[-window]) + 1e-9)
    o_chg = (obv_w[-1] - obv_w[0]) / (abs(obv_w[0]) + 1)
    divergence = 0
    if o_chg > 0.05 and p_chg < 0.01:
        divergence = 20
    elif o_chg > 0.02 and p_chg < 0.05:
        divergence = 10

    accel = 0
    if len(obv_series) >= window + 5:
        short_slope = linear_slope(obv_series[-5:])
        if short_slope > slope * 1.1:
            accel = 10

    raw = 40 + norm_slope + divergence + accel
    return max(0, min(100, round(raw)))


def calc_rsi(closes, period=14):
    """Standard Wilder's RSI."""
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


# ─── ANALYSE A SINGLE STOCK ───────────────────────────────────────────────────

def analyse_ticker(ticker_raw, ticker_frame, obv_days=OBV_DAYS, lookback_days=LOOKBACK_DAYS, latest_date=None):
    """
    Apply screening criteria to an already-fetched price_cache frame (see
    that module - all three screeners share one cache now, refreshed once
    up front, so this function only does the (much lighter) market-cap/
    sector lookups over the network, not the price history itself).
    Returns dict or None if the ticker doesn't pass.

    Raw (auto_adjust=False) prices, matching HH SCREENER.py's fix - see
    price_cache.py's docstring for why.
    """
    sym = ticker_raw.upper().strip()
    yahoo_sym = sym if sym.endswith('.AX') else sym + '.AX'

    try:
        ticker_obj = yf.Ticker(yahoo_sym)

        try:
            info = ticker_obj.fast_info
            market_cap = getattr(info, 'market_cap', None) or 0
        except Exception:
            market_cap = 0

        if market_cap and market_cap < MIN_MARKET_CAP:
            return None

        min_needed = max(obv_days + 5, lookback_days + 5, SMA_LONG + 5)
        if ticker_frame is None or len(ticker_frame) < min_needed:
            return None

        # Same staleness guard as HH SCREENER.py (added there 2026-09-02,
        # ported here after the same bug pattern showed up in Pullback via
        # SUN - a stale-cache signal genuinely valid as of its last cached
        # session, but price had already moved on by the time this ran).
        if latest_date is not None and ticker_frame["date"].iloc[-1] < latest_date:
            return None

        closes  = ticker_frame['close'].tolist()
        opens   = ticker_frame['open'].tolist()
        highs   = ticker_frame['high'].tolist()
        lows    = ticker_frame['low'].tolist()
        volumes = ticker_frame['volume'].tolist()
        dates   = [d.strftime('%Y-%m-%d') for d in ticker_frame['date'].tolist()]

        price = closes[-1]

        # Filter: price >= 5 cents
        if price < MIN_PRICE:
            return None

        # Filter: market cap >= $50M (fallback via shares outstanding)
        if market_cap == 0:
            try:
                shares = getattr(info, 'shares', None) or 0
                market_cap = shares * price if shares else 0
            except Exception:
                market_cap = 0
        if market_cap > 0 and market_cap < MIN_MARKET_CAP:
            return None

        # Filter: 30-day volume >= 50,000. Median, not mean: a single spike
        # day (a stock otherwise dead most of the month) can drag a 30-day
        # AVERAGE above threshold even when it barely trades - found
        # 2026-09-02 on HH SCREENER.py's identical filter (RAU: mean
        # 49,659 cleared its 20,000 filter; median 0, since 16 of its last
        # 30 days had zero volume). avg_vol (the true mean) is kept too,
        # only for the displayed stat below - the threshold check uses the
        # median.
        avg_vol = sum(volumes[-30:]) / min(30, len(volumes))
        median_vol = statistics.median(volumes[-30:])
        if median_vol < MIN_AVG_VOLUME:
            return None

        # ── Criterion: Rising OBV over 30 days ────────────────────────────
        obv = calc_obv(closes, volumes)
        obv_window = obv[-obv_days:]
        obv_slope = linear_slope(obv_window)
        if obv_slope <= 0:
            return None

        score = obv_score(obv, closes, window=obv_days)

        # ── Criterion: Price has NOT broken the 30-day high ──────────────
        n_day_high = max(highs[-(lookback_days + 1):-1])
        if price >= n_day_high:
            return None

        gap_pct = (n_day_high - price) / n_day_high * 100  # info column only

        # ── Criterion: RSI between 45 and 70 ──────────────────────────────
        rsi = calc_rsi(closes, period=RSI_PERIOD)
        if rsi is None or not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        # ── Criterion: price above 20-day and 50-day SMA ──────────────────
        sma20 = calc_sma(closes, SMA_SHORT)
        sma50 = calc_sma(closes, SMA_LONG)
        if sma20 is None or sma50 is None:
            return None
        if price <= sma20 or price <= sma50:
            return None

        # ── Extra info metrics ─────────────────────────────────────────────
        prev_close = closes[-2]
        change_1d = (price - prev_close) / prev_close * 100

        vol_today = volumes[-1]
        vol_avg20 = sum(volumes[-20:]) / min(20, len(volumes))
        vol_ratio = vol_today / (vol_avg20 + 1)

        obv_chg_pct = (obv_window[-1] - obv_window[0]) / (abs(obv_window[0]) + 1) * 100

        # Sector is only fetched for tickers that already passed every other
        # filter - yfinance's full .info scrape is much slower than
        # fast_info, so doing this for every ticker in a 300-stock bulk scan
        # would be costly. Only the handful of actual results pay that cost.
        try:
            sector = ticker_obj.info.get('sector') or 'Other'
        except Exception:
            sector = 'Other'

        trim = slice(-CHART_TRIM_BARS, None)

        return {
            'ticker':       sym,
            'sector':       sector,
            'price':        round(price, 4),
            'market_cap':   int(market_cap) if market_cap else 0,
            'change_1d':    round(change_1d, 2),
            'obv_score':    score,
            'obv_chg_pct':  round(obv_chg_pct, 1),
            'rsi':          round(rsi, 1),
            'sma20':        round(sma20, 4),
            'sma50':        round(sma50, 4),
            'thirty_day_high': round(n_day_high, 4),
            'gap_pct':      round(gap_pct, 2),
            'vol_ratio':    round(vol_ratio, 2),
            'avg_vol':      int(avg_vol),
            'dates':        dates[trim],
            'opens':        [round(v, 4) for v in opens[trim]],
            'highs':        [round(v, 4) for v in highs[trim]],
            'lows':         [round(v, 4) for v in lows[trim]],
            'closes':       [round(v, 4) for v in closes[trim]],
            'volumes':      [int(v) for v in volumes[trim]],
        }

    except Exception:
        return None


# ─── SCAN ALL TICKERS ─────────────────────────────────────────────────────────

def run_scan(tickers, obv_days=OBV_DAYS, workers=DEFAULT_WORKERS):
    total = len(tickers)
    t0 = time.time()

    print(f"\n🔄 Refreshing shared price cache for {total} ASX tickers | {workers} threads\n")
    cache, fetched_ok, _ = price_cache.refresh_cache(tickers, workers=workers, max_history=HISTORY_PERIOD)
    min_needed = max(obv_days + 5, LOOKBACK_DAYS + 5, SMA_LONG + 5)
    usable = price_cache.count_usable(cache, tickers, min_days=min_needed)
    latest_date = cache["date"].max() if not cache.empty else None
    print(f"   Cache refresh done in {time.time()-t0:.1f}s  |  Fresh this run: {fetched_ok}/{total}  |  Usable overall: {usable}/{total}")

    results = []
    failed = 0
    done = 0
    t1 = time.time()

    print(f"\n🔍 Scoring {total} ASX tickers  |  {workers} threads  |  OBV window: {obv_days}d\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyse_ticker, t, price_cache.get_ticker_frame(cache, t), obv_days, latest_date=latest_date): t for t in tickers}

        for fut in as_completed(futures):
            done += 1
            tick = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    flag = f"  ✅ {tick:8s}  score={res['obv_score']:3d}  rsi={res['rsi']:5.1f}  gap={res['gap_pct']:5.1f}%"
                else:
                    flag = f"  ·  {tick}"
            except Exception as e:
                failed += 1
                flag = f"  ✗  {tick}  ({e})"

            elapsed = time.time() - t1
            rate    = done / elapsed if elapsed > 0 else 0
            eta     = (total - done) / rate if rate > 0 else 0

            sys.stdout.write(
                f"\r  [{done:4d}/{total}]  {rate:4.1f}/s  ETA {eta:4.0f}s  "
                f"Signals: {len(results):3d}   "
            )
            sys.stdout.flush()

            if done % 50 == 0:
                time.sleep(RATE_LIMIT_SLEEP)

    print(f"\n\n✅ Scan complete in {time.time()-t0:.1f}s")
    print(f"   Scanned: {total}  |  Signals: {len(results)}  |  Got real data: {fetched_ok}  |  Failed: {failed}")
    return results, usable


# ─── HTML DASHBOARD ────────────────────────────────────────────────────────────
# The card-grid/mini-chart dashboard itself lives in dashboard_template.py,
# shared with PULLBACK SCREENER.py. This just adapts this screener's result
# dicts into the shared card schema.

def build_html_report(results, excluded, total_scanned, out_path):
    from dashboard_template import render_dashboard_html

    def to_card(r):
        return {
            'ticker': r['ticker'],
            'sector': r['sector'],
            'market_cap': r['market_cap'],
            'price': r['price'],
            'change_1d': r['change_1d'],
            'score': r['obv_score'],
            'stats': [
                {'label': 'RSI', 'value': f"{r['rsi']:.0f}"},
                {'label': 'Gap', 'value': f"{r['gap_pct']:.1f}%"},
                {'label': 'Vol', 'value': f"{r['vol_ratio']:.1f}×"},
            ],
            'dates': r['dates'], 'opens': r['opens'], 'highs': r['highs'],
            'lows': r['lows'], 'closes': r['closes'], 'volumes': r['volumes'],
        }

    render_dashboard_html(
        cards=[to_card(r) for r in results],
        excluded_cards=[to_card(r) for r in excluded],
        total_scanned=total_scanned,
        title='ASX PRE-BREAKOUT SCREENER',
        subtitle='ASX stocks quietly building strength before a breakout — not stocks already breaking out.',
        footer_note=(
            'ASX Pre-Breakout Screener · Data via Yahoo Finance (yfinance), ticker universe via SeaBee/Market Index<br>'
            'Criteria: rising OBV (30d) AND price below 30-day high AND RSI 45-70 AND price above 20d &amp; 50d SMA '
            'AND price ≥ $0.05 AND market cap ≥ $50M AND 30d avg volume ≥ 50,000.'
        ),
        out_path=out_path + '.html',
    )
    print(f"  📄 HTML report → {out_path}.html")


def build_csv(results, out_path):
    # exclude the embedded chart-series arrays (dates/opens/highs/lows/closes/
    # volumes) from the CSV - those are for the HTML dashboard's mini charts,
    # not useful as spreadsheet columns.
    array_fields = {'dates', 'opens', 'highs', 'lows', 'closes', 'volumes'}
    fieldnames = ['ticker', 'sector', 'price', 'market_cap', 'change_1d', 'obv_score',
                  'obv_chg_pct', 'rsi', 'sma20', 'sma50', 'thirty_day_high',
                  'gap_pct', 'vol_ratio', 'avg_vol']
    if results:
        fieldnames = [k for k in results[0].keys() if k not in array_fields]
    with open(out_path + '.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow({k: v for k, v in r.items() if k not in array_fields})
    print(f"  📊 CSV export  → {out_path}.csv")


def build_tradingview_watchlist(results, out_path):
    """
    Export a plain-text, comma-separated list of tickers prefixed with
    'ASX:' so it can be pasted directly into TradingView's
    Watchlist -> Import list feature.
    """
    tv_tickers = [f"ASX:{r['ticker']}" for r in results]
    with open(out_path + '_tradingview_watchlist.txt', 'w', encoding='utf-8') as f:
        f.write(','.join(tv_tickers))
    print(f"  📥 TradingView watchlist → {out_path}_tradingview_watchlist.txt")


# ─── COOLDOWN / SEEN-TICKER HISTORY ───────────────────────────────────────────

def _weekdays_between(d1, d2):
    """
    Approximate trading-day count between two dates (Mon-Fri only). This
    ignores public holidays - a full ASX trading calendar would be needed for
    perfect accuracy, but for a cooldown filter this is close enough (off by
    at most a day or two around a holiday, never enough to matter here).
    """
    if d2 < d1:
        d1, d2 = d2, d1
    days = 0
    cur = d1
    while cur < d2:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon=0 ... Fri=4
            days += 1
    return days


def load_seen_history():
    """Load the {ticker: last_seen_date} history from disk. Returns {} if the
    file doesn't exist yet (e.g. first ever run) or can't be read."""
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
    """Tickers that appeared in results within the last `cooldown_days`
    trading days, and should be skipped this run's output."""
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
    """Mark every ticker that made it into today's final results as 'seen
    today'. Only tickers that actually PASSED every criterion get logged -
    not every ticker the script merely scanned internally."""
    today_str = date.today().strftime('%Y-%m-%d')
    for r in results:
        history[r['ticker']] = today_str
    return history


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ASX Pre-Breakout Screener')
    parser.add_argument('--days', type=int, default=OBV_DAYS,
                        help=f'OBV / lookback window in trading days (default: {OBV_DAYS})')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                        help=f'Parallel threads (default: {DEFAULT_WORKERS})')
    parser.add_argument('--out', type=str, default='asx_breakout_results',
                        help='Output filename without extension (default: asx_breakout_results)')
    parser.add_argument('--tickers', type=str, default='',
                        help='Comma-separated ticker list (overrides full ASX scan)')
    parser.add_argument('--no-open', action='store_true',
                        help="Don't auto-open the HTML report in a browser tab when done")
    parser.add_argument('--min-vol', type=int, default=MIN_AVG_VOLUME,
                        help=f'Min 30-day avg daily volume filter (default: {MIN_AVG_VOLUME})')
    parser.add_argument('--min-price', type=float, default=MIN_PRICE,
                        help=f'Min price filter in dollars (default: {MIN_PRICE})')
    parser.add_argument('--min-mcap', type=float, default=MIN_MARKET_CAP,
                        help=f'Min market cap in dollars (default: {MIN_MARKET_CAP:,.0f})')
    parser.add_argument('--fresh', action='store_true',
                        help=f'Ignore the {COOLDOWN_DAYS}-day cooldown and show ALL matching '
                             f'stocks, including ones you already saw recently')
    parser.add_argument('--cooldown', type=int, default=COOLDOWN_DAYS,
                        help=f'Trading days a stock stays hidden after last appearing (default: {COOLDOWN_DAYS}). Ignored if --fresh is used.')
    args = parser.parse_args()

    import __main__ as _m
    _m.MIN_PRICE = args.min_price
    _m.MIN_MARKET_CAP = args.min_mcap
    _m.MIN_AVG_VOLUME = args.min_vol

    print("=" * 60)
    print("  ASX PRE-BREAKOUT SCREENER")
    print("=" * 60)
    print(f"  Filters: price ≥ ${args.min_price:.2f}  |  mkt cap ≥ ${args.min_mcap/1e6:.0f}M  |  avg vol ≥ {args.min_vol:,}")
    print(f"  Criteria: OBV rising ({args.days}d) · below {args.days}d high · RSI {RSI_MIN}-{RSI_MAX} · price > 20d & 50d SMA")
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

    results, usable = run_scan(tickers, obv_days=args.days, workers=args.workers)

    # Circuit breaker: on a full-universe run, if the shared price cache
    # doesn't have usable data for most of the universe - whether from
    # today's fetches or an earlier run's still-recent ones - the pipeline
    # is broken (Yahoo Finance throttling the source IP with nothing decent
    # cached yet), not "a quiet day" - abort instead of publishing a
    # near-empty report. Skipped for --tickers test runs, which are too
    # small for this ratio to mean anything.
    if not args.tickers and len(tickers) >= MIN_UNIVERSE_FOR_CHECK:
        usable_ratio = usable / len(tickers)
        if usable_ratio < MIN_FETCH_RATIO:
            print(f"\n❌ Only {usable}/{len(tickers)} tickers ({usable_ratio:.0%}) have usable cached "
                  f"price data - likely Yahoo Finance throttling this machine/IP with nothing decent "
                  f"cached yet. Aborting WITHOUT writing a report, so the last good one stays live.")
            sys.exit(1)

    # Sort by score descending
    results.sort(key=lambda x: x['obv_score'], reverse=True)

    # ─── Cooldown filter: hide stocks you already reviewed recently ───────────
    # `skipped` also feeds the dashboard's "Show already seen" toggle, so it's
    # tracked even on a --fresh run (as an empty list) rather than left undefined.
    history = load_seen_history()
    skipped = []
    if args.fresh:
        print(f"\n  🔄 --fresh used: showing all results, ignoring cooldown history")
    else:
        excluded_tickers = get_excluded_tickers(history, args.cooldown)
        before_count = len(results)
        skipped = [r for r in results if r['ticker'] in excluded_tickers]
        results = [r for r in results if r['ticker'] not in excluded_tickers]
        if skipped:
            print(f"\n  🔁 Hid {len(skipped)} stock(s) already seen within the last {args.cooldown} trading days:")
            print(f"     {', '.join(sorted(r['ticker'] for r in skipped))}")
            print(f"     (use --fresh to include them, or --cooldown 0 to disable this)")

    # Log today's results (whatever's actually being shown) so tomorrow's run
    # knows to hide them - this happens even on a --fresh run, since you did
    # review them again just now.
    history = update_seen_history(history, results)
    save_seen_history(history)

    if results:
        print(f"\n{'─'*60}")
        print(f"  TOP SIGNALS\n")
        print(f"  {'TICKER':<8} {'SCORE':>5} {'RSI':>5} {'GAP%':>6}")
        print(f"  {'─'*8} {'─'*5} {'─'*5} {'─'*6}")
        for r in results[:20]:
            print(f"  {r['ticker']:<8} {r['obv_score']:>5} {r['rsi']:>5.1f} {r['gap_pct']:>5.1f}%")
        if len(results) > 20:
            print(f"  ... and {len(results)-20} more in the report")

    out_base  = os.path.join(SCRIPT_DIR, 'asx_breakout_results')
    html_path = out_base + '.html'
    csv_path  = out_base + '.csv'
    tv_path   = out_base + '_tradingview_watchlist.txt'

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
    csv_ok  = os.path.isfile(csv_path)
    tv_ok   = os.path.isfile(tv_path)

    print(f"\n{'='*60}")
    print(f"  {'✅' if html_ok else '❌'} HTML → {html_path}")
    print(f"  {'✅' if csv_ok  else '❌'} CSV  → {csv_path}")
    print(f"  {'✅' if tv_ok   else '❌'} TradingView watchlist → {tv_path}")
    print(f"{'='*60}")

    if html_ok and not args.no_open:
        try:
            import webbrowser
            webbrowser.open(html_path)
            print(f"\n  🌐 Opening HTML report in your browser...")
        except Exception:
            print(f"\n  Open manually: {html_path}")


def _pause_and_exit(code=0):
    """Always pause before exit so the terminal window stays open."""
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
