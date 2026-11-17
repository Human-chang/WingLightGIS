"""底部動作列 (ui/action_bar.py)：照 WingLight 規範的 ActionBar 元件。

固定在工具頁最下面，不隨卡片捲動：左邊是輸出位置 (路徑輸入框＋「瀏覽」＋「開啟資料夾」)，
右邊是現況與這個畫面唯一的主要按鈕；主要按鈕左邊可以多一顆次要按鈕 (圖資快覽的「另存勾選的向量圖層為 SHP」)。
背景作業執行中，主要按鈕換成次要按鈕「取消」、次要按鈕停用，
現況換成一顆黃色 (glow) 小圓點加進度文字；完成時換成 ok 打勾，失敗時換成 hi 警告。
"""

from __future__ import annotations

import ctypes
import math
import os
import time
import tkinter
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Optional

import customtkinter as ctk

from ui import drag_drop
from ui.icons import IconButton, icon
from ui.task_status import TaskStatus
from ui.theme_constants import (
    BTN_PRIMARY,
    BTN_SECONDARY,
    FONT_FAMILY,
    FONT_FAMILY_MONO,
    FONT_SIZE_BODY,
    FONT_SIZE_CAPTION,
    FONT_SIZE_MONO,
    T,
)

CANCEL_TEXT: str = "取消"
CANCELLING_TEXT: str = "取消中"
GLOW_DOT_SIZE: int = 8
# 黃色圓點明暗：週期 (秒)、最暗時的亮度 (0～1)、更新間隔 (毫秒)
PULSE_PERIOD_S: float = 2.2
PULSE_MIN: float = 0.25
PULSE_STEP_MS: int = 80
# 有次要按鈕時現況最寬 220 px (100% 縮放)，長的折成兩行 (按鈕高 36，列高不變)：
# 不折的話視窗 1360 寬時輸出位置只剩約 100 px，1200 以下主要按鈕會被切掉 (H3 實測)
STATUS_WRAP_WITH_SECONDARY: int = 220


class ActionBar(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        *,
        primary_text: str,
        primary_command: Callable[[], None],
        cancel_command: Callable[[], None],
        default_output: str = "",
        on_output_changed: Optional[Callable[[], None]] = None,
        secondary_text: Optional[str] = None,
        secondary_command: Optional[Callable[[], None]] = None,
        secondary_icon: str = "save",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("corner_radius", 0)
        kwargs.setdefault("fg_color", T["panel"])
        super().__init__(master, **kwargs)
        self._primary_text = primary_text
        self._ready_text: str = ""
        self._ready_ok: bool = True
        self._secondary_ok: bool = True
        self._showing_result: bool = False
        self._busy: bool = False
        self._pulse_job: Optional[str] = None
        self._pulse_started: float = 0.0
        font_caption = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION)
        font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)

        # 上緣 1 px 分隔線
        ctk.CTkFrame(self, height=1, corner_radius=0, fg_color=T["line"]).grid(row=0, column=0, columnspan=9, sticky="ew")
        self.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(self, text="", image=icon("folder", role="muted", size=18), width=18).grid(
            row=1, column=0, padx=(24, 6), pady=12
        )
        ctk.CTkLabel(self, text="輸出到", font=font_caption, text_color=T["muted"]).grid(row=1, column=1, padx=(0, 8))
        # 打字、按「瀏覽」、拖入資料夾都會改到這個變數，改了就通知工具頁重新判斷現況
        self.output_var = tkinter.StringVar(master=self, value=default_output)
        self.entry_out_dir = ctk.CTkEntry(
            self,
            textvariable=self.output_var,
            font=ctk.CTkFont(family=FONT_FAMILY_MONO, size=FONT_SIZE_MONO),
        )
        self.entry_out_dir.grid(row=1, column=2, sticky="ew", padx=(0, 8))
        if on_output_changed is not None:
            self.output_var.trace_add("write", lambda *_args: on_output_changed())
        drag_drop.bind_folder_entry(self.entry_out_dir)

        self.btn_browse = ctk.CTkButton(self, text="瀏覽", width=60, font=font_body, command=self.browse_output, **BTN_SECONDARY)
        self.btn_browse.grid(row=1, column=3, padx=(0, 8))
        self.btn_open = IconButton(
            self, text="開啟資料夾", icon_name="folder-open", font=font_body, command=self.open_output, **BTN_SECONDARY
        )
        self.btn_open.grid(row=1, column=4, padx=(0, 16))

        # 現況：圖示＋一句話；執行中前面多一顆 glow 圓點
        self.glow_dot = ctk.CTkFrame(
            self, width=GLOW_DOT_SIZE, height=GLOW_DOT_SIZE, corner_radius=GLOW_DOT_SIZE // 2, fg_color=T["glow"]
        )
        self.lbl_status = ctk.CTkLabel(self, text="", font=font_caption, compound="left", anchor="e")
        self.lbl_status.grid(row=1, column=6, padx=(0, 12), sticky="e")

        self.btn_secondary: Optional[IconButton] = None
        if secondary_text:
            self.btn_secondary = IconButton(
                self, text=secondary_text, icon_name=secondary_icon, font=font_body, height=36,
                command=secondary_command, **BTN_SECONDARY,
            )
            self.btn_secondary.grid(row=1, column=7, padx=(0, 8))
            self.lbl_status.configure(wraplength=STATUS_WRAP_WITH_SECONDARY, justify="left")

        self.btn_primary = IconButton(
            self,
            text=primary_text,
            icon_name="play",
            icon_role="on-accent",
            icon_filled=True,
            font=ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY, weight="bold"),
            command=primary_command,
            **BTN_PRIMARY,
        )
        self.btn_primary.grid(row=1, column=8, padx=(0, 24))
        self.btn_cancel = IconButton(
            self, text=CANCEL_TEXT, icon_name="close", font=font_body, height=36, command=cancel_command, **BTN_SECONDARY
        )

    # -------------------------------------------------------------------------
    # 輸出位置
    # -------------------------------------------------------------------------
    def output_text(self) -> str:
        # 讀變數而不是輸入框：變數改了通知工具頁時，輸入框本身還沒跟著更新
        return self.output_var.get().strip()

    def set_output(self, folder: str) -> None:
        self.output_var.set(folder)

    def browse_output(self) -> None:
        folder = filedialog.askdirectory(title="選取成果輸出資料夾")
        if folder:
            self.set_output(str(Path(folder).resolve()))

    def open_output(self) -> None:
        """在檔案總管開啟輸出資料夾；還沒建立時說明第一次輸出成果時才會有。"""
        text = self.output_text().strip('"')
        if not text:
            messagebox.showinfo("開啟資料夾", "還沒指定輸出資料夾，請先按「瀏覽」選一個。")
            return
        folder = Path(text)
        if not folder.is_dir():
            messagebox.showinfo("開啟資料夾", f"這個資料夾還不存在，第一次輸出成果時會自動建立：\n{folder}")
            return
        os.startfile(str(folder))  # noqa: S606 (Windows 檔案總管)

    # -------------------------------------------------------------------------
    # 現況與執行中狀態
    # -------------------------------------------------------------------------
    def set_ready(self, text: str, ok: bool = True) -> None:
        """沒有作業在跑時的現況 (例如「可以開始：3 個檔案」「還沒選裁切邊界」)，一定配圖示；
        條件不夠 (ok=False) 時主要按鈕停用，不要讓使用者按了才跳錯誤。

        剛做完的作業結果 (完成／已取消／失敗) 會留著，直到現況的內容變了或出現問題才換掉。
        """
        changed = (text, ok) != (self._ready_text, self._ready_ok)
        self._ready_text, self._ready_ok = text, ok
        self.btn_primary.configure(
            state="normal" if ok else "disabled",
            fg_color=BTN_PRIMARY["fg_color"] if ok else T["bg"],
        )
        if self._busy:
            # 作業執行中，現況那一格顯示的是進度，先不換
            return
        if self._showing_result and not changed and ok:
            return
        self._showing_result = False
        self._show_status(text, "ok" if ok else "warn", icon_name="check" if ok else "warning")

    @property
    def ready(self) -> bool:
        return self._ready_ok

    def set_secondary_ready(self, ok: bool) -> None:
        """次要按鈕能不能按 (原因由工具頁自己寫在卡片裡)；作業執行中一律停用。"""
        self._secondary_ok = ok
        if self.btn_secondary is not None:
            self.btn_secondary.configure(state="normal" if ok and not self._busy else "disabled")

    def _show_status(self, text: str, role: Optional[str], *, icon_name: Optional[str] = None) -> None:
        image = icon(icon_name, role=role) if icon_name and role and text else None
        self.lbl_status.configure(text=text, text_color=T[role] if role else T["text"], image=image)
        if image is None:
            # CTkLabel 的 configure(image=None) 不會真的拿掉圖 (customtkinter 6.0)
            self.lbl_status._label.configure(image="")

    def apply_task_status(self, status: TaskStatus) -> None:
        """PanelInspector 的作業狀態變了：執行中換成「取消」與黃色圓點，結束後換回主要按鈕。"""
        self._busy = status.busy
        self.set_secondary_ready(self._secondary_ok)
        if status.busy:
            self.btn_primary.grid_remove()
            self.btn_cancel.grid(row=1, column=8, padx=(0, 24))
            cancelling = status.state == "cancelling"
            self.btn_cancel.configure(state="disabled" if cancelling else "normal", text=CANCELLING_TEXT if cancelling else CANCEL_TEXT)
            self.glow_dot.grid(row=1, column=5, padx=(0, 6))
            self._start_pulse()
            self._show_status(status.text(), None)
            return
        self._stop_pulse()
        self.glow_dot.grid_remove()
        self.btn_cancel.grid_remove()
        self.btn_primary.grid(row=1, column=8, padx=(0, 24))
        results = {"done": ("ok", "check"), "cancelled": ("warn", "warning"), "failed": ("hi", "warning")}
        if status.state in results:
            role, icon_name = results[status.state]
            self._show_status(status.text(), role, icon_name=icon_name)
            self._showing_result = True
        else:
            self._showing_result = False
            self.set_ready(self._ready_text, self._ready_ok)

    # -------------------------------------------------------------------------
    # 黃色圓點：2.2 秒一個週期慢慢明暗；Windows 關掉動畫效果時直接停在最亮
    # -------------------------------------------------------------------------
    def _start_pulse(self) -> None:
        if self._pulse_job is not None:
            return
        self._pulse_started = time.monotonic()
        if reduce_motion():
            self.glow_dot.configure(fg_color=T["glow"])
            return
        self._pulse()

    def _pulse(self) -> None:
        phase = ((time.monotonic() - self._pulse_started) % PULSE_PERIOD_S) / PULSE_PERIOD_S
        brightness = PULSE_MIN + (1.0 - PULSE_MIN) * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
        mode_index = 1 if ctk.get_appearance_mode() == "Dark" else 0
        self.glow_dot.configure(fg_color=mix_color(T["panel"][mode_index], T["glow"][0], brightness))
        self._pulse_job = self.after(PULSE_STEP_MS, self._pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_job is not None:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
        self.glow_dot.configure(fg_color=T["glow"])

    def destroy(self) -> None:
        if self._pulse_job is not None:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
        super().destroy()


def mix_color(base: str, top: str, amount: float) -> str:
    """base 與 top 兩個 #RRGGBB 顏色按 amount (0＝base、1＝top) 混合。"""
    a = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(top[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02X}" for x, y in zip(a, b))


def reduce_motion() -> bool:
    """Windows「顯示動畫效果」關掉時 (設定 > 協助工具 > 視覺效果) 回傳 True；讀不到時當成沒關。"""
    try:
        enabled = ctypes.c_bool(True)
        # SPI_GETCLIENTAREAANIMATION = 0x1042
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return not enabled.value
    except (AttributeError, OSError):
        pass
    return False
