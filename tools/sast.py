"""Read-only static analysis: `sast.scan` runs bandit with a pinned ruleset over the changed files.

The scan reads the head revision from the object database, never the working tree. Each changed
Python file is copied out of git into a private temporary directory under a neutral name and bandit
runs there, so no configuration, exclude list or `# nosec` comment in the repository under review
changes what is reported. Findings carry head-revision line numbers.

Bandit was chosen over semgrep because it is a small Apache-2.0 wheel locked in `uv.lock` with its
rules built in, and needs no network at scan time; the cost is that it reads Python only, so other
changed files are listed as not scanned. See `docs/CONVENTIONS.md` (Tools).
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolExecutionError,
    ToolNotFoundError,
    ToolSpec,
    ToolUnavailableError,
    fail_hint,
    git,
    resolve_commit,
    resolve_inside,
    resolve_root,
    run,
    truncate,
)

SCANNER = "bandit"
"""The scanner executable; it comes from the `bandit` dependency pinned in `pyproject.toml`."""

RULESET = (
    "B102,B103,B104,B105,B106,B107,B108,B110,B112,B113,B201,B202,B324,"
    "B301,B302,B303,B304,B305,B306,B307,B308,B310,B311,B312,B313,B314,B315,B316,B317,B318,B319,"
    "B321,B323,B401,B402,B403,B404,B405,B406,B407,B408,B409,B411,B412,B413,B415,"
    "B501,B502,B503,B504,B505,B506,B507,B508,B509,"
    "B601,B602,B603,B604,B605,B606,B607,B608,B609,B610,B611,B612,B613,B614,B615,"
    "B701,B702,B703,B704"
)
"""Every check bandit 1.9.4 ships except B101 (`assert`), named so no plugin can add a rule."""

MAX_FILE_BYTES = 1_048_576
"""A changed file larger than this is listed as not scanned rather than copied out."""

_SYMLINK_MODE = "120000"


def _changed_paths(root: Path, base: str, head: str, rel: Path | None) -> list[str]:
    """Return the paths added, copied, modified or retyped from `base` to `head`, sorted."""
    args = ["diff", "--name-only", "-z", "--no-renames", "--diff-filter=ACMT", base, head]
    if rel is not None:
        args += ["--", rel.as_posix()]
    proc = git(args, root)
    if proc.returncode != 0:
        raise ToolNotFoundError(f"git diff failed for {base}..{head}", hint=fail_hint(proc))
    return sorted(p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p)


def _blob(root: Path, head: str, path: str) -> tuple[bytes | None, str]:
    """Return the blob at `head:path` and an empty reason, or None and why it is not scanned."""
    proc = git(["ls-tree", "-z", "--long", head, "--", path], root)
    entry = proc.stdout.decode("utf-8", errors="replace").rstrip("\0")
    if proc.returncode != 0 or not entry:
        return None, "not in the head revision"
    meta = entry.split("\t", 1)[0].split()
    if meta[0] == _SYMLINK_MODE or meta[1] != "blob":
        return None, "not a regular file"
    if meta[3].isdigit() and int(meta[3]) > MAX_FILE_BYTES:
        return None, f"larger than {MAX_FILE_BYTES} bytes"
    blob = git(["cat-file", "blob", meta[2]], root)
    if blob.returncode != 0:
        raise ToolExecutionError(f"git cat-file failed for {path!r}", hint=fail_hint(blob))
    return blob.stdout, ""


def _bandit(files: dict[str, bytes]) -> dict[str, Any]:
    """Run bandit over `files`, keyed by their neutral names, and return its parsed JSON report."""
    argv = [SCANNER, "--format", "json", "--quiet", "--exit-zero", "--ignore-nosec"]
    argv += ["--severity-level", "all", "--confidence-level", "all", "--tests", RULESET]
    with tempfile.TemporaryDirectory(prefix="sast-") as tmp:
        work = Path(tmp)
        for name, data in files.items():
            (work / name).write_bytes(data)
        try:
            proc = run([*argv, "--", *sorted(files)], work)
        except ToolUnavailableError as exc:
            raise ToolUnavailableError(
                f"{SCANNER} is not installed or not on PATH",
                hint="run `uv sync`; bandit is a locked dependency of the factory",
            ) from exc
    if proc.returncode != 0:
        raise ToolExecutionError(f"{SCANNER} exited {proc.returncode}", hint=fail_hint(proc))
    try:
        report = json.loads(proc.stdout)
    except ValueError as exc:
        raise ToolExecutionError(
            f"{SCANNER} printed no JSON report", hint=fail_hint(proc) or "check bandit --version"
        ) from exc
    if not isinstance(report, dict):
        raise ToolExecutionError(f"{SCANNER} printed a report that is not a JSON object")
    return report


def _name(filename: object) -> str:
    """Return bandit's name for a scanned file without the `./` it adds."""
    text = str(filename)
    return text.removeprefix("./")


def _finding(source: str, result: dict[str, Any]) -> tuple[str, int, str]:
    """Return one bandit result as its sort key and its `path:line: RULE name: message` line."""
    line = int(result.get("line_number") or 0)
    severity = str(result.get("issue_severity", "")).lower()
    confidence = str(result.get("issue_confidence", "")).lower()
    text = (
        f"{source}:{line}: {result.get('test_id')} {result.get('test_name')}: "
        f"{result.get('issue_text')} (severity {severity}, confidence {confidence})"
    )
    return source, line, text


def scan(
    repo_path: str | Path,
    base_ref: str,
    head_ref: str,
    path: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Return bandit's findings in the files changed from `base_ref` to `head_ref`, truncated.

    The first line counts the files scanned and the findings. Each finding is one line,
    `path:line: RULE name: message (severity S, confidence C)`, sorted by path and line, with the
    line in the head revision. A changed file that is not scanned (not Python, a symlink, too large
    or unparseable) gets a `not scanned: path (reason)` line; a deleted file gets none. `path`
    limits the scan to that file or directory and is checked like `git.show_diff`'s. Unknown refs
    raise `ToolNotFoundError`, a missing scanner `ToolUnavailableError`, and a scanner that fails
    or prints no JSON report `ToolExecutionError`.
    """
    root = resolve_root(repo_path)
    rel = None if path is None else resolve_inside(root, path)
    base = resolve_commit(root, base_ref)
    head = resolve_commit(root, head_ref)
    files: dict[str, bytes] = {}
    names: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for changed in _changed_paths(root, base, head, rel):
        if not changed.endswith(".py"):
            skipped.append((changed, "not Python; bandit reads Python only"))
            continue
        data, reason = _blob(root, head, changed)
        if data is None:
            skipped.append((changed, reason))
            continue
        name = f"f{len(files) + 1:04d}.py"
        files[name] = data
        names[name] = changed
    findings: list[tuple[str, int, str]] = []
    if files:
        report = _bandit(files)
        for result in report.get("results") or []:
            name = _name(result.get("filename"))
            findings.append(_finding(names.get(name, name), result))
        for error in report.get("errors") or []:
            name = _name(error.get("filename"))
            reason = str(error.get("reason") or "bandit could not read it")
            skipped.append((names.get(name, name), reason))
    findings.sort()
    scanned, count = len(files), len(findings)
    lines = [
        (
            f"{SCANNER}: {scanned} file{'' if scanned == 1 else 's'} scanned, "
            f"{count} finding{'' if count == 1 else 's'}"
        )
    ]
    lines += [text for _, _, text in findings]
    lines += [f"not scanned: {source} ({reason})" for source, reason in sorted(skipped)]
    return truncate(("\n".join(lines) + "\n").encode(), max_bytes)


TOOLS = (
    ToolSpec(
        name="sast.scan",
        description=(
            "Run the bandit static analyser, with a pinned ruleset, over the Python files changed "
            "from one git ref to another. Returns one line per finding, path:line: rule: message, "
            "with head-revision line numbers. A finding is a lead to verify against the code, "
            "not a result."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "base_ref": {"type": "string", "description": "The older ref, the diff's left side."},
                "head_ref": {"type": "string", "description": "The newer ref, whose files are scanned."},
                "path": {"type": "string", "description": "Limit the scan to this path, relative to the repository root."},
            },
            "required": ["base_ref", "head_ref"],
            "additionalProperties": False,
        },
        impl=scan,
    ),
)  # fmt: skip
"""The tools this module declares; `tools.registry` collects them."""
