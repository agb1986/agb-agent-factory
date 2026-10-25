"""Webhook delivery."""

import json
import urllib.request


def build_request(url: str, payload: dict[str, object], token: str) -> urllib.request.Request:
    """Return a POST request that carries `payload` as JSON and authenticates with `token`."""
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )


def send(url: str, payload: dict[str, object], token: str) -> int:
    """Deliver `payload` to `url` and return the HTTP status code."""
    request = build_request(url, payload, token)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status
