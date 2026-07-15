# fleet-dash

A local web dashboard for Claude Code and Codex CLI sessions across all
projects/worktrees, their subagents, live token usage, and — the headline feature —
**remote interaction**: pending questions render as tappable buttons and answers are sent through
the provider's native control path. Built 2026-07-13; still evolving.

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
  notification opens the dashboard). It also selects **Fit the screen** or **Centered · fixed
  width** for every full-screen reading surface. Persisted to `config.json`, token required.
- **🔔 per-session mute** on every card header (works collapsed): 🔕 silences that session's
  pushes (waiting/stalled/spend) without touching the fleet-wide categories. Mutes persist
  across daemon restarts and auto-expire 30 days after being set.
- Card headers stay lean: the $ total appears only on an open card; done-agent count and
  agent spend live in the detail panel ("completed agents", "session info"), not the header.
  The running-agent count stays visible everywhere.
- **➕ new coding session** (button under the live list): choose Claude Code or Codex CLI, then
  pick a directory (recent ones the daemon has seen, or type a path under `~`), a model, and an
  effort level (`low`…`max`). Codex sessions also choose Plan or Default mode and start in Plan
  by default. Claude sessions can request a **new git worktree** — it opens
  a fresh iTerm tab running `claude` with those
  flags, then auto-opens that session's full chat view here once it appears, so you can send
  the first prompt from your phone. Untrusted folders are flagged: Claude Code asks "do you
  trust the files in this folder?" at startup and **only your Mac can answer that** — trust is
  inherited from a parent dir, so worktrees under a trusted repo start clean.

## Codex CLI integration

Fleet Dash owns one detached App Server as the canonical Codex runtime. It starts the documented
`codex app-server --listen unix://…` transport, then connects as one client through the documented
WebSocket-over-Unix protocol at `~/.claude/fleet-dash/codex-app-server.sock`. The detached listener
survives a Fleet web daemon restart and is reused instead of duplicated. (`codex app-server daemon
start` is not used: that manager requires Codex's standalone installer, while this machine uses the
npm CLI.) Fleet does not scrape the Codex TUI or parse `~/.codex` rollout files.

- Threads created by Fleet Dash are remembered in `codex_threads.json`, including their runtime
  ownership, mode, and last normalized conversation, and resume after daemon restarts.
- A terminal started with
  `codex resume --remote unix://$HOME/.claude/fleet-dash/codex-app-server.sock <thread-id>`
  is another client of that same runtime. Fleet adopts socket-attached CLI threads and can steer the
  active turn without resuming a second agent. The card's **attach** button opens this TUI form.
- ChatGPT Desktop and Codex VS Code threads use a different App Server. Fleet discovers their
  transcripts through paginated `thread/list`, keeps them in the collapsed **headless sessions**
  fold, and exposes them as view-only. There is deliberately no **take over** action: `thread/resume`
  on Fleet's server would create a second runtime copy, not attach to Desktop's active agent.
  Independently launched CLI threads that are not connected to Fleet's socket are likewise view-only.
  Child subagent threads never become duplicate top-level cards.
- Conversation history, prompt submission, interruption, and approval decisions use App Server
  thread/turn APIs.
- Codex thread IDs are stored as `codex:<native-id>` so they cannot collide with Claude IDs.
- Codex costs display as unavailable rather than being priced with Claude rates. App Server's
  exact per-thread token total and model context-window size drive each card's context gauge.
- Every managed Codex chat and file view has Plan/Default controls in its top-right overflow menu.
  The main fleet cards stay mode-free. The selected mode is
  persisted and applied through App Server's experimental `thread/settings/update` API; the
  installed Codex 0.144.4 behavior is covered by live and deterministic tests. Structured questions
  appear whenever Codex actually sends a request, rather than being inferred from the selected mode.
- Codex account rate-limit windows, reset times, plan type, reset credits, and lifetime token
  total come directly from App Server account APIs and appear in the top usage header. The unused
  GPT-5.3-Codex-Spark preview-model allowance remains available in the API payload but is omitted
  from the dashboard.
- Codex subagent conversations and lifecycle events are visible. App Server exposes no public client
  RPC for direct subagent input or stop, so relay and stop actions go through the parent turn and are
  labelled that way. Per-agent tokens are shown only when App Server supplies them; currency cost and
  throughput remain unavailable instead of displaying fabricated zeroes.
- App Server file-change and generated-image items appear as changed/generated artifacts with the
  same root-containment, size, and preview-type checks as Claude files. They are not labelled as
  explicitly delivered files because Codex has no SendUserFile-equivalent event.
- `/compact` and `/review` invoke native App Server actions. `$skill-name` selections use native
  `skills/list` metadata and `SkillUserInput`. TUI-only slash commands are not passed accidentally as
  ordinary prompts.
- Question, permission, command, file-change, and MCP elicitation requests use their native App
  Server response shapes. `request_user_input` is single-select plus optional free text; MCP
  elicitation supports provider-declared multi-select and accept/decline/cancel.

Requires a `codex` executable with App Server support. Set `codex_enabled` to `false` to disable
the provider without affecting Claude sessions.
- **Headless sessions** are external Codex transcripts that Fleet Dash can see but whose App Server it
  does not own. Their fold stays collapsed by default and every action remains in the owning client.
- **Dormant sessions** get their own fold above session history. For Claude, dormant means the
  transcript hasn't moved in over 2h (`dormant_seconds`) and no agents are running. For Codex, it
  means App Server reports the managed thread as `notLoaded` after more than 24h of inactivity.
  They're kept out of the live list and can never "need you".
- **Idle sessions** remain in the always-visible list because they are managed and can accept a turn
  immediately.
- **State chip:** `needs you` (blocked on a question/permission — amber), `done ✓` (work turn
  finished <15 min ago, unharvested), `running`, `stalled` (transcript frozen >4 min mid-turn),
  `idle` (at prompt, nothing pending), `dormant` (quiet >2h — VS Code backends, forgotten panes).
- **Provider-usage header** (top of the page, under the totals): provider, email, and plan details
  use middle-dot separators. Claude shows the logged-in account email and a local lifetime-token
  total from `~/.claude/stats-cache.json`; that total sums uncached input, cache writes, cache reads,
  and output represented by main and saved-subagent transcripts on this Mac. It excludes deleted
  history, other computers, and claude.ai activity. Claude's **5-hour** and **weekly** (7-day)
  utilization comes from the statusline payload's `rate_limits` (the same data `/usage` shows), which
  `statusline-command.sh` writes to `~/.claude/fleet-dash/usage.json`; those gauges populate after a
  session's first API call. Codex uses App Server `account/read` for the signed-in email and plan,
  plus every rate-limit bucket, reset time, account lifetime tokens, and available reset-credit count.
- **Per card meta line** — two groups on one row: **left** is activity (running-agent count ·
  quiet time, plus the running skill / compaction when active); **right**, right-adjusted, is
  the context-used bar (**amber ≥50%, red ≥60%** — compaction is expensive and costs you working
  context, so this is your cue to wrap up or `/compact` deliberately) · `model - effort`. 🔔 mute
  sits in the tail. Codex mode and lifecycle actions live in the full-view overflow menu.
- **Conversation peek** on every card: the newest actual message (prose only — tool calls and
  system events are skipped), tagged YOU / CLAUDE, between the meta row and the subagent rows.
  Headings, emphasis, lists, links, and inline code render as compact Markdown; document-scale
  code blocks and tables collapse rather than turning a status card into a document viewer.
  The ⚙ panel gives the session peek and the subagent-row peek their own on/off switch and line
  height (1–6; defaults: sessions on at 2 lines, subagents off at 1). The line count also caps
  what the server sends, so a short peek isn't shipping long text every poll. Tapping a
  subagent's peek opens that agent's chat. A card blocked on a QUESTION shows no peek — the ask
  is the context.
- **Running subagents inline** (type, description, model, throughput, sparkline, live $). The
  `tok/s` figure is throughput — tokens per second the agent is processing, **cache reads
  included** — so it is a liveness signal (is it moving?), not output speed; a big context makes
  it large.
- **Model · effort** wherever a model is shown (`opus · high`). Effort lives only in the
  statusline payload, so `statusline-command.sh` side-writes it per session for the daemon; a
  session whose statusline hasn't rendered yet shows the model alone. Subagent effort comes from
  the agent definition's frontmatter pin, or the parent session's effort when it pins none.
- **"open"** on a Claude card header (desktop only) brings that iTerm tab to the front. A managed
  Codex card shows **attach**, which opens a new Codex TUI connected to the canonical shared runtime.
  External Codex cards show disabled **view only** because their Desktop/VS Code runtime is separate.
- **Pin sessions to a watchlist at the top:** pinning lifts the full card into a
  **📌 pinned sessions** block directly below the usage header. The order is **stable** — the
  order you pinned them — and never reshuffles as session states change. On **desktop**, use the
  contained 📌 button immediately to the right of **open/attach/view only** in the session header; on
  **mobile**, **long-press** the header (a short tap still opens
  its chat). Pins are in-memory and clear on reload.
- **Tap any agent row — running or completed — for its own full-screen chat view:** the
  subagent's conversation (the prompt it was given, its replies, its tool calls), an agent-info
  dropdown (id, type, description, model, state, started/last activity, token split, $), and a
  **relay box**. A subagent has **no terminal of its own** — the only channel to it is the
  parent Claude calling `SendMessage`. So the box types a tagged relay line into the **parent
  session's** input (`[fleet-dash relay to subagent … ] your text`), and the parent forwards it.
  Delivery is the parent's call, not a guarantee — the view says so above the box. It's refused
  outright while the parent is blocked on a prompt (that input box is the question UI, and the
  relay would answer it). Its overflow menu has **light/dark mode** and **stop parent turn**, with
  the same caveat: a subagent has no
  terminal, so stopping it means Esc into its **parent** — ending the parent's whole turn and
  every other subagent under it. Every stop, anywhere, goes through an "are you sure"
  interstitial that says what will be lost.
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
  autocomplete), the amber question block when it's blocked on you, and a delivered-file strip.
  Its top-right **⋮ menu** contains Codex Plan/Default (when applicable), light/dark mode, Stop turn,
  and Close session. Stop and close both confirm first. Closing an active session stops its current
  turn and subagents, then archives a Codex thread or terminates only the registered Claude process;
  Claude's iTerm tab remains open. The conversation moves to **Session history**. The card keeps its
  inline conversation for scanning; this is for actually reading and working a session.
  The chat view and the file viewer are **mutually exclusive** and swap in one tap: tapping a
  file chip in the chat view opens that file (chat closes), and the viewer's own **⤢ full view**
  button (right of "show conversation") takes you straight back. The two are built to resemble
  each other — same docked bar, same file strip (one shared builder), same send box — so swapping
  feels like changing what's on screen, not changing screens. Session chat, Markdown, and subagent
  chat also share the persisted reading-width setting.
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
- **File viewer** extras: the same **⋮ menu** exposes light/dark mode, Codex mode, stop, and close
  actions that apply to the owning session. Light mode gives the document a paper theme; the choice
  is persisted per device and shared with the full chat and subagent views. A **📄 files strip**
  (header button) expands a one-line horizontally
  scrolling selector of everything the session delivered, for switching files without leaving
  the viewer; and a **docked action bar** at the bottom carries, top to bottom: a
  "▸ show conversation" toggle (expands the session's recent conversation, scrollable), the
  pending question (collapsible via "▾ hide question"; full option descriptions, Other input,
  ✕ dismiss), and the always-visible free-text send box.
- The needs-you context box on a card is deliberately short (~150px, scrollable); the detail
  panel's "recent conversation" is the tall one.
- **Session history** dropdown: every closed session the daemon ever saw (title, final spend,
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
| `codex_enabled` | true | start the Codex App Server adapter |
| `codex_command` | "" | optional absolute Codex executable path; auto-detected from PATH or `~/.nvm` |
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

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
npm install
npx playwright install chromium
npm run test:browser
```

The Python suite includes a deterministic fake App Server transport plus adapter and shared-engine
fixtures. Playwright runs the same provider/UI matrix at desktop and 390×844 mobile sizes. Scripts
named `tests/live_*_smoke.py` are opt-in checks against the running daemon; paid-turn scripts say so
in their docstring and archive threads they create.

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
- Claude VS Code extension sessions have no tty → view-only (injection reports "no terminal").
- ChatGPT Desktop and Codex VS Code do not expose their private App Server endpoint to Fleet Dash, so
  those transcripts are view-only. Managed Codex threads can open an attached TUI on Fleet's shared
  socket, but Fleet cannot focus an already-open Codex terminal tab.
- Codex App Server has no direct client-to-subagent input/stop RPC or per-thread currency-cost field.
  Fleet Dash exposes the corresponding parent-mediated/unavailable states and never substitutes zero
  as a measurement.
- Codex Plan/Default mutation currently uses an experimental App Server method. It is verified
  against the installed CLI and isolated in the adapter, but may require an adapter update if Codex
  changes that experimental protocol.
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
