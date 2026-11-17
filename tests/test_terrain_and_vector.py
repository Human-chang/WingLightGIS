"""地形引擎整合測試 (tests/test_terrain_and_vector.py)

以合成 DEM 實際呼叫引擎函式 (會執行沙盒的 gdalwarp / gdal_contour / gdaldem / gdal_polygonize / whitebox_tools)，
並核對成果檔內的數值：
1. [A-01] 兩期 DEM 土方：相差 1 m 的兩張平面 DEM，填方體積 = 面積 × 1。
2. [G-15] 等高線：斜面 DEM 的等高線為線型、有 ELEV 欄位，DXF 帶 Z 值。
3. [A-02] 水保坡度分級：20% 斜面全部落在第 3 級，向量化圖層有圖徵。
4. [A-04] 集水區：V 形谷地 DEM 在谷口劃設，產出的集水區圖層有圖徵且面積大於 0。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List

import numpy as np
import openpyxl
from osgeo import ogr

from core import geo_io
from core.terrain_lab import (
    calculate_earthwork,
    classify_slope_soil_conservation,
    delineate_watershed_whitebox,
    generate_contours,
)
from tests._support import IsolatedWorkspaceTestCase, attribute_rows, write_dem

CELL_SIZE = 5.0
ORIGIN_X = 250000.0
TOP_Y = 2650200.0


def ramp_dem(rows: int = 20, cols: int = 20, rise_per_cell: float = 1.0) -> np.ndarray:
    """往東每格升高 rise_per_cell 公尺的斜面 (5 m 網格下 1 m/格 = 20% 坡度)。"""
    return np.tile(np.arange(cols, dtype=np.float32) * rise_per_cell, (rows, 1))


def read_line_z_values(vector_path: Path) -> List[float]:
    """讀出每條線第一個頂點的 Z 值。"""
    geo_io.ensure_gdal_ready()
    dataset = ogr.Open(str(vector_path))
    try:
        layer = dataset.GetLayer(0)
        return [feature.GetGeometryRef().GetZ(0) for feature in layer]
    finally:
        dataset = None


class EarthworkTest(IsolatedWorkspaceTestCase):
    """[A-01] 兩期 DEM 土方計算。"""

    def test_uniform_one_metre_fill(self) -> None:
        before = write_dem(self.data_dir / "前期.tif", np.full((20, 20), 100.0, dtype=np.float32))
        after = write_dem(self.data_dir / "後期.tif", np.full((20, 20), 101.0, dtype=np.float32))
        excel = self.out_dir / "土方.xlsx"

        result = calculate_earthwork(
            before, after, output_diff_raster=self.out_dir / "高程差.tif", output_excel_path=excel
        )

        # 20×20 格、5 m 網格 = 10000 m²，全區填高 1 m
        self.assertAlmostEqual(result.effective_area_m2, 10000.0, places=1)
        self.assertAlmostEqual(result.total_fill_m3, 10000.0, places=1)
        self.assertAlmostEqual(result.total_cut_m3, 0.0, places=2)
        self.assertAlmostEqual(result.net_volume_m3, 10000.0, places=1)
        self.assertAlmostEqual(result.max_fill_height_m, 1.0, places=2)
        self.assertAlmostEqual(result.max_cut_depth_m, 0.0, places=2)
        self.assertTrue(result.diff_raster_path.is_file())

        sheet = openpyxl.load_workbook(excel).active
        values = {sheet.cell(row=row, column=1).value: sheet.cell(row=row, column=2).value for row in range(8, 14)}
        self.assertAlmostEqual(values["總填方體積 (Fill Volume)"], 10000.0, places=1)
        self.assertAlmostEqual(values["最大填土高度 (Max Fill)"], 1.0, places=2)

    def test_cut_and_fill_halves(self) -> None:
        before = write_dem(self.data_dir / "前期.tif", np.full((20, 20), 100.0, dtype=np.float32))
        after_grid = np.full((20, 20), 100.0, dtype=np.float32)
        after_grid[:, :10] = 98.0
        after_grid[:, 10:] = 103.0
        after = write_dem(self.data_dir / "後期.tif", after_grid)

        result = calculate_earthwork(before, after, output_diff_raster=self.out_dir / "dz.tif", output_excel_path=self.out_dir / "dz.xlsx")

        self.assertAlmostEqual(result.total_cut_m3, 5000.0 * 2.0, places=1)
        self.assertAlmostEqual(result.total_fill_m3, 5000.0 * 3.0, places=1)
        self.assertAlmostEqual(result.max_cut_depth_m, 2.0, places=2)
        self.assertAlmostEqual(result.max_fill_height_m, 3.0, places=2)


class GenerateContoursTest(IsolatedWorkspaceTestCase):
    """[G-15] 等高線抽繪。"""

    def setUp(self) -> None:
        super().setUp()
        self.dem = write_dem(self.data_dir / "斜面.tif", ramp_dem())

    def test_shapefile_contours_are_lines_with_elev(self) -> None:
        output = self.out_dir / "等高線.shp"
        generate_contours(self.dem, output, interval=5.0)

        info = geo_io.read_layer_info(output)
        self.assertIn("Line", info.geometry_type)
        self.assertIn("ELEV", info.field_names)
        self.assertEqual(info.epsg, 3826)
        elevations = sorted(row["ELEV"] for row in attribute_rows(output))
        self.assertEqual(elevations, [5.0, 10.0, 15.0])

    def test_dxf_contours_carry_z(self) -> None:
        output = self.out_dir / "等高線.dxf"
        generate_contours(self.dem, output, interval=5.0)

        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(sorted(read_line_z_values(output)), [5.0, 10.0, 15.0])
        self.assertEqual({row["Layer"] for row in attribute_rows(output)}, {"CONTOUR"})

    def test_non_positive_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate_contours(self.dem, self.out_dir / "x.gpkg", interval=0)


class SlopeClassificationTest(IsolatedWorkspaceTestCase):
    """[A-02] 水保坡度分級。"""

    def test_twenty_percent_ramp_is_class_three(self) -> None:
        dem = write_dem(self.data_dir / "斜面.tif", ramp_dem())
        result = classify_slope_soil_conservation(dem, output_dir=self.out_dir)

        by_level = {item["level"]: item for item in result.class_statistics}
        # gdaldem -compute_edges 在四個角落的像元只算得出一半坡度 (10%，第 2 級)，其餘 396 格都是 20%
        self.assertAlmostEqual(by_level[3]["area_m2"], 9900.0, places=1)
        self.assertAlmostEqual(by_level[3]["ratio_pct"], 99.0, places=2)
        self.assertAlmostEqual(by_level[2]["area_m2"], 100.0, places=1)
        self.assertAlmostEqual(result.total_area_m2, 10000.0, places=1)

        info = geo_io.read_layer_info(result.polygon_vector_path, "slope_classes")
        self.assertGreater(info.feature_count, 0)
        self.assertEqual({row["SLOPE_CLASS"] for row in attribute_rows(result.polygon_vector_path, "slope_classes")}, {2, 3})
        self.assertTrue(result.excel_report_path.is_file())


class WatershedTest(IsolatedWorkspaceTestCase):
    """[A-04] 集水區劃設。"""

    def test_valley_outlet_gives_watershed_polygon(self) -> None:
        rows, cols = 40, 40
        row_idx, col_idx = np.indices((rows, cols))
        # V 形谷地：谷線在第 20 列，整體往東降低，出水口在東側谷口
        elevations = (np.abs(row_idx - 20) * 2.0 + (cols - col_idx) * 1.0 + 100.0).astype(np.float32)
        dem = write_dem(self.data_dir / "valley.tif", elevations)
        pour_x = ORIGIN_X + (cols - 2 + 0.5) * CELL_SIZE
        pour_y = TOP_Y - (20 + 0.5) * CELL_SIZE

        result = delineate_watershed_whitebox(dem, (pour_x, pour_y), output_dir=self.out_dir)

        info = geo_io.read_layer_info(result.watershed_polygon, "watershed")
        self.assertGreater(info.feature_count, 0)
        self.assertGreater(result.catchment_area_m2, 0.0)
        self.assertLessEqual(result.catchment_area_m2, rows * cols * CELL_SIZE * CELL_SIZE)
        self.assertAlmostEqual(result.catchment_area_ha, result.catchment_area_m2 / 10000.0, places=3)


if __name__ == "__main__":
    unittest.main()
