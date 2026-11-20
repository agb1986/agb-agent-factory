"""End-to-end tests for `factory ticket`, run through `factory.cli.main` on a store under tmp_path."""

import json
from pathlib import Path
from typing import Any

import pytest

from factory.cli import main
from factory.exit import ExitCode

ADD = [
    "ticket", "add", "--kind", "feature", "--title", "Add the thing", "--body", "Context.",
    "--acceptance", "the thing exists", "--repo", "agb1986/agb-agent-factory", "--ref", "main",
]  # fmt: skip


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    return path


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


def test_the_whole_life_of_an_item_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    first = run_json(ADD, capsys)
    assert (first["id"], first["status"], first["cost_usd"]) == ("wi-1", "todo", 0.0)
    assert first["events"][0]["from_status"] is None
    assert first["created_at"].endswith("Z")
    second = run_json(
        [*ADD, "--depends-on", "wi-1", "--size", "S", "--impacted-path", "runtime/x.py",
         "--parent", "wi-1", "--reason", "split from wi-1"],
        capsys,
    )  # fmt: skip
    assert (second["depends_on"], second["size"], second["impacted_paths"]) == (
        ["wi-1"],
        "S",
        ["runtime/x.py"],
    )
    ready = run_json(["ticket", "ready"], capsys)
    assert (ready["promoted"], ready["blocked"]) == (["wi-1"], [])
    assert [item["id"] for item in ready["ready"]] == ["wi-1"]
    for to in ("in_progress", "in_review", "done"):
        moved = run_json(["ticket", "move", "wi-1", to, "--reason", f"to {to}"], capsys)
    assert moved["status"] == "done"
    assert [e["to_status"] for e in moved["events"]] == [
        "todo",
        "ready",
        "in_progress",
        "in_review",
        "done",
    ]
    linked = run_json(["ticket", "link", "wi-1", "p1"], capsys)
    assert linked["pipeline_run_ids"] == ["p1"]
    assert run_json(["ticket", "ready"], capsys)["promoted"] == ["wi-2"]
    listed = run_json(["ticket", "list", "--status", "ready"], capsys)
    assert [item["id"] for item in listed["work_items"]] == ["wi-2"]
    assert len(run_json(["ticket", "list"], capsys)["work_items"]) == 2
    assert run_json(["ticket", "show", "wi-2"], capsys)["status"] == "ready"


def test_the_text_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_text(ADD, capsys).startswith("wi-1  todo  feature  Add the thing\n")
    assert run_text(["ticket", "ready"], capsys) == (
        "promoted wi-1\n1 ready:\n  wi-1\tfeature\tAdd the thing\n"
    )
    shown = run_text(["ticket", "link", "wi-1", "p1"], capsys)
    for line in (
        "repo: agb1986/agb-agent-factory @ main",
        "cost so far: $0.0000",
        "pipeline runs: p1",
        "  - the thing exists",
        "Context.",
        "(new) -> todo  human: created",
        "todo -> ready  store: no dependencies",
    ):
        assert line in shown
    assert run_text(["ticket", "list"], capsys) == "wi-1\tready\tfeature\tAdd the thing\n"
    moved = run_text(["ticket", "move", "wi-1", "dropped", "--reason", "dup"], capsys)
    assert "ready -> dropped  human: dup" in moved
    assert run_text(["ticket", "ready"], capsys) == "0 ready\n"
    assert run_text(["ticket", "list", "--status", "todo"], capsys) == ""


def test_an_illegal_move_exits_2_with_the_legal_ones_as_a_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_json(ADD, capsys)
    error = run_json(["ticket", "move", "wi-1", "done", "--reason", "x"], capsys, ExitCode.USAGE)
    assert error["code"] == 2
    assert error["message"] == "wi-1 cannot move from todo to done"
    assert error["hint"] == "from todo: blocked, dropped, ready"
    assert run_json(["ticket", "show", "wi-1"], capsys)["status"] == "todo"
    text = run_text(["ticket", "move", "wi-1", "todo", "--reason", "x"], capsys, ExitCode.USAGE)
    assert "wi-1 is already todo" in text


def test_an_unknown_id_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["ticket", "show", "wi-9"],
        ["ticket", "move", "wi-9", "ready", "--reason", "x"],
        ["ticket", "link", "wi-9", "p1"],
        [*ADD, "--depends-on", "wi-9"],
    ):
        assert run_json(argv, capsys, ExitCode.NOT_FOUND)["code"] == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["ticket"],
        ["ticket", "add", "--kind", "feature"],
        [*ADD[:5], "two\nlines", *ADD[6:]],
        [*ADD, "--impacted-path", "/abs"],
        [*ADD, "--size", "XL"],
        [*ADD[:2], "--kind", "chore", *ADD[4:]],
        ["ticket", "move", "wi-1", "shipped", "--reason", "x"],
        ["ticket", "move", "wi-1", "ready"],
        ["ticket", "list", "--status", "shipped"],
    ],
)
def test_bad_arguments_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    assert main(argv) == ExitCode.USAGE


def test_a_bad_field_names_the_field(capsys: pytest.CaptureFixture[str]) -> None:
    error = run_json([*ADD, "--repo", "not a repo"], capsys, ExitCode.USAGE)
    assert error["message"].startswith("repo: ")
    assert error["hint"] == "factory ticket add --help"
    assert run_json(["ticket", "list"], capsys) == {"work_items": []}


def test_an_empty_reason_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(ADD, capsys)
    assert run_json(["ticket", "move", "wi-1", "ready", "--reason", " "], capsys, 2)["code"] == 2
