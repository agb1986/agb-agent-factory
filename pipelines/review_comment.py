"""`factory review --pr N [--comment]`: review a pull request and post the findings back to it.

`resolve_pull_request` turns a pull request number into the two commits to review: its head and the
merge base of its base branch and head, which is the diff GitHub shows (and the one the inline
comments are placed on). `post_review` is the outside write: after the panel has run, pipeline code
calls `github.comment_pr` through `runtime.outside.write` for the resolved judge manifest, with the
judge's merged findings as inline comments and the verdict and score as the review's body. No model
is given the write; the rules are in `docs/decisions/0003-outside-writes.md`.

A rerun of the same command on the same pull request edits the factory's earlier review rather
than adding another: `github.comment_pr` finds it by the marker it puts in the footer.
"""

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory.exit import FactoryError
from runtime.outside import OutsideContext, write
from runtime.trace import Trace
from tools._common import (
    OutsideWriteRefused,
    ToolInputError,
    ToolNotFoundError,
    git,
    resolve_commit,
    resolve_root,
)
from tools.github import read_pr

if TYPE_CHECKING:
    from pipelines.review_panel import PanelResult

__all__ = [
    "TOOL",
    "PullRequest",
    "comment_text",
    "failure_section",
    "finding_comments",
    "post_review",
    "resolve_pull_request",
    "review_body",
]

TOOL = "github.comment_pr"
"""The outside write this module makes, by its ADR 0003 name."""

_SEVERITIES = ("blocker", "major", "minor", "info")


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A pull request resolved for review: `base` is the merge base and `head` the head commit."""

    number: int
    base: str
    head: str


def resolve_pull_request(repo_path: str | Path, number: int) -> PullRequest:
    """Return the commits to review for pull request `number` of the repository's `origin`.

    Reads the pull request through `github.read_pr` (it needs `GH_TOKEN`) and takes the merge base of
    its base and head commits from the local repository, so the commits must be there. Raises
    `ToolNotFoundError` with the fetch to run when one is missing, and the GitHub read's own errors.
    """
    root = resolve_root(repo_path)
    pull = json.loads(read_pr(root, number))
    commits = {}
    for side in ("base", "head"):
        sha = (pull.get(side) or {}).get("sha")
        if not isinstance(sha, str) or not sha:
            raise ToolNotFoundError(f"GitHub sent no {side} commit for pull request {number}")
        try:
            commits[side] = resolve_commit(root, sha)
        except ToolNotFoundError as exc:
            raise ToolNotFoundError(
                f"the {side} commit {sha} of pull request {number} is not in {str(repo_path)!r}",
                hint=f"fetch it first: git fetch origin pull/{number}/head",
            ) from exc
    proc = git(["merge-base", commits["base"], commits["head"]], root)
    if proc.returncode != 0:
        raise ToolInputError(
            f"pull request {number} has no merge base in {str(repo_path)!r}",
            hint="the clone may be shallow: git fetch --unshallow",
        )
    return PullRequest(
        number, proc.stdout.decode("ascii", errors="replace").strip(), commits["head"]
    )


def _severity_rank(severity: str) -> int:
    return _SEVERITIES.index(severity) if severity in _SEVERITIES else len(_SEVERITIES)


def _finding_text(finding: Mapping[str, Any]) -> str:
    """Return the Markdown of one merged finding: severity, category and title, then the body."""
    text = f"**{finding['severity']}** ({finding['category']}): {finding['title']}\n\n{finding['body']}"
    fix = finding.get("suggested_fix")
    return f"{text}\n\nSuggested fix: {fix}" if fix else text


def finding_comments(result: PanelResult) -> list[dict[str, Any]]:
    """Return the judge's merged findings as inline comments `{path, line, body}`.

    A comment sits on the finding's first line. Findings that start on the same line of the same
    file share one comment, because `github.comment_pr` matches an earlier comment by path and
    line. The result is empty when the judge did not run.
    """
    if result.judgement is None:
        return []
    findings = result.judgement.model_dump(mode="json")["merged_findings"]
    placed: dict[tuple[str, int], list[str]] = {}
    for finding in sorted(findings, key=lambda f: _severity_rank(f["severity"])):
        placed.setdefault((finding["file"], finding["line_start"]), []).append(
            _finding_text(finding)
        )
    return [
        {"path": path, "line": line, "body": "\n\n---\n\n".join(texts)}
        for (path, line), texts in placed.items()
    ]


def review_body(result: PanelResult, pull: PullRequest) -> str:
    """Return the review's summary: the verdict and score, the judge's rationale and the scores.

    When the judge gate skipped the judge there is no score or rationale, so the body says so and
    gives the gate's verdict and what each reviewer found. When triage skipped the review, no
    reviewer looked at the change: the body says so, names the gate that skipped and says how to
    force a full review, so that it cannot read as a review that found nothing. Reviewers that
    failed are named.
    """
    verdict = "ship" if result.ship else "do not ship"
    sections = []
    if result.skipped:
        sections.append("## Review: skipped by triage")
        sections.append(
            f"Triage judged this change low risk ({result.triage.skip_reason()}), so no reviewer "
            "and no judge looked at it. This is not a review that found nothing."
        )
        sections.append(
            "To force a full review, run "
            f"`factory review <repo_path> --pr {pull.number} --comment --no-triage`."
        )
    elif result.judgement is not None and result.assessment is not None:
        output = result.judgement.model_dump(mode="json")
        found = len(output["merged_findings"])
        sections.append(f"## Review: {verdict} (weighted score {result.assessment.score:.2f})")
        sections.append(output["rationale"])
        sections.append(
            "\n".join(
                f"- {name}: {score['value']:.2f}. {score['justification']}"
                for name, score in output["scores"].items()
            )
        )
        sections.append(f"The judge merged {found} finding{'s' if found != 1 else ''}.")
    else:
        sections.append(f"## Review: {verdict}")
        sections.append(
            "The reviewers' answers were clear-cut, so the judge did not run. "
            f"The judge gate's verdict is {result.gate.verdict}."
        )
        sections.append(
            "\n".join(
                f"- {r.spec.address}: {r.review.verdict}, {len(r.review.findings)} "
                f"finding{'s' if len(r.review.findings) != 1 else ''}"
                for r in result.reviewers
                if r.review is not None
            )
        )
    failed = [r.spec.address for r in result.reviewers if r.review is None]
    if failed:
        sections.append(f"Reviewers that failed: {', '.join(failed)}.")
    sections.append(f"Reviewed commit `{pull.head[:12]}`.")
    return "\n\n".join(section for section in sections if section)


def _judge_run_id(trace: Trace, result: PanelResult) -> str | None:
    """Return the `run_id` of the judge's row in this pipeline run, or None when it did not run."""
    for row in trace.pipeline_runs(result.pipeline_run_id):
        if row.agent_ref == result.judge.address and row.kind == "llm" and row.tag is None:
            return row.run_id
    return None


def post_review(result: PanelResult, pull: PullRequest, context: OutsideContext) -> dict[str, Any]:
    """Post `result` on the pull request through `github.comment_pr`; return the `comment` section.

    The write is made for the judge manifest `result.judge`, which lists the tool whether or not
    the judge ran; `context.parent_run_id` is set to the judge's row when there is one. The section
    is `{"outcome": "written" | "dry_run", "action": "create" | "edit", "url", "body"}`; the call's
    trace row is written by `runtime.outside.write`. Raises what it raises, `OutsideWriteRefused`
    and `ToolInputError` for a refusal, so use `failure_section` to report one.
    """
    context = dataclasses.replace(context, parent_run_id=_judge_run_id(context.trace, result))
    done = write(
        TOOL,
        {
            "pr": pull.number,
            "body": review_body(result, pull),
            "comments": finding_comments(result),
            "commit": pull.head,
        },
        agent=result.judge,
        context=context,
    )
    return {"outcome": "dry_run" if context.dry_run else "written", **done}


def comment_text(section: Mapping[str, Any]) -> str:
    """Return the one line the text report gives for a `comment` section."""
    if "error" in section:
        return f"comment: {section['outcome']}, {section['error']}"
    if section["outcome"] == "dry_run":
        return f"comment: dry run, would {section['action']} the review"
    past = {"create": "created", "edit": "edited"}.get(section["action"], section["action"])
    return f"comment: {past} the review at {section['url']}"


def failure_section(exc: FactoryError) -> dict[str, Any]:
    """Return the `comment` section for a write that did not happen: its outcome and why.

    The outcome is `refused` for a refusal or a bad argument, both made before anything was sent,
    and `failed` for any other error, after which a part of the write may have reached GitHub.
    """
    refused = isinstance(exc, OutsideWriteRefused | ToolInputError)
    return {
        "outcome": "refused" if refused else "failed",
        "error": exc.message,
        "hint": exc.hint,
    }
