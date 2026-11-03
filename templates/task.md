+++
id = "<task-id>"
target = "orchestrator"
grade = "L2"                  # L1 | L2 | L3 | L3R (risk area)
lane = "B"                    # B (Codex) | C (Claude) | D (interactive)
model = "gpt-6.1-sol"
effort = "medium"             # low | medium | high | xhigh
timeout_min = 45
modify = ["src/orch/<module>.py"]
read = ["src/orch/**", "tests/**", "docs/design/**"]
verify = ".venv/bin/python -m pytest tests/test_<module>.py"  # no network, no writes outside the worktree
depends_on = []
# tests = "none"              # L1 only: dispatch without a test commit
+++

<!-- Write the body in Korean. Keep identifiers and paths in English. -->

# Purpose

<!-- One or two sentences defining the finished state. -->

# Confirmed facts

<!-- Facts the main agent has verified. No guesses. -->

# Interface

<!-- Signatures and schemas to implement. Omit if not applicable.
     Stubs in the first commit have `raise NotImplementedError` bodies; list their
     files in `modify`. -->

# Verification

Passing `verify` above is required. Gates G1 to G3 run again afterwards.

Gate G3 runs ruff, so also pass these on the files you changed:

- `.venv/bin/python -m ruff check <paths>`
- `.venv/bin/python -m ruff format --check <paths>`

# Budget

- Time: `timeout_min`, enforced.
- Tokens: <guideline only, not enforced>
- Retries allowed: 1

# Stop conditions

Stop immediately and report if any of these occur:

- A confirmed fact does not match the code: `BLOCKED`, reason `spec_mismatch`.
- The task needs a change outside `modify`: `BLOCKED`, reason `out_of_scope`.
- The budget is exceeded: `FAILED`.
- A usage limit error occurs: `BLOCKED_QUOTA`.

# Prohibited

Do not re-analyze this specification. Do not change the design. Do not modify test
files. Do not run `git commit`, `git push` or merge. Do not run commands in the
background.

# Result report

Before you finish, write `.orch-result.json` in the worktree root, conforming to
`schemas/result.json`, with `task_id` set to this task's `id` and `log_path` set to
`~/.orch/orchestrator/logs/<task-id>.log`. Leave your changes uncommitted.
