"""第 5 步第十一段：分頁 2 每張卡片可自選輸出資料夾，留空時跟著來源檔走。"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

from core.env_runner import run_command
from tests._support import IsolatedWorkspaceTestCase, convert_vector, line, square, write_geojson
from ui.output_dir_row import OutputDirRow

GCP_TEXT = "0, 0, 250000, 2650000\n100, 0, 250100, 2650000\n0, 80, 250000, 2650080\n100, 80, 250100, 2650080\n"


class TestVectorOutputDirRows(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.tab = self.app.tab_vector
        self.app.update()
        self.custom_dir = self.tmp_dir / "自選 成果" / "向量"

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 150.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy:
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _run(self, handler: Callable[[], None]) -> str:
        """執行處置函式到背景作業結束，回傳完成對話框的內文。"""
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            handler()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showerror.assert_not_called()
            tab_box.showwarning.assert_not_called()
            tab_box.showinfo.assert_called_once()
            return tab_box.showinfo.call_args[0][1]

    def _parcels_shp(self) -> Path:
        source = write_geojson(
            self.data_dir / "parcels.geojson",
            [
                ({"SECT": "A", "NO": "1"}, square(250000, 2650000, 250100, 2650100)),
                ({"SECT": "B", "NO": "2"}, square(250100, 2650000, 250200, 2650100)),
            ],
        )
        return convert_vector(source, self.data_dir / "parcels.shp")

    def _write_dxf(self, name: str, x0: float = 0.0, y0: float = 0.0) -> Path:
        source = self.data_dir / f"{name}.geojson"
        features = [
            ({"Layer": "P"}, {"type": "Point", "coordinates": [x0 + 10.0, y0 + 10.0]}),
            ({"Layer": "L"}, line((x0, y0), (x0 + 100.0, y0 + 80.0))),
            ({"Layer": "B"}, square(x0 + 10, y0 + 10, x0 + 60, y0 + 40)),
        ]
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": props, "geometry": geom} for props, geom in features],
        }
        source.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        target = self.data_dir / f"{name}.dxf"
        run_command(["ogr2ogr", "-f", "DXF", str(target), str(source)])
        source.unlink()
        return target

    def _data_names(self) -> list:
        return sorted(p.name for p in self.data_dir.iterdir())

    def test_every_card_has_its_own_empty_output_row(self) -> None:
        tab = self.tab
        rows = [
            tab.out_cadastral,
            tab.out_merge,
            tab.out_simplify,
            tab.out_csv,
            tab.out_split,
            tab.out_dxf_split,
            tab.out_cad_fix,
        ]
        self.assertEqual(len({id(row.entry) for row in rows}), 7)
        self.assertEqual(len({str(row.entry.master) for row in rows}), 7, "每張卡片各一列")
        for row in rows:
            self.assertIsInstance(row, OutputDirRow)
            self.assertIsNone(row.chosen())
            card = row.entry.master
            occupied = [int(child.grid_info()["row"]) for child in card.grid_slaves() if child not in (row.label, row.entry, row.button)]
            self.assertNotIn(int(row.entry.grid_info()["row"]), occupied, "輸出資料夾列不可與其他元件疊在同一列")

    def test_output_pointing_at_a_file_is_rejected_on_every_card(self) -> None:
        tab = self.tab
        shp = self._parcels_shp()
        dxf = self._write_dxf("plan")
        csv = self.data_dir / "pts.csv"
        csv.write_text("name,X,Y\na,250000,2650000\n", encoding="utf-8")
        before = self._data_names()
        for entry in (tab.entry_inter_bound, tab.entry_inter_cad, tab.entry_simp_file, tab.entry_split_file):
            tab._set_entry(entry, str(shp))
        tab._set_entry(tab.entry_csv_file, str(csv))
        tab._set_entry(tab.entry_split_col, "SECT")
        tab._set_entry(tab.entry_dxf_split_file, str(dxf))
        tab._set_entry(tab.entry_cad_fix_file, str(dxf))
        tab.txt_gcp.insert("1.0", GCP_TEXT)
        tab.merge_files = [shp.resolve(), dxf.resolve()]
        for row in (tab.out_cadastral, tab.out_merge, tab.out_simplify, tab.out_csv, tab.out_split, tab.out_dxf_split, tab.out_cad_fix):
            row.set(str(csv))
        handlers = [
            tab._handle_run_cadastral_intersection,
            tab._handle_run_merge_vectors,
            tab._handle_fix_encoding,
            tab._handle_simplify,
            tab._handle_convert_csv_to_points,
            tab._handle_run_split_by_attribute,
            tab._handle_split_dxf_by_geometry,
            tab._handle_scale_cad,
            tab._handle_gcp_georeference,
        ]
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box:
            for count, handler in enumerate(handlers, start=1):
                handler()
                self.assertEqual(tab_box.showwarning.call_count, count, handler.__name__)
                self.assertIn("請改選資料夾", tab_box.showwarning.call_args[0][1])
            tab_box.showinfo.assert_not_called()
            tab_box.showerror.assert_not_called()
        self.assertFalse(self.inspector.is_busy)
        self.assertEqual(self._data_names(), before)

    def test_cadastral_writes_into_chosen_folder(self) -> None:
        tab = self.tab
        boundary = write_geojson(self.data_dir / "boundary.geojson", [({"name": "site"}, square(250050, 2650000, 250150, 2650100))])
        tab._set_entry(tab.entry_inter_bound, str(boundary))
        tab._set_entry(tab.entry_inter_cad, str(self._parcels_shp()))
        tab.out_cadastral.set(str(self.custom_dir))
        message = self._run(tab._handle_run_cadastral_intersection)
        self.assertIn(f"成果資料夾: {self.custom_dir.resolve()}", message)
        self.assertEqual(len(list(self.custom_dir.glob("*.xlsx"))), 1)
        self.assertFalse((self.data_dir / "parcels_地籍交集成果").exists())

    def test_merge_writes_into_chosen_folder(self) -> None:
        tab = self.tab
        first = write_geojson(self.data_dir / "a.geojson", [({"n": 1}, square(250000, 2650000, 250100, 2650100))])
        second = write_geojson(self.data_dir / "b.geojson", [({"n": 2}, square(250100, 2650000, 250200, 2650100))])
        tab.merge_files = [first.resolve(), second.resolve()]
        tab.out_merge.set(str(self.custom_dir))
        message = self._run(tab._handle_run_merge_vectors)
        self.assertIn(str(self.custom_dir.resolve() / "merged_layers.gpkg"), message)
        self.assertTrue((self.custom_dir / "merged_layers.gpkg").exists())
        self.assertFalse((self.data_dir / "merged_layers.gpkg").exists())

    def test_encoding_and_simplify_share_one_row(self) -> None:
        tab = self.tab
        shp = self._parcels_shp()
        tab._set_entry(tab.entry_simp_file, str(shp))

        # 留空：跟著來源檔走
        self._run(tab._handle_fix_encoding)
        self.assertTrue((self.data_dir / "parcels_utf8.shp").exists())

        tab.out_simplify.set(str(self.custom_dir))
        message = self._run(tab._handle_fix_encoding)
        self.assertIn(str(self.custom_dir.resolve() / "parcels_utf8.shp"), message)
        self._run(tab._handle_simplify)
        for name in ("parcels_utf8.shp", "parcels_utf8.cpg", "parcels_simp1m.shp"):
            self.assertTrue((self.custom_dir / name).exists(), f"缺少 {name}")
        self.assertFalse((self.data_dir / "parcels_simp1m.shp").exists())

    def test_csv_points_write_into_chosen_folder(self) -> None:
        tab = self.tab
        csv = self.data_dir / "樁位.csv"
        csv.write_text("點名,X,Y\n甲,250000,2650000\n乙,250100,2650100\n", encoding="utf-8")
        tab._set_entry(tab.entry_csv_file, str(csv))
        tab.out_csv.set(str(self.custom_dir))
        message = self._run(tab._handle_convert_csv_to_points)
        self.assertIn(str(self.custom_dir.resolve() / "樁位_points.gpkg"), message)
        self.assertTrue((self.custom_dir / "樁位_points.gpkg").exists())
        self.assertFalse((self.data_dir / "樁位_points.gpkg").exists())

    def test_split_by_attribute_follows_chosen_folder_or_source(self) -> None:
        tab = self.tab
        tab._set_entry(tab.entry_split_file, str(self._parcels_shp()))
        tab._set_entry(tab.entry_split_col, "SECT")

        message = self._run(tab._handle_run_split_by_attribute)
        self.assertIn("共產出: 2 個獨立檔案", message)
        self.assertTrue((self.data_dir / "split_SECT").is_dir())

        tab.out_split.set(str(self.custom_dir))
        message = self._run(tab._handle_run_split_by_attribute)
        self.assertIn(f"目錄: {self.custom_dir.resolve()}", message)
        produced = [p for p in self.custom_dir.rglob("*") if p.is_file()]
        self.assertTrue(produced)
        self.assertFalse((self.custom_dir / "split_SECT").exists(), "自選資料夾時不再多包一層")

    def test_dxf_split_writes_into_chosen_folder(self) -> None:
        tab = self.tab
        dxf = self._write_dxf("mixed", 250000.0, 2650000.0)
        tab._set_entry(tab.entry_dxf_split_file, str(dxf))
        tab.out_dxf_split.set(str(self.custom_dir))
        message = self._run(tab._handle_split_dxf_by_geometry)
        self.assertIn(str(self.custom_dir.resolve() / "mixed_layers.gpkg"), message)
        self.assertTrue((self.custom_dir / "mixed_layers.gpkg").exists())
        self.assertEqual(self._data_names(), ["mixed.dxf"])

    def test_cad_scale_and_gcp_share_one_row(self) -> None:
        tab = self.tab
        dxf = self._write_dxf("local_plan")
        tab._set_entry(tab.entry_cad_fix_file, str(dxf))
        tab.txt_gcp.insert("1.0", GCP_TEXT)
        tab.out_cad_fix.set(str(self.custom_dir))
        message = self._run(tab._handle_scale_cad)
        self.assertIn(str(self.custom_dir.resolve() / "local_plan_scaled.dxf"), message)
        message = self._run(tab._handle_gcp_georeference)
        self.assertIn(str(self.custom_dir.resolve() / "local_plan_georef.gpkg"), message)
        for name in ("local_plan_scaled.dxf", "local_plan_georef.gpkg"):
            self.assertTrue((self.custom_dir / name).exists(), f"缺少 {name}")
        self.assertEqual(self._data_names(), ["local_plan.dxf"])


if __name__ == "__main__":
    unittest.main()
