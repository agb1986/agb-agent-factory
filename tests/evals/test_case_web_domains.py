"""A `citations_resolve` case's `allow_domains` replace the agent's web allow-list for its run."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from evals.cases import Case, CitationsCheck, Criterion, Expected, Rubric
from evals.graders.llm_judge import JudgeVerdict
from evals.run import evaluate
from registry.common_schemas import Claim
from registry.resolver import resolve
from registry.spec import LLMSpec, WebSpec
from runtime.trace import Trace

PAGE_URL = "https://docs.python.org/3/page"
PAGE = "The quick brown fox."


class ResearchOutput(BaseModel):
    claims: list[Claim]


ANSWER = {
    "claims": [
        {
            "text": "A fox.",
            "fact": True,
            "citations": [
                {
                    "url": PAGE_URL,
                    "quote": "The quick brown fox",
                    "retrieved_at": "2026-11-07T10:00:00Z",
                }
            ],
        }
    ]
}


@pytest.fixture(scope="module")
def plain() -> LLMSpec:
    """A real agent whose manifest has no web tools."""
    found = resolve("code-reviewer", "^1.2")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def web(plain: LLMSpec) -> LLMSpec:
    """The same agent as if its manifest allowed `web.fetch` on two hosts."""
    tools = plain.tools.model_copy(
        update={
            "allow": (*plain.tools.allow, "web.fetch"),
            "web": WebSpec(allow_domains=("example.com", "peps.python.org")),
        }
    )
    return plain.model_copy(update={"tools": tools})


def test_with_web_domains_replaces_the_list_and_keeps_the_models(web: LLMSpec) -> None:
    narrowed = web.with_web_domains(["docs.python.org"])
    assert narrowed.tools.web.allow_domains == ("docs.python.org",)
    assert narrowed.tools.allow == web.tools.allow
    assert narrowed.input_class is web.input_class
    assert narrowed.output_class is web.output_class
    assert web.tools.web.allow_domains == ("example.com", "peps.python.org")


def test_with_web_domains_leaves_an_agent_without_web_tools_alone(plain: LLMSpec) -> None:
    assert plain.with_web_domains(["docs.python.org"]) is plain


@pytest.mark.parametrize("host", ["https://docs.python.org", "Docs.python.org", "localhost"])
def test_with_web_domains_checks_each_host(web: LLMSpec, host: str) -> None:
    with pytest.raises(ValidationError, match="bare lowercase hosts"):
        web.with_web_domains([host])


@pytest.mark.parametrize("host", ["https://docs.python.org", "docs.python.org/3", "python"])
def test_a_case_host_must_be_bare_and_lowercase(host: str) -> None:
    with pytest.raises(ValidationError, match="bare lowercase hosts"):
        CitationsCheck(allow_domains=(host,))


class _Judge:
    def __call__(self, expected: Expected, actual: Any) -> JudgeVerdict:
        return JudgeVerdict(passed=True, score=1.0, reason="fine")


class _Fetcher:
    def __call__(self, url: str) -> str:
        return PAGE


def _case(case_id: str, check: CitationsCheck | None) -> Case:
    rubric = Rubric(criteria=(Criterion(id="ok", text="fine"),), threshold=0.5)
    return Case(
        id=case_id,
        fixture="off-by-one",
        expected=Expected(verdict=None, rubric=rubric, citations_resolve=check),
        grader="rubric",
    )


def _domains_seen(spec: LLMSpec, cases: list[Case], tmp_path: Path) -> dict[str, tuple[str, ...]]:
    """Evaluate `cases` and return, per case, the web allow-list its run was given."""
    seen: dict[str, tuple[str, ...]] = {}

    def runner(run_spec: LLMSpec, payload: Any, trace: Trace, **kwargs: Any) -> BaseModel:
        seen[payload["case"]] = run_spec.tools.web.allow_domains
        assert run_spec.input_class is spec.input_class
        return ResearchOutput.model_validate(ANSWER)

    repo = tmp_path / "repos" / "off-by-one"
    repo.mkdir(parents=True)
    for case in cases:
        case.payload["case"] = case.id
    report = evaluate(
        spec,
        cases,
        Trace(tmp_path / "traces.duckdb"),
        runner=runner,
        rubric_judge=_Judge(),
        fetcher=_Fetcher(),
        fixture_paths={"off-by-one": repo},
        eval_run_id="eval-run-1",
        workers=1,
    )
    assert [r.error for r in report.results] == [None] * len(cases)
    return seen


def test_a_citations_case_runs_with_its_own_domains_and_others_with_the_manifest(
    web: LLMSpec, tmp_path: Path
) -> None:
    cases = [
        _case("cited", CitationsCheck(allow_domains=("docs.python.org",))),
        _case("uncited", None),
    ]
    seen = _domains_seen(web, cases, tmp_path)
    assert seen == {
        "cited": ("docs.python.org",),
        "uncited": ("example.com", "peps.python.org"),
    }


def test_a_citations_case_does_not_give_web_tools_to_an_agent_without_them(
    plain: LLMSpec, tmp_path: Path
) -> None:
    cases = [_case("cited", CitationsCheck(allow_domains=("docs.python.org",)))]
    assert _domains_seen(plain, cases, tmp_path) == {"cited": ()}
