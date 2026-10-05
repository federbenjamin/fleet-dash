# fleet-dash agent guide

`AGENTS.md` is the canonical development guide. `README.md` is the short public
overview with screenshots. Keep user-facing setup and behavior in
`docs/reference.md`, visual rules in `design-system/`, and session-placement
details in `docs/session-organization.md`. Do not turn this file back into a
changelog, incident log, roadmap, or exhaustive file index.

Update `docs/reference.md` with user-visible behavior changes, and `README.md`
when a headline feature or a screenshot goes stale (`docs/images/` is shot from
the browser fixture server, never from a live dashboard). Update this file only
when a durable development or safety constraint changes.

## Project shape

fleet-dash is a Python `ThreadingHTTPServer` plus a no-build browser app.

```text
server.py                  HTTP routes and process startup
fleetdash/
  paths.py                 instance paths; patch this module in tests
  config.py                defaults, validation, pricing, shared constants
  tail.py                  incremental Claude transcript fold
  placement.py             provider-neutral queue placement
  screen.py                pure Claude TUI screen classifier
  engine.py                Engine composition and initialization
  engine_scan.py           registry scan and published snapshot
  engine_act.py            action validation and native dispatch
  engine_context.py        conversations, files, prompts, screen observation
  engine_transport.py      transport selection and tty/process discovery
  engine_tmux.py           tmux read/write/spawn/focus
  engine_spawn.py          spawning, handoff, applet exchange, settings
  engine_ledger.py         closed sessions, history, usage, handoffs
  engine_notify.py         Outbox, notifications, Web Push actions
  engine_receipts.py       idempotent native-action receipts
  engine_worktree.py       preview-ticketed worktree cleanup
  engine_uploads.py        private phone-image uploads
  engine_staging.py        staging capability isolation
  codex_runtime.py         Codex runtime lifecycle and migration
  codex_protocol.py        App Server JSON-RPC transport
  codex_adapter.py         Codex normalization, ownership, capabilities
  codex_observer.py        read-only external Codex rollout observer
  briefing*.py             notification/budget/device store and scheduler
  outbox.py                durable send/schedule queues
  search_index.py          isolated transcript search worker
static/js/                 16 raw ES modules; no bundler
static/fleet.css           browser styles
static/sw.js               service worker and offline boundary
dashboard.html             application shell
hooks/pending-capture.py   Claude prompt hook
tests/                     unittest, Playwright, Node, and opt-in live smokes
```

Production is `~/.claude/fleet-dash-prod` on port 8377 with state in
`~/.claude/fleet-dash-prod-state`. This checkout is staging on port 8378 with
state in `~/.claude/fleet-dash-staging-state`. Both read shared captures from
`~/.claude/fleet-dash-capture`.

The scan folds Claude JSONL under `scan_lock`, publishes bounded immutable
projections, then completes provider-neutral work outside that lock.
`scan_serialize` permits only one scan at a time. A low-priority child process
owns search indexing so large-corpus parsing never contends with HTTP threads for
the Python GIL.

## Retained invariants

Do not reuse or renumber IDs. Gaps are retired rules; any remaining source
reference to a retired ID is historical context, not an active instruction.

### Claude prompts, transcripts, and state

1. **Questions come only from the hook capture.** Never reconstruct an open
   `AskUserQuestion` from transcript rows: Claude flushes that tool call only
   after resolution. Transcript pending remains a permission fallback.

2. **A later Notification must not overwrite an existing question capture.**

4. **Native prompt keys are protocol, not UI guesses.** Keep the recipes in
   `engine_act.py` and their tests:

   - A one-question single select uses digit, then a separate raw CR.
   - In a multi-question ask, a single-select digit advances by itself; adding CR
     creates a phantom Enter on the next page.
   - Multi-select digits toggle. Submit through the exact arrow/row sequence
     already encoded in `engine_act.py`.
   - Permission allow is one digit and deny is Esc. A persistent-grant key is
     derived from the captured screen; never assume row 2 exists or means allow.
   - Other text is control-character stripped. Escape sequences and CR are sent
     as separate transport writes.

   Do not change these recipes from memory. Use a disposable tmux/Claude rig and
   add captured-frame tests.

5. **Revalidate at the injection boundary.** Under `scan_lock`, re-poll the exact
   Tail when an action depends on transcript freshness. Prompt answers require a
   matching server-observed nonce and registry `waiting`; interrupt requires an
   active turn. Where tmux screen evidence exists, refuse a mismatched widget.

7. **`stalled` means frozen mid-tool.** Assistant prose with no open tool settles
   to done even without `end_turn`. Parent error/kill notices and a child whose
   newest row is `[Request interrupted by user]` are immediate terminal evidence.
   Later child output supersedes an older terminal notice.

10. **Session file reads are whitelist-only.** `/api/file` accepts an opaque file
    ID and serves only paths projected from that session's own deliveries.
    `file_backups` may resolve a previously delivered file but must never widen the
    whitelist. Keep Claude backup resolution under the exact session directory.
    Return HTML as text; use `nosniff`.

11. **Normalize transcript noise in `Tail._fold`.** Filter metadata and command
    wrappers there, fold human `queued_command` attachments as user messages, and
    expose supported system records as event rows. `AskUserQuestion` results
    become complete QA events.

12. **Every tool call is a conversation row.** `KEY_TOOLS` controls prominence,
    not visibility. Keep only a bounded result preview in memory; fetch full
    bounded results by tool ID from the exact session transcript. Conversation
    freshness is `Tail.convo_rev`, including in-place result updates.

15. **`usage_stats` is cumulative per transcript path.** Flush dirty rows with
    `INSERT OR REPLACE`; additive upserts double-count after restart. If counting
    semantics for a kind change, delete that kind's old rows once.

17. **Compaction evidence is not file order.** Claude writes the completed block
    after compaction and can append earlier timestamps. Order conversation events
    by timestamp, order native setting evidence by byte offset, and use the
    PreCompact checkpoint or a tmux `compacting` label for live state. The screen
    classifier must test `compacting` before the still-visible input box.

22. **Claude effort primarily comes from transcript evidence.** Prefer the newest
    assistant-row effort by byte offset. Statusline capture is a legacy fallback.
    A Fleet-accepted override survives restart until strictly newer native
    evidence replaces it.

23. **Closed sessions have no process.** Read a closed Claude session only through
    its ledger row and `_safe_claude_transcript`. Reopen only an exact UUID with a
    validated cwd under HOME through `_terminal_spawn`. Closed views are otherwise
    read-only.

31. **Placement is provider-neutral and server-owned.** `placement.py` and
    `Engine.organize_session` own `ui_group`, reason, access, and primary action.
    Do not use a UI group as provider availability. Keep
    `docs/session-organization.md` synchronized when placement semantics change.

43. **Only folding touches a live Tail under `scan_lock`.** Request paths normally
    read published main/child snapshots. A startup/fallback poll must take the
    lock. Key child snapshots by `(parent_session_id, agent_id)`.

62. **Live Claude history pages by transcript byte offset.** Ring indexes shift
    as the deque evicts and are not valid cursors. `session_context(before=…)`
    reads bounded backwards chunks into a throwaway Tail; `paginate_context` must
    pass through projections that already set `paged`.

69. **Codex child state is verified from the child thread.** Parent activity is
    discovery metadata, not final lifecycle truth. Terminal child state is
    monotonic and child reads receive their own bounded refresh deadline.

81. **Transcript mtime is not activity.** Use `Tail.activity_ep` through
    `Engine.transcript_quiet`. It is monotonic, does not stamp the initial
    backfill, and does stamp later file growth even when a new row lacks a
    timestamp.

### Native actions and terminal transports

3. **Apple Events go through the signed applet.** Never call iTerm AppleScript
   from the launchd process. tmux uses no Apple Events.

9. **Never inject into a real session during development.** The only exception is
   the explicit user-driven live prompt protocol. Use isolated tmux rigs for
   automated or exploratory native-key tests.

14. **Prompt suppression is request-identity keyed.** Suppress optimistically
   before awaiting delivery. A definite pre-delivery failure may restore the
   selector; duplicate or uncertain delivery must keep it suppressed.

18. **A leading slash opens Claude's command popup.** Append a space before CR
   when sending a slash command that contains no space. The browser menu inserts
   commands; it does not bypass the normal send and danger-confirmation path.

19. **Claude subagents have no tty.** Relay by typing a tagged request into the
   parent for the parent to forward. Never describe relay or stop as targeted:
   Esc stops the parent's turn and every sibling. Refuse relay while the parent is
   waiting on a native prompt. Validate agent IDs before building paths.

20. **Spawn commands are server-composed.** Accept only allowlisted provider
   options, regex-bounded worktree names, and existing directories under HOME.
   Quote paths and never accept a client command, socket, pane, tty, or transport.
   All spawn sites use `_terminal_spawn`.

21. **Folder trust remains Claude-owned.** Read inherited
   `hasTrustDialogAccepted` state for warnings, but never write it or answer the
   trust dialog remotely.

24. **Keep the fold lock narrow.** `act()` takes `scan_lock` only for actions that
   need a fresh Tail and calls `Tail.poll()` inside it. The applet mailbox exchange
   is serialized by its own lock. Transport delays occur between keys, never
   after the last key.

25. **Use the transport dispatcher.** Foreground Claude sessions go through
   `_terminal_write`/`_terminal_spawn`; never call a tmux or applet write method
   directly. Background jobs use the fixed-argv official attach/stop transport.
   Private PTY output is readiness evidence only and must not enter APIs, logs, or
   errors.

30. **Codex control follows runtime ownership.** Production uses Codex's managed
   App Server daemon and default control socket; staging uses its isolated private
   socket. Persisted `runtime_owner` plus the current App Server projection grants
   mutation authority. Visibility, `source`, rollout access, or a discovered tty
   never grants ownership.

   The rollout observer is read-only. A discovered exact Codex terminal may add
   bounded text/image-path delivery and focus only; it cannot grant ownership or
   enable approval, interrupt, close, archive, compact, review, or takeover.
   Connection-generation App Server evidence is the only active-turn authority.

38. **Resume-and-send is exact and idempotent.** Only a validated resumable Claude
   session or an owned Codex thread may expose it. Requests contain session ID,
   text, and a client request ID—never a client path or runtime claim.

40. **Claude permission-mode changes use the native state machine.** Allow only
   server-catalogued modes on an idle session with no prompt, compaction, turn
   fence, or unresolved control delivery. `dontAsk` is startup-only. Never accept
   folder trust or the bypass warning for the user.

65. **Model/effort changes are compare-and-set.** Require current expected values,
   the server catalogue, provider ownership, an idle/prompt-free state, and a
   per-session mutation lock. Compose only exact native commands/API calls.
   Persist accepted values only after provider acceptance; retain partial or
   uncertain outcomes without automatic retry.

66. **Unknown native delivery is never retryable.** Only
   `injector_not_launched`, `background_connection_lost`, and
   `terminal_not_available` prove that no native input was written. Timeout or
   lost acknowledgement after a possible write is uncertain. Persist the fence,
   do not restore/retry automatically, and require new provider evidence.

68. **Composer text cannot answer a native question.** The compound
   `dismiss_then_send` action dismisses the exact nonce first, then creates an
   idempotent when-available Outbox row. It never types text during the native
   selector transition.

73. **Transport choice is per-session and server-derived.** `auto` uses tmux for
   a tty mapped to a live pane and the applet otherwise; `tmux` refuses non-tmux
   sessions; `applet` is the iTerm fallback. A tmux spawn may fall back only after
   a proven pre-delivery failure. Patch `paths.TMUX_SOCKETS` in tests.

74. **Screen reads are bounded and read-only.** Raw pane text is token-gated,
   escaped in the client, never logged/cached/snapshotted, and unavailable for
   Codex, background Claude jobs, non-tmux sessions, or production sessions
   observed from staging. `/api/prompt-options` inherits the same boundary.

75. **One native prompt has one server-owned identity.** Hook, transcript, and
   screen nonces are evidence keys, not client authority. Keep a stable
   `request_id` across a hook/transcript source flip, retire it only when raw
   pending state disappears, and fence every accepted or uncertain answer against
   a second device.

76. **Native actions use durable idempotency receipts.** The browser mints
   `client_request_id` before dispatch. Replaying it returns the stored result and
   never types again. Let `act()` decide failed versus uncertain. Receipt storage
   failure must not block the action; a missing receipt is ambiguous.

77. **Screen evidence may refuse, never authorize by absence.** The pure
   classifier distinguishes `question`, `permission`, `trust`, `compacting`,
   `input`, and `unknown`. A recognized mismatch blocks prompt keys. `unknown`,
   unreadable, or no tmux route means no evidence and does not weaken existing
   hook/registry gates.

78. **The scan may retain a label, never terminal text.** Screen observation is a
   batched pass outside `scan_lock`. `_screen_states` contains only derived labels
   and timestamps. Raw captures stay request-scoped because `/api/fleet` may be
   cached offline.

79. **tmux prompt sequences settle between keys.** `_tmux_settle` must observe a
   screen change before declaring it stable. Only a settled matching question or
   permission proceeds immediately; unreadable/unchanged/timeout falls back to
   the fixed delay, and a settled different surface aborts as uncertain.

80. **A screen-derived pending prompt is permission-only.** The server may mint a
   temporary permission nonce when registry `waiting` and a captured pane agree
   before the hook arrives. Answering it requires a fresh readable permission
   screen. Never derive question shape or question keys from screen text.

### Storage, queues, and security boundaries

6. **Route on the path without its query string.** Use
   `self.path.split("?", 1)[0]` or the route tables.

8. **Legacy ntfy is manual-test-only.** Provider scans and startup never dispatch
   it. It is not a Web Push fallback.

39. **New-session identity is exact.** A provisional client card may be replaced
   only by the server-returned session ID, never by cwd. A lost spawn response is
   ambiguous and must not trigger automatic spawn retry.

41. **Worktree removal is preview-ticketed and post-close.** Resolve paths on the
   server, refuse the primary/unregistered/foreign-locked/shared worktree, bind a
   short-lived ticket to the exact status revision, close the provider first, then
   re-probe before fixed-argv `git worktree remove`. Dirty tracked or untracked
   files require explicit force; ignored build output does not. Never delete the
   branch or silently retry partial failure.

44. **The service worker caches only the public shell and the last exact
   `/api/fleet` snapshot.** All token-bearing APIs, actions, context details,
   files, screen text, notifications, search, and settings stay network-only.
   Bump `SHELL_CACHE` when the shell asset contract changes.

46. **Web Push delivery is isolated and secret-redacted.** Secrets live only in a
   same-owner 0600 file. The supervised Node helper uses validated HTTPS push
   origins and global DNS addresses; HTTP/scan paths only enqueue durable work.
   Never project endpoints, subscriptions, keys, raw helper output, or raw errors.

48. **Push policy is global and revisioned.** Claims/retries revalidate policy and
   device eligibility. Capability actions are authenticated by their narrow HMAC
   capability, not the act token; keep the route credential-omitting and return a
   generic rejection for invalid, expired, replayed, or stale capabilities.

49. **Provider failures are scoped.** A thread limit or malformed provider row
   disables that session, not the dashboard. Bind HTTP before the first provider
   scan and retain last-good projections during provider-wide refresh failure.

52. **Do not automatically retry ambiguous sends.** The device-local offline
   queue flushes only after a successful non-cached fleet poll. Once dispatch may
   have begun, remove the item from automatic retry and offer manual restoration.
   The service worker never sends messages.

54. **Phone images are private server-owned attachments.** Accept bounded opaque
   IDs, validate magic bytes, normalize with fixed argv, strip metadata, bind each
   upload to one exact session, enforce quotas/expiry, and resolve paths
   server-side. Clients never supply local paths. Queue copies are private and
   independent.

56. **Production and staging are capability-isolated.** Code, state, tokens,
   cookies, sockets, uploads, applet mailboxes, service workers, and push
   registrations are instance-specific. Staging may observe shared transcripts
   but can mutate only server-recorded staging-owned sessions, all created in
   managed staging worktrees. Strip client `__*` fields before policy checks.

61. **The server chooses send-now versus queue.** Immediate delivery requires
   exact provider writability or current owned Codex steer authority. Busy Claude
   and attached Codex terminals use a durable exact-session queue. Do not infer
   availability from placement. Revalidate again at native dispatch.

64. **Outbox mutations are versioned and idempotent.** Creation uses a stable
   client request ID; edits/retargets compare exact versions; claims are not
   deletable; retries get independent attachment copies; cancellation preserves
   terminal delivery truth.

### Browser contracts worth preserving

34. **Optimistic rows remain until canonical transcript confirmation.** An
   unrelated revision is not confirmation. Definite failures may restore a draft
   or selector without retrying; uncertain outcomes may only be dismissed.

36. **The session workspace owns its route and state.** Chat, Files, Subagents,
   and Details share one composer and pending drawer. Routes use opaque file IDs
   and validated agent IDs, never paths. Polls preserve section, selection,
   drafts, attachments, and scroll state.

37. **Subagent filters do not reorder hierarchy.** Keep needed ancestors visible.
   Relay remains parent-mediated and is disabled for terminal agents or a waiting
   parent.

45. **Collapsed session-card height is a maximum, not a floor.** Measure current
   content and never clip pending actions, errors, delivery feedback, or live
   agents. `.card` uses `overflow: clip`, not `hidden`.

55. **Paint heavy destinations before doing their work.** Settings, Search, New
   session, and notification detail must show their destination/loading state
   before expensive render or fetch work.

60. **The native-question drawer has independent persistent scroll and size.**
   Do not replace it during a resize gesture or its settle frame. Key persisted
   state by session plus prompt identity.

63. **Fullscreen surfaces are stack-aware modals; the desktop docked session pane
   is not.** Only the top modal owns focus/inert state. A docked workspace leaves
   the queue interactive but becomes inert beneath a true modal.

67. **Reading position has explicit authority.** Only a real user scroll gesture
   disengages follow-tail. Preserve an anchor across rerenders and viewport
   changes; optimistic failures are not canonical tail authority.

71. **Browser modules share state through `globalThis`.** Inline handlers and
   cross-module bare names depend on it. A new module must be added to:

   - `dashboard.html` modulepreloads
   - `static/sw.js` `SHELL_ASSETS`, with a cache bump
   - `server.py` `APP_MODULES`
   - the browser fixture server

   Use the `setHtml`/`setText`/`setClass`/`setAttr` helpers for managed elements;
   unchanged DOM writes still destroy focus and scroll state.

## HTTP and filesystem rules

- `Handler.GET_ROUTES` and `POST_ROUTES` are the authentication inventory. Add
  routes there, not as scattered path branches.
- Open GET routes may expose bounded, path-free projections, including projected
  conversation context. Local file bytes, raw terminal/tool output, command
  metadata, search content, settings, notifications, diagnostics, and mutation
  state require the act token unless a narrower capability is the complete
  authorization.
- Treat all session IDs, agent IDs, file IDs, tool IDs, worktree names, upload
  IDs, request IDs, and provider rows as untrusted. Validate shape before SQL,
  path, process, or terminal use.
- Build subprocess calls with fixed argv when possible. The few terminal command
  strings are server-composed from allowlisted and quoted parts only.
- Runtime state belongs under the configured instance state directory, never the
  source checkout. Modules read roots as `fleetdash.paths.X` attributes so tests
  have one patch point.

## Development workflow

Run the smallest relevant tests while iterating, then the complete affected
suite:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
scripts/coverage.sh --show-missing
npm run test:push
npm run test:browser
```

Coverage is expected to remain at least 95% per Python module. Use
`# pragma: no cover` only for entrypoint guards or justified unreachable
defensive branches.

Files named `tests/live_*_smoke.py` touch a running daemon and are opt-in. Read the
docstring first; some create paid provider turns. Never use a live smoke as a
substitute for a deterministic unit/browser test.

For native Claude behavior, use a separate tmux server and disposable session.
Never target the operator's tmux server or a real session. `noop` is the harmless
end-to-end transport probe because it delivers no keys.

Playwright disables the service worker by default. Opt in only for PWA tests;
otherwise it can intercept before `page.route` and invalidate API mocks. Poll-
rebuilt DOM nodes can detach between reads, so use retrying locators/geometry
helpers. Disengage follow-tail with a real wheel/touch gesture, not a synthetic
`scroll` event.

Restart staging after Python changes:

```bash
launchctl kickstart -k gui/$(id -u)/com.benjaminfeder.fleet-dash.staging
curl -fsS http://127.0.0.1:8378/api/fleet
```

Static assets are served per request and open tabs reload through `page_v`.
Promote production only with `scripts/deploy-production.sh`; never point
production at this checkout.

## External integration points

- `~/.claude/settings.json`: hook registrations
- `~/Library/LaunchAgents/com.benjaminfeder.fleet-dash*.plist`: live daemons
- `~/.claude/fleet-dash-*-state`: private instance state
- `~/.claude/fleet-dash-capture`: shared hook/statusline captures
- `$TMUX_TMPDIR/tmux-<uid>` or `/tmp/tmux-<uid>`: operator tmux sockets
- macOS TCC grant: injector applet to iTerm2 only

Setup, applet rebuilds, phone/PWA onboarding, config fields, terminal usage, and
current known gaps belong in `docs/reference.md`.

<!-- >>> git-workflow (generated block; do not edit by hand) -->
## Git workflow

- `main` changes only through a PR, squash-merged.
- Branch names: `<type>/<slug>`, the type being the commit type (`feat`, `fix`, `docs`, `chore`, `refactor`).
<!-- <<< git-workflow -->
