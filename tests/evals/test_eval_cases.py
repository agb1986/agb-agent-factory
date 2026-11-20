"""Tests for evals/cases.py: the case schema and `load_cases`."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.cases import CASES_FILE, Case, CaseFileError, CasesNotFound, load_cases
from factory.exit import ExitCode, exit_code
from registry.resolver import AGENTS_DIR


def _case(**overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": "one",
        "fixture": "off-by-one",
        "payload": {"base": "base", "head": "head"},
        "expected": {
            "verdict": "request_changes",
            "defects": [
                {
                    "id": "d1",
                    "file": "a.py",
                    "line_start": 1,
                    "line_end": 2,
                    "severity_floor": "minor",
                }
            ],
        },
        "grader": "llm_judge",
    }
    return case | overrides


def _load(tmp_path: Path, *cases: dict[str, Any]) -> list[Case]:
    path = tmp_path / "cases.yaml"
    path.write_text(yaml.safe_dump({"cases": list(cases)}))
    return load_cases(path)


def _rejects(tmp_path: Path, *cases: dict[str, Any]) -> str:
    with pytest.raises(CaseFileError) as info:
        _load(tmp_path, *cases)
    assert exit_code(info.value) == ExitCode.AUTH_OR_CONFIG
    return info.value.message


def test_the_code_reviewer_cases_load_unchanged() -> None:
    cases = load_cases(AGENTS_DIR / "code-reviewer" / CASES_FILE)
    assert [case.id for case in cases] == [
        "off-by-one",
        "clean-refactor",
        "untested-change",
        "mixed",
    ]
    mixed = cases[-1]
    defects = [
        (d.id, d.file, d.line_start, d.line_end, d.severity_floor) for d in mixed.expected.defects
    ]
    assert defects == [
        ("free-shipping-boundary", "shop/shipping.py", 12, 12, "major"),
        ("camel-case-locals", "shop/format.py", 6, 7, "info"),
    ]


def test_a_case_without_a_grader_is_rejected(tmp_path: Path) -> None:
    case = _case()
    del case["grader"]
    assert "grader" in _rejects(tmp_path, case)


def test_the_jev_grader_is_accepted_by_the_schema(tmp_path: Path) -> None:
    assert _load(tmp_path, _case(grader="jev"))[0].grader == "jev"


def test_a_case_may_expect_no_verdict_and_carry_criteria(tmp_path: Path) -> None:
    expected = _case()["expected"] | {"verdict": None, "criteria": "The rationale names it."}
    (case,) = _load(tmp_path, _case(expected=expected))
    assert case.expected.verdict is None
    assert case.expected.criteria == "The rationale names it."


def test_criteria_default_to_none(tmp_path: Path) -> None:
    assert _load(tmp_path, _case())[0].expected.criteria is None


def test_a_missing_file_is_not_found_with_exit_code_3(tmp_path: Path) -> None:
    with pytest.raises(CasesNotFound) as info:
        load_cases(tmp_path / "nope" / "cases.yaml")
    assert exit_code(info.value) == ExitCode.NOT_FOUND


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        (_case(id="Bad Id"), "id"),
        (_case(fixture="../escape"), "fixture"),
        (_case(grader="vibes"), "grader"),
        (_case(payload={"repo_path": "/x"}), "repo_path"),
        (_case(expected={"defects": []}), "verdict"),
        (_case(extra=1), "extra"),
        (
            _case(
                expected={
                    "verdict": "approve",
                    "defects": [
                        {
                            "id": "d",
                            "file": "a.py",
                            "line_start": 5,
                            "line_end": 4,
                            "severity_floor": "minor",
                        }
                    ],
                }
            ),
            "line_end",
        ),
        (
            _case(
                expected={
                    "verdict": "approve",
                    "defects": [
                        {
                            "id": "d",
                            "file": "a.py",
                            "line_start": 1,
                            "line_end": 1,
                            "severity_floor": "huge",
                        }
                    ],
                }
            ),
            "severity_floor",
        ),
    ],
)
def test_an_invalid_case_is_rejected_naming_the_file_and_field(
    tmp_path: Path, case: dict[str, Any], fragment: str
) -> None:
    message = _rejects(tmp_path, case)
    assert "cases.yaml" in message
    assert fragment in message


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    assert "duplicate" in _rejects(tmp_path, _case(), _case())


def test_an_empty_case_list_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text("cases: []\n")
    with pytest.raises(CaseFileError):
        load_cases(path)


def test_a_file_that_is_not_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text("cases: [unclosed\n")
    with pytest.raises(CaseFileError):
        load_cases(path)


def _rubric(**overrides: Any) -> dict[str, Any]:
    rubric: dict[str, Any] = {
        "criteria": [
            {"id": "clear", "text": "The plan is clear."},
            {"id": "safe", "text": "The plan has a rollback.", "required": True, "floor": 0.5},
        ],
        "threshold": 0.7,
        "required_fields": ["summary"],
        "forbidden": ["TODO"],
        "paths_exist": ["files"],
    }
    return rubric | overrides


def _rubric_case(**overrides: Any) -> dict[str, Any]:
    return _case(grader="rubric", expected={"verdict": None, "rubric": _rubric(**overrides)})


def test_a_rubric_case_loads(tmp_path: Path) -> None:
    [case] = _load(tmp_path, _rubric_case())
    rubric = case.expected.rubric
    assert rubric is not None
    assert [(c.id, c.required, c.floor) for c in rubric.criteria] == [
        ("clear", False, None),
        ("safe", True, 0.5),
    ]
    assert (rubric.threshold, rubric.forbidden, rubric.paths_exist) == (0.7, ("TODO",), ("files",))


def test_the_rubric_grader_needs_a_rubric(tmp_path: Path) -> None:
    assert "expected.rubric" in _rejects(tmp_path, _case(grader="rubric"))


@pytest.mark.parametrize("grader", ["llm_judge", "jev"])
def test_a_rubric_is_read_only_by_the_rubric_and_sandbox_graders(
    tmp_path: Path, grader: str
) -> None:
    case = _case(grader=grader, expected={"verdict": None, "rubric": _rubric()})
    assert "only the rubric and sandbox_tests graders" in _rejects(tmp_path, case)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"criteria": []}, "criteria"),
        ({"threshold": 1.5}, "threshold"),
        ({"criteria": [{"id": "a", "text": "x", "required": True}]}, "needs a floor"),
        ({"criteria": [{"id": "a", "text": "x", "floor": 0.5}]}, "only read on a required"),
        ({"criteria": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]}, "duplicate ids"),
        ({"forbidden": [""]}, "forbidden"),
    ],
)
def test_an_invalid_rubric_is_rejected(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    assert message in _rejects(tmp_path, _rubric_case(**overrides))
