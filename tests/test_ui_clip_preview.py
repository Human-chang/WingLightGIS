"""步驟 9-2：裁切分頁的預覽跟著清單與邊界走，瀏覽器地圖按了才產生、沒變就沿用。"""

from __future__ import annotations

import time
import unittest
from unittest import mock

import numpy as np

from core import clip_preview
from tests._support import IsolatedWorkspaceTestCase, write_dem

SQUARE_TEXT = "250050 2650050\n250150 2650050\n250150 2650150\n250050 2650150\n"


class TestClipTabPreview(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.tab = self.app.tab_dem
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

    def _caption(self) -> str:
        return self.inspector.lbl_preview_caption.cget("text")

    def _paste_boundary(self) -> None:
        self.tab.txt_coords.delete("1.0", "end")
        self.tab.txt_coords.insert("1.0", SQUARE_TEXT)
        with mock.patch("ui.panel_inspector.messagebox"):
            self.tab._handle_create_boundary_from_text()
            self._pump_until_idle()

    def test_thumbnail_follows_selection_and_boundary(self) -> None:
        first = write_dem(self.data_dir / "甲.tif", np.full((40, 40), 10.0))
        second = write_dem(self.data_dir / "乙.tif", np.full((40, 40), 20.0))
        self.tab._add_input_files([first, second])
        self._pump_until_idle()
        self.assertEqual(self._caption(), "原始：甲.tif (還沒有裁切邊界)")

        self.tab._select_file_index(1)
        self._pump_until_idle()
        self.assertEqual(self._caption(), "原始：乙.tif (還沒有裁切邊界)")

        self._paste_boundary()
        self.assertEqual(self._caption(), "原始：乙.tif　紅框：裁切邊界")
        # 舊縮圖檔刪掉，只留目前這張
        thumbnails = sorted(p.name for p in self.work_temp.glob("preview_*_dem.png"))
        self.assertEqual(thumbnails, [self.inspector.current_preview_image.name])

        self.tab._handle_clear_files()
        self._pump_until_idle()
        self.assertEqual(self._caption(), "紅框：裁切邊界 (還沒投入原始檔)")

    def test_browser_map_is_reused_until_something_changes(self) -> None:
        dem = write_dem(self.data_dir / "地形.tif", np.full((40, 40), 10.0))
        self.tab._add_input_files([dem])
        self._paste_boundary()

        build = mock.Mock(wraps=clip_preview.build_clip_preview_map)
        with mock.patch("ui.tab_clip_workflow.build_clip_preview_map", build), mock.patch(
            "ui.tab_clip_workflow.open_preview_in_browser"
        ) as opener, mock.patch("ui.panel_inspector.messagebox") as box:
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
            self.assertEqual(build.call_count, 1, "內容沒變就直接開上次的地圖")
            self.assertEqual(opener.call_count, 2)

            # 重建邊界 (檔案時間變了) 就要重做
            self._paste_boundary()
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
            self.assertEqual(build.call_count, 2)
            box.showerror.assert_not_called()
        self.assertEqual(build.call_args.kwargs["source"], dem.resolve())
        self.assertIsNone(build.call_args.kwargs["clipped"])

    def test_browser_map_with_nothing_logs_a_hint(self) -> None:
        with mock.patch("ui.tab_clip_workflow.open_preview_in_browser") as opener:
            self.inspector._handle_open_browser_map()
            self._pump_until_idle()
        opener.assert_not_called()
        self.assertIn("還沒有可以放上地圖的圖資", self.inspector.txt_terminal.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
