"""One endpoint that runs `factory review` and returns its JSON report.

`POST /review` takes the arguments of `factory review` and runs the command in this process, so the
behaviour, the exit codes and the trace are exactly those of the CLI. There is no authentication:
whoever can reach the port can start a review that spends the API key, so the Compose profile
publishes the port on the host's loopback address only and `docs/deploy.md` says so. Do not put it
on a network without a proxy that authenticates.

One review runs at a time. The trace is a DuckDB file that one process may write, and the command
captures stdout and stderr of the process while it runs, so a second request waits for the first.
"""

import contextlib
import io
import json
import threading
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from factory.cli import main as factory_main
from factory.exit import ExitCode

NoFlag = Annotated[str, StringConstraints(min_length=1, pattern=r"^[^-]")]
"""A path, ref or reviewer name that cannot be read as a command-line flag."""

STATUS_BY_EXIT = {
    ExitCode.ERROR: 500,
    ExitCode.USAGE: 422,
    ExitCode.NOT_FOUND: 404,
    ExitCode.AUTH_OR_CONFIG: 503,
}
"""The HTTP status for a `factory review` that ended without a report, by exit code."""

_lock = threading.Lock()

app = FastAPI(title="agb-agent-factory", description="Trigger for `factory review`. No auth.")


class ReviewRequest(BaseModel):
    """The arguments of `factory review`, under the same names."""

    repo_path: NoFlag = Field(description="path of the git repository to review, in the container")
    base: NoFlag = Field(description="git ref the change starts from")
    head: NoFlag = Field(description="git ref under review")
    reviewers: list[NoFlag] | None = Field(
        default=None, description="reviewers to run; default: every reviewer of the panel"
    )
    allow_unpublished: bool = Field(
        default=False, description="also use agent versions that have not passed publish-check"
    )
    no_triage: bool = Field(default=False, description="do not call the Jev triage agent")
    budget_usd: float | None = Field(
        default=None, gt=0, description="stop the review once it has spent this many dollars"
    )

    def argv(self) -> list[str]:
        """Return the `factory` argument list for this request, always with `--json`."""
        argv = ["review", self.repo_path, f"--base={self.base}", f"--head={self.head}", "--json"]
        if self.reviewers:
            argv.append("--reviewers=" + ",".join(self.reviewers))
        if self.allow_unpublished:
            argv.append("--allow-unpublished")
        if self.no_triage:
            argv.append("--no-triage")
        if self.budget_usd is not None:
            argv.append(f"--budget-usd={self.budget_usd}")
        return argv


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Return `{"status": "ok"}` while the process is up. It touches neither the API nor the trace."""
    return {"status": "ok"}


@app.post("/review")
def review(request: ReviewRequest) -> JSONResponse:
    """Run `factory review` and return `{"exit_code": n, "report": {...}}`.

    A change that does not ship is still a report: status 200 with `exit_code` 1 and `ship` false
    inside it, as the CLI prints it. A run that ends without a report (bad arguments, a missing
    repository, a missing key, a spent budget) returns the CLI's error envelope with the status of
    its exit code: 422, 404, 503, or 500 for any other failure.
    """
    out, err = io.StringIO(), io.StringIO()
    with _lock, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = factory_main(request.argv())
    report = _parse(out.getvalue())
    if report is not None:
        return JSONResponse({"exit_code": int(code), "report": report})
    envelope = _parse(_last_line(err.getvalue()))
    if not isinstance(envelope, dict) or "error" not in envelope:
        raise HTTPException(status_code=500, detail="review failed without a report")
    status = STATUS_BY_EXIT.get(code, 500)
    return JSONResponse(envelope, status_code=status)


def _last_line(text: str) -> str:
    """Return the last non-empty line of `text`: where the error envelope goes."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _parse(text: str) -> Any:
    """Return the JSON document in `text`, or `None` when it holds none."""
    try:
        return json.loads(text)
    except ValueError:
        return None
