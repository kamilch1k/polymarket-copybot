# polymarket-copybot

Single-file Polymarket copy-trading bot with a native desktop UI, quantitative
trader selection, risk gates on every copy, and chain-level reconciliation.
Watches one or more traders and mirrors their trades at a fraction of their size.

Built in a few days of pair-programming with Claude, then left running unattended
with real (small) money. Everything below is from that live run.

## Live results — first 4 days (Jul 3–7, 2026)

![cumulative P&L](docs/pnl.svg)

| Metric | Value |
|---|---|
| Bankroll at start | $33.11 |
| Balance at current marks (Jul 7 evening) | **$87.94** — $35.87 cash + $52.06 in open positions |
| Cumulative P&L, Polymarket accounting (hourly series, through Jul 7 20:00) | **+$27.88** (peak +$33.74, deepest trough −$1.54) |
| Settled copies | 29 — **18 won / 11 lost (62%)** |
| Capital returned by wins | $108.76 |
| Copies that never filled (FAK zero-fills, $0 moved, auto-refunded) | 6 |
| Biggest single-match result | Portugal–Spain: 3 legs, 3 wins, ~+$12.9 on ~$16 staked |

Honest caveats: 4 days is a tiny sample; a chunk of the current balance is
mark-to-market on open in-play positions and can still swing; every edge was
measured during World Cup 2026, a uniquely liquid regime that ends July 19 —
the roster gets re-screened after that. Nothing here is financial advice.

## Why these traders — the selection math

The bot doesn't copy whoever tops the leaderboard. Candidates go through a
screening pipeline, and the final call is a friction-adjusted expected-value
estimate of what *copying* them transfers to you (which is not the same as
what *they* make).

**Stage 1 — survival screens** (leaderboard top-50 across 7d/30d/all windows):

- 30-day profit above $5k, with the equity curve ≥45% green days
- max drawdown < 70% of the 30-day profit (no all-in cowboys)
- weekly consistency: profitable in most week-buckets of the month
- traded within the last 3 days; account old enough to have a real record

**Stage 2 — copyability.** A trader can be profitable and still uncopyable:

- market-maker/HFT bots (hundreds of orders/day, ~$10 median size) are noise —
  their edge is the spread itself, which a copier pays instead of earns
- horizon mix: we only copy markets resolving within ~2 days (configurable),
  so a trader's edge has to live in short-horizon markets, ≥80% of their flow

**Stage 3 — net copy edge.** For each window *w* ∈ {7d, 30d}:

```
edge_w     = PnL_w / Volume_w              (what they earn per $ traded)
crossings  = 1 + sell_ratio                (a flipper pays the spread twice;
                                            hold-to-resolution settles free)
friction   = 2.3% per crossing             (measured at our order size)
net_w      = edge_w − crossings × friction
```

A trader is only armed if **net is positive at both bounds** — the pessimistic
and optimistic window must both survive friction.

**The roster this run** (measured Jul 6, 7-day windows):

| Trader | 7d P&L | 7d volume | sell% | short-horizon | net copy edge |
|---|---|---|---|---|---|
| NonceChaser | +$568k | $371k | 9% | mixed¹ | +39% … +150% |
| MD14 | +$386k | $1.95M | 2% | 100% | +4.0% … +17.5% |
| RISK-IS-NEVER-OK | +$553k | $719k | 1% | 100% | +17% … +75% |

¹ NonceChaser pivoted into long-horizon politics mid-run; the 2-day horizon cap
automatically trims those, so only his short-horizon flow gets copied.

**Rejected, with reasons** (same math, failing grades): a +$3.3M/week whale
with a 5-day-old account (no persistence evidence); a high-volume flipper whose
sell-heavy style pays friction twice, leaving net edge straddling zero; several
mm-bots with 200–500 orders/day; and profitable-looking accounts whose net edge
after friction rounded to ~0.

## Features

- **Real-time copying** — WebSocket stream of platform trades (sub-second reaction), REST polling as reconciliation + fallback, per-trade dedupe across both paths
- **Risk gates on every copy** — no stacking (one position per market regardless of how many fills the target sprays), no chasing (skips if the price ran past the target's fill + slippage), horizon cap (skip markets resolving beyond N days), failed-buy cooldown
- **Auto-budget** — spend cap follows the wallet (total − reserve) and per-trade size scales with it, so the bot breathes with wins and losses without manual bumps
- **Chain-level reconciliation** — detects zero-filled FAK orders and auto-swept resolved positions by reading balances and the Conditional Tokens payout vector straight from Polygon, so the ledger stays true even when every Polymarket API is blind (negRisk markets)
- **Multi-trader with attribution** — every copy, skip and log line names which target it came from
- **Copy button + green tint** — missed trades (bot offline, restart baseline) appear in the live feed; rows that would genuinely copy glow green, one click replays them through the exact same gates
- **Native app or headless** — pywebview window on desktop, `--headless` for a VPS under systemd (`vps/` has the full bootstrap: service unit, setup script, API-driven server provisioning)
- **Claude copilot** — `claude -p` with a live bot-state snapshot; explains trades/skips and can tune settings via a whitelisted action protocol (it can never place trades or read the key)
- **Observability** — on-chain wallet panel (cash + positions + P&L, including API-blind holdings), persistent trade history, external watchdog script (health, connection, missed-trade audit) suitable for cron
- **Self-testing** — `--check` runs an offline suite of ledger/gate/UI unit tests; the deploy script refuses to ship if it fails

## Run

```
pip install -r requirements.txt
python copybot.py             # desktop app window
python copybot.py --headless  # engine + local web UI only (VPS/service mode)
python copybot.py --check     # offline self-test
python watchdog.py            # external health + missed-trade audit
```

Configure everything in the UI (http://127.0.0.1:8777 when headless). Config —
including the private key, by explicit owner's choice — persists to
`copybot_config.json`: **gitignored, never committed** (the full git history is
scanned for key material as part of the release checklist), plaintext,
single-user machine assumption. Use a dedicated wallet holding only what you
can afford to lose.

For a $5/mo always-on deployment see `vps/`: `provision.ps1` creates the server
through the Hetzner API, `setup.sh` bootstraps it (venv, firewall = SSH only,
UI loopback-bound, systemd service, auto security updates), `push.ps1` ships
updates.

## Safety model

- Boots watch-only unless fully configured; approve mode queues every copy for a manual ✓
- SELLs only what the bot itself bought, clamped to the actual on-chain balance
- Buys capped per-trade and by a hard budget; losses stay counted against it (drawdown self-throttle)
- The copilot can tune settings but can never place trades or read the key
- The trading wallet should only ever hold the bankroll — that's the real security boundary

## Disclaimer

This is a hobby project that trades real money badly or well depending on the
week. Prediction markets are gambling-adjacent. Past performance of a 4-day
World-Cup-season sample predicts nothing. Run it with money you can lose.
