"""WingLightGIS.exe 啟動器 (風格統一 D1、D2 段)：launcher\\build_launcher.bat 編得出來、
替主程式設的環境變數與參數跟原本的 .bat 相同、--debug 用 python.exe、沒有 runtime 時問要不要建立；
螢火蟲啟動畫面在主程式通知後關閉、主程式出錯時說明怎麼查、最多等 15 秒；
H4-2：已經有一份開著 (ui/single_instance.py 的 Mutex) 時不顯示啟動畫面、等主程式交完檔、傳回它的結束代碼；
命令列的相對路徑轉成完整路徑。

做法：在含中文與空白的暫存資料夾編一份 exe，旁邊放替身 main.py (把收到的東西寫成 JSON；
環境變數 STUB_MODE 決定它之後做什麼) 與啟動畫面的圖，runtime 用目錄連結 (junction) 指到真的 runtime；
測完只移除連結本身。替身留著沒結束的，只用它自己寫下的 PID 關掉。
"""

from __future__ import annotations

import _winapi
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Optional

from tests._support import PROJECT_ROOT
from ui import single_instance

BUILD_BAT = PROJECT_ROOT / "launcher" / "build_launcher.bat"
REAL_RUNTIME = PROJECT_ROOT / "runtime"

STUB_MAIN = r'''
import json, os, sys
from pathlib import Path

keys = ["PYTHONHOME", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8", "GDAL_FILENAME_IS_UTF8", "CPL_ZIP_ENCODING"]
data = {
    "pid": os.getpid(),
    "executable": sys.executable,
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "env": {k: os.environ.get(k) for k in keys},
    "path_head": os.environ["PATH"].split(";")[:4],
    "ready_event": os.environ.get("WINGLIGHTGIS_READY_EVENT"),
}
out = Path(__file__).with_name("result.json")
tmp = out.with_suffix(".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
os.replace(tmp, out)

mode = os.environ.get("STUB_MODE", "")
if mode == "ready":
    # 等一下再用正式的 ui/launcher_link.py 通知啟動器，之後還要過一陣子才結束
    import time
    sys.path.insert(0, os.environ["STUB_PROJECT_ROOT"])
    from ui.launcher_link import notify_launcher
    time.sleep(1.5)
    notify_launcher()
    time.sleep(4)
elif mode == "fail":
    sys.exit(3)
elif mode == "hang":
    import time
    time.sleep(25)
elif mode == "slow":
    import time
    time.sleep(2)
'''

WM_GETTEXT = 0x000D
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
IDNO = 7
SPLASH_TITLE = "WingLightGIS 啟動中"
SPLASH_FILES = ["firefly_bright.png", "firefly_dim.png", "title_1x.png", "title_2x.png", "hint_1x.png", "hint_2x.png"]
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _same_path(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(str(b)))


def _dialog_texts(pid: int) -> Optional[tuple]:
    """找 pid 的對話框 (#32770)，回傳 (視窗代號, 裡面所有控制項的文字)。"""
    user32 = ctypes.windll.user32
    found: List[int] = []

    def on_window(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if owner.value == pid and cls.value == "#32770" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(on_window), 0)
    if not found:
        return None
    texts: List[str] = []

    def on_child(hwnd, _):
        buf = ctypes.create_unicode_buffer(1024)
        user32.SendMessageW(hwnd, WM_GETTEXT, 1024, buf)
        texts.append(buf.value)
        return True

    user32.EnumChildWindows(found[0], EnumProc(on_child), 0)
    return found[0], texts


def _has_visible_window(pid: int, title: str) -> bool:
    user32 = ctypes.windll.user32
    hits: List[int] = []

    def on_window(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if owner.value == pid and buf.value == title and user32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(on_window), 0)
    return bool(hits)


def _process_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT＝還在跑
    finally:
        kernel32.CloseHandle(handle)


def _kill(pid: int) -> None:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)  # PROCESS_TERMINATE | SYNCHRONIZE
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.WaitForSingleObject(handle, 5000)
        kernel32.CloseHandle(handle)


class TestLauncher(unittest.TestCase):
    tmp_root: Path
    app_dir: Path
    exe: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not (REAL_RUNTIME / "pythonw.exe").is_file():
            raise unittest.SkipTest("沒有 runtime，無法測啟動器")
        cls.tmp_root = Path(tempfile.mkdtemp(prefix="gis_launcher_test_"))
        cls.app_dir = cls.tmp_root / "啟動 測試"
        cls.app_dir.mkdir()
        cls.exe = cls.app_dir / "WingLightGIS.exe"
        env = dict(os.environ, LAUNCHER_OUT=str(cls.exe))
        build = subprocess.run(["cmd.exe", "/c", str(BUILD_BAT), "--no-pause"], env=env, capture_output=True,
                               timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        if build.returncode != 0 or not cls.exe.is_file():
            cls._cleanup()
            raise AssertionError(f"build_launcher.bat 失敗 (rc={build.returncode}): {build.stdout!r} {build.stderr!r}")
        (cls.app_dir / "main.py").write_text(STUB_MAIN, encoding="utf-8")
        splash_dir = cls.app_dir / "assets" / "splash"
        splash_dir.mkdir(parents=True)
        for name in SPLASH_FILES:
            shutil.copy2(PROJECT_ROOT / "assets" / "splash" / name, splash_dir / name)
        _winapi.CreateJunction(str(REAL_RUNTIME), str(cls.app_dir / "runtime"))

    @classmethod
    def _cleanup(cls) -> None:
        link = cls.tmp_root / "啟動 測試" / "runtime"
        # 只移除連結本身 (對連結 rmdir 不會碰到它指向的真 runtime)；確定是連結才移除，再刪暫存資料夾
        if os.path.lexists(link):
            if os.lstat(link).st_reparse_tag != stat.IO_REPARSE_TAG_MOUNT_POINT:
                raise AssertionError(f"{link} 不是目錄連結，不刪")
            os.rmdir(link)
        shutil.rmtree(cls.tmp_root, ignore_errors=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cleanup()
        assert (REAL_RUNTIME / "python.exe").is_file()

    def setUp(self) -> None:
        self.result = self.app_dir / "result.json"
        self.result.unlink(missing_ok=True)

    def _wait_result(self, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.result.is_file():
                return json.loads(self.result.read_text(encoding="utf-8"))
            time.sleep(0.1)
        self.fail("替身 main.py 沒有執行")

    def test_starts_pythonw_with_same_environment_as_old_bat(self) -> None:
        # 模擬裝了舊版 ArcMap 的電腦：系統帶著 Python 2.7 的 PYTHONHOME
        env = dict(os.environ, PYTHONHOME=r"C:\Python27\ArcGIS10.8", PYTHONUTF8="0", WINGLIGHTGIS_READY_EVENT="外面帶進來的")
        args = [r"D:\資料 夾\a b.tif", 'say "hi"', "C:\\末尾 斜線\\", ""]
        proc = subprocess.Popen([str(self.exe), *args], env=env)
        # 替身寫完就以 0 結束 (沒通知)，啟動畫面跟著關，啟動器回傳 0
        self.assertEqual(proc.wait(timeout=30), 0)
        data = self._wait_result()
        # 通知用的事件是這個啟動器自己建的，不沿用外面帶進來的
        self.assertEqual(data["ready_event"], f"Local\\WingLightGIS-ready-{proc.pid}")

        self.assertTrue(_same_path(data["executable"], self.app_dir / "runtime" / "pythonw.exe"), data["executable"])
        self.assertTrue(_same_path(data["argv"][0], self.app_dir / "main.py"))
        self.assertEqual(data["argv"][1:], args)
        self.assertTrue(_same_path(data["cwd"], self.app_dir))
        self.assertEqual(data["env"], {
            "PYTHONHOME": None,
            "PYTHONPATH": str(self.app_dir) + "\\",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "GDAL_FILENAME_IS_UTF8": "YES",
            "CPL_ZIP_ENCODING": "CP950",
        })
        runtime = self.app_dir / "runtime"
        expected_head = [runtime, runtime / "Library" / "bin", runtime / "Library" / "usr" / "bin", runtime / "Scripts"]
        for got, want in zip(data["path_head"], expected_head):
            self.assertTrue(_same_path(got, want), (got, want))
        self.assertTrue((self.app_dir / "workspace_temp").is_dir())
        self.assertTrue((self.app_dir / "config").is_dir())

    def test_debug_uses_console_python_and_waits(self) -> None:
        env = dict(os.environ, WINGLIGHTGIS_READY_EVENT="外面帶進來的")
        proc = subprocess.run([str(self.exe), "--debug", "x"], env=env, timeout=60, cwd=self.tmp_root)
        # --debug 等主程式結束才返回，結束代碼原樣傳回
        self.assertEqual(proc.returncode, 0)
        data = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertTrue(_same_path(data["executable"], self.app_dir / "runtime" / "python.exe"), data["executable"])
        # 相對路徑照使用者所在的資料夾轉成完整路徑 (主程式的工作資料夾是程式資料夾)
        self.assertEqual(data["argv"][1:], ["--debug", str(self.tmp_root / "x")])
        # --debug 不顯示啟動畫面，也不給通知用的事件
        self.assertIsNone(data["ready_event"])

    def _start(self, mode: str) -> subprocess.Popen:
        env = dict(os.environ, STUB_MODE=mode, STUB_PROJECT_ROOT=str(PROJECT_ROOT))
        return subprocess.Popen([str(self.exe)], env=env)

    def test_splash_shows_then_closes_when_main_program_is_ready(self) -> None:
        proc = self._start("ready")
        stub_pid = None
        try:
            # 替身 1.5 秒後才通知，這段時間啟動畫面要在
            deadline = time.monotonic() + 10
            while not _has_visible_window(proc.pid, SPLASH_TITLE) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(_has_visible_window(proc.pid, SPLASH_TITLE), "沒有出現啟動畫面")
            stub_pid = self._wait_result()["pid"]
            self.assertEqual(proc.wait(timeout=15), 0)
            # 啟動器是收到通知才結束的：替身 (之後還要 4 秒才結束) 這時還在跑
            self.assertTrue(_process_alive(stub_pid))
        finally:
            if proc.poll() is None:
                proc.kill()
            if stub_pid:
                _kill(stub_pid)

    def test_main_program_failing_before_window_explains_how_to_debug(self) -> None:
        proc = self._start("fail")
        try:
            dialog = None
            deadline = time.monotonic() + 20
            while dialog is None and time.monotonic() < deadline:
                dialog = _dialog_texts(proc.pid)
                time.sleep(0.1)
            self.assertIsNotNone(dialog, "沒有出現錯誤訊息")
            hwnd, texts = dialog
            message = "\n".join(texts)
            self.assertIn("結束代碼 3", message)
            self.assertIn("WingLightGIS.exe --debug", message)
            # 錯誤訊息出現時啟動畫面已經關了
            self.assertFalse(_has_visible_window(proc.pid, SPLASH_TITLE))
            # 只有「確定」鈕的訊息框不吃 WM_COMMAND IDOK，用關閉 (等於按確定)
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            self.assertEqual(proc.wait(timeout=20), 1)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_splash_gives_up_after_max_wait(self) -> None:
        start = time.monotonic()
        proc = self._start("hang")
        stub_pid = None
        try:
            stub_pid = self._wait_result()["pid"]
            self.assertEqual(proc.wait(timeout=30), 0)
            elapsed = time.monotonic() - start
            # 規範 Splash：WingLightGIS 最多等 15 秒；主程式不受影響，繼續跑
            self.assertGreater(elapsed, 14.5)
            self.assertLess(elapsed, 20)
            self.assertTrue(_process_alive(stub_pid))
        finally:
            if proc.poll() is None:
                proc.kill()
            if stub_pid:
                _kill(stub_pid)

    def _hold_instance_mutex(self) -> int:
        """假裝已經有一份開著：建主程式會建的那個 Mutex (名稱用 Python 端的算法，順便確認兩邊算的一樣)。"""
        handle, already = single_instance._create_mutex(single_instance.instance_names(self.app_dir).mutex)
        self.assertFalse(already)
        self.addCleanup(single_instance._kernel32.CloseHandle, handle)
        return handle

    def test_running_instance_skips_splash_and_waits_for_hand_off(self) -> None:
        self._hold_instance_mutex()
        env = dict(os.environ, STUB_MODE="slow")
        proc = subprocess.Popen([str(self.exe), "宗地.shp"], env=env, cwd=self.tmp_root)
        try:
            data = self._wait_result()
            self.assertEqual(data["argv"][1:], [str(self.tmp_root / "宗地.shp")])
            self.assertIsNone(data["ready_event"], "不顯示啟動畫面，也不給通知用的事件")
            # 替身 2 秒後才結束：這段時間啟動器在等，而且沒有啟動畫面
            time.sleep(0.5)
            self.assertIsNone(proc.poll())
            self.assertFalse(_has_visible_window(proc.pid, SPLASH_TITLE))
            self.assertEqual(proc.wait(timeout=15), 0)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_running_instance_passes_exit_code_without_its_own_message(self) -> None:
        self._hold_instance_mutex()
        proc = self._start("fail")
        try:
            # 交不出去時主程式自己跳訊息，啟動器只傳回結束代碼
            self.assertEqual(proc.wait(timeout=20), 3)
            self.assertIsNone(_dialog_texts(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_other_folders_instance_does_not_count(self) -> None:
        # D:\APP 那份開著，不影響開發版 (名稱照程式資料夾)
        handle, _ = single_instance._create_mutex(single_instance.instance_names(self.tmp_root / "別的資料夾").mutex)
        self.addCleanup(single_instance._kernel32.CloseHandle, handle)
        proc = self._start("ready")
        stub_pid = None
        try:
            stub_pid = self._wait_result()["pid"]
            self.assertIsNotNone(self._wait_result()["ready_event"])
            self.assertEqual(proc.wait(timeout=15), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            if stub_pid:
                _kill(stub_pid)

    def test_without_runtime_asks_before_running_setup(self) -> None:
        bare = self.tmp_root / "沒有 runtime"
        bare.mkdir()
        exe = bare / "WingLightGIS.exe"
        shutil.copy2(self.exe, exe)
        # 有 setup 檔才會問；這裡回答「否」，所以它不會被執行
        (bare / "setup_portable_env.bat").write_text("exit /b 9\r\n", encoding="ascii")
        proc = subprocess.Popen([str(exe)])
        try:
            dialog = None
            deadline = time.monotonic() + 20
            while dialog is None and time.monotonic() < deadline:
                dialog = _dialog_texts(proc.pid)
                time.sleep(0.1)
            self.assertIsNotNone(dialog, "沒有出現詢問對話框")
            hwnd, texts = dialog
            self.assertTrue(any("還沒有建立 WingLightGIS 的執行環境" in t for t in texts), texts)
            ctypes.windll.user32.PostMessageW(hwnd, WM_COMMAND, IDNO, 0)
            self.assertEqual(proc.wait(timeout=20), 1)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertTrue((bare / "workspace_temp").is_dir())
        self.assertFalse((bare / "runtime").exists())

    def test_built_exe_is_not_committed(self) -> None:
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("WingLightGIS.exe", ignored)


class TestLauncherScripts(unittest.TestCase):
    """兩支 .bat 照專案規則：CP950、CRLF、無 BOM、不能有第二個位元組是 \\ | ^ 的字；舊啟動檔已拿掉。"""

    def test_bat_encoding_rules(self) -> None:
        for path in (BUILD_BAT, PROJECT_ROOT / "setup_portable_env.bat"):
            raw = path.read_bytes()
            text = raw.decode("cp950")
            self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", path.name)
            self.assertTrue(raw.count(b"\r\n") == raw.count(b"\n") == raw.count(b"\r"), path.name)
            bad = [c for c in text if ord(c) >= 0x80 and c.encode("cp950")[1] in (0x5C, 0x7C, 0x5E)]
            self.assertEqual(bad, [], path.name)

    def test_old_launcher_name_is_gone(self) -> None:
        self.assertFalse((PROJECT_ROOT / "啟動_GIS工程工具箱.bat").exists())
        setup = (PROJECT_ROOT / "setup_portable_env.bat").read_bytes().decode("cp950")
        self.assertNotIn("GIS工程工具箱", setup)
        self.assertIn("WingLightGIS.exe", setup)
        self.assertIn(r'call "%TOOLKIT_ROOT%launcher\build_launcher.bat" --no-pause', setup)


if __name__ == "__main__":
    unittest.main()
