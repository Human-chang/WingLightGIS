"""發佈打包自我測試模組 (core/selftest.py)。

供 `runtime\\python.exe main.py --self-test <結果.json>` 在組裝完成的發佈資料夾內執行：
1. 全程使用 tempfile 獨立暫存與設定目錄，不寫入或修改程式資料夾與 %APPDATA%。
2. 不依賴 tests/ 與 ui/lab/，所有測試圖資皆於暫存區現場合成。
3. 驗證：
   - GDAL/PROJ 環境與 TWD97 -> WGS84 座標轉換
   - CustomTkinter 主視窗建立、主題與分頁切換、安全關閉 (無 ui.lab 時正常不載入)
   - 圖資快覽：瀏覽器出圖 HTML、另存 SHP 與另存 KMZ（並讀回 KMZ 驗證）
   - 三種裁切工作流：KMZ 邊界匯入、DEM 裁切（含等高線 DXF 與坡度圖）、正射影像裁切（含 CAD 底圖 JPG）、SHP 裁切（含屬性 Excel）
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PIL import Image

from core import __version__, env_runner, geo_io
from core.boundary_builder import import_boundary_file
from core.clip_master import clip_dem_layer, clip_ortho_layer, clip_vector_layer
from core.clip_preview import STYLE_VECTOR
from core.dem_products import generate_contour_dxf, generate_slope_percent
from core.quick_view_export import ExportSource, export_to_kml, export_to_shapefiles
from core.quick_view_map import MapSource, build_quick_view_map

logger = logging.getLogger("core.selftest")


@dataclass
class SelfTestCheckResult:
    """單一自我測試項目的執行結果。"""

    name: str
    passed: bool
    detail: str
    duration_ms: float


def _build_synthetic_dataset(work_dir: Path) -> Tuple[Path, Path, Path, Path]:
    """在暫存資料夾內合成向量、邊界、DEM 與正射影像（純透過 CLI 與 Pillow，無需 osgeo）。"""
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. 地籍／用地向量 GeoJSON (EPSG:3826, 範圍 250000~250100, 2700000~2700100)
    parcel_geojson = work_dir / "parcel_src.geojson"
    parcel_payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3826"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"名稱": "範例用地A", "PARCEL_ID": "A001"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[249990.0, 2699990.0], [250110.0, 2699990.0], [250110.0, 2700110.0], [249990.0, 2700110.0], [249990.0, 2699990.0]]],
                },
            }
        ],
    }
    parcel_geojson.write_text(json.dumps(parcel_payload, ensure_ascii=False), encoding="utf-8")

    parcel_shp = work_dir / "sample_parcel.shp"
    env_runner.run_command(
        ["ogr2ogr", "-f", "ESRI Shapefile", "-lco", "ENCODING=UTF-8", str(parcel_shp), str(parcel_geojson)],
        check=True,
    )

    # 2. 裁切邊界 GeoJSON (100m x 100m，落在向量內部)
    boundary_geojson = work_dir / "boundary_src.geojson"
    boundary_payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3826"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"名稱": "範例邊界"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[250000.0, 2700000.0], [250100.0, 2700000.0], [250100.0, 2700100.0], [250000.0, 2700100.0], [250000.0, 2700000.0]]],
                },
            }
        ],
    }
    boundary_geojson.write_text(json.dumps(boundary_payload, ensure_ascii=False), encoding="utf-8")

    # 3. 16x16 ESRI ASCII DEM -> GeoTIFF (涵蓋 249980~250140, 2699960~2700120, 解析度 10m)
    rows: List[str] = []
    for r in range(16):
        cols = [f"{100.0 + r * 0.8 + c * 0.4:.2f}" for c in range(16)]
        rows.append(" ".join(cols))
    dem_asc = work_dir / "dem_src.asc"
    dem_asc.write_text(
        "\n".join(
            [
                "ncols 16",
                "nrows 16",
                "xllcorner 249980.0",
                "yllcorner 2699960.0",
                "cellsize 10.0",
                "nodata_value -9999",
                *rows,
            ]
        ),
        encoding="ascii",
    )
    dem_tif = work_dir / "sample_dem.tif"
    env_runner.run_command(
        ["gdal_translate", "-of", "GTiff", "-ot", "Float32", "-a_srs", "EPSG:3826", str(dem_asc), str(dem_tif)],
        check=True,
    )

    # 4. 16x16 RGB 正射影像 (PNG + .pgw) -> GeoTIFF
    ortho_png = work_dir / "ortho_src.png"
    img = Image.new("RGB", (16, 16), color=(96, 148, 84))
    img.save(ortho_png)
    ortho_pgw = work_dir / "ortho_src.pgw"
    ortho_pgw.write_text("10.0\n0.0\n0.0\n-10.0\n249985.0\n2700115.0\n", encoding="ascii")
    ortho_tif = work_dir / "sample_ortho.tif"
    env_runner.run_command(
        ["gdal_translate", "-of", "GTiff", "-a_srs", "EPSG:3826", str(ortho_png), str(ortho_tif)],
        check=True,
    )

    return parcel_shp, boundary_geojson, dem_tif, ortho_tif


def _check_gdal_proj_transform(work_dir: Path, boundary_geojson: Path) -> str:
    health = env_runner.check_environment_health()
    if not health.is_healthy:
        raise RuntimeError(f"執行環境檢查未通過: {'; '.join(health.warnings)}")

    wgs84_geojson = work_dir / "reprojected_wgs84.geojson"
    env_runner.run_command(
        ["ogr2ogr", "-f", "GeoJSON", "-s_srs", "EPSG:3826", "-t_srs", "EPSG:4326", str(wgs84_geojson), str(boundary_geojson)],
        check=True,
    )
    data = json.loads(wgs84_geojson.read_text(encoding="utf-8"))
    coords = data["features"][0]["geometry"]["coordinates"][0]
    lon, lat = coords[0][0], coords[0][1]
    if not (120.0 <= lon <= 122.0 and 23.0 <= lat <= 26.0):
        raise AssertionError(f"TWD97->WGS84 轉換座標異常: ({lon}, {lat})")
    return f"GDAL/PROJ 正常；TWD97->WGS84 首點 ({lon:.5f}, {lat:.5f})"


def _check_main_window_lifecycle() -> str:
    import ui.dashboard_app as dash

    app = dash.GisMasterToolkitApp()
    try:
        app.update_idletasks()
        app.update()
        expected = ["view", "dem", "ortho", "shp"]
        if app.tool_keys[:4] != expected:
            raise AssertionError(f"預期工具鍵 {expected}，實際為 {app.tool_keys}")
        has_lab_dir = (env_runner.get_toolkit_root() / "ui" / "lab").is_dir()
        if not has_lab_dir:
            if dash.lab is not None or app.btn_lab is not None:
                raise AssertionError("發佈環境不應出現測試版工具開關")
            if any(name == "ui.lab" or name.startswith("ui.lab.") for name in sys.modules):
                raise AssertionError("發佈環境不應載入 ui.lab")
        return f"主視窗開啟與關閉正常（工具數量 {len(app.tool_keys)}，發佈純淨模式={not has_lab_dir}）"
    finally:
        app.destroy()


def _check_quick_view_export_and_kmz(work_dir: Path, parcel_shp: Path) -> str:
    out_html = work_dir / "quick_view.html"
    map_res = build_quick_view_map(out_html, [MapSource(path=parcel_shp, style=STYLE_VECTOR)])
    if not map_res.html_path.is_file() or map_res.html_path.stat().st_size < 1000:
        raise AssertionError("圖資快覽地圖 HTML 產出失敗")

    shp_dir = work_dir / "export_shp"
    shp_res = export_to_shapefiles([ExportSource(path=parcel_shp)], shp_dir)
    if not shp_res.output_files or not shp_res.output_files[0].is_file():
        raise AssertionError(f"圖資快覽另存 SHP 失敗: {shp_res.skipped}")

    kmz_dir = work_dir / "export_kmz"
    kmz_res = export_to_kml([ExportSource(path=parcel_shp)], kmz_dir, kmz=True)
    if not kmz_res.output_files:
        raise AssertionError(f"圖資快覽另存 KMZ 失敗: {kmz_res.skipped}")
    kmz_path = kmz_res.output_files[0]
    with zipfile.ZipFile(kmz_path, "r") as zf:
        if "doc.kml" not in zf.namelist():
            raise AssertionError("KMZ 壓縮檔缺少 doc.kml")
    kmz_info = geo_io.read_layer_info(kmz_path)
    if kmz_info.feature_count < 1:
        raise AssertionError("無法從匯出的 KMZ 讀回圖徵")
    return f"快覽 HTML ({map_res.html_path.stat().st_size} bytes)、另存 SHP 與 KMZ（圖徵數 {kmz_info.feature_count}）皆通過"


def _check_clip_workflows(work_dir: Path, parcel_shp: Path, boundary_geojson: Path, dem_tif: Path, ortho_tif: Path) -> str:
    # 先把 100m x 100m 裁切邊界轉成 KMZ 再匯入，同時驗證 K3 (KMZ 邊界匯入) + 三種裁切
    boundary_kmz_res = export_to_kml([ExportSource(path=boundary_geojson)], work_dir / "boundary_kmz", kmz=True)
    if not boundary_kmz_res.output_files:
        raise AssertionError(f"邊界轉換 KMZ 失敗: {boundary_kmz_res.skipped}")
    boundary_res = import_boundary_file(boundary_kmz_res.output_files[0], output_gpkg=work_dir / "master_boundary.gpkg")
    master_boundary = boundary_res.gpkg_path

    # 1. DEM 裁切 + 等高線 DXF + 坡度圖
    dem_out = work_dir / "clipped_dem.tif"
    dem_res = clip_dem_layer(dem_tif, master_boundary, dem_out, target_resolution=10.0)
    if not dem_res.is_success or not dem_out.is_file():
        raise AssertionError(f"DEM 裁切失敗: {dem_res.error_message}")
    contour_dxf = work_dir / "clipped_contour.dxf"
    generate_contour_dxf(dem_out, contour_dxf, interval=1.0, index_interval=5.0)
    if not contour_dxf.is_file() or contour_dxf.stat().st_size == 0:
        raise AssertionError("DEM 等高線 DXF 產出失敗")
    slope_tif = work_dir / "clipped_slope.tif"
    generate_slope_percent(dem_out, slope_tif)
    if not slope_tif.is_file():
        raise AssertionError("DEM 坡度圖產出失敗")

    # 2. 正射影像裁切 + CAD 底圖 JPG
    ortho_out = work_dir / "clipped_ortho.tif"
    ortho_res = clip_ortho_layer(
        ortho_tif,
        master_boundary,
        ortho_out,
        generate_pyramids=False,
        export_cad_background=True,
        target_resolution=10.0,
    )
    if not ortho_res.is_success or not ortho_out.is_file() or not (ortho_res.cad_bg_jpg and ortho_res.cad_bg_jpg.is_file()):
        raise AssertionError(f"正射影像裁切或 CAD 底圖產出失敗: {ortho_res.error_message}")

    # 3. SHP 裁切 + 屬性 Excel（原 14400 m² 裁切成 100m x 100m ≈ 10000 m²）
    shp_out = work_dir / "clipped_parcel.shp"
    excel_out = work_dir / "clipped_parcel.xlsx"
    shp_res = clip_vector_layer(parcel_shp, master_boundary, shp_out, excel_output_path=excel_out)
    if not shp_res.is_success or not shp_out.is_file() or not excel_out.is_file():
        raise AssertionError(f"SHP 裁切或屬性 Excel 產出失敗: {shp_res.error_message}")
    clipped_area = shp_res.area_m2 or 0.0
    if not (9000.0 <= clipped_area <= 11000.0):
        raise AssertionError(f"SHP 裁切後面積計算異常（預期約 10000 m²）: {clipped_area}")

    return (
        f"KMZ 邊界匯入、DEM (含 DXF/坡度)、正射影像 (含 CAD JPG)、"
        f"SHP 裁切 (面積 {clipped_area:.1f} m², Excel 已產生) 皆通過"
    )


def _run_step(name: str, func: Callable[[], str], checks: List[SelfTestCheckResult]) -> bool:
    t0 = time.perf_counter()
    try:
        detail = func()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        checks.append(SelfTestCheckResult(name=name, passed=True, detail=detail, duration_ms=round(elapsed_ms, 1)))
        logger.info("[SELF-TEST PASS] %s: %s (%.1f ms)", name, detail, elapsed_ms)
        return True
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.exception("[SELF-TEST FAIL] %s", name)
        checks.append(SelfTestCheckResult(name=name, passed=False, detail=str(exc), duration_ms=round(elapsed_ms, 1)))
        return False


def run_self_test(report_path: Path) -> int:
    """執行完整自我測試並輸出 JSON 報告；全部通過回傳 0，否則回傳 1。"""
    t_start = time.perf_counter()
    checks: List[SelfTestCheckResult] = []
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    prev_temp = env_runner._workspace_temp_override
    prev_config = env_runner._config_dir_override
    prev_output = env_runner._default_output_override

    try:
        with tempfile.TemporaryDirectory(prefix="winglightgis_selftest_") as tmp_str:
            tmp_root = Path(tmp_str)
            env_runner.set_workspace_temp_dir(tmp_root / "workspace_temp")
            env_runner.set_config_dir(tmp_root / "config")
            env_runner.set_default_output_dir(tmp_root / "outputs")

            work_dir = tmp_root / "work"
            parcel_shp, boundary_geojson, dem_tif, ortho_tif = _build_synthetic_dataset(work_dir)

            ok1 = _run_step(
                "gdal_proj_transform",
                lambda: _check_gdal_proj_transform(work_dir, boundary_geojson),
                checks,
            )
            ok2 = _run_step(
                "main_window_lifecycle",
                _check_main_window_lifecycle,
                checks,
            )
            ok3 = _run_step(
                "quick_view_export_and_kmz",
                lambda: _check_quick_view_export_and_kmz(work_dir, parcel_shp),
                checks,
            )
            ok4 = _run_step(
                "clip_workflows_dem_ortho_shp",
                lambda: _check_clip_workflows(work_dir, parcel_shp, boundary_geojson, dem_tif, ortho_tif),
                checks,
            )
            all_passed = bool(ok1 and ok2 and ok3 and ok4)
    except Exception as exc:
        logger.exception("自我測試準備階段失敗")
        checks.append(SelfTestCheckResult(name="setup", passed=False, detail=str(exc), duration_ms=0.0))
        all_passed = False
    finally:
        env_runner.set_workspace_temp_dir(prev_temp)
        env_runner.set_config_dir(prev_config)
        env_runner.set_default_output_dir(prev_output)

    payload = {
        "version": __version__,
        "all_passed": all_passed,
        "elapsed_seconds": round(time.perf_counter() - t_start, 2),
        "checks": [asdict(item) for item in checks],
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all_passed else 1
