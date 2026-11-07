"""Tests for `sast.scan`: a fake bandit on PATH for the contract, the real one when installed."""

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from repo_helpers import Repo, run_git

from tools import TOOLS, sast
from tools._common import (
    PathEscapeError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolUnavailableError,
)
from tools.registry import READ_ONLY, SANDBOX_DENIED, SANDBOX_ONLY

FAKE_BANDIT = """\
#!{python}
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_BANDIT_LOG"])
log.write_text(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
    "files": {{p.name: p.read_text() for p in pathlib.Path.cwd().iterdir()}}}}))
sys.stdout.write(pathlib.Path(os.environ["FAKE_BANDIT_OUT"]).read_text())
sys.stderr.write(os.environ.get("FAKE_BANDIT_ERR", ""))
sys.exit(int(os.environ.get("FAKE_BANDIT_RC", "0")))
"""


def _result(filename: str, line: int, test_id: str = "B105", **extra: Any) -> dict[str, Any]:
    return {
        "filename": filename,
        "line_number": line,
        "test_id": test_id,
        "test_name": "hardcoded_password_string",
        "issue_text": "Possible hardcoded password: 'x'",
        "issue_severity": "LOW",
        "issue_confidence": "MEDIUM",
    } | extra


class FakeBandit:
    """A `bandit` script on PATH that records its call and prints a canned report."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "bandit"
        script.write_text(FAKE_BANDIT.format(python=sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.log = tmp_path / "bandit-call.json"
        self.out = tmp_path / "bandit-out.json"
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("FAKE_BANDIT_LOG", str(self.log))
        monkeypatch.setenv("FAKE_BANDIT_OUT", str(self.out))
        self.report(results=[])

    def report(
        self, results: list[dict[str, Any]], errors: tuple[dict[str, Any], ...] = ()
    ) -> None:
        self.out.write_text(json.dumps({"errors": list(errors), "results": results}))

    def call(self) -> dict[str, Any]:
        return json.loads(self.log.read_text())


@pytest.fixture
def bandit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBandit:
    return FakeBandit(tmp_path, monkeypatch)


def _commit_python(repo: Repo, files: dict[str, str], message: str = "third") -> str:
    for name, text in files.items():
        path = repo.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    run_git(repo.root, "add", "-A")
    run_git(repo.root, "commit", "-q", "-m", message)
    return repo.git("rev-parse", "HEAD")


class TestScan:
    def test_maps_findings_to_repository_paths_sorted(self, repo: Repo, bandit: FakeBandit) -> None:
        head = _commit_python(repo, {"src/z.py": "B = 2\n", "src/a.py": "A = 1\n"})
        bandit.report([_result("./f0003.py", 1, "B311"), _result("./f0001.py", 9)])
        out = sast.scan(repo.root, repo.first, head)
        lines = out.splitlines()
        assert lines[0] == "bandit: 3 files scanned, 2 findings"
        assert lines[1] == (
            "src/a.py:9: B105 hardcoded_password_string: Possible hardcoded password: 'x' "
            "(severity low, confidence medium)"
        )
        assert lines[2].startswith("src/z.py:1: B311 ")
        assert "not scanned: leak.txt (not Python; bandit reads Python only)" in lines

    def test_scans_head_blobs_under_neutral_names_with_the_pinned_ruleset(
        self, repo: Repo, bandit: FakeBandit
    ) -> None:
        (repo.root / "src" / "app.py").write_text("WORKING_TREE = 'not committed'\n")
        sast.scan(repo.root, repo.first, repo.second)
        call = bandit.call()
        assert call["files"] == {"f0001.py": "def greet():\n    return 'hello, world'\n"}
        assert not Path(call["cwd"]).is_relative_to(repo.root)
        argv = call["argv"]
        assert argv[argv.index("--format") + 1] == "json"
        assert "--ignore-nosec" in argv and "--exit-zero" in argv
        assert argv[argv.index("--tests") + 1] == sast.RULESET
        assert argv[-2:] == ["--", "f0001.py"]
        assert not Path(call["cwd"]).exists()

    def test_ruleset_excludes_only_assert(self) -> None:
        rules = sast.RULESET.split(",")
        assert "B101" not in rules
        assert {"B105", "B311", "B602", "B608"} <= set(rules)
        assert len(rules) == len(set(rules))

    def test_reports_no_findings_and_skips_bandit_without_python(
        self, repo: Repo, bandit: FakeBandit
    ) -> None:
        head = _commit_python(repo, {"docs/notes.md": "x\n"})
        out = sast.scan(repo.root, repo.second, head)
        assert out == (
            "bandit: 0 files scanned, 0 findings\n"
            "not scanned: docs/notes.md (not Python; bandit reads Python only)\n"
        )
        assert not bandit.log.exists()

    def test_clean_scan_has_only_the_header(self, repo: Repo, bandit: FakeBandit) -> None:
        out = sast.scan(repo.root, repo.first, repo.second, path="src")
        assert out == "bandit: 1 file scanned, 0 findings\n"

    def test_deleted_files_are_not_listed(self, repo: Repo, bandit: FakeBandit) -> None:
        run_git(repo.root, "rm", "-q", "src/app.py")
        run_git(repo.root, "commit", "-q", "-m", "drop")
        out = sast.scan(repo.root, repo.second, "HEAD")
        assert out == "bandit: 0 files scanned, 0 findings\n"

    def test_symlinked_python_and_large_files_are_not_scanned(
        self, repo: Repo, bandit: FakeBandit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (repo.root / "out.py").symlink_to(repo.outside / "secret.txt")
        monkeypatch.setattr(sast, "MAX_FILE_BYTES", 10)
        head = _commit_python(repo, {"big.py": "X = 1  # well over ten bytes\n"})
        out = sast.scan(repo.root, repo.second, head)
        assert "not scanned: out.py (not a regular file)" in out
        assert "not scanned: big.py (larger than 10 bytes)" in out
        assert out.startswith("bandit: 0 files scanned")

    def test_files_bandit_cannot_parse_are_listed(self, repo: Repo, bandit: FakeBandit) -> None:
        bandit.report([], errors=({"filename": "./f0001.py", "reason": "syntax error"},))
        out = sast.scan(repo.root, repo.first, repo.second)
        assert "not scanned: src/app.py (syntax error)" in out

    def test_truncates_with_a_marker(self, repo: Repo, bandit: FakeBandit) -> None:
        bandit.report([_result("./f0001.py", n) for n in range(1, 50)])
        out = sast.scan(repo.root, repo.first, repo.second, max_bytes=200)
        assert out.splitlines()[-1].startswith("[truncated: ")
        assert "limit 200 bytes]" in out

    def test_unknown_ref_is_not_found(self, repo: Repo, bandit: FakeBandit) -> None:
        with pytest.raises(ToolNotFoundError, match="no-such-ref"):
            sast.scan(repo.root, "no-such-ref", repo.second)

    def test_path_outside_the_root_is_refused(self, repo: Repo, bandit: FakeBandit) -> None:
        with pytest.raises(PathEscapeError):
            sast.scan(repo.root, repo.first, repo.second, path="../outside")

    def test_scanner_failure_is_an_execution_error(
        self, repo: Repo, bandit: FakeBandit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_BANDIT_RC", "2")
        monkeypatch.setenv("FAKE_BANDIT_ERR", "bandit: error: unknown test B999\n")
        with pytest.raises(ToolExecutionError, match="exited 2") as err:
            sast.scan(repo.root, repo.first, repo.second)
        assert err.value.hint == "bandit: error: unknown test B999"

    @pytest.mark.parametrize("printed", ["not json", "[]"])
    def test_a_report_that_is_not_a_json_object_is_an_execution_error(
        self, repo: Repo, bandit: FakeBandit, printed: str
    ) -> None:
        bandit.out.write_text(printed)
        with pytest.raises(ToolExecutionError, match="bandit printed"):
            sast.scan(repo.root, repo.first, repo.second)

    def test_missing_scanner_is_unavailable(
        self, repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "only-git"
        bin_dir.mkdir()
        git = shutil.which("git")
        assert git is not None
        (bin_dir / "git").symlink_to(git)
        monkeypatch.setenv("PATH", str(bin_dir))
        with pytest.raises(ToolUnavailableError, match="bandit is not installed") as err:
            sast.scan(repo.root, repo.first, repo.second)
        assert err.value.hint is not None and "uv sync" in err.value.hint


class TestRegistry:
    def test_scan_is_a_registered_read_only_tool(self) -> None:
        assert "sast.scan" in TOOLS
        assert "sast.scan" in READ_ONLY
        assert "sast.scan" not in SANDBOX_ONLY
        assert "sast.scan" not in SANDBOX_DENIED
        spec = next(spec for spec in sast.TOOLS if spec.name == "sast.scan")
        assert spec.input_schema["required"] == ["base_ref", "head_ref"]
        assert not (spec.writes or spec.reaches_out or spec.needs_web_domains)


@pytest.mark.skipif(shutil.which("bandit") is None, reason="bandit is not on PATH")
class TestRealBandit:
    def test_flags_a_hardcoded_password_on_its_head_line(self, repo: Repo) -> None:
        head = _commit_python(
            repo,
            {"conf/settings.py": '"""Settings."""\n\nURL = "https://x.invalid"\npassword = "s3cr3t-value"\n'},
        )  # fmt: skip
        out = sast.scan(repo.root, repo.second, head)
        assert out.splitlines()[0] == "bandit: 1 file scanned, 1 finding"
        assert out.splitlines()[1].startswith("conf/settings.py:4: B105 hardcoded_password_string:")

    def test_ignores_nosec_and_assert(self, repo: Repo) -> None:
        head = _commit_python(repo, {"m.py": 'password = "abc"  # nosec\nassert password\n'})
        out = sast.scan(repo.root, repo.second, head)
        assert out.splitlines() == [
            "bandit: 1 file scanned, 1 finding",
            (
                "m.py:1: B105 hardcoded_password_string: Possible hardcoded password: 'abc' "
                "(severity low, confidence medium)"
            ),
        ]
