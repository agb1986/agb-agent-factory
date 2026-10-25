"""Exit codes, the error types that carry them, and the JSON error envelope.

`ExitCode` is the single source of truth for the codes every `factory` command returns. The numbers
match agb-tools, so the `agb factory` wrapper (#30) passes them through unchanged.

Raise a `FactoryError` subclass for a failure the user or agent should see, and let the CLI's root
handler turn any exception into a code with `exit_code()` and, under `--json`, into the stderr
envelope with `error_envelope()`. Never call `sys.exit` with a bare number.

Messages and hints are shown to users and agents: never put a secret in either.
"""

import argparse
from collections.abc import Mapping
from enum import IntEnum
from typing import ClassVar, Self


class ExitCode(IntEnum):
    """Process exit codes for every `factory` command. Each member's docstring says when to use it."""

    def __new__(cls, value: int, doc: str) -> Self:
        member = int.__new__(cls, value)
        member._value_ = value
        member.__doc__ = doc
        return member

    OK = 0, "Success. Under `--json`, the result document is on stdout."
    ERROR = 1, "Failure, or a negative result such as ship false or a pass rate below the floor."
    USAGE = 2, "Bad arguments or flags, including a missing or unknown subcommand."
    NOT_FOUND = 3, "The agent, version, run, fixture or file asked for does not exist."
    AUTH_OR_CONFIG = 4, "Missing or invalid key, config or required tool; also API auth failures."


class FactoryError(Exception):
    """A failure the user or agent should see: a one-line `message` and an optional `hint`.

    The exit code is the class attribute `code`. Subclass the base that matches the failure, so a
    later typed error (a resolver miss, a missing key, `AgentError`, `JevError`, `GuardError`)
    inherits the right code without touching this module.
    """

    code: ClassVar[ExitCode] = ExitCode.ERROR

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(FactoryError):
    """Bad arguments or flags that argparse could not catch itself (exit 2)."""

    code = ExitCode.USAGE


class NotFoundError(FactoryError):
    """The thing asked for does not exist (exit 3)."""

    code = ExitCode.NOT_FOUND


class AuthOrConfigError(FactoryError):
    """A key, config value or required external tool is missing or invalid (exit 4)."""

    code = ExitCode.AUTH_OR_CONFIG


def known_exceptions() -> Mapping[type[BaseException], ExitCode]:
    """Return every exception type this module claims a code for, mapped to that code.

    Subclasses of a listed type inherit its code. Anything not covered maps to `ExitCode.ERROR`.
    `anthropic` is imported here rather than at module level so that commands which never call the
    API, such as `factory --version`, do not pay for the import.
    """
    import anthropic

    return {
        **{cls: cls.code for cls in (FactoryError, UsageError, NotFoundError, AuthOrConfigError)},
        argparse.ArgumentError: ExitCode.USAGE,
        anthropic.AuthenticationError: ExitCode.AUTH_OR_CONFIG,
        anthropic.PermissionDeniedError: ExitCode.AUTH_OR_CONFIG,
    }


def exit_code(exc: BaseException) -> ExitCode:
    """Return the exit code for `exc`.

    A `FactoryError` returns its own `code`. Any other exception returns the code of the nearest
    class in its MRO that `known_exceptions()` lists, and `ExitCode.ERROR` when there is none.
    """
    if isinstance(exc, FactoryError):
        return exc.code
    table = known_exceptions()
    for cls in type(exc).__mro__:
        if issubclass(cls, BaseException) and cls in table:
            return table[cls]
    return ExitCode.ERROR


def error_envelope(exc: BaseException) -> dict[str, dict[str, object]]:
    """Return the JSON error envelope a failed `--json` command writes to stderr as one line.

    The shape is `{"error": {"code": int, "message": str, "hint": str | null}}`. The message falls
    back to the exception's class name when `str(exc)` is empty.
    """
    if isinstance(exc, FactoryError):
        message, hint = exc.message, exc.hint
    else:
        message, hint = str(exc) or type(exc).__name__, None
    return {"error": {"code": int(exit_code(exc)), "message": message, "hint": hint}}
