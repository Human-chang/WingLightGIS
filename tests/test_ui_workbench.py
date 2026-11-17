"""側欄工作台 (風格統一 C1、C2 段)：側欄切換工具並保留各自的內容、深淺色記在設定檔、底部動作列的輸出位置與現況、
「開始裁切」條件不夠時停用、處理中的黃色圓點、Ctrl+1～4、視窗太窄時收起檢視欄；開程式時停在圖資快覽 (H1)。"""

from __future__ import annotations

import time
import unittest
from unittest import mock

import customtkinter as ctk
import numpy as np

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase, square, write_dem, write_geojson
from ui import settings
from ui.theme_constants import T


class TestSettingsFile(IsolatedWorkspaceTestCase):
    def test_missing_or_broken_file_means_no_setting(self) -> None:
        self.assertEqual(settings.settings_path().parent, self.config_dir)
        settings.settings_path().unlink(missing_ok=True)
        self.assertIsNone(settings.saved_appearance_mode())
        settings.settings_path().write_text("{壞掉的 JSON", encoding="utf-8")
        self.assertEqual(settings.load_settings(), {})
        self.assertIsNone(settings.saved_appearance_mode())

    def test_appearance_round_trip_keeps_other_keys(self) -> None:
        settings.save_setting("其他設定", "保留")
        settings.save_appearance_mode("Dark")
        self.assertEqual(settings.saved_appearance_mode(), "Dark")
        settings.save_appearance_mode("Light")
        self.assertEqual(settings.saved_appearance_mode(), "Light")
        self.assertEqual(settings.load_settings()["其他設定"], "保留")

    def test_settings_file_is_not_committed(self) -> None:
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("config/WingLightGIS.json", ignored)


class TestWorkbench(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        settings.settings_path().unlink(missing_ok=True)
        import ui.dashboard_app as dash

        self.dash = dash
        self.app = dash.GisMasterToolkitApp()
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        ctk.set_appearance_mode("Dark")
        super().tearDown()

    def test_sidebar_switches_tools_and_keeps_their_input(self) -> None:
        app = self.app
        # 測試版工具的兩項也在側欄裡，開關關著 (預設) 時藏起來、不在 tool_keys
        self.assertEqual(list(app.sidebar.items), ["view", "dem", "ortho", "shp", "lab_vector", "lab_terrain"])
        self.assertEqual(app.tool_keys, ["view", "dem", "ortho", "shp"])
        self.assertEqual(app.current_tool, "view", "開程式時停在圖資快覽")
        self.assertTrue(app.tab_view.is_active)
        app.sidebar.items["dem"].invoke()
        app.update()
        self.assertFalse(app.tab_view.is_active)
        self.assertFalse(app.tab_view.winfo_ismapped())
        app.tab_dem.entry_buffer.delete(0, "end")
        app.tab_dem.entry_buffer.insert(0, "15")
        # 等同使用者點側欄
        app.sidebar.items["shp"].invoke()
        app.update()
        self.assertEqual(app.current_tool, "shp")
        self.assertEqual(app.sidebar.selected, "shp")
        self.assertEqual(tuple(app.sidebar.items["shp"].cget("text_color")), T["accent"])
        self.assertEqual(tuple(app.sidebar.items["dem"].cget("text_color")), T["text"])
        self.assertTrue(app.tab_shp.is_active)
        self.assertFalse(app.tab_dem.is_active)
        app.sidebar.items["dem"].invoke()
        app.update()
        self.assertEqual(app.tab_dem.entry_buffer.get(), "15")
        with self.assertRaises(KeyError):
            app.select_tool("不存在的工具")

    def test_theme_toggle_is_remembered(self) -> None:
        before = ctk.get_appearance_mode()
        self.app.toggle_appearance()
        after = ctk.get_appearance_mode()
        self.assertNotEqual(before, after)
        self.assertEqual(settings.saved_appearance_mode(), after)
        self.assertEqual(self.app.btn_theme.cget("text"), "深色" if after == "Dark" else "淺色")
        self.app.destroy()
        # 下次開啟用記住的
        ctk.set_appearance_mode(before)
        self.app = self.dash.GisMasterToolkitApp()
        self.app.update()
        self.assertEqual(ctk.get_appearance_mode(), after)

    def test_open_output_folder(self) -> None:
        bar = self.app.tab_dem.action_bar
        bar.set_output(str(self.out_dir / "還沒建立"))
        with mock.patch("ui.action_bar.messagebox") as box, mock.patch("ui.action_bar.os.startfile") as startfile:
            bar.btn_open.invoke()
            box.showinfo.assert_called_once()
            self.assertIn("第一次輸出成果時會自動建立", box.showinfo.call_args[0][1])
            startfile.assert_not_called()
            bar.set_output(str(self.out_dir))
            bar.btn_open.invoke()
            startfile.assert_called_once_with(str(self.out_dir))

    def test_ready_status_always_has_an_icon(self) -> None:
        bar = self.app.tab_dem.action_bar
        bar.set_ready("還沒選裁切邊界", ok=False)
        self.assertEqual(bar.lbl_status.cget("text"), "還沒選裁切邊界")
        self.assertEqual(tuple(bar.lbl_status.cget("text_color")), T["warn"])
        self.assertIsNotNone(bar.lbl_status.cget("image"))

    def test_start_button_is_disabled_until_everything_is_ready(self) -> None:
        tab = self.app.tab_dem
        bar = tab.action_bar
        self.assertEqual(bar.lbl_status.cget("text"), "還沒加入原始檔")
        self.assertEqual(tab.btn_execute.cget("state"), "disabled")
        self.assertEqual(tuple(tab.btn_execute.cget("fg_color")), T["bg"])

        dem = write_dem(self.data_dir / "地形.tif", np.ones((20, 20)))
        tab._add_input_files([dem])
        self.assertEqual(bar.lbl_status.cget("text"), "正在判別圖資類型")
        self._pump_until_idle()
        self.assertEqual(bar.lbl_status.cget("text"), "還沒選裁切邊界")
        self.assertEqual(tab.btn_execute.cget("state"), "disabled")

        # 邊界檔路徑存在就算有邊界輸入 (按開始裁切時會自動建立)
        boundary = write_geojson(self.data_dir / "邊界.geojson", [({"n": 1}, square(250010, 2650010, 250080, 2650090))])
        tab.entry_bound_file.insert(0, str(boundary))
        tab._refresh_ready()
        self.assertEqual(bar.lbl_status.cget("text"), "可以開始：1 個檔案")
        self.assertEqual(tab.btn_execute.cget("state"), "normal")
        self.assertEqual(tuple(tab.btn_execute.cget("fg_color")), T["accent"])

        bar.set_output("")
        self.assertEqual(bar.lbl_status.cget("text"), "還沒指定輸出資料夾")
        self.assertEqual(tab.btn_execute.cget("state"), "disabled")

    def test_result_stays_until_the_situation_changes(self) -> None:
        from ui.task_status import TaskStatus

        bar = self.app.tab_dem.action_bar
        bar.set_ready("可以開始：2 個檔案", ok=True)
        bar.apply_task_status(TaskStatus("running", "批次裁切", "[1/2] 地形.tif"))
        bar.set_ready("可以開始：2 個檔案", ok=True)
        self.assertEqual(bar.lbl_status.cget("text"), "批次裁切：[1/2] 地形.tif", "執行中不蓋掉進度")
        bar.apply_task_status(TaskStatus("done", "批次裁切"))
        bar.set_ready("可以開始：2 個檔案", ok=True)
        self.assertEqual(bar.lbl_status.cget("text"), "批次裁切：完成")
        bar.set_ready("可以開始：3 個檔案", ok=True)
        self.assertEqual(bar.lbl_status.cget("text"), "可以開始：3 個檔案")

    def test_glow_dot_pulses_while_running(self) -> None:
        from ui import action_bar
        from ui.task_status import TaskStatus

        self.assertEqual(action_bar.mix_color("#000000", "#FFC83D", 0.0), "#000000")
        self.assertEqual(action_bar.mix_color("#000000", "#FFC83D", 1.0), "#FFC83D")
        bar = self.app.tab_dem.action_bar
        with mock.patch.object(action_bar, "reduce_motion", return_value=False):
            bar.apply_task_status(TaskStatus("running", "測試"))
            self.assertIsNotNone(bar._pulse_job)
            bar.apply_task_status(TaskStatus("done", "測試"))
            self.assertIsNone(bar._pulse_job)
        # Windows 關掉動畫效果時圓點不閃，停在最亮
        with mock.patch.object(action_bar, "reduce_motion", return_value=True):
            bar.apply_task_status(TaskStatus("running", "測試"))
            self.assertIsNone(bar._pulse_job)
            self.assertEqual(tuple(bar.glow_dot.cget("fg_color")), T["glow"])
            bar.apply_task_status(TaskStatus("done", "測試"))

    def test_keyboard_shortcuts_switch_tools(self) -> None:
        self.app.focus_force()
        # 開場在圖資快覽 (Ctrl+1)，倒著按才每次都會換頁
        for index, key in reversed(list(enumerate(("view", "dem", "ortho", "shp"), start=1))):
            self.app.event_generate(f"<Control-Key-{index}>")
            self.app.update()
            self.assertEqual(self.app.current_tool, key)

    def test_narrow_window_hides_the_inspector(self) -> None:
        tab = self.app.tab_dem
        self.app.set_inspector_collapsed(True)
        self.app.update()
        self.assertEqual(tab.inspector_slot.winfo_manager(), "")
        self.assertFalse(self.app.inspector_panel.winfo_ismapped())
        self.app.select_tool("shp")
        self.app.update()
        self.assertEqual(self.app.tab_shp.inspector_slot.winfo_manager(), "")
        self.app.set_inspector_collapsed(False)
        self.app.update()
        self.assertEqual(self.app.tab_shp.inspector_slot.winfo_manager(), "grid")
        self.assertTrue(self.app.inspector_panel.winfo_ismapped())

    def _pump_until_idle(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.app.inspector_panel.is_busy and all(t._light_pending == 0 for t in self.app.clip_tabs.values()):
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")


if __name__ == "__main__":
    unittest.main()
