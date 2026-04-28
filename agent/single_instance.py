"""Single-instance lock using a named Win32 mutex."""

from __future__ import annotations

import sys

# `Local\` mantém o mutex no escopo da sessão do usuário logado (não exige
# privilégio SeCreateGlobalPrivilege, ao contrário de `Global\`). Como o
# agente roda sempre no contexto interativo do usuário, isso basta.
_MUTEX_NAME = "Local\\EldenSysAgent_SingleInstance_v1"


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None
        self.already_running = False
        if sys.platform != "win32":
            return
        try:
            import win32api  # type: ignore
            import win32event  # type: ignore
            import winerror  # type: ignore

            self.handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
            err = win32api.GetLastError()
            if err == winerror.ERROR_ALREADY_EXISTS:
                self.already_running = True
        except Exception:
            # Em último caso, fallback: tenta detectar pelo socket da porta.
            self.already_running = _port_in_use(17777)

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            import win32api  # type: ignore

            win32api.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None


def _port_in_use(port: int) -> bool:
    """Fallback: detecta se a porta do agente já está ocupada."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
