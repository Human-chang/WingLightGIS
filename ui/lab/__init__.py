"""測試版工具 (ui/lab/)：還沒用實際案件驗證過的工具，側欄最下面的「測試版工具」打開才出現在側欄。

- 發佈版把整個 ui/lab 資料夾拿掉 (程式不在裡面，不是隱藏)；主程式找不到這個資料夾時照常啟動、也沒有開關
  (ui/dashboard_app.py 的 LAB_TOOLS)。主程式其他地方不要 import ui.lab。
- 計算函式在 core/，跟發佈版共用。
- LAB_TOOLS 是側欄「測試版工具」組的項目，照順序：向量與 CAD (6 個工具)、地形與水文 (4 個工具)。
  每一項是一頁 (ui/lab/lab_page.py)，頁面上方一列切換該頁的工具 (ui/lab/tool_switcher.py)。
  頁面第一次點到才建立，工具分頁的模組也到那時才載入 (會連帶載入 openpyxl 等)，這個檔不要 import 它們。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Tuple

if TYPE_CHECKING:
    from ui.panel_inspector import PanelInspector

LAB_GROUP_TITLE: str = "測試版工具"
LAB_KEY_VECTOR: str = "lab_vector"
LAB_KEY_TERRAIN: str = "lab_terrain"
# 每一頁標題下面的提醒 (warn 色配警告圖示)
LAB_NOTICE: str = "測試版，還沒用實際案件驗證過；成果的面積、座標與高程請自行核對後再使用。"


@dataclass(frozen=True)
class LabTool:
    """側欄「測試版工具」組的一項。factory(master, inspector, **kwargs) 建立工具分頁 (可捲動的框)。"""

    key: str
    title: str
    icon_name: str
    description: str
    factory: Callable[..., Any]


def _create_vector_tab(master: Any, inspector: PanelInspector, **kwargs: Any) -> Any:
    from ui.lab.tab_vector_cad import TabVectorCad

    return TabVectorCad(master, inspector=inspector, **kwargs)


def _create_terrain_tab(master: Any, inspector: PanelInspector, **kwargs: Any) -> Any:
    from ui.lab.tab_terrain_hydro import TabTerrainHydro

    return TabTerrainHydro(master, inspector=inspector, **kwargs)


LAB_TOOLS: Tuple[LabTool, ...] = (
    LabTool(
        key=LAB_KEY_VECTOR,
        title="向量與 CAD",
        icon_name="table",
        description="地籍交集清冊、DXF 分層與校正、多檔合併、亂碼與抽稀、CSV 轉點、屬性拆檔。",
        factory=_create_vector_tab,
    ),
    LabTool(
        key=LAB_KEY_TERRAIN,
        title="地形與水文",
        icon_name="ruler",
        description="兩期土方計算、水保坡度分類、斷面高程擷取、集水區劃設。",
        factory=_create_terrain_tab,
    ),
)
