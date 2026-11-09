"""
Builds call_cross_reference.json - per-ticker, whether Craig Dickson is
currently bullish on it (FA) and/or whether any of the 4 TA hosts (David
Bird, Craig Tapping, Jamie Bell, Frank Watkins) most recently tagged it
Bullish within roughly the last two weeks (TA). Lets the technical
screeners (HH/LL/Pullback/Momentum/OBV/Volume Anomaly/Wave2) badge a
ticker when an educator has also flagged it, without re-deriving any of
this logic themselves.

  - FA bullish: no freshness cutoff - this is Craig's CURRENT stance, not
    a recent-mention check, so it stays shown until he actually changes
    his mind. Reuses craig-bullish-map.html's own survivor logic
    (build_craig_bullish_map.py's load_tracker_records/derive_survivors)
    verbatim, so this never drifts from what that page itself shows.
  - TA bullish: freshness-gated to TA_BULLISH_WINDOW_DAYS, since a TA call
    goes stale fast (unlike an FA thesis, a chart-based bullish read from
    2 months ago says nothing about the setup today). Parsed from the
    call summary .txt files directly (not the tracker spreadsheet) via
    build_mtm_archive.py's own parse_file(), because that's what reliably
    attributes each call to its real host (from the filename) - the
    tracker's free-text "Call Name" column has far too many host-less or
    oddly-titled rows ("FMG: The FA to the EW to the TA", "Macro Sunday",
    "FA in Practice: Meet...") to regex a host out of reliably.

Local-only (both the call-summary folder and the tracker spreadsheet live
on Google Drive, which GitHub Actions runners don't have access to) -
like build_mtm_archive.py, this can only ever run on Brian's machine. Its
OUTPUT (this JSON) gets committed so every screener script, which DOES
run on GH Actions, can read it cheaply via call_badges.py without needing
Drive access itself.

Run manually, or alongside build_mtm_archive.py in run_mtm_wrapper.sh,
whenever new call summaries land - otherwise the badges just go stale
until the next run (silently, since TA bullish is already freshness-
gated and FA bullish only ever reflects whatever the last run saw).

Usage:
    python3 build_call_cross_reference.py
"""
import importlib.util
import json
import os
from datetime import date, timedelta

import build_craig_bullish_map as bullmap
from build_mtm_archive import FILENAME_RE, SRC_DIR, parse_file

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(SCRIPT_DIR, "call_cross_reference.json")
TA_BULLISH_WINDOW_DAYS = 14


def build_fa_bullish(universe):
    records = bullmap.load_tracker_records(universe)
    survivors = bullmap.derive_survivors(records)
    return {ticker: info["display"]["date"] for ticker, info in survivors.items()}


def strict_resolve_ticker(raw, universe):
    """Like bullmap.resolve_ticker(), but returns None (never a best-effort
    guess) when nothing validates against the live universe - needed here
    because call summary headings aren't always a single real ticker
    ("A-REITs (SCG, SGP, GPT, VCX, MGR, CHC, DXS)", "Gold", "XJO"); the Bull
    Map surfaces an unresolved entry for a human to fix, but this cache has
    no such review step, so anything that doesn't cleanly resolve just gets
    dropped rather than silently mis-attributed to the wrong code."""
    raw = (raw or "").strip()
    if not raw:
        return None
    bare = raw.upper()
    if bare in universe:
        return bare
    m = bullmap.PAREN_CODE_RE.search(raw)
    if m and m.group(1).upper() in universe:
        return m.group(1).upper()
    pre = raw.split("(")[0].strip().upper()
    if pre and pre in universe:
        return pre
    return None


def build_ta_bullish(universe):
    cutoff = (date.today() - timedelta(days=TA_BULLISH_WINDOW_DAYS)).isoformat()

    files = [f for f in os.listdir(SRC_DIR) if FILENAME_RE.match(f)]
    latest = {}  # ticker -> iso_date (keeps the most recent)
    skipped = 0
    for fname in files:
        path = os.path.join(SRC_DIR, fname)
        try:
            call = parse_file(path, fname)
        except Exception:
            skipped += 1
            continue
        if call["call_type"] != "TA" or call["iso_date"] < cutoff:
            continue
        for raw_ticker, bias, _tf, _ts, _bullets in call["sections"]:
            if bias != "Bullish":
                continue
            ticker = strict_resolve_ticker(raw_ticker, universe)
            if not ticker:
                continue
            if ticker not in latest or call["iso_date"] > latest[ticker]:
                latest[ticker] = call["iso_date"]
    if skipped:
        print(f"  (skipped {skipped} unparseable file(s))")
    return latest


def main():
    spec = importlib.util.spec_from_file_location("hh", os.path.join(SCRIPT_DIR, "HH SCREENER.py"))
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)
    universe = hh.get_asx_universe()

    fa_bullish = build_fa_bullish(universe)
    ta_bullish = build_ta_bullish(universe)

    data = {"fa_bullish": fa_bullish, "ta_bullish": ta_bullish}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0, separators=(",", ":"), sort_keys=True)

    print(f"FA bullish (Craig's current stance): {len(fa_bullish)} ticker(s)")
    print(f"TA bullish (last {TA_BULLISH_WINDOW_DAYS}d, Dave/Craig T/Jamie/Frank): {len(ta_bullish)} ticker(s)")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
