#!/usr/bin/env python3
"""
Polymarket copy-trading bot + local dashboard app.

Run it, a browser opens http://127.0.0.1:8777 . Configure everything in the page
(⚙ Settings): target wallet, your funder wallet, private key, and sizing. Then
click Go LIVE. It shows what the bot holds / wants to buy, your target's live
activity + how to copy him best, and a leaderboard of traders to one-click copy.

Localhost-only on purpose: the page has a live-trade switch and a kill button.

Everything — targets, funder, sizing, AND the private key — persists to
copybot_config.json next to this file. That file is gitignored (never pushed);
it is plaintext on this PC, by the owner's choice. Configure once, runs forever.

SETUP
  pip install py-clob-client-v2 requests websocket-client
RUN
  python copybot.py --check     offline self-check
  python copybot.py             start app; browser opens; configure in the page
"""
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

# ---- config (defaults; editable live in the web UI) -------------------------
TARGETS = []                  # proxy wallets to copy — set in the page, several OK
BANKROLL = 30.0               # your total stake, for sizing hints only
COPY_FRACTION = 0.01          # copy 1% of his share size
MAX_USDC_PER_TRADE = 5.0      # per-copy notional cap
MIN_NOTIONAL = 1.0            # Polymarket rejects orders under ~$1
SLIPPAGE = 0.02              # accept up to this much worse than his fill
MIN_HIS_NOTIONAL = 0.0        # copy his BUY only if he put >= this many $ in (0 = no floor)
SPEND_CAP = 15.0             # hard stop: total live BUY $ the bot may ever spend (sells always allowed)
MAX_DAYS_OUT = 0.0           # only copy BUYs on markets ending within N days (0 = any horizon)
MAX_LEGS_PER_EVENT = 2       # max open BUY legs per match/event (0 = uncapped) — same-match
                             # legs are near-perfectly correlated: m legs act as ONE bet at m× size
AUTO_CAP_RESERVE = 8.0       # auto-budget: SPEND_CAP = wallet total − this reserve (0 = manual cap)
AUTO_TRADE_PCT = 10.0        # auto-size: MAX_USDC_PER_TRADE = this % of wallet, $5 floor (0 = manual)
MODE = "auto"                 # "auto" = copy instantly · "approve" = queue for your click
POLL_SECONDS = 15             # REST polling is the fallback; WebSocket is the fast path
LEADERS_EVERY = 20            # refresh leaderboard every N polls (~5 min)
SIGNATURE_TYPE = 3            # 1 = old email/magic · 2 = browser wallet · 3 = new Polymarket wallet (2026+). Auto-detected.
PORT = 8777
HEADLESS = False              # set by --headless: no window, systemd owns the lifecycle
PRIVATE_KEY_MEM = None        # persisted to config by owner's choice (single-user PC)
CONFIG_FILE = Path(__file__).with_name("copybot_config.json")
STATE_FILE = Path(__file__).with_name("copybot_state.json")
CHAT_FILE = Path(__file__).with_name("copybot_chat.jsonl")  # persisted Claude copilot log
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
WS_URL = "wss://ws-live-data.polymarket.com"  # real-time platform activity stream
CLAUDE_MODEL = "claude-opus-4-8"  # runs on the owner's Claude CLI login (Max plan)
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # legacy collateral (bridged USDC.e on Polygon)
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"    # pUSD — Polymarket collateral since the 2026-04-28 V2 upgrade
POLYGON_RPCS = ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"]
# -----------------------------------------------------------------------------

LOCK = threading.Lock()
STATE = {
    "live": False, "holdings": {}, "names": {}, "seen": set(), "baselined": set(),
    "log": deque(maxlen=200), "copies": 0, "started": time.time(), "last_poll": 0.0,
    "error": "", "tnames": {}, "target_feed": [], "leaders": [],
    "funder": "", "pk_set": False, "conn": None,  # conn: (ok?, message) after Test

    "pending": {}, "pid": 0,     # trades awaiting approval (approve mode)
    "history": [],               # every executed copy, persisted
    "ws": False,                 # realtime feed connected?
    "wallet": {},                # on-chain truth: usdc cash + open positions
    "chat": deque(maxlen=30),    # conversation with the in-bot Claude copilot
    "thinking": False,           # a Claude request is in flight
    "spent_live": 0.0,           # net live $ in play, enforced against SPEND_CAP
    "live_cost": {},             # token -> live $ spent on it (resolve/sell frees budget)
    "bought_at": {},             # token -> epoch of our buy (ghost-reconcile grace period)
    "missing_deps": [],          # runtime modules that failed to import at startup
    "who": {},                   # token -> trader we copied the open position from
}


def check_deps(log=True):
    """Surface any missing runtime module at startup as a loud banner, instead of
    letting it fail cryptically deep inside the Test-trade / auto-live path.
    Re-run quietly by bot_loop while modules are flagged missing: a transient
    import failure (pip mid-upgrade, locked site-packages) must not strand the
    bot in DRY forever after the environment heals — that cost 6 live hours once."""
    missing = []
    for mod, pip_name in (("requests", "requests"), ("websocket", "websocket-client"),
                          ("regex", "regex"), ("py_clob_client_v2", "py-clob-client-v2")):
        try:
            __import__(mod)
        except Exception:
            missing.append(pip_name)
    STATE["missing_deps"] = missing
    if missing and log:
        logline(kind="error", note="missing modules: " + ", ".join(missing)
                + " — run: pip install " + " ".join(missing) + " , then relaunch")
    return missing


# ---- pure logic (unit-tested in _check) -------------------------------------
def my_buy_size(his_size, price, fraction=None, cap=None):
    """Shares to buy to mirror his_size at `price`, floored at the $1 exchange
    minimum: every buy of his gets copied — same market beats same size.
    Defaults read the live globals so UI edits take effect immediately."""
    if price <= 0:
        return 0.0
    fraction = COPY_FRACTION if fraction is None else fraction
    cap = MAX_USDC_PER_TRADE if cap is None else cap
    notional = min(max(his_size * price * fraction, MIN_NOTIONAL), cap)
    return round(notional / price, 2)


def limit_price(ref_price, side, tick=0.01):
    """Marketable limit: cross the book by SLIPPAGE so the copy fills,
    rounded to the market's tick (aggressive direction) and clamped in-book."""
    if side == "BUY":
        px = min(1 - tick, ref_price * (1 + SLIPPAGE))
        px = math.ceil(px / tick - 1e-9) * tick
        return round(min(px, 1 - tick), 4)
    px = max(tick, ref_price * (1 - SLIPPAGE))
    px = math.floor(px / tick + 1e-9) * tick
    return round(max(px, tick), 4)


def key(trade):
    return f"{trade['transactionHash']}:{trade['asset']}:{trade['side']}"


def event_key(title):
    """Same-match legs share an 'X vs. Y' title prefix — that's the correlation
    unit the per-match cap counts. Titles without a versus-prefix (props,
    'Will X win…') each stay their own event; identical-market stacking is
    already blocked by the no-stacking gate."""
    head = str(title or "").split(":", 1)[0].strip().lower()
    return head if " vs" in head else str(title or "").strip().lower()


def copy_stats(feed):
    trades = [t for t in feed if t.get("side")]
    if not trades:
        return ["waiting for his activity…"]
    notionals = [float(t["size"]) * float(t["price"]) for t in trades]
    avg = sum(notionals) / len(notionals)
    buys = sum(1 for t in trades if t["side"].upper() == "BUY")
    per_copy = min(max(avg * COPY_FRACTION, MIN_NOTIONAL), MAX_USDC_PER_TRADE)
    lines = [
        f"his last {len(trades)} trades: {buys} buys / {len(trades) - buys} sells",
        f"his avg trade ≈ ${avg:,.0f}",
        f"you copy {COPY_FRACTION * 100:.1f}% (min ${MIN_NOTIONAL:.0f}, cap ${MAX_USDC_PER_TRADE:.0f}) → ≈ ${per_copy:.2f}/copy",
        f"budget ${SPEND_CAP:.0f} → room for ~{int(SPEND_CAP / max(per_copy, 0.01))} concurrent copies",
    ]
    if avg * COPY_FRACTION > MAX_USDC_PER_TRADE:
        lines.append("⚠ his size is big — your $ cap binds, so copies are flat, not proportional. "
                     "Raise the cap or drop the fraction to track him faithfully.")
    elif avg * COPY_FRACTION < MIN_NOTIONAL:
        lines.append(f"✓ his clips are small — every buy copies at the ${MIN_NOTIONAL:.0f} "
                     "exchange minimum (flat sizing, same markets)")
    else:
        lines.append("✓ cap rarely binds — copies scale with his size. Faithful tracking.")
    with LOCK:
        drifts = [e["drift"] for e in STATE["history"] if e.get("drift") is not None]
    if drifts:
        lines.append(f"copy lag cost: {sum(drifts) / len(drifts):+.1f}¢/share avg over {len(drifts)} copies "
                     f"(positive = market moved against you before your copy)")
    return lines


def ready():
    key_ = PRIVATE_KEY_MEM or os.environ.get("PM_PRIVATE_KEY")
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER")
    return bool(TARGETS and key_ and funder)


def parse_targets(text):
    """Comma/space-separated 0x addresses -> validated list."""
    out = []
    for a in text.replace(",", " ").split():
        if valid_addr(a) and a not in out:
            out.append(a)
    return out


def valid_addr(a):
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", a or ""))


def key_wallet(k):
    """Cryptographic proof the key is real: derive the address it signs as.
    Empty string = not a usable key."""
    try:
        from eth_account import Account
        return Account.from_key(k).address
    except Exception:
        return ""


# ---- persistence ------------------------------------------------------------
def load_config():
    global TARGETS, COPY_FRACTION, MAX_USDC_PER_TRADE, SLIPPAGE, BANKROLL, MODE, \
        PRIVATE_KEY_MEM, MIN_HIS_NOTIONAL, SIGNATURE_TYPE
    if not CONFIG_FILE.exists():
        return
    c = json.loads(CONFIG_FILE.read_text())
    TARGETS = c.get("targets") or ([c["target"]] if c.get("target") else [])
    if c.get("private_key"):
        PRIVATE_KEY_MEM = c["private_key"]
        STATE["pk_set"] = True
    MODE = c.get("mode", MODE)
    COPY_FRACTION = c.get("fraction", COPY_FRACTION)
    MAX_USDC_PER_TRADE = c.get("cap", MAX_USDC_PER_TRADE)
    SLIPPAGE = c.get("slippage", SLIPPAGE)
    BANKROLL = c.get("bankroll", BANKROLL)
    MIN_HIS_NOTIONAL = c.get("min_his", MIN_HIS_NOTIONAL)
    SIGNATURE_TYPE = c.get("sig_type", SIGNATURE_TYPE)
    globals()["SPEND_CAP"] = c.get("spend_cap", SPEND_CAP)
    globals()["MAX_DAYS_OUT"] = c.get("max_days_out", MAX_DAYS_OUT)
    globals()["MAX_LEGS_PER_EVENT"] = int(c.get("max_legs_per_event", MAX_LEGS_PER_EVENT))
    globals()["AUTO_CAP_RESERVE"] = c.get("auto_cap_reserve", AUTO_CAP_RESERVE)
    globals()["AUTO_TRADE_PCT"] = c.get("auto_trade_pct", AUTO_TRADE_PCT)
    STATE["funder"] = c.get("funder", "")


def save_config():
    CONFIG_FILE.write_text(json.dumps({  # includes the key: owner's choice, file is gitignored
        "targets": TARGETS, "funder": STATE.get("funder", ""), "fraction": COPY_FRACTION,
        "cap": MAX_USDC_PER_TRADE, "slippage": SLIPPAGE, "bankroll": BANKROLL,
        "mode": MODE, "min_his": MIN_HIS_NOTIONAL, "sig_type": SIGNATURE_TYPE,
        "spend_cap": SPEND_CAP, "max_days_out": MAX_DAYS_OUT,
        "max_legs_per_event": MAX_LEGS_PER_EVENT,
        "auto_cap_reserve": AUTO_CAP_RESERVE, "auto_trade_pct": AUTO_TRADE_PCT,
        "private_key": PRIVATE_KEY_MEM or ""}, indent=2))


def load_state():
    if not STATE_FILE.exists():
        return
    s = json.loads(STATE_FILE.read_text())
    STATE["seen"] = set(s.get("seen", []))
    STATE["holdings"] = s.get("holdings", {})
    STATE["names"] = s.get("names", {})
    STATE["history"] = s.get("history", [])
    STATE["spent_live"] = s.get("spent_live", 0.0)
    STATE["live_cost"] = s.get("live_cost", {})
    STATE["bought_at"] = s.get("bought_at", {})
    STATE["who"] = s.get("who", {})
    # backfill attribution for positions bought before the who-map existed:
    # BUY history entries have carried the trader name since day one
    bywho = {e.get("name"): e["who"] for e in STATE["history"]
             if e.get("side") == "BUY" and e.get("who")}
    for tid, nm in STATE["names"].items():
        if tid not in STATE["who"] and STATE["holdings"].get(tid, 0) > 0 and bywho.get(nm):
            STATE["who"][tid] = bywho[nm]
    if audit_ledger_0707():
        save_state()


def save_state():
    with LOCK:
        data = {"seen": sorted(STATE["seen"]), "holdings": STATE["holdings"],
                "names": STATE["names"], "history": STATE["history"][-500:],
                "spent_live": STATE["spent_live"], "live_cost": STATE["live_cost"],
                "bought_at": STATE["bought_at"], "who": STATE["who"]}
    STATE_FILE.write_text(json.dumps(data))


LEDGER_AUDIT_0707 = [  # chain-verified 2026-07-07: "never filled" ghosts that were real fills
    ("Canada vs. Morocco: O/U 2.5 — Under", 3.81, "lost"),
    ("Canada vs. Morocco: O/U 2.5 — Over", 5.12, "won"),
    ("Spread: France (-3.5) — Paraguay", 5.51, "won"),
    ("United States vs. Belgium: Team to Advance — Belgium", 6.55, "won"),
    ("Spread: Milwaukee Brewers (-1.5) — St. Louis Cardinals", 1.03, "won"),
    ("Argentina vs. Egypt: 1st Half O/U 0.5 — Over", 6.12, "won"),
    ("Argentina vs. Egypt: O/U 2.5 — Under", 3.04, "lost"),
    ("Argentina vs. Egypt: O/U 2.5 — Over", 1.02, "won"),
]


def audit_ledger_0707():
    """One-time repair for the ghost-detector bug fixed the same day: rewrite the
    eight chain-verified mislabels in place and re-count the two losses whose cost
    the bogus 'refund' had freed from the odometer. Idempotent — the 'never filled'
    marker is gone after the rewrite. Returns how many entries it fixed."""
    fixed = 0
    for e in STATE["history"]:
        if e.get("side") != "GHOST" or "never filled" not in str(e.get("note", "")):
            continue
        for name, cost, verdict in LEDGER_AUDIT_0707:
            if e.get("name") == name and f"${cost:.2f}" in str(e.get("note", "")):
                if verdict == "won":
                    e["kind"] = "live"
                    e["note"] = (f"resolved WON (auto-swept) — ${cost:.2f} freed to budget "
                                 "[audit: fill was real, payout verified on-chain]")
                else:
                    e["kind"] = "skip"
                    e["note"] = ("resolved LOST — stays counted against budget "
                                 "[audit: fill was real, payout verified on-chain]")
                    STATE["spent_live"] = round(STATE["spent_live"] + cost, 2)
                fixed += 1
    return fixed


def logline(hist=False, **e):
    e["t"] = time.strftime("%H:%M:%S")
    with LOCK:
        STATE["log"].appendleft(e)
        if hist:  # also record in the persistent trade history (survives restarts)
            STATE["history"].append({**e, "d": time.strftime("%Y-%m-%d")})


# ---- polymarket api ---------------------------------------------------------
def fetch_trades(user, limit=100, offset=0):
    r = requests.get(f"{DATA_API}/activity",
                     params={"user": user, "type": "TRADE", "limit": limit, "offset": offset}, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_trades_backfilled(user, seen, page=100, cap=600):
    """Newest fills, paged backward until we reach one we've already seen — so a
    burst bigger than a single page can't silently outrun the poll (the 100-limit
    gap that dropped 2 of a 485-trade LoL surge). Caps the walk so a brand-new
    target with no seen history doesn't page forever."""
    out, offset = [], 0
    while offset < cap:
        batch = fetch_trades(user, limit=page, offset=offset)
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if any(key(t) in seen for t in batch):  # reached known ground — nothing older is fresh
            break
        if len(batch) < page:
            break
        offset += page
    return out


def fetch_leaders():
    try:
        r = requests.get(f"{DATA_API}/v1/leaderboard", params={"category": "OVERALL"}, timeout=15)
        r.raise_for_status()
        d = r.json()
        rows = d if isinstance(d, list) else d.get("data") or d.get("leaderboard") or []
        return rows[:20]
    except Exception as ex:
        logline(kind="error", note=f"leaderboard: {ex}")
        return []


def midpoint(tid):
    """Current mid price, or None if unavailable (then we trust his fill price)."""
    try:
        r = requests.get(f"{CLOB_HOST}/midpoint", params={"token_id": tid}, timeout=5)
        return float(r.json()["mid"])
    except Exception:
        return None


TICKS = {}  # token_id -> tick size, cached


def tick_of(tid):
    if tid not in TICKS:
        try:
            r = requests.get(f"{CLOB_HOST}/tick-size", params={"token_id": tid}, timeout=5)
            TICKS[tid] = float(r.json()["minimum_tick_size"])
        except Exception:
            return 0.01  # sane default, don't cache failures
    return TICKS[tid]


def _erc20_balance(token, addr):
    data = "0x70a08231" + addr[2:].lower().rjust(64, "0")  # balanceOf(addr)
    for rpc in POLYGON_RPCS:
        try:
            r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                              "params": [{"to": token, "data": data}, "latest"]}, timeout=8).json()
            if r.get("result"):
                return int(r["result"], 16) / 1e6
        except Exception:
            continue
    return None


def onchain_usdc(addr):
    """Spendable cash straight from Polygon (no auth, authoritative): pUSD (the
    collateral since Polymarket's V2 upgrade) plus any legacy USDC.e. None on failure."""
    if not valid_addr(addr):
        return None
    pusd = _erc20_balance(PUSD, addr)
    usdce = _erc20_balance(USDC_E, addr)
    if pusd is None and usdce is None:
        return None
    return (pusd or 0.0) + (usdce or 0.0)


def market_state(tid):
    """'won'/'lost' for a closed market's token, 'open' if trading, None if unknown."""
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"clob_token_ids": tid}, timeout=10)
        m = (r.json() or [{}])[0]
        if m.get("closed") is False:
            return "open"
        toks = json.loads(m.get("clobTokenIds") or "[]")
        px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        if tid in toks and len(px) == len(toks):
            return "won" if px[toks.index(tid)] > 0.5 else "lost"
    except Exception:
        pass
    try:  # negRisk sports tokens are invisible to gamma — the CLOB price tape isn't:
        # a tape that stopped hours ago pinned at 0/1 is a resolved market
        r = requests.get(f"{CLOB_HOST}/prices-history",
                         params={"market": tid, "interval": "1w", "fidelity": 60}, timeout=10)
        h = (r.json() or {}).get("history") or []
        if h and time.time() - h[-1].get("t", 0) > 7200:
            p = float(h[-1].get("p", 0.5))
            if p > 0.98:
                return "won"
            if p < 0.02:
                return "lost"
    except Exception:
        pass
    return _ctf_payout(tid)  # last resort: on-chain payout record (API-blind proof)


CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens (Polygon)
COND_CACHE = {}  # token -> (conditionId, outcomeIndex), learned from our own fills


def _cond_of(tid):
    """conditionId + outcomeIndex for a token we traded, from our own fill records."""
    if tid in COND_CACHE:
        return COND_CACHE[tid]
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER", "")
    try:
        for off in (0, 500):
            rows = requests.get(f"{DATA_API}/activity",
                                params={"user": funder, "limit": 500, "offset": off}, timeout=10).json()
            if not isinstance(rows, list) or not rows:
                break
            for a in rows:
                t2, cid = str(a.get("asset", "")), a.get("conditionId")
                if t2 and cid and a.get("outcomeIndex") is not None and t2 not in COND_CACHE:
                    COND_CACHE[t2] = (cid, int(a["outcomeIndex"]))
            if len(rows) < 500:
                break
    except Exception:
        pass
    return COND_CACHE.setdefault(tid, (None, None))


def _ctf_payout(tid):
    """'won'/'lost' straight from the CTF contract's payout vector, or None while
    unresolved. Works when gamma, the positions API and the price tape are all
    blind (negRisk sweeps) — the chain is the settlement-grade source."""
    cid, idx = _cond_of(tid)
    if not cid or idx is None:
        return None
    try:
        from eth_utils import keccak
    except ImportError:
        return None

    def call(data):
        for rpc in POLYGON_RPCS:
            try:
                r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                             "params": [{"to": CTF, "data": data}, "latest"]},
                                  timeout=10)
                res = r.json().get("result")
                if res and res != "0x":
                    return int(res, 16)
            except Exception:
                continue
        return None

    cid32 = cid[2:].rjust(64, "0")
    den = call("0x" + keccak(text="payoutDenominator(bytes32)")[:4].hex() + cid32)
    if not den:
        return None  # unresolved (or RPC unreachable): no verdict, try again later
    num = call("0x" + keccak(text="payoutNumerators(bytes32,uint256)")[:4].hex()
               + cid32 + hex(idx)[2:].rjust(64, "0"))
    if num is None:
        return None
    return "won" if num > 0 else "lost"


RECON_NEXT = {}  # token -> don't re-check before this time (API-lag backoff)


def reconcile_ghosts():
    """Holdings the positions API no longer shows. Two causes: zero-fill FAKs
    (bought nothing, refund the budget) and auto-swept resolved markets (credit
    wins). Chain balance is the arbiter; recent buys are left alone (cache lag)."""
    now = time.time()
    with LOCK:
        held = {t: s for t, s in STATE["holdings"].items() if s > 0}
        visible = {p.get("asset") for p in STATE["wallet"].get("positions", [])
                   if not p.get("_synth")}  # synth rows are ours — they prove nothing
    for tid, sh in held.items():
        if tid in visible or now < RECON_NEXT.get(tid, 0):
            continue
        if now - STATE["bought_at"].get(tid, 0) < 900:
            continue  # too fresh: positions API and CLOB cache may simply lag
        outcome = market_state(tid)
        if outcome is None:
            RECON_NEXT[tid] = now + 600
            continue
        if outcome == "open":
            # market still trading: only a zero on-chain balance proves a zero-fill
            try:
                cl = get_client()
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                bp = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid,
                                            signature_type=SIGNATURE_TYPE)
                cl.update_balance_allowance(bp)
                bal = int(float(cl.get_balance_allowance(bp).get("balance", 0)) / 1e4) / 100.0
            except Exception:
                continue  # can't verify this cycle
            if bal >= 0.01:
                RECON_NEXT[tid] = now + 600  # healthy position; positions API is behind
                continue
            # zero balance in an "open" book proves nothing by itself: gamma and the
            # CLOB keep reporting a resolved-and-swept market as trading for a while.
            # The chain's payout vector separates a zero-fill from a swept resolution,
            # and any fill of ours on record means it was never a ghost.
            chain = _ctf_payout(tid)
            if chain in ("won", "lost"):
                outcome = chain
            elif _cond_of(tid)[0]:
                RECON_NEXT[tid] = now + 900  # we filled; payout just not reported yet
                continue
        name = STATE["names"].get(tid, tid[:16])
        credited = 0.0
        with LOCK:
            was_live = STATE["live_cost"].pop(tid, None)
            STATE["holdings"].pop(tid, None)
            STATE["bought_at"].pop(tid, None)
            if was_live is not None and outcome == "won":
                # free this copy's stake (same cost-based model as settle_resolved);
                # the winnings land in the wallet, never in the in-play figure
                before = STATE["spent_live"]
                STATE["spent_live"] = round(max(0.0, before - was_live), 2)
                credited = round(before - STATE["spent_live"], 2)
            elif was_live is not None and outcome == "open":
                STATE["spent_live"] = round(max(0.0, STATE["spent_live"] - was_live), 2)
        note = (f"resolved WON (auto-swept) — ${credited:,.2f} freed to budget" if outcome == "won" else
                f"buy never filled on-chain — ${was_live or 0:,.2f} refunded to budget" if outcome == "open" else
                "resolved LOST — stays counted against budget")
        logline(hist=True, kind="live" if outcome == "won" else "skip",
                side="GHOST", name=name, shares=sh, note=note)
        save_state()


def reconcile_odometer():
    """Keep the in-play odometer honest against the stakes actually at risk.

    FLOOR: spent_live never reads below the sum of open live stakes (heals stale
    under-counts). CEILING (only when no buy is mid-placement): spent_live is
    exactly the open stakes — any excess is settled-loss cruft. That excess used
    to 'stay counted' as a drawdown throttle, but the wallet-following cap
    (SPEND_CAP = wallet − reserve) already throttles on drawdown; counting it
    twice let the odometer ratchet above the cap with nothing open, freezing the
    bot with no way down (no open position left to free cost). Releasing to the
    real money-at-risk fixes that and self-heals every time the book goes flat.
    Guarded on no in-flight/pending buy so it can never race a placement into a
    double-spend. Returns the amount released (for the log)."""
    released = 0.0
    with LOCK:
        floor = round(sum(STATE["live_cost"].values()), 2)
        if STATE["spent_live"] < floor:
            STATE["spent_live"] = floor
        elif STATE["spent_live"] > floor and not INFLIGHT_BUYS and not STATE["pending"]:
            released = round(STATE["spent_live"] - floor, 2)
            STATE["spent_live"] = floor
    if released >= 0.01:
        logline(kind="skip", note=f"budget reconcile: released ${released:.2f} of settled-loss cruft; "
                                  f"money at risk is ${floor:.2f} (the cap already tracks the wallet)")
    return released


def auto_cap():
    """Wallet-driven sizing: SPEND_CAP = total − reserve, MAX_USDC_PER_TRADE = %
    of total ($5 floor). Budgets then breathe with wins/losses, no manual bumps."""
    if AUTO_CAP_RESERVE <= 0 and AUTO_TRADE_PCT <= 0:
        return
    with LOCK:
        w = dict(STATE["wallet"])
    cash = w.get("usdc")
    if cash is None:
        return  # chain read failed this cycle; keep the last values, don't jerk around
    total = cash + sum(float(p.get("currentValue") or 0) for p in w.get("positions", []))
    if AUTO_CAP_RESERVE > 0:
        new = max(0.0, round(total - AUTO_CAP_RESERVE, 2))
        if abs(new - SPEND_CAP) >= 1.0:
            logline(kind="skip", note=f"auto-budget: cap ${SPEND_CAP:.2f} → ${new:.2f} "
                                      f"(wallet ${total:.2f} − ${AUTO_CAP_RESERVE:g} reserve)")
        globals()["SPEND_CAP"] = new
    if AUTO_TRADE_PCT > 0:
        newc = max(5.0, round(total * AUTO_TRADE_PCT / 100, 2))
        if abs(newc - MAX_USDC_PER_TRADE) >= 0.5:
            logline(kind="skip", note=f"auto-size: per-trade cap ${MAX_USDC_PER_TRADE:.2f} → ${newc:.2f} "
                                      f"({AUTO_TRADE_PCT:g}% of ${total:.2f} wallet)")
        globals()["MAX_USDC_PER_TRADE"] = newc


def settle_resolved():
    """When a copied position's market resolves, free its budget: a win frees the
    stake it consumed (the profit lands in the wallet, not the odometer), so it is
    no longer 'in play'. Losses stay counted (still money in the hole). DRY copies
    just get cleared, never credited."""
    with LOCK:
        held = {t: s for t, s in STATE["holdings"].items() if s > 0}
        posmap = {p.get("asset"): p for p in STATE["wallet"].get("positions", [])}
    for tid, sh in held.items():
        p = posmap.get(tid)
        if not p or not p.get("redeemable"):
            continue
        won = float(p.get("curPrice") or 0) > 0.5
        name = STATE["names"].get(tid, tid[:16])
        with LOCK:
            cost = STATE["live_cost"].pop(tid, None)  # what this copy actually cost us
            was_live = cost is not None
            STATE["holdings"].pop(tid, None)
            back = round(cost, 2) if (won and was_live) else 0.0
            if back:  # free the stake; the winnings sit in the wallet, not the in-play figure
                STATE["spent_live"] = round(max(0.0, STATE["spent_live"] - back), 2)
        note = (f"resolved WON — ${back:,.2f} stake freed back into budget" if won and was_live else
                "resolved WON (dry copy — nothing was spent)" if won else
                "resolved LOST — stays counted against budget")
        logline(hist=True, kind="live" if won else "skip", side="RESOLVE", name=name, shares=sh, note=note)
        save_state()


def _augment_positions(w):
    """positions-API blind spots (negRisk sweeps): bot-held tokens the API omits
    still show in the wallet card and count toward the auto-budget total."""
    have = {p.get("asset") for p in w.get("positions", [])}
    with LOCK:
        mine = {t: s for t, s in STATE["holdings"].items() if s > 0 and t not in have}
        nm = dict(STATE["names"])
    for t, s in mine.items():
        px = midpoint(t)
        full = nm.get(t, t[:14])
        ti, _, oc = full.rpartition(" — ")
        w.setdefault("positions", []).append({
            "asset": t, "title": ti or full, "outcome": oc if ti else "",
            "size": s, "curPrice": px, "currentValue": round((px or 0) * s, 2),
            "redeemable": False, "_synth": True})


def fetch_wallet():
    """On-chain truth for YOUR funder wallet: cash (direct Polygon query) and open
    positions (public data-api). Shows the exact address checked so a wrong-wallet
    setup is visible instead of a silent $0."""
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER")
    if not funder:
        return
    w = {"checked": funder, "usdc": onchain_usdc(funder)}
    try:
        pos = requests.get(f"{DATA_API}/positions", params={"user": funder, "limit": 50},
                           timeout=15).json()
        if isinstance(pos, list):
            live = sorted((p for p in pos if float(p.get("size") or 0) > 0),
                          key=lambda p: -float(p.get("currentValue") or 0))
            w["positions"] = live[:40]
    except Exception:
        pass
    _augment_positions(w)
    try:  # CLOB "tradeable" balance, if connected — may differ from on-chain (open orders)
        if CLIENT:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            b = CLIENT.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            if isinstance(b, dict) and b.get("balance") is not None:
                w["tradeable"] = float(b["balance"]) / 1e6
    except Exception:
        pass
    with LOCK:
        STATE["wallet"].update(w)


def fetch_name(addr):
    try:
        r = requests.get(f"{GAMMA_API}/public-profile", params={"address": addr}, timeout=10)
        if r.ok:
            j = r.json()
            return j.get("name") or j.get("pseudonym") or ""
    except Exception:
        pass
    return ""


def make_client():
    # v2 client: the exchange rejected v1's order signing ("invalid order version")
    global SIGNATURE_TYPE
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
    k = PRIVATE_KEY_MEM or os.environ.get("PM_PRIVATE_KEY")
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER")
    if not k or not funder:
        raise RuntimeError("set private key and funder in Settings")

    def build(sig):
        cl = ClobClient(CLOB_HOST, key=k, chain_id=137, signature_type=sig, funder=funder)
        cl.set_api_creds(cl.create_or_derive_api_key())
        return cl

    def clob_balance(cl):
        try:
            b = cl.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(b.get("balance", 0)) / 1e6 if isinstance(b, dict) else 0.0
        except Exception:
            return 0.0

    cl = build(SIGNATURE_TYPE)
    if clob_balance(cl) <= 0:
        # the exchange keeps a separate ledger per signature type — find the funded one
        for sig in (3, 2, 1, 0):
            if sig == SIGNATURE_TYPE:
                continue
            try:
                alt = build(sig)
            except Exception:
                continue
            if clob_balance(alt) > 0:
                SIGNATURE_TYPE = sig
                save_config()
                logline(kind="skip", note=f"auto-detected wallet signature type {sig} (exchange holds balance there)")
                return alt
    return cl


# ---- trading ----------------------------------------------------------------
CLIENT = None


def get_client():
    global CLIENT
    if CLIENT is None:
        CLIENT = make_client()
    return CLIENT


def over_budget(notional):
    """True when a live BUY of `notional` $ would break the hard spend cap."""
    with LOCK:
        return STATE["spent_live"] + notional > SPEND_CAP + 1e-9


def _order(tid, side, shares, ref, name, drift=None, who=""):
    price = limit_price(ref, side, tick_of(tid))
    with LOCK:
        live = STATE["live"]
        STATE["copies"] += 1
    if live and side == "BUY" and over_budget(price * shares):
        logline(kind="skip", side="BUY", name=name,
                note=f"HARD STOP: ${STATE['spent_live']:.2f} of ${SPEND_CAP:.2f} budget spent")
        return False
    kind, note = "dry", ""
    if live:
        from py_clob_client_v2.clob_types import (OrderArgs, MarketOrderArgs, OrderType,
                                                  BalanceAllowanceParams, AssetType)
        try:
            cl = get_client()
            if side == "BUY":
                # market buy sized by a whole-cent USDC amount — the exchange rejects
                # buy maker amounts with >2 decimals (price*size gives too many)
                amt = max(1.0, round(price * shares, 2))
                signed = cl.create_market_order(MarketOrderArgs(
                    token_id=tid, amount=amt, side="BUY", price=price, order_type=OrderType.FAK))
            else:
                # selling an outcome token needs the CLOB's cached token balance synced
                # with the chain first, else it reads 0 and rejects ("not enough balance")
                try:
                    bp = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL,
                                                token_id=tid, signature_type=SIGNATURE_TYPE)
                    cl.update_balance_allowance(bp)
                    # $-sized market buys fill fractional shares (6.666665, not our rounded
                    # 6.67) — sell what the chain actually holds, floored, or the CLOB 400s
                    actual = int(float(cl.get_balance_allowance(bp).get("balance", 0)) / 1e4) / 100.0
                    if 0 < actual < shares:
                        shares = actual
                except Exception:
                    pass
                # FAK marketable limit: fill now or die, never rest at a stale price
                signed = cl.create_order(OrderArgs(token_id=tid, price=price, size=shares, side="SELL"))
            note = str(cl.post_order(signed, OrderType.FAK))[:80]
            kind = "live"
            with LOCK:  # cap tracks net $ in play: buys add, sell proceeds refund
                delta = price * shares if side == "BUY" else -price * shares
                STATE["spent_live"] = round(max(0.0, STATE["spent_live"] + delta), 2)
                if side == "BUY":  # remember which holdings cost live money (for resolve credit)
                    STATE["live_cost"][tid] = round(STATE["live_cost"].get(tid, 0.0) + price * shares, 2)
                else:
                    STATE["live_cost"].pop(tid, None)  # we always exit the whole position
        except Exception as ex:
            if "no orders found" in str(ex):  # FAK into a torn-down book: benign, no $ moved
                kind, note = "skip", "book empty — FAK found nothing to match (market closing?)"
            else:
                kind, note = "error", str(ex)[:140]
            if side == "BUY":
                FAILED_BUY_AT[tid] = time.time()  # don't hammer a market that just rejected us
    e = {"d": time.strftime("%Y-%m-%d"), "t": time.strftime("%H:%M:%S"), "kind": kind,
         "side": side, "name": name, "shares": shares, "price": price, "note": note,
         "drift": drift, "who": who}
    with LOCK:
        STATE["log"].appendleft(e)
        STATE["history"].append(e)
        if side == "BUY" and who and kind in ("live", "dry"):
            STATE["who"][tid] = who  # remember whose trade this position mirrors
    return kind in ("live", "dry")  # only an actually-placed order updates holdings


def execute(it):
    """Place a copy intent. SELL re-clamps to what we still hold (an approval
    may sit in the queue while other sells drain the position)."""
    tid, side, shares = it["tid"], it["side"], it["shares"]
    if side == "SELL":
        with LOCK:
            held = STATE["holdings"].get(tid, 0.0)
        shares = min(shares, held)
        if shares <= 0:
            logline(kind="skip", side="SELL", name=it["name"], note="nothing left to sell")
            return
    if _order(tid, side, shares, it["ref"], it["name"], it.get("drift"), it.get("who", "")):
        with LOCK:
            delta = shares if side == "BUY" else -shares
            STATE["holdings"][tid] = round(STATE["holdings"].get(tid, 0.0) + delta, 2)
            if side == "BUY":
                STATE["bought_at"][tid] = time.time()
    save_state()


def submit(it):
    """Route an intent: execute now (auto) or queue for your click (approve)."""
    if MODE == "approve":
        with LOCK:
            STATE["pid"] += 1
            it["id"] = str(STATE["pid"])
            it["t"] = time.strftime("%H:%M:%S")
            STATE["pending"][it["id"]] = it
        logline(kind="pend", side=it["side"], name=it["name"], shares=it["shares"], who=it.get("who", ""),
                price=limit_price(it["ref"], it["side"]), note="awaiting approval")
    else:
        execute(it)


ENDS_CACHE = {}
INFLIGHT_BUYS = set()  # tokens whose first copy is mid-placement (race guard, in-memory)
FAILED_BUY_AT = {}     # token -> when its last buy failed (cooldown against retry-hammering)
POLL_FAILS = {}        # target -> consecutive poll failures (transient resets stay silent)


def market_end_ts(tid):
    """Epoch when this token's market ends (gamma lookup, cached). None = unknown."""
    if tid in ENDS_CACHE:
        return ENDS_CACHE[tid]
    ts = None
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"clob_token_ids": tid}, timeout=10)
        e = ((r.json() or [{}])[0].get("endDate") or "").replace("Z", "+00:00")
        if e:
            from datetime import datetime
            ts = datetime.fromisoformat(e).timestamp()
    except Exception:
        pass  # unknown horizon copies through: missing data must not silence the bot
    ENDS_CACHE[tid] = ts
    return ts


def handle(trade):
    tid = trade["asset"]
    side = trade["side"].upper()
    price = float(trade["price"])
    his = float(trade["size"])
    name = f"{trade.get('title', '?')} — {trade.get('outcome', '?')}"
    pw = str(trade.get("proxyWallet", "")).lower()
    who = trade.get("_who") or STATE["tnames"].get(pw) or (pw[:8] + "…" if pw else "")
    with LOCK:
        STATE["names"][tid] = name
    if side == "BUY":
        # one copy per market: his 82-fill burst is ONE order. INFLIGHT closes the
        # ws-thread/poll-thread race while the first copy's order is still placing.
        with LOCK:
            dup = (STATE["holdings"].get(tid, 0) > 0 or tid in INFLIGHT_BUYS or any(
                p.get("tid") == tid and p.get("side") == "BUY" for p in STATE["pending"].values()))
            if not dup:
                INFLIGHT_BUYS.add(tid)
        if dup:
            logline(kind="skip", side="BUY", name=name, who=who,
                    note="already holding/queued this market — not stacking copies")
            return
        if time.time() - FAILED_BUY_AT.get(tid, 0) < 90:
            logline(kind="skip", side="BUY", name=name, who=who,
                    note="cooling down — this market just rejected a buy")
            return
    try:
        if side == "BUY":
            if his * price < MIN_HIS_NOTIONAL:
                logline(kind="skip", side="BUY", name=name, who=who,
                        note=f"his ${his * price:,.0f} < ${MIN_HIS_NOTIONAL:,.0f} conviction floor")
                return
            if MAX_DAYS_OUT > 0:
                ets = market_end_ts(tid)
                if ets and (ets - time.time()) > MAX_DAYS_OUT * 86400:
                    logline(kind="skip", side="BUY", name=name, who=who,
                            note=f"resolves in {(ets - time.time()) / 86400:.0f}d — beyond your {MAX_DAYS_OUT:g}d horizon")
                    return
            if MAX_LEGS_PER_EVENT > 0:
                ev = event_key(name.rpartition(" — ")[0])
                with LOCK:
                    legs = sum(1 for t2, s2 in STATE["holdings"].items() if s2 > 0 and t2 != tid
                               and event_key(STATE["names"].get(t2, "").rpartition(" — ")[0]) == ev)
                    legs += sum(1 for p in STATE["pending"].values() if p.get("side") == "BUY"
                                and event_key(str(p.get("name", "")).rpartition(" — ")[0]) == ev)
                if legs >= MAX_LEGS_PER_EVENT:
                    logline(kind="skip", side="BUY", name=name, who=who,
                            note=f"per-match cap: {legs} leg(s) already open on this event — "
                                 f"correlated legs act as one bet at {legs + 1}× size")
                    return
            shares = my_buy_size(his, price)
            if shares <= 0:
                logline(kind="skip", side="BUY", name=name, who=who, note="unpriced trade")
                return
        else:
            with LOCK:
                held = STATE["holdings"].get(tid, 0.0)
            shares = held  # he's exiting — exit our whole copy (we size by $, not by his %)
            if shares <= 0:
                logline(kind="skip", side="SELL", name=name, who=who, note="nothing copied")
                return
            if shares * price < MIN_NOTIONAL:
                logline(kind="skip", side="SELL", name=name, who=who,
                        note="position under the $1 sell minimum — rides to resolution")
                return

        # pre-flight: check where the market is NOW, not where it was when he traded
        mid = midpoint(tid)
        drift = None
        if mid:
            drift = round(((mid - price) if side == "BUY" else (price - mid)) * 100, 2)
            if side == "BUY" and mid > price * (1 + SLIPPAGE):
                logline(kind="skip", side="BUY", name=name, who=who,
                        note=f"won't chase: mid {mid:.3f} already ran past his {price:.3f}+slippage")
                return
            ref = min(price, mid) if side == "BUY" else mid  # never pay above market / always exit at market
        else:
            ref = price
        submit({"tid": tid, "side": side, "shares": shares, "ref": ref, "name": name,
                "drift": drift, "who": who})
    finally:
        if side == "BUY":  # holdings/pending now reflect the copy; marker no longer needed
            INFLIGHT_BUYS.discard(tid)


def copy_missed(k):
    """Feed-row 'copy' button: the owner replays one missed/baselined BUY through
    the NORMAL pipeline — every gate still applies (no stacking, cooldown, horizon,
    no-chase vs current mid, auto-sizing). Fires only on the owner's click."""
    with LOCK:
        t = next((x for x in STATE["target_feed"] if key(x) == k), None)
    if t and str(t.get("side", "")).upper() == "BUY":
        handle(dict(t))
    elif not t:
        # never fail silently: fast feeds (6 targets) push rows out between render and click
        logline(kind="skip", side="BUY", note="copy: that row already scrolled out of the feed — nothing replayed")


def copy_all_missed():
    """'Copy all shown' button: replay every displayed, un-held feed BUY through
    the NORMAL pipeline, one at a time — each copy still passes every gate
    (no stacking, no chasing, horizon, budget). Fires only on the owner's click."""
    with LOCK:
        feed = list(STATE["target_feed"])[:15]
        taken = {tid for tid, sh in STATE["holdings"].items() if sh > 0}
        taken |= {p.get("tid") for p in STATE["pending"].values()}
        taken |= set(INFLIGHT_BUYS)
    todo, seen = [], set()
    for t in feed:
        a = t.get("asset")
        if str(t.get("side", "")).upper() == "BUY" and a not in taken and a not in seen:
            seen.add(a)
            todo.append(t)
    logline(kind="skip", note=f"copy-all: replaying {len(todo)} shown BUY row(s) through the gates")
    for t in todo:
        handle(dict(t))


def sell_position(tid):
    """Market-sell one on-chain position (the wallet-card sell button). Sells the
    actual held balance, so it also liquidates strays like old test-trade legs."""
    with LOCK:
        pos = next((p for p in STATE["wallet"].get("positions", []) if p.get("asset") == tid), {})
    name = f'{pos.get("title", "position")} — {pos.get("outcome", "?")}'
    if not STATE["live"]:
        logline(kind="skip", side="SELL", name=name, note="DRY mode — flip to LIVE to sell for real")
        return
    try:
        cl = get_client()
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        p = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid,
                                   signature_type=SIGNATURE_TYPE)
        cl.update_balance_allowance(p)  # sync CLOB's cached balance with the chain
        held = int(float(cl.get_balance_allowance(p).get("balance", 0)) / 1e4) / 100.0
        if held <= 0:
            logline(kind="error", side="SELL", name=name, note="chain reports 0 balance for this token")
            return
        ref = midpoint(tid) or float(pos.get("curPrice") or 0)
        if not ref:
            logline(kind="error", side="SELL", name=name, note="no market price available")
            return
        if _order(tid, "SELL", held, ref, name):
            with LOCK:
                STATE["holdings"].pop(tid, None)
            save_state()
    except Exception as ex:
        logline(kind="error", side="SELL", name=name, note=str(ex)[:140])


def pick_test_market():
    """An active, liquid market whose token the CLOB verifiably accepts right now.
    (The target's own feed is unusable here: fast sports markets resolve within
    hours and their token ids go invalid — the cause of the 'invalid token id' 400.)"""
    r = requests.get(f"{GAMMA_API}/markets",
                     params={"active": "true", "closed": "false", "order": "volume24hr",
                             "ascending": "false", "limit": 12}, timeout=15)
    for m in r.json():
        if m.get("enableOrderBook") is False or m.get("acceptingOrders") is False:
            continue
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except ValueError:
            continue
        for tid in toks[:1]:
            mid = midpoint(tid)
            if mid and 0.05 <= mid <= 0.95:  # CLOB knows the token AND it's not near-resolved
                return tid, mid, (m.get("question") or m.get("slug") or "?")
    raise RuntimeError("no active liquid market found")


def test_trade():
    """Real-money end-to-end proof: buy ~$1 on a live liquid market, then
    immediately sell it back. Costs a few cents of spread.
    ponytail: $0.10 isn't possible — Polymarket rejects orders under $1 notional."""
    try:
        cl = get_client()
        tid, ref, title = pick_test_market()
        name = f"TEST · {title}"
        from py_clob_client_v2.clob_types import (OrderArgs, MarketOrderArgs, OrderType,
                                                  BalanceAllowanceParams, AssetType)
        tick = tick_of(tid)
        buy_px = limit_price(ref, "BUY", tick)
        r = cl.post_order(cl.create_market_order(MarketOrderArgs(
            token_id=tid, amount=1.00, side="BUY", price=buy_px, order_type=OrderType.FAK)), OrderType.FAK)
        logline(hist=True, kind="live", side="BUY", name=name, shares=round(1.0 / buy_px, 2), price=buy_px, note=str(r)[:70])
        time.sleep(3)  # let the buy settle
        # sync the CLOB's view of our token balance, then sell exactly what we actually hold
        # (also liquidates any leftover position from earlier failed test round-trips)
        p = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid, signature_type=SIGNATURE_TYPE)
        cl.update_balance_allowance(p)
        held = int(float(cl.get_balance_allowance(p).get("balance", 0)) / 1e4) / 100.0  # floor to cents
        if held < 0.01:
            logline(kind="error", note="test: buy left no sellable balance (fill may be pending)")
            return
        sell_px = limit_price(ref, "SELL", tick)
        r = cl.post_order(cl.create_order(OrderArgs(token_id=tid, price=sell_px, size=held, side="SELL")),
                          OrderType.FAK)
        logline(hist=True, kind="live", side="SELL", name=name, shares=held, price=sell_px, note=str(r)[:70])
        save_state()
        with LOCK:
            STATE["conn"] = (True, "✓ test trade round-trip sent — see activity log for both fills")
    except Exception as ex:
        with LOCK:
            STATE["conn"] = (False, f"test trade failed: {str(ex)[:140]}")
        logline(kind="error", note=f"test trade: {str(ex)[:140]}")


def try_go_live(source):
    """Auto-enable live trading when fully configured (owner wants hands-off)."""
    if STATE["live"] or not ready():
        return
    try:
        get_client()
        with LOCK:
            STATE["live"] = True
            if not STATE["conn"]:
                STATE["conn"] = (True, "connected — creds verified at auto-live")
        logline(kind="live", note=f"auto-live: trading enabled ({source})")
    except Exception as ex:
        logline(kind="error", note=f"auto-live failed ({source}): {str(ex)[:120]}")


# ---- claude copilot ----------------------------------------------------------
def bot_context():
    """Compact live snapshot handed to Claude so it can explain what's going on."""
    with LOCK:
        holdings = [{"market": STATE["names"].get(k, k[:14]), "shares": v}
                    for k, v in STATE["holdings"].items() if v > 0]
        log = [{k: e.get(k) for k in ("t", "kind", "side", "name", "shares", "price", "note")}
               for e in list(STATE["log"])[:12]]
        drifts = [e["drift"] for e in STATE["history"] if e.get("drift") is not None]
        w = STATE["wallet"]
        ctx = {
            "live_trading": STATE["live"], "mode": MODE, "ws_realtime": STATE["ws"],
            "targets": [{"addr": a, "name": STATE["tnames"].get(a, "?")} for a in TARGETS],
            "settings": {"copy_fraction": COPY_FRACTION, "max_usd_per_trade": MAX_USDC_PER_TRADE,
                         "min_his_buy_usd": MIN_HIS_NOTIONAL, "slippage": SLIPPAGE,
                         "bankroll_usd": BANKROLL},
            "bot_holdings": holdings,
            "pending_approvals": len(STATE["pending"]),
            "wallet_usdc_cash": w.get("usdc"),
            "wallet_positions_value": round(sum(float(p.get("currentValue") or 0)
                                                for p in w.get("positions", [])), 2),
            "copies_made": STATE["copies"],
            "avg_copy_lag_cost_cents": round(sum(drifts) / len(drifts), 2) if drifts else None,
            "recent_activity_log": log,
        }
    return ctx


ALLOWED_OPS = ("live", "mode", "fraction", "cap", "min_his", "slippage",
               "bankroll", "add_target", "drop_target")


def parse_actions(text):
    """Split Claude's reply from its trailing 'ACTIONS: [...]' control line."""
    acts, keep = [], []
    for line in text.splitlines():
        if line.strip().startswith("ACTIONS:"):
            try:
                acts = json.loads(line.strip()[len("ACTIONS:"):].strip())
            except ValueError:
                pass
        else:
            keep.append(line)
    return "\n".join(keep).strip(), (acts if isinstance(acts, list) else [])


def apply_actions(acts):
    """Whitelisted bot controls Claude may invoke when the owner asks for a change."""
    global MODE, COPY_FRACTION, MAX_USDC_PER_TRADE, MIN_HIS_NOTIONAL, SLIPPAGE, BANKROLL
    applied = []
    for a in acts[:6]:
        if not isinstance(a, dict) or a.get("op") not in ALLOWED_OPS:
            continue
        op, v = a["op"], a.get("value")
        try:
            if op == "live":
                if v:
                    try_go_live("claude copilot")
                    applied.append("live ON" if STATE["live"] else "live requested (check status)")
                else:
                    with LOCK:
                        STATE["live"] = False
                    applied.append("live OFF")
            elif op == "mode" and v in ("auto", "approve"):
                MODE = v
                applied.append(f"mode={v}")
            elif op in ("fraction", "cap", "min_his", "slippage", "bankroll"):
                v = float(v)
                if op == "fraction":
                    COPY_FRACTION = v
                elif op == "cap":
                    MAX_USDC_PER_TRADE = v
                elif op == "min_his":
                    MIN_HIS_NOTIONAL = v
                elif op == "slippage":
                    SLIPPAGE = v
                else:
                    BANKROLL = v
                applied.append(f"{op}={v:g}")
            elif op == "add_target" and valid_addr(str(v)) and v not in TARGETS:
                TARGETS.append(v)
                with LOCK:
                    STATE["baselined"].discard(v)
                applied.append(f"added target {str(v)[:8]}…")
            elif op == "drop_target" and v in TARGETS:
                TARGETS.remove(v)
                applied.append(f"dropped target {str(v)[:8]}…")
        except (TypeError, ValueError):
            continue
    if applied:
        save_config()
        logline(kind="skip", note="claude copilot: " + ", ".join(applied))
    return applied


def chat_add(who, text):
    """Append one copilot message to the in-memory log AND to disk (jsonl)."""
    entry = {"who": who, "text": text, "t": time.strftime("%Y-%m-%d %H:%M:%S")}
    with LOCK:
        STATE["chat"].append(entry)
    try:
        with CHAT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_chat():
    if not CHAT_FILE.exists():
        return
    try:
        for ln in CHAT_FILE.read_text(encoding="utf-8").splitlines()[-30:]:
            try:
                STATE["chat"].append(json.loads(ln))
            except ValueError:
                pass
    except Exception:
        pass


def ask_claude(q):
    with LOCK:
        STATE["thinking"] = True
        history = "\n".join(f"{m['who']}: {m['text'][:400]}" for m in list(STATE["chat"])[-6:])
    chat_add("you", q)
    try:
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("claude CLI not found on PATH — install Claude Code or add it to PATH")
        prompt = f"""You are the copilot living inside the owner's Polymarket copy-trading bot (single user, their own machine, their own $30 account). You see the bot's live state below. Answer the owner plainly in a few short sentences: explain what the bot is doing, why trades/skips happened, what settings mean, or advise. Be concrete, use the numbers.

CURRENT BOT STATE (live):
{json.dumps(bot_context(), ensure_ascii=False)}

RECENT CONVERSATION:
{history or "(none)"}

You can control the bot. If — and only if — the owner asks for a change, end your reply with one final line:
ACTIONS: [{{"op": "...", "value": ...}}]
Allowed ops: live (true/false) · mode ("auto"/"approve") · fraction (number) · cap ($) · min_his ($) · slippage (0-1) · bankroll ($) · add_target ("0x...") · drop_target ("0x...").
If no change was requested, end with exactly: ACTIONS: []

OWNER: {q}"""
        # scrub inherited session vars so the CLI always uses the owner's own login
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTHROPIC", "CLAUDE"))}
        r = subprocess.run([exe, "-p", prompt, "--model", CLAUDE_MODEL],
                           capture_output=True, text=True, timeout=240,
                           encoding="utf-8", errors="replace", env=env)
        out = (r.stdout or "").strip()
        if not out:
            out = f"(claude cli gave no output: {(r.stderr or 'unknown error')[:200]})"
        reply, acts = parse_actions(out)
        applied = apply_actions(acts)
        if applied:
            reply += ("\n\n⚙ applied: " + ", ".join(applied))
        chat_add("claude", reply or "(empty reply)")
    except subprocess.TimeoutExpired:
        chat_add("claude", "(timed out after 240s — try again)")
    except Exception as ex:
        chat_add("claude", f"(error: {str(ex)[:200]})")
    finally:
        with LOCK:
            STATE["thinking"] = False


def ws_loop():
    """Real-time path: stream every platform trade, act on our targets' ones
    in <1s instead of waiting for the next poll. REST polling stays running
    underneath as reconciliation + fallback (same seen-keys, so no doubles)."""
    try:
        import websocket
    except ImportError:
        logline(kind="error", note="websocket-client not installed — realtime off, polling only")
        return

    def on_open(ws):
        with LOCK:
            STATE["ws"] = True
        ws.send(json.dumps({"action": "subscribe",
                            "subscriptions": [{"topic": "activity", "type": "trades"}]}))

    def on_message(ws, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        for msg in (m if isinstance(m, list) else [m]):
            if not isinstance(msg, dict) or msg.get("topic") != "activity":
                continue
            p = msg.get("payload") or {}
            for t in (p if isinstance(p, list) else [p]):
                if not all(k in t for k in ("transactionHash", "asset", "side", "price", "size")):
                    continue
                wallet = str(t.get("proxyWallet", "")).lower()
                match = next((a for a in TARGETS if a.lower() == wallet), None)
                if not match:
                    continue
                k = key(t)
                with LOCK:
                    if k in STATE["seen"] or match not in STATE["baselined"]:
                        continue  # dupe, or first poll hasn't baselined this target yet
                    STATE["seen"].add(k)
                handle(t)
                save_state()

    def on_down(ws, *a):
        with LOCK:
            STATE["ws"] = False

    while True:
        try:
            websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                   on_error=on_down, on_close=on_down
                                   ).run_forever(ping_interval=25, ping_timeout=10)
        except Exception:
            pass
        with LOCK:
            STATE["ws"] = False
        time.sleep(5)  # reconnect backoff


def bot_loop():
    try_go_live("startup")
    polls = 0
    while True:
        if STATE["missing_deps"] and not check_deps(log=False):
            # environment healed (pip finished / lock released): restart the realtime
            # thread (its import failed once and it exited) and re-arm via auto-live
            logline(kind="live", note="runtime modules recovered — realtime restarted, live re-armed if configured")
            threading.Thread(target=ws_loop, daemon=True).start()
            try_go_live("deps recovered")
        if polls % LEADERS_EVERY == 0:  # leaderboard loads even before a target is set
            leaders = fetch_leaders()
            if leaders:
                with LOCK:
                    STATE["leaders"] = leaders
        fetch_wallet()
        settle_resolved()
        reconcile_ghosts()
        reconcile_odometer()  # floor to open stakes; release settled-loss cruft when flat
        auto_cap()
        polls += 1

        if not TARGETS:
            time.sleep(POLL_SECONDS)
            continue

        merged = []
        for target in list(TARGETS):
            try:
                with LOCK:
                    seen_snap = set(STATE["seen"])
                    baselined = target in STATE["baselined"]
                # first sight: one page is enough (we only baseline it, never copy).
                # steady state: page back to known ground so bursts can't slip through.
                trades = fetch_trades(target) if not baselined \
                    else fetch_trades_backfilled(target, seen_snap)
                with LOCK:
                    STATE["error"] = ""
                    STATE["last_poll"] = time.time()
                POLL_FAILS.pop(target, None)
            except Exception as ex:
                # one TCP reset self-heals on the next 15s cycle (WS still live) —
                # only a streak means the feed is actually unreachable
                n = POLL_FAILS[target] = POLL_FAILS.get(target, 0) + 1
                if n == 3:
                    logline(kind="error", note=f"poll of {target[:8]}… failing {n}x in a row: {str(ex)[:90]}")
                if n >= 3:
                    with LOCK:
                        STATE["error"] = f"{target[:8]}… unreachable ~{n * POLL_SECONDS}s: {str(ex)[:120]}"
                continue

            with LOCK:
                nm = STATE["tnames"].get(target)
            if not nm:
                nm = fetch_name(target) or target[:8] + "…"
                with LOCK:
                    STATE["tnames"][target] = nm
            for t in trades:
                t["_who"] = nm
            merged += trades[:25]

            with LOCK:
                first_time = target not in STATE["baselined"]
            if first_time:
                with LOCK:
                    for t in trades:
                        STATE["seen"].add(key(t))
                    STATE["baselined"].add(target)
                logline(kind="skip", note=f"baselined {len(trades)} past trades of {nm} — watching from now")
            else:
                for t in reversed(trades):
                    k = key(t)
                    with LOCK:
                        fresh = k not in STATE["seen"]
                    if fresh:
                        handle(t)
                        with LOCK:
                            STATE["seen"].add(k)

        merged.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
        with LOCK:
            STATE["target_feed"] = merged[:25]
        save_state()
        for t in merged[:25]:  # warm end-date cache so the feed can tint copyable rows
            if str(t.get("side", "")).upper() == "BUY":
                market_end_ts(t.get("asset"))
        time.sleep(POLL_SECONDS)


# ---- dashboard --------------------------------------------------------------
KIND_COLOR = {"live": "#4ade80", "dry": "#93c5fd", "skip": "#9ca3af",
              "error": "#f87171", "pend": "#fbbf24"}


def _nw(e):
    """Market name plus a dim 'which trader' suffix when attribution is known."""
    w = e.get("who") if isinstance(e, dict) else ""
    return f'{e.get("name", "")}' + (f' <span class=dim>· {w}</span>' if w else "")


def _pending_card():
    with LOCK:
        pending = list(STATE["pending"].values())
    if not pending and MODE != "approve":
        return ""
    rows = ""
    for it in pending:
        px = limit_price(it["ref"], it["side"])
        col = "#4ade80" if it["side"] == "BUY" else "#fca5a5"
        rows += (f'<tr><td class=dim>{it["t"]}</td><td style=color:{col}>{it["side"]}</td>'
                 f'<td>{_nw(it)}</td><td class=r>{it["shares"]:g} @ {px}</td><td>'
                 f'<form method=post action=/approve style=display:inline>'
                 f'<input type=hidden name=id value="{it["id"]}"><button class=go>✓ approve</button></form> '
                 f'<form method=post action=/reject style=display:inline>'
                 f'<input type=hidden name=id value="{it["id"]}"><button class=kill>✗</button></form>'
                 f'</td></tr>')
    rows = rows or "<tr><td colspan=5 class=dim>queue empty — new copies will wait here for your ✓</td></tr>"
    return (f'<h2 style=color:#fbbf24>⏳ Awaiting your approval</h2><div class=card>'
            f'<table><tr><th>time</th><th>side</th><th>market — outcome</th>'
            f'<th class=r>size</th><th></th></tr>{rows}</table></div>')


def _history_rows():
    with LOCK:
        hist = list(STATE["history"])[-30:][::-1]
    out = ""
    for e in hist:
        c = KIND_COLOR.get(e.get("kind"), "#e5e7eb")
        out += (f'<tr><td class=dim>{e.get("d", "")} {e.get("t", "")}</td>'
                f'<td style=color:{c}>{e.get("kind", "").upper()}</td><td>{e.get("side", "")}</td>'
                f'<td>{_nw(e)}</td><td class=r>{e.get("shares", "")} @ {e.get("price", "")}</td>'
                f'<td class=dim>{e.get("note", "")}</td></tr>')
    return out or "<tr><td colspan=6 class=dim>no trades yet</td></tr>"


def _chat_card():
    with LOCK:
        chat = list(STATE["chat"])
        thinking = STATE["thinking"]
    if not chat and not thinking:
        return ""
    rows = ""
    for m in chat:
        who_col = "#a78bfa" if m["who"] == "claude" else "#6b7280"
        body = html.escape(m["text"]).replace("\n", "<br>")
        rows += (f'<div style="margin:6px 0"><span style="color:{who_col};font-weight:700">'
                 f'{"🤖 claude" if m["who"] == "claude" else "you"}</span> '
                 f'<span>{body}</span></div>')
    if thinking:
        rows += '<div class=dim>🤖 claude (opus 4.8) is thinking…</div>'
    return f'<h2>Claude copilot</h2><div class=card>{rows}</div>'


def _fmt_ends(p):
    """'2026-07-20 · 17d left' | 'today!' | 'ended — resolving' | 'settled'."""
    ed = (p.get("endDate") or "")[:10]
    if not ed:
        return "?"
    try:
        import datetime
        days = (datetime.date.fromisoformat(ed) - datetime.date.today()).days
    except ValueError:
        return ed
    if p.get("redeemable"):
        return f"{ed} · settled"
    if days < 0:
        return f"{ed} · resolving"
    if days == 0:
        return "today!"
    return f"{ed} · {days}d left"


def _wallet_card():
    with LOCK:
        w = dict(STATE["wallet"])
        who = dict(STATE["who"])
    allpos = w.get("positions", [])
    pos = [p for p in allpos if float(p.get("currentValue") or 0) > 0.02]   # worth showing
    dust = len(allpos) - len(pos)                                            # settled/worthless
    cash = w.get("usdc")
    total = sum(float(p.get("currentValue") or 0) for p in pos)
    rows = ""
    for p in pos:
        pnl = float(p.get("cashPnl") or 0)
        pct = float(p.get("percentPnl") or 0)
        col = "#4ade80" if pnl >= 0 else "#f87171"
        sell = ""
        if p.get("asset") and not p.get("redeemable"):
            sell = (f'<form method=post action=/sellpos style=display:inline '
                    f'onsubmit="return confirm(\'Sell this whole position at market now?\')">'
                    f'<input type=hidden name=tid value="{p["asset"]}">'
                    f'<button class=dry title="market-sell the whole position">sell</button></form>')
        via = who.get(p.get("asset"), "")
        via = f' <span class=dim>· via {via}</span>' if via else ""
        rows += (f'<tr><td>{p.get("title", "?")} — {p.get("outcome", "?")}{via}</td>'
                 f'<td class=r>{float(p.get("size") or 0):g}</td>'
                 f'<td class=r>{float(p.get("avgPrice") or 0):.3f} → {float(p.get("curPrice") or 0):.3f}</td>'
                 f'<td class=r>${float(p.get("currentValue") or 0):,.2f}</td>'
                 f'<td class=r style=color:{col}>{pnl:+,.2f} ({pct:+.1f}%)</td>'
                 f'<td class=dim>{_fmt_ends(p)}</td><td>{sell}</td></tr>')
    rows = rows or "<tr><td colspan=7 class=dim>no open positions on-chain</td></tr>"
    if dust:
        rows += (f'<tr><td colspan=7 class=dim>… {dust} settled/worthless positions hidden '
                 f'(old resolved bets, $0 value)</td></tr>')
    checked = w.get("checked", "")
    tradeable = w.get("tradeable")
    if cash is None:
        cash_s = "cash: couldn't read chain (retrying)"
    else:
        cash_s = f"${cash:,.2f} cash (pUSD)"
        if tradeable is not None and abs(tradeable - cash) > 0.01:
            cash_s += f" (${tradeable:,.2f} tradeable on CLOB)"
    total_s = f" · ${cash + total:,.2f} total" if cash is not None else ""
    warn = ""
    if cash is not None and (cash + total) < 1.0:  # no cash and no live value, ignoring dead $0 dust
        warn = ('<div class=err style="margin-top:6px">This wallet is empty on-chain. '
                'If Polymarket shows a balance, your money is on a different address than the funder above — '
                'open Polymarket → Deposit, copy that exact 0x address into the Funder field, and use its exported key.</div>')
    return (f'<h2>Your wallet — on-chain truth</h2><div class=card>'
            f'<div style=margin-bottom:4px><b>{cash_s}</b> · ${total:,.2f} in positions{total_s}</div>'
            f'<div class=dim style="font-size:11px;margin-bottom:6px">checking {checked}</div>'
            f'<table><tr><th>market — outcome</th><th class=r>shares</th><th class=r>avg → now</th>'
            f'<th class=r>value</th><th class=r>pnl</th><th>plays out</th><th></th></tr>{rows}</table>{warn}</div>')


def _target_rows():
    with LOCK:
        feed = list(STATE["target_feed"])[:15]
        taken = {tid for tid, sh in STATE["holdings"].items() if sh > 0}
        taken |= {p.get("tid") for p in STATE["pending"].values()}
        taken |= set(INFLIGHT_BUYS)
    out = ""
    now = time.time()
    for t in feed:
        side = str(t.get("side", "")).upper()
        col = "#4ade80" if side == "BUY" else "#fca5a5"
        name = f"{t.get('title', '?')} — {t.get('outcome', '?')}"
        ts = t.get("timestamp")
        when = time.strftime("%m-%d %H:%M", time.localtime(float(ts))) if ts else ""
        act = ""
        if side == "BUY" and t.get("asset") not in taken:
            act = (f'<form method=post action=/copymiss style=display:inline>'
                   f'<input type=hidden name=k value="{html.escape(key(t))}">'
                   f'<button class=copy title="copy this trade now — goes through the normal '
                   f'gates (no stacking, no chasing, horizon, auto-size)">copy</button></form>')
        # tint = this row would actually copy if clicked: fresh BUY, not held, market
        # still open and inside the horizon cap (cache-only check; price gate at click)
        ets = ENDS_CACHE.get(t.get("asset"))
        good = (act and ets and ets > now
                and (MAX_DAYS_OUT <= 0 or ets - now <= MAX_DAYS_OUT * 86400))
        tint = ' style="background:rgba(74,222,128,.08)"' if good else ""
        out += (f'<tr{tint}><td class=dim>{when}</td>'
                f'<td class=dim>{t.get("_who", "")}</td><td style=color:{col}>{side}</td><td>{name}</td>'
                f'<td class=r>{float(t.get("size", 0)):g}</td><td class=r>@{t.get("price", "")}</td><td>{act}</td></tr>')
    return out or "<tr><td colspan=7 class=dim>no recent activity</td></tr>"


def _status_card():
    """Am-I-actually-set-up panel: every prerequisite with its fix."""
    with LOCK:
        last, conn, live = STATE["last_poll"], STATE["conn"], STATE["live"]
        tnames = dict(STATE["tnames"])
    signer = key_wallet(PRIVATE_KEY_MEM or os.environ.get("PM_PRIVATE_KEY", ""))
    key_ok = bool(signer)
    funder_ok = valid_addr(STATE.get("funder") or os.environ.get("PM_FUNDER", ""))
    feed_ok = bool(last and time.time() - last < POLL_SECONDS * 3)
    tgt_label = ", ".join(tnames.get(a, a[:8] + "…") for a in TARGETS)
    conn_ok = bool(conn and conn[0])
    conn_msg = (conn[1] if conn else 'unverified — click "Test connection"')
    items = [
        ("Target trader(s)", bool(TARGETS),
         tgt_label or "paste an address in Settings, or click copy on the leaderboard below"),
        ("Watching their trades (data feed)", feed_ok,
         "live" if feed_ok else "waiting for first poll — needs a target set"),
        ("Real-time feed (WebSocket)", STATE["ws"],
         "connected — copies land in under a second" if STATE["ws"]
         else "reconnecting… polling covers the gap (15s worst case)"),
        ("Funder wallet", funder_ok, "saved" if funder_ok else "paste your Polymarket deposit address in Settings"),
        ("Private key", key_ok,
         f"✓ verified real — signs as {signer}" if key_ok else "paste it in Settings"),
        ("Polymarket trading connection", conn_ok, conn_msg),
        ("Live trading", live, "ON — copying for real" if live else "OFF — click Go LIVE once everything above is ✓"),
    ]
    rows = "".join(
        f'<tr><td>{"✅" if ok else "❌"}</td><td>{what}</td><td class=dim>{detail}</td></tr>'
        for what, ok, detail in items)
    return (f'<div class=card style=margin-bottom:16px><h2 style=margin-top:0>Status — what\'s left to make it trade</h2>'
            f'<table>{rows}</table>'
            f'<form method=post action=/test style=display:inline><button class=copy>Test connection</button></form> '
            f'<form method=post action=/testtrade style=display:inline '
            f'onsubmit="return confirm(\'Places a REAL ~$1 buy and sells it right back. Costs a few cents. Go?\')">'
            f'<button class=copy>Test trade (~$1 round trip)</button></form>'
            f'</div>')


def _leader_rows():
    with LOCK:
        leaders = list(STATE["leaders"])
    cur = {a.lower() for a in TARGETS}
    out = ""
    for i, l in enumerate(leaders, 1):
        addr = str(l.get("proxyWallet") or l.get("address") or "")
        nm = l.get("userName") or l.get("name") or (addr[:8] + "…")
        pnl = l.get("pnl") or l.get("profit") or 0
        try:
            pnl = f"${float(pnl):,.0f}"
        except (TypeError, ValueError):
            pnl = str(pnl)
        copying = addr.lower() in cur
        here = " style=background:#1e293b" if copying else ""
        label, cls = ("✓ copying — drop", "dry") if copying else ("copy", "copy")
        btn = (f'<form method=post action=/target style=margin:0><input type=hidden name=addr value="{addr}">'
               f'<button class={cls}>{label}</button></form>') if addr else ""
        out += (f'<tr{here}><td class=dim>{i}</td><td>{nm}</td>'
                f'<td class=r style=color:#4ade80>{pnl}</td><td>{btn}</td></tr>')
    return out or "<tr><td colspan=4 class=dim>leaderboard loading…</td></tr>"


SCOUT = {"running": False, "note": "", "rows": [], "at": 0.0}
SPORTY = (" vs", "O/U", "win on", "end in a draw", "Team to Advance", "(BO", "1st Half", "Spread:")


def _net_edge(pnl7, d30, vol7, sell_ratio, friction=0.023):
    """Copy-EV bounds per $1 mirrored: their edge per $ traded (7d and 30d
    estimates) minus friction per spread crossing. A flipper pays the exit
    crossing too; holding to resolution settles free. None = no volume."""
    if vol7 <= 0:
        return None
    e7 = pnl7 / vol7
    e30 = d30 / max(1.0, vol7 * 30 / 7)
    crossings = 1 + sell_ratio
    return (round(min(e7, e30) - crossings * friction, 4),
            round(max(e7, e30) - crossings * friction, 4))


def _curve_screen(vals, min_d30=5000.0):
    """Stage-1 survival on a 30d equity curve: profitable, mostly-green days,
    drawdown under 70% of the month's profit. None = screened out."""
    if len(vals) < 8:
        return None
    d30 = vals[-1] - vals[0]
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    green = 100 * sum(1 for d in deltas if d > 0) / len(deltas)
    peak, mdd = -1e18, 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    if d30 < min_d30 or green < 45 or mdd > 0.7 * max(d30, 1.0):
        return None
    return {"d30": round(d30), "green": round(green), "mdd": round(mdd)}


def _scout_deep(c):
    """Deep pass on one survivor: 7d activity mix -> copyability + net edge."""
    import statistics
    addr = c["addr"]
    try:
        r = requests.get("https://lb-api.polymarket.com/profit",
                         params={"window": "7d", "limit": 1, "address": addr}, timeout=15).json()
        pnl7 = float(r[0]["amount"]) if r else 0.0
    except Exception:
        pnl7 = 0.0
    now = time.time()
    fills, vol, sells = [], 0.0, 0
    mkt_cost, clusters = {}, {}
    for d in range(7):
        try:
            rows = requests.get(f"{DATA_API}/activity",
                                params={"user": addr, "limit": 500,
                                        "start": int(now - (d + 1) * 86400),
                                        "end": int(now - d * 86400)}, timeout=15).json()
        except Exception:
            continue
        for a in rows if isinstance(rows, list) else []:
            if a.get("type") != "TRADE":
                continue
            usd = float(a.get("usdcSize", 0))
            vol += usd
            side = (a.get("side") or "").upper()
            sells += side == "SELL"
            fills.append(a)
            if side == "BUY":
                cid = a.get("conditionId") or "?"
                mkt_cost[cid] = mkt_cost.get(cid, 0.0) + usd
            k = (a.get("conditionId"), side, a.get("timestamp", 0) // 300)
            clusters[k] = clusters.get(k, 0.0) + usd
        time.sleep(0.12)
    if not fills:
        return {**c, "verdict": "idle this week", "net": None}
    sell_ratio = sells / len(fills)
    od = round(len(clusters) / 7, 1)
    med = statistics.median(clusters.values()) if clusters else 0.0
    short = known = 0
    for cid, cost in sorted(mkt_cost.items(), key=lambda kv: -kv[1])[:8]:
        sample = next(a for a in fills if a.get("conditionId") == cid)
        ets = market_end_ts(sample.get("asset"))
        if ets:
            known += 1
            short += (ets - sample.get("timestamp", now)) <= 2 * 86400
        elif any(kk in (sample.get("title") or "") for kk in SPORTY):
            known += 1
            short += 1  # negRisk sports: same-day by construction
    short_pct = round(100 * short / known) if known else 0
    net = _net_edge(pnl7, c["d30"], vol, sell_ratio)
    verdict = ("mm-bot — uncopyable" if od > 300 or (med < 20 and od > 120) else
               "long-horizon" if short_pct < 55 else
               "PASS" if net and net[0] > 0 else "edge ≈ 0 after friction")
    return {**c, "pnl7": round(pnl7), "vol7": round(vol), "od": od,
            "sell": round(100 * sell_ratio), "short": short_pct, "net": net, "verdict": verdict}


def scout_run():
    """Background scan: leaderboards -> survival screen -> deep metrics.
    Read-only (~150 public API calls, 2-3 min). Results land in SCOUT."""
    with LOCK:
        if SCOUT["running"]:
            return
        SCOUT.update(running=True, note="pooling leaderboards…", rows=[])
    try:
        pool = {}
        for w in ("7d", "30d"):
            try:
                for r in requests.get("https://lb-api.polymarket.com/profit",
                                      params={"window": w, "limit": 50}, timeout=15).json():
                    a = (r.get("proxyWallet") or "").lower()
                    if a and a not in {t.lower() for t in TARGETS}:
                        pool.setdefault(a, r.get("name") or r.get("pseudonym") or a[:10])
            except Exception:
                pass
            time.sleep(0.2)
        surv = []
        for i, (addr, name) in enumerate(pool.items(), 1):
            with LOCK:
                SCOUT["note"] = f"survival screen {i}/{len(pool)}"
            try:
                c = requests.get("https://user-pnl-api.polymarket.com/user-pnl",
                                 params={"user_address": addr, "interval": "1m",
                                         "fidelity": "1d"}, timeout=15).json()
                s = _curve_screen([float(p["p"]) for p in sorted(c, key=lambda p: p["t"])])
                if s:
                    surv.append(dict(addr=addr, name=name, **s))
            except Exception:
                pass
            time.sleep(0.1)
        surv.sort(key=lambda r: -r["d30"])
        surv = surv[:12]  # deep pass is the expensive part — take the strongest
        done = []
        for i, cand in enumerate(surv, 1):
            with LOCK:
                SCOUT["note"] = f"deep metrics {i}/{len(surv)}: {cand['name'][:18]}"
            done.append(_scout_deep(cand))
            with LOCK:
                SCOUT["rows"] = list(done)
        with LOCK:
            SCOUT["note"] = f"done {time.strftime('%H:%M')} — {sum(1 for r in done if r.get('verdict') == 'PASS')} PASS of {len(done)} deep-checked ({len(pool)} pooled)"
    finally:
        with LOCK:
            SCOUT["running"] = False
            SCOUT["at"] = time.time()


def _scout_card():
    with LOCK:
        s = {"running": SCOUT["running"], "note": SCOUT["note"], "rows": list(SCOUT["rows"])}
    cur = {a.lower() for a in TARGETS}
    btn = ('<form method=post action=/scout style=display:inline>'
           f'<button class=go {"disabled" if s["running"] else ""}>'
           f'{"scanning…" if s["running"] else "Scan for copyable traders"}</button></form>')
    note = f' <span class="tag dim">{html.escape(s["note"])}</span>' if s["note"] else ""
    vcol = {"PASS": "#4ade80", "mm-bot — uncopyable": "#9ca3af",
            "long-horizon": "#9ca3af", "idle this week": "#9ca3af"}
    rows = ""
    for r in sorted(s["rows"], key=lambda r: -(r["net"][0] if r.get("net") else 9e9), reverse=False):
        net = f'{r["net"][0]:+.0%} … {r["net"][1]:+.0%}' if r.get("net") else "—"
        copying = r["addr"].lower() in cur
        act = ("<span class='tag dim'>copying ✓</span>" if copying else
               f'<form method=post action=/target style=margin:0>'
               f'<input type=hidden name=addr value="{r["addr"]}">'
               f'<button class=copy title="start copying — same as clicking copy on the leaderboard">copy</button></form>')
        rows += (f'<tr><td>{html.escape(str(r["name"]))[:20]}</td>'
                 f'<td class=r>${r["d30"]:,}</td><td class=r>${r.get("vol7", 0):,}</td>'
                 f'<td class=r>{r.get("od", "—")}</td><td class=r>{r.get("sell", "—")}%</td>'
                 f'<td class=r>{r.get("short", "—")}%</td><td class=r>{net}</td>'
                 f'<td style="color:{vcol.get(r.get("verdict"), "#fbbf24")}">{r.get("verdict", "")}</td>'
                 f'<td>{act}</td></tr>')
    if not rows:
        rows = ('<tr><td colspan=9 class=dim>'
                + ("scanning — results appear as each trader finishes…" if s["running"] else
                   "not run yet — the scan is read-only, takes ~2–3 min, and ranks the current "
                   "leaderboard by friction-adjusted copy edge (the math in the README)")
                + "</td></tr>")
    return (f'<h2>Trader scout — who is worth copying right now</h2><div class=card>{btn}{note}'
            f'<table><tr><th>trader</th><th class=r>30d pnl</th><th class=r>7d vol</th>'
            f'<th class=r>ord/d</th><th class=r>sell%</th><th class=r>≤2d%</th>'
            f'<th class=r>net copy edge</th><th>verdict</th><th></th></tr>{rows}</table></div>')


def _vstyle(value, ok):
    """Green border = present and verified real. Red = present but bad."""
    if not value:
        return ""
    return "style=border-color:#16a34a;border-width:2px" if ok \
        else "style=border-color:#dc2626;border-width:2px"


def _settings_form():
    is_ready = ready()
    pk_val = PRIVATE_KEY_MEM or os.environ.get("PM_PRIVATE_KEY", "")
    funder_val = STATE.get("funder", "") or os.environ.get("PM_FUNDER", "")
    signer = key_wallet(pk_val)
    key_note = (f' — <span style=color:#4ade80>✓ real key, signs as {signer}</span>' if signer
                else (' — <span style=color:#f87171>✗ not a valid key</span>' if pk_val else ""))
    return f"""<div class=card style=margin-bottom:16px>
<h2 style=margin-top:0>⚙ Settings {'· ✅ ready' if is_ready else '· ⚠ setup needed'}</h2>
<form method=post action=/settings class=settings>
  <label>Target wallet(s) — comma-separate for several<input name=target value="{', '.join(TARGETS)}" {_vstyle(TARGETS, True)} placeholder="0x…, 0x… (or click copy on leaderboard traders)"></label>
  <label>Funder wallet (your deposit address)<input name=funder value="{funder_val}" {_vstyle(funder_val, valid_addr(funder_val))} placeholder="0x…"></label>
  <label>Private key (saved to copybot_config.json on this PC){key_note}<input name=private_key value="" {_vstyle(pk_val, bool(signer))} autocomplete=off placeholder="{'saved ✓ — leave blank to keep, paste to replace' if pk_val else '0x…'}"></label>
  <label>Mode (saves the instant you switch)<select name=mode onchange="this.form.submit()">
    <option value=auto {"selected" if MODE == "auto" else ""}>auto — copy instantly</option>
    <option value=approve {"selected" if MODE == "approve" else ""}>approve — I click ✓ per trade</option>
  </select></label>
  <label>Copy fraction<input name=fraction value="{COPY_FRACTION}"></label>
  <label>Max $ / trade<input name=cap id=tcap value="{MAX_USDC_PER_TRADE}">
    <span id=tcapnote class=dim style="display:none">← auto-sized from wallet</span></label>
  <label>Copy his BUY only if ≥ $<input name=min_his value="{MIN_HIS_NOTIONAL}"></label>
  <label>Hard stop: total live buys ≤ $<input name=spend_cap id=capin value="{SPEND_CAP}">
    <span id=capnote class=dim style="display:none">← overridden while auto budget is on</span></label>
  <label>Only copy markets ending within <input name=max_days_out value="{MAX_DAYS_OUT:g}"> days (0 = any horizon; sells always follow)</label>
  <label>Max open legs per match/event <input name=max_legs_per_event value="{MAX_LEGS_PER_EVENT}"> (0 = uncapped — same-match legs are correlated: m legs ≈ one bet at m× size)</label>
  <label><span style="display:flex;align-items:center;gap:6px"><input type=checkbox id=acr_on style="width:auto;margin:0">
    Auto budget — cap = wallet total minus this $ reserve (uncheck for manual cap)</span>
    <input name=auto_cap_reserve id=acr value="{AUTO_CAP_RESERVE:g}"></label>
  <label><span style="display:flex;align-items:center;gap:6px"><input type=checkbox id=atp_on style="width:auto;margin:0">
    Auto per-trade cap — this % of wallet, $5 floor (uncheck for manual)</span>
    <input name=auto_trade_pct id=atp value="{AUTO_TRADE_PCT:g}"></label>
  <label>Slippage<input name=slippage value="{SLIPPAGE}"></label>
  <label>Bankroll $<input name=bankroll value="{BANKROLL}"></label>
  <button class=save type=submit>Save</button>
</form>
<script>
(function() {{
  // checkbox IS the toggle: unchecking writes 0 into the field (= off on Save),
  // re-checking restores the previous value (or the default).
  function wire(chkId, numId, dimIds, noteId, dflt) {{
    var chk = document.getElementById(chkId), num = document.getElementById(numId),
        note = document.getElementById(noteId);
    function sync() {{
      var on = parseFloat(num.value) > 0;
      chk.checked = on;
      dimIds.forEach(function(id) {{
        var el = document.getElementById(id);
        el.style.opacity = on ? 0.35 : 1;
        el.title = on ? 'auto-managed — ignored while the auto toggle is on' : '';
      }});
      note.style.display = on ? '' : 'none';
    }}
    chk.addEventListener('change', function() {{
      if (chk.checked) {{ num.value = num.dataset.prev > 0 ? num.dataset.prev : dflt; }}
      else {{ num.dataset.prev = parseFloat(num.value) || dflt; num.value = 0; }}
      sync();
    }});
    num.addEventListener('input', sync);
    sync();
  }}
  wire('acr_on', 'acr', ['capin'], 'capnote', 8);
  wire('atp_on', 'atp', ['tcap'], 'tcapnote', 10);
}})();
</script>
</div>"""


def render_dyn():
    """Everything that changes — swapped into the page in place every 3s.
    The settings form lives OUTSIDE this, so refresh can never eat your input."""
    with LOCK:
        live, copies, err = STATE["live"], STATE["copies"], STATE["error"]
        started, last = STATE["started"], STATE["last_poll"]
        holdings = [(STATE["names"].get(k, k[:16]), v, STATE["who"].get(k, "")) for k, v in STATE["holdings"].items() if v > 0]
        tnames = dict(STATE["tnames"])
        feed = list(STATE["target_feed"])
        log = list(STATE["log"])
    up = int(time.time() - started)
    age = "never" if not last else f"{int(time.time() - last)}s ago"
    if not live:
        pill = '<span style="background:#374151;color:#93c5fd">◦ DRY — watch only, no trades</span>'
    elif MODE == "auto":
        pill = '<span style="background:#166534;color:#4ade80">● LIVE · AUTO — copying trades now</span>'
    else:
        pill = '<span style="background:#7c5e00;color:#fbbf24">● LIVE · APPROVE — waiting for your ✓ (nothing trades unattended)</span>'
    if live:
        toggle = '<form method=post action=/dry style=display:inline><button class=dry>Go DRY</button></form>'
    elif ready():
        toggle = '<form method=post action=/live style=display:inline><button class=go>Go LIVE ▶</button></form>'
    else:
        toggle = '<span class="tag dim">configure ⚙ to enable live</span>'

    hrows = "".join(f'<tr><td>{n}{f" <span class=dim>· via {w_}</span>" if w_ else ""}</td>'
                    f'<td class=r>{s:g}</td></tr>' for n, s, w_ in holdings) \
        or "<tr><td colspan=2 class=dim>no open positions</td></tr>"
    intents = [e for e in log if e.get("kind") == "dry" and e.get("side") == "BUY"][:8]
    irows = "".join(f'<tr><td class=dim>{e["t"]}</td><td>{e.get("name", "")}</td>'
                    f'<td class=r>{e.get("shares", "")}@{e.get("price", "")}</td></tr>' for e in intents) \
        or "<tr><td colspan=3 class=dim>none — live mode fills immediately</td></tr>"
    lrows = ""
    for e in log[:40]:
        c = KIND_COLOR.get(e.get("kind"), "#e5e7eb")
        px = f'@{e["price"]}' if "price" in e else ""
        lrows += (f'<tr><td class=dim>{e["t"]}</td><td style=color:{c}>{e.get("kind", "").upper()}</td>'
                  f'<td>{e.get("side", "")}</td><td>{_nw(e)}</td>'
                  f'<td class=r>{e.get("shares", "")} {px}</td><td class=dim>{e.get("note", "")}</td></tr>')
    lrows = lrows or "<tr><td colspan=6 class=dim>waiting…</td></tr>"
    stats = "".join(f"<li>{s}</li>" for s in copy_stats(feed))
    errbar = f'<div class=err>{err}</div>' if err else ""
    md = STATE.get("missing_deps") or []
    if md:
        errbar = (f'<div class=err>⚠ Missing Python modules: <b>{", ".join(md)}</b> — the app cannot trade until '
                  f'these are installed. Open a terminal and run:<br><code>pip install {" ".join(md)}</code><br>'
                  f'then close and relaunch Copybot.</div>') + errbar
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER", "")
    funder_chip = f'<span class="tag dim">funder {funder}</span>' if funder else ""

    def _prof_btn(addr, label, tip):
        return (f'<form method=post action=/openpm style=display:inline>'
                f'<input type=hidden name=addr value="{addr}">'
                f'<button class=copy title="{tip}">{label} ↗</button></form>')

    tgt_btns = " ".join(_prof_btn(a, tnames.get(a, a[:8] + "…"),
                                  "open their Polymarket profile in your browser") for a in TARGETS) \
        or '<span class="tag dim">no target set</span>'
    me_btn = _prof_btn(funder, "my profile", "open your Polymarket profile in your browser") if funder else ""

    return f"""<div class=bar>
  <span class=pill>{pill}</span>
  <span class="tag dim">copying</span> {tgt_btns}
  {funder_chip}
  {me_btn}
  <span class="tag dim">copies {copies}</span>
  <span class="tag" style="color:#fbbf24">budget ${STATE["spent_live"]:.2f} / ${SPEND_CAP:.2f}</span>
  <span class="tag dim">up {up // 3600}h{up % 3600 // 60}m</span>
  <span class="tag dim">polled {age}</span>
  {toggle}
  <form method=post action=/kill style=display:inline onsubmit="return confirm('Kill the bot?')"><button class=kill>Kill</button></form>
</div>
{errbar}
{_chat_card()}
{_status_card()}
{_pending_card()}
{_wallet_card()}
<div class=grid>
  <div class=card>
    <h2>Bot — holding</h2>
    <table><tr><th>market — outcome</th><th class=r>shares</th></tr>{hrows}</table>
    <h2>Bot — wants to buy (DRY intents)</h2>
    <table><tr><th>time</th><th>market — outcome</th><th class=r>size</th></tr>{irows}</table>
  </div>
  <div class=card>
    <h2>Targets — live activity <form method=post action=/copyall style=display:inline
      onsubmit="return confirm('Replay ALL shown BUY rows through the normal gates (no stacking, no chasing, horizon, budget)?')">
      <button class=copy title="copy every displayed BUY that is not already held — each one still passes all gates">copy all shown</button></form></h2>
    <table><tr><th>when</th><th>trader</th><th>side</th><th>market — outcome</th><th class=r>size</th><th class=r>px</th><th></th></tr>{_target_rows()}</table>
    <h2>How to copy him best</h2>
    <ul class=tips>{stats}</ul>
  </div>
</div>
{_scout_card()}
<h2>Who else to copy (leaderboard — click to switch target)</h2>
<div class=card><table><tr><th>#</th><th>trader</th><th class=r>pnl</th><th></th></tr>{_leader_rows()}</table></div>
<h2>Trade history</h2>
<div class=card><table><tr><th>when</th><th>kind</th><th>side</th><th>market — outcome</th><th class=r>size</th><th>note</th></tr>{_history_rows()}</table></div>
<h2>Bot activity log</h2>
<div class=card><table><tr><th>time</th><th>kind</th><th>side</th><th>market — outcome</th><th class=r>size</th><th>note</th></tr>{lrows}</table></div>"""


BUILD = time.strftime("%Y-%m-%d %H:%M", time.localtime(Path(__file__).stat().st_mtime))


def render():
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>copybot</title><style>
body{{background:#0b0f17;color:#e5e7eb;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:18px;max-width:1100px}}
h1{{font-size:15px;margin:0 0 12px}} .dim{{color:#6b7280}} .r{{text-align:right}}
.bar{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px}}
span.pill,span.tag{{padding:3px 9px;border-radius:6px;font-weight:600}}
button{{border:0;border-radius:6px;padding:6px 13px;font:inherit;font-weight:700;cursor:pointer}}
button.go{{background:#166534;color:#fff}} button.dry{{background:#374151;color:#fff}}
button.kill{{background:#7f1d1d;color:#fff}} button.copy{{background:#1d4ed8;color:#fff;padding:3px 10px}}
button.save{{background:#1d4ed8;color:#fff;margin-top:4px}}
table{{border-collapse:collapse;width:100%;margin:4px 0 16px}}
td,th{{padding:4px 9px;border-bottom:1px solid #1f2937;text-align:left}} th{{color:#9ca3af;font-weight:600}}
h2{{font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:.05em;margin:14px 0 2px}}
.err{{background:#7f1d1d;color:#fecaca;padding:6px 10px;border-radius:6px;margin-bottom:12px}}
ul.tips{{margin:4px 0 16px;padding-left:18px}} ul.tips li{{margin:2px 0}}
.card{{background:#0f1523;border:1px solid #1f2937;border-radius:10px;padding:14px 16px}}
details.cfg{{background:#0f1523;border:1px solid #1f2937;border-radius:10px;padding:10px 14px;margin-bottom:16px}}
details.cfg summary{{cursor:pointer;font-weight:700}}
.settings{{display:grid;grid-template-columns:1fr 1fr;gap:10px 16px;margin-top:12px}}
.settings label{{display:flex;flex-direction:column;font-size:11px;color:#9ca3af;gap:3px}}
.settings input,.settings select{{background:#0b0f17;border:1px solid #334155;border-radius:6px;color:#e5e7eb;padding:6px 8px;font:inherit}}
.settings button{{grid-column:1/-1;justify-self:start}}
</style></head><body>
<h1>Polymarket copybot <span class=dim style=font-weight:400>build {BUILD}</span></h1>
{_settings_form()}
<form method=post action=/ask style="display:flex;gap:8px;margin-bottom:14px">
  <input name=q autocomplete=off placeholder="Ask Claude anything about the bot — or tell it to change settings, switch modes, go live/dry…"
         style="flex:1;background:#0f1523;border:1px solid #334155;border-radius:8px;color:#e5e7eb;padding:8px 10px;font:inherit">
  <button class=copy>Ask Claude</button>
</form>
<div id=dyn>{render_dyn()}</div>
<script>
// instant paste feedback: green the moment the value looks right, red + reason
// if not. Server re-verifies cryptographically on Save.
(function() {{
  var rules = {{
    target: {{ test: function(v) {{ return v.trim().split(/[\\s,]+/).every(function(a) {{ return /^0x[a-fA-F0-9]{{40}}$/.test(a); }}); }},
              ok: '✓ valid address — press Save', bad: '✗ not a valid 0x address (42 chars)' }},
    funder: {{ test: function(v) {{ return /^0x[a-fA-F0-9]{{40}}$/.test(v.trim()); }},
              ok: '✓ valid address — press Save', bad: '✗ not a valid 0x address (42 chars)' }},
    private_key: {{ test: function(v) {{ return v.trim() === '' || /^(0x)?[0-9a-fA-F]{{64}}$/.test(v.trim()); }},
              ok: '✓ valid key format — press Save to verify + store', bad: '✗ a key is 64 hex chars (66 with 0x)' }}
  }};
  document.querySelectorAll('.settings input').forEach(function(inp) {{
    var rule = rules[inp.name];
    if (!rule) return;
    var hint = document.createElement('div');
    hint.style.fontSize = '11px';
    inp.parentNode.appendChild(hint);
    var paint = function() {{
      var v = inp.value.trim();
      if (!v) {{ inp.style.border = ''; hint.textContent = ''; return; }}
      var good = rule.test(v);
      inp.style.border = '2px solid ' + (good ? '#16a34a' : '#dc2626');
      hint.style.color = good ? '#4ade80' : '#f87171';
      hint.textContent = good ? rule.ok : rule.bad;
    }};
    inp.addEventListener('input', paint);
    paint();  // also color prefilled (saved) values on load
  }});
}})();
// live update: only the #dyn region is swapped — the settings form above is
// never re-rendered, so nothing you type can ever disappear.
setInterval(function() {{
  fetch('/dyn').then(function(r) {{ return r.text(); }})
    .then(function(h) {{ document.getElementById('dyn').innerHTML = h; }})
    .catch(function() {{}});
}}, 3000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self):
        if urlparse(self.path).path == "/dyn":
            self._send(200, render_dyn())
        else:
            self._send(200, render())

    def do_POST(self):
        global TARGETS, COPY_FRACTION, MAX_USDC_PER_TRADE, SLIPPAGE, BANKROLL, \
            PRIVATE_KEY_MEM, MODE, CLIENT, MIN_HIS_NOTIONAL
        path = urlparse(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)).decode()
        f = parse_qs(body)

        def num(name, cur):
            try:
                return float(f.get(name, [""])[0])
            except ValueError:
                return cur

        if path == "/live":
            if ready():
                try:
                    get_client()  # validate creds NOW, not on the first trade
                    with LOCK:
                        STATE["live"] = True
                        STATE["error"] = ""
                except Exception as ex:
                    with LOCK:
                        STATE["error"] = f"can't go live — credential check failed: {str(ex)[:120]}"
            else:
                with LOCK:
                    STATE["error"] = "Set target + funder + private key in Settings before going live."
        elif path == "/test":
            CLIENT = None  # force a fresh connection with current creds
            try:
                cl = get_client()
                bal = ""
                try:
                    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
                    b = cl.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                    if isinstance(b, dict) and b.get("balance") is not None:
                        bal = f" · balance ${float(b['balance']) / 1e6:,.2f} USDC"
                except Exception:
                    pass  # connection proved; balance is a bonus
                with LOCK:
                    STATE["conn"] = (True, f"connected — API creds derived OK{bal}")
            except KeyError as ex:
                with LOCK:
                    STATE["conn"] = (False, f"missing setting: {ex}")
            except Exception as ex:
                with LOCK:
                    STATE["conn"] = (False, str(ex)[:160])
        elif path == "/testtrade":
            threading.Thread(target=test_trade, daemon=True).start()
        elif path == "/sellpos":  # wallet-card sell button: liquidate one position
            tid = f.get("tid", [""])[0].strip()
            if tid:
                threading.Thread(target=sell_position, args=(tid,), daemon=True).start()
        elif path == "/copymiss":  # feed-row copy button: owner replays a missed BUY
            k = f.get("k", [""])[0].strip()
            if k:
                threading.Thread(target=copy_missed, args=(k,), daemon=True).start()
        elif path == "/copyall":  # feed button: replay ALL shown missed BUYs (each gated)
            threading.Thread(target=copy_all_missed, daemon=True).start()
        elif path == "/scout":  # read-only trader scan; single-flight guarded inside
            threading.Thread(target=scout_run, daemon=True).start()
        elif path == "/openpm":  # open a Polymarket profile in the system browser
            addr = f.get("addr", [""])[0].strip()
            if valid_addr(addr):
                try:  # no browser/DISPLAY on a headless VPS — never crash the handler
                    webbrowser.open(f"https://polymarket.com/profile/{addr}")
                except Exception:
                    pass
        elif path == "/ask":
            q = f.get("q", [""])[0].strip()
            if q and not STATE["thinking"]:
                threading.Thread(target=ask_claude, args=(q,), daemon=True).start()
        elif path == "/approve":
            pid = f.get("id", [""])[0]
            with LOCK:
                it = STATE["pending"].pop(pid, None)
            if it:
                execute(it)
        elif path == "/reject":
            pid = f.get("id", [""])[0]
            with LOCK:
                it = STATE["pending"].pop(pid, None)
            if it:
                logline(kind="skip", side=it["side"], name=it["name"], note="rejected by you")
        elif path == "/dry":
            with LOCK:
                STATE["live"] = False
        elif path == "/target":  # leaderboard button: toggle copying this trader
            addr = f.get("addr", [""])[0].strip()
            if addr.startswith("0x") and len(addr) == 42:
                if addr in TARGETS:
                    TARGETS.remove(addr)
                    logline(kind="skip", note=f"dropped target {addr[:8]}…")
                else:
                    TARGETS.append(addr)
                    with LOCK:
                        STATE["baselined"].discard(addr)
                    logline(kind="skip", note=f"added target {addr[:8]}…")
                save_config()
        elif path == "/settings":
            new_targets = parse_targets(f.get("target", [""])[0])
            for a in new_targets:
                if a not in TARGETS:
                    with LOCK:
                        STATE["baselined"].discard(a)
            TARGETS = new_targets
            fn = f.get("funder", [""])[0].strip()
            if fn:
                STATE["funder"] = fn
            pk = f.get("private_key", [""])[0].strip()
            if pk:
                PRIVATE_KEY_MEM = pk
                with LOCK:
                    STATE["pk_set"] = True
            m = f.get("mode", [""])[0]
            if m in ("auto", "approve"):
                MODE = m
            COPY_FRACTION = num("fraction", COPY_FRACTION)
            MAX_USDC_PER_TRADE = num("cap", MAX_USDC_PER_TRADE)
            MIN_HIS_NOTIONAL = num("min_his", MIN_HIS_NOTIONAL)
            globals()["SPEND_CAP"] = num("spend_cap", SPEND_CAP)
            globals()["MAX_DAYS_OUT"] = num("max_days_out", MAX_DAYS_OUT)
            globals()["MAX_LEGS_PER_EVENT"] = int(num("max_legs_per_event", MAX_LEGS_PER_EVENT))
            globals()["AUTO_CAP_RESERVE"] = num("auto_cap_reserve", AUTO_CAP_RESERVE)
            globals()["AUTO_TRADE_PCT"] = num("auto_trade_pct", AUTO_TRADE_PCT)
            SLIPPAGE = num("slippage", SLIPPAGE)
            BANKROLL = num("bankroll", BANKROLL)
            save_config()
            try_go_live("settings saved")  # fully configured -> start trading immediately
        elif path == "/kill":
            if HEADLESS:  # on a VPS the process is systemd-managed, not window-bound
                self._send(200, "headless: use `systemctl stop copybot` on the server")
                return
            self._send(200, "bye")
            os._exit(0)  # ponytail: abrupt, but it's a side-project button
        self._redirect()

    def log_message(self, *a):
        pass


# ---- entry ------------------------------------------------------------------
def _check():
    # never touch the real config/state during self-checks (they hold live creds)
    globals()["CONFIG_FILE"] = Path(os.environ.get("TEMP", ".")) / "copybot_check_config.json"
    globals()["STATE_FILE"] = Path(os.environ.get("TEMP", ".")) / "copybot_check_state.json"
    globals()["CHAT_FILE"] = Path(os.environ.get("TEMP", ".")) / "copybot_check_chat.jsonl"
    globals()["midpoint"] = lambda tid: None  # offline: no live mid / tick lookups
    globals()["tick_of"] = lambda tid: 0.01
    assert my_buy_size(1000, 0.50, 0.01, 50) == 10.0
    assert my_buy_size(1000, 0.50, 0.01, 3) == 6.0
    assert my_buy_size(10, 0.50, 0.01, 50) == 2.0     # tiny clip floors to $1 min
    assert my_buy_size(272, 0.2925, 0.01, 5) == 3.42  # his real median-size clip → $1 copy
    assert my_buy_size(100, 0.0, 0.01, 5) == 0.0      # unpriced trade never divides by zero
    assert int(6666665 / 1e4) / 100.0 == 6.66         # chain balance floors to sellable shares
    assert limit_price(0.50, "BUY") == 0.51
    assert limit_price(0.50, "SELL") == 0.49
    assert limit_price(0.99, "BUY") == 0.99
    assert limit_price(0.505, "BUY", 0.001) == 0.516   # sub-cent tick honored
    assert limit_price(0.505, "SELL", 0.001) == 0.494
    assert not ready()  # nothing configured
    global MODE
    STATE["live"] = False
    # auto mode: sell executes immediately, lands in history
    STATE["holdings"] = {"t": 4.0}
    handle({"asset": "t", "side": "SELL", "price": 0.5, "size": 1000, "title": "Q", "outcome": "Yes"})
    assert STATE["holdings"]["t"] == 0.0
    assert STATE["history"] and STATE["history"][-1]["kind"] == "dry"
    # approve mode: buy queues, then approve executes it
    MODE = "approve"
    handle({"asset": "t2", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q2", "outcome": "No"})
    assert len(STATE["pending"]) == 1 and STATE["holdings"].get("t2") is None
    execute(STATE["pending"].popitem()[1])
    assert STATE["holdings"]["t2"] == 10.0
    MODE = "auto"
    # conviction floor (pinned for the test): his $25 buy is below a $50 floor -> skipped
    globals()["MIN_HIS_NOTIONAL"] = 50.0
    handle({"asset": "t3", "side": "BUY", "price": 0.5, "size": 50, "title": "Q3", "outcome": "Yes"})
    assert "conviction" in STATE["log"][0]["note"] and STATE["holdings"].get("t3") is None
    globals()["MIN_HIS_NOTIONAL"] = 0.0
    # pre-flight: refuse to chase a run-away price
    globals()["midpoint"] = lambda tid: 0.60
    handle({"asset": "t4", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q4", "outcome": "Yes"})
    assert "won't chase" in STATE["log"][0]["note"] and STATE["holdings"].get("t4") is None
    # pre-flight: mid below his fill -> copy at the better (market) price, drift recorded
    globals()["midpoint"] = lambda tid: 0.48
    handle({"asset": "t5", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q5", "outcome": "Yes"})
    assert STATE["holdings"]["t5"] > 0 and STATE["history"][-1]["drift"] == -2.0
    assert STATE["history"][-1]["price"] == limit_price(0.48, "BUY")  # priced off mid, not his fill
    globals()["midpoint"] = lambda tid: None
    # horizon filter: months-out market skipped, soon-ending market copied
    globals()["MAX_DAYS_OUT"] = 2.0
    globals()["market_end_ts"] = lambda tid: time.time() + 30 * 86400
    handle({"asset": "t6", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q6", "outcome": "Yes"})
    assert "horizon" in STATE["log"][0]["note"] and STATE["holdings"].get("t6") is None
    globals()["market_end_ts"] = lambda tid: time.time() + 3600
    handle({"asset": "t7", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q7",
            "outcome": "Yes", "_who": "guyX"})
    assert STATE["holdings"].get("t7") == 10.0
    assert STATE["history"][-1].get("who") == "guyX"   # copies say which trader triggered them
    assert "guyX" in render_dyn()
    # burst fills don't stack: second buy on an already-held market is skipped
    handle({"asset": "t7", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q7", "outcome": "Yes"})
    assert "not stacking" in STATE["log"][0]["note"] and STATE["holdings"]["t7"] == 10.0
    assert "t7" not in INFLIGHT_BUYS  # marker released after the copy landed
    # race guard: a concurrent in-flight copy of the same market blocks the second thread
    INFLIGHT_BUYS.add("t8")
    handle({"asset": "t8", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q8", "outcome": "Yes"})
    assert "not stacking" in STATE["log"][0]["note"] and STATE["holdings"].get("t8") is None
    INFLIGHT_BUYS.discard("t8")
    # a market that just rejected a buy is left alone for a bit, not hammered
    FAILED_BUY_AT["t9"] = time.time()
    handle({"asset": "t9", "side": "BUY", "price": 0.5, "size": 1000, "title": "Q9", "outcome": "Yes"})
    assert "cooling down" in STATE["log"][0]["note"] and STATE["holdings"].get("t9") is None
    FAILED_BUY_AT.clear()
    globals()["MAX_DAYS_OUT"] = 0.0

    # per-match cap: same-event legs share an event key; a third leg is refused,
    # a different match sails through (the Phillies O/U ladder / Egypt basket fix)
    assert event_key("Argentina vs. Egypt: O/U 2.5") == event_key("Argentina vs. Egypt: Team to Advance")
    assert event_key("Will Spain win on 2026-07-10?") != event_key("Spain vs. Belgium: O/U 2.5")
    globals()["MAX_LEGS_PER_EVENT"] = 2
    STATE["holdings"].update({"e1": 3.0, "e2": 2.0})
    STATE["names"]["e1"] = "Philadelphia Phillies vs. Detroit Tigers: O/U 9.5 — Under"
    STATE["names"]["e2"] = "Philadelphia Phillies vs. Detroit Tigers: O/U 8.5 — Under"
    handle({"asset": "e3", "side": "BUY", "price": 0.5, "size": 1000,
            "title": "Philadelphia Phillies vs. Detroit Tigers: O/U 7.5", "outcome": "Under"})
    assert "per-match cap" in STATE["log"][0]["note"] and STATE["holdings"].get("e3") is None
    handle({"asset": "e4", "side": "BUY", "price": 0.5, "size": 1000,
            "title": "Seattle Mariners vs. Tampa Bay Rays: O/U 7.5", "outcome": "Under",
            "_who": "cnyek"})
    assert STATE["holdings"].get("e4", 0) > 0, "different event must pass the cap"
    assert STATE["who"].get("e4") == "cnyek"  # position remembers whose trade it mirrors
    # ...and the wallet card surfaces it next to the position
    STATE["wallet"] = {"usdc": 1.0, "positions": [{"asset": "e4", "title": "Seattle Mariners vs. Tampa Bay Rays: O/U 7.5",
                                                   "outcome": "Under", "size": 2.0, "curPrice": 0.5, "currentValue": 1.0}]}
    assert "via cnyek" in _wallet_card()
    STATE["wallet"] = {}
    for k in ("e1", "e2", "e4"):
        STATE["holdings"].pop(k, None)
        STATE["who"].pop(k, None)
    # resolution frees budget: live win credits, dry win doesn't, loss stays counted
    STATE["holdings"].update({"w1": 2.0, "w2": 3.0, "w3": 4.0})
    STATE["live_cost"] = {"w1": 1.0, "w3": 1.2}
    STATE["spent_live"] = 10.0
    STATE["wallet"] = {"positions": [
        {"asset": "w1", "redeemable": True, "curPrice": 1},    # live copy, WON  -> frees $1 stake
        {"asset": "w2", "redeemable": True, "curPrice": 1},    # dry copy, WON   -> no credit
        {"asset": "w3", "redeemable": True, "curPrice": 0}]}   # live copy, LOST -> no credit
    settle_resolved()
    assert STATE["spent_live"] == 9.0 and not STATE["live_cost"]  # frees w1's $1 cost, not its $2 payout
    assert all(t not in STATE["holdings"] for t in ("w1", "w2", "w3"))
    STATE["wallet"] = {}
    # ghost reconcile: API-invisible holdings settled by chain balance + market outcome
    class _FakeCl:
        def update_balance_allowance(self, p):
            pass
        def get_balance_allowance(self, p):
            return {"balance": 0}
    _states = {"g1": "won", "g2": "lost", "g3": "open"}
    globals()["get_client"], _real_ms = (lambda: _FakeCl()), market_state
    globals()["market_state"] = lambda tid: _states.get(tid)  # unknown tokens: the skip path
    STATE["holdings"].update({"g1": 2.0, "g2": 3.0, "g3": 4.0})
    STATE["live_cost"] = {"g1": 1.0, "g2": 1.1, "g3": 1.2}
    STATE["bought_at"] = {}          # no timestamp = old enough to reconcile
    STATE["spent_live"] = 10.0
    STATE["wallet"] = {"positions": []}  # nothing visible -> all three are ghosts
    reconcile_ghosts()
    # won g1 frees its $1 cost; lost g2 stays; open g3 was a zero-fill -> refund cost (1.2)
    assert STATE["spent_live"] == 7.8, STATE["spent_live"]
    assert not STATE["live_cost"] and all(t not in STATE["holdings"] for t in ("g1", "g2", "g3"))
    # a won ghost frees its own stake; other live positions keep their cost counted
    _states["g4"] = "won"
    STATE["holdings"].update({"g4": 8.0, "gX": 1.0})
    STATE["live_cost"] = {"g4": 5.0, "gX": 4.5}
    STATE["spent_live"] = 9.5
    STATE["wallet"] = {"positions": [{"asset": "gX"}]}   # gX visible, g4 is the ghost
    reconcile_ghosts()
    assert STATE["spent_live"] == 4.5, STATE["spent_live"]  # frees g4's $5 stake; gX's $4.5 stays
    assert "g4" not in STATE["holdings"] and "g4" not in STATE["live_cost"]
    STATE["holdings"].pop("gX", None)
    STATE["live_cost"] = {}
    # odometer reconcile: floor lifts spent_live up to open stakes; when flat it also
    # releases settled-loss cruft (the deadlock fix) — but never while a buy is in flight
    INFLIGHT_BUYS.clear(); STATE["pending"] = {}
    STATE["live_cost"] = {"z1": 2.17}
    STATE["spent_live"] = 0.0
    reconcile_odometer()
    assert STATE["spent_live"] == 2.17, STATE["spent_live"]     # floor lifts the under-count up
    STATE["spent_live"] = 9.99                                  # stale cruft above real risk
    reconcile_odometer()
    assert STATE["spent_live"] == 2.17, STATE["spent_live"]     # released down to money at risk
    STATE["live_cost"] = {}
    reconcile_odometer()
    assert STATE["spent_live"] == 0.0, STATE["spent_live"]      # fully flat -> odometer zero (deadlock cleared)
    STATE["live_cost"] = {"z2": 3.0}; STATE["spent_live"] = 8.0
    INFLIGHT_BUYS.add("zX")
    reconcile_odometer()
    assert STATE["spent_live"] == 8.0, STATE["spent_live"]      # held: a buy is mid-placement, no release
    INFLIGHT_BUYS.discard("zX")
    reconcile_odometer()
    assert STATE["spent_live"] == 3.0, STATE["spent_live"]      # released once the book is flat again
    STATE["live_cost"] = {}
    globals()["market_state"] = _real_ms
    # synth wallet rows: blind-spot holdings appear, but never fool the reconciler
    _mid = midpoint
    globals()["midpoint"] = lambda t: 0.6
    STATE["holdings"]["gs"] = 5.0
    STATE["names"]["gs"] = "Some negRisk market — No"
    _w = {"positions": []}
    _augment_positions(_w)
    srow = _w["positions"][-1]
    assert srow["_synth"] and srow["asset"] == "gs" and srow["currentValue"] == 3.0
    assert srow["title"] == "Some negRisk market" and srow["outcome"] == "No"
    assert "gs" not in {p.get("asset") for p in _w["positions"] if not p.get("_synth")}
    globals()["midpoint"] = _mid
    STATE["holdings"].pop("gs", None)
    STATE["names"].pop("gs", None)
    STATE["wallet"] = {}
    assert any("1 buys" in x for x in copy_stats([{"side": "BUY", "size": 100, "price": 0.5}]))
    a1, a2 = "0x" + "1" * 40, "0x" + "2" * 40
    assert parse_targets(f"{a1}, {a2} garbage 0xshort") == [a1, a2]
    assert valid_addr(a1) and not valid_addr("0xZZ") and not valid_addr("")
    assert key_wallet("0x" + "11" * 32).startswith("0x")  # any 32-byte scalar is a key
    assert key_wallet("junk") == "" and key_wallet("") == ""
    # claude copilot: action-line parsing + whitelisted application
    reply, acts = parse_actions('The bot is fine.\nACTIONS: [{"op":"mode","value":"approve"},{"op":"cap","value":3}]')
    assert reply == "The bot is fine." and len(acts) == 2
    assert set(apply_actions(acts)) == {"mode=approve", "cap=3"}
    assert MODE == "approve" and MAX_USDC_PER_TRADE == 3.0
    MODE, globals()["MAX_USDC_PER_TRADE"] = "auto", 5.0
    assert apply_actions([{"op": "rm -rf", "value": 1}, "junk"]) == []  # non-whitelisted ignored
    assert parse_actions("no control line at all")[1] == []
    assert json.dumps(bot_context())  # snapshot is JSON-serializable
    # hard spend cap arithmetic
    STATE["spent_live"], globals()["SPEND_CAP"] = 19.50, 20.0
    assert not over_budget(0.49) and over_budget(0.51)
    STATE["spent_live"] = 0.0
    # auto budget: cap follows wallet total minus reserve; 0 disables it
    STATE["wallet"] = {"usdc": 50.0, "positions": [{"currentValue": 3.0}]}
    globals()["AUTO_CAP_RESERVE"], globals()["AUTO_TRADE_PCT"] = 8.0, 10.0
    auto_cap()
    assert SPEND_CAP == 45.0
    assert MAX_USDC_PER_TRADE == 5.3               # 10% of $53 wallet
    STATE["wallet"] = {"usdc": 20.0, "positions": []}
    auto_cap()
    assert MAX_USDC_PER_TRADE == 5.0               # small wallet -> $5 floor holds
    globals()["AUTO_CAP_RESERVE"], globals()["SPEND_CAP"] = 0.0, 20.0
    globals()["AUTO_TRADE_PCT"], globals()["MAX_USDC_PER_TRADE"] = 0.0, 5.0
    auto_cap()
    assert SPEND_CAP == 20.0 and MAX_USDC_PER_TRADE == 5.0   # disabled -> untouched
    STATE["wallet"] = {"usdc": None}
    globals()["AUTO_CAP_RESERVE"], globals()["SPEND_CAP"] = 8.0, 20.0
    auto_cap()
    assert SPEND_CAP == 20.0                       # unreadable chain -> keep last cap
    globals()["AUTO_CAP_RESERVE"], globals()["AUTO_TRADE_PCT"] = 8.0, 10.0
    globals()["MAX_USDC_PER_TRADE"] = 5.0
    STATE["wallet"] = {}
    # wallet sell button: renders per live position, DRY mode refuses politely
    STATE["wallet"] = {"positions": [{"asset": "tok9", "title": "T9", "outcome": "Yes",
                                      "size": 10, "avgPrice": 0.5, "curPrice": 0.5,
                                      "currentValue": 5.0, "cashPnl": 0, "percentPnl": 0}]}
    assert "/sellpos" in _wallet_card() and 'value="tok9"' in _wallet_card()
    STATE["live"] = False
    sell_position("tok9")
    assert "DRY mode" in STATE["log"][0]["note"]
    STATE["wallet"] = {}
    page = render()
    assert "copybot" in page and "Settings" in page and "leaderboard" in page and "Trade history" in page
    assert "capnote" in page and "id=acr" in page  # manual cap dims while auto budget is on
    assert "Ask Claude" in page and "action=/ask" in page
    STATE["missing_deps"] = ["regex"]  # banner surfaces missing runtime deps
    assert "Missing Python modules" in render_dyn() and "pip install regex" in render_dyn()
    # a transient import failure heals: the quiet re-check clears the flag
    # (all deps exist in the test env), which is what lets bot_loop re-arm
    assert check_deps(log=False) == [] and STATE["missing_deps"] == []
    # chat persistence round-trips through disk
    chat_add("you", "does the chat save?")
    STATE["chat"].clear()
    load_chat()
    assert any(m["text"] == "does the chat save?" for m in STATE["chat"])
    CHAT_FILE.unlink(missing_ok=True)
    dyn = render_dyn()
    assert "Status" in dyn and "Test connection" in dyn and "❌" in dyn  # unconfigured -> red rows
    assert "Your wallet" in dyn and "no open positions on-chain" in dyn
    import datetime as _dt
    _soon = (_dt.date.today() + _dt.timedelta(days=17)).isoformat()
    STATE["wallet"] = {"usdc": 28.5, "checked": "0x" + "a" * 40,
                       "positions": [{"title": "Q", "outcome": "Yes", "size": 10, "avgPrice": 0.5,
                                      "curPrice": 0.6, "currentValue": 6.0, "cashPnl": 1.0,
                                      "percentPnl": 20.0, "endDate": _soon},
                                     {"title": "dead", "outcome": "No", "size": 5, "currentValue": 0.0,
                                      "cashPnl": -3.0, "redeemable": True, "endDate": "2026-06-23"}]}
    card = _wallet_card()
    assert "$28.50 cash (pUSD)" in card and "$34.50 total" in card
    assert "17d left" in card and "(+20.0%)" in card          # countdown + pnl%
    assert "1 settled/worthless positions hidden" in card      # dust collapsed
    STATE["wallet"] = {"usdc": 0.0, "checked": "0x" + "b" * 40, "positions": []}
    assert "empty on-chain" in _wallet_card()  # wrong-wallet warning fires

    # copy_missed: only a real feed BUY replays, and through the normal handle()
    calls = []
    _oh = handle
    globals()["handle"] = lambda t: calls.append(t["asset"])
    STATE["target_feed"] = [
        {"transactionHash": "0xh1", "asset": "tokS", "side": "SELL", "price": 0.5, "size": 9},
        {"transactionHash": "0xh2", "asset": "tokB", "side": "BUY", "price": 0.5, "size": 9}]
    copy_missed("0xh1:tokS:SELL")
    copy_missed("0xh2:tokB:BUY")
    copy_missed("bogus")
    assert "scrolled out" in STATE["log"][0]["note"]  # a missed key is never silent
    globals()["handle"] = _oh
    assert calls == ["tokB"], calls
    assert "/copymiss" in _target_rows()   # un-held BUY row gets its copy button
    STATE["holdings"]["tokB"] = 5.0
    assert "/copymiss" not in _target_rows()  # held -> button gone (no stacking)
    del STATE["holdings"]["tokB"]

    # fetch_trades_backfilled: pages back until it reaches a seen trade, so a burst
    # bigger than one page can't slip past (the LoL-surge gap). Stub the pager to
    # yield a full, all-distinct page for every offset (an unbounded burst).
    _oft = fetch_trades
    globals()["fetch_trades"] = lambda u, limit=100, offset=0: \
        [{"transactionHash": f"0x{offset + i}", "asset": "a", "side": "BUY"} for i in range(limit)]
    got = fetch_trades_backfilled("u", {"0x150:a:BUY"}, page=100, cap=600)  # seen sits in page 2
    assert len(got) == 200 and any(key(t) == "0x150:a:BUY" for t in got)  # walked past page 1, then stopped
    # nothing ever seen (fresh target): the cap bounds the walk, no infinite paging
    assert len(fetch_trades_backfilled("u", set(), page=100, cap=300)) == 300
    globals()["fetch_trades"] = _oft

    # copy-all: dedupes sprayed assets, skips held rows and sells, replays the rest
    calls = []
    _oh = handle
    globals()["handle"] = lambda t: calls.append(t["asset"])
    STATE["holdings"]["tokHeld"] = 5.0
    STATE["target_feed"] = [
        {"transactionHash": "0xa", "asset": "tokN1", "side": "BUY", "price": 0.5, "size": 9},
        {"transactionHash": "0xb", "asset": "tokN1", "side": "BUY", "price": 0.5, "size": 9},
        {"transactionHash": "0xc", "asset": "tokHeld", "side": "BUY", "price": 0.5, "size": 9},
        {"transactionHash": "0xd", "asset": "tokN2", "side": "SELL", "price": 0.5, "size": 9}]
    copy_all_missed()
    globals()["handle"] = _oh
    assert calls == ["tokN1"], calls
    del STATE["holdings"]["tokHeld"]

    # green tint marks exactly the rows that would copy: open market inside horizon
    _mdo = MAX_DAYS_OUT
    globals()["MAX_DAYS_OUT"] = 2.0
    nowt = time.time()
    STATE["target_feed"] = [
        {"transactionHash": "0x1", "asset": "tokIn", "side": "BUY", "price": 0.5, "size": 9, "title": "T-in"},
        {"transactionHash": "0x2", "asset": "tokFar", "side": "BUY", "price": 0.5, "size": 9, "title": "T-far"},
        {"transactionHash": "0x3", "asset": "tokEnded", "side": "BUY", "price": 0.5, "size": 9, "title": "T-end"},
        {"transactionHash": "0x4", "asset": "tokIn", "side": "SELL", "price": 0.5, "size": 9, "title": "T-sell"}]
    ENDS_CACHE.update({"tokIn": nowt + 3600, "tokFar": nowt + 90 * 86400, "tokEnded": nowt - 3600})
    chunks = _target_rows().split("<tr")[1:]
    tinted = [i for i, c in enumerate(chunks) if "rgba(74,222,128" in c]
    assert tinted == [0], tinted  # only the in-horizon open BUY; far/ended/sell stay plain
    globals()["MAX_DAYS_OUT"] = _mdo

    # trader-scout math: edge bounds, friction crossings, survival screen, card render
    lo, hi = _net_edge(pnl7=100_000, d30=150_000, vol7=400_000, sell_ratio=0.5)
    assert (lo, hi) == (0.053, 0.2155), (lo, hi)   # e30=8.75%, e7=25%, minus 1.5 crossings
    assert _net_edge(0, 0, 0, 0) is None            # no volume, no verdict
    assert _curve_screen([0, 5e3, 9e3, 12e3, 15e3, 17e3, 19e3, 20e3])["d30"] == 20000
    assert _curve_screen([0, 20e3, 1e3, 2e3, 3e3, 4e3, 5e3, 6e3]) is None   # 95% drawdown
    assert _curve_screen([0, 1e3]) is None                                   # too short
    SCOUT["rows"] = [{"addr": "0x" + "9" * 40, "name": "TestGuy", "d30": 50000, "green": 60,
                      "mdd": 100, "pnl7": 9000, "vol7": 80000, "od": 12.0, "sell": 5,
                      "short": 95, "net": (0.05, 0.2), "verdict": "PASS"}]
    card = _scout_card()
    assert "TestGuy" in card and "+5% … +20%" in card and "/target" in card and "PASS" in card
    SCOUT["rows"] = []

    # the settings form must NEVER echo the stored key back into served HTML
    globals()["PRIVATE_KEY_MEM"] = "0x" + "ab" * 32
    try:
        assert ("ab" * 32) not in _settings_form(), "private key leaked into the page!"
    finally:
        globals()["PRIVATE_KEY_MEM"] = None

    # ledger audit: a mislabeled ghost gets rewritten and a lost cost re-counted
    STATE["history"] = [{"side": "GHOST", "kind": "skip", "name": LEDGER_AUDIT_0707[0][0],
                         "note": "buy never filled on-chain — $3.81 refunded to budget"},
                        {"side": "GHOST", "kind": "skip", "name": "Some Other Market — Yes",
                         "note": "buy never filled on-chain — $5.00 refunded to budget"}]
    STATE["spent_live"] = 1.0
    assert audit_ledger_0707() == 1
    assert STATE["spent_live"] == 4.81, "lost cost not re-counted into the odometer"
    assert "resolved LOST" in STATE["history"][0]["note"]
    assert "never filled" in STATE["history"][1]["note"]  # untouched: not on the audit list
    assert audit_ledger_0707() == 0  # idempotent

    # headless guard: the flag exists and defaults off; /kill honors it (VPS = systemd-owned)
    assert HEADLESS is False  # desktop default; --headless flips it in __main__
    src = _order.__globals__["Handler"].do_POST.__code__.co_consts
    assert any(isinstance(c, str) and "systemctl stop copybot" in c for c in src), \
        "/kill headless guard missing"
    print("self-check OK")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
        sys.exit()
    HEADLESS = "--headless" in sys.argv
    url = f"http://127.0.0.1:{PORT}"
    ThreadingHTTPServer.allow_reuse_address = False  # else two instances share the port on Windows
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        server = None  # already running — just open a window on it
    if server:
        load_config()
        load_state()
        load_chat()
        check_deps()
        threading.Thread(target=bot_loop, daemon=True).start()
        threading.Thread(target=ws_loop, daemon=True).start()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"copybot: {url}")
    if "--headless" in sys.argv:  # service/VPS mode: engine + HTTP UI, no window
        if not server:
            sys.exit("copybot: port in use — another instance is already running")
        while True:
            time.sleep(3600)
    try:
        import webview  # native desktop window (Windows WebView2)
        webview.create_window("Copybot", url, width=1180, height=920)
        webview.start()          # returns when the window is closed
        os._exit(0)              # window closed = app quits, bot stops
    except ImportError:
        webbrowser.open(url)     # fallback: browser tab
        if server:
            while True:
                time.sleep(3600)
