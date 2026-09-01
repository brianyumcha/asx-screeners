# ASX Screeners

Two Python screeners over the full ASX (ticker universe via SeaBee API / Market Index, price data via Yahoo Finance):

- **`OBV SCREENER.py`** — pre-breakout screener: rising OBV, price below its recent high, RSI 45-70, price above 20d/50d SMA.
- **`PULLBACK SCREENER.py`** — Fibonacci "Zag Zone" pullback screener: price retracing 38.2%-61.8% of its latest swing, scored on volume/RSI/Stoch RSI/OBV confluence.

Both write a card-grid HTML dashboard (mini candlestick charts, sector/size/timeframe filters), a CSV, and a TradingView watchlist `.txt`.

## Run locally

```
python3 "OBV SCREENER.py"
python3 "PULLBACK SCREENER.py"
```

(or the `run-obv` / `run-pullback` shell aliases, if set up)

## Runs automatically

A GitHub Actions workflow (`.github/workflows/scan.yml`) runs both screeners on weekday afternoons (AEST, after ASX close) and publishes the dashboards to GitHub Pages. Cooldown state (`seen_tickers*.json`) is committed back to the repo each run so "already seen" tracking persists across runs.

Not financial advice — research tooling only.
