"""工程專案工作流配方記憶與管理模組 (core/recipe_manager.py)

嚴格遵守專案規格書第 0 節與第 3.5 節 [A-08] 規範：
1. 將 UI 勾選之所有參數（來源 CRS、目標 CRS、Buffer 距離、DEM 目標解析度、重取樣演算法、
   是否產出影像金字塔、是否輸出 CAD 對位底圖、是否產生 KMZ、等高線間距）序列化儲存於 ./config/recipes.json。
2. 支援一鍵記憶新配方、更新現有配方、重設回預設配方，並回傳名稱清單供 UI 下拉選單使用。
3. recipes.json 讀不懂時先把原檔內容另存備份再退回預設配方；存檔一律先寫暫存檔再原子取代。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from core.crs_doctor import EPSG_TWD97_121, resolve_assigned_srs
from core.env_runner import ToolkitError, get_config_dir

logger = logging.getLogger("core.recipe_manager")

# 舊版配方檔用過、但沒有任何程式認得的來源座標系代號，載入時換成 crs_doctor.ASSIGNABLE_SOURCE_SRS 的鍵
LEGACY_SOURCE_CRS_ALIASES: Dict[str, str] = {
    "TWD67_7PARAM": "TWD67_121",
}


def _normalize_source_crs(value: Any) -> str:
    """把配方的來源座標系整理成 resolve_assigned_srs 認得的寫法。"""
    text = str(value).strip()
    return LEGACY_SOURCE_CRS_ALIASES.get(text.upper(), text)


@dataclass
class WorkflowRecipe:
    """工作流配方資料結構。"""

    name: str
    description: str = ""
    source_crs: str = f"EPSG:{EPSG_TWD97_121}"
    target_crs: str = f"EPSG:{EPSG_TWD97_121}"
    buffer_distance: float = 0.0
    dem_resolution: float = 5.0
    dem_resampling: str = "bilinear"
    generate_pyramids: bool = True
    export_cad_bg: bool = True
    export_kmz: bool = True
    contour_interval: float = 5.0
    extra_options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化為字典型態。"""
        return asdict(self)

    def resolve_source_srs(self) -> Optional[str]:
        """回傳可當作引擎 assigned_srs 的來源座標系 (EPSG 代碼或 PROJ 字串)。

        只在來源檔沒有座標系標籤時才會被引擎採用；source_crs 留空回傳 None (交給引擎依範圍推測)，
        認不得的寫法拋 ToolkitError。
        """
        return resolve_assigned_srs(_normalize_source_crs(self.source_crs))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkflowRecipe:
        """自字典反序列化為實例物件。"""
        return cls(
            name=str(data.get("name", "未命名配方")),
            description=str(data.get("description", "")),
            source_crs=_normalize_source_crs(data.get("source_crs", f"EPSG:{EPSG_TWD97_121}")),
            target_crs=str(data.get("target_crs", f"EPSG:{EPSG_TWD97_121}")),
            buffer_distance=float(data.get("buffer_distance", 0.0)),
            dem_resolution=float(data.get("dem_resolution", 5.0)),
            dem_resampling=str(data.get("dem_resampling", "bilinear")),
            generate_pyramids=bool(data.get("generate_pyramids", True)),
            export_cad_bg=bool(data.get("export_cad_bg", True)),
            export_kmz=bool(data.get("export_kmz", True)),
            contour_interval=float(data.get("contour_interval", 5.0)),
            extra_options=dict(data.get("extra_options", {})),
        )


def get_recipes_file_path() -> Path:
    """取得 config/recipes.json 檔案路徑，若目錄不存在則自動建立。"""
    return get_config_dir() / "recipes.json"


def _default_recipes() -> Dict[str, WorkflowRecipe]:
    """沒有可用配方時使用的預設配方。"""
    return {
        "default": WorkflowRecipe(
            name="標準工程邊界裁切 (TWD97 5m DEM)",
            description="適用一般土木水利工程邊界裁切，預設輸出 TWD97 (EPSG:3826)，DEM 降為 5m",
        )
    }


def _backup_damaged_recipes_file(recipe_file: Path, raw_bytes: bytes) -> Path:
    """把讀不懂的 recipes.json 原樣另存到同目錄，回傳備份檔路徑。

    檔名帶內容雜湊，同一份壞檔重複載入只會留一份備份。備份寫不出去時拋 ToolkitError，
    不讓呼叫端繼續往下存檔而蓋掉使用者的配方。
    """
    digest = hashlib.sha1(raw_bytes).hexdigest()[:8]
    backup_file = recipe_file.with_name(f"{recipe_file.stem}.damaged-{digest}{recipe_file.suffix}")
    if backup_file.exists() and backup_file.read_bytes() == raw_bytes:
        return backup_file
    try:
        backup_file.write_bytes(raw_bytes)
    except OSError as err:
        raise ToolkitError(f"{recipe_file.name} 內容無法解讀，且無法備份到 {backup_file}: {err}") from err
    return backup_file


def load_all_recipes() -> Dict[str, WorkflowRecipe]:
    """載入所有預設與自訂工作流配方。

    檔案整份讀不懂 (不是 UTF-8、JSON 語法錯、最外層不是物件) 時回傳預設配方；只有個別配方讀不懂時略過該筆。
    兩種情況都會先把原檔內容另存成 recipes.damaged-<雜湊>.json，之後存檔才不會讓使用者的配方消失。
    """
    recipe_file = get_recipes_file_path()
    if not recipe_file.exists():
        # 若檔案遺失，寫入預設配方
        recipes_dict = _default_recipes()
        save_all_recipes(recipes_dict)
        return recipes_dict

    try:
        raw_bytes = recipe_file.read_bytes()
    except OSError as err:
        raise ToolkitError(f"無法讀取配方檔 {recipe_file}: {err}") from err

    try:
        raw_data = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(raw_data, dict):
            raise ValueError("最外層不是 JSON 物件")
    except ValueError as err:
        backup_file = _backup_damaged_recipes_file(recipe_file, raw_bytes)
        logger.warning("%s 內容無法解讀 (%s)，原檔已另存為 %s，暫時改用預設配方。", recipe_file.name, err, backup_file.name)
        return _default_recipes()

    recipes: Dict[str, WorkflowRecipe] = {}
    damaged_ids: List[str] = []
    for k, v in raw_data.items():
        try:
            if not isinstance(v, dict):
                raise ValueError("配方內容不是 JSON 物件")
            recipes[k] = WorkflowRecipe.from_dict(v)
        except (TypeError, ValueError) as err:
            damaged_ids.append(k)
            logger.warning("配方 %s 內容無法解讀，已略過: %s", k, err)

    if damaged_ids:
        backup_file = _backup_damaged_recipes_file(recipe_file, raw_bytes)
        logger.warning("%s 有 %d 筆配方無法解讀 (%s)，原檔已另存為 %s。", recipe_file.name, len(damaged_ids), "、".join(damaged_ids), backup_file.name)

    if not recipes:
        return _default_recipes()
    return recipes


def save_all_recipes(recipes: Dict[str, WorkflowRecipe]) -> None:
    """將所有配方字典寫入 config/recipes.json。

    先完整寫到同目錄的暫存檔，再以 os.replace 取代正式檔；中途失敗時正式檔維持原樣。
    """
    recipe_file = get_recipes_file_path()
    out_dict: Dict[str, Any] = {}
    for k, v in recipes.items():
        out_dict[k] = v.to_dict()

    temp_file = recipe_file.with_name(f"{recipe_file.name}.tmp-{os.getpid()}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(out_dict, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, recipe_file)
    except BaseException:
        try:
            temp_file.unlink()
        except OSError:
            pass
        raise


def get_recipe_by_id(recipe_id: str) -> Optional[WorkflowRecipe]:
    """依據配方識別碼取得配方物件。"""
    all_recs = load_all_recipes()
    return all_recs.get(recipe_id)


def add_or_update_recipe(recipe_id: str, recipe: WorkflowRecipe) -> None:
    """新增或更新指定配方並即時寫入磁碟。"""
    all_recs = load_all_recipes()
    all_recs[recipe_id] = recipe
    save_all_recipes(all_recs)
    logger.info("已成功儲存工作流配方: %s (%s)", recipe_id, recipe.name)


def list_recipe_display_options() -> List[Tuple[str, str]]:
    """回傳 UI 下拉選單專用之配方選項清單串列，元素為配方識別碼與顯示名稱元組。"""
    all_recs = load_all_recipes()
    return [(rid, r.name) for rid, r in all_recs.items()]
