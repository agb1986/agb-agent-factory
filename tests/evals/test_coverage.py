"""Tests of `evals.coverage` and the coverage check of the `sandbox_tests` grader.

Most tests measure for real with `local_run`, which runs the sandbox command with this
interpreter on the host, so they need neither Docker nor the image. The last ones run in the
sandbox, when the image is there, and check that the reports in the test-writer's cover-mode
cases are still what the fixtures measure.
"""

import functools
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from evals.cases import ExpectedCoverage, ExpectedRun, load_cases
from evals.coverage import (
    REPORT_MARKER,
    CoverageError,
    check_targets,
    coverage_argv,
    coverage_delta,
    measure_coverage,
    parse_report,
    restore_head,
)
from evals.graders import sandbox_tests
from evals.graders.sandbox_tests import changed_files, check_run
from fixtures.build import build
from registry.resolver import AGENTS_DIR
from registry.work_schemas import CoverageReport, FileCoverage
from tools._common import ToolInputError
from tools.sandbox import SANDBOX_IMAGE, SandboxResult

DISCOUNT_TEST = """import pytest

from pricing.discount import apply_discount


def test_rounds_the_discounted_price() -> None:
    assert apply_discount(999, 10) == 899


def test_rejects_a_percent_out_of_range() -> None:
    with pytest.raises(ValueError):
        apply_discount(100, 101)
"""


def local_run(root: Path, argv: Sequence[str]) -> SandboxResult:
    """Run a sandbox command on the host, with this interpreter standing in for `python`."""
    command = [sys.executable if part == "python" else part for part in argv]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_"))
    }
    env |= {"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}
    start = time.monotonic()
    proc = subprocess.run(command, cwd=root, env=env, capture_output=True, check=False, timeout=120)
    return SandboxResult(
        argv=tuple(argv),
        exit_code=proc.returncode,
        output=proc.stdout + proc.stderr,
        seconds=time.monotonic() - start,
    )


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, check=False
    )
    return proc.returncode == 0


def _build(tmp_path: Path, name: str) -> Path:
    return build([name], dest=tmp_path / "repos")[0].path


def _report(path: str, statements: int, missing: list[int], exit_code: int = 0) -> CoverageReport:
    return CoverageReport(
        files=[
            FileCoverage(
                path=path,
                statements=statements,
                covered=statements - len(missing),
                missing_lines=missing,
            )
        ],
        pytest_exit_code=exit_code,
    )


# --- pure pieces


def test_the_command_sends_the_script_then_targets_then_pytest_args() -> None:
    argv = coverage_argv(["a.py", "b.py"], ["-q", "tests"])
    assert argv[:2] == ["python", "-c"]
    assert argv[3:] == ["a.py", "b.py", "--", "-q", "tests"]


def test_the_report_is_read_from_the_last_marker_line() -> None:
    report = _report("a.py", 3, [2])
    output = f"noise\n{REPORT_MARKER}{{}}\n{REPORT_MARKER}{report.model_dump_json()}\n".encode()
    result = SandboxResult(argv=(), exit_code=0, output=output, seconds=0.0)
    assert parse_report(result) == report


@pytest.mark.parametrize(
    "output", [b"ImportError: no pytest", f'{REPORT_MARKER}{{"files": 1}}'.encode()]
)
def test_output_without_a_valid_report_is_a_coverage_error(output: bytes) -> None:
    with pytest.raises(CoverageError):
        parse_report(SandboxResult(argv=(), exit_code=4, output=output, seconds=0.0))


def test_the_delta_counts_lines_that_became_covered() -> None:
    delta = coverage_delta(_report("a.py", 5, [1, 2, 3]), _report("a.py", 5, [3]))
    assert delta.newly_covered == {"a.py": [1, 2]}
    assert delta.still_missing == {"a.py": [3]}
    assert (delta.gained, delta.missing) == (2, 1)
    full = coverage_delta(_report("a.py", 5, []), _report("a.py", 5, []))
    assert (full.newly_covered, full.still_missing) == ({}, {})


def test_the_delta_needs_the_same_paths() -> None:
    with pytest.raises(ValueError, match="different paths"):
        coverage_delta(_report("a.py", 1, []), _report("b.py", 1, []))


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ([], "no target paths"),
        (["../outside.py"], "outside.py"),
        (["README.md"], "not a Python file"),
        (["pricing/missing.py"], "not a Python file"),
        (["pricing/discount.py", "./pricing/discount.py"], "twice"),
    ],
)
def test_bad_targets_are_refused(tmp_path: Path, targets: list[str], message: str) -> None:
    repo = _build(tmp_path, "untested-change")
    with pytest.raises(ToolInputError, match=message):
        check_targets(repo.resolve(), targets)


def test_restore_head_puts_back_edits_and_deletes_new_files(tmp_path: Path) -> None:
    repo = _build(tmp_path, "untested-change")
    source = repo / "pricing" / "discount.py"
    original = source.read_bytes()
    source.write_text("broken = True\n")
    (repo / "tests" / "test_new.py").write_text("def test_x() -> None:\n    pass\n")
    restore_head(repo, changed_files(repo))
    assert source.read_bytes() == original
    assert changed_files(repo) == []


# --- measured on the host


def test_an_untested_module_is_all_missing(tmp_path: Path) -> None:
    repo = _build(tmp_path, "untested-change")
    report = measure_coverage(repo, ["pricing/discount.py"], run=local_run)
    assert report.pytest_exit_code == 0
    (entry,) = report.files
    assert (entry.statements, entry.covered) == (5, 0)
    assert entry.missing_lines == [1, 4, 6, 7, 8]


def test_new_tests_cover_the_module(tmp_path: Path) -> None:
    repo = _build(tmp_path, "untested-change")
    before = measure_coverage(repo, ["pricing/discount.py"], run=local_run)
    (repo / "tests" / "test_discount.py").write_text(DISCOUNT_TEST)
    after = measure_coverage(repo, ["pricing/discount.py"], run=local_run)
    delta = coverage_delta(before, after)
    assert (delta.gained, delta.missing) == (5, 0)


def test_a_failing_suite_is_reported_not_raised(tmp_path: Path) -> None:
    repo = _build(tmp_path, "off-by-one-red")
    report = measure_coverage(repo, ["series/rolling.py"], run=local_run)
    assert report.pytest_exit_code == 1
    assert report.files[0].covered > 0


def test_the_partly_covered_fixture_misses_the_error_branches_and_restock(tmp_path: Path) -> None:
    repo = _build(tmp_path, "partly-covered")
    report = measure_coverage(repo, ["inventory/stock.py"], run=local_run)
    assert report.files[0].missing_lines == [7, 9, 15, 16, 17, 18, 19, 20]


# --- the grader's coverage check


@pytest.fixture
def host_grader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_tests, "measure_coverage", functools.partial(measure_coverage, run=local_run)
    )


@pytest.mark.usefixtures("host_grader")
def test_the_grader_passes_tests_that_cover_every_line(tmp_path: Path) -> None:
    repo = _build(tmp_path, "untested-change")
    (repo / "tests" / "test_discount.py").write_text(DISCOUNT_TEST)
    expected = ExpectedRun(
        coverage=ExpectedCoverage(paths=("pricing/discount.py",), min_gain=5, max_missing=0)
    )
    assert check_run(expected, {}, repo) == []
    assert changed_files(repo) == ["tests/test_discount.py"]


@pytest.mark.usefixtures("host_grader")
def test_the_grader_reports_too_small_a_gain_and_the_lines_left(tmp_path: Path) -> None:
    repo = _build(tmp_path, "untested-change")
    (repo / "tests" / "test_discount.py").write_text(
        DISCOUNT_TEST.split("\n\n\ndef test_rejects")[0] + "\n"
    )
    expected = ExpectedRun(
        coverage=ExpectedCoverage(paths=("pricing/discount.py",), min_gain=5, max_missing=0)
    )
    failures = check_run(expected, {}, repo)
    assert len(failures) == 2
    assert failures[0].startswith("coverage gained 4 lines, expected at least 5")
    assert "pricing/discount.py 7" in failures[1]


@pytest.mark.usefixtures("host_grader")
def test_the_grader_reports_a_bad_path(tmp_path: Path) -> None:
    repo = _build(tmp_path, "untested-change")
    expected = ExpectedRun(coverage=ExpectedCoverage(paths=("nope.py",)))
    (failure,) = check_run(expected, {}, repo)
    assert failure.startswith("coverage: target path 'nope.py'")


def test_the_grader_reports_a_run_without_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_report(root: Path, argv: Sequence[str]) -> SandboxResult:
        return SandboxResult(argv=tuple(argv), exit_code=4, output=b"boom", seconds=0.0)

    monkeypatch.setattr(
        sandbox_tests, "measure_coverage", functools.partial(measure_coverage, run=no_report)
    )
    repo = _build(tmp_path, "untested-change")
    expected = ExpectedRun(coverage=ExpectedCoverage(paths=("pricing/discount.py",)))
    (failure,) = check_run(expected, {}, repo)
    assert failure.startswith("coverage could not be measured")


def test_expected_coverage_rejects_repeats_and_an_empty_gain() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        ExpectedCoverage(paths=("a.py", "a.py"))
    with pytest.raises(ValueError):
        ExpectedCoverage(paths=("a.py",), min_gain=0)
    with pytest.raises(ValueError):
        ExpectedCoverage(paths=())


# --- in the sandbox


@pytest.mark.skipif(not _image_present(), reason=f"docker image {SANDBOX_IMAGE} absent")
def test_the_cover_cases_carry_the_coverage_the_sandbox_measures(tmp_path: Path) -> None:
    cases = [
        case
        for case in load_cases(AGENTS_DIR / "test-writer" / "evals" / "cases.yaml")
        if case.payload.get("mode") == "cover"
    ]
    assert cases
    for case in cases:
        repo = build([case.fixture], dest=tmp_path / case.id)[0].path
        measured = measure_coverage(repo, case.payload["target_paths"])
        assert measured == CoverageReport.model_validate(case.payload["coverage"]), case.id
