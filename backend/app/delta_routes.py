from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import datetime
from typing import Literal

from .auth import require_auth
from .delta_engine import delta_service
from .delta_config import delta_capabilities, timeframe_enabled
from .delta_reporting import paper_row, account_rows, inr_rate
import time

router = APIRouter(prefix="/api/delta", dependencies=[Depends(require_auth)])


class BacktestRequest(BaseModel):
    minutes: Literal[5, 7, 15, 30, 60, 240]
    start_date: datetime.date
    end_date: datetime.date
    settings: dict = Field(default_factory=dict)
    path: Literal['high_first', 'low_first'] = 'high_first'


def require_delta(*, section: str | None = None, minutes: int | None = None):
    capabilities = delta_capabilities()
    if capabilities["config_error"] or not capabilities["delta_enabled"]:
        raise HTTPException(404, capabilities["config_error"] or "Delta is disabled")
    if section and not capabilities["sections"].get(section, False):
        raise HTTPException(404, f"Delta {section} is disabled")
    if minutes is not None and not timeframe_enabled(minutes, capabilities):
        raise HTTPException(404, "That Delta timeframe is disabled")
    return capabilities


@router.get('/capabilities')
def capabilities():
    return delta_capabilities()


@router.get('/overview')
def overview():
    require_delta(section="overview")
    try:
        return delta_service.overview()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta overview is temporarily unavailable") from None


@router.post('/backtest/run')
def backtest(request: BacktestRequest):
    require_delta(section="backtest", minutes=request.minutes)
    from .delta_backtest import BACKTEST_LOCK, run_backtest
    if not BACKTEST_LOCK.acquire(blocking=False):
        raise HTTPException(409, 'A Delta backtest is already running; retry when it finishes')
    try:
        return run_backtest(request.minutes, request.start_date, request.end_date, request.settings, request.path)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, 'Delta backtest data unavailable; check history/proxy connectivity') from None
    finally:
        BACKTEST_LOCK.release()


@router.get("/{minutes}/status")
def status(minutes: int):
    if minutes not in (5, 7, 15, 30, 60, 240):
        raise HTTPException(400, "Invalid Delta timeframe")
    require_delta(minutes=minutes)
    return delta_service.snapshot(minutes)


@router.get("/{minutes}/trades")
def trades(minutes: int, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    require_delta(minutes=minutes)
    try:
        strategy = delta_service.strategy(minutes)
        return {"trades": [paper_row(row, delta_service.client.region) for row in strategy.broker.store.trades(offset, limit)]}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta trade history unavailable") from None


@router.put("/{minutes}/settings")
def settings(minutes: int, changes: dict):
    require_delta(minutes=minutes)
    try:
        return delta_service.save_settings(minutes, changes)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta settings were not saved; check database availability") from None


class CloseRequest(BaseModel):
    position_id: str


class ProtectionRequest(BaseModel):
    position_id: str
    sl_price: float = Field(gt=0, allow_inf_nan=False)
    target_price: float = Field(gt=0, allow_inf_nan=False)
    expected_sl: float = Field(gt=0, allow_inf_nan=False)
    expected_target: float = Field(gt=0, allow_inf_nan=False)


@router.put('/{minutes}/protection')
def edit_protection(minutes: int, request: ProtectionRequest):
    require_delta(minutes=minutes)
    try:
        position = delta_service.edit_protection(minutes, request.position_id, request.sl_price, request.target_price, request.expected_sl, request.expected_target)
        return {'position': position}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, 'Protection was not saved; reload before retrying') from None


@router.get('/{minutes}/export')
def export(minutes: int, kind: Literal['open', 'closed']):
    require_delta(minutes=minutes)
    from .delta_export import export_paper
    try:
        return export_paper(delta_service, minutes, kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, 'CSV export failed; no partial download was created') from None


@router.get("/account/{kind}")
def account(kind: str, after: str | None = Query(None, max_length=256), source: str = "paper", minutes: int = 15, offset: int = Query(0, ge=0)):
    require_delta(section="activity", minutes=minutes if source == "paper" else None)
    if kind not in {"positions", "open_orders", "stop_orders", "history"}:
        raise HTTPException(400, "Invalid Delta account view")
    if source not in {"paper", "live"} or minutes not in {5, 7, 15, 30, 60, 240}:
        raise HTTPException(400, "Invalid Delta source or timeframe")
    if source == "paper":
        return paper_account(kind, minutes, offset)
    client = delta_service.client
    if not client:
        raise HTTPException(503, "Delta account connection is not configured")
    path = "/v2/positions/margined" if kind == "positions" else "/v2/orders/history" if kind == "history" else "/v2/orders"
    params = {} if kind == "positions" else {"page_size": 100}
    if kind in {"open_orders", "stop_orders"}:
        params["states"] = "open,pending"
    if after and kind != "positions":
        params["after"] = after
    try:
        payload = client.get(path, params, private=True, envelope=True)
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise ValueError("Unexpected Delta account response")
        if kind == "stop_orders":
            rows = [r for r in rows if r.get("stop_order_type")]
        elif kind == "open_orders":
            rows = [r for r in rows if not r.get("stop_order_type")]
        return {"rows": account_rows(rows, kind, client.region),
                "next_cursor": (payload.get("meta") or {}).get("after") if kind != "positions" else None,
                "fetched_at": time.time(), "exchange": client.region, "mode": "account_read_only",
                "inr_rate": inr_rate(client.region, "USD")}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta account data unavailable; this is not an empty-account confirmation") from None


def paper_account(kind, minutes, offset):
    try:
        strategy = delta_service.strategy(minutes)
        positions = strategy.broker.open_positions()
        rows = []
        more = False
        note = "Simulated records only; no orders are submitted to Delta."
        if kind == "open_orders":
            note = "Paper market entries fill immediately; there are no pending entry orders."
        elif kind == "history":
            closed = strategy.broker.store.trades(offset, 101)
            more = len(closed) > 100
            for trade in closed[:100] + (positions if offset == 0 else []):
                rows.append({"id": trade["id"] + ":entry", "symbol": trade["symbol"], "side": trade["side"],
                             "lots": trade["qty"], "entry_price": trade["entry_price"], "time": trade["entry_time"],
                             "state": "simulated", "order_type": "Entry", "currency": trade.get("quote_currency")})
                if trade.get("exit_time"):
                    rows.append({"id": trade["id"] + ":exit", "symbol": trade["symbol"],
                                 "side": "SELL" if trade["side"] == "BUY" else "BUY", "lots": trade["qty"],
                                 "entry_price": trade["exit_price"], "time": trade["exit_time"], "state": "simulated",
                                 "order_type": trade.get("exit_reason"), "currency": trade.get("quote_currency"),
                                 "pnl_inr": paper_row(trade, delta_service.client.region).get("pnl_inr")})
            rows.sort(key=lambda r: r["time"], reverse=True)
            note += " Each history page covers up to 100 closed trades and their entry/exit events."
        else:
            for position in positions:
                row = paper_row(position, delta_service.client.region)
                base = {"id": row["id"], "symbol": row["symbol"], "side": row["side"], "lots": row["qty"],
                        "entry_price": row["entry_price"], "time": row["entry_time"], "state": "simulated",
                        "currency": row.get("quote_currency"), "margin": row.get("estimated_entry_margin"),
                        "margin_inr": row.get("margin_inr"), "order_type": "Position"}
                if kind == "positions":
                    rows.append(base)
                else:
                    for label, value in (("Virtual SL", row["sl_price"]), ("Virtual target", row["target_price"])):
                        rows.append({**base, "id": row["id"] + label, "side": "SELL" if row["side"] == "BUY" else "BUY",
                                     "order_type": label, "stop_price": value, "margin": None, "margin_inr": None})
        return {"rows": rows, "has_more": more, "fetched_at": time.time(), "mode": "paper", "note": note,
                "inr_rate": inr_rate(delta_service.client.region, "USD")}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta paper records unavailable") from None


@router.post("/{minutes}/close")
def close(minutes: int, request: CloseRequest):
    require_delta(minutes=minutes)
    try:
        delta_service.close(minutes, request.position_id)
        return {"closed": True}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta paper exit was not confirmed; reload position status") from None


@router.post("/{minutes}/resume")
def resume(minutes: int):
    require_delta(minutes=minutes)
    try:
        return {"cooldown": delta_service.resume(minutes)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta rest timer could not be cleared; retry after reloading") from None


@router.post("/connection/check")
def connection_check():
    require_delta()
    try:
        if not delta_service.client:
            raise ValueError("Delta is not configured")
        delta_service.client.get("/v2/wallet/balances", private=True)
        return {"verified": True, "message": "Delta read-only account access verified. Execution remains paper only."}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta account verification unavailable") from None
