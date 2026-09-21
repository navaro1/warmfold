"""Token price tables and cost estimates.

Prices are USD per million tokens as (input, cache_read, write_1h, write_5m).
Matching is prefix-based on the model id, longest prefix first. An unknown
model gets opus-5 prices and ``is_assumed`` reports True.
"""

# (input, cache_read, write_1h, write_5m), $/MTok
_PRICES = {
    "claude-fable-5-1": (10.0, 0.25, 20.0, 10.0),
    "claude-fable-5": (10.0, 0.25, 20.0, 10.0),
    "claude-opus-5": (5.0, 0.50, 10.0, 5.0),
    "claude-opus-4-8": (5.0, 0.50, 10.0, 5.0),
    "claude-opus-4-7": (5.0, 0.50, 10.0, 5.0),
    "claude-opus-4-6": (5.0, 0.50, 10.0, 5.0),
    "claude-sonnet-5": (2.0, 0.20, 4.0, 2.0),
    "claude-sonnet-4-6": (3.0, 0.30, 6.0, 3.0),
    "claude-haiku-4-5": (1.0, 0.10, 2.0, 1.0),
}

# USD per million output tokens, used for the compaction summary.
_OUTPUT_PRICES = {
    "claude-fable-5-1": 50.0,
    "claude-fable-5": 50.0,
    "claude-opus-5": 25.0,
    "claude-opus-4-8": 25.0,
    "claude-opus-4-7": 25.0,
    "claude-opus-4-6": 25.0,
    "claude-sonnet-5": 10.0,
    "claude-sonnet-4-6": 15.0,
    "claude-haiku-4-5": 5.0,
}

_SUMMARY_OUTPUT_TOKENS = 3000.0

# Longest prefixes first so that e.g. claude-fable-5-1 wins over claude-fable-5.
_PREFIXES = sorted(_PRICES, key=len, reverse=True)
_OUTPUT_PREFIXES = sorted(_OUTPUT_PRICES, key=len, reverse=True)


def _match(table, prefixes, model):
    text = str(model or "")
    for prefix in prefixes:
        if text.startswith(prefix):
            return table[prefix], False
    return table["claude-opus-5"], True


def price(model):
    """Return (input, cache_read, write_1h, write_5m) in $/MTok."""
    return _match(_PRICES, _PREFIXES, model)[0]


def is_assumed(model):
    """True when the model id is unknown and opus-5 prices were assumed."""
    return _match(_PRICES, _PREFIXES, model)[1]


def output_price(model):
    """Return the output price in $/MTok used for the summary tokens."""
    return _match(_OUTPUT_PRICES, _OUTPUT_PREFIXES, model)[0]


def cold_return_usd(tokens, model, ttl):
    """Cost of the first request on a cold cache: the cache is rebuilt."""
    if not tokens:
        return 0.0
    _input, _read, write_1h, write_5m = price(model)
    rate = write_1h if ttl == 3600 else write_5m
    return (float(tokens) / 1e6) * rate


def warm_compact_usd(tokens, model):
    """Cost of a warm compaction: cache-read the prefix, write a summary."""
    if not tokens:
        return 0.0
    _input, cache_read, _w1h, _w5m = price(model)
    read_cost = (float(tokens) / 1e6) * cache_read
    summary_cost = (_SUMMARY_OUTPUT_TOKENS / 1e6) * output_price(model)
    return read_cost + summary_cost
