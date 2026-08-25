"""
LiveBroker wraps the same persistence/accounting model as PaperBroker,
but sends actual Fyers orders before recording the trade in the app.

The live path writes to separate live_* tables so the paper flow stays
isolated and backwards-compatible.
"""
from __future__ import annotations

import datetime
import time

from .audit_log import audit_log
from .fyers_client import get_fyers_model, get_wallet_balance
from .paper_broker import PaperBroker
from .proxy_health import check_proxy_reachable
from .runtime_mode import get_fyers_config, get_runtime_trading_mode
from .supabase_client import run_with_supabase
from .timezone import IST
from .trailing_stop import SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN, uses_silver_breakeven_stop


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
            entry_time=self._safe_entry_time(actual_entry_time, entry_time),
        )

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        side = position["side"]
        qty = int(position["qty"])
        exit_side = "SELL" if side == "BUY" else "BUY"

        # Cancel any pending protective orders BEFORE placing the manual
        # market exit. Otherwise the SL/Target order stays live at Fyers
        # and can fire against our closed position — which Fyers would
        # execute as a fresh reverse trade (accidental short/long).
        snapshot = position.get("signal_snapshot") or {}
        for order_id_key in ("fyers_sl_order_id", "fyers_target_order_id"):
            protective_order_id = snapshot.get(order_id_key)
            if protective_order_id:
                self._cancel_fyers_order(protective_order_id, reason=f"close_trade:{exit_reason}")

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
            modify_response = self._modify_slm_order(sl_order_id, new_sl, exit_side, symbol=position.get("symbol"))
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

    def _modify_slm_order(self, order_id: str, new_stop_price: float, exit_side: str, symbol: str | None = None) -> dict:
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
        payload = {
            "id": str(order_id),
            "type": 4,                                # keep SL-M
            "limitPrice": limit_price,
            "stopPrice": rounded_stop,
            "qty": 0,                                 # 0 = keep current qty
        }
        response = fyers.modify_order(payload)
        print(f"[live_broker] modify_slm {order_id} @stop={rounded_stop} limit={limit_price}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

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

            # Use current LTP as the best available exit price. This won't
            # match Fyers' actual manual-exit fill exactly but it's close
            # enough for P&L bookkeeping — a wrong closed-position record
            # is still much better than an orphaned open that triggers a
            # reverse trade later.
            try:
                ltp_map = get_live_ltp_batch([symbol])
                exit_price = float(ltp_map.get(symbol) or position.get("entry_price") or 0)
            except Exception:
                exit_price = float(position.get("entry_price") or 0)

            try:
                super().close_trade(
                    position,
                    exit_price,
                    exit_reason="MANUAL_EXTERNAL_EXIT",
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

    def reconcile_open_positions(self) -> dict:
        """Poll Fyers orderbook and detect SL/Target fills. For each fill:
        close our position record + cancel the sibling protective order so
        it doesn't accidentally reverse the position later. Meant to be
        called every ~30 seconds by the engine background loop.

        Returns a summary dict: {'reconciled': N, 'errors': M}.
        """
        summary = {"reconciled": 0, "errors": 0, "already_closed": 0, "externally_closed": 0}
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
        if self.algo_id == "algo3":
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
        parsed = self._parse_fill_time(row, allow_order_time=allow_order_time)
        if parsed:
            return parsed.astimezone(datetime.timezone.utc).isoformat()
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

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
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.datetime.strptime(text, fmt).replace(tzinfo=IST).astimezone(datetime.timezone.utc)
            except ValueError:
                continue
        return None

    def _safe_entry_time(self, candidate: str | None, fallback: str | None) -> str:
        """Use a matched broker fill time, otherwise retain the strategy event."""
        return candidate or fallback or datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _safe_exit_time(self, position: dict, candidate: str | None) -> str:
        """Never persist an exit before the recorded entry.

        A broker orderbook can expose its creation time instead of execution
        time. If that value is stale, using the reconciliation timestamp is
        truthful enough for the audit row and avoids impossible intervals.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        exit_at = self._coerce_datetime(candidate) if candidate else now
        entry_at = self._coerce_datetime(position.get("entry_time"))
        if entry_at and exit_at <= entry_at:
            print(
                "[live_broker] rejected non-monotonic Fyers exit timestamp "
                f"symbol={position.get('symbol')} entry={entry_at.isoformat()} "
                f"candidate_exit={exit_at.isoformat()}"
            )
            exit_at = now
        return exit_at.astimezone(datetime.timezone.utc).isoformat()
