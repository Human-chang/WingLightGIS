"""測試版工具 (第 10 步)：側欄最下面的開關打開才出現「測試版工具」組 (向量與 CAD、地形與水文)，開關記在設定檔；
發佈版整個 ui/lab 拿掉後主程式照常啟動、沒有開關。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import customtkinter as ctk

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui import settings

BASE_KEYS = ["view", "dem", "ortho", "shp"]
LAB_KEYS = ["lab_vector", "lab_terrain"]

# 在拿掉 ui/lab 的複本裡用全新的 Python 行程：載入每一個模組、開主視窗，回報開關與側欄
RELEASE_STARTUP = r'''
import json, pkgutil, importlib, sys
sys.path.insert(0, sys.argv[1])
from core import env_runner
env_runner.set_workspace_temp_dir(sys.argv[2] + "/workspace_temp")
env_runner.set_config_dir(sys.argv[2] + "/config")
env_runner.set_default_output_dir(sys.argv[2] + "/outputs")
import core, ui
modules = ["main"] + [f"{pkg.__name__}.{info.name}" for pkg in (core, ui) for info in pkgutil.iter_modules(pkg.__path__)]
for name in modules:
    importlib.import_module(name)
import ui.dashboard_app as dash
app = dash.GisMasterToolkitApp()
app.update()
app.event_generate("<Control-Key-5>")
app.update()
print(json.dumps({
    "root": str(env_runner.get_toolkit_root()),
    "lab": dash.lab is None,
    "toggle": app.btn_lab is None,
    "keys": app.tool_keys,
    "items": list(app.sidebar.items),
    "current": app.current_tool,
    "loaded": sorted(name for name in sys.modules if name.startswith("ui.lab")),
    "count": len(modules),
}))
app.destroy()
'''


class TestLabSetting(IsolatedWorkspaceTestCase):
    def test_off_unless_saved_on(self) -> None:
        settings.settings_path().unlink(missing_ok=True)
        self.assertFalse(settings.saved_lab_tools_enabled())
        settings.save_setting("其他設定", "保留")
        settings.save_lab_tools_enabled(True)
        self.assertTrue(settings.saved_lab_tools_enabled())
        settings.save_lab_tools_enabled(False)
        self.assertFalse(settings.saved_lab_tools_enabled())
        self.assertEqual(settings.load_settings()["其他設定"], "保留")
        # 不是 true 的值都當成關
        settings.save_setting(settings.LAB_TOOLS_KEY, "yes")
        self.assertFalse(settings.saved_lab_tools_enabled())


class TestLabTools(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        settings.settings_path().unlink(missing_ok=True)

    def tearDown(self) -> None:
        app = getattr(self, "app", None)
        if app is not None:
            app.destroy()
        ctk.set_appearance_mode("Dark")
        super().tearDown()

    def _open(self) -> None:
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp()
        self.app.update()

    def test_off_by_default(self) -> None:
        self._open()
        self.assertFalse(self.app.lab_enabled)
        self.assertEqual(self.app.btn_lab.cget("text"), "測試版工具：關")
        self.assertEqual(self.app.tool_keys, BASE_KEYS)
        self.assertFalse(self.app.lab_section.winfo_ismapped())
        self.assertFalse(self.app.sidebar.items["lab_vector"].winfo_viewable())
        # 開關在側欄最下面那一區的第一項 (深淺色、關於的上面)
        self.assertIs(self.app.btn_lab.master, self.app.sidebar.bottom)
        self.assertLess(self.app.btn_lab.winfo_y(), self.app.btn_theme.winfo_y())
        # 關著時 Ctrl+5 沒有作用，也不能直接切過去
        self.app.event_generate("<Control-Key-5>")
        self.app.update()
        self.assertEqual(self.app.current_tool, "view")
        with self.assertRaises(KeyError):
            self.app.select_tool("lab_vector")
        self.assertEqual(self.app.lab_pages, {})

    def test_turning_on_shows_group_and_is_remembered(self) -> None:
        self._open()
        self.app.btn_lab.invoke()
        self.app.update()
        self.assertTrue(self.app.lab_enabled)
        self.assertEqual(self.app.btn_lab.cget("text"), "測試版工具：開")
        self.assertTrue(settings.saved_lab_tools_enabled())
        self.assertEqual(self.app.tool_keys, BASE_KEYS + LAB_KEYS)
        self.assertTrue(self.app.sidebar.items["lab_terrain"].winfo_viewable())
        # 打開不換頁，頁也還沒建
        self.assertEqual(self.app.current_tool, "view")
        self.assertEqual(self.app.lab_pages, {})

        # 下次開程式照記住的
        self.app.destroy()
        self._open()
        self.assertTrue(self.app.lab_enabled)
        self.assertTrue(self.app.sidebar.items["lab_vector"].winfo_viewable())

    def test_lab_page_layout_and_inspector(self) -> None:
        settings.save_lab_tools_enabled(True)
        self._open()
        self.app.sidebar.items["lab_vector"].invoke()
        self.app.update()
        page = self.app.lab_page("lab_vector")
        self.assertEqual(self.app.current_tool, "lab_vector")
        self.assertEqual(self.app.sidebar.selected, "lab_vector")
        self.assertTrue(page.winfo_viewable())
        self.assertIs(page.content, self.app.tab_vector)
        self.assertEqual(page.lbl_page_title.cget("text"), "向量與 CAD")
        self.assertIn("測試版", page.lbl_notice.cget("text"))
        self.assertIn("自行核對", page.lbl_notice.cget("text"))
        # 檢視欄搬到這一頁，不接裁切工具或圖資快覽的地圖與邊界
        inspector = self.app.inspector_panel
        self.assertIs(inspector.grid_info()["in"], page.inspector_slot)
        self.assertIsNone(inspector.on_use_extent)
        self.assertIsNone(inspector.on_open_browser_preview)
        self.assertIsNone(inspector.current_preview_html)
        # Ctrl+6＝地形與水文；只有目前這一頁顯示
        self.app.event_generate("<Control-Key-6>")
        self.app.update()
        self.assertEqual(self.app.current_tool, "lab_terrain")
        self.assertFalse(page.winfo_viewable())
        self.assertTrue(self.app.tab_terrain.winfo_viewable())
        self.assertIs(inspector.grid_info()["in"], self.app.lab_page("lab_terrain").inspector_slot)
        # 切回裁切工具，檢視欄跟著回去
        self.app.event_generate("<Control-Key-2>")
        self.app.update()
        self.assertIs(inspector.grid_info()["in"], self.app.tab_dem.inspector_slot)
        self.assertIsNotNone(inspector.on_use_extent)

    def test_turning_off_on_lab_page_goes_back_to_quick_view(self) -> None:
        settings.save_lab_tools_enabled(True)
        self._open()
        self.app.select_tool("lab_terrain")
        self.app.update()
        self.app.btn_lab.invoke()
        self.app.update()
        self.assertFalse(self.app.lab_enabled)
        self.assertFalse(settings.saved_lab_tools_enabled())
        self.assertEqual(self.app.current_tool, "view")
        self.assertEqual(self.app.sidebar.selected, "view")
        self.assertFalse(self.app.lab_page("lab_terrain").winfo_viewable())
        self.assertFalse(self.app.lab_section.winfo_ismapped())
        # 不在測試版工具頁時關掉，留在原本的頁
        self.app.btn_lab.invoke()
        self.app.select_tool("shp")
        self.app.btn_lab.invoke()
        self.app.update()
        self.assertEqual(self.app.current_tool, "shp")

    def test_lab_pages_follow_inspector_collapse(self) -> None:
        settings.save_lab_tools_enabled(True)
        self._open()
        self.app.select_tool("lab_vector")
        self.app.update()
        self.app.set_inspector_collapsed(True)
        self.app.update()
        self.assertFalse(self.app.lab_page("lab_vector").inspector_slot.winfo_ismapped())
        # 收起時才建的頁也是收起的
        self.app.select_tool("lab_terrain")
        self.app.update()
        self.assertFalse(self.app.lab_page("lab_terrain").inspector_slot.winfo_ismapped())
        self.app.set_inspector_collapsed(False)
        self.app.update()
        self.assertTrue(self.app.lab_page("lab_terrain").inspector_slot.winfo_ismapped())

    def test_without_lab_package_there_is_no_toggle(self) -> None:
        with mock.patch("ui.dashboard_app.lab", None):
            self._open()
            self.assertIsNone(self.app.btn_lab)
            self.assertIsNone(self.app.lab_section)
            self.assertEqual(self.app.tool_keys, BASE_KEYS)
            self.assertEqual(list(self.app.sidebar.items), BASE_KEYS)
            self.app.set_lab_tools_enabled(True)
            self.assertEqual(self.app.tool_keys, BASE_KEYS)
            with self.assertRaises(KeyError):
                self.app.lab_page("lab_vector")


class TestReleaseWithoutLab(IsolatedWorkspaceTestCase):
    """照發佈版的做法拿掉 ui/lab：每個模組都載入得了、主視窗開得起來、沒有開關也沒有測試版工具。"""

    def test_release_copy_starts_without_lab(self) -> None:
        copy_root = self.tmp_dir / "發佈版 複本"
        skip = shutil.ignore_patterns("__pycache__", "lab")
        for name in ("core", "ui", "assets"):
            shutil.copytree(PROJECT_ROOT / name, copy_root / name, ignore=skip)
        shutil.copy2(PROJECT_ROOT / "main.py", copy_root / "main.py")
        self.assertFalse((copy_root / "ui" / "lab").exists())
        self.assertTrue((copy_root / "ui" / "icons.py").exists())

        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-B", "-c", RELEASE_STARTUP, str(copy_root), str(self.tmp_dir)],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result["root"], str(copy_root), "複本要用自己的程式，不是開發版的")
        self.assertTrue(result["lab"])
        self.assertTrue(result["toggle"])
        self.assertEqual(result["keys"], BASE_KEYS)
        self.assertEqual(result["items"], BASE_KEYS)
        self.assertEqual(result["current"], "view")
        self.assertEqual(result["loaded"], [])
        self.assertGreater(result["count"], 30)


if __name__ == "__main__":
    unittest.main()
