"""WingLightGIS 應用程式唯一主入口 (main.py)

嚴格遵守專案規格書第 0 節鐵律：
1. 目標電腦安裝有舊版 ArcMap 10.x (32-bit Python 2.7)，絕對禁止更動 Windows 全域環境變數。
2. 啟動時主動清理 Python 記憶體內可能殘留之 PYTHONHOME 與宿主環境變數。
3. 繫結沙盒獨立目錄 ./runtime/Library/bin 並啟動 CustomTkinter 現代雙欄卡片式介面。
4. 命令列帶的檔案路徑 (例如設成 .shp 的預設開啟程式後點兩下) 加進圖資快覽；已經有一份開著時
   交給那一份後結束，不另開視窗 (ui/single_instance.py)。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

# 將專案根目錄加入模組搜尋路徑
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 配置全域日誌記錄格式
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def sanitize_runtime_environment() -> None:
    """清理宿主可能存在的 Python 2.7 或 ArcMap 全域殘留變數。"""
    for harmful_var in ("PYTHONHOME", "GDAL_DRIVER_PATH"):
        if harmful_var in os.environ:
            logger.info("已自執行期環境中移除潛在污染變數: %s", harmful_var)
            os.environ.pop(harmful_var, None)

    # 強制 UTF-8 編碼與 GDAL 檔名相容性
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["GDAL_FILENAME_IS_UTF8"] = "YES"
    # 全域 SHAPE_ENCODING 會蓋掉 Shapefile 的 .cpg 與輸出編碼設定；系統或別的軟體可能設了它，這裡從本行程移除
    os.environ.pop("SHAPE_ENCODING", None)


def command_line_paths(argv: Sequence[str]) -> List[str]:
    """命令列帶的檔案路徑，轉成完整路徑 (第一份程式的工作資料夾可能不同)；略過 -- 開頭的選項 (例如 --debug)。"""
    return [os.path.abspath(arg) for arg in argv if arg and not arg.startswith("--")]


def _parse_self_test_path(argv: Sequence[str]) -> Optional[Path]:
    """檢查命令列是否帶有 --self-test [<結果.json>]，若有則回傳輸出報告路徑。"""
    for idx, arg in enumerate(argv):
        if arg == "--self-test":
            next_arg = argv[idx + 1] if idx + 1 < len(argv) else ""
            if next_arg and not next_arg.startswith("--"):
                return Path(next_arg)
            return Path("selftest_result.json")
        if arg.startswith("--self-test="):
            value = arg.split("=", 1)[1].strip()
            return Path(value or "selftest_result.json")
    return None


def _show_startup_error(title: str, message: str) -> None:
    # 先關掉啟動器的螢火蟲啟動畫面，錯誤訊息才不會被蓋住
    try:
        from ui.launcher_link import notify_launcher
        notify_launcher()
    except Exception:
        logger.exception("無法通知啟動器")
    try:
        import tkinter.messagebox as msgbox
        msgbox.showerror(title, message)
    except Exception:
        print(f"\n【嚴重錯誤】{message}\n", file=sys.stderr)


def main() -> None:
    """主程式啟動入口函式。"""
    logger.info("正在啟動 WingLightGIS。")
    sanitize_runtime_environment()

    # 發佈自我測試：在單一執行個體檢查之前攔截，避免把報告檔名當成要開啟的圖資丟給已開啟視窗
    self_test_report = _parse_self_test_path(sys.argv[1:])
    if self_test_report is not None:
        from core.selftest import run_self_test
        sys.exit(run_self_test(self_test_report))

    paths = command_line_paths(sys.argv[1:])

    # 已經有一份開著：把檔案交給它就結束 (要在載入介面之前，才快)
    from ui.single_instance import HandOffError, claim_or_hand_off
    server = None
    try:
        server = claim_or_hand_off(paths)
        if server is None:
            logger.info("WingLightGIS 已經開著，%d 個檔案交給那一份。", len(paths))
            return
    except HandOffError as exc:
        _show_startup_error("WingLightGIS", str(exc))
        sys.exit(1)
    except Exception:
        # 判斷不了就照常開一份 (只是不能把之後開的檔交進來)
        logger.exception("單一執行個體檢查失敗，照常啟動")

    # 工作列把本程式當成 WingLightGIS (不是 pythonw.exe)；要在建立任何視窗之前設
    from ui.app_icon import set_app_user_model_id
    set_app_user_model_id()

    # 匯入前端主視窗模組並啟動 UI 事件主迴圈
    try:
        from ui.dashboard_app import launch_app
        launch_app(paths, server)
    except Exception as exc:
        logger.exception("應用程式啟動時發生未預期異常: %s", exc)
        _show_startup_error(
            "啟動錯誤",
            f"WingLightGIS 啟動失敗：\n{exc}\n\n請確認是否已執行 setup_portable_env.bat 配置獨立執行環境。",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
