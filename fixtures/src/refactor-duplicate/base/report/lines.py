"""Format the lines of an invoice."""


def invoice_line(name: str, cents: int) -> str:
    """Return `name: E.CC EUR` for a charge of `cents`, which must not be negative."""
    if cents < 0:
        raise ValueError(f"negative amount for {name}: {cents}")
    euros, rest = divmod(cents, 100)
    return f"{name}: {euros}.{rest:02d} EUR"
