"""ntfy notifications: sending, deduplication and the config permission check."""

import json
import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from orch import config
from orch.config import Config
from orch.redact import RedactionError, redact

TIMEOUT_SEC = 5
DEDUP_WINDOW = timedelta(minutes=30)
NTFY_URL = "https://ntfy.sh/"

Transport = Callable[[str, bytes, float], None]


class Outcome(StrEnum):
    """Result of one notification attempt."""

    SENT = "sent"
    DISABLED = "disabled"
    REFUSED_PERMISSIONS = "refused_permissions"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


def validate_event(event: str) -> None:
    """Raise ValueError if event is not a known event kind."""
    if not isinstance(event, str) or (
        event not in {"quota", "blocked", "needs_review", "input_wait", "test"}
        and not re.fullmatch(r"doctor_[a-z0-9_]+", event)
    ):
        raise ValueError("Invalid notification event")


def dedup_key(event: str, *, target: str = "", task_id: str = "") -> str:
    """Return the deduplication key of an event."""
    validate_event(event)
    if event == "test":
        raise ValueError("Test notifications do not use deduplication")
    if event == "quota":
        return "quota"
    if event.startswith("doctor_"):
        return f"{target}|{event}"
    return f"{target}|{event}|{task_id}"


def message(event: str, *, target: str = "", task_id: str = "") -> str:
    """Return the notification body before redaction."""
    validate_event(event)
    text = f"orch {event}"
    if target:
        text += f" target={target}"
    if task_id:
        text += f" task={task_id}"
    return text


def permissions_ok(path: Path) -> bool:
    """Return True if no group or other permission bit is set on path."""
    try:
        return path.stat().st_mode & 0o077 == 0
    except OSError:
        return False


def load_state(path: Path) -> dict[str, str]:
    """Return the last send time per key, or an empty dict."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in state.items()
        ):
            return state
    except OSError, ValueError, RecursionError:
        pass
    return {}


def save_state(path: Path, state: Mapping[str, str]) -> None:
    """Replace the state file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            json.dump(dict(state), stream)
        os.replace(temporary, path)
    except BaseException:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def should_send(state: Mapping[str, str], key: str, now: datetime) -> bool:
    """Return False if key was sent within DEDUP_WINDOW before now."""
    try:
        last = datetime.fromisoformat(state[key])
    except KeyError, ValueError, TypeError:
        return True
    if last.tzinfo is None or last.utcoffset() is None:
        return True
    return not timedelta(0) <= now - last < DEDUP_WINDOW


def urllib_transport(url: str, body: bytes, timeout: float) -> None:
    """POST body to url with the standard library."""
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def _warn(stderr: TextIO, text: str) -> None:
    """Keep warning output failures from affecting the caller."""
    try:
        stderr.write(f"orch: 알림 {text}\n")
    except Exception:
        pass


def send(
    event: str,
    *,
    settings: Config,
    orch_home: Path,
    home: Path,
    now: datetime,
    target: str = "",
    task_id: str = "",
    transport: Transport = urllib_transport,
    stderr: TextIO | None = None,
    dedup: bool = True,
) -> Outcome:
    """Send one notification. Never raises except for an invalid event."""
    validate_event(event)
    stderr = sys.stderr if stderr is None else stderr
    try:
        if settings.notify_method != "ntfy":
            return Outcome.DISABLED
        if not permissions_ok(config.config_path(orch_home)):
            _warn(stderr, "설정 파일 권한을 확인하세요. chmod 600으로 제한하세요.")
            return Outcome.REFUSED_PERMISSIONS

        if dedup:
            key = dedup_key(event, target=target, task_id=task_id)
            state = load_state(config.notify_state_path(orch_home))
            if not should_send(state, key, now):
                return Outcome.SUPPRESSED

        topic = settings.notify_topic
        try:
            text = redact(
                message(event, target=target, task_id=task_id),
                home=home,
                orch_home=orch_home,
                secrets=[topic],
            )
        except RedactionError:
            _warn(stderr, "본문의 비공개 정보 검사에 실패했습니다.")
            return Outcome.FAILED

        try:
            transport(NTFY_URL + topic, text.encode("utf-8"), TIMEOUT_SEC)
        except Exception as exc:
            warning = "전송에 실패했습니다."
            try:
                detail = redact(
                    " ".join(str(exc).splitlines()),
                    home=home,
                    orch_home=orch_home,
                    secrets=[topic],
                )
                if (
                    detail
                    and topic not in detail
                    and NTFY_URL not in detail
                    and " ".join(text.splitlines()) not in detail
                ):
                    warning += f" {detail}"
            except Exception:
                pass
            _warn(stderr, warning)
            return Outcome.FAILED

        if dedup:
            state[key] = now.isoformat()
            try:
                save_state(config.notify_state_path(orch_home), state)
            except OSError:
                _warn(stderr, "전송 기록을 저장하지 못했습니다.")
        return Outcome.SENT
    except Exception:
        _warn(stderr, "처리에 실패했습니다.")
        return Outcome.FAILED
