"""卡片共用的「輸出資料夾」列 (ui/output_dir_row.py)
====================================================
每張工具卡片都放一列，讓使用者自選成果要寫到哪個資料夾；
留空時依 ui/output_paths.py 的規則跟著來源檔走。
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog
from typing import Any, Optional, Union

import customtkinter as ctk

from ui import drag_drop
from ui.output_paths import result_folder_beside
from ui.theme_constants import BTN_SECONDARY

OUTPUT_DIR_LABEL: str = "輸出資料夾:"
OUTPUT_DIR_PLACEHOLDER: str = "留空＝寫在來源檔旁；也可自行指定資料夾"


class OutputDirRow:
    """一列「輸出資料夾」標籤＋輸入框＋瀏覽鈕，佔用卡片的第 0～2 欄。"""

    def __init__(self, card: Any, *, row: int, font: Optional[ctk.CTkFont] = None) -> None:
        self.label = ctk.CTkLabel(card, text=OUTPUT_DIR_LABEL, font=font)
        self.label.grid(row=row, column=0, padx=6, pady=2, sticky="w")
        self.entry = ctk.CTkEntry(card, placeholder_text=OUTPUT_DIR_PLACEHOLDER)
        self.entry.grid(row=row, column=1, padx=4, pady=2, sticky="ew")
        drag_drop.bind_folder_entry(self.entry)
        self.button = ctk.CTkButton(card, text="選資料夾", width=65, command=self._browse, **BTN_SECONDARY)
        self.button.grid(row=row, column=2, padx=6, pady=2)

    def _browse(self) -> None:
        folder = filedialog.askdirectory(title="選取成果輸出資料夾")
        if not folder:
            return
        self.set(str(Path(folder).resolve()))

    def set(self, value: str) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def chosen(self) -> Optional[Path]:
        """使用者自選的資料夾；留空時為 None。指到既有檔案時 raise ValueError。"""
        text = self.entry.get().strip().strip('"')
        if not text:
            return None
        folder = Path(text).resolve()
        if folder.is_file():
            raise ValueError(f"輸出資料夾指到的是一個檔案，請改選資料夾：\n{folder}")
        return folder

    def folder_for(self, source_path: Union[str, Path], result_label: str) -> Path:
        """多檔成果的資料夾：自選的資料夾，或來源檔旁的「<來源檔名>_<成果名稱>」(不建立資料夾)。"""
        return self.chosen() or result_folder_beside(source_path, result_label)

    def file_for(self, default_path: Union[str, Path]) -> Path:
        """單一成果的路徑：自選資料夾＋預設檔名，或原本的預設路徑 (不建立資料夾)。"""
        default = Path(default_path)
        folder = self.chosen()
        return folder / default.name if folder is not None else default
