"""App 圖示與工作列 (ui/app_icon.py)：主視窗與子視窗的標題列圖示，以及工作列用的程式識別碼。

- 圖示檔 assets/app_icon.ico 由 tools/make_app_icon.py 從規範的 winglightgis.svg 產生。
- 工作列：程式是 pythonw.exe 跑的，不設識別碼的話工作列會把它當成 Python、用 Python 的圖示。
  SetCurrentProcessExplicitAppUserModelID 只影響本行程，不寫登錄檔；要在建立任何視窗之前呼叫。
- CustomTkinter 的視窗 (CTk、CTkToplevel) 建立 200 ms 後會換上它自己的藍色圖示，
  除非已經呼叫過 iconbitmap，所以每個視窗建立後都要呼叫 apply_window_icon。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tkinter as tk
from pathlib import Path

logger = logging.getLogger("ui.app_icon")

APP_ICON_PATH: Path = Path(__file__).resolve().parent.parent / "assets" / "app_icon.ico"
APP_USER_MODEL_ID: str = "HumanChang.WingLightGIS"


def set_app_user_model_id() -> bool:
    """讓工作列把本行程當成 WingLightGIS；成功回傳 True (不是 Windows 或呼叫失敗回傳 False)。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        return ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID) == 0
    except (AttributeError, OSError) as exc:
        logger.warning("無法設定工作列識別碼: %s", exc)
        return False


def apply_window_icon(window: tk.Wm, *, as_default: bool = False) -> bool:
    """視窗標題列與工作列換成 App 圖示；as_default=True 時之後新建的純 Tk 子視窗也用它。

    圖示檔不見或讀不了時只記警告 (視窗照常開)，回傳 False。
    """
    if not APP_ICON_PATH.is_file():
        logger.warning("找不到 App 圖示: %s", APP_ICON_PATH)
        return False
    try:
        # 給了 default 時 tkinter 只送 -default (改的是視窗類別的圖示)，所以這個視窗自己的圖示要另外設一次
        window.iconbitmap(str(APP_ICON_PATH))
        if as_default:
            window.iconbitmap(default=str(APP_ICON_PATH))
    except tk.TclError as exc:
        logger.warning("無法套用 App 圖示: %s", exc)
        return False
    return True
