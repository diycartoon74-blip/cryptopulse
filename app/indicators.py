"""Technical indicators over plain python lists.

Kept dependency-free (no pandas/numpy) because the dashboard only needs a handful
of overlays and the series are small (a few hundred bars).
"""

from __future__ import annotations

from typing import Optional


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average; leading entries are None until seeded."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's RSI."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[dict], period: int = 14) -> list[Optional[float]]:
    """Average true range; candles use the compact t/o/h/l/c/v shape."""
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) < 2:
        return out
    trs: list[float] = []
    prev_close = float(candles[0]["c"])
    for candle in candles[1:]:
        high, low, close = float(candle["h"]), float(candle["l"]), float(candle["c"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    if len(trs) < period:
        return out
    value = sum(trs[:period]) / period
    out[period] = value
    for i in range(period + 1, len(candles)):
        value = (value * (period - 1) + trs[i - 2]) / period
        out[i] = value
    return out


def vwap(candles: list[dict], anchor_bars: int = 0) -> list[Optional[float]]:
    """Volume-weighted average price.

    anchor_bars > 0 restarts the session at that many bars from the left so the
    intraday overlays stay meaningful on long windows.
    """
    out: list[Optional[float]] = [None] * len(candles)
    start = 0
    if anchor_bars and len(candles) > anchor_bars:
        start = len(candles) - anchor_bars
    pv = vol = 0.0
    for i in range(start, len(candles)):
        candle = candles[i]
        typical = (float(candle["h"]) + float(candle["l"]) + float(candle["c"])) / 3.0
        volume = float(candle.get("v") or 0.0) or 1e-9
        pv += typical * volume
        vol += volume
        out[i] = pv / vol
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd line, signal line, histogram) aligned with `values`.

    The signal line is the EMA of the macd line, so it only exists from the first
    non-None macd value onwards; earlier slots stay None.
    """
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line: list[Optional[float]] = [
        (f - s) if f is not None and s is not None else None for f, s in zip(ema_fast, ema_slow)
    ]
    positions = [i for i, v in enumerate(line) if v is not None]
    compact = [line[i] for i in positions]  # type: ignore[assignment]
    compact_signal = ema(compact, signal)
    out_signal: list[Optional[float]] = [None] * len(values)
    for pos, value in zip(positions, compact_signal):
        out_signal[pos] = value
    hist: list[Optional[float]] = [
        (m - s) if m is not None and s is not None else None for m, s in zip(line, out_signal)
    ]
    return line, out_signal, hist


def percent_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old in (None, 0):
        return None
    return (new / float(old) - 1.0) * 100.0
