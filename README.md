# orchestrator

English | [한국어](README.ko.md)

`orch` is a global CLI through which a Claude Code main agent decomposes work,
runs Codex or Claude in isolated Git worktrees, and delivers pull requests after
tests, mechanical gates, and review. This repository is the hub for multiple target
repositories. The Python runtime uses only the standard library. Agents do not merge PRs.

## Capabilities

- Task specifications with TOML front matter and a Markdown body.
- Scheduling with task dependencies, path locks, execution slots, and reservation expiry.
- Per-run logs and result reports, timeouts, usage-limit detection, retries, and recovery.
- Scope (G1), test (G2), and lint (G3) gates, cross-review, PR updates, and merge waiting.
- SQLite execution records, generated TODO views, and tmux main sessions and status panels.
- macOS sandbox verification, hooks and command guards, redaction, doctor, and launchd services.

**Implementation baseline:** `1c191614` (2026-11-18); declared package version `0.15.0`.
GitHub tracking storage, synchronization engine, and CLI are implemented;
service wiring remains. The web workspace and priority-based automatic
scheduling are not implemented. Check the installed binary separately from this source baseline.

## Documentation

The [documentation index](docs/README.md) also links the design sources and ADRs.

| Topic | Document |
| --- | --- |
| Implemented functionality and commands | [Features and CLI](docs/reference/features.md) |
| Components and execution flow | [Architecture](docs/reference/architecture.md) |
| Prerequisites, configuration, storage, services, recovery | [Runtime and operations](docs/operations/runtime.md) |
| Project selection and dependencies | [Work priority](docs/operations/work-priority.md) |
| Delivery phases and prerequisites | [Build procedure (Korean)](docs/design/구축%20절차.md) |
| Requirements and decisions | [Requirements (Korean)](docs/design/요건%20정의.md), [ADRs](docs/decisions/README.md) |

## Getting started

The operational environment requires macOS, Python 3.14+, uv, Git, GitHub CLI,
tmux, and authenticated agent binaries. GitHub operations use the designated App bot.
Read [runtime setup](docs/operations/runtime.md) before registering target repositories
and binary paths. Install an approved version, set `ORCH_HUB` to the hub's absolute
path in your shell, and start the main session with `orch open <target>`.

Use the [work plan](templates/work-plan.md) and [task specification](templates/task.md)
templates. The main agent checks priorities and prerequisites and dispatches small
approved bundles. Use `orch --help` and `orch --version` to inspect the installed CLI.

## Development and verification

Prepare the development environment with `uv sync --frozen`. Test and lint tools are
development dependencies. Follow the harness checks in [CLAUDE.md](CLAUDE.md) and the
[main procedure](config/main.md). Full tests and lint run through the installed
`orch sandbox run`; the two designated files that start a real sandbox are checked separately.

`src/orch/` contains execution code, `tests/` verification, `schemas/` output contracts,
`config/` injected policy, and `templates/` specification formats. Never commit runtime
databases, logs, or secrets. Policy changes take effect in a new main session opened
from the updated, merged hub; code changes require an approved installation.
