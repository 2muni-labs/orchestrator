# Implemented features and CLI

English | [한국어](features.ko.md)

Baseline: `b757a619`, 2026-11-18. Implementation lives in [src/orch](../../src/orch);
the authoritative command list is `_parser()` in [cli.py](../../src/orch/cli.py).
This describes source implementation, not the current machine's installed or enabled features.

## Execution pipeline

| Feature | Implemented behavior | Main source |
| --- | --- | --- |
| Target registration | Repository, default branch, setup/test/lint, shared paths | config.py |
| Specification parsing | Validate TOML fields, paths, lanes, models; retain Markdown body | taskfile.py |
| Preparation | Create worktree from default branch, run setup, reject shared/test modify scope | lifecycle.py |
| Reservation | Check predecessor completion, glob conflicts, concurrency, pauses; enforce TTL | scheduler.py |
| Execution | Lane-specific argv/env, logs, process lifetime/timeouts, result collection | runner.py, lanes/ |
| Results | Validate JSON reports; fail missing or malformed reports | results.py |
| Gates | G1 scope/frozen tests, G2 tests, G3 lint; commit after passing | gate.py |
| Review | Validate/save reviews, triage, Codex review of Claude implementations | review.py, cli.py |
| Pull requests | Incorporate default branch, verify, push/open PR; record conflicts/failures | prflow.py, github.py |
| Merge waiting | Observe PR, CI, review state and return the next required action | waiter.py |
| Recovery/cleanup | close/discard/retry/resume/kill and process/worktree management | lifecycle.py, gc.py |

L3/L3R tasks and explicit review flags require review. Check safety-path policy and
code-level gate conditions separately. Creating a PR is neither approval nor merge;
merging belongs to the user or the designated verification workflow on the user's account.

## Available commands

| Command | Purpose |
| --- | --- |
| `orch --version`, `orch --help` | Package version and help |
| `orch target add PATH --name ... --github ... --setup ... --test ... --lint ...` | Register target |
| `orch open NAME [--no-attach]` | Main tmux session with injected policy and panels |
| `orch prepare SPEC` | Prepare specification and worktree |
| `orch dispatch ID`, `orch next` | Reserve one task / reconsider queued tasks |
| `orch run ID`, `orch gate ID` | Agent execution / mechanical gates |
| `orch review ID --by codex` | Run Codex cross-review |
| `orch review save ID [--triage FILE]` | Save review JSON from standard input |
| `orch pr ID [--resolution TEXT]`, `orch update ID` | Publish/update PR / incorporate default branch |
| `orch wait ID --merged [--interval N] [--max-wait N]` | Wait for merge/check/review outcomes |
| `orch close ID`, `orch discard ID` | Confirm completion and clean up / discard |
| `orch retry ID`, `orch resume ID [--keep]`, `orch kill ID` | Retry failure / recover blocked task / stop |
| `orch status [--human]`, `orch watch [--once] [--panels]` | Status output and panels |
| `orch doctor`, `orch gc [--dry-run]`, `orch notify test` | Health, retention cleanup, notification test |
| `orch service install/uninstall/restore` | Register/remove launchd services or restore sessions |
| `orch sandbox run --dir PATH -- COMMAND ...` | Execute verification under the gate profile |
| `orch hook NAME` | Internal entry point for injected hooks |

Most commands return JSON. State-changing commands can refuse an action based on the
current state. Operations such as resume/kill retain the main policy's approval
boundaries. Do not treat design-only commands absent from help as available CLI features.

## Partial implementation: GitHub work tracking

[tracking.py](../../src/orch/tracking.py) validates/imports bindings into the DB and
manages project/record/task/alias relationships and synchronization state.
[tracksync.py](../../src/orch/tracksync.py) implements locking, desired-state computation,
dry runs, remote writes, verification, and failure recording.
[github.py](../../src/orch/github.py) contains issue and Projects operations.

- Task ownership: prepared → Todo; blocked/conflict/orphaned → Blocked;
  closed → Done; discarded → Cancelled; other states → In Progress.
- Harness ownership: derive state from PR OPEN/MERGED/CLOSED.
- Aggregate/manual ownership: require acceptance; one completed task does not close the aggregate.
- Editing GitHub state does not dispatch or merge local tasks.

The baseline CLI parser has no `track` command and service has no `orch.track` job.
An implemented engine does not establish that continuous synchronization is running.
Check #495–498 for CLI, service, release, and procedure integration.

## Unimplemented or design-stage features

| Item | Boundary at this baseline |
| --- | --- |
| Work priority | Main policy and Markdown plans; no scheduler/DB priority field |
| milestone / replaces | Future fields described in template comments; not accepted by current parser |
| events/show/roadmap/versions/selfcheck/explore | Designs/some schemas exist; commands absent from current CLI |
| Web workspace | No server/PWA/web terminal/shared layout/device input ownership implementation |
| Dedicated tmux socket/network allowlist proxy | Distinguish future hardening/workspace design from current adapters |
| Automatic agent version switching | Pins record measurement baselines, not completed automated checks/installations |

This inventory does not reduce the requirements. Track planned work through the
[build procedure](../design/구축%20절차.md) and its issues.
