# Setup Guide

This guide covers a fresh personal/development installation on Linux or Windows. The included `setup.sh` and systemd unit are Ubuntu-oriented; the manual steps below are the portable path. The checked-in configuration is used as-is unless you intentionally edit `config/config.yaml`.

## Requirements

- Python 3.11 recommended (the code requires modern Python syntax; use Python 3.10+).
- Git and outbound HTTPS access.
- Upstox developer app credentials and a user-authorized Upstox account for live market data.
- At least one notification route configured if you want alerts.
- TA-Lib's Python package is in `requirements.txt`. If pip cannot install its wheel on your OS/Python version, install the matching TA-Lib native library/wheel per the package's platform instructions, then retry requirements installation.

## 1. Clone the repository

Linux/macOS shell:

```bash
git clone https://github.com/vsp9714-sys/stock-monitor.git
cd stock-monitor
```

Windows PowerShell:

```powershell
git clone https://github.com/vsp9714-sys/stock-monitor.git
Set-Location stock-monitor
```

## 2. Create an isolated Python environment

### Linux

Ubuntu 22.04/24.04 example (if Python 3.11 is not already installed):

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git build-essential pkg-config libta-lib-dev libxml2-dev libxslt1-dev libfreetype6-dev libpng-dev
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On a Linux distribution without `libta-lib-dev`, use its equivalent TA-Lib development package or a compatible prebuilt wheel. `bash setup.sh` is the repository's OCI setup helper, but it assumes `/home/ubuntu/repos/stock-monitor`, sudo/apt, and systemd; do not use it unchanged on other distributions.

### Windows PowerShell

Install Python 3.11 (64-bit) and Git, then:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` or invoke the environment directly without activation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If TA-Lib has no wheel for your Python/platform, install the native dependency and compatible Python package using the upstream TA-Lib installation guide. Windows may also need the Microsoft Visual C++ runtime for binary packages. On Windows, install the `tzdata` Python package if the environment cannot resolve `Asia/Kolkata` through `zoneinfo`; Python/pytz timezone support is required for correct exchange scheduling.

## 3. Configure the default settings and credentials

The checked-in `config/config.yaml` is the default settings file. It selects the Nifty 500 universe, 15-minute scans during the 09:15–15:30 IST NSE session, 95% minimum scan coverage, grouped confluence with two independent groups, a ₹1 crore/20-session liquidity screen, informational benchmark/sector context, optional derivatives disabled, and ATR references enabled. See [USAGE.md](USAGE.md) before changing any setting.

Create the ignored secrets file if one does not already exist (do not overwrite a populated local file):

Linux:

```bash
test -f config/secrets.yaml || cp config/secrets.yaml.example config/secrets.yaml
chmod 600 config/secrets.yaml
```

Windows PowerShell:

```powershell
if (-not (Test-Path config/secrets.yaml)) { Copy-Item config/secrets.yaml.example config/secrets.yaml }
```

Edit `config/secrets.yaml` and enter the Upstox API key/secret. For Telegram, add a bot token and selected destination chat ID. The checked-in Telegram notifier is enabled, so replace its placeholders or disable it in `config/config.yaml`; otherwise notifier initialization/delivery will fail. Never commit `config/secrets.yaml`, `.env`, access tokens, or database files. Use OS file permissions/ACLs to restrict the secrets file to the account running the application.

Configuration precedence is: `config/config.yaml` → recursive override from `config/secrets.yaml` → `.env` values loaded only when not already in the process environment → supported environment overrides. Environment variable names include `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_ACCESS_TOKEN` (also `UPSTOX_STATIC_TOKEN`/`UPSTOX_TOKEN`), `UPSTOX_REFRESH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `DISCORD_WEBHOOK_URL`.

## 4. Authorize Upstox

From the repository root with the virtual environment active:

```bash
python -m src.fetcher.upstox_auth_setup
```

The command opens the Upstox authorization page and listens locally on `localhost:8080` for the callback. Ensure the Upstox app redirect URI matches `http://localhost:8080/callback`. On a headless Linux host, run the authorization command on a machine with a browser and callback reachable, or complete the provider's OAuth flow through an approved secure method; do not paste access tokens into shell history or commit them. The fetcher stores token state under ignored `data/` and attempts refresh where supported. Access tokens expire; reauthorize when the stored credentials are no longer valid.

## 5. Initialize and warm daily history

Create application directories and initialize the SQLite database by running a command such as:

```bash
python -m src.cache_warmup
```

This initializes the database and warms history for the configured universe. It is resumable but makes provider requests (paced at the configured interval), so allow time and do not run multiple warm-ups concurrently. If you want only database initialization before a manual test, `python -c "from src.db import get_engine; get_engine()"` creates the schema.

The configured indicators require enough history for their longest lookback (currently at least 210 daily bars). Stocks that do not have enough history are skipped; an incomplete universe/coverage condition is shown in the scan summary.

## 6. Verify before enabling an always-on process

A one-shot scan may send real notifications to enabled destinations:

```bash
python -m src.scan_once
```

Only run it when you intend to perform a scan and accept possible alerts. For a cache-only readiness refresh that sends no alerts:

```bash
python -m src.refresh_daily_cache --manual --universe nifty500
```

For an offline backtest against local cache:

```bash
python -m src.backtest.cli --from-date 2025-01-01 --to-date 2025-12-31
```

## 7. Run continuously

### Linux with systemd (Ubuntu example)

The service template uses `/home/ubuntu/repos/stock-monitor` and the `ubuntu` account. If your checkout is elsewhere or runs under another account, edit `systemd/stock-monitor.service` first (WorkingDirectory, User/Group, PATH, ExecStart, log paths):

```bash
sudo cp systemd/stock-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-monitor
systemctl status stock-monitor
journalctl -u stock-monitor -f
```

The process itself runs its schedule; an empty crontab is expected. On startup, it initializes the DB and refreshes the verified instrument master. If started while the NSE market is open, it may immediately perform a scan and send alerts. Start it outside market hours if you do not want that startup scan.

To stop or restart:

```bash
sudo systemctl stop stock-monitor
sudo systemctl restart stock-monitor
```

### Windows (foreground or Task Scheduler)

There is no bundled Windows service definition. For a foreground run:

```powershell
.\.venv\Scripts\python.exe -m src.main
```

Press Ctrl+C for graceful shutdown. To run at logon/startup, use Windows Task Scheduler with:

- Program: `<checkout>\.venv\Scripts\python.exe`
- Arguments: `-m src.main`
- Start in: `<checkout>`
- A dedicated Windows account with read/write access to `config`, `data`, `logs`, `charts/output`, and report directories.

Do not run two scheduler instances against the same database. Ensure the host stays awake/connected for scheduled jobs and uses correct time synchronization. Windows service/Task Scheduler behavior has not been operationally acceptance-tested by this repository's maintainers; verify logs and timezone behavior on the target host.

## Updating

Back up `data/stocks.db` and `config/secrets.yaml` securely before upgrades. Pull a reviewed release/branch, install any changed requirements in the same virtual environment, inspect config changes, then restart the service outside market hours. Keep the SQLite database and credentials out of source control.

## Troubleshooting

- **Missing Upstox auth:** rerun the OAuth command and check API key/secret, redirect URI, token status, and host clock.
- **No Telegram delivery:** confirm the bot token, destination ID, bot/group membership, `individual_alert` / `group_alert` switches, and notifier logs.
- **No symbols / fail-closed scan:** inspect the summary and logs for instrument-master, Nifty 500 source/snapshot, quote freshness, coverage, or calendar errors. Do not switch to `all_nse` merely to bypass a validation failure without understanding why.
- **NSE calendar unavailable:** add/verify the official calendar for that year in `config/nse_trading_calendar.yaml`; unknown years intentionally fail closed.
- **TA-Lib installation failure:** confirm Python architecture/version and use a matching upstream native library/wheel before installing requirements again.
