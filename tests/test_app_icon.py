"""App 圖示 (風格統一 D1 段)：產生器 (tools/make_app_icon.py) 畫出的 logo.png 與 app_icon.ico、
主視窗與子視窗的標題列圖示、工作列識別碼 (ui/app_icon.py)。"""

from __future__ import annotations

import ctypes
import importlib.util
import struct
import time
import unittest

import customtkinter as ctk
from PIL import Image, ImageChops

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui import app_icon, settings

_spec = importlib.util.spec_from_file_location("make_app_icon", PROJECT_ROOT / "tools" / "make_app_icon.py")
make_app_icon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_app_icon)

WM_GETICON = 0x007F
ICON_SMALL, ICON_BIG = 0, 1


def _same_pixels(a: Image.Image, b: Image.Image) -> bool:
    # RGBA 圖的 getbbox 預設只看透明度，要連顏色一起比
    diff = ImageChops.difference(a.convert("RGBA"), b.convert("RGBA"))
    return a.size == b.size and diff.getbbox(alpha_only=False) is None


def _ico_frames(path):
    """讀 ICO 目錄：{尺寸: (是否 PNG, 圖)}。"""
    data = path.read_bytes()
    _, kind, count = struct.unpack_from("<HHH", data, 0)
    frames = {}
    with Image.open(path) as ico:
        for index in range(count):
            width, _h, _c, _r, _p, _bpp, _size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
            size = width or 256
            ico.size = (size, size)
            ico.load()
            frames[size] = (data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n", ico.copy())
    return kind, frames


class TestAppIconFiles(unittest.TestCase):
    def test_logo_png_matches_generator(self) -> None:
        # assets/logo.png 要跟規範的 SVG 同步：改了 SVG 或產生器，要重跑 tools/make_app_icon.py
        with Image.open(make_app_icon.LOGO_PNG) as saved:
            self.assertEqual(saved.size, (512, 512))
            self.assertTrue(_same_pixels(saved, make_app_icon.render_app_icon(512)))

    def test_ico_has_every_size_drawn_separately(self) -> None:
        kind, frames = _ico_frames(make_app_icon.APP_ICON_ICO)
        self.assertEqual(kind, 1)
        self.assertEqual(sorted(frames), [16, 24, 32, 48, 64, 128, 256])
        for size, (is_png, image) in frames.items():
            # 256 px 存 PNG，其餘存 BMP (舊 API 與 Tk 都讀得懂)
            self.assertEqual(is_png, size >= 256, size)
            self.assertTrue(_same_pixels(image, make_app_icon.render_app_icon(size)), size)

    def test_small_sizes_drop_the_highlight(self) -> None:
        svg = make_app_icon.APP_ICON_SVG
        for size in (16, 24):
            plain = make_app_icon.render_svg(svg, size, skip=make_app_icon.is_highlight)
            self.assertTrue(_same_pixels(make_app_icon.render_app_icon(size), plain), size)
            self.assertFalse(_same_pixels(make_app_icon.render_svg(svg, size), plain), size)
        self.assertTrue(_same_pixels(make_app_icon.render_app_icon(32), make_app_icon.render_svg(svg, 32)))

    def test_icon_is_product_green_feather_tile(self) -> None:
        image = make_app_icon.render_app_icon(256)
        # 羽毛形：右上角幾乎是直角 (有顏色)，左上角是大圓角 (透明)
        self.assertGreater(image.getpixel((240, 12))[3], 200)
        self.assertEqual(image.getpixel((8, 8))[3], 0)
        # 方塊本體是產品色 #1E8F63 (右下角避開高光與白色圖層)
        self.assertEqual(image.getpixel((235, 150))[:3], (0x1E, 0x8F, 0x63))
        # 中間的圖層是白色
        self.assertEqual(image.getpixel((128, 98))[:3], (255, 255, 255))


class TestSvgRenderer(IsolatedWorkspaceTestCase):
    def _svg(self, body: str) -> object:
        path = self.tmp_dir / "測試.svg"
        path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">{body}</svg>', encoding="utf-8")
        return path

    def test_fill_opacity_clip_and_transparency(self) -> None:
        path = self._svg(
            '<defs><clipPath id="c"><rect x="0" y="0" width="5" height="10"/></clipPath></defs>'
            '<rect x="0" y="0" width="10" height="5" fill="#FF0000"/>'
            '<rect x="0" y="0" width="10" height="5" fill="#FFFFFF" fill-opacity="0.5" clip-path="url(#c)"/>'
        )
        image = make_app_icon.render_svg(path, 10)
        left, right, empty = image.getpixel((2, 2)), image.getpixel((7, 2)), image.getpixel((5, 8))
        self.assertEqual(right, (255, 0, 0, 255))
        self.assertEqual(left[0], 255)
        self.assertAlmostEqual(left[1], 128, delta=1)
        self.assertEqual(empty[3], 0)

    def test_rounded_rect_circle_and_round_stroke(self) -> None:
        path = self._svg(
            '<rect width="10" height="10" rx="4" fill="#000000"/>'
            '<circle cx="5" cy="5" r="2" fill="#00FF00"/>'
            '<path d="M1 9h8" fill="none" stroke="#0000FF" stroke-width="1" stroke-linecap="round"/>'
        )
        image = make_app_icon.render_svg(path, 100)
        self.assertEqual(image.getpixel((2, 2))[3], 0)  # 圓角外面透明
        self.assertEqual(image.getpixel((50, 50)), (0, 255, 0, 255))
        self.assertEqual(image.getpixel((50, 90)), (0, 0, 255, 255))

    def test_unsupported_content_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_app_icon.render_svg(self._svg('<polygon points="0,0 5,5 0,5" fill="#000000"/>'), 10)
        with self.assertRaises(ValueError):
            make_app_icon.render_svg(self._svg('<path d="M1 1h5" stroke="#000000" fill="none"/>'), 10)


class TestWindowIcons(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        settings.settings_path().unlink(missing_ok=True)
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        ctk.set_appearance_mode("Dark")
        super().tearDown()

    def _icons(self, window) -> tuple:
        hwnd = int(window.wm_frame(), 16)
        send = ctypes.windll.user32.SendMessageW
        send.restype = ctypes.c_ssize_t
        return send(hwnd, WM_GETICON, ICON_SMALL, 0), send(hwnd, WM_GETICON, ICON_BIG, 0)

    def test_main_window_uses_app_icon(self) -> None:
        small, big = self._icons(self.app)
        self.assertTrue(small and big)
        # CustomTkinter 只在沒呼叫過 iconbitmap 時才換上它的藍色圖示
        self.assertTrue(self.app._iconbitmap_method_called)
        # 等過 CustomTkinter 換圖示的 200 ms，圖示仍然是同一個
        time.sleep(0.4)
        self.app.update()
        self.assertEqual(self._icons(self.app), (small, big))

    def test_log_window_uses_app_icon(self) -> None:
        inspector = self.app.inspector_panel
        inspector.open_log_window()
        window = inspector.log_window
        window.update()
        self.assertTrue(window._iconbitmap_method_called)
        self.assertTrue(all(self._icons(window)))
        inspector._close_log_window()

    def test_missing_icon_file_only_warns(self) -> None:
        missing = self.tmp_dir / "沒有這個.ico"
        original = app_icon.APP_ICON_PATH
        app_icon.APP_ICON_PATH = missing
        try:
            with self.assertLogs("ui.app_icon", level="WARNING"):
                self.assertFalse(app_icon.apply_window_icon(self.app))
        finally:
            app_icon.APP_ICON_PATH = original


class TestAppUserModelId(unittest.TestCase):
    def test_taskbar_id_is_set_for_this_process_only(self) -> None:
        self.assertTrue(app_icon.set_app_user_model_id())
        value = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value))
        self.assertEqual(hr, 0)
        self.assertEqual(value.value, "HumanChang.WingLightGIS")
        ctypes.windll.ole32.CoTaskMemFree(value)


if __name__ == "__main__":
    unittest.main()
