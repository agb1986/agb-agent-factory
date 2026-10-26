"""The pipeline budget: a dollar cap shared by every agent run of one pipeline invocation.

A pipeline builds one `Budget` and passes it to every `runtime.runner.run`,
`runtime.async_runner.arun` and `runtime.jev_runner.run_jev` call, and to the eval graders, through
their `budget` keyword. A run with a budget

- checks it before it starts, and refuses to start once nothing is left;
- charges each model message's cost as that message arrives, so runs that share the budget, on
  threads or gathered on one event loop, see each other's spend while they are still running; and
- raises `BudgetExceeded` from the charge that takes the spend past the cap, so a run that would
  pass the cap stops at its next message instead of finishing. A Jev run or a judge call is one
  charge, made when it returns.

A run that fails for another reason only adds its uncharged cost, so the budget never hides the
error that ended it. The first charge past the cap raises `BudgetExceeded`, and a pipeline lets
that error end the whole pipeline: it is not a failed reviewer or a failed eval case.

The overshoot bound. A run cannot be stopped in the middle of a request, so runs in flight when
the cap is passed each finish the request they are waiting on, charge it, and stop there. The
final spend is therefore at most the cap plus one model message, one Jev call or one judge call
per run in flight at the moment the cap was passed. Runs that are not yet started are refused.
A run whose model has no price costs nothing here, as in the trace.

`Budget` is safe to share between threads; its arithmetic is under a lock.
"""

import threading

from factory.exit import FactoryError, UsageError

DEFAULT_BUDGET_USD = 5.0
"""The pipeline budget when no `--budget-usd` is given. Edit it here; nothing else holds the default."""


class BudgetExceeded(FactoryError):
    """The pipeline's spend passed its budget (exit 1). The message states the spend so far."""

    def __init__(self, budget_usd: float, spent_usd: float) -> None:
        super().__init__(
            f"budget of ${budget_usd:.4f} exceeded: ${spent_usd:.4f} spent so far",
            hint="raise it with --budget-usd, or see where the spend goes with `factory cost`",
        )
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd


class Budget:
    """A dollar cap and the spend charged against it so far."""

    def __init__(self, usd: float) -> None:
        """Make a budget of `usd` dollars. Raises `UsageError` unless `usd` is positive."""
        if not usd > 0:
            raise UsageError(f"a budget must be positive, not {usd}", hint="pass --budget-usd > 0")
        self.usd = float(usd)
        self._spent = 0.0
        self._lock = threading.Lock()

    @property
    def spent(self) -> float:
        """Dollars charged so far."""
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> float:
        """Dollars left; negative once the cap has been passed."""
        return self.usd - self.spent

    def check(self, pending: float = 0.0) -> None:
        """Raise `BudgetExceeded` if the spend plus `pending` passes the cap.

        With no `pending` it also raises when the spend has reached the cap exactly, because a
        run that starts then can only go over.
        """
        spent = self.spent
        if spent + pending > self.usd or (pending == 0.0 and spent >= self.usd):
            raise BudgetExceeded(self.usd, spent + pending)

    def charge(self, cost: float | None) -> None:
        """Add `cost` to the spend, then raise `BudgetExceeded` if that passes the cap.

        `None` (an unpriced run) adds nothing.
        """
        spent = self.add(cost)
        if spent > self.usd:
            raise BudgetExceeded(self.usd, spent)

    def add(self, cost: float | None) -> float:
        """Add `cost` to the spend without checking the cap, and return the new spend."""
        with self._lock:
            self._spent += cost or 0.0
            return self._spent
