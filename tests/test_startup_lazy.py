"""啟動加速 (風格統一 D3 段)：啟動時只建立看得到的那一頁 (H1 起是圖資快覽)，三個裁切頁第一次點到才建立，
測試版工具第一次取用才建立；folium、openpyxl 等第一次用到才載入 (core/lazy_import.py)。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

import customtkinter as ctk

from core.lazy_import import LazyModule
from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui import settings

# 在全新的 Python 行程開主視窗，回報哪些重的模組已經載入
FRESH_STARTUP = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from core import env_runner
env_runner.set_workspace_temp_dir(sys.argv[2] + "/workspace_temp")
env_runner.set_config_dir(sys.argv[2] + "/config")
env_runner.set_default_output_dir(sys.argv[2] + "/outputs")
import ui.dashboard_app as dash
app = dash.GisMasterToolkitApp()
app.update()
names = ["folium", "openpyxl", "ui.lab.lab_page", "ui.lab.tab_vector_cad", "ui.lab.tab_terrain_hydro", "core.vector_ops", "core.terrain_lab", "ui.about_dialog"]
print(json.dumps({name: name in sys.modules for name in names}))
app.destroy()
'''


class TestLazyModule(unittest.TestCase):
    def test_loads_on_first_attribute_access(self) -> None:
        module = LazyModule("json")
        self.assertFalse(module.is_loaded)
        self.assertIn("尚未載入", repr(module))
        self.assertEqual(module.dumps([1]), "[1]")
        self.assertTrue(module.is_loaded)
        self.assertIs(module.load(), sys.modules["json"])

    def test_missing_module_fails_only_when_used(self) -> None:
        module = LazyModule("沒有這個模組_wl")
        with self.assertRaises(ModuleNotFoundError):
            module.anything

    def test_preview_modules_use_lazy_folium(self) -> None:
        from core import clip_preview, preview_engine

        self.assertIsInstance(preview_engine.folium, LazyModule)
        self.assertIsInstance(preview_engine.plugins, LazyModule)
        self.assertIsInstance(clip_preview.folium, LazyModule)


class TestLazyToolPages(IsolatedWorkspaceTestCase):
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

    def test_only_the_first_tool_is_built_at_startup(self) -> None:
        pages = self.app.clip_tabs
        self.assertEqual(list(pages), ["dem", "ortho", "shp"])
        self.assertEqual(len(pages), 3)
        self.assertIn("shp", pages)  # 查有沒有這個工具不會建立它
        self.assertEqual(list(pages.built()), [])
        self.assertEqual(list(self.app.built_tool_pages()), ["view"])
        self.assertEqual(self.app.lab_pages, {})

    def test_tools_are_built_when_first_selected(self) -> None:
        self.app.sidebar.items["ortho"].invoke()
        self.app.update()
        self.assertEqual(sorted(self.app.clip_tabs.built()), ["ortho"])
        self.assertTrue(self.app.tab_ortho.winfo_ismapped())
        self.app.event_generate("<Control-Key-4>")
        self.app.update()
        self.assertEqual(sorted(self.app.clip_tabs.built()), ["ortho", "shp"])
        self.assertEqual(self.app.current_tool, "shp")
        # 同一頁不會建第二次
        self.assertIs(self.app.clip_tabs["shp"], self.app.tab_shp)
        with self.assertRaises(KeyError):
            self.app.select_tool("沒有這個工具")

    def test_hidden_tools_are_built_on_first_access(self) -> None:
        vector = self.app.tab_vector
        self.assertIs(self.app.tab_vector, vector)
        self.assertIsNotNone(self.app.tab_terrain)
        self.assertEqual(sorted(self.app.lab_pages), ["lab_terrain", "lab_vector"])
        # 測試版工具開關關著 (預設)：建好了也不放進版面
        self.app.update()
        self.assertFalse(self.app.lab_enabled)
        self.assertEqual(self.app.lab_page("lab_vector").winfo_manager(), "")
        self.assertFalse(vector.winfo_viewable())

    def test_fresh_start_does_not_load_heavy_modules(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", FRESH_STARTUP, str(PROJECT_ROOT), str(self.tmp_dir)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        loaded = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(loaded, {name: False for name in loaded}, loaded)


if __name__ == "__main__":
    unittest.main()
