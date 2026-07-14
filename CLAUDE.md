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
snapshot_cache ─ server.py ─ GET /api/fleet ─ dashboard.html (fetch poll 2s, self-reloads via page_v)
                          ├ GET /api/context?sid= ─ Tail.convo ring (recent turns) + Tail.files
                          │   (SendUserFile deliveries); page refetches only when the session's
                          │   convo_v/files_n fields in /api/fleet move
                          ├ GET /api/file?sid=&p= (token) ─ Engine.file_content (whitelist)
                          └ POST /api/act (token) ─ Engine.act ─ inject-request.txt ─
                            open -g FleetDashInjector.app ─ iTerm write by tty ─ inject-result.txt
ledger.db: agent_runs (finalized agent spend), session_runs (live + closed sessions)
```

One process (launchd `com.benjaminfeder.fleet-dash`): HTTP threads + one poll thread.
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
   `status` is authoritative (waiting → needs_you; idle → turn_done if fresh end_turn else
   idle; busy → running/stalled).
   Two non-signals, both checked and rejected 2026-07-14: the parent's `tool_result` for the
   Agent tool_use fires at SPAWN for a background agent ("Async agent launched successfully"),
   so its presence proves nothing; and the `task-notification` rows that do carry a real
   `<status>completed</status>` aren't written while the parent is mid-turn, so they lag.
8. **First scan is seed-only for ntfy** (`Engine.seeded`) — never push pre-existing states at
   daemon start. Spend pushes fire only on the highest crossed multiple.
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
    the ask TUI: the relay would answer the question). `agent_id` is client-supplied → it is
    hard-whitelisted (`agent-[A-Za-z0-9_-]{1,64}`, basename only) before any path is built, or
    `/api/agent_context` becomes an arbitrary-file read.

## Dev workflow

- Engine/server change: `launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash`,
  then `curl -s http://127.0.0.1:8377/api/fleet | python3 -m json.tool | head`.
  `dashboard.html` needs NO restart — served per-request; open tabs self-reload via `page_v`
  (the file's mtime in /api/fleet).
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
- `server.py` — ThreadingHTTPServer; GET `/` + `/api/fleet` + `/api/context`
  + `/api/agent_context?sid=&aid=` (one subagent's convo + info; same Tail fold as a session)
  + `/api/file` + `/api/commands` (token-gated: it reads names/descriptions off disk),
  POST `/api/act` + `/api/settings` (both token-gated; settings persists the
  `notify` toggles, the `NUM_KEYS` thresholds (range-validated; `stall_seconds` also drives
  the stalled STATE, not just the push), and `muted_sessions` (sid → ts, pruned at 30d;
  muted sessions skip all per-session pushes) into config.json via `Engine.update_settings`.
  Fleet-quiet fires once per quiet episode, `fleet_quiet_minutes` after the busy→idle
  transition (`Engine.quiet_since`), not on a time-bucket dedupe).
- `dashboard.html` — self-contained page: render loop, pendingBox/sessionCard/convoBox/
  closedSection/rollupTable, built-in markdown renderer (`md()` — no CDN), file viewer overlay
  (`#viewer`, survives re-renders by living outside `#sessions`), act client, token-cookie
  bootstrap (`?token=`), typing-focus render guard, convo scroll preservation across re-renders
  (sticky-bottom unless the user scrolled up). UI open/closed state must live in JS globals
  (`open`/`infoOpen`/`doneOpen`/`filesOpen`/`closedOpen`/`rollupOpen`) re-applied at render —
  the 2s innerHTML re-render destroys native `<details>` state otherwise. The viewer's docked
  action bar (`renderViewerBar`, rebuilt each render tick for `viewerSid`) duplicates the card's
  act controls — its element ids are `vft-`/`vmsg-` (never `ft-`/`msg-`: the card's ids coexist
  in the DOM and getElementById would hit the wrong one). The bar owns its expandable `.vconvo`
  chat (global `viewerChatOpen`) with its own scroll preservation; the card scroll pass is
  scoped to `#sessions .convo` so the two never fight. Bar order: conversation toggle, then
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
  targets the wrong surface's element (getElementById hits the card's copy).
- `hooks/pending-capture.py` — hook entry (PreToolUse/PostToolUse AskUserQuestion, Notification).
- `injector.applescript` — applet source; request-file flags: 0=raw text, 1=text+LF, 2=raw CR.
- `com.benjaminfeder.fleet-dash.plist` — launchd copy (live one in ~/Library/LaunchAgents).
- Untracked runtime: `config.json` (secrets: act_token, ntfy topic), `ledger.db`, `pending/`,
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
- Ledger backfill from surviving transcripts (pre-2026-07-13 history).
- VS Code sessions: no tty → view-only by design.
