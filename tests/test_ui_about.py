"""「關於」視窗 (風格統一 E 段)：側欄最下面的「關於」、視窗內容與連結、只開一個、Esc 關閉、App 圖示、
系列標誌圖與產生器 (tools/make_about.py) 同步。"""

from __future__ import annotations

import ctypes
import importlib.util
import time
import unittest
from typing import Any, Iterator, List
from unittest import mock

import customtkinter as ctk
from PIL import Image, ImageChops

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui import settings
from ui.app_info import APP_AUTHOR, APP_NAME, APP_URL, APP_VERSION
from ui.theme_constants import T

_spec = importlib.util.spec_from_file_location("make_about", PROJECT_ROOT / "tools" / "make_about.py")
make_about = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_about)

WM_GETICON, ICON_SMALL, ICON_BIG = 0x007F, 0, 1
SPEC_FONT_SIZES = {20, 14, 13, 12, 11}


def walk(widget: Any) -> Iterator[Any]:
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


class TestMarkImage(unittest.TestCase):
    def test_mark_matches_generator(self) -> None:
        # assets/about/ 要跟規範的 winglight-mark.svg 同步：改了 SVG 或產生器，要重跑 tools/make_about.py
        with Image.open(make_about.MARK_PNG) as saved:
            self.assertEqual(saved.size, (48, 48))
            diff = ImageChops.difference(saved.convert("RGBA"), make_about.render_mark())
            self.assertIsNone(diff.getbbox(alpha_only=False))

    def test_mark_is_indigo_tile_with_wings_and_glow_dot(self) -> None:
        mark = make_about.render_mark()
        scale = make_about.MARK_SIZE / 120
        # 靛藍方塊 (accent 淺色值)、右邊那片翅膀是實色白、光點在翅膀正下方 (glow)
        self.assertEqual(mark.getpixel((round(20 * scale), round(100 * scale))), (0x2B, 0x55, 0xD4, 255))
        self.assertEqual(mark.getpixel((round(84 * scale), round(40 * scale))), (255, 255, 255, 255))
        self.assertEqual(mark.getpixel((round(60 * scale), round(90 * scale))), (0xFF, 0xC8, 0x3D, 255))
        # 左邊那片比較淡 (白色 75% 疊在靛藍上)
        left_wing = mark.getpixel((round(46 * scale), round(48 * scale)))
        self.assertTrue(0x2B < left_wing[0] < 255, left_wing)
        # 右上角幾乎是直角：角落有顏色；左上角是大圓角：角落透明
        self.assertEqual(mark.getpixel((make_about.MARK_SIZE - 3, 2))[3] > 0, True)
        self.assertEqual(mark.getpixel((1, 1))[3], 0)


class TestAboutDialog(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        settings.settings_path().unlink(missing_ok=True)
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp()
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        ctk.set_appearance_mode("Dark")
        super().tearDown()

    def _open(self) -> Any:
        self.app.btn_about.invoke()
        dialog = self.app.about_window
        self.assertIsNotNone(dialog)
        # CTkToplevel 建立時先藏起來，換好標題列顏色 5 ms 後才顯示
        deadline = time.monotonic() + 2
        while not dialog.winfo_viewable() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertTrue(dialog.winfo_viewable())
        return dialog

    def _texts(self, dialog: Any) -> List[str]:
        return [w.cget("text") for w in walk(dialog) if isinstance(w, (ctk.CTkLabel, ctk.CTkButton))]

    def test_about_is_last_item_at_sidebar_bottom(self) -> None:
        sidebar = self.app.sidebar
        button = self.app.btn_about
        self.assertEqual(button.cget("text"), "關於")
        self.assertIs(button.master, sidebar.bottom)
        self.assertNotIn(button, sidebar.items.values())
        self.assertEqual(tuple(button.cget("text_color")), T["muted"])
        # 深淺色切換在上、「關於」在最下面
        self.assertGreater(button.winfo_y(), self.app.btn_theme.winfo_y())
        self.assertEqual(sidebar.bottom.pack_slaves()[-1], button)

    def test_shows_product_version_author_and_link(self) -> None:
        dialog = self._open()
        self.assertEqual(dialog.title(), f"關於 — {APP_NAME}")
        texts = self._texts(dialog)
        for expected in (APP_NAME, f"版本 {APP_VERSION}", APP_AUTHOR, "github.com/Human-chang", "WingLight 系列", "關閉"):
            self.assertIn(expected, texts)
        # 夜空底深淺色都一樣
        self.assertEqual(tuple(dialog.band.cget("fg_color")), T["splash-bg"])
        self.assertEqual(tuple(dialog.lbl_name.cget("text_color")), T["splash-text"])
        # 可以點的文字是 accent 字；這個視窗沒有主要動作，沒有 accent 填色的按鈕
        self.assertEqual(tuple(dialog.btn_link.cget("text_color")), T["accent"])
        for widget in walk(dialog):
            if isinstance(widget, ctk.CTkButton):
                self.assertNotEqual(widget.cget("fg_color"), T["accent"], widget.cget("text"))
        # 字級只用規範那幾種
        for widget in walk(dialog):
            if isinstance(widget, (ctk.CTkLabel, ctk.CTkButton)) and widget.cget("text"):
                self.assertIn(widget.cget("font").cget("size"), SPEC_FONT_SIZES, widget.cget("text"))
        # 螢火蟲與標誌都有讀到圖
        self.assertIsNotNone(dialog.lbl_firefly.cget("image"))
        self.assertIsNotNone(dialog.lbl_mark.cget("image"))

    def test_link_opens_github_in_browser(self) -> None:
        dialog = self._open()
        with mock.patch("ui.about_dialog.webbrowser.open", return_value=True) as browser:
            dialog.btn_link.invoke()
        browser.assert_called_once_with(APP_URL)

    def test_only_one_dialog_and_escape_closes_it(self) -> None:
        dialog = self._open()
        self.app.btn_about.invoke()
        self.app.update()
        self.assertIs(self.app.about_window, dialog)
        toplevels = [w for w in self.app.winfo_children() if isinstance(w, ctk.CTkToplevel) and w.winfo_exists()]
        self.assertEqual(toplevels, [dialog])
        # 按鍵送給有焦點的視窗 (使用者點「關於」時程式本來就在前景)
        dialog.focus_force()
        dialog.update()
        dialog.event_generate("<Escape>")
        self.app.update()
        self.assertFalse(dialog.winfo_exists())
        # 關掉後再點會開新的一個
        again = self._open()
        self.assertIsNot(again, dialog)
        self.assertTrue(again.winfo_exists())
        again.btn_close.invoke()
        self.app.update()
        self.assertFalse(again.winfo_exists())

    def test_dialog_is_placed_over_main_window(self) -> None:
        dialog = self._open()
        dialog.update()
        center_x = dialog.winfo_rootx() + dialog.winfo_width() // 2
        main_center_x = self.app.winfo_rootx() + self.app.winfo_width() // 2
        self.assertLessEqual(abs(center_x - main_center_x), 2)
        self.assertGreaterEqual(dialog.winfo_rooty(), self.app.winfo_rooty())
        self.assertFalse(dialog.resizable()[0] or dialog.resizable()[1])

    def test_dialog_uses_app_icon(self) -> None:
        dialog = self._open()
        self.assertTrue(dialog._iconbitmap_method_called)
        hwnd = int(dialog.wm_frame(), 16)
        send = ctypes.windll.user32.SendMessageW
        send.restype = ctypes.c_ssize_t
        self.assertTrue(send(hwnd, WM_GETICON, ICON_SMALL, 0) and send(hwnd, WM_GETICON, ICON_BIG, 0))

    def test_missing_images_only_warn(self) -> None:
        import ui.about_dialog as about

        missing = self.tmp_dir / "沒有這張.png"
        with mock.patch.object(about, "FIREFLY_PNG", missing), mock.patch.object(about, "MARK_PNG", missing):
            with self.assertLogs("ui.about_dialog", level="WARNING"):
                dialog = self._open()
        self.assertIn(APP_NAME, self._texts(dialog))
        # 圖不見時版面寬度不變
        dialog.update()
        self.assertEqual(round(dialog.winfo_width() / ctk.ScalingTracker.get_window_scaling(dialog)), about.ABOUT_WIDTH)

    def test_theme_switch_while_open(self) -> None:
        dialog = self._open()
        for mode in ("Light", "Dark"):
            ctk.set_appearance_mode(mode)
            self.app.update()
            self.assertTrue(dialog.winfo_exists())
            self.assertEqual(tuple(dialog.band.cget("fg_color")), T["splash-bg"])


if __name__ == "__main__":
    unittest.main()
