# Upstox V3 quote-field contract

The scanner uses two different data products for different purposes:

- Historical daily candles are canonical inputs to RSI/MACD/EMA/volume signal checks.
- Full Market Quotes V3 supplies the live quote price for an alert. Its `last_price` is the LTP, and `last_trade_time` is the epoch-millisecond timestamp checked for freshness (default maximum age: 300 seconds; configurable as `upstox.max_quote_age_seconds`).

Upstox's official [Full Market Quotes V3 documentation](https://upstox.com/developer/api-documentation/get-full-market-quote-v3/) shows `ohlc` (`open`, `high`, `low`, `close`, `volume`, `ts`) separately from top-level `last_price`, `last_trade_time`, and `prev_close_price`. The official [OHLC Quotes documentation](https://upstox.com/developer/api-documentation/get-market-quote-ohlc/) describes the 1-day response as `live_ohlc`, with previous-day OHLC available from Historical Candle Data. The V3 example also shows `ohlc.ts` separately from `last_trade_time`; accordingly, `ohlc.ts` is not used as a freshness timestamp.

Therefore, quote `ohlc.close` is treated only as part of the live session snapshot. It is neither presumed to equal LTP nor persisted as a finalized daily close. `prev_close_price` is retained as the explicit prior-close field. Missing required LTP fields or an absent/stale trade timestamp makes that symbol unavailable for alerting; the scanner does not fall back to `ohlc.close`.

A synthetic regression fixture intentionally provides different values for `ohlc.close`, `prev_close_price`, and `last_price` to guard this contract without live credentials.

## Daily-bar completion and refresh

The scanner's swing indicators consume only `ohlcv_cache` daily candles. Intraday quote snapshots never enter that cache. The service refreshes the cache at 15:45 Asia/Kolkata on weekdays using Upstox historical daily candles, not quote OHLC. The refresh starts from each symbol's latest cached session, uses the existing historical-request limiter and bounded retry behavior, commits in checkpoints, and is idempotent on replay. A refresh run before 15:45 or on weekends is skipped. If Upstox has not published a session's completed daily candle yet, that symbol is unchanged and a later post-close run can retrieve it.
