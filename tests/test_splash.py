"""螢火蟲啟動畫面 (風格統一 D2 段)：tools/make_splash.py 產生的圖、主程式通知啟動器關掉啟動畫面
(ui/launcher_link.py)。啟動器本身 (畫面出現、通知後關閉、出錯訊息、最多 15 秒) 在 tests/test_launcher.py。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import importlib.util
import os
import unittest
from unittest import mock

import customtkinter as ctk
from PIL import Image, ImageChops

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui import launcher_link, settings

_spec = importlib.util.spec_from_file_location("make_splash", PROJECT_ROOT / "tools" / "make_splash.py")
make_splash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_splash)

FONTS_AVAILABLE = (make_splash.FONT_DIR / "msjh.ttc").is_file() and (make_splash.FONT_DIR / "msjhbd.ttc").is_file()


def _same_pixels(a: Image.Image, b: Image.Image) -> bool:
    # RGBA 圖的 getbbox 預設只看透明度，要連顏色一起比
    diff = ImageChops.difference(a.convert("RGBA"), b.convert("RGBA"))
    return a.size == b.size and diff.getbbox(alpha_only=False) is None


class _Event:
    """測試用的具名事件 (代替啟動器建的那個)。"""

    def __init__(self, name: str) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateEventW.restype = wt.HANDLE
        self.kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR)
        self.kernel32.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
        self.kernel32.CloseHandle.argtypes = (wt.HANDLE,)
        self.name = name
        self.handle = self.kernel32.CreateEventW(None, True, False, name)
        assert self.handle

    def is_set(self) -> bool:
        return self.kernel32.WaitForSingleObject(self.handle, 0) == 0

    def close(self) -> None:
        self.kernel32.CloseHandle(self.handle)


class TestSplashImages(unittest.TestCase):
    def test_firefly_frames_match_generator(self) -> None:
        # assets/splash/ 要跟規範的 firefly.svg 同步：改了 SVG 或產生器，要重跑 tools/make_splash.py
        for path, dim in ((make_splash.BRIGHT_PNG, False), (make_splash.DIM_PNG, True)):
            with Image.open(path) as saved:
                self.assertEqual(saved.size, (440, 440))
                self.assertTrue(_same_pixels(saved, make_splash.render_firefly(dim=dim)), path.name)

    def test_bright_and_dim_differ_only_in_tail_glow(self) -> None:
        bright, dim = make_splash.render_firefly(), make_splash.render_firefly(dim=True)
        box = ImageChops.difference(bright, dim).getbbox(alpha_only=False)
        # 光暈是圓心 (100, 150)、半徑 48 的圓 (viewBox 200 → 440 px，2.2 倍)
        scale = 440 / 200
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        self.assertGreaterEqual(left, round((100 - 48) * scale) - 1)
        self.assertGreaterEqual(top, round((150 - 48) * scale) - 1)
        self.assertLessEqual(right, round((100 + 48) * scale) + 1)
        self.assertLessEqual(bottom, round((150 + 48) * scale) + 1)
        # 光暈範圍內 (尾巴旁邊) 亮的那張比較亮
        point = (round(100 * scale), round(190 * scale))
        self.assertGreater(sum(bright.getpixel(point)[:3]), sum(dim.getpixel(point)[:3]))

    @unittest.skipUnless(FONTS_AVAILABLE, "沒有微軟正黑體")
    def test_text_images_match_generator_and_spec(self) -> None:
        for name, (_text, px, _bold, color) in make_splash.TEXTS.items():
            for scale in make_splash.TEXT_SCALES:
                with Image.open(make_splash.text_png(name, scale)) as saved:
                    self.assertTrue(_same_pixels(saved, make_splash.render_text(name, scale)), (name, scale))
                    # 圖高＝一行 (字級 × 1.5)；字的顏色照規範
                    self.assertEqual(saved.height, round(px * 1.5 * scale))
                    colors = saved.convert("RGBA").getcolors(maxcolors=1 << 20)
                    inked = {"#%02X%02X%02X" % pixel[:3] for _count, pixel in colors if pixel[3] > 0}
                    self.assertEqual(inked, {color})
                    self.assertGreater(saved.getchannel("A").getextrema()[1], 200)
            with Image.open(make_splash.text_png(name, 1)) as one, Image.open(make_splash.text_png(name, 2)) as two:
                # 不同字級的字距微調不同，寬度不會剛好兩倍
                self.assertAlmostEqual(two.width, one.width * 2, delta=one.width * 0.05)


class TestLauncherLink(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop(launcher_link.READY_EVENT_ENV, None)
        self.event = _Event(f"Local\\WingLightGIS-test-{os.getpid()}-{id(self)}")

    def tearDown(self) -> None:
        self.event.close()
        self._env.stop()
        super().tearDown()

    def test_not_started_by_launcher_does_nothing(self) -> None:
        self.assertFalse(launcher_link.notify_launcher())
        self.assertFalse(self.event.is_set())

    def test_sets_event_once_and_hides_it_from_child_processes(self) -> None:
        os.environ[launcher_link.READY_EVENT_ENV] = self.event.name
        self.assertTrue(launcher_link.notify_launcher())
        self.assertTrue(self.event.is_set())
        self.assertNotIn(launcher_link.READY_EVENT_ENV, os.environ)
        self.assertFalse(launcher_link.notify_launcher())

    def test_launcher_already_gone_is_not_an_error(self) -> None:
        os.environ[launcher_link.READY_EVENT_ENV] = self.event.name + "-已結束"
        self.assertFalse(launcher_link.notify_launcher())
        self.assertNotIn(launcher_link.READY_EVENT_ENV, os.environ)

    def test_main_window_notifies_after_first_shown(self) -> None:
        os.environ[launcher_link.READY_EVENT_ENV] = self.event.name
        settings.settings_path().unlink(missing_ok=True)
        import ui.dashboard_app as dash

        app = dash.GisMasterToolkitApp()
        try:
            # 建好還沒顯示時不通知
            self.assertFalse(self.event.is_set())
            app.update()
            app.update()
            self.assertTrue(self.event.is_set())
        finally:
            app.destroy()
            ctk.set_appearance_mode("Dark")

    def test_startup_error_closes_splash_before_message(self) -> None:
        import main

        os.environ[launcher_link.READY_EVENT_ENV] = self.event.name
        seen = []
        # 不帶 unittest 的命令列參數、不真的去找已經開著的那一份
        with mock.patch("sys.argv", ["main.py"]), mock.patch("ui.single_instance.claim_or_hand_off", return_value=mock.Mock()), \
                mock.patch("ui.dashboard_app.launch_app", side_effect=RuntimeError("測試用的啟動失敗")), \
                mock.patch("tkinter.messagebox.showerror", side_effect=lambda *a, **k: seen.append(self.event.is_set())), \
                mock.patch("ui.app_icon.set_app_user_model_id"), \
                self.assertLogs("main", level="ERROR"):
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 1)
        # 跳錯誤訊息的時候，啟動畫面已經叫關了
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
