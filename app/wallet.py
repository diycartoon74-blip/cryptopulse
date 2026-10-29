"""Paper wallet: cash in the display currency (USDT by default), spot
positions, market/limit orders and history.

Nothing here touches a real exchange. Fills happen against the live mark price
with a simulated taker fee, so the numbers are close to what a small spot trade
would cost without pretending to be brokerage.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from .config import WALLET_PATH
from .storage import JsonStore


class WalletError(ValueError):
    """Raised for anything the user should see as a rejection reason."""


FEE_RATE = 0.001  # 0.10% spot taker fee


def _round_qty(value: float) -> float:
    """Six significant units, so BTC and memecoin quantities both stay readable."""
    if value == 0:
        return 0.0
    magnitude = abs(value)
    if magnitude >= 1000:
        return round(value, 4)
    if magnitude >= 1:
        return round(value, 6)
    from math import floor, log10

    digits = min(14, max(8, 8 - int(floor(log10(magnitude)))))
    return round(value, digits)


def _price_tick(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 1:
        return 4
    if price >= 0.01:
        return 6
    return 10


class PaperWallet:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.store = JsonStore(WALLET_PATH)
        self._lock = threading.RLock()
        saved = self.store.load(None) or {}
        self.currency: str = settings.display_currency.upper()
        self.cash: float = float(saved.get("cash", settings.capital))
        self.positions: dict[str, dict] = {k: dict(v) for k, v in (saved.get("positions") or {}).items()}
        self.orders: list[dict] = list(saved.get("orders") or [])
        self.trades: list[dict] = list(saved.get("trades") or [])
        self.ledger: list[dict] = list(saved.get("ledger") or [])
        self.lifetime: dict = dict(saved.get("lifetime") or {"deposited": 0.0, "withdrawn": 0.0, "fees": 0.0, "realised": 0.0})
        self.opened_at: float = float(saved.get("opened_at") or time.time())
        if not self.ledger:
            # The starting balance is paper money the wallet was handed, so count
            # it as a deposit - otherwise "Deposited" reads 0 next to that entry.
            self.lifetime["deposited"] = round(float(self.lifetime.get("deposited", 0.0)) + self.cash, 2)
            self._entry("deposit", self.cash, "starting paper capital")
            self._persist()

    # ------------------------------------------------------------ persistence
    def _persist(self) -> None:
        self.store.save(
            {
                "cash": self.cash,
                "positions": self.positions,
                "orders": self.orders[-60:],
                "trades": self.trades[-200:],
                "ledger": self.ledger[-100:],
                "lifetime": self.lifetime,
                "opened_at": self.opened_at,
                "saved_at": time.time(),
            }
        )

    def _entry(self, kind: str, amount: float, note: str) -> dict:
        row = {
            "id": uuid.uuid4().hex[:8],
            "at": int(time.time() * 1000),
            "kind": kind,
            "amount": round(amount, 2),
            "note": note,
            "cashAfter": round(self.cash, 2),
        }
        self.ledger.append(row)
        self.ledger = self.ledger[-100:]
        return row

    # ----------------------------------------------------------------- cash
    def deposit(self, amount: float, note: str = "") -> dict:
        amount = round(float(amount), 2)
        if amount <= 0:
            raise WalletError("deposit must be a positive amount")
        if amount > 10_000_000:
            raise WalletError("keep deposits under 10,000,000")
        with self._lock:
            self.cash += amount
            self.lifetime["deposited"] = round(float(self.lifetime.get("deposited", 0.0)) + amount, 2)
            entry = self._entry("deposit", amount, note or "paper deposit")
            self._persist()
        return entry

    def withdraw(self, amount: float, note: str = "") -> dict:
        amount = round(float(amount), 2)
        if amount <= 0:
            raise WalletError("withdrawal must be a positive amount")
        with self._lock:
            if amount > self.cash:
                raise WalletError(f"only {self.currency} {self.cash:,.2f} available")
            self.cash -= amount
            self.lifetime["withdrawn"] = round(float(self.lifetime.get("withdrawn", 0.0)) + amount, 2)
            entry = self._entry("withdraw", -amount, note or "paper withdrawal")
            self._persist()
        return entry

    # -------------------------------------------------------------- positions
    def position(self, coin_id: str) -> Optional[dict]:
        return self.positions.get(coin_id)

    def _open(self, coin_id: str, name: str, symbol: str) -> dict:
        pos = self.positions.get(coin_id)
        if not pos:
            pos = {"coin": coin_id, "name": name, "symbol": symbol, "qty": 0.0, "cost": 0.0}
            self.positions[coin_id] = pos
        return pos

    def buy(self, coin_id: str, name: str, symbol: str, price: float, qty: Optional[float] = None,
            quote_amount: Optional[float] = None, reason: str = "manual", order_id: Optional[str] = None) -> dict:
        price = float(price)
        if price <= 0:
            raise WalletError("no valid market price for this coin")
        if len(self.positions) >= self.settings.max_open_positions and coin_id not in self.positions:
            raise WalletError(f"position limit reached ({self.settings.max_open_positions} coins)")
        with self._lock:
            quantity = _round_qty(float(qty)) if qty else 0.0
            if not quantity and quote_amount:
                quantity = _round_qty(float(quote_amount) / price)
            if quantity <= 0:
                raise WalletError("quantity must be greater than zero")
            gross = quantity * price
            fee = gross * FEE_RATE
            total = gross + fee
            if total > self.cash + 1e-9:
                raise WalletError(
                    f"needs {self.currency} {total:,.2f} (fee included) but only {self.currency} {self.cash:,.2f} free"
                )
            self.cash -= total
            self.lifetime["fees"] = round(float(self.lifetime.get("fees", 0.0)) + fee, 4)
            pos = self._open(coin_id, name, symbol)
            new_qty = pos["qty"] + quantity
            pos["cost"] = round(pos["cost"] + total, 4)
            pos["qty"] = _round_qty(new_qty)
            pos["avg"] = round(pos["cost"] / new_qty, _price_tick(pos["cost"] / new_qty)) if new_qty else 0.0
            pos["openedAt"] = pos.get("openedAt") or int(time.time() * 1000)
            pos["lastBuyAt"] = int(time.time() * 1000)
            trade = self._trade("buy", pos, price, quantity, fee, reason, order_id)
            self._persist()
            return trade

    def sell(self, coin_id: str, price: float, qty: Optional[float] = None, fraction: Optional[float] = None,
             reason: str = "manual", order_id: Optional[str] = None) -> dict:
        price = float(price)
        pos = self.positions.get(coin_id)
        if not pos or pos["qty"] <= 0:
            raise WalletError("you have no open position in this coin")
        if price <= 0:
            raise WalletError("no valid market price for this coin")
        with self._lock:
            quantity = _round_qty(float(qty)) if qty else 0.0
            if not quantity and fraction:
                quantity = _round_qty(pos["qty"] * max(0.0, min(1.0, float(fraction))))
            if quantity <= 0:
                raise WalletError("quantity must be greater than zero")
            if quantity > pos["qty"] + 1e-12:
                quantity = pos["qty"]
            gross = quantity * price
            fee = gross * FEE_RATE
            proceeds = gross - fee
            cost_basis = pos["avg"] * quantity
            realised = proceeds - cost_basis
            self.cash += proceeds
            self.lifetime["fees"] = round(float(self.lifetime.get("fees", 0.0)) + fee, 4)
            self.lifetime["realised"] = round(float(self.lifetime.get("realised", 0.0)) + realised, 2)
            remaining = _round_qty(pos["qty"] - quantity)
            pos["qty"] = remaining
            pos["cost"] = round(max(0.0, pos["cost"] - cost_basis), 4)
            pos["lastSellAt"] = int(time.time() * 1000)
            if remaining <= 0:
                pos["qty"] = 0.0
                pos["closed"] = True
            trade = self._trade("sell", pos, price, quantity, fee, reason, order_id, realised)
            if pos.get("closed"):
                self.positions.pop(coin_id, None)
            self._persist()
            return trade

    def _trade(self, side: str, pos: dict, price: float, qty: float, fee: float, reason: str,
               order_id: Optional[str] = None, realised: Optional[float] = None) -> dict:
        trade = {
            "id": uuid.uuid4().hex[:8],
            "at": int(time.time() * 1000),
            "side": side,
            "coin": pos["coin"],
            "symbol": pos["symbol"],
            "name": pos["name"],
            "price": round(price, _price_tick(price)),
            "qty": qty,
            "value": round(price * qty, 2),
            "fee": round(fee, 4),
            "reason": reason,
            "orderId": order_id,
            "realised": round(realised, 2) if realised is not None else None,
        }
        self.trades.append(trade)
        self.trades = self.trades[-200:]
        return trade

    # ----------------------------------------------------------------- orders
    def place_limit(self, coin_id: str, name: str, symbol: str, side: str, qty: float, price: float) -> dict:
        side = "buy" if str(side).lower().startswith("b") else "sell"
        qty = _round_qty(float(qty))
        price = float(price)
        if qty <= 0 or price <= 0:
            raise WalletError("quantity and price must both be positive")
        with self._lock:
            if side == "buy":
                needed = qty * price * (1 + FEE_RATE)
                if needed > self.cash + 1e-9:
                    raise WalletError(f"reserve needs {self.currency} {needed:,.2f}, only {self.cash:,.2f} free")
            else:
                held = self.positions.get(coin_id, {}).get("qty", 0.0)
                if qty > held + 1e-12:
                    raise WalletError(f"you only hold {held:g} {symbol}")
            order = {
                "id": uuid.uuid4().hex[:8],
                "at": int(time.time() * 1000),
                "coin": coin_id,
                "symbol": symbol,
                "name": name,
                "side": side,
                "qty": qty,
                "price": round(price, _price_tick(price)),
                "status": "open",
            }
            self.orders.append(order)
            self.orders = self.orders[-60:]
            self._persist()
            return order

    def cancel_order(self, order_id: str) -> dict:
        with self._lock:
            for order in self.orders:
                if order["id"] == order_id and order["status"] == "open":
                    order["status"] = "cancelled"
                    order["closedAt"] = int(time.time() * 1000)
                    self._persist()
                    return order
        raise WalletError("open order not found")

    def fill_orders(self, marks: dict[str, float]) -> list[dict]:
        """Fill pending limit orders whose price has been crossed."""
        fills: list[dict] = []
        with self._lock:
            for order in self.orders:
                if order["status"] != "open":
                    continue
                price = marks.get(order["coin"])
                if not price:
                    continue
                if order["side"] == "buy" and price <= order["price"]:
                    order["status"] = "filled"
                    order["filledAt"] = int(time.time() * 1000)
                    try:
                        trade = self.buy(order["coin"], order["name"], order["symbol"], price,
                                         qty=order["qty"], reason="limit buy", order_id=order["id"])
                        fills.append({"order": order, "trade": trade})
                    except WalletError as exc:
                        order["status"] = "rejected"
                        order["error"] = str(exc)
                elif order["side"] == "sell" and price >= order["price"]:
                    order["status"] = "filled"
                    order["filledAt"] = int(time.time() * 1000)
                    try:
                        trade = self.sell(order["coin"], price, qty=order["qty"],
                                          reason="limit sell", order_id=order["id"])
                        fills.append({"order": order, "trade": trade})
                    except WalletError as exc:
                        order["status"] = "rejected"
                        order["error"] = str(exc)
            if fills:
                self._persist()
        return fills

    # ---------------------------------------------------------------- marks
    def close_position(self, coin_id: str, price: float, reason: str = "manual close") -> dict:
        return self.sell(coin_id, price, fraction=1.0, reason=reason)

    def square_off(self, marks: dict[str, float]) -> list[dict]:
        closed = []
        for coin_id in list(self.positions):
            price = marks.get(coin_id)
            if price:
                closed.append(self.sell(coin_id, price, fraction=1.0, reason="square off all"))
        return closed

    def holdings_value(self, marks: dict[str, float]) -> tuple[float, float]:
        """(market value, unrealised P&L)."""
        value = pnl = 0.0
        for coin_id, pos in self.positions.items():
            price = marks.get(coin_id) or 0.0
            value += pos["qty"] * price
            pnl += pos["qty"] * price - pos["cost"]
        return round(value, 2), round(pnl, 2)

    def snapshot(self, marks: dict[str, float]) -> dict:
        with self._lock:
            value, unreal = self.holdings_value(marks)
            equity = self.cash + value
            positions = []
            for pos in sorted(self.positions.values(), key=lambda p: -p["cost"]):
                price = marks.get(pos["coin"]) or 0.0
                market = pos["qty"] * price
                pnl = market - pos["cost"]
                positions.append(
                    {
                        **pos,
                        "mark": round(price, _price_tick(price)) if price else None,
                        "value": round(market, 2),
                        "pnl": round(pnl, 2),
                        "pnlPct": round(pnl / pos["cost"] * 100.0, 2) if pos["cost"] else 0.0,
                    }
                )
            return {
                "currency": self.currency,
                "cash": round(self.cash, 2),
                "invested": round(value, 2),
                "equity": round(equity, 2),
                "unrealised": round(unreal, 2),
                "unrealisedPct": round(unreal / (equity or 1) * 100.0, 2),
                "realised": round(float(self.lifetime.get("realised", 0.0)), 2),
                "fees": round(float(self.lifetime.get("fees", 0.0)), 2),
                "deposited": round(float(self.lifetime.get("deposited", 0.0)), 2),
                "withdrawn": round(float(self.lifetime.get("withdrawn", 0.0)), 2),
                "exposurePct": round(value / (equity or 1) * 100.0, 1),
                "positions": positions,
                "orders": self.orders[-25:][::-1],
                "trades": self.trades[-25:][::-1],
                "ledger": self.ledger[-12:][::-1],
                "openCount": len([o for o in self.orders if o["status"] == "open"]),
            }
