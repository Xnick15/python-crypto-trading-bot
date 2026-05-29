
from typing import List


def closes_from_candles(candles: List[List[float]]) -> List[float]:
    return [float(c[4]) for c in candles]


def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for price in values[period:]:
        out.append((price - out[-1]) * multiplier + out[-1])
    return [out[0]] * (period - 1) + out


def sma(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    out = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(values[i])
        else:
            w = values[i + 1 - period:i + 1]
            out.append(sum(w) / period)
    return out


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    out = [50.0] * period
    out.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        out.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))

    while len(out) < len(values):
        out.insert(0, 50.0)
    return out


def pct_change(cur: float, prev: float) -> float:
    return 0.0 if prev == 0 else ((cur - prev) / prev) * 100.0
