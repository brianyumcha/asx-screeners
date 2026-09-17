"""Rebuilds sector-rotation.html: a market-cap-weighted view of which ASX
GICS sectors are leading/lagging the broader market, and whether that
leadership is accelerating or fading.

Why this is built from constituent stocks rather than the 11 raw ASX
sector index symbols (^AXMJ, ^AXEJ, etc.): rs_utils.py already tried
that for per-stock RS and dropped it entirely - those symbols reliably
fail to fetch on GitHub Actions runners (see rs_utils.py's
SECTOR_BENCHMARK docstring for the full investigation). Rather than
depend on the same known-broken data source, this script computes each
sector's return directly from its own constituent stocks' prices - data
that's already proven to fetch reliably (price_cache.parquet, refreshed
daily by scan.yml, ~95% universe coverage). No sector-index fetch at
all; the only live network call this script makes is for STW.AX, the
same liquid ASX 200 ETF proxy the rest of the site already relies on as
its market-wide benchmark (also proven reliable - see rs_utils.py).

Method: for each sector, take every constituent with a market cap above
the site-wide floor (HH SCREENER.py's MIN_MARKET_CAP) and enough price
history, and compute a market-cap-weighted average % change over each
lookback window. Weighting (not a simple average) means one tiny stock's
wild swing can't distort the sector's read - matches how real sector
indices work. The same windows are computed for STW.AX; each sector's
return minus STW.AX's return over the same window is its "relative"
performance - positive means the sector is beating the broader market.

The rotation quadrant plots 3-month relative performance (medium-term
position) against 1-month relative performance (recent momentum):
  Leading   (top-right):    ahead of the market and still accelerating
  Weakening (bottom-right): ahead of the market but recently cooling
  Lagging   (bottom-left):  behind the market and still falling further
  Improving (top-left):     behind the market but recently catching up
"""
import datetime
import json
import math

import price_cache

WINDOWS = [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126)]
TEMPLATE = "sector_rotation_template.html"
OUTPUT = "sector-rotation.html"
BENCHMARK_SYMBOL = "STW.AX"

DISPLAY_LABEL = {"Info Tech": "Tech"}


def pct_change_n(closes, n):
    if len(closes) < n + 1:
        return None
    base = closes[-1 - n]
    latest = closes[-1]
    if not base or math.isnan(base) or math.isnan(latest):
        return None
    return (latest - base) / base * 100


def esc_js(s):
    if not s:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def main():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hh", "HH SCREENER.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)

    universe = hh.get_asx_universe()
    cache = price_cache.load_cache()

    sectors = [s for s in hh.SECTOR_ORDER if s != "Other"]
    sector_constituents = {s: [] for s in sectors}

    for t, info in universe.items():
        if len(t) != 3:
            continue
        sector = info.get("sector")
        if sector not in sector_constituents:
            continue
        mcap = info.get("market_cap") or 0
        if mcap < hh.MIN_MARKET_CAP:
            continue
        frame = price_cache.get_ticker_frame(cache, t, min_days=130)
        if frame is None or len(frame) < 30:
            continue
        sector_constituents[sector].append((mcap, frame["close"].tolist()))

    print("Fetching benchmark (STW.AX)...")
    bench_df = price_cache._fetch_one(BENCHMARK_SYMBOL, "1y", verbose=True)
    # dropna: a live fetch mid-session can return a NaN close for today's
    # still-forming bar (see rs_utils.py's fetch_benchmark_series, same
    # fix) - constituents don't hit this since they come from the
    # already-committed price_cache.parquet, not a fresh fetch.
    bench_closes = bench_df["Close"].dropna().tolist() if bench_df is not None else []
    if not bench_closes:
        raise RuntimeError("Could not fetch STW.AX benchmark - aborting without writing a report.")

    bench_return = {label: pct_change_n(bench_closes, n) for label, n in WINDOWS}

    rows = []
    for sector, items in sector_constituents.items():
        if len(items) < 3:
            print(f"  ⚠ Skipping {sector}: only {len(items)} usable constituent(s)")
            continue
        row = {"sector": sector, "count": len(items)}
        for label, n in WINDOWS:
            weighted_sum, weight_used = 0.0, 0.0
            for mcap, closes in items:
                chg = pct_change_n(closes, n)
                if chg is None:
                    continue
                weighted_sum += mcap * chg
                weight_used += mcap
            row[label] = (weighted_sum / weight_used) if weight_used > 0 else None
        for label, _ in WINDOWS:
            if row[label] is not None and bench_return[label] is not None:
                row[f"{label}_rel"] = row[label] - bench_return[label]
            else:
                row[f"{label}_rel"] = None
        rows.append(row)

    rows.sort(key=lambda r: (r["1M_rel"] if r["1M_rel"] is not None else -999), reverse=True)
    print(f"Computed rotation for {len(rows)} sector(s).")

    def js_num(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "null"
        return str(round(v, 2))

    data_lines = []
    for r in rows:
        label = DISPLAY_LABEL.get(r["sector"], r["sector"])
        vals = ",".join(
            js_num(r[k]) for k in ["1W", "1M", "3M", "6M", "1W_rel", "1M_rel", "3M_rel", "6M_rel"]
        )
        data_lines.append(f'["{esc_js(label)}",{r["count"]},{vals}]')
    data_block = ",\n".join(data_lines)

    bench_vals = ",".join(js_num(bench_return[label]) for label, _ in WINDOWS)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    assert "<<<BENCH_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    html = html.replace("<<<BENCH_DATA>>>", bench_vals)
    build_date = datetime.date.today().strftime("%-d %b %Y")
    html = html.replace("<<<BUILD_DATE>>>", build_date)

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
