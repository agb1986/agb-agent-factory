"""A live round trip of `notify.issue` and `--from-issue` on a scratch repository (#95).

Needs `FACTORY_LIVE=1`, a `GH_TOKEN` that can write issues, and `FACTORY_LIVE_SCRATCH_REPO` naming
a throwaway `owner/name` repository the token's user owns or collaborates on; skipped without it.
Each run opens one issue there, posts a reply as the token's user, and closes the issue at the end
(the test closes it, never the factory). The posts are made for the `escalation-summary`
manifest, which lists `notify.issue` in `tools.allow`; no model is called.
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from registry.common_schemas import Ticket
from registry.resolver import AgentSpec, load_spec
from registry.spec import LLMSpec
from runtime.escalations import escalate
from runtime.notify import answer_from_issue, notify_escalation
from runtime.outside import OutsideContext
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tools import notify
from tools.github import gh

SCRATCH = "FACTORY_LIVE_SCRATCH_REPO"
SUMMARY_AGENT = Path(__file__).resolve().parents[2] / "agents" / "escalation-summary" / "agent.yaml"
ACTOR = "pipeline:build_loop"


@pytest.fixture
def scratch() -> str:
    full = os.environ.get(SCRATCH, "").strip()
    if not full:
        pytest.skip(f"{SCRATCH} is not set")
    return full


@pytest.fixture
def clone(tmp_path: Path, scratch: str) -> Path:
    root = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    origin = f"https://github.com/{scratch}.git"
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return root


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "trace.duckdb") as t:
        yield t


def agent() -> AgentSpec:
    spec = load_spec(SUMMARY_AGENT)
    assert isinstance(spec, LLMSpec)
    assert notify.TOOL in spec.tools.allow
    return spec


@pytest.mark.live_github
def test_post_rerun_and_answer_from_the_issue(clone: Path, trace: Trace, scratch: str) -> None:
    items = WorkItems(trace)
    item_id = items.add(
        Ticket(
            kind="feature",
            title="notify.issue live test",
            body="A scratch ticket.",
            acceptance=["the escalation reaches the issue"],
            repo=scratch,
            ref="main",
        )
    ).id
    items.move(item_id, "ready", "go")
    items.link_run(item_id, "live-1")
    items.move(item_id, "in_progress", "start", actor=ACTOR, pipeline_run_id="live-1")
    record = escalate(
        trace,
        item_id,
        trigger="blocked_output",
        question="Which module owns the parser? (live test, ignore)",
        reason="live test",
        actor=ACTOR,
        pipeline_run_id="live-1",
    )
    first = notify_escalation(
        trace,
        record.id,
        agent=agent(),
        context=OutsideContext.for_repository(clone, pipeline_run_id="live-1", trace=trace),
        summary="A live test of notify.issue.",
    )
    number = first["issue"]
    try:
        assert (first["outcome"], first["action"], first["error"]) == ("written", "create", None)
        assert items.get(item_id).issue_number == number

        rerun = notify_escalation(
            trace,
            record.id,
            agent=agent(),
            context=OutsideContext.for_repository(clone, pipeline_run_id="live-2", trace=trace),
            summary="A live test of notify.issue, rerun.",
        )
        assert (rerun["outcome"], rerun["action"], rerun["issue"]) == ("written", "edit", number)

        time.sleep(2)  # GitHub's timestamps are in seconds; the reply must come after the post
        gh(
            f"repos/{scratch}/issues/{number}/comments",
            clone,
            method="POST",
            data={"body": "Use x."},
        )
        done = answer_from_issue(trace, record.id, cwd=clone)
        assert done.record.answer == "Use x."
        assert str(done.record.answer_source).startswith(f"github:{scratch}#{number} comment ")
        assert items.get(item_id).status == "ready"
    finally:
        if isinstance(number, int):
            gh(f"repos/{scratch}/issues/{number}", clone, method="PATCH", data={"state": "closed"})
