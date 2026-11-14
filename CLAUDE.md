@AGENTS.md

# Hub rules for the main agent

`orch open` injects the main agent rules for every target from `config/main.md`. This
file adds only the rules for a session whose target is the hub itself. A rule that
applies to every target goes in `config/main.md`, not here. `R` below stands for
`~/.orch/orchestrator`.

## Escape hatch session

- An escape hatch session is `claude` started in the hub without `orch open`. It gets
  no hooks, no injected main rules and no injected agent definitions. Use one only to
  repair broken hooks or a broken `orch open`, and only when the user says this is
  one. It skips the `orch open` check at session start.
- `.claude/settings.local.json` in the hub clone, ignored by git, sets `ORCH_HUB` and
  `ORCH_TARGET` only so that an escape hatch session can still run `orch`. The user
  settings (`~/.claude/settings.json`) set `permissions.additionalDirectories` to
  `["~/.orch/orchestrator"]`, so it can also read `R`.

## Merge policy

- The lint column of the merge table is ruff: `ruff check` and `ruff format --check`.
- A harness pull request gets no `user_check` and no `needs_review` notification,
  because it has no task. Check its changed files against the hub target's
  `safety_paths` in `targets.toml` yourself and mark them in the table. The list
  includes `targets.toml` itself, so a change to it is always a user check.
- The `main` ruleset requires one approval and the bot cannot approve its own pull
  request, so the bot cannot merge one without an approval. This does not stop a
  merge after an approval, so the merge policy still applies.

## Harness pull requests

- `orch` refuses a `modify` that reaches a shared file, so a pull request changing one
  is a harness pull request you write yourself, outside `orch`: create the worktree
  with `git worktree add R/worktrees/<id> -b task/<id> origin/main`, run
  `uv sync --frozen`, push with `git push -u origin task/<id>`, and open it with `gh`.
  It has no task, so `orch wait` does not apply: learn of the merge from the user.
  After the merge, remove the worktree and local branch. If it conflicts with `main`,
  merge `origin/main` into the branch and push normally.
- A harness pull request must also pass before you push: in its worktree, run the
  sandboxed checks below on the final commit. This covers every push to that branch.
  Put the results in the pull request body.

## Sandboxed checks in the hub

The sandboxed checks in `config/main.md` take this form in the hub:

1. Run `uv sync --frozen` in the worktree first, outside the sandbox. The profile
   denies writes under `.venv`.
2. `orch sandbox run --dir <worktree> -- .venv/bin/python -m pytest -rs`, then the
   same with `.venv/bin/python -m ruff check .` and
   `.venv/bin/python -m ruff format --check .`. Each must print `ok` true.
3. Read the pytest log at `log_path`. Every `SKIPPED` line must name
   `tests/test_sandbox_real.py` or `tests/integration/test_sandbox_gate.py` with the
   reason `already inside a sandbox, where sandbox-exec cannot start`. Any other skip
   is a failure.
4. Then run exactly those two files outside the sandbox in the same worktree,
   `.venv/bin/python -m pytest -rs tests/test_sandbox_real.py
   tests/integration/test_sandbox_gate.py`, and confirm 0 skipped. They start the
   real `sandbox-exec`, which cannot start inside a sandbox.

Report both counts. Only those two files run outside, because only they need to. The
remaining risk: they run the branch's `sandbox.py`, `gate.py` and what those import
outside the sandbox, before that code merges.

## Releases and the installed orch

- After step 9, update the hub clone's `main` to `origin/main`. `orch open` reads the
  main rules and agent definitions from the hub clone.
- Changed code runs only after a merge, a new tag and an upgrade the user approves:
  confirm that `orch status` shows no running task, back up `~/.orch/state.db` with
  `sqlite3 ~/.orch/state.db ".backup <file>"`, then `uv tool install --reinstall` the
  new tag. A plain `cp` misses the write-ahead log and leaves a stale copy.
- The user creates a version tag only after the version pull request merges, on the
  resulting `main` commit. Never ask for a tag before that merge.
- `orch run` runs the agents by the absolute paths in `~/.orch/config`, not through
  `PATH` or a mise shim, because a shim follows the current directory's mise
  settings. The usage limit check reads only the current run's log.

## Tests in the hub

- Write `verify` as `.venv/bin/python -m pytest ...`.
- The `orch` fixture in `tests/test_cli.py` writes back what it reads from `capsys`
  (`sys.stderr.write(captured.err)`), so a test can still assert on the capture
  (m-notify-cli).
- Call state-changing `orch` commands in tests through the `orch` fixture in
  `tests/test_cli.py` or the integration `Sandbox`. Both check the transition with
  `models.can_transition` before the call (`tests/transitions.py`). Mark a call that
  expects a refusal, or whose command is replaced with a fake, `unchecked=True`. In a
  test with several calls, make every call before asserting on behavior the stubs
  lack, so that an early failure cannot hide a later call from the check.
- Run integration tests in the integration `Sandbox` with frozen commit dates. One
  test that expected two commits to have different ids passed 20 of 20 runs on one
  machine and failed on a faster one.
- An external tool's behavior goes into a spec only as measured on the pinned version
  (m14-cli: only exit 2 or a deny output blocks a PreToolUse hook).

## guard false-positive check

- `scripts/guard_regress.py` checks guard for false positives against your local
  transcripts. Run a branch's guard with the worktree's `.venv/bin/python`, and the
  installed guard with the installed orch tool's Python from the hub clone.
- Only the number summary (total, unique, allow, deny, ask, false positives, deny
  count per reason) leaves this machine. Never put `--show` output in a commit
  message, pull request body or report.
