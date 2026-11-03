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
  conflicts and P0 emergency fixes.
- Judge from the result JSON and gates. Read sub-agent code only when a gate fails
  or a task is partial. Do not re-analyze or re-implement work that passed.
- Never merge a pull request. Never push to `main` or force push. Push task
  branches only.

## GitHub authentication

Use the GitHub App bot only. `git push` goes through the configured credential
helper. Call `gh` only in this form, and never pass the token to a sub-agent:

```
GH_TOKEN=$(~/.orch/bin/ghapp-token.sh) gh ...
```

## Manual procedure (Phase 2)

`orch` does not exist yet, so you run its steps by hand. `R` below stands for
`~/.orch/orchestrator`.

| # | Step | What to do |
| --- | --- | --- |
| 0 | Plan | Report the task list (id, purpose, grade, lane, modify, dependencies) and get user approval. |
| 1 | Prepare | `git fetch origin`, update the hub clone's `main` to `origin/main`, then `git worktree add R/worktrees/<id> -b task/<id> origin/main`, then run `uv sync --frozen` in the worktree. |
| 2 | Tests first | Write tests and interface stubs (bodies are `raise NotImplementedError`) in the worktree, confirm the tests to be implemented fail, and make exactly one commit. If a test already passes with only the stubs, say why in the commit message. To fix it before dispatch, use `git commit --amend`; never add a second commit. |
| 3 | Spec | Write `R/tasks/<id>.md` from `templates/task.md`. |
| 4 | Run | In a background shell, run `scripts/run-sub.sh <id>` from the hub clone. Report the dispatch and end the turn. Do not poll. |
| 5 | Judge | On the completion notice, read `R/results/<id>.json`, which `run-sub.sh` has moved there or written as a failure. Ignore the notice's exit code. If `DONE`, run `scripts/gate.sh <id>` from the hub clone. If `report_missing`, read the log before retrying: limit detection can miss a usage limit error, and a retry would be wasted. |
| 6 | Commit | When the gates pass, commit the sub-agent's changes on `task/<id>`. |
| 7 | Review | For L3 and above, or risk areas: run `git diff <test_sha>..HEAD > R/reviews/<id>.diff`, then start `orch-reviewer` in the background with the path to that diff and the worktree. Save its JSON as `R/reviews/<id>.json`. |
| 8 | Pull request | Rebase on `origin/main`, re-run the tests, `git push -u origin task/<id>`, create the pull request in English, and report to the user. |
| 9 | After merge | The user merges on GitHub. Return to the hub clone, then remove the worktree and local branch. |
| 10 | Failure | Retry once: reset to the test commit, remove `.orch-result.json`, append a failure summary to the spec. On a second failure, discard and re-decompose. |

- Run at most 1 or 2 tasks at once, and check by hand that their `modify` paths do
  not overlap.
- `<test_sha>` is the first commit after the branch point from `origin/main`, which is why step 2 allows exactly one commit.
- `run-sub.sh` reads the lane, model, effort and `timeout_min` from the spec's front matter, removes tokens from the sub-agent's environment, enforces the time limit, reports `BLOCKED_QUOTA` when a run without a valid result ends with a usage limit message, and runs lane B only. Keep the worktree of a `BLOCKED_QUOTA` task and wait for the user. It runs Codex by the absolute path of the globally pinned version (`mise use -g codex@0.160.0`), not through `PATH` or a mise shim, because a shim follows the current directory's mise settings.
- Use absolute paths in commands. `cd` into a worktree carries over to later commands, and you cannot remove a worktree you are still inside.
- Run `run-sub.sh` and `gate.sh` from the hub clone, not the worktree. A sub-agent can change the worktree's copy.
- **Only `tests/**` is frozen.** List each stub file in the spec's `modify`. Gate
  G1 fails if a file under `tests/` changes.
- Write `verify` as `.venv/bin/python -m pytest ...`. It must work without network
  access and must not write outside the worktree.
- Do not add `--add-dir`. Under `workspace-write`, a sub-agent cannot run
  `git commit`, write outside the worktree, or use `uv run`, which is why you sync,
  move the result file and commit.
- Worktrees and reviews live outside this repository. To let you and the read-only
  sub-agents read them without a permission prompt, each clone needs an uncommitted
  `.claude/settings.local.json` with `permissions.additionalDirectories` set to
  `["~/.orch/orchestrator"]`.
- If the procedure proves awkward, fix the harness through a pull request.
