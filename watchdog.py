# Copybot watchdog: app health + Polymarket connection + missed-trade audit.
# Read-only: never trades, never changes state. Prints a compact report.
import json, re, sys, time
from pathlib import Path
import requests

APP = "http://127.0.0.1:8777/dyn"
CONFIG = Path(__file__).with_name("copybot_config.json")   # portable: same dir as this script
STATE = Path(__file__).with_name("copybot_state.json")
GRACE = 300          # seconds a trade may sit unprocessed before it counts as missed
LOOKBACK = 25 * 60   # audit window > loop interval (15 min) so nothing falls between firings

problems, notes = [], []

# ---- 1) app serving? ----
try:
    html = requests.get(APP, timeout=10).text
except Exception as ex:
    print("app: NOT SERVING —", str(ex)[:120])
    print("VERDICT: DOWN")
    sys.exit(0)
text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

# ---- 2) header + status facts ----
pill = ("LIVE · AUTO" if "LIVE · AUTO" in text else
        "LIVE · APPROVE" if "LIVE · APPROVE" in text else
        "DRY" if "DRY — watch only" in text else "?")
m = re.search(r"budget \$([\d.]+) / \$([\d.]+)", text)
budget = f"${m.group(1)} / ${m.group(2)}" if m else "?"
m = re.search(r"up (\d+)h(\d+)m", text)
up_s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 if m else 0
m = re.search(r"polled (\d+)s ago", text)
poll_age = int(m.group(1)) if m else None
m = re.search(r"copies (\d+)", text)
copies = m.group(1) if m else "?"
ws_ok = bool(re.search(r"✅ Real-time feed", text))
conn_ok = bool(re.search(r"✅ Polymarket trading connection", text))
errs = []  # only errors inside the audit window: solved ones must age out, not alarm forever
try:
    for e in json.load(open(STATE)).get("history", []):
        if e.get("kind") != "error":
            continue
        try:
            ts = time.mktime(time.strptime(f'{e.get("d")} {e.get("t")}', "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if ts >= time.time() - LOOKBACK:
            errs.append(f'{e.get("t")} {e.get("side", "")} {(e.get("name") or "")[:40]} | {(e.get("note") or "")[:90]}')
except Exception:
    pass

if pill == "?":
    problems.append("mode pill unreadable — page layout changed?")
if pill == "DRY":
    notes.append("bot is in DRY mode — watching, not trading")
if poll_age is None:
    problems.append("no successful poll yet")
elif poll_age > 120:
    problems.append(f"poll is stale ({poll_age}s ago) — feed thread may be stuck")
if not ws_ok:
    notes.append("WebSocket down — poll fallback still copies, just slower (~15s)")
if not conn_ok:
    problems.append("Polymarket trading connection not verified")
if errs:
    problems.append(f"{len(errs)} ERROR log entries, latest: {errs[-1]}")

# ---- 3) missed-trade audit: every fresh target trade must be in the seen-set ----
missed, fresh_total, tgt_names = [], 0, []
try:
    cfg = json.load(open(CONFIG))
    targets = cfg.get("targets", [])
    seen = set(json.load(open(STATE)).get("seen", []))
    now = time.time()
    app_start = now - up_s
    for tgt in targets:
        r = requests.get("https://data-api.polymarket.com/activity",
                         params={"user": tgt, "limit": 500}, timeout=20)
        r.raise_for_status()
        for a in r.json():
            if a.get("type") != "TRADE":
                continue
            ts = a.get("timestamp", 0)
            # only trades: recent, after this process baselined, and past grace
            if ts < now - LOOKBACK or ts < app_start + 120 or ts > now - GRACE:
                continue
            fresh_total += 1
            k = f'{a.get("transactionHash")}:{a.get("asset")}:{a.get("side")}'
            if k not in seen:
                missed.append(f'{time.strftime("%H:%M", time.localtime(ts))} {a.get("side")} '
                              f'{(a.get("title") or "?")[:48]} ${float(a.get("usdcSize", 0)):,.0f}')
        tgt_names.append(tgt[:10])
except Exception as ex:
    problems.append(f"audit failed (API/files): {str(ex)[:100]}")

if missed:
    problems.append(f"MISSED {len(missed)}/{fresh_total} target trades (not in seen-set): "
                    + " | ".join(missed[:5]))

# ---- report ----
print(f"mode: {pill} | budget: {budget} | copies: {copies} | up: {up_s // 3600}h{up_s % 3600 // 60}m "
      f"| polled: {poll_age}s ago | ws: {'ok' if ws_ok else 'DOWN'} | conn: {'ok' if conn_ok else 'UNVERIFIED'}")
print(f"audit: {fresh_total} fresh target trade(s) in last {LOOKBACK // 60}min window "
      f"(targets: {', '.join(tgt_names) or 'none'}) — {'ALL processed' if not missed else 'GAPS FOUND'}")
for n in notes:
    print("note:", n)
for p in problems:
    print("problem:", p)
print("VERDICT:", "OK" if not problems else "ATTENTION")
