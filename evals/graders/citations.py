"""The `citations_resolve` check: every citation a research output relies on must be true.

`rubric` scores the reasoning of a research agent; this check covers the facts. It reads the
claims of the output (see `registry.common_schemas.Claim`), fetches the URL of each citation of
each claim marked `fact: true` through `web.fetch`, and requires the citation's quote to appear in
the page. It is configured by `expected.citations_resolve` in a case (`evals.cases.CitationsCheck`)
and runs after the other deterministic checks and before the grader model is called, so a case with
a false quote costs no grading call. Which agents need it is in docs/evals.md.

A quote that is not in the page, and a fact with no citation, fail the case. A page that cannot be
had does not: a URL outside the case's domains, a failed or timed-out fetch or an invalid URL raises
`CitationCheckError`, which the eval runner reports as a case error, never as a pass.
"""

import re
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from evals.cases import CitationsCheck
from factory.exit import FactoryError, UsageError
from tools import web
from tools._common import ToolError, ToolUnavailableError

type Fetcher = Callable[[str], str]
"""Returns the page at a URL as `web.fetch` does: Markdown, possibly in its untrusted-data block."""

FETCH_MAX_BYTES = 4_000_000
"""How much of a page the default fetcher keeps; a quote past the cut cannot be found."""

_IMAGE_OR_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_LINE_MARKUP = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+)", re.MULTILINE
)
_EMPHASIS = re.compile(r"[*`]|(?<!\w)_|_(?!\w)")
_PUNCTUATION = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


class CitationCheckError(FactoryError):
    """A citation could not be checked: its page was out of bounds or could not be fetched."""


def web_fetcher(repo_path: str | Path) -> Fetcher:
    """Return a `Fetcher` that calls `tools.web.fetch` with a large size limit.

    The agent's allow-list is not applied here: `check_citations` applies the case's domains
    before it calls the fetcher.
    """
    return lambda url: web.fetch(repo_path, url, max_bytes=FETCH_MAX_BYTES)


def normalise(text: str) -> str:
    """Return `text` in the form quotes and pages are compared in.

    Applied identically to the quote and the page, so it can only make two texts equal that a
    reader would call the same passage; it never ignores words. It removes the untrusted-data block
    `web.fetch` wraps a page in, applies Unicode NFKC, turns Markdown links and images into their
    text, drops heading, quote, list and number markers at the start of a line, drops `*`, backticks
    and underscores that mark emphasis (not those inside a word), maps curly quotes and en and em
    dashes to ASCII, and collapses every run of whitespace to one space. Case is kept.
    """
    text = unicodedata.normalize("NFKC", web.unwrap(text))
    text = _IMAGE_OR_LINK.sub(r"\1", text)
    text = _LINE_MARKUP.sub("", text)
    text = _EMPHASIS.sub("", text).translate(_PUNCTUATION)
    return " ".join(text.split())


def quote_in_page(quote: str, page: str) -> bool:
    """Return whether the normalised `quote` is a substring of the normalised `page`.

    A quote that normalises to nothing is never found.
    """
    wanted = normalise(quote)
    return bool(wanted) and wanted in normalise(page)


def check_citations(
    check: CitationsCheck, actual: Mapping[str, Any], fetch: Fetcher
) -> tuple[str, ...]:
    """Run `citations_resolve` on `actual` and return one line per failed citation.

    `check.claims` is a dotted path through the output's mappings to a list of claims. It is a
    failure when the path is missing or not a list, when it holds no claim marked `fact: true`
    (a research output with no fact has nothing to verify), when such a claim has no citations,
    and when a citation's quote is not in its page (see `normalise`). Claims with `fact` false are
    not checked. Each URL is fetched once per call. Raises `CitationCheckError` when a URL is
    outside `check.allow_domains` or invalid, or when `fetch` raises a `ToolError`; a missing
    `agb` (`ToolUnavailableError`) propagates, as it is a setup problem and not a case result.
    """
    value: Any = actual
    for part in check.claims.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return (f"claims field {check.claims} is missing",)
        value = value[part]
    if not isinstance(value, list):
        return (f"claims field {check.claims} is not a list",)
    facts = [
        (index, claim)
        for index, claim in enumerate(value)
        if isinstance(claim, Mapping) and claim.get("fact") is True
    ]
    if not facts:
        return (f"{check.claims}: no claim is marked as a fact, so nothing can be verified",)
    pages: dict[str, str] = {}
    failures: list[str] = []
    for index, claim in facts:
        where = f"{check.claims}.{index}"
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            failures.append(f"{where}: a fact has no citation")
            continue
        for number, citation in enumerate(citations):
            url, quote = _fields(citation)
            if url is None or quote is None:
                failures.append(f"{where}.citations.{number}: needs a url and a quote")
                continue
            if url not in pages:
                pages[url] = _page(url, check.allow_domains, fetch)
            if not quote_in_page(quote, pages[url]):
                failures.append(f"{where}.citations.{number}: the quote is not in {url}")
    return tuple(failures)


def _fields(citation: Any) -> tuple[str | None, str | None]:
    if not isinstance(citation, Mapping):
        return None, None
    url, quote = citation.get("url"), citation.get("quote")
    return (
        url if isinstance(url, str) and url else None,
        quote if isinstance(quote, str) and quote else None,
    )


def _page(url: str, allow_domains: tuple[str, ...], fetch: Fetcher) -> str:
    try:
        web.check_web_call("web.fetch", {"url": url}, allow_domains)
        return fetch(url)
    except ToolUnavailableError:
        raise
    except (ToolError, UsageError) as exc:
        raise CitationCheckError(f"{url}: {exc}") from exc
