"""圖資快覽的另存 SHP (core/quick_view_export.py)。

1. 轉到選的輸出座標系與文字編碼 (UTF-8 附 .cpg／CP950)；欄位名超過 10 bytes 在字元邊界截斷。
2. 來源座標系跟瀏覽器地圖同一套：選了就以它為準 (有標籤也照它)；沒選照標籤；沒有標籤推測；推測不出來擋下並說原因。
3. DXF 只存勾選的 CAD 圖層，依點、線、面分開存成 <檔名>_<CAD圖層>_point／_line／_poly.shp，只留 Layer、Text、2D；
   「封閉線轉面」把首尾相接的線 (含 CAD 設成封閉的聚合線) 存成面。其他格式點線面混雜時也分開存。
4. 檔名：同一次存出的重複時加 _2，來源 SHP 不會被蓋掉；輸出不開放經緯度；全部都做不出來時拋 ToolkitError。
5. 另存 KML／KMZ：WGS84、每個來源檔一個，檔裡的圖層各一個資料夾 (名稱照圖層名)，點線面不拆；
   「名稱」或「Name」當標記名、其他屬性進 ExtendedData；每個資料夾一種顏色；KMZ 是同一份 KML 壓成 doc.kml。
"""

from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from core import geo_io
from core.env_runner import ToolkitError, run_command, run_json_command
from core.quick_view_export import (
    KMZ_DOC_NAME,
    NO_SRS_REASON,
    ExportSource,
    export_to_kml,
    export_to_shapefiles,
    file_label,
    kml_name_field,
)
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    line,
    square,
    strip_prj,
    total_area,
    write_geojson,
    write_kml,
)

KML = "{http://www.opengis.net/kml/2.2}"

PARCELS = [
    ({"地號": "0001", "道路用地面積": 2500.0, "備註": "道路用地"}, square(250000, 2650000, 250050, 2650050)),
    ({"地號": "0002", "道路用地面積": 1200.5, "備註": None}, square(250050, 2650000, 250100, 2650050)),
]
X0, Y0 = 250000.0, 2650000.0


def _point(x: float, y: float, z: float = 0.0) -> Dict[str, Any]:
    return {"type": "Point", "coordinates": [x, y, z]}


def _ring_line(x0: float, y0: float, x1: float, y1: float) -> Dict[str, Any]:
    """首尾相接 (沒有封閉旗標) 的矩形線。"""
    return line((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))


class TestQuickViewExport(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.messages: List[str] = []

    def _log(self) -> str:
        return "".join(self.messages)

    def _shp(self, name: str, *, epsg: int = 3826, label: bool = True, folder: Path = None) -> Path:  # type: ignore[assignment]
        folder = folder or self.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        shp = convert_vector(write_geojson(self.tmp_dir / f"{name}_{epsg}.geojson", PARCELS, epsg=epsg), folder / f"{name}.shp")
        return shp if label else strip_prj(shp)

    def _dxf(self, name: str, features: List[Tuple[str, Dict[str, Any]]]) -> Path:
        """(CAD 圖層, 幾何) 寫成 DXF (Big5)；面寫成 HATCH，線寫成沒有封閉旗標的 LWPOLYLINE。"""
        source = self.tmp_dir / f"{name}.geojson"
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"Layer": layer}, "geometry": geom} for layer, geom in features],
        }
        source.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        dxf = self.data_dir / f"{name}.dxf"
        run_command(["ogr2ogr", "-f", "DXF", str(dxf), str(source)])
        dxf.write_bytes(dxf.read_text(encoding="utf-8").replace("ANSI_1252", "ANSI_950").encode("cp950"))
        return dxf

    def _road_dxf(self) -> Path:
        return self._dxf(
            "路線",
            [
                ("樁位", _point(X0 + 10, Y0 + 10, 12.5)),
                ("樁位", _point(X0 + 20, Y0 + 20, 13.5)),
                ("中心線", line((X0, Y0), (X0 + 100, Y0 + 50))),
                ("用地範圍", _ring_line(X0 + 100, Y0, X0 + 130, Y0 + 20)),
                ("建物", square(X0 + 30, Y0 + 30, X0 + 60, Y0 + 50)),
                ("混合", _point(X0 + 5, Y0 + 5)),
                ("混合", line((X0, Y0 + 5), (X0 + 50, Y0 + 5))),
            ],
        )

    def _extent(self, path: Path) -> Tuple[float, float, float, float]:
        extent = geo_io.read_layer_info(path).extent
        assert extent is not None
        return extent

    def _geometry_types(self, path: Path) -> List[str]:
        info = run_json_command(["ogrinfo", "-json", "-features", str(path)])
        return [feature["geometry"]["type"] for feature in info["layers"][0]["features"]]

    def _names(self, result: Any) -> List[str]:
        return [path.name for path in result.output_files]

    # -------------------------------------------------------------------------
    # 1. 座標系與編碼
    # -------------------------------------------------------------------------
    def test_shp_to_twd67_utf8_keeps_attributes_and_truncates_long_field_names(self) -> None:
        shp = self._shp("宗地")
        result = export_to_shapefiles([ExportSource(shp)], self.out_dir, target_srs="TWD67_121", text_callback=self.messages.append)

        self.assertEqual(self._names(result), ["宗地.shp"])
        self.assertEqual(result.skipped, [])
        # 座標表轉成的暫存點位：SHP 用原本的 .csv 主檔名
        titled = export_to_shapefiles([ExportSource(shp, title="測量成果.csv")], self.out_dir / "titled")
        self.assertEqual(self._names(titled), ["測量成果.shp"])
        out = self.out_dir / "宗地.shp"
        for suffix in (".dbf", ".shx", ".prj", ".cpg"):
            self.assertTrue(out.with_suffix(suffix).is_file(), suffix)
        self.assertEqual(out.with_suffix(".cpg").read_text(encoding="ascii").strip().upper(), "UTF-8")
        rows = attribute_rows(out)
        self.assertEqual([row["地號"] for row in rows], ["0001", "0002"])
        self.assertEqual(rows[0]["備註"], "道路用地")
        # TWD97 → TWD67 在本島約差 X -828 m、Y +207 m；面積不變
        x_shift = self._extent(out)[0] - self._extent(shp)[0]
        y_shift = self._extent(out)[1] - self._extent(shp)[1]
        self.assertTrue(-900 < x_shift < -750 and 150 < y_shift < 260, (x_shift, y_shift))
        self.assertAlmostEqual(total_area(out), 5000.0, delta=0.5)
        self.assertIn("完成：1 個 SHP", self._log())
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_viewshp_")], [], "暫存檔要清掉")

    def test_long_field_names_are_cut_on_character_boundaries(self) -> None:
        # GeoJSON 的欄位名沒有長度限制；SHP 上限 10 bytes
        source = write_geojson(self.data_dir / "宗地.geojson", PARCELS)
        utf8 = export_to_shapefiles([ExportSource(source)], self.out_dir / "utf8", text_callback=self.messages.append)
        # UTF-8 的中文 3 bytes：「道路用地面積」截成「道路用」
        self.assertEqual(list(attribute_rows(utf8.output_files[0])[0]), ["地號", "道路用", "備註"])
        self.assertIn("欄位名上限 10 bytes", self._log())

        big5 = export_to_shapefiles([ExportSource(source)], self.out_dir / "big5", encoding="CP950")
        out = big5.output_files[0]
        self.assertEqual(out.with_suffix(".cpg").read_text(encoding="ascii").strip().upper(), "CP950")
        dbf = out.with_suffix(".dbf").read_bytes()
        self.assertIn("道路用地面".encode("cp950"), dbf, "CP950 的中文 2 bytes：可以留 5 個字")
        self.assertIn("道路用地".encode("cp950"), dbf)
        rows = attribute_rows(out)
        self.assertEqual(list(rows[0]), ["地號", "道路用地面", "備註"])
        self.assertEqual(rows[0]["備註"], "道路用地")
        self.assertEqual(geo_io.read_layer_info(out).epsg, 3826)

    def test_chosen_source_srs_wins_even_over_the_label(self) -> None:
        shp = self._shp("宗地")
        result = export_to_shapefiles([ExportSource(shp, source_srs="TWD67_121")], self.out_dir, target_srs="TWD97_121")
        out = result.output_files[0]
        self.assertEqual(geo_io.read_layer_info(out).epsg, 3826)
        # 當成 TWD67 轉回 TWD97：往反方向移
        x_shift = self._extent(out)[0] - self._extent(shp)[0]
        self.assertTrue(750 < x_shift < 900, x_shift)

    def test_unlabelled_source_is_guessed_or_blocked_with_reason(self) -> None:
        guessed = self._shp("無標籤", label=False)
        local = strip_prj(
            convert_vector(
                write_geojson(self.tmp_dir / "local.geojson", [({"n": 1}, square(10, 10, 60, 60))]), self.data_dir / "區域座標.shp"
            )
        )
        result = export_to_shapefiles(
            [ExportSource(guessed), ExportSource(local)], self.out_dir, text_callback=self.messages.append
        )
        self.assertEqual(self._names(result), ["無標籤.shp"])
        self.assertEqual(geo_io.read_layer_info(result.output_files[0]).epsg, 3826)
        self.assertEqual(result.skipped, [f"區域座標.shp：{NO_SRS_REASON}"])
        self.assertIn("座標系為推測值", self._log())
        self.assertFalse((self.out_dir / "區域座標.shp").exists())

        # 選了來源座標系就能存
        chosen = export_to_shapefiles([ExportSource(local, source_srs="TWD97_121")], self.out_dir / "選了")
        self.assertEqual(self._names(chosen), ["區域座標.shp"])
        with self.assertRaises(ToolkitError) as ctx:
            export_to_shapefiles([ExportSource(local)], self.out_dir / "都不行")
        self.assertIn(NO_SRS_REASON, str(ctx.exception))
        self.assertFalse((self.out_dir / "都不行").exists() and any((self.out_dir / "都不行").iterdir()))

    def test_geographic_output_is_rejected(self) -> None:
        with self.assertRaises(ToolkitError) as ctx:
            export_to_shapefiles([ExportSource(self._shp("宗地"))], self.out_dir, target_srs="WGS84")
        self.assertIn("不開放經緯度", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 2. DXF：勾選的 CAD 圖層、依點線面分開、封閉線轉面
    # -------------------------------------------------------------------------
    def test_dxf_layers_are_split_by_geometry_type(self) -> None:
        dxf = self._road_dxf()
        result = export_to_shapefiles(
            [ExportSource(dxf, cad_layers=("樁位", "中心線", "用地範圍", "建物", "混合"))],
            self.out_dir,
            text_callback=self.messages.append,
        )
        self.assertEqual(
            self._names(result),
            [
                "路線_樁位_point.shp", "路線_中心線_line.shp", "路線_用地範圍_line.shp", "路線_建物_poly.shp",
                "路線_混合_point.shp", "路線_混合_line.shp",
            ],
        )
        self.assertEqual(result.skipped, [])
        for out in result.output_files:
            info = geo_io.read_layer_info(out)
            self.assertEqual(info.field_names, ["Layer", "Text"], out.name)
            self.assertEqual(info.epsg, 3826, out.name)
        self.assertEqual({row["Layer"] for row in attribute_rows(self.out_dir / "路線_樁位_point.shp")}, {"樁位"})
        self.assertEqual(self._geometry_types(self.out_dir / "路線_樁位_point.shp"), ["Point", "Point"], "2D 的點")
        self.assertAlmostEqual(total_area(self.out_dir / "路線_建物_poly.shp"), 600.0, delta=0.01)
        self.assertIn("「路線.dxf：混合」依點、線、面分開存：點 1 個、線 1 個", self._log())
        self.assertIn("推測", self._log(), "DXF 沒有標籤，依範圍推測")

    def test_unchecked_cad_layers_are_not_saved(self) -> None:
        dxf = self._road_dxf()
        result = export_to_shapefiles([ExportSource(dxf, cad_layers=("中心線",))], self.out_dir)
        self.assertEqual(self._names(result), ["路線_中心線_line.shp"])
        self.assertEqual(sorted(p.suffix for p in self.out_dir.iterdir()), [".cpg", ".dbf", ".prj", ".shp", ".shx"])

    def test_closed_lines_become_polygons(self) -> None:
        dxf = self._road_dxf()
        result = export_to_shapefiles(
            [ExportSource(dxf, cad_layers=("中心線", "用地範圍", "建物"))], self.out_dir, closed_lines_as_polygons=True
        )
        self.assertEqual(self._names(result), ["路線_中心線_line.shp", "路線_用地範圍_poly.shp", "路線_建物_poly.shp"])
        self.assertAlmostEqual(total_area(self.out_dir / "路線_用地範圍_poly.shp"), 600.0, delta=0.01)
        self.assertEqual(self._geometry_types(self.out_dir / "路線_用地範圍_poly.shp"), ["Polygon"])

    def test_cad_closed_polyline_flag_becomes_polygon(self) -> None:
        """CAD 裡設成封閉 (群組碼 70 = 1、最後一點不重複) 的聚合線也轉成面。"""
        dxf = self._dxf("封閉", [("地界", _ring_line(X0, Y0, X0 + 40, Y0 + 25))])
        text = dxf.read_bytes().decode("cp950")
        old_head = "地界\n100\nAcDbPolyline\n 70\n0\n 90\n5\n"
        x0, y1, y0 = int(X0), int(Y0) + 25, int(Y0)
        old_tail = f" 10\n{x0}\n 20\n{y1}\n 10\n{x0}\n 20\n{y0}\n  0\n"
        self.assertIn(old_head, text)
        self.assertIn(old_tail, text)
        text = text.replace(old_head, "地界\n100\nAcDbPolyline\n 70\n1\n 90\n4\n").replace(old_tail, f" 10\n{x0}\n 20\n{y1}\n  0\n")
        dxf.write_bytes(text.encode("cp950"))

        as_lines = export_to_shapefiles([ExportSource(dxf, cad_layers=("地界",))], self.out_dir / "線")
        self.assertEqual(self._names(as_lines), ["封閉_地界_line.shp"])
        as_polygons = export_to_shapefiles(
            [ExportSource(dxf, cad_layers=("地界",))], self.out_dir / "面", closed_lines_as_polygons=True
        )
        self.assertEqual(self._names(as_polygons), ["封閉_地界_poly.shp"])
        self.assertAlmostEqual(total_area(as_polygons.output_files[0]), 1000.0, delta=0.01)

    def test_cad_layer_name_is_made_safe_for_file_names(self) -> None:
        self.assertEqual(file_label('A/B:C*"D'), "A_B_C__D")
        self.assertEqual(file_label("圖層. "), "圖層")
        self.assertEqual(file_label(" "), "空白")
        # CAD 圖層名本來就不能有 / : * 等字元，但可以用點結尾 (Windows 檔名不行)
        dxf = self._dxf("邊線", [("道路邊線.", line((X0, Y0), (X0 + 10, Y0 + 10)))])
        result = export_to_shapefiles([ExportSource(dxf, cad_layers=("道路邊線.",))], self.out_dir)
        self.assertEqual(self._names(result), ["邊線_道路邊線_line.shp"])

    # -------------------------------------------------------------------------
    # 3. 其他格式點線面混雜、檔名
    # -------------------------------------------------------------------------
    def test_mixed_geojson_is_split_but_single_type_keeps_its_name(self) -> None:
        mixed = write_geojson(
            self.data_dir / "混雜.geojson",
            [({"名稱": "樁"}, {"type": "Point", "coordinates": [X0, Y0]}), ({"名稱": "地"}, square(X0, Y0, X0 + 10, Y0 + 10))],
        )
        single = write_geojson(self.data_dir / "只有面.geojson", PARCELS)
        result = export_to_shapefiles([ExportSource(mixed), ExportSource(single)], self.out_dir)
        self.assertEqual(self._names(result), ["混雜_point.shp", "混雜_poly.shp", "只有面.shp"])
        self.assertEqual(attribute_rows(self.out_dir / "混雜_point.shp")[0]["名稱"], "樁")

    def test_duplicate_names_get_a_number_and_sources_are_never_overwritten(self) -> None:
        a = self._shp("宗地", folder=self.data_dir / "甲")
        b = self._shp("宗地", folder=self.data_dir / "乙")
        result = export_to_shapefiles([ExportSource(a), ExportSource(b)], self.out_dir)
        self.assertEqual(self._names(result), ["宗地.shp", "宗地_2.shp"])

        # 輸出到來源所在的資料夾：不蓋掉來源
        before = a.read_bytes()
        same = export_to_shapefiles([ExportSource(a)], a.parent, target_srs="TWD67_121")
        self.assertEqual(self._names(same), ["宗地_2.shp"])
        self.assertEqual(a.read_bytes(), before)
        self.assertEqual(geo_io.read_layer_info(a).epsg, 3826)

        # 再存一次同一個資料夾：取代上次的
        again = export_to_shapefiles([ExportSource(a), ExportSource(b)], self.out_dir)
        self.assertEqual(self._names(again), ["宗地.shp", "宗地_2.shp"])
        self.assertEqual(len([p for p in self.out_dir.iterdir() if p.suffix == ".shp"]), 2)

    # -------------------------------------------------------------------------
    # 4. 另存 KML／KMZ
    # -------------------------------------------------------------------------
    def _kml_tree(self, path: Path) -> ET.Element:
        if path.suffix.lower() == ".kmz":
            with zipfile.ZipFile(path) as archive:
                return ET.fromstring(archive.read(KMZ_DOC_NAME))
        return ET.fromstring(path.read_bytes())

    def _folders(self, root: ET.Element) -> Dict[str, ET.Element]:
        return {folder.findtext(f"{KML}name"): folder for folder in root.iter(f"{KML}Folder")}

    def _placemarks(self, folder: ET.Element) -> List[ET.Element]:
        return list(folder.iter(f"{KML}Placemark"))

    def _geometry_tags(self, folder: ET.Element) -> List[str]:
        tags = ("Point", "LineString", "Polygon", "MultiGeometry")
        return [child.tag.split("}")[1] for mark in self._placemarks(folder) for child in mark if child.tag.split("}")[1] in tags]

    def test_name_field_prefers_chinese_then_name_in_any_case(self) -> None:
        self.assertEqual(kml_name_field(["地號", "NAME"]), "NAME")
        self.assertEqual(kml_name_field(["Name", "名稱", "地號"]), "名稱")
        self.assertEqual(kml_name_field(["name"]), "name")
        self.assertIsNone(kml_name_field(["地號", "Layer"]))

    def test_twd97_source_becomes_wgs84_kml_with_names_attributes_and_style(self) -> None:
        parcels = [({"名稱": f"甲{i}", "Name": f"A{i}", **props}, geom) for i, (props, geom) in enumerate(PARCELS, start=1)]
        source = write_geojson(self.data_dir / "宗地.geojson", parcels)
        result = export_to_kml([ExportSource(source)], self.out_dir, text_callback=self.messages.append)

        self.assertEqual(self._names(result), ["宗地.kml"])
        self.assertEqual(result.skipped, [])
        out = result.output_files[0]
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("<?xml"), text[:60])
        self.assertIn('<kml xmlns="http://www.opengis.net/kml/2.2">', text, "預設命名空間，不要 ns0:")
        self.assertNotIn("wl_kml_name", text, "標記名欄位不另外列進屬性")
        root = self._kml_tree(out)
        folders = self._folders(root)
        self.assertEqual(list(folders), ["宗地"], "只有一個圖層：資料夾名是主檔名")
        marks = self._placemarks(folders["宗地"])
        self.assertEqual([mark.findtext(f"{KML}name") for mark in marks], ["甲1", "甲2"], "「名稱」比「Name」優先")
        data = {item.get("name"): item.text for item in marks[0].iter(f"{KML}SimpleData")}
        self.assertEqual(data, {"Name": "A1", "地號": "0001", "道路用地面積": "2500", "備註": "道路用地"}, "欄位名不截斷")
        self.assertEqual({mark.findtext(f"{KML}styleUrl") for mark in marks}, {"#layer_1.style"})
        style = root.find(f"{KML}Document/{KML}Style")
        self.assertIsNotNone(style)
        self.assertEqual(style.findtext(f"{KML}LineStyle/{KML}color"), "ff2222ff")
        self.assertEqual(style.findtext(f"{KML}PolyStyle/{KML}color"), "4000d7ff", "面半透明，不是 Google Earth 預設的不透明白色")
        # TWD97 (250000, 2650000) 在中央經線上：東經 121.0、北緯約 23.95 (範圍是 最小X、最小Y、最大X、最大Y)
        info = geo_io.read_layer_info(out)
        self.assertEqual(info.epsg, 4326)
        assert info.extent is not None
        self.assertTrue(abs(info.extent[0] - 121.0) < 1e-6 and 23.9 < info.extent[1] < 24.0, info.extent)
        self.assertIn("「宗地.geojson」→ 宗地.kml：1 個資料夾、2 個圖徵", self._log())
        self.assertIn("完成：1 個 KML (WGS84 經緯度，UTF-8)", self._log())
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_viewkml_")], [], "暫存檔要清掉")

    def test_kmz_is_the_same_kml_zipped_as_doc_kml(self) -> None:
        shp = self._shp("宗地")
        kml = export_to_kml([ExportSource(shp)], self.out_dir).output_files[0]
        kmz = export_to_kml([ExportSource(shp)], self.out_dir, kmz=True, text_callback=self.messages.append).output_files[0]
        self.assertEqual(kmz.name, "宗地.kmz")
        with zipfile.ZipFile(kmz) as archive:
            self.assertEqual(archive.namelist(), [KMZ_DOC_NAME])
            self.assertEqual(archive.read(KMZ_DOC_NAME), kml.read_bytes())
        self.assertEqual(len(attribute_rows(kmz, "宗地")), 2, "GDAL 讀得回來")
        self.assertIn("完成：1 個 KMZ", self._log())

    def test_dxf_cad_layers_become_folders_without_splitting_geometry_types(self) -> None:
        dxf = self._road_dxf()
        layers = ("樁位", "中心線", "用地範圍", "建物", "混合")
        result = export_to_kml([ExportSource(dxf, cad_layers=layers)], self.out_dir, closed_lines_as_polygons=True)
        self.assertEqual(self._names(result), ["路線.kml"])
        root = self._kml_tree(result.output_files[0])
        folders = self._folders(root)
        self.assertEqual(list(folders), list(layers), "照勾選順序，每個 CAD 圖層一個資料夾")
        self.assertEqual(self._geometry_tags(folders["樁位"]), ["Point", "Point"])
        self.assertEqual(self._geometry_tags(folders["用地範圍"]), ["Polygon"], "封閉線轉面")
        self.assertEqual(sorted(self._geometry_tags(folders["混合"])), ["LineString", "Point"], "點線放在同一個資料夾")
        self.assertEqual(len(root.findall(f"{KML}Document/{KML}Style")), 5, "每個資料夾一種顏色")
        fields = {item.get("name") for item in root.iter(f"{KML}SimpleField")}
        self.assertEqual(fields, {"Layer", "Text"}, "DXF 同另存 SHP 只留 Layer、Text")
        self.assertEqual(geo_io.list_data_layers(result.output_files[0]), list(layers), "GDAL 讀回是同樣的圖層")

        as_lines = export_to_kml([ExportSource(dxf, cad_layers=("用地範圍",))], self.out_dir / "線")
        self.assertEqual(self._geometry_tags(self._folders(self._kml_tree(as_lines.output_files[0]))["用地範圍"]), ["LineString"])

    def test_kml_folders_are_kept_and_sources_are_never_overwritten(self) -> None:
        source = write_kml(self.out_dir / "會勘.kml")
        before = source.read_bytes()
        chosen = ("會勘點", "工區範圍", "中心線")
        result = export_to_kml([ExportSource(source, data_layers=chosen)], self.out_dir)
        self.assertEqual(self._names(result), ["會勘_2.kml"], "輸出到來源的資料夾：不蓋掉來源")
        self.assertEqual(source.read_bytes(), before)
        root = self._kml_tree(result.output_files[0])
        folders = self._folders(root)
        self.assertEqual(list(folders), list(chosen))
        point_a = self._placemarks(folders["會勘點"])[0]
        self.assertEqual(point_a.findtext(f"{KML}name"), "點A")
        self.assertEqual(point_a.findtext(f"{KML}description"), "擋土牆")
        names = [child.tag.split("}")[1] for child in point_a]
        self.assertLess(names.index("description"), names.index("styleUrl"), "KML 的元素順序")
        fields = {item.get("name") for item in root.iter(f"{KML}SimpleField")}
        self.assertFalse(fields & {"tessellate", "extrude", "visibility", "altitudeMode"}, fields)

        # 同一次存出的檔名重複時加 _2；再存一次取代上次的
        other = write_kml(self.data_dir / "會勘.kmz")
        twice = export_to_kml([ExportSource(source, data_layers=("會勘點",)), ExportSource(other)], self.out_dir / "兩個")
        self.assertEqual(self._names(twice), ["會勘.kml", "會勘_2.kml"])
        again = export_to_kml([ExportSource(source, data_layers=("會勘點",))], self.out_dir / "兩個")
        self.assertEqual(self._names(again), ["會勘.kml"])
        self.assertEqual(list(self._folders(self._kml_tree(again.output_files[0]))), ["會勘點"])

    def test_kml_skips_failed_layers_and_raises_when_nothing_is_saved(self) -> None:
        source = write_kml(self.data_dir / "會勘.kml")
        result = export_to_kml(
            [ExportSource(source, data_layers=("會勘點", "沒有這層"))], self.out_dir, text_callback=self.messages.append
        )
        self.assertEqual(self._names(result), ["會勘.kml"])
        self.assertEqual(len(result.skipped), 1)
        self.assertTrue(result.skipped[0].startswith("會勘.kml：沒有這層："), result.skipped)
        self.assertEqual(list(self._folders(self._kml_tree(result.output_files[0]))), ["會勘點"])

        local = strip_prj(
            convert_vector(
                write_geojson(self.tmp_dir / "local.geojson", [({"n": 1}, square(10, 10, 60, 60))]), self.data_dir / "區域座標.shp"
            )
        )
        with self.assertRaises(ToolkitError) as ctx:
            export_to_kml([ExportSource(local)], self.out_dir / "都不行", kmz=True)
        self.assertIn("都沒有存成 KMZ", str(ctx.exception))
        self.assertIn(NO_SRS_REASON, str(ctx.exception))
        chosen = export_to_kml([ExportSource(local, source_srs="TWD97_121")], self.out_dir / "選了")
        self.assertEqual(self._names(chosen), ["區域座標.kml"])


if __name__ == "__main__":
    unittest.main()
