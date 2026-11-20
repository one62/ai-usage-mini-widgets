"""Shared color theme, typography constants, and compact time/backoff formatters."""

from __future__ import annotations

import time


BG = "#222127"
TEXT = "#FFFFFF"
MUTED = "#C6C3CE"
TRACK = "#424149"
YELLOW = "#F9E29C"
GRADIENT_STOPS = (
    (0.00, "#A5E9C1"),
    (0.28, "#A3E1F7"),
    (0.55, "#F9E29C"),
    (0.78, "#F7C697"),
    (1.00, "#F6AEAE"),
)
WINDOW_WIDTH = 270
WINDOW_HEIGHT = 38
FONT_FAMILY = "NEXON Football Gothic L"
BOLD_FONT_FAMILY = "NEXON Football Gothic B"
RECOVERY_DELAYS = (15, 30, 60, 120, 300)


def recovery_delay(failure_count: int) -> int:
    """Return a bounded backoff while continuing to retry forever."""
    return RECOVERY_DELAYS[min(max(1, failure_count) - 1, len(RECOVERY_DELAYS) - 1)]


def reset_short(timestamp: int | None, *, weekly: bool = False) -> str:
    if timestamp is None:
        return "—"
    seconds = max(0, timestamp - int(time.time()))
    if seconds == 0:
        return "now"
    total_minutes = seconds // 60
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if weekly:
        return f"{days}d {hours}h"
    if total_minutes == 0:
        return "<1m"
    if total_minutes == 1440:
        return "24h 0m"
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def usage_color(used_percent: int) -> str:
    return gradient_color(max(0.0, min(1.0, used_percent / 100)))


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def gradient_color(position: float) -> str:
    """Return a restrained iOS-style usage color along the configured gradient."""
    position = max(0.0, min(1.0, position))
    for index in range(1, len(GRADIENT_STOPS)):
        left_at, left_color = GRADIENT_STOPS[index - 1]
        right_at, right_color = GRADIENT_STOPS[index]
        if position <= right_at:
            span = right_at - left_at
            mix = 0.0 if span == 0 else (position - left_at) / span
            left = _rgb(left_color)
            right = _rgb(right_color)
            channels = tuple(round(a + ((b - a) * mix)) for a, b in zip(left, right))
            return "#{:02X}{:02X}{:02X}".format(*channels)
    return GRADIENT_STOPS[-1][1]
