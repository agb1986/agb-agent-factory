from webhooks.sender import build_request


def test_request_carries_the_payload_and_the_token() -> None:
    request = build_request("https://hooks.example.invalid/x", {"a": 1}, "tok")
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.data == b'{"a": 1}'


def test_deploy_notice_goes_to_the_deploy_hook(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr("webhooks.sender.send", lambda url, payload, token: sent.append(url) or 200)
    from webhooks.sender import notify_deploy

    assert notify_deploy("api", "1.2.3") == 200
    assert sent == ["https://hooks.example.invalid/deploy"]
