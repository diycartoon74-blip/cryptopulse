"""Coin catalogue: the ranked market table, search, global stats and pairs.

CoinGecko owns the catalogue (it knows every listed coin, its rank, market cap and
logo). Binance only decides *which* coins get a fast live feed - a coin with a
USDT pair gets one-second candles, everything else moves with the slower
CoinGecko refresh.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from .config import ADDED_PATH, GECKO_MAX_DAYS, MANUAL_PAIRS, PAIR_PREFIXES, PAIRS_PATH, STARRED_PATH
from .datafeed import downsample, read_json, write_json


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and value == value else None


def _pct(value: Any) -> Optional[float]:
    return round(float(value), 2) if isinstance(value, (int, float)) and value == value else None


class MarketCatalogue:
    def __init__(self, settings, gecko, binance) -> None:
        self.settings = settings
        self.gecko = gecko
        self.binance = binance
        self.rows: dict[str, dict] = {}
        self.updated_at: Optional[float] = None
        self.error: Optional[str] = None
        self.added: list[str] = [str(x) for x in (read_json(ADDED_PATH, []) or [])]
        self.starred: list[str] = [str(x) for x in (read_json(STARRED_PATH, []) or [])]
        self._usdt_bases: set[str] = set()
        self._pairs_loaded_at: float = 0.0
        # Tether's live USD print, used to restate CoinGecko's USD numbers in the
        # display currency. Stays 1.0 until the first refresh sees USDT.
        self.usdt_usd: float = 1.0
        self.global_stats: dict = {}
        self.trending: list[dict] = []
        self.breadth: dict = {"up": 0, "down": 0, "flat": 0}
        self.gainers: list[dict] = []
        self.losers: list[dict] = []

    # ---------------------------------------------------------------- currency
    @property
    def display_rate(self) -> float:
        """Multiply a USD amount by this to get the display currency (USDT/USD)."""
        if self.settings.display_currency != "usdt":
            return 1.0
        anchor = self.usdt_usd
        # A depegged or missing USDT print should degrade to 1:1, not 1000:1.
        if not anchor or not 0.5 <= anchor <= 2.0:
            return 1.0
        return 1.0 / anchor

    def _capture_anchor(self, raw_rows: list[dict]) -> None:
        for raw in raw_rows:
            if str(raw.get("id")) == "tether":
                price = _num(raw.get("current_price"))
                if price:
                    self.usdt_usd = float(price)
                return

    def to_display(self, value: Optional[float]) -> Optional[float]:
        """Restate a CoinGecko USD amount in the display currency.

        Rounded to 12 significant digits so 0.998995 / 0.998995 lands on 1.0
        instead of 0.9999999999999999.
        """
        if value is None:
            return None
        rate = self.display_rate
        return value if rate == 1.0 else float(f"{value * rate:.12g}")

    # ------------------------------------------------------------------ pairs
    def _load_pairs(self, force: bool = False) -> None:
        """Discover which base assets have a Binance USDT pair (cached to disk)."""
        now = time.time()
        if not force and self._usdt_bases and now - self._pairs_loaded_at < 6 * 3600:
            return
        if not force:
            cached = read_json(PAIRS_PATH, None)
            if cached and now - float(cached.get("at", 0)) < 12 * 3600:
                self._usdt_bases = set(cached.get("bases", []))
                self._pairs_loaded_at = now
                return
        tickers = self.binance.get("api/v3/ticker/price", None, ttl=0)
        if not isinstance(tickers, list):
            # Keep whatever we had; a missing pair only downgrades to the slow feed.
            return
        bases = set()
        for entry in tickers:
            symbol = str(entry.get("symbol", ""))
            if symbol.endswith("USDT") and len(symbol) > 4:
                bases.add(symbol[:-4])
        if bases:
            self._usdt_bases = bases
            self._pairs_loaded_at = now
            write_json(PAIRS_PATH, {"at": now, "bases": sorted(bases)})

    def pair_for(self, row: dict) -> Optional[str]:
        """Binance symbol for a coin, e.g. BTC -> BTCUSDT."""
        coin_id = row["id"]
        if coin_id in MANUAL_PAIRS:
            candidate = MANUAL_PAIRS[coin_id] + "USDT"
            if not self._usdt_bases or candidate in {b + "USDT" for b in self._usdt_bases}:
                return candidate
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            return None
        for prefix in PAIR_PREFIXES:
            base = f"{prefix}{symbol}"
            if base in self._usdt_bases:
                return f"{base}USDT"
        if not self._usdt_bases:
            return f"{symbol}USDT"
        return None

    # --------------------------------------------------------------- markets
    def refresh(self) -> None:
        self._load_pairs()
        wanted = max(50, min(1000, int(self.settings.top_coins)))
        fetched: list[dict] = []
        per_page = min(250, wanted)
        pages = (wanted + per_page - 1) // per_page
        for page in range(1, pages + 1):
            payload = self.gecko.get(
                "coins/markets",
                {
                    "vs_currency": self.settings.vs_currency,
                    "order": "market_cap_desc",
                    "per_page": per_page,
                    "page": page,
                    "sparkline": "true",
                    "price_change_percentage": "1h,24h,7d",
                },
                ttl=30,
                cache_key=f"markets:p{page}:{per_page}",
            )
            if isinstance(payload, list):
                fetched.extend(payload)
            else:
                break

        merged: dict[str, dict] = {}
        # Read the USDT/USD print out of the raw payload first: _normalise restates
        # every money field in the display currency, tether's included.
        self._capture_anchor(fetched)
        for raw in fetched:
            row = self._normalise(raw)
            if row:
                merged[row["id"]] = row

        # Search-added coins come back on their own ids call so they stay in the
        # table even when they sit outside the top 250.
        if self.added:
            extra = self.gecko.get(
                "coins/markets",
                {
                    "vs_currency": self.settings.vs_currency,
                    "ids": ",".join(self.added[:100]),
                    "sparkline": "true",
                    "price_change_percentage": "1h,24h,7d",
                },
                ttl=30,
                cache_key="markets:added",
            )
            if isinstance(extra, list):
                for raw in extra:
                    row = self._normalise(raw)
                    if row and row["id"] not in merged:
                        row["added"] = True
                        merged[row["id"]] = row
            # A pinned coin must not disappear just because its quote call failed
            # (rate limits are routine), so fall back to the last row we held.
            for coin_id in self.added:
                if coin_id not in merged and coin_id in self.rows:
                    merged[coin_id] = self.rows[coin_id]

        if not merged:
            self.error = self.gecko.last_error or "no market data"
            return

        self.error = None
        self.rows = merged
        self.updated_at = time.time()
        for row in self.rows.values():
            row["pair"] = self.pair_for(row)
            row["starred"] = row["id"] in self.starred
        self._derive_views()

    def _normalise(self, raw: dict) -> Optional[dict]:
        coin_id = raw.get("id")
        if not coin_id:
            return None
        spark = (raw.get("sparkline_in_7d") or {}).get("price") or []
        to_display = self.to_display

        def money(key: str) -> Optional[float]:
            """A USD field restated in the display currency."""
            return to_display(_num(raw.get(key)))

        row = {
            "id": str(coin_id),
            "symbol": str(raw.get("symbol") or "").upper(),
            "name": str(raw.get("name") or coin_id),
            "image": str(raw.get("image") or ""),
            "price": money("current_price"),
            "rank": _num(raw.get("market_cap_rank")),
            "mcap": money("market_cap"),
            "fdv": money("fully_diluted_valuation"),
            "vol": money("total_volume"),
            "chg1h": _pct(raw.get("price_change_percentage_1h_in_currency")),
            "chg24h": _pct(raw.get("price_change_percentage_24h_in_currency")),
            "chg7d": _pct(raw.get("price_change_percentage_7d_in_currency")),
            "high24": money("high_24h"),
            "low24": money("low_24h"),
            "ath": money("ath"),
            "athChg": _pct(raw.get("ath_change_percentage")),
            "atl": money("atl"),
            "atlChg": _pct(raw.get("atl_change_percentage")),
            "supply": _num(raw.get("circulating_supply")),
            "maxSupply": _num(raw.get("max_supply")),
            "athDate": str(raw.get("ath_date") or "")[:10],
            "spark": [to_display(v) for v in downsample(spark, 48)],
            "sparkDays": 7,
            "added": False,
            "starred": False,
            "pair": None,
            "live": False,
            "updatedAt": self.updated_at,
        }
        return row if row["price"] is not None else None

    def _derive_views(self) -> None:
        ranked = sorted(self.rows.values(), key=lambda r: (r["rank"] if r["rank"] else 99_999))
        movers = [r for r in ranked if r.get("chg24h") is not None and (r["mcap"] or 0) > 0]
        self.breadth = {
            "up": sum(1 for r in movers if r["chg24h"] > 0.05),
            "down": sum(1 for r in movers if r["chg24h"] < -0.05),
            "flat": sum(1 for r in movers if -0.05 <= r["chg24h"] <= 0.05),
        }
        self.gainers = sorted(movers, key=lambda r: -r["chg24h"])[:6]
        self.losers = sorted(movers, key=lambda r: r["chg24h"])[:6]
        self._ranked = ranked

    # ---------------------------------------------------------------- ordered
    def table_rows(self, limit: int = 1000) -> list[dict]:
        base = getattr(self, "_ranked", None) or sorted(
            self.rows.values(), key=lambda r: (r["rank"] if r["rank"] else 99_999)
        )
        return base[:limit]

    def get(self, coin_id: str) -> Optional[dict]:
        return self.rows.get(coin_id)

    # ----------------------------------------------------------------- search
    def search(self, query: str, limit: int = 12) -> dict:
        """CoinGecko search reaches the whole catalogue, not just the top 250."""
        query = (query or "").strip()
        if len(query) < 2:
            return {"query": query, "results": [], "note": "type at least 2 characters"}

        found = self.gecko.get("search", {"query": query}, ttl=120, cache_key=f"search:{query.lower()}")
        candidates: list[dict] = []
        if isinstance(found, dict):
            for item in (found.get("coins") or [])[:25]:
                candidates.append(
                    {
                        "id": str(item.get("id")),
                        "symbol": str(item.get("symbol") or "").upper(),
                        "name": str(item.get("name") or ""),
                        "rank": _num(item.get("market_cap_rank")),
                        "image": str(item.get("thumb") or ""),
                    }
                )

        # Local matches first: cheaper and instant for coins already loaded.
        lowered = query.lower()
        for row in self.table_rows(2000):
            if lowered in row["symbol"].lower() or lowered in row["name"].lower():
                if not any(c["id"] == row["id"] for c in candidates):
                    candidates.append(
                        {
                            "id": row["id"],
                            "symbol": row["symbol"],
                            "name": row["name"],
                            "rank": row["rank"],
                            "image": row["image"],
                            "price": row["price"],
                            "chg24h": row["chg24h"],
                            "mcap": row["mcap"],
                        }
                    )
        candidates = [c for c in candidates if c["id"]][:limit]

        # Price the results we do not already hold (ranked ones are already live).
        # These quotes are display-only: they go into a side cache so searching a
        # rank-13,000 memecoin cannot quietly slip it into the tracked table. The
        # PIN button is what promotes a coin, via add_coin().
        missing = [c["id"] for c in candidates if c["id"] not in self.rows]
        quoted: dict[str, dict] = {}
        if missing:
            priced = self.gecko.get(
                "coins/markets",
                {
                    "vs_currency": self.settings.vs_currency,
                    "ids": ",".join(missing),
                    "price_change_percentage": "24h",
                },
                ttl=60,
                cache_key="search:priced:" + ",".join(sorted(missing)),
            )
            if isinstance(priced, list):
                for raw in priced:
                    row = self._normalise(raw)
                    if row:
                        row["pair"] = self.pair_for(row)
                        quoted[row["id"]] = row
            for candidate in candidates:
                row = quoted.get(candidate["id"])
                if row:
                    candidate["price"] = row["price"]
                    candidate["chg24h"] = row["chg24h"]
                    candidate["mcap"] = row["mcap"]
        for candidate in candidates:
            candidate["inTable"] = candidate["id"] in self.rows
            candidate["added"] = candidate["id"] in self.added
        candidates.sort(key=lambda c: (c["rank"] if c["rank"] else 99_999))
        return {"query": query, "results": candidates}

    def add_coin(self, coin_id: str) -> dict:
        coin_id = coin_id.strip().lower()
        if coin_id not in self.added:
            self.added.append(coin_id)
            write_json(ADDED_PATH, self.added)
            if coin_id not in self.rows:
                payload = self.gecko.get(
                    "coins/markets",
                    {
                        "vs_currency": self.settings.vs_currency,
                        "ids": coin_id,
                        "sparkline": "true",
                        "price_change_percentage": "1h,24h,7d",
                    },
                    ttl=0,
                )
                if isinstance(payload, list):
                    for raw in payload:
                        row = self._normalise(raw)
                        if row:
                            row["added"] = True
                            row["pair"] = self.pair_for(row)
                            self.rows[row["id"]] = row
        self._derive_views()
        return {
            "added": self.added,
            "row": self.rows.get(coin_id),
            "note": "coin pinned to your table" if self.rows.get(coin_id) else "coin queued - it appears on the next refresh",
        }

    def remove_coin(self, coin_id: str) -> dict:
        coin_id = coin_id.strip().lower()
        self.added = [c for c in self.added if c != coin_id]
        write_json(ADDED_PATH, self.added)
        return {"added": self.added}

    def set_star(self, coin_id: str, on: bool) -> dict:
        coin_id = coin_id.strip().lower()
        if on and coin_id not in self.starred:
            self.starred.append(coin_id)
        elif not on:
            self.starred = [c for c in self.starred if c != coin_id]
        write_json(STARRED_PATH, self.starred)
        for row in self.rows.values():
            row["starred"] = row["id"] in self.starred
        return {"starred": self.starred}

    # ----------------------------------------------------------------- extras
    def refresh_global(self) -> None:
        payload = self.gecko.get("global", None, ttl=120, cache_key="global")
        if not isinstance(payload, dict):
            return
        data = payload.get("data") or {}
        mcap = _num((data.get("total_market_cap") or {}).get(self.settings.vs_currency))
        vol = _num((data.get("total_volume") or {}).get(self.settings.vs_currency))
        dominance = data.get("market_cap_percentage") or {}
        self.global_stats = {
            "mcap": self.to_display(mcap),
            "mcapChange24h": _pct(data.get("market_cap_change_percentage_24h_usd")),
            "vol": self.to_display(vol),
            "btcDominance": _pct(dominance.get("btc")),
            "ethDominance": _pct(dominance.get("eth")),
            "activeCoins": _num(data.get("active_cryptocurrencies")),
            "markets": _num(data.get("markets")),
            "at": time.time(),
        }

    def refresh_trending(self) -> None:
        payload = self.gecko.get("search/trending", None, ttl=300, cache_key="trending")
        if not isinstance(payload, dict):
            return
        out = []
        for entry in (payload.get("coins") or [])[:7]:
            item = entry.get("item") or {}
            data = item.get("data") or {}
            out.append(
                {
                    "id": str(item.get("id")),
                    "symbol": str(item.get("symbol") or "").upper(),
                    "name": str(item.get("name") or ""),
                    "rank": _num(item.get("market_cap_rank")),
                    "image": str(item.get("thumb") or ""),
                    # search/trending only ever quotes USD, so restate it too.
                    "price": self.to_display(_num(data.get("price"))),
                    "chg24h": _pct((data.get("price_change_percentage_24h") or {}).get("usd")),
                }
            )
        self.trending = out

    def coin_detail(self, coin_id: str) -> Optional[dict]:
        """Deeper stats for the selected coin, from CoinGecko's coin endpoint."""
        days = min(GECKO_MAX_DAYS, 1)
        payload = self.gecko.get(
            f"coins/{coin_id}/market_chart",
            {"vs_currency": self.settings.vs_currency, "days": days, "interval": "5m"},
            ttl=120,
            cache_key=f"spark:{coin_id}",
        )
        detail: dict[str, Any] = {"coin": self.rows.get(coin_id)}
        if isinstance(payload, dict):
            prices = payload.get("prices") or []
            series = [p[1] for p in prices]
            detail["today"] = [downsample([self.to_display(v) for v in series], 160)]
            if prices:
                detail["lastTs"] = prices[-1][0]
        return detail
