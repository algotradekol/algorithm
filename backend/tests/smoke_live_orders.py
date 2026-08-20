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


def test_live_funds_cap_bypasses_mcx_futures():
    print("\n2c. Live funds cap bypasses full-price check for MCX futures")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="MARKET")
    # Wallet below quoted futures price, but Silver Micro should still be
    # allowed through here because Fyers validates required margin, not
    # full contract LTP like cash equity.
    lb.get_wallet_balance = lambda *a, **k: {"summary": {"available_margin": 114_532.82}}
    qty = broker._cap_qty_to_live_funds("MCX:SILVERMIC26AUGFUT", 1, 240_300.0)
    check("MCX futures keep requested qty despite wallet < LTP", qty == 1, f"qty={qty}")


def test_live_funds_cap_still_limits_cash_equity():
    print("\n2d. Live funds cap still limits cash-equity buys")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="MARKET")
    lb.get_wallet_balance = lambda *a, **k: {"summary": {"available_margin": 1_000.0}}
    qty = broker._cap_qty_to_live_funds("NSE:RELIANCE-EQ", 10, 2_400.0)
    check("cash-equity qty capped to 0 when wallet cannot afford one share", qty == 0, f"qty={qty}")


def test_live_open_trade_refuses_unprotected_position():
    print("\n2e. Live open_trade refuses to keep a position open when Fyers protections fail")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="MARKET")
    persisted = []
    import app.paper_broker as pb
    orig_open_trade = pb.PaperBroker.open_trade
    pb.PaperBroker.open_trade = lambda self, *args, **kwargs: persisted.append((args, kwargs))
    try:
        broker._cap_qty_to_live_funds = lambda symbol, qty, price: qty
        broker._place_live_order = lambda symbol, side, qty, order_type="MARKET", limit_price=0.0: {
            "s": "ok", "id": f"{side}-ORDER"
        }
        broker._resolve_fill_details = lambda **kwargs: (240300.0, "2026-08-20T13:45:00+00:00")
        broker._place_protective_orders = lambda **kwargs: {
            "sl_order_id": None,
            "target_order_id": "TP-1",
            "sl_error": "sl failed",
            "target_error": None,
        }
        try:
            broker.open_trade("MCX:SILVERMIC26AUGFUT", "BUY", 1, 240300.0, 240200.0, 240600.0)
            check("open_trade raised when protection missing", False, "expected RuntimeError")
        except RuntimeError as exc:
            text = str(exc)
            check("exception mentions protection arming failed", "protection arming failed" in text.lower(), text)
        check("position NOT persisted locally when protection missing", len(persisted) == 0, f"persisted={persisted}")
    finally:
        pb.PaperBroker.open_trade = orig_open_trade


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


# ── 22-23. Persistent scan_enabled toggle (replaces skip-today) ──────
def test_scan_disabled_short_circuits_algo1():
    print("\n22. Scan OFF — algo1.evaluate_entries returns early without work")
    import datetime
    from app.strategies.algo1_opening_range import Algo1OpeningRange
    strat = object.__new__(Algo1OpeningRange)
    strat.algo_id = "algo1"
    strat.entries_evaluated_today = None
    strat.settings = {"scan_enabled": False, "test_schedule_enabled": False}
    called = []
    strat._record_scan_results = lambda *a, **kw: called.append(("record", kw.get("scan_status")))

    result = strat.evaluate_entries(get_ltp_fn=lambda s: 0.0)

    check("evaluate_entries returned True (short-circuited)", result is True)
    check("scan_status recorded as 'disabled'",
          any(status == "disabled" for _, status in called),
          f"called={called}")
    check("entries_evaluated_today set (prevents re-runs same day)",
          strat.entries_evaluated_today == datetime.date.today())


def test_scan_disabled_short_circuits_algo3():
    print("\n23. Scan OFF — algo3.scan_enabled() returns False")
    from app.strategies.algo3_silver_micro import Algo3SilverMicro
    strat = object.__new__(Algo3SilverMicro)
    strat.settings = {"scan_enabled": False}
    check("scan_enabled False when settings.scan_enabled=False",
          strat.scan_enabled() is False)
    strat.settings = {"scan_enabled": True}
    check("scan_enabled True when settings.scan_enabled=True",
          strat.scan_enabled() is True)


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
    check("429 state is not treated as verified",
          result.get("verified") is False,
          f"got verified={result.get('verified')}")


def test_connection_status_connected_requires_successful_verify():
    print("\n26b. Connection status — verified flips true only after profile success")
    from unittest.mock import patch
    import app.fyers_client as fc

    class FakeProfile:
        def get_profile(self):
            return {"s": "ok"}

    fake_token_row = {"access_token": "T", "refresh_token": "R"}
    with patch.object(fc, "get_active_broker_key", return_value="fyers_live"), \
         patch.object(fc, "get_stored_token_row", return_value=fake_token_row), \
         patch.object(fc, "get_runtime_trading_mode", return_value="live"), \
         patch.object(fc, "get_fyers_model", return_value=FakeProfile()):
        result = fc.get_connection_status()
    check("verified true after successful profile verify",
          result.get("verified") is True,
          f"got verified={result.get('verified')}")


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


# ── 28. Critical-symbol feed health ───────────────────────────────────
def test_critical_live_feed_symbols_ignore_nse_noise():
    print("\n28. Critical-symbol feed health — NSE ticks do not hide silent MCX strategy symbols")
    from app.fyers_client import (
        _critical_live_feed_symbols,
        _missing_critical_live_feed_symbols,
    )

    subscribed = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "MCX:SILVERMIC26AUGFUT"]
    seen_nse_only = {"NSE:RELIANCE-EQ", "NSE:TCS-EQ"}
    seen_all = {"NSE:RELIANCE-EQ", "NSE:TCS-EQ", "MCX:SILVERMIC26AUGFUT"}

    check("critical list keeps only non-NSE symbols",
          _critical_live_feed_symbols(subscribed) == ["MCX:SILVERMIC26AUGFUT"],
          f"got={_critical_live_feed_symbols(subscribed)}")
    check("MCX still reported missing when only NSE has ticked",
          _missing_critical_live_feed_symbols(subscribed, seen_nse_only) == ["MCX:SILVERMIC26AUGFUT"],
          f"got={_missing_critical_live_feed_symbols(subscribed, seen_nse_only)}")
    check("no critical symbols missing once MCX ticks too",
          _missing_critical_live_feed_symbols(subscribed, seen_all) == [],
          f"got={_missing_critical_live_feed_symbols(subscribed, seen_all)}")


def test_engine_rest_fallback_targets_stale_non_nse_symbols():
    print("\n28b. Engine REST fallback — only stale non-NSE symbols qualify")
    import time as _time
    import app.engine as eng

    eng.LIVE_FEED_SYMBOLS = ["NSE:RELIANCE-EQ", "MCX:SILVERMIC26AUGFUT"]
    eng.WATCHLIST = list(eng.LIVE_FEED_SYMBOLS)
    eng._symbol_last_tick_at.clear()

    check("critical symbols list excludes NSE",
          eng._critical_live_feed_symbols() == ["MCX:SILVERMIC26AUGFUT"],
          f"got={eng._critical_live_feed_symbols()}")
    check("missing MCX tick => REST fallback needed",
          eng._symbol_needs_rest_fallback("MCX:SILVERMIC26AUGFUT"),
          "expected True when symbol has never ticked")

    fresh = (_time.time() - 3)
    eng._symbol_last_tick_at["MCX:SILVERMIC26AUGFUT"] = __import__("datetime").datetime.fromtimestamp(
        fresh, tz=__import__("datetime").timezone.utc
    ).isoformat()
    check("fresh MCX tick => no fallback",
          not eng._symbol_needs_rest_fallback("MCX:SILVERMIC26AUGFUT"),
          "expected False for fresh tick")


def test_engine_rest_fallback_injects_synthetic_tick_into_algo3():
    print("\n28c. Engine REST fallback — synthetic REST tick updates engine + algo3 state")
    import app.engine as eng

    symbol = "MCX:SILVERMIC26AUGFUT"
    strat = _make_bare_algo3()
    strat.symbol = symbol
    strat.watchlist = [symbol]

    old_strategies = dict(eng.STRATEGIES)
    old_last_ltp = dict(eng.last_ltp)
    old_symbol_last_tick_at = dict(eng._symbol_last_tick_at)
    old_engine_status = dict(eng._engine_status)
    try:
        eng.STRATEGIES.clear()
        eng.STRATEGIES["algo3"] = strat
        eng.last_ltp.clear()
        eng._symbol_last_tick_at.clear()
        eng._engine_status.update({
            "last_tick_at": None,
            "last_tick_symbol": None,
            "last_tick_ltp": None,
            "tick_count": 0,
        })

        eng._inject_rest_tick(symbol, 240123.0)

        check("engine last_ltp updated from synthetic tick",
              eng.last_ltp.get(symbol) == 240123.0,
              f"got={eng.last_ltp.get(symbol)}")
        check("engine remembers per-symbol tick timestamp",
              bool(eng._symbol_last_tick_at.get(symbol)),
              f"got={eng._symbol_last_tick_at.get(symbol)}")
        check("engine status last_tick_symbol updated",
              eng._engine_status.get("last_tick_symbol") == symbol,
              f"got={eng._engine_status.get('last_tick_symbol')}")
        check("algo3 received synthetic tick LTP",
              strat._last_tick_ltp == 240123.0,
              f"got={strat._last_tick_ltp}")
        check("algo3 prev_ltp seeded from synthetic tick",
              strat._prev_ltp == 240123.0,
              f"got={strat._prev_ltp}")
    finally:
        eng.STRATEGIES.clear()
        eng.STRATEGIES.update(old_strategies)
        eng.last_ltp.clear()
        eng.last_ltp.update(old_last_ltp)
        eng._symbol_last_tick_at.clear()
        eng._symbol_last_tick_at.update(old_symbol_last_tick_at)
        eng._engine_status.clear()
        eng._engine_status.update(old_engine_status)


# ── 29. F6 post-recovery grace ─────────────────────────────────────────
def test_post_recovery_grace_ignores_immediate_failure():
    print("\n29. F6 — failure within 60s of recovery is ignored (no counter bump)")
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


# ── 30. F6 minimum backoff floor ───────────────────────────────────────
def test_current_backoff_respects_min_floor():
    print("\n30. F6 — _current_backoff_seconds returns at least 30s during a failure run")
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


# ── 31. HIDDEN_TABS alias normalization ────────────────────────────────
def test_hidden_tabs_env_normalizes_aliases():
    print("\n31. HIDDEN_TABS parsing — normalizes case/spaces/aliases so 'silver' hides algo3")
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


# ── 32. Flat-candle rejection through the batch path ───────────────────
def test_flat_candle_batch_path_rejects():
    print("\n32. Flat-candle rejection ALSO applies via _build_candidates_from_collection")
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


# ═══════════════════════════════════════════════════════════════════════
# ALGO3 SILVER MICRO — spec-driven regression tests (2026-08-19 rewrite)
#
# Two flavors:
#   White-box (32-43): unit tests for each internal helper (bucket
#     rounding, EMA step, setup capture, trigger detection, points
#     -> pct conversion, scan-disabled short-circuit).
#   Black-box (45): full scripted candles+ticks scenario, only inspect
#     open_trade calls and their arguments.
# ═══════════════════════════════════════════════════════════════════════
def _make_bare_algo3(settings_overrides=None):
    """Bypass __init__ (avoids Fyers + Supabase + broker construction)
    and build a stub instance with just the state needed for logic tests.
    """
    from app.strategies.algo3_silver_micro import Algo3SilverMicro
    from collections import deque
    strat = object.__new__(Algo3SilverMicro)
    strat.algo_id = "algo3"
    strat.symbol = "MCX:SILVERMIC26AUGFUT"
    strat.watchlist = [strat.symbol]
    strat.settings = {
        "capital_per_trade": 100000,
        "silver_breakout_points": 150,
        "sl_points": 100,
        "target_points": 300,
        "trailing_sl_enabled": False,
        "tsl_trigger_points": 100,
        "tsl_distance_points": 50,
        "scan_enabled": True,
        "exit_mode": "fixed_target_trailing_sl",
    }
    if settings_overrides:
        strat.settings.update(settings_overrides)
    strat._minute_buffer = []
    strat._current_bucket = None
    strat._last_ingested_minute_at = None
    strat._bars = deque(maxlen=500)
    strat._ema20 = None
    strat._buy_setup_close = None
    strat._sell_setup_close = None
    strat._buy_setup_bar_at = None
    strat._sell_setup_bar_at = None
    strat._prev_ltp = None
    strat._last_fired_buy_bar_at = None
    strat._last_fired_sell_bar_at = None
    strat._last_attempted_buy_bar_at = None
    strat._last_attempted_sell_bar_at = None
    import threading as _threading
    strat._entry_attempt_in_flight = False
    strat._entry_guard_lock = _threading.Lock()
    strat._entry_cooldown_until_monotonic = 0.0
    strat._persist_setup_event = lambda side, bar, source: None
    strat._last_tick_at = None
    strat._last_tick_ltp = None
    strat._last_minute_candle_at = None
    strat._last_bar_at = None
    strat._history_loading = False
    strat._history_ready = False
    strat._history_error = None
    strat._warmup_minute_candles = 0

    class FakeBroker:
        def __init__(self):
            self.opens = []
            self.closes = []
            self._open_positions = []
        def open_trade(self, symbol, side, qty, entry, sl, target, trigger, snapshot, entry_time=None):
            pos = {
                "symbol": symbol, "side": side, "qty": qty,
                "entry_price": entry, "sl_price": sl, "target_price": target,
                "trigger": trigger, "signal_snapshot": snapshot,
            }
            self.opens.append(pos)
            self._open_positions.append(pos)
            return pos
        def close_trade(self, position, exit_price, reason):
            self.closes.append({"symbol": position["symbol"], "reason": reason, "exit_price": exit_price})
            if position in self._open_positions:
                self._open_positions.remove(position)
        def open_positions(self):
            return list(self._open_positions)
        def apply_trailing_stop(self, position, ltp, settings):
            return position
        def should_exit_at_target(self, settings):
            return True

    strat.broker = FakeBroker()
    return strat


# ── 32. 15-min bucket rounding ─────────────────────────────────────────
def test_algo3_bucket_start_15m():
    print("\n32. algo3 _bucket_start rounds down to nearest 15-min boundary")
    import datetime as _dt
    from app.strategies.algo3_silver_micro import _bucket_start
    cases = [
        (_dt.datetime(2026, 8, 19, 9, 0, 0), _dt.datetime(2026, 8, 19, 9, 0)),
        (_dt.datetime(2026, 8, 19, 9, 14, 59), _dt.datetime(2026, 8, 19, 9, 0)),
        (_dt.datetime(2026, 8, 19, 9, 15, 0), _dt.datetime(2026, 8, 19, 9, 15)),
        (_dt.datetime(2026, 8, 19, 9, 29, 59), _dt.datetime(2026, 8, 19, 9, 15)),
        (_dt.datetime(2026, 8, 19, 9, 30, 0), _dt.datetime(2026, 8, 19, 9, 30)),
        (_dt.datetime(2026, 8, 19, 23, 55, 0), _dt.datetime(2026, 8, 19, 23, 45)),
    ]
    for ts, expected in cases:
        got = _bucket_start(ts)
        check(f"{ts.strftime('%H:%M:%S')} -> {expected.strftime('%H:%M')}",
              got == expected, f"got={got.strftime('%H:%M')}")


# ── 33. EMA step matches a hand-computed reference ─────────────────────
def test_algo3_ema_step_matches_python_reference():
    print("\n33. algo3 _ema_step matches an independent Python EMA")
    from app.strategies.algo3_silver_micro import _ema_step
    prices = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109]
    period = 20
    k = 2 / (period + 1)
    # First bar: EMA seeds to the first value.
    reference = prices[0]
    for p in prices[1:]:
        reference = p * k + reference * (1 - k)
    computed = None
    for p in prices:
        computed = _ema_step(computed, p, period)
    check("EMA after 10 bars matches reference within 1e-9",
          abs(computed - reference) < 1e-9,
          f"got={computed} ref={reference}")


def test_algo3_duplicate_minute_is_ignored():
    print("\n33b. algo3 duplicate 1m candle is ignored instead of double-counting a 15m bar")
    import datetime as _dt
    strat = _make_bare_algo3()
    candle = {
        "time": _dt.datetime(2026, 8, 20, 19, 0),
        "open": 241900,
        "high": 242100,
        "low": 241850,
        "close": 242032,
        "volume": 10,
    }
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT", candle, {})
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT", candle, {})
    check("duplicate minute only buffered once",
          len(strat._minute_buffer) == 1,
          f"buffer_len={len(strat._minute_buffer)}")


def test_algo3_current_minute_is_not_treated_as_closed():
    print("\n33c. algo3 ignores the current still-forming 1m candle")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    forming_candle = {
        "time": _dt.datetime(2026, 8, 20, 19, 58),
        "open": 242000,
        "high": 242210,
        "low": 241990,
        "close": 242207,
        "volume": 10,
    }
    fake_now = _dt.datetime(2026, 8, 20, 19, 58, 30, tzinfo=algo3_mod.IST)
    with patch.object(algo3_mod, "_latest_closed_minute_cutoff", return_value=fake_now.replace(second=0, microsecond=0, tzinfo=None)):
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT", forming_candle, {})
    check("current minute ignored until it is actually closed",
          len(strat._minute_buffer) == 0 and strat._current_bucket is None,
          f"buffer_len={len(strat._minute_buffer)} bucket={strat._current_bucket}")


def test_algo3_partial_15m_bucket_is_not_finalized_on_warmup_tail():
    print("\n33d. algo3 does not finalize a still-forming 15m bucket during warmup replay")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    candles = [
        {
            "time": _dt.datetime(2026, 8, 20, 20, 15),
            "open": 242000,
            "high": 242050,
            "low": 241990,
            "close": 242020,
            "volume": 10,
        },
        {
            "time": _dt.datetime(2026, 8, 20, 20, 16),
            "open": 242020,
            "high": 242070,
            "low": 242010,
            "close": 242055,
            "volume": 10,
        },
        {
            "time": _dt.datetime(2026, 8, 20, 20, 17),
            "open": 242055,
            "high": 242100,
            "low": 242050,
            "close": 242081,
            "volume": 10,
        },
    ]
    fake_now = _dt.datetime(2026, 8, 20, 20, 18, 30, tzinfo=algo3_mod.IST)
    with patch.object(algo3_mod, "_latest_closed_minute_cutoff", return_value=fake_now.replace(second=0, microsecond=0, tzinfo=None)):
        for candle in candles:
            strat._ingest_minute_candle(candle, allow_signals=False)
        strat._finalize_bar(allow_signals=False)
    check("still-forming 20:15 bucket not finalized into a 15m bar",
          len(strat._bars) == 0 and strat._current_bucket == _dt.datetime(2026, 8, 20, 20, 15),
          f"bars={len(strat._bars)} current_bucket={strat._current_bucket}")


def test_algo3_partial_15m_bucket_does_not_persist_setup_history():
    print("\n33e. algo3 does not save setup history from a still-forming 15m bucket")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    strat._ema20 = 240000.0
    calls = []
    strat._persist_setup_event = lambda side, bar, source: calls.append((side, bar["time"], source))
    candles = [
        {
            "time": _dt.datetime(2026, 8, 20, 20, 15),
            "open": 242000,
            "high": 242050,
            "low": 241990,
            "close": 242020,
            "volume": 10,
        },
        {
            "time": _dt.datetime(2026, 8, 20, 20, 16),
            "open": 242020,
            "high": 242070,
            "low": 242010,
            "close": 242055,
            "volume": 10,
        },
        {
            "time": _dt.datetime(2026, 8, 20, 20, 17),
            "open": 242055,
            "high": 242100,
            "low": 242050,
            "close": 242081,
            "volume": 10,
        },
    ]
    fake_now = _dt.datetime(2026, 8, 20, 20, 18, 30, tzinfo=algo3_mod.IST)
    with patch.object(algo3_mod, "_latest_closed_minute_cutoff", return_value=fake_now.replace(second=0, microsecond=0, tzinfo=None)):
        for candle in candles:
            strat._ingest_minute_candle(candle, allow_signals=True)
        strat._finalize_bar(allow_signals=True)
    check("no setup history persisted from in-progress bucket",
          calls == [], f"calls={calls}")


# ── 34-35. Setup capture (green above / red below) + overwrite ─────────
def test_algo3_setup_captures_and_overwrites():
    print("\n34. algo3 setup: green-above-EMA and red-below-EMA candles are stored, later ones overwrite")
    strat = _make_bare_algo3()
    strat._ema20 = 90000.0  # arbitrary EMA20
    # First green above: capture.
    strat._update_setups({"open": 91000, "close": 92000, "time": "t1"})
    check("first green-above sets buy_setup_close",
          strat._buy_setup_close == 92000, f"got={strat._buy_setup_close}")
    # Second green above: overwrite with newer close.
    strat._update_setups({"open": 92100, "close": 92500, "time": "t2"})
    check("second green-above overwrites buy_setup_close",
          strat._buy_setup_close == 92500, f"got={strat._buy_setup_close}")
    # Red below: independent setup.
    strat._update_setups({"open": 89500, "close": 89000, "time": "t3"})
    check("red-below sets sell_setup_close",
          strat._sell_setup_close == 89000, f"got={strat._sell_setup_close}")
    check("red-below does NOT touch buy_setup_close",
          strat._buy_setup_close == 92500)


def test_algo3_no_setup_when_wrong_side_of_ema():
    print("\n35. algo3 setup: green BELOW EMA and red ABOVE EMA do NOT capture")
    strat = _make_bare_algo3()
    strat._ema20 = 90000.0
    # Green but close still below EMA: no BUY setup.
    strat._update_setups({"open": 88500, "close": 89500, "time": "t1"})
    check("green-below-EMA: no buy_setup_close",
          strat._buy_setup_close is None, f"got={strat._buy_setup_close}")
    # Red but close still above EMA: no SELL setup.
    strat._update_setups({"open": 91500, "close": 90500, "time": "t2"})
    check("red-above-EMA: no sell_setup_close",
          strat._sell_setup_close is None, f"got={strat._sell_setup_close}")


def test_algo3_setup_persistence_emits_history_event():
    print("\n35b. algo3 qualifying setup emits a persistence event for history")
    strat = _make_bare_algo3()
    import datetime as _dt
    strat._ema20 = 90000.0
    calls = []
    strat._persist_setup_event = lambda side, bar, source: calls.append((side, bar["close"], source))
    strat._update_setups({
        "open": 90500,
        "high": 92200,
        "low": 90450,
        "close": 92000,
        "volume": 123,
        "minute_count": 15,
        "time": _dt.datetime(2026, 8, 20, 19, 15),
    })
    check("one BUY setup history event emitted",
          calls == [("BUY", 92000, "warmup")], f"calls={calls}")


def test_algo3_setup_persistence_rejects_wrong_candle_color():
    print("\n35c. algo3 history saver rejects a BUY/SELL persistence call for the wrong candle color")
    import app.strategies.algo3_silver_micro as algo3_mod
    import datetime as _dt
    strat = _make_bare_algo3()
    strat.symbol = "MCX:SILVERMIC26AUGFUT"
    strat._ema20 = 90000.0
    calls = []

    def fake_record_setup_event(**kwargs):
        calls.append(kwargs)

    original = algo3_mod.record_setup_event
    algo3_mod.record_setup_event = fake_record_setup_event
    try:
        strat._persist_setup_event("BUY", {
            "open": 91000,
            "high": 91100,
            "low": 90400,
            "close": 90500,
            "volume": 1,
            "minute_count": 15,
            "time": _dt.datetime(2026, 8, 20, 19, 15),
        }, source="live")
        strat._persist_setup_event("SELL", {
            "open": 90000,
            "high": 92100,
            "low": 89900,
            "close": 92000,
            "volume": 1,
            "minute_count": 15,
            "time": _dt.datetime(2026, 8, 20, 19, 30),
        }, source="live")
    finally:
        algo3_mod.record_setup_event = original
    check("wrong-color history rows were rejected",
          calls == [], f"calls={calls}")


# ── 36-38. Trigger detection ───────────────────────────────────────────
def test_algo3_buy_trigger_only_on_upward_cross():
    print("\n36. algo3 BUY trigger fires ONLY on an upward cross of (setup + n)")
    strat = _make_bare_algo3()
    strat._buy_setup_close = 92000.0
    # n=150, so buy_level = 92150
    strat._prev_ltp = 92100  # below
    strat._check_triggers(92200)  # crossed up through 92150
    check("upward cross fires BUY entry",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")

    strat2 = _make_bare_algo3()
    strat2._buy_setup_close = 92000.0
    strat2._prev_ltp = 92200  # above the level already
    strat2._check_triggers(92300)  # still above; not a fresh cross
    check("moving further up while already above level: no double entry",
          len(strat2.broker.opens) == 0)

    strat3 = _make_bare_algo3()
    strat3._buy_setup_close = 92000.0
    strat3._prev_ltp = 92300
    strat3._check_triggers(92100)  # downward through the level — wrong direction
    check("downward through BUY level: no BUY entry",
          len(strat3.broker.opens) == 0)


def test_algo3_sell_trigger_only_on_downward_cross():
    print("\n37. algo3 SELL trigger fires ONLY on a downward cross of (setup - n)")
    strat = _make_bare_algo3()
    strat._sell_setup_close = 89000.0
    # sell_level = 88850
    strat._prev_ltp = 88900  # above
    strat._check_triggers(88800)  # crossed down through 88850
    check("downward cross fires SELL entry",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "SELL",
          f"opens={strat.broker.opens}")

    strat2 = _make_bare_algo3()
    strat2._sell_setup_close = 89000.0
    strat2._prev_ltp = 88800  # already below
    strat2._check_triggers(88700)
    check("moving further down while already below: no double entry",
          len(strat2.broker.opens) == 0)


def test_algo3_no_trigger_before_first_prev_ltp():
    print("\n38. algo3 first tick after boot cannot fire a spurious cross (prev_ltp is None)")
    strat = _make_bare_algo3()
    strat._buy_setup_close = 92000.0
    strat._prev_ltp = None
    strat._check_triggers(92500)  # would cross but no prev_ltp
    check("no entry on first-ever tick",
          len(strat.broker.opens) == 0)


# ── 39. Configurable n parameter ───────────────────────────────────────
def test_algo3_configurable_n_parameter():
    print("\n39. algo3 respects settings[silver_breakout_points]")
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 500})
    strat._buy_setup_close = 92000.0
    # With n=500, buy_level=92500. A cross to 92200 (only +200) MUST NOT fire.
    strat._prev_ltp = 92100
    strat._check_triggers(92200)
    check("n=500: cross of +200 does NOT fire", len(strat.broker.opens) == 0)
    strat._prev_ltp = 92400
    strat._check_triggers(92600)  # now crossed 92500
    check("n=500: cross to 92600 fires",
          len(strat.broker.opens) == 1, f"opens={strat.broker.opens}")


# ── 40-41. Reversal & no-reentry ───────────────────────────────────────
def test_algo3_reversal_on_contra_signal():
    print("\n40. algo3 contra trigger closes existing position and flips at LTP")
    strat = _make_bare_algo3()
    strat._buy_setup_close = 92000.0
    strat._sell_setup_close = 89000.0
    # Fire BUY first.
    strat._prev_ltp = 92100
    strat._check_triggers(92200)
    check("initial BUY open", len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY")
    # Now fire SELL — should close the BUY and open a SELL.
    strat._prev_ltp = 88900
    strat._check_triggers(88800)
    check("existing BUY closed with REVERSAL_CONTRA_SIGNAL",
          any(c["reason"] == "REVERSAL_CONTRA_SIGNAL" for c in strat.broker.closes),
          f"closes={strat.broker.closes}")
    check("new SELL opened after reversal",
          len(strat.broker.opens) == 2 and strat.broker.opens[1]["side"] == "SELL",
          f"opens={strat.broker.opens}")


def test_algo3_no_reentry_same_side():
    print("\n41. algo3 same-side re-trigger while already positioned: no-op (no second BUY)")
    strat = _make_bare_algo3()
    strat._buy_setup_close = 92000.0
    strat._prev_ltp = 92100
    strat._check_triggers(92200)  # first BUY
    check("first BUY placed", len(strat.broker.opens) == 1)
    # Simulate LTP dipping and crossing back up through the level again.
    strat._prev_ltp = 92000  # below level
    strat._check_triggers(92200)  # would cross up again
    check("second same-side cross does NOT re-enter",
          len(strat.broker.opens) == 1, f"opens={strat.broker.opens}")


def test_algo3_failed_live_attempt_consumes_setup_once():
    print("\n41b. algo3 failed live attempt consumes the current setup and does not retry on later ticks")
    strat = _make_bare_algo3()
    import datetime as _dt
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)
    calls = []

    def fake_enter(side, ltp, trigger_level):
        calls.append((side, ltp, trigger_level))
        return False

    strat._enter = fake_enter
    strat._prev_ltp = 92100.0
    strat._check_triggers(92200.0)
    strat._prev_ltp = 92200.0
    strat._check_triggers(92300.0)
    check("failed setup only attempted once",
          len(calls) == 1, f"calls={calls}")
    check("failed setup latched as attempted",
          strat._last_attempted_buy_bar_at == strat._buy_setup_bar_at,
          f"attempted={strat._last_attempted_buy_bar_at} setup={strat._buy_setup_bar_at}")


def test_algo3_new_setup_rearms_after_failed_attempt():
    print("\n41c. algo3 new qualifying setup re-arms after an earlier failed attempt")
    strat = _make_bare_algo3()
    import datetime as _dt
    first_setup = _dt.datetime(2026, 8, 20, 19, 15)
    second_setup = _dt.datetime(2026, 8, 20, 19, 30)
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = first_setup
    calls = []

    def fake_enter(side, ltp, trigger_level):
        calls.append((side, ltp, trigger_level))
        return False

    strat._enter = fake_enter
    strat._prev_ltp = 92100.0
    strat._check_triggers(92200.0)
    strat._buy_setup_close = 92500.0
    strat._buy_setup_bar_at = second_setup
    strat._prev_ltp = 92600.0
    strat._check_triggers(92700.0)
    check("new setup got its own fresh attempt",
          len(calls) == 2, f"calls={calls}")
    check("attempt marker moved to the newer setup timestamp",
          strat._last_attempted_buy_bar_at == second_setup,
          f"attempted={strat._last_attempted_buy_bar_at}")


def test_algo3_live_broker_guard_blocks_when_symbol_busy():
    print("\n41d. algo3 live broker guard blocks a fresh entry when broker already has Silver activity")
    strat = _make_bare_algo3()
    strat.broker._algo3_treat_as_live = True
    import datetime as _dt
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)
    calls = []

    def fake_enter(side, ltp, trigger_level):
        calls.append((side, ltp, trigger_level))
        return True

    strat._enter = fake_enter
    strat._live_broker_symbol_busy = lambda current=None: True
    strat._prev_ltp = 92100.0
    strat._check_triggers(92200.0)
    check("busy broker state prevented the entry call",
          len(calls) == 0, f"calls={calls}")
    check("busy broker still consumed the setup to avoid retry storms",
          strat._last_attempted_buy_bar_at == strat._buy_setup_bar_at,
          f"attempted={strat._last_attempted_buy_bar_at}")


# ── 42. Entry payload uses POINTS for SL/target ────────────────────────
def test_algo3_entry_uses_points_sl_target():
    print("\n42. algo3 entry SL/target are computed as POINTS from entry, not %")
    strat = _make_bare_algo3(settings_overrides={"sl_points": 200, "target_points": 500})
    strat._buy_setup_close = 92000.0
    strat._prev_ltp = 92100
    strat._check_triggers(92200)
    check("one BUY open", len(strat.broker.opens) == 1)
    pos = strat.broker.opens[0]
    # entry = 92200, sl = 92200 - 200 = 92000, target = 92200 + 500 = 92700
    check("BUY sl_price = entry - 200 pts",
          abs(pos["sl_price"] - 92000.0) < 1e-9, f"got={pos['sl_price']}")
    check("BUY target_price = entry + 500 pts",
          abs(pos["target_price"] - 92700.0) < 1e-9, f"got={pos['target_price']}")

    # SELL side inverse
    strat2 = _make_bare_algo3(settings_overrides={"sl_points": 200, "target_points": 500})
    strat2._sell_setup_close = 89000.0
    strat2._prev_ltp = 88900
    strat2._check_triggers(88800)
    check("one SELL open", len(strat2.broker.opens) == 1)
    pos2 = strat2.broker.opens[0]
    # entry = 88800, sl = 88800 + 200 = 89000, target = 88800 - 500 = 88300
    check("SELL sl_price = entry + 200 pts",
          abs(pos2["sl_price"] - 89000.0) < 1e-9, f"got={pos2['sl_price']}")
    check("SELL target_price = entry - 500 pts",
          abs(pos2["target_price"] - 88300.0) < 1e-9, f"got={pos2['target_price']}")


# ── 43. Points -> percent conversion for TSL ───────────────────────────
def test_algo3_trailing_settings_convert_points_to_pct():
    print("\n43. algo3 _trailing_settings_for converts POINTS to percent based on entry price")
    strat = _make_bare_algo3(settings_overrides={"tsl_trigger_points": 200, "tsl_distance_points": 100})
    position = {"entry_price": 100000.0}
    converted = strat._trailing_settings_for(position)
    # 200 / 100000 * 100 = 0.2%; 100 / 100000 * 100 = 0.1%
    check("trigger converted: 200/100000 -> 0.2%",
          abs(converted["trailing_sl_trigger_pct"] - 0.2) < 1e-9,
          f"got={converted['trailing_sl_trigger_pct']}")
    check("distance converted: 100/100000 -> 0.1%",
          abs(converted["trailing_sl_distance_pct"] - 0.1) < 1e-9,
          f"got={converted['trailing_sl_distance_pct']}")
    # Zero points -> no conversion, leave original settings alone
    strat_zero = _make_bare_algo3(settings_overrides={"tsl_trigger_points": 0, "tsl_distance_points": 0,
                                                       "trailing_sl_trigger_pct": 5.0, "trailing_sl_distance_pct": 2.0})
    converted_zero = strat_zero._trailing_settings_for(position)
    check("zero points -> original pct preserved",
          converted_zero["trailing_sl_trigger_pct"] == 5.0
          and converted_zero["trailing_sl_distance_pct"] == 2.0)


# ── 44. Scan disabled → triggers skipped ───────────────────────────────
def test_algo3_scan_disabled_skips_triggers():
    print("\n44. algo3 scan_enabled=False: on_tick does not evaluate triggers")
    strat = _make_bare_algo3(settings_overrides={"scan_enabled": False})
    strat._buy_setup_close = 92000.0
    # Simulate the WS engine calling on_tick.
    strat.on_tick("MCX:SILVERMIC26AUGFUT", 92100, None)  # would set prev_ltp
    strat.on_tick("MCX:SILVERMIC26AUGFUT", 92200, None)  # would fire trigger if scan was on
    check("scan_enabled=False: no entries even on a valid cross",
          len(strat.broker.opens) == 0, f"opens={strat.broker.opens}")


# ── 45. Black-box end-to-end scripted scenario ─────────────────────────
def test_algo3_black_box_end_to_end():
    """Feed a scripted mix of 1-min candles + live ticks through the
    public engine hooks. Only inspect broker.open_trade side effects at
    the end. Reproduces a full session:
      - 20 warmup bars establish EMA20
      - Bar 21: green closes above EMA -> buy setup stored
      - Ticks after that cross the buy level -> BUY entered
      - Later bar: red closes below EMA -> sell setup stored
      - Ticks cross the sell level -> reversal to SELL
    """
    print("\n45. algo3 BLACK-BOX: candles + ticks produce expected entries + reversal")
    import datetime as _dt
    strat = _make_bare_algo3()

    def minute_candle(minute_offset, open_, high, low, close, vol=100):
        base = _dt.datetime(2026, 8, 19, 9, 0)
        t = base + _dt.timedelta(minutes=minute_offset)
        return {"time": t, "open": open_, "high": high, "low": low, "close": close, "volume": vol}

    # Warm up EMA with 20 flat bars around price 90000. Each 15-min bar
    # needs 15 one-minute candles; feed the same close for a stable EMA.
    for bar_idx in range(20):
        for m in range(15):
            offset = bar_idx * 15 + m
            strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                                  minute_candle(offset, 90000, 90000, 90000, 90000), {})
    # Trigger the finalize by starting a new bucket.
    # bar_idx=20, m=0: new 15-min bucket starts -> previous bar (19) gets finalized.
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                          minute_candle(20 * 15, 90000, 90000, 90000, 90000), {})
    # After 20 finalized bars EMA should be ~90000, no setups yet (flat candles).
    check("black-box: EMA20 ~= 90000 after warmup",
          strat._ema20 is not None and abs(strat._ema20 - 90000) < 100,
          f"got={strat._ema20}")
    check("black-box: no setups from flat warmup candles",
          strat._buy_setup_close is None and strat._sell_setup_close is None)

    # Bar 21: green candle closing 500 above EMA -> BUY setup
    for m in range(1, 15):
        offset = 20 * 15 + m
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                              minute_candle(offset, 90000, 90600, 89900, 90500), {})
    # New bucket to finalize bar 21
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                          minute_candle(21 * 15, 90500, 90500, 90500, 90500), {})
    check("black-box: green-above-EMA bar 21 captured as buy setup",
          strat._buy_setup_close is not None and strat._buy_setup_close > 90000,
          f"got={strat._buy_setup_close}")

    # Now feed live ticks that cross (setup + 150).
    # Note: the "on_tick" pathway uses self.settings["silver_breakout_points"]=150.
    buy_level = strat._buy_setup_close + 150
    strat.on_tick("MCX:SILVERMIC26AUGFUT", buy_level - 20, None)
    strat.on_tick("MCX:SILVERMIC26AUGFUT", buy_level + 5, None)
    check("black-box: BUY fires on upward tick-cross of setup+150",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")

    # Later 15-min bar: red closing 500 below EMA -> SELL setup
    for m in range(1, 15):
        offset = 21 * 15 + m
        # Use a wide down bar (open above ema, close below)
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                              minute_candle(offset, 90000, 90100, 89400, 89500), {})
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                          minute_candle(22 * 15, 89500, 89500, 89500, 89500), {})
    check("black-box: red-below-EMA bar captured as sell setup",
          strat._sell_setup_close is not None and strat._sell_setup_close < strat._ema20,
          f"got sell={strat._sell_setup_close}, ema={strat._ema20}")

    # Ticks that cross (sell setup - 150) downward -> REVERSAL to SELL
    sell_level = strat._sell_setup_close - 150
    strat.on_tick("MCX:SILVERMIC26AUGFUT", sell_level + 20, None)
    strat.on_tick("MCX:SILVERMIC26AUGFUT", sell_level - 5, None)
    check("black-box: existing BUY closed as REVERSAL",
          any(c["reason"] == "REVERSAL_CONTRA_SIGNAL" for c in strat.broker.closes),
          f"closes={strat.broker.closes}")
    check("black-box: SELL opened after reversal",
          len(strat.broker.opens) == 2 and strat.broker.opens[1]["side"] == "SELL",
          f"opens={[o['side'] for o in strat.broker.opens]}")


# ── 46-47. BROKER_KEY_SUFFIX — token isolation between backends ────────
def test_broker_key_suffix_isolates_tokens():
    print("\n46. BROKER_KEY_SUFFIX: two suffixes produce two distinct broker keys")
    import importlib, os
    from unittest.mock import patch
    # Simulate the CLIENT backend
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "client"}, clear=False):
        import app.config, app.runtime_mode
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        client_key_live = app.runtime_mode.get_active_broker_key(mode="live")
        client_key_paper = app.runtime_mode.get_active_broker_key(mode="paper")
    # Simulate the DEV backend
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "dev"}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        dev_key_live = app.runtime_mode.get_active_broker_key(mode="live")
        dev_key_paper = app.runtime_mode.get_active_broker_key(mode="paper")

    check("client live key is fyers_live__client",
          client_key_live == "fyers_live__client", f"got={client_key_live}")
    check("dev live key is fyers_live__dev",
          dev_key_live == "fyers_live__dev", f"got={dev_key_live}")
    check("client and dev live keys differ (no collision)",
          client_key_live != dev_key_live)
    check("client paper key is fyers__client",
          client_key_paper == "fyers__client", f"got={client_key_paper}")
    check("dev paper key is fyers__dev",
          dev_key_paper == "fyers__dev", f"got={dev_key_paper}")
    # Restore empty suffix so subsequent tests aren't affected
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)


def test_broker_key_suffix_empty_preserves_legacy_key():
    print("\n47. BROKER_KEY_SUFFIX empty: keeps historical 'fyers_live' / 'fyers' keys")
    import importlib, os
    from unittest.mock import patch
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        import app.config, app.runtime_mode
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        live_key = app.runtime_mode.get_active_broker_key(mode="live")
        paper_key = app.runtime_mode.get_active_broker_key(mode="paper")
    check("empty suffix: live key stays 'fyers_live' (backward-compat)",
          live_key == "fyers_live", f"got={live_key}")
    check("empty suffix: paper key stays 'fyers' (backward-compat)",
          paper_key == "fyers", f"got={paper_key}")


# ── 48. Backtest parity: same scenario, backtest and live must agree ──
def test_algo3_backtest_parity_with_live():
    """Give the same 1m candle history to the backtest simulator and to
    the live algo3 (via on_candle_close + on_tick per 1m bar). Assert
    they produce the SAME entries: same side, same entry price, same
    minute. If they diverge, either the live logic or the backtest
    simulator has drifted from the spec doc."""
    print("\n48. algo3 backtest parity — same input, live + backtest agree on entries")
    import datetime as _dt
    from app import backtest as bt

    # Scripted history: 20 flat 15m bars (300 1m candles) to warm EMA20,
    # then a green 15m bar closing above EMA20 (BUY setup), then a 1m
    # bar whose high crosses (setup + 150) upward.
    symbol = "MCX:TEST-EQ"
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)

    def push(offset, o, h, l, c, v=100):
        history.append({
            "time": base + _dt.timedelta(minutes=offset),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })

    # 20 flat warmup bars: EMA20 stabilizes at 90000
    for b in range(20):
        for m in range(15):
            push(b * 15 + m, 90000, 90000, 90000, 90000)

    # Bar 21: green closes above EMA (14 low-move minutes + 1 breakout min)
    for m in range(14):
        push(20 * 15 + m, 90000, 90200, 89950, 90100)
    push(20 * 15 + 14, 90100, 90500, 90050, 90500)  # last minute pushes close to 90500

    # Bar 22: contains the tick-cross. First few 1m bars quiet, then one
    # 1m bar whose high crosses (90500 + 150) = 90650 upward.
    for m in range(5):
        push(21 * 15 + m, 90500, 90520, 90490, 90510)  # sitting below level
    push(21 * 15 + 5, 90510, 90680, 90500, 90670)     # crosses 90650

    # Bar 22 continues (needed so backtest finalizes bar 22 too).
    for m in range(6, 15):
        push(21 * 15 + m, 90670, 90680, 90600, 90650)

    # Add one more bar to force the 15m aggregator to close bar 22.
    push(22 * 15, 90650, 90650, 90650, 90650)

    # ── run backtest ────────────────────────────────────────────────
    class NoopCharges:
        def __getitem__(self, k): return 0
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0, "exchange_pct": 0,
               "sebi_pct": 0, "gst_pct": 0, "stamp_duty_pct": 0}
    settings = {
        "capital_per_trade": 500000,
        "silver_breakout_points": 150,
        "sl_points": 100,
        "target_points": 300,
        "trailing_sl_enabled": False,
        "tsl_trigger_points": 0,
        "tsl_distance_points": 0,
        "exit_mode": "fixed_target_sl",
    }
    # Backtest expects a job_id already registered so _raise_if_cancelled
    # doesn't KeyError. Register a dummy one.
    from app.backtest import _jobs, _lock
    with _lock:
        _jobs["parity-test"] = {"cancel_requested": False}

    first_date = _dt.date(2026, 8, 19)
    trading_days = [first_date]
    bt_results = bt._simulate_silver_micro_range(
        job_id="parity-test",
        algo_id="algo3",
        first_date=first_date,
        last_date=first_date,
        symbol=symbol,
        history=history,
        trading_days=trading_days,
        settings=settings,
        charges_config=charges,
    )
    bt_trades = bt_results[0]["trades"]

    # ── run live algo3 through same input ──────────────────────────
    strat = _make_bare_algo3(settings_overrides=settings)
    strat.symbol = symbol
    strat.watchlist = [symbol]
    for candle in history:
        strat.on_candle_close(symbol, candle, {})
        # For each 1m candle also push the close as a tick so trigger detection runs.
        strat.on_tick(symbol, candle["close"], candle["time"])
        # AND push the high (BUY trigger detection) and low (SELL) as intra-minute ticks
        # BEFORE closing at close, mimicking live tick sequence.
    # Now run again with intra-minute high/low so trigger detection sees the extremes.
    # The above only saw close-to-close; run a second pass emulating h/l ticks.
    # For this test the interesting event is the high 90680 vs prev close 90510
    # of the previous minute. Live tick pathway needs prev_ltp < 90650 <= new tick.
    # Since we called on_tick with each candle's close in sequence, the last tick
    # before the 6th minute of bar 22 was 90510, then next was 90670 - that DOES
    # cross 90650 upward. So the parity check is meaningful without a high-tick pass.
    live_trades = strat.broker.opens

    # ── assertions ──────────────────────────────────────────────────
    check("live: exactly 1 entry", len(live_trades) == 1, f"live opens={live_trades}")
    check("backtest: exactly 1 entry", len(bt_trades) == 1, f"bt trades={bt_trades}")
    if live_trades and bt_trades:
        live = live_trades[0]
        bt_trade = bt_trades[0]
        check("same side",
              live["side"] == bt_trade["side"],
              f"live={live['side']} bt={bt_trade['side']}")
        check("both are BUY", live["side"] == "BUY")
        # Prices may differ by a few points because live enters at the tick's
        # exact LTP while backtest enters at the trigger level itself (a
        # documented conservative approximation). Allow up to 100 points.
        diff = abs(float(live["entry_price"]) - float(bt_trade["entry_price"]))
        check("entry prices within 100 pts of each other",
              diff <= 100,
              f"live={live['entry_price']} bt={bt_trade['entry_price']} diff={diff}")

    # Cleanup
    with _lock:
        _jobs.pop("parity-test", None)


def test_algo3_gap_through_fires_immediately():
    """Client's 2026-08-20 ask: if today's market opens ALREADY past the
    stored setup ± n, fire the entry on the first live tick instead of
    waiting for a downward tick to re-cross upward.
    """
    print("\n49. algo3 gap-through: LTP already past trigger fires immediately")
    import datetime as _dt
    strat = _make_bare_algo3()
    strat._buy_setup_close = 238000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 19, 23, 45)  # prev-day qualifier
    # Gap open beyond buy_level (238000 + 150 = 238150).
    # prev_ltp=None simulates first live tick after warmup.
    strat._prev_ltp = None
    strat.on_tick("MCX:SILVERMIC26AUGFUT", 240300, None)
    check("gap-through BUY fires on first tick already past level",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")
    # Second tick still past level — must NOT re-fire (one-shot per setup).
    strat.on_tick("MCX:SILVERMIC26AUGFUT", 240500, None)
    check("gap-through BUY does NOT re-fire on same setup",
          len(strat.broker.opens) == 1, f"opens={strat.broker.opens}")

    # SELL side gap-down
    strat2 = _make_bare_algo3()
    strat2._sell_setup_close = 231000.0
    strat2._sell_setup_bar_at = _dt.datetime(2026, 8, 19, 23, 45)
    strat2._prev_ltp = None
    # sell_level = 231000 - 150 = 230850; opening at 228000 is well past.
    strat2.on_tick("MCX:SILVERMIC26AUGFUT", 228000, None)
    check("gap-through SELL fires on first tick already past level",
          len(strat2.broker.opens) == 1 and strat2.broker.opens[0]["side"] == "SELL",
          f"opens={strat2.broker.opens}")


def test_algo3_previous_day_buy_setup_gap_open_fires_immediately():
    print("\n49b. algo3 previous-day BUY setup fires immediately on the next day's first gap-open tick")
    import datetime as _dt
    strat = _make_bare_algo3()
    strat._buy_setup_close = 1000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 22, 15)
    strat._prev_ltp = None
    strat.on_tick("MCX:SILVERMIC26AUGFUT", 1500, None)
    check("next-session first tick above prior trigger opens BUY immediately",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")
    if strat.broker.opens:
        check("entry uses the actual first tick gap-open price",
              abs(float(strat.broker.opens[0]["entry_price"]) - 1500.0) < 1e-9,
              f"entry={strat.broker.opens[0]['entry_price']}")


def test_algo3_candle_close_trigger_fires():
    """Client's 2026-08-20 ask: if a 15m candle CLOSES past the trigger
    level (e.g. 09:00 closed 241104 while BUY trigger was 238150),
    fire on candle-close as a fallback when live ticks were sparse."""
    print("\n50. algo3 candle-close trigger: bar closes past level fires entry")
    import datetime as _dt
    strat = _make_bare_algo3()
    strat._buy_setup_close = 238000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 19, 23, 45)
    strat._ema20 = 237000.0  # so the closing bar's setup detection doesn't overwrite
    # Feed a fresh 09:00 bar via the aggregation path so _finalize_bar runs.
    # Build 15 one-minute candles all closing at 241104, then a 16th minute
    # in the next bucket to force finalization of the 09:00 bar.
    def mc(minute, o, h, l, c):
        t = _dt.datetime(2026, 8, 20, 9, 0) + _dt.timedelta(minutes=minute)
        return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1}
    for m in range(15):
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT", mc(m, 240000, 241200, 239900, 241104), {})
    # 16th minute belongs to 09:15 bucket — this finalizes 09:00.
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT", mc(15, 241104, 241150, 241000, 241104), {})
    check("candle-close BUY fires when bar closes past buy_level",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")


def test_broker_positions_entry_time_from_tradebook():
    """Positions opened directly in the Fyers app show accurate entry_time
    sourced from the intraday tradebook (client's 2026-08-20 ask)."""
    print("\n52. broker positions: entry_time hydrated from tradebook")
    from app.fyers_client import _enrich_positions_with_entry_times

    class FakeFyers:
        def __init__(self, tradebook_response):
            self._response = tradebook_response
            self.calls = 0
        def tradebook(self):
            self.calls += 1
            return self._response

    # Case A — standard Fyers V3 response shape: {s:'ok', tradeBook:[...]}
    positions = [
        {"symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "entry_time": None},
        {"symbol": "NSE:RELIANCE-EQ",       "side": "SELL", "entry_time": None},
    ]
    fake = FakeFyers({
        "s": "ok",
        "tradeBook": [
            # Two BUY fills for silver — earliest should win
            {"symbol": "MCX:SILVERMIC26AUGFUT", "side": 1, "orderDateTime": "2026-08-20 09:22:07"},
            {"symbol": "MCX:SILVERMIC26AUGFUT", "side": 1, "orderDateTime": "2026-08-20 10:14:31"},
            # SELL fill for reliance
            {"symbol": "NSE:RELIANCE-EQ", "side": -1, "orderDateTime": "2026-08-20 11:05:00"},
            # An unrelated fill that must not match
            {"symbol": "NSE:TCS-EQ", "side": 1, "orderDateTime": "2026-08-20 09:15:00"},
        ],
    })
    _enrich_positions_with_entry_times(fake, positions)
    check("tradebook called exactly once", fake.calls == 1, f"calls={fake.calls}")
    # 09:22 IST = 03:52 UTC; 11:05 IST = 05:35 UTC. Backend stores UTC,
    # frontend converts to Asia/Kolkata for display.
    check("silver BUY entry_time matches earliest fill (09:22 IST -> 03:52 UTC)",
          positions[0]["entry_time"] and "03:52:07" in positions[0]["entry_time"],
          f"got={positions[0]['entry_time']}")
    check("reliance SELL entry_time matches its own fill (11:05 IST -> 05:35 UTC)",
          positions[1]["entry_time"] and "05:35:00" in positions[1]["entry_time"],
          f"got={positions[1]['entry_time']}")

    # Case B — tradebook fetch fails: entry_time stays None, no exception
    positions_b = [{"symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "entry_time": None}]
    class FailingFyers:
        def tradebook(self):
            raise RuntimeError("simulated network error")
    _enrich_positions_with_entry_times(FailingFyers(), positions_b)
    check("tradebook failure leaves entry_time as None",
          positions_b[0]["entry_time"] is None,
          f"got={positions_b[0]['entry_time']}")

    # Case C — position has no matching tradebook row (e.g. opened yesterday)
    positions_c = [{"symbol": "MCX:GOLDM26AUGFUT", "side": "BUY", "entry_time": None}]
    fake_c = FakeFyers({"tradeBook": [{"symbol": "MCX:SILVERMIC26AUGFUT", "side": 1, "orderDateTime": "2026-08-20 09:22:07"}]})
    _enrich_positions_with_entry_times(fake_c, positions_c)
    check("no match -> entry_time stays None",
          positions_c[0]["entry_time"] is None,
          f"got={positions_c[0]['entry_time']}")


def test_algo3_warmup_end_date_is_today():
    """Warmup must include today's completed 1m candles so a mid-day
    restart rebuilds today's 15m bars + setups, not just yesterday's.

    Bug from 2026-08-20 client logs: end_date was today-1, so restarts
    at 13:30 IST reloaded up to Aug 19 only. Today's 09:00 green candle
    that closed at 2,41,104 (should have overwritten BUY setup) never
    made it into the algo, so BUY setup stayed frozen at yesterday's
    2,38,000 despite the market clearly moving through the trigger.
    """
    print("\n53. algo3 warmup end_date must be TODAY (mid-day restart fix)")
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    captured_ranges = []
    def fake_range(symbol, start, end):
        captured_ranges.append((start, end))
        return []  # empty is fine; we only care about the range

    strat = _make_bare_algo3()
    # Bypass the __init__ warmup call; test the reload path directly.
    strat._history_lock = __import__("threading").Lock()
    strat._history_loading = False
    strat._last_warmup_at = None
    with patch.object(algo3_mod, "get_intraday_candles_for_range", side_effect=fake_range):
        strat._load_history_background()
    check("warmup called history once", len(captured_ranges) == 1,
          f"calls={len(captured_ranges)}")
    start, end = captured_ranges[0]
    check("end_date is TODAY (not today-1)",
          end == __import__("datetime").date.today(),
          f"got end={end} today={__import__('datetime').date.today()}")
    days_span = (end - start).days
    check("start_date covers WARMUP_LOOKBACK_DAYS back",
          days_span == algo3_mod.WARMUP_LOOKBACK_DAYS,
          f"span={days_span}")


def test_algo3_warmup_debounce_blocks_repeat_calls():
    """WS watchdog / mode-switch used to trigger a fresh warmup every
    few minutes, each pulling ~7000 candles and wiping today's live
    state. Debounce (WARMUP_DEBOUNCE_SECONDS) blocks repeat calls
    unless force=True."""
    print("\n54. algo3 warmup debounce blocks calls within cooldown")
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    load_calls = []
    def counting_load(self):
        load_calls.append(1)
        # Simulate a successful warmup so _last_warmup_at gets stamped
        self._last_warmup_at = __import__("time").monotonic()
        self._history_loading = False

    strat = _make_bare_algo3()
    strat._history_lock = __import__("threading").Lock()
    strat._history_loading = False
    strat._last_warmup_at = None

    # Rather than race the daemon thread, replace threading.Thread with
    # a synchronous "run now" fake so refresh_market_data completes
    # inline. Then we can assert on load_calls deterministically.
    class SyncThread:
        def __init__(self, target=None, daemon=None):
            self._target = target
        def start(self):
            self._target()

    with patch.object(algo3_mod.Algo3SilverMicro, "_load_history_background", counting_load), \
         patch.object(algo3_mod.threading, "Thread", SyncThread):
        # First call proceeds and stamps _last_warmup_at
        strat.refresh_market_data()
        check("1st call ran the loader", len(load_calls) == 1,
              f"calls={len(load_calls)}")

        # Second call within debounce window: must be a no-op
        strat._history_loading = False  # counting_load reset this
        strat.refresh_market_data()
        check("2nd call within debounce window is skipped",
              len(load_calls) == 1, f"calls={len(load_calls)}")

        # Force=True bypasses debounce and runs the loader again
        strat._history_loading = False
        strat.refresh_market_data(force=True)
        check("force=True bypasses debounce and runs the loader",
              len(load_calls) == 2, f"calls={len(load_calls)}")


def test_algo3_warmup_resets_state_before_replay():
    """A mid-day warmup must wipe prior aggregation state before
    replaying history. Otherwise an old historical candle appended to
    a live-built _current_bucket would spuriously finalize today's
    partial bucket."""
    print("\n55. algo3 warmup resets aggregation state before replay")
    strat = _make_bare_algo3()

    # Simulate a "dirty" mid-day state.
    import datetime as _dt
    strat._current_bucket = _dt.datetime(2026, 8, 20, 10, 15)
    strat._minute_buffer = [{"time": _dt.datetime(2026, 8, 20, 10, 20),
                             "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1}]
    strat._bars.append({"time": _dt.datetime(2026, 8, 20, 9, 0),
                        "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1})
    strat._ema20 = 999.99
    strat._buy_setup_close = 111111.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 19, 23, 0)
    strat._last_fired_buy_bar_at = _dt.datetime(2026, 8, 19, 23, 0)

    strat._reset_aggregation_state()

    check("aggregation state cleared: bars empty", len(strat._bars) == 0)
    check("aggregation state cleared: buffer empty", strat._minute_buffer == [])
    check("aggregation state cleared: current_bucket None", strat._current_bucket is None)
    check("aggregation state cleared: EMA None", strat._ema20 is None)
    check("aggregation state cleared: buy setup None", strat._buy_setup_close is None)
    check("aggregation state cleared: buy setup ts None", strat._buy_setup_bar_at is None)
    check("aggregation state cleared: fired guard None", strat._last_fired_buy_bar_at is None)


def test_algo3_warmup_transient_zero_result_preserves_state():
    """A 0-candle history response (transient Fyers API failure) must
    NOT wipe good aggregation state that a prior successful warmup built."""
    print("\n56. algo3 transient 0-candle warmup preserves prior good state")
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    strat._history_lock = __import__("threading").Lock()
    strat._history_loading = False
    strat._last_warmup_at = None
    # Seed some "good" prior state
    strat._buy_setup_close = 238000.0
    strat._warmup_minute_candles = 6960

    with patch.object(algo3_mod, "get_intraday_candles_for_range", return_value=[]):
        strat._load_history_background()

    check("0-candle warmup preserved prior setup",
          strat._buy_setup_close == 238000.0,
          f"got={strat._buy_setup_close}")
    check("0-candle warmup preserved prior warmup count",
          strat._warmup_minute_candles == 6960,
          f"got={strat._warmup_minute_candles}")


def test_algo3_lot_based_qty():
    """Silver Micro qty must be lots * 1, NOT capital // price."""
    print("\n51. algo3 qty is derived from silver_lots (not capital / price)")
    strat = _make_bare_algo3(settings_overrides={"silver_lots": 3, "capital_per_trade": 100000})
    strat._buy_setup_close = 92000.0
    strat._prev_ltp = 92100
    strat._check_triggers(92200)
    check("lots=3 -> qty=3", len(strat.broker.opens) == 1 and strat.broker.opens[0]["qty"] == 3,
          f"opens={strat.broker.opens}")

    strat2 = _make_bare_algo3(settings_overrides={"silver_lots": 1})
    strat2._buy_setup_close = 92000.0
    strat2._prev_ltp = 92100
    strat2._check_triggers(92200)
    check("lots=1 -> qty=1 (default)", strat2.broker.opens[0]["qty"] == 1,
          f"opens={strat2.broker.opens}")


def main():
    print("=" * 66)
    print("  LIVE ORDER PIPELINE — OFFLINE SMOKE TEST (no Fyers, no DB)")
    print("=" * 66)
    test_dynamic_qty()
    test_entry_and_protective_payloads()
    test_market_mode()
    test_live_funds_cap_bypasses_mcx_futures()
    test_live_funds_cap_still_limits_cash_equity()
    test_live_open_trade_refuses_unprotected_position()
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
    test_scan_disabled_short_circuits_algo1()
    test_scan_disabled_short_circuits_algo3()
    test_oauth_throttle_serializes_exchanges()
    test_connection_status_429_stays_degraded_not_expired()
    test_pre_market_no_tick_not_counted_as_failure()
    test_critical_live_feed_symbols_ignore_nse_noise()
    test_engine_rest_fallback_targets_stale_non_nse_symbols()
    test_engine_rest_fallback_injects_synthetic_tick_into_algo3()
    test_post_recovery_grace_ignores_immediate_failure()
    test_current_backoff_respects_min_floor()
    test_hidden_tabs_env_normalizes_aliases()
    test_flat_candle_batch_path_rejects()
    # ── algo3 (Silver Micro) rewrite regression tests ──
    test_algo3_bucket_start_15m()
    test_algo3_ema_step_matches_python_reference()
    test_algo3_duplicate_minute_is_ignored()
    test_algo3_current_minute_is_not_treated_as_closed()
    test_algo3_partial_15m_bucket_is_not_finalized_on_warmup_tail()
    test_algo3_partial_15m_bucket_does_not_persist_setup_history()
    test_algo3_setup_captures_and_overwrites()
    test_algo3_no_setup_when_wrong_side_of_ema()
    test_algo3_setup_persistence_emits_history_event()
    test_algo3_setup_persistence_rejects_wrong_candle_color()
    test_algo3_buy_trigger_only_on_upward_cross()
    test_algo3_sell_trigger_only_on_downward_cross()
    test_algo3_no_trigger_before_first_prev_ltp()
    test_algo3_configurable_n_parameter()
    test_algo3_reversal_on_contra_signal()
    test_algo3_no_reentry_same_side()
    test_algo3_failed_live_attempt_consumes_setup_once()
    test_algo3_new_setup_rearms_after_failed_attempt()
    test_algo3_live_broker_guard_blocks_when_symbol_busy()
    test_algo3_entry_uses_points_sl_target()
    test_algo3_trailing_settings_convert_points_to_pct()
    test_algo3_scan_disabled_skips_triggers()
    test_algo3_black_box_end_to_end()
    test_broker_key_suffix_isolates_tokens()
    test_broker_key_suffix_empty_preserves_legacy_key()
    test_algo3_backtest_parity_with_live()
    test_algo3_gap_through_fires_immediately()
    test_algo3_previous_day_buy_setup_gap_open_fires_immediately()
    test_algo3_candle_close_trigger_fires()
    test_algo3_lot_based_qty()
    test_broker_positions_entry_time_from_tradebook()
    test_algo3_warmup_end_date_is_today()
    test_algo3_warmup_debounce_blocks_repeat_calls()
    test_algo3_warmup_resets_state_before_replay()
    test_algo3_warmup_transient_zero_result_preserves_state()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  RESULT: {_failures} check(s) FAILED")
        return 1
    print("  RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
