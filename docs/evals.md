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
research agents are the only agents allowed to. The first is the planning agent `product-architect`,
which turns an idea or raw feedback into a PRD. The planning agent `story-breakdown` turns a PRD into
ordered tickets; its cases check the tickets' impacted paths with a `*` in `paths_exist` and score
the dependencies and the parallel-safety of the cut by rubric (#91).
The research agent `benchmark` compares candidate libraries for one need, and every number in its
table is a `Claim` that `citations_resolve` checks against the cited page (#105).
The research agent `spike` runs throwaway code in the networked spike sandbox to learn an API's
real shape; its cases are graded by rubric on the observations it reports, each with the command
and the output, and by `citations_resolve` on the facts it read (#106).

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
