# Evals

Every agent has an `evals/cases.yaml` next to its manifest, and `factory eval <name>@<range>` runs
it. Each case names a fixture repository, the payload the agent gets, what it is expected to
answer, and the grader that decides whether it did. The case schema is in `evals/cases.py` and the
conventions for it are in docs/CONVENTIONS.md, under "Eval cases". This page says which grader an
agent may use.

## Ground truth first

An eval is only as good as what it compares against. The `llm_judge`, `sandbox_tests` and `jev`
graders all start from ground truth that the case states: a verdict, planted defects at known
lines, tests that must fail and then pass, or the option a Jev question must pick. The judge model
only grades what is left once those deterministic checks pass.

The following agents keep ground-truth evals and must not use the `rubric` grader:

- the reviewers (`code-reviewer`, `security-reviewer`, `perf-reviewer` and any later one);
- the fixers and the test-writer;
- the judge;
- the Jev agents (`diff-triage`, `judge-gate`, `action-guard` and any later `kind: jev` agent).

Their outputs have a right answer, and a case that does not check it measures nothing.

## The `rubric` grader

Some agents write prose with no single right answer: a plan, a ticket, documentation, a research
report. For those, and only those, a case may use `grader: rubric`. Planning, documentation and
research agents are the only agents allowed to.

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
   - no string anywhere in the output may contain one of `forbidden` (case-sensitive);
   - each of `paths_exist`, a dotted path whose value is a path or a list of paths, must name
     files or directories that exist inside the fixture repository.

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
