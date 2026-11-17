"""測試共用基底 (tests/_support.py)

所有測試一律繼承 IsolatedWorkspaceTestCase：
1. 把 workspace_temp 與 config 導向 tempfile 目錄，測試結束後還原。
2. 暫存目錄的子目錄刻意含中文與空白，讓每個整合測試都順便涵蓋非 ASCII 路徑。
3. 測試前後各拍一次正式 config/ 與 workspace_temp/ 的快照，有任何變動即判定失敗。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import env_runner, geo_io

PROJECT_ROOT: Path = env_runner.get_toolkit_root()
REAL_CONFIG_DIR: Path = PROJECT_ROOT / "config"
REAL_WORKSPACE_TEMP: Path = PROJECT_ROOT / "workspace_temp"

DirSnapshot = Optional[Dict[str, Tuple[int, int]]]


def snapshot_directory(directory: Path) -> DirSnapshot:
    """記錄目錄內所有檔案的大小與修改時間；目錄不存在時回傳 None。"""
    if not directory.exists():
        return None
    snapshot: Dict[str, Tuple[int, int]] = {}
    for item in sorted(directory.rglob("*")):
        if item.is_file():
            stat = item.stat()
            snapshot[item.relative_to(directory).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


# ==========================================
# 合成測試資料
# ==========================================

def square(x0: float, y0: float, x1: float, y1: float) -> Dict[str, Any]:
    """回傳矩形面的 GeoJSON 幾何。"""
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def write_geojson(
    path: Path,
    features: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    epsg: int = 3826,
) -> Path:
    """把 (屬性, 幾何) 清單寫成帶座標系宣告的 GeoJSON。

    注意：沒有 crs 成員的 GeoJSON 會被 GDAL 當成 EPSG:4326，要測「無座標系」請改用 strip_prj 處理過的 Shapefile。
    """
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{epsg}"}},
        "features": [{"type": "Feature", "properties": props, "geometry": geometry} for props, geometry in features],
    }
    path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    return path


# 假的 Google Earth 範圍 (臺北附近，經緯度)：根層一個點，「會勘點」2 個點，「工區範圍」1 個面＋子資料夾「中心線」1 條線，
# 「空資料夾」沒有地標
SAMPLE_KML: str = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>測試範圍</name>
<Placemark><name>根層點</name><Point><coordinates>121.50,25.03,0</coordinates></Point></Placemark>
<Folder><name>會勘點</name>
 <Placemark><name>點A</name><description>擋土牆</description><Point><coordinates>121.51,25.04,0</coordinates></Point></Placemark>
 <Placemark><name>點B</name><Point><coordinates>121.52,25.05,0</coordinates></Point></Placemark>
</Folder>
<Folder><name>工區範圍</name>
 <Placemark><name>範圍1</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
 121.50,25.00,0 121.53,25.00,0 121.53,25.02,0 121.50,25.02,0 121.50,25.00,0
 </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
 <Folder><name>中心線</name>
  <Placemark><name>線1</name><LineString><coordinates>121.50,25.01,0 121.53,25.01,0</coordinates></LineString></Placemark>
 </Folder>
</Folder>
<Folder><name>空資料夾</name></Folder>
</Document></kml>
"""
SAMPLE_KML_LAYERS: Tuple[str, ...] = ("測試範圍", "會勘點", "工區範圍", "中心線")

# 假的 GPS 紀錄：2 個航點、1 條軌跡 (3 個軌跡點)，沒有路線
SAMPLE_GPX: str = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
<wpt lat="25.04" lon="121.51"><name>WP1</name></wpt>
<wpt lat="25.05" lon="121.52"><name>WP2</name></wpt>
<trk><name>巡查軌跡</name><trkseg>
 <trkpt lat="25.00" lon="121.50"></trkpt><trkpt lat="25.01" lon="121.51"></trkpt><trkpt lat="25.02" lon="121.52"></trkpt>
</trkseg></trk>
</gpx>
"""


def write_kml(path: Path, text: str = SAMPLE_KML) -> Path:
    """寫 KML；副檔名是 .kmz 時包成 KMZ (壓縮檔裡的 doc.kml)。"""
    if path.suffix.lower() == ".kmz":
        import zipfile

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("doc.kml", text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def write_world_image(
    path: Path,
    *,
    size: Tuple[int, int] = (40, 40),
    pixel: Optional[float] = 5.0,
    origin_x: float = 250000.0,
    top_y: float = 2650200.0,
    color: Tuple[int, int, int] = (200, 100, 50),
) -> Path:
    """純色 JPG／PNG (依副檔名)，pixel 有值時旁邊寫世界檔 (.jgw／.pgw：副檔名頭尾加 w，座標是左上角像元中心)。
    預設 40×40 像元、5 m，涵蓋 x 250000～250200、y 2650000～2650200 (同 test_clip_ortho.write_ortho)。"""
    from PIL import Image

    image = Image.new("RGB", size, color)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        image.save(path, quality=100)
    else:
        image.save(path)
    if pixel is not None:
        ext = path.suffix.lower().lstrip(".")
        lines = [pixel, 0.0, 0.0, -pixel, origin_x + pixel / 2, top_y - pixel / 2]
        path.with_suffix(f".{ext[0]}{ext[-1]}w").write_text("".join(f"{v!r}\n" for v in lines), encoding="ascii")
    return path


def write_gpx(path: Path, text: str = SAMPLE_GPX) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def convert_vector(source: Path, target: Path, **export_options: Any) -> Path:
    """把合成資料轉成另一種格式 (依副檔名)；Shapefile 以 UTF-8 寫出並帶 .cpg。"""
    geo_io.export_vector(source, None, target, **export_options)
    return target


def strip_prj(shapefile: Path) -> Path:
    """刪除 Shapefile 的 .prj，模擬沒有座標系標籤的來源。"""
    shapefile.with_suffix(".prj").unlink()
    return shapefile


def attribute_rows(path: Path, layer_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """讀出成果檔的全部屬性列。"""
    return geo_io.read_attribute_rows(path, layer_name)


def write_dem(
    path: Path,
    elevations: Any,
    *,
    origin_x: float = 250000.0,
    top_y: float = 2650200.0,
    cell_size: float = 5.0,
    epsg: Optional[int] = 3826,
    nodata: float = -9999.0,
) -> Path:
    """把二維高程陣列寫成 GeoTIFF DEM (左上角為 origin_x, top_y)；epsg 為 None 時不寫座標系。"""
    from osgeo import gdal, osr

    geo_io.ensure_gdal_ready()
    rows, cols = elevations.shape
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((origin_x, cell_size, 0.0, top_y, 0.0, -cell_size))
    if epsg is not None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        dataset.SetProjection(srs.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(elevations.astype("float32"))
    band.FlushCache()
    dataset = None
    return path


def line(*points: Tuple[float, float]) -> Dict[str, Any]:
    """回傳折線的 GeoJSON 幾何。"""
    return {"type": "LineString", "coordinates": [list(point) for point in points]}


def total_area(path: Path, layer_name: Optional[str] = None) -> float:
    """以 SQL 把成果圖層的面積加總寫成欄位再讀回 (平方公尺)。"""
    with geo_io.TempVectorFiles("test_area") as temp_files:
        staged = temp_files.new("stage")
        geo_io.stage_vector_to_gpkg(path, staged, source_layer=layer_name, stage_layer="stage")
        summed = temp_files.new("sum")
        geo_io.run_sql_to_gpkg(staged, 'SELECT SUM(ST_Area("geom")) AS "TOTAL" FROM "stage"', summed, "total")
        return float(geo_io.read_attribute_rows(summed, "total")[0]["TOTAL"] or 0.0)


class IsolatedWorkspaceTestCase(unittest.TestCase):
    """把暫存與設定目錄隔離到 tempfile 的測試基底類別。"""

    def setUp(self) -> None:
        super().setUp()
        self._real_snapshots = {
            REAL_CONFIG_DIR: snapshot_directory(REAL_CONFIG_DIR),
            REAL_WORKSPACE_TEMP: snapshot_directory(REAL_WORKSPACE_TEMP),
        }

        self._tmp_holder = tempfile.TemporaryDirectory(prefix="gis_toolkit_test_", ignore_cleanup_errors=True)
        self.tmp_dir: Path = Path(self._tmp_holder.name) / "測試 工作區"
        self.work_temp: Path = self.tmp_dir / "workspace_temp"
        self.config_dir: Path = self.tmp_dir / "config"
        self.data_dir: Path = self.tmp_dir / "來源圖資"
        self.out_dir: Path = self.tmp_dir / "成果輸出"
        for directory in (self.work_temp, self.config_dir, self.data_dir, self.out_dir):
            directory.mkdir(parents=True, exist_ok=True)

        # 複製一份正式設定檔供測試讀取與改寫，正式檔本身不會被碰到
        if REAL_CONFIG_DIR.exists():
            for item in REAL_CONFIG_DIR.iterdir():
                if item.is_file():
                    shutil.copy2(item, self.config_dir / item.name)

        env_runner.set_workspace_temp_dir(self.work_temp)
        env_runner.set_config_dir(self.config_dir)
        self.default_output_dir: Path = self.tmp_dir / "預設成果"
        env_runner.set_default_output_dir(self.default_output_dir)
        self.addCleanup(self._restore_and_verify_real_directories)

    def _restore_and_verify_real_directories(self) -> None:
        env_runner.set_workspace_temp_dir(None)
        env_runner.set_config_dir(None)
        env_runner.set_default_output_dir(None)
        self._tmp_holder.cleanup()
        for directory, before in self._real_snapshots.items():
            self.assertEqual(
                before,
                snapshot_directory(directory),
                f"測試不得改動正式目錄: {directory}",
            )
