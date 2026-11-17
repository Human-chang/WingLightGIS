"""WingLightGIS 主視窗框架 (ui/dashboard_app.py)

照 WingLight 設計規範的「側欄工作台」版面：
1. 左邊側欄 (ui/sidebar.py，208 px)：「檢視」組的圖資快覽 (ui/tab_quick_view.py)，「裁切工具」組的 DEM／DSM、
   正射影像、SHP 三個裁切工具；最下面是測試版工具開關、深淺色切換與「關於」(ui/about_dialog.py，第一次點才載入)。
   測試版工具 (ui/lab/) 的開關打開時，側欄多一組「測試版工具」(向量與 CAD、地形與水文)，開關記在設定檔；
   發佈版整個 ui/lab 拿掉，這時沒有開關也沒有那一組 (lab 是 None)。
2. 右邊主區：目前那個工具的頁面 (圖資快覽頁，或 TabClipWorkflow 各一個實例：標題列、三張編號卡片、底部動作列)，
   右側檢視欄 (PanelInspector) 各頁共用，切換工具時搬到該頁的位置。每個工具各自記住已經填的東西。
3. 主題：WingLight 的淺色／深色兩套色票 (ui/theme_constants.py)；第一次開啟跟著 Windows，
   使用者切換後記在 config/WingLightGIS.json (ui/settings.py)。
4. 視窗預設 1360x840，最小 1100x680；標題列用 Windows 原本的，圖示是 App 圖示 (ui/app_icon.py)。主區太窄時先收起檢視欄。
5. 快速鍵：Ctrl+1～Ctrl+4 依側欄順序切換工具 (測試版工具打開時再加 Ctrl+5、Ctrl+6)。
6. 主視窗第一次出現並畫好時，通知 WingLightGIS.exe 關掉螢火蟲啟動畫面 (ui/launcher_link.py)。
7. 開程式時停在圖資快覽 (start_tool 可以改，測試用來直接開在裁切工具)。啟動時只建立看得到的那一頁：
   三個裁切工具頁第一次點到才建立 (各約 0.4 秒)，測試版工具的頁 (tab_vector、tab_terrain，約 0.9 秒) 也是第一次取用才建立。
8. 命令列帶的檔案與之後再開的那幾份交來的檔案 (ui/single_instance.py) 用 open_paths 加進圖資快覽；
   主視窗畫好、啟動畫面關掉之前收到的先存著，畫好才加 (提示視窗才不會被啟動畫面蓋住)。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

import customtkinter as ctk

from core.env_runner import check_environment_health
from ui import drag_drop
from ui.app_icon import apply_window_icon
from ui.app_info import APP_NAME
from ui.icons import icon
from ui.launcher_link import notify_launcher
from ui.panel_inspector import PanelInspector
from ui.settings import save_appearance_mode, save_lab_tools_enabled, saved_appearance_mode, saved_lab_tools_enabled
from ui.sidebar import ITEM_ICON_SIZE, Sidebar
from ui.tab_clip_workflow import CLIP_KIND_DEM, CLIP_KIND_ORTHO, CLIP_KIND_SHP, CLIP_KINDS, ClipKind, TabClipWorkflow
from ui.tab_quick_view import TOOL_ICON as VIEW_TOOL_ICON
from ui.tab_quick_view import TOOL_KEY as VIEW_TOOL_KEY
from ui.tab_quick_view import TOOL_TITLE as VIEW_TOOL_TITLE
from ui.tab_quick_view import TabQuickView
from ui.theme_constants import T, apply_ctk_theme

if TYPE_CHECKING:
    from ui.about_dialog import AboutDialog
    from ui.lab.lab_page import LabToolPage
    from ui.lab.tab_terrain_hydro import TabTerrainHydro
    from ui.lab.tab_vector_cad import TabVectorCad
    from ui.single_instance import InstanceServer

logger = logging.getLogger("ui.dashboard_app")

# 測試版工具 (ui/lab/)：發佈版整個資料夾拿掉，找不到時是 None (側欄沒有開關也沒有那一組)。
# 主程式只有這裡碰 ui.lab；它的 __init__ 只有清單，工具分頁到第一次取用才載入
lab: Optional[ModuleType] = importlib.import_module("ui.lab") if importlib.util.find_spec("ui.lab") else None
LAB_TOGGLE_TEXTS: Dict[bool, str] = {True: "測試版工具：開", False: "測試版工具：關"}

WINDOW_SIZE: str = "1360x840"
WINDOW_MIN_SIZE = (1100, 680)
SIDEBAR_GROUP_VIEW: str = "檢視"
SIDEBAR_GROUP_CLIP: str = "裁切工具"
# 主區寬度 (邏輯像素，未乘螢幕縮放) 小於這個值時先收起右側檢視欄，讓卡片有地方放
INSPECTOR_COLLAPSE_BELOW: int = 860


class LazyToolPages(Mapping[str, TabClipWorkflow]):
    """三個裁切工具頁，以 ClipKind.key 為鍵；第一次取用 pages[key] 才建立那一頁。

    鍵永遠是三個 (側欄與快速鍵照順序用)；built() 只回傳已經建好的頁，
    切換工具、收起檢視欄用它，才不會順便把其他頁建出來。
    """

    def __init__(self, kinds: Sequence[ClipKind], factory: Callable[[ClipKind], TabClipWorkflow]) -> None:
        self._kinds: Dict[str, ClipKind] = {kind.key: kind for kind in kinds}
        self._factory = factory
        self._built: Dict[str, TabClipWorkflow] = {}

    def __getitem__(self, key: str) -> TabClipWorkflow:
        if key not in self._built:
            self._built[key] = self._factory(self._kinds[key])
        return self._built[key]

    def __contains__(self, key: object) -> bool:
        return key in self._kinds

    def __iter__(self) -> Iterator[str]:
        return iter(self._kinds)

    def __len__(self) -> int:
        return len(self._kinds)

    def built(self) -> Dict[str, TabClipWorkflow]:
        return dict(self._built)


class GisMasterToolkitApp(ctk.CTk):
    """WingLightGIS 主應用程式視窗。"""

    def __init__(self, start_tool: str = VIEW_TOOL_KEY) -> None:
        # 主題要在建立主視窗之前換好：視窗底色與所有沒指定顏色的元件都讀主題。
        # 主題也把預設字型從 Roboto (Windows 沒有) 換成微軟正黑體 13 px
        apply_ctk_theme()
        super().__init__()

        self.title(APP_NAME)
        # 標題列與工作列的 App 圖示 (之後新建的 Tk 子視窗也用它)
        apply_window_icon(self, as_default=True)
        self.geometry(WINDOW_SIZE)
        self.minsize(*WINDOW_MIN_SIZE)
        # 第一次開啟跟著 Windows 的深淺色；使用者選過就用記住的
        ctk.set_appearance_mode(saved_appearance_mode() or "System")

        # 拖放要在建立元件前載入；失敗時只是不能拖放
        self._drag_drop_ready = drag_drop.enable_drag_drop(self)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(2, weight=1)
        self.sidebar = Sidebar(self)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        # 側欄右緣 1 px 分隔線
        ctk.CTkFrame(self, width=1, corner_radius=0, fg_color=T["line"]).grid(row=0, column=1, sticky="ns")
        self.main_area = ctk.CTkFrame(self, corner_radius=0, fg_color=T["bg"])
        self.main_area.grid(row=0, column=2, sticky="nsew")
        self.main_area.grid_rowconfigure(0, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        # 檢視欄先建立 (各工具頁要用它)，切換工具時由工具頁放到自己的位置
        self.inspector_panel = PanelInspector(self.main_area)
        self.inspector_collapsed: bool = False
        self._build_tools()
        self._build_sidebar_bottom()
        # Ctrl+數字照側欄上看得到的順序；測試版工具關著時 Ctrl+5、Ctrl+6 沒有作用
        for number in range(1, len(self._all_tool_keys) + 1):
            self.bind(f"<Control-Key-{number}>", lambda _event, n=number: self._select_tool_number(n))
        self.current_tool: str = ""
        self.select_tool(start_tool)
        self.main_area.bind("<Configure>", self._on_main_resize, add="+")

        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        # 主視窗第一次出現時叫啟動器關掉啟動畫面
        self._shown_once: bool = False
        # 畫好 (通知過啟動器) 之前要開的檔先存著
        self.ready: bool = False
        self._pending_paths: List[str] = []
        self.bind("<Map>", self._on_first_map, add="+")
        # 啟動時執行沙盒環境健康檢查
        self.after(200, self._perform_initial_health_check)

    # -------------------------------------------------------------------------
    # 工具頁與側欄
    # -------------------------------------------------------------------------
    def _build_tools(self) -> None:
        """側欄的工具：圖資快覽 (第一次取用才建立)，三個獨立的裁切工具 (清單、邊界、選項各自一份；
        clip_tabs 以固定代號 ClipKind.key 為鍵)，以及測試版工具 (有 ui/lab 才有，開關打開才顯示)。"""
        self.sidebar.add_group(SIDEBAR_GROUP_VIEW)
        self._tab_view: Optional[TabQuickView] = None
        self.sidebar.add_item(VIEW_TOOL_KEY, VIEW_TOOL_TITLE, VIEW_TOOL_ICON, command=lambda: self.select_tool(VIEW_TOOL_KEY))
        self.sidebar.add_group(SIDEBAR_GROUP_CLIP)
        self.clip_tabs = LazyToolPages(CLIP_KINDS, self._create_clip_tab)
        for kind in CLIP_KINDS:
            self.sidebar.add_item(kind.key, kind.tab_title, kind.icon_name, command=lambda key=kind.key: self.select_tool(key))
        self._all_tool_keys: List[str] = [VIEW_TOOL_KEY, *self.clip_tabs]
        # 測試版工具：側欄可以整組藏起來的一區；各頁第一次點到 (或取用 lab_page) 才建立
        self.lab_pages: Dict[str, LabToolPage] = {}
        self.lab_section: Optional[ctk.CTkFrame] = None
        self.lab_enabled: bool = False
        if lab is not None:
            self.lab_section = self.sidebar.add_section()
            self.sidebar.add_group(lab.LAB_GROUP_TITLE, section=self.lab_section)
            for tool in lab.LAB_TOOLS:
                self.sidebar.add_item(
                    tool.key, tool.title, tool.icon_name, command=lambda key=tool.key: self.select_tool(key), section=self.lab_section
                )
                self._all_tool_keys.append(tool.key)
            self.lab_enabled = saved_lab_tools_enabled()
            self.sidebar.set_section_visible(self.lab_section, self.lab_enabled)

    @property
    def tool_keys(self) -> List[str]:
        """側欄上看得到的工具，照側欄 (與快速鍵) 的順序。"""
        lab_keys = {tool.key for tool in lab.LAB_TOOLS} if lab is not None and not self.lab_enabled else set()
        return [key for key in self._all_tool_keys if key not in lab_keys]

    def _select_tool_number(self, number: int) -> None:
        keys = self.tool_keys
        if number <= len(keys):
            self.select_tool(keys[number - 1])

    def _create_clip_tab(self, kind: ClipKind) -> TabClipWorkflow:
        tab = TabClipWorkflow(self.main_area, inspector=self.inspector_panel, kind=kind)
        if self.inspector_collapsed:
            tab.set_inspector_visible(False)
        return tab

    @property
    def tab_view(self) -> TabQuickView:
        if self._tab_view is None:
            self._tab_view = TabQuickView(self.main_area, inspector=self.inspector_panel)
            if self.inspector_collapsed:
                self._tab_view.set_inspector_visible(False)
        return self._tab_view

    @property
    def tab_dem(self) -> TabClipWorkflow:
        return self.clip_tabs[CLIP_KIND_DEM.key]

    @property
    def tab_ortho(self) -> TabClipWorkflow:
        return self.clip_tabs[CLIP_KIND_ORTHO.key]

    @property
    def tab_shp(self) -> TabClipWorkflow:
        return self.clip_tabs[CLIP_KIND_SHP.key]

    @property
    def tab_clip(self) -> TabClipWorkflow:
        """舊名稱：既有測試與程式用 tab_clip 指第一頁。"""
        return self.tab_dem

    def lab_page(self, key: str) -> LabToolPage:
        """測試版工具的一頁；第一次取用才建立 (開關關著時也可以建，只是不顯示)。"""
        if key not in self.lab_pages:
            if lab is None:
                raise KeyError(f"沒有測試版工具 (找不到 ui/lab): {key}")
            # 模組也到這時才載入 (工具分頁會連帶載入 openpyxl 等，約 0.2 秒)
            from ui.lab.lab_page import LabToolPage

            tool = next(tool for tool in lab.LAB_TOOLS if tool.key == key)
            page = LabToolPage(self.main_area, inspector=self.inspector_panel, tool=tool)
            if self.inspector_collapsed:
                page.set_inspector_visible(False)
            self.lab_pages[key] = page
        return self.lab_pages[key]

    @property
    def tab_vector(self) -> TabVectorCad:
        """測試版工具「向量與 CAD」的工具分頁 (原分頁 2)。"""
        return self.lab_page(lab.LAB_KEY_VECTOR).content

    @property
    def tab_terrain(self) -> TabTerrainHydro:
        """測試版工具「地形與水文」的工具分頁 (原分頁 3)。"""
        return self.lab_page(lab.LAB_KEY_TERRAIN).content

    def built_tool_pages(self) -> Dict[str, Any]:
        """已經建好的工具頁 (圖資快覽、裁切工具、測試版工具)，以工具代號為鍵。"""
        pages: Dict[str, Any] = {}
        if self._tab_view is not None:
            pages[VIEW_TOOL_KEY] = self._tab_view
        pages.update(self.clip_tabs.built())
        pages.update(self.lab_pages)
        return pages

    def select_tool(self, key: str) -> None:
        """切換工具：主區換成那一頁 (其他頁的內容保留)，檢視欄換成那一頁自己的體檢與預覽。"""
        if key not in self.tool_keys:
            raise KeyError(f"沒有這個工具: {key}")
        for other_key, page in self.built_tool_pages().items():
            if other_key != key:
                page.grid_remove()
                page.deactivate()
        # 還沒建過的頁這時才建立
        if key == VIEW_TOOL_KEY:
            tab = self.tab_view
        elif key in self.clip_tabs:
            tab = self.clip_tabs[key]
        else:
            tab = self.lab_page(key)
        tab.grid(row=0, column=0, sticky="nsew")
        tab.activate()
        self.sidebar.select(key)
        self.current_tool = key

    def open_paths(self, paths: Sequence[str], raise_window: bool = True) -> None:
        """命令列或後開的那一份交來的檔案：切到圖資快覽加進清單，並把視窗叫到前面。

        主視窗還沒畫好時先存著，畫好才一起加 (_notify_launcher_ready)。"""
        if not self.ready:
            self._pending_paths.extend(paths)
            return
        # 沒帶檔案 (再點一次 exe) 只叫到前面，不換頁
        if paths:
            self.select_tool(VIEW_TOOL_KEY)
            self.tab_view.add_paths([Path(p) for p in paths], select_opened=True)
        if raise_window:
            self.bring_to_front()

    def bring_to_front(self) -> None:
        """縮小的還原，並拉到最前面 (暫時設成最上層再取消：Windows 不准搶前景時至少蓋在其他視窗上)。"""
        if self.state() == "iconic":
            self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after_idle(self.attributes, "-topmost", False)
        self.focus_force()

    def _on_main_resize(self, event: Any) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self.main_area)
        self.set_inspector_collapsed(event.width / scaling < INSPECTOR_COLLAPSE_BELOW)

    def set_inspector_collapsed(self, collapsed: bool) -> None:
        """收起或放回右側檢視欄 (每個工具頁都跟著)。"""
        if collapsed == self.inspector_collapsed:
            return
        self.inspector_collapsed = collapsed
        # 還沒建立的頁建立時會照目前的狀態 (_create_clip_tab、tab_view)
        for page in self.built_tool_pages().values():
            page.set_inspector_visible(not collapsed)

    def _build_sidebar_bottom(self) -> None:
        self.btn_lab: Optional[ctk.CTkButton] = None
        if self.lab_section is not None:
            self.btn_lab = self.sidebar.add_bottom_item("", "package", self.toggle_lab_tools)
            self._update_lab_item()
        self.btn_theme = self.sidebar.add_bottom_item("", "sun", self.toggle_appearance)
        self._update_theme_item()
        self.btn_about = self.sidebar.add_bottom_item("關於", "info", self.open_about)
        self.about_window: Optional[AboutDialog] = None
        # 跟著 Windows 時，系統切換深淺色也要更新這一項
        ctk.AppearanceModeTracker.add(self._on_appearance_changed, self)

    def open_about(self) -> None:
        """開「關於」視窗 (已開著就拉到最前面)。"""
        if self.about_window is not None and self.about_window.winfo_exists():
            self.about_window.lift()
            self.about_window.focus_set()
            return
        # 模組到第一次點才載入，不影響啟動時間
        from ui.about_dialog import AboutDialog

        self.about_window = AboutDialog(self)

    def _on_appearance_changed(self, _mode: str) -> None:
        self._update_theme_item()

    def destroy(self) -> None:
        ctk.AppearanceModeTracker.remove(self._on_appearance_changed)
        super().destroy()

    # -------------------------------------------------------------------------
    # 測試版工具開關
    # -------------------------------------------------------------------------
    def toggle_lab_tools(self) -> None:
        self.set_lab_tools_enabled(not self.lab_enabled)

    def set_lab_tools_enabled(self, enabled: bool) -> None:
        """打開／關掉側欄的「測試版工具」組，並記在設定檔；關掉時正在用測試版工具就切回圖資快覽。"""
        if self.lab_section is None:
            return
        self.lab_enabled = enabled
        save_lab_tools_enabled(enabled)
        self.sidebar.set_section_visible(self.lab_section, enabled)
        self._update_lab_item()
        if self.current_tool not in self.tool_keys:
            self.select_tool(VIEW_TOOL_KEY)

    def _update_lab_item(self) -> None:
        if self.btn_lab is not None:
            self.btn_lab.configure(text=LAB_TOGGLE_TEXTS[self.lab_enabled])

    # -------------------------------------------------------------------------
    # 深淺色
    # -------------------------------------------------------------------------
    def toggle_appearance(self) -> None:
        """切換深色／淺色，並記在設定檔。"""
        new_mode = "Light" if ctk.get_appearance_mode() == "Dark" else "Dark"
        ctk.set_appearance_mode(new_mode)
        save_appearance_mode(new_mode)
        self._update_theme_item()

    def _update_theme_item(self) -> None:
        """側欄的深淺色項目顯示目前是哪一種：太陽＝淺色、月亮＝深色。"""
        dark = ctk.get_appearance_mode() == "Dark"
        self.btn_theme.configure(
            text="深色" if dark else "淺色",
            image=icon("moon" if dark else "sun", role="muted", size=ITEM_ICON_SIZE),
        )

    # -------------------------------------------------------------------------
    # 啟動診斷與結束
    # -------------------------------------------------------------------------
    def _perform_initial_health_check(self) -> None:
        """啟動時在背景檢查獨立沙盒與工具鏈配置健全度。"""
        health = check_environment_health()
        if not health.is_healthy:
            msg = "【環境提醒】未偵測到完整的 ./runtime 獨立環境。\n"
            for w in health.warnings:
                msg += f"• {w}\n"
            msg += "\n若需使用完整功能，請點擊根目錄下的 'setup_portable_env.bat' 初始化環境。"
            self.inspector_panel.append_log(f"[環境警告] {msg}\n")
        else:
            self.inspector_panel.append_log("[環境檢測] 獨立沙盒 ./runtime 已就緒，GDAL/OGR C++ 原生工具鏈完整！\n")
        if not self._drag_drop_ready:
            self.inspector_panel.append_log("[環境提醒] 拖放功能無法啟用，請改用各欄位的瀏覽按鈕選檔。\n")

    def _on_first_map(self, event: Any) -> None:
        # 綁在主視窗上的 <Map> 也會收到每個子元件的 Map，只看主視窗自己的第一次
        if event.widget is not self or self._shown_once:
            return
        self._shown_once = True
        self.after_idle(self._notify_launcher_ready)

    def _notify_launcher_ready(self) -> None:
        # 先把這一輪的版面與重畫做完，啟動畫面關掉時主視窗已經畫好
        self.update_idletasks()
        notify_launcher()
        self.ready = True
        if self._pending_paths:
            paths, self._pending_paths = self._pending_paths, []
            self.after_idle(self.open_paths, paths, False)

    def _on_window_close(self) -> None:
        """視窗關閉前執行清理與退出。"""
        logger.info("使用者關閉主視窗，正在結束應用程式。")
        self.destroy()
        sys.exit(0)


def launch_app(paths: Sequence[str] = (), server: Optional[InstanceServer] = None) -> None:
    """啟動 CustomTkinter 圖形介面應用程式。paths：命令列帶的檔案；server：收後開的那幾份交來的檔案。"""
    app = GisMasterToolkitApp()
    app.open_paths(paths, raise_window=False)
    if server is not None:
        # 交來的檔在背景執行緒收到，排回主執行緒加
        server.set_handler(lambda handed: app.inspector_panel.post_to_ui(lambda: app.open_paths(handed)))
    app.mainloop()


if __name__ == "__main__":
    launch_app()
