"""向量引擎整合測試 (tests/test_vector_engines.py)

以合成資料實際呼叫引擎函式 (會執行沙盒的 ogr2ogr / ogrmerge)，並核對成果檔內的數值。
測試資料：兩筆 100 m × 100 m 相鄰宗地、一筆遠方宗地，以及各蓋住前兩筆一半的工程邊界。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List

import openpyxl

from core import crs_doctor, geo_io, vector_ops
from core.boundary_builder import dxf_lines_to_polygon_boundary, generate_buffer
from core.clip_master import clip_vector_layer
from core.env_runner import ToolkitError, build_isolated_env, get_runtime_dir, get_toolkit_root, run_command
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    line,
    square,
    total_area,
    write_geojson,
)

PARCELS = [
    ({"段名": "大安段", "地號": "1", "NOTE": "O'Brien", "ZONE_NO": 1, "AREA_M2": 999.0}, square(250000, 2650000, 250100, 2650100)),
    ({"段名": "信義段", "地號": "2", "NOTE": None, "ZONE_NO": 2, "AREA_M2": 888.0}, square(250100, 2650000, 250200, 2650100)),
    ({"段名": "大安段", "地號": "3", "NOTE": "", "ZONE_NO": 1, "AREA_M2": 1.0}, square(260000, 2650000, 260100, 2650100)),
]
# x 250050～250150：各蓋住宗地 1、2 的一半 (5000 m²)，宗地 3 完全在外
BOUNDARY = [({"id": 1}, square(250050, 2649900, 250150, 2650200))]


class VectorEngineTestCase(IsolatedWorkspaceTestCase):
    """準備宗地與工程邊界的共用基底。"""

    def setUp(self) -> None:
        super().setUp()
        self.parcels_geojson = write_geojson(self.data_dir / "宗地.geojson", PARCELS)
        boundary_geojson = write_geojson(self.data_dir / "邊界.geojson", BOUNDARY)
        self.boundary = convert_vector(boundary_geojson, self.data_dir / "master_boundary.gpkg")

    def parcels_as(self, suffix: str) -> Path:
        """回傳指定格式的宗地圖 (.geojson / .gpkg / .shp)。"""
        if suffix == ".geojson":
            return self.parcels_geojson
        return convert_vector(self.parcels_geojson, self.data_dir / f"宗地{suffix}")

    def by_land_no(self, path: Path) -> Dict[str, Dict[str, Any]]:
        return {row["地號"]: row for row in attribute_rows(path)}


class SniffCrsTest(VectorEngineTestCase):
    """P0-1：crs_doctor 漏 import json / re，座標系診斷一呼叫就 NameError。"""

    def test_sniff_crs_reads_vector_extent_and_epsg(self) -> None:
        for suffix in (".geojson", ".gpkg", ".shp"):
            with self.subTest(source=suffix):
                result = crs_doctor.sniff_crs(self.parcels_as(suffix))
                self.assertIsInstance(result, crs_doctor.CrsDiagnosisResult)
                self.assertEqual(result.detected_epsg, 3826)
                self.assertAlmostEqual(result.extent.min_x, 250000.0, places=2)
                self.assertAlmostEqual(result.extent.max_y, 2650100.0, places=2)


class ClipVectorLayerTest(VectorEngineTestCase):
    """P0-5 / P0-6 / P1-1：幾何欄位名、-sql 圖層名、面積須為裁切後的值。"""

    def test_area_is_computed_after_clipping(self) -> None:
        cases = ((".geojson", ".gpkg"), (".gpkg", ".shp"), (".shp", ".shp"), (".shp", ".gpkg"))
        for source_suffix, output_suffix in cases:
            with self.subTest(source=source_suffix, output=output_suffix):
                output = self.out_dir / f"裁切_{source_suffix[1:]}{output_suffix}"
                result = clip_vector_layer(self.parcels_as(source_suffix), self.boundary, output)

                self.assertTrue(result.is_success)
                self.assertEqual(result.feature_count, 2)
                self.assertEqual(result.area_m2, 10000.0)
                self.assertEqual(result.area_ha, 1.0)

                rows = self.by_land_no(output)
                self.assertEqual(sorted(rows), ["1", "2"])
                for land_no, cent_x in (("1", 250075.0), ("2", 250125.0)):
                    row = rows[land_no]
                    # 來源自帶的 AREA_M2 (999 / 888) 必須被裁切後重新計算的值取代
                    self.assertEqual(row["AREA_M2"], 5000.0)
                    self.assertEqual(row["ORIG_M2"], 10000.0)
                    self.assertEqual(row["AREA_HA"], 0.5)
                    self.assertEqual(row["RATIO_PCT"], 50.0)
                    self.assertEqual(row["LEN_M"], 300.0)
                    self.assertEqual(row["CENT_X"], cent_x)
                    self.assertEqual(row["CENT_Y"], 2650050.0)
                self.assertEqual(rows["1"]["段名"], "大安段")
                self.assertEqual(geo_io.read_layer_info(output).epsg, 3826)

    def test_no_intersection_gives_empty_result_not_error(self) -> None:
        far = write_geojson(self.data_dir / "遠方邊界.geojson", [({"id": 9}, square(100000, 2500000, 100100, 2500100))])
        messages: List[str] = []
        output = self.out_dir / "空成果.gpkg"
        result = clip_vector_layer(self.parcels_geojson, far, output, text_callback=messages.append)
        self.assertEqual(result.feature_count, 0)
        self.assertEqual(result.area_m2, 0.0)
        self.assertEqual(geo_io.read_layer_info(output).feature_count, 0)
        self.assertTrue(any("沒有交集" in message for message in messages))

    def test_geographic_target_srs_is_rejected(self) -> None:
        with self.assertRaises(ToolkitError):
            clip_vector_layer(self.parcels_geojson, self.boundary, self.out_dir / "經緯度.gpkg", target_srs="EPSG:4326")


class KeepWholeTest(VectorEngineTestCase):
    """K5 整筆保留：跟邊界有重疊的圖徵整筆選出、不切開；ORIG_M2 整筆面積、AREA_M2 範圍內面積。"""

    def test_overlapping_parcels_are_kept_whole_with_both_areas(self) -> None:
        # 宗地 4 只有邊線貼著邊界的右邊 (x = 250150)：範圍內面積是 0，不選
        parcels = [*PARCELS, ({"段名": "信義段", "地號": "4"}, square(250150, 2650000, 250250, 2650100))]
        touching = write_geojson(self.data_dir / "宗地加貼邊.geojson", parcels)
        for output_suffix in (".shp", ".gpkg"):
            with self.subTest(output=output_suffix):
                messages: List[str] = []
                output = self.out_dir / f"整筆{output_suffix}"
                result = clip_vector_layer(touching, self.boundary, output, keep_whole=True, text_callback=messages.append)
                rows = self.by_land_no(output)
                self.assertEqual(sorted(rows), ["1", "2"], "宗地 3 在外、宗地 4 只貼邊")
                self.assertEqual(result.feature_count, 2)
                self.assertEqual(result.area_m2, 10000.0, "回傳的是範圍內面積合計")
                self.assertAlmostEqual(total_area(output), 20000.0, delta=0.01, msg="圖形不切開")
                for land_no, cent_x in (("1", 250050.0), ("2", 250150.0)):
                    row = rows[land_no]
                    self.assertEqual(row["ORIG_M2"], 10000.0)
                    self.assertEqual(row["AREA_M2"], 5000.0)
                    self.assertEqual(row["AREA_HA"], 0.5)
                    self.assertEqual(row["RATIO_PCT"], 50.0)
                    self.assertEqual(row["LEN_M"], 400.0, "周長照整筆算")
                    self.assertEqual(row["CENT_X"], cent_x, "形心照整筆算")
                self.assertEqual(rows["1"]["段名"], "大安段")
                self.assertNotIn("_WL_FID", geo_io.read_layer_info(output).field_names)
                self.assertTrue(any("[整筆保留]" in message for message in messages), messages)

    def test_lines_and_points_are_kept_when_they_touch(self) -> None:
        features = [
            ({"編號": "穿過"}, line((250000, 2650000), (250300, 2650000))),
            ({"編號": "在外"}, line((260000, 2650000), (260100, 2650000))),
        ]
        pipes = write_geojson(self.data_dir / "管線.geojson", features)
        rows = {row["編號"]: row for row in attribute_rows(clip_vector_layer(
            pipes, self.boundary, self.out_dir / "管線.gpkg", keep_whole=True
        ).output_path)}
        self.assertEqual(sorted(rows), ["穿過"])
        self.assertEqual(rows["穿過"]["LEN_M"], 300.0, "線不切開")
        self.assertEqual(rows["穿過"]["AREA_M2"], 0.0)

        points = write_geojson(
            self.data_dir / "樁位.geojson",
            [({"樁號": "內"}, {"type": "Point", "coordinates": [250100, 2650050]}),
             ({"樁號": "外"}, {"type": "Point", "coordinates": [250010, 2650050]})],
        )
        kept = attribute_rows(clip_vector_layer(points, self.boundary, self.out_dir / "樁位.gpkg", keep_whole=True).output_path)
        self.assertEqual([row["樁號"] for row in kept], ["內"])

    def test_batch_engine_names_the_output_whole(self) -> None:
        from core.clip_master import BatchClipEngine

        engine = BatchClipEngine(self.boundary, self.out_dir, keep_whole_features=True, export_attribute_excel=True)
        results = engine.execute_batch([self.parcels_geojson])
        self.assertEqual(results[0].output_path.name, "宗地_whole.shp")
        self.assertEqual(results[0].attribute_excel.name, "宗地_whole.xlsx")  # type: ignore[union-attr]
        self.assertAlmostEqual(total_area(results[0].output_path), 20000.0, delta=0.01)


class ScaleVectorCoordinatesTest(VectorEngineTestCase):
    """P0-5：縮放 SQL 寫死 geometry，GPKG 來源 (幾何欄位 geom) 直接失敗。"""

    def test_millimetre_coordinates_scaled_to_metres(self) -> None:
        mm_geojson = write_geojson(
            self.data_dir / "毫米圖.geojson",
            [({"名稱": "基地"}, square(250000000, 2650000000, 250100000, 2650100000))],
        )
        for suffix in (".geojson", ".gpkg", ".shp"):
            with self.subTest(source=suffix):
                source = mm_geojson if suffix == ".geojson" else convert_vector(mm_geojson, self.data_dir / f"毫米圖{suffix}")
                output = self.out_dir / f"縮放_{suffix[1:]}.gpkg"
                crs_doctor.scale_vector_coordinates(source, output, 0.001)

                info = geo_io.read_layer_info(output)
                self.assertEqual(info.feature_count, 1)
                for actual, expected in zip(info.extent, (250000.0, 2650000.0, 250100.0, 2650100.0)):
                    self.assertAlmostEqual(actual, expected, places=3)
                self.assertEqual(attribute_rows(output)[0]["名稱"], "基地")

    def test_zero_scale_factor_is_rejected(self) -> None:
        with self.assertRaises(ToolkitError):
            crs_doctor.scale_vector_coordinates(self.parcels_geojson, self.out_dir / "x.gpkg", 0.0)


class MergeVectorLayersTest(VectorEngineTestCase):
    """P0-4：ogrmerge 啟動器無法執行，改用 python -m osgeo_utils.ogrmerge。"""

    def setUp(self) -> None:
        super().setUp()
        # 第二個來源是經緯度座標，合併時必須被重投影到 TWD97
        self.extra = write_geojson(
            self.data_dir / "補充宗地.geojson",
            [({"段名": "松山段", "地號": "9"}, square(121.0, 23.95, 121.001, 23.951))],
            epsg=4326,
        )

    def test_single_layer_merge_keeps_all_features_and_chinese_fields(self) -> None:
        inputs = [self.parcels_as(".shp"), convert_vector(self.extra, self.data_dir / "補充宗地.gpkg")]
        for suffix in (".gpkg", ".shp"):
            with self.subTest(output=suffix):
                output = self.out_dir / f"合併成果{suffix}"
                returned = vector_ops.merge_vector_layers(inputs, output)
                self.assertEqual(Path(returned), output.resolve())

                info = geo_io.read_layer_info(output)
                self.assertEqual(info.feature_count, 4)
                self.assertEqual(info.epsg, 3826)
                self.assertIn("段名", info.field_names)
                # 經緯度來源已轉成 TM2 公尺座標，整體範圍不會再出現 121 這種經度值
                self.assertGreater(info.extent[0], 100000.0)
                self.assertEqual(
                    sorted(row["段名"] for row in attribute_rows(output)),
                    ["信義段", "大安段", "大安段", "松山段"],
                )

    def test_multi_layer_merge_writes_one_layer_per_source(self) -> None:
        inputs = [self.parcels_as(".shp"), self.extra]
        output = self.out_dir / "多圖層.gpkg"
        vector_ops.merge_vector_layers(inputs, output, single_layer=False)
        self.assertEqual(len(geo_io.list_layer_names(output)), 2)

        with self.assertRaises(ToolkitError):
            vector_ops.merge_vector_layers(inputs, self.out_dir / "多圖層.shp", single_layer=False)

    def test_missing_input_is_reported(self) -> None:
        with self.assertRaises(FileNotFoundError):
            vector_ops.merge_vector_layers([self.parcels_geojson, self.data_dir / "不存在.shp"], self.out_dir / "m.gpkg")


class CadastralReportTest(VectorEngineTestCase):
    """P0-5 / P0-6 / P1-1：地籍交集清冊的面積必須是劃入工區的面積，且報表不可為空。"""

    def test_report_lists_clipped_area_per_parcel(self) -> None:
        for suffix in (".shp", ".gpkg"):
            with self.subTest(source=suffix):
                out_dir = self.out_dir / f"清冊_{suffix[1:]}"
                result = vector_ops.generate_cadastral_intersection_report(
                    self.boundary, self.parcels_as(suffix), output_dir=out_dir
                )
                self.assertEqual(result.total_parcels_count, 2)
                self.assertEqual(result.total_clipped_area_m2, 10000.0)
                self.assertEqual(result.total_clipped_area_ha, 1.0)
                self.assertEqual(result.total_original_area_m2, 20000.0)

                rows = self.by_land_no(result.clipped_vector_path)
                self.assertEqual(rows["1"]["CLIP_M2"], 5000.0)
                self.assertEqual(rows["2"]["RATIO_PCT"], 50.0)

                sheet = openpyxl.load_workbook(result.excel_report_path).active
                data = {sheet.cell(row=r, column=3).value: [sheet.cell(row=r, column=c).value for c in range(1, 10)] for r in (5, 6)}
                self.assertEqual(data["1"][1:], ["大安段", "1", 10000.0, 5000.0, 0.5, 50.0, 250075.0, 2650050.0])
                self.assertEqual(data["2"][1:], ["信義段", "2", 10000.0, 5000.0, 0.5, 50.0, 250125.0, 2650050.0])
                totals = [sheet.cell(row=7, column=c).value for c in range(1, 8)]
                self.assertEqual(totals, ["合計", "共計 2 筆地號", None, 20000.0, 10000.0, 1.0, 50.0])


class SplitByAttributeTest(VectorEngineTestCase):
    """P0-7：相異值解析、欄位名與值的跳脫、數值欄位、NULL 與空字串。"""

    def counts_by_file(self, files: List[Path]) -> Dict[str, int]:
        return {path.name: geo_io.read_layer_info(path).feature_count for path in files}

    def test_split_by_chinese_text_field(self) -> None:
        files = vector_ops.split_vector_by_attribute(self.parcels_as(".gpkg"), "段名", self.out_dir / "依段名")
        self.assertEqual(self.counts_by_file(files), {"宗地_大安段.shp": 2, "宗地_信義段.shp": 1})
        rows = attribute_rows(self.out_dir / "依段名" / "宗地_大安段.shp")
        self.assertEqual(sorted(row["地號"] for row in rows), ["1", "3"])

    def test_split_handles_quote_null_and_empty_string(self) -> None:
        # 欄位名刻意用小寫，比對不分大小寫
        files = vector_ops.split_vector_by_attribute(
            self.parcels_geojson, "note", self.out_dir / "依備註", output_format="GPKG"
        )
        self.assertEqual(
            self.counts_by_file(files),
            {"宗地_O'Brien.gpkg": 1, "宗地_空值NULL.gpkg": 1, "宗地_空白.gpkg": 1},
        )
        self.assertEqual(attribute_rows(self.out_dir / "依備註" / "宗地_O'Brien.gpkg")[0]["地號"], "1")
        self.assertEqual(attribute_rows(self.out_dir / "依備註" / "宗地_空值NULL.gpkg")[0]["地號"], "2")
        self.assertEqual(attribute_rows(self.out_dir / "依備註" / "宗地_空白.gpkg")[0]["地號"], "3")

    def test_split_by_integer_field(self) -> None:
        files = vector_ops.split_vector_by_attribute(
            self.parcels_as(".shp"), "ZONE_NO", self.out_dir / "依分區", output_format="GeoJSON"
        )
        self.assertEqual(self.counts_by_file(files), {"宗地_1.geojson": 2, "宗地_2.geojson": 1})

    def test_split_to_dxf_writes_one_file_per_value(self) -> None:
        files = vector_ops.split_vector_by_attribute(
            self.parcels_geojson, "段名", self.out_dir / "依段名DXF", output_format="DXF"
        )
        self.assertEqual(sorted(path.name for path in files), ["宗地_信義段.dxf", "宗地_大安段.dxf"])
        for path in files:
            self.assertGreater(path.stat().st_size, 0)

    def test_unknown_field_and_format_are_rejected(self) -> None:
        with self.assertRaises(ToolkitError) as ctx:
            vector_ops.split_vector_by_attribute(self.parcels_geojson, "不存在", self.out_dir / "x")
        self.assertIn("段名", str(ctx.exception))
        with self.assertRaises(ToolkitError):
            vector_ops.split_vector_by_attribute(self.parcels_geojson, "段名", self.out_dir / "x", output_format="KML")


class GenerateBufferTest(VectorEngineTestCase):
    """[G-08] 環域：來源不論 SHP 或 GPKG 都要算得出來，屬性要保留。"""

    def setUp(self) -> None:
        super().setUp()
        self.site_geojson = write_geojson(
            self.data_dir / "工區.geojson", [({"名稱": "甲工區"}, square(250000, 2650000, 250100, 2650100))]
        )

    def _assert_buffered(self, source: Path, output: Path) -> None:
        result = generate_buffer(source, output, buffer_distance=10.0)
        self.assertEqual(result, output.resolve())
        rows = attribute_rows(output)
        self.assertEqual([row["名稱"] for row in rows], ["甲工區"])
        # 100×100 外推 10 m：10000 + 4×1000 + 圓角 π×10² ≈ 14314 (圓角以折線近似，略小)
        area = total_area(output)
        self.assertGreater(area, 14300.0)
        self.assertLess(area, 14320.0)

    def test_buffer_from_shapefile_source(self) -> None:
        source = convert_vector(self.site_geojson, self.data_dir / "工區.shp")
        self._assert_buffered(source, self.out_dir / "環域_shp來源.gpkg")

    def test_buffer_from_gpkg_source_to_shapefile(self) -> None:
        source = convert_vector(self.site_geojson, self.data_dir / "工區.gpkg")
        output = self.out_dir / "環域_gpkg來源.shp"
        self._assert_buffered(source, output)
        self.assertEqual(output.with_suffix(".cpg").read_text(encoding="ascii").strip(), "UTF-8")

    def test_negative_buffer_shrinks_area(self) -> None:
        output = self.out_dir / "內縮.gpkg"
        generate_buffer(self.site_geojson, output, buffer_distance=-10.0)
        self.assertAlmostEqual(total_area(output), 6400.0, delta=1.0)


class DxfLinesToPolygonTest(VectorEngineTestCase):
    """[G-05] DXF 線轉面：四條首尾相接的線要圍成 100×100 的面。"""

    def _write_dxf(self, name: str, lines: List[Dict[str, Any]], cad_layer: str = "BOUNDARY") -> Path:
        source = write_geojson(self.data_dir / f"{name}.geojson", [({"id": idx}, geom) for idx, geom in enumerate(lines)])
        return convert_vector(source, self.data_dir / f"{name}.dxf", dxf_layer_name=cad_layer)

    def _closed_lines(self) -> List[Dict[str, Any]]:
        x0, y0, x1, y1 = 250000.0, 2650000.0, 250100.0, 2650100.0
        return [line((x0, y0), (x1, y0)), line((x1, y0), (x1, y1)), line((x1, y1), (x0, y1)), line((x0, y1), (x0, y0))]

    def test_four_lines_build_boundary_polygon(self) -> None:
        dxf = self._write_dxf("邊界線", self._closed_lines())
        output = self.out_dir / "master_boundary.gpkg"
        result = dxf_lines_to_polygon_boundary(dxf, output, cad_layer="BOUNDARY")

        self.assertAlmostEqual(result.area_m2, 10000.0, places=1)
        self.assertAlmostEqual(result.area_ha, 1.0, places=4)
        self.assertAlmostEqual(result.extent.min_x, 250000.0, places=2)
        self.assertAlmostEqual(result.extent.max_y, 2650100.0, places=2)
        info = geo_io.read_layer_info(output, "boundary")
        self.assertEqual(info.feature_count, 1)
        self.assertIn("Polygon", info.geometry_type)
        self.assertEqual(info.epsg, 3826)
        self.assertEqual(attribute_rows(output, "boundary"), [{"LAYER_NAME": "BOUNDARY"}])
        self.assertAlmostEqual(total_area(output, "boundary"), 10000.0, places=1)
        self.assertTrue(result.geojson_path.is_file())

    def test_all_layers_used_when_cad_layer_not_given(self) -> None:
        dxf = self._write_dxf("邊界線_全部", self._closed_lines())
        result = dxf_lines_to_polygon_boundary(dxf, self.out_dir / "all.gpkg")
        self.assertAlmostEqual(result.area_m2, 10000.0, places=1)

    def test_unknown_layer_is_reported(self) -> None:
        closed_dxf = self._write_dxf("邊界線_查無圖層", self._closed_lines())
        with self.assertRaises(ToolkitError):
            dxf_lines_to_polygon_boundary(closed_dxf, self.out_dir / "none.gpkg", cad_layer="O'Brien")
        self.assertFalse((self.out_dir / "none.gpkg").exists())

    def test_closed_lines_are_not_flagged_as_auto_closed(self) -> None:
        messages: List[str] = []
        result = dxf_lines_to_polygon_boundary(
            self._write_dxf("邊界線_已閉合", self._closed_lines()),
            self.out_dir / "closed.gpkg",
            text_callback=messages.append,
        )
        self.assertFalse(result.was_auto_closed)
        self.assertFalse(any("閉鎖" in message or "打斷" in message for message in messages))

    def test_open_lines_are_auto_closed_and_reported(self) -> None:
        # 少畫一條邊：末點接回首點 (100 m) 後仍是 100×100
        open_dxf = self._write_dxf("未閉合", self._closed_lines()[:3])
        output = self.out_dir / "open.gpkg"
        messages: List[str] = []
        result = dxf_lines_to_polygon_boundary(open_dxf, output, cad_layer="BOUNDARY", text_callback=messages.append)

        self.assertTrue(result.was_auto_closed)
        self.assertAlmostEqual(result.area_m2, 10000.0, places=1)
        info = geo_io.read_layer_info(output, "boundary")
        self.assertEqual(info.epsg, 3826)
        self.assertEqual(info.feature_count, 1)
        self.assertEqual(attribute_rows(output, "boundary"), [{"LAYER_NAME": "BOUNDARY"}])
        self.assertAlmostEqual(total_area(output, "boundary"), 10000.0, places=1)
        self.assertTrue(any("自動首尾閉鎖" in message and "100.000 m" in message for message in messages))

    def test_small_gaps_between_segments_are_snapped(self) -> None:
        # 四個角各留 0.1 m 左右的缺口，線段順序與方向也打亂
        x0, y0, x1, y1 = 250000.0, 2650000.0, 250100.0, 2650100.0
        gapped = [
            line((x1, y1 - 0.1), (x1, y0 + 0.1)),
            line((x0 + 0.1, y0), (x1 - 0.1, y0)),
            line((x0, y0 + 0.1), (x0, y1 - 0.1)),
            line((x1 - 0.1, y1), (x0 + 0.1, y1)),
        ]
        messages: List[str] = []
        result = dxf_lines_to_polygon_boundary(
            self._write_dxf("有缺口", gapped), self.out_dir / "gapped.gpkg", text_callback=messages.append
        )
        self.assertTrue(result.was_auto_closed)
        # 四個角各被截掉 0.1×0.1÷2
        self.assertAlmostEqual(result.area_m2, 10000.0 - 0.02, delta=0.02)
        self.assertTrue(any("最大缺口 0.141 m" in message for message in messages))

    def test_gap_beyond_snap_tolerance_is_rejected(self) -> None:
        x0, y0, x1, y1 = 250000.0, 2650000.0, 250100.0, 2650100.0
        parallel = [line((x0, y0), (x1, y0)), line((x0, y1), (x1, y1))]
        dxf = self._write_dxf("兩條平行線", parallel)
        output = self.out_dir / "parallel.gpkg"
        with self.assertRaisesRegex(ToolkitError, "互不相接"):
            dxf_lines_to_polygon_boundary(dxf, output)
        self.assertFalse(output.exists())
        # 放寬容許值後可以串接成面
        result = dxf_lines_to_polygon_boundary(dxf, output, snap_tolerance=150.0)
        self.assertAlmostEqual(result.area_m2, 10000.0, places=1)

    def test_negative_snap_tolerance_is_rejected(self) -> None:
        dxf = self._write_dxf("單線", [line((250000.0, 2650000.0), (250100.0, 2650000.0))])
        for bad in (-0.5, float("nan")):
            with self.assertRaisesRegex(ValueError, "閉鎖容許距離"):
                dxf_lines_to_polygon_boundary(dxf, self.out_dir / "bad.gpkg", snap_tolerance=bad)

    def test_crossing_lines_without_nodes_are_split(self) -> None:
        # 井字形：四條線都超出轉角 20 m，交點沒有打斷
        x0, y0, x1, y1 = 250000.0, 2650000.0, 250100.0, 2650100.0
        crossing = [
            line((x0 - 20, y0), (x1 + 20, y0)),
            line((x1, y0 - 20), (x1, y1 + 20)),
            line((x1 + 20, y1), (x0 - 20, y1)),
            line((x0, y1 + 20), (x0, y0 - 20)),
        ]
        messages: List[str] = []
        result = dxf_lines_to_polygon_boundary(
            self._write_dxf("井字線", crossing), self.out_dir / "crossing.gpkg", text_callback=messages.append
        )
        self.assertFalse(result.was_auto_closed)
        self.assertAlmostEqual(result.area_m2, 10000.0, places=1)
        self.assertAlmostEqual(result.extent.min_x, x0, places=2)
        self.assertTrue(any("打斷" in message for message in messages))

    def test_single_straight_line_cannot_form_boundary(self) -> None:
        dxf = self._write_dxf("單一直線", [line((250000.0, 2650000.0), (250100.0, 2650000.0))])
        output = self.out_dir / "single.gpkg"
        with self.assertRaises(ToolkitError):
            dxf_lines_to_polygon_boundary(dxf, output)
        self.assertFalse(output.exists())


class GeoIoTest(VectorEngineTestCase):
    """geo_io 共用工具：欄位名截斷、條件匯出、DXF 寫出 (GDAL_DATA 位於中文路徑)。"""

    def test_safe_shp_field_names_cut_on_character_boundary(self) -> None:
        names = ["工程用地面積", "工程用地比例", "AREA_M2", "VERY_LONG_FIELD_NAME"]
        mapping = geo_io.make_safe_shp_field_names(names, "UTF-8")
        self.assertEqual(list(mapping), names)
        self.assertEqual(mapping["AREA_M2"], "AREA_M2")
        new_names = list(mapping.values())
        self.assertEqual(len({name.casefold() for name in new_names}), len(names))
        for name in new_names:
            self.assertLessEqual(len(name.encode("utf-8")), geo_io.SHP_FIELD_NAME_MAX_BYTES)

    def test_export_shapefile_renames_long_fields_and_filters(self) -> None:
        source = write_geojson(
            self.data_dir / "長欄位.geojson",
            [({"工程用地面積": 1.5, "類別": "甲"}, square(0, 0, 10, 10)), ({"工程用地面積": 2.5, "類別": "乙"}, square(20, 0, 30, 10))],
        )
        staged = geo_io.stage_vector_to_gpkg(source, self.data_dir / "長欄位.gpkg")
        result = geo_io.export_vector(
            staged.path, staged.layer_name, self.out_dir / "長欄位.shp",
            where=geo_io.build_equality_filter(staged.find_field("類別"), "乙"),
        )
        self.assertIn("工程用地面積", result.renamed_fields)
        self.assertEqual((self.out_dir / "長欄位.cpg").read_text(encoding="ascii").strip().upper(), "UTF-8")
        rows = attribute_rows(result.output_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["類別"], "乙")
        self.assertEqual(rows[0][result.renamed_fields["工程用地面積"]], 2.5)

    def test_export_dxf_works_under_non_ascii_project_path(self) -> None:
        output = self.out_dir / "宗地.dxf"
        geo_io.export_vector(self.parcels_geojson, None, output, dxf_layer_name="PARCEL")
        self.assertGreater(output.stat().st_size, 0)
        self.assertGreaterEqual(geo_io.read_layer_info(output).feature_count, 3)

    def test_reprojection_works_under_non_ascii_project_path_without_appdata_copy(self) -> None:
        env = build_isolated_env()
        proj_dir = get_runtime_dir() / "Library" / "share" / "proj"
        self.assertEqual(env["PROJ_LIB"], env["PROJ_DATA"])
        self.assertTrue((get_toolkit_root() / env["PROJ_LIB"]).resolve().samefile(proj_dir))
        if not str(proj_dir).isascii():
            self.assertTrue(env["PROJ_LIB"].isascii())
            self.assertFalse(Path(env["PROJ_LIB"]).is_absolute())

        # 把 PROJ 的 user-writable 目錄導向空資料夾，確保不是靠 %LOCALAPPDATA%/proj 的舊複本才成功
        empty_user_dir = self.data_dir / "proj_user"
        empty_user_dir.mkdir()
        output = self.out_dir / "parcels_wgs84.gpkg"
        run_command(
            ["ogr2ogr", "-f", "GPKG", "-t_srs", "EPSG:4326", str(output), str(self.parcels_geojson)],
            extra_env={"PROJ_USER_WRITABLE_DIRECTORY": str(empty_user_dir)},
        )
        min_x, min_y, _max_x, _max_y = geo_io.read_layer_info(output).extent
        self.assertAlmostEqual(min_x, 121.0, places=3)
        self.assertAlmostEqual(min_y, 23.95, delta=0.05)
        self.assertEqual(list(empty_user_dir.iterdir()), [])

    def test_sql_quoting(self) -> None:
        self.assertEqual(geo_io.quote_ident('a"b'), '"a""b"')
        self.assertEqual(geo_io.quote_literal("O'Brien"), "'O''Brien'")


if __name__ == "__main__":
    unittest.main()
