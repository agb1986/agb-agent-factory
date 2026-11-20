"""`factory escalations`: list, show and answer the questions stopped pipelines asked (#93).

Each function here is one subcommand. It opens the trace store named by `FACTORY_TRACE_DB` (see
`runtime/trace.py`), works through `runtime.escalations`, prints text or one JSON document on
stdout, and returns 0. Failures are raised, not printed: the CLI's root handler maps
`InvalidEscalation` and `IllegalTransitionError` to exit 2 and `EscalationNotFound` to 3.
"""

import json
from pathlib import Path

from factory.exit import UsageError
from runtime import escalations
from runtime.trace import Trace
from runtime.work_items import WorkItems


def list_records(*, open_only: bool = False, as_json: bool = False) -> int:
    """Print every record, or only the open ones, oldest first. Returns 0, even for none.

    Text is one `id<TAB>open|answered<TAB>work item<TAB>trigger<TAB>first question line` line per
    record; JSON is `{"escalations": [...]}` with each record as `show --json` prints it.
    """
    with Trace() as trace:
        records = escalations.list_escalations(trace, open_only=open_only)
    if as_json:
        print(json.dumps({"escalations": [record.to_json() for record in records]}))
        return 0
    for record in records:
        state = "open" if record.is_open else "answered"
        first = record.question.splitlines()[0] if record.question else ""
        print(f"{record.id}\t{state}\t{record.work_item_id}\t{record.trigger}\t{first}")
    return 0


def show(escalation_id: str, *, as_json: bool = False) -> int:
    """Print one record as Markdown, or as one JSON document. Returns 0.

    The Markdown is `escalations.render_markdown`: the summary first when the record has one (the
    paragraph and the options with their evidence), then the question and a summary table, then
    the diff, the agent outputs and the suite tail in `<details>` sections. A record without a
    summary, because the agent failed, its output failed the check or the run's budget was spent,
    prints the record alone. The JSON is the record as `list --json` prints it, plus `summary`:
    the stored summary or null. Raises `EscalationNotFound` for an unknown id.
    """
    with Trace() as trace:
        record = escalations.get(trace, escalation_id)
        summary = escalations.get_summary(trace, escalation_id)
    if as_json:
        print(
            json.dumps(
                {**record.to_json(), "summary": summary.to_json() if summary is not None else None}
            )
        )
    else:
        print(escalations.render_markdown(record, summary))
    return 0


def answer(escalation_id: str, text: str, *, as_json: bool = False) -> int:
    """Store the answer `text`, or the file `@path` names, and print where the item went. Returns 0.

    The source is `cli` for text and `file:<path>` for a file. JSON is
    `{"escalation": {...}, "work_item": {"id", "status"}}`; text is one line saying whether the
    item moved to `ready` or still waits on other open records. Raises `UsageError` for an
    unreadable file and whatever `escalations.answer` raises: exit 2 for an empty or too-long
    answer, a record already answered or an item no longer escalated, exit 3 for an unknown id.
    """
    source = "cli"
    if text.startswith("@"):
        path = Path(text[1:])
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"cannot read the answer from {path}: {exc}") from exc
        source = f"file:{path}"
    with Trace() as trace:
        record = escalations.answer(trace, escalation_id, text, source)
        item = WorkItems(trace).get(record.work_item_id)
        others = escalations.list_escalations(trace, work_item_id=item.id, open_only=True)
        still_open = [other.id for other in others]
    if as_json:
        document = {
            "escalation": record.to_json(),
            "work_item": {"id": item.id, "status": item.status},
        }
        print(json.dumps(document))
    elif still_open:
        print(f"{record.id} answered; {item.id} stays {item.status}: {', '.join(still_open)} open")
    else:
        print(f"{record.id} answered; {item.id} is {item.status}")
    return 0
