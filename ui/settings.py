"""使用者設定 (ui/settings.py)：記在 config/WingLightGIS.json。

目前記深淺色主題、側欄有沒有打開測試版工具。檔案裡可能有使用者的資料夾路徑 (之後的設定)，已加進 .gitignore，不提交。
讀不到或內容壞掉時當成沒有設定，不影響啟動。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from core.env_runner import get_config_dir
from ui.app_info import APP_NAME

logger = logging.getLogger("ui.settings")

APPEARANCE_KEY: str = "appearance"
LAB_TOOLS_KEY: str = "lab_tools"
# 存在設定檔裡的值 → CustomTkinter 的外觀模式
APPEARANCE_MODES: Dict[str, str] = {"light": "Light", "dark": "Dark"}


def settings_path() -> Path:
    return get_config_dir() / f"{APP_NAME}.json"


def load_settings() -> Dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        logger.warning("設定檔讀取失敗，改用預設值: %s", err)
        return {}
    return data if isinstance(data, dict) else {}


def save_setting(key: str, value: Any) -> None:
    """改一個設定值並寫回檔案；寫不進去只記警告 (例如程式放在唯讀位置)。"""
    data = load_settings()
    data[key] = value
    try:
        settings_path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as err:
        logger.warning("設定檔寫入失敗: %s", err)


def saved_appearance_mode() -> Optional[str]:
    """使用者選過的外觀 ("Light"／"Dark")；沒選過時為 None (第一次開啟跟著 Windows)。"""
    return APPEARANCE_MODES.get(str(load_settings().get(APPEARANCE_KEY, "")).lower())


def save_appearance_mode(mode: str) -> None:
    save_setting(APPEARANCE_KEY, "dark" if mode.lower() == "dark" else "light")


def saved_lab_tools_enabled() -> bool:
    """側欄有沒有打開測試版工具；沒設定過是關的。"""
    return load_settings().get(LAB_TOOLS_KEY) is True


def save_lab_tools_enabled(enabled: bool) -> None:
    save_setting(LAB_TOOLS_KEY, bool(enabled))
