# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""A scripted loopback HTTP server for transport tests.

Each test supplies a handler that receives the accepted connection and the
raw request head and writes whatever bytes the scenario needs: a complete
response, a trickled one, or nothing at all.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping

Handler = Callable[[socket.socket, bytes], None]


def response_bytes(
    status: int,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    *,
    content_length: bool = True,
) -> bytes:
    """Build a complete HTTP/1.1 response that closes the connection."""
    lines = [f"HTTP/1.1 {status} Scripted"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    if content_length:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    head = "\r\n".join(lines).encode("ascii") + b"\r\n\r\n"
    return head + body


class LoopbackServer:
    """Accept loopback connections and hand each one to the handler."""

    def __init__(
        self, handler: Handler, *, tls_context: ssl.SSLContext | None = None
    ) -> None:
        self._handler = handler
        self._tls_context = tls_context
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.requests: list[bytes] = []
        self.client_closed: list[bool] = []
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    @property
    def url(self) -> str:
        scheme = "https" if self._tls_context else "http"
        return f"{scheme}://127.0.0.1:{self.port}"

    def wait_closed(self, timeout: float = 5.0) -> bool:
        """Return whether the client closed the first handled connection."""
        end = time.monotonic() + timeout
        while not self.client_closed and time.monotonic() < end:
            time.sleep(0.01)
        return bool(self.client_closed and self.client_closed[0])

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()

    def __enter__(self) -> LoopbackServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            if self._tls_context is not None:
                conn = self._tls_context.wrap_socket(conn, server_side=True)
            conn.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                piece = conn.recv(4096)
                if not piece:
                    return
                head += piece
            self.requests.append(head)
            try:
                self._handler(conn, head)
            except OSError:
                # The peer closed while the handler was still writing.
                self.client_closed.append(True)
                return
            # Did the client close its side? EOF arrives once it did.
            conn.settimeout(3)
            try:
                self.client_closed.append(conn.recv(1) == b"")
            except OSError:
                self.client_closed.append(False)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def send_all(conn: socket.socket, data: bytes) -> None:
    conn.sendall(data)


def trickle(conn: socket.socket, data: bytes, interval: float) -> None:
    """Send ``data`` one byte at a time, pausing ``interval`` seconds between bytes."""
    for index in range(len(data)):
        conn.sendall(data[index : index + 1])
        time.sleep(interval)


def hold(conn: socket.socket, seconds: float) -> None:
    """Keep the connection open without sending anything."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
