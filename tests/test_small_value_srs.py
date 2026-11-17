"""小數值區域座標 整合測試 (tests/test_small_value_srs.py)

沒有座標系標籤、數值落在 ±180／±90 內但不在臺灣周邊的檔案 (自訂原點的 CAD 區域座標)
不可被猜成 EPSG:4326：實際呼叫 DXF 轉檔與裁切，確認會要求指派而不是產出位置錯誤的成果。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List

from core import geo_io
from core.boundary_builder import dxf_lines_to_polygon_boundary, import_boundary_file
from core.clip_master import clip_vector_layer
from core.crs_doctor import SourceSrsUndeterminedError, decide_source_srs, sniff_vector_crs
from tests._support import IsolatedWorkspaceTestCase, convert_vector, line, square, strip_prj, total_area, write_geojson


class SmallValueLocalCoordinatesTest(IsolatedWorkspaceTestCase):
    def _local_dxf(self) -> Path:
        """以假設座標 (原點在圖面左下角、單位公尺) 繪製的 DXF：80 m × 40 m 的封閉線。"""
        outline = line((10.0, 5.0), (90.0, 5.0), (90.0, 45.0), (10.0, 45.0), (10.0, 5.0))
        source = write_geojson(self.data_dir / "假設座標.geojson", [({"NAME": "基地"}, outline)])
        return convert_vector(source, self.data_dir / "假設座標.dxf")

    def _unlabelled_shapefile(self, name: str, x0: float, y0: float, x1: float, y1: float) -> Path:
        source = write_geojson(self.data_dir / f"{name}.geojson", [({"NAME": name}, square(x0, y0, x1, y1))])
        return strip_prj(convert_vector(source, self.data_dir / f"{name}.shp"))

    def test_local_dxf_is_not_guessed_as_lonlat(self) -> None:
        dxf = self._local_dxf()

        with self.assertRaises(SourceSrsUndeterminedError) as caught:
            decide_source_srs(dxf)

        message = str(caught.exception)
        self.assertIn("不在臺灣周邊", message)
        self.assertIn("TWD97_121", message)
        diagnosis = sniff_vector_crs(dxf)
        self.assertEqual((diagnosis.status_level, diagnosis.status_code), ("RED", "LONLAT_UNLABELED_OUTSIDE_TAIWAN"))

    def test_local_dxf_export_requires_assignment(self) -> None:
        dxf = self._local_dxf()
        output = self.out_dir / "基地.gpkg"

        with self.assertRaises(SourceSrsUndeterminedError):
            dxf_lines_to_polygon_boundary(dxf, output)
        self.assertFalse(output.exists())

        dxf_lines_to_polygon_boundary(dxf, output, assigned_srs="TWD97_121")
        # 指派後座標原封不動 (舊程式會當成經緯度重投影，80 m × 40 m 變成數千萬公頃)
        info = geo_io.read_layer_info(output)
        self.assertEqual(info.epsg, 3826)
        self.assertAlmostEqual(total_area(output), 3200.0, delta=0.5)

    def test_local_shapefile_clip_requires_assignment(self) -> None:
        local = self._unlabelled_shapefile("區域座標", 10.0, 5.0, 90.0, 45.0)
        boundary = write_geojson(
            self.data_dir / "邊界.geojson", [({"NAME": "工區"}, square(250000.0, 2650000.0, 250100.0, 2650100.0))]
        )
        output = self.out_dir / "裁切.gpkg"

        with self.assertRaises(SourceSrsUndeterminedError):
            clip_vector_layer(local, boundary, output)
        self.assertFalse(output.exists())

    def test_unlabelled_lonlat_near_taiwan_is_still_guessed(self) -> None:
        lonlat = self._unlabelled_shapefile("經緯度", 121.0, 24.0, 121.001, 24.001)
        messages: List[str] = []

        decision = decide_source_srs(lonlat)

        self.assertEqual(decision.s_srs, "EPSG:4326")
        self.assertTrue(decision.is_guess)
        output = self.out_dir / "經緯度轉出.gpkg"
        import_boundary_file(lonlat, output, text_callback=messages.append)
        self.assertTrue(any("座標系為推測值" in message for message in messages))
        # 0.001 度見方在北緯 24 度約 101.7 m × 110.8 m
        self.assertAlmostEqual(total_area(output), 11270.0, delta=200.0)


if __name__ == "__main__":
    unittest.main()
