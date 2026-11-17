"""字級測試：確認介面文字字級符合規範下限。"""

from __future__ import annotations

import unittest
from typing import Any, Iterator, List, Tuple

import customtkinter as ctk

from tests._support import IsolatedWorkspaceTestCase
from ui import theme_constants

MIN_FONT_SIZE = 11


def walk(widget: Any) -> Iterator[Any]:
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


class TestFontSizes(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp()
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def test_size_constants(self) -> None:
        # WingLight 規範字級 (px；CustomTkinter 的字級數字就是像素)：
        # 工具名稱 20、卡片標題 14、內文 13、欄位名稱 12、側欄小標 11、座標 12
        tc = theme_constants
        sizes = (
            tc.FONT_SIZE_PAGE_TITLE,
            tc.FONT_SIZE_CARD_TITLE,
            tc.FONT_SIZE_BODY,
            tc.FONT_SIZE_CAPTION,
            tc.FONT_SIZE_OVERLINE,
            tc.FONT_SIZE_MONO,
        )
        self.assertEqual(sizes, (20, 14, 13, 12, 11, 12))
        self.assertGreaterEqual(min(sizes), MIN_FONT_SIZE)
        self.assertGreaterEqual(tc.FONT_SIZE_BODY, 12)

    def test_no_widget_uses_a_font_smaller_than_minimum(self) -> None:
        # 工具頁第一次用到才建立：先全部建出來，圖資快覽、三個裁切頁與測試版工具都要檢查到
        self.assertIsNotNone(self.app.tab_view)
        for key in self.app.clip_tabs:
            self.app.clip_tabs[key]
        self.assertIsNotNone(self.app.tab_vector)
        self.assertIsNotNone(self.app.tab_terrain)
        checked = 0
        too_small: List[Tuple[str, int]] = []
        for widget in walk(self.app):
            if not isinstance(widget, (ctk.CTkLabel, ctk.CTkButton, ctk.CTkEntry, ctk.CTkTextbox, ctk.CTkOptionMenu, ctk.CTkCheckBox)):
                continue
            font = widget.cget("font")
            size = font.cget("size") if isinstance(font, ctk.CTkFont) else abs(int(font[1])) if isinstance(font, tuple) else None
            if size is None:
                continue
            checked += 1
            if size < MIN_FONT_SIZE:
                text = widget.cget("text") if isinstance(widget, (ctk.CTkLabel, ctk.CTkButton)) else type(widget).__name__
                too_small.append((str(text)[:30], size))
        self.assertGreater(checked, 100, "應該檢查到三個分頁與右欄的元件")
        self.assertEqual(too_small, [])

    def test_body_and_log_fonts_use_the_constants(self) -> None:
        inspector = self.app.inspector_panel
        self.assertEqual(inspector.txt_terminal.cget("font").cget("size"), theme_constants.FONT_SIZE_MONO)
        self.assertEqual(inspector.txt_terminal.cget("font").cget("family"), theme_constants.FONT_FAMILY_MONO)
        for tab in (self.app.tab_clip, self.app.tab_vector, self.app.tab_terrain):
            self.assertEqual(tab.font_body.cget("size"), theme_constants.FONT_SIZE_BODY)
            self.assertEqual(tab.font_body.cget("family"), theme_constants.FONT_FAMILY)
            self.assertEqual(tab.font_title.cget("size"), theme_constants.FONT_SIZE_CARD_TITLE)
            self.assertEqual(tab.font_caption.cget("size"), theme_constants.FONT_SIZE_CAPTION)
            self.assertEqual(tab.font_caption.cget("weight"), "normal", "欄位名稱不用粗體")

    def test_sidebar_items_use_the_ui_font(self) -> None:
        # 主題預設的 Roboto 在 Windows 不存在，側欄項目要用微軟正黑體；選中的那一項粗體
        buttons = list(self.app.sidebar.items.values())
        # 圖資快覽、三個裁切工具、測試版工具兩項 (開關關著時藏起來)
        self.assertEqual(len(buttons), 6)
        for button in buttons:
            self.assertEqual(button.cget("font").cget("family"), theme_constants.FONT_FAMILY)
            self.assertEqual(button.cget("font").cget("size"), theme_constants.FONT_SIZE_BODY)
        self.assertEqual(self.app.sidebar.items["view"].cget("font").cget("weight"), "bold")


if __name__ == "__main__":
    unittest.main()
