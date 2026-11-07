from notify.client import backoff_delays, message_request


def test_message_request_carries_the_text_and_the_token(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_API_TOKEN", "tok")
    request = message_request("ops", "deployed")
    assert request.get_header("X-api-token") == "tok"
    assert request.data == b'{"to": "ops", "text": "deployed"}'


def test_backoff_doubles_and_jitter_stays_under_the_base() -> None:
    delays = backoff_delays(3, base_s=1.0)
    assert len(delays) == 3
    for attempt, delay in enumerate(delays):
        assert 2**attempt <= delay <= 2**attempt + 1.0
