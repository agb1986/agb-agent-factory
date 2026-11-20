"""A live dry run of `factory hotfix` against the real Messages API, Jev API and github.com.

Needs `FACTORY_LIVE=1`, `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`, a `GH_TOKEN` that can read the
target repository, and `FACTORY_HOTFIX_REPO`, the `owner/name` of a scratch GitHub repository
that has a `hotfix` label; it is skipped without that variable, without Docker or without the
sandbox image. It fixes the incident ticket of tests/golden/incident.json in a throwaway repository
whose `origin` names the scratch repository, and runs as a dry run: nothing is pushed, no pull
request is opened and the work item does not move.

It checks that the reproduction test still failed the event's way, that the fixer changed the code
and not the test with its file policy on, that the suite passed in the sandbox, that the panel
reviewed the commit, that the item is still ready and linked to the run, and that every run is
traced under the one pipeline run id. The agents are resolved with `allow_unpublished`, because the
pipeline is run before the versions are published. It was not run as part of #109.
"""

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from pipelines.build_loop import FILE_WRITE_REFUSED_TAG
from pipelines.hotfix import resolve_hotfix_agents, run_hotfix
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tests.live.test_build_loop_live import _image_present
from tests.live.test_review_panel_live import _without_rg
from tests.pipelines.test_hotfix import SCORES, SOURCES, hotfix_ticket
from tools.sandbox import sandbox_image

RUN_ID = "live-hotfix-1"
TARGET = os.environ.get("FACTORY_HOTFIX_REPO", "")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "widgets"
    for path, content in SOURCES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "reviews")
    _git(root, "remote", "add", "origin", f"https://github.com/{TARGET}.git")
    return root


@pytest.mark.live
@pytest.mark.live_jev
@pytest.mark.live_github
@pytest.mark.skipif(not TARGET, reason="FACTORY_HOTFIX_REPO is not set")
@pytest.mark.skipif(not _image_present(), reason=f"docker image {sandbox_image()} absent")
def test_a_dry_run_fixes_the_incident_and_the_panel_reviews_it(tmp_path: Path) -> None:
    agents = resolve_hotfix_agents(allow_unpublished=True)
    panel = dataclasses.replace(
        agents.panel,
        reviewers=tuple(_without_rg(spec) for spec in agents.panel.reviewers),
        judge=_without_rg(agents.panel.judge),
    )
    agents = dataclasses.replace(agents, fixer=_without_rg(agents.fixer), panel=panel)
    root = _repo(tmp_path)
    before = _git(root, "rev-parse", "HEAD"), _git(root, "for-each-ref", "refs/heads", "refs/tags")
    work = tmp_path / "work"
    work.mkdir()
    with Trace(tmp_path / "trace.duckdb") as trace:
        items = WorkItems(trace)
        item = items.add(hotfix_ticket(repo=TARGET))
        items.ready()
        result = run_hotfix(
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
    assert doc["reproduction"]["reproduced"] is True
    assert doc["pull_request"]["url"] is None
    assert doc["fix"]["suite"]["passed"], doc["fix"]["suite"]["output_tail"]
    paths = {entry["path"] for entry in doc["fix"]["files_changed"]}
    assert SCORES in paths and not any(path.startswith("tests/") for path in paths), paths

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
    assert agents.fixer.address in refs
    assert {spec.address for spec in panel.reviewers} <= refs
    assert FILE_WRITE_REFUSED_TAG not in {row[1] for row in rows}
    assert all(row[3] is None for row in rows), [row for row in rows if row[3] is not None]
