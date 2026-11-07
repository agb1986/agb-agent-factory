"""`factory run`: run one LLM agent once on a JSON payload and print its output.

The agent is `name@^MAJOR.MINOR`, or the highest version of `name` when the range is left out;
only published versions count unless `allow_unpublished` is set, as in the pipelines. The payload
is a JSON object read from a file or from stdin (`-`). When the agent's input model has a
`repo_path` field it is set to `--repo`, as the eval runner does, so a payload need not repeat it.

`allow_domains`, from `--allow-domain`, replace the manifest's `tools.web.allow_domains` for this
run; they are not merged with it (`registry.spec.LLMSpec.with_web_domains`). The manifest must
still allow the web tools with a non-empty list, so an agent without web tools cannot be given
any here: passing `--allow-domain` to one is a usage error.

The run is traced with the tag `run:<name@version>`. Its run id is also its pipeline run id, so
`factory trace <run_id>` exports it and the reported cost includes the action guard's runs, if the
agent has a guard.
"""

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, TextIO

from packaging.version import Version

from factory.exit import AuthOrConfigError, ExitCode, UsageError
from registry.resolver import AgentNotFound, NotPublished, list_agents, resolve
from registry.spec import DOMAIN_PATTERN, LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.runner import API_KEY_ENV, run
from runtime.trace import Trace

TAG_PREFIX = "run:"
"""Prefix of the trace tag of a `factory run` run; the agent's address follows it."""


def resolve_agent(ref: str, *, allow_unpublished: bool) -> LLMSpec:
    """Resolve `name[@range]` to an LLM agent; a Jev agent is a `UsageError`.

    Without a range the highest version is taken, among published versions unless
    `allow_unpublished`. Raises `AgentNotFound` (exit 3) when there is no such agent and
    `NotPublished` (exit 3) when none of its versions is published.
    """
    name, at, version_range = ref.partition("@")
    if not name or (at and not version_range):
        raise UsageError(f"invalid agent reference {ref!r}", hint="use `name@^MAJOR.MINOR`")
    published_only = not allow_unpublished
    if not at:
        versions = [Version(found.version) for found in list_agents() if found.name == name]
        if not versions:
            raise AgentNotFound(f"no agent named {name!r}", hint="run `factory list`")
        if published_only:
            published = {
                Version(found.version)
                for found in list_agents(published_only=True)
                if found.name == name
            }
            if not published:
                raise NotPublished(f"no version of {name} is published", ())
            versions = list(published)
        highest = max(versions)
        version_range = f"^{highest.major}.{highest.minor}"
    spec = resolve(name, version_range, published_only=published_only)
    if not isinstance(spec, LLMSpec):
        raise UsageError(
            f"{spec.address} is not an LLM agent, it is kind {spec.kind}",
            hint="`factory run` runs `kind: llm` agents only",
        )
    return spec


def check_domains(spec: LLMSpec, domains: list[str]) -> LLMSpec:
    """Return `spec` with `domains` as its web allow-list, or unchanged when there are none.

    Raises `UsageError` for an entry that is not a bare lowercase host, and when the manifest
    lists no domain, since the override only narrows or redirects web tools the agent has.
    """
    if not domains:
        return spec
    for domain in domains:
        if not re.fullmatch(DOMAIN_PATTERN, domain):
            raise UsageError(
                f"--allow-domain {domain!r} is not a bare lowercase host",
                hint="use a host such as docs.python.org, without a scheme, path or port",
            )
    if not spec.tools.web.allow_domains:
        raise UsageError(
            f"{spec.address} has no web tools, so --allow-domain does not apply",
            hint="the manifest must list tools.web.allow_domains for the override to apply",
        )
    return spec.with_web_domains(domains)


def read_payload(source: str, stdin: TextIO | None = None) -> dict[str, Any]:
    """Read the payload: a JSON object from the file `source`, or from stdin when it is `-`.

    A missing file, text that is not JSON, and JSON that is not an object are `UsageError`s.
    """
    if source == "-":
        text = (sys.stdin if stdin is None else stdin).read()
        where = "stdin"
    else:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --input {source}: {exc.strerror}") from None
        where = source
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"the payload in {where} is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise UsageError(f"the payload in {where} is not a JSON object")
    return payload


def command(
    ref: str,
    *,
    input: str,
    repo: str = ".",
    allow_domains: list[str] | None = None,
    as_json: bool,
    allow_unpublished: bool = False,
    budget_usd: float | None = None,
) -> int:
    """Run `factory run`: run the agent `ref` once on the payload in `input` and print the output.

    Under `as_json` stdout gets one object with `agent`, `version`, `run_id`, `cost_usd`,
    `duration_ms` and `output`. Without it the output is pretty-printed on stdout and a one-line
    cost summary goes to stderr. Returns 0; failures raise and the CLI maps them to exit codes.
    `budget_usd` caps the spend (default: `DEFAULT_BUDGET_USD`).
    """
    spec = check_domains(
        resolve_agent(ref, allow_unpublished=allow_unpublished), list(allow_domains or ())
    )
    payload = read_payload(input)
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        raise UsageError(f"--repo {repo} is not a directory")
    if "repo_path" in spec.input_class.model_fields:
        payload["repo_path"] = str(repo_path)
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
    run_id = str(uuid.uuid4())
    start = time.monotonic()
    with Trace() as trace:
        output = run(
            spec,
            payload,
            trace,
            repo_path=repo_path,
            pipeline_run_id=run_id,
            run_id=run_id,
            tag=f"{TAG_PREFIX}{spec.address}",
            budget=budget,
        )
        cost_usd = trace.cost_by_pipeline_run(run_id)
    duration_ms = round((time.monotonic() - start) * 1000)
    document = output.model_dump(mode="json")
    if as_json:
        print(
            json.dumps(
                {
                    "agent": spec.name,
                    "version": spec.version,
                    "run_id": run_id,
                    "cost_usd": cost_usd,
                    "duration_ms": duration_ms,
                    "output": document,
                }
            )
        )
    else:
        print(json.dumps(document, indent=2))
        print(f"{spec.address} run {run_id}: ${cost_usd:.4f} in {duration_ms} ms", file=sys.stderr)
    return ExitCode.OK
