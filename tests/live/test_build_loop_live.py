"""A live dry run of `factory build` against the real Messages API, Jev API and github.com.

Needs `FACTORY_LIVE=1`, `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY` and a `GH_TOKEN` that can read
public repositories, and is skipped without Docker and the sandbox image. It builds a small ticket
in a throwaway repository whose `origin` names this project's own public repository, so
`github.create_pr` can check the target, and runs as a dry run: nothing is pushed, no pull request
is opened and the work item does not move.

It checks that the feature-coder changed a file and a test with its file policy on, that the suite
passed in the sandbox, that the panel reviewed the commit, that the item is still ready and linked
to the run, and that every run is traced under the one pipeline run id. The agents are resolved
with `allow_unpublished`, because the loop is run before the versions are published.
"""

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from pipelines.build_loop import FILE_WRITE_REFUSED_TAG, resolve_build_agents, run_build
from registry.common_schemas import Ticket
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tests.live.test_review_panel_live import _without_rg
from tools.sandbox import sandbox_image

RUN_ID = "live-build-1"
FULL = "agb1986/agb-agent-factory"
ORIGIN = f"https://github.com/{FULL}.git"
FILES = {
    "shapes.py": "def area(width: int, height: int) -> int:\n    return width * height\n",
    "tests/test_shapes.py": "from shapes import area\n\n\ndef test_area() -> None:\n"
    "    assert area(2, 3) == 6\n",
}


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", sandbox_image()], capture_output=True, check=False
    )
    return proc.returncode == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "shapes"
    for path, content in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "shapes")
    _git(root, "remote", "add", "origin", ORIGIN)
    return root


@pytest.mark.live
@pytest.mark.live_jev
@pytest.mark.live_github
@pytest.mark.skipif(not _image_present(), reason=f"docker image {sandbox_image()} absent")
def test_a_dry_run_builds_the_ticket_and_the_panel_reviews_it(tmp_path: Path) -> None:
    agents = resolve_build_agents("feature", allow_unpublished=True)
    panel = dataclasses.replace(
        agents.panel,
        reviewers=tuple(_without_rg(spec) for spec in agents.panel.reviewers),
        judge=_without_rg(agents.panel.judge),
    )
    agents = dataclasses.replace(agents, coder=_without_rg(agents.coder), panel=panel)
    root = _repo(tmp_path)
    before = _git(root, "rev-parse", "HEAD"), _git(root, "for-each-ref", "refs/heads", "refs/tags")
    work = tmp_path / "work"
    work.mkdir()
    ticket = Ticket(
        kind="feature",
        title="Shapes report their perimeter",
        body="Add `perimeter(width, height)` to shapes.py, next to `area`.",
        acceptance=["perimeter(2, 3) returns 10", "a test in tests/test_shapes.py covers it"],
        repo=FULL,
        ref="main",
        impacted_paths=["shapes.py", "tests/test_shapes.py"],
    )
    with Trace(tmp_path / "trace.duckdb") as trace:
        items = WorkItems(trace)
        item = items.add(ticket)
        items.ready()
        result = run_build(
            agents,
            item.id,
            trace,
            repo_path=root,
            dry_run=True,
            out_dir=tmp_path / "out",
            work_dir=work,
            pipeline_run_id=RUN_ID,
        )
        after = items.get(item.id)
        rows = (
            trace._connect()
            .execute("SELECT agent_ref, tag, pipeline_run_id, error FROM traces")
            .fetchall()
        )

    doc = result.to_json()
    assert doc["outcome"] == "dry_run", doc["stopped"]
    assert doc["pull_request"]["url"] is None
    last = doc["rounds"][-1]
    assert last["suite"]["passed"], last["suite"]["output_tail"]
    paths = {entry["path"] for entry in last["files_changed"]}
    assert "shapes.py" in paths and any(path.startswith("tests/") for path in paths), paths

    # The item did not move, the run is linked, and the source repository is as it was.
    assert after.status == "ready" and after.pipeline_run_ids == (RUN_ID,)
    assert (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "for-each-ref", "refs/heads", "refs/tags"),
    ) == before
    assert _git(root, "status", "--porcelain") == ""
    assert list(work.iterdir()) == []

    # Every run is traced under the one pipeline run id, and no write was refused.
    assert {row[2] for row in rows} == {RUN_ID}
    refs = {row[0] for row in rows}
    assert agents.coder.address in refs
    assert {spec.address for spec in panel.reviewers} <= refs
    assert FILE_WRITE_REFUSED_TAG not in {row[1] for row in rows}
    assert all(row[3] is None for row in rows), [row for row in rows if row[3] is not None]
