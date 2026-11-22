# Evals

Every agent has an `evals/cases.yaml` next to its manifest, and `factory eval <name>@<range>` runs
it. Each case names a fixture repository, the payload the agent gets, what it is expected to
answer, and the grader that decides whether it did. The case schema is in `evals/cases.py` and the
conventions for it are in docs/CONVENTIONS.md, under "Eval cases". This page says which grader an
agent may use.

## Ground truth first

An eval is only as good as what it compares against. The `llm_judge`, `sandbox_tests`, `incident`,
`ground_truth` and `jev` graders all start from ground truth that the case states: a verdict,
planted defects at known lines, tests that must fail and then pass, entries of a fixture's
`ground-truth.yaml`, or the option a Jev question must pick. The judge model only grades what is
left once those deterministic checks pass.

The following agents keep ground-truth evals and must not use the `rubric` grader:

- the reviewers (`code-reviewer`, `security-reviewer`, `perf-reviewer` and any later one);
- the fixers and the test-writer;
- the judge;
- `code-archaeology`, whose account of a codebase is checked against that codebase;
- the Jev agents (`diff-triage`, `judge-gate`, `action-guard` and any later `kind: jev` agent).

Their outputs have a right answer, and a case that does not check it measures nothing.

## The `rubric` grader

Some agents write prose with no single right answer: a plan, a ticket, documentation, a research
report. For those, and only those, a case may use `grader: rubric`. Planning, documentation and
research agents are the only agents allowed to. The first is the planning agent `product-architect`,
which turns an idea or raw feedback into a PRD. The planning agent `story-breakdown` turns a PRD into
ordered tickets; its cases check the tickets' impacted paths with a `*` in `paths_exist` and score
the dependencies and the parallel-safety of the cut by rubric (#91).
The research agent `benchmark` compares candidate libraries for one need, and every number in its
table is a `Claim` that `citations_resolve` checks against the cited page (#105).
The research agent `spike` runs throwaway code in the networked spike sandbox to learn an API's
real shape; its cases are graded by rubric on the observations it reports, each with the command
and the output, and by `citations_resolve` on the facts it read (#106).

Two agents outside those teams may use the rubric. The first is `escalation-summary` (#94, team
`hitl`). It writes a paragraph and a few options for a person to choose from an escalation record,
prose that has no single right answer. Its ground truth is not the prose but the record itself:
each case sets `expected.options_in_record`, and the case fails, without a model call, when an
option's evidence is not a span of the record or a dollar figure was invented (see below).

The second is `extraction-modularization` (#112, team `decomp`). Its blueprint states a feature's
inputs, outputs and invariants in words, which have no single right answer. The boundary it draws
does have one, so every case carries an `expected.ground_truth` block, and an answer whose boundary
or ties are wrong fails without a judge call ([below](#the-ground_truth-grader)).

A rubric case has `expected.verdict: null` and an `expected.rubric` block:

```yaml
- id: rollout-plan
  fixture: clean-refactor
  payload: {goal: "Plan the rollout of the new pricing module."}
  expected:
    verdict: null
    rubric:
      criteria:
        - id: names-the-risk
          text: The plan names what could break for current customers.
          required: true
          floor: 0.6
        - id: has-steps
          text: The plan is a list of steps a person can follow in order.
        - id: has-rollback
          text: The plan says how to undo the change.
      threshold: 0.7
      required_fields: [summary, steps]
      forbidden: [TODO, lorem ipsum]
      paths_exist: [files]
  grader: rubric
```

Grading runs in this order:

1. The output schema, as for every agent: an answer that does not validate never reaches a grader.
2. The deterministic checks of the rubric, with no model call:
   - each of `required_fields`, a dotted path through the output, must be present and not empty;
   - each of `empty_fields` must be present and empty (null, blank or an empty list), for an
     answer that must hold something back, such as a plan with no goals for a request that
     contradicts itself; a field cannot be in both lists;
   - no string anywhere in the output may contain one of `forbidden` (case-sensitive);
   - each of `paths_exist`, a dotted path whose value is a path or a list of paths, must name
     files or directories that exist inside the fixture repository. A `*` part walks every item
     of a list, so `tickets.*.impacted_paths` checks the paths of every ticket; the path fails
     when the list is missing, a part is absent from any item, or a `*` meets something that is
     not a list.

   A failure fails the case with the failures as the reason, and the grader model is not called.
3. The grader model, Haiku (`claude-haiku-5-5`), scores each criterion from 0 to 1 with a reason,
   using a fixed system prompt (`evals/graders/rubric.py`). It is sent the criteria and the answer,
   and never sees the threshold or the floors.
4. The pass decision is code: the case passes when the mean of the scores is at or above
   `threshold` and every `required` criterion scores at or above its own `floor`. The reason names
   the mean, the threshold and each failing criterion with the grader's reason.

A grader that answers with scores for other criteria than it was given is asked once more, and a
second bad answer, a refusal or an API error is a case error (`RubricError`), never a pass. Each
grader call writes one trace row, `agent_ref` `eval-rubric@claude-haiku-5-5`, under the eval's tag
(`eval:<name>@<version>`) and pipeline run id, with its cost, so `factory eval` reports the
grader's spend with the agent's.

Write criteria that a reader could check against the answer alone, one property each. A criterion
such as "the plan is good" scores whatever the model feels; "the plan says how to undo the change"
scores what the plan says. Mark a criterion `required` when an answer without it is wrong however
well it does on the rest.

## A rubric on a `sandbox_tests` case

An agent graded on the work tree it leaves may still owe a reader some prose. A `sandbox_tests`
case may then add an `expected.rubric` block, of the same shape, as an extra check on the answer
(#86). The work tree stays the ground truth: the case is graded first by its `expected.run` checks
(the suite, the new tests, what changed), then by the rubric's deterministic checks against the
case's work tree, then by the rubric's model, and the first failure stops it. `grader` stays
`sandbox_tests`; this does not let a coder or a fixer use `grader: rubric`.

The `feature-coder` (#86) is the first agent to do so. Its four cases each give it a ticket on a
fixture. Three must leave a passing suite, passing tests in the files the answer lists, and changed
code and tests, and the rubric checks that every acceptance criterion maps to a test and that the
summary says what was built and where. The fourth is a ticket whose criteria contradict the code:
nothing may change, and the rubric checks that the answer is blocked and names the contradiction.
The rubric model never sees the ticket, so each criterion restates what it needs from it.

## A refactor case has no rubric

The `refactorer` (#98) is graded on the work tree only. Its four cases each give it a refactor
ticket on a fixture with a planted smell: an N-plus-one query (`n-plus-one`), duplicate code
(`refactor-duplicate`), a deprecated call (`refactor-deprecated`) and magic numbers
(`refactor-magic-numbers`). Each must leave a passing suite, changed production code and unchanged
tests, and its `expected.run.public_api` lists the ticket's target files, so the case fails when a
public name or signature in them differs from the fixture's HEAD (`evals/public_api.py`, which reads
both with `ast`). These checks are deterministic and decide the case; the `criteria` the judge reads
afterwards check only that the answer says what changed and what stayed the same.

## The `citations_resolve` check

`rubric` scores how well a research agent reasons; it cannot tell whether the facts are true.
`citations_resolve` covers that. It is a deterministic check, not a grader: a rubric case turns it
on with `expected.citations_resolve`, and only a `rubric` case may.

**Which agents need it.** Every research agent: any agent whose output states facts about the
world taken from web pages (issues #104 and #105 are the first). A research agent publishes only
when its cases use both, `rubric` for the reasoning and `citations_resolve` for the truth, and at
least one case must set `citations_resolve`. Planning and documentation agents, which state no
sourced facts, use `rubric` alone. Reviewers, fixers, the judge and the Jev agents use neither.

**How an output marks a fact.** The output model holds a list of `Claim`
(`registry/common_schemas.py`), each with `text`, `fact` and `citations`. A claim with
`fact: true` must have at least one `Citation` (`url`, `quote`, `retrieved_at`) and the schema
rejects it otherwise; a claim with `fact: false` (an opinion, a recommendation, a summary of the
agent's own reasoning) may cite nothing and is not checked.

```yaml
  expected:
    verdict: null
    rubric: {criteria: [...], threshold: 0.7}
    citations_resolve:
      claims: claims            # dotted path to the list of claims; this is the default
      allow_domains: [docs.python.org, peps.python.org]
  grader: rubric
```

Order of grading: the output schema, the rubric's deterministic checks, `citations_resolve`, then
the grader model. A case that fails an earlier step never reaches a later one, so a false quote
costs no grading call.

What it does, for each citation of each claim marked as a fact:

1. The URL's host must be one of `allow_domains` or a subdomain of one, the same rule the runtime
   applies to an agent run (`tools.web.check_web_call`). `allow_domains` is the case's own list
   and cannot be empty.
2. The page is fetched through `web.fetch` (`agb web fetch --markdown`), once per URL per case.
3. The quote must appear in the page text. Both are normalised the same way first, and nothing
   else is forgiven: the untrusted-data block around the page is removed, Unicode NFKC is applied,
   Markdown links and images become their text, heading, quote, list and number markers at the
   start of a line are dropped, `*`, backticks and emphasis underscores (not those inside a word)
   are dropped, curly quotes and en and em dashes become ASCII, and every run of whitespace
   becomes one space. Case, word order and every word are kept, so a quote that is absent, or
   paraphrased, fails.

The outcomes:

- A quote that is not in its page, a fact with no citation, an output with no claim marked as a
  fact, or a missing or non-list `claims` path **fails** the case. The reason lists each failed
  citation by its path, such as `claims.2.citations.0`.
- A URL outside `allow_domains`, an invalid URL, or a fetch that fails or times out is a **case
  error** (`CitationCheckError`), never a pass and never a failed quote: the check could not be
  made. A missing `agb` ends the eval, as for every tool.

The fetcher is injectable (`evaluate(..., fetcher=...)`, a function from URL to page text), so
tests and calibration runs need no network.

**The case's domains are also the run's.** When a case sets `citations_resolve.allow_domains`,
the eval runner runs the agent on that case with that list in place of the manifest's
`tools.web.allow_domains` (`LLMSpec.with_web_domains`). The case list replaces the manifest list,
it is not merged with it, so the agent can reach exactly the hosts the check will accept and a
case can point a general research agent at the sources its question needs. A case without the
check runs with the manifest list. The override never hands out web tools: the manifest must
still allow `web.fetch` or `web.search` with a non-empty list, and for an agent whose manifest
lists no domain the case list is ignored. Entries are bare lowercase hosts, as in the manifest,
and a case with any other entry fails to load. `factory run --allow-domain` is the same override
for one run outside the eval.

## The `options_in_record` check

`expected.options_in_record: true` turns on a deterministic step of a `rubric` case, for
`escalation-summary` (#94). It runs after the rubric's own checks and before the grader model, and
calls `runtime.escalation_summary.ungrounded(answer, payload)`, where the payload is the cut-down
escalation record the agent was given. The case fails when the answer names no option, when an
option's `evidence` is shorter than 12 characters or is not a substring of one single text of the
record (the question, the diff, the suite tail, one agent output or one string value of an output
that is JSON; runs of whitespace count as one space, nothing else is normalised), or when a dollar
figure in the summary is neither written in those texts nor within $0.005 of the record's
`cost_usd`.

The same function runs before a summary is stored (`runtime.after_escalation`), and a summary that
fails it is not stored, so the eval measures exactly the rule the pipeline enforces. Only a
`rubric` case may set the flag; `evals/cases.py` refuses it on any other grader. The four cases are
hand-written records, one per trigger (`blocked_output`, `budget`, `changes_requested`,
`check_failed`); docs/CONVENTIONS.md (Escalations, Summaries) has the rest of the rules.

## The `incident` grader

triage-diagnostics (#108) is graded by `incident` (`evals/graders/incident.py`). Its cases pair a
fixture that plants a crash on `head` with a hand-written Sentry event for that crash, in the shape
`sentry.read_issue` returns; no case reads Sentry, and `head` plays the release tag. The ground
truth is in the case:

```yaml
  expected:
    verdict: null
    incident:
      suspected_path: reviews/scores.py   # the file of the planted fault
      exception: ZeroDivisionError        # the class the event shows
      crash_file: reviews/scores.py       # the file of the frame that raised it
  grader: incident
```

A case passes on two checks, in this order, and the grader calls no model:

1. The answer's `suspected_paths` list `suspected_path`.
2. The answer's `reproduction_test` reproduces the event. Its code is written at its path into a
   fresh copy of the fixture at `head` and run with pytest in the offline sandbox. It reproduces
   the event when pytest exits 1, the output has an `E   <exception>` line (a module prefix is
   allowed), and the traceback has a line in `crash_file`. A test that fails on an assertion, or
   that raises the right exception from another file, does not.

`suspected_path` and `crash_file` differ when the fault is above the raising frame, as in the
`crash-loader-caller` case, where the loader no longer skips a feed line with no price and
`int("")` raises one frame below it. The rule in step 2 is `reproduces`, and `factory incident` applies the same function to the
tests test-writer writes ([incident.md](incident.md)), so the eval scores what the pipeline counts.
The grader needs Docker and the sandbox image.

## The `ground_truth` grader

code-archaeology (#112) is graded by `ground_truth` (`evals/graders/ground_truth.py`). Its cases run
on a fixture that holds a `ground-truth.yaml` ([fixtures/README.md](../fixtures/README.md#ground-truthyaml)),
which the grader loads by the case's fixture name. A case names the entries to check by id, and
repeats nothing else of that file:

```yaml
  expected:
    verdict: null
    ground_truth:
      dead_code: [dead-function, dead-module]
      data_flows: [flow-usage-to-invoice]
      call_chains: [chain-billing-run]
      dynamic_calls: [dynamic-hooks]
  grader: ground_truth
```

Every list is optional, and at least one id must be given. The checks are exact, every failure names
the entry's id, and the grader calls no model: an answer that passes its checks passes, with score
1.0. The grader reads the `dead_code`, `data_flows` and `execution_paths` of the answer, and for
`features` the fields of an `ExtractionBlueprint`. No prose is compared, and neither are line
numbers, module roles or, except where a rule says so, the extra entries an answer may have.

- `dead_code`: `dead_code` of the answer has an entry with the same symbol and the same `kind`.
- Decoys: no `dead_code` entry of the answer is a decoy, or a class or module a decoy is in. This is
  checked against every decoy of the fixture in every case, whatever the case names. A decoy under
  `dynamic_lookups` is not called dead and is no failure.
- `data_flows`: one flow of the answer names every module the entry's steps are in, has every step's
  symbol except at most one (a flow of two steps may miss none), and reaches the entry's first
  module before its last.
- `call_chains`: every call of the chain is in the answer as a `resolved` call with the same caller
  and callee. The calls may be spread over several execution paths.
- `dynamic_calls`: the answer has an `unresolved` call whose `caller` is the entry's symbol and whose
  `expression` is the entry's call. Whitespace is ignored, and the expression matches when it
  contains the entry's snippet or has the same text before its first `(`.
- Not guessed: no `resolved` call of the answer goes from a dynamic call's site to one of its
  targets. This too is checked in every case, for every dynamic call of the fixture.
- `features`: the blueprint's `symbols`, the callers in its `called_from`, the symbols in its
  `state` and the paths in its `pinning_tests` hold every one the entry lists, and its `uses` hold
  every one but at most one. Nothing in the entry's `uses` or `called_from` may be in the
  blueprint's `symbols`, by itself or through its class or module. Other extra entries are no
  failure.

A `rubric` case may carry `expected.ground_truth` as well. The same checks then run before its judge,
and a failed one ends the case; the cases of extraction-modularization name a feature this way. No
other grader takes the block. A case whose fixture has no ground
truth, or that names an id the file lacks in that section, is a usage error and not a failed grade.

To check another section of the ground truth, add its field to `ExpectedGroundTruth` in
`evals/cases.py`, a function to `evals/graders/ground_truth.py`, and a call to it in
`check_ground_truth`.

The target, `legacy-billing`, is synthetic and small. A pass rate on it shows that an agent finds what
was planted there. It does not show that the agent holds up on a large or a real codebase
([ADR 0005](decisions/0005-decomposition-target.md)).
