# polymarket-copybot

Single-file Polymarket copy-trading bot with a native desktop UI and an
embedded Claude copilot. Watches one or more traders and mirrors their trades
at a fraction of their size.

## Features
- **Real-time copying** — WebSocket stream of platform trades (sub-second reaction), REST polling as fallback, per-trade dedupe
- **Faithful execution** — pre-flight midpoint check (never chases a run-away price), FAK orders (fill now or die), per-market tick sizes, conviction floor (skips the target's dust buys)
- **Native app** — pywebview window, no browser needed; partial-refresh UI so typed input never disappears; single-instance guard
- **Setup in the UI** — targets, funder, private key, sizing all in the page; instant green/red validation; key verified cryptographically (derives its signer address); auto-goes-live when fully configured
- **Claude copilot** — `claude -p` (Opus 4.8) with a live bot-state snapshot; explains trades/skips/settings and can change modes & sizing via a whitelisted action protocol
- **Observability** — status checklist, on-chain wallet panel (cash + positions + PnL), persistent trade history, copy-lag cost metric, test-connection and ~$1 test-trade buttons

## Run
```
pip install py-clob-client-v2 requests websocket-client pywebview
python copybot.py            # opens the app window
python copybot.py --check    # offline self-test
```
Configure everything in the window. Config (including the private key, by
owner's choice) persists to `copybot_config.json` — gitignored, plaintext,
single-user machine assumption.

## Safety model
- Boots watch-only unless fully configured; approve mode queues every copy for a manual ✓
- SELLs only what the bot itself bought; buys capped per-trade and by exchange minimums
- The copilot can tune settings but can never place trades or read the key
