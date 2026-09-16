from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import datetime
from typing import Literal

from .auth import require_auth
from .delta_engine import service_for
from .delta_config import delta_capabilities, asset_capabilities, timeframe_enabled
from .delta_reporting import paper_row, account_rows, inr_rate
import time

router = APIRouter(prefix="/api/delta", dependencies=[Depends(require_auth)])


Asset = Literal['gold', 'silver']
Mode = Literal['paper', 'live']


def asset_service(asset, mode: Mode = 'paper'):
    return service_for(asset, mode)


class BacktestRequest(BaseModel):
    asset: Asset = 'gold'
    minutes: Literal[5, 7, 15, 30, 60, 120, 240]
    start_date: datetime.date
    end_date: datetime.date
    settings: dict = Field(default_factory=dict)
    path: Literal['high_first', 'low_first'] = 'high_first'


def require_delta(*, section: str | None = None, minutes: int | None = None, asset: Asset = 'gold'):
    capabilities = asset_capabilities(asset)
    if capabilities["config_error"] or not capabilities["delta_enabled"]:
        raise HTTPException(404, capabilities["config_error"] or "Delta is disabled")
    if section and not capabilities["sections"].get(section, False):
        raise HTTPException(404, f"Delta {section} is disabled")
    if minutes is not None and minutes not in capabilities['enabled_timeframes']:
        raise HTTPException(404, "That Delta timeframe is disabled")
    return capabilities


@router.get('/capabilities')
def capabilities():
    return delta_capabilities()


@router.get('/overview')
def overview(asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(section="overview", asset=asset)
    service = asset_service(asset, mode)
    try:
        return service.overview()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta overview is temporarily unavailable") from None


@router.post('/backtest/run')
def backtest(request: BacktestRequest):
    require_delta(section="backtest", minutes=request.minutes, asset=request.asset)
    from .delta_backtest import BACKTEST_LOCK, DeltaBacktestCancelled, clear_cancel_request, run_backtest
    if not BACKTEST_LOCK.acquire(blocking=False):
        raise HTTPException(409, 'A Delta backtest is already running; retry when it finishes')
    clear_cancel_request()
    started = time.monotonic()
    print(
        "[delta_backtest] START "
        f"asset={request.asset} minutes={request.minutes} "
        f"range={request.start_date.isoformat()}..{request.end_date.isoformat()} "
        f"path={request.path}"
    )
    try:
        result = run_backtest(request.minutes, request.start_date, request.end_date, request.settings, request.path, asset=request.asset)
        summary = result.get("summary") if isinstance(result, dict) else {}
        print(
            "[delta_backtest] END "
            f"asset={request.asset} minutes={request.minutes} "
            f"trades={(summary or {}).get('trades')} net={(summary or {}).get('net')} "
            f"elapsed={time.monotonic() - started:.2f}s"
        )
        return result
    except DeltaBacktestCancelled:
        print(
            "[delta_backtest] CANCELLED "
            f"asset={request.asset} minutes={request.minutes} "
            f"elapsed={time.monotonic() - started:.2f}s"
        )
        raise HTTPException(409, 'Delta backtest cancelled') from None
    except (ValueError, TypeError, KeyError) as exc:
        print(
            "[delta_backtest] FAILED "
            f"asset={request.asset} minutes={request.minutes} "
            f"error={exc}"
        )
        raise HTTPException(400, str(exc)) from None
    except Exception as exc:
        print(
            "[delta_backtest] ERROR "
            f"asset={request.asset} minutes={request.minutes} "
            f"error={exc}"
        )
        raise HTTPException(503, 'Delta backtest data unavailable; check history/proxy connectivity') from None
    finally:
        BACKTEST_LOCK.release()


@router.post('/backtest/cancel')
def cancel_backtest(asset: Asset = 'gold'):
    require_delta(section="backtest", asset=asset)
    from .delta_backtest import cancel_active_backtest
    if not cancel_active_backtest():
        raise HTTPException(404, 'No Delta backtest is running')
    print(f"[delta_backtest] cancel requested asset={asset}")
    return {"status": "cancelling"}


@router.get("/{minutes}/status")
def status(minutes: int, asset: Asset = 'gold', mode: Mode = 'paper'):
    if minutes not in (5, 7, 15, 30, 60, 120, 240):
        raise HTTPException(400, "Invalid Delta timeframe")
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    return service.snapshot(minutes)


@router.get("/{minutes}/trades")
def trades(
    minutes: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    asset: Asset = 'gold',
    mode: Mode = 'paper',
    today_only: bool = True,
):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    try:
        strategy = service.strategy(minutes)
        rows = (
            daily_trades_with_previous_context(strategy.broker.store, offset, limit)
            if today_only else
            strategy.broker.store.trades(offset, limit)
        )
        return {"trades": [paper_row(row, service.client.region) for row in rows], "today_only": today_only}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta trade history unavailable") from None


def daily_trades_with_previous_context(store, offset=0, limit=100):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    filtered = []
    previous_context = None
    page_offset = 0
    while True:
        page = store.trades(page_offset, 500)
        if not page:
            break
        for row in page:
            try:
                closed = datetime.datetime.fromisoformat(str(row.get("exit_time", "")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            closed_utc = closed if closed.tzinfo else closed.replace(tzinfo=datetime.timezone.utc)
            if closed_utc.astimezone(datetime.timezone.utc).date() == today:
                filtered.append(row)
                continue
            previous_context = row
            break
        if previous_context is not None or len(page) < 500:
            break
        page_offset += len(page)
    if previous_context is not None:
        filtered.append(previous_context)
    return filtered[offset:offset + limit]


@router.put("/{minutes}/settings")
def settings(minutes: int, changes: dict, asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    try:
        return service.save_settings(minutes, changes)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta settings were not saved; check database availability") from None


class CloseRequest(BaseModel):
    position_id: str


class PauseRequest(BaseModel):
    duration_minutes: int = Field(ge=0, le=10_080)


class ProtectionRequest(BaseModel):
    position_id: str
    sl_price: float = Field(gt=0, allow_inf_nan=False)
    target_price: float = Field(gt=0, allow_inf_nan=False)
    expected_sl: float = Field(gt=0, allow_inf_nan=False)
    expected_target: float = Field(gt=0, allow_inf_nan=False)


@router.put('/{minutes}/protection')
def edit_protection(minutes: int, request: ProtectionRequest, asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    print(
        f"[delta-{mode}] protection edit request asset={asset} minutes={minutes} "
        f"pos={request.position_id} new_sl={request.sl_price} new_target={request.target_price} "
        f"expected_sl={request.expected_sl} expected_target={request.expected_target}"
    )
    try:
        position = service.edit_protection(minutes, request.position_id, request.sl_price, request.target_price, request.expected_sl, request.expected_target)
        print(
            f"[delta-{mode}] protection edit OK asset={asset} minutes={minutes} "
            f"pos={request.position_id} sl={position.get('sl_price')} target={position.get('target_price')} "
            f"sl_source={position.get('sl_source')} target_source={position.get('target_source')}"
        )
        return {'position': position}
    except ValueError as exc:
        print(f"[delta-{mode}] protection edit refused asset={asset} minutes={minutes} pos={request.position_id}: {exc}")
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        print(f"[delta-{mode}] protection edit failed asset={asset} minutes={minutes} pos={request.position_id}: {exc!r}")
        raise HTTPException(503, 'Protection was not saved; reload before retrying') from None


@router.get('/{minutes}/export')
def export(minutes: int, kind: Literal['open', 'closed'], asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    from .delta_export import export_paper
    try:
        return export_paper(service, minutes, kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, 'CSV export failed; no partial download was created') from None


@router.get("/account/{kind}")
def account(kind: str, after: str | None = Query(None, max_length=256), source: str = "paper", minutes: int = 15, offset: int = Query(0, ge=0), asset: Asset = 'gold'):
    require_delta(section="activity", minutes=minutes if source == "paper" else None, asset=asset)
    service = asset_service(asset, 'live' if source == 'live' else 'paper')
    if kind not in {"positions", "open_orders", "stop_orders", "history"}:
        raise HTTPException(400, "Invalid Delta account view")
    if source not in {"paper", "live"} or minutes not in {5, 7, 15, 30, 60, 120, 240}:
        raise HTTPException(400, "Invalid Delta source or timeframe")
    if source == "paper":
        return paper_account(kind, minutes, offset, asset)
    client = service.client
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
        product = getattr(service, 'product', None) or {}
        product_id = product.get('id')
        client_symbol = getattr(client, 'symbol', None)
        if not isinstance(client_symbol, str) or not client_symbol:
            client_symbol = None
        if client_symbol or product_id is not None:
            rows = _delta_product_rows(rows, client_symbol, product_id)
        if kind == "stop_orders":
            rows = [r for r in rows if r.get("stop_order_type")]
        elif kind == "open_orders":
            rows = [r for r in rows if not r.get("stop_order_type")]
        formatted = account_rows(rows, kind, client.region)
        note = None
        if kind == "positions":
            order_payload = client.get("/v2/orders", {"page_size": 100, "states": "open,pending"}, private=True, envelope=True)
            order_rows = order_payload.get("result")
            if not isinstance(order_rows, list):
                raise ValueError("Unexpected Delta open orders response")
            if client_symbol or product_id is not None:
                order_rows = _delta_product_rows(order_rows, client_symbol, product_id)
            formatted.extend(account_rows(order_rows, "open_orders", client.region))
            note = "Positions view includes active account positions plus waiting open, stop-loss, and target orders for this product."
        return {"rows": formatted,
                "next_cursor": (payload.get("meta") or {}).get("after") if kind != "positions" else None,
                "fetched_at": time.time(), "exchange": client.region, "mode": "account_read_only",
                "note": note, "inr_rate": inr_rate(client.region, "USD")}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta account data unavailable; this is not an empty-account confirmation") from None


def _delta_product_rows(rows, client_symbol, product_id):
    return [
        row for row in rows if
        (client_symbol and (row.get('product_symbol') or (row.get('product') or {}).get('symbol')) == client_symbol)
        or (product_id is not None and str(row.get('product_id')) == str(product_id))
    ]


def paper_account(kind, minutes, offset, asset: Asset = 'gold'):
    service = asset_service(asset)
    try:
        strategy = service.strategy(minutes)
        positions = strategy.broker.open_positions()
        rows = []
        more = False
        note = "Simulated records only; no orders are submitted to Delta."
        if kind == "open_orders":
            note = "Paper target rows are virtual protection levels. Paper market entries fill immediately; there are no pending entry orders."
        elif kind == "stop_orders":
            note = "Paper stop rows are virtual engine protection levels, not exchange orders."
        elif kind == "positions":
            note = "Positions view includes active paper positions plus their virtual stop-loss and target levels."
        if kind == "history":
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
                                 "pnl_inr": paper_row(trade, service.client.region).get("pnl_inr")})
            rows.sort(key=lambda r: r["time"], reverse=True)
            note += " Each history page covers up to 100 closed trades and their entry/exit events."
        else:
            for position in positions:
                row = paper_row(position, service.client.region)
                base = {"id": row["id"], "symbol": row["symbol"], "side": row["side"], "lots": row["qty"],
                        "entry_price": row["entry_price"], "time": row["entry_time"], "state": "simulated",
                        "currency": row.get("quote_currency"), "margin": row.get("estimated_entry_margin"),
                        "margin_inr": row.get("margin_inr"), "order_type": "Position"}
                protection_rows = (("Virtual SL", row["sl_price"]), ("Virtual target", row["target_price"]))
                if kind == "positions":
                    rows.append(base)
                elif kind == "open_orders":
                    protection_rows = (("Virtual target", row["target_price"]),)
                elif kind == "stop_orders":
                    protection_rows = (("Virtual SL", row["sl_price"]),)
                for label, value in protection_rows:
                    if kind != "history":
                        rows.append({**base, "id": row["id"] + label, "side": "SELL" if row["side"] == "BUY" else "BUY",
                                     "order_type": label, "stop_price": value, "margin": None, "margin_inr": None})
        return {"rows": rows, "has_more": more, "fetched_at": time.time(), "mode": "paper", "note": note,
                "inr_rate": inr_rate(service.client.region, "USD")}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta paper records unavailable") from None


@router.post("/{minutes}/close")
def close(minutes: int, request: CloseRequest, asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    print(f"[delta-{mode}] close request asset={asset} minutes={minutes} pos={request.position_id}")
    try:
        service.close(minutes, request.position_id)
        print(f"[delta-{mode}] close OK asset={asset} minutes={minutes} pos={request.position_id}")
        return {"closed": True}
    except (ValueError, RuntimeError) as exc:
        print(f"[delta-{mode}] close refused asset={asset} minutes={minutes} pos={request.position_id}: {exc}")
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        print(f"[delta-{mode}] close failed asset={asset} minutes={minutes} pos={request.position_id}: {exc!r}")
        raise HTTPException(503, f"Delta {mode} exit was not confirmed; reload position status") from None


@router.post("/{minutes}/resume")
def resume(minutes: int, asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    try:
        return {"cooldown": service.resume(minutes)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta rest timer could not be cleared; retry after reloading") from None


@router.post("/{minutes}/pause")
def pause(minutes: int, request: PauseRequest, asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(minutes=minutes, asset=asset)
    service = asset_service(asset, mode)
    try:
        return {"cooldown": service.pause(minutes, request.duration_minutes)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta rest timer could not be started; retry after reloading") from None


@router.post("/connection/check")
def connection_check(asset: Asset = 'gold', mode: Mode = 'paper'):
    require_delta(asset=asset)
    service = asset_service(asset, mode)
    try:
        if not service.client:
            raise ValueError("Delta is not configured")
        service.client.get("/v2/wallet/balances", private=True)
        message = "Delta account access verified. Live execution is enabled only when DELTA_LIVE_ENABLED=true and Trading permission is on."
        return {"verified": True, "message": message}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta account verification unavailable") from None
