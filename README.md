# fleet-dash

A local web dashboard for Claude Code and Codex CLI sessions across all
projects/worktrees, their subagents, live token usage, and — the headline feature —
**remote interaction**: pending questions render as tappable buttons and answers are sent through
the provider's native control path. Built 2026-07-13; still evolving.

**Dashboard:** http://127.0.0.1:8377 (always on — launchd daemon, starts at login)

## What it shows

- **Now is an operations queue:** **Pinned** sessions appear first, then one deduplicated **Action
  inbox** for questions, approvals, MCP forms, explicit reply requests, intervention errors, and
  unreviewed completed work. **Working** and **Available** session cards follow; empty groups collapse
  while Available retains a small empty state. Action rows show provider, access, reason, age, and
  delivery state, then open the same full-chat response controls used everywhere else; there are no
  checkboxes, bulk actions, or duplicate right-side navigation buttons. The
  separate **History** destination owns dormant, external, reopenable, and closed sessions. Cards
  use reasons such as **Reply requested**, **Command approval**, **Working**, and **Inactive**
  instead of raw provider lifecycle terms. A separate **View only** access label identifies sessions
  owned by another runtime. An archived external thread is removed from Fleet inventory rather than
  retained as view-only inventory.
  The complete classification and action contract is in
  [`docs/session-organization.md`](docs/session-organization.md). The sticky command box carries the
  distinct-session counts for **Needs you**, **Working**, and **Available** instead of repeating a
  totals line. On desktop the nav-rail footer carries per-window usage bars (the accounts flyout
  opens beside the rail); on mobile the Now header keeps a compact summary chip such as
  **Usage · Claude 23/8 · Codex 14**. Each card is headed by the
  session's AI tab title (same string as your iTerm tab), with project · branch · provider beneath;
  a 118px meta rail on the right carries status, model, context %, quiet time, and live agent
  count ("Console" design system — see `design-system/`). Pin/unpin is right-click on the header
  (desktop) or long-press (mobile); pinned cards show a `⌖ pinned` marker.
- **Notifications is the durable interruption desk:** **Needs action**, **Updates**, **Snoozed**,
  **Problems**, **Briefing**, and **History** are views over one canonical event stream. The rail and
  mobile tab show active/unread counts; opening a row loads its current exact state before marking it
  read. Event detail supports 15-minute, one-hour, and tomorrow snooze, early wake, session mute until
  manual unmute, delivery retry, and expired-device reconnect. Exact links use
  `#notifications/<event-id>` and participate in refresh and browser/native back. Desktop keeps a
  split list/detail view; mobile opens detail as a full-height drawer. Briefing now lives here instead
  of competing with the live Action Inbox on Now. Every event type has two independent controls:
  **Show in Fleet Notification Center** governs in-app history/badges, while **Web Push** governs
  lock-screen delivery and cadence.
- **Responsive application navigation:** desktop uses a persistent rail for Now, Notifications,
  Search, Workstreams, History, Insights, and Settings. At 390×844 and other narrow widths it becomes
  a fixed bottom bar; History, Insights, and Settings live under More. The URL hash preserves destinations
  across refresh and browser/native back gestures. Settings places the desktop rail on the left or
  right per browser; mobile always keeps the bottom bar. Now and Workstreams have sticky text/state
  filters whose named saved views remain on this device. Settings and History paint their visible
  destination immediately, then do heavier rendering/fetch work on the next frame. Returning to
  History preserves the loaded page; only **Show more** fetches the next 100 sessions. New Session
  and notification actions paint local feedback without rebuilding the fleet or Notification Center.
- **Lightweight Workstreams:** sessions are grouped by canonical Git repository; linked worktrees
  roll into the main repository while keeping their branch and worktree labels. Non-Git folders use
  canonical cwd, missing or unknown locations stay separate, and symlink/nested-repository cases do
  not merge unrelated work. Each group shows state counts, providers, branches, current context,
  measured or partial cost, latest outcome, and its filtered sessions. Git changes, tests, PR state,
  and budgets say **not observed/not configured** until their later evidence systems measure them.
  Repository grouping is loaded through `/api/workstreams` only while that destination is open, so
  it does not enlarge or delay the two-second `/api/fleet` poll. Recognized GitHub repositories expose
  one external **GitHub ↗** link; Fleet does not duplicate GitHub repository or pull-request pages.
- **Durable local drafts:** unsent composer, relay, handoff, scheduling, new-session, question-other,
  and filter text survives polling, navigation, and reloads on that device. A draft clears only when
  its action is accepted for sending or the user deletes the text. Password/secret answers are never
  persisted. If Fleet is known to be offline, ordinary messages pressed Send enter a bounded local
  queue, appear immediately as **Queued offline**, and send in order after a live fleet poll confirms
  reconnection. While online, the same Send button sends immediately only when the exact session can
  accept the message now; otherwise Fleet saves it durably in the server Outbox and shows
  **Queued · waiting for session** until that session becomes available. A **Needs you** card caused
  only by the assistant asking for a reply remains immediately writable; that placement does not
  mean the provider is busy. If a native structured question is visibly open, normal composer Send
  declines that exact question first and then durably queues the typed text/images as the follow-up;
  it can never type the message into the option selector. A changed or rejected question keeps the
  draft untouched, and an offline queued follow-up remembers the question nonce through reconnection.
  Commands and skills remain immediate-only and stay as drafts while
  offline. A brief provider-discovery gap after Fleet restarts leaves exact-session messages waiting
  for up to two minutes instead of falsely declaring the session gone. The full-chat composer also accepts up to four
  JPEG, PNG, GIF, WebP, HEIC, or HEIF images at 10 MB each. Image drafts survive reloads in private
  device storage, can queue offline with their message, and are removed locally after delivery or
  after 24 hours. In full chat, picture and scheduled-send actions live in the upward **＋** menu.
  On phones the composer stays docked immediately above the keyboard, the full-screen view follows
  the visible iOS viewport, and a vertical drag on conversation history dismisses the keyboard.
  Fleet also keeps a bounded last-good cache of recently opened session, closed-session, and
  subagent conversations on that device. A failed refresh or offline reload shows saved history as
  stale instead of replacing it with an empty/error screen; refreshed tails merge with older pages.
- **Incremental global search:** Search covers every retained Claude and Codex main transcript,
  saved subagent transcript, session metadata, and provider-referenced text artifact on this Mac —
  including sessions Fleet did not create. Provider, project, and event-type filters narrow results;
  each hit opens bounded exact context and can jump to the live session, known subagent, or safe
  artifact preview. An isolated low-priority worker maintains `search.db` with SQLite WAL/FTS5, so
  initial indexing and transcript updates do not block the provider poll or `/api/fleet`. Progress,
  parser/file warnings, and a confirmed rebuild control are visible on the page. Search is action-
  token protected because it exposes unmanaged local transcripts.
- **One session workspace:** every live or historical session opens at a stable
  `#session/<sid>/chat` route with persistent **Chat**, **Files**, **Subagents**, and **Details**
  sections. On wide desktops (≥1200px) the workspace is a persistent **docked right pane** beside
  the queue — tapping any session opens it there without covering Now; the splitter between the
  panes drags (650–1200px, persisted), and ⤢/⤡ toggles an expanded view that keeps the nav rail
  and centers the chat column. Narrow windows and phones keep the full-screen workspace. Files and agents have opaque, directly reloadable selection routes; local paths never
  appear in the URL or context response. The Subagents section starts with **Active** enabled on
  every initial open, preserves spawn order and required ancestors, and offers **All** for terminal
  agents; its tab count includes active agents only. On desktop the Files and Subagents list dividers
  are draggable or keyboard-resizable and their separate widths persist in that browser. One
  contextual composer and one pending-request drawer serve the whole workspace at a stable height.
  On mobile, horizontal swipes move between adjacent sections without wrapping; a right swipe that
  starts at the left edge exits the workspace. Horizontally scrollable readers keep their own gesture.
  Main-agent activity appears as the newest non-interactive Chat row. Closed
  sessions show explicit retained/unavailable states; eligible exact-session resumes use a
  text-only, idempotent first send, while external Codex threads remain view-only.
- **⚙ settings** (desktop rail or mobile More): an install-and-delivery rail distinguishes browser
  install, notification permission, and registered-device health. It can enable/repair a Web Push
  subscription, rename or pause this device, disconnect it, and queue a real test push without
  making the Settings request wait for the push provider.
  Subscription endpoints and encryption keys are write-only; the UI receives only redacted health.
  A separate **Legacy ntfy** panel can send one generic manual test when explicitly enabled and
  configured. It has no automatic categories, fallback, duplicates, content-bearing payload, or
  tap target. The page also selects the per-device desktop navigation side and
  **Fit the screen** or **Centered · fixed width** for every full-screen reading surface. Server
  settings persist to `config.json`; the navigation side stays in that browser.
- **🔔 per-session mute** on every card header (works collapsed): 🔕 silences that session's
  Web Push deliveries without hiding its canonical in-app notifications. Mutes persist
  across daemon restarts until manually unmuted.
- Card headers stay lean: the $ total appears only on an open card; done-agent count and
  agent spend live in the detail panel ("completed agents", "session info"), not the header.
  The running-agent count stays visible everywhere. The whole header opens Chat (and is keyboard
  accessible); the pin remains an independent control. Active-subagent previews contain only real
  agent rows—there is no placeholder row when only one agent is active.
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

Production Fleet, ordinary interactive `codex` terminals, and Codex Remote Control are clients of
Codex's managed App Server daemon on its default control socket at
`~/.codex/app-server-control/app-server-control.sock`. Fleet starts the idempotent manager with
`codex app-server daemon start`, enables Remote Control, and connects through the documented
WebSocket-over-Unix protocol. The daemon command is visible in npm builds, but Codex requires the
official standalone installation at runtime; if it is missing or rejected, Fleet leaves the private
source and terminal routing untouched and reports the migration blocker. Fleet never scrapes the
Codex TUI. Staging remains isolated on its
private `~/.claude/fleet-dash-staging-state/codex-app-server.sock` listener and explicit `--remote` routes
are never rewritten. For an
recently updated external thread, Fleet may defensively observe a small allowlist of lifecycle and
visible-message events in its local `~/.codex/sessions` rollout so the view-only card can track work
that another runtime reports only as `notLoaded`. Observation is limited to the 32 newest external
threads updated within 24 hours, plus explicitly pinned external threads.
External sessions retain the ordinary **Working** and **Available** lifecycle labels; the separate
**View only** access label communicates that Fleet cannot control their runtime.

- Threads created by Fleet Dash are remembered in `codex_threads.json`, including their runtime
  ownership, mode, model, effort, and last normalized conversation, and resume after daemon
  restarts. Codex's `thread/list` and `thread/read` responses omit model and effort, so Fleet uses
  those saved selections after a daemon restart or compaction instead of losing the ability to send
  the next Plan-mode turn. Every new
  Fleet Codex session immediately sends a visible, normal `hi` turn. That creates the rollout the
  runtime needs instead of leaving an empty, unresumable thread shell. Fleet never starts or resumes
  a TUI for the thread: `thread/resume` can abort an active turn and can create a second runtime
  agent when ownership is wrong.
  A pre-bootstrap shell is retained only while Fleet's App Server still reports it loaded; if both
  that runtime state and the rollout are absent, Fleet removes the unusable ghost card.
- Fleet installs an idempotent launcher at `~/.local/share/fleet-dash/bin/codex` and one marked PATH
  block in `~/.zshrc` (with a one-time backup). It routes interactive `codex`, `resume`, `fork`, and
  archive commands through `--remote unix://`; admin and noninteractive subcommands pass through to
  the current real executable. Explicit `--remote` arguments also pass through unchanged. This means
  a normal terminal session started in any directory is automatically another client of the same
  managed runtime. Fleet adopts daemon-loaded CLI threads and can steer the
  active turn without resuming a second agent. If that exact command is running on one real TTY,
  Fleet can send through and focus the existing terminal when App Server turn authority is absent.
  Exact App Server authority wins when both routes exist. During compaction, Fleet follows the
  provider's replacement turn ID from either the current `contextCompaction` item or the legacy
  compacted notification, so messages continue steering the same active turn after compaction instead
  of landing in a stale terminal input. When that exact command is still running on one real TTY,
  the card shows **Open** to focus it. Otherwise Codex has no terminal control.
  App Server's raw `source` label is diagnostic only: it can report `vscode` for a remote CLI.
  Fleet adopts a thread when the managed daemon's exact `thread/loaded/list` proves it is loaded,
  persists that runtime ownership, and reports normalized managed/external provenance separately.
- Existing Fleet-owned private-runtime threads migrate automatically. Fleet first preserves a
  `codex_threads.json.pre-managed-daemon.bak`, waits for active turns, requests, compaction,
  incomplete bootstrap, and attached legacy terminals to drain, compares every owned thread's
  canonical history fingerprint on both runtimes, then SIGTERMs only the exact same-UID private
  listener. Metadata commits only after target verification; corrupt state or any mismatch leaves
  the source authoritative. Migration status is visible in the provider diagnostics and never holds
  Claude's scan or action locks.
- ChatGPT Desktop and Codex VS Code threads use a different App Server. Fleet discovers their
  transcripts through paginated `thread/list`, puts active work under **Working**, recently inactive
  work under **Available**, and sessions beyond the configured inactivity threshold (2 hours by
  default) in **Session history**. They
  remain view-only. The 32 newest external threads updated within 24 hours, plus pinned external
  threads, observe local `task_started`, `task_complete`, `turn_aborted`, user-message, and
  agent-message rollout events, so their state and preview stay current without claiming control.
  Unknown/malformed rollout additions are ignored with a visible observation warning. There is
  deliberately no **take over** action: `thread/resume`
  on Fleet's server would create a second runtime copy, not attach to Desktop's active agent.
  Independently launched CLI threads that are not connected to Fleet's socket are likewise view-only.
  Child subagent threads never become duplicate top-level cards.
- Conversation history, prompt submission, interruption, and approval decisions use App Server
  thread/turn APIs. A transient `thread/read` failure keeps the last conversation and leaves a
  Fleet-owned thread interactive; a provider-wide list failure keeps its last placement and
  controls under one stale-data banner. Lifecycle timeouts recycle only Fleet's client connection,
  not the shared runtime. Stale rows disable direct mutation while still allowing durable queueing
  where ownership is proven. Archive discovery pages up to a bounded 1,000 threads; persisted
  Fleet-owned IDs missing from that window receive targeted reads before they can vanish. Detail
  reads use bounded concurrency and one total refresh budget, then back off after a failure. Unknown
  or external IDs cannot gain mutation or unprojected context/file access by supplying a UUID.
- Model choices come from Codex's bounded, credential-free `~/.codex/models_cache.json` catalog.
  Fleet does not call `model/list` during session refresh, so a slow model-manager refresh cannot
  block thread health or detach live chats.
- Codex thread IDs are stored as `codex:<native-id>` so they cannot collide with Claude IDs.
- Codex costs display as unavailable rather than being priced with Claude rates. App Server's
  exact per-thread token total and model context-window size drive each card's context gauge;
  the read-only rollout observer supplies the same fields, model, and effort for external sessions;
  when a live token update omits the window, Fleet uses the selected model's declared local
  catalog window. Cards show only the context-fill gauge, while the detail view shows used and
  total context tokens.
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
  labelled that way. Active counts are reconciled against each child thread's canonical turn status;
  a completed child stays completed even when the parent history contains only older started/activity
  events. Per-agent tokens are shown only when App Server supplies them; currency cost and throughput
  remain unavailable instead of displaying fabricated zeroes.
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
  An unloaded external view-only thread is the exception: Fleet clears a prose-only request after
  30 minutes because it cannot submit a reply to that runtime.
  A completed-work handoff with a concrete summary or verification enters the Action Inbox as
  **Completed work is ready to review / Unreviewed** until opened. Progress prose and interrupted
  turns do not. Each Action Inbox row opens directly; it has no checkbox, bulk action, or duplicate
  View/Respond control. Other completed non-question turns remain **Available** without entering
  the Action Inbox.
- **History** is one flat chronological destination for dormant, inactive external, reopenable, and
  closed sessions. Search it by title/project/message, then combine Access chips (All, Continue,
  View only, Reopen) with Provider chips (All, Claude, Codex). Dormant means no active turn and no
  recent activity; it is a diagnostic raw state, not a separate page section. Fleet indexes every
  surviving top-level Claude transcript under `~/.claude/projects` on startup, including sessions
  from before Fleet was installed. Saved subagent transcripts remain inside their parent
  conversation instead of becoming duplicate history rows.
- Provider-wide failures appear once as a banner. Fleet preserves the last known placement instead
  of turning every session into a duplicate error card.
- **Usage entry** (desktop: nav-rail footer bars · mobile: Now-header chip): it shows the active
  Claude account's 5-hour/weekly values
  and the highest active non-Spark Codex window, for example **Usage · Claude 23/8 · Codex 14**.
  At 70% an active account/window turns it amber; at 90% it turns red. Inactive Claude profiles do
  not color the summary, but retain their own gauge colors inside the panel. Tapping opens every
  provider/account gauge in the accounts flyout (pinned beside the rail on desktop; a sheet under
  the chip on mobile). Provider, email, and plan details use
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
  height (1–6; defaults: sessions on at 2 lines, subagents off at 1). Fleet sends at most 800
  characters of the latest session message. That line setting also fixes the height of ordinary
  collapsed session cards, so short/missing messages and poll updates do not move the list.
  Open cards, explicitly expanded peeks, and cards with questions, errors, inline feedback, or
  running subagents grow to fit those controls; their collapsed message peek still reserves the
  configured number of lines, so a short message does not leave a different-sized hole. Overflow
  replaces the final collapsed row with a clickable `...`. Only a truncated peek responds to a
  whole-row tap; a fully visible collapsed peek is inert. Once expanded, the exposed content is
  inert too—use **Less** to collapse it. Expansion reveals the full bounded 800-character preview. Tapping a
  subagent's peek opens that agent's chat. A card blocked on a QUESTION shows no peek — the ask
  is the context.
- **Complete messages in full chat:** session card peeks stay bounded, but the full-screen
  conversation keeps the entire user or assistant message. Long consecutive Claude response rows
  merge without dropping their tails.
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
- **Sticky full-chat work footer** at the bottom of the conversation history while the main session
  or any child subagent is active. Main and child work have separate symbols and counts; tapping the
  footer expands the active names and distinguishes ordinary work from slow-but-still-possible work.
  It disappears only when neither the main session nor a child is active.
- **Model · effort** wherever a model is shown (`opus · high`). Effort lives only in the
  statusline payload, so `statusline-command.sh` side-writes it per session for the daemon; a
  session whose statusline hasn't rendered yet shows the model alone. Subagent effort comes from
  the agent definition's frontmatter pin, or the parent session's effort when it pins none. In an
  existing full-screen chat, the top-right menu can change model and effort while the provider is
  idle. Options come from Fleet's server-owned provider catalogs; changing model immediately repairs
  an incompatible effort. Claude uses its native `/model` and `/effort` commands, while Fleet-owned
  Codex threads use App Server `thread/settings/update`. External, stale, active, and staging-observed
  sessions never claim an unsafe control. A definitive failure before Claude receives a command
  restores the prior selection. Partial acceptance or a lost native result keeps the exact accepted
  values, shows an unconfirmed warning, and disables another control change until newer terminal
  evidence reconciles it. A newer model, effort, or Codex mode chosen directly in the provider
  replaces Fleet's settled feedback on the next refresh. Rapid follow-up Claude messages queue while
  its registry catches up with a just-started turn, so they cannot land in the wrong terminal state.
- A Claude or Codex card's whole header opens Fleet chat, so live cards do not duplicate conversation
  navigation with **Open**, **Continue**, or **View** buttons. Explicit **Respond** and **Review**
  controls remain when the label carries action meaning beyond navigation. **Open in Terminal**
  (desktop only, behind the workspace ⋮ menu) brings that Claude iTerm tab to the front; for a
  Claude background job it starts the
  official `claude attach` client. Codex shows **Open** only when Fleet proves that the exact thread
  already has one live terminal on the managed socket. Fleet never creates a Codex terminal from the
  dashboard, and it renders no disabled terminal placeholder for active, starting, or view-only Codex
  sessions. When present, **Open** sits immediately left of the ⋮ menu in full-screen chat.
- **Pin sessions to a watchlist at the top:** pinning lifts the full card into a
  **📌 pinned sessions** block at the top of Now. Pinned cards keep the order in which
  they were pinned; a new pin appends at the bottom, and urgency/activity changes do not move it.
  Cards are relocated rather than duplicated. The visible 📌 button appears in session headers and
  Needs-you Action Inbox rows on desktop and mobile. Mobile also supports **long-pressing** a session
  header as a shortcut (it highlights immediately; a short tap still opens its chat). Pins persist
  in server settings across reloads, daemon restarts, and devices. A failed
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
  autocomplete), the amber question block when it's blocked on you, and a compact upper strip with
  session status plus the most recently received file.
  Its top-right **⋮ menu** contains Codex Plan/Default or Claude permission mode (when applicable),
  light/dark mode, Stop turn, and Close session. Stop and close both confirm first. Closing an active session stops its current
  turn and subagents, then archives a Codex thread or terminates only the registered Claude process;
  Claude's iTerm tab remains open. A secondary Git worktree can be preserved or removed after close;
  the branch and primary worktree are never removed. Dirty removal is a separate red confirmation
  based on `git status` (changed and untracked files); ignored build output does not make a worktree
  dirty. Cleanup is blocked while another live Fleet
  session uses that worktree. A lock created by the Claude session itself is released only after that
  session closes; unrelated Git worktree locks remain blocked. The conversation moves to **History**.
  Closing a full-chat workspace always returns to the dashboard, never to an earlier closed chat.
  The card's bounded Markdown
  peek remains the scanning surface; full view is for actually reading and working a session.
  The chat view and the file viewer are **mutually exclusive** and swap in one tap: tapping the
  latest-file button or an inline file chip in chat opens that file, while the viewer's **Chat**
  button beside its file browser opens the canonical full
  conversation and preserves the current draft. Both surfaces call the same composer renderer and
  therefore use the exact `＋ | message | Send` row, image drafts, Send behavior, and delivery
  feedback. Its resting controls are one 44px row; newline input grows upward to four lines.
  Session chat, Markdown, and subagent chat also share the persisted reading-width setting.
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
  - In full chat, a long question has its own scrollable drawer. Its top grip drags upward to
    nearly fill the area below the title bar or downward into a compact waiting bar. The drawer's
    size, collapsed state, and reading position survive Fleet's two-second refreshes, so polling
    cannot snap a question back to the top.
  - AskUserQuestion → full question + option buttons (multi-select = toggles + submit), plus an
    **"Other" free-text input** (types your own answer into the TUI's "Type something" row) and
    a **✕ dismiss button** (= the TUI's "Chat about this": the session hears "user declined"
    and returns to normal chat).
  - Multi-question asks (2+) show **one question at a time** with ‹ › arrows and an
    answered-count, a per-question "selected:" line + Other input, and one "submit all
    answers" button (single-select picks auto-advance, like the terminal).
  - Permission request → the notification text + allow / always allow / deny buttons.
  - A transcript fallback can remain visible for context before Claude's native terminal is ready.
    Its controls stay disabled until the live registry confirms the same prompt is actually waiting.
    If Fleet loses acknowledgement after sending any answer key, that nonce stays blocked across
    reload/restart; check the terminal instead of retrying it blindly.
  - The selector disappears and the selected answer appears in chat immediately on click, before
    Fleet waits for Claude's terminal submission. Its spinner remains through provider acceptance
    until the canonical **You answered…** event replaces it.
  - If a file was delivered shortly before the question (the deliver-then-ask pattern), the box
    leads with a **"read first" chip** — visible even on a collapsed card. Window:
    `question_file_pair_seconds`.
- **Conversation context**: the session's recent turns (your prompts + Claude's replies,
  markdown-rendered, including messages you send mid-turn from the app) in a scrollable box — shown automatically above the amber box when a
  session needs you; for every other state it's in the tap-detail panel ("recent conversation").
  A message appears there immediately with a small sending spinner. If its exact session is busy,
  that spinner becomes the visible **Queued · waiting for session** state and the durable Outbox
  keeps the message editable/cancellable across browser exit or a daemon restart. After Outbox
  delivery it shows **Sent** until the provider transcript replaces the placeholder. An immediate
  request that definitively fails before native launch, or receives no canonical transcript
  confirmation after 15 seconds, gets a red `!`; tapping it restores the text to the composer. If
  native delivery may already have happened but Fleet lost the acknowledgement, the row instead says
  **Delivery unconfirmed**, offers no restore/retry, and requires a terminal check. Failed and
  unconfirmed receipts can be dismissed; restoring a definite failure fills the composer without
  retrying. Either choice is remembered so a durable Outbox row cannot resurrect the receipt or
  force chat back to the bottom. Message and
  subagent-relay composers are multiline: **Return adds a
  newline**, **Command-Return sends on macOS**, and **Control-Return sends elsewhere**; the explicit
  Send/Relay button remains available. The full-chat **＋** menu offers **Send picture** and
  **Schedule message**. The picture action opens the phone camera/photo picker (or desktop file
  picker) through the native file input that receives the original iOS tap. Cancel changes nothing;
  selection closes the menu and persists one shared draft. Selected images are shown beside the composer and delivered to
  either Claude or Codex with the message. Structured-question answers use the selected option labels and the
  same placeholder behavior (secret free text is shown only as “private answer”). A definite
  failure exposes **Restore**, which reopens the original selector with its selection preserved but
  never resubmits; uncertain delivery exposes no retry. The owning card
  on the main fleet page also shows a compact **Submitting / Submitted / Failed** receipt for
  question answers and inline quick responses such as permissions, dismissals, and MCP forms.
  If Fleet itself is online but temporarily loses control of a Fleet-owned active Codex turn, the
  exact text/images enter the server Outbox once and show **Waiting for Codex connection**. A safe
  reconnect steers the active turn or starts the next turn after authoritative completion; ambiguous
  state keeps waiting and never forces a resume or duplicate retry. If Codex definitively rejects a
  steer because that recorded turn no longer exists, Fleet clears the stale **working** state and
  starts the exact payload once as the next turn. A different active-turn id remains queued instead
  of creating concurrent work.
  Every Outbox row has **Delete** once it is not actively sending or spawning. Deleting pending work
  cancels it before it can send; deleting a settled row preserves its original delivery outcome.
  Both move out of the active filters and into the dedicated **Cancelled** category.
  Key tool calls appear inline terminal-style as a single `● Edit(path)` line —
  Edit/Write/Bash/Agent/Skill/SendUserFile only; read-only chatter (Read/Grep/Glob) is hidden.
  The buffer keeps the last ~120 entries per session.
- **Delivered files**: anything the session sent you via SendUserFile appears as a tappable chip
  (📄 documents, 🖼 images) **inline in the conversation at the point it was delivered**, with its
  caption — so the message explaining the file sits right with it. The detail panel also has a
  "delivered files" dropdown (caption + delivered-ago). Chips open a full-screen viewer with
  Markdown, sandboxed HTML, PDF, formatted JSON, raw text, and images; viewing contents requires the act token (same `?token=`
  opt-in). If Claude removes an original scratch file, Fleet uses Claude's confined per-session
  file-history backup when one exists; a file with neither source nor backup shows "(gone)".
  Re-delivering or updating a previously listed path
  moves it back to newest without duplicating it, so chat's latest-file button is accurate.
  The file viewer keeps its selector at a fixed height; selector buttons show up to 40
  filename characters, then an ellipsis, while the full filename remains available to assistive
  technology and in the button tooltip.
  HTML previews can run inline JavaScript for generated charts and other self-contained artifacts.
  They run in an opaque sandbox with no Fleet-origin access; external scripts, embedded frames,
  links, remote assets, network requests, and form destinations are removed or blocked.
- **File viewer** extras: its slim toolbar shows only close, filename, and file/session actions—no
  duplicate session title/project/model header or separator. The same **⋮ menu** exposes light/dark mode, Codex mode, stop, and close
  actions that apply to the owning session. Light mode gives the document a paper theme; the choice
  is persisted per device and shared with the full chat and subagent views. A **📄 files strip**
  (header button) expands a one-line horizontally
  scrolling selector of everything the session delivered, for switching files without leaving
  the viewer. Its docked action bar has no embedded conversation disclosure: the upper strip is the
  flexible file browser plus an equal-height **Chat** button, followed by the same compact
  `＋ | message | Send` composer as full chat. The ＋ menu owns **Send picture** and
  **Schedule message** on both reading surfaces. On phones the dock sits directly against the iOS
  keyboard edge. Opening or closing the keyboard preserves the visible message/paragraph; chat that
  is already near its bottom follows canonical replies and late image/font growth, while an actual
  upward reader gesture disengages follow-tail.
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
session (wraps `python3 -m fleetdash.engine spend --cwd "$PWD"`; needs sandbox-off because the
engine probes PIDs).

## How it works (one paragraph)

A launchd daemon (`server.py` + the `fleetdash/` package) polls `~/.claude/sessions/*.json` (the CLI's live
registry — pid, status busy/shell/idle/waiting, claude.ai bridge id) and incrementally tails each
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
Fleet is also an installable PWA. Its root-scoped service worker refreshes the app shell from the
network first and keeps the last successful `/api/fleet` snapshot on that device. After temporary
connection loss or a reload, Fleet opens the cached dashboard immediately as explicitly offline and
read-only except for its device-local ordinary-message queue. Queued messages send in order after a
live fleet poll confirms reconnection; a connection drop during an attempted send requires manual
restore so Fleet cannot duplicate a message with an unknown outcome. Conversation-detail endpoints
stay network-only, but the bounded last successful conversation is retained on that device and shown
as stale until a live refresh succeeds. Notification, settings, action, search, and token-bearing
responses stay network-only. Web Push delivery runs
in a supervised Node helper outside provider scans and HTTP request locks. Fleet creates its VAPID
and action keys once in ignored `push-secrets.json` with mode 0600; an invalid or loosened secret
file disables delivery instead of silently replacing keys and breaking registered devices. Its
pinned DNS lookup supports Node 18–24 and prefers a validated IPv4 address on dual-stack hosts when
the Mac has no IPv6 route, while retaining IPv6-only support.
Only current Needs-you questions/approvals/forms/reply requests, confirmed provider or delivery
failures, and prolonged stalls enter the external policy. A registered device must first pass its
explicit test push. Each event gets one initial delivery per eligible device and at most one
15-minute reminder wave; Snooze replaces that reminder with one wake, while session Mute suppresses
every device until manual unmute. Lock-screen payloads contain generic state and an opaque exact-event
link only. Snooze/Mute shortcuts use ten-minute, single-use signed capabilities; they never carry the
reusable dashboard token. Clients without system action buttons open the exact event with the same
controls at the top of Fleet.

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

## Production and staging

Fleet runs two deliberately separate app instances:

| Instance | Code | Local URL | Runtime state | Purpose |
|---|---|---|---|---|
| Production | `~/.claude/fleet-dash-prod` | `http://127.0.0.1:8377` | `~/.claude/fleet-dash-prod-state` | Stable app tracking `main` |
| Staging | `~/.claude/fleet-dash` | `http://127.0.0.1:8378` | `~/.claude/fleet-dash-staging-state` | Development branches and live verification |

Shared hook/statusline captures (`pending/`, `effort/`, `usage.json`) live in
`~/.claude/fleet-dash-capture`, selected by `FLEET_DASH_CAPTURE_DIR` in both launchd plists. The
repo checkouts hold source only — no runtime state.

Staging has its own config/token, ledger, search index, uploads, logs, Codex App Server socket,
injector applet, browser origin, service worker, drafts, offline queue, and push subscriptions. It
reads the shared Claude/Codex registries and transcripts so real production sessions are visible,
but the server removes their mutation capabilities and rejects forged action requests. Only exact
session IDs created by staging are controllable. Every staging-created session is forced into a new
`fleet-staging/*` branch and managed worktree under `~/.claude/fleet-dash-staging-state/workspaces`.
Production and staging store browser credentials in separate `act_token_production` and
`act_token_staging` cookies. This matters on localhost and the shared tailnet hostname because
browser cookies do not distinguish ports.

The staging launch agent is `com.benjaminfeder.fleet-dash.staging`; its mobile-installable HTTPS
origin is `https://macbook-pro.tail24da27.ts.net:8443`. The purple **STAGING** banner must always be
visible there. Production remains at the default Tailscale HTTPS origin.

Restart one instance without touching the other:

```
launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash
launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash.staging
```

Promote and relaunch production with `scripts/deploy-production.sh`. It requires a clean production
checkout, fetches `origin/main`, refuses non-descendant history, pins the checkout to that exact
release, installs production dependencies, restarts only production, and verifies both the API and
Web Push. Never point production at the development checkout or share its `node_modules` directory
with staging.

## Enabling phone use

1. Install **Tailscale** on the Mac and phone, sign both into your tailnet.
2. On the Mac: `tailscale serve --bg 8377` → gives an HTTPS URL like
   `https://<mac-name>.<tailnet>.ts.net`.
3. On the phone, open that URL once with `?token=<act_token>` appended (get it via:
   `python3 -c "import json;print(json.load(open('$HOME/.claude/fleet-dash-prod-state/config.json'))['act_token'])"`).
4. In Safari, Share → **Add to Home Screen**. Open the installed Fleet app, then open Settings →
   **Devices & delivery**. Notification permission is requested only from the explicit Enable
   button. Desktop browsers can use **Install Fleet** when they expose the install prompt.
5. Click **Send test**. Fleet queues the encrypted minimal test immediately; Settings then reports
   device delivery health and qualifies that exact subscription for production delivery. Replacing
   a browser subscription requires a fresh successful test. Web Push is the only automatic external
   path. The separate legacy ntfy switch permits only a generic manual test; it is never automatic,
   a fallback, or a duplicate destination.

**Devices & delivery** lists every connected browser. The current browser keeps its setup controls;
other devices can be renamed, tested, paused/resumed, or removed remotely. Removing a device revokes
its subscription and suppresses queued delivery while retaining redacted delivery history.

Settings → **Notifications** controls in-app visibility plus one global push policy shared by every
enabled device. Each event kind has a separate **Show in Fleet Notification Center** switch. Its
push rule may be Off, Once,
Once + reminder, or Repeat until resolved, with bounded delay, interval, maximum count, minimum
severity, and optional quiet-hours bypass. Changing an Off rule does not backfill existing active
events unless you explicitly select **Apply this change**. Session mute and event snooze always
override the global rule; a muted session stays muted until manually unmuted in Settings → Sessions.
The section also shows the exact next due push and the most recent delivery outcome. Expand **What
these settings mean** for the delivery precedence and severity guide; every expanded event rule
defines the event and explains each visible control. **All events** admits Info, Warning, and
Critical; **Warning or Critical** excludes Info; **Critical only** admits only Critical. Routine
successes normalize to Info, while severe budget, repository, and scheduled-delivery failures
normalize to Critical.

## Config (`config.json`)

| key | default | meaning |
|---|---|---|
| `poll_seconds` | 2 | scan cadence |
| `codex_enabled` | true | start the Codex App Server adapter |
| `codex_command` | "" | optional absolute Codex executable path; auto-detected from PATH or `~/.nvm` |
| `codex_remote_control` | true | enable Remote Control on the managed production daemon; staging remains private |
| `search_enabled` | true | start the isolated local transcript indexer and authenticated Search APIs |
| `search_discover_seconds` | 2 | filesystem discovery cadence for new/changed transcript sources |
| `search_batch_rows` | 250 | bounded JSONL rows committed per worker batch |
| `stall_seconds` | 600 | frozen-mid-turn threshold (long Bash gates freeze transcripts!) |
| `dormant_seconds` | 7200 | quiet sessions demote to dormant |
| `turn_done_window_seconds` | 900 | how long "done ✓" persists before fading to idle |
| `question_file_pair_seconds` | 300 | max age of a delivered file to pair as "read first" on a question |
| `legacy_ntfy_enabled` | false | allow one generic manual ntfy test; never automatic/fallback/duplicate |
| `muted_sessions` | {} | session_id → mute-ts map behind the 🔔 card toggle; persists until manual unmute |
| `pinned_sessions` | [] | persisted session ids relocated into the Pinned section in stable pin order; new pins append at the bottom |
| `reply_available` | {} | session id → conversation revision explicitly marked available |
| `read_sessions` | {} | session id → opened conversation revision for the New response badge |
| `rates` | — | $/1M by family. **`fable` is a PLACEHOLDER (opus rates) — fix when published** |
| `permission_keys` | 1/2/Esc | keystrokes for allow/always/deny (empty value = Esc) |
| `ntfy_server`/`ntfy_topic` | ntfy.sh / "" | manual legacy test destination; empty topic = unavailable |
| `web_push_allowed_origins` | [] | optional exact HTTPS push-service origins added by the local operator; never client-supplied |
| `web_push_node_command` | "" | optional absolute Node 18+ executable; otherwise resolved without shell startup files |
| `web_push_subject` | "" | optional credential-free VAPID HTTPS or `mailto:` contact |
| `act_token` | generated | device token for the act endpoint |

Retired automatic-ntfy keys may remain in an older `config.json` for migration compatibility, but
Fleet no longer reads them for dispatch and Settings rejects attempts to change them.

Apply config/engine changes with: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`
(dashboard/static asset changes need no restart — open tabs self-reload). Log: `fleet-dash.log`.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
scripts/coverage.sh --show-missing   # same suite under coverage.py (pip install coverage)
python3 tests/search_benchmark.py
npm install
npm run test:push
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
scripts/build-injector.sh production ~/.claude/fleet-dash-prod-state/FleetDashInjector.app
```
(compiles `injector.applescript`, sets `OSAAppletStayOpen` + the bundle ID, and ad-hoc signs).
A rebuild MAY re-trigger the automation prompt once (ad-hoc signature changes).

`scripts/build-injector.sh staging ~/.claude/fleet-dash-staging-state/FleetDashInjector.app` compiles the
same source with staging's private request directory and bundle ID
`com.benjaminfeder.fleet-dash.staging.injector`. The two resident applets therefore cannot race the
same request/result files. The staging applet receives its own one-time iTerm automation approval.

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
- Claude background jobs do not have an iTerm route. Fleet controls a validated eight-character
  background job through Claude Code's official `claude attach <job>` client in a private PTY,
  detaches with Ctrl-Z after each action, and uses `claude stop <job>` for close. A true Claude VS
  Code extension session has neither that background-job identity nor a terminal route and remains
  view-only.
- ChatGPT Desktop and Codex VS Code do not expose their private App Server endpoint to Fleet Dash, so
  those transcripts are view-only. Fleet can send through and focus a Codex TUI only when its
  process command names the exact Fleet socket and canonical thread UUID on one unambiguous TTY.
- Codex App Server has no direct client-to-subagent input/stop RPC or per-thread currency-cost field.
  Fleet Dash exposes the corresponding parent-mediated/unavailable states and never substitutes zero
  as a measurement.
- Codex Plan/Default mutation currently uses an experimental App Server method. It is verified
  against the installed CLI and isolated in the adapter, but may require an adapter update if Codex
  changes that experimental protocol.
- The generic file viewer supports images, browser-native PDF, formatted JSON, sandboxed interactive
  HTML, and a minimal built-in Markdown renderer (headings, lists, tables, code, quotes, links).
  Exotic Markdown falls back to plain paragraphs; other text files show raw.
- A closed conversation is read-only until it is reopened. Every surviving Claude main transcript
  can be viewed; Reopen is offered only when the exact UUID transcript and original working
  directory pass Fleet's local safety checks. Deleted transcripts and missing working directories
  remain View-only or unavailable. Subagent chats are reachable only while their parent session is live.
- Messaging a subagent is a **relay through the parent**, never a direct channel — there is no
  such thing as typing into a subagent (no tty; `SendMessage` from the parent is the only path).
- Screen-peek ("show me what this stalled session's terminal displays") is proven as a technique
  (`contents of session` osascript) but not built into the UI yet.
- Fable pricing placeholder; ledger has no backfill from pre-daemon transcripts.
