"""「關於」視窗用的圖 (tools/make_about.py)：系列標誌 (規範 Logos 的 winglight-mark.svg) 畫成 PNG。

關於視窗 (ui/about_dialog.py) 最下面一行「WingLight 系列」前面放標誌，畫面上 24 px (規範標誌最小 24 px)；
這裡畫 2 倍大 (48 px)，放大顯示時才不會糊。螢火蟲直接用啟動畫面那張亮的 (tools/make_splash.py)。
繪圖沿用 tools/make_app_icon.py 的 render_svg (程式執行時不讀 SVG)。

輸出：assets/about/winglight_mark.png

用法：
    runtime\\python.exe tools\\make_about.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_app_icon import LOGO_SOURCE_DIR, PROJECT_ROOT, render_svg  # noqa: E402

MARK_SVG = LOGO_SOURCE_DIR / "winglight-mark.svg"
OUTPUT_DIR = PROJECT_ROOT / "assets" / "about"
MARK_PNG = OUTPUT_DIR / "winglight_mark.png"

MARK_DISPLAY_SIZE: int = 24
MARK_SIZE: int = MARK_DISPLAY_SIZE * 2


def render_mark() -> Image.Image:
    return render_svg(MARK_SVG, MARK_SIZE)


def generate() -> List[Path]:
    """重新產生 assets/about/ 的圖；回傳寫出的檔案。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    render_mark().save(MARK_PNG, optimize=True)
    return [MARK_PNG]


def main() -> None:
    for path in generate():
        print(f"已產生 {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
