"""`JevSpec`: the validated `kind: jev` manifest, and the output models generated from it.

A Jev agent asks typed questions about a text and gets probabilities back. The fields, the gate
rules and the shape of each answer are fixed in `docs/CONVENTIONS.md` under "Manifest: kind jev";
the wire format they map onto is in `docs/jev-sdk.md`. Everything the manifest can get wrong is
caught when it loads: unknown keys, duplicate ids, counts out of range, and every gate or routing
entry that points at a question, option or gate that does not exist.

`JevSpec.output_class` is the Pydantic model for one run's answers: a `JevOutput` with one field per
question id, each field typed by the question (`NoulAnswer`, `ChoiceAnswer` or `ScoreAnswer`).
"""

import keyword
import re
from string import Formatter
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    create_model,
    field_validator,
    model_validator,
)

from registry.spec import AgentSpecBase, EvalsSpec, Name, _Model

MIN_CHOICE_OPTIONS = 2
MAX_CHOICE_OPTIONS = 10
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

# `jev-1.14.0` is accepted; the rolling aliases `jev-latest` and `jev-preview` are not.
JEV_MODEL_PATTERN = r"^jev-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"

type Identifier = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
type Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type Label = Annotated[str, Field(min_length=1, pattern=r"^\S(.*\S)?$")]

GATE_RULES = ("p_above", "p_below", "confidence_below", "answer_in")
GATE_ACTIONS = ("skip", "escalate")


class JevEvalsSpec(EvalsSpec):
    """The `evals` block of a Jev agent, which may also set the recall floor for risky diffs."""

    min_recall_risky: float | None = Field(default=None, ge=0, le=1)


class InputSpec(_Model):
    """The `input` block: how the payload becomes the text Jev reads, and how long it may be.

    `render` is a `str.format` template whose names are simple payload field names; `fields` lists
    them. Anything rendered beyond `max_chars` is cut by the runner, which flags the run truncated.
    """

    render: str = Field(min_length=1)
    max_chars: int = Field(gt=0)

    @field_validator("render")
    @classmethod
    def _plain_fields_only(cls, value: str) -> str:
        names = _template_fields(value)
        if not names:
            raise ValueError("template uses no payload field, so the payload would be ignored")
        return value

    @property
    def fields(self) -> tuple[str, ...]:
        """The payload field names the template uses, in order of first appearance."""
        return _template_fields(self.render)


def _template_fields(template: str) -> tuple[str, ...]:
    """Return the field names of a `str.format` template; raise `ValueError` for anything but plain names.

    A name must be an identifier. Positional (`{}`, `{0}`), attribute (`{a.b}`) and index (`{a[0]}`)
    access, and fields nested inside a format spec, are rejected so that a template can only ever
    read the payload's own fields.
    """
    names: list[str] = []
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"template is not valid: {exc}") from exc
    for _, name, spec, _ in parsed:
        if name is None:
            continue
        if not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f"template field {name!r} must be a plain payload field name")
        if spec and "{" in spec:
            raise ValueError(f"template field {name!r} nests a field in its format spec")
        if name not in names:
            names.append(name)
    return tuple(names)


class _QuestionBase(_Model):
    id: Identifier
    text: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _usable_as_field(cls, value: str) -> str:
        if keyword.iskeyword(value) or value.startswith("model_"):
            raise ValueError(f"{value!r} cannot be a field of the output model")
        return value


class NoulQuestion(_QuestionBase):
    """A yes-or-no question. The answer is `p`, the probability of yes; it has no confidence."""

    type: Literal["noul"]


class ChoiceQuestion(_QuestionBase):
    """A question with 2 to 10 unique answers fixed in advance."""

    type: Literal["choice"]
    options: tuple[Label, ...] = Field(min_length=MIN_CHOICE_OPTIONS, max_length=MAX_CHOICE_OPTIONS)

    @field_validator("options")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _no_duplicates(value, "options")
        return value


class ScoreQuestion(_QuestionBase):
    """A question answered on an ordered scale of 2 to 10 named levels, lowest first.

    `range` is `[min, max]`, with `min` below `max`, and is the scale the answer's `value` is
    reported on. `levels` name the steps; each level becomes a key of the answer's `probs`.
    """

    type: Literal["score"]
    range: tuple[
        Annotated[float, Field(allow_inf_nan=False)], Annotated[float, Field(allow_inf_nan=False)]
    ]
    levels: tuple[Label, ...] = Field(min_length=MIN_SCORE_LEVELS, max_length=MAX_SCORE_LEVELS)

    @field_validator("range")
    @classmethod
    def _min_below_max(cls, value: tuple[float, float]) -> tuple[float, float]:
        low, high = value
        if not low < high:
            raise ValueError(f"range must have min below max, got [{low}, {high}]")
        return value

    @field_validator("levels")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _no_duplicates(value, "levels")
        return value


type Question = Annotated[
    NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")
]


def _no_duplicates(values: tuple[str, ...], what: str) -> None:
    repeated = sorted({v for v in values if values.count(v) > 1})
    if repeated:
        raise ValueError(f"duplicate {what}: {', '.join(repr(v) for v in repeated)}")


class _GateBase(_Model):
    name: Identifier
    question: Identifier
    action: Literal["skip", "escalate"]
    unless: Identifier | None = None
    """The name of another gate; this gate does not fire when that one fires.

    The named gate must exist, must not be this gate, and must have no `unless` of its own, so a
    manifest cannot chain or loop the rule.
    """


class ProbabilityGate(_GateBase):
    """`p_above` or `p_below`: compares a probability with `value`.

    On a noul question the probability is `p`. On a choice or score question it is `probs[option]`,
    so `option` names the answer or level to read; a noul gate must not set it.
    """

    rule: Literal["p_above", "p_below"]
    value: Probability
    option: str | None = None


class ConfidenceGate(_GateBase):
    """`confidence_below`: fires when the answer's `confidence` is under `value`.

    Choice and score answers carry a confidence. A noul answer has none, so this rule is invalid
    on a noul question.
    """

    rule: Literal["confidence_below"]
    value: Probability


class AnswerInGate(_GateBase):
    """`answer_in`: fires when a choice's `answer`, or a score's `level`, is one of `value`."""

    rule: Literal["answer_in"]
    value: tuple[str, ...] = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _no_duplicates(value, "answers")
        return value


type Gate = Annotated[ProbabilityGate | ConfidenceGate | AnswerInGate, Field(discriminator="rule")]


class RouteSpec(_Model):
    """One routing entry. `when` is a gate name or `question=answer`; `route` lists agent names.

    The agents run in the order given, and none may repeat.
    """

    when: str = Field(min_length=1)
    route: tuple[Name, ...] = Field(min_length=1)

    @field_validator("route")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _no_duplicates(value, "agents")
        return value


class JevSpec(AgentSpecBase):
    """A Jev agent: typed questions over a rendered input, with gates and routing on the answers.

    The loader returns this for `kind: jev`. Besides the shared fields it validates every field in
    `docs/CONVENTIONS.md` under "Manifest: kind jev". `model` must be a pinned id such as
    `jev-1.14.0`, never an alias, because gate thresholds are tuned against one version. Cross
    references are checked together: every gate names a real question and a rule that question's
    type supports, and every routing entry names a real gate or a real answer.

    `act_on_decision` is off by default: a pipeline then logs the `Decision` and runs everything.
    With it on, a pipeline that is allowed to act honours `skip` and `route` (#20).
    """

    kind: Literal["jev"]
    evals: JevEvalsSpec
    model: str = Field(pattern=JEV_MODEL_PATTERN)
    input: InputSpec
    questions: tuple[Question, ...] = Field(min_length=1)
    gates: tuple[Gate, ...] = ()
    routing: tuple[RouteSpec, ...] = ()
    act_on_decision: bool = False

    _output_class: type[JevOutput] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check_references(self) -> JevSpec:
        problems = _check_questions(self.questions)
        by_id = {q.id: q for q in self.questions}
        problems += _check_gates(self.gates, by_id)
        problems += _check_routing(self.routing, self.gates, by_id)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def output_class(self) -> type[JevOutput]:
        """The `JevOutput` subclass for this agent, with one field per question id.

        Built on first use and then reused, so every caller sees the same class.
        """
        if self._output_class is None:
            self._output_class = build_output_model(self)
        return self._output_class


def _check_questions(questions: tuple[Question, ...]) -> list[str]:
    seen: set[str] = set()
    problems = []
    for question in questions:
        if question.id in seen:
            problems.append(f"questions: duplicate id {question.id!r}")
        seen.add(question.id)
    return problems


def _check_gates(gates: tuple[Gate, ...], by_id: dict[str, Question]) -> list[str]:
    problems = []
    names: set[str] = set()
    by_name = {gate.name: gate for gate in gates}
    for index, gate in enumerate(gates):
        where = f"gates[{index}] ({gate.name})"
        if gate.name in names:
            problems.append(f"{where}: duplicate gate name")
        names.add(gate.name)
        problems += _check_unless(where, gate, by_name)
        question = by_id.get(gate.question)
        if question is None:
            problems.append(f"{where}: question {gate.question!r} is not defined")
            continue
        problems += _check_gate_rule(where, gate, question)
    return problems


def _check_unless(where: str, gate: Gate, by_name: dict[str, Gate]) -> list[str]:
    if gate.unless is None:
        return []
    if gate.unless == gate.name:
        return [f"{where}: unless names the gate itself"]
    other = by_name.get(gate.unless)
    if other is None:
        return [f"{where}: unless names no gate {gate.unless!r}"]
    if other.unless is not None:
        return [f"{where}: unless names {gate.unless!r}, which has an unless of its own"]
    return []


def _check_gate_rule(where: str, gate: Gate, question: Question) -> list[str]:
    answers = _answers(question)
    match gate:
        case ConfidenceGate() if isinstance(question, NoulQuestion):
            return [f"{where}: confidence_below is invalid on noul question {question.id!r}"]
        case AnswerInGate() if answers is None:
            return [f"{where}: answer_in is invalid on noul question {question.id!r}"]
        case AnswerInGate() if answers is not None:
            unknown = [v for v in gate.value if v not in answers]
            if unknown:
                return [f"{where}: {question.id!r} has no answer {', '.join(map(repr, unknown))}"]
        case ProbabilityGate() if answers is None:
            if gate.option is not None:
                return [f"{where}: option is invalid on noul question {question.id!r}"]
        case ProbabilityGate() if answers is not None:
            if gate.option is None:
                return [f"{where}: {gate.rule} on {question.id!r} needs an option"]
            if gate.option not in answers:
                return [f"{where}: {question.id!r} has no answer {gate.option!r}"]
    return []


def _answers(question: Question) -> tuple[str, ...] | None:
    """The options of a choice or the levels of a score; `None` for a noul question."""
    match question:
        case ChoiceQuestion():
            return question.options
        case ScoreQuestion():
            return question.levels
        case NoulQuestion():
            return None


def _check_routing(
    routing: tuple[RouteSpec, ...], gates: tuple[Gate, ...], by_id: dict[str, Question]
) -> list[str]:
    gate_names = {g.name for g in gates}
    problems = []
    seen: set[str] = set()
    for index, entry in enumerate(routing):
        where = f"routing[{index}] ({entry.when})"
        if entry.when in seen:
            problems.append(f"{where}: duplicate entry")
        seen.add(entry.when)
        question_id, separator, answer = entry.when.partition("=")
        if not separator:
            if entry.when not in gate_names:
                problems.append(f"{where}: no gate named {entry.when!r}")
            continue
        question = by_id.get(question_id)
        answers = None if question is None else _answers(question)
        if question is None:
            problems.append(f"{where}: question {question_id!r} is not defined")
        elif answers is None:
            problems.append(f"{where}: noul question {question_id!r} has no answers to route on")
        elif answer not in answers:
            problems.append(f"{where}: {question_id!r} has no answer {answer!r}")
    return problems


class _Answer(BaseModel):
    """Base of the generated answer models: immutable, and unknown keys are errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoulAnswer(_Answer):
    """A noul answer: `p` is the probability of yes. There is no confidence."""

    p: Probability


class ChoiceAnswer(_Answer):
    """A choice answer: the likeliest option, a probability for every option, and a confidence."""

    answer: str
    probs: dict[str, Probability]
    confidence: Probability


class ScoreAnswer(_Answer):
    """A score answer: the expected `value`, the likeliest `level`, `probs` per level, confidence."""

    value: float = Field(allow_inf_nan=False)
    level: str
    probs: dict[str, Probability]
    confidence: Probability


class JevOutput(BaseModel):
    """Base of every generated output model; a subclass has one field per question id."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def build_output_model(spec: JevSpec) -> type[JevOutput]:
    """Generate the output model of `spec`: a `JevOutput` with one field per question id.

    A noul field is a `NoulAnswer`. A choice or score field is a subclass of `ChoiceAnswer` or
    `ScoreAnswer` that also checks `answer` or `level` against the question's options or levels,
    and that `probs` has exactly one entry for each of them. Prefer `spec.output_class`, which
    builds the model once.
    """
    fields: dict[str, Any] = {q.id: (_answer_model(q), ...) for q in spec.questions}
    name = "".join(part.capitalize() for part in re.split(r"[-_]", spec.name)) + "Output"
    return create_model(name, __base__=JevOutput, __doc__=spec.description, **fields)


def _answer_model(question: Question) -> type[_Answer]:
    match question:
        case NoulQuestion():
            return NoulAnswer
        case ChoiceQuestion():
            return _closed_answer(question, ChoiceAnswer, "answer", question.options)
        case ScoreQuestion():
            return _closed_answer(question, ScoreAnswer, "level", question.levels)


def _closed_answer[A: _Answer](
    question: Question, base: type[A], picked: str, allowed: tuple[str, ...]
) -> type[A]:
    """Subclass `base` so that `picked` is one of `allowed` and `probs` covers exactly `allowed`."""

    def check(self: Any) -> Any:
        chosen = getattr(self, picked)
        if chosen not in allowed:
            raise ValueError(f"{question.id}.{picked} {chosen!r} is not one of {list(allowed)}")
        if set(self.probs) != set(allowed):
            raise ValueError(f"{question.id}.probs must have exactly the keys {list(allowed)}")
        return self

    validators: dict[str, Any] = {"_closed": model_validator(mode="after")(check)}
    return create_model(
        f"{question.id.title().replace('_', '')}Answer",
        __base__=base,
        __validators__=validators,
        __doc__=question.text,
    )
