"""向量與 CAD 深度工具箱模組 (core/vector_ops.py)

嚴格遵守專案規格書第 0 節與第 3.3 節鐵律：
1. 嚴禁使用 geopandas, rasterio, shapely, pyproj, arcpy。
2. 允許使用：openpyxl (用地清冊 Excel 報表匯出)。
3. [G-07] 多圖幅批次合併 (ogrmerge.py)。
4. [G-10] 密集頂點抽稀 (-simplify) 與 Shapefile 中文 Big5 / CP950 / UTF-8 亂碼自動修復。
5. [G-11] 座標表格 (.csv / .txt) 轉空間點位 / 線段 (OGR CSV 驅動)。
6. [A-05] 地籍/分區空間交集 (Intersection) 與 Excel 面積清冊報表輸出。
7. [A-07] 向量圖層依屬性欄位自動拆檔 (Split by Attribute)。
"""

from __future__ import annotations

import csv
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# 允許使用 openpyxl 產生地籍與工程清冊
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core import csv_points, geo_io
from core.clip_master import (
    CENTROID_X_FIELD,
    CENTROID_Y_FIELD,
    ORIG_AREA_FIELD,
    RATIO_FIELD,
    clip_vector_to_metrics_gpkg,
)
from core.crs_doctor import EPSG_TWD97_121, EPSG_WGS84, decide_and_report_source_srs, decide_source_srs
from core.env_runner import (
    CommandResult,
    ToolkitError,
    get_workspace_temp_dir,
    resolve_binary,
    run_command,
    run_json_command,
)

logger = logging.getLogger("core.vector_ops")

# 地籍交集成果的劃入面積欄位 (平方公尺 / 公頃)
CADASTRAL_CLIP_AREA_FIELD: str = "CLIP_M2"
CADASTRAL_CLIP_HA_FIELD: str = "CLIP_HA"

# 清冊尋找地段與地號欄位時依序比對的欄位名 (不分大小寫)
_SECTION_FIELD_CANDIDATES: Tuple[str, ...] = ("SECT", "SECTION", "段名", "段號")
_LAND_NO_FIELD_CANDIDATES: Tuple[str, ...] = ("LANDNO", "PARCEL_NO", "地號", "LOT_NO")

# 拆檔支援的輸出格式與副檔名
_SPLIT_OUTPUT_SUFFIXES: Dict[str, str] = {
    "ESRI Shapefile": ".shp",
    "GPKG": ".gpkg",
    "GeoJSON": ".geojson",
    "DXF": ".dxf",
}
# 拆檔可用的欄位型別 (日期、清單等型別的相等條件寫法因驅動而異，不支援)
_SPLIT_FIELD_TYPES: Tuple[str, ...] = ("String", "Integer", "Integer64", "Real")


# DXF 依幾何型別分層：輸出圖層名 → (OGR_GEOMETRY 條件值, ogr2ogr 的輸出幾何參數)
_DXF_GEOMETRY_LAYERS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "points": (("POINT", "MULTIPOINT"), ("-explodecollections", "-nlt", "POINT")),
    "lines": (("LINESTRING", "MULTILINESTRING"), ("-nlt", "MULTILINESTRING")),
    "polygons": (("POLYGON", "MULTIPOLYGON"), ("-nlt", "MULTIPOLYGON")),
}
# 分層時保留的 DXF 欄位 (CAD 圖層名與文字內容)
_DXF_KEPT_FIELDS: Tuple[str, ...] = ("Layer", "Text")


@dataclass
class DxfGeometrySplitResult:
    """DXF 依點、線、面分層的成果。"""

    output_gpkg: Path
    # 輸出圖層名 (points / lines / polygons) → 圖徵數；沒有圖徵的型別不會出現
    layer_feature_counts: Dict[str, int]
    # 不是點、線、面 (例如幾何集合) 而被略過的圖元數
    skipped_feature_count: int
    duration_seconds: float


@dataclass
class CadastralIntersectionResult:
    """地籍空間交集報表計算成果結構。"""

    clipped_vector_path: Path
    excel_report_path: Path
    total_parcels_count: int
    total_clipped_area_m2: float
    total_clipped_area_ha: float
    duration_seconds: float
    total_original_area_m2: float = 0.0


# ============================================================================
# [G-07] 多圖幅批次合併 (Merge via ogrmerge)
# ============================================================================

def merge_vector_layers(
    input_files: Sequence[Union[str, Path]],
    output_path: Union[str, Path],
    *,
    single_layer: bool = True,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 ogrmerge 將多個向量圖資 (.shp, .gpkg) 合併為單一圖層檔案。

    核心規範（規格書 3.3 節）：
    ogrmerge.py -single -f "GPKG" -t_srs EPSG:3826 -o merged.gpkg *.shp

    實作方式：
    1. 每個來源先各自正規化成暫存 GPKG 並重投影到 target_srs。
    2. 以沙盒的 python -m osgeo_utils.ogrmerge 合併成暫存 GPKG (runtime/Scripts 的 ogrmerge.exe 啟動器
       在可攜環境下無法執行；ogrmerge 直接寫 Shapefile 也會把中文欄位名切成殘缺字元)。
    3. 單一圖層模式再匯出成 output_path 副檔名對應的格式；多圖層模式 (single_layer=False) 只能輸出 GPKG。

    來源各自判斷座標系：有座標系的用自己的；沒有的才採用 assigned_srs，未指派則依座標範圍推測。
    """
    missing = [str(f) for f in input_files if not Path(f).exists()]
    if missing:
        raise FileNotFoundError("以下欲合併的圖資不存在: " + "、".join(missing))
    valid_inputs = [Path(f).resolve() for f in input_files]
    if not valid_inputs:
        raise ValueError("提供的欲合併圖資清單為空！")

    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    if not single_layer and geo_io.infer_vector_driver(out_p) != "GPKG":
        raise ToolkitError("多圖層合併 (single_layer=False) 只能輸出 GeoPackage (.gpkg)。")

    if text_callback:
        text_callback(f"[向量合併] 正在合併 {len(valid_inputs)} 個圖層至 {out_p.name}。\n")

    with geo_io.TempVectorFiles("merge") as tmp:
        staged_paths: List[Path] = []
        used_layer_names: set = set()
        stage_share = 70.0 / len(valid_inputs)
        for idx, src in enumerate(valid_inputs):
            # 多圖層模式以來源檔名當圖層名，重名時加上序號
            stage_layer = re.sub(r"[^\w]+", "_", src.stem).strip("_") or f"layer_{idx + 1}"
            if stage_layer.casefold() in used_layer_names:
                stage_layer = f"{stage_layer}_{idx + 1}"
            used_layer_names.add(stage_layer.casefold())

            srs_decision = decide_and_report_source_srs(src, assigned_srs, text_callback=text_callback)
            staged = geo_io.stage_vector_to_gpkg(
                src,
                tmp.new(f"src{idx}"),
                stage_layer=stage_layer,
                target_srs=target_srs,
                source_srs=srs_decision.s_srs,
                progress_callback=geo_io.scaled_progress(progress_callback, idx * stage_share, (idx + 1) * stage_share),
                text_callback=text_callback,
            )
            staged_paths.append(staged.path)

        python_exe = resolve_binary("python")
        geom_option = f"GEOMETRY_NAME={geo_io.STAGE_GEOMETRY_COLUMN}"
        if single_layer:
            merged_gpkg = tmp.new("merged")
            cmd = [
                python_exe, "-m", "osgeo_utils.ogrmerge",
                "-single",
                "-f", "GPKG",
                "-o", str(merged_gpkg),
                "-nln", "merged",
                "-lco", geom_option,
                *[str(p) for p in staged_paths],
            ]
            run_command(
                cmd,
                progress_callback=geo_io.scaled_progress(progress_callback, 70.0, 90.0),
                text_callback=text_callback,
                check=True,
            )
            geo_io.export_vector(
                merged_gpkg,
                "merged",
                out_p,
                progress_callback=geo_io.scaled_progress(progress_callback, 90.0, 100.0),
                text_callback=text_callback,
            )
        else:
            cmd = [
                python_exe, "-m", "osgeo_utils.ogrmerge",
                "-f", "GPKG",
                "-overwrite_ds",
                "-o", str(out_p),
                "-nln", "{LAYER_NAME}",
                "-lco", geom_option,
                *[str(p) for p in staged_paths],
            ]
            run_command(
                cmd,
                progress_callback=geo_io.scaled_progress(progress_callback, 70.0, 100.0),
                text_callback=text_callback,
                check=True,
            )

    if not out_p.exists():
        raise ToolkitError(f"向量合併失敗，未產生輸出檔: {out_p}")
    return out_p


# ============================================================================
# [G-10] 密集頂點抽稀與中文 Big5 / UTF-8 亂碼修復
# ============================================================================

def simplify_vector_geometry(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    tolerance: float = 1.0,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 ogr2ogr -simplify 對過於密集之多邊形或線段進行頂點抽稀瘦身。

    來源有座標系就用來源的；沒有時才採用 assigned_srs，未指派則依座標範圍推測。
    """
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not in_p.exists():
        raise FileNotFoundError(f"輸入圖資不存在: {in_p}")

    srs_decision = decide_and_report_source_srs(in_p, assigned_srs, text_callback=text_callback)

    with geo_io.TempVectorFiles("simplify") as temp_files:
        staged_gpkg = temp_files.new("stage")
        geo_io.stage_vector_to_gpkg(
            in_p,
            staged_gpkg,
            target_srs=target_srs,
            source_srs=srs_decision.s_srs,
            make_valid=True,
            text_callback=text_callback,
        )
        # ogr2ogr 的 -simplify 在重投影之前執行，容許值會以來源單位計算 (經緯度來源就是「度」)，
        # 所以先重投影到 target_srs，匯出時才抽稀，tolerance 才是目標座標系的單位 (公尺)
        geo_io.export_vector(
            staged_gpkg, "stage", out_p, extra_args=["-simplify", str(tolerance)], text_callback=text_callback
        )
    return out_p


def repair_shapefile_encoding(
    shapefile_path: Union[str, Path],
    *,
    source_encoding: Optional[str] = None,
    output_path: Optional[Union[str, Path]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """把 Big5/CP950 (或編碼標示錯誤) 的 Shapefile 轉存成帶 .cpg 的 UTF-8 Shapefile，來源檔完全不改動。

    作法：
    1. source_encoding 未指定時自動判定：有 .cpg 照 .cpg；沒有 .cpg 或 .cpg 標錯時依 DBF 內容判斷。
    2. 以 -oo ENCODING=<來源編碼> 讀進暫存 GPKG，再以 -lco ENCODING=UTF-8 寫出新檔。
    """
    in_shp = Path(shapefile_path).resolve()
    if not in_shp.exists() or in_shp.suffix.lower() != ".shp":
        raise ValueError(f"請提供有效的 Shapefile (.shp) 檔案: {in_shp}")

    out_shp = Path(output_path).resolve() if output_path else in_shp.parent / f"{in_shp.stem}_utf8.shp"
    if out_shp == in_shp:
        raise ValueError(f"輸出檔不可與來源檔相同: {in_shp}")

    used_encoding = geo_io.resolve_source_encoding(in_shp, source_encoding)
    with geo_io.TempVectorFiles("repair_enc") as temp_files:
        staged_gpkg = temp_files.new("stage")
        geo_io.stage_vector_to_gpkg(in_shp, staged_gpkg, source_encoding=source_encoding, text_callback=text_callback)
        geo_io.export_vector(staged_gpkg, "stage", out_shp, shp_encoding="UTF-8", text_callback=text_callback)

    if text_callback:
        source_label = used_encoding or "依 .cpg／DBF 檔頭"
        text_callback(f"[編碼修復] 已以 {source_label} 讀取來源，輸出至 {out_shp.name} (CPG: UTF-8)。\n")

    return out_shp


# ============================================================================
# [G-11] 座標表格 (.csv / .txt) 轉空間點位圖資
# ============================================================================

def convert_csv_table_to_points(
    csv_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    x_column_pattern: str = csv_points.X_COLUMN_PATTERN,
    y_column_pattern: str = csv_points.Y_COLUMN_PATTERN,
    source_srs: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """使用 GDAL OGR CSV 驅動將純文字試算表高速轉為點位向量圖資。

    流程：
    1. 以 X_POSSIBLE_NAMES / Y_POSSIBLE_NAMES 把座標欄位讀成點，先存成沒有座標系的暫存 GPKG。
    2. 表格沒有座標系標籤：採用 assigned_srs (source_srs 為舊參數名，同義)，未指派則依座標範圍推測。
    3. 重投影到 target_srs 後匯出成 output_path 副檔名對應的格式。

    表格可以是 UTF-8、UTF-16 (帶 BOM) 或 Big5 (CP950)，副檔名 .csv／.txt 皆可，分隔符號由 GDAL 自動判斷
    (步驟 1 在 core/csv_points.py，圖資快覽也用它)。
    """
    in_csv = Path(csv_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not in_csv.exists():
        raise FileNotFoundError(f"找不到座標表格: {in_csv}")

    if text_callback:
        text_callback(f"[表格轉點] 正在將座標表格 {in_csv.name} 轉為空間圖資。\n")

    with geo_io.TempVectorFiles("csv_points") as temp_files:
        raw_gpkg = temp_files.new("raw")
        encoding_label = csv_points.table_to_raw_points(
            in_csv, raw_gpkg, x_column_pattern=x_column_pattern, y_column_pattern=y_column_pattern, text_callback=text_callback
        )
        if text_callback and encoding_label != "UTF-8":
            text_callback(f"[表格轉點] {in_csv.name} 的文字編碼判定為 {encoding_label}，已轉成 UTF-8 讀取。\n")

        srs_decision = decide_source_srs(raw_gpkg, assigned_srs or source_srs, layer_name=csv_points.POINTS_LAYER)
        if text_callback:
            prefix = "[注意] " if srs_decision.is_guess else "[座標系] "
            text_callback(f"{prefix}{srs_decision.message.replace(raw_gpkg.name, in_csv.name)}\n")

        staged = geo_io.stage_vector_to_gpkg(
            raw_gpkg,
            temp_files.new("stage"),
            source_layer="points",
            stage_layer="points",
            target_srs=target_srs,
            source_srs=srs_decision.s_srs,
            text_callback=text_callback,
        )
        geo_io.export_vector(staged.path, "points", out_p, text_callback=text_callback)
    return out_p


# ============================================================================
# [A-05] 地籍/分區空間交集與工程用地 Excel 清冊輸出
# ============================================================================

def _first_attribute_text(props: Dict[str, Any], candidates: Sequence[str]) -> str:
    """依候選欄位名順序 (不分大小寫) 取第一個非空白的屬性值，找不到時回傳空字串。"""
    by_folded_name = {str(name).casefold(): value for name, value in props.items()}
    for candidate in candidates:
        value = by_folded_name.get(candidate.casefold())
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def generate_cadastral_intersection_report(
    master_boundary_path: Union[str, Path],
    cadastral_shapefile_path: Union[str, Path],
    *,
    output_dir: Optional[Union[str, Path]] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> CadastralIntersectionResult:
    """執行 [A-05] 將工程邊界與地籍圖執行空間交集，並以 openpyxl 輸出工程用地徵收清冊。

    計算欄位：
    - ORIG_M2: 每筆地號之原始總面積
    - CLIP_M2: 劃入工區之徵收/佔用精確面積
    - RATIO_PCT: 劃入工區佔比 (CLIP_M2 / ORIG_M2 * 100%)
    - 輸出帶標題、千分位數字、背景填色與總計列之標準工程用地清冊 (.xlsx)。

    地籍圖有座標系就用自己的；沒有時才採用 assigned_srs，未指派則依座標範圍推測。
    """
    start_time = time.time()
    bound_p = Path(master_boundary_path).resolve()
    cad_p = Path(cadastral_shapefile_path).resolve()
    temp_dir = get_workspace_temp_dir()
    out_base = Path(output_dir).resolve() if output_dir else temp_dir
    out_base.mkdir(parents=True, exist_ok=True)

    if not bound_p.exists() or not cad_p.exists():
        raise FileNotFoundError("工程邊界檔或地籍圖檔案不存在！")

    out_clipped_gpkg = out_base / f"{cad_p.stem}_intersected_parcels.gpkg"
    out_excel = out_base / f"{cad_p.stem}_工程用地清冊報表.xlsx"

    if text_callback:
        text_callback(f"[地籍交集] 正在執行空間交集運算 (地籍圖: {cad_p.name})。\n")

    # 1～2. 重投影後先記下每筆宗地的原始面積，再以工程邊界裁切並計算劃入面積、比例與形心
    #       (面積必須在裁切完成後才計算，否則得到的是裁切前的整筆面積)
    with geo_io.TempVectorFiles("cadastral") as tmp:
        result_info = clip_vector_to_metrics_gpkg(
            cad_p,
            bound_p,
            tmp,
            target_srs=target_srs,
            assigned_srs=assigned_srs,
            area_field=CADASTRAL_CLIP_AREA_FIELD,
            hectare_field=CADASTRAL_CLIP_HA_FIELD,
            progress_callback=geo_io.scaled_progress(progress_callback, 0.0, 70.0),
            text_callback=text_callback,
        )
        geo_io.export_vector(
            result_info.path,
            result_info.layer_name,
            out_clipped_gpkg,
            output_layer_name=out_clipped_gpkg.stem,
            progress_callback=geo_io.scaled_progress(progress_callback, 70.0, 80.0),
            text_callback=text_callback,
        )

    if progress_callback:
        progress_callback(80.0, "空間交集完成，正在統計屬性與匯出 Excel 清冊。")

    # 3. 讀取裁切後之屬性資料
    features = geo_io.read_attribute_rows(out_clipped_gpkg, out_clipped_gpkg.stem)
    if not features and text_callback:
        text_callback("[注意] 地籍圖與工程邊界沒有交集，清冊內沒有任何地號。\n")

    # 4. 利用 openpyxl 建立標準工程用地清冊報表
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "工程用地徵收清冊"

    title_font = Font(name="Microsoft JhengHei UI", size=13, bold=True, color="1F497D")
    header_font = Font(name="Microsoft JhengHei UI", size=10, bold=True, color="FFFFFF")
    data_font = Font(name="Microsoft JhengHei UI", size=9)
    total_font = Font(name="Microsoft JhengHei UI", size=10, bold=True, color="9C0006")

    fill_header = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    fill_total = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    ws.merge_cells("A1:I1")
    ws["A1"] = "工程用地劃入範圍地籍清冊與面積統計表"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    ws["A2"] = f"圖資來源: {cad_p.name} | 基準座標系: {target_srs}"
    ws["A2"].font = Font(name="Microsoft JhengHei UI", size=8, italic=True, color="595959")

    columns = [
        ("序號", 8),
        ("地段代碼/名稱", 18),
        ("地號標示", 16),
        ("原始面積 (m²)", 18),
        ("工區劃入面積 (m²)", 20),
        ("工區劃入面積 (公頃)", 20),
        ("劃入比例 (%)", 14),
        ("形心座標 (X)", 15),
        ("形心座標 (Y)", 15),
    ]
    number_formats = {4: "#,##0.00", 5: "#,##0.00", 6: "#,##0.0000", 7: "0.00", 8: "#,##0.00", 9: "#,##0.00"}

    for c_idx, (col_title, col_width) in enumerate(columns, start=1):
        cell = ws.cell(row=4, column=c_idx, value=col_title)
        cell.font = header_font
        cell.fill = fill_header
        cell.alignment = Alignment(horizontal="center", vertical="center")
        col_letter = get_column_letter(c_idx)
        ws.column_dimensions[col_letter].width = col_width
    ws.row_dimensions[4].height = 24

    total_orig_m2 = 0.0
    total_m2 = 0.0
    total_ha = 0.0

    for idx, props in enumerate(features, start=1):
        r_idx = idx + 4

        # 智慧搜尋地段與地號欄位
        sect_val = _first_attribute_text(props, _SECTION_FIELD_CANDIDATES) or "無段名"
        land_no = _first_attribute_text(props, _LAND_NO_FIELD_CANDIDATES) or f"地號-{idx}"
        orig_m2 = float(props.get(ORIG_AREA_FIELD) or 0.0)
        area_m2 = float(props.get(CADASTRAL_CLIP_AREA_FIELD) or 0.0)
        area_ha = float(props.get(CADASTRAL_CLIP_HA_FIELD) or (area_m2 / 10000.0))
        ratio_pct = float(props.get(RATIO_FIELD) or 0.0)
        cent_x = float(props.get(CENTROID_X_FIELD) or 0.0)
        cent_y = float(props.get(CENTROID_Y_FIELD) or 0.0)

        total_orig_m2 += orig_m2
        total_m2 += area_m2
        total_ha += area_ha

        ws.cell(row=r_idx, column=1, value=idx).alignment = Alignment(horizontal="center")
        ws.cell(row=r_idx, column=2, value=sect_val)
        ws.cell(row=r_idx, column=3, value=land_no)

        for c_i, value in zip(range(4, 10), (orig_m2, area_m2, area_ha, ratio_pct, cent_x, cent_y)):
            cell = ws.cell(row=r_idx, column=c_i, value=value)
            cell.number_format = number_formats[c_i]
            cell.alignment = Alignment(horizontal="right")

        for c_i in range(1, 10):
            ws.cell(row=r_idx, column=c_i).font = data_font
            ws.cell(row=r_idx, column=c_i).border = thin_border

    # 合計列
    tot_row = len(features) + 5
    ws.cell(row=tot_row, column=1, value="合計").alignment = Alignment(horizontal="center")
    ws.cell(row=tot_row, column=2, value=f"共計 {len(features)} 筆地號")
    ws.cell(row=tot_row, column=3, value="")

    total_ratio_pct = round(total_m2 * 100.0 / total_orig_m2, 2) if total_orig_m2 > 0 else 0.0
    for c_i, value in zip(range(4, 8), (total_orig_m2, total_m2, total_ha, total_ratio_pct)):
        cell = ws.cell(row=tot_row, column=c_i, value=value)
        cell.number_format = number_formats[c_i]
        cell.alignment = Alignment(horizontal="right")

    for c_i in range(1, 10):
        c = ws.cell(row=tot_row, column=c_i)
        c.font = total_font
        c.fill = fill_total
        c.border = thin_border

    wb.save(out_excel)
    duration = time.time() - start_time

    if progress_callback:
        progress_callback(100.0, "地籍交集與用地清冊完成。")
    if text_callback:
        text_callback(f"[地籍交集] 用地清冊 Excel 報表匯出完成: {out_excel.name}\n")

    return CadastralIntersectionResult(
        clipped_vector_path=out_clipped_gpkg,
        excel_report_path=out_excel,
        total_parcels_count=len(features),
        total_clipped_area_m2=round(total_m2, 2),
        total_clipped_area_ha=round(total_ha, 4),
        duration_seconds=duration,
        total_original_area_m2=round(total_orig_m2, 2),
    )


# ============================================================================
# [A-07] 向量圖層依屬性欄位自動拆檔 (Split by Attribute)
# ============================================================================

def split_vector_by_attribute(
    input_vector_path: Union[str, Path],
    split_field: str,
    output_directory: Union[str, Path],
    *,
    output_format: str = "ESRI Shapefile",
    assigned_srs: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    """執行 [A-07] 依指定欄位名稱將單一圖層拆分為多個獨立的子圖層檔案。

    流程：
    1. 來源先正規化成暫存 GPKG，以 osgeo 讀出欄位的相異值與各值的圖徵數 (NULL 與空字串各自成一組)。
    2. 逐一以跳脫過的條件式匯出；非 DXF 格式會核對每個輸出檔的圖徵數，不符即中止。

    欄位名比對不分大小寫；只支援文字與數值欄位。
    拆檔不重投影，座標原樣保留；來源沒有座標系標籤時，有傳 assigned_srs 才把它標到輸出檔上，
    沒傳就維持無標籤 (不推測)。
    """
    in_p = Path(input_vector_path).resolve()
    out_dir = Path(output_directory).resolve()

    if not in_p.exists():
        raise FileNotFoundError(f"找不到欲拆檔之向量圖資: {in_p}")
    if output_format not in _SPLIT_OUTPUT_SUFFIXES:
        raise ToolkitError(
            f"拆檔不支援輸出格式「{output_format}」(可用: {'、'.join(_SPLIT_OUTPUT_SUFFIXES)})"
        )
    ext = _SPLIT_OUTPUT_SUFFIXES[output_format]
    out_dir.mkdir(parents=True, exist_ok=True)

    stage_args: List[str] = []
    if assigned_srs:
        srs_decision = decide_and_report_source_srs(in_p, assigned_srs, text_callback=text_callback)
        # 拆檔不重投影：來源自帶座標系時保留原標籤 (含 TWD67 的 EPSG 標籤)
        if srs_decision.s_srs and not srs_decision.has_embedded_srs:
            stage_args = ["-a_srs", srs_decision.s_srs]

    output_files: List[Path] = []
    with geo_io.TempVectorFiles("split") as tmp:
        staged = geo_io.stage_vector_to_gpkg(
            in_p, tmp.new("src"), stage_layer="src", extra_args=stage_args, text_callback=text_callback
        )

        field_info = staged.find_field(split_field)
        if field_info is None:
            raise ToolkitError(
                f"圖層沒有欄位「{split_field}」(現有欄位: {'、'.join(staged.field_names) or '無'})"
            )
        if field_info.type_name not in _SPLIT_FIELD_TYPES:
            raise ToolkitError(
                f"欄位「{field_info.name}」的型別是 {field_info.type_name}，拆檔只支援文字與數值欄位。"
            )

        # 1. 統計該欄位所有相異值 (含 NULL 與空字串) 與各值的圖徵數
        value_counts = geo_io.read_value_counts(staged.path, field_info.name, staged.layer_name)
        if not value_counts:
            raise ToolkitError(f"圖層沒有任何圖徵，無法依欄位 {field_info.name} 拆檔。")

        if text_callback:
            text_callback(f"[自動拆檔] 欄位 {field_info.name} 包含 {len(value_counts)} 個相異屬性值，開始拆檔。\n")

        used_labels: set = set()
        for value, expected_count in value_counts.items():
            label = _unique_split_label(_split_value_label(value), used_labels)
            out_sub_file = out_dir / f"{in_p.stem}_{label}{ext}"

            # 2. 依欄位型別組出條件式 (字串跳脫單引號、數值不加引號、NULL 用 IS NULL)
            geo_io.export_vector(
                staged.path,
                staged.layer_name,
                out_sub_file,
                output_format=output_format,
                where=geo_io.build_equality_filter(field_info, value),
                text_callback=text_callback,
            )

            # DXF 讀回時圖徵會被拆解重組，筆數不具比對意義
            if output_format != "DXF":
                actual_count = geo_io.read_layer_info(out_sub_file).feature_count
                if actual_count != expected_count:
                    raise ToolkitError(
                        f"拆檔結果筆數不符: {out_sub_file.name} 應有 {expected_count} 筆，實際 {actual_count} 筆。"
                    )
            output_files.append(out_sub_file)

    if text_callback:
        text_callback(f"[自動拆檔] 拆檔完成，共產出 {len(output_files)} 個獨立圖層。\n")

    return output_files


# ============================================================================
# [G-05] DXF 依幾何型別 (點、線、面) 分層並強制 2D
# ============================================================================

def split_dxf_by_geometry_type(
    dxf_path: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
    *,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> DxfGeometrySplitResult:
    """把 DXF 的圖元依點、線、面拆成同一個 GPKG 的 points / lines / polygons 三個圖層。

    - 每個型別各跑一次 ogr2ogr -where "OGR_GEOMETRY IN (...)" -dim XY：Z 值一律捨棄，只留 Layer 與 Text 欄位。
    - 沒有圖元的型別不產生圖層；不是點、線、面的圖元 (例如幾何集合) 會被略過並回報個數。
    - 封閉的聚合線在 DXF 裡仍是線，會歸在 lines；要把線圍成面請用 dxf_lines_to_polygon_boundary。
    - DXF 沒有座標系標籤：採用 assigned_srs，未指派則依座標範圍推測，再重投影到 target_srs。
    - 輸出檔為 output_dir (未指定時為暫存目錄) 下的 <DXF 檔名>_layers.gpkg，既有的同名檔會被取代。
    """
    start_time = time.time()
    in_dxf = Path(dxf_path).resolve()
    if not in_dxf.exists():
        raise FileNotFoundError(f"找不到 DXF 檔案: {in_dxf}")
    if in_dxf.suffix.lower() != ".dxf":
        raise ToolkitError(f"分層淨化只接受 DXF 檔案: {in_dxf.name}")

    out_dir = Path(output_dir).resolve() if output_dir else get_workspace_temp_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_gpkg = out_dir / f"{in_dxf.stem}_layers.gpkg"

    total_count = geo_io.read_layer_info(in_dxf).feature_count
    if total_count == 0:
        raise ToolkitError(f"DXF 內沒有任何圖元: {in_dxf.name}")
    srs_decision = decide_and_report_source_srs(in_dxf, assigned_srs, text_callback=text_callback)

    layer_counts: Dict[str, int] = {}
    matched_count = 0
    geo_io.remove_vector_file(out_gpkg)
    with geo_io.TempVectorFiles("dxf_split") as temp_files:
        staged_layers: List[Tuple[str, Path]] = []
        for layer_name, (geometry_names, geometry_args) in _DXF_GEOMETRY_LAYERS.items():
            where = f"OGR_GEOMETRY IN ({', '.join(geo_io.quote_literal(name) for name in geometry_names)})"
            staged = geo_io.stage_vector_to_gpkg(
                in_dxf,
                temp_files.new(layer_name),
                target_srs=target_srs,
                source_srs=srs_decision.s_srs,
                # -nomd：不帶 DXF 檔頭變數，否則 GDAL 會在 GPKG 旁邊另外寫一個 .aux.xml
                extra_args=["-where", where, "-dim", "XY", "-select", ",".join(_DXF_KEPT_FIELDS), "-nomd", *geometry_args],
                text_callback=text_callback,
            )
            if staged.feature_count > 0:
                layer_counts[layer_name] = staged.feature_count
                staged_layers.append((layer_name, staged.path))
                # 多點被拆成單點後筆數會變多，略過數以來源的圖元數計
                matched_count += min(staged.feature_count, total_count) if layer_name == "points" else staged.feature_count

        if not staged_layers:
            raise ToolkitError(f"DXF 內沒有點、線、面圖元可供分層: {in_dxf.name}")
        for layer_name, staged_path in staged_layers:
            geo_io.export_vector(staged_path, "stage", out_gpkg, output_layer_name=layer_name, text_callback=text_callback)

    skipped_count = max(0, total_count - matched_count)
    if text_callback:
        labels = {"points": "點", "lines": "線", "polygons": "面"}
        summary = "、".join(f"{labels[name]} {count} 個" for name, count in layer_counts.items())
        text_callback(f"[DXF 分層] {in_dxf.name} 已分層並轉為 2D: {summary}，輸出至 {out_gpkg.name}。\n")
        if skipped_count:
            text_callback(f"[注意] 有 {skipped_count} 個圖元不是點、線、面 (例如幾何集合)，已略過。\n")

    return DxfGeometrySplitResult(
        output_gpkg=out_gpkg,
        layer_feature_counts=layer_counts,
        skipped_feature_count=skipped_count,
        duration_seconds=time.time() - start_time,
    )


def _split_value_label(value: Any) -> str:
    """把屬性值轉成可放進檔名的標籤。"""
    if value is None:
        return "空值NULL"
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value)
    if not text.strip():
        return "空白"

    # 清理不合法的檔名字元與控制字元；Windows 不允許檔名以點或空白結尾
    text = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", text).strip().rstrip(". ")
    return text[:60] or "空白"


def _unique_split_label(label: str, used_labels: set) -> str:
    """Windows 檔名不分大小寫，標籤重複時依序加上 _2、_3。"""
    candidate = label
    serial = 2
    while candidate.casefold() in used_labels:
        candidate = f"{label}_{serial}"
        serial += 1
    used_labels.add(candidate.casefold())
    return candidate
