# Dashboard UX and latency roadmap

Status: Active  
Branch: `feat/dashboard-ux-latency`  
Base: `f75567f` (`main`, 2026-07-16)  
Scope: Fleet Dash desktop and mobile interaction quality, session controls, and full-screen
operational telemetry.

This document is the implementation ledger for the dashboard UX and responsiveness pass. A feature
is complete only when its requirement, failure path, responsive behavior, and latency gate are all
verified. Provider completion time is measured separately from Fleet's own response time.

## Product contract

1. A tap or click produces visible feedback within 100 ms p95.
2. If the requested work is not complete within 100 ms, the existing Fleet spinner/progress pattern
   appears and names the pending operation.
3. Routine local-daemon actions complete within 250 ms p95. Provider startup, model work, Git network
   operations, and native TUI transitions are timed separately and never hidden behind a frozen UI.
4. Optimistic UI remains recoverable: a failed send or spawn retains the user's text and offers an
   explicit restore or retry action. Fleet never claims provider confirmation before it exists.
5. Telemetry is provider-honest. Unknown effort, context capacity, compaction threshold, cache
   counters, or exact cost is omitted or labeled unavailable; it is never inferred from unrelated
   data or rendered as zero.
6. Mutating provider tests use purpose-created disposable Claude/Codex sessions and disposable Git
   worktrees. Existing real sessions are read-only during development.

## Settled design decisions

### Now hierarchy and filtering

- Fleet Briefing moves above the global Pinned block.
- The separate `"N need you · N working · N available"` totals line is removed. State counts live in
  their matching command-bar filter chips.
- `Needs you` counts distinct sessions, matching the number of session cards the filter displays.
- A new `Subagents N` chip selects a flat list of active subagents. One card represents one child and
  shows the parent project/session breadcrumb, agent type, model, state, and latest activity. Tapping
  it opens that subagent directly.
- Active means every child not in the existing authoritative `done` or `ended` states. This includes
  a stalled child because it still requires supervision.
- Now remains an action queue. These filters change presentation only and do not alter
  `Engine.organize_session` state semantics.

### Usage placement

Three approaches were considered:

1. **Selected — command-bar Usage chip.** Remove the large always-on Usage slab. A quiet `Usage` chip
   opens the full provider/account detail in a desktop popover or mobile sheet. The chip normally has
   no number; at 70% it shows the most urgent percentage in amber, and at 90% in red.
2. A collapsed usage summary row above the queues. This preserves ambient detail but still spends
   permanent vertical space on information that is not normally actionable.
3. Move all detail to Insights and show only alerts on Now. This is clean but makes routine quota
   checks needlessly indirect.

When multiple selected accounts or providers are visible, the chip uses the account/window closest
to its limit. The detail surface still shows every selected account, active-account marker, reset,
scope, and local lifetime-token qualification exactly as today.

### Session cards and shell

- An ordinary Claude card has one chat affordance: tapping its header. The redundant chat primary
  button is removed. The native iTerm focus action remains and is renamed `Terminal`.
- Contextual primary actions such as `Respond`, `View response`, and `Attach` remain; this change does
  not remove an action required by the session state.
- Desktop Settings can place the main navigation rail on the left or right. The choice is stored per
  browser/device. Mobile keeps the bottom navigation.
- The Markdown viewer retains a slim toolbar with close, filename, theme, and file actions. It drops
  the session title, project/branch/model header, and separator.
- Settings and other full-screen overlays opened from chat stack above the current chat. Closing the
  top overlay restores the exact chat scroll position and whether the `Why Fleet put this here`
  evidence panel was open.

### Composers and session startup

- Every message composer is multiline. Return inserts a newline and never sends.
- Command-Return sends on macOS; Control-Return sends on other desktop platforms. Mobile and
  unmodified-keyboard use the explicit Send button.
- Slash-command suggestions may be chosen explicitly, but Return must not accidentally submit the
  composer or fire a highlighted native TUI command.
- Changing the New Session model updates the selected value immediately and never waits for the
  forecast request. Forecast loading uses the existing spinner pattern and stale forecast results
  cannot overwrite a newer selection.
- Starting a session immediately opens a provisional full-screen chat, creates a provisional Now
  card, and renders the initial user message optimistically with `Starting session…`.
- The canonical provider session replaces the provisional object in place. Failure leaves the text
  recoverable and offers retry; it never silently drops the provisional row.

### Claude permission mode

- The normal choices are Manual, Auto, Accept Edits, and Plan.
- `Don't ask` lives under an Advanced group.
- `Bypass permissions` is shown only when the current Claude runtime permits it and requires a
  separate high-warning confirmation every time it is selected.
- The current mode is visible in session metadata. It may be changed from the full-screen overflow
  menu and expanded card details, not from an always-visible card selector.
- A change is disabled while the session is busy, waiting on a question/permission, closed,
  view-only, or otherwise unsafe to inject into.
- Fleet derives the current value from Claude's `permission-mode` transcript rows. The mutation uses
  a fixed server-side mode allowlist and a disposable-session-verified native Claude interaction;
  it never accepts client-supplied shell or arbitrary key sequences.

### Closing sessions and secondary worktrees

- Closing a session whose working directory is a secondary Git worktree offers:
  `Close and preserve worktree` and `Close and remove worktree`.
- The primary worktree never receives a removal option. Removing a worktree never deletes its
  branch.
- Clean removal is the normal destructive choice. If the worktree is dirty, normal removal is
  disabled and the confirmation lists modified, deleted, staged, and untracked paths.
- A separate red `Force remove dirty worktree` choice is available behind a second high-warning
  confirmation. It lists the dirty paths and a bounded count/list of ignored files that removal
  would also erase.
- Removal is refused while another live Fleet session uses the same worktree. Git paths are
  canonicalized and commands are composed from validated argv, never shell text.
- If provider close succeeds but cleanup fails, the session remains closed and Fleet reports the
  cleanup failure with the preserved worktree path. It never retries destructive cleanup silently.

### Full-screen operational status strip

The strip is docked directly above the composer in main-session and subagent full-screen chat.
Desktop begins with four compact rows:

```text
⎇ branch ↑ahead↓behind │ worktree
model · effort │ Ctx: used% → tokens until compact
cache read% │ cache write · spikes · peak │ tree/session cost · turn cost
CW ▁▁▁▂▁▁▃▁…
```

- Git ahead/behind preserves the current iTerm statusline definition: compare with the last-fetched
  `origin/main`; no fetch occurs on the render or request path. Missing data is omitted.
- The middle row shows model/effort, context fullness, and amount until compaction only when the
  provider exposes enough information to calculate each value honestly.
- Cache telemetry preserves the current Claude statusline semantics: last 50 changed CacheWrite
  values approximate turns; a write above 20k is a spike; count and peak are retained; current
  read-hit ratio and turn-cost delta use the latest usage event.
- A main-session total is the whole session tree: main plus all subagents. Tapping the total opens a
  breakdown. A subagent full view shows that subagent's own cost only.
- On mobile the default collapsed view has two rows: branch/worktree, then
  model/effort/context/until-compact. Tapping expands cache, spike, cost, and graph rows.
- Closed sessions and completed subagents freeze their final telemetry and display it read-only.
- Full-chat headers keep only the session title and controls. Operational metadata moves into the
  strip and is not duplicated above it.
- Telemetry is maintained incrementally in bounded Tail/adapter state. No full transcript scan or
  Git fetch may enter an HTTP hot path.

## Requirement catalogue

| ID | Requirement and acceptance condition | Milestone |
| --- | --- | --- |
| NOW-001 | Fleet Briefing renders before Pinned at desktop and mobile widths, including empty states. | M1 |
| NOW-002 | State totals leave the standalone header and appear in All/Needs you/Working/Available chips; Needs you equals distinct visible sessions. | M1 |
| NOW-003 | Subagents chip count and flat active-child cards use authoritative lifecycle states and open the correct child. | M1 |
| USE-001 | Usage is a command-bar chip; full detail opens as popover/sheet; only the worst selected account/window appears at 70%/90% warning. | M1 |
| SHELL-001 | Navigation rail supports per-device left/right placement on desktop without changing mobile navigation. | M1 |
| SHELL-002 | Overlay stacking and close restore chat scroll/evidence state; menus are never below evidence. | M1 |
| VIEW-001 | Markdown viewer toolbar shows filename/actions only and no session metadata separator. | M1 |
| CARD-001 | Ordinary Claude cards have header-to-chat plus one clearly named Terminal action, with contextual state actions preserved. | M1 |
| INPUT-001 | All message composers are multiline; Return adds newline; Command-Return/Control-Return send; explicit Send remains. | M2 |
| SPAWN-001 | Model selection paints immediately, forecast work is cancellable/versioned, and loading is visible after 100 ms. | M2 |
| SPAWN-002 | New session has immediate provisional card/chat/message, canonical in-place replacement, and recoverable failure. | M2 |
| PERM-001 | Current Claude permission mode is parsed and shown; safe allowed modes can be changed only from allowed session states. | M3 |
| PERM-002 | Don't ask is advanced; bypass is capability-gated, server-allowlisted, and separately confirmed. | M3 |
| CLOSE-001 | Secondary-worktree close offers preserve/remove; primary worktree and branch deletion are impossible. | M3 |
| CLOSE-002 | Dirty removal is disabled normally; explicit force confirmation lists dirty/ignored risk and shared-live-session cleanup is refused. | M3 |
| STAT-001 | Main and subagent full-screen views render the adaptive desktop/mobile status strip with honest omission. | M4 |
| STAT-002 | Git, context/compact, cache/spike/graph, tree/turn cost, mobile expansion, and closed freeze follow the settled semantics. | M4 |
| LAT-001 | Every interactive control has immediate pressed/optimistic/loading feedback and a visible recoverable failure state. | M1–M5 |
| LAT-002 | Visible feedback is <100 ms p95 and routine local completion is <250 ms p95 on both reference viewports. | M5 |
| LAT-003 | Baseline/final timings cover the full interaction inventory; provider/native completion is reported separately. | M0/M5 |
| DOC-001 | README, CLAUDE.md invariants, this ledger, and any superseded platform-roadmap decisions agree with shipped behavior. | Every milestone |

## Interaction latency inventory

Each flow is measured from pointer/key input to the first visible state change and, separately, to
Fleet-local completion. Async provider/network completion is a third measurement when applicable.

| Surface | Flows that must be measured and receive waiting/failure UI |
| --- | --- |
| Navigation | Every destination, browser back, mobile More, saved views, left/right rail change |
| Now | Text/state filters, subagent filter, Briefing, Pinned, queue/card expansion, peek expansion |
| Session chat | Open/close, pagination, evidence, overflow, theme, files, commands, composer, send, restore, stop, attach/focus/reopen |
| Native requests | Question answers, permissions, approval/elicitation decisions, dismiss, relay, interrupt |
| Session lifecycle | New form open, provider/model/effort/worktree choice, forecast, start, provisional reconciliation, close/archive, worktree cleanup |
| Subagents | List/filter, open, pagination, relay, parent-turn stop warning, completed telemetry |
| Search/history | Query, filter, pagination, result open, context load, index rebuild |
| Workstreams/repository | Open, refresh, Git evidence, commit, push, draft PR, mark-ready, outcome/error states |
| Insights/usage | Usage chip/detail, account switching display, budgets, date/window changes |
| Settings | Open/close/restore, every toggle/select/input save, validation and server error |
| Outbox/handoff | Open, create/edit/cancel/retry/send-now, target validation, preview and accept |
| File/Markdown | Open, render, theme, close, download/copy action feedback, missing/denied file |
| Poll/recovery | Stale banner, daemon recovery, optimistic-row canonical confirmation, concurrent refresh |

Instrumentation and tests must distinguish synchronous render work, HTTP duration, Apple-event/native
injection, and provider work. The audit records p50/p95, payload changes, and any intentional debounce.
Debounce is allowed only where the user already sees the updated local value immediately.

## Roadmap

### M0 — Baseline and plan

Status: In progress

Recorded unchanged-branch live baseline on 2026-07-16:

- `/api/fleet`: 7.914/46.317 ms client p50/p95; 192,986-byte median response.
- `/api/context`: 153.122/884.342 ms p50/p95 for the selected 26,933-byte conversation. This is
  already outside the local-completion contract and is an explicit M5 target.
- History: 10.763/21.839 ms p50/p95. Search: 5.351/102.497 ms p50/p95.
- First useful render: desktop 148.770/297.741 ms p50/p95; mobile 148.506/205.205 ms.
- Once data arrived, render itself was 5.2 ms desktop and 5.8 ms mobile p95; initial latency is
  therefore dominated by page/poll startup rather than DOM rendering.
- Engine scan p50/p95 was 395.185/2,398.911 ms. Context requests contend with Tail folding and must
  be separated from that stateful scan path without violating `scan_lock` invariants.

- Capture the current browser/API baseline using the same live corpus and both 1440×1000 and
  390×844 viewports used by the existing performance suite.
- Add interaction timing helpers/tests without adding a visible diagnostics burden to the product.
- Freeze this requirement catalogue before dependent implementation.

Exit gate: baseline artifacts include first-feedback and completion timings for every inventory row,
with unsupported destructive live paths represented by deterministic fixtures and disposable tests.

### M1 — Now, shell, cards, viewer, and overlays

Status: Complete · 2026-07-16

- Implement `NOW-001`–`NOW-003`, `USE-001`, `SHELL-001`–`SHELL-002`, `VIEW-001`, and `CARD-001`.
- Keep state mapping server-authoritative; derive chip counts and subagent presentation from one
  snapshot so displayed counts cannot disagree with filtered results.
- Preserve focus, scroll, back behavior, responsive layout, and reduced-motion behavior.

Exit gate: deterministic desktop/mobile browser tests cover every empty/loading/error/populated state;
filter and overlay feedback meet the 100 ms p95 gate.

Implemented `NOW-001`–`NOW-003`, `USE-001`, `SHELL-001`–`SHELL-002`, `VIEW-001`, and
`CARD-001`. The full deterministic browser pass reached 84/90 before stopping only on six stale
expectations for the intentional `Attach` capitalization and removed redundant Claude primary
button. Those three affected flows were corrected and rerun on both viewports; all six passed. The
new M1-focused paths passed on desktop and mobile, including a real scroll-restoration race caught
and fixed by the test. The unchanged Python suite remains 173/173.

### M2 — Composer and optimistic startup

Status: Pending

- Implement `INPUT-001` and `SPAWN-001`–`SPAWN-002` using the existing optimistic conversation-row
  confirmation contract.
- Separate selected-form state from forecast state. Abort or ignore obsolete forecast responses.
- Use a stable client-generated provisional ID and one canonical reconciliation path for Claude and
  Codex.

Exit gate: multiline keyboard tests pass across desktop/mobile; start/model paths visibly respond
within 100 ms; delayed/success/failure/race fixtures retain the exact user text once.

### M3 — Claude permissions and worktree-safe close

Status: Pending

- Implement `PERM-001`–`PERM-002` and `CLOSE-001`–`CLOSE-002`.
- First verify the installed Claude CLI's exact permission-mode transition in a disposable session.
- Add bounded Git worktree inspection and server-side validated cleanup actions. Keep provider close
  and optional filesystem cleanup as separately reported outcomes.

Exit gate: unit/API/browser coverage proves mode/state/capability gates and path/argv safety. Live
Claude permission changes and clean/dirty worktree cleanup pass only in purpose-created disposable
sessions/worktrees; branches and primary worktrees survive.

### M4 — Adaptive operational status strip

Status: Pending

- Implement `STAT-001`–`STAT-002` with bounded incremental telemetry for both providers.
- Reuse the established Claude cache-write graph and spike calculations. Keep Codex/closed views
  honest when fields are unavailable.
- Remove duplicated operational metadata from the full-chat header.

Exit gate: deterministic token/cache/cost/Git fixtures cover missing, partial, live, compacting,
multi-agent, completed, and closed states at both viewports. No transcript scan or Git fetch appears
in request profiling.

### M5 — Full responsiveness audit and release gate

Status: Pending

- Execute `LAT-001`–`LAT-003` across the full inventory, not only the changed features.
- Remove avoidable blocking work and render churn. Add immediate local state or the shared spinner to
  every remaining slow action.
- Compare baseline and final p50/p95 on the same fixture/live corpus. Treat a regression in an
  unrelated flow as a failure of this pass.
- Reconcile `README.md`, `CLAUDE.md`, `docs/platform-roadmap.md`, and this ledger.

Exit gate: all deterministic tests pass; safe live API/browser/refresh/restart checks pass; disposable
Claude/Codex/worktree tests pass; `git diff --check` is clean; every catalogue ID has current source
and test evidence.

## Verification matrix

Run proportionally after each milestone and in full at M5:

```text
python3 -m unittest discover -s tests -p 'test_*.py'
npx playwright test tests/browser/fleet.spec.js
python3 tests/search_benchmark.py
python3 tests/perf_baseline.py --samples 40 --context-samples 10 --history-samples 20 \
  --search-samples 40 --search-query fleet --local-action-auth --skip-corpus
node tests/browser_baseline.js http://127.0.0.1:8377/ 8
python3 tests/live_api_smoke.py
FLEET_DASH_LIVE_URL=http://127.0.0.1:8377 FLEET_DASH_LIVE_AUTH=1 \
  npx playwright test tests/browser/live.spec.js
python3 tests/live_refresh_soak.py
python3 tests/live_restart_smoke.py
git diff --check
```

Engine/server changes require a daemon restart before live verification. Static assets do not.
Live mutation uses only the repository's explicit disposable test protocol. No development test
injects into an existing user session.

## Progress ledger

| Milestone | State | Evidence |
| --- | --- | --- |
| M0 | In progress | Roadmap created; live API/browser baseline recorded; interaction harness pending |
| M1 | Complete | Focused M1 desktop/mobile checks passed; affected legacy flows passed; Python 173/173 |
| M2 | Pending | — |
| M3 | Pending | — |
| M4 | Pending | — |
| M5 | Pending | — |
