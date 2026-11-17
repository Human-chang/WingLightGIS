"""來源座標系決策測試 (tests/test_source_srs.py)

涵蓋 P1-2：來源有座標系就用來源的；沒有才依範圍推測 (經緯度 → EPSG:4326、臺灣 TM2 → EPSG:3826)
或採用使用者指派；兩者皆非時要求使用者指定。
"""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from core import crs_doctor, geo_io
from core.crs_doctor import Extent2D, SourceSrsUndeterminedError
from core.env_runner import ToolkitError
from tests._support import (
    IsolatedWorkspaceTestCase,
    convert_vector,
    square,
    strip_prj,
    total_area,
    write_dem,
    write_geojson,
)


class GuessSrsFromExtentTest(unittest.TestCase):
    """純數值的範圍推測規則。"""

    def test_lonlat_and_tm2_ranges(self) -> None:
        self.assertEqual("EPSG:4326", crs_doctor.guess_srs_from_extent(Extent2D(120.9, 23.9, 121.1, 24.1)))
        # 金門、東沙也在臺灣周邊的經緯度範圍內
        self.assertEqual("EPSG:4326", crs_doctor.guess_srs_from_extent(Extent2D(118.2, 24.4, 118.5, 24.5)))
        self.assertEqual("EPSG:4326", crs_doctor.guess_srs_from_extent(Extent2D(116.7, 20.6, 116.8, 20.8)))
        self.assertEqual("EPSG:3826", crs_doctor.guess_srs_from_extent(Extent2D(250000, 2650000, 250100, 2650100)))
        # 澎湖、金門、馬祖的 TM2 數值也在範圍內，一律猜 TWD97
        self.assertEqual("EPSG:3826", crs_doctor.guess_srs_from_extent(Extent2D(60000, 2600000, 61000, 2601000)))
        self.assertEqual("EPSG:3826", crs_doctor.guess_srs_from_extent(Extent2D(440000, 2900000, 445000, 2940000)))

    def test_out_of_range_extents_are_not_guessed(self) -> None:
        self.assertIsNone(crs_doctor.guess_srs_from_extent(None))
        # 公釐單位的 CAD 座標
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(250000000, 2650000000, 250100000, 2650100000)))
        # 只有一部分落在 TM2 範圍內
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(250000, 2650000, 500000, 2650100)))
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(0, 0, 1000, 1000)))

    def test_small_values_outside_taiwan_are_not_guessed_as_lonlat(self) -> None:
        # 自訂原點的 CAD 區域座標 (假設座標)：數值在 ±180／±90 內但不是經緯度
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(0, 0, 100, 50)))
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(-20.0, -15.0, 35.5, 42.0)))
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(-180.0, -90.0, 180.0, 90.0)))
        # 中心在臺灣周邊但四角超出臺灣周邊 (例如 100～140)，同樣不猜
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(100.0, 5.0, 140.0, 45.0)))
        self.assertIsNone(crs_doctor.guess_srs_from_extent(Extent2D(float("nan"), 0, 1, 1)))


class DecideSourceSrsTest(IsolatedWorkspaceTestCase):
    """以實際檔案驗證來源座標系決策。"""

    def _shapefile(self, name: str, geometry: dict, epsg: int, *, keep_prj: bool) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", [({"名稱": "甲"}, geometry)], epsg=epsg)
        shapefile = convert_vector(geojson, self.data_dir / f"{name}.shp")
        return shapefile if keep_prj else strip_prj(shapefile)

    def test_embedded_srs_is_used_without_s_srs(self) -> None:
        shapefile = self._shapefile("有座標系", square(250000, 2650000, 250100, 2650100), 3826, keep_prj=True)
        decision = crs_doctor.decide_source_srs(shapefile)
        self.assertTrue(decision.has_embedded_srs)
        self.assertFalse(decision.is_guess)
        self.assertIsNone(decision.s_srs)
        self.assertEqual([], decision.as_ogr_args())

        # 來源已有座標系時，指派值不採用
        overridden = crs_doctor.decide_source_srs(shapefile, "TWD67_121")
        self.assertEqual([], overridden.as_ogr_args())
        self.assertIn("不予採用", overridden.message)

    def test_unlabelled_tm2_shapefile_is_guessed_as_twd97(self) -> None:
        shapefile = self._shapefile("無標籤TM2", square(250000, 2650000, 250100, 2650100), 3826, keep_prj=False)
        decision = crs_doctor.decide_source_srs(shapefile)
        self.assertFalse(decision.has_embedded_srs)
        self.assertTrue(decision.is_guess)
        self.assertEqual(["-s_srs", "EPSG:3826"], decision.as_ogr_args())
        self.assertIn("座標系為推測值", decision.message)
        self.assertAlmostEqual(250000.0, decision.extent.min_x, places=3)

    def test_unlabelled_lonlat_shapefile_is_guessed_as_wgs84_and_reprojects(self) -> None:
        shapefile = self._shapefile("無標籤經緯度", square(121.0, 24.0, 121.001, 24.001), 4326, keep_prj=False)
        decision = crs_doctor.decide_source_srs(shapefile)
        self.assertTrue(decision.is_guess)
        self.assertEqual("EPSG:4326", decision.s_srs)
        self.assertIn("座標系為推測值", decision.message)

        staged = self.out_dir / "staged.gpkg"
        info = geo_io.stage_vector_to_gpkg(shapefile, staged, source_srs=decision.s_srs, target_srs="EPSG:3826")
        self.assertEqual(3826, info.epsg)
        # 0.001° × 0.001° 在北緯 24 度約 101.7 m × 110.8 m
        self.assertAlmostEqual(11260.0, total_area(staged), delta=150.0)

    def test_assigned_srs_is_used_for_unlabelled_source(self) -> None:
        shapefile = self._shapefile("無標籤67", square(250000, 2650000, 250100, 2650100), 3826, keep_prj=False)
        decision = crs_doctor.decide_source_srs(shapefile, "TWD67_121")
        self.assertFalse(decision.is_guess)
        self.assertEqual(crs_doctor.TWD67_121_PROJ, decision.s_srs)
        self.assertNotIn("推測", decision.message)

        by_code = crs_doctor.decide_source_srs(shapefile, "EPSG:3825")
        self.assertEqual(["-s_srs", "EPSG:3825"], by_code.as_ogr_args())

        with self.assertRaises(ToolkitError):
            crs_doctor.decide_source_srs(shapefile, "不是座標系")

    def test_embedded_twd67_label_uses_project_seven_parameters(self) -> None:
        geometry = square(250000, 2650000, 250100, 2650100)
        cases = (
            (3828, "TWD67_121", crs_doctor.TWD67_121_PROJ, "EPSG:3826", (0.5, 2.0)),
            (3827, "TWD67_119", crs_doctor.TWD67_119_PROJ, "EPSG:3825", (800.0, 900.0)),
        )
        for epsg, key, proj, target, default_gap_m in cases:
            with self.subTest(epsg=epsg):
                labelled = self._shapefile(f"有標籤{epsg}", geometry, epsg, keep_prj=True)
                decision = crs_doctor.decide_source_srs(labelled)
                self.assertTrue(decision.has_embedded_srs)
                self.assertFalse(decision.is_guess)
                self.assertEqual(["-s_srs", proj], decision.as_ogr_args())
                self.assertIn("七參數", decision.message)

                # 與「無標籤 + 指派 TWD67」的重投影結果一致
                unlabelled = self._shapefile(f"無標籤{epsg}", geometry, epsg, keep_prj=False)
                assigned = crs_doctor.decide_source_srs(unlabelled, key)
                from_label = geo_io.stage_vector_to_gpkg(
                    labelled, self.out_dir / f"label_{epsg}.gpkg", source_srs=decision.s_srs, target_srs=target
                )
                from_assigned = geo_io.stage_vector_to_gpkg(
                    unlabelled, self.out_dir / f"assigned_{epsg}.gpkg", source_srs=assigned.s_srs, target_srs=target
                )
                for got, expected in zip(from_label.extent, from_assigned.extent):
                    self.assertAlmostEqual(expected, got, places=4)

                # 不帶 -s_srs 時：3828→3826 PROJ 只做三參數平移 (與七參數差約 1 m)；
                # 3827→3825 PROJ 完全沒做基準面平移 (差約 830 m)
                by_proj_default = geo_io.stage_vector_to_gpkg(
                    labelled, self.out_dir / f"default_{epsg}.gpkg", target_srs=target
                )
                difference = max(abs(a - b) for a, b in zip(from_label.extent, by_proj_default.extent))
                low, high = default_gap_m
                self.assertGreater(difference, low)
                self.assertLess(difference, high)

    def test_embedded_twd67_raster_label_uses_project_seven_parameters(self) -> None:
        labelled = write_dem(self.data_dir / "有標籤67.tif", np.full((10, 10), 100.0), epsg=3828)
        decision = crs_doctor.decide_source_srs(labelled, "TWD97_121")
        self.assertTrue(decision.has_embedded_srs)
        self.assertEqual(["-s_srs", crs_doctor.TWD67_121_PROJ], decision.as_ogr_args())
        self.assertIn("不予採用", decision.message)

    def test_unlabelled_source_outside_known_ranges_requires_assignment(self) -> None:
        shapefile = self._shapefile("無標籤公釐", square(250000000, 2650000000, 250100000, 2650100000), 3826, keep_prj=False)
        with self.assertRaises(SourceSrsUndeterminedError) as caught:
            crs_doctor.decide_source_srs(shapefile)
        self.assertIn("請指定來源座標系", str(caught.exception))

        # 指派之後就不再拋錯
        self.assertEqual("EPSG:3826", crs_doctor.decide_source_srs(shapefile, "TWD97_121").s_srs)

    def test_raster_with_and_without_srs(self) -> None:
        elevations = np.full((10, 10), 100.0)
        labelled = write_dem(self.data_dir / "有座標系.tif", elevations)
        decision = crs_doctor.decide_source_srs(labelled)
        self.assertTrue(decision.has_embedded_srs)
        self.assertEqual([], decision.as_ogr_args())
        self.assertIn("TWD97", decision.source_label)

        unlabelled = write_dem(self.data_dir / "無座標系.tif", elevations, epsg=None)
        guessed = crs_doctor.decide_source_srs(unlabelled)
        self.assertFalse(guessed.has_embedded_srs)
        self.assertTrue(guessed.is_guess)
        self.assertEqual(["-s_srs", "EPSG:3826"], guessed.as_ogr_args())
        self.assertIn("座標系為推測值", guessed.message)

        assigned = crs_doctor.decide_raster_source_srs(unlabelled, "TWD67_121")
        self.assertFalse(assigned.is_guess)
        self.assertEqual(crs_doctor.TWD67_121_PROJ, assigned.s_srs)

        far_away = write_dem(self.data_dir / "範圍外.tif", elevations, epsg=None, origin_x=900000.0, top_y=100000.0)
        with self.assertRaises(SourceSrsUndeterminedError):
            crs_doctor.decide_source_srs(far_away)


if __name__ == "__main__":
    unittest.main()
