# ADR 0002: Work items

Status: proposed, awaiting the owner's decision. Issue: #81.

This record fixes the vocabulary the factory needs before it builds features from tickets: what a work item is, the states it moves through, who may move it, how the next item is chosen, and what a "team" is. The rules themselves are in [CONVENTIONS.md](../CONVENTIONS.md#work-items). This record gives the reasons and what each later issue must build. Where the issue left a choice open, the choice below is a proposal, and the owner's review decides it.

## Decision in one line

A work item is a row in the trace store's DuckDB file with a fixed state machine that only pipelines and `factory` commands move, the next item is chosen by code, and a team is a metadata label on an agent's manifest, with composition in pipeline modules.

## Context

Phases 1 to 6 built agents that each answer one request: review a diff, fix a finding, write a test. The owner's proposal `sp.md` describes a factory that takes an idea through planning, building, review and release, in eight teams of agents with an Engineering Lead that orchestrates them. Phase 7 (#113) starts that work with the store (#82), the outside-write policy (#83), the GitHub tools (#84), the rubric grader (#85), feature-coder (#86) and the build loop (#87). Each of these needs the same words for the same things, so they are fixed first.

The rules this decision has to fit:

- [PLAN.md](../PLAN.md#rules): no agent holds state between runs.
- [Gates](../CONVENTIONS.md#gates): thresholds, routing and skip decisions are code in `runtime/gates.py`, never a model's choice. A Jev `score` question returns `{value, level, probs, confidence}`, and the triage decision row is a `kind` `jev` trace row with no cost of its own.
- [Trace row](../CONVENTIONS.md#trace-row): the trace store is one DuckDB file with one connection and one writer per process, and schema changes are numbered migrations in `MIGRATIONS` in `runtime/trace.py`. `Trace.cost_by_pipeline_run` already sums a pipeline run's cost.
- `registry/spec.py` forbids unknown manifest keys, so any new manifest field is a schema change in the registry.
- [Exit codes](../CONVENTIONS.md#exit-codes): `UsageError` exits 2 and `NotFoundError` exits 3.

## Options considered

### Where the work item lives

- **(a) Two tables in the trace store's DuckDB file.** Proposed. Migrations go into the existing `MIGRATIONS`, the store uses the `Trace` connection and lock, and the cost of an item is a read over rows that already exist.
- **(b) A separate DuckDB file.** Rejected. DuckDB allows one writer process per file, so a second file means a second connection, a second migration list and a second lock in every pipeline that touches both, and the item's cost would join across files.
- **(c) GitHub Issues or Projects.** Rejected for the store. The factory must run against a local repository with no remote, such as a fixture, and offline tests cannot depend on the GitHub API. The state machine would also be enforced by labels any person or tool can change. A work item may name the GitHub issue it came from in its `body`; #84 may add a sync later, from the store outwards.

### Where the state lives

- **(a) In the store, moved by code.** Proposed. Pipelines and `factory ticket` call `move`, which checks one table of legal pairs and writes an event with a reason.
- **(b) Inside agents.** Rejected. An agent that remembers its backlog between runs breaks the rule that no agent holds state, cannot be versioned or evaluated on fixed cases, and leaves no trace of why an item moved. An Engineering Lead agent that holds the backlog in its context, as `sp.md` describes, is this option.

### Who chooses the next item

- **(a) Code over the store, with an optional Jev ranking as one sort key.** Proposed. The order is deterministic and testable, and a Jev score can refine it only when it is confident, as the triage gate already works.
- **(b) An LLM orchestrator that assigns work.** Rejected. It would put routing in a model, which [Gates](../CONVENTIONS.md#gates) forbids, add cost to every step, and make the order impossible to reproduce. An agent may still be asked a question whose answer code uses; it never makes the assignment.

### What a team is

- **(a) A metadata label in `agent.yaml`, with composition in pipelines.** Proposed. The registry and the publish gate stay as they are, and a pipeline already pins the agents it composes.
- **(b) A team abstraction in the registry.** Rejected. Team manifests that are resolved, versioned and published would add a second unit next to the agent, with its own semver, its own eval gate and its own address syntax, for no behaviour that an agent address and a pipeline do not already give. A team's membership changes when an agent is added, so every new agent would bump a team version, and a pipeline that composes agents of two teams, as the build loop does, would pin two team ranges as well as its agents.

## Decision

Adopt option (a) in each choice above, with the rules in [CONVENTIONS.md](../CONVENTIONS.md#work-items). The reasons:

- **The store sits next to the trace.** One file, one connection and one migration list keep the single-writer rule simple. The cost so far is derived from the trace, not stored, so it cannot drift.
- **States are few and every move has an owner.** Eight states, each move made by one named piece of code or by a person, and a typed error for every other pair. `done` and `dropped` are final, so history is never rewritten; a regression is a new item.
- **Nothing skips review.** `done` is reachable only from `in_review`, so no pipeline can finish an item without a person seeing its pull request, which fits the outside-write policy of #83.
- **Ordering is code.** Incidents first, then the items that unblock the most others, then a confident Jev score when a ranking agent exists, then age. A ranking agent is optional, and its failure only drops one key.
- **Teams are labels.** They group agents for people and reports, and they never change what runs.

### Proposals the owner should check

These are choices the issue left open, made here so the rules contain no gap:

- **Ids are `wi-<n>`**, counted in the store, never a GitHub issue number, so a local fixture and a GitHub repository use the same ids.
- **Two fields beyond the issue's list**: `impacted_paths` and `size`, because story-breakdown (#91) produces them and the orchestrator's parallelism rule needs `impacted_paths`.
- **A parent may be a PRD**, written `prd:<pipeline_run_id>`, so the plan loop (#92) can link tickets to the PRD it stored without a work item for the PRD itself.
- **Every move has a non-empty reason and an actor** (`human`, `pipeline:<module>` or `store`).
- **`blocked` and `escalated` differ by whether a human question settles them.** Until #93 lands, the build loop moves every stop to `blocked`; after it, a stop a human can answer goes to `escalated`.
- **Moves into `done` and `dropped` are made by a person.** No pipeline watches pull requests yet.
- **A person may make any legal move** with `factory ticket move`, and no illegal one, the owner included.
- **`IllegalTransitionError` is a `UsageError` (exit 2)** and `WorkItemNotFound` is a `NotFoundError` (exit 3).
- **The kind decides the pipeline by a fixed table**, and two items run at the same time only when their `impacted_paths` are known and disjoint.
- **Pipeline modules are named after the loop, not the team.** The issue suggested `pipelines/<team>.py`. #87 and #92 already name `pipelines/build_loop.py` and `pipelines/plan_loop.py`, and the build loop composes `dev` and `qa` agents, so one module per team would split it.
- **The `team` field bumps no version.** It never reaches a model. Under the patch rule, adding it to twelve published manifests would force twelve republishes for no change in behaviour.

## Consequences

### What later issues must build

- **#82, the store.** Add `work_items` and `work_item_events` as the next migrations in `MIGRATIONS`. Write `runtime/work_items.py` with `add`, `get`, `list(status=...)`, `move(id, to, reason)`, `link_run(id, run_id)` and `ready()`, the transition table, `IllegalTransitionError` and `WorkItemNotFound`, the acyclic check on `depends_on`, and the cost so far as a read over the trace. Add the `Ticket` payload model to `registry/common_schemas.py`. Add `factory ticket add|list|show|move|link|ready` and `docs/work-items.md`. Tests cover every legal pair and every rule in the list of illegal moves.
- **#86, feature-coder.** `FeatureInput` carries a `Ticket` and never a status or a cost; `FeatureOutput` may say `blocked` with a reason, which the build loop turns into a move. The same pull request adds the optional `team` field to `AgentSpecBase` with the eight values, sets `team: dev` on feature-coder, and sets the team of each existing agent as in the table below, with no version bump.
- **#87, the build loop.** `pipelines/build_loop.py` and `factory build <ticket-id>`. It refuses an item that is not `ready`, links its run, moves the item to `in_progress` before the clone, to `in_review` when the panel says `ship` and the pull request is open, and to `blocked` in a `finally` block on any other end, with the cause as the reason. Its docstring names the `dev` and `qa` teams.
- **#91, story-breakdown.** Its output tickets use the `Ticket` shape without ids, with dependencies as indexes into its own list, `impacted_paths` from `deps.impact` (#88) and `size` as `S`, `M` or `L`. It writes nothing to the store.
- **#92, the plan loop.** `pipelines/plan_loop.py` stores the PRD at `data/plans/<run-id>.md`, turns story-breakdown's indexes into `wi-<n>` ids, and writes each ticket in `todo` with `parent` `prd:<run-id>` only after a person approves the plan. It never moves an item to `ready`; `ready()` does.
- **#93, escalations.** An `escalations` table, the move from `in_progress` or `blocked` to `escalated`, and `factory escalations answer`, which stores the answer and moves the item to `ready` so the next run gets it as payload. From then on the build loop sends a stop a human can answer to `escalated`, and `blocked` keeps only failures outside the item.
- **#96, the metrics report.** `runtime/reporting.py` and `scripts/factory_report.py` read the events table for the time spent in each state and the number of items blocked or escalated, report the cost so far per item, and may group agents by `team`.

### What the decision rules out

- A team manifest, a team address, a team version or a team publish gate.
- An Engineering Lead agent that holds the backlog or assigns work.
- An LLM call that picks the next item, sets a priority or chooses a pipeline.
- Any tool that lets an agent read or write the store.
- Moves out of `done` or `dropped`, and moves into `done` that skip review.

## The eight teams of sp.md and the phases

The owner's proposal `sp.md` names eight teams. Each maps to a `team` value, to the phases that build it, and to what each of its agents becomes in this repository. Existing agents get the team in the last column when #86 adds the field. `action-guard` gets none, because it is a guard used by every sandboxed pipeline, not a member of a team.

| Team in `sp.md` | `team` | Phases and issues | What each agent becomes | Existing agents |
| --- | --- | --- | --- | --- |
| Product Management and Planning | `planning` | 8 (#114): #88 to #92 | Product Architect: product-architect (#90). Story Breakdown: story-breakdown (#91). Dependency Mapping: the `deps` tool (#88), not an agent. The plan loop (#92) composes them behind human approval | product-architect |
| Development Squad | `dev` | 7 (#113): #86, #87; 10 (#116): #97, #98 | Engineering Lead: the build loop and the orchestrator rule, which are code, not an agent. Feature Coding: feature-coder (#86). Refactoring: refactorer (#98). Documentation: documentation-agent (#97) | fixer |
| QA and Cyber Security | `qa` | 10 (#116): #99 to #101 | Test Generation: test-writer 0.2 cover mode (#99). Security Auditor: security-reviewer with `sast.scan` (#101). Peer Reviewer: the review panel with `review --comment` (#100) | code-reviewer, security-reviewer, perf-reviewer, judge, test-writer |
| SRE | `sre` | 12 (#118): #107 to #109 | Risk Assessment: what exists already, diff-triage, the judge gate and the judge's score. Triage and Diagnostics: triage-diagnostics with the Sentry tools (#107, #108). Self-Healing Patch: the hotfix pipeline (#109), which stops at a pull request and never deploys | diff-triage, judge-gate |
| Research | `research` | 11 (#117): #102 to #106 | Technical Feasibility: technical-feasibility (#104). Competitor and Benchmark: benchmark (#105). Prototyping (Spike): the spike agent in a networked sandbox (#106) | technical-feasibility, benchmark |
| Decomposition | `decomp` | 13 (#119): #110 to #112, deferred | Code Archaeological, and Extraction and Modularization: built against the target #110 picks. Black-Box Behavioral: expected to become its own project or be dropped | none |
| HITL Bridge | `hitl` | 9 (#115): #93 to #95 | Escalation and Context Binder: the escalation store (#93), escalation-summary (#94) and `notify.issue` (#95). Expert Router: deferred, because there is one human | none |
| Data and Analytics | `analytics` | 9 (#115): #96 | Factory Performance Telemetry: the metrics report (#96), a script, not an agent. RAG and Vector DB Curation: no issue builds it; `memory.search` (#89) only reads agb memory | none |

Three of `sp.md`'s agents become code rather than agents: the Engineering Lead, because assignment is code; Dependency Mapping, because a dependency graph is computed, not reasoned about; and Factory Performance Telemetry, because a report over the trace needs no model.
