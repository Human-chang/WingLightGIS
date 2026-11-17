"""Milestone 4 單元測試驗證 (tests/test_recipes_and_ui.py)

驗證項目：
1. 工作流配方序列化與載入管理 ([A-08])：
   - 驗證 recipes.json 預設配方載入、欄位完整度與序列化。
   - 驗證新增、查詢與更新自訂工作流配方。
2. 前端介面模組語法與類別完整性：
   - 驗證 ui/dashboard_app.py, ui/panel_inspector.py, ui/tab_clip_workflow.py,
     ui/lab/tab_vector_cad.py, ui/lab/tab_terrain_hydro.py, main.py 之匯入無任何遺漏。
"""

import json
import unittest

from core.recipe_manager import (
    WorkflowRecipe,
    add_or_update_recipe,
    get_recipe_by_id,
    get_recipes_file_path,
    list_recipe_display_options,
    load_all_recipes,
)
from tests._support import REAL_CONFIG_DIR, IsolatedWorkspaceTestCase


class TestRecipesAndUi(IsolatedWorkspaceTestCase):
    """Milestone 4 配方管理與前端模組單元測試案例。"""

    def test_recipes_file_is_redirected_to_temp_dir(self) -> None:
        """配方檔路徑必須落在測試暫存目錄，且正式設定檔不含測試殘留。"""
        self.assertEqual(get_recipes_file_path(), self.config_dir / "recipes.json")
        real_recipes = json.loads((REAL_CONFIG_DIR / "recipes.json").read_text(encoding="utf-8"))
        self.assertNotIn("test_custom_recipe", real_recipes)

    def test_recipes_serialization_and_loading(self) -> None:
        """測試 [A-08] 工作流配方載入、反序列化與欄位檢查。"""
        all_recs = load_all_recipes()
        self.assertGreaterEqual(len(all_recs), 1, "系統必須至少包含 1 個預設配方")
        self.assertIn("default", all_recs)

        default_rec = all_recs["default"]
        self.assertIsInstance(default_rec, WorkflowRecipe)
        self.assertIn("3826", default_rec.target_crs)
        self.assertAlmostEqual(default_rec.dem_resolution, 5.0)

        # 測試字典序列化與還原
        rec_dict = default_rec.to_dict()
        self.assertIn("name", rec_dict)
        self.assertIn("dem_resolution", rec_dict)

        restored_rec = WorkflowRecipe.from_dict(rec_dict)
        self.assertEqual(restored_rec.name, default_rec.name)
        self.assertEqual(restored_rec.dem_resolution, default_rec.dem_resolution)

    def test_custom_recipe_add_and_query(self) -> None:
        """測試新增自訂配方並能透過 ID 成功查詢。"""
        custom_id = "test_custom_recipe"
        custom_recipe = WorkflowRecipe(
            name="測試專用配方 (10m DEM)",
            description="單元測試自訂配方",
            dem_resolution=10.0,
            buffer_distance=25.0,
            export_kmz=False,
        )

        add_or_update_recipe(custom_id, custom_recipe)

        queried = get_recipe_by_id(custom_id)
        self.assertIsNotNone(queried)
        self.assertEqual(queried.name, "測試專用配方 (10m DEM)")
        self.assertAlmostEqual(queried.dem_resolution, 10.0)
        self.assertAlmostEqual(queried.buffer_distance, 25.0)
        self.assertFalse(queried.export_kmz)

        # 測試選單選項包含此配方
        display_options = list_recipe_display_options()
        display_ids = [opt[0] for opt in display_options]
        self.assertIn(custom_id, display_ids)

        # 新配方只寫進暫存目錄的 recipes.json
        saved = json.loads((self.config_dir / "recipes.json").read_text(encoding="utf-8"))
        self.assertIn(custom_id, saved)

    def test_ui_modules_import_cleanliness(self) -> None:
        """測試所有 UI 前端模組均能被無誤正常匯入。"""
        import ui.dashboard_app as dash
        import ui.panel_inspector as insp
        import ui.tab_clip_workflow as tab_clip
        import ui.lab.tab_terrain_hydro as tab_terr
        import ui.lab.tab_vector_cad as tab_vec
        import main

        self.assertTrue(hasattr(dash, "GisMasterToolkitApp"))
        self.assertTrue(hasattr(insp, "PanelInspector"))
        self.assertTrue(hasattr(tab_clip, "TabClipWorkflow"))
        self.assertTrue(hasattr(tab_terr, "TabTerrainHydro"))
        self.assertTrue(hasattr(tab_vec, "TabVectorCad"))
        self.assertTrue(hasattr(main, "main"))

    def test_ui_app_lifecycle_instantiation(self) -> None:
        """測試 CustomTkinter 主視窗完整實例化、組件掛載與銷毀生命週期。"""
        import ui.dashboard_app as dash
        app = dash.GisMasterToolkitApp(start_tool="dem")
        self.assertIsNotNone(app.inspector_panel)
        self.assertIsNotNone(app.tab_clip)
        self.assertIsNotNone(app.tab_vector)
        self.assertIsNotNone(app.tab_terrain)
        app.update()
        app.destroy()


if __name__ == "__main__":
    unittest.main()
