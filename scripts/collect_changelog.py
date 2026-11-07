"""Move the changelog fragments in `changelog.d/` into `CHANGELOG.md` under `## [Unreleased]`.

Usage: `python scripts/collect_changelog.py [--dry-run] [--changelog PATH] [--fragments DIR]`,
run from the repository root. A fragment is a file `changelog.d/<id>.<section>.md`, where `id` is
the issue number the change closes (or a short slug when there is none) and `section` is one of
added, changed, deprecated, removed, fixed or security. Its content is the bullet line, or lines,
exactly as they go into the changelog: each entry starts with `- ` and is written for a user, in
Keep a Changelog form. `changelog.d/README.md` is not a fragment and is left alone.

Fragments are read in id order, numbers first and in numeric order, then slugs in name order, and
each one's entries are appended to the end of its `### Section` under `## [Unreleased]`. A section
heading that does not exist yet is created in Keep a Changelog order relative to the headings that
do. The fragments are then deleted, so the release commit holds the changelog and the removals
together. With `--dry-run` nothing is written or deleted and the script prints what each section
would gain.

Exit codes follow `factory`: 0 done, including when there is nothing to collect, and 2 when the
script could not run (a fragment with an unknown section or an empty one, an entry that is not a
bullet, a changelog without an `## [Unreleased]` heading). The convention is in
`docs/CONVENTIONS.md` under "Changelog and releases" and the release steps in `docs/releasing.md`.
"""

import argparse
import re
import sys
from pathlib import Path

SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")
"""The Keep a Changelog sections, in the order a release lists them."""

UNRELEASED = "## [Unreleased]"
_FRAGMENT = re.compile(r"(?P<id>[^.]+)\.(?P<section>[a-z]+)\.md")
_README = "README.md"


class ScriptError(Exception):
    """The script could not run; the message says why (exit 2)."""


class Fragment:
    """One fragment: its path, the section it belongs to and its bullet lines."""

    def __init__(self, path: Path) -> None:
        match = _FRAGMENT.fullmatch(path.name)
        if match is None:
            raise ScriptError(f"{path}: name it <id>.<section>.md")
        self.path = path
        self.id = match["id"]
        self.section = match["section"]
        if self.section not in SECTIONS:
            raise ScriptError(f"{path}: section must be one of {', '.join(SECTIONS)}")
        self.lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
        while self.lines and not self.lines[-1]:
            self.lines.pop()
        while self.lines and not self.lines[0]:
            self.lines.pop(0)
        if not self.lines:
            raise ScriptError(f"{path}: the fragment is empty")
        if not self.lines[0].startswith("- "):
            raise ScriptError(f"{path}: an entry starts with '- ', got {self.lines[0]!r}")
        for line in self.lines:
            if line and not line.startswith(("- ", "  ")):
                raise ScriptError(f"{path}: a continuation line is indented, got {line!r}")

    def sort_key(self) -> tuple[int, int, str]:
        """Numbers first, in numeric order; then slugs, in name order."""
        if self.id.isdigit():
            return (0, int(self.id), self.id)
        return (1, 0, self.id)


def read_fragments(directory: Path) -> list[Fragment]:
    """Return the fragments in `directory` in collection order; an absent directory has none."""
    if not directory.is_dir():
        return []
    paths = [p for p in directory.iterdir() if p.is_file() and p.name != _README]
    return sorted((Fragment(p) for p in paths), key=Fragment.sort_key)


def _unreleased_span(lines: list[str]) -> tuple[int, int]:
    """Return the line index after the Unreleased heading and the index of the next `## `."""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == UNRELEASED) + 1
    except StopIteration:
        raise ScriptError(f"the changelog has no {UNRELEASED!r} heading") from None
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("## ") or _is_footer(lines[i])),
        len(lines),
    )
    return start, end


def _is_footer(line: str) -> bool:
    """The link block or the comment above it ends the last section of the file."""
    return line.startswith(("[", "<!--"))


def _section_end(lines: list[str], heading_index: int, end: int) -> int:
    """Return the index just after the last entry line of the section at `heading_index`."""
    last = heading_index
    for i in range(heading_index + 1, end):
        if lines[i].startswith("### "):
            break
        if lines[i].strip():
            last = i
    return last + 1


def merge(changelog: str, fragments: list[Fragment]) -> tuple[str, dict[str, list[str]]]:
    """Return the changelog with the fragments' entries added, and the entries by section."""
    lines = changelog.splitlines()
    gained: dict[str, list[str]] = {}
    for fragment in fragments:
        start, end = _unreleased_span(lines)
        heading = f"### {fragment.section.capitalize()}"
        headings = {
            lines[i].strip().removeprefix("### ").lower(): i
            for i in range(start, end)
            if lines[i].startswith("### ")
        }
        if fragment.section in headings:
            at = _section_end(lines, headings[fragment.section], end)
            lines[at:at] = fragment.lines
        else:
            later = [
                headings[s]
                for s in SECTIONS[SECTIONS.index(fragment.section) + 1 :]
                if s in headings
            ]
            if later:
                at = min(later)
                lines[at:at] = [heading, "", *fragment.lines, ""]
            else:
                at = _trim_blank(lines, end)
                lines[at:at] = ["", heading, "", *fragment.lines]
        gained.setdefault(fragment.section, []).extend(fragment.lines)
    text = "\n".join(lines)
    return (text + "\n" if changelog.endswith("\n") else text), gained


def _trim_blank(lines: list[str], end: int) -> int:
    """Return the index after the last non-blank line before `end`."""
    at = end
    while at > 0 and not lines[at - 1].strip():
        at -= 1
    return at


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the entries, change nothing")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--fragments", type=Path, default=Path("changelog.d"))
    args = parser.parse_args(argv)
    try:
        fragments = read_fragments(args.fragments)
        if not fragments:
            print(f"no fragments in {args.fragments}")
            return 0
        if not args.changelog.is_file():
            raise ScriptError(f"{args.changelog}: no such file")
        text, gained = merge(args.changelog.read_text(encoding="utf-8"), fragments)
    except ScriptError as err:
        print(f"collect_changelog: {err}", file=sys.stderr)
        return 2
    for section in SECTIONS:
        if section in gained:
            print(f"### {section.capitalize()}")
            print("\n".join(gained[section]))
    if args.dry_run:
        print(f"dry run: {len(fragments)} fragment(s) left in place")
        return 0
    args.changelog.write_text(text, encoding="utf-8")
    for fragment in fragments:
        fragment.path.unlink()
    print(f"collected {len(fragments)} fragment(s) into {args.changelog}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
