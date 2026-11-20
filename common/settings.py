"""Shared widget preference persistence and atomic JSON file helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class WidgetSettings:
    transparency: int = 30
    x: int | None = None
    y: int | None = None
    movement_locked: bool = False
    hover_transparency_enabled: bool = True


def app_settings_path(app_dir_name: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or Path.home()
    return Path(base) / app_dir_name / "settings.json"


def _integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    # bool is an int subclass, but is not a slider value or coordinate.
    return type(value) is int and minimum <= value <= maximum


def validate_settings(data: object) -> WidgetSettings:
    settings = WidgetSettings()
    if not isinstance(data, dict):
        return settings

    transparency = data.get("transparency")
    if _integer_in_range(transparency, 0, 100):
        settings.transparency = transparency

    movement_locked = data.get("movement_locked")
    if type(movement_locked) is bool:
        settings.movement_locked = movement_locked

    hover_transparency_enabled = data.get("hover_transparency_enabled")
    if type(hover_transparency_enabled) is bool:
        settings.hover_transparency_enabled = hover_transparency_enabled

    x, y = data.get("x"), data.get("y")
    if _integer_in_range(x, -(2**31), 2**31 - 1) and _integer_in_range(y, -(2**31), 2**31 - 1):
        settings.x, settings.y = x, y
    return settings


def load_settings_from_path(path: Path) -> WidgetSettings:
    """Read settings without creating or changing any files."""
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return WidgetSettings()
    return validate_settings(data)


def save_settings_to_path(settings: WidgetSettings, path: Path) -> None:
    """Atomically replace the settings file, propagating filesystem failures."""
    validated = validate_settings(asdict(settings))
    write_json_atomic(path, asdict(validated))


def write_json_atomic(destination: Path, data: dict) -> None:
    """Write JSON to a temporary file and atomically replace destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.stem}-",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(data, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        # Windows requires the temporary file to be closed before replacement,
        # and antivirus/indexers can briefly hold a transient lock.
        for attempt in range(3):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Keep the original write/replace error for the caller to log.
                pass
