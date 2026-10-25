"""Tests for runtime/pricing.py."""

import logging

import pytest

from runtime import pricing
from runtime.pricing import PRICES, TokenUsage, cost_usd


@pytest.fixture(autouse=True)
def _reset_warned() -> None:
    pricing._warned.clear()


def test_every_required_model_has_a_price() -> None:
    for model in ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        assert model in PRICES


def test_cost_adds_all_four_token_kinds() -> None:
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(2.0 + 10.0 + 0.20 + 2.5)


def test_none_tokens_count_as_zero() -> None:
    assert cost_usd("claude-opus-5-5", TokenUsage(output_tokens=500_000)) == pytest.approx(10.0)
    assert cost_usd("claude-opus-5-5", TokenUsage()) == 0.0


def test_unknown_model_is_none_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="runtime.pricing"):
        assert cost_usd("claude-nope", TokenUsage(input_tokens=10)) is None
        assert cost_usd("claude-nope", TokenUsage(input_tokens=10)) is None
    assert [r.getMessage() for r in caplog.records].count(
        "no price for model 'claude-nope'; cost_usd will be null"
    ) == 1
    assert len(caplog.records) == 1


def test_each_unknown_model_warns_separately(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="runtime.pricing"):
        cost_usd("model-a", TokenUsage())
        cost_usd("model-b", TokenUsage())
    assert len(caplog.records) == 2


def test_prices_are_read_only() -> None:
    with pytest.raises(TypeError):
        PRICES["claude-opus-5-5"] = PRICES["claude-haiku-5-5"]  # ty: ignore[invalid-assignment]
