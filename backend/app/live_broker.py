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


# NSE equity tick size. Fyers V3 rejects prices that aren't a multiple of the
# stock's tick with `code -50: LimitPrice not a multiple of tick size 0.0500`.
# Nearly every NSE equity trades on 0.05 tick, so we round every stop/limit
# price we send to Fyers to the nearest 0.05. A handful of low-price ETFs use
# 0.01 — rounding those to 0.05 is coarser than optimal but never invalid.
_TICK_SIZE = 0.05

# When a stop-loss order gets triggered, market may already have gapped past
# the stop. Fyers V3 requires limitPrice > 0 for SL-M orders (`code -50:
# limitPrice: Must be greater than or equal to 0.0025`), so we send it as
# SL-Limit with the limit sitting 0.5% beyond the stop in the fill direction
# (below the stop for a SELL exit, above the stop for a BUY exit). That gives
# the order enough slack to fill through normal gap-through moves while still
# not accepting catastrophic slippage.
_SL_LIMIT_SLIPPAGE_PCT = 0.5


def _round_to_tick(price: float, tick: float = _TICK_SIZE) -> float:
    """Round `price` to the nearest multiple of `tick`. Never returns 0 for a
    positive input — Fyers rejects both non-tick multiples and zero prices."""
    if price is None or price <= 0:
        return 0.0
    return round(round(float(price) / tick) * tick, 2)


def _sl_limit_price(stop_price: float, exit_side: str) -> float:
    """Compute the LIMIT companion price for an SL-Limit order.

    exit_side is the side of the PROTECTIVE order:
      - 'SELL' exits a long position -> limit sits BELOW stop so a
        downward gap still fills.
      - 'BUY' exits a short position -> limit sits ABOVE stop so an
        upward gap still fills.
    """
    if stop_price is None or stop_price <= 0:
        return 0.0
    factor = 1.0 - (_SL_LIMIT_SLIPPAGE_PCT / 100.0) if exit_side.upper() == "SELL" else 1.0 + (_SL_LIMIT_SLIPPAGE_PCT / 100.0)
    return _round_to_tick(float(stop_price) * factor)


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
        if side == "BUY":
            sl_price = actual_entry_price * (sl_price / entry_price) if entry_price else sl_price
            target_price = actual_entry_price * (target_price / entry_price) if entry_price else target_price
        else:
            sl_price = actual_entry_price * (sl_price / entry_price) if entry_price else sl_price
            target_price = actual_entry_price * (target_price / entry_price) if entry_price else target_price

        # ── HARD SL + TARGET AT FYERS ──────────────────────────────────
        # Place protective orders IMMEDIATELY after entry fill so Fyers
        # holds the SL/Target server-side. If our app crashes at any
        # point, Fyers still auto-exits when either level is touched.
        # Both orders are on the reverse side and MIS/INTRADAY so they're
        # linked to the same intraday position.
        entry_order_id = self._extract_order_id(order_response)
        protective = self._place_protective_orders(
            symbol=symbol,
            entry_side=side,
            qty=qty,
            sl_price=sl_price,
            target_price=target_price,
        )
        # Persist Fyers order IDs in signal_snapshot so the reconciliation
        # thread can look them up and detect SL/Target fills without any
        # in-memory dependency (survives container restarts).
        merged_snapshot = dict(signal_snapshot or {})
        merged_snapshot["fyers_entry_order_id"] = entry_order_id
        merged_snapshot["fyers_sl_order_id"] = protective.get("sl_order_id")
        merged_snapshot["fyers_target_order_id"] = protective.get("target_order_id")
        merged_snapshot["fyers_sl_error"] = protective.get("sl_error")
        merged_snapshot["fyers_target_error"] = protective.get("target_error")

        super().open_trade(
            symbol,
            side,
            qty,
            actual_entry_price,
            sl_price,
            target_price,
            entry_trigger,
            merged_snapshot,
            entry_time=actual_entry_time,
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
        super().close_trade(position, actual_exit_price, exit_reason, exit_time=actual_exit_time)

    # ── Protective order helpers ──────────────────────────────────────
    def _place_protective_orders(
        self,
        symbol: str,
        entry_side: str,
        qty: int,
        sl_price: float,
        target_price: float,
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
        # docstring for why we send a 0.5%-slack limit rather than 0.
        rounded_stop = _round_to_tick(stop_price)
        limit_price = _sl_limit_price(rounded_stop, side)
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
        rounded_limit = _round_to_tick(limit_price)
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
            modify_response = self._modify_slm_order(sl_order_id, new_sl, exit_side)
            if self._looks_successful(modify_response):
                print(
                    f"[live_broker] trailed hard SL {position.get('symbol')} "
                    f"order {sl_order_id} -> {new_sl:.2f}"
                )
            else:
                print(
                    f"[live_broker] trailed SL modify REJECTED for "
                    f"{position.get('symbol')} order {sl_order_id}: {modify_response}"
                )
        except Exception as exc:
            print(
                f"[live_broker] trailed SL modify failed for "
                f"{position.get('symbol')}: {exc}"
            )
        return updated

    def _modify_slm_order(self, order_id: str, new_stop_price: float, exit_side: str) -> dict:
        """Update the stopPrice of an existing SLM order at Fyers so a
        trailing-SL adjustment is enforced server-side, not just in our db.

        Must also pass exit_side so the paired limitPrice is recomputed —
        Fyers rejects the modify with the same code -50 as fresh placement
        if limitPrice is 0 or not on tick, so we round + slippage-buffer here
        exactly like _place_slm_order does."""
        fyers = get_fyers_model(use_proxy=True)
        rounded_stop = _round_to_tick(new_stop_price)
        limit_price = _sl_limit_price(rounded_stop, exit_side)
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
            try:
                super().close_trade(
                    position,
                    fill_price,
                    exit_reason=f"{filled_reason}_FYERS",
                    exit_time=self._extract_fill_time(filled_order),
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
            "limitPrice": _round_to_tick(limit_price) if is_limit else 0,
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
        """Read order_type setting for this algo. Defaults to LIMIT at LTP."""
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
    ) -> tuple[float, str]:
        fyers = get_fyers_model("live")
        order_id = self._extract_order_id(order_response)
        deadline = time.time() + 8
        latest_match = None
        while time.time() < deadline:
            latest_match = self._find_latest_fill(fyers, symbol, side, qty, order_id)
            if latest_match:
                break
            time.sleep(0.5)
        price = self._extract_fill_price(latest_match, fallback_price)
        executed_at = self._extract_fill_time(latest_match)
        return price, executed_at

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
                if order_id and row_order_id and row_order_id != order_id:
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
        for key in ("id", "order_id", "orderId", "fyOrderId", "exchangeOrderId"):
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

    def _extract_fill_time(self, row: dict | None) -> str:
        parsed = self._parse_fill_time(row)
        if parsed:
            return parsed.isoformat()
        return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

    def _parse_fill_time(self, row: dict | None) -> datetime.datetime | None:
        if not isinstance(row, dict):
            return None
        for key in (
            "tradeTime",
            "tradeDateTime",
            "tradedAt",
            "tradedOn",
            "updatedAt",
            "updated_at",
            "orderDateTime",
            "createdAt",
            "timestamp",
            "time",
        ):
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
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
        except ValueError:
            pass
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
        return None
