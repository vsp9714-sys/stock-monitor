# Stock Monitor

Swing trading signal detector for NSE-listed ordinary shares. Runs on an OCI instance and sends alerts via Telegram (+ Discord/Email) when signals are detected.

## Features
- **Signals**: RSI oversold/overbought, MACD crossover, EMA crossovers (9×21, 21×50, 50×200 Golden/Death Cross), Volume breakout
- **Universe**: NSE mainboard and SME ordinary shares verified against NSE's official equity masters by ISIN (including EQ/BE/BZ and SM/ST/SZ series; bonds and other non-share instruments are excluded)
- **Data**: Real-time via Upstox API (free tier)
- **Fundamentals**: Market cap, cap category, P/E, ROE, promoter holding, last 4Q profit
- **Charts**: Candlestick + EMA overlays + RSI + MACD (dark theme)
- **Notifications**: Telegram (+ Discord, Email, WhatsApp — pluggable)
- **Schedule**: Internal Python scheduler scans every 15 min; market-hours gate is 9:15 AM – 3:30 PM IST, Mon–Fri
- **Persistent**: systemd service with auto-restart

## Quick Start

### 1. One-time setup
```bash
cd /home/ubuntu/repos/stock-monitor
bash setup.sh
```

### 2. Configure API keys
```bash
nano config/secrets.yaml
```
Required:
- `upstox.api_key` + `upstox.api_secret` (from [Upstox Developer](https://developer.upstox.com/))
- `notifications.telegram.bot_token` + the enabled Telegram destination(s)

Telegram recipient routing is controlled in `config/config.yaml`:

```yaml
notifications:
  telegram:
    individual_alert: "Y"   # send to chat_id
    chat_id: "YOUR_TELEGRAM_CHAT_ID"
    group_alert: "N"        # change to Y to send to group_chat_id
    group_chat_id: ""       # Telegram group chat ID, commonly starts with -100
```

`individual_alert` and `group_alert` accept Y/N (or YAML boolean values). At least one must be enabled, and its corresponding chat ID must be populated. Keep `bot_token` in the ignored `config/secrets.yaml`; the config loader merges secrets over `config.yaml`. The bot must be added to the group and allowed to post. Enabling both switches sends independently to both destinations.

### 3. Upstox OAuth2 (required daily — Upstox tokens expire every 24h)
```bash
source .venv/bin/activate
python -m src.fetcher.upstox_auth_setup
```

### 4. Start
```bash
sudo systemctl start stock-monitor
sudo systemctl enable stock-monitor   # Auto-start on reboot
```

### 5. Monitor
```bash
systemctl status stock-monitor
systemctl show stock-monitor -p ActiveState -p SubState -p MainPID -p ActiveEnterTimestamp
tail -f logs/stock_monitor.log
journalctl -u stock-monitor -f
```

The service is scheduled by the Python `schedule` loop inside the systemd-managed process, so an empty `crontab -l` is expected. Check for systemd timers separately with `systemctl list-timers --all | grep -i stock-monitor`; this project does not use a systemd timer. The verified NSE instrument master is refreshed on startup and daily at 08:15 IST; configured membership is resolved and snapshotted at 08:30 IST, before market scans. Startup fails closed if the official source masters cannot be validated, preventing scans against a stale universe that may contain bonds.

## Configurable stock universes (WP17)

The scanner supports `all_nse` (backward-compatible fallback), `nifty500`, `custom`, and `composed`. The checked-in runtime config selects `nifty500`, restricting scans and incremental refreshes to verified Nifty 500 constituents. Universe membership is independent of liquidity/tradability: WP10's liquidity rules are not applied here.

```yaml
universe:
  mode: nifty500
  nifty500_min_count: 450
  nifty500_max_count: 550
  custom_symbols: []
  include_fno: false
```

Nifty 500 plus a custom watchlist:

```yaml
universe:
  mode: composed
  custom_symbols: [ABCAPITAL, ZOMATO]
  include_fno: false
```

Nifty 500 plus F&O underlyings plus a custom watchlist:

```yaml
universe:
  mode: composed
  custom_symbols: [ABCAPITAL, ZOMATO]
  include_fno: true
```

For a custom-only watchlist use `mode: custom` and populate `custom_symbols`. In composed mode, the selector unions the official Nifty Indices Nifty 500 CSV, optionally NSE's current F&O market-lot underlyings, and the configured custom symbols, then normalizes and deduplicates them. Each symbol must also exist in the verified NSE instrument master. Unsupported legacy examples such as `all_bse` and `nifty50` have been removed rather than left as misleading options.

Nifty 500 membership is refreshed at most once on each configured weekday, before market scans (08:30 IST), using `https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv`. The source must have the `Symbol` column, unique normalized symbols, and a count within the configured bounds. Valid source snapshots and the selected daily universe are stored in SQLite with source/retrieval metadata. If refresh fails, a validated last-known-good snapshot is used and marked stale with age/error. If none exists, the selected universe fails closed; it never silently broadens to `all_nse`. Optional F&O source failure is explicitly reported and does not widen the selection.

For `nifty500`, `custom`, and `composed`, the default allowed series is regular `EQ`; set `allowed_series` explicitly to opt into another verified NSE share series. For compatibility, an unset filter in `all_nse` retains its existing share-series coverage. Series eligibility is not liquidity validation. Scan summaries expose mode, source/retrieval timestamps and staleness, member-source counts, unique selected count, series exclusions, quote/history eligibility, and a dated symbol snapshot. `final_tradable_count` currently means scan-eligible before WP10's liquidity filter; the summary states that liquidity filtering has not yet been applied.

## Upstox pacing and daily candle cache

The scanner stores historical daily OHLCV in SQLite (`ohlcv_cache`) and uses Upstox V3 full-market quotes in batches for each scan. The quote API supports up to 500 instrument keys per request; this project defaults to 100 keys per request to keep URLs comfortably sized. With 3,207 active shares that is about 33 quote requests, rather than 3,207 individual history requests on every scan. If an HTTP quote batch fails, the sweep fails closed before sending alerts. If a successful response omits individual symbols (for example, an illiquid instrument with no quote), only those symbols are skipped; alerts may be sent for fully quoted/evaluated symbols, and the summary is explicitly marked incomplete.

On a new deployment, warm the history cache before expecting complete scans:

```bash
cd /home/ubuntu/repos/stock-monitor
./.venv/bin/python -m src.cache_warmup
```

The warm-up is resumable: it commits every 20 completed symbols, and reruns skip symbols that already have the required history. The required history follows the slowest configured indicator (currently 210 daily bars). Historical requests are paced at 1 second each; with the checked-in Nifty 500 mode, the initial pass takes at least about 8 minutes, and longer if Upstox asks for retries. An `all_nse` pass over ~3,200 shares takes at least 54 minutes. Do not start a second warm-up concurrently. A nonzero exit status means there are stocks that still need cache repair; use the printed failed-symbol list and rerun after addressing the cause.

During normal operation, the service checks for the latest completed daily session at 06:00 Asia/Kolkata on configured NSE trading sessions, using Upstox's historical-candle endpoint (not live quote OHLC). It first probes `RELIANCE`, `TCS`, and `360ONE`; a refresh of the configured universe starts only when all three report the same latest candle date. If the provider has not published a consistent recent session yet, the check skips without marking every symbol `no_bar` or deleting cached data. The paced pass targets that observed session date and runs in a background thread so it does not stall the scan scheduler; one in-process lock prevents duplicate runs. Per-symbol checkpoints make confirmed symbols resumable after interruption/restart. Missing bars and symbol-specific invalid instrument keys are checkpointed per symbol; broader provider failures stop the pass so it can safely resume after recovery.

To manually run the same readiness-gated refresh outside 06:00–07:00 IST, use `./.venv/bin/python -m src.refresh_daily_cache --manual --universe nifty500`. It refreshes OHLCV only; it never runs scans or sends alerts. To remove OHLCV, refresh checkpoints, and fundamentals outside the configured universe after a verified backup, first use `./.venv/bin/python -m src.prune_market_cache` for a dry run, then add `--execute`. The instrument master and historical signals/delivery audit rows are retained.

To intentionally send one manual alert sweep of the configured universe (uses the configured routes and normal per-destination escalation deduplication), run:

```bash
./.venv/bin/python -m src.scan_once
```

The command prints a JSON summary and exits nonzero if the scan is incomplete. Batch/API failures, mixed quote sessions, malformed OHLCV, or coverage below `scan.min_coverage` fail closed and dispatch no alerts. Individual missing-quote symbols and shares with less than the required daily history are skipped; alerts are eligible only if the configured minimum coverage is still met.

Confirmed confluence signals use completed daily candles from SQLite. During an open NSE session, the scanner may also evaluate a **separate provisional intraday confluence** by appending the quote's current-session OHLC to an in-memory copy of completed history. Provisional alerts have a distinct signal type/deduplication key and are explicitly labelled with the session date and quote time; they are not finalized daily bars and are never written into `ohlcv_cache`. Intraday cumulative volume is excluded from provisional scoring until a time-of-day comparable baseline is implemented (Batch B item 3). The scanner validates quote freshness, price/volume ranges, OHLC consistency, cached candle integrity, and exchange-session dates before evaluation. `scan.min_coverage` defaults to `0.95` (95% of the configured universe must be evaluable); below that threshold the sweep sends no alerts.

Market hours and session dates use `Asia/Kolkata` and the checked-in NSE Capital Market calendar in `config/nse_trading_calendar.yaml`. The file is versioned by year and cites the official circular at its header. Unknown years fail closed until their official calendar is added. Special sessions remain closed unless explicit hours are configured.

`upstox.historical_min_interval_seconds`, `upstox.quote_min_interval_seconds`, `upstox.request_max_retries`, and `upstox.quote_batch_size` in `config/config.yaml` control pacing, bounded transient retries, and quote batching. HTTP 429 and 5xx/network errors use bounded backoff; permanent 4xx errors fail immediately. Historical backfill is paced below the standard user quota; normal recurring scans use batched quotes and cached history.

## Alert escalation and delivery deduplication

For each stock, direction, and calendar date, the scanner stores one confluence signal row. It sends the initial qualifying score (for example, 2/6), then sends again only when the score increases (for example, 3/6). The row's score, price, check snapshot, and chart reference are updated to the highest observed score. Delivery scores are recorded independently per destination, so an unchanged score is not resent and a failed destination can be retried without resending to destinations that succeeded. Existing `notified` confluence rows are migrated as already delivered at their stored score for pre-existing destinations to avoid rollout duplicates; a newly enabled group still receives its first alert.

## Project Structure
```
stock-monitor/
├── config/
│   ├── config.yaml          # All settings (safe to commit)
│   └── secrets.yaml         # API keys (NEVER commit — gitignored)
├── data/
│   └── stocks.db            # SQLite database
├── src/
│   ├── main.py              # Entry point + scheduler
│   ├── config_loader.py     # Config + secrets merge
│   ├── db.py                # SQLite models + helpers
│   ├── scanner.py           # Main scanning orchestrator
│   ├── fetcher/
│   │   ├── upstox.py        # Upstox API (OHLCV, instruments, LTP)
│   │   ├── upstox_auth_setup.py  # One-time OAuth2 setup
│   │   └── fundamentals.py  # Screener.in scraper
│   ├── signals/
│   │   ├── rsi.py           # RSI signal
│   │   ├── macd.py          # MACD crossover
│   │   ├── ema_cross.py     # EMA crossovers (9×21, 21×50, 50×200)
│   │   └── volume_breakout.py # Volume spike + price breakout
│   └── notifiers/
│       ├── base.py          # Abstract base + AlertPayload
│       ├── telegram.py      # Telegram Bot API
│       ├── discord.py       # Discord webhook
│       └── email_notifier.py # SMTP email
├── charts/
│   ├── chart_generator.py   # Candlestick chart with indicators
│   └── output/              # Generated chart PNGs
├── systemd/
│   └── stock-monitor.service
├── setup.sh                 # One-time OCI setup script
└── requirements.txt
```

## Signals Reference

| Signal | Type | Trigger |
|--------|------|---------|
| RSI Oversold | BUY | RSI < 30 |
| RSI Overbought | SELL | RSI > 70 |
| MACD Bullish Cross | BUY | MACD crosses above signal line |
| MACD Bearish Cross | SELL | MACD crosses below signal line |
| EMA 9×21 Bullish | BUY | 9 EMA crosses above 21 EMA |
| EMA 9×21 Bearish | SELL | 9 EMA crosses below 21 EMA |
| EMA 21×50 Bullish | BUY | 21 EMA crosses above 50 EMA |
| EMA 21×50 Bearish | SELL | 21 EMA crosses below 50 EMA |
| Golden Cross | BUY | 50 EMA crosses above 200 EMA |
| Death Cross | SELL | 50 EMA crosses below 200 EMA |
| Volume Breakout Bullish | BUY | 2×+ volume + price breaks 20-day high |
| Volume Breakout Bearish | SELL | 2×+ volume + price breaks 20-day low |

## Telegram Alert Format

Each alert includes:
- 📍 Signal type + direction (BUY/SELL)
- 💰 Current price
- 🏢 Company name + market cap + cap category
- 📊 P/E, ROE, promoter holding
- 📉 Last 4 quarters net profit trend
- 🔗 Screener.in link
- 📸 Candlestick chart with EMA + RSI + MACD

## Upstox Token Note

Upstox access tokens expire **daily**. A cron job can automate re-auth if you have a headless flow, but for now the manual auth script must be run each morning before market open. Future enhancement: implement token persistence via Upstox refresh token.

## Coming Next (Docker)
Once stable on systemd, wrap in a Docker container for portability and easier updates.
