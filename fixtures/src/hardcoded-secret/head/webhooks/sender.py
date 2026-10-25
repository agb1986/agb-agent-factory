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


DEPLOY_HOOK_URL = "https://hooks.example.invalid/deploy"
DEPLOY_HOOK_TOKEN = "whk_4f9a1c7e2b8d4036a5e1c9d27b60f3a8"


def notify_deploy(service: str, version: str) -> int:
    """Tell the deploy channel that `service` now runs `version`."""
    return send(DEPLOY_HOOK_URL, {"service": service, "version": version}, DEPLOY_HOOK_TOKEN)
