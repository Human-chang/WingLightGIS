"""步驟 9-3：DEM 裁切成果的等高線 DXF 與坡度圖 (core/dem_products.py)。

1. 等高線：一個 DXF、「首曲線」「計曲線」兩圖層、每條線帶高程；計曲線的高程不在首曲線重複；
   檔頭 ANSI_950 + Big5 圖層名 (臺灣 AutoCAD 不亂碼)，GDAL 讀回圖層名正確。
2. 間距檢查：計曲線要是首曲線的 2 倍以上整數倍；平地沒有等高線時拋錯而不是輸出空檔。
3. 坡度：平面坡度的百分比數值正確。
"""

from __future__ import annotations

import math
import unittest
from collections import defaultdict
from typing import Dict, List, Set

import numpy as np

from core import geo_io
from core.dem_products import (
    CONTOUR_LAYER_MAJOR,
    CONTOUR_LAYER_MINOR,
    contour_output_path,
    generate_contour_dxf,
    generate_slope_percent,
    slope_output_path,
    validate_contour_intervals,
)
from core.env_runner import ToolkitError
from tests._support import IsolatedWorkspaceTestCase, write_dem


def sloped_dem_values() -> np.ndarray:
    """40×40 格、每格 5 m 的斜面：往東每格升 0.6 m、往南每格升 0.3 m (高程約 100～135 m)。"""
    rows, cols = np.mgrid[0:40, 0:40]
    return (100.0 + cols * 0.6 + rows * 0.3).astype(float)


def dxf_lines_by_layer(dxf) -> Dict[str, List[float]]:
    """讀回 DXF：每個圖層的每條線的高程 (Z)。"""
    from osgeo import ogr

    geo_io.ensure_gdal_ready()
    dataset = ogr.Open(str(dxf))
    try:
        layer = dataset.GetLayer(0)
        found: Dict[str, List[float]] = defaultdict(list)
        for feature in layer:
            geometry = feature.GetGeometryRef()
            zs = {round(geometry.GetZ(i), 6) for i in range(geometry.GetPointCount())}
            assert len(zs) == 1, f"一條等高線的高程應該都一樣: {zs}"
            found[feature.GetField("Layer")].append(zs.pop())
        return dict(found)
    finally:
        dataset = None


class TestContourDxf(IsolatedWorkspaceTestCase):
    def test_two_layers_with_elevations_and_no_duplicates(self) -> None:
        dem = write_dem(self.data_dir / "地形_dem_1m.tif", sloped_dem_values())
        out = contour_output_path(dem)
        self.assertEqual(out.name, "地形_dem_1m_contours.dxf")
        messages: List[str] = []
        result = generate_contour_dxf(dem, out, interval=1.0, index_interval=5.0, text_callback=messages.append)

        lines = dxf_lines_by_layer(out)
        self.assertEqual(set(lines), {CONTOUR_LAYER_MINOR, CONTOUR_LAYER_MAJOR}, "圖層名讀回要是正確的中文")
        self.assertEqual(len(lines[CONTOUR_LAYER_MAJOR]), result.major_count)
        self.assertEqual(len(lines[CONTOUR_LAYER_MINOR]), result.minor_count)
        majors: Set[float] = set(lines[CONTOUR_LAYER_MAJOR])
        minors: Set[float] = set(lines[CONTOUR_LAYER_MINOR])
        self.assertTrue(all(z % 5 == 0 for z in majors), majors)
        self.assertTrue(all(z % 1 == 0 and z % 5 != 0 for z in minors), minors)
        self.assertFalse(majors & minors, "計曲線的高程不在首曲線重複")
        self.assertLessEqual(set(float(z) for z in range(101, 135)), majors | minors)
        self.assertLessEqual(majors | minors, set(float(z) for z in range(100, 136)))

        raw = out.read_bytes()
        self.assertIn(b"ANSI_950", raw)
        self.assertIn(CONTOUR_LAYER_MAJOR.encode("cp950"), raw)
        self.assertNotIn(CONTOUR_LAYER_MAJOR.encode("utf-8"), raw)
        self.assertTrue(any("首曲線" in m and "條" in m for m in messages))
        self.assertFalse(any("discarded" in m for m in messages), "不把 GDAL 的誤導警告放進日誌")

    def test_half_metre_and_two_and_half(self) -> None:
        dem = write_dem(self.data_dir / "dem.tif", sloped_dem_values())
        out = self.out_dir / "c.dxf"
        generate_contour_dxf(dem, out, interval=0.5, index_interval=2.5)
        lines = dxf_lines_by_layer(out)
        self.assertTrue(all(math.isclose(z / 2.5, round(z / 2.5)) for z in lines[CONTOUR_LAYER_MAJOR]))
        self.assertIn(100.5, lines[CONTOUR_LAYER_MINOR])

    def test_interval_validation(self) -> None:
        self.assertEqual(validate_contour_intervals(1, 5), 5)
        self.assertEqual(validate_contour_intervals(0.5, 2.5), 5)
        for bad in ((1, 2.5), (2, 2), (0, 5), (1, -5), (float("nan"), 5)):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_contour_intervals(*bad)

    def test_flat_dem_has_no_contours(self) -> None:
        dem = write_dem(self.data_dir / "平地.tif", np.full((20, 20), 50.2))
        out = self.out_dir / "平地.dxf"
        with self.assertRaises(ToolkitError) as ctx:
            generate_contour_dxf(dem, out)
        self.assertIn("沒有等高線", str(ctx.exception))
        self.assertFalse(out.exists())


class TestSlope(IsolatedWorkspaceTestCase):
    def test_plane_slope_percent(self) -> None:
        dem = write_dem(self.data_dir / "地形_dem_1m.tif", sloped_dem_values())
        out = generate_slope_percent(dem, slope_output_path(dem))
        self.assertEqual(out.name, "地形_dem_1m_slope_pct.tif")
        from osgeo import gdal

        dataset = gdal.Open(str(out))
        try:
            values = dataset.GetRasterBand(1).ReadAsArray()
            srs_wkt = dataset.GetProjection()
        finally:
            dataset = None
        # 往東 0.6/5、往南 0.3/5 → √(0.12² + 0.06²) = 13.42 %
        self.assertAlmostEqual(float(values[20, 20]), math.hypot(0.12, 0.06) * 100.0, places=2)
        self.assertIn("TWD97", srs_wkt)


if __name__ == "__main__":
    unittest.main()
