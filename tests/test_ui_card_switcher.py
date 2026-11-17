"""色彩規範與單卡切換器的測試。"""

from __future__ import annotations

import unittest
from typing import Any, List

import customtkinter as ctk

from tests._support import IsolatedWorkspaceTestCase
from ui import theme_constants
from ui.theme_constants import BTN_PRIMARY, BTN_SECONDARY, T
from ui.lab.tool_switcher import ToolCardSwitcher

VECTOR_TOOLS = ["地籍交集清冊", "DXF分層與校正", "多檔合併", "亂碼與抽稀", "CSV轉點", "屬性拆檔"]
# 「坡度與等高線」跟 DEM／DSM 裁切重複，不在切換列 (卡片照樣建立、不顯示)
TERRAIN_TOOLS = ["兩期土方計算", "水保坡度分類", "斷面高程擷取", "集水區劃設"]


def buttons_in(widget: Any) -> List[ctk.CTkButton]:
    """遞迴找出 widget 底下所有 CTkButton。"""
    found: List[ctk.CTkButton] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            found.append(child)
        else:
            found.extend(buttons_in(child))
    return found


class TestThemeConstants(unittest.TestCase):
    def test_two_button_styles_follow_spec(self) -> None:
        # 規範 Button：主要按鈕 accent 填色、on-accent 字；次要按鈕 input 填色、1px line 框；8px 圓角
        self.assertEqual(BTN_PRIMARY["fg_color"], ("#2B55D4", "#8FA8FF"))
        self.assertEqual(BTN_PRIMARY["hover_color"], ("#2347B8", "#A9BCFF"))
        self.assertEqual(BTN_PRIMARY["text_color"], ("#FFFFFF", "#10142B"))
        self.assertEqual(BTN_PRIMARY["height"], 36)
        self.assertEqual(BTN_SECONDARY["fg_color"], T["input"])
        self.assertEqual(BTN_SECONDARY["hover_color"], T["hover"])
        self.assertEqual(BTN_SECONDARY["border_color"], T["line"])
        self.assertEqual(BTN_SECONDARY["border_width"], 1)
        for style in (BTN_PRIMARY, BTN_SECONDARY):
            self.assertEqual(style["corner_radius"], 8)
        # 系列裡沒有紅色的危險按鈕
        self.assertFalse(hasattr(theme_constants, "BTN_SUBTLE_DANGER"))


class TestCardSwitcher(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp()
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _assert_only_shown(self, switcher: ToolCardSwitcher, tool_name: str) -> None:
        self.assertEqual(switcher.current, tool_name)
        self.assertEqual(switcher.button.get(), tool_name)
        for name in switcher.tool_names:
            for card in switcher.cards_of(name):
                expected = "grid" if name == tool_name else ""
                self.assertEqual(card.winfo_manager(), expected, f"切到「{tool_name}」時「{name}」的卡片顯示狀態不對")

    def test_vector_tab_shows_one_tool_at_a_time(self) -> None:
        switcher = self.app.tab_vector.tool_switcher
        self.assertEqual(switcher.tool_names, VECTOR_TOOLS)
        self.assertEqual(switcher.button.cget("values"), VECTOR_TOOLS)
        self.assertEqual(switcher.button.grid_info()["row"], 1)
        self._assert_only_shown(switcher, VECTOR_TOOLS[0])
        for name in VECTOR_TOOLS:
            self.assertGreaterEqual(len(switcher.cards_of(name)), 1, f"「{name}」沒有掛任何卡片")
            switcher.show(name)
            self.app.update()
            self._assert_only_shown(switcher, name)

    def test_dxf_tool_stacks_split_and_fix_cards(self) -> None:
        tab = self.app.tab_vector
        switcher = tab.tool_switcher
        cards = switcher.cards_of("DXF分層與校正")
        self.assertEqual(len(cards), 2)
        switcher.show("DXF分層與校正")
        self.app.update()
        self.assertEqual([card.grid_info()["row"] for card in cards], [2, 3])
        self.assertEqual(tab.entry_cad_fix_file.master, cards[1])

    def test_terrain_tab_shows_one_tool_at_a_time(self) -> None:
        switcher = self.app.tab_terrain.tool_switcher
        self.assertEqual(switcher.tool_names, TERRAIN_TOOLS)
        self._assert_only_shown(switcher, TERRAIN_TOOLS[0])
        for name in TERRAIN_TOOLS:
            self.assertEqual(len(switcher.cards_of(name)), 1)
            switcher.show(name)
            self.app.update()
            self._assert_only_shown(switcher, name)
            self.assertEqual(self.app.tab_terrain.card_slope_contours.winfo_manager(), "")

    def test_clicking_segmented_button_switches_card(self) -> None:
        switcher = self.app.tab_terrain.tool_switcher
        # 等同使用者點切換鈕：CTkSegmentedButton 點擊時會 set 後呼叫 command
        switcher.button.set(TERRAIN_TOOLS[3], from_button_callback=True)
        self.app.update()
        self._assert_only_shown(switcher, TERRAIN_TOOLS[3])

    def test_hidden_cards_keep_their_input(self) -> None:
        tab = self.app.tab_vector
        tab._set_entry(tab.entry_csv_file, "D:/data/points.csv")
        tab.tool_switcher.show("屬性拆檔")
        self.app.update()
        tab.tool_switcher.show("CSV轉點")
        self.app.update()
        self.assertEqual(tab.entry_csv_file.get(), "D:/data/points.csv")

    def test_unknown_tool_name_is_rejected(self) -> None:
        switcher = self.app.tab_vector.tool_switcher
        with self.assertRaises(KeyError):
            switcher.show("不存在的工具")
        with self.assertRaises(KeyError):
            switcher.register("不存在的工具", ctk.CTkFrame(self.app.tab_vector))
        self.assertEqual(switcher.current, VECTOR_TOOLS[0])

    def test_card_buttons_use_only_semantic_colours(self) -> None:
        allowed = {
            (str(BTN_PRIMARY["fg_color"]), str(BTN_PRIMARY["hover_color"])),
            (str(BTN_SECONDARY["fg_color"]), str(BTN_SECONDARY["hover_color"])),
        }
        for tab in (self.app.tab_vector, self.app.tab_terrain):
            switcher = tab.tool_switcher
            for name in switcher.tool_names:
                primary_count = 0
                for card in switcher.cards_of(name):
                    for button in buttons_in(card):
                        colours = (str(tuple(button.cget("fg_color")) if isinstance(button.cget("fg_color"), (list, tuple)) else button.cget("fg_color")),
                                   str(tuple(button.cget("hover_color")) if isinstance(button.cget("hover_color"), (list, tuple)) else button.cget("hover_color")))
                        self.assertIn(colours, allowed, f"「{name}」的按鈕「{button.cget('text')}」沒有套用語意色")
                        if colours[0] == str(BTN_PRIMARY["fg_color"]):
                            primary_count += 1
                self.assertGreaterEqual(primary_count, 1, f"「{name}」沒有主執行鈕")


if __name__ == "__main__":
    unittest.main()
