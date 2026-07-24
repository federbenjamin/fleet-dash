#!/usr/bin/env python3
"""fleet-dash engine: scans live Claude Code sessions + their subagent
transcripts into a fleet snapshot; maintains the spend ledger and notifications.

Data sources (all local, read-only):
  ~/.claude/sessions/<pid>.json          live-session registry (CLI-maintained)
  ~/.claude/projects/<proj>/<sid>.jsonl  main-thread transcript
  ~/.claude/projects/<proj>/<sid>/subagents/agent-*.jsonl (+ .meta.json)

CLI:  engine.py spend [--cwd DIR | --session SID]   one-shot spend table
      engine.py snapshot                            one-shot fleet JSON
"""
import json, os, sys, glob, threading, queue
from collections import deque
from .codex_adapter import CodexAdapter
from .codex_observer import CodexRolloutObserver
from .repo_center import RepositoryOutcomeCenter
from .outbox import OutboxManager
from .briefing import FleetOperations


from . import paths as pathcfg
from .config import (
    CLAUDE_MODELS, CLAUDE_EFFORTS, _validated_claude_delivery_uncertain,
    _validated_claude_control_overrides, _validated_claude_control_uncertain,
    _scrub_private_log, _runtime_log_secrets, load_config, model_family,
    cwd_to_project_dir)
from .tail import Tail


from .engine_staging import StagingOps
from .engine_uploads import UploadOps
from .engine_scan import ScanOps
from .engine_ledger import LedgerOps
from .engine_context import ContextOps
from .engine_worktree import WorktreeOps
from .engine_tmux import TmuxOps
from .engine_receipts import ReceiptOps
from .engine_transport import TransportOps
from .engine_notify import NotifyOps
from .engine_act import ActOps
from .engine_spawn import SpawnOps


class Engine(StagingOps,
             UploadOps,
             ScanOps,
             LedgerOps,
             ContextOps,
             WorktreeOps,
             TmuxOps,
             ReceiptOps,
             TransportOps,
             NotifyOps,
             ActOps,
             SpawnOps):
    def __init__(self, cfg):
        self.cfg = cfg
        self.config_lock = threading.RLock()
        _scrub_private_log(os.path.join(pathcfg.BASE, "fleet-dash.log"), _runtime_log_secrets(cfg))
        for runtime_name in ("config.json", "fleet-dash.log"):
            runtime_path = os.path.join(pathcfg.BASE, runtime_name)
            try:
                if not os.path.islink(runtime_path):
                    os.chmod(runtime_path, 0o600, follow_symlinks=False)
            except FileNotFoundError:
                pass
        self.tails = {}                 # path -> Tail
        self.velocity = {}              # path -> deque[(t, total_tokens)]
        self._agent_eff = {}            # agent-def path -> (mtime, declared effort)
        self._tty_cache = {}            # pid -> tty (never changes; skips a ~25ms `ps`)
        self._codex_terminal_routes_cache = (0.0, {})
        # tmux transport: the executable is resolved once ("" once proven
        # absent) and the pane map is refreshed only on the act/write path, so
        # the two-second fleet scan never pays for terminal discovery.
        self._tmux_executable = None
        self._tmux_panes_cache = (0.0, {})
        self._tmux_lock = threading.Lock()
        self._claude_command_cache = {} # pid -> argv text (one bounded lookup per process)
        # A successful native /effort command is authoritative immediately, but
        # Claude reports effort only through the next statusline side-write.
        # Keep the accepted value until that newer provider report arrives.
        self._claude_control_overrides = _validated_claude_control_overrides(
            cfg.get("claude_control_overrides"))
        self._claude_effort_overrides = {
            sid: (entry["effort"]["value"], entry["effort"]["accepted_at"])
            for sid, entry in self._claude_control_overrides.items()
            if "effort" in entry}
        self._claude_control_uncertain = _validated_claude_control_uncertain(
            cfg.get("claude_control_uncertain"))
        self._claude_background = None  # lazy official `claude attach` bridge
        self._claude_background_error = None
        self._cleanup_tickets = {}      # opaque close-preview tickets, never client paths
        self._cleanup_lock = threading.Lock()
        # The stay-open injector applet consumes one fixed request/result mailbox.
        # Serialize the complete exchange or concurrent HTTP/Outbox actions can
        # overwrite each other and type into the wrong terminal.
        self._inject_lock = threading.Lock()
        # The global injector mailbox protects only the final AppleScript
        # exchange. A per-session lock must also cover the preceding registry /
        # tail validation and the accepted in-memory projection, otherwise two
        # requests can both pass the same idle/CAS snapshot before taking turns
        # at the mailbox.
        self._claude_mutation_locks_guard = threading.Lock()
        self._claude_mutation_locks = {}
        self._claude_turn_fences_guard = threading.Lock()
        self._claude_turn_fences = {}
        self._claude_interrupted = {}    # sid -> transcript revision at accepted Esc
        self._claude_delivery_uncertain_guard = threading.Lock()
        self._claude_delivery_uncertain = _validated_claude_delivery_uncertain(
            cfg.get("claude_delivery_uncertain"))
        self._image_upload_lock = threading.RLock()
        self._image_cleanup_due = 0.0
        self._image_cleanup_running = False
        self._image_cleanup_skip = 0
        self.db = None
        self.pending_seen = {}          # pending nonce -> first-observed timestamp
        # Server-owned prompt identity (invariant 75). A prompt can be evidenced
        # by two different nonces; these map both onto one stable request_id and
        # fence a request that has already been answered.
        self._request_ids = {}          # sid -> identity record
        self._answered_requests = {}    # sid -> {request_id, at}
        self._request_identity_guard = threading.Lock()
        self._act_receipt_prune_due = 0.0
        # Receipt ownership is per-call-stack: only the outermost act()
        # binds one (engine_act.act), and nesting is a thread fact.
        self._act_depth = threading.local()
        self.lock = threading.Lock()
        self.db_lock = threading.RLock()
        self.scan_lock = threading.Lock()   # tails are stateful; one folder at a time
        # One _scan at a time. scan_lock used to give this for free by wrapping
        # the whole call; it now covers only the Tail fold, so the serialization
        # the scan itself relies on needs its own lock.
        self.scan_serialize = threading.Lock()
        self._scan_fold_wait_ms = 0.0
        self._scan_lock_held_ms = 0.0
        self.snapshot_cache = {}
        # Read-only HTTP context/file requests consume immutable bounded
        # projections from the last completed scan. They must not wait behind
        # unrelated ledger/history/status work under scan_lock.
        self._claude_context_snapshots = {}
        self._claude_agent_context_snapshots = {}
        self._finalized_agent_revisions = {}
        self._session_ledger_signatures = {}
        self._session_ledger_written_at = {}
        self._session_ledger_live_ids = None
        self.scan_timings_ms = deque(maxlen=240)
        self.scan_wait_timings_ms = deque(maxlen=240)
        self.last_state_journal_ms = 0.0
        self.history_backfilled = False
        self._workstream_cache = {}       # canonical cwd -> (expires_at, identity)
        self._workstreams_snapshot_cache = None
        self._closed_sessions_cache = None
        self._compact_settings_cache = {}
        self._operational_git_cache = {}
        self._operational_git_pending = set()
        self._operational_git_lock = threading.Lock()
        self._operational_git_queue = queue.Queue(maxsize=300)
        self._operational_git_workers_started = False
        self.repo_center = RepositoryOutcomeCenter(cache_seconds=8)
        ledger_path = os.path.join(pathcfg.BASE, "ledger.db")
        self.ledger_status = self._prepare_ledger(ledger_path)
        self.outbox = OutboxManager(
            ledger_path,
            recovery_source_root=os.path.join(pathcfg.BASE, "uploads"),
            asset_root=os.path.join(pathcfg.BASE, "outbox-images"))
        self.operations = FleetOperations(ledger_path)
        self.web_push = None
        self.web_push_lock = threading.RLock()
        self._provider_session_cache = {"codex": []}
        self.codex_scan_error = None
        self.codex_launcher_status = None
        self._state_event_signatures = None
        self._handoff_links_cache = None
        self._handoff_links_version = 0
        # Claude's registry can flash `waiting` between assistant text and the
        # next tool call. Keep the transition time so an uncorroborated flash
        # remains Working instead of manufacturing a "Response needed" card.
        self.registry_status_since = {}  # session_id -> (status, first_seen)
        try:
            from .codex_protocol import CodexAppServer, UnixWebSocketProcess
            from .codex_runtime import (CodexRuntimeMigration,
                                       LEGACY_RUNTIME_OWNER, MANAGED_RUNTIME_OWNER,
                                       codex_command, codex_control_socket,
                                       codex_runtime_migration_needed,
                                       ensure_managed_codex_runtime,
                                       ensure_shared_codex_runtime,
                                       migrate_codex_runtime_metadata)
            executable = codex_command(cfg.get("codex_command") or None)
            state_path = os.path.join(pathcfg.BASE, "codex_threads.json")
            staging_runtime = cfg.get("instance_mode") == "staging"
            managed_socket = codex_control_socket(managed=True)
            legacy_socket = codex_control_socket(managed=False, state_dir=pathcfg.BASE)
            remote_control = bool(cfg.get("codex_remote_control", True))

            def client_for(socket_path, startup):
                return CodexAppServer(
                    [executable, "app-server", "--listen", "unix://" + socket_path],
                    process_factory=lambda *args, **kwargs: UnixWebSocketProcess(
                        socket_path, timeout=8), startup=startup)

            runtime_migration = None
            if staging_runtime:
                control_socket = legacy_socket
                runtime_owner = LEGACY_RUNTIME_OWNER
                codex_client = client_for(
                    control_socket,
                    lambda: ensure_shared_codex_runtime(executable, control_socket))
            elif codex_runtime_migration_needed(state_path, legacy_socket):
                control_socket = legacy_socket
                runtime_owner = LEGACY_RUNTIME_OWNER
                codex_client = client_for(
                    legacy_socket,
                    lambda: ensure_shared_codex_runtime(executable, legacy_socket))

                def target_factory():
                    return client_for(
                        managed_socket,
                        lambda: ensure_managed_codex_runtime(
                            executable, managed_socket,
                            enable_remote_control=remote_control))

                runtime_migration = CodexRuntimeMigration(
                    executable, state_path, legacy_socket, managed_socket,
                    target_factory)
            else:
                control_socket = managed_socket
                runtime_owner = MANAGED_RUNTIME_OWNER
                if bool(cfg.get("codex_enabled", True)):
                    migrate_codex_runtime_metadata(state_path, phase="committed")
                codex_client = client_for(
                    managed_socket,
                    lambda: ensure_managed_codex_runtime(
                        executable, managed_socket,
                        enable_remote_control=remote_control))
            self.codex_observer = CodexRolloutObserver()
            self.codex = CodexAdapter(enabled=bool(cfg.get("codex_enabled", True)),
                                      client=codex_client,
                                      state_path=state_path,
                                      stall_seconds=int(cfg.get("stall_seconds") or 180),
                                      dormant_seconds=int(
                                          cfg.get("dormant_seconds") or 7200),
                                      external_observer=self.codex_observer,
                                      runtime_owner=runtime_owner,
                                      runtime_migration=runtime_migration)
            if not staging_runtime and bool(cfg.get("codex_enabled", True)):
                self.codex_launcher_status = {
                    "installed": False, "shell_configured": False,
                    "state": "waiting_runtime"}
        except Exception as exc:
            self.codex_observer = None
            self.codex = CodexAdapter(enabled=False, client=object())
            self.codex.error = str(exc)

    MODELS = CLAUDE_MODELS
    EFFORTS = CLAUDE_EFFORTS
    CLAUDE_START_PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")


# ---------------------------------------------------------------- one-shots

def find_session_for_cwd(cwd):
    hits = []
    for p in glob.glob(os.path.join(pathcfg.SESSIONS, "*.json")):
        try:
            d = json.load(open(p))
            os.kill(d["pid"], 0)
        except Exception:
            continue
        if d.get("cwd") == cwd:
            hits.append(d)
    return hits


def spend_table(sid, cwd):
    cfg = load_config()
    proj_dir = cwd_to_project_dir(cwd)
    subdir = os.path.join(proj_dir, sid, "subagents")
    rows, total = [], 0.0
    for meta_path in sorted(glob.glob(os.path.join(subdir, "*.meta.json"))):
        agent_id = os.path.basename(meta_path)[:-len(".meta.json")]
        jl = os.path.join(subdir, agent_id + ".jsonl")
        if not os.path.isfile(jl):
            continue
        meta = json.load(open(meta_path))
        t = Tail(jl)
        t.poll()
        c = t.cost(cfg)
        total += c
        rows.append((meta.get("agentType", "?"), model_family(t.model), t.ti, t.tw, t.tr, t.to, c,
                     (meta.get("description") or "")[:40]))
    print(f"session {sid[:8]}  ({cwd})")
    print(f"{'agentType':<24}{'model':<8}{'in':>9}{'cwrite':>10}{'cread':>11}{'out':>8}{'~$':>8}  description")
    for r in rows:
        print(f"{r[0][:23]:<24}{r[1]:<8}{r[2]:>9}{r[3]:>10}{r[4]:>11}{r[5]:>8}{r[6]:>8.3f}  {r[7]}")
    print("-" * 100)
    print(f"{'TOTAL subagent spend':<70}{total:>8.3f}")
    if not rows:
        print("(no subagents in this session yet)")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "snapshot":
        eng = Engine(load_config())
        print(json.dumps(eng.scan(), indent=2))
    elif args[0] == "spend":
        cwd = sid = None
        if "--cwd" in args:
            cwd = args[args.index("--cwd") + 1]
        if "--session" in args:
            sid = args[args.index("--session") + 1]
        if sid and not cwd:
            for p in glob.glob(os.path.join(pathcfg.SESSIONS, "*.json")):
                try:
                    d = json.load(open(p))
                except Exception:
                    continue
                if str(d.get("sessionId", "")).startswith(sid):
                    sid, cwd = d["sessionId"], d.get("cwd")
        if cwd and not sid:
            hits = find_session_for_cwd(cwd)
            if not hits:
                print(f"no live session with cwd {cwd}"); sys.exit(1)
            if len(hits) > 1:
                print(f"{len(hits)} live sessions share this cwd — showing all:\n")
            for h in hits:
                spend_table(h["sessionId"], h["cwd"])
                print()
            return
        if not (sid and cwd):
            print("usage: engine.py spend --cwd DIR | --session SID"); sys.exit(1)
        spend_table(sid, cwd)
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":  # pragma: no cover - module entrypoint, exercised via main()
    main()
