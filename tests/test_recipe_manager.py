"""配方管理 P1-9 整合測試 (tests/test_recipe_manager.py)

1. recipes.json 損毀 (整份或個別配方) 時，使用者原本的內容要找得回來，不可被預設配方蓋掉。
2. 存檔先寫暫存檔再原子取代，中途失敗時正式檔不變、不留暫存檔。
3. source_crs：舊代號 TWD67_7PARAM 換成引擎認得的寫法，並可解析成 assigned_srs。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from core import recipe_manager
from core.crs_doctor import ASSIGNABLE_SOURCE_SRS
from core.env_runner import ToolkitError
from core.recipe_manager import (
    WorkflowRecipe,
    add_or_update_recipe,
    get_recipe_by_id,
    load_all_recipes,
    save_all_recipes,
)
from tests._support import REAL_CONFIG_DIR, IsolatedWorkspaceTestCase


class DamagedRecipesFileTest(IsolatedWorkspaceTestCase):
    """recipes.json 讀不懂時不可洗掉使用者配方。"""

    def setUp(self) -> None:
        super().setUp()
        self.recipe_file = self.config_dir / "recipes.json"

    def _other_files(self) -> List[Path]:
        return sorted(p for p in self.config_dir.iterdir() if p.name.startswith("recipes") and p != self.recipe_file)

    def _assert_recoverable(self, original: bytes) -> None:
        """原始內容必須還在正式檔或某個備份檔裡。"""
        candidates = [self.recipe_file] + self._other_files()
        self.assertTrue(any(p.read_bytes() == original for p in candidates), "使用者原本的配方內容已經找不回來")

    def test_truncated_json_is_backed_up_before_any_save(self) -> None:
        # 模擬存檔到一半斷電：JSON 被截斷
        original = '{\n  "我的配方": {"name": "集水區 20m 緩衝", "buffer_distance": 20.0},\n  "第二個": {"na'.encode("utf-8")
        self.recipe_file.write_bytes(original)

        with self.assertLogs("core.recipe_manager", level="WARNING") as logs:
            recipes = load_all_recipes()
        self.assertEqual(list(recipes), ["default"])
        self.assertTrue(any("另存" in line for line in logs.output))

        add_or_update_recipe("新配方", WorkflowRecipe(name="新配方"))

        self._assert_recoverable(original)
        self.assertIn("新配方", json.loads(self.recipe_file.read_text(encoding="utf-8")))

    def test_repeated_load_keeps_single_backup(self) -> None:
        self.recipe_file.write_bytes(b"\xff\xfe not json")

        for _ in range(3):
            load_all_recipes()

        self.assertEqual(len(self._other_files()), 1)

    def test_top_level_list_is_treated_as_damaged(self) -> None:
        original = b'[{"name": "x"}]'
        self.recipe_file.write_bytes(original)

        recipes = load_all_recipes()

        self.assertEqual(list(recipes), ["default"])
        self._assert_recoverable(original)

    def test_one_bad_recipe_does_not_drop_the_rest(self) -> None:
        original = json.dumps(
            {
                "好的": {"name": "好的配方", "buffer_distance": 15},
                "壞的": {"name": "壞的配方", "buffer_distance": "十五公尺"},
                "不是物件": 3,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.recipe_file.write_bytes(original)

        recipes = load_all_recipes()
        self.assertEqual(list(recipes), ["好的"])
        self.assertAlmostEqual(recipes["好的"].buffer_distance, 15.0)

        add_or_update_recipe("新配方", WorkflowRecipe(name="新配方"))

        self._assert_recoverable(original)

    def test_empty_object_is_not_overwritten_on_load(self) -> None:
        self.recipe_file.write_bytes(b"{}")

        recipes = load_all_recipes()

        self.assertEqual(list(recipes), ["default"])
        self.assertEqual(self.recipe_file.read_bytes(), b"{}")

    def test_utf8_bom_file_is_readable(self) -> None:
        # 用記事本改過的 recipes.json 會帶 BOM
        self.recipe_file.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": {"name": "記事本改過"}}, ensure_ascii=False).encode("utf-8"))

        self.assertEqual(load_all_recipes()["a"].name, "記事本改過")
        self.assertEqual(self._other_files(), [])

    def test_backup_failure_raises_instead_of_falling_back(self) -> None:
        self.recipe_file.write_bytes(b"{ broken")

        with mock.patch.object(Path, "write_bytes", side_effect=PermissionError("唯讀")):
            with self.assertRaises(ToolkitError):
                load_all_recipes()

        self.assertEqual(self.recipe_file.read_bytes(), b"{ broken")


class AtomicSaveTest(IsolatedWorkspaceTestCase):
    """存檔中途失敗時正式檔維持原樣。"""

    def setUp(self) -> None:
        super().setUp()
        self.recipe_file = self.config_dir / "recipes.json"
        save_all_recipes({"原有": WorkflowRecipe(name="原有配方", buffer_distance=12.5)})
        self.before = self.recipe_file.read_bytes()

    def _assert_untouched(self) -> None:
        self.assertEqual(self.recipe_file.read_bytes(), self.before)
        self.assertEqual([p.name for p in self.config_dir.iterdir() if p.name.startswith("recipes")], ["recipes.json"])

    def test_failure_while_writing_keeps_old_file(self) -> None:
        def half_written_dump(obj, fp, **kwargs):
            fp.write('{"原有": {"na')
            raise OSError("磁碟已滿")

        with mock.patch.object(recipe_manager.json, "dump", side_effect=half_written_dump):
            with self.assertRaises(OSError):
                add_or_update_recipe("新配方", WorkflowRecipe(name="新配方"))

        self._assert_untouched()

    def test_failure_while_replacing_keeps_old_file(self) -> None:
        with mock.patch.object(recipe_manager.os, "replace", side_effect=PermissionError("檔案被其他程式開啟")):
            with self.assertRaises(PermissionError):
                add_or_update_recipe("新配方", WorkflowRecipe(name="新配方"))

        self._assert_untouched()

    def test_successful_save_round_trips_and_leaves_no_temp_file(self) -> None:
        add_or_update_recipe("新配方", WorkflowRecipe(name="新配方", contour_interval=2.0))

        self.assertEqual(list(load_all_recipes()), ["原有", "新配方"])
        self.assertAlmostEqual(get_recipe_by_id("新配方").contour_interval, 2.0)
        self.assertEqual([p.name for p in self.config_dir.iterdir() if p.name.startswith("recipes")], ["recipes.json"])


class RecipeSourceCrsTest(IsolatedWorkspaceTestCase):
    """source_crs 要能交給引擎當 assigned_srs。"""

    def test_legacy_alias_is_normalized_on_load(self) -> None:
        (self.config_dir / "recipes.json").write_text(
            json.dumps({"舊": {"name": "舊地籍", "source_crs": "TWD67_7PARAM"}}, ensure_ascii=False), encoding="utf-8"
        )

        recipe = load_all_recipes()["舊"]

        self.assertEqual(recipe.source_crs, "TWD67_121")
        self.assertEqual(recipe.resolve_source_srs(), ASSIGNABLE_SOURCE_SRS["TWD67_121"])
        self.assertIn("+towgs84", recipe.resolve_source_srs())

    def test_resolve_source_srs_variants(self) -> None:
        self.assertEqual(WorkflowRecipe(name="預設").resolve_source_srs(), "EPSG:3826")
        self.assertIsNone(WorkflowRecipe(name="自動", source_crs="").resolve_source_srs())
        with self.assertRaises(ToolkitError):
            WorkflowRecipe(name="亂寫", source_crs="火星座標").resolve_source_srs()

    def test_every_shipped_recipe_has_resolvable_source_crs(self) -> None:
        shipped = json.loads((REAL_CONFIG_DIR / "recipes.json").read_text(encoding="utf-8"))
        self.assertNotIn("TWD67_7PARAM", json.dumps(shipped))

        recipes = load_all_recipes()

        self.assertEqual(sorted(recipes), sorted(shipped))
        for recipe_id, recipe in recipes.items():
            self.assertIsNotNone(recipe.resolve_source_srs(), recipe_id)
        self.assertIn("+towgs84", recipes["twd67_legacy_conversion"].resolve_source_srs())


if __name__ == "__main__":
    unittest.main()
