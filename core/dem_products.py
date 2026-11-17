"""DEM 裁切成果的附加成果 (core/dem_products.py)

1. 等高線 DXF generate_contour_dxf：gdal_contour 以首曲線間距抽繪 (每條線帶高程 Z)，
   高程是計曲線間距整數倍的放「計曲線」圖層、其餘放「首曲線」圖層；每個高程只有一條線，計曲線不會在首曲線重複。
   DXF 改以 Big5 (ANSI_950) 寫出，臺灣的 AutoCAD 開啟時中文圖層名不會亂碼。
2. 坡度圖 generate_slope_percent：gdaldem slope -p 的百分比坡度 GeoTIFF。

都由裁切後的 DEM 產生，座標系與 DEM 相同；運算一律經 CLI。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from core import geo_io
from core.env_runner import ToolkitError, run_command

logger = logging.getLogger("core.dem_products")

TextCallback = Optional[Callable[[str], None]]

CONTOUR_LAYER_MINOR: str = "首曲線"
CONTOUR_LAYER_MAJOR: str = "計曲線"
DEFAULT_CONTOUR_INTERVAL: float = 1.0
DEFAULT_INDEX_INTERVAL: float = 5.0
# 高程是不是計曲線間距的整數倍，容許的浮點誤差 (以「倍數」計)
_INDEX_TOLERANCE: float = 1e-6
_ELEVATION_FIELD: str = "ELEV"
# GDAL 寫 DXF 的預設檔頭字碼頁，換成臺灣 AutoCAD 的 Big5
_DXF_DEFAULT_CODEPAGE: str = "ANSI_1252"
_DXF_BIG5_CODEPAGE: str = "ANSI_950"


@dataclass
class ContourDxfResult:
    """等高線 DXF 成果與兩個圖層各有幾條線。"""

    output_path: Path
    minor_count: int
    major_count: int


def contour_output_path(dem_output: Path) -> Path:
    return dem_output.with_name(f"{dem_output.stem}_contours.dxf")


def slope_output_path(dem_output: Path) -> Path:
    return dem_output.with_name(f"{dem_output.stem}_slope_pct.tif")


def validate_contour_intervals(interval: float, index_interval: float) -> int:
    """檢查首曲線與計曲線間距，回傳計曲線是首曲線的幾倍；不合理時拋 ValueError (訊息可直接給使用者看)。"""
    if not (math.isfinite(interval) and interval > 0 and math.isfinite(index_interval) and index_interval > 0):
        raise ValueError("首曲線與計曲線間距都必須是大於 0 的數字 (公尺)。")
    ratio = index_interval / interval
    multiple = round(ratio)
    if multiple < 2 or abs(ratio - multiple) > _INDEX_TOLERANCE:
        raise ValueError(
            f"計曲線間距要是首曲線間距的整數倍 (2 倍以上)，例如首曲線 1 m、計曲線 5 m；"
            f"目前是首曲線 {interval:g} m、計曲線 {index_interval:g} m。"
        )
    return multiple


def _is_major_elevation(elevation: float, index_interval: float) -> bool:
    ratio = elevation / index_interval
    return abs(ratio - round(ratio)) < _INDEX_TOLERANCE


def _write_big5_dxf(source_dxf: Path, output_dxf: Path) -> None:
    """GDAL 寫出的 DXF 檔頭是 ANSI_1252、中文卻是 UTF-8 位元組；改標 ANSI_950 並整份轉成 Big5。"""
    text = source_dxf.read_bytes().decode("utf-8")
    if _DXF_DEFAULT_CODEPAGE not in text:
        raise ToolkitError(f"DXF 檔頭找不到字碼頁 {_DXF_DEFAULT_CODEPAGE}，無法轉成 Big5。")
    text = text.replace(_DXF_DEFAULT_CODEPAGE, _DXF_BIG5_CODEPAGE, 1)
    output_dxf.write_bytes(text.encode("cp950"))


def generate_contour_dxf(
    dem_path: Union[str, Path],
    output_dxf: Union[str, Path],
    *,
    interval: float = DEFAULT_CONTOUR_INTERVAL,
    index_interval: float = DEFAULT_INDEX_INTERVAL,
    text_callback: TextCallback = None,
) -> ContourDxfResult:
    """由 DEM 抽繪等高線，寫成一個 DXF：「首曲線」「計曲線」兩個圖層，每條線帶高程 (LWPOLYLINE 的 Elevation)。"""
    validate_contour_intervals(interval, index_interval)
    dem = Path(dem_path).resolve()
    out = Path(output_dxf).resolve()
    if not dem.exists():
        raise FileNotFoundError(f"找不到 DEM: {dem}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if text_callback:
        text_callback(f"[等高線] {dem.name}：首曲線 {interval:g} m、計曲線 {index_interval:g} m。\n")

    with geo_io.TempVectorFiles("contour_dxf") as temp_files:
        lines = temp_files.new("lines")
        run_command(
            [
                "gdal_contour",
                "-a", _ELEVATION_FIELD,
                "-i", repr(float(interval)),
                "-3d",
                "-f", "GPKG",
                "-nln", "contour",
                str(dem), str(lines),
            ],
            text_callback=text_callback,
            check=True,
        )
        info = geo_io.read_layer_info(lines, "contour")
        if info.feature_count == 0:
            raise ToolkitError(f"{dem.name} 範圍內的高程變化不到一個首曲線間距 ({interval:g} m)，沒有等高線可以輸出。")
        elevations = [
            float(row[_ELEVATION_FIELD])
            for row in geo_io.read_attribute_rows(lines, "contour", fields=[_ELEVATION_FIELD])
        ]
        major_count = sum(1 for value in elevations if _is_major_elevation(value, index_interval))

        index_text = repr(float(index_interval))
        major_sql = (
            f"ABS({_ELEVATION_FIELD} / {index_text} - ROUND({_ELEVATION_FIELD} / {index_text})) < {_INDEX_TOLERANCE!r}"
        )
        sql = (
            f"SELECT {geo_io.quote_ident(info.sql_geometry_name)}, "
            f"CASE WHEN {major_sql} THEN {geo_io.quote_literal(CONTOUR_LAYER_MAJOR)} "
            f"ELSE {geo_io.quote_literal(CONTOUR_LAYER_MINOR)} END AS {geo_io.quote_ident('Layer')} "
            f"FROM {geo_io.quote_ident('contour')}"
        )
        utf8_dxf = temp_files.new("utf8", suffix=".dxf")
        # 這一步 GDAL 會警告「Z 值會被捨棄」，實際上高程有寫進 LWPOLYLINE 的 Elevation，不把這段訊息放進日誌免得誤會
        run_command(["ogr2ogr", "-f", "DXF", str(utf8_dxf), str(lines), "-sql", sql], check=True)
        _write_big5_dxf(utf8_dxf, out)

    minor_count = len(elevations) - major_count
    if text_callback:
        text_callback(
            f"[等高線] 已輸出 {out.name}：{CONTOUR_LAYER_MINOR} {minor_count} 條、{CONTOUR_LAYER_MAJOR} {major_count} 條 (每條線帶高程)。\n"
        )
    return ContourDxfResult(output_path=out, minor_count=minor_count, major_count=major_count)


def generate_slope_percent(
    dem_path: Union[str, Path],
    output_tif: Union[str, Path],
    *,
    text_callback: TextCallback = None,
) -> Path:
    """由 DEM 計算百分比坡度 (gdaldem slope -p，邊緣也計算)，寫成 LZW 壓縮的 GeoTIFF。"""
    dem = Path(dem_path).resolve()
    out = Path(output_tif).resolve()
    if not dem.exists():
        raise FileNotFoundError(f"找不到 DEM: {dem}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if text_callback:
        text_callback(f"[坡度] {dem.name}：計算百分比坡度。\n")
    run_command(
        ["gdaldem", "slope", str(dem), str(out), "-p", "-compute_edges", "-co", "COMPRESS=LZW"],
        text_callback=text_callback,
        check=True,
    )
    if text_callback:
        text_callback(f"[坡度] 已輸出 {out.name} (單位 %)。\n")
    return out
