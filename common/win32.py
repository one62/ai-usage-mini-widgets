"""Windows platform helpers: DPI awareness, fonts, monitors, DWM, registry, and single-instance mutex."""

from __future__ import annotations

import atexit
import ctypes
import os
import sys
import tkinter as tk
import winreg
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox
from typing import Callable


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

_USER32_CACHE: ctypes.WinDLL | None = None


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MouseHookData(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def _get_user32() -> ctypes.WinDLL:
    global _USER32_CACHE
    if _USER32_CACHE is None:
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.MonitorFromRect.argtypes = [ctypes.POINTER(_Rect), ctypes.c_ulong]
        api.MonitorFromRect.restype = ctypes.c_void_p
        api.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        api.GetMonitorInfoW.restype = ctypes.c_int
        api.GetSystemMetrics.argtypes = [ctypes.c_int]
        api.GetSystemMetrics.restype = ctypes.c_int
        api.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        api.MessageBoxW.restype = ctypes.c_int
        _USER32_CACHE = api
    return _USER32_CACHE


def _show_native_info(title: str, message: str) -> None:
    if os.name == "nt":
        try:
            _get_user32().MessageBoxW(None, message, title, 0x40)  # MB_OK | MB_ICONINFORMATION
            return
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


def load_bundled_fonts(font_directory: Path | None = None) -> None:
    """Register bundled Nexon Football Gothic fonts privately for this process."""
    if os.name != "nt":
        return
    target_dir = font_directory or DEFAULT_FONT_DIR
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    gdi32.AddFontResourceExW.restype = ctypes.c_int
    gdi32.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    gdi32.RemoveFontResourceExW.restype = wintypes.BOOL
    private_font = 0x10  # FR_PRIVATE: available only to this process.
    for filename in ("NEXONFootballGothic-Light.otf", "NEXONFootballGothic-Bold.otf"):
        path = str(target_dir / filename)
        if gdi32.AddFontResourceExW(path, private_font, None):
            atexit.register(gdi32.RemoveFontResourceExW, path, private_font, None)


def clamp_position(
    x: int,
    y: int,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Keep the entire widget inside a monitor's usable area."""
    left, top, right, bottom = area
    return (
        max(left, min(x, right - width)),
        max(top, min(y, bottom - height)),
    )


def monitor_work_area(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Return the nearest monitor work area, excluding the Windows taskbar."""
    if os.name != "nt":
        return (0, 0, max(width, x + width), max(height, y + height))
    try:
        user32 = _get_user32()
        rect = _Rect(x, y, x + width, y + height)
        monitor = user32.MonitorFromRect(ctypes.byref(rect), 2)  # nearest monitor
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            return (work.left, work.top, work.right, work.bottom)
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    except (AttributeError, OSError):
        return (0, 0, max(width, x + width), max(height, y + height))


def pythonw_command_for(script_path: Path) -> str:
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    selected = pythonw if pythonw.exists() else interpreter
    return f'"{selected}" "{script_path.resolve()}"'


def autostart_enabled_for(run_value: str, script_path: Path) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, run_value)
            return value == pythonw_command_for(script_path)
    except FileNotFoundError:
        return False


def set_autostart_for(run_value: str, script_path: Path, enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, run_value, 0, winreg.REG_SZ, pythonw_command_for(script_path))
        else:
            try:
                winreg.DeleteValue(key, run_value)
            except FileNotFoundError:
                pass


def enable_dpi_awareness() -> None:
    """Use physical pixels for Tk, monitor bounds, and low-level hook points."""
    if os.name != "nt":
        return
    api = ctypes.WinDLL("user32", use_last_error=True)
    try:
        api.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        api.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        api.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        api.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        for context in (-4, -3):  # PER_MONITOR_AWARE_V2, then V1
            api.SetProcessDpiAwarenessContext(ctypes.c_void_p(context))
            # Also covers an executable whose manifest already set a default.
            if api.SetThreadDpiAwarenessContext(ctypes.c_void_p(context)):
                return
    except AttributeError:
        # Early Windows 10 releases lack the context APIs.
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) in (0, -2147024891):  # S_OK or E_ACCESSDENIED
            return
    raise RuntimeError("Could not enable per-monitor DPI awareness")


def run_single_instance(
    mutex_name: str,
    app_title: str,
    create_and_run: Callable[[], None],
) -> None:
    """Ensure only one instance per named Windows mutex runs at a time."""
    if os.name != "nt":
        raise SystemExit("이 위젯은 Windows에서만 실행할 수 있습니다.")

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    error_already_exists = 183
    ctypes.set_last_error(0)
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    error = ctypes.get_last_error()
    if not mutex:
        raise ctypes.WinError(error)

    if error == error_already_exists:
        kernel.CloseHandle(mutex)
        _show_native_info(app_title, "위젯이 이미 실행 중입니다.")
        return

    try:
        create_and_run()
    finally:
        kernel.CloseHandle(mutex)
