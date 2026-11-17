"""把檔案或資料夾從檔案總管拖進輸入框與清單 (ui/drag_drop.py)
====================================================
主視窗建立元件前先呼叫 enable_drag_drop(root) 載入 tkdnd；載入失敗 (或測試時沒有主視窗)
其餘函式都不做事，按鈕選檔照常可用。
- bind_paths：拖入的所有路徑交給 callback (清單用)。
- bind_file_entry：輸入框只收一個檔案。
- bind_folder_entry：輸入框只收資料夾；拖入檔案時改填它所在的資料夾。
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable, List, Optional

from ui.file_guards import validate_supported_files

logger = logging.getLogger("ui.drag_drop")


def enable_drag_drop(root: Any) -> bool:
    """在主視窗載入 tkdnd；成功回傳 True。"""
    try:
        from tkinterdnd2 import TkinterDnD

        TkinterDnD._require(root)
    except Exception as err:  # 只是不能拖放，不影響其他功能
        logger.warning("拖放功能無法啟用: %s", err)
        return False
    return True


def is_enabled(widget: Any) -> bool:
    """widget 所在的視窗是否已載入 tkdnd (每個主視窗各自載入，不用全域旗標)。"""
    try:
        return bool(widget.tk.call("package", "provide", "tkdnd"))
    except Exception:
        return False


def parse_drop_data(widget: Any, data: str) -> List[Path]:
    """把 tkdnd 的拖放字串 (含空白的路徑以大括號包住) 拆成路徑清單。"""
    return [Path(item) for item in widget.tk.splitlist(data) if item]


def bind_paths(widget: Any, callback: Callable[[List[Path]], None]) -> bool:
    """讓 widget 接受拖放，拖入的路徑清單交給 callback；未啟用時回傳 False。"""
    if not is_enabled(widget):
        return False
    from tkinterdnd2 import COPY, DND_FILES

    def _on_drop(event: Any) -> str:
        paths = parse_drop_data(widget, event.data)
        if paths:
            try:
                callback(paths)
            except Exception as err:
                logger.exception("處理拖放失敗")
                messagebox.showerror("錯誤", f"處理拖入的檔案失敗:\n{err}")
        return COPY

    # CTk 元件外層是框架，真正的輸入框／文字框／文字標籤與底圖畫布在內層，都註冊才不會漏接
    inner = (getattr(widget, name, None) for name in ("_entry", "_textbox", "_label", "_canvas"))
    for target in (widget, *inner):
        if target is not None:
            target.drop_target_register(DND_FILES)
            target.dnd_bind("<<Drop>>", _on_drop)
    return True


def _set_entry(entry: Any, value: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, value)


def bind_file_entry(entry: Any, on_file: Optional[Callable[[Path], None]] = None) -> bool:
    """輸入框接受拖入一個檔案：填入路徑；有 on_file 時改交給它處理。"""

    def _accept(paths: List[Path]) -> None:
        path = paths[0]
        if path.is_dir():
            messagebox.showwarning("警告", f"這裡要拖入檔案，不是資料夾：\n{path}")
            return
        _, cad_warning = validate_supported_files([path])
        if cad_warning:
            messagebox.showwarning("警告", cad_warning)
            return
        if len(paths) > 1:
            logger.info("一次拖入 %d 個檔案，只採用第一個: %s", len(paths), path)
        if on_file is not None:
            on_file(path.resolve())
        else:
            _set_entry(entry, str(path.resolve()))

    return bind_paths(entry, _accept)


def bind_folder_entry(entry: Any) -> bool:
    """輸入框接受拖入資料夾；拖入檔案時填它所在的資料夾。"""

    def _accept(paths: List[Path]) -> None:
        path = paths[0]
        folder = path if path.is_dir() else path.parent
        _set_entry(entry, str(folder.resolve()))

    return bind_paths(entry, _accept)
