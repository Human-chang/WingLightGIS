"""DWG／DGN 選取防呆的測試。"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from tests._support import IsolatedWorkspaceTestCase
from ui.file_guards import UNSUPPORTED_CAD_MESSAGE, is_unsupported_cad, validate_supported_files


class TestFileGuards(unittest.TestCase):
    def test_validate_supported_files_drops_dwg_and_dgn(self) -> None:
        kept, warning = validate_supported_files(["a.shp", "B.DWG", Path("c.dgn"), "d.dxf"])
        self.assertEqual([p.name for p in kept], ["a.shp", "d.dxf"])
        self.assertIsNotNone(warning)
        self.assertIn(UNSUPPORTED_CAD_MESSAGE, warning)
        self.assertIn("B.DWG", warning)
        self.assertIn("c.dgn", warning)

    def test_validate_supported_files_without_cad_has_no_warning(self) -> None:
        kept, warning = validate_supported_files(["a.shp", "b.tif"])
        self.assertEqual(len(kept), 2)
        self.assertIsNone(warning)
        self.assertTrue(is_unsupported_cad("x.Dgn"))
        self.assertFalse(is_unsupported_cad("x.dxf"))

    def test_long_reject_list_is_summarised(self) -> None:
        _, warning = validate_supported_files([f"f{i}.dwg" for i in range(8)])
        self.assertIn("等 8 個", warning)
        self.assertNotIn("f7.dwg", warning)


class TestUiGuardsAndRecipes(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.inspector = self.app.inspector_panel
        self.clip = self.app.tab_clip
        self.app.update()

    def tearDown(self) -> None:
        self.app.destroy()
        super().tearDown()

    def _pump_until_idle(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if not self.inspector.is_busy and self.clip._light_pending == 0:
                self.app.update()
                return
            time.sleep(0.02)
        self.fail("背景作業逾時未結束")

    def _log_text(self) -> str:
        return self.inspector.txt_terminal.get("1.0", "end")

    # ------------------------------------------------------------------ DWG／DGN 防呆
    def test_browse_rejects_dwg_in_vector_and_terrain_tabs(self) -> None:
        cases = [
            ("ui.lab.tab_vector_cad", self.app.tab_vector, self.app.tab_vector.entry_inter_bound),
            ("ui.lab.tab_terrain_hydro", self.app.tab_terrain, self.app.tab_terrain.entry_dem_before),
        ]
        for module, tab, entry in cases:
            with self.subTest(module=module), mock.patch(f"{module}.filedialog") as dialog, mock.patch(f"{module}.messagebox") as box:
                dialog.askopenfilename.return_value = str(self.data_dir / "plan.dwg")
                tab._browse_into(entry, "選取", [("全部", "*.*")])
                box.showwarning.assert_called_once()
                self.assertIn("另存為 DXF", box.showwarning.call_args[0][1])
                self.assertEqual(entry.get(), "")

                good = self.data_dir / "ok.gpkg"
                dialog.askopenfilename.return_value = str(good)
                tab._browse_into(entry, "選取", [("全部", "*.*")])
                box.showwarning.assert_called_once()
                self.assertEqual(entry.get(), str(good.resolve()))

    def test_merge_picker_drops_dgn(self) -> None:
        tab = self.app.tab_vector
        picked = (str(self.data_dir / "a.shp"), str(self.data_dir / "old.dgn"), str(self.data_dir / "b.gpkg"))
        with mock.patch("ui.lab.tab_vector_cad.filedialog") as dialog, mock.patch("ui.lab.tab_vector_cad.messagebox") as box:
            dialog.askopenfilenames.return_value = picked
            tab._browse_merge_files()
            box.showwarning.assert_called_once()
            self.assertIn("old.dgn", box.showwarning.call_args[0][1])
        self.assertEqual([p.name for p in tab.merge_files], ["a.shp", "b.gpkg"])
        self.assertIn("已選取 2 個", tab.lbl_merge_count.cget("text"))


if __name__ == "__main__":
    unittest.main()
