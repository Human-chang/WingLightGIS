"""可勾選的清單 (ui/check_list.py)：WingLight 規範的 List 元件，每列第一欄是勾選框。

照規範 List：外框 panel 底、1px line 框線、radius-md 圓角；表頭 bg 底、muted 小字，寫「檔名（已勾選 3／4）」，
表頭的勾選框按一下全勾、全部勾著時按一下全不勾。列的上下留 space-8、左右留 space-12、body 字；
滑鼠移過是 hover 底，選取是 soft 底、字色不變；沒勾的列字變 muted。

- 列用 ttk.Treeview：幾百個檔也不卡，底色能自己指定。勾選框是畫好的圖 (規範 Input：16px、圓角 4px，
  沒勾是 panel 底＋1px muted 框，勾了是 accent 底＋on-accent 的勾)，點到圖只切換勾選，點其他地方是選取；
  空白鍵切換選取那一列，上下鍵換選取。
- 第一欄之後可以有好幾欄 (columns：表頭字與寬度)，靠右排、寬度固定，第一欄佔剩下的寬度。
- 列可以有子列 (例如 DXF 裡的 CAD 圖層)：expandable=True 時每列勾選框前面留一格展開鈕 (Icons 群組的 chevron)，
  有子列的才畫出來，點它展開／收合 (左右鍵也可以)；子列往右縮一格、有自己的勾選框，父列沒勾時子列的字也是 muted。
- 表頭用畫布自己畫：Treeview 表頭的圖只能放在字的右邊。表頭的 bg 底在外框的圓角裡面，CustomTkinter 的
  子元件疊在圓角上會把圓角蓋掉，所以表頭的底連同上面兩個圓角、框線、下面的分隔線一起畫成一張圖。
- 跟規範不同的地方：列與列之間的 1px 分隔線 Treeview 畫不出來，只靠滑過與選取的底色分辨。
- 這裡只管畫面：清單內容 (哪些列、哪些勾著、選哪一列) 由用它的地方保管，改了就呼叫 set_rows()；
  使用者的操作用 on_select (選了第幾列，None 是沒選；點子列算選它的父列)、on_toggle (切換第幾列)、
  on_toggle_child (切換第幾列的第幾個子列)、on_toggle_all (True 全勾、False 全不勾) 通知。展開與否由清單自己記。
這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/check_list.py 是同一份，改了要兩邊一起改
(kit 裡 import 的 ui.theme_constants 就是 kit/winglight_ctk.py)。
"""

from __future__ import annotations

import tkinter
import tkinter.font as tkfont
from dataclasses import dataclass
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from ui.theme_constants import FONT_FAMILY, FONT_SIZE_BODY, FONT_SIZE_CAPTION, RADIUS_MD, T

STYLE_NAME: str = "WingLight.Treeview"
_FIELD_ELEMENT: str = "WingLight.field"
OFF_TAG: str = "off"
HOVER_TAG: str = "hover"
# 規範 List 的間距 (100% 縮放時的 px)：列的上下 space-8、左右 space-12；勾選框與檔名之間 space-8
ROW_PAD_X: int = 12
ROW_PAD_Y: int = 8
CHECK_GAP: int = 8
# 規範 Input 的勾選框
CHECKBOX_SIZE: int = 16
CHECKBOX_RADIUS: int = 4
# 展開鈕 (16px 線條圖示) 與勾選框之間的距離；子列往右縮的寬度＝勾選框＋與檔名的間距，子列的勾選框對齊父列的檔名
CHEVRON_SIZE: int = 16
CHEVRON_GAP: int = 4
CHILD_INDENT: int = CHECKBOX_SIZE + CHECK_GAP
# 展開鈕的狀態：沒有子列時是空格 (只佔位置，讓勾選框對齊)
CHEVRON_NONE: str = "none"
CHEVRON_CLOSED: str = "closed"
CHEVRON_OPEN: str = "open"
# Treeview 其他欄的字比欄位的 padding 再右移 4px (實測，跟縮放無關)，表頭要對齊就從 padding 扣掉
_CELL_TEXT_INSET: int = 4
# Icons 群組 check 與 chevron-down 的路徑 (24 格)；收合時的展開鈕是 chevron-down 轉成朝右
_CHECK_POINTS: Tuple[Tuple[float, float], ...] = ((5, 12), (10, 17), (19, 7))
_CHEVRON_DOWN_POINTS: Tuple[Tuple[float, float], ...] = ((6, 9), (12, 15), (18, 9))
_CHEVRON_RIGHT_POINTS: Tuple[Tuple[float, float], ...] = ((9, 6), (15, 12), (9, 18))
_CHECK_STROKE: float = 2.0
_ICON_STROKE: float = 1.75  # 線條圖示在 24 格裡的筆畫寬
_SUPERSAMPLE: int = 8
_COLOR_NAMES: Tuple[str, ...] = ("panel", "bg", "line", "text", "muted", "soft", "hover", "accent", "on-accent")


@dataclass(frozen=True)
class CheckRow:
    """清單的一列：text 是第一欄 (檔名)，details 是後面各欄的字，children 是子列 (只有一層)。"""

    text: str
    details: Tuple[str, ...] = ()
    checked: bool = True
    children: Tuple["CheckRow", ...] = ()


def _draw_polyline(
    draw: ImageDraw.ImageDraw, points: Sequence[Tuple[float, float]], *, x0: float, unit: float, width: float, color: str
) -> None:
    """24 格的線條 (圓頭、圓角) 畫到 (x0, 0) 起、每格 unit 像素的位置。"""
    scaled = [(x0 + x * unit, y * unit) for x, y in points]
    draw.line(scaled, fill=color, width=round(width), joint="curve")
    for x, y in (scaled[0], scaled[-1]):
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color)


def checkbox_image(
    checked: bool, colors: Dict[str, str], scaling: float = 1.0, chevron: Optional[str] = None
) -> Image.Image:
    """列的第一欄的圖：左邊留 space-12、(展開鈕)、勾選框、右邊留 space-8，留白是透明的。

    chevron 是 None 時沒有展開鈕那一格 (不能展開的清單)；CHEVRON_NONE 只留空格。"""
    s = _SUPERSAMPLE
    box = round(CHECKBOX_SIZE * scaling)
    left = round(ROW_PAD_X * scaling)
    chevron_width = 0 if chevron is None else round((CHEVRON_SIZE + CHEVRON_GAP) * scaling)
    width = left + chevron_width + box + round(CHECK_GAP * scaling)
    big = Image.new("RGBA", (width * s, box * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    if chevron in (CHEVRON_CLOSED, CHEVRON_OPEN):
        points = _CHEVRON_RIGHT_POINTS if chevron == CHEVRON_CLOSED else _CHEVRON_DOWN_POINTS
        icon_px = round(CHEVRON_SIZE * scaling) * s
        _draw_polyline(
            draw, points, x0=left * s, unit=icon_px / 24, width=_ICON_STROKE * icon_px / 24, color=colors["muted"]
        )
    x0 = (left + chevron_width) * s
    rect = (x0, 0, x0 + box * s - 1, box * s - 1)
    radius = CHECKBOX_RADIUS * scaling * s
    if checked:
        draw.rounded_rectangle(rect, radius=radius, fill=colors["accent"])
        _draw_polyline(
            draw, _CHECK_POINTS, x0=x0, unit=box * s / 24, width=_CHECK_STROKE * scaling * s, color=colors["on-accent"]
        )
    else:
        border = max(1, round(scaling)) * s
        draw.rounded_rectangle(rect, radius=radius, fill=colors["panel"], outline=colors["muted"], width=border)
    return big.resize((width, box), Image.LANCZOS)


def header_background(width: int, height: int, *, radius: int, border: int, colors: Dict[str, str]) -> Image.Image:
    """表頭的底：外框的上面兩個圓角與框線、bg 底、最下面一條分隔線。colors["outside"] 是外框外面的顏色。

    跟 CustomTkinter 畫外框的方式對齊：寬度捨去成偶數 (奇數時最右邊 1px 是外面的顏色)。"""
    s = 4
    drawn = width // 2 * 2
    big = Image.new("RGB", (width * s, height * s), colors["outside"])
    draw = ImageDraw.Draw(big)
    bottom = (height + 2 * radius) * s  # 往下畫超過圖的範圍，下面兩個角不會是圓的
    draw.rounded_rectangle((0, 0, drawn * s - 1, bottom), radius=radius * s, fill=colors["line"])
    b = border * s
    draw.rounded_rectangle((b, b, drawn * s - 1 - b, bottom), radius=max(0, radius - border) * s, fill=colors["bg"])
    draw.rectangle((b, (height - border) * s, drawn * s - 1 - b, height * s - 1), fill=colors["line"])
    return big.resize((width, height), Image.LANCZOS)


def _ensure_style(style: ttk.Style) -> None:
    """清單的 ttk 樣式：外框拿掉 (外框由 CustomTkinter 畫)，列只有勾選框圖與文字 (不要內建展開鈕與虛線焦點框)。"""
    if _FIELD_ELEMENT not in style.element_names():
        # Windows 預設的 vista 主題的底是系統畫的，不吃顏色；default 主題的可以指定底色
        style.element_create(_FIELD_ELEMENT, "from", "default")
    style.layout(STYLE_NAME, [(_FIELD_ELEMENT, {"sticky": "nswe", "border": "0", "children": [
        ("Treeview.padding", {"sticky": "nswe", "children": [("Treeview.treearea", {"sticky": "nswe"})]}),
    ]})])
    style.layout(f"{STYLE_NAME}.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.image", {"side": "left", "sticky": ""}),
        ("Treeitem.text", {"side": "left", "sticky": ""}),
    ]})])
    style.configure(STYLE_NAME, borderwidth=0, relief="flat")


def _split_iid(iid: str) -> Tuple[int, Optional[int]]:
    """清單項目的代號：第 i 列是 "i"，它的第 j 個子列是 "i/j"。"""
    parent, _, child = iid.partition("/")
    return int(parent), (int(child) if child else None)


class CheckList(ctk.CTkFrame):
    """可勾選的清單 (規範 List)。title 是第一欄的表頭 (後面自動加「（已勾選 m／n）」)，
    columns 是後面各欄的 (表頭字, 100% 縮放時的寬度)。"""

    def __init__(
        self,
        master: Any,
        *,
        title: str,
        columns: Sequence[Tuple[str, int]] = (),
        expandable: bool = False,
        visible_rows: int = 4,
        on_select: Optional[Callable[[Optional[int]], None]] = None,
        on_toggle: Optional[Callable[[int], None]] = None,
        on_toggle_child: Optional[Callable[[int, int], None]] = None,
        on_toggle_all: Optional[Callable[[bool], None]] = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("fg_color", T["panel"])
        kwargs.setdefault("border_color", T["line"])
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("corner_radius", RADIUS_MD)
        super().__init__(master, **kwargs)
        self.title = title
        self.columns: Tuple[Tuple[str, int], ...] = tuple(columns)
        self.expandable = expandable
        self.on_select = on_select
        self.on_toggle = on_toggle
        self.on_toggle_child = on_toggle_child
        self.on_toggle_all = on_toggle_all
        self._rows: List[CheckRow] = []
        self._selected: Optional[int] = None
        self._hover: Optional[str] = None
        self._colors: Dict[str, str] = {}
        self._images: Dict[Tuple[bool, Optional[str]], ImageTk.PhotoImage] = {}
        self._header_photo: Optional[ImageTk.PhotoImage] = None
        self._scroll_view: Optional[Tuple[float, float]] = None
        self._style = ttk.Style(self)
        _ensure_style(self._style)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.header = tkinter.Canvas(self, highlightthickness=0, borderwidth=0)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._column_ids = tuple(f"c{index}" for index in range(len(self.columns)))
        self.tree = ttk.Treeview(
            self, style=STYLE_NAME, columns=self._column_ids, show="tree", selectmode="browse", height=visible_rows
        )
        self.tree.grid(row=1, column=0, sticky="nsew")
        # 捲軸的高度跟著清單 (預設要 200px，會把清單撐高，撐高後放得下又收起捲軸，來回跳)
        self.scrollbar = ctk.CTkScrollbar(self, command=self.tree.yview, height=20)
        self.tree.configure(yscrollcommand=self._on_tree_scroll)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _e: self._set_hover(None))
        self.tree.bind("<MouseWheel>", self._on_wheel)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        # 左右鍵展開／收合時，事件比展開狀態先到，等狀態改完再換展開鈕的圖
        self.tree.bind("<<TreeviewOpen>>", lambda _e: self.after_idle(self._refresh_images))
        self.tree.bind("<<TreeviewClose>>", lambda _e: self.after_idle(self._refresh_images))
        self.tree.bind("<Configure>", lambda _e: self._draw_header(), add="+")
        self.header.bind("<Configure>", lambda _e: self._draw_header())
        self.header.bind("<Button-1>", self._on_header_click)
        self._restyle()

    # -------------------------------------------------------------------------
    # 給用它的地方
    # -------------------------------------------------------------------------
    @property
    def rows(self) -> List[CheckRow]:
        return list(self._rows)

    @property
    def selected(self) -> Optional[int]:
        return self._selected

    def set_rows(self, rows: Sequence[CheckRow], selected: Optional[int] = None) -> None:
        """換成這些列，selected 是選取的那一列 (None 是不選)。同一列沿用原本的項目，捲動位置與展開狀態不會跑掉。"""
        self._rows = list(rows)
        self._sync_items("", [str(index) for index in range(len(self._rows))], self._rows)
        for index, row in enumerate(self._rows):
            self._sync_items(str(index), [f"{index}/{child}" for child in range(len(row.children))], row.children)
        if self._hover is not None and not self.tree.exists(self._hover):
            self._hover = None
        self._selected = selected if selected is not None and 0 <= selected < len(self._rows) else None
        current = self.tree.selection()
        if self._selected is None:
            self.tree.selection_remove(*current)
        elif not (current and _split_iid(current[0])[0] == self._selected):
            # 選著同一列的子列時不動 (點子列算選它的父列)
            iid = str(self._selected)
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        self._draw_header()

    def see(self, index: int) -> None:
        """捲到第 index 列 (已經看得到就不動)。"""
        if 0 <= index < len(self._rows):
            self.tree.see(str(index))

    def header_text(self) -> str:
        """第一欄表頭的字：有列時是「檔名（已勾選 3／4）」(只算最上層的列)。"""
        if not self._rows:
            return self.title
        checked = sum(1 for row in self._rows if row.checked)
        return f"{self.title}（已勾選 {checked}／{len(self._rows)}）"

    def all_checked(self) -> bool:
        return bool(self._rows) and all(row.checked for row in self._rows)

    def is_open(self, index: int) -> bool:
        return bool(self.tree.exists(str(index)) and self.tree.item(str(index), "open"))

    def set_open(self, index: int, is_open: bool) -> None:
        """展開或收合第 index 列 (沒有子列時不動)。"""
        iid = str(index)
        if self.tree.exists(iid) and self.tree.get_children(iid):
            self.tree.item(iid, open=is_open)
            self.tree.item(iid, image=self._image_for(iid))

    def checkbox_center(self, index: int, child: Optional[int] = None) -> Tuple[int, int]:
        """第 index 列 (或它的第 child 個子列) 勾選框中心在 tree 裡的座標 (列要先捲到看得見)。"""
        iid = str(index) if child is None else f"{index}/{child}"
        _x, y, _w, h = self.tree.bbox(iid)
        return self._checkbox_left(child is not None) + round(CHECKBOX_SIZE / 2 * self._scaling), y + h // 2

    def chevron_center(self, index: int) -> Tuple[int, int]:
        """第 index 列展開鈕中心在 tree 裡的座標。"""
        _x, y, _w, h = self.tree.bbox(str(index))
        return round((ROW_PAD_X + CHEVRON_SIZE / 2) * self._scaling), y + h // 2

    def drop_targets(self) -> Tuple[Any, ...]:
        """拖放要註冊的元件：外框、表頭、清單本體、捲軸。"""
        return (self, self.header, self.tree, self.scrollbar)

    # -------------------------------------------------------------------------
    # 外觀：縮放與深淺色一變就重畫
    # -------------------------------------------------------------------------
    def _set_appearance_mode(self, mode_string: str) -> None:
        super()._set_appearance_mode(mode_string)
        if hasattr(self, "tree"):
            self._restyle()

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        super()._set_scaling(*args, **kwargs)
        if hasattr(self, "tree"):
            self._restyle()

    def _restyle(self) -> None:
        s = self._apply_widget_scaling(1.0)
        self._scaling = s
        self._colors = {name: self._apply_appearance_mode(T[name]) for name in _COLOR_NAMES}
        self._colors["outside"] = self._apply_appearance_mode(self.cget("bg_color"))
        # 跟 CustomTkinter 畫外框時一樣取整數
        self._border = round(self._apply_widget_scaling(self.cget("border_width")))
        self._radius = round(self._apply_widget_scaling(self.cget("corner_radius")))

        body_px = round(FONT_SIZE_BODY * s)
        caption_px = round(FONT_SIZE_CAPTION * s)
        self._caption_font = (FONT_FAMILY, -caption_px)
        pad_y = round(ROW_PAD_Y * s)
        row_height = tkfont.Font(root=self, family=FONT_FAMILY, size=-body_px).metrics("linespace") + 2 * pad_y
        self._header_height = (
            tkfont.Font(root=self, family=FONT_FAMILY, size=-caption_px).metrics("linespace") + 2 * pad_y + self._border
        )
        c = self._colors
        self._style.configure(
            STYLE_NAME, rowheight=row_height, font=(FONT_FAMILY, -body_px), indent=round(CHILD_INDENT * s),
            background=c["panel"], fieldbackground=c["panel"], foreground=c["text"],
        )
        # 選取只換底色；字色不設 (沒勾的列選取後仍是 muted)，空的設定也擋掉 vista 主題選取時的白字
        self._style.map(STYLE_NAME, background=[("selected", c["soft"])], foreground=[])
        # 列的左邊留白已經畫在勾選框圖裡 (vista 主題預設另外留 4px，表頭會對不齊)
        self._style.configure(f"{STYLE_NAME}.Item", padding=0)
        self._style.configure(f"{STYLE_NAME}.Cell", padding=(max(0, round(ROW_PAD_X * s) - _CELL_TEXT_INSET), 0, 0, 0))
        self.tree.tag_configure(OFF_TAG, foreground=c["muted"])
        self.tree.tag_configure(HOVER_TAG, background=c["hover"])
        self.tree.column("#0", width=round(120 * s), minwidth=round(120 * s), stretch=True)
        for column_id, (_title, width) in zip(self._column_ids, self.columns):
            self.tree.column(column_id, width=round(width * s), stretch=False, anchor="w")
        # 清單本體不壓到外框：左右讓出框線，下面讓出圓角
        self.tree.grid_configure(padx=(self._border, 0), pady=(0, self._radius))
        self.header.configure(height=self._header_height, background=c["bg"])

        self._images = {}
        self._refresh_images()
        self._draw_header()

    def _image(self, checked: bool, chevron: Optional[str]) -> ImageTk.PhotoImage:
        key = (checked, chevron)
        if key not in self._images:
            self._images[key] = ImageTk.PhotoImage(
                checkbox_image(checked, self._colors, self._scaling, chevron), master=self
            )
        return self._images[key]

    def _image_for(self, iid: str) -> ImageTk.PhotoImage:
        index, child = _split_iid(iid)
        row = self._rows[index]
        if child is not None:
            return self._image(row.children[child].checked, CHEVRON_NONE if self.expandable else None)
        chevron: Optional[str] = None
        if self.expandable:
            if not row.children:
                chevron = CHEVRON_NONE
            else:
                chevron = CHEVRON_OPEN if self.tree.item(iid, "open") else CHEVRON_CLOSED
        return self._image(row.checked, chevron)

    def _refresh_images(self) -> None:
        for index, row in enumerate(self._rows):
            iid = str(index)
            if self.tree.exists(iid):
                self.tree.item(iid, image=self._image_for(iid))
                for child in range(len(row.children)):
                    self.tree.item(f"{iid}/{child}", image=self._image_for(f"{iid}/{child}"))

    def _sync_items(self, parent: str, iids: List[str], rows: Sequence[CheckRow]) -> None:
        """parent 底下的項目換成 rows：多的刪掉、少的補上、其他沿用 (捲動位置與展開狀態不會跑掉)。"""
        existing = self.tree.get_children(parent)
        if len(existing) > len(iids):
            self.tree.delete(*existing[len(iids):])
        for position, (iid, row) in enumerate(zip(iids, rows)):
            if position >= len(existing):
                self.tree.insert(parent, "end", iid=iid)
            self.tree.item(iid, text=row.text, values=row.details, image=self._image_for(iid), tags=self._tags(iid))

    def _checkbox_left(self, is_child: bool) -> int:
        """勾選框左邊在 tree 裡的 x (子列再往右縮一格)。"""
        s = self._scaling
        left = round(ROW_PAD_X * s)
        if self.expandable:
            left += round((CHEVRON_SIZE + CHEVRON_GAP) * s)
        if is_child:
            left += round(CHILD_INDENT * s)
        return left

    def _draw_header(self) -> None:
        width = self.header.winfo_width()
        if width <= 1 or not self._colors:
            return
        height = self._header_height
        background = header_background(width, height, radius=self._radius, border=self._border, colors=self._colors)
        self._header_photo = ImageTk.PhotoImage(background, master=self)
        canvas = self.header
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=self._header_photo)
        # 勾選框與字跟清單的列對齊 (清單本體左邊讓出框線)
        x0 = self._border
        middle = (height - self._border) // 2
        checkbox = self._image(self.all_checked(), CHEVRON_NONE if self.expandable else None)
        canvas.create_image(x0, middle, anchor="w", image=checkbox, tags="checkbox")
        font = self._caption_font
        muted = self._colors["muted"]
        canvas.create_text(x0 + checkbox.width(), middle, anchor="w", text=self.header_text(), font=font, fill=muted, tags="title")
        # 其他欄靠右排：從清單本體的右緣往左扣各欄寬度
        tree_width = self.tree.winfo_width() if self.tree.winfo_width() > 1 else width - 2 * self._border
        column_x = x0 + tree_width - sum(round(w * self._scaling) for _t, w in self.columns)
        for index, (title, column_width) in enumerate(self.columns):
            canvas.create_text(
                column_x + round(ROW_PAD_X * self._scaling), middle, anchor="w", text=title, font=font, fill=muted,
                tags=f"column{index}",
            )
            column_x += round(column_width * self._scaling)

    def _tags(self, iid: str) -> Tuple[str, ...]:
        index, child = _split_iid(iid)
        row = self._rows[index]
        checked = row.checked if child is None else (row.checked and row.children[child].checked)
        tags: List[str] = []
        if not checked:
            tags.append(OFF_TAG)
        if self._hover == iid:
            tags.append(HOVER_TAG)
        return tuple(tags)

    # -------------------------------------------------------------------------
    # 操作
    # -------------------------------------------------------------------------
    def _on_click(self, event: Any) -> str:
        """點到展開鈕是展開／收合；點到勾選框只切換勾選；點列的其他地方是選取；點到列下面的空白是取消選取。"""
        self.tree.focus_set()
        iid = self.tree.identify_row(event.y)
        if not iid:
            self._emit_select(None)
            return "break"
        index, child = _split_iid(iid)
        chevron_end = round((ROW_PAD_X + CHEVRON_SIZE + CHEVRON_GAP / 2) * self._scaling)
        if self.expandable and child is None and event.x < chevron_end and self._rows[index].children:
            self.set_open(index, not self.is_open(index))
        elif self.tree.identify_element(event.x, event.y).endswith("image"):
            self._emit_toggle(iid)
        else:
            if child is not None:
                self.tree.selection_set(iid)
            self._emit_select(index)
        return "break"

    def _emit_toggle(self, iid: str) -> None:
        index, child = _split_iid(iid)
        if child is None:
            if self.on_toggle is not None:
                self.on_toggle(index)
        elif self.on_toggle_child is not None:
            self.on_toggle_child(index, child)

    def _on_space(self, _event: Any) -> str:
        chosen = self.tree.selection()
        if chosen:
            self._emit_toggle(chosen[0])
        return "break"

    def _on_tree_select(self, _event: Any) -> None:
        """上下鍵換了選取：通知用它的地方。程式自己設的選取 (set_rows) 跟記下的一樣，不再通知。"""
        chosen = self.tree.selection()
        index = _split_iid(chosen[0])[0] if chosen else None
        if index != self._selected:
            self._emit_select(index)

    def _emit_select(self, index: Optional[int]) -> None:
        self._selected = index
        if self.on_select is not None:
            self.on_select(index)

    def _on_header_click(self, event: Any) -> None:
        """點表頭的勾選框：全部勾著時全不勾，其他情況全勾。"""
        left = self._border + self._checkbox_left(False)
        box = round(CHECKBOX_SIZE * self._scaling)
        if not self._rows or not left - box // 2 <= event.x < left + box + box // 2:
            return
        if self.on_toggle_all is not None:
            self.on_toggle_all(not self.all_checked())

    def _on_motion(self, event: Any) -> None:
        self._set_hover(self.tree.identify_row(event.y) or None)

    def _set_hover(self, iid: Optional[str]) -> None:
        if iid == self._hover:
            return
        old, self._hover = self._hover, iid
        for item in (old, iid):
            if item is not None and self.tree.exists(item):
                self.tree.item(item, tags=self._tags(item))

    def _on_wheel(self, event: Any) -> Optional[str]:
        """清單捲得動時只捲清單 (外面的卡片區不跟著捲)；捲不動時交給外面。"""
        first, last = self.tree.yview()
        if first <= 0.0 and last >= 1.0:
            return None
        self.tree.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _on_tree_scroll(self, first: str, last: str) -> None:
        """清單放得下時把捲軸收起來。

        位置沒變就不更新：CTkScrollbar.set() 裡會 update_idletasks()，清單在那裡重畫又會再呼叫這裡，沒擋會無限遞迴。"""
        view = (float(first), float(last))
        if view == self._scroll_view:
            return
        self._scroll_view = view
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scrollbar.grid_remove()
        elif not self.scrollbar.winfo_manager():
            self.scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 1), pady=(0, RADIUS_MD))
        self.scrollbar.set(first, last)
