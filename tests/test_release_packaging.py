"""發佈環境退回 %APPDATA% 與 --self-test 單元測試 (tests/test_release_packaging.py)。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as app_main
from core import env_runner
from ui import settings


class TestAppDataFallback(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_temp = env_runner._workspace_temp_override
        self._prev_config = env_runner._config_dir_override
        self._prev_output = env_runner._default_output_override
        env_runner.set_workspace_temp_dir(None)
        env_runner.set_config_dir(None)
        env_runner.set_default_output_dir(None)
        env_runner.reset_writable_cache()

    def tearDown(self) -> None:
        env_runner.reset_writable_cache()
        env_runner.set_workspace_temp_dir(self._prev_temp)
        env_runner.set_config_dir(self._prev_config)
        env_runner.set_default_output_dir(self._prev_output)

    def test_unwritable_toolkit_root_falls_back_to_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            fake_ro_root = tmp_dir / "ro_root"
            (fake_ro_root / "config").mkdir(parents=True)
            (fake_ro_root / "config" / "recipes.json").write_text('{"recipes": []}\n', encoding="utf-8")
            (fake_ro_root / "config" / "terrain_ramp.txt").write_text("0 0 0 0\n", encoding="utf-8")

            fake_appdata = tmp_dir / "AppData" / "Roaming"
            fake_appdata.mkdir(parents=True)

            def _raise_perm(*args, **kwargs):
                raise PermissionError("simulated Program Files ACL")

            with mock.patch.dict(os.environ, {"APPDATA": str(fake_appdata)}), \
                 mock.patch.object(env_runner, "get_toolkit_root", return_value=fake_ro_root), \
                 mock.patch("builtins.open", side_effect=_raise_perm):
                self.assertFalse(env_runner.is_toolkit_root_writable())

            with mock.patch.dict(os.environ, {"APPDATA": str(fake_appdata)}), \
                 mock.patch.object(env_runner, "get_toolkit_root", return_value=fake_ro_root):
                cfg_dir = env_runner.get_config_dir()
                tmp_work = env_runner.get_workspace_temp_dir()
                out_dir = env_runner.get_default_output_dir()

                expected_base = fake_appdata.resolve() / "WingLightGIS"
                self.assertEqual(cfg_dir, expected_base / "config")
                self.assertEqual(tmp_work, expected_base / "workspace_temp")
                self.assertEqual(out_dir, expected_base / "outputs")
                # 預設 recipes.json 與 terrain_ramp.txt 會一併複製到 AppData 設定目錄
                self.assertTrue((cfg_dir / "recipes.json").is_file())
                self.assertTrue((cfg_dir / "terrain_ramp.txt").is_file())

                settings.save_appearance_mode("Dark")
                self.assertEqual(settings.saved_appearance_mode(), "Dark")
                self.assertTrue((cfg_dir / "WingLightGIS.json").is_file())


class TestSelfTestCli(unittest.TestCase):
    def test_parse_self_test_path(self) -> None:
        self.assertIsNone(app_main._parse_self_test_path(["a.shp"]))
        self.assertEqual(app_main._parse_self_test_path(["--self-test"]), Path("selftest_result.json"))
        self.assertEqual(app_main._parse_self_test_path(["--self-test", "out.json"]), Path("out.json"))
        self.assertEqual(app_main._parse_self_test_path(["--self-test=out2.json"]), Path("out2.json"))

    def test_cli_self_test_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            report_path = Path(tmp_str) / "report.json"
            proc = subprocess.run(
                [sys.executable, str(Path(app_main.__file__).resolve()), "--self-test", str(report_path)],
                cwd=str(Path(app_main.__file__).resolve().parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
            self.assertEqual(proc.returncode, 0, msg=f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
            self.assertTrue(report_path.is_file())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["all_passed"])
            self.assertGreaterEqual(len(payload["checks"]), 4)
            for chk in payload["checks"]:
                self.assertTrue(chk["passed"], msg=str(chk))


if __name__ == "__main__":
    unittest.main()
