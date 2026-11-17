"""通知啟動器關掉啟動畫面 (ui/launcher_link.py)。

WingLightGIS.exe 先啟動主程式、再畫螢火蟲啟動畫面 (launcher/WingLightGIS.cs)，兩邊同時進行。
啟動器建立一個有名字的 Windows 事件，名字放在環境變數 WINGLIGHTGIS_READY_EVENT 傳給主程式；
主視窗第一次畫好、或啟動失敗要跳錯誤訊息之前，主程式呼叫 notify_launcher() 把事件設起來，
啟動器看到就關掉啟動畫面 (主程式結束或等了 15 秒也會關)。

不是從啟動器開的 (例如開發時直接跑 main.py、測試) 就沒有這個環境變數，什麼都不做。

這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/launcher_link.py 是同一份，改了要兩邊一起改
(其他工具把 READY_EVENT_ENV 換成自己的名字，跟啟動器的 ReadyEventEnv 一致)。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys

logger = logging.getLogger("ui.launcher_link")

READY_EVENT_ENV: str = "WINGLIGHTGIS_READY_EVENT"
_EVENT_MODIFY_STATE = 0x0002


def notify_launcher() -> bool:
    """叫啟動器關掉啟動畫面；有通知到回傳 True。只通知一次 (之後的呼叫回傳 False)。

    環境變數讀完就從本行程移除，主程式之後開的子行程 (GDAL 指令等) 不會帶著它。
    """
    name = os.environ.pop(READY_EVENT_ENV, None)
    if not name or not sys.platform.startswith("win"):
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wt.DWORD, wt.BOOL, wt.LPCWSTR)
    kernel32.OpenEventW.restype = wt.HANDLE
    kernel32.SetEvent.argtypes = (wt.HANDLE,)
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
    if not handle:
        # 啟動器已經等到上限先結束了，事件跟著消失；不影響主程式
        logger.info("啟動器的事件已不存在 (錯誤碼 %s)", ctypes.get_last_error())
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)
