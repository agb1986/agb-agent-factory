from payments.client import charge_request


def test_charge_request_carries_the_amount_and_the_token() -> None:
    request = charge_request(1250, "EUR", "tok")
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.data == b'{"amount": 1250, "currency": "EUR"}'
    assert request.full_url == "https://payments.example.invalid/v1/charges"
