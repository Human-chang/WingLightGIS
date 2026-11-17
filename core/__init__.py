"""GIS_Master_Toolkit 核心後端模組套件 (Core Backend Package)

本套件遵循專案規格書第 0 節鐵律 (ABSOLUTE GUARDRAILS)：
1. 嚴格隔離 ArcMap 10.x (32-bit Python 2.7) 宿主環境，不污染系統環境變數。
2. 絕對禁止依賴 geopandas, rasterio, fiona, shapely, pyproj, scipy, matplotlib, pyqgis, arcpy。
3. 所有空間運算 100% 透過 core.env_runner 呼叫原生 GDAL/OGR C++ CLI 與 SQLite SQL 完成。
4. 提供非同步執行緒與回呼機制，確保 UI 永遠保持流暢無阻塞。
"""

import os
from pathlib import Path
from typing import Final

# 向 Windows 核心註冊獨立沙盒 DLL 搜尋路徑 (支援 Python 3.8+ Windows 安全載入機制)
_runtime_dir = Path(__file__).resolve().parent.parent / "runtime"
_lib_bin = _runtime_dir / "Library" / "bin"
if _lib_bin.exists() and hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(str(_lib_bin))
    except OSError as _err:
        _ = str(_err)

# add_dll_directory 管不到延遲載入 (delay-load) 的相依 DLL：numpy 的 MKL 執行緒層在第一次呼叫
# LAPACK (例如 numpy.linalg.lstsq) 時才載入，若 PATH 找不到沙盒的 Library/bin，行程會直接以
# 0xC06D007F 崩潰。這裡只調整「本行程」的 PATH (os.environ)，不寫入系統或使用者環境變數。
if _lib_bin.exists():
    _path_items = os.environ.get("PATH", "").split(os.pathsep)
    _lib_bin_key = os.path.normcase(str(_lib_bin))
    if _lib_bin_key not in {os.path.normcase(_item) for _item in _path_items}:
        os.environ["PATH"] = os.pathsep.join([str(_lib_bin)] + [_item for _item in _path_items if _item])

__version__: Final[str] = "1.0.0"
__author__: Final[str] = "GIS Engineering Toolkit Development Team"

__all__ = [
    "env_runner",
    "geo_io",
    "crs_doctor",
    "boundary_builder",
    "clip_master",
    "preview_engine",
    "terrain_lab",
    "vector_ops",
    "recipe_manager",
]
