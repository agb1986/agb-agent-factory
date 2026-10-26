"""The deployment files: the image is non-root with no key, compose publishes nothing by default."""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text().splitlines()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]

# Built at run time so no key-like literal sits in this file.
KEY_SHAPES = (
    re.compile("sk-ant-" + r"[A-Za-z0-9_-]{8,}"),
    re.compile("whk_" + r"[A-Za-z0-9_-]{8,}"),
)


def instructions(name: str) -> list[str]:
    """The Dockerfile lines that start with `name`, comments excluded."""
    return [
        line.strip()
        for line in DOCKERFILE.splitlines()
        if line.strip().upper().startswith(name) and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("text", [DOCKERFILE, "\n".join(DOCKERIGNORE), ENV_EXAMPLE])
def test_no_key_literal_in_the_deployment_files(text: str) -> None:
    assert not any(shape.search(text) for shape in KEY_SHAPES)


def test_the_image_runs_as_a_non_root_user_and_ends_there() -> None:
    users = instructions("USER")
    assert users == ["USER factory"]
    assert "--uid 10001" in DOCKERFILE


def test_the_entrypoint_is_factory_and_the_data_directory_is_a_volume() -> None:
    assert instructions("ENTRYPOINT") == ['ENTRYPOINT ["factory"]']
    assert instructions("VOLUME") == ["VOLUME /app/data"]
    assert "FACTORY_TRACE_DB=/app/data/traces.duckdb" in DOCKERFILE


def test_no_secret_reaches_the_image_by_a_build_instruction() -> None:
    for line in instructions("ARG") + instructions("ENV"):
        assert "API_KEY" not in line
    assert not [line for line in instructions("COPY") if ".env" in line]


def test_the_build_context_leaves_out_env_files_and_data() -> None:
    for entry in (".env", ".env.*", "secrets.env", "data", ".git"):
        assert entry in DOCKERIGNORE
    assert "!.env.example" in DOCKERIGNORE


def test_the_image_pins_the_sdk_log_level_to_warning() -> None:
    assert "TYPESAFE_LOG_LEVEL=warning" in DOCKERFILE


def test_the_env_example_names_both_keys_and_gives_no_value() -> None:
    values = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert values == {"ANTHROPIC_API_KEY": "", "TYPESAFE_API_KEY": ""}


def test_the_default_service_publishes_no_port_and_reads_the_env_file() -> None:
    factory = SERVICES["factory"]
    assert "ports" not in factory
    assert factory["env_file"] == ".env"
    assert factory.get("profiles") is None


def test_the_api_sits_behind_a_profile_and_binds_loopback_only() -> None:
    api = SERVICES["api"]
    assert api["profiles"] == ["api"]
    assert api["ports"] == ["127.0.0.1:8000:8000"]


@pytest.mark.parametrize("name", ["factory", "api"])
def test_each_service_is_limited_and_locked_down(name: str) -> None:
    service = SERVICES[name]
    assert service["cpus"] == 2
    assert service["mem_limit"] == "1g"
    assert service["pids_limit"] == 256
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["environment"]["TYPESAFE_LOG_LEVEL"] == "warning"
    assert "factory-data:/app/data" in service["volumes"]


def test_the_docker_socket_is_not_mounted_by_default() -> None:
    for service in SERVICES.values():
        assert not [v for v in service["volumes"] if "docker.sock" in v]
    assert SERVICES["factory"]["build"]["args"]["WITH_DOCKER_CLI"] == "0"


def test_repositories_are_mounted_read_only() -> None:
    for service in SERVICES.values():
        assert [v for v in service["volumes"] if v.endswith(":/repos:ro")]


def test_the_sdk_honors_the_log_level_variable() -> None:
    """The variable the image sets is the one `typesafe_sdk` reads, so it does lower the level."""
    code = "import logging, typesafe_sdk; print(logging.getLogger('typesafe_sdk').level)"
    environment = {**os.environ, "TYPESAFE_LOG_LEVEL": "warning"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    assert result.stdout.strip() == str(logging.WARNING)
