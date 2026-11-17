"""臺灣空間座標系診斷、TWD67 七參數轉換與 CAD GCP 對齊模組 (core/crs_doctor.py)

嚴格遵守專案規格書第 0 節與第 2 節鐵律：
1. 嚴禁使用 geopandas, rasterio, shapely, pyproj。
2. 內建臺灣標準座標定義與 TWD67 七參數 PROJ 字串，徹底修正 850 公尺基準面偏移地雷。
3. [G-01] 實作向量與網格全自動範圍座標嗅探與診斷警示燈號。
4. [G-02] TWD67 ↔ TWD97 轉換指令封裝。
5. [G-03] DXF 公釐 (mm) 縮放 (0.001) 與 GCP 控制點一階仿射變換計算。
6. [G-04] GDAL -tap (Target Aligned Pixels) 致命語法防呆：強制綁定 -tr <xres> <yres>。
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# 允許使用 numpy（規範第 0 節明訂允許用於數值矩陣計算）
import numpy as np

from core import geo_io
from core.env_runner import (
    CommandResult,
    ToolkitError,
    get_workspace_temp_dir,
    run_command,
    run_json_command,
)

logger = logging.getLogger("core.crs_doctor")

# ============================================================================
# 2.1 臺灣標準座標系代碼與 PROJ 定義字串
# ============================================================================

EPSG_TWD97_121: int = 3826  # TWD97 TM2 臺灣本島 (中央經線 121°E) - 預設目標標準座標系
EPSG_TWD97_119: int = 3825  # TWD97 TM2 澎湖金門馬祖 (中央經線 119°E)
EPSG_WGS84: int = 4326      # WGS84 經緯度 (GPS 標準)
EPSG_TWD67_121: int = 3828  # TWD67 TM2 臺灣本島 (內政部舊地籍/地形圖標準)
EPSG_TWD67_119: int = 3827  # TWD67 TM2 澎湖金馬

# 關鍵地雷解法：TWD67 臺灣本島 (121°E) 完整七參數 PROJ 定義
# 解決直接宣告 EPSG:3828 導致 850 公尺未平移問題
TWD67_121_PROJ: str = (
    "+proj=tmerc +lat_0=0 +lon_0=121 +k=0.9999 +x_0=250000 +y_0=0 "
    "+ellps=aust_SA "
    "+towgs84=-752,-358,-179,-0.0000011698,0.0000018398,0.0000009822,0.00002329 "
    "+units=m +no_defs"
)

# TWD67 澎湖金馬 (119°E) 七參數 PROJ 定義
TWD67_119_PROJ: str = (
    "+proj=tmerc +lat_0=0 +lon_0=119 +k=0.9999 +x_0=250000 +y_0=0 "
    "+ellps=aust_SA "
    "+towgs84=-752,-358,-179,-0.0000011698,0.0000018398,0.0000009822,0.00002329 "
    "+units=m +no_defs"
)

# TWD97 標準 PROJ 字串 (GRS80 橢球)
TWD97_121_PROJ: str = (
    "+proj=tmerc +lat_0=0 +lon_0=121 +k=0.9999 +x_0=250000 +y_0=0 "
    "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
)

TWD97_119_PROJ: str = (
    "+proj=tmerc +lat_0=0 +lon_0=119 +k=0.9999 +x_0=250000 +y_0=0 "
    "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
)


# ============================================================================
# 資料結構定義
# ============================================================================

@dataclass
class Extent2D:
    """二維空間邊界框結構 (min_x, min_y, max_x, max_y)。"""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        """邊界框寬度。"""
        return abs(self.max_x - self.min_x)

    @property
    def height(self) -> float:
        """邊界框高度。"""
        return abs(self.max_y - self.min_y)

    @property
    def center_x(self) -> float:
        """邊界框中心點 X。"""
        return (self.min_x + self.max_x) / 2.0

    @property
    def center_y(self) -> float:
        """邊界框中心點 Y。"""
        return (self.min_y + self.max_y) / 2.0

    def as_tuple(self) -> Tuple[float, float, float, float]:
        """轉為標準四元組 (min_x, min_y, max_x, max_y)。"""
        return (self.min_x, self.min_y, self.max_x, self.max_y)


@dataclass
class CrsDiagnosisResult:
    """檔案空間座標診斷分析報告。"""

    file_path: Optional[str]
    extent: Optional[Extent2D]
    raw_srs_name: Optional[str]
    detected_epsg: Optional[int]
    status_level: str  # "GREEN" (正常), "YELLOW" (需指派或注意), "RED" (警告/單位錯誤)
    status_code: str
    status_title: str
    status_message: str
    suggested_action: str
    recommended_assign_srs: Optional[str] = None
    recommended_transform_srs: Optional[str] = None
    needs_unit_scaling: bool = False
    scale_factor: float = 1.0


@dataclass
class ControlPoint:
    """GCP 地面控制點對，用於 CAD 仿射校正。"""

    src_x: float
    src_y: float
    dst_x: float
    dst_y: float
    point_id: Optional[str] = None


@dataclass
class AffineTransformResult:
    """2D 仿射變換運算結果 (X' = A*X + B*Y + C, Y' = D*X + E*Y + F)。"""

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float
    rmse: float
    residuals: List[float] = field(default_factory=list)

    def transform(self, x: float, y: float) -> Tuple[float, float]:
        """對單一點進行一階仿射變換。"""
        new_x = self.a * x + self.b * y + self.c
        new_y = self.d * x + self.e * y + self.f
        return (new_x, new_y)


@dataclass
class GcpGeoreferenceResult:
    """控制點仿射校正的成果。"""

    output_path: Path
    transform: AffineTransformResult
    control_point_count: int
    target_srs: str


# ============================================================================
# [G-01] 座標嗅探邏輯 (Heuristic Sniffer)
# ============================================================================

# 臺灣周邊的經緯度範圍 (含金門、馬祖、東沙)。有標籤的經緯度資料不在這個範圍只會提醒；
# 沒有標籤的資料要整個落在範圍內才會被推測成 EPSG:4326 (見 guess_srs_from_extent)
_TAIWAN_LON_RANGE: Tuple[float, float] = (116.0, 124.0)
_TAIWAN_LAT_RANGE: Tuple[float, float] = (20.0, 27.0)

# 標籤為臺灣 TM2 的 EPSG 代碼 → (名稱, 是否為 TWD67)
_TM2_LABELS: Dict[int, Tuple[str, bool]] = {
    EPSG_TWD97_121: ("TWD97 TM2 121 分帶", False),
    EPSG_TWD97_119: ("TWD97 TM2 119 分帶", False),
    EPSG_TWD67_121: ("TWD67 TM2 121 分帶", True),
    EPSG_TWD67_119: ("TWD67 TM2 119 分帶", True),
}

# 沒有傳 is_geographic 時，用來判斷標籤是否為經緯度的 EPSG 代碼 (WGS84、TWD97、TWD67)
_GEOGRAPHIC_EPSG_CODES = frozenset({EPSG_WGS84, 3824, 3821})


def _center_in_tm2_range(extent: Extent2D) -> bool:
    """範圍中心是否落在臺灣 TM2 的數值範圍 (121 與 119 分帶數值重疊，共用同一組範圍)。"""
    return (
        _TM2_X_RANGE[0] <= extent.center_x <= _TM2_X_RANGE[1]
        and _TM2_Y_RANGE[0] <= extent.center_y <= _TM2_Y_RANGE[1]
    )


def _center_near_taiwan_lonlat(extent: Extent2D) -> bool:
    """範圍中心是否落在臺灣周邊的經緯度範圍。"""
    return (
        _TAIWAN_LON_RANGE[0] <= extent.center_x <= _TAIWAN_LON_RANGE[1]
        and _TAIWAN_LAT_RANGE[0] <= extent.center_y <= _TAIWAN_LAT_RANGE[1]
    )


def _within_lonlat_bounds(extent: Extent2D) -> bool:
    """四至是否都在 ±180／±90 內 (數值上可以是經緯度，也可能是自訂原點的小數值區域座標)。"""
    return all(abs(x) <= 180.0 for x in (extent.min_x, extent.max_x)) and all(
        abs(y) <= 90.0 for y in (extent.min_y, extent.max_y)
    )


def _within_taiwan_lonlat(extent: Extent2D) -> bool:
    """四至是否整個落在臺灣周邊的經緯度範圍。"""
    return all(_TAIWAN_LON_RANGE[0] <= x <= _TAIWAN_LON_RANGE[1] for x in (extent.min_x, extent.max_x)) and all(
        _TAIWAN_LAT_RANGE[0] <= y <= _TAIWAN_LAT_RANGE[1] for y in (extent.min_y, extent.max_y)
    )


def _looks_like_tm2_in_millimetres(extent: Extent2D) -> bool:
    """座標縮小 0.001 倍後是否落在臺灣 TM2 範圍 (CAD 以公釐繪製)。"""
    scaled = Extent2D(*(value * 0.001 for value in extent.as_tuple()))
    return _center_in_tm2_range(scaled)


def diagnose_extent(
    extent: Extent2D,
    file_path: Optional[str] = None,
    raw_srs_name: Optional[str] = None,
    detected_epsg: Optional[int] = None,
    *,
    has_embedded_srs: Optional[bool] = None,
    is_geographic: Optional[bool] = None,
) -> CrsDiagnosisResult:
    """依座標系標籤與四至範圍產生診斷燈號；診斷只供顯示，實際處理以 decide_source_srs 的決策為準。

    has_embedded_srs 未傳時，以「有 detected_epsg 或 raw_srs_name」視為有標籤。

    有標籤 (處理時一律依標籤，不會被數值推翻)：
    - 臺灣 TM2 (EPSG:3826／3825／3828／3827)：數值在 TM2 範圍內時，TWD97 為綠燈、TWD67 為黃燈
      (會自動套用七參數)；數值不在範圍內為紅燈 (標籤或單位有誤)。
    - 經緯度：在臺灣周邊為綠燈，不在臺灣周邊為黃燈，超出 ±180／±90 為紅燈。
    - 其他可辨識的座標系 (例如 UTM)：綠燈，無法由數值檢查範圍。
    - 無法對應 EPSG 代碼的自訂座標系：黃燈。

    沒有標籤 (與 guess_srs_from_extent 的推測規則一致)：
    - 落在臺灣 TM2 數值範圍：黃燈，推測為 EPSG:3826。121／119 分帶與 TWD67／TWD97 的數值範圍重疊，
      無法只靠數值區分，離島與舊圖資要由使用者指派。
    - 整個落在臺灣周邊的經緯度範圍：黃燈，推測為 EPSG:4326。
    - |X|≤180 且 |Y|≤90 但不在臺灣周邊：紅燈，多半是自訂原點的區域座標，不推測，必須由使用者指派。
    - 縮小 0.001 倍後落在 TM2 範圍：紅燈，CAD 以公釐繪製。
    - 其他：紅燈，必須由使用者指派來源座標系。
    """
    labelled = bool(detected_epsg or raw_srs_name) if has_embedded_srs is None else bool(has_embedded_srs)
    label_text = raw_srs_name or (f"EPSG:{detected_epsg}" if detected_epsg else "來源內嵌座標系")

    def build(level: str, code: str, title: str, message: str, action: str, **extra: Any) -> CrsDiagnosisResult:
        return CrsDiagnosisResult(
            file_path=file_path,
            extent=extent,
            raw_srs_name=label_text if labelled else "無標籤",
            detected_epsg=detected_epsg,
            status_level=level,
            status_code=code,
            status_title=title,
            status_message=message,
            suggested_action=action,
            **extra,
        )

    if not all(math.isfinite(value) for value in extent.as_tuple()):
        return build(
            "RED",
            "EXTENT_INVALID",
            "座標範圍無效",
            "檔案的四至範圍含有無限大或非數值，請檢查圖資內是否有異常座標。",
            "MANUAL_INSPECT",
        )

    center_text = f"({extent.center_x:.2f}, {extent.center_y:.2f})"

    def millimetre_result() -> CrsDiagnosisResult:
        return build(
            "RED",
            "CAD_MM_SCALE",
            "CAD 單位為公釐 (mm) 警示",
            "座標縮小 0.001 倍後才落在臺灣二度分帶範圍，判定為 AutoCAD 以公釐 (mm) 繪製導致數值放大 1000 倍！"
            "建議先縮小 0.001 倍回到公尺。",
            "SCALE_0001",
            recommended_assign_srs=f"EPSG:{EPSG_TWD97_121}",
            needs_unit_scaling=True,
            scale_factor=0.001,
        )

    if labelled:
        geographic = (detected_epsg in _GEOGRAPHIC_EPSG_CODES) if is_geographic is None else bool(is_geographic)
        if geographic:
            if any(abs(x) > 180.0 for x in (extent.min_x, extent.max_x)) or any(
                abs(y) > 90.0 for y in (extent.min_y, extent.max_y)
            ):
                return build(
                    "RED",
                    "LABEL_EXTENT_MISMATCH",
                    "座標系標籤與座標數值不符",
                    f"檔案標註為經緯度座標系 ({label_text})，但座標範圍超出 ±180／±90 度，標籤可能有誤。"
                    "處理時仍會依標籤，請先確認原圖的座標系。",
                    "MANUAL_INSPECT",
                )
            if _center_near_taiwan_lonlat(extent):
                if detected_epsg == EPSG_WGS84:
                    return build(
                        "GREEN",
                        "WGS84_STANDARD",
                        "標準 WGS84 經緯度",
                        "檔案座標數值落於臺灣周邊的經緯度範圍，且已正確標註 EPSG:4326。",
                        "CONVERT_TO_3826",
                        recommended_transform_srs=f"EPSG:{EPSG_TWD97_121}",
                    )
                return build(
                    "GREEN",
                    "GEOGRAPHIC_STANDARD",
                    "已標註的經緯度座標系",
                    f"檔案標註為經緯度座標系 ({label_text})，座標落於臺灣周邊，處理時會依標籤重投影。",
                    "CONVERT_TO_3826",
                    recommended_transform_srs=f"EPSG:{EPSG_TWD97_121}",
                )
            return build(
                "YELLOW",
                "LABELED_OUTSIDE_TAIWAN",
                "經緯度座標不在臺灣周邊",
                f"檔案標註為經緯度座標系 ({label_text})，中心座標 {center_text} 不在臺灣周邊。"
                "標籤合法，處理時會依標籤重投影；請確認是否選對檔案。",
                "MANUAL_INSPECT",
            )

        if detected_epsg in _TM2_LABELS:
            label_name, is_twd67 = _TM2_LABELS[detected_epsg]
            if not _center_in_tm2_range(extent):
                if _looks_like_tm2_in_millimetres(extent):
                    return millimetre_result()
                return build(
                    "RED",
                    "LABEL_EXTENT_MISMATCH",
                    "座標系標籤與座標數值不符",
                    f"檔案標註為 {label_name} (EPSG:{detected_epsg})，但中心座標 {center_text} 不在臺灣二度分帶的數值範圍內，"
                    "標籤或單位可能有誤。處理時仍會依標籤，請先確認原圖的座標系與單位。",
                    "MANUAL_INSPECT",
                )
            if is_twd67:
                is_zone_121 = detected_epsg == EPSG_TWD67_121
                return build(
                    "YELLOW",
                    "TWD67_NEED_7PARAM",
                    "TWD67 基準面偏移警示",
                    f"此檔案標註為舊版 {label_name} (EPSG:{detected_epsg})，與現行 TWD97 相差約 830 公尺。"
                    "本工具處理時會自動改用專案的七參數定義轉換，不需另外處理。",
                    "TRANSFORM_TWD67_7PARAM",
                    recommended_assign_srs=TWD67_121_PROJ if is_zone_121 else TWD67_119_PROJ,
                    recommended_transform_srs=f"EPSG:{EPSG_TWD97_121 if is_zone_121 else EPSG_TWD97_119}",
                )
            if detected_epsg == EPSG_TWD97_121:
                return build(
                    "GREEN",
                    "TWD97_STANDARD",
                    "標準 TWD97 TM2 臺灣本島座標系",
                    "檔案座標落於臺灣二度分帶標準範圍，且已正確標註 EPSG:3826，可直接進行裁切運算。",
                    "NONE",
                )
            return build(
                "GREEN",
                "TWD97_119_STANDARD",
                "TWD97 TM2 119 分帶 (澎湖金門馬祖)",
                "檔案已標註 EPSG:3825 (中央經線 119°E)，處理時會依標籤重投影到目標座標系。",
                "NONE",
            )

        if detected_epsg:
            return build(
                "GREEN",
                "LABELED_OTHER_CRS",
                "已標註其他座標系",
                f"檔案已標註座標系 ({label_text})，處理時會依標籤重投影到目標座標系。"
                "此座標系無法由座標數值檢查是否位於臺灣，請自行確認。",
                "NONE",
            )
        return build(
            "YELLOW",
            "LABELED_UNIDENTIFIED",
            "座標系標籤無法對應 EPSG 代碼",
            f"檔案帶有座標系定義 ({label_text})，但無法對應到 EPSG 代碼，處理時會依檔案內的定義重投影。"
            "若它是自訂的 TWD67 定義而沒有基準面轉換參數，成果會偏移約 830 公尺，請確認原圖的座標系。",
            "MANUAL_INSPECT",
        )

    guessed = guess_srs_from_extent(extent)
    if guessed == f"EPSG:{EPSG_TWD97_121}":
        return build(
            "YELLOW",
            "TM2_UNLABELED",
            "無標籤之臺灣二度分帶 (TM2)",
            "檔案未附帶 .prj 或 CRS 標籤，數值落在臺灣二度分帶範圍，系統預設視為 TWD97 121 分帶 (EPSG:3826)。"
            "121 與 119 分帶、TWD67 與 TWD97 的數值範圍重疊，無法只靠數值區分："
            "澎湖、金門、馬祖的 119 分帶資料請手動指派 TWD97_119；民國 86 年前繪製的圖資請指派 TWD67。",
            "ASSIGN_3826_DEFAULT",
            recommended_assign_srs=f"EPSG:{EPSG_TWD97_121}",
        )
    if guessed == f"EPSG:{EPSG_WGS84}":
        return build(
            "YELLOW",
            "WGS84_UNLABELED",
            "無標籤之經緯度座標",
            "座標值落於臺灣周邊的經緯度範圍，但檔案未標註座標系。系統預設視為 EPSG:4326 後轉為目標座標系。",
            "ASSIGN_4326_AND_CONVERT",
            recommended_assign_srs=f"EPSG:{EPSG_WGS84}",
            recommended_transform_srs=f"EPSG:{EPSG_TWD97_121}",
        )
    if _within_lonlat_bounds(extent):
        return build(
            "RED",
            "LONLAT_UNLABELED_OUTSIDE_TAIWAN",
            "無標籤的小數值座標",
            f"檔案未標註座標系，中心座標 {center_text} 在經緯度的數值範圍內但不在臺灣周邊，"
            "多半是自訂原點的區域座標 (例如假設座標的 CAD 圖)，不會自動當成經緯度。"
            "處理前必須手動指派來源座標系 (確實是經緯度請指派 WGS84)，或先做控制點對齊。",
            "MANUAL_INSPECT",
        )
    if _looks_like_tm2_in_millimetres(extent):
        return millimetre_result()
    return build(
        "RED",
        "CRS_OUT_OF_BOUNDS",
        "無法判斷座標系",
        f"檔案未標註座標系，中心座標 {center_text} 不在經緯度或臺灣二度分帶的數值範圍內，無法推測。"
        "處理前必須手動指派來源座標系，並確認原圖的座標與單位。",
        "MANUAL_INSPECT",
    )


def sniff_vector_crs(vector_path: Union[str, Path]) -> CrsDiagnosisResult:
    """讀取向量檔的四至邊界與座標系標籤進行診斷。

    一個檔有好幾個圖層 (KML 的資料夾、GPKG、GPX) 時，範圍是所有有圖徵的圖層合起來，座標系照第一個有圖徵的圖層。"""
    vec_path = Path(vector_path).resolve()
    if not vec_path.exists():
        raise FileNotFoundError(f"向量檔案不存在: {vec_path}")

    info = geo_io.read_layer_info(vec_path)
    extent = info.extent
    if len(info.layer_names) > 1:
        extents = [geo_io.read_layer_info(vec_path, name).extent for name in geo_io.list_data_layers(vec_path)]
        extents = [item for item in extents if item]
        if extents:
            extent = (
                min(item[0] for item in extents),
                min(item[1] for item in extents),
                max(item[2] for item in extents),
                max(item[3] for item in extents),
            )
    if not extent:
        raise ToolkitError(f"向量檔案 {vec_path.name} 未包含有效的四至邊界 (Extent)！")

    return diagnose_extent(
        extent=Extent2D(*extent),
        file_path=str(vec_path),
        raw_srs_name=info.srs_name,
        detected_epsg=info.epsg,
        has_embedded_srs=info.has_srs,
        is_geographic=info.is_geographic if info.has_srs else None,
    )


def _raster_extent_from_info(info_json: Dict[str, Any], raster_name: str) -> Extent2D:
    """由 gdalinfo -json 的結果取出網格四至邊界；沒有 cornerCoordinates 時自 geoTransform 與 size 回推。"""
    corner_coords = info_json.get("cornerCoordinates", {})
    lower_left = corner_coords.get("lowerLeft")
    upper_right = corner_coords.get("upperRight")

    if lower_left and upper_right:
        min_x = min(float(lower_left[0]), float(upper_right[0]))
        min_y = min(float(lower_left[1]), float(upper_right[1]))
        max_x = max(float(lower_left[0]), float(upper_right[0]))
        max_y = max(float(lower_left[1]), float(upper_right[1]))
    else:
        geo_trans = info_json.get("geoTransform")
        size = info_json.get("size")
        if geo_trans and size and len(geo_trans) >= 6 and len(size) >= 2:
            x0, dx, _, y0, _, dy = [float(v) for v in geo_trans]
            w, h = float(size[0]), float(size[1])
            x1 = x0 + dx * w
            y1 = y0 + dy * h
            min_x = min(x0, x1)
            max_x = max(x0, x1)
            min_y = min(y0, y1)
            max_y = max(y0, y1)
        else:
            raise ToolkitError(f"無法自網格檔案 {raster_name} 取得邊界座標 (Corner Coordinates)！")

    return Extent2D(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)


@dataclass
class _RasterSrsMetadata:
    """網格檔的座標系標籤與四至範圍。"""

    has_embedded_srs: bool
    srs_name: Optional[str]
    epsg: Optional[int]
    is_geographic: bool
    extent: Extent2D


def _read_raster_srs_metadata(raster_path: Path) -> _RasterSrsMetadata:
    """以 gdalinfo -json 讀取網格的座標系標籤與範圍 (診斷與來源座標系決策共用)。"""
    if not raster_path.exists():
        raise FileNotFoundError(f"網格檔案不存在: {raster_path}")

    info_json = run_json_command(["gdalinfo", "-json", str(raster_path)])
    crs_info = info_json.get("coordinateSystem")
    wkt = crs_info.get("wkt") if isinstance(crs_info, dict) else crs_info
    has_embedded_srs = bool(wkt and str(wkt).strip())
    srs_name: Optional[str] = None
    is_geographic = False
    if has_embedded_srs:
        from osgeo import osr

        geo_io.ensure_gdal_ready()
        try:
            srs = osr.SpatialReference(wkt=str(wkt))
            srs_name = srs.GetName() or None
            is_geographic = bool(srs.IsGeographic())
        except RuntimeError as err:
            logger.debug("讀取網格座標系名稱略過: %s", err)

    return _RasterSrsMetadata(
        has_embedded_srs=has_embedded_srs,
        srs_name=srs_name,
        epsg=geo_io.identify_epsg_from_wkt(wkt) if has_embedded_srs else None,
        is_geographic=is_geographic,
        extent=_raster_extent_from_info(info_json, raster_path.name),
    )


def sniff_raster_crs(raster_path: Union[str, Path]) -> CrsDiagnosisResult:
    """呼叫 gdalinfo -json 讀取網格圖資 (DEM, TIF) 之四至邊界與座標系標籤進行診斷。"""
    ras_path = Path(raster_path).resolve()
    meta = _read_raster_srs_metadata(ras_path)
    return diagnose_extent(
        extent=meta.extent,
        file_path=str(ras_path),
        raw_srs_name=meta.srs_name,
        detected_epsg=meta.epsg,
        has_embedded_srs=meta.has_embedded_srs,
        is_geographic=meta.is_geographic if meta.has_embedded_srs else None,
    )


def sniff_crs(file_path: Union[str, Path]) -> CrsDiagnosisResult:
    """自動偵測檔案類型並調用對應的診斷函式。

    支援副檔名：
    - 向量：.shp, .gpkg, .geojson, .dxf, .kml, .kmz, .gpx, .csv
    - 網格：.tif, .tiff, .dem, .vrt, .asc, .img, .png, .jpg, .jpeg
    """
    path = Path(file_path)
    if path.suffix.lower() in _RASTER_SUFFIXES:
        return sniff_raster_crs(path)
    return sniff_vector_crs(path)


# ============================================================================
# 來源座標系決策 (P1-2)：來源有座標系就用來源的，沒有才依範圍推測或由使用者指派
# ============================================================================

# 使用者可指派給「沒有座標系標籤的來源」的座標系 (鍵值供 UI 下拉選單使用)
ASSIGNABLE_SOURCE_SRS: Dict[str, str] = {
    "TWD97_121": f"EPSG:{EPSG_TWD97_121}",
    "TWD97_119": f"EPSG:{EPSG_TWD97_119}",
    "TWD67_121": TWD67_121_PROJ,
    "TWD67_119": TWD67_119_PROJ,
    "WGS84": f"EPSG:{EPSG_WGS84}",
}

# 臺灣 TM2 座標的數值範圍 (公尺)；刻意放寬以涵蓋澎湖、金門、馬祖
_TM2_X_RANGE: Tuple[float, float] = (50_000.0, 450_000.0)
_TM2_Y_RANGE: Tuple[float, float] = (2_350_000.0, 2_950_000.0)

# 來源標籤是 TWD67 的 EPSG 代碼時，PROJ 預設只做三參數平移 (與七參數差約 1 m)，一律改用專案的七參數定義
_EMBEDDED_TWD67_OVERRIDES: Dict[int, Tuple[str, str]] = {
    EPSG_TWD67_121: ("TWD67_121", TWD67_121_PROJ),
    EPSG_TWD67_119: ("TWD67_119", TWD67_119_PROJ),
}


def assigned_srs_for_epsg(epsg: int) -> str:
    """把來源檔的 EPSG 代碼換成可傳給 assigned_srs 的寫法：TWD67 用專案的七參數定義鍵，其他用 EPSG:n。"""
    if epsg in _EMBEDDED_TWD67_OVERRIDES:
        return _EMBEDDED_TWD67_OVERRIDES[epsg][0]
    return f"EPSG:{epsg}"

_RASTER_SUFFIXES = frozenset({".tif", ".tiff", ".dem", ".vrt", ".asc", ".img", ".png", ".jpg", ".jpeg"})


class SourceSrsUndeterminedError(ToolkitError):
    """來源沒有座標系標籤，且無法由座標範圍推測，必須由使用者指定。"""


@dataclass
class SourceSrsDecision:
    """單一來源檔的座標系決策結果。

    s_srs 為 None 表示來源自帶座標系，指令不可再帶 -s_srs；否則為要指派給來源的座標系字串。
    例外：來源標籤是 TWD67 (EPSG:3828／3827) 時 has_embedded_srs 為 True 且 s_srs 為七參數定義，
    重投影時要以它取代標籤，才會與無標籤指派 TWD67_121／TWD67_119 的結果一致。
    """

    file_path: str
    has_embedded_srs: bool
    s_srs: Optional[str]
    is_guess: bool
    source_label: str
    message: str
    extent: Optional[Extent2D] = None

    def as_ogr_args(self) -> List[str]:
        """回傳要附加給 ogr2ogr / gdalwarp 的來源座標系參數；來源自帶座標系時為空串列。"""
        return ["-s_srs", self.s_srs] if self.s_srs else []


def guess_srs_from_extent(extent: Optional[Extent2D]) -> Optional[str]:
    """依四至範圍推測沒有標籤的來源座標系；無法判斷時回傳 None。

    整個落在臺灣周邊的經緯度範圍視為經緯度 (EPSG:4326)；落在臺灣 TM2 數值範圍視為 TWD97 (EPSG:3826)。
    TM2 一律猜 TWD97，不猜 TWD67 (兩者數值範圍重疊，只能由使用者指派)。
    |X|≤180 且 |Y|≤90 但不在臺灣周邊的不猜：自訂原點的 CAD 區域座標 (例如 0～100 公尺) 數值上與經緯度無法區分，
    猜成經緯度會把成果丟到地球另一邊。
    """
    if extent is None:
        return None
    if not all(math.isfinite(value) for value in extent.as_tuple()):
        return None
    xs = (extent.min_x, extent.max_x)
    ys = (extent.min_y, extent.max_y)

    if _within_taiwan_lonlat(extent):
        return f"EPSG:{EPSG_WGS84}"
    if all(_TM2_X_RANGE[0] <= x <= _TM2_X_RANGE[1] for x in xs) and all(
        _TM2_Y_RANGE[0] <= y <= _TM2_Y_RANGE[1] for y in ys
    ):
        return f"EPSG:{EPSG_TWD97_121}"
    return None


def resolve_assigned_srs(assigned_srs: Optional[str]) -> Optional[str]:
    """把使用者指派的座標系轉成可傳給 -s_srs 的字串；未指派時回傳 None。

    可傳 ASSIGNABLE_SOURCE_SRS 的鍵 (例如 "TWD67_121")，或直接傳 EPSG 代碼／PROJ 字串。
    """
    if assigned_srs is None or not str(assigned_srs).strip():
        return None
    text = str(assigned_srs).strip()
    if text.upper() in ASSIGNABLE_SOURCE_SRS:
        return ASSIGNABLE_SOURCE_SRS[text.upper()]

    from osgeo import osr

    geo_io.ensure_gdal_ready()
    try:
        osr.SpatialReference().SetFromUserInput(text)
    except RuntimeError as err:
        choices = "、".join(ASSIGNABLE_SOURCE_SRS)
        raise ToolkitError(f"無法辨識指派的座標系: {assigned_srs} (可用 {choices}，或 EPSG 代碼／PROJ 字串)") from err
    return text


def _decide_from_metadata(
    path: Path,
    has_embedded_srs: bool,
    embedded_name: Optional[str],
    extent: Optional[Extent2D],
    assigned_srs: Optional[str],
    embedded_epsg: Optional[int] = None,
) -> SourceSrsDecision:
    """向量與網格共用的決策規則。"""
    assigned = resolve_assigned_srs(assigned_srs)

    if has_embedded_srs:
        label = embedded_name or "來源內嵌座標系"
        message = f"{path.name}：使用來源自帶的座標系 ({label})。"
        twd67_override: Optional[str] = None
        if embedded_epsg in _EMBEDDED_TWD67_OVERRIDES:
            twd67_key, twd67_override = _EMBEDDED_TWD67_OVERRIDES[embedded_epsg]
            message += f"來源標籤為 EPSG:{embedded_epsg}，重投影時改用專案的七參數定義 ({twd67_key})。"
        if assigned:
            message += "來源已有座標系，指派的座標系不予採用。"
        return SourceSrsDecision(
            file_path=str(path),
            has_embedded_srs=True,
            s_srs=twd67_override,
            is_guess=False,
            source_label=label,
            message=message,
            extent=extent,
        )

    if assigned:
        assigned_label = str(assigned_srs).strip()
        return SourceSrsDecision(
            file_path=str(path),
            has_embedded_srs=False,
            s_srs=assigned,
            is_guess=False,
            source_label=assigned_label,
            message=f"{path.name}：來源沒有座標系標籤，依使用者指派視為 {assigned_label}。",
            extent=extent,
        )

    guessed = guess_srs_from_extent(extent)
    if guessed is None:
        if extent is None:
            reason = "也沒有可供判斷的座標範圍"
        else:
            range_text = f"座標範圍 X {extent.min_x:.3f}～{extent.max_x:.3f}、Y {extent.min_y:.3f}～{extent.max_y:.3f} "
            if all(math.isfinite(value) for value in extent.as_tuple()) and _within_lonlat_bounds(extent):
                reason = range_text + "在經緯度的數值範圍內但不在臺灣周邊 (可能是自訂原點的區域座標)"
            else:
                reason = range_text + "不在臺灣周邊的經緯度或臺灣 TM2 的數值範圍內"
        raise SourceSrsUndeterminedError(
            f"{path.name} 沒有座標系標籤，{reason}，無法推測。請指定來源座標系 (可用 {'、'.join(ASSIGNABLE_SOURCE_SRS)})。"
        )

    message = (
        f"{path.name}：來源沒有座標系標籤，依座標範圍視為 {guessed}，座標系為推測值；"
        "若實際為 TWD67 或其他座標系，請改為手動指派。"
    )
    logger.warning(message)
    return SourceSrsDecision(
        file_path=str(path),
        has_embedded_srs=False,
        s_srs=guessed,
        is_guess=True,
        source_label=f"{guessed} (推測)",
        message=message,
        extent=extent,
    )


def decide_vector_source_srs(
    vector_path: Union[str, Path],
    assigned_srs: Optional[str] = None,
    *,
    layer_name: Optional[str] = None,
) -> SourceSrsDecision:
    """決定向量來源要不要指派座標系；來源檔本身不會被改動。"""
    path = Path(vector_path)
    info = geo_io.read_layer_info(path, layer_name)
    extent = Extent2D(*info.extent) if info.extent else None
    return _decide_from_metadata(path, info.has_srs, info.srs_name, extent, assigned_srs, info.epsg)


def decide_raster_source_srs(
    raster_path: Union[str, Path],
    assigned_srs: Optional[str] = None,
) -> SourceSrsDecision:
    """決定網格來源要不要指派座標系；來源檔本身不會被改動。"""
    path = Path(raster_path)
    meta = _read_raster_srs_metadata(path)
    return _decide_from_metadata(path, meta.has_embedded_srs, meta.srs_name, meta.extent, assigned_srs, meta.epsg)


def decide_source_srs(
    file_path: Union[str, Path],
    assigned_srs: Optional[str] = None,
    *,
    layer_name: Optional[str] = None,
) -> SourceSrsDecision:
    """依副檔名分流到向量或網格的來源座標系決策。"""
    path = Path(file_path)
    if path.suffix.lower() in _RASTER_SUFFIXES:
        return decide_raster_source_srs(path, assigned_srs)
    return decide_vector_source_srs(path, assigned_srs, layer_name=layer_name)


def decide_and_report_source_srs(
    file_path: Union[str, Path],
    assigned_srs: Optional[str] = None,
    *,
    layer_name: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> SourceSrsDecision:
    """引擎函式用的入口：做出來源座標系決策，並在「指派或推測」時把訊息送到日誌框。"""
    decision = decide_source_srs(file_path, assigned_srs, layer_name=layer_name)
    if text_callback and decision.s_srs:
        prefix = "[注意] " if decision.is_guess else "[座標系] "
        text_callback(f"{prefix}{decision.message}\n")
    return decision


# ============================================================================
# [G-02] TWD67 ↔ TWD97 七參數轉換指令生成
# ============================================================================

_TWD67_ZONE_DEFINITIONS: Dict[int, Tuple[str, int]] = {
    121: (TWD67_121_PROJ, EPSG_TWD97_121),
    119: (TWD67_119_PROJ, EPSG_TWD97_119),
}


def build_twd67_to_twd97_srs_args(zone: int = 121, target_srs: Optional[str] = None) -> List[str]:
    """產出將 TWD67 轉換為 TWD97 所需之 GDAL/OGR 參數串列。

    嚴格解決 850 公尺基準面偏移地雷，-s_srs 強制注入完整臺灣七參數定義。
    zone 只接受 121 (本島) 或 119 (澎湖金門馬祖)；未指定 target_srs 時轉到同分帶的 TWD97
    (121 → EPSG:3826、119 → EPSG:3825)。
    """
    if zone not in _TWD67_ZONE_DEFINITIONS:
        raise ValueError(f"TWD67 分帶只接受 121 或 119，收到: {zone!r}")
    source_proj, default_target_epsg = _TWD67_ZONE_DEFINITIONS[zone]
    target = resolve_assigned_srs(target_srs) or f"EPSG:{default_target_epsg}"
    return ["-s_srs", source_proj, "-t_srs", target]


def convert_twd67_vector_to_twd97(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    zone: int = 121,
    target_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """透過 ogr2ogr 將向量資料自 TWD67 (七參數) 精確平移並轉換為 TWD97。

    預設轉到同分帶的 TWD97 (zone=121 → EPSG:3826、zone=119 → EPSG:3825)，可用 target_srs 另外指定。
    """
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    if not in_p.exists():
        raise FileNotFoundError(f"輸入圖資不存在: {in_p}")

    source_srs, resolved_target = build_twd67_to_twd97_srs_args(zone=zone, target_srs=target_srs)[1::2]
    with geo_io.TempVectorFiles("twd67") as temp_files:
        staged_gpkg = temp_files.new("stage")
        geo_io.stage_vector_to_gpkg(
            in_p,
            staged_gpkg,
            source_srs=source_srs,
            target_srs=resolved_target,
            make_valid=True,
            progress_callback=geo_io.scaled_progress(progress_callback, 0.0, 70.0),
            text_callback=text_callback,
        )
        geo_io.export_vector(
            staged_gpkg,
            "stage",
            out_p,
            progress_callback=geo_io.scaled_progress(progress_callback, 70.0, 100.0),
            text_callback=text_callback,
        )
    return out_p


def convert_twd67_raster_to_twd97(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    zone: int = 121,
    target_srs: Optional[str] = None,
    resampling: str = "bilinear",
    target_res: Optional[float] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> CommandResult:
    """透過 gdalwarp 將 DEM/高程網格自 TWD67 (七參數) 重投影至 TWD97。

    預設轉到同分帶的 TWD97 (zone=121 → EPSG:3826、zone=119 → EPSG:3825)，可用 target_srs 另外指定。
    """
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    if not in_p.exists():
        raise FileNotFoundError(f"輸入圖資不存在: {in_p}")
    srs_args = build_twd67_to_twd97_srs_args(zone=zone, target_srs=target_srs)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "gdalwarp",
        *srs_args,
        "-r", resampling,
        "-co", "COMPRESS=LZW",
        "-overwrite",
    ]

    if target_res is not None and target_res > 0:
        cmd.extend(build_tap_parameters(target_res_x=target_res, target_res_y=target_res))

    cmd.extend([str(in_p), str(out_p)])

    return run_command(cmd, progress_callback=progress_callback, text_callback=text_callback)


# ============================================================================
# [G-03] CAD (.dxf) 單位縮放與 GCP 控制點一階仿射變換
# ============================================================================

def scale_vector_coordinates(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    scale_factor: float = 0.001,
    *,
    layer_name: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> CommandResult:
    """利用 ogr2ogr 的 SQLite 方言 ScaleCoords 函式對幾何坐標進行等比縮放（例如 mm 轉 m 乘 0.001）。

    來源先正規化成暫存 GPKG (幾何欄位固定為 geom)，SQL 才不必猜來源的幾何欄位名與圖層名；
    輸出格式依 output_path 的副檔名決定。回傳值為縮放那一步的 CommandResult。
    """
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    factor = float(scale_factor)
    if not math.isfinite(factor) or factor == 0.0:
        raise ToolkitError(f"縮放倍率必須是非零的有限數值: {scale_factor}")

    with geo_io.TempVectorFiles("scale") as tmp:
        staged = geo_io.stage_vector_to_gpkg(
            in_p,
            tmp.new("src"),
            source_layer=layer_name,
            stage_layer="src",
            text_callback=text_callback,
        )
        geom_ident = geo_io.quote_ident(staged.geometry_column)
        select_items = [f"ScaleCoords({geom_ident}, {factor!r}, {factor!r}) AS {geom_ident}"]
        select_items.extend(geo_io.build_field_select_list(staged))
        sql = f"SELECT {', '.join(select_items)} FROM {geo_io.quote_ident(staged.layer_name)}"

        scaled_gpkg = tmp.new("scaled")
        result = geo_io.run_sql_to_gpkg(
            staged.path,
            sql,
            scaled_gpkg,
            "scaled",
            extra_args=["-makevalid"],
            progress_callback=progress_callback,
            text_callback=text_callback,
        )
        geo_io.export_vector(scaled_gpkg, "scaled", out_p, text_callback=text_callback)

    return result


# 控制點共線判定：中心化後第二奇異值與第一奇異值的比值低於此值，視為共線
_COLLINEAR_RELATIVE_TOLERANCE = 1e-9


def _require_non_collinear_sources(control_points: Sequence[ControlPoint]) -> Tuple[float, float]:
    """確認控制點的來源座標有效且不共線，回傳來源座標的平均值 (供中心化使用)。"""
    coords = np.array(
        [[pt.src_x, pt.src_y, pt.dst_x, pt.dst_y] for pt in control_points], dtype=np.float64
    )
    if not np.all(np.isfinite(coords)):
        raise ValueError("控制點座標含有無限大或非數值，無法計算仿射變換！")

    mean_x, mean_y = float(coords[:, 0].mean()), float(coords[:, 1].mean())
    centered = coords[:, :2] - np.array([mean_x, mean_y])
    singular_values = np.linalg.svd(centered, compute_uv=False)
    if singular_values[0] == 0.0:
        raise ValueError("控制點的來源座標全部重疊，無法計算仿射變換，請改選分散的控制點！")
    if singular_values[-1] <= singular_values[0] * _COLLINEAR_RELATIVE_TOLERANCE:
        raise ValueError("控制點的來源座標共線 (排成一直線)，無法決定仿射變換，請至少加入一個不在同一直線上的控制點！")
    return mean_x, mean_y


def calculate_affine_transform(control_points: Sequence[ControlPoint]) -> AffineTransformResult:
    """純 Python + NumPy 最小平方法 (Least Squares) 計算 2D 一階仿射變換矩陣與 RMSE。

    仿射變換方程式：
        X_dst = A * X_src + B * Y_src + C
        Y_dst = D * X_src + E * Y_src + F

    參數：
        control_points: 至少需 3 組 (N >= 3) 不共線之控制點對 (src_x, src_y, dst_x, dst_y)。
        點數不足、來源座標共線或重疊時拋出 ValueError。

    回傳：
        AffineTransformResult 包含 6 個變換參數、RMSE (公尺) 與各點殘差。
    """
    n = len(control_points)
    if n < 3:
        raise ValueError(f"一階仿射變換至少需要 3 組不共線控制點，目前僅提供 {n} 組！")
    mean_x, mean_y = _require_non_collinear_sources(control_points)

    # 構造矩陣 M 與向量 U, V；來源座標先減去平均值，TM2 這種百萬級的數值才不會讓矩陣病態
    # [x - mx, y - my, 1] * [A, B, C']^T = X_dst
    # [x - mx, y - my, 1] * [D, E, F']^T = Y_dst
    matrix_m = np.zeros((n, 3), dtype=np.float64)
    vec_u = np.zeros(n, dtype=np.float64)
    vec_v = np.zeros(n, dtype=np.float64)

    for i, pt in enumerate(control_points):
        matrix_m[i, 0] = pt.src_x - mean_x
        matrix_m[i, 1] = pt.src_y - mean_y
        matrix_m[i, 2] = 1.0
        vec_u[i] = pt.dst_x
        vec_v[i] = pt.dst_y

    # 解最小平方法
    try:
        params_x = np.linalg.lstsq(matrix_m, vec_u, rcond=None)[0]
        params_y = np.linalg.lstsq(matrix_m, vec_v, rcond=None)[0]
    except np.linalg.LinAlgError as err:
        raise ToolkitError(f"仿射變換求解失敗，控制點可能共線或重疊: {err}") from err

    a, b = float(params_x[0]), float(params_x[1])
    d, e = float(params_y[0]), float(params_y[1])
    c = float(params_x[2]) - a * mean_x - b * mean_y
    f = float(params_y[2]) - d * mean_x - e * mean_y

    # 計算各點殘差與整體均方根誤差 (RMSE)
    residuals: List[float] = []
    sq_err_sum = 0.0

    for pt in control_points:
        est_x = a * pt.src_x + b * pt.src_y + c
        est_y = d * pt.src_x + e * pt.src_y + f
        dx = est_x - pt.dst_x
        dy = est_y - pt.dst_y
        dist_err = math.sqrt(dx * dx + dy * dy)
        residuals.append(dist_err)
        sq_err_sum += dist_err * dist_err

    rmse = math.sqrt(sq_err_sum / n)

    return AffineTransformResult(
        a=a,
        b=b,
        c=c,
        d=d,
        e=e,
        f=f,
        rmse=rmse,
        residuals=residuals,
    )


def build_gcp_arguments(control_points: Sequence[ControlPoint], order: int = 1) -> List[str]:
    """生成 GDAL CLI 專用之 -gcp 參數列與 -order 參數。

    格式：-gcp <src_x> <src_y> <dst_x> <dst_y> 重複多次後接 -order 1
    """
    if len(control_points) < 3 and order >= 1:
        raise ValueError(f"仿射校正 (order={order}) 至少需要 3 組 GCP 控制點！")
    if order >= 1:
        _require_non_collinear_sources(control_points)

    args: List[str] = []
    for pt in control_points:
        args.extend(["-gcp", f"{pt.src_x:.6f}", f"{pt.src_y:.6f}", f"{pt.dst_x:.6f}", f"{pt.dst_y:.6f}"])

    args.extend(["-order", str(order)])
    return args


def parse_control_points_text(raw_text: str) -> List[ControlPoint]:
    """把使用者貼上的控制點文字解析成 ControlPoint 清單。

    每列一組「圖面X, 圖面Y, 真實X, 真實Y」，以逗號、Tab 或空白分隔；空白列與 # 開頭的列略過。
    欄數不是 4 或含非數值時拋 ValueError，訊息帶列號。
    """
    points: List[ControlPoint] = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.replace(",", " ").replace("，", " ").replace("\t", " ").split()
        if len(tokens) != 4:
            raise ValueError(f"第 {line_no} 列應為 4 個數值 (圖面X, 圖面Y, 真實X, 真實Y)，實際讀到 {len(tokens)} 個: {line}")
        try:
            values = [float(token) for token in tokens]
        except ValueError:
            raise ValueError(f"第 {line_no} 列含有不是數值的內容: {line}") from None
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"第 {line_no} 列含有無限大或非數值: {line}")
        points.append(ControlPoint(values[0], values[1], values[2], values[3], point_id=str(len(points) + 1)))
    return points


def georeference_vector_with_gcps(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    control_points: Sequence[ControlPoint],
    *,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    layer_name: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> GcpGeoreferenceResult:
    """以控制點對向量圖資 (通常是任意座標的 CAD 圖) 做一階仿射校正，成果標成 target_srs。

    控制點的「真實座標」必須就是 target_srs 的座標。先以最小平方法算出仿射參數與 RMSE (供使用者判斷
    控制點品質)，實際的座標變換交給 ogr2ogr -gcp -order 1；輸出格式依 output_path 的副檔名決定。
    控制點不足 3 組或共線時拋 ValueError。
    """
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    points = list(control_points)
    transform = calculate_affine_transform(points)
    gcp_args = build_gcp_arguments(points, order=1)
    if text_callback:
        text_callback(f"[GCP 校正] 控制點 {len(points)} 組，仿射變換 RMSE = {transform.rmse:.4f}\n")

    with geo_io.TempVectorFiles("gcp") as tmp:
        staged = geo_io.stage_vector_to_gpkg(
            in_p,
            tmp.new("src"),
            source_layer=layer_name,
            stage_layer="src",
            text_callback=text_callback,
        )
        warped_gpkg = tmp.new("warped")
        cmd = ["ogr2ogr", "-f", "GPKG", str(warped_gpkg), str(staged.path)]
        cmd.extend(gcp_args)
        cmd.extend(["-a_srs", target_srs, "-nln", "warped", "-lco", "GEOMETRY_NAME=geom", staged.layer_name])
        run_command(cmd, progress_callback=progress_callback, text_callback=text_callback, check=True)
        geo_io.export_vector(warped_gpkg, "warped", out_p, text_callback=text_callback)

    return GcpGeoreferenceResult(
        output_path=out_p,
        transform=transform,
        control_point_count=len(points),
        target_srs=target_srs,
    )


# 經緯度 1 度大約的公尺數 (南北向；東西向再乘緯度的 cos)，只用來估原圖解析度
_METERS_PER_DEGREE: float = 111_320.0


def raster_pixel_size_m(raster_path: Union[str, Path]) -> Tuple[float, bool]:
    """原圖一個像素的大小 (公尺，長寬取較大的)，回傳 (公尺, 是否為估計值)。

    投影座標 (TWD97／TWD67) 直接是公尺；經緯度的圖 (有標籤，或沒標籤但座標在經緯度範圍內) 依中心緯度換算，是估計值。
    給裁切工具寫「原圖 1 m」與擋下比原圖細的解析度用。"""
    ras_path = Path(raster_path).resolve()
    info_json = run_json_command(["gdalinfo", "-json", str(ras_path)])
    geo_trans = info_json.get("geoTransform")
    if not geo_trans or len(geo_trans) < 6:
        raise ToolkitError(f"無法自網格檔案 {ras_path.name} 取得 geoTransform 像素解析度！")
    xres, yres = abs(float(geo_trans[1])), abs(float(geo_trans[5]))
    wkt = str((info_json.get("coordinateSystem") or {}).get("wkt") or "").lstrip().upper()
    extent = _raster_extent_from_info(info_json, ras_path.name)
    if wkt:
        geographic = wkt.startswith(("GEOGCRS", "GEOGCS"))
    else:
        geographic = all(abs(v) <= 180 for v in (extent.min_x, extent.max_x)) and all(
            abs(v) <= 90 for v in (extent.min_y, extent.max_y)
        )
    if not geographic:
        return max(xres, yres), False
    center_lat = math.radians((extent.min_y + extent.max_y) / 2)
    return max(xres * _METERS_PER_DEGREE * math.cos(center_lat), yres * _METERS_PER_DEGREE), True


# ============================================================================
# [G-04] GDAL -tap (Target Aligned Pixels) 致命語法防呆
# ============================================================================

def get_raster_resolution(raster_path: Union[str, Path]) -> Tuple[float, float]:
    """呼叫 gdalinfo -json 讀取網格圖資之原圖空間解析度 (xres, yres)。"""
    ras_path = Path(raster_path).resolve()
    if not ras_path.exists():
        raise FileNotFoundError(f"網格檔案不存在: {ras_path}")

    info_json = run_json_command(["gdalinfo", "-json", str(ras_path)])
    geo_trans = info_json.get("geoTransform")

    if not geo_trans or len(geo_trans) < 6:
        raise ToolkitError(f"無法自網格檔案 {ras_path.name} 取得 geoTransform 像素解析度！")

    xres = abs(float(geo_trans[1]))
    yres = abs(float(geo_trans[5]))

    if xres <= 0 or yres <= 0:
        raise ToolkitError(f"網格檔案解析度非正數: xres={xres}, yres={yres}")

    return (xres, yres)


def srs_is_geographic(srs_text: str) -> bool:
    """判斷座標系定義 (EPSG 代碼、PROJ 字串或 WKT) 是不是以「度」為單位的經緯度座標系。"""
    from osgeo import osr

    geo_io.ensure_gdal_ready()
    srs = osr.SpatialReference()
    try:
        srs.SetFromUserInput(str(srs_text))
    except RuntimeError as err:
        raise ToolkitError(f"無法辨識座標系定義: {srs_text}") from err
    return bool(srs.IsGeographic())


def _round_to_significant(value: float, digits: int = 2) -> float:
    """四捨五入到指定的有效位數。"""
    if value == 0:
        return 0.0
    return round(value, digits - 1 - int(math.floor(math.log10(abs(value)))))


def resolve_warp_resolution(
    raster_path: Union[str, Path],
    source_decision: SourceSrsDecision,
    target_srs: str,
    target_resolution: Optional[float] = None,
    *,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[float, float]:
    """決定 gdalwarp 的 -tr 解析度 (單位是目標座標系的單位)。

    - 有指定 target_resolution：照用。
    - 沒指定、來源與目標同為公尺或同為度：沿用來源解析度。
    - 沒指定、來源與目標單位不同 (經緯度 DEM 轉 TWD97，或反過來)：來源解析度的數值不能直接拿來用
      (0.00005 度會被當成 0.00005 公尺)，改問 gdalwarp 重投影後的原生解析度，取兩位有效數字的正方形像元。
    """
    if target_resolution is not None:
        if not target_resolution > 0:
            raise ValueError(f"目標解析度必須為正數: {target_resolution}")
        return (float(target_resolution), float(target_resolution))

    ras_path = Path(raster_path).resolve()
    orig_x, orig_y = get_raster_resolution(ras_path)
    if source_decision.s_srs:
        source_is_geographic = srs_is_geographic(source_decision.s_srs)
    else:
        source_is_geographic = _read_raster_srs_metadata(ras_path).is_geographic
    if source_is_geographic == srs_is_geographic(target_srs):
        return (orig_x, orig_y)

    with geo_io.TempVectorFiles("warp_resolution") as temp_files:
        probe_vrt = temp_files.new("probe", ".vrt")
        run_command(
            ["gdalwarp", "-of", "VRT", "-overwrite", *source_decision.as_ogr_args(), "-t_srs", target_srs, str(ras_path), str(probe_vrt)],
            check=True,
        )
        native_x, native_y = get_raster_resolution(probe_vrt)
    resolution = _round_to_significant(min(native_x, native_y))
    if text_callback:
        source_unit, target_unit = ("度", "公尺") if source_is_geographic else ("公尺", "度")
        text_callback(
            f"[注意] {ras_path.name} 的解析度單位是{source_unit}、目標座標系是{target_unit}，未指定目標解析度，"
            f"改用重投影後的原生解析度 {resolution:g} {target_unit}；需要整數解析度請自行指定。\n"
        )
    return (resolution, resolution)


def build_tap_parameters(
    target_res_x: Optional[float] = None,
    target_res_y: Optional[float] = None,
    raster_path: Optional[Union[str, Path]] = None,
) -> List[str]:
    """GDAL -tap (Target Aligned Pixels) 致命防呆參數建構器。

    關鍵鐵律（規格書 2.3 節）：
    在 gdalwarp 中若只寫 -tap 而沒有同時給定 -tr <xres> <yres>，GDAL 會直接報錯崩潰！
    1. 若有指定解析度：回傳 ["-tr", str(xres), str(yres), "-tap"]。
    2. 若未指定解析度但提供 raster_path：自 gdalinfo 讀取原圖解析度後，安全回傳。
    3. 若兩者皆無：主動拋出 ValueError，絕不允許孤立的 -tap 傳入 CLI！
    """
    if target_res_x is not None and target_res_y is not None:
        if target_res_x <= 0 or target_res_y <= 0:
            raise ValueError(f"目標解析度必須為正數，目前傳入: ({target_res_x}, {target_res_y})")
        return ["-tr", f"{target_res_x:g}", f"{target_res_y:g}", "-tap"]

    if target_res_x is not None and target_res_y is None:
        if target_res_x <= 0:
            raise ValueError(f"目標解析度必須為正數: {target_res_x}")
        return ["-tr", f"{target_res_x:g}", f"{target_res_x:g}", "-tap"]

    # 使用者選擇維持原解析度但要求像元對齊 (-tap)
    if raster_path is not None:
        orig_x, orig_y = get_raster_resolution(raster_path)
        return ["-tr", f"{orig_x:g}", f"{orig_y:g}", "-tap"]

    raise ValueError(
        "【致命防呆攔截】GDAL -tap (Target Aligned Pixels) 語法嚴禁單獨使用！"
        "必須同時指定目標解析度 (-tr <xres> <yres>) 或傳入原圖以供動態解析！"
    )
