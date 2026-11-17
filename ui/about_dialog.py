"""「關於」視窗 (ui/about_dialog.py)：產品名、版本、作者與 GitHub 連結；側欄最下面的「關於」打開它。

照 WingLight 設計規範：
1. 上半部是一塊夜空 (splash-bg，深淺色都一樣)：螢火蟲 (靜態、最亮那張) 與產品名、版本，跟啟動畫面同一套。
   螢火蟲只出現在夜空底上 (規範 Logos)；圖直接用啟動畫面的 assets/splash/firefly_bright.png。
2. 下半部跟著深淺色：作者、可以點的 GitHub 連結 (accent 字，用瀏覽器開)、系列標誌與「WingLight 系列」、「關閉」。
   這個視窗沒有主要動作，「關閉」是次要按鈕；Esc、Enter 也能關。
3. 標題列用 Windows 原本的，圖示是 App 圖示；固定大小，出現時放在主視窗正中間偏上。
4. 由 dashboard_app 第一次點「關於」時才 import (不影響啟動時間)，同時只開一個。
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from typing import Any, Optional

import customtkinter as ctk
from PIL import Image

from ui.app_icon import apply_window_icon
from ui.app_info import APP_AUTHOR, APP_NAME, APP_URL, APP_VERSION
from ui.icons import icon
from ui.theme_constants import (
    BTN_SECONDARY,
    FONT_FAMILY,
    FONT_SIZE_BODY,
    FONT_SIZE_CAPTION,
    FONT_SIZE_PAGE_TITLE,
    RADIUS_SM,
    T,
)

logger = logging.getLogger("ui.about_dialog")

ASSETS_DIR: Path = Path(__file__).resolve().parent.parent / "assets"
FIREFLY_PNG: Path = ASSETS_DIR / "splash" / "firefly_bright.png"
# tools/make_about.py 產生 (2 倍大)
MARK_PNG: Path = ASSETS_DIR / "about" / "winglight_mark.png"

ABOUT_WIDTH: int = 400
FIREFLY_SIZE: int = 120
MARK_SIZE: int = 24
SERIES_NAME: str = "WingLight 系列"


def link_text(url: str) -> str:
    """連結上顯示的字：拿掉 https:// (點下去開的還是完整網址)。"""
    return url.removeprefix("https://").removeprefix("http://")


def _load_image(path: Path, size: int) -> Optional[ctk.CTkImage]:
    """讀圖做成 CTkImage；圖檔不見或壞掉時只記警告，視窗照常開 (少一張圖)。"""
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
    except OSError as exc:
        logger.warning("讀不到關於視窗的圖 %s: %s", path, exc)
        return None
    return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))


class AboutDialog(ctk.CTkToplevel):
    def __init__(self, master: Any) -> None:
        # CTkToplevel 建立時先藏起來，換好標題列顏色 5 ms 後才顯示；趁這時候把內容與位置擺好
        super().__init__(master)
        apply_window_icon(self)
        self.title(f"關於 — {APP_NAME}")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Return>", lambda _event: self.close())

        font_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_PAGE_TITLE, weight="bold")
        font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        font_caption = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION)

        # 1. 夜空：螢火蟲、產品名、版本
        self.band = ctk.CTkFrame(self, corner_radius=0, fg_color=T["splash-bg"])
        self.band.pack(fill="x")
        self._firefly_image = _load_image(FIREFLY_PNG, FIREFLY_SIZE)
        # 這一列撐出視窗的寬度 (圖不見時也維持一樣的版面)
        self.lbl_firefly = ctk.CTkLabel(self.band, text="", image=self._firefly_image, width=ABOUT_WIDTH, height=FIREFLY_SIZE)
        self.lbl_firefly.pack(pady=(20, 4))
        self.lbl_name = ctk.CTkLabel(self.band, text=APP_NAME, font=font_title, text_color=T["splash-text"])
        self.lbl_name.pack()
        self.lbl_version = ctk.CTkLabel(
            self.band, text=f"版本 {APP_VERSION}", font=font_caption, text_color=T["splash-muted"], height=20
        )
        self.lbl_version.pack(pady=(0, 20))

        # 2. 作者與連結
        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(fill="x", padx=24, pady=(16, 0))
        info.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(info, text="作者", font=font_caption, text_color=T["muted"], anchor="w", width=64).grid(
            row=0, column=0, sticky="w"
        )
        self.lbl_author = ctk.CTkLabel(info, text=APP_AUTHOR, font=font_body, anchor="w")
        # 跟下一列連結的字對齊：CTkButton 左邊固定留 corner_radius 那麼寬
        self.lbl_author.grid(row=0, column=1, sticky="w", padx=(RADIUS_SM, 0))
        ctk.CTkLabel(info, text="GitHub", font=font_caption, text_color=T["muted"], anchor="w", width=64).grid(
            row=1, column=0, sticky="w"
        )
        # 可以點的文字：accent 字、滑鼠移過 hover 底，後面一個外部連結圖示
        self.btn_link = ctk.CTkButton(
            info,
            text=link_text(APP_URL),
            image=icon("external", role="accent", size=16),
            compound="right",
            anchor="w",
            width=0,
            height=28,
            corner_radius=RADIUS_SM,
            border_width=0,
            fg_color="transparent",
            hover_color=T["hover"],
            text_color=T["accent"],
            font=font_body,
            command=self.open_url,
        )
        self.btn_link.grid(row=1, column=1, sticky="w")

        ctk.CTkFrame(self, height=1, corner_radius=0, fg_color=T["line"]).pack(fill="x", padx=24, pady=16)

        # 3. 系列標誌與關閉
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(0, 20))
        self._mark_image = _load_image(MARK_PNG, MARK_SIZE)
        self.lbl_mark = ctk.CTkLabel(footer, text="", image=self._mark_image, width=MARK_SIZE, height=MARK_SIZE)
        self.lbl_mark.pack(side="left")
        self.lbl_series = ctk.CTkLabel(footer, text=SERIES_NAME, font=font_caption, text_color=T["muted"], anchor="w")
        self.lbl_series.pack(side="left", padx=(8, 0))
        self.btn_close = ctk.CTkButton(footer, text="關閉", font=font_body, width=88, command=self.close, **BTN_SECONDARY)
        self.btn_close.pack(side="right")

        self._place_over(master)
        # 顯示後把鍵盤焦點給這個視窗，Esc、Enter 才收得到
        self._focus_job = self.after(50, self.focus_set)

    def _place_over(self, master: Any) -> None:
        """放在主視窗正中間偏上 (跟 Windows 的對話框一樣)；主視窗沒顯示時交給系統決定。"""
        self.update_idletasks()
        if not master.winfo_ismapped():
            return
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        # 位置是外框左上角 (含 Windows 看不見的邊框與標題列)，內容區還要往右下偏；
        # 這個視窗跟主視窗的外框一樣，偏多少照主視窗算 (100% 縮放時左右 8、上面 31 px)
        frame_x = master.winfo_rootx() - master.winfo_x()
        frame_y = master.winfo_rooty() - master.winfo_y()
        x = master.winfo_rootx() + (master.winfo_width() - width) // 2 - frame_x
        y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3) - frame_y
        # 只給位置；CTkToplevel.geometry 會把數字乘上縮放，位置不能乘
        self.wm_geometry(f"+{x}+{y}")

    def open_url(self) -> None:
        """用預設瀏覽器開作者的 GitHub。"""
        if not webbrowser.open(APP_URL):
            logger.warning("無法開啟瀏覽器: %s", APP_URL)

    def close(self) -> None:
        self.destroy()

    def destroy(self) -> None:
        # 很快就關掉時，還沒執行的「給焦點」不能留著 (視窗已經不在了)
        self.after_cancel(self._focus_job)
        super().destroy()
