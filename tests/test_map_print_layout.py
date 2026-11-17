"""瀏覽器地圖的簡易出圖 (core/map_print_layout.py)。

1. 加上工具列 (圖名、A4／A3 橫式、列印、離開)、指北針、圖例、「出圖」鈕，圖例跟著圖層開關 (用圖層的網頁變數名)。
2. 列印時藏起操作用的控制項，只留比例尺 (公尺)、出處與自己的東西；圖磚強制不透明。
3. 文字一律寫成 ASCII JSON，圖例文字裡有 </script> 也不會弄壞網頁；不合法的符號種類與變數名拋錯。
4. 開程式時不載入 branca (第一次加版面才載入)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from core.map_print_layout import (
    LEGEND_LINE,
    LEGEND_POLYGON,
    PAPER_SIZES,
    LegendEntry,
    add_print_layout,
)
from core.preview_engine import new_nlsc_map
from tests._support import IsolatedWorkspaceTestCase

ROOT = Path(__file__).resolve().parents[1]


def _js(text: str) -> str:
    return json.dumps(text)[1:-1]


class TestMapPrintLayout(IsolatedWorkspaceTestCase):
    def _render(self, legend_labels=("宗地範圍",), title: str = "") -> str:  # type: ignore[no-untyped-def]
        import folium

        fmap = new_nlsc_map()
        legend = []
        for label in legend_labels:
            layer = folium.GeoJson(
                {"type": "FeatureCollection", "features": [
                    {"type": "Feature", "id": "0", "properties": {}, "geometry": {"type": "Point", "coordinates": [121.0, 23.9]}}
                ]},
                name=label,
            )
            layer.add_to(fmap)
            legend.append(LegendEntry(layer.get_name(), label, LEGEND_POLYGON, "#FF2222", "#FFD700", 0.25))
        add_print_layout(fmap, legend, title=title)
        html_path = self.out_dir / "map.html"
        fmap.save(str(html_path))
        return html_path.read_text(encoding="utf-8")

    def test_toolbar_legend_and_print_rules(self) -> None:
        html = self._render(title="範例示意圖")
        self.assertIn('<div id="wl-print-bar">', html)
        for key, (label, width, height) in PAPER_SIZES.items():
            self.assertIn(f'<option value="{key}">{label}</option>', html)
            self.assertIn(f'"{key}": {{"w": {width}, "h": {height}}}', html)
        self.assertIn("列印／另存 PDF", html)
        self.assertIn(_js("範例示意圖"), html)
        self.assertIn(_js("宗地範圍"), html)
        self.assertIn("map.hasLayer(layers[index])", html, "圖例跟著圖層開關")
        self.assertRegex(html, r"var layers = \[geo_json_\w+\];")
        # 圖層變數宣告在前、出圖程式在後
        self.assertLess(html.index("var geo_json_"), html.index("var layers = ["))
        self.assertIn("@media print", html)
        self.assertIn(".leaflet-control:not(.leaflet-control-scale):not(.leaflet-control-attribution):not(.wl-keep)", html)
        self.assertIn("img.leaflet-tile { opacity: 1 !important; }", html)
        self.assertIn("@page { size: ' + key + ' landscape", html)
        self.assertIn("#print-", html)

    def test_text_is_escaped(self) -> None:
        html = self._render(legend_labels=("a</script><b>x",))
        self.assertNotIn("a</script>", html)
        self.assertIn("a<\\/script>", html)

    def test_invalid_entries(self) -> None:
        import folium

        fmap = folium.Map()
        with self.assertRaises(ValueError):
            add_print_layout(fmap, [LegendEntry("geo_json_1", "x", "hexagon")])
        with self.assertRaises(ValueError):
            add_print_layout(fmap, [LegendEntry("geo_json_1; alert(1)", "x", LEGEND_LINE)])

    def test_branca_is_loaded_only_when_used(self) -> None:
        code = "import sys, core.map_print_layout, core.quick_view_map; print('branca' in sys.modules, 'folium' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
