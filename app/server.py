"""FastAPI app: serves the dashboard, REST state and the WebSocket stream."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .chartdata import TIMEFRAMES
from .config import WEB_DIR, Settings
from .engine import TradingEngine
from .wallet import WalletError

settings = Settings()
engine = TradingEngine(settings)
INDEX = Path(WEB_DIR) / "index.html"

REFRESH_SECONDS = settings.markets_refresh_seconds
PUSH_SECONDS = settings.push_seconds
TICK_SECONDS = settings.tick_seconds
_latest_tick: dict | None = None


async def _refresh_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(engine.refresh)
        except Exception as exc:  # the loop must survive a bad upstream
            engine.log("error", f"refresh loop: {type(exc).__name__}: {exc}")
        await asyncio.sleep(REFRESH_SECONDS)


async def _tick_loop() -> None:
    """One-second tape for the focused coin's forming candle.

    Running it once in the app keeps every browser tab on the same print and lets
    the REST fallback reuse the frame.
    """
    global _latest_tick
    while True:
        started = time.monotonic()
        try:
            frame = await asyncio.to_thread(engine.ticks)
            if frame.get("candle"):
                _latest_tick = frame
        except Exception as exc:
            engine.log("error", f"tick loop: {type(exc).__name__}: {exc}")
        await asyncio.sleep(max(0.2, TICK_SECONDS - (time.monotonic() - started)))


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(_refresh_loop()), asyncio.create_task(_tick_loop())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CryptoPulse", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------------- requests
class WalletTxn(BaseModel):
    amount: float
    note: Optional[str] = None


class FocusUpdate(BaseModel):
    coin: Optional[str] = None
    tf: Optional[str] = None


class StarUpdate(BaseModel):
    coin: str
    on: bool = True


class TradeRequest(BaseModel):
    coin: str
    qty: Optional[float] = None
    fraction: Optional[float] = None
    quote_amount: Optional[float] = None


class OrderRequest(BaseModel):
    coin: str
    side: str = "buy"
    qty: float
    price: Optional[float] = None


class SettingsUpdate(BaseModel):
    auto_trade: Optional[bool] = None


# ---------------------------------------------------------------------- pages
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def state() -> dict:
    return await asyncio.to_thread(engine.snapshot, True)


@app.get("/api/ticks")
async def ticks() -> dict:
    """Last one-second tape frame (used when WebSockets are unavailable)."""
    if _latest_tick is None:
        return await asyncio.to_thread(engine.ticks)
    return _latest_tick


@app.get("/api/health")
async def health() -> dict:
    snapshot = await asyncio.to_thread(engine.snapshot)
    return {
        "status": "ok" if snapshot["coins"] else "warming",
        "mode": snapshot["settings"]["mode"],
        "coins_ready": snapshot["status"]["coins_ready"],
        "live_coins": snapshot["status"]["live_coins"],
        "last_refresh": snapshot["status"]["last_refresh"],
        "last_error": snapshot["status"]["last_error"],
        "wallet_backend": "local json",
        "auto_trade": snapshot["settings"]["auto_trade"],
        "halted": snapshot["risk"]["halted"],
    }


@app.get("/api/chart/{coin_id}")
async def chart(coin_id: str, tf: str = "5m") -> dict:
    coin_id = coin_id.strip().lower()
    if tf not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"unknown timeframe: {tf}")
    if not engine.data.get(coin_id):
        await asyncio.to_thread(engine.data.add_coin, coin_id)
        if not engine.data.get(coin_id):
            raise HTTPException(status_code=404, detail=f"unknown coin: {coin_id}")
    try:
        return await asyncio.to_thread(engine.charts.get, coin_id, tf)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"chart unavailable: {exc}")


@app.get("/api/search")
async def search(q: str = "") -> dict:
    if not q.strip():
        return {"query": q, "results": []}
    return await asyncio.to_thread(engine.data.search, q)


@app.get("/api/coin/{coin_id}")
async def coin(coin_id: str) -> dict:
    row = engine.data.get(coin_id.strip().lower())
    if not row:
        raise HTTPException(status_code=404, detail=f"unknown coin: {coin_id}")
    return row


# ----------------------------------------------------------------- interaction
@app.post("/api/focus")
async def focus(update: FocusUpdate) -> dict:
    await asyncio.to_thread(engine.set_focus, update.coin or engine.focus, update.tf)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"focus": engine.focus, "tf": engine.tf}
    return snapshot


@app.post("/api/star")
async def star(update: StarUpdate) -> dict:
    await asyncio.to_thread(engine.data.set_star, update.coin, update.on)
    return await asyncio.to_thread(engine.snapshot)


@app.post("/api/coin/{coin_id}/pin")
async def pin_coin(coin_id: str) -> dict:
    result = await asyncio.to_thread(engine.data.add_coin, coin_id)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = result
    return snapshot


@app.post("/api/coin/{coin_id}/unpin")
async def unpin_coin(coin_id: str) -> dict:
    result = await asyncio.to_thread(engine.data.remove_coin, coin_id)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = result
    return snapshot


@app.post("/api/settings")
async def update_settings(update: SettingsUpdate) -> dict:
    if update.auto_trade is not None:
        engine.set_auto(update.auto_trade)
    return await asyncio.to_thread(engine.snapshot)


@app.post("/api/resume")
async def resume() -> dict:
    engine.resume()
    return await asyncio.to_thread(engine.snapshot)


@app.post("/api/refresh")
async def refresh_now() -> dict:
    await asyncio.to_thread(engine.refresh)
    return await asyncio.to_thread(engine.snapshot)


@app.post("/api/wallet/deposit")
async def wallet_deposit(txn: WalletTxn) -> dict:
    try:
        entry = await asyncio.to_thread(engine.deposit, txn.amount, txn.note or "")
    except WalletError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"wallet": "deposit", "entry": entry}
    return snapshot


@app.post("/api/wallet/withdraw")
async def wallet_withdraw(txn: WalletTxn) -> dict:
    try:
        entry = await asyncio.to_thread(engine.withdraw, txn.amount, txn.note or "")
    except WalletError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"wallet": "withdraw", "entry": entry}
    return snapshot


@app.post("/api/trade/buy")
async def buy(req: TradeRequest) -> dict:
    result = await asyncio.to_thread(
        engine.open_position, req.coin, req.qty, req.fraction or 1.0, req.quote_amount
    )
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"trade": result}
    if not result.get("ok"):
        snapshot["error"] = result.get("error")
    return snapshot


@app.post("/api/trade/sell")
async def sell(req: TradeRequest) -> dict:
    result = await asyncio.to_thread(
        engine.close_position, req.coin, req.fraction if req.fraction is not None else 1.0
    )
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"trade": result}
    if not result.get("ok"):
        snapshot["error"] = result.get("error")
    return snapshot


@app.post("/api/signals/{signal_id}/execute")
async def execute_signal(signal_id: str) -> dict:
    result = await asyncio.to_thread(engine.execute_manual, signal_id)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"trade": result}
    return snapshot


@app.post("/api/orders")
async def limit_order(req: OrderRequest) -> dict:
    result = await asyncio.to_thread(engine.place_order, req.coin, req.side, req.qty, req.price)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"order": result}
    if not result.get("ok"):
        snapshot["error"] = result.get("error")
    return snapshot


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: str) -> dict:
    result = await asyncio.to_thread(engine.cancel_order, order_id)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"order": result}
    return snapshot


@app.post("/api/positions/{coin_id}/close")
async def close_position(coin_id: str, fraction: float = 1.0) -> dict:
    result = await asyncio.to_thread(engine.close_position, coin_id, fraction)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"closed": result}
    return snapshot


@app.post("/api/square-off")
async def square_off() -> dict:
    closed = await asyncio.to_thread(engine.square_off_all)
    snapshot = await asyncio.to_thread(engine.snapshot)
    snapshot["action"] = {"square_off": closed}
    return snapshot


# ------------------------------------------------------------------- streaming
@app.websocket("/ws")
async def stream(ws: WebSocket) -> None:
    await ws.accept()
    sent_tick = None
    last_state = 0.0
    first_push = True
    try:
        while True:
            now = time.monotonic()
            if now - last_state >= PUSH_SECONDS:
                # The first push carries the sparkline curves; later ones only
                # refresh them once per market cycle, so the stream stays small.
                payload = await asyncio.to_thread(
                    engine.snapshot, True if first_push else "auto"
                )
                first_push = False
                payload["type"] = "state"
                await ws.send_json(payload)
                last_state = now
            elif _latest_tick is not None and _latest_tick is not sent_tick:
                await ws.send_json(_latest_tick)
                sent_tick = _latest_tick
            await asyncio.sleep(0.25)
    except (WebSocketDisconnect, RuntimeError):
        return
    except Exception:
        with suppress(Exception):
            await ws.close()
