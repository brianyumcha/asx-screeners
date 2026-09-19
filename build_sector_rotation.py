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

Each sector also carries a trailing tail: the same (3M relative, 1M
relative) position recomputed as of TAIL_WEEKS weekly snapshots back,
so the chart shows the actual rotation path (like a real RRG's tail),
not just today's static point. Started at 8 weeks; the first real render
(2026-09-17) showed several sectors' tails swinging across most of the
chart's width, drowning out the current-position dots - raw relative
returns are noisier than a real RRG's smoothed/normalized RS-Ratio line,
so a shorter tail reads much cleaner. Dropped to 4 weeks. Still a single
constant if it ever needs revisiting.

Below the quadrant/table, a second chart plots every sector's own
cumulative % change (not relative to XJO - each sector's own absolute
move) on one shared scale, indexed to 0% at the start of whichever
lookback the viewer picks (4W/8W/12W toggle, default 8W), plus XJO
itself as a dashed reference line - the "who's actually up or down, and
by how much" view the quadrant's relative-performance framing doesn't
answer directly. To support that toggle without re-fetching per click,
this script bakes cumulative_series() out to LINE_CHART_MAX_WEEKS (the
longest option) and the browser re-baselines/slices down to whatever
shorter window is selected - see sector_rotation_template.html's
setLineWeeks(). x-axis labels are real trading dates (STW.AX's own
DatetimeIndex, since every series shares the same ASX trading calendar),
not "Nw ago" offsets.
"""
import datetime
import json
import math

import price_cache

WINDOWS = [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126)]
TEMPLATE = "sector_rotation_template.html"
OUTPUT = "sector-rotation.html"
BENCHMARK_SYMBOL = "STW.AX"
TAIL_WEEKS = 4
TAIL_STEP_DAYS = 5  # 1 trading week
LINE_CHART_DEFAULT_WEEKS = 8
LINE_CHART_MAX_WEEKS = 12
LINE_CHART_MAX_DAYS = LINE_CHART_MAX_WEEKS * TAIL_STEP_DAYS

DISPLAY_LABEL = {"Info Tech": "Tech"}


def pct_change_at(closes, offset, n):
    """% change over an n-bar window ending `offset` trading days before
    the most recent bar (offset=0 -> the window ending today) - the same
    calculation pct_change_n did, generalised so the tail can ask for the
    same window as of an earlier date without re-slicing every caller."""
    end_idx = len(closes) - 1 - offset
    start_idx = end_idx - n
    if start_idx < 0 or end_idx < 0:
        return None
    base = closes[start_idx]
    latest = closes[end_idx]
    if not base or math.isnan(base) or math.isnan(latest):
        return None
    return (latest - base) / base * 100


def pct_change_n(closes, n):
    return pct_change_at(closes, 0, n)


def weighted_relative(items, bench_closes, offset):
    """Market-cap-weighted 1M/3M relative-to-benchmark return for one
    sector, as of `offset` trading days before today. Returns (rel_1m,
    rel_3m), either possibly None if there isn't enough history that far
    back for either side."""
    result = {}
    for label, n in (("1M", 21), ("3M", 63)):
        weighted_sum, weight_used = 0.0, 0.0
        for mcap, closes in items:
            chg = pct_change_at(closes, offset, n)
            if chg is None:
                continue
            weighted_sum += mcap * chg
            weight_used += mcap
        sector_ret = (weighted_sum / weight_used) if weight_used > 0 else None
        bench_ret = pct_change_at(bench_closes, offset, n)
        result[label] = (sector_ret - bench_ret) if (sector_ret is not None and bench_ret is not None) else None
    return result["1M"], result["3M"]


def cumulative_series(closes_list, days, weights=None):
    """Market-cap-weighted cumulative % change series, one point per
    trading day from `days` ago (always 0%, the indexed baseline) through
    today inclusive - length days+1. `closes_list` is a list of individual
    tickers' close-price lists; `weights` (market caps) defaults to equal
    weight (used for the single-symbol benchmark case)."""
    if weights is None:
        weights = [1.0] * len(closes_list)
    series = []
    for d in range(days, -1, -1):
        weighted_sum, weight_used = 0.0, 0.0
        for w, closes in zip(weights, closes_list):
            end_idx = len(closes) - 1 - d
            start_idx = len(closes) - 1 - days
            if start_idx < 0 or end_idx < 0:
                continue
            base, cur = closes[start_idx], closes[end_idx]
            if not base or math.isnan(base) or math.isnan(cur):
                continue
            weighted_sum += w * (cur - base) / base * 100
            weight_used += w
        series.append((weighted_sum / weight_used) if weight_used > 0 else None)
    return series


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
    bench_close_series = bench_df["Close"].dropna() if bench_df is not None else None
    bench_closes = bench_close_series.tolist() if bench_close_series is not None else []
    if not bench_closes:
        raise RuntimeError("Could not fetch STW.AX benchmark - aborting without writing a report.")

    # Real trading dates for the line chart's x-axis, taken from the
    # benchmark's own DatetimeIndex - every sector's line_series shares
    # the same "N trading days ago" indexing, and they all trade on the
    # same ASX calendar, so one shared date axis is valid for all of them.
    line_dates = [d.strftime("%-d %b") for d in bench_close_series.index[-(LINE_CHART_MAX_DAYS + 1):]]

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

        tail = []
        for week in range(TAIL_WEEKS, -1, -1):  # oldest first, today (week=0) last
            offset = week * TAIL_STEP_DAYS
            rel1m, rel3m = weighted_relative(items, bench_closes, offset)
            tail.append((rel3m, rel1m))
        row["tail"] = tail

        weights = [mcap for mcap, _ in items]
        closes_list = [closes for _, closes in items]
        row["line_series"] = cumulative_series(closes_list, LINE_CHART_MAX_DAYS, weights=weights)

        rows.append(row)

    rows.sort(key=lambda r: (r["1M_rel"] if r["1M_rel"] is not None else -999), reverse=True)
    print(f"Computed rotation for {len(rows)} sector(s).")

    bench_line_series = cumulative_series([bench_closes], LINE_CHART_MAX_DAYS)

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
        tail_json = "[" + ",".join(f"[{js_num(rel3m)},{js_num(rel1m)}]" for rel3m, rel1m in r["tail"]) + "]"
        line_json = "[" + ",".join(js_num(v) for v in r["line_series"]) + "]"
        data_lines.append(f'["{esc_js(label)}",{r["count"]},{vals},{tail_json},{line_json}]')
    data_block = ",\n".join(data_lines)

    bench_vals = ",".join(js_num(bench_return[label]) for label, _ in WINDOWS)
    # NOT self-bracketed - matches bench_vals's convention above, since the
    # template's own `const BENCH_LINE = [<<<BENCH_LINE_DATA>>>];` supplies
    # the brackets. Self-bracketing here used to double-wrap the array
    # (`[[0,-0.33,...]]`), which silently broke the benchmark line (Y(v)
    # on an array is NaN) - found 2026-09-19 while adding the lookback
    # toggle, but present in every build before that too.
    bench_line_json = ",".join(js_num(v) for v in bench_line_series)
    line_dates_json = json.dumps(line_dates)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    assert "<<<BENCH_DATA>>>" in template, "template placeholder missing"
    assert "<<<BENCH_LINE_DATA>>>" in template, "template placeholder missing"
    assert "<<<LINE_CHART_MAX_WEEKS>>>" in template, "template placeholder missing"
    assert "<<<LINE_CHART_DEFAULT_WEEKS>>>" in template, "template placeholder missing"
    assert "<<<LINE_DATES_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    html = html.replace("<<<BENCH_DATA>>>", bench_vals)
    html = html.replace("<<<BENCH_LINE_DATA>>>", bench_line_json)
    html = html.replace("<<<LINE_CHART_MAX_WEEKS>>>", str(LINE_CHART_MAX_WEEKS))
    html = html.replace("<<<LINE_CHART_DEFAULT_WEEKS>>>", str(LINE_CHART_DEFAULT_WEEKS))
    html = html.replace("<<<LINE_DATES_DATA>>>", line_dates_json)
    build_date = datetime.date.today().strftime("%-d %b %Y")
    html = html.replace("<<<BUILD_DATE>>>", build_date)

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
