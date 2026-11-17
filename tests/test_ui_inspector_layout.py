"""右側檢視欄：固定寬度、約 200 px 的預覽、四角座標、空間體檢、處理紀錄只顯示最新一行與紀錄視窗、預覽放大與圖說。"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from PIL import Image

from core.crs_doctor import sniff_crs
from tests._support import IsolatedWorkspaceTestCase, square, write_geojson
from ui import panel_inspector
from ui.theme_constants import BTN_PRIMARY, BTN_SECONDARY, STATUS_OK


def colour(value: Any) -> str:
    return str(tuple(value)) if isinstance(value, (list, tuple)) else str(value)


class TestInspectorLayout(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _write_image(self, name: str, size: tuple) -> Any:
        path = self.data_dir / name
        Image.new("RGB", size, (120, 160, 90)).save(path)
        return path

    def test_fixed_width_and_small_preview(self) -> None:
        ins = self.inspector
        self.assertEqual(ins.cget("width"), panel_inspector.INSPECTOR_WIDTH)
        self.assertEqual(ins.preview_canvas_frame.cget("height"), panel_inspector.PREVIEW_HEIGHT)
        self.assertEqual(panel_inspector.INSPECTOR_WIDTH, 348)
        self.assertLessEqual(panel_inspector.PREVIEW_HEIGHT, 200)
        # 預覽在最上面，接著四角座標、空間體檢、處理紀錄
        rows = [int(w.grid_info()["row"]) for w in (ins.preview_canvas_frame, ins.txt_corners, ins.card_diag, ins.lbl_log_last.master)]
        self.assertEqual(rows, sorted(rows))

    def test_log_shows_latest_line_and_full_log_in_window(self) -> None:
        ins = self.inspector
        ins.append_log("[開始] 第一件\n")
        ins.append_log("[完成] 第二件\n\n")
        self.assertEqual(ins.lbl_log_last.cget("text"), "[完成] 第二件")
        self.assertIn("第一件", ins.txt_terminal.get("1.0", "end"))
        # 完整紀錄不放在版面上
        self.assertEqual(ins.txt_terminal.winfo_manager(), "")

        ins.btn_show_log.invoke()
        self.app.update()
        self.assertIsNotNone(ins.log_window)
        self.assertIn("第一件", ins._log_window_text.get("1.0", "end"))
        ins.append_log("[提示] 視窗開著時也同步\n")
        self.assertIn("視窗開著時也同步", ins._log_window_text.get("1.0", "end"))
        ins.btn_clear_log.invoke()
        self.assertEqual(ins.txt_terminal.get("1.0", "end-1c"), "")
        self.assertEqual(ins._log_window_text.get("1.0", "end-1c"), "")
        self.assertEqual(ins.lbl_log_last.cget("text"), panel_inspector.LOG_EMPTY_TEXT)
        ins._close_log_window()
        self.assertIsNone(ins.log_window)

    def test_log_line_cap(self) -> None:
        ins = self.inspector
        with mock.patch.object(panel_inspector, "MAX_LOG_LINES", 50):
            for index in range(200):
                ins.append_log(f"第 {index} 列\n")
            lines = ins.txt_terminal.get("1.0", "end-1c").split("\n")
            self.assertLessEqual(len(lines), 50)
            self.assertIn("第 199 列", lines)
            self.assertNotIn("第 0 列", lines)
            self.assertNotIn("[系統就緒] WingLightGIS 核心已初始化。", lines)
        ins.clear_log()
        self.assertEqual(ins.txt_terminal.get("1.0", "end"), "\n")

    def test_diagnosis_rows(self) -> None:
        ins = self.inspector
        cells = {key: (int(label.grid_info()["row"]), int(label.grid_info()["column"])) for key, label in ins.diag_value_labels.items()}
        self.assertEqual(
            cells,
            {"來源圖資:": (2, 1), "座標系統:": (3, 1), "閉鎖面積:": (4, 1), "四至邊界:": (5, 1), "診斷建議:": (6, 1)},
        )
        self.assertEqual(ins.lbl_status_badge.cget("text_color"), STATUS_OK)

        boundary = write_geojson(self.data_dir / "site.geojson", [({"name": "site"}, square(250000, 2650000, 250100, 2650050))])
        ins.update_spatial_diagnosis(sniff_crs(boundary), area_m2=5000.0)
        self.assertEqual(ins.diag_value_labels["來源圖資:"].cget("text"), "site.geojson")
        self.assertIn("5,000.00 m²", ins.diag_value_labels["閉鎖面積:"].cget("text"))
        self.assertIn("250,000.0", ins.diag_value_labels["四至邊界:"].cget("text"))
        self.assertTrue(ins.lbl_status_badge.cget("text").startswith("正常："))

    def test_buttons_are_secondary(self) -> None:
        ins = self.inspector
        # KMZ 鈕已拿掉 (步驟 8-1)；預覽下方是圖說 (9-2)，再下面是瀏覽器地圖鈕
        self.assertFalse(hasattr(ins, "btn_kmz"))
        self.assertEqual(int(ins.lbl_preview_caption.grid_info()["row"]), int(ins.preview_canvas_frame.grid_info()["row"]) + 1)
        self.assertEqual(int(ins.btn_browser_map.grid_info()["row"]), int(ins.lbl_preview_caption.grid_info()["row"]) + 1)
        for button in (ins.btn_browser_map, ins.btn_use_extent, ins.btn_copy_corners, ins.btn_show_log):
            self.assertEqual(colour(button.cget("fg_color")), colour(BTN_SECONDARY["fg_color"]))
            self.assertNotEqual(colour(button.cget("fg_color")), colour(BTN_PRIMARY["fg_color"]))

    def test_resize_reuses_preview_image_object(self) -> None:
        ins = self.inspector
        ins.load_preview_image(self._write_image("relief.png", (400, 200)))
        first = ins._preview_ctk_image
        self.assertIsNotNone(first)
        with mock.patch.object(panel_inspector.ctk, "CTkImage") as image_cls:
            for _ in range(5):
                ins._on_canvas_resize(None)
            # 模擬畫布變大：只改顯示尺寸
            ins._preview_size = (1, 1)
            ins._on_canvas_resize(None)
            image_cls.assert_not_called()
        self.assertIs(ins._preview_ctk_image, first)
        width, height = ins._preview_size
        self.assertAlmostEqual(width / height, 2.0, delta=0.05)
        self.assertEqual(tuple(first.cget("size")), (width, height))

        # 換一張圖才會重建
        ins.load_preview_image(self._write_image("relief2.png", (100, 300)))
        self.assertIsNot(ins._preview_ctk_image, first)
        self.assertLess(ins._preview_size[0], ins._preview_size[1])

    def test_click_preview_opens_zoom_window(self) -> None:
        ins = self.inspector
        ins._handle_zoom_preview()
        self.assertIsNone(ins.preview_window)
        self.assertIn("沒有預覽圖可以放大", ins.txt_terminal.get("1.0", "end"))

        ins.load_preview_image(self._write_image("relief.png", (400, 200)))
        ins._handle_zoom_preview()
        self.app.update()
        window = ins.preview_window
        self.assertIsNotNone(window)
        self.assertTrue(window.winfo_exists())
        self.assertIn("relief.png", window.title())
        zoom_w, zoom_h = ins.lbl_zoom_image.cget("image").cget("size")
        self.assertAlmostEqual(zoom_w / zoom_h, 2.0, delta=0.05)
        self.assertLessEqual(zoom_w, 1200)
        self.assertLessEqual(zoom_w, int(ins.winfo_screenwidth() * panel_inspector.ZOOM_SCREEN_RATIO))

        # 再點一次或換圖都沿用同一個視窗
        ins.load_preview_image(self._write_image("other.png", (300, 300)))
        self.app.update()
        self.assertIs(ins.preview_window, window)
        self.assertIn("other.png", window.title())

        ins._close_preview_window()
        self.app.update()
        self.assertIsNone(ins.preview_window)
        self.assertFalse(window.winfo_exists())

    def test_caption_follows_image_and_clear_preview(self) -> None:
        ins = self.inspector
        ins.load_preview_image(self._write_image("relief.png", (400, 200)), "原始：relief.tif　紅框：裁切邊界")
        self.assertEqual(ins.lbl_preview_caption.cget("text"), "原始：relief.tif　紅框：裁切邊界")
        ins._handle_zoom_preview()
        self.app.update()
        self.assertIsNotNone(ins.preview_window)

        ins.clear_preview()
        self.app.update()
        self.assertIsNone(ins.current_preview_image)
        self.assertEqual(ins.lbl_preview_caption.cget("text"), "")
        self.assertEqual(ins.lbl_image_display.cget("text"), panel_inspector.PREVIEW_PLACEHOLDER_TEXT)
        self.assertIsNone(ins.preview_window, "清空預覽時大圖視窗也要關掉")


if __name__ == "__main__":
    unittest.main()
