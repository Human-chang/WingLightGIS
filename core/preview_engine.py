"""雙軌輕量成果預覽與 3D 陰影渲染引擎 (core/preview_engine.py)

嚴格遵守專案規格書第 0 節與第 3.5 節鐵律：
1. 嚴禁使用 geopandas, rasterio, shapely, pyproj, arcpy。
2. 允許使用：Pillow (混色合成)、folium (網頁預覽)、numpy。
3. [G-16] 3D 山體陰影混色合成 (gdaldem)：
   - 調用 gdaldem hillshade 與 color-relief 產生陰影與色階圖。
   - 使用 Pillow 以色彩相乘 (Multiply) 混色合成 0.5 秒極速 3D 立體地形縮圖。
   - 自動將工程裁切邊界以鮮紅外框疊加於 3D 畫布。
4. [G-17] Folium 雙軌抽稀網頁預覽 + 國土測繪圖資服務雲 WMTS：
   - 雙軌防崩潰鐵律：絕對不將數百 MB 原始 SHP 直接塞入瀏覽器，
     強制以 ogr2ogr 抽稀 (-simplify 1.5) 並轉為 EPSG:4326 GeoJSON 替身檔。
   - 內建內政部國土測繪中心 (NLSC) 4 大標準 WMTS 圖磚服務：
     正射影像 (航照)、通用電子地圖、透明道路疊加層、地籍圖參考層；另有空白 (白底) 底圖選項。
   - 整合即時量測工具 (MeasureControl) 與游標座標 (MousePosition)。
   - 支援一鍵導出 Google Earth .kmz 檔案。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# 允許使用之第三方套件
from PIL import Image, ImageChops, ImageDraw

from core import geo_io
from core.crs_doctor import EPSG_TWD97_121, EPSG_WGS84, decide_and_report_source_srs
from core.env_runner import (
    BinaryNotFoundError,
    CommandResult,
    ToolkitError,
    get_config_dir,
    get_workspace_temp_dir,
    run_command,
    run_json_command,
)
from core.lazy_import import LazyModule

# folium 載入約 0.43 秒，只有畫瀏覽器地圖時用到：第一次用到才載入 (core/lazy_import.py)
folium = LazyModule("folium")
plugins = LazyModule("folium.plugins")

logger = logging.getLogger("core.preview_engine")

# ============================================================================
# 內政部國土測繪中心 (NLSC) 標準 WMTS 圖磚服務網址常數
# ============================================================================

NLSC_WMTS_PHOTO2: str = (
    "https://wmts.nlsc.gov.tw/wmts/PHOTO2/default/GoogleMapsCompatible/{z}/{y}/{x}"
)
NLSC_WMTS_EMAP: str = (
    "https://wmts.nlsc.gov.tw/wmts/EMAP/default/GoogleMapsCompatible/{z}/{y}/{x}"
)
NLSC_WMTS_EMAP2: str = (
    "https://wmts.nlsc.gov.tw/wmts/EMAP2/default/GoogleMapsCompatible/{z}/{y}/{x}"
)
NLSC_WMTS_LAND_OPENDATA: str = (
    "https://wmts.nlsc.gov.tw/wmts/LAND_OPENDATA/default/GoogleMapsCompatible/{z}/{y}/{x}"
)
# 底圖選項的最後一個：沒有圖磚的白底 (簡易出圖用)
BLANK_BASEMAP_NAME: str = "空白 (白底)"
BLANK_BASEMAP_COLOR: str = "#FFFFFF"

# 臺灣高程標準彩色渲染色階表內容 (公尺 -> R G B)
DEFAULT_TERRAIN_RAMP_TEXT: str = """# 臺灣通用地形高程著色階梯表
-9999 0 0 0 0
0 46 139 87
100 102 189 99
300 166 217 106
600 217 239 139
1000 254 224 139
1500 253 174 97
2000 244 109 67
2500 215 48 39
3000 165 0 38
3500 180 180 180
3952 255 255 255
nv 0 0 0 0
"""


@dataclass
class Preview3DResult:
    """3D 山體陰影合成預覽成果。"""

    preview_image_path: Path
    hillshade_path: Path
    color_relief_path: Path
    image_width: int
    image_height: int
    duration_seconds: float


@dataclass
class FoliumPreviewResult:
    """Folium 網頁地圖預覽成果。"""

    html_path: Path
    thinned_geojson_path: Path
    center_lat: float
    center_lon: float
    feature_count: int
    thinned_geojson_paths: List[Path] = field(default_factory=list)


# ============================================================================
# [G-16] 3D 山體陰影色階混色合成 (gdaldem + Pillow)
# ============================================================================

def ensure_terrain_ramp_file() -> Path:
    """確保 config/terrain_ramp.txt 色階定義檔存在，若無則自動建立預設色表。"""
    ramp_path = get_config_dir() / "terrain_ramp.txt"

    if not ramp_path.exists():
        with open(ramp_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TERRAIN_RAMP_TEXT)
        logger.info("已自動生成預設高程色彩色階表: %s", ramp_path)

    return ramp_path


def _iter_positions(coordinates: Any) -> Any:
    """走訪 GeoJSON coordinates 的巢狀陣列，逐一產出 (x, y)。"""
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        return
    if isinstance(coordinates[0], (int, float)):
        if len(coordinates) >= 2:
            yield (float(coordinates[0]), float(coordinates[1]))
        return
    for item in coordinates:
        yield from _iter_positions(item)


def _iter_geometry_positions(geometry: Optional[Dict[str, Any]]) -> Any:
    """產出一個 GeoJSON 幾何 (含 GeometryCollection) 的所有頂點座標。"""
    if not geometry:
        return
    if geometry.get("type") == "GeometryCollection":
        for member in geometry.get("geometries") or []:
            yield from _iter_geometry_positions(member)
        return
    yield from _iter_positions(geometry.get("coordinates"))


def srs_args(flag: str, srs: str, temp_files: geo_io.TempVectorFiles) -> List[str]:
    """回傳 GDAL 的座標系參數 (例如 -t_srs)；WKT 又長又含引號，寫成 .prj 暫存檔再把檔名交給 GDAL。"""
    if "[" in srs:
        prj = temp_files.new(f"srs{flag}", suffix=".prj")
        prj.write_text(srs, encoding="utf-8")
        return [flag, str(prj)]
    return [flag, srs]


def _extract_boundary_coordinates(
    boundary_file: Path,
    target_srs_wkt: Optional[str] = None,
) -> List[List[Tuple[float, float]]]:
    """萃取邊界檔內多邊形環的頂點座標；有給 target_srs_wkt 時先重投影到該座標系 (WKT、EPSG 或 PROJ 字串皆可)。

    邊界沒有座標系標籤時無法重投影，退回直接使用原座標。
    """
    rings: List[List[Tuple[float, float]]] = []

    with geo_io.TempVectorFiles("preview3d_boundary") as temp_files:
        temp_json = temp_files.new("rings", suffix=".geojson")
        attempts: List[List[str]] = []
        if target_srs_wkt:
            attempts.append(srs_args("-t_srs", target_srs_wkt, temp_files))
        attempts.append([])

        data: Optional[Dict[str, Any]] = None
        for attempt_args in attempts:
            geo_io.remove_vector_file(temp_json)
            result = run_command(["ogr2ogr", "-f", "GeoJSON", *attempt_args, str(temp_json), str(boundary_file)], check=False)
            if result.returncode == 0 and temp_json.is_file():
                with open(temp_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                break

        if data is None:
            logger.warning("讀不到邊界檔 %s 的幾何，預覽圖不畫外框。", boundary_file.name)
            return rings

    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        g_type = geom.get("type", "")
        coords = geom.get("coordinates", [])
        polygons = [coords] if g_type == "Polygon" else coords if g_type == "MultiPolygon" else []
        for poly in polygons:
            for r in poly:
                ring_pts = [(float(pt[0]), float(pt[1])) for pt in r]
                if ring_pts:
                    rings.append(ring_pts)

    return rings


def generate_3d_hillshade_preview(
    dem_path: Union[str, Path],
    *,
    boundary_path: Optional[Union[str, Path]] = None,
    output_preview_path: Optional[Union[str, Path]] = None,
    z_factor: float = 1.5,
    draw_boundary_outline: bool = True,
    max_preview_size: int = 2048,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Preview3DResult:
    """呼叫 gdaldem 產生陰影與色階圖，並使用 Pillow 進行相乘混色與邊界疊加。

    步驟（規格書 3.5 節）：
    0. DEM 長邊超過 max_preview_size 像素時，先以 gdal_translate -outsize 縮成暫存 DEM，
       後續都對縮圖運算，Pillow 不會開到全解析度影像。
    1. gdaldem hillshade out_dem.tif ./workspace_temp/hillshade.tif -z 1.5 -compute_edges
    2. gdaldem color-relief out_dem.tif ./config/terrain_ramp.txt ./workspace_temp/color.tif
    3. 用 Pillow 將 color.tif 與 hillshade.tif 進行 Multiply 混色合成。
    4. 將裁切邊界重投影到 DEM 座標系後換算為像素坐標，繪製鮮紅外框 (#FF2222)。
    """
    import time
    start_time = time.time()
    dem_p = Path(dem_path).resolve()
    if not dem_p.exists():
        raise FileNotFoundError(f"找不到 DEM 高程圖資: {dem_p}")
    if max_preview_size <= 0:
        raise ValueError(f"max_preview_size 須為正整數，收到: {max_preview_size}")

    temp_dir = get_workspace_temp_dir()
    hillshade_tif = temp_dir / "hillshade.tif"
    color_tif = temp_dir / "color.tif"
    ramp_file = ensure_terrain_ramp_file()

    final_preview_p = (
        Path(output_preview_path).resolve()
        if output_preview_path
        else (temp_dir / "preview_3d.png")
    )
    final_preview_p.parent.mkdir(parents=True, exist_ok=True)

    info_json = run_json_command(["gdalinfo", "-json", str(dem_p)])
    size = info_json.get("size") or []
    if len(size) < 2 or int(size[0]) <= 0 or int(size[1]) <= 0:
        raise ToolkitError(f"讀不到 DEM 的影像尺寸: {dem_p.name}")
    src_width, src_height = int(size[0]), int(size[1])

    with geo_io.TempVectorFiles("preview3d") as temp_files:
        # 0. 大 DEM 先縮圖
        render_dem = dem_p
        shrink_ratio = max(src_width, src_height) / float(max_preview_size)
        if shrink_ratio > 1.0:
            out_width = max(1, int(round(src_width / shrink_ratio)))
            out_height = max(1, int(round(src_height / shrink_ratio)))
            render_dem = temp_files.new("small", suffix=".tif")
            if text_callback:
                text_callback(
                    f"[3D預覽] DEM 為 {src_width} × {src_height} 像素，先縮為 {out_width} × {out_height} 再渲染。"
                )
            run_command(
                [
                    "gdal_translate", "-of", "GTiff",
                    "-outsize", str(out_width), str(out_height),
                    "-r", "bilinear",
                    str(dem_p), str(render_dem),
                ],
                text_callback=text_callback,
                check=True,
            )
            info_json = run_json_command(["gdalinfo", "-json", str(render_dem)])

        # 1. 執行 gdaldem hillshade
        if text_callback:
            text_callback("[3D預覽] 正在渲染地形山體陰影 (Hillshade)。")
        cmd_hillshade = [
            "gdaldem", "hillshade",
            str(render_dem),
            str(hillshade_tif),
            "-z", str(z_factor),
            "-compute_edges",
        ]
        run_command(cmd_hillshade, text_callback=text_callback, check=True)

        # 2. 執行 gdaldem color-relief
        if text_callback:
            text_callback("[3D預覽] 正在產生連續高程彩色著色層 (Color-Relief)。")
        cmd_color = [
            "gdaldem", "color-relief",
            str(render_dem),
            str(ramp_file),
            str(color_tif),
        ]
        run_command(cmd_color, text_callback=text_callback, check=True)

    # 3. 使用 Pillow 讀取並執行 Multiply 混色合成
    if text_callback:
        text_callback("[3D預覽] 正在透過 Pillow 進行影像混色合成。")

    with Image.open(color_tif) as img_color, Image.open(hillshade_tif) as img_shade:
        color_rgb = img_color.convert("RGB")
        shade_gray = img_shade.convert("L")

    shade_rgb = Image.merge("RGB", (shade_gray, shade_gray, shade_gray))
    blended_img = ImageChops.multiply(color_rgb, shade_rgb).convert("RGBA")

    # 4. 若指定邊界檔案，將多邊形邊界轉為像素座標並畫上鮮紅色外框
    if boundary_path and draw_boundary_outline:
        bound_p = Path(boundary_path).resolve()
        geo_trans = info_json.get("geoTransform")
        if bound_p.exists() and geo_trans and len(geo_trans) >= 6:
            x0, dx, _, y0, _, dy = [float(v) for v in geo_trans]
            crs_info = info_json.get("coordinateSystem")
            dem_wkt = crs_info.get("wkt") if isinstance(crs_info, dict) else None
            rings = _extract_boundary_coordinates(bound_p, dem_wkt)

            draw = ImageDraw.Draw(blended_img)
            red_color = (255, 30, 30, 255)
            outline_width = 3

            for ring in rings:
                # 地理座標換算為像素行列
                pixel_coords = [((world_x - x0) / dx, (world_y - y0) / dy) for world_x, world_y in ring]
                if len(pixel_coords) >= 2:
                    draw.line(pixel_coords, fill=red_color, width=outline_width)

    # 儲存高品質預覽圖
    blended_img.save(final_preview_p, format="PNG")
    duration = time.time() - start_time

    if text_callback:
        text_callback(f"[3D預覽] 3D 立體預覽縮圖生成完成，耗時 {duration:.2f} 秒。\n")

    return Preview3DResult(
        preview_image_path=final_preview_p,
        hillshade_path=hillshade_tif,
        color_relief_path=color_tif,
        image_width=blended_img.width,
        image_height=blended_img.height,
        duration_seconds=duration,
    )


# ============================================================================
# [G-17] Folium 雙軌抽稀網頁預覽 + 國土測繪圖資 WMTS
# ============================================================================

# 抽稀前先統一到公尺制座標系，simplify_tolerance 才是公尺 (ogr2ogr 的 -simplify 以來源座標單位計)
_PREVIEW_METRIC_SRS: str = f"EPSG:{EPSG_TWD97_121}"

# 內嵌到 HTML 的 GeoJSON 超過這個大小就提醒 (瀏覽器會明顯變慢)
_PREVIEW_GEOJSON_WARN_BYTES: int = 10 * 1024 * 1024

# 各圖層的 (外框色, 填色)，圖層數超過時循環使用
_PREVIEW_LAYER_COLORS: List[Tuple[str, str]] = [
    ("#FF2222", "#FFD700"),
    ("#1E6FFF", "#7FB8FF"),
    ("#1B8A3A", "#8FE0A0"),
    ("#8A2BE2", "#D2A8F0"),
    ("#FF8C00", "#FFD199"),
]

_PREVIEW_TOOLTIP_ALIASES: Dict[str, str] = {
    "NAME": "圖層/名稱:",
    "AREA_M2": "面積 (m²):",
    "AREA_HA": "面積 (公頃):",
}


def thin_and_reproject_vector_for_preview(
    input_vector_path: Union[str, Path],
    output_geojson_path: Union[str, Path],
    *,
    simplify_tolerance: float = 1.5,
    assigned_srs: Optional[str] = None,
    spatial_filter: Optional[Tuple[float, float, float, float]] = None,
    source_srs: Optional[str] = None,
    where: Optional[str] = None,
    source_layer: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """雙軌防崩潰規則：以 ogr2ogr 抽稀並轉為 EPSG:4326 GeoJSON 替身檔。

    嚴格規範（規格書 3.5 節）：
    絕對不可將數百 MB 的原始圖資塞入 Folium，否則導致瀏覽器直接當機凍結。

    分兩步：先正規化並重投影到 EPSG:3826 的暫存 GPKG，再以公尺為單位抽稀 (simplify_tolerance，
    0 表示不抽稀) 並轉成 EPSG:4326；座標保留 6 位小數 (約 0.1 m)。
    來源沒有座標系標籤時採用 assigned_srs，未指派則依座標範圍推測。
    source_srs：已經決定好的來源座標系 (-s_srs)，給了就直接用，有標籤的檔也照它 (圖資快覽用)；assigned_srs 不再參考。
    spatial_filter：(min_x, min_y, max_x, max_y)，以來源檔本身的座標只取這個範圍內的圖徵 (ogr2ogr -spat)。
    where：只取符合條件的圖徵 (ogr2ogr -where，例如 DXF 的 CAD 圖層；字串要自己跳脫)。
    source_layer：檔案裡的哪一個圖層 (KML 的資料夾、GPKG 的圖層)；None 時取第一個有圖徵的圖層。
    """
    in_p = Path(input_vector_path).resolve()
    out_p = Path(output_geojson_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not in_p.exists():
        raise FileNotFoundError(f"找不到來源向量檔案: {in_p}")

    # 若目標 GeoJSON 已存在，先主動刪除以防 GDAL 3.x GeoJSON 驅動層結構衝突
    geo_io.remove_vector_file(out_p)

    with geo_io.TempVectorFiles("preview_vec") as temp_files:
        if source_srs is None:
            source_srs = decide_and_report_source_srs(in_p, assigned_srs, text_callback=text_callback).s_srs
        extra_args: List[str] = []
        if spatial_filter:
            extra_args.extend(["-spat", *(repr(float(v)) for v in spatial_filter)])
        if where:
            extra_args.extend(["-where", where])
        staged = geo_io.stage_vector_to_gpkg(
            in_p,
            temp_files.new("stage"),
            source_layer=source_layer,
            stage_layer="preview",
            target_srs=_PREVIEW_METRIC_SRS,
            source_srs=source_srs,
            extra_args=extra_args or None,
            text_callback=text_callback,
        )

        cmd = ["ogr2ogr", "-f", "GeoJSON", "-t_srs", f"EPSG:{EPSG_WGS84}"]
        if simplify_tolerance > 0:
            cmd.extend(["-simplify", str(simplify_tolerance)])
        cmd.extend(["-lco", "COORDINATE_PRECISION=6", str(out_p), str(staged.path), "preview"])
        run_command(cmd, text_callback=text_callback, check=True)
    return out_p


def generate_folium_wmts_map(
    vector_layers: Union[Union[str, Path], Sequence[Union[str, Path]]],
    *,
    output_html_path: Optional[Union[str, Path]] = None,
    simplify_tolerance: float = 1.5,
    auto_open_browser: bool = False,
    text_callback: Optional[Callable[[str], None]] = None,
) -> FoliumPreviewResult:
    """產出套疊內政部國土測繪中心 (NLSC) WMTS 圖磚之互動式網頁地圖 preview_map.html。

    內建圖層（規格書 3.5 節）：
    1. 正射影像圖 (PHOTO2) - 航照底圖
    2. 臺灣通用電子地圖 (EMAP)
    3. 透明道路與地名疊加層 (EMAP2)
    4. 地籍圖參考層 (LAND_OPENDATA)
    5. 線上量距/面積工具 (MeasureControl)
    6. 游標經緯度座標即時顯示 (MousePosition)

    vector_layers 內每個存在的檔案各自抽稀後成為一個可開關的圖層 (顏色不同)；沒有圖徵的圖層略過並提示，
    全部都沒有圖徵時仍產出只有底圖的地圖 (中心為臺灣地理中心)。
    """
    temp_dir = get_workspace_temp_dir()
    final_html_p = (
        Path(output_html_path).resolve()
        if output_html_path
        else (temp_dir / "preview_map.html")
    )
    final_html_p.parent.mkdir(parents=True, exist_ok=True)

    input_list = [vector_layers] if isinstance(vector_layers, (str, Path)) else list(vector_layers)
    resolved_inputs: List[Path] = []
    for item in input_list:
        if Path(item).exists():
            resolved_inputs.append(Path(item).resolve())
        elif text_callback:
            text_callback(f"[地圖預覽] 找不到圖資，已略過: {Path(item).name}\n")

    if not resolved_inputs:
        raise ValueError("未提供有效的向量圖資以供產生地圖預覽！")

    # 1. 每個圖層各自抽稀並轉為 EPSG:4326 GeoJSON
    layer_payloads: List[Tuple[Path, Dict[str, Any]]] = []
    preview_geojson_paths: List[Path] = []
    lons: List[float] = []
    lats: List[float] = []
    total_features = 0

    for layer_index, source_vector in enumerate(resolved_inputs):
        index_tag = "" if layer_index == 0 else f"_{layer_index + 1}"
        preview_geojson = temp_dir / f"preview_vec_{source_vector.stem}{index_tag}.geojson"
        thin_and_reproject_vector_for_preview(
            source_vector,
            preview_geojson,
            simplify_tolerance=simplify_tolerance,
            text_callback=text_callback,
        )
        preview_geojson_paths.append(preview_geojson)

        with open(preview_geojson, "r", encoding="utf-8") as f:
            geojson_data = json.load(f)
        features = [feat for feat in geojson_data.get("features") or [] if feat.get("geometry")]
        if not features:
            if text_callback:
                text_callback(f"[地圖預覽] {source_vector.name} 沒有圖徵，地圖上不會顯示這個圖層。\n")
            continue

        if text_callback and preview_geojson.stat().st_size > _PREVIEW_GEOJSON_WARN_BYTES:
            size_mb = preview_geojson.stat().st_size / 1024 / 1024
            text_callback(
                f"[注意] {source_vector.name} 抽稀後仍有 {size_mb:.0f} MB，會整份內嵌到網頁，瀏覽器可能變慢；"
                "可加大抽稀容許度。\n"
            )

        geojson_data["features"] = features
        total_features += len(features)
        for feat in features:
            for lon, lat in _iter_geometry_positions(feat["geometry"]):
                lons.append(lon)
                lats.append(lat)
        layer_payloads.append((source_vector, geojson_data))

    # 以全部圖層的範圍中心當地圖中心，若無座標則預設為臺灣地理中心 (南投埔里 23.9738, 120.9820)
    if lons and lats:
        center_lat = (min(lats) + max(lats)) / 2.0
        center_lon = (min(lons) + max(lons)) / 2.0
        zoom_start = 14
    else:
        center_lat = 23.9738
        center_lon = 120.9820
        zoom_start = 8

    # 2. 構建 Folium Map 物件＋國土測繪中心底圖
    folium_map = new_nlsc_map(center_lat, center_lon, zoom_start)

    # 3. 疊加抽稀後的工程向量圖資 (第一個圖層紅框金底，其餘依序換色)
    for layer_index, (source_vector, geojson_data) in enumerate(layer_payloads):
        outline_color, fill_color = _PREVIEW_LAYER_COLORS[layer_index % len(_PREVIEW_LAYER_COLORS)]
        folium.GeoJson(
            geojson_data,
            name=f"工程裁切圖資: {source_vector.name}",
            style_function=vector_style_function(outline_color, fill_color),
            tooltip=area_tooltip(geojson_data["features"]),
        ).add_to(folium_map)

    if lons and lats:
        folium_map.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], max_zoom=18)

    # 4. 量測、游標座標、全螢幕與圖層開關
    add_map_tools(folium_map)

    # 儲存 HTML
    folium_map.save(str(final_html_p))

    if text_callback:
        text_callback(f"[地圖預覽] Folium 互動地圖已產生: {final_html_p.name}\n")

    if auto_open_browser:
        open_preview_in_browser(final_html_p)

    return FoliumPreviewResult(
        html_path=final_html_p,
        thinned_geojson_path=preview_geojson_paths[0],
        center_lat=center_lat,
        center_lon=center_lon,
        feature_count=total_features,
        thinned_geojson_paths=preview_geojson_paths,
    )


def new_nlsc_map(center_lat: float = 23.9738, center_lon: float = 120.9820, zoom_start: int = 8) -> folium.Map:
    """建立以國土測繪中心 (NLSC) 圖磚為底圖的 Folium 地圖 (預設中心為臺灣地理中心)。"""
    folium_map = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom_start,
        tiles=None,  # 停用預設 OpenStreetMap，改用內政部圖資
        control_scale=True,
    )

    # 注入國土測繪中心 (NLSC) 4 大標準 WMTS 圖磚圖層
    # 航照正射影像 (PHOTO2)
    folium.TileLayer(
        tiles=NLSC_WMTS_PHOTO2,
        attr="內政部國土測繪中心 - 正射影像圖 (PHOTO2)",
        name="國土測繪 - 航照正射影像",
        overlay=False,
        control=True,
        max_zoom=20,
    ).add_to(folium_map)

    # 臺灣通用電子地圖 (EMAP)
    folium.TileLayer(
        tiles=NLSC_WMTS_EMAP,
        attr="內政部國土測繪中心 - 臺灣通用電子地圖 (EMAP)",
        name="國土測繪 - 臺灣通用電子地圖",
        overlay=False,
        control=True,
        # 底圖一次只開一個：開地圖時是航照，電子地圖要在圖層開關選 (兩個都開會疊在航照上面、白載一份圖磚)
        show=False,
        max_zoom=20,
    ).add_to(folium_map)

    # 透明道路與地名疊加層 (EMAP2) - 預設開啟疊加
    folium.TileLayer(
        tiles=NLSC_WMTS_EMAP2,
        attr="內政部國土測繪中心 - 透明道路標記 (EMAP2)",
        name="[疊加] 透明路網與地名標註",
        overlay=True,
        control=True,
        show=True,
        max_zoom=20,
    ).add_to(folium_map)

    # 地籍圖參考層 (LAND_OPENDATA) - 預設可切換勾選
    folium.TileLayer(
        tiles=NLSC_WMTS_LAND_OPENDATA,
        attr="內政部國土測繪中心 - 地籍圖開放資料",
        name="[疊加] 地籍圖界線層",
        overlay=True,
        control=True,
        show=False,
        max_zoom=20,
    ).add_to(folium_map)

    # 空白底圖 (白底，簡易出圖用)：沒有圖磚的底圖選項，選了只剩地圖本身的白色背景
    # (leaflet.css 排在這段後面，同樣的選擇器會被它的灰底蓋掉，所以多指定 .folium-map)
    folium.FeatureGroup(name=BLANK_BASEMAP_NAME, overlay=False, control=True, show=False).add_to(folium_map)
    folium_map.get_root().header.add_child(
        folium.Element(f"<style>.folium-map.leaflet-container {{ background: {BLANK_BASEMAP_COLOR}; }}</style>")
    )
    return folium_map


def vector_style_function(
    outline_color: str, fill_color: str, *, weight: float = 2.5, fill_opacity: float = 0.35
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """回傳 folium.GeoJson 用的樣式函式 (全部圖徵同一個樣式)。"""

    def _style_function(feature: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "fillColor": fill_color,
            "color": outline_color,
            "weight": weight,
            "fillOpacity": fill_opacity,
        }

    return _style_function


def area_tooltip(features: Sequence[Dict[str, Any]]) -> Optional[folium.GeoJsonTooltip]:
    """名稱與面積欄位的提示框；只列每個圖徵都有的欄位 (folium 遇到資料裡沒有的欄位會直接拋錯)，都沒有時回傳 None。"""
    tooltip_fields = [
        field_name
        for field_name in _PREVIEW_TOOLTIP_ALIASES
        if all(field_name in (feat.get("properties") or {}) for feat in features)
    ]
    if not tooltip_fields:
        return None
    return folium.GeoJsonTooltip(
        fields=tooltip_fields,
        aliases=[_PREVIEW_TOOLTIP_ALIASES[field_name] for field_name in tooltip_fields],
        localize=True,
    )


def add_map_tools(folium_map: folium.Map) -> None:
    """加上量距量面積、游標經緯度、全螢幕與圖層開關 (圖層開關要最後加，才會列出前面所有圖層)。"""
    # 距離與面積即時測量工具
    plugins.MeasureControl(
        position="topleft",
        primary_length_unit="meters",
        secondary_length_unit="kilometers",
        primary_area_unit="sqmeters",
        secondary_area_unit="hectares",
        active_color="#FF0000",
        completed_color="#800000",
    ).add_to(folium_map)

    # 游標經緯度座標即時追蹤
    plugins.MousePosition(
        position="bottomright",
        separator=" | ",
        empty_string="NaN",
        lng_first=True,
        num_digits=6,
        prefix="WGS84 (經度/緯度):",
    ).add_to(folium_map)

    # 全螢幕切換按鈕
    plugins.Fullscreen(position="topright").add_to(folium_map)

    # 圖層切換控制器
    folium.LayerControl(position="topright", collapsed=False).add_to(folium_map)


def export_raster_preview_png(
    raster_path: Union[str, Path],
    output_png_path: Optional[Union[str, Path]] = None,
    *,
    max_size: int = 1024,
) -> Path:
    """把 8 位元的 RGB／RGBA 網格 (例如土方紅藍色階圖) 轉成 PNG 縮圖，供右欄預覽畫布載入。

    長邊超過 max_size 時等比例縮小；未指定輸出位置時寫到暫存目錄的 <檔名>_preview.png。
    """
    src = Path(raster_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"找不到要預覽的網格: {src}")
    if max_size < 1:
        raise ValueError(f"max_size 必須是正整數: {max_size}")
    out = (
        Path(output_png_path).resolve()
        if output_png_path
        else get_workspace_temp_dir() / f"{src.stem}_preview.png"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    width, height = (int(v) for v in run_json_command(["gdalinfo", "-json", str(src)])["size"])
    scale = min(1.0, float(max_size) / float(max(width, height)))
    out_w = max(1, int(round(width * scale)))
    out_h = max(1, int(round(height * scale)))
    run_command(
        [
            "gdal_translate",
            "-of", "PNG",
            "-outsize", str(out_w), str(out_h),
            "--config", "GDAL_PAM_ENABLED", "NO",
            str(src), str(out),
        ],
        check=True,
    )
    return out


def open_preview_in_browser(html_path: Union[str, Path]) -> None:
    """呼叫系統預設瀏覽器秒開地圖預覽網頁。

    Windows 直接用 .html 的關聯程式開檔：有些電腦的 file: 通訊協定沒有登錄開啟指令，
    webbrowser.open("file:///...") 會失敗並退回 IEXPLORE.EXE，在 Windows 11 被導到 Edge 的 MSN 首頁。
    """
    p = Path(html_path).resolve()
    if not p.exists():
        return
    if os.name == "nt":
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
            return
        except OSError as err:
            logger.warning("以 .html 關聯程式開啟失敗，改用 webbrowser: %s", err)
    webbrowser.open(p.as_uri())


# ============================================================================
# 一鍵導出 Google Earth (.kmz)
# ============================================================================

def export_to_kmz(
    vector_path: Union[str, Path],
    output_kmz_path: Optional[Union[str, Path]] = None,
    *,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """將向量圖資一鍵轉存為 Google Earth 專用之 .kmz 檔案。

    防呆相容策略：
    1. 優先嘗試 GDAL 原生 LIBKML 驅動程式直接輸出 .kmz。
    2. 若系統缺少 LIBKML，自動回退使用標準 KML 驅動程式產出 doc.kml，
       再以 Python 標準庫 zipfile 無損打包為合規的 .kmz，保證 100% 成功。
    """
    in_p = Path(vector_path).resolve()
    if not in_p.exists():
        raise FileNotFoundError(f"找不到來源圖資: {in_p}")

    out_kmz = (
        Path(output_kmz_path).resolve()
        if output_kmz_path
        else in_p.with_suffix(".kmz")
    )
    out_kmz.parent.mkdir(parents=True, exist_ok=True)

    if text_callback:
        text_callback(f"[匯出KMZ] 正在將 {in_p.name} 轉存為 Google Earth KMZ 檔。\n")

    # 方法 1：嘗試以 LIBKML 輸出
    cmd_libkml = [
        "ogr2ogr",
        "-f", "LIBKML",
        "-t_srs", f"EPSG:{EPSG_WGS84}",
        "-overwrite",
        str(out_kmz),
        str(in_p),
    ]
    res = run_command(cmd_libkml, text_callback=text_callback, check=False)

    if res.is_success and out_kmz.exists():
        if text_callback:
            text_callback("[匯出KMZ] 透過 LIBKML 原生驅動程式輸出成功。\n")
        return out_kmz

    # 方法 2：標準 KML + Python zipfile 打包回退機制
    logger.debug("LIBKML 驅動程式未啟用，切換至 KML + Zip 備用封裝流程。")
    temp_dir = get_workspace_temp_dir()
    temp_kml = temp_dir / f"{in_p.stem}_temp.kml"

    cmd_kml = [
        "ogr2ogr",
        "-f", "KML",
        "-t_srs", f"EPSG:{EPSG_WGS84}",
        "-overwrite",
        str(temp_kml),
        str(in_p),
    ]
    run_command(cmd_kml, text_callback=text_callback, check=True)

    # 將 doc.kml 壓縮為 .kmz
    with zipfile.ZipFile(out_kmz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(temp_kml, arcname="doc.kml")

    try:
        temp_kml.unlink()
    except OSError as err:
        logger.debug("移除中繼 KML 略過: %s", err)

    if text_callback:
        text_callback(f"[匯出KMZ] 備用封裝完成，已產出 {out_kmz.name}。\n")

    return out_kmz
