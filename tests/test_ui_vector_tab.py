"""第 5 步第二段：向量與 CAD 分頁改走 run_task、來源座標系選單、DXF 點線面分層卡片的測試。"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from core import geo_io
from core.env_runner import run_command
from tests._support import IsolatedWorkspaceTestCase, convert_vector, square, write_geojson

TWD67_LABEL = "TWD67 121 分帶 (七參數轉換，臺灣本島)"


class TestVectorTab(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.tab = self.app.tab_vector
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy:
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _log_text(self) -> str:
        return self.inspector.txt_terminal.get("1.0", "end")

    def _write_dxf(self) -> Path:
        source = self.data_dir / "mixed.geojson"
        features = [
            ({"Layer": "P"}, {"type": "Point", "coordinates": [250010.0, 2650010.0, 12.5]}),
            ({"Layer": "P"}, {"type": "Point", "coordinates": [250020.0, 2650020.0, 13.5]}),
            ({"Layer": "L"}, {"type": "LineString", "coordinates": [[250000.0, 2650000.0, 10.0], [250100.0, 2650050.0, 20.0]]}),
            ({"Layer": "B"}, square(250030, 2650030, 250060, 2650050)),
        ]
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": props, "geometry": geom} for props, geom in features],
        }
        source.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        target = self.data_dir / "mixed.dxf"
        run_command(["ogr2ogr", "-f", "DXF", str(target), str(source)])
        return target

    def test_dxf_split_card_end_to_end(self) -> None:
        dxf = self._write_dxf()
        self.tab._set_entry(self.tab.entry_dxf_split_file, str(dxf))
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_split_dxf_by_geometry()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]

        out_gpkg = dxf.parent / "mixed_layers.gpkg"
        self.assertTrue(out_gpkg.exists(), "成果應寫在 DXF 同資料夾")
        self.assertEqual(geo_io.read_layer_info(out_gpkg, "points").feature_count, 2)
        self.assertEqual(geo_io.read_layer_info(out_gpkg, "lines").feature_count, 1)
        self.assertEqual(geo_io.read_layer_info(out_gpkg, "polygons").feature_count, 1)
        self.assertIn("點 2 個", message)
        self.assertIn("線 1 個", message)
        self.assertIn("面 1 個", message)
        self.assertIn("[DXF 分層完成]", self._log_text())

    def test_dxf_split_card_rejects_dwg(self) -> None:
        self.tab._set_entry(self.tab.entry_dxf_split_file, str(self.data_dir / "plan.dwg"))
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box:
            self.tab._handle_split_dxf_by_geometry()
            tab_box.showwarning.assert_called_once()
            self.assertIn("DXF", tab_box.showwarning.call_args[0][1])
        self.assertFalse(self.inspector.is_busy)

    def test_cadastral_intersection_accepts_gpkg_end_to_end(self) -> None:
        boundary = write_geojson(self.data_dir / "boundary.geojson", [({"name": "site"}, square(250050, 2650000, 250150, 2650100))])
        parcels_src = write_geojson(
            self.data_dir / "parcels.geojson",
            [
                ({"段名": "大安段", "地號": "1"}, square(250000, 2650000, 250100, 2650100)),
                ({"段名": "信義段", "地號": "2"}, square(250100, 2650000, 250200, 2650100)),
            ],
        )
        parcels = convert_vector(parcels_src, self.data_dir / "parcels.gpkg")
        self.tab._set_entry(self.tab.entry_inter_bound, str(boundary))
        self.tab._set_entry(self.tab.entry_inter_cad, str(parcels))
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_run_cadastral_intersection()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            self.assertIn("劃入地號數: 2 筆", tab_box.showinfo.call_args[0][1])
        self.assertIn("[交集完成]", self._log_text())

    def test_merge_needs_two_files_and_writes_beside_first_input(self) -> None:
        first = write_geojson(self.data_dir / "a.geojson", [({"n": 1}, square(250000, 2650000, 250100, 2650100))])
        second = write_geojson(self.data_dir / "b.geojson", [({"n": 2}, square(250100, 2650000, 250200, 2650100))])

        self.tab.merge_files = [first.resolve()]
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box:
            self.tab._handle_run_merge_vectors()
            tab_box.showwarning.assert_called_once()
        self.assertFalse(self.inspector.is_busy)

        self.tab.merge_files = [first.resolve(), second.resolve()]
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_run_merge_vectors()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
        merged = self.data_dir / "merged_layers.gpkg"
        self.assertTrue(merged.exists(), "合併成果應寫在第一個來源檔的資料夾，而不是暫存目錄")
        self.assertEqual(geo_io.read_layer_info(merged).feature_count, 2)

    def test_selected_source_srs_reaches_every_engine(self) -> None:
        self.tab.opt_source_srs.set(TWD67_LABEL)
        shp = str(self.data_dir / "x.shp")
        self.tab._set_entry(self.tab.entry_simp_file, shp)
        self.tab._set_entry(self.tab.entry_csv_file, str(self.data_dir / "pts.csv"))
        self.tab._set_entry(self.tab.entry_split_file, shp)
        self.tab._set_entry(self.tab.entry_split_col, "SECT")
        self.tab._set_entry(self.tab.entry_inter_bound, shp)
        self.tab._set_entry(self.tab.entry_inter_cad, shp)
        self.tab._set_entry(self.tab.entry_dxf_split_file, str(self.data_dir / "x.dxf"))
        self.tab.merge_files = [Path(shp).resolve(), (self.data_dir / "y.shp").resolve()]

        fake_cad = mock.Mock(
            total_parcels_count=1, total_clipped_area_m2=1.0, total_clipped_area_ha=0.0001, excel_report_path=Path("r.xlsx")
        )
        fake_dxf = mock.Mock(layer_feature_counts={"lines": 3}, skipped_feature_count=2, output_gpkg=Path("o.gpkg"))
        engines = {
            "simplify_vector_geometry": (self.tab._handle_simplify, Path("s.shp")),
            "convert_csv_table_to_points": (self.tab._handle_convert_csv_to_points, Path("p.gpkg")),
            "split_vector_by_attribute": (self.tab._handle_run_split_by_attribute, [Path("1.shp")]),
            "generate_cadastral_intersection_report": (self.tab._handle_run_cadastral_intersection, fake_cad),
            "split_dxf_by_geometry_type": (self.tab._handle_split_dxf_by_geometry, fake_dxf),
            "merge_vector_layers": (self.tab._handle_run_merge_vectors, Path("m.gpkg")),
        }
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            for name, (handler, result) in engines.items():
                with self.subTest(engine=name), mock.patch(f"ui.lab.tab_vector_cad.{name}", return_value=result) as engine:
                    handler()
                    self._pump_until_idle()
                    engine.assert_called_once()
                    self.assertEqual(engine.call_args[1]["assigned_srs"], "TWD67_121")
            box.showerror.assert_not_called()
            self.assertEqual(tab_box.showinfo.call_count, len(engines))
        self.assertIn("線 3 個；另有 2 個非點線面圖元被略過", self._log_text())


if __name__ == "__main__":
    unittest.main()
