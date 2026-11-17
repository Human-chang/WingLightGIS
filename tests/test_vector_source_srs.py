"""向量工具箱的來源座標系測試 (tests/test_vector_source_srs.py)

涵蓋 P1-2 (vector_ops 部分)：
1. merge_vector_layers / simplify_vector_geometry / generate_cadastral_intersection_report：
   無標籤來源依範圍推測或採用 assigned_srs；來源有座標系時不得被覆寫。
2. split_vector_by_attribute：不重投影，只有在指派時才把座標系標到輸出檔。
3. convert_csv_table_to_points：表格沒有座標系標籤，依範圍推測或指派後重投影。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from core import geo_io, vector_ops
from core.crs_doctor import SourceSrsUndeterminedError
from core.env_runner import ToolkitError
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    square,
    strip_prj,
    total_area,
    write_geojson,
)

TM2_PARCEL = square(250000, 2650000, 250100, 2650100)
# 橫跨東經 121 度 (X = 250000) 的宗地，寬約 101.7 m、高約 110.8 m，面積約 11,270 平方公尺
LONLAT_PARCEL = square(120.9995, 24.0, 121.0005, 24.001)
# TWD67 → TWD97 約往東 828 m、往南 207 m
DATUM_SHIFT = (828.0, -207.0)


class VectorSourceSrsTestCase(IsolatedWorkspaceTestCase):
    """準備無標籤 TM2 宗地與帶 WGS84 標籤宗地的共用基底。"""

    def unlabelled_shapefile(self, name: str, features: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", features)
        return strip_prj(convert_vector(geojson, self.data_dir / f"{name}.shp"))

    def setUp(self) -> None:
        super().setUp()
        self.unlabelled = self.unlabelled_shapefile("無標籤宗地", [({"段名": "大安段", "地號": "1"}, TM2_PARCEL)])
        self.lonlat = write_geojson(
            self.data_dir / "經緯度宗地.geojson", [({"段名": "松山段", "地號": "9"}, LONLAT_PARCEL)], epsg=4326
        )


class MergeSourceSrsTest(VectorSourceSrsTestCase):
    """合併：每個來源各自決定座標系。"""

    def test_unlabelled_source_is_guessed_and_labelled_source_reprojected(self) -> None:
        output = self.out_dir / "合併_推測.gpkg"
        messages: List[str] = []
        vector_ops.merge_vector_layers([self.unlabelled, self.lonlat], output, text_callback=messages.append)

        info = geo_io.read_layer_info(output)
        self.assertEqual(2, info.feature_count)
        self.assertEqual(3826, info.epsg)
        # 無標籤宗地 X 250000～250100；經緯度宗地重投影後 X 約 249949～250051
        self.assertAlmostEqual(249949.0, info.extent[0], delta=2.0)
        self.assertAlmostEqual(250100.0, info.extent[2], delta=0.01)
        self.assertAlmostEqual(10000.0 + 11270.0, total_area(output), delta=150.0)
        guesses = [message for message in messages if "座標系為推測值" in message]
        self.assertEqual(1, len(guesses))
        self.assertIn("無標籤宗地.shp", guesses[0])

    def test_assigned_srs_only_applies_to_unlabelled_source(self) -> None:
        output = self.out_dir / "合併_指派67.gpkg"
        vector_ops.merge_vector_layers([self.unlabelled, self.lonlat], output, assigned_srs="TWD67_121")

        info = geo_io.read_layer_info(output)
        # 經緯度宗地留在原地，無標籤宗地以 TWD67 解讀後往東移約 828 m
        self.assertAlmostEqual(249949.0, info.extent[0], delta=2.0)
        self.assertAlmostEqual(250100.0 + DATUM_SHIFT[0], info.extent[2], delta=30.0)
        self.assertAlmostEqual(10000.0 + 11270.0, total_area(output), delta=150.0)

    def test_unlabelled_source_outside_known_ranges_is_rejected(self) -> None:
        millimetres = self.unlabelled_shapefile(
            "無標籤公釐宗地", [({"地號": "1"}, square(250000000, 2650000000, 250100000, 2650100000))]
        )
        output = self.out_dir / "不該產生.gpkg"
        with self.assertRaises(SourceSrsUndeterminedError):
            vector_ops.merge_vector_layers([self.lonlat, millimetres], output)
        self.assertFalse(output.exists())


class SimplifySourceSrsTest(VectorSourceSrsTestCase):
    """抽稀：無標籤來源的推測與指派、有標籤來源不被覆寫。"""

    def test_unlabelled_shapefile_is_guessed_as_twd97(self) -> None:
        output = self.out_dir / "抽稀_推測.shp"
        messages: List[str] = []
        vector_ops.simplify_vector_geometry(self.unlabelled, output, tolerance=1.0, text_callback=messages.append)

        info = geo_io.read_layer_info(output)
        self.assertEqual(3826, info.epsg)
        self.assertEqual((250000.0, 2650000.0, 250100.0, 2650100.0), tuple(info.extent))
        self.assertEqual([{"段名": "大安段", "地號": "1"}], attribute_rows(output))
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_assigned_twd67_shifts_result_by_datum_offset(self) -> None:
        output = self.out_dir / "抽稀_指派67.gpkg"
        messages: List[str] = []
        vector_ops.simplify_vector_geometry(
            self.unlabelled, output, assigned_srs="TWD67_121", text_callback=messages.append
        )

        info = geo_io.read_layer_info(output)
        self.assertAlmostEqual(250000.0 + DATUM_SHIFT[0], info.extent[0], delta=30.0)
        self.assertAlmostEqual(2650000.0 + DATUM_SHIFT[1], info.extent[1], delta=30.0)
        self.assertAlmostEqual(10000.0, total_area(output), delta=5.0)
        self.assertTrue(any("使用者指派" in message for message in messages))

    def test_embedded_wgs84_is_reprojected_and_assignment_ignored(self) -> None:
        output = self.out_dir / "抽稀_經緯度.gpkg"
        messages: List[str] = []
        vector_ops.simplify_vector_geometry(
            self.lonlat, output, assigned_srs="TWD97_121", text_callback=messages.append
        )

        info = geo_io.read_layer_info(output)
        self.assertEqual(3826, info.epsg)
        self.assertAlmostEqual(250000.0, (info.extent[0] + info.extent[2]) / 2.0, delta=1.0)
        # 被當成 TWD97 的話面積會是 1e-6 的量級
        self.assertAlmostEqual(11270.0, total_area(output), delta=150.0)
        self.assertFalse(any("指派" in message or "推測" in message for message in messages))


    def test_tolerance_is_applied_in_target_metres(self) -> None:
        # 下緣中點往外凸 0.5 m (面積多 25 m²)：容許值 1 m 會抽掉，0.1 m 不會
        bumped = {
            "type": "Polygon",
            "coordinates": [[
                [250000, 2650000], [250050, 2649999.5], [250100, 2650000],
                [250100, 2650100], [250000, 2650100], [250000, 2650000],
            ]],
        }
        source = write_geojson(self.data_dir / "凸點宗地.geojson", [({"地號": "1"}, bumped)])
        coarse = vector_ops.simplify_vector_geometry(source, self.out_dir / "抽稀_1m.gpkg", tolerance=1.0)
        fine = vector_ops.simplify_vector_geometry(source, self.out_dir / "抽稀_10cm.gpkg", tolerance=0.1)
        self.assertAlmostEqual(10000.0, total_area(coarse), delta=0.01)
        self.assertAlmostEqual(10025.0, total_area(fine), delta=0.01)


class CadastralReportSourceSrsTest(VectorSourceSrsTestCase):
    """地籍交集清冊：地籍圖沒有座標系標籤時的推測與指派。"""

    def setUp(self) -> None:
        super().setUp()
        self.boundary = write_geojson(
            self.data_dir / "邊界.geojson", [({"id": 1}, square(250050, 2640000, 260000, 2660000))]
        )

    def test_unlabelled_cadastral_map_is_guessed_as_twd97(self) -> None:
        messages: List[str] = []
        result = vector_ops.generate_cadastral_intersection_report(
            self.boundary, self.unlabelled, output_dir=self.out_dir / "推測", text_callback=messages.append
        )
        self.assertEqual(1, result.total_parcels_count)
        self.assertEqual(5000.0, result.total_clipped_area_m2)
        self.assertEqual(10000.0, result.total_original_area_m2)
        row = attribute_rows(result.clipped_vector_path)[0]
        self.assertEqual((250075.0, 2650050.0), (row["CENT_X"], row["CENT_Y"]))
        self.assertTrue(any("座標系為推測值" in message for message in messages))

    def test_assigned_twd67_moves_parcel_fully_inside_boundary(self) -> None:
        result = vector_ops.generate_cadastral_intersection_report(
            self.boundary, self.unlabelled, output_dir=self.out_dir / "指派67", assigned_srs="TWD67_121"
        )
        # 往東移約 828 m 後整筆落在邊界 (X ≥ 250050) 內
        self.assertEqual(1, result.total_parcels_count)
        self.assertAlmostEqual(10000.0, result.total_clipped_area_m2, delta=5.0)
        row = attribute_rows(result.clipped_vector_path)[0]
        self.assertAlmostEqual(250050.0 + DATUM_SHIFT[0], row["CENT_X"], delta=30.0)
        self.assertAlmostEqual(2650050.0 + DATUM_SHIFT[1], row["CENT_Y"], delta=30.0)
        self.assertAlmostEqual(100.0, row["RATIO_PCT"], delta=0.1)

    def test_embedded_wgs84_cadastral_map_ignores_assignment(self) -> None:
        result = vector_ops.generate_cadastral_intersection_report(
            self.boundary, self.lonlat, output_dir=self.out_dir / "經緯度", assigned_srs="TWD97_121"
        )
        # 宗地 X 約 249949～250051，邊界從 250050 起，只劃入最東邊約 1 m 寬的一條
        self.assertEqual(1, result.total_parcels_count)
        self.assertAlmostEqual(11270.0, result.total_original_area_m2, delta=150.0)
        self.assertGreater(result.total_clipped_area_m2, 10.0)
        self.assertLess(result.total_clipped_area_m2, 300.0)


class SplitSourceSrsTest(VectorSourceSrsTestCase):
    """拆檔：座標原樣保留，只有指派時才標上座標系。"""

    FEATURES = [
        ({"段名": "大安段", "地號": "1"}, square(250000, 2650000, 250100, 2650100)),
        ({"段名": "信義段", "地號": "2"}, square(250100, 2650000, 250200, 2650100)),
    ]

    def test_unlabelled_source_stays_unlabelled_without_assignment(self) -> None:
        # 公釐座標無法推測，但拆檔不重投影，不應該因此失敗
        source = self.unlabelled_shapefile(
            "公釐宗地", [({"段名": "大安段"}, square(250000000, 2650000000, 250100000, 2650100000))]
        )
        files = vector_ops.split_vector_by_attribute(source, "段名", self.out_dir / "無標籤", output_format="GPKG")
        self.assertEqual(1, len(files))
        info = geo_io.read_layer_info(files[0])
        self.assertFalse(info.has_srs)
        self.assertEqual((250000000.0, 2650000000.0, 250100000.0, 2650100000.0), tuple(info.extent))

    def test_assigned_srs_labels_outputs_without_moving_coordinates(self) -> None:
        source = self.unlabelled_shapefile("待拆宗地", self.FEATURES)
        messages: List[str] = []
        files = vector_ops.split_vector_by_attribute(
            source, "段名", self.out_dir / "指派", assigned_srs="TWD97_121", text_callback=messages.append
        )
        self.assertEqual(2, len(files))
        extents = {}
        for path in files:
            info = geo_io.read_layer_info(path)
            self.assertEqual(3826, info.epsg)
            extents[attribute_rows(path)[0]["段名"]] = tuple(info.extent)
        self.assertEqual((250000.0, 2650000.0, 250100.0, 2650100.0), extents["大安段"])
        self.assertEqual((250100.0, 2650000.0, 250200.0, 2650100.0), extents["信義段"])
        self.assertTrue(any("使用者指派" in message for message in messages))

    def test_embedded_srs_is_kept_when_assignment_given(self) -> None:
        files = vector_ops.split_vector_by_attribute(
            self.lonlat, "段名", self.out_dir / "有標籤", output_format="GPKG", assigned_srs="TWD67_121"
        )
        info = geo_io.read_layer_info(files[0])
        self.assertEqual(4326, info.epsg)
        self.assertAlmostEqual(120.9995, info.extent[0], places=6)


class CsvToPointsSourceSrsTest(IsolatedWorkspaceTestCase):
    """座標表格轉點：表格沒有座標系標籤。"""

    def _write_csv(self, name: str, header: str, rows: Sequence[str]) -> Path:
        path = self.data_dir / f"{name}.csv"
        path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
        return path

    def test_tm2_table_is_guessed_as_twd97(self) -> None:
        table = self._write_csv("樁位", "點名,X,Y", ["甲,250000,2650000", "乙,250100,2650100"])
        messages: List[str] = []
        for suffix in (".gpkg", ".shp"):
            with self.subTest(output=suffix):
                output = self.out_dir / f"樁位{suffix}"
                vector_ops.convert_csv_table_to_points(table, output, text_callback=messages.append)
                info = geo_io.read_layer_info(output)
                self.assertEqual(3826, info.epsg)
                self.assertEqual(2, info.feature_count)
                self.assertEqual((250000.0, 2650000.0, 250100.0, 2650100.0), tuple(info.extent))
                self.assertEqual(["甲", "乙"], [row["點名"] for row in attribute_rows(output)])
        self.assertTrue(any("座標系為推測值" in message and "樁位.csv" in message for message in messages))

    def test_lonlat_table_is_reprojected_to_target(self) -> None:
        table = self._write_csv("經緯度樁位", "name,Lon,Lat", ["A,121.0,24.0"])
        output = self.out_dir / "經緯度樁位.gpkg"
        vector_ops.convert_csv_table_to_points(table, output)
        info = geo_io.read_layer_info(output)
        self.assertEqual(3826, info.epsg)
        self.assertAlmostEqual(250000.0, info.extent[0], delta=0.01)
        self.assertGreater(info.extent[1], 2600000.0)

    def test_assigned_twd67_shifts_points_under_both_keyword_names(self) -> None:
        table = self._write_csv("舊樁位", "點名,X,Y", ["甲,250000,2650000"])
        for keyword in ("assigned_srs", "source_srs"):
            with self.subTest(keyword=keyword):
                output = self.out_dir / f"舊樁位_{keyword}.gpkg"
                vector_ops.convert_csv_table_to_points(table, output, **{keyword: "TWD67_121"})
                info = geo_io.read_layer_info(output)
                self.assertAlmostEqual(250000.0 + DATUM_SHIFT[0], info.extent[0], delta=30.0)
                self.assertAlmostEqual(2650000.0 + DATUM_SHIFT[1], info.extent[1], delta=30.0)

    def test_table_without_coordinate_columns_is_reported(self) -> None:
        table = self._write_csv("沒有座標", "點名,備註", ["甲,無"])
        output = self.out_dir / "沒有座標.gpkg"
        with self.assertRaises(ToolkitError) as ctx:
            vector_ops.convert_csv_table_to_points(table, output)
        self.assertIn("找不到座標欄位", str(ctx.exception))
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
