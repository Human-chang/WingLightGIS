"""手動輸入座標建立邊界的測試 (tests/test_boundary_points.py)

涵蓋 P1-7 第一段：
1. parse_coordinate_text：純數字點號、逗號千分位、經緯度欄位對調、混用座標、相異頂點數。
2. create_boundary_from_points：經緯度與 TWD67 輸入重投影到 EPSG:3826，面積為平方公尺；
   無法推測座標系、面積為 0 時拋錯。
"""

from __future__ import annotations

import unittest
from typing import List

from core import geo_io
from core.boundary_builder import (
    create_boundary_from_points,
    detect_coordinate_kind,
    parse_coordinate_text,
)
from core.crs_doctor import SourceSrsUndeterminedError
from tests._support import IsolatedWorkspaceTestCase, attribute_rows, total_area

# TWD67 → TWD97 約往東 828 m、往南 207 m
DATUM_SHIFT = (828.0, -207.0)


class ParseCoordinateTextTest(IsolatedWorkspaceTestCase):
    """座標文字解析。"""

    def test_numeric_point_id_is_not_taken_as_x(self) -> None:
        text = "1, 250000.5, 2650000.0\n2, 250100.5, 2650000.0\n3, 250100.5, 2650100.0\n4, 250000.5, 2650100.0"
        pts = parse_coordinate_text(text)
        self.assertEqual([(250000.5, 2650000.0), (250100.5, 2650000.0)], [p.as_tuple() for p in pts[:2]])
        self.assertEqual(["1", "2", "3", "4"], [p.point_id for p in pts])

    def test_numeric_point_id_without_spaces_and_swapped_columns(self) -> None:
        text = "1,2650000,250000\n2,2650000,250100\n3,2650100,250100"
        pts = parse_coordinate_text(text)
        self.assertEqual([(250000.0, 2650000.0), (250100.0, 2650000.0), (250100.0, 2650100.0)], [p.as_tuple() for p in pts])

    def test_xyz_columns_keep_first_two_as_coordinates(self) -> None:
        text = "250000 2650000 15\n250100 2650000 16\n250100 2650100 17"
        pts = parse_coordinate_text(text)
        self.assertEqual((250000.0, 2650000.0), pts[0].as_tuple())
        self.assertEqual("P1", pts[0].point_id)

    def test_numeric_point_id_with_local_coordinates(self) -> None:
        # 區域座標沒有已知範圍可判斷：第一欄全是不重複的整數才當點號
        pts = parse_coordinate_text("1, 1000.5, 2000.0\n2, 1100.5, 2000.0\n3, 1100.5, 2100.0")
        self.assertEqual((1000.5, 2000.0), pts[0].as_tuple())
        # 第一欄有小數就是座標 (X, Y, Z)
        pts = parse_coordinate_text("1000.5, 2000.0, 12\n1100.5, 2000.0, 13\n1100.5, 2100.0, 14")
        self.assertEqual((1000.5, 2000.0), pts[0].as_tuple())

    def test_thousands_separators(self) -> None:
        tab_text = "P1\t250,000.5\t2,650,000.0\nP2\t250,100.5\t2,650,000.0\nP3\t250,100.5\t2,650,100.0"
        pts = parse_coordinate_text(tab_text)
        self.assertEqual((250000.5, 2650000.0), pts[0].as_tuple())
        self.assertEqual("P1", pts[0].point_id)

        mixed_text = '250,000.5, 2,650,000.0\n"250,100.5","2,650,000.0"\n250,100.5 2,650,100.0'
        pts = parse_coordinate_text(mixed_text)
        self.assertEqual(
            [(250000.5, 2650000.0), (250100.5, 2650000.0), (250100.5, 2650100.0)], [p.as_tuple() for p in pts]
        )

    def test_comma_separated_three_digit_values_are_not_merged(self) -> None:
        # 100,200 是兩個數值，不是十萬零二百
        pts = parse_coordinate_text("100,200\n300,200\n300,400")
        self.assertEqual([(100.0, 200.0), (300.0, 200.0), (300.0, 400.0)], [p.as_tuple() for p in pts])

    def test_lat_lon_order_is_swapped_and_kind_detected(self) -> None:
        pts = parse_coordinate_text("24.0, 121.0\n24.0, 121.001\n24.001, 121.001\n24.001, 121.0")
        self.assertEqual((121.0, 24.0), pts[0].as_tuple())
        self.assertEqual("LONLAT", detect_coordinate_kind(pts))
        # 點號剛好落在緯度範圍 (24) 也不可被當成座標
        pts = parse_coordinate_text("24, 121.0, 24.0\n25, 121.001, 24.0\n26, 121.001, 24.001")
        self.assertEqual((121.0, 24.0), pts[0].as_tuple())

    def test_header_and_comment_lines_are_skipped(self) -> None:
        pts = parse_coordinate_text("點號\tX\tY\n# 註解 1 2 3\nA\t250000\t2650000\nB\t250100\t2650000\nC\t250100\t2650100")
        self.assertEqual(["A", "B", "C"], [p.point_id for p in pts])

    def test_row_with_single_number_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 2 列只讀到一個數值"):
            parse_coordinate_text("250000 2650000\n25O100 2650000\n250100 2650100\n250000 2650100")

    def test_mixed_tm2_and_lonlat_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "混用"):
            parse_coordinate_text("250000 2650000\n250100 2650000\n121.0 24.0")

    def test_out_of_range_row_is_rejected(self) -> None:
        # 第 3 列的 Y 少一位數
        with self.assertRaisesRegex(ValueError, "第 3 列"):
            parse_coordinate_text("250000 2650000\n250100 2650000\n250100 265010\n250000 2650100")

    def test_fewer_than_three_distinct_vertices_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "相異頂點"):
            parse_coordinate_text("250000 2650000\n250100 2650000\n250000 2650000\n250100 2650000")

    def test_non_finite_tokens_are_not_numbers(self) -> None:
        with self.assertRaises(ValueError):
            parse_coordinate_text("nan inf\n250000 2650000\n250100 2650000")


class CreateBoundaryFromPointsTest(IsolatedWorkspaceTestCase):
    """由點列建立主邊界 GPKG。"""

    def test_twd97_points_build_square_boundary(self) -> None:
        pts = parse_coordinate_text("250000 2650000\n250100 2650000\n250100 2650100\n250000 2650100")
        output = self.out_dir / "主邊界.gpkg"
        messages: List[str] = []
        result = create_boundary_from_points(pts, output_gpkg_path=output, text_callback=messages.append)

        self.assertTrue(result.was_auto_closed)
        self.assertEqual(5, result.vertex_count)
        self.assertAlmostEqual(10000.0, result.area_m2, places=2)
        self.assertAlmostEqual(1.0, result.area_ha, places=4)
        self.assertAlmostEqual(250000.0, result.extent.min_x, places=3)
        self.assertAlmostEqual(2650100.0, result.extent.max_y, places=3)
        info = geo_io.read_layer_info(output, "boundary")
        self.assertEqual(3826, info.epsg)
        self.assertEqual(1, info.feature_count)
        self.assertAlmostEqual(10000.0, total_area(output, "boundary"), places=2)
        rows = attribute_rows(output, "boundary")
        self.assertAlmostEqual(10000.0, rows[0]["AREA_M2"], places=2)
        self.assertEqual(5, rows[0]["VERTEX_NUM"])
        self.assertTrue(result.geojson_path.is_file())
        self.assertTrue(any("座標系為推測值" in message and "EPSG:3826" in message for message in messages))

    def test_lonlat_points_are_reprojected_and_area_is_square_metres(self) -> None:
        # 橫跨東經 121 度 (X = 250000) 的範圍，寬約 101.7 m、高約 110.8 m；輸入順序為 緯度, 經度
        pts = parse_coordinate_text("24.0, 120.9995\n24.0, 121.0005\n24.001, 121.0005\n24.001, 120.9995")
        output = self.out_dir / "經緯度邊界.gpkg"
        messages: List[str] = []
        result = create_boundary_from_points(pts, output_gpkg_path=output, text_callback=messages.append)

        self.assertEqual(3826, geo_io.read_layer_info(output, "boundary").epsg)
        self.assertAlmostEqual(250000.0, (result.extent.min_x + result.extent.max_x) / 2.0, delta=0.5)
        self.assertAlmostEqual(101.7, result.extent.max_x - result.extent.min_x, delta=0.5)
        self.assertAlmostEqual(110.8, result.extent.max_y - result.extent.min_y, delta=0.5)
        self.assertAlmostEqual(101.7 * 110.8, result.area_m2, delta=120.0)
        self.assertAlmostEqual(result.area_m2, total_area(output, "boundary"), places=1)
        self.assertTrue(any("EPSG:4326" in message for message in messages))

    def test_assigned_twd67_shifts_boundary_by_datum_offset(self) -> None:
        pts = parse_coordinate_text("250000 2650000\n250100 2650000\n250100 2650100\n250000 2650100")
        output = self.out_dir / "指派67邊界.gpkg"
        messages: List[str] = []
        result = create_boundary_from_points(
            pts, output_gpkg_path=output, assigned_srs="TWD67_121", text_callback=messages.append
        )

        self.assertAlmostEqual(250000.0 + DATUM_SHIFT[0], result.extent.min_x, delta=30.0)
        self.assertAlmostEqual(2650000.0 + DATUM_SHIFT[1], result.extent.min_y, delta=30.0)
        self.assertAlmostEqual(10000.0, result.area_m2, delta=5.0)
        self.assertEqual(3826, geo_io.read_layer_info(output, "boundary").epsg)
        self.assertTrue(any("使用者指派" in message for message in messages))
        self.assertFalse(any("推測" in message for message in messages))

    def test_default_output_goes_to_workspace_temp(self) -> None:
        result = create_boundary_from_points(
            [(250000.0, 2650000.0), (250100.0, 2650000.0), (250100.0, 2650100.0)]
        )
        self.assertEqual(self.work_temp / "master_boundary.gpkg", result.gpkg_path)
        self.assertAlmostEqual(5000.0, result.area_m2, places=2)
        leftovers = [p.name for p in self.work_temp.iterdir() if p.name.startswith("_point_boundary")]
        self.assertEqual([], leftovers)

    def test_polar_sort_repairs_bowtie(self) -> None:
        bowtie = [(250000.0, 2650000.0), (250100.0, 2650100.0), (250100.0, 2650000.0), (250000.0, 2650100.0)]
        output = self.out_dir / "沙漏.gpkg"
        with self.assertRaisesRegex(ValueError, "面積為 0"):
            create_boundary_from_points(bowtie, output_gpkg_path=output)
        self.assertFalse(output.exists())

        result = create_boundary_from_points(bowtie, output_gpkg_path=output, apply_polar_sort=True)
        self.assertTrue(result.was_polar_sorted)
        self.assertAlmostEqual(10000.0, result.area_m2, places=2)

    def test_collinear_points_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "面積為 0"):
            create_boundary_from_points(
                [(250000.0, 2650000.0), (250050.0, 2650050.0), (250100.0, 2650100.0)],
                output_gpkg_path=self.out_dir / "共線.gpkg",
            )

    def test_undeterminable_coordinates_are_rejected_until_assigned(self) -> None:
        local = [(1000.0, 2000.0), (1100.0, 2000.0), (1100.0, 2100.0), (1000.0, 2100.0)]
        output = self.out_dir / "區域座標.gpkg"
        with self.assertRaises(SourceSrsUndeterminedError):
            create_boundary_from_points(local, output_gpkg_path=output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
