# ADR 0005: Decomposition target

Status: accepted, 2026-11-22. Issue: #110.

## Decision in one line

The decomposition team works on `legacy-billing`, a synthetic legacy codebase kept in this repository as a fixture; `code-archaeology` and `extraction-modularization` are built against it, and `black-box-behavioural` is dropped.

## Context

Phase 13 (#119) adds a team that reads a legacy codebase and prepares one feature for extraction. #110 asks for the target and for which of three agents to build:

- **code-archaeology**: dead code, data flows, the dependency graph and the execution paths from an entry point.
- **extraction-modularization**: one feature out of a monolith, as a blueprint a coder can take.
- **black-box-behavioural**: inputs in, business rules out, with no source.

The target has to be Python or TypeScript so the existing read-only tools apply (`git.read_file`, `rg.search`, `deps.graph`, `deps.impact`), and it has to have tests. #112 requires deterministic eval checks "against known dead code and known data flows", so the dead code and the data flows of the target must be known in advance, not estimated after the fact.

## Options considered

### (a) A synthetic legacy fixture in this repository (chosen)

A package written to look like an old monolith, with dead code, a call chain and data flows planted on purpose and recorded beside it. The ground truth is exact, so an eval can check an agent's dead code list and data flows without a judge. It builds with `fixtures.build` like every other fixture, costs nothing to fetch, carries no licence question and puts no private code into prompts or traces. It is small, so an eval run stays cheap.

### (b) One of the owner's older repositories

Rejected for now: none was nominated, and the owner asked on 2026-11-21 for a stand-in instead. A real repository has no record of which code is dead, so the deterministic checks of #112 would need a person to label it first, and its source would go into model calls and the trace store.

### (c) A public legacy codebase with tests

Rejected: the same missing ground truth, plus a pinned vendored copy or a network fetch in the eval path, a licence to carry, and a size that makes every eval case expensive before the agents have been shown to work at all.

## Decision

**Target.** `fixtures/src/legacy-billing/`, in the shape every fixture has (`base/`, `head/`, `defects.yaml`), built by `uv run python -m fixtures.build legacy-billing`. #111 writes it, because its tools need "fixture repositories with planted dead code and a known call chain", and #112 runs its eval cases on the same tree. It is a Python package of roughly fifteen to twenty-five modules for invoicing and billing, written in the style the agents are meant to untangle: one oversized module, module-level state shared across modules, imports inside functions, helpers copied between modules and a dispatch table keyed by strings. It must hold:

- **Planted dead code** of several kinds: a function nothing calls, a class nothing uses, a method nothing calls, a module nothing imports, and a function reached only from other dead code.
- **Decoys that are not dead**: a function called only from the tests, a function reached only through the string-keyed dispatch table, and a name re-exported through `__init__.py`. A tool or agent that reports one of these as dead is wrong, and a call a static reading cannot resolve is reported as unresolved, never guessed.
- **A known call chain** at least four calls deep, from the command-line entry point down to storage.
- **Known data flows** between named modules.
- **Two features bound into the monolith**, each with tests that pin its behaviour today, so the extraction agent has more than one thing to isolate.

Like the other fixtures, its own tests pass on both refs and its files pass ruff and ty, so planted dead code is unused definitions, not unused imports. The `head` commit only changes the README, as in `storefront` and `tracker`: the agents read a tree, not a diff.

**Ground truth.** Recorded in the fixture directory and checked against the tree by the fixture tests, so it cannot drift off the code. Dead code entries go in `defects.yaml` with the category `dead-code`. The call chain, the data flows, the module map and the decoys go in a second file beside it; #111 defines its schema.

**TypeScript.** The target is Python only. The TypeScript half of `deps.dead_code` and `deps.call_graph` is tested on small trees written inside the tool tests, and no agent eval depends on it.

**Agents built.** `code-archaeology@0.1` and `extraction-modularization@0.1`, as #112 scopes them.

**Agent dropped.** `black-box-behavioural` is dropped, not made a project of its own. It needs a running target and a fuzzing harness, the factory has neither, and on a target whose source the team can read it would tell the team nothing the other two agents do not. It needs no manifest, tool or eval, and no issue is open for it.

This decision would change on one of two things: the owner nominates a real repository as a second target, or a running system with no readable source turns up. The first calls for a new record that names the repository and how its ground truth is labelled; the second reopens the black-box agent as a separate project.

## Consequences

- #111 adds the `legacy-billing` fixture, its ground truth and the two tools; #112 adds the two agents and four eval cases each on that fixture.
- Evals on a synthetic target show that the agents find what was planted in a small codebase. They do not show that the agents hold up on a large or real one; the README of the fixture and the agents' eval notes say so.
- The "done when" of #119, a blueprint in the store as a `refactor` ticket the feature coder can take, is met against `legacy-billing`. That takes a live run, which is the operator's.
- **Never rename the fixture**, as for every fixture that eval cases name.
