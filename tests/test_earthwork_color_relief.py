"""土方紅藍色階圖 整合測試 (tests/test_earthwork_color_relief.py)

calculate_earthwork 產出高程差圖後，
以 gdaldem color-relief 輸出紅藍雙色色階圖 (挖方紅、填方藍、無變化白、NoData 透明)。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, List

import numpy as np

from core import crs_doctor, geo_io
from core.env_runner import ToolkitError
from core.terrain_lab import calculate_earthwork, render_earthwork_color_relief
from tests._support import IsolatedWorkspaceTestCase, write_dem


def read_bands(path: Path) -> Any:
    from osgeo import gdal

    geo_io.ensure_gdal_ready()
    dataset = gdal.Open(str(path))
    try:
        return dataset.ReadAsArray()
    finally:
        dataset = None


class EarthworkColorReliefTest(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.before = write_dem(self.data_dir / "前期.tif", np.full((20, 20), 100.0, dtype=np.float32))
        after = np.full((20, 20), 100.0, dtype=np.float32)
        after[:, :8] = 98.0    # 左側挖 2 m
        after[:, 12:] = 103.0  # 右側填 3 m
        after[0, 10] = -9999.0  # 中間無變化帶上的一格 NoData
        self.after = write_dem(self.data_dir / "後期.tif", after)

    def test_color_relief_is_written_next_to_diff_raster(self) -> None:
        messages: List[str] = []
        result = calculate_earthwork(
            self.before,
            self.after,
            output_diff_raster=self.out_dir / "工區_dz.tif",
            output_excel_path=self.out_dir / "工區.xlsx",
            text_callback=messages.append,
        )

        self.assertEqual(result.color_raster_path, self.out_dir / "工區_color.tif")
        self.assertTrue(result.color_raster_path.is_file())
        self.assertAlmostEqual(result.color_range_m, 3.0, places=3)
        bands = read_bands(result.color_raster_path)
        self.assertEqual(bands.shape, (4, 20, 20))
        self.assertEqual(bands.dtype, np.uint8)
        red, green, blue, alpha = (bands[i].astype(int) for i in range(4))
        # 填 3 m = 色階上限：深藍
        self.assertEqual((red[5, 15], green[5, 15], blue[5, 15], alpha[5, 15]), (33, 102, 172, 255))
        # 挖 2 m = 色階的 -2/3：偏紅，介於淺紅與深紅之間
        self.assertGreater(red[5, 3], blue[5, 3] + 80)
        self.assertTrue(178 <= red[5, 3] <= 239)
        # 無變化：白
        self.assertEqual((red[5, 10], green[5, 10], blue[5, 10], alpha[5, 10]), (255, 255, 255, 255))
        # NoData：全透明
        self.assertEqual(alpha[0, 10], 0)
        self.assertEqual(int(np.count_nonzero(alpha == 0)), 1)
        # 色階圖與高程差圖同座標系、同範圍
        color_meta = crs_doctor._read_raster_srs_metadata(result.color_raster_path)
        diff_meta = crs_doctor._read_raster_srs_metadata(result.diff_raster_path)
        self.assertEqual(color_meta.epsg, 3826)
        self.assertEqual(color_meta.extent, diff_meta.extent)
        self.assertTrue(any("紅色為挖方" in m and "±3.00 m" in m for m in messages))
        # 不留下色階表暫存檔
        self.assertEqual([p.name for p in self.work_temp.iterdir() if "dzcolor" in p.name], [])

    def test_custom_color_path_and_default_name_without_dz_suffix(self) -> None:
        custom = self.out_dir / "圖" / "挖填分佈.tif"
        result = calculate_earthwork(
            self.before,
            self.after,
            output_diff_raster=self.out_dir / "高程差.tif",
            output_excel_path=self.out_dir / "a.xlsx",
            output_color_raster=custom,
        )
        self.assertEqual(result.color_raster_path, custom)
        self.assertTrue(custom.is_file())

        result = calculate_earthwork(
            self.before,
            self.after,
            output_diff_raster=self.out_dir / "高程差.tif",
            output_excel_path=self.out_dir / "a.xlsx",
        )
        self.assertEqual(result.color_raster_path, self.out_dir / "高程差_color.tif")
        self.assertTrue(result.color_raster_path.is_file())

    def test_color_path_must_differ_from_diff_raster(self) -> None:
        same = self.out_dir / "dz.tif"
        with self.assertRaises(ToolkitError):
            calculate_earthwork(
                self.before, self.after, output_diff_raster=same, output_excel_path=self.out_dir / "a.xlsx",
                output_color_raster=same,
            )
        self.assertFalse(same.exists())

    def test_no_change_renders_all_white(self) -> None:
        same_after = write_dem(self.data_dir / "未變動.tif", np.full((20, 20), 100.0, dtype=np.float32))
        result = calculate_earthwork(
            self.before, same_after, output_diff_raster=self.out_dir / "dz.tif", output_excel_path=self.out_dir / "a.xlsx"
        )
        self.assertEqual(result.total_cut_m3, 0.0)
        self.assertEqual(result.total_fill_m3, 0.0)
        bands = read_bands(result.color_raster_path)
        self.assertTrue(np.all(bands == 255))

    def test_render_clamps_values_beyond_range(self) -> None:
        result = calculate_earthwork(
            self.before, self.after, output_diff_raster=self.out_dir / "dz.tif", output_excel_path=self.out_dir / "a.xlsx"
        )
        narrow = render_earthwork_color_relief(result.diff_raster_path, self.out_dir / "窄色階.tif", 1.0)
        bands = read_bands(narrow).astype(int)
        # 範圍只給 ±1 m：挖 2 m 與填 3 m 都維持兩端的顏色
        self.assertEqual(tuple(bands[:3, 5, 3]), (178, 24, 43))
        self.assertEqual(tuple(bands[:3, 5, 15]), (33, 102, 172))


if __name__ == "__main__":
    unittest.main()
