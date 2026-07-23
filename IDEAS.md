# IDEAS — deferred structural notes

Logged during the 2026-07-23 reorganization (state relocation + `fleetdash/`
package + Engine mixin split). Each item was deliberately left out of that
pass; revisit individually.

## Code structure

- **Engine mixins still share one flat namespace.** The ten `engine_*.py`
  mixins interact only through `self`, so any method can still call any other.
  The next decoupling step is real collaborator objects (e.g. an
  `InjectionTransport` owning the applet mailbox + locks, a `LedgerStore`
  owning the sqlite handles) injected into `Engine.__init__`, letting each
  piece be constructed and tested alone.
- **Retire `fleetdash/engine.py`'s compat re-export block.** Tests and
  server.py still import `DEFAULT_CONFIG`, `Tail`, `classify_placement`, etc.
  from `fleetdash.engine`. Update importers to the real modules
  (`fleetdash.config`, `fleetdash.tail`, `fleetdash.placement`) and delete the
  `# noqa` block; also drop the `time/subprocess/signal` re-imports once
  `test_engine_providers.py` patches those on the owning mixin modules
  instead of `engine_module`.
- ~~`codex_adapter.py` and `briefing.py` splits~~ — **resolved 2026-07-23**:
  codex split into `codex_runtime.py` (lifecycle/migration, lowest layer) +
  `codex_protocol.py` (`UnixWebSocketProcess`/`CodexAppServer`) +
  `codex_adapter.py` (adapter/state) in PR #35; briefing split into
  `briefing_store.py` (StoreOps) + `briefing_scheduler.py` (SchedulerOps) +
  the `FleetOperations` facade in `briefing.py` in PR #33. Both pure code
  motion, importers updated, 405 tests green.
- **`static/app.js` is a 6.2k-line single script.** A split needs a decision
  first: ES modules served raw (multiple requests, sw shell-cache list and
  `page_v` mtime logic must cover every file) vs. introducing a bundling step
  (repo currently has no build). Either way the render/act/overlay/draft
  subsystems are separable.
- **`server.py` route table.** The Handler's do_GET/do_POST dispatch is a long
  if/elif chain; a table of `(path, token_required, handler)` would make the
  auth surface auditable at a glance.

## Drift / latent gaps noticed while auditing (verify before fixing)

- ~~Statusline effort side-write block missing~~ — **resolved 2026-07-23** by
  switching the primary effort source to the transcript: Claude Code ≥2.1.217
  stamps `effort` on every assistant row (main + subagent); `Tail.effort` folds
  it and `effort_for` prefers it (invariant 22 rewritten). The side-write file
  remains a legacy fallback; restoring the canonical statusline block is now
  optional (only matters for pre-2.1.217 builds).
- ~~`file_selector_for_path` ignores backups~~ — **resolved 2026-07-23** with
  `Tail.delivered_paths`, a bounded durable delivery whitelist that outlives
  the files/convo ring buffers; backups stay resolution-only (they track every
  checkpointed file and must never widen the whitelist).
- ~~Subagent effort from the child transcript~~ — **resolved 2026-07-23**:
  `scan_agents` and `agent_context` prefer the child tail's own effort rows;
  the frontmatter pin → parent fallback remains for older transcripts.
- ~~Instance naming asymmetry~~ — **resolved 2026-07-23**: state dirs renamed
  to `fleet-dash-prod-state` / `fleet-dash-staging-state` (plists, both
  applets, deploy script, and docs updated; both daemons verified after).
- ~~`capture_base()` fallback~~ — **resolved 2026-07-23**: the no-env default
  is now `~/.claude/fleet-dash-capture` (where hooks actually write); the test
  fixture patches `paths.CAPTURE_BASE` alongside `BASE`.
- **Stale runtime leftovers in `~/.claude/fleet-dash-prod-state`:**
  `codex_threads.json.pre-managed-daemon.bak` (2026-07-22) and the empty
  `.migration.lock` — delete once the managed-daemon migration is confirmed
  good. A full pre-reorg runtime backup lives at
  `~/.claude/fleet-dash-backup-2026-07-23/`; delete when comfortable.

## Docs

- ~~CLAUDE.md/AGENTS.md duplication~~ — **resolved 2026-07-23**: AGENTS.md is
  the single canonical guide (with a code-structure section); CLAUDE.md is
  just `@AGENTS.md`. The invariant-list discoverability follow-up is also
  resolved 2026-07-23: a thematic quick map heads the Invariants section;
  numbers stay stable because code comments cite "invariant N".
- ~~`docs/` subfolders~~ — **resolved 2026-07-23**: roadmaps moved to
  `docs/roadmaps/`, postmortems/regression evidence to `docs/postmortems/`;
  `docs/session-organization.md` (the one live reference) stays at the root.
  Cross-references updated.
