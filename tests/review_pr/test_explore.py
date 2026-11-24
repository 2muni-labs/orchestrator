"""explore: Codex read-only exploration with checked answers (technical design 4.16)."""
# ruff: noqa: F811 - fixtures imported from test_runner are used as parameters

import dataclasses
import fcntl
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch import review
from orch.config import Limits, TargetPaths
from orch.explore import (
    EXPLORE_TIMEOUT_SEC,
    QUESTION_LIMIT_BYTES,
    ExploreError,
    ExploreOutcome,
    ExploreRun,
    acquire_slot,
    check_answer,
    explore_prompt,
    explore_root,
    next_explore_log,
    read_question,
    release_slot,
    resolve_dir,
    run_explore,
)
from test_runner import (  # noqa: F401 - fixtures used by name
    HUB,
    git,
    home,
    isolated_git,
    make_config,
    make_env,
    make_target,
    paths,
    repo,
    write,
)

EID = "q1"
QUESTION = "runner.py에서 프록시를 시작하는 함수는 무엇인가?\n"


# Helpers


def question_file(paths: TargetPaths, eid: str = EID, text: str = QUESTION) -> Path:
    path = explore_root(paths) / f"{eid}.md"
    write(path, text)
    return path


def claim(path: str = "src/app.py", line: int = 1, **extra: object) -> dict:
    return {
        "statement": "앱 모듈이 있다.",
        "path": path,
        "line": line,
        "method": "read",
        **extra,
    }


def answer(eid: str = EID, claims: list | None = None, unresolved=()) -> dict:
    return {
        "explore_id": eid,
        "claims": [claim()] if claims is None else claims,
        "unresolved": list(unresolved),
    }


def answer_file(tmp_path: Path, value: object) -> Path:
    path = tmp_path / "answer.json"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def explore(
    repo: Path,
    paths: TargetPaths,
    home: Path,
    scenario: str,
    *,
    question: Path | None = None,
    dir_arg: Path | None = None,
    lane_a: int = 2,
    timeout_sec: int | None = None,
    **extra: str,
) -> ExploreRun:
    return run_explore(
        question_file(paths) if question is None else question,
        dir_arg=dir_arg,
        target=make_target(repo),
        config=make_config(limits=Limits(lane_a=lane_a)),
        paths=paths,
        environ=make_env(home, scenario, **extra),
        timeout_sec=timeout_sec,
    )


def saved_run(repo, paths, home, tmp_path, value: object, **extra) -> ExploreRun:
    return explore(
        repo,
        paths,
        home,
        "explore_answer",
        ORCH_FAKE_ANSWER_FILE=str(answer_file(tmp_path, value)),
        **extra,
    )


@pytest.fixture
def held() -> Iterator[list]:
    """Open lock files held by "other runs"; closed after the test."""
    handles: list = []
    yield handles
    for handle in handles:
        handle.close()


def hold(paths: TargetPaths, eid: str, handles: list) -> Path:
    path = explore_root(paths) / f"{eid}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    handles.append(handle)
    return path


def is_held(path: Path) -> bool:
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


# Constants


def test_the_limits_are_the_designed_values() -> None:
    assert (EXPLORE_TIMEOUT_SEC, QUESTION_LIMIT_BYTES) == (900, 8192)


def test_the_explore_directory_is_under_the_target_runtime(paths: TargetPaths) -> None:
    assert explore_root(paths) == paths.root / "explore"


def test_next_explore_log_numbers_per_question(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    assert next_explore_log(logs, EID) == logs / "explore" / f"{EID}.1.log"
    write(logs / "explore" / f"{EID}.1.log", "")
    write(logs / "explore" / f"{EID}.1.proxy.log", "")
    write(logs / "explore" / "other.4.log", "")
    assert next_explore_log(logs, EID) == logs / "explore" / f"{EID}.2.log"


# Question file


def test_a_question_in_the_explore_directory_is_read(paths: TargetPaths) -> None:
    assert read_question(question_file(paths), paths) == (EID, QUESTION)


@pytest.mark.parametrize(
    "name",
    ["Q1.md", "-q.md", "q_1.md", "q1.txt", "q1.md.md", "a" * 65 + ".md", ".md"],
)
def test_a_question_with_a_bad_name_is_refused(paths: TargetPaths, name: str) -> None:
    path = explore_root(paths) / name
    write(path, QUESTION)
    with pytest.raises(ExploreError):
        read_question(path, paths)


def test_the_longest_id_is_accepted(paths: TargetPaths) -> None:
    eid = "a" + "-" * 62 + "z"
    assert read_question(question_file(paths, eid), paths)[0] == eid


def test_a_question_outside_the_explore_directory_is_refused(
    paths: TargetPaths, tmp_path: Path
) -> None:
    path = tmp_path / f"{EID}.md"
    write(path, QUESTION)
    with pytest.raises(ExploreError):
        read_question(path, paths)
    nested = explore_root(paths) / "sub" / f"{EID}.md"
    write(nested, QUESTION)
    with pytest.raises(ExploreError):
        read_question(nested, paths)


def test_a_question_given_by_a_relative_path_is_read(
    paths: TargetPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    question_file(paths)
    monkeypatch.chdir(explore_root(paths))
    assert read_question(Path(f"{EID}.md"), paths) == (EID, QUESTION)


def test_a_symlinked_question_is_refused(paths: TargetPaths, tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    write(source, QUESTION)
    link = explore_root(paths) / f"{EID}.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(source)
    with pytest.raises(ExploreError):
        read_question(link, paths)


def test_a_symlink_inside_the_explore_directory_is_refused(
    paths: TargetPaths,
) -> None:
    source = question_file(paths, "source")
    link = explore_root(paths) / f"{EID}.md"
    link.symlink_to(source)
    with pytest.raises(ExploreError):
        read_question(link, paths)


def test_a_missing_question_is_refused(paths: TargetPaths) -> None:
    with pytest.raises(ExploreError):
        read_question(explore_root(paths) / f"{EID}.md", paths)


def test_a_directory_named_like_a_question_is_refused(paths: TargetPaths) -> None:
    path = explore_root(paths) / f"{EID}.md"
    path.mkdir(parents=True)
    with pytest.raises(ExploreError):
        read_question(path, paths)


@pytest.mark.parametrize("text", ["", " \n\t\n"])
def test_an_empty_question_is_refused(paths: TargetPaths, text: str) -> None:
    with pytest.raises(ExploreError):
        read_question(question_file(paths, text=text), paths)


def test_the_size_limit_counts_bytes(paths: TargetPaths) -> None:
    exact = "가" * (QUESTION_LIMIT_BYTES // 3) + "a" * (QUESTION_LIMIT_BYTES % 3)
    assert len(exact.encode("utf-8")) == QUESTION_LIMIT_BYTES
    assert read_question(question_file(paths, text=exact), paths)[1] == exact
    with pytest.raises(ExploreError):
        read_question(question_file(paths, text=exact + "a"), paths)


def test_a_question_that_is_not_utf8_is_refused(paths: TargetPaths) -> None:
    path = explore_root(paths) / f"{EID}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe question")
    with pytest.raises(ExploreError):
        read_question(path, paths)


# --dir


def test_the_default_directory_is_the_target_repository(
    repo: Path, paths: TargetPaths
) -> None:
    assert resolve_dir(None, target=make_target(repo), paths=paths) == repo.resolve()


def test_the_target_repository_may_be_named(repo: Path, paths: TargetPaths) -> None:
    named = resolve_dir(repo / "src" / "..", target=make_target(repo), paths=paths)
    assert named == repo.resolve()


def test_a_worktree_of_the_target_is_accepted(repo: Path, paths: TargetPaths) -> None:
    worktree = paths.worktrees / "feat-x"
    worktree.mkdir(parents=True)
    got = resolve_dir(worktree, target=make_target(repo), paths=paths)
    assert got == worktree.resolve()


@pytest.mark.parametrize("where", ["outside", "subdir", "worktrees", "nested", "file"])
def test_other_directories_are_refused(
    repo: Path, paths: TargetPaths, tmp_path: Path, where: str
) -> None:
    candidates = {
        "outside": tmp_path / "elsewhere",
        "subdir": repo / "src",
        "worktrees": paths.worktrees,
        "nested": paths.worktrees / "feat-x" / "src",
        "file": paths.worktrees / "feat-y",
    }
    path = candidates[where]
    if where == "file":
        write(path, "not a directory\n")
    else:
        path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ExploreError):
        resolve_dir(path, target=make_target(repo), paths=paths)


def test_a_missing_worktree_is_refused(repo: Path, paths: TargetPaths) -> None:
    paths.worktrees.mkdir(parents=True)
    with pytest.raises(ExploreError):
        resolve_dir(paths.worktrees / "gone", target=make_target(repo), paths=paths)


def test_a_symlink_into_the_worktrees_is_judged_by_its_real_path(
    repo: Path, paths: TargetPaths, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    paths.worktrees.mkdir(parents=True)
    (paths.worktrees / "sneaky").symlink_to(outside)
    with pytest.raises(ExploreError):
        resolve_dir(paths.worktrees / "sneaky", target=make_target(repo), paths=paths)


# Prompt


def test_the_prompt_carries_the_id_the_question_and_the_format() -> None:
    prompt = explore_prompt(EID, QUESTION)
    assert prompt.startswith(f'Explore id: "{EID}"\n')
    assert QUESTION in prompt
    assert "Do not modify any files." in prompt
    for word in (
        '"explore_id"',
        '"claims"',
        '"statement"',
        '"path"',
        '"line"',
        '"method"',
        '"read"',
        '"grep"',
        '"unresolved"',
        "Korean",
    ):
        assert word in prompt


# Answer checks


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    write(path / "src" / "app.py", "one\ntwo\nthree\n")
    write(path / "src" / "tail.py", "one\ntwo")
    write(path / "empty.txt", "")
    write(path / "docs" / "가이드.md", "첫 줄\n")
    return path


def check(value: object, root: Path, eid: str = EID) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return check_answer(text, explore_id=eid, root=root)


def test_a_valid_answer_is_returned_as_parsed(root: Path) -> None:
    value = answer(
        claims=[
            claim(),
            claim("src/app.py", 3, method="grep"),
            claim("src/tail.py", 2),
            claim("docs/가이드.md", 1),
        ],
        unresolved=["확인하지 못함"],
    )
    assert check(value, root) == value


def test_an_answer_with_no_claims_is_valid(root: Path) -> None:
    value = answer(claims=[], unresolved=["아무것도 찾지 못함"])
    assert check(value, root) == value


@pytest.mark.parametrize(
    "text",
    ["hello", "", "[]", "null", '"text"', '{"explore_id": "q1"} trailing'],
)
def test_text_that_is_not_one_json_object_is_invalid(root: Path, text: str) -> None:
    with pytest.raises(ValueError):
        check(text, root)


def test_an_answer_for_another_question_is_invalid(root: Path) -> None:
    with pytest.raises(ValueError):
        check(answer("q2"), root)


@pytest.mark.parametrize("missing", ["explore_id", "claims", "unresolved"])
def test_a_missing_top_level_field_is_invalid(root: Path, missing: str) -> None:
    value = answer()
    del value[missing]
    with pytest.raises(ValueError):
        check(value, root)


def test_an_extra_top_level_field_is_invalid(root: Path) -> None:
    with pytest.raises(ValueError):
        check({**answer(), "note": "x"}, root)


@pytest.mark.parametrize("missing", ["statement", "path", "line", "method"])
def test_a_claim_missing_a_field_is_invalid(root: Path, missing: str) -> None:
    item = claim()
    del item[missing]
    with pytest.raises(ValueError):
        check(answer(claims=[item]), root)


@pytest.mark.parametrize(
    "item",
    [
        claim(extra="x"),
        claim(method="test"),
        claim(method="READ"),
        claim(line=0),
        claim(line=-1),
        claim(line=True),
        claim(line=1.0),
        claim(line="1"),
        claim(statement=""),
        claim(statement="가" * 601),
        claim(statement=3),
        claim(path=""),
        claim(path=3),
        "not an object",
    ],
)
def test_a_claim_with_a_bad_value_is_invalid(root: Path, item: object) -> None:
    with pytest.raises(ValueError):
        check(answer(claims=[item]), root)


def test_the_length_limits_are_inclusive(root: Path) -> None:
    value = answer(
        claims=[claim(statement="가" * 600)] * 40,
        unresolved=["나" * 300] * 20,
    )
    assert check(value, root) == value


@pytest.mark.parametrize(
    "value",
    [
        answer(claims=[claim()] * 41),
        answer(unresolved=["x"] * 21),
        answer(unresolved=[""]),
        answer(unresolved=["나" * 301]),
        answer(unresolved=[3]),
        {**answer(), "unresolved": "x"},
        {**answer(), "claims": {}},
        {**answer(), "explore_id": 1},
    ],
)
def test_counts_and_unresolved_items_are_checked(root: Path, value: dict) -> None:
    with pytest.raises(ValueError):
        check(value, root)


def test_a_path_longer_than_the_limit_is_invalid(root: Path) -> None:
    name = "d/" * 149 + "ab"
    assert len(name) == 300
    write(root / name, "x\n")
    assert check(answer(claims=[claim(name)]), root)
    longer = "d/" * 149 + "abc"
    write(root / longer, "x\n")
    with pytest.raises(ValueError):
        check(answer(claims=[claim(longer)]), root)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/hosts",
        "../outside.txt",
        "src/../src/app.py",
        "src//app.py",
        "./src/app.py",
        "src/app.py/",
        "src",
        "missing.py",
        "src/missing.py",
    ],
)
def test_a_path_that_is_not_a_file_inside_the_root_is_invalid(
    root: Path, tmp_path: Path, path: str
) -> None:
    write(tmp_path / "outside.txt", "secret\n")
    with pytest.raises(ValueError):
        check(answer(claims=[claim(path)]), root)


def test_a_symlink_that_leaves_the_root_is_invalid(root: Path, tmp_path: Path) -> None:
    write(tmp_path / "outside.txt", "secret\n")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError):
        check(answer(claims=[claim("link.txt")]), root)


def test_a_symlink_that_stays_in_the_root_is_followed(root: Path) -> None:
    (root / "alias.py").symlink_to(root / "src" / "app.py")
    assert check(answer(claims=[claim("alias.py", 3)]), root)


@pytest.mark.parametrize(
    ("path", "line"),
    [("src/app.py", 4), ("src/tail.py", 3), ("empty.txt", 1)],
)
def test_a_line_past_the_end_of_the_file_is_invalid(
    root: Path, path: str, line: int
) -> None:
    with pytest.raises(ValueError):
        check(answer(claims=[claim(path, line)]), root)


def test_lines_are_counted_by_newline_bytes_only(root: Path) -> None:
    write(root / "cr.txt", "a\rb\rc\n")
    assert check(answer(claims=[claim("cr.txt", 1)]), root)
    with pytest.raises(ValueError):
        check(answer(claims=[claim("cr.txt", 2)]), root)
    (root / "bytes.bin").write_bytes(b"\xff\n\xfe\n\x00")
    assert check(answer(claims=[claim("bytes.bin", 3)]), root)


def test_one_bad_claim_makes_the_whole_answer_invalid(root: Path) -> None:
    with pytest.raises(ValueError):
        check(answer(claims=[claim(), claim("missing.py")]), root)


# Slots (ADR 0029)


def test_a_slot_is_a_held_lock_file_with_the_pid(paths: TargetPaths) -> None:
    fd = acquire_slot(paths, EID, 2)
    assert fd is not None
    lock = explore_root(paths) / f"{EID}.lock"
    try:
        assert is_held(lock)
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        release_slot(paths, EID, fd)
    assert not lock.exists()


def test_the_limit_counts_held_locks(paths: TargetPaths, held: list) -> None:
    hold(paths, "a", held)
    hold(paths, "b", held)
    assert acquire_slot(paths, EID, 2) is None
    assert not (explore_root(paths) / f"{EID}.lock").exists()
    fd = acquire_slot(paths, EID, 3)
    assert fd is not None
    release_slot(paths, EID, fd)


def test_the_same_question_cannot_run_twice(paths: TargetPaths, held: list) -> None:
    lock = hold(paths, EID, held)
    assert acquire_slot(paths, EID, 5) is None
    assert lock.exists() and is_held(lock)


def test_stale_lock_files_are_removed_and_not_counted(
    paths: TargetPaths, held: list
) -> None:
    root = explore_root(paths)
    write(root / "dead.lock", "99999\n")
    write(root / f"{EID}.lock", "99998\n")
    hold(paths, "live", held)
    fd = acquire_slot(paths, EID, 2)
    assert fd is not None
    try:
        assert not (root / "dead.lock").exists()
        assert (root / "live.lock").exists()
        assert (root / f"{EID}.lock").read_text(encoding="utf-8").strip() == str(
            os.getpid()
        )
    finally:
        release_slot(paths, EID, fd)


def test_other_files_in_the_explore_directory_are_left_alone(
    paths: TargetPaths,
) -> None:
    root = explore_root(paths)
    for name in ("a.md", "a.json", "notes.txt"):
        write(root / name, "x\n")
    fd = acquire_slot(paths, EID, 1)
    assert fd is not None
    release_slot(paths, EID, fd)
    assert sorted(p.name for p in root.iterdir() if p.name != ".slots.lock") == [
        "a.json",
        "a.md",
        "notes.txt",
    ]


def test_slots_are_taken_one_at_a_time(paths: TargetPaths) -> None:
    results: list[int | None] = []
    barrier = threading.Barrier(6)

    def take(index: int) -> None:
        barrier.wait()
        results.append(acquire_slot(paths, f"t{index}", 2))

    threads = [threading.Thread(target=take, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    taken = [fd for fd in results if fd is not None]
    assert len(taken) == 2
    for fd in taken:
        os.close(fd)


# Runs


def test_a_valid_answer_is_saved(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    value = answer(claims=[claim(), claim("src/app.py", 1)], unresolved=["미확인"])
    result = saved_run(repo, paths, home, tmp_path, value)
    log = paths.logs / "explore" / f"{EID}.1.log"
    assert result == ExploreRun(
        explore_id=EID,
        outcome=ExploreOutcome.SAVED,
        exit_code=0,
        answer_path=explore_root(paths) / f"{EID}.json",
        log_path=log,
        claims=2,
        unresolved=1,
        proxy_denials=0,
    )
    saved = json.loads(result.answer_path.read_text(encoding="utf-8"))
    assert saved == value
    assert log.is_file()
    assert not (explore_root(paths) / f"{EID}.lock").exists()
    assert sorted(p.name for p in explore_root(paths).iterdir()) == [
        ".slots.lock",
        f"{EID}.json",
        f"{EID}.md",
    ]


def test_the_agent_gets_the_sandboxed_command_and_the_prompt(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    args_file = tmp_path / "args.json"
    result = explore(
        repo,
        paths,
        home,
        "review_args",
        ORCH_FAKE_ARGS_FILE=str(args_file),
        GH_TOKEN="secret",
    )
    seen = json.loads(args_file.read_text(encoding="utf-8"))
    argv = seen["argv"]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[argv.index("-o") + 1] == str(paths.logs / "explore" / f"{EID}.1.out")
    assert argv[argv.index("-C") + 1] == str(repo.resolve())
    assert argv[argv.index("-m") + 1] == review.REVIEW_MODEL
    assert f"model_reasoning_effort={review.REVIEW_EFFORT}" in argv
    assert argv[-1] == "-" and "--json" in argv
    assert Path(seen["cwd"]).resolve() == repo.resolve()
    assert "GH_TOKEN" not in seen["env"]
    assert {"TMPDIR", "HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY"} <= set(seen["env"])
    assert seen["stdin"] == explore_prompt(EID, QUESTION)
    # The fake wrote a review, not an answer.
    assert result.outcome is ExploreOutcome.INVALID


def test_a_worktree_can_be_explored(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    worktree = paths.worktrees / "feat-x"
    write(worktree / "only_here.py", "x = 1\n")
    value = answer(claims=[claim("only_here.py", 1)])
    result = saved_run(repo, paths, home, tmp_path, value, dir_arg=worktree)
    assert result.outcome is ExploreOutcome.SAVED


def test_an_answer_about_another_root_is_invalid(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    value = answer(claims=[claim("only_here.py", 1)])
    result = saved_run(repo, paths, home, tmp_path, value)
    assert result.outcome is ExploreOutcome.INVALID


@pytest.mark.parametrize(
    "value",
    ["hello", answer("q2"), answer(claims=[claim("src/app.py", 99)])],
)
def test_an_invalid_answer_is_not_saved(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path, value: object
) -> None:
    result = saved_run(repo, paths, home, tmp_path, value)
    assert (result.outcome, result.exit_code) == (ExploreOutcome.INVALID, 0)
    assert (result.claims, result.unresolved) == (0, 0)
    assert not result.answer_path.exists()


def test_an_undecodable_answer_is_invalid(
    repo: Path, paths: TargetPaths, home: Path
) -> None:
    result = explore(repo, paths, home, "review_bad_bytes")
    assert result.outcome is ExploreOutcome.INVALID


def test_an_answer_without_command_events_is_invalid(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    result = saved_run(repo, paths, home, tmp_path, answer(), ORCH_FAKE_NO_COMMANDS="1")
    assert result.outcome is ExploreOutcome.INVALID
    assert not result.answer_path.exists()


def test_a_failed_run_keeps_the_previous_answer(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    first = saved_run(repo, paths, home, tmp_path, answer(unresolved=["처음"]))
    assert first.outcome is ExploreOutcome.SAVED
    before = first.answer_path.read_text(encoding="utf-8")
    second = saved_run(repo, paths, home, tmp_path, "hello")
    assert second.outcome is ExploreOutcome.INVALID
    assert second.log_path == paths.logs / "explore" / f"{EID}.2.log"
    assert first.answer_path.read_text(encoding="utf-8") == before


def test_a_new_answer_replaces_the_saved_one(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    saved_run(repo, paths, home, tmp_path, answer(unresolved=["처음"]))
    result = saved_run(repo, paths, home, tmp_path, answer(unresolved=["다음"]))
    saved = json.loads(result.answer_path.read_text(encoding="utf-8"))
    assert saved["unresolved"] == ["다음"]
    leftovers = [p.name for p in explore_root(paths).iterdir()]
    assert sorted(leftovers) == [".slots.lock", f"{EID}.json", f"{EID}.md"]


def test_exit_zero_without_a_final_message_is_a_failure(
    repo: Path, paths: TargetPaths, home: Path
) -> None:
    result = explore(repo, paths, home, "review_no_output")
    assert (result.outcome, result.exit_code) == (ExploreOutcome.FAILED, 0)


def test_a_nonzero_exit_is_a_failure(
    repo: Path, paths: TargetPaths, home: Path
) -> None:
    result = explore(repo, paths, home, "review_fail")
    assert (result.outcome, result.exit_code) == (ExploreOutcome.FAILED, 1)
    assert "boom" in result.log_path.read_text(encoding="utf-8")


def test_a_usage_limit_is_quota(repo: Path, paths: TargetPaths, home: Path) -> None:
    result = explore(repo, paths, home, "review_usage_limit")
    assert result.outcome is ExploreOutcome.QUOTA


def test_the_time_limit_stops_the_run(
    repo: Path, paths: TargetPaths, home: Path
) -> None:
    started = time.monotonic()
    result = explore(repo, paths, home, "timeout", timeout_sec=1)
    assert time.monotonic() - started < 20
    assert result.outcome is ExploreOutcome.TIMEOUT
    assert not (explore_root(paths) / f"{EID}.lock").exists()


def test_an_agent_that_cannot_start_is_a_failure(
    repo: Path, paths: TargetPaths, home: Path
) -> None:
    result = run_explore(
        question_file(paths),
        dir_arg=None,
        target=make_target(repo),
        config=make_config(codex_path=home / "missing" / "codex"),
        paths=paths,
        environ={"PATH": "/usr/bin", "HOME": str(home)},
    )
    assert (result.outcome, result.exit_code) == (ExploreOutcome.FAILED, None)
    assert not (explore_root(paths) / f"{EID}.lock").exists()


@pytest.mark.parametrize("mode", ["die", "deny"])
def test_the_proxy_runs_for_the_whole_run(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path, mode: str
) -> None:
    result = explore(
        repo,
        paths,
        home,
        "explore_answer",
        ORCH_FAKE_ANSWER_FILE=str(answer_file(tmp_path, answer())),
        ORCH_FAKE_SLEEP="1",
        ORCH_FAKE_PROXY=mode,
    )
    if mode == "die":
        assert result.outcome is ExploreOutcome.FAILED
        assert not result.answer_path.exists()
    else:
        assert (result.outcome, result.proxy_denials) == (ExploreOutcome.SAVED, 1)
    proxy_log = paths.logs / "explore" / f"{EID}.1.proxy.log"
    assert proxy_log.exists()


def test_a_proxy_that_stops_ends_a_hanging_run_quickly(
    repo: Path, paths: TargetPaths, home: Path
) -> None:
    started = time.monotonic()
    result = explore(repo, paths, home, "timeout", ORCH_FAKE_PROXY="die")
    assert time.monotonic() - started < 20
    assert result.outcome is ExploreOutcome.FAILED


def test_a_run_without_an_absolute_hub_fails_without_starting_codex(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    args_file = tmp_path / "args.json"
    result = explore(
        repo,
        paths,
        home,
        "review_args",
        ORCH_FAKE_ARGS_FILE=str(args_file),
        ORCH_HUB="relative/hub",
    )
    assert (result.outcome, result.exit_code) == (ExploreOutcome.FAILED, None)
    assert not args_file.exists()


def test_the_run_tmpdir_is_removed(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    env_file = tmp_path / "env.json"
    explore(repo, paths, home, "review_env_values", ORCH_FAKE_ENV_FILE=str(env_file))
    seen = json.loads(env_file.read_text(encoding="utf-8"))
    assert seen["tmpdir_is_dir"] is True
    assert not Path(seen["TMPDIR"]).exists()


def test_the_slot_is_held_while_codex_runs(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    box: list[ExploreRun] = []
    runner = threading.Thread(
        target=lambda: box.append(
            explore(
                repo,
                paths,
                home,
                "explore_answer",
                ORCH_FAKE_ANSWER_FILE=str(answer_file(tmp_path, answer())),
                ORCH_FAKE_SLEEP="3",
            )
        )
    )
    runner.start()
    lock = explore_root(paths) / f"{EID}.lock"
    deadline = time.monotonic() + 10
    while not (paths.logs / "explore" / f"{EID}.1.log").exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    try:
        assert is_held(lock)
        assert acquire_slot(paths, "other", 1) is None
    finally:
        runner.join()
    assert box[0].outcome is ExploreOutcome.SAVED
    assert not lock.exists()


def test_a_busy_run_starts_nothing_and_writes_no_log(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path, held: list
) -> None:
    hold(paths, "a", held)
    hold(paths, "b", held)
    args_file = tmp_path / "args.json"
    result = explore(
        repo, paths, home, "review_args", ORCH_FAKE_ARGS_FILE=str(args_file)
    )
    assert result == ExploreRun(
        explore_id=EID,
        outcome=ExploreOutcome.BUSY,
        exit_code=None,
        answer_path=explore_root(paths) / f"{EID}.json",
        log_path=None,
        claims=0,
        unresolved=0,
        proxy_denials=0,
    )
    assert not args_file.exists()
    assert not (paths.logs / "explore").exists()


def test_the_limit_comes_from_the_config(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path, held: list
) -> None:
    hold(paths, "a", held)
    result = explore(repo, paths, home, "review_no_output", lane_a=1)
    assert result.outcome is ExploreOutcome.BUSY
    result = explore(repo, paths, home, "review_no_output", lane_a=2)
    assert result.outcome is ExploreOutcome.FAILED


def test_refusals_happen_before_any_slot_or_log(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    bad = explore_root(paths) / "Bad.md"
    write(bad, QUESTION)
    with pytest.raises(ExploreError):
        explore(repo, paths, home, "review_no_output", question=bad)
    with pytest.raises(ExploreError):
        explore(repo, paths, home, "review_no_output", dir_arg=tmp_path / "elsewhere")
    assert not (paths.logs / "explore").exists()
    assert sorted(p.name for p in explore_root(paths).iterdir()) == [
        "Bad.md",
        f"{EID}.md",
    ]


def test_a_proxy_that_cannot_start_fails_without_starting_codex(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    args_file = tmp_path / "args.json"
    result = explore(
        repo,
        paths,
        home,
        "review_args",
        ORCH_FAKE_ARGS_FILE=str(args_file),
        ORCH_FAKE_PROXY="broken",
    )
    assert (result.outcome, result.exit_code) == (ExploreOutcome.FAILED, None)
    assert not args_file.exists()
    assert not (explore_root(paths) / f"{EID}.lock").exists()


def test_the_report_holds_no_answer_text(
    repo: Path, paths: TargetPaths, home: Path, tmp_path: Path
) -> None:
    value = answer(claims=[claim(statement="SECRET-STATEMENT")], unresolved=["X"])
    result = saved_run(repo, paths, home, tmp_path, value)
    assert "SECRET-STATEMENT" not in repr(dataclasses.asdict(result))
