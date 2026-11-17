"""分頁「CAD 圖面校正」卡片 (單位縮放與控制點對齊) 的測試。"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from core import geo_io
from core.env_runner import run_command
from tests._support import IsolatedWorkspaceTestCase, square, total_area

TWD97_119_LABEL = "TWD97 119 分帶 (EPSG:3825，澎湖金門馬祖)"
# 圖面座標 -> 真實座標：平移 (x + 250000, y + 2650000)
GCP_TEXT = "# 圖面X, 圖面Y, 真實X, 真實Y\n0, 0, 250000, 2650000\n100, 0, 250100, 2650000\n0, 80, 250000, 2650080\n100, 80, 250100, 2650080\n"


class TestCadFixCard(IsolatedWorkspaceTestCase):
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

    def _write_drawing(self, name: str, geometry: dict, *, as_dxf: bool) -> Path:
        """寫一個沒有座標系標籤的圖面檔 (GeoJSON 或 DXF)。"""
        source = self.data_dir / f"{name}.geojson"
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"Layer": "LOT"}, "geometry": geometry}],
        }
        source.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        if not as_dxf:
            return source
        target = self.data_dir / f"{name}.dxf"
        run_command(["ogr2ogr", "-f", "DXF", str(target), str(source)])
        return target

    def _set_gcp_text(self, text: str) -> None:
        self.tab.txt_gcp.delete("1.0", "end")
        self.tab.txt_gcp.insert("1.0", text)

    def test_scale_dxf_from_millimetres(self) -> None:
        dxf = self._write_drawing("mm_plan", square(10000, 10000, 60000, 40000), as_dxf=True)
        self.tab._set_entry(self.tab.entry_cad_fix_file, str(dxf))
        self.assertEqual(self.tab.entry_cad_scale.get(), "0.001")
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_scale_cad()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
        scaled = self.data_dir / "mm_plan_scaled.dxf"
        self.assertTrue(scaled.exists(), "縮放成果應與來源同資料夾、同格式")
        min_x, min_y, max_x, max_y = geo_io.read_layer_info(scaled).extent
        self.assertAlmostEqual(10.0, min_x, places=4)
        self.assertAlmostEqual(10.0, min_y, places=4)
        self.assertAlmostEqual(60.0, max_x, places=4)
        self.assertAlmostEqual(40.0, max_y, places=4)
        self.assertIn("[單位縮放完成]", self._log_text())

    def test_scale_rejects_bad_inputs(self) -> None:
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box:
            self.tab._handle_scale_cad()
            self.assertEqual(tab_box.showwarning.call_count, 1)

            self.tab._set_entry(self.tab.entry_cad_fix_file, str(self.data_dir / "plan.dwg"))
            self.tab._handle_scale_cad()
            self.assertEqual(tab_box.showwarning.call_count, 2)
            self.assertIn("另存為 DXF", tab_box.showwarning.call_args[0][1])

            self.tab._set_entry(self.tab.entry_cad_fix_file, str(self.data_dir / "missing.dxf"))
            self.tab._handle_scale_cad()
            self.assertEqual(tab_box.showwarning.call_count, 3)

            drawing = self._write_drawing("plan", square(0, 0, 10, 10), as_dxf=False)
            self.tab._set_entry(self.tab.entry_cad_fix_file, str(drawing))
            for bad in ("千分之一", "0", "-2"):
                self.tab._set_entry(self.tab.entry_cad_scale, bad)
                self.tab._handle_scale_cad()
            self.assertEqual(tab_box.showerror.call_count, 3)
        self.assertFalse(self.inspector.is_busy)
        self.assertFalse((self.data_dir / "plan_scaled.geojson").exists())

    def test_gcp_preview_shows_rmse_and_flags_bad_points(self) -> None:
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box:
            self._set_gcp_text(GCP_TEXT)
            self.tab._handle_gcp_preview()
            tab_box.showerror.assert_not_called()
            self.assertIn("RMSE：0.0000", self.tab.lbl_gcp_rmse.cget("text"))
            self.assertIn("控制點 4 組", self.tab.lbl_gcp_rmse.cget("text"))
            self.assertIn("[GCP 試算]", self._log_text())

            # 第四點真實座標打錯 30 公尺：RMSE 偏大要提醒
            self._set_gcp_text(GCP_TEXT.replace("250100, 2650080", "250130, 2650080"))
            self.tab._handle_gcp_preview()
            tab_box.showerror.assert_not_called()
            self.assertIn("誤差偏大", self.tab.lbl_gcp_rmse.cget("text"))

            self._set_gcp_text("0 0 250000 2650000\n1 1 250001 2650001\n2 2 250002 2650002\n")
            self.tab._handle_gcp_preview()
            self.assertEqual(tab_box.showerror.call_count, 1)
            self.assertIn("共線", tab_box.showerror.call_args[0][1])

            self._set_gcp_text("0 0 250000\n")
            self.tab._handle_gcp_preview()
            self.assertEqual(tab_box.showerror.call_count, 2)
            self.assertIn("第 1 列", tab_box.showerror.call_args[0][1])
            self.assertIn("無法試算", self.tab.lbl_gcp_rmse.cget("text"))

    def test_gcp_georeference_dxf_end_to_end(self) -> None:
        dxf = self._write_drawing("local_plan", square(10, 10, 60, 40), as_dxf=True)
        self.tab._set_entry(self.tab.entry_cad_fix_file, str(dxf))
        self._set_gcp_text(GCP_TEXT)
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_gcp_georeference()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            self.assertIn("RMSE = 0.0000", tab_box.showinfo.call_args[0][1])
        out = self.data_dir / "local_plan_georef.gpkg"
        self.assertTrue(out.exists())
        info = geo_io.read_layer_info(out)
        self.assertEqual(info.epsg, 3826)
        min_x, min_y, max_x, max_y = info.extent
        self.assertAlmostEqual(250010.0, min_x, places=3)
        self.assertAlmostEqual(2650010.0, min_y, places=3)
        self.assertAlmostEqual(250060.0, max_x, places=3)
        self.assertAlmostEqual(2650040.0, max_y, places=3)
        self.assertAlmostEqual(1500.0, total_area(out), delta=0.01)
        self.assertIn("[GCP 校正完成]", self._log_text())

    def test_gcp_target_srs_menu_reaches_engine(self) -> None:
        drawing = self._write_drawing("island", square(10, 10, 60, 40), as_dxf=False)
        self.tab._set_entry(self.tab.entry_cad_fix_file, str(drawing))
        self._set_gcp_text(GCP_TEXT)
        self.tab.opt_gcp_target_srs.set(TWD97_119_LABEL)
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self.tab._handle_gcp_georeference()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
        self.assertEqual(geo_io.read_layer_info(self.data_dir / "island_georef.gpkg").epsg, 3825)

    def test_gcp_georeference_stops_on_bad_points(self) -> None:
        drawing = self._write_drawing("plan2", square(10, 10, 60, 40), as_dxf=False)
        self.tab._set_entry(self.tab.entry_cad_fix_file, str(drawing))
        self._set_gcp_text("0 0 250000 2650000\n100 0 250100 2650000\n")
        with mock.patch("ui.lab.tab_vector_cad.messagebox") as tab_box:
            self.tab._handle_gcp_georeference()
            tab_box.showerror.assert_called_once()
            self.assertIn("至少需要 3 組", tab_box.showerror.call_args[0][1])
        self.assertFalse(self.inspector.is_busy)
        self.assertFalse((self.data_dir / "plan2_georef.gpkg").exists())


if __name__ == "__main__":
    unittest.main()
