"""Daily latest version check of the agents and the doctor agent_latest item (4.13)."""

import json
import os
import re
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from orch import selfcheck
from orch.models import CheckLevel

LATEST_FORMAT: Final = 1
CHECK_INTERVAL: Final = timedelta(hours=24)
FETCH_TIMEOUT_SEC: Final = 10
BODY_LIMIT: Final = 65536
AGENTS: Final = ("claude", "codex")
VERSION_PATTERN: Final = r"[0-9]+(\.[0-9]+){1,3}"
READ_CHUNK: Final = 8192


class LatestError(Exception):
    """config/pins.toml cannot be read or a latest_url is missing or not https."""


@dataclass(frozen=True, slots=True)
class AgentLatest:
    """The latest version of one agent, or why it is unknown."""

    latest: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class LatestRecord:
    """The contents of ~/.orch/latest.json."""

    checked_at: str
    agents: Mapping[str, AgentLatest]


Fetch = Callable[[str, float], tuple[int, bytes]]


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an https URL."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise NotImplementedError


def http_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    """GET url and return its status and at most BODY_LIMIT + 1 body bytes."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read(BODY_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, b""


def latest_path(orch_home: Path) -> Path:
    """Return the path of the latest version record."""
    return orch_home / "latest.json"


def load_urls(hub: Path) -> dict[str, str]:
    """Return each agent's latest_url from the hub's config/pins.toml."""
    try:
        with (hub / "config" / "pins.toml").open("rb") as stream:
            pins = tomllib.load(stream)
        urls = {}
        for agent in AGENTS:
            url = pins[agent]["latest_url"]
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("Invalid latest URL")
            urls[agent] = url
        return urls
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LatestError("Cannot load latest URLs") from exc


def parse_latest(agent: str, status: int, body: bytes) -> AgentLatest:
    """Classify one fetched response."""
    if status != 200:
        return AgentLatest(None, "http")
    if len(body) > BODY_LIMIT:
        return AgentLatest(None, "format")
    try:
        if agent == "claude":
            version = body.decode("ascii").strip()
        elif agent == "codex":
            data = json.loads(body)
            version = data.get("version") if isinstance(data, dict) else None
        else:
            return AgentLatest(None, "format")
        if isinstance(version, str) and re.fullmatch(VERSION_PATTERN, version):
            return AgentLatest(version, None)
    except Exception:
        pass
    return AgentLatest(None, "format")


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 comparing dotted versions numerically."""
    left = [int(part) for part in a.split(".")]
    right = [int(part) for part in b.split(".")]
    length = max(len(left), len(right))
    left.extend([0] * (length - len(left)))
    right.extend([0] * (length - len(right)))
    return (left > right) - (left < right)


def read_record(path: Path) -> LatestRecord | None:
    """Return the stored record, or None when it is missing or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or type(data.get("format")) is not int
            or data["format"] != LATEST_FORMAT
            or not isinstance(data.get("checked_at"), str)
            or not isinstance(data.get("agents"), dict)
        ):
            return None
        checked_at = datetime.fromisoformat(data["checked_at"])
        if checked_at.utcoffset() is None:
            return None
        agents = {}
        for agent in AGENTS:
            entry = data["agents"][agent]
            if not isinstance(entry, dict):
                return None
            version, error = entry["latest"], entry["error"]
            if (
                error not in (None, "http", "format", "fetch")
                or (version is not None and not isinstance(version, str))
                or (isinstance(version, str) != (error is None))
                or (
                    isinstance(version, str)
                    and re.fullmatch(VERSION_PATTERN, version) is None
                )
            ):
                return None
            agents[agent] = AgentLatest(version, error)
        return LatestRecord(data["checked_at"], agents)
    except OSError, ValueError, KeyError, TypeError, RecursionError:
        return None


def write_record(path: Path, record: LatestRecord) -> None:
    """Write the record atomically."""
    data = {
        "format": LATEST_FORMAT,
        "checked_at": record.checked_at,
        "agents": {
            agent: {
                "latest": record.agents[agent].latest,
                "error": record.agents[agent].error,
            }
            for agent in AGENTS
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def refresh(
    *, orch_home: Path, hub: Path, now: datetime, fetch: Fetch = http_fetch
) -> LatestRecord:
    """Return a record no older than CHECK_INTERVAL, fetching when needed."""
    path = latest_path(orch_home)
    record = read_record(path)
    if record is not None:
        checked_at = datetime.fromisoformat(record.checked_at)
        if checked_at <= now and now - checked_at < CHECK_INTERVAL:
            return record
    urls = load_urls(hub)
    agents = {}
    for agent in AGENTS:
        try:
            status, body = fetch(urls[agent], FETCH_TIMEOUT_SEC)
        except Exception:
            agents[agent] = AgentLatest(None, "fetch")
        else:
            agents[agent] = parse_latest(agent, status, body)
    record = LatestRecord(now.isoformat(), agents)
    write_record(path, record)
    return record


def current_versions(orch_home: Path) -> dict[str, str | None]:
    """Return each agent's current version from agents.json."""
    versions: dict[str, str | None] = dict.fromkeys(AGENTS)
    try:
        state = selfcheck.load_agents_state(selfcheck.agents_state_path(orch_home))
    except selfcheck.SelfcheckError:
        return versions
    for agent in AGENTS:
        current = state["agents"].get(agent, {}).get("current")
        if current is not None:
            versions[agent] = current["version"]
    return versions


def check(
    *, orch_home: Path, hub: Path, now: datetime, fetch: Fetch = http_fetch
) -> tuple[CheckLevel, str | None]:
    """Judge the doctor agent_latest item."""
    record = refresh(orch_home=orch_home, hub=hub, now=now, fetch=fetch)
    currents = current_versions(orch_home)
    level = CheckLevel.OK
    details = []
    for agent in AGENTS:
        current = currents[agent]
        version = record.agents[agent].latest
        if current is None:
            detail = "no_record"
        elif version is None:
            detail = "unknown"
        elif compare_versions(version, current) > 0:
            detail = "behind"
            level = CheckLevel.WARN
        else:
            continue
        details.append(f"{agent}={detail}")
    return level, ",".join(details) or None
