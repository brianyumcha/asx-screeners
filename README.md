# ASX Screeners

Three Python screeners over the full ASX (ticker universe via SeaBee API / Market Index, price data via Yahoo Finance):

- **`OBV SCREENER.py`** — pre-breakout screener: rising OBV, price below its recent high, RSI 45-70, price above 20d/50d SMA.
- **`PULLBACK SCREENER.py`** — Fibonacci "Zag Zone" pullback screener: price retracing 38.2%-61.8% of its latest swing, scored on volume/RSI/Stoch RSI/OBV confluence.
- **`HH SCREENER.py`** — Higher-High screener: structural breakout of the last swing high, by sector — a Python/HTML port of the "HH Indicator (BT)" TradingView scripts, with daily/weekly signal toggle and an OBV-confirmation column.

OBV and Pullback write a card-grid HTML dashboard (mini candlestick charts, sector/size/timeframe filters); HH writes a sector-grouped dashboard with a Charts/Table view toggle (chart view shows a mini candlestick+SMA chart per signal, with 1M/3M/6M zoom). All three also write a CSV.

## Run locally

```
python3 "OBV SCREENER.py"
python3 "PULLBACK SCREENER.py"
python3 "HH SCREENER.py"
```

(or the `run-obv` / `run-pullback` / `run-hh` shell aliases, if set up)

## Runs automatically

A GitHub Actions workflow (`.github/workflows/scan.yml`) publishes the dashboards to GitHub Pages on a schedule:

- **OBV + Pullback** — once daily, 5:00pm AEST (after ASX close). Cooldown state (`seen_tickers*.json`) is committed back to the repo each run so "already seen" tracking persists across runs.
- **Higher-High** — 5x daily during market hours (10:30am, 1pm, 3:30pm, 4:30pm, and again as part of the 5pm full run). No cooldown/scoring concept — every ticker passing the liquidity filters is shown, live-signal state only, so re-running intraday just reflects current state.

Manually trigger a full run (all three) any time via `workflow_dispatch` (Actions tab → Run workflow, or `gh workflow run scan.yml`).

Not financial advice — research tooling only.
