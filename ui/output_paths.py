"""成果輸出位置規則 (ui/output_paths.py)
====================================================
三個分頁共用同一套規則，成果一律跟著來源圖資走，不寫進會被清理的暫存區：
- 只產出一個檔案 (或一組同名檔) 的工具：寫在來源檔同資料夾，檔名加上後綴。
- 一次產出多個檔案的工具 (報表＋圖層＋影像)：寫在來源檔同資料夾底下的「<來源檔名>_<成果名稱>」子資料夾。
- 批次裁切由使用者指定輸出資料夾；預設值是工具箱根目錄下的 outputs/clipped_outputs。
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from core.env_runner import get_default_output_dir

CLIP_OUTPUT_FOLDER_NAME: str = "clipped_outputs"


def result_folder_beside(source_path: Union[str, Path], result_label: str) -> Path:
    """回傳來源檔旁的成果子資料夾路徑「<來源檔名>_<成果名稱>」(不建立資料夾)。"""
    label = result_label.strip()
    if not label:
        raise ValueError("成果名稱不可為空白！")
    source = Path(source_path).resolve()
    return source.parent / f"{source.stem}_{label}"


def default_clip_output_dir() -> Path:
    """批次裁切的預設輸出資料夾 (不建立資料夾)。"""
    return get_default_output_dir() / CLIP_OUTPUT_FOLDER_NAME
