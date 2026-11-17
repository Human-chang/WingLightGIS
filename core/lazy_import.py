"""重的第三方模組等第一次用到才載入 (core/lazy_import.py)。

規範「打包與發佈」的效能要求：啟動時只載入畫面需要的東西，重的模組等使用者按下去才載入。
folium (瀏覽器地圖) 載入約 0.43 秒，但只有按「瀏覽器套疊航照底圖」這類按鈕才用得到。
在模組最上面寫 folium = LazyModule("folium") 代替 import folium，函式裡的寫法 (folium.Map(...)) 不用改，
第一次取用屬性時才真的載入。型別註解要配合 from __future__ import annotations (註解不會被求值)。
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


class LazyModule:
    """第一次取用屬性時才 import 的模組替身。"""

    def __init__(self, name: str) -> None:
        # 直接寫進 __dict__：這兩個名字找得到，就不會走到 __getattr__
        self.__dict__["_lazy_name"] = name
        self.__dict__["_lazy_module"] = None

    @property
    def is_loaded(self) -> bool:
        return self.__dict__["_lazy_module"] is not None

    def load(self) -> ModuleType:
        module = self.__dict__["_lazy_module"]
        if module is None:
            module = importlib.import_module(self.__dict__["_lazy_name"])
            self.__dict__["_lazy_module"] = module
        return module

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.load(), attr)

    def __repr__(self) -> str:
        state = "已載入" if self.is_loaded else "尚未載入"
        return f"<LazyModule {self.__dict__['_lazy_name']} ({state})>"
