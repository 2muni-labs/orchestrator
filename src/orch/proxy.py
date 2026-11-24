"""Allowlist CONNECT proxy for Codex runs (ADR 0018, ADR 0028)."""

from __future__ import annotations

import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

PROXY_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)
ALLOWED_PORT = 443
HEAD_LIMIT = 8192
FIELD_LIMIT = 255
ACCEPT_POLL_SEC = 0.2
TARGET_RE = re.compile(r"(?P<host>\[[^\]]*\]|[^\s:\[\]]+):(?P<port>[0-9]{1,5})")


class ProxyError(Exception):
    """The proxy cannot load its hosts, start, or keep running."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    allowed: bool
    host: str
    port: int | None
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyEvent:
    at: str
    allowed: bool
    host: str
    port: int | None
    reason: str


def load_proxy_hosts(hub: Path) -> frozenset[str]:
    """Read and check [codex] proxy_hosts from the hub's config/pins.toml."""
    raise NotImplementedError


def normalize_host(host: str) -> str | None:
    """Lowercase, strip one trailing dot and check the host name syntax."""
    raise NotImplementedError


def is_ip_literal(host: str) -> bool:
    """Return whether the host is an IP address in any form."""
    raise NotImplementedError


def decide(method: str, target: str, hosts: frozenset[str]) -> Decision:
    """Decide one request line against the allowlist."""
    raise NotImplementedError


def proxy_env(port: int) -> dict[str, str]:
    """Return the six proxy variables pointing at 127.0.0.1:<port>."""
    raise NotImplementedError


def open_listener(
    factory: Callable[..., socket.socket] = socket.socket,
) -> socket.socket:
    """Bind a listening socket on 127.0.0.1 with a free port."""
    raise NotImplementedError


def default_connect(host: str, port: int, timeout: float) -> socket.socket:
    """Open the upstream connection."""
    raise NotImplementedError


class AllowlistProxy:
    """A CONNECT proxy that passes only allowlisted hosts on port 443."""

    def __init__(
        self,
        hosts: Iterable[str],
        log_path: Path,
        *,
        connector: Callable[[str, int, float], socket.socket] = default_connect,
        listener_factory: Callable[[], socket.socket] = open_listener,
        connect_timeout: float = 10.0,
        head_timeout: float = 10.0,
        max_connections: int = 64,
    ) -> None:
        raise NotImplementedError

    def start(self) -> int:
        raise NotImplementedError

    @property
    def port(self) -> int:
        raise NotImplementedError

    def alive(self) -> bool:
        raise NotImplementedError

    def stop(self, timeout: float = 5.0) -> None:
        raise NotImplementedError

    def events(self) -> tuple[ProxyEvent, ...]:
        raise NotImplementedError

    def denials(self) -> tuple[ProxyEvent, ...]:
        raise NotImplementedError

    def __enter__(self) -> AllowlistProxy:
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:
        raise NotImplementedError
