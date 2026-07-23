# Fleet Dash platform roadmap

Status: M0–M12 production-complete. M13 and M14 are implemented with automated release gates passed;
isolated staging and phone validation remain on `fix/send-now-or-queue`. The durable source of truth is
[`adversarial-bug-scan-roadmap.md`](adversarial-bug-scan-roadmap.md).

## Implementation progress

| Milestone | Status | Current evidence |
| --- | --- | --- |
| M0 — Baseline and roadmap | Complete · 2026-07-16 | Roadmap catalogue, persistent external-thread observation, quick-response delivery, repeatable Python/browser baselines, and pushed commits through `b9bf0e0`. |
| M1 — App shell and shared components | Complete · 2026-07-16 | Semantic zero-build shell in `dashboard.html`, responsive/navigation/component rules in `static/fleet.css`, route/back/filter behavior in `static/app.js`, 82 Python tests, 40 deterministic Playwright desktop/mobile tests, and 2 running-daemon Playwright checks. |
| M2 — Incremental global search | Complete · 2026-07-16 | Isolated low-priority index process, per-source WAL/FTS5 state, authenticated search/status/context/rebuild APIs, desktop/mobile Search UI, exact session/subagent/artifact context, 88 Python tests, 44 deterministic browser tests, and the 100k-message/2k-source benchmark. Live while indexing 2.7 GB: `/api/fleet` p95 12.653 ms, search p95 26.619 ms, server search p95 24.515 ms. |
| M3 — Action inbox and Workstreams | Complete · 2026-07-16 | Stable provider-neutral action IDs, safe persistent bulk triage, canonical repo/worktree grouping on a lazy API, honest unavailable evidence, saved destination filters, 96 Python tests, 48 deterministic desktop/mobile browser tests, and 4 running-daemon browser checks. |
| M4 — State evidence | Complete · 2026-07-16 | Pure placement classifier, bounded evidence facts, durable transition journal/API, full-chat evidence rail, recent external-completion handling, and last-good Codex outage recovery. |
| M5 — Provider handoff | Complete · 2026-07-16 | Redacted indexed preview, exact provider-native identity, editable desktop/mobile UI, selectable artifacts and advanced controls, durable bidirectional links, safe retry, 108 Python tests, 56 deterministic browser checks, and 6 safe running-daemon checks. |
| M6 — Repository outcome center | Complete · 2026-07-16; in-app center retired · 2026-07-17 | Cached argv-only Git evidence and transcript-derived outcomes remain available to Workstreams. GitHub now opens externally; duplicated GitHub/Git/PR pages and action forms were removed from Fleet. |
| M7 — Message Outbox and light automations | Complete · 2026-07-16 | Durable SQLite outbox, four one-time send modes, atomic claim/lease recovery, exact target/account/session validation, central responsive UI, 139 Python tests, 70 deterministic browser checks, 10 safe running-daemon checks, and a real create/cancel smoke with no message dispatched. |
| M8 — Briefings, budgets, and forecasts | Complete · 2026-07-16 | Durable per-device briefings, reviewed history, source links, mute-aware quiet/scheduled digests, persistent notification failures, scoped cumulative budgets, optional fail-closed future-spawn limits, honest mixed-provider measurement, action-inbox alerts, 156 Python tests, 74 deterministic browser checks, and 12 live checks. |
| M9 — Optimization pass | Complete · 2026-07-16 | Paginated History/conversations, bounded diagnostics, stable Workstream caching, transactional search counts, a 165 KB live fleet response, 3.521 ms fleet API p95, and 91.734/58.278 ms desktop/mobile first-useful-render p95. |
| M10 — Bug-fix and resilience pass | Complete · 2026-07-16 | Strict request/config/outbox/budget validation, bounded HTTP failures, derived-database recovery, request-local ledger reads, Claude pre-transcript visibility, Codex propagation-race recovery, 173 Python tests, 82 deterministic browser checks, 12 live browser checks, and a clean 120-request concurrent refresh soak. |
| M11 — Dashboard UX and responsiveness | Complete · 2026-07-16 | Action-oriented Now filters, compact Usage, multiline composers, optimistic startup, Claude permission controls, worktree-safe close, adaptive status strips, stable pin order, stale-request cancellation, recoverable interaction feedback, 185 Python tests, 112 deterministic browser checks, 12 live browser checks, and live server-route p95 below 5 ms. |
| M12 — Notification Center and Web Push | Complete · N0–N6 · 2026-07-16/17 | Encrypted compatibility probe; canonical lifecycle migration; installable shell-only PWA; durable Web Push; responsive canonical Notification Center; installed macOS/iPhone app-closed delivery, badges, exact deep links, production policy, bounded reminders, Snooze/Mute capabilities, minimal lock-screen payloads, manual-only legacy ntfy, restart/saturation/privacy gates, and live p95 contracts. |
| M13 — Provider control, canonical composer, and global notification controls | Automated gates complete; staging phone gate pending · 2026-07-18 | Official Claude background attach/stop transport; connection-generation Codex authority; definitive stale-turn recovery; durable provider-reconnect text/image queue; one full-chat/Markdown composer; poll-stable resizable question drawer; section-routed Settings; global per-kind cadence and quiet-hours policy. The consolidated gate now includes 353 Python tests, 11 Node push/privacy tests, two clean complete desktop/mobile matrices, and four named latency inventories. Installed-device provider, notification, and keyboard checks remain release gates. |
| M14 — Adversarial delivery and control hardening | Automated gates complete; staging phone gate pending · 2026-07-18 | Exact-session send-now-or-queue, durable optimistic receipts/drafts/context, independent in-app/Web Push controls, provider mutation serialization/CAS, existing-chat model/effort controls, mobile/offline resilience, and F01–F57/B1–B15. No production promotion until isolated staging and the user-approved phone gate pass. |

Completion here records the milestone gate, not proof by assertion. M10 reopened the M0–M10
catalogue rows, verified the current implementation and tests, and recorded the source/runtime
limitations. M12 completion records its dedicated deterministic, live, real-device, privacy, and
latency evidence rather than treating roadmap status as proof.

This roadmap turns Fleet Dash from a session list into a local operations desk for supervising
Claude Code and Codex work. It preserves one shared application, provider-independent sessions,
every existing control, and the current safe-action boundaries.

## Settled decisions

- Global search indexes every local Claude and Codex transcript, including sessions Fleet did not
  create, saved subagents, delivered/generated artifacts, and session metadata. It does not crawl
  arbitrary repository files.
- Search uses an incremental on-disk index. Fleet startup, `/api/fleet`, and the two-second provider
  poll must never wait for a full transcript scan.
- Provider handoff opens an editable preview that can be accepted unchanged with one primary action.
- Git/GitHub supports status, commit, push, draft-PR creation, and an explicitly confirmed
  ready-for-review action. Fleet never merges a PR.
- Notification Center owns durable events and Briefing history. Standards-based Web Push is the only
  automatic external transport; ntfy remains manual legacy only. External payloads stay minimal and
  actions stay limited to Open, Snooze, and Mute. One global policy controls every enabled device;
  every canonical event kind has an independent in-app visibility switch plus an
  Off/Once/Once+reminder/Repeat Web Push rule, with conservative defaults.
- Desktop uses a per-device left/right navigation rail. Mobile uses bottom navigation. M12 adds
  Notifications as a primary destination and moves History under More on mobile.
- Workstreams are lightweight repository/project groupings, not a new task-management system.
- Light automations are one-time outgoing messages, not a general recurring automation system. They
  support send at a time, send when an existing session/agent is next available, send when a selected
  usage window resets, and start a configured new coding session at a time and send its message.
- Now owns a central Outbox beside the action inbox. Creating an outbox item is the single explicit
  authorization for its later automatic send; execution does not ask for a second confirmation.
- Features must remain usable in a 390×844 viewport and must not turn Now into a control wall.
- Pinned sessions retain persisted insertion order. New pins append at the bottom; urgency and
  activity changes never reorder them.
- Each milestone gets deterministic tests, a commit, and a push before dependent work starts.

## Requirements catalogue and completion ledger

This catalogue is the source-of-truth checklist for the work below. Every requirement has a stable
ID, an observable acceptance condition, and an owning milestone. A milestone is not complete until
its IDs have implementation evidence and passing deterministic tests. M10 re-audits every ID against
the current source and running app; a checked box or this document's prose is not evidence by itself.

### Product and safety decisions

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| DEC-001 | Keep Claude Code and Codex in one Fleet app. Provider sessions remain independent and no duplicate Codex app or implicit cross-provider spawn is introduced. | All |
| DEC-002 | Preserve provider-appropriate capabilities. Unsupported values render as unavailable, never fabricated zeroes or hidden controls presented as parity. | All |
| DEC-003 | Search all local Claude/Codex main and saved-subagent transcripts, Fleet-known artifact text, and session metadata, including sessions Fleet did not create. Never crawl arbitrary repository files. | M2 |
| DEC-004 | Provider handoff always shows an editable preview with a one-action accept-unchanged path and clearly creates an independent session. | M5 |
| DEC-005 | Git actions stop at commit, push, draft PR, and explicitly confirmed mark-ready. Fleet never merges. | M6 |
| DEC-006 | Before M12, Briefings live in Fleet and ntfy is optional for immediate blockers and one fleet-quiet digest. DEC-020 supersedes this delivery policy when M12 ships. | M7/M12 |
| DEC-007 | Desktop navigation is a per-device left/right rail. Mobile navigation is a bottom bar with overflow for Insights and Settings. | M1/M11 |
| DEC-008 | Workstreams are lightweight repo/project groupings, not tasks, kanban, ownership, or dependencies. | M3 |
| DEC-009 | Every new interaction works at 390×844 and desktop size without overloading Now. Split destinations when density warrants it. | All |
| DEC-010 | External ChatGPT Desktop/VS Code Codex threads remain view-only unless explicitly connected to Fleet's App Server. They are still discoverable and their observed transcript activity is trackable. | M0/M4 |
| DEC-011 | Preserve stable insertion order while a session remains Working. Re-sort only when it leaves and later re-enters Working. | M1/M3 |
| DEC-012 | User messages, quick replies, question answers, and approvals appear optimistically with sending, confirmed, and failed states. Failed content stays retryable. | M0/M1 |
| DEC-013 | Automations are one-time send variants, not recurring jobs: send at time, when available, when a selected usage window resets, or by starting a configured new session at time. | M7 |
| DEC-014 | Now contains the central Outbox. Creating an item explicitly authorizes its later automatic spawn/send; no unattended merge, shell, approval, or other action is generalized from this permission. | M7 |
| DEC-015 | A due message waits if its existing target is busy. A closed/missing target becomes Blocked and is never implicitly reopened; the user may retarget it. | M7 |
| DEC-016 | “When usage resets” binds to one selected provider identity and one provider-reported window. Fleet follows updated provider reset evidence and never infers across accounts/windows. | M7 |
| DEC-017 | Scheduled new sessions snapshot every New Session field, including provider, project/cwd, model, effort, mode, and optional worktree, and remain editable until dispatch. | M7 |
| DEC-018 | Pinned sessions use persisted insertion order. New pins append at the bottom; urgency/activity sorting never changes the pinned list. | M11 |
| DEC-019 | Full-chat operational telemetry is bounded, incrementally maintained, and provider-honest. Missing context, cache, Git, or currency data is omitted rather than inferred. | M11 |
| DEC-020 | Fleet Notification Center is the notification source of truth. Standards-based Web Push is the only automatic external transport; ntfy is manual legacy only and never a fallback or duplicate destination. | M12 |
| DEC-021 | External notifications disclose only generic state and elapsed time. Push may Open Fleet or directly Snooze the exact event/Mute the exact session; every consequential action opens Fleet for current-state review. | M12 |
| DEC-022 | Superseded by DEC-030. The M12 production baseline pushed only confirmed Needs-you requests, approvals, provider/delivery failures, and prolonged stalls, with at most one reminder. | M12/M13 |
| DEC-023 | One provider usage/rate/context limit blocks only its session. It never prevents the HTTP server from starting or labels the whole dashboard unreachable. | M10 |
| DEC-024 | GitHub information opens the canonical external GitHub URL. Fleet does not duplicate a repository or pull-request page. | M6 |
| DEC-025 | Non-secret text drafts remain device-local until send/manual deletion. Ordinary messages sent while Fleet is known offline queue locally and flush FIFO only after a live reconnect; unknown delivery outcomes never auto-retry. | M1/M10 |
| DEC-026 | Full-chat image attachments support Claude and Codex. Device blobs and server-normalized copies are private, session-scoped, metadata-stripped, and expire after 24 hours; pre-dispatch uploads may retry, but unknown provider delivery never does. | M1/M10 |
| DEC-027 | A Claude `kind:bg` session is controlled only through the official fixed-argv `claude attach <job>`/Ctrl-Z detach and `claude stop <job>` paths. A retained PTY fd is not an iTerm route, and Fleet never reads Claude's private daemon roster or credentials. | M13 |
| DEC-028 | Codex steer/interrupt authority exists only for a live turn notification received on the current App Server connection generation. Transcript observation affects status only. Temporary authority loss may queue an exact idempotent text/image send in the central Outbox; it never forces `thread/resume`. | M13 |
| DEC-029 | Full chat and Markdown share one canonical main-session composer. Markdown embeds no conversation panel; Chat opens full conversation while preserving the draft. Return inserts a newline and only modified Return or Send submits. | M13 |
| DEC-030 | One revisioned global notification policy applies to every enabled device. Every canonical kind has independent in-app visibility and supports Off, Once, Once + reminder, or bounded Repeat for Web Push, with severity, delay, quiet-hours, mute, snooze, and explicit apply-to-current semantics. Device rows control delivery health/pause only. | M13/M14 |

### Navigation, presentation, and interaction

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| UX-001 | Provide Now, Notifications, Search, Workstreams, History, Insights, and Settings as navigable destinations; deep links, refresh, browser history, and native back/swipe preserve the expected destination. | M1/M12 |
| UX-002 | Keep pinned sessions above Needs you. Now then presents Needs you, Working, and Available in that order; History owns external inactive, dormant, reopenable, and closed sessions. | M1 |
| UX-003 | Use one canonical card hierarchy: identity/outcome first, reason and access second, metadata/details on demand. Expanded cards preserve contrast without changing semantic state. | M1 |
| UX-004 | Render state, reason, access, and provider as separate fields. Color is never the only distinction and counters use consistent typography/color. | M1 |
| UX-005 | Consolidate questions, approvals, MCP forms, delivery receipts, retry, and validation into one interaction component reused on cards, full chat, and action inbox. | M1/M3 |
| UX-006 | Add a sticky, destination-aware command bar for text search, filter chips, saved views, and primary actions without duplicating every control on every page. | M1/M3 |
| UX-007 | Full chat, Markdown viewer, and subagent viewer honor Fit screen versus centered/max-width settings and expose only relevant overflow-menu actions. | M1 |
| UX-008 | Peek renders safe formatted Markdown, truncates at 500 characters, replaces overflow with a clickable ellipsis, and expands to the complete response without a second truncation limit. | M0/M1 |
| UX-009 | Mobile scroll gestures collapse the open keyboard, and all touch targets, overlays, sticky regions, and bottom navigation remain usable at 390×844. | M1 |
| UX-010 | Loading, empty, stale, unavailable, read-only, validation, sending, success, and failure states exist for every destination and mutation. | All |
| UX-011 | Account summaries show provider, signed-in identity, plan/usage details, and honest locally scoped lifetime token descriptions; multiple Claude identities are supported where local data exposes them. | M8 |
| UX-012 | Session close/stop controls live in the full-chat overflow menu with confirmation. Stop affects the current turn; Claude close may terminate its terminal, while provider capability text remains explicit. | M1 |
| UX-013 | Every message composer groups Send now, Schedule, When available, and When usage resets without making the common Send now path slower. New Session offers Schedule session alongside Start session. | M7 |
| UX-014 | The Outbox is reachable from Now and the sticky command bar, has a pending-count badge, and supports edit, send now, retarget, retry, and cancel where state permits. | M7 |
| UX-015 | Now puts counts in its All/Needs you/Working/Available/Subagents filters and moves detailed provider usage behind a warning-aware Usage chip. M11's Briefing-above-Pinned placement was intentionally superseded by UX-019 when N4 moved Briefing into Notifications. | M11/M12 |
| UX-016 | Every composer is multiline: Return inserts a newline, modified Return or the explicit button sends, and optimistic startup/sending states retain exact text on failure. | M11 |
| UX-017 | Full chat and subagent chat expose an adaptive operational status strip; Markdown and chat headers avoid duplicated metadata; nested overlays always stack above chat evidence. | M11 |
| UX-018 | Every asynchronous control paints a pressed/optimistic/loading state immediately, rejects duplicate submissions, ignores stale responses, and keeps an inline restore/retry path on failure. | M11 |
| UX-019 | Notifications provides Needs action, Updates, Snoozed, Problems, Briefing, and History with an unread badge, exact event deep links, responsive navigation, and per-device read state. Now retains only the live Action Inbox. | M12 |
| UX-020 | Fleet is an installable mobile/desktop PWA with explicit install, permission, subscription, test-delivery, reconnect, unsupported, and delivery-health states. It caches only the token-free shell and last exact `/api/fleet` snapshot; every other private API and transcript response remains network-only. | M12 |
| UX-021 | Every non-secret text field restores its local draft after navigation/reload. Known-offline ordinary messages show a durable queued receipt and flush in order after a live reconnect; commands remain unsent drafts. | M1/M10 |
| UX-022 | The full-chat composer opens the mobile camera/photo library or desktop image picker, keeps up to four 10 MB image drafts across reloads, shows attached-image receipts, and flushes image messages after reconnect without duplicating provider dispatch. | M1/M10 |

### Intelligent global search

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| SRCH-001 | Incrementally index every source in DEC-003 into a separate WAL/FTS5 database without delaying initial fleet render, provider polling, or `/api/fleet`. | M2 |
| SRCH-002 | Persist per-source offsets and generations. Correctly handle append, partial final JSON, truncate, replace, delete, parser-version change, duplicate records, and restart. | M2 |
| SRCH-003 | Normalize provider, session, subagent, project, cwd, branch, role, kind, timestamp, title, text, and authorized artifact metadata while preserving a stable source key. | M2 |
| SRCH-004 | Support debounced queries, cancellation, prefix matching, relevance/recency boosts, provider/project/kind filters, bounded safe snippets, pagination, and recent-conversation empty queries. | M2 |
| SRCH-005 | Expose index progress, lag, per-source errors, and a controlled rebuild. Malformed/unknown records degrade visibly without stopping other sources. | M2 |
| SRCH-006 | Require local action authentication because unmanaged transcripts become searchable. Never disclose unauthorized source paths or bypass artifact preview safety. | M2 |
| SRCH-007 | Pass the 100k-message/2k-source benchmark: warm p95 <75 ms, cold p95 <150 ms, live lag <2 polls, HTTP blocking slice <25 ms, and `/api/fleet` p95 regression <5 ms. | M2/M9 |
| SRCH-008 | Search results open the canonical session/subagent/artifact context and work on desktop and mobile with keyboard and screen-reader navigation. | M2 |

### Action inbox and Workstreams

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| ACT-001 | Normalize structured questions, explicit prose questions/reply requests, approvals, permissions, MCP forms, actionable errors, budget alerts, Git/PR failures, and unreviewed completed outcomes into one inbox. | M3/M6/M8 |
| ACT-002 | Each action shows request, essential context, age, provider, access, reason, primary action, and delivery state, and opens the canonical interaction component. | M3 |
| ACT-003 | Deduplicate the same underlying request across cards/inbox/refresh/restart and reject stale or duplicate answers without losing the pending request. | M3 |
| ACT-004 | Bulk actions are limited to mark read/available, mute, and notification dismissal. Never bulk-approve a command or file change. | M3 |
| ACT-005 | Immediate submission feedback appears both inside full chat and on main-page quick responses; question answers remain visible with sending/confirmed/failed status. | M0/M1 |
| WORK-001 | Group Git sessions by canonical main repository root, roll linked worktrees underneath it, and group non-Git sessions by canonical cwd. | M3 |
| WORK-002 | Show per-workstream state counts, active branches/worktrees/providers, latest outcome, changed files/tests/PR summary, measured usage/cost, budget state, and filtered sessions. | M3/M6/M8 |
| WORK-003 | Handle symlinks, missing paths, nested repositories, detached heads, renamed roots, and one-provider outages without merging unrelated projects. | M3 |

### State evidence and external tracking

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| EVID-001 | Extract a pure provider-neutral placement classifier returning state, reason, access, primary action, ordered evidence, winning rule, suppressed rules, and confidence. | M4 |
| EVID-002 | Questions are Needs you only when provider structure or unambiguous assistant prose requests a response. Plans, status reports, and quoted/example questions do not create false attention. | M4 |
| EVID-003 | Persist meaningful deduplicated transitions with bounded safe evidence, expose paginated history, and recover after restart without rewriting past truth. | M4 |
| EVID-004 | The evidence rail explains the exact current placement using provider signal, transcript event, pending work, age, classifier rule, and stale/inferred status. | M4 |
| EVID-005 | External view-only Codex transcripts move to Working when current local transcript evidence shows an active turn, then Available/recently completed or History based on completion and inactivity. Fleet never implies it can steer them. | M0/M4 |
| EVID-006 | Provider/index/account errors retain the last good snapshot as explicitly stale, recover automatically, and never take the other provider down. | M4/M10 |

### Editable provider handoff

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| HAND-001 | Build an editable preview from objective, relevant recent turns, unresolved work, repo/branch/worktree, changes, tests, PR, and artifacts, excluding secrets and credentials. | M5 |
| HAND-002 | Default controls are useful and the unchanged preview is accepted with one primary action; advanced provider/model/effort/mode/worktree/artifact options stay available. | M5 |
| HAND-003 | Support Claude→Codex, Codex→Claude, and same-provider continuation while keeping source and destination sessions independent and linked for navigation. | M5 |
| HAND-004 | Identify the newly created provider-native session before delivery. Never submit to a similar existing session; spawn/delivery failure stays visible and retryable. | M5 |
| HAND-005 | Keep an extension seam for future provider spawning but do not implement implicit Claude-to-Codex subagent spawning. | M5 |

### Repository and delivery outcome center

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| REPO-001 | Cache bounded, timed, argv-only probes for branch/worktree, dirty state, diffstat, upstream/ahead/behind, commits, PR/check/review state, and observed test/build results. | M6 |
| REPO-002 | Distinguish observed passing/failing/stale/not observed. GitHub/provider/network failure never turns an unknown into success and never blocks the fleet. | M6 |
| REPO-003 | Workstreams show bounded local outcome evidence and one validated external GitHub link. Fleet exposes no in-app repository/PR detail page or duplicate commit/push/PR forms. | M6 |
| REPO-004 | Retained compatibility mutation endpoints require auth, canonical repo confinement, enum/length/path validation, bounded progress, durable outcome, and no free-form shell; no current UI exposes them. | M6 |
| REPO-005 | Never offer or execute merge, automatic commit/push/PR readiness, or history rewrite. | M6 |

### Message Outbox and light automations

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| AUTO-001 | Persist four message intents: send to an existing session/agent at a time, send when it is next available, send when a selected usage window resets, and start a configured new session at a time then send. | M7 |
| AUTO-002 | Store absolute triggers in UTC plus the creation-time IANA zone for display. Reject nonexistent local times, disambiguate repeated DST times, and execute an overdue item once after restart. | M7 |
| AUTO-003 | Existing-target delivery revalidates target identity, access, pending requests, and provider capability. Busy targets wait; already-available targets queued “when available” send immediately; closed/missing/read-only targets become Blocked. | M7 |
| AUTO-004 | Agent targets use only the provider's supported relay path. Completed, missing, direct-relay-unsupported, or read-only agents become Blocked with a retarget action rather than silently sending to the parent. | M7 |
| AUTO-005 | Usage-reset delivery stores provider, stable account/profile identity, window identity, and last reported reset. It follows a provider-updated reset time, waits for fresh post-reset evidence, and blocks visibly when identity/window data becomes unavailable. | M7 |
| AUTO-006 | Scheduled new-session delivery snapshots the validated New Session form, revalidates it at dispatch, spawns exactly one identifiable session, and sends only after that identity is confirmed. A spawn failure never sends to a similar existing session. | M7 |
| AUTO-007 | Use durable claim/lease transitions so concurrent polls and daemon restarts do not intentionally duplicate a send. If Fleet crashes after dispatch but before confirmation, mark Confirmation unknown and never auto-retry. | M7 |
| AUTO-008 | User-visible states are Scheduled, Waiting for availability, Waiting for usage reset, Spawning, Sending, Sent, Confirmation unknown, Blocked, Failed, and Cancelled, each with reason, timestamps, target, trigger, and permitted actions. | M7 |
| AUTO-009 | Pending items are editable and cancellable. Send now bypasses the trigger but revalidates the target. Failed/Blocked/Confirmation-unknown items require an explicit retry; Sent and Cancelled records are immutable audit history. | M7 |
| AUTO-010 | The ordinary Send now button remains primary and one tap. Adjacent scheduling choices share the same composer text, validation, attachments, provider targeting, and optimistic delivery component. | M7 |
| AUTO-011 | Outbox reads and mutations require local action authentication, messages are length-bounded and stored locally, secret fields are never copied from provider/account config, and no automation accepts shell text as an executable action. | M7 |
| AUTO-012 | Deterministic fake-clock/provider tests cover same-second due items, concurrency, order, DST, restart in every state, unavailable providers/accounts, reset shifts, busy-to-available, closed/retargeted targets, spawn/send partial failure, stale IDs, cancellation races, and large queues. | M7/M10 |

### Briefings, notifications, usage, and budgets

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| BRIEF-001 | Build deterministic in-app sections for attention, reviewed/unreviewed completion, slow work, Git/test/artifact outcomes, budget warnings, and unavailable measurements. | M8 |
| BRIEF-002 | Persist a per-device review cursor without deleting evidence/history; deduplicate events across poll/restart and preserve source links. | M8 |
| BRIEF-003 | Preserve the M8 ntfy behavior until M12 switches transport. M12 then removes quiet/scheduled external digests, keeps failures visible in Notification Center, and makes session mute suppress Web Push until manual unmute. | M8/M12 |
| BUD-001 | Configure alert-only budgets by session, workstream, provider, or fleet for exact USD, tokens, runtime, and concurrency, with optional explicit blocking of future spawns only. | M8 |
| BUD-002 | Label exact, partial, token-only, and unavailable measurement. Never convert Codex quota tokens into API spend or display fabricated zero cost/throughput. | M8 |
| BUD-003 | Forecast from recent measured burn and provider/model/project history, showing sample size/confidence and “not enough history”; never interrupt active work automatically. | M8 |
| USE-001 | Claude historical totals state their local transcript/stat-cache scope; Codex quota/lifetime/context numbers state their provider scope and omit unused Spark-specific quota. | M8 |

### Cross-cutting quality gates

| ID | Requirement and acceptance condition | Owner |
| --- | --- | --- |
| QUAL-001 | Every new read/mutation route validates auth, IDs, enums, lengths, cursors, and canonical paths; outputs are escaped and paginated/bounded. | All |
| QUAL-002 | Unit, fake-provider/protocol, engine/API, Playwright desktop/mobile, safe-path, auth/read-only, crash/restart, and opt-in live tests cover each requirement's success and failure paths. | All/M10 |
| QUAL-003 | Capture baseline and final p50/p95 latency, payload, index lag, render/input latency, and memory on the same corpus; optimize only with before/after evidence. | M0/M9 |
| QUAL-004 | Each milestone is a focused append-only commit pushed to its approved feature branch; unrelated user work is preserved and `main` is never changed. | All |
| QUAL-005 | Final audit cites current `file:line` implementation and test evidence for every catalogue ID, reloads the daemon, checks HTTP/browser console/network state, and lists any exact provider limitation. | M10 |
| QUAL-006 | Input-to-visible-feedback is <100 ms p95 and routine Fleet-local completion is <250 ms p95 on desktop and 390×844 mobile; named interaction surfaces retain repeatable latency gates. | M11 |

### Milestone traceability gate

Before each milestone commit, its catalogue IDs are copied into the commit's verification note with
the exact tests that prove them. The completion audit must account for every ID in this catalogue;
missing, partially implemented, or documentation-only IDs keep M10 open. New decisions discovered
during implementation receive a new ID here before dependent code is written.

## Product model

Fleet exposes four independent dimensions. No feature may collapse them into one ambiguous label.

| Dimension | Question it answers | Examples |
| --- | --- | --- |
| State | What requires attention now? | Needs you, Working, Available, History |
| Reason | Why is it in that state? | Question waiting, Running tests, External, Inactive |
| Access | What can Fleet safely do? | Interactive, View only, Reopen |
| Provider | Which runtime owns it? | Claude Code, Codex CLI |

The durable organizational unit remains the provider session. Workstreams group sessions by canonical
repository root and project identity without introducing task ownership, dependencies, or hidden
cross-provider coupling.

## UX direction

### Subject and job

The subject is a local operations desk for one person supervising many coding agents. Its single job
is to make the next useful action obvious while keeping raw runtime evidence one tap away.

### Visual system

Keep Fleet's established dark, terminal-adjacent identity instead of replacing it with a generic SaaS
dashboard.

| Token | Value | Use |
| --- | --- | --- |
| Night deck | `#0d1117` | App background |
| Session plate | `#161b22` | Available and reading surfaces |
| Raised plate | `#1c2330` | Active cards and selected navigation |
| Instrument line | `#2d333b` | Structure and separation |
| Signal blue | `#58a6ff` | Primary actions and current destination |
| State signals | `#3fb950`, `#d29922`, `#f85149` | Working, attention, failure only |

- Navigation, headings, and prose use the system UI face for fast scanning.
- IDs, branches, tokens, timings, and command output use SF Mono/Menlo.
- Color never carries state alone; every signal has a label or icon.
- Motion is limited to live delivery, active work, and route transitions, and respects reduced motion.

### Layout

Desktop:

```text
┌──────────────┬────────────────────────────────────────────────────────┐
│ Fleet Dash   │ Destination title                 account / new       │
│              ├────────────────────────────────────────────────────────┤
│ Now       3  │ sticky query + filters + saved view                   │
│ Search       ├────────────────────────────────────────────────────────┤
│ Workstreams  │ destination content                                   │
│ History      │                                                        │
│ Insights     │                                                        │
│ Settings     │                                                        │
└──────────────┴────────────────────────────────────────────────────────┘
```

Mobile:

```text
┌──────────────────────────────┐
│ title              primary   │
│ query / filter button        │
│                              │
│ destination content          │
│                              │
├──────────────────────────────┤
│ Now Search Work History More │
└──────────────────────────────┘
```

Search and Workstreams remain reachable from the bottom bar. Insights and Settings live under More
when six equal-width targets would become cramped. Browser history and the native back swipe return
from detail screens without losing scroll, filters, or drafts.

### Signature element: evidence rail

Every session has a narrow state rail that opens a chronological explanation: provider signal,
normalized state transition, pending work, last meaningful transcript event, and the exact placement
rule. This is Fleet-specific instrumentation, not decoration. The rest of the interface stays quiet.

### Design self-critique

A sidebar, cards, and bottom navigation alone would look like any operations dashboard. The revision
anchors the design in Fleet's actual material: session plates read like compact flight strips, state
colors are sparse instrument signals, and the evidence rail exposes the classifier instead of adding
decorative charts. No gradients, ornamental metrics, or duplicate desktop/mobile controls are added.

## Shared interaction architecture

The current single HTML file must not absorb seven feature systems. Split it without changing the
zero-build deployment model:

```text
dashboard.html                 semantic shell and overlay roots
static/fleet.css               tokens, shell, responsive layout, shared components
static/app.js                  boot and compatibility exports
static/store.js                fleet cache, route state, optimistic actions
static/api.js                  fetch, auth, cancellation, latency capture
static/router.js               destinations, overlay history, swipe/back behavior
static/components.js           cards, questions, approvals, files, delivery, evidence
static/views/now.js            action inbox and live inventory
static/views/search.js         indexed search
static/views/workstreams.js    repository/project grouping
static/views/history.js        inactive and closed sessions
static/views/insights.js       costs, briefings, and budgets
static/views/settings.js       grouped settings
```

These remain browser-native modules served locally by `server.py`; no framework or build service is
introduced. Existing inline event handlers are migrated to delegated events so the same question,
approval, delivery, and session-card components render in Now, full chat, Markdown, and subagent
surfaces.

## Indexed global search

### Data sources

- `~/.claude/projects/*/*.jsonl` main transcripts.
- `~/.claude/projects/*/*/subagents/*.jsonl` saved subagent transcripts.
- `~/.codex/sessions/**/*.jsonl` Codex transcripts, whether or not Fleet loaded the thread.
- Delivered/generated artifact metadata and safe text extracted only from files explicitly recorded
  by the owning session.
- Fleet session metadata: title, provider, project, cwd, branch, model, state, and access.

### Storage

Use a separate `~/.claude/fleet-dash/search.db` so indexing cannot lock the usage/session ledger.
Enable WAL, `busy_timeout`, and FTS5. Keep one writer connection in the index worker and short-lived
read connections for HTTP requests.

```text
search_sources(
  path PRIMARY KEY, provider, session_id, agent_id, kind,
  size, mtime_ns, byte_offset, generation, indexed_at, error
)
search_documents(
  id PRIMARY KEY, source_path, source_key UNIQUE,
  provider, session_id, agent_id, project, cwd, branch,
  role, kind, timestamp, title, text, artifact_path
)
search_fts USING fts5(title, text, project, branch, content=search_documents)
```

`source_key` is stable within a transcript. Appended complete JSONL records index from `byte_offset`.
Truncation, inode replacement, or parser-version changes increment `generation` and rebuild only that
source. Partial trailing records remain unconsumed. Deleted sources are tombstoned in bounded batches.

### Scheduling and query behavior

- Initial discovery runs after the first fleet snapshot in a low-priority worker.
- Live sources are checked every poll; archive discovery runs on a slower cadence.
- Each indexing slice has a time and document budget, yields, and resumes from durable offsets.
- The UI debounces by 150 ms, cancels superseded requests, requests 40 results, and paginates by an
  opaque cursor.
- FTS queries use prefix matching for the final token, BM25 ranking, recency, exact title/branch
  boosts, provider/access filters, and escaped snippets.
- Empty queries return recent indexed conversations rather than scanning the database.
- Unknown or malformed rows create a visible per-source indexing warning without stopping the worker.

### Performance gates

- `/api/fleet` p95 regression from the pre-index baseline: less than 5 ms.
- Warm search p95: under 75 ms; cold search p95: under 150 ms on the real local corpus.
- A newly completed transcript line becomes searchable within two fleet polls.
- Initial backfill never holds the engine scan lock and never blocks an HTTP response for more than
  25 ms.
- Deterministic benchmark corpus: at least 100,000 messages, 2,000 sources, long text, malformed rows,
  truncation, and concurrent queries.

Search endpoints require the local action token because this feature exposes conversations Fleet did
not previously surface. Search results return bounded snippets, never raw source paths outside safe
metadata, and artifact previews retain the existing session/path authorization rules.

## Action inbox

Now begins with an action inbox, not another duplicate session list. It normalizes:

- Structured questions, approvals, permissions, and MCP forms.
- Explicit prose reply requests.
- Session/provider errors requiring intervention.
- Budget alerts and Git/PR failures.
- Completed work not yet reviewed.

Each row shows the request, essential context, age, provider, access, primary action, and delivery
state. Safe bulk actions are limited to mark-read, mark-available, mute, and dismiss notifications.
Fleet never bulk-approves commands or file changes. Selecting an action opens the canonical response
component; it does not create a second action implementation.

Working and Available remain below the inbox on Now. Pinned items remain first. Empty groups collapse;
Available keeps a small empty state. Stable Working insertion order remains unchanged.

## Lightweight Workstreams

Workstreams group sessions by canonical Git root; non-Git directories group by canonical cwd. Linked
worktrees roll up under the main repository while preserving their branch/worktree labels.

Each workstream shows:

- Needs-you, working, available, and historical session counts.
- Active branches/worktrees and providers.
- Latest meaningful outcome and changed-file/test/PR summary.
- Measured token/cost totals with unavailable values kept explicit.
- Budget state and a filtered session list.

It does not add manual tasks, dependencies, ownership, kanban stages, or a second session lifecycle.

## State evidence

### Durable model

Add a transition journal to the ledger:

```text
state_events(
  id INTEGER PRIMARY KEY, session_id, provider, at,
  raw_state, reg_status, normalized_state, ui_group,
  reason, access, evidence_kind, evidence_summary, revision
)
```

Write only meaningful changes, not every poll. Dedupe identical transitions by session/revision.
Evidence summaries contain bounded, user-safe facts rather than full provider payloads.

### Classifier contract

Extract placement into a pure provider-neutral classifier. Its result includes:

- State, reason, access, and primary action.
- Ordered evidence facts.
- The rule identifier that won.
- Suppressed lower-priority rules for diagnostics.
- Confidence: confirmed, inferred, stale, or unknown.

The evidence rail displays the latest facts immediately and pages historical transitions on demand.
Every user-facing state and stale/error recovery path gets fixture coverage.

## Editable provider handoff

“Continue in Claude” and “Continue in Codex” open a preview containing:

- Objective inferred from the first meaningful user request and current title.
- Latest relevant user/assistant exchanges.
- Unresolved questions, approvals, failures, and TODO-like statements.
- Repository, cwd, branch/worktree, changed files, tests, PR, and delivered artifacts.
- Source-session link and an explicit statement that this creates an independent session.

The preview is plain editable text with sensible defaults. The primary button accepts it unchanged.
Advanced controls choose provider, model, effort, mode, same directory versus new worktree, and which
artifact references to include. Secret fields and raw credentials never enter the preview.

Creation uses provider-native starts where available. Codex starts through Fleet's App Server. Claude
starts through the existing validated terminal path with a Fleet-reserved `--session-id`, then submits
the approved handoff only after that exact registered UUID appears. New Codex worktrees are created
with bounded argv-only Git commands beneath Fleet's managed worktree root. Failure at either stage
remains visible and retryable against the recorded destination; Fleet never sends the handoff to a
merely similar pre-existing session. Only link identity/status and a preview hash are retained; edited
handoff bodies are omitted from Fleet's ledger and daemon action log.

## Git, PR, build, and delivery outcomes

### Read model

Repository probes use argv arrays, fixed subcommands, per-command timeouts, canonical repository
roots, and a TTL cache. They collect:

- Branch, detached state, worktree path, dirty counts, diffstat, upstream, ahead/behind.
- Latest local commit and whether the session's changed files are committed.
- GitHub PR number/state, draft status, URL, review decision, mergeability, and check rollup through
  `gh --json` when authenticated.
- Recent test/build commands and exit status from normalized transcript tool events. These are labeled
  with their source and age; absence is “not observed,” never “passing.”

Provider or GitHub failure marks the cached result stale and leaves the other provider operational.

### Presentation and compatibility actions

Workstreams retain the bounded local summary and expose one validated external **GitHub ↗** link.
Fleet no longer renders repository/PR detail pages or commit, push, draft-PR, and mark-ready forms.
The authenticated argv-only mutation endpoints remain temporarily for compatibility; no current UI
calls them. Merge remains unavailable.

## Message Outbox and light automations

The Outbox is the outgoing counterpart to the action inbox. It lives within Now, with a compact
pending count in the navigation/command bar, because these records are current operational work—not
settings and not historical conversations. The default Now view shows only the next few pending or
blocked items; opening Outbox shows the complete filterable audit list.

### Send modes

| Mode | Trigger | Target behavior |
| --- | --- | --- |
| Send at time | One absolute local date/time | Revalidate the existing session/agent at or after the instant; wait if busy |
| When available | First observed Available state, or immediately if already Available | Send to the exact existing interactive session or supported agent relay |
| When usage resets | Fresh provider evidence that one selected account/window passed its reported reset | Revalidate that target, then send or wait for availability |
| Schedule new session | One absolute local date/time | Revalidate the snapshotted New Session form, spawn one exact session, identify it, then send |

Scheduling is deliberately one-time. A recurring job editor, arbitrary state predicates, shell hooks,
automatic approvals, and chained workflows are outside this scope.

### Durable data and state machine

Use the existing ledger database with a dedicated table and indexes; scheduling must not depend on an
open browser tab.

```text
outbox_messages(
  id TEXT PRIMARY KEY, created_at, updated_at, created_zone,
  kind, state, message, target_provider, target_session_id, target_agent_id,
  trigger_at, usage_account_id, usage_window_id, observed_reset_at,
  spawn_spec_json, claimed_at, lease_until, attempt_count,
  destination_session_id, provider_receipt, sent_at, error, blocked_reason
)
```

The state machine is:

```text
Scheduled ───────────────┐
Waiting for availability├─> Spawning? -> Sending -> Sent
Waiting for usage reset ┘                  │  │
                                          │  └-> Confirmation unknown
                                          └----> Failed
Any pending state -> Blocked | Cancelled
Blocked/Failed/Confirmation unknown --explicit retry/retarget--> pending
```

The scheduler uses an atomic database claim and expiring lease so only one poller owns a due record.
It persists `Sending` before provider dispatch and a provider receipt immediately after acceptance.
Provider APIs do not promise an idempotency key for an arbitrary turn, so a process death in that
narrow interval becomes **Confirmation unknown**. Fleet must not auto-retry and risk sending the same
message twice.

Times are stored in UTC and retain the IANA creation zone for editing/display. The UI rejects a local
time skipped by daylight-saving changes and asks which occurrence is intended for an ambiguous time.
Overdue records execute once after restart. Equal-time records use creation order. A transient
provider outage retries with bounded backoff until 24 hours after the trigger, then becomes Blocked;
the user may edit that expiry or send now.

### Validation and editing

- Existing targets must still exist, be interactive, expose submit/relay, have no unresolved provider
  request that would make a free-text turn unsafe, and not be actively running. Busy targets wait.
- Closed, missing, external view-only, or unsupported direct-agent targets become Blocked. Fleet never
  silently reopens, takes over, or substitutes a parent session.
- Usage-reset records store the stable local account/profile and provider window identifiers. A reset
  time update moves the trigger; missing/stale usage data pauses rather than guessing.
- Scheduled new sessions persist the same validated provider, cwd/project, model, effort, mode,
  worktree, and initial-message fields as New Session. Dispatch never selects a session by recency.
- Until claimed, records can be edited or cancelled. Send now still performs all current validation.
  Sent and Cancelled rows are immutable; retry creates an explicit new attempt linked in audit data.

Every composer keeps Send now as its primary button and puts Schedule, When available, and When usage
resets in one adjacent send-options control. The New Session form similarly offers Start session and
Schedule session. Both use the canonical delivery receipt so the Outbox and conversation do not
disagree about Sending, Sent, or failure.

## Briefings and digests

Briefings remain deterministic selections from state transitions, Git outcomes, delivery events,
and budget alerts. They do not claim model-generated conclusions that the underlying evidence cannot
support.

M12 moves Briefing from Now into Notification Center as an on-demand summary over the canonical
event stream. The same durable records power Needs action, Updates, Snoozed, Problems, Briefing, and
History; reading advances only the current device's cursor and never deletes evidence.

Web Push follows DEC-030's global kind rules. Defaults retain the M12 actionable/failure policy:
question, approval, form, reply, and confirmed failure send once plus one 15-minute reminder; stall
sends once; completion, artifact, outcome, budget, measurement, and Fleet notices start Off. Turning
on an informational rule is explicit and does not backfill current events unless requested. ntfy is
an explicitly enabled fixed-copy manual test and never receives automatic fallback or duplicate
delivery. Muted sessions keep their in-app events but suppress external delivery until manual
unmute.

## Budgets and forecasts

Budgets can target a session, workstream, provider, or global fleet and can limit:

- Exact USD where the provider exposes enough measured data.
- Tokens where currency is unavailable.
- Runtime and concurrent agent count.

Codex session currency remains unavailable unless the protocol begins reporting it; Fleet must not
price quota tokens as API spend. A mixed-provider budget therefore shows exact, partial, or
token-only scope.

Forecasts use recent measured burn rate and historical medians for the same provider/model/project.
They show the sample size and confidence; insufficient history is “not enough history.” Before spawn,
Fleet shows the relevant historical median and budget headroom without blocking creation unless the
user explicitly configured a hard limit. Initial budget enforcement is alert-only; a separate setting
may enable blocking new spawns, never interrupt active work automatically.

## API surface

Planned read routes:

```text
GET /api/search?q=&provider=&project=&kind=&cursor=
GET /api/search/status
GET /api/actions
GET /api/workstreams
GET /api/evidence?sid=&cursor=
GET /api/handoff?sid=&provider=
GET /api/repo?root=
GET /api/outbox?state=&cursor=
GET /api/briefing?cursor=
GET /api/budgets
```

Planned authenticated mutations remain under the existing action/settings boundary:

```text
POST /api/act  type=handoff
POST /api/act  type=git_commit | git_push | pr_create_draft | pr_mark_ready
POST /api/act  type=outbox_create | outbox_update | outbox_cancel
POST /api/act  type=outbox_send_now | outbox_retry | outbox_retarget
POST /api/settings  budgets, digest preferences, saved views
```

Every route validates lengths, enums, IDs, cursors, canonical paths, and authorization. Large lists
paginate. Provider, index, Git, and notification failures return scoped stale/error objects rather
than taking down `/api/fleet`.

## Sequential milestones and commits

### M0 — Baseline and roadmap

- Commit this roadmap alone.
- Commit the already-tested main-card quick-response feedback separately.
- Record baseline scan, fleet API, context API, browser-load, and corpus-size measurements.

Exit: clean tree, baseline results saved in the optimization section of this document, all existing
tests green.

### M1 — App shell and shared components

- Split static assets and introduce the responsive router/shell.
- Add desktop rail, mobile bottom navigation, sticky command bar, and destinations.
- Move existing Now, History, Insights, and Settings without dropping controls.
- Standardize card hierarchy and state/reason/access/provider presentation.
- Consolidate question/approval/delivery rendering.

Commit: `Build responsive Fleet application shell`.

Exit: behavior-parity tests, desktop/mobile screenshots, keyboard focus, back swipe, reduced motion,
read-only mode, and no lost settings or actions.

### M2 — Incremental global search

- Add index worker, parsers, schema, authenticated APIs, Search destination, filters, snippets,
  pagination, progress, errors, and rebuild control.
- Index all settled sources and benchmark the real and deterministic corpora.

Commit: `Add incremental cross-provider transcript search`.

Exit: correctness, restart, append/truncate, malformed-input, concurrency, authorization, latency,
mobile, and large-corpus tests pass.

### M3 — Action inbox and Workstreams

- Add normalized action records/view and safe bulk triage.
- Add canonical repository grouping and workstream summaries.
- Make the sticky command bar destination-aware with saved filters.

Commit: `Add action inbox and repository workstreams`.

Exit: no duplicate actions, stable Working order, provider outage isolation, mobile triage, and
worktree rollup tests pass.

Completed 2026-07-16. `ACT-001`–`ACT-005`, `WORK-001`–`WORK-003`, `UX-005`, `UX-006`, and the
applicable `UX-010` states are represented by normalized action records, persistent safe-only bulk
triage, the existing canonical question/approval/full-chat renderer, saved Now/Workstreams filters,
and lazy repository grouping. Git changes, test results, PR state, and budgets remain explicitly
`not_observed`/`not_configured`; their measured enrichments stay owned by M6 and M8 rather than being
fabricated here. Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 96 passed.
- `npx playwright test tests/browser/fleet.spec.js` — 48 passed at desktop 1440×1000 and mobile
  390×844, including inbox dedupe/triage, no bulk approval, worktree rollup, saved views, provider
  outage, read-only, stale, optimistic delivery, and pin placement.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377 FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 4 passed across both viewports with no console/network failures.
- The initial implementation exposed a 158-workstream grouping cost on every fleet poll. The final
  design moves it to `GET /api/workstreams`; `/api/fleet` contains only the bounded action inbox, and
  the Workstreams page refreshes its lazy cached rollup only while open.

### M4 — State evidence

- Extract pure placement classifier.
- Add evidence facts, transition journal/API, evidence rail, and diagnostic history.

Commit: `Explain session state with durable evidence`.

Exit: every provider/raw state, transition, ambiguity, stale recovery, and classifier regression has
deterministic coverage.

Completed 2026-07-16. `EVID-001`–`EVID-005` and the M4 portion of `EVID-006` are implemented by a
pure provider-neutral classifier that returns the normalized state, placement, reason, access,
primary action, ordered bounded evidence, winning rule, suppressed rules, and confidence without
mutating its input. The SQLite transition journal records only meaningful consecutive changes,
survives restart, pages through `GET /api/evidence`, and skips already-journaled immutable closed
rows before rebuilding evidence. The card detail explains current placement; full chat exposes a
responsive “Why here?” rail with current facts and historical transitions. Recent view-only Codex
completions now remain Available for review before aging into History. Engine-level Codex failures
retain the last good session snapshot as stale/view-only and recover without removing Claude.
Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 100 passed, including every placement
  branch, false prose-question cases, same-session journal dedupe/pagination, stale/recovery, and
  provider isolation.
- `npx playwright test tests/browser/fleet.spec.js` — 50 passed at desktop 1440×1000 and mobile
  390×844, including card evidence, full-chat rail/history, responsive overlay behavior, stale and
  external states, and all prior fleet regressions.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377 FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 4 passed across both viewports with live evidence history and no
  console/network failures.
- On the live archive with 26 current and 734 closed sessions, the steady evidence-journal slice was
  2.615 ms. An initial per-session SQLite lookup design was rejected after it pushed scans to about
  250 ms; the final cache performs one startup signature load and no per-closed-row hot-path query.

### M5 — Provider handoff

- Add handoff synthesis, editable preview, worktree/provider controls, safe spawn/identification, and
  retryable delivery.

Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 108 passed, including redaction,
  indexed/fallback preview context, Claude→Codex, Codex→Claude, same-provider continuation, exact
  UUID targeting, timeout/retry, durable links, argv-only worktree creation, and failed-worktree
  cleanup.
- `npx playwright test tests/browser/fleet.spec.js` — 56 passed across desktop and 390×844 mobile,
  including chat/Markdown/closed-session entry points, editable accept flow, artifact toggles,
  advanced controls, native back, token protection, exact navigation, and no-duplicate retry.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377/ FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 6 safe running-daemon checks across both viewports, including an
  authenticated handoff preview that does not create or send a session.
- The installed in-app Browser was initialized exactly through its Browser skill, but this desktop
  task advertises no browser surfaces. The newest desktop log shows the native pipe rejecting the
  helper peer as `untrusted-code-signing-identity`; this is a helper-connection blocker, not a claim
  that the reinstalled ChatGPT application has an invalid signature. Deterministic and live
  Playwright remain green while this runtime-specific blocker is open for M10.

Commit: `Add editable cross-provider session handoff`.

Exit: Claude→Codex, Codex→Claude, same-provider continuation, same cwd, new worktree, spawn failure,
wrong-session protection, and mobile acceptance tests pass.

### M6 — Repository outcome center

- Add cached Git/GitHub/test outcome probes and Workstream/session presentation.
- Add confirmed commit, push, draft-PR, and mark-ready actions.

Current amendment (2026-07-17): the in-app Repository outcome page and mutation forms were retired.
Workstreams keep bounded local evidence and open GitHub externally. The verification below records
the historical M6 gate, not the current UI contract.

Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 121 passed, including clean/dirty,
  detached, remote/no-remote, upstream/no-upstream, ahead/behind, cache, timeout/error, no-gh,
  unauthenticated/network/malformed GitHub payloads, observed test/build outcomes, stale revisions,
  canonical observed-worktree confinement, fixed argv, durable outcomes, and every supported action.
- `npx playwright test tests/browser/fleet.spec.js` — 64 passed across desktop and 390×844 mobile,
  including Workstream and session entry points, authenticated repository details, editable commit and
  draft-PR previews, all confirmations, commit/push/draft/ready lifecycle, back navigation, and no
  browser console/runtime failures.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377/ FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 8 safe running-daemon checks across both viewports, including a real
  Git/GitHub repository outcome read with no mutation.
- `python3 tests/live_repo_smoke.py` — passed a real temporary local commit and push against a
  temporary bare remote; it did not touch the Fleet checkout or GitHub.
- Live after restart: `/api/workstreams` returned in 0.011 s from the repository cache; an explicit
  `federbenjamin/fleet-dash` GitHub detail lookup returned in 0.702 s and honestly reported that the
  current branch has no PR. The bulk Workstreams route deliberately skips per-repository GitHub
  network calls. The former Repository outcome UI that performed the full lookup is now retired.

Commit: `Add repository and pull-request outcome center`.

Exit: clean/dirty/detached/ahead/behind/no-remote/no-gh/unauthenticated/stale-network fixtures,
action validation, confirmation, and real opt-in repository smoke tests pass.

### M7 — Message Outbox and light automations

- Add durable outbox storage, fake-clock scheduler, four send modes, target/reset/spawn validation,
  claim/lease recovery, bounded retry, and audit states.
- Add composer send options, scheduled New Session, central Outbox, pending count, filters, editing,
  send-now, retarget, retry, and cancel actions.

Completed 2026-07-16. `AUTO-001`–`AUTO-012`, `UX-013`, `UX-014`, and the M7 portions of
`DEC-006` and `DEC-013`–`DEC-017` are implemented in the shared Fleet app. The scheduler uses the
ledger's WAL database and a separate one-second daemon loop. Exact existing-session, supported-agent,
usage-account/window, and snapshotted-new-session destinations are revalidated at dispatch. Atomic
claims, leases, provider backoff, crash recovery, ambiguous/nonexistent DST handling, immutable audit
records, and explicit retry/retarget paths are covered by fake-clock and concurrency tests. Codex uses
the saved initial message as its exact first turn; Claude reserves an exact session UUID and sends only
after that identity appears.

Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 139 passed.
- `npx playwright test tests/browser/fleet.spec.js` — 70 passed across desktop and 390×844 mobile.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377/ FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 10 safe running-daemon checks passed with no dispatch.
- `python3 tests/live_api_smoke.py` — passed against 26 live sessions, 7 Codex models, and 2 native
  Codex commands.
- `python3 tests/live_outbox_smoke.py` — created a future Claude delivery, verified it persisted, then
  cancelled it; the message was not sent.
- `launchctl kickstart -k gui/501/com.benjaminfeder.fleet-dash` and local HTTP verification — daemon
  restarted and returned 200.
- The ChatGPT in-app Browser remains unavailable to this desktop root task: the current desktop log
  records the Browser helper/socket peer being rejected as `untrusted-code-signing-identity`, and the
  desktop exposes zero Browser surfaces. This is a current helper connection blocker, not evidence
  that the reinstalled ChatGPT app itself has an invalid signature. Repository-native Playwright is
  the deterministic visual fallback.

Commit: `Add scheduled and state-triggered message outbox`.

Exit: every `AUTO-*` requirement passes unit, fake-provider/API, restart/crash/concurrency, desktop,
mobile, authentication, read-only, and safe opt-in provider tests. Ordinary Send now has no added tap
or latency regression.

### M8 — Briefings, digests, budgets, and forecasts

- Add event selection, briefing cursors, quiet digest, optional schedules, budget scopes, alerts, and
  honest mixed-provider forecasts.

Completed 2026-07-16. `BRIEF-001`–`BRIEF-003`, `BUD-001`–`BUD-003`, `USE-001`, and the M8 portions of
`ACT-001` and `WORK-002` are implemented by a WAL-backed operations ledger. Briefing events retain
evidence and source links after a monotonic per-device review cursor advances; alert episodes dedupe
across polling and restart, then reappear after recovery and recurrence. Current attention, reviewed
and unreviewed completions, unusually slow work, repository/outbox/artifact outcomes, budget alerts,
unavailable measurements, and persistent notification failures have deterministic in-app sections.
Muted sessions remain visible in-app while being omitted from per-session pushes and counted in
quiet digests.

Budgets target session, workstream, provider, or fleet scopes for cumulative locally observed USD,
tokens, and runtime, plus current concurrency. Measurement is explicitly exact, partial, token-only,
or unavailable. Codex currency remains unavailable and is never inferred from quota tokens. Budget
alerts appear in the shared action inbox and link to Insights. Hard limits are opt-in, block only
future matching spawns, never interrupt active work, and fail closed if an explicitly enabled safety
check becomes unavailable. Spawn estimates use matching provider/model/project medians and recent
measured burn with sample size and confidence.

Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 156 passed, including event dedupe and
  recurrence, review cursors, mute/quiet restart behavior, notification failure evidence, every
  budget scope/measurement state, cumulative closed-session measurements, forecast confidence,
  durable settings, action-inbox normalization, and fail-closed explicit spawn limits.
- `npx playwright test tests/browser/fleet.spec.js` — 74 passed across desktop 1440×1000 and mobile
  390×844, including reviewed/unreviewed briefings, source navigation, budget inbox alerts, responsive
  settings, scheduled-digest preferences, honest token scope, spawn forecasts, and all prior flows.
- `python3 tests/live_api_smoke.py` — passed against 26 live sessions and seven Codex models; briefing,
  budget, forecast, notification-setting, command, context, and authentication contracts were read
  without dispatching work.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377 FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 12 passed across both viewports with no console or network failures.
- After restart settled, live reads measured `/api/fleet` 11.300 ms, `/api/briefing` 38.942 ms, and
  `/api/budgets` 45.219 ms. Startup lock contention is retained as an explicit M9 optimization target.
- The daemon was restarted repeatedly during implementation and returned HTTP 200 after the final
  existing-config notification-default migration. The installed in-app Browser runtime remains
  blocked by the previously documented helper/socket trust rejection; repository-native and live
  Playwright provide deterministic visual coverage in the meantime.

Commit: `Add fleet briefings and measurable budgets`.

Exit: dedupe, mute, quiet episodes, restart, partial cost, token-only Codex, forecast confidence,
settings persistence, and push-failure tests pass.

### M9 — Optimization pass

- Completed 2026-07-16. Every response now reports server time and payload bytes; authenticated
  diagnostics retain bounded per-route latency/status/payload samples plus engine phase, DB-wait,
  search-lag, and process-memory measurements. The browser retains bounded render, poll,
  poll-payload, and input-to-feedback samples in `window.__fleetPerf`.
- The live 2.87 GB search corpus and a deterministic 100,000-message/2,000-source corpus were
  profiled. The deterministic corpus built in 2.022 seconds, cold search measured 14.710/15.441 ms
  p50/p95, warm search measured 0.004/0.011 ms, and live append-to-result lag measured 0.058 seconds.
- Closed-session metadata moved out of the two-second fleet poll into a paginated History endpoint.
  Pinned closed sessions remain in the fleet response, while `closed_ids` preserves direct lookup.
  Full conversations load newest-first in 50-message pages and preserve scroll position when older
  pages are prepended. The live fleet payload fell from 1,162,003 bytes immediately before M9 to
  159,249 bytes; the measured context response fell to 18,256 bytes.
- Closed-session filesystem checks, project aggregation, and identical search results use bounded,
  invalidation-aware caches. Search document counts are maintained transactionally instead of
  scanning the FTS table on every status read. Operations measurement signatures skip unchanged
  session queries and writes. Missing artifacts settle as visible errors instead of being reindexed
  forever.
- No transcript or index scan was added to an HTTP request path. Search ingestion remains in the
  background worker; fleet requests serialize the current engine snapshot; History and conversation
  pages are loaded only when opened. Pagination and cache invalidation have deterministic regression
  coverage.

Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 159 passed.
- `npx playwright test tests/browser/fleet.spec.js` — 76 passed across desktop 1440×1000 and mobile
  390×844, including 205-row History pagination, a 205-message conversation, invalid cursors, and
  optimistic input/quick-response feedback below 100 ms.
- `python3 tests/live_api_smoke.py` — passed against 26 live sessions and seven Codex models.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377 FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 12 passed across both viewports with no console or network failures.
- `python3 tests/search_benchmark.py` — all size, build-time, cold/warm-query, and live-lag gates
  passed on the deterministic large corpus.
- `node tests/browser_baseline.js http://127.0.0.1:8377/ 8` — desktop and mobile results recorded
  below; browser poll payload measured 158,937 bytes.
- `python3 tests/perf_baseline.py --samples 40 --context-samples 10 --history-samples 20
  --search-samples 40 --search-query fleet --local-action-auth --skip-corpus` — API, engine, search,
  database, payload, and memory results recorded below.

Commit: `Optimize fleet indexing and interaction latency`.

Exit: before/after measurements recorded below, no correctness regression, and no hidden background
work on the HTTP hot path.

### M10 — Bug-fix and resilience pass

Completed 2026-07-16. The final audit covered every route family, provider branch, persistent
mutation, large paged surface, empty/error/loading/stale state, and desktop/mobile destination.

Hardening and bug fixes:

- The HTTP boundary now limits request bodies, times out stalled clients, contains unexpected route
  failures, survives client disconnects, and never logs the local action URL/token. Conversation,
  closed-session, and subagent pagination share one newest-first contract.
- Settings updates are staged under a lock and written only after strict validation. Unknown fields,
  orphan companion fields, control characters, overlong IDs, booleans disguised as numbers, and
  invalid notification/digest/budget/outbox values are rejected without partially changing memory
  or disk.
- Confirmed corrupt search indexes and shared ledgers are preserved under unique quarantine names.
  Fleet starts clean with an explicit recovery warning instead of taking down both providers or
  exposing raw SQLite details.
- A final live soak uncovered a separate concurrency defect: the scan loop's long-lived SQLite
  connection was also used by threaded Insights requests. Concurrent browser/API traffic could
  poison that connection until restart with `database disk image is malformed` or `file is not a
  database`, even while the file passed `PRAGMA quick_check`. HTTP ledger reads now use independent,
  short-lived connections. A poisoned-scan-connection regression test and a 120-request concurrent
  live refresh soak both pass with no new daemon errors.
- New Claude registry sessions are now visible and interactive before their first transcript row is
  written. They report an honest empty starting conversation and unavailable cost/context rather
  than disappearing until the first message.
- Newly created managed Codex threads remain visible while `thread/list` propagation catches up.
  The adapter preserves runtime-proven state instead of briefly dropping the session.
- Large live, closed, and subagent conversations load 50 newest messages at a time, preserve scroll
  position while prepending history, and isolate subagent caches by parent plus child ID. Load and
  recovery failures are visible rather than silently retaining stale content.
- Workstream refreshes use a stable session digest and eight-second repository/budget bucket. The UI
  polls this derived destination every eight seconds instead of rebuilding it on every fleet tick.

Deterministic verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 173 passed. Coverage includes malformed
  protocol data, Codex `thread/list` propagation races, all mapped conversation/state fixtures,
  same-second revisions, invalid/stale/duplicate questions and approvals, large conversations,
  one-provider outages, atomic settings, hostile action bodies, path validation, disconnects,
  timeouts, database quarantine, poisoned SQLite connections, and Claude sessions with no transcript.
- `npx playwright test tests/browser/fleet.spec.js` — 82 passed across desktop 1440×1000 and mobile
  390×844. This includes all shared-provider session states, optimistic sends/answers, approvals,
  files, commands/skills, subagents, History, settings, Outbox, Insights, responsive navigation,
  large paged conversations, ledger recovery, and the pre-transcript Claude state.
- `python3 tests/search_benchmark.py` remained within every deterministic large-corpus gate from M9.
- `git diff --check` passed.

Live and recovery verification:

- `python3 tests/live_api_smoke.py` — passed against 27 live sessions, seven Codex models, and 54
  provider commands after the request-local SQLite fix.
- `FLEET_DASH_LIVE_URL=http://127.0.0.1:8377 FLEET_DASH_LIVE_AUTH=1 npx playwright test
  tests/browser/live.spec.js` — 12 passed across both viewports with no console or network failures.
- `python3 tests/live_refresh_soak.py` — 120 concurrent read requests across fleet, Insights,
  History, Briefing, Search, and conversation endpoints passed with eight workers and no new log
  errors.
- `python3 tests/live_restart_smoke.py` — restart during an active Codex turn recovered both
  providers and the exact thread, then archived the test thread.
- The safe provider matrix passed discovery, Plan/Default, send, interruption, structured question,
  artifact, native compact, quota/context, restart, Claude focus/question/permission/mute/close,
  temporary-repository commit/push, and outbox create/cancel flows. The real Codex subagent run
  produced and discovered a child lifecycle with the default model; two earlier model attempts
  returned prose without spawning and were not counted as lifecycle success. The live approval run
  auto-denied the protected action before an approval request surfaced, verified the protected path
  remained unchanged, and relies on deterministic protocol/UI coverage for every approval shape and
  decision.
- `python3 tests/perf_baseline.py --samples 40 --context-samples 10 --history-samples 20
  --search-samples 40 --search-query fleet --local-action-auth --skip-corpus` measured fleet p50/p95
  2.944/3.521 ms, context 0.707/0.821 ms, History 3.277/3.726 ms, and live search
  0.821/1.340 ms. The live fleet response was 165,452 bytes and a 50-message context page was 17,499
  bytes.
- `node tests/browser_baseline.js http://127.0.0.1:8377/ 8` measured first useful render p50/p95
  51.945/91.734 ms desktop and 53.842/58.278 ms mobile. The browser poll payload was 165,539 bytes.
- Repeated `launchctl kickstart -k gui/501/com.benjaminfeder.fleet-dash` restarts settled at HTTP 200.

Exact remaining limitations:

- This ChatGPT desktop task still exposes no in-app Browser surface. The installed Browser skill was
  initialized, and the current desktop log identifies a helper/socket peer rejection as
  `untrusted-code-signing-identity`. This is not evidence that the reinstalled ChatGPT application
  signature is invalid. Repository-native and live Playwright provide the deterministic visual
  coverage until that desktop-runtime connection is repaired outside Fleet Dash.
- External ChatGPT Desktop/IDE Codex threads are intentionally view-only unless they were explicitly
  started against Fleet's App Server. Fleet will not claim safe control over another App Server's
  in-memory thread.
- Codex account quota and context tokens are exposed, but the provider does not supply exact local
  per-session USD cost through the current App Server path. Fleet renders cost as unavailable rather
  than zero. Claude and Codex lifetime/account scopes remain provider-specific and are labeled.

Commit: `Harden Fleet platform workflows`.

Exit: no known P0/P1 defects, every discovered lower-severity defect was fixed, the daemon was
reloaded, fresh live console/network and daemon logs were clean, and the branch was pushed.

### M11 — Dashboard UX and responsiveness

Completed 2026-07-16. The detailed product decisions and implementation ledger live in
[`docs/roadmaps/dashboard-ux-latency-roadmap.md`](dashboard-ux-latency-roadmap.md).

- Now puts state totals in filter chips, adds a flat active-subagent view, moves Fleet Briefing above
  Pinned, and moves provider/account usage into a compact warning-aware chip. Pinned order is the
  stored insertion order; new pins append at the bottom and failed mutations restore that exact order.
- Desktop navigation can sit left or right. Chat/settings/evidence overlays stack and restore
  correctly. Markdown and full-chat headers no longer duplicate operational metadata.
- Composers use Return for newlines and modified Return/explicit Send for delivery. Model changes,
  session startup, messages, questions, relay, Terminal, Outbox, Settings, search/history pagination,
  commands, files, Briefing, and pinning all paint immediate progress and retain a recovery path.
- Claude permission modes and secondary-worktree cleanup are capability/state gated. Bypass and
  dirty force removal retain separate high-warning confirmations and bounded risk lists.
- Main, subagent, completed-agent, and closed-session chats render an adaptive status strip from
  bounded incremental context/cache/cost state and cached fixed-argv Git evidence.
- Poll, Insights, forecast, history, search, and command races are sequence/abort guarded. Unchanged
  conversations retain their DOM. Live main/subagent context reads use parent-scoped immutable
  scan-published snapshots instead of waiting behind the fleet-wide Tail fold lock.
- The newer Claude `task-notification` terminal states (`completed`, `killed`, `failed`) now settle
  background agents immediately unless newer child output proves that task ID resumed.

Verification:

- `python3 -m unittest discover -s tests -p 'test_*.py'` — 185 passed.
- `npx playwright test tests/browser/fleet.spec.js tests/browser/latency.spec.js` — 112 passed across
  desktop and mobile. Every named interaction surface has 20 first-feedback samples below 100 ms;
  fixture poll/native completion stays below 250 ms.
- `python3 tests/perf_baseline.py ... --assert-contract` — fleet/main-context/subagent-context/
  History/search/action-ping client p95 measured 11.107/4.864/3.580/5.533/1.556/0.962 ms on the
  live corpus; each server-route p95 was below 5 ms.
- `node tests/browser_baseline.js ... 16 --assert-contract` — first useful render p95 was 236.828 ms
  desktop and 241.110 ms mobile; render p95 was 5.2/5.9 ms and poll p95 60.7/59.7 ms.
- `python3 tests/search_benchmark.py` passed every 100k-message/2k-source gate.
- `python3 tests/live_api_smoke.py`, `python3 tests/live_refresh_soak.py`, and the disposable
  restart-during-turn smoke passed. The authenticated live browser matrix passed 12/12 after its
  assertion distinguished intentional stale-request `ERR_ABORTED` cancellation from outages.

Exit: all M11 catalogue rows and the detailed dashboard catalogue have implementation, failure-path,
responsive, deterministic, and live evidence. The daemon is running the M11 engine.

### M12 — Notification Center and Web Push

Implemented 2026-07-16/17; N0a/N0b and N1–N6 are complete. The
approved implementation contract, requirements catalogue, migration order, and release gates live in
[`docs/roadmaps/push-notification-redesign-roadmap.md`](push-notification-redesign-roadmap.md).

- **N0a/N0b — Compatibility prototype and baseline (complete):** runtime/latency and encrypted-
  request evidence passed, followed by installed iPhone/macOS app-closed delivery, icon badges,
  and exact event deep links before N5 changes production triggers.
- **N1 — Canonical event and device stores:** replace truncated/time-bucket identities with durable
  event lifecycle, device, read, snooze, mute, and leased-delivery records.
- **N2 — Installable Fleet PWA:** add the manifest, private-data-safe service worker, permission and
  subscription repair flows, per-device setup, and honest health states.
- **N3 — Persistent Web Push delivery:** add the supervised fixed-protocol Node helper, VAPID,
  endpoint confinement, durable retry, expired-subscription handling, and redacted diagnostics.
- **N4 — Notification Center consolidation (complete):** desktop/mobile Notifications owns exact
  canonical event detail, per-device read state, snooze/wake/mute/retry controls, delivery problems,
  badges, filters/history, and Briefing; Now retains its live Action Inbox.
- **N5 — Production policy and actions (complete):** enabled the selected event policy, one
  reminder, minimal payloads, exact deep links, and one-use Snooze/Mute capabilities; deterministic
  privacy/replay gates and installed macOS/iPhone production delivery passed.
- **N6 — Legacy retirement and release (complete):** ntfy is fixed-copy manual-test-only; migration,
  diagnostics, docs, restart/saturation/privacy/latency gates, and real app-closed delivery passed.

Exit: Web Push is Fleet's only automatic external transport; Notification Center is the canonical
durable inbox; no push discloses work content or performs a consequential action; real iPhone and
macOS app-closed delivery, exact deep links, restart recovery, privacy, and latency gates pass.

Final N6 evidence: 223 Python and 11 Node tests; ten targeted desktop/mobile notification and named
latency checks; live API/Notification/push/privacy smokes; `/api/fleet` 26.143 ms p95, Notification
reads 4.981 ms p95, no-op actions 1.532 ms p95, canonical projection 2.202 ms p95, and enqueue
2.063 ms p95. Installed iPhone/macOS delivery and final native interaction were user-confirmed.

### M13 — Provider control, canonical composer, and global notification controls

Implemented on `fix/provider-control-recovery` on 2026-07-17. The detailed requirements, settled
choices, source mapping, and still-open release gates live in
[`docs/roadmaps/provider-control-composer-notification-settings-roadmap.md`](provider-control-composer-notification-settings-roadmap.md).

- Claude background actions and close use Claude Code's official attach/detach/stop commands through
  a bounded private PTY; foreground iTerm injection and its proven question key map are unchanged.
- Codex control authority is scoped to one App Server connection generation. Direct sends made while
  an owned active turn is temporarily uncontrolled enter the central Outbox with an idempotency key;
  reconnect safely chooses steer, next turn, or continued waiting from authoritative lifecycle state.
  A definitive provider rejection that the recorded turn no longer exists clears stale Working state
  and starts the exact payload once; an active-turn mismatch queues instead. Fullscreen questions use
  a per-question, vertically resizable drawer whose independent reading position survives polling.
- Full chat and Markdown use one composer renderer. The viewer contains no second conversation;
  mobile keyboard/picker geometry, opaque backdrop, touch bounds, persisted text/images, and
  offline/provider queues have deterministic desktop/mobile coverage.
- Settings is divided into Notifications, Devices & delivery, Sessions, Appearance, Budgets &
  spawning, and Advanced. Global notification rules cover all 12 canonical kinds and the scheduler
  revalidates revisions across quiet hours, snooze, mute, restart, DST, and worker leases.
- Verification on the implementation branch: 269 Python tests; 11 Node Web Push/privacy tests; 68
  applicable desktop and 69 applicable 390×844 browser checks; two complete named latency-inventory
  runs; isolated staging API/read-only browser smoke; and targeted screenshot inspection. Three
  unrelated mobile checks timed out only during the 12-minute serialized full-suite run and each
  passed twice immediately in isolation. The
  in-app visual-QA browser backend was unavailable, so
  installed iPhone keyboard/photo and macOS+iPhone cadence behavior remain explicit staging gates.

## Verification matrix

Each completed milestone ran its relevant subset; M9 and M10 ran the full pre-M12 matrix. M12 uses
the expanded deterministic, restart, saturation, privacy, latency, and real-device matrix in its
dedicated roadmap.

- Python unit and provider-fixture suite.
- Protocol/failure harnesses and index parser fixtures.
- Engine/API tests with fake Claude, Codex, Git, GitHub, ntfy, and filesystem sources.
- Playwright desktop 1440×1000 and mobile 390×844, including focused screenshots.
- Authentication/read-only testing for every read and mutation route.
- Safe-path and hostile-input tests for transcript, artifact, repository, cursor, and action inputs.
- Opt-in live Claude and Codex session matrix.
- Daemon/App Server/index-worker crash and restart soak.

## Optimization results

Populate in M0 and M9. Measurements use the same real corpus and deterministic benchmark fixtures.

M0 was recorded on 2026-07-16 against the launchd daemon on this machine. API results use 40 fleet
requests and three complete context requests for the pinned 159 KB desktop Codex thread. Engine
timing uses 22 real two-second polls after restart. Browser results use eight new Chromium pages per
viewport and stop at the first visible usage/session surface. RSS is the launchd Python process after
one minute. Corpus counting reads every local Claude/Codex JSONL source but does not parse message
content.

| Metric | M0 baseline | M9 result | Gate |
| --- | ---: | ---: | ---: |
| Engine scan p50/p95 | 140.184 / 902.369 ms | 127.783 / 293.487 ms | no regression from indexing |
| `/api/fleet` p50/p95 | 9.296 / 10.994 ms | 2.850 / 4.349 ms | baseline + <5 ms p95 |
| `/api/context` p50/p95 | 2.157 / 2.676 ms | 1.028 / 1.483 ms | improve or unchanged |
| Search warm p50/p95 | n/a | 0.004 / 0.011 ms fixture; 1.321 / 2.451 ms live HTTP | <75 ms p95 |
| Search cold p50/p95 | n/a | 14.710 / 15.441 ms | <150 ms p95 |
| Live index lag p95 | n/a | 0.058 s measured append | <2 poll intervals |
| Initial index wall time | n/a | 2.022 s, background worker | background only |
| Desktop first useful render p50/p95 | 77.589 / 140.699 ms | 54.075 / 97.534 ms | improve or unchanged |
| Mobile first useful render p50/p95 | 73.206 / 75.661 ms | 55.811 / 90.511 ms | improve or unchanged |
| Mobile input-to-feedback | <100 ms deterministic gate; real instrumentation pending | <100 ms deterministic gate; runtime instrumentation added | <100 ms |
| Poll payload bytes p50 | 768,834 bytes | 159,249 API; 158,937 browser | bounded with pagination |
| Context payload bytes | 159,125 bytes | 18,256 bytes per 50-message page | paginate large conversations |
| Daemon steady-state RSS | 26,032 KiB | 48,688 KiB current; 194,944 KiB process peak | measured and justified |

The M9 RSS value is a direct process measurement after the full live and browser matrix. The peak is
the operating-system process high-water mark and includes startup, the one-time search-count schema
migration, indexing, and browser/live test traffic; it is not the steady-state reading. M10 retains a
restart/soak check to distinguish a stable larger working set from a leak before final handoff.

Real local corpus baseline:

| Source | JSONL files | Rows | Bytes |
| --- | ---: | ---: | ---: |
| Claude projects | 3,212 | 536,706 | 2,639,704,900 |
| Codex sessions | 47 | 25,894 | 87,897,067 |
| Total | 3,259 | 562,600 | 2,727,601,967 |

M3 live verification found that the unfiltered “recent results” Search query can take about 5.2
seconds while the real multi-gigabyte index is actively growing, although an actual `fleet` text
query returned in about 103 ms in the same check. M3 stopped issuing that blank query automatically;
Search now waits for text or a filter. M9 should add an indexed recent-results path before any blank
browse behavior is reintroduced and should reduce read contention during active indexing.

Repeat with:

```bash
python3 tests/perf_baseline.py --samples 40 --context-samples 10 --history-samples 20 \
  --search-samples 40 --search-query fleet --local-action-auth --skip-corpus
node tests/browser_baseline.js http://127.0.0.1:8377/ 8
```

## Explicit non-goals

- Merging Claude and Codex sessions or pretending they share runtime state.
- Automatic cross-provider agent spawning without an explicit user handoff.
- Full task/kanban/dependency management in Workstreams.
- Indexing arbitrary repository files or remote cloud conversations absent locally.
- Fabricating Codex currency costs.
- Automatic commits, pushes, PR readiness, merges, or active-turn interruption.
- Recurring schedules, arbitrary workflow predicates, chained automations, shell hooks, or automatic
  approvals. Outbox authorization applies only to its exact saved message intent.
- Multi-user permissions, hosted deployment, or replacing provider-native trust controls.
