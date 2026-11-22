# Handoff Specification: Stock-Monitor Framework Audit, Bug Fixes & v2 Enhancement Roadmap

> **Target Implementer:** Gemini 3.8 Flash (Antigravity)  
> **Repository:** `stock-monitor` (NSE Swing Trading Signal Scanner & Paper Trading Validation Engine)  
> **Baseline Verified:** Points 1–5 implemented (`108 / 108` pytest passing)  
> **Trading Profile:** Indian Cash Equity (NSE CNC Delivery, Long-Only Swing, ₹5,00,000 Capital / ₹1,00,000 Tranche Cap / ₹5,000 Max Risk per Trade, 1:2 R:R)

---

## Table of Contents

1. [Repository Audit & Current Architecture](#1-repository-audit--current-architecture)
2. [Defects Found in Existing Codebase (Fix First)](#2-defects-found-in-existing-codebase-fix-first)
3. [Phase 1 (P0): Bug Fixes & Core Strategy Guardrails](#3-phase-1-p0-bug-fixes--core-strategy-guardrails)
4. [Phase 2 (P0/P1): Paper Trading Hardening & DB Migrations](#4-phase-2-p0p1-paper-trading-hardening--db-migrations)
5. [Phase 3 (P1): High-Impact Signal & Institutional Upgrades](#5-phase-3-p1-high-impact-signal--institutional-upgrades)
6. [Phase 4 (P2): Deferred Refinements (Post-Baseline Only)](#6-phase-4-p2-deferred-refinements-post-baseline-only)
7. [Configuration Specification (`config/config.yaml`)](#7-configuration-specification-configconfigyaml)
8. [File & Integration Map](#8-file--integration-map)
9. [Verification & Test Matrix](#9-verification--test-matrix)

---

## 1. Repository Audit & Current Architecture

### Strengths Verified in the Current Codebase
* **Exchange Calendar & Data Integrity:** `src/market_calendar.py` validates sessions against `config/nse_trading_calendar.yaml` (failing closed on unknown years). `src/scanner.py` validates Upstox V3 quote timestamps, keeps provisional intraday bars strictly in memory, and enforces a `95%` universe coverage gate before dispatching alerts.
* **NSE-Specific Execution Guardrails:** `src/signals/volume_breakout.py` normalizes intraday volume using the NSE U-curve (`nse_expected_volume_fraction`), `src/scanner.py` applies Close Location Value (CLV) wick filtering and two-tier alerting (`intraday_radar` vs. `power_hour_confirmed` at `15:10+` IST), and `src/db.py` tracks per-destination score escalation in `SignalDelivery`.
* **Indicator Smoothing Parity:** Both `src/signals/rsi.py` and `src/signals/adx.py` already use Wilder's smoothing (`ewm(alpha=1/period, adjust=False)`), matching TradingView and Zerodha Kite.

### Structural Gaps Remaining Relative to the Swing Strategy
1. **No Hard Macro-Trend Gate:** EMA crossovers are only confluence votes. A stock in a macro downtrend (`EMA 50 < EMA 200`) can still fire a `BUY` alert via `EMA 21x50` + `Volume`.
2. **No Upper RSI Exhaustion Cap:** Switching RSI to `trend_momentum` (`>= 55`) leaves no ceiling, allowing alerts when RSI is already overextended (`70–85`).
3. **No Tranche Capital Cap in Position Sizing:** `src/risk.py` sizes solely by risk budget (`floor(risk_budget_inr / risk_distance)`), which breaches the ₹1,00,000 tranche cap on tight-ATR setups.
4. **No Earnings / Corporate Action Blackout Awareness:** Overnight earnings gaps bypass stop-loss levels entirely.
5. **Symmetric BUY/SELL Paper Trading on a Long-Only CNC Account:** Simulated short trades mix into weekly scorecards unless filtered via `allowed_directions`.

---

## 2. Defects Found in Existing Codebase (Fix First)

Resolve every item below before building Phase 3 features:

| ID | Severity | File & Location | Exact Defect | Runtime Impact |
| :--- | :--- | :--- | :--- | :--- |
| **BUG-1** | **Critical** | `src/main.py` (L26, L179) | `generate_weekly_report_job()` calls `get_session()`, but `src/main.py` only imports `get_engine` from `src.db`. | Scheduled Saturday 09:00 IST weekly report crashes with `NameError: name 'get_session' is not defined`. |
| **BUG-2** | **Critical** | `src/backtest/cli.py` (L79–84, L130) | `run_backtest()` constructs `Scanner` with `object.__new__(Scanner)` without setting `evaluator.guardrails_cfg = cfg.get("guardrails", {})`, and omits `"guardrails"` from `strategy_cfg` hashing. | Backtests ignore custom `config.yaml` guardrail thresholds and produce unchanged `config_hash_sha256` when guardrails change. |
| **BUG-3** | **Critical** | `src/db.py` (L214), `src/paper_trading.py` (L49–65) | `PaperTrade` unique constraint `(symbol, direction, entry_date)` and open-trade dedup omit `alert_tier`. | A stock firing `intraday_radar` at 10:30 AM blocks its own `power_hour_confirmed` entry at 3:15 PM, skewing Radar vs. Confirmed win rates. |
| **BUG-4** | **High** | `src/scanner.py` (L1699–1703) | `quotes_by_symbol` for `monitor_open_paper_trades()` is built only from current `tradable_candidates`. | Open paper trades whose symbols drop out of the liquidity filter or active universe stop getting monitored. |
| **BUG-5** | **High** | `src/paper_trading.py` (L71, L139, L189, L277) | Uses host `date.today()` (UTC on Linux VM) instead of IST, and counts calendar days (`(cur_date - entry_date).days`) for `max_hold_days` and `cooldown_days`. Cooldown also checks `entry_date` instead of `exit_date`. | UTC/IST session drift; `max_hold_days: 10` exits after ~6–7 trading sessions (fewer during holiday weeks). |
| **BUG-6** | **High** | `src/paper_trading.py` (L152–190) | Stop/target exits and MFE/MAE inspect only 15-minute `last_price`, ignoring session `high` and `low`. | Intraday stop/target hits between 15-minute polls are missed; MFE/MAE understates real excursion. |
| **BUG-7** | **High** | `src/signals/adx.py` (L30, L106–146) | Warmup checks `len(df) < period + 2` (two cascaded Wilder smoothings need `>= 2 * period + 1`), and `adx_filter()` ignores `+DI` vs. `-DI` alignment with signal `direction`. | A strong downtrend (`ADX=35, -DI=40, +DI=10`) passes the ADX filter for a `BUY` alert. |
| **BUG-8** | **High** | `src/notifiers/discord.py` (L138), `src/notifiers/email_notifier.py` (L136) | `broadcast_message()` checks `if not self.enabled`, but neither `__init__` sets `self.enabled`. | Calling paper-trade/weekly broadcasts with Discord or Email enabled raises `AttributeError: ... has no attribute 'enabled'`. |
| **BUG-9** | **Medium** | `src/db.py` (L284) | `get_engine()` runs only `Base.metadata.create_all(_engine)` with no migration helper. | New columns or updated unique constraints on `paper_trades` fail on existing `data/stocks.db` instances. |
| **BUG-10** | **Medium** | `charts/chart_generator.py` (L106–125) | Slices `df.tail(lookback_candles)` (`60` bars) *before* calculating `ema50`, `ema200`, `rsi`, and `macd`. | `EMA 200` is never rendered (`len(df) < 210`), and `EMA 50` / `MACD` / `RSI` have distorted warmup values on alert charts. |

---

## 3. Phase 1 (P0): Bug Fixes & Core Strategy Guardrails

### Step 1.1 — Fix `NameError` in `src/main.py` and `AttributeError` in Notifiers
* **Files:** `src/main.py`, `src/notifiers/discord.py`, `src/notifiers/email_notifier.py`
* **Changes:**
  1. In `src/main.py` (line 26), update import to `from src.db import get_engine, get_session`.
  2. In `DiscordNotifier.__init__()` (`src/notifiers/discord.py`), set `self.enabled = bool(dc.get("enabled", True))`.
  3. In `EmailNotifier.__init__()` (`src/notifiers/email_notifier.py`), set `self.enabled = bool(email_cfg.get("enabled", True))`.

### Step 1.2 — Fix Chart Indicator Warmup in `charts/chart_generator.py`
* **File:** `charts/chart_generator.py`
* **Changes:**
  1. In `generate_signal_chart()`, compute `ema9`, `ema21`, `ema50` (if `len(df) >= 60`), `ema200` (if `len(df) >= 210`), `rsi_series` (if `len(df) >= 24`), and `macd_line, sig_line, histogram` (if `len(df) >= 40`) on the **full** input `df` *first*.
  2. Slice `df` and all computed indicator series to `.tail(lookback_candles)` immediately before building `plot_df` and `addplots`.

### Step 1.3 — Fix Backtest Guardrail Parity & Metrics (`src/backtest/cli.py`, `src/backtest/engine.py`)
* **Files:** `src/backtest/cli.py`, `src/backtest/engine.py`
* **Changes:**
  1. In `run_backtest()` (`src/backtest/cli.py`), set `evaluator.guardrails_cfg = cfg.get("guardrails", {})` right after setting `evaluator.confluence_cfg`.
  2. In `strategy_cfg` (`src/backtest/cli.py`), include `"guardrails"` in the hashed keys: `("signals", "confluence", "guardrails", "backtest", "risk")`.
  3. In `simulate_period()` (`src/backtest/engine.py`):
     * Support optional `backtest.max_hold_days` (default `None` to preserve backward compatibility with existing tests; when set to a positive integer, exit at close on `j - entry_idx + 1 >= max_hold_days` with `exit_reason="time_expiry"`).
     * Support optional `backtest.allowed_directions` (default `["BUY", "SELL"]`; skip signals not in `allowed_directions`).
     * Add `profit_factor` (sum of positive net returns divided by absolute sum of negative net returns, or `None` if no losses) and `expectancy_r` (mean `net_r_multiple`) to `summary`.

### Step 1.4 — Fix ADX Warmup & Add Directional Alignment (`src/signals/adx.py`, `src/scanner.py`)
* **Files:** `src/signals/adx.py`, `src/scanner.py`, `config/config.yaml`
* **Changes:**
  1. Update minimum bar check in `compute_adx()` and `adx_filter()` from `period + 2` to `(2 * period) + 1`.
  2. Update `adx_filter()` signature to accept `direction: Optional[str] = None` and `require_di_alignment: bool = True`.
  3. When `adx_val >= min_adx` and `require_di_alignment=True`:
     * If `direction == "BUY"` and `p_di <= m_di`, return `passed=False`, `reason="di_opposes_buy"`.
     * If `direction == "SELL"` and `m_di <= p_di`, return `passed=False`, `reason="di_opposes_sell"`.
  4. In `Scanner._scan_stock()` (`src/scanner.py`), pass `direction=consensus_dir` and `require_di_alignment=bool(adx_cfg.get("require_di_alignment", True))` to `adx_filter()`.

### Step 1.5 — Add Hard Macro-Trend Gate (`Close > EMA 50 > EMA 200`)
* **Files:** `src/scanner.py`, `config/config.yaml`, `src/config_loader.py`
* **Logic in `Scanner._scan_stock()` (run right after `evaluate_confluence()` passes, before wick/ADX filters):**
  1. Read `trend_gate_cfg = guardrails_cfg.get("trend_gate", {})`.
  2. Apply when `enabled` is true and matches `apply_to_provisional` / `apply_to_daily`.
  3. Require `len(df) >= slow_ema + 10` (default `fast_ema=50`, `slow_ema=200`); if insufficient history, either pass with `status="unavailable"` or reject if `fail_closed_on_short_history: true` (default `false` so newer listings are not silently broken unless opted in).
  4. Compute `ema_fast = compute_ema(closes, fast_ema).iloc[-1]` and `ema_slow = compute_ema(closes, slow_ema).iloc[-1]`.
  5. For `BUY`: reject if `(require_price_above_fast and current_price <= ema_fast)` or `(require_fast_above_slow and ema_fast <= ema_slow)`.
  6. For `SELL`: reject if `(require_price_above_fast and current_price >= ema_fast)` or `(require_fast_above_slow and ema_fast >= ema_slow)`.
  7. Attach `trend_gate` dict (`passed`, `ema_fast`, `ema_slow`, `fast_period`, `slow_period`) to `result`.

### Step 1.6 — Add RSI Exhaustion Ceiling Guardrail
* **Files:** `src/scanner.py`, `config/config.yaml`, `src/config_loader.py`
* **Logic in `Scanner._scan_stock()` (immediately after `trend_gate`):**
  1. Read `rsi_ex_cfg = guardrails_cfg.get("rsi_exhaustion", {})`.
  2. Reuse the `rsi_val` already computed in the RSI check (or compute via `compute_rsi(df["close"].astype(float), period).iloc[-1]` if the RSI signal check is disabled).
  3. Reject `BUY` if `rsi_val > float(rsi_ex_cfg.get("max_rsi_buy", 68.0))`.
  4. Reject `SELL` if `rsi_val < float(rsi_ex_cfg.get("min_rsi_sell", 32.0))`.
  5. Validate in `src/config_loader.py`: `0 <= min_rsi_sell < max_rsi_buy <= 100`.

### Step 1.7 — Enforce Dual Risk + Tranche Position Sizing in `src/risk.py`
* **Files:** `src/risk.py`, `src/config_loader.py`, `src/notifiers/telegram.py`, `config/config.yaml`
* **Changes:**
  1. In `risk_reference()`, read optional `max_tranche_inr` and `max_risk_distance_pct` from `config`.
  2. When `account_capital_inr` and `risk_percent` are configured:
     * `qty_by_risk = max(0, math.floor(risk_budget_inr / distance))`
     * If `max_tranche_inr` is positive and finite: `qty_by_tranche = max(0, math.floor(float(max_tranche_inr) / entry))`, `quantity = min(qty_by_risk, qty_by_tranche)`, and `sizing_constraint = "tranche_capped" if qty_by_tranche < qty_by_risk else "risk_capped"`.
     * Else: `quantity = qty_by_risk`, `sizing_constraint = "risk_capped"`.
     * Always populate `notional_inr = round(quantity * entry, 2)`, `actual_risk_inr = round(quantity * distance, 2)`, `risk_distance_pct = round((distance / entry) * 100.0, 2)`, and `fits_risk_distance_cap = risk_distance_pct <= float(max_risk_distance_pct)` (when configured).
  3. Update `TelegramNotifier._format_message()` to display `qty`, `notional_inr`, and `actual_risk_inr` when `position_size_status == "available"`.

---

## 4. Phase 2 (P0/P1): Paper Trading Hardening & DB Migrations

### Step 2.1 — Idempotent SQLite Schema Migration (`src/db.py`)
* **File:** `src/db.py`
* **Changes:**
  1. Update `PaperTrade`:
     * Change `__table_args__` unique constraint to:
       `UniqueConstraint("symbol", "direction", "entry_date", "alert_tier", name="uq_paper_trade_symbol_dir_date_tier")`
     * Add columns: `gross_pnl_percent = Column(Float)`, `cost_percent = Column(Float)`, `net_r_multiple = Column(Float)`, `fits_tranche_rules = Column(Boolean)`, `strategy_version = Column(String(30))`.
  2. Update `PaperTradeWeeklyReport`:
     * Add columns: `profit_factor = Column(Float)`, `expectancy_r = Column(Float)`, `buy_trades = Column(Integer, default=0)`, `buy_win_rate = Column(Float)`, `sell_trades = Column(Integer, default=0)`, `sell_win_rate = Column(Float)`.
  3. Add `ensure_schema_migrations(engine)` called immediately after `Base.metadata.create_all(_engine)` in `get_engine()` (and exposed for tests):
     * Inspect `PRAGMA table_info(paper_trades)` and `PRAGMA table_info(paper_trade_weekly_reports)` and run `ALTER TABLE ... ADD COLUMN ...` for any missing columns.
     * Inspect `SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_trades'`; if `uq_paper_trade_symbol_dir_date` exists without `alert_tier`, rebuild `paper_trades` inside a single transaction (`PRAGMA foreign_keys=OFF` -> create temp table with new constraint -> copy all existing rows -> drop old -> rename -> `PRAGMA foreign_keys=ON`).

### Step 2.2 — Fix Tier Isolation, IST Trading-Day Calendar, High/Low Monitoring & Costs (`src/paper_trading.py`, `src/scanner.py`)
* **Files:** `src/paper_trading.py`, `src/scanner.py`, `config/config.yaml`
* **Changes:**
  1. **Allowed Directions Filter:** In `record_paper_trade_entry()`, read `allowed_directions = [d.upper() for d in pt_cfg.get("allowed_directions", ["BUY", "SELL"])]`. Return `None` immediately if `direction.upper() not in allowed_directions`.
  2. **Tier-Isolated Deduplication:** Scope Dedup Rules 1, 2, and 3 by `(symbol, direction, alert_tier)` so `intraday_radar` and `power_hour_confirmed` / `confirmed_daily` maintain separate, uncorrupted virtual books.
  3. **IST Date Standardization & NSE Trading-Day Counter:**
     * Import `IST` and `is_trading_day`, `CalendarUnavailable` from `src.market_calendar`.
     * Replace every `date.today()` in `src/paper_trading.py` with `datetime.now(IST).date()`.
     * Add `trading_days_between(start_day: date, end_day: date, market_cfg: Optional[dict] = None) -> int` counting trading sessions strictly after `start_day` through `end_day` using `is_trading_day()` (falling back to weekday count on `CalendarUnavailable`).
     * Use `trading_days_between()` for `max_hold_days` and for `cooldown_days` (measuring cooldown since `PaperTrade.exit_date` of the latest closed trade for that `(symbol, direction, alert_tier)`, NOT `entry_date`).
  4. **Ensure Open Paper Trades Always Receive Quotes (`src/scanner.py`):**
     * Before `self.fetcher.get_full_market_quotes(...)` in `Scanner.run_scan()`, query distinct open symbols from `PaperTrade` (`status == "open"`), resolve their `Stock.instrument_key`, and include them in the quote request batch (keeping `stocks` / coverage math strictly tied to the configured universe).
     * Map `quotes_by_symbol` from **all** fetched `fresh_quotes` (using `{instrument_key: symbol}` for universe + open paper trade stocks), not just `tradable_candidates`.
  5. **Intraday High/Low Exit & MFE/MAE Realism (`src/paper_trading.py`):**
     * In `monitor_open_paper_trades()`, read `current_price = float(quote.get("last_price", 0.0))`, `day_open = float(quote.get("open") or current_price)`, `day_high = float(quote.get("high") or current_price)`, `day_low = float(quote.get("low") or current_price)`.
     * **Entry-day guard:** If `cur_date == trade.entry_date`, use `current_price` only (prevents pre-entry morning highs/lows from creating phantom fills).
     * **Subsequent sessions (`cur_date > trade.entry_date`):**
       * For `BUY`: update `max_favorable_excursion = max(mfe, day_high)` and `max_adverse_excursion = min(mae, day_low)`. Evaluate stop-first collision rule: if `day_low <= trade.stop_loss`, close as `stop_hit` at `min(day_open, trade.stop_loss)`; else if `day_high >= trade.target_price`, close as `target_hit` at `trade.target_price`.
       * For `SELL`: symmetric inverse (`day_high >= trade.stop_loss` -> `max(day_open, trade.stop_loss)`; `day_low <= trade.target_price` -> `trade.target_price`).
  6. **Transaction Costs & Net R-Multiple:**
     * Read `cost_pct = float(pt_cfg.get("round_trip_cost_pct", 0.30))`.
     * On trade close, compute `gross_pnl_percent`, store `cost_percent = cost_pct`, set net `pnl_percent = round(gross_pnl_percent - cost_pct, 2)`, and compute `net_r_multiple = round(pnl_percent / (risk_distance / entry_price * 100.0), 2)` when `risk_distance > 0`.
  7. **Store Strategy Metadata:** Persist `strategy_version = str(pt_cfg.get("strategy_version", "v1_baseline"))` and `fits_tranche_rules` (boolean: `risk_distance / entry_price * 100 <= max_risk_distance_pct`).
  8. **Upgrade Weekly Scorecard (`generate_weekly_report()`):**
     * Compute `profit_factor`, `expectancy_r` (mean `net_r_multiple`), `buy_trades` / `buy_win_rate`, `sell_trades` / `sell_win_rate`, and mean MFE%/MAE% across closed trades.
     * Include Expectancy (R) and Profit Factor in the Saturday Telegram summary.
     * Set `paper_trading.send_milestone_alerts: false` by default in `config.yaml` (milestones still persist to `paper_trade_milestones` DB table without spamming Telegram).

---

## 5. Phase 3 (P1): High-Impact Signal & Institutional Upgrades

> **Rollout Gate:** Run Phases 1–2 under `strategy_version: "v1_baseline"` for 2–4 weeks (and run offline backtests via `python -m src.backtest.cli`) before enabling Phase 3 toggles under `strategy_version: "v2_enhanced"`.

### Step 3.1 — Cross-Sectional Relative Strength Rating (1–99)
* **Files:** `src/market_context.py`, `src/scanner.py`, `src/notifiers/base.py`, `src/notifiers/telegram.py`, `config/config.yaml`
* **Implementation:**
  1. Add `compute_universe_rs_ratings(frames: dict[str, pd.DataFrame], benchmark: pd.DataFrame, *, lookback_short: int = 20, lookback_long: int = 50) -> dict[str, dict]` to `src/market_context.py`:
     * For each symbol, compute `rs_short = relative_strength(frame, benchmark, lookback=lookback_short)` and `rs_long = relative_strength(frame, benchmark, lookback=lookback_long)`.
     * Composite excess return: `$0.6 \times \text{rs}_{20} + 0.4 \times \text{rs}_{50}$` (fallback to `$\text{rs}_{20}$` if `rs_long` unavailable).
     * Rank all available symbols into a 1–99 integer percentile (`rs_rating`).
  2. Call once per scan in `Scanner.run_scan()` after `valid_frames` is built (zero extra API calls).
  3. Add guardrail `guardrails.relative_strength`:
     * For `BUY`: reject if `rs_rating < min_rs_rating_buy` (default `60`) or (`require_positive_excess_buy` and `rs_20_pct <= 0`).
     * For `SELL`: reject if `rs_rating > max_rs_rating_sell` (default `40`).
  4. Render `RS Rating: 84/99 (+6.2% vs Nifty)` in Telegram alerts.

### Step 3.2 — Bollinger Band Squeeze -> Expansion (`src/signals/bollinger.py`) + Safe 4-Group Confluence
* **Files:** `src/signals/bollinger.py` (new), `src/signals/confluence.py`, `src/config_loader.py`, `src/scanner.py`, `config/config.yaml`
* **Implementation:**
  1. Create `src/signals/bollinger.py` with `BollingerResult` dataclass and `detect_bollinger_squeeze(symbol, df, period=20, num_std=2.0, squeeze_percentile=20.0, squeeze_lookback=5, percentile_window=120)`:
     * `mid = close.rolling(period).mean()`, `std = close.rolling(period).std(ddof=0)`
     * `upper = mid + num_std * std`, `lower = mid - num_std * std`
     * `bandwidth = (upper - lower) / mid`
     * Compute rolling percentile rank of `bandwidth` over `min(percentile_window, available_bw_bars)` (require `>= 60` bars minimum).
     * Trigger `bb_squeeze_breakout_up` (`BUY`) when: (a) bandwidth percentile was `<= squeeze_percentile` on at least one of the prior `squeeze_lookback` bars, (b) `close.iloc[-1] > upper.iloc[-1]`, and (c) `bandwidth.iloc[-1] > bandwidth.iloc[-2]` (confirmed volatility expansion). Symmetric for `bb_squeeze_breakout_down` (`SELL`).
  2. **Prevent Confluence Loosening:** Adding a 4th group (`"volatility"`) while keeping `min_groups_to_alert: 2` increases trigger pairs from `$\binom{3}{2} = 3$` to `$\binom{4}{2} = 6$`.
     * Update `SIGNAL_GROUPS = ("trend", "momentum", "participation", "volatility")` in `src/signals/confluence.py`, map `bb_* -> "volatility"`, and update validation in both `confluence.py` and `config_loader.py` from hardcoded `3` to `len(SIGNAL_GROUPS)`.
     * Add `confluence.require_trend_or_momentum: true` in `evaluate_confluence()`: when enabled, reject alerts whose agreeing votes come *only* from `{"participation", "volatility"}` without at least one aligned `trend` or `momentum` state.
  3. **ADX Squeeze Exemption:** In `Scanner._scan_stock()`, when `bb_squeeze` fired in the consensus direction and `guardrails.adx_filter.allow_rising_adx_on_bb_squeeze` is true, allow ADX to pass if `adx >= squeeze_min_adx` (`15.0`), `adx_today > adx_yesterday`, and `+DI` / `-DI` alignment holds.

### Step 3.3 — Supertrend Trailing Stop & Optional Trend Signal (`src/signals/supertrend.py`)
* **Files:** `src/signals/supertrend.py` (new), `src/paper_trading.py`, `src/backtest/engine.py`, `src/signals/confluence.py`, `config/config.yaml`
* **Implementation:**
  1. Create `compute_supertrend(df, period=10, multiplier=3.0)` using Wilder ATR (`ewm(alpha=1/period, min_periods=period, adjust=False)`) and standard band ratcheting:
     * `hl2 = (high + low) / 2.0`, `basic_upper = hl2 + multiplier * atr`, `basic_lower = hl2 - multiplier * atr`.
     * Ratchet `final_upper` down only when `prev_close <= prev_final_upper`; ratchet `final_lower` up only when `prev_close >= prev_final_lower`.
     * Flip trend state when `close` crosses the active band.
  2. **Primary Role (Trailing Stop Engine):** Add `paper_trading.trailing_stop_mode` (`"none" | "breakeven_1r" | "supertrend"`):
     * `"breakeven_1r"`: Once a `BUY` trade's MFE reaches `entry_price + risk_distance`, ratchet `stop_loss = max(stop_loss, entry_price * (1 + cost_pct / 100.0))`.
     * `"supertrend"`: On each new session (`cur_date > trade.entry_date`), if trade is in profit, ratchet `stop_loss = max(stop_loss, completed_supertrend_val)` (never loosen).
  3. **Secondary Role (Optional Entry Signal):** Provide `detect_supertrend(symbol, df, period=10, multiplier=3.0)` mapped to `"trend"` in `confluence.py`, defaulting to `signals.supertrend.enabled: false` until proven via backtest.

### Step 3.4 — NSE Post-Close Delivery Volume Confirmation (`src/fetcher/nse_delivery.py`)
* **Files:** `src/fetcher/nse_delivery.py` (new), `src/db.py`, `src/main.py`, `src/scanner.py`
* **Endpoint:** `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv` (published ~18:00 IST on NSE trading days; requires standard browser `User-Agent` header).
* **Mandatory Parser Safeguards:**
  1. Strip whitespace from **all** column headers and string values (NSE pads fields like `" SERIES"`, `" EQ"`, `" DELIV_PER"`).
  2. Parse the internal `DATE1` column (`%d-%b-%Y`) and **reject the file** if `parsed_date != requested_date` (on some holidays NSE returns HTTP 200 with the previous day's file instead of 404).
  3. Filter strictly to `SERIES == "EQ"`.
  4. Persist into a new `DeliveryHistory` table (`symbol`, `trade_date`, `traded_qty`, `delivery_qty`, `delivery_pct`; unique `(symbol, trade_date)`).
  5. Compute `delivery_qty_ratio = today_delivery_qty / avg_10d_delivery_qty`. Tag post-close alerts with `Institutional Accumulation` when `delivery_pct >= 45.0` and `delivery_qty_ratio >= 1.5`, or `Speculative Intraday Churn` when `delivery_pct < 30.0`.
  6. Schedule at `18:45 IST` (Mon–Fri) in `src/main.py`. Never block intraday / 3:15 PM scans on unavailable same-day delivery data.

### Step 3.5 — Earnings & Corporate Action Blackout Filter (`src/fetcher/nse_events.py`)
* **Files:** `src/fetcher/nse_events.py` (new), `src/db.py`, `src/main.py`, `src/scanner.py`, `src/notifiers/telegram.py`
* **Endpoints:**
  * Board meetings (quarterly results): `https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={DD-MM-YYYY}&to_date={DD-MM-YYYY}`
  * Corporate actions (dividends/splits/bonus): `https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={DD-MM-YYYY}&to_date={DD-MM-YYYY}`
* **Implementation:**
  1. Warm a `requests.Session` against `https://www.nseindia.com` once per morning refresh (`08:25 IST` in `src/main.py`), fetch the next 14 calendar days, and store into `CorporateEventCache` (`symbol`, `event_type`, `event_date`, `purpose`, `fetched_at`).
  2. Check upcoming events within `guardrails.event_blackout.blackout_trading_days` (default `3`):
     * `mode: "warn"` -> attach a prominent `EVENT WARNING` banner to the alert payload.
     * `mode: "filter"` -> suppress the alert entirely.
     * `fail_open: true` -> log a warning and allow scanning if NSE blocks or errors.

---

## 6. Phase 4 (P2): Deferred Refinements (Post-Baseline Only)

Implement only after `PaperTradeWeeklyReport` and `src.backtest.cli` establish positive net R-expectancy on Phases 1–3:
1. **MACD Histogram Acceleration (`src/signals/macd.py`):** Add `histogram_bars_rising` and `histogram_slope` metadata to `MACDResult` as informational context.
2. **OBV Divergence (`src/signals/obv.py`):** Add 20-bar normalized price vs. OBV slope comparison to flag bearish distribution divergences on `BUY` breakouts.
3. **Candlestick Confirmation Badges (`src/signals/candlestick.py`):** Detect `bullish_engulfing`, `hammer`, and `morning_star` purely as informational alert badges (never an independent voting group).
4. **Portfolio-Aware Backtester (`src/backtest/engine.py`):** Add a multi-position simulator modeling `max_concurrent_positions: 5`, `max_tranche_inr: 100000`, and `max_risk_inr: 5000` to measure true portfolio drawdown.

---

## 7. Configuration Specification (`config/config.yaml`)

Add or update these sections in `config/config.yaml` and add matching validation in `src/config_loader.py`:

```yaml
guardrails:
  # Preserve existing wick_filter, volume_pace, two_tier_alerts

  trend_gate:
    enabled: true
    fast_ema: 50
    slow_ema: 200
    require_price_above_fast: true
    require_fast_above_slow: true
    fail_closed_on_short_history: false
    apply_to_provisional: true
    apply_to_daily: true

  rsi_exhaustion:
    enabled: true
    max_rsi_buy: 68.0
    min_rsi_sell: 32.0
    apply_to_provisional: true
    apply_to_daily: true

  adx_filter:
    enabled: true
    period: 14
    min_adx: 20.0
    strong_trend_threshold: 25.0
    require_di_alignment: true
    allow_rising_adx_on_bb_squeeze: true
    squeeze_min_adx: 15.0
    apply_to_provisional: true
    apply_to_daily: true

  relative_strength:
    enabled: false            # Enable in Phase 3 (v2_enhanced)
    lookback_short: 20
    lookback_long: 50
    min_rs_rating_buy: 60
    require_positive_excess_buy: true
    max_rs_rating_sell: 40

  event_blackout:
    enabled: false            # Enable in Phase 3
    mode: "warn"              # "warn" | "filter"
    blackout_trading_days: 3
    fail_open: true

signals:
  # Preserve existing rsi, macd, ema_crossover, volume_breakout
  bollinger:
    enabled: false            # Enable in Phase 3 after v1 baseline completes
    period: 20
    num_std: 2.0
    squeeze_percentile: 20.0
    squeeze_lookback: 5
    percentile_window: 120

  supertrend:
    enabled: false            # Optional entry vote; primary use is trailing stop
    period: 10
    multiplier: 3.0

confluence:
  min_groups_to_alert: 2
  direction_consensus: true
  mode: "crossover"
  require_trend_or_momentum: true

risk:
  enabled: true
  atr_period: 14
  stop_atr_multiple: 1.5
  target_r_multiple: 2.0
  account_capital_inr: null   # Override to 500000 in config/secrets.yaml
  risk_percent: 1.0           # ₹5,000 max risk on ₹5,00,000
  max_tranche_inr: 100000     # Hard ₹1,00,000 capital cap per trade
  max_risk_distance_pct: 6.0

backtest:
  atr_period: 14
  stop_atr_multiple: 1.5
  target_r_multiple: 2.0
  fee_bps_per_side: 5.0
  slippage_bps_per_side: 5.0
  train_fraction: 0.7
  max_hold_days: null         # Optional: set to 10 to mirror paper_trading
  allowed_directions: ["BUY", "SELL"]

paper_trading:
  enabled: true
  strategy_version: "v1_baseline"
  allowed_directions: ["BUY"] # Long-only CNC delivery validation
  max_hold_days: 10           # Measured in NSE trading days via market_calendar
  cooldown_days: 3            # Measured in NSE trading days since exit_date
  round_trip_cost_pct: 0.30   # Statutory NSE CNC charges + slippage deduction
  trailing_stop_mode: "none"  # "none" | "breakeven_1r" | "supertrend"
  milestones: [2.5, -2.5, 5.0, -5.0]
  send_milestone_alerts: false
  send_exit_alerts: true
  weekly_report_day: "saturday"
  weekly_report_time: "09:00"
```

---

## 8. File & Integration Map

| File | Action | Phase | Summary of Work |
| :--- | :--- | :--- | :--- |
| `src/main.py` | Modify | P1 / P3 | Import `get_session`; schedule `08:25` event refresh & `18:45` delivery refresh. |
| `src/notifiers/discord.py` | Modify | P1 | Set `self.enabled` in `__init__` so `broadcast_message()` works. |
| `src/notifiers/email_notifier.py` | Modify | P1 | Set `self.enabled` in `__init__` so `broadcast_message()` works. |
| `charts/chart_generator.py` | Modify | P1 | Compute EMAs/RSI/MACD on full `df` before slicing `.tail(lookback_candles)`. |
| `src/backtest/cli.py` | Modify | P1 | Wire `evaluator.guardrails_cfg`; hash `"guardrails"` in `config_hash_sha256`. |
| `src/backtest/engine.py` | Modify | P1 / P3 | Add `max_hold_days`, `allowed_directions`, `profit_factor`, `expectancy_r`, optional Supertrend trail. |
| `src/signals/adx.py` | Modify | P1 | Fix warmup `(2 * period) + 1`; enforce `+DI` vs. `-DI` alignment. |
| `src/risk.py` | Modify | P1 | Enforce `max_tranche_inr` cap (`min(qty_by_risk, qty_by_tranche)`), `actual_risk_inr`, `sizing_constraint`. |
| `src/scanner.py` | Modify | P1–P3 | Add `trend_gate`, `rsi_exhaustion`, open-paper-trade quote union, RS rating, Bollinger, and event blackout. |
| `src/db.py` | Modify | P2 / P3 | Add `ensure_schema_migrations()`, tier-scoped `PaperTrade` unique constraint, cost/PF columns, `DeliveryHistory`, `CorporateEventCache`. |
| `src/paper_trading.py` | Modify | P2 | Tier-isolated dedup, IST dates, trading-day hold/cooldown, high/low stop-target checks, `0.30%` cost deduction, `allowed_directions`, PF/Expectancy. |
| `src/config_loader.py` | Modify | P1–P3 | Validate new guardrail configs and dynamic `len(SIGNAL_GROUPS)` upper bound. |
| `src/market_context.py` | Modify | P3 | Add cross-sectional `compute_universe_rs_ratings()` (1–99 percentile). |
| `src/signals/confluence.py` | Modify | P3 | Add `"volatility"` group, `bb_*` & `supertrend_*` mapping, `require_trend_or_momentum`. |
| `src/signals/bollinger.py` | Create | P3 | Bollinger Band squeeze -> expansion detector. |
| `src/signals/supertrend.py` | Create | P3 | Wilder-ATR Supertrend series + trailing stop / direction flip detector. |
| `src/fetcher/nse_delivery.py` | Create | P3 | NSE `sec_bhavdata_full` fetcher with whitespace stripping, `EQ` filter, and `DATE1` check. |
| `src/fetcher/nse_events.py` | Create | P3 | NSE board-meeting & corporate-action cache with `fail_open: true`. |
| `src/notifiers/telegram.py` | Modify | P1 / P3 | Render tranche-capped qty/risk, RS Rating, delivery badge, and earnings warning banner. |

---

## 9. Verification & Test Matrix

All existing `108` tests must continue passing. Add tests for every phase before closing each PR:

### Phase 1 Tests
* `tests/test_paper_trading.py`: Invoke `src.main.generate_weekly_report_job()` with a mocked scanner and in-memory DB; confirm no `NameError`. Test `DiscordNotifier.broadcast_message()` and `EmailNotifier.broadcast_message()` do not raise `AttributeError`.
* `tests/test_signals.py`: Verify `generate_signal_chart()` plots `EMA 200` when given `>= 220` rows even with `lookback_candles=60`.
* `tests/test_adx_guardrail.py`:
  * Confirm `adx_filter(..., direction="BUY")` rejects strong downtrend (`ADX >= 25, -DI > +DI`).
  * Confirm `compute_adx()` / `adx_filter()` require `>= 2 * period + 1` rows.
* `tests/test_guardrails_and_two_tier.py`:
  * Confirm `trend_gate` rejects `BUY` when `EMA 50 < EMA 200` even if `EMA 21x50` + `Volume` fire.
  * Confirm `rsi_exhaustion` rejects `BUY` when `RSI > 68.0` and passes when `55.0 <= RSI <= 68.0`.
* `tests/test_batch_e_backtest_risk.py`:
  * Confirm `run_backtest()` sets `evaluator.guardrails_cfg` and changes `config_hash_sha256` when `guardrails.adx_filter.min_adx` changes.
  * Confirm `risk_reference()` caps quantity at `floor(100000 / entry)` when tight ATR would otherwise deploy `> ₹1,00,000`, and caps at `floor(5000 / risk_distance)` on wide ATR.

### Phase 2 Tests
* `tests/test_paper_trading.py`:
  * Confirm `intraday_radar` (10:30 AM) and `power_hour_confirmed` (3:15 PM) on the same symbol/day both persist separate `PaperTrade` rows.
  * Confirm `allowed_directions: ["BUY"]` skips `SELL` signals.
  * Confirm `trading_days_between()` excludes NSE holidays from `config/nse_trading_calendar.yaml` (e.g., `2026-11-10`).
  * Confirm `cooldown_days` checks `exit_date` of closed trades (not `entry_date`).
  * Confirm prior-day open `BUY` trade closes as `stop_hit` at `min(open, stop_loss)` when `quote["low"] <= stop_loss` even if `quote["last_price"] > stop_loss`, while entry-day trades check `last_price` only.
  * Confirm `0.30%` round-trip cost reduces net `pnl_percent` and `net_r_multiple`.
  * Confirm weekly report outputs `profit_factor`, `expectancy_r`, and `BUY`/`SELL` split.
* `tests/test_db_and_config.py`:
  * Create a legacy v1 `paper_trades` table (with old constraint and missing columns), run `ensure_schema_migrations()`, and verify existing rows survive intact with the new columns and tier-scoped unique constraint.
* `tests/test_upstox_cache.py` or `tests/test_paper_trading.py`:
  * Confirm an open `PaperTrade` symbol excluded from `tradable_candidates` is still unioned into quote fetching and monitored in `Scanner.run_scan()`.

### Phase 3 Tests
* `tests/test_bollinger.py`: Verify squeeze detection, breakout expansion, `require_trend_or_momentum`, and ADX rising-squeeze exemption (`15 <= ADX < 20` and rising).
* `tests/test_supertrend.py`: Verify Wilder-ATR Supertrend ratcheting, direction flips, and trailing-stop ratcheting (never loosening stops).
* `tests/test_nse_delivery.py`: Verify whitespace stripping on headers/values, `SERIES == "EQ"` filtering, and rejection of an HTTP 200 CSV whose `DATE1` column does not match the requested date.
* `tests/test_nse_events.py`: Verify warning/filtering modes and `fail_open: true` behavior on HTTP 403/timeout.
* `tests/test_batch_d_context.py`: Verify `compute_universe_rs_ratings()` outputs `1..99` percentile ranks and blocks `RS < 60` when enabled.

### Execution Command for Antigravity
Run full suite after each phase:
```bash
pytest -v
python -c "from src.config_loader import load_config; load_config(); print('configuration valid')"
```