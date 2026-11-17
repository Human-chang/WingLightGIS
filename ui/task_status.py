"""背景作業的狀態 (ui/task_status.py)：PanelInspector 執行作業時通知底部動作列 (ActionBar) 顯示。"""

from __future__ import annotations

from dataclasses import dataclass

# 狀態：idle 沒有作業、running 執行中、cancelling 取消中、done 完成、cancelled 已取消、failed 失敗
TASK_STATES = ("idle", "running", "cancelling", "done", "cancelled", "failed")


@dataclass(frozen=True)
class TaskStatus:
    state: str = "idle"
    label: str = ""
    message: str = ""

    @property
    def busy(self) -> bool:
        return self.state in ("running", "cancelling")

    def text(self) -> str:
        """給狀態列看的一句話。"""
        if self.state == "running":
            return f"{self.label}：{self.message}" if self.message else f"{self.label}：執行中"
        if self.state == "cancelling":
            return f"{self.label}：取消中"
        if self.state == "done":
            return f"{self.label}：完成"
        if self.state == "cancelled":
            return f"{self.label}：已取消"
        if self.state == "failed":
            return f"{self.label}：失敗，原因寫在跳出的訊息與處理紀錄"
        return ""
