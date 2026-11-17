"""裁切分頁：改走 run_task、外推不累加、清單點選移除、閉鎖容許、DEM 解析度欄位，以及配方列與右欄 KMZ 鈕已拿掉 (步驟 8-1)；
清單的勾選：沒勾的檔不送去裁切 (風格統一 G 段)。"""

from __future__ import annotations

import re
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from ui.tab_clip_workflow import format_resolution, parse_resolution
from tests._support import IsolatedWorkspaceTestCase, attribute_rows, square, total_area, write_dem, write_geojson, write_world_image

SQUARE_TEXT = "250000 2650000\n250100 2650000\n250100 2650100\n250000 2650100\n"


def decode_js_escapes(html: str) -> str:
    """folium 把圖層名稱的中文寫成 \\uXXXX，還原後才好比對。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), html)


class TestClipTab(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.tab = self.app.tab_clip
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy and all(t._light_pending == 0 for t in self.app.clip_tabs.values()):
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _use_tab(self, tab: Any) -> None:
        """切到指定的裁切分頁 (等同使用者點分頁)，之後的操作都對這一頁。"""
        self.tab = tab
        self.app.select_tool(tab.kind.key)

    def _log_text(self) -> str:
        return self.inspector.txt_terminal.get("1.0", "end")

    def _build_boundary_from_text(self) -> Path:
        self.tab.txt_coords.delete("1.0", "end")
        self.tab.txt_coords.insert("1.0", SQUARE_TEXT)
        self.tab._handle_create_boundary_from_text()
        self._pump_until_idle()
        self.assertIsNotNone(self.tab.master_boundary_gpkg, self._log_text())
        return self.tab.master_boundary_gpkg

    def _set_out_dir(self, out_dir: Path) -> None:
        self.tab.entry_out_dir.delete(0, "end")
        self.tab.entry_out_dir.insert(0, str(out_dir))

    def _set_buffer(self, text: str) -> None:
        self.tab.entry_buffer.delete(0, "end")
        self.tab.entry_buffer.insert(0, text)

    def test_paste_coordinates_builds_boundary(self) -> None:
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            boundary = self._build_boundary_from_text()
            box.showerror.assert_not_called()
            tab_box.showwarning.assert_not_called()
        self.assertTrue(boundary.exists())
        self.assertAlmostEqual(total_area(boundary), 10000.0, delta=1.0)
        self.assertIn("10,000.00", self.tab.lbl_paste_area.cget("text"))
        self.assertIn("自動閉合: 是", self.tab.lbl_paste_area.cget("text"))
        # 右欄縮圖只有邊界 (還沒投入原始檔)；瀏覽器地圖要按鈕才產生
        self.assertIsNotNone(self.inspector.current_preview_image)
        self.assertIn("還沒投入原始檔", self.inspector.lbl_preview_caption.cget("text"))
        self.assertIsNone(self.inspector.current_preview_html)
        self.assertIn("[邊界完成]", self._log_text())

    def test_buffer_does_not_accumulate_and_zero_restores(self) -> None:
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            base = self._build_boundary_from_text()
            self._set_buffer("20")
            self.tab._handle_apply_buffer()
            self._pump_until_idle()
            first = self.tab.master_boundary_gpkg
            first_area = total_area(first)
            self.tab._handle_apply_buffer()
            self._pump_until_idle()
            second_area = total_area(self.tab.master_boundary_gpkg)

            self.assertNotEqual(first, base)
            self.assertGreater(first_area, 140.0 * 140.0 - 400.0)
            self.assertLess(first_area, 140.0 * 140.0 + 1.0)
            self.assertAlmostEqual(first_area, second_area, delta=0.5, msg="重複按外推不可累加")

            self._set_buffer("0")
            self.tab._handle_apply_buffer()
            self._pump_until_idle()
            self.assertEqual(self.tab.master_boundary_gpkg, base)
            box.showerror.assert_not_called()
            self.assertEqual(tab_box.showinfo.call_count, 3)
        self.assertIn("[外推還原]", self._log_text())

    def test_remove_selected_removes_the_clicked_row(self) -> None:
        self._use_tab(self.app.tab_shp)
        paths = [
            write_geojson(self.data_dir / f"{name}.geojson", [({"n": name}, square(250000, 2650000, 250050, 2650050))])
            for name in ("a", "b", "c")
        ]
        self.assertEqual(self.tab._add_input_files(paths), 3)
        self.assertEqual(self.tab._add_input_files(paths[:1]), 0, "重複的檔案不可再加入")
        self._pump_until_idle()
        # 判別完類型後，第二欄是所在資料夾
        self.assertEqual([row.details for row in self.tab.file_list.rows], [(self.data_dir.name,)] * 3)

        self.tab._handle_remove_selected()
        self.assertEqual(len(self.tab.selected_input_files), 3, "沒點選時不可移除任何一筆")
        self.assertIn("請先在清單點選", self._log_text())

        self.tab._select_file_index(1)
        self._pump_until_idle()
        self.assertIn("已選取: b.geojson", self.tab.lbl_file_count.cget("text"))
        self.tab._handle_remove_selected()
        self.assertEqual([p.name for p in self.tab.selected_input_files], ["a.geojson", "c.geojson"])
        self.assertIsNone(self.tab._selected_file_index)

    def test_unchecked_files_are_skipped(self) -> None:
        self._use_tab(self.app.tab_shp)
        paths = [
            write_geojson(self.data_dir / f"{name}.geojson", [({"n": name}, square(250000, 2650000, 250050, 2650050))])
            for name in ("a", "b", "c")
        ]
        self.tab._add_input_files(paths)
        self.assertEqual([row.details for row in self.tab.file_list.rows], [("判別中",)] * 3)
        self._pump_until_idle()
        self.assertTrue(all(row.checked for row in self.tab.file_list.rows), "新加入的檔預設勾著")
        with mock.patch("ui.panel_inspector.messagebox"):
            self._build_boundary_from_text()
        self._set_out_dir(self.out_dir / "skip")
        self.assertEqual(self.tab._readiness(), (True, "可以開始：3 個檔案"))

        self.tab._toggle_file(1)
        self.assertEqual([p.name for p in self.tab.checked_input_files()], ["a.geojson", "c.geojson"])
        self.assertEqual(self.tab.file_list.header_text(), "檔名（已勾選 2／3）")
        self.assertEqual(self.tab._readiness(), (True, "可以開始：2 個檔案（1 個沒勾，會跳過）"))
        with mock.patch("ui.tab_clip_workflow.BatchClipEngine") as engine, mock.patch("ui.tab_clip_workflow.messagebox"):
            engine.return_value.execute_batch.return_value = []
            self.tab._handle_start_batch_clip()
            self._pump_until_idle()
        sent = engine.return_value.execute_batch.call_args[0][0]
        self.assertEqual([p.name for p in sent], ["a.geojson", "c.geojson"])
        self.assertIn("1 個檔沒勾", self._log_text())

        self.tab._set_all_files_checked(False)
        self.assertEqual(self.tab._readiness(), (False, "還沒勾選要處理的檔案"))
        self.assertEqual(self.tab.option_checks["excel"].cget("state"), "disabled", "選項只算有勾的檔")
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box:
            self.tab._handle_start_batch_clip()
            self.assertIn("都沒勾", tab_box.showwarning.call_args[0][1])
        self.assertFalse(self.inspector.is_busy)

        self.tab._set_all_files_checked(True)
        self.assertEqual(len(self.tab.checked_input_files()), 3)
        self.tab._toggle_file(0)
        self.tab._select_file_index(0)
        self._pump_until_idle()
        self.tab._handle_remove_selected()
        self.assertEqual(self.tab.file_list.header_text(), "檔名（已勾選 2／2）", "移除的檔不再算沒勾")

    def test_snap_tolerance_entry_is_validated_and_passed_on(self) -> None:
        self.assertEqual(self.tab.entry_snap_tolerance.get(), "0.5")
        dxf = self.data_dir / "邊界.dxf"
        dxf.write_text("", encoding="utf-8")
        self.tab.entry_bound_file.insert(0, str(dxf))

        def _set_snap(text: str) -> None:
            self.tab.entry_snap_tolerance.delete(0, "end")
            self.tab.entry_snap_tolerance.insert(0, text)

        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch(
            "ui.tab_clip_workflow.dxf_lines_to_polygon_boundary", side_effect=RuntimeError("測試中止")
        ) as engine, mock.patch("ui.panel_inspector.messagebox"):
            for bad in ("-1", "abc", "nan"):
                _set_snap(bad)
                self.tab._handle_dxf_lines_to_poly()
            self.assertEqual(tab_box.showerror.call_count, 3)
            self.assertFalse(self.inspector.is_busy)
            engine.assert_not_called()

            _set_snap("2")
            self.tab._handle_dxf_lines_to_poly()
            self._pump_until_idle()
            self.assertEqual(engine.call_args.kwargs["snap_tolerance"], 2.0)

    def test_dwg_is_rejected_for_inputs_and_boundary(self) -> None:
        dwg = self.data_dir / "plan.dwg"
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box:
            self.assertEqual(self.tab._add_input_files([dwg]), 0)
            self.tab._load_boundary_file(dwg)
            self.assertEqual(tab_box.showwarning.call_count, 2)
            for call in tab_box.showwarning.call_args_list:
                self.assertIn("DXF", call[0][1])
        self.assertEqual(self.tab.selected_input_files, [])
        self.assertEqual(self.tab.entry_bound_file.get(), "")
        self.assertFalse(self.inspector.is_busy)

    def test_batch_clip_end_to_end(self) -> None:
        rows, cols = 40, 20
        west = write_dem(self.data_dir / "dem_west.tif", np.full((rows, cols), 100.0), origin_x=249950, top_y=2650150)
        east = write_dem(self.data_dir / "dem_east.tif", np.full((rows, cols), 120.0), origin_x=250050, top_y=2650150)
        out_dir = self.out_dir / "clip_out"

        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self._build_boundary_from_text()
            self.tab._add_input_files([west, east])
            self._pump_until_idle()
            self._set_out_dir(out_dir)
            # 勾選項預設都不勾；清單有兩幅 DEM，拼接與 3D 縮圖才可勾
            for checkbox in self.tab.option_checks.values():
                self.assertEqual(checkbox.get(), 0)
                self.assertEqual(checkbox.cget("state"), "normal")
                checkbox.select()

            self.tab._handle_start_batch_clip()
            self.assertEqual(self.tab.btn_execute.cget("state"), "disabled")
            self._pump_until_idle(timeout=300.0)

            box.showerror.assert_not_called()
            tab_box.showwarning.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]

        self.assertEqual(self.tab.btn_execute.cget("state"), "normal")
        self.assertIn("失敗: 0 件", message)
        self.assertTrue(list(out_dir.glob("dem_mosaic*.tif")), f"兩幅相鄰 DEM 應拼接成單一成果: {sorted(p.name for p in out_dir.iterdir())}")
        self.assertFalse(list(out_dir.glob("*.kmz")), "DEM 頁沒有 KMZ 選項")
        # 等高線與坡度圖由裁切 (拼接) 後的 DEM 產生，放在成果旁邊
        self.assertEqual(len(list(out_dir.glob("dem_mosaic*_contours.dxf"))), 1)
        self.assertEqual(len(list(out_dir.glob("dem_mosaic*_slope_pct.tif"))), 1)
        self.assertIn("等高線 DXF: 1 個", message)
        self.assertIn("坡度圖: 1 個", message)
        self.assertIn("3D 山體陰影", self.inspector.lbl_preview_caption.cget("text"))
        html = self._open_browser_map()
        self.assertIn("原始：dem_west.tif", html)
        self.assertIn("裁切後：dem_mosaic", html)
        self.assertIn("裁切邊界", html)
        self.assertIn("data:image/png;base64,", html)

    def _open_browser_map(self) -> str:
        """按右欄「瀏覽器套疊航照底圖」，回傳產生的 HTML 內容 (不真的開瀏覽器)。"""
        with mock.patch("ui.tab_clip_workflow.open_preview_in_browser") as opener, mock.patch(
            "ui.panel_inspector.messagebox"
        ) as box:
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
            box.showerror.assert_not_called()
            opener.assert_called_once()
        html_path = opener.call_args[0][0]
        self.assertEqual(self.inspector.current_preview_html, html_path)
        return decode_js_escapes(html_path.read_text(encoding="utf-8"))

    def test_shp_batch_clip_with_kmz(self) -> None:
        self._use_tab(self.app.tab_shp)
        parcels = write_geojson(
            self.data_dir / "parcels.geojson",
            [
                ({"地號": "1"}, square(249950, 2650000, 250050, 2650100)),
                ({"地號": "2"}, square(250050, 2650000, 250150, 2650100)),
            ],
        )
        out_dir = self.out_dir / "shp_out"
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self._build_boundary_from_text()
            self.tab._add_input_files([parcels])
            self._pump_until_idle()
            self._set_out_dir(out_dir)
            self.tab.option_checks["kmz"].select()
            self.tab.option_checks["excel"].select()
            self.tab._handle_start_batch_clip()
            self._pump_until_idle(timeout=300.0)
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
            message = tab_box.showinfo.call_args[0][1]
        self.assertIn("失敗: 0 件", message)
        self.assertTrue((out_dir / "parcels_clipped.shp").exists())
        self.assertTrue(list(out_dir.glob("*.kmz")), "勾選 KMZ 時應一併匯出")
        self.assertIn("KMZ:", message)
        self.assertTrue((out_dir / "parcels_clipped.xlsx").exists())
        self.assertIn("屬性 Excel: 1 個", message)
        html = self._open_browser_map()
        self.assertIn("原始：parcels.geojson", html)
        self.assertIn("裁切後：parcels_clipped.shp", html)
        self.assertIn("裁切邊界", html)

    def test_shp_keep_whole_reaches_the_engine(self) -> None:
        """K5：勾「整筆保留」時碰到範圍的宗地整筆選出，成果是 _whole.shp。"""
        self._use_tab(self.app.tab_shp)
        parcels = write_geojson(
            self.data_dir / "parcels.geojson",
            [
                ({"地號": "1"}, square(249950, 2650000, 250050, 2650100)),
                ({"地號": "2"}, square(250050, 2650000, 250150, 2650100)),
            ],
        )
        out_dir = self.out_dir / "shp_whole"
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self._build_boundary_from_text()
            self.tab._add_input_files([parcels])
            self._pump_until_idle()
            self._set_out_dir(out_dir)
            self.assertEqual(self.tab.option_checks["whole"].cget("state"), "normal")
            self.tab.option_checks["whole"].select()
            self.tab._handle_start_batch_clip()
            self._pump_until_idle(timeout=300.0)
            box.showerror.assert_not_called()
            message = tab_box.showinfo.call_args[0][1]
        self.assertIn("整筆保留", message)
        output = out_dir / "parcels_whole.shp"
        self.assertTrue(output.exists())
        self.assertFalse((out_dir / "parcels_clipped.shp").exists())
        # 邊界 250000～250100：兩筆各有一半在範圍內
        rows = {row["地號"]: row for row in attribute_rows(output)}
        self.assertEqual(sorted(rows), ["1", "2"])
        for row in rows.values():
            self.assertAlmostEqual(row["ORIG_M2"], 10000.0, places=1, msg="整筆不切開")
            self.assertAlmostEqual(row["AREA_M2"], 5000.0, places=1, msg="範圍內面積")
        self.assertAlmostEqual(total_area(output), 20000.0, delta=0.01)

    def test_ortho_resolution_reaches_tif_and_cad_jpeg(self) -> None:
        from tests.test_clip_ortho import write_ortho

        self._use_tab(self.app.tab_ortho)
        self.assertIsNone(self.app.tab_dem.combo_ortho_res, "影像解析度只有正射頁有")
        self.assertEqual(self.tab.combo_ortho_res.get(), "維持原圖解析度")
        ortho = write_ortho(self.data_dir / "空拍.tif")
        out_dir = self.out_dir / "ortho_out"
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self._build_boundary_from_text()
            self.tab._add_input_files([ortho])
            self._pump_until_idle()
            self._set_out_dir(out_dir)

            self.tab.combo_ortho_res.set("abc")
            self.tab._handle_start_batch_clip()
            self.assertIn("影像解析度", tab_box.showerror.call_args[0][1])
            self.assertFalse(self.inspector.is_busy)

            # 單位是公分：原圖 5 m，填 10 (公分) 比原圖細，不准開始
            self.tab.combo_ortho_res.set("10")
            self.tab._handle_start_batch_clip()
            self.assertIn("解析度不能小於原圖 (500 公分)", tab_box.showwarning.call_args[0][1])
            self.assertFalse(self.inspector.is_busy)

            self.tab.combo_ortho_res.set("1000")
            self.tab.option_checks["cad_bg"].select()
            self.tab._handle_start_batch_clip()
            self._pump_until_idle(timeout=300.0)
            box.showerror.assert_not_called()
            tab_box.showinfo.assert_called_once()
        self.assertTrue((out_dir / "空拍_ortho_10m.tif").exists(), sorted(p.name for p in out_dir.iterdir()))
        jgw = out_dir / "空拍_ortho_10m_cad_bg.jgw"
        self.assertAlmostEqual(float(jgw.read_text(encoding="ascii").split()[0]), 10.0, places=6)

    def test_contour_intervals_are_validated_before_running(self) -> None:
        dem = write_dem(self.data_dir / "地形.tif", np.ones((20, 20)))
        self.assertEqual((self.tab.entry_contour_interval.get(), self.tab.entry_index_interval.get()), ("1", "5"))
        self.assertIsNone(self.app.tab_ortho.entry_contour_interval, "等高線間距只有 DEM 頁有")
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox"):
            self._build_boundary_from_text()
            self.tab._add_input_files([dem])
            self._pump_until_idle()
            self.tab.option_checks["contour"].select()
            for minor, major in (("1", "2.5"), ("abc", "5"), ("0", "5")):
                self.tab.entry_contour_interval.delete(0, "end")
                self.tab.entry_contour_interval.insert(0, minor)
                self.tab.entry_index_interval.delete(0, "end")
                self.tab.entry_index_interval.insert(0, major)
                self.tab._handle_start_batch_clip()
                self.assertFalse(self.inspector.is_busy, f"{minor}/{major} 不應開始執行")
            self.assertEqual(tab_box.showerror.call_count, 3)
            self.assertIn("整數倍", tab_box.showerror.call_args_list[0][0][1])

    def test_batch_clip_error_restores_button(self) -> None:
        parcels = write_dem(self.data_dir / "p.tif", np.ones((20, 20)))
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self._build_boundary_from_text()
            self.tab._add_input_files([parcels])
            self._pump_until_idle()

            self.tab.entry_out_dir.delete(0, "end")
            self.tab._handle_start_batch_clip()
            tab_box.showwarning.assert_called_once()
            self.assertIn("輸出資料夾", tab_box.showwarning.call_args[0][1])
            self.assertFalse(self.inspector.is_busy)

            self._set_out_dir(self.out_dir / "boom")
            with mock.patch("ui.tab_clip_workflow.BatchClipEngine", side_effect=RuntimeError("引擎壞了")):
                self.tab._handle_start_batch_clip()
                self.assertEqual(self.tab.btn_execute.cget("state"), "disabled")
                self._pump_until_idle()
            box.showerror.assert_called_once()
            self.assertEqual(box.showerror.call_args[0][0], "批次作業異常")
            tab_box.showinfo.assert_not_called()
        self.assertEqual(self.tab.btn_execute.cget("state"), "normal")
        self.assertEqual(self.tab.btn_execute.cget("text"), "開始裁切")

    def test_dem_resolution_field_reaches_engine(self) -> None:
        self.assertEqual(self.tab.combo_dem_res.get(), "1", "DEM 解析度預設 1 m")
        # 原圖 0.25 m：下面填的數字都不比原圖細
        parcels = write_dem(self.data_dir / "p.tif", np.ones((20, 20)), cell_size=0.25)
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox") as box:
            self._build_boundary_from_text()
            self.tab._add_input_files([parcels])
            self._pump_until_idle()
            self._set_out_dir(self.out_dir / "res_out")

            with mock.patch("ui.tab_clip_workflow.BatchClipEngine") as engine_cls:
                engine_cls.return_value.execute_batch.return_value = []
                for bad in ("abc", "0", "-1"):
                    self.tab.combo_dem_res.set(bad)
                    self.tab._handle_start_batch_clip()
                self.assertEqual(tab_box.showerror.call_count, 3)
                engine_cls.assert_not_called()

                used = []
                for text in ("0.5", "維持原圖解析度", "2.5 m"):
                    self.tab.combo_dem_res.set(text)
                    self.tab._handle_start_batch_clip()
                    self._pump_until_idle()
                    used.append(engine_cls.call_args[1]["dem_target_resolution"])
            box.showerror.assert_not_called()

        self.assertEqual(used, [0.5, None, 2.5])
        kwargs = engine_cls.call_args[1]
        self.assertEqual(kwargs["target_srs"], "EPSG:3826")
        self.assertEqual(kwargs["dem_resampling"], "bilinear")

    def test_resolution_hint_reads_the_original_and_blocks_finer_values(self) -> None:
        """K2：說明寫原圖解析度；比原圖細時「開始裁切」停用；預設 1 m 比原圖細時改成維持原圖。"""
        self.assertEqual(self.tab.lbl_resolution_hint.cget("text"), "可自行填寫數值 (公尺)。")
        coarse = write_dem(self.data_dir / "粗.tif", np.ones((20, 20)), cell_size=5.0)
        fine = write_dem(self.data_dir / "細.tif", np.ones((20, 20)), cell_size=0.5)
        with mock.patch("ui.tab_clip_workflow.messagebox"), mock.patch("ui.panel_inspector.messagebox"):
            self._build_boundary_from_text()
            self.tab._add_input_files([fine])
            self._pump_until_idle()
            self.assertEqual(self.tab.combo_dem_res.get(), "1", "原圖比預設細時不動")
            self.assertEqual(self.tab.lbl_resolution_hint.cget("text"), "原圖 0.5 m；可自行填寫 0.5 以上的數值。")
            self.tab._add_input_files([coarse])
            self._pump_until_idle()
        self.assertEqual(self.tab.combo_dem_res.get(), "維持原圖解析度")
        self.assertIn("原圖比預設的 1 m 粗", self._log_text())
        self.assertEqual(self.tab.lbl_resolution_hint.cget("text"), "原圖 0.5～5 m；可自行填寫 5 以上的數值。")
        self.tab.combo_dem_res.set("2")
        self.tab._refresh_ready()
        self.assertEqual(self.tab.btn_execute.cget("state"), "disabled")
        self.assertIn("解析度不能小於原圖 (5 公尺)", self.tab.action_bar.lbl_status.cget("text"))
        # 取消勾選粗的那個：以勾選的檔為準
        self.tab._toggle_file(self.tab.selected_input_files.index(coarse.resolve()))
        self.assertEqual(self.tab.btn_execute.cget("state"), "normal")
        self.assertEqual(self.tab.lbl_resolution_hint.cget("text"), "原圖 0.5 m；可自行填寫 0.5 以上的數值。")
        self.tab.combo_dem_res.set("0.1")
        self.tab._refresh_ready()
        self.assertEqual(self.tab.btn_execute.cget("state"), "disabled")

    def test_ortho_page_takes_images_with_world_files_in_centimetres(self) -> None:
        """K2：正射頁收附世界檔的 JPG／PNG，解析度單位是公分；沒有世界檔的照片移出或略過並說原因。"""
        self._use_tab(self.app.tab_ortho)
        self.assertEqual(self.tab.lbl_resolution_hint.cget("text")[:13], "可自行填寫數值 (公分)。")
        folder = self.data_dir / "照片與底圖"
        folder.mkdir()
        image = write_world_image(folder / "底圖.jpg", pixel=0.05)
        write_world_image(folder / "現場.jpg", pixel=None)
        photo = write_world_image(self.data_dir / "單張照片.png", pixel=None)
        with mock.patch("ui.tab_clip_workflow.messagebox") as tab_box, mock.patch("ui.panel_inspector.messagebox"):
            self.assertEqual(self.tab._add_input_folder(folder), 1)
            self.tab._add_input_files([photo])
            self._pump_until_idle()
        self.assertIn("另有 1 個 JPG／PNG 沒有世界檔 (沒有座標)，已略過", self._log_text())
        self.assertIn("單張照片.png：沒有世界檔 (.jgw／.pgw)，沒有座標，不能裁切", tab_box.showwarning.call_args[0][1])
        self.assertEqual(self.tab.selected_input_files, [image.resolve()])
        self.assertTrue(self.tab.lbl_resolution_hint.cget("text").startswith("原圖 5 公分；可自行填寫 5 以上的數值。"))
        self.tab.combo_ortho_res.set("4")
        self.tab._refresh_ready()
        self.assertIn("解析度不能小於原圖 (5 公分)", self.tab.action_bar.lbl_status.cget("text"))

    def test_recipe_bar_and_inspector_kmz_button_are_gone(self) -> None:
        self.assertFalse(hasattr(self.tab, "opt_recipes"))
        self.assertFalse(hasattr(self.tab, "btn_save_recipe"))
        self.assertFalse(hasattr(self.inspector, "btn_kmz"))


class TestParseDemResolution(unittest.TestCase):
    def test_numbers_units_and_keep_original(self) -> None:
        self.assertIsNone(parse_resolution("維持原圖解析度"))
        self.assertEqual(parse_resolution(" 0.5 "), 0.5)
        self.assertEqual(parse_resolution("1 m"), 1.0)
        self.assertEqual(parse_resolution("2公尺"), 2.0)
        # 正射頁的單位是公分 (1 單位 0.01 m)；寫了單位照寫的
        self.assertAlmostEqual(parse_resolution("10", 0.01), 0.1)
        self.assertAlmostEqual(parse_resolution("25 公分", 1.0), 0.25)
        self.assertAlmostEqual(parse_resolution("5cm", 1.0), 0.05)
        self.assertAlmostEqual(parse_resolution("0.5 m", 0.01), 0.5)
        for bad in ("", "abc", "0", "-3", "nan", "inf", "公分"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_resolution(bad)

    def test_format_in_page_units(self) -> None:
        self.assertEqual(format_resolution(1.0, 1.0), "1")
        self.assertEqual(format_resolution(0.02634, 0.01), "2.63")
        self.assertEqual(format_resolution(12.5, 0.01), "1250")
        self.assertEqual(format_resolution(30.0, 1.0), "30")


if __name__ == "__main__":
    unittest.main()
