"""The `jev` grader: a Jev noul question decides whether an answer meets its case's expectation.

`JevJudge` has the signature of a `Judge`. It renders the case's expectation and the agent's answer
into the grader manifest (`jev_grader.yaml`), runs the one noul question through `run_jev`, and
passes the answer when the probability is over the threshold of the manifest's `matches` gate. The
score is that probability. Every call writes one Jev trace row under the eval's tag.

When the Jev run fails (`JevError`) the grader does not guess: it falls back to the `fallback`
judge, normally the `LLMJudge`, and marks the verdict `grader_fallback`, which `evals.run` reports
per case so a run graded by the other grader is visible. A missing or rejected key and an
exhausted budget are not fallbacks and propagate.

`python -m evals.graders.jev` runs both graders over the answers of real agents and reports how
often they agree; see "Grader agreement" in docs/jev-sdk.md.
"""

import argparse
import json
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from evals.cases import CASES_FILE, Expected, load_cases
from evals.graders.llm_judge import Judge, JudgeVerdict
from registry.jev_spec import JevSpec, ProbabilityGate
from runtime.jev_runner import JevError, JevTransport, run_jev
from runtime.trace import Trace

GRADER_MANIFEST = Path(__file__).with_name("jev_grader.yaml")
"""The grader's manifest, next to this module."""

MATCH_GATE = "matches"
"""The name of the gate whose value is the pass threshold."""


class JevVerdict(JudgeVerdict):
    """A `JudgeVerdict` that also says whether the other grader produced it.

    `grader_fallback` is true when the Jev run failed and the fallback judge graded the answer.
    This class is never sent to a model as a schema; `JudgeVerdict` still is.
    """

    grader_fallback: bool = False


@cache
def load_grader_spec() -> JevSpec:
    """Load and validate the grader manifest, and check that it has the `matches` gate.

    Raises `ValueError` when the manifest has no `matches` gate on a noul question.
    """
    data = yaml.safe_load(GRADER_MANIFEST.read_text(encoding="utf-8"))
    spec = JevSpec.model_validate({**data, "path": GRADER_MANIFEST})
    threshold_of(spec)
    return spec


def threshold_of(spec: JevSpec) -> float:
    """Return the pass threshold: the value of the `matches` gate of `spec`.

    Raises `ValueError` when there is no such gate or it is not a `p_above` rule.
    """
    for gate in spec.gates:
        if gate.name == MATCH_GATE:
            if isinstance(gate, ProbabilityGate) and gate.rule == "p_above":
                return gate.value
            break
    raise ValueError(f"{spec.address}: needs a `{MATCH_GATE}` gate with rule p_above")


class JevJudge:
    """Grade an answer with the Jev grader agent, falling back to `fallback` when Jev fails.

    `trace`, `tag` and `pipeline_run_id` go to the Jev trace row. `transport` is for tests; by
    default the SDK transport is used. It is safe to call from several threads when `trace` is.
    """

    def __init__(
        self,
        trace: Trace,
        fallback: Judge,
        *,
        tag: str | None = None,
        pipeline_run_id: str | None = None,
        transport: JevTransport | None = None,
        spec: JevSpec | None = None,
    ) -> None:
        self._trace = trace
        self._fallback = fallback
        self._tag = tag
        self._pipeline_run_id = pipeline_run_id
        self._transport = transport
        self._spec = spec or load_grader_spec()
        self._threshold = threshold_of(self._spec)

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JevVerdict:
        """Return the verdict on `actual`; `grader_fallback` is set when the fallback decided."""
        payload = {
            "expected": json.dumps(
                expected.model_dump(mode="json", exclude_none=True, exclude={"run"}), indent=2
            ),
            "actual": json.dumps(dict(actual), indent=2),
        }
        try:
            output = run_jev(
                self._spec,
                payload,
                self._trace,
                pipeline_run_id=self._pipeline_run_id,
                transport=self._transport,
                tag=self._tag,
            )
        except JevError as exc:
            verdict = self._fallback(expected, actual)
            reason = f"jev grader failed ({exc.message}); {verdict.reason}"
            return JevVerdict(
                passed=verdict.passed, score=verdict.score, reason=reason, grader_fallback=True
            )
        p = output.model_dump()[MATCH_GATE]["p"]
        return JevVerdict(
            passed=p > self._threshold,
            score=p,
            reason=f"jev grader: p(matches) {p:.2f}, threshold {self._threshold:.2f}",
        )


@dataclass
class Agreement:
    """How the two graders compared on one agent's answers.

    `pairs` holds `(llm_passed, jev_passed, jev_fell_back)` for every answer that reached the
    graders, that is, passed the deterministic checks.
    """

    agent: str
    pairs: list[tuple[bool, bool, bool]] = field(default_factory=list)

    @property
    def compared(self) -> int:
        """Answers graded by both."""
        return len(self.pairs)

    @property
    def agreed(self) -> int:
        """Answers both graders passed or both failed."""
        return sum(llm == jev for llm, jev, _ in self.pairs)

    @property
    def fallbacks(self) -> int:
        """Answers where the Jev grader failed and the LLM judge decided for both."""
        return sum(fell_back for _, _, fell_back in self.pairs)

    def to_json(self) -> dict[str, Any]:
        """Return the numbers as a JSON-ready dict."""
        return {
            "agent": self.agent,
            "compared": self.compared,
            "agreed": self.agreed,
            "agreement": self.agreed / self.compared if self.compared else None,
            "fallbacks": self.fallbacks,
        }


class _Both:
    """A `Judge` that asks both graders and keeps the pair; the LLM judge's verdict is returned.

    Returning the shipped grader's verdict means a comparison run grades exactly as an eval does.
    """

    def __init__(self, llm: Judge, jev: Judge, agreement: Agreement) -> None:
        self._llm = llm
        self._jev = jev
        self._agreement = agreement
        self._lock = threading.Lock()

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        llm = self._llm(expected, actual)
        jev = self._jev(expected, actual)
        fell_back = isinstance(jev, JevVerdict) and jev.grader_fallback
        with self._lock:
            self._agreement.pairs.append((llm.passed, jev.passed, fell_back))
        return llm


def compare(refs: Sequence[str], trace: Trace) -> list[Agreement]:
    """Run each agent in `refs` once per case and compare the two graders on its answers.

    Each answer that passes the deterministic checks is graded by the `LLMJudge` and the `JevJudge`
    both, and the pair is recorded. Cases run one at a time. This calls live models.
    """
    from evals.graders.llm_judge import LLMJudge
    from evals.run import evaluate, resolve_ref

    results = []
    for ref in refs:
        spec = resolve_ref(ref)
        agreement = Agreement(spec.address)
        tag = f"eval-compare:{spec.address}"
        llm = LLMJudge(trace, tag=tag)
        try:
            both = _Both(llm, JevJudge(trace, llm, tag=tag), agreement)
            evaluate(
                spec,
                load_cases(spec.directory / CASES_FILE),
                trace,
                judge=both,
                jev_judge=both,
                workers=1,
            )
        finally:
            llm.close()
        results.append(agreement)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Run `python -m evals.graders.jev [REF ...] [--json]` and print the agreement per agent."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.graders.jev",
        description="Grade the answers of real agents with the LLM judge and the Jev grader and "
        "report how often they agree. Calls live models.",
        epilog="example: python -m evals.graders.jev code-reviewer@^1.2 judge-gate@^0.1 --json",
    )
    parser.add_argument("refs", nargs="*", help="agents as name@^M.m; default: every agent")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    refs = args.refs or _default_refs()
    with Trace() as trace:
        results = compare(refs, trace)
    if args.json:
        print(json.dumps([result.to_json() for result in results]))
    else:
        for result in results:
            rate = f"{result.agreed / result.compared:.0%}" if result.compared else "n/a"
            print(
                f"{result.agent}: {result.agreed}/{result.compared} agree ({rate}), "
                f"{result.fallbacks} fell back"
            )
    return 0


def _default_refs() -> list[str]:
    """Every agent that has eval cases and does not run in the sandbox, at its highest version."""
    from evals.run import resolve_ref
    from registry.resolver import list_agents

    refs = []
    for name in sorted({found.name for found in list_agents()}):
        spec = resolve_ref(name)
        if not getattr(spec, "sandbox", False) and (spec.directory / CASES_FILE).exists():
            refs.append(spec.address)
    return refs


if __name__ == "__main__":
    sys.exit(main())
