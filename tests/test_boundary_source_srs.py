"""邊界引擎的來源座標系測試 (tests/test_boundary_source_srs.py)

涵蓋 P1-2 (boundary_builder 部分)：
1. generate_buffer：無標籤來源依範圍推測或採用 assigned_srs；來源有座標系時不得被覆寫。
2. dxf_lines_to_polygon_boundary：DXF 沒有座標系標籤，依範圍推測或採用 assigned_srs，無法推測時拋錯。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import geo_io
from core.boundary_builder import dxf_lines_to_polygon_boundary, generate_buffer
from core.crs_doctor import SourceSrsUndeterminedError
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    line,
    square,
    strip_prj,
    total_area,
    write_geojson,
)

# 100×100 外推 10 m：10000 + 4×1000 + 圓角 π×10² ≈ 14314 (圓角以折線近似，略小)
BUFFERED_AREA = 14314.0
# TWD67 → TWD97 約往東 828 m、往南 207 m
DATUM_SHIFT = (828.0, -207.0)


def extent_center(path: Path, layer_name: Optional[str] = None) -> Tuple[float, float]:
    min_x, min_y, max_x, max_y = geo_io.read_layer_info(path, layer_name).extent
    return (min_x + max_x) / 2.0, (min_y + max_y) / 2.0


class GenerateBufferSourceSrsTest(IsolatedWorkspaceTestCase):
    """環域：無標籤來源的推測與指派、有標籤來源不被覆寫。"""

    def _unlabelled_shapefile(self, name: str, geometry: Dict[str, Any]) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", [({"名稱": "甲工區"}, geometry)])
        return strip_prj(convert_vector(geojson, self.data_dir / f"{name}.shp"))

    def test_unlabelled_tm2_shapefile_is_guessed_as_twd97(self) -> None:
        source = self._unlabelled_shapefile("無標籤工區", square(250000, 2650000, 250100, 2650100))
        output = self.out_dir / "無標籤環域.gpkg"
        messages: List[str] = []
        generate_buffer(source, output, buffer_distance=10.0, text_callback=messages.append)

        self.assertEqual(3826, geo_io.read_layer_info(output).epsg)
        self.assertEqual(["甲工區"], [row["名稱"] for row in attribute_rows(output)])
        self.assertAlmostEqual(BUFFERED_AREA, total_area(output), delta=15.0)
        center_x, center_y = extent_center(output)
        self.assertAlmostEqual(250050.0, center_x, delta=0.01)
        self.assertAlmostEqual(2650050.0, center_y, delta=0.01)
        self.assertTrue(any("座標系為推測值" in message and "EPSG:3826" in message for message in messages))

    def test_assigned_twd67_shifts_buffer_by_datum_offset(self) -> None:
        source = self._unlabelled_shapefile("無標籤67工區", square(250000, 2650000, 250100, 2650100))
        output = self.out_dir / "指派67環域.shp"
        messages: List[str] = []
        generate_buffer(
            source, output, buffer_distance=10.0, assigned_srs="TWD67_121", text_callback=messages.append
        )

        center_x, center_y = extent_center(output)
        self.assertAlmostEqual(250050.0 + DATUM_SHIFT[0], center_x, delta=30.0)
        self.assertAlmostEqual(2650050.0 + DATUM_SHIFT[1], center_y, delta=30.0)
        self.assertAlmostEqual(BUFFERED_AREA, total_area(output), delta=20.0)
        self.assertTrue(any("使用者指派" in message for message in messages))
        self.assertFalse(any("推測" in message for message in messages))

    def test_embedded_wgs84_is_reprojected_and_assignment_ignored(self) -> None:
        # 橫跨東經 121 度 (X = 250000) 的宗地，寬約 101.7 m、高約 110.8 m
        source = write_geojson(
            self.data_dir / "經緯度工區.geojson",
            [({"名稱": "甲工區"}, square(120.9995, 24.0, 121.0005, 24.001))],
            epsg=4326,
        )
        output = self.out_dir / "經緯度環域.gpkg"
        messages: List[str] = []
        generate_buffer(
            source, output, buffer_distance=10.0, assigned_srs="TWD97_121", text_callback=messages.append
        )

        min_x, min_y, max_x, max_y = geo_io.read_layer_info(output).extent
        self.assertAlmostEqual(250000.0, (min_x + max_x) / 2.0, delta=1.0)
        self.assertAlmostEqual(101.7 + 20.0, max_x - min_x, delta=1.0)
        self.assertAlmostEqual(110.8 + 20.0, max_y - min_y, delta=1.0)
        # 被當成 TWD97 的話面積只會是圓角的 314 m² 左右
        self.assertAlmostEqual(15830.0, total_area(output), delta=200.0)
        self.assertFalse(any("指派" in message or "推測" in message for message in messages))

    def test_unlabelled_source_outside_known_ranges_is_rejected(self) -> None:
        source = self._unlabelled_shapefile("無標籤公釐工區", square(250000000, 2650000000, 250100000, 2650100000))
        output = self.out_dir / "不該產生.gpkg"
        with self.assertRaises(SourceSrsUndeterminedError):
            generate_buffer(source, output, buffer_distance=10.0)
        self.assertFalse(output.exists())


class DxfBoundarySourceSrsTest(IsolatedWorkspaceTestCase):
    """DXF 線轉面：DXF 一律沒有座標系標籤。"""

    def _write_dxf(self, name: str, scale: float = 1.0) -> Path:
        x0, y0, x1, y1 = (value * scale for value in (250000.0, 2650000.0, 250100.0, 2650100.0))
        lines = [line((x0, y0), (x1, y0)), line((x1, y0), (x1, y1)), line((x1, y1), (x0, y1)), line((x0, y1), (x0, y0))]
        source = write_geojson(self.data_dir / f"{name}.geojson", [({"id": idx}, geom) for idx, geom in enumerate(lines)])
        return convert_vector(source, self.data_dir / f"{name}.dxf", dxf_layer_name="BOUNDARY")

    def test_tm2_dxf_is_guessed_as_twd97_and_reported(self) -> None:
        output = self.out_dir / "master_boundary.gpkg"
        messages: List[str] = []
        result = dxf_lines_to_polygon_boundary(
            self._write_dxf("邊界線"), output, cad_layer="BOUNDARY", text_callback=messages.append
        )

        self.assertAlmostEqual(10000.0, result.area_m2, places=1)
        self.assertAlmostEqual(250000.0, result.extent.min_x, places=2)
        self.assertAlmostEqual(2650100.0, result.extent.max_y, places=2)
        self.assertEqual(3826, geo_io.read_layer_info(output, "boundary").epsg)
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_assigned_twd67_shifts_boundary_by_datum_offset(self) -> None:
        output = self.out_dir / "指派67邊界.gpkg"
        messages: List[str] = []
        result = dxf_lines_to_polygon_boundary(
            self._write_dxf("邊界線67"), output, assigned_srs="TWD67_121", text_callback=messages.append
        )

        self.assertAlmostEqual(10000.0, result.area_m2, delta=5.0)
        self.assertAlmostEqual(250000.0 + DATUM_SHIFT[0], result.extent.min_x, delta=30.0)
        self.assertAlmostEqual(2650000.0 + DATUM_SHIFT[1], result.extent.min_y, delta=30.0)
        self.assertEqual(3826, geo_io.read_layer_info(output, "boundary").epsg)
        center_x, _ = extent_center(output, "boundary")
        self.assertAlmostEqual(250050.0 + DATUM_SHIFT[0], center_x, delta=30.0)
        self.assertTrue(any("使用者指派" in message for message in messages))

    def test_millimetre_dxf_is_rejected_until_srs_is_decidable(self) -> None:
        output = self.out_dir / "公釐邊界.gpkg"
        with self.assertRaises(SourceSrsUndeterminedError):
            dxf_lines_to_polygon_boundary(self._write_dxf("公釐邊界線", scale=1000.0), output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
