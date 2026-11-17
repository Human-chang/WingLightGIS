"""座標表格 (.csv／.txt) 轉點位 (core/csv_points.py)

圖資快覽與 [G-11] 表格轉點 (vector_ops.convert_csv_table_to_points) 共用。這個模組不載入 openpyxl，開程式時可以 import。
1. decode_table_bytes／write_table_as_utf8_csv：判斷文字編碼 (BOM → UTF-8 → Big5 CP950)；GDAL 的 CSV 驅動只認 UTF-8。
2. looks_like_coordinate_table：只看第一行的欄位名，有 X 類與 Y 類的欄才算座標表 (資料夾裡的說明、紀錄 .txt 不用真的轉就能略過)。
3. table_to_raw_points：轉成沒有座標系的點位 GPKG (圖層 points)，來源座標系由呼叫端決定；
   找不到座標欄位或沒有任何有效座標時拋 ToolkitError。
4. rename_in_messages：處理紀錄裡的暫存點位檔名換回原本的表格檔名。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

from core import geo_io
from core.env_runner import ToolkitError, run_command

TextCallback = Optional[Callable[[str], None]]

# GDAL CSV 驅動的 X_POSSIBLE_NAMES／Y_POSSIBLE_NAMES (不分大小寫，* 是萬用字元)
X_COLUMN_PATTERN: str = "X,E,Lon*,經度,坐標X"
Y_COLUMN_PATTERN: str = "Y,N,Lat*,緯度,坐標Y"
POINTS_LAYER: str = "points"
TABLE_EXTENSIONS = frozenset({".csv", ".txt"})
# 判斷是不是座標表時最多讀這麼多位元組 (第一行在裡面就好)
HEADER_PEEK_BYTES: int = 64 * 1024
COLUMNS_HINT: str = "X／Y、E／N、經度／緯度 (Lon／Lat)、坐標X／坐標Y"

_SEPARATORS = re.compile(r"[,;\t]")


def decode_table_bytes(raw: bytes, name: str) -> Tuple[str, str]:
    """判斷座標表格的文字編碼並解碼，回傳 (文字, 編碼名稱)。

    判斷順序：BOM (UTF-8／UTF-16) → 整份可用 UTF-8 解碼 → 整份可用 Big5 (CP950) 解碼；都不行就拋 ToolkitError。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates = [("utf-8-sig", "UTF-8")]
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = [("utf-16", "UTF-16")]
    else:
        candidates = [("utf-8", "UTF-8"), ("cp950", "Big5 (CP950)")]
    for codec, label in candidates:
        try:
            return raw.decode(codec), label
        except UnicodeDecodeError:
            continue
    raise ToolkitError(f"無法辨識座標表格 {name} 的文字編碼 (已嘗試 UTF-8 與 Big5)，請另存成 UTF-8 的 CSV 後再試。")


def write_table_as_utf8_csv(table_path: Path, utf8_csv_path: Path) -> str:
    """把座標表格另存成 UTF-8 (無 BOM) 的 .csv，回傳判定的編碼名稱。來源檔不動。"""
    text, label = decode_table_bytes(table_path.read_bytes(), table_path.name)
    utf8_csv_path.write_bytes(text.encode("utf-8"))
    return label


def _first_line(raw: bytes) -> Optional[str]:
    """表格的第一行 (讀不出來時 None)。只讀了開頭一段，UTF-16 的最後一個字可能被切斷，所以忽略解不開的尾巴。"""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="ignore")
    else:
        line = raw.split(b"\n", 1)[0]
        if line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        for codec in ("utf-8", "cp950"):
            try:
                text = line.decode(codec)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None
    lines = text.splitlines()
    return lines[0] if lines else None


def _header_fields(line: str) -> List[str]:
    """第一行拆成欄位名：有逗號、分號或 Tab 就照它們拆，都沒有時照空白拆；去掉引號與前後空白。"""
    parts = _SEPARATORS.split(line) if _SEPARATORS.search(line) else line.split()
    return [part.strip().strip('"').strip() for part in parts if part.strip().strip('"').strip()]


def _matches(fields: Sequence[str], pattern: str) -> bool:
    names = [name.strip().casefold() for name in pattern.split(",") if name.strip()]
    return any(fnmatch.fnmatchcase(field.casefold(), name) for field in fields for name in names)


def looks_like_coordinate_table(
    table_path: Union[str, Path],
    *,
    x_column_pattern: str = X_COLUMN_PATTERN,
    y_column_pattern: str = Y_COLUMN_PATTERN,
) -> bool:
    """第一行的欄位名裡有 X 類與 Y 類的欄 (跟 GDAL 用同樣的欄位名) 就算座標表；讀不到、解不開時 False。"""
    try:
        with open(table_path, "rb") as handle:
            raw = handle.read(HEADER_PEEK_BYTES)
    except OSError:
        return False
    line = _first_line(raw)
    if line is None:
        return False
    fields = _header_fields(line)
    return _matches(fields, x_column_pattern) and _matches(fields, y_column_pattern)


def rename_in_messages(text_callback: TextCallback, staged_name: str, table_name: str) -> TextCallback:
    """包一層 text_callback：訊息裡的暫存點位檔名 (例如 樁位.gpkg) 換成原本的表格檔名 (樁位.csv)。名稱相同時原樣回傳。"""
    if text_callback is None or staged_name == table_name:
        return text_callback
    return lambda text: text_callback(text.replace(staged_name, table_name))


def table_to_raw_points(
    table_path: Union[str, Path],
    output_gpkg: Union[str, Path],
    *,
    x_column_pattern: str = X_COLUMN_PATTERN,
    y_column_pattern: str = Y_COLUMN_PATTERN,
    keep_coordinate_columns: bool = False,
    text_callback: TextCallback = None,
) -> str:
    """座標表格轉成沒有座標系的點位 GPKG (圖層 points)，回傳判定的文字編碼名稱。

    keep_coordinate_columns：X、Y 欄也留在屬性裡 (圖資快覽點圖徵時看得到原始座標)。
    表格可以是 UTF-8、UTF-16 (帶 BOM) 或 Big5 (CP950)，副檔名 .csv／.txt 皆可，分隔符號由 GDAL 自動判斷。
    """
    in_table = Path(table_path).resolve()
    out_gpkg = Path(output_gpkg).resolve()
    if not in_table.exists():
        raise FileNotFoundError(f"找不到座標表格: {in_table}")
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    with geo_io.TempVectorFiles("csv_points") as temp_files:
        # GDAL 的 CSV 驅動只認 UTF-8，也不認 .txt 副檔名：一律先轉成 UTF-8 的暫存 .csv
        utf8_csv = temp_files.new("utf8", suffix=".csv")
        encoding_label = write_table_as_utf8_csv(in_table, utf8_csv)
        cmd = [
            "ogr2ogr",
            "-f", "GPKG",
            str(out_gpkg),
            str(utf8_csv),
            "-oo", f"X_POSSIBLE_NAMES={x_column_pattern}",
            "-oo", f"Y_POSSIBLE_NAMES={y_column_pattern}",
            "-oo", f"KEEP_GEOM_COLUMNS={'YES' if keep_coordinate_columns else 'NO'}",
            "-nln", POINTS_LAYER,
            "-lco", f"GEOMETRY_NAME={geo_io.STAGE_GEOMETRY_COLUMN}",
        ]
        run_command(cmd, text_callback=text_callback, check=True)
    if geo_io.read_layer_info(out_gpkg, POINTS_LAYER).extent is None:
        raise ToolkitError(
            f"座標表格 {in_table.name} 找不到座標欄位或沒有任何有效座標 "
            f"(X 欄位名: {x_column_pattern}；Y 欄位名: {y_column_pattern})。"
        )
    return encoding_label
