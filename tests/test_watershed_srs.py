"""集水區劃設的座標系處理 整合測試 (tests/test_watershed_srs.py)

1. 無標籤 DEM：依 assigned_srs 或範圍推測標上座標系後分析 (WhiteboxTools 讀不了無標籤 DEM)。
2. 經緯度 DEM：先重投影到 EPSG:3826，捕捉距離與面積才是公尺。
3. 出水口與 DEM 座標系不同 (pour_point_srs)、出水口在 DEM 範圍外、成果碎塊。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List, Tuple

import numpy as np

from core import geo_io
from core.crs_doctor import _read_raster_srs_metadata
from core.env_runner import ToolkitError, run_command
from core.terrain_lab import delineate_watershed_whitebox
from tests._support import IsolatedWorkspaceTestCase, write_dem

ORIGIN_X = 250000.0
TOP_Y = 2650200.0
CELL_SIZE = 5.0
ROWS = 40
COLS = 40
FULL_AREA_M2 = ROWS * COLS * CELL_SIZE * CELL_SIZE


def valley() -> np.ndarray:
    """V 形谷地：谷線在第 20 列，整體往東降低。"""
    row_idx, col_idx = np.indices((ROWS, COLS))
    return (np.abs(row_idx - 20) * 2.0 + (COLS - col_idx) * 1.0 + 100.0).astype(np.float32)


def cell_center(row: float, col: float) -> Tuple[float, float]:
    return ORIGIN_X + (col + 0.5) * CELL_SIZE, TOP_Y - (row + 0.5) * CELL_SIZE


def to_lonlat(x: float, y: float) -> Tuple[float, float]:
    """以 gdaltransform 把 TWD97 座標換成經緯度。"""
    result = run_command(["gdaltransform", "-s_srs", "EPSG:3826", "-t_srs", "EPSG:4326"], input_text=f"{x} {y}\n")
    lon, lat = result.stdout.split()[:2]
    return float(lon), float(lat)


class WatershedSrsTest(IsolatedWorkspaceTestCase):
    """[A-04] 集水區劃設的座標系。"""

    def setUp(self) -> None:
        super().setUp()
        self.outlet = cell_center(20, COLS - 2)

    def _epsg(self, path: Path) -> object:
        if path.suffix.lower() == ".tif":
            return _read_raster_srs_metadata(path).epsg
        return geo_io.identify_epsg_from_wkt(geo_io.read_layer_info(path).srs_wkt)

    def _leftover_temp_files(self) -> List[str]:
        return sorted(p.name for p in self.work_temp.iterdir() if p.name.startswith("_watershed_"))

    def test_unlabelled_dem_uses_guessed_srs(self) -> None:
        dem = write_dem(self.data_dir / "無標籤.tif", valley(), epsg=None)
        messages: List[str] = []

        result = delineate_watershed_whitebox(dem, self.outlet, output_dir=self.out_dir, text_callback=messages.append)

        self.assertGreater(result.catchment_area_m2, FULL_AREA_M2 * 0.8)
        self.assertEqual(self._epsg(result.watershed_polygon), 3826)
        self.assertEqual(self._epsg(result.watershed_raster), 3826)
        self.assertTrue(any(message.startswith("[注意]") for message in messages))
        # 來源 DEM 不可被改動 (仍然沒有標籤)
        self.assertFalse(_read_raster_srs_metadata(dem).has_embedded_srs)
        self.assertEqual(self._leftover_temp_files(), [])

    def test_unlabelled_dem_with_assigned_srs(self) -> None:
        dem = write_dem(self.data_dir / "無標籤67.tif", valley(), epsg=None)

        result = delineate_watershed_whitebox(dem, self.outlet, output_dir=self.out_dir, assigned_srs="TWD67_121")

        self.assertGreater(result.catchment_area_m2, FULL_AREA_M2 * 0.8)
        info = geo_io.read_layer_info(result.watershed_polygon, "watershed")
        # 成果標成 TWD67 (GRS67 橢球長半徑 6378160)，座標值不變
        self.assertIn("6378160", info.srs_wkt or "")
        self.assertNotEqual(self._epsg(result.watershed_polygon), 3826)
        self.assertAlmostEqual(info.extent[0], ORIGIN_X, delta=CELL_SIZE * COLS)

    def test_lonlat_dem_is_reprojected_before_analysis(self) -> None:
        source = write_dem(self.data_dir / "tm2.tif", valley())
        lonlat_dem = self.data_dir / "經緯度.tif"
        run_command(["gdalwarp", "-t_srs", "EPSG:4326", "-r", "bilinear", str(source), str(lonlat_dem)])
        outlet_lonlat = to_lonlat(*self.outlet)
        messages: List[str] = []

        result = delineate_watershed_whitebox(lonlat_dem, outlet_lonlat, output_dir=self.out_dir, text_callback=messages.append)

        # 面積以平方公尺計 (經緯度直接算會是 1e-6 量級)
        self.assertGreater(result.catchment_area_m2, FULL_AREA_M2 * 0.6)
        self.assertLess(result.catchment_area_m2, FULL_AREA_M2 * 1.2)
        self.assertEqual(self._epsg(result.watershed_polygon), 3826)
        self.assertTrue(any("重投影" in message for message in messages))
        self.assertEqual(self._leftover_temp_files(), [])

    def test_pour_point_in_other_srs(self) -> None:
        dem = write_dem(self.data_dir / "valley.tif", valley())
        same_srs = delineate_watershed_whitebox(dem, self.outlet, output_dir=self.out_dir / "a")

        result = delineate_watershed_whitebox(dem, to_lonlat(*self.outlet), output_dir=self.out_dir / "b", pour_point_srs="WGS84")

        self.assertAlmostEqual(result.catchment_area_m2, same_srs.catchment_area_m2, places=1)

    def test_pour_point_outside_dem_is_rejected(self) -> None:
        dem = write_dem(self.data_dir / "valley.tif", valley())

        with self.assertRaises(ToolkitError) as caught:
            delineate_watershed_whitebox(dem, (self.outlet[0] + 5000.0, self.outlet[1]), output_dir=self.out_dir)
        self.assertIn("不在 DEM 範圍內", str(caught.exception))

        # 把經緯度當成 TWD97 輸入是最常見的錯
        with self.assertRaises(ToolkitError):
            delineate_watershed_whitebox(dem, (121.0, 24.0), output_dir=self.out_dir)
        self.assertEqual(self._leftover_temp_files(), [])

    def test_diagonal_catchment_is_one_polygon(self) -> None:
        dem = write_dem(self.data_dir / "valley.tif", valley())
        # 左上角山坡上的點：上游只有沿對角線排列的幾格
        result = delineate_watershed_whitebox(dem, cell_center(2, 2), output_dir=self.out_dir, snap_distance_m=0.0)

        info = geo_io.read_layer_info(result.watershed_polygon, "watershed")
        self.assertEqual(info.feature_count, 1)
        self.assertGreater(result.catchment_area_m2, CELL_SIZE * CELL_SIZE)

    def test_negative_snap_distance_is_rejected(self) -> None:
        dem = write_dem(self.data_dir / "valley.tif", valley())
        with self.assertRaises(ValueError):
            delineate_watershed_whitebox(dem, self.outlet, output_dir=self.out_dir, snap_distance_m=-1.0)


if __name__ == "__main__":
    unittest.main()
