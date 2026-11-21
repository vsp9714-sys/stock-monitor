"""Notification system exports and factory."""
from src.notifiers.base import AlertPayload, BaseNotifier
from src.notifiers.telegram import TelegramNotifier
from src.notifiers.discord import DiscordNotifier
from src.notifiers.email_notifier import EmailNotifier


def get_notifiers(cfg: dict) -> list[BaseNotifier]:
    """Instantiate enabled notifiers from configuration."""
    notifiers: list[BaseNotifier] = []
    notif_cfg = cfg.get("notifications", {})
    if notif_cfg.get("telegram", {}).get("enabled"):
        notifiers.append(TelegramNotifier(cfg))
    if notif_cfg.get("discord", {}).get("enabled"):
        notifiers.append(DiscordNotifier(cfg))
    if notif_cfg.get("email", {}).get("enabled"):
        notifiers.append(EmailNotifier(cfg))
    return notifiers


__all__ = [
    "AlertPayload",
    "BaseNotifier",
    "TelegramNotifier",
    "DiscordNotifier",
    "EmailNotifier",
    "get_notifiers",
]
