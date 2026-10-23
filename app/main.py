"""Entry point for Imagine Studio."""

from __future__ import annotations

import sys

from app.config import HOST, OUTPUT_DIR, PORT, REPO_ROOT
from app.ui.studio import mount


def _quiet_windows_connection_reset() -> None:
    """Close a dead Windows socket without a callback traceback.

    ProactorEventLoop calls shutdown() after the peer has already reset
    the connection. That raises WinError 10054 inside an event-loop
    callback. The socket is already gone; close it and continue.
    """
    if sys.platform != "win32":
        return
    import socket

    import asyncio.proactor_events as pe

    base = pe._ProactorBasePipeTransport
    if getattr(base, "_imagine_reset_guard", False):
        return

    def _call_connection_lost(self, exc):
        if self._called_connection_lost:
            return
        try:
            self._protocol.connection_lost(exc)
        finally:
            sock = self._sock
            if sock is not None:
                try:
                    if hasattr(sock, "shutdown") and sock.fileno() != -1:
                        sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
                self._sock = None
            server = self._server
            if server is not None:
                server._detach(self)
                self._server = None
            self._called_connection_lost = True

    base._call_connection_lost = _call_connection_lost
    base._imagine_reset_guard = True


def main() -> None:
    _quiet_windows_connection_reset()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure repo root is importable when run as script
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from nicegui import ui

    mount()
    ui.run(
        title="Imagine Studio",
        host=HOST,
        port=PORT,
        reload=False,
        # Don't open a browser tab on every restart; start.sh reuses one.
        show=False,
        favicon="🎮",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
