"""
Main entry point — scheduler and lifecycle manager.

Responsibilities:
1. Initialize DB and refresh the verified share universe before scanning
2. Schedule scans every 15 minutes during market hours (IST)
3. Refresh instrument master daily (before market open)
4. Handle graceful shutdown on SIGTERM/SIGINT

Run:
    python -m src.main

Or via systemd:
    systemctl start stock-monitor
"""
import logging
import logging.handlers
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytz
import schedule

from src.config_loader import get_config
from src.db import get_engine
from src.fetcher.upstox import UpstoxFetcher
from src.scanner import Scanner

# ─── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    log_dir = Path(log_cfg.get("log_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "stock_monitor.log"
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=log_cfg.get("backup_count", 7),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)

# ─── Market time helpers ──────────────────────────────────────────────────────

IST = pytz.timezone("Asia/Kolkata")


def is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def next_scan_time() -> str:
    now = datetime.now(IST)
    return now.strftime("%Y-%m-%d %H:%M:%S IST")


# ─── Scheduled jobs ───────────────────────────────────────────────────────────

scanner: Scanner = None
_shutdown = False
_daily_refresh_lock = threading.Lock()


def run_scan_job() -> None:
    """Scheduled job: run market scan if market is open."""
    if not is_market_hours():
        logger.debug("Market closed — skipping scan.")
        return
    logger.info(f"[{next_scan_time()}] Running scheduled scan...")
    try:
        summary = scanner.run_scan()
        if summary.complete:
            logger.info(
                "Scan complete: %s/%s evaluated; %s alerts dispatched.",
                summary.evaluated_count, summary.universe_size, summary.alerts_dispatched,
            )
        else:
            logger.error(
                "Scan incomplete: %s/%s evaluated; %s failures; %s alerts dispatched.",
                summary.evaluated_count, summary.universe_size,
                summary.failed_count, summary.alerts_dispatched,
            )
    except Exception as e:
        logger.error(f"Scan job failed: {e}", exc_info=True)


def refresh_instruments_job() -> None:
    """Scheduled job: refresh instrument master daily before market open."""
    logger.info("Refreshing instrument master (daily job)...")
    try:
        count = scanner.refresh_instrument_master()
        logger.info(f"Instrument master refreshed: {count} stocks.")
    except Exception as e:
        logger.error(f"Instrument refresh failed: {e}", exc_info=True)


def refresh_daily_cache_job() -> None:
    """Probe provider readiness and refresh the latest completed session before market open."""
    if not _daily_refresh_lock.acquire(blocking=False):
        logger.info("Morning daily cache refresh already running; duplicate trigger skipped.")
        return

    def run_refresh() -> None:
        try:
            logger.info("Checking readiness for latest completed daily candles (morning job)...")
            result = scanner.refresh_latest_completed_daily_cache()
            logger.info("Morning daily cache refresh result: %s", result)
        except Exception as e:
            logger.error(f"Morning daily cache refresh failed: {e}", exc_info=True)
        finally:
            _daily_refresh_lock.release()

    try:
        threading.Thread(
            target=run_refresh,
            name="daily-candle-refresh",
            daemon=True,
        ).start()
    except Exception:
        _daily_refresh_lock.release()
        raise


def graceful_shutdown(signum, frame) -> None:
    global _shutdown
    logger.info(f"Received signal {signum}. Shutting down gracefully...")
    _shutdown = True


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    global scanner

    cfg = get_config()
    setup_logging(cfg)

    logger.info("=" * 60)
    logger.info("  Stock Monitor — Indian Market Signal Detector")
    logger.info("=" * 60)

    # Register shutdown handlers
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    # Initialize DB
    get_engine()
    logger.info("Database initialized.")

    # Initialize scanner
    scanner = Scanner()

    # Refresh the verified share universe before any scan. Besides keeping
    # listings current, this deactivates legacy non-share rows (e.g. bonds).
    # The refresh validates both source masters and fails closed on bad data.
    logger.info("Refreshing verified NSE share universe before scanning...")
    scanner.refresh_instrument_master()

    scan_interval = cfg["market"].get("scan_interval_minutes", 15)

    # Schedule: scan every N minutes
    schedule.every(scan_interval).minutes.do(run_scan_job)

    # Schedule: refresh instruments daily at 08:30 server-local time.
    schedule.every().day.at("08:30").do(refresh_instruments_job)

    # Probe Upstox's latest completed session twice in the requested 06:00-07:00
    # IST window. The paced full pass starts only when the provider is ready;
    # otherwise the existing confirmed cache remains available.
    schedule.every().day.at("06:30", "Asia/Kolkata").do(refresh_daily_cache_job)
    schedule.every().day.at("06:55", "Asia/Kolkata").do(refresh_daily_cache_job)

    logger.info(f"Scheduler started. Scan interval: every {scan_interval} minutes.")
    logger.info("Market hours: 09:15 – 15:30 IST, Mon–Fri")
    logger.info("Waiting for next scan window...")

    # Run an immediate scan if market is open right now
    if is_market_hours():
        logger.info("Market is currently open — running initial scan now.")
        run_scan_job()

    # Main loop
    while not _shutdown:
        schedule.run_pending()
        time.sleep(30)  # Check every 30 seconds

    logger.info("Stock Monitor shut down.")


if __name__ == "__main__":
    main()
