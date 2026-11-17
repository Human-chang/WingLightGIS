"""編碼相關整合測試 (tests/test_encoding.py)

1. P1-3：移除全域 SHAPE_ENCODING 之後，Big5 Shapefile (有 .cpg／無 .cpg) 的欄位名與屬性值讀取正確，
   CP950 輸出的欄位名確實是 Big5 位元組；編碼修復不改動來源檔。
2. P1-4：run_command 逐段讀取輸出時，中文字不會被切壞。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import main
from core import crs_doctor, env_runner, geo_io, vector_ops
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    snapshot_directory,
    square,
    write_geojson,
)

PARCELS = [
    ({"段名": "大安段", "地號": "1", "土地使用分區名稱": "住宅區"}, square(250000, 2650000, 250100, 2650100)),
    ({"段名": "信義段", "地號": "2", "土地使用分區名稱": "商業區"}, square(250100, 2650000, 250200, 2650100)),
]
# CP950 下欄位名上限 10 bytes = 5 個中文字
BIG5_ROWS = [
    {"段名": "大安段", "地號": "1", "土地使用分": "住宅區"},
    {"段名": "信義段", "地號": "2", "土地使用分": "商業區"},
]


class EncodingTestCase(IsolatedWorkspaceTestCase):
    """準備 Big5 Shapefile 的共用基底。"""

    def setUp(self) -> None:
        super().setUp()
        self.parcels_geojson = write_geojson(self.data_dir / "宗地.geojson", PARCELS)

    def make_big5_shapefile(self, name: str, *, with_cpg: bool, clear_ldid: bool = False) -> Path:
        """產生 Big5 Shapefile；clear_ldid 把 DBF 檔頭的語系代碼清成 0 (ArcMap 與舊軟體常見的寫法)。"""
        shp = convert_vector(self.parcels_geojson, self.data_dir / f"{name}.shp", shp_encoding="big5")
        if not with_cpg:
            shp.with_suffix(".cpg").unlink()
        if clear_ldid:
            dbf = shp.with_suffix(".dbf")
            raw = bytearray(dbf.read_bytes())
            raw[29] = 0
            dbf.write_bytes(bytes(raw))
        return shp

    def staged_rows(self, source: Path) -> List[Dict[str, Any]]:
        """經 ogr2ogr (CLI) 轉成暫存 GPKG 後讀回屬性，驗證 CLI 端的編碼處理。"""
        staged = self.work_temp / f"{source.stem}_staged.gpkg"
        geo_io.stage_vector_to_gpkg(source, staged)
        return attribute_rows(staged, "stage")


class ShapeEncodingEnvTest(EncodingTestCase):
    """全域 SHAPE_ENCODING 不可以傳進 CLI 行程，也不可以留在本行程。"""

    def test_isolated_env_drops_shape_encoding(self) -> None:
        with mock.patch.dict(os.environ, {"SHAPE_ENCODING": "UTF-8"}):
            self.assertNotIn("SHAPE_ENCODING", env_runner.build_isolated_env())

    def test_main_sanitize_removes_shape_encoding(self) -> None:
        with mock.patch.dict(os.environ, {"SHAPE_ENCODING": "UTF-8"}):
            main.sanitize_runtime_environment()
            self.assertNotIn("SHAPE_ENCODING", os.environ)

    def test_big5_with_cpg_is_read_correctly_even_if_host_sets_shape_encoding(self) -> None:
        shp = self.make_big5_shapefile("宗地_big5", with_cpg=True)
        with mock.patch.dict(os.environ, {"SHAPE_ENCODING": "UTF-8"}):
            self.assertEqual(self.staged_rows(shp), BIG5_ROWS)


class Big5ShapefileTest(EncodingTestCase):
    """Big5 Shapefile 讀寫。"""

    def test_cp950_output_writes_big5_field_names(self) -> None:
        shp = self.make_big5_shapefile("宗地_big5", with_cpg=True)
        self.assertEqual(shp.with_suffix(".cpg").read_text(encoding="ascii").strip().upper(), "CP950")
        dbf_bytes = shp.with_suffix(".dbf").read_bytes()
        self.assertIn("段名".encode("cp950"), dbf_bytes)
        self.assertIn("大安段".encode("cp950"), dbf_bytes)
        self.assertNotIn("段名".encode("utf-8"), dbf_bytes)

    def test_big5_with_cpg_read_back(self) -> None:
        shp = self.make_big5_shapefile("宗地_big5", with_cpg=True)
        self.assertEqual(attribute_rows(shp), BIG5_ROWS)
        self.assertEqual(self.staged_rows(shp), BIG5_ROWS)

    def test_big5_without_cpg_read_back(self) -> None:
        shp = self.make_big5_shapefile("宗地_big5_無cpg", with_cpg=False)
        self.assertEqual(attribute_rows(shp), BIG5_ROWS)
        self.assertEqual(self.staged_rows(shp), BIG5_ROWS)

    def test_big5_without_cpg_and_without_ldid_read_back(self) -> None:
        shp = self.make_big5_shapefile("宗地_big5_arcmap", with_cpg=False, clear_ldid=True)
        self.assertEqual(geo_io.detect_shapefile_encoding(shp), "CP950")
        self.assertEqual(attribute_rows(shp), BIG5_ROWS)
        self.assertEqual(self.staged_rows(shp), BIG5_ROWS)


class RepairShapefileEncodingTest(EncodingTestCase):
    """[G-10] 編碼修復：輸出 UTF-8 新檔，來源檔不動。"""

    def test_repair_writes_utf8_copy_and_leaves_source_untouched(self) -> None:
        shp = self.make_big5_shapefile("舊圖", with_cpg=False, clear_ldid=True)
        before = snapshot_directory(self.data_dir)

        output = vector_ops.repair_shapefile_encoding(shp)

        self.assertEqual(output, shp.parent / "舊圖_utf8.shp")
        self.assertEqual(output.with_suffix(".cpg").read_text(encoding="ascii").strip(), "UTF-8")
        # UTF-8 下欄位名上限 10 bytes = 3 個中文字
        self.assertEqual(
            attribute_rows(output),
            [{"段名": "大安段", "地號": "1", "土地使": "住宅區"}, {"段名": "信義段", "地號": "2", "土地使": "商業區"}],
        )
        self.assertIn("大安段".encode("utf-8"), output.with_suffix(".dbf").read_bytes())

        geo_io.remove_vector_file(output)
        self.assertEqual(snapshot_directory(self.data_dir), before, "來源 Shapefile 不得被改動，也不得多出 .cpg")

    def test_repair_respects_existing_cpg_and_explicit_encoding(self) -> None:
        with_cpg = self.make_big5_shapefile("有cpg", with_cpg=True)
        output = vector_ops.repair_shapefile_encoding(with_cpg, output_path=self.out_dir / "有cpg_utf8.shp")
        self.assertEqual([row["段名"] for row in attribute_rows(output)], ["大安段", "信義段"])

        no_cpg = self.make_big5_shapefile("無cpg", with_cpg=False, clear_ldid=True)
        output = vector_ops.repair_shapefile_encoding(
            no_cpg, source_encoding="CP950", output_path=self.out_dir / "無cpg_utf8.shp"
        )
        self.assertEqual([row["段名"] for row in attribute_rows(output)], ["大安段", "信義段"])

    def test_repair_rejects_non_shapefile(self) -> None:
        with self.assertRaises(ValueError):
            vector_ops.repair_shapefile_encoding(self.parcels_geojson)


class ShapefileWritersTest(EncodingTestCase):
    """原本靠 --config SHAPE_ENCODING 寫 SHP 的函式，改寫後中文欄位仍要正確。"""

    def test_simplify_to_shapefile_keeps_chinese_attributes(self) -> None:
        output = self.out_dir / "抽稀.shp"
        vector_ops.simplify_vector_geometry(self.parcels_geojson, output, tolerance=1.0)
        self.assertEqual(output.with_suffix(".cpg").read_text(encoding="ascii").strip(), "UTF-8")
        self.assertEqual([row["段名"] for row in attribute_rows(output)], ["大安段", "信義段"])
        self.assertEqual(geo_io.read_layer_info(output).feature_count, 2)

    def test_twd67_to_twd97_shifts_coordinates_and_keeps_chinese_attributes(self) -> None:
        output = self.out_dir / "轉97.shp"
        result = crs_doctor.convert_twd67_vector_to_twd97(self.parcels_geojson, output)
        self.assertEqual(result, output.resolve())
        self.assertEqual([row["段名"] for row in attribute_rows(output)], ["大安段", "信義段"])
        info = geo_io.read_layer_info(output)
        self.assertEqual(info.epsg, 3826)
        # TWD67 → TWD97 在臺灣本島約為 X +828 m、Y −207 m
        self.assertAlmostEqual(info.extent[0] - 250000.0, 828.0, delta=15.0)
        self.assertAlmostEqual(info.extent[1] - 2650000.0, -207.0, delta=15.0)


class RunCommandDecodingTest(EncodingTestCase):
    """P1-4：CLI 輸出逐段解碼不得把中文字切壞。"""

    def test_many_chinese_field_names_survive_chunked_reading(self) -> None:
        field_names = [f"欄位名稱測試第{index:02d}號" for index in range(40)]
        source = write_geojson(
            self.data_dir / "多欄位.geojson",
            [({name: f"值{index}" for index, name in enumerate(field_names)}, square(250000, 2650000, 250100, 2650100))],
        )
        gpkg = convert_vector(source, self.data_dir / "多欄位.gpkg")
        lines: List[str] = []

        result = env_runner.run_command(["ogrinfo", "-so", str(gpkg), "多欄位"], text_callback=lines.append, check=True)

        self.assertNotIn("�", result.stdout)
        for name in field_names:
            self.assertIn(name, result.stdout)
        streamed = "".join(lines)
        self.assertNotIn("�", streamed)
        for name in field_names:
            self.assertIn(name, streamed)


if __name__ == "__main__":
    unittest.main()
