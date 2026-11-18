# Work priority and dependencies

English | [한국어](work-priority.ko.md)

This document separates required main-agent planning records from proposed GitHub
configuration. Baseline: source `b757a619` and orch Projects #2–9 observed on 2026-11-18.
Read [implemented features](../reference/features.md) and
[main policy](../../config/main.md) alongside this document.

## Current behavior

| Area | Implemented behavior | Gap |
| --- | --- | --- |
| Task spec / DB | Store task ids in depends_on | No business-priority field |
| Scheduler | Require closed predecessors; discarded predecessors block successors | No external issue/install/measurement checks |
| orch next | Inspect queued work in creation order | No priority ordering or automatic preemption |
| TODO / status | Show execution state and some blocker information | No full dependency DAG or priority view |
| GitHub Projects | All eight orch projects have Status | No Priority fields |
| Tracking | Bindings and synchronization engine exist | Priority/dependency sync and CLI/service wiring remain |

`models.Severity.P0` is review finding severity, not business priority. L1–L3R defines
risk/verification classification. Do not copy these values between concepts.

## Required recording procedure

Persist each plan at `R/plans/<bundle-id>.md` using the
[template](../../templates/work-plan.md), where R is `~/.orch/<target>`. The main agent
creates/manages the directory. The current CLI does not generate or validate this file.
Apply the procedure to new bundles and existing work when next replanned; do not invent
historical priorities for every completed item.

Record task/record ids, target issue/project, priority and reason, readiness,
predecessors, completion evidence, successors, review date, and change history.
Use `none` for no dependency and `unknown` for missing evidence. Resolve unknown hard
prerequisites before dispatch.

- **Executable dependencies:** put registered task ids in TOML depends_on and keep the
  body's list consistent. The scheduler checks predecessor closed states.
- **External hard prerequisites:** record issue URLs, decisions, measurements,
  installations, and acceptance conditions in the body. The main verifies evidence;
  a closed issue alone does not prove installation.
- **Soft ordering:** record preferred sequencing separately from hard dependencies
  so it does not unnecessarily prevent parallel work.
- **Changes:** recheck at plan approval, before dispatch, on predecessor completion or
  discard, and at handoff. Record dated old/new values, reasons, and affected successors.
- **Cycles/missing predecessors:** resolve A→B→A and missing references during planning.
  External relationships are not checked by the CLI; preserve the review result.

The local plan owns priority/external dependency decisions. The issue's Planning block
and project Priority field mirror it; state.db remains execution-state authority.
Reconcile user priority edits in GitHub at the next planning review. Record failed
writes as pending sync without losing the authoritative local data. Place Planning
outside the sync-managed progress block. Do not edit generated TODO.md or add unsupported
keys to task TOML or the tracking import schema.

## Proposed GitHub setup

The following is a proposal. Documentation changes alone do not configure remote fields
or reprioritize all issues.

| Priority | Meaning | Example |
| --- | --- | --- |
| P0 | Emergency recovery from a current incident, data loss, or confirmed serious safety failure | A problem with verified impact/evidence |
| P1 | Opens the current objective's critical path | Tracking integration/release, mandatory Phase 5a prerequisites |
| P2 | Prepares the next objective or delivers ordinary improvements | Workspace measurements and design decisions |
| P3 | Later-phase or conditional work | v2/v3 or improvements without a demonstrated current need |

Do not assign P0 to an entire project or infer P0/P1 from a security-related title.
When an important outcome is blocked, find the first unmet prerequisite and record
why its priority should be temporarily promoted. Exclude completed/cancelled work from
selection while retaining historical priority records.

Use the same Priority single-select (P0–P3) in each group project and keep Status intact.
An item field does not rank projects themselves. Put this selection summary at the top
of each project README:

| Field | Value |
| --- | --- |
| Focus rank | Current candidate order, maintenance, or deferred |
| Outcome | Result to complete in the current focus period |
| Next eligible issue | Next issue whose prerequisites are satisfied |
| First blocker | First unmet prerequisite, owner, and next check |
| Review date | Last evidence review |

If needed, use a separate portfolio Project with one representative issue per group.
Keep the existing group projects and do not create duplicate issues for the same work.
Record hard issue relationships as URLs in Planning first. Before using native GitHub
blocked-by relationships, measure availability and App permissions in this organization;
mirror only the same verified relationships. Parent/sub-issue relationships describe
decomposition and do not substitute for dependencies.

Suggested views: Ready sorted by Priority, Blocked for prerequisite resolution,
Needs Decision, Needs Verification, and Done. Current Status has no Ready option, so
Todo alone does not prove eligibility. Check readiness in Planning or introduce a
separate Readiness field. Do not rename Status options used by synchronization.

## Proposed project order

This order follows delivery prerequisites, not issue counts. OPEN does not prove
unimplemented work: tracking storage and the engine already exist in source, so inspect
linked PRs and execution evidence before starting another implementation.

| Group / project | Recommended focus | First checks |
| --- | --- | --- |
| [G05 / #7](https://github.com/orgs/2muni-labs/projects/7) | First; critical path P1 | Evidence and remaining scope for #495 CLI, #496 services, #497 release, #498 procedure |
| [G07 / #2](https://github.com/orgs/2muni-labs/projects/2) | Second candidate; P2 | #149 baseline/policy, then CU prerequisites; promote only work that actually unblocks 5a |
| [G06 / #8](https://github.com/orgs/2muni-labs/projects/8) | Third; preparation P2 | #349–352 decisions, #409–411 measurements; implementation after 5a |
| [G01 / #3](https://github.com/orgs/2muni-labs/projects/3) | Maintenance | Promote individual current incidents or critical-path blockers |
| [G02 / #4](https://github.com/orgs/2muni-labs/projects/4) | Maintenance | Assess PID/process cleanup and test instability using current reproduction evidence |
| [G03 / #5](https://github.com/orgs/2muni-labs/projects/5) | Maintenance | Select sandbox checks that block 5a safety prerequisites |
| [G04 / #6](https://github.com/orgs/2muni-labs/projects/6) | Maintenance | #398 recovery records and #414–415 real-environment evidence |
| [G08 / #9](https://github.com/orgs/2muni-labs/projects/9) | Deferred P3 | External targets and v2/v3 after v1 acceptance |

Within G05, reconcile merged tracking work against CLI/service/release integration
rather than reimplementing it. Then check unmet FL and M17→M18→M19 prerequisites against
the build procedure. #328 requires one actual week of unattended operation after
installation; documents/tests cannot replace that observation period. G07's CU6 two-week
pilot does not replace it either.

Workspace implementation proposals are in
[PR #506](https://github.com/2muni-labs/orchestrator/pull/506), with visual additions in
[PR #145](https://github.com/2muni-labs/orchestrator/pull/145). Both are OPEN at this
baseline and must not be treated as implemented features.

## Rollout and future automation

1. Reconcile unfinished issues with PR, task, and installation evidence to establish readiness.
2. Review priority definitions/project order, then configure consistent fields/views.
3. Populate Priority/Planning for the next execution bundle and verify writes by read-back.
4. Approve and dispatch small bundles containing eligible work only.
5. Refresh next eligible issue and blocker at every bundle completion.

Implement automatic priority ordering as separate work: task/DB schema and migration,
compatible defaults, deterministic priority→creation time→id ordering, TODO/status/show
contracts, and GitHub field mapping/permissions/retries/conflict handling. Tests must
cover old DB/spec compatibility, ties, high-priority work still waiting for dependencies,
locks or pauses, no preemption, and preserved execution state on GitHub failure.
Until that implementation ships, do not say orch next follows GitHub priorities.

Tracking and discussion publication: [issue #509](https://github.com/2muni-labs/orchestrator/issues/509).
