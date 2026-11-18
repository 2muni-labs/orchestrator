# Backend runtime and operations

English | [한국어](runtime.ko.md)

Baseline: `1c191614`, 2026-11-18. This describes the environment implemented by the
repository, not a verification of this host's installation, authentication, or active services.

## Technology

| Component | Current role |
| --- | --- |
| Python >=3.14 | CLI and application logic; no external Python runtime dependencies |
| uv / uv_build | Locked environment setup and package builds |
| sqlite3 | Local state, WAL, migrations, transactions |
| Git / gh | Worktrees, change checks, PR/CI/Projects APIs |
| Claude Code / Codex CLI | Main session and task/review execution |
| tmux | Per-target sessions/panels and pane lifetime checks |
| macOS sandbox-exec | OS isolation for gate verification commands |
| launchd | Session restoration, periodic doctor and GC |
| Optional ntfy | Notifications; default is none |
| pytest / ruff / jsonschema | Development/test dependencies |

The operational path depends on macOS sandbox-exec and launchd. Meeting the Python
version requirement does not establish equivalent Linux/Windows operational support.
`config/pins.toml` records measured baselines: Claude 2.1.319, Codex 0.160.0, tmux 3.8,
macOS 15.8.1. These are neither latest-version recommendations nor installed-version
claims. Re-measure the listed blocking/warning items when changing external tools.

## Configuration and directories

`ORCH_HUB` is the absolute hub path containing policy and targets.toml. `ORCH_TARGET`
identifies the main session's target. Runtime defaults to `~/.orch`; custom `ORCH_HOME`
follows config path rules. Never copy real settings, keys, or notification topics into public docs.

| Path | Contents |
| --- | --- |
| hub/targets.toml | Target clone/repository, default branch, setup/test/lint, shared/safety paths |
| ~/.orch/config | Terminal, limits, absolute agent paths, notifications, doctor, gate settings |
| ~/.orch/state.db | Cross-target task/run/lock/event/tracking state |
| ~/.orch/<target>/tasks | Confirmed task specifications |
| results / reviews / logs | Results, reviews/triage/diffs, per-run logs |
| worktrees | Per-task Git worktrees |
| TODO.md / terminal.json | Generated state view / owned pane information |
| plans | Main-owned plans, priorities, external dependencies |
| github-tracking.json | Tracking import input; distinguish reviewed seed from runtime copy |
| ~/.orch/service | Service logs |

[config.py](../../src/orch/config.py) defaults to total=3, lane_b=2, lane_a=2,
10-minute reservation TTL, 30-day retention, and 10GB warning/2GB failure disk thresholds.
The main policy's recommended concurrency (1–2) differs from code limits. The planned
B-lane limit of 3 is not the installed default. Configure absolute agent paths to avoid
current-directory shim behavior.

## Installation and startup

1. Check the approved release/tag and host prerequisites. A source version string
   does not prove deployment.
2. Install the approved version with `uv tool install`; check `orch --version` and
   `orch --help`. Replacing an operational version follows the upgrade procedure below.
3. Register the target path/repository and setup/test/lint commands through hub
   targets.toml or `orch target add`. Tests must work without network access.
4. Configure absolute Claude/Codex binary paths and necessary local settings. Prepare
   the designated App bot authentication; never put tokens in tasks, logs, or docs.
5. Run `orch open <target>` from a shell with `ORCH_HUB` set. The launcher injects
   config/main.md, settings, and agent definitions.
6. Inspect `orch status` and `orch doctor`. Doctor can record checks, pause dispatch,
   clean up, or stop processes; it is not a read-only inspection command.
7. Register launchd with `orch service install` if needed and verify actual behavior
   after reboot/login. Successful registration does not satisfy reboot acceptance.

## Services and observation

Current [service.py](../../src/orch/service.py) creates three services.

| Label | Schedule | Command |
| --- | --- | --- |
| orch.session | RunAtLoad | orch service restore |
| orch.doctor | Every 600 seconds | orch doctor |
| orch.gc | Daily at 04:00 | orch gc |

Launchd receives absolute executable paths, a restricted PATH, and ORCH_HUB. There are
no orch.track, orch.workspace, or orch.fulltest services at this baseline. Doctor checks
disk, key tools/connections, and main-pane health; selected failures pause dispatch.
Watch/TODO are observation views, not editable recovery controls.

## Verification and upgrades

Prepare the environment outside the sandbox with `uv sync --frozen`; do not install
into `.venv` inside the sandbox. Hub changes use the installed orch for full verification:

```text
orch sandbox run --dir <worktree> -- .venv/bin/python -m pytest -rs
orch sandbox run --dir <worktree> -- .venv/bin/python -m ruff check .
orch sandbox run --dir <worktree> -- .venv/bin/python -m ruff format --check .
```

Inspect each JSON's ok and log_path. Only skips in `tests/test_sandbox_real.py` and
`tests/integration/test_sandbox_gate.py` with reason
`already inside a sandbox, where sandbox-exec cannot start` are permitted. Run exactly
those two files outside the sandbox in the same worktree and confirm zero skips.
Never replace a failed sandboxed command with an unrestricted run to make it pass.

Before upgrading, confirm there are no running tasks and back up `~/.orch/state.db`
with SQLite `.backup`. Copying only the DB file can omit recent WAL changes. Reinstall
the user-approved tag and check state, commands, and services. Older binaries may reject
a newer DB schema: rollback planning must cover a compatible DB and any execution
records created since the backup, as well as the binary. Policy changes require an
updated hub and a new `orch open` session.

## Incident investigation and records

Use task id and run number to correlate state, result JSON, review, gates, logs, and
PR head. Share only short summaries and verified links externally. Distinguish auth
failures, quota, test failures, and report_missing. Recover blocked/orphaned tasks under
the policy's approval boundaries; never repair state by manually changing DB rows.

When GitHub lags, first inspect local execution evidence. The CLI now exists in source, but do not suggest `orch track`
as available on an installed version that predates its integration. Check installed
support and record unapplied work as pending sync in the plan.
