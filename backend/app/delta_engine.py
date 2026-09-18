"""Independent 24/7 Delta paper engine; never registered in FYERS STRATEGIES."""
from __future__ import annotations

import copy
import datetime
import json
import threading
import time
import traceback

from .delta_client import DeltaClient, epoch_seconds, positive
from .delta_candles import aggregate_custom_minutes, delta_resolution
from .delta_config import delta_capabilities, asset_capabilities, timeframe_enabled
from .delta_live import DeltaLiveBroker
from .delta_log import delta_log
from .delta_paper import DeltaPaperBroker, DeltaStore
from .delta_reporting import paper_row, inr_rate
from .strategies.delta_gold import DELTA_DEFAULTS, DELTA_TIMEFRAMES, DeltaGold, validate_settings, defaults_for
from .strategies.delta_silver import DeltaSilver


DELTA_LIVE_MAX_TRACKED_TRADES = 1


class DeltaService:
    def __init__(self, asset='gold', mode='paper'):
        if mode not in {'paper', 'live'}:
            raise ValueError("Delta mode must be paper or live")
        self.asset = asset
        self.mode = mode
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.client = None
        self.strategies = {}
        self.product = None
        self.error = f"Delta {mode} engine is starting"
        self.ws_error = None
        self.ws_connected = False
        self.last_price = None
        self.last_event_at = 0.0
        self.last_source = None
        self.socket = None
        self.started = False
        self.initialized = False
        self._overview_trade_cache = {}
        self._live_flat_confirmations = {}

    def start(self):
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._run, name=f"delta-{self.mode}", daemon=True).start()

    def stop(self):
        self.stop_event.set()
        if self.socket:
            self.socket.close()

    def _initialize(self):
        capabilities = asset_capabilities(self.asset)
        if capabilities["config_error"]:
            self.error = capabilities["config_error"]
            self.initialized = True
            return
        if not capabilities["delta_enabled"]:
            self.error = "Delta is disabled by DELTA_HIDDEN_SECTIONS"
            self.initialized = True
            return
        credential_scope = "live" if self.mode == "live" else "read"
        self.client = (
            DeltaClient(credential_scope=credential_scope)
            if self.asset == 'gold' else
            DeltaClient(asset=self.asset, credential_scope=credential_scope)
        )
        error = self.client.configuration_error()
        if error:
            raise ValueError(error)
        if self.mode == "live" and not self.client.live_enabled:
            self.error = "Delta live is disabled. Set DELTA_LIVE_ENABLED=true only after DELTA_LIVE_API_KEY has Trading permission."
            self.initialized = True
            return
        if self.mode == "live" and self.asset != "gold":
            self.error = "Delta live is enabled only for Gold right now."
            self.initialized = True
            return
        product = self.client.product()
        delta_log(
            "service_initialized",
            mode=self.mode,
            asset=self.asset,
            symbol=self.client.symbol,
            product_id=product.get("id"),
            enabled_timeframes=capabilities["enabled_timeframes"],
            live_enabled=getattr(self.client, "live_enabled", False),
        )
        strategies = {}
        for minutes in capabilities["enabled_timeframes"]:
            key = f"delta:{self.client.region}:{self.client.symbol}:{minutes}:{self.mode}"
            if self.asset == 'silver':
                key = f"delta:silver:{self.client.region}:{self.client.symbol}:{minutes}:{self.mode}"
            store = DeltaStore(key)
            broker = (
                DeltaLiveBroker(store, dict(defaults_for(self.asset)), product, self.client)
                if self.mode == "live" else
                DeltaPaperBroker(store, dict(defaults_for(self.asset)), product)
            )
            if self.mode == "live":
                broker.entry_guard = lambda side, minutes=minutes: self._validate_live_entry(minutes, side)
            strategy_class = DeltaSilver if self.asset == 'silver' else DeltaGold
            strategies[minutes] = strategy_class(minutes, self.client.symbol, broker)
        with self.lock:
            self.strategies = strategies
            self.product = product
            self.error = None
            self.initialized = True
        threading.Thread(target=self._websocket, name=f"delta-{self.mode}-public-ws", daemon=True).start()

    def _run(self):
        while not self.stop_event.is_set() and not self.initialized:
            try:
                self._initialize()
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta storage unavailable; check Supabase migration and backend logs"
                print(f"[delta-{self.mode}] startup: {self.error}")
                self.stop_event.wait(30)
        if not self.client:
            return
        next_history = {minutes: 0.0 for minutes in self.strategies}
        next_rest = 0.0
        next_live_reconcile = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            for minutes, strategy in self.strategies.items():
                if now < next_history[minutes]:
                    continue
                next_history[minutes] = now + 10
                interval = minutes * 60
                bucket = int(now // interval) * interval
                # Include the forming candle to establish its actual open.
                # Custom intervals are assembled from 1m rows.
                custom_minutes = minutes in {7, 120}
                lookback = 250 if minutes == 7 else 40 if minutes == 120 else 300
                start = strategy.last_candle_epoch or bucket - lookback * interval
                try:
                    resolution = delta_resolution(minutes)
                    rows = self.client.candles(resolution, start, int(now))
                    if custom_minutes:
                        rows = aggregate_custom_minutes(rows, minutes, current_time=now)
                    with self.lock:
                        strategy.ingest_history(rows, now)
                        delta_log(
                            "history_refresh_ok",
                            mode=self.mode,
                            asset=self.asset,
                            minutes=minutes,
                            resolution=resolution,
                            rows=len(rows),
                            start=start,
                            end=int(now),
                            last_candle_epoch=strategy.last_candle_epoch,
                            current_candle_open=strategy._current_candle_open,
                            data_error=strategy.data_error,
                        )
                        next_history[minutes] = min(bucket + interval + 1, now + 60)
                except Exception as exc:
                    with self.lock:
                        strategy.data_error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta candle refresh failed"
                        delta_log(
                            "history_refresh_failed",
                            mode=self.mode,
                            asset=self.asset,
                            minutes=minutes,
                            start=start,
                            end=int(now),
                            error=strategy.data_error,
                        )
            if now >= next_rest and now - self.last_event_at > 5:
                next_rest = now + 2
                try:
                    price, stamp = self.client.recent_trade()
                    delta_log("rest_trade", mode=self.mode, asset=self.asset, price=price, stamp=stamp)
                    self.accept_price(price, stamp, "REST")
                except Exception as exc:
                    self.error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta REST data unavailable"
                    delta_log("rest_trade_failed", mode=self.mode, asset=self.asset, error=self.error)
            if self.mode == "live" and now >= next_live_reconcile:
                next_live_reconcile = now + 5
                try:
                    self._reconcile_live_positions()
                except Exception as exc:
                    self.error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta live reconciliation unavailable"
                    delta_log("live_reconcile_failed", mode=self.mode, asset=self.asset, error=self.error)
            self.stop_event.wait(0.5)

    def accept_price(self, price, stamp, source):
        price = positive(price)
        stamp = epoch_seconds(stamp)
        age = time.time() - stamp
        if not -2 <= age <= 15:
            delta_log("price_ignored_stale", mode=self.mode, asset=self.asset, source=source, price=price, stamp=stamp, age_seconds=age)
            return  # Old last-trade prices must not masquerade as fresh ticks.
        with self.lock:
            if stamp <= self.last_event_at:
                delta_log("price_ignored_duplicate", mode=self.mode, asset=self.asset, source=source, price=price, stamp=stamp, last_event_at=self.last_event_at)
                return
            self.last_price, self.last_event_at, self.last_source = price, stamp, source
            self.error = None
            delta_log("price_accepted", mode=self.mode, asset=self.asset, source=source, price=price, stamp=stamp, strategies=list(self.strategies))
            for strategy in self.strategies.values():
                try:
                    strategy.process_price(price, stamp)
                except Exception as exc:
                    self.error = self._processing_error_message(exc)
                    print(
                        f"[delta-{self.mode}] processing failed "
                        f"asset={self.asset} minutes={getattr(strategy, 'minutes', '?')} "
                        f"symbol={getattr(strategy, 'symbol', '?')} error={exc!r}"
                    )
                    delta_log(
                        "strategy_processing_failed",
                        mode=self.mode,
                        asset=self.asset,
                        minutes=getattr(strategy, "minutes", None),
                        symbol=getattr(strategy, "symbol", None),
                        price=price,
                        stamp=stamp,
                        error=repr(exc),
                    )
                    traceback.print_exc()
                    # Do not continue submitting entries after a failed protection write.
                    strategy.data_error = self.error
                    break

    def _processing_error_message(self, exc):
        raw = str(exc).strip()
        detail = raw if raw else exc.__class__.__name__
        lowered = detail.lower()
        if any(term in lowered for term in ("supabase", "postgrest", "save_delta_paper", "delta_paper_state", "delta_paper_trades", "database")):
            return f"Delta {self.mode} state save failed: {detail}"
        if self.mode == "live":
            return f"Delta live order/protection update failed: {detail}"
        return f"Delta {self.mode} processing failed: {detail}"

    def _validate_live_entry(self, minutes, side):
        if self.mode != "live":
            return
        current = self.strategies.get(minutes)
        if current and current.broker.state.get("position"):
            delta_log("live_entry_blocked_same_timeframe", minutes=minutes, side=side)
            raise ValueError(f"Delta live {minutes}m already has an open tracked trade")
        open_items = [
            (tf, strategy.broker.state.get("position"))
            for tf, strategy in self.strategies.items()
            if strategy.broker.state.get("position")
        ]
        if open_items:
            delta_log("live_entry_blocked_existing_trade", minutes=minutes, side=side, open_items=[(tf, p.get("side")) for tf, p in open_items])
            raise ValueError(
                "Delta live entry blocked: one active live timeframe trade is already open. "
                "Close it before allowing another Delta live trade through."
            )
        opposite = [
            (tf, position)
            for tf, position in open_items
            if position.get("side") != side
        ]
        if opposite:
            frames = ", ".join(f"{tf}m {position.get('side')}" for tf, position in opposite)
            delta_log("live_entry_blocked_opposite_side", minutes=minutes, side=side, opposite=frames)
            raise ValueError(
                "Delta live entry blocked: opposite-side live trade already exists "
                f"({frames}). Delta nets PAXGUSD positions, so this would reduce or reverse it."
            )

    def _reconcile_live_positions(self):
        if self.mode != "live" or not self.client or not self.product:
            return
        tracked = {minutes: strategy for minutes, strategy in self.strategies.items() if strategy.broker.state.get("position")}
        if not tracked:
            self._live_flat_confirmations.clear()
            delta_log("live_reconcile_no_tracked_positions", asset=self.asset)
            return
        payload = self.client.get("/v2/positions/margined", private=True, envelope=True)
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise ValueError("Unexpected Delta positions response")
        product_id = str(self.product.get("id"))
        live_size = 0.0
        for row in rows:
            row_product = row.get("product") or {}
            row_symbol = row.get("product_symbol") or row_product.get("symbol")
            if str(row.get("product_id")) == product_id or row_symbol == self.client.symbol:
                try:
                    live_size += float(row.get("size") or 0)
                except (TypeError, ValueError):
                    continue
        with self.lock:
            delta_log(
                "live_reconcile_snapshot",
                asset=self.asset,
                tracked_minutes=list(tracked),
                live_size=live_size,
                last_price=self.last_price,
            )
            if abs(live_size) > 0:
                self._live_flat_confirmations.clear()
                active_orders = next(iter(tracked.values())).broker._active_product_orders()
                for strategy in tracked.values():
                    strategy.broker.sync_protection(active_orders)
                return
            for minutes, strategy in tracked.items():
                position = strategy.broker.state.get("position")
                if not position:
                    self._live_flat_confirmations.pop(minutes, None)
                    continue
                confirmations = self._live_flat_confirmations.get(minutes, 0) + 1
                self._live_flat_confirmations[minutes] = confirmations
                delta_log(
                    "live_flat_confirmation",
                    minutes=minutes,
                    confirmations=confirmations,
                    position_id=position.get("id"),
                    side=position.get("side"),
                    exit_price=self.last_price or position.get("entry_price"),
                )
                if confirmations < 2:
                    continue
                exit_price = self.last_price or position.get("entry_price")
                # Pass exit_reason=None so record_external_close can infer
                # SL / TRAILING_SL / TARGET (and their _EDITED variants) from
                # the most recent live_close_error, instead of always writing
                # MANUAL_EXTERNAL_EXIT when Delta's own stop/target actually
                # fired natively and just beat our reduce_only close.
                if strategy.broker.record_external_close(exit_price):
                    self._live_flat_confirmations.pop(minutes, None)

    def _websocket(self):
        import websocket
        delay = 2
        while not self.stop_event.is_set():
            def opened(ws):
                self.ws_error = None
                ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [
                    {"name": "trades", "symbols": [self.client.symbol]},
                ]}}))

            def message(ws, raw):
                try:
                    data = json.loads(raw)
                    if data.get("type") == "subscriptions":
                        channels = data.get("channels", [])
                        self.ws_connected = any(c.get("name") == "trades" and self.client.symbol in c.get("symbols", []) and not c.get("error") for c in channels)
                        if not self.ws_connected:
                            self.ws_error = "Delta rejected the trades subscription; REST fallback active"
                            ws.close()
                        return
                    if data.get("type") != "trades":
                        return
                    symbol = data.get("sy") or data.get("symbol")
                    if symbol != self.client.symbol:
                        return
                    self.accept_price(data.get("p", data.get("price")), data.get("t", data.get("timestamp")), "WS")
                except (ValueError, TypeError, KeyError):
                    self.ws_error = "Invalid Delta trade event ignored"

            def failed(ws, error):
                self.ws_error = "Delta WebSocket disconnected; REST fallback active"

            self.socket = websocket.WebSocketApp(self.client.ws_url, on_open=opened, on_message=message, on_error=failed)
            try:
                self.socket.run_forever(ping_interval=20, ping_timeout=10, **self.client.websocket_options())
            except Exception:
                self.ws_error = "Delta WebSocket connection failed; REST fallback active"
            self.ws_connected = False
            if time.time() - self.last_event_at < 15:
                delay = 2
            self.stop_event.wait(delay)
            delay = min(delay * 2, 60)

    def strategy(self, minutes):
        if minutes not in DELTA_TIMEFRAMES:
            raise ValueError("Delta timeframe must be 5, 7, 15, 30, 60, 120 or 240 minutes")
        if not timeframe_enabled(minutes, asset=self.asset):
            raise ValueError("That Delta timeframe is disabled in this deployment")
        if minutes not in self.strategies:
            raise ValueError(self.error or "Delta is not ready")
        return self.strategies[minutes]

    def snapshot(self, minutes):
        with self.lock:
            result = {
                "asset": self.asset, "mode": self.mode, "exchange": self.client.region if self.client else None,
                "symbol": self.client.symbol if self.client else None, "minutes": minutes,
                "error": self.error, "ws_connected": self.ws_connected, "ws_error": self.ws_error,
                "ltp": self.last_price, "last_tick_at": self.last_event_at or None,
                "source": self.last_source, "stale": time.time() - self.last_event_at > 15,
                "proxy_configured": bool(self.client and self.client.proxy),
                "credentials_configured": bool(self.client and self.client.key and self.client.secret),
            }
            strategy = self.strategies.get(minutes)
            if not strategy:
                return result
            state = copy.deepcopy(strategy.broker.state)
            position = state.get("position")
            if position and self.last_price:
                position["unrealized_pnl"] = strategy.broker.pnl(position, self.last_price)
            today = datetime.datetime.now(datetime.timezone.utc).date()
            today_rows = []
            for row in self._all_closed_trades(minutes, strategy):
                try:
                    closed = datetime.datetime.fromisoformat(str(row.get("exit_time", "")).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                closed_utc = closed if closed.tzinfo else closed.replace(tzinfo=datetime.timezone.utc)
                if closed_utc.astimezone(datetime.timezone.utc).date() == today:
                    today_rows.append(row)
            today_stats = self._trade_stats(today_rows)
            result.update(
                settings=strategy.settings, position=paper_row(position, self.client.region) if position else None,
                summary={k: state[k] for k in ("gross_pnl", "fees", "closed_count", "buy_count", "sell_count")},
                today_summary={
                    "gross_pnl": today_stats["gross"],
                    "fees": today_stats["fees"],
                    "closed_count": today_stats["trades"],
                    "buy_count": sum(1 for row in today_rows if row.get("side") == "BUY"),
                    "sell_count": sum(1 for row in today_rows if row.get("side") == "SELL"),
                },
                history_error=strategy.data_error, ema20=strategy._ema20,
                volume_ema20=strategy._volume_ema20,
                current_candle_open=strategy._current_candle_open,
                last_bar_at=strategy.last_candle_epoch,
                buy_reference=strategy._buy_setup_close, sell_reference=strategy._sell_setup_close,
                references=list(strategy.reference_history),
                cooldown=strategy.cooldown_status(),
                quote_currency=self.product.get("quoting_asset", {}).get("symbol"),
                settlement_currency=self.product.get("settling_asset", {}).get("symbol"),
                contract_value=float(self.product["contract_value"]),
                contract_unit=self.product.get("contract_unit_currency"),
                inr_rate=inr_rate(self.client.region, self.product.get("quoting_asset", {}).get("symbol")),
            )
            return copy.deepcopy(result)

    @staticmethod
    def _trade_stats(rows):
        wins = sum(1 for row in rows if float(row.get("net_pnl") or 0) > 0)
        losses = sum(1 for row in rows if float(row.get("net_pnl") or 0) < 0)
        gross = sum(float(row.get("gross_pnl") or 0) for row in rows)
        fees = sum(float(row.get("fees") or 0) for row in rows)
        return {
            "trades": len(rows), "wins": wins, "losses": losses,
            "breakeven": len(rows) - wins - losses,
            "win_rate": (wins / len(rows) * 100) if rows else 0.0,
            "gross": gross, "fees": fees, "net": gross - fees,
        }

    def _all_closed_trades(self, minutes, strategy):
        closed_count = int(strategy.broker.state.get("closed_count") or 0)
        cached = self._overview_trade_cache.get(minutes)
        if cached and cached[0] == closed_count:
            return cached[1]
        rows = []
        offset = 0
        while True:
            page = strategy.broker.store.trades(offset, 1000)
            rows.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        self._overview_trade_cache[minutes] = (closed_count, rows)
        return rows

    def overview(self):
        with self.lock:
            capabilities = asset_capabilities(self.asset)
            if not capabilities["delta_enabled"] or not capabilities["sections"]["overview"]:
                raise ValueError("Delta overview is disabled in this deployment")
            today = datetime.datetime.now(datetime.timezone.utc).date()
            rate = inr_rate(self.client.region, self.product.get("quoting_asset", {}).get("symbol")) if self.client and self.product else None
            entries = []
            for minutes in capabilities["enabled_timeframes"]:
                strategy = self.strategy(minutes)
                state = copy.deepcopy(strategy.broker.state)
                rows = self._all_closed_trades(minutes, strategy)
                today_rows = []
                for row in rows:
                    try:
                        closed = datetime.datetime.fromisoformat(str(row.get("exit_time", "")).replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        continue
                    if (closed if closed.tzinfo else closed.replace(tzinfo=datetime.timezone.utc)).astimezone(datetime.timezone.utc).date() == today:
                        today_rows.append(row)
                all_time = self._trade_stats(rows)
                # Persistent totals remain authoritative for historical financial values.
                all_time.update(gross=float(state.get("gross_pnl") or 0), fees=float(state.get("fees") or 0))
                all_time["net"] = all_time["gross"] - all_time["fees"]
                position = state.get("position")
                unrealized = strategy.broker.pnl(position, self.last_price) if position and self.last_price is not None else 0.0
                entries.append({
                    "minutes": minutes, "label": strategy.display_name,
                    "scan_enabled": strategy.settings["scan_enabled"],
                    "trading_enabled": strategy.settings["trading_enabled"],
                    "stale": time.time() - self.last_event_at > 15,
                    "last_bar_at": strategy.last_candle_epoch,
                    "ema20": strategy._ema20, "volume_ema20": strategy._volume_ema20,
                    "position": paper_row({**position, "unrealized_pnl": unrealized}, self.client.region) if position else None,
                    "cooldown": strategy.cooldown_status(),
                    "unrealized": unrealized, "today": self._trade_stats(today_rows), "all_time": all_time,
                    "combined_today": self._trade_stats(today_rows)["net"] + unrealized,
                    "combined_all_time": all_time["net"] + unrealized,
                })
            def total(period):
                result = {key: sum(float(row[period][key]) for row in entries) for key in ("trades", "wins", "losses", "breakeven", "gross", "fees", "net")}
                result["win_rate"] = result["wins"] / result["trades"] * 100 if result["trades"] else 0.0
                return result
            unrealized_total = sum(row["unrealized"] for row in entries)
            today_total, all_total = total("today"), total("all_time")
            return copy.deepcopy({
                "asset": self.asset, "symbol": self.product.get('symbol') if self.product else None,
                "generated_at": time.time(), "utc_date": today.isoformat(), "ltp": self.last_price,
                "currency": self.product.get("quoting_asset", {}).get("symbol") if self.product else None,
                "inr_rate": rate, "timeframes": entries,
                "totals": {"today": today_total, "all_time": all_total, "unrealized": unrealized_total,
                           "today_with_unrealized": today_total["net"] + unrealized_total,
                           "all_time_with_unrealized": all_total["net"] + unrealized_total},
            })

    def save_settings(self, minutes, changes):
        with self.lock:
            strategy = self.strategy(minutes)
            previous_settings = copy.deepcopy(strategy.settings)
            settings = validate_settings({**strategy.settings, **changes}, self.asset)
            state = copy.deepcopy(strategy.broker.state)
            state["settings"] = settings
            strategy.broker.commit(state)
            strategy.settings = settings
            delta_log(
                "settings_saved", mode=self.mode, asset=self.asset, minutes=minutes,
                changes={key: {"before": previous_settings.get(key), "after": value}
                         for key, value in settings.items() if previous_settings.get(key) != value},
                settings=settings, cooldown=strategy.cooldown_status(),
                position_id=(state.get("position") or {}).get("id"),
                ltp=self.last_price, last_tick_at=self.last_event_at,
            )
            if (
                self.last_price is not None
                and self.last_event_at
                and time.time() - self.last_event_at <= 15
            ):
                strategy.recheck_triggers_after_settings_change(
                    self.last_price,
                    datetime.datetime.fromtimestamp(self.last_event_at, datetime.timezone.utc),
                )
            return settings

    def close(self, minutes, position_id):
        with self.lock:
            strategy = self.strategy(minutes)
            if time.time() - self.last_event_at > 15 or self.last_price is None:
                raise ValueError("Fresh Delta price unavailable; cannot simulate a current exit")
            position = strategy._open_position()
            if not position or position["id"] != position_id:
                raise ValueError("That position has already closed or changed")
            strategy.broker.close_trade(position, self.last_price, "MANUAL_EXIT")

    def resume(self, minutes):
        with self.lock:
            strategy = self.strategy(minutes)
            state = copy.deepcopy(strategy.broker.state)
            state["cooldown_until"] = 0.0
            state["cooldown_reason"] = None
            strategy.broker.commit(state)
            strategy._sl_cooldown_until_monotonic = 0.0
            delta_log("entries_resumed", mode=self.mode, asset=self.asset, minutes=minutes,
                      settings=strategy.settings, cooldown=strategy.cooldown_status())
            return strategy.cooldown_status()

    def pause(self, minutes, duration_minutes):
        with self.lock:
            strategy = self.strategy(minutes)
            duration = float(duration_minutes)
            if not duration.is_integer() or not 0 <= duration <= 10_080:
                raise ValueError("Rest duration must be a whole number from 0 to 10080 minutes")
            if int(duration) == 0:
                return self.resume(minutes)
            state = copy.deepcopy(strategy.broker.state)
            state["cooldown_until"] = time.time() + int(duration) * 60
            state["cooldown_reason"] = "MANUAL_PAUSE"
            strategy.broker.commit(state)
            delta_log("entries_paused", mode=self.mode, asset=self.asset, minutes=minutes,
                      duration_minutes=int(duration), settings=strategy.settings,
                      cooldown=strategy.cooldown_status())
            return strategy.cooldown_status()

    def edit_protection(self, minutes, position_id, sl_price, target_price, expected_sl, expected_target, new_exit_mode=None):
        with self.lock:
            strategy = self.strategy(minutes)
            if self.last_price is None or not -2 <= time.time() - self.last_event_at <= 15:
                raise ValueError('Fresh Delta price unavailable; reload before editing protection')
            if self.mode == "live":
                strategy.broker.sync_protection()
            current = strategy._open_position()
            if not current or current['id'] != position_id:
                raise ValueError('That position has closed or changed; reload before editing')
            if current['sl_price'] != expected_sl or current['target_price'] != expected_target:
                raise ValueError('Protection changed while editing; reopen the editor')
            sl, target = positive(sl_price), positive(target_price)
            current_mode = (current.get('signal_snapshot') or {}).get('silver_exit_policy')
            mode_changing = new_exit_mode is not None and new_exit_mode != current_mode
            if sl == current['sl_price'] and target == current['target_price'] and not mode_changing:
                raise ValueError('Change at least one protection level or the exit mode')
            buy = current['side'] == 'BUY'
            if not (sl < self.last_price < target if buy else target < self.last_price < sl):
                raise ValueError('BUY requires SL < current price < target; SELL requires target < current price < SL')
            if not (target > current['entry_price'] if buy else target < current['entry_price']):
                raise ValueError('Target must remain on the profitable side of entry')
            protection = current['signal_snapshot'].get('silver_breakeven')
            if protection and protection.get('armed') and (sl < current['entry_price'] if buy else sl > current['entry_price']):
                raise ValueError('An armed breakeven stop cannot be moved back into loss')
            if protection and not protection.get('armed') and (target <= protection['activation_price'] if buy else target >= protection['activation_price']):
                raise ValueError('Final target must remain beyond the pending TSL activation level')
            # Build the exit-mode patch on a hypothetical post-edit position
            # (so the fresh silver_breakeven / delta_three_candle_tsl uses the
            # NEW sl/target the user is saving, not the pre-edit values).
            exit_mode_patch = None
            if mode_changing:
                from .strategies.delta_gold import exit_mode_snapshot_patch
                projected = dict(current)
                projected['sl_price'] = sl
                projected['target_price'] = target
                exit_mode_patch = exit_mode_snapshot_patch(
                    new_exit_mode, projected, strategy.settings, minutes, self.asset,
                    datetime.datetime.now(datetime.timezone.utc),
                )
            if hasattr(strategy.broker, "update_protection"):
                return strategy.broker.update_protection(current, sl, target, self.last_price, exit_mode_patch=exit_mode_patch)
            state = copy.deepcopy(strategy.broker.state)
            position = state['position']
            sl_changed = sl != current['sl_price']
            target_changed = target != current['target_price']
            edit_event = {
                'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'source': 'manual_paper',
                'previous_sl': current['sl_price'], 'previous_target': current['target_price'],
                'new_sl': sl, 'new_target': target, 'ltp': self.last_price,
            }
            if exit_mode_patch:
                edit_event['exit_mode_from'] = current_mode
                edit_event['exit_mode_to'] = new_exit_mode
            position.setdefault('protection_edits', []).append(edit_event)
            position.update(sl_price=sl, target_price=target)
            # sl_source / target_source drive the SL_EDITED / TARGET_EDITED
            # exit_reason so audit rows distinguish an original protective
            # exit from one against a manually-moved level.
            if sl_changed:
                position['sl_source'] = 'manual'
            if target_changed:
                position['target_source'] = 'manual'
            if exit_mode_patch:
                from .strategies.delta_gold import apply_exit_mode_patch
                apply_exit_mode_patch(position, exit_mode_patch)
            elif protection:
                position['signal_snapshot']['silver_breakeven']['target_price'] = target
            strategy.broker.commit(state)
            return copy.deepcopy(position)


delta_service = DeltaService()
delta_live_service = DeltaService(mode='live')
delta_silver_service = DeltaService('silver')


def service_for(asset='gold', mode='paper'):
    if asset == 'gold':
        return delta_live_service if mode == 'live' else delta_service
    if asset == 'silver':
        if mode == 'live':
            raise ValueError('Delta Silver live is not enabled')
        return delta_silver_service
    raise ValueError('Unknown Delta asset')
