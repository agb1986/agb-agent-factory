"""Tests for `pipelines.incident`: a Sentry event to an incident ticket, with every model stubbed.

The repository is the `crash-empty-average` fixture, whose `head` branch plays the release tag.
The runner and the sandbox are fakes; the trace and the work-item store are DuckDB files under
`tmp_path`. Nothing calls a model, Docker or Sentry.
"""

import copy
import json
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from factory.cli import main
from factory.exit import ExitCode, NotFoundError, UsageError
from fixtures.build import build
from pipelines import incident
from pipelines.incident import (
    ACTOR,
    BODY_SECTIONS,
    REPRODUCED_CONFIDENCE,
    TEST_WRITER,
    TRIAGE,
    IncidentAgents,
    IncidentError,
    IncidentResult,
    crash_file,
    incident_sections,
    is_reproduced,
    load_event,
    parse_event,
    release_commit,
    resolve_incident_agents,
    run_incident,
    stack_trace,
)
from registry.spec import LLMSpec
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems
from tools._common import ToolNotFoundError, resolve_commit
from tools.sandbox import SandboxResult

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "agents" / "triage-diagnostics" / "evals" / "cases.yaml"
RUN_ID = "11111111-2222-3333-4444-555555555555"
TEST_PATH = "tests/test_no_ratings.py"
TEST_CODE = (
    "from reviews.listing import product_line\n\n\n"
    "def test_a_product_with_no_ratings_has_a_line() -> None:\n"
    '    assert product_line("Travel mug", []) == "Travel mug: no ratings"\n'
)
REPRODUCED = (
    b"tests/test_no_ratings.py F\n"
    b"reviews/scores.py:7: in average_rating\n"
    b"    return round(total / len(ratings), 1)\n"
    b"E   ZeroDivisionError: division by zero\n"
    b"1 failed in 0.02s\n"
)
ASSERTION = (
    b"tests/test_no_ratings.py F\n"
    b"tests/test_no_ratings.py:5: in test_a_product_with_no_ratings_has_a_line\n"
    b"E   AssertionError: assert 'x' == 'y'\n"
    b"1 failed in 0.02s\n"
)


def _event() -> dict[str, Any]:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    (case,) = [c for c in cases if c["id"] == "empty-ratings-divide-by-zero"]
    return copy.deepcopy(case["payload"]["event"])


DIAGNOSIS: dict[str, Any] = {
    "title": "ZeroDivisionError in average_rating for a product with no ratings",
    "summary": "average_rating divides by len(ratings), which is 0 for a product without "
    "ratings. product_line passes the empty list straight through.",
    "reproduction_steps": [
        "Load a product with no ratings.",
        "Call product_line with its name and the empty list.",
        "average_rating raises ZeroDivisionError.",
    ],
    "suspected_paths": ["reviews/scores.py", "reviews/listing.py"],
    "culprit": {
        "file": "reviews/scores.py",
        "line_start": 6,
        "line_end": 7,
        "reason": "it divides by the number of ratings without checking for none.",
    },
    "confidence": 0.75,
    "reproduction_test": {"path": TEST_PATH, "code": TEST_CODE},
}


class FakeSandbox:
    """Stands in for `DockerSandbox`: records what it is asked and answers `exit_code`."""

    def __init__(self, exit_code: int = 1, output: bytes = REPRODUCED) -> None:
        self.exit_code = exit_code
        self.output = output
        self.checked = 0
        self.runs: list[tuple[Path, tuple[str, ...]]] = []
        self.cleaned: list[Path] = []

    def check(self) -> None:
        self.checked += 1

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        self.runs.append((root, tuple(argv)))
        return SandboxResult(tuple(argv), self.exit_code, self.output, 0.1)

    def cleanup(self, root: Path) -> None:
        self.cleaned.append(root)


class IncidentRunner:
    """Stands in for `runtime.runner.run`: the diagnosis, then a test-writer that writes a file."""

    def __init__(
        self,
        diagnosis: Mapping[str, Any] | None = None,
        *,
        write: bool = True,
        failing: Mapping[str, Exception] | None = None,
    ) -> None:
        self.diagnosis = dict(DIAGNOSIS if diagnosis is None else diagnosis)
        self.write = write
        self.failing = dict(failing or {})
        self.calls: list[dict[str, Any]] = []

    def __call__(self, spec: LLMSpec, payload: BaseModel, trace: Trace, **kwargs: Any) -> BaseModel:
        self.calls.append({"spec": spec, "payload": payload.model_dump(mode="json"), **kwargs})
        if spec.name in self.failing:
            raise self.failing[spec.name]
        trace.record(
            TraceRow(
                run_id=kwargs["run_id"],
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=payload.model_dump(mode="json"),
                cost_usd=0.25,
            )
        )
        if spec.name == TRIAGE[0]:
            return spec.output_class.model_validate(self.diagnosis)
        files = []
        if self.write:
            target = Path(kwargs["repo_path"]) / TEST_PATH
            target.write_text(TEST_CODE, encoding="utf-8")
            files = [TEST_PATH]
        return spec.output_class.model_validate(
            {
                "files_changed": [
                    {"path": path, "action": "created", "summary": "reproduces the event"}
                    for path in files
                ],
                "commands_run": [],
                "test_files": files,
                "fails_on_head": bool(files),
                "summary": "a test for a product with no ratings",
            }
        )


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    (fixture,) = build(["crash-empty-average"], dest=tmp_path_factory.mktemp("repos"))
    return fixture.path


@pytest.fixture(scope="module")
def agents() -> IncidentAgents:
    return resolve_incident_agents(allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def event(agents: IncidentAgents) -> Any:
    return parse_event(agents.triage, _event())


def _work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def _incident(
    agents: IncidentAgents,
    repo: Path,
    event: Any,
    trace: Trace,
    tmp_path: Path,
    *,
    runner: IncidentRunner | None = None,
    sandbox: FakeSandbox | None = None,
    **kwargs: Any,
) -> IncidentResult:
    kwargs.setdefault("work_dir", _work_dir(tmp_path))
    return run_incident(
        agents,
        repo,
        event,
        trace,
        out_dir=tmp_path / "out",
        sandbox=sandbox or FakeSandbox(),
        runner=runner or IncidentRunner(),
        pipeline_run_id=RUN_ID,
        **kwargs,
    )


# --- resolving the agents


def test_the_agents_resolve_to_a_read_only_triage_and_a_sandboxed_test_writer(
    agents: IncidentAgents,
) -> None:
    assert agents.triage.address == "triage-diagnostics@0.1.0"
    assert agents.test_writer.name == TEST_WRITER[0]
    assert agents.triage.sandbox_profile is None and agents.triage.files is None
    assert agents.test_writer.sandbox_profile == "offline"


def test_a_sandboxed_triage_agent_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = incident.resolve

    def sandboxed(name: str, *args: Any, **kwargs: Any) -> Any:
        spec = real(name, *args, **kwargs)
        return spec.model_copy(update={"sandbox": True}) if name == TRIAGE[0] else spec

    monkeypatch.setattr(incident, "resolve", sandboxed)
    with pytest.raises(UsageError, match="not read-only"):
        resolve_incident_agents(allow_unpublished=True)


def test_an_unsandboxed_test_writer_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = incident.resolve

    def unsandboxed(name: str, *args: Any, **kwargs: Any) -> Any:
        spec = real(name, *args, **kwargs)
        return spec.model_copy(update={"sandbox": False}) if name == TEST_WRITER[0] else spec

    monkeypatch.setattr(incident, "resolve", unsandboxed)
    with pytest.raises(UsageError, match="not sandboxed offline"):
        resolve_incident_agents(allow_unpublished=True)


def test_a_non_llm_agent_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = SimpleNamespace(address="triage-diagnostics@0.1.0")
    monkeypatch.setattr(incident, "resolve", lambda *args, **kwargs: stub)
    with pytest.raises(UsageError, match="not an LLM agent"):
        resolve_incident_agents(allow_unpublished=True)


# --- the event


def test_an_event_loads_from_json_and_from_a_file(tmp_path: Path) -> None:
    assert load_event(json.dumps(_event()))["short_id"] == "SHOP-7A"
    path = tmp_path / "issue.json"
    path.write_text(json.dumps(_event()), encoding="utf-8")
    assert load_event(f"@{path}")["id"] == "4501"


def test_a_missing_event_file_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError, match="no file"):
        load_event(f"@{tmp_path / 'missing.json'}")


def test_an_unreadable_event_file_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "binary.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(UsageError, match="cannot read"):
        load_event(f"@{path}")


@pytest.mark.parametrize(
    ("text", "message"),
    [("{", "not valid JSON"), ("[1, 2]", "must be a JSON object")],
)
def test_a_bad_event_is_a_usage_error(text: str, message: str) -> None:
    with pytest.raises(UsageError, match=message):
        load_event(text)


def test_an_event_without_exceptions_is_an_incident_error(agents: IncidentAgents) -> None:
    document = _event()
    document["exceptions"] = []
    with pytest.raises(IncidentError, match="exceptions"):
        parse_event(agents.triage, document)


def test_an_event_with_an_unknown_field_is_an_incident_error(agents: IncidentAgents) -> None:
    with pytest.raises(IncidentError, match="tags"):
        parse_event(agents.triage, {**_event(), "tags": {}})


# --- the release


def test_the_release_resolves_by_name(repo: Path, event: Any) -> None:
    assert release_commit(repo, event, None) == ("head", resolve_commit(repo, "head"))


def test_the_release_resolves_as_a_v_tag(
    repo: Path, agents: IncidentAgents, tmp_path: Path
) -> None:
    clone = tmp_path / "tagged"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    base = resolve_commit(clone, "origin/base")
    subprocess.run(["git", "-C", str(clone), "tag", "v1.4.0", base], check=True)
    event = parse_event(agents.triage, {**_event(), "release": "1.4.0"})
    assert release_commit(clone, event, None) == ("v1.4.0", base)


def test_a_ref_overrides_the_release(repo: Path, event: Any) -> None:
    assert release_commit(repo, event, "base") == ("base", resolve_commit(repo, "base"))


def test_a_ref_that_names_no_commit_is_not_found(repo: Path, event: Any) -> None:
    with pytest.raises(ToolNotFoundError):
        release_commit(repo, event, "no-such-ref")


def test_an_event_without_a_release_needs_a_ref(repo: Path, agents: IncidentAgents) -> None:
    event = parse_event(agents.triage, {**_event(), "release": None})
    with pytest.raises(IncidentError, match="names no release") as raised:
        release_commit(repo, event, None)
    assert "--ref" in (raised.value.hint or "")


def test_an_unknown_release_is_an_incident_error(repo: Path, agents: IncidentAgents) -> None:
    event = parse_event(agents.triage, {**_event(), "release": "2.0.0"})
    with pytest.raises(IncidentError, match="not a tag or commit") as raised:
        release_commit(repo, event, None)
    assert "2.0.0, v2.0.0" in (raised.value.hint or "")


# --- mapping frames to files


def _with_frames(agents: IncidentAgents, frames: list[dict[str, Any]]) -> Any:
    document = _event()
    document["exceptions"][0]["frames"] = frames
    return parse_event(agents.triage, document)


def test_the_crash_file_is_the_innermost_in_app_frame(event: Any, repo: Path) -> None:
    assert crash_file(event, repo) == "reviews/scores.py"


def test_the_crash_file_skips_library_frames(agents: IncidentAgents, repo: Path) -> None:
    event = _with_frames(
        agents,
        [
            {"file": "/app/reviews/scores.py", "line": 7, "in_app": True},
            {"file": "/usr/lib/python3.12/statistics.py", "line": 3, "in_app": False},
        ],
    )
    assert crash_file(event, repo) == "reviews/scores.py"


def test_unmarked_frames_count_when_none_is_in_app(agents: IncidentAgents, repo: Path) -> None:
    event = _with_frames(agents, [{"file": "site-packages/reviews/listing.py", "line": 8}])
    assert crash_file(event, repo) == "reviews/listing.py"


@pytest.mark.parametrize(
    "frames",
    [
        [],
        [{"file": "/app/elsewhere/other.py", "in_app": True}],
        [{"file": "/app/../reviews/scores.py", "in_app": True}],
        [{"file": "/app/reviews/scores.py", "in_app": False}],
    ],
)
def test_the_crash_file_is_none_when_no_frame_maps(
    agents: IncidentAgents, repo: Path, frames: list[dict[str, Any]]
) -> None:
    assert crash_file(_with_frames(agents, frames), repo) is None


# --- the stack trace and the body


def test_the_stack_trace_reads_like_a_python_traceback(event: Any) -> None:
    assert stack_trace(event) == (
        "Traceback (most recent call last):\n"
        '  File "/app/reviews/listing.py", line 8, in product_line\n'
        '  File "/app/reviews/scores.py", line 7, in average_rating\n'
        "ZeroDivisionError: division by zero"
    )


def test_chained_exceptions_are_joined_like_python_does(agents: IncidentAgents) -> None:
    document = _event()
    document["exceptions"].insert(0, {"type": "KeyError", "frames": [{"function": "load"}]})
    trace = stack_trace(parse_event(agents.triage, document))
    first, second = trace.split(
        "\n\nThe above exception was the cause of the following exception:\n\n"
    )
    assert first.endswith('  File "?", line ?, in load\nKeyError')
    assert second.endswith("ZeroDivisionError: division by zero")


def test_a_body_without_the_sections_in_order_is_refused() -> None:
    with pytest.raises(ValueError, match="not an incident body"):
        incident_sections("## Summary\n\ntext\n")
    swapped = "\n\n".join(f"## {name}\n\nx" for name in reversed(BODY_SECTIONS))
    with pytest.raises(ValueError, match="not an incident body"):
        incident_sections(swapped)


# --- the run


def test_a_reproduced_event_files_a_ready_incident(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    sandbox = FakeSandbox()
    result = _incident(agents, repo, event, trace, tmp_path, sandbox=sandbox)
    head = resolve_commit(repo, "head")

    assert result.reproduction.reproduced
    assert result.reproduction.test_files == (TEST_PATH,)
    assert result.confidence == REPRODUCED_CONFIDENCE
    assert result.item.status == "ready"
    assert result.item.kind == "incident"
    assert result.item.ref == head
    assert result.item.repo == str(repo.resolve())
    assert result.item.impacted_paths == ("reviews/scores.py", "reviews/listing.py")
    assert result.item.pipeline_run_ids == (RUN_ID,)

    sections = incident_sections(result.item.body)
    assert tuple(sections) == BODY_SECTIONS
    assert f"- Release: head (commit {head})" in sections["Event"]
    assert "- Crash file: reviews/scores.py" in sections["Event"]
    assert "- Events: 214, 61 users; first seen" in sections["Event"]
    assert "ZeroDivisionError: division by zero" in sections["Stack trace"]
    assert sections["Reproduction steps"].startswith("1. Load a product with no ratings.")
    assert sections["Suspected paths"] == "- `reviews/scores.py`\n- `reviews/listing.py`"
    assert sections["Culprit"].startswith("`reviews/scores.py` lines 6-7: ")
    assert "- Confidence: 0.90" in sections["Confidence"]
    assert "- Diagnosis confidence: 0.75" in sections["Confidence"]
    assert TEST_CODE.rstrip() in sections["Reproduction test"]
    assert is_reproduced(result.item.body)

    ((root, argv),) = sandbox.runs
    assert argv[-1] == TEST_PATH and "--tb=short" in argv
    assert sandbox.checked == 1 and sandbox.cleaned == [root]
    assert not root.exists()


def test_the_store_records_the_filing_and_the_promotion(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    result = _incident(agents, repo, event, trace, tmp_path)
    events = WorkItems(trace).events(result.item.id)
    assert [(e.from_status, e.to_status) for e in events] == [(None, "todo"), ("todo", "ready")]
    assert {e.actor for e in events} == {ACTOR}
    assert {e.pipeline_run_id for e in events} == {RUN_ID}
    assert "SHOP-7A" in events[0].reason and "head" in events[0].reason


def test_a_test_that_fails_for_another_reason_files_a_todo_incident(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    sandbox = FakeSandbox(exit_code=1, output=ASSERTION)
    result = _incident(agents, repo, event, trace, tmp_path, sandbox=sandbox)
    assert not result.reproduction.reproduced
    assert result.item.status == "todo"
    assert result.confidence == DIAGNOSIS["confidence"]
    assert "did not fail with ZeroDivisionError" in (result.reproduction.why_not or "")
    sections = incident_sections(result.item.body)
    assert sections["Reproduction test"].startswith("No test reproduces the event yet")
    assert not is_reproduced(result.item.body)
    assert result.reproduction.to_json()["run"]["exit_code"] == 1


def test_a_passing_test_does_not_reproduce(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    sandbox = FakeSandbox(exit_code=0, output=b"1 passed in 0.01s\n")
    result = _incident(agents, repo, event, trace, tmp_path, sandbox=sandbox)
    assert result.item.status == "todo"


def test_no_test_file_files_a_todo_incident_without_a_run(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    sandbox = FakeSandbox()
    result = _incident(
        agents, repo, event, trace, tmp_path, runner=IncidentRunner(write=False), sandbox=sandbox
    )
    assert result.item.status == "todo"
    assert result.reproduction.why_not == "the test-writer wrote no test file"
    assert sandbox.runs == []
    assert result.reproduction.to_json()["run"] is None


def test_an_event_whose_frames_map_to_no_file_is_not_reproduced(
    agents: IncidentAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    event = _with_frames(agents, [{"file": "/srv/other/app.py", "in_app": True}])
    sandbox = FakeSandbox()
    result = _incident(agents, repo, event, trace, tmp_path, sandbox=sandbox)
    assert result.item.status == "todo"
    assert "no frame" in (result.reproduction.why_not or "")
    assert sandbox.runs == []
    assert "- Crash file: unknown" in incident_sections(result.item.body)["Event"]


def test_the_test_writer_gets_a_reproduce_finding_and_the_triage_run_as_parent(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    runner = IncidentRunner()
    _incident(agents, repo, event, trace, tmp_path, runner=runner)
    triage, writer = runner.calls
    assert triage["spec"].name == TRIAGE[0]
    assert triage["payload"]["ref"] == resolve_commit(repo, "head")
    assert triage["payload"]["event"]["short_id"] == "SHOP-7A"
    assert writer["payload"]["mode"] == "reproduce"
    finding = writer["payload"]["finding"]
    assert finding["file"] == "reviews/scores.py"
    assert (finding["line_start"], finding["line_end"]) == (6, 7)
    assert finding["severity"] == "major" and finding["category"] == "crash"
    assert "no `pytest.raises`" in finding["body"] and TEST_PATH in finding["body"]
    assert writer["parent_run_id"] == triage["run_id"]
    assert {c["pipeline_run_id"] for c in runner.calls} == {RUN_ID}


def test_a_fatal_event_is_a_blocker_finding(
    agents: IncidentAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    event = parse_event(agents.triage, {**_event(), "level": "fatal"})
    runner = IncidentRunner()
    _incident(agents, repo, event, trace, tmp_path, runner=runner)
    assert runner.calls[1]["payload"]["finding"]["severity"] == "blocker"


def test_the_report_holds_the_run_and_its_cost(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    result = _incident(agents, repo, event, trace, tmp_path, repository="acme/shop")
    assert result.report_path == tmp_path / "out" / f"{RUN_ID}.json"
    document = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert document == json.loads(json.dumps(result.to_json()))
    assert document["total_cost_usd"] == pytest.approx(0.5)
    assert document["ticket"]["repo"] == "acme/shop"
    assert document["ticket"]["status"] == "ready"
    assert document["triage"]["ref"] == agents.triage.address
    assert document["reproduction"]["run"]["output_tail"].endswith("1 failed in 0.02s\n")
    assert WorkItems(trace).cost_usd(result.item.id) == pytest.approx(0.5)
    text = result.text()
    assert "reproduced by tests/test_no_ratings.py" in text and "(ready)" in text


def test_the_text_report_says_why_nothing_reproduced(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    result = _incident(agents, repo, event, trace, tmp_path, runner=IncidentRunner(write=False))
    assert "not reproduced: the test-writer wrote no test file" in result.text()


def test_a_failed_agent_files_nothing(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    sandbox = FakeSandbox()
    runner = IncidentRunner(failing={TEST_WRITER[0]: RuntimeError("model down")})
    with pytest.raises(RuntimeError, match="model down"):
        _incident(agents, repo, event, trace, tmp_path, runner=runner, sandbox=sandbox)
    assert WorkItems(trace).list() == []
    assert len(sandbox.cleaned) == 1


def test_a_ref_reads_another_commit(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    result = _incident(agents, repo, event, trace, tmp_path, ref="base")
    assert result.release == "base"
    assert result.item.ref == resolve_commit(repo, "base")


def test_a_work_dir_that_is_not_a_directory_is_a_usage_error(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    with pytest.raises(UsageError, match="not a directory"):
        _incident(agents, repo, event, trace, tmp_path, work_dir=tmp_path / "missing")


def test_a_work_dir_with_a_colon_is_a_usage_error(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    work = tmp_path / "a:b"
    work.mkdir()
    with pytest.raises(UsageError, match="':'"):
        _incident(agents, repo, event, trace, tmp_path, work_dir=work)


def test_an_out_dir_that_cannot_be_made_is_a_usage_error(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(UsageError, match="output directory"):
        run_incident(
            agents,
            repo,
            event,
            trace,
            out_dir=blocker / "out",
            work_dir=_work_dir(tmp_path),
            sandbox=FakeSandbox(),
            runner=IncidentRunner(),
        )


def test_an_unknown_release_stops_before_any_agent(
    agents: IncidentAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    event = parse_event(agents.triage, {**_event(), "release": "9.9.9"})
    runner, sandbox = IncidentRunner(), FakeSandbox()
    with pytest.raises(IncidentError):
        _incident(agents, repo, event, trace, tmp_path, runner=runner, sandbox=sandbox)
    assert runner.calls == [] and sandbox.checked == 0


def test_the_source_work_tree_is_never_changed(
    agents: IncidentAgents, repo: Path, event: Any, trace: Trace, tmp_path: Path
) -> None:
    before = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    _incident(agents, repo, event, trace, tmp_path)
    after = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert before == after
    assert not (repo / TEST_PATH).exists()


# --- factory incident


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> IncidentRunner:
    """Point `factory incident` at fakes: the runner, the sandbox, the trace database and the key."""
    runner = IncidentRunner()
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(incident, "run", runner)
    monkeypatch.setattr(incident, "DockerSandbox", FakeSandbox)
    return runner


def _argv(repo: Path, tmp_path: Path, *extra: str, event: str | None = None) -> list[str]:
    return [
        "incident",
        str(repo),
        "--event",
        json.dumps(_event()) if event is None else event,
        "--out-dir",
        str(tmp_path / "out"),
        "--work-dir",
        str(_work_dir(tmp_path)),
        "--allow-unpublished",
        *extra,
    ]


def test_factory_incident_prints_the_report_and_exits_0(
    cli: IncidentRunner, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(repo, tmp_path, "--json", "--repository", "acme/shop")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ticket"]["kind"] == "incident"
    assert document["ticket"]["status"] == "ready"
    assert document["ticket"]["repo"] == "acme/shop"
    assert Path(document["report_path"]).is_file()
    assert "incident run" in captured.err
    with Trace(tmp_path / "traces.duckdb") as trace:
        (item,) = WorkItems(trace).list()
    assert item.id == document["ticket"]["id"]


def test_factory_incident_prints_text_without_json(
    cli: IncidentRunner, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(repo, tmp_path, "--budget-usd", "1.5")) == ExitCode.OK
    out = capsys.readouterr().out
    assert out.startswith("incident run ")
    assert "reproduced by tests/test_no_ratings.py" in out and "(ready)" in out
    assert [call["budget"].usd for call in cli.calls] == [1.5, 1.5]


def test_factory_incident_needs_the_api_key(
    cli: IncidentRunner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert main(_argv(repo, tmp_path)) == ExitCode.AUTH_OR_CONFIG
    assert cli.calls == []


def test_factory_incident_checks_the_release_before_the_key(
    cli: IncidentRunner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    event = json.dumps({**_event(), "release": "9.9.9"})
    assert main(_argv(repo, tmp_path, event=event)) == ExitCode.USAGE
    assert cli.calls == []


def test_factory_incident_refuses_a_bad_event(
    cli: IncidentRunner, repo: Path, tmp_path: Path
) -> None:
    assert main(_argv(repo, tmp_path, event="not json")) == ExitCode.USAGE
    assert main(_argv(repo, tmp_path, event=f"@{tmp_path / 'none.json'}")) == ExitCode.NOT_FOUND
    assert cli.calls == []
