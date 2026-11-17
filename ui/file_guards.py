"""
檔案選取防呆 (ui/file_guards.py)
====================================================
GDAL 讀不了 AutoCAD 的 .dwg 與 MicroStation 的 .dgn 原生格式：使用者選到這類檔案時，
各分頁統一用這裡的函式把它們剔除，並取得一致的提示文字。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

UNSUPPORTED_CAD_EXTENSIONS = frozenset({".dwg", ".dgn"})
UNSUPPORTED_CAD_MESSAGE: str = "為維持軟體極致輕量與穩定，請先於 AutoCAD / MicroStation 將檔案另存為 DXF (.dxf) 再行載入。"
# 提示文字最多列出幾個被剔除的檔名
MAX_LISTED_NAMES: int = 5


def is_unsupported_cad(path: Union[str, Path]) -> bool:
    """副檔名是 .dwg 或 .dgn 時回傳 True。"""
    return Path(path).suffix.lower() in UNSUPPORTED_CAD_EXTENSIONS


def validate_supported_files(paths: Iterable[Union[str, Path]]) -> Tuple[List[Path], Optional[str]]:
    """剔除 .dwg／.dgn，回傳 (可用的檔案, 給使用者看的提示)；沒有被剔除的檔案時提示為 None。"""
    kept: List[Path] = []
    rejected: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if is_unsupported_cad(path):
            rejected.append(path)
        else:
            kept.append(path)
    if not rejected:
        return kept, None
    names = "、".join(p.name for p in rejected[:MAX_LISTED_NAMES])
    if len(rejected) > MAX_LISTED_NAMES:
        names += f" 等 {len(rejected)} 個"
    return kept, f"{UNSUPPORTED_CAD_MESSAGE}\n\n已略過: {names}"
