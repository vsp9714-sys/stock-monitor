# Usage and Configuration Reference

This is the detailed operator guide for the checked-in application. The source of truth for defaults is [`config/config.yaml`](config/config.yaml); secret placeholders and overrides are in [`config/secrets.yaml.example`](config/secrets.yaml.example). The loader recursively merges the secrets file over the main config, then applies dotenv/environment overrides. Most changes take effect on the next process start; restart a running process after changing files because configuration is loaded once per process.

## Operating model

The application has three distinct paths:

1. **Scheduled scanner (`python -m src.main`)** — initializes SQLite and the verified instrument master, then scans every `market.scan_interval_minutes`. A scheduled tick only calls the scanner while the configured NSE session/calendar is open. Starting the process during open market hours triggers an immediate scan. Scans may send alerts.
2. **Daily cache maintenance** — at 06:00 Asia/Kolkata on weekdays, it probes Upstox for the latest completed daily candle. The configured probe symbols must agree on a session date before the selected-universe refresh begins. It only writes cache/checkpoints; it does not scan or notify. Universe membership is separately refreshed at 08:30 IST; the verified instrument master at startup and 08:15 IST.
3. **One-shot/offline commands** — cache warmup, manual cache refresh, scan-once, pruning, and backtest. Read each command's effect below; `scan_once` can send notifications while the cache commands and backtest do not.

All times and exchange session dates are interpreted in `Asia/Kolkata`. The checked-in NSE Capital Market holiday calendar is versioned in `config/nse_trading_calendar.yaml`; an unknown year fails closed rather than assuming holidays or sessions.

## How to operate

Run commands from the repository root with the project virtual environment active (Linux `.venv/bin/python`; Windows `.venv\Scripts\python.exe`).

### Start the scheduled monitor

```bash
python -m src.main
```

On Linux production, the included systemd unit starts this command. On Windows, run it in a foreground terminal or via Task Scheduler (see [SETUP.md](SETUP.md)). The main loop polls its scheduler every 30 seconds. It does not depend on cron. If startup is during an open NSE session, it immediately scans; that scan may deliver notifications.

### Warm the historical cache

```bash
python -m src.cache_warmup
```

Fetches sufficient daily history for the configured universe, uses durable checkpoints, and is resumable. It is rate-limited by Upstox settings; do not run concurrent warm-ups. It does not evaluate alerts.

### Run a readiness-gated cache refresh

Scheduled equivalent, limited to the 06:00–07:00 IST window:

```bash
python -m src.refresh_daily_cache
```

Manual override outside that window (cache only):

```bash
python -m src.refresh_daily_cache --manual --universe nifty500
```

Supported command-only `--universe` values: `all_nse`, `nifty500`, `custom`, `composed`. An override does not edit saved config. The refresh skips safely if provider readiness probes do not agree. A skipped run is not a successful full refresh.

A separately scheduled/manual one-time path that permits weekend runs is:

```bash
python -m src.refresh_daily_cache_once
```

This has the same readiness guard and is cache-only. It does not scan or send notifications.

### Intentionally run a scan once

```bash
python -m src.scan_once
```

Uses the configured universe, normal scan validation and notification routes. It can send alerts and update signal/delivery records. The command prints a JSON summary and exits nonzero if incomplete. Do not use it as a cache-refresh substitute.

### Run an offline backtest

```bash
python -m src.backtest.cli --from-date 2025-01-01 --to-date 2025-12-31
# Optional:
python -m src.backtest.cli --from-date 2025-01-01 --to-date 2025-12-31 --symbols RELIANCE,TCS --out-dir reports/example
```

Reads only local completed daily candles and cached context, performs no provider requests and sends no notifications. It writes JSON, CSV and text reports to `reports/backtests/<UTC timestamp>/` unless `--out-dir` is supplied. It uses next-available-session open entries, configured ATR stop/target, fixed per-side fee/slippage assumptions, and stop-first handling when one daily candle spans both stop and target. Current symbols may create survivorship bias; point-in-time membership is applied only where a saved universe snapshot exists. It reports sequential closed-trade drawdown, not simultaneous portfolio drawdown. Market/sector filter mode is rejected because historical sector mapping is not point-in-time stored.

### Safely prune data outside the current universe

```bash
python -m src.prune_market_cache             # dry run
python -m src.prune_market_cache --execute   # destructive deletion after review
```

The execute option creates a SQLite backup first. Review the dry-run table counts and the backup path before executing. It removes out-of-universe OHLCV, refresh checkpoints and fundamentals; it preserves the verified stock master, signal records and delivery audit. A changed universe or temporary selector issue can make this operation consequential; do not use it casually.

### Logs and runtime checks

- File log: `logs/stock_monitor.log` (daily rotation, configured retention).
- systemd journal: `journalctl -u stock-monitor -f`.
- SQLite database: `data/stocks.db` by default.
- Generated chart images: `charts/output/`.
- Backtest reports: `reports/backtests/`.

Scan summaries include selected universe provenance, selected/evaluable/evaluated counts, quote/history omissions, liquidity exclusions, coverage, alerts, and incompleteness reasons. A batch quote failure, malformed OHLCV, mixed sessions, or coverage below `scan.min_coverage` fails closed before dispatch. Individual missing quotes or insufficient-history symbols may be skipped if the remaining coverage threshold is satisfied.

## Configuration reference

Edit `config/config.yaml` for non-secret behavior. Secret values belong in ignored `config/secrets.yaml` or supported environment variables. Avoid committing tokens, webhook URLs, app passwords, or account identifiers.

### `upstox`

| Setting | Checked-in default | Effect |
|---|---:|---|
| `api_key`, `api_secret` | placeholders in `secrets.yaml.example` | OAuth client credentials; keep in secrets file/environment. |
| `redirect_uri` | `http://localhost:8080/callback` | Must match the registered Upstox OAuth app redirect. |
| `access_token` | empty | Current access token; token state is persisted under ignored `data/`. |
| `historical_min_interval_seconds` | `1.0` | Minimum delay between historical-candle requests; raising it slows warmup/backfill but reduces request rate. |
| `quote_min_interval_seconds` | `0.1` | Minimum pacing between quote batches. |
| `request_max_retries` | `3` | Bounded retries for transient network/429/5xx failures; permanent 4xx errors are not retried as transient. |
| `quote_batch_size` | `100` | Quote keys per API request (provider allows up to 500); smaller batches make more requests, larger batches use longer URLs. |
| `max_quote_age_seconds` | runtime fallback `300` | Maximum accepted quote age. This key is not currently included in the checked-in YAML; add it under `upstox` to override. |

The OAuth fetcher may also persist/use `refresh_token`; this is a secret and is not a strategy setting.

### `market`, `scan`

| Setting | Default | Effect |
|---|---:|---|
| `market.timezone` | `Asia/Kolkata` | Exchange timezone for all schedule/session calculations. Keep aligned with calendar interpretation. |
| `market.open_time` / `close_time` | `09:15` / `15:30` | Regular session bounds. NSE holidays/special sessions are governed by the versioned exchange calendar. |
| `market.scan_interval_minutes` | `15` | How often the in-process scheduler checks/runs a scan. Increasing it reduces scan frequency; ticks outside session are ignored. |
| `market.trading_days` | `[0,1,2,3,4]` | Python weekday numbers (Monday=0 through Sunday=6) allowed by the general session helper. The official versioned NSE calendar remains the holiday authority. |
| `scan.min_coverage` | `0.95` | Minimum fraction of configured universe that must be evaluable for a complete scan. Below it the scan fails closed and sends no alerts. |

The scheduler checks every 30 seconds, but the scan interval is what determines scheduled cadence. Keep the host clock synchronized.

### `universe`

| Setting | Default | Effect |
|---|---:|---|
| `mode` | `nifty500` | `all_nse` selects all verified common-share series; `nifty500` selects official Nifty 500 members intersected with the verified NSE instrument master; `custom` resolves only `custom_symbols`; `composed` unions Nifty 500 + custom symbols + optionally F&O underlyings. Selected membership is snapshotted and de-duplicated. |
| `custom_symbols` | `[]` | NSE symbols for `custom`/`composed`; unmatched symbols are not silently accepted. |
| `include_fno` | `false` | In `composed` mode only, include current F&O underlyings. Provider/source failure is reported and never widens to all NSE. |
| `nifty500_min_count` / `nifty500_max_count` | `450` / `550` | Guardrails for validating the downloaded constituent source. They do not force selection to exactly 500. |
| `allowed_series` | unset | Optional series allowlist for `nifty500`, `custom`, and `composed`; default there is regular `EQ`. Unset `all_nse` preserves legacy share-series coverage. |
| `min_mcap_cr` | `0` | Minimum market capitalization in crore rupees; zero disables this threshold. Missing fundamentals cannot be assumed to satisfy a positive cap threshold. |
| `min_price` | `10` | Excludes shares below this INR price; set lower (or zero) to relax this guard. |

The selected universe is not necessarily the number actually scanned: verified-master membership, series eligibility, available quotes/history, liquidity, and the coverage gate further affect results. Nifty 500 source guardrails protect against malformed/partial downloads. A validated last-known-good constituent snapshot is used if refresh fails; without one, selection fails closed.

#### `universe.tradability`

| Setting | Default | Effect |
|---|---:|---|
| `enabled` | `true` | Applies completed-session liquidity eligibility during scans and backtests. Existing legacy configs with this whole block omitted do not acquire a new filter implicitly. |
| `average_traded_value_sessions` | `20` | Number of finalized daily sessions used for the average `close × volume` (INR). No partial current-session candle is used. |
| `minimum_average_traded_value_inr` | `10000000` (₹1 crore) | Minimum average traded value. Raising it makes the universe more selective. |
| `minimum_observations` | `20` | Minimum usable observations required, from 1 up to the configured lookback. |
| `unknown_policy` | `exclude` | If liquidity cannot be calculated, `exclude` removes the candidate; `include` leaves it eligible but reports the unknown context. |

### `signals`

Each check can be disabled independently. Enabling a check does not guarantee an alert: it must contribute a directional vote under the confluence policy.

| Setting | Default | Effect |
|---|---:|---|
| `signals.rsi.enabled` | `true` | Enables RSI contribution to the momentum group. |
| `signals.rsi.period` | `14` | RSI lookback length in completed bars (provisional evaluation appends the in-memory session bar). Larger values smooth/slow RSI. |
| `signals.rsi.mode` | `mean_reversion` | `mean_reversion` buys oversold/sells overbought; `trend_momentum` uses bullish/bearish state thresholds. |
| `signals.rsi.confirmation` | `level` | `level` qualifies while at/beyond the threshold; `cross_back` waits for RSI to return through the extreme boundary. In `crossover` confluence mode a fresh qualifying event is still required. |
| `signals.rsi.oversold_threshold` / `overbought_threshold` | `30` / `70` | Mean-reversion boundaries; oversold must be below overbought. |
| `signals.rsi.bullish_threshold` / `bearish_threshold` | `55` / `45` | Trend-momentum state boundaries; bearish must be below bullish. |
| `signals.macd.enabled` | `true` | Enables MACD contribution to the momentum group. |
| `signals.macd.fast_period` / `slow_period` / `signal_period` | `12` / `26` / `9` | MACD fast/slow EMA and signal smoothing lengths. |
| `signals.ema_crossover.enabled` | `true` | Enables EMA checks in the trend group. |
| `signals.ema_crossover.pairs` | `[[9,21],[21,50],[50,200]]` | Fast/slow EMA pairs. They all count as at most one trend-group vote, preventing several EMA crosses from inflating score. |
| `signals.volume_breakout.enabled` | `true` | Enables price/volume breakout in the participation group. |
| `signals.volume_breakout.volume_multiplier` | `2.0` | Required volume multiple versus the mean of prior daily volumes. |
| `signals.volume_breakout.breakout_lookback_days` | `20` | Price must break the prior N-session high/low. Provisional cumulative volume is compared against complete prior-day volumes; it is not time-of-day normalized. |

### `confluence`

| Setting | Default | Effect |
|---|---:|---|
| `min_groups_to_alert` | `2` | Required independent directional groups from trend, momentum, participation (valid range 1–3). This replaces counting raw indicator checks. |
| `direction_consensus` | `true` | Rejects a candidate if enabled groups produce conflicting BUY and SELL directions. |
| `mode` | `crossover` | `crossover` requires fresh qualifying events; `trend` may count aligned current EMA/MACD/RSI states as continuation. Volume breakout remains event-based. Only `crossover` and `trend` are valid. |

An EMA 9/21 and 21/50 cross on the same scan still provides one trend vote, not two. RSI and MACD share one momentum vote. By default at least one other group must agree, and an opposing direction blocks the alert. Alert details retain the individual checks for auditability. Alert escalation uses group-score versions and only resends when a destination's score improves.

### `market_context`

| Setting | Default | Effect |
|---|---:|---|
| `enabled` | `true` | Calculates/caches benchmark and configured-sector context. |
| `mode` | `informational` | `informational` annotates alerts without blocking signals; `filter` suppresses candidates when configured market/sector regimes conflict with signal direction. |
| `filter_unknown_policy` | `exclude` | In `filter` mode, `exclude` fails closed when required context is unavailable; `include` allows candidates despite unavailable context. |
| `benchmark_key` | `NSE_INDEX|Nifty 50` | Upstox index instrument key for broad-market context. |
| `lookback_sessions` | `20` | Completed sessions for relative-strength comparison (stock return minus benchmark return). |
| `short_sma_sessions` / `long_sma_sessions` | `20` / `50` | Windows for regime state; short must be no greater than long. |
| `cache_ttl_hours` | `12` | How long cached index candles are reused before refresh. |
| `sector_index_keys` | explicit mapping in YAML | Maps exact fundamentals sector labels to documented NSE index keys. Current labels are `Banks - Private`, `Banks - Public`, `Automobiles`, `Information Technology`, `Pharmaceuticals`, `FMCG`, `Metals & Mining`, `Oil, Gas & Fuels`, `Financial Services`, and `Healthcare`. Add mappings only for verified provider identifiers. Unmapped labels remain unavailable; no inferred match is made. |

Batch E backtests reject `market_context.mode: filter` because historical sector membership is not stored point-in-time. For normal scans, this is informational by default.

### `derivatives`

| Setting | Default | Effect |
|---|---:|---|
| `enabled` | `false` | Enables optional current F&O option-chain/open-interest context. When false no OI fetch is needed. |
| `cache_ttl_minutes` | `15` | Reuses an in-process OI context for this duration. |
| `max_context_symbols_per_scan` | `25` | Caps symbols with OI fetches per scan to control provider quota/latency. |

Only current F&O-eligible underlyings are queried. OI, PCR and changes are context, not an independent signal vote. Unsupported/missing values are labelled unavailable and never converted to zero. OI is not point-in-time backtested.

### `risk` and `backtest`

`risk` applies to live alert annotations; `backtest` controls offline replay assumptions. These values do not route orders.

| Setting | Default | Effect |
|---|---:|---|
| `risk.enabled` | `true` | Shows/hides ATR-based model stop/target references on alerts. |
| `risk.atr_period` | `14` | Completed daily bars used for the average true range calculation. |
| `risk.stop_atr_multiple` | `1.5` | Stop distance = ATR × this value from the reference entry price. |
| `risk.target_r_multiple` | `2.0` | Target distance as a multiple of stop risk. |
| `risk.account_capital_inr` | `null` | If set, permits an illustrative quantity estimate; leave null to omit sizing. No broker/account balance is fetched. |
| `risk.risk_percent` | `0.5` | Percent of configured capital used as a model risk budget. Used for quantity only when capital is also set. |
| `backtest.atr_period` | `14` | ATR period for replay exits. |
| `backtest.stop_atr_multiple` | `1.5` | Stop distance multiple for replay. |
| `backtest.target_r_multiple` | `2.0` | Profit target in R multiples for replay. |
| `backtest.fee_bps_per_side` | `5.0` | Fixed fee assumption on each side, in basis points. |
| `backtest.slippage_bps_per_side` | `5.0` | Fixed adverse slippage assumption on each side, in basis points. |
| `backtest.train_fraction` | `0.7` | Fraction of available sessions used as the in-sample portion for the chronological walk-forward summary; must be at least 0.5 and below 1. |

The backtest assumes next available session open entry, stop-first when one daily candle hits stop and target, and gap-through stop fills at open. It is a deterministic replay of the configured daily strategy, not a realistic execution simulator. Fees/slippage exclude taxes, market impact, liquidity constraints beyond the configured screen, and order queue uncertainty. Quantity sizing formula is risk budget divided by stop distance, rounded down; it is not a recommendation.

### `fundamentals`

| Setting | Default | Effect |
|---|---:|---|
| `screener_base_url` | `https://www.screener.in` | Source URL for fundamentals scraper. |
| `cache_ttl_hours` | `24` | Reuse interval for fundamentals cache; stale cached values are marked stale, not fresh. |
| `request_delay_seconds` | `2.0` | Delay between requests to respect the source. |

Fundamentals are annotations unless a positive `universe.min_mcap_cr` makes market cap an eligibility condition. Missing/stale fundamentals are explicitly represented.

### `notifications`

Only Telegram, Discord, and Email are currently constructed by the notifier factory. The `whatsapp` block exists as future/config placeholder but is not currently active.

#### Telegram

| Setting | Default | Effect |
|---|---|---|
| `enabled` | `true` | Creates Telegram notifier. Configure valid secret credentials or disable. |
| `bot_token` | placeholder | Bot API token; put in `config/secrets.yaml` or `TELEGRAM_BOT_TOKEN`. |
| `individual_alert` | `Y` | Send to `chat_id` when enabled. Accepts `Y/N` or YAML booleans. |
| `chat_id` | placeholder | Individual/user chat destination. |
| `group_alert` | `N` | Send to `group_chat_id` when enabled. |
| `group_chat_id` | empty | Group destination; add bot and allow it to post. |
| `send_chart` | `true` | Attach generated chart to alerts. |

At least one of individual/group switches must be enabled and its matching destination populated. Both may be enabled; each destination has independent delivery deduplication.

#### Discord

| Setting | Default | Effect |
|---|---|---|
| `enabled` | `false` | Creates Discord webhook notifier when true. |
| `webhook_url` | placeholder | Secret webhook URL; can be overridden with `DISCORD_WEBHOOK_URL`. |
| `send_chart` | `true` | Include chart image where supported. |

#### Email

| Setting | Default | Effect |
|---|---|---|
| `enabled` | `false` | Creates SMTP email notifier when true. |
| `smtp_host` / `smtp_port` | `smtp.gmail.com` / `587` | SMTP endpoint and submission port. |
| `username` / `password` | placeholders | SMTP credentials; use an app password where required. |
| `from_addr` | configured email | Sender address; defaults to username in notifier. |
| `to_addrs` | one placeholder recipient | Recipient list. |
| `send_chart` | `false` | Attach chart if enabled. |

#### WhatsApp placeholder

`account_sid`, `auth_token`, `from_number`, and `to_number` are present in YAML but are not read by current notifier construction. Changing them alone will not enable WhatsApp alerts.

### `database`, `logging`

| Setting | Default | Effect |
|---|---|---|
| `database.path` | `data/stocks.db` | SQLite database path, relative to repository root when run from root. Keep it on a reliable local filesystem; do not share concurrently across multiple scheduler processes. |
| `logging.level` | `INFO` | Python log threshold; examples include DEBUG, INFO, WARNING, ERROR. |
| `logging.log_dir` | `logs` | Directory for rotating `stock_monitor.log`. |
| `logging.backup_count` | `7` | Number of daily rotated logs to retain. |

## Secrets and environment overrides

`config/secrets.yaml` is recursively merged over `config/config.yaml`; its directory structure should match. The loader also reads the first values not already set in the process environment from repository `.env`, `~/.env`, then `/home/ubuntu/.env`. Supported process environment overrides (highest precedence) are:

| Variable | Setting overridden |
|---|---|
| `UPSTOX_API_KEY` | `upstox.api_key` |
| `UPSTOX_API_SECRET` | `upstox.api_secret` |
| `UPSTOX_ACCESS_TOKEN`, `UPSTOX_STATIC_TOKEN`, `UPSTOX_TOKEN` | `upstox.access_token` |
| `UPSTOX_REFRESH_TOKEN` | `upstox.refresh_token` |
| `TELEGRAM_BOT_TOKEN` | `notifications.telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | `notifications.telegram.chat_id` |
| `DISCORD_WEBHOOK_URL` | `notifications.discord.webhook_url` |

Do not put secrets in CLI arguments, screenshots, logs, or source control. `.gitignore` excludes `config/secrets.yaml`, `.env`, `data/`, generated charts, and logs.

## Changing settings safely

1. Stop or plan a controlled restart of the running process; it loads configuration once at startup.
2. Back up `config/config.yaml` and the SQLite database before changing universe/filter policy.
3. Change one policy area at a time; keep the YAML types correct (booleans unquoted; lists as YAML lists; RSI/confluence modes exactly as documented).
4. Run `python -c "from src.config_loader import load_config; load_config(); print('configuration valid')"` to validate supported modes and thresholds. This prints no secret values.
5. For selector changes, review a scan summary and selected count before considering cache pruning. Universe selection and cache cleanup are separate operations.
6. Restart outside market hours if startup scans/alerts are not desired. Inspect logs after startup.

Invalid confluence/RSI modes, group counts outside 1–3, invalid tradability thresholds, invalid market-context windows, and invalid risk/backtest assumptions raise configuration errors at startup/load time.
