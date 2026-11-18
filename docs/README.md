# orchestrator documentation

English | [한국어](README.ko.md)

This index separates implemented behavior, operations, design requirements, and future
plans. Implementation descriptions use source commit `1c191614` (2026-11-18), whose
package declares version `0.15.0`. Source and installed binaries can differ even when
the version string is the same; verify deployment separately.

## Implementation and operations

| Document | Contents |
| --- | --- |
| [Features and CLI](reference/features.md) | Available commands, partial implementations, missing features |
| [Architecture and data flow](reference/architecture.md) | Module boundaries, state transitions, storage, execution, GitHub |
| [Runtime and operations](operations/runtime.md) | Python/macOS/tmux, configuration, services, checks, recovery |
| [Work priority and dependencies](operations/work-priority.md) | Current gaps, required records, proposed project ordering |
| [Work plan template](../templates/work-plan.md) | Priority, prerequisites, evidence, change history |
| [Task template](../templates/task.md) | Executable specification and main-owned Planning fields |

English is the default for these reference and operations documents. Each links its
preserved Korean edition. Update both editions together when their meaning changes.

## Design sources

The existing design sources below remain in Korean. They define requirements, not a
claim that every feature is implemented. When they conflict, precedence is requirements,
technical, execution, security, then screen design. The build procedure sets current phase scope.

- [Requirements](design/요건%20정의.md): roles, permissions, gates, workspace requirements.
- [Technical design](design/기술%20설계.md): data model, state machine, command/output contracts.
- [Execution design](design/실행%20설계.md): lanes, directories, injection, operational procedure.
- [Security design](design/보안%20설계.md): threat model, sandbox, authentication, device-loss response.
- [Screen design](design/화면%20설계.md): workspace interaction and device acceptance criteria.
- [Build procedure](design/구축%20절차.md): phase prerequisites and progress history.
- [ADR index](decisions/README.md): reasons and history of design decisions.
- [GitHub recovery bindings](github-tracking.json): reviewed group/issue seed, not execution-state authority.

## Open proposals

The workspace implementation design and plan in
[PR #506](https://github.com/2muni-labs/orchestrator/pull/506) and the visual extensions in
[PR #145](https://github.com/2muni-labs/orchestrator/pull/145) are unmerged at this baseline.
After merge, link their local documents and recheck implementation status.

## Maintenance

When implementing a feature, update its status in features, its owning modules in
architecture, and its configuration/operational changes in runtime. Compare the CLI
list with the parser in `src/orch/cli.py`. Distinguish source that is not installed,
commands that exist only in designs, and functions without CLI wiring. Never publish
real tokens, logs, or personal absolute paths as examples.
