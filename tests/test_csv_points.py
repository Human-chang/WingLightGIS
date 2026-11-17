"""座標表格轉點 P1-9 整合測試 (tests/test_csv_points.py)

實際呼叫 vector_ops.convert_csv_table_to_points，涵蓋表格的文字編碼與副檔名：
1. Big5 (CP950) 表格：欄位名與屬性值讀成正確中文，含尾位元組為 0x5C 的「許功蓋」。
2. Big5 表格以中文欄位名 (經度／緯度) 當座標欄位。
3. 帶 BOM 的 UTF-8、帶 BOM 的 UTF-16 (Excel「Unicode 文字」)。
4. .txt 副檔名的 Tab 分隔表格。
5. 來源表格不被改動；無法辨識編碼時拋 ToolkitError。
core/csv_points.py (圖資快覽用，不載入 openpyxl)：
6. looks_like_coordinate_table 只看第一行：各種編碼、分隔符號、欄位名大小寫與萬用字元；說明文字、只有 X、讀不到的檔都不算。
7. table_to_raw_points：沒有座標系的點位，可保留 X／Y 欄；找不到座標欄位拋 ToolkitError。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import csv_points, geo_io, vector_ops
from core.env_runner import ToolkitError
from tests._support import IsolatedWorkspaceTestCase, attribute_rows


class CsvEncodingTest(IsolatedWorkspaceTestCase):
    """座標表格的文字編碼。"""

    def _write_table(self, name: str, text: str, encoding: str) -> Path:
        path = self.data_dir / name
        path.write_bytes(text.encode(encoding))
        return path

    def test_big5_table_keeps_chinese_field_names_and_values(self) -> None:
        text = "點名,X,Y,備註\n許功蓋,250000,2650000,界樁\n乙,250100,2650100,水準點\n"
        table = self._write_table("big5樁位.csv", text, "cp950")
        original_bytes = table.read_bytes()
        messages = []

        for suffix in (".gpkg", ".shp"):
            with self.subTest(output=suffix):
                output = self.out_dir / f"big5樁位{suffix}"
                vector_ops.convert_csv_table_to_points(table, output, text_callback=messages.append)

                info = geo_io.read_layer_info(output)
                self.assertEqual(info.epsg, 3826)
                self.assertEqual(tuple(info.extent), (250000.0, 2650000.0, 250100.0, 2650100.0))
                rows = attribute_rows(output)
                self.assertEqual([row["點名"] for row in rows], ["許功蓋", "乙"])
                self.assertEqual([row["備註"] for row in rows], ["界樁", "水準點"])

        self.assertEqual(table.read_bytes(), original_bytes)
        self.assertTrue(any("Big5" in message for message in messages))
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_csv_points_")], [])

    def test_big5_chinese_coordinate_column_names(self) -> None:
        table = self._write_table("big5經緯度.csv", "名稱,經度,緯度\n甲,121.0,24.0\n", "cp950")
        output = self.out_dir / "big5經緯度.gpkg"

        vector_ops.convert_csv_table_to_points(table, output)

        info = geo_io.read_layer_info(output)
        self.assertEqual(info.feature_count, 1)
        self.assertAlmostEqual(info.extent[0], 250000.0, delta=0.01)
        self.assertEqual(attribute_rows(output)[0]["名稱"], "甲")

    def test_utf8_bom_and_utf16_tables(self) -> None:
        cases = {
            "utf-8-sig": "點名,X,Y\n甲,250000,2650000\n",
            "utf-16": "點名\tX\tY\r\n甲\t250000\t2650000\r\n",
        }
        for encoding, text in cases.items():
            with self.subTest(encoding=encoding):
                table = self._write_table(f"表格_{encoding}.csv", text, encoding)
                output = self.out_dir / f"表格_{encoding}.gpkg"

                vector_ops.convert_csv_table_to_points(table, output)

                self.assertEqual(tuple(geo_io.read_layer_info(output).extent), (250000.0, 2650000.0, 250000.0, 2650000.0))
                self.assertEqual(attribute_rows(output)[0]["點名"], "甲")

    def test_tab_separated_txt_table(self) -> None:
        table = self._write_table("樁位.txt", "點名\tX\tY\n甲\t250000\t2650000\n乙\t250100\t2650100\n", "utf-8")
        output = self.out_dir / "樁位_txt.gpkg"

        vector_ops.convert_csv_table_to_points(table, output)

        self.assertEqual(geo_io.read_layer_info(output).feature_count, 2)
        self.assertEqual([row["點名"] for row in attribute_rows(output)], ["甲", "乙"])

    def test_undecodable_table_is_rejected(self) -> None:
        table = self.data_dir / "壞檔.csv"
        table.write_bytes(b"name,X,Y\n\xff\xff\xff,250000,2650000\n")
        output = self.out_dir / "壞檔.gpkg"

        with self.assertRaises(ToolkitError) as ctx:
            vector_ops.convert_csv_table_to_points(table, output)

        self.assertIn("編碼", str(ctx.exception))
        self.assertFalse(output.exists())



class CsvPointsModuleTest(IsolatedWorkspaceTestCase):
    """core/csv_points.py：判斷是不是座標表、轉成沒有座標系的點位。"""

    def _write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.data_dir / name
        path.write_bytes(text.encode(encoding))
        return path

    def test_header_check_recognises_coordinate_tables(self) -> None:
        cases = {
            "逗號.csv": ("點號,X,Y,H\n1,250000,2650000,12.3\n", "utf-8"),
            "大小寫與引號.csv": ('"pt","x","y"\n1,250000,2650000\n', "utf-8"),
            "EN.txt": ("點號\tE\tN\n1\t250000\t2650000\n", "utf-8"),
            "big5.csv": ("名稱,經度,緯度\n甲,121.0,24.0\n", "cp950"),
            "坐標.csv": ("樁號;坐標X;坐標Y\n0K+000;250000;2650000\n", "cp950"),
            "萬用字元.csv": ("name,Longitude,Latitude\na,121,24\n", "utf-8"),
            "空白分隔.txt": ("PT X Y\n1 250000 2650000\n", "utf-8"),
            "utf16.txt": ("點號\tX\tY\r\n1\t250000\t2650000\r\n", "utf-16"),
            "bom.csv": ("點號,X,Y\n1,250000,2650000\n", "utf-8-sig"),
        }
        for name, (text, encoding) in cases.items():
            with self.subTest(name):
                self.assertTrue(csv_points.looks_like_coordinate_table(self._write(name, text, encoding)))

    def test_header_check_skips_other_text_files(self) -> None:
        cases = {
            "說明.txt": ("這個資料夾放的是 112 年的測量成果，X 與 Y 請看附表。\n", "utf-8"),
            "只有X.csv": ("點號,X,高程\n1,250000,12\n", "utf-8"),
            "紀錄.txt": ("2026-11-17 會勘紀錄\n", "cp950"),
            "空檔.csv": ("", "utf-8"),
        }
        for name, (text, encoding) in cases.items():
            with self.subTest(name):
                self.assertFalse(csv_points.looks_like_coordinate_table(self._write(name, text, encoding)))
        binary = self.data_dir / "不是文字.txt"
        binary.write_bytes(bytes(range(256)) * 4)
        self.assertFalse(csv_points.looks_like_coordinate_table(binary))
        self.assertFalse(csv_points.looks_like_coordinate_table(self.data_dir / "沒有這個檔.csv"))

    def test_raw_points_have_no_srs_and_can_keep_coordinate_columns(self) -> None:
        table = self._write("樁位.csv", "點號,X,Y\n許功蓋,250000,2650000\n乙,250100,2650100\n", "cp950")
        kept = self.out_dir / "保留.gpkg"
        self.assertEqual(csv_points.table_to_raw_points(table, kept, keep_coordinate_columns=True), "Big5 (CP950)")
        info = geo_io.read_layer_info(kept, csv_points.POINTS_LAYER)
        self.assertIsNone(info.epsg, "來源座標系由呼叫端決定")
        self.assertEqual(info.feature_count, 2)
        rows = attribute_rows(kept)
        self.assertEqual([(row["點號"], row["X"], row["Y"]) for row in rows], [("許功蓋", 250000.0, 2650000.0), ("乙", 250100.0, 2650100.0)], "X／Y 存成數字")

        dropped = self.out_dir / "不保留.gpkg"
        csv_points.table_to_raw_points(table, dropped)
        self.assertNotIn("X", attribute_rows(dropped)[0])

    def test_table_without_coordinates_is_rejected(self) -> None:
        table = self._write("清冊.csv", "地號,面積\n0001,12.5\n")
        with self.assertRaises(ToolkitError) as ctx:
            csv_points.table_to_raw_points(table, self.out_dir / "清冊.gpkg")
        self.assertIn("找不到座標欄位", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
