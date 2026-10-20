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


def load_config() -> dict[str, Any]:
    """Load and merge config.yaml + secrets.yaml."""
    with open(_CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    if _SECRETS_FILE.exists():
        with open(_SECRETS_FILE) as f:
            secrets = yaml.safe_load(f) or {}
        config = _deep_merge(config, secrets)
    else:
        print(
            f"[config] WARNING: {_SECRETS_FILE} not found. "
            "Copy secrets.yaml.example → secrets.yaml and fill in your keys."
        )

    # Allow environment variable overrides for critical secrets
    _apply_env_overrides(config)
    return config


def _apply_env_overrides(config: dict) -> None:
    """Override config values from environment variables (useful for Docker/CI)."""
    env_map = {
        "UPSTOX_API_KEY": ("upstox", "api_key"),
        "UPSTOX_API_SECRET": ("upstox", "api_secret"),
        "UPSTOX_ACCESS_TOKEN": ("upstox", "access_token"),
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
