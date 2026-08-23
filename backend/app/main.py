"""
main.py — the FastAPI app. Starts the trading engine as a background
thread on startup, and exposes REST endpoints the Next.js frontend
polls for live state. All routes except /health require a valid
Supabase auth token.
"""
import datetime
import asyncio
import json
import logging
import math
import threading

# Silence Uvicorn's per-request access log. The Next.js dashboard polls
# ~10 endpoints every 1-2s (positions, trades, feed-status, summary,
# scan-results...), which drowns Railway logs in hundreds of
# `INFO: ... GET /api/... 200 OK` lines per minute and buries the
# actual algo signal / MCX diagnostics we care about. Errors still log
# via uvicorn.error at WARNING and above.
logging.getLogger("uvicorn.access").disabled = True
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from fastapi import FastAPI, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import jwt

try:
    from fyers_apiv3 import fyersModel
except ImportError:  # pragma: no cover - keeps /health alive if SDK is missing
    fyersModel = None

# Log the installed Fyers SDK version at boot. Requirements pin only
# 'fyers-apiv3' (no version), so every Railway deploy pulls latest.
# If Fyers ships a new SDK that changes MCX WebSocket handling, this
# tells us immediately what version is running so we can pin to a
# known-working one via requirements.txt.
try:
    from importlib.metadata import version as _pkg_version
    _fyers_sdk_version = _pkg_version("fyers-apiv3")
    print(f"[boot] fyers-apiv3 version: {_fyers_sdk_version}")
except Exception as _exc:  # pragma: no cover
    print(f"[boot] fyers-apiv3 version lookup failed: {_exc}")

from .config import ALLOWED_ORIGINS, APP_PIN, FRONTEND_URL, SUPABASE_JWT_SECRET
from .auth import require_auth
from .engine import attach_entry_triggers, enrich_positions_with_ltp, get_engine_status, last_ltp, restart_live_feed, start_engine, stop_live_feed, STRATEGIES, _clear_token_expired
from .charges import get_charges_config, set_charges_config
from .audit_log import audit_log
from .fyers_client import get_broker_orders, get_broker_positions, get_connection_status, get_price_history, get_wallet_balance
from .fyers_auth import disconnect_broker_tokens, exchange_auth_code, store_broker_tokens
from .runtime_mode import (
    clear_pending_fyers_login_mode,
    clear_pending_fyers_login_origin,
    get_active_broker_key,
    get_fyers_config,
    get_pending_fyers_login_origin,
    get_pending_fyers_login_mode,
    get_runtime_trading_mode,
    normalize_trading_mode,
    set_pending_fyers_login_origin,
    set_pending_fyers_login_mode,
)
from .supabase_client import supabase
from .silver_setup_history import get_setup_history
from .timezone import IST


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
    from .broadcaster import set_manager
    set_manager(manager)
    # Start engine in a background thread so it doesn't block FastAPI startup
    engine_thread = threading.Thread(target=start_engine, daemon=True)
    engine_thread.start()
    yield


app = FastAPI(title="Algo Paper Trading API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.kolkatalgo\.in|https://.*\.vercel\.app|http://localhost(:\d+)?",
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
    except WebSocketDisconnect:
        # Client closed the socket before sending auth (tab close, refresh mid-handshake).
        print("[ws] client disconnected during auth handshake")
        return
    except asyncio.TimeoutError:
        print("[ws] auth timeout after 10s; closing socket")
        try:
            await ws.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
        return
    except Exception as exc:
        print(f"[ws] auth failed: {type(exc).__name__}: {exc}")
        try:
            await ws.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
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
def fyers_login_url(request: Request, mode: str | None = None, _user=Depends(require_auth)):
    if fyersModel is None:
        raise HTTPException(status_code=503, detail="Fyers SDK is not installed in this environment.")
    requested_mode = normalize_trading_mode(mode or get_runtime_trading_mode())
    fyers_config = get_fyers_config(requested_mode)
    set_pending_fyers_login_origin(request.headers.get("origin") or request.headers.get("referer"))
    session = fyersModel.SessionModel(
        client_id=fyers_config["client_id"],
        secret_key=fyers_config["secret_key"],
        redirect_uri=fyers_config["redirect_uri"],
        response_type="code",
        grant_type="authorization_code",
    )
    auth_url = session.generate_authcode()
    parts = urlsplit(auth_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["state"] = requested_mode
    auth_url = urlunsplit(parts._replace(query=urlencode(query)))
    set_pending_fyers_login_mode(requested_mode)
    audit_log(
        "fyers",
        "login-url generated",
        mode=requested_mode,
        broker=get_active_broker_key(requested_mode),
        client_id=fyers_config["client_id"],
        redirect_uri=fyers_config["redirect_uri"],
    )
    return {"url": auth_url}


@app.get("/api/fyers/status")
def fyers_status(_user=Depends(require_auth)):
    return get_connection_status()


@app.post("/api/fyers/refresh-token")
def fyers_refresh_token(_user=Depends(require_auth)):
    from .engine import try_refresh_access_token
    if not try_refresh_access_token(reason="api_manual"):
        raise HTTPException(status_code=400, detail=get_engine_status().get("last_token_refresh_error") or "Fyers token refresh failed")
    return {"status": "ok", "message": "Fyers access token refreshed from refresh token."}


@app.get("/api/fyers/token-status")
def fyers_token_status(_user=Depends(require_auth)):
    from .fyers_auth import get_token_status
    return get_token_status()


@app.get("/api/fyers/funds")
def fyers_funds(mode: str | None = None, _user=Depends(require_auth)):
    active_mode = get_runtime_trading_mode()
    requested_mode = normalize_trading_mode(mode or active_mode)
    if requested_mode != active_mode:
        raise HTTPException(
            status_code=409,
            detail=f"Trading mode changed to {active_mode}; discard the stale {requested_mode} funds request.",
        )
    broker = get_active_broker_key(active_mode)
    try:
        result = get_wallet_balance(active_mode)
        return {**result, "trading_mode": active_mode, "broker": broker}
    except Exception as exc:
        message = str(exc)
        audit_log(
            "fyers",
            "funds request failed",
            mode=active_mode,
            broker=broker,
            error=message,
        )
        status_code = 409 if "No Fyers access token" in message else 502
        raise HTTPException(status_code=status_code, detail=message)


@app.get("/api/fyers/positions")
def fyers_positions(mode: str | None = None, _user=Depends(require_auth)):
    active_mode = get_runtime_trading_mode()
    requested_mode = normalize_trading_mode(mode or active_mode)
    if requested_mode != active_mode:
        raise HTTPException(
            status_code=409,
            detail=f"Trading mode changed to {active_mode}; discard the stale {requested_mode} positions request.",
        )
    broker = get_active_broker_key(active_mode)
    try:
        result = get_broker_positions(active_mode)
        return {**result, "trading_mode": active_mode, "broker": broker}
    except Exception as exc:
        message = str(exc)
        audit_log(
            "fyers",
            "positions request failed",
            mode=active_mode,
            broker=broker,
            error=message,
        )
        status_code = 409 if "No Fyers access token" in message else 502
        raise HTTPException(status_code=status_code, detail=message)


@app.get("/api/fyers/orders")
def fyers_orders(mode: str | None = None, _user=Depends(require_auth)):
    active_mode = get_runtime_trading_mode()
    requested_mode = normalize_trading_mode(mode or active_mode)
    if requested_mode != active_mode:
        raise HTTPException(
            status_code=409,
            detail=f"Trading mode changed to {active_mode}; discard the stale {requested_mode} orders request.",
        )
    broker = get_active_broker_key(active_mode)
    try:
        result = get_broker_orders(active_mode)
        return {**result, "trading_mode": active_mode, "broker": broker}
    except Exception as exc:
        message = str(exc)
        audit_log(
            "fyers",
            "orders request failed",
            mode=active_mode,
            broker=broker,
            error=message,
        )
        status_code = 409 if "No Fyers access token" in message else 502
        raise HTTPException(status_code=status_code, detail=message)


@app.post("/api/fyers/disconnect")
def fyers_disconnect(force: bool = Query(False), source: str = Query("manual_ui"), _user=Depends(require_auth)):
    # F14 recovery lock: if the engine is currently auto-recovering a
    # transient WS drop, refuse a logout unless the caller explicitly says
    # ?force=true. Prevents the client-panics-and-relogs cascade that
    # kicked off 2026-08-17's rate-limit storm.
    engine_status = get_engine_status()
    if engine_status.get("auto_recovering") and not force:
        eta = max(
            int(engine_status.get("ws_circuit_open_seconds_remaining") or 0),
            int(engine_status.get("ws_next_backoff_seconds") or 0),
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "recovery_in_progress",
                "message": (
                    "System is reconnecting to Fyers automatically. "
                    "Wait a moment or pass ?force=true to override."
                ),
                "eta_seconds": eta,
            },
        )
    mode = get_runtime_trading_mode()
    broker = get_active_broker_key(mode)
    disconnect_broker_tokens(mode, source=source)
    clear_pending_fyers_login_mode()
    clear_pending_fyers_login_origin()
    stop_live_feed(reason=f"fyers_disconnect:{source}")
    audit_log("fyers", "disconnect requested", mode=mode, broker=broker, forced=force, source=source)
    return {
        "status": "ok",
        "message": f"FYERS {mode} connection disconnected.",
        "trading_mode": mode,
        "broker": broker,
    }


@app.get("/api/runtime/trading-mode")
def runtime_trading_mode(_user=Depends(require_auth)):
    return {
        "trading_mode": get_runtime_trading_mode(),
        "broker": get_active_broker_key(),
    }


@app.put("/api/runtime/trading-mode")
def update_runtime_trading_mode(payload: dict, _user=Depends(require_auth)):
    from .engine import apply_trading_mode

    requested_mode = payload.get("trading_mode") or payload.get("mode")
    if not requested_mode:
        raise HTTPException(status_code=400, detail="trading_mode is required")
    try:
        result = apply_trading_mode(str(requested_mode))
        audit_log("runtime", "trading mode updated", requested_mode=str(requested_mode), result=result)
        return result
    except RuntimeError as exc:
        message = str(exc)
        if "cooldown" in message.lower():
            status_code = 429
        elif "Close all open positions" in message:
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=message)


@app.get("/api/ai/sessions")
def ai_sessions(_user=Depends(require_auth)):
    from .ai_assistant import list_sessions
    return list_sessions(_user.get("sub", "unknown"))


@app.post("/api/ai/sessions")
def ai_create_session(payload: dict, _user=Depends(require_auth)):
    from .ai_assistant import create_session
    return create_session(_user.get("sub", "unknown"), payload.get("title") or "New chat")


@app.get("/api/ai/sessions/{session_id}/messages")
def ai_messages(session_id: str, _user=Depends(require_auth)):
    from .ai_assistant import get_messages
    return get_messages(session_id)


@app.delete("/api/ai/sessions/{session_id}")
def ai_delete_session(session_id: str, _user=Depends(require_auth)):
    from .ai_assistant import delete_session
    return delete_session(_user.get("sub", "unknown"), session_id)


@app.post("/api/ai/chat")
def ai_chat(payload: dict, _user=Depends(require_auth)):
    from .ai_assistant import AIProviderError, AIProviderRateLimitError, send_message
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


def _fyers_login_failed_redirect(redirect_base: str, reason: str) -> RedirectResponse:
    """Redirect back to the dashboard with a URL-encoded error reason so the
    frontend can show the user WHY login failed (Cloudflare 429, token
    exchange rejected, config error, etc.) instead of the generic
    'try again' banner."""
    from urllib.parse import quote_plus
    # Short reason label + one-line human-readable explanation combined.
    encoded = quote_plus(reason[:400])
    return RedirectResponse(
        f"{redirect_base}/dashboard?fyers_login=failed&reason={encoded}"
    )


@app.get("/api/fyers/callback")
def fyers_callback(auth_code: str = None, code: str = None, state: str | None = None, mode: str | None = None):
    received_code = auth_code or code
    frontend_origin = get_pending_fyers_login_origin() or FRONTEND_URL
    redirect_base = frontend_origin.rstrip("/")
    if not received_code:
        clear_pending_fyers_login_origin()
        return _fyers_login_failed_redirect(
            redirect_base,
            "Fyers OAuth callback did not include an auth_code — the login popup was closed before completion.",
        )
    if fyersModel is None:
        print("[fyers] OAuth callback received, but fyers_apiv3 is not installed.")
        clear_pending_fyers_login_origin()
        return _fyers_login_failed_redirect(
            redirect_base,
            "Backend server error: fyers_apiv3 package is not installed. Contact the app operator.",
        )
    callback_mode = normalize_trading_mode(state or mode or get_pending_fyers_login_mode() or get_runtime_trading_mode())
    fyers_config = get_fyers_config(callback_mode)
    audit_log(
        "fyers",
        "oauth callback received",
        callback_mode=callback_mode,
        runtime_mode=get_runtime_trading_mode(),
        broker=get_active_broker_key(callback_mode),
        client_id=fyers_config["client_id"],
        redirect_uri=fyers_config["redirect_uri"],
    )
    try:
        response = exchange_auth_code(received_code, mode=callback_mode)
    except Exception as exc:
        audit_log("fyers", "oauth callback exchange failed", mode=callback_mode, error=str(exc))
        clear_pending_fyers_login_mode()
        clear_pending_fyers_login_origin()
        return _fyers_login_failed_redirect(redirect_base, str(exc))
    store_broker_tokens(response, mode=callback_mode)
    clear_pending_fyers_login_mode()
    clear_pending_fyers_login_origin()
    # Fresh token minted — clear the "known expired" flag so watchdog can
    # resume WS handshake attempts immediately.
    _clear_token_expired(f"fresh OAuth callback ({callback_mode})")
    # Fresh OAuth-minted token; bypass any live 429 backoff so the new
    # session starts. Delayed 15s via Timer so Fyers releases the old WS
    # session on their side before we handshake again — stacking a fresh
    # handshake on top of a still-warm one triggered a 429+circuit-open
    # storm on 2026-08-10 (Aug 10). Timer thread is daemon so shutdown
    # isn't blocked.
    threading.Timer(
        15.0,
        restart_live_feed,
        kwargs={"reason": f"fyers_oauth_callback:{callback_mode}", "ignore_backoff": True},
    ).start()
    audit_log("fyers", "oauth callback completed", mode=callback_mode)
    return RedirectResponse(f"{redirect_base}/dashboard?fyers_login=success")


@app.get("/api/algo/{algo_id}/summary")
def algo_summary(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    summary = strategy.broker.summary()
    settings = getattr(strategy, "settings", None) or {}
    feed_status = getattr(strategy, "feed_status", None)
    if callable(feed_status):
        try:
            summary = {**summary, "feed_status": feed_status()}
        except Exception as exc:
            summary = {**summary, "feed_status_error": str(exc)}
    return {
        **summary,
        "max_trades_per_day": settings.get("max_trades_per_day", 10),
        "max_buy_trades": settings.get("max_buy_trades", 5),
        "max_sell_trades": settings.get("max_sell_trades", 5),
        "scan_enabled": bool(settings.get("scan_enabled", True)),
    }


@app.get("/api/algo/{algo_id}/feed-status")
def algo_feed_status(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    feed_status = getattr(strategy, "feed_status", None)
    if not callable(feed_status):
        raise HTTPException(status_code=404, detail="Feed diagnostics are not available for this strategy.")
    try:
        return feed_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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
    try:
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

        settings = getattr(strategy, "settings", {}) or {}
        if algo_id == "algo3":
            # Silver Micro trades in whole lots. Do not reject using
            # capital//price math — MCX futures are margin-based, not
            # cash-equity "can I afford one share" based.
            qty = max(1, int(settings.get("silver_lots", 1) or 1))
        else:
            capital_per_trade = float(settings.get("capital_per_trade") or 0)
            qty = int(capital_per_trade // entry_price)
            if qty < 1:
                raise HTTPException(status_code=400, detail="Capital per trade is below the current share price.")

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
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[manual_trade] failed for {algo_id}: {exc}")
        raise HTTPException(status_code=502, detail=f"Manual trade failed: {exc}")


@app.get("/api/algo/{algo_id}/trades")
def algo_trades(algo_id: str, _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return attach_entry_triggers(algo_id, strategy.broker.recent_trades())


@app.get("/api/algo/{algo_id}/history")
def algo_history(algo_id: str, days: int = Query(default=30, ge=1, le=180), _user=Depends(require_auth)):
    strategy = get_strategy_or_raise(algo_id)
    return strategy.broker.daily_history(days)


@app.get("/api/algo/{algo_id}/setup-history")
def algo_setup_history(
    algo_id: str,
    side: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=180),
    limit: int = Query(default=100, ge=1, le=500),
    current_session_only: bool = Query(default=False),
    live_only: bool = Query(default=False),
    _user=Depends(require_auth),
):
    get_strategy_or_raise(algo_id)
    normalized_side = side.upper() if isinstance(side, str) else None
    if normalized_side not in {None, "BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    return get_setup_history(
        algo_id,
        side=normalized_side,
        days=days,
        limit=limit,
        current_session_only=current_session_only,
        live_only=live_only,
    )


@app.get("/api/algo/{algo_id}/settings")
def get_algo_settings(algo_id: str, _user=Depends(require_auth)):
    from .strategy_settings import get_settings
    return get_settings(algo_id)


@app.put("/api/algo/{algo_id}/settings")
def update_algo_settings(algo_id: str, settings: dict, _user=Depends(require_auth)):
    from .strategy_settings import update_settings
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
    from .strategy_settings import reset_settings
    settings = reset_settings(algo_id)
    strategy = STRATEGIES.get(algo_id)
    if strategy and hasattr(strategy, "reload_settings"):
        strategy.reload_settings()
    return settings


@app.post("/api/algo/{algo_id}/scan-enabled")
def set_algo_scan_enabled(algo_id: str, payload: dict, _user=Depends(require_auth)):
    """Persistent toggle: sets strategy_settings.scan_enabled and reloads
    the strategy so the change takes effect immediately. Stays off (or
    on) until the user toggles again — no midnight auto-reset."""
    from .strategy_settings import get_settings, update_settings
    strategy = get_strategy_or_raise(algo_id)
    active_mode = get_runtime_trading_mode()
    enabled = bool(payload.get("enabled", True))
    current = get_settings(algo_id)
    current["scan_enabled"] = enabled
    # update_settings returns None; the value we just set is what got saved
    # (modulo Supabase-side column filtering, which strategy_settings itself
    # handles). Read back via get_settings to be sure the DB round-trip agrees.
    update_settings(algo_id, current)
    saved = get_settings(algo_id)
    if hasattr(strategy, "reload_settings"):
        strategy.reload_settings()
    audit_log("strategy", "scan_enabled toggled", algo_id=algo_id, enabled=enabled, trading_mode=active_mode)
    return {
        "algo_id": algo_id,
        "trading_mode": active_mode,
        "scan_enabled": bool(saved.get("scan_enabled", True)),
    }


@app.get("/api/algo/{algo_id}/scan-results")
def get_scan_results(algo_id: str, _user=Depends(require_auth)):
    from .engine import SCAN_RESULTS
    strategy = get_strategy_or_raise(algo_id)
    schedule_status = getattr(strategy, "schedule_status", None)
    schedule = schedule_status(datetime.datetime.now(IST)) if schedule_status else {"enabled": False}
    if schedule.get("enabled"):
        default_message = (
            f"Scheduled test is waiting for the {schedule['candle_time']} IST signal candle; "
            f"entry evaluation starts at {schedule['entry_time']} IST."
        )
    else:
        default_message = "No scan run yet today. The 09:15:00 signal is evaluated from 09:16:00 IST."
    result = SCAN_RESULTS.get(algo_id, {
        "algo_id": algo_id,
        "message": default_message,
    })
    if schedule_status:
        result = {**result, "schedule": schedule}
    return result


@app.get("/api/compare")
def compare_algos(_user=Depends(require_auth)):
    return {algo_id: strategy.broker.summary() for algo_id, strategy in STRATEGIES.items()}


@app.get("/api/calendar")
def calendar_days(days: int = Query(default=60, ge=1, le=365), _user=Depends(require_auth)):
    from .calendar_store import list_calendar_days
    return list_calendar_days(days)


@app.get("/api/calendar/{snapshot_date}")
def calendar_day(snapshot_date: str, _user=Depends(require_auth)):
    from .calendar_store import get_calendar_day
    return get_calendar_day(snapshot_date)


@app.delete("/api/calendar/{snapshot_date}")
def delete_calendar_date(snapshot_date: str, _user=Depends(require_auth)):
    from .calendar_store import delete_calendar_day
    return delete_calendar_day(snapshot_date)


@app.delete("/api/calendar/{snapshot_date}/{algo_id}")
def delete_calendar_algo_snapshot(snapshot_date: str, algo_id: str, _user=Depends(require_auth)):
    from .calendar_store import delete_calendar_snapshot
    return delete_calendar_snapshot(snapshot_date, algo_id)


@app.post("/api/calendar/snapshot")
def calendar_snapshot(payload: dict | None = None, _user=Depends(require_auth)):
    from .calendar_store import save_dashboard_snapshot
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
            from .calendar_store import store_market_candles
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
    from .backtest import start_backtest
    from .strategies.algo3_silver_micro import _resolve_silver_symbol
    algo_id = str(payload.get("algo_id") or "")
    silver_sell_plan = payload.get("silver_sell_plan")
    # Accept date for existing clients while range-aware clients send both fields.
    start_date = str(payload.get("start_date") or payload.get("date") or "")
    end_date = str(payload.get("end_date") or start_date)
    try:
        if algo_id == "algo3":
            # Resolve at request time so backtests always target the current
            # front-month contract, matching what live is trading.
            return start_backtest(
                algo_id,
                start_date,
                end_date,
                [_resolve_silver_symbol()],
                silver_sell_plan=silver_sell_plan,
            )
        return start_backtest(algo_id, start_date, end_date, engine.WATCHLIST)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/backtests/{job_id}")
def backtest_status(job_id: str, _user=Depends(require_auth)):
    from .backtest import get_backtest_job
    job = get_backtest_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backtest job not found. It may predate durable job storage or have been removed.")
    return job


@app.post("/api/backtests/{job_id}/cancel")
def cancel_backtest(job_id: str, _user=Depends(require_auth)):
    from .backtest import cancel_backtest_job
    job = cancel_backtest_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backtest job not found.")
    return job
