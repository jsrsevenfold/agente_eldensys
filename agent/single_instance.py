"""Single-instance lock using a named Win32 mutex."""

from __future__ import annotations

import sys

_MUTEX_NAME = "Global\\EldenSysAgent_SingleInstance_v1"


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None
        self.already_running = False
        if sys.platform != "win32":
            return
        try:
            import win32event  # type: ignore
            import winerror  # type: ignore

            self.handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
            last_error = win32event.GetLastError() if hasattr(win32event, "GetLastError") else 0
            # CreateMutex itself doesn't expose last_error directly via win32event;
            # the safer path is to inspect via win32api.
            import win32api  # type: ignore

            err = win32api.GetLastError()
            if err == winerror.ERROR_ALREADY_EXISTS:
                self.already_running = True
        except Exception:
            self.already_running = False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            import win32api  # type: ignore

            win32api.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None
