# Stock Monitor

Swing trading signal detector for all 5000+ NSE/BSE listed stocks. Runs on an OCI instance and sends real-time alerts via Telegram (+ Discord/Email) when signals are detected.

## Features
- **Signals**: RSI oversold/overbought, MACD crossover, EMA crossovers (9×21, 21×50, 50×200 Golden/Death Cross), Volume breakout
- **Universe**: All NSE/BSE EQ stocks (~5000+)
- **Data**: Real-time via Upstox API (free tier)
- **Fundamentals**: Market cap, cap category, P/E, ROE, promoter holding, last 4Q profit
- **Charts**: Candlestick + EMA overlays + RSI + MACD (dark theme)
- **Notifications**: Telegram (+ Discord, Email, WhatsApp — pluggable)
- **Schedule**: Every 15 min during market hours (9:15 AM – 3:30 PM IST, Mon–Fri)
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
- `notifications.telegram.bot_token` + `chat_id`

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
tail -f logs/stock_monitor.log
journalctl -u stock-monitor -f
```

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
