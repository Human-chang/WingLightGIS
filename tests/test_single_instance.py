"""只開一份 WingLightGIS (ui/single_instance.py) 與 main.py 的命令列。

1. 名稱跟著程式資料夾 (大小寫、結尾斜線不算不同)；開發版與 D:\\APP 那份各自一份。
2. 第一份收到後開的那一份交來的路徑；主視窗還沒好 (還沒 set_handler) 時先存著。
3. 好幾份同時開 (檔案總管一次選好幾個檔開啟) 都交進同一份；第一份管道還沒開好時後開的會等。
4. 第一份沒有回應時逾時報錯；第一份結束後再開的自己當第一份；看不懂的訊息 (例如 pickle) 略過。
5. main.py：命令列路徑轉完整路徑、略過 --debug；交出去就結束，不載入介面；交不出去跳錯誤並以 1 結束。
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import threading
import time
import unittest
import uuid
from multiprocessing.connection import Client
from pathlib import Path
from typing import List
from unittest import mock

from tests._support import PROJECT_ROOT
from ui import single_instance
from ui.single_instance import HandOffError, InstanceServer, claim_or_hand_off, instance_names


def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class TestSingleInstance(unittest.TestCase):
    def setUp(self) -> None:
        # 每個測試自己的名稱，不會碰到使用者開著的 WingLightGIS
        self.names = instance_names(Path(f"D:\\測試資料夾\\{uuid.uuid4().hex}"), prefix="WingLightGISTest")
        self.received: List[List[str]] = []
        self.servers: List[InstanceServer] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.close()

    def _claim(self, paths=(), timeout: float = 5.0):
        result = claim_or_hand_off(list(paths), names=self.names, timeout=timeout)
        if result is not None:
            self.servers.append(result)
        return result

    def test_names_follow_the_program_folder(self) -> None:
        a = instance_names(Path("D:\\APP\\WingLightGIS"))
        self.assertEqual(a, instance_names(Path("d:\\app\\winglightgis\\")), "大小寫與結尾斜線不算不同")
        self.assertNotEqual(a.mutex, instance_names(Path("D:\\Tools\\WingLightGIS_Dev")).mutex, "開發版與日常用的那份各自一份")
        self.assertTrue(a.mutex.startswith("Local\\WingLightGIS-"))
        self.assertTrue(a.pipe.startswith("\\\\.\\pipe\\WingLightGIS-"))
        self.assertEqual(single_instance.default_names(), instance_names(PROJECT_ROOT))

    def test_second_copy_hands_paths_to_the_first(self) -> None:
        server = self._claim()
        self.assertIsInstance(server, InstanceServer)
        server.set_handler(self.received.append)
        self.assertIsNone(self._claim(["D:\\圖資\\宗地.shp", "D:\\圖資\\路線.dxf"]), "第二份交出去就好")
        self.assertIsNone(self._claim([]), "沒帶檔案也交 (第一份叫到前面)")
        self.assertTrue(_wait_for(lambda: len(self.received) == 2))
        self.assertEqual(self.received, [["D:\\圖資\\宗地.shp", "D:\\圖資\\路線.dxf"], []])

    def test_paths_before_the_window_is_ready_are_kept(self) -> None:
        server = self._claim()
        self.assertIsNone(self._claim(["D:\\a.shp"]))
        self.assertIsNone(self._claim(["D:\\b.shp"]))
        self.assertEqual(self.received, [])
        server.set_handler(self.received.append)
        self.assertEqual(self.received, [["D:\\a.shp"], ["D:\\b.shp"]])

    def test_several_copies_started_together_all_hand_off(self) -> None:
        server = self._claim()
        server.set_handler(self.received.append)
        code = (
            "import sys\n"
            "from ui.single_instance import InstanceNames, claim_or_hand_off\n"
            "names = InstanceNames(sys.argv[1], sys.argv[2])\n"
            "sys.exit(0 if claim_or_hand_off(sys.argv[3:], names=names, timeout=10) is None else 3)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT), PYTHONIOENCODING="utf-8")
        copies = [
            subprocess.Popen(
                [sys.executable, "-c", code, self.names.mutex, self.names.pipe, f"D:\\圖資\\第{i}區.shp"],
                cwd=PROJECT_ROOT,
                env=env,
            )
            for i in range(5)
        ]
        self.assertEqual([p.wait(timeout=60) for p in copies], [0] * 5)
        self.assertTrue(_wait_for(lambda: len(self.received) == 5))
        self.assertEqual(sorted(p for paths in self.received for p in paths), sorted(f"D:\\圖資\\第{i}區.shp" for i in range(5)))

    def test_waits_while_the_first_copy_is_still_starting(self) -> None:
        # 第一份已經有 Mutex、管道還沒開 (還在載入)
        handle, already = single_instance._create_mutex(self.names.mutex)
        self.assertFalse(already)
        results: list = []
        worker = threading.Thread(target=lambda: results.append(claim_or_hand_off(["D:\\a.shp"], names=self.names, timeout=10)))
        worker.start()
        time.sleep(0.5)
        self.assertTrue(worker.is_alive(), "管道還沒開時後開的那一份在等")
        server = InstanceServer(self.names, handle)
        self.servers.append(server)
        server.set_handler(self.received.append)
        worker.join(timeout=10)
        self.assertEqual(results, [None])
        self.assertEqual(self.received, [["D:\\a.shp"]])

    def test_unresponsive_first_copy_times_out(self) -> None:
        handle, _ = single_instance._create_mutex(self.names.mutex)
        try:
            started = time.monotonic()
            with self.assertRaises(HandOffError) as raised:
                claim_or_hand_off(["D:\\a.shp"], names=self.names, timeout=0.3)
            self.assertLess(time.monotonic() - started, 3)
            self.assertIn("沒有回應", str(raised.exception))
        finally:
            single_instance._kernel32.CloseHandle(handle)

    def test_after_the_first_copy_closes_the_next_one_is_first(self) -> None:
        self._claim().close()
        self.assertIsInstance(self._claim(), InstanceServer)

    def test_unreadable_messages_are_ignored(self) -> None:
        server = self._claim()
        server.set_handler(self.received.append)
        for junk in (b"\xff\xfe not utf-8", b'{"paths": "D:\\\\a.shp"}', pickle.dumps({"paths": ["D:\\a.shp"]})):
            with self.assertLogs("ui.single_instance", level="WARNING"):
                with Client(self.names.pipe, family="AF_PIPE") as conn:
                    conn.send_bytes(junk)
                    with self.assertRaises((EOFError, OSError)):
                        conn.recv_bytes()
        self.assertIsNone(self._claim(["D:\\b.shp"]), "略過之後照常收")
        self.assertTrue(_wait_for(lambda: self.received == [["D:\\b.shp"]]))


class TestMainCommandLine(unittest.TestCase):
    def test_paths_become_absolute_and_options_are_skipped(self) -> None:
        import main

        self.assertEqual(
            main.command_line_paths(["--debug", "宗地.shp", "D:\\圖資\\路線.dxf"]),
            [os.path.abspath("宗地.shp"), "D:\\圖資\\路線.dxf"],
        )

    def test_handing_off_ends_without_loading_the_window(self) -> None:
        import main

        with mock.patch("sys.argv", ["main.py", "D:\\圖資\\宗地.shp"]), \
                mock.patch("ui.single_instance.claim_or_hand_off", return_value=None) as claim, \
                mock.patch("ui.dashboard_app.launch_app") as launch:
            main.main()
        claim.assert_called_once_with(["D:\\圖資\\宗地.shp"])
        launch.assert_not_called()

    def test_first_copy_passes_paths_and_server_to_the_window(self) -> None:
        import main

        server = mock.Mock()
        with mock.patch("sys.argv", ["main.py", "D:\\圖資\\宗地.shp"]), \
                mock.patch("ui.single_instance.claim_or_hand_off", return_value=server), \
                mock.patch("ui.app_icon.set_app_user_model_id"), \
                mock.patch("ui.dashboard_app.launch_app") as launch:
            main.main()
        launch.assert_called_once_with(["D:\\圖資\\宗地.shp"], server)

    def test_failed_hand_off_shows_error_and_exits_1(self) -> None:
        import main

        with mock.patch("sys.argv", ["main.py"]), \
                mock.patch("ui.single_instance.claim_or_hand_off", side_effect=HandOffError("沒有回應")), \
                mock.patch("tkinter.messagebox.showerror") as box, \
                mock.patch("ui.dashboard_app.launch_app") as launch:
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("沒有回應", box.call_args[0][1])
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
