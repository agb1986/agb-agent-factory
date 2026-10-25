"""Tests for the sandbox_tests grader: the case schema, the merge into `grade`, and real runs.

The runs need Docker and the sandbox image (`docker build -t factory-sandbox:0.1 -f
tools/sandbox.Dockerfile tools`); without them those tests skip.
"""

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import Case, ExpectedRun
from evals.graders import grade
from evals.graders.llm_judge import JudgeVerdict
from evals.graders.sandbox_tests import changed_files, check_run, is_test_file
from factory.exit import UsageError
from fixtures.build import build
from tools.sandbox import SANDBOX_IMAGE


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, check=False
    )
    return proc.returncode == 0


needs_image = pytest.mark.skipif(
    not _image_present(), reason=f"docker image {SANDBOX_IMAGE} absent"
)

NEW_TEST = """from series.rolling import rolling_sums


def test_last_window_is_included() -> None:
    assert rolling_sums([1, 2, 3, 4], 2) == [3, 5, 7]
"""


def _case(run: dict[str, Any] | None, grader: str = "sandbox_tests") -> dict[str, Any]:
    expected: dict[str, Any] = {"verdict": None}
    if run is not None:
        expected["run"] = run
    return {"id": "c", "fixture": "f", "payload": {}, "expected": expected, "grader": grader}


def _build(tmp_path: Path, name: str) -> Path:
    return build([name], dest=tmp_path / "repos")[0].path


class _Judge:
    """A judge that passes everything and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, expected: object, actual: Mapping[str, Any]) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(passed=True, score=0.9, reason="judged")


# --- the case schema


def test_a_sandbox_case_needs_expected_run() -> None:
    with pytest.raises(ValidationError, match="expected.run"):
        Case.model_validate(_case(None))


def test_other_graders_refuse_expected_run() -> None:
    with pytest.raises(ValidationError, match="expected.run"):
        Case.model_validate(_case({"suite": "pass"}, grader="llm_judge"))


def test_passes_on_base_needs_new_tests() -> None:
    with pytest.raises(ValidationError):
        ExpectedRun.model_validate({"passes_on_base": True})


def test_an_unknown_run_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExpectedRun.model_validate({"suite": "pass", "colour": "red"})


# --- is_test_file


@pytest.mark.parametrize(
    "path",
    ["tests/test_a.py", "pkg/tests/helpers.py", "test_a.py", "a_test.py", "conftest.py"],
)
def test_test_files_are_recognised(path: str) -> None:
    assert is_test_file(path)


@pytest.mark.parametrize("path", ["series/rolling.py", "README.md", "pkg/testing.py", "latest.py"])
def test_production_files_are_not_test_files(path: str) -> None:
    assert not is_test_file(path)


# --- grade


def test_grade_without_a_repo_path_is_a_usage_error() -> None:
    case = Case.model_validate(_case({"suite": "pass"}))
    with pytest.raises(UsageError):
        grade(case, {}, _Judge())


def test_grade_fails_without_calling_the_judge_when_the_work_tree_is_wrong(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    case = Case.model_validate(_case({"production": "changed"}))
    judge = _Judge()
    graded = grade(case, {}, judge, repo)
    assert not graded.passed
    assert "production code unchanged" in graded.reason
    assert judge.calls == 0


def test_grade_goes_on_to_the_judge_when_the_work_tree_is_right(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    case = Case.model_validate(_case({"production": "unchanged", "tests": "unchanged"}))
    judge = _Judge()
    graded = grade(case, {}, judge, repo)
    assert graded.passed
    assert judge.calls == 1


# --- changed_files


def test_changed_files_lists_edits_and_new_files_but_not_caches(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    assert changed_files(repo) == []
    (repo / "series" / "rolling.py").write_text("x = 1\n")
    (repo / "tests" / "test_new.py").write_text(NEW_TEST)
    (repo / "series" / "__pycache__").mkdir()
    (repo / "series" / "__pycache__" / "rolling.cpython-312.pyc").write_bytes(b"\0")
    assert changed_files(repo) == ["series/rolling.py", "tests/test_new.py"]


def test_check_run_rejects_a_test_file_that_leaves_the_repository(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    failures = check_run(ExpectedRun(new_tests="fail"), {"test_files": ["../x.py"]}, repo)
    assert failures
    assert "test_files" in failures[0]


def test_check_run_needs_test_files_for_new_tests(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    failures = check_run(ExpectedRun(new_tests="fail"), {"test_files": []}, repo)
    assert failures == ["test_files is empty: the agent named no test file to run"]


# --- check_run in the sandbox


@needs_image
def test_a_new_test_that_fails_on_head_and_passes_on_base_meets_the_expectation(
    tmp_path: Path,
) -> None:
    repo = _build(tmp_path, "off-by-one")
    (repo / "tests" / "test_last.py").write_text(NEW_TEST)
    run = ExpectedRun(new_tests="fail", passes_on_base=True, production="unchanged")
    assert check_run(run, {"test_files": ["tests/test_last.py"]}, repo) == []


@needs_image
def test_a_new_test_that_passes_on_head_is_not_a_failing_test(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    (repo / "tests" / "test_ok.py").write_text("def test_ok() -> None:\n    assert True\n")
    failures = check_run(ExpectedRun(new_tests="fail"), {"test_files": ["tests/test_ok.py"]}, repo)
    assert len(failures) == 1
    assert "should fail" in failures[0]


@needs_image
def test_a_collection_error_is_not_a_failing_test(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    (repo / "tests" / "test_broken.py").write_text("import nothing_here\n")
    failures = check_run(
        ExpectedRun(new_tests="fail"), {"test_files": ["tests/test_broken.py"]}, repo
    )
    assert len(failures) == 1
    assert "got 2" in failures[0]


@needs_image
def test_a_test_that_also_fails_on_base_is_flagged(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    (repo / "tests" / "test_bad.py").write_text("def test_bad() -> None:\n    assert 1 == 2\n")
    run = ExpectedRun(new_tests="fail", passes_on_base=True)
    failures = check_run(run, {"test_files": ["tests/test_bad.py"]}, repo)
    assert len(failures) == 1
    assert "base state" in failures[0]


@needs_image
def test_the_base_check_leaves_the_repository_untouched(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one")
    (repo / "tests" / "test_last.py").write_text(NEW_TEST)
    check_run(
        ExpectedRun(new_tests="fail", passes_on_base=True),
        {"test_files": ["tests/test_last.py"]},
        repo,
    )
    assert changed_files(repo) == ["tests/test_last.py"]


@needs_image
def test_the_suite_check_follows_a_fix(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one-red")
    run = ExpectedRun(suite="pass", production="changed", tests="unchanged")
    before = check_run(run, {}, repo)
    assert any("full suite fails" in text for text in before)
    assert any("production code unchanged" in text for text in before)
    source = (repo / "series" / "rolling.py").read_text()
    (repo / "series" / "rolling.py").write_text(source.replace("len(values) - 1", "len(values)"))
    assert check_run(run, {}, repo) == []


@needs_image
def test_editing_a_test_file_breaks_a_tests_unchanged_expectation(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one-red")
    (repo / "tests" / "test_rolling.py").write_text("def test_nothing() -> None:\n    pass\n")
    failures = check_run(ExpectedRun(tests="unchanged"), {}, repo)
    assert failures == ["test files changed, expected none: tests/test_rolling.py"]
