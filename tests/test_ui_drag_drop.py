"""拖放檔案與資料夾的測試 (ui/drag_drop.py)。

無法在測試裡產生真的檔案總管拖放，改成取出 tkdnd 綁在 <<Drop>> 的指令，
帶入與 tkdnd 相同順序的 18 個替換值直接呼叫，走的是同一條 Python 回呼路徑。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import customtkinter as ctk

from tests._support import IsolatedWorkspaceTestCase, square, write_geojson
from ui import drag_drop

# tkdnd 替換值的順序：%A %a %b %C %c {%CST} {%CTT} %D %e {%L} {%m} {%ST} %T {%t} {%TT} %W %X %Y
_DATA_INDEX = 7
_WIDGET_INDEX = 15


def simulate_drop(widget: Any, *paths: Path) -> Any:
    """對 widget (CTk 元件取內層輸入框／文字框) 模擬一次拖放。"""
    target = getattr(widget, "_entry", None) or getattr(widget, "_textbox", None) or widget
    script = target.tk.call("bind", target._w, "<<Drop>>")
    funcid = str(script).split()[0]
    args = [""] * 18
    args[_DATA_INDEX] = target.tk.call("list", *[str(p) for p in paths])
    args[_WIDGET_INDEX] = target._w
    args[16] = args[17] = "0"
    return target.tk.call(funcid, *args)


class TestDragDropHelpers(unittest.TestCase):
    def test_root_without_tkdnd_is_left_alone(self) -> None:
        root = ctk.CTk()
        try:
            entry = ctk.CTkEntry(root)
            self.assertFalse(drag_drop.is_enabled(entry))
            self.assertFalse(drag_drop.bind_file_entry(entry))
        finally:
            root.destroy()

    def test_paths_with_spaces_and_chinese_are_split(self) -> None:
        root = ctk.CTk()
        try:
            data = root.tk.call("list", r"D:\資料 夾\a b.shp", r"D:\x.tif")
            self.assertEqual(drag_drop.parse_drop_data(root, data), [Path(r"D:\資料 夾\a b.shp"), Path(r"D:\x.tif")])
        finally:
            root.destroy()


class TestDragDropInApp(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy and all(t._light_pending == 0 for t in self.app.clip_tabs.values()):
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _vector(self, name: str, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        return write_geojson(folder / f"{name}.geojson", [({"n": name}, square(250000, 2650000, 250050, 2650050))])

    def test_tkdnd_is_loaded(self) -> None:
        self.assertTrue(self.app._drag_drop_ready)
        self.assertTrue(drag_drop.is_enabled(self.app))

    def test_drop_files_and_folder_into_clip_list(self) -> None:
        tab = self.app.tab_shp
        single = self._vector("單檔", self.data_dir)
        folder = self.data_dir / "整批 圖資"
        self._vector("a", folder)
        self._vector("b", folder / "子資料夾")

        self.assertEqual(simulate_drop(tab.file_list.tree, single, folder), "copy")
        self._pump_until_idle()

        self.assertEqual(sorted(p.name for p in tab.selected_input_files), ["a.geojson", "b.geojson", "單檔.geojson"])
        self.assertNotIn("拖進清單", tab.lbl_file_count.cget("text"))

    def test_drop_boundary_file_loads_it(self) -> None:
        tab = self.app.tab_clip
        boundary = self._vector("邊界", self.data_dir)
        with mock.patch("ui.tab_clip_workflow.messagebox"), mock.patch("ui.panel_inspector.messagebox") as box:
            simulate_drop(tab.entry_bound_file, boundary)
            self._pump_until_idle()
            box.showerror.assert_not_called()
        self.assertEqual(tab.entry_bound_file.get(), str(boundary.resolve()))
        self.assertIsNotNone(tab.master_boundary_gpkg)

    def test_drop_into_file_entry_rejects_folder_and_dwg(self) -> None:
        entry = self.app.tab_terrain.entry_road_line
        dwg = self.data_dir / "plan.dwg"
        with mock.patch("ui.drag_drop.messagebox") as box:
            simulate_drop(entry, self.data_dir)
            simulate_drop(entry, dwg)
            self.assertEqual(box.showwarning.call_count, 2)
        self.assertEqual(entry.get(), "")

        line = self._vector("中心線", self.data_dir)
        simulate_drop(entry, line)
        self.assertEqual(entry.get(), str(line.resolve()))

    def test_drop_file_into_output_dir_uses_its_folder(self) -> None:
        row = self.app.tab_vector.out_merge
        source = self._vector("來源", self.data_dir / "成果放這")
        simulate_drop(row.entry, source)
        self.assertEqual(row.entry.get(), str(source.parent.resolve()))

    def test_drop_into_merge_card_appends(self) -> None:
        tab = self.app.tab_vector
        first = self._vector("圖幅1", self.data_dir)
        folder = self.data_dir / "圖幅"
        self._vector("圖幅2", folder)
        (folder / "說明.txt").write_text("不是向量", encoding="utf-8")

        simulate_drop(tab.lbl_merge_count, first)
        simulate_drop(tab.lbl_merge_count, folder, first)

        self.assertEqual([p.name for p in tab.merge_files], ["圖幅1.geojson", "圖幅2.geojson"])
        self.assertIn("已選取 2 個", tab.lbl_merge_count.cget("text"))


if __name__ == "__main__":
    unittest.main()
