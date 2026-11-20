"""Typed settings read from a flat mapping of strings."""

DEFAULT_TIMEOUT = "30"


def timeout_seconds(config: dict[str, str]) -> int:
    """Return the request timeout in seconds."""
    raw = config["timeout"]
    return int(raw)


def base_url(config: dict[str, str]) -> str:
    """Return the service URL without a trailing slash."""
    return config["base_url"].rstrip("/")
