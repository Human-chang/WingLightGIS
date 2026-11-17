"""圖資快覽頁 (ui/tab_quick_view.py)：側欄第 1 個工具，開程式時停在這一頁。

1. 標題列：工具名稱與一句說明。
2. 卡片 1 圖資：加入檔案、加入資料夾、拖放、命令列 (SHP、DXF、GPKG、GeoJSON、KML／KMZ、GPX、DEM／正射 TIF、附世界檔的 JPG／PNG、
   CSV／TXT 座標表)；資料夾裡沒有世界檔的 JPG／PNG (照片) 不提示、處理紀錄寫一行，單獨加入的提示沒有座標；
   DWG／DGN 提示先另存 DXF，其他副檔名提示不支援；跟著 SHP／TIF 一起選到的附屬檔 (.dbf、.prj、.tfw…) 不提示、直接略過。
   座標表 (core/csv_points.py)：判別時在背景轉成沒有座標系的暫存點位 (workspace_temp/quick_view_csv，X／Y 欄留在屬性裡)，
   類型寫「座標表」，地圖、另存 SHP、縮圖都用暫存點位 (名稱照原本的 .csv 檔名)。資料夾裡的 .csv／.txt 只收第一行像座標表的，
   其他的 (說明、紀錄) 不提示、處理紀錄寫一行；單獨加入的讀不出點位時跟其他讀不到的檔一樣提示原因。
   清單每一列 (ui/check_list.py)：勾選框、檔名、類型、原始座標系 (檔案裡的標籤，沒有寫「無標籤」)、
   來源座標系 (預設依原始標籤；沒有標籤時依座標範圍推測，寫「推測 TWD97 121」，推測不出來寫「需指派」)。
   清單下面的選單改選取那一列的來源座標系：選了就以它為準，連有標籤的檔也照它 (給標錯座標的檔用)，
   這點跟裁切工具不同 (裁切工具的指派只對沒有標籤的檔有作用)。DXF 可以展開，列出 CAD 圖層各自勾選；
   一個檔有好幾個有圖徵的圖層 (KML／KMZ 的資料夾、GPKG 的圖層、GPX 的航點／路線／軌跡) 時也可以展開，各自勾選。
3. 卡片 2 另存：右上選格式 SHP／KML／KMZ；輸出座標系 (TWD97／TWD67 的 121、119 分帶，不開放經緯度)、
   文字編碼 (UTF-8 附 .cpg／Big5 CP950)，KML／KMZ 固定 WGS84、UTF-8 (兩個選單變灰、顯示固定值並寫原因，切回 SHP 還原)；
   DXF「封閉線轉面」，下面一行寫現在能不能存、會跳過幾個 (條件不夠時底部的「另存」停用)。
4. 右欄 (三頁共用的 PanelInspector)：選取那一列的體檢與縮圖；「用這個範圍當邊界」這頁用不到，
   「瀏覽器套疊航照底圖」跟底部主要按鈕相同。
5. 底部動作列 (ui/action_bar.py)：輸出位置 (預設 <預設輸出>/view_outputs)、現況、
   次要按鈕「另存勾選的向量圖層為 SHP」(字跟著格式換；core/quick_view_export.py：照各列的來源座標系轉到卡片 2 選的
   座標系與編碼，DXF 只存勾選的 CAD 圖層、SHP 依點線面分開存，KML／KMZ 每個檔一個、圖層各一個資料夾)、
   主要按鈕「在瀏覽器開啟地圖」——
   勾選的圖資各自一層 (DXF 勾選的 CAD 圖層、KML 勾選的資料夾等各自一層)，依各列的來源座標系疊上國土測繪底圖，
   點圖徵看完整屬性 (core/quick_view_map.py)。內容沒變就直接開上次的地圖。
   來源座標系「需指派」的列兩邊都跳過，寫在現況；另存完跳出的訊息也列出沒有存的圖層與原因。
類型判別、讀座標系、DXF 與多圖層檔的圖層清單、縮圖都在背景做 (PanelInspector 的 run_in_background 派送)；地圖與另存用 run_task (可取消)。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import customtkinter as ctk

from core import csv_points, geo_io
from core.boundary_builder import list_dxf_layers
from core.clip_master import TrackType, detect_track_type
from core.clip_preview import STYLE_DEM, STYLE_IMAGE, STYLE_VECTOR, render_clip_thumbnail
from core.crs_doctor import CrsDiagnosisResult, sniff_crs
from core.env_runner import get_default_output_dir, get_workspace_temp_dir, run_in_background
from core.preview_engine import open_preview_in_browser
from core.quick_view_export import KML_ENCODING, ExportSource, QuickViewExport, export_to_kml, export_to_shapefiles
from core.quick_view_map import MapSource, QuickViewMap, build_quick_view_map
from ui import drag_drop
from ui.action_bar import ActionBar
from ui.check_list import CheckList, CheckRow
from ui.file_guards import is_unsupported_cad, validate_supported_files
from ui.icons import IconButton, icon
from ui.panel_inspector import INSPECTOR_WIDTH, PREVIEW_TITLE_VIEW, PanelInspector
from ui.segmented import SegmentedControl
from ui.srs_options import (
    OUTPUT_SRS_CHOICES,
    output_srs_labels,
    short_srs_name,
    view_source_srs_from_label,
    view_source_srs_label,
    view_source_srs_labels,
)
from ui.theme_constants import (
    BTN_SECONDARY,
    CARD_FG_COLOR,
    FONT_FAMILY,
    FONT_SIZE_BODY,
    FONT_SIZE_CAPTION,
    FONT_SIZE_CARD_TITLE,
    FONT_SIZE_PAGE_TITLE,
    RADIUS_LG,
    T,
)

logger = logging.getLogger("ui.tab_quick_view")

TOOL_KEY: str = "view"
TOOL_TITLE: str = "圖資快覽"
TOOL_ICON: str = "layers"
TOOL_DESCRIPTION: str = "把圖資拖進來，確認座標系、在瀏覽器套疊航照看圖，再另存成需要的座標系。"
CARD_TITLE_DATA: str = "圖資"
# 可以加進清單的副檔名 (資料夾只收這些)；DWG／DGN 另外提示
VIEW_EXTENSIONS = frozenset(
    {".shp", ".dxf", ".gpkg", ".geojson", ".kml", ".kmz", ".gpx", ".tif", ".tiff", ".img", ".vrt", ".dem", ".asc"}
    | geo_io.IMAGE_EXTENSIONS
)
# 座標表：背景轉成暫存點位 (資料夾裡只收第一行像座標表的)
TABLE_EXTENSIONS = csv_points.TABLE_EXTENSIONS
TABLE_STAGE_FOLDER_NAME: str = "quick_view_csv"
VIEW_FILETYPES: Tuple[str, str] = ("圖資", ";".join(f"*{ext}" for ext in sorted(VIEW_EXTENSIONS | TABLE_EXTENSIONS)))
# SHP、TIF 的附屬檔：在檔案總管全選一起拖進來時不提示 (.xml 是 .shp.xml、.aux.xml)
SIDECAR_EXTENSIONS = frozenset(
    {".dbf", ".shx", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fix", ".xml", ".tfw", ".tifw", ".ovr", ".aux", ".lock"}
    | geo_io.WORLD_FILE_EXTENSIONS
)
UNSUPPORTED_TITLE: str = "不支援的檔案"
UNSUPPORTED_HINT: str = (
    "圖資快覽可以開 SHP、DXF、GPKG、GeoJSON、KML、KMZ、GPX、TIF、IMG、VRT、DEM、ASC、附世界檔的 JPG／PNG，"
    "以及 CSV／TXT 座標表。"
)
UNREADABLE_TEXT: str = "讀不到圖資內容 (可能不是 GIS 圖檔或檔案損壞)"
NOT_TABLE_TEXT: str = f"不是座標表 (第一行找不到 {csv_points.COLUMNS_HINT} 欄位)"
# 清單的欄 (表頭字, 100% 縮放時的寬度)
LIST_TITLE: str = "檔名"
LIST_COLUMNS: Tuple[Tuple[str, int], ...] = (("類型", 64), ("原始座標系", 120), ("來源座標系", 140))
LIST_VISIBLE_ROWS: int = 6
DETECTING_TEXT: str = "判別中"
NO_LABEL_TEXT: str = "無標籤"
BY_LABEL_TEXT: str = "依原始標籤"
NEEDS_ASSIGN_TEXT: str = "需指派"
SOURCE_MENU_LABEL: str = "選取那一列的來源座標系"
SOURCE_MENU_HINT: str = "選了就以它為準，有座標系標籤的檔也照它 (給標錯座標的檔用)。DXF 與座標表沒有標籤，要自己確認。"
SMALL_BUTTON_WIDTH: int = 80
MAX_LISTED_UNKNOWN: int = 5
MAP_BUTTON_TEXT: str = "在瀏覽器開啟地圖"
# 另存格式 (卡片 2 右上的分段選擇)；第一項是預設
FORMAT_SHP, FORMAT_KML, FORMAT_KMZ = "SHP", "KML", "KMZ"
EXPORT_FORMATS: Tuple[str, ...] = (FORMAT_SHP, FORMAT_KML, FORMAT_KMZ)
EXPORT_BUTTON_TEMPLATE: str = "另存勾選的向量圖層為 {}"
EXPORT_BUTTON_TEXT: str = EXPORT_BUTTON_TEMPLATE.format(FORMAT_SHP)
CARD_TITLE_EXPORT: str = "另存"
# 文字編碼 (選單文字, geo_io 的 SHP 編碼)；第一項是預設
ENCODING_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("UTF-8 (推薦，附 .cpg 防亂碼)", "UTF-8"),
    ("Big5／CP950 (舊版軟體相容)", "CP950"),
)
# KML／KMZ：兩個選單變灰、顯示固定的值，下面一行寫原因
KML_SRS_LABEL: str = "WGS84 經緯度"
KML_FIXED_NOTE: str = "KML／KMZ 的座標系固定 WGS84 經緯度、文字編碼固定 UTF-8 (KML 格式的規定)，不能選。"
CLOSED_POLYGON_TEXT: str = "DXF：封閉的線轉成面 (首尾相接的線存成面)"
# 分三行寫：Tk 在英數前後的空白折行，一整段會折得長短不一
_KML_HINT: str = (
    "勾選的向量圖資照各列的來源座標系，每個檔各存一個 {fmt}{extra}，存到底部「輸出到」的資料夾。\n"
    "檔裡的圖層 (DXF 勾選的 CAD 圖層、KML 的資料夾) 各一個資料夾，點、線、面放在一起；DEM／正射不轉。\n"
    "「名稱」或「Name」欄位當標記名，其他屬性在 Google Earth 點了看得到。"
)
FORMAT_BUTTON_WIDTH: int = 64
EXPORT_HINTS: Dict[str, str] = {
    FORMAT_SHP: (
        "勾選的向量圖資照各列的來源座標系，轉成上面選的座標系與編碼，各存一個 SHP，存到底部「輸出到」的資料夾。"
        "DXF 只存勾選的 CAD 圖層，依點、線、面分開存 (檔名_圖層_point／_line／_poly.shp)；"
        "KML 的資料夾等檔案裡的圖層各自存 (檔名_圖層.shp)；DEM／正射不轉。"
    ),
    FORMAT_KML: _KML_HINT.format(fmt="KML", extra=""),
    FORMAT_KMZ: _KML_HINT.format(fmt="KMZ", extra=" (壓縮過的 KML)"),
}
# 預設輸出資料夾 (另存用；在預設輸出位置底下)
VIEW_OUTPUT_FOLDER_NAME: str = "view_outputs"
SKIP_NEEDS_ASSIGN: str = "需指派來源座標系"
SKIP_NO_CAD_LAYER: str = "沒有勾選 CAD 圖層"
SKIP_NO_LAYER: str = "沒有勾選圖層"
SKIP_NOT_VECTOR: str = "DEM／正射不能另存"

# 類型 (清單「類型」欄的字) 與縮圖畫法
KIND_VECTOR: str = "向量"
KIND_CAD: str = "CAD"
KIND_DEM: str = "DEM"
KIND_ORTHO: str = "正射"
KIND_TABLE: str = "座標表"
_PREVIEW_STYLE: Dict[str, str] = {
    KIND_VECTOR: STYLE_VECTOR, KIND_CAD: STYLE_VECTOR, KIND_TABLE: STYLE_VECTOR, KIND_DEM: STYLE_DEM, KIND_ORTHO: STYLE_IMAGE,
}
VECTOR_KINDS = frozenset({KIND_VECTOR, KIND_CAD, KIND_TABLE})


def kind_text(path: Path, track: TrackType) -> Optional[str]:
    """清單「類型」欄：向量、CAD (DXF)、DEM、正射；判別不出來時 None。"""
    if track == TrackType.VECTOR_CADASTRE:
        return KIND_CAD if path.suffix.lower() == ".dxf" else KIND_VECTOR
    return {TrackType.TERRAIN_DEM: KIND_DEM, TrackType.ORTHO_AERIAL: KIND_ORTHO}.get(track)


@dataclass
class Detection:
    """背景判別一個檔的結果。kind 是 None 時讀不到，reason 是原因；data_path 是座標表轉成的暫存點位。"""

    kind: Optional[str]
    diagnosis: Optional[CrsDiagnosisResult] = None
    layers: Tuple[str, ...] = ()
    data_path: Optional[Path] = None
    reason: str = UNREADABLE_TEXT


@dataclass
class ViewItem:
    """清單裡的一個圖資。kind 與 diagnosis 是 None 時還在判別；source_srs 是 None 時依原始標籤。
    data_path：座標表轉成的暫存點位 (地圖、另存、縮圖用它)；其他圖資是 None (直接用 path)。"""

    path: Path
    kind: Optional[str] = None
    diagnosis: Optional[CrsDiagnosisResult] = None
    source_srs: Optional[str] = None
    checked: bool = True
    layers: Tuple[str, ...] = ()
    unchecked_layers: Set[str] = field(default_factory=set)
    data_path: Optional[Path] = None

    @property
    def read_path(self) -> Path:
        """實際讀的檔：座標表是暫存點位，其他是檔案本身。"""
        return self.data_path or self.path

    @property
    def title(self) -> Optional[str]:
        """給地圖、另存用的名稱：座標表用原本的檔名 (不然會寫成暫存檔名)。"""
        return self.path.name if self.data_path is not None else None

    @property
    def detected(self) -> bool:
        return self.kind is not None

    @property
    def labelled(self) -> bool:
        """檔案裡有沒有座標系標籤 (判別完才知道)。"""
        return self.diagnosis is not None and self.diagnosis.raw_srs_name != NO_LABEL_TEXT

    def original_srs_text(self) -> str:
        if self.diagnosis is None:
            return DETECTING_TEXT if not self.detected else ""
        if not self.labelled:
            return NO_LABEL_TEXT
        if self.diagnosis.detected_epsg:
            return short_srs_name(self.diagnosis.detected_epsg)
        return self.diagnosis.raw_srs_name or ""

    def source_srs_text(self) -> str:
        if self.source_srs is not None:
            return short_srs_name(self.source_srs)
        if self.diagnosis is None:
            return DETECTING_TEXT if not self.detected else ""
        if self.labelled:
            return BY_LABEL_TEXT
        guess = self.diagnosis.recommended_assign_srs
        return f"推測 {short_srs_name(guess)}" if guess else NEEDS_ASSIGN_TEXT

    @property
    def needs_assign(self) -> bool:
        """沒有標籤、推測不出來、也還沒選來源座標系 (清單寫「需指派」)：放不上地圖。"""
        return (
            self.source_srs is None
            and self.diagnosis is not None
            and not self.labelled
            and not self.diagnosis.recommended_assign_srs
        )

    def checked_layers(self) -> List[str]:
        return [layer for layer in self.layers if layer not in self.unchecked_layers]

    def _usable_layers(self) -> Tuple[Optional[Dict[str, Tuple[str, ...]]], str]:
        """地圖與另存共用的檢查：({"cad_layers" 或 "data_layers": 勾選的圖層}，沒有子列時是空的＝整個檔, "")；
        不能用時 (None, 原因)。DXF 的子列是 CAD 圖層，其他 (KML 的資料夾、GPKG 的圖層) 是檔案裡的圖層。"""
        if self.kind is None:
            return None, DETECTING_TEXT
        if self.needs_assign:
            return None, SKIP_NEEDS_ASSIGN
        if not self.layers:
            return {}, ""
        checked = tuple(self.checked_layers())
        if not checked:
            return None, SKIP_NO_CAD_LAYER if self.kind == KIND_CAD else SKIP_NO_LAYER
        return {"cad_layers" if self.kind == KIND_CAD else "data_layers": checked}, ""

    def thumbnail_layers(self) -> Tuple[str, ...]:
        """縮圖要畫的檔案裡的圖層 (多圖層的檔全部畫；DXF 整個檔一起畫，不用指定)。"""
        return self.layers if self.kind != KIND_CAD else ()

    def map_source(self) -> Tuple[Optional[MapSource], str]:
        """放上地圖用的 MapSource；放不上去時回傳 (None, 原因)。只放勾選的 CAD 圖層或檔案裡的圖層。"""
        layers, reason = self._usable_layers()
        if layers is None or self.kind is None:
            return None, reason
        return MapSource(self.read_path, _PREVIEW_STYLE[self.kind], self.source_srs, title=self.title, **layers), ""

    def export_source(self) -> Tuple[Optional[ExportSource], str]:
        """另存 (SHP／KML／KMZ) 用的 ExportSource；不能存時回傳 (None, 原因)。DXF 只存勾選的 CAD 圖層。"""
        if self.kind is not None and self.kind not in VECTOR_KINDS:
            return None, SKIP_NOT_VECTOR
        layers, reason = self._usable_layers()
        if layers is None:
            return None, reason
        return ExportSource(self.read_path, self.source_srs, title=self.title, **layers), ""


class TabQuickView(ctk.CTkFrame):
    """圖資快覽頁。右側檢視欄跟裁切工具共用：activate() 把它放進這一頁的 inspector_slot，
    要給它的東西 (體檢、縮圖) 先記在這一頁，目前顯示的才送出。"""

    def __init__(self, master: Any, inspector: PanelInspector, **kwargs: Any) -> None:
        kwargs.setdefault("fg_color", T["bg"])
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)
        self.inspector = inspector
        self.is_active: bool = False
        self.items: List[ViewItem] = []
        self._selected_index: Optional[int] = None
        self._light_pending: int = 0
        self._shown_diagnosis: Optional[CrsDiagnosisResult] = None
        self._shown_preview_image: Optional[Path] = None
        self._shown_preview_caption: str = ""
        self._preview_request: Optional[Path] = None
        self._preview_seq: int = 0
        self._last_thumbnail: Optional[Path] = None
        self._shown_preview_html: Optional[Path] = None
        self._map_key: Optional[Tuple[Any, ...]] = None
        # 座標表的暫存點位資料夾這次開程式清過了沒 (第一次加座標表時清掉上次留下的)
        self._table_dir_ready: bool = False

        self.font_page_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_PAGE_TITLE, weight="bold")
        self.font_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CARD_TITLE, weight="bold")
        self.font_caption = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION)
        self.font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)

        # 版面跟裁切工具頁相同：第 0 列標題列；第 1 列左邊可捲動的卡片區、右邊檢視欄的位置
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self.scroll_area = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.scroll_area.grid(row=1, column=0, sticky="nsew", padx=(16, 0), pady=(0, 16))
        self.scroll_area.grid_columnconfigure(0, weight=1)
        self.inspector_slot = ctk.CTkFrame(self, fg_color="transparent", width=INSPECTOR_WIDTH, corner_radius=0)
        self.inspector_slot.grid(row=1, column=1, sticky="nsew", padx=(8, 24), pady=(0, 16))
        self.inspector_slot.grid_rowconfigure(0, weight=1)
        self.inspector_slot.grid_columnconfigure(0, weight=1)
        # 卡片 2 先建：它的選單畫的時候會 update_idletasks (CTkOptionMenu)，趁頁面還空的時候做，重排的東西少
        self._build_export_card()
        self._build_data_card()
        # Tab 鍵照疊放順序走：卡片 2 放回卡片 1 後面
        self.card_export.lift(self.card_data)
        self._build_action_bar()
        self._refresh_list()

    # -------------------------------------------------------------------------
    # 右欄 (三頁共用)：先記在這一頁，目前顯示的這一頁才送出
    # -------------------------------------------------------------------------
    def activate(self) -> None:
        self.is_active = True
        self.inspector.grid(in_=self.inspector_slot, row=0, column=0, sticky="nsew")
        self.inspector.lift()
        self.inspector.on_use_extent = None
        self.inspector.on_open_browser_preview = self._open_browser_map
        self.inspector.set_preview_title(PREVIEW_TITLE_VIEW)
        self.inspector.reset_view()
        if self._shown_diagnosis is not None:
            self.inspector.update_spatial_diagnosis(self._shown_diagnosis, can_use_extent=False)
        if self._shown_preview_image is not None:
            self.inspector.load_preview_image(self._shown_preview_image, self._shown_preview_caption)
        if self._shown_preview_html is not None:
            self.inspector.set_current_preview_html(self._shown_preview_html)

    def deactivate(self) -> None:
        self.is_active = False

    def destroy(self) -> None:
        self.inspector.remove_status_listener(self.action_bar.apply_task_status)
        super().destroy()

    def set_inspector_visible(self, visible: bool) -> None:
        """視窗太窄時收起檢視欄 (卡片區改用整個寬度)；放寬後放回來。"""
        if visible:
            self.inspector_slot.grid()
            self.scroll_area.grid_configure(padx=(16, 0))
        else:
            self.inspector_slot.grid_remove()
            self.scroll_area.grid_configure(padx=(16, 16))

    def _show_diagnosis(self, diagnosis: Optional[CrsDiagnosisResult]) -> None:
        self._shown_diagnosis = diagnosis
        if not self.is_active:
            return
        if diagnosis is None:
            self.inspector.reset_view()
            if self._shown_preview_image is not None:
                self.inspector.load_preview_image(self._shown_preview_image, self._shown_preview_caption)
        else:
            self.inspector.update_spatial_diagnosis(diagnosis, can_use_extent=False)

    def _show_preview_image(self, image_path: Optional[Path], caption: str = "") -> None:
        self._shown_preview_image = image_path
        self._shown_preview_caption = caption if image_path is not None else ""
        if not self.is_active:
            return
        if image_path is None:
            self.inspector.clear_preview()
        else:
            self.inspector.load_preview_image(image_path, caption)

    def _show_preview_html(self, html_path: Path) -> None:
        self._shown_preview_html = html_path
        if self.is_active:
            self.inspector.set_current_preview_html(html_path)

    def _run_light(self, what: str, func: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        """在背景做不需要取消鈕的短查詢 (判別類型、讀座標系、縮圖)，結果回主執行緒；失敗只記日誌。"""
        self._light_pending += 1

        def _finished() -> None:
            self._light_pending -= 1

        run_in_background(
            func,
            on_success=on_done,
            on_error=lambda err: self.inspector.append_log(f"[{what}] 略過: {err}\n"),
            on_finish=_finished,
            dispatcher=self.inspector.post_to_ui,
        )

    # -------------------------------------------------------------------------
    # 標題列與卡片
    # -------------------------------------------------------------------------
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(18, 12))
        header.grid_columnconfigure(0, weight=1)
        self.header = header
        self.lbl_page_title = ctk.CTkLabel(header, text=TOOL_TITLE, font=self.font_page_title, anchor="w")
        self.lbl_page_title.grid(row=0, column=0, sticky="w")
        self.lbl_description = ctk.CTkLabel(
            header, text=TOOL_DESCRIPTION, font=self.font_body, text_color=T["muted"], anchor="w", justify="left"
        )
        self.lbl_description.grid(row=1, column=0, sticky="w", pady=(2, 0))

    def _make_card(self, row: int, number: int, title: str) -> Tuple[ctk.CTkFrame, ctk.CTkFrame]:
        """編號卡片 (規範 Panel，跟裁切工具頁相同)：編號圓點＋卡片標題，右邊可放這張卡的按鈕。"""
        card = ctk.CTkFrame(
            self.scroll_area, corner_radius=RADIUS_LG, fg_color=CARD_FG_COLOR, border_width=1,
            border_color=T["line"], bg_color=T["bg"],
        )
        card.grid(row=row, column=0, sticky="ew", padx=(8, 8), pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 10))
        head.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            head, text=str(number), width=22, height=22, corner_radius=11, fg_color=T["soft"], text_color=T["accent"],
            font=ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text=title, font=self.font_title, anchor="w").grid(row=0, column=1, sticky="w", padx=(10, 0))
        return card, head

    def _build_data_card(self) -> None:
        """卡片 1：圖資清單、選取那一列的來源座標系、移除與清空。"""
        card, head = self._make_card(0, 1, CARD_TITLE_DATA)
        self.card_data = card
        ctk.CTkLabel(head, text="也可以直接把檔案或資料夾拖進來", font=self.font_caption, text_color=T["muted"]).grid(
            row=0, column=2, sticky="e", padx=(8, 8)
        )
        IconButton(
            head, text="加入檔案", icon_name="plus", width=SMALL_BUTTON_WIDTH, font=self.font_body,
            command=self._handle_add_files, **BTN_SECONDARY,
        ).grid(row=0, column=3, padx=(0, 8))
        IconButton(
            head, text="加入資料夾", icon_name="folder", width=SMALL_BUTTON_WIDTH, font=self.font_body,
            command=self._handle_add_folder, **BTN_SECONDARY,
        ).grid(row=0, column=4)

        self.file_list = CheckList(
            card,
            title=LIST_TITLE,
            columns=LIST_COLUMNS,
            expandable=True,
            visible_rows=LIST_VISIBLE_ROWS,
            on_select=self._select_index,
            on_toggle=self._toggle_item,
            on_toggle_child=self._toggle_layer,
            on_toggle_all=self._set_all_checked,
        )
        self.file_list.grid(row=1, column=0, sticky="ew", padx=16)
        for target in self.file_list.drop_targets():
            drag_drop.bind_paths(target, self._add_dropped_paths)

        source_row = ctk.CTkFrame(card, fg_color="transparent")
        source_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 0))
        ctk.CTkLabel(source_row, text=SOURCE_MENU_LABEL, font=self.font_caption, text_color=T["muted"], anchor="w").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.opt_source_srs = ctk.CTkOptionMenu(
            source_row, values=view_source_srs_labels(), width=300, dynamic_resizing=False,
            command=self._handle_source_srs_chosen,
        )
        self.opt_source_srs.grid(row=0, column=1, sticky="w")
        self.lbl_source_hint = ctk.CTkLabel(
            card, text=SOURCE_MENU_HINT, font=self.font_caption, text_color=T["muted"], anchor="w", justify="left"
        )
        self.lbl_source_hint.grid(row=3, column=0, sticky="w", padx=16, pady=(4, 0))

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 14))
        footer.grid_columnconfigure(0, weight=1)
        self.lbl_file_count = ctk.CTkLabel(footer, text="", font=self.font_caption, text_color=T["muted"], anchor="w")
        self.lbl_file_count.grid(row=0, column=0, sticky="w")
        IconButton(
            footer, text="移除所選", icon_name="close", width=SMALL_BUTTON_WIDTH, font=self.font_body,
            command=self._handle_remove_selected, **BTN_SECONDARY,
        ).grid(row=0, column=1, padx=(8, 8))
        IconButton(
            footer, text="清空清單", icon_name="trash", width=SMALL_BUTTON_WIDTH, font=self.font_body,
            command=self._handle_clear, **BTN_SECONDARY,
        ).grid(row=0, column=2)

    def _build_export_card(self) -> None:
        """卡片 2：另存格式 (SHP／KML／KMZ)、輸出座標系、文字編碼、DXF 封閉線轉面，以及現在能不能存。"""
        card, head = self._make_card(1, 2, CARD_TITLE_EXPORT)
        self.card_export = card
        # CTkOptionMenu 畫的時候會 update_idletasks：卡片建好才放進捲動區，少幾次整個捲動區重排 (開程式總時間的差別在誤差內)
        card.grid_remove()
        self.seg_export_format = SegmentedControl(head, values=list(EXPORT_FORMATS), command=self._show_export_format)
        self.seg_export_format.grid(row=0, column=3, sticky="e")
        # 選項只有三個英文字：CTkButton 預設 140 px 太寬
        for button in self.seg_export_format.buttons.values():
            button.configure(width=FORMAT_BUTTON_WIDTH)
        # 切到 KML／KMZ 時記下 SHP 選的座標系與編碼，切回來還原
        self._shp_menu_choice: Optional[Tuple[str, str]] = None
        menus = ctk.CTkFrame(card, fg_color="transparent")
        menus.grid(row=1, column=0, sticky="ew", padx=16)
        for column, text in enumerate(("輸出座標系", "文字編碼")):
            ctk.CTkLabel(menus, text=text, font=self.font_caption, text_color=T["muted"], anchor="w").grid(
                row=0, column=column, sticky="w", padx=(0 if column == 0 else 12, 0)
            )
        self.opt_output_srs = ctk.CTkOptionMenu(menus, values=output_srs_labels(), width=300, dynamic_resizing=False)
        self.opt_output_srs.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.opt_output_srs.set(output_srs_labels()[0])
        self.opt_encoding = ctk.CTkOptionMenu(
            menus, values=[label for label, _ in ENCODING_CHOICES], width=240, dynamic_resizing=False
        )
        self.opt_encoding.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        self.opt_encoding.set(ENCODING_CHOICES[0][0])
        # KML／KMZ 時才顯示：兩個選單為什麼不能選
        self.lbl_format_note = ctk.CTkLabel(
            card, text=KML_FIXED_NOTE, font=self.font_caption, text_color=T["muted"], anchor="w", justify="left", wraplength=640
        )
        self.lbl_format_note.grid(row=2, column=0, sticky="w", padx=16, pady=(6, 0))
        self.lbl_format_note.grid_remove()
        self.chk_closed_polygons = ctk.CTkCheckBox(
            card, text=CLOSED_POLYGON_TEXT, font=self.font_body, checkbox_width=18, checkbox_height=18
        )
        self.chk_closed_polygons.grid(row=3, column=0, sticky="w", padx=16, pady=(10, 0))
        self.lbl_export_hint = ctk.CTkLabel(
            card, text=EXPORT_HINTS[FORMAT_SHP], font=self.font_caption, text_color=T["muted"], anchor="w", justify="left",
            wraplength=640,
        )
        self.lbl_export_hint.grid(row=4, column=0, sticky="w", padx=16, pady=(6, 0))
        self.lbl_export_status = ctk.CTkLabel(card, text="", font=self.font_caption, compound="left", anchor="w")
        self.lbl_export_status.grid(row=5, column=0, sticky="w", padx=16, pady=(6, 14))
        # 說明文字跟著卡片寬度折行
        card.bind("<Configure>", self._on_export_card_resize, add="+")
        card.grid()

    def _on_export_card_resize(self, event: Any) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        wraplength = max(300, event.width / scaling - 40)
        self.lbl_export_hint.configure(wraplength=wraplength)
        self.lbl_format_note.configure(wraplength=wraplength)

    def export_format(self) -> str:
        """卡片 2 選的另存格式 (SHP／KML／KMZ)。"""
        return self.seg_export_format.get()

    def _show_export_format(self, export_format: str) -> None:
        """切換另存格式：KML／KMZ 的座標系與編碼是固定的，兩個選單變灰、顯示固定的值並寫原因；切回 SHP 時還原原本選的。"""
        is_kml = export_format != FORMAT_SHP
        if is_kml and self._shp_menu_choice is None:
            self._shp_menu_choice = (self.opt_output_srs.get(), self.opt_encoding.get())
            self.opt_output_srs.set(KML_SRS_LABEL)
            self.opt_encoding.set(KML_ENCODING)
        elif not is_kml and self._shp_menu_choice is not None:
            srs_label, encoding_label = self._shp_menu_choice
            self._shp_menu_choice = None
            self.opt_output_srs.set(srs_label)
            self.opt_encoding.set(encoding_label)
        state = "disabled" if is_kml else "normal"
        self.opt_output_srs.configure(state=state)
        self.opt_encoding.configure(state=state)
        if is_kml:
            self.lbl_format_note.grid()
        else:
            self.lbl_format_note.grid_remove()
        self.lbl_export_hint.configure(text=EXPORT_HINTS[export_format])
        self.btn_export.configure(text=EXPORT_BUTTON_TEMPLATE.format(export_format))
        self._refresh_ready()

    def _build_action_bar(self) -> None:
        """底部動作列 (不隨卡片捲動)：輸出位置、現況、次要按鈕「另存」、主要按鈕「在瀏覽器開啟地圖」。"""
        self.action_bar = ActionBar(
            self,
            primary_text=MAP_BUTTON_TEXT,
            primary_command=self._open_browser_map,
            cancel_command=self.inspector.cancel_current_task,
            default_output=str(get_default_output_dir() / VIEW_OUTPUT_FOLDER_NAME),
            on_output_changed=self._refresh_ready,
            secondary_text=EXPORT_BUTTON_TEXT,
            secondary_command=self._export_checked,
        )
        self.action_bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.btn_open_map = self.action_bar.btn_primary
        self.btn_export = self.action_bar.btn_secondary
        self.inspector.add_status_listener(self.action_bar.apply_task_status)

    # -------------------------------------------------------------------------
    # 加入圖資
    # -------------------------------------------------------------------------
    def _handle_add_files(self) -> None:
        files = filedialog.askopenfilenames(title="選取要看的圖資", filetypes=[VIEW_FILETYPES, ("所有檔案", "*.*")])
        if files:
            self.add_paths([Path(f) for f in files])

    def _handle_add_folder(self) -> None:
        folder = filedialog.askdirectory(title="選取包含圖資的資料夾")
        if folder:
            self.add_paths([Path(folder)])

    def _add_dropped_paths(self, paths: List[Path]) -> None:
        self.add_paths(paths)

    def add_paths(self, paths: List[Path], select_opened: bool = False) -> int:
        """加入檔案與資料夾 (資料夾含子資料夾)，只收 VIEW_EXTENSIONS，回傳新增的個數；類型與座標系在背景判別。

        單一檔案的副檔名不支援時提示並略過 (附屬檔不提示)；DWG／DGN 另外提示先另存 DXF。
        select_opened (點兩下開檔)：選取第一個加入的檔並捲到那一列 (判別完才選)；都已經在清單裡時選它。"""
        files: List[Path] = []
        unsupported: List[Path] = []
        cad_count = 0
        for path in paths:
            if not path.is_dir():
                suffix = path.suffix.lower()
                if suffix in VIEW_EXTENSIONS or suffix in TABLE_EXTENSIONS or is_unsupported_cad(path):
                    files.append(path)
                elif suffix not in SIDECAR_EXTENSIONS:
                    unsupported.append(path)
                continue
            found = not_table = no_world = 0
            for f in sorted(path.rglob("*")):
                if not f.is_file():
                    continue
                suffix = f.suffix.lower()
                if suffix in geo_io.IMAGE_EXTENSIONS and not geo_io.has_world_file(f):
                    no_world += 1
                elif suffix in VIEW_EXTENSIONS or (suffix in TABLE_EXTENSIONS and csv_points.looks_like_coordinate_table(f)):
                    files.append(f)
                    found += 1
                elif suffix in TABLE_EXTENSIONS:
                    not_table += 1
                elif is_unsupported_cad(f):
                    cad_count += 1
            self.inspector.append_log(f"[資料夾匯入] {path.name} 裡有 {found} 個圖資。\n")
            if not_table:
                self.inspector.append_log(f"[資料夾匯入] 另有 {not_table} 個 CSV／TXT 不是座標表，已略過。\n")
            if no_world:
                self.inspector.append_log(f"[資料夾匯入] 另有 {no_world} 個 JPG／PNG 沒有世界檔 (沒有座標)，已略過。\n")
        if cad_count:
            self.inspector.append_log(f"[資料夾匯入] 另有 {cad_count} 個 DWG／DGN 無法直接讀取，請先另存成 DXF。\n")
        if unsupported:
            shown = [p.name for p in unsupported[:MAX_LISTED_UNKNOWN]]
            if len(unsupported) > len(shown):
                shown.append(f"…等共 {len(unsupported)} 個檔")
            message = "以下檔案不是圖資，已略過：\n" + "\n".join(shown) + f"\n\n{UNSUPPORTED_HINT}"
            self.inspector.append_log(f"[清單] {message}\n")
            messagebox.showwarning(UNSUPPORTED_TITLE, message)
        return self._add_files(files, select_opened)

    def _add_files(self, paths: List[Path], select_opened: bool = False) -> int:
        kept, cad_warning = validate_supported_files(paths)
        if cad_warning:
            messagebox.showwarning("警告", cad_warning)
        known = {item.path for item in self.items}
        added: List[Path] = []
        already: List[Path] = []
        for p in kept:
            resolved = p.resolve()
            if resolved in known:
                already.append(resolved)
                continue
            known.add(resolved)
            self.items.append(ViewItem(resolved))
            added.append(resolved)
        self._refresh_list()
        if not added:
            if select_opened and already:
                self._select_and_show(next(i for i, item in enumerate(self.items) if item.path in already))
            return 0

        table_dir = self._table_stage_dir() if any(f.suffix.lower() in TABLE_EXTENSIONS for f in added) else None

        def _detect() -> Dict[Path, Detection]:
            return {f: detect_view_file(f, table_dir) for f in added}

        self._run_light("圖資判別", _detect, lambda results: self._detected(results, select_opened))
        return len(added)

    def _table_stage_dir(self) -> Path:
        """座標表的暫存點位放這裡；這次開程式第一次用時清掉上次留下的。"""
        folder = get_workspace_temp_dir() / TABLE_STAGE_FOLDER_NAME
        if not self._table_dir_ready:
            shutil.rmtree(folder, ignore_errors=True)
            self._table_dir_ready = True
        return folder

    def _detected(self, results: Dict[Path, Detection], select_new: bool = False) -> None:
        unknown: List[Path] = []
        first_new: Optional[int] = None
        for item in self.items:
            if item.path not in results:
                continue
            found = results[item.path]
            if found.kind is None:
                unknown.append(item.path)
                continue
            item.kind, item.diagnosis, item.layers, item.data_path = found.kind, found.diagnosis, found.layers, found.data_path
        if unknown:
            self._remove_paths(unknown)
            shown = [f"{p.name}：{results[p].reason}" for p in unknown[:MAX_LISTED_UNKNOWN]]
            if len(unknown) > len(shown):
                shown.append(f"…等共 {len(unknown)} 個檔")
            message = "以下檔案已移出清單：\n" + "\n".join(shown)
            self.inspector.append_log(f"[清單] {message}\n")
            messagebox.showwarning("讀不到的檔案", message)
        for index, item in enumerate(self.items):
            if item.path in results and item.detected:
                first_new = index
                break
        self.inspector.append_log(f"[圖資快覽] 判別完成：{len(results) - len(unknown)} 個圖資。\n")
        if first_new is not None and select_new:
            self._select_and_show(first_new)
        elif self._selected_index is None and first_new is not None:
            self._select_index(first_new)
        else:
            self._refresh_list()
            if self._selected_index is not None:
                self._show_selected()

    def _remove_paths(self, paths: List[Path]) -> None:
        selected = self.selected_item()
        self.items = [item for item in self.items if item.path not in paths]
        self._selected_index = self.items.index(selected) if selected in self.items else None

    # -------------------------------------------------------------------------
    # 清單操作
    # -------------------------------------------------------------------------
    def selected_item(self) -> Optional[ViewItem]:
        index = self._selected_index
        return self.items[index] if index is not None and 0 <= index < len(self.items) else None

    def checked_items(self) -> List[ViewItem]:
        """有勾的圖資 (照清單順序)。"""
        return [item for item in self.items if item.checked]

    def _select_index(self, index: Optional[int]) -> None:
        """選取清單第 index 筆 (從 0 起算)：右欄顯示它的體檢與縮圖，來源座標系選單改成它的設定。"""
        self._selected_index = index if index is not None and 0 <= index < len(self.items) else None
        self._refresh_list()
        self._show_selected()

    def _select_and_show(self, index: int) -> None:
        """選取並捲到那一列 (點兩下開檔用)。"""
        self._select_index(index)
        self.file_list.see(index)

    def _show_selected(self) -> None:
        item = self.selected_item()
        self._show_diagnosis(item.diagnosis if item is not None else None)
        self._refresh_preview()

    def _toggle_item(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.items[index].checked = not self.items[index].checked
            self._refresh_list()

    def _toggle_layer(self, index: int, layer_index: int) -> None:
        if not 0 <= index < len(self.items):
            return
        item = self.items[index]
        if 0 <= layer_index < len(item.layers):
            layer = item.layers[layer_index]
            if layer in item.unchecked_layers:
                item.unchecked_layers.discard(layer)
            else:
                item.unchecked_layers.add(layer)
            self._refresh_list()

    def _set_all_checked(self, checked: bool) -> None:
        for item in self.items:
            item.checked = checked
        self._refresh_list()

    def _handle_source_srs_chosen(self, label: str) -> None:
        """清單下面的選單：改選取那一列的來源座標系。"""
        item = self.selected_item()
        if item is None:
            return
        item.source_srs = view_source_srs_from_label(label)
        self.inspector.append_log(f"[座標系] {item.path.name} 的來源座標系改為：{label}\n")
        self._refresh_list()

    def _handle_remove_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            self.inspector.append_log("[移除] 請先在清單點選要移除的那一列。\n")
            return
        self.items.remove(item)
        self.inspector.append_log(f"[移除] 已自清單移除: {item.path.name}\n")
        self._select_index(None)

    def _handle_clear(self) -> None:
        self.items = []
        self.inspector.append_log("[清單] 圖資清單已清空。\n")
        self._select_index(None)

    def _refresh_list(self) -> None:
        """清單、來源座標系選單與統計跟著目前的資料重畫。"""
        rows = []
        for item in self.items:
            children = tuple(
                CheckRow(layer, checked=layer not in item.unchecked_layers) for layer in item.layers
            )
            details = (item.kind or "", item.original_srs_text(), item.source_srs_text())
            rows.append(CheckRow(item.path.name, details, item.checked, children))
        self.file_list.set_rows(rows, self._selected_index)

        item = self.selected_item()
        self.opt_source_srs.set(view_source_srs_label(item.source_srs if item is not None else None))
        self.opt_source_srs.configure(state="normal" if item is not None else "disabled")
        selected = f"，已選取: {item.path.name}" if item is not None else ""
        hint = "，點一列可選取" if self.items and item is None else ""
        self.lbl_file_count.configure(text=f"共 {len(self.items)} 個檔案{selected}{hint}")
        self._refresh_ready()

    # -------------------------------------------------------------------------
    # 右欄縮圖：選取那一列的圖資 (只畫它自己，不需要對齊座標)
    # -------------------------------------------------------------------------
    def _refresh_preview(self) -> None:
        item = self.selected_item()
        source = item.read_path if item is not None and item.kind is not None else None
        if source == self._preview_request:
            return
        self._preview_request = source
        self._preview_seq += 1
        seq = self._preview_seq
        if source is None or item is None or item.kind is None:
            self._set_thumbnail(None)
            return
        out_png = get_workspace_temp_dir() / f"preview_{seq}_{TOOL_KEY}.png"
        style = _PREVIEW_STYLE[item.kind]
        layers = item.thumbnail_layers()

        def _render() -> Tuple[Optional[Path], str]:
            try:
                return render_clip_thumbnail(out_png, style=style, source=source, layers=layers), ""
            except Exception as err:
                return None, str(err)

        def _rendered(payload: Tuple[Optional[Path], str]) -> None:
            path, error = payload
            if seq != self._preview_seq:
                if path is not None:
                    path.unlink(missing_ok=True)
                return
            if path is None:
                self.inspector.append_log(f"[預覽] 縮圖產生失敗：{error}\n")
            self._set_thumbnail(path, source.name)

        self._run_light("預覽縮圖", _render, _rendered)

    def _set_thumbnail(self, path: Optional[Path], caption: str = "") -> None:
        previous = self._last_thumbnail
        self._last_thumbnail = path
        self._show_preview_image(path, caption)
        if previous is not None and previous != path:
            previous.unlink(missing_ok=True)

    # -------------------------------------------------------------------------
    # 瀏覽器地圖：勾選的圖資各自一層，依各列的來源座標系疊上國土測繪底圖
    # -------------------------------------------------------------------------
    def _map_plan(self) -> Tuple[List[MapSource], List[Tuple[str, str]]]:
        """勾選的圖資 → (要放上地圖的, [(檔名, 跳過的原因)])，照清單順序。"""
        sources: List[MapSource] = []
        skipped: List[Tuple[str, str]] = []
        for item in self.checked_items():
            source, reason = item.map_source()
            if source is None:
                skipped.append((item.path.name, reason))
            else:
                sources.append(source)
        return sources, skipped

    def _readiness(self) -> Tuple[bool, str]:
        if not self.items:
            return False, "還沒加入圖資"
        checked = self.checked_items()
        if not checked:
            return False, "還沒勾選要看的圖資"
        if any(not item.detected for item in checked):
            return False, "正在判別圖資"
        sources, skipped = self._map_plan()
        if not sources:
            return False, f"勾選的圖資都放不上地圖（{_reasons_text(skipped)}）"
        return True, f"可以開地圖：{len(sources)} 個圖資{_skip_note(skipped)}"

    def _refresh_ready(self) -> None:
        if not hasattr(self, "action_bar"):
            return
        ok, text = self._readiness()
        self.action_bar.set_ready(text, ok)
        ok, text = self._export_readiness()
        self.action_bar.set_secondary_ready(ok)
        self.lbl_export_status.configure(
            text=text, text_color=T["ok"] if ok else T["warn"], image=icon("check" if ok else "warning", role="ok" if ok else "warn")
        )

    # -------------------------------------------------------------------------
    # 另存：勾選的向量圖資照各列的來源座標系，存成卡片 2 選的格式 (SHP 轉到選的座標系與編碼；KML／KMZ 固定 WGS84)
    # -------------------------------------------------------------------------
    def _export_plan(self) -> Tuple[List[ExportSource], List[Tuple[str, str]]]:
        """勾選的向量圖資 → (要存的, [(檔名, 跳過的原因)])，照清單順序；DEM／正射不算在內。"""
        sources: List[ExportSource] = []
        skipped: List[Tuple[str, str]] = []
        for item in self.checked_items():
            if item.kind not in VECTOR_KINDS:
                continue
            source, reason = item.export_source()
            if source is None:
                skipped.append((item.path.name, reason))
            else:
                sources.append(source)
        return sources, skipped

    def _export_readiness(self) -> Tuple[bool, str]:
        if not self.items:
            return False, "還沒加入圖資"
        checked = self.checked_items()
        if any(not item.detected for item in checked):
            return False, "正在判別圖資"
        if not any(item.kind in VECTOR_KINDS for item in checked):
            return False, f"還沒勾選向量圖資（{SKIP_NOT_VECTOR}）"
        sources, skipped = self._export_plan()
        if not sources:
            return False, f"勾選的向量圖資都不能另存（{_reasons_text(skipped)}）"
        if not self.action_bar.output_text().strip('"'):
            return False, "還沒指定輸出資料夾（底部「輸出到」）"
        return True, f"可以另存：{len(sources)} 個向量圖資{_skip_note(skipped)}"

    def output_srs_key(self) -> str:
        """卡片 2 選的輸出座標系 (ASSIGNABLE_SOURCE_SRS 的鍵)。"""
        chosen = self.opt_output_srs.get()
        return next((key for label, key in OUTPUT_SRS_CHOICES if label == chosen), OUTPUT_SRS_CHOICES[0][1])

    def output_encoding(self) -> str:
        """卡片 2 選的文字編碼 (UTF-8／CP950)。"""
        chosen = self.opt_encoding.get()
        return next((value for label, value in ENCODING_CHOICES if label == chosen), ENCODING_CHOICES[0][1])

    def _export_checked(self) -> None:
        """底部次要按鈕：把勾選的向量圖層另存成卡片 2 選的格式；完成後列出存了幾個、沒有存的圖層與原因。"""
        ok, text = self._export_readiness()
        if not ok:
            self.inspector.append_log(f"[提示] {text}。\n")
            return
        export_format = self.export_format()
        label = f"另存 {export_format}"
        sources, skipped = self._export_plan()
        for name, reason in skipped:
            self.inspector.append_log(f"[{label}] {name} 跳過：{reason}。\n")
        out_dir = Path(self.action_bar.output_text().strip('"'))
        closed = bool(self.chk_closed_polygons.get())

        if export_format == FORMAT_SHP:
            target_srs = self.output_srs_key()
            encoding = self.output_encoding()
            settings = f"座標系：{short_srs_name(target_srs)}　文字編碼：{encoding}"

            def _task() -> QuickViewExport:
                return export_to_shapefiles(
                    sources,
                    out_dir,
                    target_srs=target_srs,
                    encoding=encoding,
                    closed_lines_as_polygons=closed,
                    progress_callback=self.inspector.set_progress,
                    text_callback=self.inspector.append_log,
                )
        else:
            settings = f"座標系：{KML_SRS_LABEL}　文字編碼：{KML_ENCODING}"

            def _task() -> QuickViewExport:
                return export_to_kml(
                    sources,
                    out_dir,
                    kmz=export_format == FORMAT_KMZ,
                    closed_lines_as_polygons=closed,
                    progress_callback=self.inspector.set_progress,
                    text_callback=self.inspector.append_log,
                )

        def _done(result: QuickViewExport) -> None:
            lines = [
                f"已存成 {len(result.output_files)} 個 {export_format}。",
                settings,
                f"成果目錄：{result.output_dir}",
            ]
            not_saved = [f"{name}：{reason}" for name, reason in skipped] + result.skipped
            if not not_saved:
                messagebox.showinfo(f"{label} 完成", "\n".join(lines))
                return
            shown = not_saved[:MAX_LISTED_UNKNOWN]
            if len(not_saved) > len(shown):
                shown.append(f"…等共 {len(not_saved)} 個圖層 (原因請看右欄日誌)")
            messagebox.showwarning(f"{label} 完成 (有圖層沒有存)", "\n".join([*lines, "", "以下圖層沒有存：", *shown]))

        self.inspector.run_task(label, _task, on_success=_done, error_title=f"{label} 失敗")

    def _open_browser_map(self) -> None:
        """底部主要按鈕與右欄「瀏覽器套疊航照底圖」：產生地圖並開啟；勾選、來源座標系與檔案都沒變就直接開上次的。"""
        ok, text = self._readiness()
        if not ok:
            self.inspector.append_log(f"[提示] {text}。\n")
            return
        sources, skipped = self._map_plan()
        for name, reason in skipped:
            self.inspector.append_log(f"[地圖預覽] {name} 跳過：{reason}。\n")
        key = tuple(
            (_file_key(source.path), source.source_srs, source.cad_layers, source.data_layers) for source in sources
        )
        html = self._shown_preview_html
        if key == self._map_key and html is not None and html.exists():
            open_preview_in_browser(html)
            return
        out_html = get_workspace_temp_dir() / f"preview_map_{TOOL_KEY}.html"

        def _task() -> QuickViewMap:
            return build_quick_view_map(out_html, sources, text_callback=self.inspector.append_log)

        def _done(result: QuickViewMap) -> None:
            self._map_key = key
            self._show_preview_html(result.html_path)
            open_preview_in_browser(result.html_path)
            if result.skipped:
                shown = result.skipped[:MAX_LISTED_UNKNOWN]
                if len(result.skipped) > len(shown):
                    shown.append(f"…等共 {len(result.skipped)} 個圖層")
                messagebox.showwarning("有圖層沒有放上地圖", "以下圖層沒有放上地圖：\n" + "\n".join(shown))

        self.inspector.run_task("產生瀏覽器地圖", _task, on_success=_done, error_title="瀏覽器地圖產生失敗")


def detect_view_file(path: Path, table_dir: Optional[Path]) -> Detection:
    """背景判別一個檔：類型、座標系、DXF 的 CAD 圖層 (或多圖層檔裡有圖徵的圖層，只有一個時不列)；
    座標表先轉成 table_dir 裡的暫存點位。讀不到時 kind 是 None 並寫原因。"""
    if path.suffix.lower() in TABLE_EXTENSIONS:
        if table_dir is None or not csv_points.looks_like_coordinate_table(path):
            return Detection(None, reason=NOT_TABLE_TEXT)
        staged = table_dir / uuid.uuid4().hex[:8] / f"{path.stem}.gpkg"
        try:
            csv_points.table_to_raw_points(path, staged, keep_coordinate_columns=True)
            return Detection(KIND_TABLE, sniff_crs(staged), data_path=staged)
        except Exception as err:
            logger.debug("座標表 %s 轉點位失敗: %s", path, err)
            return Detection(None, reason=f"讀不出點位 ({err})")
    try:
        kind = kind_text(path, detect_track_type(path))
    except Exception:
        kind = None
    if kind is None:
        if path.suffix.lower() in geo_io.IMAGE_EXTENSIONS:
            return Detection(None, reason=f"{geo_io.NO_WORLD_FILE_REASON}，不能疊圖")
        return Detection(None)
    try:
        diagnosis = sniff_crs(path)
    except Exception as err:
        # 連範圍都讀不到的檔看不了圖 (壞掉的 .tif 依副檔名會被判成正射，要在這裡擋)
        logger.debug("讀 %s 的座標系失敗: %s", path, err)
        return Detection(None)
    layers: Tuple[str, ...] = ()
    try:
        if kind == KIND_CAD:
            layers = tuple(list_dxf_layers(path))
        elif kind == KIND_VECTOR:
            data_layers = tuple(geo_io.list_data_layers(path))
            layers = data_layers if len(data_layers) > 1 else ()
    except Exception as err:
        logger.debug("讀 %s 的圖層略過: %s", path, err)
    return Detection(kind, diagnosis, layers)


def _reasons_text(skipped: List[Tuple[str, str]]) -> str:
    """跳過的原因 (不重複，照出現順序) 用頓號接起來。"""
    return "、".join(dict.fromkeys(reason for _, reason in skipped))


def _skip_note(skipped: List[Tuple[str, str]]) -> str:
    """現況後面的括號：全部都是需指派時寫明，否則寫會跳過幾個；沒有跳過的回傳空字串。"""
    if not skipped:
        return ""
    needs = sum(1 for _, reason in skipped if reason == SKIP_NEEDS_ASSIGN)
    return f"（{needs} 個需指派來源座標系，會跳過）" if needs == len(skipped) else f"（{len(skipped)} 個會跳過）"


def _file_key(path: Path) -> Tuple[str, Optional[int]]:
    """(路徑, 修改時間)：判斷地圖要不要重做用。"""
    try:
        return str(path), path.stat().st_mtime_ns
    except OSError:
        return str(path), None
