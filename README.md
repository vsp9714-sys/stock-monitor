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

The service is scheduled by the Python `schedule` loop inside the systemd-managed process, so an empty `crontab -l` is expected. Check for systemd timers separately with `systemctl list-timers --all | grep -i stock-monitor`; this project does not use a systemd timer. The verified stock universe is refreshed on startup and daily at 08:30 using the server's local timezone (the scan market-hours gate itself uses Asia/Kolkata). Startup fails closed if the official source masters cannot be validated, preventing scans against a stale universe that may contain bonds.

## Upstox pacing and daily candle cache

The scanner stores historical daily OHLCV in SQLite (`ohlcv_cache`) and uses Upstox V3 full-market quotes in batches for each scan. The quote API supports up to 500 instrument keys per request; this project defaults to 100 keys per request to keep URLs comfortably sized. With 3,207 active shares that is about 33 quote requests, rather than 3,207 individual history requests on every scan. If an HTTP quote batch fails, the sweep fails closed before sending alerts. If a successful response omits individual symbols (for example, an illiquid instrument with no quote), only those symbols are skipped; alerts may be sent for fully quoted/evaluated symbols, and the summary is explicitly marked incomplete.

On a new deployment, warm the history cache before expecting complete scans:

```bash
cd /home/ubuntu/repos/stock-monitor
./.venv/bin/python -m src.cache_warmup
```

The warm-up is resumable: it commits every 20 completed symbols, and reruns skip symbols that already have the required history. The required history follows the slowest configured indicator (currently 210 daily bars). Historical requests are paced at 1 second each; a first full backfill of ~3,200 shares therefore takes at least 54 minutes and may take longer if Upstox asks for retries. Do not start a second warm-up concurrently. A nonzero exit status means there are stocks that still need cache repair; use the printed failed-symbol list and rerun after addressing the cause.

During normal operation, the service checks for the latest completed daily session at 06:30 and 06:55 Asia/Kolkata on weekdays, using Upstox's historical-candle endpoint (not live quote OHLC). Each check first probes `RELIANCE`, `TCS`, and `360ONE`; a full-universe refresh starts only when all three report the same latest candle date. If the provider has not published a consistent recent session yet, the check skips without marking every symbol `no_bar` or deleting cached data. The paced full pass targets that observed session date and runs in a background thread so it does not stall the scan scheduler; one in-process lock prevents duplicate runs. Per-symbol checkpoints make confirmed symbols resumable after interruption/restart. Provider errors and missing target bars remain retryable.

To intentionally send one manual full-universe alert sweep (uses the configured routes and normal per-destination escalation deduplication), run:

```bash
./.venv/bin/python -m src.scan_once
```

The command prints a JSON summary and exits nonzero if the scan is incomplete. Batch/API failures or mixed quote sessions fail closed and dispatch no alerts. Individual missing-quote symbols and shares with less than the required daily history are skipped; eligible, fully quoted shares can still alert, and the summary reports the scan as incomplete.

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
