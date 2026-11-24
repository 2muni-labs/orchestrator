"""Tests for the allowlist CONNECT proxy (m-codex-proxy, ADR 0018, ADR 0028).

The gate sandbox denies TCP and AF_UNIX binds and connects, so every test injects
the listener and the upstream connector and uses socket.socketpair() only.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from orch import proxy
from orch.proxy import (
    ACCEPT_POLL_SEC,
    ALLOWED_PORT,
    FIELD_LIMIT,
    HEAD_LIMIT,
    PROXY_VARS,
    AllowlistProxy,
    Decision,
    ProxyError,
    decide,
    is_ip_literal,
    load_proxy_hosts,
    normalize_host,
    open_listener,
    proxy_env,
)

REPO = Path(__file__).resolve().parents[2]
HOSTS = frozenset({"chatgpt.com", "ab.chatgpt.com"})
FAKE_PORT = 41000
IO_TIMEOUT = 3.0


# Fakes


class FakeListener:
    """A listening socket whose accept() hands out queued socketpair ends."""

    def __init__(self) -> None:
        self.pending: queue.Queue[socket.socket | BaseException] = queue.Queue()
        self.timeouts: list[float | None] = []
        self.closed = threading.Event()

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", FAKE_PORT)

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        if self.closed.is_set():
            raise OSError("listener closed")
        wait = self.timeouts[-1] if self.timeouts and self.timeouts[-1] else 0.05
        try:
            item = self.pending.get(timeout=wait)
        except queue.Empty:
            raise TimeoutError("timed out") from None
        if isinstance(item, BaseException):
            raise item
        return item, ("127.0.0.1", 50000)

    def close(self) -> None:
        self.closed.set()

    def push(self, item: socket.socket | BaseException) -> None:
        self.pending.put(item)


class FakeUpstream:
    """A connector that returns one end of a socketpair per call."""

    def __init__(self, error: OSError | None = None) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.servers: list[socket.socket] = []
        self.error = error
        self.connected = threading.Event()

    def __call__(self, host: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((host, port, timeout))
        if self.error is not None:
            raise self.error
        near, far = socket.socketpair()
        far.settimeout(IO_TIMEOUT)
        self.servers.append(far)
        self.connected.set()
        return near


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.listener = FakeListener()
        self.upstream = FakeUpstream()
        self.log_path = tmp_path / "proxy.log"
        self.proxies: list[AllowlistProxy] = []
        self.clients: list[socket.socket] = []

    def make(self, **kwargs: object) -> AllowlistProxy:
        kwargs.setdefault("connector", self.upstream)
        kwargs.setdefault("listener_factory", lambda: self.listener)
        hosts = kwargs.pop("hosts", sorted(HOSTS))
        log_path = kwargs.pop("log_path", self.log_path)
        p = AllowlistProxy(hosts, log_path, **kwargs)  # type: ignore[arg-type]
        self.proxies.append(p)
        return p

    def started(self, **kwargs: object) -> AllowlistProxy:
        p = self.make(**kwargs)
        p.start()
        return p

    def connect(self) -> socket.socket:
        client, server_side = socket.socketpair()
        client.settimeout(IO_TIMEOUT)
        self.clients.append(client)
        self.listener.push(server_side)
        return client

    def close(self) -> None:
        for p in self.proxies:
            try:
                p.stop(timeout=2.0)
            except Exception:
                pass
        for s in self.clients + self.upstream.servers:
            s.close()

    def log_lines(self) -> list[dict[str, object]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def wait_until(cond: Callable[[], bool], timeout: float = IO_TIMEOUT) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def read_response(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def read_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def read_to_eof(sock: socket.socket) -> bytes:
    data = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except ConnectionResetError:
            return data
        if not chunk:
            return data
        data += chunk


def connect_request(target: str, extra: bytes = b"") -> bytes:
    return (
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("latin-1") + extra
    )


ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def deny_response(code: str) -> bytes:
    return f"HTTP/1.1 {code}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()


# Host rules


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("chatgpt.com", "chatgpt.com"),
        ("ChatGPT.COM", "chatgpt.com"),
        ("chatgpt.com.", "chatgpt.com"),
        ("AB.ChatGPT.com.", "ab.chatgpt.com"),
        ("chatgpt.com..", None),
        ("", None),
        (".", None),
        ("chat gpt.com", None),
        ("chatgpt.com:443", None),
        ("chatgpt..com", None),
        ("chatgpt_com", None),
        ("chatgpt.cоm", None),
        ("chatgpt.com/", None),
        ("a" * 254, None),
    ],
)
def test_normalize_host(host: str, expected: str | None) -> None:
    assert normalize_host(host) == expected


def test_normalize_host_keeps_a_253_character_name() -> None:
    name = ".".join(["a" * 63] * 3 + ["a" * 61])
    assert len(name) == 253
    assert normalize_host(name) == name


@pytest.mark.parametrize(
    "host",
    [
        "1.2.3.4",
        "1.2.3.4.",
        "127.1",
        "0x7f.0.0.1",
        "0X7F.0.0.1",
        "2130706433",
        "::1",
        "[::1]",
        "[2001:db8::1]",
        "::ffff:127.0.0.1",
    ],
)
def test_ip_literals_are_recognized(host: str) -> None:
    assert is_ip_literal(host) is True


@pytest.mark.parametrize("host", ["chatgpt.com", "1x.com", "a1.b2", "0xg.com", "x1"])
def test_names_are_not_ip_literals(host: str) -> None:
    assert is_ip_literal(host) is False


# decide


@pytest.mark.parametrize(
    ("method", "target", "expected"),
    [
        (
            "CONNECT",
            "chatgpt.com:443",
            Decision(allowed=True, host="chatgpt.com", port=443, reason="allowed"),
        ),
        (
            "CONNECT",
            "ab.chatgpt.com:443",
            Decision(allowed=True, host="ab.chatgpt.com", port=443, reason="allowed"),
        ),
        (
            "CONNECT",
            "ChatGPT.COM:443",
            Decision(allowed=True, host="chatgpt.com", port=443, reason="allowed"),
        ),
        (
            "CONNECT",
            "chatgpt.com.:443",
            Decision(allowed=True, host="chatgpt.com", port=443, reason="allowed"),
        ),
        (
            "GET",
            "chatgpt.com:443",
            Decision(allowed=False, host="", port=None, reason="method"),
        ),
        (
            "connect",
            "chatgpt.com:443",
            Decision(allowed=False, host="", port=None, reason="method"),
        ),
        (
            "CONNECT",
            "evilchatgpt.com:443",
            Decision(allowed=False, host="evilchatgpt.com", port=443, reason="host"),
        ),
        (
            "CONNECT",
            "chatgpt.com.evil.net:443",
            Decision(
                allowed=False, host="chatgpt.com.evil.net", port=443, reason="host"
            ),
        ),
        (
            "CONNECT",
            "x.chatgpt.com:443",
            Decision(allowed=False, host="x.chatgpt.com", port=443, reason="host"),
        ),
        (
            "CONNECT",
            "chatgpt.co:443",
            Decision(allowed=False, host="chatgpt.co", port=443, reason="host"),
        ),
        (
            "CONNECT",
            "chatgpt.com:80",
            Decision(allowed=False, host="chatgpt.com", port=80, reason="port"),
        ),
        (
            "CONNECT",
            "chatgpt.com:4430",
            Decision(allowed=False, host="chatgpt.com", port=4430, reason="port"),
        ),
        (
            "CONNECT",
            "evil.net:80",
            Decision(allowed=False, host="evil.net", port=80, reason="host"),
        ),
        (
            "CONNECT",
            "chatgpt.com",
            Decision(allowed=False, host="chatgpt.com", port=None, reason="target"),
        ),
        (
            "CONNECT",
            "chatgpt.com:",
            Decision(allowed=False, host="chatgpt.com:", port=None, reason="target"),
        ),
        (
            "CONNECT",
            "chatgpt.com:99999",
            Decision(
                allowed=False, host="chatgpt.com:99999", port=None, reason="target"
            ),
        ),
        (
            "CONNECT",
            "chatgpt.com:443:1",
            Decision(
                allowed=False, host="chatgpt.com:443:1", port=None, reason="target"
            ),
        ),
        (
            "CONNECT",
            "http://chatgpt.com/",
            Decision(
                allowed=False, host="http://chatgpt.com/", port=None, reason="target"
            ),
        ),
        (
            "CONNECT",
            "1.2.3.4:443",
            Decision(allowed=False, host="1.2.3.4", port=443, reason="ip_literal"),
        ),
        (
            "CONNECT",
            "127.1:443",
            Decision(allowed=False, host="127.1", port=443, reason="ip_literal"),
        ),
        (
            "CONNECT",
            "[::1]:443",
            Decision(allowed=False, host="[::1]", port=443, reason="ip_literal"),
        ),
        (
            "CONNECT",
            "[chatgpt.com]:443",
            Decision(
                allowed=False, host="[chatgpt.com]", port=443, reason="ip_literal"
            ),
        ),
        (
            "CONNECT",
            "chatgpt.com..:443",
            Decision(allowed=False, host="chatgpt.com..", port=443, reason="target"),
        ),
        (
            "CONNECT",
            "chät.com:443",
            Decision(allowed=False, host="ch\\xe4t.com", port=443, reason="target"),
        ),
    ],
)
def test_decide(method: str, target: str, expected: Decision) -> None:
    assert decide(method, target, HOSTS) == expected


def test_decide_cuts_a_long_raw_host() -> None:
    decision = decide("CONNECT", "a" * 300 + ".com:443", HOSTS)
    assert decision.allowed is False
    assert decision.reason == "target"
    assert decision.host == ("a" * 300)[:FIELD_LIMIT]
    assert len(decision.host) == FIELD_LIMIT


def test_decide_uses_only_the_given_hosts() -> None:
    assert decide("CONNECT", "example.org:443", frozenset({"example.org"})).allowed
    assert not decide("CONNECT", "chatgpt.com:443", frozenset({"example.org"})).allowed


# load_proxy_hosts


def test_load_proxy_hosts_reads_the_hub_pins() -> None:
    assert load_proxy_hosts(REPO) == HOSTS


def write_pins(tmp_path: Path, body: str) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pins.toml").write_text(body)
    return tmp_path


def test_load_proxy_hosts_reads_a_custom_list(tmp_path: Path) -> None:
    hub = write_pins(tmp_path, '[codex]\nproxy_hosts = ["example.org"]\n')
    assert load_proxy_hosts(hub) == frozenset({"example.org"})


@pytest.mark.parametrize(
    "body",
    [
        "[claude]\nversion = '1'\n",
        "[codex]\nversion = '1'\n",
        "[codex]\nproxy_hosts = []\n",
        "[codex]\nproxy_hosts = 'chatgpt.com'\n",
        "[codex]\nproxy_hosts = [1]\n",
        "[codex]\nproxy_hosts = ['ChatGPT.com']\n",
        "[codex]\nproxy_hosts = ['chatgpt.com.']\n",
        "[codex]\nproxy_hosts = ['chatgpt.com:443']\n",
        "[codex]\nproxy_hosts = ['1.2.3.4']\n",
        "[codex]\nproxy_hosts = ['chatgpt.com', 'chatgpt.com']\n",
        "[codex]\nproxy_hosts = ['']\n",
        "[codex\nproxy_hosts = ['chatgpt.com']\n",
    ],
)
def test_load_proxy_hosts_refuses_a_bad_list(tmp_path: Path, body: str) -> None:
    hub = write_pins(tmp_path, body)
    with pytest.raises(ProxyError):
        load_proxy_hosts(hub)


def test_load_proxy_hosts_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProxyError):
        load_proxy_hosts(tmp_path)


# proxy_env


def test_proxy_env_sets_all_six_variables() -> None:
    env = proxy_env(41000)
    assert PROXY_VARS == (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    )
    assert env == {name: "http://127.0.0.1:41000" for name in PROXY_VARS}


def test_proxy_env_returns_a_new_dict() -> None:
    first = proxy_env(1)
    first["HTTPS_PROXY"] = "changed"
    assert proxy_env(1)["HTTPS_PROXY"] == "http://127.0.0.1:1"


@pytest.mark.parametrize("port", [0, 65536, -1, True, "443", 443.0])
def test_proxy_env_refuses_a_bad_port(port: object) -> None:
    with pytest.raises(ValueError):
        proxy_env(port)  # type: ignore[arg-type]


# open_listener


class RecordingSocket:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_on = fail_on
        self.closed = False

    def bind(self, address: tuple[str, int]) -> None:
        self.calls.append(("bind", (address,)))
        if self.fail_on == "bind":
            raise OSError("bind failed")

    def listen(self, backlog: int) -> None:
        self.calls.append(("listen", (backlog,)))
        if self.fail_on == "listen":
            raise OSError("listen failed")

    def close(self) -> None:
        self.closed = True


def test_open_listener_binds_loopback_with_a_free_port() -> None:
    made: list[tuple[object, ...]] = []
    sock = RecordingSocket()

    def factory(*args: object) -> RecordingSocket:
        made.append(args)
        return sock

    assert open_listener(factory) is sock  # type: ignore[arg-type]
    assert made == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert sock.calls == [("bind", (("127.0.0.1", 0),)), ("listen", (64,))]
    assert sock.closed is False


@pytest.mark.parametrize("step", ["bind", "listen"])
def test_open_listener_closes_the_socket_on_failure(step: str) -> None:
    sock = RecordingSocket(fail_on=step)
    with pytest.raises(ProxyError):
        open_listener(lambda *args: sock)  # type: ignore[arg-type,return-value]
    assert sock.closed is True


def test_open_listener_reports_a_factory_failure() -> None:
    def factory(*args: object) -> socket.socket:
        raise OSError("no sockets")

    with pytest.raises(ProxyError):
        open_listener(factory)


def test_default_connect_uses_create_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    sentinel = object()

    def fake(address: tuple[str, int], timeout: float) -> object:
        calls.append((address, timeout))
        return sentinel

    monkeypatch.setattr(proxy.socket, "create_connection", fake)
    assert proxy.default_connect("chatgpt.com", 443, 7.5) is sentinel
    assert calls == [(("chatgpt.com", 443), 7.5)]


# Construction


@pytest.mark.parametrize(
    "hosts", [[], ["ChatGPT.com"], ["1.2.3.4"], ["chatgpt.com."], ["a b"], ["x", "x"]]
)
def test_the_proxy_refuses_bad_hosts(h: Harness, hosts: list[str]) -> None:
    with pytest.raises(ProxyError):
        h.make(hosts=hosts)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_connections": 0},
        {"connect_timeout": 0},
        {"head_timeout": 0},
        {"connect_timeout": -1.0},
    ],
)
def test_the_proxy_refuses_bad_limits(h: Harness, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        h.make(**kwargs)


# Start and stop


def test_start_returns_the_listener_port_and_opens_a_private_log(h: Harness) -> None:
    p = h.make()
    assert p.start() == FAKE_PORT
    assert p.port == FAKE_PORT
    assert p.alive() is True
    assert h.listener.timeouts[0] == ACCEPT_POLL_SEC
    assert stat.S_IMODE(h.log_path.stat().st_mode) == 0o600
    assert p.events() == ()


def test_the_port_needs_a_started_proxy(h: Harness) -> None:
    p = h.make()
    with pytest.raises(ProxyError):
        _ = p.port
    assert p.alive() is False


def test_start_twice_and_start_after_stop_are_refused(h: Harness) -> None:
    p = h.started()
    with pytest.raises(ProxyError):
        p.start()
    p.stop()
    with pytest.raises(ProxyError):
        p.start()


def test_a_listener_failure_is_a_proxy_error(h: Harness) -> None:
    def factory() -> socket.socket:
        raise OSError("cannot bind")

    p = h.make(listener_factory=factory)
    with pytest.raises(ProxyError):
        p.start()
    assert p.alive() is False


def test_a_proxy_error_from_the_listener_propagates(h: Harness) -> None:
    def factory() -> socket.socket:
        raise ProxyError("cannot bind")

    p = h.make(listener_factory=factory)
    with pytest.raises(ProxyError):
        p.start()
    assert p.alive() is False


def test_a_log_that_cannot_open_closes_the_listener(h: Harness) -> None:
    p = h.make(log_path=h.tmp_path / "missing" / "proxy.log")
    with pytest.raises(ProxyError):
        p.start()
    assert h.listener.closed.is_set()
    assert p.alive() is False


def test_stop_closes_the_listener_and_ends_alive(h: Harness) -> None:
    p = h.started()
    began = time.monotonic()
    p.stop()
    assert time.monotonic() - began < 2.0
    assert h.listener.closed.is_set()
    assert p.alive() is False
    p.stop()


def test_stop_before_start_is_allowed(h: Harness) -> None:
    p = h.make()
    p.stop()
    assert p.alive() is False


def test_the_context_manager_starts_and_stops(h: Harness) -> None:
    p = h.make()
    with p as entered:
        assert entered is p
        assert p.alive() is True
    assert p.alive() is False
    assert h.listener.closed.is_set()


def test_an_accept_failure_ends_alive_and_closes_the_listener(h: Harness) -> None:
    p = h.started()
    h.listener.push(OSError("accept broke"))
    assert wait_until(lambda: not p.alive())
    assert h.listener.closed.is_set()


def test_the_accept_loop_survives_timeouts(h: Harness) -> None:
    p = h.started()
    time.sleep(ACCEPT_POLL_SEC * 3)
    assert p.alive() is True
    client = h.connect()
    client.sendall(connect_request("chatgpt.com:443"))
    assert read_response(client) == ESTABLISHED


# Allowed requests


def test_an_allowed_connect_relays_both_ways_without_headers(h: Harness) -> None:
    p = h.started(connect_timeout=7.5)
    client = h.connect()
    client.sendall(
        b"CONNECT chatgpt.com:443 HTTP/1.1\r\n"
        b"Host: chatgpt.com:443\r\n"
        b"Proxy-Authorization: Basic c2VjcmV0dG9rZW4=\r\n"
        b"\r\nEARLY"
    )
    assert read_response(client) == ESTABLISHED
    assert h.upstream.calls == [("chatgpt.com", ALLOWED_PORT, 7.5)]
    upstream = h.upstream.servers[0]
    assert read_exact(upstream, 5) == b"EARLY"
    client.sendall(b"ping")
    assert read_exact(upstream, 4) == b"ping"
    upstream.sendall(b"pong")
    assert read_exact(client, 4) == b"pong"

    events = p.events()
    assert len(events) == 1
    event = events[0]
    assert (event.allowed, event.host, event.port, event.reason) == (
        True,
        "chatgpt.com",
        443,
        "allowed",
    )
    assert p.denials() == ()

    text = h.log_path.read_text()
    assert "c2VjcmV0dG9rZW4" not in text
    assert "Authorization" not in text
    lines = h.log_lines()
    assert lines == [
        {
            "at": event.at,
            "host": "chatgpt.com",
            "port": 443,
            "proxy": "allow",
            "reason": "allowed",
        }
    ]
    assert text.endswith("\n")
    assert list(json.loads(text.splitlines()[0])) == [
        "at",
        "host",
        "port",
        "proxy",
        "reason",
    ]


def test_the_connector_gets_the_normalized_host(h: Harness) -> None:
    h.started()
    client = h.connect()
    client.sendall(connect_request("AB.ChatGPT.com.:443"))
    assert read_response(client) == ESTABLISHED
    assert h.upstream.calls[0][:2] == ("ab.chatgpt.com", 443)


def test_a_large_transfer_arrives_whole(h: Harness) -> None:
    h.started()
    client = h.connect()
    client.sendall(connect_request("chatgpt.com:443"))
    assert read_response(client) == ESTABLISHED
    upstream = h.upstream.servers[0]
    payload = os.urandom(256 * 1024)
    sender = threading.Thread(target=upstream.sendall, args=(payload,))
    sender.start()
    assert read_exact(client, len(payload)) == payload
    sender.join(IO_TIMEOUT)


def test_an_upstream_eof_half_closes_the_client(h: Harness) -> None:
    h.started()
    client = h.connect()
    client.sendall(connect_request("chatgpt.com:443"))
    assert read_response(client) == ESTABLISHED
    upstream = h.upstream.servers[0]
    upstream.sendall(b"tail")
    upstream.shutdown(socket.SHUT_WR)
    assert read_to_eof(client) == b"tail"
    client.sendall(b"more")
    assert read_exact(upstream, 4) == b"more"


def test_a_client_eof_half_closes_the_upstream(h: Harness) -> None:
    h.started()
    client = h.connect()
    client.sendall(connect_request("chatgpt.com:443", b"req"))
    assert read_response(client) == ESTABLISHED
    client.shutdown(socket.SHUT_WR)
    upstream = h.upstream.servers[0]
    assert read_to_eof(upstream) == b"req"
    upstream.sendall(b"answer")
    assert read_exact(client, 6) == b"answer"


def test_stop_closes_open_relays(h: Harness) -> None:
    p = h.started()
    client = h.connect()
    client.sendall(connect_request("chatgpt.com:443"))
    assert read_response(client) == ESTABLISHED
    p.stop()
    assert read_to_eof(client) == b""
    assert read_to_eof(h.upstream.servers[0]) == b""


def test_requests_are_logged_one_event_each_in_order(h: Harness) -> None:
    p = h.started()
    for target in ("chatgpt.com:443", "evil.net:443", "ab.chatgpt.com:443"):
        client = h.connect()
        client.sendall(connect_request(target))
        read_response(client)
        assert wait_until(lambda: len(h.log_lines()) == len(p.events()))
    reasons = [(e.host, e.reason) for e in p.events()]
    assert reasons == [
        ("chatgpt.com", "allowed"),
        ("evil.net", "host"),
        ("ab.chatgpt.com", "allowed"),
    ]
    assert [(e.host, e.reason) for e in p.denials()] == [("evil.net", "host")]
    assert [line["proxy"] for line in h.log_lines()] == ["allow", "deny", "allow"]


# Denied requests


@pytest.mark.parametrize(
    ("request_bytes", "host", "port", "reason"),
    [
        (connect_request("evilchatgpt.com:443"), "evilchatgpt.com", 443, "host"),
        (
            connect_request("chatgpt.com.evil.net:443"),
            "chatgpt.com.evil.net",
            443,
            "host",
        ),
        (connect_request("chatgpt.com:80"), "chatgpt.com", 80, "port"),
        (connect_request("1.1.1.1:443"), "1.1.1.1", 443, "ip_literal"),
        (connect_request("[::1]:443"), "[::1]", 443, "ip_literal"),
        (connect_request("2130706433:443"), "2130706433", 443, "ip_literal"),
        (
            b"GET http://chatgpt.com/ HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n",
            "",
            None,
            "method",
        ),
    ],
)
def test_a_denied_request_gets_403_and_no_upstream(
    h: Harness, request_bytes: bytes, host: str, port: int | None, reason: str
) -> None:
    p = h.started()
    client = h.connect()
    client.sendall(request_bytes)
    assert read_to_eof(client) == deny_response("403 Forbidden")
    assert h.upstream.calls == []
    assert wait_until(lambda: len(p.events()) == 1)
    event = p.denials()[0]
    assert (event.allowed, event.host, event.port, event.reason) == (
        False,
        host,
        port,
        reason,
    )
    assert h.log_lines() == [
        {"at": event.at, "host": host, "port": port, "proxy": "deny", "reason": reason}
    ]
    assert p.alive() is True


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"CONNECT chatgpt.com:443\r\n\r\n",
        b"CONNECT chatgpt.com:443 HTTP/2.0\r\n\r\n",
        b"CONNECT  chatgpt.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT chatgpt.com:443 HTTP/1.1 extra\r\n\r\n",
        b"\r\n\r\n",
    ],
)
def test_a_malformed_request_gets_400(h: Harness, request_bytes: bytes) -> None:
    p = h.started()
    client = h.connect()
    client.sendall(request_bytes)
    assert read_to_eof(client) == deny_response("400 Bad Request")
    assert h.upstream.calls == []
    assert wait_until(lambda: len(p.events()) == 1)
    event = p.denials()[0]
    assert (event.host, event.port, event.reason) == ("", None, "malformed")


def test_an_oversized_head_gets_400(h: Harness) -> None:
    p = h.started()
    client = h.connect()
    head = b"CONNECT chatgpt.com:443 HTTP/1.1\r\nX: " + b"a" * HEAD_LIMIT
    try:
        client.sendall(head)
    except OSError:
        pass
    assert read_to_eof(client).startswith(b"HTTP/1.1 400 Bad Request\r\n")
    assert h.upstream.calls == []
    assert wait_until(lambda: len(p.events()) == 1)
    assert p.denials()[0].reason == "malformed"


def test_a_head_exactly_at_the_limit_is_read(h: Harness) -> None:
    p = h.started()
    client = h.connect()
    start = b"CONNECT chatgpt.com:443 HTTP/1.1\r\nX: "
    end = b"\r\n\r\n"
    client.sendall(start + b"a" * (HEAD_LIMIT - len(start) - len(end)) + end)
    assert read_response(client) == ESTABLISHED
    assert p.events()[0].reason == "allowed"


def test_a_client_that_closes_before_the_head_is_recorded(h: Harness) -> None:
    p = h.started()
    client = h.connect()
    client.sendall(b"CONNECT chat")
    client.shutdown(socket.SHUT_WR)
    assert wait_until(lambda: len(p.events()) == 1)
    assert p.denials()[0].reason == "malformed"
    assert h.upstream.calls == []


def test_a_slow_head_times_out(h: Harness) -> None:
    p = h.started(head_timeout=0.2)
    client = h.connect()
    client.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n")
    assert wait_until(lambda: len(p.events()) == 1)
    assert p.denials()[0].reason == "malformed"
    assert h.upstream.calls == []
    assert read_to_eof(client).startswith(b"HTTP/1.1 400 Bad Request\r\n")


def test_an_upstream_failure_gets_502(h: Harness) -> None:
    h.upstream.error = OSError("unreachable")
    p = h.started()
    client = h.connect()
    client.sendall(connect_request("chatgpt.com:443"))
    assert read_to_eof(client) == deny_response("502 Bad Gateway")
    assert wait_until(lambda: len(p.events()) == 1)
    event = p.denials()[0]
    assert (event.allowed, event.host, event.port, event.reason) == (
        False,
        "chatgpt.com",
        443,
        "upstream",
    )
    assert p.alive() is True


def test_connections_over_the_limit_get_503(h: Harness) -> None:
    p = h.started(max_connections=1)
    first = h.connect()
    first.sendall(connect_request("chatgpt.com:443"))
    assert read_response(first) == ESTABLISHED
    second = h.connect()
    assert read_to_eof(second) == deny_response("503 Service Unavailable")
    assert wait_until(lambda: len(p.events()) == 2)
    busy = p.denials()[0]
    assert (busy.host, busy.port, busy.reason) == ("", None, "busy")
    assert len(h.upstream.calls) == 1


def test_a_finished_connection_frees_its_slot(h: Harness) -> None:
    p = h.started(max_connections=1)
    first = h.connect()
    first.sendall(connect_request("evil.net:443"))
    assert read_to_eof(first).startswith(b"HTTP/1.1 403")
    assert wait_until(lambda: len(p.events()) == 1)
    time.sleep(0.3)
    second = h.connect()
    second.sendall(connect_request("chatgpt.com:443"))
    assert read_response(second) == ESTABLISHED


# Fail closed


def test_a_log_write_failure_stops_the_proxy_without_passing(h: Harness) -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        p = h.started(log_path=Path(f"/dev/fd/{write_end}"))
        client = h.connect()
        client.sendall(connect_request("chatgpt.com:443"))
        response = read_to_eof(client)
        assert b"200" not in response
        assert wait_until(lambda: not p.alive())
        assert h.listener.closed.is_set()
        for upstream in h.upstream.servers:
            assert read_to_eof(upstream) == b""
    finally:
        os.close(write_end)
