"""
LiveBroker wraps the same persistence/accounting model as PaperBroker,
but sends actual Fyers orders before recording the trade in the app.

The live path writes to separate live_* tables so the paper flow stays
isolated and backwards-compatible.
"""
from __future__ import annotations

import datetime
import threading
import time

from .audit_log import audit_log
from .fyers_client import get_fyers_model, get_wallet_balance
from .paper_broker import PaperBroker
from .proxy_health import check_proxy_reachable
from .runtime_mode import get_fyers_config, get_runtime_trading_mode
from .supabase_client import run_with_supabase
from .timezone import IST
from .trailing_stop import SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN, uses_silver_breakeven_stop


# ── Fyers as single source of truth (Design B, 2026-08-26) ────────────
# Every write path (open_trade, close_trade) that touches Fyers for a given
# symbol runs under _symbol_lock and consults _fyers_current_net_qty right
# before acting. Two workers (WS tick handler, reconciliation loop, user
# Exit button, EOD square-off) can no longer race and place duplicate
# orders. Fyers is the authority — our in-memory / DB state is a mirror,
# never a decision-maker for whether to send another order.
#
# 2026-08-26 incident that motivated this: check_exits() detected SL cross
# on our WS tick 4s AFTER the Fyers-side SL-Limit had already fired, and
# fired a fresh MARKET sell to "close" the (already flat) position. That
# second sell took the client from flat → short 1. Guard prevents it.
_symbol_locks: dict[str, threading.RLock] = {}
_symbol_locks_lock = threading.Lock()

# A Fyers MARKET entry can fill and be delivered over the Order Update WS
# before the protective orders and local live_positions row are persisted.
# Keep this small, process-local handoff record so that transient state is
# recognised as algo-owned rather than being bootstrapped as a Fyers-app
# position. It expires deliberately: a process that truly fails before it
# persists the row must still be recoverable on the next reconciliation pass.
_LIVE_ENTRY_HANDOFF_SECONDS = 90.0
_live_entry_handoffs: dict[str, dict[str, object]] = {}
_live_entry_handoffs_lock = threading.Lock()


def _handoff_key(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _mark_live_entry_handoff(symbol: str, order_id: str, side: str, qty: int) -> None:
    key = _handoff_key(symbol)
    if not key or not order_id:
        return
    with _live_entry_handoffs_lock:
        _live_entry_handoffs[key] = {
            "order_id": str(order_id).strip(),
            "side": str(side or "").upper(),
            "qty": int(qty),
            "expires_at": time.monotonic() + _LIVE_ENTRY_HANDOFF_SECONDS,
        }


def _live_entry_handoff(symbol: str) -> dict[str, object] | None:
    key = _handoff_key(symbol)
    if not key:
        return None
    with _live_entry_handoffs_lock:
        handoff = _live_entry_handoffs.get(key)
        if handoff and float(handoff.get("expires_at") or 0) > time.monotonic():
            return dict(handoff)
        _live_entry_handoffs.pop(key, None)
    return None


def _clear_live_entry_handoff(symbol: str, order_id: str | None = None) -> None:
    key = _handoff_key(symbol)
    if not key:
        return
    with _live_entry_handoffs_lock:
        current = _live_entry_handoffs.get(key)
        if current is None:
            return
        if order_id and str(current.get("order_id") or "") != str(order_id).strip():
            return
        _live_entry_handoffs.pop(key, None)


def _symbol_lock(symbol: str) -> threading.RLock:
    """Return the per-symbol RLock, creating it on first use. RLock so a
    caller already holding the lock can reenter (e.g. safety-flatten from
    inside open_trade)."""
    key = str(symbol or "").strip().upper()
    with _symbol_locks_lock:
        lock = _symbol_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _symbol_locks[key] = lock
    return lock


def _fyers_current_net_qty(symbol: str) -> float | None:
    """Fyers-authoritative current net_qty for `symbol`, or None if Fyers
    is unreachable. Positive = long, negative = short, 0 = flat.

    Returning None on failure is deliberate — callers must treat "unknown"
    as "do not skip the safety action". If we can't ask Fyers, we fall back
    to the current behaviour (place the order) so a Fyers outage does not
    leave a naked position uncovered.
    """
    try:
        from .fyers_client import get_broker_positions
        result = get_broker_positions("live")
    except Exception as exc:
        print(f"[live_broker] pre-flight positions fetch failed for {symbol}: {exc}")
        return None
    if not isinstance(result, dict) or not result.get("available"):
        return None
    target = str(symbol or "").strip().upper()
    for row in result.get("positions") or []:
        if str(row.get("symbol") or "").strip().upper() == target:
            try:
                return float(row.get("net_qty", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    # Fyers responded and this symbol wasn't in the positions list → flat.
    return 0.0


# Default tick size for NSE equity. Fyers V3 rejects prices that aren't a
# multiple of the instrument's tick with `code -50: LimitPrice not a multiple
# of tick size ...`. Every price we send to Fyers has to be rounded to the
# instrument's tick — different exchanges/products use different ticks:
#
#   NSE cash equity  → 0.05  (Rs 0.05 minimum price move)
#   MCX SILVERMIC    → 1.0   (Rs 1 whole-rupee ticks)
#   MCX GOLDM        → 1.0
#   MCX CRUDEOIL     → 1.0
#   MCX NATURALGAS   → 0.10
#
# 2026-08-25 incident: SL-M placement for MCX:SILVERMIC26AUGFUT rejected
# with `LimitPrice not a multiple of tick size 1.0000` because the code
# was rounding to 0.05 and producing 243218.8 (decimal), then Fyers ate
# the position with a safety-fallback market flatten. Fix is per-symbol
# tick resolution below.
_DEFAULT_TICK_SIZE = 0.05
_MCX_TICK_SIZE = 1.0
_MCX_NATURALGAS_TICK_SIZE = 0.10

# Backwards-compat alias for external callers that imported _TICK_SIZE.
_TICK_SIZE = _DEFAULT_TICK_SIZE

# When a stop-loss triggers, price may already be gapping past the stop.
# Fyers V3 requires limitPrice > 0 even on SL-M, so we send it as SL-Limit
# with a slack limit that lets the order fill through modest gaps but not
# catastrophic slippage.
#
# NSE: 0.5% works because equity moves are usually well under 0.5% per tick.
# MCX: 0.5% on a Rs 2.4L Silver contract = Rs 1,200 slack. Way too wide,
# and the resulting limit ends up a decimal that fails tick-rounding for
# a Rs 1 tick. Use a fixed points slack per MCX instrument instead.
_SL_LIMIT_SLIPPAGE_PCT = 0.5
_SL_LIMIT_SLIPPAGE_POINTS_MCX = 150.0


def _tick_size_for(symbol: str | None) -> float:
    """Look up the Fyers tick size for a symbol.

    Rules kept intentionally narrow — we only recognise the products we
    actually trade. Anything unknown falls back to the NSE-equity default
    (0.05), which is what the codebase used everywhere before this
    per-symbol helper existed.
    """
    if not symbol:
        return _DEFAULT_TICK_SIZE
    text = str(symbol).strip().upper()
    if text.startswith("MCX:"):
        # Natural gas is finer than the rest of MCX.
        if "NATURALGAS" in text:
            return _MCX_NATURALGAS_TICK_SIZE
        return _MCX_TICK_SIZE
    return _DEFAULT_TICK_SIZE


def _round_to_tick(price: float, tick: float | None = None, symbol: str | None = None) -> float:
    """Round `price` to the nearest multiple of the instrument's tick.

    Callers should pass EITHER an explicit `tick` OR a `symbol` (the tick
    is derived from the symbol via _tick_size_for). When both are omitted
    the NSE-equity default is used — kept as-is to preserve backwards
    compatibility with older callers that don't know their symbol yet.
    Never returns 0 for a positive input — Fyers rejects both non-tick
    multiples and zero prices.
    """
    if price is None or price <= 0:
        return 0.0
    resolved_tick = tick if tick is not None else _tick_size_for(symbol)
    if resolved_tick <= 0:
        resolved_tick = _DEFAULT_TICK_SIZE
    # Decimals in the output would defeat the whole point on Rs 1-tick
    # products, so round to exactly as many decimals as the tick needs
    # (tick 1.0 -> 0 decimals, tick 0.10 -> 1, tick 0.05 -> 2, tick 0.01 -> 2).
    if resolved_tick >= 1:
        decimals = 0
    else:
        decimals = max(0, int(_math.ceil(-_math_log10(resolved_tick))))
    return round(round(float(price) / resolved_tick) * resolved_tick, decimals)


def _sl_limit_price(stop_price: float, exit_side: str, symbol: str | None = None) -> float:
    """Compute the LIMIT companion price for an SL-Limit order, symbol-aware.

    exit_side is the side of the PROTECTIVE order:
      - 'SELL' exits a long position -> limit sits BELOW stop so a
        downward gap still fills.
      - 'BUY' exits a short position -> limit sits ABOVE stop so an
        upward gap still fills.

    Slack policy:
      - MCX: fixed 150-point slack, then rounded to the Rs 1 tick.
      - NSE (or unknown symbol): 0.5% slack, then rounded to 0.05.
    """
    if stop_price is None or stop_price <= 0:
        return 0.0
    stop = float(stop_price)
    is_mcx = symbol and str(symbol).strip().upper().startswith("MCX:")
    if is_mcx:
        offset = _SL_LIMIT_SLIPPAGE_POINTS_MCX
        limit = stop - offset if exit_side.upper() == "SELL" else stop + offset
    else:
        factor = 1.0 - (_SL_LIMIT_SLIPPAGE_PCT / 100.0) if exit_side.upper() == "SELL" else 1.0 + (_SL_LIMIT_SLIPPAGE_PCT / 100.0)
        limit = stop * factor
    return _round_to_tick(limit, symbol=symbol)


# math.log10 imported lazily to avoid touching module import order.
import math as _math
def _math_log10(x: float) -> float:
    return _math.log10(x) if x > 0 else 0.0


class LiveBroker(PaperBroker):
    def state_table_name(self) -> str:
        return "live_algo_state"

    def positions_table_name(self) -> str:
        return "live_positions"

    def trades_table_name(self) -> str:
        return "live_trades"

    def close_stale_open_positions(self) -> int:
        # Avoid auto-closing real broker positions on startup. Reconcile them
        # manually or through a dedicated live clean-up flow instead.
        return 0

    def open_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        entry_trigger: str | None = None,
        signal_snapshot: dict | None = None,
        entry_time: str | None = None,
    ):
        with _symbol_lock(symbol):
            return self._open_trade_locked(
                symbol, side, qty, entry_price, sl_price, target_price,
                entry_trigger, signal_snapshot, entry_time,
            )

    def _open_trade_locked(
        self,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        entry_trigger: str | None,
        signal_snapshot: dict | None,
        entry_time: str | None,
    ):
        # Do not create a live carry-forward position until the broker-side
        # GTT/OCO protection path is implemented and verified for MCX. The
        # paper/backtest path intentionally supports this setting already.
        if bool((signal_snapshot or {}).get("overnight_carry_enabled")):
            raise RuntimeError(
                "Silver Micro 2.0 overnight carry is available in paper and backtest only. "
                "Live entry was blocked because FYERS overnight GTT/OCO protection is not configured yet."
            )
        # Do not let a second caller submit while the first accepted Fyers
        # entry is still being protected and persisted locally.
        pending_entry = _live_entry_handoff(symbol)
        if pending_entry:
            raise RuntimeError(
                f"Fyers live entry refused: {symbol} is awaiting local tracking "
                f"for entry order {pending_entry.get('order_id')}."
            )

        # Design B pre-flight — Fyers is the single source of truth for
        # whether we already hold this symbol. Refuse to double up on a
        # duplicate signal, restart race, or reconciliation lag.
        current_net = _fyers_current_net_qty(symbol)
        if current_net is not None and abs(current_net) >= 1:
            print(
                f"[live_broker] pre-flight ABORT open_trade {symbol} {side} x{qty}: "
                f"Fyers already holds net_qty={current_net} — refusing duplicate entry"
            )
            raise RuntimeError(
                f"Fyers live entry refused: {symbol} already positioned at Fyers "
                f"(net_qty={current_net}). Reconciliation must clear it first."
            )

        qty = self._cap_qty_to_live_funds(symbol, qty, entry_price)
        if qty < 1:
            raise RuntimeError("Fyers live order failed: available live funds are below the current share price.")
        # Entry order type is driven by strategy settings (default LIMIT at LTP,
        # per client spec). Read at call-time so a settings change takes effect
        # on the next trade without a restart.
        entry_order_type, entry_limit_price = self._entry_order_params(entry_price)
        order_response = self._place_live_order(
            symbol, side, qty,
            order_type=entry_order_type,
            limit_price=entry_limit_price,
        )
        if not self._looks_successful(order_response):
            raise RuntimeError(f"Fyers live order failed: {order_response}")
        actual_entry_price, actual_entry_time = self._resolve_fill_details(
            symbol=symbol,
            side=side,
            qty=qty,
            order_response=order_response,
            fallback_price=entry_price,
        )
        # Silver risk is expressed in absolute points, so rebuild both
        # protection levels from the actual fill instead of proportionally
        # scaling planned prices (which silently changes point distances).
        initial_sl_points = abs(float(entry_price) - float(sl_price))
        final_target_points = abs(float(target_price) - float(entry_price))
        if side == "BUY":
            sl_price = actual_entry_price - initial_sl_points
            target_price = actual_entry_price + final_target_points
        else:
            sl_price = actual_entry_price + initial_sl_points
            target_price = actual_entry_price - final_target_points

        # ── HARD FYERS PROTECTION ───────────────────────────────────────
        # Place protective orders IMMEDIATELY after entry fill so Fyers
        # holds both the initial stop and final target server-side. In the
        # four-input breakeven policy only the SL changes at activation.
        # Both orders are on the reverse side and MIS/INTRADAY so they're
        # linked to the same intraday position.
        entry_order_id = self._extract_order_id(order_response)
        _mark_live_entry_handoff(symbol, entry_order_id, side, qty)
        merged_snapshot = dict(signal_snapshot or {})
        breakeven_mode = (
            merged_snapshot.get("silver_exit_policy")
            == SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN
        )
        breakeven_snapshot = dict(merged_snapshot.get("silver_breakeven") or {})
        if breakeven_mode and breakeven_snapshot.get("final_target_enabled"):
            activation_points = float(breakeven_snapshot.get("activation_points") or 0)
            if activation_points > 0:
                breakeven_snapshot["activation_price"] = (
                    actual_entry_price + activation_points
                    if side == "BUY"
                    else actual_entry_price - activation_points
                )
            breakeven_snapshot["target_price"] = target_price
            breakeven_snapshot["initial_sl_price"] = sl_price
            merged_snapshot["silver_breakeven"] = breakeven_snapshot
        target_required = not breakeven_mode or bool(breakeven_snapshot.get("final_target_enabled"))
        protective = self._place_protective_orders(
            symbol=symbol,
            entry_side=side,
            qty=qty,
            sl_price=sl_price,
            target_price=target_price,
            include_target=target_required,
        )
        if not protective.get("sl_order_id") or (target_required and not protective.get("target_order_id")):
            # Live positions must not remain naked. If either protective
            # order failed, immediately flatten the fresh entry at Fyers
            # and refuse to persist the position in our database.
            exit_side = "SELL" if side.upper() == "BUY" else "BUY"
            flatten_response = self._place_live_order(symbol, exit_side, qty)
            _clear_live_entry_handoff(symbol, entry_order_id)
            raise RuntimeError(
                "Fyers live protection arming failed: "
                f"sl_order_id={protective.get('sl_order_id')!r} "
                f"target_order_id={protective.get('target_order_id')!r} "
                f"sl_error={protective.get('sl_error')!r} "
                f"target_error={protective.get('target_error')!r} "
                f"flatten_response={flatten_response!r}"
            )
        # Persist Fyers order IDs in signal_snapshot so the reconciliation
        # thread can look them up and detect SL/Target fills without any
        # in-memory dependency (survives container restarts).
        merged_snapshot["fyers_entry_order_id"] = entry_order_id
        merged_snapshot["fyers_sl_order_id"] = protective.get("sl_order_id")
        merged_snapshot["fyers_target_order_id"] = protective.get("target_order_id")
        merged_snapshot["fyers_sl_error"] = protective.get("sl_error")
        merged_snapshot["fyers_target_error"] = protective.get("target_error")
        merged_snapshot["fyers_protection_policy"] = (
            "sl_and_final_target_with_breakeven" if breakeven_mode else "sl_and_target"
        )

        confirmed_entry_time = self._safe_entry_time(
            actual_entry_time,
            self._extract_fill_time(order_response),
            entry_time,
        )
        try:
            super().open_trade(
                symbol,
                side,
                qty,
                actual_entry_price,
                sl_price,
                target_price,
                entry_trigger,
                merged_snapshot,
                # Fyers tradebook time is authoritative when available. The
                # strategy event time is a safe fallback for delayed tradebook
                # hydration and keeps paper/live audit rows aligned.
                entry_time=confirmed_entry_time,
            )
        finally:
            # If persistence itself fails, leave no stale local ownership
            # marker behind. Reconciliation can then safely recover the real
            # Fyers position on its next pass.
            _clear_live_entry_handoff(symbol, entry_order_id)

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        symbol = position["symbol"]
        with _symbol_lock(symbol):
            return self._close_trade_locked(position, exit_price, exit_reason)

    def _close_trade_locked(self, position: dict, exit_price: float, exit_reason: str):
        symbol = position["symbol"]
        side = position["side"]
        qty = int(position["qty"])
        exit_side = "SELL" if side == "BUY" else "BUY"

        # Design B pre-flight — Fyers is the single source of truth for
        # whether this position is still open. If Fyers already reports
        # net_qty=0 (SL-Limit fired, Target LIMIT fired, user manually
        # exited via Fyers app, EOD square-off by broker), the market
        # exit we're about to place would just OPEN a fresh reverse
        # position. Cancel any leftover protective orders and record
        # the DB closure at the caller's price — no fresh market order.
        #
        # 2026-08-26 incident: our tick handler saw LTP cross SL 4s after
        # the Fyers-side SL fired; the resulting duplicate market SELL
        # took the client from flat → short 1. Guard prevents it.
        current_net = _fyers_current_net_qty(symbol)
        if current_net is not None and current_net == 0:
            print(
                f"[live_broker] pre-flight SKIP close_trade {symbol}: "
                f"already flat at Fyers (net_qty=0, reason={exit_reason}) — "
                f"no market exit sent; cancelling residual protective orders"
            )
            snapshot = position.get("signal_snapshot") or {}
            protective_ids = [
                snapshot.get("fyers_sl_order_id"),
                snapshot.get("fyers_target_order_id"),
            ]
            for oid in protective_ids:
                if oid:
                    self._cancel_fyers_order(
                        oid,
                        reason=f"close_trade:already_flat:{exit_reason}",
                    )
            # Use the actual Fyers fill (price + time) if we can find it —
            # otherwise the exit row shows the reconcile-loop moment, not
            # when the SL/Target actually fired.
            actual_price, actual_time = self._resolve_closed_fill_details(
                position=position,
                exit_side=exit_side,
                qty=qty,
                fallback_price=exit_price,
                candidate_order_ids=protective_ids,
            )
            super().close_trade(
                position,
                actual_price,
                exit_reason,
                exit_time=self._safe_exit_time(position, actual_time),
            )
            return

        # Cancel any pending protective orders BEFORE placing the manual
        # market exit. Otherwise the SL/Target order stays live at Fyers
        # and can fire against our closed position — which Fyers would
        # execute as a fresh reverse trade (accidental short/long).
        snapshot = position.get("signal_snapshot") or {}
        sl_order_id = snapshot.get("fyers_sl_order_id")
        if sl_order_id:
            sl_response = self._cancel_fyers_order(
                sl_order_id,
                reason=f"close_trade:{exit_reason}",
            )
            if isinstance(sl_response, dict) and sl_response.get("code") == -52:
                print(
                    f"[live_broker] SKIP market exit: SL {sl_order_id} already "
                    "filled at Fyers"
                )
                # -52 == SL already filled at Fyers. Look up its actual fill
                # (price + time) from tradebook so the closed-trade row
                # reflects when it really fired, not when we noticed.
                actual_price, actual_time = self._resolve_closed_fill_details(
                    position=position,
                    exit_side=exit_side,
                    qty=qty,
                    fallback_price=exit_price,
                    candidate_order_ids=[sl_order_id],
                )
                super().close_trade(
                    position,
                    actual_price,
                    exit_reason,
                    exit_time=self._safe_exit_time(position, actual_time),
                )
                return

        target_order_id = snapshot.get("fyers_target_order_id")
        if target_order_id:
            self._cancel_fyers_order(
                target_order_id,
                reason=f"close_trade:{exit_reason}",
            )

        order_response = self._place_live_order(position["symbol"], exit_side, qty)
        if not self._looks_successful(order_response):
            raise RuntimeError(f"Fyers live exit order failed: {order_response}")
        actual_exit_price, actual_exit_time = self._resolve_fill_details(
            symbol=position["symbol"],
            side=exit_side,
            qty=qty,
            order_response=order_response,
            fallback_price=exit_price,
        )
        super().close_trade(
            position,
            actual_exit_price,
            exit_reason,
            exit_time=self._safe_exit_time(position, actual_exit_time),
        )

    # ── Protective order helpers ──────────────────────────────────────
    def _place_protective_orders(
        self,
        symbol: str,
        entry_side: str,
        qty: int,
        sl_price: float,
        target_price: float,
        include_target: bool = True,
    ) -> dict:
        """Place SL (SLM) + Target (LIMIT) orders on the reverse side of
        the entry. Returns Fyers order IDs (or None + error) for each."""
        exit_side = "SELL" if entry_side.upper() == "BUY" else "BUY"
        result = {
            "sl_order_id": None,
            "target_order_id": None,
            "sl_error": None,
            "target_error": None,
        }

        # Stop-Loss Market (type=4). Fires as market when stopPrice touched.
        sl_id, sl_err = self._place_with_retry(
            "SL", symbol,
            lambda: self._place_slm_order(symbol, exit_side, qty, sl_price),
        )
        result["sl_order_id"] = sl_id
        result["sl_error"] = sl_err

        if include_target:
            # Take-profit Limit (type=1). Fills at target price if market reaches it.
            tp_id, tp_err = self._place_with_retry(
                "Target", symbol,
                lambda: self._place_limit_order(symbol, exit_side, qty, target_price),
            )
            result["target_order_id"] = tp_id
            result["target_error"] = tp_err

        return result

    def _place_with_retry(self, label: str, symbol: str, fn) -> tuple[str | None, str | None]:
        """Call `fn` once; if the response isn't successful, sleep 5s and try
        one more time. Returns (order_id, error_str). Covers transient 429s
        after our tick-rounding + limitPrice fixes reduced the reject rate
        to near-zero — retry is the second layer of protection."""
        for attempt in (1, 2):
            try:
                response = fn()
                if self._looks_successful(response):
                    return self._extract_order_id(response), None
                error = str(response)
                if attempt == 1:
                    print(f"[live_broker] {label} order attempt {attempt} rejected {symbol}, retrying in 5s: {response}")
                    time.sleep(5)
                    continue
                print(f"[live_broker] {label} order rejected {symbol} after retry: {response}")
                return None, error
            except Exception as exc:
                error = str(exc)
                if attempt == 1:
                    print(f"[live_broker] {label} order attempt {attempt} exception {symbol}, retrying in 5s: {exc}")
                    time.sleep(5)
                    continue
                print(f"[live_broker] {label} order exception {symbol} after retry: {exc}")
                return None, error
        return None, "unreachable"

    def _place_slm_order(self, symbol: str, side: str, qty: int, stop_price: float) -> dict:
        preflight_error = self._proxy_preflight(symbol, side, qty, "place_slm")
        if preflight_error:
            return preflight_error
        fyers = get_fyers_model(use_proxy=True)
        # Fyers V3 requires limitPrice > 0 even on SL-M — see _sl_limit_price
        # docstring for why we send a slack limit rather than 0. Both prices
        # rounded to THIS symbol's tick (Rs 1 for MCX, Rs 0.05 for NSE).
        rounded_stop = _round_to_tick(stop_price, symbol=symbol)
        limit_price = _sl_limit_price(rounded_stop, side, symbol=symbol)
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 4,                        # SL-M (Fyers now treats it as SL-Limit under the hood)
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": rounded_stop,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        response = fyers.place_order(payload)
        print(f"[live_broker] place_slm {symbol} {side} x{qty} @stop={rounded_stop} limit={limit_price}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def _place_limit_order(self, symbol: str, side: str, qty: int, limit_price: float) -> dict:
        preflight_error = self._proxy_preflight(symbol, side, qty, "place_limit")
        if preflight_error:
            return preflight_error
        fyers = get_fyers_model(use_proxy=True)
        rounded_limit = _round_to_tick(limit_price, symbol=symbol)
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 1,                        # LIMIT
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": rounded_limit,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        response = fyers.place_order(payload)
        print(f"[live_broker] place_limit {symbol} {side} x{qty} @limit={rounded_limit}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def apply_trailing_stop(self, position: dict, ltp: float, settings: dict) -> dict:
        """Extend PaperBroker's trailing-stop logic to ALSO update the hard
        SL order at Fyers whenever the trailed SL moves. Without this, the
        Fyers-side SL stays at its original level while our database shows
        a tighter trailed SL — a real crash would still exit at the OLD SL,
        defeating the point of trailing."""
        previous_sl = float(position.get("sl_price") or 0)
        updated = super().apply_trailing_stop(position, ltp, settings)
        new_sl = float(updated.get("sl_price") or 0)
        # Only push to Fyers if the SL actually moved and we have a live
        # SL order id to modify.
        if new_sl == previous_sl or new_sl <= 0:
            return updated
        snapshot = updated.get("signal_snapshot") or position.get("signal_snapshot") or {}
        sl_order_id = snapshot.get("fyers_sl_order_id")
        if not sl_order_id:
            # No hard SL to sync — soft-SL-only position.
            return updated
        # Exit side is the OPPOSITE of the position's entry side.
        entry_side = str(updated.get("side") or position.get("side") or "").upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        try:
            modify_response = self._modify_slm_order(
                sl_order_id,
                new_sl,
                exit_side,
                qty=int(position.get("qty") or 0),
                symbol=position.get("symbol"),
            )
            if self._looks_successful(modify_response):
                print(
                    f"[live_broker] trailed hard SL {position.get('symbol')} "
                    f"order {sl_order_id} -> {new_sl:.2f}"
                )
                return updated
            error = str(modify_response)
        except Exception as exc:
            error = str(exc)

        print(
            f"[live_broker] trailed SL modify failed for "
            f"{position.get('symbol')} order {sl_order_id}: {error}"
        )
        if uses_silver_breakeven_stop(position, settings):
            # Never claim that the position is protected at breakeven unless
            # FYERS accepted the amend. Restore the persisted initial stop
            # and let the next tick retry the one-time arm safely.
            rollback = {
                "sl_price": previous_sl,
                "trailing_sl_active": bool(position.get("trailing_sl_active")),
                "signal_snapshot": position.get("signal_snapshot") or {},
            }
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name())
                .update(rollback).eq("id", position["id"]).execute()
            )
            print(
                f"[live_broker] LIVE PROTECTION FAILURE: retained original "
                f"SL {previous_sl:.2f} for {position.get('symbol')} after "
                "breakeven amend failure"
            )
            return {**position, **rollback}
        return updated

    def apply_candle_pair_trailing_stop(
        self,
        position: dict,
        ltp: float,
        first_bar: dict,
        second_bar: dict,
        buffer_points: float,
    ) -> dict:
        """Persist and broker-confirm a Silver Micro 2.0 pair trail move."""
        previous_sl = float(position.get("sl_price") or 0)
        updated = super().apply_candle_pair_trailing_stop(
            position, ltp, first_bar, second_bar, buffer_points
        )
        new_sl = float(updated.get("sl_price") or 0)
        if new_sl == previous_sl or new_sl <= 0:
            return updated

        snapshot = updated.get("signal_snapshot") or position.get("signal_snapshot") or {}
        sl_order_id = snapshot.get("fyers_sl_order_id")
        if not sl_order_id:
            return updated
        entry_side = str(updated.get("side") or position.get("side") or "").upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        try:
            response = self._modify_slm_order(
                sl_order_id,
                new_sl,
                exit_side,
                qty=int(position.get("qty") or 0),
                symbol=position.get("symbol"),
            )
            if self._looks_successful(response):
                print(
                    f"[live_broker] candle-pair hard SL {position.get('symbol')} "
                    f"order {sl_order_id} -> {new_sl:.2f}"
                )
                return updated
            error = str(response)
        except Exception as exc:
            error = str(exc)

        rollback = {
            "sl_price": previous_sl,
            "trailing_sl_active": bool(position.get("trailing_sl_active")),
            "signal_snapshot": position.get("signal_snapshot") or {},
        }
        run_with_supabase(
            lambda supabase: supabase.table(self.positions_table_name())
            .update(rollback).eq("id", position["id"]).execute()
        )
        print(
            f"[live_broker] LIVE PROTECTION FAILURE: retained SL {previous_sl:.2f} "
            f"for {position.get('symbol')} after candle-pair amend failure: {error}"
        )
        return {**position, **rollback}

    def _modify_slm_order(
        self,
        order_id: str,
        new_stop_price: float,
        exit_side: str,
        *,
        qty: int,
        symbol: str | None = None,
    ) -> dict:
        """Update the stopPrice of an existing SLM order at Fyers so a
        trailing-SL adjustment is enforced server-side, not just in our db.

        Must also pass exit_side so the paired limitPrice is recomputed —
        Fyers rejects the modify with the same code -50 as fresh placement
        if limitPrice is 0 or not on tick, so we round + slippage-buffer here
        exactly like _place_slm_order does."""
        fyers = get_fyers_model(use_proxy=True)
        # Per-symbol tick + slack — same rules as fresh SL placement.
        rounded_stop = _round_to_tick(new_stop_price, symbol=symbol)
        limit_price = _sl_limit_price(rounded_stop, exit_side, symbol=symbol)
        if int(qty) < 1:
            raise ValueError("FYERS stop-loss amend requires a positive quantity.")
        payload = {
            "id": str(order_id),
            "type": 4,                                # keep SL-M
            "limitPrice": limit_price,
            "stopPrice": rounded_stop,
            "qty": int(qty),
        }
        response = fyers.modify_order(payload)
        print(f"[live_broker] modify_slm {order_id} @stop={rounded_stop} limit={limit_price}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def _modify_limit_order(
        self,
        order_id: str,
        new_limit_price: float,
        *,
        qty: int,
        symbol: str | None = None,
    ) -> dict:
        """Update the limitPrice of an existing LIMIT order at Fyers.

        Used to move a protective TARGET order after the user edits the
        target from the website. The stop version already exists as
        _modify_slm_order; this is its sibling for pure LIMIT orders.
        """
        fyers = get_fyers_model(use_proxy=True)
        rounded_limit = _round_to_tick(new_limit_price, symbol=symbol)
        if int(qty) < 1:
            raise ValueError("FYERS target amend requires a positive quantity.")
        payload = {
            "id": str(order_id),
            "type": 1,                                # keep LIMIT
            "limitPrice": rounded_limit,
            "qty": int(qty),
        }
        response = fyers.modify_order(payload)
        print(f"[live_broker] modify_limit {order_id} @limit={rounded_limit}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def modify_protection(
        self,
        position_id,
        *,
        sl_price: float | None = None,
        target_price: float | None = None,
    ) -> dict:
        """Edit SL and/or Target on an open live position.

        Order of operations is FYERS-FIRST, DB-SECOND for each leg — if the
        exchange rejects the amend we never write a fake in-sync DB value.
        Unsupplied fields are left untouched. A leg with no stored order id
        (e.g. a soft-SL-only position) skips the Fyers call and updates
        just the DB record — matches how the trailing-SL amender behaves.
        """
        position = self._find_open_position(position_id)
        if not position:
            raise ValueError(f"Position {position_id!r} is not open for this algo.")
        side = str(position.get("side") or "").upper()
        entry_price = float(position.get("entry_price") or 0)
        symbol = position.get("symbol")
        snapshot = position.get("signal_snapshot") or {}
        exit_side = "SELL" if side == "BUY" else "BUY"
        qty = int(position.get("qty") or 0)
        if qty < 1:
            raise ValueError("Live position quantity must be at least 1 before editing protection.")

        updates: dict = {}
        errors: list[str] = []

        if sl_price is not None:
            new_sl = float(sl_price)
            if new_sl <= 0:
                raise ValueError("Stop loss must be greater than zero.")
            if side == "BUY" and entry_price and new_sl >= entry_price:
                raise ValueError("Stop loss for a BUY must be below the entry price.")
            if side == "SELL" and entry_price and new_sl <= entry_price:
                raise ValueError("Stop loss for a SELL must be above the entry price.")
            sl_order_id = snapshot.get("fyers_sl_order_id")
            if sl_order_id:
                try:
                    response = self._modify_slm_order(
                        sl_order_id, new_sl, exit_side, qty=qty, symbol=symbol
                    )
                    if not self._looks_successful(response):
                        errors.append(f"Fyers refused SL amend: {response}")
                    else:
                        updates["sl_price"] = round(new_sl, 2)
                except Exception as exc:
                    errors.append(f"Fyers SL amend raised: {exc}")
            else:
                errors.append(
                    "No tracked FYERS stop-loss order was found for this live position. "
                    "Refresh the position or re-arm protection before editing SL."
                )

        if target_price is not None:
            new_target = float(target_price)
            if new_target <= 0:
                raise ValueError("Target must be greater than zero.")
            if side == "BUY" and entry_price and new_target <= entry_price:
                raise ValueError("Target for a BUY must be above the entry price.")
            if side == "SELL" and entry_price and new_target >= entry_price:
                raise ValueError("Target for a SELL must be below the entry price.")
            target_order_id = snapshot.get("fyers_target_order_id")
            if target_order_id:
                try:
                    response = self._modify_limit_order(
                        target_order_id, new_target, qty=qty, symbol=symbol
                    )
                    if not self._looks_successful(response):
                        errors.append(f"Fyers refused target amend: {response}")
                    else:
                        updates["target_price"] = round(new_target, 2)
                except Exception as exc:
                    errors.append(f"Fyers target amend raised: {exc}")
            else:
                errors.append(
                    "No tracked FYERS target order was found for this live position. "
                    "Refresh the position or re-arm protection before editing target."
                )

        if updates:
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name())
                .update(updates).eq("id", position["id"]).execute()
            )
            print(
                f"[live_broker] protection updated for position {position.get('id')} "
                f"{symbol} {side}: {updates}"
            )

        if errors:
            # Partial-failure path: raise so the endpoint returns 502 and the
            # UI can show what Fyers said. Any successful legs are already
            # persisted so the two sides don't drift silently.
            raise RuntimeError("; ".join(errors))

        return {**position, **updates}

    def _cancel_fyers_order(self, order_id: str, reason: str = "") -> dict:
        try:
            fyers = get_fyers_model(use_proxy=True)
            response = fyers.cancel_order({"id": str(order_id)})
            print(f"[live_broker] cancel_order {order_id} ({reason}): {response}")
            return response if isinstance(response, dict) else {"raw": response}
        except Exception as exc:
            print(f"[live_broker] cancel_order failed {order_id} ({reason}): {exc}")
            return {"s": "error", "message": str(exc)}

    # Positions younger than this get skipped by the external-close sync so a
    # brand-new entry doesn't get wrongly closed before Fyers' positions
    # endpoint has caught up with the fresh fill. Bumped from 60s -> 300s
    # after 2026-08-11 09:19 IST: the endpoint returned net_qty=0 for
    # ABCAPITAL (age=62s) and ABLBL (age=88s) on real 9:15 entries, then
    # correctly reported open_position_count=2 at 09:20:31 and continuously
    # after. The false-close cancelled the protective SL+Target orders and
    # left both positions unprotected at Fyers. 5 minutes covers observed
    # Fyers indexing lag with headroom.
    _EXTERNAL_CLOSE_MIN_AGE_SECONDS = 300.0

    # Second layer of defense: require the Fyers positions endpoint to report
    # net_qty=0 for a symbol on TWO consecutive polls before we treat it as
    # externally closed. A single flaky/inconsistent response can no longer
    # trigger the cancel-siblings + mark-closed cascade. Cleared to 0 whenever
    # Fyers confirms the position is still held.
    _EXTERNAL_CLOSE_CONFIRM_POLLS = 2

    def _sync_externally_closed_positions(
        self, open_positions: list[dict], summary: dict
    ) -> list[dict]:
        """Cross-check our open DB positions against Fyers' current position
        book. Any symbol we still have open that Fyers reports as flat
        (net_qty == 0 or missing) has been closed outside the app — mark it
        closed with reason MANUAL_EXTERNAL_EXIT so check_exits won't fire a
        duplicate MARKET order and accidentally open a reverse position.

        Returns the list of positions still open after sync (i.e. those
        Fyers agrees are still live) so the caller can continue with normal
        SL/Target detection on just those rows.
        """
        try:
            from .fyers_client import get_broker_positions, get_live_ltp_batch
            result = get_broker_positions("live")
        except Exception as exc:
            print(f"[live_broker] position-sync fetch failed: {exc}")
            summary["errors"] += 1
            return open_positions  # can't sync -> pass through

        if not isinstance(result, dict) or not result.get("available"):
            # Fyers unavailable / cache stale — safer to skip than false-close.
            return open_positions

        fyers_by_symbol: dict[str, dict] = {}
        for row in result.get("positions") or []:
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            try:
                net_qty = float(row.get("net_qty", 0) or 0)
            except (TypeError, ValueError):
                net_qty = 0.0
            fyers_by_symbol[symbol] = {"net_qty": net_qty, "row": row}

        remaining: list[dict] = []
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for position in open_positions:
            symbol = str(position.get("symbol") or "").strip()
            entry_raw = position.get("entry_time") or ""
            # Skip freshly-opened positions — Fyers' positions() endpoint can
            # take up to a minute to reflect a just-filled entry.
            try:
                entry_dt = datetime.datetime.fromisoformat(str(entry_raw).replace("Z", "+00:00"))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=datetime.timezone.utc)
                age_seconds = (now_utc - entry_dt).total_seconds()
            except (TypeError, ValueError):
                age_seconds = self._EXTERNAL_CLOSE_MIN_AGE_SECONDS + 1

            fyers_row = fyers_by_symbol.get(symbol)
            fyers_holds_it = fyers_row is not None and abs(fyers_row["net_qty"]) >= 1

            # Confirmation counter — Fyers positions endpoint has been observed
            # to return net_qty=0 momentarily even for held positions (indexing
            # lag around 09:15 open). We require N consecutive zero-polls before
            # trusting the "flat" signal.
            if not hasattr(self, "_external_zero_poll_count"):
                self._external_zero_poll_count = {}
            if fyers_holds_it:
                self._external_zero_poll_count.pop(symbol, None)
            else:
                self._external_zero_poll_count[symbol] = (
                    self._external_zero_poll_count.get(symbol, 0) + 1
                )
            zero_polls = self._external_zero_poll_count.get(symbol, 0)
            confirmed_flat = zero_polls >= self._EXTERNAL_CLOSE_CONFIRM_POLLS

            if fyers_holds_it or age_seconds < self._EXTERNAL_CLOSE_MIN_AGE_SECONDS or not confirmed_flat:
                if not fyers_holds_it and age_seconds >= self._EXTERNAL_CLOSE_MIN_AGE_SECONDS and not confirmed_flat:
                    print(
                        f"[live_broker] external-close pending confirmation for {symbol}: "
                        f"zero-poll {zero_polls}/{self._EXTERNAL_CLOSE_CONFIRM_POLLS}, age={int(age_seconds)}s"
                    )
                remaining.append(position)
                continue

            # Fyers is flat on this symbol but we still show it open. Close
            # in our DB at the last known LTP (approximation — actual exit
            # price would need per-order lookup) and cancel any resting SL/
            # Target orders to be safe.
            print(
                f"[live_broker] external-close detected {symbol}: "
                f"Fyers net_qty=0, age={int(age_seconds)}s — marking "
                f"MANUAL_EXTERNAL_EXIT"
            )
            snapshot = position.get("signal_snapshot") or {}
            for order_key in ("fyers_sl_order_id", "fyers_target_order_id"):
                oid = snapshot.get(order_key)
                if oid:
                    self._cancel_fyers_order(oid, reason="manual_external_exit_cleanup")

            # Prefer the actual Fyers fill (price + time) from tradebook so
            # the closed-trade row reflects when the exit really happened.
            # Fall back to current LTP + reconcile-now if the tradebook row
            # isn't findable.
            entry_side = str(position.get("side") or "").upper()
            exit_side = "SELL" if entry_side == "BUY" else "BUY"
            qty_val = int(position.get("qty") or 0)
            actual_price, actual_time = self._resolve_closed_fill_details(
                position=position,
                exit_side=exit_side,
                qty=qty_val,
                fallback_price=None,
                candidate_order_ids=[
                    snapshot.get("fyers_sl_order_id"),
                    snapshot.get("fyers_target_order_id"),
                ],
            )
            if actual_price is None:
                try:
                    ltp_map = get_live_ltp_batch([symbol])
                    actual_price = float(ltp_map.get(symbol) or position.get("entry_price") or 0)
                except Exception:
                    actual_price = float(position.get("entry_price") or 0)

            try:
                super().close_trade(
                    position,
                    actual_price,
                    exit_reason="MANUAL_EXTERNAL_EXIT",
                    exit_time=self._safe_exit_time(position, actual_time),
                )
                summary["externally_closed"] += 1
                # Successful close — drop the confirmation counter so we start
                # fresh if the same symbol is re-entered later in the session.
                self._external_zero_poll_count.pop(symbol, None)
            except Exception as exc:
                print(f"[live_broker] external-close DB write failed for {symbol}: {exc}")
                summary["errors"] += 1
                # Keep in remaining so we retry next cycle rather than losing track.
                remaining.append(position)

        return remaining

    # ── Fyers → us real-time sync (2026-08-26 Design B, Component 2) ───
    # Handle a single event pushed by the Fyers Order Update WebSocket.
    # Called from fyers_order_updates.connect_order_update_feed via a
    # dispatch thread. Idempotent — receiving the same event twice must
    # not double-write / double-cancel.
    #
    # event shape (normalized by fyers_order_updates._normalize_event):
    #   {"kind": "order" | "trade" | "position",
    #    "symbol": "MCX:SILVERMIC26AUGFUT",
    #    "order_id": "...",          # order events only
    #    "status": 1|2|4|5|6,        # order events; 2=FILLED, 1=CANCELLED, 5=REJECTED
    #    "side": "BUY" | "SELL",     # order/trade
    #    "traded_price": float,      # fills only
    #    "traded_qty": int,          # fills only
    #    "net_qty": int,             # position events only
    #    "traded_at": ISO str,       # optional
    #    "raw": {...}}               # the untouched payload for logging
    _ORDER_STATUS_FILLED = 2
    _ORDER_STATUS_CANCELLED = 1
    _ORDER_STATUS_REJECTED = 5

    def handle_order_event(self, event: dict) -> dict:
        """Route one Fyers order-update event to the right DB action.

        Returns a small dict describing what we did — used by the caller
        for logging and by the smoke tests. Never raises for a routing
        decision; only raises for underlying DB write failures.
        """
        if not isinstance(event, dict):
            return {"action": "ignored", "reason": "non-dict-event"}
        kind = event.get("kind")
        symbol = str(event.get("symbol") or "").strip()
        if not symbol:
            return {"action": "ignored", "reason": "missing-symbol", "kind": kind}

        with _symbol_lock(symbol):
            if kind == "order":
                return self._handle_order_status_event(event, symbol)
            if kind == "position":
                return self._handle_position_snapshot_event(event, symbol)
            if kind == "trade":
                # Trade events currently duplicate the fill info we already
                # get from the order event with status=2. Keep them for
                # visibility only.
                return {"action": "ignored", "reason": "trade-event-covered-by-order", "symbol": symbol}
            return {"action": "ignored", "reason": f"unknown-kind:{kind}", "symbol": symbol}

    def _handle_order_status_event(self, event: dict, symbol: str) -> dict:
        order_id = str(event.get("order_id") or "").strip()
        status = event.get("status")
        try:
            status_int = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_int = None

        matched_position, matched_role = self._find_position_by_order_id(order_id)

        if status_int == self._ORDER_STATUS_FILLED:
            handoff = _live_entry_handoff(symbol)
            if handoff and str(handoff.get("order_id") or "") == order_id:
                print(
                    f"[live_broker] WS push entry {symbol} order {order_id} is awaiting "
                    "local persistence — deferring external-order classification"
                )
                return {"action": "entry_pending_persist", "symbol": symbol, "order_id": order_id}
            if matched_position and matched_role in ("sl", "target"):
                # Fyers-side SL or Target fired. Close DB immediately —
                # no waiting on the 30s reconciliation poll. Also cancels
                # the sibling protective order.
                fill_price = float(event.get("traded_price") or matched_position.get(
                    "sl_price" if matched_role == "sl" else "target_price"
                ) or 0.0)
                exit_reason = f"{matched_role.upper()}_FYERS"
                if matched_role == "sl" and self._is_trailing_stop(matched_position):
                    exit_reason = "TRAILING_SL_FYERS"
                snapshot = matched_position.get("signal_snapshot") or {}
                sibling_key = "fyers_target_order_id" if matched_role == "sl" else "fyers_sl_order_id"
                sibling_id = snapshot.get(sibling_key)
                if sibling_id:
                    self._cancel_fyers_order(sibling_id, reason=f"{exit_reason}_ws_push_sibling_cancel")
                normalized_exit_time = self._safe_exit_time(
                    matched_position, event.get("traded_at")
                )
                try:
                    super().close_trade(
                        matched_position,
                        fill_price,
                        exit_reason=exit_reason,
                        exit_time=normalized_exit_time,
                    )
                except TypeError:
                    super().close_trade(matched_position, fill_price, exit_reason)
                print(
                    f"[live_broker] WS push closed {symbol} {exit_reason} @ {fill_price} "
                    f"(order {order_id})"
                )
                return {"action": "closed", "symbol": symbol, "role": matched_role,
                        "exit_reason": exit_reason, "fill_price": fill_price}

            # Unknown fill for this symbol — a MANUAL entry or exit the
            # client placed themselves in the Fyers app. Log so it lands
            # in Railway; downstream reconciliation will pick up the
            # net_qty change if it opened a new position.
            side = str(event.get("side") or "").upper()
            print(
                f"[live_broker] WS push MANUAL_EXTERNAL {side} {symbol} "
                f"@ {event.get('traded_price')} qty={event.get('traded_qty')} "
                f"(order {order_id}) — not one of our tracked protective orders"
            )
            return {"action": "logged_manual_fill", "symbol": symbol, "side": side,
                    "order_id": order_id}

        if status_int == self._ORDER_STATUS_CANCELLED and matched_position and matched_role in ("sl", "target"):
            # A protective order was cancelled (possibly by the client
            # from Fyers UI). Clear its id from our snapshot so
            # close_trade doesn't try to cancel it again later.
            snapshot = dict(matched_position.get("signal_snapshot") or {})
            snapshot[f"fyers_{matched_role}_order_id"] = None
            self._persist_snapshot(matched_position, snapshot)
            print(
                f"[live_broker] WS push {matched_role.upper()} order cancelled at Fyers "
                f"for {symbol} (order {order_id}) — snapshot cleared"
            )
            return {"action": "cleared_snapshot", "symbol": symbol, "role": matched_role,
                    "order_id": order_id}

        return {"action": "ignored", "reason": f"status:{status_int}", "symbol": symbol,
                "order_id": order_id}

    def _handle_position_snapshot_event(self, event: dict, symbol: str) -> dict:
        try:
            net_qty = float(event.get("net_qty", 0) or 0)
        except (TypeError, ValueError):
            net_qty = 0.0

        matched = self._find_open_position_for_symbol(symbol)
        if matched and net_qty == 0:
            # Fyers pushed "you are flat on this symbol" but our DB still
            # thinks it's open. This is exactly the today-morning stuck-
            # entry case: client manually exited from Fyers app, and we
            # otherwise would have waited for the 30s poll + 2-poll
            # confirmation + 300s grace before catching up.
            snapshot = matched.get("signal_snapshot") or {}
            for order_key in ("fyers_sl_order_id", "fyers_target_order_id"):
                oid = snapshot.get(order_key)
                if oid:
                    self._cancel_fyers_order(oid, reason="ws_push_position_flat_cleanup")
            exit_price = float(matched.get("_last_ltp") or matched.get("entry_price") or 0.0)
            try:
                super().close_trade(
                    matched, exit_price, exit_reason="MANUAL_EXTERNAL_EXIT",
                )
            except TypeError:
                super().close_trade(matched, exit_price, "MANUAL_EXTERNAL_EXIT")
            print(
                f"[live_broker] WS push MANUAL_EXTERNAL_EXIT {symbol} — Fyers reports "
                f"net_qty=0, DB row force-synced (no polling delay)"
            )
            return {"action": "closed", "symbol": symbol, "exit_reason": "MANUAL_EXTERNAL_EXIT"}

        if not matched and abs(net_qty) >= 1:
            handoff = _live_entry_handoff(symbol)
            if handoff:
                print(
                    f"[live_broker] WS push position {symbol} net_qty={net_qty} is awaiting "
                    "local entry persistence — deferring external-entry classification"
                )
                return {"action": "entry_pending_persist", "symbol": symbol, "net_qty": net_qty}
            # Manual entry at Fyers we didn't place. Don't insert a fake
            # tracking row (the algo's guards would then think it owns
            # this position). Just log so audit sees it.
            print(
                f"[live_broker] WS push MANUAL_EXTERNAL_ENTRY {symbol} net_qty={net_qty} — "
                "not managed by algo; algo entry guard will still block duplicate entry"
            )
            return {"action": "logged_manual_entry", "symbol": symbol, "net_qty": net_qty}

        return {"action": "ignored", "reason": "position-in-sync", "symbol": symbol, "net_qty": net_qty}

    # ── Helpers for the WS handler ────────────────────────────────────
    def _find_position_by_order_id(self, order_id: str) -> tuple[dict | None, str | None]:
        """Return (position, role) where role is 'sl', 'target', or 'entry',
        or (None, None) if no open position matches this Fyers order id."""
        if not order_id:
            return None, None
        oid = str(order_id).strip()
        try:
            positions = self.open_positions()
        except Exception:
            return None, None
        for position in positions:
            snap = position.get("signal_snapshot") or {}
            if str(snap.get("fyers_sl_order_id") or "") == oid:
                return position, "sl"
            if str(snap.get("fyers_target_order_id") or "") == oid:
                return position, "target"
            if str(snap.get("fyers_entry_order_id") or "") == oid:
                return position, "entry"
        return None, None

    def _find_open_position_for_symbol(self, symbol: str) -> dict | None:
        target = str(symbol or "").strip().upper()
        try:
            positions = self.open_positions()
        except Exception:
            return None
        for position in positions:
            if str(position.get("symbol") or "").strip().upper() == target:
                return position
        return None

    def _persist_snapshot(self, position: dict, snapshot: dict) -> None:
        """Best-effort snapshot update. Falls back to in-memory only if
        the parent broker doesn't expose a persistence hook — the smoke
        tests exercise both paths."""
        position["signal_snapshot"] = snapshot
        updater = getattr(super(), "update_position_snapshot", None)
        if callable(updater):
            try:
                updater(position, snapshot)
            except Exception as exc:
                print(f"[live_broker] snapshot persist failed for {position.get('symbol')}: {exc}")

    def _bootstrap_broker_only_positions(
        self,
        watchlist: list[str] | None,
        summary: dict,
    ) -> int:
        """Insert a live_positions row for each FYERS-held position in this
        strategy's watchlist that our DB doesn't already know about.

        The row is marked as a broker-recovered position and given
        unreachable SL / Target values so the algo's own
        ``check_exits`` loop can never fire a MARKET order against it.
        FYERS remains the sole exit path. When the user closes the
        position from the FYERS app the next reconcile pass sees
        ``net_qty=0`` and the existing external-close logic writes a
        MANUAL_EXTERNAL_EXIT row into ``live_trades`` — which is what
        makes the trade land in Closed Trades Today with a full audit
        trail (entry, exit, side, qty) instead of silently disappearing
        from Open Positions.
        """
        try:
            from .fyers_client import get_broker_positions
            result = get_broker_positions("live")
        except Exception as exc:
            print(f"[live_broker] broker-only bootstrap fetch failed: {exc}")
            return 0
        if not isinstance(result, dict) or not result.get("available"):
            return 0

        watchlist_upper = {str(s or "").strip().upper() for s in (watchlist or []) if s}
        # Empty watchlist = "don't bootstrap anything". Callers that want
        # to bootstrap every FYERS symbol pass a sentinel; the engine now
        # always passes the strategy's own watchlist, so an unset value
        # means "do nothing" which is the safe default.
        if not watchlist_upper:
            return 0

        known_symbols = {
            str(p.get("symbol") or "").strip().upper()
            for p in self.open_positions()
        }

        inserted = 0
        for row in result.get("positions") or []:
            symbol = str(row.get("symbol") or "").strip()
            symbol_upper = symbol.upper()
            if not symbol or symbol_upper not in watchlist_upper:
                continue
            if symbol_upper in known_symbols:
                continue  # DB already tracks this — algo-owned or already bootstrapped.
            if _live_entry_handoff(symbol):
                print(
                    f"[live_broker] bootstrap deferred for {symbol}: accepted algo entry "
                    "is awaiting local persistence"
                )
                continue
            try:
                net_qty = float(row.get("net_qty", 0) or 0)
            except (TypeError, ValueError):
                net_qty = 0.0
            if abs(net_qty) < 1:
                continue

            side = "SELL" if net_qty < 0 else "BUY"
            entry_price = float(row.get("entry_price") or 0)
            if entry_price <= 0:
                entry_price = float(row.get("ltp") or 0)
            if entry_price <= 0:
                continue

            # Unreachable protective levels so check_exits' ltp<=sl (BUY)
            # and ltp>=sl (SELL) branches can never fire.
            if side == "BUY":
                sl_price = 0.0
                target_price = 1_000_000_000.0
            else:
                sl_price = 1_000_000_000.0
                target_price = 0.0

            entry_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
            row_payload = {
                "algo_id": self.storage_algo_id(),
                "symbol": symbol,
                "side": side,
                "qty": int(abs(net_qty)),
                "entry_price": entry_price,
                "sl_price": sl_price,
                "target_price": target_price,
                "status": "open",
                "entry_time": entry_time,
                "entry_trigger": "Recovered from FYERS broker position",
                "signal_snapshot": {
                    "origin": "fyers_recovered_position",
                    "fyers_app_managed": True,
                    "symbol": symbol,
                    "side": side,
                    "entry_price": entry_price,
                },
            }
            try:
                run_with_supabase(
                    lambda supabase: supabase.table(self.positions_table_name())
                    .insert(row_payload).execute()
                )
                inserted += 1
                print(
                    f"[live_broker] bootstrapped recovered FYERS position {symbol} "
                    f"{side} qty={int(abs(net_qty))} @ {entry_price:.2f} — will "
                    f"land in Closed Trades when FYERS reports flat"
                )
            except Exception as exc:
                print(f"[live_broker] bootstrap insert failed for {symbol}: {exc}")
                continue

        if inserted:
            summary.setdefault("bootstrapped_fyers_app", 0)
            summary["bootstrapped_fyers_app"] += inserted
        return inserted

    def reconcile_open_positions(self, watchlist: list[str] | None = None) -> dict:
        """Poll Fyers orderbook and detect SL/Target fills. For each fill:
        close our position record + cancel the sibling protective order so
        it doesn't accidentally reverse the position later. Meant to be
        called every ~30 seconds by the engine background loop.

        ``watchlist`` restricts the fyers-app-managed bootstrap step to
        symbols this strategy actually cares about, so Silver's LiveBroker
        never bootstraps a random NSE stock the user might have bought
        directly in the FYERS app (and vice versa).

        Returns a summary dict: {'reconciled': N, 'errors': M}.
        """
        summary = {"reconciled": 0, "errors": 0, "already_closed": 0, "externally_closed": 0}

        # Step 0 — for each symbol in this strategy's watchlist that FYERS
        # reports as HELD but our DB has no matching open row (user opened
        # the position directly in the FYERS app), insert a placeholder
        # live_positions row. That row rides the existing external-close
        # detection path in Step 1, so when FYERS reports the position
        # flat the algo writes a MANUAL_EXTERNAL_EXIT row into
        # live_trades — which is what puts it into the Closed Trades panel.
        try:
            self._bootstrap_broker_only_positions(watchlist, summary)
        except Exception as exc:
            print(f"[live_broker] fyers-app-position bootstrap error: {exc}")
            summary["errors"] += 1

        open_positions = self.open_positions()
        if not open_positions:
            return summary

        # Step 1 — detect positions closed externally at Fyers (user hit Exit
        # in the Fyers app, EOD square-off, whatever). Without this, our
        # check_exits loop can later fire a MARKET exit for a position that
        # is already flat at Fyers — which Fyers then executes as a NEW
        # reverse trade (accidental short/long). Happened all day on 2026-08-10.
        remaining_open = self._sync_externally_closed_positions(open_positions, summary)
        if not remaining_open:
            return summary
        open_positions = remaining_open

        # Step 2 — fetch the orderbook once and index by order_id for O(1) lookup.
        try:
            fyers = get_fyers_model(use_proxy=False)  # read-only, direct
            response = fyers.orderbook()
        except Exception as exc:
            print(f"[live_broker] reconcile orderbook fetch failed: {exc}")
            summary["errors"] += 1
            return summary

        orders_by_id: dict[str, dict] = {}
        for row in self._iter_rows(response):
            oid = self._extract_order_id(row)
            if oid:
                orders_by_id[oid] = row

        for position in open_positions:
            snapshot = position.get("signal_snapshot") or {}
            sl_id = snapshot.get("fyers_sl_order_id")
            tp_id = snapshot.get("fyers_target_order_id")
            symbol = position.get("symbol")

            filled_order = None
            filled_reason = None
            sibling_id = None

            for oid, reason, sibling in (
                (sl_id, "SL", tp_id),
                (tp_id, "TARGET", sl_id),
            ):
                if not oid:
                    continue
                order = orders_by_id.get(str(oid))
                if not order:
                    continue
                # Fyers orderbook status codes: 2 = FILLED / TRADED,
                # 1 = CANCELLED, 5 = REJECTED, 4 = TRANSIT, 6 = PENDING.
                status_code = self._safe_int(order.get("status"))
                if status_code == 2:
                    filled_order = order
                    filled_reason = reason
                    sibling_id = sibling
                    break

            if not filled_order:
                # No fill yet — but the user might have edited the SL or
                # Target price directly at Fyers (mobile app / web). Push
                # any drift back into our DB so the website's Open Trades
                # panel reflects the current live protective levels.
                self._sync_pending_protection_drift(position, sl_id, tp_id, orders_by_id)
                continue

            # Cancel the sibling protective order (if it's still open) so
            # it doesn't fire against a flat position.
            if sibling_id:
                sibling_order = orders_by_id.get(str(sibling_id))
                if sibling_order:
                    sibling_status = self._safe_int(sibling_order.get("status"))
                    if sibling_status in {4, 6}:  # transit / pending
                        self._cancel_fyers_order(
                            sibling_id,
                            reason=f"{filled_reason}_hit_cancel_sibling",
                        )

            # Record the exit in our positions/trades tables via
            # PaperBroker.close_trade, using the actual Fyers fill price
            # from the filled order.
            fill_price = self._extract_fill_price(filled_order, float(position.get("sl_price") if filled_reason == "SL" else position.get("target_price")))
            exit_reason = f"{filled_reason}_FYERS"
            if filled_reason == "SL" and self._is_trailing_stop(position):
                exit_reason = "TRAILING_SL_FYERS"
            try:
                super().close_trade(
                    position,
                    fill_price,
                    exit_reason=exit_reason,
                    # An orderbook's orderDateTime is when the protection was
                    # created, not necessarily when it filled. Use it only as
                    # a last resort for tradebook rows, never for this path.
                    exit_time=self._safe_exit_time(
                        position,
                        self._extract_fill_time(filled_order, allow_order_time=False),
                    ),
                )
                summary["reconciled"] += 1
                print(f"[live_broker] reconciled {symbol} {filled_reason} @ {fill_price}")
            except Exception as exc:
                summary["errors"] += 1
                print(f"[live_broker] reconcile close_trade failed for {symbol}: {exc}")

        return summary

    def _sync_pending_protection_drift(
        self,
        position: dict,
        sl_id,
        tp_id,
        orders_by_id: dict[str, dict],
    ) -> None:
        """If the user edited stop/limit at Fyers, mirror the drift to our DB.

        Only applies to still-PENDING protective orders (Fyers status 4/6).
        A filled or cancelled order is handled by the surrounding reconcile
        logic; we do not treat those as protection drift.
        """
        drift_updates: dict = {}

        def _current_price(row: dict, price_key: str) -> float | None:
            value = row.get(price_key)
            try:
                num = float(value)
            except (TypeError, ValueError):
                return None
            return num if num > 0 else None

        try:
            if sl_id:
                sl_row = orders_by_id.get(str(sl_id))
                if sl_row and self._safe_int(sl_row.get("status")) in {4, 6}:
                    fyers_stop = _current_price(sl_row, "stopPrice")
                    stored_stop = float(position.get("sl_price") or 0)
                    if (
                        fyers_stop is not None
                        and stored_stop > 0
                        and abs(fyers_stop - stored_stop) >= 0.01
                    ):
                        drift_updates["sl_price"] = round(fyers_stop, 2)
            if tp_id:
                tp_row = orders_by_id.get(str(tp_id))
                if tp_row and self._safe_int(tp_row.get("status")) in {4, 6}:
                    fyers_limit = _current_price(tp_row, "limitPrice")
                    stored_target = float(position.get("target_price") or 0)
                    if (
                        fyers_limit is not None
                        and stored_target > 0
                        and abs(fyers_limit - stored_target) >= 0.01
                    ):
                        drift_updates["target_price"] = round(fyers_limit, 2)
            if not drift_updates:
                return
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name())
                .update(drift_updates).eq("id", position["id"]).execute()
            )
            print(
                f"[live_broker] Fyers protection drift synced back to DB for "
                f"position {position.get('id')} {position.get('symbol')}: "
                f"{drift_updates}"
            )
        except Exception as exc:
            # Never let a drift-sync failure abort the reconcile loop.
            print(
                f"[live_broker] pending protection drift sync failed for "
                f"position {position.get('id')} {position.get('symbol')}: {exc}"
            )

    def _proxy_preflight(self, symbol: str, side: str, qty: int, op: str) -> dict | None:
        """Return a Fyers-style error dict if the proxy is configured but
        unreachable; None when it's safe to place the order.

        Applies to every live order path (entry, SL-M, limit). On 2026-08-17
        a stale bore.pub port silently caused every order to fall through to
        Railway's egress IP, producing 30 x code:-99 "Bad request" rejections
        that took ~8 min to spot. Fail loud instead of leaking a
        non-whitelisted egress IP.
        """
        mode = get_runtime_trading_mode()
        try:
            proxy_url = get_fyers_config(mode).get("proxy_url") or ""
        except Exception as exc:
            # Config unavailable (e.g. test environment with no Fyers env vars).
            # Preflight can't meaningfully check; fall through to whatever the
            # caller has stubbed. Production paths always have config populated.
            print(f"[live_broker] preflight config unavailable ({exc}); skipping proxy check")
            return None
        proxy_reachable = True
        proxy_error: str | None = None
        if proxy_url:
            proxy_reachable, proxy_error = check_proxy_reachable(proxy_url)
        audit_log(
            "fyers",
            f"{op} preflight",
            mode=mode,
            broker="fyers_live",
            proxy_configured=bool(proxy_url),
            proxy_reachable=proxy_reachable,
            proxy_error=proxy_error,
            symbol=symbol,
            side=side,
            qty=qty,
        )
        if proxy_url and not proxy_reachable:
            msg = (
                f"Fyers proxy unreachable ({proxy_error}); refusing to place "
                f"{op} for {symbol} to avoid leaking a non-whitelisted egress IP. "
                f"Check bore/GCP proxy and Railway LIVE_FYERS_PROXY_URL env var."
            )
            print(f"[live_broker] {op} {symbol} {side} x{qty} REFUSED: {msg}")
            return {"s": "error", "code": "proxy_unreachable", "message": msg}
        return None

    def _place_live_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = "MARKET",
        limit_price: float = 0.0,
    ) -> dict:
        preflight_error = self._proxy_preflight(symbol, side, qty, "place_order")
        if preflight_error:
            return preflight_error

        fyers = get_fyers_model(use_proxy=True)
        is_limit = str(order_type).upper() == "LIMIT" and limit_price > 0
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 1 if is_limit else 2,   # 1=Limit 2=Market 3=SL 4=SLM
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": _round_to_tick(limit_price, symbol=symbol) if is_limit else 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        response = fyers.place_order(payload)
        print(f"[live_broker] place_order {symbol} {side} x{qty} type={'LIMIT@'+str(payload['limitPrice']) if is_limit else 'MARKET'}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def _looks_successful(self, response: dict) -> bool:
        # Fyers V3 order responses always include an "id" field, even when
        # the order was REJECTED (e.g. code -99 "RED:'MIS' Orders are
        # disallowed after system square off" still ships with id). Earlier
        # revision treated presence of id as success and recorded phantom
        # positions in the paper broker table for orders Fyers never opened.
        # Trust only the explicit status string.
        if not isinstance(response, dict):
            return False
        status = str(response.get("s") or response.get("status") or "").lower()
        if status in {"error", "err", "failed", "reject", "rejected"}:
            return False
        if status in {"ok", "success", "accepted"}:
            return True
        # No status field at all — fall back to the id heuristic, but only
        # when there is no error/message field indicating a failure.
        has_error_indicator = any(
            response.get(k) for k in ("message", "error", "errmsg", "code")
        )
        if has_error_indicator:
            return False
        return any(response.get(key) for key in ("id", "order_id", "orderId"))

    def _entry_order_params(self, entry_price: float) -> tuple[str, float]:
        """Choose the entry order parameters for this strategy."""
        # Silver Micro breakout entries must not remain pending while price
        # crosses a trigger. Enforce MARKET even if an older saved setting
        # still contains LIMIT from before Silver became market-only.
        if self.algo_id in {"algo3", "algo5"}:
            return "MARKET", 0.0
        try:
            from .strategy_settings import get_settings
            settings = get_settings(self.algo_id)
            order_type = str(settings.get("order_type", "LIMIT")).upper()
        except Exception:
            order_type = "LIMIT"
        if order_type == "LIMIT":
            return "LIMIT", float(entry_price)
        return "MARKET", 0.0

    def _uses_full_price_funds_cap(self, symbol: str) -> bool:
        """True when a naive `available_funds // price` cap is valid.

        NSE/BSE cash-style buys can be roughly sanity-checked against the
        instrument's quoted price. MCX futures cannot: the quoted LTP is a
        contract price, while the broker blocks/approves based on required
        margin. Using full LTP there wrongly rejects valid 1-lot entries
        (e.g. SILVERMIC at 2.4L with ~1.1L wallet balance).
        """
        text = str(symbol or "").strip().upper()
        return not text.startswith("MCX:")

    def _cap_qty_to_live_funds(self, symbol: str, requested_qty: int, entry_price: float) -> int:
        if requested_qty < 1 or entry_price <= 0:
            return 0
        if not self._uses_full_price_funds_cap(symbol):
            return int(requested_qty)
        try:
            funds = get_wallet_balance("live")
        except Exception:
            return int(requested_qty)

        summary = funds.get("summary") if isinstance(funds, dict) else {}
        if not isinstance(summary, dict):
            return int(requested_qty)

        balance = summary.get("available_margin")
        if balance is None:
            balance = summary.get("wallet_balance")
        try:
            affordable_qty = int(float(balance) // float(entry_price))
        except (TypeError, ValueError, ZeroDivisionError):
            return int(requested_qty)
        if affordable_qty < 1:
            return 0
        return min(int(requested_qty), affordable_qty)

    def _resolve_fill_details(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_response: dict,
        fallback_price: float,
    ) -> tuple[float, str | None]:
        fyers = get_fyers_model("live")
        order_id = self._extract_order_id(order_response)
        deadline = time.time() + 8
        latest_match = None
        while time.time() < deadline:
            latest_match = self._find_latest_fill(fyers, symbol, side, qty, order_id)
            if latest_match:
                break
            time.sleep(0.5)
        if not latest_match:
            # Do not substitute an arbitrary historical trade merely to get a
            # timestamp. The strategy event/current time is safer than a false
            # broker fill time and preserves chronological trade records.
            return float(fallback_price), None
        return (
            self._extract_fill_price(latest_match, fallback_price),
            self._extract_fill_time(latest_match),
        )

    def _resolve_closed_fill_details(
        self,
        *,
        position: dict,
        exit_side: str,
        qty: int,
        fallback_price: float | None,
        candidate_order_ids: list,
    ) -> tuple[float | None, str | None]:
        """Look up the actual Fyers fill for a position that was already
        closed at the broker before we tried to exit.

        Used by the three "we didn't fire the market exit" paths:
        pre-flight flat guard, `-52 not pending` shortcut, and external-close
        reconcile. Without this, `close_trade` stamps exit_time as the
        moment we detected the closure — which can be seconds to minutes
        after the SL/Target actually fired. That drift shows up on the
        Closed Trades panel as wrong exit times.

        Returns (price, iso_time) — either can be None if the tradebook
        doesn't surface a matching fill; the caller must handle fallbacks.
        """
        try:
            fyers = get_fyers_model("live")
        except Exception as exc:
            print(f"[live_broker] tradebook fetch failed for fill lookup: {exc}")
            return fallback_price, None
        symbol = str(position.get("symbol") or "").strip()
        if not symbol:
            return fallback_price, None
        # Try each known protective order id first — Fyers matches by
        # order_id are the strongest signal. Fall back to symbol/side/qty.
        matched = None
        for oid in candidate_order_ids or []:
            oid_str = str(oid or "").strip()
            if not oid_str:
                continue
            matched = self._find_latest_fill(fyers, symbol, exit_side, qty, oid_str)
            if matched:
                break
        if matched is None:
            matched = self._find_latest_fill(fyers, symbol, exit_side, qty, None)
        if matched is None:
            return fallback_price, None
        price = self._extract_fill_price(matched, fallback_price if fallback_price is not None else 0.0)
        iso_time = self._extract_fill_time(matched, allow_order_time=False)
        return price, iso_time

    def _find_latest_fill(
        self,
        fyers,
        symbol: str,
        side: str,
        qty: int,
        order_id: str | None,
    ) -> dict | None:
        candidates: list[dict] = []
        for loader in (lambda: fyers.tradebook(), lambda: fyers.tradehistory({})):
            try:
                response = loader()
            except Exception:
                continue
            for row in self._iter_rows(response):
                if not isinstance(row, dict):
                    continue
                row_symbol = str(row.get("symbol") or row.get("fySymbol") or "").upper()
                if row_symbol != symbol.upper():
                    continue
                row_side = self._normalize_side(row.get("side") or row.get("transactionType") or row.get("buySell"))
                if row_side and row_side != side.upper():
                    continue
                row_order_id = self._extract_order_id(row)
                # Once Fyers has returned an order id, never fall back to a
                # same-symbol/same-side row. That can select an older fill and
                # write an entry or exit time from a different trade.
                if order_id and row_order_id != order_id:
                    continue
                row_qty = self._safe_int(
                    row.get("qty"),
                    row.get("filledQty"),
                    row.get("tradedQty"),
                    row.get("fillQty"),
                )
                if row_qty is not None and row_qty != qty:
                    continue
                candidates.append(row)
        if not candidates:
            return None
        candidates.sort(
            key=lambda row: self._parse_fill_time(row) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        )
        return candidates[-1]

    def _iter_rows(self, response) -> list[dict]:
        if isinstance(response, list):
            return [row for row in response if isinstance(row, dict)]
        if not isinstance(response, dict):
            return []
        for key in (
            "tradeBook",
            "tradebook",
            "tradeHistory",
            "tradehistory",
            "orderBook",
            "orderbook",
            "data",
        ):
            rows = response.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
            if isinstance(rows, dict):
                for nested_key in ("rows", "items", "orders", "trades"):
                    nested_rows = rows.get(nested_key)
                    if isinstance(nested_rows, list):
                        return [row for row in nested_rows if isinstance(row, dict)]
        return []

    def _extract_order_id(self, row: dict | None) -> str | None:
        if not isinstance(row, dict):
            return None
        for key in (
            "id", "order_id", "orderId", "orderNumber", "orderNo",
            "fyOrderId", "exchangeOrderId",
        ):
            value = row.get(key)
            if value:
                return str(value)
        return None

    def _normalize_side(self, raw) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip().upper()
        if text in {"1", "BUY", "B"}:
            return "BUY"
        if text in {"-1", "SELL", "S"}:
            return "SELL"
        return None

    def _safe_float(self, *values) -> float | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _safe_int(self, *values) -> int | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return None

    def _extract_fill_price(self, row: dict | None, fallback_price: float) -> float:
        if not isinstance(row, dict):
            return float(fallback_price)
        return float(
            self._safe_float(
                row.get("tradedPrice"),
                row.get("tradePrice"),
                row.get("tradeAvgPrice"),
                row.get("avgPrice"),
                row.get("price"),
                row.get("ltp"),
                fallback_price,
            )
            or fallback_price
        )

    def _extract_fill_time(self, row: dict | None, *, allow_order_time: bool = True) -> str:
        # Return timestamps in IST — the app is Indian-markets-only and
        # trades display clocks in IST. Storing UTC then formatting on
        # the frontend has bitten us in the past; keep the wall-clock
        # reading correct in the DB row itself.
        parsed = self._parse_fill_time(row, allow_order_time=allow_order_time)
        if parsed:
            return parsed.astimezone(IST).isoformat()
        return datetime.datetime.now(IST).isoformat()

    @staticmethod
    def _is_trailing_stop(position: dict) -> bool:
        """Whether a filled broker stop is the currently trailed stop."""
        snapshot = position.get("signal_snapshot") or {}
        trailing = snapshot.get("trailing") if isinstance(snapshot, dict) else None
        return bool(position.get("trailing_sl_active")) or bool(
            isinstance(trailing, dict) and trailing.get("activated")
        )

    def _parse_fill_time(
        self,
        row: dict | None,
        *,
        allow_order_time: bool = True,
    ) -> datetime.datetime | None:
        if not isinstance(row, dict):
            return None
        keys = [
            "tradeTime",
            "tradeDateTime",
            "tradedAt",
            "tradedOn",
            "updatedAt",
            "updated_at",
            "timestamp",
            "time",
        ]
        if allow_order_time:
            keys.extend(("orderDateTime", "createdAt"))
        for key in keys:
            value = row.get(key)
            parsed = self._coerce_datetime(value)
            if parsed:
                return parsed
        return None

    def _coerce_datetime(self, value) -> datetime.datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 1_000_000_000_000:
                number /= 1000
            try:
                return datetime.datetime.fromtimestamp(number, tz=datetime.timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        iso_candidate = text.replace("Z", "+00:00")
        try:
            parsed = datetime.datetime.fromisoformat(iso_candidate)
            if parsed.tzinfo is None:
                # FYERS returns no-offset wall-clock timestamps in IST.
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(datetime.timezone.utc)
        except ValueError:
            pass
        for fmt in (
            "%d-%m-%Y %H:%M:%S",
            "%d-%b-%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
        ):
            try:
                return datetime.datetime.strptime(text, fmt).replace(tzinfo=IST).astimezone(datetime.timezone.utc)
            except ValueError:
                continue
        return None

    def _safe_entry_time(
        self,
        candidate: str | None,
        broker_confirmed: str | None,
        fallback: str | None,
    ) -> str:
        """Prefer the true FYERS fill time, then FYERS order confirmation.

        The strategy event time is only a last resort. This keeps the UI and
        persisted trade row aligned with the broker-confirmed market-order
        moment even when the tradebook fill hydrates a few seconds later.
        """
        return candidate or broker_confirmed or fallback or datetime.datetime.now(IST).isoformat()

    def _safe_exit_time(self, position: dict, candidate: str | None) -> str:
        """Never persist an exit before the recorded entry.

        A broker orderbook can expose its creation time instead of execution
        time. If that value is stale, using the reconciliation timestamp is
        truthful enough for the audit row and avoids impossible intervals.
        Output is IST-tagged for consistency with the paper broker.
        """
        now = datetime.datetime.now(IST)
        exit_at = self._coerce_datetime(candidate) if candidate else now
        if exit_at is None:
            exit_at = now
        entry_at = self._coerce_datetime(position.get("entry_time"))
        if entry_at and exit_at <= entry_at:
            print(
                "[live_broker] rejected non-monotonic Fyers exit timestamp "
                f"symbol={position.get('symbol')} entry={entry_at.isoformat()} "
                f"candidate_exit={exit_at.isoformat()}"
            )
            exit_at = now
        return exit_at.astimezone(IST).isoformat()
