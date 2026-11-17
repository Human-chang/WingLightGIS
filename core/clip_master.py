"""三軌平行批次空間裁切引擎 (core/clip_master.py)

嚴格遵守專案規格書第 0 節與第 3.2 節鐵律：
1. 嚴禁使用 geopandas, rasterio, shapely, pyproj, arcpy。
2. 所有空間運算 100% 透過 core.env_runner 呼叫原生 GDAL/OGR C++ CLI 與 SQLite SQL 完成。
3. 軌道 1（地籍/向量）：調用 ogr2ogr 進行 -clipsrc 與 -makevalid，並利用 SQLite SQL
   自動計算並在輸出屬性表末端寫入精確面積（AREA_M2, AREA_HA）、長度 (LEN_M) 與形心座標 (CENT_X, CENT_Y)。
4. 軌道 2（地形 DEM）：支援多圖幅虛擬拼接 (gdalbuildvrt)，使用 gdalwarp 進行 -cutline 裁切，
   強制指定 -r bilinear 或 -r average（嚴禁 near）、-tap 網格整數對齊與 LZW 無損壓縮。
5. 軌道 3（空拍正射影像）：使用 gdalwarp（-r cubic）裁切，產出 .ovr 影像金字塔 (gdaladdo)，
   並透過 gdal_translate 同步輸出 AutoCAD 對位底圖（.jpg + .jgw）。
6. 提供批次排程分流引擎 (BatchClipEngine)，全自動識別檔案類型並回報百分比進度。
"""

from __future__ import annotations

import enum
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from core import geo_io
from core.crs_doctor import (
    EPSG_TWD97_121,
    SourceSrsDecision,
    _read_raster_srs_metadata,
    build_tap_parameters,
    decide_and_report_source_srs,
    get_raster_resolution,
    resolve_warp_resolution,
    srs_is_geographic,
)
from core.env_runner import (
    CommandResult,
    ToolkitError,
    get_workspace_temp_dir,
    run_command,
    run_json_command,
)

logger = logging.getLogger("core.clip_master")

# 裁切流程寫入成果屬性表的計算欄位 (一律英文名，皆在 DBF 欄位名 10 bytes 上限內)
ORIG_AREA_FIELD: str = "ORIG_M2"    # 裁切前面積 (平方公尺)
CLIP_AREA_FIELD: str = "AREA_M2"    # 裁切後面積 (平方公尺)
CLIP_HA_FIELD: str = "AREA_HA"      # 裁切後面積 (公頃)
RATIO_FIELD: str = "RATIO_PCT"      # 裁切後面積占裁切前面積的百分比
LENGTH_FIELD: str = "LEN_M"         # 裁切後周長 (面) 或長度 (線)，公尺
CENTROID_X_FIELD: str = "CENT_X"    # 裁切後形心 X
CENTROID_Y_FIELD: str = "CENT_Y"    # 裁切後形心 Y
# 整筆保留 (keep_whole) 時 ORIG_M2 是整筆面積、AREA_M2／AREA_HA／RATIO_PCT 是範圍內 (交集) 的部分，LEN_M 與形心照整筆算
_WHOLE_KEY_FIELD: str = "_WL_FID"   # 整筆保留時暫存用的來源圖徵編號 (裁切成果接回原圖形)，不會寫進成果
WHOLE_OUTPUT_SUFFIX: str = "_whole"  # 批次裁切整筆保留的成果檔名 <檔名>_whole.shp (一般裁切是 _clipped)


class TrackType(enum.Enum):
    """圖資作業軌道分類列舉。"""

    VECTOR_CADASTRE = "VECTOR_CADASTRE"  # 軌道 1：地籍圖、用地界線、工程線段等向量圖資
    TERRAIN_DEM = "TERRAIN_DEM"          # 軌道 2：數值高程模型 (DEM/DTM) 網格
    ORTHO_AERIAL = "ORTHO_AERIAL"        # 軌道 3：無人機空拍正射影像 (RGB/RGBA GeoTIFF)
    UNKNOWN = "UNKNOWN"                  # 無法判定之格式


@dataclass
class ClipJobResult:
    """單一檔案裁切處理成果結構。"""

    input_path: Path
    output_path: Path
    track_type: TrackType
    is_success: bool
    duration_seconds: float
    error_message: Optional[str] = None
    area_m2: Optional[float] = None
    area_ha: Optional[float] = None
    feature_count: Optional[int] = None
    cad_bg_jpg: Optional[Path] = None
    cad_bg_jgw: Optional[Path] = None
    pyramid_ovr: Optional[Path] = None
    # 向量裁切的屬性 Excel (有要求但沒寫成時為 None，原因記在日誌)
    attribute_excel: Optional[Path] = None
    raster_dims: Optional[Tuple[int, int]] = None
    target_srs: str = f"EPSG:{EPSG_TWD97_121}"
    # 多幅 DEM 拼接成一件成果時的全部來源檔 (此時 input_path 是第一幅)；單幅為 None
    merged_input_paths: Optional[List[Path]] = None


# ============================================================================
# 圖資軌道智慧分類器 (Track Classifier)
# ============================================================================

def detect_track_type(file_path: Union[str, Path]) -> TrackType:
    """依副檔名與內部波段屬性，智慧判定圖資屬於軌道 1 (向量)、軌道 2 (DEM) 或軌道 3 (空拍正射)。

    判定準則：
    1. .shp, .gpkg, .geojson, .dxf, .kml, .kmz, .gpx -> 軌道 1 (向量圖資)
    2. .tif, .dem, .vrt, .asc, .img -> 呼叫 gdalinfo -json 檢查波段：
       - 單波段且數值型態為 Float32 / Float64 / Int16 / Int32 -> 軌道 2 (地形 DEM)
       - 多波段 (>= 3 波段 RGB/RGBA) 或 Byte 色盤 -> 軌道 3 (空拍正射影像)
    3. .jpg, .jpeg, .png -> 讀得到座標 (有世界檔) 才算軌道 3；沒有座標的照片無法判別 (UNKNOWN)
    """
    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"目標圖資不存在: {p}")

    ext = p.suffix.lower()

    # 1. 向量副檔名識別
    vector_extensions = {".shp", ".gpkg", ".geojson", ".json", ".dxf", ".kml", ".kmz", ".gpx"}
    if ext in vector_extensions:
        return TrackType.VECTOR_CADASTRE

    # 圖片：不管波段，有座標 (gdalinfo 給得出 geoTransform) 就當正射影像
    if ext in geo_io.IMAGE_EXTENSIONS:
        try:
            info_json = run_json_command(["gdalinfo", "-json", str(p)])
        except Exception as read_err:
            logger.debug("以 gdalinfo 讀取圖片 %s 失敗: %s", p.name, read_err)
            return TrackType.UNKNOWN
        return TrackType.ORTHO_AERIAL if info_json.get("geoTransform") else TrackType.UNKNOWN

    # 2. 網格副檔名識別與波段探測
    raster_extensions = {".tif", ".tiff", ".dem", ".vrt", ".asc", ".img"}
    if ext in raster_extensions:
        try:
            info_json = run_json_command(["gdalinfo", "-json", str(p)])
            bands = info_json.get("bands", [])
            band_count = len(bands)

            if band_count >= 3:
                # 3 或 4 波段 (RGB / RGBA) 判定為空拍正射影像
                return TrackType.ORTHO_AERIAL

            if band_count == 1:
                band_info = bands[0]
                data_type = str(band_info.get("type", "")).lower()
                # 浮點數或 16 位元以上高程通常為 DEM
                if any(dt in data_type for dt in ("float", "int16", "int32", "uint16")):
                    return TrackType.TERRAIN_DEM
                # 若為單波段 Byte，檢查有無色盤或統計值
                color_interp = str(band_info.get("colorInterpretation", "")).lower()
                if "palette" in color_interp or "gray" in color_interp:
                    return TrackType.ORTHO_AERIAL
                return TrackType.TERRAIN_DEM

            if band_count == 2:
                # 2 波段 (如 Gray + Alpha) 視為正射影像
                return TrackType.ORTHO_AERIAL

        except Exception as read_err:
            logger.debug("以 gdalinfo 偵測網格波段異常，依副檔名預設判定: %s", read_err)
            if ext == ".dem":
                return TrackType.TERRAIN_DEM
            return TrackType.ORTHO_AERIAL

    return TrackType.UNKNOWN


# ============================================================================
# 軌道 1：地籍與向量圖資裁切引擎 (Track 1: Vector / Cadastre)
# ============================================================================

def _promote_to_multi_args(geometry_type: str) -> List[str]:
    """面與線圖層裁切後可能變成多部件，先把輸出圖層型別升為 Multi，避免 GPKG 圖層型別與圖徵不符。"""
    type_name = geometry_type.lower()
    if ("polygon" in type_name or "line string" in type_name) and "multi" not in type_name:
        return ["-nlt", "PROMOTE_TO_MULTI"]
    return []


def clip_vector_to_metrics_gpkg(
    input_path: Union[str, Path],
    master_boundary_path: Union[str, Path],
    tmp: geo_io.TempVectorFiles,
    *,
    input_layer: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    area_field: str = CLIP_AREA_FIELD,
    hectare_field: str = CLIP_HA_FIELD,
    result_layer: str = "clipped",
    keep_whole: bool = False,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> geo_io.VectorLayerInfo:
    """向量裁切共用流程：重投影 → 記錄裁切前面積 → 裁切 → 計算裁切後面積、占比、長度與形心。

    keep_whole (整筆保留，用地清冊用)：跟邊界有重疊的圖徵整筆選出、不切開。面要有一部分面積落在邊界內才算
    (只有邊線貼著的不算)，線與點碰到就算。ORIG_M2 是整筆面積，area_field／hectare_field／RATIO_PCT
    是落在邊界內的部分，LEN_M 與形心照整筆的圖形算。做法：B 段多記來源圖徵編號，C 段照樣裁切，
    D 段把裁切成果的面積依編號接回原圖形。

    為什麼要分段：ogr2ogr 在同一個指令內會「先執行 -sql、後執行 -clipsrc」，所以寫在同一個指令的
    ST_Area 算到的是裁切前的面積；而且面積必須在重投影到目標座標系 (公尺) 之後才算，否則來源若是
    經緯度，得到的會是平方度。因此拆成四段，每一段各是一次 ogr2ogr：
      A. 來源正規化成暫存 GPKG：重投影到 target_srs、修復無效幾何 (幾何欄位固定為 geom)。
      B. SQL 寫入裁切前面積 ORIG_M2。
      C. -clipsrc 裁切 (不帶 SQL)。
      D. SQL 依裁切後的幾何寫入 area_field、hectare_field、RATIO_PCT、LEN_M、CENT_X、CENT_Y。

    來源若已有與上述計算欄位同名的欄位 (不分大小寫，例如把本工具的成果再裁切一次)，一律以重新計算的值取代。
    來源有座標系就用來源的；沒有時才採用 assigned_srs，未指派則依座標範圍推測 (見 crs_doctor.decide_source_srs)。

    回傳：
        暫存成果 GPKG 的圖層資訊 (檔案由 tmp 管理，呼叫端需在 tmp 清除前匯出)。
    """
    in_p = Path(input_path)
    bound_p = Path(master_boundary_path)
    computed_fields = [
        ORIG_AREA_FIELD, area_field, hectare_field, RATIO_FIELD, LENGTH_FIELD, CENTROID_X_FIELD, CENTROID_Y_FIELD,
    ]

    # A. 正規化 + 重投影
    srs_decision = decide_and_report_source_srs(
        in_p, assigned_srs, layer_name=input_layer, text_callback=text_callback
    )
    staged = geo_io.stage_vector_to_gpkg(
        in_p,
        tmp.new("a_src"),
        source_layer=input_layer,
        stage_layer="src",
        target_srs=target_srs,
        source_srs=srs_decision.s_srs,
        make_valid=True,
        progress_callback=geo_io.scaled_progress(progress_callback,0.0, 30.0),
        text_callback=text_callback,
    )
    if staged.is_geographic:
        raise ToolkitError(
            f"目標座標系 {target_srs} 是經緯度座標系，面積與長度無法以公尺計算；請改用 TWD97 二度分帶等投影座標系。"
        )

    replaced = [name for name in staged.field_names if name.casefold() in {f.casefold() for f in computed_fields}]
    if replaced and text_callback:
        text_callback(f"[注意] 來源圖層已有欄位 {'、'.join(replaced)}，成果中將以本次重新計算的值取代。\n")

    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    kept_fields = geo_io.build_field_select_list(staged, exclude=computed_fields)
    orig_ident = geo_io.quote_ident(ORIG_AREA_FIELD)

    # B. 裁切前面積 (整筆保留時多記來源圖徵編號；ROWID + 0 讓 GDAL 當成一般欄位，不當 FID)
    key_ident = geo_io.quote_ident(_WHOLE_KEY_FIELD)
    key_items = [f"ROWID + 0 AS {key_ident}"] if keep_whole else []
    sql_orig = (
        f"SELECT {', '.join([geom, *kept_fields])}, ST_Area({geom}) AS {orig_ident}{''.join(', ' + k for k in key_items)} "
        f"FROM {geo_io.quote_ident(staged.layer_name)}"
    )
    orig_gpkg = tmp.new("b_orig")
    geo_io.run_sql_to_gpkg(
        staged.path, sql_orig, orig_gpkg, "orig",
        progress_callback=geo_io.scaled_progress(progress_callback,30.0, 45.0),
        text_callback=text_callback,
    )

    # C. 裁切 (不帶 SQL)
    clip_gpkg = tmp.new("c_clip")
    geo_io.remove_vector_file(clip_gpkg)
    cmd_clip = [
        "ogr2ogr",
        "-f", "GPKG",
        str(clip_gpkg),
        str(orig_gpkg),
        "orig",
        "-clipsrc", str(bound_p),
        "-nln", "clip",
        "-lco", f"GEOMETRY_NAME={geo_io.STAGE_GEOMETRY_COLUMN}",
        *_promote_to_multi_args(staged.geometry_type),
    ]
    run_command(
        cmd_clip,
        progress_callback=geo_io.scaled_progress(progress_callback,45.0, 75.0),
        text_callback=text_callback,
        check=True,
    )

    # D. 裁切後的面積、占比、長度、形心
    def _metric_items(shape: str, area_expr: str) -> List[str]:
        """shape：算長度與形心的圖形；area_expr：落在邊界內的面積。"""
        length_expr = f"CASE WHEN ST_Dimension({shape}) = 2 THEN ST_Perimeter({shape}) ELSE ST_Length({shape}) END"
        return [
            f"ROUND({orig_ident}, 2) AS {orig_ident}",
            f"ROUND({area_expr}, 2) AS {geo_io.quote_ident(area_field)}",
            f"ROUND({area_expr} / 10000.0, 4) AS {geo_io.quote_ident(hectare_field)}",
            f"CASE WHEN {orig_ident} > 0 THEN ROUND({area_expr} * 100.0 / {orig_ident}, 2) ELSE 0.0 END "
            f"AS {geo_io.quote_ident(RATIO_FIELD)}",
            f"ROUND({length_expr}, 2) AS {geo_io.quote_ident(LENGTH_FIELD)}",
            f"ROUND(ST_X(ST_Centroid({shape})), 2) AS {geo_io.quote_ident(CENTROID_X_FIELD)}",
            f"ROUND(ST_Y(ST_Centroid({shape})), 2) AS {geo_io.quote_ident(CENTROID_Y_FIELD)}",
        ]

    result_gpkg = tmp.new("d_result")
    if not keep_whole:
        metric_items = _metric_items(geom, f"ST_Area({geom})")
        sql_metrics = f"SELECT {', '.join([geom, *kept_fields, *metric_items])} FROM {geo_io.quote_ident('clip')}"
        geo_io.run_sql_to_gpkg(
            clip_gpkg, sql_metrics, result_gpkg, result_layer,
            progress_callback=geo_io.scaled_progress(progress_callback,75.0, 90.0),
            text_callback=text_callback,
        )
        return geo_io.read_layer_info(result_gpkg, result_layer)

    # 整筆保留：裁切成果複製進 B 段的 GPKG，依來源圖徵編號把範圍內面積接回原圖形
    run_command(
        [
            "ogr2ogr", "-f", "GPKG", "-update", str(orig_gpkg), str(clip_gpkg), "clip", "-nln", "clip",
            "-lco", f"GEOMETRY_NAME={geo_io.STAGE_GEOMETRY_COLUMN}",
        ],
        text_callback=text_callback,
        check=True,
    )
    inside = "c.inside_m2"
    whole_items = [f"o.{item}" for item in [geom, *kept_fields]]
    sql_whole = (
        f"SELECT {', '.join([*whole_items, *_metric_items(f'o.{geom}', inside)])} "
        f"FROM {geo_io.quote_ident('orig')} AS o JOIN ("
        f"SELECT {key_ident} AS clip_key, SUM(ST_Area({geom})) AS inside_m2 FROM {geo_io.quote_ident('clip')} "
        f"WHERE {geom} IS NOT NULL GROUP BY {key_ident}"
        f") AS c ON o.{key_ident} = c.clip_key "
        # 面只有邊線貼著邊界 (交集面積是 0) 的不算；線與點碰到就算
        f"WHERE ST_Dimension(o.{geom}) < 2 OR {inside} > 0"
    )
    geo_io.run_sql_to_gpkg(
        orig_gpkg, sql_whole, result_gpkg, result_layer,
        progress_callback=geo_io.scaled_progress(progress_callback,75.0, 90.0),
        text_callback=text_callback,
    )
    return geo_io.read_layer_info(result_gpkg, result_layer)


def clip_vector_layer(
    input_path: Union[str, Path],
    master_boundary_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    input_layer: Optional[str] = None,
    output_format: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    excel_output_path: Optional[Union[str, Path]] = None,
    keep_whole: bool = False,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> ClipJobResult:
    """執行軌道 1：地籍與向量圖資精確裁切、幾何修復與地籍屬性統計。

    excel_output_path：指定時另把成果屬性寫成 Excel (全部原屬性＋面積欄位＋合計列，見 _write_clip_excel)；
    Excel 寫不出來 (例如檔案正在 Excel 開著) 只記警告，裁切成果照樣算成功。
    keep_whole：整筆保留 (跟邊界有重疊的圖徵整筆選出、不切開；見 clip_vector_to_metrics_gpkg)；
    回傳的 area_m2／area_ha 是落在邊界內的面積合計。

    核心規範（規格書 3.2 節）：
    1. 調用 ogr2ogr 進行 -clipsrc 與 -makevalid。
    2. 利用 SQLite SQL 在輸出屬性表末端寫入數據 (流程見 clip_vector_to_metrics_gpkg)：
       - ORIG_M2: 裁切前面積 (平方公尺)
       - AREA_M2: 裁切後多邊形面積 (平方公尺)
       - AREA_HA: 裁切後面積 (公頃，四捨五入至小數第 4 位)
       - RATIO_PCT: 裁切後面積占裁切前面積的百分比
       - LEN_M: 裁切後幾何周長或線段長度 (公尺)
       - CENT_X, CENT_Y: 裁切後幾何形心座標
    3. 輸出格式依 output_path 副檔名決定；Shapefile 以 UTF-8 寫出並產生 .cpg。
    4. assigned_srs 只在來源沒有座標系標籤時採用 (例如 "TWD67_121")；未指派則依座標範圍推測。
    """
    start_time = time.time()
    in_p = Path(input_path).resolve()
    bound_p = Path(master_boundary_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not in_p.exists():
        raise FileNotFoundError(f"找不到來源向量檔案: {in_p}")
    if not bound_p.exists():
        raise FileNotFoundError(f"找不到主裁切邊界檔 (master_boundary): {bound_p}")

    with geo_io.TempVectorFiles("clip") as tmp:
        result_info = clip_vector_to_metrics_gpkg(
            in_p,
            bound_p,
            tmp,
            input_layer=input_layer,
            target_srs=target_srs,
            assigned_srs=assigned_srs,
            keep_whole=keep_whole,
            progress_callback=progress_callback,
            text_callback=text_callback,
        )
        if keep_whole and text_callback:
            text_callback(f"[整筆保留] {in_p.name}：跟邊界有重疊的 {result_info.feature_count:,} 筆整筆選出，不切開。\n")

        # 統計裁切成果面積與筆數 (直接讀屬性值，不解析 ogrinfo 文字輸出)
        rows = geo_io.read_attribute_rows(result_info.path, result_info.layer_name, fields=[CLIP_AREA_FIELD])
        feature_count = len(rows)
        total_area_m2 = round(sum(float(row[CLIP_AREA_FIELD] or 0.0) for row in rows), 2)
        total_area_ha = round(total_area_m2 / 10000.0, 4)

        if feature_count == 0 and text_callback:
            text_callback(f"[注意] {in_p.name} 與裁切邊界沒有交集，成果為空圖層。\n")

        geo_io.export_vector(
            result_info.path,
            result_info.layer_name,
            out_p,
            output_format=output_format,
            progress_callback=geo_io.scaled_progress(progress_callback,90.0, 100.0),
            text_callback=text_callback,
        )

        # Excel 從暫存 GPKG 讀：欄位名是完整的原名 (Shapefile 成果的欄位名可能被截成 10 bytes)
        excel_path: Optional[Path] = None
        if excel_output_path is not None:
            try:
                excel_path = _write_clip_excel(
                    result_info, Path(excel_output_path).resolve(), in_p, bound_p, target_srs, text_callback,
                    keep_whole=keep_whole,
                )
            except Exception as err:
                _warn(text_callback, f"{in_p.name} 的屬性 Excel 沒有產生：{err}")

    duration = time.time() - start_time

    return ClipJobResult(
        input_path=in_p,
        output_path=out_p,
        track_type=TrackType.VECTOR_CADASTRE,
        is_success=True,
        duration_seconds=duration,
        area_m2=total_area_m2,
        area_ha=total_area_ha,
        feature_count=feature_count,
        target_srs=target_srs,
        attribute_excel=excel_path,
    )


# 屬性 Excel 裡本工具計算欄位的中文說明與數字格式
CLIP_EXCEL_LABELS: Dict[str, str] = {
    ORIG_AREA_FIELD: "裁切前面積 (m²)",
    CLIP_AREA_FIELD: "裁切後面積 (m²)",
    CLIP_HA_FIELD: "裁切後面積 (公頃)",
    RATIO_FIELD: "面積比例 (%)",
    LENGTH_FIELD: "周長或長度 (m)",
    CENTROID_X_FIELD: "形心 X",
    CENTROID_Y_FIELD: "形心 Y",
}
# 整筆保留：ORIG_M2 是整筆面積，AREA_M2 等是落在邊界內的部分
WHOLE_EXCEL_LABELS: Dict[str, str] = {
    **CLIP_EXCEL_LABELS,
    ORIG_AREA_FIELD: "整筆面積 (m²)",
    CLIP_AREA_FIELD: "範圍內面積 (m²)",
    CLIP_HA_FIELD: "範圍內面積 (公頃)",
    RATIO_FIELD: "範圍內比例 (%)",
}
CLIP_EXCEL_FORMATS: Dict[str, str] = {
    ORIG_AREA_FIELD: "#,##0.00",
    CLIP_AREA_FIELD: "#,##0.00",
    CLIP_HA_FIELD: "#,##0.0000",
    RATIO_FIELD: "0.00",
    LENGTH_FIELD: "#,##0.00",
    CENTROID_X_FIELD: "0.00",
    CENTROID_Y_FIELD: "0.00",
}


def _srs_display(target_srs: str) -> str:
    """說明列用的座標系名稱 (TWD67 的七參數 PROJ 字串太長，換成簡稱)。"""
    if target_srs.startswith("+proj") or "towgs84" in target_srs:
        return "TWD67 (七參數)"
    return target_srs


def _write_clip_excel(
    result_info: geo_io.VectorLayerInfo,
    output_xlsx: Path,
    source: Path,
    boundary: Path,
    target_srs: str,
    text_callback: Optional[Callable[[str], None]],
    *,
    keep_whole: bool = False,
) -> Path:
    """把裁切成果的屬性寫成 Excel；合計列：裁切前／後面積、公頃、面積比例 (合計後再算)，線圖層另加總長度。
    keep_whole (整筆保留) 時欄名改成整筆面積、範圍內面積、範圍內比例。"""
    metric_rows = geo_io.read_attribute_rows(
        result_info.path, result_info.layer_name, fields=[ORIG_AREA_FIELD, CLIP_AREA_FIELD, LENGTH_FIELD]
    )
    orig_total = round(sum(float(row[ORIG_AREA_FIELD] or 0.0) for row in metric_rows), 2)
    clip_total = round(sum(float(row[CLIP_AREA_FIELD] or 0.0) for row in metric_rows), 2)
    totals: Dict[str, Optional[float]] = {
        ORIG_AREA_FIELD: orig_total,
        CLIP_AREA_FIELD: clip_total,
        CLIP_HA_FIELD: round(clip_total / 10000.0, 4),
        RATIO_FIELD: round(clip_total * 100.0 / orig_total, 2) if orig_total > 0 else None,
    }
    if "line" in result_info.geometry_type.lower():
        # 線圖層的總長度有意義；面圖層的周長加總沒有意義，留白
        totals[LENGTH_FIELD] = round(sum(float(row[LENGTH_FIELD] or 0.0) for row in metric_rows), 2)

    # openpyxl 載入約 0.2 秒，有勾「屬性輸出 Excel」才用到，到這裡才載入
    from core.attribute_report import write_attribute_excel

    if keep_whole:
        title = f"{source.name} 整筆保留成果屬性表"
        explain = (
            "整筆保留：跟邊界有重疊的圖徵整筆列出、不切開 (面只有邊線貼著邊界的不算)；整筆面積是原圖形的面積，"
            "範圍內面積是落在邊界內的部分 (平方公尺)；範圍內比例 = 範圍內面積 ÷ 整筆面積；原屬性照原值列出，不加總。"
        )
    else:
        title = f"{source.name} 裁切成果屬性表"
        explain = "面積依裁切後的圖形計算 (平方公尺)；面積比例 = 裁切後面積 ÷ 裁切前面積；原屬性照原值列出，不加總。"
    report = write_attribute_excel(
        result_info.path,
        result_info.layer_name,
        output_xlsx,
        title=title,
        notes=[
            f"裁切邊界：{boundary.name}　座標系：{_srs_display(target_srs)}　產出時間：{time.strftime('%Y-%m-%d %H:%M')}",
            explain,
        ],
        column_labels=WHOLE_EXCEL_LABELS if keep_whole else CLIP_EXCEL_LABELS,
        number_formats=CLIP_EXCEL_FORMATS,
        totals=totals,
    )
    if text_callback:
        text_callback(f"[屬性 Excel] 已輸出 {report.output_path.name}：{report.row_count} 筆＋合計列。\n")
    return report.output_path


# ============================================================================
# 網格裁切成果檢查：邊界與圖資沒有重疊時，gdalwarp 仍會產出整片 NoData 的檔案
# ============================================================================

# 檢查有沒有有效像元時，遮罩縮小讀取的單邊像素上限 (不讀全解析度)
_VALID_DATA_SAMPLE_SIZE: int = 1024


def raster_has_valid_data(raster_path: Union[str, Path]) -> bool:
    """網格是否至少有一個有效像元：讀第 1 波段的遮罩 (NoData 或 Alpha)，大圖縮小取樣。"""
    from osgeo import gdal

    geo_io.ensure_gdal_ready()
    dataset = gdal.Open(str(raster_path))
    if dataset is None:
        raise ToolkitError(f"無法開啟裁切成果: {raster_path}")
    try:
        width, height = dataset.RasterXSize, dataset.RasterYSize
        scale = max(width, height) / _VALID_DATA_SAMPLE_SIZE
        buf_w, buf_h = (width, height) if scale <= 1 else (max(1, int(width / scale)), max(1, int(height / scale)))
        mask = dataset.GetRasterBand(1).GetMaskBand().ReadAsArray(0, 0, width, height, buf_xsize=buf_w, buf_ysize=buf_h)
        return bool((mask > 0).any())
    finally:
        dataset = None


def _describe_no_overlap(source_paths: Sequence[Path], boundary_path: Path) -> str:
    """列出邊界與圖資的範圍 (各自的座標系)，座標系相同時附上相距距離，幫使用者看出是不是拿錯檔。"""
    try:
        bound_info = geo_io.read_layer_info(boundary_path)
        metas = [_read_raster_srs_metadata(p) for p in source_paths]
    except Exception as err:
        logger.debug("讀取範圍說明略過: %s", err)
        return ""
    if bound_info.extent is None:
        return ""
    b_min_x, b_min_y, b_max_x, b_max_y = bound_info.extent
    r_min_x = min(m.extent.min_x for m in metas)
    r_min_y = min(m.extent.min_y for m in metas)
    r_max_x = max(m.extent.max_x for m in metas)
    r_max_y = max(m.extent.max_y for m in metas)
    text = (
        f"邊界範圍 X {b_min_x:.1f}～{b_max_x:.1f}、Y {b_min_y:.1f}～{b_max_y:.1f} ({bound_info.srs_name or '無座標系標籤'})；"
        f"圖資範圍 X {r_min_x:.1f}～{r_max_x:.1f}、Y {r_min_y:.1f}～{r_max_y:.1f} ({metas[0].srs_name or '無座標系標籤'})"
    )
    same_projected_srs = (
        bound_info.epsg is not None
        and all(m.epsg == bound_info.epsg for m in metas)
        and not bound_info.is_geographic
    )
    if same_projected_srs:
        gap_x = max(0.0, b_min_x - r_max_x, r_min_x - b_max_x)
        gap_y = max(0.0, b_min_y - r_max_y, r_min_y - b_max_y)
        gap = (gap_x**2 + gap_y**2) ** 0.5
        if gap > 0:
            text += f"，兩者相距約 {gap / 1000.0:.1f} km"
    return text + "。"


def _reject_empty_raster_output(out_p: Path, source_paths: Sequence[Path], boundary_path: Path) -> None:
    """裁切成果沒有任何有效像元時刪掉成果檔並拋錯 (批次裁切會把這一件記為失敗)。"""
    if raster_has_valid_data(out_p):
        return
    for leftover in (out_p, out_p.with_name(f"{out_p.name}.aux.xml")):
        leftover.unlink(missing_ok=True)
    names = "、".join(p.name for p in source_paths)
    raise ToolkitError(
        f"{names} 裁切後沒有任何有效資料：邊界與圖資沒有重疊，或重疊處都是 NoData。"
        f"{_describe_no_overlap(source_paths, boundary_path)}"
        "請確認邊界與圖資是同一地點、座標系是否正確。"
    )


# ============================================================================
# 軌道 2：地形高程 DEM 裁切與降精度引擎 (Track 2: Terrain DEM)
# ============================================================================

def build_virtual_mosaic(
    raster_paths: Sequence[Union[str, Path]],
    output_vrt_path: Union[str, Path],
    *,
    resolution: str = "highest",
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 gdalbuildvrt 進行 0 秒極速虛擬無損拼接多幅 DEM / TIF 圖幅。"""
    vrt_p = Path(output_vrt_path).resolve()
    vrt_p.parent.mkdir(parents=True, exist_ok=True)

    input_str_paths = [str(Path(p).resolve()) for p in raster_paths]
    if not input_str_paths:
        raise ValueError("提供的網格圖幅串列不得為空！")

    cmd = [
        "gdalbuildvrt",
        "-resolution", resolution,
        "-overwrite",
        str(vrt_p),
    ] + input_str_paths

    run_command(cmd, progress_callback=progress_callback, text_callback=text_callback, check=True)
    return vrt_p


# 批次裁切把多幅 DEM 拼接成單一成果時的輸出檔名 (不含解析度標記與副檔名)
DEM_MOSAIC_OUTPUT_STEM: str = "dem_mosaic"
# 兩幅 DEM 的重疊面積超過較小一幅的這個比例，就視為同一範圍的不同版本 (例如兩期 DEM)，不自動拼接
_MOSAIC_MAX_OVERLAP_RATIO: float = 0.5


def find_dem_mosaic_blocker(dem_paths: Sequence[Union[str, Path]]) -> Optional[str]:
    """檢查多幅 DEM 能不能直接虛擬拼接；可以回傳 None，不行回傳原因。

    - 座標系要一致 (都沒有標籤也算一致)：gdalbuildvrt 遇到座標系不同的圖幅只會警告並跳過，成果會缺一塊。
    - 任兩幅不可大幅重疊：重疊處只會留下後面那一幅的值，兩期 DEM 一起投入時會悄悄少掉一期。
    """
    metadata = [(Path(p), _read_raster_srs_metadata(Path(p).resolve())) for p in dem_paths]
    first_path, first_meta = metadata[0]
    for other_path, other_meta in metadata[1:]:
        if (other_meta.has_embedded_srs, other_meta.epsg, other_meta.srs_name) != (
            first_meta.has_embedded_srs,
            first_meta.epsg,
            first_meta.srs_name,
        ):
            return f"{first_path.name} 與 {other_path.name} 的座標系不同"

    for index, (path_a, meta_a) in enumerate(metadata):
        a = meta_a.extent
        area_a = (a.max_x - a.min_x) * (a.max_y - a.min_y)
        for path_b, meta_b in metadata[index + 1:]:
            b = meta_b.extent
            area_b = (b.max_x - b.min_x) * (b.max_y - b.min_y)
            overlap_w = min(a.max_x, b.max_x) - max(a.min_x, b.min_x)
            overlap_h = min(a.max_y, b.max_y) - max(a.min_y, b.min_y)
            smaller = min(area_a, area_b)
            if overlap_w > 0 and overlap_h > 0 and smaller > 0 and overlap_w * overlap_h > smaller * _MOSAIC_MAX_OVERLAP_RATIO:
                return f"{path_a.name} 與 {path_b.name} 的範圍大幅重疊，可能是同一地區的不同期 DEM"
    return None


def clip_dem_layer(
    input_dem_paths: Union[Union[str, Path], Sequence[Union[str, Path]]],
    master_boundary_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    target_resolution: Optional[float] = None,
    resampling_method: str = "bilinear",
    dst_nodata: float = -9999.0,
    source_srs: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> ClipJobResult:
    """執行軌道 2：地形高程 DEM 裁切、多幅虛擬拼接與降精度重取樣。

    核心規範（規格書 3.2 節）：
    1. 若輸入多個分幅，先調用 gdalbuildvrt 生成虛擬拼接中繼檔 (各幅座標系必須一致，否則拋 ToolkitError；
       重疊處以清單中較後面的圖幅為準)。
    2. 使用 -cutline 與 -crop_to_cutline 依邊界裁切。
    3. 重取樣演算法強制使用 -r bilinear 或 -r average（嚴禁預設 near 產生階梯斷層）。
    4. 致命語法防呆：強制套用 -tap 與整數像元對齊 (-tr {res} {res} -tap)。
    5. 加入 -co COMPRESS=LZW 無損壓縮。
    6. 來源有座標系就用來源的，不帶 -s_srs；沒有時才採用 assigned_srs (source_srs 為舊參數名，同義)，
       未指派則依座標範圍推測。
    7. 成果沒有任何有效像元 (邊界與圖資沒有重疊等) 時刪掉成果並拋 ToolkitError。
    """
    start_time = time.time()
    bound_p = Path(master_boundary_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not bound_p.exists():
        raise FileNotFoundError(f"找不到主裁切邊界檔: {bound_p}")

    # 重取樣演算法驗證
    valid_resampling = {"bilinear", "average", "cubic", "cubicspline", "lanczos"}
    clean_resample = resampling_method.lower().strip()
    if clean_resample not in valid_resampling:
        logger.warning("指定之重取樣演算法 %s 不建議用於連續高程 DEM，自動校正為 bilinear", clean_resample)
        clean_resample = "bilinear"

    # 處理單圖幅或多圖幅
    input_list = [input_dem_paths] if isinstance(input_dem_paths, (str, Path)) else list(input_dem_paths)
    resolved_inputs = [Path(p).resolve() for p in input_list]

    for p in resolved_inputs:
        if not p.exists():
            raise FileNotFoundError(f"找不到輸入高程檔案: {p}")

    with geo_io.TempVectorFiles("mosaic") as temp_files:
        if len(resolved_inputs) == 1:
            primary_input = resolved_inputs[0]
        else:
            # 多圖幅自動虛擬拼接；gdalbuildvrt 會跳過座標系不同的圖幅，所以先檢查
            first_meta = _read_raster_srs_metadata(resolved_inputs[0])
            for other in resolved_inputs[1:]:
                other_meta = _read_raster_srs_metadata(other)
                if (other_meta.has_embedded_srs, other_meta.epsg, other_meta.srs_name) != (
                    first_meta.has_embedded_srs,
                    first_meta.epsg,
                    first_meta.srs_name,
                ):
                    raise ToolkitError(
                        f"多幅 DEM 的座標系不一致，無法拼接: {resolved_inputs[0].name} 與 {other.name}。請分開裁切。"
                    )
            primary_input = build_virtual_mosaic(
                resolved_inputs, temp_files.new("dem", ".vrt"), text_callback=text_callback
            )

        srs_decision = decide_and_report_source_srs(
            primary_input, assigned_srs or source_srs, text_callback=text_callback
        )

        # 構建 -tap 與 -tr 參數（防呆防崩潰）；解析度單位以目標座標系為準
        res_x, res_y = resolve_warp_resolution(
            primary_input, srs_decision, target_srs, target_resolution, text_callback=text_callback
        )
        tap_tr_args = build_tap_parameters(target_res_x=res_x, target_res_y=res_y)

        cmd = [
            "gdalwarp",
            *srs_decision.as_ogr_args(),
            "-t_srs", target_srs,
            "-cutline", str(bound_p),
            "-crop_to_cutline",
            "-r", clean_resample,
            "-dstnodata", str(dst_nodata),
            "-co", "COMPRESS=LZW",
            "-overwrite",
        ] + tap_tr_args + [str(primary_input), str(out_p)]

        run_command(cmd, progress_callback=progress_callback, text_callback=text_callback, check=True)
    _reject_empty_raster_output(out_p, resolved_inputs, bound_p)
    duration = time.time() - start_time

    # 取得裁切後之解析度與尺寸
    dims = None
    try:
        info_json = run_json_command(["gdalinfo", "-json", str(out_p)])
        size = info_json.get("size")
        if size and len(size) >= 2:
            dims = (int(size[0]), int(size[1]))
    except Exception as info_err:
        logger.debug("讀取 DEM 成果尺寸略過: %s", info_err)

    return ClipJobResult(
        input_path=resolved_inputs[0],
        output_path=out_p,
        track_type=TrackType.TERRAIN_DEM,
        is_success=True,
        duration_seconds=duration,
        raster_dims=dims,
        target_srs=target_srs,
        merged_input_paths=list(resolved_inputs) if len(resolved_inputs) > 1 else None,
    )


# ============================================================================
# 軌道 3：空拍正射影像裁切、金字塔與 CAD 底圖輸出 (Track 3: Aerial Ortho)
# ============================================================================

# JPEG 格式的單邊像素上限
_JPEG_MAX_DIMENSION = 65535


def _warn(text_callback: Optional[Callable[[str], None]], message: str) -> None:
    """把不中斷流程的警告同時寫進日誌與日誌框。"""
    logger.warning(message)
    if text_callback:
        text_callback(f"[警告] {message}\n")


def _cad_jpeg_band_args(
    raster_info: Dict[str, Any],
    text_callback: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """依裁切成果的波段組成，回傳輸出 CAD 底圖 JPEG 時要加給 gdal_translate 的參數。

    JPEG 沒有透明度：4 波段 (RGBA) 直接輸出會被寫成 CMYK，所以只取顏色波段、不帶 Alpha。
    8-bit 來源不加 -scale (會改變顏色)，其他位元深度才轉成 Byte 並拉伸。
    """
    bands = raster_info.get("bands") or []
    color_bands = [band for band in bands if str(band.get("colorInterpretation", "")).lower() != "alpha"]
    args: List[str] = []
    if len(color_bands) == 1 and color_bands[0].get("colorTable"):
        # 色盤影像要展開成 RGB，否則 JPEG 內容會是色盤編號
        args.extend(["-b", str(color_bands[0].get("band", 1)), "-expand", "rgb"])
    else:
        picked = color_bands[:3] if len(color_bands) >= 3 else color_bands[:1]
        for band in picked:
            args.extend(["-b", str(band.get("band", bands.index(band) + 1))])
        if any(str(band.get("type", "Byte")) != "Byte" for band in picked):
            args.extend(["-ot", "Byte", "-scale"])

    size = raster_info.get("size") or []
    if len(size) >= 2 and max(int(size[0]), int(size[1])) > _JPEG_MAX_DIMENSION:
        width, height = int(size[0]), int(size[1])
        ratio = _JPEG_MAX_DIMENSION / max(width, height)
        new_width = max(1, min(_JPEG_MAX_DIMENSION, int(width * ratio)))
        new_height = max(1, min(_JPEG_MAX_DIMENSION, int(height * ratio)))
        args.extend(["-outsize", str(new_width), str(new_height), "-r", "average"])
        _warn(
            text_callback,
            f"裁切成果 {width}×{height} 像素超過 JPEG 單邊 {_JPEG_MAX_DIMENSION} 像素的上限，"
            f"CAD 底圖縮小為 {new_width}×{new_height} (對位檔 .jgw 已同步調整)。",
        )
    return args


def _warn_if_finer_than_source(
    source: Path,
    srs_decision: SourceSrsDecision,
    target_srs: str,
    target_resolution: float,
    text_callback: Optional[Callable[[str], None]],
) -> None:
    """指定的解析度比原圖細時提醒 (檔案會變大但不會更清楚)；來源或目標是經緯度時單位不同，不比較。"""
    source_is_geographic = (
        srs_is_geographic(srs_decision.s_srs) if srs_decision.s_srs else _read_raster_srs_metadata(source).is_geographic
    )
    if source_is_geographic or srs_is_geographic(target_srs):
        return
    source_res = min(get_raster_resolution(source))
    if target_resolution < source_res * 0.999:
        _warn(
            text_callback,
            f"指定的解析度 {target_resolution:g} m 比 {source.name} 原圖的 {source_res:g} m 還細，"
            "檔案會變大但影像不會更清楚。",
        )


def clip_ortho_layer(
    input_ortho_path: Union[str, Path],
    master_boundary_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    generate_pyramids: bool = True,
    export_cad_background: bool = True,
    target_resolution: Optional[float] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> ClipJobResult:
    """執行軌道 3：空拍正射影像依邊界裁切、影像金字塔加速與 CAD 對位底圖產出。

    核心規範（規格書 3.2 節）：
    0. target_resolution (目標座標系單位，通常是公尺)：指定時以 -tr -tap 重取樣 (像元變大、位置不變)，
       CAD 底圖由裁切成果轉出，解析度自然相同；None 維持原圖解析度。比原圖細時只提醒 (檔案變大但不會更清楚)。
    1. 使用 gdalwarp（-r cubic）維持空拍攝影細節紋理 (縮小時 GDAL 會放大取樣範圍，不會鋸齒)。
    2. 加入 -co COMPRESS=LZW 與 -co TILED=YES 支援區塊式快取。
    3. 自動執行 gdaladdo -ro -r average 2 4 8 16 建立外部 .ovr 金字塔，讓 ArcMap 秒開。
    4. [A-06] 同步調用 gdal_translate -of JPEG -co WORLDFILE=YES 輸出 AutoCAD 對位底圖 (.jpg + .jgw)；
       只取 RGB 三個波段 (JPEG 沒有透明度，4 波段會被寫成 CMYK)，8-bit 來源不拉伸顏色。
    6. 裁切加 -dstalpha，邊界外為透明而不是黑色。
    5. 來源有座標系就用來源的，不帶 -s_srs；沒有時才採用 assigned_srs，未指派則依座標範圍推測。
    7. 成果沒有任何有效像元 (邊界與圖資沒有重疊等) 時刪掉成果並拋 ToolkitError，不建金字塔與 CAD 底圖。
    """
    start_time = time.time()
    in_p = Path(input_ortho_path).resolve()
    bound_p = Path(master_boundary_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not in_p.exists():
        raise FileNotFoundError(f"找不到空拍影像檔案: {in_p}")
    if not bound_p.exists():
        raise FileNotFoundError(f"找不到主裁切邊界檔: {bound_p}")

    # 1. 執行空拍正射影像裁切 (使用 cubic 重取樣)
    srs_decision = decide_and_report_source_srs(in_p, assigned_srs, text_callback=text_callback)
    resolution_args: List[str] = []
    if target_resolution is not None:
        res_x, res_y = resolve_warp_resolution(in_p, srs_decision, target_srs, target_resolution)
        resolution_args = build_tap_parameters(target_res_x=res_x, target_res_y=res_y)
        _warn_if_finer_than_source(in_p, srs_decision, target_srs, target_resolution, text_callback)
    cmd_warp = [
        "gdalwarp",
        *srs_decision.as_ogr_args(),
        "-t_srs", target_srs,
        "-cutline", str(bound_p),
        "-crop_to_cutline",
        "-dstalpha",
        "-r", "cubic",
        *resolution_args,
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        "-overwrite",
        str(in_p),
        str(out_p),
    ]

    # 上一次留下的外部金字塔與新成果不相符，先移除
    stale_ovr = out_p.with_name(f"{out_p.name}.ovr")
    stale_ovr.unlink(missing_ok=True)
    run_command(cmd_warp, progress_callback=progress_callback, text_callback=text_callback, check=True)
    _reject_empty_raster_output(out_p, [in_p], bound_p)
    out_info = run_json_command(["gdalinfo", "-json", str(out_p)])

    # 2. 建立 .ovr 影像金字塔 (ArcMap 秒開關鍵)
    ovr_path = None
    if generate_pyramids:
        if text_callback:
            text_callback("[金字塔] 正在為裁切後之空拍正射影像建立多層級快顯金字塔 (.ovr)。\n")
        # -ro：金字塔寫成外部 .ovr，不加的話會寫進 TIFF 內部
        cmd_addo = ["gdaladdo", "-ro", "-r", "average", str(out_p), "2", "4", "8", "16"]
        addo_result = run_command(cmd_addo, text_callback=text_callback, check=False)
        expected_ovr = out_p.with_name(f"{out_p.name}.ovr")
        if expected_ovr.exists():
            ovr_path = expected_ovr
        else:
            _warn(text_callback, f"影像金字塔 (.ovr) 建立失敗 (代碼 {addo_result.returncode})，裁切成果本身不受影響。")

    # 3. [A-06] 同步輸出 AutoCAD 對位底圖 (.jpg + .jgw)
    cad_jpg_path = None
    cad_jgw_path = None
    if export_cad_background:
        if text_callback:
            text_callback("[CAD底圖] 正在導出 AutoCAD 對位影像 (.jpg + .jgw)。\n")
        cad_jpg = out_p.parent / f"{out_p.stem}_cad_bg.jpg"
        for stale in (cad_jpg, cad_jpg.with_suffix(".jgw"), cad_jpg.with_suffix(".wld"), cad_jpg.with_name(f"{cad_jpg.name}.aux.xml")):
            stale.unlink(missing_ok=True)
        cmd_translate = [
            "gdal_translate",
            "-of", "JPEG",
            "-co", "WORLDFILE=YES",
            *_cad_jpeg_band_args(out_info, text_callback),
            str(out_p),
            str(cad_jpg),
        ]
        translate_result = run_command(cmd_translate, text_callback=text_callback, check=False)
        if translate_result.returncode == 0 and cad_jpg.exists():
            cad_jpg_path = cad_jpg
            # GDAL 的 JPEG 驅動把對位檔寫成 .wld，AutoCAD 要的是 .jgw
            jgw_candidate = cad_jpg.with_suffix(".jgw")
            wld_file = cad_jpg.with_suffix(".wld")
            if wld_file.exists():
                wld_file.replace(jgw_candidate)
            if jgw_candidate.exists():
                cad_jgw_path = jgw_candidate
        else:
            _warn(text_callback, f"AutoCAD 對位底圖 (.jpg) 導出失敗 (代碼 {translate_result.returncode})，裁切成果本身不受影響。")

    duration = time.time() - start_time

    # 讀取尺寸
    dims = None
    size = out_info.get("size")
    if size and len(size) >= 2:
        dims = (int(size[0]), int(size[1]))

    return ClipJobResult(
        input_path=in_p,
        output_path=out_p,
        track_type=TrackType.ORTHO_AERIAL,
        is_success=True,
        duration_seconds=duration,
        cad_bg_jpg=cad_jpg_path,
        cad_bg_jgw=cad_jgw_path,
        pyramid_ovr=ovr_path,
        raster_dims=dims,
        target_srs=target_srs,
    )


# ============================================================================
# 三軌平行批次分流排程總管 (Batch Clip Engine)
# ============================================================================

class BatchClipEngine:
    """自動化三軌平行批次分流裁切排程管理器。

    功能：
    - 接收使用者投入之任意混合檔案清單 (地籍 SHP, 高程 DEM, 空拍 TIF, CAD DXF)。
    - 全自動依格式與內部特徵分流至軌道 1、軌道 2 或軌道 3。
    - 多幅相鄰的 DEM 先以 gdalbuildvrt 拼接，裁切成單一無縫成果 (dem_mosaic*.tif)。
    - 即時計算整體批次進度並傳遞至 UI 進度條與日誌框。
    """

    def __init__(
        self,
        master_boundary_path: Union[str, Path],
        output_directory: Union[str, Path],
        *,
        dem_target_resolution: Optional[float] = None,
        dem_resampling: str = "bilinear",
        generate_pyramids: bool = True,
        export_cad_bg: bool = True,
        target_srs: str = f"EPSG:{EPSG_TWD97_121}",
        assigned_srs: Optional[str] = None,
        mosaic_dems: bool = True,
        ortho_target_resolution: Optional[float] = None,
        export_attribute_excel: bool = False,
        keep_whole_features: bool = False,
    ) -> None:
        # 投入多幅 DEM 時先拼接再裁切成單一成果；座標系不同或範圍大幅重疊 (多期 DEM) 時自動改回逐幅裁切
        self.mosaic_dems = mosaic_dems
        self.master_boundary = Path(master_boundary_path).resolve()
        self.output_dir = Path(output_directory).resolve()
        self.dem_target_res = dem_target_resolution
        # 正射影像的解析度 (None 維持原圖)；裁出的 GeoTIFF 與 AutoCAD 底圖 JPG 都是這個解析度
        self.ortho_target_res = ortho_target_resolution
        # 向量成果另輸出屬性 Excel (與成果 SHP 同名 .xlsx)
        self.export_attribute_excel = export_attribute_excel
        # 向量整筆保留 (跟邊界有重疊的圖徵整筆選出，成果 <檔名>_whole.shp)
        self.keep_whole_features = keep_whole_features
        self.dem_resampling = dem_resampling
        self.generate_pyramids = generate_pyramids
        self.export_cad_bg = export_cad_bg
        self.target_srs = target_srs
        # 只會套用在沒有座標系標籤的來源；有標籤的來源一律用自己的座標系
        self.assigned_srs = assigned_srs

        if not self.master_boundary.exists():
            raise FileNotFoundError(f"主裁切邊界檔不存在: {self.master_boundary}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def execute_batch(
        self,
        input_files: Sequence[Union[str, Path]],
        *,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        text_callback: Optional[Callable[[str], None]] = None,
    ) -> List[ClipJobResult]:
        """執行批次裁切任務，循序分流處理並匯總成果報告。"""
        valid_paths = [Path(f).resolve() for f in input_files if Path(f).exists()]
        total_files = len(valid_paths)
        if total_files == 0:
            if text_callback:
                text_callback("【提示】投入之檔案清單為空或檔案不存在。\n")
            return []

        results: List[ClipJobResult] = []

        if text_callback:
            text_callback(f"開始執行三軌批次裁切作業，共計 {total_files} 個圖資檔案。\n")

        # 先分類；多幅 DEM 可拼接時合併成一件工作，排在第一幅 DEM 的位置
        tracks = [detect_track_type(file_path) for file_path in valid_paths]
        dem_paths = [file_path for file_path, track in zip(valid_paths, tracks) if track == TrackType.TERRAIN_DEM]
        mosaic_paths: List[Path] = []
        if self.mosaic_dems and len(dem_paths) > 1:
            blocker = find_dem_mosaic_blocker(dem_paths)
            if blocker is None:
                mosaic_paths = dem_paths
                if text_callback:
                    text_callback(f"[DEM 拼接] 投入 {len(dem_paths)} 幅 DEM，先虛擬拼接再裁切成單一成果。\n")
            elif text_callback:
                text_callback(f"[注意] {len(dem_paths)} 幅 DEM 不自動拼接 ({blocker})，改為逐幅裁切。\n")

        jobs: List[Tuple[TrackType, List[Path]]] = []
        for file_path, track in zip(valid_paths, tracks):
            if mosaic_paths and track == TrackType.TERRAIN_DEM:
                if file_path == mosaic_paths[0]:
                    jobs.append((track, mosaic_paths))
                continue
            jobs.append((track, [file_path]))

        total_jobs = len(jobs)
        for idx, (track, job_paths) in enumerate(jobs, start=1):
            file_path = job_paths[0]
            job_label = file_path.name if len(job_paths) == 1 else f"{file_path.name} 等 {len(job_paths)} 幅 DEM"
            base_percent = ((idx - 1) / total_jobs) * 100.0
            next_percent = (idx / total_jobs) * 100.0

            def _sub_progress(p: float, msg: str) -> None:
                if progress_callback:
                    combined = base_percent + (p / 100.0) * (next_percent - base_percent)
                    progress_callback(combined, f"[{idx}/{total_jobs}] {job_label}: {msg}")

            if text_callback:
                text_callback(f"--> [{idx}/{total_jobs}] 檔案: {job_label} | 判定類別: {track.value}\n")

            try:
                if track == TrackType.VECTOR_CADASTRE:
                    suffix = WHOLE_OUTPUT_SUFFIX if self.keep_whole_features else "_clipped"
                    out_name = f"{file_path.stem}{suffix}.shp"
                    out_path = self.output_dir / out_name
                    job_res = clip_vector_layer(
                        file_path,
                        self.master_boundary,
                        out_path,
                        target_srs=self.target_srs,
                        assigned_srs=self.assigned_srs,
                        excel_output_path=out_path.with_suffix(".xlsx") if self.export_attribute_excel else None,
                        keep_whole=self.keep_whole_features,
                        progress_callback=_sub_progress,
                        text_callback=text_callback,
                    )
                elif track == TrackType.TERRAIN_DEM:
                    # 解析度用 :g，0.5 m 寫成 _0.5m (整數仍是 _10m)
                    res_tag = f"_{self.dem_target_res:g}m" if self.dem_target_res else ""
                    if len(job_paths) > 1:
                        out_name = f"{DEM_MOSAIC_OUTPUT_STEM}{res_tag}.tif"
                    else:
                        out_name = f"{file_path.stem}_dem{res_tag}.tif"
                    out_path = self.output_dir / out_name
                    job_res = clip_dem_layer(
                        job_paths if len(job_paths) > 1 else file_path,
                        self.master_boundary,
                        out_path,
                        target_resolution=self.dem_target_res,
                        resampling_method=self.dem_resampling,
                        target_srs=self.target_srs,
                        assigned_srs=self.assigned_srs,
                        progress_callback=_sub_progress,
                        text_callback=text_callback,
                    )
                elif track == TrackType.ORTHO_AERIAL:
                    res_tag = f"_{self.ortho_target_res:g}m" if self.ortho_target_res else ""
                    out_name = f"{file_path.stem}_ortho{res_tag}.tif"
                    out_path = self.output_dir / out_name
                    job_res = clip_ortho_layer(
                        file_path,
                        self.master_boundary,
                        out_path,
                        generate_pyramids=self.generate_pyramids,
                        export_cad_background=self.export_cad_bg,
                        target_resolution=self.ortho_target_res,
                        target_srs=self.target_srs,
                        assigned_srs=self.assigned_srs,
                        progress_callback=_sub_progress,
                        text_callback=text_callback,
                    )
                else:
                    if text_callback:
                        text_callback(f"[略過] 無法判定圖資型態: {file_path.name}\n")
                    job_res = ClipJobResult(
                        input_path=file_path,
                        output_path=file_path,
                        track_type=TrackType.UNKNOWN,
                        is_success=False,
                        duration_seconds=0.0,
                        error_message="不支援的圖資類型",
                    )

                results.append(job_res)

            except Exception as err:
                logger.exception("裁切處理發生例外: %s", err)
                if text_callback:
                    text_callback(f"【錯誤】{job_label} 處理失敗: {err}\n")
                results.append(
                    ClipJobResult(
                        input_path=file_path,
                        output_path=file_path,
                        track_type=track,
                        is_success=False,
                        duration_seconds=0.0,
                        error_message=str(err),
                        merged_input_paths=list(job_paths) if len(job_paths) > 1 else None,
                    )
                )

        if progress_callback:
            progress_callback(100.0, "批次裁切全部完成")
        if text_callback:
            success_count = sum(1 for r in results if r.is_success)
            text_callback(f"三軌批次裁切全部作業完成：成功 {success_count} 件 / 失敗 {len(results) - success_count} 件。\n")

        return results
