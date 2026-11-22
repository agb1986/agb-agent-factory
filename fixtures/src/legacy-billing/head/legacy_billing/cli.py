"""The command line."""

import argparse
import sys
from collections.abc import Sequence

from legacy_billing import storage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legacy-billing")
    parser.add_argument("--data", default="data", help="directory of the tables")
    commands = parser.add_subparsers(dest="command", required=True)

    bill = commands.add_parser("bill", help="bill every customer for a period")
    bill.add_argument("period")
    bill.add_argument("--issued", required=True)

    late = commands.add_parser("late-fees", help="charge late fees")
    late.add_argument("today")

    pay = commands.add_parser("pay", help="record a payment")
    pay.add_argument("customer")
    pay.add_argument("cents", type=int)
    pay.add_argument("reference")

    credit = commands.add_parser("credit", help="issue a credit note")
    credit.add_argument("invoice")
    credit.add_argument("cents", type=int)

    add = commands.add_parser("add-customer", help="add or replace a customer")
    add.add_argument("customer")
    add.add_argument("--name", required=True)
    add.add_argument("--email", required=True)
    add.add_argument("--plan", default="basic")
    add.add_argument("--country", default="IE")
    add.add_argument("--joined", required=True)
    add.add_argument("--promo", default="")

    readings = commands.add_parser("import-usage", help="import a usage file")
    readings.add_argument("path")

    report = commands.add_parser("report", help="print a report")
    report.add_argument("name")

    export = commands.add_parser("export", help="export the ledger")
    export.add_argument("format")

    statement = commands.add_parser("statement", help="print a customer statement")
    statement.add_argument("customer")

    close = commands.add_parser("close", help="close a period")
    close.add_argument("period")
    return parser


def _add_customer(args: argparse.Namespace) -> None:
    from legacy_billing import customers

    customers.add_customer(
        {
            "id": args.customer,
            "name": args.name,
            "email": args.email,
            "plan": args.plan,
            "country": args.country,
            "joined": args.joined,
            "promo": args.promo,
            "active": True,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return the exit status."""
    from legacy_billing import audit, billing

    args = _parser().parse_args(argv)
    storage.open_store(args.data)
    audit.install()
    if args.command == "bill":
        print(f"{billing.run_billing(args.period, args.issued)} invoices")
    elif args.command == "late-fees":
        print(f"{billing.apply_late_fees(args.today)} late fees")
    elif args.command == "pay":
        from legacy_billing import payments

        left = payments.record_payment(args.customer, args.cents, args.reference)
        print(f"{left} left over")
    elif args.command == "credit":
        print(billing.issue_credit_note(args.invoice, args.cents))
    elif args.command == "add-customer":
        _add_customer(args)
    elif args.command == "import-usage":
        from legacy_billing import importer

        print(f"{importer.import_usage(args.path)} readings")
    elif args.command == "report":
        from legacy_billing import reports

        print(reports.run_report(args.name))
    elif args.command == "export":
        print(billing.export_ledger(args.format))
    elif args.command == "statement":
        print(billing.customer_statement(args.customer))
    else:
        try:
            billing.close_period(args.period)
        except billing.PeriodError as exc:
            print(exc, file=sys.stderr)
            return 1
    return 0
