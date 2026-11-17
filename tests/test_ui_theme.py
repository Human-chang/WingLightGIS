"""WingLight 設計規範第 6 版的色票、CustomTkinter 主題、介面文字不放 emoji (風格統一 A 段)。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import customtkinter as ctk
from tkinter import ttk

from tests._support import PROJECT_ROOT, IsolatedWorkspaceTestCase
from ui.theme_constants import FONT_FAMILY, FONT_SIZE_BODY, T, color_for_mode

# 規範 tokens.json 的色票名稱 (淺／深兩值的 16 個，加上只有一個值的品牌色 7 個)
SPEC_TOKEN_NAMES = {
    "bg", "panel", "chrome", "input", "hover", "soft", "line", "text", "muted", "disabled",
    "accent", "accent-hover", "on-accent", "ok", "warn", "hi",
    "product-pdf", "product-finder", "product-gis", "glow", "splash-bg", "splash-text", "splash-muted",
}
HEX_COLOR = re.compile(r"^#[0-9A-F]{6}$")
# emoji 與 Tk 會去掃系統字型找的符號：補充平面、雜項符號與裝飾符號、雜項技術符號、幾何形狀、變體選擇符
EMOJI_LIKE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿■-◿️]")


class TestTokens(unittest.TestCase):
    def test_tokens_match_spec_names_and_are_hex_pairs(self) -> None:
        self.assertEqual(set(T), SPEC_TOKEN_NAMES)
        for name, (light, dark) in T.items():
            self.assertRegex(light, HEX_COLOR, name)
            self.assertRegex(dark, HEX_COLOR, name)

    def test_key_values_follow_spec(self) -> None:
        self.assertEqual(T["bg"], ("#F6F5F3", "#1C1B1A"))
        self.assertEqual(T["accent"], ("#2B55D4", "#8FA8FF"))
        self.assertEqual(T["on-accent"], ("#FFFFFF", "#10142B"))
        self.assertEqual(T["product-gis"][0], "#1E8F63")
        self.assertEqual(T["product-pdf"], ("#D8433B", "#D8433B"))
        self.assertEqual(T["product-finder"], ("#C77700", "#C77700"))
        self.assertEqual(T["glow"][0], "#FFC83D")

    def test_color_for_mode(self) -> None:
        self.assertEqual(color_for_mode(T["soft"], "Light"), "#E6ECFB")
        self.assertEqual(color_for_mode(T["soft"], "Dark"), "#2A3152")

    def test_ui_source_has_no_emoji(self) -> None:
        found = []
        for path in sorted((PROJECT_ROOT / "ui").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if EMOJI_LIKE.search(line):
                    found.append(f"{path.name}:{number}: {line.strip()[:60]}")
        self.assertEqual(found, [], "介面文字不放 emoji，改用線條圖示 PNG")


class TestCtkTheme(IsolatedWorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        import ui.dashboard_app as dash

        self.app = dash.GisMasterToolkitApp(start_tool="dem")
        self.app.update()

    def tearDown(self) -> None:
        ctk.set_appearance_mode("Dark")
        self.app.destroy()
        super().tearDown()

    def test_window_and_default_widgets_use_the_theme(self) -> None:
        self.assertEqual(tuple(self.app.cget("fg_color")), T["bg"])
        theme = ctk.ThemeManager.theme
        self.assertEqual(theme["CTkFont"]["family"], FONT_FAMILY)
        self.assertEqual(theme["CTkFont"]["size"], FONT_SIZE_BODY)
        self.assertEqual(tuple(theme["CTkButton"]["fg_color"]), T["input"], "沒指定樣式的按鈕是次要按鈕")
        self.assertEqual(tuple(theme["CTkCheckBox"]["border_color"]), T["muted"])
        self.assertEqual(self.app.title(), "WingLightGIS")

    def test_file_list_selected_row_follows_appearance_mode(self) -> None:
        from ui.check_list import STYLE_NAME

        file_list = self.app.tab_dem.file_list
        style = ttk.Style(file_list)
        ctk.set_appearance_mode("Light")
        self.app.update()
        self.assertEqual([tuple(map(str, e)) for e in style.map(STYLE_NAME, "background")], [("selected", T["soft"][0])])
        self.assertEqual(style.lookup(STYLE_NAME, "foreground"), T["text"][0])
        ctk.set_appearance_mode("Dark")
        self.app.update()
        self.assertEqual([tuple(map(str, e)) for e in style.map(STYLE_NAME, "background")], [("selected", T["soft"][1])])


if __name__ == "__main__":
    unittest.main()
