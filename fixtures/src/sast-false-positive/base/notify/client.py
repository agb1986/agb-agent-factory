"""Requests to the notification service."""

import json
import os
import urllib.request

NOTIFY_URL = "https://notify.example.invalid/v1/messages"


def message_request(recipient: str, text: str) -> urllib.request.Request:
    """Return a POST request that sends `text` to `recipient`."""
    body = json.dumps({"to": recipient, "text": text}).encode("utf-8")
    headers = {
        "X-Api-Token": os.environ["NOTIFY_API_TOKEN"],
        "Content-Type": "application/json",
    }
    return urllib.request.Request(NOTIFY_URL, data=body, headers=headers, method="POST")
