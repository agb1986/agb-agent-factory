"""Requests to the notification service."""

import json
import os
import random
import urllib.request

NOTIFY_URL = "https://notify.example.invalid/v1/messages"
TOKEN_HEADER = "X-Api-Token"
TOKEN_ENV = "NOTIFY_API_TOKEN"


def auth_headers() -> dict[str, str]:
    """Return the headers that authenticate a request, with the token read from the environment."""
    return {TOKEN_HEADER: os.environ[TOKEN_ENV], "Content-Type": "application/json"}


def message_request(recipient: str, text: str) -> urllib.request.Request:
    """Return a POST request that sends `text` to `recipient`."""
    body = json.dumps({"to": recipient, "text": text}).encode("utf-8")
    return urllib.request.Request(NOTIFY_URL, data=body, headers=auth_headers(), method="POST")


def backoff_delays(attempts: int, base_s: float = 0.5) -> list[float]:
    """Return the wait before each retry: doubling, plus jitter so clients do not retry in step."""
    return [base_s * 2**i + random.uniform(0, base_s) for i in range(attempts)]
