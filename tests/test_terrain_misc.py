"""地形引擎 P1-9 整合測試 (tests/test_terrain_misc.py)

1. 網格陣列讀寫 (_read_geotiff_to_numpy / _write_numpy_to_geotiff)：非正方形像元、沒有 NoData 的來源。
2. [A-02] 坡度分級的自訂門檻：等級代碼不是 1 與 6、順序顛倒、門檻不相接。
3. [A-01] 土方：兩期 DEM 範圍不同時只算重疊區、完全不重疊時拋錯、Excel 淨土方差備註。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Optional

import numpy as np
import openpyxl
from osgeo import gdal, osr

from core import geo_io
from core.env_runner import ToolkitError
from core.terrain_lab import (
    _read_geotiff_to_numpy,
    _write_numpy_to_geotiff,
    calculate_earthwork,
    classify_slope_soil_conservation,
)
from tests._support import IsolatedWorkspaceTestCase, attribute_rows, write_dem

ORIGIN_X = 250000.0
TOP_Y = 2650200.0


def write_rect_cell_dem(
    path: Path,
    elevations: np.ndarray,
    *,
    cell_x: float,
    cell_y: float,
    nodata: Optional[float] = None,
) -> Path:
    """寫出像元寬高不同的 GeoTIFF DEM (EPSG:3826)；nodata 為 None 時不設定 NoData。"""
    geo_io.ensure_gdal_ready()
    rows, cols = elevations.shape
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((ORIGIN_X, cell_x, 0.0, TOP_Y, 0.0, -cell_y))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3826)
    dataset.SetProjection(srs.ExportToWkt())
    band = dataset.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.WriteArray(elevations.astype("float32"))
    band.FlushCache()
    dataset = None
    return path


def ramp(rows: int, cols: int, rise_per_cell: float) -> np.ndarray:
    """往東每格升高 rise_per_cell 公尺的斜面。"""
    return np.tile(np.arange(cols, dtype=np.float32) * rise_per_cell, (rows, 1))


class RasterArrayIoTest(IsolatedWorkspaceTestCase):
    """網格陣列讀寫輔助函式。"""

    def test_read_keeps_first_row_and_rect_cell_size_without_nodata(self) -> None:
        grid = np.arange(12, dtype=np.float32).reshape(3, 4) + 0.123456
        dem = write_rect_cell_dem(self.data_dir / "長方格.tif", grid, cell_x=5.0, cell_y=2.0)

        matrix, header = _read_geotiff_to_numpy(dem)

        self.assertEqual(matrix.shape, (3, 4))
        np.testing.assert_array_equal(matrix, grid)
        self.assertEqual((header["nrows"], header["ncols"]), (3, 4))
        self.assertAlmostEqual(header["cellsize_x"], 5.0)
        self.assertAlmostEqual(header["cellsize_y"], 2.0)
        self.assertNotIn("nodata_value", header)

    def test_write_round_trip_keeps_position_values_and_srs(self) -> None:
        grid = np.arange(12, dtype=np.float32).reshape(3, 4) + 0.123456
        dem = write_rect_cell_dem(self.data_dir / "長方格.tif", grid, cell_x=5.0, cell_y=2.0, nodata=-9999.0)
        matrix, header = _read_geotiff_to_numpy(dem)
        self.assertEqual(header["nodata_value"], -9999.0)

        output = _write_numpy_to_geotiff(matrix, header, self.out_dir / "寫回.tif", nodata_value=-9999.0, target_srs="EPSG:3826")

        dataset = gdal.Open(str(output))
        try:
            self.assertEqual(dataset.GetGeoTransform(), (ORIGIN_X, 5.0, 0.0, TOP_Y, 0.0, -2.0))
            self.assertEqual(dataset.GetRasterBand(1).GetNoDataValue(), -9999.0)
            self.assertEqual(geo_io.identify_epsg_from_wkt(dataset.GetProjection()), 3826)
            np.testing.assert_array_equal(dataset.GetRasterBand(1).ReadAsArray(), grid)
        finally:
            dataset = None
        self.assertEqual(list(self.work_temp.glob("_temp_out_*")), [])

    def test_write_without_geotransform_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _write_numpy_to_geotiff(np.zeros((2, 2), dtype=np.float32), {"ncols": 2, "nrows": 2}, self.out_dir / "x.tif")


class SlopeCustomThresholdTest(IsolatedWorkspaceTestCase):
    """[A-02] 自訂坡度分級門檻。"""

    def test_custom_codes_cover_flat_and_steeper_than_top_limit(self) -> None:
        # 左半平地 (0%)、右半 20% 斜面；最高一級上限只寫到 15%，超過的仍歸最高一級
        grid = np.zeros((20, 40), dtype=np.float32)
        grid[:, 20:] = np.arange(20, dtype=np.float32) * 1.0
        dem = write_dem(self.data_dir / "半平半斜.tif", grid)

        result = classify_slope_soil_conservation(
            dem,
            thresholds=[(11, "平緩", 0.0, 5.0), (12, "陡", 5.0, 15.0)],
            output_dir=self.out_dir,
        )

        by_level = {item["level"]: item for item in result.class_statistics}
        self.assertEqual(set(by_level), {11, 12})
        self.assertAlmostEqual(by_level[11]["area_m2"] + by_level[12]["area_m2"], 20000.0, places=1)
        self.assertAlmostEqual(by_level[11]["ratio_pct"] + by_level[12]["ratio_pct"], 100.0, places=1)
        # 平地區至少有 18 整欄 (交界兩欄受斜面影響)，斜面區至少 18 整欄
        self.assertGreaterEqual(by_level[11]["area_m2"], 18 * 20 * 25.0)
        self.assertGreaterEqual(by_level[12]["area_m2"], 18 * 20 * 25.0)
        codes = {row["SLOPE_CLASS"] for row in attribute_rows(result.polygon_vector_path, "slope_classes")}
        self.assertEqual(codes, {11, 12})

    def test_code_one_as_steepest_class_is_not_treated_as_lowest(self) -> None:
        dem = write_dem(self.data_dir / "斜面.tif", ramp(20, 20, 1.0))

        result = classify_slope_soil_conservation(
            dem,
            thresholds=[(1, "陡 (> 15%)", 15.0, 999.0), (6, "緩 (≦ 15%)", 0.0, 15.0)],
            output_dir=self.out_dir,
        )

        by_level = {item["level"]: item for item in result.class_statistics}
        # 四個角落像元 10%，其餘 396 格 20%
        self.assertAlmostEqual(by_level[1]["area_m2"], 9900.0, places=1)
        self.assertAlmostEqual(by_level[6]["area_m2"], 100.0, places=1)
        self.assertAlmostEqual(result.total_area_m2, 10000.0, places=1)
        self.assertEqual([item["level"] for item in result.class_statistics], [6, 1])

    def test_rect_cell_area_uses_both_cell_sizes(self) -> None:
        dem = write_rect_cell_dem(self.data_dir / "長方格.tif", ramp(10, 10, 1.0), cell_x=5.0, cell_y=2.0, nodata=-9999.0)

        result = classify_slope_soil_conservation(dem, output_dir=self.out_dir)

        self.assertAlmostEqual(result.total_area_m2, 10 * 10 * 5.0 * 2.0, places=1)

    def test_invalid_thresholds_are_rejected(self) -> None:
        dem = write_dem(self.data_dir / "斜面.tif", ramp(10, 10, 1.0))
        invalid_sets = {
            "不相接": [(1, "甲", 0.0, 5.0), (2, "乙", 10.0, 30.0)],
            "代碼重複": [(1, "甲", 0.0, 5.0), (1, "乙", 5.0, 30.0)],
            "代碼為 0": [(0, "甲", 0.0, 5.0), (1, "乙", 5.0, 30.0)],
            "下限大於上限": [(1, "甲", 5.0, 0.0)],
        }
        for label, thresholds in invalid_sets.items():
            with self.subTest(label), self.assertRaises(ValueError):
                classify_slope_soil_conservation(dem, thresholds=thresholds, output_dir=self.out_dir)


class EarthworkExtentTest(IsolatedWorkspaceTestCase):
    """[A-01] 兩期 DEM 範圍不同。"""

    def test_only_overlap_is_compared_cell_by_cell(self) -> None:
        # 後期 DEM 往東偏 10 格 (50 m)，同一地點比前期高 1 m；重疊區 10 欄 × 20 列 = 5000 m²
        before = write_dem(self.data_dir / "前期.tif", ramp(20, 20, 1.0))
        after_grid = np.tile(np.arange(10, 30, dtype=np.float32) + 1.0, (20, 1))
        after = write_dem(self.data_dir / "後期.tif", after_grid, origin_x=ORIGIN_X + 50.0)
        diff_tif = self.out_dir / "高程差.tif"
        excel = self.out_dir / "土方.xlsx"

        result = calculate_earthwork(before, after, output_diff_raster=diff_tif, output_excel_path=excel)

        self.assertAlmostEqual(result.effective_area_m2, 5000.0, places=1)
        self.assertAlmostEqual(result.total_fill_m3, 5000.0, places=1)
        self.assertAlmostEqual(result.total_cut_m3, 0.0, places=2)
        self.assertAlmostEqual(result.max_fill_height_m, 1.0, places=2)

        dataset = gdal.Open(str(diff_tif))
        try:
            geotransform = dataset.GetGeoTransform()
            self.assertEqual((dataset.RasterXSize, dataset.RasterYSize), (10, 20))
            self.assertAlmostEqual(geotransform[0], ORIGIN_X + 50.0)
            self.assertAlmostEqual(geotransform[3], TOP_Y)
            np.testing.assert_allclose(dataset.GetRasterBand(1).ReadAsArray(), 1.0, atol=1e-4)
        finally:
            dataset = None

        sheet = openpyxl.load_workbook(excel).active
        notes = {sheet.cell(row=row, column=1).value: sheet.cell(row=row, column=4).value for row in range(8, 14)}
        net_note = notes["淨土方差 (Net Difference)"]
        self.assertIn("正值：填方大於挖方，土方不足需借土運入", net_note)
        self.assertIn("負值：挖方大於填方，有剩餘土方需外運", net_note)

    def test_non_overlapping_dems_are_rejected(self) -> None:
        before = write_dem(self.data_dir / "前期.tif", np.full((10, 10), 100.0, dtype=np.float32))
        after = write_dem(self.data_dir / "後期.tif", np.full((10, 10), 101.0, dtype=np.float32), origin_x=ORIGIN_X + 500.0)

        with self.assertRaises(ToolkitError):
            calculate_earthwork(before, after, output_diff_raster=self.out_dir / "dz.tif", output_excel_path=self.out_dir / "dz.xlsx")


if __name__ == "__main__":
    unittest.main()
