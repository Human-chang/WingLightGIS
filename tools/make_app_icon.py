"""App 圖示產生器 (tools/make_app_icon.py)：把 WingLight 設計規範 Logos 群組的 SVG 畫成 PNG 與 ICO。

開發時執行，產生的檔案跟著程式 (assets/)；程式與啟動器執行時都不讀 SVG，也不需要 SVG 套件 (只要 Pillow)。
這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/make_app_icon.py 是同一份，改了要兩邊一起改。
要跟 make_icons.py 放在同一個資料夾 (共用路徑解析與圓頭筆畫)。
規範的標誌 SVG 只用到平塗色：<path>、<circle>、<ellipse>、<rect> (可有 rx)、fill／stroke 與各自的透明度、
圓頭的筆畫、<clipPath>，所以這裡只實作這些 (路徑解析與筆畫沿用 tools/make_icons.py)，遇到別的元素就報錯。
填色是把每個子路徑塗滿後取聯集：規範的 SVG 沒有挖洞的路徑，細節都是用另一個形狀蓋上去。
跟線條圖示一樣先畫大幾倍再縮小，邊緣才平滑；縮小前先換成預乘透明度，透明邊緣才不會有一圈暗色。

輸出：
- assets/logo.png：512 px (README、關於視窗)。
- assets/app_icon.ico：16、24、32、48、64、128、256 px，每個尺寸各自畫 (不是由大圖縮小)；
  16、24 px 照規範拿掉左上角的高光 (SVG 裡唯一有 clip-path 的元素)。
  256 px 存成 PNG，其餘存成 32 位元 BMP (舊的 Windows API、Tk、csc 的 /win32icon 都讀得懂)。

用法 (放在專案的 tools/ 底下，SVG 放 assets/logos_src/；WingLightGIS 的 python 是 runtime\\python.exe)：
    python tools\\make_app_icon.py
"""

from __future__ import annotations

import io
import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_icons import Point, Subpath, draw_round_stroke, parse_path  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGO_SOURCE_DIR = PROJECT_ROOT / "assets" / "logos_src"
# 各產品換成自己的 App 圖示 (規範 Logos：winglightpdf.svg、winglightfinder.svg、winglightgis.svg)
APP_ICON_SVG = LOGO_SOURCE_DIR / "winglightgis.svg"
LOGO_PNG = PROJECT_ROOT / "assets" / "logo.png"
APP_ICON_ICO = PROJECT_ROOT / "assets" / "app_icon.ico"

LOGO_SIZE: int = 512
ICO_SIZES: Tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
# 規範：16 與 24 px 可以拿掉高光和最細的線 (三層圖層的線寬是邊長的 5%，比規範下限 4% 粗，留著)
PLAIN_SIZES: Tuple[int, ...] = (16, 24)
# ICO 裡這個尺寸以上存 PNG (256 px 的 BMP 要 260 KB)
ICO_PNG_MIN_SIZE: int = 256
# 先畫大幾倍再縮小：小圖用 8 倍 (同線條圖示)，大圖 4 倍就夠平滑
SUPERSAMPLE_SMALL: int = 8
SUPERSAMPLE_LARGE: int = 4
CIRCLE_STEPS: int = 180

Rgb = Tuple[int, int, int]
SkipRule = Callable[[ET.Element], bool]


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _color(value: Optional[str]) -> Optional[Rgb]:
    """#RGB／#RRGGBB 轉成 (r, g, b)；none 回傳 None。"""
    if value is None or value.strip() == "none":
        return None
    text = value.strip()
    if text.startswith("#") and len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    if text.startswith("#") and len(text) == 7:
        return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
    raise ValueError(f"不支援的顏色寫法: {value}")


def _ellipse(cx: float, cy: float, rx: float, ry: float) -> List[Subpath]:
    points = [(cx + rx * math.cos(2 * math.pi * i / CIRCLE_STEPS), cy + ry * math.sin(2 * math.pi * i / CIRCLE_STEPS))
              for i in range(CIRCLE_STEPS)]
    return [(points, True)]


def element_subpaths(element: ET.Element) -> List[Subpath]:
    """一個圖形元素轉成子路徑 (viewBox 座標)。"""
    tag = _tag(element)

    def num(name: str) -> float:
        return float(element.get(name, 0))

    if tag == "path":
        return parse_path(element.get("d", ""))
    if tag == "circle":
        return _ellipse(num("cx"), num("cy"), num("r"), num("r"))
    if tag == "ellipse":
        return _ellipse(num("cx"), num("cy"), num("rx"), num("ry"))
    if tag == "rect":
        x, y, w, h = num("x"), num("y"), num("width"), num("height")
        rx_attr, ry_attr = element.get("rx"), element.get("ry")
        rx = float(rx_attr if rx_attr is not None else (ry_attr or 0))
        ry = float(ry_attr if ry_attr is not None else rx)
        rx, ry = min(rx, w / 2), min(ry, h / 2)
        if rx <= 0 or ry <= 0:
            return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)]
        return parse_path(
            f"M{x + rx} {y}H{x + w - rx}A{rx} {ry} 0 0 1 {x + w} {y + ry}V{y + h - ry}"
            f"A{rx} {ry} 0 0 1 {x + w - rx} {y + h}H{x + rx}A{rx} {ry} 0 0 1 {x} {y + h - ry}"
            f"V{y + ry}A{rx} {ry} 0 0 1 {x + rx} {y}Z"
        )
    raise ValueError(f"不支援的 SVG 元素 <{tag}>")


def _fill_mask(subpaths: Sequence[Subpath], size: Tuple[int, int]) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for points, _closed in subpaths:
        if len(points) >= 3:
            draw.polygon(points, fill=255)
    return mask


def _stroke_mask(subpaths: Sequence[Subpath], size: Tuple[int, int], width: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for points, closed in subpaths:
        draw_round_stroke(draw, points, closed, width / 2)
    return mask


def _paint(canvas: Image.Image, mask: Image.Image, rgb: Rgb, opacity: float, clip: Optional[Image.Image]) -> None:
    if clip is not None:
        mask = ImageChops.multiply(mask, clip)
    if opacity < 1:
        mask = mask.point(lambda v: round(v * opacity))
    layer = Image.new("RGBA", canvas.size, rgb + (0,))
    layer.putalpha(mask)
    canvas.alpha_composite(layer)


def render_svg(svg_path: Path, width: int, height: Optional[int] = None, *,
               skip: Optional[SkipRule] = None,
               opacity_scale: Optional[Callable[[ET.Element], float]] = None) -> Image.Image:
    """把 SVG 畫成 width×height 的 RGBA 圖 (沒給高度就照 viewBox 比例)。

    skip 回傳 True 的元素不畫；opacity_scale 回傳的倍數乘上該元素的透明度 (啟動畫面用它畫光暈暗的那張)。
    """
    root = ET.parse(svg_path).getroot()
    vb_x, vb_y, vb_w, vb_h = (float(v) for v in root.get("viewBox", "").replace(",", " ").split())
    height = height or round(width * vb_h / vb_w)
    supersample = SUPERSAMPLE_SMALL if max(width, height) <= 64 else SUPERSAMPLE_LARGE
    size = (width * supersample, height * supersample)
    scale_x, scale_y = size[0] / vb_w, size[1] / vb_h

    def placed(subpaths: Sequence[Subpath]) -> List[Subpath]:
        return [([((x - vb_x) * scale_x, (y - vb_y) * scale_y) for x, y in points], closed) for points, closed in subpaths]

    clip_paths: Dict[str, ET.Element] = {el.get("id", ""): el for el in root.iter() if _tag(el) == "clipPath"}
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for element in root:
        if _tag(element) == "defs" or (skip is not None and skip(element)):
            continue
        subpaths = placed(element_subpaths(element))
        clip = None
        clip_ref = element.get("clip-path")
        if clip_ref:
            clip_id = clip_ref.strip()[len("url(#"):-1]
            clip = Image.new("L", size, 0)
            for child in clip_paths[clip_id]:
                clip = ImageChops.lighter(clip, _fill_mask(placed(element_subpaths(child)), size))
        opacity = float(element.get("opacity", 1)) * (opacity_scale(element) if opacity_scale else 1.0)
        fill = _color(element.get("fill", "#000000"))
        if fill is not None:
            _paint(canvas, _fill_mask(subpaths, size), fill, opacity * float(element.get("fill-opacity", 1)), clip)
        stroke = _color(element.get("stroke"))
        if stroke is not None:
            if any(not closed for _, closed in subpaths) and element.get("stroke-linecap", "butt") != "round":
                raise ValueError(f"{svg_path.name}：只支援圓頭的開放筆畫")
            width_px = float(element.get("stroke-width", 1)) * scale_x
            _paint(canvas, _stroke_mask(subpaths, size, width_px), stroke,
                   opacity * float(element.get("stroke-opacity", 1)), clip)
    # 預乘透明度後再縮小，透明邊緣才不會混進透明像素的黑色
    return canvas.convert("RGBa").resize((width, height), Image.BOX).convert("RGBA")


def is_highlight(element: ET.Element) -> bool:
    """App 圖示左上角的高光：規範的 App 圖示 SVG 裡唯一有 clip-path 的元素。"""
    return element.get("clip-path") is not None


def render_app_icon(size: int) -> Image.Image:
    return render_svg(APP_ICON_SVG, size, skip=is_highlight if size in PLAIN_SIZES else None)


def _ico_bmp(image: Image.Image) -> bytes:
    """ICO 裡的 32 位元 BMP：BITMAPINFOHEADER (高度寫兩倍)＋由下往上的 BGRA＋1 位元的 AND 遮罩。"""
    width, height = image.size
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    flipped = image.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    pixels = flipped.tobytes("raw", "BGRA")
    alpha = flipped.getchannel("A").tobytes()
    row_bytes = (width + 31) // 32 * 4
    and_mask = bytearray(row_bytes * height)
    for y in range(height):
        for x in range(width):
            if alpha[y * width + x] == 0:
                and_mask[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    return header + pixels + bytes(and_mask)


def write_ico(images: Sequence[Image.Image], path: Path) -> None:
    """多尺寸 ICO：大圖存 PNG、小圖存 BMP。"""
    blobs: List[Tuple[int, int, bytes]] = []
    for image in images:
        width, height = image.size
        if width >= ICO_PNG_MIN_SIZE:
            buffer = io.BytesIO()
            image.save(buffer, "PNG", optimize=True)
            data = buffer.getvalue()
        else:
            data = _ico_bmp(image)
        blobs.append((width, height, data))
    offset = 6 + 16 * len(blobs)
    directory = b""
    for width, height, data in blobs:
        directory += struct.pack("<BBBBHHII", width % 256, height % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(blobs)) + directory + b"".join(data for _, _, data in blobs))


def generate() -> List[Path]:
    """重新產生 assets/logo.png 與 assets/app_icon.ico；回傳寫出的檔案。"""
    render_app_icon(LOGO_SIZE).save(LOGO_PNG, optimize=True)
    write_ico([render_app_icon(size) for size in ICO_SIZES], APP_ICON_ICO)
    return [LOGO_PNG, APP_ICON_ICO]


def main() -> None:
    for path in generate():
        print(f"已產生 {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
