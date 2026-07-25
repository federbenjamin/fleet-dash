# fleet-dash — agent development guide

The canonical dev guide for every coding agent (`CLAUDE.md` is just `@AGENTS.md`). Companion to
`README.md` (human setup/usage). Update BOTH in the same commit as any feature: README for
what/how-to-use, this file for invariants + dev workflow.

## Code structure (start here)

```
server.py                 entrypoint + HTTP routes; APP_ROOT anchors dashboard.html + static/
fleetdash/                everything server.py imports
  paths.py                  instance dirs (BASE/CAPTURE/…) — the single test patch point
  config.py                 DEFAULT_CONFIG, load_config, pricing, shared constants
  placement.py              Now-queue classification + reply-request detection
  screen.py                 pure TUI screen classifier (question/permission/trust/input)
  tail.py                   Tail: incremental transcript fold (offsets, convo/files, usage)
  engine.py                 Engine = __init__ + constants + spend CLI
  engine_scan.py            registry scan, session organization, status, control state
  engine_act.py             act(): the injection dispatcher
  engine_context.py         conversation/file projections, hook pending, prompt identity, effort
  engine_receipts.py        durable act receipts + idempotent replay
  engine_ledger.py          spend ledger, closed sessions, history, handoffs
  engine_notify.py          notifications, Web Push, Outbox actions
  engine_spawn.py           spawn/handoff, iTerm applet exchange, settings
  engine_tmux.py            tmux transport: pane discovery, key delivery, spawn, focus
  engine_transport.py       tty/process resolution, Codex terminal routes, transport
                            dispatcher (_terminal_write/_terminal_spawn), bg attach
  engine_worktree.py        worktree preview/cleanup tickets
  engine_uploads.py         phone image uploads
  engine_staging.py         staging isolation
  codex_protocol.py         App Server WebSocket/JSON-RPC client (UnixWebSocketProcess, CodexAppServer)
  codex_runtime.py          Codex runtime lifecycle/migration + CodexError base exception
  codex_adapter.py          CodexAdapter: thread/state normalization + capability mapping
  codex_observer.py         read-only external-thread rollout observer
  codex_launcher.py         Codex TUI router (stdlib-only, spawned as a script)
  claude_background.py      official `claude attach` background transport
  briefing.py               FleetOperations facade: projections/observe/budgets/devices
  briefing_store.py           sqlite store: schema, migrations, row/serialization helpers
  briefing_scheduler.py       cadence/quiet-hours/delivery-lease scheduling
  outbox.py                 durable send/schedule queues
  repo_center.py            repository/git observation
  search_index.py           search worker (stdlib-only, spawned as a script)
  web_push.py + web_push_worker.js   Web Push supervisor + Node helper
static/                   the whole browser app (js/*.js ES modules, fleet.css, sw.js — no build step)
  fonts/                    self-hosted Console webfonts (Space Grotesk var + IBM Plex Mono ×3)
design-system/            the "Console" design system (tokens, component specs, guidelines) —
                          the visual contract every new surface matches; see its HANDOFF.md
dashboard.html            application shell
hooks/pending-capture.py  Claude hook: pending-question capture → fleet-dash-capture
scripts/                  build-injector.sh, deploy-production.sh, coverage.sh
tests/                    unittest suite (test_*.py) + live smokes + browser specs
```

Instances & dirs under `~/.claude/`: `fleet-dash` = dev/staging code (this repo) ·
`fleet-dash-prod` = production code · `fleet-dash-prod-state` / `fleet-dash-staging-state` =
per-instance runtime state (DBs, config, uploads, injector mailbox + applet, log) ·
`fleet-dash-capture` = shared hook/statusline captures (pending/, effort/, usage.json).

## Architecture (data flow)

```
~/.claude/sessions/<pid>.json      CLI live registry: sessionId, cwd, status busy/shell/idle/waiting,
                                   name, bridgeSessionId (claude.ai deep link). PID-liveness
                                   filters stale files (they're deleted on clean exit only).
~/.claude/projects/<proj>/<sid>.jsonl            main transcript  ─┐ incremental byte-offset
~/.claude/projects/<proj>/<sid>/subagents/*.jsonl agent transcripts┘ tails (engine.Tail)
~/.claude/fleet-dash-capture/pending/<sid>.json  hook-captured pending prompt (the ONLY source)
        ↓ fleetdash/ engine (Engine.scan, poll thread, 2s)
snapshot_cache ─ server.py ─ GET /api/fleet ─ dashboard.html + static/js/*.js (fetch poll 2s,
                                               self-reloads via page_v)
                          ├ GET /api/context?sid= ─ Tail.convo ring (recent turns) + Tail.files
                          │   (SendUserFile deliveries); page refetches only when the session's
                          │   convo_v/files_n fields in /api/fleet move
                          ├ GET /api/file?sid=&fid= (token) ─ Engine.file_content (whitelist)
                          └ POST /api/act (token) ─ Engine.act ─ inject-request.txt ─
                            open -g FleetDashInjector.app ─ iTerm write by tty ─ inject-result.txt
Claude/Codex JSONL ─ fleetdash/search_index.py --worker (nice 10) ─ search.db WAL/FTS5
                                      └ server.py separate reader ─ authenticated
                                        /api/search, /api/search/status, /api/search/context
ledger.db: agent_runs (finalized agent spend), session_runs (live + closed sessions)
           notification_events/devices/deliveries ─ web_push.py background lease worker
                                                    └ fixed web_push_worker.js Node protocol
```

The launchd parent has HTTP threads + one provider poll thread. It owns one low-priority child
indexer process; a file lock prevents overlapping writers during launch-agent restarts, and the
child exits when its parent disappears. Keep search parsing out of the parent interpreter: a Python
thread regressed live `/api/fleet` p95 by contending for the GIL on the 2.7 GB local corpus.
`Engine.scan_lock` serializes ALL Tail folding (poll loop and act's freshness re-poll) — Tails
are stateful offsets; concurrent folds double-count. It is held ONLY around that folding, not
around the whole scan: `_scan` splits into `_scan_claude_sessions` (locked) and
`_scan_after_fold` (unlocked), with one short re-acquire for the status-strip Tail read.
`Engine.scan_serialize` keeps one `_scan` at a time — the guarantee the wide lock used to give
for free. `Engine.lock` guards snapshot_cache only.
Measured live 2026-07-24 (49 sessions): the fold is 32.3 ms of a 733.9 ms scan — 4.4% — so an
act freshness re-poll waits ~32 ms instead of up to the p95 1141 ms it used to. Anything added to
`_scan_after_fold` that touches a Tail must take `scan_lock` around exactly that read.

## Invariants — violating these re-breaks debugged behavior

Numbers are stable identifiers (code comments cite "invariant N") — never renumber;
new invariants append. Quick map by theme (an invariant may appear in two groups):

- Native prompt capture & injection (Claude TUI): 1–5, 9, 14, 18, 40, 65, 66, 68, 75, 77
- Applet, transports & click latency: 3, 24, 25, 73, 74
- Session/agent state & Now placement: 7, 31–33, 49, 61, 69
- Transcript folding, effort & usage accounting: 11, 12, 15–17, 22, 42, 43
- Codex runtime & ownership: 30, 38, 49, 65, 69
- Security boundaries (files, spawn, trust, uploads, staging, closed sessions):
  10, 19–21, 23, 41, 54, 56, 74
- Notifications & Web Push: 2, 8, 44, 46–48
- Sends, Outbox & delivery certainty: 34, 38, 39, 50, 52, 61, 64, 66, 68, 76
- Browser UI (cards, overlays, composer, workspace): 13, 26–29, 35–37, 39, 45, 71,
  51, 53, 55, 57–60, 62, 63, 67, 70, 72
- HTTP routing gotcha: 6

1. **Pending questions NEVER come from the transcript.** The CLI flushes AskUserQuestion
   tool_use rows only when answered (row timestamps are creation-time and lie). Hook capture
   (`hooks/pending-capture.py`, registered in `~/.claude/settings.json`) is the only source.
   The transcript-derived `mt.pending` path survives only as a permission-prompt fallback: it
   explicitly skips AskUserQuestion rows, because a question falling through to the permission
   branch would render with a completely different key recipe (invariants 4, 5). One prompt with
   two possible nonces is why identity is server-owned (invariant 75).
2. **The Notification event must never clobber a question capture.** ~6s after every question
   opens, an input-needed Notification containing "permission" fires for the SAME event.
3. **All Apple Events go through the applet.** launchd-context osascript hangs FOREVER on the
   TCC check (cannot present the dialog) — never call `osascript -e 'tell app "iTerm2" …'`
   from engine/server. The applet must keep `CFBundleIdentifier`
   (com.benjaminfeder.fleet-dash.injector) or TCC grants can't persist. This scopes to the
   **applet transport only**: the tmux transport sends no Apple Events at all, and the target
   state has none (the operator may switch terminal emulators — see invariant 73).
4. **TUI key map (verified live, don't re-derive):** single-select = digit + CR. Multi-select:
   digits toggle (focus stays), Enter toggles the FOCUSED row (not submit!); submit =
   `\x1b[C` (right-arrow → "✔ Submit" tab) + CR. Enter must be raw CR — request-file flag `2`
   → applet sends `character id 13`; iTerm's `newline YES` sends LF (toggles, doesn't submit).
   **Multi-question asks** (sandbox-proven 2026-07-14 after 6 corrupted live rounds): tab bar
   `← ☐ Q1 ☐ Q2 ✔ Submit →`. Single-select: BARE DIGIT instant-selects and advances — never
   follow it with a separate CR write: that CR re-fires on the next view as it mounts (the
   "phantom Enter": toggles row 1 of a multi, auto-answers option 1 of a single, cascades).
   Multi-select: digit writes toggle (focus stays row 1); `\x1b[B` × (n_options+1) walks to
   the Next/Submit row; one bare CR advances CLEANLY (no phantom from that row). Review pane:
   bare digit "1" submits. Escape sequences and CRs are dropped when chunked into one write
   with other bytes — send each key as its own write. Engine `multiq` builds this; the client
   sends `n_options` per answer for the walk. **Other + dismiss** (sandbox-proven 2026-07-14):
   the TUI numbers a "Type something" row at n+1 and "Chat about this" at n+2. Single-select
   Other = digit n+1 (focuses the row, does NOT select) → raw text → CR (submits/advances,
   clean). Multi-select Other = digit n+1 (toggles its checkbox) → DOWN×n (focus its input) →
   text → DOWN (Next/Submit row) → CR; that ROW path opens the Review pane even on a
   single-question multi (append the "1") whereas the right-arrow ✔ Submit TAB skips Review —
   both verified same-day, keep the no-Other multi path on the TAB. Esc anywhere = "User
   declined to answer questions" (same outcome as Chat about this) — the dashboard's ✕.
   Other text is control-char-stripped server-side (a smuggled \r would fire as Enter). The
   browser's `n_options`, `multi`, and answer counts are display hints only: `act()` rebuilds the
   nonce-matched prompt kind, option count, multi flag, and Other availability from authoritative
   hook/transcript data, caps the shape, and rejects a mismatched action type before building keys. Debug
   rig: spawn a sandbox `claude --model haiku` in a new iTerm tab, make it ask, drive it via
   scratchpad sbx2.py — never experiment on real sessions. The recipes are transport-independent
   and were **re-confirmed on v2.1.219, 2026-07-24** — do not re-derive them. tmux delivers the
   same per-key steps through `send-keys -l -- <key>` (verified byte-exact on tmux 3.7b: no
   key-name lookup, no C-escape processing, UTF-8 and embedded LF preserved) and keeps the same
   fixed inter-key delay as the applet. Replacing that delay with settle detection is W5-T2b of
   [`design-responsiveness-and-transport.md`](design-responsiveness-and-transport.md), not shipped.
5. **Injection freshness:** act() re-polls the tail under scan_lock and validates the nonce
   (hook-file nonce or transcript tool_use_id) before writing keys, AND refuses prompt answers
   (option/multiq/permission/dismiss) when the registry status isn't `waiting`. The second
   check is load-bearing: a PreToolUse capture can outlive an ask that another hook BLOCKED —
   the ghost question renders, but the session sits at its main input and injected digits
   would type (and send) as a message. hook_pending also HIDES (never deletes — PostToolUse owns
   the clear) a question capture older than `GHOST_QUESTION_GRACE` on a non-waiting session for the
   same reason; the grace is wide because nothing re-surfaces a dropped capture any more. `interrupt` (Esc mid-turn) has the mirror gate: it
   requires status `busy`, or `shell` plus a freshly re-polled mid-tool transcript, so an Esc can
   never land in an idle session's input box. Close also interrupts an active shell before SIGTERM.
   **Where a screen is readable, the guess is replaced by evidence** — invariant 77's classifier
   refuses any prompt answer whose widget is not the one the pane is rendering. That is an extra
   refusal, never an extra permission: the registry gates above all still apply, and a session
   outside tmux behaves exactly as before.
6. **`http.server` self.path includes the query string.** Route on `path.split("?",1)[0]`.
7. **Agent state semantics: "stalled" means frozen mid-TOOL, nothing else.** An agent is
   working only while something is in flight — a `tool_use` awaiting its result, or a
   `tool_result` it hasn't answered. If its last row is assistant prose with no tool call it
   has SETTLED: mark it done after `agent_idle_done_seconds` (30) even with no end_turn.
   **Never gate done on `stop_reason` alone** — a long final report routinely ends on a
   stop_reason-less text row (`('assistant', None, ['text'])`), and the old end_turn-only rule
   left those agents decaying into red "stalled" forever, out of "completed agents" and never
   finalized to the ledger (383 agents across the history, 0 of them actually mid-tool).
   `stop_reason: end_turn` still gets the fast 5s grace. Agents whose PARENT went idle/waiting
   with no end_turn are `ended` (canceled) and finalize to the ledger. Sessions: registry
   `status` is authoritative after corroboration (hook-captured waiting → needs_you
   immediately; a bare waiting flag must persist for `WAITING_CONFIRM_SECONDS` because Claude
   can flash it between progress prose and the next tool; idle → turn_done if fresh end_turn
   else idle; busy → running/stalled). Claude's `shell` registry state is active while the
   transcript remains mid-tool, but a completed `end_turn` wins over a stale detached shell child
   and returns the session to turn_done/idle.
   **CANCELLED is separate, authoritative and immediate.** Older Claude builds mark it when the
   parent's Agent `tool_result` comes back `is_error: true`; `Tail.errored_tools` collects those
   ids. Newer builds can instead leave the Agent spawn result successful and emit a queued/
   attached `<task-notification>` with `<status>killed</status>` after `TaskStop` (verified
   2026-07-16 on session `9e8b990e`, agent `agent-a9f377…`). `Tail.agent_terminals` retains bounded
   `completed`/`killed`/`failed` notices and `scan_agents` maps them to `done`/`ended` immediately.
   A task id can resume, so a terminal notice applies only when its timestamp is at or after the
   child transcript's newest row; later child output supersedes it. The *presence* of the parent's
   ordinary Agent `tool_result` still proves nothing because a background agent gets one at spawn.
8. **Legacy ntfy is manual-test-only.** Provider scans and daemon startup never dispatch ntfy.
   The single token-gated test route emits fixed generic copy only when the explicit legacy switch
   and topic are configured; it has no click URL, automatic category, fallback, or duplicate path.
   The M12 migration keeps historical ntfy claims in `notification_deliveries_legacy` and writes
   canonical lifecycle rows to `notification_events`; the two identities must not be conflated.
   Canonical identities use the full provider/session/native revision, never truncated session IDs,
   transcript mtimes, or repeat buckets. Informational events are resolved observations; per-device
   cursors, not lifecycle state, decide whether they are unread.
9. **Never inject into real sessions during dev-testing** except via the user-driven live-test
   protocol below. The auto-mode classifier blocks self-injection from the building session.
10. **`/api/file` serves ONLY whitelisted paths** — paths recorded from that session's own
    SendUserFile tool_use rows, and it's token-gated. Never accept a free-form client path:
    that would turn the act token into an arbitrary-disk-read credential over the tailnet.
    The whitelist is the files deque + convo chips + `Tail.delivered_paths` (a bounded durable
    map fed only by `_file_add`, because the two ring buffers can age a delivery out); the
    `file_backups` mapping tracks EVERY checkpointed file and must NEVER widen the whitelist.
    A missing Claude delivery may resolve through the transcript's `file-history-snapshot` mapping,
    but only beneath `~/.claude/file-history/<exact UUID>/` with a regex-bounded opaque backup
    basename. The mapping is server-observed; the client still supplies only the delivered path.
    Delivered HTML stays `text/plain` at this authenticated endpoint so direct navigation cannot
    execute it in Fleet's origin. `sandboxedHtmlDocument` keeps inline scripts for self-contained
    generated artifacts, removes external scripts/frames/navigation and remote-asset URL attributes,
    then renders with `sandbox="allow-scripts"` (never `allow-same-origin`) and a deny-by-default CSP.
    Every successful file response carries `X-Content-Type-Options: nosniff`; PDF uses the browser's
    native unsandboxed document frame because Chromium's isolated PDF viewer requires its own scripts.
11. **Convo capture filters user-row noise in `Tail._fold`** — isMeta rows, `<command-`/
    `<local-command`/`Caveat:` prefixes, `<system-reminder>` blocks, and the post-compaction
    "This session is being continued from" blob. Consecutive assistant text rows merge into one
    logical reply unless a KEY_TOOLS entry lands between them. Extend the filter list there,
    not in the client. **Mid-turn user messages never become user rows** — they arrive as
    `type:"attachment"` rows (`attachment.type:"queued_command"`, `origin.kind:"human"`,
    prompt is a string OR content-block list) plus transient `queue-operation` rows; fold the
    attachment only, or the message is invisible in the convo. **`type:"system"` rows become
    event rows** (`role:"event"`): `compact_boundary`, `model_refusal_fallback` (the "Fable 5's
    safeguards flagged this … switched to Opus 4.8" notice — a refusal fallback, NOT a usage
    limit), `api_error` (consecutive retries collapse into one row with a count), and
    `local_command` (its stdout attaches to the preceding `/command` event). `turn_duration` /
    `stop_hook_summary` are noise — never surface them. `AskUserQuestion` gets a `qa` event
    carrying every question and the chosen answer, parsed from the tool_result's
    `"<question>"="<answer>"` pairs; it renders in FULL (never truncated) — the user reads it
    to confirm the right answers landed.
12. **Convo tool lines show `KEY_TOOLS` only** (engine.py constant — user decision: hide
    Read/Grep/Glob/task bookkeeping), rendered as ONE line, no result line (user decision
    2026-07-14; results are still captured engine-side via `_tool_refs`). Context freshness
    rides `Tail.convo_rev` (a counter), NOT the last entry's timestamp — an in-place result
    mutation must still bump `convo_v` or clients never refetch.
13. **`.card` must stay `overflow:clip`, never `hidden`.** `clip` keeps the border-radius
    clipping without turning the card into a scroll container — `hidden` would create one,
    which breaks any viewport-sticky descendant and silently changes hit-testing/scroll
    behavior inside the card. (The Console redesign dropped the sticky `.shead`, but the
    clip-not-hidden rule still stands for the radius clipping.)
14. **Answer suppression is client-side and IDENTITY-keyed:** a sent answer records
    `answered[sid]=requestKey(p)` and the selector hides immediately (the engine's pending clears a
    poll or two later). Never suppress by sid alone — the next ask must render — and never by the
    raw nonce, which flips when the prompt's evidence source changes (invariant 75). Suppression
    happens BEFORE the await, not after the provider replies: `sendPerm`/`sendDismiss` go through
    `suppressWhileAnswering`, options through `beginOptimisticAnswer`. Only a definite pre-delivery
    failure reopens the selector; `duplicate` and uncertain outcomes leave it hidden.
15. **`usage_stats` rows are CUMULATIVE per transcript path, flushed with INSERT OR
    REPLACE.** Tails re-read whole files at daemon start, so cumulative+replace is the
    idempotency mechanism — switching the drain to additive upserts double-counts every
    restart. Skill $ is turn-cost attribution (Skill tool_use → end_turn, most recent skill
    wins); tool "tokens in" is result chars/4 — both estimates, unlike the ledger's real $.
    Corollary: REPLACE never purges keys the counting logic stopped producing — after any
    change to how a kind is counted, `DELETE FROM usage_stats WHERE kind='<kind>'` once and
    restart, or stale rows keep polluting the aggregates.
16. **Cache-bust detection (`Tail._cache_track`) counts RE-PAID tokens only** —
    `min(prev_read+prev_write − read, write + uncached_input)`, threshold 2048 — a shrunken
    read alone (title-gen side call, context edit) costs nothing and must not register.
    Side calls with a tiny prefix must not become the next call's baseline (the 0.3× guard).
    Cause priority: compaction > model switch > idle/ttl > skill > tail-rewrite (read still
    ≥50% of prev prefix = breakpoint drift) > deep bust.
17. **A compaction writes NOTHING to the transcript while it runs.** The whole block — the
    `/compact` command rows AND the `compact_boundary` — is flushed when it FINISHES, so the
    command rows land *after* the boundary in file order while carrying *earlier* timestamps.
    Two consequences: (a) `_event_add` inserts by timestamp, not append order, or `/compact`
    renders below the compaction it triggered; (b) "issued but no boundary yet" is undetectable
    from the transcript — the live pill reads the **PreCompact hook's checkpoint file mtime**
    (`~/.claude/compaction/<project>/checkpoint-<sid>.md`) = compaction start, suppressed once
    a boundary lands or after 900s. Projects with no PreCompact hook get no pill (the finished
    event row still lands). Don't "fix" the pill by inferring from transcript silence.
18. **A leading `/` opens the TUI's OWN command popup, where Enter fires the HIGHLIGHTED entry
    — not the typed text.** Injecting a bare `/foo` + CR can therefore run a *different*
    command. A trailing space closes the popup, so `act()` appends one to any `/…` text with no
    space (sandbox-proven 2026-07-14: `/status`+CR ran the highlighted match; `/status `+CR
    submitted the literal text, popup gone). The dashboard's `/` menu only ever INSERTS text
    (never sends) — every send goes through `sendText`, which confirms first for
    `DANGER_COMMANDS` (clear/compact/quit/exit/logout/rewind).
19. **A subagent has NO tty — there is nothing to inject into.** It runs inside the parent's
    process; the only channel to it is the parent Claude calling `SendMessage`. So `act`'s
    `relay` type types a tagged line into the **parent's** input box and lets the parent
    forward it — never present this as a direct channel, and never "fix" it by trying to write
    to the agent. The relay is refused when the parent's status is `waiting` (that input box is
    the ask TUI: the relay would answer the question). **Stopping a subagent is Esc into its
    PARENT** — there is no per-agent kill — so its interstitial must say that it ends the
    parent's whole turn and every sibling agent; never word it as a targeted stop.
    `agent_id` is client-supplied → it is
    hard-whitelisted (`agent-[A-Za-z0-9_-]{1,64}`, basename only) before any path is built, or
    `/api/agent_context` becomes an arbitrary-file read.
20. **Spawn composes its command from ALLOWLISTED parts, never client text.** `act` type
    `spawn` → `_terminal_spawn` → a terminal running
    `cd <dir> && claude [--model M] [--effort E] [--worktree [name]]`. Model and effort
    must be members of `Engine.MODELS` / `EFFORTS`, the worktree name is regex-bounded, the dir
    must exist and resolve under `$HOME`, and the path is `shlex.quote`d. Never accept a
    free-form command string — the act token would become a remote shell.
    **Where it runs is the transport's business, what may be composed is not.** tmux wins when
    the operator already has a session named `tmux_session` (default `fleet`) running:
    `new-window -d -t =<session>`, with `; exec ${SHELL:-/bin/sh}` appended so the pane and its
    scrollback outlive the session, the way the applet's terminal tab did. Otherwise this falls
    back to applet verb `SPAWN` (line 1 of the request file instead of a tty) → a new iTerm tab.
    **Fleet must NEVER run `tmux new-session` itself.** A tmux server inherits the environment of
    whoever starts it, so a server started by this launchd daemon hands Claude
    `PATH=/usr/bin:/bin:/usr/sbin:/sbin` — proven live 2026-07-24, where the spawned pane answered
    `zsh:1: command not found: claude`. That environment cannot be honestly reconstructed here:
    this operator's passwd shell is zsh while their sessions run bash, and the directory holding
    `claude` is added by a bash startup file, so neither `$SHELL`, the passwd entry, nor a login
    shell is a safe guess. Joining a server the operator started inherits it exactly.
    All three spawn sites go through the dispatcher — new session, Reopen
    (invariant 23), and background-job attach (invariant 25) — or a terminal switch breaks two
    of them silently.
21. **Claude Code's folder-trust is INHERITED, and fleet-dash must never write it.**
    `~/.claude.json` `projects[dir].hasTrustDialogAccepted` is keyed by dir, but a git worktree
    under a trusted repo has NO entry of its own and still starts clean (verified 2026-07-14),
    while a fresh dir with no trusted ancestor stops at "do you trust the files in this folder?"
    — a prompt only the Mac can answer, and one a spawned session hangs on. So `is_trusted()`
    walks ANCESTORS (an exact-path check falsely flags every worktree as untrusted), the picker
    labels untrusted dirs, and the spawn reply carries `trust_prompt`. Setting that flag
    ourselves would defeat a security gate from a remote device — don't.
22. **Claude's live effort comes from the transcript (≥2.1.217); the statusline side-write is a
    legacy fallback.** Claude Code stamps the current effort level onto every assistant transcript
    row (top-level `effort`, main AND subagent transcripts — verified 2026-07-23). `Tail.effort` +
    `Tail.effort_evidence_offset` fold it with byte-offset evidence order (row timestamps lie under
    compaction), and `Engine.effort_for` prefers it. The old path — the statusline payload's
    `"effort":{"level":…}` side-written to `fleet-dash-capture/effort/<session_id>` by
    `~/.claude/statusline-command.sh` — remains as a wall-clock fallback for older builds, but the
    canonical statusline currently lacks that block, so without transcript rows the UI shows the
    model alone. SUBAGENT effort prefers the child transcript's own effort rows (same
    ≥2.1.217 stamp); older transcripts fall back to the agent DEFINITION's frontmatter pin
    (`.claude/agents/<type>.md` → `effort:`), then to the parent session's effort when
    the agent pins none — that fallback is not a guess, it is what the runtime does. Plugin
    types (`plugin:agent`) have no local file: fall back to the parent. The narrow exception is a
    Fleet-issued, successfully delivered native `/effort` change: persist its accepted value with the
    transcript effort-evidence offset (and acceptance time) so a daemon restart cannot revert the
    UI. Newer native evidence retires the override: a transcript effort row past the recorded
    baseline, or a newer statusline side-write. A zero/absent baseline (pre-transcript-evidence
    record) may only be retired by the wall-clock path — old transcript rows must never revert it.
23. **A CLOSED session has no process:** the registry can't resolve it, so `closed_context`
    reads the ledger's validated `transcript_path` (falling back to the legacy cwd mapping only for
    old rows). Its overlay is read-only — no send box, no stop, no mute. A closed Claude session may
    expose **Reopen**: that opens a new terminal running `claude --resume <exact UUID>` through
    `_terminal_spawn` (invariant 20), never a bare iTerm tab. Never accept
    a client-supplied path or cwd. `_safe_claude_transcript` must continue to require an exact UUID
    filename directly beneath one `~/.claude/projects` directory, and `_safe_reopen_cwd` must keep
    the working directory inside HOME.
24. **Click latency is the injection path — keep these four fixes.** Measured 2026-07-14: a
    one-keystroke `focus` cost 850ms while a ping cost 2ms. Clauses (a), (b) and (d) describe the
    applet mailbox; tmux has no mailbox (each key is its own `send-keys`, p50 7.3 ms) but keeps
    the same between-steps-only delay rule. (c) is transport-independent.
    (a) The applet delays only BETWEEN
    steps, never after the last; (b) the delay is per-request (flag 4) — 0.4s ONLY for ask-TUI
    key sequences where it is load-bearing (invariant 4), 0.05s for text/focus/interrupt/relay;
    (c) `act()` takes `scan_lock` + re-polls the tail only for native-surface mutations that need
    final freshness: prompt answers, controls, direct text/images, handoffs, and relays. Focus and
    interrupt keep the no-tail fast path. The poll thread holds that lock only while FOLDING (the
    architecture note above), so these actions wait ~32 ms rather than a whole scan — but
    `mt.poll()` must stay INSIDE the lock or it races the fold and double-counts; (d) the
    result file is polled every 20ms, not 300ms. The applet owns one fixed request/result mailbox,
    so a dedicated Engine lock serializes the complete atomic request publish, `open`, and matching-
    result wait; concurrent HTTP/Outbox actions must never share that exchange. The applet is stay-open
    (`OSAAppletStayOpen`), so `open -g` reopens the resident process (`on reopen`) instead of
    launching one. Net: 850ms → ~260ms. Any change here is re-verified in the SANDBOX with a
    real multi-question ask before shipping.
25. **Transports and their verbs.** Three Claude transports exist and every Engine caller reaches
    them through the `_terminal_write` / `_terminal_spawn` dispatcher (invariant 73) — never a
    transport method directly, or a terminal switch strands whichever site was missed.
    **tmux** (`engine_tmux.py`) is the emulator-agnostic one: `send-keys -l -- <key>` per step,
    `new-session`/`new-window` for spawn, and focus = `select-window` + `select-pane` plus a
    generic `open -a <terminal_app>` raise rather than an Apple Event. Its failures split the way
    invariant 66 requires: `terminal_not_available` proves no byte was written (no tmux binary,
    unknown pane, a refusal before the first key), while a lost result or a refusal after any key
    is `delivery_uncertain`. **Applet verbs:** flag 0/1/2 = write text / text+LF / raw CR;
    **flag 3 = focus** (select that
    window+tab, activate iTerm — types nothing); line 1 `SPAWN` = new tab running a composed
    command. `act` type `focus` powers the **Open in Terminal** item in the session workspace's ⋮
    overflow menu (Console locked decision: never a card button and never a standalone header
    button — Claude terminal access lives only behind ⋮; only Codex's narrowly proved exact-terminal
    **Open** keeps a `#sctrl` button per invariant 30, and focusing a Mac tab from a phone is
    meaningless). This path is only for a
    foreground Claude process whose exact PID/tty maps to a tmux pane or an iTerm session. A
    `kind:bg` registry row
    has no terminal route: `ClaudeBackgroundTransport` validates its eight-hex job id, starts the
    official fixed-argv `claude attach <job>` client in a private PTY, writes only Engine-composed
    text/keys, then sends Claude's documented Ctrl-Z detach. Close uses fixed-argv
    `claude stop <job>`. Never read the private daemon roster, accept a client socket/tty/job id, or
    speak Claude's private rendezvous protocol. PTY bytes are readiness evidence only and must never
    be returned through an error, API, snapshot, cache, or log. Attachment failures are scoped
    `background_connection_lost`; they must not crash, reload, or hide the session.
26. **Claude plan usage mirrors Claude Usage's selected profiles, without exposing credentials.**
    `Engine.claude_usage_profiles` watches
    `~/Library/Preferences/HamedElfayome.Claude-Usage.plist` by mtime/size and projects ONLY profile
    id/name, account email, selected/active state, refresh interval, display flags, 5-hour/general-
    weekly/Fable-weekly quota percentages and resets, and last-update time. The same profile objects
    also contain session keys and credential
    JSON: never return, log, cache, or snapshot the raw objects. Multi-profile mode renders every
    selected account and its active marker. The usage entry point summarizes the active Claude
    profile's 5-hour/weekly percentages plus the highest active non-Spark Codex bucket: on
    desktop the nav-rail footer renders per-window bars (`#railusage`, green <70 / amber ≥70 /
    red ≥90) that open the accounts flyout pinned beside the rail; on mobile the Now-header
    chip keeps the `Usage · Claude X/Y · Codex Z` summary. Only the active Claude profile (falling back to the first selected
    profile when the active marker is missing) and active Codex windows drive amber at 70% and red at
    90%. Inactive Claude profiles retain their own gauge colors in the full popover/sheet but never
    color the summary button. If the app is absent/unreadable, fall back to the Claude
    Code statusline side-write at `~/.claude/fleet-dash-capture/usage.json` plus the mtime-watched
    `~/.claude.json` login email. The adjacent **local lifetime-token** figure is a different,
    machine-wide scope: `Engine.claude_lifetime_tokens` reads `~/.claude/stats-cache.json`
    `modelUsage` and sums `inputTokens` + `cacheCreationInputTokens` + `cacheReadInputTokens` +
    `outputTokens` across models. Show that aggregate once, not once per profile. It represents
    retained main-session and saved-subagent transcripts on this Mac; it excludes deleted history,
    other computers, and claude.ai. Keep the word `local` in the UI.
27. **One light theme, two surfaces.** `setTheme(light)` toggles `.light` on BOTH `#vbody` (file
    viewer) and `#sbody` (full chat view) and swaps both ☀︎/☾ buttons; `toggleTheme` flips it;
    persisted as `viewer_light`. Light CSS is keyed off a bare `.light` ancestor (not `#vbody.light`)
    so it applies in either container. `#stheme` is fixed 40×32 so the glyph swap can't resize the
    chat header (`#vtheme` stays the big 28px viewer button).
28. **Pinned sessions are server-persisted and live in a GLOBAL block.**
    `pinnedSessions` mirrors `/api/fleet.settings.pinned_sessions`; `toggleSessionPin` writes
    `pin_session` + `pinned` through `/api/settings`. `renderPinned` fills `#pinned` (directly below
    Fleet Briefing) in persisted insertion order. Fleet urgency/activity changes never reorder it; a
    new pin appends at the bottom. Pinned cards are relocated, never duplicated.
    Console redesign (locked decision): pin/unpin is right-click on the session header
    (desktop, via `oncontextmenu`) or long-press (mobile); cards carry no pin button — the
    `⌖ pinned` marker + blue border show pinned state. Action Inbox rows keep their `.spin`
    buttons. The long-press hold paints immediately and `sessionTap`
    swallows the following click so pinning does not also open the chat. `pinActions` suppresses
    duplicate writes; failure restores the exact prior
    order and renders inline retry instead of a blocking alert.
29. **Full chat view lands at the bottom on open.** `sessionOpened` (set in `openSession`/
    `openClosed`) forces `#sbody` to `scrollHeight` on the first render regardless of prior
    scrollTop (a tall cached convo starts at 0 → the sticky-bottom test would otherwise keep the
    top). `#sact` renders AFTER and shrinks `#sbody`, so re-pin in a `requestAnimationFrame` once
    layout settles. `wantBottom` also keeps the poll re-render stuck to the bottom when already there.
30. **One canonical Codex runtime; ownership is never inferred from transcript access.** Engine starts
    a detached `codex app-server --listen unix://…` process, then the adapter connects through the
    documented WebSocket-over-Unix protocol at
    `<state dir>/codex-app-server.sock` (production: `~/.claude/fleet-dash-prod-state/`). Do not use
    Codex's default control-socket path;
    that belongs to its standalone daemon manager. Do not replace this
    with `app-server daemon start` on the npm install: that manager requires the separate standalone
    Codex installer. Fleet-created threads and CLI threads
    actually loaded on that socket are persisted with `thread_meta.runtime_owner=fleet_shared` and
    may be steered by Fleet or an attached `codex resume --remote unix://...` TUI. A newly created
    Fleet thread immediately starts a visible normal `hi` turn; `thread/start` alone has no rollout
    and cannot be resumed by the TUI. `thread/resume` can abort an active turn or create a second
    runtime agent when ownership is wrong, so Fleet must never start or resume a Codex TUI. Render no
    Attach button and no disabled starting/turn-active/view-only terminal placeholder. Preserve an
    empty shell only while `thread/loaded/list` still contains it; otherwise it has no runtime or
    rollout and must be discarded as a ghost. An **Open** control is allowed only when bounded process
    discovery proves the exact thread UUID, Fleet socket, and one real TTY; it focuses that existing
    terminal and does nothing else. Keep it directly left of the full-chat overflow menu. `source=vscode` is not ownership
    evidence: App Server uses it for Fleet's rich-client and remote-CLI threads too. A thread present
    in the exact shared runtime's `thread/loaded/list` must be adopted regardless of that label and
    persisted with `runtime_owner=fleet_shared`. Only that persisted ownership is controllable; an
    unowned transcript stays headless + view-only. Expose normalized managed/external provenance
    separately from the raw provider label. Never restore the old takeover action: resuming one of
    those ids on Fleet's server creates a second runtime agent.
    A live terminal is a separate, narrowly proved transport—not ownership evidence. Discover it
    only from a bounded process listing whose argv contains `codex resume`, the exact Fleet Unix
    socket, and one canonical thread UUID on a real TTY. Parent/child processes on the same TTY are
    one route; two distinct TTYs are ambiguous and enable nothing. That route may enable only text,
    image-path text, and focus. It must never enable close, interrupt, approval, archive, compact,
    review, takeover, or ownership, and its process/TTY details never enter the API. Exact App Server
    turn authority always wins over the terminal fallback. Both legacy `thread/compacted` and current
    `contextCompaction` item notifications carry the post-compact `turnId`; bind that id to the current
    transport generation and keep the turn running until `turn/completed`. This prevents compaction
    from leaving a stale pre-compact steer id or demoting a controllable turn to terminal typing.
    When App Server authority is genuinely absent, the exact terminal route may still flush recovery-
    outbox messages.
    Optional metadata must never poison or block the critical `thread/list` refresh. Read model
    choices from Codex's bounded, credential-free `~/.codex/models_cache.json`; never put automatic
    `model/list` calls on Fleet's shared control WebSocket. A `thread/read` failure is a per-session
    refresh warning: retain the prior preview, back off detail reads for 30s, and keep an owned
    session interactive. `thread/start` and `thread/resume` report model and effort, but the canonical
    `Thread` returned by `thread/list` / `thread/read` does not. Persist the selections in owned
    `thread_meta` at creation and on later settings changes, and use them as the refresh fallback;
    otherwise a daemon restart after compaction leaves a Plan-mode session unable to start its next
    turn. A lifecycle timeout closes only Fleet's client transport so the next call
    reconnects to the detached runtime. A provider-wide list outage marks cached state stale,
    disables direct mutation/close/archive/answer controls, and exposes only durable queue submit
    where ownership is proven. Reconnection is not evidence
    that a thread is unloaded: check `thread/loaded/list` before an exact on-demand resume and never
    resume every remembered thread, because `thread/resume` may abort an active turn.
    `CodexRolloutObserver` is the narrow exception to the adapter's no-rollout-parsing rule: the 32
    most recently updated external threads within 24 hours, plus explicitly pinned external threads,
    consume an allowlist of local lifecycle and visible-message events so `notLoaded` does not hide
    active CLI/Desktop/VS Code work. The observer is read-only,
    incremental, path-confined, row-bounded, and tolerant of malformed/unknown additions. Its result
    may update state, preview, and context, but must never update ownership or enable submit,
    interrupt, archive, close, attach, compact, review, or relay capabilities.
    Control authority is connection-generation scoped: only lifecycle notifications received on
    the current App Server connection may provide the active turn id for steer/interrupt. Rollout or
    `thread/read` evidence may show observed work but can never repopulate that authority. On loss,
    an owned active thread projects `control_state=reconnecting` and `queue_submit`, not submit.
    Direct text/images use the server Outbox with `origin=direct_send_recovery`,
    `kind=provider_reconnect`, and a client-stable idempotency key. Queue-owned images are private
    copies. Reconnect to the same authoritative active turn steers once; authoritative completion
    starts one next turn; ambiguous state stays `waiting_provider`. Terminal failures are visible
    and restorable but never auto-retried. A synchronous `turn/steer` rejection saying there is no
    active turn is the narrow definitive non-delivery exception: clear turn authority and stale
    running evidence, then retry the exact text/image payload once through `turn/start`. An expected-
    turn mismatch instead proves that another turn exists: clear local authority and queue the
    payload. Timeouts and unknown errors remain ambiguous and must never trigger a direct retry.
    Every existing-thread mutation requires both an exact current projection and persisted
    `runtime_owner=fleet_shared`; terminal fallback and focus obey the same gate. Live context
    requires an exact current projection, while closed context/file access requires an exact closed
    ledger row. `thread/list` pages to a bounded 1,000 rows and targeted-reads persisted owned IDs
    omitted from that window. Detail reads run in a bounded worker pool under one total refresh
    deadline. A malformed row is isolated as a stale, non-mutable row, including on first refresh.
31. **Now placement is an action queue, not a provider-state dump.** `Engine.organize_session`
    is the source of truth for `ui_group`, `reason_label`, `primary_action`, `access`,
    `reply_requested`, and `new_response`. Fleet Briefing precedes the session queue; session order is
    Pinned → Needs you → Working → Available. Recent external/view-only sessions stay in
    Working or Available; they move to History after `dormant_seconds`. The rollout observer's
    separate 24-hour discovery window does not control placement. Their lifecycle
    reason labels remain `Working` / `Available`; the separate `View only` access label carries
    ownership.
    Pinned/Needs/Working hide when empty; Available stays visible. The standalone History
    destination is decommissioned: the flat chronological session inventory (dormant, external,
    reopenable, closed) lives in **Search under TYPE=SESSION** — an empty query lists every session
    newest-first with access chips (All/Continue/View only/Reopen) and the provider filter; legacy
    `#history` deep links land there. `requests_reply` examines the newest
    complete assistant prose outside code/quotes, and only its FINAL question. Comprehension tags
    ("does that make sense?", "how does that look?", "right?") and idle solicitations ("what's
    next?", "anything else?") are excluded because they request no decision; a forced choice, a
    permission ask ("want me to X?"), or any other bare interrogative ending still counts.
    Its revision remains Needs you until a user reply
    or `mark_available_session`; opening does not clear it. `mark_read_session` clears only the New
    response badge. Provider-wide stale state preserves the last placement and renders one banner.
    Keep [`docs/session-organization.md`](docs/session-organization.md) synchronized with any mapping.
32. **Claude history backfill indexes main transcripts, not subagents.** On the first scan after each
    daemon start, `backfill_claude_history` discovers `~/.claude/projects/*/*.jsonl`, extracts bounded
    head/tail metadata, and upserts by session id/path. Nested `subagents/*.jsonl` files remain part of
    their parent conversation and must not become session cards. Unknown historical cost and agent
    counts stay SQL `NULL`/unavailable rather than becoming fabricated zero measurements. History is
    paged in the browser 100 rows at a time.
33. **Working order is entry order, not activity order.** `stable_working_order` retains incumbents,
    appends sessions newly classified as `ui_group=working`, removes sessions that leave, and persists
    `working_order` in config. `_persist_config_fields` serializes poll/UI writers and atomically
    replaces the file; a pin or settings write must not erase the order. Poll-time activity, quiet
    time, and transcript changes must never move a card within Working. A session that leaves and
    later re-enters appends as a new entrant.
34. **Optimistic chat rows remain until canonical confirmation.** Ordinary text and structured-
    question answers render immediately in the owning session conversation. Direct text keeps its
    spinner until an equal normalized user row appears in refreshed context; an unrelated revision
    is not confirmation. A structured-answer click nonce-suppresses its selector and inserts the
    labeled receipt before any full render or native request wait. Structured answers render their
    actual labels (never secret free text) and keep the spinner after provider acceptance until the
    canonical QA event arrives. A definite answer failure gets a red Restore button that removes the
    receipt and reopens the preserved selector without retrying; an uncertain answer has no Restore.
    A direct-text HTTP failure or 15 seconds without its confirmation produces a red restore button;
    restore refills the composer and never retries. A focused composer must not
    block `#sbody` transcript repaints—preserve the composer below the body update instead. The main
    fleet card mirrors the newest question-answer placeholder as a compact delivery receipt. Inline
    permission/dismiss/elicitation actions create the same receipt even when the card's More panel
    is closed; failures remain visible beside the still-actionable request.
35. **Session card peeks are capped at exactly 800 characters including the ellipsis.** The server
    caps both providers; CSS controls the collapsed line count. When measured content overflows, the
    final collapsed row is a clickable `...`; tapping anywhere in a truncated collapsed peek expands
    it. A fully visible collapsed peek does nothing, and exposed expanded content does nothing—only
    the explicit **Less** button collapses it. Expansion removes the height clamp but does not fetch or imply more than the
    bounded 800-character payload. Full-chat conversation text is never clipped: `_convo_add`
    retains complete rows and complete consecutive-assistant merges.
36. **Session workspace state is unified and route-owned.** Chat, Files, Subagents, and Details are
    panels of one `#sview` shell with one contextual composer and one pending-request drawer; the
    desktop docked-pane vs full-screen presentation (invariant 72) changes only chrome, never
    routes, section state, or composer/drawer ownership. Stable
    routes use `#session/<sid>/<section>` plus opaque file IDs or hard-validated agent IDs; neither
    routes nor context payloads expose local paths. Browser Back clears a selected file or agent,
    then returns to the prior section, then exits the workspace. Preserve per-section scroll,
    composer drafts, attachments, and the selected section across polling renders. On mobile,
    horizontal swipes move one section at a time and stop at Chat/Details. A right swipe beginning
    within the left-edge gutter exits the entire workspace; horizontally scrollable content and
    editable controls retain their gesture instead of changing sections.
37. **Subagent filtering never changes hierarchy order.** Every initial Subagents open selects
    `Active`; `All` exposes terminal agents. Filtering only changes visibility, retaining otherwise
    filtered ancestors needed to place an active descendant. No agent is selected by default. Relay
    still travels through the parent (invariant 19), and is disabled for terminal agents or while
    the parent is waiting.
38. **Closed-session send is exact, text-only, and idempotent.** Only resumable Claude sessions and
    Fleet-owned Codex shared-runtime threads may expose resume-and-send. The request contains only
    session ID, text, and request ID; persisted delivery state makes retries harmless. Wait for the
    exact session to become writable before delivering once. Failure preserves the draft and closed
    state. Never infer Codex ownership from transcript access, source, or thread visibility.
39. **New-session identity is optimistic but exact.** `spawnProvisional` immediately owns one
    client-generated card/full-chat identity (and any scheduled initial message) while
    `/api/act spawn` is pending. Only the exact server-returned `session_id` may replace it; never
    reconcile by cwd. A Claude initial text is sent after that exact session becomes discoverable,
    while Codex accepts it atomically at thread creation. Explicit spawn rejection may offer retry;
    a lost response may have created a session and must not retry automatically. Forecast requests
    are abortable and sequence-gated so an older model result cannot overwrite the latest selection.
    Console locked decisions: the spawn form fills the session workspace pane (docked beside the
    queue on wide desktops, full-screen on mobile; `#newsess` in Now keeps only the entry button)
    and on spawn the pane becomes the provisional session's chat; the form has **no initial-message
    field** — the first message is typed into the live chat, or into the Schedule overlay for a
    scheduled spawn; quick-spawn recents are device-local (`fleet.quickSpawns.v1`, non-secret spawn
    configuration only) and right-click/context-menu pins a row. Message and relay
    composers are `<textarea>` controls: Return is always a newline; only Command-Return on macOS or
    Control-Return elsewhere sends.
40. **Claude permission mode is a native, state-gated control.** `Tail.permission_mode` accepts only
    Claude's allowlisted transcript values. Live changes are allowed only for an idle registered
    Claude session with no hook/transcript request, compaction, turn-start fence, or unresolved
    control delivery, and whose process exposes the target in `permission_modes`;
    `act(permission_mode)` composes fixed Shift+Tab steps and retains the 0.4s native-TUI inter-key
    delay. Each key is acknowledged separately, every accepted intermediate mode is persisted, and
    a lost acknowledgement fails closed across restart until newer native evidence arrives.
    `dontAsk` is a
    new-session-only Advanced choice because it is not in Claude's live cycle. `bypassPermissions`
    is exposed only when the already-running process was launched with Claude's enabling flag and
    the client must show a separate high-warning confirmation every time. Fleet never accepts
    Claude's folder-trust or bypass warning on the user's behalf.
41. **Secondary-worktree cleanup is preview-ticketed and happens after provider close.**
    `close_worktree_preview` resolves the canonical workstream identity, refuses primary/unregistered/
    prunable worktrees and unrelated Git locks, and returns bounded porcelain-v2 dirty plus
    ignored-file evidence. A `claude session … (pid <same pid> …)` lock is the narrow exception:
    it remains locked during preview and is released only after that exact Claude process closes.
    Ignored paths can number in the millions: stream their NUL output into an exact count, full
    digest, and first 40 paths under a fixed byte/time cap; never buffer the whole list.
    Its opaque five-minute ticket binds session/provider/root/worktree and the exact status revision.
    Normal removal requires clean status and no ignored files; force requires the explicit dirty path;
    either is refused while another live Fleet session uses the exact worktree. Only after close marks
    the ticket may `cleanup_closed_worktree` re-probe the revision and run fixed argv
    `git -C <root> worktree remove [--force] <worktree>`. Never delete the branch. A close/cleanup
    partial failure is reported as session closed with the worktree preserved; never retry silently.
42. **Full-chat operational status is bounded, cached, and provider-honest.** `status_line` is the
    only payload for the strip directly above the main/subagent composer. Claude `Tail` retains the
    latest usage, turn cost, and at most 50 changed CacheWrite values; writes above 20k increment the
    spike ledger. Context excludes output tokens. Git comparison is fixed to
    `refs/remotes/origin/main...HEAD`, runs via fixed argv on a background cache refresh, and never
    fetches or blocks an HTTP/render path. Main cost is the session tree with a bounded breakdown;
    subagent cost is child-only. Unknown Codex/starting fields are omitted, never rendered as zero.
    `session_runs.status_line_json` freezes the final bounded payload for closed sessions; completed
    child payloads freeze with the child. Full-chat headers carry the title, one quiet identity line
    (project · branch · provider · access), and controls — operational metadata (git state, model,
    context, cost) still renders only in this status strip, never in the header.
43. **Routine Claude conversation reads never wait for the fleet-wide Tail fold, and neither does
    anything else the scan is not folding.** The lock covers `_scan_claude_sessions` and the
    status-strip read only (see the architecture note); everything else in the scan runs unlocked.
    `_scan` publishes
    immutable bounded main snapshots keyed by session and subagent snapshots keyed by
    `(parent_session_id, agent_id)`; `/api/context`, `/api/agent_context`, and `/api/file` read those
    projections without `scan_lock`. The locked `Tail.poll()` path is startup/fallback only, before a
    completed scan has published that exact conversation. Keep the parent in the child key because
    different sessions can reuse an agent ID. Snapshot pruning follows the live registry/child set.
44. **Web Push subscriptions are write-only outbound credentials.** `/api/push/subscription`
    accepts only action-token-authenticated HTTPS subscriptions whose endpoint is port 443 on a
    built-in push-service origin (or an exact operator-configured origin), with no credentials,
    fragment, IP literal, oversized URL, or malformed P-256/auth material. The server derives the
    origin; it never trusts a client origin field. Device/config/list projections expose only ID,
    display name, platform, permission, enabled/read state, bounded preferences, health, and
    timestamps—never endpoint, origin, subscription JSON, keys, or raw failure details. The PWA
    reuses `fleet.briefingDevice.v1`; re-registration preserves its read cursor and preferences.
    `FleetOperations` keeps the shared ledger mode 0600 because it now holds encrypted-push
    subscription credentials.
    `/sw.js` has root scope. Navigations and shell assets are network-first; the worker caches the
    token-free root shell plus the explicit versioned assets. The only cached API response is the last
    successful exact `/api/fleet` snapshot, stored under a fixed key on that device. Offline fallback
    adds `X-Fleet-Offline: 1`; the client renders the snapshot read-only, preserves drafts, and never
    mistakes it for current provider state. Never make unhashed shell assets cache-first. Bump
    `SHELL_CACHE` for structural shell changes. All other `/api/*` responses—including actions,
    conversation detail, notifications, settings, search, and token-bearing URLs—remain network-only.
45. **The session-peek line setting caps ordinary collapsed-card height; short content shrinks
    it.** A `.fixedpeek` card uses the measured fixed frame `76px + lines × 17.9px` on desktop and
    `100px + lines × 17.9px` on mobile, where `lines = min(preview_session_lines, measured content
    lines)` — `schedulePeekOverflow` writes the post-layout minimum into the card's
    `--session-card-lines` each render so a short last message never reserves empty preview rows
    (operator decision 2026-07-24); the meta rail renders as a 24px inline status row
    directly under the header (Console 10a — `.cmain{display:contents}` + flex `order`
    interleave the one `.cmeta` node; never render it twice, which breaks strict-mode
    locators) (or zero preview rows when session peeks are disabled); the card
    header itself opens Chat (invariant 63). Never put
    `.fixedpeek` on an open card, an explicitly expanded peek, or a card showing a pending request,
    error, reply request, inline delivery/pin feedback, or running subagents: those cards must grow
    to keep every action visible. Keep the height inputs synchronized with the header/peek/meta
    CSS measurements if their typography or padding changes.
46. **Web Push delivery is isolated, durable, and secret-redacted.** VAPID/action material exists
    only in ignored `push-secrets.json`: it must be a same-owner regular file with mode 0600, and
    malformed or weak-permission content disables delivery instead of regenerating keys. The Python
    supervisor resolves a fixed Node 18+ executable without a shell, strips proxy environment, and
    speaks bounded JSONL to one restart/backoff-managed `web_push_worker.js`. The worker validates a
    fixed/exact push-origin allowlist, resolves only global addresses, pins the chosen address into a
    TLS-verified HTTPS request, permits no redirect/proxy/custom client headers, and uses
    `web-push.generateRequestDetails` rather than its network sender. The pinning lookup callback
    supports both the legacy single-address form and Node 24's `all:true` record-array form. When a
    host is dual-stack, prefer a validated IPv4 record because this Mac may have AAAA DNS answers
    without an IPv6 route; retain the validated IPv6 fallback for IPv6-only networks. HTTP actions
    and provider scans
    only persist/coalesce jobs; the background thread claims SQLite leases and applies bounded
    jittered retry for timeout/429/5xx, Retry-After, and helper failure. A 404/410 or revoked
    permission scrubs the subscription and disables the device. Authenticated status contains only the
    VAPID public key, helper state/restart count, and queue aggregates; endpoint, subscription keys,
    private keys, and raw errors never cross a read API. A successful Settings test qualifies that
    exact subscription; changing the subscription clears qualification until a new test succeeds.
    Startup forces config/log/database sidecars to 0600 and scrubs known current/legacy action,
    VAPID, token, topic, and dashboard secrets from the existing log in place; replacing the log
    inode would strand launchd's open file descriptor and is not equivalent.
47. **Notification Center owns durable interruption history; Now owns live actions.** The old Now
    Briefing block must not return: Briefing is an on-demand section beside Needs action, Updates,
    Snoozed, Problems, and History. `GET /api/notifications` is action-token protected and projects
    canonical event counts, per-device unread state, session mute state, and redacted delivery
    problems. A browser identity needs no push registration: its first successful snapshot seeds a
    durable read cursor at the current edge, later reads are monotonic, and a future push
    registration inherits that cursor. Exact `#notifications/<opaque-id>` routes load the current
    event successfully before marking through its sequence; browser/native back closes the detail.
    Snooze, Wake, Mute/Unmute, and Retry are separate token-gated POST routes and event mutations
    require the exact source revision. Badges count canonical active/unread events, never delivery
    attempts. The desktop list runs full-width and a tapped row grows a thin actions sub-card on
    that row (snooze 15m/1h/tomorrow, mute, mark read, Details — Console locked decision: never a
    bar on the chat pane); a session event's cue button opens its session (the docked pane on wide
    desktops), and an open exact detail route restores the list/detail split. Mobile detail stays a
    fixed drawer above the bottom bar.
    Delivery failures may expose bounded device name/platform/status/timestamps and retryability,
    never endpoint, origin, subscription material, keys, or raw errors. Canonical reconciliation is
    incremental: its signature contains only push-relevant actions, mutes, eligible stalls, provider
    failures, and the in-process Briefing generation. An unchanged signature performs no projection
    I/O; a provider failure bypasses the cache until corroborated, and daemon restart always runs one
    full reconciliation. `notification_kind_policy.in_app_enabled` is independent of push cadence.
    Ordinary lists, active/unread counts, title/app badges, and delivery-problem projections include
    only enabled kinds after their in-app effective edge. Exact event deep links remain readable so
    a push can open canonical detail even when that kind is hidden from in-app lists.
48. **Production Web Push is globally policy-bounded and capability-authenticated.** One durable
    global policy applies to every enabled device. `notification_global_policy` owns master state,
    quiet hours, timezone, and revision; `notification_kind_policy` has one row for every canonical
    kind with an independent in-app switch plus Off/Once/Once+reminder/Repeat, severity floor,
    initial delay, repeat interval, maximum
    successful wave count, and quiet-hours bypass. Defaults preserve the former interruption
    behavior: question/approval/form/reply/failure use one reminder, stall sends once, and
    informational kinds are Off. Device rows own connection, enable/pause, permission,
    qualification, and health only—never cadence overrides. Session mute and event snooze outrank
    quiet hours and kind rules. Quiet hours hold/coalesce rather than replay missed intervals. A rule
    newly enabled for existing active events schedules nothing unless `apply_current` is explicit;
    a worst case above 12 pushes/day requires high-cadence confirmation. Policy writes use expected
    revisions. In-app-only edits increment the public row revision but not `push_revision`, so they
    cannot suppress unrelated push jobs; every schedule/claim/retry/wake revalidates the push
    revision so obsolete jobs
    become suppressed. Transport retries do not consume another user cadence count. A target device
    must still be enabled, permission-granted, subscription-present, explicitly test-qualified, and
    healthy. Delivery rows persist an explicit purpose plus source and policy revisions. Mute is
    session-wide and must update both `muted_sessions` config and the notification DB before future
    claims. The encrypted payload is generic, uses a hashed tag and exact
    `#notifications/<event>` link, and carries only Open plus optional Snooze/Mute capabilities.
    Capabilities are HMAC-signed for one event/device/action, expire after ten minutes, persist only a
    consumed JTI hash, and are accepted solely by credential-omitting
    `/api/push/capability-action`; that route ignores `act_token` and returns one generic rejection
    for malformed, expired, replayed, or stale tokens. A failed shortcut keeps the system
    notification visible and opens current Fleet detail without putting the capability in a URL.
    Removing a device deletes its redacted registration/read cursor and suppresses its queued jobs;
    historical delivery rows retain only the opaque device id. Disconnecting the current device may
    instead preserve its redacted row so it can reconnect with its read position intact.
    Settings explains this model in a collapsed field guide and gives every expanded event kind and
    control a one-line definition. Severity labels are user-facing thresholds: All events maps to
    `info`, Warning or Critical maps to `warning`, and Critical only maps to `critical`. Briefing
    severities must normalize before entering `notification_events`: `success` → `info` and `high`
    → `critical`; existing rows are migrated at DB initialization. Never let an unranked Briefing
    severity silently make an enabled event ineligible for push.
    The separate legacy flag enables only one manually invoked generic ntfy test route; provider
    scans never dispatch it and it is never a Web Push fallback or duplicate path.
49. **One provider limit blocks one session, never the dashboard.** App Server error payloads are
    string-or-object; normalize them to bounded scalar text before storing or rendering. Recognized
    rate/usage/quota/context limits set only that thread to `blocked`, disable its submit capability,
    and place it in Needs you as **Limit reached**. A client render exception is a display error, not
    “server unreachable.” The HTTP server binds before the first provider scan so one slow/malformed
    session cannot remove the dashboard during startup.
50. **Non-secret text drafts are device-local and send-cleared.** `fleet.drafts.v1` stores bounded
    composer, relay, handoff, schedule, new-session, request-Other/form, and filter text. Poll renders,
    overlays, navigation, and reloads must restore it. Empty input deletes its key immediately;
    successful send/schedule/handoff/spawn/request acceptance clears the owning key/prefix. Failed or
    failed delivery preserves/restores the text. Never attach a draft key to a password or provider-
    declared secret field, and never put drafts in a server API.
51. **GitHub is an external destination, not a Fleet detail page.** Repository observation projects a
    validated canonical HTTPS `github_url`. Workstreams render it as a normal external link so the OS
    may open the GitHub app or website. Briefing repository links use the same URL. Do not restore the
    `#repoview` overlay, session-menu Repository outcome entry, or duplicated GitHub/Git/PR form UI.
52. **Known-offline ordinary messages use a device-local queue.** `fleet.offlineMessages.v1` stores at
    most 100 bounded messages and renders a persistent **Queued offline** receipt. A successful,
    non-cached `/api/fleet` poll is the only reconnect signal that may start the FIFO flush. Slash
    commands and skills stay as drafts because their semantics may be destructive. Remove a queue row
    after provider acceptance or a known rejection. If the connection drops after dispatch begins,
    remove it from automatic retry and show a manual restore failure: delivery is unknown and an
    automatic retry could duplicate the message. A bounded `fleet.contextCache.v1` retains recent
    main/subagent/closed conversations on that device. Refresh merges the newest tail by stable
    identity/cursor without discarding loaded older pages; fetch failure preserves last-good messages
    with stale/error metadata. Never send from the service worker.
53. **Full-chat work activity is independent of transcript output.** `#sactivity` is a non-overlay
    footer below the `#sbody` scroll area, so it remains visible without covering messages. Show the
    main indicator for session `running`/`stalled`, and a separate count for every child not in
    `done`/`ended`; either signal may appear alone. The expandable detail names active children and
    labels stalled work as slow, not stopped. Preserve the native `<details>` open state across polls
    by replacing its HTML only when the main/child state signature changes.
54. **Phone images are private, scoped attachments—not client paths.** The full-chat composer stores
    at most four 10 MB JPEG/PNG/GIF/WebP/HEIC/HEIF blobs in lazy IndexedDB and keeps only opaque draft/
    queue ids in localStorage. `/api/upload-image` is token-gated, rejects chunked or oversized bodies,
    validates magic bytes, ignores the client filename for path construction, normalizes through fixed-
    argv `sips`, and removes every JPEG APP/COM metadata segment (including EXIF GPS/XMP). Files and
    0600 metadata live under the 0700 `fleet-dash/uploads` directory, are bound to one server-observed
    live or ledger-backed session, and expire after 24 hours. A temporarily missing known session may
    accept the upload only for a durable exact-session queue; an unknown id remains rejected.
    `image_text` and the default `send_message` path resolve ids
    server-side: Codex receives native `localImage` inputs; Claude receives only Fleet-managed
    absolute paths in injected text. Upload IDs are immutable and collision-rejected. Live upload
    storage is capped at 32 files/80 MB per session and 200 files/512 MB globally; raw and normalized
    retained sizes are checked under the upload lock, and cleanup rotates through the whole directory
    rather than sampling the same prefix forever. Image
    uploads may retry before provider dispatch; an unknown dispatch outcome never auto-retries. The
    service worker never caches image bytes.
55. **Heavy destinations paint before they work.** Opening Settings must reveal the destination and
    its existing loading-spinner pattern before building the full settings tree. Search TYPE=SESSION
    renders its cached session list on navigation and refetches only when criteria change;
    revisiting must never paginate implicitly—only **Show more** owns the next 100 rows. Opening
    New session paints only the workspace pane form, not a synchronous full-fleet render. A
    notification detail action paints its busy/success state inside the active detail pane, not by
    rebuilding the list and every filter; canonical list reconciliation remains asynchronous. Keep
    these narrow commits: rebuilding whole surfaces produced 133–1026 ms first-feedback outliers even
    though their network work was asynchronous.
56. **Production and staging are hard-separated instances.** Production code runs from the dedicated
    `~/.claude/fleet-dash-prod` checkout on 8377 with state in `~/.claude/fleet-dash-prod-state`;
    development/staging runs from the `~/.claude/fleet-dash` checkout on 8378 with state in
    `~/.claude/fleet-dash-staging-state`. Shared hook/statusline captures (pending/, effort/, usage.json)
    live in `~/.claude/fleet-dash-capture` (`FLEET_DASH_CAPTURE_DIR`, set by both plists).
    `server.APP_ROOT` is always the directory containing `server.py`;
    `paths.BASE` is instance runtime state selected by `FLEET_DASH_STATE_DIR`. Never collapse those
    concepts again: production must not serve live-edited staging assets, and the repo root holds
    source only — never runtime state. Staging has its own config,
    token, ledger/search DBs, uploads, log, Codex socket, injector request/result files, applet bundle
    ID, browser origin, service worker, drafts, outbox, and push registrations. It may READ the shared
    registries/transcripts and hook/statusline captures, but a server-side exact-ID allowlist
    (`staging_owned_sessions`) strips capabilities from every production session and rejects every
    mutation even if a client forges the POST. Only sessions spawned by staging are registered; every
    spawn is forced into a server-created `fleet-staging/*` worktree rooted under staging state.
    Double-underscore request keys are stripped before policy checks, so a client cannot claim the
    internal prepared-worktree marker. Notification projection in staging receives staging-owned
    sessions only; production requests/provider failures must never leak into staging pushes.
    Browser credentials use the instance-scoped `act_token_production` and `act_token_staging`
    cookie names. Cookies do not distinguish ports, so a shared `act_token` name makes opening one
    instance silently break authenticated reads and actions in the other.
57. **A collapsed session peek hugs its content up to the configured line cap.** The peek box and
    the card frame both key off `--session-card-lines = min(preview_session_lines, measured content
    lines)` (invariant 45), so a one-line message renders a one-line peek on fixed and non-fixed
    cards alike — the configured value is a MAXIMUM, not a floor (operator decision 2026-07-24).
    Keep message content top-aligned and the truncated `...` control bottom-anchored; never
    return unused preview height as a strip of card background.
58. **The mobile full-chat composer follows the visual viewport.** `syncVisualViewport` projects
    `window.visualViewport.height/offsetTop` into CSS variables used by `#sview`, `#aview`, and
    `#viewer`; the underlying Now screen must never peek through beside the iOS keyboard. Full-chat
    actions are split into a scrollable `.session-context` and a non-scrolling `.composer-dock`.
    Focusing the main composer hides that context strip so chat history gets the remaining height;
    a vertical drag starting in `#sbody` blurs the composer, while a tap does not. Textareas use 16px
    type on coarse pointers to prevent iOS focus zoom, auto-grow only to a bounded height, and keep
    Return as newline-only. `renderComposer` is the sole main-session composer for both full chat and
    the file viewer: it owns one exact `＋ | textarea | Send` row, draft/image state, menu, send path, and
    feedback. All three resting controls share the 44px token; newline input grows upward through four
    lines. Chat's separate upper strip is `status | latest received file`; the file viewer's is
    `file browser | Chat`, with equal 42px controls. The file browser's outer height stays 42px;
    its chips fit inside the border and render at most 40 filename characters plus an ellipsis,
    preserving the full filename in `aria-label` and `title`. Chat's collapsed status and fixed latest-file
    button are 44px; the expansion chevron lives inside the compact status rather than adding a third
    row. The file button bottom-aligns when status expands and must never stretch. The upward **＋**
    menu is the only entry point
    for **Send picture** and **Schedule message**. Photo must be the actual transparent file input/
    label tap target—never a synthetic `picker.click()` after mutating or dismissing the menu—so iOS
    retains trusted activation. Cancel changes nothing; selection closes the menu and stays drafted
    without refocusing. Safari's native keyboard
    accessory bar is not controllable from a web app, so layout must remain correct with it present.
59. **Settings is section-routed and mutation-safe.** Settings is an ordinary left-column
    DESTINATION (`#route-settings`, operator decision 2026-07-24 — it renders beside a docked
    session pane like Search or Notifications and never covers it; the old full-screen
    `#settingsview` overlay must not return). It owns Notifications, Devices &
    delivery, Sessions, Appearance, Budgets & spawning, and Advanced at exact
    `#settings/<section>` routes. Desktop uses a rail; mobile renders one section under a sticky
    selector. Browser/native Back unwinds section history before leaving the destination; an open
    session pane simply stays open beside it. Async loaders may rerender only the section whose data they own—an
    unrelated push/workstream/budget response must never detach an active control. Inputs that save
    on blur must not synchronously replace the button the user is clicking. Settings serializes
    reentrant renders and defers data-driven replacement while a text, number, or select field owns
    focus; checkbox state never blocks a legitimate refresh. Policy edits lock their
    own row while saving, use expected revisions, paint Saved/Error in place, and roll back on
    failure. Muted-session search filters existing rows in place so typing focus survives.
    Console section contents (locked, mockups 9a/11a–11d): Devices & delivery also owns the
    manual legacy-ntfy card; Sessions owns the conversation-peek toggles/line counts, the stall
    threshold, muted sessions, and the pinned-session list (unpin only — pinning stays the card
    gesture, invariant 28); Appearance owns only device-local presentation — the reading theme
    control writes invariant 27's `viewer_light` (Console dark / Paper light, no app-wide theme),
    plus navigation side and reading width. Advanced keeps diagnostics only. Each policy-rule
    summary renders the two independent status chips (`FLEET CENTER · ON/OFF`, `PUSH · <cadence>`)
    because in-app visibility and push cadence are separate controls (invariant 47/48).
60. **A fullscreen question is a persistent, independently scrollable drawer.** `#sact` renders a
    `.question-drawer` keyed by session id + pending nonce. Preserve its nested scroll position when
    the two-second poll replaces the action DOM; the conversation and question have separate scroll
    state. Suppress replacement for the entire pointer-resize gesture AND for a short settle window after pointerup (`questionResizeSettling`) — a forced render landing between pointerup and the next paint detaches the live drawer and discards its reading position, which is the same damage the gesture guard exists to prevent. Its horizontal grip uses
    Pointer Events with `touch-action:none`: dragging upward clamps below the title bar while leaving
    the composer visible; dragging below the snap threshold collapses to a waiting bar. Keyboard
    Up/Down resizes, Home collapses, and End expands. Persist height/collapsed state per nonce in
    bounded localStorage and clamp it after viewport/orientation changes. A drawer the user never
    resized hugs its content via `max-height` (no dead space under short questions); the first
    explicit resize clears that cap and switches to the persisted fixed height. The pend's own
    ✕ dismiss button is CSS-repositioned into the drawer header row (its `.ptlabel` is hidden there
    as a duplicate of the drawer title, which carries `nativePromptLabel` honesty) — it stays the
    single `.xbtn` dismiss control. Changing question pages
    gets a distinct scroll key; unrelated refreshes must never return the current page to its top.
61. **Ordinary Send is one server-owned send-now-or-queue decision.** The browser sends normal text/photos
    as `send_message` with an idempotent `client_request_id`; slash commands and skills keep their
    immediate-only actions. `Engine._send_now_or_queue` may deliver immediately only when the exact
    target is Available, or when a Fleet-owned Codex App Server turn has
    `control_state=connected_active` and can be steered now. Typing into a busy Claude or attached
    Codex terminal is NOT immediate delivery: persist an `origin=automatic_fallback`,
    `kind=when_available` Outbox row and render **Queued · waiting for session** without arming the
    15-second transcript timer. `OutboxManager.create_delivery` owns idempotency and copies temporary
    image uploads into private queue storage before publishing the row. Provider-state refresh,
    browser exit, and daemon restart must not lose or duplicate it. When availability is proven,
    dispatch exactly once; keep the optimistic row until canonical transcript confirmation. A
    connection drop after an immediate HTTP dispatch stays **Delivery unconfirmed** and is never
    auto-retried because the server may already have accepted it. **Never use `ui_group` as an
    availability signal.** An idle provider whose assistant asks a direct question belongs in
    **Needs you**, but it is immediately writable; making Outbox wait for that card to become
    Available deadlocks the very reply that would clear it. Gate sends on native state, pending
    requests, compaction, and exact control authority only. Provider discovery is asynchronous after
    daemon restart: an exact target missing from one snapshot waits through a bounded 120-second
    reconnect grace and dispatches if it reappears. Only continuous absence beyond that grace may
    block the delivery.

62. **Polling and context refresh are single-flight and last-good.** The two-second fleet poll starts
    its next request only after the current one settles; a bounded timeout marks a genuinely hung
    request offline, while force-refresh alone may supersede an older generation. Never abort every
    slow-but-progressing response on the next interval. Main, closed, and subagent context failures
    retain the last-good in-memory/device cache. Conversation revision refreshes merge the current
    tail into older loaded pages and preserve the reader's scroll anchor.

63. **Full-screen overlays are stack-aware modals — except the desktop-docked session pane.**
    Search/Handoff/Outbox/Schedule/Confirm surfaces (Settings is a destination, invariant 59,
    not an overlay), and the session workspace whenever it
    presents full-screen (mobile and viewports below the invariant-72 dock threshold), have dialog
    semantics, an accessible name, focus entry/trap, inert lower layers, and opener restoration. The
    highest visible z-index owns focus. When the workspace is DOCKED (`#sview.docked`) it is
    deliberately not a modal: `aria-modal="false"`, it never joins the modal stack, never inerts the
    shell, and never traps Tab — the queue stays interactive beside it — but it still goes inert
    beneath any real stacked modal above it. Every one of
    those surfaces uses the shared visual viewport on phones; focused fields scroll into the
    remaining viewport instead of exposing the screen beneath the keyboard. The labelled, focusable
    card header opens Chat by pointer or keyboard; live Claude and Codex cards must not duplicate
    navigation-only Open/Continue/View actions as buttons. Respond/Review, Pin, terminal focus, and
    other explicit controls stop propagation and retain their own action. History rows keep their
    separate action buttons.

64. **Outbox identity and retries are concurrency-safe.** Scheduled creation persists the browser's
    stable `client_request_id` as the unique idempotency key. Editable updates/retargets require the
    exact expected version in the SQL predicate. A retry copies queue-owned images into independent
    storage and refuses missing bytes; one source may have only one active direct retry. A proven
    successful descendant marks failed ancestors `superseded`, preserving audit text while clearing
    attention. Fair selection orders by `next_attempt_at` so unavailable rows cannot starve ready
    work; unchanged usage-reset rows always move their next check forward. Per-row **Delete** is a
    durable cancellation/archive action: an unclaimed pending row becomes `cancelled`; a terminal
    row retains its true state while `cancelled_at` and `cancelled_from_state` move it into the
    **Cancelled** UI category. Never delete an active `sending`/`spawning` claim, and remove only
    queue-owned attachments after the cancellation transaction succeeds.

65. **Existing-chat model/effort changes are provider-native and compare-and-set.** The full-chat
    overflow renders controls only from `models_by_provider` plus explicit session capabilities.
    Active rows disable them; external/view-only and staging-observed rows expose no control. The
    browser repairs effort immediately when model changes, serializes model/effort/mode/permission
    writes per session, and versions responses. A definitive pre-launch failure restores the last
    accepted pair; partial acceptance or a lost acknowledgement retains the exact accepted prefix,
    shows a warning, and never invites a contradictory retry. Only
    an in-flight request may override canonical values; settled success/error feedback can remain,
    but a newer provider snapshot immediately owns the labels, selectors, and next expected pair. The
    server requires both expected values, revalidates them against live provider state, and checks
    the server-owned catalog. Codex catalog rows are bounded, stripped, control-character-free,
    visibility-filtered, and deterministically deduplicated; a changed cache that fails parsing
    clears the catalog and controls rather than retaining stale options. Claude accepts this
    action only while its registry is idle with no hook/transcript request and no active compaction,
    and composes only exact `/model <allowlisted-id>` and `/effort <allowlisted-level>` commands;
    arbitrary command text is never accepted. `/model` and `/effort` are separate acknowledged
    native transactions so a second-command failure cannot erase the first accepted value. Accepted
    Claude model/effort/permission projections persist with transcript byte offsets or statusline
    mtimes across daemon restart, and retire only on newer native evidence; transcript row timestamps
    are not ordering authority because compaction can append older-timestamp rows. A per-session
    Claude mutation lock spans the fresh
    registry/tail checks, terminal write, and accepted projection, including Outbox image delivery.
    After a submitted message, a turn-start fence blocks or queues registry-lag follow-ups until
    Claude reports active then idle, or the changed transcript folds to awaiting input. Codex uses
    `thread/settings/update`; one per-thread mutation lock serializes settings, mode, turn starts,
    and compaction, with live active/pending/compacting state rechecked immediately before mutation.
    Persisted settings revisions cover both settings and mode changes, use compare-and-set, and
    refresh projection commits reconcile under
    the same barrier so an old in-flight refresh cannot overwrite an accepted save. Write
    `thread_meta` only after App Server acceptance. If that durability write fails, return success
    with an explicit warning and keep the accepted runtime/UI projection; never report a false
    provider rejection. Model/effort saves preserve a newer live collaboration mode; mode saves
    preserve an explicitly null live effort rather than reviving stale metadata. Refresh, compaction,
    daemon restart, and the next turn retain durable values.

66. **Unknown native delivery is never safely retryable.** The applet launch result divides
    definitive pre-launch failure from post-launch confirmation loss, and every transport must
    make the same split — `_native_write_failed_before_delivery` is the one place that lists the
    proven-failure codes (`injector_not_launched`, `background_connection_lost`,
    `terminal_not_available`). A subprocess timeout is never proof: tmux may have handed the bytes
    to its server before the client gave up, so a lost result is uncertain even on the first key.
    If Claude may have received
    message, image, relay, handoff, prompt keys, or a control, Fleet returns delivery uncertainty and
    does not auto-retry. Outbox records content as `confirmation_unknown`; direct surfaces tell the
    user to check the terminal and do not offer Restore/retry. Prompt uncertainty is durable and
    nonce-scoped, including one-key dismiss/deny: the same nonce emits no more keys across daemon
    restart. Hook captures use private atomic replace so partial JSON cannot erase that fence, and
    their reader closes every file deterministically. A missing capture while the registry still
    says `waiting` retains uncertainty. So does a non-waiting registry sample while the exact same
    capture remains valid: permission captures have no clear hook and can mask a later Notification.
    Clear only for a different valid nonce, session removal, or capture disappearance corroborated
    by provider completion. Question/approval controls are advertised only while that exact pending
    request and registry `waiting` agree. Background transport and every Engine caller likewise
    distinguish a proven failure before the first native write from ambiguity after any write.

67. **Full-screen reading position has explicit authority.** `captureReadingAnchor` /
    `restoreReadingAnchor` preserve the first visible chat message or Markdown block synchronously
    across composer focus, visual-viewport changes, and dock rerenders, then repeat after paint for
    WebKit geometry. Chat opens in follow-tail mode and stays there while its canonical tail remains
    within 120px; canonical replies and `ResizeObserver` growth repin after layout. Only a wheel,
    touch, scrollbar, or navigation-key gesture may disengage it—layout-generated scroll events do
    not. Failed/uncertain optimistic receipts are never tail authority. Definite failures expose
    Restore and Dismiss; uncertain rows expose Dismiss only. Resolving a server Outbox receipt writes
    a bounded tombstone so reconciliation and reload cannot recreate it. Repeated Claude or Codex
    file deliveries move the existing path to newest without duplication; chat exposes only that
    newest file beside status, while the file viewer retains the full file browser.

68. **Composer text cannot answer a native question.** When `pendingQuestion(sid)` is visible,
    `sendText` submits one `dismiss_then_send` action containing that exact nonce and the captured
    text/image upload ids. `Engine._dismiss_question_then_send` must accept the provider's nonce-bound
    dismiss first and then create an idempotent when-available Outbox row; it always queues after
    dismissal and never writes during the selector-to-chat transition. A changed nonce, rejected
    dismiss, or uncertain native delivery sends no message and leaves the composer draft/images in
    place. The device-local offline queue persists `dismissNonce` and replays the same compound action
    after reconnection. Never implement this as text followed by Escape, parallel requests, or a
    timer: text could select an option, and a delay cannot prove which native surface owns input.

69. **Codex child lifecycle has its own bounded refresh phase.** A parent `thread/read` supplies
    discovery metadata, but its `subAgentActivity` history may stop at `started` or `interacted` even
    after the child finishes. Reconcile every nonterminal child against the child thread: latest turn
    `completed` → `done`; `failed`/`interrupted` → `ended`; an idle/notLoaded child with turns →
    `done`. The child reads get a fresh `_refresh_budget_seconds` deadline after parent detail reads;
    never reuse the already-spent parent deadline. Child ids are immutable, so terminal state is
    monotonic across refreshes and terminal children are not re-read. A stale parent projection or a
    later timeout must never resurrect one into the active-subagent count.

70. **Workspace activity and sizing describe only what is actionable now.** The Subagents tab count
    and the session card's agent list use nonterminal agents only. That card list (`cardAgentList`)
    renders EVERY running/stalled agent — never capped, never re-sorted, so `agentRow`'s depth
    indentation still describes the spawn hierarchy — and it appears on any card with live agents,
    not only Working ones (user decision 2026-07-21, reverting the two-row preview). The row carries
    type, description, sparkline, then model/effort — throughput, total tokens, and cost were dropped
    from it (same decision); per-agent spend stays in the workspace status line. A card showing
    it must never take the `.fixedpeek` frame or the list is clipped (invariant 45). Tapping any row
    routes to `#session/<sid>/subagents` with that agent selected via `selectWorkspaceAgent`; the
    standalone `#aview` overlay (`agentTap`) remains the destination for the completed-agents fold,
    whose terminal rows the section's default Active filter would otherwise hide. Chat renders
    main-session work as one non-interactive newest-row
    indicator and never repeats active child details there. The parent composer/status bar uses the
    same wrapper in Chat, Files, unselected Subagents, and Details. Desktop Files/Subagents splits
    persist separate browser-local widths, clamp the list to 220–520px while preserving at least
    320px for the reader, and expose a labelled keyboard-operable separator. Mobile remains list-first
    and has no divider; once a file is selected, a horizontal file-chip strip above the reader
    (`#sfilechips`, Console 11e) switches files in place and replaces the back-to-list button —
    browser Back and the edge swipe still clear the selection per invariant 36.

71. **The browser app's ES modules share one global namespace on `globalThis`.** Shared mutable
    state is declared `globalThis.<name>=…` in its owning module — never a module-level `let`,
    whose published copy would go stale on reassignment. Top-level functions and consts publish
    via `Object.assign(globalThis,{…})` (functions in the module-top block, consts at module
    end) because inline `onclick=` handlers in generated HTML and cross-module bare-name
    references resolve through the global object. Never name a global after an existing `window`
    property. A new module joins four lists: `dashboard.html` modulepreload, `sw.js`
    `SHELL_ASSETS` (+ cache bump), `server.py` `APP_MODULES`, and the browser fixture server.
    Full contract and module roster: the `static/js/` file-map bullet.

72. **The Console two-pane shell: on wide desktops the session workspace is a persistent docked
    right pane.** On viewports ≥1200px CSS-wide, an open `#sview` docks (`html.fd-pane` +
    `#sview.docked`) beside the left destination column instead of covering it; below that
    threshold and on mobile it remains the full-screen surface, and a window resize converts
    in place (`applyWorkspaceChrome`). The splitter (`#ssplit`, the pane's left edge) resizes the
    PANE width and is keyboard-operable (arrows/Home/End) with ARIA values. **The divider position
    is the user's number** (operator decision 2026-07-24): `workspacePaneWidth` holds the manual
    preference verbatim (persisted in `fleet.paneSplit.v1`) and only the render-time
    `applyPaneSplit` clamps to the live bounds [650, min(1200, viewport − rail − 420)] px — a
    transient clamp (narrow window) must never overwrite the preference, and route changes must
    never move the divider (`html{scrollbar-gutter:stable}` keeps a destination's document
    scrollbar from shifting the fixed pane sideways). ⤢/⤡ (`#sexpand`) toggles pane ↔ expanded
    (`html.fd-expanded`): expanded keeps the nav rail, fills everything right of it, hides the
    covered `#appmain`, and centers chat at ~820px. **Expansion is transient and never persisted**
    (operator decision 2026-07-24, `fleet.paneExpanded.v1` removed): opening a session (card tap,
    new sid) and choosing any rail destination always return to the split pane; ⤢ only lasts
    within the currently open session view. The docked pane is NOT a modal
    (invariant 63); route ownership, sections, and Back behavior are unchanged (invariant 36). The
    card open in the pane carries `.paneopen`; needs-you question cards flip their cue to
    `ANSWER IN PANE →` exactly when docking is available (`workspaceDockable()`), staying
    `ANSWER IN CHAT →` otherwise.

73. **Transport selection is per-session, server-derived, and never client-supplied.**
    `_terminal_write(tty, steps, step_delay)` and `_terminal_spawn(command, label)` in
    `engine_transport.py` are the ONLY terminal entry points the rest of the Engine may call.
    They choose from state Fleet already resolved: the tty `_tty_for_pid` returned for that
    session's PID, matched against a live tmux pane map. A client never names a transport,
    socket, pane, tmux session, or command — that would make the act token a remote shell in a
    second way. `terminal_transport` selects `auto` (tmux for a tty that is a live tmux pane,
    applet otherwise; spawn joins the operator's tmux session when one exists — invariant 20),
    `tmux` (a non-tmux session
    exposes no terminal transport at all), or `applet` (the legacy iTerm2 path only — the revert
    switch). `_terminal_spawn` may retry through the applet ONLY on a `terminal_not_available`
    tmux result, which proves no window was created; a `delivery_uncertain` spawn must never
    fall through or the fallback opens a second session. The pane map (`_tmux_panes`, 2s cache, ≤8 sockets under `paths.TMUX_SOCKETS`) is
    refreshed only on the act/write path, **never inside `_scan`**: terminal discovery must not
    join the two-second fleet poll. Both implementations return `_iterm_write`'s exact contract so
    every caller keeps invariant 66's split unchanged. tmux is what survives the operator changing
    terminal emulator — the applet's whole verb surface is `tell application "iTerm2"`, so any new
    iTerm-bound call site is a regression, not a shortcut. Tests must never reach the developer's
    own tmux server: `paths.TMUX_SOCKETS` is the patch point and the shared fixtures pin
    `terminal_transport: "applet"`.

74. **Screen observation is read-only, and its refusals are the design.** `Engine.session_screen`
    (`engine_context.py`) plus token-gated `GET /api/screen` return the text a live session's tmux
    pane is rendering — the first time Fleet has been able to see a terminal rather than infer one
    from the registry word, the hook capture and the transcript fold. `_tmux_capture` sends no
    keys, so a capture can never disturb a session; it uses `capture-pane -p` (never `-e`, which
    would keep escape sequences), scrubs control characters, and bounds the result to 200 rows ×
    400 columns. It runs only on request — never on the scan path (invariant 73) — and the result
    is never cached, logged, or snapshotted. Screen bytes are the most untrusted input in the app:
    the client renders them inside a `<pre>` through `esc()` and nowhere else.
    **Four refusals, each enforcing an existing boundary rather than a missing feature:** a
    background Claude job (its private PTY bytes are readiness evidence that must never cross an
    API — invariant 25), any Codex thread (its terminal route enables exactly text, image-path
    text, and focus — invariant 30), a production session read from staging (a live terminal read
    is a capability, and staging holds none over sessions it did not start — invariant 56), and a
    session that is not in a tmux pane (the applet has no read verb at all). Adding a fifth
    consumer of the capture is fine; widening any of these four is a boundary change. The second
    consumer is `screen_prompt_kind` (invariant 77) and it obeys the same rule: request path only.

75. **One native prompt has one server-owned identity, and an answered prompt is fenced.**
    A prompt can be evidenced two ways — the hook capture's `hook-<ms>` nonce and, for permissions,
    the transcript `tool_use_id` the fallback uses once that capture expires — and the nonce FLIPS
    when the source changes. The nonce stays the injection key; identity is
    `pending.request_id`, minted by `_apply_request_identity` (`engine_context.py`) when a prompt
    first appears on a session and retired the moment the raw pending goes away. Within one open
    prompt, a nonce already recorded, a matching per-source content signature, OR the first sighting
    through the *other* source all resolve to the same id — content is not comparable across sources
    (a hook permission carries Claude's notification text, the transcript carries a tool name and
    JSON), so continuity is the evidence. A different signature from a source already seen mints a
    new id. Retirement must key off the RAW pending, never off the fence's own suppression, or the
    fence clears itself on the next scan. The client suppresses on `requestKey(p)` =
    `request_id || nonce`, so a flip no longer re-renders an answered prompt under a second identity.
    **The fence:** `act()` records `_record_answered_request` for every prompt answer it accepts —
    and for every one whose delivery is merely uncertain, because uncertainty is not permission to
    try again (invariant 66) — then refuses a later answer to the same identity with
    `code:"duplicate"` for `ANSWERED_FENCE_SECONDS`. That is what stops two devices rendering one
    prompt from both answering it. A proven pre-delivery failure records nothing and leaves the
    prompt answerable. The fence expires rather than persisting, so a prompt the provider never
    resolved comes back instead of going permanently invisible. A nonce the scan has not recorded is
    never fenced: without a record there is no identity, and refusing on a guess would strand a
    genuinely open prompt.

76. **A native action carries a durable receipt, and the same request id never types twice.**
    `/api/act` reaching Claude's terminal is not repeatable, so invariant 66 makes Fleet say
    "delivery uncertain" and stop — correct, but until now nothing durable recorded what actually
    happened, and a phone that lost its connection mid-answer could never find out. The browser
    mints a `client_request_id` BEFORE the request (the failure this exists for is losing the
    RESPONSE, so a server-generated id would arrive too late), `Engine.begin_act_receipt`
    (`engine_receipts.py`) binds one `act_receipts` row to it, and `resolve_act_receipt` writes the
    outcome. Replaying the same id returns the recorded result — `ok`/`replayed` when it was
    delivered, the recorded failure otherwise, `in_flight` while it is still running — and NEVER
    writes a second set of keys. Only the OUTERMOST `act()` binds a receipt: `act` re-enters itself
    under the per-session Claude mutation lock and `_send_now_or_queue` forwards the same
    `client_request_id` to a nested direct send, so nesting is tracked by a thread-local depth
    counter rather than a field on the action, which is client-supplied and untrusted.
    **The verdict comes from act(), not from the transport classifier**: act already labels a
    possibly-delivered failure `delivery_uncertain`/`control_delivery_uncertain`, and every other
    refusal (empty text, stale nonce, capability gate) is one it made before touching the transport
    — re-deriving the state with `_native_write_failed_before_delivery` would mislabel all of those
    as uncertain and send the user to check a terminal that received nothing.
    Receipts are internal, never projected into `/api/fleet`, and pruned after
    `ACT_RECEIPT_TTL_SECONDS` (24h, operator decision 2026-07-24). Read-only probes (`ping`, `noop`,
    `focus`) are excluded because replaying one costs nothing. A storage failure must never block an
    action: `begin_act_receipt` returns no receipt and the action proceeds exactly as before.
    Token-gated `GET /api/act-receipt?rid=` is how a reconnected browser asks. **A missing receipt
    is ambiguous, not a verdict** — the request may never have arrived, its receipt may have been
    pruned, or the ledger may have refused the write while the keys still landed — so the client
    stops tracking it and leaves whatever the user was already shown. A `delivered` receipt returns
    the optimistic row to WAITING, not confirmed: canonical transcript confirmation still owns the
    final state (invariant 34).
    This is deliberately not the async worker the design document proposed. Measured on staging
    2026-07-24, a full `noop` act costs a median 3.0 ms through tmux against 296.1 ms through the
    applet, so "return in ~10 ms and poll a receipt" buys nothing once the terminal switch lands.
    Durability was the other half of that plan and stands on its own.

77. **Before writing prompt keys, Fleet looks at the screen.** `fleetdash/screen.py` is a pure
    classifier: pane text in, one of `question` / `permission` / `trust` / `input` / `unknown` out.
    Every marker in it was read off a real `capture-pane -p` frame from Claude Code v2.1.219 in a
    disposable tmux sandbox (2026-07-24) — the same discipline invariant 4's key map was built with,
    and for the same reason. The **footer** is the discriminator, because the option list looks
    nearly identical on all three modal surfaces: `Enter to select · ↑/↓ to navigate · Esc to cancel`
    is the ask, `Esc to cancel · Tab to amend` is a permission prompt, `Enter to confirm · Esc to
    cancel` plus `I trust this folder` is the trust dialog, and a lone `❯` directly under a rule is
    the empty input box. Body markers (`Chat about this`, `Do you want to`) catch a frame whose
    footer scrolled off. Only the last `TAIL_LINES` rows are read: a question answered ten screens
    ago must not classify the pane.
    `Engine.screen_prompt_kind(reg, tty)` captures and classifies; `act()` uses it to refuse
    `option`/`multiq`/`permission`/`dismiss` whose widget is not the one on screen, with
    `code:"screen_mismatch"` and nothing written. That closes invariant 5's oldest guess: digits
    typed into an ordinary input box become a MESSAGE, and the registry word was the only thing
    standing between Fleet and that. The `trust` case is sharper still — digits there would accept
    Claude's folder-trust dialog, which Fleet must never do (invariant 21).
    **`unknown` and `None` mean NO EVIDENCE, never "no prompt".** A session outside tmux, an
    unreadable pane, or a mid-turn screen all return None and leave the previous behaviour exactly
    as it was; the gate can only ever refuse, never authorize. It runs on the REQUEST path only —
    one capture per prompt answer, never on the scan (invariant 74).

## Dev workflow

- Coverage: `scripts/coverage.sh [--show-missing]` runs the full unittest suite under
  coverage.py — 98% lines overall, floor 95%/module (2026-07-23). Keep new code at that bar.
  Coverage-focused tests live in `tests/test_cov_*.py`; shared fixtures are
  `tests/test_cov_common.py` (`EngineCovBase`) and `tests/test_cov_common_ops.py`
  (`EngineFixture`/`FakeCodex`). `# pragma: no cover` is reserved for `__main__` guards and
  provably unreachable defensive branches, each justified in its PR.
- Engine/server change: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`,
  then `curl -s http://127.0.0.1:8377/api/fleet | python3 -m json.tool | head`.
- Staging changes are restarted independently with
  `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash.staging`, then probe
  `http://127.0.0.1:8378/api/fleet`. Development edits belong in the staging checkout; production
  is promoted only from merged `origin/main` with `scripts/deploy-production.sh`. The script keeps
  the checkout detached at the exact release, installs its private production dependencies, restarts
  only production, and verifies the API identity plus Web Push readiness.
  `dashboard.html` and allowlisted `static/` assets need NO restart — served per-request; open tabs
  self-reload via `page_v` (the newest page/asset mtime in `/api/fleet`).
- Log: `~/.claude/fleet-dash-prod-state/fleet-dash.log` (production; staging logs in
  `~/.claude/fleet-dash-staging-state/`) (stdout+stderr). Failures worth logging get
  `print(..., file=sys.stderr, flush=True)` — that's the debugging channel that cracked every
  bug so far. `act` failures and 403s are already logged.
- **Synthetic pending probe** (server-side test without a real prompt): spawn a `sleep` child,
  write a fake `~/.claude/sessions/<pid>.json` (status "waiting") + fake transcript with an
  unanswered AskUserQuestion tool_use, poll /api/fleet, clean up. Pattern in the session
  scratchpad (`probe_pending.py`) — recreate as needed.
- **Harmless injection probe:** `POST /api/act {"type":"noop", "session_id":…}` — full
  daemon→selected Claude transport chain, delivers zero keystrokes. A foreground session takes
  tmux or the applet/iTerm path depending on where its tty lives (invariant 73); background jobs
  use the official attach/detach path.
- **tmux transport rig:** never drive a real session. `tmux -L fdrig new-session -d -s fdrig
  -x 120 -y 40 'claude --model haiku'`, then `tmux -L fdrig send-keys -t fdrig:0.0 …` and read
  it back with `capture-pane -p`. Targets are `session:window.pane` — a bare pane index fails
  with "can't find pane". Write captures to a file and open them with the Read tool; piping
  box-drawing characters through `grep`/`cat -v` hits illegal byte sequences.
- **Live interactive test protocol:** the building session asks a real AskUserQuestion; the
  user answers it FROM the dashboard. The recorded answer proves (or pinpoints) the loop.
- **Screen ground truth (tmux rig).** To see what a TUI actually displays: start a SEPARATE tmux
  server/session (never the operator's), run a disposable `claude --model haiku` in a scratch dir,
  drive it with `tmux send-keys -t <session>:0.0`, and read frames with `tmux capture-pane -p -t
  <session>:0.0`. Note the target form: `-t =<name>` selects a SESSION (`has-session`,
  `new-window`), but `send-keys`/`capture-pane` want a PANE (`<name>:0.0`). A scratch dir with no
  trusted ancestor also gives you the folder-trust dialog for free. Kill the session and remove the
  dir afterwards. The old osascript/iTerm technique still works where tmux is absent.
  Watch the quota: the statusline in the capture shows the weekly percentage, and each turn spends
  it.
- Applet rebuild: README recipe; ad-hoc re-sign may re-prompt the automation grant once.
- **Browser specs: the service worker is OFF by default.** `reset()` in `tests/browser/fleet.spec.js`
  stubs `navigator.serviceWorker.register` unless a spec passes `{serviceWorker:true}`. A registered
  worker intercepts fetches BEFORE Playwright's `page.route` sees them and can win registration
  during the setup idle callback, so a spec mocking an API endpoint intermittently exercised the real
  fixture instead of its mock — proven 2026-07-24: an `/api/act` mocked to fail was answered for real
  and the prompt vanished for good. Four specs genuinely test the PWA and opt back in. The stub must
  stay inside a try/catch: init scripts run in EVERY frame, and merely READING
  `navigator.serviceWorker` throws `SecurityError` in the sandboxed artifact iframe.
- **Never measure a poll-rebuilt node through a handle.** `#sact`, `#sctrl` and the card tails are
  replaced on a 2s tick, so `locator.boundingBox()` or `el.evaluate(el=>el.nextElementSibling…)` can
  resolve an element that is detached before it is read — a null with no error. Use `stableBox()`
  (retries) or express the relationship as a locator (`'#sctrl > .termbtn + .ovwrap'`).
- Headless page test: `chrome --headless=new --dump-dom http://127.0.0.1:8377/` renders with
  JS executed; grep for `class="pend"` etc.
- The building session's own Bash runs sandboxed — anything probing PIDs (`os.kill`) or writing
  under `~/.claude` needs `dangerouslyDisableSandbox`.

## File map (repo)

Python library code lives in the `fleetdash/` package; the repo root keeps the
runtime entrypoint (`server.py`), web assets, hooks, scripts, docs, and tests.
`fleetdash/search_index.py` and `fleetdash/codex_launcher.py` stay stdlib-only
because they are also spawned directly as scripts by absolute path.

- `fleetdash/paths.py` — instance path resolution (BASE/CAPTURE/PROJECTS/…), the single
  patch point for filesystem roots; all modules read them as `paths.X` attributes.
- `fleetdash/config.py` — DEFAULT_CONFIG, load_config + validators, pricing (`usd`,
  `model_family`), private-json/log-scrub helpers, shared constants
  (KEY_TOOLS, IMAGE_UPLOAD_*, DANGER_COMMANDS, BUILTIN_COMMANDS, CLAUDE_MODELS/EFFORTS).
- `fleetdash/placement.py` — Now-queue classification: `classify_placement`,
  `requests_reply`, `completed_handoff`, closed placement, handoff redaction (invariant 31).
- `fleetdash/screen.py` — pure, stdlib-only classifier for a captured Claude TUI screen
  (invariant 77). Text in, one label out; it captures nothing itself, so it can never put a
  subprocess on a hot path. Markers come from real sandbox frames, never from memory.
- `fleetdash/tail.py` — Tail (incremental jsonl fold + convo/files ring buffers +
  usage_stats counters).
- `fleetdash/engine.py` — the Engine class (imports + `__init__` + class
  constants) composed from ten topical mixins, plus the spend CLI
  (`PYTHONPATH=~/.claude/fleet-dash python3 -m fleetdash.engine spend --cwd|--session`,
  used by the global `/subagent-spend` command). GET `/api/insights?days=N` aggregates
  agent_runs + session_runs + usage_stats. The Console Insights page charts `by_day`
  (agent ledger $) stacked with `token_mix` daily totals (measured session $) — two
  different measures, labelled as such in the legend; missing data renders as absent,
  never as a fabricated zero (invariant 32). `Engine.commands(sid)` builds the slash
  catalog per session: BUILTIN_COMMANDS + `<cwd>/.claude` + `~/.claude` + every
  installed plugin's installPath (`commands/**/*.md` namespaced with `:`,
  `skills/*/SKILL.md`), description from frontmatter `description:`.
- `fleetdash/engine_*.py` — Engine mixin modules, methods grouped by concern:
  `engine_staging` (staging isolation), `engine_uploads` (phone images),
  `engine_scan` (registry scan/organization/status/control state), `engine_ledger`
  (spend ledger/closed sessions/history/handoffs), `engine_context`
  (conversation/file/context projections, hook pending, prompt request identity +
  the answered fence, screen observation, effort, commands),
  `engine_worktree` (cleanup tickets), `engine_tmux` (the tmux transport),
  `engine_receipts` (durable act receipts, idempotent replay, retention),
  `engine_transport` (tty/process resolution,
  codex terminal routes, the `_terminal_write`/`_terminal_spawn` transport dispatcher,
  background attach), `engine_notify` (notifications/Web
  Push/Outbox actions), `engine_act` (the act() injection dispatcher),
  `engine_spawn` (spawn/handoff, iTerm applet exchange, settings).
- `fleetdash/engine_tmux.py` — `TmuxOps`: bounded tmux-socket enumeration under
  `paths.TMUX_SOCKETS`, the cached tty→pane map, per-key `send-keys -l` delivery,
  spawn into the operator's session, pane focus, and the read-only `capture-pane`
  screen read (`_tmux_capture`, invariant 74). Selection and failure semantics are
  invariant 73 and invariant 66; the module header records exactly what was verified
  against tmux 3.7b, so the encoding does not have to be re-derived.
- `fleetdash/web_push.py` + `fleetdash/web_push_worker.js` — private key store, asynchronous
  durable-lease supervisor, bounded helper protocol, Web Push encryption/request
  construction, endpoint/DNS confinement, and
  retry/result mapping. No provider scan or HTTP handler performs remote delivery.
- `fleetdash/claude_background.py` — fixed-argv official Claude background attach/stop client,
  bounded private
  PTY readiness, allowlisted Engine key operations, per-job serialization, and secret-free errors.
- `fleetdash/briefing.py` — `FleetOperations` facade (imported as `from .briefing import
  FleetOperations`): list/detail/badge/unread projections, session observation, budgets,
  forecasts, and device/notification management. Composed from two mixins split out of this
  file as pure code motion:
  - `fleetdash/briefing_store.py` — `StoreOps`: the SQLite store — table DDL, migrations
    (including the M12 ntfy-legacy migration and severity normalization), connection/
    transaction machinery, generic (de)serialization/validation, row projections, and
    event/meta CRUD. Also defines the shared module constants and `OperationsError`
    (re-exported from `briefing` for `from .briefing import OperationsError`).
  - `fleetdash/briefing_scheduler.py` — `SchedulerOps`: cadence/policy scheduling — quiet
    hours, kind-policy evaluation, delivery leases, wave/repeat logic, and push-revision
    suppression.
- `fleetdash/outbox.py` — scheduled/waiting messages plus provider-neutral automatic send
  fallback and Codex
  control-recovery queues; private queue-owned images are never projected as paths.
- `fleetdash/codex_protocol.py` — the App Server protocol client layer split out of
  `codex_adapter`: the `UnixWebSocketProcess` Unix-transport shim, the synchronous JSON-RPC
  `CodexAppServer`, and the error-payload normalization helpers those clients own
  (`_error_text`/`_is_limit_error`/`_safe_json`). Depends only on `codex_runtime`
  (CodexError, codex_command); never imports `codex_adapter`.
- `fleetdash/codex_runtime.py` — the lowest Codex layer: the `CodexError` base exception,
  executable resolution (`codex_command`), shared/managed App Server startup
  (`ensure_shared_codex_runtime`/`ensure_managed_codex_runtime`, `codex_control_socket`),
  and the private-to-managed metadata migration (`CodexRuntimeMigration`,
  `migrate_codex_runtime_metadata`, `codex_runtime_migration_needed`,
  `LEGACY_RUNTIME_OWNER`/`MANAGED_RUNTIME_OWNER`). Imports nothing from `codex_protocol`
  or `codex_adapter`.
- `fleetdash/codex_adapter.py` — `CodexAdapter`: normalized Codex
  threads/turns/items/questions/approvals/artifacts/subagents, shared-runtime ownership, and
  provider capability mapping. Imports the transport from `codex_protocol` and the runtime
  lifecycle from `codex_runtime`.
- `server.py` — ThreadingHTTPServer. `Handler.reply` **gzips** any response ≥1400 bytes whose
  type is JSON/text/SVG/JS when the client accepts it (level 4: the fleet snapshot goes
  225,140 → 38,360 bytes, 17%, for ~1.3 ms — measured live on 49 sessions 2026-07-24). Metrics
  and `X-Fleet-Payload-Bytes` keep reporting the size the app PRODUCED, not the wire size, so
  payload budgets stay comparable; a compression failure falls back to the plain body rather
  than failing the response. The service worker caches and replays an encoded `/api/fleet`
  correctly — verified by the PWA/offline specs, which is why the browser fixture server
  compresses the same responses. `Handler.GET_ROUTES`/`POST_ROUTES` map each
  route to `(auth, handler)` so the token-auth surface is auditable in one place —
  new routes are added to those tables, never as new `if` branches.
  GET `/` + `/api/fleet` + `/api/context`
  + `/api/agent_context?sid=&aid=` (one subagent's convo + info; same Tail fold as a session)
  + `/api/file` + `/api/commands` (token-gated: it reads names/descriptions off disk)
  + `/api/screen?sid=` (token-gated: one live tmux pane's rendered text, invariant 74),
  + token-gated `/api/act-receipt?rid=` (durable action receipts, invariant 76),
  + token-gated `/api/search`, `/api/search/status`, `/api/search/context`, `/api/notifications`,
  `/api/notification-policy`, `/api/push/config`, and `/api/push/devices`; POST `/api/act` +
  `/api/upload-image` + `/api/settings` + `/api/notification-policy` +
  `/api/search/rebuild` + notification read/snooze/wake/mute/retry and
  device/subscription/test routes are token-gated.
  Settings persists the manual `legacy_ntfy_enabled` switch, range-validated UI/session thresholds
  (`stall_seconds` also drives the stalled STATE), `muted_sessions` (sid → ts, persists until manual unmute),
  `pinned_sessions`, `reply_available`, and `read_sessions` into config.json via
  `Engine.update_settings`. Muted sessions skip all per-session Web Push deliveries.
- `fleetdash/search_index.py` — isolated incremental Claude/Codex transcript and saved-subagent parser,
  provider-referenced artifact indexer, per-source offset/generation/error state, WAL/FTS5 query and
  exact-context reader, controlled rebuild, and worker-parent lifecycle. It never crawls arbitrary
  repository files. Unknown/malformed/oversized records stay bounded and visible in Search warnings.
- `dashboard.html` — semantic application shell and overlay roots. Desktop navigation is a per-device left/right rail;
  mobile navigation is a bottom bar with Insights/Settings under More. Destinations are URL-hash
  routed and participate in browser/native back; the flat session inventory lives in Search
  TYPE=SESSION (invariant 31), not a separate History destination.
- `static/fleet.css` — design tokens, responsive shell, shared cards, reading surfaces, and reduced-
  motion/mobile rules.
- `static/js/` — the browser app as 16 raw ES modules (no bundler), split from
  the former single `static/app.js` as contiguous verbatim slices:
  `main.js` (entry: side-effect imports in order + poll loop/init),
  `state-store.js`, `nav.js`, `search.js`, `ui-utils.js`, `outbox.js`,
  `push.js`, `notifications.js`, `context.js`, `viewer-handoff.js`,
  `overlays.js`, `workspace.js`, `cards.js`, `settings-actions.js`,
  `history-spawn.js`, `insights.js`. **Module contract (invariant 71):**
  shared mutable state is declared as `globalThis.<name>=…` in its owning
  module (never module-level `let` — an exported/copied binding goes stale on
  reassignment); every top-level function/const publishes via
  `Object.assign(globalThis,{…})` (functions in the module-top block so
  hoisting makes them callable from any later module's init; consts at module
  end), because the generated HTML's inline `onclick=` handlers and
  cross-module bare-name references resolve through the global object. Never
  name a global after an existing `window` property (the old `open` Set is
  `openCards` for exactly that reason). `dashboard.html` lists every module as
  `<link rel="modulepreload">` so the network-first shell loads in one
  parallel burst; a new module must be added to that list, `sw.js`
  `SHELL_ASSETS` (+ cache bump), `server.py` `APP_MODULES`, and the browser
  fixture server. Legacy notes below refer to this code as "app.js" — the
  subsystem descriptions still apply, now spread across the modules:
  render loop, pendingBox/sessionCard/convoBox/
  renderQueue/historySection/insightsSection, built-in Markdown/static-HTML/JSON/PDF render routing, file viewer overlay
  (`#viewer`, survives re-renders by living outside `#sessions`), act client, token-cookie
  bootstrap (`?token=`), typing-focus render guard. UI open/closed state must live in JS globals
  (`open`/`infoOpen`/`doneOpen`/`filesOpen` plus route/filter globals) re-applied at render —
  a full innerHTML re-render destroys native `<details>` state otherwise.
- `static/manifest.webmanifest` + `static/sw.js` + `static/offline.html` + `static/icons/` — stable
  install identity, root-scoped network/private-data boundary, cached last-fleet offline view, and
  regular/maskable PWA artwork. Bump the service-worker cache name when changing its shell contract.
  **`#sessions` is reconciled in place, NOT innerHTML-replaced** (`reconcileCards`): each
  `.card[data-sid]` node persists across polls, and only its volatile `.ctop` wrapper
  (`cardTop(s)` — header/meta/peek/pending/running-agents) is rebuilt each tick, so a card is
  never torn down and remounted. The old `.detail` "more" tail (`cardDetail`/`detailSig`) is
  GONE: the Console redesign moved every fold it held — mute, handoff links, state evidence,
  delivered files, completed agents — into the session workspace, leaving those two functions
  unreachable. They were also the only reason a card read the conversation cache.
  **A card fetches `/api/context` only when it has an outstanding optimistic receipt to
  confirm (invariant 34) or a pending request it can answer inline** — a pinned needs-you
  session, since an unpinned one is an Action Inbox row that opens the pane to answer. The
  peek is `s.last_msg` from the poll and never needed the cache. Measured on production: 49 of
  49 cards fetched a conversation every time it moved and none of them used it. The trade
  (operator decision D4) is that the FIRST open of a session on a device shows
  `loading conversation…` briefly; `fleet.contextCache.v1` covers every later open. Two browser
  specs measured layout before the conversation landed and now wait for it. The viewer's docked
  action bar (`renderViewerBar`, rebuilt each render tick for `viewerSid`) duplicates the card's
  act controls — its element ids are `vft-`/`vmsg-` (never `ft-`/`msg-`: the card's ids coexist
  in the DOM and getElementById would hit the wrong one). The bar owns its expandable `.vconvo`
  chat (global `viewerChatOpen`) with its own scroll preservation (the card body no longer
  hosts a conversation — it lives only in the full-screen view). Bar order: conversation toggle, then
  the collapsible question block (`viewerQOpen`), then the always-visible freetext. The
  header's 📄 strip (`#vfiles`, `viewerFilesOpen`, current file `viewerPath`) is a one-line
  horizontal file switcher rebuilt by `renderVFiles()` (preserves scrollLeft). `singleQBlock`
  is the shared single-question selector (card + viewer; descriptions, Other input backed by
  `otherDraft`, ✕ dismiss); `mqBlock` renders multi-question asks ONE question at a time
  (`mqSel[sid] = {nonce, qi, a, other}`, ‹ › nav, single-select picks auto-advance `qi`).
  User-action handlers call `uiRefresh()` (forced card render + forced viewer-bar render) —
  a plain `render(last,true)` leaves the viewer bar un-repainted under the touch guard.
  **`uiRefresh()` and `scheduleRender(force,after)` coalesce to ONE paint per frame**
  (`main.js`; `uiRefresh` is `scheduleRender(true)`). One answer tap used to call `render()`
  four or more times and every completed `/api/context` fetch called it again, so N sessions
  moving in one poll meant N full rebuilds. A frame's `force` values UNION, so an explicit
  action still overrides the touch/scroll guard even if a background fetch scheduled the frame
  first — which is why `ensureCtx` must schedule UNFORCED (`scheduleRender(false)`) or a
  background fetch repaints through the guard and kills scroll momentum. rAF is when the
  browser paints anyway, so nothing is delayed, only deduplicated. **Anything that writes into
  an element the render creates — an inline status message — must go through the `after`
  callback, not the next line**, or the deferred paint overwrites it; `settingMessage`,
  the mode/permission "changing…" notes, and `recordInputFeedback` (which must measure when
  the UI actually changed) all do.
  **Never assign HTML that has not changed.** An identical `innerHTML` write still tears the
  subtree down, re-parses it, drops focus and forces relayout. `setHtml(el, html)`
  (`ui-utils.js`) skips it and returns whether it wrote; `reconcileCards` guards each `.ctop`
  and `renderParentWorkspaceAction` guards `#sact` the same way, so an idle poll now performs
  ZERO card writes (measured: 8 mutations across four polls with two cards → 0; production
  renders 48). This is what stops untouched cards re-laying out — and moving under your
  finger — every two seconds. It supersedes the audit's `uiRefresh(sid)` scoping: the same
  outcome with no call-site changes and no stale cross-card aggregates.
  A browser spec asserts the zero and that a genuinely changed card still repaints. **`render()` passes `force` down to `renderSession(force)`**:
  a `pointerdown` keeps `touching()` true for 800ms, so without it the workspace pane deferred the
  repaint that the tap itself requested — an answered permission kept its live buttons for most of
  a second. The unforced poll render still defers during a gesture, and the question drawer keeps
  its own gesture + settle suppression (invariant 60) ahead of the force.
  The render guard covers desktop too: `wheel` feeds the same `lastMove` window as
  `touchmove` (a poll re-render mid-wheel kills scroll momentum). Scrollbar auto-hide is a
  separate `scroll`-capture listener toggling `.scrolling` — deliberately NOT fed into
  `lastMove`, because programmatic sticky-bottom restores fire scroll events and would
  starve re-renders. Scrollbar styling uses standard `scrollbar-color`/`scrollbar-width`
  ONLY — in Chrome, setting `scrollbar-color` disables `::-webkit-scrollbar` styling, so
  never mix the two.
  Question-file pairing is engine-side (`_paired_files`, window `question_file_pair_seconds`
  anchored to the hook capture ts) so chips work on collapsed cards without an /api/context
  fetch. The `/` autocomplete (`slashInput`/`slashPick`/`slashClose`, `cmdCache` per session,
  one `/api/commands` fetch per session) hangs under each freetext input; rows carry
  `onmousedown="event.preventDefault()"` so the input keeps focus — the blur would otherwise
  drop the `typing` render guard and a poll tick could destroy the button between mousedown
  and click. The subagent chat overlay (`#aview`, `openAgent`/`renderAgent`/`closeAgent`,
  `agentCache` keyed on the agent's `convo_v`) mirrors the file viewer's shape minus the file
  strip; tapping an agent row opens it (the old inline info dropdown is gone — the info block
  now lives inside the overlay). It repaints from the 2s tick with its own focus/touch guard.
  The full-screen SESSION view (`#sview`, `openSession`/`renderSession`/`closeSession`, ⤢
  button by the card's "recent conversation" header) is the same overlay with a real send box,
  the question block, file chips and interrupt/mute. THREE act surfaces now coexist in the DOM
  (card, file viewer, session overlay) — each needs its own element-id prefix (`ft-`/`msg-`,
  `vft-`/`vmsg-`, `sft-`/`smsg-`) and every act builder (`pendingBox`, `singleQBlock`,
  `mqBlock`, `sendPerm`, `sendInterrupt`) takes that prefix; a hardcoded prefix silently
  targets the wrong surface's element (getElementById hits the card's copy). The session view
  and the file viewer are MUTUALLY EXCLUSIVE — `openSession` calls `closeViewer()` and
  `viewFile` calls `closeSession()`; they swap via the chat view's file chips and the viewer
  bar's ⤢ button. Keep that invariant: two stacked full-screen overlays leave the lower one's
  inputs focusable underneath.
- `hooks/pending-capture.py` — hook entry (PreToolUse/PostToolUse AskUserQuestion, Notification).
- `injector.applescript` — applet source; request-file flags: 0=raw text, 1=text+LF, 2=raw CR.
- `scripts/deploy-production.sh` — fail-fast merged-release promotion and production-only relaunch.
- `com.benjaminfeder.fleet-dash.plist` + `com.benjaminfeder.fleet-dash.staging.plist` — isolated
  production/staging launchd copies (live copies in `~/Library/LaunchAgents`).
- Runtime state lives OUTSIDE the repo since 2026-07-23: per-instance state dirs
  (`~/.claude/fleet-dash-prod-state` production, `~/.claude/fleet-dash-staging-state` staging) hold
  `config.json` (secrets: act_token, ntfy topic), `push-secrets.json`, `ledger.db`,
  `search.db*`, `codex_threads.json`, `uploads/`, `inject-request/result.txt`,
  `fleet-dash.log`, and `FleetDashInjector.app`; the shared capture dir
  (`~/.claude/fleet-dash-capture`) holds `pending/`, `effort/`, and `usage.json`.

## Outside-repo touchpoints (document changes to these here)

`~/.claude/settings.json` (hook registrations point at this repo's `hooks/pending-capture.py`) ·
`~/Library/LaunchAgents/…plist` (live daemons; env vars select state/capture dirs) ·
`~/.claude/commands/subagent-spend.md` (slash command; runs `python3 -m fleetdash.engine spend`) ·
`~/.claude/fleet-dash-prod-state` + `~/.claude/fleet-dash-capture` (production state / shared captures) ·
`/tmp/tmux-<uid>/` (`paths.TMUX_SOCKETS`; tmux server sockets Fleet reads, and `default` where it
spawns — shared with the operator's own tmux, never created per instance) ·
TCC Automation grant (injector→iTerm2; the applet transport only).

## Roadmap / known gaps

- Permission-prompt injection: ONE real variant now verified (sandbox, v2.1.219, 2026-07-24) — a
  file-write prompt rendering `❯ 1. Yes` / `2. Yes, and allow Claude to edit its own settings for
  this session` / `3. No` under `Esc to cancel · Tab to amend`. The default
  `permission_keys` (`allow:"1"`, `always:"2"`, `deny:""`→Esc) is correct for it, and a bare digit
  instant-selects there exactly as it does on a single-select ask (invariant 4). Other variants
  (Bash commands, MCP tools) still have unobserved wording for row 2, so per-variant tuning remains
  possible; deny=Esc stays the safe choice because it cancels every variant. Invariant 77's
  classifier now makes the rendered variant observable, which is what made this testable at all.
- Screen peek is BUILT (invariant 74): the workspace Details section reads a tmux session's live
  pane on demand, which is what a stalled session's tool output looks like when the transcript
  cannot say. Still open from W5-T2a of
  [`design-responsiveness-and-transport.md`](design-responsiveness-and-transport.md): feeding that
  same capture back into state — observed ghost-question detection (replacing invariant 5's
  registry-status proxy), stall diagnosis, compaction without a PreCompact hook, trust-prompt
  confirmation, and Codex tty disambiguation. T2b (settle detection and pre-flight verification
  replacing the fixed inter-key delay) is gated on that document's drive allowlist, open question 6.
- A session started in a plain terminal tab cannot be migrated into tmux, so the applet stays
  until the last such session turns over. It is on a countdown, not maintained in parallel: the
  operator is moving off iTerm2 and every iTerm-bound path dies with that move.
- Tailscale serve + installed-PWA onboarding remain user-side.
- Fable pricing placeholder in `config.json` rates.
- Claude VS Code sessions: no tty → view-only by design. ChatGPT Desktop/Codex VS Code transcripts
  are also view-only because their App Server is separate from Fleet's canonical Codex daemon.
