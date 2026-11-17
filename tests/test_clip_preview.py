"""步驟 9-2：裁切分頁的預覽 (core/clip_preview.py)。

1. 右欄縮圖：DEM／正射／向量的原始檔＋邊界紅框畫在實際位置；邊界先轉到原始檔的座標系 (TWD67 七參數)。
2. 瀏覽器地圖：原始／裁切後／裁切邊界三層可勾；網格轉成網頁座標後位置正確；圖徵很多的向量只放邊界附近；
   做不出來的圖層略過，三層都沒有才拋錯。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import List, Optional, Tuple
from unittest import mock

import numpy as np
from PIL import Image

from core import clip_preview, geo_io
from core.clip_preview import (
    STYLE_DEM,
    STYLE_IMAGE,
    STYLE_VECTOR,
    build_clip_preview_map,
    render_clip_thumbnail,
)
from core.env_runner import ToolkitError, run_command
from tests._support import IsolatedWorkspaceTestCase, square, write_dem, write_geojson
from tests.test_clip_ortho import write_ortho

# DEM／影像：左上角 (250000, 2650200)、40×40 格、每格 5 m → 範圍 200 m 見方
GRID_EXTENT = (250000.0, 2650000.0, 250200.0, 2650200.0)
BOUNDARY_SQUARE = square(250050, 2650050, 250150, 2650150)


def red_bbox(image_path: Path) -> Optional[Tuple[int, int, int, int]]:
    """紅框 (純紅) 像素的範圍 (左, 上, 右, 下)；沒有紅框時回傳 None。"""
    pixels = np.asarray(Image.open(image_path).convert("RGB")).astype(int)
    red = (pixels[..., 0] > 245) & (pixels[..., 1] < 30) & (pixels[..., 2] < 30)
    rows, cols = np.nonzero(red)
    if rows.size == 0:
        return None
    return int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())


def to_pixel(x: float, y: float, view: Tuple[float, float, float, float], width: int, height: int) -> Tuple[float, float]:
    min_x, min_y, max_x, max_y = view
    return (x - min_x) / (max_x - min_x) * width, (max_y - y) / (max_y - min_y) * height


def expanded(extent: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """縮圖畫面範圍：四邊各外擴 4%。"""
    margin = max(extent[2] - extent[0], extent[3] - extent[1]) * 0.04
    return extent[0] - margin, extent[1] - margin, extent[2] + margin, extent[3] + margin


def lonlat(x: float, y: float, epsg: int = 3826) -> Tuple[float, float]:
    from osgeo import osr

    geo_io.ensure_gdal_ready()
    src = osr.SpatialReference()
    src.ImportFromEPSG(epsg)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    for srs in (src, dst):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lon, lat, _ = osr.CoordinateTransformation(src, dst).TransformPoint(x, y)
    return lon, lat


def decode_js_escapes(html: str) -> str:
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), html)


def image_overlay_bounds(html: str) -> List[Tuple[float, float, float, float]]:
    """HTML 裡每個 ImageOverlay 的 (南, 西, 北, 東)。"""
    number = r"(-?[\d.]+(?:e-?\d+)?)"
    pattern = rf'L\.imageOverlay\(\s*"data:image/png;base64,[^"]+",\s*\[\[{number},\s*{number}\],\s*\[{number},\s*{number}\]\]'
    return [tuple(float(v) for v in match) for match in re.findall(pattern, html)]


class TestClipThumbnail(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.boundary = write_geojson(self.data_dir / "邊界.geojson", [({}, BOUNDARY_SQUARE)])

    def _assert_boundary_at(self, image_path: Path, view: Tuple[float, float, float, float], tolerance: float = 4.0) -> None:
        width, height = Image.open(image_path).size
        left, top = to_pixel(250050, 2650150, view, width, height)
        right, bottom = to_pixel(250150, 2650050, view, width, height)
        bbox = red_bbox(image_path)
        self.assertIsNotNone(bbox, "縮圖上沒有紅框")
        for got, want in zip(bbox, (left, top, right, bottom)):
            self.assertAlmostEqual(got, want, delta=tolerance)

    def test_dem_thumbnail_draws_boundary_at_its_position(self) -> None:
        rows, cols = np.mgrid[0:40, 0:40]
        dem = write_dem(self.data_dir / "地形.tif", (rows * 2.0 + cols).astype(float))
        out = render_clip_thumbnail(self.out_dir / "dem.png", style=STYLE_DEM, source=dem, boundary=self.boundary)
        self.assertEqual(Image.open(out).size, (1024, 1024))
        self._assert_boundary_at(out, expanded(GRID_EXTENT))
        # DEM 彩色高程：高處 (左上) 與低處 (右下) 顏色不同，且都不是畫布底色
        pixels = np.asarray(Image.open(out).convert("RGB")).astype(int)
        self.assertGreater(np.abs(pixels[100, 100] - pixels[900, 900]).sum(), 60)
        self.assertNotEqual(tuple(pixels[100, 100]), (226, 226, 226))

    def test_boundary_is_reprojected_into_twd67_source(self) -> None:
        # 無標籤 DEM 實際是 TWD67 座標 (比 TWD97 約往西 828 m、往北 207 m)；邊界是 TWD97，蓋在同一塊地
        dem = write_dem(
            self.data_dir / "舊圖.tif", np.full((40, 40), 30.0), origin_x=250000.0 - 828.0, top_y=2650200.0 + 207.0, epsg=None
        )
        out = render_clip_thumbnail(
            self.out_dir / "twd67.png", style=STYLE_DEM, source=dem, boundary=self.boundary, assigned_srs="TWD67_121"
        )
        width, _ = Image.open(out).size
        left, top, right, bottom = red_bbox(out)
        # 沒轉座標系的話邊界在 800 多公尺外，畫面會被撐大、紅框只剩約 100 像素寬
        self.assertAlmostEqual(right - left, 100.0 / 216.0 * width, delta=15)
        # 七參數與「828 m、207 m」約略值的差在 10 m 內 (約 47 像素)
        self.assertAlmostEqual(left, 58.0 / 216.0 * width, delta=50)
        self.assertAlmostEqual(top, 58.0 / 216.0 * width, delta=50)

    def test_ortho_thumbnail_stretches_16_bit(self) -> None:
        ortho = write_ortho(self.data_dir / "空拍.tif", data_type="UInt16", scale=200)
        out = render_clip_thumbnail(self.out_dir / "ortho.png", style=STYLE_IMAGE, source=ortho, boundary=self.boundary)
        pixels = np.asarray(Image.open(out).convert("RGB")).astype(int)
        # x < 250150 是 0 (暗)，以東有值 (拉伸後亮)
        self.assertLess(pixels[512, 200].sum(), 60)
        self.assertGreater(pixels[512, 900].sum(), 600)
        self._assert_boundary_at(out, expanded(GRID_EXTENT))

    def test_vector_thumbnail_draws_shared_edges(self) -> None:
        parcels = write_geojson(
            self.data_dir / "宗地.geojson",
            [({"n": 1}, square(250000, 2650000, 250100, 2650100)), ({"n": 2}, square(250100, 2650000, 250200, 2650100))],
        )
        out = render_clip_thumbnail(self.out_dir / "vector.png", style=STYLE_VECTOR, source=parcels)
        width, height = Image.open(out).size
        self.assertEqual((width, height), (1024, round(116 / 216 * 1024)))
        pixels = np.asarray(Image.open(out).convert("RGB"))
        row = height // 2
        edge = tuple(clip_preview._VECTOR_EDGE_COLOR[:3])
        fill = tuple(clip_preview._VECTOR_FILL_COLOR[:3])
        self.assertIn(edge, [tuple(pixels[row, col]) for col in range(508, 517)], "兩塊宗地共用的界線要畫出來")
        self.assertEqual(tuple(pixels[row, 300]), fill)
        self.assertIsNone(red_bbox(out), "沒有邊界就不畫紅框")

    def test_boundary_only_thumbnail(self) -> None:
        out = render_clip_thumbnail(self.out_dir / "only.png", style=STYLE_DEM, boundary=self.boundary)
        self._assert_boundary_at(out, expanded((250050.0, 2650050.0, 250150.0, 2650150.0)))
        pixels = np.asarray(Image.open(out).convert("RGB")).astype(int)
        center = pixels[512, 512]
        self.assertGreater(center[0], center[1] + 10, "只有邊界時框內淡紅填色")
        self.assertLess(center[0], 245, "填色是淡紅，不會被當成紅框")

    def test_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            render_clip_thumbnail(self.out_dir / "x.png", style=STYLE_DEM)
        with self.assertRaises(ValueError):
            render_clip_thumbnail(self.out_dir / "x.png", style="3d", boundary=self.boundary)


class TestClipPreviewMap(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.boundary = write_geojson(self.data_dir / "邊界.geojson", [({}, BOUNDARY_SQUARE)])

    def test_three_layers_and_raster_position(self) -> None:
        dem = write_dem(self.data_dir / "地形.tif", np.arange(1600, dtype=float).reshape(40, 40))
        clipped = write_dem(self.data_dir / "地形_dem_1m.tif", np.full((20, 20), 800.0), origin_x=250050, top_y=2650150)
        result = build_clip_preview_map(
            self.out_dir / "map.html", style=STYLE_DEM, source=dem, boundary=self.boundary, clipped=clipped
        )
        self.assertEqual(result.layer_names, ["原始：地形.tif", "裁切後：地形_dem_1m.tif", "裁切邊界"])
        html = decode_js_escapes(result.html_path.read_text(encoding="utf-8"))
        for name in result.layer_names:
            self.assertIn(name, html)

        overlays = image_overlay_bounds(html)
        self.assertEqual(len(overlays), 2)
        south, west, north, east = overlays[0]
        sw_lon, sw_lat = lonlat(250000, 2650000)
        ne_lon, ne_lat = lonlat(250200, 2650200)
        # 允許約一格 (5 m) 的誤差
        for got, want in ((south, sw_lat), (west, sw_lon), (north, ne_lat), (east, ne_lon)):
            self.assertAlmostEqual(got, want, delta=6e-5)
        # 裁切後的範圍在原始範圍裡面
        c_south, c_west, c_north, c_east = overlays[1]
        self.assertTrue(south <= c_south < c_north <= north and west <= c_west < c_east <= east)

    def test_many_features_only_near_boundary(self) -> None:
        parcels = write_geojson(
            self.data_dir / "全縣地籍.geojson",
            [
                ({"n": 1}, square(250000, 2650000, 250100, 2650100)),
                ({"n": 2}, square(250100, 2650100, 250200, 2650200)),
                ({"n": 3}, square(255000, 2655000, 255100, 2655100)),
            ],
        )
        messages: List[str] = []
        with mock.patch.object(clip_preview, "NEAR_BOUNDARY_FEATURE_LIMIT", 2):
            result = build_clip_preview_map(
                self.out_dir / "map.html", style=STYLE_VECTOR, source=parcels, boundary=self.boundary,
                text_callback=messages.append,
            )
        html = result.html_path.read_text(encoding="utf-8")
        self.assertRegex(html, r'"n":\s*1\b')
        self.assertNotRegex(html, r'"n":\s*3\b', "5 公里外的圖徵不放進地圖")
        self.assertTrue(any("只放邊界附近" in m for m in messages))

    def test_awkward_field_names_do_not_break_the_map(self) -> None:
        """欄位名帶括號、值都不重複：folium 不能把它寫進 JS；欄位名切在半個中文字上 (直接用 ogr2ogr 寫 SHP) 也放得上去。"""
        source = write_geojson(
            self.tmp_dir / "parcels.geojson",
            [({"面積(m2)": 100.0 + i, "使用分區": "農業區"}, square(250000 + i * 100, 2650000, 250100 + i * 100, 2650100)) for i in range(2)],
        )
        shp = self.data_dir / "宗地.shp"
        run_command(["ogr2ogr", "-f", "ESRI Shapefile", "-lco", "ENCODING=UTF-8", str(shp), str(source)], check=True)
        result = build_clip_preview_map(self.out_dir / "map.html", style=STYLE_VECTOR, source=shp, boundary=self.boundary)
        self.assertEqual(result.layer_names, ["原始：宗地.shp", "裁切邊界"])
        html = result.html_path.read_text(encoding="utf-8")
        self.assertNotIn("feature.properties.", html.replace("layer.feature.properties[v]", ""))

    def test_failed_layer_is_skipped(self) -> None:
        # 無標籤又不在臺灣座標範圍的 DEM 判斷不出座標系：「原始」層略過，邊界照樣放
        local = write_dem(self.data_dir / "區域座標.tif", np.ones((10, 10)), origin_x=100.0, top_y=200.0, epsg=None)
        messages: List[str] = []
        result = build_clip_preview_map(
            self.out_dir / "map.html", style=STYLE_DEM, source=local, boundary=self.boundary, text_callback=messages.append
        )
        self.assertEqual(result.layer_names, ["裁切邊界"])
        self.assertTrue(any("「原始：區域座標.tif」層略過" in m for m in messages))

        with self.assertRaises(ToolkitError):
            build_clip_preview_map(self.out_dir / "none.html", style=STYLE_DEM, source=local)


if __name__ == "__main__":
    unittest.main()
