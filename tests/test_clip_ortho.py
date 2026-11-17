"""空拍正射影像裁切整合測試 (tests/test_clip_ortho.py)

P1-5：金字塔要寫成外部 .ovr、CAD 底圖 JPEG 不可變成 CMYK 也不可改變顏色、邊界外要透明。
測試資料：200 m 見方的純色 RGB 影像，以三角形邊界裁切 (外接矩形內有一半在邊界外)。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, List, Tuple

import numpy as np

from core import geo_io
from core.clip_master import _cad_jpeg_band_args, clip_ortho_layer
from tests._support import IsolatedWorkspaceTestCase, write_geojson

RGB = (200, 100, 50)


def write_ortho(path: Path, *, data_type: str = "Byte", scale: int = 1, with_alpha: bool = False) -> Path:
    """寫出 40×40、像元 5 m 的純色影像 (EPSG:3826)；scale 用來產生超出 8-bit 範圍的數值。"""
    from osgeo import gdal, osr

    geo_io.ensure_gdal_ready()
    band_count = 4 if with_alpha else 3
    gdal_type = gdal.GDT_Byte if data_type == "Byte" else gdal.GDT_UInt16
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), 40, 40, band_count, gdal_type)
    dataset.SetGeoTransform((250000.0, 5.0, 0.0, 2650200.0, 0.0, -5.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3826)
    dataset.SetProjection(srs.ExportToWkt())
    for index, value in enumerate(RGB, start=1):
        band = dataset.GetRasterBand(index)
        # x < 250150 的部分為 0，讓非 8-bit 來源拉伸後仍有明暗差異可驗證
        pixels = np.full((40, 40), value * scale, dtype="uint16")
        pixels[:, :30] = 0
        band.WriteArray(pixels)
    if with_alpha:
        alpha = dataset.GetRasterBand(4)
        alpha.SetColorInterpretation(gdal.GCI_AlphaBand)
        alpha.WriteArray(np.full((40, 40), 255, dtype="uint16"))
    dataset.FlushCache()
    dataset = None
    return path


def read_raster(path: Path) -> Tuple[Any, List[str]]:
    """讀出全部波段的陣列 (波段, 列, 行) 與各波段的色彩意義名稱。"""
    from osgeo import gdal

    geo_io.ensure_gdal_ready()
    dataset = gdal.Open(str(path))
    try:
        pixels = dataset.ReadAsArray()
        if pixels.ndim == 2:
            pixels = pixels[np.newaxis, :, :]
        names = [
            gdal.GetColorInterpretationName(dataset.GetRasterBand(i).GetColorInterpretation())
            for i in range(1, dataset.RasterCount + 1)
        ]
        return pixels, names
    finally:
        dataset = None


class ClipOrthoTest(IsolatedWorkspaceTestCase):
    """clip_ortho_layer 的成果檔內容。"""

    def setUp(self) -> None:
        super().setUp()
        # 直角三角形：直角在右下，外接矩形 100 m 見方 (x 250100～250200、y 2650000～2650100)，左上半部在邊界外
        triangle = {
            "type": "Polygon",
            "coordinates": [[[250100, 2650000], [250200, 2650000], [250200, 2650100], [250100, 2650000]]],
        }
        self.boundary = write_geojson(self.data_dir / "三角邊界.geojson", [({"id": 1}, triangle)])

    def test_outside_boundary_is_transparent_and_pyramid_is_external(self) -> None:
        source = write_ortho(self.data_dir / "空拍.tif")
        output = self.out_dir / "空拍裁切.tif"
        result = clip_ortho_layer(source, self.boundary, output)

        self.assertEqual(result.raster_dims, (20, 20))
        pixels, names = read_raster(output)
        self.assertEqual(names, ["Red", "Green", "Blue", "Alpha"])
        # 左上角在三角形外 → 透明；右下角在三角形內 → 不透明且顏色不變
        self.assertEqual(int(pixels[3, 1, 1]), 0)
        self.assertEqual(int(pixels[3, 18, 18]), 255)
        self.assertEqual(tuple(int(pixels[band, 18, 18]) for band in range(3)), RGB)

        self.assertEqual(result.pyramid_ovr, output.with_name("空拍裁切.tif.ovr"))
        self.assertTrue(result.pyramid_ovr.is_file())

    def test_cad_background_is_rgb_jpeg_with_original_colours(self) -> None:
        for label, with_alpha in (("RGB", False), ("RGBA", True)):
            with self.subTest(source=label):
                source = write_ortho(self.data_dir / f"空拍_{label}.tif", with_alpha=with_alpha)
                output = self.out_dir / f"裁切_{label}.tif"
                result = clip_ortho_layer(source, self.boundary, output, generate_pyramids=False)

                self.assertIsNone(result.pyramid_ovr)
                self.assertTrue(result.cad_bg_jpg.is_file())
                self.assertEqual(result.cad_bg_jgw, result.cad_bg_jpg.with_suffix(".jgw"))
                self.assertTrue(result.cad_bg_jgw.is_file())
                self.assertFalse(result.cad_bg_jpg.with_suffix(".wld").exists())
                pixels, names = read_raster(result.cad_bg_jpg)
                self.assertEqual(names, ["Red", "Green", "Blue"])
                # JPEG 為有損壓縮，容許少量誤差；沒有被 -scale 拉伸成別的顏色
                for band, expected in enumerate(RGB):
                    self.assertAlmostEqual(int(pixels[band, 18, 18]), expected, delta=8)

                world = [float(line) for line in result.cad_bg_jgw.read_text(encoding="ascii").split()]
                self.assertAlmostEqual(world[0], 5.0, places=6)
                self.assertAlmostEqual(world[4], 250102.5, places=3)
                self.assertAlmostEqual(world[5], 2650097.5, places=3)

    def test_sixteen_bit_source_is_stretched_to_byte_jpeg(self) -> None:
        source = write_ortho(self.data_dir / "空拍16.tif", data_type="UInt16", scale=100)
        output = self.out_dir / "裁切16.tif"
        result = clip_ortho_layer(source, self.boundary, output, generate_pyramids=False)

        pixels, names = read_raster(result.cad_bg_jpg)
        self.assertEqual(names, ["Red", "Green", "Blue"])
        self.assertEqual(str(pixels.dtype), "uint8")
        # 邊界內的亮區拉伸到接近 255，暗區 (列 18、行 5) 接近 0
        self.assertGreater(int(pixels[0, 18, 18]), 200)
        self.assertLess(int(pixels[0, 18, 5]), 50)

    def test_coarser_resolution_applies_to_tif_and_cad_jpeg(self) -> None:
        # 9-4：像元 5 m → 10 m，位置不變，CAD 底圖跟著同一個解析度
        source = write_ortho(self.data_dir / "空拍.tif")
        messages: List[str] = []
        result = clip_ortho_layer(
            source, self.boundary, self.out_dir / "粗.tif", generate_pyramids=False, target_resolution=10.0,
            text_callback=messages.append,
        )
        self.assertEqual(result.raster_dims, (10, 10))
        pixels, _ = read_raster(result.output_path)
        self.assertEqual(tuple(int(pixels[band, 9, 9]) for band in range(3)), RGB)

        world = [float(line) for line in result.cad_bg_jgw.read_text(encoding="ascii").split()]
        self.assertAlmostEqual(world[0], 10.0, places=6)
        self.assertAlmostEqual(world[3], -10.0, places=6)
        # 左上角像元中心：外接矩形左上角 (250100, 2650100) 往內半格
        self.assertAlmostEqual(world[4], 250105.0, places=3)
        self.assertAlmostEqual(world[5], 2650095.0, places=3)
        self.assertFalse(any("還細" in m for m in messages))

    def test_finer_resolution_only_warns(self) -> None:
        source = write_ortho(self.data_dir / "空拍.tif")
        messages: List[str] = []
        result = clip_ortho_layer(
            source, self.boundary, self.out_dir / "細.tif", generate_pyramids=False, export_cad_background=False,
            target_resolution=2.5, text_callback=messages.append,
        )
        self.assertEqual(result.raster_dims, (40, 40))
        self.assertTrue(any("比 空拍.tif 原圖的 5 m 還細" in m for m in messages), messages)

    def test_cad_background_can_be_skipped(self) -> None:
        source = write_ortho(self.data_dir / "空拍.tif")
        output = self.out_dir / "只裁切.tif"
        result = clip_ortho_layer(source, self.boundary, output, generate_pyramids=False, export_cad_background=False)
        self.assertIsNone(result.cad_bg_jpg)
        self.assertEqual(sorted(item.name for item in self.out_dir.iterdir()), ["只裁切.tif"])


class CadJpegBandArgsTest(unittest.TestCase):
    """依波段組成決定 gdal_translate 參數 (純函式)。"""

    @staticmethod
    def _info(types: List[str], interpretations: List[str], size: Tuple[int, int] = (100, 100)) -> dict:
        bands = [
            {"band": index, "type": band_type, "colorInterpretation": interpretation}
            for index, (band_type, interpretation) in enumerate(zip(types, interpretations), start=1)
        ]
        return {"size": list(size), "bands": bands}

    def test_alpha_band_is_dropped_and_byte_is_not_scaled(self) -> None:
        info = self._info(["Byte"] * 4, ["Red", "Green", "Blue", "Alpha"])
        self.assertEqual(_cad_jpeg_band_args(info), ["-b", "1", "-b", "2", "-b", "3"])

    def test_grey_with_alpha_and_non_byte(self) -> None:
        self.assertEqual(_cad_jpeg_band_args(self._info(["Byte", "Byte"], ["Gray", "Alpha"])), ["-b", "1"])
        self.assertEqual(
            _cad_jpeg_band_args(self._info(["UInt16"] * 3, ["Red", "Green", "Blue"])),
            ["-b", "1", "-b", "2", "-b", "3", "-ot", "Byte", "-scale"],
        )

    def test_palette_image_is_expanded(self) -> None:
        info = self._info(["Byte"], ["Palette"])
        info["bands"][0]["colorTable"] = {"palette": "RGB", "count": 2, "entries": [[0, 0, 0, 255], [255, 0, 0, 255]]}
        self.assertEqual(_cad_jpeg_band_args(info), ["-b", "1", "-expand", "rgb"])

    def test_oversized_image_is_shrunk_below_jpeg_limit(self) -> None:
        messages: List[str] = []
        info = self._info(["Byte"] * 3, ["Red", "Green", "Blue"], size=(131070, 50000))
        args = _cad_jpeg_band_args(info, messages.append)
        self.assertEqual(args[-5:], ["-outsize", "65535", "25000", "-r", "average"])
        self.assertTrue(any("上限" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
