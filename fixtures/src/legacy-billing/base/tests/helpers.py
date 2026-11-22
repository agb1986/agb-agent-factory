"""A customer row the tests start from."""

from typing import Any


def customer(customer_id: str, **fields: Any) -> dict[str, Any]:
    """Return a customer on the basic plan; `fields` replace single columns."""
    row: dict[str, Any] = {
        "id": customer_id,
        "name": customer_id.upper(),
        "email": f"{customer_id}@example.test",
        "plan": "basic",
        "country": "IE",
        "joined": "2024-01-01",
        "active": True,
    }
    row.update(fields)
    return row
