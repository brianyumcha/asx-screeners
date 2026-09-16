"""Builds conviction-stack.html: cross-references every independent signal
this site tracks - insider buying, Craig's FA bull map, HH breakout, OBV
pre-breakout, Pullback (Zag Zone) - into one ranked view of which tickers
currently have the most signals agreeing.

Runs as the last step of scan.yml, after OBV/Pullback/HH have already
written their local result files this run and insider-index.html/
craig-bullish-map.html are already on disk (git-tracked, restored by
checkout) - reads local files only, no extra network calls, so it's
always in sync with whatever this same run just produced.

A ticker only appears here with 2+ signals - a single-signal ticker is
just "on one list", not a conviction stack (and with insider buying alone
covering 500+ tickers, including every 1-signal ticker would bury the
signal in noise rather than surface it).

Signal definitions (each independent of the others):
  - Insider Buying: has any row in insider-index.html (already scoped to
    the last 180 days by that tracker itself).
  - FA Pick: ticker appears anywhere in Craig's current bull map.
  - HH Breakout: hh_daily or hh_weekly fired on this run - a fresh new-high
    signal, not just "still sitting at an old high" (see high_tier, which
    persists longer and isn't used here for that reason).
  - Pre-Breakout (OBV): ticker passed the OBV screener's own criteria this
    run (rising OBV, below 30d high, RSI 45-70, etc).
  - Pullback (Zag Zone): ticker passed the Pullback screener's own
    criteria this run (in the 38.2-61.8% retracement zone).
"""
import datetime
import json
import re

TEMPLATE = "conviction_stack_template.html"
OUTPUT = "conviction-stack.html"

HH_FILE = "asx_hh_results.html"
OBV_FILE = "asx_breakout_results.html"
PULLBACK_FILE = "asx_pullback_results.html"
INSIDER_FILE = "insider-index.html"
BULLMAP_FILE = "craig-bullish-map.html"

BULLMAP_ENTRY_RE = re.compile(
    r"\['([A-Z0-9]{1,5})',\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)',\s*'(\d{4}-\d{2}-\d{2})'\]"
)


def load_json_data(path, varname="DATA"):
    with open(path) as f:
        html = f.read()
    m = re.search(rf"const {varname} = (.*?);\n", html)
    if not m:
        raise ValueError(f"couldn't find `const {varname} = ...;` in {path}")
    return json.loads(m.group(1))


def load_insider_data(path):
    with open(path) as f:
        html = f.read()
    m = re.search(r"const DATA = \[(.*?)\n\];", html, re.S)
    if not m:
        raise ValueError(f"couldn't find insider DATA array in {path}")
    return json.loads("[" + m.group(1) + "]")


def load_bullmap_tickers(path):
    """Returns {ticker: latest_call_date} across every commodity/theme group -
    a ticker can appear in multiple groups over time, so this keeps the max
    date seen for it."""
    with open(path) as f:
        html = f.read()
    latest = {}
    for ticker, _name, _commentary, date in BULLMAP_ENTRY_RE.findall(html):
        if ticker not in latest or date > latest[ticker]:
            latest[ticker] = date
    return latest


def esc_js(s):
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    hh_rows = load_json_data(HH_FILE)
    obv_rows = load_json_data(OBV_FILE)
    pullback_rows = load_json_data(PULLBACK_FILE)
    insider_rows = load_insider_data(INSIDER_FILE)
    bullmap = load_bullmap_tickers(BULLMAP_FILE)

    print(f"Loaded: {len(hh_rows)} HH, {len(obv_rows)} OBV, {len(pullback_rows)} Pullback, "
          f"{len(insider_rows)} Insider, {len(bullmap)} FA Pick tickers.")

    # ticker -> accumulated info
    info = {}

    def get(t):
        if t not in info:
            info[t] = {
                "name": None, "sector": None, "mcap": None, "price": None, "change1d": None,
                "insider": False, "insider_date": None,
                "fapick": False, "fapick_date": None,
                "hh": False, "hh_tier": None,
                "obv": False,
                "pullback": False,
            }
        return info[t]

    for r in hh_rows:
        if not (r.get("hh_daily") or r.get("hh_weekly")):
            continue
        d = get(r["ticker"])
        d["hh"] = True
        d["hh_tier"] = r.get("high_tier")
        d["name"] = d["name"] or r.get("name")
        d["sector"] = d["sector"] or r.get("sector")
        d["mcap"] = d["mcap"] or r.get("market_cap")
        d["price"] = d["price"] or r.get("price")
        d["change1d"] = d["change1d"] if d["change1d"] is not None else r.get("change_1d")

    for r in obv_rows:
        d = get(r["ticker"])
        d["obv"] = True
        d["sector"] = d["sector"] or r.get("sector")
        d["mcap"] = d["mcap"] or (r.get("market_cap") or None)
        d["price"] = d["price"] or r.get("price")
        d["change1d"] = d["change1d"] if d["change1d"] is not None else r.get("change_1d")

    for r in pullback_rows:
        d = get(r["ticker"])
        d["pullback"] = True
        d["sector"] = d["sector"] or r.get("sector")
        d["mcap"] = d["mcap"] or (r.get("market_cap") or None)
        d["price"] = d["price"] or r.get("price")
        d["change1d"] = d["change1d"] if d["change1d"] is not None else r.get("change_1d")

    for row in insider_rows:
        tk, name, sector, lastdate, _numpurchases, _numinsiders, _insiders, _value, _shares, mcap, price, change1d = row[:12]
        d = get(tk)
        d["insider"] = True
        d["insider_date"] = lastdate
        d["name"] = d["name"] or name
        d["sector"] = d["sector"] or sector
        d["mcap"] = d["mcap"] or mcap
        d["price"] = d["price"] or price
        d["change1d"] = d["change1d"] if d["change1d"] is not None else change1d

    for tk, date in bullmap.items():
        d = get(tk)
        d["fapick"] = True
        d["fapick_date"] = date

    rows = []
    for tk, d in info.items():
        count = sum([d["insider"], d["fapick"], d["hh"], d["obv"], d["pullback"]])
        if count < 2:
            continue
        rows.append((
            tk, d["name"], d["sector"] or "Other", count,
            d["insider"], d["fapick"], d["hh"], d["obv"], d["pullback"],
            d["mcap"], d["price"], d["change1d"],
            d["insider_date"], d["fapick_date"], d["hh_tier"],
        ))

    rows.sort(key=lambda r: (-r[3], r[0]))
    print(f"{len(rows)} tickers with 2+ signals (of {len(info)} appearing anywhere).")

    data_lines = []
    for (tk, name, sector, count, insider, fapick, hh, obv, pullback,
         mcap, price, change1d, insider_date, fapick_date, hh_tier) in rows:
        mcap_s = str(int(mcap)) if mcap else "null"
        price_s = str(price) if price is not None else "null"
        chg_s = str(round(change1d, 1)) if change1d is not None else "null"
        insider_date_s = json.dumps(insider_date) if insider_date else "null"
        fapick_date_s = json.dumps(fapick_date) if fapick_date else "null"
        hh_tier_s = json.dumps(hh_tier) if hh_tier else "null"
        data_lines.append(
            f'["{esc_js(tk)}","{esc_js(name)}","{esc_js(sector)}",{count},'
            f'{json.dumps(bool(insider))},{json.dumps(bool(fapick))},{json.dumps(bool(hh))},'
            f'{json.dumps(bool(obv))},{json.dumps(bool(pullback))},'
            f'{mcap_s},{price_s},{chg_s},{insider_date_s},{fapick_date_s},{hh_tier_s}]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    build_date = datetime.date.today().strftime("%-d %b %Y")
    html = html.replace("<<<BUILD_DATE>>>", build_date)

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
