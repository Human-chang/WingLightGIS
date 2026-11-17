"""Milestone 1 單元測試驗證 (tests/test_boundary.py)

驗證項目：
1. 傳入未閉合之 4 點 TWD97 座標字串，確認智慧解析器正確萃取。
2. 自動閉鎖 (Auto-Close Ring)：確認能自動補上第 5 點（等於首點），使環閉合。
3. 鞋帶公式 (Shoelace Formula)：確認面積精確無誤 (例如 100m x 100m 正方形 = 10,000 m² = 1.0 ha)。
4. 欄位自動倒置修正：確認當輸入為 (Y, X) 順序時能自動辨識並對調為標準 (X, Y)。
5. 形心極角排序 (atan2)：確認能將交錯頂點依逆時針排序，消除沙漏自相交。
6. 座標嗅探器 (CRS Sniffer)：驗證 TWD97, TWD67, WGS84, CAD 公釐 (mm) 之狀態與燈號。
7. GDAL -tap 致命語法防呆：驗證單獨使用 -tap 未提供解析度時能主動攔截並拋出例外。
8. CAD GCP 控制點一階仿射變換：驗證最小平方法矩陣計算與 RMSE。
"""

import math
import unittest
from pathlib import Path

from core.boundary_builder import (
    CoordinatePoint,
    auto_close_ring,
    calculate_shoelace_area,
    parse_coordinate_text,
    sort_points_by_polar_angle,
)
from core.crs_doctor import (
    ControlPoint,
    Extent2D,
    build_tap_parameters,
    calculate_affine_transform,
    diagnose_extent,
)
from tests._support import IsolatedWorkspaceTestCase


class TestBoundaryBuilderAndCrsDoctor(IsolatedWorkspaceTestCase):
    """Milestone 1 核心功能單元測試案例。"""

    def test_unclosed_four_points_auto_close_and_shoelace_area(self) -> None:
        """測試規格書驗證點：未閉合之 4 點 TWD97 座標字串，確認自動補上第 5 點並算出正確面積。"""
        # 定義一個 100m x 100m 之正方形工區，故意僅給 4 個頂點且首尾未閉合
        # 正確面積應為 100 * 100 = 10,000 m² = 1.000000 公頃 (ha)
        raw_clipboard_text = """
        P1\t250000.0\t2600000.0
        P2\t250100.0\t2600000.0
        P3\t250100.0\t2600100.0
        P4\t250000.0\t2600100.0
        """

        # 1. 驗證智慧文字解析
        pts = parse_coordinate_text(raw_clipboard_text)
        self.assertEqual(len(pts), 4, "應精確解析出 4 個原始頂點")
        self.assertAlmostEqual(pts[0].x, 250000.0)
        self.assertAlmostEqual(pts[0].y, 2600000.0)

        # 2. 驗證自動首尾閉鎖功能 (Auto-Close)
        closed_pts, was_auto_closed = auto_close_ring(pts)
        self.assertTrue(was_auto_closed, "系統必須偵測到末點與首點不合並自動閉合")
        self.assertEqual(len(closed_pts), 5, "閉合後之頂點數量應為 5 (首點複製追加至末端)")

        # 第 5 點必須與第 1 點完全吻合
        self.assertAlmostEqual(closed_pts[-1].x, closed_pts[0].x, places=4)
        self.assertAlmostEqual(closed_pts[-1].y, closed_pts[0].y, places=4)

        # 3. 驗證鞋帶公式計算之幾何面積
        area_m2, area_ha = calculate_shoelace_area(closed_pts)
        self.assertAlmostEqual(area_m2, 10000.0, places=2, msg="100m x 100m 正方形面積應精確為 10000 平方公尺")
        self.assertAlmostEqual(area_ha, 1.0, places=4, msg="10000 平方公尺換算應為 1.0000 公頃")

    def test_reversed_column_order_auto_detection(self) -> None:
        """測試使用者誤將 Y 貼在第 1 欄、X 貼在第 2 欄時的智慧欄位修正。"""
        # 輸入格式為: [Y座標 (7位數), X座標 (6位數)]
        inverted_text = """
        2600000.0, 250000.0
        2600000.0, 250100.0
        2601000.0, 250100.0
        2601000.0, 250000.0
        """
        pts = parse_coordinate_text(inverted_text)
        self.assertEqual(len(pts), 4)

        # 系統必須自動將 6 位數識別為 X，7 位數識別為 Y
        self.assertAlmostEqual(pts[0].x, 250000.0, msg="系統應自動將 250000 識別並指派為 X")
        self.assertAlmostEqual(pts[0].y, 2600000.0, msg="系統應自動將 2600000 識別並指派為 Y")

        # 面積應為 100m * 1000m = 100,000 m² = 10 ha
        area_m2, area_ha = calculate_shoelace_area(pts)
        self.assertAlmostEqual(area_m2, 100000.0, places=2)
        self.assertAlmostEqual(area_ha, 10.0, places=4)

    def test_polar_angle_sorting_for_bowtie_polygon(self) -> None:
        """測試順序錯亂時依形心極角自動排序 (防止沙漏自相交)。"""
        # 故意打亂頂點順序為對角交錯：P1 -> P3 -> P2 -> P4
        # 若直接連線會形成 8 字形 / 沙漏形 (自相交多邊形)
        jumbled_pts = [
            CoordinatePoint(x=0.0, y=0.0),
            CoordinatePoint(x=10.0, y=10.0),
            CoordinatePoint(x=10.0, y=0.0),
            CoordinatePoint(x=0.0, y=10.0),
        ]

        # 排序前若直接算面積會互相抵消歸零
        bad_area, _ = calculate_shoelace_area(jumbled_pts)
        self.assertAlmostEqual(bad_area, 0.0, places=2, msg="自相交沙漏多邊形直接計算面積會抵消為 0")

        # 套用形心極角排序修復
        sorted_pts = sort_points_by_polar_angle(jumbled_pts)
        self.assertEqual(len(sorted_pts), 5, "排序並閉合後應為 5 點")

        # 排序後應計算出正確正方形面積 10 * 10 = 100 m²
        fixed_area, _ = calculate_shoelace_area(sorted_pts)
        self.assertAlmostEqual(fixed_area, 100.0, places=2, msg="極角排序修復後面積應回復為 100 m²")

    def test_crs_sniffer_diagnose_extent(self) -> None:
        """測試 [G-01] 座標自動嗅探邏輯與警示燈號。"""
        # 1. 正常 TWD97 二度分帶 (帶 3826 標籤)
        ext_twd97 = Extent2D(min_x=250000.0, min_y=2650000.0, max_x=251000.0, max_y=2651000.0)
        diag_97 = diagnose_extent(ext_twd97, detected_epsg=3826)
        self.assertEqual(diag_97.status_level, "GREEN")
        self.assertEqual(diag_97.status_code, "TWD97_STANDARD")

        # 2. 無標籤之臺灣二度分帶 (TM2)
        diag_unlabeled = diagnose_extent(ext_twd97, detected_epsg=None, raw_srs_name=None)
        self.assertEqual(diag_unlabeled.status_level, "YELLOW")
        self.assertEqual(diag_unlabeled.status_code, "TM2_UNLABELED")
        self.assertEqual(diag_unlabeled.recommended_assign_srs, "EPSG:3826")

        # 3. 標註為舊版 TWD67 (存在 850m 基準面偏移地雷)
        diag_67 = diagnose_extent(ext_twd97, detected_epsg=3828)
        self.assertEqual(diag_67.status_level, "YELLOW")
        self.assertEqual(diag_67.status_code, "TWD67_NEED_7PARAM")
        self.assertIn("+towgs84=", str(diag_67.recommended_assign_srs))

        # 4. WGS84 經緯度
        ext_wgs84 = Extent2D(min_x=121.50, min_y=25.00, max_x=121.55, max_y=25.05)
        diag_wgs = diagnose_extent(ext_wgs84, detected_epsg=4326)
        self.assertEqual(diag_wgs.status_level, "GREEN")
        self.assertEqual(diag_wgs.status_code, "WGS84_STANDARD")

        # 5. CAD 以公釐 (mm) 繪製導致數值放大 1000 倍 (大於 1 億)
        ext_cad_mm = Extent2D(min_x=250000000.0, min_y=2650000000.0, max_x=251000000.0, max_y=2651000000.0)
        diag_mm = diagnose_extent(ext_cad_mm)
        self.assertEqual(diag_mm.status_level, "RED")
        self.assertEqual(diag_mm.status_code, "CAD_MM_SCALE")
        self.assertTrue(diag_mm.needs_unit_scaling)
        self.assertAlmostEqual(diag_mm.scale_factor, 0.001)

    def test_gdal_tap_parameter_guard(self) -> None:
        """測試 [G-04] GDAL -tap 致命語法防呆：嚴禁單獨傳遞 -tap。"""
        # 1. 正常傳入目標解析度：應成功生成 -tr 與 -tap
        tap_args = build_tap_parameters(target_res_x=5.0, target_res_y=5.0)
        self.assertEqual(tap_args, ["-tr", "5", "5", "-tap"])

        # 2. 致命錯誤：未傳入任何解析度與原圖 -> 必須拋出 ValueError 攔截崩潰
        with self.assertRaises(ValueError) as ctx:
            build_tap_parameters(target_res_x=None, target_res_y=None, raster_path=None)
        self.assertIn("致命防呆攔截", str(ctx.exception))

        # 3. 傳入負數解析度：必須拋出例外
        with self.assertRaises(ValueError):
            build_tap_parameters(target_res_x=-5.0, target_res_y=5.0)

    def test_affine_transform_calculation(self) -> None:
        """測試 [G-03] 控制點一階仿射變換矩陣與 RMSE 最小平方法運算。"""
        # 設定已知變換：平移 (+100, +200) 且放大 2 倍
        # X_dst = 2 * X_src + 100
        # Y_dst = 2 * Y_src + 200
        pts = [
            ControlPoint(src_x=0.0, src_y=0.0, dst_x=100.0, dst_y=200.0),
            ControlPoint(src_x=10.0, src_y=0.0, dst_x=120.0, dst_y=200.0),
            ControlPoint(src_x=0.0, src_y=10.0, dst_x=100.0, dst_y=220.0),
            ControlPoint(src_x=10.0, src_y=10.0, dst_x=120.0, dst_y=220.0),
        ]

        result = calculate_affine_transform(pts)
        self.assertAlmostEqual(result.a, 2.0, places=5)
        self.assertAlmostEqual(result.b, 0.0, places=5)
        self.assertAlmostEqual(result.c, 100.0, places=5)
        self.assertAlmostEqual(result.d, 0.0, places=5)
        self.assertAlmostEqual(result.e, 2.0, places=5)
        self.assertAlmostEqual(result.f, 200.0, places=5)
        self.assertAlmostEqual(result.rmse, 0.0, places=5, msg="無誤差條件下 RMSE 應趨近於 0")

        # 驗證任意測試點之仿射轉換
        test_x, test_y = result.transform(5.0, 5.0)
        self.assertAlmostEqual(test_x, 110.0, places=5)
        self.assertAlmostEqual(test_y, 210.0, places=5)


if __name__ == "__main__":
    unittest.main()
