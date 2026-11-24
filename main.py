"""Pump.fun Replay Sprint -- launch sniper with a rug filter (agent-fun/replay/v2).

Watches every coin from its CreateEvent and buys only launches that pass a rug
filter in their first slots: the creator has not sold, the creator's own opening
buy is small, few distinct non-creator wallets piled in at the open (a launch
bundled by many wallets is usually the creator's cluster waiting to dump), and the
curve already holds some real SOL. Takes profit fast, cuts losers fast, never
holds past max_hold slots, and leaves at once when the creator sells.

One buy per coin, one order per buy/sell (no splitting: every extra order costs
another 105,000-lamport tx fee, and the guardrail here needs no fill pacing).
Round trips are at least 0.01 SOL, so every filled position counts toward the
20-distinct-mint guardrail.

Deterministic by construction: no clock, no randomness, no I/O, iteration only
over insertion-ordered dicts and sorted position lists. The grader runs it as
``python3 -P -s /bot/main.py`` with ``PYTHONHASHSEED=0``.

Fees the strategy prices against: 95 bps protocol + 30 bps creator on every
fill, plus a 105,000-lamport tx fee per order. A 0.1 SOL round trip therefore
costs roughly 2.7% all-in, so entries must clear that bar on average.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

BPS = 10_000
LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL
TX_FEE = 105_000  # base 5,000 + priority 100,000 lamports, per the hello fees
CURVE_FEE_BPS = 125  # protocol 95 + creator 30 (fallback bps from hello)

#: Tunable strategy parameters. Calibrated on the kit's dev window with the
#: local replay sim (see the repo's calibration notes in README.md); the sweep
#: preferred flat regions over single best cells.
PARAMS = {
    # Entry window: youngest/oldest age (slots since CreateEvent) at which a
    # first buy may still be decided.
    "min_age": 0,
    "max_age": 3,
    # Lamports to aim for per coin in total; smallest single buy worth sending
    # (0.01 SOL: the guardrail's minimum round-trip size).
    "stake_lamports": 100_000_000,
    "min_buy_lamports": 10_000_000,
    # Real SOL reserve window at decision time (lamports). The 3 SOL floor
    # keeps the bot out of dead launches (dev-window finding: coins below
    # 1 SOL real at age 3 almost never pay).
    "min_real_sol": 3_000_000_000,
    "max_real_sol": 30_000_000_000,
    # Creator's own buys in the create slot: at most this many lamports.
    "max_dev_buy": 2_500_000_000,
    # Trades seen and distinct (non-creator) buyers seen before entry.
    # Three distinct buyers at age <= 3 is the strongest early signal in
    # the dev window (hit-rate on +15% more than doubles vs <= 1 buyer).
    "min_trades": 0,
    "min_buyers": 3,
    # Distinct non-creator buyers before entry, at most.
    "max_buyers": 3,
    # Lamports sold by others / lamports bought, as bps, at most.
    "max_sell_ratio_bps": 10_000,
    # Largest single non-creator sell seen, as bps of the real SOL reserve
    # before it, at most.
    "max_big_sell_bps": 10_000,
    # Price now over the create price, as bps: window. 13,000 = +30%: the
    # launch must already be running before the bot chases it (dev-window
    # finding: run-up >= 20% at age 3 lifts the +15%-hit rate from 23% to
    # ~40%, and >= 30% lifts the win rate to ~25% with far fewer losers).
    "min_runup_bps": 13_000,
    "max_runup_bps": 1_000_000,
    # The recent-flow window (slots) and what it must show.
    "flow_window": 10,
    "min_flow_lamports": -1_000_000_000_000,
    "min_recent_buyers": 0,
    # Creator sold before entry: skip the coin.
    "skip_dev_sold": True,
    # Slippage allowed on buys (bps); sells always go out at 10_000.
    "buy_slippage_bps": 10_000,
    # Exit when the position is worth this much over its cost (bps).
    "tp_bps": 2_500,
    # Exit when it is worth this much under its cost (bps).
    "sl_bps": 800,
    # Trailing stop: once up at least trail_arm_bps, exit on a fall of
    # trail_bps from the best value (1_000_000 = off). Armed from the first
    # tick in profit (arm 0), so a 5% pullback from the peak banks the gain.
    "trail_arm_bps": 0,
    "trail_bps": 500,
    # Exit after holding this many slots.
    "max_hold": 20,
    # Exit when the creator sells while held.
    "exit_on_dev_sell": True,
}


class Coin:
    """A watched curve: its opening tape, in first-seen order."""

    __slots__ = (
        "mint", "create_slot", "creator", "vq0", "vt0", "dev_buy", "trades",
        "buyers", "bought", "sold_by_others", "big_sell_bps", "dev_sold",
        "last_trade_slot", "recent", "buys_sent", "spent_sent",
        "last_buy_vq", "last_buy_vt", "last_buy_slot", "done", "complete",
    )

    def __init__(self, mint, create_slot, creator, vq0, vt0):
        self.mint = mint
        self.create_slot = create_slot
        self.creator = creator
        self.vq0 = vq0
        self.vt0 = vt0
        self.dev_buy = 0
        self.trades = 0
        self.buyers = set()
        self.bought = 0
        self.sold_by_others = 0
        self.big_sell_bps = 0
        self.dev_sold = False
        self.last_trade_slot = create_slot
        self.recent = []  # (slot, is_buy, sol, user)
        self.buys_sent = 0
        self.spent_sent = 0
        self.last_buy_vq = 0
        self.last_buy_vt = 1
        self.last_buy_slot = 0
        self.done = False
        self.complete = False


class Held:
    """Bookkeeping for one open position."""

    __slots__ = ("opened_slot", "tx_paid", "best", "dev_sold_at_open")

    def __init__(self, opened_slot, tx_paid, dev_sold_at_open):
        self.opened_slot = opened_slot
        self.tx_paid = tx_paid
        self.best = 0
        self.dev_sold_at_open = dev_sold_at_open


def sell_value(tokens, vq, vt):
    """What selling `tokens` against (vq, vt) would pay after curve fees."""
    if tokens <= 0 or vt <= 0:
        return 0
    q = (tokens * vq) // (vt + tokens)
    return q - (q * CURVE_FEE_BPS + BPS - 1) // BPS


class Strategy:
    def __init__(self, params=None):
        self.p = dict(PARAMS)
        if params:
            self.p.update(params)
        self.coins = {}  # mint -> Coin, first-seen order
        self.held = {}  # mint -> Held
        self.selling = set()  # mints with a sell in flight

    def _flow(self, coin, slot):
        """Net lamports bought minus sold, and distinct buyers, over the
        last flow_window slots."""
        p = self.p
        from_slot = slot - p["flow_window"]
        drop = 0
        while drop < len(coin.recent) and coin.recent[drop][0] <= from_slot:
            drop += 1
        if drop:
            del coin.recent[:drop]
        net = 0
        buyers = set()
        for (_s, is_buy, sol, user) in coin.recent:
            if is_buy:
                net += sol
                if user != coin.creator:
                    buyers.add(user)
            else:
                net -= sol
        return net, len(buyers)

    def _read_events(self, batch):
        for e in batch["events"]:
            if e["event"] == "CreateEvent":
                self.coins[e["mint"]] = Coin(
                    e["mint"], e["slot"], e["creator"],
                    int(e["virtual_quote_reserves"]),
                    int(e["virtual_token_reserves"]),
                )
            elif e["event"] == "TradeEvent":
                c = self.coins.get(e["mint"])
                if c is None:
                    continue
                sol = int(e["sol_amount"])
                c.trades += 1
                c.last_trade_slot = e["slot"]
                c.recent.append((e["slot"], e["is_buy"], sol, e["user"]))
                is_dev = e["user"] == c.creator
                if e["is_buy"]:
                    c.bought += sol
                    if is_dev and e["slot"] == c.create_slot:
                        c.dev_buy += sol
                    elif not is_dev:
                        c.buyers.add(e["user"])
                elif is_dev:
                    c.dev_sold = True
                else:
                    c.sold_by_others += sol
                    rq_before = int(e["real_quote_reserves"]) + sol
                    if rq_before > 0:
                        bps = (sol * BPS) // rq_before
                        if bps > c.big_sell_bps:
                            c.big_sell_bps = bps
            else:  # CompleteEvent
                c = self.coins.get(e["mint"])
                if c is not None:
                    c.complete = True
                    c.done = True

    def on_batch(self, state, batch):
        p = self.p
        self._read_events(batch)
        tx = state.tx_fee_lamports()
        cap = state.hello["caps"]["max_orders_per_batch"]
        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = (
                int(m["virtual_quote_reserves"]),
                int(m["virtual_token_reserves"]),
            )

        # Fills: track held positions (harness closes start with "_").
        for f in batch["fills"]:
            if f["client_id"].startswith("_"):
                self.held.pop(f["mint"], None)
                self.selling.discard(f["mint"])
                continue
            h = self.held.get(f["mint"])
            if h is not None and f["status"] != "rejected":
                h.tx_paid += int(f["tx_fee_lamports"])
            if (
                f["side"] == "buy"
                and f["status"] == "filled"
                and f["mint"] not in self.held
            ):
                c = self.coins.get(f["mint"])
                self.held[f["mint"]] = Held(
                    f["slot"], int(f["tx_fee_lamports"]),
                    c.dev_sold if c is not None else False,
                )
        for mint in list(self.selling):
            pending_sell = False
            for po in state.pending.values():
                if po.order["mint"] == mint and po.order["side"] == "sell":
                    pending_sell = True
                    break
            if not pending_sell:
                self.selling.discard(mint)
        for mint in list(self.held):
            if mint in state.positions or mint in self.selling:
                continue
            pending_buy = False
            for po in state.pending.values():
                if po.order["mint"] == mint and po.order["side"] == "buy":
                    pending_buy = True
                    break
            if not pending_buy:
                del self.held[mint]

        orders = []

        # Exits first.
        for mint in sorted(state.positions):
            if len(orders) >= cap:
                break
            if mint in self.selling:
                continue
            pos = state.positions[mint]
            c = self.coins.get(mint)
            h = self.held.get(mint)
            if h is None:
                h = Held(state.slot, tx, c.dev_sold if c is not None else False)
                self.held[mint] = h
            mk = marks.get(mint)
            if mk is None:
                curve = state.curves.get(mint)
                if curve is None:
                    continue
                mk = (curve.virtual_quote_reserves, curve.virtual_token_reserves)
            value = sell_value(int(pos.token_amount), mk[0], mk[1]) - tx
            basis = int(pos.cost_lamports) + h.tx_paid
            if value > h.best:
                h.best = value
            exit_now = False
            if basis > 0:
                if value * BPS >= basis * (BPS + p["tp_bps"]):
                    exit_now = True
                if value * BPS <= basis * (BPS - p["sl_bps"]):
                    exit_now = True
                if (
                    h.best * BPS >= basis * (BPS + p["trail_arm_bps"])
                    and value * BPS <= h.best * (BPS - p["trail_bps"])
                ):
                    exit_now = True
            if state.slot - h.opened_slot >= p["max_hold"]:
                exit_now = True
            if c is not None:
                if p["exit_on_dev_sell"] and c.dev_sold and not h.dev_sold_at_open:
                    exit_now = True
            if not exit_now:
                continue
            orders.append(
                {
                    "side": "sell",
                    "mint": mint,
                    "token_amount": pos.token_amount,
                    "max_slippage_bps": 10_000,
                }
            )
            self.selling.add(mint)
            if c is not None:
                c.done = True

        # Entries: first buy only, in first-seen order.
        free = state.free_cash_lamports() - tx * len(orders)
        open_slots = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        stake = p["stake_lamports"]
        min_buy = p["min_buy_lamports"]
        for c in self.coins.values():
            if len(orders) >= cap:
                break
            if c.done or c.complete:
                continue
            age = state.slot - c.create_slot
            if age > p["max_age"]:
                c.done = True
                continue
            if age < p["min_age"]:
                continue
            if c.buys_sent > 0:
                continue
            if open_slots <= 0:
                continue
            pending_buy = False
            for po in state.pending.values():
                if po.order["mint"] == c.mint and po.order["side"] == "buy":
                    pending_buy = True
                    break
            if pending_buy or c.mint in self.selling:
                continue
            curve = state.curves.get(c.mint)
            if curve is None or curve.complete:
                continue
            if c.dev_sold and p["skip_dev_sold"]:
                c.done = True
                continue
            # Rug filter.
            rq = curve.real_quote_reserves
            if rq < p["min_real_sol"] or rq > p["max_real_sol"]:
                continue
            if c.dev_buy > p["max_dev_buy"]:
                continue
            if c.trades < p["min_trades"] or len(c.buyers) < p["min_buyers"]:
                continue
            if len(c.buyers) > p["max_buyers"]:
                continue
            if c.bought > 0 and c.sold_by_others * BPS > c.bought * p["max_sell_ratio_bps"]:
                continue
            if c.big_sell_bps > p["max_big_sell_bps"]:
                continue
            runup = (curve.virtual_quote_reserves * c.vt0 * BPS) // (
                curve.virtual_token_reserves * c.vq0
            ) if curve.virtual_token_reserves and c.vq0 else 0
            if runup > p["max_runup_bps"] or runup < p["min_runup_bps"]:
                continue
            net_flow, recent_buyers = self._flow(c, state.slot)
            if net_flow < p["min_flow_lamports"] or recent_buyers < p["min_recent_buyers"]:
                continue
            max_buy = state.max_buy_lamports(c.mint)
            amount = stake if stake < max_buy else max_buy
            if amount < min_buy:
                continue
            if free < amount + tx:
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": c.mint,
                    "quote_lamports": amount,
                    "max_slippage_bps": p["buy_slippage_bps"],
                }
            )
            free -= amount + tx
            c.buys_sent += 1
            c.spent_sent += amount
            c.last_buy_vq = curve.virtual_quote_reserves
            c.last_buy_vt = curve.virtual_token_reserves
            c.last_buy_slot = state.slot
            open_slots -= 1
        return orders


STRATEGY = Strategy()


def on_batch(state, batch):
    return STRATEGY.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bot.run(on_batch)
