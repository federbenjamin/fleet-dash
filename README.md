# fleet-dash

A local web dashboard for Claude Code and Codex CLI sessions across all
projects/worktrees, their subagents, live token usage, and — the headline feature —
**remote interaction**: pending questions render as tappable buttons and answers are sent through
the provider's native control path. Built 2026-07-13; still evolving.

**Dashboard:** http://127.0.0.1:8377 (always on — launchd daemon, starts at login)

## What it shows

- **Now is an operations queue:** **Fleet Briefing** appears first, then **Pinned** sessions and one deduplicated **Action
  inbox** for questions, approvals, MCP forms, explicit reply requests, intervention errors, and
  unreviewed completed work. **Working** and **Available** session cards follow; empty groups collapse
  while Available retains a small empty state. Action rows show provider, access, reason, age, and
  delivery state, then open the same full-chat response controls used everywhere else. Safe bulk
  triage is limited to review/available markers, mute, and dismissal of reviewable notices—never an
  unresolved provider request and never approval. The
  separate **History** destination owns dormant, external, reopenable, and closed sessions. Cards
  use reasons such as **Reply requested**, **Command
  approval**, **Working elsewhere**, and **Inactive** instead of raw provider lifecycle terms.
  The complete classification and action contract is in
  [`docs/session-organization.md`](docs/session-organization.md). The sticky command box carries the
  distinct-session counts for **Needs you**, **Working**, and **Available** instead of repeating a
  totals line. Its **Subagents** filter opens a flat active-child view with each parent breadcrumb,
  model, state, and latest activity. Each card is headed by the
  session's AI tab title (same string as your iTerm tab), with project · branch beneath. On an open card the header
  pins to the top of the screen while you scroll the card body (collapse from anywhere), and
  scrolls away past the card's end.
- **Responsive application navigation:** desktop uses a persistent rail for Now, Search,
  Workstreams, History, Insights, and Settings. At 390×844 and other narrow widths it becomes a
  fixed bottom bar; Insights and Settings live under More. The URL hash preserves destinations
  across refresh and browser/native back gestures. Settings places the desktop rail on the left or
  right per browser; mobile always keeps the bottom bar. Now and Workstreams have sticky text/state
  filters whose named saved views remain on this device.
- **Lightweight Workstreams:** sessions are grouped by canonical Git repository; linked worktrees
  roll into the main repository while keeping their branch and worktree labels. Non-Git folders use
  canonical cwd, missing or unknown locations stay separate, and symlink/nested-repository cases do
  not merge unrelated work. Each group shows state counts, providers, branches, current context,
  measured or partial cost, latest outcome, and its filtered sessions. Git changes, tests, PR state,
  and budgets say **not observed/not configured** until their later evidence systems measure them.
  Repository grouping is loaded through `/api/workstreams` only while that destination is open, so
  it does not enlarge or delay the two-second `/api/fleet` poll.
- **Incremental global search:** Search covers every retained Claude and Codex main transcript,
  saved subagent transcript, session metadata, and provider-referenced text artifact on this Mac —
  including sessions Fleet did not create. Provider, project, and event-type filters narrow results;
  each hit opens bounded exact context and can jump to the live session, known subagent, or safe
  artifact preview. An isolated low-priority worker maintains `search.db` with SQLite WAL/FTS5, so
  initial indexing and transcript updates do not block the provider poll or `/api/fleet`. Progress,
  parser/file warnings, and a confirmed rebuild control are visible on the page. Search is action-
  token protected because it exposes unmanaged local transcripts.
- **⚙ settings** (desktop rail or mobile More): an install-and-delivery rail distinguishes browser
  install, notification permission, and registered-device health. It can enable/repair a Web Push
  subscription, rename or pause this device, disconnect it, and eventually send a real test push.
  Subscription endpoints and encryption keys are write-only; the UI receives only redacted health.
  The same page retains per-category toggles for the legacy ntfy pushes (waiting-on-you,
  stalled, spend threshold, fleet quiet), **their thresholds** (blocked seconds, stall
  seconds — this one also drives the "stalled" chip, $ step, fleet-idle minutes), and the
  **push tap-target** (`dashboard_url` — set it to your Tailscale URL and tapping a
  notification opens the dashboard). It also selects the per-device desktop navigation side and
  **Fit the screen** or **Centered · fixed width** for every full-screen reading surface. Server
  settings persist to `config.json`; the navigation side stays in that browser.
- **🔔 per-session mute** on every card header (works collapsed): 🔕 silences that session's
  pushes (waiting/stalled/spend) without touching the fleet-wide categories. Mutes persist
  across daemon restarts until manually unmuted.
- Card headers stay lean: the $ total appears only on an open card; done-agent count and
  agent spend live in the detail panel ("completed agents", "session info"), not the header.
  The running-agent count stays visible everywhere.
- **➕ new coding session** (button under the live list): choose Claude Code or Codex CLI, then
  pick a directory (recent ones the daemon has seen, or type a path under `~`), a model, and an
  effort level (`low`…`max`). Codex sessions also choose Plan or Default mode and start in Plan
  by default. Claude sessions choose Manual, Auto, Accept Edits, Plan, or the advanced Don't Ask
  permission mode; Auto remains subject to Claude's account/model eligibility. Claude sessions can
  request a **new git worktree** — it opens
  a fresh iTerm tab running `claude` with those flags. Fleet immediately opens a provisional
  card and full chat with the initial message and a startup spinner, then replaces it in place with
  the exact native session. A rejected start keeps the exact setup available to retry or restore.
  Model changes paint immediately while stale forecast requests are cancelled or ignored.
  Untrusted folders are flagged: Claude Code asks "do you
  trust the files in this folder?" at startup and **only your Mac can answer that** — trust is
  inherited from a parent dir, so worktrees under a trusted repo start clean.

## Codex CLI integration

Fleet Dash owns one detached App Server as the canonical Codex runtime. It starts the documented
`codex app-server --listen unix://…` transport, then connects as one client through the documented
WebSocket-over-Unix protocol at `~/.claude/fleet-dash/codex-app-server.sock`. The detached listener
survives a Fleet web daemon restart and is reused instead of duplicated. (`codex app-server daemon
start` is not used: that manager requires Codex's standalone installer, while this machine uses the
npm CLI.) Fleet never scrapes the Codex TUI. App Server remains the only control surface. For an
explicitly pinned external thread, Fleet may defensively observe a small allowlist of lifecycle and
visible-message events in its local `~/.codex/sessions` rollout so the view-only card can track work
that the separate Desktop/VS Code App Server reports only as `notLoaded`.

- Threads created by Fleet Dash are remembered in `codex_threads.json`, including their runtime
  ownership, mode, and last normalized conversation, and resume after daemon restarts. Every new
  Fleet Codex session immediately sends a visible, normal `hi` turn. That creates the rollout the
  TUI needs instead of leaving an empty, unresumable thread shell. **Attach** stays disabled as
  **turn active** until that bootstrap turn finishes because resuming an active thread aborts its turn.
  A pre-bootstrap shell is retained only while Fleet's App Server still reports it loaded; if both
  that runtime state and the rollout are absent, Fleet removes the unusable ghost card.
- A terminal started with
  `codex resume --remote unix://$HOME/.claude/fleet-dash/codex-app-server.sock <thread-id>`
  is another client of that same runtime. Fleet adopts socket-attached CLI threads and can steer the
  active turn without resuming a second agent. The card's **attach** button opens this TUI form.
- ChatGPT Desktop and Codex VS Code threads use a different App Server. Fleet discovers their
  transcripts through paginated `thread/list`, puts active work under **Working** and inactive work
  in **Session history**, and exposes them as view-only. Pinned external threads also observe local
  `task_started`, `task_complete`, `turn_aborted`, user-message, and agent-message rollout events, so
  their state and preview stay current without claiming control. Unknown/malformed rollout additions
  are ignored with a visible observation warning. There is deliberately no **take over** action: `thread/resume`
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
  total come directly from App Server account APIs and appear in the on-demand Usage panel. The unused
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
- **Needs you** includes native questions/approvals and ordinary assistant prose that directly asks
  for a reply. Opening prose does not dismiss it: replying or choosing **Mark available** does.
  Completed non-question turns remain **Available** and show **new** until opened.
- **History** is one flat chronological destination for dormant, inactive external, reopenable, and
  closed sessions. Search it by title/project/message, then combine Access chips (All, Continue,
  View only, Reopen) with Provider chips (All, Claude, Codex). Dormant means no active turn and no
  recent activity; it is a diagnostic raw state, not a separate page section. Fleet indexes every
  surviving top-level Claude transcript under `~/.claude/projects` on startup, including sessions
  from before Fleet was installed. Saved subagent transcripts remain inside their parent
  conversation instead of becoming duplicate history rows.
- Provider-wide failures appear once as a banner. Fleet preserves the last known placement instead
  of turning every session into a duplicate error card.
- **Usage chip** (inside the Now command box): it normally reads only **Usage**. At 70% it shows the
  most urgent selected account/window percentage in amber; at 90% it turns red. Tapping opens every
  provider/account gauge in a desktop popover or mobile sheet. Provider, email, and plan details use
  middle-dot separators. When Claude Usage is installed, Fleet mirrors its selected profiles,
  active-account marker, 5-hour/weekly/Fable-weekly gauges, visibility setting, and live file updates. Fleet reads
  only display-safe identity/quota fields from the app preferences; its stored credentials never enter
  the Fleet API. Without that app, the current Claude Code login and statusline `rate_limits`
  side-write remain the single-account fallback. The local lifetime-token total comes from
  `~/.claude/stats-cache.json` and is shown once because it aggregates every retained main and saved-
  subagent transcript on this Mac across profiles. It excludes deleted history, other computers, and
  claude.ai activity. Codex uses App Server `account/read` for the signed-in email and plan, plus every
  rate-limit bucket, reset time, account lifetime tokens, and available reset-credit count.
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
  height (1–6; defaults: sessions on at 2 lines, subagents off at 1). Fleet sends at most 500
  characters of the latest session message. That line setting also fixes the height of ordinary
  collapsed session cards, so short/missing messages and poll updates do not move the list.
  Open cards, explicitly expanded peeks, and cards with questions, errors, inline feedback, or
  running subagents grow to fit those controls. Overflow replaces the final collapsed row with a
  clickable `...`; expanding reveals the full bounded 500-character preview. Tapping a
  subagent's peek opens that agent's chat. A card blocked on a QUESTION shows no peek — the ask
  is the context.
- **Running subagents inline** (type, description, model, throughput, sparkline, live $). The
  `tok/s` figure is throughput — tokens per second the agent is processing, **cache reads
  included** — so it is a liveness signal (is it moving?), not output speed; a big context makes
  it large.
- **Full-chat status strip** directly above the main or subagent composer. Desktop shows branch
  versus the last-fetched `origin/main`, worktree, model/effort, context and explicit compaction
  headroom, cache-read hit rate, CacheWrite/spikes/peak, session-tree or child cost, turn cost, and
  the last 50 changed CacheWrite values. Mobile starts with the two identity/context rows and
  expands usage details on tap. Missing provider data is omitted; completed agents and closed
  sessions keep their last known values. Tapping a main tree total opens the main-plus-children
  breakdown. No Git fetch or transcript rescan occurs when the strip opens.
- **Model · effort** wherever a model is shown (`opus · high`). Effort lives only in the
  statusline payload, so `statusline-command.sh` side-writes it per session for the daemon; a
  session whose statusline hasn't rendered yet shows the model alone. Subagent effort comes from
  the agent definition's frontmatter pin, or the parent session's effort when it pins none.
- A Claude card's whole header opens Fleet chat; the redundant second chat button is gone. **Terminal**
  (desktop only) brings that Claude iTerm tab to the front. A managed
  Codex card shows **Attach**, which opens a new Codex TUI connected to the canonical shared runtime.
  External Codex cards show disabled **view only** because their Desktop/VS Code runtime is separate.
  The same open/attach/view-only control appears immediately left of the ⋮ menu in full-screen chat.
- **Pin sessions to a watchlist at the top:** pinning lifts the full card into a
  **📌 pinned sessions** block directly below Fleet Briefing. Pinned cards keep the order in which
  they were pinned; a new pin appends at the bottom, and urgency/activity changes do not move it.
  Cards are relocated rather than duplicated. On **desktop**, use the
  contained 📌 button immediately to the right of **open/attach/view only** in the session header; on
  **mobile**, **long-press** the header (it highlights immediately; a short tap still opens
  its chat). Pins persist in server settings across reloads, daemon restarts, and devices. A failed
  pin restores the prior order and stays visible with Retry. Pinning an
  external Codex thread also opts it into read-only local lifecycle/message observation; it does not
  make the thread interactive.
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
  Its top-right **⋮ menu** contains Codex Plan/Default or Claude permission mode (when applicable),
  light/dark mode, Stop turn, and Close session. Stop and close both confirm first. Closing an active session stops its current
  turn and subagents, then archives a Codex thread or terminates only the registered Claude process;
  Claude's iTerm tab remains open. A secondary Git worktree can be preserved or removed after close;
  the branch and primary worktree are never removed. Dirty removal is a separate red confirmation
  that lists changed, untracked, and ignored files, and cleanup is blocked while another live Fleet
  session uses that worktree. The conversation moves to **History**. The card's bounded Markdown
  peek remains the scanning surface; full view is for actually reading and working a session.
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
- **Why this is here** appears in every expanded card and as a **Why here?** control in full chat.
  It shows the exact classifier rule, provider/CLI signal, pending work, latest transcript event,
  activity age, ownership, stale status, confidence, and any lower-priority rules that were
  suppressed. Full chat opens a desktop side rail or mobile in-place sheet and pages durable
  placement transitions from `GET /api/evidence`; repeated polls do not create duplicate history.
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
  A sent message appears there immediately with a small sending spinner. The placeholder is
  replaced only when the provider transcript confirms it. A failed request, or one still
  unconfirmed after 15 seconds, gets a red `!`; tapping it restores the text to the composer and
  never retries automatically. Message and subagent-relay composers are multiline: **Return adds a
  newline**, **Command-Return sends on macOS**, and **Control-Return sends elsewhere**; the explicit
  Send/Relay button remains available. Structured-question answers use the selected option labels and the
  same placeholder behavior (secret free text is shown only as “private answer”). The owning card
  on the main fleet page also shows a compact **Submitting / Submitted / Failed** receipt for
  question answers and inline quick responses such as permissions, dismissals, and MCP forms.
  Key tool calls appear inline terminal-style as a single `● Edit(path)` line —
  Edit/Write/Bash/Agent/Skill/SendUserFile only; read-only chatter (Read/Grep/Glob) is hidden.
  The buffer keeps the last ~120 entries per session.
- **Delivered files**: anything the session sent you via SendUserFile appears as a tappable chip
  (📄 md/text, 🖼 images) **inline in the conversation at the point it was delivered**, with its
  caption — so the message explaining the file sits right with it. The detail panel also has a
  "delivered files" dropdown (caption + delivered-ago). Chips open a full-screen viewer with
  markdown rendered and images inline; viewing contents requires the act token (same `?token=`
  opt-in); files since deleted show "(gone)".
- **File viewer** extras: its slim toolbar shows only close, filename, and file/session actions—no
  duplicate session title/project/model header or separator. The same **⋮ menu** exposes light/dark mode, Codex mode, stop, and close
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
- **History destination:** inactive sessions plus every surviving top-level Claude transcript
  (title, provider, project, state, and age), loaded 100 rows at a time. Search and combine Access
  and Provider filters. A closed row always has **View**. It also has **Reopen** when its exact
  transcript and original working directory still exist; Reopen starts `claude --resume <id>` in a
  new iTerm tab. Tap the row itself for its info block. Session ids, cwds and agent ids in any info
  block are **tap-to-copy**.
- **Insights destination** (7/30/90-day window; each subsection its own dropdown): where
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
only writes AskUserQuestion rows to the transcript *after* they're answered. Hook evidence is
immediate; a bare registry `waiting` flag is confirmed for 3 seconds because Claude can flash it
between progress prose and the next tool call. Answers are injected
by `FleetDashInjector.app` (a TCC-authorized applet: daemon writes a request file, `open -g`, the
applet types into the iTerm session matched by tty). Finished agent runs and closed sessions are
recorded in `ledger.db` (sqlite). A separate low-priority `search_index.py --worker` process
incrementally indexes Claude/Codex transcripts and provider-referenced artifacts into `search.db`;
the HTTP process uses a separate WAL reader for authenticated search and exact-context requests.
Fleet is also an installable PWA. Its root-scoped service worker caches only versioned public shell
assets. API responses, transcripts, notification data, settings, and token-bearing navigation stay
network-only; offline navigation renders only **Reconnect to your tailnet**.

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

## Enabling phone use

1. Install **Tailscale** on the Mac and phone, sign both into your tailnet.
2. On the Mac: `tailscale serve --bg 8377` → gives an HTTPS URL like
   `https://<mac-name>.<tailnet>.ts.net`.
3. On the phone, open that URL once with `?token=<act_token>` appended (get it via:
   `python3 -c "import json;print(json.load(open('$HOME/.claude/fleet-dash/config.json'))['act_token'])"`).
4. In Safari, Share → **Add to Home Screen**. Open the installed Fleet app, then open Settings →
   **Fleet app & Web Push**. Notification permission is requested only from the explicit Enable
   button. Desktop browsers can use **Install Fleet** when they expose the install prompt.
5. During the dark N2 migration the shell and device registration are present, while the Settings
   rail says **Server setup pending** until the N3 VAPID/delivery worker is configured. The existing
   ntfy transport remains available during this migration; it is not an automatic Web Push fallback.

## Config (`config.json`)

| key | default | meaning |
|---|---|---|
| `poll_seconds` | 2 | scan cadence |
| `codex_enabled` | true | start the Codex App Server adapter |
| `codex_command` | "" | optional absolute Codex executable path; auto-detected from PATH or `~/.nvm` |
| `search_enabled` | true | start the isolated local transcript indexer and authenticated Search APIs |
| `search_discover_seconds` | 2 | filesystem discovery cadence for new/changed transcript sources |
| `search_batch_rows` | 250 | bounded JSONL rows committed per worker batch |
| `stall_seconds` | 240 | frozen-mid-turn threshold (long Bash gates freeze transcripts!) |
| `dormant_seconds` | 7200 | quiet sessions demote to dormant |
| `turn_done_window_seconds` | 900 | how long "done ✓" persists before fading to idle |
| `awaiting_input_notify_seconds` | 180 | blocked-on-you push debounce |
| `spend_threshold_usd` | 5 | per-session push threshold (fires per multiple) |
| `question_file_pair_seconds` | 300 | max age of a delivered file to pair as "read first" on a question |
| `notify` | all true | per-category push toggles (needs_you/stall/spend/fleet_quiet) — the ⚙ panel edits this |
| `fleet_quiet_minutes` | 0 | how long the fleet must stay fully idle before the quiet push (0 = on transition) |
| `muted_sessions` | {} | session_id → mute-ts map behind the 🔔 card toggle; persists until manual unmute |
| `pinned_sessions` | [] | persisted session ids relocated into the Pinned section in stable pin order; new pins append at the bottom |
| `reply_available` | {} | session id → conversation revision explicitly marked available |
| `read_sessions` | {} | session id → opened conversation revision for the New response badge |
| `rates` | — | $/1M by family. **`fable` is a PLACEHOLDER (opus rates) — fix when published** |
| `permission_keys` | 1/2/Esc | keystrokes for allow/always/deny (empty value = Esc) |
| `dashboard_url` | "" | ntfy `Click` target — tapping a push opens this URL (⚙ panel edits it) |
| `ntfy_server`/`ntfy_topic` | ntfy.sh / fleet-… | push channel (empty topic = disabled) |
| `web_push_public_key` | "" | public VAPID key projected to authenticated devices; private VAPID material never belongs in `config.json` |
| `web_push_allowed_origins` | [] | optional exact HTTPS push-service origins added by the local operator; never client-supplied |
| `act_token` | generated | device token for the act endpoint |

Apply config/engine changes with: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`
(dashboard/static asset changes need no restart — open tabs self-reload). Log: `fleet-dash.log`.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/search_benchmark.py
npm install
npx playwright install chromium
npm run test:browser
```

The Python suite includes a deterministic fake App Server transport plus adapter and shared-engine
fixtures. Playwright runs the same provider/UI matrix at desktop and 390×844 mobile sizes. Scripts
named `tests/live_*_smoke.py` are opt-in checks against the running daemon; paid-turn scripts say so
in their docstring and archive threads they create. `tests/search_benchmark.py` creates a disposable
100k-message/2k-source corpus and enforces the warm, cold, and append-lag search gates.

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
  so it can't be used as a completion signal either. Explicit task notifications with terminal
  `completed`/`killed`/`failed` status are used when present, but only until newer child output
  proves that task id resumed.
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
- The markdown viewer is a minimal built-in renderer (headings, lists, tables, code, quotes,
  links) — exotic markdown falls back to plain paragraphs. Non-md text files show raw.
- A closed conversation is read-only until it is reopened. Every surviving Claude main transcript
  can be viewed; Reopen is offered only when the exact UUID transcript and original working
  directory pass Fleet's local safety checks. Deleted transcripts and missing working directories
  remain View-only or unavailable. Subagent chats are reachable only while their parent session is live.
- Messaging a subagent is a **relay through the parent**, never a direct channel — there is no
  such thing as typing into a subagent (no tty; `SendMessage` from the parent is the only path).
- Screen-peek ("show me what this stalled session's terminal displays") is proven as a technique
  (`contents of session` osascript) but not built into the UI yet.
- Fable pricing placeholder; ledger has no backfill from pre-daemon transcripts.
