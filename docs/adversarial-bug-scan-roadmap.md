# Adversarial UI + server bug scan roadmap

Status: automated implementation and release gates complete on `fix/send-now-or-queue`; isolated
staging API smoke passed, real-phone validation pending.
Scope: current draft PR and isolated staging only; production promotion remains user-gated.
Evidence date: 2026-07-18.

Follow-on mobile chat/Markdown layout work is tracked separately in
[`mobile-chat-viewer-layout-fix-plan.md`](mobile-chat-viewer-layout-fix-plan.md); its open gates now
also block staging approval and production promotion.

This is the source of truth for the adversarial scan. A finding appears here only after a second
source pass by the primary agent. “Fixed” means the implementation and named focused regression
pass; it is not release-complete until the complete backend/browser suites and staging reproduction
also pass. Line numbers describe the audited revision and may move during implementation.

## Release gates

1. Close every accepted finding below or document concrete blocking evidence.
2. Keep unknown/external Codex threads read-only under every action path.
3. Preserve every message, image, draft, loaded transcript page, and delivery receipt through
   offline, retry, reload, compaction, provider restart, and transient discovery gaps.
4. Run backend, push-worker, desktop browser, mobile browser, restart, compaction, delayed-poll,
   concurrent-injector, and malformed-provider tests.
5. Restart staging and complete a phone test. Do not ready, merge, or deploy production before the
   user approves staging.

## Primary acceptance contract

These are the user-reported behaviors that caused this branch and remain gates even when they are
not adversarial-scan findings:

| ID | Required behavior | Current evidence |
| --- | --- | --- |
| A01 | Ordinary Send means **send now when safe; otherwise queue until the exact session becomes available**. It must not fail merely because the provider is busy or temporarily disconnected. | Implemented in `engine.py:5849-5958`; backend and desktop/mobile queue regressions pass. |
| A02 | A queued message/image is immediately labelled Queued, survives navigation/browser/daemon restart, dispatches exactly once, and remains visible until canonical transcript confirmation. | Implemented across `outbox.py` and `static/app.js`; durability, idempotency, reconnect, photo, and receipt regressions pass. F12-F17 and F21 harden its failure paths. |
| A03 | Codex compaction/reconnect must preserve exact thread ownership, current turn routing, model, effort, and collaboration mode. A post-compact send must reach the same session. | Committed branch fixes plus current F01, F05-F06, F18, F20, F31, and F36 coverage; automated restart/compaction coverage passes, staging phone confirmation remains. |
| A04 | The composer `+` menu must stay open through the trusted tap, dismiss cleanly after choosing Schedule or Photo, and never drop the selected action. | Existing desktop/mobile photo-menu regression passes; included again in the complete matrix and real-phone staging script. |
| A05 | Every notification type has two independent controls: in-app Notification Center visibility and external Web Push cadence. | Implemented by F04; backend/browser coverage passes and phone staging remains. |
| A06 | The top-right menu of an existing full chat must allow changing both model and effort with provider-valid choices and explicit saved/error feedback. | Implemented for both providers; B1-B15 focused regressions and the complete automated gates pass. Phone staging validation remains. |
| A07 | The complete UI/server scan, accepted fixes, documentation, and verification evidence live in this file and stay synchronized as work changes. | Active; F01-F57, B1-B15, and the verification ledger are the source of truth. |
| A08 | Release is staging-first. The draft PR is never readied, merged, or promoted to production until the user completes the phone test and explicitly approves it. | Locked release policy. |

## Verification ledger

- Backend: 318 tests passed after the combined implementation.
- Push/service worker: `npm run test:push` passed 11/11.
- Changed-path browser batches: 23 passed with one expected desktop skip; adjacent offline coverage
  passed in desktop and mobile.
- First complete browser matrix: 156 passed and 14 expected live/mobile-only skips; it exposed F37
  and F38. Both fixes then passed their focused desktop/mobile matrix (4/4).
- First doubled complete browser gate: 317 passed, 28 expected live-only skips, and three
  repeat-only mobile failures. Those failures are tracked as F40-F42; the clean count reset to zero.
- Clean complete browser matrix runs: two of two consecutive passes. The final
  `--repeat-each=2` gate passed 352 tests with 28 expected live-only/platform skips and zero failures
  in 35.1 minutes. This is 176 passes and 14 skips per complete desktop/mobile matrix. All four
  named interaction latency inventories passed their first-feedback and local-completion budgets.
- Latest focused provider-control run: 150/152 passed. It caught two turn-fence integration defects:
  a busy registry observation returned before marking the fence active, and an older relay test did
  not model the provider transition after a prior text send. Both remain part of B11 and must pass
  before the complete backend gate is credited.
- F52/F54 focused transport-hook run: 11/11 passed. It covers readiness failure before any background
  write, partial/unclean-detach ambiguity after a write, atomic 0600 hook JSON, no temp residue, and
  Notification non-clobbering of a richer question capture.
- First post-F54 focused provider/Outbox run: 244/246 passed. The two failures were stale fixture
  models, not accepted source behavior: one left a permission hook active after its simulated answer,
  and one changed permission mode without advancing the new native-evidence offset. Both tests must
  model the authoritative provider transition before this gate is credited.
- After correcting those fixtures, the two regressions pass. The consolidated release-blocker
  command passed 10/10, covering exact accepted-prefix projection, restart durability, lost-control-
  acknowledgement fail-closed behavior, one-key/multi-key prompt retry suppression, native prompt
  capability parity, direct text/image/handoff confirmation-unknown routing, and Codex explicit-null
  effort persistence through refresh. The entire Outbox module also passed 32/32.
- F50/F51 browser parity passed 4/4 in the focused Playwright run: both the native-prompt capability
  gate and no-automatic-retry confirmation-unknown UI passed on desktop and mobile.
- F52/F56 clearing/reader follow-up initially passed 2/2, but the final independent audit rejected
  one test's premise: a single transition away from `waiting` is not enough to clear the fence while
  the exact same permission capture remains valid. That stale capture can suppress a newer
  Notification and later become actionable again. F52 was reopened until the corrected same-nonce
  busy→new-waiting race passed. The independent F56 descriptor-close regression remains valid.
- Combined post-fix provider/queue gate: 253/253 passed across the Claude and Codex engines,
  Codex adapter fixtures/protocol, Outbox, background transport, and pending-capture hook. This
  supersedes the earlier 244/246 fixture run; complete browser gate passed; staging gate remains open.
- First complete backend run: 349/350; one load-sensitive Web Push CPU-threshold assertion failed
  while wall/read responsiveness remained unaffected, then passed 5/5 focused. The immediate clean
  complete rerun supersedes it: 351/351 passed in 25.967s. One clean complete backend pass is
  credited in that historical stage; it was rerun after the reopened F52/F54 fixes below.
- F54 Engine-routing follow-up: 128/128 provider/background tests passed. The new regressions cover
  single-select, multi-select, dismiss, deny, one/two-command settings, and one/two-key permission
  cycles: a proven pre-write background failure creates no durable uncertainty fence and the exact
  action remains safely retryable. Post-write ambiguity remains fail-closed.
- The first post-provider doubled browser gate was stopped after 37 passes because source changed.
  Before interruption it exposed F57: ordinary actionable question fixtures still advertised the
  pre-F50 unconditional capability shape, so native answer buttons were disabled and two established
  recovery/question tests could not exercise their actions. No complete browser pass was credited
  from that discarded run.
- F52's corrected same-nonce busy→waiting fence passed 2/2 with native capability parity. F57's
  affected recovery/question flows passed 4/4 on desktop and mobile. Both then passed the restarted
  complete gates recorded below.
- Post-audit complete backend gate: 353/353 passed in 26.214s after F52, F54, and F57. This is the
  currently credited complete backend result.
- Final doubled complete browser gate: 352 passed, 28 expected skips, zero failed in 35.1 minutes.
  Two consecutive complete desktop/mobile matrices and all four latency inventories are credited.
- Isolated staging restart/API smoke passed: port 8378 reported `mode=staging`, this checkout as its
  source root, both providers healthy, one idle controllable staging-owned Codex session, and 27
  production sessions projected read-only. Authenticated Outbox and notification-policy reads also
  passed. Real-iPhone interaction and installed Web Push checks remain pending.

## P0 — security and cross-session integrity

### F01 — Unknown Codex IDs bypass ownership checks

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Impact: an authenticated caller can submit, resume, change mode, compact, review, interrupt,
  archive, or close an arbitrary thread ID that is absent from the current Fleet projection.
- Evidence: `codex_adapter.py:1792-1930` rejects only a known read-only row and applies capability
  checks only when `known` exists; `_ensure_loaded` at `codex_adapter.py:1549-1584` may resume the
  supplied ID.
- Fix: require an exact current session and persisted `runtime_owner=fleet_shared` for every
  existing-thread action. Thread creation remains the only action that accepts no existing target.
- Test: enumerate every thread-scoped action against unknown and external IDs and assert zero client
  calls and no state mutation.

### F02 — Concurrent native terminal actions share one injector mailbox

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Impact: simultaneous actions can overwrite each other, type into the wrong terminal, or leave one
  request waiting for 30 seconds.
- Evidence: threaded HTTP handling in `server.py:523-529`; one unguarded
  `inject-request.txt`/`inject-result.txt` transaction in `engine.py:6463-6507`; the applet reads and
  overwrites the same files in `injector.applescript:33-43,66,113`.
- Fix: serialize the complete publish/open/result exchange with a dedicated Engine lock and publish
  the request atomically. Keep per-request IDs as result validation.
- Test: two barrier-synchronized writes to different TTYs must not overlap and must receive their
  exact matching result.

### F03 — Codex subagent transcript reads are not parent-scoped

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Impact: a caller can pair one parent session with another thread ID and read that other thread as
  if it were the parent’s subagent.
- Evidence: `engine.py:4337-4355` calls Codex before proving membership;
  `codex_adapter.py:1740-1754` ignores the parent and reads `agent_id` directly.
- Fix: require the parent to exist and the exact child ID to appear in `parent.agents` in both Engine
  and adapter before `thread/read`.
- Test: a sibling session’s child and an unknown UUID both return `no such subagent` without a
  provider read.

### F04 — Notification type “Off” controls push only, not Notification Center

- Status: fixed; backend/browser regressions passed; phone staging gate pending.
- Impact: disabled event kinds continue to enter the in-app center, unread badge, active count, and
  title count.
- Evidence: kind policy contains push cadence/severity only (`briefing.py:50-68,535-671`); events are
  always inserted (`briefing.py:743-815`); policy is consulted only during push scheduling
  (`briefing.py:1061-1104`); snapshot counts all events (`briefing.py:1732-1792`).
- Fix: add an independent `in_app_enabled` switch per kind, expose it clearly in Settings, and apply
  it consistently to projections, unread/active counts, title/badges, and future event visibility.
  Preserve hidden rows as audit history so re-enabling does not fabricate new events.
- Test: every kind across push on/off × in-app on/off.

## P1 — delivery, provider recovery, and durable UI state

### F05 — One malformed Codex thread can freeze refresh forever

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: `_refreshing` is set in `codex_adapter.py:994-1001`; only list failure is caught at
  `1003-1019`; the flag is cleared only on the normal tail at `1366-1371`.
- Fix: outer failure containment/finally plus per-thread normalization isolation. Preserve healthy
  rows, make the bad row stale with a bounded diagnostic, and allow the next refresh to recover.
- Test: malformed + healthy thread, followed by a repaired payload.

### F06 — Provider-list failure leaves stale Codex mutation controls enabled

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: list failure changes only state in `codex_adapter.py:1008-1017`; the existing fixture at
  `tests/test_codex_adapter_fixtures.py:355-365` currently asserts stale submit/close remain enabled.
- Fix: stale state disables direct mutation/close/archive/answer controls and exposes durable queue
  submission where safe. `act()` must independently reject stale rows.
- Test: every stale capability and forged direct action.

### F07 — Sustained slow `/api/fleet` responses starve polling forever

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: every tick aborts the previous controller (`static/app.js:5044-5049`) while a fixed
  two-second interval starts the next (`5080-5086`). At 2.5 seconds latency no response applies and
  no offline banner appears.
- Fix: single-flight periodic polling scheduled after completion. Explicit force refresh may
  supersede an older request while retaining sequence protection and a bounded hung-request timeout.
- Test: sustained 2.5-second responses update the UI; a genuinely hung request times out visibly.

### F08 — Context refresh failure destroys the last good conversation

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: `ensureCtx` deletes the cache on fetch error (`static/app.js:1493-1503`); subagent refresh
  replaces prior messages with an empty error (`2787-2802`); the service worker bypasses context APIs
  (`static/sw.js:38-63`).
- Fix: preserve last-good data with stale/error metadata and persist a bounded main/subagent/closed
  context cache for offline reload. Show stale/offline state without hiding history.
- Test: loaded chat + failed refresh and full offline reload both keep the transcript readable.

### F09 — A new conversation revision discards all older loaded pages

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: older pages prepend at `static/app.js:1517-1534`, but `ensureCtx` and `ensureAgentCtx`
  replace the entire window with the newest 50 at `1493-1501,2787-2800`.
- Fix: stable-ID/cursor merge and dedupe of the refreshed tail into the loaded window; preserve the
  older cursor and the visible-message scroll anchor.
- Test: load 200 rows, advance `convo_v`, and retain the oldest loaded row and reading position.

### F10 — Model/effort dependent selectors can submit stale combinations

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: new-session model changes do not repair effort (`static/app.js:4534-4549`), while the
  focused-form render guard at `5001-5004` blocks replacement. Handoff has the same dependency at
  `1917-1939,1993-2000`.
- Fix: one immediate dependent-selector synchronizer for new session, handoff, and scheduled spawn;
  clear selections that are not valid for the new provider/model.
- Test: provider/model/worktree changes while focused never submit an invalid stale value.

### F11 — Failed command/skill actions permanently clear their draft

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: composer/draft/images clear before action dispatch at `static/app.js:4046-4050`; those
  paths return without an optimistic restorable row.
- Fix: clear only after provider acceptance or use the same durable recovery receipt as ordinary
  text. Restore exact command/arguments on failure and reload.
- Test: rejected command and skill retain exact input through rerender/reload.

### F12 — Double-submit creates duplicate scheduled Outbox rows

- Status: fixed; backend/browser regressions passed; complete browser gate passed; staging gate pending.
- Evidence: `submitSchedule` has no busy guard or idempotency key and leaves the submit button enabled
  (`static/app.js:861-902`).
- Fix: synchronous busy lock/disabled feedback plus a stable create idempotency key enforced by the
  server.
- Test: two immediate submissions produce one row and one provider intent.

### F13 — Ambiguous reconnect delivery deletes the durable offline record

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: `flushOfflineMessages` removes persistence at `static/app.js:4117-4121` before handling
  `network_error` at `4122-4125`.
- Fix: retain a durable `confirmation_unknown` record with the same idempotency ID until canonical
  confirmation or manual restore/delete.
- Test: connection loss after dispatch survives reload without retrying or losing the text/images.

### F14 — Server-queued chat receipts vanish after reload

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: server Outbox IDs live only on in-memory optimistic rows (`static/app.js:3715-3739`);
  reconciliation at `661-678` cannot rebuild a missing row; only offline messages hydrate at
  `1606-1615`.
- Fix: persist or reconstruct per-session Outbox receipts and keep image draft IDs until terminal
  state/canonical confirmation.
- Test: queue online to a busy session, reload the owning chat, then confirm sent/canonical cleanup.

### F15 — Retrying or retargeting an Outbox message drops its photos

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: public rows strip paths (`outbox.py:203-227`); retry rebuilds scalar fields from the
  public row (`576-603`).
- Fix: retry from the internal row and copy queue-owned images into independently owned retry
  storage. Cleanup of either attempt must not break the other.
- Test: bytes, image count, dispatch payload, and independent cleanup.

### F16 — Old unavailable Outbox rows can starve later ready rows

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: the oldest due 20 are selected by trigger time (`outbox.py:773-784`); waiting rows get a
  one-second retry and win the same batch again (`843-851`). Usage-reset no-change paths can also
  continue without moving the next attempt (`793-806`).
- Fix: fairness by `next_attempt_at` with a rotating stable tie-breaker; always defer unchanged
  usage-reset checks.
- Test: 21 rows, first 20 permanently busy, final row ready and delivered promptly.

### F17 — Claude Outbox dispatch can inject against stale idle state

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: Outbox uses a snapshot copy (`engine.py:5708-5725,5808-5812`); ordinary Claude text does
  not perform the freshness re-poll used for prompt answers (`5885-6165`).
- Fix: reject/queue an over-age snapshot and re-read authoritative registry/pending state immediately
  before queued/direct terminal text injection.
- Test: cached idle changes to waiting before dispatch; no text is injected and the row remains queued.

### F18 — Codex App Server initialization races and can remain half-initialized

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: `CodexAppServer.start` publishes the process before initialize/initialized completes
  (`codex_adapter.py:350-389`); concurrent callers may send while initialization is in progress;
  initialize timeout cleanup is narrower than all initialization failures (`435-455`).
- Fix: explicit starting/initializing/ready state under a condition; one initializer, bounded waiters,
  and full transport cleanup on initialization or notification failure.
- Test: concurrent first requests and failed initialize followed by successful retry.

### F19 — Rollout observer reads an unbounded append before applying row limits

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: `codex_observer.py:146-153` calls `read()` and concatenates the entire append; row bounds
  are enforced only after allocation at `114-130`.
- Fix: chunked incremental line parsing, bounded bytes/time per pass, and discard mode for an
  oversized row without retaining its remainder.
- Test: huge single row and large historical append stay within a fixed memory/read budget.

### F20 — Fleet-owned Codex threads beyond the newest 100 can disappear after restart

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: production list defaults to 100 (`codex_adapter.py:676-688`); omitted remembered rows
  survive only with loaded/live/recent evidence (`1303-1359`); the current 150-row fixture bypasses
  production pagination.
- Fix: fully page an explicit sane bound and/or targeted-read every persisted owned ID before
  declaring it absent. Absence from one archive page must never close a controllable session.
- Test: real paginated fake with 150 rows and empty loaded state after restart.

### F21 — Successful retry leaves the failed source permanently in “needs review”

- Status: fixed; regression passed; fresh staging gate pending.
- Evidence: `outbox.py:576-593` creates a linked retry but never resolves/supersedes the source;
  `counts()` at `530-538` counts the source forever. Staging retained two attention rows after the
  replacement was sent successfully.
- Fix: introduce an explicit superseded/resolved audit state or equivalent relation-aware count.
  Preserve history but remove resolved source attempts from current attention.
- Test: failed → retry → sent leaves zero attention while both immutable attempts remain in history.

## P2 — consistency, accessibility, and bounded resources

### F22 — Notification action state leaks between events and accepts double taps

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: one global state at `static/app.js:1224-1227`; no per-event busy/generation guard at
  `1342-1356`.
- Fix/test: event-ID keyed action state, disabled buttons, request generation, and A/B event race.

### F23 — One transient omitted snapshot closes full-screen chat

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: `static/app.js:2697-2703` closes immediately when the open SID is absent.
- Fix/test: retain last-good session/context through bounded reconnect/discovery grace and close only
  on authoritative tombstone/closed evidence.

### F24 — iOS visual-viewport handling excludes most full-screen forms

- Status: fixed in implementation; mobile geometry regression passed; real-phone gate pending.
- Evidence: JS keyboard handling recognizes only chat/agent/viewer (`static/app.js:2222-2235`); CSS
  visual viewport applies only to those surfaces (`static/fleet.css:1377-1390`).
- Fix/test: shared full-screen overlay selector, visual viewport sizing, focused-field scroll, and
  mobile browser geometry checks for Settings, Search, Handoff, Outbox, and Schedule.

### F25 — Full-screen overlays are not semantic/focus modals

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: overlay roots at `dashboard.html:149-191` lack dialog semantics; open/close functions do
  not consistently focus, trap, inert, or restore the opener (`static/app.js:2197-2209,2560-2590`).
- Fix/test: centralized stack-aware modal controller with `role=dialog`, `aria-modal`, labels, focus
  trap, shell inerting, and opener restoration.

### F26 — Ordinary Claude cards lack a keyboard/screen-reader open control

- Status: fixed; desktop/mobile regression passed; complete browser gate passed; staging gate pending.
- Evidence: pointer-only header div at `static/app.js:3067-3068`; ordinary Claude states omit the
  explicit primary button at `3061-3075`.
- Fix/test: a real labelled open button for every card without nested interactive controls.

### F27 — Outbox optimistic concurrency does not check the expected version

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: update reads a version but the SQL predicate omits it (`outbox.py:540-559`).
- Fix/test: require expected version from the client and make one of two same-version edits fail stale.

### F28 — Worktree cleanup consumes its ticket before transient process shutdown

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: ticket pop precedes the still-running check in `engine.py:4788-4814`.
- Fix/test: retain a valid ticket on transient process/inspection failure; second attempt succeeds.

### F29 — Structured-question counts are client-controlled and unbounded

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: `engine.py:6024-6067` trusts client counts when building key sequences.
- Fix/test: derive indices/counts from authoritative pending data, impose a strict cap, and reject a
  malicious billion-option request before allocating or injecting.

### F30 — Upload IDs can collide across sessions and storage has no total quota

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: paths derive only from a client ID (`engine.py:1440-1443`); replace can overwrite the same
  final/meta name (`1495-1557`); cleanup samples only the first 2,000 names.
- Fix/test: server-generated or session-namespaced atomic IDs, reject collisions, global byte/count
  quota, and fair complete cleanup.

### F31 — Codex refresh performs sequential detail reads without a total budget

- Status: fixed; regression passed; complete browser gate passed; staging gate pending.
- Evidence: managed parent details and child details are read serially in
  `codex_adapter.py:1083-1098,1185-1211`, each with provider timeouts.
- Fix/test: bounded concurrency and a total refresh budget, prioritizing active/visible owned rows;
  retain cached detail for work not reached within the pass.

### F32 — Full browser suite contains load-sensitive transient assertions

- Status: fixed; two clean complete matrices and all four latency inventories passed.
- Evidence: full run produced four failures after long serial load; every failed test passed in
  isolation/repetition (quick response 3/3, three mobile paths 6/6). One assertion requires observing
  “Submitting” even when the response already reached “Submitted.” Verification later exposed the
  same class of defect in poll callbacks that dereferenced `.at(-1)` before the fixture had recorded
  its first action, converting a retryable empty state into an immediate `TypeError`.
- Fix/test: assert immediate feedback without requiring a transient state after a faster terminal
  state, use event-driven fixture gates where a specific intermediate state matters, make every
  action poll tolerate an empty precondition, and rerun the complete matrix twice.

### F33 — Notification delivery problems include old successful deliveries

- Status: found during implementation verification; fixed with regression coverage.
- Evidence: the delivery-problem query selected every latest delivery for a currently failing device,
  regardless of that delivery's own state (`briefing.py:1825-1850` in the fixed revision). A prior
  successful Web Push test therefore appeared as a second current problem after an unrelated push
  failed.
- Fix: project only failed, expired, queued, sending, or retrying deliveries, and apply the independent
  in-app `failure`-kind visibility rule to this noncanonical problem projection too.
- Test: a successful device test followed by a terminal notification failure produces one problem;
  disabling in-app failures hides both the canonical failure event and the problem projection.

### F34 — A forged permission action can target a question nonce

- Status: found during primary-agent source verification; fixed with regression coverage.
- Evidence: nonce freshness was checked before key generation, but the server did not require the
  submitted action type to match the authoritative pending kind. A `permission` action carrying a
  live question nonce could therefore inject a permission digit into the question TUI.
- Fix: derive the pending kind from the nonce-matched hook/transcript row and require question
  actions to target questions and permission actions to target permission requests. Dismiss remains
  intentionally valid for either prompt surface.
- Test: a valid question nonce plus forged permission action is rejected with zero terminal writes.

### F35 — Two retry requests can create duplicate active replacement messages

- Status: found during primary-agent source verification; fixed with regression coverage.
- Evidence: the browser serialized one button, but `OutboxManager.retry` left the failed source
  retryable and did not check for an existing active child. Two devices or concurrent requests could
  create two replacements that would later dispatch independently.
- Fix: inside the same immediate transaction that publishes a retry, reject creation while that
  source already has a pending, sending, sent, or confirmation-unknown direct retry. A failed,
  blocked, cancelled, or superseded child does not prevent a deliberate later retry.
- Test: the first retry is created and a second request against its source fails stale, leaving one
  replacement row.

### F36 — Unknown Codex IDs can read unprojected thread context

- Status: found during primary-agent source verification; fixed with regression coverage.
- Evidence: Engine routed every `codex:*` context and file request directly to the adapter, and
  `closed_context` fell through to Codex even when no exact ledger row existed. Read-only Fleet
  access is intentional, but it applies to projected live/history sessions—not arbitrary provider
  identifiers absent from Fleet.
- Fix: live context requires an exact current Codex projection; closed context requires an exact
  closed ledger row; file access requires either one before the adapter performs its existing
  artifact-path whitelist check.
- Test: unknown live, closed, and file requests are rejected without any provider call.

### F37 — Handoff dependency repair can collapse Advanced settings

- Status: found during complete-matrix verification; fixed with regression coverage.
- Evidence: changing the handoff model replaces the open `<details>` element. Its detached toggle
  event could then write `advanced=false` after the replacement rendered, hiding effort and
  worktree controls during the same interaction.
- Fix: capture the live open state before replacement and ignore toggle events from detached
  elements.
- Test: the existing exact-provider handoff path changes model, then immediately changes effort and
  worktree settings in both desktop and mobile projects.

### F38 — Latency inventory referenced removed global notification state

- Status: found during complete-matrix verification; fixed.
- Evidence: notification actions became correctly event-scoped under F22, but the latency harness
  still waited on the deleted `notificationActionState` global and aborted before measuring its
  budgets.
- Fix: wait on the exact event key in `notificationActionStates`.
- Test: the named latency inventory completes in both desktop and mobile projects and retains the
  100 ms first-feedback and 250 ms local-completion budgets.

### F39 — Expected connection loss is reported as a render crash

- Status: found during clean-matrix verification; fixed with regression coverage.
- Evidence: the hardened poller preserves the last usable screen and shows offline state after a
  rejected `fetch()`, but its catch block logged every non-timeout failure as
  `Fleet Dash render/poll failed` at console-error severity. A normal transient disconnect therefore
  looked identical to a JavaScript rendering defect and failed the no-runtime-errors gate.
- Fix: classify network `TypeError` failures as warnings while retaining error severity for genuine
  render/runtime exceptions.
- Test: the connection-loss flows retain the full screen and offline banner without a console
  runtime error; the complete desktop/mobile matrix keeps its global runtime-error assertion.

### F40 — Search criteria can retain the prior query during a rapid mobile filter change

- Status: fixed with deterministic regression; complete browser gate passed; staging gate pending.
- Evidence: `tests/browser/fleet.spec.js:466-468` clears the query, changes the provider, and waits
  for the Claude-only result. The trace showed the product request still contained
  `q=protocol+regression&provider=claude`; the failure snapshot likewise restored the old query.
  Search criteria lived only in DOM controls, so same-document browser/form restoration could
  silently replace a cleared value without dispatching another input event.
- Fix: one canonical `searchFilters` state owns query/provider/kind/project. Every UI event updates
  it, every request reads it, and controls resynchronize from it before dispatch; overlay startup
  initializes both state and controls together.
- Test: simulate delayed, event-free form restoration after the query is cleared, then require the
  provider request to omit `q`, the DOM to repair to empty, and the Claude result to render.

### F41 — Optimistic direct-text timeout can fail to expose its restore action

- Status: fixed; 28 focused desktop/mobile repetitions passed; complete browser gate passed; staging gate pending.
- Evidence: `tests/browser/fleet.spec.js:1409-1413` waits 16 seconds for the failed-message restore
  control after the 15-second confirmation deadline. Trace timing showed the sending receipt at
  `1751140.036`, the assertion ending at `1767364.826`, and the restore control appearing about 38
  ms later. The one-shot timer had fired, but recovery depended on a full fleet repaint that crossed
  the assertion boundary under mobile trace/render load.
- Fix: each optimistic send owns an absolute confirmation deadline. Every render reconciles overdue
  rows, so background timer throttling cannot strand them; timeout and immediate failure repaint the
  open receipt synchronously instead of waiting for a fleet render. Confirmation, removal, and
  restore all clear the owning timer.
- Test: a fake clock proves timeout recovery while `uiRefresh()` is suppressed, and a separate case
  proves the next render self-heals an overdue row after timer throttling. Original/new mobile flows
  passed 8/8, desktop 4/4, and the two new regressions passed 16/16 across both layouts.

### F42 — Closing full chat can lose focus restoration to the card Chat button

- Status: fixed; 20/20 focused desktop/mobile repetitions passed; complete browser gate passed; staging gate pending.
- Evidence: `tests/browser/fleet.spec.js:2399-2401` closes `#sview` and requires focus to return to
  the exact card Chat control. Trace call `call@2260` closed the view and the modal controller did
  restore focus, but the next fleet poll replaced `.ctop` with `innerHTML`, detached that focused
  node, and left `<body>` active.
- Fix: card reconciliation captures the focused header control's semantic identity and restores the
  matching enabled, visible, non-inert replacement with `preventScroll`. This protects Chat, pin,
  terminal, and primary card controls across poll-driven header replacement.
- Test: force a live card reconciliation immediately after full-chat close and require the restored
  Chat focus to survive it; 10 desktop and 10 mobile repetitions passed.

### F43 — Existing-chat model and effort controls are absent

- Status: fixed; all B1-B15 focused regressions, the 253-test provider/queue gate, and the complete
  backend/browser gates passed; staging validation remains.
- Evidence: the full-chat overflow at `static/app.js:1969-2021` exposes Codex collaboration mode and
  Claude permission mode, but no model or effort controls. The Codex action path at
  `codex_adapter.py:1817-1841` changes model/effort only as incidental inputs to a mode update; no
  provider-neutral existing-session settings action exists. The current branch commits contain no
  implementation for the user's explicit request.
- Fix: add provider-valid model/effort selectors to the full-chat top-right menu, serialize and
  version the save, retain the prior values on failure, and update server-owned session metadata only
  after provider acceptance. Disable it while the provider cannot safely apply settings. Claude and
  Codex must each use their real supported control path; never synthesize a capability from
  transcript access.
- Test: change model, repair effort options immediately, change effort, verify saved/error feedback,
  reload, compact/reconnect, and confirm the next turn uses the accepted values for both providers.

F43 backend review blockers:

1. **B1 — optional/stale compare-and-set.** Both expected model and effort must be required, and the
   comparison must use canonical `thread_state` values received from live provider notifications
   before falling back to the persisted projection. A missing expected field or intervening attached-
   TUI change must reject without calling the provider.
2. **B2 — action-time safety race.** Refresh-time capability is insufficient. A per-thread mutation
   lock must serialize settings, mode, turn start/steer, and compaction; settings must re-read active
   turn, pending request, and compaction evidence immediately before provider mutation. Capability
   must also disable while compaction is active. Refresh metadata/projection writes must participate
   in the same ordering or use a monotonic settings revision: a refresh that derived old values
   before an accepted change must never overwrite the newly persisted model/effort afterward. A
   barrier regression must force that exact interleaving.
3. **B3 — stale catalog remains authoritative after parse failure.** When a changed model-cache
   signature is unreadable or malformed, existing-session settings must disable until a valid
   catalog is loaded; the last old catalog cannot remain mutation authority.
4. **B4 — model catalog normalization exposes hidden/malformed choices.** Filter hidden entries;
   strip, bound, and control-character-check identifiers/efforts/display names; deduplicate model IDs
   deterministically so UI and server validate the same effort set.
5. **B5 — provider success plus metadata failure is misreported as provider failure.** Once the
   provider accepts the settings, keep the accepted UI/projection values and return an explicit
   durability warning if local persistence fails. Never roll the UI back and invite a contradictory
   retry after the provider already changed.
6. **B6 — Codex mode changes bypass settings revision/durability semantics.** A mode update can also
   change effort, but its provider-success path neither bumps/CASes `settings_revision` nor protects
   against a refresh derived before the change; local persistence failure is falsely returned as a
   total action failure. Apply the same commit ordering, revision barrier, and accepted-with-warning
   contract used by model/effort changes.
7. **B7 — Claude validation and terminal mutation are not session-serialized.** The global injector
   mailbox prevents file corruption but does not make the idle/CAS check atomic with the terminal
   write. Two settings actions, or a settings change and a new text turn, can both pass validation
   before either writes. A per-Claude-session mutation lock must cover fresh registry/tail checks,
   native write, and accepted projection update; forced-barrier tests must exercise both races.
8. **B8 — Claude settings ignores pending-request and compaction evidence.** Registry `idle` alone
   is insufficient: fresh hook pending, transcript permission fallback, or `compacting_secs` must
   disable the projected capability and reject the action at the final native boundary so settings
   commands never enter a request/compaction surface.
9. **B9 — queued Claude image dispatch bypasses the session mutation lock.** Its direct native
   injection path does not re-enter `act()`, so it can race a model/effort change or text turn after
   both validated an idle session. It must share the same per-session lock and final freshness gate;
   a barrier regression must force that race.
10. **B10 — Codex settings can revert a fresh collaboration-mode change.** Model/effort mutation
    derives mode from the Fleet projection instead of preferring live App Server settings. If an
    attached TUI changes mode just before the save, Fleet can submit the obsolete mode along with the
    new model. Use live `collaboration_mode` as canonical and cover the interleaving.
11. **B11 — Claude registry lag reopens the lock after a submitted turn.** The per-session lock ends
    after terminal CR, but Claude's registry may still report idle until its asynchronous update.
    A second send or settings action can acquire the lock and inject into the newly started turn. Add
    a short local starting fence that clears only after authoritative provider state observes the
    transition, and force text→text and text→settings races under delayed registry updates.
12. **B12 — a Codex mode change can resurrect an explicitly cleared live effort.** The mode path
    treats live `effort: null` as if the field were absent, falls back to stale projected metadata,
    and submits that old effort along with the mode. Preserve explicit live null as canonical; use a
    fallback only when neither live nor session evidence has the key. Cover attached-TUI clear/reset
    followed by a Fleet mode change and assert the stale effort is never resubmitted.
13. **B13 — Claude can partially accept a two-command settings change while Fleet reports total
    failure.** `/model` and `/effort` are currently one injector batch with one aggregate result. If
    the model command succeeds and the effort command fails, Fleet preserves the old pair and invites
    a contradictory retry even though Claude already changed model. Execute commands through
    individually acknowledged writes (unless a real atomic provider route exists), project every
    accepted component, and return an explicit partial-acceptance warning. Force the second command
    to fail and assert Fleet never claims or displays a full rollback.
14. **B14 — accepted Claude controls are lost on Fleet daemon restart.** Model and permission are
    projected only into the current Tail object and effort uses an in-memory override. If Fleet
    restarts before Claude emits a newer transcript/statusline marker, the UI reverts to old values;
    for a partial permission cycle this displays the wrong approval policy. Persist bounded,
    timestamped accepted model/effort/permission overrides, restore them at Engine start, and retire
    them only when newer authoritative provider evidence arrives. Model/permission supersession must
    use transcript byte-order/evidence positions rather than row timestamps because compaction can
    append older-timestamp rows later; effort may use the statusline file mtime. Persist through the
    serialized config merge, validate/bound restored entries, and prune dead sessions. Reinstantiate
    Engine after both a full settings save and a partial permission cycle, then cover stale-old rows,
    newer direct-TUI evidence, and compaction timestamp inversion.
15. **B15 — the first native control write can apply while its acknowledgement is lost.** Splitting
    model/effort commands and permission-cycle keys only proves previously acknowledged prefixes. A
    one-command change—or the first command/key of a longer change—may reach Claude while the applet
    result times out, leaving Fleet unable to claim either rollback or acceptance. Persist a bounded
    control-delivery-uncertain state, block further model/effort/permission mutations across restart,
    and reconcile only from newer native evidence or an explicit terminal check. Definitive failures
    before native launch may remain retryable; post-launch ambiguity may not. Force first-command and
    first-key applied/lost-result cases, including Engine re-instantiation.

Each blocker requires its named deterministic regression before F43 can be marked fixed.

### F44 — Settled browser settings state can hide later canonical provider changes

- Status: fixed; focused canonical-provider supersession regressions passed; complete browser gate passed; staging gate remains.
- Evidence: `static/app.js:1689-1695` makes the status label prefer `sessionSettingActions`, and
  `static/app.js:1714-1722` returns that saved action for every non-error state without comparing it
  to the current session projection. Successful action entries are never cleared. A later model or
  effort change made directly in Claude/Codex can therefore remain hidden indefinitely, and the next
  Fleet save derives its expected pair from the obsolete action instead of canonical provider state.
- Fix: optimistic action values win only while that exact request is active. Settled feedback may
  remain visible, but any divergent canonical projection supersedes it immediately and becomes the
  next compare-and-set baseline. Remove entries for sessions that leave the fleet and keep the cache
  bounded.
- Test: save through Fleet, mutate canonical fixture/provider settings without another Fleet action,
  refresh, require header/menu to show the canonical values, then verify the next save submits that
  canonical expected pair.

### F45 — Claude permission-mode changes can inject into a pending request or compaction

- Status: fixed; focused backend regressions passed; complete browser gate passed; staging gate remains.
- Impact: while Claude's registry still says idle, a transcript-derived permission prompt or active
  compaction can coexist with an enabled permission-mode control. Shift+Tab then lands in the wrong
  native surface and can make an unintended selection.
- Evidence: the projection at `engine.py:2558-2559` checks only registry idle plus a known mode. The
  action at `engine.py:6299-6326` polls the Tail but does not reject hook pending, `mt.pending`, or
  `compacting_secs`. A deterministic registry-idle probe with a pending Bash tool and active
  compaction returned success and injected `Shift+Tab`.
- Fix: use the same authoritative hook/transcript-pending and compaction gates for both the projected
  permission capability and the final native action boundary. Preserve the per-session mutation
  lock and return a clear unavailable reason without emitting any key.
- Test: cover hook pending, transcript permission fallback, and active compaction separately in the
  fleet projection and direct action path; all must disable/reject with zero injector writes.

### F46 — Direct Claude text and images can inject into a newly appeared request or compaction

- Status: fixed; focused backend regressions passed; complete browser gate passed; staging gate remains.
- Impact: an ordinary send selected from a stale fleet snapshot—or a direct slash-command send—can
  type and submit text into Claude's ask/permission or compaction surface while the registry still
  reports idle.
- Evidence: `engine.py:6214-6222` checks registry idle, but the final direct `text`/`image_text`
  boundary is excluded from the Tail freshness block at `engine.py:6249-6251`. A deterministic idle
  probe with hook pending, transcript Bash pending, and active compaction still returned success and
  injected `/status`. The analogous queued-image path already performs the required checks.
- Fix: under the existing per-session mutation lock, poll the Tail and reject hook pending,
  transcript pending, compaction, or turn-start fence immediately before every direct text/image
  injection. Return queueable provider-unavailable for ordinary send so it enters the durable exact-
  session queue; direct slash-command actions fail safely instead of typing.
- Test: force each freshness signal between snapshot selection and direct text/image dispatch; assert
  zero injector writes, ordinary send becomes Queued, and direct slash shows a recoverable failure.

### F47 — The direct-send freshness fix can block valid subagent relays during normal tool work

- Status: fixed; busy-tool relay and unsafe prompt/compaction regressions passed; complete browser gate passed; staging gate remains.
- Impact: a parent Claude turn normally has Bash or other tool IDs in `Tail.pending`. Treating every
  pending tool as a permission surface rejects the relay command that is specifically meant to reach
  a busy parent so it can forward a message to its subagent.
- Evidence: the new shared gate at `engine.py:6266-6274` applies `mt.pending` to `relay`. A
  deterministic busy-parent probe with an ordinary in-flight Bash tool now returns provider-control
  unavailable and emits no relay, contradicting the verified relay semantics in invariant 19.
- Fix: keep direct idle text/image fail-closed on any pending evidence. For relay, reject registry
  waiting, fresh request hooks, compaction, and the unsafe idle transcript-permission fallback, but
  allow an ordinary tool pending while the parent is actively busy.
- Test: a busy parent with normal tool pending accepts the relay; an idle registry with transcript
  permission fallback rejects it; waiting/hook pending and compaction remain rejected.

### F48 — A partial Claude permission-mode key cycle leaves Fleet showing the wrong safety mode

- Status: fixed; focused partial-cycle and restart-durability regressions passed; complete browser gate passed; staging gate remains.
- Impact: changing across two or more native permission modes emits several Shift+Tab keys. If an
  early key succeeds and a later key fails, Claude is already in an intermediate mode while Fleet
  reports failure and retains the old mode. The displayed approval policy and the next cycle's
  baseline are then wrong.
- Evidence: `engine.py:6338-6344` batches every cycle key, while the injector returns only one
  aggregate result. Fleet updates `Tail.permission_mode` only after whole-batch success near
  `engine.py:6590`. The existing default→plan test proves that path needs two keys but does not model
  a second-key failure.
- Fix: send and acknowledge each cycle key inside the per-session lock. Project every accepted
  intermediate mode. If a later key fails, return the actual accepted mode with an explicit warning
  instead of claiming rollback. Persist that accepted mode until a newer native provider marker
  supersedes it, so a Fleet daemon restart cannot restore the wrong safety state.
- Test: force the second key of default→plan to fail; require the UI/projection to show accept-edits,
  expose a warning, survive Engine re-instantiation, and derive the next save from that accepted
  intermediate state.

### F49 — Partial native delivery can make a Claude prompt answer unsafe to retry

- Status: fixed; all single/multi/dismiss/deny retry-suppression and restart regressions passed;
  complete browser gate passed; staging gate remains.
- Impact: question and approval recipes mutate a stateful terminal surface. If an early digit,
  toggle, navigation key, or even a one-key dismiss/deny Esc succeeds but its acknowledgement is
  lost, Fleet reports ordinary failure and leaves the same nonce actionable. Retrying can untoggle a
  choice, advance twice, or send the repeated key into the next question/main input.
- Evidence: `engine.py:6418-6517` builds multi-step option/multiq/permission sequences, but the
  injector exchange returns one aggregate result. Invariant 4 documents that an extra key after a
  transition can corrupt later questions; current source tracks no applied prefix or uncertain
  nonce.
- Fix: never offer a blind retry after ambiguous multi-step delivery. Record the session+nonce as
  delivery-uncertain in bounded private durable state until canonical pending evidence changes,
  suppress further keys for that nonce across daemon restarts, and show a clear terminal-check/
  refresh state. Per-step acknowledgements may improve diagnosis but do not make replay safe after a
  partially applied stateful sequence.
- Test: force prefix-only application for both a single-select and a multi-select recipe; the first
  call reports delivery-uncertain, every repeat emits zero keys, a newly instantiated Engine with the
  same pending nonce remains blocked, and a new canonical nonce restores normal answering. Repeat
  the lost-ack test for one-key dismiss and deny actions.

### F50 — A visible fallback prompt can advertise actions the native terminal cannot accept yet

- Status: fixed; backend projection/action parity and desktop/mobile browser regressions passed;
  complete browser gate passed; staging gate remains.
- Impact: Fleet can render an idle transcript-derived permission fallback—or a fresh-hook prompt
  during a brief busy ghost window—with enabled answer buttons. Every click then rejects because the
  final native boundary correctly requires registry `waiting`, making the UI misleading and noisy.
- Evidence: `engine.py:2478-2481` can surface fallback pending independently of registry waiting,
  while prompt capabilities near `engine.py:2566-2568` are unconditional. Final actions at
  `engine.py:6229-6232` require `waiting`, so projection and action authority disagree.
- Fix: expose answer/approval capability only when the registry is waiting and the current nonce has
  fresh matching evidence. Keep the request visible for context but disable its controls with
  “Waiting for Claude's native prompt state” until it becomes actionable or disappears.
- Test: cover idle transcript fallback and busy fresh-hook projection through an attempted browser
  click; controls remain disabled and zero keys are emitted, then enable exactly when the same nonce
  reaches registry waiting.

### F51 — Lost injector acknowledgement makes a delivered Claude message look safely retryable

- Status: fixed; direct content, relay/handoff, Outbox, and desktop/mobile browser regressions passed;
  complete browser gate passed; staging gate remains.
- Impact: text, an image, a relay, or handoff content can reach Claude while the applet result is
  lost. Direct chat then presents an ordinary restore/retry path, and an Outbox dispatch becomes
  `failed`/retryable, so the same payload can be delivered twice. Conversely, a definitive applet
  launch failure waits 30 seconds because the launch return code is ignored.
- Evidence: `_iterm_write` at `engine.py:7122+` ignores `open`'s return code and returns the same
  generic failure for an applet error/result timeout after launch. `_send_now_or_queue` treats that
  as ordinary failure, while `outbox.py:999+` marks every non-queueable false result `failed`.
- Fix: distinguish definitive pre-launch failure from confirmation-unknown after a successful applet
  launch. Return a delivery-uncertain code for result-file error/timeout, tell direct-chat users to
  check the terminal before restoring, and move queued sends to `confirmation_unknown` with no
  automatic retry. Fail immediately on a nonzero `open` result.
- Test: force successful native write plus lost result for direct text, direct image, relay, handoff,
  and Outbox dispatch; none auto-retries and every surface says confirmation unknown. A nonzero
  applet launch fails quickly and remains safely retryable.

### F52 — A partial hook-file read can erase the durable prompt-delivery safety fence

- Status: fixed after final independent audit; the corrected same-nonce race and complete automated
  gates pass; staging validation remains.
- Impact: after an ambiguous prompt-answer delivery, one scan can race a non-atomic hook write, see no
  pending request, and permanently clear the durable uncertainty fence. The same nonce then becomes
  retryable again even though Claude never left the prompt.
- Evidence: `hooks/pending-capture.py:41-45` writes the live JSON file in place, while
  `engine.py:4622-4628` maps every parse/read failure to no prompt. The in-progress F49 scan logic
  clears uncertainty on projected pending absence, which can also occur during ghost suppression.
- Fix: write hook captures through a private same-directory temp file, fsync, and atomic replace.
  Never treat one absent/hidden/invalid read—or one non-waiting registry sample while the exact same
  valid capture remains—as canonical completion. Retain the fence while the same nonce is projected;
  clear on a different valid nonce, corroborated disappearance plus provider completion, or session
  removal.
- Test: pause a hook write after truncation/partial JSON while the same waiting nonce remains active;
  repeated scans and Engine restart must retain the fence. Cover permission ambiguity → busy with the
  old capture still present → a new waiting prompt: the old nonce must remain blocked and emit zero
  keys. A different valid nonce or corroborated disappearance/completion clears it exactly once.

### F53 — Codex refresh resurrects an effort explicitly cleared by the live runtime

- Status: fixed; explicit-null action, refresh, and persistence regression passed; complete browser gate passed; staging gate remains.
- Impact: a mode change correctly preserves live `effort: null` for its immediate response, but the
  next refresh treats null as missing and restores stale thread-read or persisted effort. The menu and
  next mutation then use a value the attached runtime explicitly cleared.
- Evidence: `_refresh` at `codex_adapter.py:1478-1497` combines live, thread, and persisted effort
  with truthy `or` expressions. A deterministic live-null plus stale-high fixture stayed null after
  the action and reverted to high after `_refresh()`.
- Fix: use key-presence semantics throughout refresh and discovery persistence, not only in the mode
  action. Explicit live null is canonical; fallback is allowed only when the live field is absent.
- Test: extend the explicit-null mode regression through at least one subsequent refresh and metadata
  reload; projected session and persisted fallback both remain null.

### F54 — Claude background transport hides delivery ambiguity after writing native input

- Status: fixed after the final independent Engine-routing follow-up; 128/128 focused
  provider/background tests passed; complete browser gate passed; staging gate remains.
- Impact: a background-session operation can be written successfully and then fail while detaching.
  Fleet reports an ordinary connection loss, so direct messages, relays, handoffs, prompt answers,
  and controls remain retryable even though the first operation may already have changed Claude.
- Evidence: the transport now distinguishes no-write and post-write failures, but the final audit
  found Engine still treated every failure except `injector_not_launched` as ambiguous. That turned
  the transport's proven pre-write `background_connection_lost` into a durable prompt/control fence.
- Fix: track whether native input began. Validation/readiness failures before the first write remain a
  definitive background connection loss; any error or unclean detach after a write returns
  delivery-uncertain and flows through F49/B15/F51's durable non-retry behavior. Engine now uses one
  explicit no-write classifier at `engine.py:5618-5623` for multi-step controls at
  `engine.py:6966` and `engine.py:7011` and for prompt/single-control routing at
  `engine.py:7041-7053`.
- Test: transport tests fail readiness before write and detach after the first write; only the latter
  is delivery-uncertain. Engine regressions at `tests/test_engine_providers.py:1264-1348` prove all
  prompt shapes and one/multi-step control changes remain retryable after the former without creating
  a durable fence.

### F55 — Relay metadata reads leak a file descriptor

- Status: fixed during the focused backend run; complete backend/browser gates passed; staging gate pending.
- Impact: every relay description lookup leaves its metadata file open until garbage collection,
  producing `ResourceWarning` and risking descriptor pressure during sustained relay use.
- Evidence: the 246-test focused run emitted a warning from `engine.py`'s
  `json.load(open(meta_path))` expression.
- Fix: read agent metadata with a context manager so the descriptor closes on success and parse
  failure.
- Test: the focused provider suite runs with warnings visible and must emit no relay metadata
  `ResourceWarning`.

### F56 — Pending-hook reads leak a file descriptor

- Status: fixed; focused close regression passed; complete backend/browser gates passed; staging gate remains.
- Impact: each live pending-prompt projection opened its JSON capture without deterministically
  closing the descriptor. A fleet with repeated prompts could accumulate descriptors until garbage
  collection and add avoidable pressure to the scan loop.
- Evidence: the audited `Engine.hook_pending` at `engine.py:4624-4629` used
  `json.load(open(path))`, the same leak shape caught in F55.
- Fix: read through a context manager. This does not weaken F52: atomic replacement prevents partial
  JSON, and a transient absent/invalid read still cannot clear an uncertain nonce while the registry
  remains on the native `waiting` surface.
- Test: instrument the built-in file opener, project a real permission capture, and assert its single
  handle is closed when `hook_pending` returns.

### F57 — Browser prompt fixtures no longer model native answer capability

- Status: fixed in the fixture; affected desktop/mobile flows and the complete browser gate pass;
  staging validation remains.
- Impact: after F50 made native prompt authority explicit, established question scenarios kept the
  old default `answer_structured=false` / `decide_approval=false` capability shape. Their buttons are
  correctly disabled, so double-tap, optimistic answer, question navigation, and approval tests stop
  exercising the behavior their names claim to cover.
- Evidence: `tests/browser_fixture_server.py` defaults prompt capabilities off, while the ordinary
  `single-question`, `multi-question`, `claude-question-slow`, and approval scenarios create a live
  pending request without enabling its matching native capability. The doubled run failed at
  `tests/browser/fleet.spec.js:218` with zero optimistic rows and later found the question button
  disabled.
- Fix: ordinary registry-waiting question scenarios explicitly project `answer_structured=true`, and
  ordinary permission scenarios project `decide_approval=true`. Keep `claude-prompt-gate` false until
  its test endpoint changes the native state so F50's disabled-to-enabled transition stays covered.
- Test: rerun the native-decision recovery and complete question/approval flows on desktop and mobile,
  then restart the entire doubled browser matrix from zero.

## Reviewed and not accepted as bugs

- Token-free read-only fleet/context APIs are intentional under the current trusted-tailnet product
  model: README defines `act_token` as the action credential and explicitly supports a yellow
  read-only device without it. Changing that would remove the designed read-only surface and is not
  silently folded into this bug batch. Mutation, file, search, notification, Outbox, settings, and
  diagnostic routes remain token-gated.
- Outbox’s atomic claim prevents duplicate concurrent dispatch; an expired in-flight lease becomes
  confirmation-unknown rather than auto-retry.
- The 120-second missing-target reconnect grace and native-state (not `ui_group`) delivery gate are
  present and tested.
- Post-compaction model/effort persistence is fixed and live-verified on staging.
- Turn ownership is generation-bound; transcript access does not confer control.
- Claude filesystem, closed-transcript, Outbox image path, and config merge confinement checks are
  present.
- Question drawer resize/scroll, plus-menu trusted file taps, ordinary optimistic restore, pin order,
  canonical composer sharing, and external GitHub links were checked and are currently sound.

## Implementation sequence

1. Security boundaries: F01–F04.
2. Provider/injector isolation and refresh recovery: F05–F07, F17–F20, F31.
3. Durable conversation/delivery state: F08–F16, F21, F27–F30.
4. UI consistency/accessibility: F10–F12, F22–F26.
5. Repeat-only browser stability: F32, F40–F42.
6. Existing-session provider controls and native-boundary safety: F43–F57 and B1–B15.
7. Test hardening, documentation reconciliation, isolated staging, and complete release gates.
