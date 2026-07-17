# fleet-dash — agent development guide

Dev-facing companion to `README.md` (human setup/usage). Update BOTH in the same commit as any
feature: README for what/how-to-use, this file for invariants + dev workflow.

## Architecture (data flow)

```
~/.claude/sessions/<pid>.json      CLI live registry: sessionId, cwd, status busy/idle/waiting,
                                   name, bridgeSessionId (claude.ai deep link). PID-liveness
                                   filters stale files (they're deleted on clean exit only).
~/.claude/projects/<proj>/<sid>.jsonl            main transcript  ─┐ incremental byte-offset
~/.claude/projects/<proj>/<sid>/subagents/*.jsonl agent transcripts┘ tails (engine.Tail)
~/.claude/fleet-dash/pending/<sid>.json          hook-captured pending prompt (the ONLY source)
        ↓ engine.py (Engine.scan, poll thread, 2s)
snapshot_cache ─ server.py ─ GET /api/fleet ─ dashboard.html + static/app.js (fetch poll 2s,
                                               self-reloads via page_v)
                          ├ GET /api/context?sid= ─ Tail.convo ring (recent turns) + Tail.files
                          │   (SendUserFile deliveries); page refetches only when the session's
                          │   convo_v/files_n fields in /api/fleet move
                          ├ GET /api/file?sid=&p= (token) ─ Engine.file_content (whitelist)
                          └ POST /api/act (token) ─ Engine.act ─ inject-request.txt ─
                            open -g FleetDashInjector.app ─ iTerm write by tty ─ inject-result.txt
Claude/Codex JSONL ─ search_index.py --worker (nice 10) ─ search.db WAL/FTS5
                                      └ server.py separate reader ─ authenticated
                                        /api/search, /api/search/status, /api/search/context
ledger.db: agent_runs (finalized agent spend), session_runs (live + closed sessions)
```

The launchd parent has HTTP threads + one provider poll thread. It owns one low-priority child
indexer process; a file lock prevents overlapping writers during launch-agent restarts, and the
child exits when its parent disappears. Keep search parsing out of the parent interpreter: a Python
thread regressed live `/api/fleet` p95 by contending for the GIL on the 2.7 GB local corpus.
`Engine.scan_lock` serializes ALL Tail folding (poll loop and act's freshness re-poll) — Tails
are stateful offsets; concurrent folds double-count. `Engine.lock` guards snapshot_cache only.

## Invariants — violating these re-breaks debugged behavior

1. **Pending questions NEVER come from the transcript.** The CLI flushes AskUserQuestion
   tool_use rows only when answered (row timestamps are creation-time and lie). Hook capture
   (`hooks/pending-capture.py`, registered in `~/.claude/settings.json`) is the only source.
   The transcript-derived `mt.pending` path survives only as a permission-prompt fallback.
2. **The Notification event must never clobber a question capture.** ~6s after every question
   opens, an input-needed Notification containing "permission" fires for the SAME event.
3. **All Apple Events go through the applet.** launchd-context osascript hangs FOREVER on the
   TCC check (cannot present the dialog) — never call `osascript -e 'tell app "iTerm2" …'`
   from engine/server. The applet must keep `CFBundleIdentifier`
   (com.benjaminfeder.fleet-dash.injector) or TCC grants can't persist.
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
   Other text is control-char-stripped server-side (a smuggled \r would fire as Enter). Debug
   rig: spawn a sandbox `claude --model haiku` in a new iTerm tab, make it ask, drive it via
   scratchpad sbx2.py — never experiment on real sessions.
5. **Injection freshness:** act() re-polls the tail under scan_lock and validates the nonce
   (hook-file nonce or transcript tool_use_id) before writing keys, AND refuses prompt answers
   (option/multiq/permission/dismiss) when the registry status isn't `waiting`. The second
   check is load-bearing: a PreToolUse capture can outlive an ask that another hook BLOCKED —
   the ghost question renders, but the session sits at its main input and injected digits
   would type (and send) as a message. hook_pending also hides a question pending >5s old on a
   non-waiting session for the same reason. `interrupt` (Esc mid-turn) has the mirror gate: it
   requires status `busy`, so an Esc can never land in an idle session's input box.
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
   else idle; busy → running/stalled).
   **CANCELLED is separate, authoritative and immediate.** Older Claude builds mark it when the
   parent's Agent `tool_result` comes back `is_error: true`; `Tail.errored_tools` collects those
   ids. Newer builds can instead leave the Agent spawn result successful and emit a queued/
   attached `<task-notification>` with `<status>killed</status>` after `TaskStop` (verified
   2026-07-16 on session `9e8b990e`, agent `agent-a9f377…`). `Tail.agent_terminals` retains bounded
   `completed`/`killed`/`failed` notices and `scan_agents` maps them to `done`/`ended` immediately.
   A task id can resume, so a terminal notice applies only when its timestamp is at or after the
   child transcript's newest row; later child output supersedes it. The *presence* of the parent's
   ordinary Agent `tool_result` still proves nothing because a background agent gets one at spawn.
8. **First scan is seed-only for ntfy** (`Engine.seeded`) — never push pre-existing states at
   daemon start. Spend pushes fire only on the highest crossed multiple.
   The M12 dark migration keeps legacy ntfy claims in `notification_deliveries_legacy` and writes
   canonical lifecycle rows to `notification_events`; the two identities must not be conflated.
   Canonical identities use the full provider/session/native revision, never truncated session IDs,
   transcript mtimes, or repeat buckets. Informational events are resolved observations; per-device
   cursors, not lifecycle state, decide whether they are unread.
9. **Never inject into real sessions during dev-testing** except via the user-driven live-test
   protocol below. The auto-mode classifier blocks self-injection from the building session.
10. **`/api/file` serves ONLY whitelisted paths** — paths recorded from that session's own
    SendUserFile tool_use rows, and it's token-gated. Never accept a free-form client path:
    that would turn the act token into an arbitrary-disk-read credential over the tailnet.
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
13. **`.card` must stay `overflow:clip`, never `hidden`.** The sticky card header
    (`position:sticky` on `.shead`) sticks to the *viewport* only while no ancestor is a
    scroll container; `overflow:hidden` makes the card one and silently kills the pinning.
    `clip` keeps the border-radius clipping without creating a scroll container.
14. **Answer suppression is client-side and nonce-keyed:** a sent answer records
    `answered[sid]=nonce` and the selector hides immediately (the engine's pending clears a
    poll or two later). Never suppress by sid alone — the next ask (new nonce) must render.
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
    `spawn` → applet verb `SPAWN` (line 1 of the request file instead of a tty) → new iTerm tab
    running `cd <dir> && claude [--model M] [--effort E] [--worktree [name]]`. Model and effort
    must be members of `Engine.MODELS` / `EFFORTS`, the worktree name is regex-bounded, the dir
    must exist and resolve under `$HOME`, and the path is `shlex.quote`d. Never accept a
    free-form command string — the act token would become a remote shell.
21. **Claude Code's folder-trust is INHERITED, and fleet-dash must never write it.**
    `~/.claude.json` `projects[dir].hasTrustDialogAccepted` is keyed by dir, but a git worktree
    under a trusted repo has NO entry of its own and still starts clean (verified 2026-07-14),
    while a fresh dir with no trusted ancestor stops at "do you trust the files in this folder?"
    — a prompt only the Mac can answer, and one a spawned session hangs on. So `is_trusted()`
    walks ANCESTORS (an exact-path check falsely flags every worktree as untrusted), the picker
    labels untrusted dirs, and the spawn reply carries `trust_prompt`. Setting that flag
    ourselves would defeat a security gate from a remote device — don't.
22. **Effort exists ONLY in the statusline payload.** `"effort":{"level":…}` is piped to the
    statusline command — it is in NEITHER the transcript NOR the session registry, so the daemon
    cannot derive it. `~/.claude/statusline-command.sh` side-writes it to
    `fleet-dash/effort/<session_id>` (its `fleet-dash effort side-write` block, write-on-change);
    `Engine.effort_for` reads that. No statusline render → no effort → the UI shows the model
    alone. SUBAGENT effort comes from the agent DEFINITION's frontmatter pin
    (`.claude/agents/<type>.md` → `effort:`), falling back to the parent session's effort when
    the agent pins none — that fallback is not a guess, it is what the runtime does. Plugin
    types (`plugin:agent`) have no local file: fall back to the parent.
23. **A CLOSED session has no process:** the registry can't resolve it, so `closed_context`
    reads the ledger's validated `transcript_path` (falling back to the legacy cwd mapping only for
    old rows). Its overlay is read-only — no send box, no stop, no mute. A closed Claude session may
    expose **Reopen**: that creates a new iTerm tab with `claude --resume <exact UUID>`. Never accept
    a client-supplied path or cwd. `_safe_claude_transcript` must continue to require an exact UUID
    filename directly beneath one `~/.claude/projects` directory, and `_safe_reopen_cwd` must keep
    the working directory inside HOME.
24. **Click latency is the injection path — keep these four fixes.** Measured 2026-07-14: a
    one-keystroke `focus` cost 850ms while a ping cost 2ms. (a) The applet delays only BETWEEN
    steps, never after the last; (b) the delay is per-request (flag 4) — 0.4s ONLY for ask-TUI
    key sequences where it is load-bearing (invariant 4), 0.05s for text/focus/interrupt/relay;
    (c) `act()` takes `scan_lock` + re-polls the tail ONLY for prompt answers (the poll thread
    holds that lock while folding the whole fleet, so a click used to wait out a full scan) —
    but `mt.poll()` must stay INSIDE the lock or it races the fold and double-counts; (d) the
    result file is polled every 20ms, not 300ms. The applet is stay-open
    (`OSAAppletStayOpen`), so `open -g` reopens the resident process (`on reopen`) instead of
    launching one. Net: 850ms → ~260ms. Any change here is re-verified in the SANDBOX with a
    real multi-question ask before shipping.
25. **Applet verbs:** flag 0/1/2 = write text / text+LF / raw CR; **flag 3 = focus** (select that
    window+tab, activate iTerm — types nothing); line 1 `SPAWN` = new tab running a composed
    command. `act` type `focus` powers the Claude card's desktop-only **Terminal** button (`.deskonly`, hidden
    on `pointer:coarse` — focusing a Mac tab from a phone is meaningless).
26. **Claude plan usage mirrors Claude Usage's selected profiles, without exposing credentials.**
    `Engine.claude_usage_profiles` watches
    `~/Library/Preferences/HamedElfayome.Claude-Usage.plist` by mtime/size and projects ONLY profile
    id/name, account email, selected/active state, refresh interval, display flags, 5-hour/general-
    weekly/Fable-weekly quota percentages and resets, and last-update time. The same profile objects
    also contain session keys and credential
    JSON: never return, log, cache, or snapshot the raw objects. Multi-profile mode renders every
    selected account and its active marker. The Now command-bar chip normally says `Usage`; at 70% it
    shows the worst selected account/window percentage in amber and at 90% in red. Its popover/sheet
    contains the full gauges. If the app is absent/unreadable, fall back to the Claude
    Code statusline side-write at `~/.claude/fleet-dash/usage.json` plus the mtime-watched
    `~/.claude.json` login email. The adjacent **local lifetime-token** figure is a different,
    machine-wide scope: `Engine.claude_lifetime_tokens` reads `~/.claude/stats-cache.json`
    `modelUsage` and sums `inputTokens` + `cacheCreationInputTokens` + `cacheReadInputTokens` +
    `outputTokens` across models. Show that aggregate once, not once per profile. It represents
    retained main-session and saved-subagent transcripts on this Mac; it excludes deleted history,
    other computers, and claude.ai. Keep the word `local` in the UI.
27. **One light theme, two surfaces.** `setTheme(light)` toggles `.light` on BOTH `#vbody` (md
    viewer) and `#sbody` (full chat view) and swaps both ☀︎/☾ buttons; `toggleTheme` flips it;
    persisted as `viewer_light`. Light CSS is keyed off a bare `.light` ancestor (not `#vbody.light`)
    so it applies in either container. `#stheme` is fixed 40×32 so the glyph swap can't resize the
    chat header (`#vtheme` stays the big 28px viewer button).
28. **Pinned sessions are server-persisted and live in a GLOBAL block.**
    `pinnedSessions` mirrors `/api/fleet.settings.pinned_sessions`; `toggleSessionPin` writes
    `pin_session` + `pinned` through `/api/settings`. `renderPinned` fills `#pinned` (directly below
    Fleet Briefing) in persisted insertion order. Fleet urgency/activity changes never reorder it; a
    new pin appends at the bottom. Pinned cards are relocated, never duplicated.
    Desktop uses the header `.spin` 📌 button. Mobile hides it and long-presses the session header;
    the hold paints immediately and `sessionTap` swallows the following click so pinning does not
    also open the chat. `pinActions` suppresses duplicate writes; failure restores the exact prior
    order and renders inline retry instead of a blocking alert.
29. **Full chat view lands at the bottom on open.** `sessionOpened` (set in `openSession`/
    `openClosed`) forces `#sbody` to `scrollHeight` on the first render regardless of prior
    scrollTop (a tall cached convo starts at 0 → the sticky-bottom test would otherwise keep the
    top). `#sact` renders AFTER and shrinks `#sbody`, so re-pin in a `requestAnimationFrame` once
    layout settles. `wantBottom` also keeps the poll re-render stuck to the bottom when already there.
30. **One canonical Codex runtime; ownership is never inferred from transcript access.** Engine starts
    a detached `codex app-server --listen unix://…` process, then the adapter connects through the
    documented WebSocket-over-Unix protocol at
    `~/.claude/fleet-dash/codex-app-server.sock`. Do not use Codex's default control-socket path;
    that belongs to its standalone daemon manager. Do not replace this
    with `app-server daemon start` on the npm install: that manager requires the separate standalone
    Codex installer. Fleet-created threads and CLI threads
    actually loaded on that socket are persisted with `thread_meta.runtime_owner=fleet_shared` and
    may be steered by Fleet or an attached `codex resume --remote unix://...` TUI. A newly created
    Fleet thread immediately starts a visible normal `hi` turn; `thread/start` alone has no rollout
    and cannot be resumed by the TUI. `thread/resume` aborts an active turn, so expose disabled
    **turn active** instead of attach until the turn finishes. Before materialization, expose disabled
    **starting**, never **attach**. Preserve that empty shell only while `thread/loaded/list` still
    contains it; otherwise it has no runtime or rollout and must be discarded as a ghost. Keep the full-chat
    open/attach/view-only button directly left of its overflow menu. `source=vscode` is not ownership
    evidence: App Server uses it for Fleet's rich-client threads too. Only a thread persisted with
    `runtime_owner=fleet_shared` is controllable; an unowned Desktop/VS Code transcript stays
    headless + view-only. Never restore the old takeover action: resuming one of those ids on Fleet's
    server creates a second runtime agent.
    `CodexRolloutObserver` is the narrow exception to the adapter's no-rollout-parsing rule: only
    explicitly pinned external threads consume an allowlist of local lifecycle and visible-message
    events so `notLoaded` does not hide active Desktop/VS Code work. The observer is read-only,
    incremental, path-confined, row-bounded, and tolerant of malformed/unknown additions. Its result
    may update state, preview, and context, but must never update ownership or enable submit,
    interrupt, archive, close, attach, compact, review, or relay capabilities.
31. **Now placement is an action queue, not a provider-state dump.** `Engine.organize_session`
    is the source of truth for `ui_group`, `reason_label`, `primary_action`, `access`,
    `reply_requested`, and `new_response`. Fleet Briefing precedes the session queue; session order is
    Pinned → Needs you → Working → Available.
    Pinned/Needs/Working hide when empty; Available stays visible. History is a separate destination
    with one chronological list and access/provider filters. `requests_reply` examines the newest
    complete assistant prose outside code/quotes. Its revision remains Needs you until a user reply
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
    is not confirmation. Structured answers render their actual labels (never secret free text) and
    may drop the spinner once the provider accepts the native answer response, but remain until the
    canonical QA event arrives. HTTP failure or 15 seconds without direct-text confirmation produces
    a red restore button; restore refills the composer and never retries. A focused composer must not
    block `#sbody` transcript repaints—preserve the composer below the body update instead. The main
    fleet card mirrors the newest question-answer placeholder as a compact delivery receipt. Inline
    permission/dismiss/elicitation actions create the same receipt even when the card's More panel
    is closed; failures remain visible beside the still-actionable request.
35. **Session card peeks are capped at exactly 500 characters including the ellipsis.** The server
    caps both providers; CSS controls the collapsed line count. When measured content overflows, the
    final collapsed row is a clickable `...`; expanded state removes the height clamp but does not
    fetch or imply more than the bounded 500-character payload.
36. **Now counts and subagent filtering are presentation-only.** The standalone totals line is gone;
    Needs you/Working/Available counts live in their matching command-bar chips. Needs you counts
    distinct sessions. `Subagents` flattens every child not in `done`/`ended`, including `stalled`,
    with its parent breadcrumb; selecting it hides the session queues without changing
    `Engine.organize_session` or turning subagents into session cards.
37. **Nested Settings preserves chat state.** When Settings opens over full chat it gets a separate
    history entry and remains above `#sevidence`. Back closes Settings alone, restores the exact
    `#sbody.scrollTop`, and preserves whether `Why Fleet put this here` was open. A pending sticky-
    bottom animation must not overwrite that saved position.
38. **Desktop rail side is per browser; file viewer identity is file-only.** `fleet.navSide.v1`
    toggles `html[data-nav-side]` between left/right and never affects mobile bottom navigation.
    `#vtitle` contains only the escaped file name/caption; session metadata and `.vfsep` do not belong
    in the Markdown viewer. Ordinary Claude cards open chat through `.shead` and keep only the
    distinct **Terminal** native-focus button; Respond/Review actions remain explicit.
39. **New-session identity is optimistic but exact.** `spawnProvisional` immediately owns one
    client-generated card/full-chat identity and the initial user message while `/api/act spawn` is
    pending. Only the exact server-returned `session_id` may replace it; never reconcile by cwd.
    Claude's initial text is sent after that exact session becomes discoverable, while Codex accepts
    it atomically at thread creation. Explicit spawn rejection may offer retry; a lost response may
    have created a session and must not retry automatically. Forecast requests are abortable and
    sequence-gated so an older model result cannot overwrite the latest selection. Message and relay
    composers are `<textarea>` controls: Return is always a newline; only Command-Return on macOS or
    Control-Return elsewhere sends.
40. **Claude permission mode is a native, state-gated control.** `Tail.permission_mode` accepts only
    Claude's allowlisted transcript values. Live changes are allowed only for an idle registered
    Claude session whose process exposes the target in `permission_modes`; `act(permission_mode)`
    composes fixed Shift+Tab steps and retains the 0.4s native-TUI inter-key delay. `dontAsk` is a
    new-session-only Advanced choice because it is not in Claude's live cycle. `bypassPermissions`
    is exposed only when the already-running process was launched with Claude's enabling flag and
    the client must show a separate high-warning confirmation every time. Fleet never accepts
    Claude's folder-trust or bypass warning on the user's behalf.
41. **Secondary-worktree cleanup is preview-ticketed and happens after provider close.**
    `close_worktree_preview` resolves the canonical workstream identity, refuses primary/unregistered/
    locked/prunable worktrees, and returns bounded porcelain-v2 dirty plus ignored-file evidence.
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
    child payloads freeze with the child. Full-chat headers contain the title and controls only.
43. **Routine Claude conversation reads never wait for the fleet-wide Tail fold.** `_scan` publishes
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
    `/sw.js` has root scope but caches only the explicit versioned public shell list. Navigations are
    network-first with the content-free tailnet reconnect page as fallback; `/api/*`, transcripts,
    notifications, settings, token-bearing URLs, and conversation content are always network-only.
45. **The session-peek line setting also owns ordinary collapsed-card height.** A `.fixedpeek`
    card uses the measured fixed frame `117px + preview_session_lines × 17.4px` (or zero preview
    rows when session peeks are disabled), with its More control anchored at the bottom. Never put
    `.fixedpeek` on an open card, an explicitly expanded peek, or a card showing a pending request,
    error, reply request, inline delivery/pin feedback, or running subagents: those cards must grow
    to keep every action visible. Keep the height inputs synchronized with the header/meta/peek/
    More CSS measurements if their typography or padding changes.

## Dev workflow

- Engine/server change: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`,
  then `curl -s http://127.0.0.1:8377/api/fleet | python3 -m json.tool | head`.
  `dashboard.html` and allowlisted `static/` assets need NO restart — served per-request; open tabs
  self-reload via `page_v` (the newest page/asset mtime in `/api/fleet`).
- Log: `~/.claude/fleet-dash/fleet-dash.log` (stdout+stderr). Failures worth logging get
  `print(..., file=sys.stderr, flush=True)` — that's the debugging channel that cracked every
  bug so far. `act` failures and 403s are already logged.
- **Synthetic pending probe** (server-side test without a real prompt): spawn a `sleep` child,
  write a fake `~/.claude/sessions/<pid>.json` (status "waiting") + fake transcript with an
  unanswered AskUserQuestion tool_use, poll /api/fleet, clean up. Pattern in the session
  scratchpad (`probe_pending.py`) — recreate as needed.
- **Harmless injection probe:** `POST /api/act {"type":"noop", "session_id":…}` — full
  daemon→applet→iTerm chain, delivers zero keystrokes.
- **Live interactive test protocol:** the building session asks a real AskUserQuestion; the
  user answers it FROM the dashboard. The recorded answer proves (or pinpoints) the loop.
- **Screen ground truth:** to see what a TUI actually displays (keybinding hints, prompt
  layout), arm a background until-loop watcher on the pending file, then `contents of session`
  via osascript from an iTerm-child shell (TCC auto-allowed there, unlike the daemon).
- Applet rebuild: README recipe; ad-hoc re-sign may re-prompt the automation grant once.
- Headless page test: `chrome --headless=new --dump-dom http://127.0.0.1:8377/` renders with
  JS executed; grep for `class="pend"` etc.
- The building session's own Bash runs sandboxed — anything probing PIDs (`os.kill`) or writing
  under `~/.claude` needs `dangerouslyDisableSandbox`.

## File map (repo)

- `engine.py` — Tail (incremental jsonl fold + convo/files ring buffers + usage_stats
  counters), Engine (scan/state/ledger/ntfy/act/hook_pending/session_context/file_content/
  insights/commands/compacting_secs), spend CLI (`spend --cwd|--session`, used by the global
  `/subagent-spend` command). GET `/api/insights?days=N` aggregates agent_runs + session_runs
  + usage_stats. `Engine.commands(sid)` builds the slash catalog per session: BUILTIN_COMMANDS
  + `<cwd>/.claude` + `~/.claude` + every installed plugin's installPath (`commands/**/*.md`
  namespaced with `:`, `skills/*/SKILL.md`), description from frontmatter `description:`.
- `codex_adapter.py` — detached Unix-listener/WebSocket JSON-RPC client, shared-runtime ownership, normalized
  Codex threads/turns/items/questions/approvals/artifacts/subagents, and provider capability mapping.
- `server.py` — ThreadingHTTPServer; GET `/` + `/api/fleet` + `/api/context`
  + `/api/agent_context?sid=&aid=` (one subagent's convo + info; same Tail fold as a session)
  + `/api/file` + `/api/commands` (token-gated: it reads names/descriptions off disk),
  + token-gated `/api/search`, `/api/search/status`, `/api/search/context`, `/api/notifications`,
  `/api/push/config`, and `/api/push/devices`; POST `/api/act` + `/api/settings` +
  `/api/search/rebuild` + notification read/device/subscription/test routes are token-gated.
  Settings persists the
  `notify` toggles, the `NUM_KEYS` thresholds (range-validated; `stall_seconds` also drives
  the stalled STATE, not just the push), `muted_sessions` (sid → ts, persists until manual unmute),
  `pinned_sessions`, `reply_available`, and `read_sessions` into config.json via
  `Engine.update_settings`. Muted sessions skip all per-session pushes.
  Fleet-quiet fires once per quiet episode, `fleet_quiet_minutes` after the busy→idle
  transition (`Engine.quiet_since`), not on a time-bucket dedupe).
- `search_index.py` — isolated incremental Claude/Codex transcript and saved-subagent parser,
  provider-referenced artifact indexer, per-source offset/generation/error state, WAL/FTS5 query and
  exact-context reader, controlled rebuild, and worker-parent lifecycle. It never crawls arbitrary
  repository files. Unknown/malformed/oversized records stay bounded and visible in Search warnings.
- `dashboard.html` — semantic application shell and overlay roots. Desktop navigation is a per-device left/right rail;
  mobile navigation is a bottom bar with Insights/Settings under More. Destinations are URL-hash
  routed, participate in browser/native back, and keep History/Insights out of Now.
- `static/fleet.css` — design tokens, responsive shell, shared cards, reading surfaces, and reduced-
  motion/mobile rules.
- `static/app.js` — render loop, pendingBox/sessionCard/convoBox/
  renderQueue/historySection/insightsSection, built-in markdown renderer (`md()` — no CDN), file viewer overlay
  (`#viewer`, survives re-renders by living outside `#sessions`), act client, token-cookie
  bootstrap (`?token=`), typing-focus render guard. UI open/closed state must live in JS globals
  (`open`/`infoOpen`/`doneOpen`/`filesOpen` plus route/filter globals) re-applied at render —
  a full innerHTML re-render destroys native `<details>` state otherwise.
- `static/manifest.webmanifest` + `static/sw.js` + `static/offline.html` + `static/icons/` — stable
  install identity, root-scoped network/private-data boundary, shell-only offline guidance, and
  regular/maskable PWA artwork. Bump the service-worker cache name when changing its shell contract.
  **`#sessions` is reconciled in place, NOT innerHTML-replaced** (`reconcileCards`): each
  `.card[data-sid]` node persists across polls. A card is split into `cardTop(s)` (volatile —
  header/meta/peek/pending/running-agents/more-btn, in a `.ctop` wrapper rebuilt every poll,
  no `<details>` so replacing it can't flash) and `cardDetail(s)` (the `.detail` "more" tail
  with the native `<details>` folds). The tail is rebuilt ONLY when `detailSig(s)` changes —
  a signature that EXCLUDES per-second time fields (started/delivered ages) so a ticking clock
  never remounts it. That is what stops an expanded card's open dropdown from blinking every
  2s. Trade-off: completed-agent/spend text in an open panel can be up to a few seconds stale
  until a material field changes. The viewer's docked
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
- `com.benjaminfeder.fleet-dash.plist` — launchd copy (live one in ~/Library/LaunchAgents).
- Untracked runtime: `config.json` (secrets: act_token, ntfy topic), `ledger.db`, `search.db*`, `pending/`,
  `inject-request/result.txt`, `fleet-dash.log`, `FleetDashInjector.app`.

## Outside-repo touchpoints (document changes to these here)

`~/.claude/settings.json` (hook registrations) · `~/Library/LaunchAgents/…plist` (live daemon) ·
`~/.claude/commands/subagent-spend.md` (slash command) · TCC Automation grant (injector→iTerm2).

## Roadmap / known gaps

- Permission-prompt injection untested against a real dialog (`permission_keys` may need tuning
  per variant; deny=Esc chosen because it cancels every variant).
- Screen-peek button (stalled-session "show me the terminal") — technique proven, UI not built.
- Tailscale serve + phone onboarding (user-side), ntfy topic subscribe.
- Fable pricing placeholder in `config.json` rates.
- Claude VS Code sessions: no tty → view-only by design. ChatGPT Desktop/Codex VS Code transcripts
  are also view-only because their App Server is separate from Fleet's canonical Codex daemon.
