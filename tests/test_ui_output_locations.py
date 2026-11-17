"""成果輸出位置一致化的測試（成果不寫進暫存區）。"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from typing import List
from unittest import mock

import numpy as np

from core import env_runner
from tests._support import IsolatedWorkspaceTestCase, convert_vector, line, square, write_dem, write_geojson
from ui.output_paths import default_clip_output_dir, result_folder_beside


class TestOutputPathRules(IsolatedWorkspaceTestCase):
    def test_result_folder_sits_beside_source(self) -> None:
        source = self.data_dir / "工區 DEM.tif"
        self.assertEqual(result_folder_beside(source, "土方成果"), self.data_dir.resolve() / "工區 DEM_土方成果")
        self.assertEqual(result_folder_beside(str(source), " 斷面成果 "), self.data_dir.resolve() / "工區 DEM_斷面成果")
        self.assertFalse((self.data_dir / "工區 DEM_土方成果").exists(), "只回傳路徑，不該先建立資料夾")
        with self.assertRaises(ValueError):
            result_folder_beside(source, "  ")

    def test_default_output_dir_is_outside_workspace_temp(self) -> None:
        self.assertEqual(env_runner.get_default_output_dir(), self.default_output_dir.resolve())
        self.assertFalse(self.default_output_dir.exists(), "取得預設成果資料夾不該自動建立它")
        clip_default = default_clip_output_dir()
        self.assertEqual(clip_default, self.default_output_dir.resolve() / "clipped_outputs")
        self.assertNotIn(self.work_temp.resolve(), clip_default.parents)

        env_runner.set_default_output_dir(None)
        try:
            real_default = env_runner.get_default_output_dir()
            self.assertEqual(real_default, env_runner.get_toolkit_root() / "outputs")
            self.assertNotIn(env_runner.get_toolkit_root() / "workspace_temp", [real_default, *real_default.parents])
        finally:
            env_runner.set_default_output_dir(self.default_output_dir)


class TestToolOutputsFollowSource(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.app.update()

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

    def _deliverables_in_temp(self) -> List[str]:
        """暫存區裡留下的成果類檔案 (報表、成果圖層)；中繼檔不算。"""
        return sorted(p.name for p in self.work_temp.rglob("*") if p.suffix.lower() in (".xlsx", ".csv"))

    def _sloped_dem(self, name: str) -> Path:
        rows, cols = 40, 60
        arr = 100.0 + np.arange(cols, dtype=float)[None, :] * 0.8 + np.arange(rows, dtype=float)[:, None] * 0.3
        return write_dem(self.data_dir / name, arr)

    def test_clip_tab_default_output_is_not_in_temp(self) -> None:
        default_text = self.app.tab_clip.entry_out_dir.get()
        self.assertEqual(Path(default_text), self.default_output_dir.resolve() / "clipped_outputs")
        self.assertNotIn(self.work_temp.resolve(), Path(default_text).parents)
        self.assertFalse(self.default_output_dir.exists(), "還沒執行裁切前不該建立預設成果資料夾")

    def test_cadastral_report_goes_beside_cadastre_file(self) -> None:
        tab = self.app.tab_vector
        boundary = write_geojson(self.data_dir / "boundary.geojson", [({"name": "site"}, square(250050, 2650000, 250150, 2650100))])
        parcels_src = write_geojson(
            self.data_dir / "parcels.geojson",
            [
                ({"段名": "大安段", "地號": "1"}, square(250000, 2650000, 250100, 2650100)),
                ({"段名": "信義段", "地號": "2"}, square(250100, 2650000, 250200, 2650100)),
            ],
        )
        parcels = convert_vector(parcels_src, self.data_dir / "parcels.gpkg")
        tab._set_entry(tab.entry_inter_bound, str(boundary))
        tab._set_entry(tab.entry_inter_cad, str(parcels))
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            tab._handle_run_cadastral_intersection()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]
        out_dir = self.data_dir.resolve() / "parcels_地籍交集成果"
        self.assertIn(f"成果資料夾: {out_dir}", message)
        self.assertTrue((out_dir / "parcels_工程用地清冊報表.xlsx").exists())
        self.assertTrue((out_dir / "parcels_intersected_parcels.gpkg").exists())
        self.assertEqual(self._deliverables_in_temp(), [])

    def test_earthwork_outputs_go_beside_after_dem(self) -> None:
        tab = self.app.tab_terrain
        before = write_dem(self.data_dir / "before.tif", np.full((40, 60), 100.0))
        after_arr = np.full((40, 60), 100.0)
        after_arr[10:20, 10:30] += 2.0
        after_arr[25:35, 35:50] -= 1.0
        after = write_dem(self.data_dir / "after.tif", after_arr)
        tab._set_entry(tab.entry_dem_before, str(before))
        tab._set_entry(tab.entry_dem_after, str(after))
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            tab._handle_calculate_earthwork()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]
        out_dir = self.data_dir.resolve() / "after_土方成果"
        self.assertIn(f"成果資料夾: {out_dir}", message)
        for name in ("earthwork_diff_dz.tif", "earthwork_summary_report.xlsx", "earthwork_diff_color.tif"):
            self.assertTrue((out_dir / name).exists(), f"缺少 {name}")
        self.assertEqual(self._deliverables_in_temp(), [])
        self.assertFalse((self.work_temp / "earthwork_diff_dz.tif").exists())

    def test_slope_classification_outputs_go_beside_dem(self) -> None:
        tab = self.app.tab_terrain
        dem = self._sloped_dem("hill.tif")
        tab._set_entry(tab.entry_slope_dem, str(dem))
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            tab._handle_classify_slope()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]
        out_dir = self.data_dir.resolve() / "hill_坡度分類成果"
        self.assertIn(f"成果資料夾: {out_dir}", message)
        self.assertEqual(len(list(out_dir.glob("*.xlsx"))), 1)
        self.assertEqual(self._deliverables_in_temp(), [])

    def test_cross_section_outputs_go_beside_centerline(self) -> None:
        tab = self.app.tab_terrain
        dem = self._sloped_dem("road_dem.tif")
        centerline = write_geojson(
            self.data_dir / "centerline.geojson", [({"name": "road"}, line((250020.0, 2650100.0), (250260.0, 2650100.0)))]
        )
        tab._set_entry(tab.entry_road_line, str(centerline))
        tab._set_entry(tab.entry_road_dem, str(dem))
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            tab._handle_extract_cross_sections()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]
        out_dir = self.data_dir.resolve() / "centerline_斷面成果"
        self.assertIn(f"成果資料夾: {out_dir}", message)
        self.assertEqual(len(list(out_dir.glob("*.csv"))), 1)
        self.assertEqual(len(list(out_dir.glob("*.gpkg"))), 1)
        self.assertEqual(self._deliverables_in_temp(), [])

    def test_watershed_outputs_go_beside_dem(self) -> None:
        tab = self.app.tab_terrain
        rows, cols = 60, 60
        # 向東南傾斜的坡面，出水口放在低處
        arr = 200.0 - np.arange(cols, dtype=float)[None, :] * 1.0 - np.arange(rows, dtype=float)[:, None] * 1.0
        dem = write_dem(self.data_dir / "basin.tif", arr, top_y=2650300.0)
        tab._set_entry(tab.entry_ws_dem, str(dem))
        tab._set_entry(tab.entry_ws_pour, "250270, 2650030")
        with mock.patch("ui.lab.tab_terrain_hydro.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            tab._handle_delineate_watershed()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]
        out_dir = self.data_dir.resolve() / "basin_集水區成果"
        self.assertIn(f"成果資料夾: {out_dir}", message)
        polygon_name = message.split("向量多邊形: ")[1].split("\n")[0]
        self.assertTrue((out_dir / polygon_name).exists())
        self.assertFalse((self.work_temp / polygon_name).exists())


if __name__ == "__main__":
    unittest.main()
