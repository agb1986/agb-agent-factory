"""Tests for pipelines/docs_check.py: what a docs change may touch, alone and on a git repository.

No Docker, no API key and no network. The rule tests feed `check_change` the text of one file before
and after; the `DocsCheck` tests commit to a small repository under `tmp_path` and pass the check
the paths the round committed, as the build loop does. The build-loop tests reuse the fakes of
`tests/pipelines/test_build_loop.py`, with a coder that answers as the documentation agent.
"""

import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.exit import ExitCode
from pipelines.behaviour import KindCheck
from pipelines.build_loop import (
    KIND_CHECKS,
    BuildAgents,
    BuildResult,
    coder_for,
    resolve_build_agents,
    run_build,
)
from pipelines.docs_check import (
    COMMENT_SYNTAX,
    BaseUnparsableError,
    CodeChangedError,
    DocsCheck,
    DocsCheckError,
    SourceFileActionError,
    UncheckedFileError,
    UnparsableSourceError,
    check_change,
    code_lines,
    is_docs_path,
    strip_docstrings,
)
from registry.common_schemas import Ticket
from registry.spec import LLMSpec
from runtime.escalations import list_escalations
from runtime.hooks import ToolCall
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems
from tests.gh_fake import FakeGh, GithubRepo, git, github_repo
from tests.pipelines.test_build_loop import (
    FULL,
    PULL_JSON,
    REPO_JSON,
    RUN_ID,
    BuildRunner,
    _ready,
    _work_dir,
    _write,
)
from tests.pipelines.test_fix_loop import FakeSandbox
from tests.pipelines.test_review_panel import FakeJev

SOURCE = """def slugify(text: str, sep: str = "-") -> str:
    found = text.split()
    return sep.join(found)
"""
DOCUMENTED = '''"""Make slugs."""


def slugify(text: str, sep: str = "-") -> str:
    """Join the words of `text` with `sep`."""
    # split on whitespace
    found = text.split()
    return sep.join(found)
'''


# --- the rules


@pytest.mark.parametrize(
    "path", ["README.md", "pkg/NOTES.md", "docs/api.md", "docs/conf/site.yaml", "docs/x"]
)
def test_docs_paths_are_markdown_anywhere_and_anything_under_docs(path: str) -> None:
    assert is_docs_path(path)


@pytest.mark.parametrize("path", ["README.rst", "pkg/docs.py", "notes/docs/a.md.txt", "a.py"])
def test_other_paths_are_not_docs(path: str) -> None:
    assert not is_docs_path(path)


def test_strip_docstrings_ignores_docstrings_and_comments_only() -> None:
    assert strip_docstrings(SOURCE) == strip_docstrings(DOCUMENTED)
    assert strip_docstrings(SOURCE) != strip_docstrings(SOURCE.replace('"-"', '"_"'))


def test_a_string_that_is_not_the_first_statement_is_code() -> None:
    late = SOURCE.replace("    found", '    x = 1\n    "not a docstring"\n    found')
    early = SOURCE.replace("    found", "    x = 1\n    found")
    assert strip_docstrings(late) != strip_docstrings(early)


def test_a_docstring_that_is_the_whole_body_still_leaves_a_body() -> None:
    assert strip_docstrings('def f():\n    """Doc."""\n') == strip_docstrings(
        "def f():\n    pass\n"
    )


def test_strip_docstrings_raises_on_source_that_does_not_parse() -> None:
    with pytest.raises(SyntaxError):
        strip_docstrings("def f(:\n")


def test_code_lines_drops_blank_lines_and_whole_line_comments_only() -> None:
    source = "# top\n\nname: app  # trailing\n  # indented\nport: 8000   \n"
    assert code_lines(source, "#") == ["name: app  # trailing", "port: 8000"]


def test_docs_files_may_be_created_and_modified() -> None:
    check_change("README.md", "modified", "# a\n", "# a\n\nMore.\n")
    check_change("docs/new.md", "created", None, "# New\n")
    check_change("docs/diagram.svg", "created", None, "<svg/>")


@pytest.mark.parametrize("path", ["README.md", "docs/api.md", "pkg/words.py"])
def test_a_deleted_file_is_refused_docs_included(path: str) -> None:
    with pytest.raises(SourceFileActionError, match="deleted"):
        check_change(path, "deleted", "x = 1\n", None)


def test_a_created_source_file_is_refused() -> None:
    with pytest.raises(SourceFileActionError, match="created the source file pkg/new.py"):
        check_change("pkg/new.py", "created", None, '"""Doc."""\n')


def test_a_python_file_may_change_in_docstrings_and_comments() -> None:
    check_change("pkg/slug.py", "modified", SOURCE, DOCUMENTED)


@pytest.mark.parametrize(
    "after",
    [
        SOURCE.replace('"-"', '"_"'),
        SOURCE.replace("-> str", "-> object"),
        "import os\n" + SOURCE,
        SOURCE + "\nx = 'a string, not a docstring'\n",
    ],
)
def test_a_python_code_change_is_refused(after: str) -> None:
    with pytest.raises(CodeChangedError, match="outside its docstrings"):
        check_change("pkg/slug.py", "modified", SOURCE, after)


def test_a_python_file_that_does_not_parse_is_refused() -> None:
    with pytest.raises(UnparsableSourceError, match="pkg/slug.py does not parse after the round"):
        check_change("pkg/slug.py", "modified", SOURCE, SOURCE + "def broken(:\n")


def test_a_python_file_that_did_not_parse_at_the_start_commit_is_the_repositorys_fault() -> None:
    with pytest.raises(BaseUnparsableError, match="does not parse at the round's start commit"):
        check_change("pkg/slug.py", "modified", "def broken(:\n", SOURCE)
    assert not issubclass(BaseUnparsableError, UnparsableSourceError)


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("run.sh", "echo hi\n", "# Say hello.\necho hi\n"),
        ("config/app.yaml", "port: 1\n", "# The port.\nport: 1\n\n"),
        ("web/app.ts", "let x = 1;\n", "// The count.\nlet x = 1;\n"),
    ],
)
def test_a_comment_line_change_is_allowed(path: str, before: str, after: str) -> None:
    check_change(path, "modified", before, after)


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("run.sh", "echo hi\n", "echo bye\n"),
        ("config/app.yaml", "port: 1\n", "port: 1  # the port\n"),
        ("web/app.ts", "let x = 1;\n", "/* The count. */\nlet x = 1;\n"),
    ],
)
def test_a_code_line_change_is_refused(path: str, before: str, after: str) -> None:
    with pytest.raises(CodeChangedError, match="outside its comment lines"):
        check_change(path, "modified", before, after)


@pytest.mark.parametrize("path", ["data/table.csv", "web/index.html", "Makefile", "a.json"])
def test_an_unknown_type_is_refused_so_the_check_fails_closed(path: str) -> None:
    with pytest.raises(UncheckedFileError) as caught:
        check_change(path, "modified", "a\n", "b\n")
    assert caught.value.hint is not None
    assert all(suffix in caught.value.hint for suffix in COMMENT_SYNTAX)


# --- DocsCheck on a repository


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "README.md").write_text("# pkg\n")
    (repo / "pkg" / "slug.py").write_text(SOURCE)
    git(repo, "init", "-q", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    return repo, git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, files: dict[str, str | None]) -> None:
    for path, text in files.items():
        target = repo / path
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "round")


def test_docs_check_is_a_kind_check() -> None:
    check: KindCheck = DocsCheck()  # ty checks the protocol; the loop calls these three
    assert all(callable(getattr(check, name)) for name in ("before", "after_commit", "after_suite"))


def test_docs_check_passes_a_round_that_only_documents(tmp_path: Path) -> None:
    repo, start = _repo(tmp_path)
    check = DocsCheck()
    check.before(repo, FakeSandbox(), start)
    _commit(repo, {"README.md": "# pkg\n\n`slugify` makes slugs.\n", "pkg/slug.py": DOCUMENTED})
    check.after_commit({"README.md": "modified", "pkg/slug.py": "modified"})
    check.after_suite(repo, FakeSandbox(), {})


def test_docs_check_compares_against_the_start_commit_not_the_last_round(tmp_path: Path) -> None:
    repo, start = _repo(tmp_path)
    check = DocsCheck()
    check.before(repo, FakeSandbox(), start)
    _commit(repo, {"pkg/slug.py": DOCUMENTED})
    _commit(repo, {"pkg/slug.py": DOCUMENTED.replace('"-"', '"_"')})
    with pytest.raises(CodeChangedError, match="pkg/slug.py"):
        check.after_commit({"pkg/slug.py": "modified"})


def test_docs_check_refuses_a_deleted_file(tmp_path: Path) -> None:
    repo, start = _repo(tmp_path)
    check = DocsCheck()
    check.before(repo, FakeSandbox(), start)
    _commit(repo, {"README.md": None})
    with pytest.raises(SourceFileActionError, match="deleted README.md"):
        check.after_commit({"README.md": "deleted"})


def test_docs_check_refuses_a_rename_as_a_delete_and_a_create(tmp_path: Path) -> None:
    repo, start = _repo(tmp_path)
    check = DocsCheck()
    check.before(repo, FakeSandbox(), start)
    _commit(repo, {"pkg/slug.py": None, "pkg/slugs.py": SOURCE})
    with pytest.raises(SourceFileActionError):
        check.after_commit({"pkg/slug.py": "deleted", "pkg/slugs.py": "created"})


def test_docs_check_needs_before(tmp_path: Path) -> None:
    with pytest.raises(DocsCheckError, match="before before"):
        DocsCheck().after_commit({"README.md": "modified"})


# --- inside the build loop


class DocsRunner(BuildRunner):
    """`BuildRunner` whose coder is the documentation agent: it writes `rounds[n]` and answers."""

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any] | BaseModel, trace: Trace, **kwargs: Any
    ) -> BaseModel:
        if spec.name != "documentation-agent":
            return super().__call__(spec, payload, trace, **kwargs)
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        self.calls.append({"spec": spec, "payload": data, **kwargs})
        clone, hook = Path(kwargs["repo_path"]), kwargs["hook"]
        files = self.rounds[min(len(self.coder_calls()), len(self.rounds)) - 1]
        for path, content in files.items():
            hook.before_tool(
                ToolCall("fs.write_file", {"path": path, "content": content}, spec.address)
            )
            _write(clone, path, content)
        for path, content in self.unhooked.items():
            _write(clone, path, content)
        trace.record(
            TraceRow(
                run_id=kwargs.get("run_id") or str(uuid.uuid4()),
                pipeline_run_id=kwargs["pipeline_run_id"],
                parent_run_id=kwargs.get("parent_run_id"),
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=data,
                cost_usd=0.25,
            )
        )
        kwargs["budget"].charge(0.25)
        written = [*files, *self.unhooked]
        return spec.output_class.model_validate(
            {
                "files_changed": [
                    {"path": path, "action": "modified", "summary": "Documented greet."}
                    for path in written
                ],
                "commands_run": [],
                "summary": "Documented greet in app.py and the README.",
                "docs_changed": [
                    {"path": path, "what": "Describes greet.", "why": "app.py prints a greeting."}
                    for path in written
                ],
                "blocked": False,
                "blocked_reason": None,
            }
        )

    def coder_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["spec"].name == "documentation-agent"]


APP = "print('hello')\n"
DOCUMENTED_APP = '"""Print the greeting."""\n\n# The one line of output.\nprint(\'hello\')\n'


def _docs_ticket() -> Ticket:
    return Ticket.model_validate(
        {
            "kind": "docs",
            "title": "Document the greeting",
            "body": "The change base..head added app.py. Document it.",
            "acceptance": ["app.py has a module docstring.", "The README names app.py."],
            "repo": FULL,
            "ref": "main",
            "impacted_paths": ["app.py"],
        }
    )


@pytest.fixture(scope="module")
def docs_agents() -> BuildAgents:
    return resolve_build_agents("docs", allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh(tmp_path, monkeypatch)
    fake.route("GET", f"repos/{FULL}", REPO_JSON)
    fake.route("POST", f"repos/{FULL}/pulls", PULL_JSON)
    return fake


@pytest.fixture
def github(tmp_path: Path, gh: FakeGh) -> GithubRepo:
    return github_repo(tmp_path)


def _document(
    agents: BuildAgents,
    github: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    runner: DocsRunner,
    sandbox: FakeSandbox,
) -> tuple[BuildResult, str]:
    item_id = _ready(trace, _docs_ticket())
    result = run_build(
        agents,
        item_id,
        trace,
        out_dir=tmp_path / "out",
        sandbox=sandbox,
        runner=runner,
        pipeline_run_id=RUN_ID,
        jev_runner=FakeJev(),
        work_dir=_work_dir(tmp_path),
        repo_path=github.root,
    )
    return result, item_id


def test_docs_tickets_go_to_the_guarded_documentation_agent_with_the_docs_check(
    docs_agents: BuildAgents,
) -> None:
    assert coder_for("docs") == ("documentation-agent", "^0.1")
    assert docs_agents.coder.address == "documentation-agent@0.1.0"
    assert docs_agents.coder.sandbox_profile == "offline"
    assert docs_agents.coder.guard is not None and docs_agents.coder.files is not None
    assert KIND_CHECKS["docs"] is DocsCheck


def test_a_docs_round_that_only_documents_ships(
    docs_agents: BuildAgents, github: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    runner = DocsRunner(
        rounds=[{"README.md": "# widgets\n\n`app.py` greets.\n", "app.py": DOCUMENTED_APP}]
    )
    sandbox = FakeSandbox()
    result, item_id = _document(docs_agents, github, trace, tmp_path, runner, sandbox)

    assert result.code == ExitCode.OK, result.document["stopped"]
    doc = result.document
    assert doc["outcome"] == "shipped" and doc["coder"] == "documentation-agent@0.1.0"
    ((entry),) = doc["rounds"]
    assert entry["files_changed"] == [
        {"path": "README.md", "action": "created"},
        {"path": "app.py", "action": "modified"},
    ]
    assert len(sandbox.runs) == 1 and len(runner.coder_calls()) == 1
    assert runner.coder_calls()[0]["payload"]["ticket"]["kind"] == "docs"
    assert WorkItems(trace).get(item_id).status == "in_review"


def test_a_docs_round_that_changes_code_stops_before_the_suite(
    docs_agents: BuildAgents, github: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    # app.py is in the agent's allow list, so the file policy lets the write through; the docs
    # check refuses the round once it is committed, because the code itself changed.
    runner = DocsRunner(rounds=[{"app.py": DOCUMENTED_APP.replace("hello", "goodbye")}])
    sandbox = FakeSandbox()
    result, item_id = _document(docs_agents, github, trace, tmp_path, runner, sandbox)

    assert result.code == ExitCode.ERROR
    doc = result.document
    assert doc["outcome"] == "stopped" and doc["pull_request"] is None
    assert doc["stopped"]["type"] == "CodeChangedError"
    assert doc["stopped"]["message"] == "app.py changed outside its docstrings and comments"
    assert sandbox.runs == []
    assert gh.writes() == []
    assert WorkItems(trace).get(item_id).status == "escalated"
    (record,) = list_escalations(trace)
    assert doc["escalation"] == record.id and record.trigger == "check_failed"
    assert record.work_item_id == item_id and record.is_open
    assert "app.py changed outside its docstrings and comments" in record.question


def test_a_docs_round_that_deletes_a_file_by_another_route_stops(
    docs_agents: BuildAgents, github: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    class Deleting(DocsRunner):
        def __call__(self, spec: LLMSpec, payload: Any, trace: Trace, **kwargs: Any) -> BaseModel:
            if spec.name == "documentation-agent":
                (Path(kwargs["repo_path"]) / "app.py").unlink()
            return super().__call__(spec, payload, trace, **kwargs)

    runner = Deleting(rounds=[{"README.md": "# widgets\n"}])
    result, item_id = _document(docs_agents, github, trace, tmp_path, runner, FakeSandbox())
    assert result.document["stopped"]["type"] == "SourceFileActionError"
    assert result.document["stopped"]["message"] == "the docs round deleted app.py"
    assert WorkItems(trace).get(item_id).status == "escalated"
    (record,) = list_escalations(trace)
    assert record.trigger == "check_failed" and result.document["escalation"] == record.id


def test_a_docs_round_on_a_file_that_did_not_parse_at_ref_blocks_the_item(
    docs_agents: BuildAgents, github: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    # The repository at ref is at fault, not the round: no answer about the docs round fixes it,
    # so the item goes to blocked with no escalation record, like a refactor's failing baseline.
    _write(github.root, "app.py", "def broken(:\n")
    git(github.root, "commit", "-qam", "break app.py")
    runner = DocsRunner(rounds=[{"app.py": "# Greets.\ndef broken(:\n"}])
    result, item_id = _document(docs_agents, github, trace, tmp_path, runner, FakeSandbox())
    assert result.document["stopped"]["type"] == "BaseUnparsableError"
    assert result.document["stopped"]["hint"].startswith("fix the file at the ticket's ref")
    assert WorkItems(trace).get(item_id).status == "blocked"
    assert result.document["escalation"] is None and list_escalations(trace) == []
