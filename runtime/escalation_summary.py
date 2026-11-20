"""What the escalation-summary agent reads, and the check its output must pass (#94).

`summary_input` cuts an escalation record down to the agent's input: the question, the trigger,
the item's state and cost, and the diff, agent outputs and suite tail capped again, smaller than
the record keeps them, so the prompt stays small enough for a cheap model.

`ungrounded` is the check behind the rule that every option the summary names appears in the
record. Each option carries `evidence`, a span the agent copied from the record; the check passes
when every span is found, word for word, in one place of the record, and when every dollar figure
the summary writes is one the record holds. It runs in two places with the same code: in the
eval, as `expected.options_in_record` (`evals.graders.grade`), and in `runtime.after_escalation`
before a summary is stored. A summary that fails it is not stored. The rules are in
`docs/CONVENTIONS.md` (Escalations, Summaries).
"""

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from runtime.escalations import Escalation, cut_head, cut_tail

SUMMARY_DIFF_CHARS = 6000
"""The longest diff the agent reads. A longer one keeps its head."""

SUMMARY_OUTPUT_CHARS = 3000
"""The longest text the agent reads of each agent output. A longer one keeps its head."""

SUMMARY_SUITE_TAIL_CHARS = 2000
"""The longest suite output the agent reads. A longer one keeps its end."""

EVIDENCE_MIN_CHARS = 12
"""The shortest evidence span that counts. A shorter one could match almost anywhere."""

COST_TOLERANCE_USD = 0.005
"""How far a dollar figure may be from the record's `cost_usd` and still count as quoting it."""

_DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")


def summary_input(record: Escalation) -> dict[str, Any]:
    """Return the agent's input for `record`, in the shape of `EscalationSummaryInput`.

    The record's id, item, trigger, question, state before the stop and cost are kept as they
    are. The diff keeps its first `SUMMARY_DIFF_CHARS` characters, each agent output its first
    `SUMMARY_OUTPUT_CHARS` and the suite output its last `SUMMARY_SUITE_TAIL_CHARS`, the cut
    marked in the text. The actor, the run ids and the answer are left out: the agent has no use
    for them.
    """
    return {
        "id": record.id,
        "work_item_id": record.work_item_id,
        "trigger": record.trigger,
        "question": record.question,
        "from_status": record.from_status,
        "cost_usd": record.cost_usd,
        "diff": cut_head(record.diff, SUMMARY_DIFF_CHARS),
        "agent_outputs": [
            {
                "agent": output.agent,
                "run_id": output.run_id,
                "output": cut_head(output.output, SUMMARY_OUTPUT_CHARS),
            }
            for output in record.agent_outputs
        ],
        "suite_tail": cut_tail(record.suite_tail, SUMMARY_SUITE_TAIL_CHARS),
    }


def ungrounded(summary: Mapping[str, Any], record: Mapping[str, Any]) -> list[str]:
    """Return why the summary is not grounded in the record, one message per problem.

    An empty list means it passes. `summary` is the agent's output as a JSON object and `record`
    either a record's `to_json()` or the agent's input; both have the keys this reads:
    `question`, `diff`, `suite_tail`, `agent_outputs` (each with `output`) and `cost_usd`.

    An option's `evidence` appears in the record when, with runs of whitespace taken as one space
    on both sides, it is at least `EVIDENCE_MIN_CHARS` characters long and a substring of one
    single text of the record: the question, the diff, the suite tail or one agent's output. An
    output that is JSON also offers each of its string values as a text, so a span quoted from a
    decoded value counts. Case, punctuation and quoting must match.

    A dollar figure (`$` and a number) in the paragraph, a label or a cost must equal one written
    in those texts, or be within `COST_TOLERANCE_USD` of `cost_usd`.
    """
    texts = [_flat(text) for text in _texts(record)]
    problems: list[str] = []
    options = summary.get("options") or []
    if not options:
        problems.append("the summary names no option")
    for index, option in enumerate(options):
        label = option.get("label", "")
        evidence = _flat(str(option.get("evidence", "")))
        if len(evidence) < EVIDENCE_MIN_CHARS:
            problems.append(
                f"options[{index}] ({label!r}): evidence is shorter than"
                f" {EVIDENCE_MIN_CHARS} characters"
            )
        elif not any(evidence in text for text in texts):
            problems.append(f"options[{index}] ({label!r}): evidence is not in the record")
    known = {figure for text in texts for figure in _figures(text)}
    cost = float(record.get("cost_usd") or 0.0)
    written = [("paragraph", str(summary.get("paragraph", "")))]
    for index, option in enumerate(options):
        written += [
            (f"options[{index}].label", str(option.get("label", ""))),
            (f"options[{index}].cost", str(option.get("cost", ""))),
        ]
    for where, text in written:
        for figure in _figures(text):
            if figure not in known and abs(figure - cost) > COST_TOLERANCE_USD:
                problems.append(f"{where}: ${figure:g} is not a figure in the record")
    return problems


def _texts(record: Mapping[str, Any]) -> Iterator[str]:
    for key in ("question", "diff", "suite_tail"):
        text = record.get(key)
        if isinstance(text, str) and text:
            yield text
    for output in record.get("agent_outputs") or []:
        text = output.get("output") if isinstance(output, Mapping) else None
        if not isinstance(text, str) or not text:
            continue
        yield text
        try:
            decoded = json.loads(text)
        except ValueError:
            continue
        yield from _leaves(decoded)


def _leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)


def _figures(text: str) -> list[float]:
    return [float(match.replace(",", "")) for match in _DOLLARS.findall(text)]


def _flat(text: str) -> str:
    return " ".join(text.split())
