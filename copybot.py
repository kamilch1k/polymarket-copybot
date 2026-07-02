#!/usr/bin/env python3
"""
Polymarket copy-trading bot + local dashboard app.

Run it, a browser opens http://127.0.0.1:8777 . Configure everything in the page
(⚙ Settings): target wallet, your funder wallet, private key, and sizing. Then
click Go LIVE. It shows what the bot holds / wants to buy, your target's live
activity + how to copy him best, and a leaderboard of traders to one-click copy.

Localhost-only on purpose: the page has a live-trade switch and a kill button.

SECURITY: target/funder/sizing persist to copybot_config.json. Your PRIVATE KEY
is kept in memory only — never written to disk — so re-enter it after a restart.

SETUP
  pip install py-clob-client requests
RUN
  python copybot.py --check     offline self-check
  python copybot.py             start app; browser opens; configure in the page
"""
import json
import os
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
TARGET = ""                   # his proxy wallet — set it in the page
BANKROLL = 30.0               # your total stake, for sizing hints only
COPY_FRACTION = 0.01          # copy 1% of his share size
MAX_USDC_PER_TRADE = 5.0      # per-copy notional cap
MIN_NOTIONAL = 1.0            # Polymarket rejects orders under ~$1
SLIPPAGE = 0.02              # accept up to this much worse than his fill
MODE = "auto"                 # "auto" = copy instantly · "approve" = queue for your click
POLL_SECONDS = 15
LEADERS_EVERY = 20            # refresh leaderboard every N polls (~5 min)
SIGNATURE_TYPE = 1            # 1 = email/magic login, 2 = browser wallet
PORT = 8777
PRIVATE_KEY_MEM = None        # in-memory only, never persisted
CONFIG_FILE = Path(__file__).with_name("copybot_config.json")
STATE_FILE = Path(__file__).with_name("copybot_state.json")
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
# -----------------------------------------------------------------------------

LOCK = threading.Lock()
STATE = {
    "live": False, "holdings": {}, "names": {}, "seen": set(), "baselined": set(),
    "log": deque(maxlen=200), "copies": 0, "started": time.time(), "last_poll": 0.0,
    "error": "", "target_name": "", "target_feed": [], "leaders": [],
    "funder": "", "pk_set": False,
    "pending": {}, "pid": 0,     # trades awaiting approval (approve mode)
    "history": [],               # every executed copy, persisted
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


def limit_price(ref_price, side):
    """Marketable limit: cross the book by SLIPPAGE so the copy fills.
    ponytail: whole-cent rounding; sub-cent markets need client.get_tick_size()."""
    if side == "BUY":
        return min(0.99, round(ref_price * (1 + SLIPPAGE), 2))
    return max(0.01, round(ref_price * (1 - SLIPPAGE), 2))


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
    return lines


def ready():
    key_ = PRIVATE_KEY_MEM or os.environ.get("PM_PRIVATE_KEY")
    funder = STATE.get("funder") or os.environ.get("PM_FUNDER")
    return bool(TARGET and key_ and funder)


# ---- persistence ------------------------------------------------------------
def load_config():
    global TARGET, COPY_FRACTION, MAX_USDC_PER_TRADE, SLIPPAGE, BANKROLL, MODE
    if not CONFIG_FILE.exists():
        return
    c = json.loads(CONFIG_FILE.read_text())
    TARGET = c.get("target", TARGET)
    MODE = c.get("mode", MODE)
    COPY_FRACTION = c.get("fraction", COPY_FRACTION)
    MAX_USDC_PER_TRADE = c.get("cap", MAX_USDC_PER_TRADE)
    SLIPPAGE = c.get("slippage", SLIPPAGE)
    BANKROLL = c.get("bankroll", BANKROLL)
    STATE["funder"] = c.get("funder", "")


def save_config():
    CONFIG_FILE.write_text(json.dumps({  # no private key here, on purpose
        "target": TARGET, "funder": STATE.get("funder", ""), "fraction": COPY_FRACTION,
        "cap": MAX_USDC_PER_TRADE, "slippage": SLIPPAGE, "bankroll": BANKROLL,
        "mode": MODE}, indent=2))


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


def _order(tid, side, shares, ref, name):
    price = limit_price(ref, side)
    with LOCK:
        live = STATE["live"]
        STATE["copies"] += 1
    kind, note = "dry", ""
    if live:
        from py_clob_client.clob_types import OrderArgs, OrderType
        try:
            cl = get_client()
            signed = cl.create_order(OrderArgs(token_id=tid, price=price, size=shares, side=side))
            note = str(cl.post_order(signed, OrderType.GTC))[:80]
            kind = "live"
        except Exception as ex:
            kind, note = "error", str(ex)[:140]
    e = {"d": time.strftime("%Y-%m-%d"), "t": time.strftime("%H:%M:%S"), "kind": kind,
         "side": side, "name": name, "shares": shares, "price": price, "note": note}
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
    if _order(tid, side, shares, it["ref"], it["name"]):
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
    submit({"tid": tid, "side": side, "shares": shares, "ref": price, "name": name})


def bot_loop():
    polls = 0
    while True:
        if polls % LEADERS_EVERY == 0:  # leaderboard loads even before a target is set
            leaders = fetch_leaders()
            if leaders:
                with LOCK:
                    STATE["leaders"] = leaders
        polls += 1

        target = TARGET
        if not target:
            time.sleep(POLL_SECONDS)
            continue

        try:
            trades = fetch_trades(target)
            with LOCK:
                STATE["error"] = ""
                STATE["last_poll"] = time.time()
                STATE["target_feed"] = trades[:25]
        except Exception as ex:
            with LOCK:
                STATE["error"] = str(ex)
            time.sleep(POLL_SECONDS)
            continue

        with LOCK:
            first_time = target not in STATE["baselined"]
        if first_time:
            with LOCK:
                for t in trades:
                    STATE["seen"].add(key(t))
                STATE["baselined"].add(target)
                STATE["target_name"] = fetch_name(target) or STATE["target_name"]
            logline(kind="skip", note=f"baselined {len(trades)} past trades — watching from now")
        else:
            for t in reversed(trades):
                k = key(t)
                with LOCK:
                    fresh = k not in STATE["seen"]
                if fresh:
                    handle(t)
                    with LOCK:
                        STATE["seen"].add(k)
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
        out += (f'<tr><td style=color:{col}>{side}</td><td>{name}</td>'
                f'<td class=r>{float(t.get("size", 0)):g}</td><td class=r>@{t.get("price", "")}</td></tr>')
    return out or "<tr><td colspan=4 class=dim>no recent activity</td></tr>"


def _leader_rows():
    with LOCK:
        leaders = list(STATE["leaders"])
        cur = TARGET.lower()
    out = ""
    for i, l in enumerate(leaders, 1):
        addr = str(l.get("proxyWallet") or l.get("address") or "")
        nm = l.get("userName") or l.get("name") or (addr[:8] + "…")
        pnl = l.get("pnl") or l.get("profit") or 0
        try:
            pnl = f"${float(pnl):,.0f}"
        except (TypeError, ValueError):
            pnl = str(pnl)
        here = " style=background:#1e293b" if addr.lower() == cur else ""
        btn = (f'<form method=post action=/target style=margin:0><input type=hidden name=addr value="{addr}">'
               f'<button class=copy>copy</button></form>') if addr else ""
        out += (f'<tr{here}><td class=dim>{i}</td><td>{nm}</td>'
                f'<td class=r style=color:#4ade80>{pnl}</td><td>{btn}</td></tr>')
    return out or "<tr><td colspan=4 class=dim>leaderboard loading…</td></tr>"


def _settings_form():
    is_ready = ready()
    open_attr = "" if is_ready else "open"
    pk_set = STATE.get("pk_set") or bool(os.environ.get("PM_PRIVATE_KEY"))
    pk_status = "🔑 set (memory)" if pk_set else "not set"
    funder_val = STATE.get("funder", "") or os.environ.get("PM_FUNDER", "")
    return f"""<details {open_attr} class=cfg><summary>⚙ Settings {'· ✅ ready' if is_ready else '· ⚠ setup needed'}</summary>
<form method=post action=/settings class=settings>
  <label>Target wallet<input name=target value="{TARGET}" placeholder="0x… (or click a leaderboard trader)"></label>
  <label>Funder wallet (your deposit address)<input name=funder value="{funder_val}" placeholder="0x…"></label>
  <label>Private key — {pk_status}<input name=private_key type=password autocomplete=off placeholder="paste to set · blank keeps current"></label>
  <label>Mode<select name=mode>
    <option value=auto {"selected" if MODE == "auto" else ""}>auto — copy instantly</option>
    <option value=approve {"selected" if MODE == "approve" else ""}>approve — I click ✓ per trade</option>
  </select></label>
  <label>Copy fraction<input name=fraction value="{COPY_FRACTION}"></label>
  <label>Max $ / trade<input name=cap value="{MAX_USDC_PER_TRADE}"></label>
  <label>Slippage<input name=slippage value="{SLIPPAGE}"></label>
  <label>Bankroll $<input name=bankroll value="{BANKROLL}"></label>
  <button class=save type=submit>Save</button>
</form>
<p class=dim>Key is held in memory only, never written to disk — re-enter after restart. Page is localhost-only.</p>
</details>"""


def render():
    with LOCK:
        live, copies, err = STATE["live"], STATE["copies"], STATE["error"]
        started, last = STATE["started"], STATE["last_poll"]
        holdings = [(STATE["names"].get(k, k[:16]), v) for k, v in STATE["holdings"].items() if v > 0]
        tname = STATE["target_name"]
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
    tlabel = ((tname + " · " if tname else "") + (TARGET[:6] + "…" + TARGET[-4:])) if TARGET else "none set"

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=3><title>copybot</title><style>
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
<h1>Polymarket copybot</h1>
{_settings_form()}
<div class=bar>
  <span class=pill>{pill}</span>
  <span class="tag dim">target {tlabel}</span>
  <span class="tag dim">copies {copies}</span>
  <span class="tag dim">up {up // 3600}h{up % 3600 // 60}m</span>
  <span class="tag dim">polled {age}</span>
  {toggle}
  <form method=post action=/kill style=display:inline onsubmit="return confirm('Kill the bot?')"><button class=kill>Kill</button></form>
</div>
{errbar}
{_pending_card()}
<div class=grid>
  <div class=card>
    <h2>Bot — holding</h2>
    <table><tr><th>market — outcome</th><th class=r>shares</th></tr>{hrows}</table>
    <h2>Bot — wants to buy (DRY intents)</h2>
    <table><tr><th>time</th><th>market — outcome</th><th class=r>size</th></tr>{irows}</table>
  </div>
  <div class=card>
    <h2>Target — live activity</h2>
    <table><tr><th>side</th><th>market — outcome</th><th class=r>size</th><th class=r>px</th></tr>{_target_rows()}</table>
    <h2>How to copy him best</h2>
    <ul class=tips>{stats}</ul>
  </div>
</div>
<h2>Who else to copy (leaderboard — click to switch target)</h2>
<div class=card><table><tr><th>#</th><th>trader</th><th class=r>pnl</th><th></th></tr>{_leader_rows()}</table></div>
<h2>Trade history</h2>
<div class=card><table><tr><th>when</th><th>kind</th><th>side</th><th>market — outcome</th><th class=r>size</th><th>note</th></tr>{_history_rows()}</table></div>
<h2>Bot activity log</h2>
<div class=card><table><tr><th>time</th><th>kind</th><th>side</th><th>market — outcome</th><th class=r>size</th><th>note</th></tr>{lrows}</table></div>
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
        self._send(200, render())

    def do_POST(self):
        global TARGET, COPY_FRACTION, MAX_USDC_PER_TRADE, SLIPPAGE, BANKROLL, PRIVATE_KEY_MEM, MODE
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
        elif path in ("/target", "/settings"):
            addr = (f.get("addr", [""])[0] or f.get("target", [""])[0]).strip()
            if addr.startswith("0x") and len(addr) == 42 and addr != TARGET:
                TARGET = addr
                with LOCK:
                    STATE["baselined"].discard(addr)
                    STATE["target_name"] = ""
                    STATE["error"] = ""
                logline(kind="skip", note=f"target -> {addr[:8]}…")
            if path == "/settings":
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
                SLIPPAGE = num("slippage", SLIPPAGE)
                BANKROLL = num("bankroll", BANKROLL)
                save_config()
        elif path == "/kill":
            self._send(200, "bye")
            os._exit(0)  # ponytail: abrupt, but it's a side-project button
        self._redirect()

    def log_message(self, *a):
        pass


# ---- entry ------------------------------------------------------------------
def _check():
    assert my_buy_size(1000, 0.50, 0.01, 50) == 10.0
    assert my_buy_size(1000, 0.50, 0.01, 3) == 6.0
    assert my_buy_size(10, 0.50, 0.01, 50) == 0.0
    assert limit_price(0.50, "BUY") == 0.51
    assert limit_price(0.50, "SELL") == 0.49
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
    assert any("1 buys" in x for x in copy_stats([{"side": "BUY", "size": 100, "price": 0.5}]))
    html = render()
    assert "copybot" in html and "Settings" in html and "leaderboard" in html and "Trade history" in html
    print("self-check OK")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
        sys.exit()
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(url)  # already running — just open its window
        sys.exit()
    load_config()
    load_state()
    threading.Thread(target=bot_loop, daemon=True).start()
    print(f"copybot: {url}  (configure in the page, then Go LIVE)")
    webbrowser.open(url)
    server.serve_forever()
