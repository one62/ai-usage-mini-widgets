"""Compatibility re-export for Gemini widget settings."""

from __future__ import annotations

from gemini_settings import (
    WidgetSettings,
    load_settings,
    save_settings,
    settings_path,
)

__all__ = [
    "WidgetSettings",
    "load_settings",
    "save_settings",
    "settings_path",
]
