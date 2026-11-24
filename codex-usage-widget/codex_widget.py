"""Small always-on-top Windows widget for ChatGPT Work/Codex usage."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import Label, messagebox

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from codex_settings import (
    PendingReset,
    archive_pending_reset,
    load_pending_reset,
    load_settings,
    save_pending_reset,
    save_settings,
)
from codex_usage import (
    AuthRequiredError,
    ResetAccountUnverifiedError,
    UsageTimeoutError,
    UsageError,
    UsageSnapshot,
    consume_reset_credit,
    find_codex_executables,
    query_usage,
)
from common.widget_base import (
    MUTED,
    PopupMenu,
    TEXT,
    BaseUsageWidget,
    reset_short,
    run_single_instance,
)


RUN_VALUE = "ChatGPTUsageWidget"


class UsageWidget(BaseUsageWidget):
    APP_TITLE = "ChatGPT Usage"
    RUN_VALUE = RUN_VALUE

    def __init__(self) -> None:
        self._init_base(script_path=Path(__file__).resolve().with_name("widget.py"))
        self.resetting = False
        self.reset_state_unreadable = False
        try:
            self.pending_reset = load_pending_reset()
        except (OSError, ValueError, RecursionError):
            self.pending_reset = None
            self.reset_state_unreadable = True
        self.reset_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._reset_drain_after_id: str | None = None
        self._build()
        self._finalize_init()

    def _load_widget_settings(self):
        return load_settings()

    def _save_widget_settings(self, settings) -> None:
        save_settings(settings)

    def _close(self) -> None:
        self._cancel_timer("_reset_drain_after_id")
        super()._close()

    def _can_start_refresh(self) -> bool:
        return not self.refreshing and not self.resetting

    def _on_refresh_state_changed(self) -> None:
        self._update_reset_menu()

    def _query_usage(self, timeout: float = 18.0) -> UsageSnapshot:
        return query_usage(timeout=timeout)

    def _is_non_retryable_fetch_error(self, exc: Exception) -> bool:
        return isinstance(exc, AuthRequiredError)

    def _open_app(self) -> None:
        self._open_chatgpt()

    def _build(self) -> None:
        logo_path = Path(__file__).resolve().parent / "assets" / "codex-logo.png"
        self._build_header_shell(
            logo_path=logo_path,
            five_fg=TEXT,
            week_fg=TEXT,
        )

        self.reset_status = Label(
            self.header, bg=self.header.cget("bg"), fg=TEXT, font=self.five_title.cget("font")
        )

        self.menu = PopupMenu(self.root, self._menu_closed, reserve_indicator_space=True)
        self.menu.add_command(label="지금 새로고침", command=self.refresh)
        self.menu.add_command(
            label="연결 상태: 확인 전",
            command=self._on_connection_status_click,
            state="disabled",
        )
        self.connection_status_index = self.menu.index("end")
        self.menu.add_separator()
        self.menu.add_info(label="계정: —")
        self.account_index = self.menu.index("end")
        self.menu.add_info(label="요금제: —", right_text="크레딧: —")
        self.plan_index = self.menu.index("end")
        self.menu.add_info(label="초기화권: —", right_text="")
        self.reset_count_index = self.menu.index("end")
        self.menu.add_command(label="초기화권 사용", state="disabled", command=self._use_reset_credit)
        self.reset_use_index = self.menu.index("end")
        self.menu.add_separator()
        self._add_common_preference_menu_items(self.menu)
        self.menu.add_separator()
        self.menu.add_command(label="ChatGPT 데스크톱 열기", command=self._open_chatgpt)
        self.menu.add_command(label="종료", command=self._close)
        self._fit_content_width()

    def _open_chatgpt(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = find_codex_executables()
            if not executables:
                raise OSError("Codex executable not found")
            subprocess.Popen([executables[0], "app"], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://chatgpt.com/")

    def _update_reset_menu(self) -> None:
        count = self.snapshot.reset_credit_count if self.snapshot else None
        expiry = self._reset_expiry_label(self.snapshot) if count else ""
        self.menu.entryconfigure(
            self.reset_count_index,
            label=f"초기화권 {count}개 사용 가능" if count is not None else "초기화권: 조회 불가",
            right_text=expiry,
        )
        retry = self.pending_reset is not None
        can_start = (
            not self.connection_failed
            and self.snapshot is not None
            and self.snapshot.reset_credit_id is not None
            and self.snapshot.executable is not None
        )
        enabled = (
            not self.resetting
            and not self.refreshing
            and not self.reset_state_unreadable
            and (retry or can_start)
        )
        label = "사용 결과 확인" if retry else "초기화권 사용"
        if self.resetting:
            label = self.reset_status.cget("text")
        elif self.reset_state_unreadable:
            label = "초기화권 사용 불가 (요청 기록 확인 필요)"
        elif count and not retry and not can_start and not self.connection_failed:
            label = "초기화권 사용 불가 (대상 확인 필요)"
        self.menu.entryconfigure(
            self.reset_use_index,
            label=label,
            state="normal" if enabled else "disabled",
        )

    @staticmethod
    def _reset_expiry_label(snapshot: UsageSnapshot) -> str:
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
        if self.resetting or self.refreshing or self.reset_state_unreadable:
            return
        request = self.pending_reset
        snapshot = self.snapshot
        retry = request is not None
        if retry:
            prompt = "이전 요청에서 초기화권이 사용됐는지 확인할까요?\n초기화권을 추가로 사용하지 않습니다."
        else:
            if (
                self.connection_failed
                or snapshot is None
                or not snapshot.reset_credit_id
                or not snapshot.executable
            ):
                return
            request = PendingReset(
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
        # Tk modal dialogs keep timers running, including scheduled refreshes.
        if self._closing:
            return
        if self.resetting or self.refreshing:
            messagebox.showinfo("사용량 초기화", "조회가 끝난 뒤 다시 선택해 주세요.", parent=self.root)
            self._ensure_topmost()
            return
        if not retry and (
            self.connection_failed or self.snapshot is not snapshot or self.pending_reset is not None
        ):
            return
        if not retry:
            try:
                save_pending_reset(request)
            except OSError:
                messagebox.showwarning("사용량 초기화", "요청을 저장하지 못해 초기화를 시작할 수 없습니다. 잠시 후 다시 시도하세요.", parent=self.root)
                self._ensure_topmost()
                return
            self.pending_reset = request
        elif self.pending_reset != request:
            return
        self.resetting = True
        self.reset_status.configure(text="사용 결과 확인 중…" if retry else "사용량 초기화 중…")
        self.reset_status.place(x=0, y=0, relwidth=1, relheight=1)
        self.reset_status.lift()
        self.menu.entryconfigure(0, state="disabled")
        self._update_reset_menu()
        threading.Thread(target=self._reset_worker, args=(request,), daemon=True).start()
        self._ensure_drain_reset_results()

    def _ensure_drain_reset_results(self) -> None:
        if getattr(self, "_reset_drain_after_id", None) is None and not getattr(self, "_closing", False):
            self._reset_drain_after_id = self.root.after(100, self._drain_reset_results)

    def _reset_worker(self, request: PendingReset) -> None:
        try:
            outcome = consume_reset_credit(
                request.key, request.executable, request.credit_id, account_id=request.account_id
            )
            self.reset_results.put(("ok", outcome))
        except UsageError as exc:
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
        self.menu.entryconfigure(0, state="normal")
        if kind == "ok":
            try:
                save_pending_reset(None)
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
            self.refresh()
            messagebox.showinfo(
                "사용량 초기화",
                messages.get(str(payload), "초기화권 사용 요청을 처리했습니다."),
                parent=self.root,
            )
            self._ensure_topmost()
        else:
            self._update_reset_menu()
            if isinstance(payload, ResetAccountUnverifiedError):
                self._offer_archive_pending_reset(str(payload))
            else:
                reason = "초기화권 사용 여부를 확인하지 못했습니다."
                if isinstance(payload, UsageTimeoutError):
                    reason = "응답이 늦어 초기화권 사용 여부를 확인하지 못했습니다."
                elif isinstance(payload, UsageError):
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
            archive_pending_reset(request)
        except (OSError, ValueError, RecursionError):
            messagebox.showwarning("사용량 초기화", "요청 기록을 보관하지 못했습니다.", parent=self.root)
        else:
            self.pending_reset = None
            self._update_reset_menu()
            self.refresh()

    def _connection_issue_label(self) -> Label:
        return self.reset_compact if self.five_section.winfo_manager() else self.week_reset

    def _set_five_hour_visible(self, visible: bool) -> None:
        if visible == bool(self.five_section.winfo_manager()):
            return
        if visible:
            self.five_section.pack(side="left", before=self.week_title.master)
            self._fit_content_width()
        else:
            self.five_section.pack_forget()
            self._fit_content_width(min_width=0)

    def _update_time_labels(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        five_hour, weekly = snapshot.metric_windows
        self.reset_compact.configure(
            text=reset_short(five_hour.resets_at if five_hour else None),
            fg=MUTED,
        )
        self.week_reset.configure(
            text=reset_short(weekly.resets_at if weekly else None, weekly=True),
            fg=MUTED,
        )

    def _render(self, snapshot: UsageSnapshot) -> None:
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

        self.menu.entryconfigure(self.account_index, label=f"계정: {snapshot.account or '—'}")
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
        self.menu.entryconfigure(
            self.plan_index,
            label=f"요금제: {plan_text}",
            right_text=f"크레딧: {credit_text}",
        )
        self._update_reset_menu()


def main() -> None:
    run_single_instance(
        r"Local\CodexUsageMiniWidget",
        "Codex 사용량 위젯",
        lambda: UsageWidget().run(),
    )


if __name__ == "__main__":
    main()
