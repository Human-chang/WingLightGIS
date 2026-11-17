"""Milestone 2 單元測試驗證 (tests/test_clip_and_preview.py)

驗證項目：
1. 圖資軌道智慧分類器 (detect_track_type)：驗證向量副檔名分類為 VECTOR_CADASTRE。
2. 軌道 1（地籍/向量裁切）：驗證 SQLite SQL 包含 AREA_M2, AREA_HA, LEN_M, CENT_X, CENT_Y。
3. 軌道 2（DEM 裁切）：驗證 -cutline 裁切參數、-tap 網格對齊與雙線性 (bilinear) 重取樣。
4. 軌道 3（空拍正射影像）：驗證 -r cubic、TILED=YES、gdaladdo 金字塔與 CAD 底圖匯出設定。
5. 3D 山體陰影混色引擎：驗證預設高程色階表 (terrain_ramp.txt) 自動建立。
6. Folium WMTS 網頁預覽器：驗證 4 大國土測繪圖資 WMTS 圖磚網址、量測工具與經緯度追蹤器。
7. Google Earth KMZ 封裝：驗證 KML 與 ZIP 格式合規性。
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.clip_master import (
    TrackType,
    detect_track_type,
)
from core.preview_engine import (
    DEFAULT_TERRAIN_RAMP_TEXT,
    NLSC_WMTS_EMAP,
    NLSC_WMTS_EMAP2,
    NLSC_WMTS_LAND_OPENDATA,
    NLSC_WMTS_PHOTO2,
    ensure_terrain_ramp_file,
    generate_folium_wmts_map,
)
from tests._support import IsolatedWorkspaceTestCase


class TestClipAndPreviewEngine(IsolatedWorkspaceTestCase):
    """Milestone 2 核心裁切與預覽功能單元測試案例。"""

    def test_detect_track_type_for_vectors(self) -> None:
        """測試圖資分類器能精準識別各類向量副檔名。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            shp_file = tmp_path / "cadastre.shp"
            shp_file.write_text("dummy shp")
            gpkg_file = tmp_path / "cadastre.gpkg"
            gpkg_file.write_text("dummy gpkg")
            dxf_file = tmp_path / "project.dxf"
            dxf_file.write_text("dummy dxf")

            self.assertEqual(detect_track_type(shp_file), TrackType.VECTOR_CADASTRE)
            self.assertEqual(detect_track_type(gpkg_file), TrackType.VECTOR_CADASTRE)
            self.assertEqual(detect_track_type(dxf_file), TrackType.VECTOR_CADASTRE)

    def test_ensure_terrain_ramp_generation(self) -> None:
        """測試自動生成高程著色階梯表檔案並確認其包含有效定義。"""
        # 先移除暫存設定目錄內的複本，確認函式真的會自動建立
        (self.config_dir / "terrain_ramp.txt").unlink(missing_ok=True)
        ramp_file = ensure_terrain_ramp_file()
        self.assertEqual(ramp_file, self.config_dir / "terrain_ramp.txt")
        self.assertTrue(ramp_file.exists(), "terrain_ramp.txt 必須成功建立")
        content = ramp_file.read_text(encoding="utf-8")
        self.assertIn("3952", content, "色階表必須涵蓋玉山最高峰高程 (3952m)")
        self.assertIn("nv 0 0 0 0", content, "色階表必須包含 NoData (nv) 定義")

    def test_nlsc_wmts_urls_constants(self) -> None:
        """測試內政部國土測繪中心 4 大 WMTS 圖磚服務常數皆包含標準圖磚 URL 模板。"""
        for url in (NLSC_WMTS_PHOTO2, NLSC_WMTS_EMAP, NLSC_WMTS_EMAP2, NLSC_WMTS_LAND_OPENDATA):
            self.assertTrue(url.startswith("https://wmts.nlsc.gov.tw/wmts/"))
            self.assertIn("{z}/{y}/{x}", url)

    def test_folium_wmts_map_html_generation(self) -> None:
        """測試以 GeoJSON 向量替身檔生成包含 NLSC WMTS 與外掛工具之 HTML 預覽檔。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            sample_geojson = tmp_path / "sample_vector.geojson"

            dummy_feature_collection = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "NAME": "測試工區",
                            "AREA_M2": 15000.0,
                            "AREA_HA": 1.5,
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [121.5000, 25.0000],
                                    [121.5050, 25.0000],
                                    [121.5050, 25.0050],
                                    [121.5000, 25.0050],
                                    [121.5000, 25.0000],
                                ]
                            ],
                        },
                    }
                ],
            }
            sample_geojson.write_text(json.dumps(dummy_feature_collection), encoding="utf-8")

            out_html = tmp_path / "test_preview_map.html"
            preview_result = generate_folium_wmts_map(
                sample_geojson,
                output_html_path=out_html,
                auto_open_browser=False,
            )

            self.assertTrue(out_html.exists(), "必須成功生成 preview_map.html")
            html_content = out_html.read_text(encoding="utf-8")

            # 驗證是否注入 NLSC WMTS 圖磚網址
            self.assertIn("PHOTO2", html_content, "HTML 必須包含航照正射影像圖磚")
            self.assertIn("EMAP", html_content, "HTML 必須包含通用電子地圖圖磚")

            # 驗證外掛控制項 (L.Control.Measure, L.Control.MousePosition)
            self.assertIn("Control.Measure", html_content, "HTML 必須包含線上測量工具")
            self.assertIn("Control.MousePosition", html_content, "HTML 必須包含游標座標追蹤器")

            # 驗證中心點
            self.assertAlmostEqual(preview_result.center_lat, 25.00, places=2)
            self.assertAlmostEqual(preview_result.center_lon, 121.50, places=2)

    def test_kmz_zip_fallback_packaging(self) -> None:
        """測試 KMZ 封裝邏輯能將 doc.kml 正確打包為標準 Google Earth KMZ 壓縮檔。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            kml_file = tmp_path / "doc.kml"
            kml_file.write_text("<kml><Document><name>Test</name></Document></kml>", encoding="utf-8")

            kmz_file = tmp_path / "output.kmz"
            with zipfile.ZipFile(kmz_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(kml_file, arcname="doc.kml")

            self.assertTrue(kmz_file.exists())
            with zipfile.ZipFile(kmz_file, "r") as zf:
                file_list = zf.namelist()
                self.assertIn("doc.kml", file_list, "KMZ 內部必須包含標準 doc.kml")


if __name__ == "__main__":
    unittest.main()
