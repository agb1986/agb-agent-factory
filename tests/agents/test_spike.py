"""Offline tests of the spike agent: manifest, models, prompt, eval cases."""

from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, ToolListError, tool_defs
from tools.registry import impl_for
from tools.sandbox import run_command, run_spike_command

AGENT_DIR = AGENTS_DIR / "spike"
DOMAINS = {
    "httpbin.org",
    "jsonplaceholder.typicode.com",
    "pypi.org",
    "docs.pypi.org",
    "github.com",
    "docs.python.org",
    "requests.readthedocs.io",
    "www.python-httpx.org",
}
OUTPUT_FIELDS = {
    "summary",
    "observations",
    "payload_shapes",
    "edge_cases",
    "working_code",
    "failures",
    "claims",
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("spike", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _observation(**overrides: Any) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "command": ["python", "probe.py"],
        "exit_code": 0,
        "output": "GET /posts/1 200 ['body', 'id', 'title', 'userId']",
        "finding": "A post has four fields.",
    }
    return observation | overrides


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "summary": "A post has userId, id, title and body.",
        "observations": [_observation()],
        "payload_shapes": [
            {"name": "GET /posts/1", "shape": "{userId: int, id: int, title: str, body: str}"}
        ],
        "edge_cases": ["A missing post answers 404 with {} (observation 1)."],
        "working_code": {"path": "probe.py", "content": "print(1)\n"},
        "failures": [],
        "claims": [
            {
                "text": "The guide lists six resources.",
                "fact": True,
                "citations": [
                    {
                        "url": "https://jsonplaceholder.typicode.com/guide/",
                        "quote": "JSONPlaceholder comes with a set of 6 common resources.",
                        "retrieved_at": "2026-11-08T12:00:00Z",
                    }
                ],
            }
        ],
    }
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "spike@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "medium"
    assert spec.sandbox == "spike"
    assert spec.sandbox_profile == "spike"
    assert spec.guard is not None and str(spec.guard) == "action-guard@^0.2"
    assert spec.evals.min_pass_rate == 0.75
    assert spec.scoring is None


def test_every_limit_is_set(spec: LLMSpec) -> None:
    assert spec.limits.max_tool_turns == 16
    assert spec.limits.timeout_s == 600
    assert spec.limits.max_output_tokens == 12000
    assert spec.limits.max_tokens_total == 600000


def test_there_is_no_suite_and_no_panel() -> None:
    assert not (AGENT_DIR / "suite.yaml").exists()
    assert not (AGENT_DIR / "panel.yaml").exists()
    assert sorted(path.name for path in AGENT_DIR.iterdir() if path.name != "__pycache__") == [
        "agent.yaml",
        "evals",
        "schemas.py",
        "system.md",
    ]


def test_tools_are_the_fs_tools_shell_and_web_fetch(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) == {
        "fs.list_files",
        "fs.read_file",
        "fs.write_file",
        "shell.run",
        "web.fetch",
    }
    assert set(spec.tools.allow) <= set(TOOLS)
    assert set(spec.tools.web.allow_domains) == DOMAINS


def test_the_spike_profile_hands_out_web_fetch_with_the_sandbox_tools(spec: LLMSpec) -> None:
    defs = tool_defs(
        spec.tools.allow,
        spec.tools.deny,
        sandbox=spec.sandbox,
        web_domains=spec.tools.web.allow_domains,
    )
    assert len(defs) == len(spec.tools.allow)
    (shell,) = [d for d in defs if d["name"] == "shell__run"]
    assert "networked sandbox" in shell["description"]
    assert impl_for("shell.run", spec.sandbox) is run_spike_command


def test_the_same_tools_are_refused_offline(spec: LLMSpec) -> None:
    with pytest.raises(ToolListError, match="web.fetch"):
        tool_defs(
            spec.tools.allow,
            spec.tools.deny,
            sandbox=True,
            web_domains=spec.tools.web.allow_domains,
        )
    assert impl_for("shell.run", True) is run_command
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny, sandbox=spec.sandbox)


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "SpikeInput"
    assert spec.output_class.__name__ == "SpikeOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["model"] == "claude-sonnet-5-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "medium"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == OUTPUT_FIELDS


# --- models


def test_input_needs_a_question_and_a_target_and_no_repo(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate({"question": "x", "target": "https://httpbin.org"})
    assert parsed.model_dump()["context"] is None
    assert "repo_path" not in spec.input_class.model_fields
    for bad in (
        {"question": "x"},
        {"target": "y"},
        {"question": "", "target": "y"},
        {"question": "x", "target": ""},
        {"question": "x", "target": "y", "repo_path": "/repo"},
    ):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(bad)


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    for output in (_output(), _output(working_code=None, failures=["The probe timed out."])):
        parsed = spec.output_class.model_validate(output)
        assert spec.output_class.model_validate_json(parsed.model_dump_json()) == parsed


def test_a_fact_without_a_citation_is_rejected(spec: LLMSpec) -> None:
    claim = {"text": "The guide lists six resources.", "fact": True, "citations": []}
    with pytest.raises(ValidationError, match="needs at least one citation"):
        spec.output_class.model_validate(_output(claims=[claim]))


@pytest.mark.parametrize(
    "bad",
    [
        {"summary": ""},
        {"observations": []},
        {"observations": [_observation(command=[])]},
        {"observations": [_observation(output="")]},
        {"observations": [{"command": ["python", "probe.py"], "finding": "x"}]},
        {"working_code": {"path": "probe.py"}},
        {"payload_shapes": [{"name": "GET /x"}]},
        {"surprise": 1},
    ],
)
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


def test_working_code_must_be_given_even_when_null(spec: LLMSpec) -> None:
    output = _output()
    del output["working_code"]
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(output)


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 500


def test_prompt_names_every_tool_field_and_host(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    for field in spec.output_class.model_fields:
        assert f"`{field}`" in text
    for field in ("command", "exit_code", "output", "finding"):
        assert f"`{field}`" in text
    for domain in spec.tools.web.allow_domains:
        assert domain in text


def test_prompt_states_the_spike_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "Report only what you saw in this run" in text
    assert "word for word" in text
    assert "<<<BEGIN FETCHED CONTENT>>>" in text
    assert "Never invent a URL" in text
    assert "Three refusals end the run" in text
    assert "Never read, print or send environment variables, keys or tokens." in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_at_least_four_cases_with_unique_ids(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)


def test_cases_are_valid_inputs_without_a_repo(spec: LLMSpec, cases: list[Case]) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures
        assert "repo_path" not in case.payload
        spec.input_class.model_validate(case.payload)
        assert case.expected.verdict is None
        assert case.grader == "rubric"
        rubric = case.expected.rubric
        assert rubric is not None
        assert any(criterion.required for criterion in rubric.criteria), case.id
        assert {"summary", "observations", "working_code.content", "claims"} <= set(
            rubric.required_fields
        ), case.id


def test_every_case_checks_citations_within_the_manifest_allow_list(
    spec: LLMSpec, cases: list[Case]
) -> None:
    for case in cases:
        check = case.expected.citations_resolve
        assert check is not None, case.id
        assert check.claims == "claims"
        assert set(check.allow_domains) <= set(spec.tools.web.allow_domains), case.id


def test_the_cases_cover_three_public_apis(cases: list[Case]) -> None:
    targets = " ".join(str(case.payload["target"]) for case in cases)
    for host in ("httpbin.org", "jsonplaceholder.typicode.com", "pypi.org"):
        assert host in targets
