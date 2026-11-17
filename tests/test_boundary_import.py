"""既有向量檔匯入為主邊界的測試 (tests/test_boundary_import.py)

涵蓋 P1-7 第三段 import_boundary_file：選 SHP／GPKG 當邊界時檢查幾何型別、圖徵數與座標系。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List

from core import geo_io
from core.boundary_builder import import_boundary_file
from core.crs_doctor import SourceSrsUndeterminedError
from core.env_runner import ToolkitError
from tests._support import (
    SAMPLE_KML,
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    line,
    square,
    strip_prj,
    total_area,
    write_geojson,
    write_kml,
)

# SAMPLE_KML 的「中心線」改成首尾相接的路徑 (Google Earth 用「路徑」圍出的範圍)
CLOSED_PATH_KML = SAMPLE_KML.replace(
    "121.50,25.01,0 121.53,25.01,0", "121.50,25.01,0 121.51,25.01,0 121.51,25.02,0 121.50,25.01,0"
)

# TWD67 → TWD97 約往東 828 m、往南 207 m
DATUM_SHIFT = (828.0, -207.0)
SITE = square(250000, 2650000, 250100, 2650100)


class ImportBoundaryFileTest(IsolatedWorkspaceTestCase):
    """選取既有向量檔當裁切邊界。"""

    def _geojson(self, name: str, geometries: List[Dict[str, Any]], epsg: int = 3826) -> Path:
        features = [({"名稱": f"第{idx}區"}, geometry) for idx, geometry in enumerate(geometries, start=1)]
        return write_geojson(self.data_dir / f"{name}.geojson", features, epsg=epsg)

    def test_labelled_shapefile_becomes_master_boundary(self) -> None:
        source = convert_vector(self._geojson("工區", [SITE]), self.data_dir / "工區.shp")
        before = {p.name: p.stat().st_mtime_ns for p in self.data_dir.iterdir()}
        messages: List[str] = []
        result = import_boundary_file(source, text_callback=messages.append)

        self.assertEqual(self.work_temp / "master_boundary.gpkg", result.gpkg_path)
        self.assertAlmostEqual(10000.0, result.area_m2, places=2)
        self.assertAlmostEqual(1.0, result.area_ha, places=4)
        self.assertAlmostEqual(250000.0, result.extent.min_x, places=3)
        info = geo_io.read_layer_info(result.gpkg_path, "boundary")
        self.assertEqual(3826, info.epsg)
        self.assertEqual(1, info.feature_count)
        self.assertIn("Polygon", info.geometry_type)
        self.assertAlmostEqual(10000.0, attribute_rows(result.gpkg_path, "boundary")[0]["AREA_M2"], places=2)
        self.assertTrue(result.geojson_path.is_file())
        self.assertFalse(any("[注意]" in message or "推測" in message for message in messages))
        # 來源檔不得被改動
        self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in self.data_dir.iterdir()})

    def test_wgs84_gpkg_is_reprojected(self) -> None:
        # 橫跨東經 121 度的範圍，寬約 101.7 m、高約 110.8 m
        geojson = self._geojson("經緯度工區", [square(120.9995, 24.0, 121.0005, 24.001)], epsg=4326)
        source = convert_vector(geojson, self.data_dir / "經緯度工區.gpkg")
        output = self.out_dir / "經緯度邊界.gpkg"
        result = import_boundary_file(source, output)

        self.assertEqual(3826, geo_io.read_layer_info(output, "boundary").epsg)
        self.assertAlmostEqual(250000.0, (result.extent.min_x + result.extent.max_x) / 2.0, delta=0.5)
        self.assertAlmostEqual(101.7 * 110.8, result.area_m2, delta=120.0)
        self.assertAlmostEqual(result.area_m2, total_area(output, "boundary"), places=1)

    def test_unlabelled_shapefile_is_guessed_or_assigned(self) -> None:
        source = strip_prj(convert_vector(self._geojson("無標籤工區", [SITE]), self.data_dir / "無標籤工區.shp"))
        messages: List[str] = []
        guessed = import_boundary_file(source, self.out_dir / "推測.gpkg", text_callback=messages.append)
        self.assertAlmostEqual(250000.0, guessed.extent.min_x, places=2)
        self.assertTrue(any("座標系為推測值" in message for message in messages))

        assigned = import_boundary_file(source, self.out_dir / "指派67.gpkg", assigned_srs="TWD67_121")
        self.assertAlmostEqual(250000.0 + DATUM_SHIFT[0], assigned.extent.min_x, delta=30.0)
        self.assertAlmostEqual(2650000.0 + DATUM_SHIFT[1], assigned.extent.min_y, delta=30.0)
        self.assertAlmostEqual(10000.0, assigned.area_m2, delta=5.0)

    def test_unlabelled_millimetre_shapefile_is_rejected(self) -> None:
        geojson = self._geojson("公釐工區", [square(250000000, 2650000000, 250100000, 2650100000)])
        source = strip_prj(convert_vector(geojson, self.data_dir / "公釐工區.shp"))
        output = self.out_dir / "不該產生.gpkg"
        with self.assertRaises(SourceSrsUndeterminedError):
            import_boundary_file(source, output)
        self.assertFalse(output.exists())

    def test_line_layer_is_rejected_and_previous_boundary_kept(self) -> None:
        output = self.out_dir / "master_boundary.gpkg"
        import_boundary_file(self._geojson("工區", [SITE]), output)

        lines = self._geojson("中心線", [line((250000, 2650000), (250100, 2650100))])
        source = convert_vector(lines, self.data_dir / "中心線.shp")
        with self.assertRaisesRegex(ToolkitError, "必須是面"):
            import_boundary_file(source, output)
        # 檢查沒通過時不可動到既有的輸出
        self.assertAlmostEqual(10000.0, total_area(output, "boundary"), places=2)

    def test_empty_layer_is_rejected(self) -> None:
        source = self._geojson("空圖層", [])
        with self.assertRaisesRegex(ToolkitError, "沒有任何圖徵"):
            import_boundary_file(source, self.out_dir / "空.gpkg")

    def test_multiple_polygons_are_kept_and_reported(self) -> None:
        source = self._geojson("兩區", [SITE, square(250200, 2650000, 250250, 2650100)])
        output = self.out_dir / "兩區.gpkg"
        messages: List[str] = []
        result = import_boundary_file(source, output, text_callback=messages.append)

        self.assertEqual(2, geo_io.read_layer_info(output, "boundary").feature_count)
        self.assertAlmostEqual(15000.0, result.area_m2, places=2)
        self.assertAlmostEqual(250250.0, result.extent.max_x, places=3)
        self.assertTrue(any("共有 2 個面" in message for message in messages))

    def test_mixed_geometry_keeps_polygons_only(self) -> None:
        source = self._geojson("混合", [SITE, line((250000, 2650000), (250100, 2650100))])
        output = self.out_dir / "混合.gpkg"
        messages: List[str] = []
        result = import_boundary_file(source, output, text_callback=messages.append)

        self.assertEqual(1, geo_io.read_layer_info(output, "boundary").feature_count)
        self.assertAlmostEqual(10000.0, result.area_m2, places=2)
        self.assertTrue(any("1 個圖徵不是面" in message for message in messages))

    def test_kml_uses_the_first_folder_with_polygons(self) -> None:
        """K3：KML／KMZ 的根層與「會勘點」只有點，自動用「工區範圍」；經緯度轉成 TWD97 算面積。"""
        output = self.out_dir / "master_boundary.gpkg"
        messages: List[str] = []
        result = import_boundary_file(write_kml(self.data_dir / "範圍.kmz"), output, text_callback=messages.append)
        self.assertEqual(result.layer_name, "工區範圍")
        self.assertTrue(any("採用第一個有面的圖層「工區範圍」" in m for m in messages))
        # 0.03 度 × 0.02 度，北緯 25 度約 3.03 km × 2.21 km
        self.assertAlmostEqual(result.area_m2 / 1e6, 6.71, delta=0.05)
        self.assertTrue(2_700_000 < result.extent.min_y < 2_800_000, result.extent)
        # 指定圖層：只有點的圖層不能當邊界，Google Earth 的提示一起寫
        with self.assertRaisesRegex(ToolkitError, "Google Earth 請用「多邊形」"):
            import_boundary_file(write_kml(self.data_dir / "範圍.kml"), output, layer_name="會勘點")
        self.assertIsNone(import_boundary_file(self._geojson("工區", [SITE]), output).layer_name, "只有一個圖層時是 None")

    def test_closed_path_becomes_a_polygon_but_open_line_does_not(self) -> None:
        output = self.out_dir / "master_boundary.gpkg"
        closed = write_kml(self.data_dir / "路徑.kml", CLOSED_PATH_KML)
        result = import_boundary_file(closed, output, layer_name="中心線")
        # 三角形：0.01 度 × 0.01 度的一半，約 1.01 km × 1.11 km ÷ 2
        self.assertAlmostEqual(result.area_m2 / 1e6, 0.56, delta=0.02)
        with self.assertRaisesRegex(ToolkitError, "首尾相接的線"):
            import_boundary_file(write_kml(self.data_dir / "開口.kml"), output, layer_name="中心線")

    def test_dxf_is_redirected_to_line_to_polygon(self) -> None:
        lines = self._geojson("邊界線", [line((250000, 2650000), (250100, 2650000))])
        dxf = convert_vector(lines, self.data_dir / "邊界線.dxf", dxf_layer_name="BOUNDARY")
        with self.assertRaisesRegex(ToolkitError, "DXF 線轉面"):
            import_boundary_file(dxf, self.out_dir / "dxf.gpkg")


if __name__ == "__main__":
    unittest.main()
