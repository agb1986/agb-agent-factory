"""Weigh the judge's four scores into one number and decide whether the change ships.

The judge returns a score from 0 to 1 per dimension and never sees the weights or the threshold;
both live in the `scoring` block of its manifest, and this module is the one place that applies
them. Two dimensions measure risk, where higher is worse, so they count as `1 - value`. The
weighted score is therefore a quality score from 0 to 1 where higher is better, and a change ships
when it reaches the threshold.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from registry.resolver import ManifestError
from registry.spec import LLMSpec, WeightsSpec

DIMENSIONS: tuple[str, ...] = tuple(WeightsSpec.model_fields)
"""The four scored dimensions, in manifest order."""

RISK_DIMENSIONS = frozenset({"correctness_risk", "security_risk"})
"""Dimensions where a higher score is worse; each counts as `1 - value`."""


@dataclass(frozen=True, slots=True)
class Assessment:
    """The weighted score of one judgement and whether it clears the ship threshold."""

    score: float
    ship: bool


def weighted_score(scores: Mapping[str, float], weights: WeightsSpec) -> float:
    """Return the weighted quality score, from 0 to 1, of one value per dimension.

    `scores` maps each name in `DIMENSIONS` to a value from 0 to 1. Risk dimensions count as
    `1 - value`. Raises `ValueError` when a dimension is missing or unknown, or a value is out of
    range.
    """
    if set(scores) != set(DIMENSIONS):
        missing = sorted(set(DIMENSIONS) - set(scores))
        unknown = sorted(set(scores) - set(DIMENSIONS))
        raise ValueError(
            f"scores must cover {', '.join(DIMENSIONS)}; missing {missing}, unknown {unknown}"
        )
    total = 0.0
    for name in DIMENSIONS:
        value = scores[name]
        if not 0 <= value <= 1:
            raise ValueError(f"score {name} is {value}, outside 0 to 1")
        quality = 1 - value if name in RISK_DIMENSIONS else value
        total += getattr(weights, name) * quality
    return total


def ship(score: float, threshold: float) -> bool:
    """Return whether a weighted score reaches the ship threshold; equal to it ships."""
    return score >= threshold


def score_values(output: BaseModel | Mapping[str, Any]) -> dict[str, float]:
    """Pull the value of each dimension out of a judge output, as a model or as its JSON dict."""
    data = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    scores = data["scores"]
    return {name: float(scores[name]["value"]) for name in DIMENSIONS}


def assess(spec: LLMSpec, output: BaseModel | Mapping[str, Any]) -> Assessment:
    """Weigh a judge output with `spec.scoring` and decide whether the change ships.

    Raises `ManifestError` when the manifest has no `scoring` block, and `ValueError` or `KeyError`
    when the output does not carry one value per dimension.
    """
    if spec.scoring is None:
        raise ManifestError(f"{spec.path}: scoring: {spec.address} has no scoring block to apply")
    score = weighted_score(score_values(output), spec.scoring.weights)
    return Assessment(score=score, ship=ship(score, spec.scoring.ship_threshold))
