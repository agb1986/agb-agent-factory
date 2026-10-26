"""Fail when a container image holds a key-like string.

Usage: `python scripts/scan_image.py IMAGE [--env NAME ...]`, with `docker` on the PATH. It runs
`docker save` and reads the archive: every layer, file by file, and the image configuration,
which holds the environment and the build history. Anything that matches `PATTERNS` is reported by
file and pattern name, never by value, and the exit code is 1. The job needs no secret and sends
nothing anywhere.

`--env NAME` also looks for the value of that environment variable of this process, for a local
check against the real key; it is skipped when the variable is unset or empty. CI does not use it.

Exit codes follow `factory`: 0 clean, 1 a key-like string found, 2 the image could not be read.
"""

import argparse
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, NamedTuple

PATTERNS: dict[str, re.Pattern[bytes]] = {
    "anthropic key (sk-ant-)": re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}"),
    "typesafe key (whk_)": re.compile(rb"whk_[A-Za-z0-9_-]{8,}"),
}
"""The key shapes to look for, by name. Each needs a tail, so a bare prefix in prose is not a hit."""

KNOWN_FAKES: tuple[re.Pattern[str], ...] = (
    re.compile(r":app/fixtures/(src|repos)/hardcoded-secret/(head/)?webhooks/sender\.py$"),
)
"""Files in an image that hold a fake key on purpose.

The `hardcoded-secret` fixture plants a made-up `whk_` token for the security reviewer to find. A
finding of the typesafe pattern in exactly these files is counted and reported but does not fail
the scan. Nothing else is exempt: a key anywhere else in the image, or any other pattern here,
still fails.
"""

CHUNK = 1 << 20
"""Bytes read at a time from a file inside a layer."""

OVERLAP = 256
"""Bytes kept from one chunk to the next, so a match cannot hide on a chunk boundary."""


class Finding(NamedTuple):
    """One match: the path inside the archive and the name of the pattern, never the value."""

    path: str
    pattern: str


def scan_bytes(data: bytes, literals: Sequence[bytes] = ()) -> list[str]:
    """Return the names of the patterns and literals found in `data`, each once.

    A literal is reported as `literal`, not by its value.
    """
    found = [name for name, pattern in PATTERNS.items() if pattern.search(data)]
    if any(literal in data for literal in literals):
        found.append("literal")
    return found


def scan_stream(stream: IO[bytes], literals: Sequence[bytes] = ()) -> list[str]:
    """Scan `stream` in chunks that overlap, and return the names found, each once."""
    keep = max([OVERLAP, *(len(literal) for literal in literals)])
    found: list[str] = []
    tail = b""
    while chunk := stream.read(CHUNK):
        data = tail + chunk
        for name in scan_bytes(data, literals):
            if name not in found:
                found.append(name)
        tail = data[-keep:]
    return found


def scan_archive(
    source: IO[bytes], literals: Sequence[bytes] = (), *, prefix: str = ""
) -> Iterator[Finding]:
    """Yield a `Finding` for every match in the seekable tar archive `source`, layers included.

    Each regular file is scanned as bytes. A file that is itself a tar archive, as a layer blob of
    `docker save` is, is opened and scanned file by file, so the path names the file inside the
    layer. A member that is not a tar archive is scanned as it is.
    """
    with tarfile.open(fileobj=source, mode="r:*") as archive:
        for member in archive:
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is None:
                continue
            path = f"{prefix}{member.name}"
            is_tar = tarfile.is_tarfile(handle)
            handle.seek(0)
            if is_tar:
                yield from scan_archive(handle, literals, prefix=f"{path}:")
            else:
                for name in scan_stream(handle, literals):
                    yield Finding(path, name)


def scan_image(image: str, literals: Sequence[bytes] = ()) -> list[Finding]:
    """Run `docker save` for `image` into a temporary file and return every `Finding` in it.

    Raises `RuntimeError` when docker cannot save the image or the archive cannot be read.
    """
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "image.tar"
        saved = subprocess.run(
            ["docker", "save", "--output", str(archive), image],
            check=False,
            capture_output=True,
            text=True,
        )
        if saved.returncode != 0:
            raise RuntimeError(f"docker save {image} failed: {saved.stderr.strip()}")
        try:
            with archive.open("rb") as source:
                return list(scan_archive(source, literals))
        except tarfile.TarError as exc:
            raise RuntimeError(f"cannot read the archive of {image}: {exc}") from exc


def is_known_fake(finding: Finding) -> bool:
    """Whether `finding` is the planted token of the `hardcoded-secret` fixture."""
    return finding.pattern == "typesafe key (whk_)" and any(
        fake.search(finding.path) for fake in KNOWN_FAKES
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Scan the image named on the command line; print each finding; return the exit code."""
    parser = argparse.ArgumentParser(description="fail when an image holds a key-like string")
    parser.add_argument("image", help="image name or id, as `docker save` takes it")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME",
        help="also look for the value of this environment variable (skipped when unset)",
    )
    args = parser.parse_args(argv)
    literals = [os.environ[name].encode() for name in args.env if os.environ.get(name)]
    try:
        findings = scan_image(args.image, literals)
    except (RuntimeError, OSError) as exc:
        print(f"scan_image: {exc}", file=sys.stderr)
        return 2
    fakes = [finding for finding in findings if is_known_fake(finding)]
    findings = [finding for finding in findings if not is_known_fake(finding)]
    if fakes:
        print(f"scan_image: {len(fakes)} planted fixture token(s) ignored (KNOWN_FAKES)")
    for finding in findings:
        print(f"{finding.path}: {finding.pattern}")
    if findings:
        print(f"scan_image: {len(findings)} key-like string(s) in {args.image}", file=sys.stderr)
        return 1
    print(f"scan_image: {args.image} is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
