"""End-to-end tests for `factory escalations`, run through `factory.cli.main` on a tmp_path store."""

import json
from pathlib import Path
from typing import Any

import pytest
from gh_fake import FakeGh

from factory.cli import main
from factory.exit import ExitCode
from registry.common_schemas import Ticket
from runtime.escalations import (
    ANSWERS_HEADING,
    AgentOutput,
    escalate,
    store_summary,
    ticket_for_run,
)
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tools import notify
from tools.github import marker_prefix
from tools.notify import escalation_key

ACTOR = "pipeline:build_loop"
TICKET = Ticket(
    kind="feature",
    title="Add the thing",
    body="Context.",
    acceptance=["the thing exists"],
    repo="agb1986/agb-agent-factory",
    ref="main",
)


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    return path


def seed(path: Path, questions: tuple[str, ...] = ("Which module owns the parser?",)) -> str:
    """Add an item, start a run on it and escalate it once per question. Return the item id."""
    with Trace(path) as trace:
        items = WorkItems(trace)
        item_id = items.add(TICKET).id
        items.move(item_id, "ready", "go")
        items.link_run(item_id, "p1")
        items.move(item_id, "in_progress", "start", actor=ACTOR, pipeline_run_id="p1")
        for n, question in enumerate(questions):
            if n:  # a person retried by hand without answering, and the run stopped again
                items.move(item_id, "ready", "retry")
                items.move(item_id, "in_progress", "again", actor=ACTOR, pipeline_run_id="p1")
            escalate(
                trace,
                item_id,
                trigger="blocked_output",
                question=question,
                reason=f"CoderBlockedError: stop {n + 1}",
                actor=ACTOR,
                pipeline_run_id="p1",
                diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
                agent_outputs=[AgentOutput("feature-coder@0.1.0", "r1", '{"blocked": true}')],
                suite_tail="1 passed\n",
            )
    return item_id


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str], code: int = 0) -> Any:
    assert main([*argv, "--json"]) == code
    captured = capsys.readouterr()
    if code:
        assert captured.out == ""
        return json.loads(captured.err.strip().splitlines()[-1])["error"]
    return json.loads(captured.out)


def run_text(argv: list[str], capsys: pytest.CaptureFixture[str], code: int = 0) -> str:
    assert main(argv) == code
    captured = capsys.readouterr()
    return captured.out if not code else captured.err


def test_list_show_and_answer_as_json(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    item_id = seed(store)
    listed = run_json(["escalations", "list"], capsys)["escalations"]
    assert [(r["id"], r["open"], r["work_item_id"]) for r in listed] == [("esc-1", True, item_id)]
    shown = run_json(["escalations", "show", "esc-1"], capsys)
    assert shown == {**listed[0], "summary": None} and shown["from_status"] == "in_progress"
    answered = run_json(["escalations", "answer", "esc-1", "--text", "In parser.py."], capsys)
    assert answered["work_item"] == {"id": item_id, "status": "ready"}
    record = answered["escalation"]
    assert (record["answer"], record["answer_source"], record["open"]) == (
        "In parser.py.",
        "cli",
        False,
    )
    assert run_json(["escalations", "list", "--open"], capsys) == {"escalations": []}
    assert len(run_json(["escalations", "list"], capsys)["escalations"]) == 1


def test_show_prints_markdown_with_details(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    item_id = seed(store)
    text = run_text(["escalations", "show", "esc-1"], capsys)
    assert text.startswith(f"# esc-1: {item_id} escalated (blocked_output)\n")
    assert "Which module owns the parser?" in text
    assert text.count("<details>") == 3 and "```diff" in text


SUMMARY: dict[str, Any] = {
    "paragraph": "The coder stopped because the ticket does not say which module owns the parser.",
    "options": [
        {
            "label": "Name the module and run again",
            "takes": "another_run",
            "cost": "Another coder and panel run.",
            "evidence": "Which module owns the parser?",
        }
    ],
}


def add_summary(path: Path) -> None:
    with Trace(path) as trace:
        store_summary(
            trace, "esc-1", SUMMARY, run_id="run-s1", agent_ref="escalation-summary@0.1.0"
        )


def test_show_prints_the_summary_above_the_record(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = seed(store)
    add_summary(store)
    text = run_text(["escalations", "show", "esc-1"], capsys)
    assert text.startswith(f"# esc-1: {item_id} escalated (blocked_output)\n")
    summary_at = text.index("## Summary")
    assert summary_at < text.index("Which module owns the parser?\n")
    assert SUMMARY["paragraph"] in text
    assert (
        "1. **Name the module and run again** (another_run): Another coder and panel run." in text
    )
    assert "Written by `escalation-summary@0.1.0` in run `run-s1`." in text
    assert text.count("<details>") == 3


def test_show_json_carries_the_summary(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    add_summary(store)
    summary = run_json(["escalations", "show", "esc-1"], capsys)["summary"]
    assert summary["escalation_id"] == "esc-1"
    assert (summary["run_id"], summary["agent_ref"]) == ("run-s1", "escalation-summary@0.1.0")
    assert summary["paragraph"] == SUMMARY["paragraph"]
    assert summary["options"] == SUMMARY["options"]
    listed = run_json(["escalations", "list"], capsys)["escalations"]
    assert "summary" not in listed[0]


def test_show_without_a_summary_prints_the_record_alone(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    assert "## Summary" not in run_text(["escalations", "show", "esc-1"], capsys)


def test_list_and_answer_text(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    item_id = seed(store, ("First?\nmore", "Second?"))
    assert run_text(["escalations", "list"], capsys) == (
        f"esc-1\topen\t{item_id}\tblocked_output\tFirst?\n"
        f"esc-2\topen\t{item_id}\tblocked_output\tSecond?\n"
    )
    assert run_text(["escalations", "answer", "esc-1", "--text", "one"], capsys) == (
        f"esc-1 answered; {item_id} stays escalated: esc-2 open\n"
    )
    assert run_text(["escalations", "answer", "esc-2", "--text", "two"], capsys) == (
        f"esc-2 answered; {item_id} is ready\n"
    )


def test_an_answer_from_a_file_names_the_file(
    store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = seed(store)
    note = tmp_path / "answer.md"
    note.write_text("Put it in parser.py.\n", encoding="utf-8")
    record = run_json(["escalations", "answer", "esc-1", "--text", f"@{note}"], capsys)
    assert record["escalation"]["answer"] == "Put it in parser.py."
    assert record["escalation"]["answer_source"] == f"file:{note}"
    with Trace(store) as trace:
        body = ticket_for_run(trace, WorkItems(trace).get(item_id)).body
    assert ANSWERS_HEADING in body and "> Put it in parser.py." in body


def test_an_unreadable_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.md"
    error = run_json(["escalations", "answer", "esc-1", "--text", f"@{missing}"], capsys, 2)
    assert error["message"].startswith(f"cannot read the answer from {missing}")


def test_a_second_answer_exits_2(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    run_json(["escalations", "answer", "esc-1", "--text", "one"], capsys)
    error = run_json(["escalations", "answer", "esc-1", "--text", "two"], capsys, ExitCode.USAGE)
    assert error["message"] == "esc-1 is already answered"


def test_an_answer_after_a_manual_move_exits_2(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = seed(store)
    run_json(["ticket", "move", item_id, "dropped", "--reason", "not needed"], capsys)
    error = run_json(["escalations", "answer", "esc-1", "--text", "x"], capsys, ExitCode.USAGE)
    assert "no longer stands" in error["message"]
    assert run_json(["escalations", "show", "esc-1"], capsys)["open"] is True


def test_an_unknown_id_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["show", "esc-9"], ["answer", "esc-9", "--text", "x"], ["show", "wi-1"]):
        error = run_json(["escalations", *argv], capsys, ExitCode.NOT_FOUND)
        assert "esc-9" in error["message"] or "wi-1" in error["message"]


@pytest.mark.parametrize(
    "argv",
    [
        ["escalations", "answer", "esc-1"],
        ["escalations", "answer", "esc-1", "--text", "x", "--from-issue"],
        ["escalations", "show"],
        ["escalations", "bogus"],
    ],
)
def test_bad_arguments_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    assert main(argv) == ExitCode.USAGE


def test_no_subcommand_prints_usage_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["escalations"]) == ExitCode.USAGE
    assert "usage:" in capsys.readouterr().err


def test_an_answer_from_the_issue_names_the_comment(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = seed(store)
    with Trace(store) as trace:
        WorkItems(trace).set_issue_number(item_id, 9)
    full = TICKET.repo
    marker = marker_prefix(notify.TOOL, escalation_key(item_id, "esc-1"))
    fake = FakeGh(tmp_path, monkeypatch)
    fake.route(
        "GET",
        f"repos/{full}/issues/9",
        {"number": 9, "body": f"Body. {marker}p1 -->", "created_at": "2026-11-20T10:00:00Z"},
    )
    reply = {
        "id": 5,
        "body": "Use parser.py.",
        "created_at": "2026-11-20T10:05:00Z",
        "user": {"login": "alice", "type": "User"},
        "author_association": "MEMBER",
        "html_url": f"https://github.com/{full}/issues/9#issuecomment-5",
    }
    fake.route("GET", f"repos/{full}/issues/9/comments?per_page=100", [reply])
    monkeypatch.chdir(tmp_path)
    out = run_json(["escalations", "answer", "esc-1", "--from-issue"], capsys)
    assert out["escalation"]["answer"] == "Use parser.py."
    assert out["escalation"]["answer_source"] == f"github:{full}#9 comment 5"
    assert out["comment"] == {"id": 5, "author": "alice", "url": reply["html_url"]}
    assert out["work_item"] == {"id": item_id, "status": "ready"}
    assert fake.writes() == []


def test_an_answer_from_the_issue_without_an_issue_exits_2(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    error = run_json(["escalations", "answer", "esc-1", "--from-issue"], capsys, ExitCode.USAGE)
    assert "no GitHub issue" in error["message"]
