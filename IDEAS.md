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
- **`codex_adapter.py` (3.5k lines) and `briefing.py` (2.7k lines) are the
  next split candidates.** Codex: protocol client (`UnixWebSocketProcess` /
  `CodexAppServer`) vs. adapter/state vs. runtime migration. Briefing:
  store/schema vs. scheduler/cadence vs. projections.
- ~~`static/app.js` single 6.2k-line script~~ — **resolved 2026-07-23**: split
  into 16 raw ES modules under `static/js/` (no bundler; user decision). Shared
  state lives on `globalThis`, functions/consts publish via `Object.assign`,
  `dashboard.html` modulepreloads every module, and sw.js/`page_v`/fixture
  server cover the new files. See the `static/js/` file-map bullet +
  invariant 71 in AGENTS.md for the module contract.
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
  just `@AGENTS.md`. Still open: the 70-invariant list has outgrown flat
  prose — numbered anchors / grouping would help discoverability.
- `docs/` mixes roadmaps, postmortems, and one live reference
  (`session-organization.md`); subfolders (`roadmaps/`, `postmortems/`) would
  keep the live reference findable.
