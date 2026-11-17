"""邊界與網格沒有重疊時，裁切不可產出整片 NoData 的檔案還回報成功 (使用者實測：DXF 是 65k 的範圍、DSM 是 93k)。"""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from core.clip_master import BatchClipEngine, clip_dem_layer, clip_ortho_layer, raster_has_valid_data
from core.env_runner import ToolkitError
from tests._support import IsolatedWorkspaceTestCase, square, write_dem, write_geojson
from tests.test_clip_ortho import write_ortho

# write_dem／write_ortho 預設範圍：X 250000～250100 (DEM) 或 250200 (空拍)、Y 2650000 或 2650100～2650200


class ClipNoOverlapTest(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.far_boundary = write_geojson(
            self.data_dir / "遠處邊界.geojson", [({"id": 1}, square(266000, 2664000, 266300, 2664300))]
        )

    def test_dem_far_from_boundary_is_rejected_and_output_removed(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", np.ones((20, 20)))
        output = self.out_dir / "高程_clip.tif"
        with self.assertRaises(ToolkitError) as caught:
            clip_dem_layer(dem, self.far_boundary, output, target_resolution=10.0)
        message = str(caught.exception)
        self.assertIn("沒有任何有效資料", message)
        self.assertIn("邊界範圍 X 266000.0", message)
        self.assertIn("圖資範圍 X 250000.0", message)
        self.assertIn("相距約 21.", message)
        self.assertFalse(output.exists())

    def test_overlap_that_is_all_nodata_is_rejected(self) -> None:
        grid = np.full((20, 20), -9999.0)
        grid[:, 15:] = 5.0  # 只有東側 25 m 有值
        dem = write_dem(self.data_dir / "走廊.tif", grid)
        west = write_geojson(self.data_dir / "西側.geojson", [({"id": 1}, square(250000, 2650100, 250050, 2650200))])
        with self.assertRaisesRegex(ToolkitError, "沒有任何有效資料") as caught:
            clip_dem_layer(dem, west, self.out_dir / "西側.tif")
        self.assertNotIn("相距", str(caught.exception))

        east = write_geojson(self.data_dir / "東側.geojson", [({"id": 1}, square(250060, 2650100, 250100, 2650200))])
        result = clip_dem_layer(dem, east, self.out_dir / "東側.tif")
        self.assertTrue(result.is_success)
        self.assertTrue(raster_has_valid_data(result.output_path))

    def test_ortho_far_from_boundary_makes_no_pyramid_or_cad_background(self) -> None:
        ortho = write_ortho(self.data_dir / "空拍.tif")
        output = self.out_dir / "空拍_ortho.tif"
        with self.assertRaisesRegex(ToolkitError, "沒有任何有效資料"):
            clip_ortho_layer(ortho, self.far_boundary, output)
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_batch_reports_the_job_as_failed(self) -> None:
        dem = write_dem(self.data_dir / "93k.tif", np.ones((20, 20)))
        messages = []
        engine = BatchClipEngine(self.far_boundary, self.out_dir, dem_target_resolution=10.0)
        results = engine.execute_batch([dem], text_callback=messages.append)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].is_success)
        self.assertIn("沒有任何有效資料", results[0].error_message)
        self.assertTrue(any("成功 0 件 / 失敗 1 件" in m for m in messages))
        self.assertFalse((self.out_dir / "93k_dem_10m.tif").exists())

    def test_large_raster_is_sampled_without_missing_a_small_valid_patch(self) -> None:
        grid = np.full((2400, 2400), -9999.0)
        grid[1200:1210, 1200:1210] = 1.0  # 10×10 像元的有效區
        dem = write_dem(self.data_dir / "大圖.tif", grid, cell_size=0.1)
        self.assertTrue(raster_has_valid_data(dem))
        empty = write_dem(self.data_dir / "空圖.tif", np.full((2400, 2400), -9999.0), cell_size=0.1)
        self.assertFalse(raster_has_valid_data(empty))


if __name__ == "__main__":
    unittest.main()
