"""批次多幅 DEM 自動拼接 整合測試 (tests/test_batch_dem_mosaic.py)

BatchClipEngine 投入多幅 DEM 時先 gdalbuildvrt 拼接再裁切成單一成果；
座標系不同或範圍大幅重疊 (多期 DEM) 時不可拼接，改回逐幅裁切。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, List

import numpy as np

from core import geo_io
from core.clip_master import BatchClipEngine, TrackType, clip_dem_layer, find_dem_mosaic_blocker
from core.env_runner import ToolkitError
from tests._support import IsolatedWorkspaceTestCase, square, write_dem, write_geojson


def flat(value: float, cols: int = 20, rows: int = 40) -> np.ndarray:
    return np.full((rows, cols), value, dtype=np.float32)


def read_band(path: Path) -> Any:
    from osgeo import gdal

    geo_io.ensure_gdal_ready()
    dataset = gdal.Open(str(path))
    try:
        return dataset.GetRasterBand(1).ReadAsArray()
    finally:
        dataset = None


class BatchDemMosaicTest(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 左幅 X 250000～250100 (高程 100)、右幅 X 250100～250200 (高程 200)，Y 皆為 2650000～2650200
        self.left = write_dem(self.data_dir / "左幅.tif", flat(100.0), origin_x=250000.0)
        self.right = write_dem(self.data_dir / "右幅.tif", flat(200.0), origin_x=250100.0)
        # 邊界橫跨兩幅：X 250050～250150、Y 2650050～2650150
        self.boundary = write_geojson(
            self.data_dir / "邊界.geojson", [({"NAME": "工區"}, square(250050.0, 2650050.0, 250150.0, 2650150.0))]
        )
        self.batch_dir = self.out_dir / "批次"

    def _leftover_mosaic_temps(self) -> List[str]:
        return [p.name for p in self.work_temp.iterdir() if "mosaic" in p.name]

    def test_adjacent_tiles_are_mosaicked_into_one_result(self) -> None:
        messages: List[str] = []
        engine = BatchClipEngine(self.boundary, self.batch_dir)

        results = engine.execute_batch([self.left, self.right], text_callback=messages.append)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result.is_success, result.error_message)
        self.assertEqual(result.track_type, TrackType.TERRAIN_DEM)
        self.assertEqual(result.output_path, self.batch_dir / "dem_mosaic.tif")
        self.assertEqual(result.input_path, self.left)
        self.assertEqual(result.merged_input_paths, [self.left, self.right])
        self.assertEqual(result.raster_dims, (20, 20))
        band = read_band(result.output_path)
        # 左半來自左幅、右半來自右幅，中間沒有缺縫
        np.testing.assert_allclose(band[:, :9], 100.0, atol=1e-3)
        np.testing.assert_allclose(band[:, 11:], 200.0, atol=1e-3)
        self.assertTrue(np.all((band >= 100.0) & (band <= 200.0)))
        self.assertTrue(any("DEM 拼接" in message and "2 幅" in message for message in messages))
        self.assertTrue(any("成功 1 件 / 失敗 0 件" in message for message in messages))
        self.assertEqual(self._leftover_mosaic_temps(), [])
        self.assertEqual([p.name for p in self.batch_dir.iterdir()], ["dem_mosaic.tif"])

    def test_mosaic_keeps_position_among_other_files_and_resolution_tag(self) -> None:
        parcels = write_geojson(
            self.data_dir / "宗地.geojson", [({"地號": "1"}, square(250000.0, 2650000.0, 250200.0, 2650200.0))]
        )
        progress: List[str] = []
        engine = BatchClipEngine(self.boundary, self.batch_dir, dem_target_resolution=10.0)

        results = engine.execute_batch(
            [self.left, parcels, self.right], progress_callback=lambda _p, msg: progress.append(msg)
        )

        self.assertEqual([r.track_type for r in results], [TrackType.TERRAIN_DEM, TrackType.VECTOR_CADASTRE])
        self.assertTrue(all(r.is_success for r in results), [r.error_message for r in results])
        self.assertEqual(results[0].output_path.name, "dem_mosaic_10m.tif")
        self.assertEqual(results[0].raster_dims, (10, 10))
        self.assertTrue(any(msg.startswith("[1/2]") and "等 2 幅 DEM" in msg for msg in progress))

    def test_fractional_resolution_keeps_its_decimal_in_file_name(self) -> None:
        # 原本用 int() 取整，0.5 m 會被寫成 _0m
        engine = BatchClipEngine(self.boundary, self.batch_dir, dem_target_resolution=2.5, mosaic_dems=False)
        results = engine.execute_batch([self.left])
        self.assertTrue(results[0].is_success, results[0].error_message)
        self.assertEqual(results[0].output_path.name, "左幅_dem_2.5m.tif")

    def test_mosaic_can_be_turned_off(self) -> None:
        engine = BatchClipEngine(self.boundary, self.batch_dir, mosaic_dems=False)

        results = engine.execute_batch([self.left, self.right])

        self.assertEqual([r.output_path.name for r in results], ["左幅_dem.tif", "右幅_dem.tif"])
        self.assertTrue(all(r.merged_input_paths is None for r in results))
        np.testing.assert_allclose(read_band(results[0].output_path).max(), 100.0, atol=1e-3)

    def test_single_dem_keeps_original_naming(self) -> None:
        results = BatchClipEngine(self.boundary, self.batch_dir).execute_batch([self.left])
        self.assertEqual([r.output_path.name for r in results], ["左幅_dem.tif"])
        self.assertIsNone(results[0].merged_input_paths)

    def test_two_period_dems_are_not_mosaicked(self) -> None:
        before = write_dem(self.data_dir / "前期.tif", flat(100.0, cols=40))
        after = write_dem(self.data_dir / "後期.tif", flat(105.0, cols=40))
        messages: List[str] = []

        self.assertIn("重疊", find_dem_mosaic_blocker([before, after]) or "")
        results = BatchClipEngine(self.boundary, self.batch_dir).execute_batch(
            [before, after], text_callback=messages.append
        )

        self.assertEqual([r.output_path.name for r in results], ["前期_dem.tif", "後期_dem.tif"])
        np.testing.assert_allclose(read_band(results[0].output_path), 100.0, atol=1e-3)
        np.testing.assert_allclose(read_band(results[1].output_path), 105.0, atol=1e-3)
        self.assertTrue(any("不自動拼接" in message and "重疊" in message for message in messages))

    def test_tiles_with_different_srs_are_not_mosaicked(self) -> None:
        unlabelled_right = write_dem(self.data_dir / "右幅無標籤.tif", flat(200.0), origin_x=250100.0, epsg=None)
        messages: List[str] = []

        self.assertIsNone(find_dem_mosaic_blocker([self.left, self.right]))
        self.assertIn("座標系不同", find_dem_mosaic_blocker([self.left, unlabelled_right]) or "")
        results = BatchClipEngine(self.boundary, self.batch_dir).execute_batch(
            [self.left, unlabelled_right], text_callback=messages.append
        )

        self.assertEqual([r.output_path.name for r in results], ["左幅_dem.tif", "右幅無標籤_dem.tif"])
        self.assertTrue(all(r.is_success for r in results), [r.error_message for r in results])
        self.assertTrue(any("不自動拼接" in message and "座標系不同" in message for message in messages))

    def test_clip_dem_layer_rejects_tiles_with_different_srs(self) -> None:
        zone_119 = write_dem(self.data_dir / "右幅119.tif", flat(200.0), origin_x=250100.0, epsg=3825)
        output = self.out_dir / "不該產生.tif"

        with self.assertRaises(ToolkitError) as caught:
            clip_dem_layer([self.left, zone_119], self.boundary, output)

        self.assertIn("座標系不一致", str(caught.exception))
        self.assertFalse(output.exists())
        self.assertEqual(self._leftover_mosaic_temps(), [])

    def test_unlabelled_tiles_are_mosaicked_with_assigned_srs(self) -> None:
        left = write_dem(self.data_dir / "左幅無標籤.tif", flat(100.0), origin_x=250000.0, epsg=None)
        right = write_dem(self.data_dir / "右幅無標籤.tif", flat(200.0), origin_x=250100.0, epsg=None)
        engine = BatchClipEngine(self.boundary, self.batch_dir, assigned_srs="TWD97_121")

        results = engine.execute_batch([left, right])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_success, results[0].error_message)
        band = read_band(results[0].output_path)
        np.testing.assert_allclose(band[:, :9], 100.0, atol=1e-3)
        np.testing.assert_allclose(band[:, 11:], 200.0, atol=1e-3)


if __name__ == "__main__":
    unittest.main()
