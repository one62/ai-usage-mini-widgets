import json
import os
import queue
import sys
import tempfile
import time
import tkinter as tk
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock


_WIDGET_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import codex_settings
import codex_usage
import codex_widget
from codex_settings import PendingReset, load_pending_reset, save_pending_reset
from codex_usage import (
    AuthRequiredError,
    ResetAccountUnverifiedError,
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
from codex_widget import UsageWidget
from common.widget_base import enable_dpi_awareness, load_bundled_fonts


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
            with mock.patch.object(codex_settings, "settings_path", return_value=fake_settings):
                self.assertIsNone(load_pending_reset())
                pending = PendingReset("req-1", r"C:\codex.exe", "cred-1", "account-1")
                save_pending_reset(pending)
                self.assertEqual(load_pending_reset(), pending)
                save_pending_reset(None)
                self.assertIsNone(load_pending_reset())

    def test_legacy_pending_reset_can_be_archived_without_losing_its_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pending-reset.json"
            legacy = {"key": "req-1", "executable": r"C:\old\codex.exe", "credit_id": "cred-1"}
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with mock.patch.object(codex_settings, "settings_path", return_value=path.with_name("settings.json")):
                pending = load_pending_reset()
                self.assertEqual(pending, PendingReset(**legacy))
                with self.assertRaises(ValueError):
                    codex_settings.archive_pending_reset(replace(pending, key="another-request"))
                widget = UsageWidget.__new__(UsageWidget)
                widget.pending_reset = pending
                widget._closing = widget.resetting = False
                widget.root = mock.Mock()
                widget._update_reset_menu = mock.Mock()
                widget.refresh = mock.Mock()
                with mock.patch.object(codex_widget.messagebox, "askyesno", side_effect=[False, True]):
                    widget._offer_archive_pending_reset("이전 요청의 계정 정보를 확인할 수 없습니다.")
                    self.assertEqual(load_pending_reset(), pending)
                    widget._offer_archive_pending_reset("이전 요청의 계정 정보를 확인할 수 없습니다.")
                self.assertIsNone(load_pending_reset())
                self.assertIsNone(widget.pending_reset)
                widget.refresh.assert_called_once_with()
                archived, = path.parent.glob("pending-reset-archived-*.json")
                self.assertEqual(json.loads(archived.read_text(encoding="utf-8")), legacy)

    def test_auth_error_propagates_as_auth_required_and_is_non_retryable(self):
        stream = [json.dumps({"id": 2, "error": {"message": "401 Unauthorized"}}) + "\n"]
        reader = _LineReader(stream)
        with self.assertRaises(AuthRequiredError):
            _wait_for_id(reader, 2, deadline=time.monotonic() + 5.0)

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
        self.assertFalse(snapshot.reset_credit_details_limited)

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
        self.assertEqual(nan_snapshot.reset_credit_id, "only-one")
        self.assertTrue(nan_snapshot.reset_credit_details_limited)

    def test_capped_credit_list_enables_a_confirmed_selection(self):
        widget = UsageWidget.__new__(UsageWidget)
        snapshot = parse_rate_limits({
            "accountId": "account-1",
            "rateLimits": {"primary": {"usedPercent": 10}},
            "rateLimitResetCredits": {
                "availableCount": 2,
                "credits": [{"id": "only-one", "status": "available", "expiresAt": None}],
            },
        })
        widget.snapshot = replace(snapshot, executable=r"C:\codex.exe")
        widget.resetting = widget.refreshing = widget.connection_failed = widget.reset_state_unreadable = False
        widget.pending_reset = None
        widget.root = mock.Mock()
        widget.menu = mock.Mock()
        widget.reset_count_index = 1
        widget.reset_use_index = 2
        widget._ensure_topmost = mock.Mock()

        widget._update_reset_menu()
        widget.menu.entryconfigure.assert_called_with(2, label="초기화권 사용", state="normal")
        with mock.patch.object(codex_widget.messagebox, "askyesno", return_value=False) as ask:
            widget._use_reset_credit()
        self.assertIn("만료일 없음", ask.call_args.args[1])
        self.assertIn("확인할 수 있는 초기화권", ask.call_args.args[1])
        self.assertEqual(snapshot.account_id, "account-1")

    def test_token_limit_error_is_not_misclassified_as_auth_and_auth_message_preserved(self):
        reader = _LineReader([json.dumps({"id": 2, "error": {"message": "max token limit exceeded"}}) + "\n"])
        with self.assertRaises(UsageError) as ctx:
            _wait_for_id(reader, 2, deadline=time.monotonic() + 5.0)
        self.assertNotIsInstance(ctx.exception, AuthRequiredError)

        reader2 = _LineReader([json.dumps({"id": 2, "error": {"message": "access token expired"}}) + "\n"])
        with self.assertRaises(AuthRequiredError):
            _wait_for_id(reader2, 2, deadline=time.monotonic() + 5.0)

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
        widget.reset_status = mock.Mock()
        widget.reset_results = queue.Queue()
        widget.resetting = True
        widget._closing = False
        widget._reset_drain_after_id = "timer-r"
        widget.pending_reset = PendingReset("k", r"C:\codex.exe", "c")
        widget.refresh = mock.Mock()
        widget._ensure_topmost = mock.Mock()
        widget._update_reset_menu = mock.Mock()

        widget.reset_results.put(("error", ""))
        with mock.patch.object(codex_widget.messagebox, "showwarning"):
            widget._drain_reset_results()
        self.assertFalse(widget.resetting)
        self.assertIsNotNone(widget.pending_reset)
        widget.root.after.assert_not_called()

        widget.resetting = True
        widget.reset_results.put(("ok", "reset"))
        with mock.patch.object(codex_widget, "save_pending_reset") as save_mock:
            with mock.patch.object(codex_widget.messagebox, "showinfo"):
                widget._drain_reset_results()
        save_mock.assert_called_once_with(None)
        self.assertIsNone(widget.pending_reset)
        widget.refresh.assert_called_once_with()
        widget.root.after.assert_not_called()

    def test_replacement_executable_checks_account_before_reusing_reset_key(self):
        previous = r"C:\old\codex.exe"
        current = r"C:\new\codex.exe"
        for account in (None, "other", "original"):
            with self.subTest(account=account):
                process = mock.Mock()
                process.poll.return_value = 0
                with (
                    mock.patch.object(codex_usage, "find_codex_executables", return_value=[current]),
                    mock.patch.object(codex_usage.subprocess, "Popen", return_value=process) as launch,
                    mock.patch.object(codex_usage, "_LineReader"),
                    mock.patch.object(codex_usage, "_wait_for_id", side_effect=[
                        {}, {"accountId": account}, {"outcome": "alreadyRedeemed"},
                    ]),
                    mock.patch.object(codex_usage, "_send") as send,
                ):
                    if account == "original":
                        result = consume_reset_credit("req-1", previous, "cred-1", account_id="original")
                        self.assertEqual(result, "alreadyRedeemed")
                    elif account is None:
                        with self.assertRaises(ResetAccountUnverifiedError):
                            consume_reset_credit("req-1", previous, "cred-1", account_id="original")
                    else:
                        with self.assertRaises(UsageError):
                            consume_reset_credit("req-1", previous, "cred-1", account_id="original")
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0][0], current)
                requests = [call.args[1] for call in send.call_args_list]
                self.assertEqual(requests[2]["method"], "account/rateLimits/read")
                if account == "original":
                    self.assertEqual(requests[3], {
                        "id": 3, "method": "account/rateLimitResetCredit/consume",
                        "params": {"idempotencyKey": "req-1", "creditId": "cred-1"},
                    })
                else:
                    self.assertEqual(len(requests), 3)

        with (
            mock.patch.object(codex_usage, "find_codex_executables", return_value=[current]),
            mock.patch.object(codex_usage, "_query_with_executable") as query,
        ):
            with self.assertRaises(ResetAccountUnverifiedError):
                consume_reset_credit("req-1", previous, "cred-1")
        query.assert_not_called()

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
