"""
Shared reader for call_cross_reference.json (built locally by
build_call_cross_reference.py, since it needs Google Drive access GitHub
Actions runners don't have - this just reads the committed JSON output,
cheap and dependency-free, safe for every screener script to import).

Surfaces, per ticker, whether Craig Dickson is currently bullish on it
(FA) and/or whether one of the 4 TA hosts (David Bird, Craig Tapping,
Jamie Bell, Frank Watkins) tagged it bullish within roughly the last two
weeks (TA) - see build_call_cross_reference.py's docstring for the exact
rules. Lets a technical screener badge a ticker when an educator has
also flagged it, without re-deriving any of that logic itself.
"""
import json
import os
from datetime import datetime

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "call_cross_reference.json")


def _fmt_date(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %b")
    except Exception:
        return iso


def load_call_badges():
    """Returns {ticker: {'fa': 'FA bullish — 27 Oct' | None,
    'ta': 'TA bullish — 29 Oct' | None}}. Missing/corrupt file -> empty
    dict (every screener just shows no badges, never errors - this is
    supplementary cross-reference data, not core to any screener's own
    signal)."""
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    fa = data.get("fa_bullish", {})
    ta = data.get("ta_bullish", {})
    out = {}
    for t in set(fa) | set(ta):
        out[t] = {
            "fa": f"FA bullish — {_fmt_date(fa[t])}" if t in fa else None,
            "ta": f"TA bullish — {_fmt_date(ta[t])}" if t in ta else None,
        }
    return out
