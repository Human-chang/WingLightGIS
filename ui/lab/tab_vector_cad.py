"""測試版工具「向量與 CAD」(ui/lab/tab_vector_cad.py)；放在 ui/lab/lab_page.py 的頁面裡 (原分頁 2)

嚴格遵守專案規格書第 4 節規範：
0. 最上方一列「無標籤圖資的來源座標系」下拉選單，套用到本頁所有卡片。
1. 頂部一列工具切換鈕，一次只顯示被選中的那個工具的卡片 (DXF 分層與 CAD 圖面校正共用一個切換項)：
   - 卡片 1：[A-05] 地籍空間交集與工程用地 Excel 清冊產生器（工區交集、徵收面積、千分位報表）。
   - 卡片 2：[G-07] 多圖幅向量批次合併 (ogrmerge.py)。
   - 卡片 3：[G-10] 密集頂點抽稀瘦身 (-simplify) 與 Big5/CP950 中文亂碼修復。
   - 卡片 4：[G-11] 座標文字表格 (.csv / .txt) 轉空間點位向量圖資。
   - 卡片 5：[A-07] 向量圖層依屬性欄位自動拆檔 (Split by Attribute)。
   - 卡片 6：DXF 依點、線、面分層並強制 2D (輸出單一 GPKG)。
2. 所有按鈕皆經 PanelInspector.run_task 在背景執行：日誌、進度、完成與錯誤對話框都回到主執行緒，
   執行中可按右欄的「取消作業」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, List, Optional, Tuple, Union

import customtkinter as ctk

from ui.theme_constants import FONT_FAMILY, FONT_FAMILY_MONO, FONT_SIZE_BODY, FONT_SIZE_CAPTION, FONT_SIZE_CARD_TITLE, FONT_SIZE_MONO
from core.crs_doctor import (
    AffineTransformResult,
    ControlPoint,
    GcpGeoreferenceResult,
    calculate_affine_transform,
    georeference_vector_with_gcps,
    parse_control_points_text,
    resolve_assigned_srs,
    scale_vector_coordinates,
)
from core.vector_ops import (
    CadastralIntersectionResult,
    DxfGeometrySplitResult,
    convert_csv_table_to_points,
    generate_cadastral_intersection_report,
    merge_vector_layers,
    repair_shapefile_encoding,
    simplify_vector_geometry,
    split_dxf_by_geometry_type,
    split_vector_by_attribute,
)
from ui import drag_drop
from ui.file_guards import UNSUPPORTED_CAD_MESSAGE, is_unsupported_cad, validate_supported_files
from ui.output_dir_row import OutputDirRow
from ui.panel_inspector import PanelInspector
from ui.theme_constants import BTN_PRIMARY, BTN_SECONDARY, CARD_FG_COLOR, STATUS_ERROR, STATUS_OK, STATUS_WARNING, T
from ui.lab.tool_switcher import ToolCardSwitcher
from ui.srs_options import (
    AUTO_SOURCE_SRS_LABEL,
    SOURCE_SRS_CHOICES,
    SOURCE_SRS_HINT,
    assigned_srs_from_label,
    source_srs_labels,
)

logger = logging.getLogger("ui.tab_vector_cad")

VECTOR_FILETYPES: Tuple[str, str] = ("向量圖資", "*.shp;*.gpkg;*.geojson")
# 拖進合併卡片的資料夾只收這些向量檔 (與 VECTOR_FILETYPES 一致)
MERGE_VECTOR_SUFFIXES = frozenset({".shp", ".gpkg", ".geojson"})
MERGE_DROP_HINT: str = " (可把檔案或資料夾拖到這張卡片)"
CAD_FIX_FILETYPES: Tuple[str, str] = ("CAD 與向量圖資", "*.dxf;*.shp;*.gpkg;*.geojson")
# CAD 圖面以公釐繪製時，乘這個倍率換成公尺
DEFAULT_CAD_SCALE_TEXT: str = "0.001"
# 控制點校正的 RMSE 超過這個值 (目標座標系的單位，通常是公尺) 就在畫面上提醒
GCP_RMSE_WARNING_THRESHOLD: float = 0.5

# 頂部切換鈕的工具名稱 (順序即顯示順序)
TOOL_CADASTRAL: str = "地籍交集清冊"
TOOL_DXF: str = "DXF分層與校正"
TOOL_MERGE: str = "多檔合併"
TOOL_ENCODING_SIMPLIFY: str = "亂碼與抽稀"
TOOL_CSV_POINTS: str = "CSV轉點"
TOOL_SPLIT: str = "屬性拆檔"
VECTOR_TOOL_NAMES: Tuple[str, ...] = (
    TOOL_CADASTRAL,
    TOOL_DXF,
    TOOL_MERGE,
    TOOL_ENCODING_SIMPLIFY,
    TOOL_CSV_POINTS,
    TOOL_SPLIT,
)

# DXF 分層後各圖層的中文名稱
DXF_LAYER_DISPLAY_NAMES = {"points": "點", "lines": "線", "polygons": "面"}


class TabVectorCad(ctk.CTkScrollableFrame):
    """測試版工具「向量與 CAD」的工作區。"""

    def __init__(
        self,
        master: Any,
        inspector: PanelInspector,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)

        self.inspector = inspector
        self.font_title = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CARD_TITLE, weight="bold")
        self.font_caption = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_CAPTION)
        self.font_body = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)
        self.font_btn = ctk.CTkFont(family=FONT_FAMILY, size=FONT_SIZE_BODY)

        self.grid_columnconfigure(0, weight=1)

        self._build_source_srs_row()
        self.tool_switcher = ToolCardSwitcher(self, VECTOR_TOOL_NAMES, row=1, font=self.font_body)
        self._build_card_cadastral_intersection()
        self._build_card_merge_vectors()
        self._build_card_simplify_and_encoding()
        self._build_card_csv_to_points()
        self._build_card_split_by_attribute()
        self._build_card_dxf_geometry_split()
        self._build_card_cad_scale_and_gcp()
        self.tool_switcher.show(VECTOR_TOOL_NAMES[0])

        for entry in (
            self.entry_inter_bound,
            self.entry_inter_cad,
            self.entry_simp_file,
            self.entry_csv_file,
            self.entry_split_file,
            self.entry_dxf_split_file,
            self.entry_cad_fix_file,
        ):
            drag_drop.bind_file_entry(entry)

    # -------------------------------------------------------------------------
    # 來源座標系指派列 (套用到本頁所有卡片)
    # -------------------------------------------------------------------------
    def _build_source_srs_row(self) -> None:
        """建立「無標籤圖資的來源座標系」下拉選單。"""
        frame = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        frame.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        frame.grid_columnconfigure(1, weight=1)

        lbl = ctk.CTkLabel(frame, text="無標籤圖資的來源座標系:", font=self.font_caption)
        lbl.grid(row=0, column=0, padx=(10, 4), pady=(6, 0), sticky="w")
        self.opt_source_srs = ctk.CTkOptionMenu(frame, values=source_srs_labels())
        self.opt_source_srs.grid(row=0, column=1, padx=(4, 10), pady=(6, 0), sticky="ew")
        self.opt_source_srs.set(AUTO_SOURCE_SRS_LABEL)

        lbl_hint = ctk.CTkLabel(
            frame, text=SOURCE_SRS_HINT, font=self.font_body, text_color=T["muted"], anchor="w", justify="left", wraplength=520
        )
        lbl_hint.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 6), sticky="w")

    # -------------------------------------------------------------------------
    # 卡片 1：地籍空間交集與 Excel 清冊
    # -------------------------------------------------------------------------
    def _build_card_cadastral_intersection(self) -> None:
        """建立 [A-05] 地籍空間交集與 Excel 清冊匯出卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_CADASTRAL, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(
            card,
            text="[A-05] 地籍圖與工區空間交集 — Excel 用地清冊報表",
            font=self.font_title,
            anchor="w",
        )
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        # 工區邊界選取
        lbl_b = ctk.CTkLabel(card, text="工區邊界檔:", font=self.font_caption)
        lbl_b.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_inter_bound = ctk.CTkEntry(card, placeholder_text="請選取 master_boundary 或工區界線")
        self.entry_inter_bound.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_b = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_inter_boundary, **BTN_SECONDARY)
        btn_b.grid(row=1, column=2, padx=6, pady=2)

        # 地籍圖選取
        lbl_c = ctk.CTkLabel(card, text="地籍圖資檔:", font=self.font_caption)
        lbl_c.grid(row=2, column=0, padx=6, pady=2, sticky="w")
        self.entry_inter_cad = ctk.CTkEntry(card, placeholder_text="請選取地籍 Shapefile / GPKG / GeoJSON")
        self.entry_inter_cad.grid(row=2, column=1, padx=4, pady=2, sticky="ew")
        btn_c = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_inter_cadastre, **BTN_SECONDARY)
        btn_c.grid(row=2, column=2, padx=6, pady=2)

        self.out_cadastral = OutputDirRow(card, row=3, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="執行空間交集並產出 Excel 用地徵收清冊",
            font=self.font_btn,
            command=self._handle_run_cadastral_intersection,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 2：多圖幅向量合併
    # -------------------------------------------------------------------------
    def _build_card_merge_vectors(self) -> None:
        """建立 [G-07] 多圖幅向量批次合併卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_MERGE, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[G-07] 多圖幅向量批次合併 (ogrmerge.py)", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        self.merge_files: List[Path] = []
        self.lbl_merge_count = ctk.CTkLabel(card, text=f"已選取 0 個圖資檔案{MERGE_DROP_HINT}", font=self.font_body, anchor="w")
        self.lbl_merge_count.grid(row=1, column=0, columnspan=2, padx=8, pady=2, sticky="w")
        for target in (card, lbl_head, self.lbl_merge_count):
            drag_drop.bind_paths(target, self._add_dropped_merge_files)

        btn_select = ctk.CTkButton(card, text="選取多個向量檔", width=140, command=self._browse_merge_files, **BTN_SECONDARY)
        btn_select.grid(row=1, column=2, padx=6, pady=2)

        self.out_merge = OutputDirRow(card, row=2, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="合併為單一 GeoPackage 圖層",
            font=self.font_btn,
            command=self._handle_run_merge_vectors,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 3：抽稀與中文亂碼修復
    # -------------------------------------------------------------------------
    def _build_card_simplify_and_encoding(self) -> None:
        """建立 [G-10] 頂點抽稀與 Shapefile 中文編碼修復卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_ENCODING_SIMPLIFY, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[G-10] 頂點抽稀瘦身 與 中文亂碼修復 (.cpg)", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_f = ctk.CTkLabel(card, text="目標圖資:", font=self.font_caption)
        lbl_f.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_simp_file = ctk.CTkEntry(card, placeholder_text="請選取待處理之 Shapefile")
        self.entry_simp_file.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_f = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_simplify_file, **BTN_SECONDARY)
        btn_f.grid(row=1, column=2, padx=6, pady=2)

        self.out_simplify = OutputDirRow(card, row=2, font=self.font_caption)

        frame_btns = ctk.CTkFrame(card, fg_color="transparent")
        frame_btns.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        frame_btns.grid_columnconfigure((0, 1), weight=1)

        btn_fix_encoding = ctk.CTkButton(
            frame_btns,
            text="補正 Big5/CP950 轉 UTF-8",
            font=self.font_btn,
            command=self._handle_fix_encoding,
            **BTN_PRIMARY,
        )
        btn_fix_encoding.grid(row=0, column=0, padx=3, pady=2, sticky="ew")

        btn_simplify = ctk.CTkButton(
            frame_btns,
            text="抽稀頂點 (容許度 1.0m)",
            font=self.font_btn,
            command=self._handle_simplify,
            **BTN_PRIMARY,
        )
        btn_simplify.grid(row=0, column=1, padx=3, pady=2, sticky="ew")

    # -------------------------------------------------------------------------
    # 卡片 4：CSV 轉空間點位
    # -------------------------------------------------------------------------
    def _build_card_csv_to_points(self) -> None:
        """建立 [G-11] 座標文字表格轉點位卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_CSV_POINTS, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[G-11] 坐標文字表 (.csv/.txt) 轉點位圖資", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_csv = ctk.CTkLabel(card, text="CSV 檔案:", font=self.font_caption)
        lbl_csv.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_csv_file = ctk.CTkEntry(card, placeholder_text="請選取包含 X, Y 數值之 CSV 試算表")
        self.entry_csv_file.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_csv = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_csv_file, **BTN_SECONDARY)
        btn_csv.grid(row=1, column=2, padx=6, pady=2)

        self.out_csv = OutputDirRow(card, row=2, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="轉為 TWD97 121 分帶空間點位 (GPKG)",
            font=self.font_btn,
            command=self._handle_convert_csv_to_points,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 5：屬性自動拆檔
    # -------------------------------------------------------------------------
    def _build_card_split_by_attribute(self) -> None:
        """建立 [A-07] 向量依屬性欄位拆檔卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_SPLIT, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[A-07] 向量圖層依屬性欄位自動拆檔", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_s = ctk.CTkLabel(card, text="目標圖資:", font=self.font_caption)
        lbl_s.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_split_file = ctk.CTkEntry(card, placeholder_text="請選取欲依屬性分割之向量檔案")
        self.entry_split_file.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_s = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_split_file, **BTN_SECONDARY)
        btn_s.grid(row=1, column=2, padx=6, pady=2)

        lbl_col = ctk.CTkLabel(card, text="分割欄位:", font=self.font_caption)
        lbl_col.grid(row=2, column=0, padx=6, pady=2, sticky="w")
        self.entry_split_col = ctk.CTkEntry(card, placeholder_text="輸入屬性欄位名稱 (例如 COUNTY, SECT)")
        self.entry_split_col.grid(row=2, column=1, padx=4, pady=2, sticky="ew")

        self.out_split = OutputDirRow(card, row=3, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="開始依屬性獨立拆檔",
            font=self.font_btn,
            command=self._handle_run_split_by_attribute,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 6：DXF 依點線面分層
    # -------------------------------------------------------------------------
    def _build_card_dxf_geometry_split(self) -> None:
        """建立 DXF 依點、線、面分層並強制 2D 的卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_DXF, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="DXF 依點、線、面分層並強制 2D", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_f = ctk.CTkLabel(card, text="DXF 檔案:", font=self.font_caption)
        lbl_f.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_dxf_split_file = ctk.CTkEntry(card, placeholder_text="請選取要分層的 DXF")
        self.entry_dxf_split_file.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_f = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_dxf_split_file, **BTN_SECONDARY)
        btn_f.grid(row=1, column=2, padx=6, pady=2)

        lbl_note = ctk.CTkLabel(
            card,
            text="輸出 <檔名>_layers.gpkg (預設與 DXF 同資料夾)，內含 points / lines / polygons 圖層；Z 值捨棄。"
            "封閉聚合線在 DXF 裡仍算線，要圍成面請用分頁 1 的「線轉面」。",
            font=self.font_body,
            text_color=T["muted"],
            anchor="w",
            justify="left",
            wraplength=520,
        )
        lbl_note.grid(row=3, column=0, columnspan=3, padx=10, pady=(0, 2), sticky="w")

        self.out_dxf_split = OutputDirRow(card, row=2, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="分層並輸出 GeoPackage",
            font=self.font_btn,
            command=self._handle_split_dxf_by_geometry,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    def _build_card_cad_scale_and_gcp(self) -> None:
        """建立 CAD 圖面校正卡片：單位縮放 (公釐轉公尺) 與控制點仿射對齊。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_DXF, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="CAD 圖面校正 (單位縮放／控制點對齊)", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_f = ctk.CTkLabel(card, text="圖面檔案:", font=self.font_caption)
        lbl_f.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_cad_fix_file = ctk.CTkEntry(card, placeholder_text="請選取要校正的 DXF 或向量檔")
        self.entry_cad_fix_file.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_f = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_cad_fix_file, **BTN_SECONDARY)
        btn_f.grid(row=1, column=2, padx=6, pady=2)

        lbl_scale = ctk.CTkLabel(card, text="縮放倍率:", font=self.font_caption)
        lbl_scale.grid(row=2, column=0, padx=6, pady=2, sticky="w")
        self.entry_cad_scale = ctk.CTkEntry(card, placeholder_text="0.001 (公釐轉公尺)")
        self.entry_cad_scale.grid(row=2, column=1, padx=4, pady=2, sticky="ew")
        self.entry_cad_scale.insert(0, DEFAULT_CAD_SCALE_TEXT)
        btn_scale = ctk.CTkButton(card, text="單位縮放", width=65, command=self._handle_scale_cad, **BTN_SECONDARY)
        btn_scale.grid(row=2, column=2, padx=6, pady=2)

        lbl_gcp = ctk.CTkLabel(
            card,
            text="控制點 (每列一組: 圖面X, 圖面Y, 真實X, 真實Y；至少 3 組且不可共線):",
            font=self.font_caption,
            anchor="w",
        )
        lbl_gcp.grid(row=3, column=0, columnspan=3, padx=6, pady=(4, 0), sticky="w")
        self.txt_gcp = ctk.CTkTextbox(card, height=70, font=ctk.CTkFont(family=FONT_FAMILY_MONO, size=FONT_SIZE_MONO))
        self.txt_gcp.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=2)

        lbl_target = ctk.CTkLabel(card, text="真實座標的座標系:", font=self.font_caption)
        lbl_target.grid(row=5, column=0, padx=6, pady=2, sticky="w")
        gcp_srs_labels = [label for label, assigned in SOURCE_SRS_CHOICES if assigned]
        self.opt_gcp_target_srs = ctk.CTkOptionMenu(card, values=gcp_srs_labels)
        self.opt_gcp_target_srs.grid(row=5, column=1, columnspan=2, padx=(4, 6), pady=2, sticky="ew")
        self.opt_gcp_target_srs.set(gcp_srs_labels[0])

        self.lbl_gcp_rmse = ctk.CTkLabel(card, text="RMSE：尚未試算", font=self.font_body, text_color=T["muted"], anchor="w")
        self.lbl_gcp_rmse.grid(row=6, column=0, columnspan=3, padx=10, pady=(0, 2), sticky="w")

        # 單位縮放與控制點校正共用這一列
        self.out_cad_fix = OutputDirRow(card, row=7, font=self.font_caption)

        frame_btns = ctk.CTkFrame(card, fg_color="transparent")
        frame_btns.grid(row=8, column=0, columnspan=3, sticky="ew", padx=4, pady=(2, 2))
        frame_btns.grid_columnconfigure((0, 1), weight=1)
        btn_preview = ctk.CTkButton(frame_btns, text="試算 RMSE", font=self.font_btn, command=self._handle_gcp_preview, **BTN_SECONDARY)
        btn_preview.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        btn_gcp = ctk.CTkButton(
            frame_btns,
            text="控制點校正並輸出 GeoPackage",
            font=self.font_btn,
            command=self._handle_gcp_georeference,
            **BTN_PRIMARY,
        )
        btn_gcp.grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        lbl_note = ctk.CTkLabel(
            card,
            text="單位縮放輸出 <檔名>_scaled (格式同來源)；控制點校正輸出 <檔名>_georef.gpkg，預設都寫在來源檔同資料夾。"
            "圖面若是公釐，請先縮放、再對縮放後的檔案做控制點校正 (控制點的圖面座標也要用縮放後的)。",
            font=self.font_body,
            text_color=T["muted"],
            anchor="w",
            justify="left",
            wraplength=520,
        )
        lbl_note.grid(row=9, column=0, columnspan=3, padx=10, pady=(0, 8), sticky="w")

    # -------------------------------------------------------------------------
    # 事件處置實作
    # -------------------------------------------------------------------------
    def _browse_cad_fix_file(self) -> None:
        self._browse_into(self.entry_cad_fix_file, "選取要校正的圖面", [CAD_FIX_FILETYPES])

    def _cad_fix_source(self) -> Optional[Path]:
        """讀出圖面校正卡片的來源檔；沒填、DWG／DGN 或檔案不存在時提示並回傳 None。"""
        f_path = self.entry_cad_fix_file.get().strip()
        if not f_path:
            messagebox.showwarning("警告", "請先選取要校正的圖面檔案！")
            return None
        if is_unsupported_cad(f_path):
            messagebox.showwarning("警告", UNSUPPORTED_CAD_MESSAGE)
            return None
        source = Path(f_path).resolve()
        if not source.exists():
            messagebox.showwarning("警告", f"找不到檔案:\n{source}")
            return None
        return source

    def _handle_scale_cad(self) -> None:
        source = self._cad_fix_source()
        if source is None:
            return
        try:
            factor = float(self.entry_cad_scale.get().strip())
        except ValueError:
            messagebox.showerror("錯誤", "縮放倍率必須為有效數值（例如 0.001）！")
            return
        if factor <= 0:
            messagebox.showerror("錯誤", "縮放倍率必須大於 0！")
            return
        out_path = self._output_file(self.out_cad_fix, source.with_name(f"{source.stem}_scaled{source.suffix}"))
        if out_path is None:
            return

        def _task() -> Path:
            scale_vector_coordinates(source, out_path, factor, text_callback=self.inspector.append_log)
            return out_path

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[單位縮放完成] 座標已乘上 {factor:g}: {res_p}\n")
            messagebox.showinfo("成功", f"座標已乘上 {factor:g}！\n輸出檔案: {res_p}")

        self.inspector.run_task(f"單位縮放 ×{factor:g}", _task, on_success=_done)

    def _read_control_points(self) -> Optional[Tuple[List[ControlPoint], AffineTransformResult]]:
        """解析控制點文字並試算仿射變換，同時更新 RMSE 標籤；內容有誤時提示並回傳 None。"""
        try:
            points = parse_control_points_text(self.txt_gcp.get("1.0", "end"))
            transform = calculate_affine_transform(points)
        except ValueError as err:
            self.lbl_gcp_rmse.configure(text="RMSE：控制點有誤，無法試算", text_color=STATUS_ERROR)
            messagebox.showerror("控制點有誤", str(err))
            return None
        worst = max(transform.residuals) if transform.residuals else 0.0
        too_large = transform.rmse > GCP_RMSE_WARNING_THRESHOLD
        note = "，誤差偏大，請檢查控制點是否打錯或單位不一致" if too_large else ""
        self.lbl_gcp_rmse.configure(
            text=f"RMSE：{transform.rmse:.4f} (控制點 {len(points)} 組，最大殘差 {worst:.4f}){note}",
            text_color=STATUS_WARNING if too_large else STATUS_OK,
        )
        return points, transform

    def _handle_gcp_preview(self) -> None:
        parsed = self._read_control_points()
        if parsed is not None:
            points, transform = parsed
            self.inspector.append_log(f"[GCP 試算] 控制點 {len(points)} 組，RMSE = {transform.rmse:.4f}\n")

    def _handle_gcp_georeference(self) -> None:
        source = self._cad_fix_source()
        if source is None:
            return
        parsed = self._read_control_points()
        if parsed is None:
            return
        points, _ = parsed
        target_srs = resolve_assigned_srs(assigned_srs_from_label(self.opt_gcp_target_srs.get()))
        if target_srs is None:
            messagebox.showwarning("警告", "請先選擇控制點真實座標的座標系！")
            return
        out_path = self._output_file(self.out_cad_fix, source.with_name(f"{source.stem}_georef.gpkg"))
        if out_path is None:
            return

        def _task() -> GcpGeoreferenceResult:
            return georeference_vector_with_gcps(
                source, out_path, points, target_srs=target_srs, text_callback=self.inspector.append_log
            )

        def _done(res: GcpGeoreferenceResult) -> None:
            self.inspector.append_log(
                f"[GCP 校正完成] 控制點 {res.control_point_count} 組，RMSE = {res.transform.rmse:.4f}: {res.output_path}\n"
            )
            messagebox.showinfo(
                "成功",
                f"控制點校正完成！\n控制點 {res.control_point_count} 組，RMSE = {res.transform.rmse:.4f}\n輸出檔案: {res.output_path}",
            )

        self.inspector.run_task("控制點校正", _task, on_success=_done)

    def _assigned_srs(self) -> Optional[str]:
        """來源座標系下拉選單目前的指派值；自動判斷時為 None。"""
        return assigned_srs_from_label(self.opt_source_srs.get())

    @staticmethod
    def _output_folder(row: OutputDirRow, source_path: Union[str, Path], result_label: str) -> Optional[Path]:
        """多檔成果的資料夾；輸出資料夾欄位填錯時跳警告並回傳 None。"""
        try:
            return row.folder_for(source_path, result_label)
        except ValueError as exc:
            messagebox.showwarning("警告", str(exc))
            return None

    @staticmethod
    def _output_file(row: OutputDirRow, default_path: Path) -> Optional[Path]:
        """單一成果的路徑；輸出資料夾欄位填錯時跳警告並回傳 None。"""
        try:
            return row.file_for(default_path)
        except ValueError as exc:
            messagebox.showwarning("警告", str(exc))
            return None

    @staticmethod
    def _set_entry(entry: ctk.CTkEntry, value: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, value)

    def _browse_into(self, entry: ctk.CTkEntry, title: str, filetypes: List[Tuple[str, str]]) -> None:
        f = filedialog.askopenfilename(title=title, filetypes=filetypes + [("所有檔案", "*.*")])
        if not f:
            return
        _, cad_warning = validate_supported_files([f])
        if cad_warning:
            messagebox.showwarning("警告", cad_warning)
            return
        self._set_entry(entry, str(Path(f).resolve()))

    def _browse_inter_boundary(self) -> None:
        self._browse_into(self.entry_inter_bound, "選取工區邊界", [VECTOR_FILETYPES])

    def _browse_inter_cadastre(self) -> None:
        self._browse_into(self.entry_inter_cad, "選取地籍圖資", [VECTOR_FILETYPES])

    def _handle_run_cadastral_intersection(self) -> None:
        b_path = self.entry_inter_bound.get().strip()
        c_path = self.entry_inter_cad.get().strip()
        if not b_path or not c_path:
            messagebox.showwarning("警告", "請先完整指定工區邊界檔與地籍圖檔案！")
            return
        assigned_srs = self._assigned_srs()
        out_dir = self._output_folder(self.out_cadastral, c_path, "地籍交集成果")
        if out_dir is None:
            return

        def _task() -> CadastralIntersectionResult:
            return generate_cadastral_intersection_report(
                b_path,
                c_path,
                output_dir=out_dir,
                assigned_srs=assigned_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )

        def _done(res: CadastralIntersectionResult) -> None:
            self.inspector.append_log(f"[交集完成] 已產出 {res.total_parcels_count} 筆劃入地號清冊: {res.excel_report_path}\n")
            messagebox.showinfo(
                "交集計算成功",
                f"地籍空間交集已完成！\n劃入地號數: {res.total_parcels_count} 筆\n總劃入面積: {res.total_clipped_area_m2:,.2f} m² ({res.total_clipped_area_ha:,.4f} 公頃)\nExcel報表: {res.excel_report_path.name}\n成果資料夾: {out_dir}",
            )

        self.inspector.run_task("地籍交集", _task, on_success=_done)

    def _browse_merge_files(self) -> None:
        files = filedialog.askopenfilenames(title="選取欲合併圖資", filetypes=[VECTOR_FILETYPES, ("所有檔案", "*.*")])
        if files:
            kept, cad_warning = validate_supported_files(files)
            if cad_warning:
                messagebox.showwarning("警告", cad_warning)
            self.merge_files = [p.resolve() for p in kept]
            self.lbl_merge_count.configure(text=f"已選取 {len(self.merge_files)} 個圖資檔案")
            self.inspector.append_log(f"[合併清單] 已載入 {len(self.merge_files)} 個圖層準備合併。\n")

    def _add_dropped_merge_files(self, paths: List[Path]) -> None:
        """拖進合併卡片的檔案追加到清單 (不取代)；資料夾展開成其中的向量檔。"""
        candidates: List[Path] = []
        for p in paths:
            if p.is_dir():
                candidates.extend(f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in MERGE_VECTOR_SUFFIXES)
            else:
                candidates.append(p)
        kept, cad_warning = validate_supported_files(candidates)
        if cad_warning:
            messagebox.showwarning("警告", cad_warning)
        added = 0
        for p in kept:
            resolved = p.resolve()
            if resolved not in self.merge_files:
                self.merge_files.append(resolved)
                added += 1
        self.lbl_merge_count.configure(text=f"已選取 {len(self.merge_files)} 個圖資檔案")
        self.inspector.append_log(f"[合併清單] 拖入 {added} 個圖層，共 {len(self.merge_files)} 個準備合併。\n")

    def _handle_run_merge_vectors(self) -> None:
        if len(self.merge_files) < 2:
            messagebox.showwarning("警告", "請先選取至少 2 個欲合併的向量檔案！")
            return

        merge_files = list(self.merge_files)
        out_merged = self._output_file(self.out_merge, merge_files[0].parent / "merged_layers.gpkg")
        if out_merged is None:
            return
        if out_merged in merge_files:
            messagebox.showwarning("警告", f"合併成果會寫到 {out_merged}，它不能同時是來源檔，請先把它移出清單或改名。")
            return
        assigned_srs = self._assigned_srs()

        def _task() -> Path:
            return merge_vector_layers(
                merge_files,
                out_merged,
                assigned_srs=assigned_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[合併成功] 已將 {len(merge_files)} 個圖層合併至: {res_p}\n")
            messagebox.showinfo("成功", f"多圖幅合併完成！\n成果檔案: {res_p}")

        self.inspector.run_task("向量合併", _task, on_success=_done)

    def _browse_simplify_file(self) -> None:
        self._browse_into(self.entry_simp_file, "選取 Shapefile", [("Shapefile", "*.shp")])

    def _handle_fix_encoding(self) -> None:
        f_path = self.entry_simp_file.get().strip()
        if not f_path:
            messagebox.showwarning("警告", "請先選取欲修復編碼之 Shapefile！")
            return

        out_utf8 = self._output_file(self.out_simplify, Path(f_path).parent / f"{Path(f_path).stem}_utf8.shp")
        if out_utf8 is None:
            return

        def _task() -> Path:
            return repair_shapefile_encoding(f_path, output_path=out_utf8, text_callback=self.inspector.append_log)

        def _done(out_p: Path) -> None:
            self.inspector.append_log(f"[編碼修復] 已將檔案轉存為標準 UTF-8 Shapefile: {out_p}\n")
            messagebox.showinfo("成功", f"已成功修復 Big5/CP950 編碼並重寫 .cpg！\n輸出檔案: {out_p}")

        self.inspector.run_task("編碼修復", _task, on_success=_done)

    def _handle_simplify(self) -> None:
        f_path = self.entry_simp_file.get().strip()
        if not f_path:
            messagebox.showwarning("警告", "請先選取欲抽稀之 Shapefile！")
            return

        out_simp = self._output_file(self.out_simplify, Path(f_path).parent / f"{Path(f_path).stem}_simp1m.shp")
        if out_simp is None:
            return
        assigned_srs = self._assigned_srs()

        def _task() -> Path:
            return simplify_vector_geometry(
                f_path, out_simp, tolerance=1.0, assigned_srs=assigned_srs, text_callback=self.inspector.append_log
            )

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[抽稀成功] 幾何頂點已依 1.0m 容許度精簡瘦身: {res_p}\n")
            messagebox.showinfo("成功", f"向量頂點抽稀完成！\n輸出檔案: {res_p}")

        self.inspector.run_task("頂點抽稀", _task, on_success=_done)

    def _browse_csv_file(self) -> None:
        self._browse_into(self.entry_csv_file, "選取 CSV 表格", [("CSV 表格", "*.csv;*.txt")])

    def _handle_convert_csv_to_points(self) -> None:
        f_path = self.entry_csv_file.get().strip()
        if not f_path:
            messagebox.showwarning("警告", "請先選取 CSV 表格檔案！")
            return

        out_gpkg = self._output_file(self.out_csv, Path(f_path).parent / f"{Path(f_path).stem}_points.gpkg")
        if out_gpkg is None:
            return
        assigned_srs = self._assigned_srs()

        def _task() -> Path:
            return convert_csv_table_to_points(
                f_path, out_gpkg, assigned_srs=assigned_srs, text_callback=self.inspector.append_log
            )

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[表格轉點] 已將 CSV 座標表成功轉為空間點位圖層: {res_p}\n")
            messagebox.showinfo("成功", f"座標表格已成功轉為點位向量圖資！\n輸出檔案: {res_p}")

        self.inspector.run_task("表格轉點", _task, on_success=_done)

    def _browse_split_file(self) -> None:
        self._browse_into(self.entry_split_file, "選取分割圖資", [VECTOR_FILETYPES])

    def _handle_run_split_by_attribute(self) -> None:
        f_path = self.entry_split_file.get().strip()
        col = self.entry_split_col.get().strip()
        if not f_path or not col:
            messagebox.showwarning("警告", "請指定欲分割之向量檔案與屬性欄位名稱！")
            return

        try:
            out_split_dir = self.out_split.chosen() or Path(f_path).parent / f"split_{col}"
        except ValueError as exc:
            messagebox.showwarning("警告", str(exc))
            return
        assigned_srs = self._assigned_srs()

        def _task() -> List[Path]:
            return split_vector_by_attribute(
                f_path, col, out_split_dir, assigned_srs=assigned_srs, text_callback=self.inspector.append_log
            )

        def _done(res_files: List[Path]) -> None:
            self.inspector.append_log(f"[拆檔完成] 已拆分為 {len(res_files)} 個獨立圖層檔案於 {out_split_dir}\n")
            messagebox.showinfo("成功", f"屬性拆檔作業完成！\n共產出: {len(res_files)} 個獨立檔案\n目錄: {out_split_dir}")

        self.inspector.run_task("屬性拆檔", _task, on_success=_done)

    def _browse_dxf_split_file(self) -> None:
        self._browse_into(self.entry_dxf_split_file, "選取 DXF", [("DXF 圖檔", "*.dxf")])

    def _handle_split_dxf_by_geometry(self) -> None:
        f_path = self.entry_dxf_split_file.get().strip()
        if not f_path:
            messagebox.showwarning("警告", "請先選取要分層的 DXF 檔案！")
            return
        if is_unsupported_cad(f_path):
            messagebox.showwarning("警告", UNSUPPORTED_CAD_MESSAGE)
            return
        assigned_srs = self._assigned_srs()
        try:
            out_dir = self.out_dxf_split.chosen() or Path(f_path).resolve().parent
        except ValueError as exc:
            messagebox.showwarning("警告", str(exc))
            return

        def _task() -> DxfGeometrySplitResult:
            return split_dxf_by_geometry_type(
                f_path, out_dir, assigned_srs=assigned_srs, text_callback=self.inspector.append_log
            )

        def _done(res: DxfGeometrySplitResult) -> None:
            counts = "、".join(
                f"{DXF_LAYER_DISPLAY_NAMES.get(name, name)} {count} 個" for name, count in res.layer_feature_counts.items()
            )
            skipped = f"；另有 {res.skipped_feature_count} 個非點線面圖元被略過" if res.skipped_feature_count else ""
            self.inspector.append_log(f"[DXF 分層完成] {counts}{skipped}: {res.output_gpkg}\n")
            messagebox.showinfo("成功", f"DXF 分層完成！\n{counts}{skipped}\n輸出檔案: {res.output_gpkg}")

        self.inspector.run_task("DXF 分層", _task, on_success=_done)
