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
three-stage pipeline, and the final call is a friction-adjusted expected-value
estimate of what *copying* them transfers to you — which is not the same as
what *they* make. **The whole pipeline ships inside the bot**: a
"Scan for copyable traders" button runs it live (read-only, ~150 public API
calls, 2–3 min) and ranks the current leaderboard by net copy edge, with a
copy button next to anyone who passes.

### The model

Copying trader $T$ means: every time $T$ opens a position, you buy the same
token within seconds at (approximately) the same price, with your own sizing.
Your expected profit per mirrored dollar is *their* skill minus *your* costs:

$$\widehat{e}_w \;=\; \frac{\mathrm{PnL}_w}{V_w} \qquad w \in \{7\mathrm{d},\,30\mathrm{d}\}$$

where $\mathrm{PnL}_w$ is their profit over window $w$ and $V_w$ their traded
volume — their realized edge per dollar pushed through the market. Two windows
give a cheap regime check: a hot week on top of a flat month reads very
differently from a consistently earning month.

Costs are dominated by **spread crossings**. A marketable order pays roughly
half the spread plus impact each time it crosses the book. If the trader holds
to resolution, settlement is frictionless — you pay one crossing. If they exit
early, your copy exits too and pays a second:

$$c \;=\; 1 + \underbrace{\frac{\text{sell fills}}{\text{all fills}}}_{\text{sell ratio}} \qquad\qquad \mathrm{net}_w \;=\; \widehat{e}_w - c \cdot f$$

with $f$ the friction per crossing. **A trader is armed only if
$\min_w \mathrm{net}_w > 0$** — the edge must survive friction even under the
pessimistic estimate. This single inequality is what killed the most tempting
candidate of the run (see rejects below).

### Where the 2.3% friction constant comes from

$f$ was measured, not assumed: at ~$5 order size on World-Cup-liquidity books,
crossing the spread on a FAK marketable order cost ≈2.3% of notional on
average (half-spread + queue slippage). Copy-lag, surprisingly, costs nothing
at this size — the bot records the drift between the target's fill price and
its own achievable price on **every live copy**:

> **55 live copies: median drift −0.5pp, mean −0.35pp** (negative = we filled
> *better* than the target), worst case +1.0pp — because the no-chase gate
> refuses any copy where the price already ran more than 2% past the target's
> fill. Lag risk is capped by construction; the spread is the real cost.

### Stage 1 — survival screens

From the 7d/30d leaderboards (top 50 each), a candidate survives if their
30-day equity curve shows:

- profit > \$5k **and** ≥45% green days (steady accumulation, not one lucky hit)
- max drawdown < 70% of the month's profit — formally
  $\max_t \left(\max_{s\le t} P_s - P_t\right) < 0.7 \cdot (P_{30} - P_0)$,
  which filters the all-in martingale cowboys who eventually donate everything back
- traded within the last 3 days (a hot hand that went quiet is unverifiable)

### Stage 2 — copyability

Profitable is not the same as copyable:

- **Market-maker/HFT bots** (order rate > ~300/day, or median clip < \$20 at
  >120/day) earn the spread — the exact thing a copier *pays*. Mirroring a
  market maker is structurally negative-EV regardless of their P&L.
- **Horizon mix**: the bot only copies markets resolving within ~2 days
  (configurable), so ≥80% of the candidate's buy flow must live there. This is
  measured per-market from on-chain end dates, with a title heuristic for
  sports markets the APIs won't describe. It also caps capital lock-up: money
  parked 6 months in a politics future has brutal opportunity cost at a \$40
  bankroll.
- **Account age**: a 5-day-old account with a +\$3.3M week is indistinguishable
  from luck (or wash trading). No record depth, no arm.

### Stage 3 — the roster this run (measured Jul 6, live APIs)

| Trader | 7d P&L | 7d volume | sell% | crossings $c$ | ≤2d flow | net copy edge $[\min_w, \max_w]$ | verdict |
|---|---|---|---|---|---|---|---|
| NonceChaser | +$568k | $371k | 9% | 1.09 | mixed¹ | **+39% … +150%** | armed |
| MD14 | +$386k | $1.95M | 2% | 1.02 | 100% | **+4.0% … +17.5%** | armed |
| RISK-IS-NEVER-OK | +$553k | $719k | 1% | 1.01 | 100% | **+17% … +75%** | armed |

¹ NonceChaser pivoted into 6-month politics futures mid-run; the horizon cap
automatically skips those, so only his short-horizon flow is mirrored.

**Rejected, same math:**

| Candidate (anonymized where fair) | Numbers | Failing grade |
|---|---|---|
| muchobliged | +$3.3M in 7d, account age **5 days** | no persistence evidence — luck and skill are indistinguishable at n≈1 week |
| Mind.The.Gap | strong gross edge, **sell-heavy flipper** → $c \approx 2$ | $\mathrm{net}_{\min} < 0 < \mathrm{net}_{\max}$: the double crossing eats the transferable edge; one −$64k day confirmed the variance |
| several (e.g. 300–500 orders/day, $10 median) | mm-bots | copier pays the spread the bot earns |
| several | net edge ∈ [−0.1%, +2%] | statistically indistinguishable from zero after friction |

### Known limitations of the estimator (read before trusting it)

- $\mathrm{PnL}_w/V_w$ mixes realized and mark-to-market profit; a whale
  marking up his own illiquid positions inflates $\widehat{e}$. The two-window
  bound and drawdown screen mitigate, not eliminate, this.
- Leaderboards are survivorship-biased by construction — the screen can only
  rank *visible* survivors, which is why the consistency requirements matter
  more than the headline P&L.
- All of this was measured during World Cup 2026 (ends Jul 19) — a uniquely
  liquid, fast-resolving regime. The roster gets re-screened when it ends.
- Sample sizes are honest but small: 4 days, 29 settled copies. The math picks
  *plausible* edges; it cannot promise them.

## Features

- **Real-time copying** — WebSocket stream of platform trades (sub-second reaction), REST polling as reconciliation + fallback, per-trade dedupe across both paths
- **Risk gates on every copy** — no stacking (one position per market regardless of how many fills the target sprays), no chasing (skips if the price ran past the target's fill + slippage), horizon cap (skip markets resolving beyond N days), failed-buy cooldown
- **Auto-budget** — spend cap follows the wallet (total − reserve) and per-trade size scales with it, so the bot breathes with wins and losses without manual bumps
- **Chain-level reconciliation** — detects zero-filled FAK orders and auto-swept resolved positions by reading balances and the Conditional Tokens payout vector straight from Polygon, so the ledger stays true even when every Polymarket API is blind (negRisk markets)
- **Multi-trader with attribution** — every copy, skip and log line names which target it came from
- **Built-in trader scout** — one click re-runs the whole selection pipeline against the live leaderboard and ranks candidates by friction-adjusted net copy edge, with per-row copy buttons; results stream in as each trader is analyzed
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
