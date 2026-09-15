"""Rebuilds insider-index.html from scratch: scans the full ASX universe
for genuine on-market director share purchases (ASX Appendix 3Y "Change of
Director's Interest Notice" filings, as aggregated by Yahoo Finance), and
renders the static page from insider_index_template.html.

Two-pass design to keep runtime reasonable across ~1900 tickers:
  Pass 1 fetches only `insider_transactions` (cheap) for every ticker and
  filters to genuine "Purchase" events within the lookback window - most
  tickers have none, so this pass alone decides what's worth spending a
  second network call on.
  Pass 2 fetches `.info` (name/mcap/price/etc.) only for the tickers that
  survived pass 1.

Unlike Materials/Healthcare/Energy/Tech, there's no keyword classification
step here (insider_transactions IS the live signal, not something to
classify), so this doesn't need a persistent cache or a scan.yml hook.

Runs weekly, not daily like the other index builds - it now fetches
each hit ticker's insider_roster_holders too (for stake-delta/%-of-
company), which pushes a full run to ~30-45min, and the 180-day
lookback window means the dataset barely moves day to day anyway. See
.github/workflows/insider_index.yml (split out from materials_index.yml
since its runtime was holding up that job's daily deploy).
"""
import datetime
import json
import re
import sys
import time

import yfinance as yf

TEMPLATE = "insider_index_template.html"
OUTPUT = "insider-index.html"
BULL_MAP_PATH = "craig-bullish-map.html"

LOOKBACK_DAYS = 180


def load_bull_map_tickers():
    try:
        with open(BULL_MAP_PATH) as f:
            content = f.read()
    except FileNotFoundError:
        return set()
    return set(re.findall(r"\['([A-Z0-9]+)','[^']*',", content))


def fetch_recent_purchases(ticker, cutoff):
    """Pass 1: cheap check - does this ticker have any genuine on-market
    purchase within the lookback window? Returns a list of purchase dicts
    (empty if none/no data/error)."""
    try:
        df = yf.Ticker(ticker + ".AX").insider_transactions
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []
    df = df.copy()
    df["Text"] = df["Text"].fillna("")
    buys = df[df["Text"].str.startswith("Purchase")].copy()
    if len(buys) == 0:
        return []
    buys["Start Date"] = __import__("pandas").to_datetime(buys["Start Date"], errors="coerce")
    recent = buys[buys["Start Date"] >= cutoff]
    out = []
    for _, r in recent.iterrows():
        out.append({
            "date": r["Start Date"].strftime("%Y-%m-%d") if r["Start Date"] is not None and str(r["Start Date"]) != "NaT" else None,
            "insider": r.get("Insider") or "",
            "position": r.get("Position") or "",
            "shares": int(r["Shares"]) if r.get("Shares") == r.get("Shares") else None,  # NaN check
            "value": float(r["Value"]) if r.get("Value") == r.get("Value") else None,
        })
    return [o for o in out if o["date"] is not None]


def fetch_company_info(ticker):
    try:
        info = yf.Ticker(ticker + ".AX").get_info()
    except Exception:
        return {"name": None, "mcap": None, "price": None, "change1d": None, "sector": None, "shares_out": None}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "mcap": info.get("marketCap") or info.get("nonDilutedMarketCap"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "change1d": info.get("regularMarketChangePercent"),
        "sector": info.get("sector"),
        "shares_out": info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"),
    }


def fetch_insider_roster(ticker):
    """Current total 'Shares Owned Directly' per insider, as of their most
    recent reported transaction - this is a resulting-balance disclosure
    (ASX Appendix 3Y convention: filings report the holding AFTER a change,
    not before), so treated as the holding *after* their latest purchase."""
    try:
        df = yf.Ticker(ticker + ".AX").insider_roster_holders
    except Exception:
        return {}
    if df is None or len(df) == 0:
        return {}
    out = {}
    for _, r in df.iterrows():
        name = r.get("Name")
        shares = r.get("Shares Owned Directly")
        if name and shares == shares:  # NaN check
            out[name] = int(shares)
    return out


def esc_js(s):
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    sys.path.insert(0, ".")
    import importlib.util
    spec = importlib.util.spec_from_file_location("hh", "HH SCREENER.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)

    universe = hh.get_asx_universe()
    tickers = sorted(t for t in universe if len(t) == 3)
    print(f"Scanning {len(tickers)} tickers for director purchases (last {LOOKBACK_DAYS} days)...")

    import pandas as pd
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=LOOKBACK_DAYS)

    bull_map_tickers = load_bull_map_tickers()
    print(f"Loaded {len(bull_map_tickers)} Bull Map tickers for cross-reference.")

    hits = {}
    for i, t in enumerate(tickers, 1):
        purchases = fetch_recent_purchases(t, cutoff)
        if purchases:
            hits[t] = purchases
        if i % 200 == 0:
            print(f"  ...pass 1: {i}/{len(tickers)} scanned, {len(hits)} hits so far")
        time.sleep(0.08)

    print(f"Pass 1 complete: {len(hits)} tickers with a recent purchase.")

    rows = []
    for i, (t, purchases) in enumerate(sorted(hits.items()), 1):
        info = fetch_company_info(t)
        roster = fetch_insider_roster(t)
        univ_entry = universe.get(t, {})
        # Universe sector (SeaBee industry -> rs_utils.SECTOR_MAP) takes
        # priority - it's the same GICS-style taxonomy every other page on
        # the site uses (Consumer Discretionary/Staples, Info Tech,
        # Financials, etc). Only fall back to yfinance's raw .info sector
        # (Yahoo's own taxonomy - Consumer Cyclical/Defensive, Technology,
        # Financial Services) for the handful of tickers missing from the
        # universe fetch, so at least something is shown.
        sector = univ_entry.get("sector") or info.get("sector") or "Other"
        mcap = info.get("mcap") or univ_entry.get("market_cap")
        shares_out = info.get("shares_out")
        purchases.sort(key=lambda p: p["date"], reverse=True)
        most_recent = purchases[0]["date"]
        num_purchases = len(purchases)
        distinct_insiders = sorted({p["insider"] for p in purchases if p["insider"]})
        total_value = sum(p["value"] for p in purchases if p["value"])
        total_shares = sum(p["shares"] for p in purchases if p["shares"])
        in_bull_map = t in bull_map_tickers

        # Per-insider stake-increase %: this insider's shares bought (in the
        # lookback window) vs. their prior holding, estimated as their
        # current roster balance minus what they just bought (roster is a
        # resulting/after-transaction balance - see fetch_insider_roster).
        shares_bought_by = {}
        for p in purchases:
            if p["insider"] and p["shares"]:
                shares_bought_by[p["insider"]] = shares_bought_by.get(p["insider"], 0) + p["shares"]

        top_insider, top_pct_increase, top_current_shares = None, None, None
        for name, bought in shares_bought_by.items():
            current = roster.get(name)
            if current is None:
                continue
            prior = current - bought
            if prior <= 0:
                continue  # can't compute a sane % increase (e.g. brand-new holder)
            pct = bought / prior * 100
            if top_pct_increase is None or pct > top_pct_increase:
                top_insider, top_pct_increase, top_current_shares = name, pct, current

        value_pct_mc = (total_value / mcap * 100) if (total_value and mcap) else None
        stake_pct_co = (top_current_shares / shares_out * 100) if (top_current_shares and shares_out) else None

        rows.append((
            t, info.get("name"), sector, most_recent, num_purchases,
            len(distinct_insiders), distinct_insiders, total_value, total_shares,
            mcap, info.get("price"), info.get("change1d"), in_bull_map,
            value_pct_mc, stake_pct_co, top_pct_increase, top_insider,
        ))
        if i % 50 == 0:
            print(f"  ...pass 2: {i}/{len(hits)} enriched")
        time.sleep(0.12)

    print(f"Done. {len(rows)} tickers with director purchases in the last {LOOKBACK_DAYS} days.")
    bull_map_overlap = sum(1 for r in rows if r[12])
    print(f"Bull Map overlap: {bull_map_overlap}/{len(rows)}.")

    data_lines = []
    for (t, name, sector, most_recent, num_purchases, num_insiders, insiders,
         total_value, total_shares, mcap, price, change1d, in_bull_map,
         value_pct_mc, stake_pct_co, top_pct_increase, top_insider) in rows:
        mcap_s = str(int(mcap)) if mcap else "null"
        price_s = str(price) if price is not None else "null"
        chg_s = str(round(change1d, 1)) if change1d is not None else "null"
        value_s = str(int(total_value)) if total_value else "null"
        shares_s = str(int(total_shares)) if total_shares else "null"
        insiders_s = json.dumps(insiders)
        value_pct_mc_s = str(round(value_pct_mc, 3)) if value_pct_mc is not None else "null"
        stake_pct_co_s = str(round(stake_pct_co, 3)) if stake_pct_co is not None else "null"
        top_pct_increase_s = str(round(top_pct_increase, 1)) if top_pct_increase is not None else "null"
        top_insider_s = json.dumps(top_insider) if top_insider else "null"
        data_lines.append(
            f'["{esc_js(t)}","{esc_js(name)}","{esc_js(sector)}","{esc_js(most_recent)}",'
            f'{num_purchases},{num_insiders},{insiders_s},{value_s},{shares_s},'
            f'{mcap_s},{price_s},{chg_s},{"true" if in_bull_map else "false"},'
            f'{value_pct_mc_s},{stake_pct_co_s},{top_pct_increase_s},{top_insider_s}]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    build_date = datetime.date.today().strftime("%-d %b %Y")
    html = html.replace("<<<BUILD_DATE>>>", build_date)
    html = html.replace("<<<LOOKBACK_DAYS>>>", str(LOOKBACK_DAYS))

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
