"""第 5 步第十段：每張卡片可自選輸出資料夾，留空時跟著來源檔走 (分頁 3)。"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import IsolatedWorkspaceTestCase, line, write_dem, write_geojson
from ui.output_dir_row import OutputDirRow


class TestTerrainOutputDirRows(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.tab = self.app.tab_terrain
        self.app.update()
        self.custom_dir = self.tmp_dir / "自選 成果" / "第一期"

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 150.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy:
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _sloped_dem(self, name: str) -> Path:
        rows, cols = 40, 60
        arr = 100.0 + np.arange(cols, dtype=float)[None, :] * 0.8 + np.arange(rows, dtype=float)[:, None] * 0.3
        return write_dem(self.data_dir / name, arr)

    def _run(self, handler) -> str:
        """執行處置函式到背景作業結束，回傳完成對話框的內文。"""
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            handler()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showwarning.assert_not_called()
            tab_box.showinfo.assert_called_once()
            return tab_box.showinfo.call_args[0][1]

    def test_every_card_has_an_output_row_above_its_run_button(self) -> None:
        tab = self.tab
        rows = [tab.out_earthwork, tab.out_slope_class, tab.out_terrain, tab.out_sections, tab.out_watershed]
        self.assertEqual(len({id(row.entry) for row in rows}), 5)
        for row in rows:
            self.assertIsInstance(row, OutputDirRow)
            self.assertEqual(row.entry.get(), "")
            self.assertIsNone(row.chosen())
            self.assertEqual(row.label.cget("text"), "輸出資料夾:")
            card = row.entry.master
            last_row = max(int(child.grid_info()["row"]) for child in card.grid_slaves())
            self.assertEqual(int(row.entry.grid_info()["row"]), last_row - 1, "輸出資料夾列應緊接在執行鈕上方")

    def test_browse_button_fills_entry_and_cancel_keeps_it(self) -> None:
        row = self.tab.out_earthwork
        with mock.patch("ui.output_dir_row.filedialog") as dialog:
            dialog.askdirectory.return_value = str(self.custom_dir)
            row.button.invoke()
            self.assertEqual(row.chosen(), self.custom_dir.resolve())
            dialog.askdirectory.return_value = ""
            row.button.invoke()
            self.assertEqual(row.chosen(), self.custom_dir.resolve())
        self.assertFalse(self.custom_dir.exists(), "只是選資料夾，還沒執行不該建立")

    def test_folder_and_file_rules(self) -> None:
        row = self.tab.out_terrain
        source = self.data_dir / "hill.tif"
        self.assertEqual(row.folder_for(source, "土方成果"), self.data_dir.resolve() / "hill_土方成果")
        self.assertEqual(row.file_for(self.data_dir / "hill_aspect.tif"), self.data_dir / "hill_aspect.tif")
        row.set(f'  "{self.custom_dir}"  ')
        self.assertEqual(row.folder_for(source, "土方成果"), self.custom_dir.resolve())
        self.assertEqual(row.file_for(self.data_dir / "hill_aspect.tif"), self.custom_dir.resolve() / "hill_aspect.tif")

    def test_output_pointing_at_a_file_is_rejected_before_running(self) -> None:
        tab = self.tab
        dem = self._sloped_dem("hill.tif")
        tab._set_entry(tab.entry_terrain_dem, str(dem))
        tab._set_entry(tab.entry_slope_dem, str(dem))
        tab.out_terrain.set(str(dem))
        tab.out_slope_class.set(str(dem))
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box:
            tab._handle_calc_slope_pct()
            tab._handle_classify_slope()
            self.assertEqual(tab_box.showwarning.call_count, 2)
            self.assertIn("請改選資料夾", tab_box.showwarning.call_args[0][1])
            tab_box.showinfo.assert_not_called()
        self.assertFalse(self.inspector.is_busy)
        self.assertEqual([p.name for p in self.data_dir.iterdir()], ["hill.tif"])

    def test_earthwork_writes_into_chosen_folder(self) -> None:
        tab = self.tab
        before = write_dem(self.data_dir / "before.tif", np.full((40, 60), 100.0))
        after_arr = np.full((40, 60), 100.0)
        after_arr[10:20, 10:30] += 2.0
        after = write_dem(self.data_dir / "after.tif", after_arr)
        tab._set_entry(tab.entry_dem_before, str(before))
        tab._set_entry(tab.entry_dem_after, str(after))
        tab.out_earthwork.set(str(self.custom_dir))
        message = self._run(tab._handle_calculate_earthwork)
        out_dir = self.custom_dir.resolve()
        self.assertIn(f"成果資料夾: {out_dir}", message)
        for name in ("earthwork_diff_dz.tif", "earthwork_summary_report.xlsx", "earthwork_diff_color.tif"):
            self.assertTrue((out_dir / name).exists(), f"缺少 {name}")
        self.assertFalse((self.data_dir / "after_土方成果").exists())

    def test_slope_classification_writes_into_chosen_folder(self) -> None:
        tab = self.tab
        tab._set_entry(tab.entry_slope_dem, str(self._sloped_dem("hill.tif")))
        tab.out_slope_class.set(str(self.custom_dir))
        message = self._run(tab._handle_classify_slope)
        self.assertIn(f"成果資料夾: {self.custom_dir.resolve()}", message)
        self.assertEqual(len(list(self.custom_dir.glob("*.xlsx"))), 1)
        self.assertFalse((self.data_dir / "hill_坡度分類成果").exists())

    def test_slope_aspect_contours_follow_chosen_folder_or_source(self) -> None:
        tab = self.tab
        dem = self._sloped_dem("hill.tif")
        tab._set_entry(tab.entry_terrain_dem, str(dem))

        # 留空：跟著來源檔走
        self._run(tab._handle_calc_slope_pct)
        self.assertTrue((self.data_dir / "hill_slope_pct.tif").exists())

        tab.out_terrain.set(str(self.custom_dir))
        message = self._run(tab._handle_calc_slope_pct)
        self.assertIn(str(self.custom_dir.resolve() / "hill_slope_pct.tif"), message)
        self._run(tab._handle_calc_aspect)
        self._run(tab._handle_generate_contours)
        for name in ("hill_slope_pct.tif", "hill_aspect.tif", "hill_contour_5m.shp"):
            self.assertTrue((self.custom_dir / name).exists(), f"缺少 {name}")
        self.assertFalse((self.data_dir / "hill_aspect.tif").exists())
        self.assertFalse((self.data_dir / "hill_contour_5m.shp").exists())

    def test_cross_sections_write_into_chosen_folder(self) -> None:
        tab = self.tab
        dem = self._sloped_dem("road_dem.tif")
        centerline = write_geojson(
            self.data_dir / "centerline.geojson", [({"name": "road"}, line((250020.0, 2650100.0), (250260.0, 2650100.0)))]
        )
        tab._set_entry(tab.entry_road_line, str(centerline))
        tab._set_entry(tab.entry_road_dem, str(dem))
        tab.out_sections.set(str(self.custom_dir))
        message = self._run(tab._handle_extract_cross_sections)
        self.assertIn(f"成果資料夾: {self.custom_dir.resolve()}", message)
        self.assertEqual(len(list(self.custom_dir.glob("*.csv"))), 1)
        self.assertEqual(len(list(self.custom_dir.glob("*.gpkg"))), 1)
        self.assertFalse((self.data_dir / "centerline_斷面成果").exists())

    def test_watershed_writes_into_chosen_folder(self) -> None:
        tab = self.tab
        arr = 200.0 - np.arange(60, dtype=float)[None, :] * 1.0 - np.arange(60, dtype=float)[:, None] * 1.0
        dem = write_dem(self.data_dir / "basin.tif", arr, top_y=2650300.0)
        tab._set_entry(tab.entry_ws_dem, str(dem))
        tab._set_entry(tab.entry_ws_pour, "250270, 2650030")
        tab.out_watershed.set(str(self.custom_dir))
        message = self._run(tab._handle_delineate_watershed)
        self.assertIn(f"成果資料夾: {self.custom_dir.resolve()}", message)
        polygon_name = message.split("向量多邊形: ")[1].split("\n")[0]
        self.assertTrue((self.custom_dir / polygon_name).exists())
        self.assertFalse((self.data_dir / "basin_集水區成果").exists())


if __name__ == "__main__":
    unittest.main()
