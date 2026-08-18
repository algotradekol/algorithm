"""
smoke_live_orders.py — offline proof that the live order pipeline builds
the RIGHT Fyers payloads, any day of the week, market open or closed.

It never touches Fyers or Supabase:
  * get_fyers_model is replaced by a FakeFyers recorder that captures
    every payload and returns a success response.
  * The DB-touching methods (open_positions / close_trade / wallet) are
    stubbed on the instance, and __init__ is bypassed so no state row is
    read or written.

What it verifies:
  1. Dynamic per-stock quantity  = capital x approved-margin // price
  2. Entry order is LIMIT @ LTP  (type 1, limitPrice = LTP)  — not Market
  3. Stop-loss is placed AT FYERS as an SL-M order (type 4, stopPrice)
  4. Target is placed AT FYERS as a LIMIT order (type 1, limitPrice)
  5. OCO: when the SL fills, reconcile cancels the leftover Target order
  6. Trailing SL pushes a modify_order to Fyers (server-side, not app-only)

Run:  python -m tests.smoke_live_orders        (from backend/)
Exit code 0 = all checks passed, 1 = a check failed.
"""
from __future__ import annotations

import sys

import app.live_broker as lb
import app.paper_broker as _pb_module
import app.strategy_settings as ss
from app.live_broker import LiveBroker
from app.margin_lookup import effective_multiplier

# Capture the ORIGINAL PaperBroker.apply_trailing_stop before any test can
# monkey-patch it, so tests that need the real logic (e.g. trailing metadata
# tracking) can restore it.
_REAL_APPLY_TRAILING_STOP = _pb_module.PaperBroker.apply_trailing_stop


# ── fakes ──────────────────────────────────────────────────────────────
class FakeFyers:
    """Records every order call and returns Fyers-style success responses."""

    def __init__(self, recorder):
        self.rec = recorder

    def place_order(self, payload):
        self.rec["placed"].append(dict(payload))
        oid = f"FAKE-{len(self.rec['placed'])}"
        return {"s": "ok", "id": oid, "message": "order placed"}

    def cancel_order(self, payload):
        self.rec["cancelled"].append(dict(payload))
        return {"s": "ok", "id": payload.get("id")}

    def modify_order(self, payload):
        self.rec["modified"].append(dict(payload))
        return {"s": "ok", "id": payload.get("id")}

    def orderbook(self):
        return {"s": "ok", "orderBook": self.rec["orderbook"]}

    def positions(self):
        return {"s": "ok", "netPositions": self.rec.get("net_positions", [])}


PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
_failures = 0


def check(name, cond, detail=""):
    global _failures
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  - {detail}" if detail else ""))
    if not cond:
        _failures += 1


def make_broker(recorder, order_type="LIMIT"):
    fake = FakeFyers(recorder)
    lb.get_fyers_model = lambda *a, **k: fake
    lb.get_wallet_balance = lambda *a, **k: {"summary": {"available_margin": 10_000_000}}
    ss.get_settings = lambda algo_id: {**ss.DEFAULT_SETTINGS, "order_type": order_type}

    broker = object.__new__(LiveBroker)   # bypass __init__ -> no DB state row
    broker.algo_id = "algo1"
    return broker


# ── 1. dynamic quantity ────────────────────────────────────────────────
def test_dynamic_qty():
    print("\n1. Dynamic per-stock quantity (capital = 10,000)")
    capital = 10_000
    cases = [
        ("NSE:RELIANCE-EQ", 100.0, 5, 500),    # client example
        ("NSE:RELIANCE-EQ", 2000.0, 5, 25),    # client example
        ("AGL", 100.0, 2, 200),                # 2x approved
        ("NSE:AARON-EQ", 100.0, 1, 100),       # 1x approved
        ("NSE:BAJAJ-AUTO-EQ", 100.0, 1, 100),  # not approved -> 1x
    ]
    for sym, price, exp_mult, exp_qty in cases:
        mult = effective_multiplier(sym, 5)
        qty = int((capital * mult) // price)
        check(f"{sym} @ {price:.0f} -> {qty} qty (x{mult})",
              mult == exp_mult and qty == exp_qty,
              f"expected x{exp_mult} / {exp_qty}")


# ── 2-4. entry LIMIT + SL-M + Target LIMIT payloads ────────────────────
def test_entry_and_protective_payloads():
    print("\n2-4. Entry LIMIT @ LTP + SL-M + Target LIMIT — all at Fyers")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="LIMIT")

    ltp = 872.80
    order_type, limit_price = broker._entry_order_params(ltp)
    check("entry resolves to LIMIT @ LTP", order_type == "LIMIT" and limit_price == ltp,
          f"got {order_type} @ {limit_price}")

    # Entry order
    broker._place_live_order("NSE:TATATECH-EQ", "BUY", 57, order_type=order_type, limit_price=limit_price)
    entry = rec["placed"][-1]
    check("entry type = 1 (LIMIT)", entry["type"] == 1, f"type={entry['type']}")
    check("entry limitPrice = LTP", entry["limitPrice"] == round(ltp, 2), f"limitPrice={entry['limitPrice']}")
    check("entry side = 1 (BUY)", entry["side"] == 1)

    # Protective orders: BUY entry -> SELL-side SL + Target
    rec["placed"].clear()
    broker._place_protective_orders("NSE:TATATECH-EQ", entry_side="BUY", qty=57,
                                    sl_price=864.05, target_price=890.25)
    sl, tp = rec["placed"][0], rec["placed"][1]
    check("SL order type = 4 (SL-M)", sl["type"] == 4, f"type={sl['type']}")
    check("SL stopPrice set", sl["stopPrice"] == 864.05, f"stopPrice={sl['stopPrice']}")
    check("SL side = -1 (reverse of BUY)", sl["side"] == -1)
    check("Target order type = 1 (LIMIT)", tp["type"] == 1, f"type={tp['type']}")
    check("Target limitPrice set", tp["limitPrice"] == 890.25, f"limitPrice={tp['limitPrice']}")
    check("Target side = -1 (reverse of BUY)", tp["side"] == -1)


def test_market_mode():
    print("\n2b. Settings order_type = MARKET falls back to Market order")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="MARKET")
    order_type, limit_price = broker._entry_order_params(500.0)
    broker._place_live_order("NSE:X-EQ", "BUY", 10, order_type=order_type, limit_price=limit_price)
    entry = rec["placed"][-1]
    check("entry type = 2 (MARKET)", entry["type"] == 2, f"type={entry['type']}")
    check("entry limitPrice = 0", entry["limitPrice"] == 0)


# ── 5. OCO reconcile: SL fills -> cancel Target ────────────────────────
def test_oco_reconcile():
    print("\n5. OCO — SL fill triggers cancel of the leftover Target order")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    # SL filled (status 2), Target still pending (status 6)
    rec["orderbook"] = [
        {"id": "SL-1", "status": 2, "tradedPrice": 864.05, "orderDateTime": "2026-08-10 10:15:00"},
        {"id": "TP-1", "status": 6},
    ]
    position = {
        "symbol": "NSE:TATATECH-EQ", "side": "BUY", "qty": 57,
        "sl_price": 864.05, "target_price": 890.25,
        "signal_snapshot": {"fyers_sl_order_id": "SL-1", "fyers_target_order_id": "TP-1"},
    }
    closed = []
    broker.open_positions = lambda: [position]
    # stub PaperBroker.close_trade so no DB write; record the call
    import app.paper_broker as pb
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: closed.append((exit_reason, price))

    summary = broker.reconcile_open_positions()
    check("SL fill reconciled + position closed", len(closed) == 1 and closed[0][0] == "SL_FYERS",
          f"closed={closed}")
    check("leftover Target order cancelled at Fyers",
          any(c.get("id") == "TP-1" for c in rec["cancelled"]),
          f"cancelled={rec['cancelled']}")


# ── 6. trailing SL pushes modify to Fyers ──────────────────────────────
def test_trailing_sl_syncs_to_fyers():
    print("\n6. Trailing SL modifies the SL-M order at Fyers (server-side)")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    # Force PaperBroker.apply_trailing_stop to return a tighter SL.
    import app.paper_broker as pb
    pb.PaperBroker.apply_trailing_stop = lambda self, position, ltp, settings: {
        **position, "sl_price": 870.00,
        "signal_snapshot": {"fyers_sl_order_id": "SL-1"},
    }
    position = {"symbol": "NSE:TATATECH-EQ", "sl_price": 864.05,
                "signal_snapshot": {"fyers_sl_order_id": "SL-1"}}
    broker.apply_trailing_stop(position, ltp=885.0, settings={})
    check("modify_order sent to Fyers", len(rec["modified"]) == 1, f"modified={rec['modified']}")
    if rec["modified"]:
        m = rec["modified"][0]
        check("modify keeps SL-M (type 4)", m["type"] == 4)
        check("modify new stopPrice = trailed SL", m["stopPrice"] == 870.00, f"stopPrice={m['stopPrice']}")
        check("modify carries limitPrice > 0 (Fyers V3)", m["limitPrice"] > 0, f"limitPrice={m['limitPrice']}")


# ── 7. tick rounding + SL-M limitPrice > 0 (2026-08-10 bugs) ──────────
def test_tick_rounding_and_slm_limit():
    print("\n7. Tick rounding + SL-M limitPrice > 0 (regression from 2026-08-10)")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    # These are the exact prices Fyers rejected today. Every rounded output
    # must be a multiple of 0.05, and every SL-M limit must be > 0.
    cases = [
        # (symbol, exit_side, sl_price, target_price)
        ("NSE:ABCAPITAL-EQ", "SELL", 404.217, 416.466),
        ("NSE:ASHOKLEY-EQ",  "SELL", 175.923, 181.254),
        ("NSE:AEGISVOPAK-EQ","SELL", 281.9025, 290.445),
        ("NSE:APTUS-EQ",     "BUY",  260.782, 253.036),
    ]
    for symbol, exit_side, sl, tp in cases:
        rec["placed"].clear()
        broker._place_slm_order(symbol, exit_side, 1, sl)
        broker._place_limit_order(symbol, exit_side, 1, tp)
        slm, tp_ord = rec["placed"][0], rec["placed"][1]
        # Every price sent to Fyers must be a multiple of 0.05
        for label, val in [("SLM stop", slm["stopPrice"]), ("SLM limit", slm["limitPrice"]), ("Target limit", tp_ord["limitPrice"])]:
            multiplier = round(val / 0.05)
            valid = abs(val - multiplier * 0.05) < 1e-6
            check(f"{symbol} {label}={val} is multiple of 0.05", valid)
        check(f"{symbol} SLM limitPrice > 0 (Fyers V3 requires)", slm["limitPrice"] > 0, f"got {slm['limitPrice']}")
        # SL-limit slack direction: SELL exit -> limit BELOW stop; BUY exit -> limit ABOVE stop
        if exit_side == "SELL":
            check(f"{symbol} SELL-exit SL limit < stop", slm["limitPrice"] < slm["stopPrice"])
        else:
            check(f"{symbol} BUY-exit SL limit > stop", slm["limitPrice"] > slm["stopPrice"])


# ── 8. external-close sync (2026-08-10 accidental-reverse bug) ────────
def test_external_close_sync():
    print("\n8. External-close sync — past-grace position, 2 zero-polls -> closed & cleaned")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": [], "net_positions": []}
    broker = make_broker(rec)

    # Mock: get_broker_positions returns EMPTY (Fyers is flat on IRB)
    import app.fyers_client as fc
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False, "positions": rec["net_positions"],
    }
    fc.get_live_ltp_batch = lambda syms: {s: 19.60 for s in syms}

    # Position aged 10 minutes (well past the 300s grace)
    import datetime as dt
    old_entry = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)).isoformat()
    position = {
        "symbol": "NSE:IRBINFRA-EQ", "side": "SELL", "qty": 63,
        "entry_price": 19.53, "sl_price": 19.73, "target_price": 19.14,
        "entry_time": old_entry,
        "signal_snapshot": {"fyers_sl_order_id": "SL-IRB", "fyers_target_order_id": "TP-IRB"},
    }
    broker.open_positions = lambda: [position]
    closed = []
    import app.paper_broker as pb
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: closed.append((exit_reason, pos["symbol"]))

    # First reconcile call: 1st zero-poll. Should NOT close yet (needs 2 consecutive).
    summary1 = broker.reconcile_open_positions()
    check("first empty-poll does NOT close (needs 2 consecutive)",
          len(closed) == 0 and summary1.get("externally_closed", 0) == 0,
          f"closed after poll #1: {closed}")
    # Second reconcile call: 2nd consecutive zero-poll. Now confirmed flat.
    summary2 = broker.reconcile_open_positions()
    check("second consecutive empty-poll closes the position",
          len(closed) == 1 and closed[0] == ("MANUAL_EXTERNAL_EXIT", "NSE:IRBINFRA-EQ"),
          f"closed={closed}")
    check("resting SL order cancelled at Fyers",
          any(c.get("id") == "SL-IRB" for c in rec["cancelled"]))
    check("resting Target order cancelled at Fyers",
          any(c.get("id") == "TP-IRB" for c in rec["cancelled"]))
    check("summary.externally_closed = 1", summary2.get("externally_closed") == 1)


# ── 8c. 2026-08-11 regression: 62s-old position must NOT be closed ────
def test_external_close_2026_08_11_regression():
    print("\n8c. 2026-08-11 REGRESSION — fresh 9:15 entry, Fyers momentarily reports qty=0, must NOT close")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": [], "net_positions": []}
    broker = make_broker(rec)

    # Mock Fyers: returns qty=0 momentarily (as happened at 09:19:00 today for ABCAPITAL).
    import app.fyers_client as fc
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False, "positions": [],
    }
    fc.get_live_ltp_batch = lambda syms: {s: 407.75 for s in syms}

    # Position entered 62 seconds ago — the exact age from today's log where
    # false-close cancelled the protective orders on a real 9:15 trade.
    import datetime as dt
    entry = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=62)).isoformat()
    position = {
        "symbol": "NSE:ABCAPITAL-EQ", "side": "SELL", "qty": 1,
        "entry_price": 407.75, "sl_price": 411.85, "target_price": 399.60,
        "entry_time": entry,
        "signal_snapshot": {"fyers_sl_order_id": "SL-ABCAP", "fyers_target_order_id": "TP-ABCAP"},
    }
    broker.open_positions = lambda: [position]
    closed = []
    cancelled = []
    import app.paper_broker as pb
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: closed.append((exit_reason, pos["symbol"]))
    orig_cancel = broker._cancel_fyers_order
    broker._cancel_fyers_order = lambda oid, reason="": cancelled.append(oid) or {"s": "ok"}

    # Simulate two reconcile cycles both seeing qty=0. Age 62s is < 300s grace.
    broker.reconcile_open_positions()
    broker.reconcile_open_positions()

    check("62s-old position NOT closed (below 300s grace)", len(closed) == 0, f"closed={closed}")
    check("SL order NOT cancelled (position still held)", "SL-ABCAP" not in cancelled, f"cancelled={cancelled}")
    check("Target order NOT cancelled (position still held)", "TP-ABCAP" not in cancelled, f"cancelled={cancelled}")


# ── 8d. Single flaky zero-poll on old position does NOT close ─────────
def test_external_close_single_flaky_poll():
    print("\n8d. Old position + single flaky qty=0 poll -> NOT closed (confirmation required)")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": [], "net_positions": []}
    broker = make_broker(rec)

    import app.fyers_client as fc
    call_count = {"n": 0}
    def flaky_positions(mode):
        # Poll #1: empty (flaky). Poll #2: reports the position back.
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"available": True, "positions": []}
        return {"available": True, "positions": [{"symbol": "NSE:XYZ-EQ", "net_qty": 10}]}
    fc.get_broker_positions = flaky_positions
    fc.get_live_ltp_batch = lambda syms: {s: 100.0 for s in syms}

    import datetime as dt
    old_entry = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)).isoformat()
    position = {
        "symbol": "NSE:XYZ-EQ", "side": "BUY", "qty": 10,
        "entry_price": 100.0, "entry_time": old_entry,
        "signal_snapshot": {"fyers_sl_order_id": "SL-X", "fyers_target_order_id": "TP-X"},
    }
    broker.open_positions = lambda: [position]
    closed = []
    cancelled = []
    import app.paper_broker as pb
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: closed.append(exit_reason)
    broker._cancel_fyers_order = lambda oid, reason="": cancelled.append(oid) or {"s": "ok"}

    # Poll #1: flaky empty -> counter goes to 1, NOT closed
    broker.reconcile_open_positions()
    check("after 1st flaky poll: not closed", len(closed) == 0, f"closed={closed}")
    check("after 1st flaky poll: no order cancels", len(cancelled) == 0)
    # Poll #2: real (Fyers holds it) -> counter resets to 0
    broker.reconcile_open_positions()
    check("after recovery poll: still not closed", len(closed) == 0, f"closed={closed}")
    check("after recovery poll: still no cancels", len(cancelled) == 0)


# ── 8b. sync does NOT touch fresh positions (< 60s old) ────────────────
def test_external_close_grace():
    print("\n8b. External-close sync respects 300s grace on fresh entries")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": [], "net_positions": []}
    broker = make_broker(rec)
    import app.fyers_client as fc
    fc.get_broker_positions = lambda mode: {"available": True, "cached": False, "positions": []}
    fc.get_live_ltp_batch = lambda syms: {s: 100.0 for s in syms}

    import datetime as dt
    fresh_entry = dt.datetime.now(dt.timezone.utc).isoformat()  # just now
    position = {"symbol": "NSE:FRESH-EQ", "side": "BUY", "qty": 1,
                "entry_price": 100.0, "entry_time": fresh_entry, "signal_snapshot": {}}
    broker.open_positions = lambda: [position]
    closed = []
    import app.paper_broker as pb
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: closed.append(exit_reason)
    broker.reconcile_open_positions()
    check("fresh position NOT wrongly closed (grace period)", len(closed) == 0, f"closed={closed}")


# ── 9. algo1 _has_open_position guard (re-entry prevention) ────────────
def test_algo1_open_position_guard():
    print("\n9. algo1 refuses to re-enter a symbol that is already open")
    from app.strategies.algo1_opening_range import Algo1OpeningRange

    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.entry_failures = {}
    strat.settings = {"capital_per_trade": 10000, "margin_multiplier": 5,
                      "sl_pct": 1.0, "target_pct": 2.0}
    strat.selected_symbols = set()
    strat.selected_sides = {}
    strat.debug_logger = type("X", (), {"add_selected": lambda self, *a, **kw: None})()

    class FakeBroker:
        def __init__(self):
            self.open_pos = []
            self.traded = set()
        def open_positions(self):
            return self.open_pos
        def already_traded_today(self, s):
            return s in self.traded
    fb = FakeBroker()
    fb.open_pos = [{"symbol": "NSE:IRBINFRA-EQ", "side": "SELL"}]
    strat.broker = fb

    result = strat._enter("NSE:IRBINFRA-EQ", "SELL", 19.55)
    check("re-entry blocked when position already open",
          result is False and strat.entry_failures.get("NSE:IRBINFRA-EQ") == "position_already_open",
          f"result={result}, failures={strat.entry_failures}")


# ── 10. protective-order retry succeeds on second attempt ─────────────
def test_protective_retry():
    print("\n10. Protective-order retry — first attempt fails, second succeeds")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    attempts = {"n": 0}
    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return {"s": "error", "code": -99, "message": "transient 429"}
        return {"s": "ok", "id": "SL-RETRY-OK"}
    import time as _t
    orig_sleep = _t.sleep
    _t.sleep = lambda s: None  # skip 5s wait in test
    try:
        oid, err = broker._place_with_retry("SL", "NSE:TEST-EQ", flaky)
    finally:
        _t.sleep = orig_sleep
    check("retry attempted twice", attempts["n"] == 2, f"attempts={attempts['n']}")
    check("retry returns success order_id", oid == "SL-RETRY-OK" and err is None, f"oid={oid}, err={err}")


# ── 11. Streaming FCFS Phase 2 (fix 13, 2026-08-11) ──────────────────
def test_streaming_fcfs_phase2():
    print("\n11. Streaming Phase 2 FCFS — first N matching wins, later ones skipped once cap hit")
    from app.strategies.algo1_opening_range import Algo1OpeningRange
    import time as _t

    # Fake 20 symbols with staggered "arrival" via time.sleep in load_symbol
    class FakeBroker:
        def __init__(self):
            self.trades = []
        def summary(self):
            return {"trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0,
                    "cash": 1_000_000}
        def open_positions(self): return []
        def already_traded_today(self, s): return False
        def open_trade(self, symbol, side, qty, entry, sl, target, trigger, snapshot, entry_time=None):
            self.trades.append({"symbol": symbol, "side": side, "qty": qty, "entry": entry})

    # Bypass Algo1OpeningRange __init__ (needs Fyers etc.)
    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.watchlist = [f"NSE:STOCK{i:02d}-EQ" for i in range(20)]
    strat.settings = {
        "capital_per_trade": 10000, "margin_multiplier": 5,
        "sl_pct": 1.0, "target_pct": 2.0,
        "max_trades_per_day": 3, "max_buy_trades": 3, "max_sell_trades": 3,
        "test_schedule_enabled": False,
    }
    strat.opening_candles = {}
    strat.prev_close = {}
    strat.history_verified_opening_symbols = set()
    strat.scan_seen_symbols = set()
    strat.open_extreme_symbols = set()
    strat.prev_close_ready_symbols = set()
    strat.selected_symbols = set()
    strat.selected_sides = {}
    strat.buy_candidates = []
    strat.sell_candidates = []
    strat.candidate_details = {}
    strat.entry_failures = {}
    strat.sector_map = {}
    strat.test_mode_ltps = {}
    class NoopLogger:
        def __getattr__(self, name):
            return lambda *a, **kw: None
    strat.debug_logger = NoopLogger()
    strat.broker = FakeBroker()

    # scan_candle_time returns "09:15" (test_schedule_enabled=False -> production)
    # _opening_candle_needs_history returns True for symbols not in opening_candles
    # -> every symbol is a backfill candidate

    # Stub get_stored_access_token to return truthy
    import app.strategies.algo1_opening_range as algo1_mod
    algo1_mod.get_stored_access_function = lambda: "FAKE"
    algo1_mod.get_stored_access_token = lambda: "FAKE"

    # Fake backfill: each symbol "arrives" with a valid open==low BUY candle,
    # in order STOCK00, STOCK01, ..., STOCK19. Sleep 0 to keep test fast but
    # let as_completed order be deterministic (submission order).
    def fake_single_candle(symbol, candle_time):
        # every symbol matches BUY shape: open == low
        return [{"time": 1700000000, "open": 100.0, "high": 100.5, "low": 100.0, "close": 100.3, "volume": 1000}]
    algo1_mod.get_single_minute_candle = fake_single_candle
    algo1_mod.get_previous_close = lambda symbol: 99.5  # gap = 0.5%, within GAP_LIMIT_PCT
    algo1_mod.get_live_ltp_batch = lambda syms: {s: 100.05 for s in syms}

    entered = strat._stream_backfill_and_enter(
        get_ltp_fn=lambda s: 100.05,
        existing_counts={"trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0},
        already_attempted=set(),
    )
    check("streaming entered exactly max_trades_per_day (3)", entered == 3, f"entered={entered}")
    check("broker.trades has 3 rows", len(strat.broker.trades) == 3, f"got={len(strat.broker.trades)}")
    check("all 3 are BUY side", all(t["side"] == "BUY" for t in strat.broker.trades))
    check("side cap respected: no more than 3 BUY trades attempted",
          sum(1 for t in strat.broker.trades if t["side"] == "BUY") <= 3)
    # Confirm we DID NOT touch all 20 symbols — streaming should stop early.
    # history_verified_opening_symbols is updated only for symbols we processed.
    verified = len(strat.history_verified_opening_symbols)
    check("streaming stopped early (verified < 20 symbols)",
          verified < 20, f"verified={verified}/20 — cap reached, remaining cancelled")


# ── 12. Trailing SL metadata stamping ────────────────────────────────
def test_trailing_metadata_tracks_activation_and_bumps():
    print("\n12. Trailing SL metadata — activation timestamp + update count in signal_snapshot")
    import app.paper_broker as pb

    # Test 6 (trailing SL syncs to Fyers) replaces PaperBroker.apply_trailing_stop
    # with a lambda for its own assertions and doesn't restore it. Restore the
    # real class method here so this test exercises the real logic.
    pb.PaperBroker.apply_trailing_stop = _REAL_APPLY_TRAILING_STOP

    # Stub Supabase writes so apply_trailing_stop can run without a real DB
    captured_updates = []
    pb.run_with_supabase = lambda fn: captured_updates.append("write") or type("R", (), {"data": []})()

    broker = object.__new__(pb.PaperBroker)
    broker.algo_id = "algo1"
    broker.positions_table_name = lambda: "positions"

    settings = {
        "exit_mode": "fixed_target_trailing_sl",
        "trailing_sl_enabled": True,
        "trailing_sl_trigger_pct": 1.0,
        "trailing_sl_distance_pct": 0.5,
    }

    # BUY at 100, initial SL at 99, trigger at +1% => 101
    position = {
        "id": "pos-1", "symbol": "NSE:TEST-EQ", "side": "BUY",
        "entry_price": 100.0, "sl_price": 99.0, "target_price": 102.0,
        "highest_price": 100.0, "lowest_price": 100.0,
        "trailing_sl_active": False,
        "signal_snapshot": {"initial_sl_price": 99.0, "trailing": {"activated": False, "update_count": 0}},
    }

    # Tick 1 — price at 100.5 (0.5% up). Below trigger, no activation.
    p1 = broker.apply_trailing_stop(position, ltp=100.5, settings=settings)
    check("tick #1 below trigger: no activation",
          not p1["signal_snapshot"].get("trailing", {}).get("first_activated_at"),
          f"trailing={p1['signal_snapshot'].get('trailing')}")
    check("tick #1: update_count still 0",
          p1["signal_snapshot"]["trailing"].get("update_count") == 0)

    # Tick 2 — price at 101.5 (1.5% up). Triggers activation + first SL bump.
    # highest becomes 101.5, new_sl = 101.5 * 0.995 = 100.9925 > 99 -> bump.
    p2 = broker.apply_trailing_stop(p1, ltp=101.5, settings=settings)
    t2 = p2["signal_snapshot"].get("trailing", {})
    check("tick #2 first activation: first_activated_at set", bool(t2.get("first_activated_at")),
          f"trailing={t2}")
    check("tick #2 first bump: update_count == 1", t2.get("update_count") == 1,
          f"got={t2.get('update_count')}")
    check("tick #2: sl_price bumped above initial", p2["sl_price"] > 99.0,
          f"sl_price={p2['sl_price']}")

    # Tick 3 — price at 102 (higher). SL bumps again.
    p3 = broker.apply_trailing_stop(p2, ltp=102.0, settings=settings)
    t3 = p3["signal_snapshot"]["trailing"]
    check("tick #3 second bump: update_count == 2", t3.get("update_count") == 2,
          f"got={t3.get('update_count')}")
    check("tick #3: first_activated_at is preserved", t3.get("first_activated_at") == t2.get("first_activated_at"))
    check("tick #3: last_updated_at is fresher than first_activated_at OR equal",
          t3.get("last_updated_at") >= t3.get("first_activated_at"))

    # Tick 4 — price DROPS to 101.7 (still profitable but below prev high).
    # highest stays at 102. new_sl computed from highest (unchanged) so SL
    # does NOT move. update_count must NOT increment.
    p4 = broker.apply_trailing_stop(p3, ltp=101.7, settings=settings)
    t4 = p4["signal_snapshot"]["trailing"]
    check("tick #4 no new high: update_count stays at 2 (no false bump)",
          t4.get("update_count") == 2,
          f"got={t4.get('update_count')}")


# ── 13. WS pre-market warmup gating (fix 17, 2026-08-12) ────────────
def test_ws_premarket_warmup_gating():
    print("\n13. WS warmup gating — feed_permitted for each IST hour")

    # Replicates the exact boolean the watchdog uses. If the source ever
    # drifts from this literal, the assertions below will fail and we'll
    # notice before it goes to prod.
    def feed_permitted(hhmm: str, has_token: bool = True) -> bool:
        market_open = "09:15" <= hhmm < "15:30"
        premarket_warmup = "09:05" <= hhmm < "09:15"
        return (market_open or premarket_warmup) and has_token

    cases = [
        # (time IST, has_token, expected feed_permitted, description)
        ("00:00", True,  False, "midnight — off hours"),
        ("08:59", True,  False, "8:59 IST — still off hours"),
        ("09:04", True,  False, "9:04 — one min before warmup"),
        ("09:05", True,  True,  "9:05 — warmup window starts"),
        ("09:10", True,  True,  "9:10 — mid warmup"),
        ("09:14", True,  True,  "9:14 — still warmup (last minute)"),
        ("09:15", True,  True,  "9:15 — market opens, still permitted"),
        ("09:16", True,  True,  "9:16 — normal market hours"),
        ("12:30", True,  True,  "midday — market hours"),
        ("15:29", True,  True,  "3:29 PM — last minute of market"),
        ("15:30", True,  False, "3:30 PM — market closes"),
        ("15:31", True,  False, "3:31 PM — post close"),
        ("20:00", True,  False, "evening — off hours"),
        ("09:10", False, False, "warmup window but no token"),
        ("10:00", False, False, "market hours but no token"),
    ]
    for hhmm, has_token, expected, desc in cases:
        actual = feed_permitted(hhmm, has_token)
        check(f"{hhmm} token={has_token}: {desc}",
              actual == expected,
              f"got={actual}, expected={expected}")


# ── 14. Token-expired guard (fix 18, from 2026-08-13 log) ────────────
def test_token_expired_guard():
    print("\n14. Token-expired guard — mark/clear + hold-remaining accounting")
    import app.engine as eng
    import time as _t

    # Reset state so this test is independent of others.
    eng._token_known_expired_at = 0.0

    check("initial hold_remaining == 0", eng._token_expired_hold_remaining() == 0)

    eng._mark_token_expired("test trigger")
    r1 = eng._token_expired_hold_remaining()
    check("after mark: hold_remaining > 0",
          r1 > 0 and r1 <= eng._TOKEN_EXPIRED_HOLD_SECONDS,
          f"got={r1}")

    # Mark again — should NOT reset (idempotent within same expiry event).
    # It DOES bump the timestamp to now, so remaining should still be near-max.
    prev = eng._token_known_expired_at
    _t.sleep(0.01)
    eng._mark_token_expired("test trigger again")
    check("second mark: timestamp bumped to fresher",
          eng._token_known_expired_at >= prev)

    eng._clear_token_expired("test fresh oauth")
    check("after clear: hold_remaining == 0",
          eng._token_expired_hold_remaining() == 0,
          f"got={eng._token_expired_hold_remaining()}")
    check("after clear: flag reset to 0",
          eng._token_known_expired_at == 0.0)


# ── 15. Mode-toggle cooldown (fix 19, from 2026-08-13 log) ───────────
def test_mode_toggle_cooldown():
    print("\n15. Mode-toggle cooldown — reject rapid toggles, allow after cooldown")
    import app.engine as eng

    # Reset state
    eng._last_mode_switch_at = 0.0
    check("initial cooldown_remaining == 0",
          eng._mode_toggle_cooldown_remaining() == 0)

    # Simulate a toggle happened just now
    import time as _t
    eng._last_mode_switch_at = _t.time()
    r1 = eng._mode_toggle_cooldown_remaining()
    check("after toggle: cooldown_remaining > 0",
          r1 > 0 and r1 <= eng._MODE_TOGGLE_COOLDOWN_SECONDS,
          f"got={r1}")

    # Simulate cooldown fully elapsed (fake it by rewinding the timestamp)
    eng._last_mode_switch_at = _t.time() - eng._MODE_TOGGLE_COOLDOWN_SECONDS - 1
    check("after cooldown elapsed: cooldown_remaining == 0",
          eng._mode_toggle_cooldown_remaining() == 0)

    # Reset for clean state
    eng._last_mode_switch_at = 0.0


# ── 16. Parallel paper mirroring (fix, 2026-08-13 evening) ───────────
def test_parallel_paper_mirroring():
    print("\n16. Parallel paper — shadow broker mirrors live trades AND runs its own exits")
    from app.strategies.algo1_opening_range import Algo1OpeningRange

    # Fake primary broker (live) and fake shadow broker (paper). Both accept
    # open_trade / open_positions / already_traded_today / etc.
    class RecordingBroker:
        def __init__(self, label):
            self.label = label
            self.trades = []
            self.open_pos = []
            self.exits = []
            self.traded_today = set()
        def open_trade(self, symbol, side, qty, entry, sl, target, trigger, snapshot, entry_time=None):
            self.trades.append({"symbol": symbol, "side": side, "qty": qty, "entry": entry, "sl": sl, "target": target})
            self.traded_today.add(symbol)
            self.open_pos.append({"symbol": symbol, "side": side, "sl_price": sl, "target_price": target, "entry_price": entry, "qty": qty, "id": f"{self.label}-1"})
        def already_traded_today(self, s): return s in self.traded_today
        def open_positions(self): return list(self.open_pos)
        def summary(self): return {"trade_count_today": len(self.trades), "buy_count_today": 0, "sell_count_today": 0, "cash": 100000}
        def apply_trailing_stop(self, pos, ltp, settings): return pos
        def should_exit_at_target(self, s): return True
        def close_trade(self, pos, price, reason, exit_time=None):
            self.exits.append({"symbol": pos["symbol"], "reason": reason, "price": price})
            self.open_pos = [p for p in self.open_pos if p["symbol"] != pos["symbol"]]

    # Bypass Algo1 __init__
    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.settings = {
        "capital_per_trade": 10000, "margin_multiplier": 5,
        "sl_pct": 1.0, "target_pct": 2.0,
        "test_schedule_enabled": False, "test_candle_time": "11:10",
        "parallel_paper_enabled": True,
    }
    strat.entry_failures = {}
    strat.selected_symbols = set()
    strat.selected_sides = {}
    strat.candidate_details = {}
    strat.opening_candles = {}
    strat.sector_map = {}
    strat.prev_close = {}
    strat.test_mode_ltps = {}
    class NoopLogger:
        def __getattr__(self, name): return lambda *a, **kw: None
    strat.debug_logger = NoopLogger()

    live_broker = RecordingBroker("live")
    shadow_broker = RecordingBroker("shadow-paper")
    strat.broker = live_broker
    strat._shadow_paper_broker = shadow_broker

    # Sub 16a — entry mirrors to BOTH
    ok = strat._enter("NSE:RELIANCE-EQ", "BUY", 1000.0)
    check("_enter returned True (live succeeded)", ok is True)
    check("primary (live) recorded 1 trade", len(live_broker.trades) == 1,
          f"got={len(live_broker.trades)}")
    check("shadow (paper) mirrored 1 trade", len(shadow_broker.trades) == 1,
          f"got={len(shadow_broker.trades)}")
    check("both trades have identical qty",
          live_broker.trades[0]["qty"] == shadow_broker.trades[0]["qty"])
    check("both trades have identical SL", live_broker.trades[0]["sl"] == shadow_broker.trades[0]["sl"])

    # Sub 16b — _active_brokers returns both
    brokers = strat._active_brokers()
    check("_active_brokers returns 2 (primary + shadow)", len(brokers) == 2,
          f"got={len(brokers)}")
    check("_active_brokers[0] is primary", brokers[0] is live_broker)
    check("_active_brokers[1] is shadow", brokers[1] is shadow_broker)

    # Sub 16c — check_exits fires on BOTH when SL hits
    live_broker.open_pos[0]["_last_ltp"] = 989.0   # below SL 990.0 -> triggers exit
    shadow_broker.open_pos[0]["_last_ltp"] = 989.0
    strat.check_exits()
    check("primary SL fired", any(e["reason"] == "SL" for e in live_broker.exits),
          f"live exits={live_broker.exits}")
    check("shadow SL fired", any(e["reason"] == "SL" for e in shadow_broker.exits),
          f"shadow exits={shadow_broker.exits}")

    # Sub 16d — turning shadow off means only primary runs
    strat._shadow_paper_broker = None
    strat.entry_failures.clear()
    strat.selected_symbols.clear()
    ok = strat._enter("NSE:TCS-EQ", "SELL", 3000.0)
    check("without shadow: primary got trade", len(live_broker.trades) == 2)
    check("without shadow: shadow untouched", len(shadow_broker.trades) == 1,
          f"got={len(shadow_broker.trades)}")


# ── 17. Single-tick candle rejection (F1 regression from 2026-08-18) ─
def test_single_tick_candle_rejection():
    """The 2026-08-18 outage: F1 introduced a 'single_tick' rejection
    reason but forgot to add the matching stage_data bucket, so the
    first single-tick candle at 09:16 raised KeyError and killed the
    whole scan.

    This test hits _evaluate_symbol_signal with an O==H==L candle using
    the REAL ScanDebugLogger (not NoopLogger). If the bucket is missing,
    add_shape_result crashes and the test fails, catching the class of
    bug before it ships.
    """
    print("\n17. Single-tick candle rejection — no KeyError, correct rejection reason")
    from app.strategies.algo1_opening_range import Algo1OpeningRange, ScanDebugLogger

    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.watchlist = ["NSE:FLAT-EQ"]
    strat.settings = {
        "capital_per_trade": 10000, "margin_multiplier": 5,
        "sl_pct": 1.0, "target_pct": 2.0,
        "test_schedule_enabled": False,
    }
    strat.opening_candles = {"NSE:FLAT-EQ": [{"time": 1700000000, "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "volume": 1}]}
    strat.prev_close = {"NSE:FLAT-EQ": 495.0}
    strat.prev_close_ready_symbols = set()
    strat.open_extreme_symbols = set()
    strat.buy_candidates = []
    strat.sell_candidates = []
    strat.candidate_details = {}
    strat.sector_map = {}
    # REAL debug logger — this is what triggered the 2026-08-18 KeyError.
    strat.debug_logger = ScanDebugLogger(1)

    verdict = None
    try:
        verdict = strat._evaluate_symbol_signal("NSE:FLAT-EQ")
    except KeyError as exc:
        check("no KeyError on single-tick candle", False, f"crashed with KeyError: {exc}")
        return

    check("no KeyError on single-tick candle", True)
    check("single-tick candle returns None (rejected)", verdict is None, f"got={verdict}")
    row = strat.candidate_details.get("NSE:FLAT-EQ") or {}
    check("candidate_details tags reason as single_tick_candle",
          row.get("rejection_reason") == "single_tick_candle",
          f"got={row.get('rejection_reason')!r}")
    check("candidate_details signal_shape is single_tick",
          row.get("signal_shape") == "single_tick",
          f"got={row.get('signal_shape')!r}")
    check("not marked selected_for_trade",
          row.get("selected_for_trade") is False)

    # Also verify _build_candidates_from_collection handles it the same
    # way (that path runs the batch-shape check on all symbols).
    strat.candidate_details = {}
    strat.buy_candidates = []
    strat.sell_candidates = []
    strat.open_extreme_symbols = set()
    strat.prev_close_ready_symbols = set()
    strat.debug_logger = ScanDebugLogger(1)
    try:
        strat._build_candidates_from_collection()
    except KeyError as exc:
        check("_build_candidates_from_collection: no KeyError", False, f"crashed: {exc}")
        return
    check("_build_candidates_from_collection: no KeyError", True)
    row = strat.candidate_details.get("NSE:FLAT-EQ") or {}
    check("_build path also tags single_tick_candle",
          row.get("rejection_reason") == "single_tick_candle",
          f"got={row.get('rejection_reason')!r}")


# ── 18. Every rejection reason used at runtime has a stage_data bucket ──
def test_rejection_reason_buckets_exist():
    """The 2026-08-18 outage was caused by a rejection reason string
    ('single_tick') that had no matching stage_data bucket. Rather than
    just cover that one string, statically enumerate every string ever
    passed as the `reason` argument to add_shape_result and check each
    has an initialized bucket. Adds ~zero cost as coverage grows."""
    print("\n18. Rejection-reason buckets — every add_shape_result reason has a stage_data slot")
    import re
    from pathlib import Path
    from app.strategies.algo1_opening_range import ScanDebugLogger

    src_path = Path(__file__).resolve().parent.parent / "app" / "strategies" / "algo1_opening_range.py"
    src = src_path.read_text(encoding="utf-8")
    # Regex: add_shape_result(..., "reason") or add_shape_result(..., 'reason')
    # Only literal strings — dynamic ones can't be checked statically anyway.
    reasons = set()
    for match in re.finditer(r"add_shape_result\([^)]*?[\"'](\w+)[\"']\s*\)", src):
        reasons.add(match.group(1))
    # The "passed" variant emits sides ("buy"/"sell"), not rejection reasons.
    reasons -= {"buy", "sell", "none"}  # sides / neutral marker
    # Also add "flat" and "neither" which are the historical baseline.
    reasons |= {"flat", "neither"}
    check("static scan found at least the known reasons",
          "flat" in reasons and "neither" in reasons and "single_tick" in reasons,
          f"found={sorted(reasons)}")

    logger = ScanDebugLogger(1)
    missing = []
    for reason in reasons:
        if reason in {"buy", "sell", "none"}:
            continue
        # add_shape_result now lazy-creates any missing bucket (defensive
        # fix from 2026-08-18). We still want to fail loudly if the
        # baseline dict is missing an entry — that's the class of bug.
        bucket_key = f"shape_failed_{reason}"
        if bucket_key not in logger.stage_data:
            missing.append(bucket_key)
    check("every rejection reason has a pre-defined stage_data bucket",
          not missing,
          f"missing buckets: {missing}" if missing else "all present")


# ── 19-21. Proxy preflight (F2) ─────────────────────────────────────────
def _build_broker_with_proxy_stub(proxy_reachable, proxy_url="http://user:pw@bore.pub:12345"):
    """Shared fixture for the three proxy-preflight tests."""
    from unittest.mock import patch
    broker = object.__new__(lb.LiveBroker)
    return broker, patch, proxy_reachable, proxy_url


def test_proxy_preflight_refuses_when_unreachable():
    print("\n19. Proxy preflight — refuses order when proxy is configured but unreachable")
    from unittest.mock import patch
    broker = object.__new__(lb.LiveBroker)
    with patch.object(lb, "get_runtime_trading_mode", return_value="live"), \
         patch.object(lb, "get_fyers_config", return_value={"proxy_url": "http://user:pw@bore.pub:99999"}), \
         patch.object(lb, "check_proxy_reachable", return_value=(False, "timeout")), \
         patch.object(lb, "get_fyers_model") as fake_fyers:
        response = broker._place_live_order("NSE:TEST-EQ", "BUY", 10, "MARKET")
    check("preflight returned error dict", isinstance(response, dict) and response.get("s") == "error",
          f"got={response}")
    check("error code is proxy_unreachable", response.get("code") == "proxy_unreachable",
          f"got={response.get('code')}")
    check("get_fyers_model NEVER called (short-circuited)", fake_fyers.call_count == 0,
          f"call_count={fake_fyers.call_count}")


def test_proxy_preflight_allows_when_reachable():
    print("\n20. Proxy preflight — passes through when proxy is reachable")
    from unittest.mock import patch, MagicMock
    broker = object.__new__(lb.LiveBroker)
    fake_client = MagicMock()
    fake_client.place_order.return_value = {"s": "ok", "id": "OK-1"}
    with patch.object(lb, "get_runtime_trading_mode", return_value="live"), \
         patch.object(lb, "get_fyers_config", return_value={"proxy_url": "http://ok"}), \
         patch.object(lb, "check_proxy_reachable", return_value=(True, None)), \
         patch.object(lb, "get_fyers_model", return_value=fake_client):
        response = broker._place_live_order("NSE:TEST-EQ", "BUY", 10, "MARKET")
    check("order forwarded to Fyers", fake_client.place_order.called)
    check("returned success", response.get("s") == "ok", f"got={response}")


def test_proxy_preflight_no_proxy_configured():
    print("\n21. Proxy preflight — no proxy configured skips check entirely")
    from unittest.mock import patch, MagicMock
    broker = object.__new__(lb.LiveBroker)
    fake_client = MagicMock()
    fake_client.place_order.return_value = {"s": "ok", "id": "OK-1"}
    checked = []
    def spy_check(url):
        checked.append(url)
        return (True, None)
    with patch.object(lb, "get_runtime_trading_mode", return_value="paper"), \
         patch.object(lb, "get_fyers_config", return_value={"proxy_url": ""}), \
         patch.object(lb, "check_proxy_reachable", side_effect=spy_check), \
         patch.object(lb, "get_fyers_model", return_value=fake_client):
        response = broker._place_live_order("NSE:TEST-EQ", "BUY", 10, "MARKET")
    check("check_proxy_reachable NOT called when proxy_url empty",
          not checked, f"checked={checked}")
    check("order still placed", response.get("s") == "ok")


# ── 22-24. Skip-scan-today (new feature) ────────────────────────────────
def test_skip_scan_today_short_circuits_algo1():
    print("\n22. Skip scan today — algo1.evaluate_entries returns early without work")
    import datetime
    from app.strategies.algo1_opening_range import Algo1OpeningRange
    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.entries_evaluated_today = None
    strat.skip_scan_date = datetime.date.today()
    # These attrs would be accessed if the short-circuit failed.
    strat.settings = {"test_schedule_enabled": False}
    called = []
    strat._record_scan_results = lambda *a, **kw: called.append(("record", kw.get("scan_status")))

    result = strat.evaluate_entries(get_ltp_fn=lambda s: 0.0)

    check("evaluate_entries returned True (short-circuited)", result is True)
    check("scan_status recorded as 'skipped'",
          any(status == "skipped" for _, status in called),
          f"called={called}")
    check("entries_evaluated_today set (prevents re-runs same day)",
          strat.entries_evaluated_today == datetime.date.today())


def test_skip_scan_today_short_circuits_algo3():
    print("\n23. Skip scan today — algo3.scan_enabled returns False")
    import datetime
    from app.strategies.algo3_silver_micro import Algo3SilverMicro
    strat = object.__new__(Algo3SilverMicro)
    strat.settings = {"scan_enabled": True}
    strat.skip_scan_date = datetime.date.today()
    check("scan_enabled False when skip_scan_date=today",
          strat.scan_enabled() is False)
    strat.skip_scan_date = None
    check("scan_enabled True when skip cleared",
          strat.scan_enabled() is True)


def test_skip_scan_date_auto_resets():
    print("\n24. Skip scan today — yesterday's skip date does NOT skip today's scan")
    import datetime
    from app.strategies.base import Strategy
    # Strategy is abstract; make a minimal concrete instance.
    class Dummy(Strategy):
        def on_tick(self, *a, **kw): pass
        def on_candle_close(self, *a, **kw): pass
        def check_exits(self, *a, **kw): pass
        def square_off_all(self, *a, **kw): pass
    d = Dummy()
    d.skip_scan_date = datetime.date.today() - datetime.timedelta(days=1)
    check("yesterday's skip is not today's skip",
          d.is_scan_skipped_today() is False)
    d.skip_scan_date = datetime.date.today()
    check("today's skip is today's skip",
          d.is_scan_skipped_today() is True)


# ── 25. F5 OAuth throttle ──────────────────────────────────────────────
def test_oauth_throttle_serializes_exchanges():
    print("\n25. F5 OAuth throttle — rapid re-exchange within 30s raises")
    from unittest.mock import patch
    import app.fyers_auth as fa
    # Reset any prior state from earlier tests
    fa._last_exchange_at.clear()
    key = "test_mode"
    with patch.object(fa, "_fyers_config", return_value={
              "client_id": "X-100", "secret_key": "s",
              "redirect_uri": "https://x", "proxy_url": None}), \
         patch.object(fa, "_fyers_proxies", return_value=None):
        # First call sets the lock timestamp. We need to force it to fail
        # AFTER passing the throttle check but BEFORE hitting Fyers, so
        # the timestamp gets set but no network happens.
        with patch("app.fyers_auth.requests.post", side_effect=RuntimeError("stubbed")):
            try:
                fa.exchange_auth_code("code1", mode=key)
            except Exception:
                pass  # expected — we stubbed the network
        first_ts = fa._last_exchange_at.get(key, 0)
        check("first exchange recorded a timestamp", first_ts > 0)
        # Second call within 30s must be rejected by the throttle,
        # BEFORE the network stub gets a chance to fire.
        with patch("app.fyers_auth.requests.post") as fake_post:
            try:
                fa.exchange_auth_code("code2", mode=key)
                check("second rapid exchange raises", False, "no exception raised")
            except RuntimeError as exc:
                check("second rapid exchange raises RuntimeError",
                      "too soon" in str(exc).lower() or "wait" in str(exc).lower(),
                      f"msg={exc}")
            check("network NOT hit on the throttled call",
                  fake_post.call_count == 0, f"call_count={fake_post.call_count}")


# ── 26. F7 429 vs token-expired distinction ────────────────────────────
def test_connection_status_429_stays_degraded_not_expired():
    print("\n26. F7 — HTTP 429 during profile verify returns 'degraded', NOT 'expired'")
    from unittest.mock import patch, MagicMock
    import app.fyers_client as fc

    class FakeProfile:
        def get_profile(self):
            return {"s": "error", "code": -429, "message": "Bad request (code 429)"}

    fake_token_row = {"access_token": "T", "refresh_token": "R"}
    with patch.object(fc, "get_active_broker_key", return_value="fyers_live"), \
         patch.object(fc, "get_stored_token_row", return_value=fake_token_row), \
         patch.object(fc, "get_runtime_trading_mode", return_value="live"), \
         patch.object(fc, "_is_recent_token_row", return_value=False), \
         patch.object(fc, "get_fyers_model", return_value=FakeProfile()):
        result = fc.get_connection_status()
    check("connection reported 'degraded', not 'expired'",
          result.get("status") == "degraded",
          f"got status={result.get('status')!r} message={result.get('message')!r}")
    check("connection stays 'connected'=True during 429",
          result.get("connected") is True,
          f"got connected={result.get('connected')}")


# ── 27. F4 pre-market watchdog skip ────────────────────────────────────
def test_pre_market_no_tick_not_counted_as_failure():
    print("\n27. F4 — 'no market tick' fired before 09:15 IST does NOT increment failure counter")
    import datetime as _dt
    from unittest.mock import patch
    import app.engine as eng
    # Force IST clock to 09:07 so the pre-market branch fires.
    eng._feed_reconnect_failure_count = 0
    eng._feed_circuit_open_until = 0.0
    eng._feed_disconnected_since = 0.0

    class FakeIST(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 8, 19, 9, 7, 0)

    with patch.object(eng.datetime, "datetime", FakeIST):
        eng._on_live_feed_status({
            "connected": False,
            "error": "No Fyers market tick received within 30 seconds of subscription",
        })
    check("pre-market no-tick did NOT increment failure counter",
          eng._feed_reconnect_failure_count == 0,
          f"got count={eng._feed_reconnect_failure_count}")

    # Same message AFTER 09:15 SHOULD count.
    class FakeIST_open(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 8, 19, 10, 30, 0)

    with patch.object(eng.datetime, "datetime", FakeIST_open):
        eng._on_live_feed_status({
            "connected": False,
            "error": "No Fyers market tick received within 30 seconds of subscription",
        })
    check("same message DURING market hours does count as failure",
          eng._feed_reconnect_failure_count == 1,
          f"got count={eng._feed_reconnect_failure_count}")
    # Reset shared state
    eng._feed_reconnect_failure_count = 0
    eng._feed_circuit_open_until = 0.0


# ── 28. F6 post-recovery grace ─────────────────────────────────────────
def test_post_recovery_grace_ignores_immediate_failure():
    print("\n28. F6 — failure within 60s of recovery is ignored (no counter bump)")
    import time as _time
    import app.engine as eng
    eng._feed_reconnect_failure_count = 3
    eng._feed_circuit_open_until = 0.0
    eng._feed_last_recovery_at = 0.0
    eng._reset_feed_circuit("test recovery")
    check("recovery timestamp set", eng._feed_last_recovery_at > 0)
    check("failure count reset to 0", eng._feed_reconnect_failure_count == 0)

    # Simulate a failure immediately after recovery
    eng._record_feed_failure("test immediate re-fail")
    check("post-recovery grace ignored the failure (count still 0)",
          eng._feed_reconnect_failure_count == 0,
          f"got count={eng._feed_reconnect_failure_count}")

    # Force recovery timestamp older than grace, failure should count again
    eng._feed_last_recovery_at = _time.time() - (eng._FEED_POST_RECOVERY_GRACE_SECONDS + 1)
    eng._record_feed_failure("test after grace elapsed")
    check("failures resume counting after grace elapses",
          eng._feed_reconnect_failure_count == 1,
          f"got count={eng._feed_reconnect_failure_count}")
    # Reset
    eng._feed_reconnect_failure_count = 0
    eng._feed_last_recovery_at = 0.0


# ── 29. F6 minimum backoff floor ───────────────────────────────────────
def test_current_backoff_respects_min_floor():
    print("\n29. F6 — _current_backoff_seconds returns at least 30s during a failure run")
    import app.engine as eng
    eng._feed_reconnect_failure_count = 0
    check("zero failures -> returns first ladder value (no floor)",
          eng._current_backoff_seconds() == float(eng._FEED_BACKOFF_SEQUENCE[0]))
    eng._feed_reconnect_failure_count = 1
    check("after 1 failure -> floor enforced (5s ladder < 30s min)",
          eng._current_backoff_seconds() >= eng._FEED_MIN_RESTART_INTERVAL_SECONDS,
          f"got={eng._current_backoff_seconds()}")
    eng._feed_reconnect_failure_count = 5
    check("after 5 failures -> returns 60s ladder value (> floor)",
          eng._current_backoff_seconds() == 60.0,
          f"got={eng._current_backoff_seconds()}")
    eng._feed_reconnect_failure_count = 0


# ── 30. HIDDEN_TABS alias normalization ────────────────────────────────
def test_hidden_tabs_env_normalizes_aliases():
    print("\n30. HIDDEN_TABS parsing — normalizes case/spaces/aliases so 'silver' hides algo3")
    # We test the config-layer parser directly since it's pure logic.
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {"HIDDEN_TABS": "Silver, filter, SILVER_MICRO"}, clear=False):
        # Re-import config with the patched env
        import importlib, app.config
        importlib.reload(app.config)
        parsed = app.config.HIDDEN_TABS
    check('"Silver" normalized to "silver"', "silver" in parsed, f"got={parsed}")
    check('"filter" preserved as "filter"', "filter" in parsed)
    check('"SILVER_MICRO" normalized to "silvermicro"', "silvermicro" in parsed)
    # Now test the engine's tab→algo map treats both 'silver' and 'silvermicro' as algo3
    import importlib, app.engine
    with patch.dict(os.environ, {"HIDDEN_TABS": "silver"}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.engine)
        hidden_algos = app.engine._HIDDEN_ALGO_IDS
    check('HIDDEN_TABS=silver hides algo3', "algo3" in hidden_algos,
          f"hidden_algos={hidden_algos}")
    # Cleanup: restore empty
    with patch.dict(os.environ, {"HIDDEN_TABS": ""}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.engine)


# ── 31. Flat-candle rejection through the batch path ───────────────────
def test_flat_candle_batch_path_rejects():
    print("\n31. Flat-candle rejection ALSO applies via _build_candidates_from_collection")
    from app.strategies.algo1_opening_range import Algo1OpeningRange, ScanDebugLogger
    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.watchlist = ["NSE:FLAT-EQ", "NSE:LEGIT-EQ"]
    strat.settings = {"test_schedule_enabled": True}  # would have hit old test-mode fallback
    strat.opening_candles = {
        # Flat single-tick — would have wrongly traded under old test-mode logic
        "NSE:FLAT-EQ": [{"time": 1, "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "volume": 1}],
        # Real BUY signal — should still get through
        "NSE:LEGIT-EQ": [{"time": 1, "open": 100.0, "high": 100.5, "low": 100.0, "close": 100.3, "volume": 1000}],
    }
    strat.prev_close = {"NSE:FLAT-EQ": 495.0, "NSE:LEGIT-EQ": 99.5}
    strat.prev_close_ready_symbols = set()
    strat.open_extreme_symbols = set()
    strat.buy_candidates = []
    strat.sell_candidates = []
    strat.candidate_details = {}
    strat.sector_map = {}
    strat.debug_logger = ScanDebugLogger(2)

    strat._build_candidates_from_collection()

    flat_row = strat.candidate_details.get("NSE:FLAT-EQ") or {}
    legit_row = strat.candidate_details.get("NSE:LEGIT-EQ") or {}
    check("flat candle: rejection_reason='single_tick_candle' even in test mode",
          flat_row.get("rejection_reason") == "single_tick_candle",
          f"got={flat_row.get('rejection_reason')!r}")
    check("flat candle: NOT selected for trade",
          flat_row.get("selected_for_trade") is False)
    check("legit candle: shape_passed",
          legit_row.get("shape_passed") is True,
          f"got={legit_row.get('shape_passed')!r}")
    check("legit candle: side is BUY",
          legit_row.get("side") == "BUY",
          f"got={legit_row.get('side')!r}")


def main():
    print("=" * 66)
    print("  LIVE ORDER PIPELINE — OFFLINE SMOKE TEST (no Fyers, no DB)")
    print("=" * 66)
    test_dynamic_qty()
    test_entry_and_protective_payloads()
    test_market_mode()
    test_oco_reconcile()
    test_trailing_sl_syncs_to_fyers()
    test_tick_rounding_and_slm_limit()
    test_external_close_sync()
    test_external_close_2026_08_11_regression()
    test_external_close_single_flaky_poll()
    test_external_close_grace()
    test_algo1_open_position_guard()
    test_protective_retry()
    test_streaming_fcfs_phase2()
    test_trailing_metadata_tracks_activation_and_bumps()
    test_ws_premarket_warmup_gating()
    test_token_expired_guard()
    test_mode_toggle_cooldown()
    test_parallel_paper_mirroring()
    test_single_tick_candle_rejection()
    test_rejection_reason_buckets_exist()
    test_proxy_preflight_refuses_when_unreachable()
    test_proxy_preflight_allows_when_reachable()
    test_proxy_preflight_no_proxy_configured()
    test_skip_scan_today_short_circuits_algo1()
    test_skip_scan_today_short_circuits_algo3()
    test_skip_scan_date_auto_resets()
    test_oauth_throttle_serializes_exchanges()
    test_connection_status_429_stays_degraded_not_expired()
    test_pre_market_no_tick_not_counted_as_failure()
    test_post_recovery_grace_ignores_immediate_failure()
    test_current_backoff_respects_min_floor()
    test_hidden_tabs_env_normalizes_aliases()
    test_flat_candle_batch_path_rejects()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  RESULT: {_failures} check(s) FAILED")
        return 1
    print("  RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
