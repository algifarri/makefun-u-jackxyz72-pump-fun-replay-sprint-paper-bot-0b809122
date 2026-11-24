[![Pump.fun Replay Sprint](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![Rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/algifarri/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![Reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/algifarri/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

# Pump.fun Replay Sprint — Paper Bot

A launch sniper with a rug filter for the [Pump.fun Replay Sprint](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) (agent-fun/replay/v2). It trades 10 SOL of paper money on a 24h replay of pump.fun bonding-curve events: 5 open positions max, 0.1 SOL per coin, one buy per coin, no shorts, no leverage.

## Strategy

Watch every coin from its `CreateEvent` and buy only launches that pass the rug filter in their first 3 slots:

- the creator has not sold, and the creator's own opening buy is small (≤ 2.5 SOL);
- at least 3 distinct non-creator wallets bought in the open (a launch bundled by many wallets is usually the creator's cluster waiting to dump — dev-window finding: ≤ 1 buyer almost never pays, ≥ 3 more than doubles the +15%-hit rate);
- the curve already holds ≥ 3 SOL real (coins below 1 SOL real at age 3 almost never pay);
- the price already ran ≥ 30% from create (dev-window finding: a ≥ 20% run-up at age 3 lifts the +15%-hit rate from 23% to ~40%).

Exits are fast, because pump.fun spikes reverse within seconds: take profit +25%, stop loss −8%, a trailing stop armed from the first tick in profit (exit on a 5% pullback from the peak), a 20-slot time limit, and an immediate exit when the creator sells. One order per buy/sell — every extra order costs another 105,000-lamport tx fee, and every round trip is ≥ 0.01 SOL, so each filled position counts toward the 20-distinct-mint guardrail.

The bot is deterministic by construction: no clock, no randomness, no I/O, iteration only over insertion-ordered dicts and sorted position lists. It runs as `python3 -P -s main.py` (stdlib only) on the kit's vendored `vendor/makefun_bot.py` shim (byte-identical to the kit's `sdk/python/makefun_bot.py`).

## Local calibration (dev window, all 3 dataset parts, 1,222 mints)

Simulated with a faithful Python port of the kit's replay engine (fees 95 + 30 bps, 105,000-lamport tx fee, 2-slot latency, seeded 2% failed-tx draw), driving the real `main.py` over stdio:

| | net PnL | fills | distinct mints | qualifying round trips | max drawdown |
|---|---|---|---|---|---|
| as submitted | **−0.155 SOL** | 102 | 51 | 51 | 2.1% |
| without failed txs | −0.143 SOL | 106 | 53 | 53 | 2.1% |
| at 2x fees | −0.252 SOL | 102 | 51 | 51 | 2.7% |

Guardrails are met with wide margin (51 qualifying mints vs 20 required; 2.1% drawdown vs 50% allowed). The dev window is a hard regime for every approach tried here — the kit's own momentum pilot scores −9.38 SOL, its reversion pilot −4.08 SOL, and a later-stage momentum rider −3.22 SOL on the same window, all with far worse drawdowns — so this entry is tuned for small, fast, diversified wins and survival rather than a dev-window best cell. Two stdio runs produce byte-identical action logs (63,369 replies).

## Files

- `main.py` — the entrypoint: strategy + protocol loop.
- `vendor/makefun_bot.py` — the kit's agent-fun/replay/v2 Python shim, vendored byte for byte.
- `LICENSE` — MIT.
