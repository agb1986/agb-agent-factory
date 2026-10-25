"""Tests for runtime/scoring.py and the manifest `scoring` block it applies."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from registry.resolver import AGENTS_DIR, ManifestError, load_spec, resolve
from registry.spec import LLMSpec, ScoringSpec, WeightsSpec
from runtime.scoring import DIMENSIONS, Assessment, assess, score_values, ship, weighted_score

WEIGHTS = WeightsSpec(
    correctness_risk=0.4, maintainability=0.15, security_risk=0.25, test_coverage=0.2
)


def _scores(**overrides: float) -> dict[str, float]:
    scores = {"correctness_risk": 0.0, "maintainability": 1.0, "security_risk": 0.0}
    return scores | {"test_coverage": 1.0} | overrides


def _judgement(**values: float) -> dict[str, Any]:
    scores = {name: {"value": value, "justification": "j"} for name, value in values.items()}
    return {"scores": scores, "merged_findings": [], "rationale": "r"}


@pytest.fixture(scope="module")
def judge() -> LLMSpec:
    spec = resolve("judge", "^2.0")
    assert isinstance(spec, LLMSpec)
    return spec


def _judge_copy(tmp_path: Path, scoring: dict[str, Any]) -> Path:
    """Copy the judge into `tmp_path` with its `scoring` block replaced; return the manifest."""
    dest = tmp_path / "judge"
    shutil.copytree(AGENTS_DIR / "judge", dest, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = dest / "agent.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    manifest.write_text(yaml.safe_dump(data | {"scoring": scoring}), encoding="utf-8")
    return manifest


# --- weighted_score


def test_a_flawless_change_scores_one() -> None:
    assert weighted_score(_scores(), WEIGHTS) == pytest.approx(1)


def test_the_worst_change_scores_zero() -> None:
    worst = _scores(correctness_risk=1, maintainability=0, security_risk=1, test_coverage=0)
    assert weighted_score(worst, WEIGHTS) == pytest.approx(0)


def test_risks_count_against_the_score_and_quality_for_it() -> None:
    scores = _scores(correctness_risk=0.7, maintainability=0.8, test_coverage=0.3)
    # 0.4 * (1 - 0.7) + 0.15 * 0.8 + 0.25 * (1 - 0.0) + 0.2 * 0.3
    assert weighted_score(scores, WEIGHTS) == pytest.approx(0.55)


@pytest.mark.parametrize("name", DIMENSIONS)
def test_each_dimension_moves_the_score_by_its_weight(name: str) -> None:
    worse = _scores(**{name: 1.0 if name.endswith("_risk") else 0.0})
    drop = weighted_score(_scores(), WEIGHTS) - weighted_score(worse, WEIGHTS)
    assert drop == pytest.approx(getattr(WEIGHTS, name))


@pytest.mark.parametrize(
    ("scores", "fragment"),
    [
        (
            {"correctness_risk": 0.1},
            "missing ['maintainability', 'security_risk', 'test_coverage']",
        ),
        (_scores() | {"speed": 0.5}, "unknown ['speed']"),
        (_scores(security_risk=1.2), "score security_risk is 1.2, outside 0 to 1"),
        (_scores(test_coverage=-0.1), "score test_coverage is -0.1, outside 0 to 1"),
    ],
)
def test_a_bad_set_of_scores_is_rejected(scores: dict[str, float], fragment: str) -> None:
    with pytest.raises(ValueError) as info:
        weighted_score(scores, WEIGHTS)
    assert fragment in str(info.value)


# --- ship


@pytest.mark.parametrize(
    ("score", "ships"), [(0.69, False), (0.7, True), (0.71, True), (0.0, False), (1.0, True)]
)
def test_a_change_ships_at_or_above_the_threshold(score: float, ships: bool) -> None:
    assert ship(score, 0.7) is ships


# --- assess


def test_assess_weighs_a_judge_output_with_its_manifest(judge: LLMSpec) -> None:
    output = judge.output_class.model_validate(
        _judgement(correctness_risk=0.7, maintainability=0.8, security_risk=0.0, test_coverage=0.3)
    )
    assessment = assess(judge, output)
    assert isinstance(assessment, Assessment)
    assert assessment.score == pytest.approx(0.55)
    assert assessment.ship is False


def test_assess_reads_an_output_already_dumped_to_json(judge: LLMSpec) -> None:
    dumped = _judgement(**_scores())
    assert score_values(dumped) == _scores()
    assessment = assess(judge, dumped)
    assert assessment.score == pytest.approx(1)
    assert assessment.ship is True


def test_assess_needs_a_scoring_block(judge: LLMSpec) -> None:
    unscored = judge.model_copy(update={"scoring": None})
    with pytest.raises(ManifestError, match="judge@2.0.0 has no scoring block"):
        assess(unscored, _judgement(**_scores()))


# --- the manifest block


def test_weights_that_do_not_sum_to_one_are_rejected() -> None:
    with pytest.raises(ValidationError, match="weights sum to 1.1, expected 1"):
        ScoringSpec.model_validate(
            {
                "weights": WEIGHTS.model_dump() | {"correctness_risk": 0.5},
                "ship_threshold": 0.7,
            }
        )


def test_weights_off_by_float_rounding_still_sum_to_one() -> None:
    thirds = {"correctness_risk": 0.1 + 0.2, "maintainability": 0.3, "security_risk": 0.2}
    spec = ScoringSpec.model_validate(
        {"weights": thirds | {"test_coverage": 0.2}, "ship_threshold": 0.5}
    )
    assert spec.weights.correctness_risk == pytest.approx(0.3)


def test_a_manifest_whose_weights_do_not_sum_to_one_fails_to_load(tmp_path: Path) -> None:
    weights = WEIGHTS.model_dump() | {"maintainability": 0.05}
    manifest = _judge_copy(tmp_path, {"weights": weights, "ship_threshold": 0.7})
    with pytest.raises(ManifestError) as info:
        load_spec(manifest)
    assert str(manifest) in info.value.message
    assert "scoring" in info.value.message
    assert "weights sum to 0.9, expected 1" in info.value.message


@pytest.mark.parametrize(
    ("scoring", "fragment"),
    [
        ({"weights": WEIGHTS.model_dump(), "ship_threshold": 1.5}, "ship_threshold"),
        ({"weights": WEIGHTS.model_dump()}, "ship_threshold"),
        (
            {"weights": WEIGHTS.model_dump() | {"speed": 0.0}, "ship_threshold": 0.7},
            "speed",
        ),
        (
            {
                "weights": {k: v for k, v in WEIGHTS.model_dump().items() if k != "test_coverage"},
                "ship_threshold": 0.7,
            },
            "test_coverage",
        ),
    ],
)
def test_a_malformed_scoring_block_fails_to_load(
    tmp_path: Path, scoring: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ManifestError) as info:
        load_spec(_judge_copy(tmp_path, scoring))
    assert fragment in info.value.message


def test_the_judge_manifest_loads_with_its_scoring_block(tmp_path: Path, judge: LLMSpec) -> None:
    assert judge.scoring is not None
    copy = load_spec(_judge_copy(tmp_path, judge.scoring.model_dump()))
    assert isinstance(copy, LLMSpec)
    assert copy.scoring == judge.scoring
