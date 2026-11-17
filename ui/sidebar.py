"""側欄 (ui/sidebar.py)：視窗左邊固定寬度的一欄，切換工具；照 WingLight 規範的 Sidebar 元件。

- 寬 208 px、chrome 底；最上面產品名，分組小標 (overline、muted)，下面是工具項目。
- 項目：18 px 線條圖示 (muted) ＋ 名稱；滑鼠移過 hover 底；選中 panel 底、accent 粗體字、圖示也變 accent。
- 最下面：深淺色切換 (太陽＝淺色、月亮＝深色) 與「關於」，用 muted 字。
- 可以整組藏起來的一區 (add_section)：測試版工具的分組與項目，開關打開才顯示。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import customtkinter as ctk

from ui.app_info import APP_NAME
from ui.icons import icon
from ui.theme_constants import FONT_FAMILY, FONT_SIZE_BODY, FONT_SIZE_OVERLINE, RADIUS_MD, T

SIDEBAR_WIDTH: int = 208
ITEM_HEIGHT: int = 38
ITEM_ICON_SIZE: int = 18


class Sidebar(ctk.CTkFrame):
    def __init__(self, master: Any, **kwargs: Any) -> None:
        kwargs.setdefault("width", SIDEBAR_WIDTH)
        kwargs.setdefault("corner_radius", 0)
        kwargs.setdefault("fg_color", T["chrome"])
        super().__init__(master, **kwargs)
        # 寬度固定，不隨內容或視窗縮放
        self.pack_propagate(False)
        self._font = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self._font_strong = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY, weight="bold")
        self._font_overline = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_OVERLINE, weight="bold")

        self.top = ctk.CTkFrame(self, fg_color="transparent")
        self.top.pack(side="top", fill="x", padx=8, pady=(12, 0))
        self.bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 12))

        self.lbl_product = ctk.CTkLabel(self.top, text=APP_NAME, font=self._font_strong, anchor="w", compound="left")
        self.lbl_product.pack(fill="x", padx=10, pady=(0, 12))

        self.items: Dict[str, ctk.CTkButton] = {}
        self._item_icons: Dict[str, str] = {}
        self.selected: Optional[str] = None

    def add_section(self) -> ctk.CTkFrame:
        """在上方再加一區，之後 add_group／add_item 給 section= 就放進這一區；整區可以用 set_section_visible 藏起來。"""
        section = ctk.CTkFrame(self.top, fg_color="transparent", corner_radius=0)
        section.pack(fill="x")
        return section

    def set_section_visible(self, section: ctk.CTkFrame, visible: bool) -> None:
        if visible:
            section.pack(fill="x")
        else:
            section.pack_forget()

    def add_group(self, title: str, section: Optional[ctk.CTkFrame] = None) -> ctk.CTkLabel:
        label = ctk.CTkLabel(self.top if section is None else section, text=title, font=self._font_overline, text_color=T["muted"], anchor="w")
        label.pack(fill="x", padx=10, pady=(4, 6))
        return label

    def _make_item(self, parent: Any, text: str, icon_name: str, command: Callable[[], None], *, muted: bool) -> ctk.CTkButton:
        button = ctk.CTkButton(
            parent,
            text=text,
            image=icon(icon_name, role="muted", size=ITEM_ICON_SIZE),
            compound="left",
            anchor="w",
            height=ITEM_HEIGHT,
            corner_radius=RADIUS_MD,
            border_width=0,
            fg_color="transparent",
            hover_color=T["hover"],
            text_color=T["muted"] if muted else T["text"],
            font=self._font,
            command=command,
        )
        button.pack(fill="x", pady=(0, 4))
        return button

    def add_item(
        self, key: str, text: str, icon_name: str, command: Callable[[], None], section: Optional[ctk.CTkFrame] = None
    ) -> ctk.CTkButton:
        """加一個工具項目 (上方，或 add_section 加的那一區)。"""
        button = self._make_item(self.top if section is None else section, text, icon_name, command, muted=False)
        self.items[key] = button
        self._item_icons[key] = icon_name
        return button

    def add_bottom_item(self, text: str, icon_name: str, command: Callable[[], None]) -> ctk.CTkButton:
        """加一個設定類項目 (最下面那一區，muted 字)；照加入的順序由上往下排。"""
        return self._make_item(self.bottom, text, icon_name, command, muted=True)

    def select(self, key: str) -> None:
        if key not in self.items:
            raise KeyError(f"側欄沒有這個工具: {key}")
        self.selected = key
        for name, button in self.items.items():
            chosen = name == key
            button.configure(
                fg_color=T["panel"] if chosen else "transparent",
                hover_color=T["panel"] if chosen else T["hover"],
                text_color=T["accent"] if chosen else T["text"],
                font=self._font_strong if chosen else self._font,
                image=icon(self._item_icons[name], role="accent" if chosen else "muted", size=ITEM_ICON_SIZE),
            )
