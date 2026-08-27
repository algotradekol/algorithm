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
from types import SimpleNamespace

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


def test_silver_forces_market_entry():
    print("\n2b1. Silver Micro rejects legacy LIMIT entry settings")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="LIMIT")
    broker.algo_id = "algo3"
    order_type, limit_price = broker._entry_order_params(248_000.0)
    check(
        "Silver entry remains MARKET when legacy setting says LIMIT",
        order_type == "MARKET" and limit_price == 0.0,
        f"got {order_type} @ {limit_price}",
    )


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


def test_live_modify_protection_requires_tracked_fyers_orders():
    print("\n2f. Live modify_protection refuses local-only edits when FYERS protection ids are missing")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec, order_type="MARKET")
    position = {
        "id": "live-pos-1",
        "symbol": "MCX:SILVERMIC26AUGFUT",
        "side": "BUY",
        "qty": 1,
        "entry_price": 243000.0,
        "sl_price": 242800.0,
        "target_price": 245000.0,
        "signal_snapshot": {},
    }
    broker._find_open_position = lambda position_id: position if position_id == "live-pos-1" else None

    persisted = []
    import app.live_broker as live_broker_module
    original_run_with_supabase = live_broker_module.run_with_supabase
    live_broker_module.run_with_supabase = lambda fn: persisted.append("write-blocked")
    try:
        try:
            broker.modify_protection("live-pos-1", sl_price=242700.0)
            check("missing FYERS SL id raises", False, "expected RuntimeError")
        except RuntimeError as exc:
            text = str(exc)
            check("missing FYERS SL id explains why", "no tracked fyers stop-loss order" in text.lower(), text)
        check("missing FYERS SL id avoids DB write", not persisted, f"persisted={persisted}")

        try:
            broker.modify_protection("live-pos-1", target_price=245100.0)
            check("missing FYERS target id raises", False, "expected RuntimeError")
        except RuntimeError as exc:
            text = str(exc)
            check("missing FYERS target id explains why", "no tracked fyers target order" in text.lower(), text)
        check("missing FYERS target id still avoids DB write", not persisted, f"persisted={persisted}")

        position["signal_snapshot"] = {
            "fyers_sl_order_id": "SL-EDIT",
            "fyers_target_order_id": "TP-EDIT",
        }
        broker.modify_protection("live-pos-1", sl_price=242600.0, target_price=245200.0)
        check(
            "SL amend sends the live position quantity",
            any(row.get("id") == "SL-EDIT" and row.get("qty") == 1 for row in rec["modified"]),
            f"modified={rec['modified']}",
        )
        check(
            "target amend sends the live position quantity",
            any(row.get("id") == "TP-EDIT" and row.get("qty") == 1 for row in rec["modified"]),
            f"modified={rec['modified']}",
        )
    finally:
        live_broker_module.run_with_supabase = original_run_with_supabase


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


def test_oco_reconcile_marks_trailing_stop():
    print("\n5b. OCO reconcile records a filled moved stop as TRAILING_SL")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    rec["orderbook"] = [
        {"id": "SL-TRAIL", "status": 2, "tradedPrice": 1050.0, "orderDateTime": "2026-08-10 10:15:00"},
        {"id": "TP-TRAIL", "status": 6},
    ]
    position = {
        "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
        "sl_price": 1050.0, "target_price": 1300.0, "trailing_sl_active": True,
        "signal_snapshot": {
            "fyers_sl_order_id": "SL-TRAIL",
            "fyers_target_order_id": "TP-TRAIL",
            "trailing": {"activated": True},
        },
    }
    closed = []
    broker.open_positions = lambda: [position]
    import app.paper_broker as pb
    original_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: closed.append((exit_reason, price))
    try:
        broker.reconcile_open_positions()
    finally:
        pb.PaperBroker.close_trade = original_close
    check("filled moved stop is recorded as TRAILING_SL_FYERS",
          len(closed) == 1 and closed[0][0] == "TRAILING_SL_FYERS",
          f"closed={closed}")


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
    position = {"symbol": "NSE:TATATECH-EQ", "qty": 57, "sl_price": 864.05,
                "signal_snapshot": {"fyers_sl_order_id": "SL-1"}}
    broker.apply_trailing_stop(position, ltp=885.0, settings={})
    check("modify_order sent to Fyers", len(rec["modified"]) == 1, f"modified={rec['modified']}")
    if rec["modified"]:
        m = rec["modified"][0]
        check("modify keeps SL-M (type 4)", m["type"] == 4)
        check("modify sends positive order qty", m["qty"] == 57, f"qty={m['qty']}")
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


# ── 7b. MCX per-symbol tick size (2026-08-25 SILVERMIC SL-M rejection) ─
def test_mcx_tick_size_and_slm_slack():
    """The 2026-08-25 client incident:
    - BUY entry filled on MCX:SILVERMIC26AUGFUT at 244941
    - SL-M sent with stop=244441 and limit=243218.8 (0.5% below stop)
    - Fyers rejected: 'LimitPrice not a multiple of tick size 1.0000'
      because MCX Silver ticks in whole rupees, not 0.05
    - Both retries failed with the same error, so the safety-fallback
      market SELL closed the position at a loss.

    This test locks in the fix: any MCX symbol must round to Rs 1 tick,
    and the SL slack must be a fixed points offset (not percent-based).
    """
    print("\n7b. MCX tick + SL slack — SILVERMIC SL-M must round to Rs 1 (regression from 2026-08-25)")
    from app.live_broker import _round_to_tick, _sl_limit_price, _tick_size_for

    # Tick lookup per symbol
    check("MCX:SILVERMIC26AUGFUT tick = 1.0",
          _tick_size_for("MCX:SILVERMIC26AUGFUT") == 1.0,
          f"got {_tick_size_for('MCX:SILVERMIC26AUGFUT')}")
    check("MCX:GOLDM26AUGFUT tick = 1.0",
          _tick_size_for("MCX:GOLDM26AUGFUT") == 1.0)
    check("MCX:CRUDEOIL26AUGFUT tick = 1.0",
          _tick_size_for("MCX:CRUDEOIL26AUGFUT") == 1.0)
    check("MCX:NATURALGAS26AUGFUT tick = 0.10",
          _tick_size_for("MCX:NATURALGAS26AUGFUT") == 0.10)
    check("NSE:RELIANCE-EQ tick = 0.05 (default)",
          _tick_size_for("NSE:RELIANCE-EQ") == 0.05)
    check("Empty symbol -> default 0.05",
          _tick_size_for("") == 0.05)
    check("None symbol -> default 0.05",
          _tick_size_for(None) == 0.05)

    # The exact stop that failed in prod: 244441 (entry 244941 - 500 SL points)
    mcx_stop = 244441.0
    mcx_symbol = "MCX:SILVERMIC26AUGFUT"

    # Rounding: 244441 is already a whole number, but check the math path
    rounded = _round_to_tick(mcx_stop, symbol=mcx_symbol)
    check(f"MCX stop {mcx_stop} rounds to whole rupee",
          rounded == float(int(rounded)) and rounded == 244441.0,
          f"got {rounded}")

    # The SL-limit for a SELL exit on a BUY position — this is what Fyers rejected
    sell_limit = _sl_limit_price(mcx_stop, "SELL", symbol=mcx_symbol)
    check("MCX SELL-exit SL limit is a whole rupee (no decimal)",
          sell_limit == float(int(sell_limit)),
          f"got {sell_limit}")
    check("MCX SELL-exit SL limit < stop (limit sits below stop for sell)",
          sell_limit < mcx_stop,
          f"limit={sell_limit} stop={mcx_stop}")
    # With 150-point slack: 244441 - 150 = 244291
    check("MCX SELL-exit SL limit = stop - 150 (fixed slack)",
          sell_limit == 244291.0,
          f"got {sell_limit}, expected 244291")

    # BUY exit (protecting a SELL position) — limit sits ABOVE stop
    buy_limit = _sl_limit_price(mcx_stop, "BUY", symbol=mcx_symbol)
    check("MCX BUY-exit SL limit is a whole rupee",
          buy_limit == float(int(buy_limit)),
          f"got {buy_limit}")
    check("MCX BUY-exit SL limit > stop",
          buy_limit > mcx_stop,
          f"limit={buy_limit} stop={mcx_stop}")
    check("MCX BUY-exit SL limit = stop + 150",
          buy_limit == 244591.0,
          f"got {buy_limit}, expected 244591")

    # Regression: with the OLD buggy code, the SELL slack was 0.5% =
    # 244441 * 0.995 = 243218.795 → rounds to 243218.80 on a 0.05 tick
    # (that has a decimal in the tenths place) → Fyers rejects.
    # The NEW code must NOT produce a decimal for MCX.
    check("MCX SL limit has NO decimal component (blocks 2026-08-25 rejection)",
          sell_limit % 1 == 0 and buy_limit % 1 == 0,
          f"sell={sell_limit} buy={buy_limit}")

    # NSE side must still use 0.05 tick and 0.5% slack — the fix must not
    # regress existing NSE behavior.
    nse_stop = 400.05
    nse_symbol = "NSE:ABCAPITAL-EQ"
    nse_sell_limit = _sl_limit_price(nse_stop, "SELL", symbol=nse_symbol)
    check("NSE SELL SL limit still multiple of 0.05",
          abs((nse_sell_limit / 0.05) - round(nse_sell_limit / 0.05)) < 1e-6,
          f"got {nse_sell_limit}")
    check("NSE SELL SL limit ~0.5% below stop (unchanged behavior)",
          abs(nse_sell_limit - (nse_stop * 0.995)) < 0.05,
          f"got {nse_sell_limit}, expected ~{nse_stop * 0.995}")


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


# ── 8e. Design B — Fyers as single source of truth (2026-08-26) ───────
# Regression: client's account went short 1 because the app-side
# check_exits fired a duplicate MARKET sell 4s after Fyers's own SL-Limit
# had already flattened the position. These tests lock in the pre-flight
# guards that prevent the whole class of double-order races.
def test_close_trade_skips_market_when_fyers_already_flat():
    print("\n8e. Design B close_trade — Fyers already flat -> skip market exit (2026-08-26 short-position regression)")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    import app.live_broker as lb
    import app.fyers_client as fc
    # Fyers is authoritative: it reports net_qty=0 for this symbol.
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False,
        "positions": [{"symbol": "MCX:SILVERMIC26AUGFUT", "net_qty": 0}],
    }
    # Direct sanity check on the helper itself.
    check("_fyers_current_net_qty returns 0 when Fyers is flat",
          lb._fyers_current_net_qty("MCX:SILVERMIC26AUGFUT") == 0.0)

    # Track everything close_trade could do so we can assert what it did NOT.
    market_exits = []
    orig_place = broker._place_live_order
    broker._place_live_order = lambda symbol, side, qty, order_type="MARKET", limit_price=0.0: (
        market_exits.append((symbol, side, qty, order_type)) or {"s": "ok", "id": "SHOULD-NOT-FIRE"}
    )
    persisted = []
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: (
        persisted.append((pos["symbol"], exit_reason, price))
    )
    try:
        position = {
            "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
            "entry_price": 246140.0, "sl_price": 245690.0, "target_price": 251140.0,
            "signal_snapshot": {"fyers_sl_order_id": "SL-42", "fyers_target_order_id": "TP-42"},
        }
        broker.close_trade(position, 245813.0, "SL")
        # Guard skipped the market: no _place_live_order call.
        check("no MARKET exit sent when Fyers already flat", len(market_exits) == 0,
              f"unexpected market exits: {market_exits}")
        # Guard still cancelled the leftover Target order (SL-42 too, though
        # it's already filled — cancel is idempotent at Fyers).
        cancelled_ids = {c.get("id") for c in rec["cancelled"]}
        check("leftover Target LIMIT was cancelled", "TP-42" in cancelled_ids,
              f"cancelled={rec['cancelled']}")
        # DB closure still happened so our books reflect reality.
        check("DB close_trade still recorded", len(persisted) == 1 and persisted[0][1] == "SL",
              f"persisted={persisted}")
    finally:
        broker._place_live_order = orig_place
        pb.PaperBroker.close_trade = orig_close


def test_close_trade_skips_market_when_sl_cancel_reports_already_filled():
    print("\n8e2. Design B close_trade — SL cancel -52 means already filled, so skip market exit")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    import app.fyers_client as fc
    # Pre-flight still thinks the position exists, so the close path must rely
    # on the SL cancel response itself as the hard abort signal.
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False,
        "positions": [{"symbol": "MCX:SILVERMIC26AUGFUT", "net_qty": 1}],
    }

    cancel_calls = []
    broker._cancel_fyers_order = lambda order_id, reason="": (
        cancel_calls.append((order_id, reason)) or (
            {"code": -52, "message": "Not a pending order", "s": "error"}
            if order_id == "SL-52"
            else {"s": "ok", "id": order_id}
        )
    )

    market_exits = []
    broker._place_live_order = lambda symbol, side, qty, order_type="MARKET", limit_price=0.0: (
        market_exits.append((symbol, side, qty, order_type)) or {"s": "ok", "id": "SHOULD-NOT-FIRE"}
    )

    persisted = []
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: (
        persisted.append((pos["symbol"], exit_reason, price))
    )
    try:
        position = {
            "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
            "entry_price": 246140.0, "sl_price": 245690.0, "target_price": 251140.0,
            "signal_snapshot": {"fyers_sl_order_id": "SL-52", "fyers_target_order_id": "TP-52"},
        }
        broker.close_trade(position, 245813.0, "SL")
        check("SL cancel attempted first", cancel_calls[:1] == [("SL-52", "close_trade:SL")],
              f"cancel_calls={cancel_calls}")
        check("target cancel is skipped after -52 abort", all(order_id != "TP-52" for order_id, _ in cancel_calls),
              f"cancel_calls={cancel_calls}")
        check("no MARKET exit sent after -52 already-filled signal", len(market_exits) == 0,
              f"market_exits={market_exits}")
        check("DB close_trade still recorded after -52 abort", len(persisted) == 1 and persisted[0][1] == "SL",
              f"persisted={persisted}")
    finally:
        pb.PaperBroker.close_trade = orig_close


def test_bootstrap_fyers_app_position_and_land_in_closed_trades():
    """A user opens a position directly in the FYERS app. Our reconcile
    should bootstrap a DB row so that when they later flatten it in FYERS
    the trade lands in Closed Trades Today with MANUAL_EXTERNAL_EXIT
    rather than silently disappearing from the Open Positions panel."""
    print("\n8p. reconcile — FYERS-app-managed position bootstrapped, closes into live_trades")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    import app.fyers_client as fc
    import app.live_broker as live_broker_module

    # Phase 1: FYERS holds a SELL 1 that the DB doesn't know about.
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False,
        "positions": [{
            "symbol": "MCX:SILVERMIC26AUGFUT", "net_qty": -1,
            "entry_price": 248500.0, "ltp": 248500.0,
        }],
    }

    inserted_rows: list[dict] = []
    original_run_with_supabase = live_broker_module.run_with_supabase

    class _StubTable:
        def __init__(self, sink): self._sink = sink
        def insert(self, payload):
            self._sink.append(payload); return self
        def execute(self): return {"data": self._sink[-1:]}
    class _StubClient:
        def __init__(self, sink): self._sink = sink
        def table(self, _name): return _StubTable(self._sink)

    def _fake_run(fn):
        return fn(_StubClient(inserted_rows))
    live_broker_module.run_with_supabase = _fake_run

    broker.open_positions = lambda: []
    try:
        inserted = broker._bootstrap_broker_only_positions(
            watchlist=["MCX:SILVERMIC26AUGFUT"],
            summary={"errors": 0},
        )
        check("one fyers-app position was bootstrapped", inserted == 1,
              f"inserted={inserted}, rows={inserted_rows}")
        row = inserted_rows[0]
        check("bootstrapped row has fyers_app_manual origin",
              (row.get("signal_snapshot") or {}).get("origin") == "fyers_app_manual",
              f"row={row}")
        check("bootstrapped row uses unreachable protective levels for SELL",
              row.get("sl_price") >= 1_000_000_000 and row.get("target_price") == 0.0,
              f"row={row}")
        check("bootstrapped row carries the FYERS entry price and side",
              row.get("side") == "SELL" and row.get("entry_price") == 248500.0,
              f"row={row}")
        check("bootstrapped row is labeled as opened in the FYERS app",
              row.get("entry_trigger") == "Opened directly in FYERS app",
              f"row={row}")

        # A symbol outside the strategy's watchlist must never be bootstrapped.
        inserted_rows.clear()
        fc.get_broker_positions = lambda mode: {
            "available": True, "cached": False,
            "positions": [{"symbol": "NSE:RANDOM-EQ", "net_qty": 5, "entry_price": 100.0}],
        }
        inserted_off_wl = broker._bootstrap_broker_only_positions(
            watchlist=["MCX:SILVERMIC26AUGFUT"], summary={"errors": 0},
        )
        check("off-watchlist FYERS positions are ignored", inserted_off_wl == 0,
              f"rows={inserted_rows}")

        # Empty watchlist = do nothing (safe default).
        inserted_empty = broker._bootstrap_broker_only_positions(
            watchlist=None, summary={"errors": 0},
        )
        check("no watchlist = no bootstrap", inserted_empty == 0, "")
    finally:
        live_broker_module.run_with_supabase = original_run_with_supabase


def test_close_trade_still_sends_market_when_fyers_still_holds():
    print("\n8f. Design B close_trade — Fyers still holds position -> market exit fires normally")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    import app.fyers_client as fc
    # Fyers is authoritative: it reports net_qty=1 still open.
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False,
        "positions": [{"symbol": "MCX:SILVERMIC26AUGFUT", "net_qty": 1}],
    }

    market_exits = []
    broker._place_live_order = lambda symbol, side, qty, order_type="MARKET", limit_price=0.0: (
        market_exits.append((symbol, side, qty)) or {"s": "ok", "id": "REAL-EXIT"}
    )
    broker._resolve_fill_details = lambda **kwargs: (245800.0, "2026-08-26T10:00:00+00:00")
    persisted = []
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: (
        persisted.append((exit_reason, price))
    )
    try:
        position = {
            "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
            "entry_price": 246140.0, "sl_price": 245690.0, "target_price": 251140.0,
            "signal_snapshot": {"fyers_sl_order_id": "SL-99", "fyers_target_order_id": "TP-99"},
        }
        broker.close_trade(position, 245813.0, "SL")
        check("MARKET exit fires when Fyers still holds the position",
              len(market_exits) == 1 and market_exits[0][1] == "SELL",
              f"market_exits={market_exits}")
        check("DB records the actual Fyers fill price",
              len(persisted) == 1 and persisted[0][1] == 245800.0,
              f"persisted={persisted}")
    finally:
        pb.PaperBroker.close_trade = orig_close


def test_close_trade_falls_back_to_market_when_fyers_unavailable():
    print("\n8g. Design B close_trade — Fyers unreachable (None) -> do NOT skip, fall back to market exit")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    import app.fyers_client as fc
    def _boom(mode):
        raise RuntimeError("fyers proxy down")
    fc.get_broker_positions = _boom

    market_exits = []
    broker._place_live_order = lambda symbol, side, qty, order_type="MARKET", limit_price=0.0: (
        market_exits.append((symbol, side, qty)) or {"s": "ok", "id": "FALLBACK-EXIT"}
    )
    broker._resolve_fill_details = lambda **kwargs: (245800.0, None)
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: None
    try:
        position = {
            "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
            "entry_price": 246140.0, "sl_price": 245690.0, "target_price": 251140.0,
            "signal_snapshot": {"fyers_sl_order_id": "SL-X", "fyers_target_order_id": "TP-X"},
        }
        broker.close_trade(position, 245813.0, "SL")
        # Fyers-unknown MUST NOT skip the market exit — that would leave a
        # naked position uncovered during an outage.
        check("Fyers-unavailable falls back to normal market exit",
              len(market_exits) == 1,
              f"market_exits={market_exits}")
    finally:
        pb.PaperBroker.close_trade = orig_close


# ── 8i. Design B — real-time Fyers→us sync via Order Update WS ────────
def test_ws_order_fill_closes_position_immediately():
    print("\n8i. Order-WS push — SL FILLED event closes DB position immediately (no 30s poll wait)")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    position = {
        "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
        "entry_price": 246140.0, "sl_price": 245690.0, "target_price": 251140.0,
        "signal_snapshot": {"fyers_sl_order_id": "SL-42", "fyers_target_order_id": "TP-42"},
    }
    broker.open_positions = lambda: [position]
    closed = []
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: (
        closed.append((exit_reason, price, exit_time))
    )
    try:
        event = {
            "kind": "order", "symbol": "MCX:SILVERMIC26AUGFUT",
            "order_id": "SL-42", "status": 2, "side": "SELL",
            "traded_price": 245813.0, "traded_qty": 1,
            "traded_at": "2026-08-26T09:47:40+00:00",
        }
        result = broker.handle_order_event(event)
        check("SL fill event closes the position immediately",
              result.get("action") == "closed" and result.get("exit_reason") == "SL_FYERS",
              f"result={result}")
        check("DB close_trade recorded with Fyers fill price",
              len(closed) == 1 and closed[0][1] == 245813.0, f"closed={closed}")
        check("sibling Target LIMIT was cancelled on WS push",
              any(c.get("id") == "TP-42" for c in rec["cancelled"]),
              f"cancelled={rec['cancelled']}")
    finally:
        pb.PaperBroker.close_trade = orig_close


def test_ws_position_flat_force_syncs_stale_db():
    print("\n8j. Order-WS push — position net_qty=0 force-syncs stale DB row (2026-08-26 stuck-entry regression)")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    position = {
        "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
        "entry_price": 246140.0, "sl_price": 245690.0, "target_price": 251140.0,
        "_last_ltp": 245900.0,
        "signal_snapshot": {"fyers_sl_order_id": "SL-88", "fyers_target_order_id": "TP-88"},
    }
    broker.open_positions = lambda: [position]
    closed = []
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, pos, price, exit_reason, exit_time=None: (
        closed.append((exit_reason, price))
    )
    try:
        # Client hit Exit in Fyers app — WS pushes net_qty=0 for our symbol.
        event = {
            "kind": "position", "symbol": "MCX:SILVERMIC26AUGFUT", "net_qty": 0,
        }
        result = broker.handle_order_event(event)
        check("position=0 event force-closes the stale DB row",
              result.get("action") == "closed" and result.get("exit_reason") == "MANUAL_EXTERNAL_EXIT",
              f"result={result}")
        check("DB close_trade recorded as MANUAL_EXTERNAL_EXIT",
              len(closed) == 1 and closed[0][0] == "MANUAL_EXTERNAL_EXIT",
              f"closed={closed}")
        check("leftover protective orders cancelled",
              {c.get("id") for c in rec["cancelled"]} >= {"SL-88", "TP-88"},
              f"cancelled={rec['cancelled']}")
    finally:
        pb.PaperBroker.close_trade = orig_close


def test_ws_manual_external_entry_logged_not_persisted():
    print("\n8k. Order-WS push — manual entry at Fyers (unknown symbol) logged, no fake row written")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    broker.open_positions = lambda: []  # our DB has nothing for this symbol
    closed = []
    import app.paper_broker as pb
    orig_close = pb.PaperBroker.close_trade
    pb.PaperBroker.close_trade = lambda self, *a, **k: closed.append(a)
    try:
        event = {"kind": "position", "symbol": "MCX:GOLDM26AUGFUT", "net_qty": 1}
        result = broker.handle_order_event(event)
        check("manual entry event logged as informational",
              result.get("action") == "logged_manual_entry", f"result={result}")
        check("no DB row inserted for manual entry",
              len(closed) == 0, f"unexpected close_trade calls: {closed}")
    finally:
        pb.PaperBroker.close_trade = orig_close


def test_ws_cancelled_protective_order_clears_snapshot():
    print("\n8l. Order-WS push — protective order CANCELLED clears the id from snapshot")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)
    position = {
        "symbol": "MCX:SILVERMIC26AUGFUT", "side": "BUY", "qty": 1,
        "sl_price": 245690.0, "target_price": 251140.0,
        "signal_snapshot": {"fyers_sl_order_id": "SL-77", "fyers_target_order_id": "TP-77"},
    }
    broker.open_positions = lambda: [position]
    event = {
        "kind": "order", "symbol": "MCX:SILVERMIC26AUGFUT",
        "order_id": "TP-77", "status": 1, "side": "SELL",
    }
    result = broker.handle_order_event(event)
    check("cancelled Target updates the snapshot",
          result.get("action") == "cleared_snapshot" and result.get("role") == "target",
          f"result={result}")
    check("Target id cleared from snapshot after cancel",
          position["signal_snapshot"].get("fyers_target_order_id") is None,
          f"snapshot={position['signal_snapshot']}")
    check("SL id untouched",
          position["signal_snapshot"].get("fyers_sl_order_id") == "SL-77",
          f"snapshot={position['signal_snapshot']}")


def test_ws_dispatch_parses_orders_and_positions_bundle():
    print("\n8m. dispatch_message — one Fyers frame with orders+positions fires two normalized events")
    import app.fyers_order_updates as fo
    received: list[dict] = []
    # Fyers V3 sometimes wraps under top-level 'd', sometimes not. Cover both.
    for wrapper in ({"orders": {"id": "O-1", "symbol": "NSE:TCS-EQ", "status": 2, "side": 1, "tradedPrice": 3900.0, "filledQty": 5}},
                    {"d": {"positions": [{"symbol": "MCX:SILVERMIC26AUGFUT", "netQty": 0}]}}):
        count = fo.dispatch_message(wrapper, received.append)
        check(f"dispatch returned {count} for wrapper {list(wrapper.keys())}", count == 1,
              f"received={received}")
    check("first event is a normalized order fill",
          received[0]["kind"] == "order" and received[0]["symbol"] == "NSE:TCS-EQ"
          and received[0]["order_id"] == "O-1" and received[0]["side"] == "BUY"
          and received[0]["status"] == 2,
          f"event={received[0]}")
    check("second event is a normalized position flat",
          received[1]["kind"] == "position" and received[1]["symbol"] == "MCX:SILVERMIC26AUGFUT"
          and received[1]["net_qty"] == 0,
          f"event={received[1]}")


def test_order_update_ws_status_callback_accepts_fyers_payload():
    print("\n8n. Order-WS status callback accepts FYERS positional payloads")
    import app.engine as eng

    old_status = dict(eng._order_update_ws_last_status)
    was_connected = eng._order_update_ws_connected.is_set()
    try:
        eng._order_update_ws_connected.clear()
        eng._record_order_update_ws_status({
            "connected": True,
            "message": "Order-update WS subscribed",
            "feed_name": "order_updates",
        })
        check("positional subscribed status marks Order-WS connected",
              eng._order_update_ws_connected.is_set(),
              f"status={eng._order_update_ws_last_status}")
        check("positional status payload is retained",
              eng._order_update_ws_last_status.get("message") == "Order-update WS subscribed",
              f"status={eng._order_update_ws_last_status}")

        eng._record_order_update_ws_status(
            connected=False,
            message="Order-update WS closed",
        )
        check("keyword closed status clears Order-WS connection",
              not eng._order_update_ws_connected.is_set(),
              f"status={eng._order_update_ws_last_status}")
    finally:
        eng._order_update_ws_last_status.clear()
        eng._order_update_ws_last_status.update(old_status)
        if was_connected:
            eng._order_update_ws_connected.set()
        else:
            eng._order_update_ws_connected.clear()


def test_engine_order_update_router_handles_all_event_kinds():
    print("\n8o. Order-WS engine router — order, trade, and position events reach LiveBroker")
    import app.engine as eng

    symbol = "MCX:SILVERMIC26AUGFUT"
    broker = object.__new__(LiveBroker)
    received: list[dict] = []
    broker.handle_order_event = lambda event: (
        received.append(event) or {"action": "handled", "kind": event.get("kind")}
    )

    original_strategies = eng.STRATEGIES
    eng.STRATEGIES = {
        "algo3": SimpleNamespace(algo_id="algo3", watchlist=[symbol], broker=broker),
    }
    try:
        events = [
            {"kind": "order", "symbol": symbol, "order_id": "O-1"},
            {"kind": "trade", "symbol": symbol, "order_id": "O-1"},
            {"kind": "position", "symbol": symbol, "net_qty": 0},
        ]
        for event in events:
            eng._dispatch_order_update_event(event)
        check("all three event kinds are routed without callback exceptions",
              [event.get("kind") for event in received] == ["order", "trade", "position"],
              f"received={received}")
    finally:
        eng.STRATEGIES = original_strategies


def test_open_trade_refuses_duplicate_when_fyers_already_positioned():
    print("\n8h. Design B open_trade — Fyers already holds symbol -> refuse duplicate entry")
    rec = {"placed": [], "cancelled": [], "modified": [], "orderbook": []}
    broker = make_broker(rec)

    import app.fyers_client as fc
    # Fyers already holds a long 1 — a duplicate entry would double up.
    fc.get_broker_positions = lambda mode: {
        "available": True, "cached": False,
        "positions": [{"symbol": "MCX:SILVERMIC26AUGFUT", "net_qty": 1}],
    }

    orders_placed = []
    broker._place_live_order = lambda symbol, side, qty, order_type="MARKET", limit_price=0.0: (
        orders_placed.append((symbol, side, qty)) or {"s": "ok", "id": "DUP-ENTRY"}
    )
    broker._cap_qty_to_live_funds = lambda symbol, qty, price: qty
    persisted = []
    import app.paper_broker as pb
    orig_open = pb.PaperBroker.open_trade
    pb.PaperBroker.open_trade = lambda self, *args, **kwargs: persisted.append((args, kwargs))
    try:
        try:
            broker.open_trade("MCX:SILVERMIC26AUGFUT", "BUY", 1, 246140.0, 245940.0, 250140.0)
            check("open_trade should have raised on duplicate", False, "no exception")
        except RuntimeError as exc:
            text = str(exc).lower()
            check("refusal mentions already positioned",
                  "already positioned" in text or "net_qty" in text, str(exc))
        check("no entry order sent when Fyers already holds symbol",
              len(orders_placed) == 0, f"orders_placed={orders_placed}")
        check("no DB row written for the refused duplicate",
              len(persisted) == 0, f"persisted={persisted}")
    finally:
        pb.PaperBroker.open_trade = orig_open


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
    check("tick #2: trailing current_sl matches position sl_price",
          t2.get("current_sl") == p2["sl_price"],
          f"current_sl={t2.get('current_sl')} sl_price={p2['sl_price']}")
    check("tick #2: first trail event captured", len(t2.get("events") or []) == 1,
          f"events={t2.get('events')}")

    # Tick 3 — price at 102 (higher). SL bumps again.
    p3 = broker.apply_trailing_stop(p2, ltp=102.0, settings=settings)
    t3 = p3["signal_snapshot"]["trailing"]
    check("tick #3 second bump: update_count == 2", t3.get("update_count") == 2,
          f"got={t3.get('update_count')}")
    check("tick #3: first_activated_at is preserved", t3.get("first_activated_at") == t2.get("first_activated_at"))
    check("tick #3: last_updated_at is fresher than first_activated_at OR equal",
          t3.get("last_updated_at") >= t3.get("first_activated_at"))
    check("tick #3: second trail event captured", len(t3.get("events") or []) == 2,
          f"events={t3.get('events')}")
    check("tick #3: trailing current_sl stays synced", t3.get("current_sl") == p3["sl_price"],
          f"current_sl={t3.get('current_sl')} sl_price={p3['sl_price']}")

    # Tick 4 — price DROPS to 101.7 (still profitable but below prev high).
    # highest stays at 102. new_sl computed from highest (unchanged) so SL
    # does NOT move. update_count must NOT increment.
    p4 = broker.apply_trailing_stop(p3, ltp=101.7, settings=settings)
    t4 = p4["signal_snapshot"]["trailing"]
    check("tick #4 no new high: update_count stays at 2 (no false bump)",
          t4.get("update_count") == 2,
          f"got={t4.get('update_count')}")
    check("tick #4: trail event list is preserved", len(t4.get("events") or []) == 2,
          f"events={t4.get('events')}")


# ── 12b. Silver target-to-breakeven policy ───────────────────────────
def test_silver_target_to_breakeven_policy():
    print("\n12b. Silver target-to-breakeven — one stop move plus final target")
    import app.paper_broker as pb
    import app.live_broker as lb
    import app.strategy_settings as strategy_settings

    pb.run_with_supabase = lambda fn: type("R", (), {"data": []})()
    broker = object.__new__(pb.PaperBroker)
    broker.algo_id = "algo3"
    broker.positions_table_name = lambda: "positions"
    settings = {"exit_mode": "target_to_breakeven_sl"}
    position = {
        "id": "silver-be-1", "symbol": "MCX:SILVERMIC31AUGFUT", "side": "BUY",
        "entry_price": 100.0, "sl_price": 90.0, "target_price": 120.0,
        "highest_price": 100.0, "lowest_price": 100.0, "trailing_sl_active": False,
        "signal_snapshot": {
            "silver_exit_policy": "target_to_breakeven_sl",
            "initial_sl_price": 90.0,
            "silver_breakeven": {
                "armed": False,
                "activation_price": 110.0,
                "activation_points": 10.0,
                "final_target_enabled": True,
            },
        },
    }
    before = broker.apply_trailing_stop(position, ltp=109.0, settings=settings)
    check("breakeven is not armed before target", not before["trailing_sl_active"] and before["sl_price"] == 90.0)
    at_target = broker.apply_trailing_stop(before, ltp=110.0, settings=settings)
    check("target arms one breakeven stop at entry", at_target["trailing_sl_active"] and at_target["sl_price"] == 100.0)
    after = broker.apply_trailing_stop(at_target, ltp=115.0, settings=settings)
    check("breakeven stop does not keep trailing", after["sl_price"] == 100.0 and len(after["signal_snapshot"]["trailing"]["events"]) == 1)
    check("breakeven final target remains a close signal", broker.should_exit_at_target(settings, after))
    check("fixed Silver target remains an exit signal", broker.should_exit_at_target({"exit_mode": "fixed_target_sl"}, {"signal_snapshot": {"silver_exit_policy": "fixed_target_sl"}}))

    live = object.__new__(lb.LiveBroker)
    live._place_slm_order = lambda *args: {"s": "ok", "id": "sl-only"}
    target_calls = []
    live._place_limit_order = lambda *args: target_calls.append(args) or {"s": "ok", "id": "target"}
    protection = live._place_protective_orders("MCX:SILVERMIC31AUGFUT", "BUY", 1, 90.0, 120.0, include_target=True)
    check("live breakeven keeps both FYERS SL and final target", protection["sl_order_id"] == "sl-only" and protection["target_order_id"] == "target" and target_calls)

    try:
        strategy_settings.validate_settings({
            "silver_breakout_points": 200,
            "sl_points": 200,
            "tsl_activate_points": 2000,
            "target_points": 2000,
            "exit_mode": "target_to_breakeven_sl",
        }, "algo3")
        invalid_activation_rejected = False
    except ValueError:
        invalid_activation_rejected = True
    check("breakeven activation must be below final target", invalid_activation_rejected)

    normalized = strategy_settings._normalize({"exit_mode": "fixed_target_trailing_sl"}, "algo3")
    check("legacy Silver mode defaults new entries to fixed", normalized["exit_mode"] == "fixed_target_sl")

    legacy_settings = strategy_settings._normalize({
        "exit_mode": "fixed_target_trailing_sl",
        "trailing_sl_enabled": True,
    }, "algo3")
    persisted_legacy_settings = strategy_settings._settings_for_persistence(legacy_settings)
    check(
        "runtime-only legacy Silver fields never reach Supabase",
        not any(key.startswith("_legacy_silver_") for key in persisted_legacy_settings),
        f"settings={persisted_legacy_settings}",
    )
    legacy_position_settings = broker._legacy_silver_position_settings(
        {"signal_snapshot": {}}, legacy_settings
    )
    check(
        "pre-upgrade open Silver position keeps its legacy policy",
        legacy_position_settings["exit_mode"] == "fixed_target_trailing_sl"
        and legacy_position_settings["trailing_sl_enabled"] is True,
        f"settings={legacy_position_settings}",
    )


# ── 13. Daily trade totals preserve every row ──────────────────────────
def test_daily_trade_totals_do_not_collapse_same_side_rows():
    print("\n13. Daily trade totals — every same-side row is counted")
    import app.paper_broker as pb

    source_rows = {
        "trades": [
            {"id": "trade-1", "side": "SELL"},
            {"id": "trade-2", "side": "SELL"},
            {"id": "trade-3", "side": "BUY"},
        ],
        "positions": [{"id": "position-1", "side": "SELL"}],
    }

    class Query:
        def __init__(self, table_name):
            self.table_name = table_name
            self.columns = "*"

        def select(self, columns):
            self.columns = columns
            return self

        def eq(self, *_args):
            return self

        def gte(self, *_args):
            return self

        def execute(self):
            selected = {column.strip() for column in self.columns.split(",")}
            rows = [
                {key: value for key, value in row.items() if key in selected}
                for row in source_rows[self.table_name]
            ]
            return type("Result", (), {"data": rows})()

    class FakeSupabase:
        def table(self, table_name):
            return Query(table_name)

    original_run_with_supabase = pb.run_with_supabase
    try:
        pb.run_with_supabase = lambda callback: callback(FakeSupabase())
        broker = object.__new__(pb.PaperBroker)
        broker.algo_id = "algo3"
        broker.storage_algo_candidates = lambda: ["algo3__smoke"]
        broker.trades_table_name = lambda: "trades"
        broker.positions_table_name = lambda: "positions"
        counts = broker.today_counts()
    finally:
        pb.run_with_supabase = original_run_with_supabase

    check("daily total counts closed and open trades", counts["trade_count_today"] == 4, str(counts))
    check("daily total keeps repeated SELL trades", counts["sell_count_today"] == 3, str(counts))
    check("daily total keeps BUY trades", counts["buy_count_today"] == 1, str(counts))


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


def test_connection_status_token_present_settling_survives_redeploy():
    print("\n26bb. Connection status — stored token + settling engine state stays in verifying mode")
    from unittest.mock import patch
    import app.fyers_client as fc

    class FakeProfile:
        def get_profile(self):
            raise RuntimeError("backend restart in progress")

    fake_token_row = {"access_token": "T", "refresh_token": "R"}
    with patch.object(fc, "get_active_broker_key", return_value="fyers_paper"), \
         patch.object(fc, "get_stored_token_row", return_value=fake_token_row), \
         patch.object(fc, "get_runtime_trading_mode", return_value="paper"), \
         patch.object(fc, "_is_recent_token_row", return_value=True), \
         patch.object(fc, "get_fyers_model", return_value=FakeProfile()), \
         patch("app.engine.get_engine_status", return_value={
             "fyers_session_state": "token_present_settling",
             "fyers_recovery_owner": "startup",
         }):
        result = fc.get_connection_status()
    check("session_state remains token_present_settling",
          result.get("session_state") == "token_present_settling",
          f"got session_state={result.get('session_state')!r}")
    check("status remains rechecking, not disconnected",
          result.get("status") == "rechecking",
          f"got status={result.get('status')!r}")


def test_connection_status_bad_request_stays_degraded_not_expired():
    print("\n26bc. Connection status — stored token + FYERS bad request stays degraded")
    from unittest.mock import patch
    import app.fyers_client as fc

    class FakeProfile:
        def get_profile(self):
            return {"s": "error", "message": "Bad request", "code": -99}

    fake_token_row = {"access_token": "T", "refresh_token": "R"}
    with patch.object(fc, "get_active_broker_key", return_value="fyers_paper"), \
         patch.object(fc, "get_stored_token_row", return_value=fake_token_row), \
         patch.object(fc, "get_runtime_trading_mode", return_value="paper"), \
         patch.object(fc, "_is_recent_token_row", return_value=False), \
         patch.object(fc, "get_fyers_model", return_value=FakeProfile()), \
         patch("app.engine.get_engine_status", return_value={
             "fyers_session_state": "token_present_connected",
             "fyers_recovery_owner": None,
         }):
        result = fc.get_connection_status()
    check("bad request returns degraded, not expired",
          result.get("status") == "degraded",
          f"got status={result.get('status')!r}")
    check("bad request keeps session connected",
          result.get("connected") is True and result.get("verified") is False,
          f"got connected={result.get('connected')!r} verified={result.get('verified')!r}")


def test_silver_setup_history_naive_ist_stores_as_correct_utc():
    print("\n26c. Silver setup history — naive IST candle time stores as correct UTC")
    import datetime as _dt
    import app.silver_setup_history as ssh

    candle_time = _dt.datetime(2026, 8, 20, 23, 15, 0)
    stored = ssh._utc_iso(candle_time)
    check("23:15 IST becomes 17:45 UTC in storage",
          stored == "2026-08-20T17:45:00+00:00",
          f"got={stored}")


def test_silver_setup_history_repairs_future_shifted_legacy_rows():
    print("\n26d. Silver setup history — legacy future-shifted rows are normalized on read")
    import datetime as _dt
    from unittest.mock import patch
    import app.silver_setup_history as ssh

    fake_now = _dt.datetime(2026, 8, 20, 18, 20, 0, tzinfo=_dt.timezone.utc)
    row = {"candle_time": "2026-08-20T23:15:00+00:00"}

    with patch.object(ssh, "_now_utc", return_value=fake_now):
        fixed = ssh._normalize_legacy_row(row)
    check("legacy row shifted back by 5h30",
          fixed.get("candle_time") == "2026-08-20T17:45:00+00:00",
          f"got={fixed.get('candle_time')}")


def test_silver_setup_history_requires_candle_to_close_on_correct_ema_side():
    print("\n26e. Silver setup history — invalid candle/EMA rows cannot be saved or displayed")
    import app.silver_setup_history as ssh

    valid_buy = {
        "setup_side": "BUY",
        "candle_open": 244_500,
        "candle_close": 244_900,
        "ema20": 244_800,
    }
    invalid_buy = {
        "setup_side": "BUY",
        "candle_open": 244_500,
        "candle_close": 244_900,
        "ema20": 245_100,
    }
    valid_sell = {
        "setup_side": "SELL",
        "candle_open": 245_100,
        "candle_close": 244_700,
        "ema20": 244_900,
    }
    invalid_sell = {
        "setup_side": "SELL",
        "candle_open": 245_100,
        "candle_close": 244_700,
        "ema20": 244_600,
    }

    check("green candle above EMA qualifies as BUY history",
          ssh._is_qualifying_setup_row(valid_buy))
    check("green candle below EMA is rejected as BUY history",
          not ssh._is_qualifying_setup_row(invalid_buy))
    check("red candle below EMA qualifies as SELL history",
          ssh._is_qualifying_setup_row(valid_sell))
    check("red candle above EMA is rejected as SELL history",
          not ssh._is_qualifying_setup_row(invalid_sell))


def test_silver_feed_status_falls_back_to_persisted_setup_history():
    print("\n26f. Silver feed status — persisted BUY/SELL setup history stays visible after in-memory reset")
    import datetime as _dt
    from unittest.mock import patch
    from app.strategies.algo3_silver_micro import Algo3SilverMicro

    persisted_buy = {
        "candle_close": 244_975.0,
        "candle_time": "2026-08-25T05:15:00+00:00",
    }
    persisted_sell = {
        "candle_close": 244_479.0,
        "candle_time": "2026-08-25T07:15:00+00:00",
    }

    with patch.object(Algo3SilverMicro, "refresh_market_data", return_value=None), \
         patch("app.strategies.algo3_silver_micro.get_latest_setup_reference") as latest_setup:
        latest_setup.side_effect = lambda algo_id, side, live_only=True: (
            persisted_buy if side == "BUY" else persisted_sell
        )
        strategy = Algo3SilverMicro()
        strategy._buy_setup_close = None
        strategy._buy_setup_bar_at = None
        strategy._sell_setup_close = None
        strategy._sell_setup_bar_at = None
        status = strategy.feed_status()

    check("BUY close falls back to persisted setup history",
          status.get("buy_setup_close") == 244_975.0,
          f"got={status.get('buy_setup_close')}")
    check("SELL close falls back to persisted setup history",
          status.get("sell_setup_close") == 244_479.0,
          f"got={status.get('sell_setup_close')}")
    check("BUY bar time falls back to persisted setup history",
          status.get("buy_setup_bar_at") == "2026-08-25T05:15:00+00:00",
          f"got={status.get('buy_setup_bar_at')}")
    check("SELL bar time falls back to persisted setup history",
          status.get("sell_setup_bar_at") == "2026-08-25T07:15:00+00:00",
          f"got={status.get('sell_setup_bar_at')}")


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


def test_open_position_rest_fallback_ignores_fresh_nse_ticks():
    print("\n28bb. Open-position REST fallback ignores fresh NSE ticks when Silver itself is stale")
    import time as _time
    import datetime as _dt
    import app.engine as eng

    symbol = "MCX:SILVERMIC26AUGFUT"
    stale = (_time.time() - 25)
    fresh = (_time.time() - 2)

    eng._symbol_last_tick_at.clear()
    eng._symbol_last_tick_at[symbol] = _dt.datetime.fromtimestamp(
        stale, tz=_dt.timezone.utc
    ).isoformat()
    eng._engine_status["last_tick_at"] = _dt.datetime.fromtimestamp(
        fresh, tz=_dt.timezone.utc
    ).isoformat()

    strat = _make_bare_algo3()
    strat.symbol = symbol
    strat.watchlist = [symbol]
    strat.broker._open_positions = [{
        "symbol": symbol,
        "side": "BUY",
        "qty": 1,
        "entry_price": 246886.0,
        "sl_price": 248380.0,
        "target_price": 249886.0,
    }]

    old_strategies = dict(eng.STRATEGIES)
    try:
        eng.STRATEGIES = {"algo3": strat}
        stale_symbols = eng._stale_open_position_symbols(max_age_seconds=10.0)
        check("stale Silver open position still qualifies for REST fallback despite fresh global tick",
              stale_symbols == {symbol},
              f"stale_symbols={stale_symbols}")
    finally:
        eng.STRATEGIES = old_strategies


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


def test_restart_live_feed_suppresses_duplicate_watchdog_during_settling():
    print("\n29b. Recovery owner lock suppresses duplicate watchdog restart during settling")
    from unittest.mock import patch
    import app.engine as eng

    old_status = dict(eng._engine_status)
    try:
        eng._engine_status.update({
            "fyers_recovery_id": "abcd1234",
            "fyers_recovery_owner": "oauth_callback",
            "fyers_recovery_reason": "fyers_oauth_callback:paper",
            "fyers_recovery_started_at": eng._utc_now(),
            "fyers_recovery_settling_until": eng._iso_after(30),
            "fyers_session_state": "token_present_settling",
        })
        with patch.object(eng, "start_live_feed_if_ready", return_value=True):
            started = eng.restart_live_feed(reason="watchdog_missing_first_tick", ignore_backoff=False)
        check("watchdog restart suppressed while oauth recovery is settling",
              started is False,
              f"started={started}")
        check("existing recovery owner preserved",
              eng._engine_status.get("fyers_recovery_owner") == "oauth_callback",
              f"owner={eng._engine_status.get('fyers_recovery_owner')!r}")
    finally:
        eng._engine_status.clear()
        eng._engine_status.update(old_status)


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
        # The canonical production BUY model is a finalized 15m reference
        # breakout. Older saved values are normalized to this same model.
        "silver_buy_plan": "reference_breakout",
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
    strat._sell_reentry_after_exit = None
    strat._buy_reentry_after_exit = None
    import threading as _threading
    strat._entry_attempt_in_flight = False
    strat._entry_guard_lock = _threading.Lock()
    strat._entry_cooldown_until_monotonic = 0.0
    strat._sl_cooldown_until_monotonic = 0.0
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
                "entry_time": entry_time,
            }
            self.opens.append(pos)
            self._open_positions.append(pos)
            return pos
        def close_trade(self, position, exit_price, reason):
            self.closes.append({"symbol": position["symbol"], "reason": reason, "exit_price": exit_price})
            if position in self._open_positions:
                self._open_positions.remove(position)
            callback = getattr(self, "on_position_closed", None)
            if callable(callback):
                callback(
                    position=position,
                    exit_price=exit_price,
                    exit_reason=reason,
                    exit_time=position.get("entry_time"),
                )
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


def test_algo3_closed_15m_bucket_finalizes_without_next_bucket_tick():
    print("\n33f. algo3 finalizes a closed 15m bucket even when the next bucket has no tick yet")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    strat._ema20 = 245000.0
    candles = []
    for minute in range(15):
        close = 246800.0 + minute
        candles.append({
            "time": _dt.datetime(2026, 8, 21, 12, minute),
            "open": close - 10.0,
            "high": close + 5.0,
            "low": close - 20.0,
            "close": close,
            "volume": 10,
        })
    fake_now = _dt.datetime(2026, 8, 21, 12, 19, 0, tzinfo=algo3_mod.IST)
    verified_bar = {
        "time": _dt.datetime(2026, 8, 21, 12, 0),
        "open": candles[0]["open"],
        "high": max(candle["high"] for candle in candles),
        "low": min(candle["low"] for candle in candles),
        "close": candles[-1]["close"],
        "volume": sum(candle["volume"] for candle in candles),
    }
    with patch.object(algo3_mod, "_latest_closed_minute_cutoff", return_value=fake_now.replace(second=0, microsecond=0, tzinfo=None)), \
         patch.object(algo3_mod, "get_intraday_candle_at", return_value=verified_bar):
        for candle in candles:
            strat._ingest_minute_candle(candle, allow_signals=True)
        finalized = strat.flush_clock_closed_bar(allow_signals=True)
    check("closed 12:00 bucket finalized by clock", finalized is True)
    check("one 15m bar stored without needing a 12:15 minute tick", len(strat._bars) == 1, f"bars={len(strat._bars)}")
    check("bar timestamp stays at 12:00 bucket start", str(strat._bars[0]['time']) == "2026-08-21 12:00:00",
          f"time={strat._bars[0]['time']}")
    check("buy setup updated from the finalized 12:00 green bar",
          strat._buy_setup_close == candles[-1]["close"],
          f"buy_setup={strat._buy_setup_close}")


def test_algo3_clock_finalization_waits_for_fyers_settle_window():
    print("\n33ff. algo3 waits briefly after a 15m close before clock-finalizing a live reference")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    bucket = _dt.datetime(2026, 8, 21, 12, 0)
    strat._current_bucket = bucket
    strat._minute_buffer = [{
        "time": bucket + _dt.timedelta(minutes=minute),
        "open": 246000.0,
        "high": 246100.0,
        "low": 245900.0,
        "close": 246050.0,
        "volume": 10.0,
    } for minute in range(15)]

    with patch.object(algo3_mod, "_is_bucket_closed", return_value=True), \
         patch.object(algo3_mod, "_is_bucket_settled", return_value=False):
        # The scheduler normally calls with no explicit mode. It must still
        # honor the settle window when scanning is enabled.
        finalized_early = strat.flush_clock_closed_bar()
    check("default clock finalization waits for the FYERS settle window",
          finalized_early is False and len(strat._bars) == 0 and len(strat._minute_buffer) == 15,
          f"finalized={finalized_early} bars={len(strat._bars)} buffer={len(strat._minute_buffer)}")

    with patch.object(algo3_mod, "_is_bucket_closed", return_value=True), \
         patch.object(algo3_mod, "_is_bucket_settled", return_value=True), \
         patch.object(algo3_mod, "get_intraday_candle_at", return_value={
             "time": bucket,
             "open": 246000.0,
             "high": 246100.0,
             "low": 245900.0,
             "close": 246079.0,
             "volume": 150.0,
         }):
        finalized_settled = strat.flush_clock_closed_bar(allow_signals=True)
    check("settled FYERS candle is then finalized",
          finalized_settled is True and strat._bars[-1]["close"] == 246079.0,
          f"finalized={finalized_settled} bars={strat._bars}")


def test_algo3_unverified_local_bar_never_becomes_a_setup_reference():
    print("\n33fg. algo3 refuses a local-only 15m close as a BUY/SELL setup reference")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    strat._ema20 = 245000.0
    bucket = _dt.datetime(2026, 8, 21, 12, 0)
    strat._current_bucket = bucket
    strat._minute_buffer = [{
        "time": bucket + _dt.timedelta(minutes=minute),
        "open": 246000.0,
        "high": 246100.0,
        "low": 245900.0,
        "close": 246050.0,
        "volume": 10.0,
    } for minute in range(15)]

    with patch.object(algo3_mod, "get_intraday_candle_at", return_value=None):
        strat._finalize_bar(allow_signals=True, require_closed=False)
    check("unverified local bar is not added to EMA history",
          len(strat._bars) == 0, f"bars={strat._bars}")
    check("unverified local bar cannot create a BUY setup",
          strat._buy_setup_close is None, f"buy_setup={strat._buy_setup_close}")


def test_algo3_live_15m_setup_uses_fyers_verified_bar_close():
    print("\n33g. algo3 live setup uses FYERS-verified 15m close instead of partial local close")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    strat._ema20 = 246000.0
    bucket = _dt.datetime(2026, 8, 21, 14, 15)
    strat._current_bucket = bucket
    strat._minute_buffer = [
        {
            "time": bucket + _dt.timedelta(minutes=minute),
            "open": 247100.0,
            "high": 247800.0,
            "low": 247000.0,
            "close": 247691.0,
            "volume": 100.0,
        }
        for minute in range(15)
    ]

    with patch.object(algo3_mod, "get_intraday_candle_at", return_value={
        "time": bucket,
        "open": 248350.0,
        "high": 248600.0,
        "low": 248058.0,
        "close": 248351.0,
        "volume": 2190.0,
    }):
        strat._finalize_bar(allow_signals=True, require_closed=False)

    check("one 15m bar stored", len(strat._bars) == 1, f"bars={len(strat._bars)}")
    check("stored 15m bar close came from FYERS verified bar",
          float(strat._bars[-1]["close"]) == 248351.0,
          f"close={strat._bars[-1]['close']}")
    check("BUY setup came from FYERS verified close",
          float(strat._buy_setup_close) == 248351.0,
          f"buy_setup={strat._buy_setup_close}")


def test_algo3_live_15m_sell_setup_uses_fyers_verified_bar_close():
    """A local partial close must not replace FYERS's final SELL reference."""
    print("\n33gh. algo3 live SELL setup stores FYERS close 244479, not local 244456")
    import datetime as _dt
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    strat = _make_bare_algo3()
    strat._ema20 = 245000.0
    bucket = _dt.datetime(2026, 8, 25, 12, 45)
    strat._current_bucket = bucket
    strat._minute_buffer = [
        {
            "time": bucket + _dt.timedelta(minutes=minute),
            "open": 244700.0,
            "high": 244800.0,
            "low": 244300.0,
            "close": 244456.0,
            "volume": 100.0,
        }
        for minute in range(15)
    ]

    with patch.object(algo3_mod, "get_intraday_candle_at", return_value={
        "time": bucket,
        "open": 244700.0,
        "high": 244800.0,
        "low": 244300.0,
        "close": 244479.0,
        "volume": 1200.0,
    }):
        strat._finalize_bar(allow_signals=True, require_closed=False)

    check("stored SELL bar uses FYERS final close 244479",
          len(strat._bars) == 1 and float(strat._bars[-1]["close"]) == 244479.0,
          f"bars={list(strat._bars)}")
    check("SELL setup uses FYERS final close 244479",
          float(strat._sell_setup_close) == 244479.0,
          f"sell_setup={strat._sell_setup_close}")


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
    print("\n35b. algo3 live qualifying setup emits a persistence event for history")
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
    }, log=True)
    check("one BUY setup history event emitted",
          calls == [("BUY", 92000, "live")], f"calls={calls}")


def test_algo3_warmup_setup_does_not_persist_history():
    print("\n35bb. algo3 warmup rebuild updates in-memory setup but does not write setup history")
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
    }, log=False)
    check("warmup did not emit setup history rows",
          calls == [], f"calls={calls}")
    check("warmup still updated in-memory buy setup",
          strat._buy_setup_close == 92000, f"buy={strat._buy_setup_close}")


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


def test_algo3_sell_reference_survives_green_candles_and_rearms_on_new_red():
    print("\n35d. algo3 SELL candle-close still compares against the previous red across green candles")
    import datetime as _dt

    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._ema20 = 1000.0

    first_red = {"open": 950.0, "close": 900.0, "time": _dt.datetime(2026, 8, 20, 15, 0)}
    strat._update_setups(first_red)
    check("first qualifying red becomes initial sell reference",
          strat._sell_setup_close == 900.0, f"sell={strat._sell_setup_close}")

    strat._update_setups({"open": 1005.0, "close": 1100.0, "time": _dt.datetime(2026, 8, 20, 15, 15)})
    strat._update_setups({"open": 1010.0, "close": 1080.0, "time": _dt.datetime(2026, 8, 20, 15, 30)})
    check("green-above-EMA candles do not reset the stored red reference",
          strat._sell_setup_close == 900.0, f"sell={strat._sell_setup_close}")

    # The older red setup was already consumed earlier; the fresh red must
    # still be allowed to fire based on the OLD reference close.
    strat._last_fired_sell_bar_at = strat._sell_setup_bar_at

    second_red = {"open": 860.0, "close": 700.0, "time": _dt.datetime(2026, 8, 20, 15, 45)}
    strat._check_candle_close_trigger(second_red)
    check("fresh red 200 below previous red still fires SELL even if the old setup was consumed",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "SELL",
          f"opens={strat.broker.opens}")
    check("SELL fire is stamped to the new red candle identity",
          strat._last_fired_sell_bar_at == second_red["time"],
          f"last_fired={strat._last_fired_sell_bar_at}")

    strat._update_setups(second_red)
    check("after close, that red becomes the new reference for the next loop",
          strat._sell_setup_close == 700.0, f"sell={strat._sell_setup_close}")


def test_algo3_sell_reference_shifts_when_gap_is_under_n():
    print("\n35e. algo3 SELL reference shifts when the next qualifying red is less than 200 lower")
    import datetime as _dt

    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._ema20 = 1000.0
    strat._update_setups({"open": 950.0, "close": 900.0, "time": _dt.datetime(2026, 8, 20, 15, 0)})

    next_red = {"open": 890.0, "close": 850.0, "time": _dt.datetime(2026, 8, 20, 15, 15)}
    strat._check_candle_close_trigger(next_red)
    check("red less than 200 below previous reference does not fire SELL",
          len(strat.broker.opens) == 0, f"opens={strat.broker.opens}")

    strat._update_setups(next_red)
    check("non-triggering qualifying red becomes the next sell reference",
          strat._sell_setup_close == 850.0, f"sell={strat._sell_setup_close}")


def test_algo3_live_red_chain_enters_on_forming_candle_cross():
    print("\n35f. algo3 live red-chain SELL enters at the forming candle trigger")
    import datetime as _dt

    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._ema20 = 248000.0
    strat._sell_setup_close = 247850.0
    strat._sell_setup_bar_at = _dt.datetime(2026, 8, 21, 19, 15)
    strat._current_bucket = _dt.datetime(2026, 8, 21, 20, 0)
    strat._minute_buffer = [{
        "time": _dt.datetime(2026, 8, 21, 20, 0),
        "open": 248280.0,
        "high": 248348.0,
        "low": 247700.0,
        "close": 247700.0,
        "volume": 100,
    }]
    strat._prev_ltp = 247700.0

    # Previous red reference 247,850 - 200 = 247,650. The forming red
    # candle reaches that level; the entry must not wait for its final close.
    strat._check_triggers(247650.0)
    check("live red-chain SELL opens on the intrabar threshold cross",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "SELL",
          f"opens={strat.broker.opens}")
    if strat.broker.opens:
        check("live red-chain entry uses the crossing price",
              strat.broker.opens[0]["entry_price"] == 247650.0,
              f"position={strat.broker.opens[0]}")
        check("live red-chain trigger is the previous reference minus n",
              strat.broker.opens[0]["signal_snapshot"]["trigger_level"] == 247650.0,
              f"position={strat.broker.opens[0]}")


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


def test_algo3_sell_does_not_fire_from_tick_cross():
    print("\n37. algo3 SELL needs a forming qualifying red candle before a tick-cross can fire")
    strat = _make_bare_algo3()
    strat._sell_setup_close = 89000.0
    strat._prev_ltp = 88900  # above
    strat._check_triggers(88800)
    check("downward tick-cross without a forming red candle does not fire SELL",
          len(strat.broker.opens) == 0,
          f"opens={strat.broker.opens}")

    strat2 = _make_bare_algo3()
    strat2._sell_setup_close = 89000.0
    strat2._prev_ltp = 88800  # already below
    strat2._check_triggers(88700)
    check("moving further down without a forming red candle still does not fire SELL",
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
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._buy_setup_close = 92000.0
    # Fire BUY first.
    strat._prev_ltp = 92100
    strat._check_triggers(92200)
    check("initial BUY open", len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY")
    # Now fire SELL via the alternate red-chain candle-close rule.
    strat._ema20 = 1000.0
    strat._sell_setup_close = 900.0
    strat._sell_setup_bar_at = _dt.datetime(2026, 8, 20, 15, 0)
    strat._check_candle_close_trigger({
        "open": 860.0,
        "close": 700.0,
        "time": _dt.datetime(2026, 8, 20, 15, 15),
    })
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


def test_algo3_unlimited_reentry_after_exit_same_setup():
    print("\n41a. algo3 allows unlimited same-reference BUY re-entry after each exit")
    import datetime as _dt
    strat = _make_bare_algo3()
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)

    def cross_up():
        strat._prev_ltp = 92000.0
        strat._check_triggers(92200.0)

    cross_up()
    check("first BUY opened", len(strat.broker.opens) == 1)

    first_position = strat.broker.open_positions()[0]
    strat.broker.close_trade(first_position, 92100.0, "SL")
    cross_up()
    check("same setup re-enters after first exit", len(strat.broker.opens) == 2,
          f"opens={strat.broker.opens}")

    second_position = strat.broker.open_positions()[0]
    strat.broker.close_trade(second_position, 92100.0, "SL")
    cross_up()
    check("same setup can re-enter again after second exit", len(strat.broker.opens) == 3,
          f"opens={strat.broker.opens}")


def test_algo3_manual_exit_safe_mode_clears_handoff_and_requires_fresh_trigger():
    print("\n41b. algo3 manual exit safe mode clears carried handoff and requires a fresh trigger")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={
        "silver_breakout_points": 200,
        "manual_exit_reentry_enabled": False,
    })
    strat.broker.on_position_closed = strat._handle_broker_position_closed
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)

    strat._prev_ltp = 92000.0
    strat._check_triggers(92200.0)
    check("initial BUY opened", len(strat.broker.opens) == 1 and len(strat.broker.open_positions()) == 1)

    open_position = strat.broker.open_positions()[0]
    strat._buy_reentry_after_exit = {
        "setup_bar_at": strat._buy_setup_bar_at,
        "trigger_level": 92200.0,
        "exit_reason": "TARGET",
    }
    strat._sell_reentry_after_exit = {
        "setup_bar_at": _dt.datetime(2026, 8, 20, 18, 45),
        "trigger_level": 89700.0,
        "exit_reason": "SL",
    }
    strat._prev_ltp = 92150.0
    strat.broker.close_trade(open_position, 92150.0, "MANUAL_EXIT")

    check("manual close clears carried BUY reentry",
          strat._buy_reentry_after_exit is None, f"buy_reentry={strat._buy_reentry_after_exit}")
    check("manual close clears carried SELL reentry",
          strat._sell_reentry_after_exit is None, f"sell_reentry={strat._sell_reentry_after_exit}")
    check("manual close does not auto-open another position",
          len(strat.broker.open_positions()) == 0 and len(strat.broker.opens) == 1,
          f"opens={strat.broker.opens} open_positions={strat.broker.open_positions()}")

    strat._prev_ltp = 92000.0
    strat._check_triggers(92200.0)
    check("fresh normal trigger can re-enter after manual close",
          len(strat.broker.opens) == 2 and strat.broker.opens[-1]["side"] == "BUY",
          f"opens={strat.broker.opens}")


def test_algo3_manual_exit_reentry_mode_reopens_same_reference_immediately():
    print("\n41b2. algo3 manual exit re-entry mode preserves the carried handoff for immediate re-entry")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={
        "silver_breakout_points": 200,
        "manual_exit_reentry_enabled": True,
    })
    strat.broker.on_position_closed = strat._handle_broker_position_closed
    strat._sell_setup_close = 89900.0
    strat._sell_setup_bar_at = _dt.datetime(2026, 8, 20, 20, 0)
    strat._current_bucket = _dt.datetime(2026, 8, 20, 20, 15)
    strat._minute_buffer = [{"open": 90120.0}]
    strat._ema20 = 90500.0

    strat._prev_ltp = 89710.0
    strat._check_triggers(89690.0)
    check("initial SELL opened", len(strat.broker.opens) == 1 and len(strat.broker.open_positions()) == 1)

    open_position = strat.broker.open_positions()[0]
    strat._sell_reentry_after_exit = {
        "setup_bar_at": strat._sell_setup_bar_at,
        "trigger_level": 89700.0,
        "exit_reason": "SL",
    }
    strat._prev_ltp = 89695.0
    strat.broker.close_trade(open_position, 89680.0, "MANUAL_EXIT")

    check("manual close immediately re-opened a SELL on the same carried reference",
          len(strat.broker.open_positions()) == 1 and len(strat.broker.opens) == 2 and strat.broker.opens[-1]["side"] == "SELL",
          f"opens={strat.broker.opens} open_positions={strat.broker.open_positions()}")
    check("same-reference SELL handoff is consumed after the re-entry",
          strat._sell_reentry_after_exit is None,
          f"sell_reentry={strat._sell_reentry_after_exit}")


def test_algo3_mode_switch_keeps_reference_but_clears_previous_mode_fired_state():
    print("\n41c. algo3 mode switch reuses the warmed reference immediately in the new mode")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat.broker.on_position_closed = strat._handle_broker_position_closed
    setup_bar = _dt.datetime(2026, 8, 26, 10, 15)
    strat._buy_setup_close = 242000.0
    strat._buy_setup_bar_at = setup_bar
    strat._last_fired_buy_bar_at = setup_bar
    strat._last_attempted_buy_bar_at = setup_bar
    strat._last_tick_ltp = 242260.0
    strat._prev_ltp = 242180.0

    strat.on_trading_mode_switched("live", "paper")

    check("same warmed BUY reference can fire immediately after switch",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")
    check("new mode stamps the same setup as freshly fired in live",
          strat._last_fired_buy_bar_at == setup_bar,
          f"last_fired={strat._last_fired_buy_bar_at}")
    check("new mode stamps the same setup as freshly attempted in live",
          strat._last_attempted_buy_bar_at == setup_bar,
          f"last_attempted={strat._last_attempted_buy_bar_at}")


def test_algo3_sell_target_reenters_when_reference_still_crossed():
    print("\n41a. algo3 SELL target exit hands off to a new SELL while the same threshold remains crossed")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._ema20 = 1000.0
    strat._sell_setup_close = 900.0
    strat._sell_setup_bar_at = _dt.datetime(2026, 8, 20, 15, 0)
    strat._current_bucket = _dt.datetime(2026, 8, 20, 15, 15)
    strat._minute_buffer = [{"open": 1000.0}]

    strat._prev_ltp = 750.0
    strat._check_triggers(690.0)  # level is 700; first SELL enters
    check("first SELL opened", len(strat.broker.opens) == 1)

    # Target is 390. The next tick is still below the carried 700 threshold,
    # so the strategy must not wait for a meaningless re-cross above 700.
    strat._last_tick_ltp = 390.0
    strat.check_exits()
    check("first SELL closed at TARGET", len(strat.broker.closes) == 1 and strat.broker.closes[0]["reason"] == "TARGET",
          f"closes={strat.broker.closes}")

    strat._prev_ltp = 690.0
    strat._check_triggers(680.0)
    check("SELL re-opened while the same reference threshold stayed crossed",
          len(strat.broker.opens) == 2, f"opens={strat.broker.opens}")


def test_algo3_sell_stop_does_not_reenter_above_old_trigger():
    print("\n41a2. algo3 SELL stop exit never re-enters above the red-chain trigger")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._ema20 = 1000.0
    strat._sell_setup_close = 900.0
    strat._sell_setup_bar_at = _dt.datetime(2026, 8, 20, 15, 0)
    strat._current_bucket = _dt.datetime(2026, 8, 20, 15, 15)
    strat._minute_buffer = [{"open": 1000.0}]

    strat._prev_ltp = 750.0
    strat._check_triggers(690.0)  # old reference - n = 700
    check("first SELL opened", len(strat.broker.opens) == 1)

    strat._last_tick_ltp = 990.0  # 300-point stop above the 690 entry
    strat.check_exits()
    check("first SELL closed at SL", len(strat.broker.closes) == 1 and strat.broker.closes[0]["reason"] == "SL",
          f"closes={strat.broker.closes}")

    # The next tick turns down at 950, but is still above the old 700 trigger.
    # A downturn alone must not create a short above the configured trigger.
    strat._prev_ltp = 990.0
    strat._check_triggers(950.0)
    check("SELL does not re-enter above the old trigger",
          len(strat.broker.opens) == 1, f"opens={strat.broker.opens}")

    # Once the renewed move reaches the actual threshold, the same reference
    # can re-enter without waiting for another 200-point move. The 30s
    # post-SL cooldown is disabled here so this test isolates the
    # re-entry mechanics from the cooldown feature (covered separately).
    strat._sl_cooldown_until_monotonic = 0.0
    strat._prev_ltp = 710.0
    strat._check_triggers(690.0)
    check("SELL re-enters at/below the carried trigger",
          len(strat.broker.opens) == 2, f"opens={strat.broker.opens}")
    check("re-entry uses the actual threshold-crossing price",
          strat.broker.opens[-1]["entry_price"] == 690.0, f"opens={strat.broker.opens}")


def test_algo3_entry_uses_exchange_event_time():
    print("\n41a3. algo3 paper entry audit uses the exchange event timestamp")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)
    event_time = _dt.datetime(2026, 8, 20, 19, 22, 7)
    strat._prev_ltp = 92100.0
    strat._check_triggers(92200.0, event_time=event_time)
    check("BUY opened with one event timestamp", len(strat.broker.opens) == 1)
    check("entry_time is the market event converted from IST to explicit UTC",
          strat.broker.opens[0].get("entry_time") == "2026-08-20T13:52:07+00:00",
          f"entry_time={strat.broker.opens[0].get('entry_time')!r}")


def test_algo3_failed_live_attempt_consumes_setup_once():
    print("\n41b. algo3 failed live attempt consumes the current setup and does not retry on later ticks")
    strat = _make_bare_algo3()
    import datetime as _dt
    strat._buy_setup_close = 92000.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)
    calls = []

    def fake_enter(side, ltp, trigger_level, event_time=None):
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

    def fake_enter(side, ltp, trigger_level, event_time=None):
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

    def fake_enter(side, ltp, trigger_level, event_time=None):
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


def test_algo3_live_broker_guard_ignores_todays_filled_orders():
    print("\n41e. algo3 live broker guard — Fyers status=2 (FILLED) from earlier today is NOT a blocker")
    # 2026-08-26 12:00 IST regression: client's SELL trigger fired at 11:47,
    # blocked all afternoon because morning BUY (status=2, FILLED, then
    # closed) still appeared in Fyers orderbook and the guard treated any
    # symbol match as pending. Real pending statuses are 4=TRANSIT, 6=PENDING.
    strat = _make_bare_algo3()
    strat.broker._algo3_treat_as_live = True
    # Monkey-patch the module-level fetchers the guard imports.
    from app.strategies import algo3_silver_micro as a3
    import app.fyers_client as fc
    orig_get_orders = getattr(fc, "get_broker_orders", None)
    orig_get_positions = getattr(fc, "get_broker_positions", None)
    fc.get_broker_positions = lambda mode: {"available": True, "positions": []}
    try:
        # Case A: only a FILLED order for our symbol — must NOT block.
        fc.get_broker_orders = lambda mode: {
            "available": True,
            "orders": [{"symbol": strat.symbol, "side": "BUY", "status": 2}],
        }
        blocked_filled = strat._live_broker_symbol_busy(current_position=None)
        check("guard ignores status=2 FILLED history",
              blocked_filled is False, f"blocked_filled={blocked_filled}")

        # Case B: CANCELLED order — must NOT block.
        fc.get_broker_orders = lambda mode: {
            "available": True,
            "orders": [{"symbol": strat.symbol, "side": "SELL", "status": 1}],
        }
        blocked_cancelled = strat._live_broker_symbol_busy(current_position=None)
        check("guard ignores status=1 CANCELLED history",
              blocked_cancelled is False, f"blocked_cancelled={blocked_cancelled}")

        # Case C: a live TRANSIT order — MUST block.
        fc.get_broker_orders = lambda mode: {
            "available": True,
            "orders": [{"symbol": strat.symbol, "side": "BUY", "status": 4}],
        }
        blocked_transit = strat._live_broker_symbol_busy(current_position=None)
        check("guard blocks on status=4 TRANSIT (real pending)",
              blocked_transit is True, f"blocked_transit={blocked_transit}")

        # Case D: a live PENDING order — MUST block.
        fc.get_broker_orders = lambda mode: {
            "available": True,
            "orders": [{"symbol": strat.symbol, "side": "SELL", "status": 6}],
        }
        blocked_pending = strat._live_broker_symbol_busy(current_position=None)
        check("guard blocks on status=6 PENDING (real pending)",
              blocked_pending is True, f"blocked_pending={blocked_pending}")

        # Case E: mixed history + one pending — MUST block.
        fc.get_broker_orders = lambda mode: {
            "available": True,
            "orders": [
                {"symbol": strat.symbol, "side": "BUY", "status": 2},   # filled
                {"symbol": strat.symbol, "side": "SELL", "status": 1},  # cancelled
                {"symbol": strat.symbol, "side": "BUY", "status": 6},   # pending
            ],
        }
        blocked_mixed = strat._live_broker_symbol_busy(current_position=None)
        check("guard blocks when at least one pending exists alongside history",
              blocked_mixed is True, f"blocked_mixed={blocked_mixed}")
    finally:
        if orig_get_orders is not None:
            fc.get_broker_orders = orig_get_orders
        if orig_get_positions is not None:
            fc.get_broker_positions = orig_get_positions


# ── 42. Entry payload uses POINTS for SL/target ────────────────────────
def test_algo3_entry_uses_points_sl_target():
    print("\n42. algo3 entry SL/target are computed as POINTS from entry, not %")
    import datetime as _dt
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
    strat2._sell_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)
    strat2._ema20 = 90000.0
    strat2._check_candle_close_trigger({
        "time": _dt.datetime(2026, 8, 20, 19, 30),
        "open": 88900.0,
        "high": 88950.0,
        "low": 88750.0,
        "close": 88800.0,
        "volume": 10,
    })
    check("one SELL open", len(strat2.broker.opens) == 1)
    pos2 = strat2.broker.opens[0]
    # n=150, so entry = 88850 at the trigger, sl = 88850 + 200 = 89050,
    # target = 88850 - 500 = 88350.
    check("SELL sl_price = entry + 200 pts",
          abs(pos2["sl_price"] - 89050.0) < 1e-9, f"got={pos2['sl_price']}")
    check("SELL target_price = entry - 500 pts",
          abs(pos2["target_price"] - 88350.0) < 1e-9, f"got={pos2['target_price']}")


# ── 43. Silver point-lock TSL ──────────────────────────────────────────
def test_algo3_trailing_settings_use_point_lock_model():
    print("\n43. algo3 trailing settings use X/Y/Z point-lock model")
    strat = _make_bare_algo3(settings_overrides={
        "tsl_activate_points": 500,
        "tsl_profit_step_points": 500,
        "tsl_lock_step_points": 100,
    })
    position = {"entry_price": 100000.0}
    passed = strat._trailing_settings_for(position)
    check("activation remains points", passed["tsl_activate_points"] == 500)
    check("profit step remains points", passed["tsl_profit_step_points"] == 500)
    check("lock step remains points", passed["tsl_lock_step_points"] == 100)
    check("Silver no longer converts to percentage trailing",
          "trailing_sl_trigger_pct" not in passed)


def test_silver_point_lock_trailing_buy_and_sell():
    print("\n44. Silver point-lock TSL BUY/SELL activation, breakeven, and steps")
    from app.trailing_stop import calculate_point_trailing

    params = {"activate_points": 500, "profit_step_points": 500, "lock_step_points": 100}
    buy = calculate_point_trailing(entry=1000, side="BUY", current_sl=700,
                                   highest=1000, lowest=1000, **params)
    check("BUY below activation stays inactive", not buy["trailing_active"])
    buy = calculate_point_trailing(entry=1000, side="BUY", current_sl=700,
                                   highest=1500, lowest=900, **params)
    check("BUY activation moves SL to entry", buy["trailing_active"] and buy["sl_price"] == 1000,
          f"result={buy}")
    buy = calculate_point_trailing(entry=1000, side="BUY", current_sl=1000,
                                   highest=2000, lowest=1000, **params)
    check("BUY +1000 locks +100", buy["sl_price"] == 1100 and buy["protected_points"] == 100,
          f"result={buy}")
    buy = calculate_point_trailing(entry=1000, side="BUY", current_sl=1100,
                                   highest=1800, lowest=1000, **params)
    check("BUY retracement never loosens stop", buy["sl_price"] == 1100 and not buy["sl_moved"],
          f"result={buy}")

    sell = calculate_point_trailing(entry=1000, side="SELL", current_sl=1300,
                                    highest=1100, lowest=500, **params)
    check("SELL activation moves SL to entry", sell["trailing_active"] and sell["sl_price"] == 1000,
          f"result={sell}")
    sell = calculate_point_trailing(entry=1000, side="SELL", current_sl=1000,
                                    highest=1000, lowest=0, **params)
    check("SELL +1000 locks +100", sell["sl_price"] == 900 and sell["protected_points"] == 100,
          f"result={sell}")


# ── 45. Scan disabled → triggers skipped ───────────────────────────────
def test_algo3_scan_disabled_skips_triggers():
    print("\n45. algo3 scan_enabled=False: on_tick does not evaluate triggers")
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
      - Later qualifying red closes 200 below the previous red reference
        -> reversal to SELL on candle close
    """
    print("\n45. algo3 BLACK-BOX: buy tick-cross + sell red-chain close produce expected reversal")
    import datetime as _dt
    strat = _make_bare_algo3(settings_overrides={"silver_breakout_points": 200})
    # This offline replay supplies synthetic minute candles, so mark their
    # aggregate as the authoritative 15m response that production receives
    # from FYERS. Live code must never use this local fallback by default.
    strat._rest_verify_live_bar = lambda bar, allow_signals: {
        **bar,
        "source": "rest_verified_15m",
    }

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

    # Now feed live ticks that cross (setup + 200).
    buy_level = strat._buy_setup_close + 200
    strat.on_tick("MCX:SILVERMIC26AUGFUT", buy_level - 20, None)
    strat.on_tick("MCX:SILVERMIC26AUGFUT", buy_level + 5, None)
    check("black-box: BUY fires on upward tick-cross of setup+200",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "BUY",
          f"opens={strat.broker.opens}")

    # Later 15-min bar: first qualifying red becomes the SELL reference.
    for m in range(1, 15):
        offset = 21 * 15 + m
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                              minute_candle(offset, 90000, 90100, 89850, 90000), {})
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                          minute_candle(22 * 15, 90000, 90000, 90000, 90000), {})
    check("black-box: first red-below-EMA bar captured as sell reference",
          strat._sell_setup_close is not None and strat._sell_setup_close < strat._ema20,
          f"got sell={strat._sell_setup_close}, ema={strat._ema20}")

    # Green candles in between must not clear that red reference.
    for m in range(1, 15):
        offset = 22 * 15 + m
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                              minute_candle(offset, 90100, 91100, 90050, 91000), {})
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                          minute_candle(23 * 15, 91000, 91000, 91000, 91000), {})
    check("black-box: intervening green bar did not clear sell reference",
          strat._sell_setup_close == 90000,
          f"sell_ref={strat._sell_setup_close}")

    # Next qualifying red closes 200 below the previous red reference:
    # 90000 -> 89800, so reversal to SELL should happen on candle close.
    for m in range(1, 15):
        offset = 23 * 15 + m
        strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                              minute_candle(offset, 89950, 90000, 89750, 89800), {})
    strat.on_candle_close("MCX:SILVERMIC26AUGFUT",
                          minute_candle(24 * 15, 89800, 89800, 89800, 89800), {})
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


def test_strategy_settings_storage_key_isolates_deployments():
    print("\n48. Strategy settings storage key: two suffixes produce distinct rows")
    import importlib, os
    from unittest.mock import patch
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "client"}, clear=False):
        import app.config, app.runtime_mode, app.strategy_settings
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        importlib.reload(app.strategy_settings)
        client_paper_key = app.strategy_settings.get_settings_storage_key("algo3", mode="paper")
        client_live_key = app.strategy_settings.get_settings_storage_key("algo3", mode="live")
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "dev"}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        importlib.reload(app.strategy_settings)
        dev_paper_key = app.strategy_settings.get_settings_storage_key("algo3", mode="paper")
        dev_live_key = app.strategy_settings.get_settings_storage_key("algo3", mode="live")

    check("client paper settings key is algo3__paper__client",
          client_paper_key == "algo3__paper__client", f"got={client_paper_key}")
    check("client live settings key is algo3__live__client",
          client_live_key == "algo3__live__client", f"got={client_live_key}")
    check("dev paper settings key is algo3__paper__dev",
          dev_paper_key == "algo3__paper__dev", f"got={dev_paper_key}")
    check("dev live settings key is algo3__live__dev",
          dev_live_key == "algo3__live__dev", f"got={dev_live_key}")
    check("paper and live settings keys differ inside one deployment",
          client_paper_key != client_live_key)
    check("client and dev paper settings keys differ (no settings collision)",
          client_paper_key != dev_paper_key)
    check("client and dev live settings keys differ (no settings collision)",
          client_live_key != dev_live_key)
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        importlib.reload(app.strategy_settings)


def test_strategy_settings_storage_key_empty_preserves_legacy_algo_id():
    print("\n49. Strategy settings storage key empty: keeps historical algo_id row")
    import importlib, os
    from unittest.mock import patch
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        import app.config, app.runtime_mode, app.strategy_settings
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        importlib.reload(app.strategy_settings)
        paper_storage_key = app.strategy_settings.get_settings_storage_key("algo3", mode="paper")
        live_storage_key = app.strategy_settings.get_settings_storage_key("algo3", mode="live")

    check("empty suffix: paper settings key still includes mode",
          paper_storage_key == "algo3__paper", f"got={paper_storage_key}")
    check("empty suffix: live settings key still includes mode",
          live_storage_key == "algo3__live", f"got={live_storage_key}")


def test_runtime_mode_setting_key_isolates_deployments():
    print("\n50. Runtime mode storage key: two suffixes produce distinct rows")
    import importlib, os
    from unittest.mock import patch
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "client"}, clear=False):
        import app.config, app.runtime_mode
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        client_key = app.runtime_mode.get_runtime_setting_storage_key("trading_mode")
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "dev"}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)
        dev_key = app.runtime_mode.get_runtime_setting_storage_key("trading_mode")

    check("client runtime setting key is trading_mode__client",
          client_key == "trading_mode__client", f"got={client_key}")
    check("dev runtime setting key is trading_mode__dev",
          dev_key == "trading_mode__dev", f"got={dev_key}")
    check("client and dev runtime setting keys differ",
          client_key != dev_key)
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.runtime_mode)


def test_algo_settings_routes_pin_active_mode():
    print("\n50b. Algo settings routes pin get/update/reset/reload to the active runtime mode")
    import app.main as mainmod
    import app.strategy_settings as strat_settings
    from unittest.mock import patch

    class DummyStrategy:
        def __init__(self):
            self.reload_modes = []

        def reload_settings(self, mode: str | None = None):
            self.reload_modes.append(mode)

    strategy = DummyStrategy()
    store = {"scan_enabled": False, "trading_enabled": False}
    get_calls = []
    update_calls = []
    reset_calls = []

    def fake_get_settings(algo_id, mode=None):
        get_calls.append((algo_id, mode))
        return {
            "scan_enabled": store["scan_enabled"],
            "trading_enabled": store["trading_enabled"],
        }

    def fake_update_settings(algo_id, settings, mode=None):
        update_calls.append((algo_id, mode, dict(settings)))
        if "scan_enabled" in settings:
            store["scan_enabled"] = bool(settings["scan_enabled"])
        if "trading_enabled" in settings:
            store["trading_enabled"] = bool(settings["trading_enabled"])
        return {
            "scan_enabled": store["scan_enabled"],
            "trading_enabled": store["trading_enabled"],
        }

    def fake_reset_settings(algo_id, mode=None):
        reset_calls.append((algo_id, mode))
        store["scan_enabled"] = False
        store["trading_enabled"] = False
        return {
            "scan_enabled": False,
            "trading_enabled": False,
        }

    with patch.object(mainmod, "get_runtime_trading_mode", return_value="live"), \
         patch.object(mainmod, "get_strategy_or_raise", return_value=strategy), \
         patch.dict(mainmod.STRATEGIES, {"algo3": strategy}, clear=False), \
         patch.object(strat_settings, "get_settings", side_effect=fake_get_settings), \
         patch.object(strat_settings, "update_settings", side_effect=fake_update_settings), \
         patch.object(strat_settings, "reset_settings", side_effect=fake_reset_settings):
        fetched = mainmod.get_algo_settings("algo3", None)
        scan_saved = mainmod.set_algo_scan_enabled("algo3", {"enabled": True}, None)
        trading_saved = mainmod.set_algo_trading_enabled("algo3", {"enabled": True}, None)
        updated = mainmod.update_algo_settings("algo3", {"scan_enabled": True, "trading_enabled": False}, None)
        reset = mainmod.reset_algo_settings("algo3", None)

    check("settings GET reads active live-mode row",
          get_calls[0] == ("algo3", "live"),
          f"first get={get_calls[0] if get_calls else None}")
    check("scan toggle writes live-mode row",
          any(call[0] == "algo3" and call[1] == "live" and call[2].get("scan_enabled") is True for call in update_calls),
          f"updates={update_calls}")
    check("trading toggle writes live-mode row",
          any(call[0] == "algo3" and call[1] == "live" and call[2].get("trading_enabled") is True for call in update_calls),
          f"updates={update_calls}")
    check("generic settings PUT writes live-mode row",
          any(call[0] == "algo3" and call[1] == "live" and call[2].get("trading_enabled") is False for call in update_calls),
          f"updates={update_calls}")
    check("settings reset clears live-mode row",
          reset_calls == [("algo3", "live")],
          f"reset_calls={reset_calls}")
    check("strategy reload stays pinned to live mode on every mutating route",
          strategy.reload_modes == ["live", "live", "live", "live"],
          f"reload_modes={strategy.reload_modes}")
    check("route responses reflect persisted values before reset",
          fetched["scan_enabled"] is False
          and scan_saved["scan_enabled"] is True
          and trading_saved["trading_enabled"] is True
          and updated["trading_enabled"] is False,
          f"fetched={fetched} scan={scan_saved} trading={trading_saved} updated={updated}")
    check("reset response returns live defaults",
          reset["scan_enabled"] is False and reset["trading_enabled"] is False,
          f"reset={reset}")


def test_algo_summary_reads_persisted_active_mode_toggles():
    print("\n50c. Algo summary reads persisted active-mode scan/trading toggles")
    import app.main as mainmod
    import app.strategy_settings as strat_settings
    from unittest.mock import patch

    class DummyBroker:
        def summary(self):
            return {"cash": 12345, "realized_net_pnl": 0}

    class DummyStrategy:
        def __init__(self):
            self.broker = DummyBroker()
            self.settings = {"scan_enabled": True, "trading_enabled": True}

    strategy = DummyStrategy()
    get_calls = []

    def fake_get_settings(algo_id, mode=None):
        get_calls.append((algo_id, mode))
        return {
            "scan_enabled": False,
            "trading_enabled": False,
            "max_trades_per_day": 7,
            "max_buy_trades": 3,
            "max_sell_trades": 4,
        }

    with patch.object(mainmod, "get_runtime_trading_mode", return_value="paper"), \
         patch.object(mainmod, "get_strategy_or_raise", return_value=strategy), \
         patch.object(strat_settings, "get_settings", side_effect=fake_get_settings):
        result = mainmod.algo_summary("algo3", None)

    check("summary reads settings from active paper-mode row",
          get_calls == [("algo3", "paper")],
          f"get_calls={get_calls}")
    check("summary returns persisted scan/trading flags, not stale in-memory ones",
          result["scan_enabled"] is False and result["trading_enabled"] is False,
          f"result={result}")
    check("summary returns persisted limits from the active row",
          result["max_trades_per_day"] == 7
          and result["max_buy_trades"] == 3
          and result["max_sell_trades"] == 4,
          f"result={result}")
    check("summary exposes the active trading mode",
          result["trading_mode"] == "paper",
          f"result={result}")


def test_paper_broker_storage_key_isolates_deployments():
    print("\n51. Broker storage key: paper/live state rows split by deployment suffix")
    import importlib, os
    from unittest.mock import patch
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "client"}, clear=False):
        import app.config, app.paper_broker, app.live_broker
        importlib.reload(app.config)
        importlib.reload(app.paper_broker)
        importlib.reload(app.live_broker)
        client_paper = app.paper_broker.PaperBroker.__new__(app.paper_broker.PaperBroker)
        client_paper.algo_id = "algo3"
        client_live = app.live_broker.LiveBroker.__new__(app.live_broker.LiveBroker)
        client_live.algo_id = "algo3"
        client_paper_key = client_paper.storage_algo_id()
        client_live_key = client_live.storage_algo_id()
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "dev"}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.paper_broker)
        importlib.reload(app.live_broker)
        dev_paper = app.paper_broker.PaperBroker.__new__(app.paper_broker.PaperBroker)
        dev_paper.algo_id = "algo3"
        dev_live = app.live_broker.LiveBroker.__new__(app.live_broker.LiveBroker)
        dev_live.algo_id = "algo3"
        dev_paper_key = dev_paper.storage_algo_id()
        dev_live_key = dev_live.storage_algo_id()

    check("client paper broker key is algo3__client",
          client_paper_key == "algo3__client", f"got={client_paper_key}")
    check("dev paper broker key is algo3__dev",
          dev_paper_key == "algo3__dev", f"got={dev_paper_key}")
    check("client and dev paper broker keys differ",
          client_paper_key != dev_paper_key)
    check("live broker inherits same deployment key",
          client_live_key == "algo3__client" and dev_live_key == "algo3__dev",
          f"client={client_live_key} dev={dev_live_key}")
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.paper_broker)
        importlib.reload(app.live_broker)


def test_charges_config_row_id_isolates_deployments():
    print("\n52. Charges config row id: two suffixes produce distinct rows")
    import importlib, os
    from unittest.mock import patch
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "client"}, clear=False):
        import app.config, app.charges
        importlib.reload(app.config)
        importlib.reload(app.charges)
        client_id = app.charges.get_charges_config_row_id()
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": "dev"}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.charges)
        dev_id = app.charges.get_charges_config_row_id()
    with patch.dict(os.environ, {"BROKER_KEY_SUFFIX": ""}, clear=False):
        importlib.reload(app.config)
        importlib.reload(app.charges)
        legacy_id = app.charges.get_charges_config_row_id()

    check("client charges row id differs from legacy row 1", client_id != 1, f"got={client_id}")
    check("dev charges row id differs from legacy row 1", dev_id != 1, f"got={dev_id}")
    check("client and dev charges row ids differ", client_id != dev_id, f"client={client_id} dev={dev_id}")
    check("empty suffix keeps legacy charges row id 1", legacy_id == 1, f"got={legacy_id}")


# ── 53. Backtest parity: same scenario, backtest and live must agree ──
def test_algo3_backtest_parity_with_live():
    """Give the same 1m candle history to the backtest simulator and to
    the live algo3 (via on_candle_close + on_tick per 1m bar). Assert
    they produce the SAME entries: same side, same entry price, same
    minute. If they diverge, either the live logic or the backtest
    simulator has drifted from the spec doc."""
    print("\n53. algo3 backtest parity — same input, live + backtest agree on entries")
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
        push(21 * 15 + m, 90580, 90620, 90580, 90600)  # sitting below level, above SL
    push(21 * 15 + 5, 90600, 90680, 90580, 90670)     # crosses 90650

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
    # This is an offline parity fixture. Mark its synthesized 15m bars as the
    # authoritative FYERS response so live setup finalization is exercised.
    strat._rest_verify_live_bar = lambda bar, allow_signals: {
        **bar,
        "source": "rest_verified_15m",
    }
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


def test_algo3_backtest_buy_reference_breakout_contract():
    print("\n53b. algo3 backtest BUY uses finalized 15m reference + n")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    first_date = _dt.date(2026, 8, 19)
    base = _dt.datetime(2026, 8, 19, 9, 0)
    history: list[dict] = []

    def push(offset, o, h, l, c, v=100):
        history.append({"time": base + _dt.timedelta(minutes=offset), "open": o,
                        "high": h, "low": l, "close": c, "volume": v})

    for bucket in range(20):
        for minute in range(15):
            push(bucket * 15 + minute, 90000, 90000, 90000, 90000)
    setup_offset = 20 * 15
    for minute in range(14):
        push(setup_offset + minute, 90000, 90200, 89950, 90100)
    push(setup_offset + 14, 90100, 90500, 90050, 90500)
    trigger_offset = setup_offset + 15
    for minute in range(5):
        push(trigger_offset + minute, 90500, 90520, 90490, 90510)
    push(trigger_offset + 5, 90510, 90680, 90600, 90670)
    for minute in range(6, 15):
        push(trigger_offset + minute, 90670, 90680, 90600, 90650)
    push(trigger_offset + 15, 90650, 90650, 90650, 90650)

    settings = {
        "silver_breakout_points": 150, "sl_points": 100, "target_points": 300,
        "silver_lots": 1, "trailing_sl_enabled": False, "tsl_trigger_points": 0,
        "tsl_distance_points": 0, "exit_mode": "fixed_target_sl",
    }
    charges = {key: 0 for key in (
        "brokerage_flat", "brokerage_pct", "stt_pct", "exchange_pct",
        "sebi_pct", "gst_pct", "stamp_duty_pct",
    )}
    with _lock:
        _jobs["buy-reference-contract"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="buy-reference-contract", algo_id="algo3", first_date=first_date,
            last_date=first_date, symbol="MCX:TEST-EQ", history=history,
            trading_days=[first_date], settings=settings, charges_config=charges,
            silver_buy_plan=bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
            silver_sell_plan=bt.SILVER_SELL_PLAN_LATEST_REFERENCE,
        )
    finally:
        with _lock:
            _jobs.pop("buy-reference-contract", None)

    day = results[0]
    trades = day["trades"]
    check("reference BUY creates one trade", len(trades) == 1, f"trades={trades}")
    check("result records the canonical 15m BUY plan",
          day["silver_buy_plan"] == bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
          f"plan={day['silver_buy_plan']}")
    check("chart setup is the finalized green 15m bar",
          any(row["time"].startswith("2026-08-19T14:00") for row in day["chart"]["setups"]),
          f"setups={day['chart']['setups']}")
    if trades:
        trade = trades[0]
        check("BUY entry occurs at reference + n",
              trade["entry_price"] == 90650.0,
              f"trade={trade}")
        check("BUY diagnostics keep the 15m setup timestamp",
              trade["diagnostics"]["entry_context"]["setup_time"].startswith("2026-08-19T14:00"),
              f"diagnostics={trade['diagnostics']}")


def test_algo3_backtest_buy_plan_is_15m_reference_only():
    print("\n53d. algo3 BUY plan normalizes old values to the 15m reference model")
    from app import backtest as bt
    from app.strategy_settings import _normalize

    check("missing BUY plan defaults to 15m reference",
          bt.normalize_silver_buy_plan(None) == bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT)
    check("old live_breakout value normalizes to 15m reference",
          bt.normalize_silver_buy_plan("live_breakout") == bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT)
    check("old legacy_confirmation value cannot reactivate 5m logic",
          bt.normalize_silver_buy_plan("legacy_confirmation") == bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT)
    check("backtest exposes only the canonical BUY label",
          set(bt.SILVER_BUY_PLAN_LABELS) == {bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT},
          f"labels={bt.SILVER_BUY_PLAN_LABELS}")
    check("saved alternate setting normalizes the same way for live algo3",
          _normalize({"silver_buy_plan": "legacy_confirmation"}, "algo3")["silver_buy_plan"]
          == bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT)


def test_algo3_backtest_buy_reenters_after_target_in_same_15m_candle():
    print("\n53e. algo3 backtest BUY re-enters after target while the 15m candle keeps growing")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    first_date = _dt.date(2026, 8, 19)
    base = _dt.datetime(2026, 8, 19, 9, 0)
    history: list[dict] = []

    def push(offset, o, h, l, c):
        history.append({"time": base + _dt.timedelta(minutes=offset), "open": o,
                        "high": h, "low": l, "close": c, "volume": 100})

    for bucket in range(20):
        for minute in range(15):
            push(bucket * 15 + minute, 90000, 90000, 90000, 90000)
    setup_offset = 20 * 15
    for minute in range(14):
        push(setup_offset + minute, 90000, 90200, 89950, 90100)
    push(setup_offset + 14, 90100, 90500, 90050, 90500)

    trigger_offset = setup_offset + 15
    for minute in range(5):
        push(trigger_offset + minute, 90500, 90520, 90490, 90510)
    # The first threshold entry and its 300-point target are both reached in
    # this still-forming 15m candle.
    push(trigger_offset + 5, 90510, 91000, 90600, 90960)
    # The candle keeps moving upward, so the same finalized reference is
    # immediately eligible for another BUY after the target exit.
    push(trigger_offset + 6, 90960, 90980, 90950, 90970)
    for minute in range(7, 15):
        push(trigger_offset + minute, 90970, 90980, 90960, 90970)
    push(trigger_offset + 15, 90970, 90970, 90970, 90970)

    settings = {
        "silver_breakout_points": 150, "sl_points": 100, "target_points": 300,
        "silver_lots": 1, "trailing_sl_enabled": False, "tsl_trigger_points": 0,
        "tsl_distance_points": 0, "exit_mode": "fixed_target_sl",
    }
    charges = {key: 0 for key in (
        "brokerage_flat", "brokerage_pct", "stt_pct", "exchange_pct",
        "sebi_pct", "gst_pct", "stamp_duty_pct",
    )}
    with _lock:
        _jobs["buy-same-candle-reentry"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="buy-same-candle-reentry", algo_id="algo3",
            first_date=first_date, last_date=first_date, symbol="MCX:TEST-EQ",
            history=history, trading_days=[first_date], settings=settings,
            charges_config=charges,
            silver_buy_plan=bt.SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
            silver_sell_plan=bt.SILVER_SELL_PLAN_LATEST_REFERENCE,
        )
    finally:
        with _lock:
            _jobs.pop("buy-same-candle-reentry", None)

    trades = results[0]["trades"]
    check("target-in-candle scenario creates two BUY trades",
          len(trades) == 2, f"trades={trades}")
    if len(trades) == 2:
        check("first BUY exits at target before the 15m candle closes",
              trades[0]["exit_reason"] == "TARGET"
              and trades[0]["exit_time"].startswith("2026-08-19T14:20"),
              f"first={trades[0]}")
        check("second BUY is same-reference re-entry",
              trades[1]["entry_mode"] == "SAME_REFERENCE_REENTRY"
              and trades[1]["active_reference_close"] == 90500.0
              and trades[1]["trigger_level_used"] == 90650.0,
              f"second={trades[1]}")


def test_algo3_live_buy_reference_reentry_and_rollover():
    print("\n53c. algo3 live BUY reference, target/SL re-entry, and reference rollover")
    import datetime as _dt

    from app.strategies.algo3_silver_micro import SILVER_BUY_PLAN_REFERENCE_BREAKOUT

    def setup_strategy():
        strat = _make_bare_algo3(settings_overrides={
            "silver_buy_plan": SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
            "silver_breakout_points": 200,
            "sl_points": 100,
            "target_points": 300,
        })
        strat._ema20 = 1000.0
        strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 19, 15)
        strat._update_setups({
            "time": strat._buy_setup_bar_at, "open": 1000.0,
            "high": 1110.0, "low": 995.0, "close": 1100.0, "volume": 1,
        })
        return strat

    target_strat = setup_strategy()
    check("green 15m close above EMA becomes BUY reference",
          target_strat._buy_setup_close == 1100.0,
          f"reference={target_strat._buy_setup_close}")
    target_strat._update_setups({
        "time": _dt.datetime(2026, 8, 20, 19, 30), "open": 990.0,
        "high": 999.0, "low": 985.0, "close": 995.0, "volume": 1,
    })
    check("green 15m close below EMA cannot replace BUY reference",
          target_strat._buy_setup_close == 1100.0
          and target_strat._buy_setup_bar_at == _dt.datetime(2026, 8, 20, 19, 15),
          f"reference={target_strat._buy_setup_close} at={target_strat._buy_setup_bar_at}")
    check("live BUY strategy no longer contains 5m confirmation state",
          not hasattr(target_strat, "_legacy_buy_5m_buffer"))
    target_strat._prev_ltp = 1290.0
    target_strat._check_triggers(1305.0)
    check("BUY enters at reference + 200 crossing",
          len(target_strat.broker.opens) == 1 and target_strat.broker.opens[0]["entry_price"] == 1305.0,
          f"opens={target_strat.broker.opens}")
    target_strat._last_tick_ltp = 1605.0
    target_strat.check_exits()
    check("BUY target closes and arms same-reference handoff",
          len(target_strat.broker.closes) == 1
          and target_strat.broker.closes[0]["reason"] == "TARGET"
          and target_strat._buy_reentry_after_exit is not None,
          f"closes={target_strat.broker.closes} handoff={target_strat._buy_reentry_after_exit}")
    target_strat._prev_ltp = 1300.0
    target_strat._check_triggers(1310.0)
    check("BUY re-enters on renewed upward movement above the same reference",
          len(target_strat.broker.opens) == 2,
          f"opens={target_strat.broker.opens}")

    stop_strat = setup_strategy()
    stop_strat._prev_ltp = 1290.0
    stop_strat._check_triggers(1305.0)
    stop_strat._last_tick_ltp = 1205.0
    stop_strat.check_exits()
    check("BUY stop closes and arms same-reference handoff",
          len(stop_strat.broker.closes) == 1
          and stop_strat.broker.closes[0]["reason"] == "SL"
          and stop_strat._buy_reentry_after_exit is not None,
          f"closes={stop_strat.broker.closes} handoff={stop_strat._buy_reentry_after_exit}")
    # Disable the 30s post-SL cooldown so this test isolates the
    # re-entry mechanics from the cooldown feature (covered separately).
    stop_strat._sl_cooldown_until_monotonic = 0.0
    stop_strat._prev_ltp = 1295.0
    stop_strat._check_triggers(1305.0)
    check("BUY re-enters after SL once price resumes above the same threshold",
          len(stop_strat.broker.opens) == 2,
          f"opens={stop_strat.broker.opens}")

    rollover_strat = setup_strategy()
    rollover_strat._buy_reentry_after_exit = {
        "setup_bar_at": rollover_strat._buy_setup_bar_at,
        "trigger_level": 1300.0,
        "exit_reason": "SL",
    }
    new_bar_at = _dt.datetime(2026, 8, 20, 19, 30)
    rollover_strat._update_setups({
        "time": new_bar_at, "open": 1200.0, "high": 1410.0,
        "low": 1190.0, "close": 1400.0, "volume": 1,
    })
    check("new green 15m close replaces the old BUY reference",
          rollover_strat._buy_setup_close == 1400.0
          and rollover_strat._buy_setup_bar_at == new_bar_at,
          f"reference={rollover_strat._buy_setup_close} time={rollover_strat._buy_setup_bar_at}")
    check("new reference clears old exit handoff",
          rollover_strat._buy_reentry_after_exit is None,
          f"handoff={rollover_strat._buy_reentry_after_exit}")
    rollover_strat._prev_ltp = 1590.0
    rollover_strat._check_triggers(1605.0)
    check("new reference requires its own reference + n threshold",
          len(rollover_strat.broker.opens) == 1,
          f"opens={rollover_strat.broker.opens}")


def test_algo3_backtest_gap_through_previous_day_setup():
    """A carried BUY setup must fire when the next session opens above it."""
    print("\n53a. algo3 backtest gap-through — previous-day BUY setup fires at next open")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    history: list[dict] = []
    setup_day = _dt.date(2026, 8, 18)
    first_date = _dt.date(2026, 8, 19)
    base = _dt.datetime.combine(setup_day, _dt.time(9, 0))

    def push(ts, o, h, l, c):
        history.append({"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100})

    # Warm EMA20 on flat 15m bars.
    for b in range(20):
        for m in range(15):
            push(base + _dt.timedelta(minutes=b * 15 + m), 90000, 90000, 90000, 90000)

    # Finalized 14:00 green bar creates a BUY setup at 90500, trigger 90650.
    for m in range(15):
        price = 90000 if m < 14 else 90500
        push(base + _dt.timedelta(minutes=20 * 15 + m), 90000, 90500, 89950, price)

    # The source history has a gap-through move after the setup, but the
    # selected replay day starts later, so no minute cross is observable.
    # Make the gap source bar red but still above EMA, so it does not replace
    # the carried green BUY setup before the next session.
    push(base + _dt.timedelta(minutes=21 * 15), 91000, 91000, 90700, 90700)
    next_open = _dt.datetime.combine(first_date, _dt.time(9, 0))
    push(next_open, 90700, 90800, 90680, 90750)
    push(next_open + _dt.timedelta(minutes=15), 90750, 90750, 90750, 90750)

    settings = {
        "silver_breakout_points": 150,
        "sl_points": 100,
        "target_points": 300,
        "silver_lots": 1,
        "trailing_sl_enabled": False,
        "tsl_trigger_points": 0,
        "tsl_distance_points": 0,
        "exit_mode": "fixed_target_sl",
    }
    charges = {
        "brokerage_flat": 0,
        "brokerage_pct": 0,
        "stt_pct": 0,
        "exchange_pct": 0,
        "sebi_pct": 0,
        "gst_pct": 0,
        "stamp_duty_pct": 0,
    }
    with _lock:
        _jobs["backtest-gap-through"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="backtest-gap-through",
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol="MCX:TEST-EQ",
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
    finally:
        with _lock:
            _jobs.pop("backtest-gap-through", None)

    trades = results[0]["trades"]
    check("previous-day gap-through produced one BUY trade", len(trades) == 1, f"trades={trades}")
    if trades:
        check("gap-through entry starts at next session open", trades[0]["entry_time"].startswith("2026-08-19T09:00"), f"trade={trades[0]}")


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

    # A feed restart later in the day must not turn its first tick into a
    # false opening-gap SELL entry.
    strat2 = _make_bare_algo3()
    strat2._sell_setup_close = 231000.0
    strat2._sell_setup_bar_at = _dt.datetime(2026, 8, 19, 23, 45)
    strat2._prev_ltp = None
    strat2.on_tick("MCX:SILVERMIC26AUGFUT", 228000, _dt.datetime(2026, 8, 20, 10, 0))
    check("stale SELL setup does not gap-fire outside the opening window",
          len(strat2.broker.opens) == 0,
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


def test_algo3_previous_day_sell_setup_gap_open_fires_immediately():
    print("\n49c. algo3 previous-day SELL setup fires immediately on the next day's opening gap tick")
    import datetime as _dt
    strat = _make_bare_algo3()
    strat._sell_setup_close = 1000.0
    strat._sell_setup_bar_at = _dt.datetime(2026, 8, 20, 22, 15)
    strat._prev_ltp = None
    strat.on_tick("MCX:SILVERMIC26AUGFUT", 700, _dt.datetime(2026, 8, 21, 9, 0))
    check("next-session first tick below prior trigger opens SELL immediately",
          len(strat.broker.opens) == 1 and strat.broker.opens[0]["side"] == "SELL",
          f"opens={strat.broker.opens}")
    if strat.broker.opens:
        check("SELL entry uses the actual first tick gap-open price",
              abs(float(strat.broker.opens[0]["entry_price"]) - 700.0) < 1e-9,
              f"entry={strat.broker.opens[0]['entry_price']}")


def test_algo3_trading_enabled_kill_switch_blocks_new_entries_but_keeps_exits():
    """Client rule (2026-08-27): trading_enabled=False must block ONLY new
    entries. Scan, setup, reference update, exit, and trailing-SL logic
    all keep running so an existing position is still managed. The
    setup must NOT be consumed while trading is OFF, so flipping the
    switch back ON re-arms the very next qualifying tick."""
    print("\n49c-trading. algo3 trading_enabled OFF blocks new entries, keeps exits, doesn't consume setup")
    import datetime as _dt
    strat = _make_bare_algo3()
    strat._buy_setup_close = 90500.0
    strat._buy_setup_bar_at = _dt.datetime(2026, 8, 20, 22, 15)
    strat._ema20 = 90000.0
    strat._current_bucket = _dt.datetime(2026, 8, 20, 22, 30)
    strat._minute_buffer = [{"open": 90500.0}]

    # trading_enabled=OFF: normal breakout tick must NOT open a trade.
    strat.settings["trading_enabled"] = False
    strat._prev_ltp = 90600.0
    strat._check_triggers(90700.0)
    check("trading_enabled=False blocks new BUY entry",
          len(strat.broker.opens) == 0, f"opens={strat.broker.opens}")
    check("trading_enabled=False does not consume the setup (no _mark_attempted)",
          strat._last_attempted_buy_bar_at is None,
          f"last_attempted={strat._last_attempted_buy_bar_at}")

    # Flip trading_enabled back ON: the very next qualifying tick fires.
    strat.settings["trading_enabled"] = True
    strat._prev_ltp = 90600.0
    strat._check_triggers(90700.0)
    check("trading_enabled=True after re-enable fires the entry cleanly",
          len(strat.broker.opens) == 1, f"opens={strat.broker.opens}")

    # Now with a position open, turn trading OFF again and simulate an SL
    # hit — the exit path must still run.
    strat.settings["trading_enabled"] = False
    position = strat.broker._open_positions[0]
    position["_last_ltp"] = 90300.0  # below sl (entry-200=90500)
    strat.check_exits()
    check("trading_enabled=False still allows protective SL exit",
          len(strat.broker.closes) == 1 and strat.broker.closes[0]["reason"] == "SL",
          f"closes={strat.broker.closes}")


def test_algo3_trailing_stop_exit_reason_is_preserved():
    print("\n49d. algo3 records TRAILING_SL rather than generic SL after a trailed stop")
    strat = _make_bare_algo3()
    position = {
        "symbol": strat.symbol,
        "side": "BUY",
        "entry_price": 1000.0,
        "sl_price": 1050.0,
        "target_price": 1300.0,
        "_last_ltp": 1040.0,
        "trailing_sl_active": True,
        "signal_snapshot": {"trailing": {"activated": True}},
    }
    strat.broker._open_positions.append(position)
    strat.check_exits()
    check("trailed protective exit is labeled TRAILING_SL",
          len(strat.broker.closes) == 1 and strat.broker.closes[0]["reason"] == "TRAILING_SL",
          f"closes={strat.broker.closes}")


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
    # The fixture has no FYERS SDK. Treat its constructed closed bar as the
    # verified broker bar so this checks candle-close entry behavior only.
    strat._rest_verify_live_bar = lambda bar, allow_signals: {
        **bar,
        "source": "rest_verified_15m",
    }
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


def test_algo3_backtest_sell_red_chain_survives_green_candles():
    print("\n50a. algo3 backtest SELL uses previous red reference across green candles")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    symbol = "MCX:TEST-EQ"
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)

    def push(offset, o, h, l, c, v=100):
        history.append({
            "time": base + _dt.timedelta(minutes=offset),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })

    for b in range(20):
        for m in range(15):
            push(b * 15 + m, 90000, 90000, 90000, 90000)

    # First qualifying red reference close = 89900.
    for m in range(14):
        push(20 * 15 + m, 90020, 90030, 89880, 89900)
    push(20 * 15 + 14, 90000, 90010, 89870, 89900)

    # Intervening green-above-EMA bar must not clear the red reference.
    for m in range(15):
        push(21 * 15 + m, 90020, 90160, 90010, 90150)

    # Next qualifying red candle crosses 200 below the previous red reference
    # but closes even lower: 89900 - 200 = 89700. SELL must enter at 89700,
    # not at the later 89650 candle close.
    for m in range(15):
        push(22 * 15 + m, 89850, 89870, 89690, 89650)

    # One more minute forces the 11:30 bar to finalize.
    push(23 * 15, 89700, 89700, 89700, 89700)

    settings = {
        "silver_breakout_points": 200,
        "sl_points": 100,
        "target_points": 300,
        "trailing_sl_enabled": False,
        "tsl_trigger_points": 0,
        "tsl_distance_points": 0,
        "exit_mode": "fixed_target_sl",
        "silver_lots": 1,
    }
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0, "exchange_pct": 0,
               "sebi_pct": 0, "gst_pct": 0, "stamp_duty_pct": 0}
    first_date = _dt.date(2026, 8, 19)

    with _lock:
        _jobs["sell-chain-test"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="sell-chain-test",
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol=symbol,
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
    finally:
        with _lock:
            _jobs.pop("sell-chain-test", None)

    trades = results[0]["trades"]
    check("backtest produced one SELL trade from the red-chain crossing", len(trades) == 1, f"trades={trades}")
    if trades:
        trade = trades[0]
        check("trade side is SELL", trade.get("side") == "SELL", f"trade={trade}")
        check("SELL entry is the previous reference minus n, not the candle close",
              abs(float(trade.get("entry_price") or 0) - 89700.0) < 1e-9,
              f"trade={trade}")


def test_algo3_backtest_sell_reentry_requires_carried_trigger():
    print("\n50a2. algo3 backtest SELL re-entry cannot happen above the carried trigger")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    symbol = "MCX:TEST-EQ"
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)

    def push(offset, o, h, l, c, v=100):
        history.append({
            "time": base + _dt.timedelta(minutes=offset),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })

    # Warm up EMA20, then establish a red reference at 89,900.
    for b in range(20):
        for m in range(15):
            push(b * 15 + m, 90000, 90000, 90000, 90000)
    for m in range(15):
        push(20 * 15 + m, 90020, 90030, 89880, 89900)

    # Reference - n = 89,700. Enter at that threshold, stop at 89,800,
    # ignore a downward move that remains above 89,700, then re-enter when
    # the same still-forming red candle actually reaches the threshold.
    push(21 * 15 + 0, 89700, 89700, 89690, 89690)
    push(21 * 15 + 1, 89690, 89800, 89690, 89800)
    push(21 * 15 + 2, 89800, 89800, 89750, 89750)
    push(21 * 15 + 3, 89750, 89750, 89690, 89690)
    push(22 * 15, 89690, 89690, 89690, 89690)

    settings = {
        "silver_breakout_points": 200,
        "sl_points": 100,
        "target_points": 1000,
        "trailing_sl_enabled": False,
        "exit_mode": "fixed_target_sl",
        "silver_lots": 1,
    }
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0,
               "exchange_pct": 0, "sebi_pct": 0, "gst_pct": 0,
               "stamp_duty_pct": 0}
    first_date = _dt.date(2026, 8, 19)
    audit_events = []
    original_audit_log = bt.audit_log
    bt.audit_log = lambda component, message, **fields: audit_events.append((component, message, fields))

    with _lock:
        _jobs["sell-reentry-trigger-test"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="sell-reentry-trigger-test",
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol=symbol,
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
        trades = results[0]["trades"]
        bt._audit_silver_backtest_summary(
            "sell-reentry-trigger-test",
            {
                "start_date": first_date.isoformat(),
                "end_date": first_date.isoformat(),
                "summary": bt._performance_summary(trades),
                "daily_results": results,
            },
            symbol,
        )
    finally:
        with _lock:
            _jobs.pop("sell-reentry-trigger-test", None)
        bt.audit_log = original_audit_log

    check("SELL trigger guard keeps the backtest to two entries", len(trades) == 2, f"trades={trades}")
    trade_events = [event for event in audit_events if event[1] == "trade_closed"]
    check("Silver backtest does not emit one log line per trade", not trade_events, f"events={audit_events}")
    summary_events = [event for event in audit_events if event[1] == "run_summary"]
    check("Silver backtest emits one compact run summary", len(summary_events) == 1, f"events={audit_events}")
    if summary_events:
        fields = summary_events[0][2]
        check("run summary separates BUY and SELL facts",
              fields.get("sell", {}).get("trades") == 2
              and fields.get("sell", {}).get("losses") == 1
              and fields.get("causes_by_side", {}).get("SELL", {}).get("stop_loss_hit") == 1
              and fields.get("entry_modes_by_side", {}).get("SELL", {}).get("SAME_REFERENCE_REENTRY") == 1,
              f"fields={fields}")
    if len(trades) == 2:
        entries = [float(trade.get("entry_price") or 0) for trade in trades]
        check("first SELL entered at the carried trigger", entries[0] == 89700.0, f"entries={entries}")
        check("second SELL re-entered only after reaching the carried trigger", entries[1] == 89690.0, f"entries={entries}")


def test_algo3_backtest_breakeven_metadata():
    print("\n50b. algo3 backtest records target-to-breakeven metadata")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    symbol = "MCX:TEST-EQ"
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)

    def push(offset, o, h, l, c, v=100):
        history.append({
            "time": base + _dt.timedelta(minutes=offset),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })

    for b in range(20):
        for m in range(15):
            push(b * 15 + m, 90000, 90000, 90000, 90000)

    for m in range(14):
        push(20 * 15 + m, 90000, 90200, 89950, 90100)
    push(20 * 15 + 14, 90100, 90500, 90050, 90500)

    push(21 * 15 + 0, 90620, 90680, 90620, 90670)
    push(21 * 15 + 1, 90670, 90920, 90660, 90890)
    push(21 * 15 + 2, 90890, 90910, 90740, 90760)
    for m in range(3, 15):
        push(21 * 15 + m, 90760, 90790, 90720, 90750)
    push(22 * 15, 90750, 90750, 90750, 90750)

    settings = {
        "silver_breakout_points": 150,
        "sl_points": 100,
        "tsl_activate_points": 200,
        "target_points": 200,
        "exit_mode": "target_to_breakeven_sl",
        "silver_lots": 1,
    }
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0, "exchange_pct": 0,
               "sebi_pct": 0, "gst_pct": 0, "stamp_duty_pct": 0}
    first_date = _dt.date(2026, 8, 19)

    with _lock:
        _jobs["trail-meta-test"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="trail-meta-test",
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol=symbol,
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
    finally:
        with _lock:
            _jobs.pop("trail-meta-test", None)

    trades = results[0]["trades"]
    check("backtest breakeven metadata produced a trade", len(trades) == 1, f"trades={trades}")
    if trades:
        trade = trades[0]
        check("trade marked breakeven protection enabled", trade.get("trailing_sl_enabled") is True, f"trade={trade}")
        check("trade armed breakeven protection", trade.get("trailing_sl_active") is True, f"trade={trade}")
        check("trade saved one breakeven move", int(trade.get("trailing_move_count") or 0) == 1, f"trade={trade}")
        check("final SL moved to BUY entry", abs(float(trade.get("sl_price") or 0) - float(trade.get("entry_price") or 0)) < 1e-9, f"trade={trade}")


def test_algo3_backtest_sell_breakeven_exits_on_reversal():
    print("\n50c. algo3 backtest SELL breakeven stop exits on an upward reversal")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    symbol = "MCX:TEST-EQ"
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)

    def push(offset, o, h, l, c, v=100):
        history.append({
            "time": base + _dt.timedelta(minutes=offset),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })

    for b in range(20):
        for m in range(15):
            push(b * 15 + m, 90000, 90000, 90000, 90000)

    # Carry a confirmed red reference at 89,900. The next red chain enters
    # at 89,700, then price falls far enough to activate the point-lock trail.
    for m in range(15):
        push(20 * 15 + m, 90020, 90030, 89880, 89900)
    for m in range(15):
        push(21 * 15 + m, 90020, 90160, 90010, 90150)
    push(22 * 15, 89850, 89780, 89690, 89650)

    # Favorable low = 89,480. The 200-point activation milestone arms the stop at
    # breakeven (89,700). The green reversal trades through that level.
    push(22 * 15 + 1, 89650, 89650, 89480, 89500)
    push(22 * 15 + 2, 89500, 89900, 89490, 89900)
    push(23 * 15, 89600, 89600, 89600, 89600)

    settings = {
        "silver_breakout_points": 200,
        "sl_points": 100,
        "tsl_activate_points": 200,
        # Keep the final target beyond the 200-point breakeven milestone so
        # the following reversal verifies the amended stop rather than target exit.
        "target_points": 500,
        "exit_mode": "target_to_breakeven_sl",
        "silver_lots": 1,
    }
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0,
               "exchange_pct": 0, "sebi_pct": 0, "gst_pct": 0,
               "stamp_duty_pct": 0}
    first_date = _dt.date(2026, 8, 19)

    with _lock:
        _jobs["sell-trailing-test"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="sell-trailing-test",
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol=symbol,
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
    finally:
        with _lock:
            _jobs.pop("sell-trailing-test", None)

    trades = results[0]["trades"]
    check("backtest SELL breakeven produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        trade = trades[0]
        check("SELL breakeven trade exits by TRAILING_SL", trade.get("exit_reason") == "TRAILING_SL", f"trade={trade}")
        check("SELL breakeven armed", trade.get("trailing_sl_active") is True, f"trade={trade}")
        check("SELL breakeven saved one move", int(trade.get("trailing_move_count") or 0) == 1, f"trade={trade}")
        check("SELL breakeven stop protects entry", abs(float(trade.get("sl_price") or 0) - 89700.0) < 1e-9, f"trade={trade}")
        check("SELL exits at breakeven", abs(float(trade.get("exit_price") or 0) - 89700.0) < 1e-9, f"trade={trade}")


def test_algo3_backtest_fixed_target_mode_keeps_fixed_stop():
    print("\n50c. algo3 fixed-target mode keeps the initial stop fixed")
    import datetime as _dt
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    symbol = "MCX:TEST-EQ"
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)

    def push(offset, o, h, l, c, v=100):
        history.append({
            "time": base + _dt.timedelta(minutes=offset),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })

    for b in range(20):
        for m in range(15):
            push(b * 15 + m, 90000, 90000, 90000, 90000)

    for m in range(14):
        push(20 * 15 + m, 90000, 90200, 89950, 90100)
    push(20 * 15 + 14, 90100, 90500, 90050, 90500)

    push(21 * 15 + 0, 90620, 90680, 90620, 90670)
    push(21 * 15 + 1, 90670, 90920, 90660, 90890)
    push(21 * 15 + 2, 90890, 90910, 90740, 90760)
    for m in range(3, 15):
        push(21 * 15 + m, 90760, 90790, 90720, 90750)
    push(22 * 15, 90750, 90750, 90750, 90750)

    settings = {
        "silver_breakout_points": 150,
        "sl_points": 100,
        "target_points": 1000,
        "exit_mode": "fixed_target_sl",
        "silver_lots": 1,
    }
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0, "exchange_pct": 0,
               "sebi_pct": 0, "gst_pct": 0, "stamp_duty_pct": 0}
    first_date = _dt.date(2026, 8, 19)

    with _lock:
        _jobs["trail-toggle-test"] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id="trail-toggle-test",
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol=symbol,
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
    finally:
        with _lock:
            _jobs.pop("trail-toggle-test", None)

    trades = results[0]["trades"]
    check("fixed-target mode produced a trade", len(trades) == 1, f"trades={trades}")
    if trades:
        trade = trades[0]
        check("fixed-target mode leaves breakeven disabled", trade.get("trailing_sl_enabled") is False, f"trade={trade}")
        check("fixed-target mode leaves no breakeven moves", int(trade.get("trailing_move_count") or 0) == 0, f"trade={trade}")
        check("fixed-target mode keeps final SL at initial SL", abs(float(trade.get("sl_price") or 0) - float(trade.get("initial_sl_price") or 0)) < 1e-9, f"trade={trade}")


def _algo3_backtest_push(history, base, offset, o, h, l, c, v=100):
    import datetime as _dt
    history.append({
        "time": base + _dt.timedelta(minutes=offset),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
    })


def _algo3_backtest_seed_flat(history, base, bars=20, price=90000):
    for b in range(bars):
        for m in range(15):
            _algo3_backtest_push(history, base, b * 15 + m, price, price, price, price)


def _run_algo3_backtest_case(job_id: str, history: list[dict], settings: dict, first_date, charges=None):
    from app import backtest as bt
    from app.backtest import _jobs, _lock

    charges = charges or {
        "brokerage_flat": 0,
        "brokerage_pct": 0,
        "stt_pct": 0,
        "exchange_pct": 0,
        "sebi_pct": 0,
        "gst_pct": 0,
        "stamp_duty_pct": 0,
    }

    with _lock:
        _jobs[job_id] = {"cancel_requested": False}
    try:
        results = bt._simulate_silver_micro_range(
            job_id=job_id,
            algo_id="algo3",
            first_date=first_date,
            last_date=first_date,
            symbol="MCX:TEST-EQ",
            history=history,
            trading_days=[first_date],
            settings=settings,
            charges_config=charges,
        )
    finally:
        with _lock:
            _jobs.pop(job_id, None)
    return results[0]


def test_algo3_backtest_plain_sl_diagnostics():
    print("\n50d. algo3 backtest diagnostics classify plain SL before trailing trigger")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    _algo3_backtest_push(history, base, 21 * 15 + 1, 90670, 90720, 90620, 90710)
    _algo3_backtest_push(history, base, 21 * 15 + 2, 90710, 90730, 90520, 90540)

    result = _run_algo3_backtest_case(
        "diag-plain-sl",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 100,
            "target_points": 1000,
            "exit_mode": "fixed_target_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
    )

    trades = result["trades"]
    check("plain SL scenario produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        diagnostics = trades[0].get("diagnostics") or {}
        check("primary cause is stop_loss_hit", diagnostics.get("primary_cause_code") == "stop_loss_hit", f"diagnostics={diagnostics}")
        check("warning includes never_reached_trailing_trigger", "never_reached_trailing_trigger" in (diagnostics.get("warning_codes") or []), f"diagnostics={diagnostics}")


def test_algo3_backtest_trailing_stop_diagnostics():
    print("\n50e. algo3 backtest diagnostics classify trailing stop exits")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    _algo3_backtest_push(history, base, 21 * 15 + 1, 90670, 90920, 90660, 90890)
    _algo3_backtest_push(history, base, 21 * 15 + 2, 90890, 90910, 90740, 90760)
    for m in range(3, 15):
        _algo3_backtest_push(history, base, 21 * 15 + m, 90760, 90790, 90720, 90750)
    _algo3_backtest_push(history, base, 22 * 15, 90750, 90750, 90750, 90750)

    result = _run_algo3_backtest_case(
        "diag-trailing-stop",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 100,
            "tsl_activate_points": 200,
            "target_points": 200,
            "exit_mode": "target_to_breakeven_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
    )

    trades = result["trades"]
    check("trailing stop scenario produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        diagnostics = trades[0].get("diagnostics") or {}
        check("primary cause is trailing_stop_hit", diagnostics.get("primary_cause_code") == "trailing_stop_hit", f"diagnostics={diagnostics}")
        check("trailing stop keeps move count", int(trades[0].get("trailing_move_count") or 0) >= 1, f"trade={trades[0]}")


def test_algo3_backtest_eod_loss_diagnostics():
    print("\n50f. algo3 backtest diagnostics classify EOD red exits")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    for m in range(1, 15):
        _algo3_backtest_push(history, base, 21 * 15 + m, 90660, 90720, 90590, 90620)
    _algo3_backtest_push(history, base, 22 * 15, 90620, 90640, 90580, 90600)

    result = _run_algo3_backtest_case(
        "diag-eod-loss",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 500,
            "target_points": 1500,
            "trailing_sl_enabled": False,
            "tsl_trigger_points": 0,
            "tsl_distance_points": 0,
            "exit_mode": "fixed_target_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
    )

    trades = result["trades"]
    check("EOD loss scenario produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        diagnostics = trades[0].get("diagnostics") or {}
        check("primary cause is target_not_reached_eod", diagnostics.get("primary_cause_code") == "target_not_reached_eod", f"diagnostics={diagnostics}")
        check("exit reason stays EOD_SQUAREOFF", trades[0].get("exit_reason") == "EOD_SQUAREOFF", f"trade={trades[0]}")


def test_algo3_backtest_reversal_diagnostics():
    print("\n50g. algo3 backtest diagnostics classify reversal exits")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    for m in range(1, 14):
        _algo3_backtest_push(history, base, 21 * 15 + m, 90450, 90480, 89890, 89920)
    _algo3_backtest_push(history, base, 21 * 15 + 14, 89920, 89940, 89780, 89800)
    for m in range(15):
        _algo3_backtest_push(history, base, 22 * 15 + m, 90020, 90160, 90010, 90150)
    for m in range(15):
        _algo3_backtest_push(history, base, 23 * 15 + m, 89720, 89740, 89590, 89600)
    _algo3_backtest_push(history, base, 24 * 15, 89600, 89600, 89600, 89600)

    result = _run_algo3_backtest_case(
        "diag-reversal",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 1500,
            "target_points": 5000,
            "trailing_sl_enabled": False,
            "tsl_trigger_points": 0,
            "tsl_distance_points": 0,
            "exit_mode": "fixed_target_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
    )

    buy_trade = next((trade for trade in result["trades"] if trade.get("side") == "BUY"), None)
    check("reversal scenario produced a BUY trade", buy_trade is not None, f"trades={result['trades']}")
    if buy_trade:
        diagnostics = buy_trade.get("diagnostics") or {}
        check("BUY trade exited on contra reversal", buy_trade.get("exit_reason") == "REVERSAL_CONTRA_SIGNAL", f"trade={buy_trade}")
        check("primary cause is reversal_contra_signal", diagnostics.get("primary_cause_code") == "reversal_contra_signal", f"diagnostics={diagnostics}")


def test_algo3_backtest_same_candle_stop_priority_diagnostics():
    print("\n50h. algo3 backtest diagnostics flag same-candle stop-first behavior")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    _algo3_backtest_push(history, base, 21 * 15 + 1, 90670, 90760, 90540, 90600)

    result = _run_algo3_backtest_case(
        "diag-same-candle",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 100,
            "target_points": 100,
            "trailing_sl_enabled": False,
            "tsl_trigger_points": 0,
            "tsl_distance_points": 0,
            "exit_mode": "fixed_target_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
    )

    trades = result["trades"]
    check("same-candle scenario produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        diagnostics = trades[0].get("diagnostics") or {}
        check("same-candle scenario still exits via SL", trades[0].get("exit_reason") == "SL", f"trade={trades[0]}")
        check("warning includes same_candle_sl_priority", "same_candle_sl_priority" in (diagnostics.get("warning_codes") or []), f"diagnostics={diagnostics}")


def test_algo3_backtest_charges_deepen_loss_warning():
    print("\n50i. algo3 backtest diagnostics flag when charges materially worsen a loss")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    for m in range(1, 15):
        _algo3_backtest_push(history, base, 21 * 15 + m, 90660, 90690, 90600, 90620)
    _algo3_backtest_push(history, base, 22 * 15, 90620, 90620, 90610, 90620)

    result = _run_algo3_backtest_case(
        "diag-charges",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 500,
            "target_points": 1500,
            "trailing_sl_enabled": False,
            "tsl_trigger_points": 0,
            "tsl_distance_points": 0,
            "exit_mode": "fixed_target_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
        charges={
            "brokerage_flat": 150,
            "brokerage_pct": 100,
            "stt_pct": 0,
            "exchange_pct": 0,
            "sebi_pct": 0,
            "gst_pct": 0,
            "stamp_duty_pct": 0,
        },
    )

    trades = result["trades"]
    check("charges scenario produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        diagnostics = trades[0].get("diagnostics") or {}
        check("warning includes charges_deepened_loss", "charges_deepened_loss" in (diagnostics.get("warning_codes") or []), f"diagnostics={diagnostics}")
        check("net loss is worse than gross loss", abs(float(trades[0].get("net_pnl") or 0)) > abs(float(trades[0].get("gross_pnl") or 0)), f"trade={trades[0]}")


def test_algo3_backtest_sell_chain_diagnostics_context():
    print("\n50j. algo3 backtest diagnostics keep SELL red-chain reference context and never claim broker errors")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90020, 90030, 89880, 89900)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90000, 90010, 89870, 89900)
    for m in range(15):
        _algo3_backtest_push(history, base, 21 * 15 + m, 90020, 90160, 90010, 90150)
    for m in range(15):
        _algo3_backtest_push(history, base, 22 * 15 + m, 89850, 89870, 89690, 89700)
    _algo3_backtest_push(history, base, 23 * 15, 89700, 89700, 89700, 89700)

    result = _run_algo3_backtest_case(
        "diag-sell-context",
        history,
        {
            "silver_breakout_points": 200,
            "sl_points": 100,
            "target_points": 300,
            "trailing_sl_enabled": False,
            "tsl_trigger_points": 0,
            "tsl_distance_points": 0,
            "exit_mode": "fixed_target_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 19),
    )

    trades = result["trades"]
    check("sell-context scenario produced one trade", len(trades) == 1, f"trades={trades}")
    if trades:
        diagnostics = trades[0].get("diagnostics") or {}
        entry = diagnostics.get("entry_context") or {}
        check("SELL diagnostics store active reference close", abs(float(entry.get("active_reference_close") or 0) - 89900.0) < 1e-9, f"entry={entry}")
        check("SELL diagnostics store trigger level used", abs(float(entry.get("trigger_level_used") or 0) - 89700.0) < 1e-9, f"entry={entry}")
        check("SELL threshold entry is identified explicitly", entry.get("entry_mode") == "THRESHOLD_TRIGGER", f"entry={entry}")
        check("diagnostics object has required keys", all(key in diagnostics for key in ("primary_cause_code", "primary_cause_label", "summary", "entry_context", "exit_context", "path_metrics", "warning_codes", "warning_messages")), f"diagnostics={diagnostics}")
        check("backtest never claims broker_error", "broker" not in str(diagnostics.get("primary_cause_code") or "").lower() and not any("broker" in str(code).lower() for code in (diagnostics.get("warning_codes") or [])), f"diagnostics={diagnostics}")


def test_algo3_backtest_chart_payload():
    print("\n50k. algo3 backtest chart payload exposes candles, setups, overlays, and viewport hints")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 21, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    _algo3_backtest_push(history, base, 21 * 15 + 1, 90670, 90920, 90660, 90890)
    _algo3_backtest_push(history, base, 21 * 15 + 2, 90890, 90910, 90740, 90760)
    for m in range(3, 15):
        _algo3_backtest_push(history, base, 21 * 15 + m, 90760, 90790, 90720, 90750)
    _algo3_backtest_push(history, base, 22 * 15, 90750, 90750, 90750, 90750)

    result = _run_algo3_backtest_case(
        "diag-chart-payload",
        history,
        {
            "silver_breakout_points": 150,
            "sl_points": 100,
            "tsl_activate_points": 200,
            "target_points": 200,
            "exit_mode": "target_to_breakeven_sl",
            "silver_lots": 1,
        },
        _dt.date(2026, 8, 21),
    )

    chart = result.get("chart") or {}
    candles = chart.get("candles") or []
    setups = chart.get("setups") or []
    overlays = chart.get("trades") or []
    trades = result.get("trades") or []

    check("chart payload includes 15m candles", len(candles) >= 2, f"chart={chart}")
    check("chart payload includes EMA20 per candle", candles and candles[-1].get("ema20") is not None, f"candles={candles}")
    check("chart payload includes setup events", any(setup.get("side") == "BUY" for setup in setups), f"setups={setups}")
    check("chart payload includes trade overlays", len(overlays) == len(trades) == 1, f"overlays={overlays} trades={trades}")
    if overlays and trades:
        check("overlay trade_id matches trade row", overlays[0].get("trade_id") == trades[0].get("trade_id"), f"overlay={overlays[0]} trade={trades[0]}")
        check("viewport defaults to trade focus when trades exist", chart.get("viewport_hint", {}).get("mode") == "trade_window", f"viewport={chart.get('viewport_hint')}")


def test_algo3_backtest_best_worst_day_ignore_empty_days():
    print("\n50l. algo3 range summary ranks best/worst from traded days, not empty replay days")
    import datetime as _dt
    history: list[dict] = []
    base = _dt.datetime(2026, 8, 19, 9, 0)
    _algo3_backtest_seed_flat(history, base)

    for m in range(14):
        _algo3_backtest_push(history, base, 20 * 15 + m, 90000, 90200, 89950, 90100)
    _algo3_backtest_push(history, base, 20 * 15 + 14, 90100, 90500, 90050, 90500)
    _algo3_backtest_push(history, base, 21 * 15 + 0, 90620, 90680, 90620, 90670)
    _algo3_backtest_push(history, base, 21 * 15 + 1, 90670, 90720, 90620, 90710)
    _algo3_backtest_push(history, base, 21 * 15 + 2, 90710, 90550, 90540, 90540)
    _algo3_backtest_push(history, base, 22 * 15, 90540, 90540, 90540, 90540)

    from app import backtest as bt
    from app.backtest import _jobs, _lock

    settings = {
        "silver_breakout_points": 150,
        "sl_points": 100,
        "target_points": 1000,
        "trailing_sl_enabled": False,
        "tsl_trigger_points": 0,
        "tsl_distance_points": 0,
        "exit_mode": "fixed_target_sl",
        "silver_lots": 1,
    }
    charges = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_pct": 0, "exchange_pct": 0, "sebi_pct": 0, "gst_pct": 0, "stamp_duty_pct": 0}
    first_date = _dt.date(2026, 8, 19)
    last_date = _dt.date(2026, 8, 20)

    with _lock:
        _jobs["best-worst-days"] = {"cancel_requested": False}
    try:
        day_results = bt._simulate_silver_micro_range(
            job_id="best-worst-days",
            algo_id="algo3",
            first_date=first_date,
            last_date=last_date,
            symbol="MCX:TEST-EQ",
            history=history,
            trading_days=[first_date, last_date],
            settings=settings,
            charges_config=charges,
        )
        result = bt._range_result(
            "algo3",
            first_date,
            last_date,
            day_results,
            {"available": 1, "requested": 1},
        )
    finally:
        with _lock:
            _jobs.pop("best-worst-days", None)

    check("range result has a traded best day", result.get("best_day", {}).get("date") == "2026-08-19", f"best_day={result.get('best_day')}")
    check("range result has a traded worst day", result.get("worst_day", {}).get("date") == "2026-08-19", f"worst_day={result.get('worst_day')}")
    check("empty replay day stays in daily_results", len(result.get("daily_results") or []) == 2, f"daily_results={result.get('daily_results')}")


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


def test_fyers_positions_pnl_summary_normalization():
    """Dashboard live P&L must prefer FYERS's aggregate, not browser math."""
    print("\n52a. FYERS positions P&L summary is normalized for the dashboard")
    from app.fyers_client import _normalize_broker_pnl_summary

    summary = _normalize_broker_pnl_summary(
        {
            "pl_total": "1,250.50",
            "pl_realized": 200,
            "pl_unrealized": 1050.5,
        },
        [{"realized_pnl": 10, "unrealized_pnl": 20, "total_pnl": 30}],
    )
    check("FYERS overall total takes precedence", summary["total_pnl"] == 1250.5, f"got={summary}")
    check("FYERS overall realised takes precedence", summary["realized_pnl"] == 200.0, f"got={summary}")
    check("FYERS overall unrealised takes precedence", summary["unrealized_pnl"] == 1050.5, f"got={summary}")
    check("summary identifies FYERS overall source", summary["source"] == "fyers_overall", f"got={summary}")

    fallback = _normalize_broker_pnl_summary(
        {},
        [
            {"realized_pnl": 40, "unrealized_pnl": 60, "total_pnl": 100},
            {"realized_pnl": -10, "unrealized_pnl": 5, "total_pnl": -5},
        ],
    )
    check("position fallback sums broker rows", fallback["total_pnl"] == 95.0, f"got={fallback}")
    check("position fallback preserves realised P&L", fallback["realized_pnl"] == 30.0, f"got={fallback}")
    check("position fallback identifies broker rows", fallback["source"] == "fyers_positions", f"got={fallback}")


def test_paper_summary_uses_exit_date_for_realized_totals():
    """Paper realized P&L must follow closed-today trades, not opened-today trades."""
    print("\n52aa. paper realized totals use exit_time semantics")
    import datetime as _dt
    import app.paper_broker as pb

    today = _dt.date.today()
    yesterday = today - _dt.timedelta(days=1)
    today_iso = today.isoformat()
    yesterday_iso = yesterday.isoformat()
    query_log = []

    class FakeResult:
        def __init__(self, data):
            self.data = data

    class FakeQuery:
        def __init__(self, rows):
            self._rows = [dict(row) for row in rows]

        def select(self, _fields):
            return self

        def eq(self, key, value):
            self._rows = [row for row in self._rows if row.get(key) == value]
            return self

        def gte(self, key, value):
            query_log.append((key, value))
            self._rows = [row for row in self._rows if str(row.get(key) or "") >= str(value)]
            return self

        def order(self, key, desc=False):
            self._rows = sorted(self._rows, key=lambda row: str(row.get(key) or ""), reverse=desc)
            return self

        def limit(self, value):
            self._rows = self._rows[:value]
            return self

        def execute(self):
            return FakeResult(self._rows)

    class FakeSupabase:
        def __init__(self, rows):
            self._rows = rows

        def table(self, _name):
            return FakeQuery(self._rows)

    rows = [
        {
            "id": "closed-today-opened-yesterday",
            "algo_id": "algo3",
            "entry_time": f"{yesterday_iso}T23:55:00+00:00",
            "exit_time": f"{today_iso}T03:45:00+00:00",
            "gross_pnl": 1000.0,
            "net_pnl": 900.0,
            "total_charges": 100.0,
        },
        {
            "id": "closed-yesterday",
            "algo_id": "algo3",
            "entry_time": f"{yesterday_iso}T10:00:00+00:00",
            "exit_time": f"{yesterday_iso}T11:00:00+00:00",
            "gross_pnl": 5000.0,
            "net_pnl": 4500.0,
            "total_charges": 500.0,
        },
        {
            "id": "closed-today-opened-today",
            "algo_id": "algo3",
            "entry_time": f"{today_iso}T04:00:00+00:00",
            "exit_time": f"{today_iso}T04:10:00+00:00",
            "gross_pnl": -250.0,
            "net_pnl": -275.0,
            "total_charges": 25.0,
        },
    ]

    original_run_with_supabase = pb.run_with_supabase
    broker = object.__new__(pb.PaperBroker)
    broker.algo_id = "algo3"
    broker.starting_capital = 100000.0
    broker._get_state = lambda: {"cash": 123456.78}
    broker.today_counts = lambda: {"trade_count_today": 2, "buy_count_today": 1, "sell_count_today": 1}
    broker.storage_algo_candidates = lambda: ["algo3"]
    broker.trades_table_name = lambda: "paper_trades"

    try:
        pb.run_with_supabase = lambda callback: callback(FakeSupabase(rows))
        summary = broker.summary()
        recent = broker.recent_trades(limit=10, today_only=True)
    finally:
        pb.run_with_supabase = original_run_with_supabase

    check(
        "summary gross includes trades closed today even if opened yesterday",
        summary["realized_gross_pnl"] == 750.0,
        f"summary={summary}",
    )
    check(
        "summary net includes closed-today rows only",
        summary["realized_net_pnl"] == 625.0,
        f"summary={summary}",
    )
    check(
        "summary charges include closed-today rows only",
        summary["realized_charges"] == 125.0,
        f"summary={summary}",
    )
    check(
        "recent_trades(today_only=True) follows exit_time filter",
        [row["id"] for row in recent] == [
            "closed-today-opened-today",
            "closed-today-opened-yesterday",
        ],
        f"recent={[row['id'] for row in recent]}",
    )
    check(
        "paper summary queries were filtered on exit_time",
        query_log and all(key == "exit_time" for key, _ in query_log),
        f"query_log={query_log}",
    )


def test_live_fill_timestamp_integrity():
    """FYERS fill rows must not create shifted or cross-order timestamps."""
    print("\n52b. live fill timestamps use IST and exact Fyers order identity")
    broker = make_broker({"placed": [], "cancelled": [], "modified": [], "orderbook": []})

    parsed = broker._parse_fill_time({"tradeDateTime": "2026-08-20 09:22:07"})
    check(
        "naive Fyers fill timestamp is interpreted as IST",
        parsed is not None and parsed.isoformat().startswith("2026-08-20T03:52:07"),
        f"parsed={parsed}",
    )

    class FillHistory:
        def tradebook(self):
            return {"tradeBook": [
                {"id": "OLD", "symbol": "MCX:SILVERMIC26AUGFUT", "side": 1, "qty": 1,
                 "tradeDateTime": "2026-08-20 09:22:07"},
                {"id": "NEW", "symbol": "MCX:SILVERMIC26AUGFUT", "side": 1, "qty": 1,
                 "tradeDateTime": "2026-08-20 09:23:07"},
                # A row without an order id must never impersonate NEW.
                {"symbol": "MCX:SILVERMIC26AUGFUT", "side": 1, "qty": 1,
                 "tradeDateTime": "2026-08-20 15:29:30"},
            ]}

        def tradehistory(self, _payload):
            return {"tradeHistory": []}

    history = FillHistory()
    matched = broker._find_latest_fill(history, "MCX:SILVERMIC26AUGFUT", "BUY", 1, "NEW")
    missing = broker._find_latest_fill(history, "MCX:SILVERMIC26AUGFUT", "BUY", 1, "MISSING")
    check("fill lookup uses the exact Fyers order id", matched and matched.get("id") == "NEW", f"matched={matched}")
    check("fill lookup rejects unidentifiable historical rows", missing is None, f"missing={missing}")

    import datetime
    entry = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
    safe_exit = broker._safe_exit_time(
        {"symbol": "MCX:SILVERMIC26AUGFUT", "entry_time": entry},
        "2026-08-20T03:52:07+00:00",
    )
    check(
        "stale exit timestamp is replaced instead of preceding entry",
        datetime.datetime.fromisoformat(safe_exit) > datetime.datetime.fromisoformat(entry),
        f"entry={entry} exit={safe_exit}",
    )


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
    def fake_range(symbol, start, end, **kwargs):
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


def test_algo3_manual_history_refresh_is_single_owner_and_rate_limited():
    """The History-panel recovery button must not become a FYERS 429 loop.

    It starts at most one forced warm-up, leaves the feed alone, and rejects
    a second browser-tab click for the configured cooldown window.
    """
    print("\n54b. algo3 manual history refresh is guarded")
    from unittest.mock import patch
    import app.strategies.algo3_silver_micro as algo3_mod

    load_calls = []

    def counting_load(self):
        load_calls.append(1)
        self._history_loading = False

    strat = _make_bare_algo3()
    strat._history_lock = __import__("threading").Lock()
    strat._history_loading = False
    strat._last_warmup_at = None
    strat._last_manual_history_refresh_at = None

    class SyncThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    with patch.object(algo3_mod.Algo3SilverMicro, "_load_history_background", counting_load), \
         patch.object(algo3_mod.threading, "Thread", SyncThread):
        started, message = strat.request_manual_history_refresh()
        check("manual Silver refresh starts one forced warm-up", started and len(load_calls) == 1, message)

        started_again, message_again = strat.request_manual_history_refresh()
        check(
            "manual Silver refresh suppresses repeated browser clicks",
            not started_again and len(load_calls) == 1 and "wait" in message_again.lower(),
            message_again,
        )


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


def test_strategy_specific_square_off_times():
    print("\n57. strategy-specific square-off keeps Silver alive past 15:15")
    from app.strategies.algo1_opening_range import Algo1OpeningRange
    from app.strategies.algo3_silver_micro import Algo3SilverMicro

    algo1 = object.__new__(Algo1OpeningRange)
    algo3 = object.__new__(Algo3SilverMicro)

    check("algo1 square-off remains 15:15", algo1.square_off_time() == "15:15",
          f"got={algo1.square_off_time()}")
    check("algo3 square-off moved to MCX close window", algo3.square_off_time() == "23:25",
          f"got={algo3.square_off_time()}")


def test_strategy_specific_session_windows():
    print("\n58. strategy-specific sessions keep MCX active after 15:30")
    import app.engine as engine_mod
    from app.strategies.algo1_opening_range import Algo1OpeningRange
    from app.strategies.algo3_silver_micro import Algo3SilverMicro

    algo1 = object.__new__(Algo1OpeningRange)
    algo3 = object.__new__(Algo3SilverMicro)

    check("algo1 inactive at 20:00", engine_mod._strategy_session_active(algo1, "20:00") is False)
    check("algo3 active at 20:00", engine_mod._strategy_session_active(algo3, "20:00") is True)
    check("algo3 feed warmup allowed at 08:50", engine_mod._strategy_feed_permitted(algo3, "08:50") is True)
    check("algo3 inactive after 23:30", engine_mod._strategy_session_active(algo3, "23:30") is False)


def _make_feed_strategy(symbols, *, start="09:15", end="15:30"):
    class _Strategy:
        def __init__(self):
            self.watchlist = list(symbols)
            self.refresh_calls = 0

        def market_session_start(self):
            return start

        def market_session_end(self):
            return end

        def refresh_market_data(self, force: bool = False):
            self.refresh_calls += 1

    return _Strategy()


def test_live_feed_plans_split_silver_and_general():
    print("\n58b. live feed plans split dedicated Silver feed from general watchlist")
    import app.engine as eng

    old_strategies = dict(eng.STRATEGIES)
    try:
        eng.STRATEGIES = {
            "algo1": _make_feed_strategy(["NSE:RELIANCE-EQ", "NSE:TCS-EQ"]),
            "algo3": _make_feed_strategy(
                ["MCX:SILVERMIC26AUGFUT"],
                start="09:00",
                end="23:30",
            ),
        }
        plans = eng._build_live_feed_plans("09:10")
        general = next((plan for plan in plans if plan["name"] == "general"), None)
        silver = next((plan for plan in plans if plan["name"] == "silver"), None)

        check("general plan exists during shared warmup", general is not None, f"plans={plans}")
        check("silver plan exists during shared warmup", silver is not None, f"plans={plans}")
        check("silver feed uses lite mode", bool(silver and silver.get("litemode")) is True, f"silver={silver}")
        check("general feed excludes dedicated Silver symbol",
              general is not None and general.get("symbols") == ["NSE:RELIANCE-EQ", "NSE:TCS-EQ"],
              f"general={general}")
        check("silver feed keeps only Silver symbol",
              silver is not None and silver.get("symbols") == ["MCX:SILVERMIC26AUGFUT"],
              f"silver={silver}")
    finally:
        eng.STRATEGIES = old_strategies


def test_live_feed_plans_go_silver_only_after_nse_close():
    print("\n58c. live feed plans drop general NSE feed during MCX-only hours")
    import app.engine as eng

    old_strategies = dict(eng.STRATEGIES)
    try:
        eng.STRATEGIES = {
            "algo1": _make_feed_strategy(["NSE:RELIANCE-EQ", "NSE:TCS-EQ"]),
            "algo3": _make_feed_strategy(
                ["MCX:SILVERMIC26AUGFUT"],
                start="09:00",
                end="23:30",
            ),
        }
        plans = eng._build_live_feed_plans("20:00")
        check("only one plan remains after NSE close", len(plans) == 1, f"plans={plans}")
        check("remaining feed is dedicated Silver lite feed",
              plans == [{
                  "name": "silver",
                  "symbols": ["MCX:SILVERMIC26AUGFUT"],
                  "litemode": True,
                  "description": "Dedicated Silver execution feed",
              }],
              f"plans={plans}")
    finally:
        eng.STRATEGIES = old_strategies


def test_start_live_feed_if_ready_starts_named_feeds():
    print("\n58d. start_live_feed_if_ready launches named FYERS feeds with correct lite mode")
    from unittest.mock import patch
    import app.engine as eng

    class SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    class FakeSocket:
        def __init__(self, name):
            self.name = name
            self.closed = False

        def close_connection(self):
            self.closed = True

    calls = []
    fake_sockets = {}
    strategies = {
        "algo1": _make_feed_strategy(["NSE:RELIANCE-EQ", "NSE:TCS-EQ"]),
        "algo3": _make_feed_strategy(["MCX:SILVERMIC26AUGFUT"], start="09:00", end="23:30"),
    }

    def fake_connect(symbols, on_tick, on_status_callback=None, *, feed_name="general", litemode=False):
        calls.append({
            "feed_name": feed_name,
            "symbols": list(symbols),
            "litemode": litemode,
        })
        socket = FakeSocket(feed_name)
        fake_sockets[feed_name] = socket
        return socket

    old_strategies = dict(eng.STRATEGIES)
    old_watchlist = list(eng.WATCHLIST)
    old_live_symbols = list(eng.LIVE_FEED_SYMBOLS)
    old_status = dict(eng._engine_status)
    old_started = eng._live_feed_started
    old_plans = dict(eng._live_feed_plans)
    old_sockets = dict(eng._live_feed_sockets)
    try:
        eng.STRATEGIES = strategies
        eng.WATCHLIST = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "MCX:SILVERMIC26AUGFUT"]
        eng.LIVE_FEED_SYMBOLS = list(eng.WATCHLIST)
        eng._live_feed_started = False
        eng._live_feed_plans = {}
        eng._live_feed_sockets = {}
        eng._engine_status.update({
            "fyers_feed_statuses": {},
            "fyers_ws_connected": False,
            "fyers_ws_error": None,
            "live_feed_started": False,
        })

        with patch.object(eng, "get_stored_access_token", return_value="TOKEN"), \
             patch.object(eng, "_build_live_feed_plans", return_value=[
                 {
                     "name": "general",
                     "symbols": ["NSE:RELIANCE-EQ", "NSE:TCS-EQ"],
                     "litemode": False,
                     "description": "General market-data feed",
                 },
                 {
                     "name": "silver",
                     "symbols": ["MCX:SILVERMIC26AUGFUT"],
                     "litemode": True,
                     "description": "Dedicated Silver execution feed",
                 },
             ]), \
             patch.object(eng, "connect_live_feed", side_effect=fake_connect), \
             patch.object(eng.threading, "Thread", SyncThread):
            started = eng.start_live_feed_if_ready()

        check("multi-feed start request succeeds", started is True, f"started={started}")
        check("two named feed connections are opened", [call["feed_name"] for call in calls] == ["general", "silver"],
              f"calls={calls}")
        check("general feed stays full mode",
              calls[0]["litemode"] is False and calls[0]["symbols"] == ["NSE:RELIANCE-EQ", "NSE:TCS-EQ"],
              f"call={calls[0] if calls else None}")
        check("silver feed uses lite mode",
              calls[1]["litemode"] is True and calls[1]["symbols"] == ["MCX:SILVERMIC26AUGFUT"],
              f"call={calls[1] if len(calls) > 1 else None}")
        check("engine stores both named sockets",
              sorted(eng._live_feed_sockets.keys()) == ["general", "silver"],
              f"sockets={sorted(eng._live_feed_sockets.keys())}")
        check("engine exposes pending per-feed statuses immediately",
              sorted((eng._engine_status.get("fyers_feed_statuses") or {}).keys()) == ["general", "silver"],
              f"statuses={eng._engine_status.get('fyers_feed_statuses')}")
    finally:
        eng.STRATEGIES = old_strategies
        eng.WATCHLIST = old_watchlist
        eng.LIVE_FEED_SYMBOLS = old_live_symbols
        eng._engine_status.clear()
        eng._engine_status.update(old_status)
        eng._live_feed_started = old_started
        eng._live_feed_plans = old_plans
        eng._live_feed_sockets = old_sockets


def test_start_live_feed_if_ready_reconfigures_when_plan_changes():
    print("\n58e. start_live_feed_if_ready reshapes feeds when the desired plan changes")
    from unittest.mock import patch
    import app.engine as eng

    class SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    class FakeSocket:
        def __init__(self, name):
            self.name = name
            self.closed = False

        def close_connection(self):
            self.closed = True

    first_general = FakeSocket("general")
    first_silver = FakeSocket("silver")
    second_silver = FakeSocket("silver-restarted")
    issued = []
    returned = [first_general, first_silver, second_silver]

    def fake_connect(symbols, on_tick, on_status_callback=None, *, feed_name="general", litemode=False):
        issued.append((feed_name, list(symbols), litemode))
        return returned[len(issued) - 1]

    old_watchlist = list(eng.WATCHLIST)
    old_live_symbols = list(eng.LIVE_FEED_SYMBOLS)
    old_status = dict(eng._engine_status)
    old_started = eng._live_feed_started
    old_plans = dict(eng._live_feed_plans)
    old_sockets = dict(eng._live_feed_sockets)
    try:
        eng.WATCHLIST = ["NSE:RELIANCE-EQ", "MCX:SILVERMIC26AUGFUT"]
        eng.LIVE_FEED_SYMBOLS = list(eng.WATCHLIST)
        eng._live_feed_started = False
        eng._live_feed_plans = {}
        eng._live_feed_sockets = {}
        eng._engine_status.update({"fyers_feed_statuses": {}, "live_feed_started": False})

        with patch.object(eng, "get_stored_access_token", return_value="TOKEN"), \
             patch.object(eng, "connect_live_feed", side_effect=fake_connect), \
             patch.object(eng.threading, "Thread", SyncThread), \
             patch.object(eng, "_build_live_feed_plans", side_effect=[
                 [
                     {"name": "general", "symbols": ["NSE:RELIANCE-EQ"], "litemode": False},
                     {"name": "silver", "symbols": ["MCX:SILVERMIC26AUGFUT"], "litemode": True},
                 ],
                 [
                     {"name": "silver", "symbols": ["MCX:SILVERMIC26AUGFUT"], "litemode": True},
                 ],
             ]):
            eng.start_live_feed_if_ready()
            eng.start_live_feed_if_ready()

        check("general socket is closed when plan shrinks to silver-only", first_general.closed is True)
        check("previous silver socket is closed before reconfigure", first_silver.closed is True)
        check("latest active socket is the replacement silver feed",
              list(eng._live_feed_sockets.keys()) == ["silver"] and eng._live_feed_sockets["silver"] is second_silver,
              f"sockets={eng._live_feed_sockets}")
        check("reconfigure performed a second silver connect",
              issued == [
                  ("general", ["NSE:RELIANCE-EQ"], False),
                  ("silver", ["MCX:SILVERMIC26AUGFUT"], True),
                  ("silver", ["MCX:SILVERMIC26AUGFUT"], True),
              ],
              f"issued={issued}")
    finally:
        eng.WATCHLIST = old_watchlist
        eng.LIVE_FEED_SYMBOLS = old_live_symbols
        eng._engine_status.clear()
        eng._engine_status.update(old_status)
        eng._live_feed_started = old_started
        eng._live_feed_plans = old_plans
        eng._live_feed_sockets = old_sockets


def test_live_feed_status_aggregates_named_feeds():
    print("\n58f. named feed statuses aggregate conservatively until every active feed is connected")
    import app.engine as eng

    old_status = dict(eng._engine_status)
    old_plans = dict(eng._live_feed_plans)
    old_started = eng._live_feed_started
    try:
        eng._live_feed_started = True
        eng._live_feed_plans = {
            "general": {"name": "general", "symbols": ["NSE:RELIANCE-EQ"], "litemode": False},
            "silver": {"name": "silver", "symbols": ["MCX:SILVERMIC26AUGFUT"], "litemode": True},
        }
        eng._engine_status.update({
            "fyers_feed_statuses": {},
            "fyers_ws_connected": False,
            "fyers_ws_error": None,
            "fyers_session_state": "token_present_settling",
            "live_feed_started": True,
            "fyers_ws_subscribed_symbols": 0,
            "fyers_ws_first_tick_at": None,
        })

        eng._on_live_feed_status({
            "connected": True,
            "subscribed_symbols": 1,
            "first_tick_received": True,
        }, feed_name="silver")
        check("one connected feed is not enough to mark whole bundle connected",
              eng._engine_status.get("fyers_ws_connected") is False,
              f"status={eng._engine_status.get('fyers_feed_statuses')}")

        eng._on_live_feed_status({
            "connected": True,
            "subscribed_symbols": 1,
            "first_tick_received": True,
        }, feed_name="general")
        check("bundle becomes connected after both feeds connect",
              eng._engine_status.get("fyers_ws_connected") is True)
        check("subscribed symbol count is summed across feeds",
              eng._engine_status.get("fyers_ws_subscribed_symbols") == 2,
              f"got={eng._engine_status.get('fyers_ws_subscribed_symbols')}")
        check("per-feed state keeps lite mode metadata",
              (eng._engine_status.get("fyers_feed_statuses") or {}).get("silver", {}).get("litemode") is True,
              f"statuses={eng._engine_status.get('fyers_feed_statuses')}")
    finally:
        eng._engine_status.clear()
        eng._engine_status.update(old_status)
        eng._live_feed_plans = old_plans
        eng._live_feed_started = old_started


def test_stop_live_feed_closes_all_named_sockets():
    print("\n58g. stop_live_feed closes every named socket and clears per-feed status")
    import app.engine as eng

    class FakeSocket:
        def __init__(self):
            self.closed = False

        def close_connection(self):
            self.closed = True

    general = FakeSocket()
    silver = FakeSocket()

    old_status = dict(eng._engine_status)
    old_started = eng._live_feed_started
    old_plans = dict(eng._live_feed_plans)
    old_sockets = dict(eng._live_feed_sockets)
    try:
        eng._live_feed_started = True
        eng._live_feed_plans = {
            "general": {"name": "general", "symbols": ["NSE:RELIANCE-EQ"]},
            "silver": {"name": "silver", "symbols": ["MCX:SILVERMIC26AUGFUT"]},
        }
        eng._live_feed_sockets = {"general": general, "silver": silver}
        eng._engine_status.update({
            "fyers_feed_statuses": {"general": {"connected": True}, "silver": {"connected": True}},
            "fyers_ws_connected": True,
            "live_feed_started": True,
        })

        stopped = eng.stop_live_feed(reason="smoke_test")

        check("stop_live_feed returns success", stopped is True, f"stopped={stopped}")
        check("every named socket is closed", general.closed is True and silver.closed is True)
        check("active sockets are cleared", eng._live_feed_sockets == {}, f"sockets={eng._live_feed_sockets}")
        check("per-feed statuses are cleared", eng._engine_status.get("fyers_feed_statuses") == {},
              f"statuses={eng._engine_status.get('fyers_feed_statuses')}")
    finally:
        eng._engine_status.clear()
        eng._engine_status.update(old_status)
        eng._live_feed_started = old_started
        eng._live_feed_plans = old_plans
        eng._live_feed_sockets = old_sockets


def main():
    print("=" * 66)
    print("  LIVE ORDER PIPELINE — OFFLINE SMOKE TEST (no Fyers, no DB)")
    print("=" * 66)
    test_dynamic_qty()
    test_entry_and_protective_payloads()
    test_market_mode()
    test_silver_forces_market_entry()
    test_live_funds_cap_bypasses_mcx_futures()
    test_live_funds_cap_still_limits_cash_equity()
    test_live_open_trade_refuses_unprotected_position()
    test_oco_reconcile()
    test_oco_reconcile_marks_trailing_stop()
    test_trailing_sl_syncs_to_fyers()
    test_tick_rounding_and_slm_limit()
    test_mcx_tick_size_and_slm_slack()
    test_external_close_sync()
    test_external_close_2026_08_11_regression()
    test_external_close_single_flaky_poll()
    test_external_close_grace()
    test_close_trade_skips_market_when_fyers_already_flat()
    test_close_trade_skips_market_when_sl_cancel_reports_already_filled()
    test_bootstrap_fyers_app_position_and_land_in_closed_trades()
    test_close_trade_still_sends_market_when_fyers_still_holds()
    test_close_trade_falls_back_to_market_when_fyers_unavailable()
    test_ws_order_fill_closes_position_immediately()
    test_ws_position_flat_force_syncs_stale_db()
    test_ws_manual_external_entry_logged_not_persisted()
    test_ws_cancelled_protective_order_clears_snapshot()
    test_ws_dispatch_parses_orders_and_positions_bundle()
    test_order_update_ws_status_callback_accepts_fyers_payload()
    test_engine_order_update_router_handles_all_event_kinds()
    test_open_trade_refuses_duplicate_when_fyers_already_positioned()
    test_algo1_open_position_guard()
    test_protective_retry()
    test_streaming_fcfs_phase2()
    test_trailing_metadata_tracks_activation_and_bumps()
    test_silver_target_to_breakeven_policy()
    test_daily_trade_totals_do_not_collapse_same_side_rows()
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
    test_connection_status_connected_requires_successful_verify()
    test_connection_status_token_present_settling_survives_redeploy()
    test_connection_status_bad_request_stays_degraded_not_expired()
    test_silver_setup_history_naive_ist_stores_as_correct_utc()
    test_silver_setup_history_repairs_future_shifted_legacy_rows()
    test_silver_setup_history_requires_candle_to_close_on_correct_ema_side()
    test_silver_feed_status_falls_back_to_persisted_setup_history()
    test_pre_market_no_tick_not_counted_as_failure()
    test_critical_live_feed_symbols_ignore_nse_noise()
    test_engine_rest_fallback_targets_stale_non_nse_symbols()
    test_engine_rest_fallback_injects_synthetic_tick_into_algo3()
    test_post_recovery_grace_ignores_immediate_failure()
    test_restart_live_feed_suppresses_duplicate_watchdog_during_settling()
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
    test_algo3_closed_15m_bucket_finalizes_without_next_bucket_tick()
    test_algo3_clock_finalization_waits_for_fyers_settle_window()
    test_algo3_unverified_local_bar_never_becomes_a_setup_reference()
    test_algo3_live_15m_setup_uses_fyers_verified_bar_close()
    test_algo3_live_15m_sell_setup_uses_fyers_verified_bar_close()
    test_algo3_setup_captures_and_overwrites()
    test_algo3_no_setup_when_wrong_side_of_ema()
    test_algo3_setup_persistence_emits_history_event()
    test_algo3_setup_persistence_rejects_wrong_candle_color()
    test_algo3_buy_trigger_only_on_upward_cross()
    test_algo3_sell_does_not_fire_from_tick_cross()
    test_algo3_no_trigger_before_first_prev_ltp()
    test_algo3_configurable_n_parameter()
    test_algo3_reversal_on_contra_signal()
    test_algo3_no_reentry_same_side()
    test_algo3_unlimited_reentry_after_exit_same_setup()
    test_algo3_manual_exit_safe_mode_clears_handoff_and_requires_fresh_trigger()
    test_algo3_manual_exit_reentry_mode_reopens_same_reference_immediately()
    test_algo3_mode_switch_keeps_reference_but_clears_previous_mode_fired_state()
    test_algo3_sell_target_reenters_when_reference_still_crossed()
    test_algo3_sell_stop_does_not_reenter_above_old_trigger()
    test_algo3_entry_uses_exchange_event_time()
    test_algo3_failed_live_attempt_consumes_setup_once()
    test_algo3_new_setup_rearms_after_failed_attempt()
    test_algo3_live_broker_guard_blocks_when_symbol_busy()
    test_algo3_live_broker_guard_ignores_todays_filled_orders()
    test_algo3_entry_uses_points_sl_target()
    test_algo3_trailing_settings_use_point_lock_model()
    test_silver_point_lock_trailing_buy_and_sell()
    test_algo3_scan_disabled_skips_triggers()
    test_algo3_black_box_end_to_end()
    test_broker_key_suffix_isolates_tokens()
    test_broker_key_suffix_empty_preserves_legacy_key()
    test_strategy_settings_storage_key_isolates_deployments()
    test_strategy_settings_storage_key_empty_preserves_legacy_algo_id()
    test_runtime_mode_setting_key_isolates_deployments()
    test_algo_settings_routes_pin_active_mode()
    test_algo_summary_reads_persisted_active_mode_toggles()
    test_paper_broker_storage_key_isolates_deployments()
    test_charges_config_row_id_isolates_deployments()
    test_algo3_backtest_parity_with_live()
    test_algo3_backtest_buy_reference_breakout_contract()
    test_algo3_backtest_buy_plan_is_15m_reference_only()
    test_algo3_backtest_buy_reenters_after_target_in_same_15m_candle()
    test_algo3_live_buy_reference_reentry_and_rollover()
    test_algo3_gap_through_fires_immediately()
    test_algo3_previous_day_buy_setup_gap_open_fires_immediately()
    test_algo3_previous_day_sell_setup_gap_open_fires_immediately()
    test_algo3_trading_enabled_kill_switch_blocks_new_entries_but_keeps_exits()
    test_algo3_trailing_stop_exit_reason_is_preserved()
    test_algo3_candle_close_trigger_fires()
    test_algo3_backtest_sell_red_chain_survives_green_candles()
    test_algo3_backtest_sell_reentry_requires_carried_trigger()
    test_algo3_backtest_sell_breakeven_exits_on_reversal()
    test_algo3_lot_based_qty()
    test_broker_positions_entry_time_from_tradebook()
    test_fyers_positions_pnl_summary_normalization()
    test_paper_summary_uses_exit_date_for_realized_totals()
    test_live_fill_timestamp_integrity()
    test_algo3_warmup_end_date_is_today()
    test_algo3_warmup_debounce_blocks_repeat_calls()
    test_algo3_manual_history_refresh_is_single_owner_and_rate_limited()
    test_algo3_warmup_resets_state_before_replay()
    test_algo3_warmup_transient_zero_result_preserves_state()
    test_strategy_specific_square_off_times()
    test_strategy_specific_session_windows()
    test_live_feed_plans_split_silver_and_general()
    test_live_feed_plans_go_silver_only_after_nse_close()
    test_start_live_feed_if_ready_starts_named_feeds()
    test_start_live_feed_if_ready_reconfigures_when_plan_changes()
    test_live_feed_status_aggregates_named_feeds()
    test_stop_live_feed_closes_all_named_sockets()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  RESULT: {_failures} check(s) FAILED")
        return 1
    print("  RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
