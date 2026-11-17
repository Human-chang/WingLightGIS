"""分段選擇 (ui/segmented.py)：兩三個互斥選項的切換鈕，照 WingLight 規範 Button 元件的「分段選擇」。

軌道 input 填色、1px line 框、8px 圓角、內距 2px；選項 6px 圓角、muted 字；選中的那一格 panel 填色、accent 粗體字。
CustomTkinter 的 CTkSegmentedButton 選中時只能換填色、不能換字色與粗細，所以自己組。
介面與 CTkSegmentedButton 相同的部分：values、get()、set(value, from_button_callback=True 時呼叫 command)。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

import customtkinter as ctk

from ui.theme_constants import FONT_FAMILY, FONT_SIZE_BODY, RADIUS_MD, RADIUS_SM, T


class SegmentedControl(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        *,
        values: Sequence[str],
        command: Optional[Callable[[str], None]] = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("fg_color", T["input"])
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", T["line"])
        kwargs.setdefault("corner_radius", RADIUS_MD)
        super().__init__(master, **kwargs)
        self._values: List[str] = list(values)
        self._command = command
        self._current: str = ""
        self._font = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self._font_selected = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY, weight="bold")
        self.buttons: Dict[str, ctk.CTkButton] = {}
        for column, value in enumerate(self._values):
            button = ctk.CTkButton(
                self,
                text=value,
                height=26,
                corner_radius=RADIUS_SM,
                border_width=0,
                fg_color=T["input"],
                hover_color=T["hover"],
                text_color=T["muted"],
                font=self._font,
                command=lambda v=value: self.set(v, from_button_callback=True),
            )
            button.grid(row=0, column=column, padx=(3 if column == 0 else 1, 3 if column == len(self._values) - 1 else 1), pady=3, sticky="ew")
            self.grid_columnconfigure(column, weight=1)
            self.buttons[value] = button
        if self._values:
            self.set(self._values[0])

    def get(self) -> str:
        return self._current

    def set(self, value: str, from_button_callback: bool = False) -> None:
        if value not in self.buttons:
            raise KeyError(f"沒有這個選項: {value}")
        self._current = value
        for name, button in self.buttons.items():
            selected = name == value
            button.configure(
                fg_color=T["panel"] if selected else T["input"],
                hover_color=T["panel"] if selected else T["hover"],
                text_color=T["accent"] if selected else T["muted"],
                font=self._font_selected if selected else self._font,
            )
        if from_button_callback and self._command is not None:
            self._command(value)

    def cget(self, attribute_name: str) -> Any:
        if attribute_name == "values":
            return list(self._values)
        return super().cget(attribute_name)
