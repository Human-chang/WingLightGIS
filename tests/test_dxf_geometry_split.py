"""DXF 依幾何型別分層 整合測試 (tests/test_dxf_geometry_split.py)

split_dxf_by_geometry_type 把 DXF 的點、線、面拆成同一個 GPKG 的三個圖層，
強制 2D、保留 Layer 與 Text 欄位，並依來源座標系決策重投影。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

from core import geo_io
from core.crs_doctor import SourceSrsUndeterminedError
from core.env_runner import ToolkitError, run_command, run_json_command
from core.vector_ops import split_dxf_by_geometry_type
from tests._support import IsolatedWorkspaceTestCase, attribute_rows, total_area

Feature = Tuple[Dict[str, Any], Dict[str, Any]]


def point(x: float, y: float, z: float) -> Dict[str, Any]:
    return {"type": "Point", "coordinates": [x, y, z]}


def line_3d(*points: Tuple[float, float, float]) -> Dict[str, Any]:
    return {"type": "LineString", "coordinates": [list(p) for p in points]}


def rectangle(x0: float, y0: float, x1: float, y1: float) -> Dict[str, Any]:
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def mixed_features(origin_x: float = 250000.0, origin_y: float = 2650000.0) -> List[Feature]:
    """兩個樁位點、一條帶高程的中心線、一個 30 m × 20 m 的建物面。"""
    return [
        ({"Layer": "樁位"}, point(origin_x + 10.0, origin_y + 10.0, 12.5)),
        ({"Layer": "樁位"}, point(origin_x + 20.0, origin_y + 20.0, 13.5)),
        ({"Layer": "中心線"}, line_3d((origin_x, origin_y, 10.0), (origin_x + 100.0, origin_y + 50.0, 20.0))),
        ({"Layer": "建物"}, rectangle(origin_x + 30.0, origin_y + 30.0, origin_x + 60.0, origin_y + 50.0)),
    ]


class DxfGeometrySplitTest(IsolatedWorkspaceTestCase):
    def _write_dxf(self, name: str, features: List[Feature], *, big5: bool = True) -> Path:
        """以 ogr2ogr 寫出 DXF (圖元帶 Z 值)；big5=True 時改成臺灣 AutoCAD 常見的 ANSI_950 (Big5) 編碼。"""
        source = self.data_dir / f"{name}.geojson"
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": props, "geometry": geom} for props, geom in features],
        }
        source.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        target = self.data_dir / f"{name}.dxf"
        run_command(["ogr2ogr", "-f", "DXF", str(target), str(source)])
        if big5:
            text = target.read_text(encoding="utf-8")
            self.assertEqual(text.count("ANSI_1252"), 1)
            target.write_bytes(text.replace("ANSI_1252", "ANSI_950").encode("cp950"))
        return target

    def _geometry_types(self, gpkg: Path, layer: str) -> List[str]:
        info = run_json_command(["ogrinfo", "-json", "-features", str(gpkg), layer])
        return [feature["geometry"]["type"] for feature in info["layers"][0]["features"]]

    def _coordinate_depths(self, gpkg: Path, layer: str) -> List[int]:
        """各圖徵第一個頂點的座標維度 (2 表示沒有 Z)。"""
        info = run_json_command(["ogrinfo", "-json", "-features", str(gpkg), layer])
        depths = []
        for feature in info["layers"][0]["features"]:
            coords = feature["geometry"]["coordinates"]
            while isinstance(coords[0], list):
                coords = coords[0]
            depths.append(len(coords))
        return depths

    def test_mixed_dxf_is_split_into_three_2d_layers(self) -> None:
        dxf = self._write_dxf("混合圖", mixed_features())
        messages: List[str] = []

        result = split_dxf_by_geometry_type(dxf, self.out_dir, text_callback=messages.append)

        self.assertEqual(result.output_gpkg, self.out_dir / "混合圖_layers.gpkg")
        self.assertEqual(result.layer_feature_counts, {"points": 2, "lines": 1, "polygons": 1})
        self.assertEqual(result.skipped_feature_count, 0)
        self.assertEqual(sorted(geo_io.list_layer_names(result.output_gpkg)), ["lines", "points", "polygons"])

        expected_types = {"points": "Point", "lines": "MultiLineString", "polygons": "MultiPolygon"}
        expected_cad_layers = {"points": ["樁位", "樁位"], "lines": ["中心線"], "polygons": ["建物"]}
        for layer, geometry_type in expected_types.items():
            info = geo_io.read_layer_info(result.output_gpkg, layer)
            self.assertEqual(info.epsg, 3826, layer)
            self.assertEqual(info.field_names, ["Layer", "Text"], layer)
            self.assertEqual(set(self._geometry_types(result.output_gpkg, layer)), {geometry_type}, layer)
            # 來源圖元帶 Z 值，成果一律是 2D
            self.assertEqual(set(self._coordinate_depths(result.output_gpkg, layer)), {2}, layer)
            cad_layers = [row["Layer"] for row in attribute_rows(result.output_gpkg, layer)]
            self.assertEqual(cad_layers, expected_cad_layers[layer], layer)

        self.assertAlmostEqual(total_area(result.output_gpkg, "polygons"), 600.0, delta=0.01)
        points = geo_io.read_layer_info(result.output_gpkg, "points")
        self.assertAlmostEqual(points.extent[0], 250010.0, places=3)
        self.assertTrue(any("點 2 個" in message and "線 1 個" in message and "面 1 個" in message for message in messages))
        self.assertEqual([p.name for p in self.work_temp.iterdir() if p.name.startswith("_dxf_split_")], [])
        self.assertEqual([p.name for p in self.out_dir.iterdir()], ["混合圖_layers.gpkg"])

    def test_missing_geometry_types_produce_no_layer_and_rerun_replaces_output(self) -> None:
        dxf = self._write_dxf("混合圖", mixed_features())
        first = split_dxf_by_geometry_type(dxf, self.out_dir)
        self.assertIn("polygons", geo_io.list_layer_names(first.output_gpkg))

        # 同名 DXF 換成只有線：前一次的 points／polygons 圖層不可殘留
        lines_only = [({"Layer": "ROAD"}, line_3d((250000.0, 2650000.0, 1.0), (250050.0, 2650000.0, 2.0)))]
        dxf = self._write_dxf("混合圖", lines_only, big5=False)
        second = split_dxf_by_geometry_type(dxf, self.out_dir)

        self.assertEqual(second.layer_feature_counts, {"lines": 1})
        self.assertEqual(geo_io.list_layer_names(second.output_gpkg), ["lines"])

    def test_default_output_goes_to_workspace_temp(self) -> None:
        dxf = self._write_dxf("預設輸出", mixed_features())
        result = split_dxf_by_geometry_type(dxf)
        self.assertEqual(result.output_gpkg, self.work_temp / "預設輸出_layers.gpkg")
        self.assertTrue(result.output_gpkg.exists())

    def test_assigned_twd67_is_reprojected_to_twd97(self) -> None:
        dxf = self._write_dxf("舊圖", mixed_features())

        result = split_dxf_by_geometry_type(dxf, self.out_dir, assigned_srs="TWD67_121")

        points = geo_io.read_layer_info(result.output_gpkg, "points")
        self.assertEqual(points.epsg, 3826)
        # TWD67 → TWD97 約往東 828 m、往南 207 m
        self.assertAlmostEqual(points.extent[0] - 250010.0, 828.0, delta=10.0)
        self.assertAlmostEqual(points.extent[1] - 2650010.0, -207.0, delta=10.0)

    def test_local_coordinates_require_assignment(self) -> None:
        dxf = self._write_dxf("假設座標", mixed_features(origin_x=0.0, origin_y=0.0))

        with self.assertRaises(SourceSrsUndeterminedError):
            split_dxf_by_geometry_type(dxf, self.out_dir)
        self.assertEqual(list(self.out_dir.iterdir()), [])

        result = split_dxf_by_geometry_type(dxf, self.out_dir, assigned_srs="TWD97_121")
        self.assertAlmostEqual(total_area(result.output_gpkg, "polygons"), 600.0, delta=0.01)

    def test_rejects_non_dxf_and_missing_file(self) -> None:
        geojson = self.data_dir / "不是dxf.geojson"
        geojson.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        with self.assertRaises(ToolkitError):
            split_dxf_by_geometry_type(geojson, self.out_dir)
        with self.assertRaises(FileNotFoundError):
            split_dxf_by_geometry_type(self.data_dir / "不存在.dxf", self.out_dir)


if __name__ == "__main__":
    unittest.main()
