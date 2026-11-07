from notify.client import message_request


def test_message_request_carries_the_text_and_the_token(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_API_TOKEN", "tok")
    request = message_request("ops", "deployed")
    assert request.get_header("X-api-token") == "tok"
    assert request.data == b'{"to": "ops", "text": "deployed"}'
