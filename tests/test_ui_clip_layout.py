"""裁切頁版面 (側欄工作台，風格統一 C1 段)：標題列、三張編號卡片、邊界來源切換、閉鎖容許在最下方、
底部動作列固定在卡片下面、按鈕只有「開始裁切」是主色 (步驟 5-7、8-1 的規則延續)。"""

from __future__ import annotations

import unittest
from typing import Any, List

import customtkinter as ctk

from tests._support import IsolatedWorkspaceTestCase
from ui.segmented import SegmentedControl
from ui.tab_clip_workflow import BOUNDARY_MODE_FILE, BOUNDARY_MODE_PASTE, CLIP_KIND_DEM
from ui.theme_constants import BTN_PRIMARY, BTN_SECONDARY, FONT_SIZE_PAGE_TITLE

PASTE_MODE = "貼座標"
FILE_MODE = "載入邊界檔"


def widgets_in(widget: Any, kind: type) -> List[Any]:
    """遞迴找出 widget 底下所有指定型別的元件 (分段選擇內部的按鈕不算)。"""
    found: List[Any] = []
    for child in widget.winfo_children():
        if isinstance(child, (ctk.CTkSegmentedButton, SegmentedControl)):
            continue
        if isinstance(child, kind):
            found.append(child)
        found.extend(widgets_in(child, kind))
    return found


def colour(value: Any) -> str:
    return str(tuple(value)) if isinstance(value, (list, tuple)) else str(value)


class TestClipTabLayout(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.tab = self.app.tab_clip
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def test_header_has_title_description_and_srs_menus(self) -> None:
        tab = self.tab
        self.assertEqual(tab.lbl_page_title.cget("text"), CLIP_KIND_DEM.tab_title)
        self.assertEqual(tab.lbl_page_title.cget("font").cget("size"), FONT_SIZE_PAGE_TITLE)
        self.assertEqual(tab.lbl_description.cget("text"), CLIP_KIND_DEM.description)
        self.assertIs(tab.opt_source_srs.master, tab.header)
        self.assertIs(tab.opt_output_srs.master, tab.header)
        self.assertEqual(int(tab.header.grid_info()["row"]), 0)

    def test_boundary_mode_switch_has_file_mode_first(self) -> None:
        self.assertEqual(widgets_in(self.tab, ctk.CTkTabview), [])
        self.assertEqual((BOUNDARY_MODE_FILE, BOUNDARY_MODE_PASTE), (FILE_MODE, PASTE_MODE))
        self.assertEqual(self.tab.seg_boundary_mode.cget("values"), [FILE_MODE, PASTE_MODE])

    def test_three_numbered_cards_in_order(self) -> None:
        tab = self.tab
        cards = [tab.card_files, tab.card_boundary, tab.card_output]
        self.assertEqual([int(card.grid_info()["row"]) for card in cards], [0, 1, 2])
        for card in cards:
            self.assertIs(card.master, tab.scroll_area)
        self.assertIs(tab.file_list.master, tab.card_files)
        labels = [w.cget("text") for w in widgets_in(tab.scroll_area, ctk.CTkLabel)]
        for text in ("1", "原始檔", "2", "裁切邊界", "3", "輸出選項"):
            self.assertIn(text, labels)

    def test_boundary_mode_switch_shows_one_frame_and_keeps_input(self) -> None:
        tab = self.tab
        paste_frame = tab.txt_coords.master
        file_frame = tab.entry_bound_file.master
        self.assertEqual(tab.seg_boundary_mode.get(), FILE_MODE)
        self.assertEqual(file_frame.winfo_manager(), "grid")
        self.assertEqual(paste_frame.winfo_manager(), "")
        # 閉鎖容許放在卡片最下方，只跟著載入檔案顯示
        self.assertEqual(tab.frame_snap.winfo_manager(), "grid")
        self.assertGreater(int(tab.frame_snap.grid_info()["row"]), int(tab.entry_buffer.master.grid_info()["row"]))

        tab.txt_coords.delete("1.0", "end")
        tab.txt_coords.insert("1.0", "1 2\n3 4\n5 6\n")
        # 等同使用者點切換鈕
        tab.seg_boundary_mode.set(PASTE_MODE, from_button_callback=True)
        self.app.update()
        self.assertEqual(paste_frame.winfo_manager(), "grid")
        self.assertEqual(file_frame.winfo_manager(), "")
        self.assertEqual(tab.frame_snap.winfo_manager(), "")

        tab._show_boundary_mode(FILE_MODE)
        self.app.update()
        self.assertEqual(tab.seg_boundary_mode.get(), FILE_MODE)
        self.assertEqual(file_frame.winfo_manager(), "grid")
        self.assertEqual(paste_frame.winfo_manager(), "")
        self.assertEqual(tab.txt_coords.get("1.0", "end").strip(), "1 2\n3 4\n5 6")

        with self.assertRaises(KeyError):
            tab._show_boundary_mode("不存在的來源")

    def test_segmented_control_marks_the_selected_option(self) -> None:
        seg = self.tab.seg_boundary_mode
        self.assertEqual(colour(seg.buttons[FILE_MODE].cget("text_color")), colour(BTN_PRIMARY["fg_color"]))
        self.assertEqual(seg.buttons[FILE_MODE].cget("font").cget("weight"), "bold")
        self.assertEqual(seg.buttons[PASTE_MODE].cget("font").cget("weight"), "normal")

    def test_action_bar_is_fixed_below_cards(self) -> None:
        tab = self.tab
        self.assertEqual(int(tab.scroll_area.grid_info()["row"]), 1)
        self.assertEqual(int(tab.action_bar.grid_info()["row"]), 2)
        self.assertEqual(tab.grid_rowconfigure(1)["weight"], 1)
        self.assertEqual(tab.grid_rowconfigure(2)["weight"], 0)
        # 輸出位置與主要按鈕在動作列，不在可捲動的卡片區
        self.assertIs(tab.btn_execute, tab.action_bar.btn_primary)
        self.assertIs(tab.entry_out_dir, tab.action_bar.entry_out_dir)
        self.assertNotIn(tab.btn_execute, widgets_in(tab.scroll_area, ctk.CTkButton))
        self.assertNotIn(tab.entry_out_dir, widgets_in(tab.scroll_area, ctk.CTkEntry))
        self.assertEqual(widgets_in(tab.action_bar, ctk.CTkOptionMenu), [])

    def test_only_execute_button_is_primary(self) -> None:
        tab = self.tab
        # 條件不夠時「開始裁切」是停用的 bg 底；條件夠了才是主色
        tab.action_bar.set_ready("可以開始：1 個檔案", ok=True)
        allowed = {colour(BTN_PRIMARY["fg_color"]), colour(BTN_SECONDARY["fg_color"])}
        buttons = widgets_in(tab.scroll_area, ctk.CTkButton) + widgets_in(tab.action_bar, ctk.CTkButton)
        # 卡片 1：加入檔案、加入資料夾、移除所選、清空清單；卡片 2：產生邊界、極角排序、清空、瀏覽、外推；
        # 動作列：瀏覽、開啟資料夾、開始裁切、取消 (執行中才顯示)
        self.assertEqual(len(buttons), 13)
        primary = []
        secondary_texts = []
        for button in buttons:
            fg = colour(button.cget("fg_color"))
            self.assertIn(fg, allowed, f"按鈕「{button.cget('text')}」沒有套用語意色")
            if fg == colour(BTN_PRIMARY["fg_color"]):
                primary.append(button)
            else:
                secondary_texts.append(button.cget("text"))
        self.assertEqual(primary, [tab.btn_execute])
        # 移除、清空也是次要按鈕 (系列裡沒有紅色的危險按鈕)
        for text in ("清空", "移除所選", "清空清單", "取消"):
            self.assertIn(text, secondary_texts)

    def test_inspector_moves_into_the_active_tool(self) -> None:
        ins = self.app.inspector_panel
        self.assertEqual(str(ins.grid_info()["in"]), str(self.app.tab_dem.inspector_slot))
        self.app.select_tool("shp")
        self.app.update()
        self.assertEqual(str(ins.grid_info()["in"]), str(self.app.tab_shp.inspector_slot))
        self.assertEqual(self.app.tab_dem.winfo_manager(), "")
        self.assertEqual(self.app.tab_shp.winfo_manager(), "grid")
        self.assertTrue(ins.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
