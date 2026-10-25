"""Tests for registry/published.py and the resolver's `published_only` mode."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from evals.graders.llm_judge import JudgeVerdict
from factory.cli import main
from factory.exit import ExitCode
from registry import AgentNotFound, NotPublished, VersionNotFound, list_agents, resolve
from registry.published import command, published_set
from registry.spec import LLMSpec
from runtime.trace import Trace

MakeAgent = Callable[..., Path]

SUMMARY = "A change."
FINDING = {
    "file": "series/rolling.py",
    "line_start": 12,
    "line_end": 13,
    "severity": "major",
    "category": "logic",
    "title": "Problem",
    "body": "It is wrong.",
}
# What a good code-reviewer answers for each of its fixtures; a bad one approves everything.
GOOD: dict[str, dict[str, Any]] = {
    "off-by-one": {"verdict": "request_changes", "findings": [FINDING]},
    "clean-refactor": {"verdict": "approve", "findings": []},
    "untested-change": {
        "verdict": "approve",
        "findings": [{**FINDING, "file": "pricing/discount.py", "line_start": 4, "line_end": 8}],
    },
    "mixed": {
        "verdict": "request_changes",
        "findings": [
            {**FINDING, "file": "shop/shipping.py", "line_start": 12, "line_end": 12},
            {**FINDING, "file": "shop/format.py", "line_start": 6, "line_end": 7},
        ],
    },
}
BAD: dict[str, dict[str, Any]] = {name: {"verdict": "approve", "findings": []} for name in GOOD}


def _runner(answers: Mapping[str, Any]) -> Callable[..., BaseModel]:
    def run(spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any) -> BaseModel:
        answer = answers[Path(kwargs["repo_path"]).name]
        return spec.output_class.model_validate({"summary": SUMMARY} | answer)

    return run


def _judge(passed: bool) -> Callable[..., JudgeVerdict]:
    return lambda expected, actual: JudgeVerdict(passed=passed, score=0.9, reason="judged")


@pytest.fixture
def trace_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    return path


def _publish_rows(path: Path) -> list[tuple[str, str]]:
    with Trace(path) as trace:
        return [(row.agent, row.version) for row in trace.published()]


# --- published_set


def test_published_set_of_a_missing_store_is_empty_and_creates_nothing(trace_db: Path) -> None:
    assert published_set() == frozenset()
    assert not trace_db.exists()


def test_published_set_reads_the_env_store(trace_db: Path) -> None:
    with Trace(trace_db) as trace:
        trace.record_published("a", "1.0.0", 1.0, "sha")
        trace.record_published("a", "1.0.0", 1.0, "sha2")
    assert published_set() == {("a", "1.0.0")}


# --- resolve and list_agents with published_only


def _publish(path: Path, *versions: tuple[str, str]) -> None:
    with Trace(path) as trace:
        for name, version in versions:
            trace.record_published(name, version, 1.0, "sha")


def test_published_only_skips_an_unpublished_higher_version(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0", subdir=True)
    make_agent("alpha", "1.1.0", subdir=True)
    _publish(trace_db, ("alpha", "1.0.0"))
    assert resolve("alpha", "^1.0", agents_dir).version == "1.1.0"
    assert resolve("alpha", "^1.0", agents_dir, published_only=True).version == "1.0.0"


def test_published_only_picks_the_highest_published_version(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0", subdir=True)
    make_agent("alpha", "1.1.0", subdir=True)
    make_agent("alpha", "1.2.0", subdir=True)
    _publish(trace_db, ("alpha", "1.0.0"), ("alpha", "1.1.0"))
    assert resolve("alpha", "^1.0", agents_dir, published_only=True).version == "1.1.0"


def test_published_only_without_a_store_raises_not_published(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0")
    with pytest.raises(NotPublished) as caught:
        resolve("alpha", "^1.0", agents_dir, published_only=True)
    assert caught.value.available == ()
    assert "no published version of alpha matches ^1.0" in caught.value.message
    assert caught.value.hint is not None
    assert "factory publish-check" in caught.value.hint
    assert "--allow-unpublished" in caught.value.hint
    assert not trace_db.exists()


def test_not_published_lists_the_published_versions(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0", subdir=True)
    make_agent("alpha", "2.0.0", subdir=True)
    _publish(trace_db, ("alpha", "1.0.0"))
    with pytest.raises(NotPublished) as caught:
        resolve("alpha", "^2.0", agents_dir, published_only=True)
    assert caught.value.available == ("1.0.0",)
    assert isinstance(caught.value, VersionNotFound)
    assert caught.value.code == ExitCode.NOT_FOUND


def test_published_only_still_reports_a_missing_agent_and_a_missing_range(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0")
    _publish(trace_db, ("alpha", "1.0.0"))
    with pytest.raises(AgentNotFound) as caught:
        resolve("beta", "^1.0", agents_dir, published_only=True)
    assert not isinstance(caught.value, VersionNotFound)
    with pytest.raises(VersionNotFound) as range_caught:
        resolve("alpha", "^3.0", agents_dir, published_only=True)
    assert not isinstance(range_caught.value, NotPublished)


def test_published_only_ignores_the_row_of_another_agent(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0")
    _publish(trace_db, ("beta", "1.0.0"))
    with pytest.raises(NotPublished):
        resolve("alpha", "^1.0", agents_dir, published_only=True)


def test_list_agents_published_only(
    make_agent: MakeAgent, agents_dir: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0", subdir=True)
    make_agent("alpha", "1.1.0", subdir=True)
    make_agent("beta", "1.0.0")
    _publish(trace_db, ("alpha", "1.0.0"))
    assert [(a.name, a.version) for a in list_agents(agents_dir)] == [
        ("alpha", "1.0.0"),
        ("alpha", "1.1.0"),
        ("beta", "1.0.0"),
    ]
    assert [(a.name, a.version) for a in list_agents(agents_dir, published_only=True)] == [
        ("alpha", "1.0.0")
    ]


def test_trace_path_argument_overrides_the_env(
    make_agent: MakeAgent, agents_dir: Path, tmp_path: Path, trace_db: Path
) -> None:
    make_agent("alpha", "1.0.0")
    other = tmp_path / "other.duckdb"
    _publish(other, ("alpha", "1.0.0"))
    assert (
        resolve("alpha", "^1.0", agents_dir, published_only=True, trace_path=other).version
        == "1.0.0"
    )
    with pytest.raises(NotPublished):
        resolve("alpha", "^1.0", agents_dir, published_only=True)


# --- factory publish-check


def test_publish_check_writes_exactly_one_row_on_pass(
    trace_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = command(
        "code-reviewer", as_json=True, runner=_runner(GOOD), judge=_judge(True), workers=2
    )
    assert code == ExitCode.OK
    out, err = capsys.readouterr()
    document = json.loads(out)
    assert "code-reviewer" in err
    assert document["published"] is True
    assert document["git_sha"]
    with Trace(trace_db) as trace:
        [row] = trace.published()
    assert row.agent == "code-reviewer"
    assert row.version == resolve("code-reviewer", "^1.0").version
    assert row.pass_rate == document["pass_rate"]
    assert row.git_sha == document["git_sha"]


def test_publish_check_writes_nothing_on_fail(
    trace_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = command(
        "code-reviewer", as_json=True, runner=_runner(BAD), judge=_judge(False), workers=2
    )
    assert code == ExitCode.ERROR
    document = json.loads(capsys.readouterr().out)
    assert document["published"] is False
    assert _publish_rows(trace_db) == []


def test_publish_check_makes_the_version_resolvable_when_published_only(
    trace_db: Path,
) -> None:
    with pytest.raises(NotPublished):
        resolve("code-reviewer", "^1.0", published_only=True)
    command("code-reviewer", as_json=False, runner=_runner(GOOD), judge=_judge(True), workers=2)
    assert resolve("code-reviewer", "^1.0", published_only=True).name == "code-reviewer"


def test_publish_check_unknown_agent_exits_3(trace_db: Path) -> None:
    assert main(["publish-check", "no-such-agent"]) == ExitCode.NOT_FOUND


def test_publish_check_rejects_zero_workers(trace_db: Path) -> None:
    assert main(["publish-check", "code-reviewer", "--workers", "0"]) == ExitCode.USAGE
