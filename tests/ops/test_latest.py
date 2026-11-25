"""Daily latest version check and the agent_latest judgment (technical design 4.13).

Response shapes come from P5-M3 (2026-11-15): the Claude Code latest URL returns
HTTP 200 with the bare version string, the npm registry returns JSON whose
"version" is the latest.
"""

import email.message
import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import latest
from orch.latest import AgentLatest, LatestError, LatestRecord
from orch.models import CheckLevel

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
CLAUDE_URL = "https://downloads.example/claude-code-releases/latest"
CODEX_URL = "https://registry.example/@openai/codex/latest"
PINS = f"""
[claude]
version = "2.1.319"
latest_url = "{CLAUDE_URL}"

[codex]
version = "0.160.0"
latest_url = "{CODEX_URL}"
"""


def write_pins(hub: Path, text: str = PINS) -> Path:
    path = hub / "config" / "pins.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hub


def write_agents(orch_home: Path, claude: str | None, codex: str | None) -> None:
    agents: dict[str, object] = {}
    for name, version in (("claude", claude), ("codex", codex)):
        if version is None:
            continue
        agents[name] = {
            "current": {
                "version": version,
                "sha256": "0" * 64,
                "path": f"/abs/{name}/{version}",
                "at": NOW.isoformat(),
                "source": "bootstrap",
            },
            "previous": None,
            "candidate": None,
        }
    orch_home.mkdir(parents=True, exist_ok=True)
    (orch_home / "agents.json").write_text(
        json.dumps({"format": 1, "agents": agents}), encoding="utf-8"
    )


class FakeFetch:
    """Answers by URL; records each call."""

    def __init__(self, answers: dict[str, tuple[int, bytes] | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, timeout))
        value = self.answers[url]
        if isinstance(value, Exception):
            raise value
        return value


def healthy(claude: str = "2.1.330", codex: str = "0.171.0") -> FakeFetch:
    return FakeFetch(
        {
            CLAUDE_URL: (200, f"{claude}\n".encode()),
            CODEX_URL: (
                200,
                json.dumps({"name": "@openai/codex", "version": codex}).encode(),
            ),
        }
    )


CLAUDE_LATEST = AgentLatest("2.1.330", None)
CODEX_LATEST = AgentLatest("0.171.0", None)


def record(
    checked_at: datetime,
    claude: AgentLatest = CLAUDE_LATEST,
    codex: AgentLatest = CODEX_LATEST,
) -> LatestRecord:
    return LatestRecord(checked_at.isoformat(), {"claude": claude, "codex": codex})


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    path = tmp_path / "orch"
    path.mkdir()
    return path


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    return write_pins(tmp_path / "hub")


# Constants and paths


def test_constants_follow_the_design() -> None:
    assert latest.CHECK_INTERVAL == timedelta(hours=24)
    assert latest.FETCH_TIMEOUT_SEC == 10
    assert latest.AGENTS == ("claude", "codex")
    assert latest.LATEST_FORMAT == 1


def test_the_record_lives_in_the_orch_home(orch_home: Path) -> None:
    assert latest.latest_path(orch_home) == orch_home / "latest.json"


# load_urls


def test_urls_come_from_the_hub_pins(hub: Path) -> None:
    assert latest.load_urls(hub) == {"claude": CLAUDE_URL, "codex": CODEX_URL}


def test_the_real_pins_name_https_latest_urls() -> None:
    root = Path(__file__).resolve().parents[2]
    urls = latest.load_urls(root)
    assert set(urls) == {"claude", "codex"}
    assert all(url.startswith("https://") for url in urls.values())


@pytest.mark.parametrize(
    "text",
    [
        None,
        "not toml [",
        PINS.replace(f'latest_url = "{CODEX_URL}"', ""),
        PINS.replace(CLAUDE_URL, "http://downloads.example/latest"),
        PINS.replace(f'"{CODEX_URL}"', "3"),
        '[claude]\nlatest_url = "https://a.example/latest"\n',
    ],
    ids=["missing", "syntax", "no-codex-url", "not-https", "not-string", "no-codex"],
)
def test_unreadable_pins_raise(tmp_path: Path, text: str | None) -> None:
    hub = tmp_path / "hub"
    if text is not None:
        write_pins(hub, text)
    with pytest.raises(LatestError):
        latest.load_urls(hub)


# parse_latest


@pytest.mark.parametrize(
    ("agent", "status", "body", "expected"),
    [
        ("claude", 200, b"2.1.330\n", AgentLatest("2.1.330", None)),
        ("claude", 200, b"  2.1.330  ", AgentLatest("2.1.330", None)),
        ("claude", 404, b"2.1.330", AgentLatest(None, "http")),
        ("claude", 200, b"<html>", AgentLatest(None, "format")),
        ("claude", 200, b"2.1.330-beta.1", AgentLatest(None, "format")),
        ("claude", 200, b"", AgentLatest(None, "format")),
        ("claude", 200, b"\xff\xfe", AgentLatest(None, "format")),
        ("codex", 200, b'{"version": "0.171.0"}', AgentLatest("0.171.0", None)),
        ("codex", 200, b'{"version": "0.172.0-alpha.3"}', AgentLatest(None, "format")),
        ("codex", 200, b'{"version": 7}', AgentLatest(None, "format")),
        ("codex", 200, b'["0.171.0"]', AgentLatest(None, "format")),
        ("codex", 200, b"0.171.0", AgentLatest(None, "format")),
        ("codex", 500, b'{"version": "0.171.0"}', AgentLatest(None, "http")),
    ],
)
def test_responses_are_classified(
    agent: str, status: int, body: bytes, expected: AgentLatest
) -> None:
    assert latest.parse_latest(agent, status, body) == expected


def test_a_body_over_the_limit_is_a_format_error() -> None:
    body = b"2.1.330" + b" " * latest.BODY_LIMIT
    assert latest.parse_latest("claude", 200, body) == AgentLatest(None, "format")


# compare_versions


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("2.1.330", "2.1.319", 1),
        ("2.1.319", "2.1.330", -1),
        ("2.1.319", "2.1.319", 0),
        ("0.171.0", "0.160.0", 1),
        ("2.10.0", "2.9.9", 1),
        ("2.1", "2.1.0", 0),
        ("2.1.0.1", "2.1.0", 1),
    ],
)
def test_versions_compare_numerically(a: str, b: str, expected: int) -> None:
    assert latest.compare_versions(a, b) == expected


# read_record and write_record


def test_a_written_record_reads_back(orch_home: Path) -> None:
    path = latest.latest_path(orch_home)
    value = record(NOW, codex=AgentLatest(None, "fetch"))
    latest.write_record(path, value)
    assert latest.read_record(path) == value
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "format": 1,
        "checked_at": NOW.isoformat(),
        "agents": {
            "claude": {"latest": "2.1.330", "error": None},
            "codex": {"latest": None, "error": "fetch"},
        },
    }
    assert [p.name for p in orch_home.iterdir()] == ["latest.json"]


def test_a_failed_write_leaves_no_temporary_file(
    orch_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(src: object, dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("os.replace", broken)
    with pytest.raises(OSError):
        latest.write_record(latest.latest_path(orch_home), record(NOW))
    assert list(orch_home.iterdir()) == []


def test_a_missing_record_reads_as_none(orch_home: Path) -> None:
    assert latest.read_record(latest.latest_path(orch_home)) is None


GOOD = {
    "format": 1,
    "checked_at": NOW.isoformat(),
    "agents": {
        "claude": {"latest": "2.1.330", "error": None},
        "codex": {"latest": None, "error": "http"},
    },
}


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        [],
        {**GOOD, "format": 2},
        {**GOOD, "format": True},
        {**GOOD, "checked_at": "yesterday"},
        {**GOOD, "checked_at": "2026-11-24T12:00:00"},
        {**GOOD, "agents": {"claude": GOOD["agents"]["claude"]}},  # type: ignore[index]
        {
            **GOOD,
            "agents": {**GOOD["agents"], "codex": {"latest": None, "error": "other"}},
        },  # type: ignore[dict-item]
        {
            **GOOD,
            "agents": {**GOOD["agents"], "codex": {"latest": "0.1.0", "error": "http"}},
        },  # type: ignore[dict-item]
        {
            **GOOD,
            "agents": {**GOOD["agents"], "codex": {"latest": None, "error": None}},
        },  # type: ignore[dict-item]
        {
            **GOOD,
            "agents": {**GOOD["agents"], "codex": {"latest": "v1", "error": None}},
        },  # type: ignore[dict-item]
    ],
    ids=[
        "not-json",
        "not-object",
        "format-2",
        "format-bool",
        "bad-time",
        "naive-time",
        "missing-agent",
        "bad-error",
        "latest-with-error",
        "neither",
        "bad-version",
    ],
)
def test_a_malformed_record_reads_as_none(orch_home: Path, value: object) -> None:
    path = latest.latest_path(orch_home)
    text = value if isinstance(value, str) else json.dumps(value)
    path.write_text(text, encoding="utf-8")
    assert latest.read_record(path) is None


def test_the_good_sample_reads(orch_home: Path) -> None:
    path = latest.latest_path(orch_home)
    path.write_text(json.dumps(GOOD), encoding="utf-8")
    assert latest.read_record(path) == record(NOW, codex=AgentLatest(None, "http"))


# refresh


def test_a_missing_record_is_fetched_and_written(orch_home: Path, hub: Path) -> None:
    fetch = healthy()
    value = latest.refresh(orch_home=orch_home, hub=hub, now=NOW, fetch=fetch)
    assert value == record(NOW)
    assert fetch.calls == [(CLAUDE_URL, 10), (CODEX_URL, 10)]
    assert latest.read_record(latest.latest_path(orch_home)) == value


@pytest.mark.parametrize("age", [timedelta(0), timedelta(hours=23, minutes=59)])
def test_a_fresh_record_is_used_without_pins_or_fetch(
    orch_home: Path, tmp_path: Path, age: timedelta
) -> None:
    stored = record(NOW - age, claude=AgentLatest("2.1.320", None))
    latest.write_record(latest.latest_path(orch_home), stored)
    fetch = healthy()
    no_hub = tmp_path / "no-hub"
    value = latest.refresh(orch_home=orch_home, hub=no_hub, now=NOW, fetch=fetch)
    assert value == stored
    assert fetch.calls == []


@pytest.mark.parametrize(
    "checked_at",
    [NOW - timedelta(hours=24), NOW - timedelta(days=3), NOW + timedelta(minutes=1)],
    ids=["one-day", "three-days", "future"],
)
def test_an_old_or_future_record_is_fetched_again(
    orch_home: Path, hub: Path, checked_at: datetime
) -> None:
    latest.write_record(
        latest.latest_path(orch_home), record(checked_at, AgentLatest("1.0.0", None))
    )
    fetch = healthy()
    value = latest.refresh(orch_home=orch_home, hub=hub, now=NOW, fetch=fetch)
    assert value == record(NOW)
    assert len(fetch.calls) == 2


def test_a_malformed_record_is_fetched_again(orch_home: Path, hub: Path) -> None:
    latest.latest_path(orch_home).write_text("{", encoding="utf-8")
    fetch = healthy()
    assert latest.refresh(orch_home=orch_home, hub=hub, now=NOW, fetch=fetch) == record(
        NOW
    )


def test_failures_are_recorded_so_the_next_fetch_waits_a_day(
    orch_home: Path, hub: Path
) -> None:
    fetch = FakeFetch({CLAUDE_URL: OSError("offline"), CODEX_URL: (503, b"")})
    value = latest.refresh(orch_home=orch_home, hub=hub, now=NOW, fetch=fetch)
    assert value == record(NOW, AgentLatest(None, "fetch"), AgentLatest(None, "http"))
    later = NOW + timedelta(minutes=10)
    again = latest.refresh(orch_home=orch_home, hub=hub, now=later, fetch=fetch)
    assert again == value
    assert len(fetch.calls) == 2


def test_any_fetch_exception_is_a_fetch_error(orch_home: Path, hub: Path) -> None:
    fetch = FakeFetch({CLAUDE_URL: ValueError("odd"), CODEX_URL: TimeoutError()})
    value = latest.refresh(orch_home=orch_home, hub=hub, now=NOW, fetch=fetch)
    assert value.agents == {
        "claude": AgentLatest(None, "fetch"),
        "codex": AgentLatest(None, "fetch"),
    }


def test_unreadable_pins_raise_when_a_fetch_is_due(
    orch_home: Path, tmp_path: Path
) -> None:
    fetch = healthy()
    with pytest.raises(LatestError):
        latest.refresh(orch_home=orch_home, hub=tmp_path / "none", now=NOW, fetch=fetch)
    assert fetch.calls == []
    assert not latest.latest_path(orch_home).exists()


# current_versions


def test_current_versions_come_from_agents_json(orch_home: Path) -> None:
    write_agents(orch_home, "2.1.319", "0.171.0")
    assert latest.current_versions(orch_home) == {
        "claude": "2.1.319",
        "codex": "0.171.0",
    }


def test_a_missing_agent_has_no_current_version(orch_home: Path) -> None:
    write_agents(orch_home, "2.1.319", None)
    assert latest.current_versions(orch_home) == {"claude": "2.1.319", "codex": None}


@pytest.mark.parametrize("text", [None, "{", '{"format": 9, "agents": {}}'])
def test_an_unreadable_agents_state_has_no_current_versions(
    orch_home: Path, text: str | None
) -> None:
    if text is not None:
        (orch_home / "agents.json").write_text(text, encoding="utf-8")
    assert latest.current_versions(orch_home) == {"claude": None, "codex": None}


# check


def judge(
    orch_home: Path, hub: Path, fetch: FakeFetch
) -> tuple[CheckLevel, str | None]:
    return latest.check(orch_home=orch_home, hub=hub, now=NOW, fetch=fetch)


def test_up_to_date_agents_are_ok(orch_home: Path, hub: Path) -> None:
    write_agents(orch_home, "2.1.330", "0.171.0")
    assert judge(orch_home, hub, healthy()) == (CheckLevel.OK, None)


def test_a_current_newer_than_latest_is_ok(orch_home: Path, hub: Path) -> None:
    write_agents(orch_home, "2.1.331", "0.172.0")
    assert judge(orch_home, hub, healthy()) == (CheckLevel.OK, None)


@pytest.mark.parametrize(
    ("claude", "codex", "detail"),
    [
        ("2.1.319", "0.171.0", "claude=behind"),
        ("2.1.330", "0.160.0", "codex=behind"),
        ("2.1.319", "0.160.0", "claude=behind,codex=behind"),
    ],
)
def test_an_agent_behind_latest_warns(
    orch_home: Path, hub: Path, claude: str, codex: str, detail: str
) -> None:
    write_agents(orch_home, claude, codex)
    assert judge(orch_home, hub, healthy()) == (CheckLevel.WARN, detail)


def test_unknown_latest_and_no_record_are_ok_with_detail(
    orch_home: Path, hub: Path
) -> None:
    write_agents(orch_home, None, "0.171.0")
    fetch = FakeFetch({CLAUDE_URL: (200, b"2.1.330"), CODEX_URL: OSError("offline")})
    assert judge(orch_home, hub, fetch) == (
        CheckLevel.OK,
        "claude=no_record,codex=unknown",
    )


def test_behind_with_an_unknown_other_still_warns(orch_home: Path, hub: Path) -> None:
    write_agents(orch_home, "2.1.319", "0.171.0")
    fetch = FakeFetch({CLAUDE_URL: (200, b"2.1.330"), CODEX_URL: (404, b"")})
    assert judge(orch_home, hub, fetch) == (
        CheckLevel.WARN,
        "claude=behind,codex=unknown",
    )


def test_the_detail_holds_no_version_strings(orch_home: Path, hub: Path) -> None:
    write_agents(orch_home, "2.1.319", "0.160.0")
    _, detail = judge(orch_home, hub, healthy())
    assert detail is not None
    assert not any(char.isdigit() for char in detail)


def test_check_raises_when_pins_cannot_be_read(orch_home: Path, tmp_path: Path) -> None:
    with pytest.raises(LatestError):
        judge(orch_home, tmp_path / "none", healthy())


def test_check_uses_the_stored_record_within_a_day(orch_home: Path, hub: Path) -> None:
    write_agents(orch_home, "2.1.319", "0.171.0")
    latest.write_record(
        latest.latest_path(orch_home),
        record(NOW - timedelta(hours=1), AgentLatest("2.1.319", None)),
    )
    fetch = healthy()
    assert judge(orch_home, hub, fetch) == (CheckLevel.OK, None)
    assert fetch.calls == []


# Fetch hardening (m-latest-https, #605)


class FakeResponse:
    """A response whose read() hands out the given chunks in order."""

    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self.chunks = list(chunks)
        self.sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.opened: list[tuple[str, float]] = []

    def open(self, url: str, timeout: float) -> FakeResponse:
        self.opened.append((url, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def use_opener(
    monkeypatch: pytest.MonkeyPatch, response: FakeResponse | Exception
) -> tuple[FakeOpener, list[tuple]]:
    opener = FakeOpener(response)
    built: list[tuple] = []

    def build_opener(*handlers: object) -> FakeOpener:
        built.append(handlers)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)

    def no_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("urlopen must not be used")

    monkeypatch.setattr(urllib.request, "urlopen", no_urlopen)
    return opener, built


def is_https_only(handler: object) -> bool:
    return handler is latest.HttpsOnlyRedirectHandler or isinstance(
        handler, latest.HttpsOnlyRedirectHandler
    )


def test_the_read_chunk_is_the_designed_value() -> None:
    assert latest.READ_CHUNK == 8192


def test_the_fetch_uses_an_opener_that_only_follows_https_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(200, [b"2.1.330\n"])
    opener, built = use_opener(monkeypatch, response)
    assert latest.http_fetch(CLAUDE_URL, 10) == (200, b"2.1.330\n")
    assert len(built) == 1
    assert any(is_https_only(handler) for handler in built[0])
    assert opener.opened == [(CLAUDE_URL, 10)]
    assert all(size == latest.READ_CHUNK for size in response.sizes)


def redirect(newurl: str) -> object:
    handler = latest.HttpsOnlyRedirectHandler()
    request = urllib.request.Request(CLAUDE_URL)
    return handler.redirect_request(
        request, io.BytesIO(b""), 302, "Found", email.message.Message(), newurl
    )


@pytest.mark.parametrize(
    "newurl",
    [
        "http://downloads.example.com/latest",
        "ftp://downloads.example.com/latest",
        "file:///etc/passwd",
        "//downloads.example.com/latest",
    ],
)
def test_a_redirect_to_a_non_https_url_is_refused(newurl: str) -> None:
    with pytest.raises(urllib.error.URLError) as caught:
        redirect(newurl)
    assert not isinstance(caught.value, urllib.error.HTTPError)


@pytest.mark.parametrize(
    "newurl", ["https://cdn.example.com/latest", "HTTPS://cdn.example.com/latest"]
)
def test_a_redirect_to_https_is_followed(newurl: str) -> None:
    followed = redirect(newurl)
    assert isinstance(followed, urllib.request.Request)
    assert followed.full_url == newurl


def test_a_refused_redirect_is_recorded_as_a_fetch_error(
    orch_home: Path, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener, _ = use_opener(
        monkeypatch, urllib.error.URLError("redirect to a non-https URL")
    )
    result = latest.refresh(orch_home=orch_home, hub=hub, now=NOW)
    assert result.agents["claude"] == AgentLatest(None, "fetch")
    assert result.agents["codex"] == AgentLatest(None, "fetch")
    assert [url for url, _ in opener.opened] == [CLAUDE_URL, CODEX_URL]


def test_an_http_error_is_still_returned_as_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        CLAUDE_URL, 404, "Not Found", email.message.Message(), io.BytesIO(b"")
    )
    use_opener(monkeypatch, error)
    assert latest.http_fetch(CLAUDE_URL, 10) == (404, b"")


def test_a_body_in_many_chunks_is_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    use_opener(monkeypatch, FakeResponse(200, [b"2.", b"1.", b"330", b"\n"]))
    assert latest.http_fetch(CLAUDE_URL, 10) == (200, b"2.1.330\n")


def test_reading_stops_past_the_body_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    chunk = b"x" * latest.READ_CHUNK
    count = latest.BODY_LIMIT // latest.READ_CHUNK + 5
    response = FakeResponse(200, [chunk] * count)
    use_opener(monkeypatch, response)
    status, body = latest.http_fetch(CLAUDE_URL, 10)
    assert status == 200
    assert body == b"x" * (latest.BODY_LIMIT + 1)
    assert response.chunks, "reading must stop once the limit is passed"


def test_a_slow_body_hits_the_total_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter(range(1000))
    monkeypatch.setattr(latest.time, "monotonic", lambda: float(next(clock)))
    response = FakeResponse(200, [b"2"] * 100)
    use_opener(monkeypatch, response)
    with pytest.raises(TimeoutError):
        latest.http_fetch(CLAUDE_URL, 5)
    assert len(response.chunks) > 90


def test_a_slow_body_is_recorded_as_a_fetch_error(
    orch_home: Path, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(range(100000))
    monkeypatch.setattr(latest.time, "monotonic", lambda: float(next(clock)))

    response = FakeResponse(200, [b"2"] * 100)
    opener, _ = use_opener(monkeypatch, response)
    result = latest.refresh(orch_home=orch_home, hub=hub, now=NOW)
    assert result.agents["claude"] == AgentLatest(None, "fetch")
    assert opener.opened[0] == (CLAUDE_URL, latest.FETCH_TIMEOUT_SEC)
    assert response.sizes, "the body must be read through the opener"


@pytest.mark.parametrize("version", ["2.1.x", "", "v2.1.330", "2", "2.1.330-beta"])
def test_a_malformed_current_version_reads_as_no_record(
    orch_home: Path, version: object
) -> None:
    write_agents(orch_home, "2.1.330", "0.171.0")
    path = orch_home / "agents.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["agents"]["claude"]["current"]["version"] = version
    path.write_text(json.dumps(state), encoding="utf-8")
    assert latest.current_versions(orch_home) == {"claude": None, "codex": "0.171.0"}


def test_a_malformed_current_version_does_not_fail_the_check(
    orch_home: Path, hub: Path
) -> None:
    write_agents(orch_home, "2.1.330", "0.160.0")
    path = orch_home / "agents.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["agents"]["claude"]["current"]["version"] = "2.1.x"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert judge(orch_home, hub, healthy()) == (
        CheckLevel.WARN,
        "claude=no_record,codex=behind",
    )
