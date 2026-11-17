"""圖資快覽 (ui/tab_quick_view.py)。

H1 清單與座標系：
1. 開程式時停在圖資快覽；右欄預覽標題跟著頁面換。
2. 加入資料夾：判別類型 (向量、CAD、DEM)、原始座標系 (有標籤寫短名、沒有寫「無標籤」)、
   來源座標系 (依原始標籤；沒有標籤時寫推測的座標系)；第一個判別完的自動選取，右欄顯示它的體檢與縮圖。
3. 清單下面的選單改選取那一列的來源座標系 (有標籤的檔也照選的)，沒選取時停用。
4. DXF 列出 CAD 圖層，可以各自勾選；判別不出類型的檔與 DWG 移出清單並提示；拖放、移除、清空。
H2 瀏覽器地圖：
5. 底部「在瀏覽器開啟地圖」跟著清單停用／啟用並寫出現況 (需指派、沒勾 CAD 圖層的會跳過)。
6. 每列照自己的來源座標系、DXF 只放勾選的 CAD 圖層；右欄的瀏覽器按鈕同一件事；內容沒變直接開上次的。
H3 另存 SHP：
7. 底部次要按鈕「另存勾選的向量圖層為 SHP」與卡片 2 的現況跟著清單與輸出位置停用／啟用 (DEM 不算、需指派的會跳過)；
   作業執行中停用。裁切工具的動作列沒有次要按鈕。
8. 照各列的來源座標系、卡片 2 選的座標系、編碼與封閉線轉面存檔；完成時列出沒有存的圖層與原因。
H4 與 CSV-2：
9. 不支援的副檔名提示、附屬檔不提示；點兩下開的檔切到這頁、選取並捲到那一列。
10. 座標表 (.csv／.txt)：資料夾裡只收像座標表的 (其他不提示)；類型「座標表」、無標籤依範圍推測；
    地圖與另存 SHP 用暫存點位、名稱照原本的檔名；單獨加入的不是座標表時提示原因。
K4 另存 KML／KMZ：
11. 卡片 2 右上選格式：KML／KMZ 時座標系與編碼選單變灰、顯示固定值並寫原因，按鈕字跟著換；切回 SHP 還原原本選的。
12. 每個來源檔存一個 KMZ，DXF 勾選的 CAD 圖層各一個資料夾；完成訊息寫 WGS84、UTF-8。
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core import geo_io, quick_view_export, quick_view_map
from core.env_runner import run_command
from tests._support import (
    SAMPLE_KML_LAYERS,
    IsolatedWorkspaceTestCase,
    convert_vector,
    line,
    square,
    strip_prj,
    write_dem,
    write_geojson,
    write_gpx,
    write_kml,
    write_world_image,
)
from ui.panel_inspector import PREVIEW_TITLE_CLIP, PREVIEW_TITLE_VIEW
from ui.srs_options import BY_LABEL_SOURCE_SRS_LABEL, output_srs_labels, view_source_srs_label, view_source_srs_labels
from ui.tab_quick_view import ENCODING_CHOICES, EXPORT_BUTTON_TEXT, KML_SRS_LABEL, MAP_BUTTON_TEXT, VIEW_OUTPUT_FOLDER_NAME
from ui.task_status import TaskStatus

PARCEL = [({"地號": "0001"}, square(250000, 2650000, 250050, 2650050))]


class TestQuickView(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp()
        self.app.update()
        self.tab = self.app.tab_view
        self.inspector = self.app.inspector_panel

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy and self.tab._light_pending == 0:
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _write_dxf(self, path: Path) -> Path:
        """CAD 圖層「中心線」「路權線」「文字」的 DXF，存成臺灣 AutoCAD 常見的 Big5 (ANSI_950)。"""
        source = self.tmp_dir / "dxf_source.geojson"
        features = [
            {"type": "Feature", "properties": {"Layer": name}, "geometry": line((250000 + i * 10, 2650000), (250100, 2650060))}
            for i, name in enumerate(("中心線", "路權線", "文字"))
        ]
        source.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
        run_command(["ogr2ogr", "-f", "DXF", str(path), str(source)])
        text = path.read_text(encoding="utf-8")
        path.write_bytes(text.replace("ANSI_1252", "ANSI_950").encode("cp950"))
        return path

    def _sample_folder(self) -> Path:
        folder = self.data_dir / "範例圖資"
        (folder / "子資料夾").mkdir(parents=True)
        convert_vector(write_geojson(self.tmp_dir / "a.geojson", PARCEL), folder / "宗地.shp")
        convert_vector(write_geojson(self.tmp_dir / "b.geojson", PARCEL, epsg=3828), folder / "舊圖.shp")
        strip_prj(convert_vector(write_geojson(self.tmp_dir / "c.geojson", PARCEL), folder / "無標籤.shp"))
        write_dem(folder / "子資料夾" / "地形.tif", np.ones((20, 20)))
        self._write_dxf(folder / "路線.dxf")
        (folder / "說明.txt").write_text("不是圖資", encoding="utf-8")
        return folder

    def _item(self, name: str):  # type: ignore[no-untyped-def]
        return next(item for item in self.tab.items if item.path.name == name)

    def test_app_starts_on_quick_view(self) -> None:
        self.assertEqual(self.app.current_tool, "view")
        self.assertTrue(self.tab.is_active)
        self.assertTrue(self.tab.winfo_ismapped())
        self.assertEqual(self.inspector.lbl_preview_title.cget("text"), PREVIEW_TITLE_VIEW)
        self.app.select_tool("dem")
        self.assertEqual(self.inspector.lbl_preview_title.cget("text"), PREVIEW_TITLE_CLIP)
        self.assertFalse(self.tab.is_active)
        self.assertEqual(self.tab.opt_source_srs.cget("state"), "disabled", "沒選取時不能改來源座標系")

    def test_folder_shows_kind_and_coordinate_systems(self) -> None:
        self.assertEqual(self.tab.add_paths([self._sample_folder()]), 5, "說明.txt 不收")
        self.assertEqual({row.details[2] for row in self.tab.file_list.rows}, {"判別中"})
        self._pump_until_idle()

        expected = {
            "宗地.shp": ("向量", "TWD97 121", "依原始標籤"),
            "舊圖.shp": ("向量", "TWD67 121", "依原始標籤"),
            "無標籤.shp": ("向量", "無標籤", "推測 TWD97 121"),
            "地形.tif": ("DEM", "TWD97 121", "依原始標籤"),
            "路線.dxf": ("CAD", "無標籤", "推測 TWD97 121"),
        }
        rows = {row.text: row.details for row in self.tab.file_list.rows}
        self.assertEqual(rows, expected)
        self.assertEqual(self._item("路線.dxf").layers, ("中心線", "文字", "路權線"))
        self.assertEqual(self.tab.file_list.header_text(), "檔名（已勾選 5／5）")
        # 第一個判別完的自動選取：右欄是它的體檢與縮圖
        first = self.tab.items[0]
        self.assertEqual(self.tab.selected_item(), first)
        self.assertEqual(self.inspector.diag_value_labels["來源圖資:"].cget("text"), first.path.name)
        self.assertEqual(self.inspector.lbl_preview_caption.cget("text"), first.path.name)
        self.assertEqual(self.tab.add_paths([self.data_dir / "範例圖資" / "宗地.shp"]), 0, "重複的不再加入")

    def test_source_srs_applies_to_the_selected_row(self) -> None:
        self.tab.add_paths([self._sample_folder()])
        self._pump_until_idle()
        labelled = self.tab.items.index(self._item("宗地.shp"))
        unlabelled = self.tab.items.index(self._item("無標籤.shp"))

        self.tab._select_index(labelled)
        self._pump_until_idle()
        self.assertEqual(self.tab.opt_source_srs.cget("state"), "normal")
        self.assertEqual(self.tab.opt_source_srs.get(), BY_LABEL_SOURCE_SRS_LABEL)
        twd67 = next(label for label in view_source_srs_labels() if label.startswith("TWD67 121"))
        self.tab.opt_source_srs.set(twd67)
        self.tab._handle_source_srs_chosen(twd67)
        self.assertEqual(self._item("宗地.shp").source_srs, "TWD67_121", "有標籤的檔也照選的")
        self.assertEqual(self.tab.file_list.rows[labelled].details[2], "TWD67 121")
        self.assertIn("來源座標系改為", self.inspector.txt_terminal.get("1.0", "end"))

        self.tab._select_index(unlabelled)
        self._pump_until_idle()
        self.assertEqual(self.tab.opt_source_srs.get(), BY_LABEL_SOURCE_SRS_LABEL, "選單換成那一列自己的設定")
        self.tab._select_index(labelled)
        self.assertEqual(self.tab.opt_source_srs.get(), twd67)
        self.tab._handle_source_srs_chosen(BY_LABEL_SOURCE_SRS_LABEL)
        self.assertIsNone(self._item("宗地.shp").source_srs)

    def test_dxf_layers_and_checkboxes(self) -> None:
        self.tab.add_paths([self._write_dxf(self.data_dir / "路線.dxf")])
        self._pump_until_idle()
        self.assertEqual([child.text for child in self.tab.file_list.rows[0].children], ["中心線", "文字", "路權線"])
        self.tab._toggle_layer(0, 2)
        item = self._item("路線.dxf")
        self.assertEqual(item.checked_layers(), ["中心線", "文字"])
        self.assertFalse(self.tab.file_list.rows[0].children[2].checked)
        self.tab._toggle_item(0)
        self.assertEqual(self.tab.checked_items(), [])
        self.tab._set_all_checked(True)
        self.assertEqual(self.tab.checked_items(), [item])
        self.assertEqual(item.checked_layers(), ["中心線", "文字"], "全勾不改 CAD 圖層的勾選")

    def test_unknown_files_and_dwg_are_rejected(self) -> None:
        broken = self.data_dir / "壞掉.tif"
        broken.write_bytes(b"not a tiff")
        dwg = self.data_dir / "平面圖.dwg"
        dwg.write_bytes(b"")
        with mock.patch("ui.tab_quick_view.messagebox") as box:
            self.assertEqual(self.tab.add_paths([dwg]), 0)
            self.assertIn("DXF", box.showwarning.call_args[0][1])
            self.tab.add_paths([broken])
            self._pump_until_idle()
            self.assertIn("壞掉.tif：讀不到圖資內容", box.showwarning.call_args[0][1])
        self.assertEqual(self.tab.items, [])
        self.assertIsNone(self.tab.selected_item())

    def test_kml_folders_and_gpx_layers_are_rows_like_cad_layers(self) -> None:
        """格式支援 K1：多資料夾的 KML／KMZ、GPX 可以展開，勾選的圖層各自上地圖；只有一個圖層的不展開。"""
        kmz = write_kml(self.data_dir / "範圍.kmz")
        gpx = write_gpx(self.data_dir / "巡查.gpx")
        single = convert_vector(write_geojson(self.tmp_dir / "a.geojson", PARCEL), self.data_dir / "單層.gpkg")
        self.tab.add_paths([kmz, gpx, single])
        self._pump_until_idle()
        self.assertEqual([item.kind for item in self.tab.items], ["向量", "向量", "向量"])
        rows = self.tab.file_list.rows
        self.assertEqual([child.text for child in rows[0].children], list(SAMPLE_KML_LAYERS))
        self.assertEqual([child.text for child in rows[1].children], ["waypoints", "tracks"])
        self.assertEqual(rows[2].children, ())
        self.assertEqual(rows[0].details[1], "WGS84 經緯度", "KML 一律是經緯度")
        self.tab._toggle_layer(0, 0)

        build = mock.Mock(wraps=quick_view_map.build_quick_view_map)
        with mock.patch("ui.tab_quick_view.build_quick_view_map", build), mock.patch(
            "ui.tab_quick_view.open_preview_in_browser"
        ), mock.patch("ui.tab_quick_view.messagebox") as box, mock.patch("ui.panel_inspector.messagebox") as box2:
            self.tab.btn_open_map.invoke()
            self._pump_until_idle()
            box.showwarning.assert_not_called()
            box2.showerror.assert_not_called()
        sources = {source.path.name: source for source in build.call_args.args[1]}
        self.assertEqual(sources["範圍.kmz"].data_layers, SAMPLE_KML_LAYERS[1:])
        self.assertEqual(sources["範圍.kmz"].cad_layers, ())
        self.assertEqual(sources["巡查.gpx"].data_layers, ("waypoints", "tracks"))
        self.assertEqual(sources["單層.gpkg"].data_layers, ())
        # 全部取消勾選的檔跳過並寫原因
        for index in range(1, len(SAMPLE_KML_LAYERS)):
            self.tab._toggle_layer(0, index)
        source, reason = self._item("範圍.kmz").map_source()
        self.assertIsNone(source)
        self.assertEqual(reason, "沒有勾選圖層")

    def test_images_need_a_world_file(self) -> None:
        """K2：附世界檔的 JPG／PNG 是正射；資料夾裡沒有世界檔的照片不提示，單獨加入的說沒有座標。"""
        folder = self.data_dir / "底圖"
        folder.mkdir()
        write_world_image(folder / "底圖.png", pixel=0.25)
        write_world_image(folder / "現場.jpg", pixel=None)
        photo = write_world_image(self.data_dir / "單張.jpg", pixel=None)
        with mock.patch("ui.tab_quick_view.messagebox") as box:
            self.assertEqual(self.tab.add_paths([folder]), 1)
            self.tab.add_paths([photo, folder / "底圖.pgw"])
            self._pump_until_idle()
        self.assertEqual([(item.path.name, item.kind) for item in self.tab.items], [("底圖.png", "正射")])
        self.assertIn("另有 1 個 JPG／PNG 沒有世界檔 (沒有座標)，已略過", self.inspector.txt_terminal.get("1.0", "end"))
        self.assertIn("單張.jpg：沒有世界檔 (.jgw／.pgw)，沒有座標，不能疊圖", self.inspector.txt_terminal.get("1.0", "end"))
        self.assertNotIn("底圖.pgw", str(box.showwarning.call_args_list), "世界檔是附屬檔，不提示")

    def test_other_file_types_are_rejected_but_sidecars_are_quiet(self) -> None:
        shp = convert_vector(write_geojson(self.tmp_dir / "a.geojson", PARCEL), self.data_dir / "宗地.shp")
        report = self.data_dir / "報告.docx"
        report.write_bytes(b"")
        with mock.patch("ui.tab_quick_view.messagebox") as box:
            self.assertEqual(self.tab.add_paths([shp.with_suffix(".dbf"), shp.with_suffix(".prj"), shp.with_suffix(".shx")]), 0)
            box.showwarning.assert_not_called()
            self.assertEqual(self.tab.add_paths([report, shp]), 1)
            self._pump_until_idle()
        self.assertEqual(box.showwarning.call_count, 1, "附屬檔不提示，docx 提示一次")
        self.assertEqual(box.showwarning.call_args[0][0], "不支援的檔案")
        self.assertIn("報告.docx", box.showwarning.call_args[0][1])
        self.assertNotIn(".dbf", box.showwarning.call_args[0][1])
        self.assertEqual([item.path.name for item in self.tab.items], ["宗地.shp"])

    # -------------------------------------------------------------------------
    # H4：命令列與後開的那一份交來的檔案
    # -------------------------------------------------------------------------
    def test_open_paths_switches_to_quick_view_and_adds_files(self) -> None:
        shp = convert_vector(write_geojson(self.tmp_dir / "a.geojson", PARCEL), self.data_dir / "宗地.shp")
        self.assertTrue(self.app.ready, "主視窗畫好後才收")
        self.app.select_tool("dem")
        self.app.open_paths([])
        self.assertEqual(self.app.current_tool, "dem", "沒帶檔案只叫到前面，不換頁")
        self.app.open_paths([str(shp)])
        self.assertEqual(self.app.current_tool, "view")
        self._pump_until_idle()
        self.assertEqual([item.path for item in self.tab.items], [shp.resolve()])

    def test_opened_file_is_selected_and_scrolled_into_view(self) -> None:
        geojson = write_geojson(self.tmp_dir / "a.geojson", PARCEL)
        listed = [convert_vector(geojson, self.data_dir / f"宗地{i}.shp") for i in range(8)]
        opened = convert_vector(geojson, self.tmp_dir / "點兩下.shp")
        self.tab.add_paths(listed)
        self._pump_until_idle()
        self.assertEqual(self.tab._selected_index, 0)
        self.app.open_paths([str(opened)])
        self._pump_until_idle()
        tree = self.tab.file_list.tree
        self.assertEqual(self.tab.selected_item().path, opened.resolve(), "點兩下開的檔選起來 (原本選著別列也一樣)")
        self.app.update()
        self.assertTrue(tree.bbox("8"), "捲到那一列 (清單只顯示 6 列)")
        self.assertFalse(tree.bbox("0"))
        # 已經在清單裡的檔再點兩下：選它並捲過去
        self.app.open_paths([str(listed[0])])
        self.app.update()
        self.assertEqual(self.tab._selected_index, 0)
        self.assertTrue(tree.bbox("0"))
        # 拖放、加入檔案不換選取
        self.tab.add_paths([convert_vector(geojson, self.tmp_dir / "拖進來.shp")])
        self._pump_until_idle()
        self.assertEqual(self.tab._selected_index, 0)

    def test_paths_before_the_window_is_ready_wait(self) -> None:
        shp = convert_vector(write_geojson(self.tmp_dir / "a.geojson", PARCEL), self.data_dir / "宗地.shp")
        self.app.ready = False
        self.app.open_paths([str(shp)])
        self.app.update()
        self.assertEqual(self.tab.items, [], "還沒畫好 (啟動畫面還在) 先不加，提示才不會被蓋住")
        self.app._notify_launcher_ready()
        self.app.update()
        self._pump_until_idle()
        self.assertEqual([item.path for item in self.tab.items], [shp.resolve()])

    def test_handed_files_restore_a_minimized_window(self) -> None:
        self.app.iconify()
        self.app.update()
        self.app.open_paths([])
        self.app.update()
        self.assertEqual(self.app.state(), "normal")
        self.assertEqual(self.app.attributes("-topmost"), 0, "最上層只是暫時的")

    # -------------------------------------------------------------------------
    # CSV-2：座標表
    # -------------------------------------------------------------------------
    def _table_folder(self) -> Path:
        folder = self.data_dir / "測量成果"
        folder.mkdir()
        (folder / "樁位.csv").write_bytes("點號,X,Y,備註\n1,250000,2650000,界樁\n2,250100,2650100,水準點\n".encode("cp950"))
        (folder / "導線點.txt").write_text("點號\tE\tN\nA1\t250050\t2650050\n", encoding="utf-8")
        (folder / "說明.txt").write_text("112 年測量成果，座標見附表。\n", encoding="utf-8")
        (folder / "清冊.csv").write_text("地號,面積\n0001,12.5\n", encoding="utf-8")
        return folder

    def test_folder_keeps_coordinate_tables_and_quietly_skips_other_text(self) -> None:
        with mock.patch("ui.tab_quick_view.messagebox") as box:
            self.assertEqual(self.tab.add_paths([self._table_folder()]), 2)
            self._pump_until_idle()
        box.showwarning.assert_not_called()
        self.assertEqual([item.path.name for item in self.tab.items], ["導線點.txt", "樁位.csv"])
        self.assertIn("另有 2 個 CSV／TXT 不是座標表", self.inspector.txt_terminal.get("1.0", "end"))
        item = self.tab.items[1]
        self.assertEqual(item.kind, "座標表")
        self.assertEqual(item.original_srs_text(), "無標籤")
        self.assertEqual(item.source_srs_text(), "推測 TWD97 121")
        self.assertTrue(item.data_path.is_file())
        self.assertTrue(item.data_path.is_relative_to(self.work_temp / "quick_view_csv"))
        self.tab._select_index(1)
        self._pump_until_idle()
        self.assertIsNotNone(self.inspector.current_preview_image, "縮圖用暫存點位畫")

    def test_table_goes_to_map_and_shapefile_under_its_own_name(self) -> None:
        self.tab.add_paths([self._table_folder() / "樁位.csv"])
        self._pump_until_idle()
        item = self.tab.items[0]
        map_source, reason = item.map_source()
        self.assertEqual(reason, "")
        result = quick_view_map.build_quick_view_map(self.out_dir / "map.html", [map_source])
        self.assertEqual(result.layer_names, ["樁位.csv"])
        export_source, _ = item.export_source()
        messages: list = []
        exported = quick_view_export.export_to_shapefiles([export_source], self.out_dir, encoding="CP950", text_callback=messages.append)
        self.assertEqual([p.name for p in exported.output_files], ["樁位.shp"])
        log = "".join(messages)
        self.assertIn("樁位.csv：來源沒有座標系標籤", log, "處理紀錄寫原本的檔名")
        self.assertNotIn("樁位.gpkg", log)
        info = geo_io.read_layer_info(exported.output_files[0])
        self.assertEqual(info.epsg, 3826)
        rows = geo_io.read_attribute_rows(exported.output_files[0])
        self.assertEqual([(row["點號"], row["X"], row["備註"]) for row in rows], [("1", 250000.0, "界樁"), ("2", 250100.0, "水準點")])

    def test_single_table_that_is_not_coordinates_explains_why(self) -> None:
        folder = self._table_folder()
        with mock.patch("ui.tab_quick_view.messagebox") as box:
            self.assertEqual(self.tab.add_paths([folder / "清冊.csv", folder / "樁位.csv"]), 2)
            self._pump_until_idle()
        self.assertIn("清冊.csv：不是座標表", box.showwarning.call_args[0][1])
        self.assertEqual([item.path.name for item in self.tab.items], ["樁位.csv"])

    def test_drop_remove_and_clear(self) -> None:
        from tests.test_ui_drag_drop import simulate_drop

        folder = self._sample_folder()
        self.assertEqual(simulate_drop(self.tab.file_list.tree, folder), "copy")
        self._pump_until_idle()
        self.assertEqual(len(self.tab.items), 5)

        self.tab._select_index(None)
        self.tab._handle_remove_selected()
        self.assertEqual(len(self.tab.items), 5, "沒選取時不移除")
        self.tab._select_index(1)
        removed = self.tab.items[1]
        self.tab._handle_remove_selected()
        self.assertNotIn(removed, self.tab.items)
        self.assertIsNone(self.tab.selected_item())
        self.tab._handle_clear()
        self._pump_until_idle()
        self.assertEqual(self.tab.file_list.rows, [])
        self.assertEqual(self.tab.lbl_file_count.cget("text"), "共 0 個檔案")
        self.assertIsNone(self.inspector.current_preview_image)

    # -------------------------------------------------------------------------
    # H2：在瀏覽器開啟地圖
    # -------------------------------------------------------------------------
    def _status(self) -> str:
        return self.tab.action_bar.lbl_status.cget("text")

    def _write_local_shp(self) -> Path:
        """沒有標籤、座標不在臺灣範圍 (自訂原點的區域座標)：推測不出來，清單寫「需指派」。"""
        shp = convert_vector(
            write_geojson(self.tmp_dir / "local.geojson", [({"n": 1}, square(10, 10, 60, 60))]), self.data_dir / "區域座標.shp"
        )
        return strip_prj(shp)

    def test_map_button_follows_the_list(self) -> None:
        bar = self.tab.action_bar
        self.assertEqual(self.tab.btn_open_map.cget("text"), MAP_BUTTON_TEXT)
        self.assertFalse(bar.ready)
        self.assertEqual(self._status(), "還沒加入圖資")
        self.tab.add_paths([self._sample_folder(), self._write_local_shp()])
        self.assertEqual(self._status(), "正在判別圖資")
        self._pump_until_idle()
        self.assertEqual(self._item("區域座標.shp").source_srs_text(), "需指派")
        self.assertTrue(bar.ready)
        self.assertEqual(self._status(), "可以開地圖：5 個圖資（1 個需指派來源座標系，會跳過）")

        dxf = self.tab.items.index(self._item("路線.dxf"))
        for layer_index in range(3):
            self.tab._toggle_layer(dxf, layer_index)
        self.assertEqual(self._status(), "可以開地圖：4 個圖資（2 個會跳過）")

        self.tab._set_all_checked(False)
        self.assertFalse(bar.ready)
        self.assertEqual(self._status(), "還沒勾選要看的圖資")
        local = self.tab.items.index(self._item("區域座標.shp"))
        self.tab._toggle_item(local)
        self.assertEqual(self._status(), "勾選的圖資都放不上地圖（需指派來源座標系）")
        self.tab._select_index(local)
        self.tab._handle_source_srs_chosen(view_source_srs_label("TWD97_121"))
        self.assertEqual(self._status(), "可以開地圖：1 個圖資")

    def test_open_map_uses_each_rows_source_srs_and_reuses_the_map(self) -> None:
        self.tab.add_paths([self._sample_folder(), self._write_local_shp()])
        self._pump_until_idle()
        dxf = self.tab.items.index(self._item("路線.dxf"))
        self.tab._toggle_layer(dxf, 2)
        self.tab._select_index(self.tab.items.index(self._item("舊圖.shp")))
        self.tab._handle_source_srs_chosen(view_source_srs_label("TWD97_121"))

        build = mock.Mock(wraps=quick_view_map.build_quick_view_map)
        with mock.patch("ui.tab_quick_view.build_quick_view_map", build), mock.patch(
            "ui.tab_quick_view.open_preview_in_browser"
        ) as opener, mock.patch("ui.tab_quick_view.messagebox") as box, mock.patch("ui.panel_inspector.messagebox") as box2:
            self.tab.btn_open_map.invoke()
            self._pump_until_idle()
            box.showwarning.assert_not_called()
            box2.showerror.assert_not_called()
            self.assertEqual(build.call_count, 1)
            sources = {source.path.name: source for source in build.call_args.args[1]}
            self.assertNotIn("區域座標.shp", sources, "需指派的跳過")
            self.assertEqual(sources["舊圖.shp"].source_srs, "TWD97_121", "照那一列選的")
            self.assertIsNone(sources["宗地.shp"].source_srs)
            self.assertEqual(sources["路線.dxf"].cad_layers, ("中心線", "文字"))
            html = opener.call_args.args[0]
            self.assertEqual(self.inspector.current_preview_html, html)
            self.assertIn("區域座標.shp 跳過：需指派來源座標系", self.inspector.txt_terminal.get("1.0", "end"))

            # 右欄「瀏覽器套疊航照底圖」：沒變就直接開上次的
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
            self.assertEqual(build.call_count, 1)
            self.assertEqual(opener.call_count, 2)

            # 改了勾選就重做
            self.tab._toggle_layer(dxf, 2)
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
            self.assertEqual(build.call_count, 2)
            self.assertEqual(build.call_args.args[1][-1].cad_layers, ("中心線", "文字", "路權線"))

        # 切到裁切工具再回來：右欄的地圖還是這一頁的
        self.app.select_tool("dem")
        self.app.select_tool("view")
        self.assertEqual(self.inspector.current_preview_html, html)

    # -------------------------------------------------------------------------
    # H3：另存 SHP
    # -------------------------------------------------------------------------
    def _export_status(self) -> str:
        return self.tab.lbl_export_status.cget("text")

    def _export_enabled(self) -> bool:
        return self.tab.btn_export.cget("state") == "normal"

    def test_export_button_follows_the_list_and_output_folder(self) -> None:
        bar = self.tab.action_bar
        self.assertEqual(self.tab.btn_export.cget("text"), EXPORT_BUTTON_TEXT)
        self.assertEqual(bar.output_text(), str(self.default_output_dir / VIEW_OUTPUT_FOLDER_NAME))
        self.assertIsNone(self.app.tab_dem.action_bar.btn_secondary, "裁切工具沒有次要按鈕")
        self.assertFalse(self._export_enabled())
        self.assertEqual(self._export_status(), "還沒加入圖資")

        self.tab.add_paths([self._sample_folder(), self._write_local_shp()])
        self._pump_until_idle()
        self.assertTrue(self._export_enabled())
        # 宗地、舊圖、無標籤、路線.dxf 4 個；地形.tif 不算；區域座標.shp 需指派
        self.assertEqual(self._export_status(), "可以另存：4 個向量圖資（1 個需指派來源座標系，會跳過）")

        bar.set_output("")
        self.assertFalse(self._export_enabled())
        self.assertEqual(self._export_status(), "還沒指定輸出資料夾（底部「輸出到」）")
        self.assertTrue(bar.ready, "開地圖不需要輸出資料夾")
        bar.set_output(str(self.out_dir))
        self.assertTrue(self._export_enabled())

        self.tab._set_all_checked(False)
        self.tab._toggle_item(self.tab.items.index(self._item("地形.tif")))
        self.assertFalse(self._export_enabled())
        self.assertEqual(self._export_status(), "還沒勾選向量圖資（DEM／正射不能另存）")
        self.assertTrue(bar.ready, "DEM 可以開地圖")
        self.tab._toggle_item(self.tab.items.index(self._item("區域座標.shp")))
        self.assertEqual(self._export_status(), "勾選的向量圖資都不能另存（需指派來源座標系）")
        self.tab._toggle_item(self.tab.items.index(self._item("宗地.shp")))
        self.assertTrue(self._export_enabled())

        # 作業執行中停用，結束後恢復
        bar.apply_task_status(TaskStatus("running", "另存 SHP"))
        self.assertFalse(self._export_enabled())
        self.tab._refresh_ready()
        self.assertFalse(self._export_enabled(), "執行中清單變了也不能按")
        bar.apply_task_status(TaskStatus("done", "另存 SHP"))
        self.assertTrue(self._export_enabled())

    def test_export_uses_each_rows_source_srs_and_the_card_settings(self) -> None:
        self.tab.add_paths([self._sample_folder(), self._write_local_shp()])
        self._pump_until_idle()
        dxf = self.tab.items.index(self._item("路線.dxf"))
        self.tab._toggle_layer(dxf, 2)
        self.tab._select_index(self.tab.items.index(self._item("舊圖.shp")))
        self.tab._handle_source_srs_chosen(view_source_srs_label("TWD97_121"))
        self.tab.action_bar.set_output(str(self.out_dir))
        twd67 = next(label for label in output_srs_labels() if label.startswith("TWD67 121"))
        self.tab.opt_output_srs.set(twd67)
        self.tab.opt_encoding.set(ENCODING_CHOICES[1][0])
        self.tab.chk_closed_polygons.select()

        export = mock.Mock(wraps=quick_view_export.export_to_shapefiles)
        with mock.patch("ui.tab_quick_view.export_to_shapefiles", export), mock.patch(
            "ui.tab_quick_view.messagebox"
        ) as box, mock.patch("ui.panel_inspector.messagebox") as box2:
            self.tab.btn_export.invoke()
            self._pump_until_idle()
            box2.showerror.assert_not_called()

        self.assertEqual(export.call_count, 1)
        sources = {source.path.name: source for source in export.call_args.args[0]}
        self.assertEqual(sorted(sources), ["宗地.shp", "無標籤.shp", "舊圖.shp", "路線.dxf"], "DEM 不轉、需指派的跳過")
        self.assertEqual(sources["舊圖.shp"].source_srs, "TWD97_121", "照那一列選的")
        self.assertIsNone(sources["宗地.shp"].source_srs)
        self.assertEqual(sources["路線.dxf"].cad_layers, ("中心線", "文字"))
        self.assertEqual(export.call_args.args[1], self.out_dir)
        kwargs = export.call_args.kwargs
        self.assertEqual((kwargs["target_srs"], kwargs["encoding"], kwargs["closed_lines_as_polygons"]), ("TWD67_121", "CP950", True))

        saved = sorted(p.name for p in self.out_dir.glob("*.shp"))
        self.assertEqual(saved, ["宗地.shp", "無標籤.shp", "舊圖.shp", "路線_中心線_line.shp", "路線_文字_line.shp"])
        self.assertEqual(self.out_dir.joinpath("宗地.cpg").read_text(encoding="ascii").strip().upper(), "CP950")
        # 轉到 TWD67：本島 X 約少 828 m
        west = geo_io.read_layer_info(self.out_dir / "宗地.shp").extent[0]  # type: ignore[index]
        self.assertTrue(249_100 < west < 249_250, west)
        # 沒有存的圖層與原因：跳出的訊息與日誌都有
        box.showinfo.assert_not_called()
        message = box.showwarning.call_args[0][1]
        self.assertIn("已存成 5 個 SHP", message)
        self.assertIn("區域座標.shp：需指派來源座標系", message)
        self.assertIn("區域座標.shp 跳過：需指派來源座標系", self.inspector.txt_terminal.get("1.0", "end"))
        self.assertEqual(self.tab.action_bar.lbl_status.cget("text"), "另存 SHP：完成")

    def test_export_without_skips_shows_info(self) -> None:
        shp = convert_vector(write_geojson(self.tmp_dir / "a.geojson", PARCEL), self.data_dir / "宗地.shp")
        self.tab.add_paths([shp])
        self._pump_until_idle()
        with mock.patch("ui.tab_quick_view.messagebox") as box:
            self.tab.btn_export.invoke()
            self._pump_until_idle()
        box.showwarning.assert_not_called()
        self.assertIn("座標系：TWD97 121　文字編碼：UTF-8", box.showinfo.call_args[0][1])
        out_dir = self.default_output_dir / VIEW_OUTPUT_FOLDER_NAME
        self.assertTrue((out_dir / "宗地.shp").is_file(), "預設輸出到 view_outputs (沒有時自動建立)")
        self.assertEqual((out_dir / "宗地.cpg").read_text(encoding="ascii").strip().upper(), "UTF-8")

    # -------------------------------------------------------------------------
    # K4：另存 KML／KMZ
    # -------------------------------------------------------------------------
    def test_kml_format_locks_the_menus_and_switching_back_restores_them(self) -> None:
        tab = self.tab
        self.assertEqual(tab.seg_export_format.cget("values"), ["SHP", "KML", "KMZ"])
        self.assertEqual(tab.export_format(), "SHP")
        self.assertFalse(tab.lbl_format_note.winfo_ismapped() or tab.lbl_format_note.grid_info(), "SHP 時不顯示")
        twd67 = next(label for label in output_srs_labels() if label.startswith("TWD67 121"))
        tab.opt_output_srs.set(twd67)
        tab.opt_encoding.set(ENCODING_CHOICES[1][0])

        tab.seg_export_format.set("KMZ", from_button_callback=True)
        self.assertEqual(tab.export_format(), "KMZ")
        self.assertEqual((tab.opt_output_srs.get(), tab.opt_encoding.get()), (KML_SRS_LABEL, "UTF-8"))
        self.assertEqual((tab.opt_output_srs.cget("state"), tab.opt_encoding.cget("state")), ("disabled", "disabled"))
        self.assertTrue(tab.lbl_format_note.grid_info(), "寫原因")
        self.assertEqual(tab.btn_export.cget("text"), "另存勾選的向量圖層為 KMZ")
        self.assertIn("每個檔各存一個 KMZ", tab.lbl_export_hint.cget("text"))
        tab.seg_export_format.set("KML", from_button_callback=True)
        self.assertEqual(tab.opt_output_srs.get(), KML_SRS_LABEL)

        tab.seg_export_format.set("SHP", from_button_callback=True)
        self.assertEqual((tab.opt_output_srs.get(), tab.opt_encoding.get()), (twd67, ENCODING_CHOICES[1][0]), "還原原本選的")
        self.assertEqual((tab.opt_output_srs.cget("state"), tab.opt_encoding.cget("state")), ("normal", "normal"))
        self.assertFalse(tab.lbl_format_note.grid_info())
        self.assertEqual((tab.output_srs_key(), tab.output_encoding()), ("TWD67_121", "CP950"))
        self.assertEqual(tab.btn_export.cget("text"), EXPORT_BUTTON_TEXT)

    def test_kml_export_saves_one_file_per_source_with_checked_layers(self) -> None:
        self.tab.add_paths([self._sample_folder(), self._write_local_shp()])
        self._pump_until_idle()
        dxf = self.tab.items.index(self._item("路線.dxf"))
        self.tab._toggle_layer(dxf, 2)
        self.tab.action_bar.set_output(str(self.out_dir))
        self.tab.chk_closed_polygons.select()
        self.tab.seg_export_format.set("KMZ", from_button_callback=True)

        export = mock.Mock(wraps=quick_view_export.export_to_kml)
        with mock.patch("ui.tab_quick_view.export_to_kml", export), mock.patch(
            "ui.tab_quick_view.messagebox"
        ) as box, mock.patch("ui.panel_inspector.messagebox") as box2:
            self.tab.btn_export.invoke()
            self._pump_until_idle()
            box2.showerror.assert_not_called()

        self.assertEqual(export.call_count, 1)
        sources = {source.path.name: source for source in export.call_args.args[0]}
        self.assertEqual(sorted(sources), ["宗地.shp", "無標籤.shp", "舊圖.shp", "路線.dxf"], "DEM 不轉、需指派的跳過")
        self.assertEqual(sources["路線.dxf"].cad_layers, ("中心線", "文字"))
        kwargs = export.call_args.kwargs
        self.assertEqual((kwargs["kmz"], kwargs["closed_lines_as_polygons"]), (True, True))
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), ["宗地.kmz", "無標籤.kmz", "舊圖.kmz", "路線.kmz"])
        self.assertEqual(geo_io.list_data_layers(self.out_dir / "路線.kmz"), ["中心線", "文字"], "CAD 圖層各一個資料夾")
        message = box.showwarning.call_args[0][1]
        self.assertIn("已存成 4 個 KMZ", message)
        self.assertIn(f"座標系：{KML_SRS_LABEL}　文字編碼：UTF-8", message)
        self.assertIn("區域座標.shp：需指派來源座標系", message)
        self.assertEqual(box.showwarning.call_args[0][0], "另存 KMZ 完成 (有圖層沒有存)")
        self.assertEqual(self.tab.action_bar.lbl_status.cget("text"), "另存 KMZ：完成")


if __name__ == "__main__":
    unittest.main()
