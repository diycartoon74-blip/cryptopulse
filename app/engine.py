"""Trading engine: assembles the dashboard snapshot, scores setups, and drives
the paper wallet.

Signals are computed from the same indicator values the chart draws, so what the
card claims is visible on the candles. Execution stays inside the paper wallet.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Optional

from .chartdata import ChartService
from .config import DATA_DIR, MAJOR_IDS, TIMEFRAMES
from .datafeed import build_feeds
from .indicators import macd
from .market import MarketCatalogue
from .storage import JsonStore
from .wallet import FEE_RATE, PaperWallet, WalletError

EVENTS_PATH = DATA_DIR / "events.json"


def _pct_from(values: list[float], lookback: int) -> Optional[float]:
    if len(values) < 2 or lookback <= 0 or len(values) <= lookback:
        return None
    past = values[-1 - lookback]
    if not past:
        return None
    return (values[-1] / past - 1.0) * 100.0


class TradingEngine:
    def __init__(self, settings) -> None:
        self.settings = settings
        gecko, binance, _ = build_feeds(settings)
        self.gecko = gecko
        self.binance = binance
        self.data = MarketCatalogue(settings, gecko, binance)
        self.charts = ChartService(self.data, settings)
        self.wallet = PaperWallet(settings)
        self.log_store = JsonStore(EVENTS_PATH)
        restored = list(reversed(self.log_store.load([]) or []))
        self.events: deque[dict] = deque(restored[:60], maxlen=80)
        self.focus: str = "bitcoin"
        self.tf: str = "5m"
        self.halted: bool = False
        self.auto_trade: bool = settings.auto_trade
        self.last_refresh: Optional[float] = None
        self.last_error: Optional[str] = None
        self.signal: Optional[dict] = None
        self.equity_history: deque[dict] = deque(maxlen=240)
        self.day_start_equity: Optional[float] = None
        self._day_key: Optional[str] = None
        self._last_signal_at: dict[str, float] = {}
        self._majors_cache: list[dict] = []
        self._majors_at: float = 0.0
        self._cycle = 0
        self._spark_cycle: Optional[float] = None
        self._last_tick: Optional[dict] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ log
    def log(self, kind: str, message: str) -> None:
        entry = {"at": int(time.time() * 1000), "kind": kind, "message": str(message)}
        self.events.appendleft(entry)
        self.events = deque(list(self.events)[:80], maxlen=80)
        try:
            self.log_store.save(list(self.events)[:80])
        except OSError:
            pass

    # -------------------------------------------------------------- refresh
    def refresh(self) -> dict:
        with self._lock:
            self._cycle += 1
            started = time.time()
            self.data.refresh()
            if self.data.error:
                self.last_error = self.data.error
            else:
                self.last_error = None
            if self.focus not in self.data.rows and self.data.rows:
                self.focus = next(iter(self.data.table_rows(1)), {}).get("id", "bitcoin")

            if self._cycle % 3 == 1:
                self.data.refresh_global()
            if self._cycle % 6 == 1:
                self.data.refresh_trending()

            marks = self.marks()
            self.wallet.fill_orders(marks)
            self._track_equity(marks)
            self._risk_check()
            self._compute_signal()
            if self.auto_trade and not self.halted:
                self._maybe_auto_trade()
            self.last_refresh = time.time()
            if self._cycle == 1:
                self.log("feed", f"catalogue loaded: {len(self.data.rows)} coins in {round(self.last_refresh - started, 2)}s")
            return self.snapshot()

    def marks(self) -> dict[str, float]:
        out = {}
        for coin_id, row in self.data.rows.items():
            price = row.get("price")
            if price:
                out[coin_id] = float(price)
        return out

    def _track_equity(self, marks: dict[str, float]) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self._day_key != today:
            self._day_key = today
            self.day_start_equity = self.wallet.cash + self.wallet.holdings_value(marks)[0]
        value, unreal = self.wallet.holdings_value(marks)
        equity = self.wallet.cash + value
        self.equity_history.append({"t": int(time.time() * 1000), "equity": round(equity, 2), "unrealised": unreal})

    def _risk_check(self) -> None:
        marks = self.marks()
        value, unreal = self.wallet.holdings_value(marks)
        equity = self.wallet.cash + value
        baseline = self.day_start_equity or equity
        drawdown_pct = (equity - baseline) / baseline * 100.0 if baseline else 0.0
        if drawdown_pct <= -self.settings.daily_loss_limit_pct and not self.halted:
            self.halted = True
            self.log("risk", f"daily loss limit hit ({drawdown_pct:.2f}%) - auto entries halted")
        if value / (equity or 1) * 100.0 > self.settings.max_exposure_pct and not self.halted:
            self.log("risk", f"exposure above {self.settings.max_exposure_pct:.0f}% of equity")

    def resume(self) -> None:
        self.halted = False
        self.day_start_equity = None
        self.log("risk", "operator resumed trading")

    # --------------------------------------------------------------- signals
    def _compute_signal(self) -> None:
        # Capture focus/timeframe once so a concurrent switch cannot mix one
        # coin's candles with another coin's quote.
        coin_id = self.focus
        tf = self.tf
        row = self.data.get(coin_id)
        if not row:
            self.signal = None
            return
        try:
            chart = self.charts.get(coin_id, tf)
        except Exception as exc:
            self.signal = {
                "id": f"{coin_id}:{tf}",
                "coin": coin_id,
                "symbol": row["symbol"],
                "bias": "neutral",
                "confidence": 0,
                "reasons": [{"label": "Chart feed", "detail": f"unavailable - {type(exc).__name__}", "vote": 0}],
                "entry": row.get("price"),
                "stop": None,
                "target": None,
                "size": None,
            }
            return

        candles = chart["candles"]
        closes = [c["c"] for c in candles]
        last = closes[-1]
        ema20 = chart["indicators"]["ema20"]
        ema50 = chart["indicators"]["ema50"]
        rsi_series = chart["indicators"]["rsi"]
        atr_series = chart["indicators"]["atr"]
        vwap_series = chart["indicators"]["vwap"]
        macd_line, macd_signal, hist = macd(closes)

        votes: list[dict] = []
        score = 0.0

        trend_20_50 = _signed(ema20[-1], ema50[-1], scale=100.0)
        if trend_20_50 is not None:
            weight = max(-1.5, min(1.5, trend_20_50 / 0.4))
            score += weight
            votes.append({
                "label": f"EMA 20 vs 50 ({tf})",
                "detail": f"{'above' if weight > 0 else 'below'} by {abs(trend_20_50):.2f}%",
                "vote": round(weight, 2),
            })

        above_vwap = _signed(last, _last_value(vwap_series), scale=100.0)
        if above_vwap is not None:
            weight = max(-1.25, min(1.25, above_vwap / 0.35))
            score += weight
            votes.append({
                "label": "Price vs session VWAP",
                "detail": f"{'holding above' if weight > 0 else 'trading below'} session VWAP ({abs(above_vwap):.2f}%)",
                "vote": round(weight, 2),
            })

        rsi_now = _last_value(rsi_series)
        if rsi_now is not None:
            # Above 50 is momentum; past the extremes it becomes exhaustion and
            # starts to count against the move instead of for it.
            if rsi_now >= 78:
                weight = -0.9
            elif rsi_now <= 22:
                weight = 0.9
            else:
                weight = max(-1.25, min(1.25, (rsi_now - 50.0) / 20.0))
            score += weight
            label = "RSI 14"
            detail = f"{rsi_now:.1f}" + (" (overbought)" if rsi_now >= 78 else " (oversold)" if rsi_now <= 22 else "")
            votes.append({"label": label, "detail": detail, "vote": round(weight, 2)})

        hist_now = _last_value(hist)
        hist_prev = _value_at(hist, -2)
        if hist_now is not None and last:
            weight = 1.0 if hist_now > 0 else -1.0
            if hist_prev is not None and (hist_now > 0) != (hist_prev > 0):
                weight *= 1.6  # a fresh cross counts for more
            score += weight
            votes.append({
                "label": "MACD histogram",
                "detail": f"{hist_now / last * 100:+.3f}% of price",
                "vote": round(weight, 2),
            })

        bar_momentum = _pct_from(closes, min(6, len(closes) - 1))
        if bar_momentum is not None:
            weight = max(-1.25, min(1.25, bar_momentum / 0.6))
            score += weight
            votes.append({"label": f"Last 6 bars ({tf})", "detail": f"{bar_momentum:+.2f}%", "vote": round(weight, 2)})

        if row.get("chg24h") is not None:
            weight = max(-1.0, min(1.0, row["chg24h"] / 4.0))
            score += weight
            votes.append({"label": "24h change", "detail": f"{row['chg24h']:+.2f}%", "vote": round(weight, 2)})

        atr_now = _last_value(atr_series) or 0.0
        # A move more than three ATRs away from the slow average is overextended,
        # so damp the confidence instead of chasing it.
        slow = _last_value(ema50)
        if atr_now and slow and abs(last - slow) > 3 * atr_now:
            score *= 0.75

        confidence = max(0, min(100, round(50 + score * 9)))
        bias = "bullish" if score > 0.45 else "bearish" if score < -0.45 else "neutral"

        stop = target = size = None
        if atr_now:
            if bias == "bullish":
                stop = last - atr_now * self.settings.stop_atr_mult
                target = last + (last - stop) * self.settings.reward_risk
            elif bias == "bearish":
                stop = last + atr_now * self.settings.stop_atr_mult
                target = last - (stop - last) * self.settings.reward_risk
            risk_amount = (self.wallet.cash + self.wallet.holdings_value(self.marks())[0]) * self.settings.risk_per_trade_pct / 100.0
            if stop and abs(last - stop) > 0:
                size = {
                    "risk": round(risk_amount, 2),
                    "qty": round(risk_amount / abs(last - stop), 8),
                    "value": round(risk_amount / abs(last - stop) * last, 2),
                }

        self.signal = {
            "id": f"{coin_id}:{tf}:{candles[-1]['t']}",
            "coin": coin_id,
            "symbol": row["symbol"],
            "name": row["name"],
            "bias": bias,
            "score": round(score, 2),
            "confidence": confidence,
            "reasons": votes,
            "entry": round(last, 8),
            "stop": round(stop, 8) if stop else None,
            "target": round(target, 8) if target else None,
            "riskReward": self.settings.reward_risk,
            "atr": round(atr_now, 8) if atr_now else None,
            "size": size,
            "tf": tf,
            "bars": len(candles),
            "source": chart["meta"]["source"],
            "live": chart["meta"]["live"],
            "threshold": self.settings.confidence_threshold,
        }

    def _maybe_auto_trade(self) -> None:
        sig = self.signal
        if not sig or sig["confidence"] < self.settings.confidence_threshold or sig["bias"] != "bullish":
            return
        key = f"{sig['coin']}:{sig['tf']}"
        cooldown = self.settings.cooldown_minutes * 60
        if time.time() - self._last_signal_at.get(key, 0) < cooldown:
            return
        size = sig.get("size") or {}
        qty = size.get("qty")
        if not qty:
            return
        row = self.data.get(sig["coin"]) or {}
        try:
            trade = self.wallet.buy(sig["coin"], row.get("name", sig["coin"]), row.get("symbol", ""),
                                    sig["entry"], qty=qty, reason=f"auto {sig['bias']} {sig['confidence']}%")
            self._last_signal_at[key] = time.time()
            self.log("auto", f"filled {trade['qty']:g} {trade['symbol']} at {trade['price']:,.6g} ({sig['confidence']}% confidence)")
        except WalletError as exc:
            self._last_signal_at[key] = time.time()
            self.log("auto", f"skipped {sig['symbol']}: {exc}")

    # ----------------------------------------------------------------- trade
    def execute_manual(self, signal_id: str, fraction: float = 1.0) -> dict:
        sig = self.signal or {}
        if sig.get("id") != signal_id:
            return {"ok": False, "error": "signal is stale - it will be recomputed on the next refresh"}
        return self.open_position(sig["coin"], qty=sig.get("size", {}).get("qty"), fraction=fraction,
                                  reason=f"manual {sig.get('bias')} {sig.get('confidence')}%")

    def open_position(self, coin_id: str, qty: Optional[float] = None, fraction: float = 1.0,
                      quote_amount: Optional[float] = None, reason: str = "manual buy") -> dict:
        row = self.data.get(coin_id)
        if not row:
            return {"ok": False, "error": f"unknown coin: {coin_id}"}
        price = self._mark(coin_id, row)
        try:
            quantity = qty
            if not quantity and quote_amount:
                # A quote-sized order (in the display currency): back out the
                # quantity including the taker fee.
                quantity = float(quote_amount) / (price * (1 + FEE_RATE)) if price else 0
            if not quantity:
                equity = self.wallet.cash + self.wallet.holdings_value(self.marks())[0]
                quantity = equity * max(0.0, min(1.0, float(fraction))) / (price or 1)
            trade = self.wallet.buy(coin_id, row["name"], row["symbol"], price, qty=quantity,
                                    quote_amount=quote_amount, reason=reason)
            self.log("trade", f"bought {trade['qty']:g} {trade['symbol']} at {trade['price']:,.6g}")
            return {"ok": True, "trade": trade}
        except WalletError as exc:
            self.log("trade", f"buy rejected ({coin_id}): {exc}")
            return {"ok": False, "error": str(exc)}

    def close_position(self, coin_id: str, fraction: float = 1.0) -> dict:
        price = self._mark(coin_id, self.data.get(coin_id) or {})
        try:
            trade = self.wallet.sell(coin_id, price, fraction=fraction, reason="manual close")
            self.log("trade", f"sold {trade['qty']:g} {trade['symbol']} at {trade['price']:,.6g} ({trade['realised']:+,.2f})")
            return {"ok": True, "trade": trade}
        except WalletError as exc:
            return {"ok": False, "error": str(exc)}

    def place_order(self, coin_id: str, side: str, qty: float, price: float) -> dict:
        row = self.data.get(coin_id)
        if not row:
            return {"ok": False, "error": f"unknown coin: {coin_id}"}
        try:
            order = self.wallet.place_limit(coin_id, row["name"], row["symbol"], side, qty, price or self._mark(coin_id, row))
            self.log("order", f"{side} {order['qty']:g} {row['symbol']} limit @ {order['price']:,.6g}")
            return {"ok": True, "order": order}
        except WalletError as exc:
            return {"ok": False, "error": str(exc)}

    def cancel_order(self, order_id: str) -> dict:
        try:
            return {"ok": True, "order": self.wallet.cancel_order(order_id)}
        except WalletError as exc:
            return {"ok": False, "error": str(exc)}

    def square_off_all(self) -> dict:
        closed = self.wallet.square_off(self.marks())
        self.log("trade", f"square-off closed {len(closed)} position(s)")
        return {"ok": True, "closed": len(closed)}

    def deposit(self, amount: float, note: str = "") -> dict:
        entry = self.wallet.deposit(amount, note)
        self.log("wallet", f"deposited {self.wallet.currency} {amount:,.2f}")
        return entry

    def withdraw(self, amount: float, note: str = "") -> dict:
        entry = self.wallet.withdraw(amount, note)
        self.log("wallet", f"withdrew {self.wallet.currency} {amount:,.2f}")
        return entry

    # ---------------------------------------------------------------- focus
    def set_focus(self, coin_id: str, tf: Optional[str] = None) -> dict:
        coin_id = (coin_id or "").strip().lower()
        if coin_id and coin_id in self.data.rows:
            self.focus = coin_id
        if tf and tf in TIMEFRAMES:
            self.tf = tf
        if coin_id and coin_id not in self.data.rows:
            self.data.add_coin(coin_id)
            if coin_id in self.data.rows:
                self.focus = coin_id
        self._compute_signal()
        return {"focus": self.focus, "tf": self.tf}

    def set_tf(self, tf: str) -> dict:
        if tf in TIMEFRAMES:
            self.tf = tf
            self._compute_signal()
        return {"tf": self.tf}

    def set_auto(self, on: bool) -> None:
        self.auto_trade = bool(on)
        self.log("settings", f"auto-trade {'enabled' if on else 'disabled'}")

    def _mark(self, coin_id: str, row: dict) -> float:
        """Tradeable price, with the tape cross-checked against the table print.

        The tape is fresher, but a frame can be built while the focus is moving
        between coins. A one-second print that is wildly off the catalogue price
        for that coin is the other coin's print, so it is ignored and logged
        rather than silently filling a trade at the wrong level.
        """
        table = float(row.get("price") or 0.0)
        tick = self._last_tick or {}
        if tick.get("coin") != coin_id or not tick.get("price"):
            return table
        live = float(tick["price"])
        if not table:
            return live
        drift = abs(live / table - 1.0)
        if drift > 0.25:
            self.log(
                "risk",
                f"ignored {coin_id} print {live:,.6g} vs table {table:,.6g} "
                f"({drift * 100:.0f}% apart) - using the table price",
            )
            return table
        return live

    # ----------------------------------------------------------------- tape
    def ticks(self) -> dict:
        """One tape frame.

        The focus is read once and reused: a POST can move it mid-frame, and
        stamping coin A's print onto coin B's row would price a trade wrongly.
        """
        coin = self.focus
        tf = self.tf
        frame: dict = {"type": "tick", "ts": int(time.time() * 1000), "coin": coin, "tf": tf}
        try:
            tick = self.charts.tick(coin, tf)
        except Exception:
            tick = None
        if tick:
            frame.update(tick)
            frame["coin"] = coin
            row = self.data.rows.get(coin)
            if row:
                row["price"] = tick["price"]
                row["live"] = True
        else:
            row = self.data.get(coin) or {}
            frame["price"] = row.get("price")
            frame["candle"] = None
            frame["source"] = "coingecko"
        self._last_tick = frame
        frame["majors"] = self._refresh_majors()
        return frame

    def _refresh_majors(self) -> list[dict]:
        if time.time() - self._majors_at < self.settings.majors_refresh_seconds:
            return self._majors_cache
        pairs = []
        for coin_id in MAJOR_IDS:
            row = self.data.get(coin_id)
            if row and row.get("pair"):
                pairs.append(row["pair"])
        if not pairs:
            return self._majors_cache
        try:
            majors = self.charts.majors(pairs)
        except Exception:
            return self._majors_cache
        if majors:
            by_pair = {m["pair"]: m for m in majors}
            for row in self.data.rows.values():
                entry = by_pair.get(row.get("pair"))
                if entry:
                    row["price"] = entry["price"] or row["price"]
                    row["chg24h"] = entry["chg24h"] if entry["chg24h"] is not None else row["chg24h"]
                    row["high24"] = entry["high"] or row["high24"]
                    row["low24"] = entry["low"] or row["low24"]
                    row["vol"] = entry["vol"] or row["vol"]
                    row["live"] = True
            self._majors_cache = majors
            self._majors_at = time.time()
        return self._majors_cache

    # -------------------------------------------------------------- snapshot
    def snapshot(self, sparklines: bool | str = "auto") -> dict:
        """`sparklines` keeps the 48-point curves off the 3-second pushes.

        "auto" sends them once per market refresh - enough to keep the table's
        curves current without shipping ~250 arrays every push.
        """
        if sparklines == "auto":
            include = bool(self.data.updated_at) and self.data.updated_at != self._spark_cycle
            if include:
                self._spark_cycle = self.data.updated_at
        else:
            include = bool(sparklines)
        marks = self.marks()
        rows = self.data.table_rows(1000)
        compact = [self._compact(row, include) for row in rows]
        wallet = self.wallet.snapshot(marks)
        focus_row = self.data.get(self.focus) or {}
        return {
            "generatedAt": int(time.time() * 1000),
            "currency": self.wallet.currency,
            # Live tether print (USD per 1 USDT). Every number in this payload is
            # already in `currency`; the browser uses the anchor to restate them
            # in whichever unit the viewer picked, USDT being the front-end default.
            "usdtUsd": round(float(self.data.usdt_usd or 1.0), 6),
            "coins": compact,
            "coinCount": len(self.data.rows),
            "topCoins": self.settings.top_coins,
            "focus": {
                "coin": self.focus,
                "symbol": focus_row.get("symbol"),
                "name": focus_row.get("name"),
                "image": focus_row.get("image"),
                "tf": self.tf,
                "price": focus_row.get("price"),
                "chg24h": focus_row.get("chg24h"),
                "pair": focus_row.get("pair"),
                "live": bool(focus_row.get("live")),
            },
            "signal": self.signal,
            "wallet": wallet,
            "global": self.data.global_stats,
            "trending": self.data.trending,
            "breadth": self.data.breadth,
            "gainers": [self._compact(r, include) for r in self.data.gainers],
            "losers": [self._compact(r, include) for r in self.data.losers],
            "starred": self.data.starred,
            "added": self.data.added,
            "majors": self._majors_cache,
            "risk": {
                "halted": self.halted,
                "maxExposurePct": self.settings.max_exposure_pct,
                "maxOpenPositions": self.settings.max_open_positions,
                "dailyLossLimitPct": self.settings.daily_loss_limit_pct,
                "riskPerTradePct": self.settings.risk_per_trade_pct,
                "confidenceThreshold": self.settings.confidence_threshold,
                "dayStartEquity": round(self.day_start_equity or 0, 2),
                "dayPnlPct": round(
                    ((wallet["equity"] - self.day_start_equity) / self.day_start_equity * 100.0)
                    if self.day_start_equity else 0.0, 2
                ),
            },
            "equityCurve": list(self.equity_history)[-120:],
            "settings": {
                "auto_trade": self.auto_trade,
                "mode": "paper",
                "broker": "simulated spot",
                "tickSeconds": self.settings.tick_seconds,
                "marketsRefreshSeconds": self.settings.markets_refresh_seconds,
            },
            "status": {
                "last_refresh": self.last_refresh,
                "last_error": self.last_error or self.data.error,
                "gecko_error": self.gecko.last_error,
                "binance_error": self.binance.last_error,
                "coins_ready": len(self.data.rows),
                "live_coins": sum(1 for r in self.data.rows.values() if r.get("pair")),
            },
            "events": list(self.events)[:24],
            "timeframes": list(TIMEFRAMES.keys()),
        }

    @staticmethod
    def _compact(row: dict, with_spark: bool = True) -> dict:
        out = {
            "id": row["id"],
            "symbol": row["symbol"],
            "name": row["name"],
            "image": row["image"],
            "price": row["price"],
            "rank": row["rank"],
            "mcap": row["mcap"],
            "vol": row["vol"],
            "chg1h": row["chg1h"],
            "chg24h": row["chg24h"],
            "chg7d": row["chg7d"],
            "high24": row["high24"],
            "low24": row["low24"],
            "ath": row["ath"],
            "athChg": row["athChg"],
            "starred": row["starred"],
            "added": row["added"],
            "pair": row["pair"],
            "live": row.get("live", False),
            "supply": row.get("supply"),
            "maxSupply": row.get("maxSupply"),
            "fdv": row.get("fdv"),
            "athDate": row.get("athDate"),
        }
        if with_spark:
            out["spark"] = row["spark"]
        return out


def _last_value(series: Optional[list]) -> Optional[float]:
    if not series:
        return None
    for value in reversed(series):
        if value is not None:
            return float(value)
    return None


def _value_at(series: Optional[list], offset: int) -> Optional[float]:
    if not series:
        return None
    try:
        value = series[offset]
    except IndexError:
        return None
    return float(value) if value is not None else None


def _signed(a: Optional[float], b: Optional[float], scale: float = 1.0) -> Optional[float]:
    if a in (None, 0) or not b:
        return None
    return (float(a) / float(b) - 1.0) * scale
