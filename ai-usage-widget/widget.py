"""Entrypoint for the combined AI usage switching widget."""

from __future__ import annotations

import sys
from pathlib import Path

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_widget import RUN_VALUE, UsageWidget, main

__all__ = ["RUN_VALUE", "UsageWidget", "main"]


if __name__ == "__main__":
    main()
