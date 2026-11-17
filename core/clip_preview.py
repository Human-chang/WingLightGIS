"""裁切分頁的預覽 (core/clip_preview.py)

1. 右欄縮圖 render_clip_thumbnail：原始檔縮圖＋裁切邊界 (紅框) 畫在實際位置。
   - DEM：gdaldem 山體陰影 × 彩色高程 (色階依這幅 DEM 的最低到最高拉開，平地也看得出起伏)。
   - 正射影像：直接縮小讀取；非 8 位元的依 2%～98% 拉伸成 8 位元。
   - 向量：gdal_rasterize 把每個圖徵燒成自己的編號，畫出每個圖徵的外框 (相鄰宗地的界線也看得到) 與淡色填色。
   邊界與原始檔的座標系不同時，先把邊界轉到原始檔的座標系 (TWD67 用七參數) 再畫；
   畫面範圍同時涵蓋原始檔與邊界，兩者沒有重疊時也看得出相對位置。
2. 瀏覽器地圖 build_clip_preview_map：國土測繪中心底圖＋可勾選的「原始」「裁切後」「裁切邊界」三層。
   網格先縮圖、再以 gdalwarp 轉成網頁地圖座標系 (EPSG:3857) 疊上；圖徵很多的向量只放邊界附近並抽稀。

預覽只供目視確認位置，數值以成果檔為準。osgeo 只用來讀網格陣列與中繼資料，運算都經 CLI。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image, ImageDraw

from core import geo_io
from core.crs_doctor import decide_raster_source_srs, decide_vector_source_srs
from core.env_runner import ToolkitError, run_command, run_json_command
from core.preview_engine import (
    _extract_boundary_coordinates,
    _iter_geometry_positions,
    add_map_tools,
    area_tooltip,
    new_nlsc_map,
    srs_args,
    thin_and_reproject_vector_for_preview,
    vector_style_function,
)
from core.lazy_import import LazyModule

# folium 第一次畫瀏覽器地圖時才載入 (core/lazy_import.py)
folium = LazyModule("folium")

logger = logging.getLogger("core.clip_preview")

TextCallback = Optional[Callable[[str], None]]
# (min_x, min_y, max_x, max_y)
Extent = Tuple[float, float, float, float]
Ring = List[Tuple[float, float]]

# 預覽的畫法：DEM 山體陰影、影像直接縮圖、向量外框
STYLE_DEM: str = "dem"
STYLE_IMAGE: str = "image"
STYLE_VECTOR: str = "vector"
PREVIEW_STYLES: Tuple[str, ...] = (STYLE_DEM, STYLE_IMAGE, STYLE_VECTOR)

# 右欄縮圖與瀏覽器網格圖層的長邊像素
THUMBNAIL_MAX_SIZE: int = 1024
WEB_LAYER_MAX_SIZE: int = 1536
# 畫面範圍往外留白的比例 (邊界貼邊時紅框才看得到)
_VIEW_MARGIN_RATIO: float = 0.04
# 縮圖最短邊至少幾個像素 (細長範圍才不會只剩一條線)
_MIN_CANVAS_SIDE: int = 16

_CANVAS_COLOR: Tuple[int, int, int, int] = (226, 226, 226, 255)
# 純紅：彩色高程的色階最紅只到 (165, 0, 38)，邊界不會和地形顏色混在一起
_BOUNDARY_COLOR: Tuple[int, int, int, int] = (255, 0, 0, 255)
_BOUNDARY_FILL: Tuple[int, int, int, int] = (255, 0, 0, 50)
_BOUNDARY_WIDTH: int = 3
_VECTOR_EDGE_COLOR: Tuple[int, int, int, int] = (25, 70, 190, 255)
_VECTOR_FILL_COLOR: Tuple[int, int, int, int] = (150, 185, 240, 255)

# 縮小倍率超過這個值時改用最近鄰讀取 (只讀需要的列，大檔快很多)
_NEAREST_SHRINK_RATIO: float = 4.0
# 非 8 位元影像拉伸時取的百分位數
_STRETCH_PERCENTILES: Tuple[float, float] = (2.0, 98.0)
# 山體陰影只套用這個比例的明暗 (純相乘太暗)
_HILLSHADE_STRENGTH: float = 0.6
_HILLSHADE_Z_FACTOR: float = 1.5
# 彩色高程的色階 (0 = 最低、1 = 最高)
_DEM_COLOR_STOPS: Tuple[Tuple[float, Tuple[int, int, int]], ...] = (
    (0.0, (46, 139, 87)),
    (0.25, (166, 217, 106)),
    (0.5, (254, 224, 139)),
    (0.75, (244, 109, 67)),
    (1.0, (165, 0, 38)),
)

# 圖徵超過這個數量的向量，瀏覽器「原始」層只放邊界附近
NEAR_BOUNDARY_FEATURE_LIMIT: int = 5000
# 「邊界附近」= 邊界範圍往外擴 (邊界寬高較大者 × 這個比例)
NEAR_BOUNDARY_MARGIN_RATIO: float = 0.5
# 瀏覽器地圖的抽稀容許 (公尺)：原始與裁切後的向量、裁切邊界
_WEB_SIMPLIFY_M: float = 1.5
_WEB_BOUNDARY_SIMPLIFY_M: float = 0.0
_WEB_MERCATOR_SRS: str = "EPSG:3857"
_WEB_MERCATOR_RADIUS: float = 6378137.0

# 瀏覽器圖層的名稱前綴
MAP_LAYER_SOURCE: str = "原始"
MAP_LAYER_CLIPPED: str = "裁切後"
MAP_LAYER_BOUNDARY: str = "裁切邊界"


@dataclass(frozen=True)
class _Georef:
    """北朝上的網格地理參考：左上角 (x0, y0) 與像元大小 (dx > 0, dy < 0)。"""

    x0: float
    y0: float
    dx: float
    dy: float

    @classmethod
    def for_extent(cls, extent: Extent, width: int, height: int) -> "_Georef":
        min_x, min_y, max_x, max_y = extent
        return cls(min_x, max_y, (max_x - min_x) / width, (min_y - max_y) / height)

    def pixel(self, x: float, y: float) -> Tuple[float, float]:
        return (x - self.x0) / self.dx, (y - self.y0) / self.dy

    def extent(self, width: int, height: int) -> Extent:
        return (self.x0, self.y0 + self.dy * height, self.x0 + self.dx * width, self.y0)


@dataclass
class _RasterPicture:
    """網格縮圖 (RGBA) 與它的地理參考；DEM 另記色階用的高程範圍。"""

    image: Image.Image
    georef: _Georef
    value_range: Optional[Tuple[float, float]] = None

    @property
    def extent(self) -> Extent:
        return self.georef.extent(*self.image.size)


@dataclass
class ClipPreviewMap:
    """瀏覽器預覽地圖的成果：HTML 與實際放上去的圖層名稱 (順序同圖層開關)。"""

    html_path: Path
    layer_names: List[str] = field(default_factory=list)


# ============================================================================
# 座標系與範圍
# ============================================================================

def source_srs_for_drawing(source: Path, style: str, assigned_srs: Optional[str] = None) -> Optional[str]:
    """原始檔座標的實際座標系 (EPSG／PROJ 字串或 WKT)，把邊界轉過來畫用；沒有標籤又判斷不出來時回傳 None。

    TWD67 標籤或指派 TWD67 時回傳七參數定義，轉換結果與裁切時一致。
    """
    try:
        if style == STYLE_VECTOR:
            decision = decide_vector_source_srs(source, assigned_srs)
        else:
            decision = decide_raster_source_srs(source, assigned_srs)
    except ToolkitError as err:
        logger.debug("判斷 %s 的座標系略過: %s", source.name, err)
        return None
    if decision.s_srs:
        return decision.s_srs
    if style == STYLE_VECTOR:
        return geo_io.read_layer_info(source).srs_wkt
    crs = run_json_command(["gdalinfo", "-json", str(source)]).get("coordinateSystem")
    return crs.get("wkt") if isinstance(crs, dict) else None


def _boundary_rings(boundary: Path, target_srs: Optional[str]) -> List[Ring]:
    """邊界的多邊形環 (轉到 target_srs；None 或轉不過去時用邊界自己的座標)。"""
    return _extract_boundary_coordinates(boundary, target_srs)


def _rings_extent(rings: Sequence[Ring]) -> Optional[Extent]:
    xs = [x for ring in rings for x, _ in ring]
    ys = [y for ring in rings for _, y in ring]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _union_extent(extents: Sequence[Optional[Extent]]) -> Optional[Extent]:
    found = [e for e in extents if e is not None and all(math.isfinite(v) for v in e)]
    if not found:
        return None
    return (
        min(e[0] for e in found),
        min(e[1] for e in found),
        max(e[2] for e in found),
        max(e[3] for e in found),
    )


def _expand_extent(extent: Extent, ratio: float) -> Extent:
    """四邊各往外擴 (寬高較大者 × ratio)；範圍是一個點或一條線時也擴成有面積。"""
    min_x, min_y, max_x, max_y = extent
    span = max(max_x - min_x, max_y - min_y)
    margin = span * ratio if span > 0 else 1.0
    return (min_x - margin, min_y - margin, max_x + margin, max_y + margin)


def _canvas_size(extent: Extent, max_size: int) -> Tuple[int, int]:
    width = extent[2] - extent[0]
    height = extent[3] - extent[1]
    scale = max_size / max(width, height)
    return (
        max(_MIN_CANVAS_SIDE, int(round(width * scale))),
        max(_MIN_CANVAS_SIDE, int(round(height * scale))),
    )


def _shrunk_size(width: int, height: int, max_size: int) -> Tuple[int, int]:
    ratio = max(width, height) / float(max_size)
    if ratio <= 1.0:
        return width, height
    return max(1, int(round(width / ratio))), max(1, int(round(height / ratio)))


# ============================================================================
# 網格縮圖
# ============================================================================

def _open_raster(path: Path) -> Any:
    from osgeo import gdal

    geo_io.ensure_gdal_ready()
    dataset = gdal.Open(str(path))
    if dataset is None:
        raise ToolkitError(f"無法開啟網格: {path.name}")
    return dataset


def _georef_of(dataset: Any, path: Path) -> _Georef:
    transform = dataset.GetGeoTransform(can_return_null=True)
    if transform is None:
        raise ToolkitError(f"{path.name} 沒有地理參考 (不知道放在哪裡)，無法預覽位置。")
    # 有旋轉的網格極少見，預覽只當北朝上畫
    return _Georef(transform[0], transform[3], transform[1], transform[5])


def _read_all_bands(path: Path) -> np.ndarray:
    """讀出全部波段，形狀一律是 (波段, 列, 行)。"""
    dataset = _open_raster(path)
    try:
        values = dataset.ReadAsArray()
    finally:
        dataset = None
    return values[np.newaxis, ...] if values.ndim == 2 else values


def _dem_ramp_text(low: float, high: float) -> str:
    """gdaldem color-relief 的色階檔內容：把色階拉開在 low～high 之間，NoData 透明。"""
    if not high > low:
        low, high = low - 1.0, high + 1.0
    lines = ["nv 0 0 0 0"]
    for position, (red, green, blue) in _DEM_COLOR_STOPS:
        lines.append(f"{low + (high - low) * position!r} {red} {green} {blue}")
    return "\n".join(lines) + "\n"


def _render_dem(
    path: Path,
    max_size: int,
    temp_files: geo_io.TempVectorFiles,
    value_range: Optional[Tuple[float, float]] = None,
) -> _RasterPicture:
    """DEM 縮圖：先縮小，再以 gdaldem 做山體陰影與彩色高程後混色；value_range 為 None 時用這幅 DEM 的高低。"""
    dataset = _open_raster(path)
    try:
        width, height = dataset.RasterXSize, dataset.RasterYSize
        georef = _georef_of(dataset, path)
    finally:
        dataset = None

    out_w, out_h = _shrunk_size(width, height, max_size)
    small = path
    if (out_w, out_h) != (width, height):
        small = temp_files.new("dem_small", suffix=".tif")
        resampling = "nearest" if width / out_w > _NEAREST_SHRINK_RATIO else "bilinear"
        run_command(
            ["gdal_translate", "-of", "GTiff", "-outsize", str(out_w), str(out_h), "-r", resampling, str(path), str(small)],
            check=True,
        )
        georef = _Georef(georef.x0, georef.y0, georef.dx * width / out_w, georef.dy * height / out_h)

    if value_range is None:
        bands = run_json_command(["gdalinfo", "-json", "-mm", str(small)]).get("bands") or [{}]
        low, high = bands[0].get("computedMin"), bands[0].get("computedMax")
        if low is None or high is None:
            raise ToolkitError(f"{path.name} 沒有有效的高程值，無法預覽。")
        value_range = (float(low), float(high))

    ramp = temp_files.new("ramp", suffix=".txt")
    ramp.write_text(_dem_ramp_text(*value_range), encoding="utf-8")
    shade_tif = temp_files.new("shade", suffix=".tif")
    color_tif = temp_files.new("color", suffix=".tif")
    run_command(
        ["gdaldem", "hillshade", str(small), str(shade_tif), "-z", str(_HILLSHADE_Z_FACTOR), "-compute_edges"],
        check=True,
    )
    run_command(["gdaldem", "color-relief", str(small), str(ramp), str(color_tif), "-alpha"], check=True)

    color = _read_all_bands(color_tif)
    shade = _read_all_bands(shade_tif)[0].astype(np.float32) / 255.0
    factor = (1.0 - _HILLSHADE_STRENGTH) + _HILLSHADE_STRENGTH * shade
    rgb = np.clip(color[:3].astype(np.float32) * factor, 0, 255).astype(np.uint8)
    rgba = np.dstack([rgb[0], rgb[1], rgb[2], color[3].astype(np.uint8)])
    return _RasterPicture(Image.fromarray(rgba, "RGBA"), georef, value_range)


def _stretch_to_byte(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """非 8 位元的波段依有效像元的 2%～98% 拉伸成 0～255；本來就是 8 位元的不動。"""
    if values.dtype == np.uint8:
        return values
    data = values.astype(np.float64)
    sample = data[valid & np.isfinite(data)]
    if sample.size == 0:
        return np.zeros(data.shape, dtype=np.uint8)
    low, high = np.percentile(sample, _STRETCH_PERCENTILES)
    if not high > low:
        high = low + 1.0
    return np.clip((np.nan_to_num(data, nan=low) - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)


def _render_image(path: Path, max_size: int) -> _RasterPicture:
    """正射影像縮圖：縮小讀取顏色波段與遮罩 (NoData／Alpha 透明)；色盤影像依色盤上色。"""
    from osgeo import gdal

    dataset = _open_raster(path)
    try:
        width, height = dataset.RasterXSize, dataset.RasterYSize
        georef = _georef_of(dataset, path)
        out_w, out_h = _shrunk_size(width, height, max_size)
        resample = gdal.GRIORA_NearestNeighbour if width / out_w > _NEAREST_SHRINK_RATIO else gdal.GRIORA_Bilinear

        def _read(band: Any) -> np.ndarray:
            return band.ReadAsArray(0, 0, width, height, buf_xsize=out_w, buf_ysize=out_h, resample_alg=resample)

        bands = [dataset.GetRasterBand(i + 1) for i in range(dataset.RasterCount)]
        color_bands = [b for b in bands if b.GetColorInterpretation() != gdal.GCI_AlphaBand] or bands[:1]
        alpha = _read(color_bands[0].GetMaskBand()).astype(np.uint8)
        valid = alpha > 0
        table = color_bands[0].GetColorTable()
        if len(color_bands) < 3 and table is not None:
            entries = [table.GetColorEntry(i)[:3] for i in range(table.GetCount())] or [(0, 0, 0)]
            lut = np.array(entries, dtype=np.uint8)
            index = np.clip(_read(color_bands[0]).astype(np.int64), 0, len(lut) - 1)
            rgb = lut[index]
            channels = [rgb[..., 0], rgb[..., 1], rgb[..., 2]]
        else:
            picked = color_bands[:3] if len(color_bands) >= 3 else color_bands[:1] * 3
            channels = [_stretch_to_byte(_read(band), valid) for band in picked]
    finally:
        dataset = None
    rgba = np.dstack([*channels, alpha])
    georef = _Georef(georef.x0, georef.y0, georef.dx * width / out_w, georef.dy * height / out_h)
    return _RasterPicture(Image.fromarray(rgba.astype(np.uint8), "RGBA"), georef)


def _render_raster(
    path: Path,
    style: str,
    max_size: int,
    temp_files: geo_io.TempVectorFiles,
    value_range: Optional[Tuple[float, float]] = None,
) -> _RasterPicture:
    if style == STYLE_DEM:
        return _render_dem(path, max_size, temp_files, value_range)
    return _render_image(path, max_size)


# ============================================================================
# 向量縮圖
# ============================================================================

def _render_vector(
    path: Path, extent: Extent, width: int, height: int, temp_files: geo_io.TempVectorFiles, layer_name: Optional[str] = None
) -> Image.Image:
    """把向量的每個圖徵以自己的編號燒成網格 (gdal_rasterize)，再畫出淡色填色與每個圖徵的外框。
    layer_name：檔案裡的哪一個圖層；None 時取第一個有圖徵的圖層。"""
    info = geo_io.read_layer_info(path, layer_name)
    ids_tif = temp_files.new("ids", suffix=".tif")
    sql = f"SELECT FID AS pv_fid FROM {geo_io.quote_ident(info.layer_name)}"
    run_command(
        [
            "gdal_rasterize",
            "-dialect", "OGRSQL", "-sql", sql, "-a", "pv_fid",
            "-ot", "Int32", "-init", "-1", "-at",
            "-te", *(repr(float(v)) for v in extent),
            "-ts", str(width), str(height),
            "-of", "GTiff",
            str(path), str(ids_tif),
        ],
        check=True,
    )
    ids = _read_all_bands(ids_tif)[0]
    filled = ids >= 0
    # 與上下左右任一格編號不同 (含空白) 的有圖徵格子就是外框
    padded = np.pad(ids, 1, constant_values=-1)
    edge = np.zeros(ids.shape, dtype=bool)
    for row, col in ((0, 1), (2, 1), (1, 0), (1, 2)):
        edge |= padded[row:row + ids.shape[0], col:col + ids.shape[1]] != ids
    edge &= filled

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[filled] = _VECTOR_FILL_COLOR
    rgba[edge] = _VECTOR_EDGE_COLOR
    return Image.fromarray(rgba, "RGBA")


# ============================================================================
# 右欄縮圖
# ============================================================================

def _paste_picture(canvas: Image.Image, georef: _Georef, picture: _RasterPicture) -> None:
    """把網格縮圖依實際範圍縮放後貼到畫布上 (透明處露出底色)。"""
    min_x, min_y, max_x, max_y = picture.extent
    left, top = georef.pixel(min_x, max_y)
    right, bottom = georef.pixel(max_x, min_y)
    size = (max(1, int(round(right - left))), max(1, int(round(bottom - top))))
    resized = picture.image.resize(size, Image.BILINEAR)
    canvas.paste(resized, (int(round(left)), int(round(top))), resized)


def _draw_rings(canvas: Image.Image, georef: _Georef, rings: Sequence[Ring], *, fill: bool) -> None:
    """畫裁切邊界紅框 (沒有原始檔時另加淡紅填色)。"""
    if not rings:
        return
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for ring in rings:
        pixels = [georef.pixel(x, y) for x, y in ring]
        if len(pixels) < 2:
            continue
        if fill and len(pixels) >= 3:
            draw.polygon(pixels, fill=_BOUNDARY_FILL)
        draw.line(pixels + pixels[:1], fill=_BOUNDARY_COLOR, width=_BOUNDARY_WIDTH, joint="curve")
    canvas.alpha_composite(overlay)


def render_clip_thumbnail(
    output_png: Union[str, Path],
    *,
    style: str,
    source: Optional[Union[str, Path]] = None,
    boundary: Optional[Union[str, Path]] = None,
    assigned_srs: Optional[str] = None,
    max_size: int = THUMBNAIL_MAX_SIZE,
    layers: Sequence[str] = (),
) -> Path:
    """右欄縮圖：原始檔 (可省略) 加上裁切邊界紅框 (可省略)，畫在同一個座標系的實際位置。

    style：STYLE_DEM／STYLE_IMAGE／STYLE_VECTOR，決定原始檔的畫法。
    assigned_srs：原始檔沒有座標系標籤時的來源座標系 (與裁切時相同)。
    layers：向量檔裡要畫的圖層 (KML 的資料夾等)，全部畫在一起；空的表示第一個有圖徵的圖層。
    """
    if style not in PREVIEW_STYLES:
        raise ValueError(f"不支援的預覽畫法: {style}")
    if source is None and boundary is None:
        raise ValueError("沒有原始檔也沒有裁切邊界，無法產生預覽。")
    out = Path(output_png).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    source_p = Path(source).resolve() if source is not None else None

    with geo_io.TempVectorFiles("thumb") as temp_files:
        drawing_srs = source_srs_for_drawing(source_p, style, assigned_srs) if source_p is not None else None
        rings = _boundary_rings(Path(boundary).resolve(), drawing_srs) if boundary is not None else []

        picture: Optional[_RasterPicture] = None
        source_extent: Optional[Extent] = None
        if source_p is not None and style != STYLE_VECTOR:
            picture = _render_raster(source_p, style, max_size, temp_files)
            source_extent = picture.extent
        elif source_p is not None:
            source_extent = _union_extent([geo_io.read_layer_info(source_p, name).extent for name in layers or (None,)])

        view = _union_extent([source_extent, _rings_extent(rings)])
        if view is None:
            raise ToolkitError("原始檔與裁切邊界都讀不到範圍，無法產生預覽。")
        view = _expand_extent(view, _VIEW_MARGIN_RATIO)
        width, height = _canvas_size(view, max_size)
        georef = _Georef.for_extent(view, width, height)

        canvas = Image.new("RGBA", (width, height), _CANVAS_COLOR)
        if picture is not None:
            _paste_picture(canvas, georef, picture)
        elif source_p is not None:
            for name in layers or (None,):
                canvas.alpha_composite(_render_vector(source_p, view, width, height, temp_files, name))
        _draw_rings(canvas, georef, rings, fill=source_p is None)

    canvas.convert("RGB").save(out, format="PNG")
    return out


# ============================================================================
# 瀏覽器地圖
# ============================================================================

def _mercator_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    lon = math.degrees(x / _WEB_MERCATOR_RADIUS)
    lat = math.degrees(2.0 * math.atan(math.exp(y / _WEB_MERCATOR_RADIUS)) - math.pi / 2.0)
    return lon, lat


def _picture_to_web_mercator(
    picture: _RasterPicture, srs: str, tag: str, temp_files: geo_io.TempVectorFiles
) -> Tuple[Image.Image, Extent]:
    """把網格縮圖 (PNG＋世界檔) 以 gdalwarp 轉成 EPSG:3857，回傳 (影像, 經緯度範圍)。"""
    png = temp_files.new(tag, suffix=".png")
    world = temp_files.new(tag, suffix=".pgw")
    picture.image.save(png, format="PNG")
    g = picture.georef
    # 世界檔記的是左上角像元的中心
    world.write_text(f"{g.dx!r}\n0\n0\n{g.dy!r}\n{g.x0 + g.dx / 2.0!r}\n{g.y0 + g.dy / 2.0!r}\n", encoding="ascii")
    warped = temp_files.new(f"{tag}_3857", suffix=".tif")
    run_command(
        [
            "gdalwarp", "-overwrite",
            *srs_args("-s_srs", srs, temp_files),
            "-t_srs", _WEB_MERCATOR_SRS,
            "-r", "bilinear",
            "-of", "GTiff",
            "--config", "GDAL_PAM_ENABLED", "NO",
            str(png), str(warped),
        ],
        check=True,
    )
    dataset = _open_raster(warped)
    try:
        width, height = dataset.RasterXSize, dataset.RasterYSize
        x0, dx, _, y0, _, dy = dataset.GetGeoTransform()
        bands = dataset.ReadAsArray()
    finally:
        dataset = None
    image = Image.fromarray(np.dstack(list(bands[:4])).astype(np.uint8), "RGBA")
    west, north = _mercator_to_lonlat(x0, y0)
    east, south = _mercator_to_lonlat(x0 + dx * width, y0 + dy * height)
    return image, (west, south, east, north)


def _png_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _add_raster_layer(
    fmap: folium.Map,
    path: Path,
    title: str,
    style: str,
    tag: str,
    assigned_srs: Optional[str],
    temp_files: geo_io.TempVectorFiles,
    *,
    opacity: float,
    value_range: Optional[Tuple[float, float]] = None,
) -> Tuple[Extent, Optional[Tuple[float, float]]]:
    """網格縮圖轉成網頁座標後以 ImageOverlay 疊上，回傳 (經緯度範圍, DEM 色階範圍)。"""
    srs = source_srs_for_drawing(path, style, assigned_srs)
    if srs is None:
        raise ToolkitError(f"{path.name} 沒有座標系標籤也判斷不出來，無法疊到地圖上 (請在最上方指定來源座標系)")
    picture = _render_raster(path, style, WEB_LAYER_MAX_SIZE, temp_files, value_range)
    image, (west, south, east, north) = _picture_to_web_mercator(picture, srs, tag, temp_files)
    folium.raster_layers.ImageOverlay(
        image=_png_data_url(image),
        bounds=[[south, west], [north, east]],
        name=title,
        opacity=opacity,
        interactive=False,
    ).add_to(fmap)
    return (west, south, east, north), picture.value_range


def _add_vector_layer(
    fmap: folium.Map,
    path: Path,
    title: str,
    tag: str,
    assigned_srs: Optional[str],
    temp_files: geo_io.TempVectorFiles,
    *,
    colors: Tuple[str, str],
    fill_opacity: float,
    simplify: float,
    spatial_filter: Optional[Extent] = None,
    text_callback: TextCallback = None,
) -> Extent:
    """向量抽稀後轉成經緯度 GeoJSON 疊上 (有面積欄位時滑鼠移上去會顯示)，回傳經緯度範圍。"""
    geojson_path = temp_files.new(tag, suffix=".geojson")
    thin_and_reproject_vector_for_preview(
        path,
        geojson_path,
        simplify_tolerance=simplify,
        assigned_srs=assigned_srs,
        spatial_filter=spatial_filter,
        text_callback=text_callback,
    )
    # 中文欄位名被別的軟體按位元組截成 10 bytes (切在半個字上) 時 ogr2ogr 照抄成不合法的 UTF-8：壞掉的字元換成 �
    with open(geojson_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    features = [feat for feat in data.get("features") or [] if feat.get("geometry")]
    if not features:
        where = "邊界附近" if spatial_filter else ""
        raise ToolkitError(f"{path.name} {where}沒有圖徵")
    # 每個圖徵給自己的 id：沒有 id 時 folium 會挑一個值都不重複的欄位寫進 JS，欄位名有空白或括號時整張地圖會壞掉
    for number, feat in enumerate(features):
        feat["id"] = str(number)
    data["features"] = features
    positions = [pos for feat in features for pos in _iter_geometry_positions(feat["geometry"])]
    folium.GeoJson(
        data,
        name=title,
        style_function=vector_style_function(*colors, fill_opacity=fill_opacity),
        tooltip=area_tooltip(features),
    ).add_to(fmap)
    lons = [lon for lon, _ in positions]
    lats = [lat for _, lat in positions]
    return (min(lons), min(lats), max(lons), max(lats))


def _near_boundary_filter(
    source: Path, boundary: Path, assigned_srs: Optional[str], text_callback: TextCallback
) -> Optional[Extent]:
    """圖徵很多的向量：回傳邊界附近的範圍 (原始檔的座標)，地圖只放這裡面的圖徵；圖徵不多時回傳 None。"""
    info = geo_io.read_layer_info(source)
    if info.feature_count <= NEAR_BOUNDARY_FEATURE_LIMIT:
        return None
    rings = _boundary_rings(boundary, source_srs_for_drawing(source, STYLE_VECTOR, assigned_srs))
    ring_extent = _rings_extent(rings)
    if ring_extent is None:
        return None
    if text_callback:
        text_callback(
            f"[地圖預覽] {source.name} 有 {info.feature_count:,} 個圖徵，「{MAP_LAYER_SOURCE}」層只放邊界附近的圖徵並抽稀。\n"
        )
    return _expand_extent(ring_extent, NEAR_BOUNDARY_MARGIN_RATIO)


def build_clip_preview_map(
    output_html: Union[str, Path],
    *,
    style: str,
    source: Optional[Union[str, Path]] = None,
    boundary: Optional[Union[str, Path]] = None,
    clipped: Optional[Union[str, Path]] = None,
    assigned_srs: Optional[str] = None,
    text_callback: TextCallback = None,
) -> ClipPreviewMap:
    """產生瀏覽器預覽地圖：國土測繪中心底圖＋「原始」「裁切後」「裁切邊界」三層 (有的才放，都可勾選開關)。

    某一層做不出來 (例如座標系判斷不出來) 只略過那一層並記在日誌；三層都沒有時拋 ToolkitError。
    """
    if style not in PREVIEW_STYLES:
        raise ValueError(f"不支援的預覽畫法: {style}")
    out = Path(output_html).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    source_p = Path(source).resolve() if source is not None else None
    boundary_p = Path(boundary).resolve() if boundary is not None else None
    clipped_p = Path(clipped).resolve() if clipped is not None else None

    fmap = new_nlsc_map()
    names: List[str] = []
    extents: List[Optional[Extent]] = []

    def _try(title: str, add: Callable[[], Any]) -> Any:
        try:
            result = add()
        except Exception as err:
            logger.warning("地圖圖層「%s」略過: %s", title, err)
            if text_callback:
                text_callback(f"[地圖預覽] 「{title}」層略過：{err}\n")
            return None
        names.append(title)
        return result

    with geo_io.TempVectorFiles("clipmap") as temp_files:
        value_range: Optional[Tuple[float, float]] = None
        if source_p is not None:
            title = f"{MAP_LAYER_SOURCE}：{source_p.name}"
            if style == STYLE_VECTOR:
                near = (
                    _near_boundary_filter(source_p, boundary_p, assigned_srs, text_callback)
                    if boundary_p is not None
                    else None
                )
                extents.append(
                    _try(
                        title,
                        lambda: _add_vector_layer(
                            fmap, source_p, title, "source", assigned_srs, temp_files,
                            colors=("#1E6FFF", "#7FB8FF"), fill_opacity=0.15, simplify=_WEB_SIMPLIFY_M,
                            spatial_filter=near, text_callback=text_callback,
                        ),
                    )
                )
            else:
                added = _try(
                    title,
                    lambda: _add_raster_layer(
                        fmap, source_p, title, style, "source", assigned_srs, temp_files, opacity=0.75
                    ),
                )
                if added is not None:
                    extents.append(added[0])
                    value_range = added[1]

        if clipped_p is not None:
            title = f"{MAP_LAYER_CLIPPED}：{clipped_p.name}"
            if style == STYLE_VECTOR:
                extents.append(
                    _try(
                        title,
                        lambda: _add_vector_layer(
                            fmap, clipped_p, title, "clipped", None, temp_files,
                            colors=("#FF8C00", "#FFD700"), fill_opacity=0.4, simplify=_WEB_SIMPLIFY_M,
                        ),
                    )
                )
            else:
                added = _try(
                    title,
                    lambda: _add_raster_layer(
                        fmap, clipped_p, title, style, "clipped", None, temp_files,
                        opacity=1.0, value_range=value_range,
                    ),
                )
                extents.append(added[0] if added is not None else None)

        if boundary_p is not None:
            extents.append(
                _try(
                    MAP_LAYER_BOUNDARY,
                    lambda: _add_vector_layer(
                        fmap, boundary_p, MAP_LAYER_BOUNDARY, "boundary", assigned_srs, temp_files,
                        colors=("#FF2222", "#FF2222"), fill_opacity=0.0, simplify=_WEB_BOUNDARY_SIMPLIFY_M,
                    ),
                )
            )

    if not names:
        raise ToolkitError("原始檔、裁切成果與裁切邊界都無法放上地圖，詳見日誌。")

    bounds = _union_extent(extents)
    if bounds is not None:
        west, south, east, north = bounds
        fmap.fit_bounds([[south, west], [north, east]], max_zoom=18)
    add_map_tools(fmap)
    fmap.save(str(out))
    if text_callback:
        text_callback(f"[地圖預覽] 已產生瀏覽器地圖 (可勾選：{'、'.join(names)})。\n")
    return ClipPreviewMap(html_path=out, layer_names=names)
