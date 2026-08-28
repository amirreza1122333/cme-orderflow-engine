"""Indicator maths. Pure functions over plain lists so they are easy to test.

Each returns None when there is not enough history, which the strategy treats
as "no opinion" rather than a neutral reading - a half-warmed EMA is worse
than no EMA.
"""

from __future__ import annotations


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period  # SMA seed
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):  # Wilder smoothing
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> float | None:
    if min(len(highs), len(lows), len(closes)) < period + 1:
        return None
    true_ranges = []
    for index in range(1, len(closes)):
        previous_close = closes[index - 1]
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - previous_close),
                abs(lows[index] - previous_close),
            )
        )
    if len(true_ranges) < period:
        return None
    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance ** 0.5


def zscore(values: list[float]) -> float | None:
    """How far the latest value sits from the mean, in standard deviations."""
    if len(values) < 20:
        return None
    deviation = stdev(values)
    if deviation == 0:
        return 0.0
    mean = sum(values) / len(values)
    return (values[-1] - mean) / deviation
