"""`factory triage`: run only the triage step of the review panel and print what it decided.

The step is `run_triage` of `pipelines.review_panel`, the same one `factory review` runs before
its reviewers, so the two cannot disagree. No reviewer and no judge runs, and no Anthropic key is
needed.
"""

import json
import sys

from factory.exit import ExitCode
from pipelines.ids import new_pipeline_run_id
from pipelines.review_panel import TriageResult, resolve_triage, run_triage
from runtime.jev_runner import run_jev
from runtime.trace import Trace
from tools import MAX_OUTPUT_BYTES
from tools.git import show_diff


def command(
    repo_path: str,
    *,
    base: str,
    head: str,
    as_json: bool,
    allow_unpublished: bool = False,
) -> int:
    """Run `factory triage`: triage `base..head` of `repo_path` and print the decision.

    Prose goes to stderr under `as_json`, where stdout gets exactly one JSON document: the
    pipeline run id, the refs and the `triage` entry of the review document. Returns 0 when the
    Jev call worked and 1 when it failed, in which case the document holds the escalation and the
    error. Unknown refs and a missing agent raise, as they do for `factory review`.
    """
    spec = resolve_triage(allow_unpublished=allow_unpublished)
    diff = show_diff(repo_path, base, head, max_bytes=MAX_OUTPUT_BYTES)
    run_id = new_pipeline_run_id()
    with Trace() as trace:
        result = run_triage(spec, diff, trace, run_id, jev_runner=run_jev)
    document = {"pipeline_run_id": run_id, "base": base, "head": head, **result.to_json()}
    if as_json:
        print(_text(result), file=sys.stderr)
        print(json.dumps(document))
    else:
        print(_text(result))
    return ExitCode.OK if result.status == "ok" else ExitCode.ERROR


def _text(result: TriageResult) -> str:
    """Return the report: the decision line, then each question's probabilities."""
    lines = [result.text()]
    if result.error:
        lines.append(f"  error: {result.error}")
    if result.truncated:
        lines.append("  the diff was cut to fit, so the answers cannot skip a review")
    for question, answer in (result.probabilities or {}).items():
        probs = answer.get("probs", {"yes": answer.get("p")})
        shown = ", ".join(f"{option} {p:.2f}" for option, p in probs.items())
        lines.append(f"  {question}: {shown}")
    return "\n".join(lines)
