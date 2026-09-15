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
classify), so this doesn't need a persistent cache or a scan.yml hook -
it's a standalone weekday build like build_materials_index.py.

Runs on the same schedule as the other index builds (see
.github/workflows/materials_index.yml).
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
        return {"name": None, "mcap": None, "price": None, "change1d": None, "sector": None}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "mcap": info.get("marketCap") or info.get("nonDilutedMarketCap"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "change1d": info.get("regularMarketChangePercent"),
        "sector": info.get("sector"),
    }


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
        univ_entry = universe.get(t, {})
        sector = info.get("sector") or univ_entry.get("sector") or "Other"
        mcap = info.get("mcap") or univ_entry.get("market_cap")
        purchases.sort(key=lambda p: p["date"], reverse=True)
        most_recent = purchases[0]["date"]
        num_purchases = len(purchases)
        distinct_insiders = sorted({p["insider"] for p in purchases if p["insider"]})
        total_value = sum(p["value"] for p in purchases if p["value"])
        total_shares = sum(p["shares"] for p in purchases if p["shares"])
        in_bull_map = t in bull_map_tickers
        rows.append((
            t, info.get("name"), sector, most_recent, num_purchases,
            len(distinct_insiders), distinct_insiders, total_value, total_shares,
            mcap, info.get("price"), info.get("change1d"), in_bull_map,
        ))
        if i % 50 == 0:
            print(f"  ...pass 2: {i}/{len(hits)} enriched")
        time.sleep(0.1)

    print(f"Done. {len(rows)} tickers with director purchases in the last {LOOKBACK_DAYS} days.")
    bull_map_overlap = sum(1 for r in rows if r[12])
    print(f"Bull Map overlap: {bull_map_overlap}/{len(rows)}.")

    data_lines = []
    for (t, name, sector, most_recent, num_purchases, num_insiders, insiders,
         total_value, total_shares, mcap, price, change1d, in_bull_map) in rows:
        mcap_s = str(int(mcap)) if mcap else "null"
        price_s = str(price) if price is not None else "null"
        chg_s = str(round(change1d, 1)) if change1d is not None else "null"
        value_s = str(int(total_value)) if total_value else "null"
        shares_s = str(int(total_shares)) if total_shares else "null"
        insiders_s = json.dumps(insiders)
        data_lines.append(
            f'["{esc_js(t)}","{esc_js(name)}","{esc_js(sector)}","{esc_js(most_recent)}",'
            f'{num_purchases},{num_insiders},{insiders_s},{value_s},{shares_s},'
            f'{mcap_s},{price_s},{chg_s},{"true" if in_bull_map else "false"}]'
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
