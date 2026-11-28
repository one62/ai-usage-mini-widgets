"""Custom Tk popup menu supporting info rows, checkbuttons, actions, and scales."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from common.formatting import FONT_FAMILY, MUTED, TEXT
from common.win32 import clamp_position, monitor_work_area


class PopupMenu:
    """Tk popup with inert information labels and selectable action rows."""

    def __init__(
        self,
        root: tk.Tk,
        on_close: Callable[[], None],
        *,
        reserve_indicator_space: bool = False,
    ) -> None:
        self.root = root
        self.on_close = on_close
        self.reserve_indicator_space = reserve_indicator_space
        self.font = tkfont.nametofont("TkMenuFont", root=root).copy()
        self.font.configure(family=FONT_FAMILY)
        self.background = "#2A3032"
        self.active_background = "#3A4345"
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.body = tk.Frame(
            self.window,
            bg=self.background,
            highlightbackground="#56525F",
            highlightthickness=1,
        )
        self.body.pack(fill="both", expand=True)
        self.entries: list[dict] = []
        self.selected: int | None = None
        self.open = False
        self.window.bind("<Escape>", self._escape)
        self.window.bind("<Up>", lambda event: self._navigate(-1))
        self.window.bind("<Down>", lambda event: self._navigate(1))
        self.window.bind("<Return>", self._activate_selected)
        self.window.bind("<space>", self._activate_selected)
        self.window.bind("<Left>", lambda event: self._adjust_scale(-1))
        self.window.bind("<Right>", lambda event: self._adjust_scale(1))
        self.window.bind("<Home>", lambda event: self._adjust_scale(endpoint="min"))
        self.window.bind("<End>", lambda event: self._adjust_scale(endpoint="max"))
        self.window.bind("<FocusOut>", self._focus_out)

    def _add_row(
        self,
        kind: str,
        label: str,
        state: str = "normal",
        command=None,
        variable: tk.BooleanVar | None = None,
        right_text: str | None = None,
    ) -> None:
        index = len(self.entries)
        row = tk.Frame(self.body, bg=self.background, bd=0)
        row.pack(fill="x")
        indicator = None
        if self.reserve_indicator_space or kind == "check":
            indicator = tk.Canvas(
                row,
                width=18,
                height=18,
                bg=self.background,
                bd=0,
                highlightthickness=0,
            )
            indicator.pack(side="left", padx=(12, 6))
        right_widget = None
        if right_text is not None:
            right_widget = tk.Label(
                row,
                anchor="e",
                padx=0,
                pady=6,
                bg=self.background,
                bd=0,
                font=self.font,
            )
            right_widget.pack(side="right", padx=(0, 12))
        text_widget = tk.Label(
            row,
            anchor="w",
            padx=0,
            pady=6,
            bg=self.background,
            bd=0,
            font=self.font,
        )
        text_widget.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0 if indicator else 12, 12),
        )
        self.entries.append(
            dict(
                kind=kind,
                label=label,
                state=state,
                command=command,
                variable=variable,
                widget=row,
                text_widget=text_widget,
                indicator=indicator,
                right_widget=right_widget,
                right_text=right_text,
            )
        )
        if kind != "info":
            for target in (row, indicator, text_widget, right_widget):
                if target is not None:
                    target.bind("<Enter>", lambda event, i=index: self._select(i))
                    target.bind("<Leave>", lambda event, i=index: self._leave_row(i, event))
                    target.bind("<ButtonRelease-1>", lambda event, i=index: self._release_row(i, event))
        # Information labels have no mouse, keyboard, or command bindings.
        self._render_row(index)

    def add_command(self, *, label: str, command, state: str = "normal") -> None:
        self._add_row("command", label, state, command)

    def add_info(self, *, label: str, right_text: str | None = None) -> None:
        self._add_row("info", label, "disabled", right_text=right_text)

    def add_checkbutton(self, *, label: str, variable: tk.BooleanVar, command) -> None:
        self._add_row("check", label, command=command, variable=variable)

    def add_scale(
        self,
        *,
        label: str,
        variable: tk.IntVar,
        command,
        from_: int = 0,
        to: int = 100,
        resolution: int = 1,
        format_value: Callable[[int], str] | None = None,
        max_value_text: str = "100%",
    ) -> None:
        index = len(self.entries)
        formatter = format_value or (lambda value: f"{value}%")
        row = tk.Frame(self.body, bg=self.background)
        row.pack(fill="x", padx=12, pady=4)
        caption = tk.Frame(row, bg=self.background)
        caption.pack(fill="x")
        text_widget = tk.Label(
            caption,
            text=label,
            anchor="w",
            bg=self.background,
            fg=TEXT,
            font=self.font,
        )
        text_widget.pack(side="left")
        font = self.font
        value_box = tk.Frame(
            caption,
            bg=self.background,
            width=font.measure(max_value_text) + 4,
            height=font.metrics("linespace") + 4,
        )
        value_box.pack(side="right")
        value_box.pack_propagate(False)
        value_widget = tk.Label(
            value_box,
            text=formatter(variable.get()),
            anchor="e",
            bg=self.background,
            fg=TEXT,
            font=font,
        )
        value_widget.pack(fill="both", expand=True)
        scale = tk.Scale(
            row,
            from_=from_,
            to=to,
            resolution=resolution,
            orient="horizontal",
            variable=variable,
            showvalue=False,
            length=210,
            width=12,
            sliderlength=16,
            bd=0,
            highlightthickness=0,
            takefocus=True,
            bg=self.background,
            troughcolor="#424149",
            activebackground="#8CD9F5",
            command=lambda value, i=index: self._scale_changed(i, value),
        )
        scale.pack(fill="x")
        self.entries.append(
            dict(
                kind="scale",
                label=label,
                state="normal",
                command=command,
                variable=variable,
                widget=row,
                text_widget=text_widget,
                value_widget=value_widget,
                scale=scale,
                font=font,
                from_=from_,
                to=to,
                resolution=resolution,
                format_value=formatter,
            )
        )
        for target in (row, caption, text_widget, value_widget, scale):
            target.bind("<Enter>", lambda event, i=index: self._select(i))
            target.bind("<Leave>", lambda event, i=index: self._leave_row(i, event))
        scale.bind("<ButtonPress-1>", lambda event, i=index: self._select(i), add="+")
        scale.bind("<FocusIn>", lambda event, i=index: self._select(i))
        scale.bind("<Up>", lambda event: self._navigate(-1))
        scale.bind("<Down>", lambda event: self._navigate(1))
        scale.bind("<Left>", lambda event: self._adjust_scale(-1))
        scale.bind("<Right>", lambda event: self._adjust_scale(1))
        scale.bind("<Home>", lambda event: self._adjust_scale(endpoint="min"))
        scale.bind("<End>", lambda event: self._adjust_scale(endpoint="max"))
        scale.bind("<Return>", self._activate_selected)
        scale.bind("<space>", self._activate_selected)

    def _scale_changed(self, index: int, value: str) -> None:
        entry = self.entries[index]
        from_ = entry["from_"]
        to = entry["to"]
        resolution = max(1, entry["resolution"])
        number = round(float(value))
        if resolution > 1:
            number = from_ + round((number - from_) / resolution) * resolution
        number = max(from_, min(to, number))
        # Keep the track fixed while dragging, including 9 -> 10 -> 100%.
        entry["value_widget"].configure(text=entry["format_value"](number))
        entry["command"](number)

    def _adjust_scale(self, delta: int = 0, *, endpoint: str | int | None = None) -> str:
        if self.selected is not None:
            entry = self.entries[self.selected]
            if entry["kind"] == "scale":
                from_ = entry["from_"]
                to = entry["to"]
                resolution = max(1, entry["resolution"])
                if endpoint == "min":
                    value = from_
                elif endpoint == "max":
                    value = to
                elif isinstance(endpoint, int):
                    value = endpoint
                else:
                    value = entry["variable"].get() + (delta * resolution)
                entry["scale"].set(max(from_, min(to, value)))
        return "break"

    def add_separator(self) -> None:
        line = tk.Frame(self.body, bg="#56525F", height=1)
        line.pack(fill="x", padx=8, pady=4)
        self.entries.append(dict(kind="separator", widget=line))

    def index(self, index: str) -> int:
        if index != "end":
            raise ValueError("Only the end index is supported")
        return len(self.entries) - 1

    def entryconfigure(self, index: int, **options) -> None:
        self.entries[index].update(options)
        if self.selected == index and not self._enabled(index):
            self.selected = None
        self._render_row(index)
        if self.open:
            self._fit_window(self.window.winfo_x(), self.window.winfo_y())

    def _enabled(self, index: int) -> bool:
        entry = self.entries[index]
        return entry["kind"] in ("command", "check", "scale") and entry["state"] == "normal"

    def _render_row(self, index: int) -> None:
        entry = self.entries[index]
        if entry["kind"] == "separator":
            return
        if entry["kind"] == "scale":
            entry["text_widget"].configure(fg=TEXT if self.selected == index else MUTED)
            entry["value_widget"].configure(text=entry["format_value"](entry["variable"].get()))
            return
        enabled = self._enabled(index)
        foreground = TEXT if enabled else MUTED
        background = self.active_background if enabled and self.selected == index else self.background
        for target in (entry["widget"], entry["text_widget"], entry["indicator"]):
            if target is not None:
                target.configure(bg=background, cursor="hand2" if enabled else "arrow")
        entry["text_widget"].configure(text=entry["label"], fg=foreground)
        if entry["right_widget"] is not None:
            entry["right_widget"].configure(
                text=entry["right_text"],
                fg=foreground,
                bg=background,
            )
        indicator = entry["indicator"]
        if indicator is not None:
            indicator.delete("all")
            if entry["kind"] == "check":
                # Draw the check instead of depending on a font's Unicode glyph.
                indicator.create_rectangle(2, 2, 15, 15, outline=foreground)
                if entry["variable"].get():
                    indicator.create_line(4, 8, 7, 11, 13, 5, fill=foreground, width=2)

    @staticmethod
    def _pointer_inside_widget(widget: tk.Widget, x_root: int, y_root: int) -> bool:
        left = widget.winfo_rootx()
        top = widget.winfo_rooty()
        width = widget.winfo_width() if widget.winfo_width() > 1 else widget.winfo_reqwidth()
        height = widget.winfo_height() if widget.winfo_height() > 1 else widget.winfo_reqheight()
        return left <= x_root < left + width and top <= y_root < top + height

    def _leave_row(self, index: int, event: tk.Event) -> None:
        if self.selected == index and not self._pointer_inside_widget(
            self.entries[index]["widget"], event.x_root, event.y_root
        ):
            self._select(None)

    def _release_row(self, index: int, event: tk.Event) -> None:
        if self._pointer_inside_widget(self.entries[index]["widget"], event.x_root, event.y_root):
            self._invoke(index)

    def _select(self, index: int | None) -> None:
        previous = self.selected
        self.selected = index if index is not None and self._enabled(index) else None
        for candidate in (previous, self.selected):
            if candidate is not None:
                self._render_row(candidate)

    def _navigate(self, step: int) -> str:
        choices = [i for i in range(len(self.entries)) if self._enabled(i)]
        if choices:
            if self.selected in choices:
                index = (choices.index(self.selected) + step) % len(choices)
            else:
                index = 0 if step > 0 else -1
            self._select(choices[index])
        return "break"

    def _activate_selected(self, event: tk.Event) -> str:
        if self.selected is not None:
            self._invoke(self.selected)
        return "break"

    def _invoke(self, index: int) -> None:
        if not self.open or not self._enabled(index):
            return
        entry = self.entries[index]
        if entry["kind"] == "scale":
            entry["scale"].focus_set()
            return
        self.close()
        if entry["kind"] == "check":
            entry["variable"].set(not entry["variable"].get())
        entry["command"]()

    def show(self, x: int, y: int) -> None:
        self._select(None)
        for index in range(len(self.entries)):
            self._render_row(index)
        self._fit_window(x, y)
        self.window.deiconify()
        self.window.lift()
        self.open = True
        self.window.focus_force()

    def _fit_window(self, x: int, y: int) -> None:
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        area = monitor_work_area(x, y, width, height)
        x, y = clamp_position(x, y, width, height, area)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        self.window.withdraw()
        self._select(None)
        self.on_close()

    def _escape(self, event: tk.Event) -> str:
        self.close()
        return "break"

    def _focus_out(self, event: tk.Event) -> None:
        self.window.after_idle(self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        if self.open:
            focused = self.window.focus_get()
            if focused is None or focused.winfo_toplevel() is not self.window:
                self.close()
