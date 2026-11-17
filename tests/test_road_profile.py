"""[A-03] 路線縱橫斷面擷取測試 (tests/test_road_profile.py)

涵蓋 P1-6：
1. 落在 DEM 範圍外或 NoData 的點高程留空，後面樁號的高程不會往前錯位。
2. 左右標籤以前進方向為準 (左側偏移量為負)。
3. 終點樁號一定會補上；橫斷面寬度不是間距整數倍時補上邊緣點。
4. 多段線組成的中心線會串接；串不起來時拋錯。
5. 成果 GPKG 帶 DEM 的座標系；中心線座標系不同時先重投影；無標籤 DEM 依範圍推測，
   無標籤中心線視為與 DEM 同座標系 (有指派時照指派)。
6. 高程查詢經 run_command (隔離環境) 並以標準輸入批次送出座標。
"""

from __future__ import annotations

import csv
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

import numpy as np
import openpyxl

from core import geo_io, terrain_lab
from core.env_runner import ToolkitError, resolve_binary, run_command
from core.terrain_lab import extract_road_profile_and_cross_sections
from tests._support import (
    IsolatedWorkspaceTestCase,
    attribute_rows,
    convert_vector,
    line,
    strip_prj,
    write_dem,
    write_geojson,
)

# write_dem 預設範圍：X 250000～250100、Y 2650100～2650200 (20×20 格、5 m 網格)


def east_ramp() -> np.ndarray:
    """高程 = 由西往東的欄號 (0～19)。"""
    return np.tile(np.arange(20, dtype=np.float32), (20, 1))


def south_ramp() -> np.ndarray:
    """高程 = 由北往南的列號 (0～19)。"""
    return np.tile(np.arange(20, dtype=np.float32).reshape(20, 1), (1, 20))


def read_csv_rows(csv_path: Path) -> List[Dict[str, str]]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def elevation(row: Dict[str, str]) -> Optional[float]:
    return float(row["地面高程_Z"]) if row["地面高程_Z"] != "" else None


class RoadProfileTest(IsolatedWorkspaceTestCase):
    """extract_road_profile_and_cross_sections 的整合測試。"""

    def _line(self, *points: Tuple[float, float], name: str = "中心線", epsg: int = 3826) -> Path:
        return write_geojson(self.data_dir / f"{name}.geojson", [({"NAME": "道路"}, line(*points))], epsg=epsg)

    def _run(self, line_path: Path, dem_path: Path, **options: Any) -> Tuple[Path, Path, List[str]]:
        messages: List[str] = []
        csv_path, gpkg_path = extract_road_profile_and_cross_sections(
            line_path, dem_path, output_dir=self.out_dir, text_callback=messages.append, **options
        )
        return csv_path, gpkg_path, messages

    def test_points_outside_dem_keep_their_row_and_end_station_is_added(self) -> None:
        grid = east_ramp()
        grid[9, 8] = -9999.0  # 樁號 80 所在的格子設為 NoData
        dem = write_dem(self.data_dir / "高程.tif", grid)
        # 起點在 DEM 西側範圍外 37.5 m，全長 85 m (不是 20 m 的整數倍)
        centerline = self._line((249962.5, 2650152.5), (250047.5, 2650152.5))

        csv_path, gpkg_path, messages = self._run(centerline, dem, station_interval=20.0, cross_section_width=0.0)

        rows = read_csv_rows(csv_path)
        self.assertEqual(
            [row["樁號標籤"] for row in rows],
            ["0K+000.00", "0K+020.00", "0K+040.00", "0K+060.00", "0K+080.00", "0K+085.00"],
        )
        # 前兩點在範圍外、樁號 80 是 NoData：都留空，其餘高程對到自己的樁號
        self.assertEqual([elevation(row) for row in rows], [None, None, 0.0, 4.0, None, 9.0])
        self.assertEqual(float(rows[-1]["里程累積距離_m"]), 85.0)
        self.assertEqual(float(rows[-1]["X坐標"]), 250047.5)
        self.assertTrue(any("3 個取樣點落在 DEM 範圍外或 NoData" in message for message in messages))

        # GPKG：有座標系、範圍外的點高程為 NULL
        info = geo_io.read_layer_info(gpkg_path)
        self.assertEqual(info.epsg, 3826)
        self.assertEqual(info.feature_count, 6)
        self.assertEqual([row["ELEV_Z"] for row in attribute_rows(gpkg_path)], [None, None, 0.0, 4.0, None, 9.0])

        # Excel 與 CSV 同名並列，內容一致
        sheet = openpyxl.load_workbook(csv_path.with_suffix(".xlsx")).active
        excel_rows = list(sheet.iter_rows(values_only=True))
        self.assertEqual(excel_rows[0][0], "樁號標籤")
        self.assertEqual([row[5] for row in excel_rows[1:]], [None, None, 0.0, 4.0, None, 9.0])

        # 暫存檔都清掉了
        self.assertEqual(list(self.work_temp.iterdir()), [])

    def test_left_is_left_of_travel_direction(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", south_ramp())
        # 往東走：左側是北邊 (列號較小、高程較低)，右側是南邊
        centerline = self._line((250022.5, 2650152.5), (250062.5, 2650152.5))

        csv_path, gpkg_path, _ = self._run(
            centerline, dem, station_interval=20.0, cross_section_width=10.0, cross_section_step=5.0
        )

        rows = read_csv_rows(csv_path)
        self.assertEqual(len(rows), 15)
        first_station = rows[:5]
        self.assertEqual([float(row["斷面偏移量_m"]) for row in first_station], [-10.0, -5.0, 0.0, 5.0, 10.0])
        self.assertEqual(
            [row["點位類別"] for row in first_station],
            ["CROSS_SECTION_LEFT", "CROSS_SECTION_LEFT", "CENTERLINE", "CROSS_SECTION_RIGHT", "CROSS_SECTION_RIGHT"],
        )
        self.assertEqual([float(row["Y坐標"]) for row in first_station], [2650162.5, 2650157.5, 2650152.5, 2650147.5, 2650142.5])
        self.assertEqual([elevation(row) for row in first_station], [7.0, 8.0, 9.0, 10.0, 11.0])

        left_rows = [row for row in attribute_rows(gpkg_path) if row["TYPE"] == "CROSS_SECTION_LEFT"]
        self.assertTrue(left_rows)
        self.assertTrue(all(row["OFFSET_M"] < 0 and row["ELEV_Z"] < 9.0 for row in left_rows))

    def test_left_follows_direction_when_heading_south(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        # 往南走：左側是東邊 (欄號較大、高程較高)
        centerline = self._line((250052.5, 2650180.0), (250052.5, 2650140.0))

        csv_path, _, _ = self._run(centerline, dem, station_interval=40.0, cross_section_width=5.0, cross_section_step=5.0)

        first_station = read_csv_rows(csv_path)[:3]
        self.assertEqual([row["點位類別"] for row in first_station], ["CROSS_SECTION_LEFT", "CENTERLINE", "CROSS_SECTION_RIGHT"])
        self.assertEqual([elevation(row) for row in first_station], [11.0, 10.0, 9.0])

    def test_section_edge_is_added_when_width_is_not_a_multiple_of_step(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", south_ramp())
        centerline = self._line((250022.5, 2650152.5), (250042.5, 2650152.5))

        csv_path, _, _ = self._run(centerline, dem, station_interval=20.0, cross_section_width=12.0, cross_section_step=5.0)

        offsets = [float(row["斷面偏移量_m"]) for row in read_csv_rows(csv_path)[:7]]
        self.assertEqual(offsets, [-12.0, -10.0, -5.0, 0.0, 5.0, 10.0, 12.0])

    def test_multi_feature_centerline_is_chained(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        # 兩個圖徵：第二段是 MultiLineString 而且畫的方向相反 (CAD 常見)
        centerline = write_geojson(
            self.data_dir / "分段中心線.geojson",
            [
                ({"NAME": "前段"}, line((250012.5, 2650152.5), (250052.5, 2650152.5))),
                ({"NAME": "後段"}, {"type": "MultiLineString", "coordinates": [[[250052.5, 2650192.5], [250052.5, 2650152.5]]]}),
            ],
        )

        csv_path, _, messages = self._run(centerline, dem, station_interval=20.0, cross_section_width=0.0)

        rows = read_csv_rows(csv_path)
        self.assertEqual([float(row["里程累積距離_m"]) for row in rows], [0.0, 20.0, 40.0, 60.0, 80.0])
        self.assertEqual((float(rows[0]["X坐標"]), float(rows[0]["Y坐標"])), (250012.5, 2650152.5))
        self.assertEqual((float(rows[-1]["X坐標"]), float(rows[-1]["Y坐標"])), (250052.5, 2650192.5))
        self.assertEqual([elevation(row) for row in rows], [2.0, 6.0, 10.0, 10.0, 10.0])
        self.assertTrue(any("2 段線串接" in message for message in messages))

    def test_disconnected_centerline_is_rejected(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        centerline = write_geojson(
            self.data_dir / "斷開的中心線.geojson",
            [
                ({"NAME": "前段"}, line((250012.5, 2650152.5), (250032.5, 2650152.5))),
                ({"NAME": "後段"}, line((250042.5, 2650152.5), (250062.5, 2650152.5))),
            ],
        )
        with self.assertRaisesRegex(ToolkitError, "中心線不連續"):
            self._run(centerline, dem)

    def test_unlabelled_line_and_dem_are_guessed_and_output_is_labelled(self) -> None:
        dem = write_dem(self.data_dir / "無標籤高程.tif", east_ramp(), epsg=None)
        shp = strip_prj(convert_vector(self._line((250002.5, 2650152.5), (250042.5, 2650152.5)), self.data_dir / "無標籤中心線.shp"))

        csv_path, gpkg_path, messages = self._run(shp, dem, station_interval=20.0, cross_section_width=0.0)

        self.assertEqual([elevation(row) for row in read_csv_rows(csv_path)], [0.0, 4.0, 8.0])
        self.assertEqual(geo_io.read_layer_info(gpkg_path).epsg, 3826)
        # 只有 DEM 依範圍推測；中心線視為與 DEM 相同
        self.assertEqual(sum("座標系為推測值" in message for message in messages), 1)
        self.assertTrue(any("視為與 DEM 相同" in message for message in messages))

    def test_unlabelled_line_follows_twd67_dem_without_shift(self) -> None:
        dem = write_dem(self.data_dir / "高程67.tif", east_ramp(), epsg=3828)
        # 中心線沒有標籤、座標與 DEM 同為 TWD67：不可被當成 TWD97 平移約 830 m
        shp = strip_prj(convert_vector(self._line((250002.5, 2650152.5), (250042.5, 2650152.5)), self.data_dir / "無標籤中心線.shp"))

        csv_path, gpkg_path, messages = self._run(shp, dem, station_interval=20.0, cross_section_width=0.0)

        rows = read_csv_rows(csv_path)
        self.assertEqual([elevation(row) for row in rows], [0.0, 4.0, 8.0])
        self.assertAlmostEqual(float(rows[0]["X坐標"]), 250002.5, delta=0.01)
        self.assertAlmostEqual(float(rows[0]["Y坐標"]), 2650152.5, delta=0.01)
        self.assertEqual(geo_io.read_layer_info(gpkg_path).epsg, 3828)
        self.assertFalse(any("座標系為推測值" in message for message in messages))

    def test_assigned_srs_still_applies_to_unlabelled_line(self) -> None:
        dem = write_dem(self.data_dir / "高程67.tif", east_ramp(), epsg=3828)
        # 使用者明確指派 TWD97：無標籤中心線照指派重投影到 DEM 的 TWD67
        shp = strip_prj(
            convert_vector(
                self._line((250030.0 + 828.0, 2650150.0 - 207.0), (250070.0 + 828.0, 2650150.0 - 207.0)),
                self.data_dir / "無標籤中心線97.shp",
            )
        )

        csv_path, _, _ = self._run(shp, dem, station_interval=20.0, cross_section_width=0.0, assigned_srs="TWD97_121")

        rows = read_csv_rows(csv_path)
        self.assertAlmostEqual(float(rows[0]["X坐標"]), 250030.0, delta=5.0)
        self.assertAlmostEqual(float(rows[0]["Y坐標"]), 2650150.0, delta=5.0)

    def test_centerline_in_other_srs_is_reprojected_to_dem_srs(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        source = self._line((250002.5, 2650152.5), (250042.5, 2650152.5))
        zone_119 = convert_vector(source, self.data_dir / "中心線119.gpkg", extra_args=["-t_srs", "EPSG:3825"])
        self.assertEqual(geo_io.read_layer_info(zone_119).epsg, 3825)

        csv_path, gpkg_path, _ = self._run(zone_119, dem, station_interval=20.0, cross_section_width=0.0)

        rows = read_csv_rows(csv_path)
        self.assertEqual([elevation(row) for row in rows], [0.0, 4.0, 8.0])
        self.assertAlmostEqual(float(rows[0]["X坐標"]), 250002.5, delta=0.01)
        self.assertAlmostEqual(float(rows[0]["Y坐標"]), 2650152.5, delta=0.01)
        self.assertEqual(geo_io.read_layer_info(gpkg_path).epsg, 3826)

    def test_dem_labelled_twd67_keeps_its_label_and_line_is_shifted(self) -> None:
        dem = write_dem(self.data_dir / "高程67.tif", east_ramp(), epsg=3828)
        # 中心線是 TWD97，位置相當於 DEM (TWD67 座標) 的中段：TWD67 → TWD97 約往東 828 m、往南 207 m
        centerline = self._line((250030.0 + 828.0, 2650150.0 - 207.0), (250070.0 + 828.0, 2650150.0 - 207.0))

        csv_path, gpkg_path, _ = self._run(centerline, dem, station_interval=20.0, cross_section_width=0.0)

        rows = read_csv_rows(csv_path)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(elevation(row) is not None for row in rows))
        self.assertAlmostEqual(float(rows[0]["X坐標"]), 250030.0, delta=5.0)
        self.assertAlmostEqual(float(rows[0]["Y坐標"]), 2650150.0, delta=5.0)
        self.assertEqual(geo_io.read_layer_info(gpkg_path).epsg, 3828)

    def test_dxf_centerline_without_srs(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        dxf = convert_vector(self._line((250002.5, 2650152.5), (250042.5, 2650152.5), (250042.5, 2650172.5)), self.data_dir / "中心線.dxf")

        csv_path, gpkg_path, _ = self._run(dxf, dem, station_interval=20.0, cross_section_width=0.0)

        rows = read_csv_rows(csv_path)
        self.assertEqual([float(row["里程累積距離_m"]) for row in rows], [0.0, 20.0, 40.0, 60.0])
        self.assertEqual([elevation(row) for row in rows], [0.0, 4.0, 8.0, 8.0])
        self.assertEqual(geo_io.read_layer_info(gpkg_path).epsg, 3826)

    def test_line_without_overlap_is_rejected(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        centerline = self._line((251000.0, 2650150.0), (251100.0, 2650150.0))
        with self.assertRaisesRegex(ToolkitError, "沒有重疊"):
            self._run(centerline, dem)

    def test_geographic_dem_is_rejected(self) -> None:
        dem = write_dem(
            self.data_dir / "經緯度高程.tif", east_ramp(), origin_x=121.0, top_y=24.002, cell_size=0.0001, epsg=4326
        )
        centerline = self._line((121.0002, 24.001), (121.0012, 24.001), epsg=4326)
        with self.assertRaisesRegex(ToolkitError, "經緯度"):
            self._run(centerline, dem)

    def test_non_positive_interval_is_rejected(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        centerline = self._line((250002.5, 2650152.5), (250042.5, 2650152.5))
        with self.assertRaises(ValueError):
            self._run(centerline, dem, station_interval=0.0)

    def test_elevations_are_queried_through_run_command_with_stdin(self) -> None:
        dem = write_dem(self.data_dir / "高程.tif", east_ramp())
        centerline = self._line((250002.5, 2650152.5), (250042.5, 2650152.5))

        with mock.patch.object(terrain_lab, "run_command", wraps=terrain_lab.run_command) as spy:
            self._run(centerline, dem, station_interval=20.0, cross_section_width=0.0)

        location_calls = [call for call in spy.call_args_list if call.args[0][0] == "gdallocationinfo"]
        self.assertEqual(len(location_calls), 1)
        self.assertEqual(len(location_calls[0].kwargs["input_text"].splitlines()), 3)


class RunCommandInputTextTest(IsolatedWorkspaceTestCase):
    """run_command 的 input_text 參數。"""

    def test_input_text_reaches_child_stdin(self) -> None:
        text = "".join(f"第{i}列 座標\n" for i in range(5000))
        script = "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"
        result = run_command([resolve_binary("python"), "-c", script], input_text=text)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), text)

    def test_stdin_is_empty_without_input_text(self) -> None:
        script = "import sys; print(len(sys.stdin.read()))"
        result = run_command([resolve_binary("python"), "-c", script])
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
