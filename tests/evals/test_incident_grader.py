"""Tests for the incident grader: the case schema, the reproduction rule and the merge into `grade`.

The rule and the schema are tested without Docker. The runs against the four triage fixtures need
Docker and the sandbox image (`docker build -t factory-sandbox:0.1 -f tools/sandbox.Dockerfile
tools`); without them those tests skip. They show each eval case can be passed: a test written
the way the prompt asks reproduces the planted crash, and one that catches the exception does not.
"""

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import Case, ExpectedIncident, load_cases
from evals.graders import grade
from evals.graders import incident as incident_grader
from evals.graders.incident import check_incident, reproduces
from evals.graders.llm_judge import JudgeVerdict
from factory.exit import UsageError
from fixtures.build import build
from tools.sandbox import SANDBOX_IMAGE, SandboxResult

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "agents" / "triage-diagnostics" / "evals" / "cases.yaml"

INCIDENT = {"suspected_path": "reviews/scores.py", "exception": "ZeroDivisionError",
            "crash_file": "reviews/scores.py"}  # fmt: skip

FAILED_OUTPUT = """\
tests/test_repro.py F                                                    [100%]

=================================== FAILURES ===================================
_________________________ test_no_ratings_shows_text __________________________
tests/test_repro.py:5: in test_no_ratings_shows_text
    assert product_line("Lamp", []) == "Lamp: no ratings yet"
reviews/listing.py:8: in product_line
    average = average_rating(ratings)
reviews/scores.py:7: in average_rating
    return round(total / len(ratings), 1)
E   ZeroDivisionError: division by zero
=========================== short test summary info ============================
FAILED tests/test_repro.py::test_no_ratings_shows_text - ZeroDivisionError: division by zero
"""


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


def _case(incident: dict[str, Any] | None = INCIDENT, grader: str = "incident") -> Case:
    expected: dict[str, Any] = {"verdict": None}
    if incident is not None:
        expected["incident"] = incident
    return Case.model_validate(
        {"id": "c", "fixture": "f", "payload": {}, "expected": expected, "grader": grader}
    )


def _answer(paths: list[str], code: str = "def test_x() -> None:\n    pass\n") -> dict[str, Any]:
    return {
        "suspected_paths": paths,
        "reproduction_test": {"path": "tests/test_repro.py", "code": code},
    }


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "reviews").mkdir(parents=True)
    (repo / "reviews" / "scores.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _no_judge(expected: Any, actual: Mapping[str, Any]) -> JudgeVerdict:
    raise AssertionError("the judge must not be called")


def _fake_sandbox(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, output: str
) -> list[tuple[Path, list[str]]]:
    calls: list[tuple[Path, list[str]]] = []

    def run(root: Path, argv: list[str]) -> SandboxResult:
        calls.append((root, argv))
        assert (root / argv[-1]).is_file(), "the test is written before it runs"
        return SandboxResult(tuple(argv), exit_code, output.encode(), 0.1)

    monkeypatch.setattr(incident_grader, "run_in_sandbox", run)
    return calls


# The case schema.


def test_the_incident_grader_needs_an_expected_incident() -> None:
    with pytest.raises(ValidationError, match="incident"):
        _case(None)


def test_another_grader_must_not_carry_an_expected_incident() -> None:
    with pytest.raises(ValidationError, match="incident"):
        _case(INCIDENT, grader="llm_judge")


def test_an_expected_exception_is_a_class_name() -> None:
    with pytest.raises(ValidationError):
        ExpectedIncident.model_validate(INCIDENT | {"exception": "Zero Division"})


def test_the_triage_diagnostics_cases_load_and_all_use_the_incident_grader() -> None:
    cases = load_cases(CASES)
    assert len(cases) >= 4
    assert {case.grader for case in cases} == {"incident"}
    assert len({case.fixture for case in cases}) == len(cases)


# The reproduction rule.


def test_a_failed_run_with_the_exception_from_the_file_reproduces() -> None:
    assert reproduces(1, FAILED_OUTPUT, "ZeroDivisionError", "reviews/scores.py")


def test_a_dotted_exception_and_a_module_prefixed_error_line_match() -> None:
    output = "pkg/mod.py:3: in f\nE   json.decoder.JSONDecodeError: Expecting value\n"
    assert reproduces(1, output, "json.decoder.JSONDecodeError", "pkg/mod.py")
    assert reproduces(1, output, "JSONDecodeError", "pkg/mod.py")


def test_a_passing_run_or_a_collection_error_does_not_reproduce() -> None:
    assert not reproduces(0, FAILED_OUTPUT, "ZeroDivisionError", "reviews/scores.py")
    assert not reproduces(2, FAILED_OUTPUT, "ZeroDivisionError", "reviews/scores.py")


def test_another_exception_does_not_reproduce() -> None:
    output = FAILED_OUTPUT.replace("E   ZeroDivisionError", "E   AssertionError")
    assert not reproduces(1, output, "ZeroDivisionError", "reviews/scores.py")


def test_an_exception_name_must_match_as_a_whole_word() -> None:
    output = FAILED_OUTPUT.replace("E   ZeroDivisionError", "E   ZeroDivisionErrorish")
    assert not reproduces(1, output, "ZeroDivisionError", "reviews/scores.py")


def test_the_exception_raised_from_another_file_does_not_reproduce() -> None:
    output = FAILED_OUTPUT.replace("reviews/scores.py:7", "reviews/other.py:7")
    assert not reproduces(1, output, "ZeroDivisionError", "reviews/scores.py")


def test_a_crash_file_matches_only_a_whole_path_component() -> None:
    output = FAILED_OUTPUT.replace("reviews/scores.py:7", "oldreviews/scores.py:7")
    assert not reproduces(1, output, "ZeroDivisionError", "reviews/scores.py")
    absolute = FAILED_OUTPUT.replace("reviews/scores.py:7", "/repo/reviews/scores.py:7")
    assert reproduces(1, absolute, "ZeroDivisionError", "reviews/scores.py")


# check_incident with the sandbox stubbed.


def test_an_answer_that_names_the_path_and_reproduces_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    calls = _fake_sandbox(monkeypatch, 1, FAILED_OUTPUT)
    expected = ExpectedIncident.model_validate(INCIDENT)
    assert check_incident(expected, _answer(["reviews/scores.py"]), repo) == []
    root, argv = calls[0]
    assert argv[-1] == "tests/test_repro.py"
    assert root != repo
    assert not (repo / "tests").exists(), "the fixture is never written to"


def test_a_missing_suspected_path_fails_and_the_test_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_sandbox(monkeypatch, 1, FAILED_OUTPUT)
    failures = check_incident(
        ExpectedIncident.model_validate(INCIDENT), _answer(["reviews/listing.py"]), _repo(tmp_path)
    )
    assert len(failures) == 1
    assert "do not include reviews/scores.py" in failures[0]
    assert len(calls) == 1


def test_a_test_that_does_not_reproduce_fails_with_the_output_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sandbox(monkeypatch, 0, "1 passed in 0.01s")
    failures = check_incident(
        ExpectedIncident.model_validate(INCIDENT), _answer(["reviews/scores.py"]), _repo(tmp_path)
    )
    assert len(failures) == 1
    assert "exit code 0" in failures[0]
    assert "1 passed" in failures[0]


def test_an_answer_without_a_test_fails_without_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_sandbox(monkeypatch, 1, FAILED_OUTPUT)
    failures = check_incident(
        ExpectedIncident.model_validate(INCIDENT), {"suspected_paths": ["reviews/scores.py"]},
        _repo(tmp_path),
    )  # fmt: skip
    assert failures == ["reproduction_test has no path and code to run"]
    assert calls == []


def test_a_test_path_that_escapes_or_already_exists_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_repro.py").write_text("")
    calls = _fake_sandbox(monkeypatch, 1, FAILED_OUTPUT)
    expected = ExpectedIncident.model_validate(INCIDENT)
    existing = check_incident(expected, _answer(["reviews/scores.py"]), repo)
    assert "already exists" in existing[0]
    escape = _answer(["reviews/scores.py"])
    escape["reproduction_test"]["path"] = "../test_out.py"
    assert "is not usable" in check_incident(expected, escape, repo)[0]
    assert calls == []


# grade().


def test_grade_passes_an_incident_case_without_calling_a_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sandbox(monkeypatch, 1, FAILED_OUTPUT)
    result = grade(_case(), _answer(["reviews/scores.py"]), _no_judge, _repo(tmp_path))
    assert result.passed
    assert result.score == 1.0


def test_grade_fails_an_incident_case_with_the_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sandbox(monkeypatch, 0, "1 passed")
    result = grade(_case(), _answer(["reviews/listing.py"]), _no_judge, _repo(tmp_path))
    assert not result.passed
    assert result.score == 0.0
    assert "do not include reviews/scores.py" in result.reason
    assert "exit code 0" in result.reason


def test_grade_needs_the_repository_for_an_incident_case() -> None:
    with pytest.raises(UsageError, match="repository path"):
        grade(_case(), _answer(["reviews/scores.py"]), _no_judge)


# Real runs against the triage fixtures.

GOOD_TESTS = {
    "crash-empty-average": (
        "tests/test_no_ratings.py",
        (
            "from reviews.listing import product_line\n\n\n"
            "def test_a_product_with_no_ratings_has_a_line() -> None:\n"
            '    assert product_line("Lamp", []) == "Lamp: no ratings yet"\n'
        ),
    ),
    "crash-missing-config-key": (
        "tests/test_default_timeout.py",
        (
            "from appconfig.client import make_client\n\n\n"
            "def test_a_config_without_a_timeout_uses_the_default() -> None:\n"
            '    assert make_client({"base_url": "https://api.example/"}).timeout == 30\n'
        ),
    ),
    "crash-loader-caller": (
        "tests/test_priceless_line.py",
        (
            "from feed.pricing import feed_total\n\n\n"
            "def test_a_line_without_a_price_is_skipped() -> None:\n"
            '    assert feed_total("A1,100,2\\nC3,,4\\n") == 200\n'
        ),
    ),
    "crash-first-of-empty": (
        "tests/test_idle_worker.py",
        (
            "from jobs.worker import run_once\n\n\n"
            "def test_an_empty_queue_is_idle() -> None:\n"
            '    assert run_once([]) == "idle"\n'
        ),
    ),
}


def _incident_cases() -> dict[str, Case]:
    return {case.fixture: case for case in load_cases(CASES)}


def test_every_triage_fixture_has_a_known_reproduction() -> None:
    assert set(_incident_cases()) == set(GOOD_TESTS)


@needs_image
@pytest.mark.parametrize("fixture", sorted(GOOD_TESTS))
def test_a_reproduction_through_the_public_path_passes_the_case(
    tmp_path: Path, fixture: str
) -> None:
    case = _incident_cases()[fixture]
    assert case.expected.incident is not None
    repo = build([fixture], dest=tmp_path / "repos")[0].path
    path, code = GOOD_TESTS[fixture]
    answer = {
        "suspected_paths": [case.expected.incident.suspected_path],
        "reproduction_test": {"path": path, "code": code},
    }
    assert check_incident(case.expected.incident, answer, repo) == []


@needs_image
def test_a_reproduction_that_catches_the_exception_fails_the_case(tmp_path: Path) -> None:
    case = _incident_cases()["crash-empty-average"]
    assert case.expected.incident is not None
    repo = build(["crash-empty-average"], dest=tmp_path / "repos")[0].path
    code = (
        "import pytest\n\nfrom reviews.listing import product_line\n\n\n"
        "def test_it_raises() -> None:\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        '        product_line("Lamp", [])\n'
    )
    answer = {
        "suspected_paths": ["reviews/scores.py"],
        "reproduction_test": {"path": "tests/test_raises.py", "code": code},
    }
    failures = check_incident(case.expected.incident, answer, repo)
    assert len(failures) == 1
    assert "exit code 0" in failures[0]
