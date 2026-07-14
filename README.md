# fleet-dash

A local web dashboard for your whole Claude Code fleet: every live session across all
projects/worktrees, their subagents, live token spend, and — the headline feature —
**remote interaction**: pending AskUserQuestions render as tappable buttons and answers are
keystroke-injected back into the owning iTerm tab. Built 2026-07-13; still evolving.

**Dashboard:** http://127.0.0.1:8377 (always on — launchd daemon, starts at login)

## What it shows

- **One card per live session**, sorted needs-you-first, headed by the session's AI tab title
  (same string as your iTerm tab), with project · branch beneath.
- **State chip:** `needs you` (blocked on a question/permission — amber), `done ✓` (work turn
  finished <15 min ago, unharvested), `running`, `stalled` (transcript frozen >4 min mid-turn),
  `idle` (at prompt, nothing pending), `dormant` (quiet >2h — VS Code backends, forgotten panes).
- **Per card:** model, context-used bar, agents running/done + agent spend, quiet time,
  `open ↗` deep link to the session's claude.ai/code page.
- **Running subagents inline** (type, description, model, tokens/sec sparkline, live $).
- **Tap a card** → detail panel: a "session info" dropdown at the top (full session id, pid, cwd,
  exact model, started-ago, CLI status, tokens in context, spend split), recent conversation,
  a **free-text send box** (types the message into that session's terminal and submits it), and
  a "completed agents" dropdown beneath it.
- **Amber interaction box** when a session is waiting on you:
  - AskUserQuestion → full question + option buttons (multi-select = toggles + submit).
  - Permission request → the notification text + allow / always allow / deny buttons.
- **Conversation context**: the session's last few turns (your prompts + Claude's replies,
  markdown-rendered) in a scrollable box — shown automatically above the amber box when a
  session needs you; for every other state it's in the tap-detail panel ("recent conversation").
- **Delivered files**: anything the session sent you via SendUserFile appears as tappable chips
  (📄 md/text, 🖼 images) under the context — opens a full-screen viewer with markdown rendered
  and images inline. Viewing file contents requires the act token (same `?token=` opt-in);
  chips for files that were since deleted show "(gone)".
- **recently closed** dropdown: last 20 closed sessions (title, final spend, agents, closed-ago).
- **agent spend · last 7 days** dropdown: per-day rollup by agentType × model from the ledger.
- Browser-tab badge `(n)` = sessions needing you.

## Companion command

`/subagent-spend` inside any Claude Code session prints the per-agent token/cost table for that
session (wraps `engine.py spend --cwd "$PWD"`; needs sandbox-off because the engine probes PIDs).

## How it works (one paragraph)

A launchd daemon (`server.py` + `engine.py`) polls `~/.claude/sessions/*.json` (the CLI's live
registry — pid, status busy/idle/waiting, claude.ai bridge id) and incrementally tails each
session's transcript jsonl + `subagents/*.jsonl` for usage/state. Pending prompts come from
**hooks** (`hooks/pending-capture.py`, registered in `~/.claude/settings.json`) because the CLI
only writes AskUserQuestion rows to the transcript *after* they're answered. Answers are injected
by `FleetDashInjector.app` (a TCC-authorized applet: daemon writes a request file, `open -g`, the
applet types into the iTerm session matched by tty). Finished agent runs and closed sessions are
recorded in `ledger.db` (sqlite).

## Manual setup — already done on this Mac

Nothing to redo unless something breaks; listed for disaster recovery:

1. launchd agent: `~/Library/LaunchAgents/com.benjaminfeder.fleet-dash.plist` (bootstrap once:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.benjaminfeder.fleet-dash.plist`).
2. Hooks registered in `~/.claude/settings.json`: PreToolUse+PostToolUse `AskUserQuestion` and
   `Notification` → `hooks/pending-capture.py`.
3. Automation grant: FleetDashInjector → iTerm2 (System Settings › Privacy & Security ›
   Automation). Gotcha: the applet must have a `CFBundleIdentifier` or the toggle won't stick.
4. Act token loaded once per device by opening `http://127.0.0.1:8377/?token=<act_token>`
   (token lives in `config.json`; yellow "read-only" banner = this device has no token).

## Enabling phone use (still TODO — the only unfinished setup)

1. Install **Tailscale** on the Mac and phone, sign both into your tailnet.
2. On the Mac: `tailscale serve --bg 8377` → gives an HTTPS URL like
   `https://<mac-name>.<tailnet>.ts.net`.
3. On the phone, open that URL once with `?token=<act_token>` appended (get it via:
   `python3 -c "import json;print(json.load(open('$HOME/.claude/fleet-dash/config.json'))['act_token'])"`).
4. Push notifications: install the **ntfy** app, subscribe to topic `fleet-efe34e31d4ca2b81`
   (server ntfy.sh). Events: session stalled, spend threshold crossed ($5 steps), fleet gone
   quiet, blocked-on-you >3 min. Topic/server/threshold all in `config.json`.

## Config (`config.json`)

| key | default | meaning |
|---|---|---|
| `poll_seconds` | 2 | scan cadence |
| `stall_seconds` | 240 | frozen-mid-turn threshold (long Bash gates freeze transcripts!) |
| `dormant_seconds` | 7200 | quiet sessions demote to dormant |
| `turn_done_window_seconds` | 900 | how long "done ✓" persists before fading to idle |
| `awaiting_input_notify_seconds` | 180 | blocked-on-you push debounce |
| `spend_threshold_usd` | 5 | per-session push threshold (fires per multiple) |
| `rates` | — | $/1M by family. **`fable` is a PLACEHOLDER (opus rates) — fix when published** |
| `permission_keys` | 1/2/Esc | keystrokes for allow/always/deny |
| `ntfy_server`/`ntfy_topic` | ntfy.sh / fleet-… | push channel (empty topic = disabled) |
| `act_token` | generated | device token for the act endpoint |

Apply config/engine changes with: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`
(dashboard.html changes need no restart — open tabs self-reload). Log: `fleet-dash.log`.

## Rebuilding the injector applet

```
osacompile -o FleetDashInjector.app injector.applescript
plutil -insert CFBundleIdentifier -string com.benjaminfeder.fleet-dash.injector \
  FleetDashInjector.app/Contents/Info.plist   # only if recreated from scratch
codesign --force --sign - FleetDashInjector.app
```
A rebuild MAY re-trigger the automation prompt once (ad-hoc signature changes).

## Hard-won platform facts baked into the design

- Pending AskUserQuestions are **invisible in the transcript** (rows flush on answer) → hooks.
- The input-needed Notification (~6s after a question) must not clobber the question capture.
- launchd-context osascript **hangs forever** on the TCC check (can't show the dialog) → applet.
- TUI keys: digits toggle; **Enter toggles the focused row in multi-select** (does NOT submit);
  submit = right-arrow to the `✔ Submit` tab + Enter; Enter must be raw CR (iTerm newline = LF).
- `http.server` keeps the query string in `self.path` (`/?token=…` ≠ `/`).
- Sandbox blocks `os.kill(pid, 0)` probes — PID-liveness tools run sandbox-off (daemon is).

## Known gaps (the "not done" list)

- Permission-prompt injection (allow/always/deny keys) is wired but **untested against a real
  permission dialog**; dialog variants may need `permission_keys` tuning.
- Multi-part (2+ question) asks render read-only — answer those at the terminal/claude.ai.
- VS Code extension sessions have no tty → view-only (injection reports "no terminal").
- "Recently closed" only records sessions the daemon saw alive (fills from 2026-07-13 onward).
- The markdown viewer is a minimal built-in renderer (headings, lists, tables, code, quotes,
  links) — exotic markdown falls back to plain paragraphs. Non-md text files show raw.
- Conversation context and file chips exist only for live sessions (closed sessions: use the
  claude.ai deep link).
- Screen-peek ("show me what this stalled session's terminal displays") is proven as a technique
  (`contents of session` osascript) but not built into the UI yet.
- Fable pricing placeholder; ledger has no backfill from pre-daemon transcripts.
