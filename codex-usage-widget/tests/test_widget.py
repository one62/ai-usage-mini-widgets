import json
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
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import codex_usage
import widget as codex_widget_module
import widget_settings
from codex_usage import (
    AuthRequiredError,
    UsageError,
    UsageSnapshot,
    UsageWindow,
    _LineReader,
    _wait_for_id,
    consume_reset_credit,
    find_codex_executables,
    parse_rate_limits,
    query_usage,
)
from common.widget_base import enable_dpi_awareness, load_bundled_fonts
from widget import UsageWidget
from widget_settings import PendingReset, load_pending_reset, save_pending_reset


class ParseRateLimitsTests(unittest.TestCase):
    def test_parses_codex_bucket(self):
        snapshot = parse_rate_limits(
            {
                "rateLimits": {},
                "rateLimitsByLimitId": {
                    "codex": {
                        "planType": "plus",
                        "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 123},
                        "secondary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": 456},
                        "credits": {"unlimited": False, "balance": "83.02622"},
                        "rateLimitReachedType": None,
                    }
                },
            }
        )
        self.assertEqual(snapshot.plan, "plus")
        self.assertEqual(snapshot.primary.remaining_percent, 93)
        self.assertEqual(snapshot.secondary.remaining_percent, 83)
        self.assertEqual(snapshot.credit_balance, Decimal("83.02622"))

    def test_clamps_percentages(self):
        snapshot = parse_rate_limits({"rateLimits": {"primary": {"usedPercent": 120}}})
        self.assertEqual(snapshot.primary.used_percent, 100)
        self.assertEqual(snapshot.primary.remaining_percent, 0)

    def test_rejects_missing_limits(self):
        with self.assertRaises(UsageError):
            parse_rate_limits({})

    def test_accepts_a_future_bucket_identifier(self):
        snapshot = parse_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex-next": {
                        "primary": {"usedPercent": "12.8", "windowDurationMins": 300}
                    }
                }
            }
        )
        self.assertEqual(snapshot.primary.used_percent, 12)

    def test_finds_versioned_desktop_codex_when_path_is_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "OpenAI" / "Codex" / "bin" / "new" / "codex.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}, clear=True):
                with mock.patch("codex_usage.shutil.which", return_value=None):
                    self.assertEqual(find_codex_executables(), [str(executable.resolve())])

    def test_pending_reset_persistence_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_settings = Path(temp) / "settings.json"
            with mock.patch.object(widget_settings, "settings_path", return_value=fake_settings):
                self.assertIsNone(load_pending_reset())
                pending = PendingReset(key="req-1", executable=r"C:\codex.exe", credit_id="cred-1")
                save_pending_reset(pending)
                self.assertEqual(load_pending_reset(), pending)
                save_pending_reset(None)
                self.assertIsNone(load_pending_reset())

    def test_auth_error_propagates_as_auth_required_and_is_non_retryable(self):
        stream = [json.dumps({"id": 2, "error": {"message": "401 Unauthorized"}}) + "\n"]
        reader = _LineReader(stream)
        with self.assertRaises(AuthRequiredError):
            _wait_for_id(reader, 2, deadline=10**12)

        with mock.patch("codex_usage.find_codex_executables", return_value=[r"C:\codex.exe"]):
            with mock.patch.object(
                codex_usage,
                "_query_with_executable",
                side_effect=AuthRequiredError("Codex 로그인이 필요합니다."),
            ):
                with self.assertRaises(AuthRequiredError):
                    query_usage(timeout=2.0)

        widget = UsageWidget.__new__(UsageWidget)
        self.assertTrue(widget._is_non_retryable_fetch_error(AuthRequiredError("auth")))
        self.assertFalse(widget._is_non_retryable_fetch_error(UsageError("conn")))

    def test_non_finite_credit_balance_and_reset_credits_parsing(self):
        snapshot = parse_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 10},
                    "credits": {"unlimited": False, "balance": "Infinity"},
                },
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [
                        {"id": "later", "status": "available", "expiresAt": 2000},
                        {"id": "sooner", "status": "available", "expiresAt": 1000},
                        {"id": "used", "status": "redeemed", "expiresAt": 500},
                    ],
                },
            }
        )
        self.assertIsNone(snapshot.credit_balance)
        self.assertEqual(snapshot.reset_credit_count, 2)
        self.assertEqual(snapshot.reset_credit_id, "sooner")
        self.assertEqual(snapshot.reset_credit_expires_at, 1000)

        nan_snapshot = parse_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 10},
                    "credits": {"unlimited": False, "balance": "NaN"},
                },
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [{"id": "only-one", "status": "available", "expiresAt": 1000}],
                },
            }
        )
        self.assertIsNone(nan_snapshot.credit_balance)
        self.assertEqual(nan_snapshot.reset_credit_count, 2)
        self.assertIsNone(nan_snapshot.reset_credit_id)

    def test_token_limit_error_is_not_misclassified_as_auth_and_auth_message_preserved(self):
        reader = _LineReader([json.dumps({"id": 2, "error": {"message": "max token limit exceeded"}}) + "\n"])
        with self.assertRaises(UsageError) as ctx:
            _wait_for_id(reader, 2, deadline=10**12)
        self.assertNotIsInstance(ctx.exception, AuthRequiredError)

        reader2 = _LineReader([json.dumps({"id": 2, "error": {"message": "access token expired"}}) + "\n"])
        with self.assertRaises(AuthRequiredError):
            _wait_for_id(reader2, 2, deadline=10**12)

        codex_usage._set_cached_executable(None)
        with mock.patch("codex_usage.find_codex_executables", return_value=[r"C:\first.exe", r"C:\second.exe"]):
            with mock.patch.object(
                codex_usage,
                "_query_with_executable",
                side_effect=[AuthRequiredError("auth msg"), UsageError("second failed")],
            ):
                with self.assertRaises(AuthRequiredError) as auth_ctx:
                    query_usage(timeout=2.0)
        self.assertEqual(str(auth_ctx.exception), "auth msg")

    def test_query_usage_caches_working_executable_and_consume_matches_case_insensitively(self):
        with tempfile.TemporaryDirectory() as temp:
            exe = Path(temp) / "Codex.exe"
            exe.touch()
            resolved = str(exe.resolve())
            expected_snapshot = UsageSnapshot(
                plan="plus",
                primary=UsageWindow(10, 300, 1000),
                secondary=None,
                credit_balance=None,
                unlimited_credits=False,
            )
            codex_usage._set_cached_executable(None)
            try:
                with mock.patch("codex_usage.find_codex_executables", return_value=[resolved]) as find_mock:
                    with mock.patch.object(codex_usage, "_query_with_executable", return_value=expected_snapshot):
                        snap1 = query_usage(timeout=2.0)
                        snap2 = query_usage(timeout=2.0)
                self.assertEqual(find_mock.call_count, 1)
                self.assertEqual(snap1.executable, resolved)
                self.assertEqual(snap2.executable, resolved)

                with mock.patch("codex_usage.find_codex_executables", return_value=[resolved]):
                    with mock.patch.object(codex_usage, "_query_with_executable", return_value="reset") as consume_mock:
                        outcome = consume_reset_credit("key-1", resolved.lower(), "cred-1")
                self.assertEqual(outcome, "reset")
                self.assertEqual(consume_mock.call_args.args[0], resolved)
            finally:
                codex_usage._set_cached_executable(None)

    def test_drain_reset_results_clears_or_retains_pending_reset_and_stops_when_idle(self):
        widget = UsageWidget.__new__(UsageWidget)
        widget.root = mock.Mock()
        widget.menu = mock.Mock()
        widget.reset_results = queue.Queue()
        widget.resetting = True
        widget._closing = False
        widget._reset_drain_after_id = "timer-r"
        widget.pending_reset = PendingReset("k", r"C:\codex.exe", "c")
        widget.refresh = mock.Mock()
        widget._ensure_topmost = mock.Mock()
        widget._update_reset_menu = mock.Mock()

        widget.reset_results.put(("error", ""))
        with mock.patch.object(codex_widget_module.messagebox, "showwarning"):
            widget._drain_reset_results()
        self.assertFalse(widget.resetting)
        self.assertIsNotNone(widget.pending_reset)
        widget.root.after.assert_not_called()

        widget.resetting = True
        widget.reset_results.put(("ok", "reset"))
        with mock.patch.object(codex_widget_module, "save_pending_reset") as save_mock:
            with mock.patch.object(codex_widget_module.messagebox, "showinfo"):
                widget._drain_reset_results()
        save_mock.assert_called_once_with(None)
        self.assertIsNone(widget.pending_reset)
        widget.refresh.assert_called_once_with()
        widget.root.after.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Requires Windows Tk font metrics")
    def test_fixed_titles_zero_border_labels_and_login_status_action(self):
        enable_dpi_awareness()
        load_bundled_fonts()
        widget = UsageWidget.__new__(UsageWidget)
        widget.root = tk.Tk()
        widget.root.withdraw()
        try:
            widget.transparency = tk.IntVar(master=widget.root, value=30)
            widget.movement_locked = tk.BooleanVar(master=widget.root, value=False)
            widget.hover_transparency_enabled = tk.BooleanVar(master=widget.root, value=True)
            widget.always_on_top = tk.BooleanVar(master=widget.root, value=True)
            widget.autostart = tk.BooleanVar(master=widget.root, value=False)
            widget._closing = False
            widget._open_chatgpt = mock.Mock()
            widget._build()

            self.assertEqual(int(widget.five_meter.cget("width")), 23)
            self.assertEqual(int(widget.week_meter.cget("width")), 23)

            # Connection status row stays disabled unless auth_required is True.
            status_entry = widget.menu.entries[widget.connection_status_index]
            self.assertEqual(status_entry["kind"], "command")
            self.assertEqual(status_entry["state"], "disabled")

            widget.refreshing = False
            widget.auth_required = True
            widget._update_connection_status()
            self.assertEqual(status_entry["label"], "연결 상태: 로그인 필요")
            self.assertEqual(status_entry["state"], "normal")
            widget.menu.open = True
            widget.menu._invoke(widget.connection_status_index)
            widget._open_chatgpt.assert_called_once_with()

            for title, label, expected_title in (
                (widget.five_title, widget.five_label, "5H"),
                (widget.week_title, widget.week_label, "WK"),
            ):
                self.assertEqual(label.master.pack_info()["padx"], (1, 0))
                self.assertEqual(title.cget("text"), expected_title)
                self.assertEqual(title.cget("anchor"), "w")
                self.assertEqual(int(title.cget("bd")), 0)
                self.assertEqual(int(title.cget("highlightthickness")), 0)
                self.assertEqual(label.cget("anchor"), "e")
                self.assertEqual(int(label.cget("bd")), 0)
                self.assertEqual(int(label.cget("highlightthickness")), 0)
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
                self.assertEqual(int(label.cget("bd")), 0)
                self.assertEqual(int(label.cget("highlightthickness")), 0)
                label.configure(text=sample)
                widget.root.update_idletasks()
                self.assertLessEqual(
                    label.winfo_reqwidth(),
                    int(label.master.cget("width")),
                )
        finally:
            widget.root.destroy()


if __name__ == "__main__":
    unittest.main()
