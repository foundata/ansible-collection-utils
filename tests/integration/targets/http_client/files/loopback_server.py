# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""A scripted loopback HTTP server for the http_client integration target.

Usage: loopback_server.py <state-directory> [<lifetime-seconds>]

Binds a free loopback port, writes it to <state-directory>/port and its
process ID to <state-directory>/pid, and exits after the lifetime. Each
request path selects a scenario.
"""

from __future__ import annotations

import gzip
import os
import socket
import sys
import threading
import time


def response(
    status: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> bytes:
    lines = [f"HTTP/1.1 {status} Scripted"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    return "\r\n".join(lines).encode("ascii") + b"\r\n\r\n" + body


def handle(conn: socket.socket) -> None:
    try:
        conn.settimeout(5)
        head = b""
        while b"\r\n\r\n" not in head:
            piece = conn.recv(4096)
            if not piece:
                return
            head += piece
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, target = request_line.split(" ")[:2]
        path = target.split("?", 1)[0]
        authorization = ""
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"authorization:"):
                authorization = line.split(b":", 1)[1].strip().decode("latin-1")
        if path == "/ok":
            body = f'{{"method": "{method}", "target": "{target}", "authenticated": {str(bool(authorization)).lower()}}}'
            conn.sendall(
                response(200, body.encode(), {"Content-Type": "application/json"})
            )
        elif path == "/echo":
            # Echoes the credential, as a careless product endpoint might.
            conn.sendall(
                response(
                    401,
                    f'{{"error": "bad token {authorization}"}}'.encode(),
                    {"Content-Type": "application/json"},
                )
            )
        elif path == "/error":
            conn.sendall(
                response(
                    500,
                    b'{"result": "failed", "errorMessage": "scripted failure"}',
                    {"Content-Type": "application/json"},
                )
            )
        elif path == "/empty":
            conn.sendall(response(204))
        elif path == "/oversized":
            conn.sendall(response(200, b"x" * (8 * 1024 * 1024)))
        elif path == "/compressed":
            conn.sendall(
                response(
                    200, gzip.compress(b'{"ok": true}'), {"Content-Encoding": "gzip"}
                )
            )
        elif path == "/redirect":
            conn.sendall(
                response(302, b"moved", {"Location": "http://127.0.0.2:9/login"})
            )
        elif path == "/stall":
            time.sleep(10)
        elif path == "/trickle":
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\nConnection: close\r\n\r\n"
            )
            for _index in range(200):
                conn.sendall(b"t")
                time.sleep(0.1)
        else:
            conn.sendall(response(404, b"not found"))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    state_dir = sys.argv[1]
    lifetime = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.5)
    with open(os.path.join(state_dir, "pid"), "w", encoding="ascii") as handle_pid:
        handle_pid.write(str(os.getpid()))
    with open(
        os.path.join(state_dir, "port.tmp"), "w", encoding="ascii"
    ) as handle_port:
        handle_port.write(str(listener.getsockname()[1]))
    os.rename(os.path.join(state_dir, "port.tmp"), os.path.join(state_dir, "port"))
    end = time.monotonic() + lifetime
    while time.monotonic() < end:
        try:
            conn, _address = listener.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
