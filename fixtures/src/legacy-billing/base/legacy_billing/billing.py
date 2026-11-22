"""Billing: the run, prices, discounts, late fees, credit notes, statements and period close.

This module grew over the years. It shares the dictionaries of `state` with everything else.
"""

from typing import Any

from legacy_billing import catalog, customers, dates, ledger, money, settings, state, storage
from legacy_billing.invoices import Invoice
from legacy_billing.numbering import next_number


class BillingError(Exception):
    """Something about the request does not fit the books."""


class PeriodError(BillingError):
    """The period is closed, or cannot be closed."""


# The billing run


def run_billing(period: str, issued: str) -> int:
    """Bill every active customer for `period` and return the number of invoices."""
    if is_closed(period):
        raise PeriodError(f"{period} is closed")
    state.RUN["period"] = period
    state.RUN["issued"] = issued
    count = 0
    for customer in customers.all_customers():
        if not customer["active"]:
            continue
        if already_billed(customer["id"], period):
            state.WARNINGS.append(f"{customer['id']} was already billed for {period}")
            continue
        bill_customer(customer)
        count += 1
    return count


def already_billed(customer_id: str, period: str) -> bool:
    """Say whether the customer has an invoice for the period."""
    for row in storage.read_rows("invoices"):
        if row["customer"] == customer_id and row["period"] == period:
            return True
    return False


def bill_customer(customer: dict[str, Any]) -> dict[str, Any]:
    """Put together, post and announce the invoice of one customer for the current run."""
    from legacy_billing import notify

    period = state.RUN["period"]
    invoice = Invoice(next_number("INV"), customer["id"], period)
    add_plan_line(invoice, customer)
    price_usage(invoice, customer)
    apply_discounts(invoice, customer)
    add_vat(invoice, customer)
    row = post_invoice(invoice)
    notify.queue_notice(
        customer["id"], "invoice", notify.invoice_text(row["number"], row["total"], row["due"])
    )
    return row


def post_invoice(invoice: Invoice) -> dict[str, Any]:
    """Save the invoice, post it to the ledger and tell every hook."""
    issued = state.RUN["issued"]
    row = invoice.to_row(issued, dates.due_day(issued, settings.PAYMENT_TERMS_DAYS))
    storage.append_row("invoices", row)
    ledger.post_entry(invoice.customer_id, "invoice", invoice.number, invoice.total)
    state.CACHE.setdefault("texts", {})[invoice.number] = invoice.text()
    for hook in state.HOOKS:
        hook(row)
    return row


# Prices


def add_plan_line(invoice: Invoice, customer: dict[str, Any]) -> None:
    """Charge the plan, in part when the customer joined during the period."""
    price = catalog.plan_price(customer["plan"])
    share = prorate(price, customer["joined"], invoice.period)
    if share:
        invoice.add_line("plan", f"Plan {customer['plan']}", share)


def prorate(cents: int, joined: str, period: str) -> int:
    """Return the part of a monthly price that is due for a customer who joined on `joined`."""
    start = dates.period_start(period)
    end = dates.period_end(period)
    day = dates.parse_day(joined)
    if day <= start:
        return cents
    if day > end:
        return 0
    days = (end - start).days + 1
    used = (end - day).days + 1
    return cents * used // days


def prorate_by_weeks(cents: int, joined: str, period: str) -> int:
    """Return the part of a monthly price that is due, counted in started weeks."""
    end = dates.period_end(period)
    used = (end - dates.parse_day(joined)).days + 1
    weeks = min(4, (used + 6) // 7)
    return cents * weeks // 4


def price_usage(invoice: Invoice, customer: dict[str, Any]) -> None:
    """Charge the metered usage of the period."""
    from legacy_billing import usage

    totals = usage.usage_for(customer["id"], invoice.period)
    for meter in sorted(totals):
        cents = totals[meter] * catalog.unit_price(meter)
        invoice.add_line("usage", f"{totals[meter]} x {meter}", cents)


def add_vat(invoice: Invoice, customer: dict[str, Any]) -> None:
    """Set the VAT of the invoice; a customer with a VAT number abroad pays none."""
    from legacy_billing import taxes

    if customer.get("vat_number") and customer["country"] != settings.HOME_COUNTRY:
        invoice.vat = 0
        return
    invoice.vat = taxes.vat_amount(invoice.subtotal(), customer["country"])


# Discounts


def apply_discounts(invoice: Invoice, customer: dict[str, Any]) -> None:
    """Add the best discount the customer has; discounts do not add up."""
    charges = invoice.charges()
    promo = promo_discount(customer, charges)
    loyalty = loyalty_discount(customer, charges, invoice.period)
    if promo == 0 and loyalty == 0:
        return
    if promo >= loyalty:
        invoice.add_line("discount", f"Promotion {customer['promo']}", -promo)
        count_promo_use(customer)
    else:
        invoice.add_line("discount", "Loyalty", -loyalty)


def promo_discount(customer: dict[str, Any], charges: int) -> int:
    """Return the promotion discount in cents, 0 when the code is unknown or used up."""
    code = customer.get("promo")
    if not code:
        return 0
    promo = catalog.find_promo(code)
    if promo is None:
        state.WARNINGS.append(f"{customer['id']} has the unknown code {code}")
        return 0
    if promo_uses(customer["id"], code) >= promo["limit"]:
        return 0
    return money.percent_of(charges, promo["percent"])


def loyalty_discount(customer: dict[str, Any], charges: int, period: str) -> int:
    """Return the discount of a customer who has been with us long enough."""
    if customers.months_active(customer, period) < settings.LOYALTY_MONTHS:
        return 0
    return money.percent_of(charges, settings.LOYALTY_PERCENT)


def promo_uses(customer_id: str, code: str) -> int:
    """Return how often a customer has used a promotion code."""
    for row in storage.read_rows("promo_uses"):
        if row["key"] == f"{customer_id}/{code}":
            return row["uses"]
    return 0


def count_promo_use(customer: dict[str, Any]) -> None:
    """Count one more use of the customer's promotion code."""
    key = f"{customer['id']}/{customer['promo']}"
    uses = promo_uses(customer["id"], customer["promo"]) + 1
    storage.replace_row("promo_uses", "key", {"key": key, "uses": uses})
    state.RUN["promos"] = state.RUN.get("promos", 0) + 1


# Late fees


def apply_late_fees(today: str) -> int:
    """Charge a fee on every overdue invoice that has none yet and return how many there were."""
    from legacy_billing import notify

    charged = 0
    for row in overdue_invoices(today):
        days = dates.days_after(row["due"], today)
        fee = late_fee_for(row)
        row["late_fee"] = fee
        storage.replace_row("invoices", "number", row)
        ledger.post_entry(row["customer"], "late_fee", row["number"], fee)
        notify.queue_notice(
            row["customer"], "late_fee", notify.late_fee_text(row["number"], fee, days)
        )
        charged += 1
    state.RUN["late_fees"] = state.RUN.get("late_fees", 0) + charged
    return charged


def overdue_invoices(today: str) -> list[dict[str, Any]]:
    """Return the unpaid invoices without a fee that are past due and past the grace days."""
    rows = []
    for row in storage.read_rows("invoices"):
        if row["late_fee"] or row["paid"] >= row["total"]:
            continue
        if dates.days_after(row["due"], today) > settings.LATE_FEE_GRACE_DAYS:
            rows.append(row)
    return rows


def late_fee_for(row: dict[str, Any]) -> int:
    """Return the fee on one invoice: a share of what is open, with a minimum."""
    owed = row["total"] - row["paid"]
    share = money.percent_of(owed, settings.LATE_FEE_PERCENT)
    return max(share, settings.LATE_FEE_FLAT_CENTS)


# Credit notes


def find_invoice(number: str) -> dict[str, Any]:
    """Return the saved row of an invoice."""
    for row in storage.read_rows("invoices"):
        if row["number"] == number:
            return row
    raise BillingError(f"no invoice {number}")


def issue_credit_note(number: str, cents: int) -> str:
    """Give back part of an invoice and return the number of the credit note."""
    row = find_invoice(number)
    if cents > row["total"]:
        raise BillingError(f"{number} is smaller than the credit")
    note = next_number("CRN")
    storage.append_row("credits", {"number": note, "invoice": number, "cents": cents})
    ledger.post_entry(row["customer"], "credit", note, -cents)
    return note


# Statements and exports


def customer_statement(customer_id: str) -> str:
    """Return every posting of a customer, with the balance after each."""
    customer = customers.get_customer(customer_id)
    lines = [f"Statement for {customer['name']} ({settings.CURRENCY})"]
    running = 0
    for entry in ledger.entries(customer_id):
        running += entry["cents"]
        lines.append(statement_line(entry, running))
    return "\n".join(lines)


def statement_line(entry: dict[str, Any], running: int) -> str:
    """Return one line of a statement."""
    amount = money.format_cents(entry["cents"]).rjust(10)
    return f"{entry['kind']:<9}{entry['reference']:<10}{amount}{money.format_cents(running):>12}"


def export_ledger(fmt: str) -> str:
    """Return the whole ledger in the format `fmt`."""
    from legacy_billing import exporters

    writer = getattr(exporters, "export_" + fmt, None)
    if writer is None:
        raise BillingError(f"no export format {fmt}")
    return writer(ledger.entries())


# Period close


def is_closed(period: str) -> bool:
    """Say whether a period has been closed."""
    return any(row["period"] == period for row in storage.read_rows("closed"))


def close_period(period: str) -> None:
    """Close a period once every active customer has been billed for it."""
    if is_closed(period):
        raise PeriodError(f"{period} is already closed")
    for customer in customers.all_customers():
        if customer["active"] and not already_billed(customer["id"], period):
            raise PeriodError(f"{customer['id']} has no invoice for {period}")
    storage.append_row("closed", {"period": period, "next": dates.next_period(period)})
