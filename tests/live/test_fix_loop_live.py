"""A live end-to-end run of `factory fix` against the real Messages API and Jev API.

Skipped unless both `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` are set, and also without Docker and
the sandbox image, since the test-writer, the fixer and the suite run there. It fixes the
`off-by-one` finding of the `off-by-one-red` fixture, whose head fails one test on purpose: the
test-writer and the fixer run in a clone with the action guard on, the suite runs in the sandbox,
and the review panel, with triage in route-only mode and the judge gate, reviews the clone's diff.

It checks that the panel approved and a patch was written, that the source repository was not
touched, that the patch applies to it and makes the whole suite pass, the planted test included,
and that every run, the guard's decisions and the two Jev calls among them, is traced under the
one pipeline run id. The agents are resolved with `allow_unpublished`, because the loop is run
before the versions are published. The Jev model ids are not checked against the models listing,
which names only the rolling aliases; a rejected id shows as a triage error, which fails the test.
"""

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import anthropic
import pytest

from fixtures.build import build
from pipelines.fix_loop import TEST_WRITE_REFUSED_TAG, load_finding, resolve_fix_agents, run_fix
from pipelines.review_panel import JUDGE_GATE_TAG, TRIAGE_TAG
from pipelines.sandbox import DockerSandbox
from registry.resolver import resolve
from runtime.trace import Trace
from tests.live.test_review_panel_live import _without_rg
from tests.pipelines.test_fix_loop import FINDING
from tools.sandbox import sandbox_image

RUN_ID = "live-fix-1"
PLANTED_TEST = "tests/test_rolling.py::test_last_window_is_included"


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


@pytest.mark.live
@pytest.mark.live_jev
@pytest.mark.skipif(not _image_present(), reason=f"docker image {sandbox_image()} absent")
def test_the_loop_fixes_the_planted_defect_and_the_panel_approves(tmp_path: Path) -> None:
    agents = resolve_fix_agents(allow_unpublished=True)
    panel = dataclasses.replace(
        agents.panel,
        reviewers=tuple(_without_rg(spec) for spec in agents.panel.reviewers),
        judge=_without_rg(agents.panel.judge),
    )
    agents = dataclasses.replace(agents, panel=panel)
    assert agents.fixer.guard is not None
    name, _, version_range = agents.fixer.guard.partition("@")
    guard = resolve(name, version_range)
    with anthropic.Anthropic() as client:
        available = {model.id for model in client.models.list()}
    for spec in (agents.test_writer, agents.fixer, guard, *panel.reviewers, panel.judge):
        assert spec.model in available, spec.address
    assert panel.triage is not None
    assert panel.gate is not None

    (fixture,) = build(["off-by-one-red"], dest=tmp_path / "repos")
    before = _git(fixture.path, "rev-parse", "HEAD"), _git(fixture.path, "for-each-ref")
    work = tmp_path / "work"
    work.mkdir()
    with Trace(tmp_path / "trace.duckdb") as trace:
        result = run_fix(
            agents,
            fixture.path,
            load_finding(json.dumps(FINDING)),
            trace,
            out_dir=tmp_path / "out",
            work_dir=work,
            pipeline_run_id=RUN_ID,
        )
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, kind, tag, pipeline_run_id, cost_usd, error FROM traces "
                "ORDER BY started_at"
            )
            .fetchall()
        )
        total = trace.cost_by_pipeline_run(RUN_ID)

    # The panel approved, so the patch was written next to the report.
    assert result.ship, result.to_json()["panel"]
    assert result.patch_path is not None and result.patch_path.is_file()
    assert result.report_path.is_file()
    assert result.suite.exit_code == 0, result.suite.output[-2000:]
    by = {path: authors for path, _, authors in result.files_changed}
    assert any(path.startswith("tests/") and "test-writer" in a for path, a in by.items()), by
    assert all(
        "fixer" not in authors for path, authors in by.items() if path.startswith("tests/")
    ), by

    # The source repository is as it was, and the clone is gone.
    assert (_git(fixture.path, "rev-parse", "HEAD"), _git(fixture.path, "for-each-ref")) == before
    assert _git(fixture.path, "status", "--porcelain") == ""
    assert list(work.iterdir()) == []

    # The patch applies to the source and makes the whole suite pass, the planted test included.
    _git(fixture.path, "apply", "--check", str(result.patch_path))
    _git(fixture.path, "apply", str(result.patch_path))
    sandbox = DockerSandbox()
    try:
        planted = sandbox.run(
            fixture.path, ["pytest", "-q", "-p", "no:cacheprovider", PLANTED_TEST]
        )
        suite = sandbox.run(fixture.path, ["pytest", "-q", "-p", "no:cacheprovider"])
    finally:
        sandbox.cleanup(fixture.path)
    assert planted.exit_code == 0, planted.output[-2000:]
    assert suite.exit_code == 0, suite.output[-2000:]

    # Every run is traced under the one pipeline run id, the guard's decisions among them.
    assert rows
    assert {row[3] for row in rows} == {RUN_ID}
    assert all(row[5] is None for row in rows), [row for row in rows if row[5] is not None]
    refs = [row[0] for row in rows]
    assert agents.test_writer.address in refs
    assert agents.fixer.address in refs
    assert {spec.address for spec in panel.reviewers} <= set(refs)
    assert any(ref.startswith("action-guard@") for ref in refs), refs
    assert TEST_WRITE_REFUSED_TAG not in {row[2] for row in rows}
    llm_rows = [row for row in rows if row[1] != "jev" and row[2] is None]
    assert all(row[4] is not None and row[4] > 0 for row in llm_rows), llm_rows
    assert total == pytest.approx(result.total_cost_usd)

    # Triage ran on the sandbox diff in route-only mode: logged, and every reviewer still ran.
    tags = [row[2] for row in rows]
    assert tags.count(TRIAGE_TAG) == 1
    assert tags.count(JUDGE_GATE_TAG) == 1
    triage = result.panel.triage
    assert triage.status == "ok", triage.error
    assert result.to_json()["panel"]["triage"]["route_only"] is True
    assert len(result.panel.reviewers) == len(panel.reviewers)
    assert all(reviewer.review is not None for reviewer in result.panel.reviewers)
