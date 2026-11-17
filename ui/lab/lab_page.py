"""測試版工具的一頁 (ui/lab/lab_page.py)

版面跟圖資快覽、裁切工具頁相同：第 0 列標題列 (工具名稱、一句說明、warn 色的測試版提醒)；
第 1 列左邊是原本的工具分頁 (自己會捲動，最上面一列來源座標系、下面一列切換工具)，右邊是檢視欄的位置。
檢視欄在這一頁只用處理紀錄與進度：切過來時體檢、四角座標與預覽清空，「在瀏覽器開啟」不接任何地圖。
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from ui.icons import icon
from ui.lab import LAB_NOTICE, LabTool
from ui.panel_inspector import INSPECTOR_WIDTH, PREVIEW_TITLE_VIEW, PanelInspector
from ui.theme_constants import FONT_FAMILY, FONT_SIZE_BODY, FONT_SIZE_PAGE_TITLE, STATUS_WARNING, T


class LabToolPage(ctk.CTkFrame):
    """側欄「測試版工具」組的一項。content 是原本的工具分頁 (TabVectorCad／TabTerrainHydro)。"""

    def __init__(self, master: Any, inspector: PanelInspector, tool: LabTool, **kwargs: Any) -> None:
        kwargs.setdefault("fg_color", T["bg"])
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)
        self.inspector = inspector
        self.tool = tool
        self.is_active: bool = False

        self.font_page_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_PAGE_TITLE, weight="bold")
        self.font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self.content = tool.factory(self, inspector, fg_color="transparent", corner_radius=0)
        self.content.grid(row=1, column=0, sticky="nsew", padx=(16, 0), pady=(0, 16))
        self.inspector_slot = ctk.CTkFrame(self, fg_color="transparent", width=INSPECTOR_WIDTH, corner_radius=0)
        self.inspector_slot.grid(row=1, column=1, sticky="nsew", padx=(8, 24), pady=(0, 16))
        self.inspector_slot.grid_rowconfigure(0, weight=1)
        self.inspector_slot.grid_columnconfigure(0, weight=1)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(18, 12))
        header.grid_columnconfigure(0, weight=1)
        self.header = header
        self.lbl_page_title = ctk.CTkLabel(header, text=self.tool.title, font=self.font_page_title, anchor="w")
        self.lbl_page_title.grid(row=0, column=0, sticky="w")
        self.lbl_description = ctk.CTkLabel(
            header, text=self.tool.description, font=self.font_body, text_color=T["muted"], anchor="w", justify="left"
        )
        self.lbl_description.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.lbl_notice = ctk.CTkLabel(
            header,
            text=f" {LAB_NOTICE}",
            image=icon("warning", role="warn"),
            compound="left",
            font=self.font_body,
            text_color=STATUS_WARNING,
            anchor="w",
            justify="left",
        )
        self.lbl_notice.grid(row=2, column=0, sticky="w", pady=(4, 0))

    def activate(self) -> None:
        self.is_active = True
        self.inspector.grid(in_=self.inspector_slot, row=0, column=0, sticky="nsew")
        self.inspector.lift()
        self.inspector.on_use_extent = None
        self.inspector.on_open_browser_preview = None
        self.inspector.set_preview_title(PREVIEW_TITLE_VIEW)
        self.inspector.reset_view()

    def deactivate(self) -> None:
        self.is_active = False

    def set_inspector_visible(self, visible: bool) -> None:
        """視窗太窄時收起檢視欄 (工具分頁改用整個寬度)；放寬後放回來。"""
        if visible:
            self.inspector_slot.grid()
            self.content.grid_configure(padx=(16, 0))
        else:
            self.inspector_slot.grid_remove()
            self.content.grid_configure(padx=(16, 16))
