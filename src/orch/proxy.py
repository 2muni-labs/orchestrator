"""Allowlist CONNECT proxy for Codex runs (ADR 0018, ADR 0028)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import tomllib
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
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
HOST_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*")
IP_NUMBER_RE = re.compile(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*")


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
    try:
        with (hub / "config" / "pins.toml").open("rb") as stream:
            values = tomllib.load(stream)["codex"]["proxy_hosts"]
        if not isinstance(values, list):
            raise ValueError("proxy_hosts must be a list")
        return _validate_hosts(values)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProxyError("Cannot load proxy hosts") from exc


def _validate_hosts(hosts: Iterable[str]) -> frozenset[str]:
    if isinstance(hosts, (str, bytes)):
        raise ValueError("Hosts must be a collection of names")
    checked: set[str] = set()
    for host in hosts:
        if (
            not isinstance(host, str)
            or normalize_host(host) != host
            or is_ip_literal(host)
            or host in checked
        ):
            raise ValueError("Invalid or duplicate proxy host")
        checked.add(host)
    if not checked:
        raise ValueError("Proxy hosts cannot be empty")
    return frozenset(checked)


def normalize_host(host: str) -> str | None:
    """Lowercase, strip one trailing dot and check the host name syntax."""
    if not host.isascii():
        return None
    value = host.lower().removesuffix(".")
    if len(value) <= 253 and HOST_RE.fullmatch(value) is not None:
        return value
    return None


def is_ip_literal(host: str) -> bool:
    """Return whether the host is an IP address in any form."""
    value = host.lower().removesuffix(".")
    if value.startswith("[") or IP_NUMBER_RE.fullmatch(value) is not None:
        return True
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def decide(method: str, target: str, hosts: frozenset[str]) -> Decision:
    """Decide one request line against the allowlist."""
    if method != "CONNECT":
        return Decision(allowed=False, host="", port=None, reason="method")
    match = TARGET_RE.fullmatch(target)
    if match is None or int(match["port"]) > 65535:
        return Decision(
            allowed=False,
            host=ascii(target)[1:-1][:FIELD_LIMIT],
            port=None,
            reason="target",
        )
    raw_host = match["host"]
    host = ascii(raw_host)[1:-1][:FIELD_LIMIT]
    port = int(match["port"])
    if is_ip_literal(raw_host):
        return Decision(allowed=False, host=host, port=port, reason="ip_literal")
    normalized = normalize_host(raw_host)
    if normalized is None:
        return Decision(allowed=False, host=host, port=port, reason="target")
    if normalized not in hosts:
        reason = "host"
    elif port != ALLOWED_PORT:
        reason = "port"
    else:
        reason = "allowed"
    return Decision(
        allowed=reason == "allowed", host=normalized, port=port, reason=reason
    )


def proxy_env(port: int) -> dict[str, str]:
    """Return the six proxy variables pointing at 127.0.0.1:<port>."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid proxy port")
    return {name: f"http://127.0.0.1:{port}" for name in PROXY_VARS}


def open_listener(
    factory: Callable[..., socket.socket] = socket.socket,
) -> socket.socket:
    """Bind a listening socket on 127.0.0.1 with a free port."""
    listener: socket.socket | None = None
    try:
        listener = factory(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
    except OSError as exc:
        if listener is not None:
            listener.close()
        raise ProxyError("Cannot open proxy listener") from exc
    return listener


def default_connect(host: str, port: int, timeout: float) -> socket.socket:
    """Open the upstream connection."""
    return socket.create_connection((host, port), timeout=timeout)


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
        try:
            self._hosts = _validate_hosts(hosts)
        except (TypeError, ValueError) as exc:
            raise ProxyError("Invalid proxy hosts") from exc
        if max_connections < 1 or connect_timeout <= 0 or head_timeout <= 0:
            raise ValueError("Connection limits and timeouts must be positive")
        self._log_path = log_path
        self._connector = connector
        self._listener_factory = listener_factory
        self._connect_timeout = connect_timeout
        self._head_timeout = head_timeout
        self._max_connections = max_connections
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._start_called = False
        self._failed = False
        self._listener: socket.socket | None = None
        self._port: int | None = None
        self._log_fd: int | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._sockets: set[socket.socket] = set()
        self._active = 0
        self._events: list[ProxyEvent] = []

    def start(self) -> int:
        with self._lock:
            if self._start_called or self._stopped.is_set():
                raise ProxyError("Proxy cannot be started again")
            self._start_called = True
            try:
                self._listener = self._listener_factory()
                self._listener.settimeout(ACCEPT_POLL_SEC)
                self._log_fd = os.open(
                    self._log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
                )
                self._port = self._listener.getsockname()[1]
                self._accept_thread = threading.Thread(
                    target=self._accept, name="orch-proxy-accept", daemon=True
                )
                self._accept_thread.start()
            except Exception as exc:
                self._failed = True
                if self._listener is not None:
                    self._listener.close()
                if self._log_fd is not None:
                    os.close(self._log_fd)
                    self._log_fd = None
                self._port = None
                raise ProxyError("Cannot start proxy") from exc
            return self._port

    @property
    def port(self) -> int:
        with self._lock:
            if self._port is None:
                raise ProxyError("Proxy has not started")
            return self._port

    def alive(self) -> bool:
        with self._lock:
            return (
                self._port is not None
                and not self._stopped.is_set()
                and not self._failed
                and self._accept_thread is not None
                and self._accept_thread.is_alive()
            )

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            self._stopped.set()
            listener = self._listener
            sockets = tuple(self._sockets)
            threads = tuple(self._workers)
            accept_thread = self._accept_thread
        if listener is not None:
            listener.close()
        for sock in sockets:
            self._close(sock)
        for thread in (accept_thread, *threads):
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if self._log_fd is not None:
                os.close(self._log_fd)
                self._log_fd = None

    def events(self) -> tuple[ProxyEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def denials(self) -> tuple[ProxyEvent, ...]:
        with self._lock:
            return tuple(event for event in self._events if not event.allowed)

    def __enter__(self) -> AllowlistProxy:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @staticmethod
    def _close(sock: socket.socket) -> None:
        # shutdown wakes threads blocked in recv/sendall before close releases it.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def _fail(self) -> None:
        with self._lock:
            self._failed = True
            listener = self._listener
            sockets = tuple(self._sockets)
        if listener is not None:
            listener.close()
        for sock in sockets:
            self._close(sock)

    def _record(self, decision: Decision) -> bool:
        with self._lock:
            if self._stopped.is_set() or self._failed:
                return False
            event = ProxyEvent(
                at=datetime.now(UTC).isoformat(),
                allowed=decision.allowed,
                host=decision.host,
                port=decision.port,
                reason=decision.reason,
            )
            self._events.append(event)
            line = (
                json.dumps(
                    {
                        "at": event.at,
                        "host": event.host,
                        "port": event.port,
                        "proxy": "allow" if event.allowed else "deny",
                        "reason": event.reason,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
                + "\n"
            )
            data = line.encode("ascii")
            try:
                if self._log_fd is None:
                    raise OSError("Proxy log is closed")
                if os.write(self._log_fd, data) != len(data):
                    raise OSError("Incomplete proxy log write")
            except OSError:
                # Mark failure under the same lock so no other request passes.
                self._failed = True
            else:
                return True
        self._fail()
        return False

    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stopped.is_set():
            with self._lock:
                if self._failed:
                    return
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except Exception:
                if not self._stopped.is_set():
                    self._fail()
                return
            with self._lock:
                if self._stopped.is_set() or self._failed:
                    self._close(client)
                    return
                if self._active < self._max_connections:
                    self._active += 1
                    self._sockets.add(client)
                    worker = threading.Thread(
                        target=self._serve,
                        args=(client,),
                        name="orch-proxy-connection",
                        daemon=True,
                    )
                    self._workers.add(worker)
                    try:
                        worker.start()
                    except Exception:
                        self._workers.remove(worker)
                        self._sockets.remove(client)
                        self._active -= 1
                        self._close(client)
                        break
                    continue
            try:
                if self._record(
                    Decision(allowed=False, host="", port=None, reason="busy")
                ):
                    with suppress(OSError):
                        client.setblocking(False)
                        client.send(
                            b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                            b"Connection: close\r\n\r\n"
                        )
            finally:
                with suppress(OSError):
                    self._close(client)
        if not self._stopped.is_set():
            self._fail()

    def _read_head(self, client: socket.socket) -> tuple[str, str, bytes]:
        client.settimeout(self._head_timeout)
        deadline = time.monotonic() + self._head_timeout
        data = bytearray()
        while True:
            end = data.find(b"\r\n\r\n")
            if end >= 0:
                end += 4
                if end > HEAD_LIMIT:
                    raise ValueError("Request head is too large")
                break
            if len(data) >= HEAD_LIMIT:
                raise ValueError("Request head is too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Request head timed out")
            client.settimeout(remaining)
            chunk = client.recv(4096)
            if not chunk:
                raise ValueError("Incomplete request head")
            data.extend(chunk)
        parts = data[:end].split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        if (
            len(parts) != 3
            or not all(parts)
            or parts[2] not in ("HTTP/1.0", "HTTP/1.1")
        ):
            raise ValueError("Invalid request line")
        return parts[0], parts[1], bytes(data[end:])

    @staticmethod
    def _respond(client: socket.socket, status: str) -> None:
        client.sendall(
            f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
        )

    def _deny(self, client: socket.socket, decision: Decision, status: str) -> None:
        if self._record(decision):
            self._respond(client, status)

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            try:
                method, target, early = self._read_head(client)
            except OSError, ValueError:
                self._deny(
                    client,
                    Decision(allowed=False, host="", port=None, reason="malformed"),
                    "400 Bad Request",
                )
                return
            decision = decide(method, target, self._hosts)
            if not decision.allowed:
                self._deny(client, decision, "403 Forbidden")
                return
            try:
                upstream = self._connector(
                    decision.host, ALLOWED_PORT, self._connect_timeout
                )
            except OSError:
                self._deny(
                    client,
                    Decision(
                        allowed=False,
                        host=decision.host,
                        port=ALLOWED_PORT,
                        reason="upstream",
                    ),
                    "502 Bad Gateway",
                )
                return
            with self._lock:
                if self._stopped.is_set() or self._failed:
                    return
                self._sockets.add(upstream)
            if not self._record(decision):
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if early:
                upstream.sendall(early)
            client.settimeout(None)
            upstream.settimeout(None)
            self._relay(client, upstream)
        except OSError:
            pass
        except Exception:
            self._fail()
        finally:
            self._close(client)
            if upstream is not None:
                self._close(upstream)
            with self._lock:
                self._sockets.discard(client)
                if upstream is not None:
                    self._sockets.discard(upstream)
                self._active -= 1
                self._workers.discard(threading.current_thread())

    def _copy(self, source: socket.socket, destination: socket.socket) -> None:
        try:
            while not self._stopped.is_set():
                data = source.recv(65536)
                if not data:
                    destination.shutdown(socket.SHUT_WR)
                    return
                destination.sendall(data)
        except OSError:
            self._close(source)
            self._close(destination)

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        reverse = threading.Thread(
            target=self._copy,
            args=(upstream, client),
            name="orch-proxy-return",
            daemon=True,
        )
        with self._lock:
            if self._stopped.is_set() or self._failed:
                return
            reverse.start()
        try:
            self._copy(client, upstream)
        finally:
            reverse.join()
