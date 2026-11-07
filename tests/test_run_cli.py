"""`factory run`: one LLM agent, once, on a JSON payload, with the model faked."""

import io
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import build_parser, main
from factory.exit import ExitCode
from registry.spec import LLMSpec
from runtime import run_once
from runtime.trace import Trace, TraceRow

SCHEMAS = """\
from pydantic import BaseModel


class In(BaseModel):
    repo_path: str
    question: str


class Out(BaseModel):
    answer: str
"""

MANIFEST = """\
name: {name}
version: {version}
kind: llm
description: Test agent.
model: claude-sonnet-5-5
effort: high
system_prompt: system.md
schemas: schemas.py
input_model: In
output_model: Out
tools:
  allow: [{tools}]
  deny: []
{web}limits:
  max_tool_turns: 4
  timeout_s: 60
  max_output_tokens: 1024
evals:
  min_pass_rate: 0.75
"""

WEB = "  web:\n    allow_domains: [docs.python.org]\n"
PAYLOAD = {"question": "What does it do?"}


def write_llm(root: Path, name: str, version: str, *, web: bool = False) -> None:
    """Write an LLM agent at `root/agents/<name>/<version>/`, with `web.fetch` when `web`."""
    directory = root / "agents" / name / version
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(
        MANIFEST.format(
            name=name,
            version=version,
            tools="git.read_file, web.fetch" if web else "git.read_file",
            web=WEB if web else "",
        )
    )
    (directory / "system.md").write_text("You answer.\n")
    (directory / "schemas.py").write_text(SCHEMAS)


def _write_jev(root: Path, name: str) -> None:
    directory = root / "agents" / name
    directory.mkdir(parents=True)
    (directory / "agent.yaml").write_text(
        f"name: {name}\nversion: 0.1.0\nkind: jev\ndescription: Test.\n"
        "model: jev-1.14.0\n"
        "input: {render: 'x {diff}', max_chars: 10}\n"
        "questions: [{id: q, type: noul, text: 'Why?'}]\n"
        "gates: []\nrouting: []\n"
        "evals: {min_pass_rate: 0.9}\n"
    )


class FakeRunner:
    """Stands in for `runtime.runner.run`: records one costed trace row and answers."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        self.calls.append({"spec": spec, "payload": dict(payload), **kwargs})
        trace.record(
            TraceRow(
                run_id=kwargs["run_id"],
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=5,
                input_json=dict(payload),
                cost_usd=0.0125,
                tag=kwargs["tag"],
            )
        )
        return spec.output_class.model_validate({"answer": "It answers."})


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A working directory with an `agents/` tree, a trace store and a test key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    write_llm(tmp_path, "answerer", "0.1.0")
    write_llm(tmp_path, "answerer", "0.2.0")
    write_llm(tmp_path, "researcher", "0.1.0", web=True)
    (tmp_path / "payload.json").write_text(json.dumps(PAYLOAD))
    return tmp_path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(run_once, "run", runner)
    return runner


def _publish(name: str, version: str) -> None:
    with Trace() as trace:
        trace.record_published(name, version, 1.0, "abc123")


def test_run_json_carries_the_agent_the_cost_and_the_output(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "answerer", "--input", "payload.json", "--allow-unpublished", "--json"]
    assert main(argv) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert set(document) == {"agent", "version", "run_id", "cost_usd", "duration_ms", "output"}
    assert document["agent"] == "answerer"
    assert document["version"] == "0.2.0"
    assert document["cost_usd"] == pytest.approx(0.0125)
    assert document["output"] == {"answer": "It answers."}
    (call,) = fake.calls
    assert call["run_id"] == call["pipeline_run_id"] == document["run_id"]
    assert call["tag"] == "run:answerer@0.2.0"
    assert call["payload"] == {**PAYLOAD, "repo_path": str(workspace)}
    assert call["repo_path"] == workspace


def test_run_text_prints_the_output_and_a_cost_line_on_stderr(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "answerer@^0.1", "--input", "payload.json", "--allow-unpublished"]
    assert main(argv) == ExitCode.OK
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"answer": "It answers."}
    assert captured.out.startswith("{\n  ")
    (line,) = captured.err.splitlines()
    assert line.startswith("answerer@0.1.0 run ")
    assert "$0.0125 in " in line


def test_run_traces_the_row_under_the_run_tag(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["run", "answerer", "--input", "payload.json", "--allow-unpublished", "--json"])
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    with Trace() as trace:
        (row,) = trace.pipeline_runs(run_id)
    assert row.tag == "run:answerer@0.2.0"


def test_run_reads_the_payload_from_stdin(
    workspace: Path,
    fake: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"question": "From stdin?"})))
    argv = ["run", "answerer", "--input", "-", "--allow-unpublished", "--json"]
    assert main(argv) == ExitCode.OK
    assert fake.calls[0]["payload"]["question"] == "From stdin?"


def test_run_takes_the_repo_flag(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = workspace / "elsewhere"
    repo.mkdir()
    argv = ["run", "answerer", "--input", "payload.json", "--repo", str(repo)]
    assert main([*argv, "--allow-unpublished"]) == ExitCode.OK
    assert fake.calls[0]["repo_path"] == repo
    assert fake.calls[0]["payload"]["repo_path"] == str(repo)


def test_run_uses_only_published_versions_by_default(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish("answerer", "0.1.0")
    assert main(["run", "answerer", "--input", "payload.json", "--json"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["version"] == "0.1.0"


def test_run_of_an_unpublished_agent_is_not_found(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "answerer", "--input", "payload.json", "--json"]) == ExitCode.NOT_FOUND
    error = json.loads(capsys.readouterr().err)["error"]
    assert "--allow-unpublished" in error["hint"]
    assert fake.calls == []


def test_run_of_an_unknown_agent_exits_3(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "no-such-agent", "--input", "payload.json", "--allow-unpublished", "--json"]
    assert main(argv) == ExitCode.NOT_FOUND
    assert json.loads(capsys.readouterr().err)["error"]["hint"] == "run `factory list`"


def test_run_of_a_jev_agent_is_a_usage_error(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_jev(workspace, "triager")
    argv = ["run", "triager", "--input", "payload.json", "--allow-unpublished", "--json"]
    assert main(argv) == ExitCode.USAGE
    assert "not an LLM agent" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert fake.calls == []


@pytest.mark.parametrize(
    ("text", "problem"),
    [("{not json", "is not JSON"), ("[1, 2]", "is not a JSON object")],
)
def test_run_with_a_bad_payload_is_a_usage_error(
    workspace: Path,
    fake: FakeRunner,
    capsys: pytest.CaptureFixture[str],
    text: str,
    problem: str,
) -> None:
    (workspace / "bad.json").write_text(text)
    argv = ["run", "answerer", "--input", "bad.json", "--allow-unpublished", "--json"]
    assert main(argv) == ExitCode.USAGE
    assert problem in json.loads(capsys.readouterr().err)["error"]["message"]
    assert fake.calls == []


def test_run_with_a_missing_payload_file_is_a_usage_error(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "answerer", "--input", "nope.json", "--allow-unpublished"]
    assert main(argv) == ExitCode.USAGE
    assert fake.calls == []


def test_run_with_a_missing_repo_is_a_usage_error(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "answerer", "--input", "payload.json", "--repo", "nope", "--allow-unpublished"]
    assert main(argv) == ExitCode.USAGE
    assert fake.calls == []


def test_run_without_a_key_is_a_config_error(
    workspace: Path,
    fake: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    argv = ["run", "answerer", "--input", "payload.json", "--allow-unpublished"]
    assert main(argv) == ExitCode.AUTH_OR_CONFIG
    assert fake.calls == []


def test_allow_domain_replaces_the_manifest_list(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "researcher", "--input", "payload.json", "--allow-unpublished"]
    hosts = ["--allow-domain", "peps.python.org", "--allow-domain", "example.com"]
    assert main([*argv, *hosts]) == ExitCode.OK
    spec = fake.calls[0]["spec"]
    assert spec.tools.web.allow_domains == ("peps.python.org", "example.com")
    assert spec.tools.allow == ("git.read_file", "web.fetch")


def test_without_allow_domain_the_manifest_list_stands(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "researcher", "--input", "payload.json", "--allow-unpublished"]
    assert main(argv) == ExitCode.OK
    assert fake.calls[0]["spec"].tools.web.allow_domains == ("docs.python.org",)


@pytest.mark.parametrize("host", ["https://example.com", "Example.com", "example.com/path", "x"])
def test_allow_domain_must_be_a_bare_lowercase_host(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    argv = ["run", "researcher", "--input", "payload.json", "--allow-unpublished", "--json"]
    assert main([*argv, "--allow-domain", host]) == ExitCode.USAGE
    assert "bare lowercase host" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert fake.calls == []


def test_allow_domain_on_an_agent_without_web_tools_is_a_usage_error(
    workspace: Path, fake: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "answerer", "--input", "payload.json", "--allow-unpublished", "--json"]
    assert main([*argv, "--allow-domain", "example.com"]) == ExitCode.USAGE
    assert "has no web tools" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert fake.calls == []


def test_run_takes_a_budget_that_is_a_number() -> None:
    args = build_parser().parse_args(["run", "a", "--input", "-", "--budget-usd", "0.5"])
    assert args.budget_usd == 0.5
    assert args.repo == "."
    assert args.allow_domains is None


def test_run_needs_an_input(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "answerer"]) == ExitCode.USAGE
