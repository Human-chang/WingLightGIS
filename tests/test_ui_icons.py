"""線條圖示：產生器 (tools/make_icons.py) 與執行時載入 (ui/icons.py) 的測試 (風格統一 B 段)。"""

from __future__ import annotations

import importlib.util
import math
import unittest

import customtkinter as ctk
from PIL import Image, ImageChops

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui import icons
from ui.theme_constants import T

_spec = importlib.util.spec_from_file_location("make_icons", PROJECT_ROOT / "tools" / "make_icons.py")
make_icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_icons)

# 規範 Icons 群組的 42 個圖示
SPEC_ICONS = {
    "folder", "folder-open", "file", "file-text", "search", "refresh", "play", "star", "dem", "image", "shape",
    "crop", "layers", "pin", "ruler", "table", "slides", "camera", "video", "package", "pushpin", "paperclip",
    "computer", "globe", "clock", "history", "calendar", "copy", "save", "trash", "external", "info", "warning",
    "check", "close", "plus", "more", "chevron-down", "sun", "moon", "bolt", "hourglass",
}


class TestIconGenerator(unittest.TestCase):
    def test_all_spec_icons_have_source_svg(self) -> None:
        self.assertEqual(set(make_icons.icon_names()), SPEC_ICONS)

    def test_parse_lines_and_relative_moves(self) -> None:
        [(points, closed)] = make_icons.parse_path("M5 12l5 5 9-10")
        self.assertEqual(points, [(5.0, 12.0), (10.0, 17.0), (19.0, 7.0)])
        self.assertFalse(closed)
        subpaths = make_icons.parse_path("M4 5h16v14H4zM4 10h16")
        self.assertEqual(len(subpaths), 2)
        self.assertEqual(subpaths[0], ([(4.0, 5.0), (20.0, 5.0), (20.0, 19.0), (4.0, 19.0)], True))
        self.assertEqual(subpaths[1][0], [(4.0, 10.0), (20.0, 10.0)])

    def test_parse_arcs_make_a_circle(self) -> None:
        # search.svg 的圓：圓心 (11, 11)、半徑 7，兩段半圓
        points, closed = make_icons.parse_path("M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14z")[0]
        self.assertTrue(closed)
        for x, y in points:
            self.assertAlmostEqual(math.hypot(x - 11, y - 11), 7.0, places=6)
        self.assertGreater(min(x for x, _ in points), 3.99)
        self.assertLess(max(x for x, _ in points), 18.01)

    def test_committed_pngs_match_the_svg_sources(self) -> None:
        # assets/icons/ 要跟 assets/icons_src/ 同步：改了 SVG 或產生器，要重跑 tools/make_icons.py
        expected = set()
        for name in make_icons.icon_names():
            subpaths = make_icons.load_icon_paths(make_icons.SOURCE_DIR / f"{name}.svg")
            for filled in ([False, True] if name in make_icons.FILLED_ICONS else [False]):
                for size in make_icons.SIZES:
                    file_name = make_icons.output_name(name, size, filled=filled)
                    expected.add(file_name)
                    with Image.open(make_icons.OUTPUT_DIR / file_name) as saved:
                        fresh = make_icons.render_mask(subpaths, size, filled=filled)
                        self.assertIsNone(ImageChops.difference(saved.convert("L"), fresh).getbbox(), file_name)
        self.assertEqual({p.name for p in make_icons.OUTPUT_DIR.glob("*.png")}, expected)

    def test_masks_are_drawn_where_the_strokes_are(self) -> None:
        mask = make_icons.render_mask(make_icons.parse_path("M12 5v14M5 12h14"), 24)
        self.assertEqual(mask.getpixel((12, 12)), 255)  # 十字中心
        self.assertEqual(mask.getpixel((2, 2)), 0)  # 角落空白
        filled = make_icons.render_mask(make_icons.load_icon_paths(make_icons.SOURCE_DIR / "play.svg"), 24, filled=True)
        hollow = make_icons.render_mask(make_icons.load_icon_paths(make_icons.SOURCE_DIR / "play.svg"), 24)
        self.assertEqual(filled.getpixel((11, 12)), 255)
        self.assertLess(hollow.getpixel((11, 12)), 64)


class TestIconLoader(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def test_source_size_follows_screen_scaling(self) -> None:
        self.assertEqual([icons.source_size_for(px) for px in (16, 18, 20, 24, 27, 32, 40)], [16, 20, 20, 24, 32, 32, 32])
        self.assertEqual(icons.icon("folder", size=16, scaling=1.5).cget("light_image").size, (24, 24))
        self.assertEqual(icons.icon("folder", size=16, scaling=1.0).cget("size"), (16, 16))

    def test_colours_come_from_the_tokens(self) -> None:
        image = icons.icon("check", role="ok", size=16, scaling=2.0)
        for theme, key in ((0, "light_image"), (1, "dark_image")):
            pixels = image.cget(key)
            opaque = [pixels.getpixel((x, y)) for x in range(32) for y in range(32) if pixels.getpixel((x, y))[3] == 255]
            self.assertTrue(opaque)
            expected = tuple(int(T["ok"][theme][i:i + 2], 16) for i in (1, 3, 5))
            self.assertEqual({p[:3] for p in opaque}, {expected})

    def test_same_icon_is_shared_and_bad_names_fail_clearly(self) -> None:
        self.assertIs(icons.icon("plus"), icons.icon("plus"))
        self.assertIsNot(icons.icon("plus"), icons.icon("plus", role="muted"))
        with self.assertRaises(FileNotFoundError):
            icons.icon("不存在的圖示")
        with self.assertRaises(ValueError):
            icons.icon("plus", role="紅色")

    def test_buttons_and_status_use_icons(self) -> None:
        tab, ins = self.app.tab_dem, self.app.inspector_panel
        # 清單還是空的：開始裁切停用，圖示是 disabled 色；條件夠了才是 on-accent
        self.assertIs(tab.btn_execute.cget("image"), icons.icon("play", role="disabled", filled=True))
        tab.action_bar.set_ready("可以開始：1 個檔案", ok=True)
        self.assertIs(tab.btn_execute.cget("image"), icons.icon("play", role="on-accent", filled=True))
        self.assertIs(ins.lbl_status_badge.cget("image"), icons.icon("check", role="ok"))
        ins._set_status_badge("提醒：沒有座標系標籤", "warn")
        self.assertIs(ins.lbl_status_badge.cget("image"), icons.icon("warning", role="warn"))
        self.assertIs(ins.btn_browser_map.cget("image"), icons.icon("globe"))

    def test_disabled_buttons_dim_their_icons(self) -> None:
        tab, ins = self.app.tab_dem, self.app.inspector_panel
        # 四角座標還沒有內容時「複製」是停用的
        self.assertEqual(ins.btn_copy_corners.cget("state"), "disabled")
        self.assertIs(ins.btn_copy_corners.cget("image"), icons.icon("copy", role="disabled"))
        tab.action_bar.btn_cancel.configure(state="disabled")
        self.assertIs(tab.action_bar.btn_cancel.cget("image"), icons.icon("close", role="disabled"))
        tab.btn_execute.configure(state="disabled", text="正在裁切")
        self.assertIs(tab.btn_execute.cget("image"), icons.icon("play", role="disabled", filled=True))
        tab.btn_execute.configure(state="normal", text="開始裁切")
        self.assertIs(tab.btn_execute.cget("image"), icons.icon("play", role="on-accent", filled=True))


class TestIconCacheFollowsWindow(IsolatedWorkspaceTestCase):
    def test_new_window_gets_new_images(self) -> None:
        # 主視窗關掉後再開一個，不能沿用屬於舊視窗的圖片 (否則 Tk 會說 image doesn't exist)
        import ui.dashboard_app as dash

        first = dash.GisMasterToolkitApp()
        first.update()
        old = icons.icon("trash")
        first.destroy()
        second = dash.GisMasterToolkitApp()
        try:
            second.update()
            self.assertIsNot(icons.icon("trash"), old)
            ctk.CTkButton(second, text="清除", image=icons.icon("trash")).grid()
            second.update()
        finally:
            second.destroy()


if __name__ == "__main__":
    unittest.main()
