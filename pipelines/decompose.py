"""From an extraction blueprint to a `refactor` ticket in the work-item store.

`extraction-modularization` (team `decomp`) returns an `ExtractionBlueprint`. Nothing in this module
runs an agent or calls a model: it renders the blueprint as markdown, makes a `Ticket` from it and
stores the ticket, so that the build loop can route the item to `refactorer` (ADR 0002).

The blueprint is the model, and the markdown is made from it in code. The body of the ticket holds
both: the markdown for a reader and, after `JSON_HEADING`, the JSON that `blueprint_of` reads back
to an equal blueprint. The prose in a blueprint is written by a model, so in the markdown each entry
is one line and cannot open a code fence; the JSON keeps the text as it was given.
"""

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime

from pydantic import ValidationError

from factory.exit import UsageError
from registry.common_schemas import Ticket
from registry.decomposition_schemas import ExtractionBlueprint
from runtime.trace import Trace
from runtime.work_items import WorkItem, WorkItems

ACTOR = "pipeline:decompose"
"""The actor of the ticket's events: ADR 0002's `pipeline:<module>`."""

JSON_HEADING = "## Blueprint as JSON"
"""The heading of the part of a ticket's body that `blueprint_of` reads."""

TITLE_CHARS = 120
"""The longest title `Ticket` takes."""

_JSON_OPEN = f"\n{JSON_HEADING}\n\n```json\n"
_JSON_CLOSE = "\n```"
_FENCE = re.compile(r"`{3,}|~{3,}")


def render_blueprint(blueprint: ExtractionBlueprint) -> str:
    """Return `blueprint` as markdown, one section per field; an empty list is said to be empty."""
    sections = [
        f"Extract the feature {_line(blueprint.feature)}, which starts at `{blueprint.start}`.",
        f"Summary: {_line(blueprint.summary)}",
        _section("Inputs", blueprint.inputs, _line),
        _section("Outputs", blueprint.outputs, _line),
        _section("Invariants", blueprint.invariants, _line),
        _section(
            "Symbols that move",
            blueprint.symbols,
            lambda entry: f"`{entry.symbol}`: {_line(entry.role)}",
        ),
        _section(
            "Outside symbols the feature uses",
            blueprint.uses,
            lambda entry: f"`{entry.symbol}`, used by `{entry.used_by}`: {_line(entry.need)}",
        ),
        _section(
            "Outside code that calls the feature",
            blueprint.called_from,
            lambda entry: f"`{entry.caller}` calls `{entry.callee}`: {_line(entry.expects)}",
        ),
        _section(
            "Shared state",
            blueprint.state,
            lambda entry: (
                f"`{entry.symbol}` {entry.access.replace('_', ' ')} {_line(entry.state)}. "
                f"Cut: {_line(entry.cut)}"
            ),
        ),
        _section(
            "Tests that pin the behaviour",
            blueprint.pinning_tests,
            lambda entry: f"`{entry.path}`: {_line(entry.pins)}",
        ),
        _section("Open questions", blueprint.open_questions, _line),
    ]
    return "\n\n".join(sections) + "\n"


def ticket_body(blueprint: ExtractionBlueprint) -> str:
    """Return the body of the ticket for `blueprint`: its markdown, then its JSON."""
    # `json.dumps` escapes every newline inside a string, so no line of it can close the fence.
    text = json.dumps(blueprint.model_dump(mode="json"), indent=2)
    return f"{render_blueprint(blueprint)}{_JSON_OPEN}{text}{_JSON_CLOSE}\n"


def blueprint_of(body: str) -> ExtractionBlueprint:
    """Return the blueprint in a ticket's `body`, as `ticket_body` wrote it.

    Text added below the JSON is ignored. Raises `ValueError` when the body has no such part or
    when what it holds is not an `ExtractionBlueprint`.
    """
    _, opened, rest = body.partition(_JSON_OPEN)
    text, closed, _ = rest.partition(_JSON_CLOSE)
    if not opened or not closed:
        raise ValueError(f"the body has no JSON block under {JSON_HEADING!r}")
    try:
        return ExtractionBlueprint.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(
            f"the JSON block under {JSON_HEADING!r} is not a blueprint: {exc}"
        ) from exc


def extraction_ticket(blueprint: ExtractionBlueprint, *, repo: str, ref: str) -> Ticket:
    """Return the `refactor` ticket that asks for the extraction `blueprint` describes.

    The acceptance criteria are the invariants, then one entry per pinning test file, which has to
    pass unchanged. The impacted paths are the modules the feature's symbols are in. A feature name
    too long for a title is cut there; the body has it whole. Raises `UsageError` when `repo` or
    `ref` is not one a `Ticket` takes.
    """
    tests: dict[str, list[str]] = {}
    for test in blueprint.pinning_tests:
        tests.setdefault(test.path, []).append(_line(test.pins))
    acceptance = [
        *(_line(invariant) for invariant in blueprint.invariants),
        *(f"{path} passes unchanged: {'; '.join(pins)}" for path, pins in tests.items()),
    ]
    modules = [entry.symbol.partition("::")[0] for entry in blueprint.symbols]
    try:
        return Ticket(
            kind="refactor",
            title=_title(blueprint.feature),
            body=ticket_body(blueprint),
            acceptance=_once(acceptance),
            repo=repo,
            ref=ref,
            impacted_paths=_once(modules),
        )
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise UsageError(f"the blueprint does not make a ticket: {reasons}") from exc


def file_extraction_ticket(
    trace: Trace,
    blueprint: ExtractionBlueprint,
    *,
    repo: str,
    ref: str,
    pipeline_run_id: str,
    now: Callable[[], datetime] | None = None,
) -> WorkItem:
    """Store the ticket for `blueprint` and return the work item, in `todo`.

    `pipeline_run_id` is the run that made the blueprint; the item is linked to it, so the run's
    cost counts as the item's. Raises `UsageError` when that is blank or when `extraction_ticket`
    does, and then stores nothing.
    """
    if not pipeline_run_id.strip():  # `add` takes a blank id that `link_run` would then refuse.
        raise UsageError("the pipeline run id must not be empty")
    ticket = extraction_ticket(blueprint, repo=repo, ref=ref)
    store = WorkItems(trace, now=now)
    item = store.add(
        ticket,
        actor=ACTOR,
        pipeline_run_id=pipeline_run_id,
        reason=f"filed from the extraction blueprint of {_line(blueprint.feature)}",
    )
    return store.link_run(item.id, pipeline_run_id)


def _title(feature: str) -> str:
    title = f"Extract {_line(feature)}"
    return title if len(title) <= TITLE_CHARS else f"{title[: TITLE_CHARS - 1].rstrip()}…"


def _section[T](heading: str, entries: Iterable[T], line: Callable[[T], str]) -> str:
    """Return a section with one list item per entry."""
    items = [f"- {line(entry)}" for entry in entries]
    return "\n".join([f"## {heading}", "", *(items or ["None."])])


def _line(text: str) -> str:
    """Return `text` on one line, with no run of backticks or tildes that could open a fence."""
    return _FENCE.sub(lambda match: match[0][0], " ".join(text.split()))


def _once(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
