"""OHLC candles per coin per timeframe.

A coin with a Binance USDT pair gets real candles and a one-second tape. Coins
without one fall back to CoinGecko's OHLC window, which is coarse but enough for
the slower timeframes; the response marks which feed was used so the UI can say
so instead of pretending.
"""

from __future__ import annotations

import time
from typing import Optional

from .config import BARS_PER_TF, GECKO_MAX_DAYS, TIMEFRAMES
from .indicators import atr, ema, rsi, vwap

# CoinGecko's /ohlc granularity depends on `days`, so pick the window that keeps
# the bars at least as fine as the requested timeframe.
GECKO_DAYS_FOR_TF = {
    "1m": 1, "5m": 1, "15m": 3, "30m": 7, "1h": 14,
    "4h": 30, "1d": 180, "1w": 365, "1M": 365, "1y": GECKO_MAX_DAYS,
}

GECKO_MINUTES = {1: 30, 3: 120, 7: 360, 14: 720, 30: 1440, 180: 7 * 1440, 365: 7 * 1440}


def _bucket_ms(tf: str, ts_ms: int) -> int:
    """Floor a timestamp to its bar, in UTC."""
    tf_cfg = TIMEFRAMES.get(tf) or {}
    minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
        "1d": 1440, "1w": 10080, "1M": 43200, "1y": 525600,
    }.get(tf, int(tf_cfg.get("binance") and 5) or 5)
    if tf in {"1M", "1y"}:
        # Calendar buckets: snap to the first of the month / year.
        stamp = time.gmtime(ts_ms / 1000)
        if tf == "1M":
            return int(time.mktime((stamp.tm_year, stamp.tm_mon, 1, 0, 0, 0, 0, 0, 0)) * 1000)
        return int(time.mktime((stamp.tm_year, 1, 1, 0, 0, 0, 0, 0, 0)) * 1000)
    return (ts_ms // (minutes * 60_000)) * (minutes * 60_000)


def _candle_from_ohlc(row: list) -> Optional[dict]:
    """CoinGecko rows are [ms, open, high, low, close]."""
    try:
        ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
    except (IndexError, TypeError):
        return None
    if None in (ts, o, h, l, c):
        return None
    return {
        "t": int(ts),
        "o": float(o), "h": float(h), "l": float(l), "c": float(c),
        "v": 0.0,
    }


def _candle_from_kline(row: list) -> Optional[dict]:
    """Binance kline: [openTime, open, high, low, close, vol, closeTime, ...]."""
    try:
        return {
            "t": int(row[0]),
            "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]),
            "v": float(row[5]),
        }
    except (IndexError, TypeError, ValueError):
        return None


class ChartService:
    def __init__(self, catalogue, settings) -> None:
        self.catalogue = catalogue
        self.settings = settings
        self.gecko = catalogue.gecko
        self.binance = catalogue.binance
        self._cache: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------ main
    def get(self, coin_id: str, tf: str) -> dict:
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe: {tf}")
        key = f"{coin_id}:{tf}"
        ttl = TIMEFRAMES[tf]["ttl"]
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]

        row = self.catalogue.get(coin_id) or {}
        pair = row.get("pair")
        result: Optional[dict] = None
        if pair:
            result = self._from_binance(coin_id, tf, pair)
        if result is None:
            result = self._from_gecko(coin_id, tf, row)
        if result is None:
            if hit:  # stale beats empty
                return hit[1]
            raise RuntimeError(f"no chart data for {coin_id} ({tf})")

        self._cache[key] = (time.time(), result)
        return result

    def _from_binance(self, coin_id: str, tf: str, pair: str) -> Optional[dict]:
        interval = TIMEFRAMES[tf]["binance"]
        limit = BARS_PER_TF.get(tf, 200)
        if interval in {"1M", "1y"}:
            # The public kline endpoint uses "1M" for months and has no yearly
            # bars, so a year is 12 month closes stitched from monthly candles.
            limit = 12 if interval == "1y" else limit
            interval = "1M"
        payload = self.binance.get(
            "api/v3/klines",
            {"symbol": pair, "interval": interval, "limit": limit},
            ttl=0,
            cache_key=f"kline:{pair}:{interval}:{limit}",
        )
        if not isinstance(payload, list) or not payload:
            return None
        candles = [c for c in (_candle_from_kline(row) for row in payload) if c]
        if len(candles) < 5:
            return None
        # Binance returns the in-progress bar last; keep it so the tape can update it.
        return self._finish(coin_id, tf, candles, source="binance", pair=pair, live=True)

    def _from_gecko(self, coin_id: str, tf: str, row: dict) -> Optional[dict]:
        days = GECKO_DAYS_FOR_TF.get(tf, 30)
        payload = self.gecko.get(
            f"coins/{coin_id}/ohlc",
            {"vs_currency": self.settings.vs_currency, "days": min(days, GECKO_MAX_DAYS)},
            ttl=max(30, TIMEFRAMES[tf]["ttl"]),
            cache_key=f"ohlc:{coin_id}:{days}",
        )
        if not isinstance(payload, list) or not payload:
            return None
        candles = [c for c in (_candle_from_ohlc(entry) for entry in payload) if c]
        if len(candles) < 5:
            return None
        wanted = BARS_PER_TF.get(tf, 200)
        candles = self._resample(candles, tf, wanted)
        # CoinGecko only quotes USD, while Binance klines are already USDT-quoted,
        # so this is the one chart path that has to restate its OHLC. Volume is a
        # base-asset count and stays as it is.
        scale = self.catalogue.to_display
        candles = [
            dict(c, o=scale(c["o"]), h=scale(c["h"]), l=scale(c["l"]), c=scale(c["c"]))
            for c in candles
        ]
        return self._finish(coin_id, tf, candles, source="coingecko", pair=None, live=False)

    def _resample(self, candles: list[dict], tf: str, wanted: int) -> list[dict]:
        """Roll coarse CoinGecko bars into the requested timeframe."""
        bucketed: dict[int, dict] = {}
        for candle in candles:
            bucket = _bucket_ms(tf, candle["t"])
            current = bucketed.get(bucket)
            if current is None:
                bucketed[bucket] = dict(candle, t=bucket)
                continue
            current["h"] = max(current["h"], candle["h"])
            current["l"] = min(current["l"], candle["l"])
            current["c"] = candle["c"]
            current["v"] += candle["v"]
        merged = sorted(bucketed.values(), key=lambda c: c["t"])
        return merged[-wanted:]

    def _finish(self, coin_id, tf, candles, source, pair, live) -> dict:
        closes = [c["c"] for c in candles]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        session_bars = {"1m": 240, "5m": 240, "15m": 96, "30m": 48, "1h": 24}.get(tf, 0)
        result = {
            "coin": coin_id,
            "tf": tf,
            "candles": candles,
            "indicators": {
                "ema20": ema20,
                "ema50": ema50,
                "rsi": rsi(closes, 14),
                "atr": atr(candles, 14),
                "vwap": vwap(candles, session_bars),
            },
            "meta": {
                "source": source,
                "pair": pair,
                "live": bool(live and source == "binance"),
                "bars": len(candles),
                "bucketMs": _bucket_ms(tf, candles[-1]["t"]) - _bucket_ms(tf, candles[-2]["t"])
                if len(candles) > 1 else 0,
                "lastPrice": closes[-1],
                "lastBarAt": candles[-1]["t"],
                "fetchedAt": time.time(),
            },
        }
        return result

    # ------------------------------------------------------------------ tape
    def tick(self, coin_id: str, tf: str) -> Optional[dict]:
        """Latest print + forming candle for the focused coin (Binance only)."""
        row = self.catalogue.get(coin_id) or {}
        pair = row.get("pair")
        if not pair or tf not in TIMEFRAMES:
            return None
        interval = TIMEFRAMES[tf]["binance"]
        if interval in {"1M", "1y"}:
            interval = "1M"
        payload = self.binance.get(
            "api/v3/klines",
            {"symbol": pair, "interval": interval, "limit": 2},
            ttl=0,
            cache_key=f"tick:{pair}:{interval}",
        )
        if not isinstance(payload, list) or not payload:
            return None
        candle = _candle_from_kline(payload[-1])
        if not candle:
            return None
        return {
            "coin": coin_id,
            "tf": tf,
            "pair": pair,
            "candle": candle,
            "price": candle["c"],
            "ts": int(time.time() * 1000),
            "source": "binance",
        }

    def majors(self, pairs: list[str]) -> list[dict]:
        """24h stats for the header ticker strip, one Binance call."""
        wanted = [p for p in pairs if p][:20]
        if not wanted:
            return []
        payload = self.binance.get(
            "api/v3/ticker/24hr",
            {"symbols": json_dumps_symbols(wanted)},
            ttl=self.settings.majors_refresh_seconds,
            cache_key="majors:" + ",".join(sorted(wanted)),
        )
        if not isinstance(payload, list):
            return []
        out = []
        for entry in payload:
            out.append(
                {
                    "pair": str(entry.get("symbol")),
                    "price": _f(entry.get("lastPrice")),
                    "chg24h": _f(entry.get("priceChangePercent")),
                    "high": _f(entry.get("highPrice")),
                    "low": _f(entry.get("lowPrice")),
                    "vol": _f(entry.get("quoteVolume")),
                }
            )
        return out


def json_dumps_symbols(symbols: list[str]) -> str:
    """Binance wants the symbols array URL-encoded as JSON."""
    inner = ",".join(f'"{s}"' for s in symbols)
    return "[" + inner + "]"


def _f(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
