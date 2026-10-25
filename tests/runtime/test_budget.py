"""Tests for runtime/budget.py."""

import threading

import pytest

from factory.exit import ExitCode, UsageError, exit_code
from runtime.budget import DEFAULT_BUDGET_USD, Budget, BudgetExceeded


def test_a_new_budget_has_spent_nothing() -> None:
    budget = Budget(2.0)
    assert budget.spent == 0.0
    assert budget.remaining == 2.0


@pytest.mark.parametrize("usd", [0, -1.0, float("nan")])
def test_a_budget_must_be_positive(usd: float) -> None:
    with pytest.raises(UsageError):
        Budget(usd)


def test_the_default_is_a_positive_number() -> None:
    assert Budget(DEFAULT_BUDGET_USD).remaining == DEFAULT_BUDGET_USD > 0


def test_charge_adds_to_the_spend() -> None:
    budget = Budget(1.0)
    budget.charge(0.25)
    budget.charge(0.25)
    assert budget.spent == 0.5
    assert budget.remaining == 0.5


def test_charge_raises_once_the_cap_is_passed() -> None:
    budget = Budget(1.0)
    budget.charge(1.0)
    with pytest.raises(BudgetExceeded) as info:
        budget.charge(0.5)
    assert info.value.budget_usd == 1.0
    assert info.value.spent_usd == 1.5
    assert budget.spent == 1.5


def test_an_unpriced_run_costs_nothing() -> None:
    budget = Budget(1.0)
    budget.charge(None)
    assert budget.add(None) == 0.0


def test_add_does_not_raise_past_the_cap() -> None:
    budget = Budget(1.0)
    assert budget.add(3.0) == 3.0


def test_check_passes_while_there_is_room() -> None:
    budget = Budget(1.0)
    budget.add(0.5)
    budget.check()
    budget.check(pending=0.5)


def test_check_with_pending_raises_when_the_sum_passes_the_cap() -> None:
    budget = Budget(1.0)
    budget.add(0.5)
    with pytest.raises(BudgetExceeded) as info:
        budget.check(pending=0.6)
    assert info.value.spent_usd == pytest.approx(1.1)


def test_check_without_pending_raises_at_exactly_the_cap() -> None:
    budget = Budget(1.0)
    budget.add(1.0)
    with pytest.raises(BudgetExceeded):
        budget.check()


def test_the_error_states_the_spend_and_exits_with_one() -> None:
    error = BudgetExceeded(5.0, 5.4321)
    assert error.message == "budget of $5.0000 exceeded: $5.4321 spent so far"
    assert error.hint
    assert exit_code(error) == ExitCode.ERROR


def test_concurrent_charges_are_all_counted() -> None:
    budget = Budget(1_000_000.0)

    def spend() -> None:
        for _ in range(1000):
            budget.add(1.0)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert budget.spent == 8000.0
