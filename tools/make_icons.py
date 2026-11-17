"""線條圖示產生器 (tools/make_icons.py)：把 WingLight 設計規範 Icons 群組的 SVG 轉成 PNG。

開發時執行，產生的 PNG 跟著程式；程式執行時不讀 SVG，也不需要 SVG 套件 (只要 Pillow)。
這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/make_icons.py 是同一份，改了要兩邊一起改。
規範的圖示都是 24 px 畫格、單一 <path>、1.75 px 筆畫、圓頭、圓角接合，所以這裡只實作需要的部分：
路徑指令 M L H V C S Q T A Z (大小寫皆可)，用 Pillow 先畫大 SUPERSAMPLE 倍再縮小，邊緣才平滑。

輸出 (assets/icons/)：
- 遮罩 <名稱>_<尺寸>.png：灰階，亮度＝不透明度。程式執行時再塗上主題色 (WingLightGIS 的 ui/icons.py)，
  所以同一張遮罩可以用在 text、muted、accent 等任何顏色，不必每種顏色各存一份。
- 填滿版 <名稱>-filled_<尺寸>.png：筆畫加上把封閉路徑塗滿，給主要按鈕裡的執行三角形 (規範 Button)。
尺寸 16、20、24、32 px，依螢幕縮放挑：100% 用 16、125% 用 20、150% 用 24、200% 用 32。

用法 (放在專案的 tools/ 底下，讀 assets/icons_src/ 的 SVG；WingLightGIS 的 python 是 runtime\\python.exe)：
    python tools\\make_icons.py              # 重新產生 assets/icons/
    python tools\\make_icons.py --sheet 圖.png  # 另外輸出一張總覽圖 (淺／深色底、四種尺寸)
"""

from __future__ import annotations

import argparse
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "assets" / "icons_src"
OUTPUT_DIR = PROJECT_ROOT / "assets" / "icons"

SIZES: Tuple[int, ...] = (16, 20, 24, 32)
# 先畫大幾倍再縮小：8 倍時每個像素有 64 個取樣點，1 px 左右的細線邊緣也平滑
SUPERSAMPLE: int = 8
VIEWBOX: float = 24.0
STROKE_WIDTH: float = 1.75
# 要另外出填滿版的圖示 (主要按鈕裡用)
FILLED_ICONS: Tuple[str, ...] = ("play",)
# 總覽圖用的底色與圖示色 (規範 tokens.json 的 (淺色, 深色))
SHEET_COLORS = {
    "panel": ("#FFFFFF", "#272625"),
    "muted": ("#5F5B55", "#B0ABA3"),
    "accent": ("#2B55D4", "#8FA8FF"),
}

Point = Tuple[float, float]
Subpath = Tuple[List[Point], bool]  # (點列, 是否封閉)

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_ARGS_PER_COMMAND = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int = 24) -> List[Point]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        out.append((
            mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
            mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1],
        ))
    return out


def _quadratic(p0: Point, p1: Point, p2: Point, steps: int = 24) -> List[Point]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        out.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0], mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
    return out


def _arc(p0: Point, rx: float, ry: float, phi_deg: float, large: bool, sweep: bool, p1: Point) -> List[Point]:
    """SVG 橢圓弧 (端點參數) 轉成折線；換算方式照 SVG 規格 F.6.5。"""
    if p0 == p1:
        return []
    if rx == 0 or ry == 0:
        return [p1]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    # 半徑太小時放大到剛好能接上兩端
    scale = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if scale > 1:
        rx, ry = rx * math.sqrt(scale), ry * math.sqrt(scale)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (p0[0] + p1[0]) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (p0[1] + p1[1]) / 2

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    steps = max(4, int(abs(delta) / math.radians(4)))
    out = []
    for i in range(1, steps + 1):
        t = theta1 + delta * i / steps
        x, y = rx * math.cos(t), ry * math.sin(t)
        out.append((cos_phi * x - sin_phi * y + cx, sin_phi * x + cos_phi * y + cy))
    out[-1] = p1
    return out


def parse_path(d: str) -> List[Subpath]:
    """SVG path 的 d 屬性轉成數個子路徑的折線 (24 px 畫格座標)。"""
    tokens = _TOKEN.findall(d)
    subpaths: List[Subpath] = []
    points: List[Point] = []
    closed = False
    cur: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    last_ctrl: Point = (0.0, 0.0)
    last_cmd = ""
    i = 0
    cmd = ""

    def flush() -> None:
        nonlocal points, closed
        if points:
            subpaths.append((points, closed))
        points, closed = [], False

    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in "Zz":
                closed = True
                cur = start
                flush()
                last_cmd = "Z"
                continue
        elif not cmd:
            raise ValueError(f"路徑開頭不是指令: {d[:30]}")
        upper = cmd.upper()
        count = _ARGS_PER_COMMAND[upper]
        args = [float(v) for v in tokens[i:i + count]]
        if len(args) < count:
            raise ValueError(f"指令 {cmd} 的參數不夠: {d[:30]}")
        i += count
        rel = cmd.islower()
        ox, oy = cur if rel else (0.0, 0.0)

        if upper == "M":
            flush()
            cur = (args[0] + ox, args[1] + oy)
            start = cur
            points = [cur]
            # M 後面接的數字對當成 L
            cmd = "l" if rel else "L"
        elif upper == "L":
            cur = (args[0] + ox, args[1] + oy)
            points.append(cur)
        elif upper == "H":
            cur = (args[0] + (cur[0] if rel else 0.0), cur[1])
            points.append(cur)
        elif upper == "V":
            cur = (cur[0], args[0] + (cur[1] if rel else 0.0))
            points.append(cur)
        elif upper == "C":
            c1 = (args[0] + ox, args[1] + oy)
            c2 = (args[2] + ox, args[3] + oy)
            end = (args[4] + ox, args[5] + oy)
            points.extend(_cubic(cur, c1, c2, end))
            last_ctrl, cur = c2, end
        elif upper == "S":
            c1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1]) if last_cmd in ("C", "S") else cur
            c2 = (args[0] + ox, args[1] + oy)
            end = (args[2] + ox, args[3] + oy)
            points.extend(_cubic(cur, c1, c2, end))
            last_ctrl, cur = c2, end
        elif upper == "Q":
            c1 = (args[0] + ox, args[1] + oy)
            end = (args[2] + ox, args[3] + oy)
            points.extend(_quadratic(cur, c1, end))
            last_ctrl, cur = c1, end
        elif upper == "T":
            c1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1]) if last_cmd in ("Q", "T") else cur
            end = (args[0] + ox, args[1] + oy)
            points.extend(_quadratic(cur, c1, end))
            last_ctrl, cur = c1, end
        elif upper == "A":
            end = (args[5] + ox, args[6] + oy)
            points.extend(_arc(cur, args[0], args[1], args[2], bool(args[3]), bool(args[4]), end))
            cur = end
        last_cmd = upper if upper not in ("M",) else "L"
    flush()
    return subpaths


def load_icon_paths(svg_path: Path) -> List[Subpath]:
    """讀規範的圖示 SVG：檢查畫格與筆畫，回傳所有 <path> 的子路徑。"""
    root = ET.parse(svg_path).getroot()
    view_box = [float(v) for v in root.get("viewBox", "0 0 24 24").split()]
    if view_box != [0.0, 0.0, VIEWBOX, VIEWBOX]:
        raise ValueError(f"{svg_path.name}：畫格不是 0 0 24 24")
    if float(root.get("stroke-width", STROKE_WIDTH)) != STROKE_WIDTH:
        raise ValueError(f"{svg_path.name}：筆畫不是 {STROKE_WIDTH}")
    subpaths: List[Subpath] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "path":
            subpaths.extend(parse_path(element.get("d", "")))
    if not subpaths:
        raise ValueError(f"{svg_path.name}：找不到 <path>")
    return subpaths


def draw_round_stroke(draw: ImageDraw.ImageDraw, points: Sequence[Point], closed: bool, half: float) -> None:
    """圓頭圓角的粗線 (半寬 half)：每一段線畫成矩形，再在每個頂點畫一個圓 (等於線段與圓的聯集)，
    頓點 (長度 0 的線段) 會變成圓點。tools/make_app_icon.py 也用這個畫標誌的筆畫。"""
    pts = list(points)
    if closed and pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length > 1e-9:
            nx, ny = -(y1 - y0) / length * half, (x1 - x0) / length * half
            draw.polygon([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)], fill=255)
    for x, y in pts:
        draw.ellipse((x - half, y - half, x + half, y + half), fill=255)


def render_mask(subpaths: Sequence[Subpath], size: int, *, filled: bool = False) -> Image.Image:
    """畫成 size×size 的灰階遮罩 (255＝完全不透明)。"""
    big = size * SUPERSAMPLE
    scale = big / VIEWBOX
    half = STROKE_WIDTH * scale / 2
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)

    def scaled(points: Iterable[Point]) -> List[Point]:
        return [(x * scale, y * scale) for x, y in points]

    if filled:
        for points, closed in subpaths:
            if closed and len(points) >= 3:
                draw.polygon(scaled(points), fill=255)
    for points, closed in subpaths:
        draw_round_stroke(draw, scaled(points), closed, half)
    return mask.resize((size, size), Image.BOX)


def icon_names() -> List[str]:
    return sorted(p.stem for p in SOURCE_DIR.glob("*.svg"))


def output_name(name: str, size: int, *, filled: bool = False) -> str:
    return f"{name}{'-filled' if filled else ''}_{size}.png"


def generate(output_dir: Path = OUTPUT_DIR) -> List[Path]:
    """依 assets/icons_src/ 重新產生全部遮罩 PNG；回傳寫出的檔案。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name in icon_names():
        subpaths = load_icon_paths(SOURCE_DIR / f"{name}.svg")
        variants = [False, True] if name in FILLED_ICONS else [False]
        for filled in variants:
            for size in SIZES:
                path = output_dir / output_name(name, size, filled=filled)
                render_mask(subpaths, size, filled=filled).save(path, optimize=True)
                written.append(path)
    return written


def contact_sheet(path: Path) -> None:
    """總覽圖：每個圖示在淺色 (muted、accent) 與深色 (muted、accent) 底上的四種尺寸，放大 2 倍方便看。"""
    names = icon_names()
    cell = 40
    themes = [("light", 0), ("dark", 1)]
    roles = ["muted", "accent"]
    columns = len(themes) * len(roles) * len(SIZES)
    sheet = Image.new("RGB", (columns * cell, len(names) * cell), "white")
    for row, name in enumerate(names):
        subpaths = load_icon_paths(SOURCE_DIR / f"{name}.svg")
        col = 0
        for _, idx in themes:
            for role in roles:
                for size in SIZES:
                    bg = Image.new("RGB", (cell, cell), SHEET_COLORS["panel"][idx])
                    mask = render_mask(subpaths, size)
                    ink = Image.new("RGB", (size, size), SHEET_COLORS[role][idx])
                    bg.paste(ink, ((cell - size) // 2, (cell - size) // 2), mask)
                    sheet.paste(bg, (col * cell, row * cell))
                    col += 1
    sheet.resize((sheet.width * 2, sheet.height * 2), Image.NEAREST).save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="把規範的線條圖示 SVG 轉成各尺寸的 PNG 遮罩")
    parser.add_argument("--sheet", type=Path, help="另外輸出一張總覽圖到這個路徑")
    args = parser.parse_args()
    written = generate()
    print(f"已產生 {len(written)} 個 PNG 到 {OUTPUT_DIR}")
    if args.sheet:
        contact_sheet(args.sheet)
        print(f"總覽圖：{args.sheet}")


if __name__ == "__main__":
    main()
