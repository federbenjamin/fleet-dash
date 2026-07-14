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
5. **Injection freshness:** act() re-polls the tail under scan_lock and validates the nonce
   (hook-file nonce or transcript tool_use_id) before writing keys. Keep this — it's the only
   guard against answering a prompt that changed.
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
    logical reply (tool calls between them don't split it). Extend the filter list there, not
    in the client.

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
  (token-gated), POST `/api/act` (token-gated).
- `dashboard.html` — self-contained page: render loop, pendingBox/sessionCard/convoBox/
  closedSection/rollupTable, built-in markdown renderer (`md()` — no CDN), file viewer overlay
  (`#viewer`, survives re-renders by living outside `#sessions`), act client, token-cookie
  bootstrap (`?token=`), typing-focus render guard, convo scroll preservation across re-renders
  (sticky-bottom unless the user scrolled up).
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
- Multi-part (2+ question) asks: render-only today. Injection would need per-tab navigation.
- Screen-peek button (stalled-session "show me the terminal") — technique proven, UI not built.
- Tailscale serve + phone onboarding (user-side), ntfy topic subscribe.
- Fable pricing placeholder in `config.json` rates.
- Ledger backfill from surviving transcripts (pre-2026-07-13 history).
- VS Code sessions: no tty → view-only by design.
