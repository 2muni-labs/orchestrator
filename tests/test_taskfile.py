from pathlib import Path

import pytest

from orch.models import Effort, Grade, Lane
from orch.taskfile import TaskFileError, TaskSpec, check_modify, load_task, parse_task

BODY = "\n# Purpose\n\n본문 +++ 그대로\n"

FRONT = {
    "id": '"feat-login"',
    "target": '"sample-app"',
    "grade": '"L2"',
    "lane": '"B"',
    "model": '"gpt-6.1-sol"',
    "effort": '"medium"',
    "timeout_min": "45",
    "modify": '["src/auth/**"]',
    "read": '["src/**", "docs/auth.md"]',
    "verify": '".venv/bin/python -m pytest tests/test_auth.py"',
    "depends_on": "[]",
}


def spec_text(body: str = BODY, drop: tuple[str, ...] = (), **changes: str) -> str:
    fields = {**FRONT, **changes}
    lines = [f"{key} = {value}" for key, value in fields.items() if key not in drop]
    return "+++\n" + "\n".join(lines) + "\n+++\n" + body


# Parsing


def test_parse_task_returns_every_field() -> None:
    assert parse_task(spec_text()) == TaskSpec(
        id="feat-login",
        target="sample-app",
        grade=Grade.L2,
        lane=Lane.B,
        model="gpt-6.1-sol",
        effort=Effort.MEDIUM,
        timeout_min=45,
        modify=("src/auth/**",),
        read=("src/**", "docs/auth.md"),
        verify=".venv/bin/python -m pytest tests/test_auth.py",
        depends_on=(),
        tests=None,
        body=BODY,
    )


def test_review_defaults_to_false() -> None:
    assert parse_task(spec_text()).review is False


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_review_accepts_a_boolean(value: str, expected: bool) -> None:
    assert parse_task(spec_text(review=value)).review is expected


def test_review_false_is_accepted_on_a_grade_that_always_reviews() -> None:
    text = spec_text(grade='"L3"', effort='"high"', review="false")
    assert parse_task(text).review is False


@pytest.mark.parametrize("value", ['"true"', "1", "0", "[]"])
def test_review_must_be_a_boolean(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(review=value))


def test_body_is_kept_exactly_and_may_be_empty() -> None:
    assert parse_task(spec_text(body="")).body == ""
    assert parse_task(spec_text(body="+++\nx\r\n")).body == "+++\nx\r\n"


def test_front_matter_comments_and_surrounding_spaces_are_accepted() -> None:
    text = spec_text().replace("+++\n", "+++  \n", 1).replace('"B"', '"B"  # lane')
    assert parse_task(text).lane is Lane.B


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no front matter",
        "\n" + spec_text(),
        "+++\nid = 1\n",
        "+++\nnot toml [\n+++\n",
    ],
)
def test_missing_or_malformed_front_matter_raises(text: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(text)


@pytest.mark.parametrize(
    "key",
    [
        "id",
        "target",
        "grade",
        "lane",
        "model",
        "timeout_min",
        "modify",
        "read",
        "verify",
    ],
)
def test_required_keys(key: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(drop=(key,)))


def test_depends_on_is_required() -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(drop=("depends_on",)))


def test_unknown_key_raises() -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(modfy='["a"]'))


# Values


@pytest.mark.parametrize(
    "task_id",
    [
        '""',
        "1",
        '"../x"',
        '"a/b"',
        '".x"',
        '"-x"',
        '"a b"',
        '"a..b"',
        '"a."',
        '"a.lock"',
        '"이름"',
    ],
)
def test_invalid_ids_raise(task_id: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(id=task_id))


@pytest.mark.parametrize("target", ['"bin"', '"../x"', '""', "1", '"State.DB"'])
def test_invalid_targets_raise_task_file_error(target: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(target=target))


@pytest.mark.parametrize("grade", ['"L4"', '"l2"', "2"])
def test_invalid_grades_raise(grade: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(grade=grade))


@pytest.mark.parametrize("lane", ['"A"', '"b"', "1"])
def test_invalid_lanes_raise(lane: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(lane=lane))


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", '"45"'])
def test_timeout_must_be_a_positive_integer(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(timeout_min=value))


@pytest.mark.parametrize("value", ["[]", '"src/a.py"', "[1]", '[""]'])
def test_modify_must_be_a_non_empty_list_of_strings(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(modify=value))


@pytest.mark.parametrize("value", ["[]", '"src/a.py"', "[1]", '[""]'])
def test_read_must_be_a_non_empty_list_of_strings(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(read=value))


@pytest.mark.parametrize("key", ["modify", "read"])
@pytest.mark.parametrize(
    "value", ['["/abs"]', '["../x"]', '["a/../b"]', '["a\\\\b"]', '["~/x"]', '["a//b"]']
)
def test_paths_must_stay_inside_the_repository(key: str, value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(**{key: value}))


def test_read_accepts_spaces_non_ascii_and_test_paths() -> None:
    value = '["docs/design/기술 설계.md", "tests/**", "src/orch/**"]'
    assert parse_task(spec_text(read=value)).read == (
        "docs/design/기술 설계.md",
        "tests/**",
        "src/orch/**",
    )


@pytest.mark.parametrize("value", ['""', '"  "', "1"])
def test_verify_must_be_a_non_blank_string(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(verify=value))


def test_depends_on_keeps_order() -> None:
    assert parse_task(spec_text(depends_on='["m1", "m2"]')).depends_on == ("m1", "m2")


@pytest.mark.parametrize("value", ['"m1"', "[1]", '["../x"]', '["feat-login"]'])
def test_invalid_depends_on_raises(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(depends_on=value))


def test_tests_none_is_allowed_on_l1() -> None:
    text = spec_text(grade='"L1"', effort='"low"', tests='"none"')
    assert parse_task(text).tests == "none"


@pytest.mark.parametrize(
    "changes",
    [
        {"tests": '"none"'},
        {"grade": '"L1"', "effort": '"low"', "tests": '"some"'},
        {"grade": '"L1"', "effort": '"low"', "tests": "1"},
    ],
)
def test_tests_is_only_none_and_only_on_l1(changes: dict[str, str]) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(**changes))


# Mapping table


@pytest.mark.parametrize(
    ("grade", "effort"),
    [("L1", "low"), ("L2", "medium"), ("L3", "high"), ("L3R", "xhigh")],
)
@pytest.mark.parametrize("lane", ["B", "D"])
def test_codex_mapping_accepts_the_table(lane: str, grade: str, effort: str) -> None:
    spec = parse_task(
        spec_text(lane=f'"{lane}"', grade=f'"{grade}"', effort=f'"{effort}"')
    )
    assert spec.effort == Effort(effort)


@pytest.mark.parametrize(
    ("grade", "effort"),
    [
        ("L1", "medium"),
        ("L2", "low"),
        ("L2", "high"),
        ("L3", "medium"),
        ("L3R", "high"),
    ],
)
def test_codex_mapping_rejects_other_efforts(grade: str, effort: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(grade=f'"{grade}"', effort=f'"{effort}"'))


def test_codex_requires_an_effort() -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(drop=("effort",)))


@pytest.mark.parametrize("value", ['"max"', '"ultra"', "1"])
def test_unknown_effort_raises(value: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(spec_text(effort=value))


@pytest.mark.parametrize(
    ("grade", "model"),
    [
        ("L1", "claude-haiku-5-5"),
        ("L2", "claude-sonnet-5-5"),
        ("L3R", "claude-opus-5-5"),
    ],
)
@pytest.mark.parametrize("lane", ["C", "D"])
def test_claude_mapping_without_effort(lane: str, grade: str, model: str) -> None:
    spec = parse_task(
        spec_text(
            lane=f'"{lane}"', grade=f'"{grade}"', model=f'"{model}"', drop=("effort",)
        )
    )
    assert spec.effort is None


def test_claude_l3_requires_high_effort() -> None:
    changes = {"lane": '"C"', "grade": '"L3"', "model": '"claude-sonnet-5-5"'}
    assert parse_task(spec_text(effort='"high"', **changes)).effort is Effort.HIGH
    with pytest.raises(TaskFileError):
        parse_task(spec_text(drop=("effort",), **changes))
    with pytest.raises(TaskFileError):
        parse_task(spec_text(effort='"medium"', **changes))


def test_claude_effort_is_rejected_where_the_table_has_none() -> None:
    with pytest.raises(TaskFileError):
        parse_task(
            spec_text(lane='"C"', model='"claude-sonnet-5-5"', effort='"medium"')
        )


@pytest.mark.parametrize(
    ("lane", "grade", "model"),
    [
        ("B", "L2", "claude-sonnet-5-5"),
        ("B", "L2", "gpt-6-astra"),
        ("C", "L2", "gpt-6.1-sol"),
        ("C", "L1", "claude-sonnet-5-5"),
        ("C", "L2", "claude-opus-5-5"),
        ("C", "L3R", "claude-sonnet-5-5"),
        ("C", "L2", "claude-fable-5-1"),
        ("D", "L2", "claude-opus-5-5"),
    ],
)
def test_models_outside_the_table_raise(lane: str, grade: str, model: str) -> None:
    drop = ("effort",) if model.startswith("claude") else ()
    with pytest.raises(TaskFileError):
        parse_task(
            spec_text(
                lane=f'"{lane}"', grade=f'"{grade}"', model=f'"{model}"', drop=drop
            )
        )


# load_task


def test_load_task_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "feat-login.md"
    path.write_text(spec_text(), encoding="utf-8")
    assert load_task(path).id == "feat-login"


def test_load_task_requires_the_stem_to_equal_the_id(tmp_path: Path) -> None:
    path = tmp_path / "other.md"
    path.write_text(spec_text(), encoding="utf-8")
    with pytest.raises(TaskFileError):
        load_task(path)


def test_load_task_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(TaskFileError):
        load_task(tmp_path / "missing.md")


def test_load_task_invalid_utf8_raises(tmp_path: Path) -> None:
    path = tmp_path / "feat-login.md"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(TaskFileError):
        load_task(path)


# check_modify

SHARED = ("uv.lock", "migrations/", ".claude/**")


@pytest.mark.parametrize(
    "glob",
    [
        "src/orch/config.py",
        "src/**",
        "src/*/x.py",
        "src/**/x.py",
        "src/tests/x.py",
        "src/uv.lock",
        "tests_helpers/x.py",
        "migrations2/x.sql",
        ".claude2/x",
        "*.py",
        "README.md",
    ],
)
def test_allowed_modify_globs(glob: str) -> None:
    check_modify([glob], SHARED)


@pytest.mark.parametrize(
    "glob",
    [
        "tests/test_a.py",
        "tests/**",
        "tests/sub/*.py",
        "tests",
        "Tests/test_a.py",
        "uv.lock",
        "UV.LOCK",
        "*.lock",
        "*",
        "migrations",
        "migrations/001.sql",
        "migrations/**",
        ".claude",
        ".claude/agents/x.md",
        ".CLAUDE/x",
        "**",
        "**/*.py",
        "*/x.py",
        "te*/x.py",
        "?ests/x.py",
    ],
)
def test_refused_modify_globs(glob: str) -> None:
    with pytest.raises(TaskFileError):
        check_modify(["src/ok.py", glob], SHARED)


def test_custom_test_paths() -> None:
    check_modify(["tests/x.py"], (), test_paths=("spec/",))
    with pytest.raises(TaskFileError):
        check_modify(["spec/x.py"], (), test_paths=("spec/",))


def test_error_names_the_offending_glob() -> None:
    with pytest.raises(TaskFileError, match="uv.lock"):
        check_modify(["uv.lock"], SHARED)
