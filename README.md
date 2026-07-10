# polymarket-copybot

Single-file Polymarket copy-trading bot with a native desktop UI, quantitative
trader selection, risk gates on every copy, and chain-level reconciliation.
Watches one or more traders and mirrors their trades at a fraction of their size.

Built in a few days of pair-programming with Claude, then left running unattended
with real (small) money. Everything below is from that live run.

## Live results — the first week (Jul 3–10, 2026)

| Metric | Value |
|---|---|
| Bankroll at start | $33.11 |
| Balance at current marks (Jul 10, 23:00) | **$50.83** — $39.47 cash + $11.36 in open positions (**+53% since start**, including two genuinely red days mid-week; the intraday marks once touched ≈$88 on thin in-play books before the Argentina–Egypt basket died) |
| Cumulative P&L | **≈ +$17.7 at CLOB-midpoint marks** ($50.83 balance − $33.11 start); Polymarket's hourly accounting reads +$13.9 — it marks open positions at exchange prices and lags midpoints |
| Settled copies | 44 — **26 won / 18 lost (59%)** — audited: every settlement cross-checked against the on-chain payout vector; 8 early entries were re-classified by that audit (see caveats) |
| Capital returned by wins | $135.98 |
| Copies that never filled (FAK zero-fills, $0 moved, auto-refunded) | 2 (an earlier count of 7 was the reconciliation bug described in the caveats — the chain audit reclassified the rest as real fills) |
| Best single-match result | Portugal–Spain: 3 legs, 3 wins, ~+$12.9 on ~$16 staked |
| Worst single match | Argentina–Egypt (Jul 7): 7 legs, ~$24 staked, net ≈ **−$12** — four died, two small legs won, one exited ≈flat via a mirrored sell |

Every trade above is independently verifiable on-chain:
**[my live Polymarket profile](https://polymarket.com/@0x8df3ad8dd5893b65c23d8b3263b00fc507a1a75e-1780997991335)** —
the bot's wallet, fills, and P&L are public record, not screenshots.

Honest caveats: a week is a tiny sample, and the daily P&L is **not** a green
staircase — Polymarket's daily closes read **−$1.5, +$18.6, +$3.6, −$0.3,
−$4.9, −$4.8, $0.0, +$3.2**. The two red days are the Argentina–Egypt basket
(seven correlated in-play legs on one match — the case study behind the
correlation math below) and its aftermath; the flat day is downtime, not
discipline: a budget-odometer freeze and a transient dependency failure each
parked the bot for hours (both root-caused and fixed in commits `5dd06de` and
`2987332` — this page doesn't pretend ops is free). Honesty note on the
ledger itself: a reconciliation bug (found, chain-verified and shipped fixed
the same evening — commit `572d5d4`) had been mislabeling already-swept
settlements as "never filled" refunds; all eight affected entries were
re-verified against the Conditional Tokens payout vector on Polygon and
corrected, and the numbers above are the audited ones. Open-position marks
still swing; most edges were measured
during World Cup 2026, a uniquely liquid regime that ends July 19 — the
roster gets re-screened after that (the Jul 8 additions include a year-round
tennis specialist precisely to outlive it). This page is updated from the
live ledger, drawdowns included. Nothing here is financial advice.

### Not just football

The roster's edge travels across categories, and the run already proves it:

- **Esports (League of Legends, BO5 series)** — same-day resolution, deep
  in-play books. One copy rode MD14's T1 accumulation; another (Team Secret
  Whales) resolved LOST — both settled within hours, exactly the turnover
  profile the horizon math wants. A Dota 2 copy FAK'd into a dust book and
  became a clean $0 zero-fill, auto-refunded by the reconciler.
- **Crypto candle markets ("Bitcoin Up or Down, 10:45–11:00AM")** — the purest
  short-horizon instrument on the platform: 15-minute resolution. The copy
  entered 17:52, **won at 18:00** — eight minutes from signal to settled cash.
  Maximum capital velocity, but spread-sensitive: friction is a huge fraction
  of a 15-minute edge, so only high-conviction fills clear the gates.
- **Baseball (MLB run-line spreads)** — daily resolution; a $1 Brewers −1.5
  copy filled and settled a win (this page briefly called it a zero-fill —
  the chain audit corrected that).
- **Politics & macro (Fed meetings, elections, geopolitics)** — the targets
  trade these heavily; the bot deliberately *skips* them today. The full
  reasoning — and the math for when copying them becomes correct — is in the
  long-horizon section below.

## Why these traders — the selection math

The bot doesn't copy whoever tops the leaderboard. Candidates go through a
three-stage pipeline, and the final call is a friction-adjusted expected-value
estimate of what *copying* them transfers to you — which is not the same as
what *they* make. **The whole pipeline ships inside the bot**: a
"Scan for copyable traders" button runs it live (read-only, ~150 public API
calls, 2–3 min) and ranks the current leaderboard by net copy edge, with a
copy button next to anyone who passes.

### The model — a proper derivation

**Setup and assumptions.** Copying trader T means: each time T buys a token
at price p_T, the bot buys the same token within seconds at price
p′ ≤ p_T(1+s), with its own sizing. Three assumptions, each labeled by how it
is checked:

- **A1 — stationary edge (tested ex post):** over the estimation window T's
  fills beat fair value by e per dollar on average, and the window's e
  predicts the copy period's e. Unverifiable in advance — this is precisely
  what the two-window bound and survival screens hedge, and what the
  end-to-end transfer table tests after the fact.
- **A2 — price-taking (true by construction):** our order is far too small to
  move the book or T's behavior — $1–5 clips against $10⁴–10⁵ books.
- **A3 — bounded fill-quality gap (enforced and measured):** our entry
  differs from T's only by a drift δ = (p′ − p_T)/p_T, hard-capped at
  s = 2% by the no-chase gate and recorded on every live copy.

**Lemma (expected transfer per mirrored dollar).** With e as above, f the
cost of one spread crossing, q the probability T exits early (holding to
resolution settles at face value, frictionlessly), and δ the entry drift:

```math
\mathbb{E}[\pi] \;=\; e \;-\; f\,(1+q) \;-\; \mathbb{E}[\delta],
\qquad \mathbb{E}[\delta] \;\le\; s
```

*Proof.* Write the copy's per-dollar return as T's return minus everything
about our execution that differs from his. By A2 the outcome distribution of
the position itself is unchanged, so the copy collects e by construction
(same token, same side). The execution differences decompose disjointly: the
entry always crosses the book once (−f); with probability q the target exits
early and the mirrored exit crosses again (−qf in expectation, by linearity);
entering at p′ instead of p_T costs the premium δ, bounded by s under A3.
Resolution itself needs no order. Summing the disjoint costs gives the
claim. ∎

Measured over 55 live copies, E[δ] ≈ −0.35pp (median −0.5pp) — *negative*:
the bot on average fills slightly **better** than the target, because the
no-chase gate only admits copies whose price hasn't already run. Dropping
the δ term, as the arming rule below does, is therefore conservative.

**Estimators.** Neither e nor q is observable directly, so both are estimated
from public fills over two windows w ∈ {7d, 30d}:

```math
\widehat{e}_w=\frac{\mathrm{PnL}_w}{V_w},
\qquad
\widehat{q}=\frac{\#\,\text{sell fills}}{\#\,\text{all fills}},
\qquad
\widehat{\mathrm{net}}_w=\widehat{e}_w-(1+\widehat{q})\,f
```

PnL-over-volume is their realized profit per dollar pushed through the market;
two windows are a cheap regime check (a hot week on a flat month reads very
differently from a consistently earning month) and yield an interval estimate
rather than a point.

**Arming rule.**

```math
\min_{w\in\{7\mathrm{d},30\mathrm{d}\}} \widehat{\mathrm{net}}_w \;>\; 0
```

— the edge must survive friction under the *pessimistic* estimate. This single
inequality is what killed the most tempting candidate of the run (see rejects
below).

Why so strict? Because a leaderboard is an extreme-value machine: screen K
candidates whose measured edges carry estimation noise σ_ε and the largest
*pure-noise* estimate you will see grows like

```math
\mathbb{E}\Big[\max_{k\le K}\varepsilon_k\Big] \;\approx\; \sigma_\varepsilon\sqrt{2\ln K}
```

— at K ≈ 100 that is ≈ 3σ of pure flattery before any skill is proven (the
winner's curse). One hot window cannot honestly clear that hurdle;
consistency across independent windows and a long record can. This is why
the pipeline weighs record depth as heavily as the point estimate.

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

### Does the edge actually transfer? Measured end-to-end

The whole model stands or falls on one question: does a copied dollar earn
what the lemma says it should? Measured mid-run (Jul 7), it checks directly —
realized profit per staked dollar on our fills, against the model's
prediction from the target's own numbers:

```math
\text{realized transfer} \;=\; \frac{\sum_{\text{copies}} \pi}{\sum_{\text{copies}} \text{staked}}
\qquad\text{vs. predicted}\qquad
\widehat{e}_{7d} - (1+\widehat{q})\,f
```

| target (that week) | their ê₇d | predicted net copy edge | our realized per staked $ |
|---|---|---|---|
| RISK-IS-NEVER-OK (+$310k on $1.14M) | +27.3% | ≈ +25% | **≈ +28%** (19 copies) |
| MD14 (−$133k on $3.0M) | −4.4% | ≈ −6.7% | **≈ −6.4%** (11 copies) |

The transfer tracks in **both directions** — sign and rough magnitude. Copying
a trader's winning week captured essentially his full per-dollar edge (the
no-chase gate means we sometimes fill *better* than him); copying a trader's
losing week faithfully delivered the loss the model predicted. The mechanism
is sound; what it cannot do is make a $46 bankroll feel like his: at 19 copies
per trader-week, one correlated in-play basket (seven legs on a single match)
swings a fifth of the bankroll at once. That's a variance problem, not an
edge problem — and it's the argument for a per-match exposure cap, the next
gate on the list. Sample sizes are tiny; treat the decimals as illustration,
the signs as evidence.

### Stage 1 — survival screens

From the 7d/30d leaderboards (top 50 each), a candidate survives if their
30-day equity curve shows:

- profit > $5k **and** ≥45% green days (steady accumulation, not one lucky hit)
- max drawdown < 70% of the month's profit — on the equity curve P:

```math
\max_{t}\Big(\max_{s\le t} P_s - P_t\Big) \;<\; 0.7\,(P_{30}-P_0)
```

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

### Stage 3 — the roster (re-measured Jul 7–8, live APIs)

| Trader | copying since | 30d P&L | 7d volume | sell% | ≤2d flow | net edge [min, max] | status |
|---|---|---|---|---|---|---|---|
| RISK-IS-NEVER-OK | Jul 3 | +$863k | $1.14M | 1% | 100% | **+15% … +25%** | armed — the run's engine (+$22 realized for us) |
| cnyek | Jul 8 | +$1.34M | $2.24M | 3% | 88% | **+12% … +17%** | armed — 2-month record, ~11 deliberate ~$46 clips/day |
| Eztennis | Jul 8 | +$3.08M | $6.32M | 6% | 88% | **+9% … +40%** | armed — 6-month record, **94% tennis**: the edge that outlives Jul 19 |
| 0x3DFb… | Jul 8 | +$1.92M | $3.45M | 0% | 100% | **+11% … +33%** | armed — 23-month account, deepest record on the board |
| MD14 | Jul 3 | +$208k | $3.02M | 2% | 100% | **−6.8% … −0.7%** | ❗ fails the arming rule this week (−$133k 7d); kept by owner's decision, under review |
| NonceChaser | Jul 3 | +$652k | $198k | 12% | 6% | −4.5% … +74% | passenger — politics pivot: the horizon cap skips ~94% of his flow |

**Why those three additions — the checks past the leaderboard.** Every Jul 8
add had to clear two screens the headline number can't fake:

1. **Fill anatomy.** Median clip and raw fill cadence unmask fragmentation.
   The scan's top point-estimate, "RJW1" (+34…69% on paper), collapsed here:
   his flow prints as **$4-median fills at ~860/day** — a signal that arrives
   as unfollowable dust, where a FAK copier gets zero-fills and chase costs
   instead of edge. cnyek is the anti-case: ~11 fills/day at $46 median —
   every signal is deliberate, mirrorable conviction.
2. **Record depth**, per the winner's-curse bound above: 2 months (cnyek),
   6 months (Eztennis), 23 months (0x3DFb…) of survivable history versus the
   4-week wonders the extreme-value math says to distrust. Eztennis adds
   regime insurance on top — tennis resolves same-day, year-round, so the
   roster no longer dies with the World Cup.

**Rejected, same math:**

| Candidate (anonymized where fair) | Numbers | Failing grade |
|---|---|---|
| RJW1 | net edge **+34% … +69%** on paper | fills fragment to a $4 median at ~860 prints/day — capture ≈ 0 for a FAK copier (see fill anatomy above) |
| muchobliged | +$4.2M lifetime, account now **4 weeks** old | still the shallowest record on the board, 500 fills/hour in-play style — re-screen after Jul 19 |
| Mind.The.Gap | strong gross edge, **sell-heavy flipper** → crossings ≈ 2 | net edge interval straddles zero (min < 0 < max): the double crossing eats the transferable edge; one −$64k day confirmed the variance |
| several (e.g. 300–500 orders/day, $10 median) | mm-bots | copier pays the spread the bot earns |
| several | net edge ∈ [−0.1%, +2%] | statistically indistinguishable from zero after friction |

### The execution math — the gates every copy passes

Selection finds edge; execution keeps it. Each mirrored BUY at target price
p_T and target share count q_T, with wallet total W:

**Sizing** (flat, bankroll-scaled — a bounded stand-in for fractional Kelly,
since per-trade μ and σ are unknowable for someone else's signal; Kelly's
optimal fraction μ/σ² cannot be estimated, so the bot bounds the fraction
instead of pretending to know it):

```math
\text{notional} \;=\; \mathrm{clip}\!\big(\phi\, q_T\, p_T,\;\; 1,\;\; C\big),
\qquad C=\max(5,\;0.10\,W)\ \ \text{USD}
```

**No-chase gate** — copy only while the price hasn't outrun the signal. This
inequality is why measured drift stays ≤ +1pp: lag cost is capped by
construction, not by luck:

```math
p_{\text{now}} \;\le\; p_T\,(1+s), \qquad s = 2\%
```

**Horizon gate & capital velocity.** Only markets resolving within H (= 2
days) are copied. With per-copy net edge μ, capital fraction κ per copy, and
holding time τ, expected log-growth per day is

```math
g \;\approx\; \frac{u}{\tau}\,\ln(1+\kappa\mu)\;\approx\;\frac{u\,\kappa\,\mu}{\tau}
```

(utilization u, turnover 1/τ). Growth is *inversely proportional to holding
time*: the same 5% edge compounds ~90× faster in a two-day market than in a
six-month future. Short horizon isn't cosmetic — it is the compounding engine.

**Budget ratchet** (drawdown self-throttle). The odometer S counts money at
risk plus unhealed losses:

```math
S \;\leftarrow\; S + \text{cost(buy)} - \text{cost(settled win)} - \text{proceeds(sell)},
\qquad S \;\ge\; \sum_{\text{open}} \text{cost}_i
```

and a buy is allowed only while S + notional ≤ W − R (reserve R). Wins free
their stake; **losses stay counted** — a losing streak mechanically shrinks
what the bot may deploy next, with no human in the loop.

**Correlation — the measured hole in the gate set.** Every gate above is
per-*market*; nothing yet limits per-*event* stacking, and in-play legs on
the same match are strongly positively correlated. Variance adds by
covariance:

```math
\sigma^2_{\text{match}} \;=\; \sum_i \sigma_i^2 + \sum_{i\neq j}\rho_{ij}\,\sigma_i\sigma_j
\;\xrightarrow{\ \rho\to 1\ }\; \Big(\sum_i \sigma_i\Big)^2
```

so m fully-correlated legs behave like **one bet of m-fold size** — the √m of
apparent diversification is fictitious. The Argentina–Egypt night measured
this live: seven legs, ~$24 at risk, −$16 gross in a single game — 36% of the
run's entire loss column. The fix is a per-match exposure cap (the next gate
planned), which attacks ρ directly instead of anyone's edge.

**Breakeven check against realized results.** Copies entered at mean price
p̄ = 0.57 (median 0.61, n = 55). A binary position bought at p̄ and held to
resolution pays 1 on a win and 0 on a loss, so with entry friction f the
expected profit is WR·1 − p̄(1+f), giving the breakeven win rate

```math
\mathrm{WR}_{be} \;=\; \bar{p}\,(1+f) \;\approx\; 0.57 \times 1.023 \;\approx\; 58.3\%
```

Realized: **59%** over 44 settled copies — a ~+0.8pp margin over breakeven:
the right sign for a transferred edge, but thin enough that luck stays firmly
on the table at n = 44. Making that honesty exact, a one-sided binomial test
gives

```math
P\big(X \ge 26 \,\big|\, n=44,\ p=0.583\big) \;\approx\; 0.52
```

— the win count alone cannot yet distinguish this bot from a breakeven coin.
The evidence that the machine works lives in the per-dollar transfer table
above (sign **and** magnitude matched, in both directions), not in the raw
win rate; n has to grow before the win rate testifies either way.

### Known limitations of the estimator (read before trusting it)

- PnL/volume mixes realized and mark-to-market profit; a whale marking up his
  own illiquid positions inflates the edge estimate. The two-window bound and
  drawdown screen mitigate, not eliminate, this.
- Leaderboards are survivorship-biased by construction — the screen can only
  rank *visible* survivors, which is why the consistency requirements matter
  more than the headline P&L.
- All of this was measured during World Cup 2026 (ends Jul 19) — a uniquely
  liquid, fast-resolving regime. The roster gets re-screened when it ends.
- Sample sizes are honest but small: one week, 44 settled copies. The math picks
  *plausible* edges; it cannot promise them.

## Will *you* make money? — expected returns, honestly derived

Everything above argues the copy *mechanism* works. This section answers the
question that actually matters: **if you run this bot, what is the probability
distribution of your outcome** — and how does it compare to boring
alternatives? All parameters below are measured from the live ledger
(n = 61 fills, 44 settled copies, 18 distinct events, Jul 3–10).

### The per-copy return model

A copy stakes a fraction κ of the bankroll at mean entry price p̄ and resolves
binary. Measured: p̄ = 0.567, mean stake $2.93 on a ~$42 wallet → κ ≈ 0.07.

```math
R=\begin{cases}+a=\dfrac{1-\bar p}{\bar p}\approx +0.764 & \text{w.p. } w\\[4pt] -1 & \text{w.p. } 1-w\end{cases}
\qquad\Longrightarrow\qquad
\mu=\mathbb{E}[R]=\frac{w}{\bar p}-1,\quad \sigma^2=wa^2+(1-w)-\mu^2\approx 0.75
```

With the measured win rate w = 26/44 = 0.591:

```math
\widehat{\mu} \;=\; +4.2\%\ \text{per staked dollar},
\qquad 95\%\ \text{CI}:\ \big[\,{-21\%},\ +30\%\,\big]
```

That interval is the single most important number on this page: at n = 44 the
**sign of the edge is not statistically established** (consistent with the
binomial test above). Everything below is conditional on scenarios for the
true w.

### Volatility drag, and where the bot sits vs Kelly

Compounding doesn't earn μ — it earns the log-growth, which variance taxes:

```math
g(\kappa)=w\ln(1+\kappa a)+(1-w)\ln(1-\kappa)\;\approx\;\kappa\mu-\tfrac12\kappa^2\sigma^2,
\qquad \kappa^{*}_{\text{Kelly}}=\frac{\mu}{\sigma^2}\approx 5.6\%
```

The bot's realized κ ≈ 7% is ≈ 1.24× Kelly at the point estimates — mildly
aggressive (it burns ~7% of the theoretical growth rate; g/day 0.0069 vs
0.0074 at κ\*). The sharper warning cuts the other way: if the true edge sits
in the lower half of the CI, the *same* sizing is deep over-Kelly, where
expected log-growth is negative even with a positive-EV coin.

### Correlation makes the variance bigger than it looks

The 44 settled copies cluster into 18 events (mean cluster 2.4 legs, worst 8 —
Argentina–Egypt). Same-event legs are strongly correlated, so effective
variance carries a design effect d_eff = 1+(m̄−1)ρ ≈ **1.7** at ρ = 0.5.
Drift is unchanged; risk is ~70% larger than an independence assumption says.

### The month-ahead distribution

At ~6.3 settled copies/day, a month is N ≈ 189 copies and the log-outcome is
approximately normal:

```math
\ln\frac{W_{30}}{W_{0}}\;\sim\;\mathcal{N}\Big(N\,g(\kappa),\;\; N\,\mathrm{Var}\big[\ln(1+\kappa R)\big]\,d_{\text{eff}}\Big),
\qquad \text{month } \sigma_{\ln}\approx 1.1
```

The answer to "what are the chances of good returns," by true-win-rate
scenario — note the first row sits **inside** the confidence interval:

| true w scenario | median month | middle 50% of months | P(green month) | P(2×) | P(½×) |
|---|---|---|---|---|---|
| 0.550 — cold streak (inside the CI) | **−53%** | −78% … 0% | 25% | 10% | 52% |
| 0.567 — pure breakeven (w = p̄) | −30% | −67% … +48% | 37% | 17% | 38% |
| 0.583 — breakeven incl. modeled frictions | +2% | −51% … +115% | 51% | 27% | 26% |
| 0.591 — **measured** | **+23%** | −41% … +158% | **57%** | 33% | 21% |
| 0.650 — optimistic | +390% | +139% … +905% | 93% | 80% | 2% |

(Two breakeven rows because fills already embed the spread we crossed —
w = p̄ is the pure bar; 0.583 adds the residual frictions modeled earlier.)

### Reality checks on the table

- **Week 1 was lucky, quantifiably.** Realized ln(50.83/33.11) = 0.43 against
  the fitted model N(0.048, 0.53²) is a **z = +0.72** draw — within 1σ, mildly
  fortunate, and precisely the reason the median row above says +23%, not +53%.
- **The left tail is bounded by construction** — the cap follows
  wallet − $8 reserve, so the worst structural outcome is ≈ −84% of the
  current bankroll, not −100% (software risk aside; see the safety section).
- **Publication bias applies to us.** This analysis went up after a green
  week. The winner's-curse bound from the selection math applies to the
  authors as much as to the leaderboard.
- **Non-stationarity.** Every parameter was measured in World Cup 2026
  liquidity, which ends July 19. The "true w" column is not a constant of
  nature; it is a moving target the roster re-screen chases.

### Against normal methods

| vehicle | typical month | month σ (log) | P(green month) | absorbs real capital? |
|---|---|---|---|---|
| savings account (~4%/yr) | +0.33% | ≈ 0 | ~100% | yes |
| S&P index (long-run averages) | +0.7% | 0.044 | ≈ 56% | yes |
| **this bot** @ measured edge | median +23% | **1.10** | ≈ 57% | **no** — the edge lives at $1–7 clips |

Read that middle column twice: the bot offers *the same coin-odds of a green
month as an index fund* (57% vs 56%) at **~25× the volatility** — the entire
difference lives in the tails, both of them. The log-Sharpe at the measured
edge (≈ 0.19/month ≈ 0.65 annualized) is *comparable to simply holding
equities*, not better — before counting the capacity ceiling: FAK fill
quality collapses well below institutional size (the RJW1 fill-anatomy
lesson), so even a perfect month at the measured edge on this bankroll is
roughly **+$12 median**. 

**Bottom line, in words:** the math says this is a small, real-looking,
statistically unconfirmed edge riding on casino-grade variance with a hard
capacity ceiling. As entertainment with a positive tilt and an audited
ledger, it is exactly what it claims to be. As a substitute for investing, it
is not one — and this section is the proof.

## Long-horizon markets — why the bot skips them today, and when that flips

The roster trades six-month politics futures, Fed-meeting markets and election
props with visible success — and the bot deliberately copies none of it. This
is a capital-allocation theorem, not squeamishness.

**The opportunity-cost bar.** From the growth identity above, a dollar in the
short-horizon book compounds at rate g. Locking that dollar into a market
resolving in τ_L days is only correct if its expected edge beats the
short book's compounded return over the same lock-up:

```math
\mu_L \;>\; (1+g)^{\tau_L} - 1 \;\approx\; g\,\tau_L
```

At this run's measured (early, tiny-bankroll, won't-scale) growth of several
percent *per day*, a 184-day position — NonceChaser's actual "Burnham next
PM" trade — would need a triple-digit expected edge to justify the lock-up.
No screened trader's verified edge clears that bar. At a large bankroll where
g decays toward zero, the bar drops like g·τ_L and long-horizon copying
becomes rational; the config knob (`max days out`) is one number away.

**The subtlety that changes the math: effective holding time.** Because the
bot mirrors *sells* too, the true capital lock-up is the **target's holding
time**, not the market's time-to-resolution:

```math
\tau_{\text{eff}} \;=\; \min(\tau_{\text{target hold}},\ \tau_{\text{resolution}})
```

A trader who swing-trades a 6-month election market with 3-day holds is,
for copying purposes, a 3-day trader — the current end-date gate is a
conservative *proxy* that over-rejects exactly this case. The correct
long-horizon gate is per-trader median holding time; it isn't shipped yet
because estimating it robustly needs weeks of per-position entry/exit pairing
(and a target who *never* exits leaves you holding to resolution anyway —
τ_eff degrades to τ_resolution precisely when you least want it).

**Which long markets would qualify first, ranked by the math:**

1. **Catalyst-dated macro (Fed meetings, scheduled announcements)** — the
   market may list for months, but copying inside the final H days before the
   catalyst needs *no new machinery*: τ collapses to days and the existing
   gate already admits them naturally as the date approaches.
2. **Liquid politics majors (presidential/PM markets)** — continuous two-sided
   books mean a mirrored exit is always available, so τ_eff ≈ the target's
   holding time; enable only with the holding-time gate above plus a
   per-category exposure cap (long marks are noisy, and PnL/volume screens
   are most inflatable exactly here).
3. **Crypto strike/expiry markets (weekly/monthly)** — bounded τ_L of 7–30
   days, deep books; the bar g·τ_L is only a few multiples of the short-book
   edge, plausibly clearable by a specialist trader with verified strike-market
   history.
4. **Last: open-ended geopolitics ("X out by year-end")** — resolution-source
   risk, API-blind negRisk plumbing (the bot's on-chain payout oracle handles
   settlement, but *pricing* stays thin), and the worst τ profile. These are
   the Burnham/Putin trades the horizon cap exists to refuse.

Until the holding-time gate ships, the honest summary is: **the bot copies the
slice of each trader whose math it can verify, and skips the slice it can't.**

## After July 19 — the long-run math of copying proven winners

The World Cup regime dies on July 19 and most of this page's measurements die
with it. What does *not* die is the underlying question, so here is its math,
football-free: **if someone has been successfully trading for a long time —
for whatever reason — how much of their success can a copier actually
inherit?**

### Skill is inherited; luck is not

An observed track record is skill plus noise:

```math
\hat e \;=\; \theta + \varepsilon,
\qquad \mathrm{SE}(\hat e) = \frac{\sigma_{\text{trade}}}{\sqrt{T}} \approx \frac{0.87}{\sqrt{T}}
```

(σ_trade ≈ 0.87 per staked dollar is this page's measured per-trade
volatility; T is the number of settled markets in the record). Copying
transfers **θ only** — the luck term ε happened to *their* past, not your
future. The expected inheritance is the Bayesian shrinkage of the headline:

```math
\mathbb{E}\big[\theta \mid \hat e\,\big] \;=\; \lambda\,\hat e,
\qquad
\lambda \;=\; \frac{\sigma_\theta^2}{\sigma_\theta^2 + \sigma_{\text{trade}}^2/T}
```

where σ_θ is how much true skill varies across traders — a few percent per
staked dollar at most, since prediction markets are near-zero-sum and the
skilled are paid by biased recreational flow (the favorite–longshot bias),
not by magic. Take σ_θ = 3% as the conservative prior. Then a **+15%
headline edge** is worth, to a copier, as a function of record depth:

| record behind the headline | T (settled markets) | SE(ê) | λ | expected inherited edge | net after ~3% off-season friction |
|---|---|---|---|---|---|
| one hot week | ~150 | 7.1% | **0.15** | +2.3% | **−0.7% — noise, uncopyable** |
| 3 months | ~700 | 3.3% | 0.45 | +6.8% | +3.8% |
| 1 year | ~2,500 | 1.7% | 0.75 | +11.2% | +8.2% |
| 2 years | ~5,000 | 1.2% | **0.86** | +12.8% | **+9.8% — mostly real** |

(A more generous prior σ_θ = 5% lifts the week-long λ only to 0.33 — still
under half.) This one table is the entire long-term thesis: **a week-long
star transfers ~15% of what you see; a two-year veteran transfers ~86%.** It
is also, retroactively, the formal justification for this run's roster calls
— the 23-month account armed, the 4-week wonder rejected — and for why the
scout weighs record depth as heavily as the point estimate.

### Edges decay; screening cadence must beat the half-life

Skill is not a constant: informational edges erode as markets adapt (and as
copiers crowd them). With half-life h, an estimate that is Δt old is worth

```math
\mathbb{E}[\theta_{t+\Delta t}] \;=\; \lambda\,\hat e \cdot 2^{-\Delta t / h}
```

A monthly re-screen keeps 71% / 89% / 94% of the fresh edge for half-lives of
2 / 6 / 12 months — so even under fast decay, cheap periodic re-screening
(the in-bot scout is one click) preserves most of what shrinkage says is
real. The strategy's maintenance cost is a scan per month, not a rewrite.

### Why the edge should persist at all — and why it stays small

The efficient-markets objection ("if this works it gets arbitraged") answers
itself at this scale: the inheritable edge lives in **$1–7 clips** on books
too shallow to interest anyone with payroll. Crowding is self-limiting by
construction — if copiers mass onto one signal, the price runs and the
no-chase gate converts the crowd's losses into our *zero-fills* (measured:
that is exactly what dust-book ghosts are). The same capacity ceiling that
caps the profit protects the edge. The equilibrium is a persistent,
small-capital, high-variance niche — permanently below institutional
attention, permanently above zero in expectation *if and only if the record
depth math above is respected*.

### What a football-free year plausibly looks like

Off-season the copyable universe is tennis (year-round, same-day), esports
(daily), crypto candle/strike markets (24/7), baseball and US sports in
rotation, and — once the holding-time gate ships — the liquid politics
majors. Flow drops (≈3 settled copies/day vs 6.3 during the Cup) and thinner
books push friction toward ~3%. Running the same distributional machinery as
above at Kelly sizing:

| true inherited edge per copy | Kelly κ\* | median month | P(green month) |
|---|---|---|---|
| +2% (thin — shallow records passing the screen) | 2.7% | +2.4% | 53% |
| +5% (solid — the 3-month-record tier) | 6.7% | +16.1% | 58% |
| +10% (deep-record specialist, post-shrinkage) | 13.3% | +82.0% | 66% |

The honest long-term claim is therefore narrow: **copying is a real,
persistent, mathematically defensible edge — of pocket-money size, casino
variance, and a strict precondition: only ever inherit from records deep
enough that λ is close to 1.** Everything else on a leaderboard is renting
someone's luck.

## Features

- **Real-time copying** — WebSocket stream of platform trades (sub-second reaction), REST polling as reconciliation + fallback, per-trade dedupe across both paths
- **Risk gates on every copy** — no stacking (one position per market regardless of how many fills the target sprays), no chasing (skips if the price ran past the target's fill + slippage), horizon cap (skip markets resolving beyond N days), failed-buy cooldown
- **Auto-budget** — spend cap follows the wallet (total − reserve) and per-trade size scales with it, so the bot breathes with wins and losses without manual bumps
- **Chain-level reconciliation** — detects zero-filled FAK orders and auto-swept resolved positions by reading balances and the Conditional Tokens payout vector straight from Polygon, so the ledger stays true even when every Polymarket API is blind (negRisk markets)
- **Multi-trader with attribution** — every copy, skip and log line names which target it came from
- **Built-in trader scout** — one click re-runs the whole selection pipeline against the live leaderboard and ranks candidates by friction-adjusted net copy edge, with per-row copy buttons; results stream in as each trader is analyzed
- **Copy buttons + green tint** — missed trades (bot offline, restart baseline) appear in the live feed; rows that would genuinely copy glow green, and one click replays them through the exact same gates — per-row or all displayed at once ("copy all shown")
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

## Why it's safe to run — verified claims, not vibes

Every claim below is checkable by grepping the (single) source file, and the
sensitive ones are enforced by self-tests that the deploy script runs before
any release.

**The key can't leave your machine.**
- The private key is used for exactly one thing: locally signing CLOB order
  structs (EIP-712) inside the official `py-clob-client` flow. It is never
  transmitted, never logged, never included in the copilot's context.
- The web UI's key field is **write-only**: the served HTML never echoes the
  stored key (enforced by a self-test that fails the build if it ever does).
- Storage is a gitignored local file; this repo's **entire git history is
  scanned for the key value in all encodings** as part of the release
  checklist — it has never touched a commit.

**The bot can't move your funds anywhere.**
- Grep the file: there is no transfer, no withdrawal, no
  `eth_sendRawTransaction` — the only Polygon RPC calls are read-only
  `eth_call`s (balances and the Conditional Tokens payout vector). The
  worst-case blast radius of a bug is *bad trades within the budget caps*,
  not exfiltrated funds.

**Bounded egress.** The complete list of hosts the bot ever contacts:
`*.polymarket.com` (data, gamma, lb, user-pnl, CLOB REST + WebSocket) and two
public Polygon RPCs (`publicnode.com`, `polygon-rpc.com`) for read-only chain
queries. No telemetry, no analytics, no third parties.

**Bounded blast radius by construction.**
- HTTP UI binds to `127.0.0.1` only — nothing is exposed to the network
  (reach it remotely via Tailscale/SSH tunnel, never a public port)
- boots watch-only unless fully configured; approve mode queues every copy
  for a manual ✓
- buys capped per-trade and by the hard budget ratchet above; SELLs only what
  the bot itself bought, clamped to the actual on-chain balance
- the Claude copilot tunes settings through a whitelisted action protocol; it
  cannot place trades and its context snapshot excludes the key
- one subprocess exists in the whole file: the copilot's `claude -p` call —
  fixed argv, no shell

**And the boundary that actually matters:** run it on a dedicated wallet that
only ever holds your bankroll. Software guarantees end where key custody
begins — size the wallet so the worst case is a shrug.

## Disclaimer

This is a hobby project that trades real money badly or well depending on the
week. Prediction markets are gambling-adjacent. Past performance of a week-long
World-Cup-season sample predicts nothing. Run it with money you can lose.
