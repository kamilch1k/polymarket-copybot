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
  pip install py-clob-client requests
RUN
  python copybot.py --check     offline self-check
  python copybot.py             start app; browser opens; configure in the page
"""
import json
import math
import os
import re
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
MIN_HIS_NOTIONAL = 50.0       # copy his BUY only if he put >= this many $ in (skip his dust)
MODE = "auto"                 # "auto" = copy instantly · "approve" = queue for your click
POLL_SECONDS = 15             # REST polling is the fallback; WebSocket is the fast path
LEADERS_EVERY = 20            # refresh leaderboard every N polls (~5 min)
SIGNATURE_TYPE = 1            # 1 = email/magic login, 2 = browser wallet
PORT = 8777
PRIVATE_KEY_MEM = None        # persisted to config by owner's choice (single-user PC)
CONFIG_FILE = Path(__file__).with_name("copybot_config.json")
STATE_FILE = Path(__file__).with_name("copybot_state.json")
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
WS_URL = "wss://ws-live-data.polymarket.com"  # real-time platform activity stream
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
}


# ---- pure logic (unit-tested in _check) -------------------------------------
def my_buy_size(his_size, price, fraction=None, cap=None):
    """Shares to buy to mirror his_size at `price`. 0 = below exchange minimum.
    Defaults read the live globals so UI edits take effect immediately."""
    fraction = COPY_FRACTION if fraction is None else fraction
    cap = MAX_USDC_PER_TRADE if cap is None else cap
    notional = min(his_size * price * fraction, cap)
    if notional < MIN_NOTIONAL:
        return 0.0
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


def copy_stats(feed):
    trades = [t for t in feed if t.get("side")]
    if not trades:
        return ["waiting for his activity…"]
    notionals = [float(t["size"]) * float(t["price"]) for t in trades]
    avg = sum(notionals) / len(notionals)
    buys = sum(1 for t in trades if t["side"].upper() == "BUY")
    per_copy = min(avg * COPY_FRACTION, MAX_USDC_PER_TRADE)
    lines = [
        f"his last {len(trades)} trades: {buys} buys / {len(trades) - buys} sells",
        f"his avg trade ≈ ${avg:,.0f}",
        f"you copy {COPY_FRACTION * 100:.1f}% capped ${MAX_USDC_PER_TRADE:.0f} → ≈ ${per_copy:.2f}/copy",
        f"bankroll ${BANKROLL:.0f} → room for ~{int(BANKROLL / max(per_copy, 0.01))} concurrent copies",
    ]
    if avg * COPY_FRACTION > MAX_USDC_PER_TRADE:
        lines.append("⚠ his size is big — your $ cap binds, so copies are flat, not proportional. "
                     "Raise the cap or drop the fraction to track him faithfully.")
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
        PRIVATE_KEY_MEM, MIN_HIS_NOTIONAL
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
    STATE["funder"] = c.get("funder", "")


def save_config():
    CONFIG_FILE.write_text(json.dumps({  # includes the key: owner's choice, file is gitignored
        "targets": TARGETS, "funder": STATE.get("funder", ""), "fraction": COPY_FRACTION,
        "cap": MAX_USDC_PER_TRADE, "slippage": SLIPPAGE, "bankroll": BANKROLL,
        "mode": MODE, "min_his": MIN_HIS_NOTIONAL,
        "private_key": PRIVATE_KEY_MEM or ""}, indent=2))


def load_state():
    if not STATE_FILE.exists():
        return
    s = json.loads(STATE_FILE.read_text())
    STATE["seen"] = set(s.get("seen", []))
    STATE["holdings"] = s.get("holdings", {})
    STATE["names"] = s.get("names", {})
    STATE["history"] = s.get("history", [])


def save_state():
    with LOCK:
        data = {"seen": sorted(STATE["seen"]), "holdings": STATE["holdings"],
                "names": STATE["names"], "history": STATE["history"][-500:]}
    STATE_FILE.write_text(json.dumps(data))


def logline(**e):
    e["t"] = time.strftime("%H:%M:%S")
    with LOCK:
        STATE["log"].appendleft(e)


# ---- polymarket api ---------------------------------------------------------
def fetch_trades(user):
    r = requests.get(f"{DATA_API}/activity",
                     params={"user": user, "type": "TRADE", "limit": 100}, timeout=15)
    r.raise_for_status()
    return r.json()


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
    from py_clob_client.client import ClobClient
    k = PRIVATE_KEY_MEM or os.environ.get("PM_PRIVATE_KEY")
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER")
    if not k or not funder:
        raise RuntimeError("set private key and funder in Settings")
    c = ClobClient(CLOB_HOST, key=k, chain_id=137, signature_type=SIGNATURE_TYPE, funder=funder)
    c.set_api_creds(c.create_or_derive_api_creds())
    return c


# ---- trading ----------------------------------------------------------------
CLIENT = None


def get_client():
    global CLIENT
    if CLIENT is None:
        CLIENT = make_client()
    return CLIENT


def _order(tid, side, shares, ref, name, drift=None):
    price = limit_price(ref, side, tick_of(tid))
    with LOCK:
        live = STATE["live"]
        STATE["copies"] += 1
    kind, note = "dry", ""
    if live:
        from py_clob_client.clob_types import OrderArgs, OrderType
        try:
            cl = get_client()
            signed = cl.create_order(OrderArgs(token_id=tid, price=price, size=shares, side=side))
            # FAK = fill what's there right now, cancel the rest — an order that
            # misses must die, not rest in the book and fill later at a stale price
            note = str(cl.post_order(signed, OrderType.FAK))[:80]
            kind = "live"
        except Exception as ex:
            kind, note = "error", str(ex)[:140]
    e = {"d": time.strftime("%Y-%m-%d"), "t": time.strftime("%H:%M:%S"), "kind": kind,
         "side": side, "name": name, "shares": shares, "price": price, "note": note,
         "drift": drift}
    with LOCK:
        STATE["log"].appendleft(e)
        STATE["history"].append(e)
    return kind != "error"


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
    if _order(tid, side, shares, it["ref"], it["name"], it.get("drift")):
        with LOCK:
            delta = shares if side == "BUY" else -shares
            STATE["holdings"][tid] = round(STATE["holdings"].get(tid, 0.0) + delta, 2)
    save_state()


def submit(it):
    """Route an intent: execute now (auto) or queue for your click (approve)."""
    if MODE == "approve":
        with LOCK:
            STATE["pid"] += 1
            it["id"] = str(STATE["pid"])
            it["t"] = time.strftime("%H:%M:%S")
            STATE["pending"][it["id"]] = it
        logline(kind="pend", side=it["side"], name=it["name"], shares=it["shares"],
                price=limit_price(it["ref"], it["side"]), note="awaiting approval")
    else:
        execute(it)


def handle(trade):
    tid = trade["asset"]
    side = trade["side"].upper()
    price = float(trade["price"])
    his = float(trade["size"])
    name = f"{trade.get('title', '?')} — {trade.get('outcome', '?')}"
    with LOCK:
        STATE["names"][tid] = name
    if side == "BUY":
        if his * price < MIN_HIS_NOTIONAL:
            logline(kind="skip", side="BUY", name=name,
                    note=f"his ${his * price:,.0f} < ${MIN_HIS_NOTIONAL:,.0f} conviction floor")
            return
        shares = my_buy_size(his, price)
        if shares <= 0:
            logline(kind="skip", side="BUY", name=name, note=f"< ${MIN_NOTIONAL} min")
            return
    else:
        with LOCK:
            held = STATE["holdings"].get(tid, 0.0)
        shares = min(held, round(his * COPY_FRACTION, 2))
        if shares <= 0 or shares * price < MIN_NOTIONAL:
            logline(kind="skip", side="SELL", name=name, note="nothing copied / dust")
            return

    # pre-flight: check where the market is NOW, not where it was when he traded
    mid = midpoint(tid)
    drift = None
    if mid:
        drift = round(((mid - price) if side == "BUY" else (price - mid)) * 100, 2)
        if side == "BUY" and mid > price * (1 + SLIPPAGE):
            logline(kind="skip", side="BUY", name=name,
                    note=f"won't chase: mid {mid:.3f} already ran past his {price:.3f}+slippage")
            return
        ref = min(price, mid) if side == "BUY" else mid  # never pay above market / always exit at market
    else:
        ref = price
    submit({"tid": tid, "side": side, "shares": shares, "ref": ref, "name": name, "drift": drift})


def test_trade():
    """Real-money end-to-end proof: buy ~$1 of the most recent market a target
    touched, then immediately sell it back. Costs a few cents of spread.
    ponytail: $0.10 isn't possible — Polymarket rejects orders under $1 notional."""
    try:
        cl = get_client()
        with LOCK:
            feed = list(STATE["target_feed"])
        t = next((x for x in feed if x.get("side")), None)
        if not t:
            logline(kind="error", note="test trade: no target activity seen yet — wait for a poll")
            return
        tid, ref = t["asset"], float(t["price"])
        name = f"TEST · {t.get('title', '?')}"
        from py_clob_client.clob_types import OrderArgs, OrderType
        tick = tick_of(tid)
        buy_px = limit_price(ref, "BUY", tick)
        shares = round(MIN_NOTIONAL * 1.1 / buy_px, 2)
        r = cl.post_order(cl.create_order(OrderArgs(token_id=tid, price=buy_px, size=shares, side="BUY")),
                          OrderType.FAK)
        logline(kind="live", side="BUY", name=name, shares=shares, price=buy_px, note=str(r)[:70])
        time.sleep(3)  # let the buy settle before selling it back
        sell_px = limit_price(ref, "SELL", tick)
        r = cl.post_order(cl.create_order(OrderArgs(token_id=tid, price=sell_px, size=shares, side="SELL")),
                          OrderType.FAK)
        logline(kind="live", side="SELL", name=name, shares=shares, price=sell_px, note=str(r)[:70])
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
        logline(kind="live", note=f"auto-live: trading enabled ({source})")
    except Exception as ex:
        logline(kind="error", note=f"auto-live failed ({source}): {str(ex)[:120]}")


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
        if polls % LEADERS_EVERY == 0:  # leaderboard loads even before a target is set
            leaders = fetch_leaders()
            if leaders:
                with LOCK:
                    STATE["leaders"] = leaders
        polls += 1

        if not TARGETS:
            time.sleep(POLL_SECONDS)
            continue

        merged = []
        for target in list(TARGETS):
            try:
                trades = fetch_trades(target)
                with LOCK:
                    STATE["error"] = ""
                    STATE["last_poll"] = time.time()
            except Exception as ex:
                with LOCK:
                    STATE["error"] = f"{target[:8]}…: {ex}"
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
        time.sleep(POLL_SECONDS)


# ---- dashboard --------------------------------------------------------------
KIND_COLOR = {"live": "#4ade80", "dry": "#93c5fd", "skip": "#9ca3af",
              "error": "#f87171", "pend": "#fbbf24"}


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
                 f'<td>{it["name"]}</td><td class=r>{it["shares"]:g} @ {px}</td><td>'
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
                f'<td>{e.get("name", "")}</td><td class=r>{e.get("shares", "")} @ {e.get("price", "")}</td>'
                f'<td class=dim>{e.get("note", "")}</td></tr>')
    return out or "<tr><td colspan=6 class=dim>no trades yet</td></tr>"


def _target_rows():
    with LOCK:
        feed = list(STATE["target_feed"])[:15]
    out = ""
    for t in feed:
        side = str(t.get("side", "")).upper()
        col = "#4ade80" if side == "BUY" else "#fca5a5"
        name = f"{t.get('title', '?')} — {t.get('outcome', '?')}"
        out += (f'<tr><td class=dim>{t.get("_who", "")}</td><td style=color:{col}>{side}</td><td>{name}</td>'
                f'<td class=r>{float(t.get("size", 0)):g}</td><td class=r>@{t.get("price", "")}</td></tr>')
    return out or "<tr><td colspan=5 class=dim>no recent activity</td></tr>"


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
  <label>Private key (saved to copybot_config.json on this PC){key_note}<input name=private_key value="{pk_val}" {_vstyle(pk_val, bool(signer))} autocomplete=off placeholder="0x…"></label>
  <label>Mode<select name=mode>
    <option value=auto {"selected" if MODE == "auto" else ""}>auto — copy instantly</option>
    <option value=approve {"selected" if MODE == "approve" else ""}>approve — I click ✓ per trade</option>
  </select></label>
  <label>Copy fraction<input name=fraction value="{COPY_FRACTION}"></label>
  <label>Max $ / trade<input name=cap value="{MAX_USDC_PER_TRADE}"></label>
  <label>Copy his BUY only if ≥ $<input name=min_his value="{MIN_HIS_NOTIONAL}"></label>
  <label>Slippage<input name=slippage value="{SLIPPAGE}"></label>
  <label>Bankroll $<input name=bankroll value="{BANKROLL}"></label>
  <button class=save type=submit>Save</button>
</form>
</div>"""


def render_dyn():
    """Everything that changes — swapped into the page in place every 3s.
    The settings form lives OUTSIDE this, so refresh can never eat your input."""
    with LOCK:
        live, copies, err = STATE["live"], STATE["copies"], STATE["error"]
        started, last = STATE["started"], STATE["last_poll"]
        holdings = [(STATE["names"].get(k, k[:16]), v) for k, v in STATE["holdings"].items() if v > 0]
        tnames = dict(STATE["tnames"])
        feed = list(STATE["target_feed"])
        log = list(STATE["log"])
    up = int(time.time() - started)
    age = "never" if not last else f"{int(time.time() - last)}s ago"
    pill = ('<span style="background:#166534;color:#4ade80">● LIVE</span>' if live
            else '<span style="background:#374151;color:#93c5fd">◦ DRY (watch-only)</span>')
    if live:
        toggle = '<form method=post action=/dry style=display:inline><button class=dry>Go DRY</button></form>'
    elif ready():
        toggle = '<form method=post action=/live style=display:inline><button class=go>Go LIVE ▶</button></form>'
    else:
        toggle = '<span class="tag dim">configure ⚙ to enable live</span>'

    hrows = "".join(f"<tr><td>{n}</td><td class=r>{s:g}</td></tr>" for n, s in holdings) \
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
                  f'<td>{e.get("side", "")}</td><td>{e.get("name", "")}</td>'
                  f'<td class=r>{e.get("shares", "")} {px}</td><td class=dim>{e.get("note", "")}</td></tr>')
    lrows = lrows or "<tr><td colspan=6 class=dim>waiting…</td></tr>"
    stats = "".join(f"<li>{s}</li>" for s in copy_stats(feed))
    errbar = f'<div class=err>{err}</div>' if err else ""
    tlabel = ", ".join(tnames.get(a, a[:8] + "…") for a in TARGETS) or "none set"
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER", "")
    funder_chip = f'<span class="tag dim">funder {funder}</span>' if funder else ""

    return f"""<div class=bar>
  <span class=pill>{pill}</span>
  <span class="tag dim">target {tlabel}</span>
  {funder_chip}
  <span class="tag dim">copies {copies}</span>
  <span class="tag dim">up {up // 3600}h{up % 3600 // 60}m</span>
  <span class="tag dim">polled {age}</span>
  {toggle}
  <form method=post action=/kill style=display:inline onsubmit="return confirm('Kill the bot?')"><button class=kill>Kill</button></form>
</div>
{errbar}
{_status_card()}
{_pending_card()}
<div class=grid>
  <div class=card>
    <h2>Bot — holding</h2>
    <table><tr><th>market — outcome</th><th class=r>shares</th></tr>{hrows}</table>
    <h2>Bot — wants to buy (DRY intents)</h2>
    <table><tr><th>time</th><th>market — outcome</th><th class=r>size</th></tr>{irows}</table>
  </div>
  <div class=card>
    <h2>Targets — live activity</h2>
    <table><tr><th>trader</th><th>side</th><th>market — outcome</th><th class=r>size</th><th class=r>px</th></tr>{_target_rows()}</table>
    <h2>How to copy him best</h2>
    <ul class=tips>{stats}</ul>
  </div>
</div>
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
<div id=dyn>{render_dyn()}</div>
<script>
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
                    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
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
            SLIPPAGE = num("slippage", SLIPPAGE)
            BANKROLL = num("bankroll", BANKROLL)
            save_config()
            try_go_live("settings saved")  # fully configured -> start trading immediately
        elif path == "/kill":
            self._send(200, "bye")
            os._exit(0)  # ponytail: abrupt, but it's a side-project button
        self._redirect()

    def log_message(self, *a):
        pass


# ---- entry ------------------------------------------------------------------
def _check():
    globals()["midpoint"] = lambda tid: None  # offline: no live mid / tick lookups
    globals()["tick_of"] = lambda tid: 0.01
    assert my_buy_size(1000, 0.50, 0.01, 50) == 10.0
    assert my_buy_size(1000, 0.50, 0.01, 3) == 6.0
    assert my_buy_size(10, 0.50, 0.01, 50) == 0.0
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
    # conviction floor: his $25 buy is dust, skipped
    handle({"asset": "t3", "side": "BUY", "price": 0.5, "size": 50, "title": "Q3", "outcome": "Yes"})
    assert "conviction" in STATE["log"][0]["note"] and STATE["holdings"].get("t3") is None
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
    assert any("1 buys" in x for x in copy_stats([{"side": "BUY", "size": 100, "price": 0.5}]))
    a1, a2 = "0x" + "1" * 40, "0x" + "2" * 40
    assert parse_targets(f"{a1}, {a2} garbage 0xshort") == [a1, a2]
    assert valid_addr(a1) and not valid_addr("0xZZ") and not valid_addr("")
    assert key_wallet("0x" + "11" * 32).startswith("0x")  # any 32-byte scalar is a key
    assert key_wallet("junk") == "" and key_wallet("") == ""
    html = render()
    assert "copybot" in html and "Settings" in html and "leaderboard" in html and "Trade history" in html
    dyn = render_dyn()
    assert "Status" in dyn and "Test connection" in dyn and "❌" in dyn  # unconfigured -> red rows
    print("self-check OK")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
        sys.exit()
    url = f"http://127.0.0.1:{PORT}"
    ThreadingHTTPServer.allow_reuse_address = False  # else two instances share the port on Windows
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        server = None  # already running — just open a window on it
    if server:
        load_config()
        load_state()
        threading.Thread(target=bot_loop, daemon=True).start()
        threading.Thread(target=ws_loop, daemon=True).start()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"copybot: {url}")
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
