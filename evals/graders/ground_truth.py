"""The `ground_truth` grader: check an answer about a codebase against a fixture's ground truth.

A case names entries of the fixture's `ground-truth.yaml` by id (`evals.cases.ExpectedGroundTruth`)
and copies none of their text. `load_truth` loads the file by the case's fixture name, and
`check_ground_truth` compares the answer, a `registry.decomposition_schemas.CodeArchaeologyOutput`
as a dict, with the entries named. Only symbols, kinds and call expressions are compared: no prose,
no line numbers, no order inside a list except the ends of a data flow. An answer that does not
hold the expected fields fails the checks and never raises.

Two checks need no id, because they say what no answer about the fixture may claim: no decoy may be
listed as dead code, and no dynamic call may be reported as resolved to one of its targets.

`docs/evals.md` states the matching rules for people writing cases. To check another section of the
ground truth, add a field of ids to `ExpectedGroundTruth`, a function here that returns its
failures, and a call to it in `check_ground_truth`.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from evals.cases import ExpectedGroundTruth
from factory.exit import UsageError
from fixtures.build import discover
from fixtures.ground_truth import CallChain, DataFlow, DeadCode, Decoy, DynamicCall, GroundTruth

_MAY_MISS = 1
"""How many steps of a data flow an answer may leave out and still describe it."""

_EXACT_STEPS = 2
"""A flow of this many steps is only its two ends, so none may be missing."""


class _Entry(Protocol):
    @property
    def id(self) -> str: ...


def load_truth(fixture: str) -> GroundTruth:
    """Load the ground truth of the fixture named `fixture`.

    Raises `UsageError` when there is no such fixture or it has no `ground-truth.yaml`, and
    `fixtures.schema.FixtureManifestError` when the file is invalid.
    """
    found = discover().get(fixture)
    if found is None:
        raise UsageError(f"expected.ground_truth: no fixture named {fixture!r}")
    truth = found.load_ground_truth()
    if truth is None:
        raise UsageError(f"expected.ground_truth: fixture {fixture} has no ground-truth.yaml")
    return truth


def check_ground_truth(
    expected: ExpectedGroundTruth, actual: Mapping[str, Any], truth: GroundTruth
) -> tuple[str, ...]:
    """Return why `actual` disagrees with `truth`, one message per entry; empty when it agrees.

    Each message names the id of its entry. Raises `UsageError` when `expected` names an id that is
    not in the section of `truth` it belongs to.
    """
    dead = _items(actual, "dead_code")
    flows = [_symbols(flow, "steps", "symbol") for flow in _items(actual, "data_flows")]
    calls = [call for path in _items(actual, "execution_paths") for call in _items(path, "calls")]
    return (
        *_check_dead_code(_named("dead_code", expected.dead_code, truth.dead_code), dead),
        *_check_decoys(truth.decoys, dead),
        *_check_data_flows(_named("data_flows", expected.data_flows, truth.data_flows), flows),
        *_check_call_chains(_named("call_chains", expected.call_chains, truth.call_chains), calls),
        *_check_dynamic_calls(
            _named("dynamic_calls", expected.dynamic_calls, truth.dynamic_calls), calls
        ),
        *_check_not_guessed(truth.dynamic_calls, calls),
    )


def _named[T: _Entry](section: str, ids: Sequence[str], entries: Iterable[T]) -> list[T]:
    """Return the entries of `section` with these ids, in the order of `ids`."""
    by_id = {entry.id: entry for entry in entries}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise UsageError(
            f"expected.ground_truth.{section}: no such entry in the ground truth: "
            f"{', '.join(unknown)}"
        )
    return [by_id[i] for i in ids]


def _check_dead_code(wanted: list[DeadCode], dead: list[Mapping[str, Any]]) -> list[str]:
    """An entry is found when the answer's `dead_code` lists its symbol with its kind."""
    failures: list[str] = []
    for entry in wanted:
        kinds = [item.get("kind") for item in dead if item.get("symbol") == entry.symbol]
        if not kinds:
            failures.append(f"dead code {entry.id}: {entry.symbol} is not listed as dead")
        elif entry.kind not in kinds:
            failures.append(
                f"dead code {entry.id}: {entry.symbol} is listed as {kinds[0]!r}, "
                f"it is a {entry.kind}"
            )
    return failures


def _check_decoys(decoys: Iterable[Decoy], dead: list[Mapping[str, Any]]) -> list[str]:
    """A decoy is called dead when its symbol is listed, or the class or module that holds it."""
    failures: list[str] = []
    for decoy in decoys:
        claims = [
            symbol
            for item in dead
            if isinstance(symbol := item.get("symbol"), str) and _holds(symbol, decoy.symbol)
        ]
        failures.extend(
            f"decoy {decoy.id}: {claim} is listed as dead, but {decoy.symbol} is in use "
            f"({decoy.kind})"
            for claim in claims
        )
    return failures


def _check_data_flows(wanted: list[DataFlow], flows: list[list[str]]) -> list[str]:
    """A flow is found when one flow of the answer passes its modules and steps, the ends in order."""
    failures: list[str] = []
    for flow in wanted:
        misses = [_flow_misses(flow, symbols) for symbols in flows]
        if any(not missing for missing in misses):
            continue
        if not misses:
            failures.append(f"data flow {flow.id}: the answer has no data flows")
            continue
        closest = min(misses, key=len)
        failures.append(
            f"data flow {flow.id}: no data flow describes it; the closest one: {'; '.join(closest)}"
        )
    return failures


def _flow_misses(flow: DataFlow, symbols: list[str]) -> list[str]:
    """Return what an answer's flow, as its step symbols, lacks of `flow`."""
    wanted = [step.symbol for step in flow.steps]
    modules = [_module(symbol) for symbol in symbols]
    misses = [
        f"module {module} is missing"
        for module in dict.fromkeys(_module(symbol) for symbol in wanted)
        if module not in modules
    ]
    absent = [symbol for symbol in dict.fromkeys(wanted) if symbol not in symbols]
    if len(absent) > (0 if len(wanted) <= _EXACT_STEPS else _MAY_MISS):
        misses.extend(f"step {symbol} is missing" for symbol in absent)
    first, last = _module(wanted[0]), _module(wanted[-1])
    if first != last and first in modules and last in modules:
        # The flow has a step in the first module before its last step in the last one.
        end = len(modules) - 1 - modules[::-1].index(last)
        if modules.index(first) > end:
            misses.append(f"{first} comes after {last}")
    return misses


def _check_call_chains(wanted: list[CallChain], calls: list[Mapping[str, Any]]) -> list[str]:
    """A chain is found when each of its calls is a resolved call of some execution path."""
    resolved = {_pair(call) for call in calls if call.get("status") == "resolved"}
    failures: list[str] = []
    for chain in wanted:
        missing = [
            f"{call.caller} calls {call.callee}"
            for call in chain.calls
            if (call.caller, call.callee) not in resolved
        ]
        if missing:
            failures.append(
                f"call chain {chain.id}: not in the execution paths: {'; '.join(missing)}"
            )
    return failures


def _check_dynamic_calls(wanted: list[DynamicCall], calls: list[Mapping[str, Any]]) -> list[str]:
    """A dynamic call is found when its site is an unresolved call with its expression."""
    failures: list[str] = []
    for entry in wanted:
        found = any(
            call.get("status") == "unresolved"
            and call.get("caller") == entry.symbol
            and _same_expression(entry.snippet, call.get("expression"))
            for call in calls
        )
        if not found:
            failures.append(
                f"dynamic call {entry.id}: {entry.snippet} in {entry.symbol} is not reported as "
                "an unresolved call"
            )
    return failures


def _check_not_guessed(dynamic: Iterable[DynamicCall], calls: list[Mapping[str, Any]]) -> list[str]:
    """A dynamic call is guessed when a path resolves a call from its site to one of its targets."""
    resolved = {_pair(call) for call in calls if call.get("status") == "resolved"}
    return [
        f"dynamic call {entry.id}: reported as a plain call from {entry.symbol} to {target}"
        for entry in dynamic
        for target in entry.targets
        if (entry.symbol, target) in resolved
    ]


def _holds(claim: str, symbol: str) -> bool:
    """Whether calling `claim` dead also calls `symbol` dead: itself, its class or its module."""
    return symbol == claim or symbol.startswith((f"{claim}::", f"{claim}."))


def _same_expression(snippet: str, expression: object) -> bool:
    """Whether an answer's `expression` is the call in `snippet`, whitespace aside.

    The snippet may stand inside a longer expression, and the arguments may be written otherwise
    (a tool shortens them): then the text up to the first parenthesis has to be the same.
    """
    if not isinstance(expression, str):
        return False
    wanted, given = ("".join(text.split()) for text in (snippet, expression))
    callee = wanted.partition("(")[0]
    return wanted in given or (bool(callee) and callee == given.partition("(")[0])


def _module(symbol: str) -> str:
    return symbol.partition("::")[0]


def _pair(call: Mapping[str, Any]) -> tuple[str, str]:
    return (str(call.get("caller")), str(call.get("callee")))


def _items(holder: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    """Return the dicts in the list `holder[field]`, or none when it is not a list."""
    value = holder.get(field)
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _symbols(holder: Mapping[str, Any], field: str, key: str) -> list[str]:
    """Return the strings under `key` of the dicts in the list `holder[field]`."""
    return [value for item in _items(holder, field) if isinstance(value := item.get(key), str)]
