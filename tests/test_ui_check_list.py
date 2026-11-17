"""可勾選的清單 (ui/check_list.py，風格統一 G 段)。

1. 勾選框圖照規範 Input：沒勾是 panel 底＋muted 框，勾了是 accent 底＋on-accent 的勾，左右留白是透明的。
2. 點勾選框只切換勾選、點列的其他地方是選取、點列下面的空白取消選取；空白鍵切換選取那一列；上下鍵換選取。
3. 表頭寫「檔名（已勾選 m／n）」，表頭的勾選框全勾／全不勾；沒勾的列字是 muted。
4. 換內容時捲動位置不跳回最上面；放得下時捲軸收起來。
5. 深淺色切換後清單的底色、選取色、灰字跟著換。
6. 多欄的表頭跟欄位對齊；子列 (DXF 圖層) 收合／展開、各自勾選，父列沒勾時子列也是 muted (圖資快覽 H1)。
"""

from __future__ import annotations

import time
import unittest
from tkinter import ttk
from typing import Any, List, Optional, Tuple

import customtkinter as ctk

from ui.check_list import (
    CHECK_GAP,
    CHEVRON_CLOSED,
    CHEVRON_NONE,
    CHEVRON_OPEN,
    CHEVRON_SIZE,
    CHEVRON_GAP,
    CHECKBOX_SIZE,
    HOVER_TAG,
    OFF_TAG,
    ROW_PAD_X,
    STYLE_NAME,
    CheckList,
    CheckRow,
    checkbox_image,
)
from ui.theme_constants import T, apply_ctk_theme


def rgb(hex_color: str) -> Tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def close_to(pixel: Tuple[int, ...], hex_color: str, tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], rgb(hex_color)))


class TestCheckboxImage(unittest.TestCase):
    def setUp(self) -> None:
        self.colors = {name: T[name][0] for name in ("panel", "muted", "accent", "on-accent")}

    def test_size_and_transparent_margins(self) -> None:
        for checked in (True, False):
            image = checkbox_image(checked, self.colors)
            self.assertEqual(image.size, (ROW_PAD_X + CHECKBOX_SIZE + CHECK_GAP, CHECKBOX_SIZE))
            self.assertEqual(image.getpixel((ROW_PAD_X // 2, CHECKBOX_SIZE // 2))[3], 0, "左邊留白是透明的")
            self.assertEqual(image.getpixel((image.width - 2, CHECKBOX_SIZE // 2))[3], 0, "右邊留白是透明的")
        self.assertEqual(checkbox_image(True, self.colors, scaling=1.5).size, (54, 24))

    def test_checked_is_accent_with_on_accent_check(self) -> None:
        image = checkbox_image(True, self.colors)
        top_middle = image.getpixel((ROW_PAD_X + 8, 3))
        self.assertEqual(top_middle, (*rgb(self.colors["accent"]), 255))
        # 勾的轉折點 (Icons 群組 check 的 10,17，縮到 16px 框裡)
        self.assertTrue(close_to(image.getpixel((ROW_PAD_X + 6, 11)), self.colors["on-accent"]))

    def test_unchecked_is_panel_with_muted_border(self) -> None:
        image = checkbox_image(False, self.colors)
        self.assertEqual(image.getpixel((ROW_PAD_X + 8, 8)), (*rgb(self.colors["panel"]), 255))
        self.assertTrue(close_to(image.getpixel((ROW_PAD_X, 8)), self.colors["muted"]))


class TestCheckList(unittest.TestCase):
    def setUp(self) -> None:
        apply_ctk_theme()
        self._mode = ctk.get_appearance_mode()
        self.root = ctk.CTk()
        self.root.geometry("560x320+40+40")
        self.calls: List[Tuple[str, Any]] = []
        self.checked = [True] * 6
        self.selected: Optional[int] = None
        self.list = CheckList(
            self.root,
            title="檔名",
            columns=(("所在資料夾", 140),),
            on_select=self._select,
            on_toggle=self._toggle,
            on_toggle_all=self._toggle_all,
        )
        self.list.pack(fill="x", padx=16, pady=16)
        self._show()
        self._wait_viewable()

    def tearDown(self) -> None:
        self.root.destroy()
        ctk.set_appearance_mode(self._mode)

    # 用清單的地方：保管勾選與選取，改了就 set_rows()
    def _select(self, index: Optional[int]) -> None:
        self.calls.append(("select", index))
        self.selected = index
        self._show()

    def _toggle(self, index: int) -> None:
        self.calls.append(("toggle", index))
        self.checked[index] = not self.checked[index]
        self._show()

    def _toggle_all(self, checked: bool) -> None:
        self.calls.append(("all", checked))
        self.checked = [checked] * len(self.checked)
        self._show()

    def _show(self) -> None:
        rows = [CheckRow(f"範例地形_{i:02d}.tif", ("A區",), on) for i, on in enumerate(self.checked)]
        self.list.set_rows(rows, self.selected)

    def _wait_viewable(self) -> None:
        deadline = time.monotonic() + 5
        while not self.list.tree.winfo_viewable() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()

    def _click(self, widget: Any, x: int, y: int) -> None:
        widget.event_generate("<Button-1>", x=x, y=y)
        self.root.update()

    def _click_row_text(self, index: int) -> None:
        x, y, width, height = self.list.tree.bbox(str(index))
        self._click(self.list.tree, x + width // 2, y + height // 2)

    def _key(self, keysym: str) -> None:
        self.list.tree.focus_force()
        self.root.update()
        self.list.tree.event_generate(f"<{keysym}>")
        self.root.update()

    def test_header_text_counts_checked_rows(self) -> None:
        self.assertEqual(self.list.header_text(), "檔名（已勾選 6／6）")
        self.checked[1] = self.checked[4] = False
        self._show()
        self.assertEqual(self.list.header_text(), "檔名（已勾選 4／6）")
        self.assertEqual(self.list.header.itemcget("title", "text"), "檔名（已勾選 4／6）")
        self.assertEqual(self.list.header.itemcget("column0", "text"), "所在資料夾")
        self.list.set_rows([])
        self.assertEqual(self.list.header_text(), "檔名")

    def test_click_on_checkbox_only_toggles(self) -> None:
        x, y = self.list.checkbox_center(1)
        self._click(self.list.tree, x, y)
        self.assertEqual(self.calls, [("toggle", 1)])
        self.assertEqual(self.list.tree.selection(), ())
        self.assertIn(OFF_TAG, self.list.tree.item("1", "tags"))
        self.assertNotIn(OFF_TAG, self.list.tree.item("0", "tags"))
        self.assertEqual(str(self.list.tree.item("1", "image")[0]), str(self.list._images[(False, None)]))

        self._click(self.list.tree, x, y)
        self.assertEqual(self.calls[-1], ("toggle", 1))
        self.assertNotIn(OFF_TAG, self.list.tree.item("1", "tags"))

    def test_click_on_row_selects_and_space_toggles_it(self) -> None:
        self._click_row_text(2)
        self.assertEqual(self.calls, [("select", 2)])
        self.assertEqual(self.list.tree.selection(), ("2",))
        self.assertEqual(self.checked, [True] * 6, "點列不改勾選")

        self._key("space")
        self.assertEqual(self.calls[-1], ("toggle", 2))
        self.assertFalse(self.checked[2])
        self.assertEqual(self.list.tree.selection(), ("2",), "切換勾選後仍選著同一列")

        self._key("Down")
        self.assertEqual(self.calls[-1], ("select", 3))
        self.assertEqual(self.list.selected, 3)

    def test_click_below_rows_clears_selection(self) -> None:
        self.checked = [True, True]
        self.selected = 0
        self._show()
        _x, y, width, height = self.list.tree.bbox("1")
        self._click(self.list.tree, width // 2, y + height * 2)
        self.assertEqual(self.calls, [("select", None)])
        self.assertEqual(self.list.tree.selection(), ())

    def test_header_checkbox_checks_all_or_none(self) -> None:
        middle = self.list.header.winfo_height() // 2
        box_x = self.list._border + ROW_PAD_X + CHECKBOX_SIZE // 2
        self._click(self.list.header, box_x, middle)
        self.assertEqual(self.calls, [("all", False)], "全部勾著時按一下全不勾")
        self.assertEqual(self.list.header_text(), "檔名（已勾選 0／6）")

        self.checked[0] = True
        self._show()
        self._click(self.list.header, box_x, middle)
        self.assertEqual(self.calls[-1], ("all", True), "有沒勾的就全勾")

        self._click(self.list.header, self.list.header.winfo_width() // 2, middle)
        self.assertEqual(len(self.calls), 2, "點表頭的字不動作")

    def test_scroll_position_is_kept_and_scrollbar_hides_when_rows_fit(self) -> None:
        self.checked = [True] * 30
        self._show()
        self.root.update()
        self.assertTrue(self.list.scrollbar.winfo_ismapped())
        self.list.tree.yview_moveto(0.5)
        self.root.update()
        before = self.list.tree.yview()
        self._toggle(20)
        self.root.update()
        self.assertEqual(self.list.tree.yview(), before)

        self.checked = [True] * 2
        self._show()
        self.root.update()
        self.assertFalse(self.list.scrollbar.winfo_ismapped())

    def test_hover_tag_follows_pointer(self) -> None:
        _x, y, _w, height = self.list.tree.bbox("3")
        self.list.tree.event_generate("<Motion>", x=40, y=y + height // 2)
        self.root.update()
        self.assertIn(HOVER_TAG, self.list.tree.item("3", "tags"))
        self.list.tree.event_generate("<Leave>")
        self.root.update()
        self.assertNotIn(HOVER_TAG, self.list.tree.item("3", "tags"))

    def test_colors_follow_appearance_mode(self) -> None:
        style = ttk.Style(self.list)
        for mode, i in (("Light", 0), ("Dark", 1)):
            ctk.set_appearance_mode(mode)
            self.root.update()
            self.assertEqual(style.lookup(STYLE_NAME, "fieldbackground"), T["panel"][i])
            self.assertEqual(style.lookup(STYLE_NAME, "foreground"), T["text"][i])
            self.assertEqual([tuple(map(str, entry)) for entry in style.map(STYLE_NAME, "background")], [("selected", T["soft"][i])])
            self.assertEqual(str(self.list.tree.tag_configure(OFF_TAG, "foreground")), T["muted"][i])
            self.assertEqual(str(self.list.tree.tag_configure(HOVER_TAG, "background")), T["hover"][i])
            self.assertEqual(self.list.header.cget("background"), T["bg"][i])



class TestExpandableCheckList(unittest.TestCase):
    """圖資快覽的清單：三欄、DXF 的 CAD 圖層是子列。"""

    def setUp(self) -> None:
        apply_ctk_theme()
        self._mode = ctk.get_appearance_mode()
        self.root = ctk.CTk()
        self.root.geometry("640x360+40+40")
        self.calls: List[Tuple[str, Any]] = []
        self.checked = [True, True]
        self.layers = [True, True, True]
        self.selected: Optional[int] = None
        self.list = CheckList(
            self.root,
            title="檔名",
            columns=(("類型", 64), ("原始座標系", 120)),
            expandable=True,
            visible_rows=6,
            on_select=self._select,
            on_toggle=self._toggle,
            on_toggle_child=self._toggle_child,
        )
        self.list.pack(fill="x", padx=16, pady=16)
        self._show()
        deadline = time.monotonic() + 5
        while not self.list.tree.winfo_viewable() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()

    def tearDown(self) -> None:
        self.root.destroy()
        ctk.set_appearance_mode(self._mode)

    def _select(self, index: Optional[int]) -> None:
        self.calls.append(("select", index))
        self.selected = index
        self._show()

    def _toggle(self, index: int) -> None:
        self.calls.append(("toggle", index))
        self.checked[index] = not self.checked[index]
        self._show()

    def _toggle_child(self, index: int, child: int) -> None:
        self.calls.append(("child", (index, child)))
        self.layers[child] = not self.layers[child]
        self._show()

    def _show(self) -> None:
        children = tuple(CheckRow(name, checked=on) for name, on in zip(("中心線", "文字", "路權線"), self.layers))
        rows = [
            CheckRow("範例宗地.shp", ("向量", "TWD97 121"), self.checked[0]),
            CheckRow("範例路線.dxf", ("CAD", "無標籤"), self.checked[1], children),
        ]
        self.list.set_rows(rows, self.selected)

    def _click(self, x: int, y: int) -> None:
        self.list.tree.event_generate("<Button-1>", x=x, y=y)
        self.root.update()

    def _image_of(self, iid: str) -> str:
        return str(self.list.tree.item(iid, "image")[0])

    def test_columns_and_header_are_aligned(self) -> None:
        tree = self.list.tree
        self.assertEqual(tree.item("0", "values"), ("向量", "TWD97 121"))
        self.assertEqual(self.list.header.itemcget("column0", "text"), "類型")
        self.assertEqual(self.list.header.itemcget("column1", "text"), "原始座標系")
        pad = round(ROW_PAD_X * self.list._scaling)
        for index in range(2):
            cell_x = tree.bbox("0", f"c{index}")[0]
            header_x = self.list.header.coords(f"column{index}")[0]
            self.assertEqual(header_x, tree.winfo_x() + cell_x + pad)
        # 每列都留展開鈕的位置，勾選框才對得齊；表頭也是
        self.assertEqual(self._image_of("0"), str(self.list._images[(True, CHEVRON_NONE)]))
        x0 = round((ROW_PAD_X + CHEVRON_SIZE + CHEVRON_GAP) * self.list._scaling)
        self.assertEqual(self.list.checkbox_center(0)[0], x0 + round(8 * self.list._scaling))

    def test_children_expand_and_collapse(self) -> None:
        tree = self.list.tree
        self.assertEqual(len(tree.get_children("1")), 3)
        self.assertFalse(self.list.is_open(1), "預設收合")
        self.assertEqual(self._image_of("1"), str(self.list._images[(True, CHEVRON_CLOSED)]))
        self._click(*self.list.chevron_center(1))
        self.assertTrue(self.list.is_open(1))
        self.assertEqual(self._image_of("1"), str(self.list._images[(True, CHEVRON_OPEN)]))
        self.assertEqual(self.calls, [], "展開不算選取也不算勾選")
        self._show()
        self.assertTrue(self.list.is_open(1), "換內容後展開狀態不變")
        self._click(*self.list.chevron_center(1))
        self.assertFalse(self.list.is_open(1))

    def test_child_checkbox_and_parent_muted(self) -> None:
        self.list.set_open(1, True)
        self.root.update()
        self._click(*self.list.checkbox_center(1, 2))
        self.assertEqual(self.calls, [("child", (1, 2))])
        self.assertIn(OFF_TAG, self.list.tree.item("1/2", "tags"))
        self.assertNotIn(OFF_TAG, self.list.tree.item("1/0", "tags"))
        self.assertEqual(self.list.header_text(), "檔名（已勾選 2／2）", "表頭只算最上層的列")

        self._click(*self.list.checkbox_center(1))
        self.assertEqual(self.calls[-1], ("toggle", 1))
        self.assertIn(OFF_TAG, self.list.tree.item("1/0", "tags"), "父列沒勾時子列也是 muted")

    def test_click_on_child_selects_its_parent(self) -> None:
        self.list.set_open(1, True)
        self.root.update()
        _x, y, width, height = self.list.tree.bbox("1/1")
        self._click(width // 2, y + height // 2)
        self.assertEqual(self.calls, [("select", 1)])
        self.assertEqual(self.list.tree.selection(), ("1/1",), "選取的底色留在點的那一列")
        self.list.tree.focus_force()
        self.root.update()
        self.list.tree.event_generate("<space>")
        self.root.update()
        self.assertEqual(self.calls[-1], ("child", (1, 1)), "空白鍵切換選取的那個子列")


if __name__ == "__main__":
    unittest.main()
