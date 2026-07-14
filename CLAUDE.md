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
   (option/multiq/permission) when the registry status isn't `waiting`. The second check is
   load-bearing: a PreToolUse capture can outlive an ask that another hook BLOCKED — the ghost
   question renders, but the session sits at its main input and injected digits would type
   (and send) as a message. hook_pending also hides a question pending >5s old on a
   non-waiting session for the same reason.
6. **`http.server` self.path includes the query string.** Route on `path.split("?",1)[0]`.
7. **Agent state semantics:** long tool calls freeze transcripts — "stalled" (>240s) agents are
   still counted active; agents whose PARENT went idle/waiting with no end_turn are `ended`
   (canceled) and finalize to the ledger. Sessions: registry `status` is authoritative
   (waiting → needs_you; idle → turn_done if fresh end_turn else idle; busy → running/stalled).
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
    not in the client.
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

- `engine.py` — Tail (incremental jsonl fold + convo/files ring buffers), Engine (scan/state/
  ledger/ntfy/act/hook_pending/session_context/file_content), spend CLI (`spend --cwd|--session`,
  used by the global `/subagent-spend` command).
- `server.py` — ThreadingHTTPServer; GET `/` + `/api/fleet` + `/api/context` + `/api/file`
  (token-gated), POST `/api/act` + `/api/settings` (both token-gated; settings persists the
  `notify` per-category push toggles into config.json via `Engine.update_settings`).
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
  Question-file pairing is engine-side (`_paired_files`, window `question_file_pair_seconds`
  anchored to the hook capture ts) so chips work on collapsed cards without an /api/context
  fetch.
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
