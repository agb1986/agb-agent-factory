"""Parse money amounts."""

import warnings


def parse_amount(text: str) -> int:
    """Return the amount in `text`, such as `12.34`, in cents. Raises `ValueError` if malformed."""
    euros, _, cents = text.strip().partition(".")
    if not euros.isdigit() or (cents and not (cents.isdigit() and len(cents) == 2)):
        raise ValueError(f"not an amount: {text!r}")
    return int(euros) * 100 + int(cents or "0")


def to_cents(text: str) -> int:
    """Deprecated since 2.0: use `parse_amount`, which this calls. Kept for old callers."""
    warnings.warn("to_cents is deprecated; use parse_amount", DeprecationWarning, stacklevel=2)
    return parse_amount(text)
