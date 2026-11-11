"""A free TCP port for a test server (issue #77): one copy for every test that needs one."""

from __future__ import annotations

import socket


def free_port() -> int:
    """A port the kernel says is free right now (bind to 0 and read the assignment)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
