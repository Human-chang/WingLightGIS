"""啟動畫面用的圖 (tools/make_splash.py)：螢火蟲亮、暗兩張，以及產品名與「啟動中」兩段字。

啟動畫面由啟動器 WingLightGIS.exe (launcher/WingLightGIS.cs) 畫：夜空底、螢火蟲、產品名、「啟動中」與三個點。
這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/make_splash.py 是同一份，改了要兩邊一起改。
要跟 make_icons.py、make_app_icon.py 放在同一個資料夾；firefly.svg 放 assets/logos_src/。
- 螢火蟲 (規範 Logos 的 firefly.svg)：動態照規範 Splash「只有靜態圖也可以」的做法，只做尾巴光暈的明暗——
  啟動器在暗的那張上面疊亮的那張、改變亮的那張的透明度 (兩張只差在光暈)，
  效果等於規範預覽裡光暈的透明度在 0.35 與 1 之間變化 (2.2 秒一個週期)。
  SVG 自己的底是 splash-bg 色的圓角方塊，疊在啟動畫面的同色底上看不出邊。
- 兩段字也先畫成圖：啟動器若用 GDI+ 畫字，第一次建立字型會列舉系統所有字型，
  這台電腦 (約 1260 個字型家族) 實測要 0.5 秒，啟動畫面會晚 0.5 秒才出現。
  字型、字級、顏色照規範：微軟正黑體 UI，splash-title 16 px 粗體 splash-text 色、splash-hint 12 px splash-muted 色；
  圖高等於一行 (字級 × 1.5)，字在圖裡上下置中，左右各留 1 px。

輸出 (assets/splash/)：
- firefly_bright.png、firefly_dim.png：440 px (畫面上是 220 px；其他縮放由啟動器縮小)。
- title_1x.png、title_2x.png、hint_1x.png、hint_2x.png：100% 縮放用 1x，其他用 2x 縮小。

用法 (要有微軟正黑體，Windows 10／11 內建；WingLightGIS 的 python 是 runtime\\python.exe)：
    python tools\\make_splash.py
"""

from __future__ import annotations

import math
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_app_icon import LOGO_SOURCE_DIR, PROJECT_ROOT, render_svg  # noqa: E402

FIREFLY_SVG = LOGO_SOURCE_DIR / "firefly.svg"
OUTPUT_DIR = PROJECT_ROOT / "assets" / "splash"
BRIGHT_PNG = OUTPUT_DIR / "firefly_bright.png"
DIM_PNG = OUTPUT_DIR / "firefly_dim.png"

FIREFLY_SIZE: int = 440
GLOW = "#FFC83D"
# 規範 Splash 預覽 (@keyframes wlglow)：光暈透明度 0.35 → 1 → 0.35
GLOW_DIM_OPACITY: float = 0.35

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# msjh.ttc／msjhbd.ttc 的第 2 個字型 (index 1) 是 Microsoft JhengHei UI
UI_FONT_INDEX: int = 1
LINE_HEIGHT: float = 1.5
TEXT_PAD: int = 1
TEXT_SCALES: Tuple[int, ...] = (1, 2)
# 名稱: (文字, 字級 px, 粗體, 顏色)
TEXTS: Dict[str, Tuple[str, int, bool, str]] = {
    "title": ("WingLightGIS", 16, True, "#F2F0EC"),   # splash-title、splash-text (各產品換成自己的產品名)
    "hint": ("啟動中", 12, False, "#B0ABA3"),          # splash-hint、splash-muted
}


def is_halo(element: ET.Element) -> bool:
    """尾巴的光暈：半透明的 glow 色圓 (星星與尾巴本身都是不透明的)。"""
    return (element.tag.rsplit("}", 1)[-1] == "circle"
            and element.get("fill", "").upper() == GLOW
            and float(element.get("fill-opacity", 1)) < 1)


def render_firefly(*, dim: bool = False) -> Image.Image:
    return render_svg(FIREFLY_SVG, FIREFLY_SIZE,
                      opacity_scale=(lambda el: GLOW_DIM_OPACITY if is_halo(el) else 1.0) if dim else None)


def ui_font(px: int, bold: bool) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_DIR / ("msjhbd.ttc" if bold else "msjh.ttc")), px, index=UI_FONT_INDEX)
    if font.getname()[0] != "Microsoft JhengHei UI":
        raise ValueError(f"字型不是 Microsoft JhengHei UI: {font.getname()}")
    return font


def render_text(name: str, scale: int) -> Image.Image:
    """一段字畫成透明底的圖：高一行、寬剛好放下字 (左右各留 TEXT_PAD)，字上下置中。"""
    text, px, bold, color = TEXTS[name]
    font = ui_font(px * scale, bold)
    width = math.ceil(font.getlength(text)) + 2 * TEXT_PAD * scale
    height = round(px * LINE_HEIGHT * scale)
    image = Image.new("RGBA", (width, height), color + "00")
    ImageDraw.Draw(image).text((width / 2, height / 2), text, font=font, fill=color, anchor="mm")
    return image


def text_png(name: str, scale: int) -> Path:
    return OUTPUT_DIR / f"{name}_{scale}x.png"


def generate() -> List[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    render_firefly().save(BRIGHT_PNG, optimize=True)
    render_firefly(dim=True).save(DIM_PNG, optimize=True)
    written = [BRIGHT_PNG, DIM_PNG]
    for name in TEXTS:
        for scale in TEXT_SCALES:
            render_text(name, scale).save(text_png(name, scale), optimize=True)
            written.append(text_png(name, scale))
    return written


def main() -> None:
    for path in generate():
        print(f"已產生 {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
