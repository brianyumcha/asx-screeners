#!/usr/bin/env python3
"""
Checks the small watchlist Morning Brief's "Today's Game Plan" card is
allowed to report on (ASX GICS sector indices + commodity ETFs/metals) for
two specific technical conditions: a fresh 20-day closing high/low, or
price sitting within ~2% of a Fibonacci retracement level computed from a
real recent swing. Pulls bars via yfinance (free, no desktop app, no API
key) instead of the TradingView MCP tools morning_brief_instructions.md
used to call directly - those need a live TradingView Desktop app running,
which blocked ever moving Morning Brief off a machine that has to stay
awake with that app open.

Swing-detection is the same fractal-pivot-on-closing-price method as the
Reversal (RSI) screener's find_pivots() (see build_reversal_rsi.py) -
reimplemented here standalone rather than imported, since that module
pulls in price_cache's full ~2000-ticker ASX universe machinery this
19-symbol watchlist doesn't need.

Run from /Users/briantam/Documents/Trading:
    python3 morning_brief_technicals.py
Prints one line per symbol that actually qualifies, or "Nothing flagged
today" if none do - same standard as the card itself.
"""
import sys

import yfinance as yf

WATCHLIST = {
    "XMJ (Materials)": "^AXMJ",
    "XEJ (Energy)": "^AXEJ",
    "XFJ (Financials)": "^AXFJ",
    "XNJ (Industrials)": "^AXNJ",
    "XIJ (IT)": "^AXIJ",
    "XPJ (A-REIT)": "^AXPJ",
    "XSJ (Consumer Staples)": "^AXSJ",
    "XDJ (Consumer Discretionary)": "^AXDJ",
    "XUJ (Utilities)": "^AXUJ",
    "XTJ (Telcos)": "^AXTJ",
    "XHJ (Health Care)": "^AXHJ",
    "GDX": "GDX",
    "SIL": "SIL",
    "COPX": "COPX",
    "URA": "URA",
    "LIT": "LIT",
    "REMX": "REMX",
    "Gold": "GC=F",
    "Silver": "SI=F",
    "Copper": "HG=F",
}

LOOKBACK_DAYS = 20
PIVOT_LEFT = 5
PIVOT_RIGHT = 5
# Just the "golden pocket" levels a chart-reader would actually flag -
# 0.382/0.786 are common enough in a 4-level sweep that nearly every
# symbol lands near one of them by coincidence, which isn't selective.
FIB_LEVELS = [0.5, 0.618]
FIB_TOLERANCE = 0.02


def find_pivots(closes, left=PIVOT_LEFT, right=PIVOT_RIGHT):
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


def check_symbol(label, ticker):
    try:
        hist = yf.Ticker(ticker).history(period="9mo", interval="1d")
    except Exception as e:
        print(f"  (skipped {label}: {e})", file=sys.stderr)
        return []
    if hist is None or len(hist) < LOOKBACK_DAYS + 1:
        print(f"  (skipped {label}: not enough history)", file=sys.stderr)
        return []

    closes = hist["Close"].tolist()
    dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    last_close = closes[-1]
    last_date = dates[-1]
    findings = []

    # 1. Fresh 20-day closing high/low (trailing 20 closes BEFORE today vs today)
    trailing = closes[-(LOOKBACK_DAYS + 1):-1]
    if last_close > max(trailing):
        findings.append(
            f"{label} at {last_close:.2f} ({last_date}) - fresh 20-day closing high "
            f"(prior 20-day range topped out at {max(trailing):.2f})"
        )
    elif last_close < min(trailing):
        findings.append(
            f"{label} at {last_close:.2f} ({last_date}) - fresh 20-day closing low "
            f"(prior 20-day range bottomed at {min(trailing):.2f})"
        )

    # 2. Within ~2% of a Fibonacci retracement level of the most recent
    # confirmed swing. The swing runs from the OLDER of the last two
    # pivots to the NEWER one; retracement is measured back from the
    # newer pivot toward the older one (standard convention).
    highs_idx, lows_idx = find_pivots(closes)
    pivots = sorted(
        [(i, closes[i], "high") for i in highs_idx] + [(i, closes[i], "low") for i in lows_idx]
    )
    if len(pivots) >= 2:
        (i1, p1, k1), (i2, p2, k2) = pivots[-2], pivots[-1]
        if k1 != k2:  # a real swing needs one high and one low, not two of the same kind
            rng = abs(p2 - p1)
            if rng > 0:
                for level in FIB_LEVELS:
                    fib_price = p2 - rng * level if k2 == "high" else p2 + rng * level
                    dist_pct = abs(last_close - fib_price) / fib_price
                    if dist_pct <= FIB_TOLERANCE:
                        swing_desc = f"the {dates[i1]} {k1} ({p1:.2f}) to {dates[i2]} {k2} ({p2:.2f}) swing"
                        findings.append(
                            f"{label} at {last_close:.2f} ({last_date}) - {dist_pct*100:.1f}% from its "
                            f"{level:.3f} retrace of {swing_desc} (level at {fib_price:.2f})"
                        )
                        break
    return findings


def main():
    all_findings = []
    for label, ticker in WATCHLIST.items():
        all_findings.extend(check_symbol(label, ticker))
    if not all_findings:
        print("Nothing flagged today")
    else:
        for f in all_findings:
            print(f)


if __name__ == "__main__":
    main()
