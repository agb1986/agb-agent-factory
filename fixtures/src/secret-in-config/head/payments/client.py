"""Requests to the payments API."""

import json
import urllib.request

from payments import config


def charge_request(amount_cents: int, currency: str, token: str) -> urllib.request.Request:
    """Return a POST request that charges `amount_cents` in `currency`, authenticated by `token`."""
    body = json.dumps({"amount": amount_cents, "currency": currency}).encode("utf-8")
    return urllib.request.Request(
        f"{config.PAYMENTS_API_URL}/charges",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )


def default_charge_request(amount_cents: int, currency: str) -> urllib.request.Request:
    """Return a charge request authenticated with the configured API token."""
    return charge_request(amount_cents, currency, config.PAYMENTS_API_TOKEN)
