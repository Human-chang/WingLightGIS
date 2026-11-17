"""附世界檔的 JPG／PNG (格式支援 K2)：有世界檔才算正射影像、可以裁切；原圖解析度 (公尺) 給裁切頁說明與擋下比原圖細的數字。
全部用程式產生的假圖片與世界檔。"""

from __future__ import annotations

import unittest

import numpy as np

from core import geo_io
from core.clip_master import TrackType, clip_ortho_layer, detect_track_type
from core.crs_doctor import raster_pixel_size_m, sniff_crs
from tests._support import IsolatedWorkspaceTestCase, write_dem, write_geojson, write_world_image


class TestWorldFileImages(IsolatedWorkspaceTestCase):
    def test_world_file_decides_whether_an_image_is_ortho(self) -> None:
        photo = write_world_image(self.data_dir / "現場照片.jpg", pixel=None)
        jpg = write_world_image(self.data_dir / "底圖.jpg")
        png = write_world_image(self.data_dir / "掃描圖.png", pixel=0.25)
        wld = write_world_image(self.data_dir / "另一種.jpeg", pixel=None)
        wld.with_suffix(".wld").write_text("1\n0\n0\n-1\n250000.5\n2650199.5\n", encoding="ascii")

        self.assertFalse(geo_io.has_world_file(photo))
        for path in (jpg, png, wld):
            with self.subTest(path.name):
                self.assertTrue(geo_io.has_world_file(path))
                self.assertEqual(detect_track_type(path), TrackType.ORTHO_AERIAL)
        self.assertEqual(detect_track_type(photo), TrackType.UNKNOWN)
        # 世界檔沒有座標系：依座標範圍推測 TWD97
        self.assertEqual(sniff_crs(jpg).recommended_assign_srs, "EPSG:3826")

    def test_pixel_size_in_metres(self) -> None:
        self.assertEqual(raster_pixel_size_m(write_world_image(self.data_dir / "a.png", pixel=0.05)), (0.05, False))
        self.assertEqual(raster_pixel_size_m(write_dem(self.data_dir / "d.tif", np.ones((4, 4)), cell_size=20.0)), (20.0, False))
        # 經緯度的 DEM (約北緯 23.5 度、0.0001 度) 換算約 11 m，是估計值
        geo = write_dem(self.data_dir / "g.tif", np.ones((4, 4)), origin_x=121.0, top_y=23.5, cell_size=0.0001, epsg=4326)
        size, approx = raster_pixel_size_m(geo)
        self.assertTrue(approx)
        self.assertAlmostEqual(size, 11.13, places=1)

    def test_jpg_with_world_file_is_clipped_to_geotiff(self) -> None:
        source = write_world_image(self.data_dir / "底圖.jpg")
        triangle = {
            "type": "Polygon",
            "coordinates": [[[250100, 2650000], [250200, 2650000], [250200, 2650100], [250100, 2650000]]],
        }
        boundary = write_geojson(self.data_dir / "三角邊界.geojson", [({"id": 1}, triangle)])
        result = clip_ortho_layer(
            source, boundary, self.out_dir / "底圖裁切.tif", generate_pyramids=False, target_resolution=10.0,
            assigned_srs="TWD97_121",
        )
        self.assertTrue(result.output_path.exists())
        self.assertEqual(result.raster_dims, (10, 10))
        self.assertAlmostEqual(raster_pixel_size_m(result.output_path)[0], 10.0)
        self.assertTrue(result.cad_bg_jgw.exists())


if __name__ == "__main__":
    unittest.main()
