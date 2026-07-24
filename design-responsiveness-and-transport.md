# Design: Fleet responsiveness and the Claude control transport

Status: **W5-T1, the first half of W5-T2a, W3 part 1, W4 and W2 shipped 2026-07-24; W1, W3 part 2
and W5-T2b/T3 remain proposed.** Written 2026-07-24 after a
measurement session against production (port 8377, 48 live sessions) and a sandboxed Claude Code
v2.1.219 rig.

W5-T1 (the tmux transport and the `_terminal_write`/`_terminal_spawn` dispatcher) was pulled ahead
of W1 under §5's forcing function: the operator confirmed the terminal switch is imminent, so
terminal independence became break-fix rather than insurance. Remaining order is unchanged:
**W1 → W5-T2a → W3 → W4 → W2 → W5-T2b/T3.** Open question 4 is answered — receipts stay internal
and are pruned after 24 hours (operator decision 2026-07-24). Open question 3 is answered as
configuration: `terminal_app` in `config.json` names whatever application `focus` raises, defaulting
to `iTerm` so nothing changed on the day it shipped.

Companions: [`latency-audit.md`](latency-audit.md) (the four findings and the original 12 options),
[`claude-transport-redesign.md`](claude-transport-redesign.md) (why Codex's transport cannot be
copied). This document supersedes both where they disagree.

Every number below was measured in that session. Nothing here is estimated from documentation.

---

## 1. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | `/api/act` becomes asynchronous with a durable server receipt | **Adopted** |
| D2 | One server-owned request identity + a server-side answered fence | **Adopted** |
| D3 | Client render work is coalesced and scoped | **Adopted** |
| D4 | Collapsed cards stop fetching `/api/context` | **Adopted** |
| D5 | tmux becomes the **primary** Claude control transport; the applet is legacy | **Adopted** |
| D6 | Key delivery becomes closed-loop: settle detection + pre-flight verification | **Adopted** |
| D7 | A PreToolUse hook must **never** block to await a dashboard answer | **Rejected** |
| D8 | iTerm2's Python API is not used | **Rejected** |
| D9 | Fleet-owned Agent SDK sessions | **Parked** |
| D10 | Codex keeps App Server as its control path; tmux touches only its fallback leg | **Adopted** |
| D11 | **No iTerm-specific dependency in the target state** — the operator may switch terminal emulators | **Adopted** |

### D11 — terminal independence is a hard requirement

The operator may move off iTerm2. Every current control path is iTerm-specific:

- `injector.applescript` is entirely `tell application "iTerm2"` — send, focus, and spawn all die
  with a terminal switch.
- Spawn composes a new iTerm tab (`create tab with default profile`, invariant 20).
- `focus` / "Open in Terminal" selects an iTerm window and tab (invariant 25, applet flag 3).

So the applet is not merely legacy to be tolerated indefinitely: it is a **single point of failure
with an externally-scheduled expiry date**. tmux is emulator-agnostic by construction — that is what
it is for — so it is the only transport that survives the switch, and it must be treated as the
primary path rather than an experiment.

Two consequences elsewhere in this document. Coexistence (W5-T1) is **insurance, not enhancement**,
and is pulled forward in the sequencing. And iTerm2's `-CC` control-mode integration is no longer a
candidate adoption path, since it is equally iTerm-bound.

### D7 — no blocking hook (rejected)

A `PreToolUse[AskUserQuestion]` hook can block for up to 600 s and return
`permissionDecision: "deny"` with a reason the model reads as the outcome, which would let Fleet
answer questions structurally with zero keystrokes.

**Rejected because the terminal must never freeze.** While the hook waits, the TUI renders nothing —
someone at the Mac cannot answer locally during that window. The operator chose this explicitly.
D6 delivers most of the same benefit (fast, verified answers) without taking the terminal away.

### D8 — no iTerm2 Python API (rejected)

`request cookie` does exist in iTerm2 3.6.11's scripting dictionary, and the applet could broker it
(it already holds a persistent TCC grant), so the authentication objection was answerable. Rejected
on three other grounds:

- **Portability.** It binds Fleet permanently to macOS and to iTerm specifically. tmux does not care
  what renders it, works over SSH, and would let the transport outlive a terminal-emulator change.
- **Dependency posture.** `fleetdash/` and `server.py` import **only the standard library** today —
  zero third-party Python imports, no `requirements.txt`. The project's sole third-party dependency
  is the Node web-push helper (`scripts/deploy-production.sh:33`). iTerm2's API is protobuf over a
  websocket, version-coupled to the app, with no compile-time check. Adopting a versioned binary
  protocol to *fix* a fragility problem works against itself.
- **It keeps the applet alive** as a cookie broker, so the most fragile component — code-signed
  bundle, TCC grant, build script, re-sign ritual — survives. tmux deletes it.

Latency did not decide this: see §2.4, where measurement showed the transport is not the bottleneck.

### D10 — Codex scope

Codex already has structured control and needs no keystrokes:

| action | call |
|---|---|
| send to an idle thread | `turn/start` (`fleetdash/codex_protocol.py:699-706`) |
| send into a running turn | `turn/steer` (`:717-725`) |
| stop | `turn/interrupt` (`:759`) |
| answer an approval/question | `respond(rid, result)` (`:404`) — the provider sends an inbound JSON-RPC request and Fleet answers that exact id |

Routing Codex through tmux would be a **downgrade**. Codex gets exactly two things from this work:
the narrow terminal-fallback leg (`_write_codex_terminal`, `fleetdash/engine_notify.py:668`) inherits
the tmux transport and closed-loop pacing, and spawn/focus become uniform across providers.

Everything in §3 (client rendering) and D4 is provider-neutral and benefits Codex directly — in fact
disproportionately, since Codex `/api/context` reads measured 5.6–112.6 ms against Claude's
2.1–4.2 ms.

---

## 2. Measured baseline

### 2.1 Server

```
/api/fleet                245,109 B   served from cache in 5–33 ms
  of which closed_ids      40,692 B   re-sent every 2 s
sessions                         48   all 48 carry last_msg
scan_p50                    541.1 ms  scan_lock held for the whole of _scan()
scan_p95                    911.3 ms
  phases: codex 222.4 · operations 57.9 · claude 28.6 · closed_history 26.6 · live_ledger 13.0
/api/context   Claude       2.1–4.2 ms    (8.6–40.5 KB per session)
/api/context   Codex       5.6–112.6 ms
```

### 2.2 Client

`uiRefresh()` is `render(last, true)` — a forced full-page rebuild (`static/js/settings-actions.js:17`).
A single option-answer tap calls it at least four times: `static/js/context.js:287`/`:289`,
`static/js/context.js:409`, `static/js/settings-actions.js:631`, `static/js/context.js:206`.

`cardTop()` calls `ensureCtx()` from inside the HTML string builder
(`static/js/cards.js:198`), and `ensureCtx` calls `render(last)` on completion
(`static/js/context.js:39` and `:41`) — render → N fetches → N renders.

`touching()` suppresses card renders for 800 ms after any pointerdown and 1500 ms after a move
(`static/js/ui-utils.js:8`), and gates only the card queue (`static/js/main.js:84`) while
`renderSession()` runs unconditionally (`:114`) — so the card and the pane can disagree for up to
800 ms.

### 2.3 Prompt identity

Two namespaces for one prompt:

- hook capture → `hook-<epoch_ms>` (`hooks/pending-capture.py:23`)
- transcript fallback → the `tool_use_id` (`fleetdash/engine_scan.py:810-819`)

`hook_pending` drops a capture when the registry is not `waiting` and it is >5 s old
(`fleetdash/engine_context.py:139-149`); `PostToolUse` deletes it on resolution. In that gap the
fallback re-surfaces the same prompt under a different nonce, and the client's nonce-keyed
suppression (`static/js/cards.js:151`) stops matching — the prompt reappears.

Observed in production (`~/.claude/fleet-dash-prod-state/fleet-dash.log`):

```
pending first seen: 7d34942b permission nonce=toolu_014H9PBXDowWSMsB4Y
pending first seen: 01df102c permission nonce=toolu_01MDcEXRFbwKVdqk5P
```

`sendPerm` (`static/js/settings-actions.js:863`) and `sendDismiss` (`:697`) never set `answered`
optimistically — only `act()` does, after the round trip (`:627-628`) — so a permission prompt stays
visible and tappable for the whole injection.

### 2.4 Transport

```
tmux  send-keys                 p50   7.3 ms
tmux  capture-pane -p           p50   7.7 ms   (bounded to the pane)
tmux  capture-pane -p -e        p50   6.6 ms

osascript process launch        p50  33.3 ms
Apple Event to iTerm2           p50  89.4 ms   → ~56 ms event-only (derived)
  scan every session's tty      p50  86.5 ms   (6 sessions; ONE bulk event —
                                                the applet's nested loops cost more)
  contents of current session   p50  86.4 ms   returns 11,581 B (whole buffer)
do shell script, first          p50    ~34 ms  (the applet base64-decodes per text step)
do shell script, subsequent     p50     ~6.5 ms
```

**Claude's TUI settle time — the actual bottleneck**, measured against a real 4-option
multi-select in the rig:

```
digit 1 (toggle)      first_change 16.6 ms   stable 138.5 ms
DOWN                  first_change 14.1 ms   stable 122.9 ms
DOWN                  first_change 46.5 ms   stable 156.8 ms
trust prompt Enter    first_change 11.1 ms   stable 177.8 ms
                                     median stable ≈ 138 ms
```

So a 22-step multiq:

| | per key | 22 steps |
|---|---|---|
| today, blind at 0.4 s | 400 ms | 8.8 s |
| tmux closed loop | ~140 ms | **~3.1 s** |
| AppleScript closed loop | ~200 ms | ~4.4 s |

**Two conclusions that changed the design.** The 0.4 s delay was never unreasonable — it is a ~3×
margin over real settle. And the transport is not the latency bottleneck; the TUI's render is. The
value of D6 is therefore **verification, not speed**, and D5 rests on portability and on deleting the
applet, not on milliseconds.

*Harness caveat:* the rig captured its baseline after sending, so changes landing inside the 15 ms
poll were invisible and 4 of 7 keys logged "no settle" despite working correctly. Baseline-before-send
is a one-line fix; the true median is likely slightly under 138 ms.

### 2.5 What the ask TUI actually renders

Claude Code v2.1.219, 4 options, multi-select:

```
←  ☐ Color  ☐ Food  ✔ Submit  →
Which colors do you prefer?
❯ 1. [ ] Red
  2. [ ] Green
  3. [ ] Blue
  4. [ ] Yellow
  5. [ ] Type something
     Next
  6. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
```

**Everything Fleet needs is plain text.** Focus is `❯`; checkbox state is `[ ]` / `[✔]`; the tab bar
carries per-question state and flipped `☐ Color` → `☒ Color` on answer. No SGR parsing and no `-e`
required — focus tracking costs the same as change detection.

**Invariant 4 is confirmed, not superseded.** Digits toggle without moving focus. `Next` was reached
after exactly 5 DOWN presses with 4 options (`n_options + 1`). "Type something" sits at `n+1`, "Chat
about this" at `n+2`. The existing recipes are correct and must be retained.

---

## 3. Workstreams

Ordered by dependency. W1–W4 are independent of W5 and deliver the responsiveness fix on their own.

### W1 — Async act with a durable receipt (D1)

`/api/act` validates, enqueues to a per-session worker, and returns `{ok, receipt_id, state}` in
~10 ms. The worker takes the locks, drives the transport, and updates the receipt. Receipt state
rides `/api/fleet`; the client spinner is driven by the receipt, not an open fetch.

Also: give each `Tail` its own lock so the act path's freshness re-poll waits on one session (~ms)
rather than the fleet-wide `scan_lock` (541–911 ms). `_scan` must then acquire per-session locks in a
fixed order.

**SHIPPED 2026-07-24, and simpler than this — per-session locks were not needed.** Measuring the
scan phases on production first showed the Tail fold is 30.3 ms of a 723.6 ms scan: the lock was
held ~24× longer than anything it protects, because it wrapped the whole call. Narrowing it to the
fold (`_scan_claude_sessions` locked, `_scan_after_fold` unlocked, one short re-acquire for the
status-strip Tail read) took the act re-poll's worst case from the p95 1141 ms to 32.3 ms — 4.4%
of the scan, measured live on 49 sessions. Per-session locks would take that 32 ms to ~1 ms for
considerably more concurrency surface, so they stay unbuilt until something shows 32 ms matters.
`Engine.scan_serialize` preserves the one-scan-at-a-time guarantee the wide lock gave for free, and
`scan_lock_held_ms` is published in `/api/diagnostics` so the number stays visible.

This *improves* invariant 66 rather than weakening it: today a dropped connection mid-fetch yields
"delivery uncertain" with nothing durable behind it. A receipt survives reload, backgrounding,
network loss and daemon restart.

Touches: `server.py` route table, `fleetdash/engine_act.py`, a new receipt store, ~20 client `act()`
call sites that read `d.ok` synchronously today.

### W2 — Server-owned request identity (D2)

**SHIPPED 2026-07-24.** All three parts landed, plus a client fix the audit turned up that had
nothing to do with identity: `render()` called `renderSession()` with no `force`, so a `pointerdown`
kept `touching()` true for 800 ms and the workspace pane deferred the repaint that the very tap had
requested. Answering a permission therefore left its buttons live for most of a second — the same
window this workstream exists to close. `renderSession(force)` now passes it through; the poll's
unforced render still defers during a touch gesture.

Two corrections to the plan as written:

- Part 1 assumed the question fallback and the hook capture both needed mapping. After part 3 there
  is no question fallback, so QUESTIONS have exactly one source. The two-nonce problem is now
  permission-only — hook capture, then transcript `tool_use_id` once the capture expires at 15 s.
  Identity still covers both kinds, because a question's identity is what the answered fence keys on.
- Content signatures cannot be compared ACROSS sources: a hook permission carries Claude's
  notification text while the transcript carries a tool name and JSON input. Continuity is the
  evidence instead — the first sighting through a source not yet seen for the currently open prompt
  is the same prompt. Within one source, a changed signature mints a new id.

The retirement rule is the subtle part: identity retires on the RAW pending going away, evaluated
before the fence's own suppression. Keying it off the projected pending would have made the fence
clear itself on the very next scan.

Full contract: invariant 75.

Three parts, all server-side:

1. **Stable `request_id`.** Derive one identity that both the hook capture and the transcript
   fallback map to. Permission prompts carry weak content, so mix in a stable first-seen marker from
   `pending_seen`. The client suppresses on `request_id`; the raw nonce stays internal to key
   injection.
2. **Answered fence.** When `act()` accepts an answer, record it and drop any matching pending until
   `PostToolUse` or a `tool_result` confirms resolution, with a conservative timeout and a
   content-difference escape so a genuinely new question always shows. Mirrors the existing
   `_claude_delivery_uncertain` fence. This also closes a hazard unrelated to latency: two devices
   can both answer the same prompt today.
3. **Delete the transcript question-fallback** (`fleetdash/engine_scan.py:811-814`). Invariant 1
   already says questions never come from the transcript; that branch contradicts it. Keep the
   permission branch — permission captures have no clear hook and genuinely need it — and widen
   `hook_pending`'s 5 s ghost guard so a capture survives the registry's `waiting` flicker.

Client companion: `sendPerm` and `sendDismiss` must set `answered` **before** the await, the way
`sendOption` already does via `beginOptimisticAnswer`.

Once W5's pre-flight verification lands, screen state becomes a third, independent ground truth for
identity — Fleet can confirm the on-screen question is the one being answered rather than inferring
it.

### W3 — Client render work (D3)

**Part 1 SHIPPED 2026-07-24.** `scheduleRender(force,after)` in `main.js`; `uiRefresh()` is
`scheduleRender(true)`. The audit found eight call sites doing DOM work right after the refresh;
four genuinely needed the paint first (`settingMessage` ×2, the mode and permission "changing…"
notes) and now pass an `after` callback, two were already rAF-wrapped and stay correct by queue
order, and two were `recordInputFeedback` — moved into `after` so the latency budget still measures
when the UI actually changed rather than when the paint was scheduled. `ensureCtx` schedules
UNFORCED, so a background fetch completing mid-scroll no longer repaints through the touch guard.
A browser spec asserts one paint per answer tap (was four or more), the `force` union, and the
`after` ordering. Part 2 (scoping to `uiRefresh(sid)`) is still open.

1. **Coalesce.** `uiRefresh()` sets a dirty flag and schedules one `requestAnimationFrame` render
   with a `force` union. Audit call sites that read the DOM synchronously afterwards
   (`restoreOptimistic`'s focus rAF, `renderComposer` follow-ups).
2. **Scope.** `uiRefresh(sid)` repaints the affected card and the open pane only; every call site
   already knows its session. Cross-card aggregates (Now counts, nav badge, Outbox summary) need a
   cheap global pass.

Keyed diffing (option 2C) stays **deferred** — it is a rendering-architecture rewrite and is
incompatible with invariant 71's inline-`onclick` contract.

### W4 — Stop fetching context for collapsed cards (D4)

**SHIPPED 2026-07-24, and no payload change was needed.** The audit found that `cardDetail` and
`detailSig` — the only card code that read the conversation cache for files and agent counts — had
been UNREACHABLE since the Console redesign moved those folds into the session workspace. So the
plan's "add `files_n` plus the newest few file chips to the session payload" bought nothing: the
consumer no longer exists. Both dead functions are deleted.

What a card genuinely needs the conversation for is confirming an outstanding optimistic receipt
(invariant 34), so that is the gate: an outstanding receipt, or a pending request the card can
answer inline. That second clause is narrower than it looks — an unpinned needs-you session renders
as an Action Inbox row and opens the pane to answer, so only a PINNED one answers from a card.
Measured on production: 49 of 49 sessions fetched `/api/context` whenever their conversation moved,
and none of them used it; after the change, none fetch.

Accepted cost: the first open of a session on a device now shows `loading conversation…` briefly
instead of being pre-warmed. `fleet.contextCache.v1` covers every later open. Two browser specs were
measuring layout before the conversation landed and now wait for it — which was the change surfacing
a real ordering assumption, not flakiness.

A collapsed card needs `last_msg` (already present, capped at 800 chars by invariant 35) and a file
count. Add `files_n` plus the newest few file chips to the session payload, delete the `ensureCtx`
call at `static/js/cards.js:198`, and fetch `/api/context` only for the open pane. Move any remaining
fetches out of the render path into `tick()`, batch-applied, one render per poll.

Also drop `closed_ids` from the poll payload (40 KB of 245 KB).

**Superseded 2026-07-24 by response compression, which was not in this plan.** `Handler.reply`
now gzips JSON/text responses: the fleet snapshot goes 225,140 → 38,360 bytes on the wire (17%),
measured live on 49 sessions, saving ~186 KB per poll — 5.3 MB/minute at the two-second cadence.
That is an order of magnitude more than removing `closed_ids`, it needs no client change, and it
helps every response rather than one field. `closed_ids` costs roughly 7 KB compressed now, so
removing it — which would risk the "a live session vanished, is it closed?" path in
`renderSession` — is no longer worth doing. The rest of W4 (collapsed cards not fetching
`/api/context`) is unaffected and still open.

Viewport-gated fetching (option 3C) stays in reserve.

### W5 — tmux transport and closed-loop delivery (D5, D6, D10)

**T1 — SHIPPED 2026-07-24** (`fleetdash/engine_tmux.py` + the dispatcher in
`fleetdash/engine_transport.py`, AGENTS.md invariant 73). Four deltas from the plan below:

- **Spawn joins the operator's tmux session; Fleet never starts a tmux server.** This corrects a
  claim below. "Fleet creates a detached pane and the operator attaches" was tried first and
  failed live: a tmux server inherits the environment of whoever starts it, so the server this
  launchd daemon created handed the pane `PATH=/usr/bin:/bin:/usr/sbin:/sbin` and Claude never
  launched (`zsh:1: command not found: claude`). The environment cannot be reconstructed
  server-side — this operator's passwd shell is zsh while their sessions run bash, and `claude`'s
  directory is added by a bash startup file, so `$SHELL`, the passwd entry and a `-ilc` login shell
  are all wrong. `new-window` into a session the operator started inherits it exactly; with no such
  session, spawn falls back to the applet.
- The fixed inter-key delay is **retained on tmux too** — settle detection is T2b, so invariant 4's
  amendment does not apply yet.
- `terminal_not_available` was added to `_native_write_failed_before_delivery` as the
  transport-neutral proven-failure code rather than overloading `injector_not_launched`.
- Pane discovery enumerates every socket under `paths.TMUX_SOCKETS`, not just `default`, so a
  `tmux -L <name>` server is found too.

T2a was **not** included — it is read-only `capture-pane` projection with its own consumers and
ships next.

**T1 — coexistence.** `_tmux_panes()` (cached listing, ~2 s, mirroring
`_codex_terminal_routes_cache`), `_tmux_target_for_tty()`, `_tmux_write()` mirroring `_iterm_write`'s
exact return contract including its `delivery_uncertain` codes, a real transport dispatcher promoted
from the `native_write` closure at `fleetdash/engine_act.py:564`, and a tmux spawn path.

Detection is per-session and cheap: `tmux list-panes -a -F '#{pane_id} #{pane_tty} #{pane_pid}'`,
matched against the tty Fleet already resolves in `_tty_for_pid`
(`fleetdash/engine_transport.py:66`). A plain iTerm tab behaves exactly as today.

Keep `_iterm_write` under its current name — roughly 70 test sites patch it directly and stay valid.

**The complete iTerm-bound surface — verified 2026-07-24, eight call sites, not two.** T1 is not done
until every one is routed through the dispatcher, or a terminal switch leaves parts of Fleet dead:

| site | what it does | provider |
|---|---|---|
| `engine_act.py:568` | main key delivery (`native_write`) | Claude |
| `engine_notify.py:504` | **Outbox** text delivery — a *second, duplicated* dispatch | Claude |
| `engine_transport.py:313` | interrupt (Esc) inside the close path | Claude |
| `engine_spawn.py:276` | `SPAWN` — new session | Claude |
| `engine_transport.py:363` | `SPAWN` — Reopen, `claude --resume` (invariant 23) | Claude |
| `engine_transport.py:271` | `SPAWN` — background job attach, `claude attach <job>` (invariant 25) | Claude |
| `engine_notify.py:668` | `_write_codex_terminal` — fallback text | Codex |
| `engine_spawn.py:186` | `focus_codex_terminal` — `__FOCUS__` (invariant 30) | Codex |

Two things this inventory corrects. There are **three** `SPAWN` sites, not one — Reopen and
background-attach also create iTerm tabs and break identically on a terminal switch. And the
transport dispatch is **duplicated**: `engine_act.py:564` and `engine_notify.py:499-504` each pick
between background and iTerm independently, so a dispatcher added to only one leaves Outbox
deliveries on the applet while direct sends use tmux.

Confirmed unaffected: `spawn_codex_session` creates threads through App Server with no terminal at
all (`engine_spawn.py:194`), so D10 holds. Claude VS Code sessions have no tty and stay view-only —
tmux cannot help there.

Per D11, T1 must also replace the non-delivery verbs:

- **Spawn** (invariant 20, applet verb `SPAWN`). tmux `new-session -d` / `new-window` composes the
  same allowlisted `cd … && claude …` command with no emulator involved. This is strictly better:
  Fleet creates a detached pane and the operator attaches from whatever terminal they are using, so
  spawn stops depending on a GUI app being frontmost at all.
- **Focus** (invariant 25, applet flag 3, the ⋮ "Open in Terminal" item). Splits into two parts:
  `tmux select-window` / `select-pane` targets the pane, which is emulator-agnostic; raising the
  terminal application itself needs a generic `open -a <app>` rather than an Apple Event to iTerm.
  The app name becomes a setting. `focus_codex_terminal` needs the same treatment.

After T1 the applet retains no responsibility that tmux cannot serve, so it can be removed on the
terminal switch rather than maintained in parallel forever.

**T2a — read-only observation. Screen peek SHIPPED 2026-07-24** (`_tmux_capture`,
`Engine.session_screen`, token-gated `GET /api/screen`, the workspace Details block, AGENTS.md
invariant 74). The remaining five bullets — feeding the same capture back into Fleet's own state —
are still open, and they are the ones that remove guesses rather than add a view.

**T2a — read-only observation. Unblocked, and cheaper than anything else here.**

Screen *reading* is separable from screen *driving*, and only driving touches invariants 21/40 or
open question 6. Everything below is `capture-pane` plus a projection — no keys are ever sent, so it
can ship with T1:

- **Screen-peek ships essentially free.** AGENTS.md line 1280 lists it as "technique proven, UI not
  built"; it becomes ~10 server lines and a `<pre>`. It was blocked on the daemon being unable to
  read a screen at all, which tmux fixes.
- **Ghost-question detection stops being a heuristic.** Invariant 5 currently guesses via registry
  status plus a 5 s timeout because a PreToolUse capture can outlive a hook-blocked ask. With a
  capture Fleet just looks: is the ask widget rendered? This removes a guess from the exact path
  that produces finding 4's reappearing prompt.
- **Stall diagnosis.** Invariant 7's "stalled" means frozen mid-tool, but Fleet cannot say *on what*
  beyond the transcript. The pane shows the live tool output.
- **Compaction without a hook.** Invariant 17 reads the PreCompact checkpoint mtime and gives
  projects with no PreCompact hook no pill at all. The compaction is visible on screen regardless.
- **Trust-prompt confirmation.** `is_trusted()` is a filesystem prediction
  (`engine_spawn.py:286`); a capture confirms the session is actually sitting on the dialog. This is
  read-only, so it sidesteps §4.1's policy question entirely — Fleet reports accurately without
  answering.
- **Codex tty disambiguation.** `_codex_terminal_routes` scans `ps` argv and gives up when two
  distinct ttys match one thread UUID (`engine_transport.py:107-182`). tmux's pane→pid mapping is
  exact, which can resolve that case.

**T2b — closed-loop driving.** Blocked on open question 6, not on T2a. Replace the fixed 0.4 s with:

- **Settle detection.** Send a key, poll every ~15 ms, proceed once the capture is byte-identical for
  two consecutive polls. Two-stable-polls rather than one-change, because partial redraws produce
  torn frames. **Capture the baseline before sending**, per §2.4's caveat.
- **Pre-flight verification.** Before the first key, assert the rendered widget matches the question
  being answered, using anchors derived from the hook capture's own JSON — never hardcoded copy. On
  mismatch, refuse and report. This fails closed and subsumes the registry-status proxy invariant 5
  uses today.
- **Post-condition assertion.** Before submitting, assert the intended checkboxes are `[✔]` and focus
  is on the expected row.

**Retain invariant 4's key recipes.** The rig confirmed them; closed-loop executes them faster and
verifies them, it does not replace them. Focus-driven navigation (pressing DOWN *until* `Next` is
focused rather than counting) is available cheaply thanks to §2.5, but is **deferred** — it couples
Fleet to Claude's visual rendering, an unversioned cosmetic contract, for little gain once each step
costs ~140 ms.

The `n`-key DOWN-walk should still be replaced by the right-arrow ✔ Submit tab path where invariant 4
already proves it valid — now worth ~700 ms per question.

**T3 — honesty surface.** Closed-loop pacing only works where the screen is readable, so capabilities
diverge by transport. Needs a capability flag and UI labelling, following the existing Codex
`access` / `read_only_reason` pattern.

**Non-Fleet benefit worth stating: sessions stop dying with the terminal.** Today quitting or
crashing iTerm kills every Claude session in it. A tmux server outlives the emulator, a crash, and a
reattach. That is independent of everything else in this document and may be worth more day to day
than the latency work.

**De-scoped by D11.** `latency-audit.md` proposed micro-optimising the applet — an early `exit repeat`
in the tty scan (`injector.applescript:72-108`) and batching the per-payload `base64` shell-outs
(`:118`). Both are now wasted effort on a component with a countdown. Dropped.

**Accepted costs.** Sessions already running in a plain terminal tab cannot be migrated to tmux, so
the applet stays until the last such session turns over — but per D11 it is on a countdown, not
maintained in parallel forever, and any session that must survive the terminal switch has to be
started in tmux. tmux is a real workflow change; there is no tmux server running on this machine
today. Line wrapping (a 100-column capture split `Submit` into `Sub`/`mit` during the rig) means
anchors must unwrap before matching.

---

## 4. Invariant amendments

| Invariant | Change |
|---|---|
| 1 | Note the transcript fallback is permission-only after W2.3 |
| 3 | Scope to the applet transport; tmux sessions involve no Apple Events. Target state per D11 has none at all |
| 20 | Spawn composes a tmux session/window, not an iTerm tab; the allowlist rules are unchanged |
| 21 | "a prompt only the Mac can answer" becomes factually false under W5-T2b — restate as policy (see §4.1) |
| 40 | "Fleet never accepts Claude's folder-trust or bypass warning on the user's behalf" becomes a promise rather than a self-enforcing limit (see §4.1) |
| 4 | **Retain the recipes** — confirmed 2026-07-24 on v2.1.219. Add: fixed inter-key delay applies to the applet transport only; tmux uses settle detection with pre-flight verification |
| 5 | Screen verification supersedes the registry-status proxy on the tmux transport |
| 14 | Suppression keys on `request_id`, not the raw nonce |
| 24 | Latency clauses (a)–(d) scope to the applet; add per-session tail locks |
| 25 | Add tmux to the transport list and the dispatcher's selection rules; `focus` becomes pane selection plus a generic app raise, not an iTerm window/tab select |
| 34, 61, 66 | Restate around durable receipts rather than synchronous HTTP results |
| 35, 45 | Card peeks no longer depend on `ctxCache` |
| 43 | Extend lock-free published snapshots to the act path |
| new | Transport selection is per-session, server-derived, and never client-supplied |
| new | Closed-loop delivery must fail closed: no keys are sent to an unverified surface |

### 4.1 Screen access converts two capability limits into policy

Fleet has never read a terminal screen. Verified 2026-07-24: zero screen reads in `fleetdash/`,
`server.py`, `injector.applescript` or `hooks/`; the applet's entire verb surface is write (flags
0/1/2), focus (3) and delay (4); the only `osascript` reference in the daemon is the comment at
`fleetdash/engine_spawn.py:311` explaining why it cannot be used. AGENTS.md's two mentions of screen
reading are a manual dev technique (line 1068) and an unbuilt roadmap item (line 1280).

Everything Fleet believes about a terminal is therefore inferred from three side channels — the
registry status word, the hook capture, and the transcript fold. Most of the hedging in invariants 5,
7 and 66 exists because those are proxies for a question Fleet cannot ask directly.

W5-T2a gives the daemon that capability for the first time. **Most of the consequences are safety
improvements**: invariant 4's blind key recipes become verifiable, invariant 5's registry-status
proxy is replaced by observation, and invariant 66's uncertainty window shrinks because Fleet can see
whether keys landed.

Two consequences run the other way, and they are the reason this section exists. Invariants 21 and 40
are currently enforced partly by **incapability** rather than policy:

- Invariant 21 states the folder-trust dialog is "a prompt only the Mac can answer." The rig rendered
  and measured that exact dialog (`❯ 1. Yes, I trust this folder`, settle 178 ms). After W5-T2b the
  statement is false.
- Invariant 40 states Fleet never accepts folder-trust or bypass warnings on the user's behalf.
  After W5-T2b that is a promise with nothing structural behind it.

**The split matters here.** T2a only reads; it changes no capability boundary and is not gated on
the allowlist. It is T2b — sending keys — that turns these two invariants from limits into promises.

Once `capture-pane` is in the control path, driving these is not a feature anyone builds — it is the
same code as answering any numbered list. **The constraint must therefore be written as an explicit
allowlist**, matching how this codebase already bounds spawn (invariant 20), the model catalog
(invariant 65) and prompt shape (invariant 4): closed-loop delivery may drive only the surfaces it is
explicitly permitted to drive, and every other rendered surface is refused by default.

**Not yet decided:** the contents of that allowlist. Only two Claude TUI surfaces have been observed
directly (the ask widget and the trust dialog); `/clear`, `/model`, the bypass warning and OAuth
prompts have not. Enumerating them now would be guesswork. This is open question 6.

---

## 5. Sequencing

**W1 → W5-T1 (+T2a) → W3 → W4 → W2 → W5-T2b/T3.**

T2a rides with T1: it is read-only `capture-pane` projection, shares T1's plumbing, touches no
policy, and closes a standing roadmap item plus three inference-based guesses. Splitting the
read-only half forward is the cheapest win identified in this document.

W5-T1 is split out and pulled forward because of D11. It is mechanical (~200 lines plus the spawn and
focus rework) and it is what keeps Fleet working through a terminal switch. Leaving it until last
would mean the switch date, which is not under this project's control, could arrive with the only
control path being an applet that talks to an application no longer in use.

W1 still goes first. It is the single largest felt improvement, and it makes T1 safe to introduce:
once the browser no longer waits on delivery, a transport taking 200 ms versus 8 s is invisible, so
tmux can be enabled on one session and compared against the applet. The receipt is also the natural
place to record per-attempt transport and timing, which turns "is tmux better" into a measurement
rather than an argument.

W5-T2b (closed-loop driving) stays last. It is the only genuinely research-shaped work here, §2.4
shows it buys verification rather than speed, and it is gated on open question 6 — valuable, but not
urgent, and not worth blocking terminal independence on.

Doing tmux delivery first *without* W1 would invite treating it as the latency fix: §2.4 shows that
leaves ~3.1 s of blocking HTTP per multiq with findings 2 and 3 untouched.

No rework is created by this order: W1, W3 and W4 never touch `_iterm_write`'s contract, and T1 adds
a sibling implementation behind the existing seam.

Rough sizing: W1 about a week including the client migration; W5-T1 about a week and a half, revised
upward for the eight-site inventory and the duplicated dispatch; T2a two to three days on top of T1;
W2 and W3 a few days each; W4 a few days; W5-T2b/T3 one to two weeks of sandbox-verified iteration.

**Forcing function:** if the terminal switch is imminent, W5-T1 moves ahead of W1. Everything else in
this plan is a comfort improvement; T1 is the difference between Fleet controlling Claude and not.

---

## 6. Open questions

1. **Codex scope (D10)** — confirm that Codex gets only the terminal-fallback leg and uniform spawn,
   and keeps App Server for everything else.
2. **Terminal switch timing (D11)** — how imminent? This decides whether W5-T1 goes before or after
   W1. It also decides how long the applet has to survive alongside tmux.
3. **Which terminal** — only affects the generic "raise the app" step in the focus rework, which
   becomes a configured application name. Delivery and spawn are unaffected either way.
4. **Receipt retention** — how long a resolved receipt is kept, and whether it is exposed in Outbox
   or stays internal.
5. **Anchor derivation** — exactly which fields of the hook capture's question JSON are used as
   pre-flight anchors, and how they are normalised across line wrapping.
6. **The drive allowlist (§4.1)** — which rendered surfaces closed-loop delivery may drive. Requires
   observing the surfaces first: `/clear`, `/model`, the bypass-permissions warning and OAuth
   prompts have not been captured, and the trust dialog needs a deliberate yes/no rather than a
   default. Blocks W5-T2b only — not T1, and not the read-only T2a.
7. **Known roadmap gap now closable** — "permission-prompt injection untested against a real dialog
   (`permission_keys` may need tuning per variant)" was untestable because Fleet could not see which
   variant rendered. T2a makes the variant observable and T2b makes the keys verifiable. Worth
   scheduling deliberately rather than leaving as a standing gap.

*(iTerm2's `-CC` control-mode integration was an open question in an earlier draft. D11 removes it:
it is as iTerm-bound as the applet.)*
