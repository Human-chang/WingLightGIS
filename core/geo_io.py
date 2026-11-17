"""圖資中繼資料讀取與共用轉檔工具 (core/geo_io.py)

設計原則：
1. 「讀取中繼資料」(圖層名、欄位、幾何欄位名、座標系、範圍、屬性值) 改用沙盒內建的 osgeo.gdal / osgeo.ogr，
   不再以正規表示式解析 ogrinfo 的文字輸出。
2. 裁切、轉檔、重投影等運算仍一律透過 core.env_runner.run_command 呼叫原生 CLI；本模組不以 osgeo 做幾何運算。
3. 使用 osgeo 前先把沙盒的 GDAL_DATA / PROJ 資料目錄套用到「本行程」，不寫入系統或使用者環境變數。
4. 向量引擎共用的三件事集中在這裡：
   - stage_vector_to_gpkg：把任何來源正規化成「圖層名固定、幾何欄位固定為 geom」的暫存 GPKG，
     之後的 SQL 就不必再猜幾何欄位名與圖層名。
   - export_vector：把暫存 GPKG 匯出成使用者要的格式；Shapefile 一律帶 -lco ENCODING 並產生 .cpg，
     欄位名依目標編碼在「字元邊界」截到 10 bytes (GDAL 自己截會把中文字切成殘缺位元組)。
   - Shapefile 來源編碼判定：無 .cpg 的 Big5 檔以 -oo ENCODING=CP950 讀取，不改動來源檔。
"""

from __future__ import annotations

import logging
import os
import struct
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Final, Iterable, List, Optional, Sequence, Tuple, Union

from osgeo import gdal, ogr, osr

from core.env_runner import (
    CommandResult,
    ToolkitError,
    build_isolated_env,
    get_runtime_dir,
    get_workspace_temp_dir,
    run_command,
)

logger = logging.getLogger("core.geo_io")

# ==========================================
# 常數
# ==========================================

# 暫存 GPKG 一律使用的幾何欄位名
STAGE_GEOMETRY_COLUMN: Final[str] = "geom"

# 輸出 Shapefile 的預設屬性編碼 (使用者決定第 4 點)；另支援 CP950 (Big5，舊版相容)
DEFAULT_SHP_OUTPUT_ENCODING: Final[str] = "UTF-8"
_SHP_ENCODING_ALIASES: Final[Dict[str, str]] = {
    "UTF-8": "UTF-8",
    "UTF8": "UTF-8",
    "65001": "UTF-8",
    "CP950": "CP950",
    "BIG5": "CP950",
    "BIG-5": "CP950",
    "950": "CP950",
}
_PYTHON_CODEC_BY_SHP_ENCODING: Final[Dict[str, str]] = {"UTF-8": "utf-8", "CP950": "cp950"}

# DBF 欄位名上限 (bytes)
SHP_FIELD_NAME_MAX_BYTES: Final[int] = 10

# 副檔名對應的 OGR 驅動名稱
_VECTOR_DRIVER_BY_SUFFIX: Final[Dict[str, str]] = {
    ".shp": "ESRI Shapefile",
    ".gpkg": "GPKG",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".dxf": "DXF",
    ".kml": "KML",
    ".csv": "CSV",
}

# 附世界檔才有座標的圖片 (正射影像的另一種存法，例如 AutoCAD 底圖 JPG)；沒有世界檔的照片沒有座標
IMAGE_EXTENSIONS: Final[frozenset] = frozenset({".jpg", ".jpeg", ".png"})
WORLD_FILE_EXTENSIONS: Final[frozenset] = frozenset({".jgw", ".jpgw", ".jpegw", ".pgw", ".pngw", ".wld"})
NO_WORLD_FILE_REASON: Final[str] = "沒有世界檔 (.jgw／.pgw)，沒有座標"

# GPX 的這兩個圖層是路線、軌跡上的每個點，跟路線、軌跡重複
GPX_DUPLICATE_LAYERS: Final[Tuple[str, ...]] = ("route_points", "track_points")
# KML／KMZ 每個地標都有的顯示設定欄位 (LIBKML 讀成屬性)，對圖資本身沒有意義
KML_DISPLAY_FIELDS: Final[frozenset] = frozenset(
    {"timestamp", "begin", "end", "altitudeMode", "tessellate", "extrude", "visibility", "drawOrder", "icon", "snippet"}
)
# 這些驅動讀出的檔，正規化時拿掉沒用的欄位 (droppable_format_fields)
_FORMAT_FIELD_DRIVERS: Final[frozenset] = frozenset({"LIBKML", "KML", "GPX"})

# DXF 驅動只接受這些屬性欄位，其餘欄位寫不進去
DXF_WRITABLE_FIELDS: Final[Tuple[str, ...]] = ("Layer", "PaperSpace", "SubClasses", "Linetype", "EntityHandle", "Text")

_NUMERIC_FIELD_TYPES: Final[Tuple[str, ...]] = ("Integer", "Integer64", "Real")

ProgressCallback = Optional[Callable[[float, str], None]]
TextCallback = Optional[Callable[[str], None]]


# ==========================================
# 資料結構
# ==========================================

@dataclass
class FieldInfo:
    """向量圖層屬性欄位描述。"""

    name: str
    type_name: str
    width: int = 0
    precision: int = 0

    @property
    def is_numeric(self) -> bool:
        """是否為數值欄位 (組 SQL 條件時不可加單引號)。"""
        return self.type_name in _NUMERIC_FIELD_TYPES

    @property
    def is_integer(self) -> bool:
        """是否為整數欄位。"""
        return self.type_name in ("Integer", "Integer64")


@dataclass
class VectorLayerInfo:
    """向量圖層中繼資料。"""

    path: Path
    driver: str
    layer_name: str
    geometry_column: str
    geometry_type: str
    feature_count: int
    fields: List[FieldInfo] = field(default_factory=list)
    fid_column: str = ""
    has_srs: bool = False
    srs_name: Optional[str] = None
    srs_wkt: Optional[str] = None
    epsg: Optional[int] = None
    extent: Optional[Tuple[float, float, float, float]] = None
    layer_names: List[str] = field(default_factory=list)
    # 座標系是否為經緯度 (單位為度)；此時 ST_Area / ST_Length 的結果不是公尺制，不可拿來當面積與長度
    is_geographic: bool = False

    @property
    def field_names(self) -> List[str]:
        """屬性欄位名稱清單 (不含 FID 與幾何欄位)。"""
        return [f.name for f in self.fields]

    @property
    def sql_geometry_name(self) -> str:
        """SQLite 方言中可用的幾何欄位名；Shapefile 等沒有具名幾何欄位的來源一律是 geometry。"""
        return self.geometry_column or "geometry"

    def find_field(self, name: str) -> Optional[FieldInfo]:
        """以不分大小寫的方式尋找欄位。"""
        wanted = name.casefold()
        for item in self.fields:
            if item.name.casefold() == wanted:
                return item
        return None


@dataclass
class VectorExportResult:
    """export_vector 的匯出結果。"""

    output_path: Path
    output_format: str
    layer_name: str
    encoding: Optional[str] = None
    # 因 DBF 10 bytes 上限而改名的欄位：原欄位名 → 實際寫出的欄位名
    renamed_fields: Dict[str, str] = field(default_factory=dict)


# ==========================================
# 本行程 GDAL 初始化
# ==========================================

_gdal_ready_lock = threading.Lock()
_gdal_ready: bool = False


def ensure_gdal_ready() -> None:
    """讓本行程內的 osgeo 使用沙盒的 GDAL_DATA 與 PROJ 資料目錄 (只影響本行程)。

    Windows 版 GDAL / PROJ 讀取含中文的環境變數路徑會失敗，所以除了設定 os.environ 之外，
    另以 API (UTF-8 字串) 直接指定搜尋目錄。
    """
    global _gdal_ready
    if _gdal_ready:
        return
    with _gdal_ready_lock:
        if _gdal_ready:
            return

        isolated_env = build_isolated_env()
        for key in ("PROJ_LIB", "PROJ_DATA", "GDAL_FILENAME_IS_UTF8"):
            value = isolated_env.get(key)
            if value:
                os.environ[key] = value
        # 全域 SHAPE_ENCODING 會蓋掉正確的 .cpg，GDAL_DRIVER_PATH 可能指向宿主環境的外掛
        for harmful_key in ("SHAPE_ENCODING", "GDAL_DRIVER_PATH"):
            os.environ.pop(harmful_key, None)

        gdal.UseExceptions()
        ogr.UseExceptions()
        osr.UseExceptions()
        gdal.SetConfigOption("GDAL_FILENAME_IS_UTF8", "YES")
        gdal.SetConfigOption("SHAPE_ENCODING", None)

        runtime_share = get_runtime_dir() / "Library" / "share"
        gdal_data_dir = runtime_share / "gdal"
        if gdal_data_dir.is_dir():
            os.environ["GDAL_DATA"] = str(gdal_data_dir)
            gdal.SetConfigOption("GDAL_DATA", str(gdal_data_dir))
            gdal.PushFinderLocation(str(gdal_data_dir))
        proj_data_dir = runtime_share / "proj"
        if (proj_data_dir / "proj.db").is_file():
            osr.SetPROJSearchPaths([str(proj_data_dir)])

        _gdal_ready = True


# ==========================================
# SQL 字串工具
# ==========================================

def quote_ident(name: str) -> str:
    """把圖層名或欄位名包成 SQL 識別字 (雙引號，內含的雙引號加倍)。"""
    return '"' + str(name).replace('"', '""') + '"'


def quote_literal(value: Any) -> str:
    """把值包成 SQL 字串常值 (單引號，內含的單引號加倍)。"""
    return "'" + str(value).replace("'", "''") + "'"


def build_equality_filter(field_info: FieldInfo, value: Any) -> str:
    """依欄位型別組出「欄位 = 值」的條件式；None 以 IS NULL 表示，數值欄位不加引號。"""
    ident = quote_ident(field_info.name)
    if value is None:
        return f"{ident} IS NULL"
    if field_info.is_integer:
        return f"{ident} = {int(value)}"
    if field_info.is_numeric:
        return f"{ident} = {float(value)!r}"
    return f"{ident} = {quote_literal(value)}"


def build_field_select_list(layer_info: "VectorLayerInfo", exclude: Iterable[str] = ()) -> List[str]:
    """回傳圖層屬性欄位的 SQL 識別字清單 (已加雙引號)；exclude 內的欄位名以不分大小寫方式排除。

    SQL 不用 SELECT *：暫存 GPKG 的 * 會把幾何欄位再帶出一次，也無法排除與計算欄位同名的來源欄位。
    """
    excluded = {str(name).casefold() for name in exclude}
    return [quote_ident(name) for name in layer_info.field_names if name.casefold() not in excluded]


# ==========================================
# Shapefile 來源編碼判定
# ==========================================

def normalize_shp_encoding(encoding: Optional[str]) -> str:
    """把使用者指定的 Shapefile 編碼名稱正規化為 UTF-8 或 CP950；None 回傳預設值。"""
    if encoding is None or not str(encoding).strip():
        return DEFAULT_SHP_OUTPUT_ENCODING
    key = str(encoding).strip().upper()
    if key not in _SHP_ENCODING_ALIASES:
        raise ToolkitError(f"不支援的 Shapefile 屬性編碼: {encoding} (可用 UTF-8 或 CP950)")
    return _SHP_ENCODING_ALIASES[key]


def _decodes_as_utf8(raw: bytes) -> bool:
    """判斷位元組是否為合法 UTF-8；因欄寬截斷而缺尾的最後一個字元不算錯。"""
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError as err:
        return "unexpected end" in err.reason and err.start >= len(raw) - 3


def _sniff_dbf_text(dbf_path: Path, max_records: int = 5000) -> Tuple[int, Optional[str]]:
    """讀取 DBF 檔頭與前段文字欄位，回傳 (LDID, 內容嗅探結果)。

    嗅探結果：None 表示全為 ASCII (任何編碼讀出來都一樣)；"UTF-8" 表示非 ASCII 內容皆為合法 UTF-8；
    "CP950" 表示含有不是 UTF-8 的位元組，依臺灣圖資慣例視為 Big5。
    """
    with open(dbf_path, "rb") as handle:
        header = handle.read(32)
        if len(header) < 32:
            return 0, None
        record_count, header_length, record_length = struct.unpack("<IHH", header[4:12])
        language_driver_id = header[29]
        descriptor_bytes = handle.read(max(header_length - 32, 0))

        text_fields: List[Tuple[int, int]] = []
        samples: List[bytes] = []
        field_offset = 1  # 每筆紀錄第 1 個 byte 是刪除旗標
        for pos in range(0, len(descriptor_bytes) - 31, 32):
            block = descriptor_bytes[pos : pos + 32]
            if block[0] == 0x0D:
                break
            samples.append(block[:11].split(b"\x00", 1)[0])
            field_type = chr(block[11])
            field_length = block[16] + (block[17] << 8 if field_type == "C" else 0)
            if field_type == "C":
                text_fields.append((field_offset, field_length))
            field_offset += field_length

        if record_length > 0 and text_fields:
            for _ in range(min(record_count, max_records)):
                record = handle.read(record_length)
                if len(record) < record_length:
                    break
                for offset, length in text_fields:
                    samples.append(record[offset : offset + length].rstrip(b" \x00"))

    has_non_ascii = False
    for raw in samples:
        if raw.isascii():
            continue
        has_non_ascii = True
        if not _decodes_as_utf8(raw):
            return language_driver_id, "CP950"
    return language_driver_id, ("UTF-8" if has_non_ascii else None)


def detect_shapefile_encoding(shapefile_path: Union[str, Path]) -> Optional[str]:
    """判定讀取 Shapefile 時是否需要以 ENCODING 開啟選項指定屬性編碼。

    回傳 None 表示交給 GDAL 依 .cpg 或 DBF 檔頭自行處理即可；否則回傳 "CP950" 或 "UTF-8"。
    - 有 .cpg：照 .cpg。唯一例外是 .cpg 寫 UTF-8 但內容根本不是 UTF-8 (被錯誤補上的 .cpg)，此時改用 CP950。
    - 無 .cpg 且 DBF 檔頭未標示語系 (LDID 為 0 或 87，ArcMap 與舊版軟體常見)：GDAL 會當成 Latin-1 而出現亂碼，
      改以內容嗅探決定。
    """
    shp_path = Path(shapefile_path)
    dbf_path = shp_path.with_suffix(".dbf")
    if not dbf_path.is_file():
        return None
    try:
        language_driver_id, sniffed = _sniff_dbf_text(dbf_path)
    except (OSError, struct.error) as err:
        logger.debug("讀取 DBF 檔頭失敗，略過編碼判定: %s", err)
        return None

    cpg_path = shp_path.with_suffix(".cpg")
    if cpg_path.is_file():
        try:
            cpg_text = cpg_path.read_text(encoding="ascii", errors="ignore").strip().upper()
        except OSError:
            cpg_text = ""
        if _SHP_ENCODING_ALIASES.get(cpg_text) == "UTF-8" and sniffed == "CP950":
            logger.warning("%s 的 .cpg 標示為 UTF-8，但屬性內容不是合法 UTF-8，改以 CP950 (Big5) 讀取。", shp_path.name)
            return "CP950"
        return None

    if language_driver_id in (0, 87):
        return sniffed
    return None


def resolve_source_encoding(vector_path: Union[str, Path], source_encoding: Optional[str] = None) -> Optional[str]:
    """決定讀取來源時要指定的屬性編碼；只有 Shapefile 需要，其他格式回傳 None。

    source_encoding 為使用者明確指定的編碼 (例如 "CP950")，優先於自動判定。
    """
    path = Path(vector_path)
    if path.suffix.lower() not in (".shp", ".dbf"):
        return None
    if source_encoding is not None and str(source_encoding).strip():
        return str(source_encoding).strip().upper()
    return detect_shapefile_encoding(path)


def source_open_options(vector_path: Union[str, Path], source_encoding: Optional[str] = None) -> List[str]:
    """回傳讀取來源時要附加給 ogr2ogr / ogrinfo 的開啟選項 (例如 ["-oo", "ENCODING=CP950"])。"""
    encoding = resolve_source_encoding(vector_path, source_encoding)
    return ["-oo", f"ENCODING={encoding}"] if encoding else []


# ==========================================
# 中繼資料讀取 (osgeo)
# ==========================================

def _open_vector(vector_path: Union[str, Path], source_encoding: Optional[str] = None) -> Any:
    """以唯讀方式開啟向量資料集。"""
    ensure_gdal_ready()
    path = Path(vector_path)
    if not path.exists():
        raise ToolkitError(f"找不到向量圖資檔案: {path}")

    encoding = resolve_source_encoding(path, source_encoding)
    open_options = [f"ENCODING={encoding}"] if encoding else []
    try:
        dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY, open_options=open_options)
    except RuntimeError as err:
        raise ToolkitError(f"無法開啟向量圖資: {path}\n{err}") from err
    if dataset is None:
        raise ToolkitError(f"無法開啟向量圖資: {path}")
    return dataset


def _pick_layer(dataset: Any, vector_path: Path, layer_name: Optional[str]) -> Any:
    """取得指定圖層；未指定時取第一個有圖徵的圖層 (都沒有圖徵時取第一個)。

    KML 根層沒有地標時那一層是空的、圖徵在後面的資料夾裡；GPKG 也可能第一個圖層是空的。"""
    if dataset.GetLayerCount() == 0:
        raise ToolkitError(f"向量圖資內沒有任何圖層: {vector_path}")
    if layer_name:
        layer = dataset.GetLayerByName(layer_name)
        if layer is None:
            available = "、".join(dataset.GetLayer(i).GetName() for i in range(dataset.GetLayerCount()))
            raise ToolkitError(f"向量圖資 {vector_path.name} 內找不到圖層「{layer_name}」(現有圖層: {available})")
        return layer
    if dataset.GetLayerCount() > 1:
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayer(index)
            if layer.GetFeatureCount() > 0:
                return layer
    return dataset.GetLayer(0)


def has_world_file(image_path: Union[str, Path]) -> bool:
    """圖片旁邊有沒有世界檔或 .aux.xml (GDAL 找的檔名：副檔名頭尾加 w 的 .jgw／.pgw、.jpgw／.pngw、.wld)。

    只看檔案在不在、不開檔 (資料夾掃描用，很快)；真的讀不讀得到座標由 clip_master.detect_track_type 判斷。"""
    path = Path(image_path)
    ext = path.suffix.lower().lstrip(".")
    if not ext:
        return False
    candidates = (f".{ext[0]}{ext[-1]}w", f".{ext}w", ".wld")
    return any(path.with_suffix(suffix).is_file() for suffix in candidates) or path.with_name(f"{path.name}.aux.xml").is_file()


def list_data_layers(vector_path: Union[str, Path], *, source_encoding: Optional[str] = None) -> List[str]:
    """一個檔裡有圖徵的圖層 (KML／KMZ 的每個資料夾、GPKG 的圖層、GPX 的航點／路線／軌跡)，照檔案裡的順序。

    沒有圖徵或沒有幾何的圖層不列；GPX 的 route_points、track_points 是路線、軌跡上的每個點 (跟路線、軌跡重複)，不列。"""
    path = Path(vector_path)
    dataset = _open_vector(path, source_encoding)
    try:
        is_gpx = dataset.GetDriver().ShortName == "GPX"
        names: List[str] = []
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayer(index)
            if is_gpx and layer.GetName() in GPX_DUPLICATE_LAYERS:
                continue
            if layer.GetGeomType() != ogr.wkbNone and layer.GetFeatureCount() > 0:
                names.append(layer.GetName())
        return names
    finally:
        dataset = None


def droppable_format_fields(info: VectorLayerInfo) -> List[str]:
    """KML／KMZ、GPX 自帶但通常沒用的欄位：KML 的顯示設定 (tessellate、extrude 等) 與整欄都沒有值的欄位。
    其他格式回傳空的 (欄位全部保留)。另存與地圖彈窗只留有用的欄位 (名稱、說明、自訂欄位)。"""
    if info.driver not in _FORMAT_FIELD_DRIVERS or not info.fields:
        return []
    names = [field_info.name for field_info in info.fields]
    drop = [name for name in names if info.driver in ("LIBKML", "KML") and name in KML_DISPLAY_FIELDS]
    rows = read_attribute_rows(info.path, info.layer_name, fields=[name for name in names if name not in drop])
    for name in names:
        if name not in drop and all(row.get(name) in (None, "") for row in rows):
            drop.append(name)
    return drop


def _identify_epsg(srs: Any) -> Optional[int]:
    """由座標系物件判定 EPSG 代碼；ArcMap 寫出的 .prj 沒有 AUTHORITY 節點，另以比對方式辨識。"""
    if srs is None:
        return None
    try:
        if (srs.GetAuthorityName(None) or "").upper() == "EPSG":
            return int(srs.GetAuthorityCode(None))
    except (RuntimeError, TypeError, ValueError) as err:
        logger.debug("讀取座標系 AUTHORITY 略過: %s", err)
    try:
        matches = srs.FindMatches() or []
    except RuntimeError as err:
        logger.debug("比對 EPSG 座標系略過: %s", err)
        matches = []
    for matched_srs, confidence in matches:
        try:
            if confidence >= 90 and (matched_srs.GetAuthorityName(None) or "").upper() == "EPSG":
                return int(matched_srs.GetAuthorityCode(None))
        except (RuntimeError, TypeError, ValueError) as err:
            logger.debug("讀取比對結果略過: %s", err)
    return None


def list_layer_names(vector_path: Union[str, Path], *, source_encoding: Optional[str] = None) -> List[str]:
    """列出向量圖資內所有圖層名稱。"""
    dataset = _open_vector(vector_path, source_encoding)
    try:
        return [dataset.GetLayer(i).GetName() for i in range(dataset.GetLayerCount())]
    finally:
        dataset = None


def identify_epsg_from_wkt(wkt: Optional[str]) -> Optional[int]:
    """由 WKT 字串判定 EPSG 代碼；無法判定時回傳 None。"""
    if not wkt or not str(wkt).strip():
        return None
    ensure_gdal_ready()
    try:
        srs = osr.SpatialReference(wkt=str(wkt))
    except RuntimeError as err:
        logger.debug("解析座標系 WKT 略過: %s", err)
        return None
    return _identify_epsg(srs)


def read_layer_info(
    vector_path: Union[str, Path],
    layer_name: Optional[str] = None,
    *,
    source_encoding: Optional[str] = None,
) -> VectorLayerInfo:
    """讀取向量圖層的中繼資料 (圖層名、幾何欄位名、欄位、座標系、範圍、圖徵數)。"""
    path = Path(vector_path)
    dataset = _open_vector(path, source_encoding)
    try:
        layer = _pick_layer(dataset, path, layer_name)
        definition = layer.GetLayerDefn()

        fields: List[FieldInfo] = []
        for idx in range(definition.GetFieldCount()):
            field_defn = definition.GetFieldDefn(idx)
            fields.append(
                FieldInfo(
                    name=field_defn.GetName(),
                    type_name=field_defn.GetTypeName(),
                    width=field_defn.GetWidth(),
                    precision=field_defn.GetPrecision(),
                )
            )

        feature_count = int(layer.GetFeatureCount())
        extent: Optional[Tuple[float, float, float, float]] = None
        if feature_count > 0 and layer.GetGeomType() != ogr.wkbNone:
            try:
                min_x, max_x, min_y, max_y = layer.GetExtent()
                extent = (float(min_x), float(min_y), float(max_x), float(max_y))
            except RuntimeError as err:
                logger.debug("讀取圖層範圍略過: %s", err)

        srs = layer.GetSpatialRef()
        return VectorLayerInfo(
            path=path,
            driver=dataset.GetDriver().ShortName,
            layer_name=layer.GetName(),
            geometry_column=layer.GetGeometryColumn() or "",
            geometry_type=ogr.GeometryTypeToName(layer.GetGeomType()),
            feature_count=feature_count,
            fields=fields,
            fid_column=layer.GetFIDColumn() or "",
            has_srs=srs is not None,
            srs_name=srs.GetName() if srs is not None else None,
            srs_wkt=srs.ExportToWkt() if srs is not None else None,
            epsg=_identify_epsg(srs),
            extent=extent,
            layer_names=[dataset.GetLayer(i).GetName() for i in range(dataset.GetLayerCount())],
            is_geographic=bool(srs.IsGeographic()) if srs is not None else False,
        )
    except RuntimeError as err:
        raise ToolkitError(f"讀取向量圖層資訊失敗: {path}\n{err}") from err
    finally:
        dataset = None


def read_attribute_rows(
    vector_path: Union[str, Path],
    layer_name: Optional[str] = None,
    *,
    fields: Optional[Sequence[str]] = None,
    source_encoding: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """讀取圖層所有圖徵的屬性值 (不讀幾何)；NULL 以 None 表示。

    fields 可限定要讀的欄位；指定了不存在的欄位會拋出 ToolkitError。
    """
    path = Path(vector_path)
    dataset = _open_vector(path, source_encoding)
    try:
        layer = _pick_layer(dataset, path, layer_name)
        definition = layer.GetLayerDefn()
        all_names = [definition.GetFieldDefn(idx).GetName() for idx in range(definition.GetFieldCount())]

        if fields is None:
            wanted = list(enumerate(all_names))
        else:
            wanted = []
            for name in fields:
                if name not in all_names:
                    raise ToolkitError(f"圖層「{layer.GetName()}」沒有欄位「{name}」(現有欄位: {'、'.join(all_names)})")
                wanted.append((all_names.index(name), name))

        if layer.TestCapability(ogr.OLCIgnoreFields):
            layer.SetIgnoredFields(["OGR_GEOMETRY"])

        rows: List[Dict[str, Any]] = []
        layer.ResetReading()
        for feature in layer:
            rows.append(
                {name: (feature.GetField(idx) if feature.IsFieldSetAndNotNull(idx) else None) for idx, name in wanted}
            )
        return rows
    except RuntimeError as err:
        raise ToolkitError(f"讀取屬性資料失敗: {path}\n{err}") from err
    finally:
        dataset = None


def read_value_counts(
    vector_path: Union[str, Path],
    field_name: str,
    layer_name: Optional[str] = None,
    *,
    source_encoding: Optional[str] = None,
) -> Dict[Any, int]:
    """統計欄位各相異值的圖徵數 (依首次出現順序)；NULL 以 None 為鍵，空字串保留為空字串。"""
    counts: Dict[Any, int] = {}
    for row in read_attribute_rows(vector_path, layer_name, fields=[field_name], source_encoding=source_encoding):
        value = row[field_name]
        counts[value] = counts.get(value, 0) + 1
    return counts


# ==========================================
# 格式與欄位名工具
# ==========================================

def infer_vector_driver(output_path: Union[str, Path], output_format: Optional[str] = None) -> str:
    """決定輸出驅動名稱：有明確指定就用指定值，否則依副檔名推斷。"""
    if output_format:
        return output_format
    suffix = Path(output_path).suffix.lower()
    if suffix not in _VECTOR_DRIVER_BY_SUFFIX:
        raise ToolkitError(f"無法由副檔名判斷輸出格式: {Path(output_path).name} (支援 .shp/.gpkg/.geojson/.dxf/.kml/.csv)")
    return _VECTOR_DRIVER_BY_SUFFIX[suffix]


def _truncate_to_bytes(text: str, codec: str, max_bytes: int) -> str:
    """在字元邊界把字串截到指定位元組數以內；目標編碼無法表示的字元以底線取代。"""
    pieces: List[str] = []
    used_bytes = 0
    for char in text:
        try:
            char_bytes = len(char.encode(codec))
        except UnicodeEncodeError:
            char, char_bytes = "_", 1
        if used_bytes + char_bytes > max_bytes:
            break
        pieces.append(char)
        used_bytes += char_bytes
    return "".join(pieces)


def make_safe_shp_field_names(
    field_names: Sequence[str],
    encoding: Optional[str] = None,
) -> Dict[str, str]:
    """替 Shapefile 輸出產生合法欄位名對照表 (原欄位名 → 寫出欄位名)。

    DBF 欄位名上限 10 bytes：UTF-8 的中文最多 3 個字、CP950 最多 5 個字。
    截斷後若與其他欄位重名 (DBF 不分大小寫)，以 _1、_2… 尾碼區分。
    """
    codec = _PYTHON_CODEC_BY_SHP_ENCODING[normalize_shp_encoding(encoding)]
    mapping: Dict[str, str] = {}
    used_keys: set = set()

    for original in field_names:
        base = _truncate_to_bytes(original, codec, SHP_FIELD_NAME_MAX_BYTES) or "FIELD"
        candidate = base
        serial = 1
        while candidate.casefold() in used_keys:
            suffix = f"_{serial}"
            candidate = _truncate_to_bytes(base, codec, SHP_FIELD_NAME_MAX_BYTES - len(suffix)) + suffix
            serial += 1
        used_keys.add(candidate.casefold())
        mapping[original] = candidate
    return mapping


def remove_vector_file(vector_path: Union[str, Path]) -> None:
    """刪除既有的輸出檔 (Shapefile 連同同名附屬檔)，供重新產生前清場。"""
    path = Path(vector_path)
    if path.suffix.lower() == ".shp":
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qpj", ".shp.xml"):
            sidecar = path.parent / f"{path.stem}{suffix}"
            if sidecar.is_file():
                sidecar.unlink()
        return
    if path.is_file():
        path.unlink()
    # .aux.xml：GDAL 以唯讀方式開啟 GPKG 等檔案、需要記下統計資訊時寫在旁邊的附屬檔
    for suffix in ("-journal", "-wal", "-shm", ".aux.xml"):
        leftover = Path(str(path) + suffix)
        if leftover.is_file():
            leftover.unlink()


def scaled_progress(
    progress_callback: ProgressCallback,
    start_percent: float,
    end_percent: float,
) -> ProgressCallback:
    """把單一指令回報的 0～100% 進度換算到整體流程的一個區段，避免多段式流程的進度條來回跳動。"""
    if progress_callback is None:
        return None
    span = end_percent - start_percent

    def _forward(percent: float, message: str) -> None:
        clamped = max(0.0, min(100.0, float(percent)))
        progress_callback(start_percent + span * clamped / 100.0, message)

    return _forward


class TempVectorFiles:
    """管理單次運算用到的暫存 GPKG：檔名不重複，離開 with 區塊時全部刪除。

    暫存檔放在 get_workspace_temp_dir() (測試時會被導向 tempfile 目錄)。
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._token = uuid.uuid4().hex[:8]
        self._paths: List[Path] = []

    def new(self, tag: str, suffix: str = ".gpkg") -> Path:
        """配置一個新的暫存檔路徑 (尚未建立檔案)。"""
        path = get_workspace_temp_dir() / f"_{self._prefix}_{self._token}_{tag}{suffix}"
        self._paths.append(path)
        return path

    def cleanup(self) -> None:
        """刪除本次配置的所有暫存檔；刪不掉 (例如仍被占用) 只記錄不拋錯。"""
        for path in self._paths:
            try:
                remove_vector_file(path)
            except OSError as err:
                logger.debug("暫存檔刪除略過: %s (%s)", path, err)
        self._paths.clear()

    def __enter__(self) -> "TempVectorFiles":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.cleanup()


# ==========================================
# 共用轉檔 (CLI)
# ==========================================

def run_sql_to_gpkg(
    source_gpkg: Union[str, Path],
    sql: str,
    output_gpkg: Union[str, Path],
    output_layer: str,
    *,
    extra_args: Optional[Sequence[str]] = None,
    progress_callback: ProgressCallback = None,
    text_callback: TextCallback = None,
) -> CommandResult:
    """以 SQLite 方言對 GPKG 執行 SQL，結果寫成另一個 GPKG。

    一律帶 -nln 與 -lco GEOMETRY_NAME=geom：-sql 的輸出圖層名預設是 SELECT、幾何欄位名會隨來源而變，
    不明確指定的話後續步驟就找不到圖層與幾何欄位。
    """
    dst = Path(output_gpkg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    remove_vector_file(dst)

    cmd: List[str] = [
        "ogr2ogr",
        "-f", "GPKG",
        str(dst),
        str(source_gpkg),
        "-dialect", "sqlite",
        "-sql", sql,
        "-nln", output_layer,
        "-lco", f"GEOMETRY_NAME={STAGE_GEOMETRY_COLUMN}",
    ]
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args)
    return run_command(cmd, progress_callback=progress_callback, text_callback=text_callback, check=True)


def stage_vector_to_gpkg(
    source_path: Union[str, Path],
    output_gpkg: Union[str, Path],
    *,
    source_layer: Optional[str] = None,
    stage_layer: str = "stage",
    target_srs: Optional[str] = None,
    source_srs: Optional[str] = None,
    source_encoding: Optional[str] = None,
    make_valid: bool = False,
    extra_args: Optional[Sequence[str]] = None,
    progress_callback: ProgressCallback = None,
    text_callback: TextCallback = None,
) -> VectorLayerInfo:
    """把來源圖層正規化成暫存 GPKG：圖層名為 stage_layer、幾何欄位為 geom、屬性一律 UTF-8。

    參數：
        source_layer: 來源圖層名；未指定時取第一個有圖徵的圖層。KML／GPX 的沒用欄位會拿掉 (droppable_format_fields)。
        target_srs: 指定時重投影到該座標系。
        source_srs: 指定時以 -s_srs 覆寫來源座標系 (只應用在來源沒有座標系標籤的情況，來源檔不會被改動)。
        source_encoding: Shapefile 來源的屬性編碼；未指定時自動判定。
        make_valid: 是否加上 -makevalid 修復無效幾何。
        extra_args: 附加給 ogr2ogr 的其他參數 (例如 -where、-simplify)。

    回傳：
        暫存 GPKG 圖層的 VectorLayerInfo。
    """
    src = Path(source_path)
    dst = Path(output_gpkg)
    source_info = read_layer_info(src, source_layer, source_encoding=source_encoding)

    dst.parent.mkdir(parents=True, exist_ok=True)
    remove_vector_file(dst)

    cmd: List[str] = ["ogr2ogr", "-f", "GPKG", str(dst), str(src)]
    cmd.extend(source_open_options(src, source_encoding))
    if source_srs:
        cmd.extend(["-s_srs", source_srs])
    if target_srs:
        cmd.extend(["-t_srs", target_srs])
    if make_valid:
        cmd.append("-makevalid")
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args)
    # KML／GPX 拿掉沒用的欄位 (呼叫端自己 -select 時不動；欄位名有逗號時 -select 分不開，全部保留)
    if "-select" not in (extra_args or ()):
        drop = droppable_format_fields(source_info)
        kept = [field_info.name for field_info in source_info.fields if field_info.name not in drop]
        if drop and not any("," in name for name in kept):
            cmd.extend(["-select", ",".join(kept)])
    cmd.extend(["-nln", stage_layer, "-lco", f"GEOMETRY_NAME={STAGE_GEOMETRY_COLUMN}", source_info.layer_name])

    run_command(cmd, progress_callback=progress_callback, text_callback=text_callback, check=True)
    return read_layer_info(dst, stage_layer)


def export_vector(
    source_path: Union[str, Path],
    source_layer: Optional[str],
    output_path: Union[str, Path],
    *,
    output_format: Optional[str] = None,
    output_layer_name: Optional[str] = None,
    shp_encoding: Optional[str] = None,
    dxf_layer_name: str = "0",
    where: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
    progress_callback: ProgressCallback = None,
    text_callback: TextCallback = None,
) -> VectorExportResult:
    """把圖層 (通常是暫存 GPKG) 匯出成最終成果檔。

    - Shapefile：一律帶 -lco ENCODING (預設 UTF-8，會產生 .cpg)；超過 10 bytes 的欄位名先在字元邊界截斷。
    - GeoJSON / DXF：這兩個驅動不支援覆寫，寫出前先刪除既有檔案。
    - DXF：只寫幾何與圖層名 (DXF 驅動不接受自訂屬性欄位)。
    - 其他 (GPKG 等)：以 -overwrite 取代同名圖層。
    - where：只匯出符合條件的圖徵 (條件式請以 build_equality_filter 等工具組出，識別字與字串需自行跳脫)。
    """
    src = Path(source_path)
    dst = Path(output_path)
    driver = infer_vector_driver(dst, output_format)
    info = read_layer_info(src, source_layer)
    layer_name = output_layer_name or dst.stem
    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = ["ogr2ogr", "-f", driver, str(dst), str(src)]
    encoding: Optional[str] = None
    renamed: Dict[str, str] = {}
    # 走 -sql 的分支把條件併進 SQL，其餘分支用 -where
    where_sql = f" WHERE {where}" if where else ""
    where_args = ["-where", where] if where else []

    if driver == "ESRI Shapefile":
        encoding = normalize_shp_encoding(shp_encoding)
        mapping = make_safe_shp_field_names(info.field_names, encoding)
        renamed = {old: new for old, new in mapping.items() if old != new}
        remove_vector_file(dst)
        if renamed:
            select_items = [quote_ident(info.geometry_column)] if info.geometry_column else []
            for old, new in mapping.items():
                select_items.append(quote_ident(old) if old == new else f"{quote_ident(old)} AS {quote_ident(new)}")
            cmd.extend(["-sql", f"SELECT {', '.join(select_items)} FROM {quote_ident(info.layer_name)}{where_sql}"])
            if text_callback:
                pairs = "、".join(f"{old}→{new}" for old, new in renamed.items())
                text_callback(f"[注意] Shapefile 欄位名上限 10 bytes，以下欄位已改名: {pairs}\n")
        else:
            cmd.extend([info.layer_name, *where_args])
        cmd.extend(["-nln", layer_name, "-lco", f"ENCODING={encoding}"])
    elif driver == "DXF":
        remove_vector_file(dst)
        geometry_ident = quote_ident(info.geometry_column) if info.geometry_column else ""
        select_items = [item for item in (geometry_ident, f"{quote_literal(dxf_layer_name)} AS {quote_ident('Layer')}") if item]
        cmd.extend(["-sql", f"SELECT {', '.join(select_items)} FROM {quote_ident(info.layer_name)}{where_sql}"])
    elif driver == "GeoJSON":
        remove_vector_file(dst)
        cmd.extend([info.layer_name, *where_args, "-nln", layer_name])
    else:
        cmd.extend([info.layer_name, *where_args, "-nln", layer_name, "-overwrite"])

    if extra_args:
        cmd.extend(str(arg) for arg in extra_args)

    run_command(cmd, progress_callback=progress_callback, text_callback=text_callback, check=True)
    if not dst.exists():
        raise ToolkitError(f"匯出向量成果失敗，未產生輸出檔: {dst}")

    return VectorExportResult(
        output_path=dst,
        output_format=driver,
        layer_name=layer_name,
        encoding=encoding,
        renamed_fields=renamed,
    )
