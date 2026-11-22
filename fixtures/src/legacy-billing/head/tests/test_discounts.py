from pathlib import Path

from helpers import customer
from legacy_billing import billing, customers, state, storage


def _lines() -> list[dict[str, object]]:
    return storage.read_rows("invoices")[-1]["lines"]


def test_promotion_lowers_the_invoice(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01", promo="WELCOME10"))
    billing.run_billing("2024-03", "2024-03-31")
    assert _lines()[-1] == {"kind": "discount", "text": "Promotion WELCOME10", "cents": -100}
    assert storage.read_rows("invoices")[0]["total"] == 1107


def test_promotion_is_used_up(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01", promo="WELCOME10"))
    billing.run_billing("2024-03", "2024-03-31")
    billing.run_billing("2024-04", "2024-04-30")
    assert [line["kind"] for line in _lines()] == ["plan"]
    assert state.RUN["promos"] == 1


def test_unknown_code_is_a_warning(store: Path) -> None:
    row = customer("ann", joined="2024-03-01", promo="NOPE")
    assert billing.promo_discount(row, 1000) == 0
    assert state.WARNINGS == ["ann has the unknown code NOPE"]


def test_loyalty_starts_after_a_year(store: Path) -> None:
    row = customer("ann", joined="2023-04-10")
    assert billing.loyalty_discount(row, 1000, "2024-03") == 0
    assert billing.loyalty_discount(row, 1000, "2024-04") == 50


def test_only_the_best_discount_applies(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2020-01-01", promo="PARTNER20"))
    customers.add_customer(customer("bob", joined="2020-01-01", promo="NOPE"))
    billing.run_billing("2024-03", "2024-03-31")
    invoices = storage.read_rows("invoices")
    assert [row["lines"][-1]["text"] for row in invoices] == ["Promotion PARTNER20", "Loyalty"]
