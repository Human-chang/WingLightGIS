"""只開一份 WingLightGIS (ui/single_instance.py)

把 WingLightGIS.exe 設成 .shp 的預設開啟程式後在檔案總管點兩下，或從命令列帶檔案開啟時，
如果已經有一份開著，就把檔案交給那一份，不另開視窗：
1. 第一份程式建立具名 Mutex，並在背景執行緒開具名管道收檔案路徑 (InstanceServer)。
2. 後開的看到 Mutex 已經存在，就經管道把路徑交過去後結束 (claim_or_hand_off 回傳 None)；
   第一份還在啟動、管道還沒開好時每 0.1 秒再試，最多 HANDOFF_TIMEOUT_S 秒 (第一份在這期間結束了就自己當第一份)。
   沒有帶檔案時也交 (空的清單)，第一份把視窗叫到前面。
3. 名稱含程式資料夾的雜湊 (instance_names)：開發版和 D:\\APP 那份各算一份，不會互相交檔。
4. 管道只傳 UTF-8 的 JSON 文字 (send_bytes／recv_bytes)，不經 pickle；收到看不懂的訊息就略過。
啟動器 (launcher/WingLightGIS.cs) 用同一個 Mutex 名稱判斷要不要顯示啟動畫面。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger("ui.single_instance")

NAME_PREFIX: str = "WingLightGIS"
# 第一份還在啟動 (電腦剛開機時最慢約 7.6 秒) 時，後開的最多等這麼久
HANDOFF_TIMEOUT_S: float = 15.0
RETRY_INTERVAL_S: float = 0.1
MAX_MESSAGE_BYTES: int = 1 << 20
ACK: bytes = b"ok"
HANDOFF_FAILED_TEXT: str = "WingLightGIS 已經開著，但沒有回應，沒辦法把檔案交給它。請關掉那一份再開一次。"
_ERROR_ALREADY_EXISTS = 183
_ASFW_ANY = -1

# 自己的 WinDLL，設 argtypes 不影響其他模組用的 ctypes.windll
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL

PathsHandler = Callable[[List[str]], None]


class HandOffError(RuntimeError):
    """已經有一份在執行，但沒辦法把檔案交給它。"""


@dataclass(frozen=True)
class InstanceNames:
    mutex: str
    pipe: str


def instance_names(app_root: Path, prefix: str = NAME_PREFIX) -> InstanceNames:
    """依程式資料夾算出 Mutex 與管道的名稱。

    資料夾路徑去掉結尾的斜線、轉小寫，取 UTF-8 的 SHA-1 前 12 碼 (啟動器的算法相同)。
    Mutex 在 Local\\ 底下 (只在這次登入有效)；管道不分登入，名稱另外加上工作階段編號。
    """
    folder = str(app_root).rstrip("\\/").lower()
    digest = hashlib.sha1(folder.encode("utf-8")).hexdigest()[:12]
    return InstanceNames(
        mutex=f"Local\\{prefix}-{digest}",
        pipe=f"\\\\.\\pipe\\{prefix}-{digest}-{_session_id()}",
    )


def default_names() -> InstanceNames:
    """這份程式的名稱 (程式資料夾＝ui 的上一層)。"""
    return instance_names(Path(__file__).resolve().parent.parent)


def _session_id() -> int:
    session = wintypes.DWORD(0)
    if not _kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return 0
    return session.value


def _create_mutex(name: str) -> Tuple[int, bool]:
    """建立 (或打開已經有的) 具名 Mutex，回傳 (handle, 之前就有)。"""
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, name)
    already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    if not handle:
        raise OSError(ctypes.get_last_error(), f"沒辦法建立 Mutex {name}")
    return handle, already


def _encode(paths: Sequence[str]) -> bytes:
    return json.dumps({"paths": [str(p) for p in paths]}, ensure_ascii=False).encode("utf-8")


def _decode(message: bytes) -> List[str]:
    data = json.loads(message.decode("utf-8"))
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("訊息裡沒有路徑清單")
    return paths


class InstanceServer:
    """第一份程式：持有 Mutex，在背景執行緒收後開的那幾份交來的路徑。

    一建立就開始收 (主視窗還沒好時，後開的也不用等)；set_handler 之前收到的先存著，設了再一次交出。
    handler 在背景執行緒呼叫，要碰畫面的話自己排回主執行緒。
    """

    def __init__(self, names: InstanceNames, mutex_handle: int) -> None:
        self.names = names
        self._mutex: Optional[int] = mutex_handle
        self._listener = Listener(names.pipe, family="AF_PIPE")
        self._lock = threading.Lock()
        self._handler: Optional[PathsHandler] = None
        self._pending: List[List[str]] = []
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="single-instance", daemon=True)
        self._thread.start()

    def set_handler(self, handler: PathsHandler) -> None:
        # 在鎖裡交，才不會跟同時收到的下一筆搶先後
        with self._lock:
            self._handler = handler
            pending, self._pending = self._pending, []
            for paths in pending:
                handler(paths)

    def _deliver(self, paths: List[str]) -> None:
        with self._lock:
            if self._handler is None:
                self._pending.append(paths)
            else:
                self._handler(paths)

    def _serve(self) -> None:
        while True:
            try:
                conn = self._listener.accept()
            except OSError as err:
                if not self._closed:
                    logger.warning("單一執行個體的管道停止接收: %s", err)
                return
            with conn:
                if self._closed:
                    return
                try:
                    paths = _decode(conn.recv_bytes(MAX_MESSAGE_BYTES))
                except (OSError, EOFError, ValueError) as err:
                    logger.warning("略過看不懂的單一執行個體訊息: %s", err)
                    continue
                # 先交出去 (或存著) 再回覆，後開的那一份收到回覆就表示檔案已經排進來
                try:
                    self._deliver(paths)
                except Exception:
                    logger.exception("處理交來的檔案失敗")
                try:
                    conn.send_bytes(ACK)
                except OSError as err:
                    logger.warning("回覆後開的那一份失敗: %s", err)

    def close(self) -> None:
        """停止接收並放掉 Mutex (程式結束時系統也會放，測試用)。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # accept() 在背景執行緒等著，連一次讓它醒來看到 _closed
        try:
            Client(self.names.pipe, family="AF_PIPE").close()
        except OSError:
            pass
        self._thread.join(timeout=5)
        self._listener.close()
        if self._mutex:
            _kernel32.CloseHandle(self._mutex)
            self._mutex = None


def _send(pipe: str, message: bytes) -> None:
    with Client(pipe, family="AF_PIPE") as conn:
        conn.send_bytes(message)
        if conn.recv_bytes(len(ACK) + 16) != ACK:
            raise HandOffError(HANDOFF_FAILED_TEXT)


def claim_or_hand_off(
    paths: Sequence[str],
    names: Optional[InstanceNames] = None,
    timeout: float = HANDOFF_TIMEOUT_S,
) -> Optional[InstanceServer]:
    """是第一份就回傳開始接收的 InstanceServer；不是就把 paths 交給第一份後回傳 None。

    第一份還在啟動、管道還沒開好時重試到 timeout 秒；交不出去時拋 HandOffError。
    """
    names = names or default_names()
    message = _encode(paths)
    deadline = time.monotonic() + timeout
    allowed_foreground = False
    while True:
        handle, already = _create_mutex(names.mutex)
        if not already:
            return InstanceServer(names, handle)
        _kernel32.CloseHandle(handle)
        if not allowed_foreground:
            # 讓第一份可以把視窗叫到最前面 (Windows 只准使用者正在操作的程式搶前景)
            _user32.AllowSetForegroundWindow(_ASFW_ANY)
            allowed_foreground = True
        try:
            _send(names.pipe, message)
            return None
        except FileNotFoundError:
            # 第一份的管道還沒開好 (或它正在結束)
            pass
        except (OSError, EOFError) as err:
            raise HandOffError(HANDOFF_FAILED_TEXT) from err
        if time.monotonic() >= deadline:
            raise HandOffError(HANDOFF_FAILED_TEXT)
        time.sleep(RETRY_INTERVAL_S)
