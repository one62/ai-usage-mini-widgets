"""Combined single-window switching widget for Codex and Gemini usage."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import Label, messagebox

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
_CODEX_DIR = _REPO_ROOT / "codex-usage-widget"
_GEMINI_DIR = _REPO_ROOT / "gemini-usage-widget"
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR), str(_CODEX_DIR), str(_GEMINI_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import codex_settings
import codex_usage
import gemini_usage
from ai_settings import (
    PROVIDER_KEYS,
    ROTATE_INTERVALS,
    load_settings,
    save_settings,
)
from common.widget_base import (
    MUTED,
    TEXT,
    WINDOW_WIDTH,
    YELLOW,
    BaseUsageWidget,
    PopupMenu,
    recovery_delay,
    reset_short,
    run_single_instance,
)


RUN_VALUE = "AIUsageWidget"
PROVIDER_ORDER = PROVIDER_KEYS
PROVIDER_LABELS = {"codex": "Codex", "gemini": "Gemini"}


def _resolve_logo_path(filename: str, source_subdir: str) -> Path:
    bundled = Path(__file__).resolve().parent / "assets" / filename
    if bundled.exists():
        return bundled
    return _REPO_ROOT / source_subdir / "assets" / filename


@dataclass
class _ProviderRuntime:
    key: str
    snapshot: object | None = None
    refreshing: bool = False
    fetch_generation: int = 0
    failure_count: int = 0
    connection_failed: bool = False
    last_error: str = ""
    auth_required: bool = False
    scheduled_after_id: str | None = None
    recovery_after_id: str | None = None
    watchdog_after_id: str | None = None


class UsageWidget(BaseUsageWidget):
    APP_TITLE = "AI Usage"
    RUN_VALUE = RUN_VALUE
    MIDDLE_CLICK_ENABLED = True

    def __init__(self) -> None:
        self._init_base(script_path=Path(__file__).resolve().with_name("widget.py"))
        self.active_provider: str = self.settings.active_provider
        self.rotate_interval_seconds: int = self.settings.rotate_interval_seconds
        self.rotate_step = tk.IntVar(
            master=self.root,
            value=ROTATE_INTERVALS.index(self.rotate_interval_seconds),
        )
        self._rotate_after_id: str | None = None
        self.providers: dict[str, _ProviderRuntime] = {
            key: _ProviderRuntime(key=key) for key in PROVIDER_ORDER
        }
        self.provider_results: queue.Queue[tuple[str, int, str, object]] = queue.Queue()
        self.resetting = False
        self.reset_state_unreadable = False
        try:
            self.pending_reset = codex_settings.load_pending_reset()
        except (OSError, ValueError, RecursionError):
            self.pending_reset = None
            self.reset_state_unreadable = True
        self.reset_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._reset_drain_after_id: str | None = None
        self._full_width = WINDOW_WIDTH
        self._build()
        self._finalize_init()

    @property
    def menu(self) -> PopupMenu:
        return self.menus[self.active_provider]

    @property
    def connection_status_index(self) -> int:
        return self._menu_indices[self.active_provider]["connection_status"]

    @staticmethod
    def _settings_tuple(settings) -> tuple[int, int | None, int | None, bool, bool, str, int]:
        return (
            settings.transparency,
            settings.x,
            settings.y,
            settings.movement_locked,
            settings.hover_transparency_enabled,
            settings.active_provider,
            settings.rotate_interval_seconds,
        )

    def _load_widget_settings(self):
        return load_settings()

    def _save_widget_settings(self, settings) -> None:
        save_settings(settings)

    def _save_preferences(self) -> None:
        if getattr(self, "rotate_step", None) is not None:
            step = max(0, min(len(ROTATE_INTERVALS) - 1, self.rotate_step.get()))
            self.rotate_interval_seconds = ROTATE_INTERVALS[step]
        self.settings.active_provider = self.active_provider
        self.settings.rotate_interval_seconds = self.rotate_interval_seconds
        super()._save_preferences()

    def _sync_active_aliases(self) -> None:
        rt = self.providers[self.active_provider]
        self.snapshot = rt.snapshot
        self.refreshing = rt.refreshing
        self.failure_count = rt.failure_count
        self.connection_failed = rt.connection_failed
        self.last_error = rt.last_error
        self.auth_required = rt.auth_required

    def _build(self) -> None:
        logo_paths = {
            "codex": _resolve_logo_path("codex-logo.png", "codex-usage-widget"),
            "gemini": _resolve_logo_path("gemini-logo.png", "gemini-usage-widget"),
        }
        initial_logo = logo_paths[self.active_provider]
        self._build_header_shell(
            logo_path=initial_logo,
            five_fg=TEXT,
            week_fg=TEXT,
        )

        self._logos: dict[str, tk.PhotoImage] = {}
        if getattr(self, "_logo_image", None) is not None:
            self._logos[self.active_provider] = self._logo_image
        for key, path in logo_paths.items():
            if key in self._logos:
                continue
            try:
                self._logos[key] = tk.PhotoImage(master=self.root, file=str(path))
            except tk.TclError:
                pass

        self.reset_status = Label(
            self.header, bg=self.header.cget("bg"), fg=TEXT, font=self.five_title.cget("font")
        )

        self.menus: dict[str, PopupMenu] = {}
        self._menu_indices: dict[str, dict[str, int]] = {}
        for key in PROVIDER_ORDER:
            other_key = self._other_provider(key)
            menu = PopupMenu(self.root, self._menu_closed, reserve_indicator_space=True)
            indices: dict[str, int] = {}

            menu.add_command(
                label=f"전환: {PROVIDER_LABELS[other_key]} 보기",
                command=lambda target=other_key: self.switch_provider(target, manual=True),
            )
            menu.add_command(label="지금 새로고침", command=self.refresh)
            indices["refresh"] = menu.index("end")
            menu.add_command(
                label="연결 상태: 확인 전",
                command=self._on_connection_status_click,
                state="disabled",
            )
            indices["connection_status"] = menu.index("end")
            menu.add_separator()

            menu.add_info(label="계정: —")
            indices["account"] = menu.index("end")
            if key == "codex":
                menu.add_info(label="요금제: —", right_text="크레딧: —")
                indices["plan"] = menu.index("end")
                menu.add_info(label="초기화권: —", right_text="")
                indices["reset_count"] = menu.index("end")
                menu.add_command(
                    label="초기화권 사용", state="disabled", command=self._use_reset_credit
                )
                indices["reset_use"] = menu.index("end")
            else:
                menu.add_info(label="요금제: —", right_text="")
                indices["plan"] = menu.index("end")
                menu.add_info(label="Claude·GPT 5시간: —", right_text="")
                indices["claude_5h"] = menu.index("end")
                menu.add_info(label="Claude·GPT 주간: —", right_text="")
                indices["claude_7d"] = menu.index("end")

            menu.add_separator()
            menu.add_scale(
                label="자동 전환",
                variable=self.rotate_step,
                command=self._set_rotate_step,
                from_=0,
                to=len(ROTATE_INTERVALS) - 1,
                resolution=1,
                format_value=self._format_rotate_step,
                max_value_text="30초",
            )
            indices["rotate_scale"] = menu.index("end")

            menu.add_separator()
            self._add_common_preference_menu_items(menu)
            menu.add_separator()
            if key == "codex":
                menu.add_command(label="ChatGPT 데스크톱 열기", command=self._open_chatgpt)
            else:
                menu.add_command(label="Antigravity 열기", command=self._open_antigravity)
            menu.add_command(label="종료", command=self._exit_by_user)

            self.menus[key] = menu
            self._menu_indices[key] = indices

        self._fit_content_width()
        self._full_width = max(WINDOW_WIDTH, self.header.winfo_reqwidth() + 16)
        self._render_active_provider()

    @staticmethod
    def _other_provider(key: str) -> str:
        return "gemini" if key == "codex" else "codex"

    def _start_extra_timers(self) -> None:
        self._restart_rotate_timer()

    def _can_start_provider_refresh(self, key: str) -> bool:
        rt = self.providers[key]
        if rt.refreshing:
            return False
        if key == "codex" and self.resetting:
            return False
        return True

    def _query_provider_usage(self, key: str, timeout: float = 18.0):
        if key == "codex":
            return codex_usage.query_usage(timeout=timeout)
        return gemini_usage.query_usage(timeout=timeout)

    @staticmethod
    def _is_provider_auth_error(key: str, exc: Exception) -> bool:
        if key == "codex":
            return isinstance(exc, codex_usage.AuthRequiredError)
        return isinstance(exc, gemini_usage.AuthRequiredError)

    def _open_app(self) -> None:
        if self.active_provider == "codex":
            self._open_chatgpt()
        else:
            self._open_antigravity()

    def _open_chatgpt(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = codex_usage.find_codex_executables()
            if not executables:
                raise OSError("Codex executable not found")
            subprocess.Popen([executables[0], "app"], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://chatgpt.com/")

    def _open_antigravity(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = gemini_usage.find_antigravity_executables()
            if not executables:
                raise OSError("Antigravity executable not found")
            subprocess.Popen([executables[0]], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://gemini.google.com/")

    def refresh(self) -> None:
        for key in PROVIDER_ORDER:
            self._refresh_provider(key)

    def _cancel_provider_timer(self, rt: _ProviderRuntime, attr_name: str) -> None:
        timer_id = getattr(rt, attr_name, None)
        if timer_id is not None:
            setattr(rt, attr_name, None)
            try:
                self.root.after_cancel(timer_id)
            except tk.TclError:
                pass

    def _refresh_provider(self, key: str) -> None:
        if not self._can_start_provider_refresh(key):
            return
        rt = self.providers[key]
        self._cancel_provider_timer(rt, "scheduled_after_id")
        self._cancel_provider_timer(rt, "recovery_after_id")
        self._cancel_provider_timer(rt, "watchdog_after_id")
        rt.refreshing = True
        rt.fetch_generation += 1
        generation = rt.fetch_generation
        self._update_refresh_menu_state(key)
        self._update_provider_connection_status(key)
        if key == "codex":
            self._update_reset_menu()
        if key == self.active_provider:
            self._sync_active_aliases()
            if rt.snapshot is None:
                self.reset_compact.configure(text="…", fg=MUTED)
        threading.Thread(target=self._fetch_provider_worker, args=(key, generation), daemon=True).start()
        rt.watchdog_after_id = self.root.after(
            50_000, lambda k=key, g=generation: self._fetch_provider_watchdog(k, g)
        )
        self._ensure_drain_results()

    def _fetch_provider_worker(self, key: str, generation: int) -> None:
        last_error: Exception | None = None
        for delay in (0.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                snapshot = self._query_provider_usage(key, timeout=18.0)
                self.provider_results.put((key, generation, "ok", snapshot))
                return
            except Exception as exc:
                if self._is_provider_auth_error(key, exc):
                    self.provider_results.put((key, generation, "error", exc))
                    return
                last_error = exc
        self.provider_results.put(
            (key, generation, "error", last_error or RuntimeError("알 수 없는 오류"))
        )

    def _fetch_provider_watchdog(self, key: str, generation: int) -> None:
        rt = self.providers[key]
        rt.watchdog_after_id = None
        if not rt.refreshing or generation != rt.fetch_generation:
            return
        rt.fetch_generation += 1
        rt.refreshing = False
        self._update_refresh_menu_state(key)
        self._handle_provider_error(key, "사용량 조회가 50초를 초과했습니다.")

    def _drain_results(self) -> None:
        self._drain_results_after_id = None
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                try:
                    key, generation, kind, payload = self.provider_results.get_nowait()
                except queue.Empty:
                    break
                rt = self.providers.get(key)
                if rt is None or generation != rt.fetch_generation:
                    continue
                self._cancel_provider_timer(rt, "watchdog_after_id")
                rt.refreshing = False
                self._update_refresh_menu_state(key)
                if kind == "ok" and payload is not None:
                    self._cancel_provider_timer(rt, "recovery_after_id")
                    self._cancel_provider_timer(rt, "scheduled_after_id")
                    rt.connection_failed = False
                    rt.last_error = ""
                    rt.auth_required = False
                    rt.failure_count = 0
                    rt.snapshot = payload
                    self._update_provider_connection_status(key)
                    self._update_provider_menu(key, payload)
                    if key == self.active_provider:
                        self._sync_active_aliases()
                        self._render_active_provider()
                    rt.scheduled_after_id = self.root.after(
                        60_000, lambda k=key: self._scheduled_provider_refresh(k)
                    )
                else:
                    auth_required = isinstance(payload, Exception) and self._is_provider_auth_error(
                        key, payload
                    )
                    message = str(payload) if payload else "알 수 없는 오류"
                    self._handle_provider_error(key, message, auth_required=auth_required)
        except Exception as exc:
            for key, rt in self.providers.items():
                if rt.refreshing:
                    rt.refreshing = False
                    self._update_refresh_menu_state(key)
            self._handle_provider_error(self.active_provider, str(exc) or "렌더링 오류")
        finally:
            if (
                any(rt.refreshing for rt in self.providers.values())
                and not getattr(self, "_closing", False)
            ):
                self._drain_results_after_id = self.root.after(100, self._drain_results)

    def _handle_provider_error(
        self, key: str, message: str = "", *, auth_required: bool = False
    ) -> None:
        rt = self.providers[key]
        rt.connection_failed = True
        rt.last_error = message or "connection failed"
        rt.auth_required = auth_required
        self._cancel_provider_timer(rt, "scheduled_after_id")
        if auth_required:
            self._cancel_provider_timer(rt, "recovery_after_id")
        rt.failure_count += 1
        self._update_provider_connection_status(key)
        if key == "codex":
            self._update_reset_menu()
        if key == self.active_provider:
            self._sync_active_aliases()
            self._render_provider_connection_issue(key)
        if auth_required:
            rt.scheduled_after_id = self.root.after(
                60_000, lambda k=key: self._scheduled_provider_refresh(k)
            )
        elif rt.recovery_after_id is None:
            delay = recovery_delay(rt.failure_count)
            rt.recovery_after_id = self.root.after(
                delay * 1000, lambda k=key: self._recovery_provider_refresh(k)
            )

    def _scheduled_provider_refresh(self, key: str) -> None:
        rt = self.providers[key]
        rt.scheduled_after_id = None
        if getattr(self, "_closing", False):
            return
        if not self._can_start_provider_refresh(key):
            rt.scheduled_after_id = self.root.after(
                60_000, lambda k=key: self._scheduled_provider_refresh(k)
            )
            return
        self._refresh_provider(key)

    def _recovery_provider_refresh(self, key: str) -> None:
        rt = self.providers[key]
        rt.recovery_after_id = None
        if getattr(self, "_closing", False):
            return
        if not self._can_start_provider_refresh(key):
            rt.recovery_after_id = self.root.after(
                1_000, lambda k=key: self._recovery_provider_refresh(k)
            )
            return
        self._refresh_provider(key)

    def _update_refresh_menu_state(self, key: str) -> None:
        rt = self.providers[key]
        disabled = rt.refreshing or (key == "codex" and self.resetting)
        self.menus[key].entryconfigure(
            self._menu_indices[key]["refresh"],
            state="disabled" if disabled else "normal",
        )

    def _update_connection_status(self) -> None:
        self._update_provider_connection_status(self.active_provider)

    def _update_provider_connection_status(self, key: str) -> None:
        rt = self.providers[key]
        clickable = False
        if rt.refreshing:
            status = "조회 중"
        elif rt.auth_required:
            status = "로그인 필요"
            clickable = True
        elif rt.connection_failed or rt.last_error:
            status = "재연결 중"
            clickable = True
        elif rt.snapshot is not None:
            status = "정상"
        else:
            status = "확인 전"
        self.menus[key].entryconfigure(
            self._menu_indices[key]["connection_status"],
            label=f"연결 상태: {status}",
            state="normal" if clickable else "disabled",
        )

    def _render_connection_issue(self) -> None:
        self._render_provider_connection_issue(self.active_provider)

    def _render_provider_connection_issue(self, key: str) -> None:
        if key != self.active_provider:
            return
        rt = self.providers[key]
        if not rt.connection_failed and not rt.last_error:
            return
        label = self._connection_issue_label()
        if rt.snapshot is not None and rt.failure_count < 3 and not rt.auth_required:
            label.configure(text="retry", fg=YELLOW)
        else:
            label.configure(text="offline", fg=MUTED)

    def _connection_issue_label(self) -> Label:
        if self.active_provider == "codex" and not self.five_section.winfo_manager():
            return self.week_reset
        return self.reset_compact

    def _set_five_hour_visible(self, visible: bool) -> None:
        if visible == bool(self.five_section.winfo_manager()):
            return
        anchor = getattr(self, "week_title", None)
        if visible and anchor is not None:
            self.five_section.pack(side="left", before=anchor.master)
        else:
            self.five_section.pack_forget()
        self._fit_content_width(min_width=getattr(self, "_full_width", WINDOW_WIDTH))

    def _render_active_provider(self) -> None:
        key = self.active_provider
        self._sync_active_aliases()
        logo_label = getattr(self, "logo_label", None)
        if logo_label is not None and key in getattr(self, "_logos", {}):
            logo_label.configure(image=self._logos[key])

        rt = self.providers[key]
        if key == "codex":
            if self.resetting and hasattr(self, "reset_status"):
                self.reset_status.place(x=0, y=0, relwidth=1, relheight=1)
                self.reset_status.lift()
            self._render_codex_header(rt.snapshot)
        else:
            if hasattr(self, "reset_status"):
                self.reset_status.place_forget()
            self._render_gemini_header(rt.snapshot)

        if rt.connection_failed or rt.last_error:
            self._render_provider_connection_issue(key)
        elif rt.snapshot is None and rt.refreshing:
            self.reset_compact.configure(text="…", fg=MUTED)

    def _render_codex_header(self, snapshot) -> None:
        if snapshot is None:
            self._set_five_hour_visible(True)
            self._render_metric_window(
                title_label=self.five_title,
                value_label=self.five_label,
                reset_label=self.reset_compact,
                meter=self.five_meter,
                used_percent=None,
                resets_at=None,
            )
            self._render_metric_window(
                title_label=self.week_title,
                value_label=self.week_label,
                reset_label=self.week_reset,
                meter=self.week_meter,
                used_percent=None,
                resets_at=None,
                weekly=True,
            )
            return

        five_hour, weekly = snapshot.metric_windows
        five_hour_visible = snapshot.has_five_hour_limit
        if five_hour_visible is not None:
            self._set_five_hour_visible(five_hour_visible)
        self._render_metric_window(
            title_label=self.five_title,
            value_label=self.five_label,
            reset_label=self.reset_compact,
            meter=self.five_meter,
            used_percent=five_hour.used_percent if five_hour else None,
            resets_at=five_hour.resets_at if five_hour else None,
        )
        self._render_metric_window(
            title_label=self.week_title,
            value_label=self.week_label,
            reset_label=self.week_reset,
            meter=self.week_meter,
            used_percent=weekly.used_percent if weekly else None,
            resets_at=weekly.resets_at if weekly else None,
            weekly=True,
        )

    def _render_gemini_header(self, snapshot) -> None:
        self._set_five_hour_visible(True)
        five = snapshot.five_hour if snapshot is not None else None
        week = snapshot.seven_day if snapshot is not None else None
        self._render_metric_window(
            title_label=self.five_title,
            value_label=self.five_label,
            reset_label=self.reset_compact,
            meter=self.five_meter,
            used_percent=five.used_percent if five else None,
            resets_at=five.resets_at if five else None,
        )
        self._render_metric_window(
            title_label=self.week_title,
            value_label=self.week_label,
            reset_label=self.week_reset,
            meter=self.week_meter,
            used_percent=week.used_percent if week else None,
            resets_at=week.resets_at if week else None,
            weekly=True,
        )

    def _update_provider_menu(self, key: str, snapshot) -> None:
        menu = self.menus[key]
        idx = self._menu_indices[key]
        if key == "codex":
            menu.entryconfigure(idx["account"], label=f"계정: {snapshot.account or '—'}")
            plan_text = snapshot.plan if snapshot.plan.lower() != "unknown" else "—"
            if snapshot.unlimited_credits:
                credit_text = "무제한"
            elif snapshot.credit_balance is not None and snapshot.credit_balance.is_finite():
                try:
                    credit_text = f"{snapshot.credit_balance.quantize(Decimal('0.01'))}"
                except InvalidOperation:
                    credit_text = str(snapshot.credit_balance)
            else:
                credit_text = "—"
            menu.entryconfigure(
                idx["plan"],
                label=f"요금제: {plan_text}",
                right_text=f"크레딧: {credit_text}",
            )
            self._update_reset_menu()
        else:
            account_text = snapshot.account_email or "—"
            menu.entryconfigure(idx["account"], label=f"계정: {account_text}")
            credit_text = (
                f"크레딧 {snapshot.credit_balance}" if snapshot.credit_balance is not None else ""
            )
            menu.entryconfigure(
                idx["plan"],
                label=f"요금제: {snapshot.plan}",
                right_text=credit_text,
            )
            c5 = snapshot.claude_gpt_five_hour
            if c5 is not None:
                menu.entryconfigure(
                    idx["claude_5h"],
                    label=f"Claude·GPT 5시간: {c5.used_percent}%",
                    right_text=reset_short(c5.resets_at) if c5.resets_at is not None else "",
                )
            else:
                menu.entryconfigure(
                    idx["claude_5h"],
                    label="Claude·GPT 5시간: —",
                    right_text="",
                )
            c7 = snapshot.claude_gpt_seven_day
            if c7 is not None:
                menu.entryconfigure(
                    idx["claude_7d"],
                    label=f"Claude·GPT 주간: {c7.used_percent}%",
                    right_text=reset_short(c7.resets_at, weekly=True)
                    if c7.resets_at is not None
                    else "",
                )
            else:
                menu.entryconfigure(
                    idx["claude_7d"],
                    label="Claude·GPT 주간: —",
                    right_text="",
                )

    def _update_gemini_claude_reset_labels(self) -> None:
        snapshot = self.providers["gemini"].snapshot
        if snapshot is None:
            return
        menu = self.menus["gemini"]
        idx = self._menu_indices["gemini"]
        c5 = snapshot.claude_gpt_five_hour
        if c5 is not None:
            menu.entryconfigure(
                idx["claude_5h"],
                right_text=reset_short(c5.resets_at) if c5.resets_at is not None else "",
            )
        c7 = snapshot.claude_gpt_seven_day
        if c7 is not None:
            menu.entryconfigure(
                idx["claude_7d"],
                right_text=reset_short(c7.resets_at, weekly=True)
                if c7.resets_at is not None
                else "",
            )

    def _update_time_labels(self) -> None:
        rt = self.providers[self.active_provider]
        if rt.snapshot is not None:
            if self.active_provider == "codex":
                five_hour, weekly = rt.snapshot.metric_windows
                self.reset_compact.configure(
                    text=reset_short(five_hour.resets_at if five_hour else None),
                    fg=MUTED,
                )
                self.week_reset.configure(
                    text=reset_short(weekly.resets_at if weekly else None, weekly=True),
                    fg=MUTED,
                )
            else:
                five = rt.snapshot.five_hour
                week = rt.snapshot.seven_day
                self.reset_compact.configure(
                    text=reset_short(five.resets_at) if five else "—",
                    fg=MUTED,
                )
                self.week_reset.configure(
                    text=reset_short(week.resets_at if week else None, weekly=True),
                    fg=MUTED,
                )
        if self.active_provider == "gemini" and getattr(self.menus["gemini"], "open", False):
            self._update_gemini_claude_reset_labels()

    def _update_clock(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._ensure_topmost()
        self._update_time_labels()
        self._render_provider_connection_issue(self.active_provider)
        self.root.after(60_000, self._update_clock)

    def _on_menu_opening(self) -> None:
        if self.active_provider == "gemini":
            self._update_gemini_claude_reset_labels()

    def _show_menu(self, x: int, y: int) -> None:
        self._cancel_timer("_rotate_after_id")
        super()._show_menu(x, y)

    def _menu_closed(self) -> None:
        super()._menu_closed()
        self._restart_rotate_timer()

    def _on_middle_click(self) -> None:
        if getattr(self, "_closing", False) or getattr(self, "_menu_open", False):
            return
        self.switch_provider(manual=True)

    def switch_provider(self, target: str | None = None, *, manual: bool = False) -> None:
        next_key = target if target in PROVIDER_ORDER else self._other_provider(self.active_provider)
        for menu in self.menus.values():
            if getattr(menu, "open", False):
                menu.close()
        if next_key != self.active_provider:
            self.active_provider = next_key
            self._render_active_provider()
        if manual:
            self._schedule_settings_save()
            self._restart_rotate_timer()

    def _can_auto_rotate(self) -> bool:
        return (
            not getattr(self, "_closing", False)
            and self.rotate_interval_seconds > 0
            and not getattr(self, "_menu_open", False)
            and not getattr(self, "resetting", False)
        )

    def _restart_rotate_timer(self) -> None:
        self._cancel_timer("_rotate_after_id")
        if self._can_auto_rotate():
            self._rotate_after_id = self.root.after(
                self.rotate_interval_seconds * 1000,
                self._auto_rotate_tick,
            )

    def _auto_rotate_tick(self) -> None:
        self._rotate_after_id = None
        if not self._can_auto_rotate():
            return
        self.switch_provider(manual=False)
        self._restart_rotate_timer()

    @staticmethod
    def _format_rotate_step(step: int) -> str:
        clamped = max(0, min(len(ROTATE_INTERVALS) - 1, step))
        seconds = ROTATE_INTERVALS[clamped]
        return "끄기" if seconds == 0 else f"{seconds}초"

    def _set_rotate_step(self, step: int) -> None:
        clamped = max(0, min(len(ROTATE_INTERVALS) - 1, step))
        self.rotate_interval_seconds = ROTATE_INTERVALS[clamped]
        self._restart_rotate_timer()
        self._schedule_settings_save()

    # Codex reset-credit management
    def _update_reset_menu(self) -> None:
        rt = self.providers["codex"]
        snapshot = rt.snapshot
        menu = self.menus["codex"]
        idx = self._menu_indices["codex"]
        count = snapshot.reset_credit_count if snapshot else None
        expiry = self._reset_expiry_label(snapshot) if count else ""
        menu.entryconfigure(
            idx["reset_count"],
            label=f"초기화권 {count}개 사용 가능" if count is not None else "초기화권: 조회 불가",
            right_text=expiry,
        )
        retry = self.pending_reset is not None
        can_start = (
            not rt.connection_failed
            and snapshot is not None
            and snapshot.reset_credit_id is not None
            and snapshot.executable is not None
        )
        enabled = (
            not self.resetting
            and not rt.refreshing
            and not self.reset_state_unreadable
            and (retry or can_start)
        )
        label = "사용 결과 확인" if retry else "초기화권 사용"
        if self.resetting:
            label = self.reset_status.cget("text")
        elif self.reset_state_unreadable:
            label = "초기화권 사용 불가 (요청 기록 확인 필요)"
        elif count and not retry and not can_start and not rt.connection_failed:
            label = "초기화권 사용 불가 (대상 확인 필요)"
        menu.entryconfigure(
            idx["reset_use"],
            label=label,
            state="normal" if enabled else "disabled",
        )

    @staticmethod
    def _reset_expiry_label(snapshot) -> str:
        expires_at = snapshot.reset_credit_expires_at
        if expires_at is None and snapshot.reset_credit_id is not None:
            return "만료일 없음"
        if expires_at is not None:
            try:
                return f"{datetime.fromtimestamp(expires_at):%Y.%m.%d.} 만료"
            except (OverflowError, OSError, ValueError):
                pass
        return "만료일 조회 불가"

    def _use_reset_credit(self) -> None:
        rt = self.providers["codex"]
        if self.resetting or rt.refreshing or self.reset_state_unreadable:
            return
        request = self.pending_reset
        snapshot = rt.snapshot
        retry = request is not None
        if retry:
            prompt = "이전 요청에서 초기화권이 사용됐는지 확인할까요?\n초기화권을 추가로 사용하지 않습니다."
        else:
            if (
                rt.connection_failed
                or snapshot is None
                or not snapshot.reset_credit_id
                or not snapshot.executable
            ):
                return
            request = codex_settings.PendingReset(
                str(uuid.uuid4()), snapshot.executable, snapshot.reset_credit_id, snapshot.account_id
            )
            expiry = self._reset_expiry_label(snapshot)
            prompt = (
                "초기화권 1개를 사용해 Codex 사용량을 초기화할까요?\n"
                "이 초기화권이 적용되는 사용 한도만 초기화됩니다.\n\n"
                f"사용할 초기화권 · {expiry}"
            )
            if snapshot.reset_credit_details_limited:
                prompt += "\n확인할 수 있는 초기화권 중에서 선택했습니다."
        title = "사용 결과 확인" if retry else "사용량 초기화"
        if not messagebox.askyesno(title, prompt, parent=self.root):
            self._ensure_topmost()
            return
        self._ensure_topmost()
        if self._closing:
            return
        if self.resetting or rt.refreshing:
            messagebox.showinfo("사용량 초기화", "조회가 끝난 뒤 다시 선택해 주세요.", parent=self.root)
            self._ensure_topmost()
            return
        if not retry and (
            rt.connection_failed or rt.snapshot is not snapshot or self.pending_reset is not None
        ):
            return
        if not retry:
            try:
                codex_settings.save_pending_reset(request)
            except OSError:
                messagebox.showwarning(
                    "사용량 초기화",
                    "요청을 저장하지 못해 초기화를 시작할 수 없습니다. 잠시 후 다시 시도하세요.",
                    parent=self.root,
                )
                self._ensure_topmost()
                return
            self.pending_reset = request
        elif self.pending_reset != request:
            return
        self.resetting = True
        self._cancel_timer("_rotate_after_id")
        self.reset_status.configure(text="사용 결과 확인 중…" if retry else "사용량 초기화 중…")
        if self.active_provider == "codex":
            self.reset_status.place(x=0, y=0, relwidth=1, relheight=1)
            self.reset_status.lift()
        self._update_refresh_menu_state("codex")
        self._update_reset_menu()
        threading.Thread(target=self._reset_worker, args=(request,), daemon=True).start()
        self._ensure_drain_reset_results()

    def _ensure_drain_reset_results(self) -> None:
        if getattr(self, "_reset_drain_after_id", None) is None and not getattr(self, "_closing", False):
            self._reset_drain_after_id = self.root.after(100, self._drain_reset_results)

    def _reset_worker(self, request: codex_settings.PendingReset) -> None:
        try:
            outcome = codex_usage.consume_reset_credit(
                request.key, request.executable, request.credit_id, account_id=request.account_id
            )
            self.reset_results.put(("ok", outcome))
        except codex_usage.UsageError as exc:
            self.reset_results.put(("error", exc))
        except Exception:
            self.reset_results.put(("error", ""))

    def _drain_reset_results(self) -> None:
        self._reset_drain_after_id = None
        if getattr(self, "_closing", False):
            return
        try:
            kind, payload = self.reset_results.get_nowait()
        except queue.Empty:
            if self.resetting:
                self._reset_drain_after_id = self.root.after(100, self._drain_reset_results)
            return
        self.resetting = False
        self.reset_status.place_forget()
        self._update_refresh_menu_state("codex")
        self._restart_rotate_timer()
        if kind == "ok":
            try:
                codex_settings.save_pending_reset(None)
            except OSError:
                pass
            else:
                self.pending_reset = None
            messages = {
                "reset": "초기화권 1개를 사용했습니다. 사용량에 반영되기까지 잠시 걸릴 수 있습니다.",
                "alreadyRedeemed": "앞선 요청에서 이미 초기화권을 사용했습니다.",
                "nothingToReset": "지금은 사용량을 초기화할 필요가 없습니다. 초기화권은 다음에 사용할 수 있습니다.",
                "noCredit": "사용할 수 있는 초기화권이 없습니다.",
            }
            self._refresh_provider("codex")
            messagebox.showinfo(
                "사용량 초기화",
                messages.get(str(payload), "초기화권 사용 요청을 처리했습니다."),
                parent=self.root,
            )
            self._ensure_topmost()
        else:
            self._update_reset_menu()
            if isinstance(payload, codex_usage.ResetAccountUnverifiedError):
                self._offer_archive_pending_reset(str(payload))
            else:
                reason = "초기화권 사용 여부를 확인하지 못했습니다."
                if isinstance(payload, codex_usage.UsageTimeoutError):
                    reason = "응답이 늦어 초기화권 사용 여부를 확인하지 못했습니다."
                elif isinstance(payload, codex_usage.UsageError):
                    reason = str(payload) or reason
                messagebox.showwarning(
                    "사용 결과 확인",
                    f"{reason}\n\n메뉴에서 ‘사용 결과 확인’을 선택하세요.",
                    parent=self.root,
                )
            self._ensure_topmost()

    def _offer_archive_pending_reset(self, detail: str) -> None:
        request = self.pending_reset
        if request is None or not messagebox.askyesno(
            "초기화권 요청 기록 보관",
            f"{detail}\n\n원래 Codex 계정에서 처리 결과를 직접 확인한 경우, "
            "이전 기록을 보관하고 새 요청을 허용하시겠습니까?",
            parent=self.root,
        ):
            return
        if self._closing or self.resetting or self.pending_reset != request:
            return
        try:
            codex_settings.archive_pending_reset(request)
        except (OSError, ValueError, RecursionError):
            messagebox.showwarning("사용량 초기화", "요청 기록을 보관하지 못했습니다.", parent=self.root)
        else:
            self.pending_reset = None
            self._update_reset_menu()
            self._refresh_provider("codex")

    def _close(self) -> None:
        self._cancel_timer("_rotate_after_id")
        self._cancel_timer("_reset_drain_after_id")
        for rt in getattr(self, "providers", {}).values():
            self._cancel_provider_timer(rt, "scheduled_after_id")
            self._cancel_provider_timer(rt, "recovery_after_id")
            self._cancel_provider_timer(rt, "watchdog_after_id")
        super()._close()


def main() -> None:
    run_single_instance(
        r"Local\AIUsageMiniWidget",
        "AI 사용량 위젯",
        lambda: UsageWidget().run(),
    )


if __name__ == "__main__":
    main()
