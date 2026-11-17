"""線條圖示 (ui/icons.py)：讀 tools/make_icons.py 產生的遮罩 PNG，塗上主題色做成 CTkImage。

- 圖示名稱照 WingLight 設計規範 Icons 群組 (folder、play、check …)，來源 SVG 在 assets/icons_src/。
- role 是顏色的用途，對應 theme_constants.T 的色票：一般跟旁邊的文字同色 (按鈕 text、清單與側欄 muted、
  選中 accent)，狀態圖示用 ok／warn／hi，主要按鈕裡用 on-accent 加填滿版 (filled=True)。
- 依螢幕縮放挑最接近的 PNG 尺寸 (16、20、24、32)，CustomTkinter 再縮放到剛好的像素，邊緣不會糊。
- 同一個主視窗裡，同一組 (名稱, 顏色, 尺寸) 只建一次 CTkImage，多個元件共用。快取跟著主視窗走：
  CTkImage 內部的 PhotoImage 屬於建立它時的 Tk 主視窗，主視窗關掉後就不能再用 (測試會反覆開關主視窗)。
"""

from __future__ import annotations

import tkinter
import weakref
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import customtkinter as ctk
from PIL import Image

from ui.theme_constants import T

ICON_DIR: Path = Path(__file__).resolve().parent.parent / "assets" / "icons"
ICON_SIZES: Tuple[int, ...] = (16, 20, 24, 32)
# 圖示可以用的顏色 (theme_constants.T 的色票名稱)
ICON_ROLES: Tuple[str, ...] = ("text", "muted", "accent", "on-accent", "disabled", "ok", "warn", "hi")

_IconKey = Tuple[str, str, int, bool, int]
_cache_by_root: "weakref.WeakKeyDictionary[tkinter.Tk, Dict[_IconKey, ctk.CTkImage]]" = weakref.WeakKeyDictionary()


def source_size_for(pixels: float) -> int:
    """要顯示 pixels 像素時用哪一張 PNG：不小於它的最小尺寸，都不夠大就用最大的。"""
    for size in ICON_SIZES:
        if size >= round(pixels):
            return size
    return ICON_SIZES[-1]


def current_scaling() -> float:
    """目前視窗的縮放倍率 (螢幕 DPI × CustomTkinter 的元件縮放)；還沒建立視窗時當成 1。"""
    dpi_scalings = ctk.ScalingTracker.window_dpi_scaling_dict.values()
    return max(dpi_scalings, default=1.0) * ctk.ScalingTracker.widget_scaling


@lru_cache(maxsize=None)
def _mask(name: str, size: int, filled: bool) -> Image.Image:
    path = ICON_DIR / f"{name}{'-filled' if filled else ''}_{size}.png"
    if not path.is_file():
        raise FileNotFoundError(f"找不到圖示 {path.name}；請確認名稱，或執行 tools/make_icons.py 重新產生")
    with Image.open(path) as image:
        return image.convert("L")


def _colored(mask: Image.Image, color: str) -> Image.Image:
    image = Image.new("RGBA", mask.size, color)
    image.putalpha(mask)
    return image


class IconButton(ctk.CTkButton):
    """前面有線條圖示的按鈕；停用時圖示跟文字一起換成 disabled 色 (CTkButton 本身只換文字顏色)。"""

    def __init__(self, master: Any, *, icon_name: str, icon_role: str = "text", icon_filled: bool = False, **kwargs: Any) -> None:
        self._icon_spec = (icon_name, icon_role, icon_filled)
        kwargs["image"] = self._icon_for_state(kwargs.get("state", "normal"))
        kwargs.setdefault("compound", "left")
        super().__init__(master, **kwargs)

    def _icon_for_state(self, state: str) -> ctk.CTkImage:
        name, role, filled = self._icon_spec
        return icon(name, role="disabled" if state == "disabled" else role, filled=filled)

    def configure(self, require_redraw: bool = False, **kwargs: Any) -> None:
        if "state" in kwargs and "image" not in kwargs:
            kwargs["image"] = self._icon_for_state(kwargs["state"])
        super().configure(require_redraw=require_redraw, **kwargs)


def icon(name: str, *, role: str = "text", size: int = 16, filled: bool = False, scaling: Optional[float] = None) -> ctk.CTkImage:
    """取得線條圖示。size 是 100% 縮放時的邊長 (按鈕與清單 16、側欄與底部動作列 18)。"""
    if role not in ICON_ROLES:
        raise ValueError(f"圖示顏色 {role} 不在 {ICON_ROLES}")
    source = source_size_for(size * (current_scaling() if scaling is None else scaling))
    key = (name, role, size, filled, source)
    root = tkinter._default_root  # 目前的主視窗；還沒建立時不快取
    cache = _cache_by_root.setdefault(root, {}) if root is not None else {}
    image = cache.get(key)
    if image is None:
        mask = _mask(name, source, filled)
        light, dark = T[role]
        image = ctk.CTkImage(light_image=_colored(mask, light), dark_image=_colored(mask, dark), size=(size, size))
        cache[key] = image
    return image
