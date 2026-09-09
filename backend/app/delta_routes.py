from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import require_auth
from .delta_engine import delta_service

router = APIRouter(prefix="/api/delta", dependencies=[Depends(require_auth)])


@router.get("/{minutes}/status")
def status(minutes: int):
    if minutes not in (15, 60):
        raise HTTPException(400, "Invalid Delta timeframe")
    return delta_service.snapshot(minutes)


@router.get("/{minutes}/trades")
def trades(minutes: int, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    try:
        strategy = delta_service.strategy(minutes)
        return {"trades": strategy.broker.store.trades(offset, limit)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta trade history unavailable") from None


@router.put("/{minutes}/settings")
def settings(minutes: int, changes: dict):
    try:
        return delta_service.save_settings(minutes, changes)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta settings were not saved; check database availability") from None


class CloseRequest(BaseModel):
    position_id: str


@router.post("/{minutes}/close")
def close(minutes: int, request: CloseRequest):
    try:
        delta_service.close(minutes, request.position_id)
        return {"closed": True}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta paper exit was not confirmed; reload position status") from None


@router.post("/connection/check")
def connection_check():
    try:
        if not delta_service.client:
            raise ValueError("Delta is not configured")
        delta_service.client.get("/v2/wallet/balances", private=True)
        return {"verified": True, "message": "Delta read-only account access verified. Execution remains paper only."}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Delta account verification unavailable") from None
