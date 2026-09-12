"""Independent 24/7 Delta paper engine; never registered in FYERS STRATEGIES."""
from __future__ import annotations

import copy
import datetime
import json
import threading
import time

from .delta_client import DeltaClient, epoch_seconds, positive
from .delta_candles import aggregate_seven_minute, delta_resolution
from .delta_config import delta_capabilities, timeframe_enabled
from .delta_paper import DeltaPaperBroker, DeltaStore
from .delta_reporting import paper_row, inr_rate
from .strategies.delta_gold import DELTA_DEFAULTS, DELTA_TIMEFRAMES, DeltaGold, validate_settings


class DeltaService:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.client = None
        self.strategies = {}
        self.product = None
        self.error = "Delta paper engine is starting"
        self.ws_error = None
        self.ws_connected = False
        self.last_price = None
        self.last_event_at = 0.0
        self.last_source = None
        self.socket = None
        self.started = False
        self.initialized = False
        self._overview_trade_cache = {}

    def start(self):
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._run, name="delta-paper", daemon=True).start()

    def stop(self):
        self.stop_event.set()
        if self.socket:
            self.socket.close()

    def _initialize(self):
        capabilities = delta_capabilities()
        if capabilities["config_error"]:
            self.error = capabilities["config_error"]
            self.initialized = True
            return
        if not capabilities["delta_enabled"]:
            self.error = "Delta is disabled by DELTA_HIDDEN_SECTIONS"
            self.initialized = True
            return
        self.client = DeltaClient()
        error = self.client.configuration_error()
        if error:
            raise ValueError(error)
        product = self.client.product()
        strategies = {}
        for minutes in capabilities["enabled_timeframes"]:
            key = f"delta:{self.client.region}:{self.client.symbol}:{minutes}:paper"
            broker = DeltaPaperBroker(DeltaStore(key), dict(DELTA_DEFAULTS), product)
            strategies[minutes] = DeltaGold(minutes, self.client.symbol, broker)
        with self.lock:
            self.strategies = strategies
            self.product = product
            self.error = None
            self.initialized = True
        threading.Thread(target=self._websocket, name="delta-public-ws", daemon=True).start()

    def _run(self):
        while not self.stop_event.is_set() and not self.initialized:
            try:
                self._initialize()
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta storage unavailable; check Supabase migration and backend logs"
                print(f"[delta-paper] startup: {self.error}")
                self.stop_event.wait(30)
        if not self.client:
            return
        next_history = {minutes: 0.0 for minutes in self.strategies}
        next_rest = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            for minutes, strategy in self.strategies.items():
                if now < next_history[minutes]:
                    continue
                next_history[minutes] = now + 10
                interval = minutes * 60
                bucket = int(now // interval) * interval
                # Include the forming candle to establish its actual open. Seven-minute
                # history is assembled from 1m rows and stays under Delta's 2000 row cap.
                lookback = 250 if minutes == 7 else 300
                start = strategy.last_candle_epoch or bucket - lookback * interval
                try:
                    resolution = delta_resolution(minutes)
                    rows = self.client.candles(resolution, start, int(now))
                    if minutes == 7:
                        rows = aggregate_seven_minute(rows, current_time=now)
                    with self.lock:
                        strategy.ingest_history(rows, now)
                        next_history[minutes] = min(bucket + interval + 1, now + 60)
                except Exception as exc:
                    with self.lock:
                        strategy.data_error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta candle refresh failed"
            if now >= next_rest and now - self.last_event_at > 5:
                next_rest = now + 2
                try:
                    price, stamp = self.client.recent_trade()
                    self.accept_price(price, stamp, "REST")
                except Exception as exc:
                    self.error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Delta REST data unavailable"
            self.stop_event.wait(0.5)

    def accept_price(self, price, stamp, source):
        price = positive(price)
        stamp = epoch_seconds(stamp)
        if not -2 <= time.time() - stamp <= 15:
            return  # Old last-trade prices must not masquerade as fresh ticks.
        with self.lock:
            if stamp <= self.last_event_at:
                return
            self.last_price, self.last_event_at, self.last_source = price, stamp, source
            self.error = None
            for strategy in self.strategies.values():
                try:
                    strategy.process_price(price, stamp)
                except Exception:
                    self.error = "Delta paper processing/persistence failed; check database availability"
                    # Do not continue submitting entries after a failed protection write.
                    strategy.data_error = self.error

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
            raise ValueError("Delta timeframe must be 5, 7, 15, 30, 60 or 240 minutes")
        if not timeframe_enabled(minutes):
            raise ValueError("That Delta timeframe is disabled in this deployment")
        if minutes not in self.strategies:
            raise ValueError(self.error or "Delta is not ready")
        return self.strategies[minutes]

    def snapshot(self, minutes):
        with self.lock:
            result = {
                "mode": "paper", "exchange": self.client.region if self.client else None,
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
            result.update(
                settings=strategy.settings, position=paper_row(position, self.client.region) if position else None,
                summary={k: state[k] for k in ("gross_pnl", "fees", "closed_count", "buy_count", "sell_count")},
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
            capabilities = delta_capabilities()
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
            settings = validate_settings({**strategy.settings, **changes})
            state = copy.deepcopy(strategy.broker.state)
            state["settings"] = settings
            strategy.broker.commit(state)
            strategy.settings = settings
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
            return strategy.cooldown_status()

    def edit_protection(self, minutes, position_id, sl_price, target_price, expected_sl, expected_target):
        with self.lock:
            strategy = self.strategy(minutes)
            if self.last_price is None or not -2 <= time.time() - self.last_event_at <= 15:
                raise ValueError('Fresh Delta price unavailable; reload before editing protection')
            current = strategy._open_position()
            if not current or current['id'] != position_id:
                raise ValueError('That position has closed or changed; reload before editing')
            if current['sl_price'] != expected_sl or current['target_price'] != expected_target:
                raise ValueError('Protection changed while editing; reopen the editor')
            sl, target = positive(sl_price), positive(target_price)
            if sl == current['sl_price'] and target == current['target_price']:
                raise ValueError('Change at least one protection level')
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
            state = copy.deepcopy(strategy.broker.state)
            position = state['position']
            position.setdefault('protection_edits', []).append({
                'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'source': 'manual_paper',
                'previous_sl': current['sl_price'], 'previous_target': current['target_price'],
                'new_sl': sl, 'new_target': target, 'ltp': self.last_price,
            })
            position.update(sl_price=sl, target_price=target)
            if protection:
                position['signal_snapshot']['silver_breakeven']['target_price'] = target
            strategy.broker.commit(state)
            return copy.deepcopy(position)


delta_service = DeltaService()
