import io
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from orch import notify
from orch.config import Config, config_path, load_config, notify_state_path
from orch.notify import (
    DEDUP_WINDOW,
    NTFY_URL,
    TIMEOUT_SEC,
    Outcome,
    dedup_key,
    load_state,
    message,
    permissions_ok,
    save_state,
    send,
    should_send,
    urllib_transport,
    validate_event,
)

TOPIC = "zq7Kx9mP2vLw4RtY8nBcHs3D"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes, float]] = []
        self.error = error

    def __call__(self, url: str, body: bytes, timeout: float) -> None:
        self.calls.append((url, body, timeout))
        if self.error is not None:
            raise self.error


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Fail the test if anything reaches the real urlopen."""
    calls: list[object] = []

    def guard(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("network used")

    monkeypatch.setattr(urllib.request, "urlopen", guard)
    yield calls
    assert calls == []


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    home = tmp_path / "orch"
    home.mkdir()
    return home


def write_config(orch_home: Path, text: str, mode: int = 0o600) -> Config:
    path = config_path(orch_home)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return load_config(orch_home)


def ntfy_config(orch_home: Path, mode: int = 0o600) -> Config:
    return write_config(
        orch_home, f'[notify]\nmethod = "ntfy"\ntopic = "{TOPIC}"\n', mode
    )


def do_send(
    event: str,
    settings: Config,
    orch_home: Path,
    transport: FakeTransport,
    stderr: io.StringIO,
    *,
    now: datetime = NOW,
    target: str = "orchestrator",
    task_id: str = "m-x",
    dedup: bool = True,
) -> Outcome:
    return send(
        event,
        settings=settings,
        orch_home=orch_home,
        home=orch_home.parent,
        now=now,
        target=target,
        task_id=task_id,
        transport=transport,
        stderr=stderr,
        dedup=dedup,
    )


def assert_warning(stderr: io.StringIO) -> str:
    text = stderr.getvalue()
    assert text.startswith("orch: 알림")
    assert text.count("\n") <= 1
    assert TOPIC not in text
    assert NTFY_URL not in text
    return text


# config.notify_state_path


def test_notify_state_path_sits_in_orch_home(tmp_path: Path) -> None:
    assert notify_state_path(tmp_path) == tmp_path / "notify.json"


# events, keys and body


@pytest.mark.parametrize(
    "event",
    [
        "quota",
        "blocked",
        "needs_review",
        "input_wait",
        "test",
        "doctor_disk",
        "doctor_a_1",
    ],
)
def test_known_events_are_valid(event: str) -> None:
    validate_event(event)


@pytest.mark.parametrize(
    "event", ["", "QUOTA", "other", "doctor_", "doctor_Disk", "doctor_x-y", "quota "]
)
def test_unknown_events_raise(event: str) -> None:
    with pytest.raises(ValueError):
        validate_event(event)


@pytest.mark.parametrize(
    ("event", "target", "task_id", "key"),
    [
        ("quota", "orchestrator", "m-x", "quota"),
        ("quota", "", "", "quota"),
        ("doctor_disk", "orchestrator", "m-x", "orchestrator|doctor_disk|m-x|"),
        ("blocked", "orchestrator", "m-x", "orchestrator|blocked|m-x"),
        ("needs_review", "orchestrator", "m-x", "orchestrator|needs_review|m-x"),
        ("input_wait", "orchestrator", "", "orchestrator|input_wait|"),
    ],
)
def test_dedup_key_per_event(event: str, target: str, task_id: str, key: str) -> None:
    assert dedup_key(event, target=target, task_id=task_id) == key


@pytest.mark.parametrize("event", ["test", "unknown"])
def test_dedup_key_refuses_test_and_unknown_events(event: str) -> None:
    with pytest.raises(ValueError):
        dedup_key(event, target="orchestrator", task_id="m-x")


@pytest.mark.parametrize(
    ("target", "task_id", "body"),
    [
        ("orchestrator", "m-x", "orch blocked target=orchestrator task=m-x"),
        ("orchestrator", "", "orch blocked target=orchestrator"),
        ("", "m-x", "orch blocked task=m-x"),
        ("", "", "orch blocked"),
    ],
)
def test_message_holds_only_event_target_and_task(
    target: str, task_id: str, body: str
) -> None:
    assert message("blocked", target=target, task_id=task_id) == body


def test_message_refuses_unknown_events() -> None:
    with pytest.raises(ValueError):
        message("other")


# permissions


@pytest.mark.parametrize(
    ("mode", "ok"),
    [
        (0o600, True),
        (0o400, True),
        (0o700, True),
        (0o644, False),
        (0o640, False),
        (0o602, False),
    ],
)
def test_permissions_ok(tmp_path: Path, mode: int, ok: bool) -> None:
    path = tmp_path / "config"
    path.write_text("", encoding="utf-8")
    path.chmod(mode)
    assert permissions_ok(path) is ok


def test_permissions_of_a_missing_file_are_not_ok(tmp_path: Path) -> None:
    assert permissions_ok(tmp_path / "missing") is False


# state file


def test_state_round_trip_creates_the_parent(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "notify.json"
    save_state(path, {"quota": NOW.isoformat()})
    assert load_state(path) == {"quota": NOW.isoformat()}
    assert json.loads(path.read_text(encoding="utf-8")) == {"quota": NOW.isoformat()}
    assert sorted(p.name for p in path.parent.iterdir()) == ["notify.json"]


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", '"x"', '{"quota": 1}', '{"quota": null}']
)
def test_unusable_state_reads_as_empty(tmp_path: Path, text: str) -> None:
    path = tmp_path / "notify.json"
    path.write_text(text, encoding="utf-8")
    assert load_state(path) == {}


def test_missing_or_unreadable_state_reads_as_empty(tmp_path: Path) -> None:
    assert load_state(tmp_path / "missing.json") == {}
    (tmp_path / "dir.json").mkdir()
    assert load_state(tmp_path / "dir.json") == {}


def test_failed_save_raises_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "notify.json"
    path.write_text('{"quota": "old"}', encoding="utf-8")

    def fail(*args: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        save_state(path, {"quota": NOW.isoformat()})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notify.json"]
    assert path.read_text(encoding="utf-8") == '{"quota": "old"}'


@pytest.mark.parametrize(
    ("state", "now", "expected"),
    [
        ({}, NOW, True),
        ({"quota": NOW.isoformat()}, NOW, False),
        ({"quota": NOW.isoformat()}, NOW + DEDUP_WINDOW - timedelta(seconds=1), False),
        ({"quota": NOW.isoformat()}, NOW + DEDUP_WINDOW, True),
        ({"quota": NOW.isoformat()}, NOW - timedelta(minutes=1), True),
        ({"quota": "not a time"}, NOW, True),
        ({"quota": "2026-11-06T12:00:00"}, NOW, True),
        ({"other": NOW.isoformat()}, NOW, True),
    ],
)
def test_should_send(state: dict[str, str], now: datetime, expected: bool) -> None:
    assert should_send(state, "quota", now) is expected


def test_should_send_compares_across_time_zones() -> None:
    seoul = timezone(timedelta(hours=9))
    state = {"quota": NOW.isoformat()}
    assert (
        should_send(state, "quota", (NOW + timedelta(minutes=10)).astimezone(seoul))
        is False
    )
    assert should_send(state, "quota", (NOW + DEDUP_WINDOW).astimezone(seoul)) is True


def test_window_and_timeout_match_the_design() -> None:
    assert DEDUP_WINDOW == timedelta(minutes=30)
    assert TIMEOUT_SEC == 5
    assert NTFY_URL == "https://ntfy.sh/"


# urllib transport


def test_urllib_transport_posts_with_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[urllib.request.Request, float]] = []
    closed: list[bool] = []

    class Response:
        def close(self) -> None:
            closed.append(True)

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> Response:
        seen.append((request, timeout))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    urllib_transport("https://ntfy.sh/topic", b"orch quota", 5)
    [(request, timeout)] = seen
    assert request.full_url == "https://ntfy.sh/topic"
    assert request.get_method() == "POST"
    assert request.data == b"orch quota"
    assert timeout == 5
    assert closed


def test_urllib_transport_propagates_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(urllib.error.URLError):
        urllib_transport("https://ntfy.sh/topic", b"orch quota", 5)


# send


def test_send_posts_the_redacted_body_and_records_the_key(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    outcome = do_send("blocked", settings, orch_home, transport, stderr)
    assert outcome is Outcome.SENT
    assert transport.calls == [
        (NTFY_URL + TOPIC, b"orch blocked target=orchestrator task=m-x", TIMEOUT_SEC)
    ]
    assert stderr.getvalue() == ""
    assert load_state(notify_state_path(orch_home)) == {
        "orchestrator|blocked|m-x": NOW.isoformat()
    }


def test_missing_notify_section_is_disabled(orch_home: Path) -> None:
    settings = load_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    assert (
        do_send("blocked", settings, orch_home, transport, stderr) is Outcome.DISABLED
    )
    assert transport.calls == []
    assert stderr.getvalue() == ""
    assert not notify_state_path(orch_home).exists()


def test_method_none_is_disabled_without_a_permission_warning(orch_home: Path) -> None:
    settings = write_config(orch_home, '[notify]\nmethod = "none"\n', 0o644)
    transport = FakeTransport()
    stderr = io.StringIO()
    assert do_send("quota", settings, orch_home, transport, stderr) is Outcome.DISABLED
    assert transport.calls == []
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_open_config_refuses_to_send(orch_home: Path, mode: int) -> None:
    settings = ntfy_config(orch_home, mode)
    transport = FakeTransport()
    stderr = io.StringIO()
    outcome = do_send("blocked", settings, orch_home, transport, stderr)
    assert outcome is Outcome.REFUSED_PERMISSIONS
    assert transport.calls == []
    assert "chmod 600" in assert_warning(stderr)
    assert not notify_state_path(orch_home).exists()


def test_same_key_is_suppressed_for_thirty_minutes(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    first = do_send("blocked", settings, orch_home, transport, stderr)
    second = do_send(
        "blocked",
        settings,
        orch_home,
        transport,
        stderr,
        now=NOW + timedelta(minutes=10),
    )
    third = do_send(
        "blocked", settings, orch_home, transport, stderr, now=NOW + DEDUP_WINDOW
    )
    assert (first, second, third) == (Outcome.SENT, Outcome.SUPPRESSED, Outcome.SENT)
    assert len(transport.calls) == 2
    assert stderr.getvalue() == ""
    assert load_state(notify_state_path(orch_home)) == {
        "orchestrator|blocked|m-x": (NOW + DEDUP_WINDOW).isoformat()
    }


def test_different_tasks_do_not_suppress_each_other(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    outcomes = [
        do_send("blocked", settings, orch_home, transport, stderr, task_id="a"),
        do_send("blocked", settings, orch_home, transport, stderr, task_id="b"),
        do_send("needs_review", settings, orch_home, transport, stderr, task_id="a"),
    ]
    assert outcomes == [Outcome.SENT] * 3


def test_quota_is_suppressed_across_targets_and_tasks(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    first = do_send(
        "quota", settings, orch_home, transport, stderr, target="a", task_id="x"
    )
    second = do_send(
        "quota", settings, orch_home, transport, stderr, target="b", task_id="y"
    )
    assert (first, second) == (Outcome.SENT, Outcome.SUPPRESSED)
    assert transport.calls[0][1] == b"orch quota target=a task=x"


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        urllib.error.URLError(f"cannot reach {NTFY_URL}{TOPIC}"),
        OSError(f"POST {NTFY_URL}{TOPIC} failed"),
        RuntimeError(TOPIC),
    ],
)
def test_transport_failure_warns_without_the_topic_and_records_nothing(
    orch_home: Path, error: Exception
) -> None:
    settings = ntfy_config(orch_home)
    stderr = io.StringIO()
    outcome = do_send("blocked", settings, orch_home, FakeTransport(error), stderr)
    assert outcome is Outcome.FAILED
    assert_warning(stderr)
    assert not notify_state_path(orch_home).exists()
    retry = FakeTransport()
    assert do_send("blocked", settings, orch_home, retry, io.StringIO()) is Outcome.SENT
    assert len(retry.calls) == 1


def test_transport_failure_message_is_kept_when_it_holds_no_secret(
    orch_home: Path,
) -> None:
    settings = ntfy_config(orch_home)
    stderr = io.StringIO()
    error = urllib.error.URLError("connection refused")
    do_send("blocked", settings, orch_home, FakeTransport(error), stderr)
    assert "connection refused" in assert_warning(stderr)


@pytest.mark.parametrize(
    ("target", "task_id"),
    [
        (TOPIC, "m-x"),
        ("orchestrator", "ghp_" + "a" * 36),
        ("orchestrator", "/Users/someone/x"),
    ],
)
def test_redaction_failure_does_not_send(
    orch_home: Path, target: str, task_id: str
) -> None:
    settings = ntfy_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    outcome = do_send(
        "blocked",
        settings,
        orch_home,
        transport,
        stderr,
        target=target,
        task_id=task_id,
    )
    assert outcome is Outcome.FAILED
    assert transport.calls == []
    text = assert_warning(stderr)
    assert task_id not in text
    assert not notify_state_path(orch_home).exists()


def test_state_write_failure_still_counts_as_sent(
    orch_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ntfy_config(orch_home)

    def fail(path: Path, state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(notify, "save_state", fail)
    transport = FakeTransport()
    stderr = io.StringIO()
    assert do_send("blocked", settings, orch_home, transport, stderr) is Outcome.SENT
    assert len(transport.calls) == 1
    assert_warning(stderr)


def test_corrupt_state_does_not_block_sending(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    notify_state_path(orch_home).write_text("{broken", encoding="utf-8")
    transport = FakeTransport()
    assert (
        do_send("quota", settings, orch_home, transport, io.StringIO()) is Outcome.SENT
    )
    assert load_state(notify_state_path(orch_home)) == {"quota": NOW.isoformat()}


def test_without_dedup_the_state_is_neither_read_nor_written(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    transport = FakeTransport()
    stderr = io.StringIO()
    outcomes = [
        do_send(
            "test",
            settings,
            orch_home,
            transport,
            stderr,
            target="",
            task_id="",
            dedup=False,
        ),
        do_send(
            "test",
            settings,
            orch_home,
            transport,
            stderr,
            target="",
            task_id="",
            dedup=False,
        ),
    ]
    assert outcomes == [Outcome.SENT, Outcome.SENT]
    assert [call[1] for call in transport.calls] == [b"orch test", b"orch test"]
    assert not notify_state_path(orch_home).exists()


def test_without_dedup_a_recent_record_does_not_suppress(orch_home: Path) -> None:
    settings = ntfy_config(orch_home)
    save_state(notify_state_path(orch_home), {"quota": NOW.isoformat()})
    before = notify_state_path(orch_home).read_bytes()
    transport = FakeTransport()
    outcome = do_send(
        "quota", settings, orch_home, transport, io.StringIO(), dedup=False
    )
    assert outcome is Outcome.SENT
    assert notify_state_path(orch_home).read_bytes() == before


def test_without_dedup_permissions_and_redaction_still_apply(orch_home: Path) -> None:
    transport = FakeTransport()
    refused = do_send(
        "test",
        ntfy_config(orch_home, 0o644),
        orch_home,
        transport,
        io.StringIO(),
        dedup=False,
    )
    failed = do_send(
        "test",
        ntfy_config(orch_home),
        orch_home,
        transport,
        io.StringIO(),
        target=TOPIC,
        dedup=False,
    )
    assert (refused, failed) == (Outcome.REFUSED_PERMISSIONS, Outcome.FAILED)
    assert transport.calls == []


@pytest.mark.parametrize("configured", [False, True])
def test_invalid_event_raises_before_anything_else(
    orch_home: Path, configured: bool
) -> None:
    settings = ntfy_config(orch_home) if configured else load_config(orch_home)
    transport = FakeTransport()
    with pytest.raises(ValueError):
        do_send("other", settings, orch_home, transport, io.StringIO())
    assert transport.calls == []


def test_warnings_go_to_the_current_sys_stderr_by_default(
    orch_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = ntfy_config(orch_home, 0o644)
    outcome = send(
        "quota",
        settings=settings,
        orch_home=orch_home,
        home=orch_home.parent,
        now=NOW,
        transport=FakeTransport(),
    )
    assert outcome is Outcome.REFUSED_PERMISSIONS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("orch: 알림")
    assert TOPIC not in captured.err


def test_default_transport_is_urllib_and_reaches_urlopen(
    orch_home: Path, no_network: list[object]
) -> None:
    settings = ntfy_config(orch_home)
    stderr = io.StringIO()
    outcome = send(
        "quota",
        settings=settings,
        orch_home=orch_home,
        home=orch_home.parent,
        now=NOW,
        stderr=stderr,
    )
    assert outcome is Outcome.FAILED
    assert len(no_network) == 1
    no_network.clear()
    assert_warning(stderr)


# The doctor key holds the task id and the check level


@pytest.mark.parametrize(
    ("event", "target", "task_id", "level", "key"),
    [
        ("doctor_disk", "", "", "fail", "|doctor_disk||fail"),
        ("doctor_disk", "", "", "warn", "|doctor_disk||warn"),
        ("doctor_main_session", "a", "", "warn", "a|doctor_main_session||warn"),
        ("doctor_stuck_task", "a", "m-x", "", "a|doctor_stuck_task|m-x|"),
        ("quota", "a", "m-x", "fail", "quota"),
        ("blocked", "a", "m-x", "fail", "a|blocked|m-x"),
        ("needs_review", "a", "m-x", "fail", "a|needs_review|m-x"),
        ("input_wait", "a", "", "fail", "a|input_wait|"),
    ],
)
def test_dedup_key_with_a_level(
    event: str, target: str, task_id: str, level: str, key: str
) -> None:
    assert dedup_key(event, target=target, task_id=task_id, level=level) == key


def send_level(
    orch_home: Path,
    transport: FakeTransport,
    level: str,
    *,
    now: datetime = NOW,
    event: str = "doctor_disk",
    task_id: str = "",
) -> Outcome:
    return send(
        event,
        settings=ntfy_config(orch_home),
        orch_home=orch_home,
        home=orch_home.parent,
        now=now,
        task_id=task_id,
        level=level,
        transport=transport,
        stderr=io.StringIO(),
    )


def test_a_worse_level_is_not_suppressed_by_the_earlier_one(orch_home: Path) -> None:
    transport = FakeTransport()
    later = NOW + timedelta(minutes=10)
    assert send_level(orch_home, transport, "warn") is Outcome.SENT
    assert send_level(orch_home, transport, "fail", now=later) is Outcome.SENT
    assert len(transport.calls) == 2
    assert load_state(notify_state_path(orch_home)) == {
        "|doctor_disk||warn": NOW.isoformat(),
        "|doctor_disk||fail": later.isoformat(),
    }


def test_the_same_level_is_still_suppressed_for_thirty_minutes(orch_home: Path) -> None:
    transport = FakeTransport()
    assert send_level(orch_home, transport, "fail") is Outcome.SENT
    assert (
        send_level(orch_home, transport, "fail", now=NOW + timedelta(minutes=29))
        is Outcome.SUPPRESSED
    )
    assert send_level(orch_home, transport, "fail", now=NOW + DEDUP_WINDOW) is (
        Outcome.SENT
    )


def test_doctor_events_of_different_tasks_do_not_suppress_each_other(
    orch_home: Path,
) -> None:
    transport = FakeTransport()
    event = "doctor_stuck_task"
    assert (
        send_level(orch_home, transport, "", event=event, task_id="a") is Outcome.SENT
    )
    assert (
        send_level(orch_home, transport, "", event=event, task_id="b") is Outcome.SENT
    )


def test_the_level_stays_out_of_the_notification_body(orch_home: Path) -> None:
    transport = FakeTransport()
    assert send_level(orch_home, transport, "fail") is Outcome.SENT
    assert transport.calls[0][1] == b"orch doctor_disk"


def test_the_level_does_not_change_other_events(orch_home: Path) -> None:
    transport = FakeTransport()
    assert send_level(orch_home, transport, "warn", event="quota") is Outcome.SENT
    assert send_level(orch_home, transport, "fail", event="quota") is Outcome.SUPPRESSED
