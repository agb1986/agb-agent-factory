from webhooks.sender import build_request


def test_request_carries_the_payload_and_the_token() -> None:
    request = build_request("https://hooks.example.invalid/x", {"a": 1}, "tok")
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.data == b'{"a": 1}'
