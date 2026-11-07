"""Tests of the cover-mode pipeline in `pipelines/cover.py`.

The agent is a fake that writes files in the clone, but the coverage is measured for real: the
sandbox is `HostSandbox`, which runs each command with this interpreter on the host (see
`tests.evals.test_coverage.local_run`), so neither Docker nor a model is needed.
"""

import subprocess
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.exit import UsageError
from fixtures.build import build
from pipelines.cover import (
    FAILING_TEST_REJECTED_TAG,
    PRODUCTION_CHANGE_REJECTED_TAG,
    CoverError,
    resolve_cover_agent,
    run_cover,
)
from pipelines.fix_loop import TEST_WRITER
from registry.spec import LLMSpec
from runtime.trace import Trace, TraceRow
from tests.evals.test_coverage import DISCOUNT_TEST, local_run
from tools._common import ToolInputError
from tools.sandbox import SandboxResult

TARGET = "pricing/discount.py"
GOOD = {"tests/test_discount.py": DISCOUNT_TEST}
WRONG = "from pricing.discount import apply_discount\n\n\ndef test_wrong() -> None:\n    assert apply_discount(100, 0) == 1\n"


class HostSandbox:
    """Stands in for `DockerSandbox`, running each command on the host and recording it."""

    def __init__(self) -> None:
        self.checked = 0
        self.runs: list[tuple[str, ...]] = []
        self.cleaned: list[Path] = []

    def check(self) -> None:
        self.checked += 1

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        self.runs.append(tuple(argv))
        return local_run(root, argv)

    def cleanup(self, root: Path) -> None:
        self.cleaned.append(root)


class CoverRunner:
    """Stands in for `runtime.runner.run`: writes `files` in the clone and traces a priced row."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = dict(files)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, spec: LLMSpec, payload: BaseModel, trace: Trace, **kwargs: Any) -> BaseModel:
        data = payload.model_dump(mode="json")
        self.calls.append({"payload": data, **kwargs})
        clone = Path(kwargs["repo_path"])
        for path, content in self.files.items():
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        trace.record(
            TraceRow(
                run_id=kwargs["run_id"],
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=data,
                cost_usd=0.25,
            )
        )
        kwargs["budget"].charge(0.25)
        return spec.output_class.model_validate(
            {
                "files_changed": [
                    {"path": path, "action": "created", "summary": "Tests."} for path in self.files
                ],
                "commands_run": [],
                "test_files": list(self.files),
                "fails_on_head": False,
                "summary": "Covers every line.",
            }
        )


@pytest.fixture(scope="module")
def writer() -> LLMSpec:
    return resolve_cover_agent(allow_unpublished=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return build(["untested-change"], dest=tmp_path / "repos")[0].path


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


def _cover(writer: LLMSpec, repo: Path, trace: Trace, runner: CoverRunner, **kwargs: Any) -> Any:
    return run_cover(
        writer, repo, [TARGET], trace, runner=runner, sandbox=kwargs.pop("sandbox", HostSandbox()),
        pipeline_run_id="cover-run", **kwargs,
    )  # fmt: skip


def _tags(trace: Trace) -> list[tuple[str | None, str | None]]:
    return (
        trace._connect()
        .execute("SELECT tag, error FROM traces WHERE tag IS NOT NULL ORDER BY tag")
        .fetchall()
    )


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def test_the_agent_is_the_test_writer_the_fix_loop_pins(writer: LLMSpec) -> None:
    assert writer.name == TEST_WRITER[0]
    assert writer.version.startswith("0.2.")


def test_passing_tests_are_kept_and_their_coverage_measured(
    writer: LLMSpec, repo: Path, trace: Trace
) -> None:
    runner = CoverRunner(GOOD)
    sandbox = HostSandbox()
    result = _cover(writer, repo, trace, runner, sandbox=sandbox)

    (call,) = runner.calls
    assert call["payload"]["mode"] == "cover"
    assert call["payload"]["target_paths"] == [TARGET]
    assert call["payload"]["coverage"]["files"][0]["missing_lines"] == [1, 4, 6, 7, 8]
    assert call["pipeline_run_id"] == "cover-run"

    assert result.before.files[0].covered == 0
    assert result.after.files[0].covered == 5
    assert (result.delta.gained, result.delta.missing) == (5, 0)
    assert result.rejected == ()
    assert result.files_changed == {"tests/test_discount.py": "created"}
    assert b"tests/test_discount.py" in result.patch
    assert result.total_cost_usd == pytest.approx(0.25)
    assert sandbox.checked == 1 and len(sandbox.cleaned) == 1
    assert _status(repo) == ""

    report = result.to_json()
    assert report["gained"] == 5 and report["still_missing"] == {}
    assert report["test_writer"] == writer.address
    assert "test_rounds_the_discounted_price" in report["patch"]


def test_failing_tests_and_production_changes_are_rejected(
    writer: LLMSpec, repo: Path, trace: Trace
) -> None:
    runner = CoverRunner(
        {**GOOD, "tests/test_wrong.py": WRONG, TARGET: "def apply_discount(a, b):\n    return 1\n"}
    )
    result = _cover(writer, repo, trace, runner)

    assert {(item.path, item.tag) for item in result.rejected} == {
        ("tests/test_wrong.py", FAILING_TEST_REJECTED_TAG),
        (TARGET, PRODUCTION_CHANGE_REJECTED_TAG),
    }
    assert result.files_changed == {"tests/test_discount.py": "created"}
    assert b"test_wrong" not in result.patch
    assert result.delta.gained == 5
    tags = _tags(trace)
    assert [tag for tag, _ in tags] == [FAILING_TEST_REJECTED_TAG, PRODUCTION_CHANGE_REJECTED_TAG]
    assert "tests/test_wrong.py" in str(tags[0][1])


def test_an_edited_test_that_fails_is_put_back(writer: LLMSpec, repo: Path, trace: Trace) -> None:
    original = (repo / "tests" / "test_tax.py").read_text(encoding="utf-8")
    runner = CoverRunner({**GOOD, "tests/test_tax.py": original + "\n\n" + WRONG})
    result = _cover(writer, repo, trace, runner)
    assert [item.path for item in result.rejected] == ["tests/test_tax.py"]
    assert "tests/test_tax.py" not in result.files_changed


def test_only_failing_tests_is_an_error(writer: LLMSpec, repo: Path, trace: Trace) -> None:
    runner = CoverRunner({"tests/test_wrong.py": WRONG})
    with pytest.raises(CoverError, match="no test file that passes") as caught:
        _cover(writer, repo, trace, runner)
    assert "tests/test_wrong.py" in str(caught.value.hint)
    assert [tag for tag, _ in _tags(trace)] == [FAILING_TEST_REJECTED_TAG]


def test_tests_that_break_the_suite_together_are_all_rejected(
    writer: LLMSpec, repo: Path, trace: Trace
) -> None:
    # Alone, test_root.py passes; with the whole suite, tests/conftest.py breaks collection.
    runner = CoverRunner(
        {
            "test_root.py": "def test_root() -> None:\n    assert True\n",
            "tests/conftest.py": "raise RuntimeError('broken')\n",
        }
    )
    with pytest.raises(CoverError, match="the suite fails with the new tests"):
        _cover(writer, repo, trace, runner)
    assert {error for _, error in _tags(trace)} == {
        "rejected test_root.py: the whole suite fails with it",
        "rejected tests/conftest.py: the whole suite fails with it",
    }


def test_a_suite_that_fails_at_head_stops_before_the_agent(
    writer: LLMSpec, tmp_path: Path, trace: Trace
) -> None:
    red = build(["off-by-one-red"], dest=tmp_path / "red")[0].path
    runner = CoverRunner(GOOD)
    with pytest.raises(CoverError, match="the suite fails at"):
        run_cover(writer, red, ["series/rolling.py"], trace, runner=runner, sandbox=HostSandbox())
    assert runner.calls == []


def test_a_bad_target_stops_before_the_agent(writer: LLMSpec, repo: Path, trace: Trace) -> None:
    runner = CoverRunner(GOOD)
    sandbox = HostSandbox()
    with pytest.raises(ToolInputError):
        run_cover(writer, repo, ["README.md"], trace, runner=runner, sandbox=sandbox)
    assert runner.calls == [] and len(sandbox.cleaned) == 1


def test_the_sandbox_is_checked_first(writer: LLMSpec, repo: Path, trace: Trace) -> None:
    class Missing(HostSandbox):
        def check(self) -> None:
            raise UsageError("no docker")

    runner = CoverRunner(GOOD)
    with pytest.raises(UsageError, match="no docker"):
        _cover(writer, repo, trace, runner, sandbox=Missing())
    assert runner.calls == []


def test_a_work_dir_that_is_not_a_directory_is_refused(
    writer: LLMSpec, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    with pytest.raises(UsageError, match="not a directory"):
        _cover(writer, repo, trace, CoverRunner(GOOD), work_dir=tmp_path / "missing")
