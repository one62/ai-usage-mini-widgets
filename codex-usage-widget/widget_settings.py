"""Compatibility re-export for Codex widget settings."""

from __future__ import annotations

from codex_settings import (
    PendingReset,
    WidgetSettings,
    load_pending_reset,
    load_settings,
    save_pending_reset,
    save_settings,
    settings_path,
)

__all__ = [
    "PendingReset",
    "WidgetSettings",
    "load_pending_reset",
    "load_settings",
    "save_pending_reset",
    "save_settings",
    "settings_path",
]
