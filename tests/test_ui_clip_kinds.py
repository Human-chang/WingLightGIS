"""步驟 9-1：DEM／正射影像／SHP 三個獨立裁切分頁。

1. 三頁各有自己的清單與邊界；右欄跟著分頁切換 (沒東西的頁是空白，切回來恢復)。
2. 類型不符的檔移出清單並提示該到哪一頁。
3. 輸出座標系選單：預設 TWD97 121；選 TWD67 時用七參數，成果座標約往西 828 m、往北 207 m。
"""

from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

import numpy as np

from core import geo_io
from tests._support import IsolatedWorkspaceTestCase, square, write_dem, write_geojson
from tests.test_clip_ortho import write_ortho

SQUARE_TEXT = "250000 2650100\n250100 2650100\n250100 2650200\n250000 2650200\n"


class TestClipKinds(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy and all(t._light_pending == 0 for t in self.app.clip_tabs.values()):
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _switch(self, tab: Any) -> None:
        self.app.select_tool(tab.kind.key)

    def _paste_boundary(self, tab: Any) -> None:
        tab.txt_coords.delete("1.0", "end")
        tab.txt_coords.insert("1.0", SQUARE_TEXT)
        tab._handle_create_boundary_from_text()
        self._pump_until_idle()
        self.assertIsNotNone(tab.master_boundary_gpkg)

    def test_three_tabs_with_their_own_state_and_inspector(self) -> None:
        self.assertEqual(
            list(self.app.clip_tabs), ["dem", "ortho", "shp"]
        )
        dem_tab, shp_tab = self.app.tab_dem, self.app.tab_shp
        self.assertTrue(dem_tab.is_active)

        with mock.patch("ui.panel_inspector.messagebox"):
            self._paste_boundary(dem_tab)
        self.assertEqual(dem_tab.master_boundary_gpkg.name, "master_boundary_dem.gpkg")
        dem_image = self.inspector.current_preview_image
        self.assertIsNotNone(dem_image)
        dem_caption = self.inspector.lbl_preview_caption.cget("text")
        self.assertIn("10,000.00 m²", self.inspector.diag_value_labels["閉鎖面積:"].cget("text"))

        # 切到 SHP 頁：右欄是空白，SHP 頁沒有邊界
        self._switch(shp_tab)
        self.assertFalse(dem_tab.is_active)
        self.assertIsNone(shp_tab.master_boundary_gpkg)
        self.assertIsNone(self.inspector.current_preview_image)
        self.assertEqual(self.inspector.lbl_preview_caption.cget("text"), "")
        self.assertEqual(self.inspector.diag_value_labels["來源圖資:"].cget("text"), "尚未載入圖資")
        self.assertEqual(self.inspector.txt_corners.get("1.0", "end").strip(), "")

        # SHP 頁自己建一個邊界，不會覆蓋 DEM 頁的檔
        with mock.patch("ui.panel_inspector.messagebox"):
            self._paste_boundary(shp_tab)
        self.assertEqual(shp_tab.master_boundary_gpkg.name, "master_boundary_shp.gpkg")
        self.assertTrue(dem_tab.master_boundary_gpkg.exists())

        # 切回 DEM 頁：右欄恢復 DEM 頁的內容
        self._switch(dem_tab)
        self.assertEqual(self.inspector.current_preview_image, dem_image)
        self.assertEqual(self.inspector.lbl_preview_caption.cget("text"), dem_caption)
        self.assertIn("10,000.00 m²", self.inspector.diag_value_labels["閉鎖面積:"].cget("text"))

    def test_wrong_kind_files_are_removed_with_a_hint(self) -> None:
        dem_tab, ortho_tab = self.app.tab_dem, self.app.tab_ortho
        vector = write_geojson(self.data_dir / "宗地.geojson", [({"n": 1}, square(250000, 2650100, 250050, 2650150))])
        ortho = write_ortho(self.data_dir / "空拍.tif")
        dem = write_dem(self.data_dir / "地形.tif", np.ones((20, 20)))

        with mock.patch("ui.tab_clip_workflow.messagebox") as box:
            dem_tab._add_input_files([vector, ortho, dem])
            self._pump_until_idle()
            box.showwarning.assert_called_once()
            message = box.showwarning.call_args[0][1]
        self.assertEqual([p.name for p in dem_tab.selected_input_files], ["地形.tif"])
        self.assertIn("宗地.geojson：這是SHP 向量圖，請到「SHP 裁切」", message)
        self.assertIn("空拍.tif：這是正射影像，請到「正射影像裁切」", message)

        with mock.patch("ui.tab_clip_workflow.messagebox") as box:
            ortho_tab._add_input_files([dem])
            self._pump_until_idle()
            self.assertIn("請到「DEM／DSM 裁切」", box.showwarning.call_args[0][1])
        self.assertEqual(ortho_tab.selected_input_files, [])

    def test_run_waits_until_types_are_detected(self) -> None:
        tab = self.app.tab_dem
        dem = write_dem(self.data_dir / "地形.tif", np.ones((20, 20)))
        tab.selected_input_files.append(dem.resolve())  # 還沒判別
        with mock.patch("ui.panel_inspector.messagebox"):
            self._paste_boundary(tab)
        with mock.patch("ui.tab_clip_workflow.messagebox") as box:
            tab._handle_start_batch_clip()
            box.showwarning.assert_called_once()
            self.assertIn("還在判別", box.showwarning.call_args[0][1])
        self.assertFalse(self.inspector.is_busy)

    def test_output_srs_reaches_engine_and_twd67_uses_seven_parameters(self) -> None:
        tab = self.app.tab_dem
        dem = write_dem(self.data_dir / "地形.tif", np.full((20, 20), 50.0))
        with mock.patch("ui.panel_inspector.messagebox"), mock.patch("ui.tab_clip_workflow.messagebox") as box:
            tab._add_input_files([dem])
            self._paste_boundary(tab)
            tab.entry_out_dir.delete(0, "end")
            tab.entry_out_dir.insert(0, str(self.out_dir / "twd67"))
            tab.combo_dem_res.set("5")

            self.assertEqual(tab.opt_output_srs.get(), "TWD97 121 分帶 (EPSG:3826，臺灣本島)")
            tab.opt_output_srs.set("TWD67 121 分帶 (七參數轉換)")
            tab._handle_start_batch_clip()
            self._pump_until_idle(timeout=300.0)
            box.showinfo.assert_called_once()
            self.assertIn("失敗: 0 件", box.showinfo.call_args[0][1])

        from osgeo import gdal

        geo_io.ensure_gdal_ready()
        dataset = gdal.Open(str(self.out_dir / "twd67" / "地形_dem_5m.tif"))
        try:
            origin_x, _, _, top_y, _, _ = dataset.GetGeoTransform()
        finally:
            dataset = None
        # TWD97 → TWD67：約往西 828 m、往北 207 m
        self.assertAlmostEqual(origin_x, 250000.0 - 828.0, delta=10.0)
        self.assertAlmostEqual(top_y, 2650200.0 + 207.0, delta=10.0)

    def test_shp_output_in_twd97_119(self) -> None:
        tab = self.app.tab_shp
        self._switch(tab)
        parcels = write_geojson(self.data_dir / "宗地.geojson", [({"n": 1}, square(250000, 2650100, 250100, 2650200))])
        with mock.patch("ui.panel_inspector.messagebox"), mock.patch("ui.tab_clip_workflow.messagebox"):
            tab._add_input_files([parcels])
            self._paste_boundary(tab)
            tab.opt_output_srs.set("TWD97 119 分帶 (EPSG:3825，澎湖金門馬祖)")
            with mock.patch("ui.tab_clip_workflow.BatchClipEngine") as engine_cls:
                engine_cls.return_value.execute_batch.return_value = []
                tab._handle_start_batch_clip()
                self._pump_until_idle()
        self.assertEqual(engine_cls.call_args.kwargs["target_srs"], "EPSG:3825")


if __name__ == "__main__":
    unittest.main()
