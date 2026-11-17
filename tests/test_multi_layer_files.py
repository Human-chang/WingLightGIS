"""一個檔裡有好幾個圖層 (格式支援 K1)：KML／KMZ 的資料夾、GPKG 的圖層、GPX 的航點／路線／軌跡。

原本沒指定圖層時只讀第一個圖層 (Google Earth 的 KML 只讀到根層，資料夾裡的地標全部漏掉、沒有提示)。
現在：list_data_layers 列出有圖徵的圖層；沒指定時取第一個有圖徵的；座標系範圍合起來算；
圖資快覽的地圖與另存 SHP 每個圖層各自一層；KML／GPX 沒用的欄位不帶。全部用程式產生的假資料。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from core import geo_io
from core.clip_master import TrackType, detect_track_type
from core.clip_preview import STYLE_VECTOR, render_clip_thumbnail
from core.crs_doctor import sniff_crs
from core.quick_view_export import ExportSource, export_to_shapefiles
from core.quick_view_map import MapSource, build_quick_view_map
from tests._support import (
    SAMPLE_KML_LAYERS,
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    square,
    write_geojson,
    write_gpx,
    write_kml,
)

# 只有資料夾、根層沒有地標的 KML
FOLDER_ONLY_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>只有資料夾</name>
<Folder><name>樁位</name>
 <Placemark><name>K0+000</name><Point><coordinates>121.50,25.00,0</coordinates></Point></Placemark>
</Folder>
</Document></kml>
"""


TRACK_ONLY_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
<trk><name>軌跡</name><trkseg><trkpt lat="25.00" lon="121.50"></trkpt><trkpt lat="25.01" lon="121.51"></trkpt></trkseg></trk>
</gpx>
"""


class TestDataLayers(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.kml = write_kml(self.data_dir / "範圍.kml")
        self.kmz = write_kml(self.data_dir / "範圍.kmz")
        self.gpx = write_gpx(self.data_dir / "巡查.gpx")

    def _two_layer_gpkg(self) -> Path:
        """GPKG：空的「預留」在最前面，再來「道路」2 個面、「排水」1 個面 (TWD97)。"""
        gpkg = self.data_dir / "工區.gpkg"
        roads = write_geojson(self.tmp_dir / "roads.geojson", [({"名稱": "甲"}, square(0, 0, 10, 10)), ({"名稱": "乙"}, square(10, 0, 20, 10))])
        roads = convert_vector(roads, self.tmp_dir / "roads.gpkg")
        drains = write_geojson(self.tmp_dir / "drains.geojson", [({"名稱": "溝"}, square(30, 30, 40, 40))])
        drains = convert_vector(drains, self.tmp_dir / "drains.gpkg")
        geo_io.export_vector(roads, None, gpkg, output_layer_name="預留", where="1 = 0")
        geo_io.export_vector(roads, None, gpkg, output_layer_name="道路")
        geo_io.export_vector(drains, None, gpkg, output_layer_name="排水")
        return gpkg

    def test_kml_and_kmz_list_every_folder_with_placemarks(self) -> None:
        for path in (self.kml, self.kmz):
            with self.subTest(path.name):
                self.assertEqual(detect_track_type(path), TrackType.VECTOR_CADASTRE)
                self.assertEqual(tuple(geo_io.list_data_layers(path)), SAMPLE_KML_LAYERS, "空資料夾不列")

    def test_gpx_lists_waypoints_and_tracks_but_not_duplicate_points(self) -> None:
        self.assertEqual(detect_track_type(self.gpx), TrackType.VECTOR_CADASTRE)
        self.assertEqual(geo_io.list_data_layers(self.gpx), ["waypoints", "tracks"])
        self.assertIn("track_points", geo_io.read_layer_info(self.gpx).layer_names)

    def test_default_layer_is_the_first_with_features(self) -> None:
        kml = write_kml(self.data_dir / "只有資料夾.kml", FOLDER_ONLY_KML)
        info = geo_io.read_layer_info(kml)
        self.assertEqual((info.layer_name, info.feature_count), ("樁位", 1))
        gpkg = self._two_layer_gpkg()
        self.assertEqual(geo_io.list_data_layers(gpkg), ["道路", "排水"])
        self.assertEqual(geo_io.read_layer_info(gpkg).layer_name, "道路")
        # GPX 的第一個圖層 (航點) 沒有時取軌跡
        tracks_only = write_gpx(self.data_dir / "只有軌跡.gpx", TRACK_ONLY_GPX)
        self.assertEqual(geo_io.read_layer_info(tracks_only).layer_name, "tracks")

    def test_extent_covers_every_layer(self) -> None:
        diagnosis = sniff_crs(self.kmz)
        self.assertEqual(diagnosis.detected_epsg, 4326)
        extent = diagnosis.extent
        # 根層只有 (121.50, 25.03)；會勘點到 25.05、工區範圍到 121.53／25.00
        self.assertAlmostEqual(extent.min_x, 121.50)
        self.assertAlmostEqual(extent.max_x, 121.53)
        self.assertAlmostEqual(extent.min_y, 25.00)
        self.assertAlmostEqual(extent.max_y, 25.05)

    def test_kml_display_fields_and_empty_fields_are_dropped(self) -> None:
        names = [field.name for field in geo_io.read_layer_info(self.kml, "會勘點").fields]
        self.assertIn("tessellate", names)
        staged = geo_io.stage_vector_to_gpkg(self.kml, self.tmp_dir / "stage.gpkg", source_layer="會勘點")
        self.assertEqual([field.name for field in staged.fields], ["Name", "description"], "說明有值的資料夾留下 description")
        staged = geo_io.stage_vector_to_gpkg(self.kml, self.tmp_dir / "stage2.gpkg", source_layer="工區範圍")
        self.assertEqual([field.name for field in staged.fields], ["Name"])
        staged = geo_io.stage_vector_to_gpkg(self.gpx, self.tmp_dir / "stage3.gpkg", source_layer="waypoints")
        self.assertEqual([field.name for field in staged.fields], ["name"])
        self.assertEqual(staged.feature_count, 2)
        # 其他格式不動
        self.assertEqual(geo_io.droppable_format_fields(geo_io.read_layer_info(self._two_layer_gpkg(), "道路")), [])

    def test_map_puts_each_folder_on_its_own_layer(self) -> None:
        layers = tuple(geo_io.list_data_layers(self.kmz))
        result = build_quick_view_map(self.tmp_dir / "map.html", [MapSource(self.kmz, STYLE_VECTOR, data_layers=layers)])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.layer_names, [f"範圍.kmz：{name}" for name in SAMPLE_KML_LAYERS])
        html = result.html_path.read_text(encoding="utf-8")
        for placemark in ("根層點", "點A", "點B", "範圍1", "線1"):
            self.assertTrue(placemark in html or json.dumps(placemark)[1:-1] in html, placemark)
        self.assertNotIn("tessellate", html)

    def test_export_saves_each_folder_and_gpx_layer(self) -> None:
        out = self.tmp_dir / "輸出"
        result = export_to_shapefiles(
            [
                ExportSource(self.kml, data_layers=SAMPLE_KML_LAYERS),
                ExportSource(self.gpx, data_layers=("waypoints", "tracks")),
            ],
            out,
        )
        self.assertEqual(result.skipped, [])
        counts = {path.name: geo_io.read_layer_info(path).feature_count for path in result.output_files}
        self.assertEqual(
            counts,
            {"範圍_測試範圍.shp": 1, "範圍_會勘點.shp": 2, "範圍_工區範圍.shp": 1, "範圍_中心線.shp": 1, "巡查_waypoints.shp": 2, "巡查_tracks.shp": 1},
        )
        self.assertEqual(attribute_rows(out / "範圍_會勘點.shp"), [{"Name": "點A", "descriptio": "擋土牆"}, {"Name": "點B", "descriptio": None}])
        self.assertIn("Line String", geo_io.read_layer_info(out / "巡查_tracks.shp").geometry_type, "軌跡存成線")
        # 經緯度轉 TWD97：x 約 25 萬、y 約 276 萬
        extent = geo_io.read_layer_info(out / "範圍_工區範圍.shp").extent
        self.assertTrue(240_000 < extent[0] < 320_000 and 2_700_000 < extent[1] < 2_800_000, extent)

    def test_thumbnail_draws_every_layer(self) -> None:
        from PIL import Image

        one = render_clip_thumbnail(self.tmp_dir / "one.png", style=STYLE_VECTOR, source=self.kml)
        every = render_clip_thumbnail(self.tmp_dir / "all.png", style=STYLE_VECTOR, source=self.kml, layers=SAMPLE_KML_LAYERS)
        background = np.array(Image.open(one))[0, 0]
        drawn_one = int((np.array(Image.open(one)) != background).any(axis=2).sum())
        drawn_all = int((np.array(Image.open(every)) != background).any(axis=2).sum())
        self.assertGreater(drawn_all, drawn_one * 5, "全部圖層畫出來的面積要比只畫根層的一個點大得多")


if __name__ == "__main__":
    unittest.main()
