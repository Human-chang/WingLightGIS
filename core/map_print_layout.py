"""瀏覽器地圖的簡易出圖 (core/map_print_layout.py)

在 folium 地圖加上「出圖」：
- 地圖左上一顆「出圖」鈕：進入出圖模式，畫面上方出現工具列 (圖名、紙張 A4／A3 橫式、「列印／另存 PDF」、「離開出圖」)，
  地圖縮成紙張可印範圍的大小 (CSS 的 mm，畫面上看到的就是印出來的樣子)，可以再拖曳縮放、開關圖層調整。
- 圖上的東西 (印出來也有)：圖名 (上方置中，沒填就不印)、指北針 (左上；網頁地圖上方是真北)、
  圖例 (右下，只列開著的圖層，文字可以直接點來改)、比例尺 (左下，出圖時只留公尺)、底圖出處。
- 列印時藏起來：工具列、縮放、量距、全螢幕、圖層開關、游標座標、「出圖」鈕。
網址加 #print-A4 或 #print-A3 開啟時直接進入出圖模式 (測試用無頭瀏覽器印 PDF 時用)。

圖面比例尺以圖上的比例尺為準；固定比例 (1/500 等) 不提供 (網頁地圖的縮放一級兩倍，各瀏覽器列印行為不一)。
folium／branca 第一次加版面時才載入 (branca 載入約 0.25 秒，開程式時不載入)。
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from typing import Any, Dict, Sequence, Tuple

# 圖例符號的種類
LEGEND_POLYGON: str = "polygon"
LEGEND_LINE: str = "line"
LEGEND_POINT: str = "point"
LEGEND_DEM: str = "dem"
LEGEND_IMAGE: str = "image"
LEGEND_KINDS: Tuple[str, ...] = (LEGEND_POLYGON, LEGEND_LINE, LEGEND_POINT, LEGEND_DEM, LEGEND_IMAGE)

# 紙張：(選單文字, 地圖寬 mm, 地圖高 mm)。橫式、四邊留 10 mm，再各少 1 mm 免得被擠到第二頁
PAPER_SIZES: Dict[str, Tuple[str, float, float]] = {
    "A4": ("A4 橫式", 276.0, 189.0),
    "A3": ("A3 橫式", 399.0, 276.0),
}
PAGE_MARGIN_MM: int = 10
PRINT_BUTTON_TEXT: str = "出圖"
PRINT_HINT: str = "圖例文字可以直接點來改。量長度以圖上的比例尺為準；列印視窗的縮放請維持 100% (預設)，要存 PDF 時印表機選「另存為 PDF」。"


@dataclass(frozen=True)
class LegendEntry:
    """圖例的一列。layer_var 是 folium 圖層物件的 get_name() (網頁裡的變數名，用來跟著圖層開關)。"""

    layer_var: str
    label: str
    kind: str
    outline: str = ""
    fill: str = ""
    fill_opacity: float = 0.0


_CSS = """
.wl-title { position: absolute; top: 10px; left: 50%; transform: translateX(-50%); z-index: 800;
  background: rgba(255,255,255,0.92); border: 1px solid #333; padding: 4px 16px; color: #111;
  font: bold 20px "Microsoft JhengHei", "Noto Sans TC", sans-serif; white-space: nowrap; pointer-events: none; }
.wl-title:empty { display: none; }
.wl-north { background: rgba(255,255,255,0.9); border-radius: 4px; padding: 4px 6px; line-height: 0; }
.wl-legend { background: rgba(255,255,255,0.92); border: 1px solid #888; padding: 6px 10px; color: #111;
  font: 12px "Microsoft JhengHei", "Noto Sans TC", sans-serif; max-width: 280px; }
.wl-legend-title { font-weight: bold; margin-bottom: 4px; }
.wl-legend-row { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.wl-legend-row svg { flex: none; }
.wl-legend-row span { outline: none; }
.wl-legend-row.wl-off { display: none; }
.wl-legend.wl-empty { display: none; }
.wl-open-print a { width: auto !important; padding: 0 8px; font: bold 13px "Microsoft JhengHei", sans-serif; color: #111; }
#wl-print-bar { display: none; position: fixed; top: 0; left: 0; right: 0; z-index: 2000; background: #fff;
  border-bottom: 1px solid #c8c8c8; padding: 8px 16px; align-items: center; gap: 12px; flex-wrap: wrap;
  font: 14px "Microsoft JhengHei", "Noto Sans TC", sans-serif; color: #111; }
#wl-print-bar input { width: 260px; font: inherit; padding: 3px 6px; }
#wl-print-bar select, #wl-print-bar button { font: inherit; padding: 3px 10px; }
#wl-print-bar .wl-hint { color: #666; font-size: 12px; flex-basis: 100%; }
body.wl-print-mode { background: #d4d4d4; overflow: auto; }
body.wl-print-mode #wl-print-bar { display: flex; }
body.wl-print-mode .folium-map { margin: 96px auto 24px; box-shadow: 0 2px 10px rgba(0,0,0,0.3); }
body.wl-print-mode .wl-open-print { display: none; }
body.wl-print-mode .leaflet-control-scale-line + .leaflet-control-scale-line { display: none; }
@media print {
  html, body { width: auto !important; height: auto !important; margin: 0 !important; padding: 0 !important;
    background: #fff !important; overflow: visible !important; }
  #wl-print-bar { display: none !important; }
  .leaflet-control:not(.leaflet-control-scale):not(.leaflet-control-attribution):not(.wl-keep) { display: none !important; }
  .leaflet-control-scale-line + .leaflet-control-scale-line { display: none; }
  .folium-map { margin: 0 !important; box-shadow: none !important; outline: 0.3mm solid #000; }
  /* 圖磚載入時是淡入的 (Leaflet 逐張改透明度)，還沒淡入完就印會整片偏白 */
  img.leaflet-tile { opacity: 1 !important; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""

_NORTH_SVG = (
    '<svg width="28" height="40" viewBox="0 0 28 40" xmlns="http://www.w3.org/2000/svg">'
    '<text x="14" y="11" text-anchor="middle" font-family="Arial, sans-serif" font-weight="bold" font-size="12" fill="#111">N</text>'
    '<polygon points="14,13 22,38 14,32 6,38" fill="#fff" stroke="#111" stroke-width="1.2" stroke-linejoin="round"/>'
    '<polygon points="14,13 14,32 6,38" fill="#111"/>'
    "</svg>"
)

_BAR_HTML = """
<div id="wl-print-bar">
  <strong>出圖</strong>
  <label>圖名 <input id="wl-title-input" type="text" placeholder="輸入圖名 (可留空)"></label>
  <label>紙張 <select id="wl-paper">{options}</select></label>
  <button id="wl-print-go" type="button">列印／另存 PDF</button>
  <button id="wl-print-exit" type="button">離開出圖</button>
  <span class="wl-hint">{hint}</span>
</div>
"""

# __MAP__、__LAYERS__、__CONFIG__ 由 Python 換掉 (變數名只有英數字與底線；設定是 ASCII 的 JSON)
_SCRIPT = """
(function () {
  var map = __MAP__;
  var layers = [__LAYERS__];
  var cfg = __CONFIG__;
  var container = map.getContainer();
  var pageStyle = document.createElement('style');
  document.head.appendChild(pageStyle);

  var titleDiv = L.DomUtil.create('div', 'wl-title', container);
  titleDiv.textContent = cfg.title;

  var North = L.Control.extend({
    options: {position: 'topleft'},
    onAdd: function () {
      var div = L.DomUtil.create('div', 'wl-north wl-keep');
      div.innerHTML = cfg.north;
      return div;
    }
  });
  new North().addTo(map);

  function swatch(entry, index) {
    var o = entry.outline, f = entry.fill;
    if (entry.kind === 'line') {
      return '<svg width="22" height="12"><line x1="1" y1="6" x2="21" y2="6" stroke="' + o + '" stroke-width="2.5"/></svg>';
    }
    if (entry.kind === 'point') {
      return '<svg width="22" height="12"><circle cx="11" cy="6" r="4.5" fill="' + f + '" fill-opacity="' + entry.fill_opacity +
        '" stroke="' + o + '" stroke-width="2"/></svg>';
    }
    if (entry.kind === 'dem') {
      return '<svg width="22" height="12"><defs><linearGradient id="wl-dem' + index + '"><stop offset="0" stop-color="#2E7D32"/>' +
        '<stop offset="0.5" stop-color="#F9E79F"/><stop offset="1" stop-color="#8D4E24"/></linearGradient></defs>' +
        '<rect x="1" y="1" width="20" height="10" fill="url(#wl-dem' + index + ')" stroke="#555" stroke-width="0.8"/></svg>';
    }
    if (entry.kind === 'image') {
      return '<svg width="22" height="12"><rect x="1" y="1" width="20" height="10" fill="#7C8F6A" stroke="#555" stroke-width="0.8"/></svg>';
    }
    return '<svg width="22" height="12"><rect x="1.5" y="1.5" width="19" height="9" fill="' + f + '" fill-opacity="' + entry.fill_opacity +
      '" stroke="' + o + '" stroke-width="2"/></svg>';
  }

  var rows = [];
  var legendDiv = null;
  var Legend = L.Control.extend({
    options: {position: 'bottomright'},
    onAdd: function () {
      legendDiv = L.DomUtil.create('div', 'wl-legend wl-keep');
      L.DomEvent.disableClickPropagation(legendDiv);
      L.DomEvent.disableScrollPropagation(legendDiv);
      var head = L.DomUtil.create('div', 'wl-legend-title', legendDiv);
      head.textContent = '圖例';
      cfg.legend.forEach(function (entry, index) {
        var row = L.DomUtil.create('div', 'wl-legend-row', legendDiv);
        row.innerHTML = swatch(entry, index);
        var label = L.DomUtil.create('span', '', row);
        label.textContent = entry.label;
        label.contentEditable = 'true';
        label.spellcheck = false;
        rows.push(row);
      });
      return legendDiv;
    }
  });
  new Legend().addTo(map);

  function refreshLegend() {
    var shown = 0;
    rows.forEach(function (row, index) {
      var on = map.hasLayer(layers[index]);
      row.classList.toggle('wl-off', !on);
      if (on) { shown += 1; }
    });
    legendDiv.classList.toggle('wl-empty', shown === 0);
  }
  map.on('overlayadd overlayremove', refreshLegend);
  refreshLegend();

  var bar = document.getElementById('wl-print-bar');
  var titleInput = document.getElementById('wl-title-input');
  var paperSelect = document.getElementById('wl-paper');
  titleInput.value = cfg.title;
  titleInput.addEventListener('input', function () { titleDiv.textContent = titleInput.value.trim(); });

  function setPaper(key) {
    var paper = cfg.papers[key];
    if (!paper) { return; }
    paperSelect.value = key;
    container.style.width = paper.w + 'mm';
    container.style.height = paper.h + 'mm';
    pageStyle.textContent = '@page { size: ' + key + ' landscape; margin: ' + cfg.margin + 'mm; }';
    map.invalidateSize();
  }
  function enterPrint(key) {
    document.body.classList.add('wl-print-mode');
    setPaper(key || paperSelect.value);
  }
  function exitPrint() {
    document.body.classList.remove('wl-print-mode');
    container.style.width = '';
    container.style.height = '';
    pageStyle.textContent = '';
    map.invalidateSize();
  }
  paperSelect.addEventListener('change', function () { setPaper(paperSelect.value); });
  document.getElementById('wl-print-go').addEventListener('click', function () { window.print(); });
  document.getElementById('wl-print-exit').addEventListener('click', exitPrint);

  var OpenPrint = L.Control.extend({
    options: {position: 'topleft'},
    onAdd: function () {
      var div = L.DomUtil.create('div', 'leaflet-bar wl-open-print');
      var link = L.DomUtil.create('a', '', div);
      link.href = '#';
      link.title = '出圖：選紙張、填圖名、列印或另存 PDF';
      link.textContent = cfg.button;
      L.DomEvent.on(link, 'click', function (e) { L.DomEvent.preventDefault(e); enterPrint(); });
      L.DomEvent.disableClickPropagation(div);
      return div;
    }
  });
  new OpenPrint().addTo(map);

  var match = /^#print-(\\w+)$/.exec(window.location.hash);
  if (match && cfg.papers[match[1]]) { enterPrint(match[1]); }
})();
"""


@functools.lru_cache(maxsize=1)
def _layout_class() -> Any:
    """folium／branca 的 MacroElement 子類別 (第一次用到才載入 branca)。"""
    from branca.element import MacroElement
    from jinja2 import Template

    class PrintLayout(MacroElement):  # type: ignore[misc]
        _template = Template(
            "{% macro header(this, kwargs) %}<style>{{ this.css }}</style>{% endmacro %}"
            "{% macro html(this, kwargs) %}{{ this.bar_html }}{% endmacro %}"
            "{% macro script(this, kwargs) %}{{ this.script_for(this._parent.get_name()) }}{% endmacro %}"
        )

        def __init__(self, css: str, bar_html: str, layers: str, config: str) -> None:
            super().__init__()
            self._name = "WingLightPrintLayout"
            self.css = css
            self.bar_html = bar_html
            self._layers = layers
            self._config = config

        def script_for(self, map_var: str) -> str:
            return _SCRIPT.replace("__MAP__", map_var).replace("__LAYERS__", self._layers).replace("__CONFIG__", self._config)

    return PrintLayout


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def add_print_layout(fmap: Any, legend: Sequence[LegendEntry], *, title: str = "") -> None:
    """在地圖加上出圖功能 (要在所有圖層加完、add_map_tools 之後呼叫，網頁裡才找得到圖層)。"""
    for entry in legend:
        if entry.kind not in LEGEND_KINDS:
            raise ValueError(f"不支援的圖例符號: {entry.kind}")
        if not entry.layer_var.replace("_", "").isalnum() or not entry.layer_var.isascii():
            raise ValueError(f"圖層變數名不合法: {entry.layer_var}")
    config = {
        "title": title,
        "north": _NORTH_SVG,
        "button": PRINT_BUTTON_TEXT,
        "margin": PAGE_MARGIN_MM,
        "papers": {key: {"w": w, "h": h} for key, (_, w, h) in PAPER_SIZES.items()},
        "legend": [
            {"label": e.label, "kind": e.kind, "outline": e.outline, "fill": e.fill, "fill_opacity": e.fill_opacity}
            for e in legend
        ],
    }
    # ASCII JSON：中文寫成 \\uXXXX；"</" 拆開，字串裡出現 </script> 也不會提早結束程式區塊
    config_js = json.dumps(config, ensure_ascii=True).replace("</", "<\\/")
    options = "".join(f'<option value="{key}">{_escape_text(label)}</option>' for key, (label, _, _) in PAPER_SIZES.items())
    bar_html = _BAR_HTML.format(options=options, hint=_escape_text(PRINT_HINT))
    layout = _layout_class()(_CSS, bar_html, ", ".join(e.layer_var for e in legend), config_js)
    layout.add_to(fmap)
