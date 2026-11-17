"""裁切引擎的來源座標系測試 (tests/test_clip_source_srs.py)

涵蓋 P1-2 (clip_master 部分) 與 P1-1 補充測試：
1. 來源有座標系時不得被強制覆寫成目標座標系 (原本 clip_dem_layer / clip_ortho_layer 一律帶 -s_srs)。
2. 來源沒有座標系時依範圍推測，或採用 assigned_srs。
3. 來源為 EPSG:4326 的宗地，裁切後 AREA_M2 是平方公尺而不是平方度。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from core import crs_doctor, geo_io
from core.clip_master import BatchClipEngine, clip_dem_layer, clip_ortho_layer, clip_vector_layer
from core.crs_doctor import SourceSrsUndeterminedError
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    square,
    strip_prj,
    write_dem,
    write_geojson,
)


def lonlat_to_twd97(lon: float, lat: float) -> Tuple[float, float]:
    """把經緯度換算成 TWD97 TM2 (EPSG:3826) 座標，供測試決定裁切邊界的位置。"""
    from osgeo import osr

    geo_io.ensure_gdal_ready()
    source, target = osr.SpatialReference(), osr.SpatialReference()
    source.ImportFromEPSG(4326)
    target.ImportFromEPSG(3826)
    for srs in (source, target):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    x, y, _ = osr.CoordinateTransformation(source, target).TransformPoint(lon, lat)
    return x, y


class ClipVectorSourceSrsTest(IsolatedWorkspaceTestCase):
    """向量裁切：經緯度來源的面積單位、無標籤來源的推測與指派。"""

    # 橫跨東經 121 度 (TWD97 中央經線，X = 250000) 的宗地，寬高各 0.001 度，面積約 11,270 平方公尺
    LONLAT_PARCEL = square(120.9995, 24.0, 121.0005, 24.001)

    def setUp(self) -> None:
        super().setUp()
        # 只留 X ≥ 250000 的一半
        self.east_half = write_geojson(
            self.data_dir / "東半邊界.geojson", [({"id": 1}, square(250000, 2600000, 260000, 2700000))]
        )
        self.everything = write_geojson(
            self.data_dir / "大範圍邊界.geojson", [({"id": 1}, square(200000, 2600000, 300000, 2700000))]
        )

    def _shapefile(self, name: str, geometry: Dict[str, Any], epsg: int) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", [({"地號": "1"}, geometry)], epsg=epsg)
        return strip_prj(convert_vector(geojson, self.data_dir / f"{name}.shp"))

    def _assert_half_parcel_in_square_metres(self, output: Path) -> None:
        rows = attribute_rows(output)
        self.assertEqual(1, len(rows))
        row = rows[0]
        # 平方度的話會是 1e-6 的量級
        self.assertAlmostEqual(11270.0, row["ORIG_M2"], delta=150.0)
        self.assertAlmostEqual(5635.0, row["AREA_M2"], delta=80.0)
        self.assertAlmostEqual(0.5635, row["AREA_HA"], delta=0.008)
        self.assertAlmostEqual(50.0, row["RATIO_PCT"], delta=0.5)
        self.assertGreater(row["CENT_X"], 250000.0)
        self.assertEqual(3826, geo_io.read_layer_info(output).epsg)

    def test_wgs84_parcel_area_is_square_metres_after_clipping(self) -> None:
        parcels = write_geojson(self.data_dir / "經緯度宗地.geojson", [({"地號": "1"}, self.LONLAT_PARCEL)], epsg=4326)
        for suffix in (".gpkg", ".shp"):
            with self.subTest(output=suffix):
                output = self.out_dir / f"經緯度裁切{suffix}"
                result = clip_vector_layer(parcels, self.east_half, output)
                self.assertEqual(1, result.feature_count)
                self.assertAlmostEqual(5635.0, result.area_m2, delta=80.0)
                self._assert_half_parcel_in_square_metres(output)

    def test_unlabelled_lonlat_shapefile_is_guessed_and_reported(self) -> None:
        parcels = self._shapefile("無標籤經緯度宗地", self.LONLAT_PARCEL, 4326)
        messages: List[str] = []
        output = self.out_dir / "無標籤經緯度裁切.gpkg"
        clip_vector_layer(parcels, self.east_half, output, text_callback=messages.append)
        self._assert_half_parcel_in_square_metres(output)
        self.assertTrue(any("座標系為推測值" in message and "EPSG:4326" in message for message in messages))

    def test_unlabelled_tm2_shapefile_defaults_to_twd97(self) -> None:
        parcels = self._shapefile("無標籤TM2宗地", square(250000, 2650000, 250100, 2650100), 3826)
        messages: List[str] = []
        output = self.out_dir / "無標籤TM2裁切.gpkg"
        result = clip_vector_layer(parcels, self.everything, output, text_callback=messages.append)
        self.assertEqual(10000.0, result.area_m2)
        row = attribute_rows(output)[0]
        self.assertEqual((250050.0, 2650050.0), (row["CENT_X"], row["CENT_Y"]))
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_assigned_twd67_shifts_result_by_datum_offset(self) -> None:
        parcels = self._shapefile("無標籤67宗地", square(250000, 2650000, 250100, 2650100), 3826)
        messages: List[str] = []
        output = self.out_dir / "指派67裁切.gpkg"
        clip_vector_layer(parcels, self.everything, output, assigned_srs="TWD67_121", text_callback=messages.append)
        row = attribute_rows(output)[0]
        # TWD67 → TWD97 約往東 828 m、往南 207 m
        self.assertAlmostEqual(250050.0 + 828.0, row["CENT_X"], delta=30.0)
        self.assertAlmostEqual(2650050.0 - 207.0, row["CENT_Y"], delta=30.0)
        self.assertAlmostEqual(10000.0, row["AREA_M2"], delta=5.0)
        self.assertTrue(any("使用者指派" in message for message in messages))
        self.assertFalse(any("推測" in message for message in messages))

    def test_unlabelled_source_outside_known_ranges_is_rejected(self) -> None:
        parcels = self._shapefile("無標籤公釐宗地", square(250000000, 2650000000, 250100000, 2650100000), 3826)
        with self.assertRaises(SourceSrsUndeterminedError):
            clip_vector_layer(parcels, self.everything, self.out_dir / "不該產生.gpkg")


class ClipRasterSourceSrsTest(IsolatedWorkspaceTestCase):
    """網格裁切：不再強制把來源當成目標座標系。"""

    def setUp(self) -> None:
        super().setUp()
        self.center_x, self.center_y = lonlat_to_twd97(121.0, 24.0)
        self.boundary = write_geojson(
            self.data_dir / "邊界.geojson",
            [({"id": 1}, square(self.center_x - 50, self.center_y - 50, self.center_x + 50, self.center_y + 50))],
        )
        elevations = np.arange(400, dtype="float32").reshape(20, 20)
        # 經緯度 DEM：東經 120.999～121.001、北緯 23.999～24.001
        self.lonlat_dem = write_dem(
            self.data_dir / "經緯度DEM.tif", elevations, origin_x=120.999, top_y=24.001, cell_size=0.0001, epsg=4326
        )
        # TM2 DEM (無標籤)：以同一個中心點為中心，200 m 見方
        self.unlabelled_dem = write_dem(
            self.data_dir / "無標籤DEM.tif",
            elevations,
            origin_x=round(self.center_x) - 100.0,
            top_y=round(self.center_y) + 100.0,
            cell_size=10.0,
            epsg=None,
        )

    def _assert_output_covers_boundary(self, output: Path) -> None:
        decision = crs_doctor.decide_raster_source_srs(output)
        self.assertTrue(decision.has_embedded_srs)
        self.assertIn("TWD97", decision.source_label)
        self.assertAlmostEqual(self.center_x, decision.extent.center_x, delta=10.0)
        self.assertAlmostEqual(self.center_y, decision.extent.center_y, delta=10.0)
        self.assertAlmostEqual(100.0, decision.extent.width, delta=10.0)

    def test_dem_with_embedded_wgs84_is_reprojected_not_overridden(self) -> None:
        output = self.out_dir / "經緯度DEM裁切.tif"
        messages: List[str] = []
        result = clip_dem_layer(self.lonlat_dem, self.boundary, output, target_resolution=5.0, text_callback=messages.append)
        self.assertTrue(result.is_success)
        # -tap 把範圍對齊到解析度的整數倍，邊界不在格線上時會多出一列或一行
        for size in result.raster_dims:
            self.assertIn(size, (20, 21))
        self._assert_output_covers_boundary(output)
        self.assertFalse(any("推測" in message or "指派" in message for message in messages))

    def test_unlabelled_dem_is_guessed_as_twd97(self) -> None:
        output = self.out_dir / "無標籤DEM裁切.tif"
        messages: List[str] = []
        result = clip_dem_layer(self.unlabelled_dem, self.boundary, output, text_callback=messages.append)
        for size in result.raster_dims:
            self.assertIn(size, (10, 11))
        self._assert_output_covers_boundary(output)
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_unlabelled_dem_with_assigned_twd67_is_shifted_away_from_boundary(self) -> None:
        # 指派成 TWD67 後整張圖往東移約 828 m，與邊界不再重疊；沿用舊參數名 source_srs 也要有效
        for keyword in ("assigned_srs", "source_srs"):
            with self.subTest(keyword=keyword):
                shifted = write_geojson(
                    self.data_dir / f"平移邊界_{keyword}.geojson",
                    [({"id": 1}, square(self.center_x + 800, self.center_y - 240, self.center_x + 860, self.center_y - 180))],
                )
                output = self.out_dir / f"指派67_{keyword}.tif"
                result = clip_dem_layer(self.unlabelled_dem, shifted, output, **{keyword: "TWD67_121"})
                self.assertTrue(result.is_success)
                decision = crs_doctor.decide_raster_source_srs(output)
                self.assertAlmostEqual(self.center_x + 830, decision.extent.center_x, delta=10.0)

    def test_ortho_with_embedded_wgs84_and_unlabelled_source(self) -> None:
        output = self.out_dir / "經緯度影像裁切.tif"
        result = clip_ortho_layer(
            self.lonlat_dem, self.boundary, output, generate_pyramids=False, export_cad_background=False
        )
        self.assertTrue(result.is_success)
        self._assert_output_covers_boundary(output)

        messages: List[str] = []
        unlabelled_output = self.out_dir / "無標籤影像裁切.tif"
        clip_ortho_layer(
            self.unlabelled_dem,
            self.boundary,
            unlabelled_output,
            generate_pyramids=False,
            export_cad_background=False,
            text_callback=messages.append,
        )
        self._assert_output_covers_boundary(unlabelled_output)
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_batch_engine_passes_assigned_srs(self) -> None:
        geojson = write_geojson(
            self.data_dir / "無標籤宗地.geojson",
            [({"地號": "1"}, square(self.center_x - 850, self.center_y + 190, self.center_x - 810, self.center_y + 230))],
        )
        parcels = strip_prj(convert_vector(geojson, self.data_dir / "無標籤宗地.shp"))
        engine = BatchClipEngine(self.boundary, self.out_dir / "批次", assigned_srs="TWD67_121")
        results = engine.execute_batch([parcels])
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].is_success, results[0].error_message)
        # 以 TWD67 解讀後平移約 (+828, -207)，才會落進邊界內
        self.assertEqual(1, results[0].feature_count)
        self.assertAlmostEqual(1600.0, results[0].area_m2, delta=5.0)


if __name__ == "__main__":
    unittest.main()
