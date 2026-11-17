"""地形高程、坡度坡向、兩期土方、縱橫斷面與水文集水區分析模組 (core/terrain_lab.py)

嚴格遵守專案規格書第 0 節與第 3.4 節鐵律：
1. 嚴禁使用 geopandas, rasterio, shapely, pyproj, arcpy。
2. 允許使用：numpy (高程矩陣運算與土方統計)、openpyxl (土方與水保坡度報表輸出)、Pillow。
3. [G-14] 地形衍生分析：調用 gdaldem slope (支援切換百分比 % 與角度 °)、aspect、hillshade、TRI、TPI、roughness。
4. [G-15] 等高線抽繪：調用 gdal_contour 依指定間距產出帶 ELEV 高程屬性之 SHP 或 DXF。
5. [A-01] 兩期 DEM 高程差 (Delta Z) 與挖填土方計算：
   - 將前期與後期 DEM 以相同網格矩陣對齊 (gdalwarp -tap -tr -cutline)。
   - 利用 numpy 矩陣精確計算 Delta Z，統計總挖方、總填方、淨土方差 (m³)，產出紅藍渲染高程變化圖與 Excel 報表。
6. [A-02] 水保/道路規範 1~6 級坡度自動分類：
   - 計算百分比坡度圖並以 numpy 重分類為 1~6 級整數網格。
   - 呼叫 gdal_polygonize 轉為多邊形向量，利用 openpyxl 輸出含面積 (ha, m²) 與佔比之標準規範清冊。
7. [A-03] 路線縱橫斷面地面高程自動擷取：
   - 依道路中心線與指定樁號間距 (如 20m) 內插里程樁號與法線橫斷面點。
   - 調用 gdallocationinfo 批次高速查詢地面高程，匯出樁號高程表 (CSV/Excel) 與斷面點位向量圖資。
8. [A-04] 水文集水區劃設 (WhiteboxTools CLI)：
   - 串接 whitebox_tools 原生執行檔執行填窪、D8 流向、匯流累積與出水口捕捉上游集水區圈繪。
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# 允許使用之第三方套件
import numpy as np
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from osgeo import gdal

from core import geo_io
from core.crs_doctor import (
    EPSG_TWD97_121,
    build_tap_parameters,
    _read_raster_srs_metadata,
    decide_and_report_source_srs,
    get_raster_resolution,
    resolve_assigned_srs,
    resolve_warp_resolution,
    srs_is_geographic,
)
from core.env_runner import (
    CommandResult,
    ToolkitError,
    get_toolkit_root,
    get_workspace_temp_dir,
    resolve_binary,
    run_command,
    run_json_command,
)

logger = logging.getLogger("core.terrain_lab")


# ============================================================================
# 資料結構定義
# ============================================================================

@dataclass
class EarthworkResult:
    """兩期 DEM 土方體積計算成果結構。"""

    before_dem: Path
    after_dem: Path
    diff_raster_path: Path
    excel_report_path: Path
    total_cut_m3: float       # 總挖方體積 (立方公尺)
    total_fill_m3: float      # 總填方體積 (立方公尺)
    net_volume_m3: float      # 淨土方差 (填方 - 挖方)
    max_cut_depth_m: float    # 最大開挖深度 (公尺)
    max_fill_height_m: float  # 最大填土高度 (公尺)
    mean_dz_m: float          # 平均高程差 (公尺)
    effective_area_m2: float  # 計算有效面積 (平方公尺)
    cell_size_m: float        # 網格單元解析度 (公尺)
    duration_seconds: float
    color_raster_path: Optional[Path] = None  # 紅藍色階圖 (RGBA)：挖方紅、填方藍、無變化白
    color_range_m: float = 0.0                # 色階兩端對應的高程差 (±公尺)


@dataclass
class SlopeClassificationResult:
    """水保 1~6 級坡度分類運算成果結構。"""

    slope_pct_raster: Path
    classified_raster: Path
    polygon_vector_path: Path
    excel_report_path: Path
    class_statistics: List[Dict[str, Any]]
    total_area_ha: float
    total_area_m2: float
    duration_seconds: float


@dataclass
class CrossSectionPoint:
    """道路斷面樁號抽高點資料結構。"""

    station_label: str       # 樁號 (例如 0K+020)
    station_distance: float  # 沿路線累積里程 (公尺)
    offset_m: float          # 橫斷面偏移量 (0 為中心線，負為左側，正為右側)
    x: float
    y: float
    elevation_z: Optional[float]  # 地面高程值 (公尺)；落在 DEM 範圍外或 NoData 時為 None
    point_type: str = "CENTERLINE"  # CENTERLINE、CROSS_SECTION_LEFT 或 CROSS_SECTION_RIGHT


@dataclass
class WatershedResult:
    """集水區劃設分析成果結構。"""

    dem_path: Path
    d8_pntr_raster: Path
    flow_accum_raster: Path
    watershed_raster: Path
    watershed_polygon: Path
    pour_point_coord: Tuple[float, float]
    catchment_area_ha: float
    catchment_area_m2: float
    duration_seconds: float


# ============================================================================
# [G-14] 地形衍生計算 (gdaldem 坡度、坡向、陰影與粗糙度)
# ============================================================================

def calculate_slope(
    dem_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    unit: str = "PERCENT",  # "PERCENT" (百分比 %) 或 "DEGREE" (角度 °)
    compute_edges: bool = True,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 gdaldem slope 計算地形坡度網格。

    參數：
        unit: PERCENT 表示百分比坡度 (-p 參數)；DEGREE 表示角度 (0~90°)。
    """
    in_dem = Path(dem_path).resolve()
    out_slope = Path(output_path).resolve()
    out_slope.parent.mkdir(parents=True, exist_ok=True)

    if not in_dem.exists():
        raise FileNotFoundError(f"找不到輸入高程圖資: {in_dem}")

    cmd = ["gdaldem", "slope", str(in_dem), str(out_slope), "-co", "COMPRESS=LZW"]

    if unit.upper() == "PERCENT":
        cmd.append("-p")

    if compute_edges:
        cmd.append("-compute_edges")

    if text_callback:
        unit_str = "百分比 (%)" if unit.upper() == "PERCENT" else "角度 (°)"
        text_callback(f"[地形分析] 正在計算地形坡度網格 ({unit_str})。\n")

    run_command(cmd, text_callback=text_callback, check=True)
    return out_slope


def calculate_aspect(
    dem_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    compute_edges: bool = True,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 gdaldem aspect 計算坡向網格 (0°~360°，0° 為正北，順時針)。"""
    in_dem = Path(dem_path).resolve()
    out_aspect = Path(output_path).resolve()
    out_aspect.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "gdaldem", "aspect",
        str(in_dem),
        str(out_aspect),
        "-co", "COMPRESS=LZW",
    ]
    if compute_edges:
        cmd.append("-compute_edges")

    if text_callback:
        text_callback("[地形分析] 正在計算地形坡向網格 (Aspect)。\n")

    run_command(cmd, text_callback=text_callback, check=True)
    return out_aspect


def calculate_terrain_indices(
    dem_path: Union[str, Path],
    output_path: Union[str, Path],
    index_type: str = "TRI",  # "TRI" (地形崎嶇度), "TPI" (位置指數), "roughness" (粗糙度)
    *,
    compute_edges: bool = True,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 gdaldem 計算 TRI, TPI 或 Roughness 地形特徵指數。"""
    in_dem = Path(dem_path).resolve()
    out_idx = Path(output_path).resolve()
    out_idx.parent.mkdir(parents=True, exist_ok=True)

    valid_types = {"TRI", "TPI", "roughness"}
    clean_type = index_type if index_type in valid_types else "TRI"

    cmd = [
        "gdaldem", clean_type,
        str(in_dem),
        str(out_idx),
        "-co", "COMPRESS=LZW",
    ]
    if compute_edges:
        cmd.append("-compute_edges")

    if text_callback:
        text_callback(f"[地形分析] 正在計算地形指標 ({clean_type})。\n")

    run_command(cmd, text_callback=text_callback, check=True)
    return out_idx


# ============================================================================
# [G-15] 等高線自動抽繪 (gdal_contour 轉 SHP / DXF)
# ============================================================================

def generate_contours(
    dem_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    interval: float = 5.0,
    elevation_attribute: str = "ELEV",
    output_format: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """呼叫 gdal_contour 依指定間距自高程 DEM 自動提取等高線。

    支援規格（規格書 3.4 節）：
    - 預設間距 interval = 5.0m (首曲線) 或 10.0m / 25.0m (計曲線)。
    - 高程屬性欄位名稱預設為 ELEV。
    - 支援輸出為 ESRI Shapefile、GeoPackage 或 AutoCAD DXF。
    - DEM 有座標系就用來源的；沒有時才採用 assigned_srs，未指派則依座標範圍推測。
    """
    in_dem = Path(dem_path).resolve()
    out_contour = Path(output_path).resolve()
    out_contour.parent.mkdir(parents=True, exist_ok=True)

    if not in_dem.exists():
        raise FileNotFoundError(f"找不到高程 DEM: {in_dem}")
    if interval <= 0:
        raise ValueError(f"等高線等高距必須大於 0，目前輸入: {interval}")

    fmt = geo_io.infer_vector_driver(out_contour, output_format)
    srs_decision = decide_and_report_source_srs(in_dem, assigned_srs, text_callback=text_callback)

    if text_callback:
        text_callback(f"[等高線] 正在抽繪等高線 (間距: {interval:g}m，格式: {fmt})。\n")

    # gdal_contour 直接輸出 DXF 會失敗且留下空檔，一律先出暫存 GPKG 再匯出；-3d 讓等高線帶 Z 值
    with geo_io.TempVectorFiles("contour") as temp_files:
        temp_gpkg = temp_files.new("lines")
        cmd = [
            "gdal_contour",
            "-a", elevation_attribute,
            "-i", str(interval),
            "-3d",
            "-f", "GPKG",
            "-nln", "contour",
            str(in_dem),
            str(temp_gpkg),
        ]
        run_command(
            cmd,
            progress_callback=geo_io.scaled_progress(progress_callback, 0.0, 80.0),
            text_callback=text_callback,
            check=True,
        )
        geo_io.export_vector(
            temp_gpkg,
            "contour",
            out_contour,
            output_format=fmt,
            dxf_layer_name="CONTOUR",
            extra_args=[*srs_decision.as_ogr_args(), "-t_srs", target_srs],
            progress_callback=geo_io.scaled_progress(progress_callback, 80.0, 100.0),
            text_callback=text_callback,
        )
    return out_contour


# ============================================================================
# 輔助函式：以沙盒內建的 osgeo.gdal 讀寫網格陣列 (不經 ASCII Grid 文字檔)
# ============================================================================

def _read_geotiff_to_numpy(raster_path: Path) -> Tuple[np.ndarray, Dict[str, Any]]:
    """把網格第 1 波段讀成 float32 二維陣列，並回傳描述網格位置的 header。

    header 內容：ncols、nrows、geotransform (GDAL 六參數)、cellsize_x、cellsize_y (皆為正值)、
    cellsize (= cellsize_x，沿用舊鍵名)；來源有設定 NoData 時才有 nodata_value。
    """
    geo_io.ensure_gdal_ready()
    try:
        dataset = gdal.Open(str(raster_path), gdal.GA_ReadOnly)
    except RuntimeError as err:
        raise ToolkitError(f"無法開啟網格檔: {raster_path.name} ({err})") from err
    try:
        band = dataset.GetRasterBand(1)
        geotransform = tuple(float(v) for v in dataset.GetGeoTransform())
        nodata = band.GetNoDataValue()
        data_matrix = band.ReadAsArray().astype(np.float32, copy=False)
    finally:
        dataset = None

    cellsize_x = math.hypot(geotransform[1], geotransform[4])
    cellsize_y = math.hypot(geotransform[2], geotransform[5])
    header: Dict[str, Any] = {
        "ncols": int(data_matrix.shape[1]),
        "nrows": int(data_matrix.shape[0]),
        "geotransform": geotransform,
        "cellsize_x": cellsize_x,
        "cellsize_y": cellsize_y,
        "cellsize": cellsize_x,
    }
    if nodata is not None:
        header["nodata_value"] = float(nodata)
    return (data_matrix, header)


def _write_numpy_to_geotiff(
    data_matrix: np.ndarray,
    header: Dict[str, Any],
    output_geotiff: Path,
    *,
    nodata_value: float = -9999.0,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
) -> Path:
    """將 NumPy 二維矩陣寫成 float32 GeoTIFF；位置取自 header["geotransform"] (陣列左上角)。

    陣列先以 osgeo 寫成未壓縮的暫存檔，座標系標籤、NoData 與 LZW 壓縮仍交給 gdal_translate。
    """
    if data_matrix.ndim != 2:
        raise ValueError(f"只能寫出二維矩陣，收到的維度為 {data_matrix.ndim}。")
    geotransform = header.get("geotransform")
    if geotransform is None or len(geotransform) != 6:
        raise ValueError("header 缺少 geotransform，無法決定網格位置。")

    geo_io.ensure_gdal_ready()
    temp_dir = get_workspace_temp_dir()
    temp_tif = temp_dir / f"_temp_out_{output_geotiff.stem}.tif"
    rows, cols = data_matrix.shape

    try:
        dataset = gdal.GetDriverByName("GTiff").Create(str(temp_tif), cols, rows, 1, gdal.GDT_Float32)
        try:
            dataset.SetGeoTransform([float(v) for v in geotransform])
            band = dataset.GetRasterBand(1)
            band.WriteArray(np.ascontiguousarray(data_matrix, dtype=np.float32))
            band.FlushCache()
        finally:
            dataset = None

        cmd = [
            "gdal_translate",
            "-of", "GTiff",
            "-a_srs", target_srs,
            "-a_nodata", str(nodata_value),
            "-co", "COMPRESS=LZW",
            str(temp_tif),
            str(output_geotiff),
        ]
        run_command(cmd, check=True)
    finally:
        try:
            temp_tif.unlink(missing_ok=True)
        except OSError as err:
            logger.debug("清理中繼 GeoTIFF 略過: %s", err)

    return output_geotiff


def _write_raster_srs_wkt(raster_path: Path, wkt_path: Path) -> Path:
    """把網格自帶的座標系寫成 WKT 檔，供 gdal_translate -a_srs 原樣標回衍生成果。"""
    info_json = run_json_command(["gdalinfo", "-json", str(raster_path)])
    crs_info = info_json.get("coordinateSystem")
    wkt = crs_info.get("wkt") if isinstance(crs_info, dict) else crs_info
    if not wkt or not str(wkt).strip():
        raise ToolkitError(f"讀不到網格的座標系定義: {raster_path.name}")
    wkt_path.write_text(str(wkt), encoding="utf-8")
    return wkt_path


# ============================================================================
# [A-01] 兩期 DEM 高程差 (Delta Z) 與挖填土方計算引擎
# ============================================================================

# 高程差色階圖：挖方 (ΔZ < 0) 由淺紅到深紅、填方 (ΔZ > 0) 由淺藍到深藍、無變化為白色
# 每列為 (相對於色階範圍的比例, R, G, B)
_EARTHWORK_COLOR_STOPS: Tuple[Tuple[float, int, int, int], ...] = (
    (-1.0, 178, 24, 43),
    (-0.5, 239, 138, 98),
    (0.0, 255, 255, 255),
    (0.5, 103, 169, 207),
    (1.0, 33, 102, 172),
)
# 兩期完全沒有高程變化時色階範圍的下限 (公尺)，避免色階表的值全部擠在 0
_EARTHWORK_MIN_COLOR_RANGE_M: float = 0.01


def _default_earthwork_color_path(diff_raster: Path) -> Path:
    """由高程差圖的檔名推出色階圖檔名：xxx_dz.tif -> xxx_color.tif，其餘在檔名後加 _color。"""
    stem = diff_raster.stem
    base = stem[:-3] if stem.lower().endswith("_dz") else stem
    return diff_raster.with_name(f"{base}_color.tif")


def render_earthwork_color_relief(
    diff_raster_path: Union[str, Path],
    output_color_raster: Union[str, Path],
    color_range_m: float,
    *,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """把高程差 (ΔZ) 網格以 gdaldem color-relief 渲染成紅藍雙色 RGBA GeoTIFF。

    色階以 0 為中心左右對稱：-color_range_m 為深紅 (挖方)、0 為白、+color_range_m 為深藍 (填方)，
    超出範圍的值維持兩端的顏色；NoData 像元輸出為全透明。
    """
    diff_p = Path(diff_raster_path).resolve()
    out_p = Path(output_color_raster).resolve()
    if not diff_p.exists():
        raise FileNotFoundError(f"高程差網格不存在: {diff_p}")
    if out_p == diff_p:
        raise ToolkitError(f"色階圖不可與高程差圖同一個檔案: {out_p}")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    span = max(float(color_range_m), _EARTHWORK_MIN_COLOR_RANGE_M)

    with geo_io.TempVectorFiles("dzcolor") as temp_files:
        ramp_path = temp_files.new("ramp", ".txt")
        ramp_lines = ["nv 0 0 0 0"]
        ramp_lines.extend(f"{ratio * span:.6f} {r} {g} {b} 255" for ratio, r, g, b in _EARTHWORK_COLOR_STOPS)
        ramp_path.write_text("\n".join(ramp_lines) + "\n", encoding="ascii")
        run_command(
            [
                "gdaldem", "color-relief",
                str(diff_p), str(ramp_path), str(out_p),
                "-alpha",
                "-of", "GTiff",
                "-co", "COMPRESS=LZW",
            ],
            text_callback=text_callback,
            check=True,
        )
    if text_callback:
        text_callback(
            f"[土方計算] 已輸出紅藍色階圖 {out_p.name}：紅色為挖方、藍色為填方、白色為無變化，"
            f"色階範圍 ±{span:.2f} m。\n"
        )
    return out_p


def calculate_earthwork(
    before_dem_path: Union[str, Path],
    after_dem_path: Union[str, Path],
    master_boundary_path: Optional[Union[str, Path]] = None,
    *,
    output_diff_raster: Optional[Union[str, Path]] = None,
    output_excel_path: Optional[Union[str, Path]] = None,
    output_color_raster: Optional[Union[str, Path]] = None,
    target_resolution: Optional[float] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> EarthworkResult:
    """執行 [A-01] 兩期 DEM 土方體積計算、Delta Z 矩陣差值與 Excel 統計報表輸出。

    兩期 DEM 與邊界各自判斷來源座標系：有座標系就用來源的；沒有時才採用 assigned_srs，
    未指派則依座標範圍推測 (見 crs_doctor.decide_source_srs)。

    嚴格流程（規格書 3.4 節）：
    1. 同步以 gdalwarp 對齊前期 (before) 與後期 (after) DEM 之解析度與邊界網格 (-tap -tr -cutline)。
    2. 透過 NumPy 計算 Delta Z = Z_after - Z_before（自動濾除 NoData 像元）。
    3. 統計總挖方 (Cut)、總填方 (Fill)、淨土方差 (Net) 與開挖/填土極值。
    4. 產出高程差 GeoTIFF、紅藍色階圖 (挖方紅、填方藍；未指定 output_color_raster 時放在高程差圖旁，
       檔名 xxx_dz.tif -> xxx_color.tif) 與帶千分位格式之 Excel 土方明細清冊。
    """
    start_time = time.time()
    in_before = Path(before_dem_path).resolve()
    in_after = Path(after_dem_path).resolve()
    temp_dir = get_workspace_temp_dir()

    if not in_before.exists() or not in_after.exists():
        raise FileNotFoundError(f"前期或後期 DEM 檔案不存在: before={in_before}, after={in_after}")

    final_diff_tif = (
        Path(output_diff_raster).resolve()
        if output_diff_raster
        else (temp_dir / "earthwork_diff_dz.tif")
    )
    final_excel = (
        Path(output_excel_path).resolve()
        if output_excel_path
        else (temp_dir / "earthwork_summary_report.xlsx")
    )
    final_color_tif = (
        Path(output_color_raster).resolve()
        if output_color_raster
        else _default_earthwork_color_path(final_diff_tif)
    )
    if final_color_tif == final_diff_tif:
        raise ToolkitError(f"色階圖不可與高程差圖同一個檔案: {final_color_tif}")
    final_diff_tif.parent.mkdir(parents=True, exist_ok=True)
    final_excel.parent.mkdir(parents=True, exist_ok=True)

    if srs_is_geographic(target_srs):
        raise ToolkitError(f"土方體積要以公尺計算，目標座標系不可為經緯度: {target_srs}。請改用 TWD97 等投影座標系。")

    # 1. 決定目標對齊解析度 (-tr)，單位為目標座標系的公尺
    before_decision = decide_and_report_source_srs(in_before, assigned_srs, text_callback=text_callback)
    res, _ = resolve_warp_resolution(
        in_before,
        before_decision,
        target_srs,
        target_resolution if target_resolution and target_resolution > 0 else None,
        text_callback=text_callback,
    )

    if text_callback:
        text_callback(f"[土方計算] 正在將兩期 DEM 統一對齊至 {res:g}m 網格矩陣。\n")

    # 2. 以 gdalwarp 雙軌嚴密對齊
    aligned_before = temp_dir / "_temp_aligned_before.tif"
    aligned_after = temp_dir / "_temp_aligned_after.tif"

    warp_options = [
        "-t_srs", target_srs,
        "-r", "bilinear",
        "-dstnodata", "-9999",
        "-co", "COMPRESS=LZW",
        "-overwrite",
    ] + build_tap_parameters(target_res_x=res, target_res_y=res)

    with geo_io.TempVectorFiles("earthwork") as temp_files:
        if master_boundary_path and Path(master_boundary_path).exists():
            # 邊界先正規化並重投影到目標座標系，裁切線才不會因為沒有標籤或座標系不同而對錯位置
            bound_p = Path(master_boundary_path).resolve()
            bound_decision = decide_and_report_source_srs(bound_p, assigned_srs, text_callback=text_callback)
            cutline_gpkg = temp_files.new("cutline")
            geo_io.stage_vector_to_gpkg(
                bound_p,
                cutline_gpkg,
                stage_layer="cutline",
                target_srs=target_srs,
                source_srs=bound_decision.s_srs,
                make_valid=True,
            )
            warp_options.extend(["-cutline", str(cutline_gpkg), "-crop_to_cutline"])

        # 兩期 DEM 各自判斷：有座標系就用來源的，不帶 -s_srs
        for source_dem, aligned_dem in ((in_before, aligned_before), (in_after, aligned_after)):
            if source_dem == in_before:
                dem_decision = before_decision
            else:
                dem_decision = decide_and_report_source_srs(source_dem, assigned_srs, text_callback=text_callback)
            run_command(
                ["gdalwarp", *dem_decision.as_ogr_args(), *warp_options, str(source_dem), str(aligned_dem)],
                check=True,
            )

    if progress_callback:
        progress_callback(40.0, "網格對齊完成，正在計算高程差值矩陣。")

    # 3. 載入 NumPy 矩陣進行 Delta Z 運算
    mat_before, header = _read_geotiff_to_numpy(aligned_before)
    mat_after, header_after = _read_geotiff_to_numpy(aligned_after)

    # 兩期範圍不一定相同 (沒有邊界裁切時各自保留原範圍)：依左上角座標換算出重疊視窗再相減，
    # 兩張都經過 -tap 對齊，原點差必為整數格
    gt_before = header["geotransform"]
    gt_after = header_after["geotransform"]
    col_shift = int(round((gt_after[0] - gt_before[0]) / gt_before[1]))
    row_shift = int(round((gt_after[3] - gt_before[3]) / gt_before[5]))
    b_col0, a_col0 = max(col_shift, 0), max(-col_shift, 0)
    b_row0, a_row0 = max(row_shift, 0), max(-row_shift, 0)
    min_cols = min(mat_before.shape[1] - b_col0, mat_after.shape[1] - a_col0)
    min_rows = min(mat_before.shape[0] - b_row0, mat_after.shape[0] - a_row0)
    if min_cols <= 0 or min_rows <= 0:
        raise ToolkitError("兩期 DEM 的範圍沒有重疊，無法計算土方。")
    b_mat = mat_before[b_row0:b_row0 + min_rows, b_col0:b_col0 + min_cols]
    a_mat = mat_after[a_row0:a_row0 + min_rows, a_col0:a_col0 + min_cols]

    # 有效遮罩 (排除 NoData 與無效值)
    nodata_val = float(header.get("nodata_value", -9999.0))
    valid_mask = (
        (b_mat > -9000.0)
        & (a_mat > -9000.0)
        & (~np.isnan(b_mat))
        & (~np.isnan(a_mat))
        & (b_mat != nodata_val)
        & (a_mat != nodata_val)
    )

    cell_area = float(res * res)
    valid_count = int(np.count_nonzero(valid_mask))

    if valid_count == 0:
        raise ToolkitError("兩期 DEM 在重疊交集區域內無任何有效高程數據像元！")

    # Delta Z = 後期高程 - 前期高程
    diff_matrix = np.full_like(b_mat, nodata_val, dtype=np.float32)
    diff_matrix[valid_mask] = a_mat[valid_mask] - b_mat[valid_mask]

    dz_valid = diff_matrix[valid_mask]

    # 4. 挖填土方統計計算 (立方公尺)
    # 填方 (Fill): Delta Z > 0 (堆積/增高)
    fill_mask = dz_valid > 0.0
    fill_dz = dz_valid[fill_mask]
    total_fill_m3 = float(np.sum(fill_dz) * cell_area)
    max_fill_height_m = float(np.max(fill_dz)) if len(fill_dz) > 0 else 0.0

    # 挖方 (Cut): Delta Z < 0 (開挖/沉陷/沖刷)
    cut_mask = dz_valid < 0.0
    cut_dz = np.abs(dz_valid[cut_mask])
    total_cut_m3 = float(np.sum(cut_dz) * cell_area)
    max_cut_depth_m = float(np.max(cut_dz)) if len(cut_dz) > 0 else 0.0

    # 淨土方差 (填方 - 挖方)
    net_volume_m3 = total_fill_m3 - total_cut_m3
    mean_dz = float(np.mean(dz_valid))
    effective_area_m2 = float(valid_count * cell_area)

    # 5. 輸出高程差 Delta Z GeoTIFF 檔案
    header["nrows"] = min_rows
    header["ncols"] = min_cols
    header["geotransform"] = (
        gt_before[0] + b_col0 * gt_before[1], gt_before[1], gt_before[2],
        gt_before[3] + b_row0 * gt_before[5], gt_before[4], gt_before[5],
    )
    _write_numpy_to_geotiff(diff_matrix, header, final_diff_tif, nodata_value=nodata_val, target_srs=target_srs)

    # 紅藍色階圖：色階範圍取挖填極值中較大者，左右對稱
    color_range_m = max(max_fill_height_m, max_cut_depth_m, _EARTHWORK_MIN_COLOR_RANGE_M)
    if progress_callback:
        progress_callback(70.0, "正在渲染紅藍色階圖。")
    render_earthwork_color_relief(final_diff_tif, final_color_tif, color_range_m, text_callback=text_callback)

    # 6. 利用 openpyxl 輸出專業工程土方計算統計 Excel 報表
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "土方計算統計表"

    # 表頭樣式
    title_font = Font(name="Microsoft JhengHei UI", size=14, bold=True, color="1F497D")
    header_font = Font(name="Microsoft JhengHei UI", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Microsoft JhengHei UI", size=10)
    total_font = Font(name="Microsoft JhengHei UI", size=11, bold=True, color="9C0006")

    header_fill = PatternFill(start_color="2F4F4F", end_color="2F4F4F", fill_type="solid")
    fill_bg = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    cut_bg = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    ws.merge_cells("A1:D1")
    ws["A1"] = "工程專案兩期 DEM 高程差與挖填土方計算統計表"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 35

    report_meta = [
        ("前期 DEM 檔案名稱", in_before.name),
        ("後期 DEM 檔案名稱", in_after.name),
        ("網格像元解析度", f"{res:g} m × {res:g} m"),
        ("有效計算面積", f"{effective_area_m2:,.2f} m² ({effective_area_m2 / 10000.0:,.4f} 公頃)"),
    ]

    for r_idx, (lbl, val) in enumerate(report_meta, start=2):
        ws[f"A{r_idx}"] = lbl
        ws[f"B{r_idx}"] = val
        ws[f"A{r_idx}"].font = Font(name="Microsoft JhengHei UI", size=9, bold=True, color="595959")
        ws[f"B{r_idx}"].font = data_font

    table_headers = ["項目名稱", "統計數值", "計量單位", "工程備註說明"]
    for col_idx, h_text in enumerate(table_headers, start=1):
        cell = ws.cell(row=7, column=col_idx, value=h_text)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[7].height = 25

    table_data = [
        ("總填方體積 (Fill Volume)", total_fill_m3, "m³ (立方公尺)", "高程增高區域堆積土方總量"),
        ("總挖方體積 (Cut Volume)", total_cut_m3, "m³ (立方公尺)", "開挖與沖刷沉陷區域土方總量"),
        ("淨土方差 (Net Difference)", net_volume_m3, "m³ (立方公尺)", "填方減挖方。正值：填方大於挖方，土方不足需借土運入；負值：挖方大於填方，有剩餘土方需外運 (未計鬆實方係數)"),
        ("最大填土高度 (Max Fill)", max_fill_height_m, "m (公尺)", "單點最大抬升垂直距離"),
        ("最大開挖深度 (Max Cut)", max_cut_depth_m, "m (公尺)", "單點最大下挖垂直距離"),
        ("平均高程變化 (Mean ΔZ)", mean_dz, "m (公尺)", "全工區平均垂直高程差"),
    ]

    for r_idx, row_items in enumerate(table_data, start=8):
        for c_idx, val in enumerate(row_items, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.font = data_font
            cell.border = thin_border
            if c_idx == 2 and isinstance(val, float):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")
                if "填方" in row_items[0]:
                    cell.fill = fill_bg
                elif "挖方" in row_items[0]:
                    cell.fill = cut_bg
            elif c_idx == 1:
                cell.font = Font(name="Microsoft JhengHei UI", bold=True)

    # 自動調整欄寬
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

    wb.save(final_excel)

    # 清理對齊暫存圖
    for tmp_tif in (aligned_before, aligned_after):
        try:
            tmp_tif.unlink()
        except OSError as err:
            logger.debug("清理暫存對齊 DEM 略過: %s", err)

    duration = time.time() - start_time
    if progress_callback:
        progress_callback(100.0, "土方計算與報表產出完成")

    return EarthworkResult(
        before_dem=in_before,
        after_dem=in_after,
        diff_raster_path=final_diff_tif,
        excel_report_path=final_excel,
        total_cut_m3=round(total_cut_m3, 2),
        total_fill_m3=round(total_fill_m3, 2),
        net_volume_m3=round(net_volume_m3, 2),
        max_cut_depth_m=round(max_cut_depth_m, 2),
        max_fill_height_m=round(max_fill_height_m, 2),
        mean_dz_m=round(mean_dz, 3),
        effective_area_m2=round(effective_area_m2, 2),
        cell_size_m=res,
        duration_seconds=duration,
        color_raster_path=final_color_tif,
        color_range_m=round(color_range_m, 3),
    )


# ============================================================================
# [A-02] 水保/道路規範 1～6 級坡度自動分類與面積統計
# ============================================================================

# 臺灣水土保持技術規範第 16 條標準門檻 (百分比 %)
DEFAULT_SLOPE_THRESHOLDS: List[Tuple[int, str, float, float]] = [
    (1, "一級坡 (≦ 5%)", 0.0, 5.0),
    (2, "二級坡 (5% ~ 15%)", 5.0, 15.0),
    (3, "三級坡 (15% ~ 30%)", 15.0, 30.0),
    (4, "四級坡 (30% ~ 40%)", 30.0, 40.0),
    (5, "五級坡 (40% ~ 55%)", 40.0, 55.0),
    (6, "六級坡 (> 55%)", 55.0, 99999.0),
]


def _normalize_slope_thresholds(
    thresholds: Sequence[Tuple[int, str, float, float]],
) -> List[Tuple[int, str, float, float]]:
    """檢查坡度分級門檻並依下限由小到大排序。

    等級代碼須為 1～32767 的相異整數 (0 保留給無資料)；各級下限須小於上限，且相鄰兩級首尾相接，
    否則會有坡度落不到任何一級。
    """
    rules: List[Tuple[int, str, float, float]] = []
    for item in thresholds:
        if len(item) != 4:
            raise ValueError(f"坡度分級門檻每一級須為 (代碼, 名稱, 下限, 上限)，收到: {item!r}")
        level_num, level_name, min_val, max_val = item
        if isinstance(level_num, bool) or not isinstance(level_num, (int, np.integer)) or not 1 <= level_num <= 32767:
            raise ValueError(f"坡度等級代碼須為 1～32767 的整數 (0 保留給無資料)，收到: {level_num!r}")
        min_val, max_val = float(min_val), float(max_val)
        if not min_val < max_val:
            raise ValueError(f"坡度分級「{level_name}」的下限 {min_val:g} 須小於上限 {max_val:g}。")
        rules.append((int(level_num), str(level_name), min_val, max_val))

    if not rules:
        raise ValueError("坡度分級門檻不可為空。")
    if len({rule[0] for rule in rules}) != len(rules):
        raise ValueError("坡度等級代碼不可重複。")

    rules.sort(key=lambda rule: rule[2])
    for lower_rule, upper_rule in zip(rules, rules[1:]):
        if not math.isclose(lower_rule[3], upper_rule[2], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"坡度分級「{lower_rule[1]}」的上限 {lower_rule[3]:g} 與「{upper_rule[1]}」的下限 {upper_rule[2]:g} 沒有相接。"
            )
    return rules


def classify_slope_soil_conservation(
    dem_path: Union[str, Path],
    *,
    thresholds: Optional[List[Tuple[int, str, float, float]]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> SlopeClassificationResult:
    """執行 [A-02] 水保 1~6 級坡度重分類、多邊形化與 Excel 面積佔比統計。

    核心規範（規格書 3.4 節）：
    1. 調用 gdaldem slope -p 計算百分比坡度圖 slope_pct.tif。
    2. 以 NumPy 依照門檻將像元數值重分類為等級代碼 (預設 1 ~ 6；自訂門檻見 _normalize_slope_thresholds)。
    3. 呼叫 gdal_polygonize 轉為多邊形向量 (GPKG)。
    4. 統計各級坡度面積（公頃、m²）與百分比，用 openpyxl 輸出專業報表。

    這支不重投影：坡度圖、分級網格與多邊形沿用 DEM 自己的座標系，不會被改標成 target_srs (參數保留以維持相容)。
    DEM 沒有座標系標籤時才採用 assigned_srs，未指派則依座標範圍推測，並標到成果上。
    經緯度 DEM 的水平單位是度，百分比坡度與面積都無從計算，直接拋 ToolkitError。
    """
    start_time = time.time()
    in_dem = Path(dem_path).resolve()
    temp_dir = get_workspace_temp_dir()
    out_base_dir = Path(output_dir).resolve() if output_dir else temp_dir
    out_base_dir.mkdir(parents=True, exist_ok=True)

    rule_list = _normalize_slope_thresholds(thresholds or DEFAULT_SLOPE_THRESHOLDS)
    srs_decision = decide_and_report_source_srs(in_dem, assigned_srs, text_callback=text_callback)
    needs_label = bool(srs_decision.s_srs) and not srs_decision.has_embedded_srs
    if needs_label:
        dem_is_geographic = srs_is_geographic(srs_decision.s_srs)
    else:
        dem_is_geographic = _read_raster_srs_metadata(in_dem).is_geographic
    if dem_is_geographic:
        raise ToolkitError(
            f"{in_dem.name} 是經緯度座標 (水平單位為度)，無法計算百分比坡度與面積。"
            "請先用 DEM 裁切把它轉成 TWD97 等以公尺為單位的座標系。"
        )

    # 1. 計算百分比坡度圖；DEM 沒有座標系標籤時，把決定採用的座標系標到坡度圖上
    slope_pct_tif = out_base_dir / f"{in_dem.stem}_slope_pct.tif"
    if needs_label:
        with geo_io.TempVectorFiles("slope") as temp_files:
            unlabelled_slope = temp_files.new("pct", ".tif")
            calculate_slope(in_dem, unlabelled_slope, unit="PERCENT", text_callback=text_callback)
            run_command(
                [
                    "gdal_translate", "-q",
                    "-a_srs", srs_decision.s_srs,
                    "-co", "COMPRESS=LZW",
                    str(unlabelled_slope),
                    str(slope_pct_tif),
                ],
                check=True,
            )
    else:
        calculate_slope(in_dem, slope_pct_tif, unit="PERCENT", text_callback=text_callback)

    if progress_callback:
        progress_callback(35.0, "坡度計算完成，正在執行 1~6 級重分類。")

    # 2. 以 NumPy 讀取坡度網格進行條件重分類
    slope_mat, header = _read_geotiff_to_numpy(slope_pct_tif)
    nodata_val = float(header.get("nodata_value", -9999.0))
    valid_mask = (slope_mat > -9000.0) & (slope_mat != nodata_val) & (~np.isnan(slope_mat))

    classified_mat = np.zeros_like(slope_mat, dtype=np.int16)
    cell_area_m2 = float(header["cellsize_x"]) * float(header["cellsize_y"])

    total_valid_pixels = int(np.count_nonzero(valid_mask))
    if total_valid_pixels == 0:
        raise ToolkitError("坡度網格中無有效像元數據！")

    stats: List[Dict[str, Any]] = []

    # 依門檻順序而不是等級代碼決定開放端：最低一級含下限以下、最高一級不設上限，每個有效像元必落在某一級
    last_index = len(rule_list) - 1
    for rule_index, (level_num, level_name, min_val, max_val) in enumerate(rule_list):
        lvl_mask = valid_mask.copy()
        if rule_index > 0:
            lvl_mask &= slope_mat > min_val
        if rule_index < last_index:
            lvl_mask &= slope_mat <= max_val

        pixel_count = int(np.count_nonzero(lvl_mask))
        classified_mat[lvl_mask] = level_num

        area_m2 = pixel_count * cell_area_m2
        area_ha = area_m2 / 10000.0
        ratio_pct = (pixel_count / total_valid_pixels) * 100.0

        stats.append({
            "level": level_num,
            "name": level_name,
            "pixel_count": pixel_count,
            "area_m2": round(area_m2, 2),
            "area_ha": round(area_ha, 4),
            "ratio_pct": round(ratio_pct, 2),
        })

    # 將無效區設為 0
    classified_mat[~valid_mask] = 0

    # 3. 寫出重分類 GeoTIFF
    classified_tif = out_base_dir / f"{in_dem.stem}_slope_classified.tif"
    source_wkt_file = temp_dir / f"_temp_srs_{in_dem.stem}.prj"
    try:
        _write_numpy_to_geotiff(
            classified_mat.astype(np.float32),
            header,
            classified_tif,
            nodata_value=0.0,
            # 不重投影：DEM 自帶座標系時沿用原標籤 (含 TWD67 的 EPSG 標籤)
            target_srs=srs_decision.s_srs if needs_label else str(_write_raster_srs_wkt(in_dem, source_wkt_file)),
        )
    finally:
        source_wkt_file.unlink(missing_ok=True)

    if progress_callback:
        progress_callback(70.0, "正在向量化坡度多邊形。")

    # 4. 呼叫 gdal_polygonize 轉為向量多邊形
    polygon_gpkg = out_base_dir / f"{in_dem.stem}_slope_polygons.gpkg"
    geo_io.remove_vector_file(polygon_gpkg)

    cmd_poly = [
        resolve_binary("python"), "-m", "osgeo_utils.gdal_polygonize", "-q",
        str(classified_tif),
        "-b", "1",
        "-of", "GPKG",
        str(polygon_gpkg),
        "slope_classes",
        "SLOPE_CLASS",
    ]
    run_command(cmd_poly, check=True)

    # 5. 利用 openpyxl 輸出水保規範坡度清冊 Excel
    excel_path = out_base_dir / f"{in_dem.stem}_slope_conservation_report.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "水保坡度分類統計"

    header_font = Font(name="Microsoft JhengHei UI", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Microsoft JhengHei UI", size=10)
    total_font = Font(name="Microsoft JhengHei UI", size=11, bold=True, color="1F497D")
    fill_header = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    fill_total = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    ws.merge_cells("A1:E1")
    ws["A1"] = f"水土保持規範坡度分級面積統計表 ({in_dem.name})"
    ws["A1"].font = Font(name="Microsoft JhengHei UI", size=13, bold=True)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    headers = ["坡度等級", "分級規範說明", "面積 (平方公尺 m²)", "面積 (公頃 ha)", "佔比面積 (%)"]
    for c_idx, h_text in enumerate(headers, start=1):
        c = ws.cell(row=3, column=c_idx, value=h_text)
        c.font = header_font
        c.fill = fill_header
        c.alignment = Alignment(horizontal="center", vertical="center")

    tot_m2 = sum(s["area_m2"] for s in stats)
    tot_ha = sum(s["area_ha"] for s in stats)

    for r_idx, s in enumerate(stats, start=4):
        ws.cell(row=r_idx, column=1, value=f"第 {s['level']} 級").alignment = Alignment(horizontal="center")
        ws.cell(row=r_idx, column=2, value=s["name"])
        cell_m2 = ws.cell(row=r_idx, column=3, value=s["area_m2"])
        cell_ha = ws.cell(row=r_idx, column=4, value=s["area_ha"])
        cell_pct = ws.cell(row=r_idx, column=5, value=f"{s['ratio_pct']:.2f}%")

        cell_m2.number_format = "#,##0.00"
        cell_ha.number_format = "#,##0.0000"
        cell_m2.alignment = Alignment(horizontal="right")
        cell_ha.alignment = Alignment(horizontal="right")
        cell_pct.alignment = Alignment(horizontal="right")

        for c_i in range(1, 6):
            ws.cell(row=r_idx, column=c_i).border = thin_border
            ws.cell(row=r_idx, column=c_i).font = data_font

    # 合計列
    total_row = len(stats) + 4
    ws.cell(row=total_row, column=1, value="合計").alignment = Alignment(horizontal="center")
    ws.cell(row=total_row, column=2, value="全工區評估總面積")
    c_tot_m2 = ws.cell(row=total_row, column=3, value=tot_m2)
    c_tot_ha = ws.cell(row=total_row, column=4, value=tot_ha)
    c_tot_pct = ws.cell(row=total_row, column=5, value="100.00%")

    c_tot_m2.number_format = "#,##0.00"
    c_tot_ha.number_format = "#,##0.0000"
    for c_i in range(1, 6):
        cell = ws.cell(row=total_row, column=c_i)
        cell.font = total_font
        cell.fill = fill_total
        cell.border = thin_border

    # 自動調整欄寬
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 18)

    wb.save(excel_path)
    duration = time.time() - start_time

    if progress_callback:
        progress_callback(100.0, "坡度分類全部完成")

    return SlopeClassificationResult(
        slope_pct_raster=slope_pct_tif,
        classified_raster=classified_tif,
        polygon_vector_path=polygon_gpkg,
        excel_report_path=excel_path,
        class_statistics=stats,
        total_area_ha=round(tot_ha, 4),
        total_area_m2=round(tot_m2, 2),
        duration_seconds=duration,
    )


# ============================================================================
# [A-03] 路線縱橫斷面地面高程自動擷取 (Road Profile & Cross-Section)
# ============================================================================

# 中心線各段端點視為相接的容許距離 (公尺)
_CENTERLINE_JOIN_TOLERANCE_M = 0.01


def _read_line_parts(geojson_path: Path) -> List[List[Tuple[float, float]]]:
    """讀出 GeoJSON 內所有線段 (LineString 與 MultiLineString 的每一段) 的平面頂點。"""
    with open(geojson_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    parts: List[List[Tuple[float, float]]] = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        g_type = geom.get("type", "")
        raw_c = geom.get("coordinates") or []
        if g_type == "LineString":
            groups = [raw_c]
        elif g_type == "MultiLineString":
            groups = raw_c
        else:
            continue
        for group in groups:
            pts = [(float(pt[0]), float(pt[1])) for pt in group]
            if len(pts) >= 2:
                parts.append(pts)
    return parts


def _chain_line_parts(parts: List[List[Tuple[float, float]]], line_name: str) -> List[Tuple[float, float]]:
    """把端點相接的多段線串成一條連續中心線；方向以第一段為準，串不起來就拋錯。

    CAD 的中心線常是多個 LINE 圖元 (每個都是一個圖徵)，只取第一段會讓里程短少。
    """
    def _near(a: Tuple[float, float], b: Tuple[float, float]) -> bool:
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= _CENTERLINE_JOIN_TOLERANCE_M

    chain = list(parts[0])
    remaining = [list(part) for part in parts[1:]]
    progressed = True
    while remaining and progressed:
        progressed = False
        for idx, part in enumerate(remaining):
            if _near(chain[-1], part[0]):
                chain.extend(part[1:])
            elif _near(chain[-1], part[-1]):
                chain.extend(reversed(part[:-1]))
            elif _near(chain[0], part[-1]):
                chain[:0] = part[:-1]
            elif _near(chain[0], part[0]):
                chain[:0] = list(reversed(part[1:]))
            else:
                continue
            del remaining[idx]
            progressed = True
            break

    if remaining:
        raise ToolkitError(
            f"中心線不連續：{line_name} 共有 {len(parts)} 段線，其中 {len(remaining)} 段的端點接不上其他線段 "
            f"(容許 {_CENTERLINE_JOIN_TOLERANCE_M} m)。請先在 CAD／GIS 把中心線接成一條，或只保留要擷取的那一條。"
        )
    return chain


def _station_label(distance_m: float) -> str:
    """把里程 (公尺) 格式化成樁號標籤，例如 1020.5 → 1K+020.50。"""
    rounded = round(distance_m, 2)
    km = int(rounded // 1000)
    return f"{km}K+{rounded - km * 1000:06.2f}"


def _sample_dem_elevations(
    dem_path: Path,
    points: Sequence[Tuple[float, float]],
    nodata_value: Optional[float],
) -> List[Optional[float]]:
    """以 gdallocationinfo 批次查詢各點的 DEM 高程；落在 DEM 範圍外或 NoData 的點回傳 None。

    -valonly 對範圍外的點輸出空行 (而且只要有一點在範圍外 returncode 就是 1)，
    所以不能看 returncode、也不能濾掉空行：輸出要逐行對回輸入的點。
    """
    coords_text = "".join(f"{x:.4f} {y:.4f}\n" for x, y in points)
    result = run_command(
        ["gdallocationinfo", "-valonly", "-geoloc", str(dem_path)],
        input_text=coords_text,
        check=False,
    )
    value_lines = result.stdout.splitlines()
    if len(value_lines) != len(points):
        raise ToolkitError(
            f"gdallocationinfo 回傳 {len(value_lines)} 列，與查詢的 {len(points)} 個點數不符，無法對應樁號。\n"
            f"STDERR: {result.stderr.strip()}"
        )

    elevations: List[Optional[float]] = []
    for value_text in value_lines:
        value_text = value_text.strip()
        try:
            value = float(value_text) if value_text else None
        except ValueError:
            value = None
        if value is not None and (
            not math.isfinite(value)
            or (nodata_value is not None and math.isclose(value, nodata_value, rel_tol=1e-9, abs_tol=1e-6))
        ):
            value = None
        elevations.append(value)
    return elevations


def extract_road_profile_and_cross_sections(
    centerline_path: Union[str, Path],
    dem_path: Union[str, Path],
    *,
    station_interval: float = 20.0,
    cross_section_width: float = 15.0,
    cross_section_step: float = 5.0,
    output_dir: Optional[Union[str, Path]] = None,
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, Path]:
    """執行 [A-03] 依道路中心線每隔指定間距 (如 20m) 內插樁號並擷取地面高程。

    參數：
        station_interval: 樁號間距 (公尺)；終點不在間距整數倍上時會另外補一個終點樁號。
        cross_section_width: 橫斷面單側寬度 (公尺)；0 表示只取中心線。
        cross_section_step: 橫斷面取樣間距 (公尺)；單側寬度不是間距整數倍時另外補上邊緣點。
        assigned_srs: 中心線或 DEM 沒有座標系標籤時要指派的座標系 (ASSIGNABLE_SOURCE_SRS 的鍵)。

    座標一律以 DEM 的座標系為準：中心線的座標系與 DEM 不同時先重投影到 DEM 的座標系，
    成果點位也標成 DEM 的座標系。中心線沒有標籤且未指派時視為與 DEM 同座標系 (不依範圍推測)。偏移量以前進方向為準，左側為負、右側為正。
    落在 DEM 範圍外或 NoData 的點高程留空 (CSV／Excel 為空白、GPKG 為 NULL)。

    輸出：
    1. 里程高程清冊 (.csv 與同檔名的 .xlsx)
    2. 斷面點位向量檔 (.gpkg)
    """
    line_p = Path(centerline_path).resolve()
    dem_p = Path(dem_path).resolve()
    out_dir = Path(output_dir).resolve() if output_dir else get_workspace_temp_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not line_p.exists() or not dem_p.exists():
        raise FileNotFoundError(f"中心線或高程檔不存在: line={line_p}, dem={dem_p}")
    if not station_interval > 0:
        raise ValueError(f"樁號間距必須大於 0: {station_interval}")

    with geo_io.TempVectorFiles("road_profile") as temp_files:
        # 1. 決定 DEM 與中心線的座標系，把中心線轉到 DEM 的座標系
        dem_decision = decide_and_report_source_srs(dem_p, assigned_srs, text_callback=text_callback)
        if dem_decision.has_embedded_srs:
            # 成果標回 DEM 原本的標籤；s_srs 有值 (有標籤的 TWD67) 時只拿它當重投影的定義
            label_srs = str(_write_raster_srs_wkt(dem_p, temp_files.new("dem_srs", ".wkt")))
        else:
            label_srs = str(dem_decision.s_srs)
        work_srs = dem_decision.s_srs or label_srs

        if geo_io.read_layer_info(line_p).has_srs or resolve_assigned_srs(assigned_srs):
            line_source_srs = decide_and_report_source_srs(line_p, assigned_srs, text_callback=text_callback).s_srs
        else:
            # 沒有標籤也沒指派 (DXF 常見)：視為與 DEM 同座標系，不依範圍推測，
            # 否則 DEM 是有標籤的 TWD67 時中心線會被當成 TWD97 而平移約 830 m
            line_source_srs = work_srs
            if text_callback:
                text_callback(
                    f"[座標系] {line_p.name}：中心線沒有座標系標籤，視為與 DEM 相同 ({dem_decision.source_label})。"
                    "若實際不同，請手動指派來源座標系。\n"
                )
        staged_gpkg = temp_files.new("line")
        staged_info = geo_io.stage_vector_to_gpkg(
            line_p,
            staged_gpkg,
            stage_layer="centerline",
            source_srs=line_source_srs,
            target_srs=work_srs,
        )
        if staged_info.is_geographic:
            raise ToolkitError(
                f"DEM 的座標系是經緯度 ({dem_p.name})，無法以公尺計算樁號與橫斷面偏移，請先把 DEM 重投影到 TWD97 等投影座標系。"
            )

        line_geojson = temp_files.new("line", ".geojson")
        geo_io.export_vector(staged_gpkg, "centerline", line_geojson, extra_args=["-lco", "COORDINATE_PRECISION=4"])
        parts = _read_line_parts(line_geojson)
        if not parts:
            raise ToolkitError(f"中心線檔案內沒有線段圖徵 (幾何型別: {staged_info.geometry_type}): {line_p.name}")
        vertices = _chain_line_parts(parts, line_p.name)
        if len(parts) > 1 and text_callback:
            text_callback(f"[斷面分析] 中心線由 {len(parts)} 段線串接而成，里程方向以第一段的方向為準。\n")

        # 2. 沿中心線內插樁號與橫斷面取樣點
        cum_dist = 0.0
        segments: List[Tuple[Tuple[float, float], Tuple[float, float], float, float]] = []
        for i in range(len(vertices) - 1):
            p1 = vertices[i]
            p2 = vertices[i + 1]
            seg_len = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if seg_len > 1e-4:
                segments.append((p1, p2, cum_dist, seg_len))
                cum_dist += seg_len
        if not segments:
            raise ToolkitError(f"中心線長度為 0，無法進行樁號線性內插: {line_p.name}")
        total_length = cum_dist

        # 樁號里程：間距整數倍，最後一定補上終點
        station_distances: List[float] = []
        station_no = 0
        while station_no * station_interval < total_length - 1e-3:
            station_distances.append(station_no * station_interval)
            station_no += 1
        station_distances.append(total_length)

        # 橫斷面偏移量：左側為負、右側為正，由左至右排列
        side_offsets: List[float] = []
        if cross_section_width > 0 and cross_section_step > 0:
            steps = int(math.floor(cross_section_width / cross_section_step + 1e-9))
            side_offsets = [s * cross_section_step for s in range(1, steps + 1)]
            if not side_offsets or cross_section_width - side_offsets[-1] > 1e-6:
                side_offsets.append(float(cross_section_width))
        offsets = [-d for d in reversed(side_offsets)] + [0.0] + side_offsets

        sampled_stations: List[CrossSectionPoint] = []
        seg_idx = 0
        for station_d in station_distances:
            while seg_idx < len(segments) - 1 and station_d > segments[seg_idx][2] + segments[seg_idx][3]:
                seg_idx += 1
            p1, p2, start_d, seg_len = segments[seg_idx]

            t = max(0.0, min(1.0, (station_d - start_d) / seg_len))
            cx = p1[0] + t * (p2[0] - p1[0])
            cy = p1[1] + t * (p2[1] - p1[1])

            # 前進方向 (dx, dy) 的右側單位法線是 (dy, -dx)；偏移量為正就往右、為負就往左
            dx = (p2[0] - p1[0]) / seg_len
            dy = (p2[1] - p1[1]) / seg_len
            label = _station_label(station_d)

            for offset in offsets:
                if offset == 0.0:
                    point_type = "CENTERLINE"
                else:
                    point_type = "CROSS_SECTION_LEFT" if offset < 0 else "CROSS_SECTION_RIGHT"
                sampled_stations.append(CrossSectionPoint(
                    station_label=label,
                    station_distance=round(station_d, 2),
                    offset_m=offset,
                    x=cx + offset * dy,
                    y=cy - offset * dx,
                    elevation_z=None,
                    point_type=point_type,
                ))

        if text_callback:
            text_callback(
                f"[斷面分析] 總里程 {total_length:.2f}m，共 {len(station_distances)} 個樁號、"
                f"{len(sampled_stations)} 個取樣點，正在讀取 DEM 高程。\n"
            )
        if progress_callback:
            progress_callback(30.0, "讀取 DEM 高程")

        # 3. 批次查詢高程 (經 run_command，使用隔離環境)
        dem_info = run_json_command(["gdalinfo", "-json", str(dem_p)])
        dem_bands = dem_info.get("bands") or [{}]
        nodata_raw = dem_bands[0].get("noDataValue")
        nodata_value = float(nodata_raw) if isinstance(nodata_raw, (int, float)) else None

        elevations = _sample_dem_elevations(dem_p, [(pt.x, pt.y) for pt in sampled_stations], nodata_value)
        for pt, elevation in zip(sampled_stations, elevations):
            pt.elevation_z = round(elevation, 2) if elevation is not None else None

        missing_count = sum(1 for pt in sampled_stations if pt.elevation_z is None)
        if missing_count == len(sampled_stations):
            raise ToolkitError(
                f"所有取樣點都落在 DEM 範圍外或 NoData：{line_p.name} 與 {dem_p.name} 沒有重疊，請確認兩者的座標系。"
            )
        if missing_count and text_callback:
            text_callback(
                f"[注意] 有 {missing_count} 個取樣點落在 DEM 範圍外或 NoData，高程留空 (共 {len(sampled_stations)} 點)。\n"
            )
        if progress_callback:
            progress_callback(70.0, "輸出里程高程清冊")

        # 4. 輸出里程高程清冊 CSV 與 Excel
        headers = ["樁號標籤", "里程累積距離_m", "斷面偏移量_m", "X坐標", "Y坐標", "地面高程_Z", "點位類別"]
        csv_path = out_dir / f"{line_p.stem}_stations_elevation.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f_csv:
            writer = csv.writer(f_csv)
            writer.writerow(headers)
            for pt in sampled_stations:
                writer.writerow([
                    pt.station_label,
                    pt.station_distance,
                    pt.offset_m,
                    f"{pt.x:.2f}",
                    f"{pt.y:.2f}",
                    "" if pt.elevation_z is None else pt.elevation_z,
                    pt.point_type,
                ])

        excel_path = csv_path.with_suffix(".xlsx")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "里程高程清冊"
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        for pt in sampled_stations:
            sheet.append([
                pt.station_label,
                pt.station_distance,
                pt.offset_m,
                round(pt.x, 3),
                round(pt.y, 3),
                pt.elevation_z,
                pt.point_type,
            ])
        for col_idx, width in enumerate((14, 16, 14, 14, 14, 12, 22), start=1):
            sheet.column_dimensions[get_column_letter(col_idx)].width = width
        sheet.freeze_panes = "A2"
        workbook.save(excel_path)

        # 5. 輸出向量點位 GPKG (標上 DEM 的座標系)
        pts_geojson = temp_files.new("stations", ".geojson")
        features = []
        for pt in sampled_stations:
            features.append({
                "type": "Feature",
                "properties": {
                    "STATION": pt.station_label,
                    "DIST_M": pt.station_distance,
                    "OFFSET_M": pt.offset_m,
                    "ELEV_Z": pt.elevation_z,
                    "TYPE": pt.point_type,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(pt.x, 3), round(pt.y, 3)],
                },
            })
        with open(pts_geojson, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False)

        out_gpkg = out_dir / f"{line_p.stem}_stations_points.gpkg"
        geo_io.export_vector(pts_geojson, None, out_gpkg, extra_args=["-a_srs", label_srs])

    if text_callback:
        text_callback(f"[斷面分析] 里程高程清冊匯出完成: {csv_path.name}、{excel_path.name}；點位: {out_gpkg.name}\n")
    if progress_callback:
        progress_callback(100.0, "斷面擷取完成")

    return (csv_path, out_gpkg)


# ============================================================================
# [A-04] 水文集水區劃設 (WhiteboxTools CLI)
# ============================================================================

def delineate_watershed_whitebox(
    dem_path: Union[str, Path],
    pour_point_coord: Tuple[float, float],
    *,
    output_dir: Optional[Union[str, Path]] = None,
    snap_distance_m: float = 50.0,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
    assigned_srs: Optional[str] = None,
    pour_point_srs: Optional[str] = None,
) -> WatershedResult:
    """執行 [A-04] WhiteboxTools CLI 水文流向分析與上游集水區邊界自動劃設。

    運作流程（規格書 3.4 節）：
    1. --run="BreachDepressionsLeastCost" (打通阻水點/填窪)
    2. --run="D8Pointer" (計算水流流向網格)
    3. --run="D8FlowAccumulation" (計算匯流累積量)
    4. --run="SnapPourPoints" (自動捕捉鄰近河道流向集中點)
    5. --run="Watershed" (計算圈繪出水口以上所有集水面積)
    6. 呼叫 gdal_polygonize 轉為向量多邊形並計算面積 (公頃與 m²)。

    參數：
        assigned_srs: DEM 沒有座標系標籤時要指派的座標系 (ASSIGNABLE_SOURCE_SRS 的鍵)；未指定時依範圍推測。
        pour_point_srs: 出水口座標所用的座標系；未指定時視為與 DEM 相同。
        snap_distance_m: 出水口捕捉距離 (公尺)。

    座標系處理：
        - WhiteboxTools 讀不了沒有座標系標籤的 DEM，先另存一份標上座標系的暫存 DEM 再分析。
        - 經緯度 DEM 先重投影到 TWD97 (EPSG:3826) 再分析，捕捉距離與面積才是公尺；成果也是 EPSG:3826。
        - 其餘情況成果沿用 DEM 的座標系。
    """
    start_time = time.time()
    in_dem = Path(dem_path).resolve()
    if not in_dem.exists():
        raise FileNotFoundError(f"DEM 檔案不存在: {in_dem}")
    if not snap_distance_m >= 0:
        raise ValueError(f"出水口捕捉距離不可為負值: {snap_distance_m}")
    temp_dir = get_workspace_temp_dir()
    out_dir = Path(output_dir).resolve() if output_dir else temp_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    wbt_bin = resolve_binary("whitebox_tools")
    pour_x, pour_y = float(pour_point_coord[0]), float(pour_point_coord[1])

    if text_callback:
        text_callback(f"[水文分析] 正在調用 WhiteboxTools CLI 進行水文地形分析 (出水口: {pour_point_coord})。\n")

    with geo_io.TempVectorFiles("watershed") as temp_files:
        # 0. 準備分析用 DEM：一定要有座標系標籤，且單位為公尺
        dem_decision = decide_and_report_source_srs(in_dem, assigned_srs, text_callback=text_callback)
        work_dem = in_dem
        if not dem_decision.has_embedded_srs:
            work_dem = temp_files.new("labelled", ".tif")
            run_command(
                ["gdal_translate", "-of", "GTiff", "-a_srs", str(dem_decision.s_srs), str(in_dem), str(work_dem)],
                text_callback=text_callback,
                check=True,
            )
        # 出水口未另外指定座標系時，與 DEM 原本的座標系相同
        dem_source_srs = dem_decision.s_srs or str(_write_raster_srs_wkt(work_dem, temp_files.new("dem_srs", ".wkt")))

        reprojected = _read_raster_srs_metadata(work_dem).is_geographic
        if reprojected:
            projected_dem = temp_files.new("projected", ".tif")
            run_command(
                [
                    "gdalwarp", "-of", "GTiff", "-overwrite",
                    "-t_srs", f"EPSG:{EPSG_TWD97_121}",
                    "-r", "bilinear",
                    str(work_dem), str(projected_dem),
                ],
                text_callback=text_callback,
                check=True,
            )
            work_dem = projected_dem
            if text_callback:
                text_callback(
                    f"[注意] {in_dem.name} 是經緯度座標，已先重投影到 TWD97 (EPSG:{EPSG_TWD97_121}) 再分析，成果也是 EPSG:{EPSG_TWD97_121}。\n"
                )
        work_srs = str(_write_raster_srs_wkt(work_dem, temp_files.new("work_srs", ".wkt")))
        work_extent = _read_raster_srs_metadata(work_dem).extent

        # 1. 建立出水口向量點位 Shapefile (轉到分析用 DEM 的座標系)
        pour_geojson = temp_files.new("pour", ".geojson")
        pour_shp = temp_files.new("pour", ".shp")
        pour_data = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"ID": 1},
                    "geometry": {"type": "Point", "coordinates": [pour_x, pour_y]},
                }
            ],
        }
        with open(pour_geojson, "w", encoding="utf-8") as f:
            json.dump(pour_data, f)

        pour_source_srs = resolve_assigned_srs(pour_point_srs)
        if pour_source_srs is None and not reprojected:
            pour_srs_args = ["-a_srs", work_srs]
        else:
            # 有標籤的 TWD67 DEM 要以七參數定義當目標，才不會差一個基準面
            pour_target_srs = dem_decision.s_srs if (dem_decision.s_srs and not reprojected) else work_srs
            pour_srs_args = ["-s_srs", pour_source_srs or dem_source_srs, "-t_srs", pour_target_srs]
        run_command(
            ["ogr2ogr", "-f", "ESRI Shapefile", "-overwrite", "-lco", "ENCODING=UTF-8", *pour_srs_args, str(pour_shp), str(pour_geojson)],
            check=True,
        )
        pour_extent = geo_io.read_layer_info(pour_shp).extent
        if pour_extent is None:
            raise ToolkitError(f"出水口座標無法轉換到 DEM 的座標系: {pour_point_coord}")
        work_pour_x, work_pour_y = float(pour_extent[0]), float(pour_extent[1])
        if not (work_extent.min_x <= work_pour_x <= work_extent.max_x and work_extent.min_y <= work_pour_y <= work_extent.max_y):
            raise ToolkitError(
                f"出水口 ({work_pour_x:.3f}, {work_pour_y:.3f}) 不在 DEM 範圍內 "
                f"(X {work_extent.min_x:.3f}～{work_extent.max_x:.3f}，Y {work_extent.min_y:.3f}～{work_extent.max_y:.3f})。"
                "請確認出水口座標與 DEM 是同一個座標系，或以 pour_point_srs 指定出水口的座標系。"
            )

        # 2. 填窪處理 (BreachDepressionsLeastCost)
        breached_dem = temp_files.new("breached", ".tif")
        cmd_breach = [
            wbt_bin,
            "--run=BreachDepressionsLeastCost",
            f"--dem={work_dem}",
            f"--output={breached_dem}",
            "--dist=50",
        ]
        run_command(cmd_breach, text_callback=text_callback, check=False)
        if not breached_dem.exists():
            # 回退使用 FillDepressions
            cmd_fill = [wbt_bin, "--run=FillDepressions", f"--dem={work_dem}", f"--output={breached_dem}"]
            run_command(cmd_fill, text_callback=text_callback, check=True)

        if progress_callback:
            progress_callback(30.0, "填窪完成，正在計算 D8 流向與匯流累積。")

        # 3. 計算 D8 水流方向
        d8_pntr = temp_dir / f"{in_dem.stem}_d8_pntr.tif"
        cmd_pntr = [wbt_bin, "--run=D8Pointer", f"--dem={breached_dem}", f"--output={d8_pntr}"]
        run_command(cmd_pntr, text_callback=text_callback, check=True)

        # 4. 計算 D8 匯流累積量
        flow_accum = temp_dir / f"{in_dem.stem}_flow_accum.tif"
        cmd_accum = [wbt_bin, "--run=D8FlowAccumulation", f"--i={d8_pntr}", f"--output={flow_accum}", "--pntr=True"]
        run_command(cmd_accum, text_callback=text_callback, check=True)

        # 5. 捕捉出水口至主河道 (SnapPourPoints)
        snapped_shp = temp_files.new("snapped", ".shp")
        cmd_snap = [
            wbt_bin,
            "--run=SnapPourPoints",
            f"--pour_pts={pour_shp}",
            f"--flow_accum={flow_accum}",
            f"--output={snapped_shp}",
            f"--snap_dist={snap_distance_m}",
        ]
        run_command(cmd_snap, text_callback=text_callback, check=False)
        final_pour = snapped_shp if snapped_shp.exists() else pour_shp

        if progress_callback:
            progress_callback(70.0, "出水口捕捉完成，正在圈繪集水區邊界。")

        # 6. 計算集水區邊界網格 (Watershed)
        watershed_tif = out_dir / f"{in_dem.stem}_watershed.tif"
        cmd_ws = [
            wbt_bin,
            "--run=Watershed",
            f"--d8_pntr={d8_pntr}",
            f"--pour_pts={final_pour}",
            f"--output={watershed_tif}",
        ]
        run_command(cmd_ws, text_callback=text_callback, check=True)

        # 7. 將集水區網格向量化為多邊形 (gdal_polygonize)；D8 流向含對角方向，以 8 鄰域相連才不會把集水區拆成碎塊
        watershed_gpkg = out_dir / f"{in_dem.stem}_watershed_boundary.gpkg"
        geo_io.remove_vector_file(watershed_gpkg)

        cmd_poly = [
            resolve_binary("python"), "-m", "osgeo_utils.gdal_polygonize", "-q", "-8",
            str(watershed_tif),
            "-b", "1",
            "-of", "GPKG",
            str(watershed_gpkg),
            "watershed",
            "WS_ID",
        ]
        run_command(cmd_poly, check=True)

        # 8. 統計集水區面積 (公頃與平方公尺)
        area_m2 = 0.0
        area_ha = 0.0
        watershed_info = geo_io.read_layer_info(watershed_gpkg, "watershed")
        area_gpkg = temp_files.new("area")
        sql = (
            f"SELECT ROUND(SUM(ST_Area({geo_io.quote_ident(watershed_info.sql_geometry_name)})), 2) AS \"TOT_AREA\" "
            f"FROM \"watershed\" WHERE \"WS_ID\" > 0"
        )
        geo_io.run_sql_to_gpkg(watershed_gpkg, sql, area_gpkg, "area")
        area_rows = geo_io.read_attribute_rows(area_gpkg, "area", fields=["TOT_AREA"])
        if area_rows and area_rows[0]["TOT_AREA"] is not None:
            area_m2 = float(area_rows[0]["TOT_AREA"])
            area_ha = round(area_m2 / 10000.0, 4)

    if area_m2 <= 0.0:
        raise ToolkitError(
            f"出水口 ({work_pour_x:.3f}, {work_pour_y:.3f}) 沒有圈出任何集水區，可能落在 DEM 的 NoData 範圍；請把出水口移到河道上再試。"
        )

    duration = time.time() - start_time
    if progress_callback:
        progress_callback(100.0, "集水區劃設完成")

    return WatershedResult(
        dem_path=in_dem,
        d8_pntr_raster=d8_pntr,
        flow_accum_raster=flow_accum,
        watershed_raster=watershed_tif,
        watershed_polygon=watershed_gpkg,
        pour_point_coord=pour_point_coord,
        catchment_area_ha=area_ha,
        catchment_area_m2=area_m2,
        duration_seconds=duration,
    )
