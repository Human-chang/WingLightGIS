"""步驟 8-2：裁切分頁的自動行為 (9-1 起三頁共用，勾選項各頁不同)。

1. 載入 DXF 自動線轉面；閉鎖容許改了按 Enter 才重做，沒改不重做。
2. Step 3 勾選項預設都不勾，清單有適用的圖資才可勾。
3. 右欄顯示原始檔的四角座標 (可複製)，「用這個範圍當邊界」沿用該檔的座標系。
4. 批次執行時邊界還沒建好，依 Step 2 的輸入自動建立。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core import geo_io
from tests._support import (
    SAMPLE_KML,
    IsolatedWorkspaceTestCase,
    convert_vector,
    line,
    square,
    total_area,
    write_dem,
    write_geojson,
    write_kml,
)
from tests.test_clip_ortho import write_ortho

# write_dem 預設範圍：X 250000～250100、Y 2650100～2650200 (20×20 格、5 m)


class TestClipTabAuto(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.tab = self.app.tab_clip
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

    def _log_text(self) -> str:
        return self.inspector.txt_terminal.get("1.0", "end")

    def _square_dxf(self, name: str, gap: float = 0.0) -> Path:
        """100 m 見方的四條邊界線 (圖層 BOUNDARY)；gap > 0 時右邊與左邊的線各少畫 gap 公尺 (兩個缺口)。

        要兩個缺口：自動閉鎖時「末點接回首點」一律會補，只有一個缺口會被當成首尾而不觸發閉鎖容許。
        """
        x0, y0, x1, y1 = 250000.0, 2650100.0, 250100.0, 2650200.0
        lines = [
            line((x0, y0), (x1, y0)),
            line((x1, y0 + gap), (x1, y1)),
            line((x1, y1), (x0, y1)),
            line((x0, y1 - gap), (x0, y0)),
        ]
        source = write_geojson(self.data_dir / f"{name}.geojson", [({"id": i}, g) for i, g in enumerate(lines)])
        return convert_vector(source, self.data_dir / f"{name}.dxf", dxf_layer_name="BOUNDARY")

    # ------------------------------------------------------------------ 1. DXF 自動線轉面
    def test_loading_dxf_converts_automatically(self) -> None:
        dxf = self._square_dxf("邊界")
        with mock.patch("ui.panel_inspector.messagebox") as box, mock.patch("ui.tab_clip_workflow.messagebox") as tab_box:
            self.tab._load_boundary_file(dxf)
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_not_called()  # 自動轉不跳成功對話框
        self.assertEqual(self.tab.opt_dxf_layer.get(), "BOUNDARY")
        self.assertIsNotNone(self.tab.master_boundary_gpkg)
        self.assertAlmostEqual(total_area(self.tab.master_boundary_gpkg), 10000.0, delta=1.0)
        self.assertIn("自動線轉面", self._log_text())

    def test_kml_boundary_lists_folders_and_switching_reimports(self) -> None:
        """K3：KMZ 邊界自動用第一個有面的資料夾，「圖層」選單列出全部資料夾；換成封閉路徑的資料夾就重做。"""
        kml = SAMPLE_KML.replace("121.50,25.01,0 121.53,25.01,0", "121.50,25.01,0 121.51,25.01,0 121.51,25.02,0 121.50,25.01,0")
        kmz = write_kml(self.data_dir / "範圍.kmz", kml)
        with mock.patch("ui.panel_inspector.messagebox") as box, mock.patch("ui.tab_clip_workflow.messagebox"):
            self.tab._load_boundary_file(kmz)
            self._pump_until_idle()
            box.showerror.assert_not_called()
            self.assertEqual(self.tab.opt_dxf_layer.get(), "工區範圍")
            self.assertEqual(self.tab.opt_dxf_layer.cget("values"), ["測試範圍", "會勘點", "工區範圍", "中心線"])
            big = total_area(self.tab.master_boundary_gpkg)
            self.tab.opt_dxf_layer.set("中心線")
            self.tab._handle_boundary_layer_chosen()
            self._pump_until_idle()
            box.showerror.assert_not_called()
        self.assertLess(total_area(self.tab.master_boundary_gpkg), big / 5)
        self.assertIn("已採用 範圍.kmz (圖層「中心線」)", self._log_text())
        # 換成只有一個圖層的檔：選單清回「只有一個圖層」
        single = convert_vector(
            write_geojson(self.tmp_dir / "s.geojson", [({"id": 1}, square(250000, 2650000, 250100, 2650100))]),
            self.data_dir / "工區.shp",
        )
        with mock.patch("ui.panel_inspector.messagebox"), mock.patch("ui.tab_clip_workflow.messagebox"):
            self.tab._load_boundary_file(single)
            self._pump_until_idle()
        self.assertEqual(self.tab.opt_dxf_layer.get(), "(只有一個圖層)")
        self.assertAlmostEqual(total_area(self.tab.master_boundary_gpkg), 10000.0, delta=1.0)

    def test_snap_tolerance_change_reconverts_only_when_changed(self) -> None:
        dxf = self._square_dxf("有缺口", gap=2.0)
        with mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._load_boundary_file(dxf)
            self._pump_until_idle()
            box.showerror.assert_called_once()  # 缺口 2 m 超過預設 0.5 m
            self.assertIn("閉鎖容許調大", box.showerror.call_args[0][1])
        self.assertIsNone(self.tab.master_boundary_gpkg)

        self.tab.entry_snap_tolerance.delete(0, "end")
        self.tab.entry_snap_tolerance.insert(0, "3")
        with mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_dxf_lines_to_poly(only_if_changed=True)
            self._pump_until_idle()
            box.showerror.assert_not_called()
        self.assertIsNotNone(self.tab.master_boundary_gpkg)

        starts = self._log_text().count("DXF 線轉面 (圖層 BOUNDARY)")
        self.tab._handle_dxf_lines_to_poly(only_if_changed=True)
        self._pump_until_idle()
        self.assertEqual(self._log_text().count("DXF 線轉面 (圖層 BOUNDARY)"), starts, "數值沒變不應重做")

    # ------------------------------------------------------------------ 2. 勾選項
    def test_option_checkboxes_follow_the_file_list(self) -> None:
        dem_tab, ortho_tab, shp_tab = self.app.tab_dem, self.app.tab_ortho, self.app.tab_shp
        self.assertEqual(set(dem_tab.option_checks), {"contour", "slope", "3d", "mosaic"})
        self.assertEqual(set(ortho_tab.option_checks), {"pyramids", "cad_bg"})
        self.assertEqual(set(shp_tab.option_checks), {"whole", "excel", "kmz"})
        for tab in (dem_tab, ortho_tab, shp_tab):
            for key, checkbox in tab.option_checks.items():
                self.assertEqual(checkbox.get(), 0, f"{key} 預設不勾")
                self.assertEqual(checkbox.cget("state"), "normal" if key == "kmz" else "disabled", key)

        dem_tab._add_input_files([write_dem(self.data_dir / "a.tif", np.ones((20, 20)))])
        self._pump_until_idle()
        for key in ("contour", "slope", "3d"):
            self.assertEqual(dem_tab.option_checks[key].cget("state"), "normal", key)
        self.assertEqual(dem_tab.option_checks["mosaic"].cget("state"), "disabled", "一幅 DEM 不能拼接")
        dem_tab._add_input_files([write_dem(self.data_dir / "b.tif", np.ones((20, 20)), origin_x=250100.0)])
        self._pump_until_idle()
        self.assertEqual(dem_tab.option_checks["mosaic"].cget("state"), "normal")

        ortho_tab._add_input_files([write_ortho(self.data_dir / "空拍.tif")])
        self._pump_until_idle()
        self.assertEqual(ortho_tab.option_checks["pyramids"].cget("state"), "normal")
        self.assertEqual(ortho_tab.option_checks["cad_bg"].cget("state"), "normal")

        dem_tab.option_checks["3d"].select()
        dem_tab._handle_clear_files()
        self.assertEqual(dem_tab.option_checks["3d"].get(), 0, "清單清空後不適用的勾選要取消")
        self.assertEqual(dem_tab.option_checks["3d"].cget("state"), "disabled")

    # ------------------------------------------------------------------ 3. 四角座標
    def test_corners_are_shown_copied_and_used_as_boundary(self) -> None:
        dem = write_dem(self.data_dir / "地形.tif", np.ones((20, 20)))
        self.tab._add_input_files([dem])
        self._pump_until_idle()

        corners = self.inspector.txt_corners.get("1.0", "end").strip().splitlines()
        self.assertEqual(
            corners,
            [
                "250000.000\t2650100.000",
                "250100.000\t2650100.000",
                "250100.000\t2650200.000",
                "250000.000\t2650200.000",
            ],
        )
        self.assertEqual(self.inspector.btn_use_extent.cget("state"), "normal")
        self.inspector._handle_copy_corners()
        self.assertEqual(self.app.clipboard_get().splitlines(), corners)

        with mock.patch("ui.panel_inspector.messagebox") as box:
            self.inspector._handle_use_extent()
            self._pump_until_idle()
            box.showerror.assert_not_called()
        self.assertEqual(self.tab.seg_boundary_mode.get(), "貼座標")
        self.assertAlmostEqual(total_area(self.tab.master_boundary_gpkg), 10000.0, delta=1.0)
        # 右欄改顯示邊界本身，按鈕停用
        self.assertEqual(self.inspector.btn_use_extent.cget("state"), "disabled")

    def test_use_extent_keeps_the_source_srs(self) -> None:
        # TWD67 標籤的 DEM：四角座標是 TWD67 數值，邊界要轉成 TWD97 (約往東 828 m、往南 207 m)
        dem = write_dem(self.data_dir / "地形67.tif", np.ones((20, 20)), epsg=3828)
        self.tab._add_input_files([dem])
        self._pump_until_idle()
        with mock.patch("ui.panel_inspector.messagebox") as box:
            self.inspector._handle_use_extent()
            self._pump_until_idle()
            box.showerror.assert_not_called()
        min_x, min_y, _max_x, _max_y = geo_io.read_layer_info(self.tab.master_boundary_gpkg).extent
        self.assertAlmostEqual(min_x, 250000.0 + 828.0, delta=5.0)
        self.assertAlmostEqual(min_y, 2650100.0 - 207.0, delta=5.0)

    # ------------------------------------------------------------------ 4. 執行時自動建立邊界
    def test_batch_builds_boundary_from_dxf_path_when_missing(self) -> None:
        dxf = self._square_dxf("手打路徑")
        dem = write_dem(self.data_dir / "地形.tif", np.full((20, 20), 50.0))
        out_dir = self.out_dir / "auto_boundary"
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._add_input_files([dem])
            self._pump_until_idle()
            # 路徑直接打進欄位，沒有載入 (不會自動線轉面)
            self.tab.entry_bound_file.insert(0, str(dxf))
            self.tab.entry_out_dir.delete(0, "end")
            self.tab.entry_out_dir.insert(0, str(out_dir))
            self.assertIsNone(self.tab.master_boundary_gpkg)

            self.tab._handle_start_batch_clip()
            self._pump_until_idle(timeout=300.0)
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            self.assertIn("失敗: 0 件", tab_box.showinfo.call_args[0][1])
        self.assertIsNotNone(self.tab.master_boundary_gpkg)
        self.assertIn("先依「2 裁切邊界」的輸入自動建立", self._log_text())
        # 假 DEM 是 5 m，預設的 1 m 比它細，已自動改成維持原圖 (K2)
        self.assertTrue((out_dir / "地形_dem.tif").exists())

    def test_batch_without_any_boundary_input_warns(self) -> None:
        dem = write_dem(self.data_dir / "地形.tif", np.ones((20, 20)))
        self.tab._add_input_files([dem])
        self._pump_until_idle()
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box:
            self.tab._handle_start_batch_clip()
            tab_box.showwarning.assert_called_once()
            self.assertIn("還沒有裁切邊界", tab_box.showwarning.call_args[0][1])
        self.assertFalse(self.inspector.is_busy)


if __name__ == "__main__":
    unittest.main()
