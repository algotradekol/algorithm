"""
main.py — the FastAPI app. Starts the trading engine as a background
thread on startup, and exposes REST endpoints the Next.js frontend
polls for live state. All routes except /health require a valid
Supabase auth token.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import ALLOWED_ORIGINS
from app.auth import require_auth
from app.engine import start_engine, STRATEGIES
from app.charges import get_charges_config, set_charges_config
from app.supabase_client import supabase


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_engine()
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
    return {"status": "ok", "strategies_running": list(STRATEGIES.keys())}


@app.get("/api/algo/{algo_id}/summary")
def algo_summary(algo_id: str, _user=Depends(require_auth)):
    strategy = STRATEGIES.get(algo_id)
    if not strategy:
        raise HTTPException(404, f"No such algo: {algo_id}")
    return strategy.broker.summary()


@app.get("/api/algo/{algo_id}/positions")
def algo_positions(algo_id: str, _user=Depends(require_auth)):
    strategy = STRATEGIES.get(algo_id)
    if not strategy:
        raise HTTPException(404, f"No such algo: {algo_id}")
    return strategy.broker.open_positions()


@app.get("/api/algo/{algo_id}/trades")
def algo_trades(algo_id: str, _user=Depends(require_auth)):
    result = supabase.table("trades").select("*").eq("algo_id", algo_id) \
        .order("exit_time", desc=True).limit(200).execute()
    return result.data


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
    return {"symbols": strategy.watchlist if strategy else []}
