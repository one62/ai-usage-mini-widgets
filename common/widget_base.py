"""Shared base widget class and public facade for usage widgets."""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox

from common.formatting import (
    BG,
    BOLD_FONT_FAMILY,
    FONT_FAMILY,
    GRADIENT_STOPS,
    MUTED,
    RECOVERY_DELAYS,
    TEXT,
    TRACK,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    YELLOW,
    gradient_color,
    recovery_delay,
    reset_short,
    usage_color,
)
from common.popup_menu import PopupMenu
from common.win32 import (
    DEFAULT_FONT_DIR,
    RUN_KEY,
    _MonitorInfo,
    _MouseHookData,
    _Rect,
    apply_rounded_corners,
    autostart_enabled_for,
    clamp_position,
    enable_dpi_awareness,
    load_bundled_fonts,
    monitor_work_area,
    pythonw_command_for,
    run_single_instance,
    set_autostart_for,
)

__all__ = [
    "BG",
    "BOLD_FONT_FAMILY",
    "DEFAULT_FONT_DIR",
    "FONT_FAMILY",
    "GRADIENT_STOPS",
    "MUTED",
    "RECOVERY_DELAYS",
    "RUN_KEY",
    "TEXT",
    "TRACK",
    "WINDOW_HEIGHT",
    "WINDOW_WIDTH",
    "YELLOW",
    "BaseUsageWidget",
    "PopupMenu",
    "_MonitorInfo",
    "_MouseHookData",
    "_Rect",
    "apply_rounded_corners",
    "autostart_enabled_for",
    "clamp_position",
    "enable_dpi_awareness",
    "gradient_color",
    "load_bundled_fonts",
    "monitor_work_area",
    "pythonw_command_for",
    "recovery_delay",
    "reset_short",
    "run_single_instance",
    "set_autostart_for",
    "usage_color",
]


class BaseUsageWidget:
    """Common windowing, hook, transparency, and layout behavior for usage widgets."""

    APP_TITLE = "Usage"
    RUN_VALUE = ""

    def _init_base(self, *, script_path: Path) -> None:
        enable_dpi_awareness()
        load_bundled_fonts()
        self._script_path = script_path
        self.settings = self._load_widget_settings()
        self._last_saved_state = self._settings_tuple(self.settings)
        self._settings_after_id: str | None = None
        self._settings_ready = False
        self._closing = False
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(self.APP_TITLE)
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.transparency = tk.IntVar(master=self.root, value=self.settings.transparency)
        self.movement_locked = tk.BooleanVar(master=self.root, value=self.settings.movement_locked)
        self.hover_transparency_enabled = tk.BooleanVar(
            master=self.root,
            value=self.settings.hover_transparency_enabled,
        )
        self._hovered = False
        self.root.attributes("-alpha", 1.0 - self.transparency.get() / 100)
        self.snapshot = None
        self.drag_origin: tuple[int, int] | None = None
        self.results: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.refreshing = False
        self.fetch_generation = 0
        self.failure_count = 0
        self.recovery_after_id: str | None = None
        self._scheduled_after_id: str | None = None
        self._watchdog_after_id: str | None = None
        self._drain_results_after_id: str | None = None
        self._hover_after_id: str | None = None
        self.connection_failed = False
        self.last_error = ""
        self.auth_required = False
        self.always_on_top = tk.BooleanVar(master=self.root, value=True)
        self.autostart = tk.BooleanVar(
            master=self.root,
            value=autostart_enabled_for(self.RUN_VALUE, self._script_path),
        )

    @staticmethod
    def _settings_tuple(settings) -> tuple[int, int | None, int | None, bool, bool]:
        return (
            settings.transparency,
            settings.x,
            settings.y,
            settings.movement_locked,
            settings.hover_transparency_enabled,
        )

    def _finalize_init(self) -> None:
        self._restore_position()
        self.root.deiconify()
        self.root.update_idletasks()
        self._apply_rounded_corners()
        self._bind_actions()
        self._enable_click_through()
        self._settings_ready = True
        self._ensure_hover_timer()
        self.refresh()
        self._start_extra_timers()
        self.root.after(60_000, self._update_clock)

    # Subclass extension points (with safe defaults for partial test instances)
    def _load_widget_settings(self):
        raise NotImplementedError

    def _save_widget_settings(self, settings) -> None:
        raise NotImplementedError

    def _start_extra_timers(self) -> None:
        pass

    def _can_start_refresh(self) -> bool:
        return not self.refreshing

    def _on_refresh_state_changed(self) -> None:
        pass

    def _query_usage(self, timeout: float = 18.0):
        raise NotImplementedError

    def _is_non_retryable_fetch_error(self, exc: Exception) -> bool:
        return False

    def _open_app(self) -> None:
        pass

    def _render(self, snapshot) -> None:
        raise NotImplementedError

    def _update_time_labels(self) -> None:
        if self.snapshot:
            self._render(self.snapshot)

    def _cancel_timer(self, attr_name: str) -> None:
        timer_id = getattr(self, attr_name, None)
        if timer_id is not None:
            setattr(self, attr_name, None)
            try:
                self.root.after_cancel(timer_id)
            except tk.TclError:
                pass

    def _ensure_drain_results(self) -> None:
        if getattr(self, "_drain_results_after_id", None) is None and not getattr(self, "_closing", False):
            self._drain_results_after_id = self.root.after(100, self._drain_results)

    def refresh(self) -> None:
        if not self._can_start_refresh():
            return
        self._cancel_timer("_scheduled_after_id")
        self._cancel_timer("recovery_after_id")
        self._cancel_timer("_watchdog_after_id")
        self.refreshing = True
        self.fetch_generation += 1
        generation = self.fetch_generation
        self.menu.entryconfigure(0, state="disabled")
        self._update_connection_status()
        self._on_refresh_state_changed()
        if self.snapshot is None:
            self.reset_compact.configure(text="…", fg=MUTED)
        threading.Thread(target=self._fetch_worker, args=(generation,), daemon=True).start()
        self._watchdog_after_id = self.root.after(50_000, lambda: self._fetch_watchdog(generation))
        self._ensure_drain_results()

    def _fetch_worker(self, generation: int) -> None:
        last_error: Exception | None = None
        for delay in (0.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                self.results.put((generation, "ok", self._query_usage(timeout=18.0)))
                return
            except Exception as exc:
                if self._is_non_retryable_fetch_error(exc):
                    self.results.put((generation, "error", exc))
                    return
                last_error = exc
        self.results.put(
            (generation, "error", last_error or RuntimeError("알 수 없는 오류"))
        )

    def _fetch_watchdog(self, generation: int) -> None:
        self._watchdog_after_id = None
        if not self.refreshing or generation != self.fetch_generation:
            return
        self.fetch_generation += 1  # Ignore a late result from the stuck worker.
        self.refreshing = False
        self.menu.entryconfigure(0, state="normal")
        self._handle_connection_error("사용량 조회가 50초를 초과했습니다.")

    def _drain_results(self) -> None:
        self._drain_results_after_id = None
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                try:
                    generation, kind, payload = self.results.get_nowait()
                except queue.Empty:
                    break
                if generation != self.fetch_generation:
                    continue
                self._cancel_timer("_watchdog_after_id")
                self.refreshing = False
                self.menu.entryconfigure(0, state="normal")
                if kind == "ok" and payload is not None:
                    self._cancel_timer("recovery_after_id")
                    self._cancel_timer("_scheduled_after_id")
                    self.connection_failed = False
                    self.last_error = ""
                    self.auth_required = False
                    self.failure_count = 0
                    self.snapshot = payload
                    self._update_connection_status()
                    self._on_refresh_state_changed()
                    self._render(payload)
                    self._scheduled_after_id = self.root.after(60_000, self._scheduled_refresh)
                else:
                    auth_required = isinstance(payload, Exception) and self._is_non_retryable_fetch_error(payload)
                    message = str(payload) if payload else "알 수 없는 오류"
                    self._handle_connection_error(message, auth_required=auth_required)
        except Exception as exc:
            self.refreshing = False
            try:
                self.menu.entryconfigure(0, state="normal")
            except tk.TclError:
                pass
            self._handle_connection_error(str(exc) or "렌더링 오류")
        finally:
            if self.refreshing and not getattr(self, "_closing", False):
                self._drain_results_after_id = self.root.after(100, self._drain_results)

    def _handle_connection_error(self, message: str = "", *, auth_required: bool = False) -> None:
        self.connection_failed = True
        self.last_error = message or "connection failed"
        self.auth_required = auth_required
        self._cancel_timer("_scheduled_after_id")
        if auth_required:
            self._cancel_timer("recovery_after_id")
        self._update_connection_status()
        self._on_refresh_state_changed()
        self.failure_count += 1
        self._render_connection_issue()
        if not auth_required and getattr(self, "recovery_after_id", None) is None:
            delay = recovery_delay(self.failure_count)
            self.recovery_after_id = self.root.after(delay * 1000, self._recovery_refresh)

    def _render_connection_issue(self) -> None:
        if not getattr(self, "connection_failed", False) and not getattr(self, "last_error", ""):
            return
        if (
            self.snapshot is not None
            and self.failure_count < 3
            and not getattr(self, "auth_required", False)
        ):
            self.reset_compact.configure(text="retry", fg=YELLOW)
        else:
            self.reset_compact.configure(text="offline", fg=MUTED)

    def _on_connection_status_click(self) -> None:
        if getattr(self, "auth_required", False):
            self._open_app()
        elif getattr(self, "connection_failed", False) or getattr(self, "last_error", ""):
            self._show_last_error()

    def _show_last_error(self) -> None:
        detail = getattr(self, "last_error", "") or "알 수 없는 연결 오류입니다."
        messagebox.showinfo(
            self.APP_TITLE,
            f"최근 연결 오류:\n\n{detail}",
            parent=self.root,
        )
        self._ensure_topmost()

    def _update_connection_status(self) -> None:
        """Display cached query state without starting a connection check."""
        clickable = False
        if self.refreshing:
            status = "조회 중"
        elif getattr(self, "auth_required", False):
            status = "로그인 필요"
            clickable = True
        elif getattr(self, "connection_failed", False) or getattr(self, "last_error", ""):
            status = "재연결 중"
            clickable = True
        elif self.snapshot is not None:
            status = "정상"
        else:
            status = "확인 전"
        self.menu.entryconfigure(
            self.connection_status_index,
            label=f"연결 상태: {status}",
            state="normal" if clickable else "disabled",
        )

    def _render_metric_window(
        self,
        *,
        value_label: tk.Label,
        reset_label: tk.Label,
        meter: tk.Canvas,
        used_percent: int | None,
        resets_at: int | None,
        weekly: bool = False,
        title_label: tk.Label | None = None,
        fixed_fg: str | None = None,
    ) -> None:
        if used_percent is not None:
            fg = fixed_fg if fixed_fg is not None else usage_color(used_percent)
            if fixed_fg is None and title_label is not None:
                title_label.configure(fg=fg)
            value_label.configure(text=f"{used_percent}%", fg=fg)
            if weekly:
                reset_label.configure(text=reset_short(resets_at, weekly=True))
            else:
                reset_label.configure(text=reset_short(resets_at), fg=MUTED)
            self.root.after_idle(lambda p=used_percent, m=meter: self._draw_segments(m, p))
        else:
            fg = fixed_fg if fixed_fg is not None else TEXT
            if fixed_fg is None and title_label is not None:
                title_label.configure(fg=fg)
            value_label.configure(text="—%", fg=fg)
            if weekly:
                reset_label.configure(text=reset_short(None, weekly=True))
            else:
                reset_label.configure(text="—", fg=MUTED)
            self._draw_segments(meter, None)

    def _build_header_shell(
        self,
        *,
        logo_path: Path,
        five_fg: str,
        week_fg: str,
    ) -> None:
        outer = tk.Frame(self.root, bg=BG, highlightbackground="#56525F", highlightthickness=1)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG, height=36, cursor="fleur")
        header.pack(fill="both", expand=True, padx=7)
        header.pack_propagate(True)
        self.header = header

        try:
            self._logo_image = tk.PhotoImage(master=self.root, file=str(logo_path))
            tk.Label(header, image=self._logo_image, bg=BG, bd=0).pack(side="left", padx=(2, 4))
        except tk.TclError:
            pass

        percent_font = tkfont.Font(self.root, family=BOLD_FONT_FAMILY, size=9)
        reset_font = tkfont.Font(self.root, family=FONT_FAMILY, size=9)

        def add_right_aligned_label(
            text: str,
            max_text: str,
            color: str,
            font: tkfont.Font,
            padx: tuple[int, int] = (0, 0),
        ) -> tk.Label:
            # Measure with Tk's active font metrics, which already reflect DPI scaling.
            column = tk.Frame(header, width=font.measure(max_text), height=18, bg=BG)
            column.pack_propagate(False)
            label = tk.Label(
                column,
                text=text,
                anchor="e",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                fg=color,
                bg=BG,
                font=font,
            )
            label.pack(fill="both", expand=True)
            column.pack(side="left", padx=padx)
            return label

        def add_metric_labels(
            title: str,
            value: str,
            max_text: str,
            color: str,
            font: tkfont.Font,
            padx: tuple[int, int] = (1, 0),
        ) -> tuple[tk.Label, tk.Label]:
            column = tk.Frame(header, width=font.measure(max_text), height=18, bg=BG)
            column.pack_propagate(False)
            title_label = tk.Label(
                column,
                text=title,
                anchor="w",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                fg=color,
                bg=BG,
                font=font,
            )
            title_label.pack(side="left", fill="y")
            value_label = tk.Label(
                column,
                text=value,
                anchor="e",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                fg=color,
                bg=BG,
                font=font,
            )
            value_label.pack(side="left", fill="both", expand=True)
            column.pack(side="left", padx=padx)
            return title_label, value_label

        self.five_title, self.five_label = add_metric_labels(
            "5H",
            "—%",
            "5H 100%",
            five_fg,
            percent_font,
        )
        self.five_meter = tk.Canvas(header, width=23, height=10, bg=BG, bd=0, highlightthickness=0)
        self.five_meter.pack(side="left", padx=(3, 5))
        self.reset_compact = add_right_aligned_label(
            "—",
            "5h 59m",
            MUTED,
            reset_font,
            padx=(0, 8),
        )

        tk.Frame(header, bg="#56525F", width=1, height=18).pack(side="left", padx=(0, 5))

        self.week_title, self.week_label = add_metric_labels(
            "WK",
            "—%",
            "WK 100%",
            week_fg,
            percent_font,
        )
        self.week_meter = tk.Canvas(header, width=23, height=10, bg=BG, bd=0, highlightthickness=0)
        self.week_meter.pack(side="left", padx=(3, 5))
        self.week_reset = add_right_aligned_label(
            "—",
            "7d 23h",
            MUTED,
            reset_font,
        )

    def _add_common_preference_menu_items(self, menu: PopupMenu) -> None:
        menu.add_scale(label="투명도", variable=self.transparency, command=self._set_transparency)
        menu.add_checkbutton(
            label="마우스를 올리면 투명도 80%",
            variable=self.hover_transparency_enabled,
            command=self._toggle_hover_transparency,
        )
        menu.add_checkbutton(
            label="이동 잠금",
            variable=self.movement_locked,
            command=self._schedule_settings_save,
        )
        menu.add_separator()
        menu.add_checkbutton(
            label="항상 맨 위에 표시",
            variable=self.always_on_top,
            command=self._toggle_topmost,
        )
        menu.add_checkbutton(
            label="Windows 시작 시 자동 실행",
            variable=self.autostart,
            command=self._toggle_autostart,
        )

    def _fit_content_width(self) -> None:
        """Fit the window to fixed pixel columns measured at the current DPI."""
        self.root.update_idletasks()
        # Label columns have fixed pixel widths, so content changes cannot resize it.
        # Tk font metrics are DPI-aware because awareness is enabled before Tk starts.
        width = max(WINDOW_WIDTH, self.header.winfo_reqwidth() + 16)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _bind_actions(self) -> None:
        self.root.bind("<Escape>", lambda _event: self._close())
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Destroy>", self._release_mouse_hook, add="+")
        self.root.bind("<Configure>", self._position_changed, add="+")

    def _restore_position(self) -> None:
        if self.settings.x is None or self.settings.y is None:
            self._position_top_right()
            return
        self.root.update_idletasks()
        width = max(WINDOW_WIDTH, self.root.winfo_reqwidth(), self.root.winfo_width())
        x, y = self.settings.x, self.settings.y
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _position_changed(self, event: tk.Event) -> None:
        if event.widget is self.root and (self.root.winfo_x(), self.root.winfo_y()) != (
            self.settings.x,
            self.settings.y,
        ):
            self._schedule_settings_save()

    def _schedule_settings_save(self) -> None:
        if not self._settings_ready or self._closing:
            return
        if self._settings_after_id is not None:
            self.root.after_cancel(self._settings_after_id)
        self._settings_after_id = self.root.after(350, self._save_preferences)

    def _save_preferences(self) -> None:
        self._settings_after_id = None
        # Read the Scale variable directly: its command can still be pending
        # when the user closes the widget immediately after the last adjustment.
        self.settings.transparency = self.transparency.get()
        self.settings.movement_locked = self.movement_locked.get()
        self.settings.hover_transparency_enabled = self.hover_transparency_enabled.get()
        self.settings.x, self.settings.y = self.root.winfo_x(), self.root.winfo_y()
        current_state = self._settings_tuple(self.settings)
        if current_state == getattr(self, "_last_saved_state", None):
            return
        try:
            self._save_widget_settings(self.settings)
        except OSError:
            pass
        else:
            self._last_saved_state = current_state

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel_timer("_settings_after_id")
        self._cancel_timer("_scheduled_after_id")
        self._cancel_timer("recovery_after_id")
        self._cancel_timer("_watchdog_after_id")
        self._cancel_timer("_drain_results_after_id")
        self._cancel_timer("_hover_after_id")
        self._save_preferences()
        self.root.destroy()

    def _position_top_right(self) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        width = max(WINDOW_WIDTH, self.root.winfo_reqwidth(), self.root.winfo_width())
        area = monitor_work_area(screen_width - 1, 0, width, WINDOW_HEIGHT)
        x, y = clamp_position(area[2] - width - 18, area[1] + 18, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _apply_rounded_corners(self) -> None:
        """Ask Windows 11 DWM for iOS-like rounded top-level corners."""
        apply_rounded_corners(self.root.winfo_id())

    def _enable_click_through(self) -> None:
        """Keep the body transparent; intercept only right gestures starting on it."""
        self._input_api = ctypes.WinDLL("user32", use_last_error=True)
        api = self._input_api
        self._hook_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        signatures = {
            "GetAncestor": ([ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "GetWindowLongW": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_long),
            "SetWindowLongW": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_long], ctypes.c_long),
            "GetWindowRect": ([ctypes.c_void_p, ctypes.POINTER(_Rect)], ctypes.c_int),
            "IsWindowVisible": ([ctypes.c_void_p], ctypes.c_int),
            "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
            "SetWindowsHookExW": (
                [ctypes.c_int, self._hook_type, ctypes.c_void_p, ctypes.c_ulong],
                ctypes.c_void_p,
            ),
            "CallNextHookEx": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t],
                ctypes.c_ssize_t,
            ),
            "UnhookWindowsHookEx": ([ctypes.c_void_p], ctypes.c_int),
            "PeekMessageW": (
                [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint],
                ctypes.c_int,
            ),
            "GetMessageW": (
                [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint],
                ctypes.c_int,
            ),
            "PostThreadMessageW": (
                [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t],
                ctypes.c_int,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        self.root.update_idletasks()
        self._input_hwnd = api.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        self._mouse_events = queue.SimpleQueue()
        self._right_captured = False
        self._menu_open = False
        self._menu_hwnd = None
        self._dragged = False
        self._drag_threshold = (
            max(1, api.GetSystemMetrics(68)),
            max(1, api.GetSystemMetrics(69)),
        )  # SM_CX/YDRAG

        # Retain the callback until unhooked so ctypes cannot collect it.
        self._mouse_callback = self._hook_type(self._mouse_hook)
        self._mouse_hook_handle = None
        self._hook_thread_id = None
        self._hook_error = None
        self._hook_ready = threading.Event()
        self._hook_stop = threading.Event()
        self._hook_thread = threading.Thread(target=self._run_mouse_hook, daemon=True)
        self._hook_thread.start()
        if not self._hook_ready.wait(5):
            self._stop_mouse_hook()
            raise RuntimeError("Mouse hook thread did not start")
        if self._hook_error is not None:
            self._stop_mouse_hook()
            raise self._hook_error
        try:
            ctypes.set_last_error(0)
            style = api.GetWindowLongW(self._input_hwnd, -20)
            if not style and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.set_last_error(0)
            previous = api.SetWindowLongW(self._input_hwnd, -20, style | 0x80000 | 0x20)
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self._stop_mouse_hook()
            raise
        self.root.after(50, self._drain_mouse_events)

    def _run_mouse_hook(self) -> None:
        """Pump hook messages independently of Tk redraws and modal menus."""
        api = self._input_api
        try:
            enable_dpi_awareness()
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentThreadId.argtypes = []
            kernel.GetCurrentThreadId.restype = ctypes.c_ulong
            kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            kernel.GetModuleHandleW.restype = ctypes.c_void_p
            self._hook_thread_id = kernel.GetCurrentThreadId()
            message = wintypes.MSG()
            # Create the thread queue before publishing readiness or posting WM_QUIT.
            api.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._mouse_hook_handle = api.SetWindowsHookExW(
                14,
                self._mouse_callback,
                kernel.GetModuleHandleW(None),
                0,
            )
            if not self._mouse_hook_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hook_ready.set()
            while not self._hook_stop.is_set():
                result = api.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
        except Exception as exc:
            self._hook_error = exc
        finally:
            if self._mouse_hook_handle:
                api.UnhookWindowsHookEx(self._mouse_hook_handle)
                self._mouse_hook_handle = None
            self._hook_ready.set()

    def _stop_mouse_hook(self) -> None:
        self._hook_stop.set()
        if self._hook_thread_id is not None:
            self._input_api.PostThreadMessageW(self._hook_thread_id, 0x12, 0, 0)  # WM_QUIT
        self._hook_thread.join(timeout=2)

    def _mouse_hook(self, code: int, message: int, data: int) -> int:
        # No Tk calls or popup work in the hook: Windows requires a quick return.
        if code < 0:
            return self._input_api.CallNextHookEx(None, code, message, data)
        if message == 0x200 and not self._right_captured:
            return self._input_api.CallNextHookEx(None, code, message, data)
        if message in (0x200, 0x201, 0x204, 0x205, 0x207, 0x20B):
            point = ctypes.cast(data, ctypes.POINTER(_MouseHookData)).contents.pt
            x, y = point.x, point.y
            if self._menu_open:
                if message in (0x201, 0x204, 0x207, 0x20B):
                    self._dismiss_menu_outside(x, y)
                return self._input_api.CallNextHookEx(None, code, message, data)
            if message == 0x204:  # WM_RBUTTONDOWN
                if self._right_captured:
                    # A previous WM_RBUTTONUP was lost (e.g., released over an elevated window).
                    self._right_captured = False
                    self._mouse_events.put(("up", x, y))
                rect = _Rect()
                if (
                    self._input_api.IsWindowVisible(self._input_hwnd)
                    and self._input_api.GetWindowRect(self._input_hwnd, ctypes.byref(rect))
                    and rect.left <= x < rect.right
                    and rect.top <= y < rect.bottom
                ):
                    self._right_captured = True
                    self._mouse_events.put(("down", x, y))
                    return 1
            elif self._right_captured:
                if message == 0x205:  # WM_RBUTTONUP, even outside the widget
                    self._right_captured = False
                    self._mouse_events.put(("up", x, y))
                    return 1
                if message == 0x200:
                    key_state = self._input_api.GetAsyncKeyState(0x02)  # VK_RBUTTON
                    if isinstance(key_state, int) and not (key_state & 0x8000):
                        self._right_captured = False
                        self._mouse_events.put(("up", x, y))
                    else:
                        self._mouse_events.put(("move", x, y))
        return self._input_api.CallNextHookEx(None, code, message, data)

    def _dismiss_menu_outside(self, x: int, y: int) -> None:
        """Queue dismissal while leaving the outside click to its target app."""
        api = self._input_api
        hwnd = api.WindowFromPoint(wintypes.POINT(x, y))
        if self._menu_hwnd and api.GetAncestor(hwnd, 2) == self._menu_hwnd:
            return
        self._mouse_events.put(("dismiss", x, y))

    def _drain_mouse_events(self) -> None:
        if getattr(self, "_closing", False):
            return
        pending_move: tuple[int, int] | None = None
        while True:
            try:
                action, x, y = self._mouse_events.get_nowait()
            except queue.Empty:
                break
            if action == "move":
                if self.drag_origin is not None:
                    dx, dy = abs(x - self._right_start[0]), abs(y - self._right_start[1])
                    if dx >= self._drag_threshold[0] or dy >= self._drag_threshold[1]:
                        self._dragged = True
                    if self._dragged and not self.movement_locked.get():
                        pending_move = (x, y)
                continue
            if pending_move is not None:
                self._move_widget(*pending_move)
                pending_move = None
            if action == "dismiss":
                self.menu.close()
            elif action == "down":
                self._right_start = (x, y)
                self.drag_origin = (x - self.root.winfo_x(), y - self.root.winfo_y())
                self._dragged = False
            elif self.drag_origin is not None:
                dx, dy = abs(x - self._right_start[0]), abs(y - self._right_start[1])
                if dx >= self._drag_threshold[0] or dy >= self._drag_threshold[1]:
                    self._dragged = True
                if self._dragged and not self.movement_locked.get():
                    self._move_widget(x, y)
                if action == "up":
                    self.drag_origin = None
                    if not self._dragged:
                        self._show_menu(x, y)
        if pending_move is not None:
            self._move_widget(*pending_move)
        active = (
            self.drag_origin is not None
            or getattr(self, "_right_captured", False)
            or getattr(self, "_menu_open", False)
        )
        self.root.after(10 if active else 50, self._drain_mouse_events)

    def _move_widget(self, pointer_x: int, pointer_y: int) -> None:
        x = pointer_x - self.drag_origin[0]
        y = pointer_y - self.drag_origin[1]
        width = self.root.winfo_width()
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        # '+-N' specifies an absolute negative coordinate in Tk geometry syntax.
        self.root.geometry(f"+{x}+{y}")

    def _show_menu(self, x: int, y: int) -> None:
        self._update_connection_status()
        self._on_menu_opening()
        self.menu.show(x, y)
        self._menu_hwnd = self._input_api.GetAncestor(self.menu.window.winfo_id(), 2)
        self._menu_open = True

    def _on_menu_opening(self) -> None:
        pass

    def _menu_closed(self) -> None:
        self._menu_open = False
        self._menu_hwnd = None
        self._ensure_topmost()

    def _release_mouse_hook(self, event: tk.Event) -> None:
        if event.widget is self.root and hasattr(self, "_hook_thread"):
            self._stop_mouse_hook()

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", self.always_on_top.get())
        self._ensure_hover_timer()

    def _ensure_topmost(self) -> None:
        """Restore native WS_EX_TOPMOST if enabled and unexpectedly cleared."""
        if self._closing or not self.always_on_top.get():
            return
        api = getattr(self, "_input_api", None)
        hwnd = getattr(self, "_input_hwnd", None)
        if api is None or not hwnd:
            return
        try:
            style = api.GetWindowLongW(hwnd, -20)
            if style and not (style & 0x8):
                self.root.attributes("-topmost", True)
                self.root.lift()
        except (OSError, tk.TclError):
            pass

    def _set_transparency(self, value: int) -> None:
        self._apply_transparency(value)
        self._schedule_settings_save()

    def _apply_transparency(self, value: int) -> None:
        alpha = 1.0 - max(0, min(100, value)) / 100
        if self._hovered and self.hover_transparency_enabled.get():
            alpha = min(alpha, 0.2)
        self.root.attributes("-alpha", alpha)

    def _toggle_hover_transparency(self) -> None:
        self._apply_transparency(self.transparency.get())
        self._schedule_settings_save()
        self._ensure_hover_timer()

    def _needs_hover_polling(self) -> bool:
        return bool(self.hover_transparency_enabled.get() or self.always_on_top.get())

    def _ensure_hover_timer(self) -> None:
        if getattr(self, "_closing", False):
            return
        if self._needs_hover_polling():
            if getattr(self, "_hover_after_id", None) is None:
                self._update_hover()
        else:
            self._cancel_timer("_hover_after_id")
            if self._hovered:
                self._hovered = False
                self._apply_transparency(self.transparency.get())

    def _update_hover(self) -> None:
        """Poll coordinates because the click-through window receives no enter events."""
        self._hover_after_id = None
        if self._closing:
            return
        if not self._needs_hover_polling():
            if self._hovered:
                self._hovered = False
                self._apply_transparency(self.transparency.get())
            return
        x, y = self.root.winfo_pointerxy()
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        hovered = (
            left <= x < left + self.root.winfo_width()
            and top <= y < top + self.root.winfo_height()
        )
        if hovered != self._hovered:
            self._hovered = hovered
            self._apply_transparency(self.transparency.get())
            if hovered and not self._menu_open and self.always_on_top.get():
                self.root.attributes("-topmost", True)
        self._hover_after_id = self.root.after(80, self._update_hover)

    def _toggle_autostart(self) -> None:
        try:
            set_autostart_for(self.RUN_VALUE, self._script_path, self.autostart.get())
        except OSError as exc:
            self.autostart.set(autostart_enabled_for(self.RUN_VALUE, self._script_path))
            messagebox.showerror(
                self.APP_TITLE,
                f"Windows 시작 시 자동 실행 설정을 변경하지 못했습니다.\n{exc}",
                parent=self.root,
            )
            self._ensure_topmost()

    def _draw_segments(self, canvas: tk.Canvas, used_percent: int | None) -> None:
        canvas.delete("all")
        count = 5
        segment_width = 3
        gap = 2
        active = 0 if used_percent is None else round(count * used_percent / 100)
        color = MUTED if used_percent is None else usage_color(used_percent)
        for index in range(count):
            x = index * (segment_width + gap)
            fill = color if index < active else TRACK
            canvas.create_rectangle(x, 2, x + segment_width, 8, fill=fill, outline="")

    def _recovery_refresh(self) -> None:
        self.recovery_after_id = None
        self.refresh()

    def _update_clock(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._ensure_topmost()
        if self.snapshot:
            self._update_time_labels()
        self._render_connection_issue()
        self.root.after(60_000, self._update_clock)

    def _scheduled_refresh(self) -> None:
        self._scheduled_after_id = None
        if getattr(self, "_closing", False):
            return
        if not self._can_start_refresh():
            self._scheduled_after_id = self.root.after(60_000, self._scheduled_refresh)
            return
        self.refresh()

    def run(self) -> None:
        self.root.mainloop()
