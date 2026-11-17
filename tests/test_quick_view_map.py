"""圖資快覽的瀏覽器地圖 (core/quick_view_map.py)。

1. 每個圖資各自一層，DXF 勾選的 CAD 圖層各自一層；點圖徵有完整屬性表 (GeoJsonPopup)，點畫成小圓點。
2. 來源座標系：指定了就以它為準 (有標籤的檔也照它)；沒指定照標籤；沒有標籤也推測不出來的略過並寫原因。
3. DEM 縮圖疊上；底圖選項有空白 (白底)，地圖背景是白色；全部都放不上去時拋 ToolkitError。
4. 圖徵太多的圖層只放外框、不帶屬性。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core.clip_preview import STYLE_DEM, STYLE_VECTOR
from core.env_runner import ToolkitError, run_command
from core.preview_engine import BLANK_BASEMAP_NAME, NLSC_WMTS_EMAP2, NLSC_WMTS_PHOTO2
from core.quick_view_map import MapSource, build_quick_view_map, layer_title
from tests._support import IsolatedWorkspaceTestCase, convert_vector, line, square, strip_prj, write_dem, write_geojson


def _js(text: str) -> str:
    """folium 寫進 HTML 的字串 (非 ASCII 字元寫成 \\uXXXX)。"""
    return json.dumps(text)[1:-1]

PARCELS = [
    ({"地號": "0001", "面積": 2500.0, "備註": None}, square(250000, 2650000, 250050, 2650050)),
    ({"地號": "0002", "面積": 2500.0, "備註": "道路用地"}, square(250050, 2650000, 250100, 2650050)),
]


class TestQuickViewMap(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.html = self.out_dir / "map.html"

    def _shp(self, name: str, *, epsg: int = 3826, label: bool = True) -> Path:
        shp = convert_vector(write_geojson(self.tmp_dir / f"{name}.geojson", PARCELS, epsg=epsg), self.data_dir / f"{name}.shp")
        return shp if label else strip_prj(shp)

    def _dxf(self) -> Path:
        source = self.tmp_dir / "dxf_source.geojson"
        features = [
            {"type": "Feature", "properties": {"Layer": name}, "geometry": line((250000 + i * 10, 2650000), (250100, 2650060))}
            for i, name in enumerate(("中心線", "路權線", "文字"))
        ]
        source.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
        dxf = self.data_dir / "路線.dxf"
        run_command(["ogr2ogr", "-f", "DXF", str(dxf), str(source)])
        dxf.write_bytes(dxf.read_text(encoding="utf-8").replace("ANSI_1252", "ANSI_950").encode("cp950"))
        return dxf

    def test_each_source_and_cad_layer_is_its_own_layer_with_popup(self) -> None:
        shp = self._shp("宗地")
        dxf = self._dxf()
        dem = write_dem(self.data_dir / "地形.tif", np.arange(400, dtype=float).reshape(20, 20))
        result = build_quick_view_map(
            self.html,
            [
                MapSource(shp, STYLE_VECTOR),
                MapSource(dxf, STYLE_VECTOR, cad_layers=("中心線", "路權線")),
                MapSource(dem, STYLE_DEM),
            ],
        )
        self.assertEqual(
            result.layer_names,
            ["宗地.shp", layer_title(dxf, "中心線"), layer_title(dxf, "路權線"), "地形.tif"],
        )
        self.assertEqual(layer_title(dxf, "中心線"), "路線.dxf：中心線")
        # 座標表轉成的暫存點位：圖層名用原本的 .csv 檔名
        titled = build_quick_view_map(self.out_dir / "titled.html", [MapSource(shp, STYLE_VECTOR, title="測量成果.csv")])
        self.assertEqual(titled.layer_names, ["測量成果.csv"])
        self.assertEqual(result.skipped, [])
        html = self.html.read_text(encoding="utf-8")
        for name in result.layer_names:
            self.assertIn(_js(name), html)
        # 點圖徵看完整屬性：每個欄位都在彈窗裡，空值寫成空字串
        self.assertEqual(html.count(".bindPopup("), 3, "三個向量圖層都有屬性彈窗")
        self.assertIn(f'let fields = ["{_js("地號")}", "{_js("面積")}", "{_js("備註")}"]', html)
        self.assertIn(_js("道路用地"), html)
        self.assertIn(f'"{_js("備註")}": ""', html)
        self.assertIn("new L.CircleMarker", html, "點畫成小圓點")
        self.assertNotIn("feature.properties.", html.replace("layer.feature.properties[v]", ""), "樣式用圖徵 id，不用欄位名")
        # 底圖：開地圖時只有航照；空白 (白底) 是選項之一，地圖背景是白色
        self.assertIn(_js(BLANK_BASEMAP_NAME), html)
        # 出圖：圖例每層一列 (面、CAD 的線、DEM)，用的是各層自己的顏色
        self.assertEqual([entry.kind for entry in result.legend], ["polygon", "line", "line", "dem"])
        self.assertEqual([entry.label for entry in result.legend], result.layer_names)
        self.assertEqual(result.legend[0].outline, "#FF2222")
        self.assertIn('<div id="wl-print-bar">', html)
        for entry in result.legend:
            self.assertIn(f"var {entry.layer_var} = ", html)
        shown = {
            url for var, url in re.findall(r'var (tile_layer_\w+) = L\.tileLayer\(\s*"([^"]+)"', html) if f"{var}.addTo(" in html
        }
        self.assertEqual(shown, {NLSC_WMTS_PHOTO2, NLSC_WMTS_EMAP2}, "航照＋透明路網")
        self.assertIn(".folium-map.leaflet-container { background: #FFFFFF; }", html)
        # 圖層範圍在臺灣本島
        for west, south, east, north in result.extents.values():
            self.assertTrue(120 < west <= east < 122 and 23 < south <= north < 25)

    def test_chosen_source_srs_wins_even_over_the_label(self) -> None:
        shp = self._shp("宗地")
        by_label = build_quick_view_map(self.html, [MapSource(shp, STYLE_VECTOR)])
        forced = build_quick_view_map(self.out_dir / "forced.html", [MapSource(shp, STYLE_VECTOR, source_srs="TWD67_121")])
        a = by_label.extents["宗地.shp"]
        b = forced.extents["宗地.shp"]
        # TWD67 與 TWD97 在本島差約 800～900 公尺 (經緯度約 0.008 度)
        shift = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
        self.assertGreater(shift, 0.005)
        self.assertLess(shift, 0.02)

    def test_unlabelled_source_is_guessed_or_skipped_with_reason(self) -> None:
        guessed = self._shp("無標籤", label=False)
        # TM2 範圍外又不像經緯度：推測不出來
        local = convert_vector(
            write_geojson(self.tmp_dir / "local.geojson", [({"n": 1}, square(10, 10, 60, 60))], epsg=3826),
            self.data_dir / "區域座標.shp",
        )
        strip_prj(local)
        result = build_quick_view_map(self.html, [MapSource(guessed, STYLE_VECTOR), MapSource(local, STYLE_VECTOR)])
        self.assertEqual(result.layer_names, ["無標籤.shp"])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("區域座標.shp：沒有座標系標籤也推測不出來", result.skipped[0])

        # 指定了來源座標系就放得上去
        assigned = build_quick_view_map(self.html, [MapSource(local, STYLE_VECTOR, source_srs="TWD97_121")])
        self.assertEqual(assigned.layer_names, ["區域座標.shp"])

    def test_field_name_cut_in_half_a_character_still_shows(self) -> None:
        """「使用分區」UTF-8 是 12 bytes，直接用 ogr2ogr 寫 SHP 會切在 10 bytes (半個「區」字)。"""
        source = write_geojson(self.tmp_dir / "cut.geojson", [({"使用分區": "農業區"}, square(250000, 2650000, 250050, 2650050))])
        shp = self.data_dir / "截斷欄位.shp"
        run_command(["ogr2ogr", "-f", "ESRI Shapefile", "-lco", "ENCODING=UTF-8", str(shp), str(source)], check=True)
        result = build_quick_view_map(self.html, [MapSource(shp, STYLE_VECTOR)])
        self.assertEqual(result.layer_names, ["截斷欄位.shp"])
        self.assertIn(_js("農業區"), self.html.read_text(encoding="utf-8"))

    def test_layers_with_too_many_features_are_outline_only(self) -> None:
        shp = self._shp("宗地")
        with mock.patch("core.quick_view_map.OUTLINE_ONLY_FEATURES", 1):
            result = build_quick_view_map(self.html, [MapSource(shp, STYLE_VECTOR)])
        self.assertEqual(result.outline_only, ["宗地.shp"])
        html = self.html.read_text(encoding="utf-8")
        self.assertNotIn(".bindPopup(", html)
        self.assertNotIn(".bindTooltip(", html)
        self.assertNotIn(_js("道路用地"), html, "屬性不寫進網頁")
        self.assertIn('"fillOpacity": 0.0', html, "只畫外框")

        small = build_quick_view_map(self.out_dir / "small.html", [MapSource(shp, STYLE_VECTOR)])
        self.assertEqual(small.outline_only, [])

    def test_nothing_drawable_raises(self) -> None:
        dem = write_dem(self.data_dir / "無標籤.tif", np.ones((10, 10)), origin_x=10.0, top_y=60.0, epsg=None)
        with self.assertRaises(ToolkitError) as ctx:
            build_quick_view_map(self.html, [MapSource(dem, STYLE_DEM)])
        self.assertIn("無標籤.tif：沒有座標系標籤也推測不出來", str(ctx.exception))
        self.assertFalse(self.html.exists())


if __name__ == "__main__":
    unittest.main()
