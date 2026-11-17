"""量啟動時間 (tools/measure_startup.py)：模擬點兩下 WingLightGIS.exe，量啟動畫面與主視窗出現的時間。

規範「打包與發佈」：對外寫的數字都要實際量過，量打包後的執行檔、從點兩下到主視窗出現、連續量 3 次；
剛複製或剛編好的第一次會被防毒軟體拖慢，不能當代表值，所以先跑暖機 (不算)。

量法：
- 模擬點兩下：開新行程、工作資料夾設成 exe 所在資料夾、拿掉 PYTHON 開頭的環境變數
  (開發時的終端機常帶著 PYTHONPATH 等，點兩下時沒有)。
- 每 5 ms 列舉一次視窗：「啟動畫面出現」＝啟動器自己的「WingLightGIS 啟動中」視窗可見；
  「主視窗出現」＝新出現的、標題剛好是 WingLightGIS 的可見視窗 (不是啟動器的)；
  「啟動畫面關閉」＝啟動畫面不再可見 (主程式通知主視窗畫好、主程式結束或 15 秒到)。
- 量完送 WM_CLOSE 關掉主視窗，等它結束。只關這次新開出來的視窗 (開始前就有的同名視窗不碰)。
- 截圖 (--shots)：用 Win32 PrintWindow 截啟動畫面 (不抓螢幕範圍)。分層視窗偶爾截到空白，所以多截幾張，空白的會標出來。

用法：
    runtime\\python.exe tools\\measure_startup.py                 # 暖機 1 次＋量 3 次
    runtime\\python.exe tools\\measure_startup.py --runs 5 --warmup 0
    runtime\\python.exe tools\\measure_startup.py --shots 截圖資料夾  # 最後一次另外截啟動畫面
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageStat

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = PROJECT_ROOT / "WingLightGIS.exe"
SPLASH_TITLE = "WingLightGIS 啟動中"
MAIN_TITLE = "WingLightGIS"
POLL_SECONDS = 0.005
TIMEOUT_SECONDS = 40.0
SHOT_TIMES: Tuple[float, ...] = (0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
_EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WM_CLOSE = 0x0010
SYNCHRONIZE = 0x00100000
PW_RENDERFULLCONTENT = 2


@dataclass
class RunResult:
    splash: Optional[float]
    main: Optional[float]
    splash_closed: Optional[float]
    exit_code: Optional[int]


def visible_windows() -> List[Tuple[int, str, int]]:
    """所有可見、有標題的頂層視窗：(視窗代號, 標題, 行程 PID)。"""
    found: List[Tuple[int, str, int]] = []

    def on_window(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd, buf.value, pid.value))
        return True

    user32.EnumWindows(_EnumProc(on_window), 0)
    return found


def capture_window(hwnd: int, path: Path) -> bool:
    """PrintWindow 截一個視窗存成 PNG；截到整張同一個顏色 (空白) 回傳 False。"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old = gdi32.SelectObject(memory_dc, bitmap)
    try:
        user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
        buffer = ctypes.create_string_buffer(width * height * 4)
        header = (ctypes.c_uint32 * 10)(40, width, -height, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
        gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, header, 0)
    finally:
        gdi32.SelectObject(memory_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
    image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
    image.save(path)
    return max(ImageStat.Stat(image).stddev) > 1


def _close_and_wait(hwnd: int, pid: int) -> None:
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        kernel32.WaitForSingleObject(handle, 15000)
        kernel32.CloseHandle(handle)


def one_run(exe: Path, shot_dir: Optional[Path] = None) -> RunResult:
    before = {pid for _, _, pid in visible_windows()}
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    start = time.perf_counter()
    launcher = subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent))
    result = RunResult(None, None, None, None)
    main_window: Optional[Tuple[int, int]] = None
    shots = list(SHOT_TIMES) if shot_dir else []
    while time.perf_counter() - start < TIMEOUT_SECONDS:
        now = time.perf_counter() - start
        windows = visible_windows()
        splash = [hwnd for hwnd, title, pid in windows if title == SPLASH_TITLE and pid == launcher.pid]
        if splash and result.splash is None:
            result.splash = now
        if splash and shots and now - result.splash >= shots[0]:
            path = shot_dir / f"splash_{shots[0]:.1f}s.png"
            ok = capture_window(splash[0], path)
            print(f"  截圖 {path.name}{'' if ok else '（空白，PrintWindow 沒截到）'}")
            shots.pop(0)
        if main_window is None:
            mains = [(hwnd, pid) for hwnd, title, pid in windows
                     if title == MAIN_TITLE and pid not in before and pid != launcher.pid]
            if mains:
                result.main = now
                main_window = mains[0]
        if result.splash is not None and not splash and result.splash_closed is None:
            result.splash_closed = now
        if main_window is not None and launcher.poll() is not None and (result.splash is None or result.splash_closed):
            break
        time.sleep(POLL_SECONDS)
    try:
        result.exit_code = launcher.wait(timeout=20)
    except subprocess.TimeoutExpired:
        result.exit_code = None
    # 讓主程式把啟動後的工作做完再關
    time.sleep(1.0)
    if main_window is not None:
        _close_and_wait(*main_window)
    return result


def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.2f}"


def summary(results: Sequence[RunResult]) -> List[str]:
    """給 commit 說明用的三行 (斜線分隔每次的秒數)。"""
    def join(values: Sequence[Optional[float]]) -> str:
        return "／".join(_fmt(v) for v in values)

    return [
        f"啟動畫面出現 {join([r.splash for r in results])} 秒",
        f"主視窗出現 {join([r.main for r in results])} 秒",
        f"啟動畫面關閉 {join([r.splash_closed for r in results])} 秒",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="模擬點兩下 WingLightGIS.exe，量啟動畫面與主視窗出現的時間")
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE, help="要量的執行檔 (預設專案資料夾的 WingLightGIS.exe)")
    parser.add_argument("--runs", type=int, default=3, help="正式量的次數 (預設 3)")
    parser.add_argument("--warmup", type=int, default=1, help="先跑幾次暖機、不算 (預設 1)")
    parser.add_argument("--shots", type=Path, help="最後一次另外截啟動畫面到這個資料夾")
    args = parser.parse_args()
    exe = args.exe.resolve()
    if not exe.is_file():
        raise SystemExit(f"找不到 {exe}，請先執行 launcher\\build_launcher.bat")
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)

    results: List[RunResult] = []
    total = args.warmup + args.runs
    for index in range(total):
        warmup = index < args.warmup
        last = index == total - 1
        result = one_run(exe, args.shots if last else None)
        label = "暖機" if warmup else f"第 {index - args.warmup + 1} 次"
        print(f"{label}: 啟動畫面出現 {_fmt(result.splash)} 秒｜主視窗出現 {_fmt(result.main)} 秒｜"
              f"啟動畫面關閉 {_fmt(result.splash_closed)} 秒｜啟動器結束代碼 {result.exit_code}", flush=True)
        if not warmup:
            results.append(result)
        time.sleep(2)
    print("連續 {} 次：".format(len(results)))
    for line in summary(results):
        print("  " + line)


if __name__ == "__main__":
    main()
