"""向量圖層的屬性 Excel (core/attribute_report.py)

一個工作表：標題、說明、欄名列、每個圖徵一列、最後一列合計。
- 全部欄位照圖層的欄位順序與原值輸出；欄名用原欄位名 (不受 Shapefile 欄位名 10 bytes 的限制)，
  呼叫端可替某些欄位加中文說明 (例如 AREA_M2 → 裁切後面積)。
- 合計列只放呼叫端給的合計值 (寫死的數值，不是公式，任何軟體開都看得到)；其他數字欄位不加總 (可能是代碼)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core import geo_io
from core.env_runner import ToolkitError

logger = logging.getLogger("core.attribute_report")

# Excel 一個工作表最多 1,048,576 列 (扣掉標題、說明、欄名與合計)
_MAX_DATA_ROWS: int = 1_048_576 - 10
# 估欄寬時最多看幾列、欄寬上下限 (字元數，中文算 2)
_WIDTH_SAMPLE_ROWS: int = 300
_MIN_WIDTH: int = 6
_MAX_WIDTH: int = 40

_TITLE_FONT = Font(bold=True, size=14)
_NOTE_FONT = Font(color="666666")
_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
_TOTAL_FILL = PatternFill("solid", fgColor="FFF2CC")
_THIN = Side(style="thin", color="999999")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

INDEX_HEADER: str = "序號"


@dataclass
class AttributeReportResult:
    output_path: Path
    row_count: int


def _display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _cell_value(value: Any) -> Any:
    """Excel 存得下的值：數字與文字照原樣 (拿掉 Excel 不接受的控制字元)，其他型別轉成文字。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value)
    return ILLEGAL_CHARACTERS_RE.sub("", str(value))


def write_attribute_excel(
    vector_path: Union[str, Path],
    layer_name: Optional[str],
    output_xlsx: Union[str, Path],
    *,
    title: str,
    notes: Sequence[str] = (),
    column_labels: Optional[Mapping[str, str]] = None,
    number_formats: Optional[Mapping[str, str]] = None,
    totals: Optional[Mapping[str, Optional[float]]] = None,
    sheet_name: str = "屬性表",
) -> AttributeReportResult:
    """把圖層全部圖徵的屬性寫成 Excel (第一欄是序號，最後一列是合計)。

    column_labels：欄位名 → 中文說明，欄名寫成「說明＋換行＋欄位名」；沒列的欄位只寫欄位名。
    number_formats：欄位名 → Excel 數字格式 (例如 "#,##0.00")，合計列同格式。
    totals：欄位名 → 合計值 (None 表示這一欄的合計留白)；沒列的欄位不加總。
    檔案被 Excel 開著時拋 ToolkitError 說明要先關掉。
    """
    out = Path(output_xlsx).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    labels = dict(column_labels or {})
    formats = dict(number_formats or {})
    total_values = dict(totals or {})

    info = geo_io.read_layer_info(vector_path, layer_name)
    rows = geo_io.read_attribute_rows(vector_path, info.layer_name)
    if len(rows) > _MAX_DATA_ROWS:
        raise ToolkitError(f"圖徵有 {len(rows):,} 筆，超過 Excel 一個工作表的列數上限，無法輸出。")
    fields = info.field_names
    column_count = len(fields) + 1

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name

    sheet.cell(row=1, column=1, value=_cell_value(title)).font = _TITLE_FONT
    for offset, note in enumerate(notes):
        sheet.cell(row=2 + offset, column=1, value=_cell_value(note)).font = _NOTE_FONT
    header_row = 2 + len(notes) + 1

    headers = [INDEX_HEADER] + [f"{labels[name]}\n{name}" if name in labels else name for name in fields]
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=header_row, column=column, value=_cell_value(header))
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for index, row in enumerate(rows, start=1):
        excel_row = header_row + index
        cell = sheet.cell(row=excel_row, column=1, value=index)
        cell.border = _BORDER
        for column, name in enumerate(fields, start=2):
            cell = sheet.cell(row=excel_row, column=column, value=_cell_value(row.get(name)))
            cell.border = _BORDER
            if name in formats:
                cell.number_format = formats[name]

    total_row = header_row + len(rows) + 1
    cell = sheet.cell(row=total_row, column=1, value=f"合計 ({len(rows)} 筆)")
    for column in range(1, column_count + 1):
        sheet.cell(row=total_row, column=column).fill = _TOTAL_FILL
        sheet.cell(row=total_row, column=column).border = _BORDER
        sheet.cell(row=total_row, column=column).font = _HEADER_FONT
    for column, name in enumerate(fields, start=2):
        value = total_values.get(name)
        if value is not None:
            total_cell = sheet.cell(row=total_row, column=column, value=value)
            if name in formats:
                total_cell.number_format = formats[name]

    # 欄寬依欄名與前幾百列的內容估算
    for column, header in enumerate(headers, start=1):
        widths: List[int] = [max(_display_width(line) for line in header.split("\n"))]
        name = fields[column - 2] if column > 1 else None
        for row in rows[:_WIDTH_SAMPLE_ROWS]:
            value = row.get(name) if name is not None else None
            if value is not None:
                widths.append(_display_width(f"{value:,.2f}" if isinstance(value, float) else str(value)))
        if column == 1:
            widths.append(_display_width(f"合計 ({len(rows)} 筆)"))
        sheet.column_dimensions[get_column_letter(column)].width = max(_MIN_WIDTH, min(_MAX_WIDTH, max(widths) + 2))
    if any(name in labels for name in fields):
        sheet.row_dimensions[header_row].height = 32

    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=2)
    last_data_row = header_row + max(len(rows), 1)
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(column_count)}{last_data_row}"

    try:
        workbook.save(out)
    except PermissionError as err:
        raise ToolkitError(f"無法寫入 {out.name}：檔案可能正在 Excel 開著，請關掉後再執行一次。") from err
    return AttributeReportResult(output_path=out, row_count=len(rows))
