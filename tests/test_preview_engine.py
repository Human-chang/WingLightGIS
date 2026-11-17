"""預覽引擎 P1-9 整合測試 (tests/test_preview_engine.py)

1. [G-16] generate_3d_hillshade_preview：大 DEM 先縮圖再交給 Pillow、邊界外框位置、邊界座標系與 DEM 不同。
2. [G-17] generate_folium_wmts_map：空圖層、缺 NAME 欄位、多圖層、經緯度來源的抽稀、路徑不存在。
"""

from __future__ import annotations

import json
import math
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

import numpy as np
from PIL import Image

from core.preview_engine import (
    generate_3d_hillshade_preview,
    generate_folium_wmts_map,
    open_preview_in_browser,
    thin_and_reproject_vector_for_preview,
)
from tests._support import IsolatedWorkspaceTestCase, square, write_dem, write_geojson


def circle(center_x: float, center_y: float, radius: float, vertices: int = 72) -> Dict[str, Any]:
    """回傳近似圓形的面 (GeoJSON 幾何)。"""
    ring = [
        [center_x + radius * math.cos(2 * math.pi * i / vertices), center_y + radius * math.sin(2 * math.pi * i / vertices)]
        for i in range(vertices)
    ]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def html_text(text: str) -> str:
    """folium 把非 ASCII 文字寫成 JSON 的 Unicode 跳脫序列，比對前先轉成同樣的形式。"""
    return json.dumps(text)[1:-1]


def is_outline_red(pixel: Tuple[int, ...]) -> bool:
    return pixel[0] > 200 and pixel[1] < 80 and pixel[2] < 80


class Hillshade3DPreviewTest(IsolatedWorkspaceTestCase):
    """[G-16] 3D 山體陰影預覽。"""

    def setUp(self) -> None:
        super().setUp()
        # 600 × 400 格、5 m 網格：X 250000～253000、Y 2648200～2650200
        rows, cols = 400, 600
        row_idx, col_idx = np.indices((rows, cols))
        self.dem = write_dem(self.data_dir / "大範圍.tif", (col_idx * 0.5 + row_idx * 0.3 + 50.0).astype(np.float32))
        self.boundary_square = square(250750.0, 2648700.0, 252250.0, 2649700.0)

    def _assert_outline_at_left_edge(self, image_path: Path) -> None:
        # 縮成 200 × 133 後每像素 15 m：邊界左緣在 x = 750 / 15 = 50，上下緣在 y = 33.3 與 100
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            self.assertTrue(any(is_outline_red(rgb.getpixel((x, 66))) for x in range(48, 53)))
            self.assertFalse(is_outline_red(rgb.getpixel((100, 66))))
            self.assertFalse(is_outline_red(rgb.getpixel((20, 66))))

    def test_large_dem_is_downsampled_before_pillow_opens_it(self) -> None:
        boundary = write_geojson(self.data_dir / "邊界.geojson", [({"NAME": "工區"}, self.boundary_square)])
        output = self.out_dir / "預覽.png"

        # 把 Pillow 的像素上限壓到遠低於原圖 (240000 像素)，原尺寸一開就會拋 DecompressionBombError
        original_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = 50000
        try:
            result = generate_3d_hillshade_preview(self.dem, boundary_path=boundary, output_preview_path=output, max_preview_size=200)
        finally:
            Image.MAX_IMAGE_PIXELS = original_limit

        self.assertEqual((result.image_width, result.image_height), (200, 133))
        with Image.open(output) as image:
            self.assertEqual(image.size, (200, 133))
        self._assert_outline_at_left_edge(output)
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_preview3d_")], [])

    def test_boundary_in_other_srs_is_reprojected_to_dem(self) -> None:
        boundary_tm2 = write_geojson(self.data_dir / "邊界.geojson", [({"NAME": "工區"}, self.boundary_square)])
        boundary_lonlat = self.data_dir / "邊界_經緯度.geojson"
        thin_and_reproject_vector_for_preview(boundary_tm2, boundary_lonlat, simplify_tolerance=0.0)
        output = self.out_dir / "預覽_經緯度邊界.png"

        generate_3d_hillshade_preview(self.dem, boundary_path=boundary_lonlat, output_preview_path=output, max_preview_size=200)

        self._assert_outline_at_left_edge(output)

    def test_small_dem_keeps_original_size(self) -> None:
        dem = write_dem(self.data_dir / "小範圍.tif", np.tile(np.arange(60, dtype=np.float32), (40, 1)))

        result = generate_3d_hillshade_preview(dem, output_preview_path=self.out_dir / "小.png")

        self.assertEqual((result.image_width, result.image_height), (60, 40))

    def test_non_positive_preview_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate_3d_hillshade_preview(self.dem, output_preview_path=self.out_dir / "x.png", max_preview_size=0)


class FoliumMapTest(IsolatedWorkspaceTestCase):
    """[G-17] Folium 網頁預覽。"""

    def test_empty_layer_gives_map_without_features(self) -> None:
        empty = write_geojson(self.data_dir / "空圖層.geojson", [])
        messages: List[str] = []

        result = generate_folium_wmts_map(empty, output_html_path=self.out_dir / "空.html", text_callback=messages.append)

        self.assertTrue(result.html_path.is_file())
        self.assertEqual(result.feature_count, 0)
        self.assertAlmostEqual(result.center_lat, 23.9738, places=3)
        self.assertTrue(any("沒有圖徵" in message for message in messages))

    def test_area_fields_without_name_do_not_break_tooltip(self) -> None:
        parcels = write_geojson(
            self.data_dir / "宗地.geojson",
            [({"地號": "12-3", "AREA_M2": 10000.0, "AREA_HA": 1.0}, square(250000.0, 2650000.0, 250100.0, 2650100.0))],
        )

        result = generate_folium_wmts_map(parcels, output_html_path=self.out_dir / "宗地.html")

        html = result.html_path.read_text(encoding="utf-8")
        self.assertEqual(result.feature_count, 1)
        self.assertIn("AREA_M2", html)
        self.assertIn(html_text("面積 (公頃):"), html)
        self.assertNotIn(html_text("圖層/名稱:"), html)
        self.assertAlmostEqual(result.center_lon, 121.0005, places=3)

    def test_all_layers_are_added(self) -> None:
        first = write_geojson(self.data_dir / "宗地.geojson", [({"NAME": "甲"}, square(250000.0, 2650000.0, 250100.0, 2650100.0))])
        second = write_geojson(
            self.data_dir / "分區.geojson",
            [
                ({"分區": "農"}, square(250200.0, 2650000.0, 250300.0, 2650100.0)),
                ({"分區": "林"}, square(250300.0, 2650000.0, 250400.0, 2650100.0)),
            ],
        )
        missing = self.data_dir / "不存在.gpkg"
        messages: List[str] = []

        result = generate_folium_wmts_map(
            [first, second, missing], output_html_path=self.out_dir / "多圖層.html", text_callback=messages.append
        )

        html = result.html_path.read_text(encoding="utf-8")
        self.assertEqual(result.feature_count, 3)
        self.assertEqual(len(result.thinned_geojson_paths), 2)
        self.assertEqual(result.thinned_geojson_path, result.thinned_geojson_paths[0])
        self.assertIn(html_text("宗地.geojson"), html)
        self.assertIn(html_text("分區.geojson"), html)
        self.assertTrue(any("不存在.gpkg" in message for message in messages))
        # 中心取全部圖層的範圍中心：X 250000～250400 的中點約為東經 121.002
        self.assertAlmostEqual(result.center_lon, 121.00197, places=3)

    def test_lonlat_source_is_simplified_in_metres(self) -> None:
        # 半徑約 50 m 的圓；容許度 1.5 若被當成「度」，整個圓會被削成三角形
        lonlat = write_geojson(self.data_dir / "經緯度圓.geojson", [({"NAME": "圓"}, circle(121.0, 24.0, 0.0005))], epsg=4326)
        output = self.out_dir / "抽稀.geojson"

        thin_and_reproject_vector_for_preview(lonlat, output, simplify_tolerance=1.5)

        ring = json.loads(output.read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"][0]
        self.assertGreaterEqual(len(ring), 10)
        self.assertLess(len(ring), 73)
        self.assertAlmostEqual(ring[0][0], 121.0005, places=4)
        # 座標只留 6 位小數，避免內嵌到 HTML 的 GeoJSON 過大
        self.assertTrue(all(len(repr(value).split(".")[-1]) <= 6 for point in ring for value in point))

    def test_no_valid_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate_folium_wmts_map([self.data_dir / "不存在.gpkg"], output_html_path=self.out_dir / "x.html")


class OpenPreviewInBrowserTest(IsolatedWorkspaceTestCase):
    """file: 通訊協定沒有開啟指令的電腦，webbrowser 會退回 IE 開到 MSN 首頁；Windows 改用 .html 關聯開檔。"""

    def setUp(self) -> None:
        super().setUp()
        self.html = self.out_dir / "中文 資料夾" / "preview_map.html"
        self.html.parent.mkdir(parents=True)
        self.html.write_text("<html></html>", encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "只有 Windows 走 os.startfile")
    def test_windows_opens_the_file_path_with_its_association(self) -> None:
        with mock.patch("core.preview_engine.os.startfile", create=True) as startfile, mock.patch(
            "core.preview_engine.webbrowser.open"
        ) as browser:
            open_preview_in_browser(self.html)
        startfile.assert_called_once_with(str(self.html.resolve()))
        browser.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "只有 Windows 走 os.startfile")
    def test_falls_back_to_webbrowser_when_association_fails(self) -> None:
        with mock.patch("core.preview_engine.os.startfile", create=True, side_effect=OSError("沒有關聯")), mock.patch(
            "core.preview_engine.webbrowser.open"
        ) as browser:
            open_preview_in_browser(self.html)
        browser.assert_called_once_with(self.html.resolve().as_uri())

    def test_missing_file_opens_nothing(self) -> None:
        with mock.patch("core.preview_engine.os.startfile", create=True) as startfile, mock.patch(
            "core.preview_engine.webbrowser.open"
        ) as browser:
            open_preview_in_browser(self.out_dir / "不存在.html")
        startfile.assert_not_called()
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
