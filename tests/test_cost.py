import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import cost  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize(
    "model,input_,read,write_1h,write_5m",
    [
        ("claude-fable-5-1", 10.0, 0.25, 20.0, 10.0),
        ("claude-fable-5", 10.0, 0.25, 20.0, 10.0),
        ("claude-opus-5", 5.0, 0.50, 10.0, 5.0),
        ("claude-sonnet-5", 2.0, 0.20, 4.0, 2.0),
        ("claude-sonnet-4-6", 3.0, 0.30, 6.0, 3.0),
        ("claude-haiku-4-5", 1.0, 0.10, 2.0, 1.0),
    ],
)
def test_price_exact_ids(model, input_, read, write_1h, write_5m):
    assert cost.price(model) == (input_, read, write_1h, write_5m)


def test_price_prefix_match_on_dated_model_id():
    assert cost.price("claude-opus-5-20260101") == (5.0, 0.50, 10.0, 5.0)
    assert cost.price("claude-haiku-4-5-20251001") == (1.0, 0.10, 2.0, 1.0)


def test_longer_prefix_wins():
    # fable-5-1 must not match the fable-5 entry
    assert cost.price("claude-fable-5-1")[0] == 10.0
    assert cost.is_assumed("claude-fable-5-1") is False


def test_unknown_model_gets_opus_prices_and_assumed():
    assert cost.price("gpt-99") == (5.0, 0.50, 10.0, 5.0)
    assert cost.price("") == (5.0, 0.50, 10.0, 5.0)
    assert cost.price(None) == (5.0, 0.50, 10.0, 5.0)
    assert cost.is_assumed("gpt-99") is True
    assert cost.is_assumed("claude-sonnet-5") is False


def test_cold_return_1h():
    # 400k tokens on sonnet-5, 1h write price 4 $/MTok
    assert cost.cold_return_usd(400000, "claude-sonnet-5", 3600) == pytest.approx(1.60)


def test_cold_return_5m():
    # 400k tokens on sonnet-5, 5m write price 2 $/MTok
    assert cost.cold_return_usd(400000, "claude-sonnet-5", 300) == pytest.approx(0.80)


def test_cold_return_zero_tokens():
    assert cost.cold_return_usd(0, "claude-sonnet-5", 3600) == 0.0
    assert cost.cold_return_usd(None, "claude-sonnet-5", 3600) == 0.0


def test_warm_compact_sonnet5():
    # 400k * 0.20/1e6 + 3000 * 10/1e6 = 0.08 + 0.03
    assert cost.warm_compact_usd(400000, "claude-sonnet-5") == pytest.approx(0.11)


def test_warm_compact_output_prices_differ_per_family():
    base = 1000000.0  # 1 MTok read
    assert cost.warm_compact_usd(base, "claude-fable-5") == pytest.approx(0.25 + 0.150)
    assert cost.warm_compact_usd(base, "claude-opus-5") == pytest.approx(0.50 + 0.075)
    assert cost.warm_compact_usd(base, "claude-sonnet-5") == pytest.approx(0.20 + 0.030)
    assert cost.warm_compact_usd(base, "claude-sonnet-4-6") == pytest.approx(0.30 + 0.045)
    assert cost.warm_compact_usd(base, "claude-haiku-4-5") == pytest.approx(0.10 + 0.015)


def test_unknown_model_uses_opus_output_price():
    # 0.50 read + 3000 * 25/1e6
    assert cost.warm_compact_usd(1000000, "mystery") == pytest.approx(0.50 + 0.075)
