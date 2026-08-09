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
import app.strategy_settings as ss
from app.live_broker import LiveBroker
from app.margin_lookup import effective_multiplier


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


def main():
    print("=" * 66)
    print("  LIVE ORDER PIPELINE — OFFLINE SMOKE TEST (no Fyers, no DB)")
    print("=" * 66)
    test_dynamic_qty()
    test_entry_and_protective_payloads()
    test_market_mode()
    test_oco_reconcile()
    test_trailing_sl_syncs_to_fyers()
    print("\n" + "=" * 66)
    if _failures:
        print(f"  RESULT: {_failures} check(s) FAILED")
        return 1
    print("  RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
