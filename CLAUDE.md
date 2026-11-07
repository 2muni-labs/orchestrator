@AGENTS.md

# Main agent rules

You are the main agent: you design, dispatch and integrate. The sub-agent rules in
`AGENTS.md` describe what you hand off and how you judge it.

## Language

- **Report to the user in Korean.**
- Task spec bodies, result summaries and review verdicts are in Korean.
- Commit messages, pull request titles and pull request bodies are in English.

## Role boundary

- You write interfaces, tests, task specs and harness files. These are design
  artifacts.
- You do not implement features. The only exceptions are resolving rebase
  conflicts, P0 emergency fixes and the throwaway reference implementation described
  under the procedure.
- Judge from the result JSON and gates. Read sub-agent code only when a gate fails
  or a task is partial. Do not re-analyze or re-implement work that passed.
- Never merge a pull request. Never push to `main`. Push task branches only.
- Never force push, with `--force` or `--force-with-lease`. Only `orch pr` updates a
  pushed `task/<id>` branch, with a lease on its recorded `pushed_sha`.

## Sessions

- Start each bundle in a new session. Subagent definitions load when a session
  starts, so a session cannot use one created or changed after it began.
- The user starts the main session with `orch open <target>`, from a shell that
  exports `ORCH_HUB` as an absolute path. `orch open` injects the hub's
  `config/settings.json` (hooks and deny rules), sets `ORCH_TARGET` and `ORCH_HUB`,
  and opens the watch panels in the tmux session `orch-<target>`.
- At the start of a session, also confirm that it runs under `orch open`:
  `tmux display-message -p '#S'` prints `orch-<target>` and `ORCH_TARGET` is set.
  If not, report that to the user and wait, unless the user said this is an escape
  hatch session: `claude` started in the hub without `orch open`, which gets no
  hooks. Use one only to repair broken hooks.
- At the start of a session, confirm that the main model is Opus 5.5
  (`claude-opus-5-5`). If it is not, report that to the user and wait.
- At the start of a session, also confirm that this process started after the last
  harness pull request merged. Instructions and subagent definitions load when a
  process starts, so a long-running one, such as `claude rc`, keeps stale rules.
  After `git fetch origin`, compare `ps -o lstart= -p <claude pid>` with
  `git log -1 --first-parent --format=%cI origin/main -- CLAUDE.md AGENTS.md .claude/`.
  If the process is older, report that to the user and wait.
- At the start of a session, also confirm that the hub clone's `HEAD` equals
  `origin/main` after that fetch. A process started after a merge but before the hub
  clone was updated still loads the old rules. If they differ, report that to the user
  and wait.

## Merge policy

- You and the sub-agents never merge, whatever the ruleset would allow.
- A separate verification session on another device (claude.ai) may merge with the
  user's account when all of these hold: the user explicitly told it to, full tests
  and ruff pass on the combined state, and it leaves an approving review first, then
  merges with a merge commit.
- The user personally checks pull requests with the `review-overridden` label and
  those that change a safety path: a path matched by the hub target's `safety_paths`
  in `targets.toml`. That list is the only definition; do not copy it elsewhere.
  `orch pr` reports `user_check` and sends the `needs_review` notification for task
  pull requests. A harness pull request gets neither, so check its changed files
  against the list yourself. Mark them in the table. The list includes
  `targets.toml` itself, so a change to it is always a user check.
- The `main` ruleset requires one approval and the bot cannot approve its own pull
  request, so the bot cannot merge one without an approval. This does not stop a
  merge after an approval, so the rule above still applies.
- After opening pull requests, report every pull request waiting for merge in this
  table, so the user can hand it to the verification session as it is:

  | PR | Head commit | `main` commit | Combined tests | Ruff | Gates | Review | Merge by |
  | --- | --- | --- | --- | --- | --- | --- | --- |

  `Merge by` is `verification session` or `user check`. The results hold only for
  those two commits: if either one moves, run step 8a again.

## GitHub authentication

Use the GitHub App bot only. `git push` goes through the configured credential
helper. Call `gh` only in this form, and never pass the token to a sub-agent:

```
GH_TOKEN=$(~/.orch/bin/ghapp-token.sh) gh ...
```

## Procedure (Phase 3)

You run tasks through the installed `orch`. `R` below stands for
`~/.orch/orchestrator`.

| # | Step | What to do |
| --- | --- | --- |
| 0 | Plan | Report the task list (id, purpose, grade, lane, modify, dependencies) and get user approval. |
| 1 | Spec | Write `R/tasks/<id>.md` from `templates/task.md`. |
| 2 | Prepare | `orch prepare R/tasks/<id>.md`. It fetches, creates `R/worktrees/<id>` on branch `task/<id>` from `origin/main`, and runs the target's setup command. It refuses a used id, an unregistered dependency, and a `modify` that reaches a shared path or `tests/`. |
| 3 | Tests first | Write tests and interface stubs (bodies are `raise NotImplementedError`) in the worktree, confirm the tests to be implemented fail, and make exactly one commit. If a test already passes with only the stubs, say why in the commit message. To fix it before dispatch, use `git commit --amend`; never add a second commit. Then check that the fields, functions and state names the spec mentions match the tests. |
| 4 | Dispatch | `orch dispatch <id>`. It refuses unless the range holds exactly one commit. `reserved` goes on to step 5. `queued` waits for the reasons in `waiting_for`; run `orch next` on each completion notice. `blocked` means a dependency was discarded. |
| 5 | Run | In a background shell, run `orch run <id>` within the 10 minute reservation. Report the dispatch and end the turn. Do not poll. |
| 6 | Judge | On the completion notice, read the command's JSON output and `R/results/<id>.json`. Ignore the notice's exit code. If `DONE`, run `orch gate <id>`, which runs G1 to G3 and commits the sub-agent's changes when they pass. A failed gate returns the task to `judging`. If `report_missing`, read the log before retrying: limit detection can miss a usage limit error, and a retry would be wasted. |
| 7 | Review | When the gate leaves the task in `reviewing` (L3, L3R, or `review = true`): run `git diff <merge-base>..HEAD > R/reviews/<id>.diff` in the worktree so that the diff holds the tests and the implementation, then start `orch-reviewer` in the background with the paths of that diff, the spec and the worktree. Pass its JSON to `orch review save <id>` on standard input. `PASS` moves the task to `ready`; `BLOCK` moves it to `judging`. |
| 8 | Pull request | `orch pr <id>`. It rebases on `origin/main`, re-runs the tests, pushes, and opens the pull request in English. Report to the user with the merge table above. A rebase conflict moves the task to `conflict`; a test failure moves it to `judging`. |
| 8a | Main moved | While a pull request is open and `origin/main` moves, run `orch update <id>`. It checks a temporary merge and discards it, pushing nothing. On failing gates it comments on the pull request. On a conflict it moves the task to `conflict`: in the worktree, rebase on `origin/main` and resolve; if the resolution changed logic and the task is L3 or above, run `orch-reviewer` on the new diff and stop and report on a P0 or P1 finding; then run `orch pr <id> --resolution "<English description>"`, which pushes with the lease and comments. A lease rejection exits 64 and leaves the remote alone: stop and report. |
| 8b | Wait | After the report in step 8, run `orch wait <id> --merged` in a background shell and end the turn. Do not poll. On the completion notice, read the JSON output and ignore the exit code. `merged` true goes to step 9, also when `state` is `conflict`. `pr_state` `CLOSED` means closed without a merge: report it, then `orch discard <id>`. `ok` true with neither of those means the wait limit (exit 60): run the same command again. So does a notice with no output, or a restart of this session while a task is `pr_open`. `ok` false (exit 64 or 70): stop and report. |
| 9 | After merge | The pull request is merged under the merge policy above. Run `orch close <id>`, which confirms the merge and removes the worktree and local branch. Update the hub clone's `main` to `origin/main`, then run `orch next`. |
| 10 | Failure | `orch retry <id>` starts the second attempt from the test commit and appends the failure to the spec. On a second failure, `orch discard <id>` and re-decompose. A task with an open pull request can be discarded only after that pull request is closed. |

- Run only the installed `orch` (`~/.local/bin/orch`), never a worktree's
  `.venv/bin/orch`. Changed code runs only after a merge, a new tag and an upgrade the
  user approves: confirm that `orch status` shows no running task, back up
  `~/.orch/state.db`, then `uv tool install --reinstall` the new tag.
- The user creates a version tag only after the version pull request merges, on
  the resulting `main` commit. Never ask for a tag before that merge.
- `orch open` sets `ORCH_HUB` and `ORCH_TARGET` for the main session.
  `.claude/settings.local.json` in the hub clone, ignored by git, sets the same two
  values only so that an escape hatch session can still run `orch`.
- The pre-bash hook denies running `codex` or `claude -p` directly, force pushes,
  `gh pr merge`, the pull request merge API path and any path under
  `~/.orch/secrets`, and asks the user before `orch resume`. A denial is a rule, not
  an error to work around: never rephrase a command to get past it. If a legitimate
  command is denied, stop and report it as a false positive.
- `orch` refuses a `modify` that reaches a shared file, so a pull request changing one
  is a harness pull request you write yourself, outside `orch`: create the worktree
  with `git worktree add R/worktrees/<id> -b task/<id> origin/main`, run
  `uv sync --frozen`, push with `git push -u origin task/<id>`, and open it with `gh`.
  It has no task, so `orch wait` does not apply: learn of the merge from the user.
  After the merge, remove the worktree and local branch. If it conflicts with `main`,
  merge `origin/main` into the branch and push normally.
- A harness pull request must also pass before you push: in its
  worktree, run `.venv/bin/python -m pytest`, `.venv/bin/python -m ruff check .` and
  `.venv/bin/python -m ruff format --check .` on the final commit. This covers every
  push to that branch. Put the results in the pull request body.
- `scripts/run-sub.sh` and `scripts/gate.sh` stay until M16 as a fallback for when
  `orch` itself fails and the user approves using them. Never handle one task with
  both: they do not update `state.db`. M16 removes only these two;
  `scripts/guard_regress.py` stays.
- `scripts/guard_regress.py` checks guard for false positives against your local
  transcripts. Run a branch's guard with the worktree's `.venv/bin/python`, and the
  installed guard with the installed orch tool's Python from the hub clone. Only the
  number summary (total, unique, allow, deny, ask, false positives, deny count per
  reason) leaves this machine. Never put `--show` output in a commit message, pull
  request body or report.
- When two pull requests go up together, finish a check on their combined state before
  the user merges and report it: full tests and ruff, in a temporary worktree that
  merges both. `orch update` checks each one against `main` only. Do not defer it to
  a later task's worktree. Watch for semantic conflicts, especially when both change
  the same test file.
- When you discard a task and dispatch it again under a new id, state that and its
  cause in your report. A commit message alone is not enough.
- Before committing tests, run them against the stubs and confirm each fails for the
  expected reason, not implemented. If one fails for another reason, such as a data
  format error, fix the test first. A test that passes with stubs alone still needs
  its reason in the commit message.
- Pass test inputs through the real parser once, because `NotImplementedError` can
  hide an input error.
- A fixture that captures output must not discard what it captured. If it reads
  `capsys` or similar for its own use, write the rest back (for example
  `sys.stderr.write(captured.err)`) so the test can still assert on it. The `orch`
  fixture in `tests/test_cli.py` does this. Otherwise an assertion on the capture
  sees an empty string: it either always fails or always passes, and the stubs fail
  earlier and hide it (m-notify-cli).
- Call state-changing `orch` commands in tests through the `orch` fixture in
  `tests/test_cli.py` or the integration `Sandbox`. Both check the transition with
  `models.can_transition` before the call (`tests/transitions.py`). Mark a call that
  expects a refusal, or whose command is replaced with a fake, `unchecked=True`. In a
  test with several calls, make every call before asserting on behavior the stubs
  lack, so that an early failure cannot hide a later call from the check.
- A spec that relies on an external tool's exit codes, output format or failure
  handling is written from results measured on the pinned version, not from
  documentation or memory. State the version and the measurement in the spec
  (m14-cli: only exit 2 or a deny output blocks a PreToolUse hook).
- Before naming a function in a spec, read its code and confirm its behavior matches
  the spec's intent.
- When a test expects a result from an existing module, such as a classification or
  an exit code, confirm the actual behavior from that module's code or tests before
  committing. Stubs fail first, so they cannot reveal a wrong expectation.
- Run at most 1 or 2 tasks at once. `orch dispatch` queues a task whose `modify`
  overlaps a running one and enforces the limits in `~/.orch/config`.
- `<merge-base>` is the output of `git merge-base origin/main HEAD`, run in the worktree.
- `orch run` reads the lane, model, effort and `timeout_min` from the spec's front matter, removes tokens from the sub-agent's environment, enforces the time limit, and reports `BLOCKED_QUOTA` when a run without a valid result ends with a usage limit message. A `BLOCKED` or `BLOCKED_QUOTA` result moves the task to `blocked` and keeps its worktree: report and wait for the user. Run `orch resume <id>` only after the user approves; it resets to the test commit unless given `--keep`, and uses no attempt. Each run writes its own log, `R/logs/<id>.<n>.log`, and the usage limit check reads only that log, so an earlier run's message cannot change the verdict. It runs the agents by the absolute paths in `~/.orch/config`, not through `PATH` or a mise shim, because a shim follows the current directory's mise settings.
- Use absolute paths in commands. `cd` into a worktree carries over to later commands, and you cannot remove a worktree you are still inside.
- **Only `tests/**` is frozen.** List each stub file in the spec's `modify`. Gate
  G1 fails if a file under `tests/` changes.
- Review judgments can vary between runs. Before a P0 or P1 finding leads to a retry
  or a discard, check it against the spec and the code. Reject a wrong finding with a
  triage file: `orch review save <id> --triage <file>`, one decision per P0 or P1
  finding. It moves the task to `ready`, and `orch pr` adds the override section and
  the `review-overridden` label.
- A confirmed finding whose `path` starts with `tests/` does not use the retry,
  because tests are frozen and a sub-agent cannot fix them. Sort it into one of three
  kinds:
  - **Wrong tests** expect incorrect behavior. Save the review without a triage file,
    `orch discard <id>`, fix the tests yourself and dispatch the work again as a new
    task with a new id.
  - **Insufficient tests, correct implementation:** only a check is missing. Register
    a follow-up task that adds the tests, then save with a triage file whose
    `insufficient_tests` decision names it. The review verdict stays `BLOCK`.
  - **Insufficient tests, implementation defect:** review confirmed a defect that no
    test catches. Save without a triage file, `orch discard <id>`, and dispatch it
    under a new id with stronger tests.
- A confirmed implementation defect never goes through triage: save without
  `--triage`, then `orch retry <id>` or `orch discard <id>`.
- When you dispatch under a new id with stronger tests, run them against the
  discarded implementation, confirm that only the tests for the defect fail, and
  state the result in the test commit message.
- **Retry exemption.** It applies only when a sub-agent reports `spec_mismatch`, the
  cause is your spec error, and the tests do not change. Report it, fix `R/tasks/<id>.md`,
  and with the user's approval run `orch resume <id>`, which returns to the test commit
  and removes uncommitted changes. This does not count against the retry. Record the
  fix and the reason at the end of the spec and in a pull request comment. If the tests
  must change, the exemption does not apply: dispatch under a new id.
- For concurrency tests in an L3 task only, you may check before dispatch that the
  tests are stable by running them repeatedly against a reference implementation.
  Never commit it, delete it right after the check, and state that you used it in
  the test commit message.
- Run integration tests in a sandbox with frozen commit dates. Before a push, repeat
  each integration test file you changed or added at least 5 times. Repeats alone can
  miss an intermittent failure, depending on machine speed:
  one test that expected two commits to have different ids passed 20 of 20 runs on one
  machine and failed on a faster one. Frozen dates make that kind of test fail every
  time. Repeats are still needed for other kinds of intermittent failure.
- Write `verify` as `.venv/bin/python -m pytest ...`. It must work without network
  access and must not write outside the worktree.
- Do not add `--add-dir`. Under `workspace-write`, a sub-agent cannot run
  `git commit`, write outside the worktree, or use `uv run`, which is why `orch` syncs,
  moves the result file and commits.
- Worktrees and reviews live outside this repository. You and the read-only
  sub-agents read them without a permission prompt because the user settings
  (`~/.claude/settings.json`) set `permissions.additionalDirectories` to
  `["~/.orch/orchestrator"]`. No per-clone setup is needed.
- Never put secrets or tokens under `~/.orch/orchestrator`. Those settings let every
  Claude session on this machine read that directory.
- If the procedure proves awkward, fix the harness through a pull request.
