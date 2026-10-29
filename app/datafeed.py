"""Rate-limited HTTP plumbing shared by every feed.

CoinGecko's free tier throttles hard (roughly 10-30 requests/min) while Binance's
public market data is much more generous, so each host gets its own minimum
spacing and a shared TTL cache. Every call is best-effort: on failure the caller
keeps serving the last good snapshot instead of blanking the dashboard.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

USER_AGENT = "CryptoPulse/1.0 (educational paper-trading dashboard)"


class RateGate:
    """Serialises calls to one host with a minimum spacing between them."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            due = self._last + self._min_spacing()
        now = time.monotonic()
        if due > now:
            time.sleep(min(due - now, 8.0))
        with self._lock:
            self._last = time.monotonic()

    def _min_spacing(self) -> float:
        return self.min_interval


class Feed:
    """One upstream API: gated requests, TTL cache, soft failure."""

    def __init__(
        self,
        name: str,
        base_url: str,
        min_interval: float,
        timeout: int = 15,
        retries: int = 2,
        headers: Optional[dict] = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if headers:
            self.session.headers.update(headers)
        self.gate = RateGate(min_interval)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.last_ok_at: Optional[float] = None
        self.calls = 0

    # -- cache -------------------------------------------------------------
    def cached(self, key: str, ttl: float) -> Any:
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return None

    def store(self, key: str, value: Any) -> Any:
        with self._lock:
            self._cache[key] = (time.time(), value)
        return value

    def get(
        self,
        path: str,
        params: Optional[dict] = None,
        ttl: float = 0.0,
        cache_key: Optional[str] = None,
    ) -> Any:
        """GET JSON, honouring the cache and the host's rate gate.

        Returns None instead of raising so callers can fall back gracefully.
        """
        key = cache_key or path + "?" + json.dumps(params or {}, sort_keys=True)
        if ttl > 0:
            hit = self.cached(key, ttl)
            if hit is not None:
                return hit

        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            self.gate.wait()
            try:
                self.calls += 1
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 429:
                    # CoinGecko rate limit: back off and try once more.
                    self.last_error = f"{self.name}: rate limited (429)"
                    time.sleep(min(12.0, 4.0 * (attempt + 1)))
                    continue
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                self.last_error = f"{self.name}: {type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(0.6 * (attempt + 1))
                continue
            self.last_ok_at = time.time()
            self.last_error = None
            if ttl > 0:
                self.store(key, payload)
            return payload

        if last_exc is None:
            # Only 429s: keep whatever we had before rather than failing hard.
            with self._lock:
                stale = self._cache.get(key)
            return stale[1] if stale else None
        with self._lock:
            stale = self._cache.get(key)
        return stale[1] if stale else None


def write_json(path: Path, value: Any) -> None:
    """Atomic-ish local persistence for the wallet and user selections."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def downsample(values: list, points: int) -> list[float]:
    """Reduce a price series to `points` samples, always keeping the last print."""
    clean = [float(v) for v in values if isinstance(v, (int, float)) and v == v]
    if len(clean) <= points or points < 2:
        return clean
    step = (len(clean) - 1) / (points - 1)
    out = [clean[round(i * step)] for i in range(points)]
    if out[-1] != clean[-1]:
        out[-1] = clean[-1]
    return out


def build_feeds(settings) -> tuple[Feed, Feed, Callable[[str], Optional[str]]]:
    """CoinGecko (catalogue) + Binance (ticks/candles) feeds, keyed by settings."""
    gecko_headers = {}
    if settings.coingecko_key:
        # Demo/pro keys go in the header; free keys use ?apikey= instead.
        if len(settings.coingecko_key) > 40:
            gecko_headers["x-cg-pro-api-key"] = settings.coingecko_key
    gecko = Feed(
        "coingecko",
        settings.coingecko_base,
        settings.gecko_min_interval,
        timeout=settings.http_timeout,
        retries=settings.request_retries,
        headers=gecko_headers or None,
    )
    if settings.coingecko_key and len(settings.coingecko_key) <= 40:
        gecko.session.params = {"apikey": settings.coingecko_key}  # type: ignore[attr-defined]

    binance = Feed(
        "binance",
        settings.binance_base,
        settings.binance_min_interval,
        timeout=settings.http_timeout,
        retries=settings.request_retries,
    )

    def pair_url(path: str, params: dict) -> str:  # pragma: no cover - helper
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{binance.base_url}/{path.lstrip('/')}?{query}"

    return gecko, binance, pair_url
