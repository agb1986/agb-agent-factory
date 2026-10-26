"""`scripts/scan_image.py`: the key patterns, layers inside an archive, and the exit codes."""

import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scan_image.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_image", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_image"] = module
    spec.loader.exec_module(module)
    return module


scan = load()

# Built at run time so this file holds no key-like literal itself.
ANTHROPIC = "sk-ant-" + "api03-" + "A" * 24
TYPESAFE = "whk_" + "b" * 24


def tar_of(files: dict[str, bytes], *, gzip: bool = False) -> bytes:
    buffer = io.BytesIO()
    mode: Literal["w", "w:gz"] = "w:gz" if gzip else "w"
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def findings(files: dict[str, bytes], literals: tuple[bytes, ...] = ()) -> list[tuple[str, str]]:
    source = io.BytesIO(tar_of(files))
    return [tuple(found) for found in scan.scan_archive(source, literals)]


@pytest.mark.parametrize(
    ("text", "name"),
    [
        (ANTHROPIC, "anthropic key (sk-ant-)"),
        (TYPESAFE, "typesafe key (whk_)"),
        (f"ENV ANTHROPIC_API_KEY={ANTHROPIC}\n", "anthropic key (sk-ant-)"),
    ],
)
def test_a_planted_key_is_found(text: str, name: str) -> None:
    assert findings({"etc/env": text.encode()}) == [("etc/env", name)]


@pytest.mark.parametrize("text", ["sk-ant-", "keys start with sk-ant- or whk_", "sk-ant-x", "whk_"])
def test_a_bare_prefix_is_not_a_key(text: str) -> None:
    assert findings({"README": text.encode()}) == []


def test_a_clean_archive_has_no_findings() -> None:
    assert findings({"a": b"hello", "b/c": b"world" * 10_000}) == []


def test_a_key_inside_a_layer_is_found_with_the_path_of_its_file() -> None:
    layer = tar_of({"app/.env": ANTHROPIC.encode(), "app/ok": b"fine"})
    assert findings({"blobs/sha256/abc": layer}) == [
        ("blobs/sha256/abc:app/.env", "anthropic key (sk-ant-)")
    ]


def test_a_compressed_layer_is_opened_too() -> None:
    layer = tar_of({"x": TYPESAFE.encode()}, gzip=True)
    assert findings({"layer.tar.gz": layer}) == [("layer.tar.gz:x", "typesafe key (whk_)")]


def test_a_key_in_the_image_config_is_found() -> None:
    config = b'{"config": {"Env": ["ANTHROPIC_API_KEY=' + ANTHROPIC.encode() + b'"]}}'
    assert findings({"blobs/sha256/cfg": config}) == [
        ("blobs/sha256/cfg", "anthropic key (sk-ant-)")
    ]


def test_a_key_across_a_chunk_boundary_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "CHUNK", 64)
    data = b"." * 60 + ANTHROPIC.encode() + b"." * 60
    assert scan.scan_stream(io.BytesIO(data)) == ["anthropic key (sk-ant-)"]


def test_a_literal_is_found_and_never_printed() -> None:
    secret = b"not-a-known-shape-0123456789"
    assert findings({"f": b"x" + secret}, (secret,)) == [("f", "literal")]


def test_main_exits_1_on_a_finding_and_names_no_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scan, "scan_image", lambda image, literals: [scan.Finding("f", "p")])
    assert scan.main(["img"]) == 1
    captured = capsys.readouterr()
    assert "f: p" in captured.out
    assert ANTHROPIC not in captured.out + captured.err


def test_main_exits_0_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "scan_image", lambda image, literals: [])
    assert scan.main(["img"]) == 0


def test_main_exits_2_when_the_image_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(image: str, literals: list[bytes]) -> list[object]:
        raise RuntimeError("no such image")

    monkeypatch.setattr(scan, "scan_image", boom)
    assert scan.main(["img"]) == 2


def test_env_option_passes_the_value_of_a_set_variable_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[bytes]] = []
    monkeypatch.setattr(scan, "scan_image", lambda image, literals: seen.append(literals) or [])
    monkeypatch.setenv("SCAN_SET", "value-1")
    monkeypatch.delenv("SCAN_UNSET", raising=False)
    scan.main(["img", "--env", "SCAN_SET", "--env", "SCAN_UNSET"])
    assert seen == [[b"value-1"]]


def test_scan_image_reports_a_docker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="No such image: nope")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: failed)
    with pytest.raises(RuntimeError, match="No such image"):
        scan.scan_image("nope")


FAKE_PATH = "blobs/sha256/abc:app/fixtures/repos/hardcoded-secret/webhooks/sender.py"


def test_the_planted_fixture_token_is_known() -> None:
    assert scan.is_known_fake(scan.Finding(FAKE_PATH, "typesafe key (whk_)"))


@pytest.mark.parametrize(
    "finding",
    [
        scan.Finding(FAKE_PATH, "anthropic key (sk-ant-)"),
        scan.Finding(
            "blobs/sha256/abc:app/fixtures/repos/mixed/shop/pricing.py", "typesafe key (whk_)"
        ),
        scan.Finding("blobs/sha256/abc:app/.env", "typesafe key (whk_)"),
        scan.Finding(FAKE_PATH + ".bak", "typesafe key (whk_)"),
    ],
)
def test_nothing_else_is_known(finding: object) -> None:
    assert not scan.is_known_fake(finding)


def test_main_ignores_the_fixture_token_but_not_a_second_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = scan.Finding(FAKE_PATH, "typesafe key (whk_)")
    monkeypatch.setattr(scan, "scan_image", lambda image, literals: [fake])
    assert scan.main(["img"]) == 0
    assert "ignored" in capsys.readouterr().out
    other = scan.Finding("blobs/sha256/abc:app/.env", "typesafe key (whk_)")
    monkeypatch.setattr(scan, "scan_image", lambda image, literals: [fake, other])
    assert scan.main(["img"]) == 1
