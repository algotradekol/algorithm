"""
main.py — the FastAPI app. Starts the trading engine as a background
thread on startup, and exposes REST endpoints the Next.js frontend
polls for live state. All routes except /health require a valid
Supabase auth token.
"""
import datetime
import asyncio
import json
import math
import threading
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fyers_apiv3 import fyersModel
import jwt

from .config import ALLOWED_ORIGINS
from .auth import require_auth
from .engine import attach_entry_triggers, enrich_positions_with_ltp, get_engine_status, last_ltp, restart_live_feed, start_engine, STRATEGIES
from .charges import get_charges_config, set_charges_config
from .fyers_client import get_connection_status, get_price_history
from .fyers_auth import exchange_auth_code, store_broker_tokens
from app.config import APP_PIN, FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI, FRONTEND_URL, SUPABASE_JWT_SECRET
from app.supabase_client import supabase


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket, already_accepted: bool = False):
        if not already_accepted:
            await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        import json
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.broadcaster import set_manager
    set_manager(manager)
    # Start engine in a background thread so it doesn't block FastAPI startup
    engine_thread = threading.Thread(target=start_engine, daemon=True)
    engine_thread.start()
    yield


app = FastAPI(title="Algo Paper Trading API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    # Intentionally lightweight: this endpoint is used for Railway's
    # deployment healthcheck and must respond immediately, without waiting
    # on the background trading engine's startup (NSE500 watchlist load,
    # strategy init, Fyers connection). Detailed engine status is available
    # via the authenticated /api/engine/status endpoint.
    return {"status": "ok"}


@app.get("/api/engine/status")
def engine_status(_user=Depends(require_auth)):
    return get_engine_status()


def get_strategy_or_raise(algo_id: str):
    strategy = STRATEGIES.get(algo_id)
    if strategy:
        return strategy

    status = get_engine_status()
    if status["state"] != "running":
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Trading engine is not ready yet.",
                **status,
            },
        )
    raise HTTPException(404, f"No such algo: {algo_id}")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Browser WebSocket clients cannot add an Authorization header. Receive
    # authentication as the first message instead of a URL query parameter so
    # Railway request logs never retain the user's JWT.
    await ws.accept()
    try:
        first_message = await asyncio.wait_for(ws.receive_text(), timeout=10)
        token = json.loads(first_message).get("token")
        if not token:
            raise ValueError("WebSocket authentication token is missing")
        jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
    except Exception:
        await ws.close(code=1008)
        return
    await manager.connect(ws, already_accepted=True)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.post("/api/pin-login")
def pin_login(payload: dict):
    if payload.get("pin") != APP_PIN:
        raise HTTPException(status_code=401, detail="Invalid PIN")
    if not SUPABASE_JWT_SECRET:
        raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET is not configured")

    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(hours=12)
    token = jwt.encode(
        {
            "sub": "pin-login",
            "role": "authenticated",
            "aud": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "login_method": "pin",
        },
        SUPABASE_JWT_SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": 12 * 60 * 60}


@app.get("/api/fyers/login-url")
def fyers_login_url(_user=Depends(require_auth)):
    session = fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    return {"url": session.generate_authcode()}


@app.get("/api/fyers/status")
def fyers_status(_user=Depends(require_auth)):
    return get_connection_status()


@app.post("/api/fyers/refresh-token")
def fyers_refresh_token(_user=Depends(require_auth)):
    from app.engine import try_refresh_access_token
    if not try_refresh_access_token(reason="api_manual"):
        raise HTTPException(status_code=400, detail=get_engine_status().get("last_token_refresh_error") or "Fyers token refresh failed")
    return {"status": "ok", "message": "Fyers access token refreshed from refresh token."}


@app.get("/api/fyers/token-status")
def fyers_token_status(_user=Depends(require_auth)):
    from app.fyers_auth import get_token_status
    return get_token_status()


@app.get("/api/ai/sessions")
def ai_sessions(_user=Depends(require_auth)):
    from app.ai_assistant import list_sessions
    return list_sessions(_user.get("sub", "unknown"))


@app.post("/api/ai/sessions")
def ai_create_session(payload: dict, _user=Depends(require_auth)):
    from app.ai_assistant import create_session
    return create_session(_user.get("sub", "unknown"), payload.get("title") or "New chat")


@app.get("/api/ai/sessions/{session_id}/messages")
def ai_messages(session_id: str, _user=Depends(require_auth)):
    from app.ai_assistant import get_messages
    return get_messages(session_id)


@app.delete("/api/ai/sessions/{session_id}")
def ai_delete_session(session_id: str, _user=Depends(require_auth)):
    from app.ai_assistant import delete_session
    return delete_session(_user.get("sub", "unknown"), session_id)


@app.post("/api/ai/chat")
def ai_chat(payload: dict, _user=Depends(require_auth)):
    from app.ai_assistant import AIProviderError, AIProviderRateLimitError, send_message
    try:
        return send_message(
            _user.get("sub", "unknown"),
            payload.get("session_id"),
            payload.get("message", ""),
            payload.get("page_context") or {},
        )
    except AIProviderRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/fyers/callback")
def fyers_callback(auth_code: str = None, code: str = None):
    received_code = auth_code or code
    if not received_code:
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?fyers_login=failed")
    session = fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    try:
        response = exchange_auth_code(received_code)
    except Exception as exc:
        print(f"[fyers] OAuth callback exchange failed: {exc}")
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?fyers_login=failed")
    store_broker_tokens(response)
    restart_live_feed(reason="fyers_oauth_callback")
    return RedirectResponse(f"{FRONTEND_URL}/dashboard?fyers_login=success")


@app.get("/api/algo/{algo_id}/summary")
def algo_summary(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    summary = strategy.broker.summary()
    settings = getattr(strategy, "settings", None) or {}
    return {
        **summary,
        "max_trades_per_day": settings.get("max_trades_per_day", 10),
        "max_buy_trades": settings.get("max_buy_trades", 5),
        "max_sell_trades": settings.get("max_sell_trades", 5),
    }


@app.get("/api/algo/{algo_id}/positions")
def algo_positions(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return attach_entry_triggers(algo_id, enrich_positions_with_ltp(strategy.broker.open_positions()))


@app.post("/api/algo/{algo_id}/positions/{position_id}/exit")
def exit_position(algo_id: str, position_id: str, _user=Depends(require_auth)):
    """Manually close one open paper position at its latest live Fyers price."""
    strategy = get_strategy_or_raise(algo_id)
    position = next(
        (row for row in strategy.broker.open_positions() if str(row.get("id")) == position_id),
        None,
    )
    if position is None:
        raise HTTPException(status_code=404, detail="Open position not found. It may already have closed.")

    exit_price = last_ltp.get(position["symbol"])
    if exit_price is None:
        raise HTTPException(
            status_code=409,
            detail="No live Fyers price is available for this symbol, so it cannot be manually exited safely.",
        )

    strategy.broker.close_trade(position, float(exit_price), "MANUAL_EXIT")
    return {
        "status": "closed",
        "algo_id": algo_id,
        "position_id": position_id,
        "symbol": position["symbol"],
        "exit_price": float(exit_price),
        "exit_reason": "MANUAL_EXIT",
    }


@app.post("/api/algo/{algo_id}/manual-trade")
def manual_trade(algo_id: str, payload: dict, _user=Depends(require_auth)):
    """Open a paper position directly from the dashboard, bypassing daily caps."""
    strategy = get_strategy_or_raise(algo_id)
    symbol = str(payload.get("symbol") or "").strip()
    side = str(payload.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="Side must be BUY or SELL.")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required.")

    open_positions = strategy.broker.open_positions()
    if any(row.get("symbol") == symbol and str(row.get("status") or "").lower() == "open" for row in open_positions):
        raise HTTPException(status_code=409, detail="This symbol already has an open paper position.")

    price = payload.get("price")
    if price is None:
        price = last_ltp.get(symbol)
    try:
        entry_price = float(price)
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="No live price is available for this symbol yet.")
    if entry_price <= 0 or not math.isfinite(entry_price):
        raise HTTPException(status_code=409, detail="No live price is available for this symbol yet.")

    capital_per_trade = float(getattr(strategy, "settings", {}).get("capital_per_trade") or 0)
    qty = int(capital_per_trade // entry_price)
    if qty < 1:
        raise HTTPException(status_code=400, detail="Capital per trade is below the current share price.")

    settings = getattr(strategy, "settings", {}) or {}
    if side == "BUY":
        sl_price = entry_price * (1 - float(settings.get("sl_pct") or 0) / 100)
        target_price = entry_price * (1 + float(settings.get("target_pct") or 0) / 100)
    else:
        sl_price = entry_price * (1 + float(settings.get("sl_pct") or 0) / 100)
        target_price = entry_price * (1 - float(settings.get("target_pct") or 0) / 100)

    trigger = payload.get("trigger") or "Manual dashboard override; bypassed automated trade caps."
    signal_snapshot = {
        "source": "manual_dashboard",
        "symbol": symbol,
        "side": side,
        "entry_ltp": entry_price,
        "trigger": trigger,
    }
    strategy.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price, trigger, signal_snapshot)
    refreshed_positions = [
        row for row in strategy.broker.open_positions()
        if row.get("symbol") == symbol and row.get("side") == side and str(row.get("status") or "").lower() == "open"
    ]
    position = max(refreshed_positions, key=lambda row: str(row.get("entry_time") or "")) if refreshed_positions else None
    return {
        "status": "opened",
        "algo_id": algo_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "target_price": target_price,
        "position_id": position.get("id") if position else None,
    }


@app.get("/api/algo/{algo_id}/trades")
def algo_trades(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return attach_entry_triggers(algo_id, strategy.broker.recent_trades())


@app.get("/api/algo/{algo_id}/history")
def algo_history(algo_id: str, days: int = Query(default=30, ge=1, le=180), _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return strategy.broker.daily_history(days)


@app.get("/api/algo/{algo_id}/settings")
def get_algo_settings(algo_id: str, _user=Depends(require_auth)):
    from app.strategy_settings import get_settings
    return get_settings(algo_id)


@app.put("/api/algo/{algo_id}/settings")
def update_algo_settings(algo_id: str, settings: dict, _user=Depends(require_auth)):
    from app.strategy_settings import update_settings
    update_settings(algo_id, settings)
    strategy = STRATEGIES.get(algo_id)
    if strategy and hasattr(strategy, "reload_settings"):
        strategy.reload_settings()
    return {"status": "updated", "algo_id": algo_id}


@app.put("/api/algo/{algo_id}/available-cash")
def update_available_cash(algo_id: str, payload: dict, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    try:
        cash = float(payload.get("cash"))
        if not math.isfinite(cash):
            raise ValueError("Available cash must be a valid number.")
        return {"status": "updated", "algo_id": algo_id, "cash": strategy.broker.set_available_cash(cash)}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/algo/{algo_id}/settings/reset")
def reset_algo_settings(algo_id: str, _user=Depends(require_auth)):
    from app.strategy_settings import reset_settings
    settings = reset_settings(algo_id)
    strategy = STRATEGIES.get(algo_id)
    if strategy and hasattr(strategy, "reload_settings"):
        strategy.reload_settings()
    return settings


@app.get("/api/algo/{algo_id}/scan-results")
def get_scan_results(algo_id: str, _user=Depends(require_auth)):
    from app.engine import SCAN_RESULTS
    strategy = get_strategy_or_raise(algo_id)
    result = SCAN_RESULTS.get(algo_id, {
        "algo_id": algo_id,
        "message": "No scan run yet today. Results appear at 9:16 AM."
    })
    schedule_status = getattr(strategy, "schedule_status", None)
    if schedule_status:
        result = {**result, "schedule": schedule_status(datetime.datetime.now(ZoneInfo("Asia/Kolkata")))}
    return result


@app.get("/api/compare")
def compare_algos(_user=Depends(require_auth)):
    return {algo_id: strategy.broker.summary() for algo_id, strategy in STRATEGIES.items()}


@app.get("/api/calendar")
def calendar_days(days: int = Query(default=60, ge=1, le=365), _user=Depends(require_auth)):
    from app.calendar_store import list_calendar_days
    return list_calendar_days(days)


@app.get("/api/calendar/{snapshot_date}")
def calendar_day(snapshot_date: str, _user=Depends(require_auth)):
    from app.calendar_store import get_calendar_day
    return get_calendar_day(snapshot_date)


@app.delete("/api/calendar/{snapshot_date}")
def delete_calendar_date(snapshot_date: str, _user=Depends(require_auth)):
    from app.calendar_store import delete_calendar_day
    return delete_calendar_day(snapshot_date)


@app.delete("/api/calendar/{snapshot_date}/{algo_id}")
def delete_calendar_algo_snapshot(snapshot_date: str, algo_id: str, _user=Depends(require_auth)):
    from app.calendar_store import delete_calendar_snapshot
    return delete_calendar_snapshot(snapshot_date, algo_id)


@app.post("/api/calendar/snapshot")
def calendar_snapshot(payload: dict | None = None, _user=Depends(require_auth)):
    from app.calendar_store import save_dashboard_snapshot
    algo_id = (payload or {}).get("algo_id")
    return save_dashboard_snapshot(algo_id=algo_id, note=(payload or {}).get("note") or "manual")


@app.get("/api/charges")
def read_charges(_user=Depends(require_auth)):
    return get_charges_config()


@app.put("/api/charges")
def update_charges(config: dict, _user=Depends(require_auth)):
    set_charges_config(config)
    return {"status": "updated", "config": config}


@app.get("/api/watchlist")
def watchlist(_user=Depends(require_auth)):
    strategy = next(iter(STRATEGIES.values()), None)
    symbols = strategy.watchlist if strategy else []
    return {"symbols": symbols, "count": len(symbols)}


@app.get("/api/market/history")
def market_history(
    symbol: str = Query(...),
    days: int = Query(default=5, ge=1, le=60),
    resolution: str = Query(default="15"),
    _user=Depends(require_auth),
):
    try:
        history = get_price_history(symbol, resolution=resolution, days=days)
        candles = history["candles"]
        warning = history["warning"]
        try:
            from app.calendar_store import store_market_candles
            store_market_candles(symbol, resolution, candles)
        except Exception as store_exc:
            warning = warning or f"History loaded but candle persistence failed: {store_exc}"
    except Exception as exc:
        candles = []
        warning = str(exc)
    return {
        "symbol": symbol,
        "resolution": resolution,
        "days": days,
        "candles": candles,
        "warning": warning,
    }


@app.post("/api/backtests")
def create_backtest(payload: dict, _user=Depends(require_auth)):
    # Read the engine module's current watchlist at request time. start_engine
    # replaces this list after symbol loading, so a module-level imported alias
    # would remain the initial empty list.
    from app import engine
    from app.backtest import start_backtest
    from app.mcx_symbols import get_active_mcx_contract
    algo_id = str(payload.get("algo_id") or "")
    # Accept date for existing clients while range-aware clients send both fields.
    start_date = str(payload.get("start_date") or payload.get("date") or "")
    end_date = str(payload.get("end_date") or start_date)
    try:
        if algo_id == "algo3":
            symbol = get_active_mcx_contract("SILVERMIC")
            if not symbol:
                raise ValueError("Could not resolve the active Silver Micro MCX contract.")
            return start_backtest(algo_id, start_date, end_date, [symbol])
        return start_backtest(algo_id, start_date, end_date, engine.WATCHLIST)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/backtests/{job_id}")
def backtest_status(job_id: str, _user=Depends(require_auth)):
    from app.backtest import get_backtest_job
    job = get_backtest_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backtest job not found. It may predate durable job storage or have been removed.")
    return job

