"""The `rubric` grader: a small model scores prose against a list of criteria.

It is for agents whose output has no planted defect to find, such as a plan, a ticket, a document
or a research report; which agents may use it is in docs/evals.md. A case's `expected.rubric`
lists the criteria, the pass threshold on their mean, and the deterministic checks that run first
(`check_output`): required fields, fields that must be empty, forbidden strings, and paths that
must exist in the fixture repository. The output schema itself was already validated by the runner.

`RubricGrader` has the signature of a `Judge`. It sends the criteria (without the threshold and the
floors, so they cannot sway the scores) and the agent's answer to `RUBRIC_MODEL` with a fixed
system prompt, in one structured-output call that returns a score from 0 to 1 and a reason for
each criterion. `score_rubric` turns the scores into a verdict in code: the answer passes when the
mean reaches the threshold and every required criterion meets its own floor. An answer that does
not score exactly the criteria it was given is retried once, then raises `RubricError`, which the
eval runner reports as a case error, never as a pass. Each call writes one trace row under the
eval's tag and pipeline run id, with its cost, as the `llm_judge` grader does.
"""

import json
import math
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.cases import Expected, Rubric
from evals.graders.llm_judge import JUDGE_MODEL, JudgeError, JudgeVerdict, LLMJudge
from factory.exit import UsageError
from runtime.pricing import TokenUsage

RUBRIC_MODEL = JUDGE_MODEL
"""The model that scores rubrics: the same Haiku model as the `llm_judge` grader."""

RUBRIC_MAX_TOKENS = 2048
RUBRIC_REF = f"eval-rubric@{RUBRIC_MODEL}"
"""The `agent_ref` the rubric grader's trace rows carry."""

SYSTEM_PROMPT = """\
You score the answer of an agent that writes prose, such as a plan, a ticket, documentation or a \
research report, against a rubric.

You get a JSON object with `criteria`, a list of criteria each with an `id` and a `text`, and \
`answer`, the agent's output. Score every criterion on its own, from 0 to 1:

- 1 when the answer meets the criterion fully and specifically.
- 0 when the answer does not meet it at all, or only claims to without saying anything concrete.
- A value in between for an answer that meets it in part, in proportion to how much is missing.

Judge only what the answer says. Do not reward length or confident wording, and do not give \
credit for a claim the answer does not support. A criterion is not met because another one is.

Return one entry in `scores` for each criterion, in the order given, with its `id` unchanged, its \
`score`, and a `reason` of one sentence that names the part of the answer that decided it. Do not \
add, drop or rename criteria.\
"""


class RubricError(JudgeError):
    """The rubric grader could not score the answer: bad scores twice, a refusal, or an API error."""


class CriterionScore(BaseModel):
    """The grader's score for one criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    reason: str


class RubricScores(BaseModel):
    """What the grader model returns: one score per criterion. This is the schema it is sent."""

    model_config = ConfigDict(extra="forbid")

    scores: list[CriterionScore]


class RubricVerdict(JudgeVerdict):
    """A `JudgeVerdict` whose `score` is the mean of the criterion scores it also carries.

    This class is never sent to a model as a schema; `RubricScores` is.
    """

    scores: tuple[CriterionScore, ...] = ()


def score_rubric(rubric: Rubric, scores: Mapping[str, CriterionScore]) -> RubricVerdict:
    """Decide a rubric in code from the grader's `scores`, keyed by criterion id.

    The verdict's `score` is the mean of the criterion scores, and it passes when that mean is at
    or above `rubric.threshold` and no required criterion scores under its floor. The reason names
    the mean and the threshold, every failing criterion with the grader's reason, and on a pass the
    score of each criterion. Raises `KeyError` when a criterion has no score.
    """
    ordered = tuple(scores[criterion.id] for criterion in rubric.criteria)
    mean = round(math.fsum(item.score for item in ordered) / len(ordered), 9)
    problems: list[str] = []
    if mean < rubric.threshold:
        problems.append(f"mean {mean:.2f} is under the threshold {rubric.threshold:.2f}")
    for criterion in rubric.criteria:
        floor = criterion.floor
        item = scores[criterion.id]
        if criterion.required and floor is not None and item.score < floor:
            problems.append(
                f"required criterion {criterion.id} scored {item.score:.2f}, under its floor "
                f"{floor:.2f}: {item.reason}"
            )
    if problems:
        reason = "; ".join(problems)
    else:
        each = ", ".join(f"{item.id} {item.score:.2f}" for item in ordered)
        reason = f"mean {mean:.2f} meets the threshold {rubric.threshold:.2f} ({each})"
    return RubricVerdict(passed=not problems, score=mean, reason=reason, scores=ordered)


def check_output(
    rubric: Rubric, actual: Mapping[str, Any], repo_path: str | Path | None
) -> tuple[str, ...]:
    """Run the rubric's deterministic checks on `actual` and return one line per failure.

    A required field is a dotted path through the output's mappings that must be present and not
    null, blank or an empty collection; an empty field must be present and be exactly that.
    A forbidden string fails wherever it appears, as a
    case-sensitive substring, in any string of the output; the failure names each field it is in.
    A `paths_exist` field must hold a path or a list of paths, each resolving, symlinks followed,
    to an existing file or directory inside `repo_path`. Raises `UsageError` when the rubric has
    `paths_exist` and `repo_path` is None.
    """
    failures: list[str] = []
    for field in rubric.required_fields:
        found, value = _lookup(actual, field)
        if not found:
            failures.append(f"required field {field} is missing")
        elif _empty(value):
            failures.append(f"required field {field} is empty")
    for field in rubric.empty_fields:
        found, value = _lookup(actual, field)
        if not found:
            failures.append(f"empty field {field} is missing")
        elif not _empty(value):
            failures.append(f"field {field} must be empty")
    for phrase in rubric.forbidden:
        where = [path for path, text in _strings(actual, "") if phrase in text]
        if where:
            failures.append(f"forbidden string {phrase!r} in {', '.join(where)}")
    if rubric.paths_exist:
        if repo_path is None:
            raise UsageError("the rubric's paths_exist check needs the fixture repository path")
        root = Path(repo_path).resolve()
        for field in rubric.paths_exist:
            failures.extend(_missing_paths(actual, field, root))
    return tuple(failures)


def _lookup(actual: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    value: Any = actual
    for part in field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, (list, tuple, dict)) and not value


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Yield `(dotted path, text)` for every string in `value`; list items are numbered."""
    if isinstance(value, str):
        yield path or "(answer)", value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}.{index}" if path else str(index))


def _missing_paths(actual: Mapping[str, Any], field: str, root: Path) -> list[str]:
    found, value = _lookup(actual, field)
    if not found:
        return [f"path field {field} is missing"]
    paths = [value] if isinstance(value, str) else value
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        return [f"path field {field} is not a path or a list of paths"]
    failures: list[str] = []
    for item in paths:
        if not item.strip():
            failures.append(f"{field}: an empty path")
            continue
        target = (root / item).resolve()
        if not target.is_relative_to(root):
            failures.append(f"{field}: {item!r} is outside the fixture repository")
        elif not target.exists():
            failures.append(f"{field}: {item!r} does not exist in the fixture repository")
    return failures


class RubricGrader(LLMJudge):
    """The real rubric grader: calls `RUBRIC_MODEL` and traces every call.

    It takes the arguments of `LLMJudge` and shares its client, tracing and budget rules. Called
    with an `expected` that has no `rubric` it raises `UsageError`. The trace row's `input_json`
    holds the whole rubric and the answer, and its `output_json` the verdict with every criterion
    score.
    """

    MODEL = RUBRIC_MODEL
    REF = RUBRIC_REF

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        """Return the rubric verdict on `actual`, tracing the call whether it succeeds or not."""
        if expected.rubric is None:
            raise UsageError("the rubric grader needs a case with expected.rubric")
        return super().__call__(expected, actual)

    def _payload(self, expected: Expected, actual: Mapping[str, Any]) -> dict[str, Any]:
        """Return the rubric and the answer; only the criteria and the answer reach the model."""
        rubric = expected.rubric
        assert rubric is not None
        return {"rubric": rubric.model_dump(mode="json"), "answer": dict(actual)}

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the scoring request: the fixed prompt and the `RubricScores` schema."""
        return {
            "model": RUBRIC_MODEL,
            "max_tokens": RUBRIC_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [dict(message) for message in messages],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(RubricScores),
                }
            },
        }

    def _judge(self, payload: dict[str, Any], replies: list[TokenUsage]) -> JudgeVerdict:
        rubric = Rubric.model_validate(payload["rubric"])
        sent = {
            "criteria": [{"id": item.id, "text": item.text} for item in rubric.criteria],
            "answer": payload["answer"],
        }
        client = self._get_client()
        messages: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(sent)}]
        for attempt in (1, 2):
            text = self._ask(client, messages, replies)
            scores, problems = _parse_scores(text, [item.id for item in rubric.criteria])
            if scores is not None:
                return score_rubric(rubric, scores)
            if attempt == 2:
                raise RubricError(
                    f"the rubric grader answered with invalid scores twice: {problems}"
                )
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        f"Your answer does not validate: {problems}. "
                        "Reply again with only the corrected JSON object."
                    ),
                },
            ]
        raise AssertionError("unreachable")  # pragma: no cover


def _parse_scores(text: str, ids: list[str]) -> tuple[dict[str, CriterionScore] | None, str]:
    """Return the scores keyed by id, or None and why they do not score exactly `ids`."""
    try:
        parsed = RubricScores.model_validate_json(text)
    except ValidationError as exc:
        return None, "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
    given = [item.id for item in parsed.scores]
    problems = []
    if missing := [item for item in ids if item not in given]:
        problems.append(f"no score for {', '.join(missing)}")
    if unknown := [item for item in given if item not in ids]:
        problems.append(f"unknown criteria {', '.join(unknown)}")
    if len(set(given)) != len(given):
        problems.append("a criterion is scored more than once")
    if problems:
        return None, "; ".join(problems)
    return {item.id: item for item in parsed.scores}, ""
