"""
工具卡片切換器 (ui/lab/tool_switcher.py)
====================================================
測試版工具兩頁共用：頂部一列 CTkSegmentedButton，下方一次只顯示被選中的那個工具的卡片，
其餘卡片以 grid_remove 隱藏 (輸入內容保留)，不再把所有卡片堆成一長條。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import customtkinter as ctk


class ToolCardSwitcher:
    """管理「工具名稱 → 一張或多張卡片」的顯示切換。"""

    def __init__(self, parent: Any, tool_names: Sequence[str], *, row: int, font: Optional[ctk.CTkFont] = None) -> None:
        """在 parent 的第 row 列放切換鈕；卡片會依序排在 row + 1 之後。"""
        if not tool_names:
            raise ValueError("工具卡片切換器至少要有一個工具名稱！")
        self._tool_names: List[str] = list(tool_names)
        self._first_card_row: int = row + 1
        self._cards: Dict[str, List[Any]] = {name: [] for name in self._tool_names}
        self._current: Optional[str] = None

        self.button = ctk.CTkSegmentedButton(
            parent,
            values=self._tool_names,
            command=self.show,
            font=font,
            dynamic_resizing=False,
        )
        self.button.grid(row=row, column=0, sticky="ew", padx=6, pady=(4, 2))

    @property
    def tool_names(self) -> List[str]:
        return list(self._tool_names)

    @property
    def current(self) -> Optional[str]:
        """目前顯示的工具名稱；還沒顯示任何卡片時為 None。"""
        return self._current

    def register(self, tool_name: str, card: Any) -> None:
        """把一張卡片掛到某個工具底下 (同一個工具可以掛多張，會由上往下排)；卡片先保持隱藏。"""
        if tool_name not in self._cards:
            raise KeyError(f"沒有這個工具名稱: {tool_name}")
        self._cards[tool_name].append(card)

    def cards_of(self, tool_name: str) -> List[Any]:
        return list(self._cards[tool_name])

    def show(self, tool_name: str) -> None:
        """只顯示指定工具的卡片，並讓切換鈕停在該項。"""
        if tool_name not in self._cards:
            raise KeyError(f"沒有這個工具名稱: {tool_name}")
        for name, cards in self._cards.items():
            if name != tool_name:
                for card in cards:
                    card.grid_remove()
        for offset, card in enumerate(self._cards[tool_name]):
            card.grid(row=self._first_card_row + offset, column=0, sticky="ew", padx=6, pady=(4, 6))
        self._current = tool_name
        if self.button.get() != tool_name:
            self.button.set(tool_name)
