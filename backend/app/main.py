"""
main.py — the FastAPI app. Starts the trading engine as a background
thread on startup, and exposes REST endpoints the Next.js frontend
polls for live state. All routes except /health require a valid
Supabase auth token.
"""
import datetime
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fyers_apiv3 import fyersModel
import jwt

from .config import ALLOWED_ORIGINS
from .auth import require_auth
from .engine import get_engine_status, restart_live_feed, start_engine, STRATEGIES
from .charges import get_charges_config, set_charges_config
from .fyers_client import get_connection_status, get_price_history
from .fyers_auth import store_broker_tokens
from app.config import APP_PIN, FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI, FRONTEND_URL, SUPABASE_JWT_SECRET
from app.supabase_client import supabase


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
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
    return {"status": "ok", **get_engine_status()}


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
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)
        return
    try:
        jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
    except Exception:
        await ws.close(code=1008)
        return
    await manager.connect(ws)
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
    session.set_token(received_code)
    response = session.generate_token()
    if "access_token" not in response:
        return RedirectResponse(f"{FRONTEND_URL}/dashboard?fyers_login=failed")
    store_broker_tokens(response)
    restart_live_feed(reason="fyers_oauth_callback")
    return RedirectResponse(f"{FRONTEND_URL}/dashboard?fyers_login=success")


@app.get("/api/algo/{algo_id}/summary")
def algo_summary(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return strategy.broker.summary()


@app.get("/api/algo/{algo_id}/positions")
def algo_positions(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return strategy.broker.open_positions()


@app.get("/api/algo/{algo_id}/trades")
def algo_trades(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return strategy.broker.recent_trades()


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


@app.get("/api/algo/{algo_id}/scan-results")
def get_scan_results(algo_id: str, _user=Depends(require_auth)):
    from app.engine import SCAN_RESULTS
    return SCAN_RESULTS.get(algo_id, {
        "algo_id": algo_id,
        "message": "No scan run yet today. Results appear at 9:16 AM."
    })


@app.get("/api/compare")
def compare_algos(_user=Depends(require_auth)):
    return {algo_id: strategy.broker.summary() for algo_id, strategy in STRATEGIES.items()}


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

