"""地形引擎的來源座標系測試 (tests/test_terrain_source_srs.py)

涵蓋 P1-2 (terrain_lab 部分)：
1. calculate_earthwork 不再強制把兩期 DEM 當成目標座標系；有座標系的來源會被重投影而不是被覆寫。
2. 沒有座標系的 DEM 與邊界依範圍推測，或採用 assigned_srs；無法判斷時拋錯。
3. generate_contours 對沒有座標系的 DEM 也能輸出 (原本只帶 -t_srs 會失敗)。
4. classify_slope_soil_conservation 的成果沿用 DEM 自己的座標系，不再一律標成目標座標系。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List

import numpy as np

from core import crs_doctor, geo_io
from core.crs_doctor import SourceSrsUndeterminedError
from core.env_runner import ToolkitError, run_json_command
from core.terrain_lab import calculate_earthwork, classify_slope_soil_conservation, generate_contours
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    square,
    strip_prj,
    write_dem,
    write_geojson,
)

# write_dem 預設範圍：X 250000～250100、Y 2650100～2650200 (20×20 格、5 m 網格)
DEM_MIN_X, DEM_MIN_Y, DEM_MAX_X, DEM_MAX_Y = 250000.0, 2650100.0, 250100.0, 2650200.0
# TWD67 → TWD97 約往東 828 m、往南 207 m
TWD67_SHIFT_X, TWD67_SHIFT_Y = 828.0, -207.0


def flat(value: float) -> np.ndarray:
    return np.full((20, 20), value, dtype=np.float32)


def ramp() -> np.ndarray:
    """往東每格升高 1 m 的斜面 (5 m 網格下為 20% 坡度)。"""
    return np.tile(np.arange(20, dtype=np.float32), (20, 1))


class EarthworkSourceSrsTest(IsolatedWorkspaceTestCase):
    """[A-01] 兩期 DEM 與邊界的來源座標系決策。"""

    def _dems(self, epsg) -> List[Path]:
        return [
            write_dem(self.data_dir / "前期.tif", flat(100.0), epsg=epsg),
            write_dem(self.data_dir / "後期.tif", flat(101.0), epsg=epsg),
        ]

    def _run(self, before: Path, after: Path, boundary=None, **options):
        return calculate_earthwork(
            before,
            after,
            boundary,
            output_diff_raster=self.out_dir / "高程差.tif",
            output_excel_path=self.out_dir / "土方.xlsx",
            **options,
        )

    def _unlabelled_boundary(self, name: str, geometry) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", [({"名稱": "計算範圍"}, geometry)])
        return strip_prj(convert_vector(geojson, self.data_dir / f"{name}.shp"))

    def test_unlabelled_dems_are_guessed_as_twd97(self) -> None:
        before, after = self._dems(None)
        messages: List[str] = []
        result = self._run(before, after, text_callback=messages.append)

        self.assertAlmostEqual(result.effective_area_m2, 10000.0, places=1)
        self.assertAlmostEqual(result.total_fill_m3, 10000.0, places=1)
        for name in ("前期.tif", "後期.tif"):
            self.assertTrue(any(name in message and "座標系為推測值" in message for message in messages), name)
        diff = crs_doctor.decide_raster_source_srs(result.diff_raster_path)
        self.assertAlmostEqual(diff.extent.center_x, 250050.0, delta=1.0)

    def test_embedded_zone_119_dems_are_reprojected_not_overridden(self) -> None:
        # 同樣的座標數值標成 119 分帶 (EPSG:3825)，實際位置在東經 119 度，換到 121 分帶後 X 約 4.6 萬
        before, after = self._dems(3825)
        messages: List[str] = []
        result = self._run(before, after, assigned_srs="TWD67_121", text_callback=messages.append)

        diff = crs_doctor.decide_raster_source_srs(result.diff_raster_path)
        self.assertTrue(diff.has_embedded_srs)
        self.assertIn("121", diff.source_label)
        self.assertLess(diff.extent.center_x, 100000.0)
        self.assertAlmostEqual(result.effective_area_m2, 10000.0, delta=1500.0)
        self.assertAlmostEqual(result.total_fill_m3, result.effective_area_m2, delta=1.0)
        self.assertAlmostEqual(result.max_fill_height_m, 1.0, places=2)
        # 來源已有座標系，指派的不採用，也不該出現推測訊息
        self.assertFalse(any("推測" in message or "使用者指派" in message for message in messages))

    def test_assigned_twd67_shifts_unlabelled_dems(self) -> None:
        before, after = self._dems(None)
        messages: List[str] = []
        result = self._run(before, after, assigned_srs="TWD67_121", text_callback=messages.append)

        diff = crs_doctor.decide_raster_source_srs(result.diff_raster_path)
        self.assertAlmostEqual(diff.extent.center_x, 250050.0 + TWD67_SHIFT_X, delta=30.0)
        self.assertAlmostEqual(diff.extent.center_y, 2650150.0 + TWD67_SHIFT_Y, delta=30.0)
        self.assertAlmostEqual(result.total_fill_m3, result.effective_area_m2, delta=1.0)
        self.assertTrue(any("使用者指派" in message for message in messages))
        self.assertFalse(any("推測" in message for message in messages))

    def test_unlabelled_dem_outside_known_ranges_is_rejected(self) -> None:
        before = write_dem(self.data_dir / "前期.tif", flat(100.0), origin_x=250000000.0, top_y=2650200000.0, epsg=None)
        after = write_dem(self.data_dir / "後期.tif", flat(101.0), origin_x=250000000.0, top_y=2650200000.0, epsg=None)
        with self.assertRaises(SourceSrsUndeterminedError):
            self._run(before, after)

    def test_unlabelled_shapefile_boundary_is_guessed(self) -> None:
        before, after = self._dems(3826)
        west_half = self._unlabelled_boundary("西半邊界", square(DEM_MIN_X, DEM_MIN_Y, DEM_MIN_X + 50.0, DEM_MAX_Y))
        messages: List[str] = []
        result = self._run(before, after, west_half, text_callback=messages.append)

        self.assertAlmostEqual(result.effective_area_m2, 5000.0, places=1)
        self.assertAlmostEqual(result.total_fill_m3, 5000.0, places=1)
        self.assertTrue(any("西半邊界.shp" in message and "座標系為推測值" in message for message in messages))
        # 有標籤的 DEM 不該出現座標系推測訊息 (色階圖的完成訊息會提到 .tif，不算)
        self.assertFalse(any(".tif" in message and "推測" in message for message in messages))

    def test_assigned_srs_applies_to_unlabelled_boundary_only(self) -> None:
        # 邊界以 TWD67 座標繪製 (無標籤)，兩期 DEM 自帶 TWD97：指派只套用在邊界上
        before, after = self._dems(3826)
        twd67_west_half = self._unlabelled_boundary(
            "舊座標西半邊界",
            square(
                DEM_MIN_X - TWD67_SHIFT_X,
                DEM_MIN_Y - TWD67_SHIFT_Y,
                DEM_MIN_X + 50.0 - TWD67_SHIFT_X,
                DEM_MAX_Y - TWD67_SHIFT_Y,
            ),
        )
        result = self._run(before, after, twd67_west_half, assigned_srs="TWD67_121")

        self.assertAlmostEqual(result.effective_area_m2, 5000.0, delta=1000.0)
        self.assertAlmostEqual(result.total_fill_m3, result.effective_area_m2, delta=1.0)
        diff = crs_doctor.decide_raster_source_srs(result.diff_raster_path)
        self.assertAlmostEqual(diff.extent.center_x, DEM_MIN_X + 25.0, delta=15.0)

    def test_boundary_in_other_srs_is_reprojected(self) -> None:
        # 經緯度邊界 (有標籤) 蓋住整張 DEM，結果應與不給邊界相同
        before, after = self._dems(3826)
        lonlat = write_geojson(
            self.data_dir / "經緯度邊界.geojson", [({"id": 1}, square(120.99, 23.95, 121.01, 23.97))], epsg=4326
        )
        result = self._run(before, after, lonlat)
        self.assertAlmostEqual(result.effective_area_m2, 10000.0, places=1)
        self.assertAlmostEqual(result.total_fill_m3, 10000.0, places=1)


class ContourSourceSrsTest(IsolatedWorkspaceTestCase):
    """[G-15] 等高線的來源座標系決策。"""

    def test_unlabelled_dem_is_guessed_and_exported(self) -> None:
        dem = write_dem(self.data_dir / "無標籤斜面.tif", ramp(), epsg=None)
        messages: List[str] = []
        for suffix in (".shp", ".dxf"):
            with self.subTest(output=suffix):
                output = self.out_dir / f"等高線{suffix}"
                generate_contours(dem, output, interval=5.0, text_callback=messages.append)
                self.assertGreater(geo_io.read_layer_info(output).feature_count, 0)
        info = geo_io.read_layer_info(self.out_dir / "等高線.shp")
        self.assertEqual(info.epsg, 3826)
        self.assertEqual(sorted(row["ELEV"] for row in attribute_rows(self.out_dir / "等高線.shp")), [5.0, 10.0, 15.0])
        self.assertAlmostEqual(info.extent[0], DEM_MIN_X + 27.5, delta=5.0)
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_embedded_zone_119_dem_is_reprojected(self) -> None:
        dem = write_dem(self.data_dir / "119斜面.tif", ramp(), epsg=3825)
        output = self.out_dir / "等高線119.gpkg"
        generate_contours(dem, output, interval=5.0, assigned_srs="WGS84")

        info = geo_io.read_layer_info(output)
        self.assertEqual(info.epsg, 3826)
        self.assertEqual(info.feature_count, 3)
        self.assertLess(info.extent[2], 100000.0)

    def test_assigned_twd67_shifts_contours(self) -> None:
        dem = write_dem(self.data_dir / "無標籤67斜面.tif", ramp(), epsg=None)
        output = self.out_dir / "等高線67.gpkg"
        generate_contours(dem, output, interval=5.0, assigned_srs="TWD67_121")

        info = geo_io.read_layer_info(output)
        self.assertEqual(info.epsg, 3826)
        self.assertAlmostEqual(info.extent[0], DEM_MIN_X + 27.5 + TWD67_SHIFT_X, delta=30.0)

    def test_unlabelled_dem_outside_known_ranges_is_rejected(self) -> None:
        dem = write_dem(self.data_dir / "公釐斜面.tif", ramp(), origin_x=250000000.0, top_y=2650200000.0, epsg=None)
        with self.assertRaises(SourceSrsUndeterminedError):
            generate_contours(dem, self.out_dir / "不該產生.gpkg", interval=5.0)


class SlopeSourceSrsTest(IsolatedWorkspaceTestCase):
    """[A-02] 坡度分級成果沿用 DEM 的座標系。"""

    def _assert_labels(self, result, epsg: int, zone: str) -> None:
        info = geo_io.read_layer_info(result.polygon_vector_path, "slope_classes")
        self.assertEqual(info.epsg, epsg)
        self.assertGreater(info.feature_count, 0)
        raster = crs_doctor.decide_raster_source_srs(result.classified_raster)
        self.assertTrue(raster.has_embedded_srs)
        self.assertIn(zone, raster.source_label)
        # 百分比坡度圖也是成果，要帶同一個座標系標籤
        slope_pct = crs_doctor.decide_raster_source_srs(result.slope_pct_raster)
        self.assertTrue(slope_pct.has_embedded_srs)
        self.assertIn(zone, slope_pct.source_label)
        self.assertAlmostEqual(slope_pct.extent.center_x, 250050.0, delta=1.0)
        # 座標不動
        self.assertAlmostEqual(raster.extent.center_x, 250050.0, delta=1.0)
        self.assertAlmostEqual(result.total_area_m2, 10000.0, places=1)

    def test_embedded_zone_119_is_kept(self) -> None:
        dem = write_dem(self.data_dir / "119斜面.tif", ramp(), epsg=3825)
        result = classify_slope_soil_conservation(dem, output_dir=self.out_dir, assigned_srs="TWD97_121")
        self._assert_labels(result, 3825, "119")

    def test_unlabelled_dem_is_guessed_as_twd97(self) -> None:
        dem = write_dem(self.data_dir / "無標籤斜面.tif", ramp(), epsg=None)
        messages: List[str] = []
        result = classify_slope_soil_conservation(dem, output_dir=self.out_dir, text_callback=messages.append)
        self._assert_labels(result, 3826, "121")
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_unlabelled_dem_takes_assigned_zone_119(self) -> None:
        dem = write_dem(self.data_dir / "無標籤119斜面.tif", ramp(), epsg=None)
        result = classify_slope_soil_conservation(dem, output_dir=self.out_dir, assigned_srs="TWD97_119")
        self._assert_labels(result, 3825, "119")
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_slope_")], [])

    def test_unlabelled_dem_takes_assigned_twd67(self) -> None:
        dem = write_dem(self.data_dir / "無標籤67斜面.tif", ramp(), epsg=None)
        result = classify_slope_soil_conservation(dem, output_dir=self.out_dir, assigned_srs="TWD67_121")
        for raster in (result.slope_pct_raster, result.classified_raster):
            wkt = run_json_command(["gdalinfo", "-json", str(raster)]).get("coordinateSystem", {}).get("wkt", "")
            # GRS67 橢球長半徑 6378160
            self.assertIn("6378160", wkt, raster.name)

    def test_geographic_dem_is_rejected(self) -> None:
        # 經緯度 DEM 的水平單位是度、高程是公尺，百分比坡度與面積都會算錯
        dem = write_dem(
            self.data_dir / "經緯度斜面.tif", ramp(), origin_x=121.0, top_y=24.0, cell_size=0.00005, epsg=4326
        )
        with self.assertRaises(ToolkitError) as caught:
            classify_slope_soil_conservation(dem, output_dir=self.out_dir)
        self.assertIn("經緯度", str(caught.exception))
        self.assertEqual(list(self.out_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
