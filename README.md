# fleet-dash

A local web dashboard for your whole Claude Code fleet: every live session across all
projects/worktrees, their subagents, live token spend, and — the headline feature —
**remote interaction**: pending AskUserQuestions render as tappable buttons and answers are
keystroke-injected back into the owning iTerm tab. Built 2026-07-13; still evolving.

**Dashboard:** http://127.0.0.1:8377 (always on — launchd daemon, starts at login)

## What it shows

- **One card per live session**, sorted needs-you-first, headed by the session's AI tab title
  (same string as your iTerm tab), with project · branch beneath. On an open card the header
  pins to the top of the screen while you scroll the card body (collapse from anywhere), and
  scrolls away past the card's end.
- **⚙ settings** (top right): per-category toggles for the ntfy pushes (waiting-on-you,
  stalled, spend threshold, fleet quiet), **their thresholds** (blocked seconds, stall
  seconds — this one also drives the "stalled" chip, $ step, fleet-idle minutes), and the
  **push tap-target** (`dashboard_url` — set it to your Tailscale URL and tapping a
  notification opens the dashboard). Persisted to `config.json`, token required.
- **🔔 per-session mute** on every card header (works collapsed): 🔕 silences that session's
  pushes (waiting/stalled/spend) without touching the fleet-wide categories. Mutes persist
  across daemon restarts and auto-expire 30 days after being set.
- Card headers stay lean: the $ total appears only on an open card; done-agent count and
  agent spend live in the detail panel ("completed agents", "session info"), not the header.
  The running-agent count stays visible everywhere.
- **➕ new Claude Code session** (button under the live list): pick a directory (recent ones the
  daemon has seen, or type a path under `~`), a model, an effort level (`low`…`max`), and
  optionally a **new git worktree** — it opens a fresh iTerm tab running `claude` with those
  flags, then auto-opens that session's full chat view here once it appears, so you can send
  the first prompt from your phone. Untrusted folders are flagged: Claude Code asks "do you
  trust the files in this folder?" at startup and **only your Mac can answer that** — trust is
  inherited from a parent dir, so worktrees under a trusted repo start clean.
- **Dormant sessions** get their own fold above closed sessions. Dormant = the transcript
  hasn't moved in over 2h (`dormant_seconds`) AND no agents are running — forgotten panes and
  VS Code backends. They're kept out of the live list and can never "need you".
- **State chip:** `needs you` (blocked on a question/permission — amber), `done ✓` (work turn
  finished <15 min ago, unharvested), `running`, `stalled` (transcript frozen >4 min mid-turn),
  `idle` (at prompt, nothing pending), `dormant` (quiet >2h — VS Code backends, forgotten panes).
- **Per card:** model, context-used bar (**amber ≥50%, red ≥60%** — compaction is expensive
  and costs you working context, so this is your cue to wrap up or `/compact` deliberately),
  running-agent count, quiet time, and 🔔 mute. (The ■ stop button lives in the full view.)
- **Last-message preview** on every card: one line of the newest actual message (prose only —
  tool calls and system events are skipped), tagged YOU / CLAUDE, between the meta row and the
  subagent rows. The ⚙ panel has a **display** toggle to show the same preview on running
  subagent rows (off by default); tapping one opens that agent's chat.
- **Running subagents inline** (type, description, model, throughput, sparkline, live $). The
  `tok/s` figure is throughput — tokens per second the agent is processing, **cache reads
  included** — so it is a liveness signal (is it moving?), not output speed; a big context makes
  it large.
- **Model · effort** wherever a model is shown (`opus · high`). Effort lives only in the
  statusline payload, so `statusline-command.sh` side-writes it per session for the daemon; a
  session whose statusline hasn't rendered yet shows the model alone. Subagent effort comes from
  the agent definition's frontmatter pin, or the parent session's effort when it pins none.
- **"open"** on each card header (desktop only): brings that session's iTerm tab to the front.
- **Tap any agent row — running or completed — for its own full-screen chat view:** the
  subagent's conversation (the prompt it was given, its replies, its tool calls), an agent-info
  dropdown (id, type, description, model, state, started/last activity, token split, $), and a
  **relay box**. A subagent has **no terminal of its own** — the only channel to it is the
  parent Claude calling `SendMessage`. So the box types a tagged relay line into the **parent
  session's** input (`[fleet-dash relay to subagent … ] your text`), and the parent forwards it.
  Delivery is the parent's call, not a guarantee — the view says so above the box. It's refused
  outright while the parent is blocked on a prompt (that input box is the question UI, and the
  relay would answer it).
- **Session events in the conversation**, the way the terminal shows them, so a remote read of
  the transcript isn't missing what the TUI told you:
  - **⧉ compaction** — `manual compaction · 289k → 14k tokens · 141s`, plus a live **"⧉
    compacting 47s"** pill in the header while one is running (see the caveat below).
  - **⇄ model fallback** — the "Fable 5's safeguards flagged this message … switched to Opus
    4.8" notice (it's a *refusal* fallback, not a usage limit) and anything of that shape.
  - **⚠ API errors** — `529 Overloaded`, `503 upstream connect error`, with the retry count
    (a retry storm collapses into one row: `⚠ 529 Overloaded ×7`).
  - **› slash commands you ran** — `/compact`, `/model`, `/login`, with their output.
  - **☑ questions you answered** — every question and the answer you picked, in **full**, so
    you can confirm from your phone that the right answers landed.
- **`/` autocomplete on the send box:** type `/` and get a clickable menu of everything that
  session can run — built-ins, your `~/.claude` skills + commands, the project's `.claude`
  skills + commands, and every installed plugin's (104 entries on this Mac), each with its
  description and scope. Tapping **inserts** the command (most take arguments); the send button
  fires it. Destructive ones (`/clear`, `/compact`, `/rewind`) are tagged and ask for
  confirmation before sending.
- **⤢ full view** (button beside the "recent conversation" header) → the whole session
  full-screen: the complete conversation with room to read, the send box (with `/`
  autocomplete), the amber question block when it's blocked on you, a delivered-file strip, and
  — in the header — the **■ stop button** (confirms first, then sends Esc: the remote "stop this
  turn"; only offered while the session is mid-turn). The card keeps its inline conversation for
  scanning; this is for actually reading and working a session.
  The chat view and the file viewer are **mutually exclusive** and swap in one tap: tapping a
  file chip in the chat view opens that file (chat closes), and the viewer's own **⤢ full view**
  button (right of "show conversation") takes you straight back. The two are built to resemble
  each other — same docked bar, same file strip (one shared builder), same send box — so
  swapping feels like changing what's on screen, not changing screens.
- **Tap a card** → detail panel, top to bottom: recent conversation (with its ⤢ full-view
  button), a one-line horizontal strip of delivered-file chips (quick open), then the "session
  info" (full session id, pid, cwd,
  exact model, started-ago, CLI status, tokens in context, spend split), "delivered files"
  (caption + age) and "completed agents" dropdowns (the latter two scroll internally past
  ~220px), and the open-in-claude.ai link.
- **Amber interaction box** when a session is waiting on you. On the CARD a question is only a
  **signal** — "multi-part question (3) — waiting on you", any read-first chip, and an
  **answer ⤢** button that opens the full view with the question expanded; the controls
  themselves live in the full-screen views (they used to swamp the fleet list). Permission
  prompts are small, so they still answer inline on the card:
  - AskUserQuestion → full question + option buttons (multi-select = toggles + submit), plus an
    **"Other" free-text input** (types your own answer into the TUI's "Type something" row) and
    a **✕ dismiss button** (= the TUI's "Chat about this": the session hears "user declined"
    and returns to normal chat).
  - Multi-question asks (2+) show **one question at a time** with ‹ › arrows and an
    answered-count, a per-question "selected:" line + Other input, and one "submit all
    answers" button (single-select picks auto-advance, like the terminal).
  - Permission request → the notification text + allow / always allow / deny buttons.
  - The selector disappears the moment an answer sends — no waiting on the next poll.
  - If a file was delivered shortly before the question (the deliver-then-ask pattern), the box
    leads with a **"read first" chip** — visible even on a collapsed card. Window:
    `question_file_pair_seconds`.
- **Conversation context**: the session's recent turns (your prompts + Claude's replies,
  markdown-rendered, including messages you send mid-turn from the app) in a scrollable box — shown automatically above the amber box when a
  session needs you; for every other state it's in the tap-detail panel ("recent conversation").
  Key tool calls appear inline terminal-style as a single `● Edit(path)` line —
  Edit/Write/Bash/Agent/Skill/SendUserFile only; read-only chatter (Read/Grep/Glob) is hidden.
  The buffer keeps the last ~120 entries per session.
- **Delivered files**: anything the session sent you via SendUserFile appears as a tappable chip
  (📄 md/text, 🖼 images) **inline in the conversation at the point it was delivered**, with its
  caption — so the message explaining the file sits right with it. The detail panel also has a
  "delivered files" dropdown (caption + delivered-ago). Chips open a full-screen viewer with
  markdown rendered and images inline; viewing contents requires the act token (same `?token=`
  opt-in); files since deleted show "(gone)".
- **File viewer** extras: a ☀︎/☾ button toggles a light "paper" theme for the document
  (persisted per device); a **📄 files strip** (header button) expands a one-line horizontally
  scrolling selector of everything the session delivered, for switching files without leaving
  the viewer; and a **docked action bar** at the bottom carries, top to bottom: a
  "▸ show conversation" toggle (expands the session's recent conversation, scrollable), the
  pending question (collapsible via "▾ hide question"; full option descriptions, Other input,
  ✕ dismiss), and the always-visible free-text send box.
- The needs-you context box on a card is deliberately short (~150px, scrollable); the detail
  panel's "recent conversation" is the tall one.
- **closed sessions** dropdown: every closed session the daemon ever saw (title, final spend,
  agents, closed-ago), with a filter box (title / project / branch); tap a row for its info
  block (full id, cwd, branch, model, lifetime, spend split). Session ids, cwds and agent ids
  in any info block are **tap-to-copy**.
- **cost insights** dropdown (7/30/90-day window; each subsection its own dropdown): where
  the tokens and money actually go —
  - **cache invalidations**: every API call whose cache_read fell short of the previous
    call's read+write, counting only tokens actually re-paid (as cache-write/uncached),
    classified by cause — compaction, model switch, idle/TTL, skill invocation, tail
    rewrite after the last breakpoint ("breakpoint drift"), deep bust — with est $ re-paid;
  - **$ by token class per day**: uncached input / cache write / cache read / output,
    priced per model family from real transcript usage — the single most direct
    "what costs money" view;
  - **by agent type**: runs, total $, avg $/run, cache-hit % (exact, from the agent ledger);
  - **by skill**: uses + the attributed cost of turns run while that skill was active
    (heuristic: from a `Skill` invocation to end-of-turn; overlapping skills attribute to the
    most recent);
  - **by tool**: uses + estimated tokens injected by tool results (chars/4) — the
    context-bloat view; a huge "tokens in" here is paid again on every later turn;
  - **by model**, **by project**, **agent $ by day**, **top sessions**.
  Session-level $ figures are lifetime costs of sessions active in the window (per-day
  session attribution isn't recorded). Tool/skill volumes accumulate from transcripts the
  daemon has tailed — history starts when this feature landed (2026-07-14) plus whatever
  live-session transcripts it re-read.
- Browser-tab badge `(n)` = sessions needing you.
- Scrollbars (vertical + horizontal) auto-hide when idle and appear while scrolling; the 2s
  refresh pauses during any scroll gesture or tap (touch and desktop wheel/trackpad alike) so
  it never yanks a scrollbox out from under you.

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
   quiet, blocked-on-you >3 min. Topic/server/threshold in `config.json`; per-category
   on/off via the dashboard's ⚙ settings.

## Config (`config.json`)

| key | default | meaning |
|---|---|---|
| `poll_seconds` | 2 | scan cadence |
| `stall_seconds` | 240 | frozen-mid-turn threshold (long Bash gates freeze transcripts!) |
| `dormant_seconds` | 7200 | quiet sessions demote to dormant |
| `turn_done_window_seconds` | 900 | how long "done ✓" persists before fading to idle |
| `awaiting_input_notify_seconds` | 180 | blocked-on-you push debounce |
| `spend_threshold_usd` | 5 | per-session push threshold (fires per multiple) |
| `question_file_pair_seconds` | 300 | max age of a delivered file to pair as "read first" on a question |
| `notify` | all true | per-category push toggles (needs_you/stall/spend/fleet_quiet) — the ⚙ panel edits this |
| `fleet_quiet_minutes` | 0 | how long the fleet must stay fully idle before the quiet push (0 = on transition) |
| `muted_sessions` | {} | session_id → mute-ts map behind the 🔔 card toggle (30-day auto-expiry) |
| `rates` | — | $/1M by family. **`fable` is a PLACEHOLDER (opus rates) — fix when published** |
| `permission_keys` | 1/2/Esc | keystrokes for allow/always/deny (empty value = Esc) |
| `dashboard_url` | "" | ntfy `Click` target — tapping a push opens this URL (⚙ panel edits it) |
| `ntfy_server`/`ntfy_topic` | ntfy.sh / fleet-… | push channel (empty topic = disabled) |
| `act_token` | generated | device token for the act endpoint |

Apply config/engine changes with: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`
(dashboard.html changes need no restart — open tabs self-reload). Log: `fleet-dash.log`.

## Rebuilding the injector applet

The applet is **stay-open** (`OSAAppletStayOpen`), so it stays resident and `open -g` hits its
`on reopen` handler instead of paying a process launch on every click.

```
osacompile -o FleetDashInjector.app injector.applescript
plutil -replace OSAAppletStayOpen -bool true FleetDashInjector.app/Contents/Info.plist
plutil -insert CFBundleIdentifier -string com.benjaminfeder.fleet-dash.injector \
  FleetDashInjector.app/Contents/Info.plist   # only if recreated from scratch
codesign --force --sign - FleetDashInjector.app
```
A rebuild MAY re-trigger the automation prompt once (ad-hoc signature changes).

## Hard-won platform facts baked into the design

- Pending AskUserQuestions are **invisible in the transcript** (rows flush on answer) → hooks.
- A finished agent **often never writes an `end_turn`** — a long final report ends on a
  stop_reason-less text row. So "done" is judged by whether anything is in flight (a tool call
  awaiting its result), not by `stop_reason`; "stalled" means frozen mid-tool. A background
  agent's `tool_result` in the parent arrives at *spawn* ("Async agent launched successfully"),
  so it can't be used as a completion signal either.
- The input-needed Notification (~6s after a question) must not clobber the question capture.
- launchd-context osascript **hangs forever** on the TCC check (can't show the dialog) → applet.
- TUI keys: digits toggle; **Enter toggles the focused row in multi-select** (does NOT submit);
  submit = right-arrow to the `✔ Submit` tab + Enter; Enter must be raw CR (iTerm newline = LF).
  The ask TUI also numbers a "Type something" row (n+1, the Other path) and a "Chat about this"
  row (n+2); Esc anywhere = declined/chat-about-this. Full key map: CLAUDE.md invariant 4.
- `http.server` keeps the query string in `self.path` (`/?token=…` ≠ `/`).
- Sandbox blocks `os.kill(pid, 0)` probes — PID-liveness tools run sandbox-off (daemon is).
- **A compaction writes nothing to the transcript while it runs** — the `/compact` rows and the
  boundary all flush at the end (and the command rows land *after* the boundary, carrying
  earlier timestamps, so events are ordered by timestamp, not file order). The live "compacting"
  pill therefore reads the **PreCompact hook's checkpoint file** mtime; a project with no
  PreCompact hook shows no pill (its finished-compaction event still lands). The TUI's
  `Compacting… 43%` progress bar is screen-only and needs screen-peek to mirror.
- **Typing `/` in the TUI opens its own command popup, where Enter fires the *highlighted*
  entry** — so injecting a bare `/foo` + Enter can run a different command. A trailing space
  closes that popup, and the daemon appends one to any `/…` message before submitting.

## Known gaps (the "not done" list)

- Permission-prompt injection (allow/always/deny keys) is wired but **untested against a real
  permission dialog**; dialog variants may need `permission_keys` tuning.
- VS Code extension sessions have no tty → view-only (injection reports "no terminal").
- "Recently closed" only records sessions the daemon saw alive (fills from 2026-07-13 onward).
- The markdown viewer is a minimal built-in renderer (headings, lists, tables, code, quotes,
  links) — exotic markdown falls back to plain paragraphs. Non-md text files show raw.
- Closed sessions are read-only: the ⤢ button opens their conversation (recovered from the
  transcript via the ledger's cwd), but there's no terminal left to send to. Subagent chats are reachable only while their parent session is live.
- Messaging a subagent is a **relay through the parent**, never a direct channel — there is no
  such thing as typing into a subagent (no tty; `SendMessage` from the parent is the only path).
- Screen-peek ("show me what this stalled session's terminal displays") is proven as a technique
  (`contents of session` osascript) but not built into the UI yet.
- Fable pricing placeholder; ledger has no backfill from pre-daemon transcripts.
