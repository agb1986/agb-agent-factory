"""Tests for the exit codes, the exception-to-code mapping and the JSON error envelope."""

import argparse
import json

import anthropic
import pytest

from factory.exit import (
    AuthOrConfigError,
    ExitCode,
    FactoryError,
    NotFoundError,
    UsageError,
    error_envelope,
    exit_code,
    known_exceptions,
)


def test_codes_are_distinct() -> None:
    # `__members__` includes aliases, which a duplicate value would create silently.
    values = [int(code) for code in ExitCode.__members__.values()]
    assert len(values) == len(set(values))


def test_numeric_values_are_fixed() -> None:
    assert {code.name: int(code) for code in ExitCode} == {
        "OK": 0,
        "ERROR": 1,
        "USAGE": 2,
        "NOT_FOUND": 3,
        "AUTH_OR_CONFIG": 4,
    }


def test_every_code_has_its_own_docstring() -> None:
    docs = [code.__doc__ for code in ExitCode]
    assert all(docs)
    assert ExitCode.__doc__ not in docs
    assert len(set(docs)) == len(docs)


def test_claimed_types_are_fixed() -> None:
    assert known_exceptions() == {
        FactoryError: ExitCode.ERROR,
        UsageError: ExitCode.USAGE,
        NotFoundError: ExitCode.NOT_FOUND,
        AuthOrConfigError: ExitCode.AUTH_OR_CONFIG,
        argparse.ArgumentError: ExitCode.USAGE,
        anthropic.AuthenticationError: ExitCode.AUTH_OR_CONFIG,
        anthropic.PermissionDeniedError: ExitCode.AUTH_OR_CONFIG,
    }


def test_claims_cover_every_failure_code() -> None:
    assert set(known_exceptions().values()) == set(ExitCode) - {ExitCode.OK}


@pytest.mark.parametrize(
    ("exc_type", "code"), list(known_exceptions().items()), ids=lambda v: getattr(v, "__name__", v)
)
def test_mapping_is_total_for_claimed_types(exc_type: type[BaseException], code: ExitCode) -> None:
    # `exit_code` looks only at the type, so skip constructors that need an HTTP response.
    exc = exc_type.__new__(exc_type)
    assert exit_code(exc) is code


def test_subclass_of_claimed_type_inherits_its_code() -> None:
    class AgentNotFound(NotFoundError):
        pass

    class WrongKey(anthropic.AuthenticationError):
        pass

    assert exit_code(AgentNotFound("no agent named 'x'")) is ExitCode.NOT_FOUND
    assert exit_code(WrongKey.__new__(WrongKey)) is ExitCode.AUTH_OR_CONFIG


def test_factory_error_subclass_can_set_its_own_code() -> None:
    class MissingFixture(FactoryError):
        code = ExitCode.NOT_FOUND

    assert exit_code(MissingFixture("fixture 'off-by-one' not built")) is ExitCode.NOT_FOUND


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad"),
        KeyError("k"),
        RuntimeError(),
        FileNotFoundError("git"),
        KeyboardInterrupt(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_unknown_exceptions_map_to_error(exc: BaseException) -> None:
    assert exit_code(exc) is ExitCode.ERROR
    assert int(exit_code(exc)) == 1


def test_envelope_for_factory_error_carries_code_message_and_hint() -> None:
    exc = NotFoundError("no agent named 'x'", hint="run `factory list`")
    assert error_envelope(exc) == {
        "error": {"code": 3, "message": "no agent named 'x'", "hint": "run `factory list`"}
    }


def test_envelope_for_unknown_exception_has_null_hint() -> None:
    assert error_envelope(ValueError("bad")) == {
        "error": {"code": 1, "message": "bad", "hint": None}
    }


def test_envelope_message_falls_back_to_class_name() -> None:
    assert error_envelope(RuntimeError())["error"]["message"] == "RuntimeError"


def test_envelope_serialises_to_one_json_line() -> None:
    line = json.dumps(error_envelope(UsageError("unknown flag --jsn", hint="did you mean --json?")))
    assert "\n" not in line
    assert json.loads(line) == {
        "error": {"code": 2, "message": "unknown flag --jsn", "hint": "did you mean --json?"}
    }
