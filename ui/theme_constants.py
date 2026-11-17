"""
WingLight 色票與字型 (WingLightGIS 的 ui/theme_constants.py)
====================================================
照「WingLight 設計規範」(https://claude.ai/artifact/5SRQQqi2AH36FKiDw7XbLR) 的 tokens.json：
每個色票是 (淺色, 深色) 一組，名稱跟規範一樣；CustomTkinter 的顏色參數直接吃這種一組兩個的值，
會跟著深淺色主題自動換。改顏色要先改規範，再改這裡。
這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/winglight_ctk.py 是同一份，改了要兩邊一起改。
用法：建立主視窗之前呼叫 apply_ctk_theme()，之後元件的顏色寫 T["accent"] 這種一組兩個的值。

按鈕只有兩種 (規範的 Button 元件)，建立時以 **BTN_PRIMARY 這種寫法帶入：
- BTN_PRIMARY：一個畫面唯一的主要動作 (開始裁切)。
- BTN_SECONDARY：其他所有動作，移除、清空也是 (系列裡沒有紅色的危險按鈕)。
狀態色 ok／warn／hi 只當文字與圖示的顏色，一定同時配文字，不能只靠顏色。

字級：CustomTkinter 的字級數字是「像素」(CTkFont 內部轉成負值)，再依螢幕縮放放大，
所以直接寫規範的 px，不要換算成 pt。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

ColorPair = Tuple[str, str]

# 規範 tokens.json 的色票 (淺色, 深色)。只有一個值的品牌色兩邊相同。
T: Dict[str, ColorPair] = {
    "bg": ("#F6F5F3", "#1C1B1A"),
    "panel": ("#FFFFFF", "#272625"),
    "chrome": ("#EEECE8", "#141312"),
    "input": ("#F6F5F3", "#1C1B1A"),
    "hover": ("#E6E3DD", "#33312F"),
    "soft": ("#E6ECFB", "#2A3152"),
    "line": ("#E3E0DB", "#3D3B38"),
    "text": ("#1F1E1C", "#F2F0EC"),
    "muted": ("#5F5B55", "#B0ABA3"),
    "disabled": ("#A39E96", "#6E6A64"),
    "accent": ("#2B55D4", "#8FA8FF"),
    "accent-hover": ("#2347B8", "#A9BCFF"),
    "on-accent": ("#FFFFFF", "#10142B"),
    "ok": ("#1B7550", "#6FD3A6"),
    "warn": ("#8A5300", "#F2C46B"),
    "hi": ("#C2362F", "#FF8A80"),
    "product-pdf": ("#D8433B", "#D8433B"),
    "product-finder": ("#C77700", "#C77700"),
    "product-gis": ("#1E8F63", "#1E8F63"),
    "glow": ("#FFC83D", "#FFC83D"),
    "splash-bg": ("#151A2E", "#151A2E"),
    "splash-text": ("#F2F0EC", "#F2F0EC"),
    "splash-muted": ("#B0ABA3", "#B0ABA3"),
}

# 圓角 (px)
RADIUS_SM: int = 6
RADIUS_MD: int = 8
RADIUS_LG: int = 10
RADIUS_XL: int = 12

BTN_PRIMARY: Dict[str, Any] = {
    "fg_color": T["accent"],
    "hover_color": T["accent-hover"],
    "text_color": T["on-accent"],
    "text_color_disabled": T["disabled"],
    "border_width": 0,
    "corner_radius": RADIUS_MD,
    "height": 36,
}

BTN_SECONDARY: Dict[str, Any] = {
    "fg_color": T["input"],
    "hover_color": T["hover"],
    "text_color": T["text"],
    "text_color_disabled": T["disabled"],
    "border_width": 1,
    "border_color": T["line"],
    "corner_radius": RADIUS_MD,
}

STATUS_OK: ColorPair = T["ok"]
STATUS_WARNING: ColorPair = T["warn"]
STATUS_ERROR: ColorPair = T["hi"]

# 卡片底色
CARD_FG_COLOR: ColorPair = T["panel"]

# 字型與字級 (px，見檔頭說明)
FONT_FAMILY: str = "Microsoft JhengHei UI"
FONT_FAMILY_MONO: str = "Consolas"
FONT_SIZE_PAGE_TITLE: int = 20  # 工具名稱，一個畫面只有一個
FONT_SIZE_CARD_TITLE: int = 14  # 卡片標題 (粗體)
FONT_SIZE_BODY: int = 13  # 一般文字：按鈕、輸入框、清單、勾選框
FONT_SIZE_CAPTION: int = 12  # 欄位名稱、表頭、狀態列
FONT_SIZE_OVERLINE: int = 11  # 側欄分組小標 (粗體)
FONT_SIZE_MONO: int = 12  # 座標、路徑 (Consolas)


def color_for_mode(pair: ColorPair, mode: str) -> str:
    """從 (淺色, 深色) 取出指定主題的顏色；給 tk.Text 標籤這類只吃單一顏色的地方用。"""
    return pair[1] if str(mode).lower() == "dark" else pair[0]


def ctk_theme_overrides() -> Dict[str, Dict[str, Any]]:
    """CustomTkinter 主題要覆蓋的值：沒指定顏色的元件也會是 WingLight 的樣子。"""
    return {
        "CTk": {"fg_color": T["bg"]},
        "CTkToplevel": {"fg_color": T["bg"]},
        "CTkFrame": {
            "corner_radius": RADIUS_LG,
            "border_width": 0,
            "fg_color": T["panel"],
            "top_fg_color": T["input"],
            "border_color": T["line"],
        },
        # 沒指定樣式的按鈕一律是次要按鈕；主要按鈕要明確帶入 BTN_PRIMARY
        "CTkButton": {
            "corner_radius": RADIUS_MD,
            "border_width": 1,
            "fg_color": T["input"],
            "hover_color": T["hover"],
            "border_color": T["line"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkLabel": {"text_color": T["text"]},
        "CTkEntry": {
            "corner_radius": RADIUS_MD,
            "border_width": 1,
            "fg_color": T["input"],
            "border_color": T["line"],
            "text_color": T["text"],
            "placeholder_text_color": T["muted"],
        },
        # 勾選框沒有填色可以分辨，外框用 muted (規範「色彩」)
        "CTkCheckBox": {
            "corner_radius": 4,
            "border_width": 2,
            "fg_color": T["accent"],
            "border_color": T["muted"],
            "hover_color": T["accent-hover"],
            "checkmark_color": T["on-accent"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkSwitch": {
            "fg_color": T["hover"],
            "progress_color": T["accent"],
            "button_color": T["muted"],
            "button_hover_color": T["text"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkRadioButton": {
            "fg_color": T["accent"],
            "border_color": T["muted"],
            "hover_color": T["accent-hover"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkProgressBar": {"fg_color": T["hover"], "progress_color": T["accent"], "border_color": T["line"]},
        "CTkSlider": {
            "fg_color": T["hover"],
            "progress_color": T["accent"],
            "button_color": T["accent"],
            "button_hover_color": T["accent-hover"],
        },
        "CTkOptionMenu": {
            "corner_radius": RADIUS_MD,
            "fg_color": T["input"],
            "button_color": T["input"],
            "button_hover_color": T["hover"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkComboBox": {
            "corner_radius": RADIUS_MD,
            "border_width": 1,
            "fg_color": T["input"],
            "border_color": T["line"],
            "button_color": T["line"],
            "button_hover_color": T["hover"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkScrollbar": {"button_color": T["disabled"], "button_hover_color": T["muted"]},
        "CTkSegmentedButton": {
            "corner_radius": RADIUS_MD,
            "border_width": 2,
            "fg_color": T["input"],
            "selected_color": T["panel"],
            "selected_hover_color": T["panel"],
            "unselected_color": T["input"],
            "unselected_hover_color": T["hover"],
            "text_color": T["text"],
            "text_color_disabled": T["disabled"],
        },
        "CTkTextbox": {
            "corner_radius": RADIUS_MD,
            "border_width": 1,
            "fg_color": T["input"],
            "border_color": T["line"],
            "text_color": T["text"],
            "scrollbar_button_color": T["disabled"],
            "scrollbar_button_hover_color": T["muted"],
        },
        "CTkScrollableFrame": {"label_fg_color": T["chrome"]},
        "DropdownMenu": {"fg_color": T["panel"], "hover_color": T["hover"], "text_color": T["text"]},
        "CTkFont": {"family": FONT_FAMILY, "size": FONT_SIZE_BODY, "weight": "normal"},
    }


def apply_ctk_theme() -> None:
    """把 CustomTkinter 的預設主題換成 WingLight 色票。要在建立主視窗與任何元件之前呼叫。"""
    import customtkinter as ctk

    # 先載入完整的內建主題 (每個元件的所有鍵都在)，再覆蓋成規範的值
    ctk.set_default_color_theme("blue")
    theme = ctk.ThemeManager.theme
    for widget_name, values in ctk_theme_overrides().items():
        theme.setdefault(widget_name, {}).update(values)
