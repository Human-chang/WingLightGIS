"""第 5 步第一段：背景作業回主執行緒、取消鈕、來源座標系選單與地形分頁接線的測試。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, List
from unittest import mock

import numpy as np

from core.crs_doctor import ASSIGNABLE_SOURCE_SRS
from core.env_runner import CommandCancelledError, run_command, run_in_background
from core.preview_engine import export_raster_preview_png
from core.terrain_lab import calculate_earthwork
from tests._support import IsolatedWorkspaceTestCase, write_dem
from ui import srs_options


class TestRunInBackgroundDispatcher(unittest.TestCase):
    """run_in_background 的 dispatcher：回呼不在背景執行緒直接執行，而是交給 dispatcher。"""

    def _run(self, target: Callable[[], Any], **kwargs: Any) -> List[Callable[[], None]]:
        posted: List[Callable[[], None]] = []
        thread = run_in_background(target, dispatcher=posted.append, **kwargs)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        return posted

    def test_success_and_finish_are_dispatched(self) -> None:
        calls: List[Any] = []
        posted = self._run(
            lambda: 42,
            on_success=lambda r: calls.append(("ok", r, threading.current_thread())),
            on_error=lambda e: calls.append(("err", e)),
            on_finish=lambda: calls.append(("fin",)),
        )
        self.assertEqual(calls, [], "回呼應該等 dispatcher 執行，而不是在背景執行緒直接跑")
        self.assertEqual(len(posted), 2)
        for func in posted:
            func()
        self.assertEqual(calls[0][:2], ("ok", 42))
        self.assertIs(calls[0][2], threading.current_thread())
        self.assertEqual(calls[1], ("fin",))

    def test_error_is_dispatched(self) -> None:
        calls: List[Any] = []

        def _boom() -> None:
            raise ValueError("壞掉了")

        posted = self._run(_boom, on_success=lambda r: calls.append("ok"), on_error=calls.append)
        self.assertEqual(calls, [])
        for func in posted:
            func()
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0], ValueError)

    def test_without_dispatcher_callbacks_run_directly(self) -> None:
        calls: List[Any] = []
        thread = run_in_background(lambda: 7, on_success=calls.append, on_finish=lambda: calls.append("fin"))
        thread.join(timeout=10)
        self.assertEqual(calls, [7, "fin"])


class TestSrsOptions(unittest.TestCase):
    def test_every_choice_maps_to_an_assignable_key(self) -> None:
        labels = srs_options.source_srs_labels()
        self.assertEqual(labels[0], srs_options.AUTO_SOURCE_SRS_LABEL)
        self.assertEqual(len(labels), len(set(labels)))
        self.assertIsNone(srs_options.assigned_srs_from_label(labels[0]))
        assigned = [srs_options.assigned_srs_from_label(label) for label in labels[1:]]
        for key in assigned:
            self.assertIn(key, ASSIGNABLE_SOURCE_SRS)
        self.assertEqual(set(assigned), set(ASSIGNABLE_SOURCE_SRS), "選單應涵蓋引擎支援的全部指派座標系 (含 TWD67)")

    def test_pour_point_choices(self) -> None:
        labels = srs_options.pour_point_srs_labels()
        self.assertEqual(labels[0], srs_options.POUR_POINT_SAME_AS_DEM_LABEL)
        self.assertIsNone(srs_options.pour_point_srs_from_label(labels[0]))
        for label in labels[1:]:
            self.assertIn(srs_options.pour_point_srs_from_label(label), ASSIGNABLE_SOURCE_SRS)

    def test_unknown_label_is_auto(self) -> None:
        self.assertIsNone(srs_options.assigned_srs_from_label("不存在的選項"))


class TestRasterPreviewPng(IsolatedWorkspaceTestCase):
    def test_earthwork_color_raster_becomes_png(self) -> None:
        from PIL import Image

        before = write_dem(self.data_dir / "before.tif", np.full((40, 60), 100.0))
        after_arr = np.full((40, 60), 100.0)
        after_arr[:, :30] += 2.0
        after_arr[:, 30:] -= 1.0
        after = write_dem(self.data_dir / "after.tif", after_arr)
        res = calculate_earthwork(before, after)
        self.assertIsNotNone(res.color_raster_path)

        png = export_raster_preview_png(res.color_raster_path, max_size=30)
        self.assertEqual(png.suffix, ".png")
        self.assertTrue(png.exists())
        self.assertFalse(Path(str(png) + ".aux.xml").exists())
        with Image.open(png) as img:
            self.assertEqual(img.format, "PNG")
            self.assertEqual(max(img.size), 30)
            self.assertEqual(img.size, (30, 20))

    def test_missing_raster_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            export_raster_preview_png(self.data_dir / "nope.tif")


class TestInspectorTaskRunner(IsolatedWorkspaceTestCase):
    """實際建立主視窗，驗證背景作業的日誌、完成、錯誤與取消都回到主執行緒。"""

    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy:
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _log_text(self) -> str:
        return self.inspector.txt_terminal.get("1.0", "end")

    def _bar(self) -> Any:
        """目前顯示的工具頁的底部動作列 (作業狀態、取消鈕在這裡)。"""
        return self.app.clip_tabs[self.app.current_tool].action_bar

    def test_success_runs_callbacks_on_main_thread(self) -> None:
        main_thread = threading.current_thread()
        seen: List[Any] = []

        def _task() -> str:
            seen.append(("task", threading.current_thread()))
            self.inspector.append_log("背景寫入的日誌\n")
            self.inspector.set_progress(40.0, "做到一半")
            return "結果"

        with mock.patch("ui.panel_inspector.messagebox") as box:
            started = self.inspector.run_task("測試作業", _task, on_success=lambda r: seen.append(("done", r, threading.current_thread())))
            self.assertTrue(started)
            self.assertTrue(self.inspector.is_busy)
            # 執行中：主要按鈕換成「取消」，黃色圓點出現
            self.assertEqual(self._bar().btn_cancel.winfo_manager(), "grid")
            self.assertEqual(self._bar().btn_cancel.cget("state"), "normal")
            self.assertEqual(self._bar().btn_primary.winfo_manager(), "")
            self.assertEqual(self._bar().glow_dot.winfo_manager(), "grid")
            self._pump_until_idle()
            box.showerror.assert_not_called()

        self.assertIsNot(seen[0][1], main_thread)
        self.assertEqual(seen[1][:2], ("done", "結果"))
        self.assertIs(seen[1][2], main_thread)
        self.assertIn("背景寫入的日誌", self._log_text())
        self.assertEqual(self._bar().btn_cancel.winfo_manager(), "")
        self.assertEqual(self._bar().btn_primary.winfo_manager(), "grid")
        self.assertEqual(self._bar().glow_dot.winfo_manager(), "")
        self.assertIn("測試作業：完成", self._bar().lbl_status.cget("text"))

    def test_progress_text_shows_in_action_bar_not_status_badge(self) -> None:
        badge_before = self.inspector.lbl_status_badge.cget("text")
        release = threading.Event()

        def _task() -> None:
            self.inspector.set_progress(55.0, "[2/3] 處理中")
            release.wait(20)

        with mock.patch("ui.panel_inspector.messagebox"):
            self.inspector.run_task("測試作業", _task)
            deadline = time.monotonic() + 10
            while "處理中" not in self._bar().lbl_status.cget("text") and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.02)
            self.assertEqual(self._bar().lbl_status.cget("text"), "測試作業：[2/3] 處理中")
            # 不顯示量不準的百分比
            self.assertNotIn("%", self._bar().lbl_status.cget("text"))
            release.set()
            self._pump_until_idle()
        self.assertEqual(self.inspector.lbl_status_badge.cget("text"), badge_before)

    def test_error_shows_dialog_on_main_thread(self) -> None:
        main_thread = threading.current_thread()
        dialog_threads: List[threading.Thread] = []
        errors: List[Exception] = []

        def _boom() -> None:
            raise RuntimeError("故意失敗")

        with mock.patch("ui.panel_inspector.messagebox") as box:
            box.showerror.side_effect = lambda *a, **k: dialog_threads.append(threading.current_thread())
            self.inspector.run_task("會失敗的作業", _boom, on_error=errors.append)
            self._pump_until_idle()
            box.showerror.assert_called_once()
            self.assertIn("故意失敗", box.showerror.call_args[0][1])

        self.assertEqual(dialog_threads, [main_thread])
        self.assertEqual(len(errors), 1)
        self.assertIn("[失敗] 會失敗的作業", self._log_text())
        self.assertFalse(self.inspector.is_busy)

    def test_second_task_is_refused_while_busy(self) -> None:
        release = threading.Event()
        with mock.patch("ui.panel_inspector.messagebox") as box:
            self.assertTrue(self.inspector.run_task("第一件", lambda: release.wait(20)))
            self.assertFalse(self.inspector.run_task("第二件", lambda: None))
            box.showwarning.assert_called_once()
            release.set()
            self._pump_until_idle()

    def test_cancel_terminates_running_cli(self) -> None:
        started = threading.Event()

        def _slow() -> None:
            started.set()
            run_command([sys.executable, "-c", "import time; time.sleep(60)"], check=True)

        successes: List[Any] = []
        with mock.patch("ui.panel_inspector.messagebox") as box:
            self.inspector.run_task("很久的作業", _slow, on_success=successes.append)
            self.assertTrue(started.wait(10))
            time.sleep(0.5)
            begin = time.monotonic()
            self.inspector.cancel_current_task()
            self.assertEqual(self._bar().btn_cancel.cget("state"), "disabled")
            self.assertEqual(self._bar().btn_cancel.cget("text"), "取消中")
            self._pump_until_idle(timeout=20.0)
            elapsed = time.monotonic() - begin
            box.showerror.assert_not_called()

        self.assertLess(elapsed, 15.0)
        self.assertEqual(successes, [])
        self.assertIn("[已取消] 很久的作業", self._log_text())
        self.assertIn("已取消", self._bar().lbl_status.cget("text"))
        self.assertEqual(self._bar().btn_cancel.winfo_manager(), "")

    def test_preview_image_file_is_not_locked(self) -> None:
        from PIL import Image

        png = self.out_dir / "preview.png"
        Image.new("RGB", (20, 10), (255, 0, 0)).save(png)
        self.inspector.load_preview_image(png)
        png.unlink()
        self.assertFalse(png.exists(), "預覽圖載入後應該已關檔，可以刪除或覆寫")

    def test_terrain_tab_earthwork_end_to_end(self) -> None:
        before = write_dem(self.data_dir / "before.tif", np.full((40, 60), 100.0))
        after_arr = np.full((40, 60), 100.0)
        after_arr[:, :30] += 2.0
        after = write_dem(self.data_dir / "after.tif", after_arr)

        tab = self.app.tab_terrain
        tab._set_entry(tab.entry_dem_before, str(before))
        tab._set_entry(tab.entry_dem_after, str(after))
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            tab._handle_calculate_earthwork()
            self._pump_until_idle(timeout=120.0)
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            self.assertIn("紅藍色階圖", tab_box.showinfo.call_args[0][1])

        log = self._log_text()
        self.assertIn("[土方完成]", log)
        self.assertIn("紅藍色階圖", log)
        preview = self.inspector.current_preview_image
        self.assertIsNotNone(preview)
        self.assertEqual(Path(preview).suffix, ".png")

    def test_terrain_tab_passes_selected_srs_to_engines(self) -> None:
        tab = self.app.tab_terrain
        tab.opt_source_srs.set("TWD67 121 分帶 (七參數轉換，臺灣本島)")
        tab.opt_pour_srs.set("WGS84 經緯度 (EPSG:4326)")
        tab._set_entry(tab.entry_ws_dem, str(self.data_dir / "dem.tif"))
        tab._set_entry(tab.entry_ws_pour, "121.0, 23.9")
        tab._set_entry(tab.entry_terrain_dem, str(self.data_dir / "dem.tif"))

        fake_ws = mock.Mock(catchment_area_ha=1.0, catchment_area_m2=10000.0, watershed_polygon=Path("ws.gpkg"))
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox"), mock.patch("ui.panel_inspector.messagebox"), mock.patch(
            "ui.lab.tab_terrain_hydro.delineate_watershed_whitebox", return_value=fake_ws
        ) as ws, mock.patch("ui.lab.tab_terrain_hydro.generate_contours", return_value=Path("c.shp")) as contours:
            tab._handle_delineate_watershed()
            self._pump_until_idle()
            tab._handle_generate_contours()
            self._pump_until_idle()

        self.assertEqual(ws.call_args[0][1], (121.0, 23.9))
        self.assertEqual(ws.call_args[1]["assigned_srs"], "TWD67_121")
        self.assertEqual(ws.call_args[1]["pour_point_srs"], "WGS84")
        self.assertEqual(contours.call_args[1]["assigned_srs"], "TWD67_121")
        self.assertEqual(contours.call_args[1]["interval"], 5.0)


if __name__ == "__main__":
    unittest.main()
