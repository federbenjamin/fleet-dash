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
- **`static/app.js` is a 6.2k-line single script.** A split needs a decision
  first: ES modules served raw (multiple requests, sw shell-cache list and
  `page_v` mtime logic must cover every file) vs. introducing a bundling step
  (repo currently has no build). Either way the render/act/overlay/draft
  subsystems are separable.
- **`server.py` route table.** The Handler's do_GET/do_POST dispatch is a long
  if/elif chain; a table of `(path, token_required, handler)` would make the
  auth surface auditable at a glance.

## Drift / latent gaps noticed while auditing (verify before fixing)

- **The statusline effort side-write block is missing.**
  `~/.claude/statusline-command.canonical.sh` (the golden copy that
  `fix-statusline.sh` restores on every SessionStart) contains no
  "fleet-dash effort side-write" block, so `fleet-dash-capture/effort/<sid>`
  has not been updated since 2026-07-14. Invariant 22 describes the block as
  present. Until it is restored to the canonical file, the UI falls back to
  model-only labels and persisted `/effort` overrides.
- **`file_selector_for_path` ignores `file_backups`.** `file_content`
  resolves missing Claude deliveries through the transcript's
  file-history-snapshot backup mapping (invariant 10), but the selector gate
  never adds backup-only paths to `allowed`. If a delivered file is deleted
  AND its path has fallen out of the files deque/convo chips, the chip 404s
  even though a backup exists. Dead local removed in the 2026-07-23 pass;
  decide whether the selector should consult backups.
- **Instance naming asymmetry.** Production state lives in
  `~/.claude/fleet-dash-state`, staging state in `~/.claude/fleet-dash-staging`,
  production code in `~/.claude/fleet-dash-prod`, dev/staging code in
  `~/.claude/fleet-dash`, shared captures in `~/.claude/fleet-dash-capture`.
  Consistent (`-prod-state`/`-staging-state`?) naming would help, but every
  rename touches plists, the applet, and the hook — batch it if ever done.
- **`capture_base()` fallback.** With captures moved to
  `fleet-dash-capture`, a bare `python3 server.py` run (no
  `FLEET_DASH_CAPTURE_DIR`) still reads captures from BASE and misses hook
  files. Both plists set the env var so the daemons are correct; changing the
  code default requires reworking the tests that patch `paths.BASE` and
  expect captures to follow it.
- **Stale runtime leftovers in `~/.claude/fleet-dash-state`:**
  `codex_threads.json.pre-managed-daemon.bak` (2026-07-22) and the empty
  `.migration.lock` — delete once the managed-daemon migration is confirmed
  good. A full pre-reorg runtime backup lives at
  `~/.claude/fleet-dash-backup-2026-07-23/`; delete when comfortable.

## Docs

- `CLAUDE.md` (92 KB) duplicates much of `AGENTS.md` (40 KB); the invariants
  list has outgrown flat prose. Consider one canonical invariants file with
  numbered anchors and slimmer per-agent front doors. (Instruction-surface
  edit: load `skill-editor` first per user rules.)
- `docs/` mixes roadmaps, postmortems, and one live reference
  (`session-organization.md`); subfolders (`roadmaps/`, `postmortems/`) would
  keep the live reference findable.
