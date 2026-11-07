"""Tests for the `citations_resolve` check, with a fake fetcher and no network."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from evals.cases import Case, CitationsCheck, Criterion, Expected, Rubric
from evals.graders import grade
from evals.graders.citations import (
    CitationCheckError,
    check_citations,
    normalise,
    quote_in_page,
)
from evals.graders.llm_judge import JudgeVerdict
from evals.run import EvalReport, evaluate
from factory.exit import UsageError
from registry.common_schemas import Claim
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.trace import Trace
from tools import ToolExecutionError, ToolUnavailableError, web

CHECK = CitationsCheck(allow_domains=("example.com",))
PAGE_URL = "https://example.com/page"
PAGE = "# Title\n\nThe **quick**  brown fox\njumps over the [lazy dog](https://x.test/d).\n"


class FakeFetcher:
    """Serves pages from a dict; a value that is an exception is raised."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


def _claim(quote: str, url: str = PAGE_URL, fact: bool = True) -> dict[str, Any]:
    cite = {"url": url, "quote": quote, "retrieved_at": "2026-11-07T10:00:00Z"}
    return {"text": "t", "fact": fact, "citations": [cite]}


def _out(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"claims": list(claims)}


# --- normalise


def test_normalise_collapses_whitespace_and_strips_markdown() -> None:
    assert normalise(PAGE) == "Title The quick brown fox jumps over the lazy dog."


def test_normalise_unwraps_the_fetch_block_and_maps_typography() -> None:
    wrapped = web._wrap("a page", "It’s “fine” — really", 4096)
    assert normalise(wrapped) == 'It\'s "fine" - really'


def test_normalise_keeps_underscores_inside_words_and_case() -> None:
    assert normalise("call _very_ my_func Now") == "call very my_func Now"


def test_a_quote_must_be_present() -> None:
    assert quote_in_page("quick brown fox jumps", PAGE)
    assert quote_in_page("lazy dog.", PAGE)
    assert not quote_in_page("slow brown fox", PAGE)
    assert not quote_in_page("quick brown FOX", PAGE)
    assert not quote_in_page("brown quick", PAGE)
    assert not quote_in_page("  **  ", PAGE)


# --- check_citations


def test_a_present_quote_resolves() -> None:
    fetch = FakeFetcher({PAGE_URL: PAGE})
    assert check_citations(CHECK, _out(_claim("The quick brown fox")), fetch) == ()


def test_a_missing_quote_fails_and_names_the_citation() -> None:
    fetch = FakeFetcher({PAGE_URL: PAGE})
    failures = check_citations(CHECK, _out(_claim("The slow brown fox")), fetch)
    assert failures == (f"claims.0.citations.0: the quote is not in {PAGE_URL}",)


def test_every_failing_citation_is_listed_and_a_url_is_fetched_once() -> None:
    fetch = FakeFetcher({PAGE_URL: PAGE})
    out = _out(_claim("no such text"), _claim("The quick brown fox"), _claim("nor this"))
    assert len(check_citations(CHECK, out, fetch)) == 2
    assert fetch.calls == [PAGE_URL]


def test_claims_that_are_not_facts_are_not_checked() -> None:
    fetch = FakeFetcher({PAGE_URL: PAGE})
    out = _out(_claim("The quick brown fox"), _claim("invented", "https://evil.test/", fact=False))
    assert check_citations(CHECK, out, fetch) == ()
    assert fetch.calls == [PAGE_URL]


def test_a_fact_without_a_citation_fails() -> None:
    claim = {"text": "t", "fact": True, "citations": []}
    failures = check_citations(CHECK, _out(claim), FakeFetcher({}))
    assert failures == ("claims.0: a fact has no citation",)


def test_an_output_with_no_fact_fails() -> None:
    [failure] = check_citations(CHECK, _out(_claim("x", fact=False)), FakeFetcher({}))
    assert "no claim is marked as a fact" in failure


def test_a_missing_or_wrong_claims_field_fails() -> None:
    assert "is missing" in check_citations(CHECK, {}, FakeFetcher({}))[0]
    assert "is not a list" in check_citations(CHECK, {"claims": "x"}, FakeFetcher({}))[0]


def test_claims_are_found_by_a_dotted_path() -> None:
    check = CitationsCheck(claims="report.claims", allow_domains=("example.com",))
    out = {"report": {"claims": [_claim("The quick brown fox")]}}
    assert check_citations(check, out, FakeFetcher({PAGE_URL: PAGE})) == ()


def test_a_url_outside_the_domains_is_an_error_and_is_not_fetched() -> None:
    fetch = FakeFetcher({})
    with pytest.raises(CitationCheckError, match="not in the allow-list"):
        check_citations(CHECK, _out(_claim("x", "https://other.test/p")), fetch)
    assert fetch.calls == []


def test_a_subdomain_of_an_allowed_domain_is_allowed() -> None:
    url = "https://docs.example.com/p"
    assert check_citations(CHECK, _out(_claim("The quick", url)), FakeFetcher({url: PAGE})) == ()


def test_a_bad_url_is_an_error() -> None:
    with pytest.raises(CitationCheckError):
        check_citations(CHECK, _out(_claim("x", "not a url")), FakeFetcher({}))


def test_a_fetch_failure_is_an_error_not_a_failed_quote() -> None:
    fetch = FakeFetcher({PAGE_URL: ToolExecutionError("agb web fetch failed")})
    with pytest.raises(CitationCheckError, match="agb web fetch failed"):
        check_citations(CHECK, _out(_claim("The quick brown fox")), fetch)


def test_a_missing_agb_is_not_turned_into_a_case_error() -> None:
    fetch = FakeFetcher({PAGE_URL: ToolUnavailableError("agb is not installed")})
    with pytest.raises(ToolUnavailableError):
        check_citations(CHECK, _out(_claim("x")), fetch)


# --- the case schema


def test_allow_domains_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        CitationsCheck(allow_domains=())


def test_only_a_rubric_case_may_use_the_check() -> None:
    with pytest.raises(ValidationError, match="only a rubric case"):
        Case(
            id="c",
            fixture="f",
            expected=Expected(verdict="approve", citations_resolve=CHECK),
            grader="llm_judge",
        )


# --- grade and evaluate


class PassingJudge:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, expected: Expected, actual: Any) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(passed=True, score=1.0, reason="fine")


def _case() -> Case:
    rubric = Rubric(criteria=(Criterion(id="ok", text="fine"),), threshold=0.5)
    return Case(
        id="research",
        fixture="off-by-one",
        expected=Expected(verdict=None, rubric=rubric, citations_resolve=CHECK),
        grader="rubric",
    )


def test_a_false_quote_fails_the_case_before_the_grader_model_is_called() -> None:
    judge = PassingJudge()
    graded = grade(
        _case(),
        _out(_claim("The slow brown fox")),
        judge,
        rubric_judge=judge,
        fetcher=FakeFetcher({PAGE_URL: PAGE}),
    )
    assert not graded.passed
    assert "the quote is not in" in graded.reason
    assert judge.calls == 0


def test_resolving_citations_go_on_to_the_grader() -> None:
    judge = PassingJudge()
    graded = grade(
        _case(),
        _out(_claim("The quick brown fox")),
        judge,
        rubric_judge=judge,
        fetcher=FakeFetcher({PAGE_URL: PAGE}),
    )
    assert graded.passed
    assert judge.calls == 1


def test_the_check_needs_a_fetcher_or_a_repository() -> None:
    judge = PassingJudge()
    with pytest.raises(UsageError):
        grade(_case(), _out(_claim("x")), judge, rubric_judge=judge)


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("code-reviewer", "^1.2")
    assert isinstance(found, LLMSpec)
    return found


class ResearchOutput(BaseModel):
    """A research agent's output model, as a later research agent would write it."""

    claims: list[Claim]


def _research_runner(answer: dict[str, Any]) -> Any:
    def runner(spec: LLMSpec, payload: Any, trace: Trace, **kwargs: Any) -> ResearchOutput:
        return ResearchOutput.model_validate(answer)

    return runner


def _run(spec: LLMSpec, tmp_path: Path, answer: dict[str, Any], fetch: FakeFetcher) -> EvalReport:
    repo = tmp_path / "repos" / "off-by-one"
    repo.mkdir(parents=True)
    return evaluate(
        spec,
        [_case()],
        Trace(tmp_path / "traces.duckdb"),
        runner=_research_runner(answer),
        rubric_judge=PassingJudge(),
        fetcher=fetch,
        fixture_paths={"off-by-one": repo},
        eval_run_id="eval-run-1",
    )


def test_evaluate_passes_a_case_whose_citations_resolve(spec: LLMSpec, tmp_path: Path) -> None:
    answer = _out(_claim("The quick brown fox"))
    [result] = _run(spec, tmp_path, answer, FakeFetcher({PAGE_URL: PAGE})).results
    assert result.passed
    assert result.error is None


def test_evaluate_fails_a_case_with_a_false_quote(spec: LLMSpec, tmp_path: Path) -> None:
    [result] = _run(spec, tmp_path, _out(_claim("lies")), FakeFetcher({PAGE_URL: PAGE})).results
    assert not result.passed
    assert result.error is None
    assert result.grade is not None
    assert "the quote is not in" in result.grade.reason


def test_evaluate_reports_a_network_error_as_a_case_error(spec: LLMSpec, tmp_path: Path) -> None:
    fetch = FakeFetcher({PAGE_URL: ToolExecutionError("timed out")})
    [result] = _run(spec, tmp_path, _out(_claim("The quick")), fetch).results
    assert not result.passed
    assert result.grade is None
    assert result.error is not None
    assert result.error.startswith("CitationCheckError:")


def test_evaluate_reports_a_url_outside_the_domains_as_a_case_error(
    spec: LLMSpec, tmp_path: Path
) -> None:
    answer = _out(_claim("x", "https://other.test/p"))
    [result] = _run(spec, tmp_path, answer, FakeFetcher({})).results
    assert result.error is not None
    assert "not in the allow-list" in result.error
