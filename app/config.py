"""Configuration and market constants.

Environment variables (optionally loaded from .env in the project root):

  HOST, PORT                 web server bind address (default 127.0.0.1:8001)
  TOP_COINS                  how many ranked coins to keep in the table (250)
  MARKETS_REFRESH_SECONDS    coin table refresh (60)
  CAPITAL                    starting paper cash in the display currency (10000)
  RISK_PER_TRADE_PCT         % of equity risked per suggested trade (1.5)
  DISPLAY_CURRENCY=usdt      unit every price, cap and balance is shown in
  VS_CURRENCY=usd            CoinGecko quote currency (the API has no usdt)
  AUTO_TRADE=0               disable automatic execution of signals
  COINGECKO_API_KEY          optional; raises the free rate limit
  BINANCE_BASE_URL           override the public market-data host
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
WALLET_PATH = DATA_DIR / "wallet.json"
STARRED_PATH = DATA_DIR / "starred.json"
ADDED_PATH = DATA_DIR / "added_coins.json"
PAIRS_PATH = DATA_DIR / "binance_pairs.json"

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def _load_dotenv() -> None:
    """Tiny .env loader so the app has no hard dependency on python-dotenv."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- timeframes ------------------------------------------------------------
# key -> (binance interval, coin-gecko `days` fallback, cache ttl seconds,
#         how the x-axis labels the bar)
TIMEFRAMES: dict[str, dict] = {
    "1m": {"binance": "1m", "days": 1, "ttl": 4, "axis": "time"},
    "5m": {"binance": "5m", "days": 1, "ttl": 8, "axis": "time"},
    "15m": {"binance": "15m", "days": 3, "ttl": 20, "axis": "time"},
    "30m": {"binance": "30m", "days": 7, "ttl": 30, "axis": "time"},
    "1h": {"binance": "1h", "days": 14, "ttl": 45, "axis": "hm"},
    "4h": {"binance": "4h", "days": 30, "ttl": 90, "axis": "day"},
    "1d": {"binance": "1d", "days": 180, "ttl": 180, "axis": "date"},
    "1w": {"binance": "1w", "days": 365, "ttl": 600, "axis": "date"},
    "1M": {"binance": "1M", "days": 730, "ttl": 900, "axis": "month"},
    "1y": {"binance": "1y", "days": 1000, "ttl": 1800, "axis": "month"},
}

# Bars kept per request; the frontend pans/zooms inside this window.
BARS_PER_TF = {
    "1m": 180, "5m": 200, "15m": 200, "30m": 200, "1h": 200,
    "4h": 210, "1d": 240, "1w": 160, "1M": 130, "1y": 60,
}

# CoinGecko OHLC resolution changes with the window, so `days` above is capped
# to what the free tier still returns at a usable granularity.
GECKO_MAX_DAYS = 365

# Shown in the header ticker strip; resolved to Binance pairs when possible.
MAJOR_IDS = [
    "bitcoin", "ethereum", "tether", "ripple", "solana", "binancecoin",
    "dogecoin", "cardano", "tron", "avalanche-2", "chainlink", "polkadot",
]

# CoinGecko tickers are sparse, so a few well-known symbol collisions are
# resolved to the Binance base asset explicitly instead of guessed.
MANUAL_PAIRS = {
    "usd-coin": "USDC",
    "binance-usd": "BUSDT",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "wrapped-bitcoin": "WBTC",
    "wrapped-steth": "WSTETH",
    "coinbase-wrapped-btc": "CBBTC",
}

# Token-name prefixes Binance uses for multiples of a coin (1000PEPE, 1MBABYDOGE).
PAIR_PREFIXES = ["", "1000", "10000", "1000000"]


@dataclass
class Settings:
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: env_int("PORT", 8001))

    top_coins: int = field(default_factory=lambda: max(50, min(1000, env_int("TOP_COINS", 250))))
    markets_refresh_seconds: int = field(default_factory=lambda: max(30, env_int("MARKETS_REFRESH_SECONDS", 60)))
    tick_seconds: float = 1.0
    push_seconds: int = 3
    majors_refresh_seconds: int = 5
    gecko_min_interval: float = 2.5  # free tier is ~10-30 calls/min
    binance_min_interval: float = 0.12
    http_timeout: int = 15
    request_retries: int = 2

    coingecko_key: str = field(default_factory=lambda: os.getenv("COINGECKO_API_KEY", "").strip())
    coingecko_base: str = field(default_factory=lambda: os.getenv("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3"))
    binance_base: str = field(default_factory=lambda: os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision"))

    auto_trade: bool = field(default_factory=lambda: env_bool("AUTO_TRADE", False))
    capital: float = field(default_factory=lambda: env_float("CAPITAL", 10000.0))
    risk_per_trade_pct: float = field(default_factory=lambda: env_float("RISK_PER_TRADE_PCT", 1.5))
    max_exposure_pct: float = 40.0
    max_open_positions: int = 8
    daily_loss_limit_pct: float = 5.0
    confidence_threshold: int = 65
    cooldown_minutes: int = 30
    stop_atr_mult: float = 1.5
    reward_risk: float = 1.8

    @property
    def vs_currency(self) -> str:
        """Quote currency used when talking to CoinGecko.

        The free API rejects "usdt" here ("invalid vs_currency"), so the
        catalogue is always pulled in USD and converted to `display_currency`
        on the way in. Binance data needs no conversion - its pairs are USDT.
        """
        return os.getenv("VS_CURRENCY", "usd").strip().lower() or "usd"

    @property
    def display_currency(self) -> str:
        """Unit every price, market cap and wallet balance is shown in."""
        return os.getenv("DISPLAY_CURRENCY", "usdt").strip().lower() or "usdt"
