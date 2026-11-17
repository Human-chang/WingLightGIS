"""邊界生成、閉鎖座標解析、DXF 線轉面與環域 (Buffer) 引擎 (core/boundary_builder.py)

嚴格遵守專案規格書第 0 節與第 3.1 節鐵律：
1. 嚴禁使用 geopandas, rasterio, shapely, pyproj。
2. [G-00] 手動輸入/貼上閉鎖座標多邊形產生器：
   - 支援 Excel (Tab)、CSV (逗號)、空格分隔，支援點名+座標 3 欄格式。
   - 自動判斷二度分帶與經緯度欄位。
   - 自動閉鎖首尾端點 (Auto-Close Ring)。
   - 提供形心極角排序 (atan2) 防止沙漏狀自相交。
   - 鞋帶公式 (Shoelace formula) 即時計算精確面積 (m² 與公頃 ha)。
   - 透過 ogr2ogr 輸出統一基準 EPSG:3826 之 ./workspace_temp/master_boundary.gpkg。
3. [G-05] DXF 圖層萃取、淨化與假封閉線轉面 (LINESTRING to POLYGON)。
4. [G-08] 邊界與路線環域外推 (ST_Buffer)。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from core import geo_io
from core.crs_doctor import (
    _TM2_X_RANGE,
    _TM2_Y_RANGE,
    ASSIGNABLE_SOURCE_SRS,
    EPSG_TWD97_121,
    Extent2D,
    SourceSrsUndeterminedError,
    decide_and_report_source_srs,
    guess_srs_from_extent,
    resolve_assigned_srs,
)
from core.env_runner import (
    CommandResult,
    ToolkitError,
    get_workspace_temp_dir,
    run_command,
    run_json_command,
)

logger = logging.getLogger("core.boundary_builder")


@dataclass
class CoordinatePoint:
    """座標點結構。"""

    x: float
    y: float
    point_id: Optional[str] = None

    def as_tuple(self) -> Tuple[float, float]:
        """轉為標準 (x, y) 數值元組。"""
        return (self.x, self.y)


@dataclass
class BoundaryBuildResult:
    """邊界產生運算結果資料結構。"""

    geojson_path: Path
    gpkg_path: Path
    vertex_count: int
    was_auto_closed: bool
    was_polar_sorted: bool
    area_m2: float
    area_ha: float
    extent: Extent2D
    target_srs: str = f"EPSG:{EPSG_TWD97_121}"
    # 邊界檔裡用的是哪個圖層 (KML 的資料夾、GPKG 的圖層)；只有一個圖層或貼座標、DXF 時是 None
    layer_name: Optional[str] = None


# ============================================================================
# [G-00] 核心數學演算法：鞋帶公式、自動閉鎖與極角排序
# ============================================================================

def calculate_shoelace_area(points: Sequence[Union[CoordinatePoint, Tuple[float, float]]]) -> Tuple[float, float]:
    """使用鞋帶公式 (Shoelace formula / Gauss's area formula) 計算任意多邊形面積。

    公式：
        A = 0.5 * | sum_{i=0}^{n-1} (X_i * Y_{i+1} - X_{i+1} * Y_i) |

    回傳：
        (area_m2, area_ha)：面積平方公尺與公頃 (1 ha = 10,000 m²)。
    """
    if len(points) < 3:
        return (0.0, 0.0)

    # 提取純數值元組串列
    pts: List[Tuple[float, float]] = [
        (p.x, p.y) if isinstance(p, CoordinatePoint) else (float(p[0]), float(p[1]))
        for p in points
    ]

    # 若末端未閉合，暫時補上首點以利循環加總
    n = len(pts)
    first_pt = pts[0]
    last_pt = pts[-1]
    dist_tail_to_head = math.hypot(last_pt[0] - first_pt[0], last_pt[1] - first_pt[1])

    eval_pts = list(pts)
    if dist_tail_to_head > 1e-4:
        eval_pts.append(first_pt)

    area_sum = 0.0
    for i in range(len(eval_pts) - 1):
        x_i, y_i = eval_pts[i]
        x_next, y_next = eval_pts[i + 1]
        area_sum += (x_i * y_next) - (x_next * y_i)

    area_m2 = abs(area_sum) * 0.5
    area_ha = area_m2 / 10000.0

    return (round(area_m2, 4), round(area_ha, 6))


def auto_close_ring(
    points: Sequence[Union[CoordinatePoint, Tuple[float, float]]],
    tolerance: float = 1e-4,
) -> Tuple[List[CoordinatePoint], bool]:
    """自動閉鎖多邊形邊界環 (Auto-Close Ring)。

    檢查首點 (X_0, Y_0) 與末點 (X_n, Y_n) 之歐式距離。
    若距離 > tolerance，自動在尾端複製追加首點完成閉合。
    """
    if not points:
        return ([], False)

    pts: List[CoordinatePoint] = [
        p if isinstance(p, CoordinatePoint) else CoordinatePoint(x=float(p[0]), y=float(p[1]))
        for p in points
    ]

    if len(pts) < 3:
        raise ValueError(f"構建多邊形至少需要 3 個頂點，目前僅提供 {len(pts)} 點！")

    first_pt = pts[0]
    last_pt = pts[-1]
    dist = math.hypot(last_pt.x - first_pt.x, last_pt.y - first_pt.y)

    was_closed = False
    if dist > tolerance:
        closed_point = CoordinatePoint(x=first_pt.x, y=first_pt.y, point_id=first_pt.point_id)
        pts.append(closed_point)
        was_closed = True

    return (pts, was_closed)


def sort_points_by_polar_angle(
    points: Sequence[Union[CoordinatePoint, Tuple[float, float]]],
) -> List[CoordinatePoint]:
    """依多邊形幾何形心極角 (atan2) 自動排序頂點（順序錯亂修復）。

    解決問題：工程人員從試算表複製貼上時，點位順序若交錯會造成「沙漏狀 (Bow-tie) 自相交多邊形」。
    透過形心座標 (cx, cy) 計算極角 theta = atan2(y - cy, x - cx)，
    將頂點依逆時針順序重新排列，最後再自動閉合。
    """
    pts: List[CoordinatePoint] = [
        p if isinstance(p, CoordinatePoint) else CoordinatePoint(x=float(p[0]), y=float(p[1]))
        for p in points
    ]

    if len(pts) < 3:
        return pts

    # 若末點與首點相同，先剔除閉鎖點以計算真實形心
    unique_pts: List[CoordinatePoint] = []
    first_pt = pts[0]
    for i, pt in enumerate(pts):
        if i > 0 and i == len(pts) - 1:
            if math.hypot(pt.x - first_pt.x, pt.y - first_pt.y) <= 1e-4:
                continue
        unique_pts.append(pt)

    if len(unique_pts) < 3:
        return pts

    # 1. 計算幾何形心
    center_x = sum(p.x for p in unique_pts) / len(unique_pts)
    center_y = sum(p.y for p in unique_pts) / len(unique_pts)

    # 2. 計算極角並排序 (逆時針方向)
    def _polar_key(p: CoordinatePoint) -> float:
        return math.atan2(p.y - center_y, p.x - center_x)

    sorted_pts = sorted(unique_pts, key=_polar_key)

    # 3. 排序完成後首尾閉合
    closed_pts, _ = auto_close_ring(sorted_pts)
    return closed_pts


# ============================================================================
# [G-00] 剪貼簿/純文字座標列智慧解析器
# ============================================================================

_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_INTEGER_RE = re.compile(r"^\d+$")
_QUOTED_RE = re.compile(r'"([^"]*)"')

# 臺灣地區經緯度的數值範圍 (含澎湖、金門、馬祖)；與 TM2 範圍不重疊，可用來分辨欄位順序
_TAIWAN_LON_RANGE: Tuple[float, float] = (117.0, 125.0)
_TAIWAN_LAT_RANGE: Tuple[float, float] = (20.0, 27.0)

COORD_KIND_TM2 = "TM2"
COORD_KIND_LONLAT = "LONLAT"


def _clean_numeric_str(token: str) -> Optional[float]:
    """嘗試自字串中清理出浮點數值（去除引號；整個字串符合千分位格式時才去除逗號）。"""
    s = token.strip().replace('"', "").replace("'", "")
    if _THOUSANDS_RE.match(s):
        s = s.replace(",", "")
    if not _NUMBER_RE.match(s):
        return None
    value = float(s)
    return value if math.isfinite(value) else None


def _split_coordinate_line(line: str) -> List[str]:
    """把一列文字切成欄位。

    逗號可能是分隔符，也可能是千分位：先以 Tab／分號／空白切開，切出來的片段若符合千分位格式
    (例如 250,100.5) 且這樣解讀能得到至少兩個數值，就當千分位；否則逗號一律當分隔符。
    """
    def _unquote(match: "re.Match[str]") -> str:
        inner = match.group(1).strip()
        return inner.replace(",", "") if _THOUSANDS_RE.match(inner) else inner

    chunks = [c.strip(",") for c in re.split(r"[\t;\s]+", _QUOTED_RE.sub(_unquote, line))]
    chunks = [c for c in chunks if c]
    if any(_THOUSANDS_RE.match(c) for c in chunks):
        if sum(1 for c in chunks if _clean_numeric_str(c) is not None) >= 2:
            return chunks
    return [t.strip() for c in chunks for t in c.split(",") if t.strip()]


def _in_range(value: float, bounds: Tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _classify_coordinate_pair(val1: float, val2: float) -> Optional[Tuple[str, float, float]]:
    """判斷兩個數值是不是臺灣 TM2 或經緯度座標，回傳 (種類, X, Y)；欄位倒置時自動對調。"""
    if _in_range(val1, _TM2_X_RANGE) and _in_range(val2, _TM2_Y_RANGE):
        return (COORD_KIND_TM2, val1, val2)
    if _in_range(val1, _TM2_Y_RANGE) and _in_range(val2, _TM2_X_RANGE):
        return (COORD_KIND_TM2, val2, val1)
    if _in_range(val1, _TAIWAN_LON_RANGE) and _in_range(val2, _TAIWAN_LAT_RANGE):
        return (COORD_KIND_LONLAT, val1, val2)
    if _in_range(val1, _TAIWAN_LAT_RANGE) and _in_range(val2, _TAIWAN_LON_RANGE):
        return (COORD_KIND_LONLAT, val2, val1)
    return None


def detect_coordinate_kind(points: Sequence[CoordinatePoint]) -> Optional[str]:
    """回傳點列的座標種類：全部落在臺灣 TM2 範圍為 "TM2"、全部落在臺灣經緯度範圍為 "LONLAT"，否則 None。"""
    kinds = set()
    for p in points:
        if _in_range(p.x, _TM2_X_RANGE) and _in_range(p.y, _TM2_Y_RANGE):
            kinds.add(COORD_KIND_TM2)
        elif _in_range(p.x, _TAIWAN_LON_RANGE) and _in_range(p.y, _TAIWAN_LAT_RANGE):
            kinds.add(COORD_KIND_LONLAT)
        else:
            return None
    return kinds.pop() if len(kinds) == 1 else None


def count_distinct_vertices(points: Sequence[CoordinatePoint]) -> int:
    """計算相異頂點數；經緯度比到小數 9 位，其餘比到 0.1 公釐。"""
    digits = 9 if detect_coordinate_kind(points) == COORD_KIND_LONLAT else 4
    return len({(round(p.x, digits), round(p.y, digits)) for p in points})


def parse_coordinate_text(raw_text: str) -> List[CoordinatePoint]:
    """智慧解析使用者從 Excel (Tab 分隔)、CSV、文字檔貼上之多行座標點字串。

    支援情境：
    1. 雙欄標準格式：X, Y 或 經度, 緯度
    2. 三欄含點號格式：P1, 250100.5, 2650200.0 或 250100.5, 2650200.0, P1；
       點號是純數字 (1, 250100.5, 2650200.0) 時，以整份資料判斷第一欄是點號還是座標。
    3. 千分位數值 (250,100.5)：欄位以 Tab、分號或空白分隔時才會被當成千分位。
    4. 自動座標欄位防呆：
       數值落在臺灣二度分帶或經緯度範圍時，即使 Y 貼在前、X 貼在後 (或 緯度, 經度) 也會自動對調。
    5. 自動跳過空白列、註解列 (#) 與沒有任何數值的標題列 (例如 "Point_ID, X, Y")。

    會拋出 ValueError 的情況：某一列只有一個數值、同一份資料混用二度分帶與經緯度、
    部分列的數值不在其他列的座標範圍內、相異頂點少於 3 個。
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("輸入的座標文字內容為空！")

    # (列號, [(數值, 原字串)], [文字標籤])
    rows: List[Tuple[int, List[Tuple[float, str]], List[str]]] = []
    for line_num, line in enumerate(raw_text.strip().splitlines(), start=1):
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("#") or clean_line.startswith("//"):
            continue

        numbers: List[Tuple[float, str]] = []
        text_labels: List[str] = []
        for token in _split_coordinate_line(clean_line):
            num_val = _clean_numeric_str(token)
            if num_val is not None:
                numbers.append((num_val, token.strip()))
            else:
                text_labels.append(token.strip())

        if not numbers:
            logger.debug("第 %d 列沒有數值，視為標頭略過: %s", line_num, clean_line)
            continue
        if len(numbers) < 2:
            raise ValueError(
                f"第 {line_num} 列只讀到一個數值，無法當成座標：{clean_line}\n"
                "請檢查該列是否有錯字；若是說明文字，請刪除或在開頭加上 #。"
            )
        rows.append((line_num, numbers, text_labels))

    # 有三個以上數值的列：判斷第一個數值是點號還是座標。
    # 以「哪一種解讀讓比較多列落在已知座標範圍」決定；分不出來時，第一欄全是不重複的整數才當點號。
    wide_rows = [numbers for _, numbers, _ in rows if len(numbers) >= 3]
    lead_is_point_id = False
    if wide_rows:
        hits_as_coord = sum(1 for n in wide_rows if _classify_coordinate_pair(n[0][0], n[1][0]))
        hits_as_id = sum(1 for n in wide_rows if _classify_coordinate_pair(n[1][0], n[2][0]))
        if hits_as_id != hits_as_coord:
            lead_is_point_id = hits_as_id > hits_as_coord
        else:
            leads = [n[0][1] for n in wide_rows]
            lead_is_point_id = all(_INTEGER_RE.match(t) for t in leads) and len(set(leads)) == len(leads)

    parsed_points: List[CoordinatePoint] = []
    kind_lines: Dict[Optional[str], List[int]] = {}
    for line_num, numbers, text_labels in rows:
        offset = 1 if (lead_is_point_id and len(numbers) >= 3) else 0
        val1, val2 = numbers[offset][0], numbers[offset + 1][0]
        if offset:
            point_id = numbers[0][1]
        else:
            point_id = text_labels[0] if text_labels else f"P{len(parsed_points) + 1}"

        classified = _classify_coordinate_pair(val1, val2)
        if classified:
            kind, coord_x, coord_y = classified
        else:
            kind, coord_x, coord_y = None, val1, val2
        kind_lines.setdefault(kind, []).append(line_num)
        parsed_points.append(CoordinatePoint(x=coord_x, y=coord_y, point_id=point_id))

    if COORD_KIND_TM2 in kind_lines and COORD_KIND_LONLAT in kind_lines:
        raise ValueError(
            f"同一份資料混用了二度分帶座標 (第 {_format_line_numbers(kind_lines[COORD_KIND_TM2])} 列) "
            f"與經緯度 (第 {_format_line_numbers(kind_lines[COORD_KIND_LONLAT])} 列)，請統一成同一種座標。"
        )
    if None in kind_lines and len(kind_lines) > 1:
        known = "二度分帶" if COORD_KIND_TM2 in kind_lines else "經緯度"
        raise ValueError(
            f"第 {_format_line_numbers(kind_lines[None])} 列的數值不在臺灣{known}座標的範圍內，"
            f"但其他列是{known}座標，請檢查是否有錯字或漏位數。"
        )

    if len(parsed_points) < 3:
        raise ValueError(
            f"有效多邊形頂點數不足！至少需要 3 個頂點座標，目前僅解析成功 {len(parsed_points)} 點。\n"
            "請檢查貼上的資料是否包含 X, Y 數值欄位。"
        )
    distinct_count = count_distinct_vertices(parsed_points)
    if distinct_count < 3:
        raise ValueError(
            f"相異頂點數不足！共讀到 {len(parsed_points)} 點，但扣除重複後只有 {distinct_count} 個相異頂點，"
            "至少需要 3 個才能圍成面。"
        )

    return parsed_points


def _format_line_numbers(line_numbers: Sequence[int], limit: int = 5) -> str:
    shown = "、".join(str(n) for n in line_numbers[:limit])
    return f"{shown} 等 {len(line_numbers)}" if len(line_numbers) > limit else shown


# ============================================================================
# [G-00] 閉鎖多邊形檔案產製 (GeoJSON 與 GPKG)
# ============================================================================

def create_boundary_from_points(
    points: Sequence[Union[CoordinatePoint, Tuple[float, float]]],
    *,
    output_gpkg_path: Optional[Union[str, Path]] = None,
    apply_polar_sort: bool = False,
    boundary_name: str = "master_boundary",
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> BoundaryBuildResult:
    """自座標點列生成標準閉鎖多邊形 GeoJSON 與統一基準 master_boundary.gpkg。

    運作流程：
    1. 驗證頂點數 (>= 3 個相異頂點)。
    2. 若啟用 apply_polar_sort，則以形心極角重新排序修復自相交。
    3. 自動補足末端首尾閉合 (Auto-Close)。
    4. 決定輸入座標的座標系：採用 assigned_srs (例如 "TWD67_121"、"WGS84")；未指派則依座標範圍推測
       (經緯度視為 EPSG:4326、臺灣 TM2 視為 EPSG:3826)，推測不出來就拋 SourceSrsUndeterminedError。
    5. 以 ogr2ogr 重投影到 target_srs，寫出 master_boundary.gpkg (圖層 boundary) 與預覽用的
       ./workspace_temp/custom_boundary.geojson。
    6. 面積與四至一律取自重投影後的成果 (平方公尺)，不是輸入座標的鞋帶面積。
    """
    if len(points) < 3:
        raise ValueError(f"構建邊界至少需要 3 個頂點，目前提供 {len(points)} 點！")

    pts: List[CoordinatePoint] = [
        p if isinstance(p, CoordinatePoint) else CoordinatePoint(x=float(p[0]), y=float(p[1]))
        for p in points
    ]
    distinct_count = count_distinct_vertices(pts)
    if distinct_count < 3:
        raise ValueError(f"構建邊界至少需要 3 個相異頂點，目前扣除重複後只有 {distinct_count} 個！")

    was_polar_sorted = False
    if apply_polar_sort:
        pts = sort_points_by_polar_angle(pts)
        was_polar_sorted = True

    # 執行首尾自動閉鎖；末點與首點只差在容許值內時直接對齊，確保環確實閉合
    closed_pts, was_auto_closed = auto_close_ring(pts)
    closed_pts[-1] = CoordinatePoint(x=closed_pts[0].x, y=closed_pts[0].y, point_id=closed_pts[-1].point_id)

    # 4. 決定輸入座標的座標系
    all_x = [p.x for p in closed_pts]
    all_y = [p.y for p in closed_pts]
    input_extent = Extent2D(min_x=min(all_x), min_y=min(all_y), max_x=max(all_x), max_y=max(all_y))
    # 以首點為原點算鞋帶和，避免大座標值的浮點誤差；面積相對於外接矩形趨近 0 即視為無法成面
    origin = closed_pts[0]
    twice_area = sum(
        (a.x - origin.x) * (b.y - origin.y) - (b.x - origin.x) * (a.y - origin.y)
        for a, b in zip(closed_pts, closed_pts[1:])
    )
    box_area = (input_extent.max_x - input_extent.min_x) * (input_extent.max_y - input_extent.min_y)
    if abs(twice_area) * 0.5 <= box_area * 1e-9:
        raise ValueError(
            "頂點圍出的面積為 0：頂點共線，或順序交錯成沙漏狀。請檢查座標，或改用「形心極角排序」。"
        )

    source_srs = resolve_assigned_srs(assigned_srs)
    if source_srs:
        srs_message = f"[座標系] 輸入座標依使用者指派視為 {str(assigned_srs).strip()}。"
    else:
        source_srs = guess_srs_from_extent(input_extent)
        if source_srs is None:
            raise SourceSrsUndeterminedError(
                f"輸入座標的範圍 X {input_extent.min_x:.3f}～{input_extent.max_x:.3f}、"
                f"Y {input_extent.min_y:.3f}～{input_extent.max_y:.3f} 不在經緯度或臺灣 TM2 的數值範圍內，"
                f"無法推測座標系。請指定座標系 (可用 {'、'.join(ASSIGNABLE_SOURCE_SRS)})。"
            )
        srs_message = (
            f"[注意] 輸入座標依數值範圍視為 {source_srs}，座標系為推測值；"
            "若實際為 TWD67、澎湖金門馬祖的 119 分帶或其他座標系，請改為手動指派。"
        )
        logger.warning(srs_message)
    if text_callback:
        text_callback(srs_message + "\n")

    temp_dir = get_workspace_temp_dir()
    if output_gpkg_path:
        final_gpkg = Path(output_gpkg_path).resolve()
    else:
        final_gpkg = temp_dir / "master_boundary.gpkg"

    # 座標不做四捨五入：經緯度取到小數 4 位會差到 11 公尺。
    # 不寫 crs 成員，來源座標系一律由 -s_srs 指派 (TWD67 七參數無法用 EPSG 代碼表示)。
    geojson_data = {
        "type": "FeatureCollection",
        "name": boundary_name,
        "features": [
            {
                "type": "Feature",
                "properties": {"NAME": boundary_name, "VERTEX_NUM": len(closed_pts)},
                "geometry": {"type": "Polygon", "coordinates": [[[p.x, p.y] for p in closed_pts]]},
            }
        ],
    }

    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    with geo_io.TempVectorFiles("point_boundary") as temp_files:
        input_geojson = temp_files.new("input", suffix=".geojson")
        with open(input_geojson, "w", encoding="utf-8") as f:
            json.dump(geojson_data, f, ensure_ascii=False, indent=2)

        staged_gpkg = temp_files.new("stage")
        stage_info = geo_io.stage_vector_to_gpkg(
            input_geojson,
            staged_gpkg,
            stage_layer="src",
            target_srs=target_srs,
            source_srs=source_srs,
            make_valid=True,
        )
        if stage_info.is_geographic:
            raise ToolkitError(f"目標座標系 {target_srs} 是經緯度，無法計算面積；請改用投影座標系 (例如 EPSG:3826)。")

        boundary_sql = (
            f'SELECT {geom}, "NAME", ROUND(ST_Area({geom}), 4) AS "AREA_M2", '
            f'ROUND(ST_Area({geom}) / 10000.0, 6) AS "AREA_HA", "VERTEX_NUM" FROM "src"'
        )
        geo_io.run_sql_to_gpkg(
            staged_gpkg,
            boundary_sql,
            final_gpkg,
            "boundary",
            extra_args=["-nlt", "MULTIPOLYGON", "-a_srs", target_srs],
        )

    boundary_rows = geo_io.read_attribute_rows(final_gpkg, "boundary", fields=["AREA_M2"])
    area_m2 = float(boundary_rows[0]["AREA_M2"] or 0.0) if boundary_rows else 0.0
    boundary_info = geo_io.read_layer_info(final_gpkg, "boundary")
    if area_m2 <= 0.0 or boundary_info.extent is None:
        geo_io.remove_vector_file(final_gpkg)
        raise ToolkitError("輸入的頂點無法圍成有面積的多邊形，請檢查座標。")
    area_ha = round(area_m2 / 10000.0, 6)
    min_x, min_y, max_x, max_y = boundary_info.extent
    extent = Extent2D(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)

    # 同步導出一份 GeoJSON 提供預覽
    geojson_file = temp_dir / "custom_boundary.geojson"
    geo_io.export_vector(final_gpkg, "boundary", geojson_file)

    return BoundaryBuildResult(
        geojson_path=geojson_file,
        gpkg_path=final_gpkg,
        vertex_count=len(closed_pts),
        was_auto_closed=was_auto_closed,
        was_polar_sorted=was_polar_sorted,
        area_m2=area_m2,
        area_ha=area_ha,
        extent=extent,
        target_srs=target_srs,
    )


# ============================================================================
# [G-05] DXF 假封閉線轉面與圖層淨化
# ============================================================================

def list_dxf_layers(dxf_path: Union[str, Path]) -> List[str]:
    """讀取 DXF 檔案內所有存在的圖層名稱 (Layer Names)。"""
    p = Path(dxf_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"找不到 DXF 檔案: {p}")

    # 透過 ogrinfo 執行 SQLite SQL 查詢 entities 圖層內的所有相異 Layer 欄位
    sql = "SELECT DISTINCT Layer FROM entities ORDER BY Layer"
    cmd = ["ogrinfo", "-ro", "-dialect", "sqlite", "-sql", sql, str(p)]
    result = run_command(cmd, check=False)

    layers: List[str] = []
    for line in result.stdout.splitlines():
        # OGR 輸出格式通常為 "  Layer (String) = 0"
        m = re.search(r"Layer\s*(?:\([^\)]+\))?\s*=\s*(.+)$", line.strip())
        if m:
            layer_val = m.group(1).strip()
            if layer_val and layer_val not in layers:
                layers.append(layer_val)

    # 若 SQL 方式未獲取，回退為解析標準圖層清單
    if not layers:
        info_json = run_json_command(["ogrinfo", "-json", "-so", str(p)])
        for lyr in info_json.get("layers", []):
            name = lyr.get("name")
            if name and name not in layers:
                layers.append(name)

    return layers or ["0", "entities"]


def _read_geojson_line_parts(geojson_path: Path) -> List[List[Tuple[float, float]]]:
    """讀出 GeoJSON 內所有折線的頂點 (只取 X、Y)；不是線的幾何略過。"""
    with open(geojson_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    parts: List[List[Tuple[float, float]]] = []

    def _collect(geometry: Optional[Dict[str, Any]]) -> None:
        if not geometry:
            return
        kind = geometry.get("type")
        if kind == "LineString":
            lines = [geometry.get("coordinates") or []]
        elif kind == "MultiLineString":
            lines = geometry.get("coordinates") or []
        elif kind == "GeometryCollection":
            for member in geometry.get("geometries") or []:
                _collect(member)
            return
        else:
            return
        for coords in lines:
            part = [(float(c[0]), float(c[1])) for c in coords]
            if len(part) >= 2:
                parts.append(part)

    for feature in data.get("features", []):
        _collect(feature.get("geometry"))
    return parts


def _chain_parts_into_ring(
    parts: Sequence[Sequence[Tuple[float, float]]],
) -> Tuple[List[Tuple[float, float]], float, float]:
    """把多段折線依端點距離首尾相接成一個封閉環。

    從最長的一段開始，每次接上端點離目前末點最近的那一段 (必要時反向)，最後把末點接回首點。
    回傳 (環的頂點, 線段之間最大的缺口, 末點接回首點的距離)。
    """
    def _length(part: Sequence[Tuple[float, float]]) -> float:
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(part, part[1:]))

    remaining = [list(part) for part in parts]
    remaining.sort(key=_length, reverse=True)
    ring = remaining.pop(0)
    joint_gap = 0.0
    while remaining:
        tail = ring[-1]
        best_index, best_reversed, best_dist = 0, False, math.inf
        for index, part in enumerate(remaining):
            for is_reversed, endpoint in ((False, part[0]), (True, part[-1])):
                dist = math.hypot(endpoint[0] - tail[0], endpoint[1] - tail[1])
                if dist < best_dist:
                    best_index, best_reversed, best_dist = index, is_reversed, dist
        part = remaining.pop(best_index)
        if best_reversed:
            part.reverse()
        joint_gap = max(joint_gap, best_dist)
        ring.extend(part[1:] if best_dist <= 1e-9 else part)

    closing_gap = math.hypot(ring[-1][0] - ring[0][0], ring[-1][1] - ring[0][1])
    if closing_gap > 0.0:
        ring.append(ring[0])
    return ring, joint_gap, closing_gap


# DXF 自動閉鎖時，線段端點缺口在這個距離 (公尺) 內才串接
DEFAULT_SNAP_TOLERANCE_M: float = 0.5


def dxf_lines_to_polygon_boundary(
    dxf_path: Union[str, Path],
    output_gpkg: Optional[Union[str, Path]] = None,
    *,
    cad_layer: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    snap_tolerance: float = DEFAULT_SNAP_TOLERANCE_M,
    text_callback: Optional[Callable[[str], None]] = None,
) -> BoundaryBuildResult:
    """將 DXF 中工程師繪製的 LINESTRING / MULTILINESTRING 假封閉線段轉換為實體面 (POLYGON)。

    防呆核心規範：
    1. 支援依 CAD 圖層名稱 (Layer) 篩選邊界線。
    2. 以 ST_BuildArea 合併線段；圍不成面時依序改用「先在交點打斷」與「自動首尾閉鎖」。
       自動閉鎖：線段之間的缺口不超過 snap_tolerance (公尺) 才串接，末點一律接回首點，缺口大小會回報。
    3. 自動加上 -makevalid 與 -dim XY 強制轉為二維平面多邊形。
    4. 輸出為統一規格之 master_boundary.gpkg。
    5. DXF 沒有座標系標籤：採用 assigned_srs (例如 "TWD67_121")，未指派則依座標範圍推測，再重投影到 target_srs。
    """
    in_dxf = Path(dxf_path).resolve()
    if not in_dxf.exists():
        raise FileNotFoundError(f"找不到 DXF 檔案: {in_dxf}")
    if not (math.isfinite(snap_tolerance) and snap_tolerance >= 0):
        raise ValueError(f"自動閉鎖容許距離必須是 0 以上的數值 (公尺): {snap_tolerance}")

    temp_dir = get_workspace_temp_dir()
    out_gpkg = Path(output_gpkg).resolve() if output_gpkg else (temp_dir / "master_boundary.gpkg")
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)

    layer_label = cad_layer or "ALL"
    # DXF 的幾何欄位沒有名字，先正規化成暫存 GPKG (幾何欄位固定為 geom) 再下 SQL
    stage_args = ["-dim", "XY"]
    if cad_layer:
        stage_args.extend(["-where", f"Layer = {geo_io.quote_literal(cad_layer)}"])

    srs_decision = decide_and_report_source_srs(in_dxf, assigned_srs, text_callback=text_callback)

    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    layer_literal = geo_io.quote_literal(layer_label)
    # ST_BuildArea 對未打斷或未閉合的線回傳 NULL 但指令本身成功，所以每一種做法都要量面積才知道有沒有成面：
    # 1. 線段端點已相接；2. 線段互相交叉但沒有在交點打斷 (先以 ST_Union 打斷)；3. 端點沒有相接 (自動閉鎖)
    build_expressions = [
        ("", f"ST_BuildArea(ST_Collect({geom}))"),
        ("線段沒有在交點打斷，已自動打斷後構面。", f"ST_BuildArea(ST_Union({geom}))"),
    ]
    area_m2 = 0.0
    was_auto_closed = False
    notes: List[str] = []

    with geo_io.TempVectorFiles("dxf_boundary") as temp_files:
        staged_gpkg = temp_files.new("lines")
        geo_io.stage_vector_to_gpkg(
            in_dxf,
            staged_gpkg,
            stage_layer="lines",
            target_srs=target_srs,
            source_srs=srs_decision.s_srs,
            extra_args=stage_args,
        )

        def _measure_area() -> float:
            # 面積由 SQL 寫成欄位後讀回，不解析 ogrinfo 的文字輸出
            area_gpkg = temp_files.new("area")
            area_sql = f'SELECT ROUND(ST_Area({geom}), 2) AS "AREA_M2" FROM "boundary"'
            geo_io.run_sql_to_gpkg(out_gpkg, area_sql, area_gpkg, "area")
            rows = geo_io.read_attribute_rows(area_gpkg, "area", fields=["AREA_M2"])
            return float(rows[0]["AREA_M2"] or 0.0) if rows else 0.0

        for note, expression in build_expressions:
            build_sql = (
                f'SELECT {expression} AS {geom}, {layer_literal} AS "LAYER_NAME" '
                f'FROM "lines" WHERE ST_Dimension({geom}) = 1'
            )
            geo_io.run_sql_to_gpkg(
                staged_gpkg,
                build_sql,
                out_gpkg,
                "boundary",
                extra_args=["-nlt", "MULTIPOLYGON", "-makevalid", "-a_srs", target_srs],
            )
            area_m2 = _measure_area()
            if area_m2 > 0.0:
                if note:
                    notes.append(note)
                break

        if area_m2 <= 0.0:
            # 自動首尾閉鎖：把線段依端點距離串成一條，再把末點接回首點
            merged_gpkg = temp_files.new("merged")
            merge_sql = f'SELECT ST_LineMerge(ST_Union({geom})) AS {geom} FROM "lines" WHERE ST_Dimension({geom}) = 1'
            geo_io.run_sql_to_gpkg(staged_gpkg, merge_sql, merged_gpkg, "merged")
            merged_geojson = temp_files.new("merged", suffix=".geojson")
            geo_io.export_vector(merged_gpkg, "merged", merged_geojson)
            parts = _read_geojson_line_parts(merged_geojson)
            if parts:
                ring, joint_gap, closing_gap = _chain_parts_into_ring(parts)
                if joint_gap > snap_tolerance:
                    geo_io.remove_vector_file(out_gpkg)
                    raise ToolkitError(
                        f"DXF 圖層「{layer_label}」有 {len(parts)} 段互不相接的線，最大缺口 {joint_gap:.3f} m，"
                        f"超過容許值 {snap_tolerance:g} m，無法自動閉鎖。請在 CAD 把線段端點接好，"
                        f"或把閉鎖容許調大到缺口以上 (確認缺口本來就該接起來): {in_dxf.name}"
                    )
                ring_geojson = temp_files.new("ring", suffix=".geojson")
                ring_data = {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"LAYER_NAME": layer_label},
                            "geometry": {"type": "Polygon", "coordinates": [[list(pt) for pt in ring]]},
                        }
                    ],
                }
                with open(ring_geojson, "w", encoding="utf-8") as f:
                    json.dump(ring_data, f, ensure_ascii=False)
                # 沒有 crs 成員的 GeoJSON 會被當成 EPSG:4326，座標其實已是 target_srs，以 -a_srs 指派
                geo_io.stage_vector_to_gpkg(
                    ring_geojson,
                    out_gpkg,
                    stage_layer="boundary",
                    make_valid=True,
                    extra_args=["-nlt", "MULTIPOLYGON", "-a_srs", target_srs],
                )
                area_m2 = _measure_area()
                was_auto_closed = True
                notes.append(
                    f"線段沒有圍成封閉範圍，已自動首尾閉鎖 (線段之間最大缺口 {joint_gap:.3f} m，"
                    f"末點接回首點的距離 {closing_gap:.3f} m)，請確認範圍是否正確。"
                )

    boundary_info = geo_io.read_layer_info(out_gpkg, "boundary")
    if area_m2 <= 0.0 or boundary_info.extent is None:
        geo_io.remove_vector_file(out_gpkg)
        raise ToolkitError(
            f"DXF 圖層「{layer_label}」的線段無法圍成封閉範圍 (找不到線段，或線段圍不出面積)，請檢查 CAD 圖面: {in_dxf.name}"
        )
    for note in notes:
        logger.warning("%s：%s", in_dxf.name, note)
        if text_callback:
            text_callback(f"[注意] DXF 圖層「{layer_label}」{note}\n")
    area_ha = round(area_m2 / 10000.0, 6)
    min_x, min_y, max_x, max_y = boundary_info.extent
    extent = Extent2D(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)

    # 同步導出一份 GeoJSON 提供預覽
    geojson_out = temp_dir / "custom_boundary.geojson"
    geo_io.export_vector(out_gpkg, "boundary", geojson_out)

    return BoundaryBuildResult(
        geojson_path=geojson_out,
        gpkg_path=out_gpkg,
        vertex_count=0,
        was_auto_closed=was_auto_closed,
        was_polar_sorted=False,
        area_m2=area_m2,
        area_ha=area_ha,
        extent=extent,
        target_srs=target_srs,
    )


# ============================================================================
# 既有向量檔 (SHP / GPKG / GeoJSON) 匯入為主邊界
# ============================================================================

def import_boundary_file(
    boundary_path: Union[str, Path],
    output_gpkg: Optional[Union[str, Path]] = None,
    *,
    layer_name: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> BoundaryBuildResult:
    """檢查使用者選取的向量檔能不能當裁切邊界，並正規化成 master_boundary.gpkg (圖層 boundary)。

    檢查項目：
    1. 圖徵數：沒有圖徵就拋錯；有多個面時全部保留並提示個數。
    2. 幾何型別：留下面 (Polygon / MultiPolygon) 與首尾相接的線 (轉成面，例如 Google Earth 用「路徑」圍的範圍)；
       一個都沒有就拋錯，點或沒接起來的線被略過時提示。
    3. 座標系：來源有座標系就用來源的；沒有時採用 assigned_srs，未指派則依座標範圍推測，
       推測不出來拋 SourceSrsUndeterminedError。一律重投影到 target_srs。
    4. 圖層：layer_name 沒指定、檔案裡又有好幾個有圖徵的圖層 (KML／KMZ 的資料夾、GPKG) 時，
       依序找第一個有面 (或封閉的線) 的圖層，用了哪一個寫在結果的 layer_name。
    來源檔不會被改動。DXF 請改用 dxf_lines_to_polygon_boundary。
    """
    in_p = Path(boundary_path).resolve()
    if not in_p.exists():
        raise FileNotFoundError(f"找不到邊界檔案: {in_p}")
    if in_p.suffix.lower() == ".dxf":
        raise ToolkitError(f"DXF 的線段要先轉成面才能當邊界，請改用「DXF 線轉面」: {in_p.name}")

    temp_dir = get_workspace_temp_dir()
    out_gpkg = Path(output_gpkg).resolve() if output_gpkg else (temp_dir / "master_boundary.gpkg")

    def _note(message: str) -> None:
        logger.warning("%s：%s", in_p.name, message)
        if text_callback:
            text_callback(f"[注意] {in_p.name}：{message}\n")

    candidates: List[Optional[str]] = [layer_name]
    if layer_name is None:
        data_layers = geo_io.list_data_layers(in_p)
        if len(data_layers) > 1:
            candidates = list(data_layers)

    with geo_io.TempVectorFiles("import_boundary") as temp_files:
        found: Optional[Tuple[Optional[str], geo_io.VectorLayerInfo, Path, List[Dict[str, Any]]]] = None
        failures: List[str] = []
        for candidate in candidates:
            source_info = geo_io.read_layer_info(in_p, candidate)
            if source_info.feature_count <= 0:
                failures.append(f"圖層「{source_info.layer_name}」沒有任何圖徵")
                continue
            polygons_gpkg, area_rows = _boundary_polygons(
                in_p, candidate, source_info, target_srs, assigned_srs, temp_files, text_callback
            )
            if area_rows:
                found = (candidate, source_info, polygons_gpkg, area_rows)
                break
            failures.append(f"圖層「{source_info.layer_name}」沒有面 (幾何型別: {source_info.geometry_type})")
        if found is None:
            hint = "；Google Earth 請用「多邊形」畫範圍，或把路徑首尾接起來" if in_p.suffix.lower() in (".kml", ".kmz") else ""
            raise ToolkitError(f"{'、'.join(failures)}，裁切邊界必須是面 (或首尾相接的線){hint}: {in_p.name}")
        used_layer, source_info, polygons_gpkg, area_rows = found
        if len(candidates) > 1:
            _note(f"檔案內有 {len(candidates)} 個圖層，採用第一個有面的圖層「{source_info.layer_name}」當邊界。")
        geo_io.export_vector(polygons_gpkg, "boundary", out_gpkg, output_layer_name="boundary")

    skipped = source_info.feature_count - len(area_rows)
    if skipped > 0:
        _note(f"{skipped} 個圖徵不是面或面積為 0，已略過。")
    if len(area_rows) > 1:
        _note(f"邊界共有 {len(area_rows)} 個面，裁切時全部視為邊界範圍，面積為合計值。")

    area_m2 = round(sum(float(row["AREA_M2"] or 0.0) for row in area_rows), 4)
    min_x, min_y, max_x, max_y = geo_io.read_layer_info(out_gpkg, "boundary").extent

    # 同步導出一份 GeoJSON 提供預覽
    geojson_out = temp_dir / "custom_boundary.geojson"
    geo_io.export_vector(out_gpkg, "boundary", geojson_out)

    return BoundaryBuildResult(
        geojson_path=geojson_out,
        gpkg_path=out_gpkg,
        vertex_count=0,
        was_auto_closed=False,
        was_polar_sorted=False,
        area_m2=area_m2,
        area_ha=round(area_m2 / 10000.0, 6),
        extent=Extent2D(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y),
        target_srs=target_srs,
        layer_name=source_info.layer_name if len(candidates) > 1 or layer_name else None,
    )


def _boundary_polygons(
    in_p: Path,
    layer_name: Optional[str],
    source_info: geo_io.VectorLayerInfo,
    target_srs: str,
    assigned_srs: Optional[str],
    temp_files: geo_io.TempVectorFiles,
    text_callback: Optional[Callable[[str], None]],
) -> Tuple[Path, List[Dict[str, Any]]]:
    """一個圖層裡的面與首尾相接的線 (轉成面)，轉到 target_srs 寫進暫存 GPKG (圖層 boundary、欄位 AREA_M2)；
    回傳 (暫存 GPKG, 每個面的 AREA_M2)，沒有面時清單是空的。"""
    srs_decision = decide_and_report_source_srs(in_p, assigned_srs, layer_name=layer_name, text_callback=text_callback)
    staged_gpkg = temp_files.new("stage")
    stage_info = geo_io.stage_vector_to_gpkg(
        in_p,
        staged_gpkg,
        source_layer=source_info.layer_name,
        stage_layer="src",
        target_srs=target_srs,
        source_srs=srs_decision.s_srs,
        make_valid=True,
        extra_args=["-dim", "XY"],
        text_callback=text_callback,
    )
    if stage_info.is_geographic:
        raise ToolkitError(f"目標座標系 {target_srs} 是經緯度，無法計算面積；請改用投影座標系 (例如 EPSG:3826)。")

    # 先寫到暫存檔，確認有面之後才取代輸出 (輸出路徑可能已有上一次的主邊界)。
    # ST_IsClosed 只認單一條線 (其他型別回傳 -1)，所以寫成「= 1」；封閉的線用 ST_BuildArea 轉面 (同另存 SHP 的封閉線轉面)
    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    is_line = f"ST_GeometryType({geom}) LIKE '%LINESTRING%'"
    shape = f"CASE WHEN {is_line} THEN ST_BuildArea({geom}) ELSE {geom} END"
    polygon_sql = (
        f'SELECT shape AS {geom}, ROUND(ST_Area(shape), 4) AS "AREA_M2" FROM ('
        f'SELECT {shape} AS shape FROM "src" '
        f"WHERE ST_Dimension({geom}) = 2 OR ({is_line} AND COALESCE(ST_IsClosed({geom}), 0) = 1)"
        f") WHERE ST_Area(shape) > 0"
    )
    polygons_gpkg = temp_files.new("polygons")
    geo_io.run_sql_to_gpkg(
        staged_gpkg,
        polygon_sql,
        polygons_gpkg,
        "boundary",
        extra_args=["-nlt", "MULTIPOLYGON", "-a_srs", target_srs],
    )
    if not polygons_gpkg.exists():
        return polygons_gpkg, []
    return polygons_gpkg, geo_io.read_attribute_rows(polygons_gpkg, "boundary", fields=["AREA_M2"])


# ============================================================================
# [G-08] 邊界與路線環域外推 (ST_Buffer)
# ============================================================================

def generate_buffer(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    buffer_distance: float,
    *,
    input_layer: Optional[str] = None,
    target_srs: str = f"EPSG:{EPSG_TWD97_121}",
    assigned_srs: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """對輸入之多邊形邊界或路線幾何進行外推或內縮 (ST_Buffer)。

    應用情境（規格書 3.1 節）：
    - 工區邊界外推 +20m 作為裁切餘裕。
    - 道路中心線外推 15m 建立路權範圍框。
    - 使用 SQLite 方言之 ST_Buffer 演算法與 -makevalid 確保幾何有效性。
    - 來源有座標系就用來源的；沒有時才採用 assigned_srs，未指派則依座標範圍推測。
    """
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if not in_p.exists():
        raise FileNotFoundError(f"輸入圖資不存在: {in_p}")

    srs_decision = decide_and_report_source_srs(
        in_p, assigned_srs, layer_name=input_layer, text_callback=text_callback
    )

    geom = geo_io.quote_ident(geo_io.STAGE_GEOMETRY_COLUMN)
    with geo_io.TempVectorFiles("buffer") as temp_files:
        staged_gpkg = temp_files.new("src")
        stage_info = geo_io.stage_vector_to_gpkg(
            in_p,
            staged_gpkg,
            source_layer=input_layer,
            stage_layer="src",
            target_srs=target_srs,
            source_srs=srs_decision.s_srs,
            make_valid=True,
            progress_callback=geo_io.scaled_progress(progress_callback, 0.0, 30.0),
            text_callback=text_callback,
        )

        select_items = [f"ST_Buffer({geom}, {float(buffer_distance)!r}) AS {geom}"]
        select_items.extend(geo_io.build_field_select_list(stage_info))
        buffered_gpkg = temp_files.new("buffered")
        geo_io.run_sql_to_gpkg(
            staged_gpkg,
            f'SELECT {", ".join(select_items)} FROM "src"',
            buffered_gpkg,
            "buffered",
            extra_args=["-nlt", "PROMOTE_TO_MULTI", "-makevalid"],
            progress_callback=geo_io.scaled_progress(progress_callback, 30.0, 70.0),
            text_callback=text_callback,
        )
        geo_io.export_vector(
            buffered_gpkg,
            "buffered",
            out_p,
            progress_callback=geo_io.scaled_progress(progress_callback, 70.0, 100.0),
            text_callback=text_callback,
        )
    return out_p
