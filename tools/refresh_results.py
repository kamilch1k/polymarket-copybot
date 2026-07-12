#!/usr/bin/env python3
"""Regenerate the README's auto results block from the live ledger.

Usage:  python tools/refresh_results.py     (paths are repo-relative)

Reads copybot_state.json (the audited settlement history), your cash straight
from Polygon and open positions from the public data-api, then rewrites
everything between the <!-- results:auto --> markers in README.md. Read-only
against the world; the only thing it writes is the README. The win/loss
counters use the same note-matching the chain audit validated: "freed" = a
settled live win, "stays counted" = a settled live loss, "dry copy" = watch
mode, "never filled" = FAK zero-fill refund.
"""
import json
import re
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
START = 33.11  # audited starting bankroll (Jul 3, 2026) — the one hand-kept constant
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

st = json.loads((REPO / "copybot_state.json").read_text(encoding="utf-8"))
wins = dry = losses = refunds = 0
freed = 0.0
for e in st.get("history", []):
    n = e.get("note") or ""
    if "dry copy" in n:
        dry += 1
    elif "freed" in n:
        wins += 1
        m = re.search(r"\$([\d.]+)", n)
        freed += float(m.group(1)) if m else 0.0
    elif "stays counted" in n:
        losses += 1
    elif "never filled" in n:
        refunds += 1
banked = float(st.get("banked") or 0.0)

funder = json.loads((REPO / "copybot_config.json").read_text(encoding="utf-8"))["funder"]


def erc20(token):
    data = "0x70a08231" + funder[2:].lower().rjust(64, "0")
    for rpc in ("https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"):
        try:
            r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                         "params": [{"to": token, "data": data}, "latest"]},
                              timeout=8).json()
            if r.get("result"):
                return int(r["result"], 16) / 1e6
        except Exception:
            continue
    raise SystemExit(f"chain read failed for {token} — not refreshing with half-truths")


cash = erc20(PUSD) + erc20(USDC_E)
pos = requests.get("https://data-api.polymarket.com/positions",
                   params={"user": funder, "limit": 50}, timeout=15).json()
posval = sum(float(p.get("currentValue") or 0) for p in pos if isinstance(p, dict)
             and float(p.get("size") or 0) > 0)
total = cash + posval
settled = wins + losses

block = f"""<!-- results:auto -->
| Metric | Value (auto-refreshed from the live ledger by `tools/refresh_results.py`) |
|---|---|
| Balance at current marks ({time.strftime('%b %d, %Y %H:%M')}) | **${total:,.2f}** — ${cash:,.2f} cash + ${posval:,.2f} in open positions (**{(total - START) / START:+.0%} since the ${START} start**) |
| Cumulative P&L | **{total - START:+,.2f}** at CLOB/data-api marks (Polymarket's hourly accounting lags unresolved legs) |
| Settled copies | {settled} — **{wins} won / {losses} lost ({100 * wins / max(settled, 1):.0f}%)** — every settlement cross-checked against the on-chain payout vector |
| Capital returned by settled wins | ${freed:,.2f} |
| Profit banked (locked out of the budget, never re-bet) | ${banked:,.2f} |
| Copies that never filled (FAK zero-fills, $0 moved, auto-refunded) | {refunds} |
<!-- /results:auto -->"""

rd = REPO / "README.md"
txt = rd.read_text(encoding="utf-8")
new = re.sub(r"<!-- results:auto -->.*?<!-- /results:auto -->", block, txt, count=1, flags=re.S)
if new == txt and block not in txt:
    raise SystemExit("results:auto markers not found in README.md")
rd.write_text(new, encoding="utf-8")
print(block)
print("\nREADME updated" if new != txt else "\nREADME already current")
