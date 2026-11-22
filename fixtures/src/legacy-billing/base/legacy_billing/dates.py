"""Days are ISO strings and periods are `YYYY-MM`."""

from datetime import date, timedelta


def parse_day(text: str) -> date:
    """Return the date of an ISO string."""
    return date.fromisoformat(text)


def period_start(period: str) -> date:
    """Return the first day of a period."""
    year, month = period.split("-")
    return date(int(year), int(month), 1)


def period_end(period: str) -> date:
    """Return the last day of a period."""
    first = period_start(next_period(period))
    return first - timedelta(days=1)


def next_period(period: str) -> str:
    """Return the period after `period`."""
    year, month = (int(part) for part in period.split("-"))
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def due_day(issued: str, terms: int) -> str:
    """Return the day an invoice issued on `issued` falls due."""
    return (parse_day(issued) + timedelta(days=terms)).isoformat()


def days_after(day: str, other: str) -> int:
    """Return how many days `other` lies after `day`."""
    return (parse_day(other) - parse_day(day)).days
