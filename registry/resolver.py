"""Find, load and validate agent manifests, and resolve `name@^MAJOR.MINOR` to one of them.

This is the one place that reads `agent.yaml`. Manifests live at `agents/<name>/agent.yaml` and at
`agents/<name>/<version>/agent.yaml`; both layouts may be mixed for one agent. The rules are in
`docs/CONVENTIONS.md` under "Agent address" and "Repository layout".
"""

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ValidationError

from factory.exit import AuthOrConfigError, NotFoundError, UsageError
from registry.spec import KINDS, NAME_PATTERN, VERSION_PATTERN, AgentSpec, JevSpec, LLMSpec

AGENTS_DIR = Path("agents")
MANIFEST = "agent.yaml"

_CARET = re.compile(r"\^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class ManifestError(AuthOrConfigError):
    """A manifest, or a file it points to, is invalid (exit 4). The message names file and field."""


class InvalidRange(UsageError):
    """A version range is not of the form `^MAJOR.MINOR` (exit 2)."""


class AgentNotFound(NotFoundError):
    """No agent with the requested name exists (exit 3)."""


class VersionNotFound(AgentNotFound):
    """The agent exists but no version satisfies the range (exit 3).

    `available` lists the versions found, lowest first, and is repeated in the hint.
    """

    def __init__(self, message: str, available: tuple[str, ...]) -> None:
        super().__init__(message, hint=f"available: {', '.join(available)}")
        self.available = available


class AgentRef(NamedTuple):
    """One agent version found on disk, as `list_agents()` reports it."""

    name: str
    version: str
    kind: str


class _Candidate(NamedTuple):
    ref: AgentRef
    version: Version
    path: Path


def caret_to_specifier(range: str) -> SpecifierSet:
    """Convert `^MAJOR.MINOR` to a `packaging` specifier.

    `^1.2` is `>=1.2.0,<2.0.0`. Before 1.0 a minor bump may break callers, so `^0.3` is
    `>=0.3.0,<0.4.0`. Raises `InvalidRange` for any other form.
    """
    match = _CARET.fullmatch(range)
    if match is None:
        raise InvalidRange(
            f"invalid version range {range!r}", hint="use `^MAJOR.MINOR`, for example `^1.2`"
        )
    major, minor = int(match[1]), int(match[2])
    upper = f"0.{minor + 1}.0" if major == 0 else f"{major + 1}.0.0"
    return SpecifierSet(f">={major}.{minor}.0,<{upper}", prereleases=False)


def list_agents(agents_dir: Path | None = None) -> list[AgentRef]:
    """Return every (name, version, kind) found under `agents_dir`, by name then version.

    Defaults to `agents/` in the working directory, which may be missing (an empty list). Raises
    `ManifestError` when any manifest found is malformed or sits in the wrong place.
    """
    root = AGENTS_DIR if agents_dir is None else agents_dir
    if not root.is_dir():
        return []
    found: list[_Candidate] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            found.extend(_candidates(root, entry.name))
    return [c.ref for c in sorted(found, key=lambda c: (c.ref.name, c.version))]


def resolve(name: str, range: str, agents_dir: Path | None = None) -> AgentSpec:
    """Return the fully loaded spec of the highest version of `name` matching the caret `range`.

    Raises `InvalidRange` for a bad range, `UsageError` for a bad name, `AgentNotFound` when the
    agent does not exist, `VersionNotFound` (listing the versions found) when none matches, and
    `ManifestError` for anything wrong in a manifest or in the schemas module an LLM agent names.
    """
    specifier = caret_to_specifier(range)
    if re.fullmatch(NAME_PATTERN, name) is None:
        raise UsageError(f"invalid agent name {name!r}", hint="use lowercase words joined by `-`")
    root = AGENTS_DIR if agents_dir is None else agents_dir
    found = _candidates(root, name)
    if not found:
        raise AgentNotFound(f"no agent named {name!r}", hint="run `factory list`")
    matching = [c for c in found if specifier.contains(c.version)]
    if not matching:
        available = tuple(c.ref.version for c in sorted(found, key=lambda c: c.version))
        raise VersionNotFound(f"no version of {name} matches {range}", available)
    return load_spec(max(matching, key=lambda c: c.version).path)


def load_spec(path: Path) -> AgentSpec:
    """Load and fully validate one manifest, and for an LLM agent import its models.

    Raises `ManifestError` naming the file and the field for malformed YAML, an unknown kind or
    key, a bad value, a missing prompt or schemas file, or a missing or non-Pydantic model class.
    """
    data = _read(path)
    kind = _kind(data, path)
    if "path" in data:
        raise ManifestError(f"{path}: path: not a manifest key")
    try:
        if kind == "llm":
            spec = LLMSpec.model_validate({**data, "path": path})
        else:
            return JevSpec.model_validate({**data, "path": path})
    except ValidationError as exc:
        raise ManifestError(_describe(path, exc)) from exc
    if not spec.system_prompt_file.is_file():
        raise ManifestError(f"{path}: system_prompt: no such file {spec.system_prompt_file}")
    module = _import_schemas(spec)
    spec.attach_models(
        _model_class(module, spec, "input_model"), _model_class(module, spec, "output_model")
    )
    return spec


def _read(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{path}: cannot read manifest: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: malformed YAML: {_one_line(exc)}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a mapping of fields")
    return data


def _kind(data: dict[str, Any], path: Path) -> str:
    kind = data.get("kind")
    if kind not in KINDS:
        raise ManifestError(f"{path}: kind: unknown kind {kind!r}, expected one of {KINDS}")
    return kind


def _candidates(root: Path, name: str) -> list[_Candidate]:
    """Peek at every manifest of agent `name` in both layouts and check where it sits."""
    agent_dir = root / name
    locations: list[tuple[Path, str | None]] = []
    if (agent_dir / MANIFEST).is_file():
        locations.append((agent_dir / MANIFEST, None))
    if agent_dir.is_dir():
        locations += [
            (sub / MANIFEST, sub.name)
            for sub in sorted(agent_dir.iterdir())
            if (sub / MANIFEST).is_file()
        ]
    found: dict[Version, _Candidate] = {}
    for path, version_dir in locations:
        candidate = _peek(path, name, version_dir)
        if candidate.version in found:
            raise ManifestError(
                f"{path}: version: {candidate.ref.version} is also at {found[candidate.version].path}"
            )
        found[candidate.version] = candidate
    return list(found.values())


def _peek(path: Path, directory_name: str, version_dir: str | None) -> _Candidate:
    """Read name, version and kind from a manifest and check them against its location."""
    data = _read(path)
    name, version = data.get("name"), data.get("version")
    if name != directory_name:
        raise ManifestError(f"{path}: name: {name!r} does not match directory {directory_name!r}")
    if not isinstance(version, str) or re.fullmatch(VERSION_PATTERN, version) is None:
        raise ManifestError(f"{path}: version: {version!r} is not MAJOR.MINOR.PATCH")
    if version_dir is not None and version_dir != version:
        raise ManifestError(
            f"{path}: version: {version!r} does not match directory {version_dir!r}"
        )
    return _Candidate(AgentRef(directory_name, version, _kind(data, path)), Version(version), path)


def _import_schemas(spec: LLMSpec) -> Any:
    """Import the agent's schemas module by file path, under a private module name."""
    file = spec.schemas_file
    if not file.is_file():
        raise ManifestError(f"{spec.path}: schemas: no such file {file}")
    module_name = f"_factory_schemas_{spec.name.replace('-', '_')}_{spec.version.replace('.', '_')}"
    import_spec = importlib.util.spec_from_file_location(module_name, file)
    if import_spec is None or import_spec.loader is None:
        raise ManifestError(f"{spec.path}: schemas: cannot import {file}")
    module = importlib.util.module_from_spec(import_spec)
    sys.modules[module_name] = module
    try:
        import_spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise ManifestError(
            f"{spec.path}: schemas: importing {file} failed: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def _model_class(module: Any, spec: LLMSpec, field: str) -> type[BaseModel]:
    """Return the class that `spec.<field>` names in `module`, which must be a Pydantic model."""
    class_name = getattr(spec, field)
    found = getattr(module, class_name, None)
    if found is None:
        raise ManifestError(
            f"{spec.path}: {field}: {class_name!r} not found in {spec.schemas_file}"
        )
    if not (isinstance(found, type) and issubclass(found, BaseModel)):
        raise ManifestError(
            f"{spec.path}: {field}: {class_name!r} in {spec.schemas_file} is not a Pydantic model"
        )
    return found


def _describe(path: Path, exc: ValidationError) -> str:
    """Render a Pydantic error as one line that names the file and each failing field."""
    problems = [
        f"{'.'.join(str(part) for part in err['loc']) or '(manifest)'}: {err['msg']}"
        for err in exc.errors()
    ]
    return f"{path}: {'; '.join(problems)}"


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())
