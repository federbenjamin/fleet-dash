# Fleet Notification Center and Web Push roadmap

Status: Approved for implementation · 2026-07-16

Branch: `design/push-notification-redesign`

Base: `dc43a53` (`main`, 2026-07-16)

Scope: Fleet-owned notifications, installable mobile/desktop PWA, standards-based Web Push,
per-device health, and reversible notification actions.

This document is the implementation contract for replacing Fleet's transport-centered ntfy alerts
with a durable Notification Center and direct Web Push. A milestone is complete only when its data
model, failure paths, security boundary, responsive UI, restart behavior, and real-device delivery
gate are verified.

## Goal

Fleet owns the notification and its lifecycle. Web Push only delivers an encrypted, minimal pointer
to that event. The operating system may display, delay, coalesce, or discard a delivery, but it never
becomes the source of truth for whether Fleet still needs the user.

```text
Claude/Codex/operations event
             │
             ▼
    canonical notification event ───────────────▶ Fleet Notification Center
             │                                      active/resolved/snoozed/history
             ▼
       per-device policy
             │
             ▼
      persistent delivery job
             │
             ▼
 Apple/Google/Mozilla/Microsoft push service
             │
             ▼
       installed Fleet PWA
             │
       Open · Snooze · Mute
```

## Settled product decisions

1. Fleet is an installable PWA on mobile and desktop. Standards-based Web Push is the primary and
   only automatic external delivery path.
2. ntfy is retained only as a manually enabled legacy integration. It is never an automatic
   fallback and never receives duplicate deliveries from the Web Push path.
3. Lock-screen content is minimal by default. It never includes prompt text, question options,
   commands, tools, file paths, branch names, costs, account identities, or transcript excerpts.
4. A notification may directly **Snooze** its event or **Mute** its session. Every consequential
   action—answer, approve, deny, send, interrupt, or execute—opens Fleet for current-state review and
   confirmation.
5. Push-capable events are Needs-you requests, approvals, confirmed provider/delivery failures, and
   prolonged stalls. Completions, spend, fleet quiet, artifacts, routine repository outcomes, and
   informational budget updates stay in Notification Center without interrupting the user.
6. An unresolved pushed event receives at most one automatic reminder 15 minutes after its first
   successful delivery. A snooze replaces the reminder schedule; state revision creates a new event.
7. Snooze applies to one event. The push shortcut snoozes for 15 minutes; Fleet also offers one hour
   and tomorrow. If still active at expiry, the event may deliver once and does not restart an
   unlimited reminder cycle.
8. Mute applies to the whole session, across devices, until manually unmuted. Muted events remain
   visible in Fleet and continue to affect Needs-you counts; only external push is suppressed.
9. Read state and device delivery preferences are per device. Snooze, mute, active/resolved state,
   and notification identity are global.
10. Notification Center absorbs Fleet Briefing's durable event history. Briefing becomes a summary
    view inside Notifications. Now keeps its live Action Inbox and no longer hosts a competing
    Briefing block.
11. No automatic periodic reminders remain. The existing five-minute stall and 30-minute waiting
    buckets are removed.
12. No implementation may place Fleet's reusable `act_token`, a VAPID private key, a subscription
    endpoint, or full event content in a notification URL, browser-visible API response, fleet
    snapshot, log, or diagnostic payload.

## Current behavior being replaced

The existing path lives in `Engine.check_notifications` and `Engine.ntfy`:

- Five hard-coded categories dispatch from the two-second scan: stalled, waiting, spend, fleet
  quiet, and scheduled daily digest.
- A per-session dedupe key uses only the first eight session-ID characters. Provider-prefixed Codex
  IDs can collide.
- Waiting duration is transcript file age, not time since the request opened.
- The same waiting event repeats every 30 minutes and the same stall repeats every five minutes.
- Every push receives one global optional Click URL instead of an event-specific route.
- Delivery is at-most-once. A failed or abandoned `queued` row can never be reclaimed.
- A missing ntfy topic records `disabled`, while Settings still presents enabled category toggles.
- Successful/disabled delivery history has no user-facing surface; failures are injected into
  Briefing indefinitely and cannot be retried or cleared.
- Completion, repository, artifact, outbox, provider, and measurement events already exist in the
  durable briefing store but are not part of one canonical notification lifecycle.

The redesign keeps the useful restart-safe event and review concepts from `briefing.py`; it does not
preserve the broken transport keys, repeat buckets, delivery schema, or global tap target.

## Product contract

1. The Notifications badge and center represent canonical Fleet events, never push-delivery counts.
2. An event has one stable identity and an explicit lifecycle: `active`, `snoozed`, `resolved`, or
   `expired`. Delivery state cannot resolve or reopen the event.
3. A push tap opens the exact event. If the event resolved before the tap, Fleet shows `Resolved`
   and current session context instead of stale controls.
4. Notification Center and all subscription/settings actions paint feedback within 100 ms p95.
5. Provider scans never perform network delivery. They persist events/jobs; a bounded worker sends
   them outside `scan_lock` and outside the HTTP request path.
6. Delivery survives daemon restart. Retry decisions are durable and idempotent per event/device/
   delivery generation.
7. Fleet remains fully usable when Node, Web Push, a browser subscription, Tailscale HTTPS, or a
   vendor push service is unavailable. The failure is scoped to notification delivery and visible.
8. Web Push setup is honest: unsupported browser, insecure origin, missing Home Screen install,
   denied permission, expired subscription, missing Node helper, and last-delivery failure are
   distinct states.
9. The service worker caches only versioned application shell assets. API, transcript, notification,
   and session responses are network-only and never stored in Cache Storage.
10. Real-device delivery is a release gate, not inferred from a successful HTTP response from a fake
    endpoint.

## Canonical data model

### `notification_events`

```text
id TEXT PRIMARY KEY                 opaque random identifier used by deep links
event_key TEXT UNIQUE NOT NULL      semantic provider/source identity
kind TEXT NOT NULL                  question, approval, failure, stall, completion, ...
state TEXT NOT NULL                 active, snoozed, resolved, expired
severity TEXT NOT NULL              critical, action, watch, info
title TEXT NOT NULL                 full in-app title
summary TEXT NOT NULL               full in-app summary, bounded
provider TEXT
session_id TEXT
workstream_id TEXT
source_type TEXT NOT NULL
source_id TEXT
source_revision TEXT NOT NULL
opened_at REAL NOT NULL             first observation of this exact event
changed_at REAL NOT NULL
resolved_at REAL
snoozed_until REAL
reminder_budget INTEGER NOT NULL    defaults to 1 for externally pushed active events
last_push_at REAL
payload_json TEXT NOT NULL          bounded event-specific details
```

Event keys use full semantic identity:

- Question/form/permission/approval: provider + full session ID + native nonce/request ID.
- Reply request: provider + full session ID + assistant-reply revision.
- Stall: provider + full session ID + active-turn revision.
- Provider/delivery failure: source + stable source ID + failure revision.
- Completion/outcome/artifact/budget: existing durable source event key.

`opened_at` is when Fleet first observes this exact actionable state. It is never derived from
transcript mtime or session quiet time.

### `notification_devices`

```text
id TEXT PRIMARY KEY                 random browser installation identity
display_name TEXT NOT NULL          editable, e.g. Ben's iPhone
platform TEXT                       feature-detected hint, not authorization evidence
subscription_json TEXT NOT NULL     endpoint + p256dh + auth, never returned after write
endpoint_origin TEXT NOT NULL
enabled INTEGER NOT NULL
permission_state TEXT NOT NULL      granted, denied, prompt, expired, unsupported
created_at REAL NOT NULL
last_registered_at REAL NOT NULL
last_success_at REAL
last_failure_at REAL
last_failure TEXT
read_cursor INTEGER NOT NULL
preferences_json TEXT NOT NULL      severity/kind switches; no arbitrary rule code
```

Subscription material is sensitive. SQLite and config files retain user-only permissions; APIs
project only device ID/name/health/timestamps/preferences.

### `notification_deliveries` v2

```text
id TEXT PRIMARY KEY
event_id TEXT NOT NULL
device_id TEXT NOT NULL
generation INTEGER NOT NULL
status TEXT NOT NULL                 queued, sending, retrying, sent, failed,
                                     suppressed, subscription_expired
attempt INTEGER NOT NULL
next_attempt_at REAL
remote_status INTEGER
remote_id TEXT
error TEXT
created_at REAL NOT NULL
updated_at REAL NOT NULL
UNIQUE(event_id, device_id, generation)
```

The migration builds a v2 table transactionally. Existing ntfy rows are imported as legacy history
and never redispatched. Current active requests are inserted into Notification Center on first scan,
but a newly registered device starts at the current event cursor so setup cannot blast old work.

## Event selection and reminder policy

| Event kind | Initial push | Reminder | Notification Center |
| --- | --- | --- | --- |
| Permission/command/file approval or MCP form | immediately after confirmed active state | once after 15m | Needs action |
| Structured question or explicit reply request | immediately after confirmed active state | once after 15m | Needs action |
| Provider or outgoing-delivery failure | immediately after confirmed failure | once after 15m if still current | Problems |
| Prolonged stall | once when the authoritative stall threshold is crossed | none | Needs action/Slow |
| Completion, artifact, successful repository outcome | never | none | Updates |
| Spend, informational budget state, fleet quiet | never | none | Updates/Briefing |
| Scheduled digest | removed | none | Briefing is available on demand |

Per-device preferences may disable an allowed kind or increase its initial delay. They may not make
informational events urgent, create unlimited repeats, or enable direct consequential actions.

When a pushed event resolves, no `resolved` push is sent. On the next app open or service-worker
interaction, Fleet closes locally displayed notifications whose event IDs are no longer active.
Push-service `topic`/notification `tag` use a bounded hash of event ID so a reminder replaces the
existing card instead of stacking a duplicate.

## Minimal external payload

Default payloads disclose state, not work content:

```text
Needs action
Fleet needs a response · waiting 2m

Problem
Fleet detected a delivery failure

Slow work
One session has stopped showing progress
```

The encrypted payload contains:

```json
{
  "version": 1,
  "event_id": "opaque-id",
  "kind": "question",
  "title": "Fleet needs you",
  "body": "Fleet needs a response · waiting 2m",
  "tag": "bounded-event-hash",
  "url": "/#notifications/opaque-id",
  "actions": ["snooze", "mute"],
  "capabilities": {"snooze": "...", "mute": "..."}
}
```

Project/session names remain off by default. A future explicit per-device privacy setting may add a
display name, but full question/command/tool/file/cost content is out of scope.

## PWA and service worker

### Installation

- Add `static/manifest.webmanifest`, maskable/regular icons, theme/background colors, stable manifest
  ID, `start_url`, and standalone display mode.
- Add `static/sw.js` at root service-worker scope and register it only in a secure context.
- iPhone/iPad instructions require Add to Home Screen and an in-app `Enable notifications` tap.
- Desktop exposes `Install Fleet` when the browser supports it and precise Safari Add to Dock help
  otherwise.
- Localhost remains valid for desktop development. Mobile setup requires the existing Tailscale
  HTTPS URL.

Apple documents standards-based Web Push for Home Screen web apps on iOS/iPadOS 16.4+ and Safari
16.1+ on macOS: <https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/>.

### Service-worker behavior

- `push`: validate version/shape/size, call `showNotification`, set badge from bounded unread count,
  and never fetch conversation content.
- `notificationclick` with no action: focus an existing Fleet client or open the exact event URL.
- `snooze`/`mute`: POST the matching one-use capability. On success close the notification. On
  network/Tailscale failure, keep it visible and open Fleet to the event with a recoverable intent.
- `notificationclose`: local dismissal only; it never marks the Fleet event read or resolved.
- `pushsubscriptionchange`: re-register when supported. Every foreground launch also compares the
  current browser subscription with Fleet and repairs drift.
- API requests, notifications, transcripts, and settings use network-only fetch behavior. Offline
  shell fallback says `Reconnect to your tailnet` without rendering cached private content.

Action buttons are progressive enhancement. On clients that do not render notification actions,
opening the exact event presents Snooze and Mute as the first controls.

## Web Push transport

Fleet's launchd daemon currently uses `/usr/bin/python3` 3.9. Current `pywebpush` requires Python
3.10+, so this milestone does not silently migrate the daemon runtime. It adds the established
`web-push` Node package as a production dependency and a supervised `web_push_worker.js` child.

- Python owns policy, persistence, retries, and device/event state.
- One long-lived Node child receives bounded newline-delimited JSON jobs and returns request-scoped
  result objects. No shell command is accepted or constructed.
- The helper performs VAPID signing, RFC-compliant payload encryption, and the HTTPS request.
- Python restarts a crashed helper with backoff. Missing Node/dependency marks Web Push unavailable
  without affecting scans or HTTP service.
- Node resolution is fixed/configured and may reuse Fleet's existing bounded NVM discovery pattern.
- `package.json` moves `web-push` into production `dependencies`; Playwright remains a dev dependency.

The VAPID keypair is generated once. The private key and action-capability secret live only in the
private Fleet config with mode 0600; only the public VAPID key is returned to an authenticated
device. The VAPID subject is the stable project HTTPS URL unless explicitly configured.

### Endpoint and SSRF boundary

Subscription endpoints are client input and therefore untrusted. Registration and delivery require:

- HTTPS endpoint, bounded URL/host/path lengths, no credentials/fragments, and valid `p256dh`/`auth`.
- Known push-service origin allowlist for Apple, Google, Mozilla, and Microsoft. Additional origins
  require explicit local configuration and a high-warning Settings flow.
- No redirects, proxies, local/private/link-local/loopback destinations, custom headers, or client-
  supplied VAPID fields.
- Bounded response reads and deadlines.

This prevents an authenticated browser from registering an arbitrary internal HTTP target and
turning the delivery worker into an SSRF client.

## Delivery reliability

The worker atomically claims due jobs with a lease. A daemon restart reclaims expired `sending`
leases. Retry uses durable attempts with jitter around 2 seconds, 10 seconds, 30 seconds, 2 minutes,
and 10 minutes.

- 2xx: `sent`, update device `last_success_at`.
- 404/410 subscription response: `subscription_expired`, disable that subscription, show reconnect.
- 408/429/5xx/network timeout: retry, honoring bounded `Retry-After` when present.
- Other 4xx: permanent `failed`; never loop.
- Exhausted retry budget: `failed` with manual Retry after device/event revalidation.

The delivery queue has a fixed worker count, batch size, per-host concurrency cap, and maximum
pending rows. Informational events may be coalesced; action events may never be silently evicted.
Diagnostics expose aggregate queue depth/age/status and per-device health, never endpoints, keys,
capabilities, payload bodies, or event summaries.

## Notification Center experience

### Navigation

- Desktop rail: add `Notifications` with an unread/active badge.
- Mobile bottom bar: `Now · Notifications · Search · Work · More`; History moves under More.
- Deep links use `#notifications/<opaque-event-id>` and participate in browser/native back.
- PWA icon badge represents unread Notification Center events for that device, not delivery jobs.

### Sections

1. **Needs action** — current questions, approvals, reply requests, and prolonged stalls.
2. **Updates** — completions, artifacts, outcomes, spend/budget information, and fleet summaries.
3. **Snoozed** — active events ordered by wake time with `Wake now`.
4. **Problems** — provider failures and push/device delivery health with Retry/Reconnect.
5. **Briefing** — an on-demand summary generated from the same canonical event stream.
6. **History** — resolved/expired events with provider, workstream, session, kind, and time filters.

Opening a row re-fetches current state. Needs-action rows reuse the existing question/approval/form
component. Resolved rows are read-only. Bulk actions stay limited to mark read, snooze, mute, unmute,
and dismiss informational history; bulk approval remains forbidden.

Now keeps its live Action Inbox. Removing the old Briefing block from Now intentionally supersedes
M11's `Briefing above Pinned` placement after Notification Center ships.

## Snooze, mute, read, and resolution

- **Snooze 15m** from a notification; **15m / 1h / tomorrow** in Fleet. Tomorrow uses the device's
  local timezone selection stored with the action, then persists an absolute UTC wake time.
- Snooze never changes provider state and never hides the event from an explicit `Show snoozed`
  query.
- At wake, revalidate the event. Resolved events close silently; active events enqueue one delivery.
- Mute is session-scoped and indefinite. It suppresses every per-session external delivery across
  devices but does not suppress provider/fleet health alerts unrelated to that session.
- Unmute is available from the session, Settings, Notification Center, and muted-event history.
- Read is per device. Opening a push does not mark read until Fleet successfully loads the event.
- Provider evidence resolves events. Delivery success, local dismissal, read state, and opening the
  screen never resolve an event.

## Action-capability security

Push actions never use `act_token`. Each delivery gets separate signed capabilities for exactly one
reversible action:

```text
version, event_id, device_id, action, expires_at, random jti
```

- HMAC signature uses a dedicated server secret.
- Ten-minute expiry; one use enforced by a durable consumed-JTI table.
- Snooze capability fixes the duration at 15 minutes.
- Mute capability fixes the exact session ID from the server-side event; the client cannot supply it.
- The server revalidates current event/device/session state before mutation.
- Tokens are sent only inside encrypted Web Push payload data and a POST body. They never appear in
  URLs, logs, error strings, delivery history, or browser page markup.
- Capability failure opens Fleet at the event with an inline retry path; it never falls through to
  the broader authenticated action API.

## HTTP contracts

All JSON routes retain the existing action-token authentication, body limits, timeouts, and scoped
error behavior unless the route is explicitly capability-authenticated.

```text
GET  /api/notifications?section=&cursor=&limit=&device=
POST /api/notifications/read       {device_id, cursor|event_ids}
POST /api/notifications/action     {event_id, action, duration?}

GET  /api/push/config              public VAPID key + feature/health projection
POST /api/push/subscription        create/update/remove current device subscription
POST /api/push/test                enqueue one minimal test event for current device
POST /api/push/capability-action   capability-authenticated snooze/mute only
GET  /api/push/devices             redacted registered-device list and health
POST /api/push/device-settings     name, enabled, kind/severity preferences
```

`/api/fleet` carries only aggregate notification counts and redacted current-device health needed by
navigation/settings. Notification pages, history, device lists, and delivery failures are paged on
demand.

## Implementation milestones

### N0 — Compatibility prototype and baseline

- Capture current notification trigger/delivery counts and scan/API latency.
- Add a disposable standalone manifest/service-worker prototype under test fixtures.
- Prove a real test push on one iPhone Home Screen install and one macOS install.
- Prove exact event deep-link opening, PWA badge update, app-closed delivery, and action-support
  fallback before altering production triggers.
- Validate the Node `web-push` helper against Apple and Chromium subscriptions and record the exact
  supported endpoint origins observed.

Exit: real-device proof exists; unsupported conditions are surfaced; `/api/fleet` and scan baselines
are recorded; no production push behavior changed.

### N1 — Canonical event and device stores

- Add the v2 event/device/delivery schema and transactional migration.
- Normalize existing Action Inbox, briefing, repository, outbox, budget, provider, and session-state
  evidence into stable event identities.
- Track true event-open time and authoritative resolution.
- Keep first-scan/new-device seed suppression.
- Add deterministic collision, restart, stale-event, resolution, mute, snooze, and migration tests.

Exit: Notification Center data can be queried deterministically; existing ntfy behavior still runs;
no event identity relies on truncated IDs, mtimes, or time buckets.

### N2 — Installable Fleet PWA and device registration

- Add manifest, icons, secure service worker, install guidance, permission flow, subscription repair,
  redacted device APIs, and per-device settings.
- Add clear unsupported/insecure/not-installed/prompt/denied/granted/expired states and test send.
- Add Network-only private data rules and offline tailnet guidance.

Exit: desktop/mobile deterministic flows pass; a real iPhone and macOS device can register, rename,
disable, reconnect, and receive a test event.

### N3 — Persistent Web Push delivery

- Add `web-push` dependency, supervised Node helper, VAPID/action key generation, endpoint allowlist,
  durable leasing/retry, device health, redacted diagnostics, coalescing, and queue bounds.
- Keep delivery completely outside scan and HTTP request locks.
- Add fake endpoint status matrix, helper crash/restart, daemon restart, 410 expiry, 429 Retry-After,
  timeout, hostile endpoint, malformed key, queue saturation, and secret-redaction tests.

Exit: delivery is at-least-attempted with bounded retry and idempotent job identity; failures are
recoverable and cannot affect core Fleet availability.

### N4 — Notification Center and surface consolidation

- Add desktop/mobile destination, badges, sections, pagination/filters, exact event detail, per-device
  read state, Snooze/Wake/Unmute/Retry/Reconnect, and responsive/accessible empty/error/loading states.
- Move Briefing history/summary into Notifications and remove the old Now Briefing block.
- Keep the live Action Inbox on Now and reuse its current action controls.
- Reconcile navigation, URL/back behavior, saved device identity, and PWA badge.

Exit: every canonical event is discoverable in exactly one durable inbox; Now and Notifications have
distinct roles; desktop 1440×1000 and mobile 390×844 pass.

### N5 — Production policy, deep links, and reversible push actions

- Switch production trigger selection to the settled table and remove periodic bucket repeats.
- Add one-reminder budget, snooze wake processing, mute-until-unmuted, minimal payload renderer,
  event-specific deep links, action capabilities, and progressive action fallback.
- Revalidate every event before enqueue, delivery, open, snooze, mute, retry, or reminder.
- Add lock-screen snapshot assertions that forbidden content never enters a payload.

Exit: stale pushes cannot expose stale controls; no event repeats beyond policy; Snooze/Mute work from
supported notification clients and remain first-class after Open everywhere else.

### N6 — ntfy retirement, observability, and release gate

- Disable ntfy by default and remove it from automatic dispatch. Preserve it behind a clearly labeled
  Legacy integration switch with no fallback/duplication semantics.
- Migrate Settings copy, README setup, CLAUDE.md invariants, platform roadmap, diagnostics, and
  notification failure history.
- Run full deterministic, live browser, restart, saturation, and real-device matrices.
- Measure scan/fleet/API/feedback performance and verify no private delivery material in logs, fleet
  JSON, browser storage, Cache Storage, screenshots, or errors.

Exit: Web Push is the only default transport; real iPhone/macOS app-closed delivery and exact deep
links pass; daemon restart loses no jobs; all docs and legacy migration behavior agree.

## Requirements catalogue

| ID | Acceptance condition | Owner |
| --- | --- | --- |
| NTF-001 | One canonical event identity uses full semantic source identity and true opened time; truncated IDs, transcript mtimes, and repeat buckets are absent. | N1 |
| NTF-002 | Event lifecycle is independent from delivery/read state and resolves only from current provider/operation evidence. | N1 |
| NTF-003 | Current Action Inbox, briefing, provider, repository, outbox, budget, and session events map without duplicate active notifications. | N1/N4 |
| PWA-001 | Fleet installs as a standalone PWA on supported mobile/desktop clients with stable identity, icons, theme, exact deep links, and badge. | N2 |
| PWA-002 | Setup distinguishes secure-context, install, permission, subscription, helper, and delivery health states and provides a real test push. | N2/N3 |
| PWA-003 | Service worker never caches API/private content and provides a bounded reconnect-to-tailnet offline shell. | N2 |
| DEV-001 | Device subscription material is write-only through authenticated APIs; client projections are redacted and device preferences/read cursor are independent. | N2 |
| PUSH-001 | VAPID-encrypted Web Push uses a supervised fixed helper, bounded endpoint allowlist, no redirects/private targets, and no shell composition. | N3 |
| PUSH-002 | Durable lease/retry handles restart, timeout, 429/5xx, permanent 4xx, expired subscriptions, saturation, and manual retry without duplicate job identity. | N3 |
| PUSH-003 | Provider scans and HTTP actions only persist work; no Web Push network request runs under `scan_lock` or blocks a request. | N3 |
| UI-NTF-001 | Notifications is a desktop/mobile destination with Needs action, Updates, Snoozed, Problems, Briefing, and History plus exact deep links/back behavior. | N4 |
| UI-NTF-002 | Now keeps live Action Inbox; Notification Center owns durable event history; the old Now Briefing block is removed. | N4 |
| UI-NTF-003 | Navigation and PWA badges reflect per-device unread canonical events, never delivery attempts. | N4 |
| ACT-NTF-001 | Push may only Open, Snooze the exact event, or Mute its exact session. All consequential actions open Fleet. | N5 |
| ACT-NTF-002 | Capabilities are one-use, ten-minute, event/device/action scoped, POST-only, secret-redacted, and revalidated. | N5 |
| PRIV-001 | Default external title/body reveal only generic state and elapsed time; forbidden prompt/tool/path/branch/cost/account content has deterministic negative tests. | N5 |
| POLICY-001 | Only settled actionable/failure/stall categories push; informational events remain in Fleet; one 15-minute reminder is the maximum. | N5 |
| POLICY-002 | Snooze replaces reminder timing; Mute is session-wide until manual unmute and never hides in-app state. | N5 |
| LEG-001 | ntfy is opt-in legacy only and is never an automatic fallback or duplicate destination. | N6 |
| QUAL-NTF-001 | Desktop/mobile deterministic tests, fake push endpoints, restart/saturation tests, real iPhone/macOS app-closed delivery, and latency/privacy gates pass. | N0–N6 |

## Verification matrix

### Deterministic

- Python unit tests for event identity/lifecycle, policy, migration, cursors, snooze/mute, capabilities,
  worker leasing/retry, device health, API validation, and redaction.
- Node tests for Web Push job validation, VAPID/encryption request construction, result mapping, size
  bounds, helper protocol corruption, and clean shutdown.
- Fake HTTPS push endpoints for every status/retry/timeout path; no external delivery in the normal
  test suite.
- Playwright desktop/mobile coverage for install/setup states, Notifications sections, badges, deep
  links, back navigation, exact current/resolved actions, Snooze/Mute/Unmute, retry/reconnect, denied
  permission, expired subscription, and service-worker action fallback.
- Generated payload snapshots assert the minimal disclosure allowlist and forbidden-content denylist.

### Live and real-device

- iPhone/iPad Home Screen install: permission from direct tap, app-closed delivery, Lock Screen,
  Notification Center, badge, exact open, Snooze, Mute, resolved-before-open, permission revocation,
  and subscription repair.
- macOS Safari/Add to Dock and one Chromium installation: app-closed/browser-state behavior, badge,
  exact open, action fallback, reconnect, and Focus/notification settings interaction.
- Daemon restart with queued, sending, retrying, snoozed, and expired-subscription rows.
- Tailnet disconnected at notification action/open, then restored without losing intent or event.
- Legacy ntfy explicitly enabled: one manual test delivery only; never automatic duplicate/fallback.

### Performance gates

- Notification event projection adds less than 5 ms to engine scan p95 on the existing live corpus.
- `/api/fleet` p95 and payload remain within the existing M11 contract; only aggregate counts/current-
  device health are added.
- Notification Center first feedback is under 100 ms p95; routine local pages/actions are under
  250 ms p95.
- Enqueue transaction is under 25 ms p95. Remote delivery latency is measured separately.
- Push worker saturation cannot delay scan, `/api/fleet`, context, or normal `/api/act` p95 by more
  than 5 ms.

## Migration and rollback

1. N1–N3 ship dark: canonical events and Web Push test devices exist while ntfy production triggers
   remain unchanged.
2. N4 exposes Notification Center using canonical events but still does not change external dispatch.
3. N5 enables Web Push policy only for devices that explicitly completed setup and test delivery.
4. N6 disables automatic ntfy dispatch. Existing ntfy configuration stays readable and can be
   manually re-enabled as Legacy; it is never selected automatically after Web Push failure.
5. Rollback disables Web Push enqueue/worker and restores the pre-switch ntfy dispatch flag without
   deleting event/device/delivery history or rewriting schema. No downgrade redispatches history.

## Explicit non-goals

- Native Swift, Kotlin, Windows, or Linux Fleet applications.
- Automatic ntfy fallback or simultaneous duplicate transports.
- Direct answer, approve, deny, send, interrupt, shell, repository, or file mutation from a push.
- Full prompt/question/command/tool/path/branch/cost/account content on the lock screen.
- Arbitrary user-authored rule code, scripts, webhooks, or unlimited escalation chains.
- Cross-user accounts, hosted multi-tenant push service, or delivery while the Fleet Mac is offline.
- Claiming Web Push delivery guarantees the operating system and push vendors do not provide.

## Implementation order

`N0 → N1 → N2 → N3 → N4 → N5 → N6`

Do not parallelize a dependent milestone before the previous milestone's source, tests, and evidence
are folded back into this document. Each milestone gets one focused append-only commit. Runtime
deployment follows the existing daemon restart and live-smoke workflow; real notification actions
must use purpose-created test events and never mutate a real provider session.
