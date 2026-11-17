"""圖資快覽的瀏覽器地圖 (core/quick_view_map.py)

國土測繪中心底圖 (航照、電子地圖、空白白底，見 core/preview_engine.new_nlsc_map) 加上勾選的圖資，
每個圖資各自一層 (DXF 勾選的 CAD 圖層各自一層)，都可以在右上角的圖層開關打開或關掉。
- 向量：抽稀後轉成經緯度 GeoJSON；點圖徵跳出完整屬性表，滑鼠移上去顯示前幾個有值的欄位；點畫成小圓點。
  圖徵超過 OUTLINE_ONLY_FEATURES 個的圖層只放外框、不帶屬性 (使用者決定，門檻在 H2 實測)。
- DEM／正射：縮圖後轉成網頁地圖座標系疊上 (跟裁切工具的瀏覽器地圖同一套畫法，core/clip_preview.py)。
來源座標系：MapSource.source_srs 有值就以它為準，有座標系標籤的檔也照它 (給標錯座標的檔用)；
None 時有標籤照標籤，沒有標籤依座標範圍推測。某一層做不出來只略過那一層並寫原因；全部都做不出來時拋 ToolkitError。
地圖左上有「出圖」(core/map_print_layout.py)：圖名、指北針、圖例 (每層的顏色與名稱)、A4／A3 橫式列印。

預覽只供目視確認位置與屬性，數值以原始檔為準。運算都經 GDAL/OGR CLI。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from core import geo_io
from core.clip_preview import (
    STYLE_DEM,
    STYLE_IMAGE,
    STYLE_VECTOR,
    WEB_LAYER_MAX_SIZE,
    Extent,
    _picture_to_web_mercator,
    _png_data_url,
    _render_raster,
    _union_extent,
    source_srs_for_drawing,
)
from core.crs_doctor import SourceSrsUndeterminedError, resolve_assigned_srs
from core.csv_points import rename_in_messages
from core.env_runner import ToolkitError
from core.lazy_import import LazyModule
from core.map_print_layout import (
    LEGEND_DEM,
    LEGEND_IMAGE,
    LEGEND_LINE,
    LEGEND_POINT,
    LEGEND_POLYGON,
    LegendEntry,
    add_print_layout,
)
from core.preview_engine import (
    _PREVIEW_GEOJSON_WARN_BYTES,
    _PREVIEW_LAYER_COLORS,
    _iter_geometry_positions,
    add_map_tools,
    new_nlsc_map,
    thin_and_reproject_vector_for_preview,
    vector_style_function,
)

# folium 第一次畫瀏覽器地圖時才載入 (core/lazy_import.py)
folium = LazyModule("folium")

logger = logging.getLogger("core.quick_view_map")

TextCallback = Optional[Callable[[str], None]]

# 向量抽稀 (公尺)：跟裁切工具的瀏覽器地圖相同
WEB_SIMPLIFY_M: float = 1.5
# 滑鼠移上去顯示的欄位數
TOOLTIP_FIELD_COUNT: int = 3
# 圖徵超過這個數的圖層只放外框、不帶屬性彈窗 (屬性會讓網頁大好幾倍)
OUTLINE_ONLY_FEATURES: int = 20_000
# 屬性表彈窗的最大高度 (px)，欄位多時捲動
POPUP_MAX_HEIGHT: int = 360
POINT_RADIUS: int = 5
VECTOR_FILL_OPACITY: float = 0.25
DEM_OPACITY: float = 0.75
IMAGE_OPACITY: float = 1.0
NO_SRS_REASON: str = "沒有座標系標籤也推測不出來，請在清單下面選這一列的來源座標系"
# GeoJSON 幾何型別 → 圖例符號
_LEGEND_KIND_BY_GEOMETRY: Dict[str, str] = {
    "Point": LEGEND_POINT, "MultiPoint": LEGEND_POINT,
    "LineString": LEGEND_LINE, "MultiLineString": LEGEND_LINE,
    "Polygon": LEGEND_POLYGON, "MultiPolygon": LEGEND_POLYGON,
}


@dataclass(frozen=True)
class MapSource:
    """要放上地圖的一個圖資。

    style：STYLE_VECTOR／STYLE_DEM／STYLE_IMAGE。
    source_srs：ASSIGNABLE_SOURCE_SRS 的鍵 (例如 "TWD67_121")；有值就以它為準，None 時依原始標籤或推測。
    cad_layers：DXF 要放的 CAD 圖層，各自一層；空的表示整個檔一層。
    title：圖層開關與訊息上的檔名；None 時用 path 的檔名 (座標表轉成的暫存點位給原本的 .csv 檔名)。
    data_layers：檔案裡要放的圖層 (KML／KMZ 的資料夾、GPKG 的圖層、GPX 的航點／路線／軌跡)，各自一層；
    空的表示取第一個有圖徵的圖層。
    """

    path: Path
    style: str
    source_srs: Optional[str] = None
    cad_layers: Tuple[str, ...] = ()
    title: Optional[str] = None
    data_layers: Tuple[str, ...] = ()

    def units(self) -> List[Tuple[Optional[str], Optional[str]]]:
        """各自一層的單位 (檔案裡的圖層, DXF 的 CAD 圖層)：DXF 照 CAD 圖層、多圖層的檔照圖層，其他整個檔一層。"""
        if self.cad_layers:
            return [(None, cad_layer) for cad_layer in self.cad_layers]
        if self.data_layers:
            return [(data_layer, None) for data_layer in self.data_layers]
        return [(None, None)]


@dataclass
class QuickViewMap:
    """瀏覽器地圖的成果：HTML、放上去的圖層 (順序同圖層開關) 與各層的經緯度範圍、圖例、
    略過的圖層與原因、只放外框 (圖徵太多、不帶屬性) 的圖層。"""

    html_path: Path
    layer_names: List[str] = field(default_factory=list)
    extents: Dict[str, Extent] = field(default_factory=dict)
    legend: List[LegendEntry] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    outline_only: List[str] = field(default_factory=list)


def layer_title(path: Path, cad_layer: Optional[str] = None) -> str:
    """圖層開關上的名稱：檔名，DXF 的 CAD 圖層 (或 KML 的資料夾等檔案裡的圖層) 寫「檔名：圖層」。"""
    return f"{path.name}：{cad_layer}" if cad_layer else path.name


def _clean_properties(features: Sequence[Dict[str, Any]]) -> List[str]:
    """空值改成空字串 (彈窗不要寫 null)，回傳欄位名 (照第一個圖徵的順序；ogr2ogr 寫的 GeoJSON 每個圖徵欄位都一樣)。"""
    for feat in features:
        feat["properties"] = {key: ("" if value is None else value) for key, value in (feat.get("properties") or {}).items()}
    return list(features[0]["properties"]) if features else []


def _tooltip_fields(features: Sequence[Dict[str, Any]], fields: Sequence[str]) -> List[str]:
    """滑鼠移上去顯示的欄位：前幾個至少有一個圖徵有值的欄位。"""
    filled = [name for name in fields if any(str(feat["properties"].get(name, "")).strip() for feat in features)]
    return filled[:TOOLTIP_FIELD_COUNT]


def _legend_kind(features: Sequence[Dict[str, Any]]) -> str:
    """圖例符號：圖徵最多的幾何型別 (DXF 的一個圖層可能點線混雜；GeometryCollection 等算線)。"""
    counts: Dict[str, int] = {}
    for feat in features:
        kind = _LEGEND_KIND_BY_GEOMETRY.get(feat["geometry"].get("type", ""), LEGEND_LINE)
        counts[kind] = counts.get(kind, 0) + 1
    return max(counts, key=lambda kind: counts[kind])


def _add_vector_layer(
    fmap: Any,
    source: MapSource,
    title: str,
    forced_srs: Optional[str],
    cad_layer: Optional[str],
    colors: Tuple[str, str],
    temp_files: geo_io.TempVectorFiles,
    result: QuickViewMap,
    text_callback: TextCallback,
    data_layer: Optional[str] = None,
) -> Tuple[Extent, LegendEntry]:
    """向量抽稀後轉成經緯度 GeoJSON 疊上，加完整屬性表彈窗與提示，回傳 (經緯度範圍, 圖例)。
    圖徵超過 OUTLINE_ONLY_FEATURES 個時只放外框 (不填色、不帶屬性)，記在 result.outline_only。"""
    geojson_path = temp_files.new("view", suffix=".geojson")
    try:
        thin_and_reproject_vector_for_preview(
            source.path,
            geojson_path,
            simplify_tolerance=WEB_SIMPLIFY_M,
            source_srs=forced_srs,
            where=f"Layer = {geo_io.quote_literal(cad_layer)}" if cad_layer else None,
            source_layer=data_layer,
            text_callback=text_callback,
        )
    except SourceSrsUndeterminedError as err:
        raise ToolkitError(NO_SRS_REASON) from err
    if text_callback and geojson_path.stat().st_size > _PREVIEW_GEOJSON_WARN_BYTES:
        size_mb = geojson_path.stat().st_size / 1024 / 1024
        text_callback(f"[地圖預覽] 「{title}」抽稀後仍有 {size_mb:.0f} MB，瀏覽器開啟會比較慢。\n")
    # 有些軟體把中文欄位名按位元組截成 10 bytes，切在半個字上；ogr2ogr 照抄成不合法的 UTF-8，壞掉的字元換成 �
    with open(geojson_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    features = [feat for feat in data.get("features") or [] if feat.get("geometry")]
    if not features:
        raise ToolkitError("沒有圖徵")
    # 每個圖徵給自己的 id：沒有 id 時 folium 會挑一個值都不重複的欄位寫進 JS (feature.properties.欄位)，
    # 欄位名有空白或括號 (例如「面積(m2)」) 時整張地圖的 JS 會壞掉
    for number, feat in enumerate(features):
        feat["id"] = str(number)
    data["features"] = features
    outline, fill = colors
    fill_opacity = VECTOR_FILL_OPACITY
    if len(features) > OUTLINE_ONLY_FEATURES:
        for feat in features:
            feat["properties"] = {}
        fill_opacity = 0.0
        result.outline_only.append(title)
        if text_callback:
            text_callback(
                f"[地圖預覽] 「{title}」有 {len(features):,} 個圖徵，超過 {OUTLINE_ONLY_FEATURES:,} 個只放外框、"
                "不帶屬性 (點了不會跳屬性表)。\n"
            )
    fields = _clean_properties(features)
    tooltip_fields = _tooltip_fields(features, fields)
    legend_kind = _legend_kind(features)
    layer = folium.GeoJson(
        data,
        name=title,
        style_function=vector_style_function(outline, fill, fill_opacity=fill_opacity),
        marker=folium.CircleMarker(radius=POINT_RADIUS, fill=True),
        tooltip=folium.GeoJsonTooltip(fields=tooltip_fields, localize=True) if tooltip_fields else None,
        popup=folium.GeoJsonPopup(fields=fields, localize=True, max_height=POPUP_MAX_HEIGHT) if fields else None,
    )
    layer.add_to(fmap)
    positions = [pos for feat in features for pos in _iter_geometry_positions(feat["geometry"])]
    lons = [lon for lon, _ in positions]
    lats = [lat for _, lat in positions]
    # 點用 CircleMarker 預設的填色透明度 (0.2)
    point_fill = 0.2 if legend_kind == LEGEND_POINT and fill_opacity > 0 else fill_opacity
    legend = LegendEntry(layer.get_name(), title, legend_kind, outline, fill, point_fill)
    return (min(lons), min(lats), max(lons), max(lats)), legend


def _add_raster_layer(
    fmap: Any,
    source: MapSource,
    title: str,
    forced_srs: Optional[str],
    tag: str,
    temp_files: geo_io.TempVectorFiles,
) -> Tuple[Extent, LegendEntry]:
    """網格縮圖轉成網頁地圖座標後以 ImageOverlay 疊上，回傳 (經緯度範圍, 圖例)。"""
    srs = forced_srs or source_srs_for_drawing(source.path, source.style)
    if srs is None:
        raise ToolkitError(NO_SRS_REASON)
    picture = _render_raster(source.path, source.style, WEB_LAYER_MAX_SIZE, temp_files)
    image, (west, south, east, north) = _picture_to_web_mercator(picture, srs, tag, temp_files)
    layer = folium.raster_layers.ImageOverlay(
        image=_png_data_url(image),
        bounds=[[south, west], [north, east]],
        name=title,
        opacity=DEM_OPACITY if source.style == STYLE_DEM else IMAGE_OPACITY,
        interactive=False,
    )
    layer.add_to(fmap)
    kind = LEGEND_DEM if source.style == STYLE_DEM else LEGEND_IMAGE
    return (west, south, east, north), LegendEntry(layer.get_name(), title, kind)


def build_quick_view_map(
    output_html: Union[str, Path],
    sources: Sequence[MapSource],
    *,
    text_callback: TextCallback = None,
) -> QuickViewMap:
    """產生圖資快覽的瀏覽器地圖：每個圖資 (DXF 的每個 CAD 圖層) 各自一層，照 sources 的順序疊。

    某一層做不出來只略過並記下原因 (QuickViewMap.skipped，也寫進日誌)；全部都做不出來時拋 ToolkitError。
    """
    out = Path(output_html).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fmap = new_nlsc_map()
    result = QuickViewMap(html_path=out)
    color_index = 0

    def _try(title: str, add: Callable[[], Tuple[Extent, LegendEntry]]) -> None:
        try:
            extent, legend = add()
        except Exception as err:
            logger.warning("地圖圖層「%s」略過: %s", title, err)
            result.skipped.append(f"{title}：{err}")
            if text_callback:
                text_callback(f"[地圖預覽] 「{title}」略過：{err}\n")
            return
        result.layer_names.append(title)
        result.extents[title] = extent
        result.legend.append(legend)

    with geo_io.TempVectorFiles("viewmap") as temp_files:
        for index, source in enumerate(sources):
            path = Path(source.path).resolve()
            name = Path(source.title or path.name)
            callback = rename_in_messages(text_callback, path.name, name.name)
            source = MapSource(
                path, source.style, source.source_srs, tuple(source.cad_layers), source.title, tuple(source.data_layers)
            )
            try:
                forced_srs = resolve_assigned_srs(source.source_srs)
            except ToolkitError as err:
                result.skipped.append(f"{name.name}：{err}")
                continue
            if forced_srs and callback:
                callback(f"[座標系] {name.name}：照清單選的來源座標系 ({source.source_srs}) 疊圖。\n")
            if source.style == STYLE_VECTOR:
                for data_layer, cad_layer in source.units():
                    title = layer_title(name, cad_layer or data_layer)
                    colors = _PREVIEW_LAYER_COLORS[color_index % len(_PREVIEW_LAYER_COLORS)]
                    color_index += 1
                    _try(
                        title,
                        lambda: _add_vector_layer(
                            fmap, source, title, forced_srs, cad_layer, colors, temp_files, result, callback, data_layer
                        ),
                    )
            elif source.style in (STYLE_DEM, STYLE_IMAGE):
                title = layer_title(name)
                _try(title, lambda: _add_raster_layer(fmap, source, title, forced_srs, f"raster{index}", temp_files))
            else:
                raise ValueError(f"不支援的畫法: {source.style}")

    if not result.layer_names:
        reasons = "\n".join(result.skipped) or "沒有勾選的圖資"
        raise ToolkitError(f"勾選的圖資都無法放上地圖：\n{reasons}")

    bounds = _union_extent(list(result.extents.values()))
    if bounds is not None:
        west, south, east, north = bounds
        fmap.fit_bounds([[south, west], [north, east]], max_zoom=18)
    add_map_tools(fmap)
    add_print_layout(fmap, result.legend)
    fmap.save(str(out))
    if text_callback:
        text_callback(f"[地圖預覽] 已產生瀏覽器地圖 (可勾選：{'、'.join(result.layer_names)})。\n")
    return result
