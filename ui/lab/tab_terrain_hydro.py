"""測試版工具「地形與水文」(ui/lab/tab_terrain_hydro.py)；放在 ui/lab/lab_page.py 的頁面裡 (原分頁 3)

卡片 3「坡度與等高線」跟 DEM／DSM 裁切重複，不放進切換列 (卡片照樣建立，程式與測試保留)。

嚴格遵守專案規格書第 4 節規範：
0. 最上方一列「無標籤圖資的來源座標系」下拉選單，套用到本頁所有卡片。
1. 包含 5 大工程水文卡片：
   - 卡片 1：[A-01] 兩期 DEM 高程差與土方體積計算（前期/後期 DEM 對齊、挖填土方量、淨土方、Excel 報表）。
   - 卡片 2：[A-02] 水土保持 1~6 級坡度規範分類與面積統計（百分比重分類、轉多邊形向量、Excel 清冊）。
   - 卡片 3：[G-14, G-15] 地形坡度坡向計算 與 等高線自動抽繪（首曲線/計曲線、DXF/SHP 輸出）。
   - 卡片 4：[A-03] 路線縱橫斷面地面高程自動擷取（樁號間距 20m、法線斷面、里程高程 CSV 匯出）。
   - 卡片 5：[A-04] 水文集水區劃設（WhiteboxTools 填窪、流向、出水口捕捉、集水區面積圈繪）。
2. 所有按鈕皆經 PanelInspector.run_task 在背景執行：日誌、進度、完成與錯誤對話框都回到主執行緒，
   執行中可按右欄的「取消作業」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Dict, List, Optional, Tuple

import customtkinter as ctk

from ui.theme_constants import FONT_FAMILY, FONT_SIZE_BODY, FONT_SIZE_CAPTION, FONT_SIZE_CARD_TITLE
from core.preview_engine import export_raster_preview_png
from core.terrain_lab import (
    EarthworkResult,
    SlopeClassificationResult,
    WatershedResult,
    calculate_aspect,
    calculate_earthwork,
    calculate_slope,
    classify_slope_soil_conservation,
    delineate_watershed_whitebox,
    extract_road_profile_and_cross_sections,
    generate_contours,
)
from ui import drag_drop
from ui.file_guards import validate_supported_files
from ui.output_dir_row import OutputDirRow
from ui.panel_inspector import PanelInspector
from ui.theme_constants import BTN_PRIMARY, BTN_SECONDARY, CARD_FG_COLOR, T
from ui.lab.tool_switcher import ToolCardSwitcher
from ui.srs_options import (
    AUTO_SOURCE_SRS_LABEL,
    POUR_POINT_SAME_AS_DEM_LABEL,
    SOURCE_SRS_HINT,
    assigned_srs_from_label,
    pour_point_srs_from_label,
    pour_point_srs_labels,
    source_srs_labels,
)

logger = logging.getLogger("ui.tab_terrain_hydro")

# 頂部切換鈕的工具名稱 (順序即顯示順序)
TOOL_EARTHWORK: str = "兩期土方計算"
TOOL_SLOPE_CLASS: str = "水保坡度分類"
# 跟 DEM／DSM 裁切重複，不在切換列 (卡片照樣建立)
TOOL_SLOPE_CONTOUR: str = "坡度與等高線"
TOOL_CROSS_SECTION: str = "斷面高程擷取"
TOOL_WATERSHED: str = "集水區劃設"
TERRAIN_TOOL_NAMES: Tuple[str, ...] = (
    TOOL_EARTHWORK,
    TOOL_SLOPE_CLASS,
    TOOL_CROSS_SECTION,
    TOOL_WATERSHED,
)

DEM_FILETYPES: Tuple[str, str] = ("DEM 網格", "*.tif;*.tiff;*.dem;*.asc;*.img")

# 等高線間距下拉選單：文字 -> 間距 (公尺)
CONTOUR_INTERVAL_CHOICES: Dict[str, float] = {
    "2.0 公尺 (精細)": 2.0,
    "5.0 公尺 (標準首曲線)": 5.0,
    "10.0 公尺": 10.0,
    "25.0 公尺 (計曲線)": 25.0,
}


class TabTerrainHydro(ctk.CTkScrollableFrame):
    """測試版工具「地形與水文」的工作區。"""

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
        self.tool_switcher = ToolCardSwitcher(self, TERRAIN_TOOL_NAMES, row=1, font=self.font_body)
        self._build_card_earthwork()
        self._build_card_slope_conservation()
        self._build_card_slope_contours()
        self._build_card_cross_sections()
        self._build_card_watershed()
        self.tool_switcher.show(TERRAIN_TOOL_NAMES[0])

        for entry in (
            self.entry_dem_before,
            self.entry_dem_after,
            self.entry_slope_dem,
            self.entry_terrain_dem,
            self.entry_road_line,
            self.entry_road_dem,
            self.entry_ws_dem,
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
    # 卡片 1：兩期 DEM 土方計算
    # -------------------------------------------------------------------------
    def _build_card_earthwork(self) -> None:
        """建立 [A-01] 兩期 DEM 高程差與土方計算卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_EARTHWORK, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[A-01] 兩期 DEM 高程差 (ΔZ) 與挖填土方體積計算", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        # 前期 DEM
        lbl_b = ctk.CTkLabel(card, text="前期 DEM (Before):", font=self.font_caption)
        lbl_b.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_dem_before = ctk.CTkEntry(card, placeholder_text="請選取開工前或災害前 DEM GeoTIFF")
        self.entry_dem_before.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_b = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_dem_before, **BTN_SECONDARY)
        btn_b.grid(row=1, column=2, padx=6, pady=2)

        # 後期 DEM
        lbl_a = ctk.CTkLabel(card, text="後期 DEM (After):", font=self.font_caption)
        lbl_a.grid(row=2, column=0, padx=6, pady=2, sticky="w")
        self.entry_dem_after = ctk.CTkEntry(card, placeholder_text="請選取完工後或復建後 DEM GeoTIFF")
        self.entry_dem_after.grid(row=2, column=1, padx=4, pady=2, sticky="ew")
        btn_a = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_dem_after, **BTN_SECONDARY)
        btn_a.grid(row=2, column=2, padx=6, pady=2)

        self.out_earthwork = OutputDirRow(card, row=3, font=self.font_caption)

        # 執行按鈕
        btn_run = ctk.CTkButton(
            card,
            text="執行網格對齊、計算挖填體積並產出 Excel 報表與紅藍色階圖",
            font=self.font_btn,
            command=self._handle_calculate_earthwork,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 2：水保 1~6 級坡度分類
    # -------------------------------------------------------------------------
    def _build_card_slope_conservation(self) -> None:
        """建立 [A-02] 水土保持 1~6 級坡度分類與清冊卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_SLOPE_CLASS, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[A-02] 水土保持 1～6 級坡度規範分類與面積統計", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_dem = ctk.CTkLabel(card, text="高程 DEM:", font=self.font_caption)
        lbl_dem.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_slope_dem = ctk.CTkEntry(card, placeholder_text="請選取待分析坡度之 DEM 檔案")
        self.entry_slope_dem.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_dem = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_slope_dem, **BTN_SECONDARY)
        btn_dem.grid(row=1, column=2, padx=6, pady=2)

        self.out_slope_class = OutputDirRow(card, row=2, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="重分類坡度、向量多邊形化並匯出水保 Excel 報表",
            font=self.font_btn,
            command=self._handle_classify_slope,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 3：坡度坡向與等高線抽繪
    # -------------------------------------------------------------------------
    def _build_card_slope_contours(self) -> None:
        """建立 [G-14, G-15] 坡度坡向計算與等高線抽繪卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        # 不放進切換列，所以不會顯示 (TOOL_SLOPE_CONTOUR)
        self.card_slope_contours = card
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[G-14, G-15] 地形坡度坡向計算 與 等高線自動抽繪", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_dem = ctk.CTkLabel(card, text="地形 DEM:", font=self.font_caption)
        lbl_dem.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_terrain_dem = ctk.CTkEntry(card, placeholder_text="請選取高程 DEM 檔案")
        self.entry_terrain_dem.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_dem = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_terrain_dem, **BTN_SECONDARY)
        btn_dem.grid(row=1, column=2, padx=6, pady=2)

        # 間距與按鈕列
        frame_contour = ctk.CTkFrame(card, fg_color="transparent")
        frame_contour.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=2)
        frame_contour.grid_columnconfigure(1, weight=1)

        lbl_int = ctk.CTkLabel(frame_contour, text="等高線間距:", font=self.font_caption)
        lbl_int.grid(row=0, column=0, padx=4, pady=2, sticky="w")

        self.opt_contour_int = ctk.CTkOptionMenu(
            frame_contour,
            values=list(CONTOUR_INTERVAL_CHOICES),
        )
        self.opt_contour_int.grid(row=0, column=1, padx=4, pady=2, sticky="ew")
        self.opt_contour_int.set("5.0 公尺 (標準首曲線)")

        self.out_terrain = OutputDirRow(card, row=3, font=self.font_caption)

        frame_btns = ctk.CTkFrame(card, fg_color="transparent")
        frame_btns.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        frame_btns.grid_columnconfigure((0, 1, 2), weight=1)

        btn_calc_slope = ctk.CTkButton(
            frame_btns,
            text="坡度圖 (%)",
            font=self.font_btn,
            command=self._handle_calc_slope_pct,
            **BTN_PRIMARY,
        )
        btn_calc_slope.grid(row=0, column=0, padx=2, pady=2, sticky="ew")

        btn_calc_aspect = ctk.CTkButton(
            frame_btns,
            text="坡向圖 (Aspect)",
            font=self.font_btn,
            command=self._handle_calc_aspect,
            **BTN_PRIMARY,
        )
        btn_calc_aspect.grid(row=0, column=1, padx=2, pady=2, sticky="ew")

        btn_gen_contour = ctk.CTkButton(
            frame_btns,
            text="抽繪等高線 (SHP)",
            font=self.font_btn,
            command=self._handle_generate_contours,
            **BTN_PRIMARY,
        )
        btn_gen_contour.grid(row=0, column=2, padx=2, pady=2, sticky="ew")

    # -------------------------------------------------------------------------
    # 卡片 4：路線斷面高程擷取
    # -------------------------------------------------------------------------
    def _build_card_cross_sections(self) -> None:
        """建立 [A-03] 道路縱橫斷面地面高程自動擷取卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_CROSS_SECTION, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[A-03] 路線縱橫斷面地面高程自動擷取 (Profile)", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_line = ctk.CTkLabel(card, text="道路中心線:", font=self.font_caption)
        lbl_line.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_road_line = ctk.CTkEntry(card, placeholder_text="請選取道路中心線 .shp / .dxf")
        self.entry_road_line.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_line = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_road_line, **BTN_SECONDARY)
        btn_line.grid(row=1, column=2, padx=6, pady=2)

        lbl_dem = ctk.CTkLabel(card, text="高程 DEM:", font=self.font_caption)
        lbl_dem.grid(row=2, column=0, padx=6, pady=2, sticky="w")
        self.entry_road_dem = ctk.CTkEntry(card, placeholder_text="請選取數值高程模型 GeoTIFF")
        self.entry_road_dem.grid(row=2, column=1, padx=4, pady=2, sticky="ew")
        btn_dem = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_road_dem, **BTN_SECONDARY)
        btn_dem.grid(row=2, column=2, padx=6, pady=2)

        self.out_sections = OutputDirRow(card, row=3, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="每 20m 內插樁號、法線採樣並匯出里程高程表 CSV",
            font=self.font_btn,
            command=self._handle_extract_cross_sections,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 卡片 5：水文集水區劃設
    # -------------------------------------------------------------------------
    def _build_card_watershed(self) -> None:
        """建立 [A-04] 水文集水區自動劃設卡片。"""
        card = ctk.CTkFrame(self, corner_radius=8, fg_color=CARD_FG_COLOR)
        self.tool_switcher.register(TOOL_WATERSHED, card)
        card.grid_columnconfigure(1, weight=1)

        lbl_head = ctk.CTkLabel(card, text="[A-04] 水文流向分析與集水區自動劃設 (WhiteboxTools)", font=self.font_title, anchor="w")
        lbl_head.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))

        lbl_dem = ctk.CTkLabel(card, text="流域 DEM:", font=self.font_caption)
        lbl_dem.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        self.entry_ws_dem = ctk.CTkEntry(card, placeholder_text="請選取未切割之集水區 DEM")
        self.entry_ws_dem.grid(row=1, column=1, padx=4, pady=2, sticky="ew")
        btn_dem = ctk.CTkButton(card, text="瀏覽檔案", width=65, command=self._browse_watershed_dem, **BTN_SECONDARY)
        btn_dem.grid(row=1, column=2, padx=6, pady=2)

        lbl_pour = ctk.CTkLabel(card, text="出水口座標:", font=self.font_caption)
        lbl_pour.grid(row=2, column=0, padx=6, pady=2, sticky="w")
        self.entry_ws_pour = ctk.CTkEntry(card, placeholder_text="格式: X, Y (例如 250150.0, 2600200.0)")
        self.entry_ws_pour.grid(row=2, column=1, columnspan=2, padx=4, pady=2, sticky="ew")

        lbl_pour_srs = ctk.CTkLabel(card, text="出水口座標系:", font=self.font_caption)
        lbl_pour_srs.grid(row=3, column=0, padx=6, pady=2, sticky="w")
        self.opt_pour_srs = ctk.CTkOptionMenu(card, values=pour_point_srs_labels())
        self.opt_pour_srs.grid(row=3, column=1, columnspan=2, padx=4, pady=2, sticky="ew")
        self.opt_pour_srs.set(POUR_POINT_SAME_AS_DEM_LABEL)

        self.out_watershed = OutputDirRow(card, row=4, font=self.font_caption)

        btn_run = ctk.CTkButton(
            card,
            text="執行填窪、D8 流向分析並劃設上游集水區邊界",
            font=self.font_btn,
            command=self._handle_delineate_watershed,
            **BTN_PRIMARY,
        )
        btn_run.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

    # -------------------------------------------------------------------------
    # 事件處置實作
    # -------------------------------------------------------------------------
    def _assigned_srs(self) -> Optional[str]:
        """來源座標系下拉選單目前的指派值；自動判斷時為 None。"""
        return assigned_srs_from_label(self.opt_source_srs.get())

    @staticmethod
    def _output_folder(row: OutputDirRow, source_path: str, result_label: str) -> Optional[Path]:
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

    def _browse_dem_before(self) -> None:
        self._browse_into(self.entry_dem_before, "選取前期 DEM", [DEM_FILETYPES])

    def _browse_dem_after(self) -> None:
        self._browse_into(self.entry_dem_after, "選取後期 DEM", [DEM_FILETYPES])

    def _handle_calculate_earthwork(self) -> None:
        b_p = self.entry_dem_before.get().strip()
        a_p = self.entry_dem_after.get().strip()
        if not b_p or not a_p:
            messagebox.showwarning("警告", "請先選取前期與後期 DEM 檔案！")
            return
        assigned_srs = self._assigned_srs()
        out_dir = self._output_folder(self.out_earthwork, a_p, "土方成果")
        if out_dir is None:
            return

        def _task() -> Tuple[EarthworkResult, Optional[Path]]:
            out_dir.mkdir(parents=True, exist_ok=True)
            res = calculate_earthwork(
                b_p,
                a_p,
                output_diff_raster=out_dir / "earthwork_diff_dz.tif",
                output_excel_path=out_dir / "earthwork_summary_report.xlsx",
                assigned_srs=assigned_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )
            preview_png = export_raster_preview_png(res.color_raster_path) if res.color_raster_path else None
            return res, preview_png

        def _done(payload: Tuple[EarthworkResult, Optional[Path]]) -> None:
            res, preview_png = payload
            self.inspector.append_log(
                f"[土方完成] 填方: {res.total_fill_m3:,.2f} m³ | 挖方: {res.total_cut_m3:,.2f} m³ | 淨土方: {res.net_volume_m3:,.2f} m³\n"
                f"Excel 報表: {res.excel_report_path}\n"
                f"高程差圖: {res.diff_raster_path}\n"
            )
            color_line = ""
            if preview_png is not None and res.color_raster_path is not None:
                self.inspector.load_preview_image(preview_png)
                self.inspector.append_log(
                    f"紅藍色階圖: {res.color_raster_path} (紅=挖方、藍=填方，色階 ±{res.color_range_m:.2f} m；已載入右欄預覽)\n"
                )
                color_line = f"\n紅藍色階圖: {res.color_raster_path.name}"
            messagebox.showinfo(
                "土方計算成功",
                f"土方體積計算完成！\n總填方: {res.total_fill_m3:,.2f} m³\n總挖方: {res.total_cut_m3:,.2f} m³\n"
                f"淨土方差: {res.net_volume_m3:,.2f} m³\n報表檔案: {res.excel_report_path.name}{color_line}\n成果資料夾: {out_dir}",
            )

        self.inspector.run_task("土方計算", _task, on_success=_done)

    def _browse_slope_dem(self) -> None:
        self._browse_into(self.entry_slope_dem, "選取高程 DEM", [DEM_FILETYPES])

    def _handle_classify_slope(self) -> None:
        d_p = self.entry_slope_dem.get().strip()
        if not d_p:
            messagebox.showwarning("警告", "請選取欲分析之 DEM 檔案！")
            return
        assigned_srs = self._assigned_srs()
        out_dir = self._output_folder(self.out_slope_class, d_p, "坡度分類成果")
        if out_dir is None:
            return

        def _task() -> SlopeClassificationResult:
            return classify_slope_soil_conservation(
                d_p,
                output_dir=out_dir,
                assigned_srs=assigned_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )

        def _done(res: SlopeClassificationResult) -> None:
            self.inspector.append_log(
                f"[水保完成] 坡度分類清冊已產出: {res.excel_report_path}，總評估面積: {res.total_area_ha:,.4f} 公頃\n"
            )
            messagebox.showinfo(
                "水保坡度分類成功",
                f"1~6 級水保坡度分類完成！\n總評估面積: {res.total_area_ha:,.4f} 公頃 ({res.total_area_m2:,.2f} m²)\nExcel清冊: {res.excel_report_path.name}\n成果資料夾: {out_dir}",
            )

        self.inspector.run_task("水保坡度分類", _task, on_success=_done)

    def _browse_terrain_dem(self) -> None:
        self._browse_into(self.entry_terrain_dem, "選取地形 DEM", [DEM_FILETYPES])

    def _handle_calc_slope_pct(self) -> None:
        d_p = self.entry_terrain_dem.get().strip()
        if not d_p:
            messagebox.showwarning("警告", "請選取地形 DEM 檔案！")
            return
        out_slope = self._output_file(self.out_terrain, Path(d_p).parent / f"{Path(d_p).stem}_slope_pct.tif")
        if out_slope is None:
            return

        def _task() -> Path:
            return calculate_slope(d_p, out_slope, unit="PERCENT", text_callback=self.inspector.append_log)

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[坡度完成] 百分比坡度圖: {res_p}\n")
            messagebox.showinfo("成功", f"百分比坡度圖已產出:\n{res_p}")

        self.inspector.run_task("坡度計算", _task, on_success=_done)

    def _handle_calc_aspect(self) -> None:
        d_p = self.entry_terrain_dem.get().strip()
        if not d_p:
            messagebox.showwarning("警告", "請選取地形 DEM 檔案！")
            return
        out_aspect = self._output_file(self.out_terrain, Path(d_p).parent / f"{Path(d_p).stem}_aspect.tif")
        if out_aspect is None:
            return

        def _task() -> Path:
            return calculate_aspect(d_p, out_aspect, text_callback=self.inspector.append_log)

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[坡向完成] 坡向圖: {res_p}\n")
            messagebox.showinfo("成功", f"坡向圖已產出:\n{res_p}")

        self.inspector.run_task("坡向計算", _task, on_success=_done)

    def _handle_generate_contours(self) -> None:
        d_p = self.entry_terrain_dem.get().strip()
        if not d_p:
            messagebox.showwarning("警告", "請選取地形 DEM 檔案！")
            return

        interval = CONTOUR_INTERVAL_CHOICES.get(self.opt_contour_int.get(), 5.0)
        out_contour = self._output_file(self.out_terrain, Path(d_p).parent / f"{Path(d_p).stem}_contour_{interval:g}m.shp")
        if out_contour is None:
            return
        assigned_srs = self._assigned_srs()

        def _task() -> Path:
            return generate_contours(
                d_p,
                out_contour,
                interval=interval,
                assigned_srs=assigned_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )

        def _done(res_p: Path) -> None:
            self.inspector.append_log(f"[等高線完成] 間距 {interval:g}m: {res_p}\n")
            messagebox.showinfo("成功", f"等高線抽繪完成 (間距 {interval:g}m)！\n輸出檔案:\n{res_p}")

        self.inspector.run_task("等高線抽繪", _task, on_success=_done)

    def _browse_road_line(self) -> None:
        self._browse_into(self.entry_road_line, "選取道路中心線", [("向量線段", "*.shp;*.dxf;*.gpkg;*.geojson")])

    def _browse_road_dem(self) -> None:
        self._browse_into(self.entry_road_dem, "選取高程 DEM", [DEM_FILETYPES])

    def _handle_extract_cross_sections(self) -> None:
        l_p = self.entry_road_line.get().strip()
        d_p = self.entry_road_dem.get().strip()
        if not l_p or not d_p:
            messagebox.showwarning("警告", "請先指定道路中心線與 DEM 檔案！")
            return
        assigned_srs = self._assigned_srs()
        out_dir = self._output_folder(self.out_sections, l_p, "斷面成果")
        if out_dir is None:
            return

        def _task() -> Tuple[Path, Path]:
            return extract_road_profile_and_cross_sections(
                l_p,
                d_p,
                output_dir=out_dir,
                assigned_srs=assigned_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )

        def _done(paths: Tuple[Path, Path]) -> None:
            csv_p, gpkg_p = paths
            self.inspector.append_log(f"[斷面完成] 里程高程表: {csv_p}\n斷面點位: {gpkg_p}\n")
            messagebox.showinfo("成功", f"斷面高程擷取完成！\n里程高程 CSV: {csv_p.name}\n斷面點位 GPKG: {gpkg_p.name}\n成果資料夾: {out_dir}")

        self.inspector.run_task("斷面擷取", _task, on_success=_done)

    def _browse_watershed_dem(self) -> None:
        self._browse_into(self.entry_ws_dem, "選取流域 DEM", [DEM_FILETYPES])

    def _handle_delineate_watershed(self) -> None:
        d_p = self.entry_ws_dem.get().strip()
        pour_str = self.entry_ws_pour.get().strip()
        if not d_p:
            messagebox.showwarning("警告", "請選取流域 DEM 檔案！")
            return

        coords = [c.strip() for c in pour_str.replace("，", ",").split(",") if c.strip()]
        if len(coords) < 2:
            messagebox.showwarning("警告", "請輸入出水口 X, Y 座標數值！")
            return

        try:
            px, py = float(coords[0]), float(coords[1])
        except ValueError:
            messagebox.showerror("錯誤", "出水口座標必須為數值！")
            return
        assigned_srs = self._assigned_srs()
        pour_point_srs = pour_point_srs_from_label(self.opt_pour_srs.get())
        out_dir = self._output_folder(self.out_watershed, d_p, "集水區成果")
        if out_dir is None:
            return

        def _task() -> WatershedResult:
            return delineate_watershed_whitebox(
                d_p,
                (px, py),
                output_dir=out_dir,
                assigned_srs=assigned_srs,
                pour_point_srs=pour_point_srs,
                progress_callback=self.inspector.set_progress,
                text_callback=self.inspector.append_log,
            )

        def _done(res: WatershedResult) -> None:
            self.inspector.append_log(
                f"[集水區完成] 集水邊界已圈繪: {res.watershed_polygon}，面積 {res.catchment_area_ha:,.2f} 公頃\n"
            )
            messagebox.showinfo(
                "劃設完成",
                f"上游集水區劃設完成！\n集水面積: {res.catchment_area_ha:,.2f} 公頃 ({res.catchment_area_m2:,.0f} m²)\n向量多邊形: {res.watershed_polygon.name}\n成果資料夾: {out_dir}",
            )

        self.inspector.run_task("集水區劃設", _task, on_success=_done)
