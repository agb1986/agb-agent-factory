"""Gates: turn the answers of a Jev agent into a `Decision`, with thresholds from its manifest.

`decide` evaluates `JevSpec.gates` and `JevSpec.routing` on a `JevOutput`. The thresholds are the
manifest's and no model decides them. Three rules hold whatever the gates say: a truncated input
never skips, an escalation clears `skip`, and a Jev failure escalates (`jev_error`). See "Gates" in
docs/CONVENTIONS.md.
"""

from dataclasses import dataclass
from typing import Any

from registry.jev_spec import (
    AnswerInGate,
    ConfidenceGate,
    Gate,
    JevOutput,
    JevSpec,
    ProbabilityGate,
)

ROUTE_ONLY = True
"""While true, a pipeline logs the `Decision` and still runs everything.

Skipping a review or narrowing the reviewers needs calibrated thresholds, two weeks of shadow mode
and a measured recall on risky diffs (#20). Until then the decision is information only: the review
panel logs it and runs every selected reviewer. #20 turns this off together with the code that acts
on `skip` and `route`; the panel refuses to run with the constant off until that code exists.
"""

TRUNCATED = "truncated"
"""`Decision.reason` when the Jev input was cut and the answers cannot be trusted to skip."""

JEV_ERROR = "jev_error"
"""`Decision.reason` when the Jev run failed."""

NO_GATE = "no_gate"
"""`Decision.reason` when no gate fired and no routing entry matched."""

JUDGE_GATE = "judge_gate"
"""`Decision.reason` when the judge-gate agent was sure enough to skip the judge."""


@dataclass(frozen=True, slots=True)
class Decision:
    """What a gate decided about a change: skip it, escalate it, or route it to named agents.

    `route` holds agent names in order without duplicates. `reason` is the name of the gate that
    decided, or `truncated` or `jev_error`; when only routing matched it is that entry's `when`
    (such as `reviewers=code`), and `no_gate` when nothing matched.
    """

    skip: bool
    escalate: bool
    route: tuple[str, ...]
    reason: str

    def to_json(self) -> dict[str, Any]:
        """Return the decision as a JSON-ready dict, `route` as a list."""
        return {
            "skip": self.skip,
            "escalate": self.escalate,
            "route": list(self.route),
            "reason": self.reason,
        }


def jev_error() -> Decision:
    """Return the decision for a failed Jev run: escalate, skip nothing, route nowhere."""
    return Decision(skip=False, escalate=True, route=(), reason=JEV_ERROR)


def decide(spec: JevSpec, output: JevOutput, truncated: bool) -> Decision:
    """Evaluate the gates and routing of `spec` on `output` and return the `Decision`.

    A gate fires on a strict comparison: `p_above` when the probability is over its value,
    `p_below` and `confidence_below` when it is under, `answer_in` when the answer is listed. A
    fired gate with action `skip` sets `skip` and one with action `escalate` sets `escalate`; the
    reason is the first escalating gate, else the first skipping one. `route` is the union, in
    manifest order, of every routing entry whose `when` is a fired gate or the answer given.

    When `truncated` is true the answers rest on part of the input, so the result is
    `skip=False, escalate=True, route=()` with reason `truncated`: review everything. An
    escalation clears `skip`.
    """
    if truncated:
        return Decision(skip=False, escalate=True, route=(), reason=TRUNCATED)
    fired = [gate for gate in spec.gates if _fires(gate, output)]
    escalating = [gate.name for gate in fired if gate.action == "escalate"]
    skipping = [gate.name for gate in fired if gate.action == "skip"]
    matched = _matched_routes(spec, {gate.name for gate in fired}, output)
    route = tuple(dict.fromkeys(agent for _, agents in matched for agent in agents))
    reason = (escalating or skipping or [when for when, _ in matched] or [NO_GATE])[0]
    return Decision(
        skip=bool(skipping) and not escalating,
        escalate=bool(escalating),
        route=route,
        reason=reason,
    )


def decide_judge_gate(spec: JevSpec, output: JevOutput, truncated: bool) -> Decision:
    """Decide whether the panel may skip the judge, from the answers of the judge-gate agent.

    This is `decide` with one rename: a skip is reported with reason `judge_gate`, whichever skip
    gate of the manifest fired, so a trace row says the judge was skipped by the gate and not by
    which threshold. An escalation keeps the reason `decide` gives (the escalating gate, or
    `truncated`), and a truncated input never skips.
    """
    decision = decide(spec, output, truncated)
    if not decision.skip:
        return decision
    return Decision(skip=True, escalate=False, route=decision.route, reason=JUDGE_GATE)


def _fires(gate: Gate, output: JevOutput) -> bool:
    answer = getattr(output, gate.question)
    match gate:
        case ProbabilityGate(rule="p_above"):
            return _probability(gate, answer) > gate.value
        case ProbabilityGate():
            return _probability(gate, answer) < gate.value
        case ConfidenceGate():
            return answer.confidence < gate.value
        case AnswerInGate():
            return _picked(answer) in gate.value


def _probability(gate: ProbabilityGate, answer: Any) -> float:
    """The probability a gate reads: `p` of a noul answer, else `probs[option]`."""
    return answer.p if gate.option is None else answer.probs[gate.option]


def _picked(answer: Any) -> str:
    """The option a choice answer picked, or the level a score answer picked."""
    return answer.answer if hasattr(answer, "answer") else answer.level


def _matched_routes(
    spec: JevSpec, fired: set[str], output: JevOutput
) -> list[tuple[str, tuple[str, ...]]]:
    matched = []
    for entry in spec.routing:
        question, separator, answer = entry.when.partition("=")
        if separator:
            hit = _picked(getattr(output, question)) == answer
        else:
            hit = entry.when in fired
        if hit:
            matched.append((entry.when, entry.route))
    return matched
