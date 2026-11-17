"""步驟 9-5：SHP 裁切成果的屬性 Excel (core/attribute_report.py＋clip_vector_layer 的 excel_output_path)。

1. 全部原屬性 (欄名是完整原名，不受 Shapefile 10 bytes 限制)＋面積欄位，最後一列合計；
   原屬性的數字欄位不加總，面積比例以合計後的面積重算。
2. 線圖層合計長度；沒有交集時只有「合計 (0 筆)」。
3. Excel 檔被開著 (寫不進去) 時裁切照樣成功，只記警告。
4. K5 整筆保留：欄名改成整筆面積、範圍內面積、範圍內比例，合計列兩種面積都加總。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import openpyxl

from core import geo_io
from core.clip_master import clip_vector_layer
from tests._support import IsolatedWorkspaceTestCase, line, square, write_geojson

LONG_FIELD = "土地使用分區名稱"


def read_sheet(path: Path) -> List[List[Any]]:
    workbook = openpyxl.load_workbook(path)
    try:
        return [list(row) for row in workbook.active.iter_rows(values_only=True)]
    finally:
        workbook.close()


def header_index(rows: List[List[Any]]) -> int:
    return next(i for i, row in enumerate(rows) if row and row[0] == "序號")


class TestClipAttributeExcel(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 邊界：250000～250150 × 2650000～2650100；甲地全部在內、乙地一半在內
        self.boundary = write_geojson(self.data_dir / "邊界.geojson", [({}, square(250000, 2650000, 250150, 2650100))])
        self.parcels = write_geojson(
            self.data_dir / "宗地.geojson",
            [
                ({"地號": 101, LONG_FIELD: "道路用地", "公告現值": 12000.5}, square(250000, 2650000, 250100, 2650100)),
                ({"地號": 102, LONG_FIELD: "農業區", "公告現值": 8000.0}, square(250100, 2650000, 250200, 2650100)),
            ],
        )

    def test_all_attributes_areas_and_total_row(self) -> None:
        messages: List[str] = []
        result = clip_vector_layer(
            self.parcels, self.boundary, self.out_dir / "宗地_clipped.shp",
            excel_output_path=self.out_dir / "宗地_clipped.xlsx", text_callback=messages.append,
        )
        self.assertEqual(result.attribute_excel, self.out_dir / "宗地_clipped.xlsx")
        rows = read_sheet(result.attribute_excel)
        self.assertEqual(rows[0][0], "宗地.geojson 裁切成果屬性表")
        h = header_index(rows)
        headers = rows[h]
        self.assertEqual(headers[:4], ["序號", "地號", LONG_FIELD, "公告現值"])
        self.assertIn("裁切後面積 (m²)\nAREA_M2", headers)
        # Shapefile 成果的欄名被截短，Excel 是完整原名
        self.assertNotIn(LONG_FIELD, geo_io.read_layer_info(result.output_path).field_names)

        col = {name.split("\n")[-1]: i for i, name in enumerate(headers)}
        data = rows[h + 1:h + 3]
        self.assertEqual([r[0] for r in data], [1, 2])
        self.assertEqual(sorted(r[col["地號"]] for r in data), [101, 102])
        by_parcel: Dict[int, List[Any]] = {r[col["地號"]]: r for r in data}
        self.assertAlmostEqual(by_parcel[102][col["AREA_M2"]], 5000.0, places=1)
        self.assertAlmostEqual(by_parcel[102][col["RATIO_PCT"]], 50.0, places=2)

        total = rows[h + 3]
        self.assertEqual(total[0], "合計 (2 筆)")
        self.assertAlmostEqual(total[col["ORIG_M2"]], 20000.0, places=1)
        self.assertAlmostEqual(total[col["AREA_M2"]], 15000.0, places=1)
        self.assertAlmostEqual(total[col["AREA_HA"]], 1.5, places=4)
        self.assertAlmostEqual(total[col["RATIO_PCT"]], 75.0, places=2)
        self.assertIsNone(total[col["地號"]], "原屬性的數字欄位不加總")
        self.assertIsNone(total[col["公告現值"]])
        self.assertIsNone(total[col["LEN_M"]], "面圖層的周長加總沒有意義")
        self.assertEqual(len(rows), h + 4)

        sheet = openpyxl.load_workbook(result.attribute_excel).active
        self.assertEqual(sheet.cell(row=h + 2, column=col["AREA_M2"] + 1).number_format, "#,##0.00")
        self.assertEqual(sheet.freeze_panes, f"B{h + 2}")
        self.assertTrue(any("[屬性 Excel]" in m for m in messages))

    def test_keep_whole_lists_whole_and_inside_areas(self) -> None:
        result = clip_vector_layer(
            self.parcels, self.boundary, self.out_dir / "宗地_whole.shp",
            excel_output_path=self.out_dir / "宗地_whole.xlsx", keep_whole=True,
        )
        rows = read_sheet(result.attribute_excel)
        self.assertEqual(rows[0][0], "宗地.geojson 整筆保留成果屬性表")
        self.assertTrue(any(row[0] and str(row[0]).startswith("整筆保留：") for row in rows[:header_index(rows)]))
        h = header_index(rows)
        headers = rows[h]
        for label in ("整筆面積 (m²)\nORIG_M2", "範圍內面積 (m²)\nAREA_M2", "範圍內面積 (公頃)\nAREA_HA", "範圍內比例 (%)\nRATIO_PCT"):
            self.assertIn(label, headers)
        self.assertNotIn("裁切後面積 (m²)\nAREA_M2", headers)
        col = {name.split("\n")[-1]: i for i, name in enumerate(headers)}
        by_parcel = {r[col["地號"]]: r for r in rows[h + 1:h + 3]}
        self.assertAlmostEqual(by_parcel[102][col["ORIG_M2"]], 10000.0, places=1)
        self.assertAlmostEqual(by_parcel[102][col["AREA_M2"]], 5000.0, places=1)
        self.assertAlmostEqual(by_parcel[102][col["RATIO_PCT"]], 50.0, places=2)
        total = rows[h + 3]
        self.assertEqual(total[0], "合計 (2 筆)")
        self.assertAlmostEqual(total[col["ORIG_M2"]], 20000.0, places=1, msg="整筆面積合計")
        self.assertAlmostEqual(total[col["AREA_M2"]], 15000.0, places=1, msg="範圍內面積合計")
        self.assertNotIn("_WL_FID", headers)

    def test_line_layer_totals_length(self) -> None:
        pipes = write_geojson(
            self.data_dir / "管線.geojson",
            [({"編號": "P1"}, line((249950, 2650050), (250250, 2650050)))],
        )
        result = clip_vector_layer(
            pipes, self.boundary, self.out_dir / "管線_clipped.shp", excel_output_path=self.out_dir / "管線.xlsx"
        )
        rows = read_sheet(result.attribute_excel)
        h = header_index(rows)
        col = {name.split("\n")[-1]: i for i, name in enumerate(rows[h])}
        self.assertAlmostEqual(rows[h + 2][col["LEN_M"]], 150.0, places=1)

    def test_no_intersection_gives_empty_table(self) -> None:
        far = write_geojson(self.data_dir / "遠方.geojson", [({"n": 1}, square(260000, 2660000, 260100, 2660100))])
        result = clip_vector_layer(
            far, self.boundary, self.out_dir / "遠方_clipped.shp", excel_output_path=self.out_dir / "遠方.xlsx"
        )
        rows = read_sheet(result.attribute_excel)
        h = header_index(rows)
        self.assertEqual(rows[h + 1][0], "合計 (0 筆)")

    def test_locked_excel_only_warns(self) -> None:
        messages: List[str] = []
        with mock.patch("openpyxl.workbook.workbook.Workbook.save", side_effect=PermissionError("locked")):
            result = clip_vector_layer(
                self.parcels, self.boundary, self.out_dir / "宗地_clipped.shp",
                excel_output_path=self.out_dir / "宗地_clipped.xlsx", text_callback=messages.append,
            )
        self.assertTrue(result.is_success)
        self.assertTrue(result.output_path.exists())
        self.assertIsNone(result.attribute_excel)
        self.assertTrue(any("Excel 開著" in m for m in messages), messages)


if __name__ == "__main__":
    unittest.main()
