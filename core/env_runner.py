"""環境隔離與原生 CLI 呼叫執行器 (core/env_runner.py)

嚴格遵守專案規格書第 0 節鐵律：
1. 目標電腦安裝有舊版 ArcMap 10.x (32-bit Python 2.7)，絕對禁止更動 Windows 全域環境變數。
2. 所有 GDAL/OGR、WhiteboxTools、SQLite 運算皆透過獨立沙盒 ./runtime 內的原生二進位 CLI 執行。
3. 支援非同步背景執行緒、進度回呼與即時 GDAL 進度碼解析，嚴禁阻塞 GUI。
4. 提供 Windows CREATE_NO_WINDOW 靜音執行，防止控制台黑窗頻繁閃爍。
"""

from __future__ import annotations

import codecs
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

# 配置模組專用日誌記錄器
logger = logging.getLogger("core.env_runner")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [env_runner] %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# Windows 旗標：隱藏子行程跳出的黑底命令列視窗 (CREATE_NO_WINDOW = 0x08000000)
_CREATE_NO_WINDOW: int = 0x08000000 if sys.platform == "win32" else 0

# GDAL CLI 的進度列：整行只有「0...10...20...100 - done.」，一個字一個字印出來
_GDAL_PROGRESS_HEAD_RE = re.compile(r"0(?:\.+\d{1,3})*")
_GDAL_PROGRESS_DONE_TEXT = " - done."
# WhiteboxTools 的進度列：整行是「英文標題: 45%」，例如 "Progress: 45%"
_LABELED_PERCENT_LINE_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _()/\-]{0,60}:\s*(\d{1,3}(?:\.\d+)?)\s*%")
# 一行超過這個長度就不可能是進度列，不再累積
_PROGRESS_LINE_LIMIT = 400


def _parse_gdal_progress_line(line: str, line_complete: bool) -> Optional[float]:
    """解讀 GDAL 進度列目前印到的百分比；整行不是進度列時回傳 None。

    line_complete 為 False 表示這一行還在繼續輸出，行尾的數字可能只印了一半 (例如 100 只印出 1)，不採計。
    """
    head = _GDAL_PROGRESS_HEAD_RE.match(line)
    if head is None:
        return None
    rest = line[head.end() :]
    finished = rest.rstrip() in (_GDAL_PROGRESS_DONE_TEXT.rstrip(), _GDAL_PROGRESS_DONE_TEXT.rstrip("."))
    dots_only = rest.strip(".") == ""
    done_in_progress = not line_complete and _GDAL_PROGRESS_DONE_TEXT.startswith(rest)
    if not (finished or dots_only or done_in_progress):
        return None

    numbers = [float(token) for token in head.group(0).split(".") if token]
    if not rest and not line_complete:
        numbers = numbers[:-1]
    if any(later < earlier for earlier, later in zip(numbers, numbers[1:])) or (numbers and numbers[-1] > 100.0):
        return None
    if finished:
        return 100.0
    if len(numbers) < 2:
        # 只有開頭的 0 還看不出是不是進度列
        return None
    return numbers[-1]


class _ProgressParser:
    """從子行程的標準輸出逐段找出進度百分比。

    只認兩種「整行都是進度」的格式 (GDAL 的點狀進度列、WhiteboxTools 的「標題: 45%」)，
    訊息內文裡碰巧出現的「50%」「檔名_30...」「done」都不算。
    """

    def __init__(self) -> None:
        self._line = ""
        self._overflow = False
        self._last_value = -1.0

    def feed(self, text: str) -> List[float]:
        """餵入新收到的文字，回傳這一段新出現的進度值 (與上次相差不到 1 的不回報)。"""
        values: List[float] = []
        for piece in re.split(r"([\r\n])", text):
            if piece in ("\r", "\n"):
                self._collect(values, line_complete=True)
                self._line = ""
                self._overflow = False
            elif piece and not self._overflow:
                self._line += piece
                if len(self._line) > _PROGRESS_LINE_LIMIT:
                    self._line = ""
                    self._overflow = True
                else:
                    self._collect(values, line_complete=False)
        return values

    def _collect(self, values: List[float], line_complete: bool) -> None:
        line = self._line.strip() if line_complete else self._line.lstrip()
        if not line:
            return
        value = _parse_gdal_progress_line(line, line_complete)
        if value is None and line_complete:
            labeled = _LABELED_PERCENT_LINE_RE.fullmatch(line)
            if labeled is not None:
                value = float(labeled.group(1))
        if value is None or not 0.0 <= value <= 100.0:
            return
        if abs(value - self._last_value) >= 1.0:
            self._last_value = value
            values.append(value)


# 目前執行緒正在進行的作業所用的取消事件 (由 cancellation_scope 設定)
_cancel_scope = threading.local()


class ToolkitError(Exception):
    """WingLightGIS 基礎例外類別。"""


class CommandExecutionError(ToolkitError):
    """CLI 指令執行失敗（回傳代碼非 0）時拋出的例外。"""

    def __init__(self, cmd: List[str], returncode: int, stdout: str, stderr: str, duration: float) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        super().__init__(
            f"外部 CLI 指令執行失敗 (代碼 {returncode})。\n"
            f"指令: {' '.join(cmd)}\n"
            f"耗時: {duration:.2f} 秒\n"
            f"錯誤訊息: {stderr.strip() or stdout.strip() or '無錯誤詳細訊息'}"
        )


class CommandCancelledError(ToolkitError):
    """使用者主動中斷或取消背景 CLI 作業時拋出的例外。"""


@contextlib.contextmanager
def cancellation_scope(cancel_event: Optional[threading.Event]) -> Iterator[Optional[threading.Event]]:
    """在 with 區塊內，讓本執行緒所有沒有另外指定 cancel_event 的 run_command 都受這個事件控制。

    引擎函式不必逐層傳遞 cancel_event：UI 把事件交給 run_in_background，使用者按取消時
    正在跑的 CLI 會被終止，之後的 CLI 也不會再啟動。
    """
    previous = getattr(_cancel_scope, "event", None)
    _cancel_scope.event = cancel_event
    try:
        yield cancel_event
    finally:
        _cancel_scope.event = previous


def get_active_cancel_event() -> Optional[threading.Event]:
    """回傳本執行緒目前生效的取消事件；不在 cancellation_scope 內時為 None。"""
    return getattr(_cancel_scope, "event", None)


def raise_if_cancelled() -> None:
    """使用者已要求取消時拋 CommandCancelledError；供引擎在不呼叫 CLI 的長迴圈中檢查。"""
    event = get_active_cancel_event()
    if event is not None and event.is_set():
        raise CommandCancelledError("作業已由使用者取消。")


class BinaryNotFoundError(ToolkitError):
    """在沙盒或系統路徑中未找到指定的 CLI 執行檔。"""


@dataclass
class CommandResult:
    """CLI 指令執行成果資料結構。"""

    cmd: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    cancelled: bool = False

    @property
    def is_success(self) -> bool:
        """確認執行結果是否成功 (returncode == 0 且未被中斷)。"""
        return self.returncode == 0 and not self.cancelled


@dataclass
class EnvironmentHealth:
    """獨立執行環境健全度診斷報告。"""

    toolkit_root: Path
    runtime_dir: Path
    is_runtime_present: bool
    python_exe: Optional[Path]
    gdal_bin_dir: Optional[Path]
    gdal_data_dir: Optional[Path]
    proj_lib_dir: Optional[Path]
    whitebox_exe: Optional[Path]
    installed_tools: Dict[str, Optional[Path]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """確認核心 GDAL/OGR 與 Python 環境是否已就緒。"""
        return (
            self.is_runtime_present
            and self.python_exe is not None
            and self.installed_tools.get("ogr2ogr") is not None
            and self.installed_tools.get("gdalwarp") is not None
        )


def get_toolkit_root() -> Path:
    """取得 WingLightGIS 的專案根目錄絕對路徑。"""
    # 當前檔案位於 core/env_runner.py，上一層即為專案根目錄
    return Path(__file__).resolve().parent.parent


def get_runtime_dir() -> Path:
    """取得獨立沙盒 ./runtime 目錄絕對路徑。"""
    return get_toolkit_root() / "runtime"


# 暫存目錄與設定目錄的覆寫值；None 表示未覆寫（自動依程式目錄可寫入與否決定位置）。
# 僅影響本行程，供整合測試與 --self-test 把所有寫入導向 tempfile 目錄，避免污染正式資料夾。
_workspace_temp_override: Optional[Path] = None
_config_dir_override: Optional[Path] = None
_default_output_override: Optional[Path] = None
_toolkit_writable_cache: Optional[bool] = None
_APPDATA_DIR_NAME: str = "WingLightGIS"
_DEFAULT_CONFIG_FILES: Tuple[str, ...] = ("recipes.json", "terrain_ramp.txt")


def get_appdata_root() -> Path:
    """取得使用者的 AppData 資料夾路徑 (%APPDATA%\\WingLightGIS)，作為唯讀安裝目錄的退回位置。"""
    appdata_env = os.environ.get("APPDATA")
    base = Path(appdata_env).resolve() if appdata_env else (Path.home() / "AppData" / "Roaming")
    return base / _APPDATA_DIR_NAME


def reset_writable_cache() -> None:
    """清除程式目錄可寫入狀態快取（主要供單元測試使用）。"""
    global _toolkit_writable_cache
    _toolkit_writable_cache = None


def is_toolkit_root_writable() -> bool:
    """實測程式所在根目錄是否可寫入（結果快取於記憶體）。

    Windows 上對 C:\\Program Files 呼叫 os.access(..., os.W_OK) 會誤回 True，
    因此實際嘗試建立並刪除暫存測試檔來確認寫入權限。
    """
    global _toolkit_writable_cache
    if _toolkit_writable_cache is not None:
        return _toolkit_writable_cache
    root = get_toolkit_root()
    probe = root / f".wl_write_probe_{os.getpid()}_{threading.get_ident()}.tmp"
    try:
        with open(probe, "w", encoding="utf-8"):
            pass
        probe.unlink(missing_ok=True)
        _toolkit_writable_cache = True
    except OSError:
        _toolkit_writable_cache = False
        logger.info("程式所在目錄無法寫入，設定／暫存／預設輸出將改存於 %s", get_appdata_root())
    return _toolkit_writable_cache


def _resolve_writable_base_dir() -> Path:
    """程式目錄可寫入時回傳程式目錄，否則退回 %APPDATA%\\WingLightGIS。"""
    return get_toolkit_root() if is_toolkit_root_writable() else get_appdata_root()


def set_workspace_temp_dir(path: Optional[Union[str, Path]]) -> None:
    """改寫執行期中繼暫存目錄；傳入 None 還原為預設位置。"""
    global _workspace_temp_override
    _workspace_temp_override = Path(path).resolve() if path is not None else None


def get_workspace_temp_dir() -> Path:
    """取得執行期中繼暫存目錄絕對路徑，若不存在則自動建立。"""
    temp_dir = _workspace_temp_override or (_resolve_writable_base_dir() / "workspace_temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def set_default_output_dir(path: Optional[Union[str, Path]]) -> None:
    """改寫預設成果資料夾；傳入 None 還原為預設位置。"""
    global _default_output_override
    _default_output_override = Path(path).resolve() if path is not None else None


def get_default_output_dir() -> Path:
    """取得預設成果資料夾絕對路徑 (不在暫存區內，清理暫存時不會被刪)；不會自動建立，真的要寫成果時才由呼叫端建立。"""
    return _default_output_override or (_resolve_writable_base_dir() / "outputs")


def set_config_dir(path: Optional[Union[str, Path]]) -> None:
    """改寫設定檔目錄；傳入 None 還原為預設位置。"""
    global _config_dir_override
    _config_dir_override = Path(path).resolve() if path is not None else None


def _seed_appdata_config_defaults(appdata_config_dir: Path) -> None:
    """初次退回 %APPDATA% 時，把程式目錄隨附的預設 recipes.json / terrain_ramp.txt 複製過去。"""
    bundled_config = get_toolkit_root() / "config"
    if not bundled_config.is_dir():
        return
    for name in _DEFAULT_CONFIG_FILES:
        src = bundled_config / name
        dst = appdata_config_dir / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError as err:
                logger.warning("複製預設設定檔 %s 至 AppData 失敗: %s", name, err)


def get_config_dir() -> Path:
    """取得設定檔目錄 (recipes.json、terrain_ramp.txt、WingLightGIS.json) 絕對路徑，若不存在則自動建立。"""
    if _config_dir_override is not None:
        config_dir = _config_dir_override
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir
    base_dir = _resolve_writable_base_dir()
    config_dir = base_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if base_dir != get_toolkit_root():
        _seed_appdata_config_defaults(config_dir)
    return config_dir


_warned_non_ascii_env_paths: set = set()


def _env_safe_path(target_dir: Path, working_dir: Path) -> str:
    """回傳可安全放進環境變數的目錄路徑字串。

    Windows 版 GDAL 以 ANSI 代碼頁讀取環境變數，卻把內容當成 UTF-8 解讀，路徑含中文時整個目錄會找不到
    (實測：GDAL_DATA 指向中文路徑時寫出 DXF 會因找不到 header.dxf 而失敗)。
    子行程的工作目錄固定時，改用相對於工作目錄的純 ASCII 相對路徑即可避開。
    """
    absolute_path = str(target_dir)
    if absolute_path.isascii():
        return absolute_path
    try:
        relative_path = os.path.relpath(absolute_path, str(working_dir))
    except ValueError:
        relative_path = absolute_path
    if relative_path.isascii():
        return relative_path
    if absolute_path not in _warned_non_ascii_env_paths:
        _warned_non_ascii_env_paths.add(absolute_path)
        logger.warning("環境變數路徑含非 ASCII 字元且無法以相對路徑表示，GDAL 可能讀不到此目錄: %s", absolute_path)
    return absolute_path


def build_isolated_env(
    extra_env: Optional[Dict[str, str]] = None,
    *,
    working_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, str]:
    """建立完全隔離 ArcMap 10.x 宿主環境的區域環境變數字典。

    核心防護規範：
    1. 複製現有環境變數，但自 PATH 中徹底濾除任何包含 ArcGIS、Python27、ArcMap 之殘留路徑。
    2. 強制清空全域 PYTHONHOME，避免 Python 3.11 誤讀 Python 2.7 標準庫。
    3. 將 ./runtime, ./runtime/Library/bin, ./runtime/Scripts 置於 PATH 最前端。
    4. 明確綁定 GDAL_DATA 與 PROJ_LIB / PROJ_DATA 至沙盒目錄，解決座標轉換七參數遺失地雷。
    5. 強制啟用 UTF-8 編碼設定，避免中文檔名或地段名破音亂碼。

    working_dir 為子行程的工作目錄 (預設專案根目錄)，用來把含中文的 GDAL_DATA、PROJ_LIB 換成相對路徑。
    """
    env = os.environ.copy()
    toolkit_root = get_toolkit_root()
    runtime_dir = get_runtime_dir()
    child_working_dir = Path(working_dir).resolve() if working_dir else toolkit_root

    # 1. 宿主 ArcMap 2.7 污染清道夫：移除危險的全域變數
    for harmful_key in ("PYTHONHOME", "GDAL_DRIVER_PATH", "PROJ_SEARCH_PATH"):
        env.pop(harmful_key, None)

    # 2. 嚴格過濾 PATH 中任何與 ArcGIS / Python27 有關的污染項目
    current_path_items = env.get("PATH", "").split(os.pathsep)
    clean_path_items: List[str] = []
    for item in current_path_items:
        item_lower = item.lower()
        if any(bad_token in item_lower for bad_token in ("arcgis", "python27", "arcmap", "desktop10.")):
            logger.debug("已自沙盒 PATH 中隔離宿主路徑: %s", item)
            continue
        if item.strip():
            clean_path_items.append(item)

    # 3. 組合獨立沙盒路徑
    runtime_bin = runtime_dir / "Library" / "bin"
    runtime_usr_bin = runtime_dir / "Library" / "usr" / "bin"
    runtime_scripts = runtime_dir / "Scripts"
    runtime_gdal_data = runtime_dir / "Library" / "share" / "gdal"
    runtime_proj_data = runtime_dir / "Library" / "share" / "proj"

    sandbox_prefixes = [
        str(runtime_dir),
        str(runtime_bin),
        str(runtime_usr_bin),
        str(runtime_scripts),
    ]

    # 合併沙盒路徑置於最優先順位
    env["PATH"] = os.pathsep.join(sandbox_prefixes + clean_path_items)

    # 4. 綁定 GDAL 與 PROJ 字典與圖資常數
    if runtime_gdal_data.exists():
        env["GDAL_DATA"] = _env_safe_path(runtime_gdal_data, child_working_dir)
    if runtime_proj_data.exists():
        # 專案路徑含中文時 PROJ 開不了絕對路徑的 proj.db，與 GDAL_DATA 一樣改用相對路徑；
        # 不再把 proj.db 複製到 %LOCALAPPDATA%/proj (該目錄的搜尋順序在 PROJ_DATA 之前，會影響同機其他軟體)
        proj_dir_for_env = _env_safe_path(runtime_proj_data, child_working_dir)
        env["PROJ_LIB"] = proj_dir_for_env
        env["PROJ_DATA"] = proj_dir_for_env

    # 5. 強制各底層 C++ 函式庫與 Python IO 採用 UTF-8
    env["PYTHONPATH"] = str(toolkit_root)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["GDAL_FILENAME_IS_UTF8"] = "YES"
    # 全域 SHAPE_ENCODING 會蓋掉來源的 .cpg 與輸出的 -lco ENCODING，一律移除 (啟動用的 .bat 會設這個變數)
    env.pop("SHAPE_ENCODING", None)
    env["CPL_ZIP_ENCODING"] = "CP950"

    # 套用使用者額外自訂的環境變數覆寫
    if extra_env:
        for k, v in extra_env.items():
            if v is not None:
                env[k] = str(v)

    return env


def resolve_binary(binary_name: str) -> str:
    """在沙盒環境或系統中解析可執行檔之絕對路徑。

    優先搜尋順序：
    1. ./runtime/Library/bin/{binary_name}.exe (GDAL/OGR C++ 原生工具)
    2. ./runtime/Scripts/{binary_name}.exe 或 .py (如 ogrmerge.py, gdal_calc.py)
    3. ./runtime/{binary_name}.exe (如 python.exe, pythonw.exe)
    4. 系統 PATH 中搜尋 (作為本機開發備用，但若包含 ArcMap 則自動警示)
    """
    runtime_dir = get_runtime_dir()
    clean_name = Path(binary_name).name
    name_without_ext = clean_name[:-4] if clean_name.lower().endswith(".exe") else clean_name

    candidate_extensions = ["", ".exe", ".bat", ".cmd", ".py"]

    search_dirs = [
        runtime_dir / "Library" / "bin",
        runtime_dir / "Library" / "usr" / "bin",
        runtime_dir / "Scripts",
        runtime_dir,
    ]

    for sdir in search_dirs:
        for ext in candidate_extensions:
            target = sdir / f"{name_without_ext}{ext}"
            if target.is_file():
                return str(target.resolve())

    # 若沙盒中找不到，嘗試從 shutil.which 透過 build_isolated_env 的 PATH 尋找
    isolated_path = build_isolated_env().get("PATH", "")
    system_found = shutil.which(clean_name, path=isolated_path)
    if system_found:
        return str(Path(system_found).resolve())

    # 特殊處理：Python 官方提供的 GDAL 腳本 (如 ogrmerge, gdal_calc)
    python_scripts_candidate = runtime_dir / "Scripts" / f"{name_without_ext}.py"
    if python_scripts_candidate.is_file():
        return str(python_scripts_candidate.resolve())

    # 若皆找不到，回傳原始名稱並記錄警告，由 subprocess 自行嘗試
    logger.debug("未於沙盒中精確定位執行檔: %s，將嘗試直接呼叫名字符號。", binary_name)
    return binary_name


def check_environment_health() -> EnvironmentHealth:
    """全面檢測本機獨立沙盒環境的配置狀態與核心工具可用性。"""
    toolkit_root = get_toolkit_root()
    runtime_dir = get_runtime_dir()
    is_runtime_present = runtime_dir.is_dir()

    python_candidate = runtime_dir / "python.exe"
    python_exe = python_candidate if python_candidate.is_file() else None

    gdal_bin_candidate = runtime_dir / "Library" / "bin"
    gdal_bin_dir = gdal_bin_candidate if gdal_bin_candidate.is_dir() else None

    gdal_data_candidate = runtime_dir / "Library" / "share" / "gdal"
    gdal_data_dir = gdal_data_candidate if gdal_data_candidate.is_dir() else None

    proj_lib_candidate = runtime_dir / "Library" / "share" / "proj"
    proj_lib_dir = proj_lib_candidate if proj_lib_candidate.is_dir() else None

    whitebox_candidate = runtime_dir / "Library" / "bin" / "whitebox_tools.exe"
    whitebox_exe = whitebox_candidate if whitebox_candidate.is_file() else None

    tools_to_check = [
        "ogr2ogr",
        "ogrinfo",
        "gdalwarp",
        "gdal_translate",
        "gdaldem",
        "gdalbuildvrt",
        "gdal_contour",
        "gdallocationinfo",
        "gdaladdo",
        "whitebox_tools",
    ]

    installed_tools: Dict[str, Optional[Path]] = {}
    warnings: List[str] = []

    for tool in tools_to_check:
        resolved = resolve_binary(tool)
        p = Path(resolved)
        if p.is_file():
            installed_tools[tool] = p
        else:
            installed_tools[tool] = None
            warnings.append(f"缺少 CLI 工具: {tool}")

    if not is_runtime_present:
        warnings.append("尚未建立 ./runtime 獨立沙盒目錄，請先執行 setup_portable_env.bat！")
    elif python_exe is None:
        warnings.append("沙盒中未找到 python.exe！")

    return EnvironmentHealth(
        toolkit_root=toolkit_root,
        runtime_dir=runtime_dir,
        is_runtime_present=is_runtime_present,
        python_exe=python_exe,
        gdal_bin_dir=gdal_bin_dir,
        gdal_data_dir=gdal_data_dir,
        proj_lib_dir=proj_lib_dir,
        whitebox_exe=whitebox_exe,
        installed_tools=installed_tools,
        warnings=warnings,
    )


def _decode_stream_chunk(chunk: bytes) -> str:
    """相容性解碼位元組字串，優先以 UTF-8 解碼，若遇中文 Windows 系統代碼頁則回退 CP950。"""
    try:
        return chunk.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return chunk.decode("cp950", errors="replace")
        except Exception:
            return chunk.decode("latin-1", errors="replace")


def run_command(
    cmd_args: Sequence[Union[str, Path]],
    *,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    text_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    check: bool = True,
    timeout: Optional[float] = None,
    cwd: Optional[Union[Path, str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> CommandResult:
    """執行外部 CLI 指令，嚴格落實非同步進度解析與沙盒環境變數隔離。

    參數：
        cmd_args: 指令與參數串列，例如 ["ogr2ogr", "-f", "GPKG", "out.gpkg", "in.shp"]。
        progress_callback: 進度百分比回呼函式，簽章為 callback(percent: float, message: str)。
        text_callback: 終端機即時日誌列回呼函式，簽章為 callback(log_line: str)。
        cancel_event: 取消事件物件，被設定時立即強制終止子行程並拋 CommandCancelledError；
            未指定時採用 cancellation_scope 設定的事件。
        check: 若為 True 且子行程 returncode != 0，主動拋出 CommandExecutionError。
        timeout: 逾時秒數限制，逾時自動中止。
        cwd: 工作目錄，預設為專案根目錄。
        extra_env: 附加至沙盒環境變數之鍵值字典。
        input_text: 要餵給子行程標準輸入的文字 (UTF-8)，例如 gdallocationinfo 的批次座標；未指定時標準輸入為空。

    回傳：
        CommandResult 物件，包含 returncode, stdout, stderr, 耗時秒數等詳細資訊。
    """
    if not cmd_args:
        raise ValueError("執行的命令列參數串列不得為空！")

    # 將所有參數轉換為字串型態，並嘗試解析主程式路徑
    str_args: List[str] = [str(arg) for arg in cmd_args]
    primary_executable = str_args[0]

    # 特殊判斷：若呼叫的是 .py 腳本且開頭不是 python，自動補上沙盒 python.exe
    if primary_executable.lower().endswith(".py"):
        python_exe = resolve_binary("python")
        str_args = [python_exe] + str_args
    else:
        str_args[0] = resolve_binary(primary_executable)

    # 建立純淨隔離之執行環境變數 (GDAL_DATA 的相對路徑需以實際工作目錄為基準)
    working_dir = str(cwd) if cwd else str(get_toolkit_root())
    env = build_isolated_env(extra_env, working_dir=working_dir)

    command_str = " ".join(f'"{a}"' if " " in a else a for a in str_args)
    if cancel_event is None:
        cancel_event = get_active_cancel_event()
    if cancel_event is not None and cancel_event.is_set():
        raise CommandCancelledError(f"作業已取消，未執行指令: {command_str}")
    logger.debug("正在執行沙盒 CLI 指令: %s", command_str)
    if text_callback:
        text_callback(f"[EXEC] {command_str}\n")

    start_time = time.time()
    cancelled = False

    try:
        proc = subprocess.Popen(
            str_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            cwd=working_dir,
            env=env,
            creationflags=_CREATE_NO_WINDOW,
            bufsize=0,  # 無緩衝以實現毫秒級即時進度攔截
        )
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"找不到可執行的二進位檔案: {str_args[0]}") from err

    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []

    # 執行緒中繼緩衝區讀取器
    def _stream_reader(
        pipe: Optional[Any],
        accumulator: List[str],
        is_stderr: bool,
    ) -> None:
        if pipe is None:
            return
        line_buffer = ""
        # 進度只看標準輸出；標準錯誤是警告與錯誤訊息
        progress_parser = _ProgressParser() if progress_callback and not is_stderr else None
        # 逐段讀取會把中文字切在位元組中間，用增量解碼器把殘缺的位元組留到下一段；
        # 遇到不是 UTF-8 的輸出 (中文 Windows 的系統訊息) 就整條串流改用 CP950
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

        try:
            while True:
                # 每次讀取少量 byte 以便捕捉 GDAL 的即時進度點
                raw_bytes = pipe.read(64)
                if not raw_bytes:
                    break

                pending_bytes = decoder.getstate()[0]
                try:
                    text = decoder.decode(raw_bytes)
                except UnicodeDecodeError:
                    decoder = codecs.getincrementaldecoder("cp950")(errors="replace")
                    text = decoder.decode(pending_bytes + raw_bytes)
                if not text:
                    continue
                accumulator.append(text)
                line_buffer += text

                if progress_parser is not None:
                    for p_val in progress_parser.feed(text):
                        progress_callback(p_val, f"處理進度: {p_val:.0f}%")

                # 當遇到換行或回車時，向終端機回報整行記錄
                while "\n" in line_buffer or "\r" in line_buffer:
                    split_idx = min(
                        [idx for idx in (line_buffer.find("\n"), line_buffer.find("\r")) if idx != -1]
                    )
                    full_line = line_buffer[:split_idx].strip()
                    line_buffer = line_buffer[split_idx + 1 :]
                    if full_line and text_callback:
                        prefix = "[STDERR] " if is_stderr else ""
                        text_callback(f"{prefix}{full_line}\n")

            # 串流結束時若還有湊不成一個字的殘餘位元組，改以 CP950 解讀
            pending_bytes = decoder.getstate()[0]
            try:
                tail_text = decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                tail_text = pending_bytes.decode("cp950", errors="replace")
            if tail_text:
                accumulator.append(tail_text)
                line_buffer += tail_text

            # 刷新剩餘未換行文字
            if line_buffer.strip() and text_callback:
                prefix = "[STDERR] " if is_stderr else ""
                text_callback(f"{prefix}{line_buffer.strip()}\n")

        finally:
            try:
                pipe.close()
            except Exception as pipe_err:
                logger.debug("關閉輸出管線略過: %s", pipe_err)

    t_out = threading.Thread(target=_stream_reader, args=(proc.stdout, stdout_chunks, False), daemon=True)
    t_err = threading.Thread(target=_stream_reader, args=(proc.stderr, stderr_chunks, True), daemon=True)
    t_out.start()
    t_err.start()

    # 標準輸入另開執行緒寫入：子行程邊讀邊輸出時，在主執行緒一次寫完會與輸出管線互相卡死
    def _stdin_writer() -> None:
        try:
            proc.stdin.write(input_text.encode("utf-8"))
        except OSError as stdin_err:
            logger.debug("寫入標準輸入中斷 (子行程已結束): %s", stdin_err)
        finally:
            try:
                proc.stdin.close()
            except OSError as stdin_err:
                logger.debug("關閉標準輸入略過: %s", stdin_err)

    if input_text is not None:
        threading.Thread(target=_stdin_writer, daemon=True).start()

    # 監控程序主迴圈，檢查取消事件與逾時
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            logger.warning("接收到使用者取消訊號，強制終止 CLI 程序！")
            proc.kill()
            cancelled = True
            break

        if timeout is not None and (time.time() - start_time) > timeout:
            proc.kill()
            proc.wait()
            raise TimeoutError(f"CLI 指令逾時強制中止 ({timeout} 秒): {command_str}")

        time.sleep(0.05)

    proc.wait()
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    duration = time.time() - start_time
    full_stdout = "".join(stdout_chunks)
    full_stderr = "".join(stderr_chunks)

    if cancelled:
        if text_callback:
            text_callback("[CANCEL] 使用者已主動取消作業。\n")
        raise CommandCancelledError(f"指令已取消: {command_str}")

    result = CommandResult(
        cmd=str_args,
        returncode=proc.returncode,
        stdout=full_stdout,
        stderr=full_stderr,
        duration_seconds=duration,
        cancelled=cancelled,
    )

    if check and result.returncode != 0:
        raise CommandExecutionError(
            cmd=str_args,
            returncode=result.returncode,
            stdout=full_stdout,
            stderr=full_stderr,
            duration=duration,
        )

    if progress_callback and result.is_success:
        progress_callback(100.0, "完成")

    return result


def run_json_command(
    cmd_args: Sequence[Union[str, Path]],
    *,
    timeout: Optional[float] = 30.0,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """執行輸出 JSON 格式的 GDAL/OGR 查詢指令（如 ogrinfo -json, gdalinfo -json），並解析為 Python 字典。

    參數：
        cmd_args: 例如 ["ogrinfo", "-json", "-so", "cadastre.shp", "cadastre"]
        timeout: 逾時時間
        extra_env: 額外環境變數

    回傳：
        解析後的 Python Dict 物件。
    """
    result = run_command(cmd_args, check=True, timeout=timeout, extra_env=extra_env)
    stdout_text = result.stdout.strip()

    if not stdout_text:
        # 有時 ogrinfo 會在 stderr 輸出警告，但 stdout 為空
        raise ToolkitError(
            f"執行 JSON 查詢時未收到任何資料。\n"
            f"指令: {' '.join(str(a) for a in cmd_args)}\n"
            f"STDERR: {result.stderr}"
        )

    # 針對少數含有開頭非 JSON 字串（如 GDAL 警告訊息）進行正規化定位 '{'
    json_start = stdout_text.find("{")
    json_end = stdout_text.rfind("}")

    if json_start != -1 and json_end != -1 and json_end > json_start:
        cleaned_json = stdout_text[json_start : json_end + 1]
    else:
        cleaned_json = stdout_text

    try:
        return json.loads(cleaned_json)
    except json.JSONDecodeError as err:
        raise ToolkitError(
            f"無法解析 CLI 輸出的 JSON 資料: {err}\n原始輸出內容前 500 字元:\n{stdout_text[:500]}"
        ) from err


def run_in_background(
    target_func: Callable,
    *args: Any,
    on_success: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
    on_finish: Optional[Callable[[], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    dispatcher: Optional[Callable[[Callable[[], None]], None]] = None,
    **kwargs: Any,
) -> threading.Thread:
    """在背景獨立執行緒中執行耗時之核心空間計算任務，避免導致 CustomTkinter GUI 介面未回應。

    參數：
        target_func: 欲非同步執行的核心函式。
        *args, **kwargs: 傳入 target_func 的參數。
        on_success: 函式執行成功時於主執行緒或安全情境調用的回呼 (傳入傳回值)。
        on_error: 發生例外時調用的回呼 (傳入 Exception 物件)。
        on_finish: 無論成功或失敗皆會調用的收尾回呼。
        cancel_event: 取消事件；設定後 target_func 內正在執行的 CLI 會被終止、後續 CLI 不再啟動，
            並以 CommandCancelledError 呼叫 on_error (即使 target_func 自己吞掉例外也一樣)，不會呼叫 on_success。
        dispatcher: 把回呼轉交給其他執行緒執行的函式 (收一個無參數的可呼叫物件)。UI 傳入能排進主執行緒的函式，
            on_success／on_error／on_finish 就都在主執行緒執行；未指定時三個回呼直接在背景執行緒呼叫。

    回傳：
        已啟動之 threading.Thread 實例。
    """

    def _call(callback: Optional[Callable[..., None]], *callback_args: Any) -> None:
        if callback is None:
            return
        if dispatcher is None:
            callback(*callback_args)
        else:
            dispatcher(lambda: callback(*callback_args))

    def _worker() -> None:
        try:
            try:
                with cancellation_scope(cancel_event):
                    result = target_func(*args, **kwargs)
                    raise_if_cancelled()
            except CommandCancelledError as exc:
                logger.info("背景空間任務已取消: %s", exc)
                _call(on_error, exc)
            except Exception as exc:
                logger.exception("背景空間任務執行發生異常: %s", exc)
                _call(on_error, exc)
            else:
                _call(on_success, result)
        finally:
            _call(on_finish)

    worker_thread = threading.Thread(target=_worker, daemon=True)
    worker_thread.start()
    return worker_thread
