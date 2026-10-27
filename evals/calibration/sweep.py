"""Sweep the triage skip threshold over labelled diffs, and choose the one to ship.

`scripts/calibrate.py` fetches every labelled diff and runs diff-triage over it once. `sweep` then
decides each diff again at every candidate threshold with `runtime.gates.decide`, on a copy of the
manifest whose skip gate is moved to that threshold (`runtime.gates.with_skip_threshold`), so the
numbers are what the panel would do: an escalating gate still clears the skip, and a truncated
input is always reviewed. A diff whose Jev call failed is left out of every number and listed.

At each threshold it measures recall on risky diffs (the share still reviewed), the skip rate on
safe diffs, and the cost per diff: the mean Jev cost, plus the share of diffs still reviewed times
the panel cost of one review when that is known. The chosen threshold is the one that skips the
most safe diffs among those whose recall reaches the manifest's `min_recall_risky`; a tie goes to
the lower threshold, which leaves more margin. docs/jev-calibration.md describes the method.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from factory.exit import UsageError
from registry.jev_spec import JevOutput, JevSpec
from runtime.gates import decide, skip_gate, with_skip_threshold

DEFAULT_THRESHOLDS = (0.02, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5)
"""The skip thresholds swept when none are given."""


class SweepError(UsageError):
    """The sweep cannot run as asked (exit 2).

    There are no thresholds or one outside (0, 1], the manifest sets no recall target or no skip
    gate can be built, or no risky or no safe diff was measured.
    """


@dataclass(frozen=True, slots=True)
class Triaged:
    """One labelled diff after triage.

    `output` holds the Jev answers and is `None` when the call failed. `truncated` says the input
    was cut, so the answers may not skip. `cost_usd` is what the pipeline run cost, read from the
    trace by its pipeline run id.
    """

    id: str
    risky: bool
    output: JevOutput | None
    truncated: bool
    cost_usd: float


@dataclass(frozen=True, slots=True)
class ThresholdRow:
    """The outcome of one skip threshold over the measured diffs.

    `missed` lists the risky diffs this threshold would skip, in label order.
    """

    threshold: float
    risky: int
    risky_reviewed: int
    safe: int
    safe_skipped: int
    cost_per_diff_usd: float
    missed: tuple[str, ...]

    @property
    def recall_risky(self) -> float:
        """The share of risky diffs still reviewed."""
        return self.risky_reviewed / self.risky

    @property
    def skip_rate_safe(self) -> float:
        """The share of safe diffs skipped."""
        return self.safe_skipped / self.safe

    def to_json(self) -> dict[str, Any]:
        """Return the row as a JSON-ready dict, with the two rates worked out."""
        return {
            "threshold": self.threshold,
            "recall_risky": self.recall_risky,
            "risky": self.risky,
            "risky_reviewed": self.risky_reviewed,
            "skip_rate_safe": self.skip_rate_safe,
            "safe": self.safe,
            "safe_skipped": self.safe_skipped,
            "cost_per_diff_usd": self.cost_per_diff_usd,
            "missed": list(self.missed),
        }


@dataclass(frozen=True, slots=True)
class Sweep:
    """Every threshold's row, the one chosen (or `None` when none reaches the recall), and counts.

    `diffs` counts the measured diffs and `failed` lists the ones whose Jev call failed.
    `skip_ceiling_safe` is the share of safe diffs that were not truncated: no threshold can skip
    more than that. `panel_cost_usd` is the cost of one full review, or `None` when it was not
    given and the cost per diff is the Jev cost alone.
    """

    question: str
    min_recall_risky: float
    rows: tuple[ThresholdRow, ...]
    chosen: ThresholdRow | None
    diffs: int
    truncated: int
    failed: tuple[str, ...]
    skip_ceiling_safe: float
    jev_cost_per_diff_usd: float
    panel_cost_usd: float | None

    def to_json(self) -> dict[str, Any]:
        """Return the sweep as a JSON-ready dict; `chosen` is the chosen threshold or `null`."""
        return {
            "question": self.question,
            "min_recall_risky": self.min_recall_risky,
            "chosen": None if self.chosen is None else self.chosen.threshold,
            "diffs": self.diffs,
            "truncated": self.truncated,
            "failed": list(self.failed),
            "skip_ceiling_safe": self.skip_ceiling_safe,
            "jev_cost_per_diff_usd": self.jev_cost_per_diff_usd,
            "panel_cost_usd": self.panel_cost_usd,
            "cost_includes_panel": self.panel_cost_usd is not None,
            "rows": [row.to_json() for row in self.rows],
        }


def check_thresholds(thresholds: Sequence[float]) -> tuple[float, ...]:
    """Return `thresholds` sorted and without repeats; raise `SweepError` unless each is in (0, 1]."""
    if not thresholds:
        raise SweepError("no thresholds to sweep", hint="pass --thresholds 0.05,0.1,0.2")
    bad = [value for value in thresholds if not 0.0 < value <= 1.0]
    if bad:
        raise SweepError(
            f"thresholds must be in (0, 1], not {', '.join(map(str, bad))}",
            hint="a p_below gate at 0 never fires",
        )
    return tuple(sorted(set(thresholds)))


def sweep(
    spec: JevSpec,
    triaged: Sequence[Triaged],
    thresholds: Sequence[float],
    *,
    question: str | None = None,
    option: str | None = None,
    panel_cost_usd: float | None = None,
) -> Sweep:
    """Decide every measured diff at every threshold and return the `Sweep`.

    `question` (and `option`, for a choice question) is what the skip gate reads; by default the
    manifest's skip gate says. The recall target is `spec.evals.min_recall_risky`. Raises
    `SweepError` for a bad threshold, when the manifest sets no recall target, when no skip gate
    can be built, or when no risky or no safe diff was measured, since then a rate has nothing to
    divide by.
    """
    thresholds = check_thresholds(thresholds)
    target = spec.evals.min_recall_risky
    if target is None:
        raise SweepError(
            f"{spec.address} sets no evals.min_recall_risky, so no threshold can be chosen",
            hint="add min_recall_risky: 0.98 to the manifest's evals block",
        )
    try:
        specs = [
            with_skip_threshold(spec, threshold, question=question, option=option)
            for threshold in thresholds
        ]
    except ValueError as exc:
        raise SweepError(str(exc), hint="pass --question needs_review") from exc
    gate = skip_gate(specs[0])
    assert gate is not None  # with_skip_threshold always leaves one
    measured = [diff for diff in triaged if diff.output is not None]
    failed = tuple(diff.id for diff in triaged if diff.output is None)
    risky = sum(diff.risky for diff in measured)
    safe = len(measured) - risky
    if not risky or not safe:
        missing = "risky" if not risky else "safe"
        raise SweepError(
            f"no {missing} diff was measured, so the sweep has nothing to divide by",
            hint="label more diffs, or drop --limit",
        )
    # Failed diffs are left out of the mean; any failure already exits 1 via SweepIncomplete.
    jev_cost = sum(diff.cost_usd for diff in measured) / len(measured)
    rows = tuple(
        _row(copy, threshold, measured, jev_cost, panel_cost_usd)
        for copy, threshold in zip(specs, thresholds, strict=True)
    )
    passing = [row for row in rows if row.recall_risky >= target]
    chosen = max(passing, key=lambda row: (row.safe_skipped, -row.threshold), default=None)
    safe_whole = sum(1 for diff in measured if not diff.risky and not diff.truncated)
    return Sweep(
        question=gate.question if gate.option is None else f"{gate.question}={gate.option}",
        min_recall_risky=target,
        rows=rows,
        chosen=chosen,
        diffs=len(measured),
        truncated=sum(diff.truncated for diff in measured),
        failed=failed,
        skip_ceiling_safe=safe_whole / safe,
        jev_cost_per_diff_usd=jev_cost,
        panel_cost_usd=panel_cost_usd,
    )


def _row(
    spec: JevSpec,
    threshold: float,
    measured: Sequence[Triaged],
    jev_cost: float,
    panel_cost_usd: float | None,
) -> ThresholdRow:
    skipped = {
        diff.id
        for diff in measured
        if diff.output is not None and decide(spec, diff.output, diff.truncated).skip
    }
    missed = tuple(diff.id for diff in measured if diff.risky and diff.id in skipped)
    risky = sum(diff.risky for diff in measured)
    reviewed_share = 1 - len(skipped) / len(measured)
    return ThresholdRow(
        threshold=threshold,
        risky=risky,
        risky_reviewed=risky - len(missed),
        safe=len(measured) - risky,
        safe_skipped=len(skipped) - len(missed),
        cost_per_diff_usd=jev_cost + reviewed_share * (panel_cost_usd or 0.0),
        missed=missed,
    )


def render_table(result: Sweep) -> str:
    """Return the sweep as a plain-text table, the chosen threshold marked with `*`."""
    lines = [
        (
            f"skip gate: {result.question} p_below <threshold>; "
            f"target recall on risky diffs >= {result.min_recall_risky}"
        ),
        (
            f"{result.diffs} diffs measured, {result.truncated} truncated (always reviewed); "
            f"at most {result.skip_ceiling_safe:.1%} of safe diffs can be skipped"
        ),
        "cost per diff: Jev"
        + (
            f" + reviewed share x panel ${result.panel_cost_usd:.4f}"
            if result.panel_cost_usd is not None
            else " only (no --panel-cost-usd given)"
        ),
        "",
        f"  {'threshold':>9}  {'recall risky':>14}  {'skip safe':>14}  {'cost/diff':>10}  missed",
    ]
    for row in result.rows:
        mark = "*" if row is result.chosen else " "
        recall = f"{row.recall_risky:.3f} {row.risky_reviewed}/{row.risky}"
        skip = f"{row.skip_rate_safe:.3f} {row.safe_skipped}/{row.safe}"
        cost = f"${row.cost_per_diff_usd:.4f}"
        missed = ", ".join(row.missed) or "-"
        lines.append(f"{mark} {row.threshold:>9}  {recall:>14}  {skip:>14}  {cost:>10}  {missed}")
    if result.failed:
        lines += ["", f"failed, not measured: {', '.join(result.failed)}"]
    return "\n".join(lines)
