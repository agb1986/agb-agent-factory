from appconfig.client import make_client


def test_client_reads_both_settings() -> None:
    client = make_client({"base_url": "https://api.example.invalid/", "timeout": "5"})
    assert client.url == "https://api.example.invalid"
    assert client.timeout == 5
