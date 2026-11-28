"""Persist the combined AI usage widget's preferences, position, and rotation settings."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from common.settings import (
    WidgetSettings,
    app_settings_path,
    validate_settings as validate_base_settings,
    write_json_atomic,
)


PROVIDER_KEYS = ("codex", "gemini")
ROTATE_INTERVALS = (0, 1, 3, 5, 10, 30)
DEFAULT_PROVIDER = "codex"
DEFAULT_ROTATE_INTERVAL = 10


@dataclass
class CombinedWidgetSettings(WidgetSettings):
    active_provider: str = DEFAULT_PROVIDER
    rotate_interval_seconds: int = DEFAULT_ROTATE_INTERVAL

    @property
    def auto_rotate(self) -> bool:
        return self.rotate_interval_seconds > 0


def settings_path() -> Path:
    return app_settings_path("AIUsageMini")


def validate_settings(data: object) -> CombinedWidgetSettings:
    base = validate_base_settings(data)
    settings = CombinedWidgetSettings(
        transparency=base.transparency,
        x=base.x,
        y=base.y,
        movement_locked=base.movement_locked,
        hover_transparency_enabled=base.hover_transparency_enabled,
    )
    if not isinstance(data, dict):
        return settings

    active_provider = data.get("active_provider")
    if isinstance(active_provider, str) and active_provider in PROVIDER_KEYS:
        settings.active_provider = active_provider

    rotate_interval = data.get("rotate_interval_seconds")
    if type(rotate_interval) is int and rotate_interval in ROTATE_INTERVALS:
        settings.rotate_interval_seconds = rotate_interval

    # Backward compatibility with legacy boolean auto_rotate=False.
    if data.get("auto_rotate") is False:
        settings.rotate_interval_seconds = 0

    return settings


def load_settings_from_path(path: Path) -> CombinedWidgetSettings:
    """Read settings without creating or changing any files."""
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return CombinedWidgetSettings()
    return validate_settings(data)


def save_settings_to_path(settings: CombinedWidgetSettings, path: Path) -> None:
    """Atomically replace the settings file, propagating filesystem failures."""
    validated = validate_settings(asdict(settings))
    write_json_atomic(path, asdict(validated))


def load_settings() -> CombinedWidgetSettings:
    """Read settings without creating or changing any files."""
    return load_settings_from_path(settings_path())


def save_settings(settings: CombinedWidgetSettings) -> None:
    """Atomically replace the settings file, propagating filesystem failures."""
    save_settings_to_path(settings, settings_path())
