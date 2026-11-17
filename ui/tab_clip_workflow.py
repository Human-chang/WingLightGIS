"""裁切工具頁：DEM／DSM、正射影像、SHP 各一頁 (ui/tab_clip_workflow.py)

照 WingLight 規範的「側欄工作台」版面 (側欄在 dashboard_app)：
1. 標題列：工具名稱與一句說明，右邊「無標籤圖資的來源座標系」(套用到邊界與待裁切圖資) 與「輸出座標系」。
2. 中間可捲動的三張編號卡片，右邊是三頁共用的檢視欄 (PanelInspector)：
   - 1 原始檔：多選檔案、加入資料夾、拖放、清單管理、點選即時體檢；每列有勾選框 (預設全勾)，沒勾的開始裁切時跳過。
   - 2 裁切邊界：預設「載入邊界檔」(SHP、DXF、KML／KMZ、GPKG、GeoJSON)，也可「貼座標」；DXF 載入即自動線轉面，
     換圖層或改閉鎖容許自動重做；KML 等有好幾個圖層的檔先用第一個有面的圖層，「圖層」選單換一個就重做；
     極角排序、外推 Buffer；檢視欄「用這個範圍當邊界」可拿原始檔的四角範圍當邊界；執行時邊界還沒建好會先自動建立。
   - 3 輸出選項：DEM／影像解析度 (可輸入數字；影像的 TIF 與 CAD 底圖共用)、等高線 DXF／坡度圖／金字塔/CAD/KMZ 等勾選；
     預設都不勾，清單有適用的圖資才可勾；DEM 頁另有等高線間距 (首曲線 1 m、計曲線 5 m)。
3. 底部動作列 (ui/action_bar.py)：輸出位置、現況、「開始裁切」；所有空間運算都經 PanelInspector.run_task 在背景執行，
   日誌、進度、完成與錯誤對話框都回到主執行緒，執行中「開始裁切」換成「取消」；清單的軌道判別與點選體檢也在背景做。
4. 預覽 (core/clip_preview.py)：右欄縮圖是「原始檔 (清單點選的那個，預設第一個有勾的)＋邊界紅框」，原始檔、邊界或
   來源座標系一變就在背景重畫；右欄「瀏覽器套疊航照底圖」按下去才產生地圖 (原始／裁切後／裁切邊界三層可勾)。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple, Union

import customtkinter as ctk

from ui.theme_constants import (
    FONT_FAMILY,
    FONT_FAMILY_MONO,
    FONT_SIZE_BODY,
    FONT_SIZE_CAPTION,
    FONT_SIZE_CARD_TITLE,
    FONT_SIZE_MONO,
    FONT_SIZE_PAGE_TITLE,
    RADIUS_LG,
)
from core.boundary_builder import (
    DEFAULT_SNAP_TOLERANCE_M,
    auto_close_ring,
    calculate_shoelace_area,
    create_boundary_from_points,
    dxf_lines_to_polygon_boundary,
    generate_buffer,
    import_boundary_file,
    list_dxf_layers,
    parse_coordinate_text,
    sort_points_by_polar_angle,
)
from core.boundary_builder import BoundaryBuildResult
from core.clip_master import BatchClipEngine, ClipJobResult, TrackType, detect_track_type
from core.clip_preview import (
    STYLE_DEM,
    STYLE_IMAGE,
    STYLE_VECTOR,
    ClipPreviewMap,
    build_clip_preview_map,
    render_clip_thumbnail,
)
from core import geo_io
from core.crs_doctor import CrsDiagnosisResult, assigned_srs_for_epsg, raster_pixel_size_m, sniff_crs
from core.dem_products import (
    DEFAULT_CONTOUR_INTERVAL,
    DEFAULT_INDEX_INTERVAL,
    contour_output_path,
    generate_contour_dxf,
    generate_slope_percent,
    slope_output_path,
    validate_contour_intervals,
)
from core.env_runner import CommandCancelledError, get_workspace_temp_dir, run_in_background
from core.preview_engine import (
    export_to_kmz,
    generate_3d_hillshade_preview,
    open_preview_in_browser,
)
from ui import drag_drop
from ui.file_guards import is_unsupported_cad, validate_supported_files
from ui.output_paths import default_clip_output_dir
from ui.action_bar import ActionBar
from ui.check_list import CheckList, CheckRow
from ui.icons import IconButton
from ui.panel_inspector import INSPECTOR_WIDTH, PREVIEW_TITLE_CLIP, PanelInspector, format_extent_corners
from ui.segmented import SegmentedControl
from ui.theme_constants import BTN_SECONDARY, CARD_FG_COLOR, STATUS_OK, T
from ui.srs_options import (
    AUTO_SOURCE_SRS_LABEL,
    SOURCE_SRS_HINT,
    assigned_srs_from_label,
    output_srs_labels,
    source_srs_labels,
    target_srs_from_label,
)

logger = logging.getLogger("ui.tab_clip_workflow")

EXECUTE_BUTTON_TEXT: str = "開始裁切"
# 卡片標題 (規範 Panel：編號圓點＋名詞)
CARD_TITLE_FILES: str = "原始檔"
CARD_TITLE_BOUNDARY: str = "裁切邊界"
CARD_TITLE_OUTPUT: str = "輸出選項"
# 原始檔清單的兩欄表頭；還沒判別完類型的檔，第二欄先寫「判別中」
FILE_LIST_TITLE: str = "檔名"
FILE_LIST_DETAIL_TITLE: str = "所在資料夾"
FILE_LIST_DETAIL_WIDTH: int = 140
DETECTING_TEXT: str = "判別中"
# 標題列兩個座標系選單的寬度 (px)
HEADER_MENU_WIDTH: int = 290
# 卡片標題列與清單下方的小按鈕寬度 (文字較長時會自動變寬)
SMALL_BUTTON_WIDTH: int = 80
KEEP_RESOLUTION_LABEL: str = "維持原圖解析度"

# 解析度下拉的預設選項：DEM 是公尺、正射影像是公分 (影像一般講幾公分)；也可直接輸入其他數字。
# DEM 預設 1 m，正射影像預設維持原圖
DEM_RESOLUTION_PRESETS: Tuple[str, ...] = (KEEP_RESOLUTION_LABEL, "0.1", "0.2", "0.5", "1", "2", "5", "10")
DEFAULT_DEM_RESOLUTION: str = "1"
ORTHO_RESOLUTION_PRESETS: Tuple[str, ...] = (KEEP_RESOLUTION_LABEL, "5", "10", "20", "25", "50", "100")
DEFAULT_ORTHO_RESOLUTION: str = KEEP_RESOLUTION_LABEL
# 各頁解析度欄的單位：(單位名稱, 原圖說明裡寫的單位, 1 單位是幾公尺)
RESOLUTION_UNITS: Dict[str, Tuple[str, str, float]] = {"dem": ("公尺", " m", 1.0), "ortho": ("公分", " 公分", 0.01)}
ORTHO_RESOLUTION_NOTE: str = "改成較大的數字可縮小檔案 (座標位置不變)；裁出的影像與 AutoCAD 底圖 JPG 都用這個解析度。"


def parse_resolution(text: str, meters_per_unit: float = 1.0) -> Optional[float]:
    """把解析度欄位的文字換成公尺數：沒寫單位時照這一頁的單位 (meters_per_unit；DEM 公尺、正射 0.01＝公分)，
    寫了「公分」「cm」「公尺」「m」照寫的。維持原圖回傳 None，不是正數時拋 ValueError。"""
    cleaned = text.strip()
    if cleaned == KEEP_RESOLUTION_LABEL:
        return None
    scale = meters_per_unit
    for suffix, suffix_scale in (("公分", 0.01), ("cm", 0.01), ("CM", 0.01), ("公尺", 1.0), ("m", 1.0), ("M", 1.0)):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            scale = suffix_scale
            break
    value = float(cleaned)
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"解析度必須大於 0: {text}")
    return value * scale


def format_resolution(value_m: float, meters_per_unit: float) -> str:
    """公尺數換成這一頁的單位，取 3 位有效數字、不用科學記號 (例：0.02634 m 換公分是 "2.63")。"""
    return f"{float(f'{value_m / meters_per_unit:.3g}'):g}"


# 「圖層」選單：還沒載入邊界檔／載入的檔只有一個圖層時
DXF_LAYER_PLACEHOLDER: str = "(載入邊界檔後選)"
SINGLE_LAYER_TEXT: str = "(只有一個圖層)"
BOUNDARY_FILETYPES: Tuple[str, str] = ("邊界檔", "*.shp;*.dxf;*.kml;*.kmz;*.gpkg;*.geojson")
# 類型不符的檔案移出清單時，提示最多列出幾個
MAX_LISTED_WRONG_KIND: int = 5
# 勾選項至少要清單裡有幾個檔才可勾 (沒列的一律可勾)
OPTION_MIN_FILES: Dict[str, int] = {
    "contour": 1, "slope": 1, "3d": 1, "mosaic": 2, "pyramids": 1, "cad_bg": 1, "excel": 1, "whole": 1,
}

# 卡片 2 邊界來源切換鈕的兩個選項 (第一個是預設)
BOUNDARY_MODE_FILE: str = "載入邊界檔"
BOUNDARY_MODE_PASTE: str = "貼座標"
BOUNDARY_MODES: Tuple[str, ...] = (BOUNDARY_MODE_FILE, BOUNDARY_MODE_PASTE)

@dataclass(frozen=True)
class ClipKind:
    """一個裁切工具的類型：只收一種圖資，各有自己的勾選項 (key, 文字)；preview_style 是預覽的畫法。

    tab_title 是側欄與標題列顯示的名稱，description 是標題下面的一句說明，icon_name 是側欄的線條圖示。
    """

    key: str
    tab_title: str
    track: TrackType
    noun: str
    extensions: FrozenSet[str]
    filetypes: Tuple[str, str]
    options: Tuple[Tuple[str, str], ...]
    preview_style: str
    options_hint: str = ""
    description: str = ""
    icon_name: str = "file"


CLIP_KIND_DEM = ClipKind(
    key="dem",
    tab_title="DEM／DSM 裁切",
    track=TrackType.TERRAIN_DEM,
    preview_style=STYLE_DEM,
    noun="DEM／DSM",
    extensions=frozenset({".tif", ".tiff", ".dem", ".asc", ".img", ".vrt"}),
    filetypes=("DEM／DSM 網格", "*.tif;*.tiff;*.dem;*.asc;*.img;*.vrt"),
    options=(
        ("contour", "等高線 DXF (首曲線／計曲線兩圖層)"),
        ("slope", "坡度圖 (%)"),
        ("3d", "產出 3D 山體陰影縮圖"),
        ("mosaic", "多幅 DEM 先拼接成單一成果 (dem_mosaic.tif)"),
    ),
    options_hint="清單裡有 DEM 才可勾；拼接要 2 幅以上。等高線與坡度圖由裁切後的 DEM 產生，放在成果旁邊",
    description="把地形網格裁到邊界範圍，可以一起出等高線和坡度圖。",
    icon_name="dem",
)
CLIP_KIND_ORTHO = ClipKind(
    key="ortho",
    tab_title="正射影像裁切",
    track=TrackType.ORTHO_AERIAL,
    preview_style=STYLE_IMAGE,
    noun="正射影像",
    extensions=frozenset({".tif", ".tiff", ".img", ".vrt", ".jpg", ".jpeg", ".png"}),
    filetypes=("正射影像 (TIF、附世界檔的 JPG／PNG)", "*.tif;*.tiff;*.img;*.vrt;*.jpg;*.jpeg;*.png"),
    options=(
        ("pyramids", "建立金字塔 (.ovr 秒開)"),
        ("cad_bg", "導出 AutoCAD 底圖 (.jpg+.jgw)"),
    ),
    options_hint="清單裡有正射影像才可勾",
    description="把正射影像或附世界檔的 JPG、PNG 裁到邊界範圍，可以改解析度、輸出 AutoCAD 底圖。",
    icon_name="image",
)
CLIP_KIND_SHP = ClipKind(
    key="shp",
    tab_title="SHP 裁切",
    track=TrackType.VECTOR_CADASTRE,
    preview_style=STYLE_VECTOR,
    noun="SHP 向量圖",
    extensions=frozenset({".shp", ".gpkg", ".geojson", ".dxf"}),
    filetypes=("向量圖資", "*.shp;*.gpkg;*.geojson;*.dxf"),
    options=(
        ("whole", "整筆保留 (碰到範圍的圖徵整筆選出，不切開；用地清冊用)"),
        ("excel", "屬性輸出 Excel (全部屬性＋面積＋合計)"),
        ("kmz", "同步產生 Google Earth KMZ"),
    ),
    options_hint=(
        "清單裡有向量圖才可勾整筆保留與 Excel；Excel 與成果 SHP 同名 (.xlsx)，欄名是完整的原欄位名。"
        "整筆保留的成果是 檔名_whole.shp，Excel 同時列整筆面積與範圍內面積"
    ),
    description="把 SHP 圖資裁到邊界範圍並重算面積，也可以整筆選出；可以輸出屬性 Excel。",
    icon_name="shape",
)
CLIP_KINDS: Tuple[ClipKind, ...] = (CLIP_KIND_DEM, CLIP_KIND_ORTHO, CLIP_KIND_SHP)


def kind_for_track(track_value: Optional[str]) -> Optional[ClipKind]:
    """圖資軌道 (TrackType 的值) 對應到哪一個裁切分頁；無法判別時回傳 None。"""
    return next((kind for kind in CLIP_KINDS if kind.track.value == track_value), None)


def folder_label(path: Path) -> str:
    """原始檔清單「所在資料夾」欄：上一層資料夾的名稱 (在磁碟根目錄時寫磁碟代號)。"""
    parent = path.parent
    return parent.name or str(parent)


def _file_key(path: Optional[Path]) -> Optional[Tuple[str, Optional[int]]]:
    """(路徑, 修改時間) — 判斷預覽要不要重做用；同名的邊界檔重建後修改時間會變。"""
    if path is None:
        return None
    try:
        return str(path), path.stat().st_mtime_ns
    except OSError:
        return str(path), None


@dataclass
class BatchClipOutcome:
    """批次裁切加上收尾 (等高線、坡度圖、3D 縮圖、KMZ) 的整體成果，由背景執行緒帶回主執行緒。"""

    results: List[ClipJobResult]
    preview_image: Optional[Path] = None
    preview_vector: Optional[Path] = None
    kmz_path: Optional[Path] = None
    contour_files: List[Path] = field(default_factory=list)
    slope_files: List[Path] = field(default_factory=list)
    # 收尾步驟失敗的說明 (裁切成果本身不受影響)
    warnings: List[str] = field(default_factory=list)
    # 執行時才自動建立的裁切邊界 (邊界建立結果, 體檢)；原本就有邊界時為 None
    built_boundary: Optional[Tuple[BoundaryBuildResult, CrsDiagnosisResult]] = None


class TabClipWorkflow(ctk.CTkFrame):
    """一個裁切工具頁 (DEM／正射／SHP 各一個實例)：標題列、三張編號卡片、底部動作列都是這一頁自己的。

    右側檢視欄 (PanelInspector) 是三頁共用的：它是主區的子元件，切到這一頁時 activate() 把它放進這一頁的
    inspector_slot (grid 的 in_ 參數)。要給檢視欄的東西 (體檢、預覽圖、預覽地圖) 都先記在這一頁，
    只有目前顯示的這一頁才真的送出；切換工具時由 activate() 把這一頁記住的內容放回去。
    """

    def __init__(
        self,
        master: Any,
        inspector: PanelInspector,
        kind: ClipKind = CLIP_KIND_DEM,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("fg_color", T["bg"])
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self.kind = kind
        self.inspector = inspector
        self.is_active: bool = False
        # 這一頁要給右欄的內容：(體檢, 面積 m², 面積 ha, 可否當邊界)、預覽圖與圖說、預覽地圖
        self._shown_diagnosis: Optional[Tuple[CrsDiagnosisResult, Optional[float], Optional[float], bool]] = None
        self._shown_preview_image: Optional[Path] = None
        self._shown_preview_caption: str = ""
        self._shown_preview_html: Optional[Path] = None
        # 預覽縮圖：最近一次要求的 (原始檔, 邊界, 指派座標系)、序號 (只採用最後一次的結果)、目前這張縮圖檔
        self._preview_request: Optional[Tuple[Any, ...]] = None
        self._preview_seq: int = 0
        self._last_thumbnail: Optional[Path] = None
        # 瀏覽器地圖：上次產生時的 (原始檔, 邊界, 裁切成果, 指派座標系)，沒變就直接開上次的
        self._map_key: Optional[Tuple[Any, ...]] = None
        # 最近一次批次裁切成功的 (原始檔, 成果檔)；換了邊界就清掉 (舊成果和新邊界對不上)
        self._clip_outputs: List[Tuple[Path, Path]] = []
        self.master_boundary_gpkg: Optional[Path] = None
        # 外推前的原始邊界：每次外推都從它算起，重複按不會越推越大
        self._base_boundary_gpkg: Optional[Path] = None
        self.selected_input_files: List[Path] = []
        self._track_cache: Dict[Path, str] = {}
        # 原圖一個像素的大小 (公尺, 是否估計值)：DEM 與正射頁在背景判別時讀
        self._pixel_size_cache: Dict[Path, Tuple[float, bool]] = {}
        self._selected_file_index: Optional[int] = None
        # 清單裡沒勾的檔 (開始裁切時跳過)；新加入的檔預設勾著，所以只記沒勾的
        self._unchecked_files: Set[Path] = set()
        # 進行中的輕量背景查詢 (軌道判別、體檢、DXF 圖層清單) 件數
        self._light_pending: int = 0
        # DEM 以雙線性重取樣；輸出座標系由標題列的選單決定
        self._dem_resampling: str = "bilinear"
        # 「圖層」選單目前列的是哪個邊界檔的圖層 (DXF 的 CAD 圖層，或 KML 的資料夾等檔案裡的圖層)；
        # 最近一次線轉面用的 (DXF 路徑, 圖層, 閉鎖容許)
        self._dxf_layers_source: Optional[Path] = None
        self._last_dxf_request: Optional[Tuple[str, Optional[str], float]] = None

        self.font_page_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_PAGE_TITLE, weight="bold")
        self.font_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CARD_TITLE, weight="bold")
        self.font_caption = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION)
        self.font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self.font_btn = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self.font_mono = ctk.CTkFont(family=FONT_FAMILY_MONO, size=FONT_SIZE_MONO)

        # 版面：第 0 列標題列；第 1 列左邊是可捲動的卡片區、右邊是檢視欄的位置；第 2 列底部動作列 (不隨卡片捲動)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self.scroll_area = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.scroll_area.grid(row=1, column=0, sticky="nsew", padx=(16, 0), pady=(0, 16))
        self.scroll_area.grid_columnconfigure(0, weight=1)
        # 捲動區裡的卡片要明確給 bg_color=T["bg"]：CustomTkinter 從透明捲動區偵測到的是當下的單一顏色，
        # 深色切淺色後卡片圓角會留著深色
        self.inspector_slot = ctk.CTkFrame(self, fg_color="transparent", width=INSPECTOR_WIDTH, corner_radius=0)
        self.inspector_slot.grid(row=1, column=1, sticky="nsew", padx=(8, 24), pady=(0, 16))
        self.inspector_slot.grid_rowconfigure(0, weight=1)
        self.inspector_slot.grid_columnconfigure(0, weight=1)

        self._build_files_card()
        self._build_boundary_card()
        self._build_output_card()
        self._build_action_bar()

    # -------------------------------------------------------------------------
    # 右欄 (三頁共用)：先記在這一頁，目前顯示的這一頁才送出
    # -------------------------------------------------------------------------
    def activate(self) -> None:
        """切換到這一頁：檢視欄搬進這一頁、清空後放回這一頁記住的體檢、預覽圖與預覽地圖；
        「用這個範圍當邊界」與瀏覽器地圖鈕交給這一頁。"""
        self.is_active = True
        self.inspector.grid(in_=self.inspector_slot, row=0, column=0, sticky="nsew")
        # 檢視欄比工具頁先建立，要拉到上層才不會被這一頁蓋住
        self.inspector.lift()
        self.inspector.on_use_extent = self._use_extent_as_boundary
        self.inspector.on_open_browser_preview = self._open_browser_map
        self.inspector.set_preview_title(PREVIEW_TITLE_CLIP)
        self.inspector.reset_view()
        if self._shown_diagnosis is not None:
            diag, area_m2, area_ha, can_use_extent = self._shown_diagnosis
            self.inspector.update_spatial_diagnosis(diag, area_m2=area_m2, area_ha=area_ha, can_use_extent=can_use_extent)
        if self._shown_preview_image is not None:
            self.inspector.load_preview_image(self._shown_preview_image, self._shown_preview_caption)
        if self._shown_preview_html is not None:
            self.inspector.set_current_preview_html(self._shown_preview_html)

    def deactivate(self) -> None:
        self.is_active = False

    def set_inspector_visible(self, visible: bool) -> None:
        """視窗太窄時收起檢視欄 (卡片區改用整個寬度)；放寬後放回來。"""
        if visible:
            self.inspector_slot.grid()
            self.scroll_area.grid_configure(padx=(16, 0))
        else:
            self.inspector_slot.grid_remove()
            self.scroll_area.grid_configure(padx=(16, 16))

    def destroy(self) -> None:
        self.inspector.remove_status_listener(self.action_bar.apply_task_status)
        super().destroy()

    def _show_diagnosis(
        self,
        diag: CrsDiagnosisResult,
        *,
        area_m2: Optional[float] = None,
        area_ha: Optional[float] = None,
        can_use_extent: bool = True,
    ) -> None:
        self._shown_diagnosis = (diag, area_m2, area_ha, can_use_extent)
        if self.is_active:
            self.inspector.update_spatial_diagnosis(diag, area_m2=area_m2, area_ha=area_ha, can_use_extent=can_use_extent)

    def _show_preview_image(self, image_path: Optional[Path], caption: str = "") -> None:
        """右欄預覽圖換成 image_path (None 表示清空)。"""
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

    def _boundary_path(self) -> Path:
        """這一頁的裁切邊界 GPKG。"""
        return self._temp_path("master_boundary", ".gpkg")

    def _temp_path(self, stem: str, suffix: str) -> Path:
        """這一頁專用的暫存檔名 (三頁同時有自己的邊界與預覽，檔名不能共用)。"""
        return get_workspace_temp_dir() / f"{stem}_{self.kind.key}{suffix}"

    # -------------------------------------------------------------------------
    # 標題列：工具名稱與說明＋無標籤圖資的來源座標系＋輸出座標系
    # -------------------------------------------------------------------------
    def _build_header(self) -> None:
        """標題列：左邊工具名稱 (page-title) 與一句說明，右邊兩個座標系選單，下面一行來源座標系的說明。"""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(18, 12))
        header.grid_columnconfigure(0, weight=1)
        self.header = header

        self.lbl_page_title = ctk.CTkLabel(header, text=self.kind.tab_title, font=self.font_page_title, anchor="w")
        self.lbl_page_title.grid(row=0, column=0, sticky="sw")
        self.lbl_description = ctk.CTkLabel(
            header, text=self.kind.description, font=self.font_body, text_color=T["muted"], anchor="w", justify="left",
            wraplength=240,
        )
        self.lbl_description.grid(row=1, column=0, sticky="nw", pady=(2, 0))

        # 選單直接放在 bg 底上，填色改用 panel 才分得開 (規範 Button／Input)
        menu_style = {"fg_color": T["panel"], "button_color": T["panel"], "button_hover_color": T["hover"]}
        ctk.CTkLabel(header, text="無標籤圖資的來源座標系", font=self.font_caption, text_color=T["muted"], anchor="w").grid(
            row=0, column=1, sticky="sw", padx=(16, 0)
        )
        # 換來源座標系時，無標籤原始檔與邊界的相對位置會變，預覽跟著重畫
        self.opt_source_srs = ctk.CTkOptionMenu(
            header,
            values=source_srs_labels(),
            width=HEADER_MENU_WIDTH,
            dynamic_resizing=False,
            command=lambda _choice: self._refresh_preview(),
            **menu_style,
        )
        self.opt_source_srs.grid(row=1, column=1, sticky="nw", padx=(16, 0), pady=(4, 0))
        self.opt_source_srs.set(AUTO_SOURCE_SRS_LABEL)

        ctk.CTkLabel(header, text="輸出座標系", font=self.font_caption, text_color=T["muted"], anchor="w").grid(
            row=0, column=2, sticky="sw", padx=(12, 0)
        )
        self.opt_output_srs = ctk.CTkOptionMenu(
            header, values=output_srs_labels(), width=HEADER_MENU_WIDTH, dynamic_resizing=False, **menu_style
        )
        self.opt_output_srs.grid(row=1, column=2, sticky="nw", padx=(12, 0), pady=(4, 0))
        self.opt_output_srs.set(output_srs_labels()[0])

        self.lbl_srs_hint = ctk.CTkLabel(
            header,
            text=SOURCE_SRS_HINT + "貼上的座標、邊界檔與待裁切圖資都適用。",
            font=self.font_caption,
            text_color=T["muted"],
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self.lbl_srs_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        # 說明文字跟著標題列寬度折行
        header.bind("<Configure>", self._on_header_resize, add="+")

    def _on_header_resize(self, event: Any) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        width = event.width / scaling
        self.lbl_srs_hint.configure(wraplength=max(300, width - 8))
        # 工具說明在兩個座標系選單左邊，可用寬度＝標題列寬度減掉兩個選單
        self.lbl_description.configure(wraplength=max(200, width - 2 * HEADER_MENU_WIDTH - 40))

    # -------------------------------------------------------------------------
    # 編號卡片 (規範 Panel)：編號圓點＋卡片標題，右邊可放這張卡的按鈕
    # -------------------------------------------------------------------------
    def _make_card(self, row: int, number: int, title: str) -> Tuple[ctk.CTkFrame, ctk.CTkFrame]:
        card = ctk.CTkFrame(
            self.scroll_area,
            corner_radius=RADIUS_LG,
            fg_color=CARD_FG_COLOR,
            border_width=1,
            border_color=T["line"],
            bg_color=T["bg"],
        )
        card.grid(row=row, column=0, sticky="ew", padx=(8, 8), pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 10))
        head.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            head,
            text=str(number),
            width=22,
            height=22,
            corner_radius=11,
            fg_color=T["soft"],
            text_color=T["accent"],
            font=ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text=title, font=self.font_title, anchor="w").grid(row=0, column=1, sticky="w", padx=(10, 0))
        return card, head

    # -------------------------------------------------------------------------
    # 卡片 1：原始檔
    # -------------------------------------------------------------------------
    def _build_files_card(self) -> None:
        """卡片 1：原始檔清單 (多選檔案、加入資料夾、拖放、勾選要處理的檔、點選即時體檢)。"""
        card, head = self._make_card(0, 1, CARD_TITLE_FILES)
        self.card_files = card
        ctk.CTkLabel(head, text="也可以直接把檔案拖進來", font=self.font_caption, text_color=T["muted"]).grid(
            row=0, column=2, sticky="e", padx=(8, 8)
        )
        btn_add_files = IconButton(
            head, text="加入檔案", icon_name="plus", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_add_files, **BTN_SECONDARY,
        )
        btn_add_files.grid(row=0, column=3, padx=(0, 8))
        btn_add_dir = IconButton(
            head, text="加入資料夾", icon_name="folder", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_add_folder, **BTN_SECONDARY,
        )
        btn_add_dir.grid(row=0, column=4)

        # 原始檔清單 (規範 List)：勾選框決定要不要處理；點一列選取 (給「移除所選」用)，右欄顯示它的體檢結果
        self.file_list = CheckList(
            card,
            title=FILE_LIST_TITLE,
            columns=((FILE_LIST_DETAIL_TITLE, FILE_LIST_DETAIL_WIDTH),),
            on_select=self._select_file_index,
            on_toggle=self._toggle_file,
            on_toggle_all=self._set_all_files_checked,
        )
        self.file_list.grid(row=1, column=0, sticky="ew", padx=16)
        for target in self.file_list.drop_targets():
            drag_drop.bind_paths(target, self._add_dropped_inputs)

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 14))
        footer.grid_columnconfigure(0, weight=1)
        self.lbl_file_count = ctk.CTkLabel(
            footer,
            text="共 0 個檔案",
            font=self.font_caption,
            text_color=T["muted"],
            anchor="w",
        )
        self.lbl_file_count.grid(row=0, column=0, sticky="w")
        btn_remove_sel = IconButton(
            footer, text="移除所選", icon_name="close", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_remove_selected, **BTN_SECONDARY,
        )
        btn_remove_sel.grid(row=0, column=1, padx=(8, 8))
        btn_clear_all = IconButton(
            footer, text="清空清單", icon_name="trash", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_clear_files, **BTN_SECONDARY,
        )
        btn_clear_all.grid(row=0, column=2)

    # -------------------------------------------------------------------------
    # 卡片 2：裁切邊界
    # -------------------------------------------------------------------------
    def _build_boundary_card(self) -> None:
        """卡片 2：裁切邊界 (預設載入邊界檔，也可貼座標)、環域外推、DXF 閉鎖容許。"""
        card, head = self._make_card(1, 2, CARD_TITLE_BOUNDARY)
        self.card_boundary = card

        # 邊界來源切換：載入外部向量檔案 (預設) vs 手動貼上閉鎖座標 (一次只顯示一種)
        self.seg_boundary_mode = SegmentedControl(head, values=list(BOUNDARY_MODES), command=self._show_boundary_mode)
        self.seg_boundary_mode.grid(row=0, column=3, sticky="e")

        tab_paste = ctk.CTkFrame(card, fg_color="transparent")
        tab_file = ctk.CTkFrame(card, fg_color="transparent")
        self._boundary_mode_frames: Dict[str, ctk.CTkFrame] = {
            BOUNDARY_MODE_PASTE: tab_paste,
            BOUNDARY_MODE_FILE: tab_file,
        }

        # --- 貼座標 ---
        tab_paste.grid_columnconfigure(0, weight=1)
        self.txt_coords = ctk.CTkTextbox(tab_paste, height=85, font=self.font_mono)
        self.txt_coords.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.txt_coords.insert(
            "1.0",
            "# 請在此貼上座標點列 (支援 Excel Tab、逗號、點名或 Y X 欄位倒置):\n"
            "250000.0\t2600000.0\n"
            "250200.0\t2600000.0\n"
            "250200.0\t2600150.0\n"
            "250000.0\t2600150.0\n",
        )

        frame_paste_btns = ctk.CTkFrame(tab_paste, fg_color="transparent")
        frame_paste_btns.grid(row=1, column=0, sticky="ew")
        frame_paste_btns.grid_columnconfigure(3, weight=1)
        IconButton(
            frame_paste_btns, text="自動閉合並產生主邊界", icon_name="shape", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_create_boundary_from_text, **BTN_SECONDARY,
        ).grid(row=0, column=0, padx=(0, 8))
        IconButton(
            frame_paste_btns, text="極角排序修復 (防自相交)", icon_name="refresh", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_polar_sort_text, **BTN_SECONDARY,
        ).grid(row=0, column=1, padx=(0, 8))
        IconButton(
            frame_paste_btns, text="清空", icon_name="trash", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=lambda: self.txt_coords.delete("1.0", "end"), **BTN_SECONDARY,
        ).grid(row=0, column=2)

        self.lbl_paste_area = ctk.CTkLabel(
            tab_paste,
            text="即時面積：尚未構建邊界",
            font=self.font_caption,
            text_color=STATUS_OK,
            anchor="w",
        )
        self.lbl_paste_area.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # --- 載入邊界檔：欄位名稱在輸入框上面 ---
        tab_file.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab_file, text="邊界檔", font=self.font_caption, text_color=T["muted"], anchor="w").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        ctk.CTkLabel(tab_file, text="圖層", font=self.font_caption, text_color=T["muted"], anchor="w").grid(
            row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 4)
        )
        self.entry_bound_file = ctk.CTkEntry(tab_file, placeholder_text="請選擇 SHP、DXF、KML／KMZ 或 GPKG 檔案")
        self.entry_bound_file.grid(row=1, column=0, sticky="ew")
        drag_drop.bind_file_entry(self.entry_bound_file, on_file=self._load_boundary_file)

        # 載入 DXF 會自動用第一個圖層線轉面、KML 等用第一個有面的圖層；換圖層就自動重做
        self.opt_dxf_layer = ctk.CTkOptionMenu(
            tab_file,
            values=[DXF_LAYER_PLACEHOLDER],
            width=150,
            dynamic_resizing=False,
            command=lambda _layer: self._handle_boundary_layer_chosen(),
        )
        self.opt_dxf_layer.grid(row=1, column=1, sticky="w", padx=(8, 0))
        ctk.CTkButton(
            tab_file, text="瀏覽", width=60, font=self.font_btn, command=self._handle_browse_boundary_file, **BTN_SECONDARY
        ).grid(row=1, column=2, padx=(8, 0))

        # 環域外推 (Buffer)
        frame_buf = ctk.CTkFrame(card, fg_color="transparent")
        frame_buf.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 0))
        frame_buf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame_buf, text="環域外推 (公尺)", font=self.font_caption, text_color=T["muted"]).grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )
        self.entry_buffer = ctk.CTkEntry(frame_buf, placeholder_text="0.0 (公尺，如 +20 或 -10；0 還原原始邊界)")
        self.entry_buffer.grid(row=0, column=1, sticky="ew")
        self.entry_buffer.insert(0, "0.0")
        ctk.CTkButton(
            frame_buf, text="外推生成餘裕框", width=SMALL_BUTTON_WIDTH, font=self.font_btn,
            command=self._handle_apply_buffer, **BTN_SECONDARY,
        ).grid(row=0, column=2, padx=(8, 0))

        # DXF 閉鎖容許放在卡片最下方 (只在載入檔案模式顯示)：線段端點缺口不超過這個距離才自動接起來
        self.frame_snap = ctk.CTkFrame(card, fg_color="transparent")
        ctk.CTkLabel(self.frame_snap, text="DXF 閉鎖容許 (公尺)", font=self.font_caption, text_color=T["muted"]).grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )
        self.entry_snap_tolerance = ctk.CTkEntry(self.frame_snap, width=80)
        self.entry_snap_tolerance.grid(row=0, column=1, sticky="w")
        self.entry_snap_tolerance.insert(0, f"{DEFAULT_SNAP_TOLERANCE_M:g}")
        # 改完按 Enter 或離開欄位：數值有變就用新的容許值重新線轉面
        for sequence in ("<Return>", "<FocusOut>"):
            self.entry_snap_tolerance.bind(sequence, lambda _event: self._handle_dxf_lines_to_poly(only_if_changed=True))
        ctk.CTkLabel(
            self.frame_snap,
            text="出現閉鎖錯誤 (缺口太大) 時再調大，改完按 Enter 重新轉換",
            font=self.font_caption,
            text_color=T["muted"],
            anchor="w",
        ).grid(row=0, column=2, padx=(8, 0), sticky="w")

        self._show_boundary_mode(BOUNDARY_MODES[0])

    def _show_boundary_mode(self, mode: str) -> None:
        """卡片 2 只顯示選中的邊界來源 (載入檔案或貼座標)，另一個隱藏但保留內容；閉鎖容許只跟著載入檔案顯示。"""
        if mode not in self._boundary_mode_frames:
            raise KeyError(f"沒有這個邊界來源: {mode}")
        for name, frame in self._boundary_mode_frames.items():
            if name == mode:
                frame.grid(row=1, column=0, sticky="ew", padx=16)
            else:
                frame.grid_remove()
        if mode == BOUNDARY_MODE_FILE:
            self.frame_snap.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 14))
            self._boundary_last_row_pad(False)
        else:
            self.frame_snap.grid_remove()
            self._boundary_last_row_pad(True)
        if self.seg_boundary_mode.get() != mode:
            self.seg_boundary_mode.set(mode)
        self._refresh_ready()

    def _boundary_last_row_pad(self, buffer_is_last: bool) -> None:
        """卡片最後一列下面留 14 px (卡片內距)。"""
        frame_buf = self.entry_buffer.master if hasattr(self, "entry_buffer") else None
        if frame_buf is not None:
            frame_buf.grid_configure(pady=(10, 14 if buffer_is_last else 0))

    # -------------------------------------------------------------------------
    # 卡片 3：輸出選項
    # -------------------------------------------------------------------------
    def _build_output_card(self) -> None:
        """卡片 3：解析度 (DEM／正射)、附加成果勾選、DEM 的等高線間距。輸出位置在底部動作列。"""
        card, head = self._make_card(2, 3, CARD_TITLE_OUTPUT)
        self.card_output = card

        # 目標解析度 (公尺)：DEM 頁與正射頁各有一個，放在卡片標題右邊；可下拉，也可直接輸入數字
        self.combo_dem_res: Optional[ctk.CTkComboBox] = None
        self.combo_ortho_res: Optional[ctk.CTkComboBox] = None
        if self.kind is CLIP_KIND_DEM:
            self.combo_dem_res = self._build_resolution_row(head, "DEM 解析度 (公尺)", DEM_RESOLUTION_PRESETS, DEFAULT_DEM_RESOLUTION)
        elif self.kind is CLIP_KIND_ORTHO:
            self.combo_ortho_res = self._build_resolution_row(head, "影像解析度 (公分)", ORTHO_RESOLUTION_PRESETS, DEFAULT_ORTHO_RESOLUTION)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))
        body.grid_columnconfigure((0, 1), weight=1, uniform="options")
        next_row = 0
        # 解析度說明：依清單裡勾選的檔寫原圖解析度 (_refresh_resolution_hint)
        self.lbl_resolution_hint: Optional[ctk.CTkLabel] = None
        if self.kind is CLIP_KIND_DEM or self.kind is CLIP_KIND_ORTHO:
            self.lbl_resolution_hint = ctk.CTkLabel(
                body, text="", font=self.font_caption, text_color=T["muted"], anchor="w", justify="left", wraplength=520
            )
            self.lbl_resolution_hint.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(0, 8))
            next_row += 1
            self._refresh_resolution_hint()

        # 這一頁的勾選項：預設都不勾，清單裡的檔數夠才可勾 (見 _refresh_option_checkboxes)
        self.option_checks: Dict[str, ctk.CTkCheckBox] = {}
        for index, (key, text) in enumerate(self.kind.options):
            checkbox = ctk.CTkCheckBox(body, text=text, font=self.font_body, checkbox_width=18, checkbox_height=18)
            checkbox.grid(row=next_row + index // 2, column=index % 2, padx=(0, 12), pady=5, sticky="w")
            self.option_checks[key] = checkbox
        next_row += (len(self.kind.options) + 1) // 2

        # 等高線間距：只有 DEM 頁有；勾「等高線 DXF」時才會檢查與使用
        self.entry_contour_interval: Optional[ctk.CTkEntry] = None
        self.entry_index_interval: Optional[ctk.CTkEntry] = None
        if self.kind is CLIP_KIND_DEM:
            frame_contour = ctk.CTkFrame(body, fg_color="transparent")
            frame_contour.grid(row=next_row, column=0, columnspan=2, sticky="w", padx=(26, 0), pady=(2, 4))
            next_row += 1
            ctk.CTkLabel(frame_contour, text="等高線間距：首曲線 (公尺)", font=self.font_caption, text_color=T["muted"]).grid(
                row=0, column=0, padx=(0, 6)
            )
            self.entry_contour_interval = ctk.CTkEntry(frame_contour, width=56)
            self.entry_contour_interval.grid(row=0, column=1, padx=(0, 12))
            self.entry_contour_interval.insert(0, f"{DEFAULT_CONTOUR_INTERVAL:g}")
            ctk.CTkLabel(frame_contour, text="計曲線 (公尺)", font=self.font_caption, text_color=T["muted"]).grid(
                row=0, column=2, padx=(0, 6)
            )
            self.entry_index_interval = ctk.CTkEntry(frame_contour, width=56)
            self.entry_index_interval.grid(row=0, column=3, padx=(0, 12))
            self.entry_index_interval.insert(0, f"{DEFAULT_INDEX_INTERVAL:g}")
            ctk.CTkLabel(frame_contour, text="計曲線要是首曲線的整數倍", font=self.font_caption, text_color=T["muted"]).grid(
                row=0, column=4, sticky="w"
            )

        if self.kind.options_hint:
            ctk.CTkLabel(
                body,
                text=self.kind.options_hint,
                font=self.font_caption,
                text_color=T["muted"],
                anchor="w",
                justify="left",
                wraplength=520,
            ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._refresh_option_checkboxes()

    def _build_resolution_row(self, head: Any, label: str, presets: Tuple[str, ...], default: str) -> ctk.CTkComboBox:
        """卡片 3 標題右邊：解析度標籤＋可下拉也可輸入的選單。"""
        ctk.CTkLabel(head, text=label, font=self.font_caption, text_color=T["muted"]).grid(row=0, column=3, padx=(8, 8), sticky="e")
        combo = ctk.CTkComboBox(head, values=list(presets), width=150, command=lambda _value: self._refresh_ready())
        combo.grid(row=0, column=4, sticky="e")
        combo.set(default)
        # 自己輸入時也要重新檢查 (比原圖細時停用開始裁切)
        combo.bind("<KeyRelease>", self._refresh_ready, add="+")
        return combo

    # -------------------------------------------------------------------------
    # 底部動作列 (不隨卡片捲動)：輸出位置、現況、開始裁切
    # -------------------------------------------------------------------------
    def _build_action_bar(self) -> None:
        # 預設輸出放在暫存區外，清理暫存時不會誤刪成果
        self.action_bar = ActionBar(
            self,
            primary_text=EXECUTE_BUTTON_TEXT,
            primary_command=self._handle_start_batch_clip,
            cancel_command=self.inspector.cancel_current_task,
            default_output=str(default_clip_output_dir()),
            on_output_changed=self._refresh_ready,
        )
        self.action_bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.entry_out_dir = self.action_bar.entry_out_dir
        # 本頁唯一的主要按鈕
        self.btn_execute = self.action_bar.btn_primary
        self.inspector.add_status_listener(self.action_bar.apply_task_status)
        # 貼座標、邊界檔路徑一改就重新判斷能不能開始
        self.txt_coords.bind("<KeyRelease>", self._refresh_ready, add="+")
        self.entry_bound_file.bind("<KeyRelease>", self._refresh_ready, add="+")
        self._refresh_ready()

    # -------------------------------------------------------------------------
    # 現況：條件夠不夠開始裁切 (顯示在底部動作列；不夠時「開始裁切」停用並寫出缺什麼)
    # -------------------------------------------------------------------------
    def _readiness(self) -> Tuple[bool, str]:
        files = self.selected_input_files
        if not files:
            return False, "還沒加入原始檔"
        if any(f not in self._track_cache for f in files):
            return False, "正在判別圖資類型"
        checked = self.checked_input_files()
        if not checked:
            return False, "還沒勾選要處理的檔案"
        problem = self._resolution_problem()
        if problem:
            return False, problem
        if not self._has_boundary_input():
            return False, "還沒選裁切邊界"
        if not self.action_bar.output_text():
            return False, "還沒指定輸出資料夾"
        skipped = len(files) - len(checked)
        note = f"（{skipped} 個沒勾，會跳過）" if skipped else ""
        return True, f"可以開始：{len(checked)} 個檔案{note}"

    def _has_boundary_input(self) -> bool:
        """已經有裁切邊界，或卡片 2 有可以自動建立邊界的輸入 (與 _pending_boundary_builder 的判斷相同)。"""
        if self.master_boundary_gpkg is not None and self.master_boundary_gpkg.exists():
            return True
        if self.seg_boundary_mode.get() == BOUNDARY_MODE_PASTE:
            return bool(self.txt_coords.get("1.0", "end").strip())
        path_text = self.entry_bound_file.get().strip().strip('"')
        return bool(path_text) and Path(path_text).exists()

    def _refresh_ready(self, _event: Any = None) -> None:
        if not hasattr(self, "action_bar"):
            return
        self._refresh_resolution_hint()
        ok, text = self._readiness()
        self.action_bar.set_ready(text, ok)

    # -------------------------------------------------------------------------
    # 解析度：原圖解析度 (勾選的檔，背景判別時讀) 與填的數字
    # -------------------------------------------------------------------------
    def _resolution_combo(self) -> Optional[ctk.CTkComboBox]:
        return self.combo_dem_res if self.combo_dem_res is not None else self.combo_ortho_res

    def _original_resolution(self) -> Optional[Tuple[float, float, bool]]:
        """勾選的檔的原圖解析度 (最細, 最粗, 有沒有估計值)，公尺；一個都還沒讀到時 None。"""
        sizes = [self._pixel_size_cache[f] for f in self.checked_input_files() if f in self._pixel_size_cache]
        if not sizes:
            return None
        values = [size for size, _approx in sizes]
        return min(values), max(values), any(approx for _size, approx in sizes)

    def _resolution_threshold(self) -> Optional[float]:
        """能填的最小解析度 (公尺)：原圖最粗的那個，照說明上寫的數字 (3 位有效數字)，才不會寫 1 卻擋 1。"""
        original = self._original_resolution()
        if original is None:
            return None
        meters_per_unit = RESOLUTION_UNITS[self.kind.key][2]
        return float(format_resolution(original[1], meters_per_unit)) * meters_per_unit

    def _resolution_problem(self) -> Optional[str]:
        """解析度欄填的數字不能用時的原因 (給現況用)；可以用或這一頁沒有解析度時 None。"""
        combo = self._resolution_combo()
        if combo is None:
            return None
        unit_name, _unit_text, meters_per_unit = RESOLUTION_UNITS[self.kind.key]
        try:
            value = parse_resolution(combo.get(), meters_per_unit)
        except ValueError:
            return f"解析度要填大於 0 的數字 ({unit_name})，或選「{KEEP_RESOLUTION_LABEL}」"
        threshold = self._resolution_threshold()
        if value is None or threshold is None or value >= threshold * (1 - 1e-9):
            return None
        return f"解析度不能小於原圖 ({format_resolution(threshold, meters_per_unit)} {unit_name})"

    def _keep_original_if_default_too_fine(self) -> None:
        """解析度還是預設值、原圖卻比它粗 (例如 5 m 的 DEM 配預設的 1 m) 時改成維持原圖，
        免得一放檔就因為預設值不能開始；使用者自己改過的數字不動。"""
        combo = self._resolution_combo()
        default = DEFAULT_DEM_RESOLUTION if self.kind is CLIP_KIND_DEM else DEFAULT_ORTHO_RESOLUTION
        if combo is None or default == KEEP_RESOLUTION_LABEL or combo.get() != default:
            return
        if self._resolution_problem():
            combo.set(KEEP_RESOLUTION_LABEL)
            unit_text = RESOLUTION_UNITS[self.kind.key][1]
            self.inspector.append_log(f"[解析度] 原圖比預設的 {default}{unit_text} 粗，已改成「{KEEP_RESOLUTION_LABEL}」。\n")

    def _refresh_resolution_hint(self) -> None:
        """解析度說明：原圖 1 m；可自行填寫 1 以上的數值 (好幾個檔不同時寫範圍，以最粗的為準)。"""
        if getattr(self, "lbl_resolution_hint", None) is None:
            return
        unit_name, unit_text, meters_per_unit = RESOLUTION_UNITS[self.kind.key]
        original = self._original_resolution()
        threshold = self._resolution_threshold()
        if original is None or threshold is None:
            text = f"可自行填寫數值 ({unit_name})。"
        else:
            finest, coarsest = (format_resolution(v, meters_per_unit) for v in original[:2])
            about = "約 " if original[2] else ""
            span = finest if finest == coarsest else f"{finest}～{coarsest}"
            text = f"原圖 {about}{span}{unit_text}；可自行填寫 {format_resolution(threshold, meters_per_unit)} 以上的數值。"
        if self.kind is CLIP_KIND_ORTHO:
            text += ORTHO_RESOLUTION_NOTE
        if self.lbl_resolution_hint.cget("text") != text:
            self.lbl_resolution_hint.configure(text=text)

    # -------------------------------------------------------------------------
    # 共用小工具
    # -------------------------------------------------------------------------
    def _assigned_srs(self) -> Optional[str]:
        """來源座標系下拉選單目前的指派值；自動判斷時為 None。"""
        return assigned_srs_from_label(self.opt_source_srs.get())

    def _run_light(self, what: str, func: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        """在背景做不需要取消鈕的短查詢 (軌道判別、體檢、圖層清單)，結果回主執行緒；失敗只記日誌。"""
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
    # 預覽：右欄縮圖 (原始檔＋邊界) 與瀏覽器地圖 (原始／裁切後／裁切邊界三層)
    # -------------------------------------------------------------------------
    def _preview_source(self) -> Optional[Path]:
        """要預覽的原始檔：清單點選的那一個，沒點選時是第一個有勾、已判別為這一頁類型的檔 (都沒勾時不管勾選)。"""
        matching = [f for f in self.selected_input_files if self._track_cache.get(f) == self.kind.track.value]
        index = self._selected_file_index
        if index is not None and 0 <= index < len(self.selected_input_files):
            selected = self.selected_input_files[index]
            if selected in matching:
                return selected
        checked = [f for f in matching if f not in self._unchecked_files]
        candidates = checked or matching
        return candidates[0] if candidates else None

    def _current_boundary(self) -> Optional[Path]:
        boundary = self.master_boundary_gpkg
        return boundary if boundary is not None and boundary.exists() else None

    def _refresh_preview(self) -> None:
        """右欄縮圖改成目前的原始檔＋裁切邊界；在背景產生，只採用最後一次要求的結果。內容沒變就不重做。"""
        source = self._preview_source()
        boundary = self._current_boundary()
        assigned_srs = self._assigned_srs()
        request = (_file_key(source), _file_key(boundary), assigned_srs)
        if request == self._preview_request:
            return
        self._preview_request = request
        self._preview_seq += 1
        seq = self._preview_seq
        if source is None and boundary is None:
            self._set_thumbnail(None)
            return

        if source is not None and boundary is not None:
            caption = f"原始：{source.name}　紅框：裁切邊界"
        elif source is not None:
            caption = f"原始：{source.name} (還沒有裁切邊界)"
        else:
            caption = "紅框：裁切邊界 (還沒投入原始檔)"
        out_png = self._temp_path(f"preview_{seq}", ".png")
        style = self.kind.preview_style

        def _render() -> Tuple[Optional[Path], str]:
            try:
                return render_clip_thumbnail(
                    out_png, style=style, source=source, boundary=boundary, assigned_srs=assigned_srs
                ), ""
            except Exception as err:
                return None, str(err)

        def _rendered(payload: Tuple[Optional[Path], str]) -> None:
            path, error = payload
            if seq != self._preview_seq:
                # 已經有更新的要求，這張不用了
                if path is not None:
                    path.unlink(missing_ok=True)
                return
            if path is None:
                self.inspector.append_log(f"[預覽] 縮圖產生失敗：{error}\n")
            self._set_thumbnail(path, caption)

        self._run_light("預覽縮圖", _render, _rendered)

    def _set_thumbnail(self, path: Optional[Path], caption: str = "") -> None:
        """右欄換成新的縮圖 (None 表示清空)，上一張縮圖檔刪掉。"""
        previous = self._last_thumbnail
        self._last_thumbnail = path
        self._show_preview_image(path, caption)
        if previous is not None and previous != path:
            previous.unlink(missing_ok=True)

    def _clipped_output_for(self, source: Optional[Path]) -> Optional[Path]:
        """瀏覽器地圖「裁切後」層：這個原始檔的裁切成果，沒有就用這一頁最近一次的第一個成果 (例如拼接成果)。"""
        outputs = [(inp, out) for inp, out in self._clip_outputs if out.exists()]
        match = next((out for inp, out in outputs if inp == source), None)
        if match is not None:
            return match
        return outputs[0][1] if outputs else None

    def _open_browser_map(self) -> None:
        """右欄「瀏覽器套疊航照底圖」：依目前的原始檔、邊界與裁切成果產生地圖並開啟；內容沒變就直接開上次的。"""
        source = self._preview_source()
        boundary = self._current_boundary()
        clipped = self._clipped_output_for(source)
        if source is None and boundary is None and clipped is None:
            self.inspector.append_log("[提示] 還沒有可以放上地圖的圖資：請先投入原始檔或定義裁切邊界。\n")
            return
        assigned_srs = self._assigned_srs()
        key = (_file_key(source), _file_key(boundary), _file_key(clipped), assigned_srs)
        html = self._shown_preview_html
        if key == self._map_key and html is not None and html.exists():
            open_preview_in_browser(html)
            return
        out_html = self._temp_path("preview_map", ".html")
        style = self.kind.preview_style

        def _task() -> ClipPreviewMap:
            return build_clip_preview_map(
                out_html,
                style=style,
                source=source,
                boundary=boundary,
                clipped=clipped,
                assigned_srs=assigned_srs,
                text_callback=self.inspector.append_log,
            )

        def _done(result: ClipPreviewMap) -> None:
            self._map_key = key
            self._show_preview_html(result.html_path)
            open_preview_in_browser(result.html_path)

        self.inspector.run_task("產生瀏覽器地圖", _task, on_success=_done, error_title="瀏覽器地圖產生失敗")

    def _adopt_boundary(
        self,
        gpkg_path: Path,
        diag: CrsDiagnosisResult,
        *,
        area_m2: Optional[float] = None,
        area_ha: Optional[float] = None,
        is_base: bool = True,
    ) -> None:
        """把一個邊界檔設為目前的主裁切邊界，並更新右欄體檢卡與預覽縮圖。"""
        self.master_boundary_gpkg = gpkg_path
        if is_base:
            self._base_boundary_gpkg = gpkg_path
        self._clip_outputs = []
        # 右欄顯示的是邊界本身，「用這個範圍當邊界」沒有意義
        self._show_diagnosis(diag, area_m2=area_m2, area_ha=area_ha, can_use_extent=False)
        self._refresh_preview()
        self._refresh_ready()

    def _clear_boundary(self) -> None:
        """清掉目前的邊界：新的邊界檢查沒通過時不可沿用上一次的 (預覽縮圖也拿掉舊紅框)。"""
        had_boundary = self.master_boundary_gpkg is not None
        self.master_boundary_gpkg = None
        self._base_boundary_gpkg = None
        if had_boundary:
            self._refresh_preview()
        self._refresh_ready()

    # -------------------------------------------------------------------------
    # 事件處置函式：卡片 2 裁切邊界
    # -------------------------------------------------------------------------
    def _handle_create_boundary_from_text(self, srs_override: Optional[str] = None) -> None:
        """手動解析剪貼簿座標文字，自動首尾閉鎖並生成 master_boundary.gpkg。

        srs_override：座標的座標系 (「用這個範圍當邊界」時是該圖資的座標系)；None 時用來源座標系選單。
        """
        raw_text = self.txt_coords.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showwarning("警告", "請先貼上座標點列！")
            return
        assigned_srs = srs_override if srs_override is not None else self._assigned_srs()
        boundary_out = self._boundary_path()

        def _task() -> Tuple[BoundaryBuildResult, bool, CrsDiagnosisResult]:
            pts = parse_coordinate_text(raw_text)
            closed_pts, was_closed = auto_close_ring(pts)
            res = create_boundary_from_points(
                closed_pts, output_gpkg_path=boundary_out, assigned_srs=assigned_srs, text_callback=self.inspector.append_log
            )
            return res, was_closed, sniff_crs(res.gpkg_path)

        def _done(payload: Tuple[BoundaryBuildResult, bool, CrsDiagnosisResult]) -> None:
            res, was_closed, diag = payload
            self._adopt_boundary(res.gpkg_path, diag, area_m2=res.area_m2, area_ha=res.area_ha)
            self.lbl_paste_area.configure(
                text=f"即時面積：{res.area_m2:,.2f} m² ({res.area_ha:,.4f} 公頃) | 頂點數: {res.vertex_count} (自動閉合: {'是' if was_closed else '否'})"
            )
            self.inspector.append_log(f"[邊界完成] 主裁切邊界已建立: {res.gpkg_path.name}，面積 {res.area_m2:,.2f} m²\n")

        if self.inspector.run_task("邊界構面", _task, on_success=_done):
            self._clear_boundary()

    def _use_extent_as_boundary(self, diag: CrsDiagnosisResult) -> None:
        """右欄「用這個範圍當邊界」：把圖資的四角座標填進貼座標框並建立邊界，座標系沿用該圖資的。"""
        if diag.extent is None:
            return
        name = Path(diag.file_path).name if diag.file_path else "圖資"
        self.txt_coords.delete("1.0", "end")
        self.txt_coords.insert(
            "1.0", f"# {name} 的四角範圍 (可修改後再按「自動閉合並產生主邊界」)\n{format_extent_corners(diag.extent)}\n"
        )
        self._show_boundary_mode(BOUNDARY_MODE_PASTE)
        srs = assigned_srs_for_epsg(diag.detected_epsg) if diag.detected_epsg else self._assigned_srs()
        self.inspector.append_log(f"[邊界] 以 {name} 的四角範圍建立裁切邊界。\n")
        self._handle_create_boundary_from_text(srs_override=srs)

    def _handle_polar_sort_text(self) -> None:
        """依形心極角重新排序文字框內之頂點。"""
        raw_text = self.txt_coords.get("1.0", "end").strip()
        try:
            pts = parse_coordinate_text(raw_text)
            sorted_pts = sort_points_by_polar_angle(pts)

            new_text = "# 已完成形心極角排序 (逆時針修復沙漏狀自相交):\n"
            # 用 repr 寫回，保留原始位數 (經緯度取到小數 3 位會差到 100 公尺)
            for p in sorted_pts:
                new_text += f"{p.x!r}\t{p.y!r}\n"

            self.txt_coords.delete("1.0", "end")
            self.txt_coords.insert("1.0", new_text)
            self.inspector.append_log("[極角排序] 已依形心重新排列頂點順序，消除自相交沙漏多邊形。\n")
            self._handle_create_boundary_from_text()
        except Exception as err:
            messagebox.showerror("錯誤", f"極角排序失敗:\n{err}")

    def _handle_browse_boundary_file(self) -> None:
        """選取既有向量邊界檔案。"""
        f = filedialog.askopenfilename(title="選取邊界圖資", filetypes=[BOUNDARY_FILETYPES, ("所有檔案", "*.*")])
        if f:
            self._load_boundary_file(Path(f))

    def _load_boundary_file(self, p: Path) -> None:
        """採用一個向量檔當邊界：DXF 列出圖層後自動用第一個圖層線轉面；其餘格式直接檢查並採用
        (KML 等有好幾個圖層時用第一個有面的，「圖層」選單列出全部、停在用的那一個)。"""
        _, cad_warning = validate_supported_files([p])
        if cad_warning:
            messagebox.showwarning("警告", cad_warning)
            return
        self.entry_bound_file.delete(0, "end")
        self.entry_bound_file.insert(0, str(p))
        self._refresh_ready()

        if p.suffix.lower() == ".dxf":

            def _show_layers(lyrs: List[str]) -> None:
                if not lyrs:
                    self.inspector.append_log(f"[DXF] {p.name} 讀不到任何圖層。\n")
                    return
                self._dxf_layers_source = p.resolve()
                self.opt_dxf_layer.configure(values=lyrs)
                self.opt_dxf_layer.set(lyrs[0])
                more = "；邊界線不在這個圖層時，從「圖層」換一個會自動重做" if len(lyrs) > 1 else ""
                self.inspector.append_log(f"[DXF] {p.name} 共 {len(lyrs)} 個圖層，先用「{lyrs[0]}」自動線轉面{more}。\n")
                self._handle_dxf_lines_to_poly()

            self._run_light("讀取 DXF 圖層", lambda: list_dxf_layers(p), _show_layers)
            return
        self._import_boundary_layer(p, None)

    def _import_boundary_layer(self, p: Path, layer: Optional[str]) -> None:
        """檢查並採用 SHP／KML／GPKG 等邊界檔 (layer 是 None 時由程式挑第一個有面的圖層)；
        檔裡不只一個有圖徵的圖層時，「圖層」選單列出它們並停在用的那一個。"""
        build = self._make_file_boundary_task(p, layer)

        def _task() -> Tuple[BoundaryBuildResult, CrsDiagnosisResult, List[str]]:
            res = build()
            layers = geo_io.list_data_layers(p) if layer is None else []
            return res, sniff_crs(res.gpkg_path), layers

        def _done(payload: Tuple[BoundaryBuildResult, CrsDiagnosisResult, List[str]]) -> None:
            res, diag, layers = payload
            if len(layers) > 1:
                self._dxf_layers_source = p.resolve()
                self.opt_dxf_layer.configure(values=layers)
                self.inspector.append_log(f"[邊界] {p.name} 有 {len(layers)} 個圖層，邊界不在「{res.layer_name}」時從「圖層」換一個會自動重做。\n")
            elif layer is None:
                self._reset_boundary_layers()
            if res.layer_name:
                self.opt_dxf_layer.set(res.layer_name)
            self._adopt_boundary(res.gpkg_path, diag, area_m2=res.area_m2, area_ha=res.area_ha)
            used = f" (圖層「{res.layer_name}」)" if res.layer_name else ""
            self.inspector.append_log(f"[邊界完成] 已採用 {p.name}{used} 為主裁切邊界，面積 {res.area_m2:,.2f} m²\n")

        if self.inspector.run_task(f"邊界檢查 ({p.name})", _task, on_success=_done, error_title="這個檔案不能當裁切邊界"):
            self._clear_boundary()

    def _reset_boundary_layers(self) -> None:
        """「圖層」選單清回「只有一個圖層」(換成只有一個圖層的邊界檔時)。"""
        self._dxf_layers_source = None
        self.opt_dxf_layer.configure(values=[SINGLE_LAYER_TEXT])
        self.opt_dxf_layer.set(SINGLE_LAYER_TEXT)

    def _make_file_boundary_task(self, path: Path, layer: Optional[str]) -> Callable[[], BoundaryBuildResult]:
        """回傳在背景檢查並採用 SHP／KML 等邊界檔的函式 (參數在主執行緒先讀好)。"""
        assigned_srs = self._assigned_srs()
        boundary_out = self._boundary_path()
        return lambda: import_boundary_file(
            path, boundary_out, layer_name=layer, assigned_srs=assigned_srs, text_callback=self.inspector.append_log
        )

    def _handle_boundary_layer_chosen(self) -> None:
        """「圖層」選單換了：DXF 用那個 CAD 圖層重新線轉面，KML 等用那個圖層重新檢查。"""
        path_text = self.entry_bound_file.get().strip().strip('"')
        path = Path(path_text) if path_text else None
        if path is not None and path.suffix.lower() != ".dxf" and path.exists():
            layer = self._selected_dxf_layer(path)
            if layer is not None:
                self._import_boundary_layer(path, layer)
            return
        self._handle_dxf_lines_to_poly()

    def _read_snap_tolerance(self) -> Optional[float]:
        """閉鎖容許欄位的數值；不是 0 以上的數字時跳錯誤並回傳 None。"""
        try:
            snap_tolerance = float(self.entry_snap_tolerance.get().strip())
        except ValueError:
            snap_tolerance = -1.0
        if not (math.isfinite(snap_tolerance) and snap_tolerance >= 0):
            messagebox.showerror("錯誤", "閉鎖容許距離必須是 0 以上的數值（公尺，例如 0.5）！")
            return None
        return snap_tolerance

    def _selected_dxf_layer(self, dxf_path: Path) -> Optional[str]:
        """「圖層」選單的圖層；選單列的不是這個檔的圖層時 (例如路徑是手打的) 回傳 None
        (DXF 表示全部圖層，KML 等表示由程式挑第一個有面的)。"""
        if self._dxf_layers_source is None or self._dxf_layers_source != dxf_path.resolve():
            return None
        return self.opt_dxf_layer.get()

    def _make_dxf_boundary_task(
        self, dxf_path: Path, cad_layer: Optional[str], snap_tolerance: float
    ) -> Callable[[], BoundaryBuildResult]:
        """回傳在背景把 DXF 線轉面成邊界的函式 (參數在主執行緒先讀好)。"""
        assigned_srs = self._assigned_srs()
        boundary_out = self._boundary_path()

        def _build() -> BoundaryBuildResult:
            return dxf_lines_to_polygon_boundary(
                dxf_path,
                boundary_out,
                cad_layer=cad_layer,
                assigned_srs=assigned_srs,
                snap_tolerance=snap_tolerance,
                text_callback=self.inspector.append_log,
            )

        return _build

    def _handle_dxf_lines_to_poly(self, only_if_changed: bool = False) -> None:
        """把 DXF 的邊界線轉成面 (ST_BuildArea／打斷／自動閉鎖)；載入 DXF、換圖層、改閉鎖容許時自動呼叫。

        only_if_changed：閉鎖容許欄位按 Enter 或離開時用；沒有 DXF 或 (路徑, 圖層, 容許值) 跟上次一樣就不做。
        """
        dxf_text = self.entry_bound_file.get().strip().strip('"')
        dxf_path = Path(dxf_text) if dxf_text else None
        is_dxf = dxf_path is not None and dxf_path.suffix.lower() == ".dxf" and dxf_path.exists()
        if not is_dxf:
            if not only_if_changed:
                messagebox.showwarning("警告", "請先選取有效的 DXF 檔案！")
            return

        snap_tolerance = self._read_snap_tolerance()
        if snap_tolerance is None:
            return
        cad_layer = self._selected_dxf_layer(dxf_path)
        request = (str(dxf_path.resolve()), cad_layer, snap_tolerance)
        if only_if_changed and request == self._last_dxf_request:
            return
        build = self._make_dxf_boundary_task(dxf_path, cad_layer, snap_tolerance)

        def _task() -> Tuple[BoundaryBuildResult, CrsDiagnosisResult]:
            res = build()
            return res, sniff_crs(res.gpkg_path)

        def _done(payload: Tuple[BoundaryBuildResult, CrsDiagnosisResult]) -> None:
            res, diag = payload
            self._adopt_boundary(res.gpkg_path, diag, area_m2=res.area_m2, area_ha=res.area_ha)
            self.inspector.append_log(f"[DXF完成] 邊界多邊形已淨化封裝至: {res.gpkg_path.name}，面積 {res.area_m2:,.2f} m²\n")

        layer_label = cad_layer or "全部圖層"
        if self.inspector.run_task(
            f"DXF 線轉面 (圖層 {layer_label})", _task, on_success=_done, error_title="DXF 線轉面失敗"
        ):
            self._last_dxf_request = request
            self._clear_boundary()

    def _handle_apply_buffer(self) -> None:
        """對原始主邊界進行環域外推或內縮 (Buffer)；每次都從原始邊界算起，0 表示還原。"""
        base = self._base_boundary_gpkg
        if not base or not base.exists():
            messagebox.showwarning("警告", "請先完成邊界定義後再執行環域外推！")
            return

        buf_str = self.entry_buffer.get().strip()
        try:
            dist = float(buf_str)
        except ValueError:
            messagebox.showerror("錯誤", "外推距離必須為有效數值（例如 20 或 -10）！")
            return

        out_buffered = get_workspace_temp_dir() / f"{base.stem}_buf_{dist:g}m.gpkg"

        def _task() -> Tuple[Path, CrsDiagnosisResult]:
            if dist == 0:
                res_p = base
            else:
                res_p = generate_buffer(base, out_buffered, buffer_distance=dist, text_callback=self.inspector.append_log)
            return res_p, sniff_crs(res_p)

        def _done(payload: Tuple[Path, CrsDiagnosisResult]) -> None:
            res_p, diag = payload
            self._adopt_boundary(res_p, diag, is_base=False)
            if dist == 0:
                self.inspector.append_log(f"[外推還原] 已還原為原始邊界: {res_p.name}\n")
                messagebox.showinfo("成功", "已還原為原始邊界 (外推 0 公尺)。")
            else:
                self.inspector.append_log(f"[外推完成] 已自原始邊界外推 {dist:g} 公尺: {res_p.name}\n")
                messagebox.showinfo("成功", f"邊界已自原始邊界外推 {dist:g} 公尺！\n(重複按不會累加；輸入 0 可還原。)")

        self.inspector.run_task(f"環域外推 {dist:g} 公尺", _task, on_success=_done)

    # -------------------------------------------------------------------------
    # 事件處置函式：卡片 1 原始檔清單
    # -------------------------------------------------------------------------
    def _handle_add_files(self) -> None:
        """加入多個圖資檔案至待處理清單。"""
        files = filedialog.askopenfilenames(
            title=f"選取要裁切的{self.kind.noun}",
            filetypes=[self.kind.filetypes, ("所有檔案", "*.*")],
        )
        if files:
            self._add_input_files([Path(f) for f in files])

    def _handle_add_folder(self) -> None:
        """加入整個資料夾內的所有支援圖資。"""
        folder = filedialog.askdirectory(title="選取包含圖資之資料夾")
        if folder:
            self._add_input_folder(Path(folder))

    def _add_dropped_inputs(self, paths: List[Path]) -> None:
        """拖進清單的檔案直接加入；資料夾展開成其中支援的圖資。"""
        for folder in (p for p in paths if p.is_dir()):
            self._add_input_folder(folder)
        files = [p for p in paths if not p.is_dir()]
        if files:
            self._add_input_files(files)

    def _add_input_folder(self, p_dir: Path) -> int:
        """把資料夾 (含子資料夾) 內這一頁可用副檔名的圖資加入清單，回傳新增的個數 (類型不符的判別後再移出)。"""
        found: List[Path] = []
        cad_count = no_world = 0
        for f in sorted(p_dir.rglob("*")):
            if not f.is_file():
                continue
            suffix = f.suffix.lower()
            if suffix in geo_io.IMAGE_EXTENSIONS and not geo_io.has_world_file(f):
                # 沒有世界檔的照片沒有座標，不提示、只記一行
                no_world += 1
            elif suffix in self.kind.extensions:
                found.append(f)
            elif is_unsupported_cad(f):
                cad_count += 1
        count = self._add_input_files(found)
        note = f"；另有 {cad_count} 個 DWG／DGN 無法直接讀取，請先另存成 DXF" if cad_count else ""
        if no_world and self.kind is CLIP_KIND_ORTHO:
            note += f"；另有 {no_world} 個 JPG／PNG 沒有世界檔 (沒有座標)，已略過"
        self.inspector.append_log(f"[資料夾匯入] 已自 {p_dir.name} 匯入 {count} 個有效圖資{note}。\n")
        return count

    def _add_input_files(self, paths: List[Path]) -> int:
        """把檔案加入待裁切清單 (略過重複與 DWG／DGN)，回傳新增的個數；軌道判別在背景進行。"""
        kept, cad_warning = validate_supported_files(paths)
        if cad_warning:
            messagebox.showwarning("警告", cad_warning)
        added: List[Path] = []
        for p in kept:
            resolved = p.resolve()
            if resolved in self.selected_input_files:
                continue
            self.selected_input_files.append(resolved)
            added.append(resolved)
        self._refresh_file_list_display()
        if not added:
            return 0

        def _detect() -> Tuple[Dict[Path, str], Optional[CrsDiagnosisResult], Dict[Path, Tuple[float, bool]]]:
            tracks: Dict[Path, str] = {}
            sizes: Dict[Path, Tuple[float, bool]] = {}
            for f in added:
                try:
                    tracks[f] = detect_track_type(f).value
                except Exception:
                    tracks[f] = TrackType.UNKNOWN.value
                # DEM 與正射頁：原圖解析度 (說明與擋下比原圖細的數字用)
                if self.kind is not CLIP_KIND_SHP and tracks[f] == self.kind.track.value:
                    try:
                        sizes[f] = raster_pixel_size_m(f)
                    except Exception as err:
                        logger.debug("讀 %s 的解析度略過: %s", f, err)
            # 右欄體檢第一個類型相符的檔
            first_ok = next((f for f in added if tracks[f] == self.kind.track.value), None)
            diag: Optional[CrsDiagnosisResult] = None
            if first_ok is not None:
                try:
                    diag = sniff_crs(first_ok)
                except Exception as err:
                    logger.debug("體檢 %s 略過: %s", first_ok, err)
            return tracks, diag, sizes

        def _detected(payload: Tuple[Dict[Path, str], Optional[CrsDiagnosisResult], Dict[Path, Tuple[float, bool]]]) -> None:
            tracks, diag, sizes = payload
            self._track_cache.update(tracks)
            self._pixel_size_cache.update(sizes)
            self._keep_original_if_default_too_fine()
            wrong = [f for f in added if tracks.get(f) != self.kind.track.value and f in self.selected_input_files]
            if wrong:
                self._remove_wrong_kind(wrong, tracks)
            self._refresh_file_list_display()
            if diag is not None:
                self._show_diagnosis(diag)
            self._refresh_preview()

        self._run_light("圖資軌道判別", _detect, _detected)
        return len(added)

    def _remove_wrong_kind(self, files: List[Path], tracks: Dict[Path, str]) -> None:
        """把類型不屬於這一頁的檔移出清單，並說明該到哪一頁。"""
        lines = []
        for f in files:
            self.selected_input_files.remove(f)
            self._track_cache.pop(f, None)
            self._unchecked_files.discard(f)
            other = kind_for_track(tracks.get(f))
            if other is None and f.suffix.lower() in geo_io.IMAGE_EXTENSIONS:
                lines.append(f"{f.name}：{geo_io.NO_WORLD_FILE_REASON}，不能裁切")
            elif other is None:
                lines.append(f"{f.name}：無法判別圖資類型 (可能不是 GIS 圖檔或檔案損壞)")
            else:
                lines.append(f"{f.name}：這是{other.noun}，請到「{other.tab_title}」")
        self._selected_file_index = None
        shown = lines[:MAX_LISTED_WRONG_KIND]
        if len(lines) > len(shown):
            shown.append(f"…等共 {len(lines)} 個檔")
        message = f"以下檔案不是{self.kind.noun}，已移出清單：\n" + "\n".join(shown)
        self.inspector.append_log(f"[清單] {message}\n")
        messagebox.showwarning("檔案類型不符", message)

    def _select_file_index(self, index: Optional[int]) -> None:
        """選取清單中的第 index 筆 (從 0 起算)；超出範圍或 None 表示取消選取。"""
        if index is None or not 0 <= index < len(self.selected_input_files):
            self._selected_file_index = None
            self._refresh_file_list_display()
            self._refresh_preview()
            return
        self._selected_file_index = index
        self._refresh_file_list_display()
        target = self.selected_input_files[index]
        self._run_light(f"體檢 {target.name}", lambda: sniff_crs(target), self._show_diagnosis)
        self._refresh_preview()

    def _handle_remove_selected(self) -> None:
        """移除清單中被點選的那一筆。"""
        index = self._selected_file_index
        if index is None or not 0 <= index < len(self.selected_input_files):
            self.inspector.append_log("[移除] 請先在清單點選要移除的那一列。\n")
            return
        removed = self.selected_input_files.pop(index)
        self._track_cache.pop(removed, None)
        self._unchecked_files.discard(removed)
        self._selected_file_index = None
        self.inspector.append_log(f"[移除] 已自清單移除: {removed.name}\n")
        self._refresh_file_list_display()
        self._refresh_preview()

    def _handle_clear_files(self) -> None:
        """清空待裁切圖資清單。"""
        self.selected_input_files.clear()
        self._track_cache.clear()
        self._unchecked_files.clear()
        self._selected_file_index = None
        self._clip_outputs = []
        self._refresh_file_list_display()
        self._refresh_preview()
        self.inspector.append_log("[清單] 待裁切圖資清單已完全清空。\n")

    def checked_input_files(self) -> List[Path]:
        """清單裡有勾的檔 (照清單順序)，開始裁切時只處理這些。"""
        return [f for f in self.selected_input_files if f not in self._unchecked_files]

    def _toggle_file(self, index: int) -> None:
        """切換清單第 index 筆 (從 0 起算) 的勾選。"""
        if not 0 <= index < len(self.selected_input_files):
            return
        target = self.selected_input_files[index]
        if target in self._unchecked_files:
            self._unchecked_files.discard(target)
        else:
            self._unchecked_files.add(target)
        self._refresh_file_list_display()
        self._refresh_preview()

    def _set_all_files_checked(self, checked: bool) -> None:
        """清單表頭的勾選框：全勾或全不勾。"""
        self._unchecked_files = set() if checked else set(self.selected_input_files)
        self._refresh_file_list_display()
        self._refresh_preview()

    def _refresh_file_list_display(self) -> None:
        """刷新卡片 1 檔案清單與統計資訊 (只用已判別好的軌道，不在主執行緒呼叫 CLI)。"""
        self.file_list.set_rows(
            [
                CheckRow(
                    f.name,
                    (folder_label(f) if f in self._track_cache else DETECTING_TEXT,),
                    f not in self._unchecked_files,
                )
                for f in self.selected_input_files
            ],
            self._selected_file_index,
        )

        selected = ""
        if self._selected_file_index is not None:
            selected = f"，已選取: {self.selected_input_files[self._selected_file_index].name}"
        hint = "，點一列可選取" if self.selected_input_files and not selected else ""
        self.lbl_file_count.configure(text=f"共 {len(self.selected_input_files)} 個檔案{selected}{hint}")
        self._refresh_option_checkboxes()
        self._refresh_ready()

    def _refresh_option_checkboxes(self) -> None:
        """依清單裡有勾、已判別為這一頁類型的檔數，決定卡片 3 哪些勾選項可勾；不夠的取消勾選並停用 (KMZ 一律可勾)。"""
        count = sum(1 for f in self.checked_input_files() if self._track_cache.get(f) == self.kind.track.value)
        for key, checkbox in self.option_checks.items():
            applicable = count >= OPTION_MIN_FILES.get(key, 0)
            if not applicable:
                checkbox.deselect()
            checkbox.configure(state="normal" if applicable else "disabled")

    def _checked(self, key: str) -> bool:
        """這一頁的某個勾選項有沒有勾；這一頁沒有這個選項時為 False。"""
        checkbox = self.option_checks.get(key)
        return bool(checkbox.get()) if checkbox is not None else False

    def _read_contour_intervals(self) -> Optional[Tuple[float, float]]:
        """等高線間距欄位的 (首曲線, 計曲線)；不合理時跳說明並回傳 None。"""
        if self.entry_contour_interval is None or self.entry_index_interval is None:
            return (DEFAULT_CONTOUR_INTERVAL, DEFAULT_INDEX_INTERVAL)
        try:
            interval = float(self.entry_contour_interval.get().strip())
            index_interval = float(self.entry_index_interval.get().strip())
            validate_contour_intervals(interval, index_interval)
        except ValueError as err:
            reason = str(err) if "間距" in str(err) else "首曲線與計曲線間距都必須是大於 0 的數字 (公尺)。"
            messagebox.showerror("等高線間距不正確", reason)
            return None
        return interval, index_interval

    def _run_product(self, outcome: BatchClipOutcome, what: str, make: Callable[[], Any]) -> None:
        """在背景做一項附加成果 (等高線、坡度圖)；失敗記進 warnings，不影響裁切成果，取消則照常中止。"""
        try:
            make()
        except CommandCancelledError:
            raise
        except Exception as err:
            outcome.warnings.append(f"{what}失敗: {err}")
            self.inspector.append_log(f"[提示] {what}失敗: {err}\n")

    # -------------------------------------------------------------------------
    # 事件處置函式：執行批次裁切
    # -------------------------------------------------------------------------
    def _handle_browse_out_dir(self) -> None:
        """選輸出資料夾 (底部動作列的「瀏覽」)。"""
        self.action_bar.browse_output()

    def _pending_boundary_builder(self) -> Optional[Callable[[], BoundaryBuildResult]]:
        """邊界還沒建好時，依卡片 2 目前的來源回傳在背景建立邊界的函式；沒有可用的輸入就跳說明並回傳 None。"""
        assigned_srs = self._assigned_srs()
        boundary_out = self._boundary_path()
        if self.seg_boundary_mode.get() == BOUNDARY_MODE_PASTE:
            raw_text = self.txt_coords.get("1.0", "end").strip()
            if raw_text:
                return lambda: create_boundary_from_points(
                    auto_close_ring(parse_coordinate_text(raw_text))[0],
                    output_gpkg_path=boundary_out,
                    assigned_srs=assigned_srs,
                    text_callback=self.inspector.append_log,
                )
        else:
            path_text = self.entry_bound_file.get().strip().strip('"')
            path = Path(path_text) if path_text else None
            if path is not None and path.exists():
                if path.suffix.lower() == ".dxf":
                    snap_tolerance = self._read_snap_tolerance()
                    if snap_tolerance is None:
                        return None
                    return self._make_dxf_boundary_task(path, self._selected_dxf_layer(path), snap_tolerance)
                return self._make_file_boundary_task(path, self._selected_dxf_layer(path))
        messagebox.showwarning("警告", "還沒有裁切邊界，請先在「2 裁切邊界」載入邊界檔或貼上座標。")
        return None

    def _handle_start_batch_clip(self) -> None:
        """一鍵啟動這一頁的批次裁切；邊界還沒建好時先依卡片 2 的輸入自動建立 (例如 DXF 線轉面)。"""
        if not self.selected_input_files:
            messagebox.showwarning("警告", "清單裡還沒有原始檔，請先在「1 原始檔」加入檔案。")
            return
        if not self.checked_input_files():
            messagebox.showwarning("警告", "清單裡的檔案都沒勾，請先在「1 原始檔」勾選要處理的檔案。")
            return

        boundary = self.master_boundary_gpkg
        build_boundary: Optional[Callable[[], BoundaryBuildResult]] = None
        if not boundary or not boundary.exists():
            build_boundary = self._pending_boundary_builder()
            if build_boundary is None:
                return

        out_text = self.entry_out_dir.get().strip()
        if not out_text:
            messagebox.showwarning("警告", "請先指定輸出資料夾！")
            return
        out_dir = Path(out_text).resolve()

        dem_res: Optional[float] = None
        if self.combo_dem_res is not None:
            try:
                dem_res = parse_resolution(self.combo_dem_res.get(), RESOLUTION_UNITS["dem"][2])
            except ValueError:
                messagebox.showerror("錯誤", "DEM 解析度必須是大於 0 的數字 (公尺，例如 1 或 0.5)，或選「維持原圖解析度」。")
                return
        ortho_res: Optional[float] = None
        if self.combo_ortho_res is not None:
            try:
                ortho_res = parse_resolution(self.combo_ortho_res.get(), RESOLUTION_UNITS["ortho"][2])
            except ValueError:
                messagebox.showerror("錯誤", "影像解析度必須是大於 0 的數字 (公分，例如 10)，或選「維持原圖解析度」。")
                return
        problem = self._resolution_problem()
        if problem:
            messagebox.showwarning("警告", f"{problem}。比原圖細不會變清楚，只會讓檔案變大。")
            return

        gen_contour = self._checked("contour")
        gen_slope = self._checked("slope")
        contour_intervals: Tuple[float, float] = (DEFAULT_CONTOUR_INTERVAL, DEFAULT_INDEX_INTERVAL)
        if gen_contour:
            contour_intervals_or_none = self._read_contour_intervals()
            if contour_intervals_or_none is None:
                return
            contour_intervals = contour_intervals_or_none

        # 類型還沒判別完的檔不能送出 (判別完才知道是不是這一頁的類型)
        if any(f not in self._track_cache for f in self.selected_input_files):
            messagebox.showwarning("警告", "清單還在判別圖資類型，請稍候幾秒再執行。")
            return

        # 只處理有勾的檔
        input_files = self.checked_input_files()
        skipped = len(self.selected_input_files) - len(input_files)
        if skipped:
            self.inspector.append_log(f"[批次裁切] 清單裡有 {skipped} 個檔沒勾，這次跳過。\n")
        dem_resampling = self._dem_resampling
        target_srs = target_srs_from_label(self.opt_output_srs.get())
        assigned_srs = self._assigned_srs()
        gen_ovr = self._checked("pyramids")
        export_cad = self._checked("cad_bg")
        export_kmz_flag = self._checked("kmz")
        export_excel = self._checked("excel")
        keep_whole = self._checked("whole")
        gen_3d = self._checked("3d")
        mosaic = self._checked("mosaic")

        def _task() -> BatchClipOutcome:
            built: Optional[Tuple[BoundaryBuildResult, CrsDiagnosisResult]] = None
            boundary_path = boundary
            if build_boundary is not None:
                self.inspector.append_log("[邊界] 裁切邊界還沒建立，先依「2 裁切邊界」的輸入自動建立。\n")
                res = build_boundary()
                built = (res, sniff_crs(res.gpkg_path))
                boundary_path = res.gpkg_path
            out_dir.mkdir(parents=True, exist_ok=True)
            engine = BatchClipEngine(
                master_boundary_path=boundary_path,
                output_directory=out_dir,
                dem_target_resolution=dem_res,
                dem_resampling=dem_resampling,
                generate_pyramids=gen_ovr,
                export_cad_bg=export_cad,
                target_srs=target_srs,
                assigned_srs=assigned_srs,
                mosaic_dems=mosaic,
                ortho_target_resolution=ortho_res,
                export_attribute_excel=export_excel,
                keep_whole_features=keep_whole,
            )
            results = engine.execute_batch(
                input_files,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )
            outcome = BatchClipOutcome(results=list(results), built_boundary=built)

            # 收尾都在背景做；失敗不影響裁切成果，但要記進 warnings 讓使用者知道
            dem_results = [r for r in results if r.track_type == TrackType.TERRAIN_DEM and r.is_success]
            for r in dem_results:
                if gen_contour:
                    self._run_product(
                        outcome,
                        f"{r.output_path.name} 等高線 DXF",
                        lambda dem=r.output_path: outcome.contour_files.append(
                            generate_contour_dxf(
                                dem,
                                contour_output_path(dem),
                                interval=contour_intervals[0],
                                index_interval=contour_intervals[1],
                                text_callback=self.inspector.append_log,
                            ).output_path
                        ),
                    )
                if gen_slope:
                    self._run_product(
                        outcome,
                        f"{r.output_path.name} 坡度圖",
                        lambda dem=r.output_path: outcome.slope_files.append(
                            generate_slope_percent(dem, slope_output_path(dem), text_callback=self.inspector.append_log)
                        ),
                    )
            if dem_results and gen_3d:
                try:
                    p3d = generate_3d_hillshade_preview(
                        dem_results[0].output_path, boundary_path=boundary_path, text_callback=self.inspector.append_log
                    )
                    outcome.preview_image = p3d.preview_image_path
                except Exception as err:
                    outcome.warnings.append(f"3D 山體陰影縮圖產生失敗: {err}")
                    self.inspector.append_log(f"[提示] 3D 山體陰影縮圖產生失敗: {err}\n")

            vec_results = [r for r in results if r.track_type == TrackType.VECTOR_CADASTRE and r.is_success]
            outcome.preview_vector = vec_results[0].output_path if vec_results else boundary_path
            if export_excel:
                for r in vec_results:
                    if r.attribute_excel is None:
                        outcome.warnings.append(f"{r.output_path.name} 的屬性 Excel 沒有產生 (原因見日誌)")

            if export_kmz_flag:
                try:
                    outcome.kmz_path = export_to_kmz(
                        outcome.preview_vector,
                        out_dir / f"{outcome.preview_vector.stem}.kmz",
                        text_callback=self.inspector.append_log,
                    )
                except Exception as err:
                    outcome.warnings.append(f"KMZ 匯出失敗: {err}")
                    self.inspector.append_log(f"[提示] KMZ 匯出失敗: {err}\n")
            return outcome

        def _done(outcome: BatchClipOutcome) -> None:
            results = outcome.results
            failed = [r for r in results if not r.is_success]
            if outcome.built_boundary is not None:
                res, diag = outcome.built_boundary
                self._adopt_boundary(res.gpkg_path, diag, area_m2=res.area_m2, area_ha=res.area_ha)
                self.inspector.append_log(f"[邊界完成] 已自動建立裁切邊界，面積 {res.area_m2:,.2f} m²\n")
            # 瀏覽器地圖的「裁切後」層用這次的成果 (要在採用邊界之後記，採用邊界會清掉舊成果)
            self._clip_outputs = [(r.input_path, r.output_path) for r in results if r.is_success]
            if outcome.preview_image is not None:
                # 右欄改顯示裁切後的 3D 縮圖；之後點清單會再換回原始檔
                self._preview_request = None
                first_dem = next(r for r in results if r.is_success and r.track_type == TrackType.TERRAIN_DEM)
                self._show_preview_image(outcome.preview_image, f"裁切後：{first_dem.output_path.name} (3D 山體陰影)")
            for r in failed:
                self.inspector.append_log(f"[裁切失敗] {r.input_path.name}: {r.error_message}\n")

            lines = [
                f"{self.kind.noun}批次裁切已完成！",
                f"成功: {len(results) - len(failed)} 件 / 失敗: {len(failed)} 件",
                f"成果目錄: {out_dir}",
            ]
            if keep_whole:
                lines.append("整筆保留：碰到範圍的圖徵整筆選出、不切開 (*_whole.shp)")
            if outcome.kmz_path is not None:
                lines.append(f"KMZ: {outcome.kmz_path.name}")
            excel_files = [r.attribute_excel for r in results if r.is_success and r.attribute_excel is not None]
            if excel_files:
                lines.append(f"屬性 Excel: {len(excel_files)} 個 (與成果同名 .xlsx)")
            if outcome.contour_files:
                lines.append(f"等高線 DXF: {len(outcome.contour_files)} 個 (*_contours.dxf)")
            if outcome.slope_files:
                lines.append(f"坡度圖: {len(outcome.slope_files)} 個 (*_slope_pct.tif)")
            if failed:
                lines.append("失敗的檔案與原因請看右欄日誌。")
            if outcome.warnings:
                lines.append("")
                lines.append("以下收尾步驟沒有完成 (裁切成果不受影響):")
                lines.extend(f"・{w}" for w in outcome.warnings)
            if failed or outcome.warnings:
                messagebox.showwarning("作業完成 (有項目需留意)", "\n".join(lines))
            else:
                messagebox.showinfo("作業完成", "\n".join(lines))

        def _restore_button() -> None:
            self.btn_execute.configure(text=EXECUTE_BUTTON_TEXT)
            self._refresh_ready()

        if self.inspector.run_task(
            "批次裁切", _task, on_success=_done, on_finish=_restore_button, error_title="批次作業異常"
        ):
            self.btn_execute.configure(state="disabled", text="正在裁切")

