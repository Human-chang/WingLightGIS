"""來源座標系下拉選單的選項 (ui/srs_options.py)

各分頁共用：選單文字對應到引擎函式的 assigned_srs（crs_doctor.ASSIGNABLE_SOURCE_SRS 的鍵）。
指派值只在檔案沒有座標系標籤時才會被引擎採用；有標籤的檔案一律以自己的標籤為準。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Union

from core.crs_doctor import ASSIGNABLE_SOURCE_SRS

AUTO_SOURCE_SRS_LABEL: str = "自動判斷 (無標籤時依座標範圍推測)"

# (選單文字, assigned_srs)；第一項為預設
SOURCE_SRS_CHOICES: Tuple[Tuple[str, Optional[str]], ...] = (
    (AUTO_SOURCE_SRS_LABEL, None),
    ("TWD97 121 分帶 (EPSG:3826，臺灣本島)", "TWD97_121"),
    ("TWD97 119 分帶 (EPSG:3825，澎湖金門馬祖)", "TWD97_119"),
    ("TWD67 121 分帶 (七參數轉換，臺灣本島)", "TWD67_121"),
    ("TWD67 119 分帶 (七參數轉換，澎湖金門馬祖)", "TWD67_119"),
    ("WGS84 經緯度 (EPSG:4326)", "WGS84"),
)

SOURCE_SRS_HINT: str = (
    "只在檔案沒有座標系標籤時採用。澎湖、金門、馬祖的 TM2 資料請手動選 119 分帶；"
    "舊圖是 TWD67 時請選 TWD67，自動判斷一律當成 TWD97。"
)

# 輸出座標系 (選單文字, ASSIGNABLE_SOURCE_SRS 的鍵)；第一項為預設。只開放投影座標 (經緯度輸出時解析度與面積要另外換算)，
# TWD67 用專案的七參數定義，不會有約 830 m 的偏移
OUTPUT_SRS_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("TWD97 121 分帶 (EPSG:3826，臺灣本島)", "TWD97_121"),
    ("TWD97 119 分帶 (EPSG:3825，澎湖金門馬祖)", "TWD97_119"),
    ("TWD67 121 分帶 (七參數轉換)", "TWD67_121"),
    ("TWD67 119 分帶 (七參數轉換)", "TWD67_119"),
)


def output_srs_labels() -> List[str]:
    """輸出座標系下拉選單的文字清單。"""
    return [label for label, _ in OUTPUT_SRS_CHOICES]


def target_srs_from_label(label: str) -> str:
    """把輸出座標系選單文字換成 gdalwarp／ogr2ogr 的 -t_srs；認不得的文字回傳預設 (TWD97 121)。"""
    for choice_label, key in OUTPUT_SRS_CHOICES:
        if choice_label == label:
            return ASSIGNABLE_SOURCE_SRS[key]
    return ASSIGNABLE_SOURCE_SRS[OUTPUT_SRS_CHOICES[0][1]]


# 圖資快覽每一列的來源座標系 (選單文字, ASSIGNABLE_SOURCE_SRS 的鍵)；第一項是預設。
# 跟上面裁切工具的指派不同：選了就以它為準，連有標籤的檔也照它 (給標錯座標的檔用)
BY_LABEL_SOURCE_SRS_LABEL: str = "依原始標籤 (沒有標籤時依座標範圍推測)"
VIEW_SOURCE_SRS_CHOICES: Tuple[Tuple[str, Optional[str]], ...] = (
    (BY_LABEL_SOURCE_SRS_LABEL, None),
    *((label, assigned) for label, assigned in SOURCE_SRS_CHOICES if assigned),
)

# 清單裡寫的座標系短名
_SHORT_NAMES_BY_KEY: Dict[str, str] = {
    "TWD97_121": "TWD97 121", "TWD97_119": "TWD97 119", "TWD67_121": "TWD67 121", "TWD67_119": "TWD67 119",
    "WGS84": "WGS84 經緯度",
}
_SHORT_NAMES_BY_EPSG: Dict[int, str] = {
    3826: "TWD97 121", 3825: "TWD97 119", 3828: "TWD67 121", 3827: "TWD67 119",
    4326: "WGS84 經緯度", 3824: "TWD97 經緯度", 3821: "TWD67 經緯度",
}


def short_srs_name(value: Union[int, str, None]) -> str:
    """座標系的短名 (例如「TWD97 121」)：value 可以是 EPSG 代碼、"EPSG:3826"、ASSIGNABLE_SOURCE_SRS 的鍵或值；
    認不得的照原樣回傳，None 回傳空字串。"""
    if value is None:
        return ""
    if isinstance(value, int):
        return _SHORT_NAMES_BY_EPSG.get(value, f"EPSG:{value}")
    text = str(value).strip()
    upper = text.upper()
    for key, srs in ASSIGNABLE_SOURCE_SRS.items():
        if upper in (key.upper(), srs.upper()):
            return _SHORT_NAMES_BY_KEY[key]
    if upper.startswith("EPSG:") and upper[5:].isdigit():
        return short_srs_name(int(upper[5:]))
    return text


def view_source_srs_labels() -> List[str]:
    """圖資快覽來源座標系選單的文字清單。"""
    return [label for label, _ in VIEW_SOURCE_SRS_CHOICES]


def view_source_srs_from_label(label: str) -> Optional[str]:
    """圖資快覽來源座標系選單文字 → ASSIGNABLE_SOURCE_SRS 的鍵；依原始標籤或認不得的文字回傳 None。"""
    return next((key for choice, key in VIEW_SOURCE_SRS_CHOICES if choice == label), None)


def view_source_srs_label(key: Optional[str]) -> str:
    """ASSIGNABLE_SOURCE_SRS 的鍵 → 圖資快覽來源座標系選單文字；None 或認不得的是「依原始標籤」。"""
    return next((choice for choice, value in VIEW_SOURCE_SRS_CHOICES if value == key), BY_LABEL_SOURCE_SRS_LABEL)


POUR_POINT_SAME_AS_DEM_LABEL: str = "與 DEM 相同"


def source_srs_labels() -> List[str]:
    """來源座標系下拉選單的文字清單。"""
    return [label for label, _ in SOURCE_SRS_CHOICES]


def assigned_srs_from_label(label: str) -> Optional[str]:
    """把選單文字換成引擎的 assigned_srs；自動判斷或認不得的文字回傳 None。"""
    for choice_label, assigned in SOURCE_SRS_CHOICES:
        if choice_label == label:
            return assigned
    return None


def label_for_assigned_srs(value: Optional[str]) -> Optional[str]:
    """把 assigned_srs 的鍵或它對應的座標系字串 (例如 "EPSG:3825") 換回選單文字；選單裡沒有時回傳 None。"""
    if value is None or not str(value).strip():
        return AUTO_SOURCE_SRS_LABEL
    wanted = str(value).strip().upper()
    for label, assigned in SOURCE_SRS_CHOICES:
        if assigned and wanted in (assigned.upper(), ASSIGNABLE_SOURCE_SRS[assigned].upper()):
            return label
    return None


def pour_point_srs_labels() -> List[str]:
    """出水口座標系下拉選單的文字清單：預設與 DEM 相同，其餘同來源座標系的指派選項。"""
    return [POUR_POINT_SAME_AS_DEM_LABEL] + [label for label, assigned in SOURCE_SRS_CHOICES if assigned]


def pour_point_srs_from_label(label: str) -> Optional[str]:
    """把出水口座標系選單文字換成引擎的 pour_point_srs；與 DEM 相同時回傳 None。"""
    if label == POUR_POINT_SAME_AS_DEM_LABEL:
        return None
    return assigned_srs_from_label(label)
