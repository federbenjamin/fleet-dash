# fleet-dash tap latency & UI-instability audit

Investigated 2026-07-24 against production (`~/.claude/fleet-dash-prod-state`, port 8377, 48 live
sessions). Every claim below cites a `file:line` that was opened, or a measurement taken against the
running daemon.

Symptoms reported: taps take seconds; taps make the UI jump/refresh; things appear then disappear.
Worst case — answering a question removes the prompt, then the prompt reappears, then disappears
again when the answer is accepted.

---

## Findings

### 1. Every tap is a synchronous round-trip that blocks on AppleScript keystroke timing

`POST /api/act` does not return until the keys have physically been typed into iTerm. The whole
chain runs inside the one HTTP request:

- `_claude_mutation_lock(sid)` → `scan_lock` (`fleetdash/engine_act.py:210`)
- `scan_lock` is held by the poll thread for the **entire** `_scan()`, not just the Claude tail fold
  (`fleetdash/engine_scan.py:642-646`). Measured on production: `scan_p50 = 541 ms`,
  `scan_p95 = 911 ms`, of which the Codex phase alone is 222 ms. A tap landing mid-scan waits
  ~0.5–0.9 s before anything happens.
- `_iterm_write` then writes the mailbox file, runs `open -g`, and polls for the result file with a
  30 s deadline (`fleetdash/engine_spawn.py:310-376`).
- The applet waits **0.4 s between every key** for prompt answers
  (`fleetdash/engine_act.py:548`).

Key counts are built at `fleetdash/engine_act.py:405-440`:

| prompt shape | steps | applet time |
|---|---|---|
| single-select, 4 options | 1 | ~0 s (delay is *between* steps) |
| multi-select, 4 options | `digits + DOWN×5 + CR` ≈ 7 | ~2.8 s |
| 3-question multiq, 4 options each | ~22 | ~8.8 s |

The client awaits that fetch (`static/js/settings-actions.js:604-607`), so the button sits on
`sending…` for the full duration.

Invariant 24 documents this path as solved at "~260 ms". That figure is only true for a
*single-keystroke* focus. It was never true for the ask-TUI paths, which are the ones actually
tapped.

### 2. Every user action triggers four or more full-app re-renders

`uiRefresh()` is `render(last,true)` — a forced rebuild of the whole page
(`static/js/settings-actions.js:17`). One option-answer tap calls it at least four times:

- `addOptimistic` (`static/js/context.js:287` / `:289`)
- `updateOptimistic` (`static/js/context.js:409`)
- the `answered` branch in `act` (`static/js/settings-actions.js:631`)
- `withNativeRequestLock`'s `finally` (`static/js/context.js:206`)

Each `render()` rebuilds `#pinned`, `#actioninbox`, `#needsyou`, `#working`, `#sessions`,
`#newsess`, `#rollup`, `#providerstate` and the usage rail, then runs `renderSession()`, then
`schedulePeekOverflow()` forces two full layout passes across every card
(`static/js/history-spawn.js:595-625`). With 48 live sessions, that is the jumping.

### 3. Rendering a card issues network requests, and each response triggers another full render

`cardTop()` calls `ensureCtx(...)` **from inside the HTML string builder**
(`static/js/cards.js:198`). All 48 production sessions have `last_msg`, so all 48 are candidates on
every render. On completion `ensureCtx` calls `render(last)` again (`static/js/context.js:39`
and `:41`).

Loop: render → N context fetches → N completions → N more full renders → repeat.

Measured per-session `/api/context` payloads: 8–40 KB. Codex context reads take 75–113 ms
server-side. On a phone the browser's 6-connections-per-origin cap means the 2 s `/api/fleet` poll
queues *behind* this burst — which is why the whole UI goes stale for seconds, not just the tapped
element.

Separately: `/api/fleet` is **245 KB, every 2 seconds, forever**. `closed_ids` alone is 40 KB of
session IDs re-sent on every poll.

### 4. The reappearing prompt: the same question has two different nonces on the server

Client suppression is nonce-keyed — `answered[sid] === pending.nonce`
(`static/js/cards.js:151`, `static/js/workspace.js:572`; invariant 14). But the server produces the
nonce from **two independent namespaces for the same prompt**:

- hook capture → `hook-<epoch_ms>` (`hooks/pending-capture.py:23`)
- transcript fallback → the `tool_use_id`, e.g. `toolu_01…` (`fleetdash/engine_scan.py:810-819`)

`hook_pending` drops a capture whenever the registry is not `waiting` and the capture is >5 s old
(`fleetdash/engine_context.py:139-149`), and `PostToolUse` deletes the file the moment the tool
resolves. In that gap, `fleetdash/engine_scan.py:811-814` re-surfaces the *same* prompt under the
transcript's `tool_use_id`. `answered[sid]` still holds the hook nonce → **the selector reappears**.
When the `tool_result` folds, pending clears → it disappears again. That is exactly the reported
sequence.

Confirmed in production, `~/.claude/fleet-dash-prod-state/fleet-dash.log`:

```
pending first seen: 7d34942b permission nonce=toolu_014H9PBXDowWSMsB4Y
pending first seen: 7d34942b permission nonce=toolu_014mejbhHWKifNNejb
pending first seen: 01df102c permission nonce=toolu_01MDcEXRFbwKVdqk5P
```

Two aggravating factors:

- **Permission and dismiss taps have no optimistic suppression at all.** `sendPerm`
  (`static/js/settings-actions.js:863`) and `sendDismiss` (`:697`) never set `answered`; that only
  happens *after* the round trip returns ok (`static/js/settings-actions.js:627-628`). So a
  permission prompt stays fully visible and tappable for the entire multi-second injection.
- **The render guard desynchronizes surfaces.** `touching()` is true for 800 ms after *any*
  pointerdown (`static/js/ui-utils.js:8`) and gates only the card queue
  (`static/js/main.js:84`), while `renderSession()` runs unconditionally
  (`static/js/main.js:114`). The pane and the card can disagree about whether a question exists for
  up to 800 ms, and a poll landing in that window is dropped entirely, leaving cards up to 2.8 s
  stale.

### Common root cause

Fleet treats "the native terminal write has completed" and "the UI has been updated" as the same
event. Every tap is modelled as a synchronous command whose HTTP response is both the
acknowledgement *and* the render trigger, and the only reconciliation mechanism is a full-page
rebuild driven by a 245 KB poll.

---

## Options

### Problem 1 — synchronous act round-trip

#### 1A. Async act with a durable server receipt
`/api/act` validates, enqueues to a per-session worker, returns `{ok, receipt_id, state:'delivering'}`
in ~10 ms. The worker takes `scan_lock`, writes the applet mailbox, updates the receipt. State rides
`/api/fleet`; the client's spinner is driven by the receipt, not the fetch promise.

- **Pros** — Kills the entire *perceived* latency regardless of how slow the keystrokes are. Strictly
  improves invariant 66: today a dropped connection mid-fetch yields "delivery uncertain" with
  nothing durable behind it; a receipt survives reload, backgrounding and daemon restart.
- **Cons** — Largest change. ~20 client `act()` callers read `d.ok` synchronously today. Needs a
  receipt store and reconciliation; touches invariants 34, 61, 66.

#### 1B. Per-session tail locks instead of the global `scan_lock`
`/api/context` already reads lock-free published snapshots (invariant 43); `act` still takes the
fleet-wide `scan_lock` to re-poll one Tail (`fleetdash/engine_act.py:210`). Give each Tail its own
lock so act waits ~ms on that session, not 541–911 ms on the whole fleet.

- **Pros** — Removes 0.5–1 s from every tap with no client change. Satisfies invariant 24(c)'s actual
  requirement ("never poll a Tail unlocked"); the global lock was always stronger than needed.
- **Cons** — Does not touch the 0.4 s/key applet cost, which dominates multi-question asks. `_scan`
  must acquire per-session locks in a fixed order to stay deadlock-free.

#### 1C. Cut the key count and re-measure the inter-key delay
For no-Other multi-selects use the right-arrow ✔ Submit **tab** path (1 key) instead of the DOWN-walk
(n+1 keys) — invariant 4 already proves both. Then sandbox-measure the true minimum inter-key delay
on current Claude builds; 0.4 s was set in July 2026.

- **Pros** — The only option that reduces *actual* delivery time. A 3-question multiq could go from
  ~22 steps @ 0.4 s (8.8 s) to ~8 steps @ 0.15 s (1.2 s).
- **Cons** — Purely empirical, and invariant 4 demands full sandbox re-verification; this exact area
  corrupted six live rounds before. Still leaves ~1 s of synchronous wait.

**Recommendation — 1A.** It is the only option that decouples UI responsiveness from TUI physics,
and the receipt makes the uncertainty semantics better rather than worse. Ship **1B** first as a
same-day win; it is independent and strictly additive. Treat 1C as a later measurement task, not a
fix.

### Problem 2 — four full re-renders per tap

#### 2A. Coalesce `uiRefresh()` into one rAF
Set a dirty flag (with a `force` union) and schedule one `requestAnimationFrame` render; multiple
calls in a turn collapse to one.

- **Pros** — Roughly five lines. Removes 3 of 4 renders per tap immediately, with no change to what a
  render does.
- **Cons** — Does not reduce the cost of the surviving render (still 48 cards + two forced reflows).
  Any call site reading the DOM synchronously after `uiRefresh()` breaks — `restoreOptimistic`'s
  focus rAF and `renderComposer` follow-ups need auditing.

#### 2B. Scope the refresh: `uiRefresh(sid)`
Repaint only the affected card plus the open pane. Every call site already knows its session id;
fall back to a full render only when none is supplied.

- **Pros** — Turns the cost from O(48 sessions) into O(1). Directly kills the jumping: untouched
  cards never re-layout, so nothing moves under your finger.
- **Cons** — ~20 call sites to thread a sid through. Cross-card effects (Now filter counts, nav
  badge, Outbox summary) still need a cheap global pass or they go stale.

#### 2C. Keyed diffing instead of string rebuilds
`reconcileCards` already preserves node identity; extend that so `cardTop` emits a patch rather than
`top.innerHTML=`.

- **Pros** — Permanently ends the whole class: focus, scroll, text selection and `<details>` state
  survive natively; `restoreCardTopFocus`/`detailSig` become unnecessary.
- **Cons** — By far the largest rewrite here, and invariant 71's contract (inline `onclick=`
  attribute strings resolving through `globalThis`) is fundamentally incompatible with most diffing
  approaches. Event handling would be rewritten at the same time.

**Recommendation — 2A now, 2B next.** 2A is close to free and removes three quarters of the work
today. 2B is where the jumping actually stops. 2C is the correct end state but is a
rendering-architecture rewrite; do not start there, and do not start it at all until 2A/2B show it
is still needed.

### Problem 3 — rendering issues network requests

#### 3A. Move context fetching out of the render path
Diff `convo_v` per session inside `tick()`, enqueue the needed fetches there, batch-apply all
results, then render **once**.

- **Pros** — Breaks the render→fetch→render loop at its root. One render per poll no matter how many
  sessions moved. Contained, client-only.
- **Cons** — Still issues up to 48 fetches when many sessions move together; needs a concurrency cap
  to avoid starving the poll.

#### 3B. Stop fetching context for collapsed cards entirely
A collapsed card needs `last_msg` (already in `/api/fleet`, already capped at 800 chars by
invariant 35) and a file count. Add `files_n` plus the two or three most recent file chips to the
session payload and delete the `ensureCtx` call at `static/js/cards.js:198`. Fetch `/api/context`
only for the open pane.

- **Pros** — Eliminates 47 of 48 fetches outright. A collapsed card should never need a second
  request. Removes `cardTop`'s hidden dependency on `ctxCache`. Paired with dropping `closed_ids`
  from every poll (40 KB of the 245 KB), the payload roughly halves.
- **Cons** — `cardDetail`'s "delivered files (N)" fold reads `ctxCache[sid].files`; it needs the new
  field or a fetch on expand. Small per-session payload increase, offset many times over.

#### 3C. Viewport-gated, concurrency-limited fetch queue
IntersectionObserver plus a 3-in-flight cap: only fetch context for cards actually on screen and the
open pane.

- **Pros** — No server change, and it protects the 2 s poll from head-of-line blocking behind a fetch
  burst regardless of which other option is chosen.
- **Cons** — A band-aid. Still fetches and still re-renders per completion unless combined with 3A.
  Scrolling triggers a fresh burst.

**Recommendation — 3B, then 3A.** 3B removes the requests rather than managing them, and it is the
one that fixes mobile, where the 6-connection cap is what makes the whole UI go stale for seconds.
3A then guarantees one render per poll for the pane fetches that remain. Keep 3C in reserve as a
safety valve, not a fix.

### Problem 4 — the reappearing prompt

All three require the same companion change: `sendPerm`
(`static/js/settings-actions.js:863`) and `sendDismiss` (`:697`) must set `answered[sid]` **before**
the await, the way `sendOption` already does via `beginOptimisticAnswer`
(`static/js/context.js:209`).

#### 4A. One server-owned request identity
Derive a stable `request_id` that both sources map to — a digest of session id plus question/tool
content — so the hook capture and the transcript fallback emit the *same* id. The client keys
suppression on `request_id`; the raw nonce stays internal to key injection.

- **Pros** — Fixes the root cause and makes the dual-source handoff invisible everywhere else
  (notifications, question-file pairing, receipts). The digest is stable across the handoff because
  the questions payload is byte-identical in both sources.
- **Cons** — Permission prompts carry much weaker content (a message string, not tool input), so
  repeated identical permission asks could collide. Needs a stable first-seen marker from
  `pending_seen` mixed in.

#### 4B. Server-side answered fence
When `act()` accepts a nonce, record `_claude_answered[sid] = {nonce, content_digest, at}` and have
`_scan` drop *any* pending matching that digest until a `PostToolUse` clear or `tool_result` confirms
it, with a conservative timeout. Mirrors the `_claude_delivery_uncertain` fence that already exists
for the failure path (invariant 66).

- **Pros** — Fixes it once for every client — phone, desktop, PWA, offline replay — instead of
  per-browser. Closes a real hazard that exists today: two devices can both answer the same prompt.
  Reuses machinery already in the engine.
- **Cons** — A bug in the fence hides a genuinely new question. Needs a content-difference escape
  hatch so a *different* question always shows.

#### 4C. Delete the transcript fallback for questions
Invariant 1 states plainly that questions never come from the transcript, yet
`fleetdash/engine_scan.py:811-814` does exactly that. Remove the question branch, keep the permission
branch, and widen `hook_pending`'s 5-second ghost guard
(`fleetdash/engine_context.py:148`) so a capture survives the registry's `waiting` flicker.

- **Pros** — Smallest diff by far. Removes the second namespace for questions by deleting the code
  that contradicts the documented invariant.
- **Cons** — Half a fix. The `toolu_` nonces in the production log are almost all **permissions**,
  and permission captures have no clear hook, so they genuinely need the transcript path. Questions
  stop flickering; permissions do not.

**Recommendation — 4B, plus the `sendPerm`/`sendDismiss` companion fix.** The server is the only
place that can see both namespaces, and it is the only fix that also protects the multi-device case.
Do **4C** alongside it as cheap hygiene; that fallback branch should not exist regardless. Treat 4A
as the deeper cleanup once 4B has proven where the identity boundaries actually are.

---

## Suggested first pass

`1B + 2A + 3B + 4B/4C`. None of these touch the rendering architecture or the applet key sequences,
and together they remove the scan-lock wait, three quarters of the render work, ~47 of 48 per-poll
requests, and the reappearing prompt.
