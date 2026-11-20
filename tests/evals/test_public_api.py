"""Tests of the public API comparison a refactor case runs (`evals.public_api`)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import Case, Expected, ExpectedRun
from evals.graders import grade
from evals.graders.llm_judge import JudgeVerdict
from evals.graders.sandbox_tests import check_run
from evals.public_api import check_public_api, public_api
from fixtures.build import build

SOURCE = """
import os
from typing import Any

LIMIT = 3
_HIDDEN = 4
NAME: str = "x"
first, _second = 1, 2
type Pair = tuple[int, int]


def f(a: int, b: str = "x", *args: Any, c: int, **kw: Any) -> str:
    return b


async def g() -> None:
    pass


def _private() -> None:
    pass


@decorated(1)
class Box[T](Base, metaclass=Meta):
    size: int
    _inner = 1

    def __init__(self, size: int) -> None:
        self.size = size

    def open(self) -> T: ...

    def _close(self) -> None: ...


class _Hidden:
    def visible(self) -> None: ...


if os.name:
    CONDITIONAL = 1
"""


def test_public_api_lists_public_names_with_their_signatures() -> None:
    assert public_api(SOURCE) == {
        "LIMIT": "assignment",
        "NAME": "assignment: str",
        "first": "assignment",
        "Pair": "type alias tuple[int, int]",
        "f": "def f(a: int, b: str='x', *args: Any, c: int, **kw: Any) -> str",
        "g": "async def g() -> None",
        "Box": "@decorated(1) class Box[T](Base, metaclass=Meta)",
        "Box.size": "assignment: int",
        "Box.__init__": "def __init__(self, size: int) -> None",
        "Box.open": "def open(self) -> T",
    }


def test_a_changed_default_or_parameter_kind_is_a_changed_signature() -> None:
    before = public_api("def f(a, b=1): ...\n")
    assert public_api("def f(a, b=2): ...\n") != before
    assert public_api("def f(a, *, b=1): ...\n") != before
    assert public_api("def f(a, b=1):\n    return a + b\n") == before


def test_public_api_raises_on_source_that_does_not_parse() -> None:
    with pytest.raises(SyntaxError):
        public_api("def f(:\n")


def _repo(tmp_path: Path) -> Path:
    return build(["refactor-duplicate"], dest=tmp_path / "repos")[0].path


def test_a_body_only_refactor_keeps_the_public_api(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "report" / "lines.py"
    target.write_text(
        target.read_text()
        + "\n\ndef _amount(cents: int) -> str:\n    return str(cents)\n\n\n_RATE = 100\n"
    )
    assert check_public_api(repo, ["report/lines.py"]) == []


def test_a_renamed_parameter_and_a_new_public_name_are_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "report" / "lines.py"
    source = target.read_text().replace("def refund_line(name", "def refund_line(label")
    target.write_text(source + "\n\nRATE = 100\n")
    (failure,) = check_public_api(repo, ["report/lines.py"])
    assert failure.startswith("public API of report/lines.py changed: added RATE; changed ")
    assert "refund_line(name: str" in failure
    assert "refund_line(label: str" in failure


def test_removed_deleted_unparsable_and_unknown_files_are_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "report" / "lines.py"
    target.write_text(target.read_text().split("def credit_line")[0])
    assert check_public_api(repo, ["report/lines.py"]) == [
        "public API of report/lines.py changed: removed credit_line"
    ]
    target.write_text("def broken(:\n")
    assert check_public_api(repo, ["report/lines.py"]) == [
        "public_api: report/lines.py no longer parses (line 1)"
    ]
    target.unlink()
    assert check_public_api(repo, ["report/lines.py", "report/new.py"]) == [
        "public_api: report/lines.py was deleted",
        "public_api: report/new.py is not in HEAD",
    ]


def test_a_path_outside_the_repository_is_reported_not_raised(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (failure,) = check_public_api(repo, ["../outside.py"])
    assert failure.startswith("public_api: ../outside.py: ")


# --- the case field and the grader


def test_expected_run_public_api_must_be_unique_python_paths() -> None:
    assert ExpectedRun(public_api=("a.py",)).public_api == ("a.py",)
    for bad in [(), ("a.py", "a.py"), ("README.md",)]:
        with pytest.raises(ValidationError):
            ExpectedRun(public_api=bad)


def test_check_run_reports_a_changed_public_api(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "report" / "lines.py"
    target.write_text(target.read_text().replace("def credit_line(", "def credit_entry("))
    run = ExpectedRun(production="changed", tests="unchanged", public_api=("report/lines.py",))
    (failure,) = check_run(run, {}, repo)
    assert "removed credit_line" in failure
    assert "added credit_entry" in failure


def test_grade_fails_a_refactor_case_on_its_public_api_without_the_judge(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "report" / "lines.py"
    target.write_text(target.read_text() + "\n\nEXTRA = 1\n")
    case = Case.model_validate(
        {
            "id": "c",
            "fixture": "refactor-duplicate",
            "payload": {},
            "expected": {
                "verdict": None,
                "criteria": "The summary says what changed.",
                "run": {"production": "changed", "public_api": ["report/lines.py"]},
            },
            "grader": "sandbox_tests",
        }
    )

    def judge(expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        raise AssertionError("the judge must not be called")

    graded = grade(case, {}, judge, repo)
    assert not graded.passed
    assert graded.reason == "public API of report/lines.py changed: added EXTRA"
