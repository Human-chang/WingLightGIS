"""座標診斷測試 (tests/test_crs_diagnosis.py)

涵蓋 P1-8 第一段：diagnose_extent 的臺灣範圍判斷，以及 sniff_crs 對實際檔案讀到的標籤與燈號。
重點：有標籤時依標籤判斷 (119 分帶、馬祖、非臺灣的合法座標系不再誤判)；
沒有標籤時與 guess_srs_from_extent 的推測規則一致。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from core import crs_doctor
from core.crs_doctor import Extent2D, diagnose_extent
from core.env_runner import ToolkitError
from tests._support import (
    IsolatedWorkspaceTestCase,
    convert_vector,
    line,
    square,
    strip_prj,
    write_dem,
    write_geojson,
)

# 本島、澎湖 (以 119 分帶表示)、馬祖 (119 分帶，Y 超過舊上限 2,850,000)
MAIN_ISLAND = Extent2D(250000.0, 2650000.0, 251000.0, 2651000.0)
PENGHU_ZONE_119 = Extent2D(305000.0, 2605000.0, 306000.0, 2606000.0)
MATSU_ZONE_119 = Extent2D(345000.0, 2897000.0, 346000.0, 2898000.0)
# 澎湖以 121 分帶表示的數值 (舊程式把這段 X 範圍當成「119 分帶」)
PENGHU_ZONE_121 = Extent2D(100000.0, 2605000.0, 101000.0, 2606000.0)

# ArcMap 寫出的 .prj 沒有 AUTHORITY 節點
ESRI_TWD67_PRJ = (
    'PROJCS["TWD_1967_TM_Taiwan",GEOGCS["GCS_TWD_1967",DATUM["D_TWD_1967",'
    'SPHEROID["GRS_1967_Truncated",6378160.0,298.25]],PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",250000.0],PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",121.0],PARAMETER["Scale_Factor",0.9999],'
    'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
)
# 自訂的 TM2 定義：沒有基準面資訊，對應不到任何 EPSG 代碼
CUSTOM_TM2_PRJ = (
    'PROJCS["unnamed",GEOGCS["GRS 67(IUGG 1967)",DATUM["unknown",SPHEROID["GRS67",6378160,298.247167427]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",121],PARAMETER["scale_factor",0.9999],'
    'PARAMETER["false_easting",250000],PARAMETER["false_northing",0],UNIT["Meter",1]]'
)


class DiagnoseExtentTest(unittest.TestCase):
    """純數值與標籤的診斷規則。"""

    def assert_diag(self, result: crs_doctor.CrsDiagnosisResult, level: str, code: str) -> None:
        self.assertEqual((level, code), (result.status_level, result.status_code), result.status_message)

    def test_labelled_tm2_zones_are_judged_by_label(self) -> None:
        self.assert_diag(diagnose_extent(MAIN_ISLAND, detected_epsg=3826), "GREEN", "TWD97_STANDARD")
        # 澎湖以 121 分帶表示是合法的 EPSG:3826，不可被改判成 119 分帶
        penghu_121 = diagnose_extent(PENGHU_ZONE_121, detected_epsg=3826)
        self.assert_diag(penghu_121, "GREEN", "TWD97_STANDARD")
        self.assertEqual(3826, penghu_121.detected_epsg)

        for extent in (PENGHU_ZONE_119, MATSU_ZONE_119):
            result = diagnose_extent(extent, detected_epsg=3825, raw_srs_name="TWD97 / TM2 zone 119")
            self.assert_diag(result, "GREEN", "TWD97_119_STANDARD")
            # 標籤是 3825 就回報 3825，不可因名稱含 TWD97 而被改成 3826
            self.assertEqual(3825, result.detected_epsg)
            self.assertEqual("TWD97 / TM2 zone 119", result.raw_srs_name)

    def test_labelled_twd67_recommends_seven_parameters_of_its_own_zone(self) -> None:
        zone_121 = diagnose_extent(MAIN_ISLAND, detected_epsg=3828)
        self.assert_diag(zone_121, "YELLOW", "TWD67_NEED_7PARAM")
        self.assertEqual(crs_doctor.TWD67_121_PROJ, zone_121.recommended_assign_srs)
        self.assertEqual("EPSG:3826", zone_121.recommended_transform_srs)
        self.assertIn("自動", zone_121.status_message)

        zone_119 = diagnose_extent(MATSU_ZONE_119, detected_epsg=3827)
        self.assert_diag(zone_119, "YELLOW", "TWD67_NEED_7PARAM")
        self.assertEqual(crs_doctor.TWD67_119_PROJ, zone_119.recommended_assign_srs)
        self.assertEqual("EPSG:3825", zone_119.recommended_transform_srs)

    def test_legal_non_taiwan_crs_is_not_red(self) -> None:
        utm = diagnose_extent(Extent2D(300000, 2650000, 301000, 2651000), detected_epsg=32651, raw_srs_name="WGS 84 / UTM zone 51N")
        self.assert_diag(utm, "GREEN", "LABELED_OTHER_CRS")
        web_mercator = diagnose_extent(Extent2D(13500000, 2800000, 13501000, 2801000), detected_epsg=3857)
        self.assert_diag(web_mercator, "GREEN", "LABELED_OTHER_CRS")

        tokyo = diagnose_extent(Extent2D(139.7, 35.6, 139.8, 35.7), detected_epsg=4326)
        self.assert_diag(tokyo, "YELLOW", "LABELED_OUTSIDE_TAIWAN")
        self.assert_diag(diagnose_extent(Extent2D(121.5, 25.0, 121.55, 25.05), detected_epsg=4326), "GREEN", "WGS84_STANDARD")
        # TWD97 經緯度 (EPSG:3824) 與馬祖、金門的經緯度
        self.assert_diag(diagnose_extent(Extent2D(119.9, 26.1, 120.0, 26.2), detected_epsg=3824), "GREEN", "GEOGRAPHIC_STANDARD")
        self.assert_diag(diagnose_extent(Extent2D(118.3, 24.4, 118.4, 24.5), detected_epsg=4326), "GREEN", "WGS84_STANDARD")

        unidentified = diagnose_extent(MAIN_ISLAND, raw_srs_name="unnamed", has_embedded_srs=True, is_geographic=False)
        self.assert_diag(unidentified, "YELLOW", "LABELED_UNIDENTIFIED")

    def test_label_contradicting_the_coordinates_is_red(self) -> None:
        local = diagnose_extent(Extent2D(0, 0, 100, 100), detected_epsg=3826)
        self.assert_diag(local, "RED", "LABEL_EXTENT_MISMATCH")
        self.assertFalse(local.needs_unit_scaling)

        millimetres = diagnose_extent(Extent2D(250000000, 2650000000, 250100000, 2650100000), detected_epsg=3826)
        self.assert_diag(millimetres, "RED", "CAD_MM_SCALE")
        self.assertTrue(millimetres.needs_unit_scaling)

        self.assert_diag(diagnose_extent(MAIN_ISLAND, detected_epsg=4326), "RED", "LABEL_EXTENT_MISMATCH")

    def test_unlabelled_tm2_cannot_tell_zones_apart(self) -> None:
        for extent in (MAIN_ISLAND, PENGHU_ZONE_121, PENGHU_ZONE_119, MATSU_ZONE_119):
            result = diagnose_extent(extent)
            self.assert_diag(result, "YELLOW", "TM2_UNLABELED")
            self.assertEqual("EPSG:3826", result.recommended_assign_srs)
            self.assertIn("TWD97_119", result.status_message)
            self.assertIn("TWD67", result.status_message)
            self.assertEqual("無標籤", result.raw_srs_name)
            self.assertIsNone(result.detected_epsg)

    def test_unlabelled_small_values_and_unknown_ranges(self) -> None:
        self.assert_diag(diagnose_extent(Extent2D(121.5, 25.0, 121.55, 25.05)), "YELLOW", "WGS84_UNLABELED")
        # 自訂原點的 CAD 區域座標：不會被當成經緯度，處理前必須指派
        local = diagnose_extent(Extent2D(0, 0, 100, 50))
        self.assert_diag(local, "RED", "LONLAT_UNLABELED_OUTSIDE_TAIWAN")
        self.assertIn("不會自動當成經緯度", local.status_message)
        self.assertIn("指派", local.status_message)

        self.assert_diag(diagnose_extent(Extent2D(900000, 100000, 901000, 101000)), "RED", "CRS_OUT_OF_BOUNDS")
        self.assert_diag(diagnose_extent(Extent2D(float("nan"), 0, 1, 1)), "RED", "EXTENT_INVALID")

    def test_millimetre_detection_covers_offshore_islands(self) -> None:
        # 澎湖以 121 分帶表示再放大 1000 倍：X 只有 1 億，舊程式的門檻 (X 大於 1 億) 會漏掉
        for extent in (
            Extent2D(250000000, 2650000000, 250100000, 2650100000),
            Extent2D(99000000, 2605000000, 99100000, 2605100000),
        ):
            result = diagnose_extent(extent)
            self.assert_diag(result, "RED", "CAD_MM_SCALE")
            self.assertAlmostEqual(0.001, result.scale_factor)
        # 數值很大但縮小後也不在臺灣：不可硬說是公釐
        self.assert_diag(diagnose_extent(Extent2D(5e9, 5e9, 5.1e9, 5.1e9)), "RED", "CRS_OUT_OF_BOUNDS")

    def test_unlabelled_diagnosis_agrees_with_source_srs_guess(self) -> None:
        extents = (
            MAIN_ISLAND,
            MATSU_ZONE_119,
            Extent2D(121.5, 25.0, 121.55, 25.05),
            Extent2D(0, 0, 100, 50),
            Extent2D(900000, 100000, 901000, 101000),
            Extent2D(250000, 2650000, 500000, 2650100),
            Extent2D(250000000, 2650000000, 250100000, 2650100000),
        )
        for extent in extents:
            with self.subTest(extent=extent.as_tuple()):
                guessed = crs_doctor.guess_srs_from_extent(extent)
                result = diagnose_extent(extent)
                if guessed is None:
                    self.assertEqual("RED", result.status_level)
                    self.assertIsNone(result.recommended_transform_srs)
                elif result.status_level != "RED":
                    self.assertEqual(guessed, result.recommended_assign_srs)


class SniffCrsFileTest(IsolatedWorkspaceTestCase):
    """以實際檔案驗證 sniff_crs 讀到的標籤與燈號。"""

    def _shapefile(self, name: str, geometry: Dict[str, Any], epsg: int, *, prj: Optional[str] = None, keep_prj: bool = True) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", [({"名稱": "甲"}, geometry)], epsg=epsg)
        shapefile = convert_vector(geojson, self.data_dir / f"{name}.shp")
        if prj is not None:
            shapefile.with_suffix(".prj").write_text(prj, encoding="ascii")
        return shapefile if keep_prj else strip_prj(shapefile)

    def test_zone_119_label_is_reported_as_3825(self) -> None:
        geometry = square(345000, 2897000, 345100, 2897100)
        geojson = write_geojson(self.data_dir / "馬祖.geojson", [({"名稱": "甲"}, geometry)], epsg=3825)
        for suffix in (".geojson", ".gpkg", ".shp"):
            with self.subTest(source=suffix):
                source = geojson if suffix == ".geojson" else convert_vector(geojson, self.data_dir / f"馬祖{suffix}")
                result = crs_doctor.sniff_crs(source)
                self.assertEqual(("GREEN", "TWD97_119_STANDARD"), (result.status_level, result.status_code))
                self.assertEqual(3825, result.detected_epsg)
                self.assertEqual("TWD97 / TM2 zone 119", result.raw_srs_name)
                self.assertAlmostEqual(2897100.0, result.extent.max_y, places=2)

    def test_unlabelled_shapefile_is_yellow_and_matches_decision(self) -> None:
        shapefile = self._shapefile("無標籤", square(250000, 2650000, 250100, 2650100), 3826, keep_prj=False)
        result = crs_doctor.sniff_crs(shapefile)
        self.assertEqual(("YELLOW", "TM2_UNLABELED"), (result.status_level, result.status_code))
        self.assertIsNone(result.detected_epsg)
        self.assertEqual(crs_doctor.decide_source_srs(shapefile).s_srs, result.recommended_assign_srs)

    def test_arcmap_style_twd67_prj_is_recognised(self) -> None:
        shapefile = self._shapefile("ArcMap67", square(250000, 2650000, 250100, 2650100), 3828, prj=ESRI_TWD67_PRJ)
        result = crs_doctor.sniff_crs(shapefile)
        self.assertEqual("TWD67_NEED_7PARAM", result.status_code)
        self.assertEqual(3828, result.detected_epsg)
        # 診斷說「會自動套用七參數」，實際決策也必須如此
        self.assertEqual(crs_doctor.TWD67_121_PROJ, crs_doctor.decide_source_srs(shapefile).s_srs)

    def test_custom_definition_without_epsg_is_yellow(self) -> None:
        shapefile = self._shapefile("自訂定義", square(250000, 2650000, 250100, 2650100), 3826, prj=CUSTOM_TM2_PRJ)
        result = crs_doctor.sniff_crs(shapefile)
        self.assertEqual(("YELLOW", "LABELED_UNIDENTIFIED"), (result.status_level, result.status_code))
        self.assertIsNone(result.detected_epsg)

    def test_labelled_utm_shapefile_is_not_red(self) -> None:
        shapefile = self._shapefile("UTM", square(300000, 2650000, 300100, 2650100), 32651)
        result = crs_doctor.sniff_crs(shapefile)
        self.assertEqual(("GREEN", "LABELED_OTHER_CRS"), (result.status_level, result.status_code))
        self.assertEqual(32651, result.detected_epsg)

    def test_dxf_in_local_coordinates_and_in_millimetres(self) -> None:
        local = convert_vector(
            write_geojson(self.data_dir / "區域.geojson", [({"id": 1}, line((0, 0), (100, 50)))]),
            self.data_dir / "區域.dxf",
        )
        result = crs_doctor.sniff_crs(local)
        self.assertEqual(("RED", "LONLAT_UNLABELED_OUTSIDE_TAIWAN"), (result.status_level, result.status_code))
        self.assertEqual("無標籤", result.raw_srs_name)

        millimetres = convert_vector(
            write_geojson(
                self.data_dir / "公釐.geojson",
                [({"id": 1}, line((250000000, 2650000000), (250100000, 2650100000)))],
            ),
            self.data_dir / "公釐.dxf",
        )
        result = crs_doctor.sniff_crs(millimetres)
        self.assertEqual("CAD_MM_SCALE", result.status_code)
        self.assertTrue(result.needs_unit_scaling)

    def test_rasters(self) -> None:
        elevations = np.full((10, 10), 100.0)
        cases = (
            ("本島.tif", {}, "GREEN", "TWD97_STANDARD", 3826),
            ("無標籤.tif", {"epsg": None}, "YELLOW", "TM2_UNLABELED", None),
            ("馬祖67.tif", {"epsg": 3827, "origin_x": 345000.0, "top_y": 2897000.0}, "YELLOW", "TWD67_NEED_7PARAM", 3827),
            ("UTM.tif", {"epsg": 32651}, "GREEN", "LABELED_OTHER_CRS", 32651),
            ("經緯度.tif", {"epsg": 4326, "origin_x": 121.0, "top_y": 24.0, "cell_size": 0.0001}, "GREEN", "WGS84_STANDARD", 4326),
            ("範圍外.tif", {"epsg": None, "origin_x": 900000.0, "top_y": 100000.0}, "RED", "CRS_OUT_OF_BOUNDS", None),
        )
        for name, options, level, code, epsg in cases:
            with self.subTest(raster=name):
                result = crs_doctor.sniff_crs(write_dem(self.data_dir / name, elevations, **options))
                self.assertEqual((level, code), (result.status_level, result.status_code))
                self.assertEqual(epsg, result.detected_epsg)
                # 顯示用的名稱是座標系名稱而不是整段 WKT
                self.assertNotIn("[", result.raw_srs_name)

    def test_missing_file_and_empty_layer(self) -> None:
        with self.assertRaises(FileNotFoundError):
            crs_doctor.sniff_crs(self.data_dir / "不存在.shp")
        with self.assertRaises(FileNotFoundError):
            crs_doctor.sniff_crs(self.data_dir / "不存在.tif")
        empty = write_geojson(self.data_dir / "空.geojson", [])
        with self.assertRaises(ToolkitError):
            crs_doctor.sniff_crs(empty)


if __name__ == "__main__":
    unittest.main()
