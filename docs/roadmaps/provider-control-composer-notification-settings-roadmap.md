# Provider control, mobile composer, and notification settings roadmap

Status: Historical R1-R3 implementation record, amended on `fix/send-now-or-queue` by the adversarial
hardening roadmap. The current draft is PR #12; automated gates pass, while staging phone approval
and production promotion remain blocked by the final gates in
[`adversarial-bug-scan-roadmap.md`](adversarial-bug-scan-roadmap.md).

Date: 2026-07-17

Base: `f9209ff` (`origin/main`)

This is the implementation contract for restoring reliable Claude and Codex control, making the
mobile and Markdown composers one coherent surface, and replacing the long Settings form with a
clear notification-control center and organized application settings.

The implementation was consolidated on the urgent recovery branch after the user directed all three
phases to proceed together. The R1/R2/R3 labels remain verification boundaries; they are no longer
separate branches. Production still promotes only the exact user-merged `origin/main` release.

## Locked product decisions

1. Notification delivery uses one global policy. Every enabled phone and desktop follows the same
   event rules. Devices may still be individually connected, paused, renamed, tested, or removed,
   but they do not have policy overrides.
2. Every canonical notification kind is configurable for external Web Push: question, approval,
   form, reply request, failure, stall, completion, artifact, outcome, budget, measurement, and
   Fleet system notice.
3. Each kind supports a full cadence: Off, once, once plus one reminder, or repeat until resolved,
   with an initial delay, repeat interval, and maximum delivery count.
4. External Web Push cadence and in-app Notification Center visibility are independent per kind.
   Turning push Off does not hide the in-app event; turning in-app visibility Off does not alter
   provider state, Needs-you placement, snooze/mute state, or an explicitly enabled push rule.
5. Global quiet hours, session mute-until-manually-unmuted, delivery history, and a preview of the
   next scheduled notification are included.
6. Claude background sessions use their real Claude Code background transport. A pseudo-terminal
   descriptor held open by a background process is not evidence of an iTerm route.
7. Codex control authority comes only from the connected App Server lifecycle. A turn ID parsed
   from a transcript may describe activity but may never authorize steer or interrupt.
8. If Fleet temporarily loses a controllable Codex runtime, text and image messages remain visible
   in a durable queue. Reconnection to the same active turn steers the queued message; completion
   before reconnection sends it as the next turn. Fleet never resumes a possibly active turn to
   force control.
9. Full chat and Markdown use one canonical composer implementation. Return inserts a newline;
   modified Return or the explicit Send button sends.
10. Selecting a photo dismisses the keyboard before opening the picker, keeps the selection as an
    unsent draft, and does not refocus the composer after selection.
11. The Markdown viewer has no embedded show/hide-conversation panel. A Chat button opens the
    canonical full-screen conversation.

## Confirmed gaps at branch start

### Claude background transport

The deployed `_tty_for_pid` fallback in `engine.py` finds `/dev/ttys009` for the reported background
process, but the process is owned by Claude Code's background PTY host, not iTerm. The production
`noop` action therefore still fails with `session tty not found in iTerm`. The live registry
identifies the background job; the official Claude client owns the attach route. Claude's private
roster contains authentication material and is outside Fleet's transport boundary.

### Codex control authority

`codex_adapter.py` currently promotes an observed transcript turn ID into `client.thread_state` when
the App Server has not supplied a live turn ID. That makes capabilities advertise submit/interrupt
even though `turn/steer` rejects the request with `no active turn to steer`. The browser's existing
offline queue only covers loss of Fleet's HTTP connection; it does not durably cover a provider
runtime that is disconnected while Fleet itself remains online.

### Mobile and Markdown composition

`static/app.js` has separate full-chat and Markdown composer markup. The Markdown viewer retains an
embedded conversation disclosure and does not share the full chat's `+` menu or image-draft surface.
The photo label opens the native picker before Fleet can blur the textarea. On iOS, the overlay uses
the visual viewport while the page behind it remains visible through the keyboard region, and the
composer reserves too much space above the keyboard.

### Settings and notifications

Settings is one long rendered form. Web Push installation, one-device setup, legacy ntfy, session
state, navigation, reading width, conversation peeks, and budgets compete in a single scroll. The
delivery engine already stores device preferences for kind, severity, and initial delay, but the
production scheduler still hard-codes eligible kinds and a one-time 15-minute reminder.

## Requirements ledger

Every row is a release gate. A phase is incomplete until its requirements have source, deterministic
test, staging, and—where named—live-device evidence.

| ID | Requirement and observable acceptance condition | Release |
| --- | --- | --- |
| CTL-001 | A live Claude `kind:bg` session accepts a harmless `noop` and a user-driven message through Claude's background transport without looking for an iTerm tab. | R1 |
| CTL-002 | Foreground Claude sessions retain the resident applet/iTerm path and all verified AskUserQuestion raw-key behavior. | R1 |
| CTL-003 | Text, images, prompt answers, permission choices, interrupts, and closes select the transport from server-owned registry evidence; the client cannot name a socket, tty, PID, or credential. | R1 |
| CTL-004 | Background transport loss is reported as **Connection lost**, not **slow**, and does not crash, hide, or reload the session. | R1 |
| CTL-005 | Fleet does not read or expose Claude daemon authentication, socket paths, roster records, or private rendezvous metadata; it uses the official attach client only. | R1 |
| CDX-001 | Transcript/observer activity may mark a Codex session Working but never grants submit, steer, interrupt, close, archive, compact, review, or relay authority. | R1 |
| CDX-002 | App Server disconnect/restart uses bounded backoff and re-establishes the same Fleet-owned runtime without `thread/resume` while an active turn may exist. | R1 |
| CDX-003 | A direct text or image send made during provider-control loss is durably recorded once with an idempotency key and remains visible after browser reload and Fleet daemon restart. | R1 |
| CDX-004 | Reconnected active turns receive queued input through `turn/steer`; turns that completed first receive it through the normal next-turn path; ambiguous control remains queued. | R1 |
| CDX-005 | Chat shows **Waiting for Codex connection**, **Sending**, **Sent**, or a recoverable failure. The central Outbox shows the same durable item and permits cancel/restore where safe. | R1 |
| CDX-006 | One Codex limit, malformed thread, or disconnected runtime changes only that session and never makes the HTTP server or another provider unreachable. | R1 |
| CMP-001 | Full chat and Markdown call one composer renderer and use the same textarea, `+` menu, Send button, draft store, keyboard behavior, image rules, status text, and disabled states. | R2 |
| CMP-002 | Markdown removes the conversation disclosure. Its right action column places **Chat** above **Send**; Chat opens the same session in full-screen chat and preserves the draft. | R2 |
| CMP-003 | Tapping **Send picture** closes the `+` menu, blurs the active textarea, settles the viewport, and only then opens the native picker. Selection stays drafted without refocus. | R2 |
| CMP-004 | With the iOS keyboard open, an opaque Fleet surface covers the layout viewport, no Now content shows behind the keyboard, and the composer-to-keyboard gap is only the intentional 4px margin. | R2 |
| CMP-005 | The conversation body is the only scrolling region while composing. A deliberate history drag dismisses the keyboard and expands the readable area; the composer cannot scroll behind the keyboard. | R2 |
| CMP-006 | Image and text drafts survive navigation/reload and clear only after provider acceptance or manual deletion. A failed/queued send can restore exact text and images. | R2 |
| SET-001 | Settings has distinct Notifications, Devices & delivery, Sessions, Appearance, Budgets & spawning, and Advanced sections instead of one long form. | R3 |
| SET-002 | Desktop uses a compact section rail and content pane. Mobile shows one section at a time behind a sticky section selector. Deep links and Back preserve the active section. | R3 |
| SET-003 | Every mutation paints feedback within 100ms, serializes rapid edits, ignores stale responses, rolls back on failure, and shows Saved/Error beside the changed control. | R3 |
| NTF-001 | Notifications exposes one global master switch and one row for every canonical kind. Each row has an independent in-app visibility switch plus its Web Push rule; no device-level cadence override remains active. | R3 |
| NTF-002 | Each kind supports Off, once, once plus one reminder, and bounded repeat-until-resolved with editable initial delay, interval, maximum count, minimum severity, and quiet-hours behavior. | R3 |
| NTF-003 | Scheduler decisions are durable and restart-safe. Policy changes suppress obsolete queued jobs and cannot replay already-delivered events. | R3 |
| NTF-004 | Session mute and event snooze outrank global rules. Quiet hours hold rather than discard delivery unless that kind explicitly bypasses quiet hours. | R3 |
| NTF-005 | Settings shows an exact human-readable schedule and mini timeline for each rule, plus the next due push and recent delivery outcome. | R3 |
| NTF-006 | Transport retries remain separate from user cadence. A 429/timeout retry does not consume another user-visible notification count. | R3 |
| NTF-007 | Enabling a previously Off kind does not silently blast existing active events. Settings previews the active count and requires an explicit apply-to-current choice. | R3 |
| NTF-008 | Minimal encrypted payloads, capability authentication, device qualification, secret redaction, and Notification Center lifecycle invariants remain intact. | R3 |
| PERF-001 | Provider scan and `/api/fleet` p95 do not regress by more than 5ms; provider reconnection, policy scheduling, and settings writes do no network work under `scan_lock`. | All |
| DOC-001 | README, CLAUDE invariants, platform roadmap, notification roadmap, session organization docs, and production/staging runbooks agree with the shipped behavior. | All |

## Implementation evidence · 2026-07-17

| Gate | Status | Evidence |
| --- | --- | --- |
| Source and deterministic backend | Pass | 265 Python tests: Claude official-attach transport, Codex lifecycle fixtures, private/idempotent Outbox recovery, policy migration/cadence/DST/revision/worker/privacy, policy-independent test delivery, device removal, and server validation. |
| Web Push and service worker | Pass | 11 Node tests: minimal payload, capability fallback/replay behavior, SSRF/DNS/TLS confinement, retry mapping, and shell-cache privacy. |
| Desktop browser | Pass | 67 applicable checks at 1440×1000; seven device-specific or opt-in live cases skip. |
| Mobile browser | Pass | 68 applicable checks at 390×844; six opt-in live cases skip. Canonical composer, touch/viewport bounds, offline images, Settings focus safety, multi-device controls, cadence, pins, and PWA recovery are included. |
| Interaction latency inventory | Pass | Complete named desktop and mobile inventories, including first-feedback and local-completion budgets. |
| Screenshot inspection | Pass with fix | Desktop/mobile composer and policy screenshots inspected; the pass found and fixed a cadence-strip selector collision, then reran the affected checks. |
| In-app Browser visual control | Unavailable | The native Browser runtime reported no available backend. No alternate browser-control surface was substituted. |
| Isolated staging smoke | Pass | Staging restarted from this checkout; `/api/fleet` reported `mode=staging`, the expected source root, both providers healthy, one idle controllable staging-owned Codex session, and 27 observed production sessions read-only. Authenticated Outbox and notification-policy reads passed. The current desktop task exposed no in-app browser backend, so the real-phone interaction gate remains explicit. |
| Staging provider control | Pending release gate | Run exact staging-owned Claude foreground/background noop/message and Codex disconnect/reconnect recovery probes. |
| Installed iPhone/macOS | Pending release gate | Verify real iOS keyboard/photo-picker geometry and one global policy across installed mobile/desktop apps, including quiet hours/repeat/mute. |
| Production | Blocked by normal release flow | Draft PR → base merged into branch conflict-free → user marks ready/merges → deploy exact `origin/main` only. |

## R1 — Restore provider control and durable send recovery

### 1. Claude transport boundary

Introduce a provider-internal Claude transport interface with two implementations:

```text
validated live registry
        │
        ├─ normal foreground session ─▶ ItermAppletTransport
        │                                 verified key sequencing unchanged
        │
        └─ kind:bg session ────────────▶ ClaudeBackgroundTransport
                                          official claude attach/stop client
```

The disposable-session probe established that Claude Code already exposes the supported transport:
`claude attach <eight-hex-job-id>`, Ctrl-Z detach, and `claude stop <job-id>`. Fleet therefore does
not read the daemon roster or reverse-engineer its authenticated private socket.

The implementation then:

- Accepts a background route only from a server-owned live `kind:bg` registry row and validates the
  derived job id as exactly eight hexadecimal characters.
- Resolves one executable without a shell, launches fixed argv in a private bounded PTY, detects only
  alternate-screen readiness markers, and never scrapes session content.
- Sends only the server-composed key/text operations already allowed by `Engine.act`; clients cannot
  name a job, PTY, command, or arbitrary frame.
- Serializes actions per job, bounds input/output/time, detaches once, and reports a redacted
  `background_connection_lost` failure without reloading the session.
- Uses fixed-argv `claude stop <job>` for close. The iTerm path remains foreground-only.

Prompt-answer freshness, waiting/busy gates, raw CR behavior, slash-command protection, image path
whitelisting, and interrupt semantics remain above the transport boundary and therefore apply to
both transports.

### 2. Codex lifecycle authority

Split observed state from control state:

```text
rollout/thread-read evidence ─▶ observed activity ─▶ placement/status only

App Server connection + notifications
        └────────────────────▶ control epoch + live turn ID ─▶ steer/interrupt
```

- Delete the path that copies `_latest_turn_lifecycle(...).turn_id` into `client.thread_state`.
- Track a monotonically increasing App Server connection epoch. A live turn ID is valid only in the
  epoch that delivered its lifecycle notification.
- Clear control authority immediately on WebSocket loss while retaining cached thread state and
  conversation evidence.
- Reconnect with bounded exponential backoff and jitter. Refresh the server's loaded-thread view;
  never call `thread/resume` to recover an uncertain active turn.
- Project separate fields such as `observed_running`, `control_state`, `queue_accepting`, and
  `control_error`. UI capabilities derive from control state, not activity state.

### 3. Durable provider-recovery queue

Extend the existing server Outbox rather than creating a second invisible queue. Add an origin and
trigger for direct sends waiting on provider control:

```text
origin: direct_send_recovery
trigger: provider_reconnect
state: waiting_provider | sending | sent | failed | cancelled
idempotency_key: browser-generated, server-unique
```

The `/api/act` send path atomically either dispatches or creates this record. A control-unavailable
error is queueable; a permission denial, view-only thread, invalid command, closed session, provider
limit, or known semantic rejection is not.

Queued images move from the temporary upload into a private, queue-owned server path with mode 0600.
The database stores only the server-generated image reference. Cancellation, successful dispatch,
and retention cleanup delete the file. No client path is accepted.

Dispatch rules are strict:

1. Same Fleet-owned thread and notification-authoritative active turn: steer once.
2. Same Fleet-owned thread, authoritatively idle/completed: start one next turn.
3. Disconnected, not loaded, or lifecycle ambiguous: remain `waiting_provider`.
4. View-only/external ownership, closed thread, limit, or terminal rejection: fail visibly without
   retargeting, resuming, or duplicating.
5. Unknown delivery after the request left Fleet remains `confirmation_unknown`; never auto-retry.

The optimistic chat row and Outbox row share one durable ID. Confirmation still requires canonical
conversation evidence; an unrelated context revision is not confirmation.

### R1 verification gates

- Unit tests for job-id validation, fixed argv, PTY readiness, input/output/time bounds, detach,
  secret redaction, stop, and foreground/background routing.
- A disposable Claude background session: full daemon → `/api/act` `noop` → background transport;
  then a user-driven harmless message and one prompt answer.
- Codex protocol fixtures for disconnect during active turn, disconnect before send, reconnect to
  same turn, turn completion before reconnect, daemon restart, App Server restart, image queue,
  duplicate HTTP retry, limit error, and ambiguous delivery.
- Browser tests for cached conversation, Connection lost banner, durable queued receipt, Outbox
  visibility, cancel/restore, and exact-once canonical confirmation.
- Staging soak with forced provider disconnects. Production is not promoted until the exact live
  provider probes succeed; advertised capabilities alone are not evidence.

## R2 — Canonical composer and iOS repair

### 1. One component

Replace viewer-specific markup with one `renderComposer`/state path used by full chat and Markdown.
The renderer owns:

- textarea and auto-resize behavior;
- persistent text and image draft keys;
- `+` menu with Schedule message and Send picture;
- Send button and modified-Return handling;
- image-draft chips and removal;
- optimistic/queued/error feedback;
- focus, keyboard, viewport, and menu-close behavior.

Surfaces pass only a small slot configuration. Full chat has the normal action row. Markdown adds a
secondary Chat action without forking the textarea, `+`, or Send components.

```text
Full chat                           Markdown viewer
┌──────────────────────┬───┬────┐  ┌──────────────────────┬──────┐
│ send message         │ + │Send│  │ send message         │ Chat │
│                      │   │    │  │                      ├──────┤
└──────────────────────┴───┴────┘  │ + attachment menu    │ Send │
 image drafts                      └──────────────────────┴──────┘
                                    image drafts
```

The viewer no longer renders or fetches a second embedded conversation. Chat preserves the current
draft, closes the Markdown viewer, opens the canonical full chat, and lands at the bottom.

### 2. Photo-picker sequence

The native picker must be opened by a button handler rather than a label's implicit file-input
activation:

1. Close the `+` tray.
2. Blur the active textarea.
3. Clear composer-focused CSS and synchronize the visual viewport.
4. Wait one animation frame so iOS commits the keyboard dismissal.
5. Click the hidden file input from the same user activation.
6. Persist accepted blobs in IndexedDB and render image drafts.
7. Do not focus the textarea after selection or cancellation.

If iOS requires synchronous picker activation on a tested version, use a visually hidden file input
covered by the explicit button while performing blur on `pointerdown`; do not reintroduce the
implicit label path.

### 3. Keyboard and overlay geometry

- Add an opaque fixed keyboard curtain above the application shell and below active overlays whenever
  `html.keyboard-open` is set. It covers the layout viewport, not only the visual viewport.
- Keep chat/Markdown overlays fully opaque and anchored to the visual viewport for their usable
  height. No Now content is allowed to show through the keyboard or its accessory region.
- Make the conversation body the flexible scroll owner. The composer/action region is content-sized,
  never a nested scrolling container.
- When the keyboard is open, suppress safe-area bottom padding and use one explicit 4px composer
  margin. Restore safe-area padding when the keyboard closes.
- A deliberate vertical drag on conversation history blurs the textarea and resynchronizes geometry.
  Taps inside the composer, text selection, and attachment interaction do not dismiss it.
- Keep 44px minimum touch targets, prevent horizontal overflow, and honor reduced motion.

The iOS previous/next/Done keyboard accessory is controlled by iOS/Safari and cannot be reliably
removed by web code. This release must not claim otherwise. The Fleet background, spacing, and
swipe-to-dismiss behavior are the repairable parts and are release gates.

### R2 verification gates

- Component-level browser assertions prove full chat and Markdown use the same classes, handlers,
  draft keys, `+` tray, image-draft renderer, and send path.
- No `viewerChatOpen`, conversation toggle, or viewer-only schedule button remains.
- iPhone viewport screenshots cover keyboard closed/open, attachment menu, selected image, Markdown,
  full chat, orientation change, and scroll-to-dismiss.
- A real iPhone test verifies the native keyboard/picker sequence and absence of background bleed;
  desktop emulation alone is insufficient.
- Offline image selection, reload, reconnect, upload, provider send, confirmation, removal, and
  cleanup remain exact-once.

## R3 — Notification policy and organized Settings

### 1. Settings information architecture

Settings becomes a small application within the existing overlay instead of one long template
string.

```text
Desktop
┌─────────────────────────────────────────────────────────────┐
│ Settings                                                ×   │
├─────────────────┬───────────────────────────────────────────┤
│ Notifications   │ section title + concise status            │
│ Devices         │                                           │
│ Sessions        │ section content                           │
│ Appearance      │                                           │
│ Budgets         │ immediate row-level save feedback          │
│ Advanced        │                                           │
└─────────────────┴───────────────────────────────────────────┘

Mobile
┌────────────────────────────┐
│ Settings                ×  │
│ [ Notifications       ▾ ]  │  sticky section selector
├────────────────────────────┤
│ one section only           │
│ full-width controls        │
└────────────────────────────┘
```

Sections:

- **Notifications** — master state, quiet hours, event rules, next scheduled push, and link to
  notification delivery history.
- **Devices & delivery** — install/PWA state, permission, registered devices, health, pause, rename,
  reconnect, remove, and test. Policy controls do not appear here.
- **Sessions** — stalled threshold and a searchable list of muted sessions with manual Unmute.
- **Appearance** — desktop navigation side, reading width, session peek, and subagent peek.
- **Budgets & spawning** — existing budget and spawn limits without a collapsed mystery section.
- **Advanced** — manual-only legacy ntfy and diagnostics with explicit danger/legacy copy.

Use exact routes such as `#settings/notifications` and `#settings/devices`; Back closes a nested
section before closing Settings. Opening Settings paints the shell and selected section immediately,
then loads network-backed health/policy data with the existing spinner.

### 2. Visual direction

Keep Fleet's compact operations-console language rather than introducing a second design system:

- background `#0d1117`, surface `#161b22`, raised surface `#1c2330`, line `#2d333b`;
- text `#e6edf3`, secondary `#8b949e`;
- blue `#58a6ff` for selected/editable state, green `#3fb950` for healthy/saved, amber `#d29922`
  for aggressive cadence warnings, red `#f85149` for delivery failure or destructive disconnect;
- system sans for labels and prose; SF Mono/Menlo for times, counts, cadence, and delivery evidence.

The signature element is a live cadence strip, not decoration. Every collapsed event row explains
the real schedule at a glance:

```text
Questions and forms          On
Now ───────── 15m                         2 deliveries maximum

Completions                  On
5m ── 30m ── 30m ── 30m                 stop after 4 or resolution
```

Expanding a row reveals only the controls needed by its selected mode. Off shows no disabled timing
fields. Once shows initial delay. Once + reminder adds interval. Repeat adds interval and maximum.
The summary updates before the save finishes; a small Saving/Saved/Error state appears in that row.

### 3. Global notification policy model

Store the authoritative policy beside notification events and deliveries in the operations SQLite
database so scheduling and policy mutation share one short transaction.

```text
notification_global_policy
  singleton_id
  enabled
  quiet_hours_enabled
  quiet_start_minute
  quiet_end_minute
  timezone
  revision
  updated_at

notification_kind_policy
  kind PRIMARY KEY
  in_app_enabled               independent Notification Center visibility
  mode                         off | once | remind_once | repeat
  minimum_severity             info | warning | critical
  initial_delay_seconds
  repeat_interval_seconds
  max_deliveries
  allow_during_quiet_hours
  revision
  updated_at
```

`notification_devices.preferences_json` is migrated to the one global policy once, then retired from
scheduler decisions. The migration uses the current production policy as the default and does not
broaden push delivery:

| Kind | Default external cadence |
| --- | --- |
| Question, approval, form, reply | Once + one reminder; initial now; reminder after 15m |
| Confirmed provider/delivery failure | Once + one reminder; initial now; reminder after 15m |
| Stall | Once when the configured stall threshold is crossed |
| Completion, artifact, outcome, budget, measurement, Fleet notice | Off |

Every row is present even when Off. Devices keep only connection, enable/pause, permission,
qualification, health, and read-cursor state.

### 4. Scheduling semantics

- **Off:** no external delivery. The event appears in Notification Center only when that kind's
  independent in-app switch is enabled.
- **Once:** one successful user-visible delivery after the initial delay.
- **Once + reminder:** initial delivery plus one reminder after the configured interval if still
  active.
- **Repeat:** initial delivery, then one delivery per interval until resolution, snooze, mute, master
  disable, policy maximum, or event revision replacement.
- Maximum count means successful user-visible deliveries, including the initial delivery. Transport
  retries for the same delivery generation do not increment it.
- Allowed ranges: initial delay 0–24h; interval 1m–7d; maximum 1–100. A policy exceeding 12 possible
  pushes per day shows an amber worst-case warning and requires explicit confirmation.
- Quiet hours use the configured Fleet timezone and handle overnight ranges and DST. Held events
  coalesce by event ID and deliver at most once when quiet hours end; they do not replay every missed
  interval.
- Per-kind **Allow during quiet hours** bypasses only quiet hours. It does not bypass mute, snooze,
  device disable, revoked permission, or failed qualification.
- Precedence is: global master Off → device unavailable/paused → session mute → event snooze → quiet
  hours → event-kind rule → durable delivery/retry state.
- Policy mutation increments a revision. Every schedule, claim, retry, and wake revalidates that
  revision. Obsolete queued/retrying rows become `suppressed` with a bounded policy reason.
- Enabling a kind with existing active events first shows `N active events match`. Default Apply
  affects new/revised events only. An explicit checkbox may schedule current matches using the new
  initial delay; it never backfills resolved history.
- Snooze expiry produces at most one wake delivery and resumes the remaining cadence budget from the
  wake time. It never replays missed quiet/snoozed intervals.

### 5. APIs and feedback

Add token-gated endpoints or methods for:

- read global policy and per-kind rows;
- patch one global field or one kind atomically with expected revision;
- preview active-event impact before enabling;
- list enabled/paused devices and health without secret subscription material;
- list next due deliveries and recent bounded outcomes.

The client sends allowlisted enums and bounded numbers only. Concurrent edits use expected revisions;
a stale save refetches the exact row and explains the conflict instead of overwriting it.

The Notifications section header reads like an operational answer:

```text
Push on · 2 devices · 6 of 12 event rules enabled
Next: Reply request in 11m · Quiet hours off
```

This is not a second Notification Center. **View delivery history** deep-links to the existing
Notifications Problems/History surface. Settings configures policy; Notifications shows events and
delivery evidence.

### R3 verification gates

- Migration tests from current M12 schema and from partially migrated/restarted databases.
- Exhaustive mode tests for all notification kinds and severity boundaries.
- Multi-device tests prove one global policy applies equally while device pause/health remains local.
- Quiet hours tests cover same-day, overnight, timezone change, spring-forward, fall-back, held-event
  coalescing, snooze overlap, and mute overlap.
- Policy edits test current-event preview, no-blast default, explicit apply-to-current, revision
  conflict, obsolete-job suppression, daemon restart, worker retry, and delivery lease recovery.
- Browser tests at desktop and 390×844 cover every Settings section, deep link, Back behavior,
  collapsed/expanded rule rows, validation, warning confirmation, optimistic save, rollback, empty
  device state, denied permission, and failed worker.
- Real macOS and installed iPhone tests prove global rule changes affect both devices, quiet hours
  hold delivery, repeat count stops exactly, session mute stops all device pushes, and manual unmute
  restores eligibility.
- Notification payload/privacy probes prove no new policy or delivery API exposes endpoint, keys,
  auth tokens, transcript content, paths, or full roster data.

## Release and branch sequence

R1, R2, and R3 were implemented together on `fix/provider-control-recovery`, branched cleanly from
`origin/main` at `f9209ff`, because the user explicitly prioritized one urgent recovery release.
They remain separate gate groups below. No phase is presented as production-verified until its named
staging/live evidence passes.

### R1 — `fix/provider-control-recovery`

1. Reproduce and capture disposable Claude background protocol evidence.
2. Implement Claude transport boundary and correct the false tty fallback.
3. Separate Codex observation from lifecycle authority.
4. Add durable direct-send recovery through the Outbox, including queued images.
5. Run unit, protocol, browser, restart, privacy, and latency suites.
6. Deploy to staging and complete user-driven Claude/Codex live tests.
7. Open a draft PR. Merge current base into the branch conflict-free before asking whether to mark it
   ready. The user merges. Promote the exact merged `origin/main` with the production deploy script.
8. Run production `noop`, send, reconnect, and provider-health probes before declaring restored.

### R2 — canonical mobile composer gate

1. Extract the shared composer and migrate full chat without visual regression.
2. Migrate Markdown, remove embedded conversation, and add Chat.
3. Repair picker sequencing, keyboard curtain, scroll ownership, and safe-area spacing.
4. Run deterministic browser and offline/image tests.
5. Deploy to staging and complete the real-iPhone keyboard/photo test with the user.
6. Include the gate in the same draft PR; user merge, exact-main production promotion, and live
   smoke rules are unchanged.

### R3 — global notification controls gate

1. Add the policy schema/migration and policy APIs.
2. Generalize scheduling to every kind and full cadence while preserving current defaults.
3. Build the Settings shell and migrate existing controls into the new information architecture.
4. Build Notifications rules, cadence strips, quiet hours, preview, and delivery evidence.
5. Run migration, scheduler, worker, browser, privacy, latency, and real-device suites.
6. Deploy to staging and complete macOS+iPhone cadence tests.
7. Include the gate in the same draft PR; user merge, exact-main production promotion, and live
   smoke rules are unchanged.

## Files expected to change

- `engine.py` — Claude transport selection, structured provider-control errors, settings projection,
  Outbox dispatch/recovery integration.
- `claude_background.py` — bounded official attach/detach/stop transport extracted from `engine.py`.
- `codex_adapter.py` — connection epoch, lifecycle authority, reconnect state, structured queueable
  failures, and safe next-turn/steer dispatch.
- `outbox.py` — provider-reconnect trigger, idempotency, queue-owned images, confirmation-unknown,
  cancellation, cleanup, and audit fields.
- `briefing.py` — global notification policy schema, migration, scheduler, revision validation,
  quiet hours, cadence accounting, and diagnostics.
- `server.py` — bounded policy/settings endpoints and structured action results.
- `static/app.js` — canonical composer, provider connection/queue receipts, Settings router, policy
  editor, cadence preview, and save feedback.
- `static/fleet.css` — keyboard curtain, canonical composer geometry, organized Settings layout, rule
  rows, cadence strip, responsive and reduced-motion states.
- `dashboard.html` — Settings section shell/anchors only where semantic markup belongs outside JS.
- `sw.js` — only if notification-action or subscription behavior changes; private policy APIs remain
  network-only and uncached.
- `tests/test_engine_providers.py`, Codex protocol/fixture tests, Outbox tests, Briefing/Web Push tests,
  server tests, browser fixtures/specs, privacy probes, live notification tests, and latency tests.
- `README.md`, `CLAUDE.md`, `docs/roadmaps/platform-roadmap.md`,
  `docs/roadmaps/push-notification-redesign-roadmap.md`, and relevant organization/latency docs.

## Superseded documentation decisions

Implementation of R3 supersedes the fixed policy in the earlier notification roadmap and platform
ledger that says only urgent kinds may push and one 15-minute reminder is the maximum. Until R3 is
actually shipped, the current hard-coded policy remains the production truth. The earlier documents
must be updated in the same R3 commit; this roadmap alone does not change runtime behavior.

## Definition of done

This roadmap is complete only when:

1. A live Claude background session and a Fleet-owned Codex session both accept messages from
   production, including a tested disconnect/reconnect case.
2. No transcript-derived Codex turn ID grants control and no Claude background session is routed to
   iTerm by assumption.
3. Queued text/images survive relevant restarts, dispatch exactly once when safe, and remain visible
   when not safe.
4. Full chat and Markdown demonstrably share one composer; the real iPhone picker/keyboard flow is
   clean and the underlying app never shows through.
5. Settings is sectioned, responsive, keyboard-accessible, and immediate to interact with.
6. Every notification kind exposes the chosen global cadence model, quiet hours work, devices share
   the policy, and the scheduler obeys exact counts across restart.
7. All affected docs describe the source and production behavior actually verified—not the intended
   behavior or a merged PR title.
