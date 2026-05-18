"""Shared socket protocol bits for the repl server/client.

We use a length-prefixed line protocol over an AF_UNIX socket (or AF_INET on
Windows). One newline-delimited JSON request → one newline-delimited JSON
reply. No streaming.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any


def default_socket_path() -> Path:
    home = Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browser-skill")))
    return home / "repl.sock"


def pid_path() -> Path:
    home = Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browser-skill")))
    return home / "repl.pid"


def recv_line(sock: socket.socket, *, bufsize: int = 65536) -> bytes:
    chunks: list[bytes] = []
    while True:
        b = sock.recv(bufsize)
        if not b:
            break
        chunks.append(b)
        if b.endswith(b"\n"):
            break
    return b"".join(chunks)


def send_json(sock: socket.socket, obj: Any) -> None:
    data = (json.dumps(obj) + "\n").encode("utf-8")
    sock.sendall(data)


def recv_json(sock: socket.socket) -> dict | None:
    raw = recv_line(sock)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
