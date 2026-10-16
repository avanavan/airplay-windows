"""One running copy per user session. Starting it again brings the running window forward.

The mutex name is also what the installer checks (AppMutex) to ask the user
to close the app before upgrading or uninstalling.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

MUTEX_NAME = "airplay-windows"
SHOW_EVENT_NAME = "airplay-windows-show"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = k.CreateEventW.restype = k.OpenEventW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.SetEvent.argtypes = k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return k


class SingleInstance:
    def __init__(self):
        self._k = _kernel32()
        self._mutex = self._k.CreateMutexW(None, False, MUTEX_NAME)
        self.first = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        self._event = None
        self._stop = threading.Event()

    def signal_running_instance(self) -> bool:
        """Ask the running copy to show its window."""
        event = self._k.OpenEventW(0x0002, False, SHOW_EVENT_NAME)   # EVENT_MODIFY_STATE
        if not event:
            return False
        self._k.SetEvent(event)
        self._k.CloseHandle(event)
        return True

    def listen(self, on_show) -> None:
        """Call `on_show()` (from a background thread) whenever another copy is started."""
        self._event = self._k.CreateEventW(None, False, False, SHOW_EVENT_NAME)

        def wait():
            while not self._stop.is_set():
                if self._k.WaitForSingleObject(self._event, 500) == WAIT_OBJECT_0 and not self._stop.is_set():
                    on_show()

        threading.Thread(target=wait, name="single-instance", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        for handle in (self._event, self._mutex):
            if handle:
                self._k.CloseHandle(handle)
