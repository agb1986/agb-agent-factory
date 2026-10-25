"""Live checks of the eval runner against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. The first test sends one request to the judge model and
checks that it is a listed model and that the judge's request is accepted. The second runs
`factory eval code-reviewer@^1.2` end to end: four reviews by the agent's model and up to four
judge calls, all traced under the eval's tag.
"""

import json
import shutil
from pathlib import Path

import anthropic
import pytest

from evals.cases import Expected, ExpectedDefect
from evals.graders.llm_judge import JUDGE_MODEL, JUDGE_REF, LLMJudge
from factory.cli import main
from runtime.trace import Trace

EXPECTED = Expected(
    verdict="request_changes",
    defects=[
        ExpectedDefect(
            id="off-by-one",
            file="series/rolling.py",
            line_start=12,
            line_end=13,
            severity_floor="major",
        )
    ],
)
ACTUAL = {
    "verdict": "request_changes",
    "summary": "The window is one element too long.",
    "findings": [
        {
            "file": "series/rolling.py",
            "line_start": 12,
            "line_end": 13,
            "severity": "major",
            "category": "logic",
            "title": "Off-by-one in the rolling window",
            "body": "The slice end is len(window) + 1, so each mean covers one extra element.",
        }
    ],
}


@pytest.mark.live
def test_the_judge_model_is_listed_and_its_request_is_accepted(tmp_path: Path) -> None:
    with anthropic.Anthropic() as client:
        assert JUDGE_MODEL in {model.id for model in client.models.list()}
        with Trace(tmp_path / "trace.duckdb") as trace:
            judge = LLMJudge(trace, tag="eval:live-check", client=client)
            verdict = judge(EXPECTED, ACTUAL)
            rows = trace._connect().execute("SELECT agent_ref, tag, cost_usd, error FROM traces")
            [row] = rows.fetchall()
    assert 0.0 <= verdict.score <= 1.0
    assert verdict.reason
    assert row[0] == JUDGE_REF
    assert row[1] == "eval:live-check"
    assert row[2] is not None and row[2] > 0
    assert row[3] is None


@pytest.mark.live
def test_factory_eval_runs_the_code_reviewer_cases_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "trace.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(db))
    if shutil.which("rg") is None:
        pytest.skip("rg is not on PATH; the code-reviewer's rg.search tool needs it")

    code = main(["eval", "code-reviewer@^1.2", "--json"])
    out, err = capsys.readouterr()
    document = json.loads(out)
    print(err)
    print(json.dumps(document, indent=2))

    assert code in (0, 1)
    assert document["total"] == 4
    assert document["tag"] == "eval:code-reviewer@1.2.0"
    assert (code == 0) == document["passed"]
    with Trace(db) as trace:
        tagged = (
            trace._connect()
            .execute(
                "SELECT agent_ref, count(*) FROM traces WHERE tag = 'eval:code-reviewer@1.2.0' "
                "GROUP BY agent_ref"
            )
            .fetchall()
        )
    counts = dict(tagged)
    assert counts["code-reviewer@1.2.0"] == 4
