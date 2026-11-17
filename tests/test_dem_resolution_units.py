"""DEM 解析度單位 整合測試 (tests/test_dem_resolution_units.py)

來源 DEM 與目標座標系的單位不同 (度 ↔ 公尺) 且沒指定目標解析度時，
clip_dem_layer／calculate_earthwork 不可把來源解析度的數值直接當成目標單位。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List, Tuple

import numpy as np

from core.clip_master import clip_dem_layer
from core.crs_doctor import decide_source_srs, get_raster_resolution, resolve_warp_resolution, srs_is_geographic
from core.env_runner import ToolkitError, run_command, run_json_command
from core.terrain_lab import calculate_earthwork
from tests._support import IsolatedWorkspaceTestCase, square, write_dem, write_geojson

ORIGIN_X = 250000.0
TOP_Y = 2650200.0
CELL_SIZE = 5.0
ROWS = 40
COLS = 40


def slope_surface(offset: float = 0.0) -> np.ndarray:
    row_idx, col_idx = np.indices((ROWS, COLS))
    return (col_idx * 0.5 + row_idx * 0.2 + 100.0 + offset).astype(np.float32)


def raster_size(path: Path) -> Tuple[int, int]:
    size = run_json_command(["gdalinfo", "-json", str(path)])["size"]
    return int(size[0]), int(size[1])


class DemResolutionUnitsTest(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tm2_dem = write_dem(self.data_dir / "tm2.tif", slope_surface())
        self.lonlat_dem = self._to_lonlat(self.tm2_dem, "經緯度.tif")
        # DEM 範圍 X 250000～250200、Y 2650000～2650200；邊界取中間 100 m 見方
        self.boundary = write_geojson(
            self.data_dir / "邊界.geojson", [({"NAME": "工區"}, square(250050.0, 2650050.0, 250150.0, 2650150.0))]
        )

    def _to_lonlat(self, source: Path, name: str) -> Path:
        target = self.data_dir / name
        run_command(["gdalwarp", "-t_srs", "EPSG:4326", "-r", "bilinear", str(source), str(target)])
        return target

    def test_lonlat_dem_clipped_to_tm2_gets_metre_resolution(self) -> None:
        messages: List[str] = []
        output = self.out_dir / "裁切.tif"

        result = clip_dem_layer(self.lonlat_dem, self.boundary, output, text_callback=messages.append)

        self.assertTrue(result.is_success)
        xres, yres = get_raster_resolution(output)
        self.assertEqual(xres, yres)
        self.assertGreater(xres, 4.0)
        self.assertLess(xres, 7.0)
        cols, rows = raster_size(output)
        # 100 m 見方、約 5 m 網格：約 20 格 (舊程式以 0.00005 公尺為網格，會要求兩百萬格見方)
        self.assertTrue(15 <= cols <= 26 and 15 <= rows <= 26, (cols, rows))
        self.assertTrue(any("原生解析度" in message for message in messages))
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_warp_resolution_")], [])

    def test_explicit_resolution_is_used_as_is(self) -> None:
        output = self.out_dir / "裁切10m.tif"

        clip_dem_layer(self.lonlat_dem, self.boundary, output, target_resolution=10.0)

        self.assertEqual(get_raster_resolution(output), (10.0, 10.0))
        # 邊界座標經過來回重投影有浮點誤差，-tap 對齊時可能多出一列
        cols, rows = raster_size(output)
        self.assertTrue(cols in (10, 11) and rows in (10, 11), (cols, rows))

    def test_same_unit_keeps_source_resolution(self) -> None:
        messages: List[str] = []
        output = self.out_dir / "裁切原解析度.tif"

        clip_dem_layer(self.tm2_dem, self.boundary, output, text_callback=messages.append)

        self.assertEqual(get_raster_resolution(output), (5.0, 5.0))
        self.assertEqual(raster_size(output), (20, 20))
        self.assertFalse(any("原生解析度" in message for message in messages))

    def test_tm2_dem_clipped_to_lonlat_gets_degree_resolution(self) -> None:
        output = self.out_dir / "裁切經緯度.tif"

        clip_dem_layer(self.tm2_dem, self.boundary, output, target_srs="EPSG:4326")

        xres, _ = get_raster_resolution(output)
        # 5 m 約為 0.000045～0.00005 度 (舊程式以 5 度為網格，整張只剩 1 格)
        self.assertGreater(xres, 0.00003)
        self.assertLess(xres, 0.00007)
        cols, rows = raster_size(output)
        self.assertTrue(15 <= cols <= 26 and 15 <= rows <= 26, (cols, rows))

    def test_resolve_warp_resolution_rejects_non_positive(self) -> None:
        decision = decide_source_srs(self.tm2_dem)
        with self.assertRaises(ValueError):
            resolve_warp_resolution(self.tm2_dem, decision, "EPSG:3826", 0.0)
        self.assertTrue(srs_is_geographic("EPSG:4326"))
        self.assertFalse(srs_is_geographic("EPSG:3826"))

    def test_earthwork_on_lonlat_dems_is_in_cubic_metres(self) -> None:
        after_tm2 = write_dem(self.data_dir / "tm2_after.tif", slope_surface(offset=1.0))
        after_lonlat = self._to_lonlat(after_tm2, "經緯度_後期.tif")

        result = calculate_earthwork(
            self.lonlat_dem,
            after_lonlat,
            self.boundary,
            output_diff_raster=self.out_dir / "dz.tif",
            output_excel_path=self.out_dir / "土方.xlsx",
        )

        # 邊界 100 m 見方、全區填高 1 m：填方約 10000 m³
        self.assertGreater(result.cell_size_m, 4.0)
        self.assertLess(result.cell_size_m, 7.0)
        self.assertAlmostEqual(result.effective_area_m2, 10000.0, delta=1500.0)
        self.assertAlmostEqual(result.total_fill_m3, result.effective_area_m2, delta=result.effective_area_m2 * 0.02)
        self.assertAlmostEqual(result.total_cut_m3, 0.0, delta=1.0)
        self.assertAlmostEqual(result.mean_dz_m, 1.0, delta=0.02)

    def test_earthwork_rejects_geographic_target(self) -> None:
        after_tm2 = write_dem(self.data_dir / "tm2_after.tif", slope_surface(offset=1.0))
        with self.assertRaises(ToolkitError):
            calculate_earthwork(self.tm2_dem, after_tm2, target_srs="EPSG:4326", output_diff_raster=self.out_dir / "dz.tif")


if __name__ == "__main__":
    unittest.main()
