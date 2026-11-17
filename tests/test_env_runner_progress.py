"""run_command 進度解析與取消 整合測試 (tests/test_env_runner_progress.py)

1. 進度只認「整行都是進度」的輸出 (GDAL 點狀進度列、WhiteboxTools 的「標題: 45%」)，
   訊息裡碰巧出現的百分比、刪節號、done 不算。
2. 取消事件可經 cancellation_scope／run_in_background 傳給引擎內層的 run_command，引擎函式不必逐層傳遞。
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, List, Optional

import numpy as np

from core import env_runner
from core.env_runner import (
    CommandCancelledError,
    ToolkitError,
    cancellation_scope,
    get_active_cancel_event,
    run_command,
    run_in_background,
)
from tests._support import IsolatedWorkspaceTestCase, write_dem

SLEEP_SCRIPT = "import time; time.sleep(60)"


def python_print_script(pieces: List[str], stream: str = "stdout", exit_code: int = 0) -> List[str]:
    """回傳一個逐段印出 pieces (每段之間稍停並 flush) 的 python 指令。"""
    script = (
        "import sys, time\n"
        f"out = sys.{stream}\n"
        f"for piece in {pieces!r}:\n"
        "    out.write(piece); out.flush(); time.sleep(0.03)\n"
        f"sys.exit({exit_code})\n"
    )
    return ["python", "-c", script]


class ProgressParsingTest(IsolatedWorkspaceTestCase):
    """進度回呼只反映真正的進度列。"""

    def _run(self, cmd: List[str], **kwargs: Any) -> List[float]:
        values: List[float] = []
        run_command(cmd, progress_callback=lambda percent, message: values.append(percent), **kwargs)
        return values

    def test_gdal_dotted_progress_is_reported_in_order(self) -> None:
        pieces = ["0", ".", ".", ".", "10", "...", "20...30", "...40...50...60...70...80...90...", "100", " - done.\n"]

        values = self._run(python_print_script(pieces))

        # 幾段輸出可能在同一次讀取裡到齊，只保證：都是 10 的倍數、不倒退、最後到 100
        self.assertGreaterEqual(len(values), 4)
        self.assertTrue(set(values) <= {10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0})
        self.assertEqual(values, sorted(values))
        self.assertIn(10.0, values)
        self.assertEqual(values[-1], 100.0)

    def test_number_split_across_reads_is_not_reported_half_way(self) -> None:
        # 100 被拆成 "1" 與 "00" 兩次送出：不可先回報 1%
        values = self._run(python_print_script(["0...50...9", "0...1", "00 - done.\n"]))

        self.assertTrue(set(values) <= {50.0, 90.0, 100.0})
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 100.0)

    def test_messages_that_merely_contain_percent_or_done_are_ignored(self) -> None:
        pieces = [
            "Warning 1: 50% of the features were skipped\n",
            "writing D:/data/abandoned_30...tif\n",
            "3 files done\n",
            "slope 45%\n",
            "ratio: 12% of total, see log\n",
        ]

        values = self._run(python_print_script(pieces, exit_code=1), check=False)

        self.assertEqual(values, [])

    def test_progress_like_text_on_stderr_is_ignored(self) -> None:
        values = self._run(python_print_script(["0...10...20...30\n", "Progress: 77%\n"], stream="stderr", exit_code=1), check=False)

        self.assertEqual(values, [])

    def test_labeled_percent_lines_are_reported(self) -> None:
        pieces = ["Reading data...\n", "Progress: 0%\n", "Progress: 45%\n", "Flow accumulation (loop 1 of 2): 80%\n", "Elapsed Time: 0.1s\n"]

        values = self._run(python_print_script(pieces))

        self.assertTrue(set(values) <= {0.0, 45.0, 80.0, 100.0})
        self.assertIn(80.0, values)
        self.assertEqual(values[-1], 100.0)

    def test_failed_command_does_not_report_completion(self) -> None:
        values = self._run(python_print_script(["0...10...20", "\nERROR: abandoned\n"], exit_code=1), check=False)

        self.assertTrue(values)
        self.assertTrue(set(values) <= {10.0, 20.0})

    def test_real_gdal_translate_progress_reaches_100(self) -> None:
        dem = write_dem(self.data_dir / "dem.tif", np.arange(200 * 200, dtype=np.float32).reshape(200, 200))

        values = self._run(["gdal_translate", "-of", "GTiff", str(dem), str(self.out_dir / "copy.tif")])

        self.assertGreaterEqual(len(values), 5)
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 100.0)
        self.assertTrue(all(value % 10 == 0 for value in values))


class CancellationTest(IsolatedWorkspaceTestCase):
    """取消事件要能終止引擎內層的 CLI。"""

    def _cancel_after(self, event: threading.Event, seconds: float) -> None:
        threading.Timer(seconds, event.set).start()

    def test_explicit_cancel_event_kills_running_command(self) -> None:
        event = threading.Event()
        self._cancel_after(event, 0.5)
        started = time.time()

        with self.assertRaises(CommandCancelledError):
            run_command(["python", "-c", SLEEP_SCRIPT], cancel_event=event)

        self.assertLess(time.time() - started, 10.0)

    def test_scope_cancels_commands_that_do_not_pass_the_event(self) -> None:
        event = threading.Event()
        self._cancel_after(event, 0.5)
        logs: List[str] = []
        started = time.time()

        with cancellation_scope(event):
            with self.assertRaises(CommandCancelledError):
                run_command(["python", "-c", SLEEP_SCRIPT])
            # 已取消後不再啟動新的 CLI
            with self.assertRaises(CommandCancelledError):
                run_command(["python", "-c", "print('不該執行')"], text_callback=logs.append)

        self.assertLess(time.time() - started, 10.0)
        self.assertEqual(logs, [])
        self.assertIsNone(get_active_cancel_event())
        # 離開範圍後同一個執行緒照常執行
        self.assertEqual(run_command(["python", "-c", "print('ok')"]).stdout.strip(), "ok")

    def test_nested_scope_restores_outer_event(self) -> None:
        outer, inner = threading.Event(), threading.Event()
        with cancellation_scope(outer):
            with cancellation_scope(inner):
                self.assertIs(get_active_cancel_event(), inner)
            self.assertIs(get_active_cancel_event(), outer)

    def test_scope_does_not_leak_to_other_threads(self) -> None:
        event = threading.Event()
        event.set()
        seen: List[Optional[threading.Event]] = []
        with cancellation_scope(event):
            worker = threading.Thread(target=lambda: seen.append(get_active_cancel_event()))
            worker.start()
            worker.join()

        self.assertEqual(seen, [None])

    def _run_background(self, target: Any, event: threading.Event) -> dict:
        outcome: dict = {"success": [], "error": [], "finished": threading.Event()}
        run_in_background(
            target,
            on_success=outcome["success"].append,
            on_error=outcome["error"].append,
            on_finish=outcome["finished"].set,
            cancel_event=event,
        )
        return outcome

    def test_background_task_is_cancelled_through_engine_layers(self) -> None:
        steps: List[str] = []

        def engine() -> str:
            steps.append("第一步")
            run_command(["python", "-c", SLEEP_SCRIPT])
            steps.append("第二步")
            return "完成"

        event = threading.Event()
        outcome = self._run_background(engine, event)
        self._cancel_after(event, 0.5)

        self.assertTrue(outcome["finished"].wait(15.0))
        self.assertEqual(steps, ["第一步"])
        self.assertEqual(outcome["success"], [])
        self.assertEqual(len(outcome["error"]), 1)
        self.assertIsInstance(outcome["error"][0], CommandCancelledError)

    def test_background_task_that_swallows_errors_still_reports_cancel(self) -> None:
        def engine_with_fallback() -> str:
            try:
                run_command(["python", "-c", SLEEP_SCRIPT])
            except ToolkitError:
                # 模擬引擎的「失敗就改走備援路徑」
                try:
                    run_command(["python", "-c", SLEEP_SCRIPT])
                except ToolkitError:
                    return "備援也失敗，回傳部分成果"
            return "完成"

        event = threading.Event()
        outcome = self._run_background(engine_with_fallback, event)
        self._cancel_after(event, 0.5)

        self.assertTrue(outcome["finished"].wait(15.0))
        self.assertEqual(outcome["success"], [])
        self.assertIsInstance(outcome["error"][0], CommandCancelledError)

    def test_background_task_without_cancel_event_behaves_as_before(self) -> None:
        outcome: dict = {"success": [], "error": [], "finished": threading.Event()}
        run_in_background(
            lambda text, repeat=1: text * repeat,
            "甲",
            repeat=2,
            on_success=outcome["success"].append,
            on_error=outcome["error"].append,
            on_finish=outcome["finished"].set,
        )

        self.assertTrue(outcome["finished"].wait(15.0))
        self.assertEqual(outcome["success"], ["甲甲"])
        self.assertEqual(outcome["error"], [])

    def test_raise_if_cancelled_only_inside_cancelled_scope(self) -> None:
        env_runner.raise_if_cancelled()
        event = threading.Event()
        with cancellation_scope(event):
            env_runner.raise_if_cancelled()
            event.set()
            with self.assertRaises(CommandCancelledError):
                env_runner.raise_if_cancelled()


if __name__ == "__main__":
    unittest.main()
