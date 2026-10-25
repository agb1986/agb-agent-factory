"""Builds the fixture repository: two commits and symlinks that point out of it."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

SECRET = "TOP-SECRET-OUTSIDE-THE-REPO"


@dataclass(frozen=True)
class Repo:
    """The fixture repository: its root, the outside directory, and the two commit ids."""

    root: Path
    outside: Path
    first: str
    second: str

    def git(self, *args: str) -> str:
        """Run git in the repository and return stdout."""
        return run_git(self.root, *args)


def run_git(cwd: Path, *args: str) -> str:
    """Run `git` with a fixed identity and no signing, returning stdout."""
    proc = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return proc.stdout.strip()


def build_repo(tmp_path: Path) -> Repo:
    """Build the repository: a first commit, a second that modifies a file, and two bad symlinks."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET + "\n")
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    run_git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# demo\n")
    (root / "src" / "app.py").write_text("def greet():\n    return 'hello'\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "first")
    first = run_git(root, "rev-parse", "HEAD")
    (root / "src" / "app.py").write_text("def greet():\n    return 'hello, world'\n")
    (root / "leak.txt").symlink_to(outside / "secret.txt")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "second")
    return Repo(root, outside, first, run_git(root, "rev-parse", "HEAD"))
