"""圖資快覽的另存 SHP／KML／KMZ (core/quick_view_export.py)

另存 SHP (export_to_shapefiles)：
把勾選的向量圖資 (SHP、GPKG、GeoJSON、KML／KMZ、GPX、DXF 勾選的 CAD 圖層) 轉成選定的輸出座標系與文字編碼的 Shapefile。
- 一個檔有好幾個圖層 (KML 的資料夾、GPKG 的圖層、GPX 的航點／路線／軌跡) 時，勾選的圖層各自存 (<檔名>_<圖層>.shp)；
  KML 的顯示設定欄位與整欄空白的欄位不存 (geo_io.droppable_format_fields)。
- 來源座標系跟瀏覽器地圖 (core/quick_view_map.py) 同一套規則：ExportSource.source_srs 有值就以它為準
  (有座標系標籤的檔也照它)；None 時有標籤照標籤，沒有標籤依座標範圍推測，推測不出來的擋下並說原因。
- 一個 SHP 只能放一種幾何：DXF 依點、線、面分開存成 <檔名>_<CAD圖層>_point／_line／_poly.shp (只存有圖徵的型別)，
  跟 core/vector_ops.split_dxf_by_geometry_type 一樣只留 Layer、Text 欄位、轉成 2D；圖塊等幾何集合先拆開再分。
  其他格式的圖層點線面混雜時也一樣分開 (<檔名>_point…)，只有一種就存成 <檔名>.shp。
- DXF 可選「封閉線轉面」：首尾相接的線 (含 CAD 裡設成封閉的聚合線) 存成面。
- 文字編碼 UTF-8 (附 .cpg) 或 CP950；欄位名超過 10 bytes 在字元邊界截斷 (geo_io.export_vector)。
- 輸出資料夾裡的同名 SHP 會被取代；不會蓋掉來源檔，同一次存出的檔名重複時加 _2、_3。

另存 KML／KMZ (export_to_kml，兩種共用；KMZ 是把同一份 KML 壓成 doc.kml)：
- 每個來源檔存一個 (<檔名>.kml／.kmz，檔名規則同 SHP)；座標系固定 WGS84 經緯度、UTF-8 (KML 的規定)。
- 檔裡的圖層 (DXF 勾選的 CAD 圖層、KML 的資料夾等) 各一個資料夾，資料夾名是圖層名 (只有一層時是主檔名)；
  點線面放在一起不拆；DXF 同 SHP 只留 Layer、Text、2D，「封閉線轉面」同樣適用 (圍不出面的維持線)。
- 有「名稱」或「Name」欄位 (不分大小寫，「名稱」優先) 的當標記名，其他屬性放進 ExtendedData (Google Earth 點了看得到)。
- 每個資料夾一種顏色 (同瀏覽器地圖)：線 2 px、面半透明填色；沒有樣式時 Google Earth 會把面畫成不透明白色。
- GDAL 直接寫 KMZ 會把每個圖層拆成另一個 KML (壓縮檔裡的中文檔名會亂碼)，中文圖層名直接當 id 會變成一串底線而撞號，
  所以先用 LIBKML 寫成圖層名是 layer_1、layer_2… 的 KML，再把資料夾名換回圖層名、加樣式，KMZ 自己壓。
某一個圖層做不出來只略過並記下原因；全部都做不出來時拋 ToolkitError。運算都經 GDAL/OGR CLI。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from core import geo_io
from core.crs_doctor import (
    EPSG_WGS84,
    SourceSrsUndeterminedError,
    decide_and_report_source_srs,
    resolve_assigned_srs,
    srs_is_geographic,
)
from core.csv_points import rename_in_messages
from core.env_runner import CommandCancelledError, ToolkitError, run_command
from core.preview_engine import _PREVIEW_LAYER_COLORS
from core.quick_view_map import VECTOR_FILL_OPACITY

logger = logging.getLogger("core.quick_view_export")

TextCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[float, str], None]]

DEFAULT_TARGET_SRS: str = "TWD97_121"
NO_SRS_REASON: str = "沒有座標系標籤也推測不出來，請在清單下面選這一列的來源座標系"
# DXF 只留的欄位 (CAD 圖層名與文字內容，同 vector_ops 的 DXF 分層)
DXF_KEPT_FIELDS: Tuple[str, ...] = ("Layer", "Text")
# 幾何分流：檔名尾碼 → (SQLite 條件, ogr2ogr 的輸出幾何參數)；同 vector_ops 的 DXF 分層，點拆成單點、線與面存成多重
_KIND_POINT, _KIND_LINE, _KIND_POLY = "point", "line", "poly"
_GEOMETRY_ARGS: Dict[str, Tuple[str, ...]] = {
    _KIND_POINT: ("-explodecollections", "-nlt", "POINT"),
    _KIND_LINE: ("-nlt", "MULTILINESTRING"),
    _KIND_POLY: ("-nlt", "MULTIPOLYGON"),
}
_KIND_TEXT: Dict[str, str] = {_KIND_POINT: "點", _KIND_LINE: "線", _KIND_POLY: "面"}
# read_layer_info 的 geometry_type (ogr.GeometryTypeToName) 去掉「3D」「Multi」後是這幾個時，整層只有一種幾何
_SINGLE_GEOMETRY_NAMES: Dict[str, str] = {"point": _KIND_POINT, "linestring": _KIND_LINE, "polygon": _KIND_POLY}

# KML／KMZ：座標系與編碼是 KML 規定的，不能選
KML_SRS: str = f"EPSG:{EPSG_WGS84}"
KML_ENCODING: str = "UTF-8"
# 當標記名的欄位 (不分大小寫，前面的優先)
KML_NAME_FIELDS: Tuple[str, ...] = ("名稱", "Name")
KML_LINE_WIDTH: int = 2
_KML_NAMESPACE: str = "http://www.opengis.net/kml/2.2"
# 暫存 GPKG 裡標記名欄位的別名 (寫 KML 時用 LIBKML_NAME_FIELD 指定它當 <name>)
_KML_NAME_COLUMN: str = "wl_kml_name"
# KMZ 裡的主檔名 (Google Earth 讀壓縮檔裡第一個 .kml)
KMZ_DOC_NAME: str = "doc.kml"


@dataclass(frozen=True)
class ExportSource:
    """要另存成 SHP 的一個向量圖資。

    source_srs：ASSIGNABLE_SOURCE_SRS 的鍵 (例如 "TWD67_121")；有值就以它為準，None 時依原始標籤或推測。
    cad_layers：DXF 要存的 CAD 圖層，各自存檔；空的表示整個檔。
    title：訊息上的檔名，存出的 SHP 也用它的主檔名；None 時用 path 的檔名 (座標表轉成的暫存點位給原本的 .csv 檔名)。
    data_layers：檔案裡要存的圖層 (KML／KMZ 的資料夾、GPKG 的圖層、GPX 的航點／路線／軌跡)，各自存檔；
    空的表示取第一個有圖徵的圖層。
    """

    path: Path
    source_srs: Optional[str] = None
    cad_layers: Tuple[str, ...] = ()
    title: Optional[str] = None
    data_layers: Tuple[str, ...] = ()

    def units(self) -> List[Tuple[Optional[str], Optional[str]]]:
        """各自存檔的單位 (檔案裡的圖層, DXF 的 CAD 圖層)，同 MapSource.units。"""
        if self.cad_layers:
            return [(None, cad_layer) for cad_layer in self.cad_layers]
        if self.data_layers:
            return [(data_layer, None) for data_layer in self.data_layers]
        return [(None, None)]


@dataclass
class QuickViewExport:
    """另存的成果：存出的檔 (照存檔順序)、沒有存的圖層與原因 (「名稱：原因」)。"""

    output_dir: Path
    output_files: List[Path] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


def unit_title(path: Path, cad_layer: Optional[str] = None) -> str:
    """日誌與略過清單上的名稱：檔名，DXF 的 CAD 圖層 (或 KML 的資料夾等檔案裡的圖層) 寫「檔名：圖層」(同瀏覽器地圖的圖層名)。"""
    return f"{path.name}：{cad_layer}" if cad_layer else path.name


def file_label(text: str) -> str:
    """CAD 圖層名 (或 KML 的資料夾等檔案裡的圖層名) 放進檔名：換掉 Windows 檔名不能用的字元，不能以點或空白結尾。"""
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", str(text)).strip().rstrip(". ")
    return cleaned[:60] or "空白"


def _single_kind(geometry_type: str) -> Optional[str]:
    """圖層宣告的幾何型別只有點、線或面其中一種時回傳那一種；Unknown、GeometryCollection 等回傳 None。"""
    name = geometry_type.lower().replace("3d", "").replace("measured", "").replace("multi", "").replace(" ", "")
    return _SINGLE_GEOMETRY_NAMES.get(name)


def _kind_sql(kind: str, fields: Sequence[str], closed_lines_as_polygons: bool) -> str:
    """從暫存 GPKG (圖層 stage、幾何欄位 geom) 取出某一種幾何的 SQL (SQLite 方言)。
    ST_IsClosed 只認單一條線 (其他型別回傳 -1)，所以一律寫成「= 1」。"""
    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    is_line = f"ST_GeometryType({geom}) LIKE '%LINESTRING%'"
    is_closed = f"COALESCE(ST_IsClosed({geom}), 0) = 1"
    expression = geom
    if kind == _KIND_POINT:
        condition = f"ST_GeometryType({geom}) LIKE '%POINT%'"
    elif kind == _KIND_LINE:
        condition = f"{is_line} AND NOT ({is_closed})" if closed_lines_as_polygons else is_line
    else:
        condition = f"ST_GeometryType({geom}) LIKE '%POLYGON%'"
        if closed_lines_as_polygons:
            condition = f"({condition}) OR ({is_line} AND {is_closed})"
            expression = f"CASE WHEN {is_line} THEN ST_BuildArea({geom}) ELSE {geom} END"
    columns = ", ".join([f"{expression} AS {geom}", *fields])
    return f'SELECT {columns} FROM "stage" WHERE {condition}'


class _Names:
    """這一次存出的檔名：Windows 檔名不分大小寫，重複時依序加 _2、_3；
    輸出資料夾裡同副檔名的來源檔 (例如另存 SHP 時的來源 SHP) 先占住，不會被蓋掉。"""

    def __init__(self, output_dir: Path, sources: Sequence[ExportSource], suffix: str = ".shp") -> None:
        self.output_dir = output_dir
        self.suffix = suffix
        self._used: Set[str] = set()
        for source in sources:
            path = Path(source.path).resolve()
            if path.suffix.lower() == suffix and path.parent == output_dir:
                self._used.add(path.stem.casefold())

    def claim(self, base: str) -> Path:
        candidate, serial = base, 2
        while candidate.casefold() in self._used:
            candidate = f"{base}_{serial}"
            serial += 1
        self._used.add(candidate.casefold())
        return self.output_dir / f"{candidate}{self.suffix}"


def _decide_source_srs(path: Path, name: Path, chosen: Optional[str], text_callback: TextCallback) -> Optional[str]:
    """-s_srs：清單選了就照選的 (有標籤也照它)；沒選時照標籤 (TWD67 標籤改用七參數)，沒有標籤依範圍推測。"""
    forced = resolve_assigned_srs(chosen)
    if forced:
        if text_callback:
            text_callback(f"[座標系] {name.name}：照清單選的來源座標系 ({chosen}) 轉換。\n")
        return forced
    try:
        return decide_and_report_source_srs(path, None, text_callback=text_callback).s_srs
    except SourceSrsUndeterminedError as err:
        raise ToolkitError(NO_SRS_REASON) from err


def _dxf_stage_args(cad_layer: Optional[str]) -> List[str]:
    """DXF 正規化的 ogr2ogr 參數：只取這個 CAD 圖層 (None＝整個檔)，只留 Layer、Text、轉成 2D。"""
    args: List[str] = []
    if cad_layer:
        args.extend(["-where", f"Layer = {geo_io.quote_literal(cad_layer)}"])
    # -nomd：不帶 DXF 檔頭變數；圖塊 (幾何集合) 先拆開，才分得進點、線、面
    args.extend(["-dim", "XY", "-select", ",".join(DXF_KEPT_FIELDS), "-nomd", "-explodecollections"])
    return args


def _record_skip(result: QuickViewExport, label: str, title: str, reason: object, text_callback: TextCallback) -> None:
    """某個圖層 (或整個檔) 沒有存：記進 result.skipped 與日誌。label 是「另存 SHP」等。"""
    logger.warning("%s「%s」略過: %s", label, title, reason)
    result.skipped.append(f"{title}：{reason}")
    if text_callback:
        text_callback(f"[{label}] 「{title}」略過：{reason}\n")


def _export_unit(
    path: Path,
    name: Path,
    cad_layer: Optional[str],
    s_srs: Optional[str],
    t_srs: str,
    encoding: str,
    closed_lines_as_polygons: bool,
    names: _Names,
    temp_files: geo_io.TempVectorFiles,
    text_callback: TextCallback,
    data_layer: Optional[str] = None,
) -> List[Path]:
    """一個圖資 (或 DXF 的一個 CAD 圖層、檔案裡的一個圖層)：轉到輸出座標系的暫存 GPKG，再依幾何型別存成一或多個 SHP。"""
    is_dxf = path.suffix.lower() == ".dxf"
    title = unit_title(name, cad_layer or data_layer)
    staged = geo_io.stage_vector_to_gpkg(
        path, temp_files.new("stage"), source_layer=data_layer, source_srs=s_srs, target_srs=t_srs,
        extra_args=_dxf_stage_args(cad_layer) if is_dxf else [], text_callback=text_callback,
    )
    if staged.feature_count == 0:
        raise ToolkitError("沒有圖徵")
    layer_label = cad_layer or data_layer
    base = f"{name.stem}_{file_label(layer_label)}" if layer_label else name.stem

    single = None if is_dxf else _single_kind(staged.geometry_type)
    if single is not None:
        out = names.claim(base)
        geo_io.export_vector(staged.path, staged.layer_name, out, shp_encoding=encoding, text_callback=text_callback)
        return [out]

    fields = geo_io.build_field_select_list(staged)
    closed = closed_lines_as_polygons and is_dxf
    parts: List[Tuple[str, Path, int]] = []
    for kind, geometry_args in _GEOMETRY_ARGS.items():
        part = temp_files.new(kind)
        extra = [*geometry_args, "-a_srs", t_srs]
        if closed and kind == _KIND_POLY:
            extra.append("-makevalid")
        geo_io.run_sql_to_gpkg(staged.path, _kind_sql(kind, fields, closed), part, "stage", extra_args=extra)
        count = geo_io.read_layer_info(part, "stage").feature_count if part.exists() else 0
        if count:
            parts.append((kind, part, count))
    if not parts:
        raise ToolkitError("沒有點、線、面圖徵 (SHP 只能存這三種)")

    outputs: List[Path] = []
    # DXF 一律加型別尾碼；其他格式只有一種有資料時照原檔名
    with_suffix = is_dxf or len(parts) > 1
    for kind, part, _count in parts:
        out = names.claim(f"{base}_{kind}" if with_suffix else base)
        geo_io.export_vector(part, "stage", out, shp_encoding=encoding, text_callback=text_callback)
        outputs.append(out)
    if text_callback:
        summary = "、".join(f"{_KIND_TEXT[kind]} {count:,} 個" for kind, _part, count in parts)
        text_callback(f"[另存 SHP] 「{title}」依點、線、面分開存：{summary}。\n")
        # 多點拆成單點後筆數會變多，算出來不是正數時就不提
        skipped = staged.feature_count - sum(count for _kind, _part, count in parts)
        if skipped > 0:
            text_callback(f"[注意] 「{title}」有 {skipped:,} 個圖徵不是點、線、面 (或封閉線圍不出面)，沒有存。\n")
    return outputs


def export_to_shapefiles(
    sources: Sequence[ExportSource],
    output_dir: Union[str, Path],
    *,
    target_srs: str = DEFAULT_TARGET_SRS,
    encoding: Optional[str] = None,
    closed_lines_as_polygons: bool = False,
    progress_callback: ProgressCallback = None,
    text_callback: TextCallback = None,
) -> QuickViewExport:
    """把向量圖資另存成 SHP：照 sources 的順序，DXF 的每個 CAD 圖層各自存。

    target_srs：輸出座標系，ASSIGNABLE_SOURCE_SRS 的鍵 (例如 "TWD67_121") 或 EPSG／PROJ 字串；不接受經緯度。
    encoding：UTF-8 (預設，附 .cpg) 或 CP950。
    closed_lines_as_polygons：DXF 首尾相接的線存成面 (其他格式不受影響)。
    某一層做不出來只略過並記下原因 (QuickViewExport.skipped，也寫進日誌)；全部都做不出來時拋 ToolkitError。
    """
    out_dir = Path(output_dir).resolve()
    t_srs = resolve_assigned_srs(target_srs)
    if not t_srs:
        raise ToolkitError("沒有指定輸出座標系")
    if srs_is_geographic(t_srs):
        raise ToolkitError("輸出座標系不開放經緯度，請選 TWD97 或 TWD67 的二度分帶")
    shp_encoding = geo_io.normalize_shp_encoding(encoding)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = QuickViewExport(output_dir=out_dir)
    names = _Names(out_dir, sources)
    total = len(sources)

    def _skip(title: str, reason: object) -> None:
        _record_skip(result, "另存 SHP", title, reason, text_callback)

    with geo_io.TempVectorFiles("viewshp") as temp_files:
        for index, source in enumerate(sources):
            path = Path(source.path).resolve()
            name = Path(source.title or path.name)
            callback = rename_in_messages(text_callback, path.name, name.name)
            if progress_callback:
                progress_callback(100.0 * index / max(total, 1), f"[{index + 1}/{total}] {name.name}：另存 SHP")
            try:
                s_srs = _decide_source_srs(path, name, source.source_srs, callback)
            except CommandCancelledError:
                raise
            except Exception as err:
                _skip(name.name, err)
                continue
            for data_layer, cad_layer in source.units():
                title = unit_title(name, cad_layer or data_layer)
                try:
                    result.output_files.extend(
                        _export_unit(
                            path, name, cad_layer, s_srs, t_srs, shp_encoding, closed_lines_as_polygons, names, temp_files,
                            callback, data_layer,
                        )
                    )
                except CommandCancelledError:
                    raise
                except Exception as err:
                    _skip(title, err)

    if not result.output_files:
        reasons = "\n".join(result.skipped) or "沒有勾選的向量圖資"
        raise ToolkitError(f"勾選的圖層都沒有存成 SHP：\n{reasons}")
    if text_callback:
        text_callback(
            f"[另存 SHP] 完成：{len(result.output_files)} 個 SHP ({target_srs}，{shp_encoding})，輸出到 {out_dir}。\n"
        )
    return result


# -----------------------------------------------------------------------------
# 另存 KML／KMZ
# -----------------------------------------------------------------------------

# Placemark 裡排在 styleUrl 前面的元素 (KML 2.2 的元素順序)
_BEFORE_STYLE_URL: frozenset = frozenset(
    {"name", "visibility", "open", "address", "phoneNumber", "Snippet", "description", "LookAt", "Camera", "TimeStamp", "TimeSpan"}
)


def kml_name_field(field_names: Sequence[str]) -> Optional[str]:
    """當標記名的欄位：照 KML_NAME_FIELDS 的順序找 (不分大小寫)；都沒有時 None。"""
    by_key: Dict[str, str] = {}
    for field_name in field_names:
        by_key.setdefault(field_name.casefold(), field_name)
    return next((by_key[wanted.casefold()] for wanted in KML_NAME_FIELDS if wanted.casefold() in by_key), None)


def _kml_tag(name: str) -> str:
    return f"{{{_KML_NAMESPACE}}}{name}"


def _kml_color(hex_rgb: str, opacity: float = 1.0) -> str:
    """#RRGGBB → KML 的 aabbggrr。"""
    red, green, blue = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"{round(opacity * 255):02x}{blue}{green}{red}".lower()


def _kml_style(style_id: str, outline: str, fill: str) -> ET.Element:
    """一個資料夾的樣式：線與面的外框用 outline 色，面填 fill 色 (半透明，同瀏覽器地圖)。點維持 Google Earth 的圖釘。"""
    style = ET.Element(_kml_tag("Style"), id=style_id)
    line_style = ET.SubElement(style, _kml_tag("LineStyle"))
    ET.SubElement(line_style, _kml_tag("color")).text = _kml_color(outline)
    ET.SubElement(line_style, _kml_tag("width")).text = str(KML_LINE_WIDTH)
    poly_style = ET.SubElement(style, _kml_tag("PolyStyle"))
    ET.SubElement(poly_style, _kml_tag("color")).text = _kml_color(fill, VECTOR_FILL_OPACITY)
    return style


def _stage_kml_unit(
    path: Path,
    cad_layer: Optional[str],
    data_layer: Optional[str],
    s_srs: Optional[str],
    closed_lines_as_polygons: bool,
    combined: Path,
    layer_id: str,
    temp_files: geo_io.TempVectorFiles,
    text_callback: TextCallback,
) -> int:
    """一個圖資 (或 DXF 的一個 CAD 圖層、檔案裡的一個圖層) 轉成 WGS84，加進 combined (GPKG) 的 layer_id 圖層：
    標記名欄位改名成 _KML_NAME_COLUMN，DXF 可選封閉線轉面。回傳圖徵數。"""
    is_dxf = path.suffix.lower() == ".dxf"
    staged = geo_io.stage_vector_to_gpkg(
        path, temp_files.new("stage"), source_layer=data_layer, source_srs=s_srs, target_srs=KML_SRS,
        extra_args=_dxf_stage_args(cad_layer) if is_dxf else [], text_callback=text_callback,
    )
    if staged.feature_count == 0:
        raise ToolkitError("沒有圖徵")
    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    expression = geom
    closed = closed_lines_as_polygons and is_dxf
    if closed:
        # ST_IsClosed 只認單一條線 (其他型別回傳 -1)；圍不出面的 (ST_BuildArea 是 NULL) 維持原本的線
        is_closed_line = f"ST_GeometryType({geom}) LIKE '%LINESTRING%' AND COALESCE(ST_IsClosed({geom}), 0) = 1"
        expression = f"CASE WHEN {is_closed_line} THEN COALESCE(ST_BuildArea({geom}), {geom}) ELSE {geom} END"
    name_field = kml_name_field(staged.field_names)
    columns = [f"{expression} AS {geom}"]
    for field_name in staged.field_names:
        ident = geo_io.quote_ident(field_name)
        columns.append(f"{ident} AS {geo_io.quote_ident(_KML_NAME_COLUMN)}" if field_name == name_field else ident)
    cmd: List[str] = ["ogr2ogr", "-f", "GPKG"]
    if combined.exists():
        cmd.append("-update")
    cmd.extend([
        str(combined), str(staged.path), "-dialect", "sqlite", "-sql", f'SELECT {", ".join(columns)} FROM "stage"',
        "-nln", layer_id, "-nlt", "GEOMETRY", "-lco", f"GEOMETRY_NAME={geo_io.STAGE_GEOMETRY_COLUMN}",
    ])
    if closed:
        cmd.append("-makevalid")
    run_command(cmd, text_callback=text_callback, check=True)
    return staged.feature_count


def _write_kml(
    combined: Path,
    folders: Sequence[Tuple[str, str]],
    out: Path,
    kmz: bool,
    temp_files: geo_io.TempVectorFiles,
    text_callback: TextCallback,
) -> None:
    """combined 的圖層 [(layer_id, 資料夾名)] 照順序寫成一個 KML：資料夾名換回圖層名、每個資料夾一種顏色；
    kmz 時壓成 KMZ (裡面是 doc.kml)。同名的檔先刪掉。"""
    raw = temp_files.new("kml", ".kml")
    run_command(
        [
            "ogr2ogr", "-f", "LIBKML", str(raw), str(combined), *(layer_id for layer_id, _title in folders),
            "-lco", "FOLDER=YES", "--config", "LIBKML_NAME_FIELD", _KML_NAME_COLUMN,
        ],
        text_callback=text_callback,
        check=True,
    )
    root = ET.parse(raw).getroot()
    document = root.find(_kml_tag("Document"))
    if document is None:
        raise ToolkitError("KML 寫出失敗 (沒有 Document)")
    folder_by_id = {folder.get("id"): folder for folder in document.iter(_kml_tag("Folder"))}
    styles: List[ET.Element] = []
    for index, (layer_id, title) in enumerate(folders):
        folder = folder_by_id.get(layer_id)
        if folder is None:
            continue
        name = folder.find(_kml_tag("name"))
        if name is None:
            name = ET.Element(_kml_tag("name"))
            folder.insert(0, name)
        name.text = title
        style_id = f"{layer_id}.style"
        styles.append(_kml_style(style_id, *_PREVIEW_LAYER_COLORS[index % len(_PREVIEW_LAYER_COLORS)]))
        for placemark in folder.iter(_kml_tag("Placemark")):
            position = next(
                (i for i, child in enumerate(placemark) if child.tag.rsplit("}", 1)[-1] not in _BEFORE_STYLE_URL),
                len(placemark),
            )
            style_url = ET.Element(_kml_tag("styleUrl"))
            style_url.text = f"#{style_id}"
            placemark.insert(position, style_url)
    # 樣式放在 Document 最前面 (KML 的元素順序：Style 在 Schema 與資料夾前面)
    for position, style in enumerate(styles):
        document.insert(position, style)
    ET.indent(root, space="  ")
    # 寫成預設命名空間 (不加 ns0:)；tostring 的 default_namespace 遇到沒有命名空間的屬性 (id、name) 會出錯
    ET.register_namespace("", _KML_NAMESPACE)
    data = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    geo_io.remove_vector_file(out)
    if kmz:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(KMZ_DOC_NAME, data)
    else:
        out.write_bytes(data)


def export_to_kml(
    sources: Sequence[ExportSource],
    output_dir: Union[str, Path],
    *,
    kmz: bool = False,
    closed_lines_as_polygons: bool = False,
    progress_callback: ProgressCallback = None,
    text_callback: TextCallback = None,
) -> QuickViewExport:
    """把向量圖資另存成 KML (kmz=True 時 KMZ)：每個來源檔一個，檔裡勾選的圖層 (DXF 的 CAD 圖層等) 各一個資料夾。

    座標系固定 WGS84 經緯度、UTF-8；來源座標系的決定同另存 SHP。
    closed_lines_as_polygons：DXF 首尾相接的線存成面 (其他格式不受影響)。
    某一層做不出來只略過並記下原因 (一個檔的圖層都做不出來時整個檔不存)；全部都做不出來時拋 ToolkitError。
    """
    out_dir = Path(output_dir).resolve()
    kind = "KMZ" if kmz else "KML"
    label = f"另存 {kind}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = QuickViewExport(output_dir=out_dir)
    names = _Names(out_dir, sources, f".{kind.lower()}")
    total = len(sources)

    def _skip(title: str, reason: object) -> None:
        _record_skip(result, label, title, reason, text_callback)

    with geo_io.TempVectorFiles("viewkml") as temp_files:
        for index, source in enumerate(sources):
            path = Path(source.path).resolve()
            name = Path(source.title or path.name)
            callback = rename_in_messages(text_callback, path.name, name.name)
            if progress_callback:
                progress_callback(100.0 * index / max(total, 1), f"[{index + 1}/{total}] {name.name}：{label}")
            try:
                s_srs = _decide_source_srs(path, name, source.source_srs, callback)
            except CommandCancelledError:
                raise
            except Exception as err:
                _skip(name.name, err)
                continue
            combined = temp_files.new(f"layers{index}")
            folders: List[Tuple[str, str]] = []
            count = 0
            for number, (data_layer, cad_layer) in enumerate(source.units(), start=1):
                layer_label = cad_layer or data_layer
                layer_id = f"layer_{number}"
                try:
                    count += _stage_kml_unit(
                        path, cad_layer, data_layer, s_srs, closed_lines_as_polygons, combined, layer_id, temp_files, callback
                    )
                except CommandCancelledError:
                    raise
                except Exception as err:
                    _skip(unit_title(name, layer_label), err)
                    continue
                folders.append((layer_id, layer_label or name.stem))
            if not folders:
                continue
            out = names.claim(name.stem)
            try:
                _write_kml(combined, folders, out, kmz, temp_files, callback)
            except CommandCancelledError:
                raise
            except Exception as err:
                _skip(name.name, err)
                continue
            result.output_files.append(out)
            if text_callback:
                text_callback(f"[{label}] 「{name.name}」→ {out.name}：{len(folders)} 個資料夾、{count:,} 個圖徵。\n")

    if not result.output_files:
        reasons = "\n".join(result.skipped) or "沒有勾選的向量圖資"
        raise ToolkitError(f"勾選的圖層都沒有存成 {kind}：\n{reasons}")
    if text_callback:
        text_callback(
            f"[{label}] 完成：{len(result.output_files)} 個 {kind} (WGS84 經緯度，{KML_ENCODING})，輸出到 {out_dir}。\n"
        )
    return result
