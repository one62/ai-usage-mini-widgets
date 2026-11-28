import os
import queue
import sys
import tempfile
import tkinter as tk
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


_WIDGET_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _WIDGET_DIR.parent
_CODEX_DIR = _REPO_ROOT / "codex-usage-widget"
_GEMINI_DIR = _REPO_ROOT / "gemini-usage-widget"
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR), str(_CODEX_DIR), str(_GEMINI_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import ai_settings
import ai_widget
import codex_settings
import codex_usage
import gemini_usage
from ai_settings import (
    DEFAULT_PROVIDER,
    DEFAULT_ROTATE_INTERVAL,
    CombinedWidgetSettings,
    load_settings_from_path,
    save_settings_to_path,
    validate_settings,
)
from ai_widget import UsageWidget, _ProviderRuntime
from common.widget_base import (
    MUTED,
    TEXT,
    TRACK,
    WINDOW_WIDTH,
    YELLOW,
    enable_dpi_awareness,
    load_bundled_fonts,
    usage_color,
)


def _make_codex_snapshot(*, five_pct=25, week_pct=60, weekly_only=False):
    primary = (
        codex_usage.UsageWindow(five_pct, 300, 1_700_000_000)
        if not weekly_only
        else None
    )
    secondary = codex_usage.UsageWindow(week_pct, 10080, 1_700_500_000)
    return codex_usage.UsageSnapshot(
        plan="plus",
        primary=primary,
        secondary=secondary,
        credit_balance=Decimal("42.5"),
        unlimited_credits=False,
        account="codex@example.com",
        executable=r"C:\codex.exe",
        account_id="acc-1",
    )


def _make_gemini_snapshot(*, five_pct=40, week_pct=80):
    return gemini_usage.UsageSnapshot(
        plan="Google AI Pro",
        account_email="gemini@example.com",
        five_hour=gemini_usage.LimitWindow(five_pct, 1_700_001_000),
        seven_day=gemini_usage.LimitWindow(week_pct, 1_700_600_000),
        claude_gpt_five_hour=gemini_usage.LimitWindow(15, 1_700_001_000),
        claude_gpt_seven_day=gemini_usage.LimitWindow(50, 1_700_600_000),
        credit_balance=250,
        fetched_at=1_000_000.0,
    )


def _build_headless_widget(active_provider="codex") -> UsageWidget:
    widget = UsageWidget.__new__(UsageWidget)
    widget.root = tk.Tk()
    widget.root.withdraw()
    widget.settings = CombinedWidgetSettings(active_provider=active_provider)
    widget._last_saved_state = widget._settings_tuple(widget.settings)
    widget._settings_after_id = None
    widget._settings_ready = True
    widget._closing = False
    widget._hovered = False
    widget._menu_open = False
    widget._input_api = None
    widget._input_hwnd = None
    widget.transparency = tk.IntVar(master=widget.root, value=30)
    widget.movement_locked = tk.BooleanVar(master=widget.root, value=False)
    widget.hover_transparency_enabled = tk.BooleanVar(master=widget.root, value=True)
    widget.always_on_top = tk.BooleanVar(master=widget.root, value=True)
    widget.autostart = tk.BooleanVar(master=widget.root, value=False)
    widget.active_provider = active_provider
    widget.rotate_interval_seconds = 10
    widget.rotate_step = tk.IntVar(master=widget.root, value=ai_settings.ROTATE_INTERVALS.index(10))
    widget._rotate_after_id = None
    widget.providers = {key: _ProviderRuntime(key=key) for key in ("codex", "gemini")}
    widget.provider_results = queue.Queue()
    widget.resetting = False
    widget.reset_state_unreadable = False
    widget.pending_reset = None
    widget.reset_results = queue.Queue()
    widget._reset_drain_after_id = None
    widget._drain_results_after_id = None
    widget._full_width = WINDOW_WIDTH
    widget._build()
    return widget


class CombinedSettingsTests(unittest.TestCase):
    def test_validate_settings_defaults_and_rejects_invalid_values(self):
        defaults = validate_settings("not-a-dict")
        self.assertEqual(defaults.active_provider, DEFAULT_PROVIDER)
        self.assertTrue(defaults.auto_rotate)
        self.assertEqual(defaults.rotate_interval_seconds, DEFAULT_ROTATE_INTERVAL)

        validated = validate_settings(
            {
                "transparency": 45,
                "active_provider": "invalid",
                "rotate_interval_seconds": True,
            }
        )
        self.assertEqual(validated.transparency, 45)
        self.assertEqual(validated.active_provider, DEFAULT_PROVIDER)
        self.assertEqual(validated.rotate_interval_seconds, DEFAULT_ROTATE_INTERVAL)

        rejected_interval = validate_settings({"rotate_interval_seconds": 12})
        self.assertEqual(rejected_interval.rotate_interval_seconds, DEFAULT_ROTATE_INTERVAL)

        legacy_off = validate_settings({"auto_rotate": False, "rotate_interval_seconds": 10})
        self.assertEqual(legacy_off.rotate_interval_seconds, 0)
        self.assertFalse(legacy_off.auto_rotate)

    def test_atomic_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            self.assertEqual(load_settings_from_path(path), CombinedWidgetSettings())
            expected = CombinedWidgetSettings(
                transparency=60,
                x=200,
                y=50,
                movement_locked=True,
                hover_transparency_enabled=False,
                active_provider="gemini",
                rotate_interval_seconds=3,
            )
            save_settings_to_path(expected, path)
            self.assertEqual(load_settings_from_path(path), expected)


class ProviderIsolationAndRotationTests(unittest.TestCase):
    def _make_mock_widget(self, active="codex"):
        widget = UsageWidget.__new__(UsageWidget)
        widget.root = mock.Mock()
        widget.root.after.return_value = "timer-1"
        widget.settings = CombinedWidgetSettings(active_provider=active)
        widget._last_saved_state = widget._settings_tuple(widget.settings)
        widget._settings_after_id = None
        widget._settings_ready = True
        widget._closing = False
        widget._menu_open = False
        widget.resetting = False
        widget.reset_state_unreadable = False
        widget.pending_reset = None
        widget.transparency = mock.Mock()
        widget.transparency.get.return_value = 30
        widget.movement_locked = mock.Mock()
        widget.movement_locked.get.return_value = False
        widget.hover_transparency_enabled = mock.Mock()
        widget.hover_transparency_enabled.get.return_value = True
        widget.active_provider = active
        widget.rotate_interval_seconds = 10
        widget.rotate_step = mock.Mock()
        widget.rotate_step.get.return_value = ai_settings.ROTATE_INTERVALS.index(10)
        widget._rotate_after_id = None
        widget._drain_results_after_id = None
        widget.providers = {key: _ProviderRuntime(key=key) for key in ("codex", "gemini")}
        widget.provider_results = queue.Queue()
        widget.menus = {"codex": mock.Mock(open=False), "gemini": mock.Mock(open=False)}
        widget._menu_indices = {
            "codex": {
                "refresh": 1,
                "connection_status": 2,
                "account": 4,
                "plan": 5,
                "reset_count": 6,
                "reset_use": 7,
                "rotate_interval": 10,
            },
            "gemini": {
                "refresh": 1,
                "connection_status": 2,
                "account": 4,
                "plan": 5,
                "claude_5h": 6,
                "claude_7d": 7,
                "rotate_interval": 10,
            },
        }
        widget.five_section = mock.Mock()
        widget.five_section.winfo_manager.return_value = "pack"
        widget.five_title = mock.Mock()
        widget.five_label = mock.Mock()
        widget.reset_compact = mock.Mock()
        widget.five_meter = mock.Mock()
        widget.week_title = mock.Mock()
        widget.week_label = mock.Mock()
        widget.week_reset = mock.Mock()
        widget.week_meter = mock.Mock()
        widget.logo_label = mock.Mock()
        widget._logos = {"codex": "codex-img", "gemini": "gemini-img"}
        widget.reset_status = mock.Mock()
        widget._full_width = 278
        widget._fit_content_width = mock.Mock()
        widget._ensure_topmost = mock.Mock()
        return widget

    def test_provider_errors_are_isolated_and_stale_generations_ignored(self):
        widget = self._make_mock_widget("codex")
        codex_snap = _make_codex_snapshot(five_pct=25, week_pct=60)
        widget.providers["codex"].refreshing = True
        widget.providers["codex"].fetch_generation = 2
        widget.providers["gemini"].refreshing = True
        widget.providers["gemini"].fetch_generation = 1

        # Stale Codex generation 1 must be ignored; Gemini auth error only affects Gemini.
        widget.provider_results.put(("codex", 1, "ok", _make_codex_snapshot(five_pct=99)))
        widget.provider_results.put(("gemini", 1, "error", gemini_usage.AuthRequiredError("login")))
        widget.provider_results.put(("codex", 2, "ok", codex_snap))
        widget._drain_results()

        self.assertIs(widget.providers["codex"].snapshot, codex_snap)
        self.assertFalse(widget.providers["codex"].connection_failed)
        self.assertTrue(widget.providers["gemini"].connection_failed)
        self.assertTrue(widget.providers["gemini"].auth_required)
        self.assertIsNone(widget.providers["gemini"].snapshot)
        # Codex UI stayed normal because Codex is active and succeeded.
        widget.five_label.configure.assert_called_with(text="25%", fg=usage_color(25))

        # Switching to Gemini shows offline without losing Codex snapshot.
        widget.switch_provider("gemini", manual=True)
        widget.reset_compact.configure.assert_called_with(text="offline", fg=MUTED)
        self.assertIs(widget.providers["codex"].snapshot, codex_snap)

    def test_watchdog_marks_only_timed_out_provider_failed(self):
        widget = self._make_mock_widget("codex")
        widget.providers["gemini"].refreshing = True
        widget.providers["gemini"].fetch_generation = 3
        widget.providers["codex"].refreshing = True
        widget.providers["codex"].fetch_generation = 1

        widget._fetch_provider_watchdog("gemini", 3)

        self.assertFalse(widget.providers["gemini"].refreshing)
        self.assertTrue(widget.providers["gemini"].connection_failed)
        self.assertEqual(widget.providers["gemini"].fetch_generation, 4)
        self.assertTrue(widget.providers["codex"].refreshing)
        self.assertFalse(widget.providers["codex"].connection_failed)

    def test_manual_switch_auto_rotate_pause_resume_and_interval_cycle(self):
        widget = self._make_mock_widget("codex")
        widget.root.after.return_value = "rot-1"

        widget._start_extra_timers()
        widget.root.after.assert_called_with(10_000, widget._auto_rotate_tick)

        # Auto-rotate switches to Gemini and schedules next tick.
        widget._auto_rotate_tick()
        self.assertEqual(widget.active_provider, "gemini")

        # While menu is open or resetting, auto-rotate pauses.
        widget._menu_open = True
        self.assertFalse(widget._can_auto_rotate())
        widget._menu_open = False
        widget.resetting = True
        self.assertFalse(widget._can_auto_rotate())
        widget.resetting = False

        # Middle click triggers manual switch and resets rotate timer + saves settings.
        widget._on_middle_click()
        self.assertEqual(widget.active_provider, "codex")
        self.assertIsNotNone(widget._settings_after_id)

        # Slider steps map to 0(끄기), 1초, 3초, 5초, 10초, 30초.
        expected_labels = ["끄기", "1초", "3초", "5초", "10초", "30초"]
        for step, (seconds, label) in enumerate(zip(ai_settings.ROTATE_INTERVALS, expected_labels)):
            self.assertEqual(widget._format_rotate_step(step), label)
            widget._set_rotate_step(step)
            self.assertEqual(widget.rotate_interval_seconds, seconds)
            if seconds == 0:
                self.assertFalse(widget._can_auto_rotate())
            else:
                self.assertTrue(widget._can_auto_rotate())
                widget.root.after.assert_any_call(seconds * 1000, widget._auto_rotate_tick)

    def test_middle_click_ignored_while_menu_open(self):
        widget = self._make_mock_widget("codex")
        widget._menu_open = True
        widget._on_middle_click()
        self.assertEqual(widget.active_provider, "codex")


@unittest.skipUnless(os.name == "nt", "Requires Windows Tk")
class CombinedWidgetUITests(unittest.TestCase):
    def test_unified_usage_colors_and_constant_width_across_provider_switches(self):
        enable_dpi_awareness()
        load_bundled_fonts()
        widget = _build_headless_widget("codex")
        try:
            widget.root.after_idle = lambda cb: cb()
            codex_weekly_only = _make_codex_snapshot(week_pct=30, weekly_only=True)
            gemini_snap = _make_gemini_snapshot(five_pct=40, week_pct=85)
            widget.providers["codex"].snapshot = codex_weekly_only
            widget.providers["gemini"].snapshot = gemini_snap

            full_width = widget._full_width
            self.assertGreaterEqual(full_width, WINDOW_WIDTH)

            # Codex weekly-only hides 5H section but keeps window width steady.
            widget._render_active_provider()
            self.assertFalse(bool(widget.five_section.winfo_manager()))
            self.assertEqual(widget.week_title.cget("fg"), usage_color(30))
            self.assertEqual(widget.week_label.cget("fg"), usage_color(30))
            self.assertEqual(widget.root.winfo_width() or full_width, full_width)

            # Switch to Gemini: 5H section reappears and both 5H & WK use usage_color.
            widget.switch_provider("gemini", manual=True)
            self.assertTrue(bool(widget.five_section.winfo_manager()))
            self.assertEqual(widget.five_title.cget("fg"), usage_color(40))
            self.assertEqual(widget.five_label.cget("text"), "40%")
            self.assertEqual(widget.five_label.cget("fg"), usage_color(40))
            self.assertEqual(widget.week_title.cget("fg"), usage_color(85))
            self.assertEqual(widget.week_label.cget("text"), "85%")
            self.assertEqual(widget.week_label.cget("fg"), usage_color(85))
            widget.root.update_idletasks()
            self.assertEqual(widget.root.winfo_reqwidth(), full_width)

            # None values fall back to TEXT.
            widget.providers["gemini"].snapshot = gemini_usage.UsageSnapshot(
                plan="Google AI Pro",
                account_email=None,
                five_hour=None,
                seven_day=None,
                claude_gpt_five_hour=None,
                claude_gpt_seven_day=None,
                credit_balance=None,
                fetched_at=1_000_000.0,
            )
            widget._render_active_provider()
            self.assertEqual(widget.five_title.cget("fg"), TEXT)
            self.assertEqual(widget.five_label.cget("fg"), TEXT)
            self.assertEqual(widget.week_title.cget("fg"), TEXT)
            self.assertEqual(widget.week_label.cget("fg"), TEXT)
        finally:
            widget.root.destroy()

    def test_shared_transparency_scale_syncs_across_both_menus_and_labels_fit(self):
        enable_dpi_awareness()
        load_bundled_fonts()
        widget = _build_headless_widget("codex")
        try:
            widget.transparency.set(70)
            widget.rotate_step.set(2)  # 3초
            widget.menus["gemini"].show(100, 100)
            scale_entries = [
                entry for entry in widget.menus["gemini"].entries if entry["kind"] == "scale"
            ]
            self.assertEqual(len(scale_entries), 2)
            self.assertEqual(scale_entries[0]["value_widget"].cget("text"), "3초")
            self.assertEqual(scale_entries[1]["value_widget"].cget("text"), "70%")
            self.assertEqual(scale_entries[1]["resolution"], 10)
            widget.menus["gemini"].close()

            for title, label in (
                (widget.five_title, widget.five_label),
                (widget.week_title, widget.week_label),
            ):
                label.configure(text="100%")
                widget.root.update_idletasks()
                self.assertLessEqual(
                    title.winfo_reqwidth() + label.winfo_reqwidth(),
                    int(label.master.cget("width")),
                )
            for label, sample in (
                (widget.reset_compact, "4h 58m"),
                (widget.week_reset, "6d 23h"),
            ):
                label.configure(text=sample)
                widget.root.update_idletasks()
                self.assertLessEqual(
                    label.winfo_reqwidth(),
                    int(label.master.cget("width")),
                )
        finally:
            widget.root.destroy()

    def test_codex_reset_credit_unverified_account_archives_pending_reset(self):
        widget = _build_headless_widget("codex")
        try:
            with tempfile.TemporaryDirectory() as temp:
                fake_settings = Path(temp) / "settings.json"
                with mock.patch.object(codex_settings, "settings_path", return_value=fake_settings):
                    pending = codex_settings.PendingReset("req-1", r"C:\old\codex.exe", "cred-1")
                    codex_settings.save_pending_reset(pending)
                    widget.pending_reset = pending
                    widget.resetting = True
                    widget.reset_results.put(
                        ("error", codex_usage.ResetAccountUnverifiedError("계정 확인 불가"))
                    )
                    with (
                        mock.patch.object(ai_widget.messagebox, "askyesno", return_value=True),
                        mock.patch.object(widget, "_refresh_provider") as refresh_mock,
                    ):
                        widget._drain_reset_results()
                    self.assertIsNone(widget.pending_reset)
                    self.assertIsNone(codex_settings.load_pending_reset())
                    refresh_mock.assert_called_once_with("codex")
        finally:
            widget.root.destroy()


if __name__ == "__main__":
    unittest.main()
