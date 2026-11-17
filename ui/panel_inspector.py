"""右側檢視欄 (ui/panel_inspector.py)

照 WingLight 規範「側欄工作台」的檢視欄：主區右邊固定寬度 (INSPECTOR_WIDTH) 的一張卡片，三個裁切工具共用，
顯示的內容由目前那一頁交給它 (TabClipWorkflow.activate)。由上往下：
1. 預覽：原始檔縮圖＋裁切邊界，約 200 px 高的靜態縮圖 (點圖可開大圖視窗)；下方圖說與「瀏覽器套疊航照底圖」。
2. 四角座標：與「貼座標」同格式，可選取複製；「用這個範圍當邊界」。
3. 空間體檢：狀態 (正常／提醒／警示，配 ok／warn／hi 色與圖示) 與來源、座標系、面積、四至、建議。
4. 處理紀錄：只顯示最新一行，「查看全部」開紀錄視窗；完整紀錄保留在 txt_terminal (超過上限丟掉最舊的列)。

背景作業 (run_task) 也由這裡執行：一次只跑一件，狀態 (執行中、進度文字、完成、取消、失敗) 透過
add_status_listener 通知每一頁的底部動作列 (ui/action_bar.py)，取消鈕也在動作列。
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import customtkinter as ctk
from PIL import Image

from core.crs_doctor import CrsDiagnosisResult
from core.env_runner import CommandCancelledError, run_in_background
from core.preview_engine import open_preview_in_browser
from ui.app_icon import apply_window_icon
from ui.app_info import APP_NAME
from ui.icons import IconButton, icon
from ui.task_status import TaskStatus
from ui.theme_constants import (
    BTN_SECONDARY,
    FONT_FAMILY,
    FONT_FAMILY_MONO,
    FONT_SIZE_BODY,
    FONT_SIZE_CAPTION,
    FONT_SIZE_CARD_TITLE,
    FONT_SIZE_MONO,
    RADIUS_LG,
    RADIUS_MD,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_WARNING,
    T,
)

logger = logging.getLogger("ui.panel_inspector")

# 檢視欄寬度 (規範 inspector-width)；預覽圖高度約 200 px，不要做大 (使用者會以為可以拖拉縮放)
INSPECTOR_WIDTH: int = 348
PREVIEW_HEIGHT: int = 200
# 日誌最多保留的列數，超過就丟掉最舊的
MAX_LOG_LINES: int = 2000
# 大圖預覽視窗最多佔螢幕的比例
ZOOM_SCREEN_RATIO: float = 0.8
STATUS_IDLE_TEXT: str = "狀態：系統就緒 (待命中)"
PREVIEW_PLACEHOLDER_TEXT: str = "暫無預覽圖\n\n(投入原始檔或定義邊界後，\n這裡會顯示原始檔與裁切邊界的位置)"
LOG_EMPTY_TEXT: str = "(還沒有紀錄)"
# 體檢各欄的預設文字 (欄位名稱 -> 文字)
DIAG_DEFAULT_TEXTS: Dict[str, str] = {
    "來源圖資:": "尚未載入圖資",
    "座標系統:": "未指定 (預設 TWD97 / EPSG:3826)",
    "閉鎖面積:": "0.00 m² (0.0000 公頃)",
    "四至邊界:": "X: --- ~ --- | Y: --- ~ ---",
    "診斷建議:": "正常，無偏移或縮放異常。",
}
# 檢視欄內容的左右內距與文字可用寬度
_PAD_X: int = 16
_VALUE_WRAP: int = INSPECTOR_WIDTH - 2 * _PAD_X - 80


# 預覽區的標題 (各工具頁切換時設定)
PREVIEW_TITLE_CLIP: str = "預覽：原始檔＋邊界"
PREVIEW_TITLE_VIEW: str = "預覽"


def format_extent_corners(extent: Any) -> str:
    """把四至範圍寫成四個角點 (左下、右下、右上、左上)，一列一點、X 與 Y 以 Tab 分隔，可直接貼進座標框。

    經緯度 (數值都在 ±180 內) 取到小數 7 位，投影座標取到 3 位 (公釐)。
    """
    geographic = all(abs(v) <= 180.0 for v in (extent.min_x, extent.max_x, extent.min_y, extent.max_y))
    digits = 7 if geographic else 3
    corners = [
        (extent.min_x, extent.min_y),
        (extent.max_x, extent.min_y),
        (extent.max_x, extent.max_y),
        (extent.min_x, extent.max_y),
    ]
    return "\n".join(f"{x:.{digits}f}\t{y:.{digits}f}" for x, y in corners)


class PanelInspector(ctk.CTkFrame):
    """右側檢視欄 (三個裁切工具共用) 與背景作業執行器。"""

    def __init__(
        self,
        master: Any,
        *,
        on_open_browser_preview: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("width", INSPECTOR_WIDTH)
        kwargs.setdefault("corner_radius", RADIUS_LG)
        kwargs.setdefault("fg_color", T["panel"])
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", T["line"])
        kwargs.setdefault("bg_color", T["bg"])
        super().__init__(master, **kwargs)

        self.on_open_browser_preview = on_open_browser_preview
        # 「用這個範圍當邊界」：由目前那一頁接手，參數是目前顯示的體檢結果
        self.on_use_extent: Optional[Callable[[CrsDiagnosisResult], None]] = None
        self.current_diagnosis: Optional[CrsDiagnosisResult] = None

        self.current_preview_html: Optional[Path] = None
        self.current_preview_image: Optional[Path] = None
        self._raw_pil_image: Optional[Image.Image] = None
        # 預覽縮圖只在換圖時建立一次，尺寸變了只改它的顯示尺寸
        self._preview_ctk_image: Optional[ctk.CTkImage] = None
        self._preview_size: Optional[Tuple[int, int]] = None
        self.preview_window: Optional[ctk.CTkToplevel] = None
        self.log_window: Optional[ctk.CTkToplevel] = None
        self._log_window_text: Optional[ctk.CTkTextbox] = None

        # 背景執行緒要更新畫面時，把動作丟進佇列，由主執行緒定時取出執行 (Tk 元件只能在主執行緒操作)
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._ui_thread = threading.current_thread()
        self._drain_job: Optional[str] = None
        # 目前執行中的背景作業 (一次只跑一件)；沒有作業時為 None
        self._active_cancel_event: Optional[threading.Event] = None
        self._active_task_label: str = ""
        self.task_status: TaskStatus = TaskStatus()
        self._status_listeners: List[Callable[[TaskStatus], None]] = []

        self.font_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CARD_TITLE, weight="bold")
        self.font_section = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION, weight="bold")
        self.font_caption = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION)
        self.font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self.font_status = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self.font_log = ctk.CTkFont(family=FONT_FAMILY_MONO, size=FONT_SIZE_MONO)

        # 寬度固定；內容多於高度時可以捲動 (視窗很矮時)
        self.grid_propagate(False)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.body = ctk.CTkScrollableFrame(self, fg_color=T["panel"], corner_radius=0)
        self.body.grid(row=0, column=0, sticky="nsew", padx=2, pady=8)
        self.body.grid_columnconfigure(0, weight=1)

        self._build_preview_section()
        self._build_corners_section()
        self._build_diagnosis_section()
        self._build_log_section()
        self._drain_ui_queue()

    # -------------------------------------------------------------------------
    # 背景作業與跨執行緒畫面更新
    # -------------------------------------------------------------------------
    def post_to_ui(self, func: Callable[[], None]) -> None:
        """把一個動作排進主執行緒執行；任何執行緒都可以呼叫。"""
        self._ui_queue.put(func)

    def _on_ui_thread(self) -> bool:
        return threading.current_thread() is self._ui_thread

    def _drain_ui_queue(self) -> None:
        """主執行緒定時取出佇列中的動作逐一執行。"""
        while True:
            try:
                func = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception as err:
                logger.exception("執行畫面更新動作時發生例外: %s", err)
        self._drain_job = self.after(50, self._drain_ui_queue)

    def destroy(self) -> None:
        if self._drain_job is not None:
            self.after_cancel(self._drain_job)
            self._drain_job = None
        if self._active_cancel_event is not None:
            self._active_cancel_event.set()
        self._close_preview_window()
        self._close_log_window()
        super().destroy()

    @property
    def is_busy(self) -> bool:
        """是否有背景作業正在執行。"""
        return self._active_cancel_event is not None

    def add_status_listener(self, listener: Callable[[TaskStatus], None]) -> None:
        """作業狀態一變就呼叫 listener(TaskStatus)；加入時先用目前的狀態呼叫一次。"""
        self._status_listeners.append(listener)
        listener(self.task_status)

    def remove_status_listener(self, listener: Callable[[TaskStatus], None]) -> None:
        if listener in self._status_listeners:
            self._status_listeners.remove(listener)

    def _set_task_status(self, status: TaskStatus) -> None:
        self.task_status = status
        for listener in list(self._status_listeners):
            try:
                listener(status)
            except Exception as err:
                logger.exception("更新作業狀態顯示時發生例外: %s", err)

    def run_task(
        self,
        label: str,
        func: Callable[[], Any],
        *,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_finish: Optional[Callable[[], None]] = None,
        error_title: str = "錯誤",
    ) -> bool:
        """在背景執行緒執行 func，並把結果帶回主執行緒。

        - func 在背景執行，裡面可以直接呼叫 append_log／set_progress (會自動轉回主執行緒)。
        - on_success(回傳值)、on_error(例外) 與錯誤對話框都在主執行緒執行。
        - on_finish() 不論成功、失敗或取消都會先被呼叫一次 (用來還原按鈕)；取消時不會呼叫 on_error。
        - 一次只跑一件作業；已有作業在跑時提示使用者並回傳 False。
        - 作業期間底部動作列的「取消」可按，按下後正在跑的 CLI 會被終止，日誌記錄已取消，不跳錯誤對話框。
        """
        if self.is_busy:
            messagebox.showwarning("作業進行中", f"「{self._active_task_label}」還在執行，請等它完成或先按「取消」。")
            return False

        cancel_event = threading.Event()
        self._active_cancel_event = cancel_event
        self._active_task_label = label
        self._set_task_status(TaskStatus("running", label))
        self.append_log(f"[開始] {label}\n")

        def _finish() -> None:
            self._active_cancel_event = None
            self._active_task_label = ""
            if on_finish:
                on_finish()

        def _succeeded(result: Any) -> None:
            _finish()
            self._set_task_status(TaskStatus("done", label))
            if on_success:
                on_success(result)

        def _failed(err: Exception) -> None:
            _finish()
            if isinstance(err, CommandCancelledError):
                self._set_task_status(TaskStatus("cancelled", label))
                self.append_log(f"[已取消] {label}\n")
                return
            self._set_task_status(TaskStatus("failed", label))
            self.append_log(f"[失敗] {label}: {err}\n")
            if on_error:
                on_error(err)
            messagebox.showerror(error_title, f"{label}失敗:\n{err}")

        run_in_background(
            func,
            on_success=_succeeded,
            on_error=_failed,
            cancel_event=cancel_event,
            dispatcher=self.post_to_ui,
        )
        return True

    def cancel_current_task(self) -> None:
        """要求取消目前的背景作業；沒有作業時不做事。"""
        if self._active_cancel_event is None:
            return
        self._active_cancel_event.set()
        self._set_task_status(TaskStatus("cancelling", self._active_task_label))
        self.append_log(f"[取消中] 正在中止「{self._active_task_label}」。\n")

    def set_progress(self, percent: float, message: str = "") -> None:
        """更新進度文字 (例如「[2/3] 地形.tif: 裁切中」)；可從背景執行緒呼叫。

        規範不顯示量不準的百分比，percent 只保留給呼叫端相容；作業沒在跑時不做事。
        """
        if not self._on_ui_thread():
            self.post_to_ui(lambda: self.set_progress(percent, message))
            return
        if message and self.task_status.state == "running":
            self._set_task_status(TaskStatus("running", self.task_status.label, message))

    # -------------------------------------------------------------------------
    # 版面
    # -------------------------------------------------------------------------
    def _separator(self, row: int) -> None:
        ctk.CTkFrame(self.body, height=1, corner_radius=0, fg_color=T["line"]).grid(
            row=row, column=0, sticky="ew", padx=_PAD_X, pady=12
        )

    def _section_title(self, parent: Any, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, font=self.font_section, text_color=T["muted"], anchor="w")

    def _build_preview_section(self) -> None:
        """預覽：標題、約 200 px 高的縮圖、圖說與瀏覽器地圖鈕。"""
        head = ctk.CTkFrame(self.body, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=_PAD_X, pady=(4, 8))
        head.grid_columnconfigure(0, weight=1)
        # 標題由目前的工具頁設定 (set_preview_title)：裁切工具是「原始檔＋邊界」，圖資快覽只有選取的圖資
        self.lbl_preview_title = ctk.CTkLabel(head, text=PREVIEW_TITLE_CLIP, font=self.font_title, anchor="w")
        self.lbl_preview_title.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text="點圖可放大", font=self.font_caption, text_color=T["muted"]).grid(row=0, column=1, sticky="e")

        self.preview_canvas_frame = ctk.CTkFrame(
            self.body, height=PREVIEW_HEIGHT, fg_color=T["bg"], corner_radius=RADIUS_MD, bg_color=T["panel"]
        )
        self.preview_canvas_frame.grid(row=1, column=0, sticky="ew", padx=_PAD_X)
        self.preview_canvas_frame.grid_propagate(False)
        self.preview_canvas_frame.grid_rowconfigure(0, weight=1)
        self.preview_canvas_frame.grid_columnconfigure(0, weight=1)

        self.lbl_image_display = ctk.CTkLabel(
            self.preview_canvas_frame,
            text=PREVIEW_PLACEHOLDER_TEXT,
            font=self.font_caption,
            text_color=T["muted"],
        )
        self.lbl_image_display.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.lbl_image_display.bind("<Button-1>", self._handle_zoom_preview)
        self.preview_canvas_frame.bind("<Configure>", self._on_canvas_resize)

        # 圖說：目前顯示的是哪個檔、紅框是什麼
        self.lbl_preview_caption = ctk.CTkLabel(
            self.body,
            text="",
            font=self.font_caption,
            text_color=T["muted"],
            anchor="w",
            justify="left",
            wraplength=INSPECTOR_WIDTH - 2 * _PAD_X,
        )
        self.lbl_preview_caption.grid(row=2, column=0, sticky="ew", padx=_PAD_X, pady=(4, 0))

        self.btn_browser_map = IconButton(
            self.body,
            text="瀏覽器套疊航照底圖",
            icon_name="globe",
            font=self.font_body,
            command=self._handle_open_browser_map,
            **BTN_SECONDARY,
        )
        self.btn_browser_map.grid(row=3, column=0, sticky="ew", padx=_PAD_X, pady=(8, 0))

    def _build_corners_section(self) -> None:
        """四角座標：與「貼座標」同格式 (X<Tab>Y)，可選取複製；可一鍵拿來當裁切邊界。"""
        self._separator(4)
        head = ctk.CTkFrame(self.body, fg_color="transparent")
        head.grid(row=5, column=0, sticky="ew", padx=_PAD_X, pady=(0, 6))
        head.grid_columnconfigure(0, weight=1)
        self._section_title(head, "四角座標").grid(row=0, column=0, sticky="w")
        self.btn_copy_corners = IconButton(
            head, text="複製", icon_name="copy", width=72, height=26, font=self.font_caption,
            command=self._handle_copy_corners, **BTN_SECONDARY,
        )
        self.btn_copy_corners.grid(row=0, column=1, sticky="e")

        self.txt_corners = ctk.CTkTextbox(self.body, height=78, font=self.font_log, wrap="none")
        self.txt_corners.grid(row=6, column=0, sticky="ew", padx=_PAD_X)
        self.txt_corners.configure(state="disabled")

        self.btn_use_extent = ctk.CTkButton(
            self.body,
            text="用這個範圍當邊界",
            font=self.font_body,
            command=self._handle_use_extent,
            **BTN_SECONDARY,
        )
        self.btn_use_extent.grid(row=7, column=0, sticky="ew", padx=_PAD_X, pady=(8, 0))
        self.btn_use_extent.configure(state="disabled")
        self._set_corners_text("")

    def _build_diagnosis_section(self) -> None:
        """空間體檢：狀態一列 (圖示＋文字)，下面每列「欄位名：值」。"""
        self._separator(8)
        frame = ctk.CTkFrame(self.body, fg_color="transparent")
        frame.grid(row=9, column=0, sticky="ew", padx=_PAD_X)
        frame.grid_columnconfigure(1, weight=1)
        self.card_diag = frame
        self._section_title(frame, "空間體檢").grid(row=0, column=0, columnspan=2, sticky="w")

        self.lbl_status_badge = ctk.CTkLabel(
            frame,
            text=STATUS_IDLE_TEXT,
            font=self.font_status,
            text_color=STATUS_OK,
            image=icon("check", role="ok"),
            compound="left",
            anchor="w",
            justify="left",
            wraplength=INSPECTOR_WIDTH - 2 * _PAD_X - 24,
        )
        self.lbl_status_badge.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 6))

        self.diag_value_labels: Dict[str, ctk.CTkLabel] = {}
        for index, (title, default_text) in enumerate(DIAG_DEFAULT_TEXTS.items()):
            lbl_k = ctk.CTkLabel(frame, text=title.rstrip(":"), font=self.font_caption, text_color=T["muted"], anchor="nw")
            lbl_k.grid(row=2 + index, column=0, sticky="nw", padx=(0, 8), pady=1)
            lbl_v = ctk.CTkLabel(
                frame, text=default_text, font=self.font_caption, anchor="w", justify="left", wraplength=_VALUE_WRAP
            )
            lbl_v.grid(row=2 + index, column=1, sticky="w", pady=1)
            self.diag_value_labels[title] = lbl_v

    def _build_log_section(self) -> None:
        """處理紀錄：只顯示最新一行；完整紀錄在 txt_terminal，「查看全部」開紀錄視窗。"""
        self._separator(10)
        frame = ctk.CTkFrame(self.body, fg_color="transparent")
        frame.grid(row=11, column=0, sticky="ew", padx=_PAD_X, pady=(0, 8))
        frame.grid_columnconfigure(0, weight=1)
        self._section_title(frame, "處理紀錄").grid(row=0, column=0, sticky="w")
        self.btn_show_log = ctk.CTkButton(
            frame,
            text="查看全部",
            width=72,
            height=26,
            font=self.font_caption,
            command=self.open_log_window,
            **BTN_SECONDARY,
        )
        self.btn_show_log.grid(row=0, column=1, sticky="e")
        self.lbl_log_last = ctk.CTkLabel(
            frame, text=LOG_EMPTY_TEXT, font=self.font_caption, text_color=T["muted"], anchor="w", justify="left",
            wraplength=INSPECTOR_WIDTH - 2 * _PAD_X,
        )
        self.lbl_log_last.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        # 完整紀錄：不放在版面上，只當資料存放處；紀錄視窗打開時同步顯示
        self.txt_terminal = ctk.CTkTextbox(self, height=10, font=self.font_log, wrap="char")
        self.append_log(f"[系統就緒] {APP_NAME} 核心已初始化。\n")

    # -------------------------------------------------------------------------
    # 處理紀錄
    # -------------------------------------------------------------------------
    def append_log(self, text: str) -> None:
        """追加日誌字串 (超過上限丟掉最舊的列)，最新一行顯示在檢視欄；可從背景執行緒呼叫。"""
        if not self._on_ui_thread():
            self.post_to_ui(lambda: self.append_log(text))
            return
        for box in (self.txt_terminal, self._log_window_text):
            if box is None:
                continue
            box.insert("end", text)
            line_count = int(box.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES:
                box.delete("1.0", f"{line_count - MAX_LOG_LINES + 1}.0")
            box.see("end")
        last = next((line for line in reversed(text.splitlines()) if line.strip()), "")
        if last:
            self.lbl_log_last.configure(text=last.strip())

    def clear_log(self) -> None:
        """清空日誌。"""
        self.txt_terminal.delete("1.0", "end")
        if self._log_window_text is not None:
            self._log_window_text.delete("1.0", "end")
        self.lbl_log_last.configure(text=LOG_EMPTY_TEXT)

    def open_log_window(self) -> None:
        """開紀錄視窗 (已開著就拉到最前面)：完整紀錄、複製全部、清除。"""
        if self.log_window is not None:
            self.log_window.lift()
            return
        window = ctk.CTkToplevel(self)
        apply_window_icon(window)
        window.title(f"處理紀錄 — {APP_NAME}")
        window.geometry("760x480")
        window.protocol("WM_DELETE_WINDOW", self._close_log_window)
        window.bind("<Escape>", lambda _event: self._close_log_window())
        window.grid_rowconfigure(0, weight=1)
        window.grid_columnconfigure(0, weight=1)
        box = ctk.CTkTextbox(window, font=ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION), wrap="char")
        box.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=12, pady=(12, 8))
        box.insert("1.0", self.txt_terminal.get("1.0", "end-1c"))
        box.see("end")
        bar = ctk.CTkFrame(window, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))
        IconButton(bar, text="複製全部", icon_name="copy", font=self.font_body, command=self._copy_log, **BTN_SECONDARY).grid(
            row=0, column=0, padx=(0, 8)
        )
        self.btn_clear_log = IconButton(bar, text="清除", icon_name="trash", font=self.font_body, command=self.clear_log, **BTN_SECONDARY)
        self.btn_clear_log.grid(row=0, column=1)
        self.log_window = window
        self._log_window_text = box

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.txt_terminal.get("1.0", "end-1c"))

    def _close_log_window(self) -> None:
        if self.log_window is not None:
            self.log_window.destroy()
            self.log_window = None
            self._log_window_text = None

    # -------------------------------------------------------------------------
    # 體檢、預覽與四角座標
    # -------------------------------------------------------------------------
    def set_preview_title(self, text: str) -> None:
        self.lbl_preview_title.configure(text=text)

    def reset_view(self) -> None:
        """把體檢、四角座標與預覽清回初始狀態 (切換到還沒載入東西的工具時用)；日誌與作業狀態不動。"""
        self.current_diagnosis = None
        self._set_status_badge(STATUS_IDLE_TEXT, "ok")
        for title, default_text in DIAG_DEFAULT_TEXTS.items():
            self.diag_value_labels[title].configure(text=default_text)
        self._set_corners_text("")
        self.btn_use_extent.configure(state="disabled")
        self.current_preview_html = None
        self.clear_preview()

    def _set_status_badge(self, text: str, role: str) -> None:
        """體檢狀態：ok 配打勾、warn／hi 配警告圖示，文字同色。"""
        colors = {"ok": STATUS_OK, "warn": STATUS_WARNING, "hi": STATUS_ERROR}
        self.lbl_status_badge.configure(
            text=text,
            text_color=colors[role],
            image=icon("check" if role == "ok" else "warning", role=role),
        )

    def clear_preview(self) -> None:
        """預覽畫布與圖說清回「暫無預覽圖」(大圖視窗一併關掉)。"""
        self.current_preview_image = None
        self._show_canvas_text(PREVIEW_PLACEHOLDER_TEXT)
        self.lbl_preview_caption.configure(text="")
        self._close_preview_window()

    def _show_canvas_text(self, text: str) -> None:
        """畫布拿掉圖、改顯示文字。

        customtkinter 6.0 的 configure(image=None) 不會真的拿掉內部 tk Label 的圖，圖片物件被回收後
        再改文字會出現「image "pyimageN" doesn't exist」，所以要直接清掉內部 Label 的 -image。
        """
        self.lbl_image_display.configure(image=None)
        self.lbl_image_display._label.configure(image="")
        self.lbl_image_display.configure(text=text)
        self._raw_pil_image = None
        self._preview_ctk_image = None
        self._preview_size = None

    def update_spatial_diagnosis(
        self,
        diagnosis: CrsDiagnosisResult,
        area_m2: Optional[float] = None,
        area_ha: Optional[float] = None,
        *,
        can_use_extent: bool = True,
    ) -> None:
        """更新空間體檢與狀態。

        can_use_extent：「用這個範圍當邊界」鈕是否可按 (顯示的是裁切邊界本身時傳 False)。
        """
        self.current_diagnosis = diagnosis
        self._set_corners_text(format_extent_corners(diagnosis.extent) if diagnosis.extent else "")
        usable = can_use_extent and diagnosis.extent is not None and self.on_use_extent is not None
        self.btn_use_extent.configure(state="normal" if usable else "disabled")

        level = diagnosis.status_level.upper()
        if level == "GREEN":
            self._set_status_badge(f"正常：{diagnosis.status_title}", "ok")
        elif level == "YELLOW":
            self._set_status_badge(f"提醒：{diagnosis.status_title}", "warn")
        else:
            self._set_status_badge(f"警示：{diagnosis.status_title}", "hi")

        fname = Path(diagnosis.file_path).name if diagnosis.file_path else "自訂輸入邊界"
        self.diag_value_labels["來源圖資:"].configure(text=fname)

        srs_text = diagnosis.raw_srs_name or (f"EPSG:{diagnosis.detected_epsg}" if diagnosis.detected_epsg else "無標籤")
        self.diag_value_labels["座標系統:"].configure(text=srs_text)

        if diagnosis.extent:
            ext = diagnosis.extent
            ext_str = f"X: {ext.min_x:,.1f} ~ {ext.max_x:,.1f}\nY: {ext.min_y:,.1f} ~ {ext.max_y:,.1f}"
        else:
            ext_str = "X: --- ~ --- | Y: --- ~ ---"
        self.diag_value_labels["四至邊界:"].configure(text=ext_str)

        if area_m2 is not None:
            ha = area_ha if area_ha is not None else (area_m2 / 10000.0)
            self.diag_value_labels["閉鎖面積:"].configure(text=f"{area_m2:,.2f} m² ({ha:,.4f} 公頃)")

        self.diag_value_labels["診斷建議:"].configure(text=diagnosis.status_message)

    def load_preview_image(self, image_path: Union[str, Path], caption: str = "") -> None:
        """載入預覽影像並繪製至畫布；caption 是畫布下方的圖說 (例如顯示的是哪個檔)。"""
        p = Path(image_path).resolve()
        if not p.exists():
            return
        self.current_preview_image = p
        self.lbl_preview_caption.configure(text=caption)
        try:
            # 讀進記憶體後就關檔，之後重新產生同名預覽圖才不會因檔案被鎖而失敗
            with Image.open(p) as opened:
                self._raw_pil_image = opened.copy()
            self._preview_ctk_image = None
            self._preview_size = None
            self._refresh_preview_canvas()
            if self.preview_window is not None:
                # 大圖視窗開著時跟著換成新圖
                self._handle_zoom_preview()
        except Exception as err:
            logger.warning("載入預覽圖片失敗: %s", err)
            self._show_canvas_text(f"預覽圖片讀取失敗: {err}")

    def _on_canvas_resize(self, event: Any) -> None:
        """畫布尺寸改變時調整縮圖。"""
        if self._raw_pil_image is not None:
            self._refresh_preview_canvas()

    @staticmethod
    def _fit_size(image_size: Tuple[int, int], box_w: int, box_h: int) -> Tuple[int, int]:
        """回傳把圖等比例放進 box 內的尺寸。"""
        orig_w, orig_h = image_size
        scale = min(box_w / orig_w, box_h / orig_h)
        return max(1, int(orig_w * scale)), max(1, int(orig_h * scale))

    def _refresh_preview_canvas(self) -> None:
        """讓縮圖符合目前畫布尺寸；尺寸沒變就不動，只改顯示尺寸、不重建影像物件。"""
        if self._raw_pil_image is None:
            return

        w = max(100, self.preview_canvas_frame.winfo_width() - 16)
        h = max(100, self.preview_canvas_frame.winfo_height() - 16)
        new_size = self._fit_size(self._raw_pil_image.size, w, h)
        if self._preview_ctk_image is not None and new_size == self._preview_size:
            return

        self._preview_size = new_size
        if self._preview_ctk_image is None:
            self._preview_ctk_image = ctk.CTkImage(
                light_image=self._raw_pil_image, dark_image=self._raw_pil_image, size=new_size
            )
            self.lbl_image_display.configure(image=self._preview_ctk_image, text="")
        else:
            self._preview_ctk_image.configure(size=new_size)

    def _handle_zoom_preview(self, event: Any = None) -> None:
        """點預覽圖：開一個大圖視窗 (已開著就換圖並拉到最前面)。"""
        if self._raw_pil_image is None:
            self.append_log("[提示] 目前沒有預覽圖可以放大。\n")
            return

        max_w = max(200, int(self.winfo_screenwidth() * ZOOM_SCREEN_RATIO))
        max_h = max(200, int(self.winfo_screenheight() * ZOOM_SCREEN_RATIO))
        orig_w, orig_h = self._raw_pil_image.size
        # 小圖不硬放大超過原尺寸的 3 倍，避免糊成一片
        size = self._fit_size((orig_w, orig_h), min(max_w, orig_w * 3), min(max_h, orig_h * 3))
        zoom_image = ctk.CTkImage(light_image=self._raw_pil_image, dark_image=self._raw_pil_image, size=size)
        title = f"預覽大圖：{self.current_preview_image.name}" if self.current_preview_image else "預覽大圖"

        if self.preview_window is None:
            window = ctk.CTkToplevel(self)
            apply_window_icon(window)
            window.protocol("WM_DELETE_WINDOW", self._close_preview_window)
            window.bind("<Escape>", lambda _event: self._close_preview_window())
            self.lbl_zoom_image = ctk.CTkLabel(window, text="")
            self.lbl_zoom_image.pack(padx=8, pady=8)
            self.preview_window = window
        self.preview_window.title(title)
        self.preview_window.geometry(f"{size[0] + 16}x{size[1] + 16}")
        self.lbl_zoom_image.configure(image=zoom_image)
        self.preview_window.lift()

    def _close_preview_window(self) -> None:
        if self.preview_window is not None:
            self.preview_window.destroy()
            self.preview_window = None

    def set_current_preview_html(self, html_path: Path) -> None:
        """設定當前產生之 HTML 地圖路徑。"""
        self.current_preview_html = html_path

    def _set_corners_text(self, text: str) -> None:
        """寫入四角座標框 (唯讀，但可選取複製)。"""
        self.txt_corners.configure(state="normal")
        self.txt_corners.delete("1.0", "end")
        self.txt_corners.insert("1.0", text)
        self.txt_corners.configure(state="disabled")
        self.btn_copy_corners.configure(state="normal" if text else "disabled")

    # -------------------------------------------------------------------------
    # 按鈕事件處置
    # -------------------------------------------------------------------------
    def _handle_copy_corners(self) -> None:
        """把四角座標複製到剪貼簿。"""
        text = self.txt_corners.get("1.0", "end").strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.append_log("[四角座標] 已複製到剪貼簿，可貼到「貼座標」修改後建立邊界。\n")

    def _handle_use_extent(self) -> None:
        """把目前顯示的圖資範圍交給目前的裁切工具當邊界。"""
        if self.on_use_extent is not None and self.current_diagnosis is not None:
            self.on_use_extent(self.current_diagnosis)

    def _handle_open_browser_map(self) -> None:
        """點擊在瀏覽器開啟 NLSC WMTS 預覽地圖。"""
        if self.on_open_browser_preview:
            self.on_open_browser_preview()
        elif self.current_preview_html and self.current_preview_html.exists():
            open_preview_in_browser(self.current_preview_html)
        else:
            self.append_log("[提示] 尚未產生地圖預覽檔，請先執行邊界構面或裁切作業。\n")
