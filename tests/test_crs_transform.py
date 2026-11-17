"""TWD67 轉換與控制點仿射測試 (tests/test_crs_transform.py)

涵蓋 P1-8 第二段：build_twd67_to_twd97_srs_args／convert_twd67_* 的 119 分帶，
以及 calculate_affine_transform／build_gcp_arguments 對共線控制點的檢查。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from core import crs_doctor, geo_io
from core.crs_doctor import ControlPoint
from core.env_runner import run_command
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    total_area,
    convert_vector,
    square,
    strip_prj,
    write_dem,
    write_geojson,
)

# 馬祖一帶的 119 分帶座標
MATSU_X, MATSU_Y = 345000.0, 2897000.0


def raster_origin_and_epsg(path: Path) -> Tuple[float, float, Optional[int]]:
    """讀出網格左上角座標與 EPSG 代碼。"""
    from osgeo import gdal

    geo_io.ensure_gdal_ready()
    dataset = gdal.Open(str(path))
    transform = dataset.GetGeoTransform()
    epsg = geo_io.identify_epsg_from_wkt(dataset.GetProjection())
    dataset = None
    return float(transform[0]), float(transform[3]), epsg


class Twd67SrsArgsTest(unittest.TestCase):
    """純參數組合。"""

    def test_each_zone_targets_its_own_twd97_zone(self) -> None:
        self.assertEqual(
            ["-s_srs", crs_doctor.TWD67_121_PROJ, "-t_srs", "EPSG:3826"],
            crs_doctor.build_twd67_to_twd97_srs_args(),
        )
        self.assertEqual(
            ["-s_srs", crs_doctor.TWD67_119_PROJ, "-t_srs", "EPSG:3825"],
            crs_doctor.build_twd67_to_twd97_srs_args(zone=119),
        )

    def test_target_can_be_overridden_and_bad_zone_is_rejected(self) -> None:
        self.assertEqual(
            ["-s_srs", crs_doctor.TWD67_119_PROJ, "-t_srs", "EPSG:3826"],
            crs_doctor.build_twd67_to_twd97_srs_args(zone=119, target_srs="TWD97_121"),
        )
        self.assertEqual("EPSG:4326", crs_doctor.build_twd67_to_twd97_srs_args(target_srs="EPSG:4326")[3])
        # 舊程式對 121 以外的任何值都默默當成 119
        for bad_zone in (120, 0, "121", None):
            with self.assertRaises(ValueError):
                crs_doctor.build_twd67_to_twd97_srs_args(zone=bad_zone)


class ConvertTwd67Test(IsolatedWorkspaceTestCase):
    """實際執行 ogr2ogr／gdalwarp 的 TWD67 → TWD97 轉換。"""

    def _unlabelled_parcel(self, name: str, x: float, y: float) -> Path:
        geojson = write_geojson(self.data_dir / f"{name}.geojson", [({"名稱": "甲"}, square(x, y, x + 100, y + 100))])
        return strip_prj(convert_vector(geojson, self.data_dir / f"{name}.shp"))

    def test_vector_zone_119_goes_to_epsg_3825(self) -> None:
        source = self._unlabelled_parcel("馬祖67", MATSU_X, MATSU_Y)
        output = crs_doctor.convert_twd67_vector_to_twd97(source, self.out_dir / "馬祖97.gpkg", zone=119)
        info = geo_io.read_layer_info(output)
        self.assertEqual(3825, info.epsg)

        # 與「指派 TWD67_119 後重投影到 EPSG:3825」的結果一致
        expected = geo_io.stage_vector_to_gpkg(
            source, self.out_dir / "expected.gpkg", source_srs=crs_doctor.TWD67_119_PROJ, target_srs="EPSG:3825"
        )
        for got, want in zip(info.extent, expected.extent):
            self.assertAlmostEqual(want, got, places=3)
        # 基準面平移約 830 m，分帶沒變所以不會出現十幾萬公尺的位移
        self.assertGreater(info.extent[0] - MATSU_X, 700.0)
        self.assertLess(info.extent[0] - MATSU_X, 950.0)
        self.assertLess(info.extent[1] - MATSU_Y, -100.0)
        self.assertGreater(info.extent[1] - MATSU_Y, -300.0)

    def test_vector_zone_121_default_and_explicit_target(self) -> None:
        source = self._unlabelled_parcel("本島67", 250000.0, 2650000.0)
        default = geo_io.read_layer_info(
            crs_doctor.convert_twd67_vector_to_twd97(source, self.out_dir / "本島97.gpkg")
        )
        self.assertEqual(3826, default.epsg)
        self.assertAlmostEqual(250828.0, default.extent[0], delta=5.0)

        # 119 分帶的來源也可以指定轉到 121 分帶
        matsu = self._unlabelled_parcel("馬祖67b", MATSU_X, MATSU_Y)
        to_121 = geo_io.read_layer_info(
            crs_doctor.convert_twd67_vector_to_twd97(matsu, self.out_dir / "馬祖121.gpkg", zone=119, target_srs="TWD97_121")
        )
        self.assertEqual(3826, to_121.epsg)
        self.assertLess(to_121.extent[0], 200000.0)

    def test_raster_zone_119_goes_to_epsg_3825(self) -> None:
        source = write_dem(
            self.data_dir / "馬祖67.tif", np.full((20, 20), 50.0), epsg=None, origin_x=MATSU_X, top_y=MATSU_Y
        )
        output = self.out_dir / "馬祖97.tif"
        crs_doctor.convert_twd67_raster_to_twd97(source, output, zone=119)
        origin_x, top_y, epsg = raster_origin_and_epsg(output)
        self.assertEqual(3825, epsg)
        self.assertGreater(origin_x - MATSU_X, 700.0)
        self.assertLess(origin_x - MATSU_X, 950.0)
        self.assertLess(top_y - MATSU_Y, -100.0)
        self.assertGreater(top_y - MATSU_Y, -300.0)

        default_output = self.out_dir / "本島97.tif"
        mainland = write_dem(self.data_dir / "本島67.tif", np.full((20, 20), 50.0), epsg=None)
        crs_doctor.convert_twd67_raster_to_twd97(mainland, default_output)
        self.assertEqual(3826, raster_origin_and_epsg(default_output)[2])

    def test_invalid_inputs_fail_before_running_gdal(self) -> None:
        source = write_dem(self.data_dir / "dem.tif", np.full((5, 5), 1.0), epsg=None)
        with self.assertRaises(ValueError):
            crs_doctor.convert_twd67_raster_to_twd97(source, self.out_dir / "x.tif", zone=120)
        self.assertFalse((self.out_dir / "x.tif").exists())
        with self.assertRaises(FileNotFoundError):
            crs_doctor.convert_twd67_raster_to_twd97(self.data_dir / "不存在.tif", self.out_dir / "y.tif")


class AffineTransformTest(unittest.TestCase):
    """控制點仿射變換的秩檢查與數值穩定性。"""

    def test_collinear_control_points_are_rejected(self) -> None:
        collinear = [
            ControlPoint(0.0, 0.0, 250000.0, 2650000.0),
            ControlPoint(10.0, 10.0, 250010.0, 2650010.0),
            ControlPoint(20.0, 20.0, 250020.0, 2650020.0),
            ControlPoint(35.0, 35.0, 250035.0, 2650035.0),
        ]
        with self.assertRaises(ValueError) as caught:
            crs_doctor.calculate_affine_transform(collinear)
        self.assertIn("共線", str(caught.exception))
        with self.assertRaises(ValueError):
            crs_doctor.build_gcp_arguments(collinear)

        # 水平線、垂直線、座標值很大的斜線
        for points in (
            [(0, 5), (10, 5), (20, 5)],
            [(7, 0), (7, 10), (7, 30)],
            [(250000, 2650000), (250100, 2650050), (250300, 2650150)],
        ):
            with self.subTest(points=points):
                with self.assertRaises(ValueError):
                    crs_doctor.calculate_affine_transform([ControlPoint(x, y, x, y) for x, y in points])

    def test_coincident_and_non_finite_points_are_rejected(self) -> None:
        same = [ControlPoint(5.0, 5.0, 100.0 + i, 200.0) for i in range(3)]
        with self.assertRaises(ValueError) as caught:
            crs_doctor.calculate_affine_transform(same)
        self.assertIn("重疊", str(caught.exception))

        broken = [ControlPoint(0, 0, 0, 0), ControlPoint(1, 0, 1, 0), ControlPoint(0, float("nan"), 0, 1)]
        with self.assertRaises(ValueError):
            crs_doctor.calculate_affine_transform(broken)

    def test_three_non_collinear_points_solve_exactly(self) -> None:
        # 旋轉 30 度、放大 0.001 倍 (公釐 → 公尺) 再平移到 TM2
        angle = np.radians(30.0)
        scale = 0.001
        a, b = scale * np.cos(angle), -scale * np.sin(angle)
        d, e = scale * np.sin(angle), scale * np.cos(angle)
        sources = [(1000.0, 2000.0), (51000.0, 2500.0), (3000.0, 82000.0)]
        points = [
            ControlPoint(x, y, a * x + b * y + 250000.0, d * x + e * y + 2650000.0) for x, y in sources
        ]
        result = crs_doctor.calculate_affine_transform(points)
        self.assertAlmostEqual(a, result.a, places=9)
        self.assertAlmostEqual(b, result.b, places=9)
        self.assertAlmostEqual(250000.0, result.c, places=4)
        self.assertAlmostEqual(2650000.0, result.f, places=4)
        self.assertLess(result.rmse, 1e-6)

    def test_large_tm2_source_coordinates_stay_accurate(self) -> None:
        # 來源與目的都是 TM2 量級 (TWD67 → TWD97 的平移)，矩陣未中心化時條件數很差
        sources = [(250000.0, 2650000.0), (250050.0, 2650000.0), (250000.0, 2650040.0), (250050.0, 2650040.0)]
        points = [ControlPoint(x, y, x + 828.0, y - 207.0) for x, y in sources]
        result = crs_doctor.calculate_affine_transform(points)
        self.assertAlmostEqual(1.0, result.a, places=7)
        self.assertAlmostEqual(0.0, result.b, places=7)
        self.assertAlmostEqual(1.0, result.e, places=7)
        self.assertLess(result.rmse, 1e-4)
        x, y = result.transform(250025.0, 2650020.0)
        self.assertAlmostEqual(250853.0, x, places=3)
        self.assertAlmostEqual(2649813.0, y, places=3)

    def test_gcp_arguments_for_valid_points(self) -> None:
        points = [ControlPoint(0, 0, 100, 200), ControlPoint(10, 0, 120, 200), ControlPoint(0, 10, 100, 220)]
        args = crs_doctor.build_gcp_arguments(points)
        self.assertEqual(3, args.count("-gcp"))
        self.assertEqual(["-order", "1"], args[-2:])

    def test_parse_control_points_text(self) -> None:
        raw = "# 圖面X, 圖面Y, 真實X, 真實Y\n0, 0, 250000, 2650000\n\n100\t0\t250100\t2650000\n0 50 250000，2650050\n"
        points = crs_doctor.parse_control_points_text(raw)
        self.assertEqual(3, len(points))
        self.assertEqual((100.0, 0.0, 250100.0, 2650000.0), (points[1].src_x, points[1].src_y, points[1].dst_x, points[1].dst_y))
        self.assertEqual(2650050.0, points[2].dst_y)
        self.assertEqual(["1", "2", "3"], [p.point_id for p in points])

    def test_parse_control_points_text_reports_bad_line(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 2 列應為 4 個數值"):
            crs_doctor.parse_control_points_text("0 0 1 1\n5 5 9\n")
        with self.assertRaisesRegex(ValueError, "第 1 列含有不是數值"):
            crs_doctor.parse_control_points_text("0 0 一 1\n")
        with self.assertRaisesRegex(ValueError, "第 1 列含有無限大"):
            crs_doctor.parse_control_points_text("0 0 nan 1\n")


class TestGcpGeoreference(IsolatedWorkspaceTestCase):
    """georeference_vector_with_gcps：圖面座標 (公尺、原點任意、有旋轉) 校正到 TWD97。"""

    # 圖面座標 -> 真實座標：旋轉 90 度再平移 (x' = 250000 - y, y' = 2650000 + x)
    @staticmethod
    def _to_real(x: float, y: float) -> Tuple[float, float]:
        return 250000.0 - y, 2650000.0 + x

    def _control_points(self):
        return [ControlPoint(x, y, *self._to_real(x, y)) for x, y in [(0.0, 0.0), (100.0, 0.0), (0.0, 80.0), (100.0, 80.0)]]

    def _drawing(self) -> Path:
        source = write_geojson(
            self.data_dir / "drawing.geojson",
            [({"name": "lot"}, square(10, 10, 60, 40))],
        )
        return strip_prj_geojson(source)

    def test_georeference_to_gpkg(self) -> None:
        logs = []
        output = self.out_dir / "georef.gpkg"
        result = crs_doctor.georeference_vector_with_gcps(self._drawing(), output, self._control_points(), text_callback=logs.append)
        self.assertEqual(output.resolve(), result.output_path)
        self.assertEqual(4, result.control_point_count)
        self.assertLess(result.transform.rmse, 1e-4)
        self.assertTrue(any("RMSE" in line for line in logs))

        info = geo_io.read_layer_info(output)
        self.assertEqual(1, info.feature_count)
        self.assertEqual(3826, info.epsg)
        min_x, min_y, max_x, max_y = info.extent
        # 圖面 (10..60, 10..40) -> 真實 x: 250000-40..250000-10, y: 2650000+10..2650000+60
        self.assertAlmostEqual(249960.0, min_x, places=3)
        self.assertAlmostEqual(249990.0, max_x, places=3)
        self.assertAlmostEqual(2650010.0, min_y, places=3)
        self.assertAlmostEqual(2650060.0, max_y, places=3)
        self.assertAlmostEqual(1500.0, total_area(output), delta=0.01)
        self.assertEqual("lot", attribute_rows(output)[0]["name"])

    def test_georeference_dxf_to_dxf(self) -> None:
        dxf = self.data_dir / "drawing.dxf"
        # DXF 驅動只收 Layer 這類固定欄位，圖面先另寫一份只有 Layer 屬性的來源
        plain = strip_prj_geojson(write_geojson(self.data_dir / "plain.geojson", [({"Layer": "LOT"}, square(10, 10, 60, 40))]))
        run_command(["ogr2ogr", "-f", "DXF", str(dxf), str(plain)])
        output = self.out_dir / "georef.dxf"
        crs_doctor.georeference_vector_with_gcps(dxf, output, self._control_points())
        min_x, min_y, max_x, max_y = geo_io.read_layer_info(output).extent
        self.assertAlmostEqual(249960.0, min_x, places=3)
        self.assertAlmostEqual(2650060.0, max_y, places=3)

    def test_too_few_or_collinear_points_are_rejected(self) -> None:
        drawing = self._drawing()
        with self.assertRaises(ValueError):
            crs_doctor.georeference_vector_with_gcps(drawing, self.out_dir / "a.gpkg", self._control_points()[:2])
        collinear = [ControlPoint(float(i), float(i), 250000.0 + i, 2650000.0 + i) for i in range(4)]
        with self.assertRaises(ValueError):
            crs_doctor.georeference_vector_with_gcps(drawing, self.out_dir / "b.gpkg", collinear)
        self.assertFalse((self.out_dir / "a.gpkg").exists())
        self.assertFalse((self.out_dir / "b.gpkg").exists())


def strip_prj_geojson(path: Path) -> Path:
    """把測試用 GeoJSON 的 crs 成員拿掉，模擬沒有座標系標籤的圖面座標檔。"""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("crs", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
