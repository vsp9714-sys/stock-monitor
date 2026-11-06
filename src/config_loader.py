"""
Config loader — merges config.yaml + secrets.yaml into a single dict.
Secrets override config values so the main config stays safe to commit.
"""
import os
from pathlib import Path
from typing import Any

import yaml


_CONFIG_DIR = Path(__file__).parent.parent / "config"
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"
_SECRETS_FILE = _CONFIG_DIR / "secrets.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_dotenv() -> None:
    """Load key-value pairs from .env file into os.environ if present."""
    candidate_paths = [
        Path(__file__).parent.parent / ".env",
        Path.home() / ".env",
        Path("/home/ubuntu/.env"),
    ]
    for env_path in candidate_paths:
        if env_path.exists() and env_path.is_file():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        if line.startswith("export "):
                            line = line[len("export "):].strip()
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key and key not in os.environ:
                            os.environ[key] = val
            except Exception:
                pass


def load_config() -> dict[str, Any]:
    """Load and merge config.yaml + secrets.yaml + .env + environment variables."""
    _load_dotenv()

    with open(_CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    if _SECRETS_FILE.exists():
        with open(_SECRETS_FILE) as f:
            secrets = yaml.safe_load(f) or {}
        config = _deep_merge(config, secrets)
    else:
        # Check if critical secrets are supplied via env variables before warning
        has_env_token = bool(os.environ.get("UPSTOX_STATIC_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN"))
        if not has_env_token:
            print(
                f"[config] WARNING: {_SECRETS_FILE} not found. "
                "Copy secrets.yaml.example → secrets.yaml and fill in your keys, or set in .env."
            )

    # Allow environment variable overrides for critical secrets
    _apply_env_overrides(config)
    validate_signal_config(config)
    validate_candidate_context_config(config)
    return config


def validate_signal_config(config: dict[str, Any]) -> None:
    """Fail early for unsupported signal policies or impossible thresholds."""
    confluence = config.get("confluence", {}) or {}
    mode = confluence.get("mode", "crossover")
    if mode not in {"crossover", "trend"}:
        raise ValueError("confluence.mode must be 'crossover' or 'trend'")
    minimum = confluence.get(
        "min_groups_to_alert", confluence.get("min_signals_to_alert", 2)
    )
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 3:
        raise ValueError("confluence.min_groups_to_alert must be an integer from 1 to 3")

    rsi = (config.get("signals", {}) or {}).get("rsi", {}) or {}
    rsi_mode = rsi.get("mode", "mean_reversion")
    if rsi_mode not in {"mean_reversion", "trend_momentum"}:
        raise ValueError("signals.rsi.mode must be 'mean_reversion' or 'trend_momentum'")
    confirmation = rsi.get("confirmation", "level")
    if confirmation not in {"level", "cross_back"}:
        raise ValueError("signals.rsi.confirmation must be 'level' or 'cross_back'")
    try:
        oversold = float(rsi.get("oversold_threshold", 30))
        overbought = float(rsi.get("overbought_threshold", 70))
        bullish = float(rsi.get("bullish_threshold", 55))
        bearish = float(rsi.get("bearish_threshold", 45))
    except (TypeError, ValueError) as exc:
        raise ValueError("RSI thresholds must be numeric values between 0 and 100") from exc
    if not all(0 <= value <= 100 for value in (oversold, overbought, bullish, bearish)):
        raise ValueError("RSI thresholds must be between 0 and 100")
    if oversold >= overbought:
        raise ValueError("signals.rsi.oversold_threshold must be below overbought_threshold")
    if bearish >= bullish:
        raise ValueError("signals.rsi.bearish_threshold must be below bullish_threshold")


def validate_candidate_context_config(config: dict[str, Any]) -> None:
    """Validate deterministic filters and optional contextual data policies."""
    universe = config.get("universe", {}) or {}
    tradability = universe.get("tradability", {}) or {}
    try:
        sessions = int(tradability.get("average_traded_value_sessions", 20))
        observations = int(tradability.get("minimum_observations", sessions))
        minimum_value = float(tradability.get("minimum_average_traded_value_inr", 10_000_000))
    except (TypeError, ValueError) as exc:
        raise ValueError("universe.tradability thresholds must be numeric") from exc
    if sessions < 1 or observations < 1 or observations > sessions or minimum_value < 0:
        raise ValueError("universe.tradability requires positive sessions, 1..sessions observations, and non-negative INR threshold")
    if tradability.get("unknown_policy", "exclude") not in {"exclude", "include"}:
        raise ValueError("universe.tradability.unknown_policy must be 'exclude' or 'include'")

    context = config.get("market_context", {}) or {}
    if context.get("mode", "informational") not in {"informational", "filter"}:
        raise ValueError("market_context.mode must be 'informational' or 'filter'")
    if context.get("filter_unknown_policy", "exclude") not in {"exclude", "include"}:
        raise ValueError("market_context.filter_unknown_policy must be 'exclude' or 'include'")
    short_window = int(context.get("short_sma_sessions", 20))
    long_window = int(context.get("long_sma_sessions", 50))
    lookback = int(context.get("lookback_sessions", 20))
    if min(short_window, long_window, lookback) < 2 or short_window > long_window:
        raise ValueError("market_context windows must be >=2 and short_sma_sessions <= long_sma_sessions")


def _apply_env_overrides(config: dict) -> None:
    """Override config values from environment variables (useful for Docker/CI/.env)."""
    env_map = {
        "UPSTOX_API_KEY": ("upstox", "api_key"),
        "UPSTOX_API_SECRET": ("upstox", "api_secret"),
        "UPSTOX_ACCESS_TOKEN": ("upstox", "access_token"),
        "UPSTOX_STATIC_TOKEN": ("upstox", "access_token"),
        "UPSTOX_TOKEN": ("upstox", "access_token"),
        "UPSTOX_REFRESH_TOKEN": ("upstox", "refresh_token"),
        "TELEGRAM_BOT_TOKEN": ("notifications", "telegram", "bot_token"),
        "TELEGRAM_CHAT_ID": ("notifications", "telegram", "chat_id"),
        "DISCORD_WEBHOOK_URL": ("notifications", "discord", "webhook_url"),
    }
    for env_var, key_path in env_map.items():
        val = os.environ.get(env_var)
        if val:
            node = config
            for key in key_path[:-1]:
                node = node.setdefault(key, {})
            node[key_path[-1]] = val


# Singleton — load once per process
_config: dict[str, Any] | None = None


def get_config() -> dict[str, Any]:
    global _config
    if _config is None:
        _config = load_config()
    return _config
