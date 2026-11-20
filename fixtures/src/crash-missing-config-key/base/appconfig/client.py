"""The HTTP client settings a service call uses."""

from dataclasses import dataclass

from appconfig.settings import base_url, timeout_seconds


@dataclass(frozen=True)
class Client:
    url: str
    timeout: int


def make_client(config: dict[str, str]) -> Client:
    """Build a client from the settings mapping."""
    return Client(url=base_url(config), timeout=timeout_seconds(config))
