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
import json, os, re, sys, glob, time, shlex, sqlite3, secrets, signal, subprocess, threading, contextlib, urllib.request, plistlib, hashlib, copy, uuid, selectors, queue, mmap, stat
from collections import deque
from codex_adapter import CodexAdapter
from codex_observer import CodexRolloutObserver
from repo_center import RepositoryOutcomeCenter, observed_test_outcome
from outbox import OutboxError, OutboxManager
from briefing import FleetOperations, OperationsError
from web_push import WebPushService

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".claude", "fleet-dash")
PROJECTS = os.path.join(HOME, ".claude", "projects")
SESSIONS = os.path.join(HOME, ".claude", "sessions")
CLAUDE_ACCOUNT = os.path.join(HOME, ".claude.json")
CLAUDE_USAGE = os.path.join(BASE, "usage.json")
CLAUDE_STATS = os.path.join(HOME, ".claude", "stats-cache.json")
CLAUDE_HISTORY = os.path.join(HOME, ".claude", "history.jsonl")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CLAUDE_USAGE_PREFS = os.path.join(
    HOME, "Library", "Preferences", "HamedElfayome.Claude-Usage.plist")

DEFAULT_CONFIG = {
    "poll_seconds": 2,
    "stall_seconds": 600,
    "dormant_seconds": 7200,
    "turn_done_window_seconds": 900,
    "agent_done_quiet_seconds": 5,
    "agent_idle_done_seconds": 30,      # settled-but-no-end_turn agent: done after this
    "question_file_pair_seconds": 300,
    "muted_sessions": {},               # session_id -> mute ts; persists until manual unmute
    "pinned_sessions": [],               # shared watchlist, stable insertion order
    "working_order": [],                 # stable entry order while sessions remain Working
    "reply_available": {},               # session_id -> dismissed conversation revision
    "read_sessions": {},                 # session_id -> opened conversation revision
    "dismissed_actions": {},             # action_id -> dismissal ts (inbox only)
    "velocity_window_points": 30,
    "port": 8377,
    "bind": "127.0.0.1",
    "codex_enabled": True,
    "codex_command": "",
    "search_enabled": True,
    "search_discover_seconds": 2,
    "search_batch_rows": 250,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "legacy_ntfy_enabled": False,
    "web_push_allowed_origins": [],      # explicit exact HTTPS push-service origins
    "web_push_node_command": "",        # optional absolute Node >=18 executable
    "web_push_subject": "",             # optional HTTPS URL or mailto VAPID contact
    # last-message peeks: on/off + how many lines each is allowed
    "preview_sessions": True,
    "preview_session_lines": 2,
    "preview_agents": False,
    "preview_agent_lines": 1,
    "reader_width": "fit",              # full-screen chat/docs: fit | centered
    "_permission_keys_note": "keystrokes injected for permission-prompt choices; deny defaults to Esc (cancels any prompt variant)",
    "permission_keys": {"allow": "1", "always": "2", "deny": ""},
    "_rates_note": "per-1M USD: [input, cache_write, cache_read, output]. fable = PLACEHOLDER (opus rates) - correct when pricing is published.",
    "rates": {
        "haiku":  [0.80, 1.00, 0.08, 4.00],
        "sonnet": [3.00, 3.75, 0.30, 15.00],
        "opus":   [5.00, 6.25, 0.50, 25.00],
        "fable":  [5.00, 6.25, 0.50, 25.00],
    },
    "_context_note": "context window per model family; user runs 1M-context models",
    "context_windows": {"default": 1000000, "haiku": 200000, "sonnet": 1000000},
}


def _write_private_json(path, payload):
    """Atomically replace a secret-bearing JSON file with owner-only permissions."""
    temp_path = f"{path}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temp_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _scrub_private_log(path, values):
    """Redact known runtime secrets in place without replacing launchd's open inode."""
    secrets_to_remove = []
    for value in values:
        if isinstance(value, str) and len(value) >= 8:
            encoded = value.encode("utf-8")
            if encoded not in secrets_to_remove:
                secrets_to_remove.append(encoded)
    if not secrets_to_remove:
        return
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return
            if opened.st_size:
                with mmap.mmap(fd, 0, access=mmap.ACCESS_WRITE) as mapped:
                    changed = False
                    for secret in secrets_to_remove:
                        offset = 0
                        while True:
                            offset = mapped.find(secret, offset)
                            if offset < 0:
                                break
                            mapped[offset:offset + len(secret)] = b"*" * len(secret)
                            offset += len(secret)
                            changed = True
                    if changed:
                        mapped.flush()
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return


def _runtime_log_secrets(cfg):
    values = [cfg.get("act_token"), cfg.get("ntfy_topic"), cfg.get("dashboard_url"),
              cfg.get("web_push_subject")]
    secret_path = os.path.join(BASE, "push-secrets.json")
    try:
        info = os.lstat(secret_path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
            return values
        fd = os.open(secret_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or
                    not stat.S_ISREG(opened.st_mode)):
                return values
            stored = json.loads(os.read(fd, 32769).decode("utf-8"))
        finally:
            os.close(fd)
        if isinstance(stored, dict):
            values.extend((stored.get("vapid_private_key"), stored.get("action_secret")))
    except (OSError, ValueError, TypeError):
        pass
    return values


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(BASE, "config.json")
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        os.makedirs(BASE, exist_ok=True)
        raw = {}
    except Exception as e:
        print(f"config.json unreadable ({e}); using defaults", file=sys.stderr)
        return cfg
    if not raw.get("act_token"):        # device token for the remote act endpoint
        raw["act_token"] = secrets.token_hex(16)
    # The former shipped default was four minutes. Move existing installs that
    # still carry that exact value to the new ten-minute default once; preserve
    # every other user-selected threshold.
    if not raw.get("_stall_default_v2"):
        if raw.get("stall_seconds", 240) == 240:
            raw["stall_seconds"] = 600
        raw["_stall_default_v2"] = True
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    _write_private_json(path, merged)
    return merged


def model_family(model):
    m = (model or "").lower()
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if fam in m:
            return fam
    return "opus"


def usd(cfg, fam, ti, tw, tr, to):
    ri, rw, rr, ro = cfg["rates"].get(fam, cfg["rates"]["opus"])
    return (ti * ri + tw * rw + tr * rr + to * ro) / 1e6


def cwd_to_project_dir(cwd):
    return os.path.join(PROJECTS, cwd.replace("/", "-").replace(".", "-"))


# convo view shows these tools only — read-only chatter (Read/Grep/Glob/task
# bookkeeping) stays hidden (user decision 2026-07-13)
KEY_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "Agent", "Skill", "SendUserFile"}
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
WAITING_CONFIRM_SECONDS = 3.0
IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024
IMAGE_UPLOAD_TTL_SECONDS = 24 * 60 * 60
IMAGE_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
IMAGE_UPLOAD_MIMES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
}

# slash commands that destroy conversation state — the page confirms before sending
DANGER_COMMANDS = {"clear", "compact", "quit", "exit", "logout", "rewind"}


def requests_reply(text):
    """Conservative plain-prose signal that an assistant explicitly wants input.

    Provider-native questions remain authoritative. This covers ordinary completed
    assistant messages, whose protocols do not carry a requires-reply field.
    Code, Markdown quotations, and quoted strings are removed before detection so
    examples such as `value?` do not manufacture attention work.
    """
    prose = str(text or "")
    if not prose.strip():
        return False
    prose = re.sub(r"```[\s\S]*?```", " ", prose)
    prose = re.sub(r"`[^`\n]*`", " ", prose)
    prose = re.sub(r"(?m)^\s*>.*$", " ", prose)
    prose = re.sub(r'"[^"\n]*"|“[^”\n]*”|\'[^\'\n]*\'|‘[^’\n]*’', " ", prose)
    explicit = bool(re.search(
        r"(?i)\b(answer|choose|confirm|pick|reply|respond|select|tell me|let me know)\b"
        r"[^.!?\n]{0,100}(?:before (?:i|we) continue|which|whether|one|option|both|these)",
        prose))
    if explicit:
        return True
    # A question is actionable only when it is the final prose request. This
    # excludes rhetorical/status questions that the assistant immediately
    # answers itself, while retaining ordinary "Should I continue?" endings.
    stripped = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", prose).strip()
    match = re.search(r"([^.!?\n]*(?:\n[^.!?\n]*)*)\?\s*$", stripped)
    if not match:
        return False
    question = re.sub(r"\s+", " ", match.group(1)).strip(" -*0123456789.)\t")
    return bool(re.match(
        r"(?i)^(?:what|which|who|when|where|why|how|do|does|did|is|are|was|were|"
        r"can|could|would|will|should|may|must|have|has|had)\b", question))


PRIMARY_ACTION_LABELS = {"respond": "Respond", "review": "Review", "open": "Open",
                         "continue": "Continue", "view": "View", "reopen": "Reopen"}
ACCESS_LABELS = {"interactive": "Interactive", "view_only": "View only",
                 "reopen": "Reopen"}


def _fact_text(value, limit=220):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + "…"


def _pending_placement(pending):
    kind = (pending or {}).get("kind")
    if kind == "question":
        return "Question waiting", "respond", "placement.pending.question"
    if kind == "elicitation":
        return "Form waiting", "respond", "placement.pending.form"
    if kind == "permission":
        approval = (pending or {}).get("approval_kind") or (pending or {}).get("tool")
        if approval == "command":
            return "Command approval", "review", "placement.pending.command_approval"
        if approval == "file_change":
            return "File approval", "review", "placement.pending.file_approval"
        return "Permission needed", "review", "placement.pending.permission"
    return None


def classify_placement(session, now, reply_available=None, read_sessions=None):
    """Pure provider-neutral session placement with diagnostic evidence."""
    reply_available = reply_available or {}
    read_sessions = read_sessions or {}
    sid = str(session.get("session_id") or "")
    revision = str(session.get("convo_v") or "")
    latest = session.get("_latest_prose") or {}
    latest_assistant = latest if latest.get("role") == "assistant" else None
    raw_state = str(session.get("state") or "idle")
    state = str(session.get("stale_previous_state") or "idle") \
        if raw_state == "stale" else raw_state
    pending = session.get("pending") or {}
    capabilities = session.get("capabilities") or {}
    external = bool(session.get("headless") or session.get("read_only"))
    provider_stale = raw_state == "stale" or bool(session.get("stale"))
    dismissed = str(reply_available.get(sid, ""))
    read_revision = str(read_sessions.get(sid, ""))
    reply_requested = bool(
        latest_assistant and requests_reply(latest_assistant.get("text"))
        and dismissed != revision)

    candidates = []
    pending_rule = _pending_placement(pending)
    if pending_rule:
        reason, primary, rule = pending_rule
        candidates.append((rule, "needs_you", reason, primary, "confirmed"))
    if raw_state == "error":
        candidates.append(("placement.provider.error", "needs_you", "Fix needed",
                           "open", "confirmed"))
    if raw_state == "blocked":
        candidates.append(("placement.provider.limit", "needs_you", "Limit reached",
                           "open", "confirmed"))
    if state == "stalled_or_prompt":
        candidates.append(("placement.state.stalled_or_prompt", "needs_you",
                           "Check session", "open", "inferred"))
    if state == "needs_you":
        candidates.append(("placement.state.needs_you", "needs_you",
                           "Response needed", "respond", "confirmed"))
    if session.get("compacting") is not None:
        candidates.append(("placement.runtime.compacting", "working", "Compacting",
                           "open", "confirmed"))
    if state == "stalled":
        candidates.append(("placement.state.stalled", "working", "Slow",
                           "open", "inferred"))
    if state == "running":
        candidates.append(("placement.state.running", "working",
                           "Working elsewhere" if external else "Working",
                           "view" if external else "open", "confirmed"))
    if reply_requested:
        candidates.append(("placement.prose.reply_requested", "needs_you",
                           "Reply requested", "respond", "inferred"))
    if state == "turn_done":
        candidates.append(("placement.state.turn_done", "available",
                           "Completed elsewhere" if external else "Available",
                           "view" if external else "continue", "confirmed"))
    if external:
        candidates.append(("placement.access.external", "history", "External",
                           "view", "confirmed"))
    if state == "reopenable":
        candidates.append(("placement.state.reopenable", "history", "Reopenable",
                           "reopen" if capabilities.get("reopen") else "view", "confirmed"))
    if state == "dormant":
        candidates.append(("placement.state.dormant", "history", "Inactive",
                           "continue", "inferred"))
    default_confidence = ("unknown" if state not in
                          {"idle", "turn_done", "running", "stalled", "needs_you",
                           "stalled_or_prompt", "dormant", "reopenable"} else "confirmed")
    candidates.append(("placement.default.available", "available", "Available",
                       "continue", default_confidence))

    winning_rule, group, reason, primary, confidence = candidates[0]
    if external or provider_stale:
        access = "view_only"
        if primary in ("respond", "review", "open", "continue"):
            primary = "view"
    elif primary == "reopen":
        access = "reopen"
    else:
        access = "interactive"
    if provider_stale:
        confidence = "stale"

    evidence = [{"kind": "provider_signal", "label": "Provider signal",
                 "value": _fact_text(
                     f"{session.get('provider') or 'claude'} state {raw_state}"
                     + (f"; CLI status {session.get('reg_status')}"
                        if session.get("reg_status") is not None else "")),
                 "confidence": "stale" if provider_stale else "confirmed"}]
    if pending:
        evidence.append({"kind": "pending_request", "label": "Pending work",
                         "value": _fact_text(
                             f"{pending.get('kind') or 'request'}"
                             + (f" · {pending.get('approval_kind') or pending.get('tool')}"
                                if pending.get("approval_kind") or pending.get("tool") else "")),
                         "confidence": "confirmed"})
    if latest:
        evidence.append({"kind": "transcript_event", "label": "Latest transcript event",
                         "value": _fact_text(
                             f"{latest.get('role') or 'unknown'} message at revision {revision or 'unknown'}"
                             + ("; direct reply requested" if reply_requested else "")),
                         "confidence": "inferred" if reply_requested else "confirmed"})
    if session.get("agents_running"):
        evidence.append({"kind": "active_work", "label": "Active work",
                         "value": f"{int(session.get('agents_running') or 0)} subagent(s) running",
                         "confidence": "confirmed"})
    if session.get("compacting") is not None:
        evidence.append({"kind": "active_work", "label": "Active work",
                         "value": f"Compaction active for {round(float(session.get('compacting') or 0))}s",
                         "confidence": "confirmed"})
    quiet = max(0, round(float(session.get("quiet_s") or 0)))
    evidence.append({"kind": "age", "label": "Last activity",
                     "value": f"{quiet}s quiet", "confidence": "confirmed"})
    if external:
        evidence.append({"kind": "access", "label": "Control",
                         "value": _fact_text(session.get("read_only_reason") or
                                             "Owned by another runtime; Fleet can only view it"),
                         "confidence": "confirmed"})
    if provider_stale:
        evidence.append({"kind": "stale", "label": "Freshness",
                         "value": _fact_text(session.get("stale_reason") or session.get("error") or
                                             "Showing the last good provider snapshot"),
                         "confidence": "stale"})

    new_response = bool(
        group == "available" and state == "turn_done" and latest_assistant
        and read_revision != revision)
    return {
        "state": state, "ui_group": group, "reason_label": reason,
        "primary_action": primary, "primary_action_label": PRIMARY_ACTION_LABELS[primary],
        "access": access, "access_label": ACCESS_LABELS[access],
        "external": external, "provider_stale": provider_stale,
        "reply_requested": reply_requested, "new_response": new_response,
        "activity_at": max(0, float(now) - float(session.get("quiet_s") or 0)),
        "winning_rule": winning_rule,
        "suppressed_rules": [candidate[0] for candidate in candidates[1:]],
        "state_confidence": confidence, "state_evidence": evidence,
    }


def classify_closed_placement(session):
    """Pure placement for a ledger session whose provider process is gone."""
    can_reopen = bool(session.get("can_reopen"))
    primary = "reopen" if can_reopen else "view"
    access = "reopen" if can_reopen else "view_only"
    closed_at = float(session.get("closed_at") or 0)
    return {
        "state": "closed", "ui_group": "history", "reason_label": "Closed",
        "primary_action": primary, "primary_action_label": PRIMARY_ACTION_LABELS[primary],
        "access": access, "access_label": ACCESS_LABELS[access],
        "external": False, "provider_stale": False, "reply_requested": False,
        "new_response": False,
        "activity_at": session.get("last_seen") or closed_at,
        "winning_rule": "placement.ledger.closed", "suppressed_rules": [],
        "state_confidence": "confirmed",
        "state_evidence": [
            {"kind": "ledger", "label": "Provider process",
             "value": "No live provider process is registered", "confidence": "confirmed"},
            {"kind": "access", "label": "Conversation access",
             "value": ("Transcript can reopen in a new Claude terminal" if can_reopen else
                       "Conversation is retained for viewing only"), "confidence": "confirmed"},
        ],
    }


def redact_handoff_text(value, limit=30_000):
    """Bound and remove common credential shapes from generated/user-edited handoffs."""
    text = str(value or "").replace("\x00", "")[:limit]
    patterns = (
        (r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----[\s\S]*?"
         r"-----END(?: [A-Z0-9]+)* PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED AWS ACCESS KEY]"),
        (r"(?i)\b(?:sk|sk-ant|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{12,}\b",
         "[REDACTED CREDENTIAL]"),
        (r"(?i)\bauthorization\b\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+",
         "Authorization: [REDACTED]"),
        (r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
         r"password|secret)\b\s*[:=]\s*[^\s,;]+", "[REDACTED CREDENTIAL]"),
        (r"(?i)([?&](?:token|key|secret|auth)=)[^&#\s]+", r"\1[REDACTED]"),
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", "Bearer [REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text.strip()

# built-in commands the TUI offers (name, description). Skills + custom commands
# are enumerated off disk per session; these have no file to read.
BUILTIN_COMMANDS = [
    ("compact", "Summarize the conversation and free context"),
    ("clear", "Wipe the conversation and start fresh"),
    ("context", "Show the context window breakdown"),
    ("cost", "Show token cost for this session"),
    ("usage", "Show session cost, plan usage, and activity stats"),
    ("status", "Version, model, account, API connectivity"),
    ("model", "Change the model for this session"),
    ("agents", "Manage subagent definitions"),
    ("todos", "Show the current todo list"),
    ("memory", "Edit CLAUDE.md memory files"),
    ("resume", "Resume a previous conversation"),
    ("rewind", "Rewind the conversation to an earlier point"),
    ("review", "Review a pull request"),
    ("pr-comments", "Fetch comments from a GitHub PR"),
    ("mcp", "Manage MCP servers"),
    ("hooks", "Manage hook configuration"),
    ("permissions", "Manage tool permissions"),
    ("config", "Open the config panel"),
    ("doctor", "Diagnose the installation"),
    ("export", "Export the conversation"),
    ("help", "List available commands"),
]

def ktok(n):
    n = int(n or 0)
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def iso_epoch(ts):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- transcripts

class Tail:
    """Incremental jsonl reader: keeps byte offset + running aggregates."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.ti = self.tw = self.tr = self.to = 0
        self.model = ""
        # Claude writes mode changes as top-level `permission-mode` records and
        # also stamps the effective mode onto human prompt rows. Keep the newest
        # observed value; the live registry does not expose it.
        self.permission_mode = None
        self.last_usage = None          # usage dict of last assistant row
        self.last_shape = None          # ('assistant', stop_reason, [content types]) or ('user', kind)
        self.first_ts = None
        self.last_ts = None
        self.git_branch = None
        self.ai_title = None
        self.pending = {}               # tool_use_id -> {name, input, uuid} awaiting a result
        self.convo = deque(maxlen=120)  # recent turns + key-tool calls
        self.convo_rev = 0              # bumps on ANY convo change (results mutate in place)
        self.files = deque(maxlen=10)   # SendUserFile deliveries: {path, caption, ts}
        self._tool_refs = {}            # tool_use_id -> convo entry (for result attach)
        # usage stats, CUMULATIVE since file start (drained via INSERT OR REPLACE —
        # a daemon restart re-reads the whole file, so cumulative+replace is
        # idempotent and backfills history; additive upserts would double-count)
        self.stats = {}                 # (day, kind, name) -> [uses, chars, ti, tw, tr, to]
        self.stats_dirty = set()
        self.active_skill = None        # skill turn-cost attribution (most recent wins)
        self.active_command = None       # slash command running this turn (/implement, …)
        self.prev_usage = None          # (epoch, model, cache_read+cache_write) of last API call
        # Bounded operational-status state. CacheWrite is recorded only when the
        # value changes, matching Claude's statusline approximation of one point
        # per turn while avoiding duplicate renders of the same usage payload.
        self.cache_write_history = deque(maxlen=50)
        self.cache_write_previous = None
        self.cache_write_spikes = 0
        self.cache_write_peak = 0
        self.cache_write_last_spike_at = None
        self.cache_write_last_spike_value = None
        self.turn_usage = [0, 0, 0, 0]  # input, cache write, cache read, output
        self.saw_compaction = False     # compaction marker since last API call
        self.skill_since_usage = None   # Skill invoked since last API call (bust suspect)
        self.last_compact_ep = 0        # epoch of the newest compact_boundary seen
        self._qa_refs = {}              # AskUserQuestion tool_use_id -> convo entry
        # tool_use_ids whose result came back is_error — for an Agent tool_use that is
        # the CANCELLATION record ("The user doesn't want to proceed with this tool
        # use"), and the only place a killed subagent is unambiguously marked
        self.errored_tools = set()
        # Newer Claude builds also emit a task-notification when a background
        # agent completes or is killed. Keep only the newest terminal notice per
        # agent. scan_agents compares its timestamp with the child transcript so
        # a later SendMessage/resume is never hidden by a stale notification.
        self.agent_terminals = {}

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:          # truncated/rotated: re-read
            self.__init__(self.path)
        if size == self.offset:
            return False
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        # only consume complete lines; leave a partial trailing line for next poll
        nl = chunk.rfind(b"\n")
        if nl < 0:
            return False
        self.offset += nl + 1
        for line in chunk[:nl + 1].splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            self._fold(o)
        return True

    def _fold(self, o):
        ts = o.get("timestamp")
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        if o.get("isCompactSummary"):
            self.saw_compaction = True
        permission_mode = o.get("permissionMode")
        if o.get("type") == "permission-mode":
            permission_mode = o.get("permissionMode")
        if permission_mode in ("default", "acceptEdits", "plan", "auto",
                               "dontAsk", "bypassPermissions"):
            self.permission_mode = permission_mode
        if o.get("type") == "permission-mode":
            return
        if o.get("type") == "system":
            self._system_event(o, ts)
            return
        if o.get("type") == "queue-operation":
            self._agent_terminal_event(o.get("content"), ts)
            return
        if o.get("type") == "attachment":
            # mid-turn user messages never become user rows — they arrive as
            # queued_command attachments (plus transient queue-operation rows,
            # which we ignore so each message folds exactly once)
            a = o.get("attachment") or {}
            if a.get("commandMode") == "task-notification":
                self._agent_terminal_event(a.get("prompt"), ts)
                return
            txt = ""
            if a.get("type") == "queued_command" \
               and (a.get("origin") or {}).get("kind") == "human":
                raw = a.get("prompt")
                if isinstance(raw, list):   # prompt may be content blocks
                    raw = "\n".join(b.get("text", "") for b in raw
                                    if isinstance(b, dict) and b.get("type") == "text")
                txt = str(raw or "").strip()
            if txt and not txt.startswith(("<command-", "/")):
                for e in reversed(self.convo):    # guard against a dequeued twin
                    if e.get("role") == "user":
                        if e.get("text") == txt:
                            txt = ""
                        break
                if txt:
                    self._convo_add("user", txt, ts)
            return
        if o.get("gitBranch"):
            self.git_branch = o["gitBranch"]
        if o.get("type") == "ai-title":            # the iTerm tab title source
            self.ai_title = o.get("aiTitle") or self.ai_title
        m = o.get("message")
        if not isinstance(m, dict):
            return
        role = m.get("role")
        content = m.get("content")
        ctypes = [b.get("type") for b in content if isinstance(b, dict)] if isinstance(content, list) else ["str"]
        if role == "assistant":
            u = m.get("usage")
            if u:
                self.ti += u.get("input_tokens", 0)
                self.tw += u.get("cache_creation_input_tokens", 0)
                self.tr += u.get("cache_read_input_tokens", 0)
                self.to += u.get("output_tokens", 0)
                self.last_usage = u
                self.model = m.get("model") or self.model
                self.turn_usage[0] += u.get("input_tokens", 0)
                self.turn_usage[1] += u.get("cache_creation_input_tokens", 0)
                self.turn_usage[2] += u.get("cache_read_input_tokens", 0)
                self.turn_usage[3] += u.get("output_tokens", 0)
                self._status_cache_track(u, ts)
                self._cache_track(u, ts, m.get("model") or self.model)
                if self.active_skill:   # attribute this turn's spend to the running skill
                    st = self._stat((self._day(ts), "skill", self.active_skill))
                    st[2] += u.get("input_tokens", 0)
                    st[3] += u.get("cache_creation_input_tokens", 0)
                    st[4] += u.get("cache_read_input_tokens", 0)
                    st[5] += u.get("output_tokens", 0)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        self.pending[b["id"]] = {"name": b.get("name"),
                                                 "input": b.get("input"), "uuid": o.get("uuid")}
                        self._stat((self._day(ts), "tool", b.get("name") or "?"))[0] += 1
                        if b.get("name") == "Skill":
                            sk = (b.get("input") or {}).get("skill") or "?"
                            self._stat((self._day(ts), "skill", sk))[0] += 1
                            self.active_skill = sk
                            self.skill_since_usage = sk
                        if b.get("name") == "SendUserFile":
                            inp = b.get("input") or {}
                            for fp in (inp.get("files") or [])[:6]:
                                if isinstance(fp, str):
                                    self._file_add(fp, inp.get("caption", ""), ts)
                        if b.get("name") == "AskUserQuestion":
                            self._qa_add(b, ts)
                        if b.get("name") in KEY_TOOLS:
                            self._tool_add(b, ts)
                txt = "\n\n".join(b.get("text", "") for b in content
                                  if isinstance(b, dict) and b.get("type") == "text").strip()
                if txt:
                    self._convo_add("assistant", txt, ts)
            if m.get("stop_reason") in ("end_turn", "stop_sequence"):
                self.pending.clear()    # turn over: unanswered tool_uses were canceled
                self.active_skill = self.active_command = None
            self.last_shape = ("assistant", m.get("stop_reason"), ctypes)
        elif role == "user":
            kind = "tool_result" if "tool_result" in ctypes else "prompt"
            if kind == "tool_result" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        if b.get("is_error") and b.get("tool_use_id"):
                            self.errored_tools.add(b["tool_use_id"])
                        p = self.pending.pop(b.get("tool_use_id"), None)
                        if p:           # result size = context the tool injected
                            self._stat((self._day(ts), "tool",
                                        p.get("name") or "?"))[1] += self._chars(b)
                        ref = self._tool_refs.pop(b.get("tool_use_id"), None)
                        if ref is not None:
                            ref["result"] = self._result_summary(b, ref.get("name"))
                            ref["failed"] = bool(b.get("is_error"))
                            ref["completed_at"] = ts
                            self.convo_rev += 1
                        qa = self._qa_refs.pop(b.get("tool_use_id"), None)
                        if qa is not None:
                            self._qa_resolve(qa, b)
            elif kind == "prompt":
                self.pending.clear()    # new user turn
                self.active_skill = self.active_command = None
                self.turn_usage = [0, 0, 0, 0]
                if not o.get("isMeta"):
                    if isinstance(content, str):
                        utxt = content
                    else:
                        utxt = "\n\n".join(b.get("text", "") for b in content
                                           if isinstance(b, dict) and b.get("type") == "text")
                    utxt = re.sub(r"<system-reminder>.*?</system-reminder>", "", utxt, flags=re.S).strip()
                    if utxt.startswith("This session is being continued"):
                        self.saw_compaction = True
                    if "<command-name>" in utxt:
                        self._command_event(utxt, ts)
                    if utxt and not utxt.startswith(("<command-", "<local-command", "Caveat:",
                                                     "This session is being continued from",
                                                     "[SYSTEM NOTIFICATION", "<task-notification")):
                        self._convo_add("user", utxt, ts)
            self.last_shape = ("user", kind, ctypes)

    def _agent_terminal_event(self, raw, ts):
        """Fold Claude's bounded task-notification XML into terminal agent state."""
        if not isinstance(raw, str) or len(raw) > 100_000 \
           or "<task-notification>" not in raw:
            return
        task = re.search(r"<task-id>([A-Za-z0-9_-]{1,64})</task-id>", raw)
        status = re.search(r"<status>(completed|killed|failed)</status>", raw,
                           flags=re.I)
        if not task or not status:
            return
        agent_id = task.group(1)
        if not agent_id.startswith("agent-"):
            agent_id = "agent-" + agent_id
        current = self.agent_terminals.get(agent_id)
        if current and (iso_epoch(current.get("ts")) or 0) > (iso_epoch(ts) or 0):
            return
        self.agent_terminals[agent_id] = {
            "status": status.group(1).lower(), "ts": ts,
        }

    def _tool_add(self, b, ts):
        name, inp = b.get("name"), b.get("input") or {}
        entry = {"role": "tool", "name": name, "ts": ts}
        if name == "SendUserFile":
            entry["files"] = [p for p in (inp.get("files") or [])[:6] if isinstance(p, str)]
            entry["caption"] = inp.get("caption", "")
        else:
            entry["arg"] = self._tool_arg(name, inp)
            if name == "Bash" and inp.get("command"):
                entry["command"] = str(inp.get("command"))[:2000]
        self.convo.append(entry)
        self.convo_rev += 1
        if b.get("id"):
            self._tool_refs[b["id"]] = entry
            if len(self._tool_refs) > 300:
                for k in list(self._tool_refs)[:150]:
                    self._tool_refs.pop(k, None)

    @staticmethod
    def _tool_arg(name, inp):
        if name == "Bash":
            v = inp.get("description") or (inp.get("command") or "").split("\n")[0]
        elif name == "Agent":
            v = inp.get("description") or inp.get("subagent_type") or ""
        elif name == "Skill":
            v = inp.get("skill") or ""
        else:
            v = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
        v = str(v).replace(HOME, "~")
        return v[:90] + ("…" if len(v) > 90 else "")

    @staticmethod
    def _result_summary(b, name=None):
        c = b.get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text")
        txt = str(c or "").strip().split("\n")[0]
        if not b.get("is_error") and name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
            if "updated successfully" in txt:
                txt = "updated ✓"
            elif "created successfully" in txt:
                txt = "created ✓"
        return ("✗ " if b.get("is_error") else "") + txt[:140] if txt else ""

    def _event_add(self, kind, title, detail, ts, level="info"):
        """Append a system-event row. Insert by TIMESTAMP, not file order: a
        compaction flushes its whole block at completion, so the `/compact`
        command row is written AFTER the boundary row it preceded in time."""
        e = {"role": "event", "kind": kind, "title": title,
             "detail": detail or "", "level": level, "ts": ts}
        self.convo_rev += 1
        ep = iso_epoch(ts) or 0
        if len(self.convo) == self.convo.maxlen:
            self.convo.popleft()        # a full deque raises on insert()
        for i in range(len(self.convo) - 1, max(-1, len(self.convo) - 9), -1):
            prev_ep = iso_epoch(self.convo[i].get("ts")) or 0
            if prev_ep <= ep:
                self.convo.insert(i + 1, e)
                return
        self.convo.append(e)

    def _system_event(self, o, ts):
        st = o.get("subtype")
        if st == "compact_boundary":
            self.saw_compaction = True
            self.last_compact_ep = max(self.last_compact_ep, iso_epoch(ts) or 0)
            m = o.get("compactMetadata") or {}
            bits = [f"{m.get('trigger') or '?'} compaction"]
            if m.get("preTokens"):
                bits.append(f"{ktok(m['preTokens'])} → {ktok(m.get('postTokens') or 0)} tokens")
            if m.get("durationMs"):
                bits.append(f"{round(m['durationMs'] / 1000)}s")
            self._event_add("compact", "Conversation compacted", " · ".join(bits), ts)
        elif st == "model_refusal_fallback":
            title = f"Switched to {o.get('fallbackModel') or 'another model'}"
            if o.get("originalModel"):
                title = f"{o['originalModel']} → {o.get('fallbackModel')}"
            self._event_add("model", title, str(o.get("content") or "")[:600], ts,
                            level="warning")
        elif st == "api_error":
            err = o.get("error") or {}
            msg = str(err.get("formatted") or err.get("message") or "API error")[:120]
            detail = f"retry {o.get('retryAttempt')}/{o.get('maxRetries')}"
            last = self.convo[-1] if self.convo else None
            if last and last.get("role") == "event" and last.get("kind") == "api_error" \
               and last.get("title") == msg:     # retry storm: collapse into one row
                last["n"] = last.get("n", 1) + 1
                last["detail"], last["ts"] = detail, ts
                self.convo_rev += 1
                return
            self._event_add("api_error", msg, detail, ts, level="error")
        elif st == "local_command":
            out = re.sub(r"</?local-command-[a-z]+>", "", str(o.get("content") or "")).strip()
            for e in reversed(self.convo):       # attach stdout to the command that ran
                if e.get("role") == "event" and e.get("kind") == "command":
                    if out and not e.get("detail"):
                        e["detail"] = out[:400]
                        self.convo_rev += 1
                    return

    def _command_event(self, utxt, ts):
        name = re.search(r"<command-name>(.*?)</command-name>", utxt, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", utxt, re.S)
        name = (name.group(1) if name else "").strip()
        if not name:
            return
        args = (args.group(1) if args else "").strip()
        name = name if name.startswith("/") else "/" + name
        self.active_command = name      # runs until this turn ends
        self._event_add("command", name, args, ts)

    def _qa_add(self, b, ts):
        qs = [{"header": q.get("header", ""), "q": q.get("question", ""), "a": None}
              for q in ((b.get("input") or {}).get("questions") or [])[:8]]
        e = {"role": "event", "kind": "qa", "title": "You answered", "level": "info",
             "detail": "", "qa": qs, "ts": ts}
        self.convo.append(e)
        self.convo_rev += 1
        if b.get("id"):
            self._qa_refs[b["id"]] = e
            if len(self._qa_refs) > 60:
                for k in list(self._qa_refs)[:30]:
                    self._qa_refs.pop(k, None)

    def _qa_resolve(self, e, b):
        """Fill each question's chosen answer from the tool_result, which reads
        'Your questions have been answered: "<question>"="<answer>", ...'."""
        c = b.get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text")
        txt = str(c or "")
        if "declined" in txt.lower():
            for q in e["qa"]:
                q["a"] = "(declined to answer)"
        else:
            got = dict(re.findall(r'"([^"]+)"="([^"]*)"', txt))
            for q in e["qa"]:
                q["a"] = got.pop(q["q"], None)
            leftovers = list(got.values())      # question text drifted: fill in order
            for q in e["qa"]:
                if q["a"] is None and leftovers:
                    q["a"] = leftovers.pop(0)
        self.convo_rev += 1

    def _convo_add(self, role, text, ts):
        if len(text) > 4000:
            text = text[:4000] + "\n…"
        self.convo_rev += 1
        # merge assistant rows within one work stretch into one logical reply
        # (a key-tool entry in between intentionally breaks the merge)
        if self.convo and role == "assistant" and self.convo[-1]["role"] == "assistant":
            prev = self.convo[-1]
            if len(prev["text"]) < 8000:
                prev["text"] = (prev["text"] + "\n\n" + text)[:8000]
            prev["ts"] = ts or prev["ts"]
            return
        self.convo.append({"role": role, "text": text, "ts": ts})

    def _cache_track(self, u, ts, mdl):
        """Per-day token-class mix + prompt-cache invalidation detection.
        Healthy loop: this call's cache_read ≈ previous call's read+write. A
        drop means the missing prefix was re-paid (write at 1.25x or uncached)
        — classify the cause from what happened since the previous call."""
        ep = iso_epoch(ts) or 0
        rd = u.get("cache_read_input_tokens", 0)
        cw = u.get("cache_creation_input_tokens", 0)
        day = self._day(ts)
        st = self._stat((day, "tokens", "all"))
        st[2] += u.get("input_tokens", 0)
        st[3] += cw
        st[4] += rd
        st[5] += u.get("output_tokens", 0)
        repaid = 0
        if self.prev_usage:
            p_ep, p_mdl, p_prefix = self.prev_usage
            missing = p_prefix - rd
            # only count tokens actually RE-PAID this call (write or uncached
            # input) — a shrunken read alone (title-gen side call, context edit)
            # costs nothing and must not register as a bust
            repaid = min(missing, cw + u.get("input_tokens", 0))
            if p_prefix > 4096 and missing > 2048 and repaid > 2048:
                gap = ep - p_ep if ep and p_ep else 0
                if self.saw_compaction:
                    cause = "compaction"
                elif p_mdl and mdl != p_mdl:
                    cause = "model switch"
                elif gap > 3900:
                    cause = "idle >1h (ttl)"
                elif gap > 330:
                    cause = "idle 5m–1h (ttl?)"
                elif self.skill_since_usage:
                    cause = "skill " + self.skill_since_usage
                elif rd >= p_prefix * 0.5:
                    # most of the prefix still read from cache: the re-paid part is
                    # the tail after the last breakpoint, rewritten call after call
                    cause = "tail rewrite (breakpoint drift)"
                else:
                    cause = "deep bust (unattributed)"
                cs = self._stat((day, "cache", cause))
                cs[0] += 1
                cs[1] += repaid
        # a tiny side-call must not become the baseline the next call is judged by
        if self.prev_usage is None or rd + cw >= self.prev_usage[2] * 0.3 or repaid > 2048:
            self.prev_usage = (ep, mdl, rd + cw)
        self.saw_compaction = False
        self.skill_since_usage = None

    def _status_cache_track(self, usage, ts):
        """Keep the bounded CacheWrite graph/spike ledger used by full chat."""
        try:
            value = max(0, int(usage.get("cache_creation_input_tokens", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            value = 0
        if value == self.cache_write_previous:
            return
        self.cache_write_previous = value
        self.cache_write_history.append(value)
        self.cache_write_peak = max(self.cache_write_peak, value)
        if value > 20_000:
            self.cache_write_spikes += 1
            self.cache_write_last_spike_at = iso_epoch(ts)
            self.cache_write_last_spike_value = value

    def status_metrics(self, cfg):
        """Bounded provider telemetry for a session or subagent status strip."""
        usage = self.last_usage or {}

        def token(name):
            try:
                return max(0, int(usage.get(name, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                return 0

        uncached = token("input_tokens")
        cache_write = token("cache_creation_input_tokens")
        cache_read = token("cache_read_input_tokens")
        denominator = uncached + cache_write + cache_read
        turn_cost = (usd(cfg, model_family(self.model), *self.turn_usage)
                     if any(self.turn_usage) else None)
        return {
            "cache_read_pct": (round(100 * cache_read / denominator)
                               if denominator else None),
            "cache_write": cache_write if self.last_usage is not None else None,
            "cache_write_history": list(self.cache_write_history),
            "cache_write_spikes": self.cache_write_spikes,
            "cache_write_peak": (self.cache_write_peak
                                 if self.cache_write_history else None),
            "cache_write_last_spike_at": self.cache_write_last_spike_at,
            "cache_write_last_spike_value": self.cache_write_last_spike_value,
            "turn_cost": round(turn_cost, 4) if turn_cost is not None else None,
        }

    def _day(self, ts):
        return str(ts)[:10] if ts else time.strftime("%Y-%m-%d")

    def _stat(self, key):
        st = self.stats.get(key)
        if st is None:
            st = self.stats[key] = [0, 0, 0, 0, 0, 0]
        self.stats_dirty.add(key)
        return st

    @staticmethod
    def _chars(b):
        c = b.get("content")
        if isinstance(c, list):
            return sum(len(x.get("text", "")) for x in c if isinstance(x, dict))
        return len(str(c or ""))

    def _file_add(self, path, caption, ts):
        for f in self.files:
            if f["path"] == path:       # re-delivery: refresh, don't duplicate
                f["ts"] = ts
                f["caption"] = caption or f["caption"]
                return
        self.files.append({"path": path, "caption": caption or "", "ts": ts})

    def last_message(self, limit=160):
        """Newest prose for the card peek, preserving Markdown block structure."""
        for e in reversed(self.convo):
            if e.get("role") in ("user", "assistant") and e.get("text"):
                txt = str(e["text"]).strip()
                return {"role": e["role"],
                        "text": (txt[:max(0, limit - 1)] + "…"
                                 if len(txt) > limit else txt)}
        return None

    def latest_prose(self):
        """Newest complete user/assistant prose for server-side classification."""
        for e in reversed(self.convo):
            if e.get("role") in ("user", "assistant") and e.get("text"):
                return {"role": e["role"], "text": str(e["text"]).strip()}
        return None

    @property
    def total_tokens(self):
        return self.ti + self.tw + self.tr + self.to

    def cost(self, cfg):
        return usd(cfg, model_family(self.model), self.ti, self.tw, self.tr, self.to)

    def context_tokens(self):
        u = self.last_usage or {}
        return (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0))

    def turn_state(self):
        """'awaiting_input' | 'running' | 'needs_answer' (AskUserQuestion pending) | 'unknown'"""
        s = self.last_shape
        if not s:
            return "unknown"
        if s[0] == "assistant":
            if s[1] in ("end_turn", "stop_sequence"):
                return "awaiting_input"
            if s[1] == "tool_use" and "tool_use" in s[2]:
                return "running"
            return "running"            # mid-stream (thinking/text, stop_reason null)
        return "running"                # user prompt or tool_result just landed


# ------------------------------------------------------------------- scanner

class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        _scrub_private_log(os.path.join(BASE, "fleet-dash.log"), _runtime_log_secrets(cfg))
        for runtime_name in ("config.json", "fleet-dash.log"):
            runtime_path = os.path.join(BASE, runtime_name)
            try:
                if not os.path.islink(runtime_path):
                    os.chmod(runtime_path, 0o600, follow_symlinks=False)
            except FileNotFoundError:
                pass
        self.tails = {}                 # path -> Tail
        self.velocity = {}              # path -> deque[(t, total_tokens)]
        self._agent_eff = {}            # agent-def path -> (mtime, declared effort)
        self._tty_cache = {}            # pid -> tty (never changes; skips a ~25ms `ps`)
        self._claude_command_cache = {} # pid -> argv text (one bounded lookup per process)
        self._cleanup_tickets = {}      # opaque close-preview tickets, never client paths
        self._cleanup_lock = threading.Lock()
        self._image_upload_lock = threading.RLock()
        self._image_cleanup_due = 0.0
        self._image_cleanup_running = False
        self.db = None
        self.pending_seen = {}          # pending nonce -> first-observed timestamp
        self.lock = threading.Lock()
        self.config_lock = threading.RLock()
        self.db_lock = threading.RLock()
        self.scan_lock = threading.Lock()   # tails are stateful; one folder at a time
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
        ledger_path = os.path.join(BASE, "ledger.db")
        self.ledger_status = self._prepare_ledger(ledger_path)
        self.outbox = OutboxManager(ledger_path)
        self.operations = FleetOperations(ledger_path)
        self.web_push = None
        self.web_push_lock = threading.RLock()
        self._provider_session_cache = {"codex": []}
        self.codex_scan_error = None
        self._state_event_signatures = None
        self._handoff_links_cache = None
        self._handoff_links_version = 0
        # Claude's registry can flash `waiting` between assistant text and the
        # next tool call. Keep the transition time so an uncorroborated flash
        # remains Working instead of manufacturing a "Response needed" card.
        self.registry_status_since = {}  # session_id -> (status, first_seen)
        try:
            from codex_adapter import (CodexAppServer, codex_command,
                                       codex_control_socket, ensure_shared_codex_runtime,
                                       UnixWebSocketProcess)
            executable = codex_command(cfg.get("codex_command") or None)
            control_socket = codex_control_socket()
            codex_client = CodexAppServer(
                [executable, "app-server", "--listen", "unix://" + control_socket],
                process_factory=lambda *args, **kwargs: UnixWebSocketProcess(
                    control_socket, timeout=8),
                startup=lambda: ensure_shared_codex_runtime(executable, control_socket))
            self.codex_observer = CodexRolloutObserver()
            self.codex = CodexAdapter(enabled=bool(cfg.get("codex_enabled", True)),
                                      client=codex_client,
                                      state_path=os.path.join(BASE, "codex_threads.json"),
                                      stall_seconds=int(cfg.get("stall_seconds") or 180),
                                      external_observer=self.codex_observer)
        except Exception as exc:
            self.codex_observer = None
            self.codex = CodexAdapter(enabled=False, client=object())
            self.codex.error = str(exc)

    @staticmethod
    def _prepare_ledger(path):
        """Quarantine only a confirmed-corrupt shared ledger so Fleet can start.

        The corrupt bytes are preserved for manual recovery. Starting empty is
        safer than restoring an old backup that could resend already-delivered
        outbox messages.
        """
        if not os.path.exists(path):
            return {"ok": True, "recovered": False}
        db = None
        try:
            db = sqlite3.connect(path, timeout=2)
            result = db.execute("PRAGMA quick_check").fetchone()
            if result and result[0] == "ok":
                return {"ok": True, "recovered": False}
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return {"ok": True, "recovered": False, "check_deferred": True}
        except sqlite3.DatabaseError:
            pass
        finally:
            if db is not None:
                db.close()
        quarantine = path + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        try:
            for suffix in ("", "-wal", "-shm"):
                source = path + suffix
                if os.path.exists(source):
                    os.replace(source, quarantine + suffix)
        except OSError as exc:
            raise RuntimeError(f"ledger is corrupt and could not be quarantined: {exc}")
        return {"ok": False, "recovered": True,
                "error": "Fleet's local ledger was corrupt. It was preserved and a clean ledger was started; Session History, Outbox, budgets, and local usage totals may be incomplete.",
                "quarantine": os.path.basename(quarantine)}

    # -- live sessions from the CLI registry
    def live_sessions(self):
        out = []
        for p in glob.glob(os.path.join(SESSIONS, "*.json")):
            try:
                with open(p) as handle:
                    d = json.load(handle)
                os.kill(d["pid"], 0)
            except Exception:
                continue
            out.append(d)
        return out

    @staticmethod
    def _image_kind(data):
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
                b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        return None

    @staticmethod
    def _strip_jpeg_metadata(data):
        """Drop JPEG APP/COM segments (EXIF GPS, XMP, camera data, comments)."""
        if not isinstance(data, bytes) or not data.startswith(b"\xff\xd8"):
            return None
        out = bytearray(data[:2])
        pos = 2
        while pos < len(data):
            marker_start = pos
            if data[pos] != 0xff:
                return None
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                return None
            marker = data[pos]
            pos += 1
            if marker == 0xda:  # scan data has byte-stuffing, so preserve the rest verbatim
                out.extend(data[marker_start:])
                return bytes(out)
            if marker in tuple(range(0xd0, 0xda)) + (0x01,):
                out.extend(data[marker_start:pos])
                continue
            if pos + 2 > len(data):
                return None
            length = int.from_bytes(data[pos:pos + 2], "big")
            end = pos + length
            if length < 2 or end > len(data):
                return None
            if not (0xe0 <= marker <= 0xef or marker == 0xfe):
                out.extend(data[marker_start:end])
            pos = end
        return bytes(out) if data.endswith(b"\xff\xd9") else None

    @staticmethod
    def _image_upload_paths(upload_id):
        root = os.path.join(BASE, "uploads")
        return root, os.path.join(root, upload_id + ".jpg"), os.path.join(root, upload_id + ".json")

    def _cleanup_image_uploads(self, now=None):
        with self._image_upload_lock:
            now = float(now or time.time())
            root = os.path.join(BASE, "uploads")
            try:
                names = os.listdir(root)[:2000]
            except FileNotFoundError:
                return
            for name in names:
                if not name.endswith(".json") or not IMAGE_UPLOAD_ID_RE.fullmatch(name[:-5]):
                    continue
                upload_id = name[:-5]
                _, image_path, meta_path = self._image_upload_paths(upload_id)
                expired = False
                try:
                    info = os.lstat(meta_path)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                        expired = True
                    else:
                        with open(meta_path) as handle:
                            meta = json.load(handle)
                        expired = float(meta.get("expires_at") or 0) <= now
                except Exception:
                    expired = True
                if expired:
                    for path in (image_path, meta_path):
                        try:
                            if stat.S_ISREG(os.lstat(path).st_mode):
                                os.unlink(path)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            pass

    def _schedule_image_cleanup(self):
        now = time.time()
        with self._image_upload_lock:
            if self._image_cleanup_running or now < self._image_cleanup_due:
                return
            self._image_cleanup_running = True
            self._image_cleanup_due = now + 3600

        def clean():
            try:
                self._cleanup_image_uploads()
            finally:
                with self._image_upload_lock:
                    self._image_cleanup_running = False
        threading.Thread(target=clean, name="fleet-image-cleanup", daemon=True).start()

    def store_image_upload(self, sid, upload_id, display_name, content_type, data):
        """Validate and normalize one private image for an interactive live session."""
        sid = str(sid or "")
        upload_id = str(upload_id or "")
        content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if not IMAGE_UPLOAD_ID_RE.fullmatch(upload_id):
            return {"ok": False, "error": "invalid image ID"}
        if not isinstance(data, bytes) or not 1 <= len(data) <= IMAGE_UPLOAD_BYTES:
            return {"ok": False, "error": "image must be between 1 byte and 10 MB"}
        detected = self._image_kind(data)
        if content_type not in IMAGE_UPLOAD_MIMES or detected != content_type and not (
                content_type == "image/heif" and detected == "image/heic"):
            return {"ok": False, "error": "image type does not match its contents"}
        with self.lock:
            session = next((copy.deepcopy(item) for item in
                self.snapshot_cache.get("sessions") or [] if item.get("session_id") == sid), None)
        if not session or not (session.get("capabilities") or {}).get("submit"):
            return {"ok": False, "error": "session is not available for image messages"}
        root, image_path, meta_path = self._image_upload_paths(upload_id)
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700, follow_symlinks=False)
        source_path = os.path.join(root, "." + upload_id + IMAGE_UPLOAD_MIMES[content_type])
        output_path = os.path.join(root, "." + upload_id + "-normalized.jpg")
        safe_name = os.path.basename(str(display_name or "image"))[:120]
        with self._image_upload_lock:
            self._cleanup_image_uploads()
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(source_path, flags, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                converted = subprocess.run([
                    "/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                    source_path, "--out", output_path], capture_output=True, text=True, timeout=30)
                if converted.returncode != 0:
                    return {"ok": False, "error": "image could not be normalized"}
                with open(output_path, "rb") as stream:
                    scrubbed = self._strip_jpeg_metadata(stream.read(IMAGE_UPLOAD_BYTES + 1))
                if not scrubbed or len(scrubbed) > IMAGE_UPLOAD_BYTES:
                    return {"ok": False, "error": "image metadata could not be removed"}
                flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(output_path, flags)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(scrubbed)
                    stream.flush()
                    os.fsync(stream.fileno())
                info = os.lstat(output_path)
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= IMAGE_UPLOAD_BYTES:
                    return {"ok": False, "error": "normalized image exceeds 10 MB"}
                os.chmod(output_path, 0o600, follow_symlinks=False)
                os.replace(output_path, image_path)
                created = time.time()
                _write_private_json(meta_path, {"version": 1, "upload_id": upload_id,
                    "session_id": sid, "display_name": safe_name, "content_type": "image/jpeg",
                    "size": info.st_size, "created_at": created,
                    "expires_at": created + IMAGE_UPLOAD_TTL_SECONDS})
                return {"ok": True, "upload_id": upload_id, "name": safe_name,
                        "content_type": "image/jpeg", "size": info.st_size,
                        "expires_at": created + IMAGE_UPLOAD_TTL_SECONDS}
            except (OSError, subprocess.SubprocessError):
                return {"ok": False, "error": "image upload failed"}
            finally:
                for path in (source_path, output_path):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass

    def _resolve_image_uploads(self, sid, upload_ids):
        if not isinstance(upload_ids, list) or not 1 <= len(upload_ids) <= 4:
            return None, "attach between 1 and 4 images"
        paths = []
        now = time.time()
        with self._image_upload_lock:
            self._cleanup_image_uploads(now)
            for upload_id in upload_ids:
                upload_id = str(upload_id or "")
                if not IMAGE_UPLOAD_ID_RE.fullmatch(upload_id):
                    return None, "invalid image ID"
                _, image_path, meta_path = self._image_upload_paths(upload_id)
                try:
                    image_info, meta_info = os.lstat(image_path), os.lstat(meta_path)
                    if not stat.S_ISREG(image_info.st_mode) or not stat.S_ISREG(meta_info.st_mode):
                        raise ValueError
                    with open(meta_path) as handle:
                        meta = json.load(handle)
                    if meta.get("session_id") != sid or float(meta.get("expires_at") or 0) <= now:
                        raise ValueError
                    if image_info.st_size != int(meta.get("size") or -1):
                        raise ValueError
                except Exception:
                    return None, "image upload is missing, expired, or belongs to another session"
                paths.append(image_path)
        return paths, None

    def tail_for(self, path):
        t = self.tails.get(path)
        if t is None:
            t = self.tails[path] = Tail(path)
        return t

    def waiting_confirmed(self, sid, status, now, pending=None):
        """Return whether Claude's registry `waiting` means real user input.

        A hook-captured question or permission is immediate evidence. A bare
        registry flag must survive the short between-tool transition seen in
        live Claude sessions before it creates user-facing attention work.
        """
        previous = self.registry_status_since.get(sid)
        if previous is None or previous[0] != status:
            self.registry_status_since[sid] = (status, now)
            age = 0
        else:
            age = max(0, now - previous[1])
        return bool(status == "waiting"
                    and (pending is not None or age >= WAITING_CONFIRM_SECONDS))

    @staticmethod
    def _pending_reason(pending):
        placement = _pending_placement(pending)
        return placement[0:2] if placement else (None, None)

    def organize_session(self, session, now):
        """Add provider-neutral placement, reason, access, and action fields."""
        placement = classify_placement(
            session, now, self.cfg.get("reply_available"), self.cfg.get("read_sessions"))
        session.pop("_latest_prose", None)
        normalized_state = placement.pop("state")
        session.update(placement)
        session["normalized_state"] = normalized_state
        session["pinned"] = str(session.get("session_id") or "") in set(
            self.cfg.get("pinned_sessions") or [])
        return session

    def organize_closed(self, session):
        sid = str(session.get("session_id") or "")
        placement = classify_closed_placement(session)
        normalized_state = placement.pop("state")
        session.update(placement)
        session["state"] = session["normalized_state"] = normalized_state
        session["pinned"] = sid in set(self.cfg.get("pinned_sessions") or [])
        return session

    @staticmethod
    def _bounded_text(value, limit=280):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _action_identity(session, kind, revision):
        raw = "\0".join((str(session.get("provider") or "claude"),
                          str(session.get("session_id") or ""), kind,
                          str(revision or "")))
        return "act-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _sort_action_records(records):
        priority = {"approval": 0, "question": 1, "form": 1, "budget": 2,
                    "problem": 3, "attention": 4, "reply": 5, "outcome": 6}
        records.sort(key=lambda item: (priority.get(item["kind"], 9),
                                       -float(item.get("created_at") or 0),
                                       item["action_id"]))
        return records

    def action_records(self, sessions):
        """Build one stable, provider-neutral inbox record per underlying request."""
        dismissed = self.cfg.get("dismissed_actions") or {}
        records, seen = [], set()
        for session in sessions:
            sid = str(session.get("session_id") or "")
            if not sid:
                continue
            pending = session.get("pending") or {}
            revision = str(session.get("convo_v") or "")
            kind, request, delivery, safe_bulk = None, None, None, ["mute"]
            action_revision = revision
            if pending:
                nonce = str(pending.get("nonce") or revision)
                action_revision = nonce
                if pending.get("kind") == "question":
                    kind, delivery = "question", "Awaiting response"
                    questions = pending.get("questions") or []
                    request = " · ".join(
                        self._bounded_text(q.get("question") or q.get("header"), 140)
                        for q in questions if isinstance(q, dict)) or "Question waiting"
                elif pending.get("kind") == "elicitation":
                    kind, delivery = "form", "Awaiting response"
                    request = pending.get("message") or "Form waiting"
                elif pending.get("kind") == "permission":
                    kind, delivery = "approval", "Awaiting decision"
                    approval = pending.get("approval_kind") or pending.get("tool")
                    label = {"command": "Review command approval",
                             "file_change": "Review file approval"}.get(
                                 approval, "Review permission request")
                    request = label
            elif session.get("reply_requested"):
                kind, request, delivery = "reply", "Reply requested", "Awaiting response"
                safe_bulk.append("mark_available")
            elif session.get("ui_group") == "needs_you":
                kind = "problem" if session.get("state") in \
                    ("blocked", "error", "stalled_or_prompt") \
                    else "attention"
                request = session.get("error") or session.get("reason_label") or "Session needs attention"
                delivery = "Intervention needed"
            elif session.get("new_response"):
                kind, request, delivery = "outcome", "Completed work is ready to review", "Unreviewed"
                safe_bulk.extend(("mark_read", "dismiss"))
            if not kind:
                continue
            action_id = self._action_identity(session, kind, action_revision)
            if action_id in seen or action_id in dismissed:
                continue
            seen.add(action_id)
            last_msg = session.get("last_msg") or {}
            context = pending.get("input_summary") or last_msg.get("text") or session.get("project")
            records.append({
                "action_id": action_id,
                "session_id": sid,
                "provider": session.get("provider") or "claude",
                "kind": kind,
                "request": self._bounded_text(request),
                "context": self._bounded_text(context, 360),
                "created_at": float(session.get("activity_at") or 0),
                "reason": session.get("reason_label") or "Needs attention",
                "access": session.get("access") or "view_only",
                "access_label": session.get("access_label") or "View only",
                "primary_action": session.get("primary_action") or "view",
                "primary_action_label": session.get("primary_action_label") or "View",
                "delivery_state": delivery,
                "safe_bulk": safe_bulk,
                "revision": revision,
                "pending_nonce": pending.get("nonce"),
                "title": session.get("title") or session.get("name") or session.get("project"),
                "project": session.get("project"),
                "muted": bool(session.get("muted")),
            })
        return self._sort_action_records(records)

    def budget_action_records(self, evaluations):
        """Expose current warning/exceeded budgets through the shared action inbox."""
        records = []
        for item in evaluations or []:
            status = item.get("status")
            if status not in ("warning", "exceeded"):
                continue
            alert_key = str(item.get("alert_key") or
                            f"budget:{item.get('id')}:{status}")
            action_id = "act-budget-" + hashlib.sha256(
                alert_key.encode("utf-8")).hexdigest()[:20]
            blocking = bool(item.get("block_spawns") and status == "exceeded")
            scope = str(item.get("scope_type") or "fleet")
            target = str(item.get("scope_id") or "all sessions")
            records.append({
                "action_id": action_id,
                "session_id": None,
                "provider": (target if scope == "provider" else "fleet"),
                "kind": "budget",
                "request": (f"{item.get('label') or 'Budget'} exceeded" if
                            status == "exceeded" else
                            f"{item.get('label') or 'Budget'} is nearing its limit"),
                "context": self._bounded_text(item.get("summary"), 360),
                "created_at": float(item.get("alert_created_at") or 0),
                "reason": "Budget exceeded" if status == "exceeded" else "Budget warning",
                "access": "measurement",
                "access_label": f"{scope.title()} scope",
                "primary_action": "view_budget",
                "primary_action_label": "Review budget",
                "delivery_state": "Future spawns blocked" if blocking else "Alert only",
                "safe_bulk": [],
                "revision": alert_key,
                "title": item.get("label"),
                "project": target if scope in ("workstream", "session") else None,
                "muted": False,
                "status": status,
                "measurement_scope": item.get("measurement_scope"),
                "budget_id": item.get("id"),
            })
        return self._sort_action_records(records)

    @staticmethod
    def _workstream_git_identity(canonical_cwd):
        """Resolve the nearest repository and its common main root without Git."""
        if not canonical_cwd or not os.path.isdir(canonical_cwd):
            return None
        current = canonical_cwd
        while True:
            marker = os.path.join(current, ".git")
            if os.path.isdir(marker):
                return {"kind": "git", "root": current, "worktree": current,
                        "missing": False}
            if os.path.isfile(marker):
                try:
                    with open(marker, errors="replace") as handle:
                        line = handle.readline(4096).strip()
                    if not line.lower().startswith("gitdir:"):
                        raise ValueError("invalid .git file")
                    gitdir = os.path.realpath(os.path.join(
                        current, line.split(":", 1)[1].strip()))
                    common_file = os.path.join(gitdir, "commondir")
                    if os.path.isfile(common_file):
                        with open(common_file, errors="replace") as handle:
                            common = handle.readline(4096).strip()
                        common_git = os.path.realpath(os.path.join(gitdir, common))
                    else:
                        common_git = gitdir
                    main_root = (os.path.dirname(common_git)
                                 if os.path.basename(common_git) == ".git" else current)
                    if not os.path.isdir(main_root):
                        main_root = current
                    return {"kind": "git", "root": main_root, "worktree": current,
                            "missing": False}
                except (OSError, ValueError):
                    return {"kind": "git", "root": current, "worktree": current,
                            "missing": False, "stale": True,
                            "error": "Git worktree metadata is unreadable"}
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    def workstream_identity(self, cwd):
        raw = str(cwd or "").strip()
        expanded = os.path.abspath(os.path.expanduser(raw or os.sep))
        canonical = os.path.realpath(expanded)
        now = time.monotonic()
        cached = self._workstream_cache.get(canonical)
        if cached and cached[0] > now:
            value = cached[1]
            cwd_missing = not os.path.isdir(canonical)
            root_missing = value.get("kind") == "git" and not os.path.isdir(value.get("root") or "")
            if bool(value.get("missing")) == cwd_missing and not root_missing:
                return dict(value)
        identity = self._workstream_git_identity(canonical)
        if identity is None:
            identity = {"kind": "folder", "root": canonical,
                        "worktree": canonical, "missing": not os.path.isdir(canonical)}
        key = identity["kind"] + "\0" + identity["root"]
        identity["workstream_id"] = "ws-" + hashlib.sha256(
            key.encode("utf-8")).hexdigest()[:20]
        self._workstream_cache[canonical] = (now + 30, dict(identity))
        if len(self._workstream_cache) > 2000:
            while len(self._workstream_cache) > 2000:
                self._workstream_cache.pop(next(iter(self._workstream_cache)))
        return identity

    @staticmethod
    def _settings_signature(paths):
        out = []
        for path in paths:
            try:
                stat = os.stat(path)
                out.append((path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                out.append((path, None, None))
        return tuple(out)

    def claude_compact_headroom(self, cwd, context_tokens, context_window, identity=None):
        """Return explicit Claude auto-compact headroom, or None when unknown.

        Project/user settings may contain credentials. Read and cache only the
        two compact env values plus the enable flag; never retain or return the
        raw settings object.
        """
        try:
            context_tokens = max(0, int(context_tokens))
            context_window = max(1, int(context_window))
        except (TypeError, ValueError, OverflowError):
            return None
        if not str(cwd or "").strip():
            return None
        cwd = os.path.realpath(str(cwd))
        identity = identity or self.workstream_identity(cwd)
        root = identity.get("root") if identity.get("kind") == "git" else cwd
        dirs = []
        for path in (root, cwd):
            if path and path not in dirs:
                dirs.append(path)
        paths = [CLAUDE_SETTINGS]
        for directory in dirs:
            paths.extend((os.path.join(directory, ".claude", "settings.json"),
                          os.path.join(directory, ".claude", "settings.local.json")))
        signature = self._settings_signature(paths)
        key = (cwd, signature)
        if key in self._compact_settings_cache:
            threshold = self._compact_settings_cache[key]
        else:
            enabled = True
            raw_window = raw_pct = None
            explicit = False
            for path in paths:
                try:
                    with open(path) as handle:
                        settings = json.load(handle)
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(settings, dict):
                    continue
                if settings.get("autoCompactEnabled") is False:
                    enabled = False
                env = settings.get("env") or {}
                if not isinstance(env, dict):
                    continue
                if "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in env:
                    raw_window = env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
                    explicit = True
                if "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in env:
                    raw_pct = env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
                    explicit = True
            threshold = None
            if enabled and explicit:
                try:
                    window = int(raw_window) if raw_window is not None else context_window
                    pct = float(raw_pct) if raw_pct is not None else 100.0
                    if window > 0 and 0 < pct <= 100:
                        threshold = round(min(window, context_window) * pct / 100)
                except (TypeError, ValueError, OverflowError):
                    threshold = None
            self._compact_settings_cache[key] = threshold
            if len(self._compact_settings_cache) > 300:
                while len(self._compact_settings_cache) > 300:
                    self._compact_settings_cache.pop(next(iter(self._compact_settings_cache)))
        return max(0, threshold - context_tokens) if threshold is not None else None

    def _operational_git_worker(self):
        while True:
            root, worktree = self._operational_git_queue.get()
            try:
                self._operational_git_probe(root, worktree)
            finally:
                self._operational_git_queue.task_done()

    def _operational_git_probe(self, root, worktree):
        data = {"root": root, "worktree": worktree, "ahead": None,
                "behind": None, "observed_at": time.time()}
        try:
            result = self._bounded_process(
                ["git", "-C", worktree, "rev-list", "--left-right", "--count",
                 "refs/remotes/origin/main...HEAD"], timeout=4, max_output=4096)
            if result.get("ok"):
                values = result.get("stdout", "").strip().split()
                if len(values) >= 2 and all(value.isdigit() for value in values[:2]):
                    data["behind"], data["ahead"] = map(int, values[:2])
        except Exception:
            pass
        finally:
            with self._operational_git_lock:
                self._operational_git_cache[worktree] = (
                    time.monotonic() + 8, data)
                self._operational_git_pending.discard(worktree)
                if len(self._operational_git_cache) > 300:
                    while len(self._operational_git_cache) > 300:
                        self._operational_git_cache.pop(
                            next(iter(self._operational_git_cache)))

    def operational_git(self, cwd, identity=None):
        """Return cached Git identity and refresh it off the scan/request path."""
        if not str(cwd or "").strip():
            return {"worktree": None, "worktree_label": None, "ahead": None,
                    "behind": None, "git_observed_at": None}
        identity = identity or self.workstream_identity(cwd)
        worktree = identity.get("worktree") or os.path.realpath(str(cwd or ""))
        out = {"worktree": worktree,
               "worktree_label": os.path.basename(worktree.rstrip(os.sep)) or worktree,
               "ahead": None, "behind": None, "git_observed_at": None}
        if identity.get("kind") != "git" or identity.get("missing"):
            return {"worktree": None, "worktree_label": None, "ahead": None,
                    "behind": None, "git_observed_at": None}
        now = time.monotonic()
        with self._operational_git_lock:
            cached = self._operational_git_cache.get(worktree)
            if cached:
                data = cached[1]
                out.update(ahead=data.get("ahead"), behind=data.get("behind"),
                           git_observed_at=data.get("observed_at"))
            if (not cached or cached[0] <= now) and worktree not in self._operational_git_pending:
                if not self._operational_git_workers_started:
                    self._operational_git_workers_started = True
                    for index in range(2):
                        threading.Thread(target=self._operational_git_worker, daemon=True,
                            name=f"fleet-git-status-{index + 1}").start()
                self._operational_git_pending.add(worktree)
                try:
                    self._operational_git_queue.put_nowait(
                        (identity.get("root"), worktree))
                except queue.Full:
                    self._operational_git_pending.discard(worktree)
        return out

    def session_status_line(self, session, tail=None):
        context_tokens = session.get("ctx_tokens")
        context_window = session.get("ctx_window")
        cwd = session.get("cwd")
        identity = self.workstream_identity(cwd) if str(cwd or "").strip() else None
        compact_remaining = None
        if session.get("provider", "claude") == "claude":
            compact_remaining = self.claude_compact_headroom(
                cwd, context_tokens, context_window, identity=identity)
        metrics = tail.status_metrics(self.cfg) if tail else {}
        session_cost = session.get("cost")
        agent_cost = session.get("agent_cost")
        tree_cost = (round(float(session_cost) + float(agent_cost), 4)
                     if isinstance(session_cost, (int, float)) and
                        isinstance(agent_cost, (int, float)) else None)
        cost_breakdown = []
        if isinstance(session_cost, (int, float)):
            cost_breakdown.append({"kind": "main", "label": "Main session",
                                   "cost": round(float(session_cost), 4)})
            for agent in (session.get("agents") or [])[:100]:
                if not isinstance(agent, dict) or not isinstance(agent.get("cost"),
                                                                  (int, float)):
                    continue
                cost_breakdown.append({
                    "kind": "agent",
                    "label": str(agent.get("description") or
                                 agent.get("agent_type") or "Subagent")[:160],
                    "cost": round(float(agent["cost"]), 4),
                })
        return {
            **self.operational_git(cwd, identity=identity),
            "branch": session.get("branch"),
            "model": session.get("model"), "effort": session.get("effort"),
            "context_tokens": context_tokens, "context_window": context_window,
            "context_pct": session.get("ctx_pct"),
            "compact_remaining": compact_remaining,
            **metrics,
            "session_cost": session_cost, "agent_cost": agent_cost,
            "tree_cost": tree_cost,
            "cost_breakdown": cost_breakdown,
            "cost_breakdown_omitted": max(0, len(session.get("agents") or []) - 100),
            "cost_scope": ("estimated" if tree_cost is not None else "unavailable"),
            "cost_label": "tree",
            "frozen": False,
        }

    def agent_status_line(self, parent, info, tail=None, metrics=None):
        context_tokens = (tail.context_tokens() if tail else info.get("ctx_tokens"))
        if parent.get("provider", "claude") == "claude":
            family = model_family(info.get("model"))
            context_window = self.cfg["context_windows"].get(
                family, self.cfg["context_windows"]["default"])
        else:
            context_window = info.get("ctx_window")
        context_pct = info.get("ctx_pct")
        if context_pct is None and context_tokens is not None and context_window:
            context_pct = round(100 * context_tokens / context_window, 1)
        compact_remaining = None
        cwd = parent.get("cwd")
        identity = self.workstream_identity(cwd) if str(cwd or "").strip() else None
        if parent.get("provider", "claude") == "claude":
            compact_remaining = self.claude_compact_headroom(
                cwd, context_tokens, context_window, identity=identity)
        cost = info.get("cost")
        return {
            **self.operational_git(cwd, identity=identity),
            "branch": parent.get("branch"), "model": info.get("model"),
            "effort": info.get("effort"), "context_tokens": context_tokens,
            "context_window": context_window, "context_pct": context_pct,
            "compact_remaining": compact_remaining,
            **(tail.status_metrics(self.cfg) if tail else (metrics or {})),
            "session_cost": cost, "agent_cost": None, "tree_cost": cost,
            "cost_scope": ("estimated" if isinstance(cost, (int, float))
                           else "unavailable"),
            "cost_label": "agent",
            "cost_breakdown": ([{"kind": "agent",
                                  "label": str(info.get("description") or
                                               info.get("agent_type") or "Subagent")[:160],
                                  "cost": round(float(cost), 4)}]
                               if isinstance(cost, (int, float)) else []),
            "cost_breakdown_omitted": 0,
            "frozen": info.get("state") in ("done", "ended", "closed"),
        }

    def workstream_records(self, sessions, closed):
        """Group live and historical sessions by canonical repository/project."""
        groups = {}
        live_ids = {str(item.get("session_id") or "") for item in sessions}
        for is_closed, session in ([(False, item) for item in sessions] +
                                   [(True, item) for item in closed]):
            sid = str(session.get("session_id") or "")
            if not sid or (is_closed and sid in live_ids):
                continue
            if session.get("cwd"):
                identity = self.workstream_identity(session.get("cwd"))
            else:
                unknown_key = "unknown\0" + str(session.get("provider") or "claude") + "\0" + sid
                identity = {"kind": "unknown", "root": "Location unavailable",
                            "worktree": None, "missing": True,
                            "error": "Provider did not report a working directory",
                            "workstream_id": "ws-" + hashlib.sha256(
                                unknown_key.encode("utf-8")).hexdigest()[:20]}
            group = groups.setdefault(identity["workstream_id"], {
                **identity,
                "title": os.path.basename(identity["root"].rstrip(os.sep)) or identity["root"],
                "sessions": [], "counts": {"needs_you": 0, "working": 0,
                                                "available": 0, "history": 0},
                "branches": set(), "worktrees": set(), "providers": set(),
                "latest_at": 0, "latest_outcome": None,
                "cost": 0.0, "cost_known": 0, "cost_unknown": 0,
                "context_tokens": 0, "context_known": 0,
                "repo_outcomes": [],
            })
            ui_group = session.get("ui_group") or "history"
            if ui_group not in group["counts"]:
                ui_group = "history"
            group["counts"][ui_group] += 1
            summary = {key: session.get(key) for key in
                       ("session_id", "provider", "title", "name", "project", "cwd",
                        "branch", "state", "ui_group", "reason_label", "access",
                        "access_label", "primary_action", "primary_action_label",
                        "activity_at", "closed_at", "can_reopen", "cost", "ctx_tokens")}
            group["sessions"].append(summary)
            if session.get("repo_outcome"):
                group["repo_outcomes"].append(dict(session["repo_outcome"]))
            if session.get("branch"):
                group["branches"].add(str(session["branch"]))
            if identity.get("worktree"):
                group["worktrees"].add(identity["worktree"])
            group["providers"].add(str(session.get("provider") or "claude"))
            activity = float(session.get("activity_at") or session.get("last_seen") or
                             session.get("closed_at") or 0)
            if activity >= group["latest_at"]:
                group["latest_at"] = activity
                last_msg = session.get("last_msg") or {}
                group["latest_outcome"] = self._bounded_text(
                    last_msg.get("text") or session.get("reason_label") or
                    session.get("title") or session.get("project"), 240)
            cost = session.get("cost")
            if isinstance(cost, (int, float)):
                group["cost"] += float(cost)
                group["cost_known"] += 1
            else:
                group["cost_unknown"] += 1
            context = session.get("ctx_tokens")
            if isinstance(context, (int, float)):
                group["context_tokens"] += int(context)
                group["context_known"] += 1
        records = []
        for group in groups.values():
            group["branches"] = sorted(group["branches"])
            group["worktrees"] = sorted(group["worktrees"])
            group["providers"] = sorted(group["providers"])
            group["sessions"].sort(key=lambda item: (
                {"needs_you": 0, "working": 1, "available": 2, "history": 3}.get(
                    item.get("ui_group"), 3), -float(item.get("activity_at") or 0)))
            group["cost"] = round(group["cost"], 4) if group["cost_known"] else None
            group["cost_scope"] = ("unavailable" if not group["cost_known"] else
                                   "partial" if group["cost_unknown"] else "exact")
            if not group["context_known"]:
                group["context_tokens"] = None
            group["repo_outcomes"].sort(key=lambda item: float(item.get("at") or 0),
                                        reverse=True)
            group["test_outcome"] = (group["repo_outcomes"][0]
                                     if group["repo_outcomes"] else None)
            group.pop("repo_outcomes", None)
            group["repo_summary"] = {"changed_files": "not_observed",
                                     "tests": "not_observed", "pull_request": "not_observed"}
            group["budget_state"] = "not_configured"
            records.append(group)
        records.sort(key=lambda item: (
            0 if item["counts"]["needs_you"] else 1,
            0 if item["counts"]["working"] else 1,
            -float(item.get("latest_at") or 0), item["title"].lower()))
        return records

    def scan(self):
        started = time.perf_counter()
        with self.scan_lock:
            acquired = time.perf_counter()
            fleet = self._scan()
        self._schedule_image_cleanup()
        elapsed = (time.perf_counter() - started) * 1000
        wait_ms = (acquired - started) * 1000
        self.scan_timings_ms.append(elapsed)
        self.scan_wait_timings_ms.append(wait_ms)
        ordered = sorted(self.scan_timings_ms)
        waits = sorted(self.scan_wait_timings_ms)
        percentile = lambda q: ordered[min(len(ordered) - 1,
                                            max(0, round((len(ordered) - 1) * q)))]
        wait_percentile = lambda q: waits[min(len(waits) - 1,
                                               max(0, round((len(waits) - 1) * q)))]
        phases = fleet.pop("_scan_phases_ms", {})
        fleet["diagnostics"] = {
            "scan_ms": round(elapsed, 3),
            "scan_p50_ms": round(percentile(.50), 3),
            "scan_p95_ms": round(percentile(.95), 3),
            "scan_wait_ms": round(wait_ms, 3),
            "scan_wait_p95_ms": round(wait_percentile(.95), 3),
            "scan_samples": len(ordered),
            "state_journal_ms": round(self.last_state_journal_ms, 3),
            "phases_ms": phases,
            "operations_db": self.operations.diagnostics(),
            "outbox_db": self.outbox.diagnostics(),
        }
        with self.lock:
            self.snapshot_cache = fleet
        return fleet

    def _scan(self):
        cfg = self.cfg
        now = time.time()
        phase_started = time.perf_counter()
        phases = {}

        def phase(name):
            nonlocal phase_started
            current = time.perf_counter()
            phases[name] = round((current - phase_started) * 1000, 3)
            phase_started = current

        sessions = []
        claude_tails = {}
        live_claude_ids = set()
        for reg in self.live_sessions():
            sid = reg.get("sessionId")
            live_claude_ids.add(sid)
            cwd = reg.get("cwd", "")
            proj_dir = cwd_to_project_dir(cwd)
            main_path = os.path.join(proj_dir, f"{sid}.jsonl")
            if not os.path.isfile(main_path):
                reg_status = reg.get("status")
                state = "running" if reg_status in ("busy", "shell") else "idle"
                sessions.append({
                    "session_id": sid, "native_session_id": sid,
                    "provider": "claude", "pid": reg.get("pid"),
                    "name": reg.get("name"), "title": reg.get("name"),
                    "project": os.path.basename(cwd) or cwd, "cwd": cwd,
                    "branch": None, "model": "", "family": "other",
                    "effort": self.effort_for(sid), "running": None,
                    "permission_mode": None,
                    "permission_modes": ["default", "acceptEdits", "plan"],
                    "last_msg": None, "_latest_prose": None, "repo_outcome": None,
                    "state": state, "reg_status": reg_status, "quiet_s": 0,
                    "ctx_tokens": None, "ctx_window": None, "ctx_pct": None,
                    "total_tokens": None,
                    "cost": None, "cost_source": "unavailable",
                    "bridge_url": (f"https://claude.ai/code/{reg['bridgeSessionId']}"
                                   if reg.get("bridgeSessionId") else None),
                    "started_ms": reg.get("startedAt"), "pending": None,
                    "compacting": None,
                    "muted": sid in (cfg.get("muted_sessions") or {}),
                    "convo_v": f"starting:{reg.get('pid')}:{reg_status}",
                    "files_n": 0, "agents": [], "agents_running": 0,
                    "agents_total": 0, "agent_cost": None,
                    "capabilities": {"submit": True, "interrupt": state == "running",
                        "close": True, "focus_terminal": True,
                        "change_permission_mode": False,
                        "answer_structured": True, "decide_approval": True,
                        "spawn_agent": True, "relay_agent": True,
                        "account_usage": True, "exact_cost": True},
                })
                continue
            mt = self.tail_for(main_path)
            mt.poll()
            claude_tails[sid] = mt
            self._claude_context_snapshots[sid] = {
                "revision": mt.convo_rev,
                "messages": copy.deepcopy(list(mt.convo)),
                "files": copy.deepcopy(list(mt.files)),
            }
            self.drain_stats(mt)
            mtime = os.path.getmtime(main_path)
            quiet = now - mtime

            reg_status = reg.get("status")  # 'busy' | 'shell' | 'idle' | 'waiting' | None
            # Hooks are positive evidence. The bare registry flag is debounced:
            # Claude briefly reports `waiting` between assistant prose and its
            # next tool call even though the turn is still progressing.
            pending = self.hook_pending(sid, reg_status)
            confirmed_waiting = self.waiting_confirmed(
                sid, reg_status, now, pending=pending)
            # parent turn over → a frozen agent is canceled, not mid-tool
            parent_idle = reg_status == "idle" or confirmed_waiting
            agents = self.scan_agents(os.path.join(proj_dir, sid, "subagents"), now,
                                      parent_idle, parent=mt)
            sess_effort = self.effort_for(sid)
            for a in agents:            # the agent chat overlay acts through the parent
                a["session_id"] = sid
                a["effort"] = self.agent_effort(a.get("agent_type"), cwd, sess_effort)
            # long tool calls freeze an agent's transcript ("stalled"); still active
            agents_running = [a for a in agents if a["state"] in ("running", "stalled")]

            turn = mt.turn_state()
            if confirmed_waiting:
                state = "needs_you"         # blocked mid-turn: question or permission prompt
            elif reg_status == "idle" or (reg_status in (None, "shell") and
                                          turn == "awaiting_input"):
                # at the prompt: only actionable if a work turn finished recently
                if turn == "awaiting_input" and quiet < cfg["turn_done_window_seconds"]:
                    state = "turn_done"
                else:
                    state = "idle"
            elif agents_running:
                state = "running"
            elif quiet > cfg["stall_seconds"] and turn != "awaiting_input":
                state = "stalled"
            else:
                state = "running"
            # AskUserQuestion pending presents as an open turn with a frozen file
            if state == "stalled" and mt.last_shape and mt.last_shape[0] == "assistant" \
               and "tool_use" in (mt.last_shape[2] or []):
                state = "stalled_or_prompt"
            # abandoned/backgrounded sessions (VS Code backends, forgotten panes)
            # aren't "waiting on you" in any actionable sense
            if quiet > cfg["dormant_seconds"] and not agents_running:
                state = "dormant"

            # hook-written pending file is the authoritative source: the CLI only
            # flushes AskUserQuestion rows to the transcript AFTER they're answered
            if pending is None:
                for tid, p in mt.pending.items():
                    if p["name"] == "AskUserQuestion":
                        qs = (p.get("input") or {}).get("questions", [])
                        pending = {"kind": "question", "nonce": tid, "questions": qs}
                        break
            if pending is None and confirmed_waiting and mt.pending:
                tid, p = list(mt.pending.items())[-1]
                pending = {"kind": "permission", "nonce": tid, "tool": p["name"],
                           "input_summary": json.dumps(p.get("input"), indent=1)[:1500]}
            if pending and pending.get("kind") == "question":
                # deliver-then-ask pattern: surface files sent shortly before the question
                q_ts = pending.pop("_ts", None) or now
                paired = self._paired_files(mt, q_ts)
                if paired:
                    pending["files"] = paired
            if pending and pending["nonce"] not in self.pending_seen:
                self.pending_seen[pending["nonce"]] = now
                if len(self.pending_seen) > 5000:
                    self.pending_seen = dict(list(self.pending_seen.items())[-2500:])
                print(f"pending first seen: {sid[:8]} {pending['kind']} nonce={pending['nonce'][:24]}",
                      file=sys.stderr, flush=True)

            ctx = mt.context_tokens()
            fam = model_family(mt.model)
            cw = cfg["context_windows"].get(fam, cfg["context_windows"]["default"])
            permission_modes = self._claude_permission_modes(reg, mt)
            sessions.append({
                "session_id": sid,
                "native_session_id": sid,
                "provider": "claude",
                "pid": reg.get("pid"),
                "name": reg.get("name"),
                "title": mt.ai_title,
                "project": os.path.basename(cwd) or cwd,
                "cwd": cwd,
                "branch": mt.git_branch,
                "model": mt.model, "family": fam,
                "effort": self.effort_for(sid),
                "permission_mode": mt.permission_mode,
                "permission_modes": permission_modes,
                # what this turn is running: a Skill beats the slash command that
                # launched it (a /command whose body invokes a skill shows the skill)
                "running": (f"/{mt.active_skill}" if mt.active_skill else mt.active_command)
                           if state in ("running", "stalled", "stalled_or_prompt",
                                        "needs_you") else None,
                # Collapsed height is CSS-controlled. Keep up to 500 characters so
                # the explicit expansion reveals a useful bounded preview.
                "last_msg": (mt.last_message(500)
                             if cfg.get("preview_sessions", True) else None),
                "_latest_prose": mt.latest_prose(),
                "repo_outcome": observed_test_outcome(
                    mt.convo, session_id=sid, provider="claude"),
                "state": state,
                "reg_status": reg_status,
                "quiet_s": round(quiet),
                "ctx_tokens": ctx, "ctx_window": cw,
                "ctx_pct": round(100 * ctx / cw, 1) if cw else None,
                "total_tokens": mt.total_tokens,
                "cost": round(mt.cost(cfg), 4),
                "bridge_url": (f"https://claude.ai/code/{reg['bridgeSessionId']}"
                               if reg.get("bridgeSessionId") else None),
                "started_ms": reg.get("startedAt"),
                "pending": pending,
                "compacting": self.compacting_secs(sid, cwd, mt),
                "muted": sid in (cfg.get("muted_sessions") or {}),
                # cache keys: the page refetches /api/context only when these move
                # (a rev counter, not last-ts: tool results mutate entries in place)
                "convo_v": mt.convo_rev,
                "files_n": len(mt.files),
                "agents": agents,
                "agents_running": len(agents_running),
                "agents_total": len(agents),
                "agent_cost": round(sum(a["cost"] for a in agents), 4),
                "cost_source": "calculated",
                "capabilities": {"submit": True, "interrupt": state == "running",
                    "close": True,
                    "change_permission_mode": (reg_status == "idle" and
                        mt.permission_mode in permission_modes),
                    "focus_terminal": True, "answer_structured": True,
                    "decide_approval": True, "spawn_agent": True,
                    "relay_agent": True, "account_usage": True, "exact_cost": True},
            })
        phase("claude")
        self.registry_status_since = {
            sid: value for sid, value in self.registry_status_since.items()
            if sid in live_claude_ids
        }
        self._claude_context_snapshots = {
            sid: value for sid, value in self._claude_context_snapshots.items()
            if sid in live_claude_ids
        }
        live_agent_keys = {
            (str(session.get("session_id") or ""), str(agent.get("agent_id") or ""))
            for session in sessions if session.get("provider") == "claude"
            for agent in (session.get("agents") or [])
        }
        self._claude_agent_context_snapshots = {
            key: value for key, value in self._claude_agent_context_snapshots.items()
            if key in live_agent_keys
        }
        live_pids = {int(session.get("pid") or 0) for session in sessions
                     if session.get("provider") == "claude"}
        self._claude_command_cache = {
            pid: command for pid, command in self._claude_command_cache.items()
            if pid in live_pids
        }
        # Codex is a second provider inside the same fleet. A failed/missing Codex
        # installation must not take down the existing Claude dashboard.
        try:
            if hasattr(self.codex, "track_external"):
                self.codex.track_external(self.cfg.get("pinned_sessions") or [])
            codex_sessions = [copy.deepcopy(item) for item in self.codex.sessions()]
            self._provider_session_cache["codex"] = copy.deepcopy(codex_sessions)
            self.codex_scan_error = None
        except Exception as exc:
            self.codex_scan_error = str(exc)
            codex_sessions = copy.deepcopy(self._provider_session_cache.get("codex") or [])
            for session in codex_sessions:
                if session.get("state") != "stale":
                    session["stale_previous_state"] = session.get("state") or "idle"
                session.update(state="stale", stale=True, stale_reason=self.codex_scan_error,
                               error=self.codex_scan_error)
                session["capabilities"] = {
                    **(session.get("capabilities") or {}), "submit": False,
                    "interrupt": False, "takeover": False, "close": False}
        sessions.extend(codex_sessions)
        phase("codex")
        muted = self.cfg.get("muted_sessions") or {}
        for session in sessions:
            session["status_line"] = self.session_status_line(
                session, claude_tails.get(session.get("session_id")))
            session["muted"] = session["session_id"] in muted
            self.organize_session(session, now)
        group_order = {"needs_you": 0, "working": 1, "available": 2, "history": 3}
        working_rank = self.stable_working_order(sessions)

        def session_order(session):
            group = session.get("ui_group") or "working"
            if group == "needs_you":
                within = -float(session.get("quiet_s") or 0)
            elif group == "working":
                within = working_rank.get(session.get("session_id"), len(working_rank))
            else:
                within = -float(session.get("activity_at") or 0)
            return (0 if session.get("pinned") else 1,
                    group_order.get(group, 1), within)

        sessions.sort(key=session_order)
        self.record_sessions(sessions, now)
        if not self.history_backfilled:
            try:
                imported = self.backfill_claude_history()
                self.history_backfilled = True
                if imported:
                    print(f"Claude history backfill: indexed {imported} transcripts",
                          file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"Claude history backfill failed: {exc}", file=sys.stderr,
                      flush=True)
        phase("live_ledger")
        closed = [self.organize_closed(item) for item in self.closed_sessions()]
        closed.sort(key=lambda item: -float(item.get("activity_at") or 0))
        links = self.handoff_link_map(
            [item.get("session_id") for item in [*sessions, *closed]])
        for item in [*sessions, *closed]:
            item["handoff_links"] = links.get(str(item.get("session_id") or ""), [])
        phase("closed_history")
        journal_started = time.perf_counter()
        self.record_state_events([*sessions, *closed], now)
        self.last_state_journal_ms = (time.perf_counter() - journal_started) * 1000
        phase("state_journal")
        actions = self.action_records(sessions)
        claude_usage = self.read_usage()
        try:
            codex_usage = self.codex.account_usage()
        except Exception as exc:
            codex_usage = {"provider": "codex", "stale": True, "error": str(exc)}
        phase("usage")
        fleet = {
            "t": now,
            "sessions": sessions,
            "totals": {
                "sessions": len(sessions),
                "busy": sum(1 for s in sessions if s["ui_group"] == "working"),
                "needs_me": sum(1 for s in sessions if s["ui_group"] == "needs_you"),
                "available": sum(1 for s in sessions if s["ui_group"] == "available"),
                "history": (sum(1 for s in sessions if s["ui_group"] == "history")
                            + len(closed)),
                "pinned": sum(1 for s in sessions if s.get("pinned"))
                          + sum(1 for s in closed if s.get("pinned")),
                "dormant": sum(1 for s in sessions if s["state"] == "dormant"),
                "done": sum(1 for s in sessions if s.get("new_response")),
                "agents_running": sum(s["agents_running"] for s in sessions),
                "session_cost": round(sum(s.get("cost") or 0 for s in sessions), 2),
                "agent_cost": round(sum(s.get("agent_cost") or 0 for s in sessions), 2),
                "cost_partial": any(s.get("cost") is None or s.get("agent_cost") is None
                                    for s in sessions),
            },
            "usage": claude_usage,
            "provider_usage": {"claude": claude_usage, "codex": codex_usage},
            "actions": actions,
            "closed": closed,
            "ledger": dict(self.ledger_status),
            "recent_dirs": self.recent_dirs(),
            "models": list(self.MODELS), "efforts": list(self.EFFORTS),
            "models_by_provider": {"claude": [{"id": m, "name": m,
                                                "efforts": list(self.EFFORTS)}
                                               for m in self.MODELS],
                                   "codex": list(self.codex.models)},
            "providers": {"claude": {"ok": True},
                          "codex": {"ok": not bool(self.codex_scan_error or self.codex.error),
                                    "error": self.codex_scan_error or self.codex.error}},
            "settings": {k: self.cfg.get(k, DEFAULT_CONFIG[k]) for k in
                         ("stall_seconds", "preview_sessions", "preview_session_lines",
                          "preview_agents", "preview_agent_lines", "reader_width",
                          "pinned_sessions", "dismissed_actions",
                          "legacy_ntfy_enabled")},
        }
        fleet["settings"]["legacy_ntfy_configured"] = bool(
            self.cfg.get("ntfy_topic") and self.cfg.get("ntfy_server"))
        try:
            fleet["outbox_summary"] = self.outbox.counts()
        except Exception as exc:
            fleet["outbox_summary"] = {"pending": 0, "attention": 0,
                                        "stale": True, "error": str(exc)}
        try:
            budgets = self.operations.observe(fleet, self.workstream_identity)
            fleet["actions"] = self._sort_action_records([
                *(fleet.get("actions") or []), *self.budget_action_records(budgets)])
            fleet["budget_summary"] = {
                "configured": len(budgets),
                "warning": sum(item.get("status") == "warning" for item in budgets),
                "exceeded": sum(item.get("status") == "exceeded" for item in budgets),
                "unavailable": sum(item.get("status") == "unavailable" for item in budgets),
            }
        except Exception as exc:
            fleet["budget_summary"] = {"configured": 0, "stale": True,
                                       "error": str(exc)}
        phase("operations")
        fleet["_scan_phases_ms"] = phases
        return fleet

    def history_snapshot(self, cursor=0, limit=100, query="", provider="", access="", sid=""):
        """Page closed-session metadata outside the two-second fleet payload."""
        try:
            cursor = max(0, int(cursor or 0))
            limit = max(1, min(200, int(limit or 100)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid history pagination"}
        query = str(query or "").strip().lower()
        sid = str(sid or "").strip()
        if len(query) > 300 or len(sid) > 320 or any(ord(char) < 32 for char in sid):
            return {"ok": False, "error": "invalid history filter"}
        if provider not in ("", "claude", "codex"):
            return {"ok": False, "error": "invalid history provider"}
        if access not in ("", "continue", "view", "reopen"):
            return {"ok": False, "error": "invalid history access"}
        with self.lock:
            rows = list(self.snapshot_cache.get("closed") or [])
        if sid:
            item = next((dict(row) for row in rows
                         if str(row.get("session_id") or "") == sid), None)
            return {"ok": True, "item": item}
        filtered = []
        for item in rows:
            if item.get("pinned"):
                continue
            if provider and (item.get("provider") or "claude") != provider:
                continue
            if access and item.get("primary_action") != access:
                continue
            if query:
                haystack = " ".join(str(item.get(key) or "") for key in (
                    "title", "name", "project", "branch", "provider", "reason_label",
                    "access_label", "state", "reg_status", "model", "cwd")).lower()
                if query not in haystack:
                    continue
            filtered.append(dict(item))
        total = len(filtered)
        items = filtered[cursor:cursor + limit]
        next_cursor = cursor + len(items) if cursor + len(items) < total else None
        return {"ok": True, "items": items, "cursor": cursor, "next_cursor": next_cursor,
                "total": total}

    def workstreams_snapshot(self):
        """Build the heavier repository rollup outside the two-second fleet path."""
        with self.lock:
            snapshot = self.snapshot_cache
            sessions = list(snapshot.get("sessions") or [])
            closed = list(snapshot.get("closed") or [])
        digest = hashlib.blake2b(digest_size=16)
        fields = ("session_id", "provider", "title", "name", "project", "cwd",
                  "branch", "state", "ui_group", "reason_label", "access",
                  "access_label", "primary_action", "primary_action_label",
                  "activity_at", "last_seen", "closed_at", "can_reopen", "cost",
                  "ctx_tokens", "last_msg", "repo_outcome")
        for item in sessions + closed:
            digest.update(json.dumps(
                {key: item.get(key) for key in fields}, sort_keys=True,
                separators=(",", ":"), default=str).encode("utf-8"))
            digest.update(b"\0")
        # Repository and budget observations can change without a conversation
        # event, so refresh those at the same cadence as their underlying cache.
        stamp = (digest.hexdigest(), int(time.monotonic() // 8))
        cached = self._workstreams_snapshot_cache
        if cached and cached[0] == stamp:
            return cached[1]
        started = time.perf_counter()
        records = self.workstream_records(sessions, closed)
        try:
            budget_data = self.operations.budgets_snapshot(snapshot)
            by_workstream = {}
            for budget in budget_data.get("budgets") or []:
                if budget.get("scope_type") == "workstream":
                    by_workstream.setdefault(str(budget.get("scope_id") or ""), []).append(budget)
            for group in records:
                scoped = by_workstream.get(group["workstream_id"], [])
                group["budgets"] = scoped
                group["budget_state"] = (
                    "exceeded" if any(item.get("status") == "exceeded" for item in scoped) else
                    "warning" if any(item.get("status") == "warning" for item in scoped) else
                    "unavailable" if any(item.get("status") == "unavailable" for item in scoped) else
                    "ok" if scoped else "not_configured")
        except Exception as exc:
            for group in records:
                group["budget_state"] = "stale"
                group["budget_error"] = str(exc)[:300]
        for group in records:
            if group.get("kind") != "git" or group.get("missing"):
                continue
            worktree = group.get("worktree") or group.get("root")
            repo = self.repo_center.snapshot(
                group["root"], worktree, test_outcome=group.get("test_outcome"),
                include_github=False)
            group["repository"] = {key: repo.get(key) for key in
                                   ("ok", "state", "worktree", "observed_at",
                                    "elapsed_ms", "cached", "error", "repo_slug",
                                    "github_url") if key in repo}
            group["repo_summary"] = self._repository_summary(repo)
        result = {"ok": True, "t": snapshot.get("t") or time.time(),
                  "version": stamp[0], "workstreams": records,
                  "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        self._workstreams_snapshot_cache = (stamp, result)
        return result

    @staticmethod
    def _repository_summary(repo):
        if not repo or not repo.get("ok"):
            return {"changed_files": "stale", "tests": "stale",
                    "pull_request": "stale"}
        changed = "clean" if not repo.get("dirty") else (
            f"{len(repo.get('files') or [])} file" +
            ("" if len(repo.get("files") or []) == 1 else "s"))
        tests = (repo.get("tests") or {}).get("state") or "not_observed"
        pr = repo.get("pr") or {}
        pull_request = RepositoryOutcomeCenter._pr_summary(pr)
        return {"changed_files": changed, "tests": tests,
                "pull_request": pull_request}

    @staticmethod
    def _repository_public(repo):
        """Return bounded repository evidence; subprocess output stays server-side."""
        if not repo:
            return None
        keys = ("ok", "state", "root", "worktree", "branch", "detached", "head_oid",
                "upstream", "ahead", "behind", "dirty", "conflicts", "files", "remotes",
                "remote", "remote_branch", "repo_slug", "github_url", "default_base", "latest_commit", "pr", "tests",
                "observed_at", "elapsed_ms", "revision", "actions", "cached", "error")
        return {key: repo.get(key) for key in keys if key in repo}

    def _repository_group(self, root, worktree=None):
        raw_root = str(root or "").strip()
        raw_worktree = str(worktree or "").strip()
        if len(raw_root) > 4096 or len(raw_worktree) > 4096:
            return None, None, "Repository path is too long"
        if not raw_root and not raw_worktree:
            return None, None, "Repository path is required"
        requested = os.path.realpath(os.path.expanduser(raw_worktree or raw_root))
        if raw_root:
            root = os.path.realpath(os.path.expanduser(raw_root))
        else:
            inferred = self.workstream_identity(requested)
            root = inferred.get("root") if inferred.get("kind") == "git" else ""
        with self.lock:
            sessions = list(self.snapshot_cache.get("sessions") or [])
            closed = list(self.snapshot_cache.get("closed") or [])
        group = next((item for item in self.workstream_records(sessions, closed)
                      if item.get("kind") == "git" and item.get("root") == root), None)
        if not group:
            return None, None, "Repository is not part of the current Fleet"
        identity = self.workstream_identity(requested)
        if identity.get("kind") != "git" or identity.get("root") != root:
            return None, None, "Worktree does not belong to this repository"
        observed = {os.path.realpath(path) for path in (group.get("worktrees") or []) if path}
        observed.add(os.path.realpath(group.get("root") or root))
        if requested not in observed:
            return None, None, "Worktree is not part of an observed Fleet session"
        if not os.path.isdir(requested):
            return None, None, "Worktree is no longer available"
        return group, requested, None

    def repository_snapshot(self, root, worktree=None, force=False):
        group, target, error = self._repository_group(root, worktree)
        if error:
            return {"ok": False, "error": error}
        repo = self.repo_center.snapshot(group["root"], target,
                                         test_outcome=group.get("test_outcome"),
                                         force=force)
        out = self._repository_public(repo)
        out["title"] = group.get("title")
        out["worktrees"] = list(group.get("worktrees") or [])
        out["recent_actions"] = self.repository_action_history(group["root"], target)
        return out

    def _record_repository_action(self, action_id, kind, root, worktree, started,
                                  status, revision, summary=None, error=None):
        self.ensure_db()
        db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=2)
        try:
            db.execute("""INSERT INTO repo_actions(
                action_id,kind,root,worktree,started_at,finished_at,status,summary,error,revision)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                action_id, kind, root, worktree, started, time.time(), status,
                str(summary or "")[:1000] or None, str(error or "")[:1000] or None,
                str(revision or "")[:80] or None))
            db.commit()
        finally:
            db.close()

    def repository_action_history(self, root, worktree, limit=8):
        self.ensure_db()
        db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=2)
        try:
            rows = db.execute("""SELECT action_id,kind,started_at,finished_at,status,
                summary,error FROM repo_actions WHERE root=? AND worktree=?
                ORDER BY id DESC LIMIT ?""", (str(root), str(worktree),
                                                max(1, min(20, int(limit))))).fetchall()
        finally:
            db.close()
        keys = ("action_id", "kind", "started_at", "finished_at", "status",
                "summary", "error")
        return [dict(zip(keys, row)) for row in rows]

    def repository_action(self, action):
        kind = str(action.get("type") or "")
        if kind not in ("git_commit", "git_push", "pr_create_draft", "pr_mark_ready"):
            return {"ok": False, "error": "Unknown repository action"}
        group, target, error = self._repository_group(action.get("root"),
                                                       action.get("worktree"))
        if error:
            return {"ok": False, "error": error}
        preview = self.repo_center.snapshot(group["root"], target,
                                            test_outcome=group.get("test_outcome"))
        action_id = "repo-" + secrets.token_hex(12)
        started = time.time()
        result = self.repo_center.perform(kind, preview, action)
        try:
            self._record_repository_action(
                action_id, kind, group["root"], target, started,
                "succeeded" if result.get("ok") else "failed", action.get("revision"),
                result.get("summary"), result.get("error"))
        except Exception as exc:
            result = {**result, "audit_warning": f"action outcome could not be recorded: {exc}"}
        result["action_id"] = action_id
        if result.get("snapshot"):
            result["snapshot"] = self._repository_public(result["snapshot"])
            result["snapshot"]["title"] = group.get("title")
            result["snapshot"]["worktrees"] = list(group.get("worktrees") or [])
            result["snapshot"]["recent_actions"] = self.repository_action_history(
                group["root"], target)
        self._workstreams_snapshot_cache = None
        return result

    def _persist_config_fields(self, changed):
        """Merge internal/UI state into config.json without dropping secret fields."""
        path = os.path.join(BASE, "config.json")
        with self.config_lock:
            try:
                with open(path) as handle:
                    raw = json.load(handle)
            except Exception:
                raw = {}
            raw.update(changed)
            _write_private_json(path, raw)

    def stable_working_order(self, sessions):
        """Append new Working entries; never reorder incumbents by activity."""
        working_ids = [str(item.get("session_id") or "") for item in sessions
                       if item.get("ui_group") == "working" and item.get("session_id")]
        active = set(working_ids)
        previous = [str(sid) for sid in (self.cfg.get("working_order") or [])]
        ordered, seen = [], set()
        for sid in previous:
            if sid in active and sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        ordered.extend(sid for sid in working_ids if sid not in seen)
        ordered = ordered[-1000:]
        if ordered != previous:
            self.cfg["working_order"] = ordered
            self._persist_config_fields({"working_order": ordered})
        return {sid: index for index, sid in enumerate(ordered)}

    def account_email(self):
        # Fallback identity when Claude Usage is not installed. Follow file changes
        # instead of pinning the first account for the daemon's entire lifetime.
        try:
            stat = os.stat(CLAUDE_ACCOUNT)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        cached = getattr(self, "_account_email", None)
        if cached and cached[0] == signature:
            return cached[1]
        email = None
        try:
            with open(CLAUDE_ACCOUNT) as f:
                email = (json.load(f).get("oauthAccount") or {}).get("emailAddress")
        except (OSError, ValueError):
            email = None
        self._account_email = (signature, email)
        return email

    @staticmethod
    def _claude_usage_iso(value):
        """Convert Apple's 2001 reference-date seconds to an ISO timestamp."""
        try:
            epoch = float(value) + 978_307_200
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
        except (TypeError, ValueError, OverflowError):
            return None

    def claude_usage_profiles(self):
        """Safe display-only projection of Claude Usage's selected profiles.

        The plist also contains live credentials. Parse only identity, selection,
        refresh, and quota fields and never return or cache the raw profile objects.
        """
        try:
            stat = os.stat(CLAUDE_USAGE_PREFS)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = getattr(self, "_claude_usage_profiles", None)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            with open(CLAUDE_USAGE_PREFS, "rb") as handle:
                prefs = plistlib.load(handle)
            profiles_blob = prefs.get("profiles_v3") or b"[]"
            if isinstance(profiles_blob, bytes):
                profiles_blob = profiles_blob.decode("utf-8")
            profiles = json.loads(profiles_blob)
            display_blob = prefs.get("multiProfileDisplayConfig") or b"{}"
            if isinstance(display_blob, bytes):
                display_blob = display_blob.decode("utf-8")
            display = json.loads(display_blob)
            active_id = str(prefs.get("activeProfileId") or "")
            mode = str(prefs.get("profileDisplayMode") or "single")
        except (OSError, ValueError, TypeError, UnicodeDecodeError,
                plistlib.InvalidFileException):
            self._claude_usage_profiles = (signature, None)
            return None

        if not isinstance(profiles, list) or not isinstance(display, dict):
            self._claude_usage_profiles = (signature, None)
            return None
        selected = [profile for profile in profiles if isinstance(profile, dict) and
                    (profile.get("isSelectedForDisplay") if mode == "multi"
                     else str(profile.get("id") or "") == active_id)]
        if not selected:
            selected = [profile for profile in profiles if isinstance(profile, dict) and
                        str(profile.get("id") or "") == active_id]
        out = []
        for profile in selected:
            try:
                account = profile.get("oauthAccountJSON") or "{}"
                if isinstance(account, bytes):
                    account = account.decode("utf-8")
                account = json.loads(account) if isinstance(account, str) else account
            except (ValueError, TypeError, UnicodeDecodeError):
                account = {}
            usage = profile.get("claudeUsage") or {}
            if not isinstance(account, dict) or not isinstance(usage, dict):
                continue

            def pct(key):
                try:
                    return max(0, min(100, round(float(usage.get(key)))))
                except (TypeError, ValueError, OverflowError):
                    return None

            out.append({
                "id": str(profile.get("id") or ""),
                "name": str(profile.get("name") or "")[:120],
                "email": str(account.get("emailAddress") or "")[:320] or None,
                "active": str(profile.get("id") or "") == active_id,
                "five_hour_pct": pct("sessionPercentage"),
                "five_hour_reset": self._claude_usage_iso(usage.get("sessionResetTime")),
                "weekly_pct": pct("weeklyPercentage"),
                "weekly_reset": self._claude_usage_iso(usage.get("weeklyResetTime")),
                "fable_weekly_pct": pct("fableWeeklyPercentage"),
                "fable_weekly_reset": self._claude_usage_iso(
                    usage.get("fableWeeklyResetTime")),
                "updated_at": self._claude_usage_iso(usage.get("lastUpdated")),
            })
        def refresh_interval(profile):
            try:
                value = int(float(profile.get("refreshInterval") or 30))
            except (TypeError, ValueError, OverflowError):
                value = 30
            return min(max(value, 5), 3600)

        result = None if not out else {
            "profiles": out,
            "profile_mode": mode,
            "refresh_seconds": min(refresh_interval(profile)
                                   for profile in selected),
            "show_week": display.get("showWeek") is not False,
            "show_active": display.get("showActiveProfileIndicator") is not False,
        }
        self._claude_usage_profiles = (signature, result)
        return result

    def claude_lifetime_tokens(self):
        """Tokens represented by Claude transcripts retained on this Mac.

        Claude's stats cache aggregates main and saved subagent transcripts by
        model. Count uncached input, cache writes, cache reads, and output so the
        number represents all model tokens processed, not just cache misses.
        """
        try:
            stat = os.stat(CLAUDE_STATS)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = getattr(self, "_claude_stats_cache", None)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            with open(CLAUDE_STATS) as f:
                models = (json.load(f).get("modelUsage") or {}).values()
            total, found = 0, False
            fields = ("inputTokens", "cacheCreationInputTokens",
                      "cacheReadInputTokens", "outputTokens")
            for usage in models:
                if not isinstance(usage, dict):
                    continue
                for field in fields:
                    value = usage.get(field)
                    if isinstance(value, bool) or value is None:
                        continue
                    try:
                        value = int(value)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if value >= 0:
                        total += value
                        found = True
            result = total if found else None
        except (OSError, ValueError, AttributeError):
            result = None
        self._claude_stats_cache = (signature, result)
        return result

    def read_usage(self):
        # Claude Usage is the primary source because it tracks every selected login
        # and refreshes them independently. The statusline side-write remains the
        # single-account fallback when that app is absent or unreadable.
        try:
            with open(CLAUDE_USAGE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}

        def pct(v):
            try:
                return round(float(v))
            except (TypeError, ValueError):
                return None

        def iso(v):
            try:
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(v)))
            except (TypeError, ValueError):
                return None
        five, weekly = pct(d.get("five_hour_pct")), pct(d.get("seven_day_pct"))
        tracked = self.claude_usage_profiles()
        email = self.account_email()
        lifetime_tokens = self.claude_lifetime_tokens()
        if tracked:
            active = next((profile for profile in tracked["profiles"]
                           if profile.get("active")), tracked["profiles"][0])
            return {**tracked,
                    "five_hour_pct": active.get("five_hour_pct"),
                    "five_hour_reset": active.get("five_hour_reset"),
                    "weekly_pct": active.get("weekly_pct"),
                    "weekly_reset": active.get("weekly_reset"),
                    "email": active.get("email"),
                    "lifetime_tokens": lifetime_tokens,
                    "lifetime_scope": "local_transcripts",
                    "source": "claude_usage"}
        if five is None and weekly is None and not email and lifetime_tokens is None:
            return None
        return {
            "five_hour_pct": five,
            "five_hour_reset": iso(d.get("five_hour_reset")),
            "weekly_pct": weekly,
            "weekly_reset": iso(d.get("seven_day_reset")),
            "email": email,
            "lifetime_tokens": lifetime_tokens,
            "lifetime_scope": "local_transcripts",
        }

    def scan_agents(self, subdir, now, parent_idle=False, parent=None):
        out = []
        cfg = self.cfg
        parent_sid = os.path.basename(os.path.dirname(subdir))
        killed = parent.errored_tools if parent else set()
        terminal = parent.agent_terminals if parent else {}
        for meta_path in glob.glob(os.path.join(subdir, "*.meta.json")):
            agent_id = os.path.basename(meta_path)[:-len(".meta.json")]
            jl = os.path.join(subdir, agent_id + ".jsonl")
            if not os.path.isfile(jl):
                continue
            try:
                with open(meta_path) as handle:
                    meta = json.load(handle)
            except Exception:
                meta = {}
            t = self.tail_for(jl)
            grew = t.poll()
            self._claude_agent_context_snapshots[(parent_sid, agent_id)] = {
                "revision": t.convo_rev,
                "messages": copy.deepcopy(list(t.convo)),
                "context_tokens": t.context_tokens(),
                "status_metrics": copy.deepcopy(t.status_metrics(cfg)),
            }
            self.drain_stats(t)
            mtime = os.path.getmtime(jl)
            quiet = now - mtime

            # An agent is WORKING only while something is in flight: a tool_use waiting
            # on its result, or a tool_result it hasn't answered yet. If its last row is
            # assistant prose with no tool call, nothing is running — it finished, even
            # when no end_turn was ever written. (Verified 2026-07-14: a long final
            # report often ends on a stop_reason-less text row, which used to decay into
            # "stalled" forever. "stalled" must mean frozen mid-TOOL, nothing else.)
            role, stop, ctypes = (t.last_shape or (None, None, []))[:3]
            settled = role == "assistant" and "tool_use" not in (ctypes or [])
            grace = (cfg["agent_done_quiet_seconds"]
                     if stop in ("end_turn", "stop_sequence")
                     else cfg["agent_idle_done_seconds"])
            done = settled and quiet > grace
            state = "done" if done else ("stalled" if quiet > cfg["stall_seconds"] else "running")

            # CANCELLED is authoritative and immediate: the parent's tool_result for
            # this agent came back is_error ("the user doesn't want to proceed with
            # this tool use"). A killed agent's own transcript ends on a USER row, so
            # `settled` (assistant-last) can never see it and it would otherwise sit
            # "running" until it rotted into red "stalled" forever. (Verified
            # 2026-07-14 on session b5996cb1: two agents rejected mid-flight.)
            if meta.get("toolUseId") in killed:
                state, done = "ended", True
            elif agent_id in terminal:
                notice = terminal[agent_id]
                notice_ep = iso_epoch(notice.get("ts"))
                child_ep = iso_epoch(t.last_ts)
                # A task id can be resumed. Only a notice at or after the newest
                # child row is terminal; later child output supersedes it.
                if notice_ep is not None and (child_ep is None or notice_ep >= child_ep):
                    state = "done" if notice.get("status") == "completed" else "ended"
                    done = True
            elif not done and parent_idle and quiet > 2 * cfg["agent_done_quiet_seconds"]:
                state = "ended"         # canceled/interrupted: no end_turn will ever come
                done = True             # finalize its spend in the ledger

            vel = self.velocity.setdefault(jl, deque(maxlen=cfg["velocity_window_points"]))
            if grew or not vel:
                vel.append((now, t.total_tokens))
            rate = 0.0
            if len(vel) >= 2:
                (t0, k0), (t1, k1) = vel[0], vel[-1]
                rate = (k1 - k0) / max(t1 - t0, 1e-9)

            fam = model_family(t.model)
            out.append({
                "agent_id": agent_id,
                "agent_type": meta.get("agentType", "?"),
                "description": meta.get("description", ""),
                "depth": meta.get("spawnDepth", 0),
                "model": t.model, "family": fam,
                "state": state, "quiet_s": round(quiet),
                "tokens": {"in": t.ti, "cache_write": t.tw, "cache_read": t.tr, "out": t.to},
                "total_tokens": t.total_tokens,
                "cost": round(t.cost(cfg), 4),
                "tok_per_s": round(rate, 1),
                "spark": [k for _, k in vel],
                "started": t.first_ts, "last": t.last_ts,
                "convo_v": t.convo_rev,     # cache key for the agent chat overlay
                # only running agents get a preview: a finished one's last line is its
                # final report, which the completed-agents view already shows
                "last_msg": (t.last_message(120 * int(cfg.get("preview_agent_lines", 1)))
                             if cfg.get("preview_agents") and not done else None),
            })
            if done:
                final_revision = (t.convo_rev, t.last_ts, state)
                final_key = (subdir, agent_id)
                if self._finalized_agent_revisions.get(final_key) != final_revision:
                    self.ledger_finalize(subdir, agent_id, meta, t)
                    self._finalized_agent_revisions[final_key] = final_revision
                    if len(self._finalized_agent_revisions) > 5000:
                        self._finalized_agent_revisions.pop(
                            next(iter(self._finalized_agent_revisions)))
        out.sort(key=lambda a: (a["state"] in ("done", "ended"), a["started"] or ""))
        return out

    # -------------------------------------------------------------- ledger
    def ensure_db(self):
        with self.db_lock:
            if self.db is not None:
                return self.db
            # This long-lived connection belongs to the sequential scan loop.
            # HTTP request threads use their own short-lived connections below.
            self.db = sqlite3.connect(os.path.join(BASE, "ledger.db"), check_same_thread=False)
            self.db.execute("""CREATE TABLE IF NOT EXISTS agent_runs(
                agent_id TEXT PRIMARY KEY, session_id TEXT, project TEXT,
                agent_type TEXT, model TEXT, description TEXT,
                in_tok INT, cw_tok INT, cr_tok INT, out_tok INT, cost REAL,
                started TEXT, ended TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS session_runs(
                session_id TEXT PRIMARY KEY, name TEXT, project TEXT, cwd TEXT,
                branch TEXT, model TEXT, cost REAL, agent_cost REAL, agents_total INT,
                bridge_url TEXT, first_seen INT, last_seen INT, closed_at INT)""")
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN title TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN provider TEXT DEFAULT 'claude'")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN transcript_path TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN status_line_json TEXT")
            except sqlite3.OperationalError:
                pass
            # rows are CUMULATIVE per transcript (path) — see Tail.stats
            self.db.execute("""CREATE TABLE IF NOT EXISTS usage_stats(
                path TEXT, day TEXT, kind TEXT, name TEXT,
                uses INT, chars INT, t_in INT, t_cw INT, t_cr INT, t_out INT, fam TEXT,
                PRIMARY KEY(path, day, kind, name))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS state_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, provider TEXT NOT NULL, at REAL NOT NULL,
                raw_state TEXT, reg_status TEXT, normalized_state TEXT, ui_group TEXT,
                reason TEXT, access TEXT, primary_action TEXT,
                evidence_kind TEXT, evidence_summary TEXT, revision TEXT,
                winning_rule TEXT, suppressed_rules TEXT, confidence TEXT,
                evidence_json TEXT, signature TEXT NOT NULL)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS state_events_session_id
                ON state_events(session_id, id DESC)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS session_links(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_session_id TEXT NOT NULL, source_provider TEXT NOT NULL,
                destination_session_id TEXT NOT NULL UNIQUE,
                destination_provider TEXT NOT NULL, created_at REAL NOT NULL,
                status TEXT NOT NULL, error TEXT, preview_hash TEXT NOT NULL)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS session_links_source
                ON session_links(source_session_id, id DESC)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS repo_actions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, root TEXT NOT NULL, worktree TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                summary TEXT, error TEXT, revision TEXT)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS repo_actions_root
                ON repo_actions(root, id DESC)""")
            self.db.commit()
            return self.db

    def ledger_reader(self):
        """Open a request-local ledger connection.

        Sharing the scan loop's sqlite connection with ThreadingHTTPServer can
        leave the connection poisoned after overlapping reads and writes even
        when the on-disk database passes quick_check. Request handlers therefore
        get an independent connection and close it after the response is built.
        """
        self.ensure_db()
        db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _safe_claude_transcript(sid, path):
        """Return a canonical top-level Claude transcript path or None.

        Session IDs and paths ultimately reach both a file reader and a terminal
        command. Require the exact UUID filename directly beneath one project
        directory; subagent files and symlinks escaping ~/.claude/projects fail.
        """
        if not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", str(sid or "")):
            return None
        if not path:
            return None
        real = os.path.realpath(os.path.expanduser(str(path)))
        root = os.path.realpath(PROJECTS)
        if os.path.dirname(os.path.dirname(real)) != root:
            return None
        if os.path.basename(real) != f"{sid}.jsonl" or not os.path.isfile(real):
            return None
        return real

    @staticmethod
    def _safe_reopen_cwd(cwd):
        if not cwd:
            return None
        real = os.path.realpath(os.path.expanduser(str(cwd)))
        home = os.path.realpath(HOME)
        if not os.path.isdir(real):
            return None
        if real != home and not real.startswith(home + os.sep):
            return None
        return real

    @staticmethod
    def _edge_json_objects(path, head_bytes=131_072, tail_bytes=262_144):
        """Decode bounded head/tail records without loading a large transcript."""
        size = os.path.getsize(path)
        chunks = []
        with open(path, "rb") as handle:
            if size <= head_bytes + tail_bytes:
                chunks.append(handle.read())
            else:
                head = handle.read(head_bytes)
                if b"\n" in head:
                    head = head[:head.rfind(b"\n") + 1]
                chunks.append(head)
                handle.seek(size - tail_bytes)
                tail = handle.read()
                if b"\n" in tail:
                    tail = tail[tail.find(b"\n") + 1:]
                chunks.append(tail)
        out = []
        for chunk in chunks:
            for raw in chunk.splitlines():
                if not raw or len(raw) > 4_000_000:
                    continue
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    out.append(value)
        return out

    @staticmethod
    def _history_titles():
        """Latest prompt-history label/cwd per Claude session, without prompt bodies."""
        out = {}
        try:
            handle = open(CLAUDE_HISTORY, errors="replace")
        except OSError:
            return out
        with handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                sid = str(row.get("sessionId") or "")
                if not re.fullmatch(
                        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", sid):
                    continue
                out[sid] = {"title": str(row.get("display") or "").strip()[:160],
                            "cwd": str(row.get("project") or ""),
                            "timestamp": float(row.get("timestamp") or 0) / 1000}
        return out

    def _claude_transcript_metadata(self, path, history=None):
        sid = os.path.splitext(os.path.basename(path))[0]
        history = history or {}
        objects = self._edge_json_objects(path)
        cwd = str(history.get("cwd") or "")
        branch = model = ai_title = custom_title = first_prompt = ""
        first_epoch = None
        for row in objects:
            cwd = str(row.get("cwd") or cwd)
            branch = str(row.get("gitBranch") or branch)
            typ = row.get("type")
            if typ == "ai-title":
                ai_title = str(row.get("aiTitle") or ai_title).strip()
            elif typ == "custom-title":
                custom_title = str(row.get("customTitle") or custom_title).strip()
            ts = iso_epoch(row.get("timestamp"))
            if ts is not None and first_epoch is None:
                first_epoch = ts
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                model = str(message.get("model") or model)
            elif message.get("role") == "user" and not row.get("isMeta") \
                    and not first_prompt:
                content = message.get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        str(block.get("text") or "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
                text = re.sub(r"<system-reminder>.*?</system-reminder>", " ",
                              str(content or ""), flags=re.S).strip()
                if text and not text.startswith(("<command-", "<local-command")):
                    first_prompt = re.sub(r"\s+", " ", text)[:160]
        stat = os.stat(path)
        created = first_epoch or getattr(stat, "st_birthtime", stat.st_ctime)
        last = max(stat.st_mtime, float(history.get("timestamp") or 0))
        title = (custom_title or ai_title or history.get("title") or first_prompt or
                 os.path.basename(cwd) or "Claude session")
        return {"session_id": sid, "name": title[:160], "title": title[:160],
                "cwd": cwd, "project": os.path.basename(cwd) or
                os.path.basename(os.path.dirname(path)), "branch": branch,
                "model": model, "first_seen": int(created), "last_seen": int(last),
                "closed_at": int(last), "transcript_path": path}

    def backfill_claude_history(self):
        """Index every resumable top-level Claude transcript exactly once per path."""
        db = self.ensure_db()
        known = dict(db.execute(
            "SELECT session_id, transcript_path FROM session_runs WHERE provider='claude'"
        ).fetchall())
        history = self._history_titles()
        imported = 0
        for candidate in sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))):
            sid = os.path.splitext(os.path.basename(candidate))[0]
            path = self._safe_claude_transcript(sid, candidate)
            if not path or known.get(sid) == path:
                continue
            meta = self._claude_transcript_metadata(path, history.get(sid))
            db.execute("""INSERT INTO session_runs(session_id,name,project,cwd,branch,
                model,cost,agent_cost,agents_total,bridge_url,first_seen,last_seen,
                closed_at,title,provider,transcript_path)
                VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,?, 'claude',?)
                ON CONFLICT(session_id) DO UPDATE SET
                transcript_path=excluded.transcript_path,
                cwd=CASE WHEN session_runs.cwd IS NULL OR session_runs.cwd=''
                         THEN excluded.cwd ELSE session_runs.cwd END,
                project=CASE WHEN session_runs.project IS NULL OR session_runs.project=''
                             THEN excluded.project ELSE session_runs.project END,
                branch=CASE WHEN session_runs.branch IS NULL OR session_runs.branch=''
                            THEN excluded.branch ELSE session_runs.branch END,
                model=CASE WHEN session_runs.model IS NULL OR session_runs.model=''
                           THEN excluded.model ELSE session_runs.model END,
                title=CASE WHEN session_runs.title IS NULL OR session_runs.title=''
                           THEN excluded.title ELSE session_runs.title END,
                name=CASE WHEN session_runs.name IS NULL OR session_runs.name=''
                          THEN excluded.name ELSE session_runs.name END,
                provider='claude'""",
                (sid, meta["name"], meta["project"], meta["cwd"], meta["branch"],
                 meta["model"], meta["first_seen"], meta["last_seen"],
                 meta["closed_at"], meta["title"], path))
            known[sid] = path
            imported += 1
        db.commit()
        return imported

    def drain_stats(self, t):
        """Flush a tail's dirty usage stats. INSERT OR REPLACE of cumulative
        counts keeps restarts idempotent (never switch this to additive)."""
        if not t.stats_dirty:
            return
        try:
            db = self.ensure_db()
            fam = model_family(t.model)
            db.executemany(
                "INSERT OR REPLACE INTO usage_stats VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(t.path, k[0], k[1], k[2], *t.stats[k], fam) for k in t.stats_dirty])
            t.stats_dirty.clear()
            db.commit()
        except Exception as e:
            print(f"usage stats error: {e}", file=sys.stderr, flush=True)

    def ledger_finalize(self, subdir, agent_id, meta, t):
        # subdir = .../projects/<proj>/<sid>/subagents
        sid = os.path.basename(os.path.dirname(subdir))
        proj = os.path.basename(os.path.dirname(os.path.dirname(subdir)))
        db = self.ensure_db()
        db.execute("""INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(agent_id) DO UPDATE SET
                      in_tok=excluded.in_tok, cw_tok=excluded.cw_tok,
                      cr_tok=excluded.cr_tok, out_tok=excluded.out_tok,
                      cost=excluded.cost, ended=excluded.ended""",
                   (agent_id, sid, proj, meta.get("agentType", "?"), t.model,
                    (meta.get("description") or "")[:200], t.ti, t.tw, t.tr, t.to,
                    t.cost(self.cfg), t.first_ts, t.last_ts))
        db.commit()

    def record_sessions(self, sessions, now):
        try:
            db = self.ensure_db()
            dirty = False
            for s in sessions:
                status_line_json = json.dumps(
                    s.get("status_line") or {}, separators=(",", ":"))
                if len(status_line_json) > 100_000:
                    status_line_json = None
                status_signature = dict(s.get("status_line") or {})
                status_signature.pop("git_observed_at", None)
                signature = (
                    s.get("name"), s.get("project"), s.get("cwd"), s.get("branch"),
                    s.get("model"), s.get("cost"), s.get("agent_cost"),
                    s.get("agents_total"), s.get("bridge_url"), s.get("title"),
                    s.get("provider", "claude"), json.dumps(
                        status_signature, separators=(",", ":")))
                sid = s["session_id"]
                due = now - self._session_ledger_written_at.get(sid, 0) >= 30
                if self._session_ledger_signatures.get(sid) == signature and not due:
                    continue
                db.execute("""INSERT INTO session_runs(session_id,name,project,cwd,branch,
                    model,cost,agent_cost,agents_total,bridge_url,first_seen,last_seen,
                    closed_at,title,provider,transcript_path,status_line_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                    name=excluded.name, project=excluded.project, branch=excluded.branch,
                    model=excluded.model, cost=excluded.cost, agent_cost=excluded.agent_cost,
                    agents_total=excluded.agents_total, bridge_url=excluded.bridge_url,
                    last_seen=excluded.last_seen, closed_at=NULL, title=excluded.title,
                    provider=excluded.provider,
                    transcript_path=COALESCE(excluded.transcript_path,
                                             session_runs.transcript_path),
                    status_line_json=COALESCE(excluded.status_line_json,
                                              session_runs.status_line_json)""",
                    (s["session_id"], s["name"], s["project"], s["cwd"], s["branch"],
                     s["model"], s["cost"], s["agent_cost"], s["agents_total"],
                     s["bridge_url"], int(now), int(now), s["title"],
                     s.get("provider", "claude"),
                     (os.path.join(cwd_to_project_dir(s.get("cwd") or ""),
                                   f"{s['session_id']}.jsonl")
                     if s.get("provider", "claude") == "claude" else None),
                     status_line_json))
                self._session_ledger_signatures[sid] = signature
                self._session_ledger_written_at[sid] = now
                dirty = True
            live = {s["session_id"] for s in sessions}
            if self._session_ledger_live_ids is None or live != self._session_ledger_live_ids:
                ordered = sorted(live)
                marks = ",".join("?" * len(ordered)) or "''"
                db.execute(f"""UPDATE session_runs SET closed_at=?
                               WHERE closed_at IS NULL AND session_id NOT IN ({marks})""",
                           [int(now)] + ordered)
                self._session_ledger_live_ids = live
                dirty = True
            for sid in set(self._session_ledger_signatures) - live:
                self._session_ledger_signatures.pop(sid, None)
                self._session_ledger_written_at.pop(sid, None)
            if dirty:
                db.commit()
        except Exception as e:
            print(f"session ledger error: {e}", file=sys.stderr, flush=True)

    @staticmethod
    def _state_event_record(session, now):
        evidence = []
        for fact in (session.get("state_evidence") or [])[:12]:
            if not isinstance(fact, dict):
                continue
            evidence.append({
                "kind": _fact_text(fact.get("kind"), 40),
                "label": _fact_text(fact.get("label"), 80),
                "value": _fact_text(fact.get("value"), 220),
                "confidence": (_fact_text(fact.get("confidence"), 20) or "unknown"),
            })
        summary = "; ".join(
            f"{fact['label']}: {fact['value']}" for fact in evidence)[:1200]
        pending = session.get("pending") or {}
        stable = {
            "raw_state": str(session.get("state") or "unknown"),
            "reg_status": str(session.get("reg_status") or ""),
            "normalized_state": str(session.get("normalized_state") or
                                    session.get("state") or "unknown"),
            "ui_group": str(session.get("ui_group") or "history"),
            "reason": str(session.get("reason_label") or ""),
            "access": str(session.get("access") or "view_only"),
            "primary_action": str(session.get("primary_action") or "view"),
            "winning_rule": str(session.get("winning_rule") or "placement.unknown"),
            "suppressed_rules": list(session.get("suppressed_rules") or [])[:20],
            "confidence": str(session.get("state_confidence") or "unknown"),
            "pending_nonce": str(pending.get("nonce") or ""),
            "reply_requested": bool(session.get("reply_requested")),
            "new_response": bool(session.get("new_response")),
            "provider_stale": bool(session.get("provider_stale")),
        }
        signature = hashlib.sha256(json.dumps(
            stable, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            **stable, "session_id": str(session.get("session_id") or ""),
            "provider": str(session.get("provider") or "claude"), "at": float(now),
            "evidence_kind": evidence[0]["kind"] if evidence else "unknown",
            "evidence_summary": summary, "evidence": evidence,
            "revision": str(session.get("convo_v") or session.get("closed_at") or ""),
            "signature": signature,
        }

    def record_state_events(self, sessions, now):
        """Persist only meaningful consecutive placement changes."""
        try:
            db = self.ensure_db()
            if self._state_event_signatures is None:
                rows = db.execute("""SELECT event.session_id,event.signature,
                                             event.normalized_state
                    FROM state_events event JOIN (
                        SELECT session_id,MAX(id) AS latest_id FROM state_events
                        GROUP BY session_id
                    ) latest ON latest.latest_id=event.id""").fetchall()
                self._state_event_signatures = {
                    sid: (signature, normalized_state)
                    for sid, signature, normalized_state in rows}
            dirty = False
            for session in sessions:
                sid = str(session.get("session_id") or "")
                previous = self._state_event_signatures.get(sid)
                # Closed ledger rows are immutable. Once their close transition
                # is journaled, skip them before rebuilding evidence/hashes on
                # every two-second live poll.
                if (session.get("normalized_state") == "closed" and previous
                        and previous[1] == "closed"):
                    continue
                record = self._state_event_record(session, now)
                if not record["session_id"]:
                    continue
                previous = self._state_event_signatures.get(record["session_id"])
                if previous and previous[0] == record["signature"]:
                    continue
                db.execute("""INSERT INTO state_events(
                    session_id,provider,at,raw_state,reg_status,normalized_state,ui_group,
                    reason,access,primary_action,evidence_kind,evidence_summary,revision,
                    winning_rule,suppressed_rules,confidence,evidence_json,signature)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    record["session_id"], record["provider"], record["at"],
                    record["raw_state"], record["reg_status"], record["normalized_state"],
                    record["ui_group"], record["reason"], record["access"],
                    record["primary_action"], record["evidence_kind"],
                    record["evidence_summary"], record["revision"],
                    record["winning_rule"], json.dumps(record["suppressed_rules"]),
                    record["confidence"], json.dumps(record["evidence"], separators=(",", ":")),
                    record["signature"]))
                self._state_event_signatures[record["session_id"]] = (
                    record["signature"], record["normalized_state"])
                dirty = True
            if dirty:
                db.commit()
        except Exception as exc:
            print(f"state journal error: {exc}", file=sys.stderr, flush=True)

    def state_history(self, sid, cursor=0, limit=40):
        sid = str(sid or "")
        if not sid or len(sid) > 300 or any(ord(char) < 32 for char in sid):
            return {"ok": False, "error": "invalid session id"}
        try:
            cursor = int(cursor or 0)
            limit = int(limit or 40)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid evidence cursor or limit"}
        if cursor < 0 or not 1 <= limit <= 100:
            return {"ok": False, "error": "evidence limit must be 1–100"}
        cols = ("id", "session_id", "provider", "at", "raw_state", "reg_status",
                "normalized_state", "ui_group", "reason", "access", "primary_action",
                "evidence_kind", "evidence_summary", "revision", "winning_rule",
                "suppressed_rules", "confidence", "evidence_json")
        where = "session_id=?" + (" AND id<?" if cursor else "")
        params = [sid] + ([cursor] if cursor else []) + [limit + 1]
        try:
            db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=2)
            rows = db.execute(
                f"SELECT {','.join(cols)} FROM state_events WHERE {where} "
                "ORDER BY id DESC LIMIT ?", params).fetchall()
            db.close()
        except Exception as exc:
            return {"ok": False, "error": f"state history unavailable: {exc}"}
        more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            event = dict(zip(cols, row))
            try:
                event["suppressed_rules"] = json.loads(event["suppressed_rules"] or "[]")
            except ValueError:
                event["suppressed_rules"] = []
            try:
                event["evidence"] = json.loads(event.pop("evidence_json") or "[]")
            except ValueError:
                event["evidence"] = []
            events.append(event)
        with self.lock:
            current = next((item for item in
                            list(self.snapshot_cache.get("sessions") or []) +
                            list(self.snapshot_cache.get("closed") or [])
                            if item.get("session_id") == sid), None)
            if current:
                current = {key: current.get(key) for key in (
                    "session_id", "provider", "state", "normalized_state", "reg_status",
                    "ui_group", "reason_label", "access", "access_label",
                    "primary_action", "primary_action_label", "winning_rule",
                    "suppressed_rules", "state_confidence", "state_evidence",
                    "provider_stale", "activity_at", "convo_v")}
        return {"ok": True, "session_id": sid, "current": current,
                "events": events,
                "next_cursor": events[-1]["id"] if more and events else None}

    def _record_handoff_link(self, source_sid, source_provider, destination_sid,
                             destination_provider, status, preview_hash, error=None):
        """Persist identity/status only. The edited handoff body is never retained."""
        now = time.time()
        self.ensure_db()
        db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=2)
        try:
            db.execute("""INSERT INTO session_links(
                source_session_id,source_provider,destination_session_id,
                destination_provider,created_at,status,error,preview_hash)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(destination_session_id) DO UPDATE SET
                  status=excluded.status,error=excluded.error,
                  preview_hash=excluded.preview_hash""", (
                str(source_sid), str(source_provider), str(destination_sid),
                str(destination_provider), now, str(status),
                str(error)[:1000] if error else None, str(preview_hash)))
            db.commit()
        finally:
            db.close()
        with self.lock:
            self._handoff_links_version += 1
            self._handoff_links_cache = None

    def _handoff_link(self, source_sid, destination_sid):
        self.ensure_db()
        db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=2)
        try:
            row = db.execute("""SELECT source_session_id,source_provider,
                destination_session_id,destination_provider,created_at,status,error,preview_hash
                FROM session_links WHERE source_session_id=? AND destination_session_id=?""",
                (str(source_sid), str(destination_sid))).fetchone()
        finally:
            db.close()
        if not row:
            return None
        keys = ("source_session_id", "source_provider", "destination_session_id",
                "destination_provider", "created_at", "status", "error", "preview_hash")
        return dict(zip(keys, row))

    def handoff_link_map(self, session_ids):
        ids = {str(sid) for sid in session_ids if sid}
        if not ids:
            return {}
        with self.lock:
            cached = self._handoff_links_cache
            version = self._handoff_links_version
        if cached and cached[0] == version:
            rows = cached[1]
        else:
            self.ensure_db()
            db = sqlite3.connect(os.path.join(BASE, "ledger.db"), timeout=2)
            try:
                rows = db.execute("""SELECT source_session_id,source_provider,
                    destination_session_id,destination_provider,created_at,status,error
                    FROM session_links ORDER BY id DESC""").fetchall()
            finally:
                db.close()
            with self.lock:
                if version == self._handoff_links_version:
                    self._handoff_links_cache = (version, rows)
        out = {sid: [] for sid in ids}
        for source_sid, source_provider, destination_sid, destination_provider, created_at, status, error in rows:
            if source_sid in ids:
                out[source_sid].append({"direction": "from", "session_id": destination_sid,
                    "provider": destination_provider, "status": status,
                    "created_at": created_at, "error": error})
            if destination_sid in ids:
                out[destination_sid].append({"direction": "to", "session_id": source_sid,
                    "provider": source_provider, "status": status,
                    "created_at": created_at, "error": error})
        return {sid: links for sid, links in out.items() if links}

    def _find_handoff_source(self, sid):
        sid = str(sid or "")
        if not sid or len(sid) > 300 or any(ord(char) < 32 for char in sid):
            return None
        with self.lock:
            records = (list(self.snapshot_cache.get("sessions") or []) +
                       list(self.snapshot_cache.get("closed") or []))
        return next((dict(item) for item in records
                     if str(item.get("session_id") or "") == sid), None)

    @staticmethod
    def _handoff_artifact_section(artifacts):
        if not artifacts:
            return "[Selected artifact references]\n(none)\n[End artifact references]"
        lines = ["[Selected artifact references]"]
        for artifact in artifacts[:20]:
            caption = str(artifact.get("caption") or "").strip()
            suffix = f" — {caption}" if caption else ""
            lines.append(f"- {artifact.get('path')}{suffix}")
        lines.append("[End artifact references]")
        return "\n".join(lines)

    def handoff_preview(self, sid, target_provider):
        target = str(target_provider or "").strip().lower()
        if target not in ("claude", "codex"):
            return {"ok": False, "error": "provider must be claude or codex"}
        source = self._find_handoff_source(sid)
        if not source:
            return {"ok": False, "error": "session is unavailable"}
        source_provider = str(source.get("provider") or "claude")
        closed = bool(source.get("closed_at") is not None or
                      source.get("normalized_state") == "closed")
        context = self.closed_context(sid) if closed else self.session_context(sid)
        indexed = None
        search = getattr(self, "search", None)
        if search and hasattr(search, "handoff_material"):
            try:
                candidate = search.handoff_material(sid, 8)
                if candidate.get("ok"):
                    indexed = candidate
            except Exception as exc:
                print(f"handoff index fallback for {sid}: {exc}", file=sys.stderr,
                      flush=True)
        messages = (indexed or {}).get("recent") or (context.get("messages") or [])[-8:]
        objective = str((indexed or {}).get("first_user") or "").strip()
        if not objective:
            objective = next((str(item.get("text") or "").strip()
                              for item in (context.get("messages") or [])
                              if item.get("role") == "user" and item.get("text")), "")
        objective = objective or source.get("title") or source.get("project") or "Continue the work"
        recent = []
        for item in messages[-8:]:
            role = str(item.get("role") or "event")
            text = str(item.get("text") or item.get("detail") or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            recent.append(f"{role.title()}: {text[:4000]}")
        unresolved = []
        pending = source.get("pending") or {}
        if pending.get("kind") == "question":
            unresolved.extend(str(q.get("question") or q.get("header") or "").strip()
                               for q in (pending.get("questions") or []))
        elif pending:
            unresolved.append(str(pending.get("input_summary") or
                                  pending.get("tool") or pending.get("kind") or ""))
        if source.get("error"):
            unresolved.append(str(source.get("error")))
        unresolved.extend((indexed or {}).get("todos") or [])
        if source.get("reply_requested"):
            unresolved.append("The source session requested a user reply.")
        artifacts, seen = [], set()
        candidates = list((indexed or {}).get("artifacts") or [])
        candidates.extend(context.get("files") or [])
        for item in candidates:
            path = os.path.realpath(os.path.expanduser(str(item.get("path") or "")))
            if not path or path in seen or not path.startswith(os.path.realpath(HOME) + os.sep):
                continue
            seen.add(path)
            artifacts.append({"path": path, "name": os.path.basename(path),
                              "caption": str(item.get("caption") or "")[:300],
                              "missing": not os.path.isfile(path)})
        cwd = str(source.get("cwd") or "")
        identity = self.workstream_identity(cwd) if cwd else {}
        unresolved_text = "\n".join(f"- {text[:800]}" for text in unresolved if text) or "- None recorded"
        recent_text = "\n\n".join(recent) or "No recent prose was available."
        preview = f"""Continue this work in a new, independent {target.title()} coding session.

Source session: {source_provider} · {sid}
Repository: {source.get('project') or os.path.basename(cwd) or 'unknown'}
Working directory: {cwd or 'unknown'}
Branch: {source.get('branch') or 'unknown'}

Objective
{objective[:6000]}

Recent conversation
{recent_text}

Unresolved work
{unresolved_text}

Repository evidence
- Canonical repository: {identity.get('root') or cwd or 'unknown'}
- Changed files: not observed yet
- Tests: not observed yet
- Pull request: not observed yet

{self._handoff_artifact_section(artifacts)}

Treat this as an independent session. Verify the repository state before changing files, and do not assume the source session has stopped."""
        default_model = source.get("model") if target == source_provider else ""
        if target == "claude" and default_model not in self.MODELS:
            family = model_family(default_model)
            default_model = family if family in self.MODELS else ""
        default_effort = source.get("effort") if target == source_provider else ""
        if target == "claude" and default_effort not in self.EFFORTS:
            default_effort = ""
        defaults = {"cwd": cwd, "model": default_model,
                    "effort": default_effort,
                    "mode": (source.get("collaboration_mode") or "plan") if target == "codex" else None,
                    "worktree": False, "worktree_name": ""}
        return {"ok": True, "source": {key: source.get(key) for key in
                ("session_id", "provider", "title", "project", "cwd", "branch", "model",
                 "effort", "collaboration_mode")}, "target_provider": target,
                "preview": redact_handoff_text(preview), "artifacts": artifacts,
                "defaults": defaults, "independent_session": True}

    def closed_sessions(self):
        cols = ("session_id", "name", "project", "cwd", "branch", "model", "cost",
                "agent_cost", "agents_total", "bridge_url", "first_seen", "last_seen",
                "closed_at", "title", "provider", "transcript_path", "status_line_json")
        db = None
        try:
            db = self.ledger_reader()
            signature = db.execute("""SELECT COUNT(*),MAX(closed_at)
                FROM session_runs WHERE closed_at IS NOT NULL""").fetchone()
            now_mono = time.monotonic()
            cached = self._closed_sessions_cache
            if cached and cached[0] == signature and now_mono < cached[1]:
                return copy.deepcopy(cached[2])
            rows = db.execute(
                f"""SELECT {','.join(cols)} FROM session_runs
                    WHERE closed_at IS NOT NULL ORDER BY closed_at DESC""").fetchall()
            out = [dict(zip(cols, r)) for r in rows]
            for row in out:
                try:
                    status_line = json.loads(row.pop("status_line_json") or "{}")
                except (TypeError, ValueError):
                    status_line = {}
                if isinstance(status_line, dict) and status_line:
                    status_line["frozen"] = True
                    row["status_line"] = status_line
                row["can_reopen"] = bool(
                    row.get("provider") == "claude" and
                    self._safe_claude_transcript(row.get("session_id"),
                                                 row.get("transcript_path")) and
                    self._safe_reopen_cwd(row.get("cwd")))
            self._closed_sessions_cache = (signature, now_mono + 60, copy.deepcopy(out))
            return out
        except Exception:
            return []
        finally:
            if db is not None:
                db.close()

    def closed_context(self, sid):
        """Conversation of a CLOSED session: its process is gone, so the registry
        can't resolve it — the ledger's cwd is the only path back to the file."""
        row = None
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute(
                "SELECT cwd, model, cost, title, project, branch, transcript_path, "
                "status_line_json "
                "FROM session_runs "
                "WHERE session_id = ?", (sid,)).fetchone()
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()
        if not row and str(sid).startswith("codex:"):
            return self.codex.context(sid)
        if not row:
            return {"ok": False, "error": "unknown session"}
        try:
            status_line = json.loads(row[7] or "{}")
        except (TypeError, ValueError):
            status_line = {}
        if isinstance(status_line, dict) and status_line:
            status_line["frozen"] = True
        else:
            status_line = None
        if str(sid).startswith("codex:"):
            result = self.codex.context(sid)
            if result.get("ok"):
                result.setdefault("info", {})["status_line"] = status_line
            return result
        fallback = os.path.join(cwd_to_project_dir(row[0] or ""), f"{sid}.jsonl")
        path = self._safe_claude_transcript(sid, row[6] or fallback)
        if not path:
            return {"ok": False, "error": "transcript is gone"}
        with self.scan_lock:
            t = self.tail_for(path)
            t.poll()
            msgs = [dict(m) for m in t.convo]
        for m in msgs:              # file chips need the same metadata the live view builds
            if m.get("role") == "tool" and m.get("files"):
                m["files"] = [{"name": os.path.basename(p), "path": p,
                               "kind": "image" if os.path.splitext(p)[1].lower() in IMG_EXTS else "text",
                               "missing": not os.path.isfile(p)} for p in m["files"]]
        return {"ok": True, "messages": msgs, "closed": True,
                "info": {"session_id": sid, "cwd": row[0], "model": row[1],
                         "cost": row[2], "title": row[3], "project": row[4],
                         "branch": row[5],
                         "status_line": status_line,
                         "can_reopen": bool(self._safe_reopen_cwd(row[0]))}}

    @staticmethod
    def trusted_dirs():
        """Dirs where Claude Code's "do you trust this folder?" prompt is already
        answered (~/.claude.json `projects[dir].hasTrustDialogAccepted`). A spawn
        into an UNTRUSTED dir stops at that prompt, which only the Mac can answer —
        so the picker flags them instead of pretending a remote start will work. We
        never WRITE this flag: it is a security gate, not a preference."""
        try:
            with open(os.path.join(HOME, ".claude.json")) as f:
                projects = (json.load(f) or {}).get("projects") or {}
        except Exception:
            return set()
        return {d for d, v in projects.items()
                if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}

    def is_trusted(self, path, trusted=None):
        """Trust is INHERITED: a git worktree under a trusted repo has no entry of
        its own in ~/.claude.json yet never prompts (verified 2026-07-14 — every
        Quirk worktree is absent from `projects` and starts clean), while a fresh
        dir with no trusted ancestor does prompt. So walk up to /."""
        trusted = self.trusted_dirs() if trusted is None else trusted
        p = os.path.realpath(path)
        while True:
            if p in trusted:
                return True
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent

    def recent_dirs(self, limit=25):
        """Directories the daemon has actually seen sessions in — the new-session
        picker's menu (a phone has no file browser). Only offers dirs a spawn would
        actually accept: never list what spawn_session will refuse."""
        home = os.path.realpath(HOME)
        trusted = self.trusted_dirs()

        def ok(d):
            if not d or not os.path.isdir(d):
                return False
            rp = os.path.realpath(d)
            return rp == home or rp.startswith(home + os.sep)

        paths = []
        db = None
        try:
            db = self.ledger_reader()
            rows = db.execute(
                "SELECT cwd, MAX(COALESCE(last_seen, 0)) t FROM session_runs "
                "WHERE cwd IS NOT NULL AND cwd != '' GROUP BY cwd "
                "ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
            paths = [r[0] for r in rows if ok(r[0])]
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()
        for r in self.live_sessions():           # live cwds first, even if unledgered
            cwd = r.get("cwd")
            if ok(cwd) and cwd not in paths:
                paths.insert(0, cwd)
        return [{"path": p, "trusted": self.is_trusted(p, trusted)} for p in paths]

    def insights(self, days=7):
        """Aggregated where-does-the-money-go view: agent_runs + session_runs
        (real $ from the ledger) + usage_stats (tool/skill volumes; skill $ is
        the attributed cost of turns run while that skill was active)."""
        cfg = self.cfg
        since_d = f"-{int(days)} days"
        since_e = int(time.time()) - int(days) * 86400
        out = {"days": days}
        db = None
        try:
            db = self.ledger_reader()
            rows = db.execute("""SELECT agent_type, count(*), sum(cost), sum(in_tok),
                sum(cw_tok), sum(cr_tok), sum(out_tok) FROM agent_runs
                WHERE started >= date('now', ?) GROUP BY agent_type
                ORDER BY sum(cost) DESC""", (since_d,)).fetchall()
            out["agents"] = [{"name": r[0], "runs": r[1], "cost": round(r[2] or 0, 2),
                              "avg": round((r[2] or 0) / max(r[1], 1), 3),
                              "cache_pct": round(100 * (r[5] or 0) /
                                                 max((r[3] or 0) + (r[4] or 0) + (r[5] or 0), 1))}
                             for r in rows]
            # skills: price the attributed tokens at each transcript's model rates
            sk = {}
            for name, fam, uses, ti, tw, tr, to_ in db.execute(
                    """SELECT name, fam, sum(uses), sum(t_in), sum(t_cw), sum(t_cr),
                       sum(t_out) FROM usage_stats WHERE kind='skill' AND day >= date('now', ?)
                       GROUP BY name, fam""", (since_d,)):
                e = sk.setdefault(name, {"name": name, "uses": 0, "cost": 0.0})
                e["uses"] += uses or 0
                e["cost"] += usd(cfg, fam, ti or 0, tw or 0, tr or 0, to_ or 0)
            out["skills"] = sorted(
                [{**e, "cost": round(e["cost"], 2),
                  "avg": round(e["cost"] / max(e["uses"], 1), 3)} for e in sk.values()],
                key=lambda x: -x["cost"])
            out["tools"] = [{"name": r[0], "uses": r[1] or 0, "tokens": round((r[2] or 0) / 4),
                             "avg_tokens": round((r[2] or 0) / 4 / max(r[1] or 1, 1))}
                            for r in db.execute(
                    """SELECT name, sum(uses), sum(chars) FROM usage_stats
                       WHERE kind='tool' AND day >= date('now', ?)
                       GROUP BY name ORDER BY sum(chars) DESC""", (since_d,))]
            fams = {}
            for model, cost in db.execute(
                    "SELECT model, sum(cost) FROM agent_runs WHERE started >= date('now', ?) "
                    "GROUP BY model", (since_d,)):
                f = fams.setdefault(model_family(model), {"agents": 0.0, "sessions": 0.0})
                f["agents"] += cost or 0
            for model, cost in db.execute(
                    "SELECT model, sum(cost) FROM session_runs WHERE last_seen >= ? "
                    "GROUP BY model", (since_e,)):
                f = fams.setdefault(model_family(model), {"agents": 0.0, "sessions": 0.0})
                f["sessions"] += cost or 0
            out["models"] = sorted(
                [{"name": k, "agents": round(v["agents"], 2), "sessions": round(v["sessions"], 2)}
                 for k, v in fams.items()],
                key=lambda x: -(x["agents"] + x["sessions"]))
            out["projects"] = [{"name": r[0] or "?", "agents": round(r[1] or 0, 2),
                                "sessions": round(r[2] or 0, 2)}
                               for r in db.execute(
                    """SELECT project, sum(agent_cost), sum(cost) FROM session_runs
                       WHERE last_seen >= ? GROUP BY project
                       ORDER BY sum(cost)+sum(agent_cost) DESC""", (since_e,))]
            out["by_day"] = [{"day": r[0], "cost": round(r[1] or 0, 2)}
                             for r in db.execute(
                    """SELECT date(started) d, sum(cost) FROM agent_runs
                       WHERE started >= date('now', ?) GROUP BY d ORDER BY d DESC""",
                    (since_d,))]
            out["top_sessions"] = [{"title": r[0] or r[1], "project": r[2],
                                    "cost": round((r[3] or 0) + (r[4] or 0), 2)}
                                   for r in db.execute(
                    """SELECT title, name, project, cost, agent_cost FROM session_runs
                       WHERE last_seen >= ? ORDER BY cost + agent_cost DESC LIMIT 12""",
                    (since_e,))]
            ca = {}
            for name, fam, ev, tok in db.execute(
                    """SELECT name, fam, sum(uses), sum(chars) FROM usage_stats
                       WHERE kind='cache' AND day >= date('now', ?) GROUP BY name, fam""",
                    (since_d,)):
                ri, rw, rr, ro = cfg["rates"].get(fam, cfg["rates"]["opus"])
                e = ca.setdefault(name, {"name": name, "events": 0, "tokens": 0, "cost": 0.0})
                e["events"] += ev or 0
                e["tokens"] += tok or 0
                e["cost"] += (tok or 0) * (rw - rr) / 1e6   # re-paid at write vs read rate
            out["cache_busts"] = sorted(
                [{**e, "cost": round(e["cost"], 2)} for e in ca.values()],
                key=lambda x: -x["cost"])
            mix = {}
            for day, fam, ti, tw, tr, to_ in db.execute(
                    """SELECT day, fam, sum(t_in), sum(t_cw), sum(t_cr), sum(t_out)
                       FROM usage_stats WHERE kind='tokens' AND day >= date('now', ?)
                       GROUP BY day, fam""", (since_d,)):
                ri, rw, rr, ro = cfg["rates"].get(fam, cfg["rates"]["opus"])
                e = mix.setdefault(day, {"day": day, "input": 0.0, "write": 0.0,
                                         "read": 0.0, "output": 0.0})
                e["input"] += (ti or 0) * ri / 1e6
                e["write"] += (tw or 0) * rw / 1e6
                e["read"] += (tr or 0) * rr / 1e6
                e["output"] += (to_ or 0) * ro / 1e6
            out["token_mix"] = sorted(
                [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in e.items()}
                 for e in mix.values()], key=lambda x: x["day"], reverse=True)
            out["totals"] = {
                "agent_cost": round(sum(a["cost"] for a in out["agents"]), 2),
                "session_cost": round(sum(p["sessions"] for p in out["projects"]), 2),
                "bust_cost": round(sum(c["cost"] for c in out["cache_busts"]), 2),
            }
            out["ok"] = True
        except Exception as e:
            print(f"insights error: {e}", file=sys.stderr, flush=True)
            out.update(ok=False, error=str(e))
        finally:
            if db is not None:
                db.close()
        return out

    def hook_pending(self, sid, reg_status):
        """Pending prompt captured by the PreToolUse/Notification hooks."""
        path = os.path.join(BASE, "pending", f"{sid}.json")
        try:
            d = json.load(open(path))
        except Exception:
            return None
        # a question stays valid while the session waits; permission notifications
        # have no clear-event, so expire them once the session stops waiting
        if reg_status != "waiting" and time.time() - d.get("ts", 0) > 15:
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        if d.get("kind") == "question":
            # ghost guard: a PreToolUse capture can outlive an ask another hook
            # blocked — hide it unless the session is (or just became) waiting
            if reg_status != "waiting" and time.time() - d.get("ts", 0) > 5:
                return None
            return {"kind": "question", "nonce": d["nonce"], "questions": d.get("questions", []),
                    "_ts": d.get("ts")}
        if d.get("kind") == "permission":
            return {"kind": "permission", "nonce": d["nonce"], "tool": "requested tool",
                    "input_summary": d.get("message", "")}
        return None

    def _paired_files(self, mt, q_epoch):
        win = self.cfg.get("question_file_pair_seconds", 300)
        out = []
        for f in mt.files:
            fe = iso_epoch(f["ts"])
            if fe is None or not (q_epoch - win <= fe <= q_epoch + 30):
                continue
            p = f["path"]
            out.append({"name": os.path.basename(p), "path": p, "caption": f["caption"],
                        "kind": "image" if os.path.splitext(p)[1].lower() in IMG_EXTS else "text",
                        "missing": not os.path.isfile(p)})
        return out[-3:]

    # ------------------------------------------------------- context + files
    def _reg_main_path(self, sid):
        reg = next((r for r in self.live_sessions() if r.get("sessionId") == sid), None)
        if not reg:
            return None, None
        return reg, os.path.join(cwd_to_project_dir(reg.get("cwd", "")), f"{sid}.jsonl")

    def agent_effort(self, agent_type, cwd, parent_effort):
        """Effort for a subagent.

        A subagent's effort is never in its transcript, but agent definitions PIN it
        in frontmatter (`effort: high`). An agent with no pin inherits the parent
        session's effort — which is exactly what the runtime does, so reporting the
        parent's value is accurate, not a guess. Plugin-namespaced types
        (`plugin:agent`) have no local file: fall back to the parent."""
        if not agent_type or ":" in agent_type:
            return parent_effort
        for root in (os.path.join(cwd, ".claude"), os.path.join(HOME, ".claude")):
            p = os.path.join(root, "agents", f"{agent_type}.md")
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            hit = self._agent_eff.get(p)
            if hit and hit[0] == mtime:
                return hit[1] or parent_effort
            try:
                with open(p, errors="replace") as f:
                    head = f.read(2000)
            except OSError:
                continue
            m = re.search(r"^effort:\s*(\w+)", head, re.M)
            eff = m.group(1) if m and m.group(1) in self.EFFORTS else None
            self._agent_eff[p] = (mtime, eff)
            return eff or parent_effort
        return parent_effort

    @staticmethod
    def effort_for(sid):
        """Effort level ('high', 'max', …) for a session.

        It exists ONLY in the statusline payload Claude Code pipes to the statusline
        command (`"effort":{"level":…}`) — not in the transcript, not in the session
        registry. So the statusline script side-writes it here (see its
        `fleet-dash effort side-write` block); no statusline, no effort."""
        try:
            with open(os.path.join(BASE, "effort", sid)) as f:
                v = f.read().strip()
        except OSError:
            return None
        return v if v in Engine.EFFORTS else None

    def compacting_secs(self, sid, cwd, mt):
        """Seconds a compaction has been running, or None.

        The transcript is SILENT during a compaction: the whole block (the
        /compact command rows AND the boundary) is flushed only when it
        finishes, so 'issued but no boundary yet' is undetectable there. The
        PreCompact hook's checkpoint file is the one live artifact — its mtime
        is the compaction's start. Sessions whose project has no PreCompact
        hook simply never show the pill (the finished-event row still lands)."""
        p = os.path.join(HOME, ".claude", "compaction",
                         os.path.basename(cwd_to_project_dir(cwd)), f"checkpoint-{sid}.md")
        try:
            started = os.path.getmtime(p)
        except OSError:
            return None
        if started <= mt.last_compact_ep:       # that compaction already landed
            return None
        elapsed = time.time() - started
        if elapsed > 900:                       # stale checkpoint, not a live run
            return None
        return round(elapsed)

    def commands(self, sid):
        """Slash-command catalog for one session: built-ins + skills + custom
        commands, user- and project-scoped (the session's own cwd)."""
        if str(sid).startswith("codex:"):
            return self.codex_commands(sid)
        reg = next((r for r in self.live_sessions() if r.get("sessionId") == sid), None)
        cwd = reg.get("cwd", "") if reg else ""
        out, seen = [], set()

        def add(name, desc, scope):
            if name in seen:
                return
            seen.add(name)
            out.append({"name": name, "desc": (desc or "")[:120], "scope": scope,
                        "danger": name.lstrip("/").split(":")[-1] in DANGER_COMMANDS})

        def desc_of(path):
            try:
                with open(path, errors="replace") as f:
                    head = f.read(2500)
            except OSError:
                return ""
            m = re.search(r"^description:\s*(.+)$", head, re.M)
            if m:
                return m.group(1).strip().strip("'\"")
            body = re.sub(r"^---.*?^---", "", head, flags=re.S | re.M).strip()
            return body.split("\n")[0].lstrip("# ").strip()

        def scan_dir(root, scope, prefix=""):
            for p in sorted(glob.glob(os.path.join(root, "commands", "**", "*.md"),
                                      recursive=True)):
                rel = os.path.relpath(p, os.path.join(root, "commands"))
                add("/" + prefix + rel[:-3].replace(os.sep, ":"), desc_of(p), scope)
            for p in sorted(glob.glob(os.path.join(root, "skills", "*", "SKILL.md"))):
                add("/" + prefix + os.path.basename(os.path.dirname(p)), desc_of(p), scope)

        for name, desc in BUILTIN_COMMANDS:
            add("/" + name, desc, "built-in")
        if cwd:
            scan_dir(os.path.join(cwd, ".claude"), "project")
        scan_dir(os.path.join(HOME, ".claude"), "user")
        try:
            with open(os.path.join(HOME, ".claude", "plugins",
                                   "installed_plugins.json")) as f:
                plugins = json.load(f).get("plugins") or {}
        except Exception:
            plugins = {}
        for key, installs in plugins.items():
            plug = key.split("@")[0]
            for inst in installs or []:
                p = inst.get("installPath")
                if p and os.path.isdir(p):
                    scan_dir(p, "plugin", prefix=plug + ":")
        return {"ok": True, "commands": out}

    def codex_commands(self, sid):
        session = next((s for s in self.snapshot_cache.get("sessions", [])
                        if s.get("session_id") == sid), {})
        if not session:
            return {"ok": False, "error": "Codex session is unavailable"}
        return self.codex.commands(sid, session.get("cwd", ""))

    def session_context(self, sid):
        """Recent conversation turns + SendUserFile deliveries for one session."""
        if str(sid).startswith("codex:"):
            return self.codex.context(sid)
        reg, path = self._reg_main_path(sid)
        if not reg:
            return {"ok": False, "error": "session not live"}
        if not os.path.isfile(path):
            return {"ok": True, "messages": [], "files": [], "starting": True}
        snapshot = self._claude_context_snapshots.get(sid)
        if snapshot is not None:
            msgs = copy.deepcopy(snapshot.get("messages") or [])
            files = copy.deepcopy(snapshot.get("files") or [])
        else:
            # Startup/test fallback before the first completed scan publishes
            # this session. Stateful folding remains serialized.
            with self.scan_lock:
                mt = self.tail_for(path)
                mt.poll()
                msgs = [dict(m) for m in mt.convo]
                files = [dict(f) for f in mt.files]
        def fmeta(p):
            return {"name": os.path.basename(p), "path": p,
                    "kind": "image" if os.path.splitext(p)[1].lower() in IMG_EXTS else "text",
                    "missing": not os.path.isfile(p)}
        for m in msgs:                  # enrich inline delivery entries for the client
            if m.get("role") == "tool" and m.get("files"):
                m["files"] = [fmeta(p) for p in m["files"]]
        out_files = []
        for f in reversed(files):       # newest delivery first
            out_files.append({**fmeta(f["path"]), "caption": f["caption"], "ts": f["ts"]})
        return {"ok": True, "messages": msgs, "files": out_files}

    def _agent_paths(self, sid, aid):
        """Resolve a subagent transcript. aid is client-supplied — hard-whitelist
        its shape and keep it a basename, or it becomes a path-traversal read."""
        if not re.fullmatch(r"agent-[A-Za-z0-9_-]{1,64}", str(aid or "")):
            return None, None
        reg, path = self._reg_main_path(sid)
        if not reg:
            return None, None
        subdir = os.path.join(cwd_to_project_dir(reg.get("cwd", "")), sid, "subagents")
        jl = os.path.join(subdir, aid + ".jsonl")
        if not os.path.isfile(jl):
            return None, None
        return jl, os.path.join(subdir, aid + ".meta.json")

    def agent_context(self, sid, aid):
        """Conversation + info for ONE subagent (same fold as a session)."""
        with self.lock:
            parent = next((dict(item) for item in
                           self.snapshot_cache.get("sessions") or []
                           if item.get("session_id") == sid), None)
        if str(sid).startswith("codex:"):
            result = self.codex.agent_context(sid, aid)
            if result.get("ok") and parent:
                agent = next((dict(item) for item in parent.get("agents") or []
                              if item.get("agent_id") == aid), {})
                info = result.setdefault("info", {})
                for key in ("agent_type", "description", "model", "family", "effort",
                            "state", "cost", "cost_source"):
                    if agent.get(key) is not None and not info.get(key):
                        info[key] = agent[key]
                info["state"] = agent.get("state") or info.get("state")
                info["status_line"] = self.agent_status_line(parent, info)
            return result
        jl, meta_path = self._agent_paths(sid, aid)
        if not jl:
            return {"ok": False, "error": "no such subagent"}
        try:
            with open(meta_path) as handle:
                meta = json.load(handle)
        except Exception:
            meta = {}
        agent = next((dict(item) for item in (parent or {}).get("agents") or []
                      if item.get("agent_id") == aid), {})
        snapshot = self._claude_agent_context_snapshots.get((sid, aid))
        if snapshot is not None:
            info = {**agent,
                    "agent_id": aid,
                    "agent_type": agent.get("agent_type") or meta.get("agentType", "?"),
                    "description": (agent.get("description") or
                                    meta.get("description", "")),
                    "depth": agent.get("depth", meta.get("spawnDepth", 0)),
                    "ctx_tokens": snapshot.get("context_tokens")}
            if parent:
                info["status_line"] = self.agent_status_line(
                    parent, info, metrics=snapshot.get("status_metrics") or {})
            return {"ok": True,
                    "messages": copy.deepcopy(snapshot.get("messages") or []),
                    "info": info}
        with self.scan_lock:
            t = self.tail_for(jl)
            t.poll()
            msgs = [dict(m) for m in t.convo]
            fam = model_family(t.model)
            reg = next((r for r in self.live_sessions()
                        if r.get("sessionId") == sid), None) or {}
            info = {"agent_id": aid, "agent_type": meta.get("agentType", "?"),
                    "description": meta.get("description", ""),
                    "depth": meta.get("spawnDepth", 0),
                    "model": t.model, "family": fam,
                    "effort": self.agent_effort(meta.get("agentType"), reg.get("cwd", ""),
                                                self.effort_for(sid)),
                    "tokens": {"in": t.ti, "cache_write": t.tw,
                               "cache_read": t.tr, "out": t.to},
                    "total_tokens": t.total_tokens, "cost": round(t.cost(self.cfg), 4),
                    "started": t.first_ts, "last": t.last_ts}
            info["state"] = agent.get("state")
            if parent:
                info["status_line"] = self.agent_status_line(parent, info, t)
        return {"ok": True, "messages": msgs, "info": info}

    def file_content(self, sid, fpath):
        """Serve a delivered file. WHITELIST: only paths recorded from this session's
        own SendUserFile tool_use rows — never a free-form client path."""
        if str(sid).startswith("codex:"):
            return self.codex.file_content(sid, fpath)
        reg, path = self._reg_main_path(sid)
        if not reg:
            return None, None, "session not live"
        snapshot = self._claude_context_snapshots.get(sid)
        if snapshot is not None:
            files = snapshot.get("files") or []
            messages = snapshot.get("messages") or []
        else:
            with self.scan_lock:
                mt = self.tail_for(path)
                mt.poll()
                files = list(mt.files)
                messages = list(mt.convo)
        allowed = {f["path"] for f in files}
        for m in messages:              # inline chips can outlive the files deque
            if m.get("role") == "tool":
                allowed.update(p for p in m.get("files") or [] if isinstance(p, str))
        if fpath not in allowed:
            return None, None, "not a file this session delivered"
        try:
            if os.path.getsize(fpath) > 8_000_000:
                return None, None, "file too large to preview (>8MB)"
            with open(fpath, "rb") as f:
                data = f.read()
        except OSError as e:
            return None, None, f"unreadable: {e}"
        ext = os.path.splitext(fpath)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml"}.get(ext, "text/plain; charset=utf-8")
        return ctype, data, None

    # ------------------------------------------------------------ injection
    @staticmethod
    def _bounded_process(argv, timeout=8, max_output=524_288):
        """Run fixed argv while bounding combined stdout/stderr in memory."""
        try:
            process = subprocess.Popen(list(argv), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except (FileNotFoundError, OSError) as exc:
            return {"ok": False, "code": None, "stdout": "", "stderr": str(exc),
                    "truncated": False}
        selector = selectors.DefaultSelector()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        total = 0
        truncated = timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    process.kill()
                    break
                events = selector.select(min(0.1, remaining))
                if not events and process.poll() is not None:
                    events = [(key, selectors.EVENT_READ)
                              for key in list(selector.get_map().values())]
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max_output - total
                    if len(chunk) > available:
                        buffers[key.data].extend(chunk[:max(0, available)])
                        total = max_output
                        truncated = True
                        process.kill()
                        break
                    buffers[key.data].extend(chunk)
                    total += len(chunk)
                if truncated:
                    break
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        stderr = buffers["stderr"].decode("utf-8", "replace")
        if timed_out:
            stderr = (stderr + "\nGit probe timed out").strip()
        if truncated:
            stderr = (stderr + "\nGit probe exceeded its output limit").strip()
        return {"ok": process.returncode == 0 and not timed_out and not truncated,
                "code": process.returncode,
                "stdout": buffers["stdout"].decode("utf-8", "replace"),
                "stderr": stderr, "truncated": truncated, "timeout": timed_out}

    @staticmethod
    def _bounded_nul_paths(argv, timeout=8, max_input=67_108_864, keep=40):
        """Stream a large NUL path list into a bounded sample, count, and digest."""
        try:
            process = subprocess.Popen(list(argv), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except (FileNotFoundError, OSError) as exc:
            return {"ok": False, "code": None, "paths": [], "count": 0,
                    "digest": "", "stderr": str(exc), "truncated": False}
        selector = selectors.DefaultSelector()
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        digest = hashlib.sha256()
        carry = bytearray()
        stderr_buffer = bytearray()
        paths = []
        count = total = 0
        truncated = timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    process.kill()
                    break
                events = selector.select(min(0.1, remaining))
                if not events and process.poll() is not None:
                    events = [(key, selectors.EVENT_READ)
                              for key in list(selector.get_map().values())]
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_input:
                        truncated = True
                        process.kill()
                        break
                    if key.data == "stderr":
                        if len(stderr_buffer) < 65_536:
                            stderr_buffer.extend(chunk[:65_536 - len(stderr_buffer)])
                        continue
                    digest.update(chunk)
                    carry.extend(chunk)
                    records = carry.split(b"\0")
                    carry = bytearray(records.pop())
                    if len(carry) > 16_384:
                        truncated = True
                        process.kill()
                        break
                    for record in records:
                        if not record:
                            continue
                        count += 1
                        if len(paths) < keep:
                            paths.append(record.decode("utf-8", "replace"))
                if truncated:
                    break
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        stderr = stderr_buffer.decode("utf-8", "replace")
        if timed_out:
            stderr = (stderr + "\nGit probe timed out").strip()
        if truncated:
            stderr = (stderr + "\nGit path probe exceeded its scan limit").strip()
        return {"ok": process.returncode == 0 and not timed_out and not truncated,
                "code": process.returncode, "paths": paths, "count": count,
                "digest": digest.hexdigest(), "stderr": stderr,
                "truncated": truncated, "timeout": timed_out}

    @staticmethod
    def _parse_worktree_list(raw):
        entries = []
        current = None
        for record in raw.split("\0"):
            if not record:
                if current:
                    entries.append(current)
                    current = None
                continue
            if record.startswith("worktree "):
                if current:
                    entries.append(current)
                current = {"path": os.path.realpath(record[9:]), "locked": False,
                           "prunable": False}
                continue
            if current is None:
                continue
            if record.startswith("HEAD "):
                current["head"] = record[5:]
            elif record.startswith("branch "):
                current["branch"] = record[7:]
            elif record == "detached":
                current["detached"] = True
            elif record.startswith("locked"):
                current["locked"] = True
                current["lock_reason"] = record[6:].strip()
            elif record.startswith("prunable"):
                current["prunable"] = True
                current["prune_reason"] = record[8:].strip()
        if current:
            entries.append(current)
        return entries

    def _sessions_using_worktree(self, worktree, exclude=()):
        target = os.path.realpath(worktree)
        excluded = {str(value) for value in exclude}
        with self.lock:
            records = copy.deepcopy(self.snapshot_cache.get("sessions") or [])
        seen = {str(item.get("session_id") or "") for item in records}
        for reg in self.live_sessions():
            sid = str(reg.get("sessionId") or "")
            if sid not in seen:
                records.append({"session_id": sid, "provider": "claude",
                                "title": reg.get("name"), "cwd": reg.get("cwd")})
        users = []
        for item in records:
            sid = str(item.get("session_id") or item.get("sessionId") or "")
            if not sid or sid in excluded or not item.get("cwd"):
                continue
            identity = self.workstream_identity(item["cwd"])
            if identity.get("kind") == "git" and \
               os.path.realpath(identity.get("worktree") or "") == target:
                users.append({"session_id": sid, "provider": item.get("provider") or "claude",
                              "title": item.get("title") or item.get("name") or sid})
        return users

    @staticmethod
    def _owned_claude_worktree_lock(session, registered):
        """Whether Claude itself locked this session's generated worktree."""
        if str(session.get("provider") or "claude") != "claude":
            return False
        try:
            pid = int(session.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        reason = str(registered.get("lock_reason") or "")
        return pid > 1 and bool(re.fullmatch(
            rf"claude session .+ \(pid {pid} start .+\)", reason))

    def close_worktree_preview(self, session, issue_ticket=True):
        """Describe optional cleanup without trusting a client path."""
        sid = str(session.get("session_id") or session.get("sessionId") or "")
        provider = str(session.get("provider") or "claude")
        identity = self.workstream_identity(session.get("cwd"))
        base = {"ok": True, "session_id": sid, "provider": provider,
                "secondary_worktree": False, "remove_allowed": False,
                "force_remove_allowed": False, "dirty": False,
                "dirty_counts": {}, "dirty_files": [], "ignored_count": 0,
                "ignored_files": [], "shared_sessions": []}
        if identity.get("kind") != "git":
            return {**base, "reason": "This session is not in a Git worktree."}
        root = os.path.realpath(identity.get("root") or "")
        worktree = os.path.realpath(identity.get("worktree") or "")
        if not root or not worktree or root == worktree:
            return {**base, "root": root, "worktree": worktree,
                    "reason": "The primary worktree is never removable from Fleet."}
        base.update(secondary_worktree=True, root=root, worktree=worktree)
        listing = self._bounded_process(
            ["git", "-C", root, "worktree", "list", "--porcelain", "-z"],
            timeout=5, max_output=262_144)
        if not listing["ok"]:
            return {**base, "inspect_ok": False,
                    "reason": (listing["stderr"] or "Git worktree registration is unavailable")[:500]}
        entries = self._parse_worktree_list(listing["stdout"])
        registered = next((item for item in entries if item["path"] == worktree), None)
        primary = entries[0]["path"] if entries else None
        if not registered or primary == worktree or registered.get("prunable"):
            return {**base, "inspect_ok": False, "registered": bool(registered),
                    "reason": "The linked worktree registration is stale or unsafe."}
        owned_lock = self._owned_claude_worktree_lock(session, registered)
        if registered.get("locked") and not owned_lock:
            return {**base, "inspect_ok": False, "registered": True, "locked": True,
                    "reason": "This worktree is locked by Git and cannot be removed from Fleet."}

        status_result = self._bounded_process(
            ["git", "-C", worktree, "status", "--porcelain=v2", "--branch", "-z",
             "--untracked-files=all"], timeout=8, max_output=1_048_576)
        ignored_result = self._bounded_nul_paths(
            ["git", "-C", worktree, "ls-files", "--others", "--ignored",
             "--exclude-standard", "-z"], timeout=8, max_input=67_108_864, keep=40)
        if not status_result["ok"] or not ignored_result["ok"]:
            detail = status_result["stderr"] or ignored_result["stderr"] or \
                "Git could not completely inspect the worktree"
            return {**base, "inspect_ok": False, "registered": True,
                    "reason": detail[:500]}

        status = RepositoryOutcomeCenter._parse_status(status_result["stdout"])
        files = status.get("files") or []
        ignored = ignored_result["paths"]
        ignored_count = ignored_result["count"]
        categories = {
            "staged": [item for item in files if item.get("staged")],
            "unstaged": [item for item in files if item.get("unstaged") and
                          not item.get("untracked")],
            "untracked": [item for item in files if item.get("untracked")],
            "conflicts": [item for item in files if item.get("conflict")],
        }
        dirty_counts = {key: len(value) for key, value in categories.items()}
        dirty_files = []
        seen_paths = set()
        for category in ("conflicts", "staged", "unstaged", "untracked"):
            for item in categories[category]:
                path = item.get("path")
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                dirty_files.append({**item, "category": category})
        shared = self._sessions_using_worktree(worktree, exclude=(sid,))
        dirty = bool(files)
        destructive_contents = dirty or bool(ignored)
        material = (root + "\0" + worktree + "\0" + listing["stdout"] + "\0" +
                    status_result["stdout"] + "\0" + ignored_result["digest"] + "\0" +
                    json.dumps(shared, sort_keys=True, separators=(",", ":")))
        revision = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        result = {**base, "inspect_ok": True, "registered": True,
                  "locked": bool(registered.get("locked")),
                  "owned_lock": owned_lock,
                  "branch": status.get("branch"), "head_oid": status.get("head_oid"),
                  "revision": revision, "dirty": dirty, "dirty_counts": dirty_counts,
                  "dirty_total": len(dirty_files), "dirty_files": dirty_files[:100],
                  "dirty_files_truncated": len(dirty_files) > 100,
                  "ignored_count": ignored_count, "ignored_files": ignored,
                  "ignored_files_truncated": ignored_count > len(ignored),
                  "shared_sessions": shared,
                  "remove_allowed": not destructive_contents and not shared,
                  "force_remove_allowed": destructive_contents and not shared}
        if shared:
            result["reason"] = "Another live Fleet session is using this worktree."
        elif destructive_contents:
            result["reason"] = "The worktree contains files that removal would erase."
        elif owned_lock:
            result["reason"] = "Claude's worktree lock will be released after the session closes."
        if issue_ticket:
            token = secrets.token_urlsafe(24)
            ticket = {"session_id": sid, "provider": provider, "root": root,
                      "worktree": worktree, "revision": revision,
                      "pid": session.get("pid"), "expires": time.time() + 300,
                      "closed_at": None}
            with self._cleanup_lock:
                now = time.time()
                self._cleanup_tickets = {key: value for key, value in
                    self._cleanup_tickets.items() if value.get("expires", 0) > now}
                self._cleanup_tickets[token] = ticket
            result["cleanup_ticket"] = token
        return result

    def _mark_cleanup_ticket_closed(self, token, sid):
        if not token:
            return
        with self._cleanup_lock:
            ticket = self._cleanup_tickets.get(str(token))
            if ticket and ticket.get("session_id") == str(sid) and \
               ticket.get("expires", 0) > time.time():
                ticket["closed_at"] = time.time()

    def _cleanup_ticket_matches(self, token, sid):
        with self._cleanup_lock:
            ticket = self._cleanup_tickets.get(str(token or ""))
            return bool(ticket and ticket.get("session_id") == str(sid) and
                        ticket.get("expires", 0) > time.time())

    def cleanup_closed_worktree(self, action):
        token = str(action.get("cleanup_ticket") or "")
        with self._cleanup_lock:
            ticket = self._cleanup_tickets.pop(token, None)
        if not ticket or ticket.get("expires", 0) <= time.time():
            return {"ok": False, "error": "cleanup preview expired — the worktree was preserved"}
        if not ticket.get("closed_at"):
            return {"ok": False, "error": "the session did not close — the worktree was preserved"}
        if str(action.get("session_id") or "") != ticket["session_id"]:
            return {"ok": False, "error": "cleanup ticket does not match this session"}
        if ticket.get("provider") == "claude" and ticket.get("pid"):
            command = ""
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                try:
                    command = subprocess.run(
                        ["ps", "-p", str(ticket["pid"]), "-o", "command="],
                        capture_output=True, text=True, timeout=2).stdout.strip()
                except Exception:
                    command = "unknown"
                if not command:
                    break
                time.sleep(0.05)
            if command:
                return {"ok": False, "error": "the Claude process is still closing — "
                        "the worktree was preserved", "worktree": ticket["worktree"],
                        "preserved": True}
        force = action.get("force") is True
        session = {"session_id": ticket["session_id"], "provider": ticket["provider"],
                   "cwd": ticket["worktree"], "pid": ticket.get("pid")}
        preview = self.close_worktree_preview(session, issue_ticket=False)
        if not preview.get("inspect_ok") or preview.get("revision") != ticket["revision"]:
            return {"ok": False, "error": "the worktree changed after preview — it was preserved",
                    "worktree": ticket["worktree"], "preserved": True}
        if preview.get("root") != ticket["root"] or preview.get("worktree") != ticket["worktree"]:
            return {"ok": False, "error": "worktree identity changed — it was preserved",
                    "worktree": ticket["worktree"], "preserved": True}
        allowed = preview.get("force_remove_allowed") if force else preview.get("remove_allowed")
        if not allowed:
            return {"ok": False, "error": preview.get("reason") or
                    "worktree removal is no longer safe", "worktree": ticket["worktree"],
                    "preserved": True}
        unlocked = False
        if preview.get("owned_lock"):
            unlock = self._bounded_process(
                ["git", "-C", ticket["root"], "worktree", "unlock", ticket["worktree"]],
                timeout=10, max_output=262_144)
            if not unlock["ok"]:
                return {"ok": False, "error": (unlock["stderr"] or unlock["stdout"] or
                        "Claude's worktree lock could not be released")[:500],
                        "worktree": ticket["worktree"], "preserved": True}
            unlocked = True
        argv = ["git", "-C", ticket["root"], "worktree", "remove"]
        if force:
            argv.append("--force")
        argv.append(ticket["worktree"])
        removed = self._bounded_process(argv, timeout=30, max_output=262_144)
        if not removed["ok"]:
            return {"ok": False, "error": (removed["stderr"] or removed["stdout"] or
                    "Git worktree removal failed")[:500], "worktree": ticket["worktree"],
                    "preserved": os.path.exists(ticket["worktree"]), "unlocked": unlocked}
        self._workstream_cache.clear()
        self._workstreams_snapshot_cache = None
        return {"ok": True, "removed": True, "forced": force,
                "worktree": ticket["worktree"], "branch_preserved": True}

    def _claude_process_command(self, reg):
        """Return one verified process command per live Claude PID.

        This is used only to discover whether Claude was started with a bypass-
        enabling flag. Cache it so the two-second fleet scan never gains a `ps`
        subprocess per session.
        """
        try:
            pid = int(reg.get("pid") or 0)
        except (TypeError, ValueError):
            return ""
        if pid <= 1:
            return ""
        if pid in self._claude_command_cache:
            return self._claude_command_cache[pid]
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                text=True, timeout=5).stdout.strip()
        except Exception:
            command = ""
        self._claude_command_cache[pid] = command
        return command

    @staticmethod
    def _claude_auto_model(model):
        """Whether the observed concrete model can expose Claude Auto mode."""
        value = str(model or "").lower().replace(".", "-")
        return bool(re.search(
            r"(?:sonnet-(?:4-6|5)|opus-(?:4-[678]|[5-9])|fable-5)", value))

    def _claude_permission_modes(self, reg, tail):
        """Native Shift-Tab cycle for this process, in its documented order."""
        modes = ["default", "acceptEdits", "plan"]
        command = self._claude_process_command(reg)
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        bypass = any(item in ("--allow-dangerously-skip-permissions",
                              "--dangerously-skip-permissions") for item in argv)
        for index, item in enumerate(argv[:-1]):
            if item == "--permission-mode" and argv[index + 1] == "bypassPermissions":
                bypass = True
        bypass = bypass or "--permission-mode=bypassPermissions" in argv \
            or tail.permission_mode == "bypassPermissions"
        if bypass:
            modes.append("bypassPermissions")
        if self._claude_auto_model(tail.model) or tail.permission_mode == "auto":
            modes.append("auto")
        return modes

    def _close_claude_session(self, reg):
        """Terminate only the registered Claude process; never close its terminal tab."""
        try:
            pid = int(reg.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 1 or pid == os.getpid():
            return {"ok": False, "error": "refusing to terminate an invalid Claude pid"}

        # Session registry entries can outlive a crashed process. Verify the PID was
        # not reused before signalling it; a cwd containing `.claude` is deliberately
        # insufficient evidence.
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                text=True, timeout=5).stdout.strip()
        except Exception as exc:
            return {"ok": False, "error": f"process lookup failed: {exc}"}
        if not command:
            return {"ok": False, "error": "Claude process is no longer running"}
        if not re.search(r"(^|[/\s])claude(?:-code)?(?:[/\s]|$)", command, re.I):
            return {"ok": False, "error": "refusing to terminate a non-Claude process"}

        interrupted = False
        interrupt_error = None
        if reg.get("status") in ("busy", "shell", "waiting"):
            tty = self._tty_cache.get(pid)
            if not tty:
                try:
                    tty = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "tty="], capture_output=True,
                        text=True, timeout=5).stdout.strip()
                except Exception as exc:
                    interrupt_error = f"tty lookup failed: {exc}"
                if tty and tty != "??":
                    self._tty_cache[pid] = tty
            if tty and tty != "??":
                result = self._iterm_write(f"/dev/{tty}", [("\x1b", False)],
                                           step_delay=0.05)
                interrupted = bool(result.get("ok"))
                if not interrupted:
                    interrupt_error = result.get("error") or "interrupt failed"
                else:
                    time.sleep(0.15)

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as exc:
            return {"ok": False, "error": f"could not terminate Claude: {exc}"}
        self._tty_cache.pop(pid, None)
        self._claude_command_cache.pop(pid, None)
        result = {"ok": True, "closed": True, "interrupted": interrupted}
        if interrupt_error:
            result["warning"] = interrupt_error
        return result

    def reopen_claude_session(self, sid):
        """Open a saved Claude transcript in a new iTerm tab.

        Both the UUID and transcript path come from the ledger, but are validated
        again here because this action crosses the local file/terminal boundary.
        """
        if any(row.get("sessionId") == sid for row in self.live_sessions()):
            return {"ok": False, "error": "session is already live"}
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute(
                "SELECT cwd, provider, transcript_path, closed_at FROM session_runs "
                "WHERE session_id=?", (sid,)).fetchone()
        except Exception as exc:
            return {"ok": False, "error": f"session lookup failed: {exc}"}
        finally:
            if db is not None:
                db.close()
        if not row or row[1] != "claude" or row[3] is None:
            return {"ok": False, "error": "session is not a closed Claude conversation"}
        if not self._safe_claude_transcript(sid, row[2]):
            return {"ok": False, "error": "saved Claude transcript is unavailable"}
        cwd = self._safe_reopen_cwd(row[0])
        if not cwd:
            return {"ok": False,
                    "error": "the session working directory no longer exists or is outside home"}
        command = (f"cd {shlex.quote(cwd)} && claude --resume "
                   f"{shlex.quote(str(sid))}")
        result = self._iterm_write("SPAWN", [(command, False)])
        if result.get("ok"):
            result.update(reopened=True, session_id=sid, cwd=cwd, command=command)
        return result

    def outbox_usage_options(self):
        """Return stable, display-safe account/window choices for reset triggers."""
        with self.lock:
            providers = copy.deepcopy(self.snapshot_cache.get("provider_usage") or {})
        options = []
        claude = providers.get("claude") or {}
        for profile in claude.get("profiles") or ([claude] if claude else []):
            account_id = str(profile.get("id") or profile.get("email") or "active")
            label = profile.get("email") or profile.get("name") or "Active Claude account"
            windows = []
            for window_id, name, field in (
                    ("five_hour", "5-hour", "five_hour_reset"),
                    ("weekly", "Weekly", "weekly_reset")):
                if profile.get(field):
                    windows.append({"id": window_id, "label": name,
                                    "reset": profile.get(field)})
            if windows:
                options.append({"provider": "claude", "account_id": account_id,
                                "label": label, "windows": windows})
        codex = providers.get("codex") or {}
        buckets = [{"id": str(item.get("id")), "label": item.get("label") or item.get("id"),
                    "reset": item.get("reset")} for item in codex.get("buckets") or []
                   if item.get("id") and item.get("reset")]
        if buckets:
            options.append({"provider": "codex",
                "account_id": str(codex.get("account_id") or codex.get("email") or "active"),
                "label": codex.get("email") or "Active Codex account", "windows": buckets})
        return options

    def outbox_snapshot(self, state=None, cursor=0, limit=100):
        try:
            result = self.outbox.list(state=state, cursor=cursor, limit=limit)
            result["summary"] = self.outbox.counts()
            result["usage_options"] = self.outbox_usage_options()
            return result
        except OutboxError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:
            return {"ok": False, "error": f"outbox is temporarily unavailable: {exc}"}

    def briefing_snapshot(self, device_id="default", cursor=None, limit=100):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        try:
            return self.operations.briefing_snapshot(
                snapshot, device_id=device_id or "default", cursor=cursor, limit=limit)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"briefing is temporarily unavailable: {exc}"}

    def notifications_snapshot(self, device_id="default", cursor=None, limit=100,
                               states=None, kinds=None, event_id=None):
        try:
            return self.operations.notification_snapshot(
                device_id=device_id or "default", cursor=cursor, limit=limit,
                states=states, kinds=kinds, event_id=event_id)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"notifications are temporarily unavailable: {exc}"}

    def push_config(self, device_id=None):
        try:
            devices = self.operations.notification_devices_snapshot(device_id)
            with self.web_push_lock:
                runtime = self.web_push.status() if self.web_push else {
                    "configured": False, "public_key": None, "delivery": "starting",
                    "helper": {"ready": False, "restarts": 0, "state": "starting"},
                    "queue": self.operations.notification_delivery_diagnostics()}
            return {"ok": True, "feature": "production",
                    "configured": bool(runtime.get("configured")),
                    "public_key": runtime.get("public_key"),
                    "delivery": runtime.get("delivery") or "unavailable",
                    "helper": runtime.get("helper"), "queue": runtime.get("queue"),
                    "current_device": devices.get("current_device"),
                    "registered_devices": devices.get("registered", 0),
                    "enabled_devices": devices.get("enabled", 0)}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push configuration is temporarily unavailable"}

    def push_devices(self, current_device_id=None):
        try:
            return self.operations.notification_devices_snapshot(current_device_id)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push devices are temporarily unavailable"}

    def push_diagnostics(self):
        with self.web_push_lock:
            runtime = self.web_push.status() if self.web_push else {
                "configured": False, "delivery": "starting",
                "helper": {"ready": False, "restarts": 0, "state": "starting"},
                "queue": self.operations.notification_delivery_diagnostics()}
        return {key: runtime.get(key) for key in
                ("configured", "delivery", "helper", "runtime", "queue")}

    def legacy_ntfy_diagnostics(self):
        out = self.operations.legacy_notification_diagnostics()
        return {"enabled": self.cfg.get("legacy_ntfy_enabled") is True,
                "configured": bool(self.cfg.get("ntfy_topic") and
                                   self.cfg.get("ntfy_server")), **out}

    def push_subscription(self, payload):
        try:
            if payload.get("remove"):
                device = self.operations.notification_remove_device(
                    payload.get("device_id"), payload.get("permission_state") or "expired")
            else:
                device = self.operations.notification_register_device(
                    payload.get("device_id"), payload.get("display_name"),
                    payload.get("platform"), payload.get("subscription"),
                    permission_state=payload.get("permission_state") or "granted",
                    preferences=payload.get("preferences"),
                    allowed_origins=self.cfg.get("web_push_allowed_origins") or [])
            return {"ok": True, "device": device}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push subscription could not be saved"}

    def push_device_settings(self, payload):
        try:
            device = self.operations.notification_update_device(
                payload.get("device_id"),
                display_name=payload.get("display_name") if "display_name" in payload else None,
                enabled=payload.get("enabled") if "enabled" in payload else None,
                preferences=payload.get("preferences") if "preferences" in payload else None)
            return {"ok": True, "device": device}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push device settings could not be saved"}

    def notifications_mark_read(self, payload):
        try:
            cursor = self.operations.notification_mark_read(
                payload.get("device_id"), payload.get("cursor"))
            return {"ok": True, "cursor": cursor}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification read state could not be saved"}

    def notifications_snooze(self, payload):
        try:
            until = self.operations.notification_snooze(
                payload.get("event_id"), payload.get("source_revision"), payload.get("until"))
            return {"ok": True, "until": until}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification could not be snoozed"}

    def notifications_wake(self, payload):
        try:
            self.operations.notification_wake(
                payload.get("event_id"), payload.get("source_revision"))
            return {"ok": True}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification could not be woken"}

    def notifications_mute(self, payload):
        try:
            event = self.operations.notification_snapshot(
                payload.get("device_id") or "default", event_id=payload.get("event_id"))
            item = (event.get("events") or [None])[0]
            if (not item or item.get("source_revision") != payload.get("source_revision") or
                    not item.get("session_id")):
                raise OperationsError("notification event is stale")
            muted = payload.get("muted")
            if not isinstance(muted, bool):
                raise OperationsError("invalid notification mute state")
            saved = self.update_settings({"mute_session": item["session_id"], "muted": muted})
            if not saved.get("ok"):
                raise OperationsError(saved.get("error") or "notification mute failed")
            self.operations.notification_set_session_mute(
                item["session_id"], item.get("provider"), muted)
            return {"ok": True, "muted": muted, "session_id": item["session_id"]}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification mute could not be saved"}

    def notifications_retry(self, payload):
        try:
            delivery = self.operations.notification_retry_delivery(payload.get("delivery_id"))
            with self.web_push_lock:
                if self.web_push:
                    self.web_push.wake_event.set()
            return {"ok": True, "delivery": delivery}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification delivery could not be retried"}

    def start_web_push(self):
        """Start the isolated delivery runtime without delaying daemon availability."""
        with self.web_push_lock:
            if self.web_push is None:
                self.web_push = WebPushService(self.operations, BASE, self.cfg)
            self.web_push.start()

    def push_capability_action(self, payload):
        """Apply one signed reversible push action without using the act token."""
        token = payload.get("capability") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            return {"ok": False, "error": "notification capability is unavailable"}

        def persist_mute(session_id):
            values = dict(self.cfg.get("muted_sessions") or {})
            values.pop(session_id, None)
            values[session_id] = time.time()
            values = dict(list(values.items())[-1000:])
            self._persist_config_fields({"muted_sessions": values})
            self.cfg["muted_sessions"] = values

        try:
            with self.web_push_lock:
                service = self.web_push
            if not service:
                raise OperationsError("notification capability is unavailable")
            with self.config_lock:
                result = service.capability_action(token, mute_callback=persist_mute)
            return {"ok": True, **result}
        except Exception:
            return {"ok": False, "error": "notification capability is unavailable"}

    def push_test(self, payload):
        try:
            with self.web_push_lock:
                service = self.web_push
            if not service or not service.status().get("configured"):
                return {"ok": False, "error": "Web Push delivery is not ready",
                        "code": "delivery_unavailable"}
            delivery = service.enqueue_test(payload.get("device_id"))
            return {"ok": True, "delivery": delivery}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "test delivery could not be queued"}

    def budgets_snapshot(self, spawn=None):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        try:
            prepared = dict(spawn or {})
            if prepared.get("cwd"):
                prepared["workstream_id"] = self.workstream_identity(
                    prepared["cwd"]).get("workstream_id")
            return self.operations.budgets_snapshot(snapshot, spawn=prepared or None)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"budgets are temporarily unavailable: {exc}"}

    def briefing_action(self, action):
        try:
            cursor = self.operations.review(action.get("device_id"), action.get("cursor"))
            return {"ok": True, "cursor": cursor}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"briefing review failed: {exc}"}

    def _outbox_current_target(self, payload):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        record = {
            "target_provider": payload.get("target_provider"),
            "target_session_id": payload.get("target_session_id"),
            "target_agent_id": payload.get("target_agent_id"),
            "destination_session_id": payload.get("destination_session_id"),
            "updated_at": time.time() - 300,
        }
        return self.outbox._target_status(record, snapshot)

    def _validate_outbox_spawn(self, spec):
        if not isinstance(spec, dict):
            raise OutboxError("new-session settings are required")
        provider = str(spec.get("provider") or "")
        if provider not in ("claude", "codex"):
            raise OutboxError("unknown provider")
        cwd = os.path.realpath(os.path.expanduser(str(spec.get("cwd") or "").strip()))
        home = os.path.realpath(HOME)
        if not cwd or not os.path.isdir(cwd):
            raise OutboxError("no such directory")
        if cwd != home and not cwd.startswith(home + os.sep):
            raise OutboxError("directory must be under your home folder")
        model = str(spec.get("model") or "").strip()
        effort = str(spec.get("effort") or "").strip()
        if provider == "claude":
            if model and model not in self.MODELS:
                raise OutboxError("unknown Claude model")
            if effort and effort not in self.EFFORTS:
                raise OutboxError("unknown effort level")
            permission_mode = str(spec.get("permission_mode") or "default")
            if permission_mode not in self.CLAUDE_START_PERMISSION_MODES:
                raise OutboxError("unknown Claude permission mode")
        else:
            permission_mode = ""
            catalog = {item.get("id"): item for item in self.codex.models}
            if model and model not in catalog:
                raise OutboxError("unknown Codex model")
            allowed = (catalog.get(model) or {}).get("efforts") or []
            if effort and allowed and effort not in allowed:
                raise OutboxError("unsupported Codex effort level")
            if str(spec.get("mode") or "plan") not in ("plan", "default"):
                raise OutboxError("Codex mode must be plan or default")
        name = str(spec.get("worktree_name") or "").strip()
        if name and not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
            raise OutboxError("worktree name: letters, digits, . _ - only")
        if spec.get("worktree") and not os.path.exists(os.path.join(cwd, ".git")):
            raise OutboxError("new worktree requires a Git repository")
        return {"provider": provider, "cwd": cwd, "model": model, "effort": effort,
                "mode": str(spec.get("mode") or "plan"),
                "permission_mode": permission_mode,
                "worktree": bool(spec.get("worktree")), "worktree_name": name}

    def _prepare_outbox_payload(self, payload):
        prepared = dict(payload or {})
        if prepared.get("kind") == "new_session" or prepared.get("spawn_spec"):
            prepared["spawn_spec"] = self._validate_outbox_spawn(prepared.get("spawn_spec"))
        elif prepared.get("target_session_id"):
            status, reason, _ = self._outbox_current_target(prepared)
            if status == "block":
                raise OutboxError(reason or "target is unavailable")
        if prepared.get("kind") == "usage_reset":
            options = self.outbox_usage_options()
            account = next((item for item in options
                if item["provider"] == prepared.get("target_provider") and
                   item["account_id"] == str(prepared.get("usage_account_id") or "")), None)
            window = next((item for item in (account or {}).get("windows", [])
                if item["id"] == str(prepared.get("usage_window_id") or "")), None)
            if not window:
                raise OutboxError("fresh evidence for that usage reset is unavailable")
            prepared["observed_reset_at"] = window["reset"]
        return prepared

    def outbox_action(self, action):
        typ = str(action.get("type") or "")
        outbox_id = str(action.get("outbox_id") or "")
        try:
            if typ == "outbox_create":
                item = self.outbox.create(self._prepare_outbox_payload(action))
            elif typ == "outbox_update":
                item = self.outbox.update(outbox_id,
                    self._prepare_outbox_payload(action.get("patch") or {}))
            elif typ == "outbox_cancel":
                item = self.outbox.cancel(outbox_id)
            elif typ == "outbox_send_now":
                item = self.outbox.send_now(outbox_id)
            elif typ == "outbox_retry":
                patch = action.get("patch") or {}
                item = self.outbox.retry(outbox_id,
                    self._prepare_outbox_payload(patch) if patch else None)
            elif typ == "outbox_retarget":
                item = self.outbox.retarget(outbox_id,
                    self._prepare_outbox_payload(action.get("patch") or {}))
            else:
                return {"ok": False, "error": "unknown outbox action"}
            return {"ok": True, "item": item, "summary": self.outbox.counts()}
        except OutboxError as exc:
            result = {"ok": False, "error": str(exc), "code": exc.code}
            if hasattr(exc, "choices"):
                result["choices"] = exc.choices
            return result
        except Exception as exc:
            return {"ok": False, "error": f"outbox action failed: {exc}"}

    def _outbox_dispatch(self, record):
        sid = record.get("destination_session_id") or record.get("target_session_id")
        action = {"type": "relay", "session_id": sid,
                  "agent_id": record.get("target_agent_id"),
                  "text": record.get("message")} if record.get("target_agent_id") else {
                  "type": "text", "session_id": sid, "text": record.get("message")}
        result = self.act(action)
        return {"ok": bool(result.get("ok")), "provider": record.get("target_provider"),
                "session_id": sid, "accepted": bool(result.get("ok")),
                "error": result.get("error")}

    def _outbox_spawn(self, record):
        spec = dict(record.get("spawn_spec") or {})
        provider = spec.get("provider")
        if provider == "codex":
            result = self.spawn_codex_session({**spec, "initial_text": record.get("message")})
            return {"ok": bool(result.get("ok")), "provider": "codex",
                    "session_id": result.get("session_id"),
                    "message_delivered": bool(result.get("ok")),
                    "accepted": bool(result.get("ok")), "error": result.get("error")}
        result = self.spawn_session(spec, reserved_sid=record.get("destination_session_id"))
        return {"ok": bool(result.get("ok")), "provider": "claude",
                "session_id": result.get("session_id"), "message_delivered": False,
                "accepted": bool(result.get("ok")), "error": result.get("error"),
                "trust_prompt": bool(result.get("trust_prompt"))}

    def run_outbox(self):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        usage = copy.deepcopy(snapshot.get("provider_usage") or {})
        self.outbox.tick(snapshot, usage, self._outbox_dispatch, self._outbox_spawn)

    def act(self, action):
        """Inject an answer into the owning iTerm session. action:
        {type:'option', session_id, nonce, digits:[1,..], n_options, other:'...'} |
        {type:'multiq', session_id, nonce,
         answers:[{digits:[..], multi:bool, n_options, other:'...'}, ..]} |
        {type:'dismiss', session_id, nonce}   (Esc = the TUI's "Chat about this") |
        {type:'permission', session_id, nonce, choice:'allow'|'always'|'deny'} |
        {type:'interrupt', session_id}        (Esc into a BUSY session: stop the turn) |
        {type:'close', session_id}            (stop if active, then SIGTERM Claude) |
        {type:'close_preview', session_id}    (read-only secondary-worktree safety probe) |
        {type:'worktree_cleanup', session_id, cleanup_ticket, force} |
        {type:'reopen', session_id}           (new terminal: claude --resume ID) |
        {type:'relay', session_id, agent_id, text}  (subagents have no tty: type a
                                              tagged line into the PARENT for it to
                                              forward with SendMessage) |
        {type:'text', session_id, text:'...'} |
        {type:'image_text', session_id, text:'...', upload_ids:['opaque-id']}"""
        if action.get("type") == "ping":     # token check for the page's acting banner
            return {"ok": True}
        if action.get("type") == "image_text":
            paths, error = self._resolve_image_uploads(
                str(action.get("session_id") or ""), action.get("upload_ids"))
            if error:
                return {"ok": False, "error": error}
            # Client-supplied paths are never accepted. Only this server-side
            # resolution can add image_paths to a provider action.
            action = {**action, "image_paths": paths}
        if action.get("type") == "briefing_review":
            return self.briefing_action(action)
        if str(action.get("type") or "").startswith("outbox_"):
            return self.outbox_action(action)
        if action.get("type") == "handoff":
            return self.execute_handoff(action)
        if action.get("type") in ("git_commit", "git_push", "pr_create_draft",
                                  "pr_mark_ready"):
            return self.repository_action(action)
        if action.get("type") == "worktree_cleanup":
            return self.cleanup_closed_worktree(action)
        if str(action.get("session_id") or "").startswith("codex:") \
           and action.get("type") == "focus":
            return self.attach_codex_terminal(action)
        if str(action.get("session_id") or "").startswith("codex:"):
            sid = action.get("session_id")
            with self.lock:
                session = next((copy.deepcopy(item) for item in
                    self.snapshot_cache.get("sessions") or []
                    if item.get("session_id") == sid), None)
            if action.get("type") == "close_preview":
                return (self.close_worktree_preview(session) if session else
                        {"ok": False, "error": "session not live"})
            if action.get("type") == "close" and action.get("cleanup_ticket") and \
               not self._cleanup_ticket_matches(action.get("cleanup_ticket"), sid):
                return {"ok": False, "error": "cleanup preview expired — refresh before closing"}
            result = self.codex.act(action)
            if action.get("type") == "close" and result.get("ok"):
                self._mark_cleanup_ticket_closed(action.get("cleanup_ticket"), sid)
            return result
        if action.get("type") == "spawn":    # no session yet — it makes one
            if action.get("provider") == "codex":
                return self.spawn_codex_session(action)
            return self.spawn_session(action)
        sid = action.get("session_id")
        if action.get("type") == "reopen":
            return self.reopen_claude_session(sid)
        reg = next((r for r in self.live_sessions() if r.get("sessionId") == sid), None)
        if not reg:
            return {"ok": False, "error": "session not live"}
        if action.get("type") == "close_preview":
            return self.close_worktree_preview({**reg, "provider": "claude"})
        if action.get("type") == "close":
            if action.get("cleanup_ticket") and not self._cleanup_ticket_matches(
                    action.get("cleanup_ticket"), sid):
                return {"ok": False, "error": "cleanup preview expired — refresh before closing"}
            result = self._close_claude_session(reg)
            if result.get("ok"):
                self._mark_cleanup_ticket_closed(action.get("cleanup_ticket"), sid)
            return result
        if action.get("type") == "permission_mode" and reg.get("status") != "idle":
            return {"ok": False, "error": "permission mode can change only while Claude is idle"}
        # a prompt answer may only go to a session actually blocked on a prompt —
        # a hook-blocked ask leaves a ghost pending file but the session stays
        # 'busy', and injected digits would land in its main input box
        if action.get("type") in ("option", "multiq", "permission", "dismiss") \
           and reg.get("status") != "waiting":
            return {"ok": False, "error": "session isn't waiting on a prompt — "
                    "this question may have been blocked or already answered"}
        if action.get("type") == "interrupt" and reg.get("status") not in ("busy", "shell"):
            return {"ok": False, "error": "session isn't mid-turn — nothing to interrupt"}
        # a relay is typed into the PARENT's input box: if the parent is blocked on
        # a prompt, that box is the ask TUI and the relay would answer the question
        if action.get("type") == "relay" and reg.get("status") == "waiting":
            return {"ok": False, "error": "the parent session is waiting on a prompt — "
                    "answer that first, then relay"}
        path = os.path.join(cwd_to_project_dir(reg.get("cwd", "")), f"{sid}.jsonl")
        if action.get("type") == "interrupt" and reg.get("status") == "shell":
            with self.scan_lock:
                shell_tail = self.tail_for(path)
                shell_tail.poll()
                if shell_tail.turn_state() == "awaiting_input":
                    return {"ok": False, "error": "the shell command has finished — "
                            "there is no active turn to interrupt"}
        # scan_lock is held by the poll thread while it folds EVERY transcript in the
        # fleet, so taking it here made a click wait out a whole scan (~300ms of the
        # measured latency). Only a PROMPT ANSWER needs the freshness re-poll (it
        # validates the nonce against the live tail); typing, focusing, interrupting
        # and relaying don't touch the tail at all — build those with no lock.
        needs_tail = action.get("type") in (
            "option", "multiq", "permission", "dismiss", "permission_mode")
        lock = self.scan_lock if needs_tail else contextlib.nullcontext()
        with lock:
            mt = self.tail_for(path)
            if needs_tail:
                mt.poll()   # NEVER poll unlocked: it would race the poll thread's
                            # fold of the same Tail and double-count its usage
            typ = action.get("type")
            steps = []                  # [(text, send_newline)]
            # free text typed into a TUI row must never smuggle keys: strip control
            # chars (a \r would fire as Enter, \x1b starts an escape sequence)
            clean = lambda t: re.sub(r"[\x00-\x1f\x7f]+", " ", str(t or "")).strip()[:300]
            if typ == "permission_mode":
                target = str(action.get("mode") or "")
                allowed = self._claude_permission_modes(reg, mt)
                current = mt.permission_mode
                if target not in ("default", "acceptEdits", "plan", "auto",
                                  "bypassPermissions"):
                    return {"ok": False, "error": "unknown Claude permission mode"}
                if current == "dontAsk":
                    return {"ok": False, "error": "Don't ask is startup-only in Claude Code; "
                            "start a new session to choose another mode"}
                if current not in allowed:
                    return {"ok": False, "error": "Claude has not reported a live permission "
                            "mode yet — send one message from the terminal first"}
                if target not in allowed:
                    if target == "auto":
                        return {"ok": False, "error": "Auto is unavailable for this running "
                                "session's model or account"}
                    if target == "bypassPermissions":
                        return {"ok": False, "error": "Bypass was not enabled when this Claude "
                                "process started"}
                    return {"ok": False, "error": "mode is unavailable for this session"}
                count = (allowed.index(target) - allowed.index(current)) % len(allowed)
                if count == 0:
                    return {"ok": True, "mode": current}
                # Shift+Tab is Claude's own documented mid-session control. The
                # cycle is server-derived; the client supplies only an allowlisted
                # target, never arbitrary keys.
                steps = [("\x1b[Z", False)] * count
            elif typ in ("option", "permission", "multiq", "dismiss"):
                nonce = action.get("nonce")
                hp = self.hook_pending(sid, reg.get("status"))
                if not ((hp and hp.get("nonce") == nonce) or nonce in mt.pending):
                    return {"ok": False, "error": "stale: the prompt changed — refresh"}
                if typ == "dismiss":
                    # Esc anywhere in the ask TUI = "Chat about this" (sandbox-proven
                    # 2026-07-14: tool returns "User declined to answer questions")
                    steps = [("\x1b", False)]
                elif typ == "multiq":
                    answers = action.get("answers") or []
                    if not answers:
                        return {"ok": False, "error": "no answers"}
                    # Sandbox-proven recipes (2026-07-14, every transition captured):
                    # single-select = BARE DIGIT (instant select + advance — a separate
                    # CR write after a digit re-fires on the next view as a "phantom
                    # Enter", which corrupted 6 live rounds; digits alone don't).
                    #   with Other: digit n+1 focuses the "Type something" row, text
                    #   types into it, one CR selects + advances (clean, no phantom).
                    # multi-select = digit writes toggle (focus stays row 1), then
                    # down-arrows to the Next/Submit row (options, "Type something",
                    # then it: n_options+1 downs from row 1), then one CR — advances
                    # cleanly onto question or review. Review = bare digit 1 submits.
                    #   with Other: digit n+1 toggles the row's checkbox, DOWN×n
                    #   focuses its input, text types in, one more DOWN reaches
                    #   Next/Submit, CR.
                    DOWN = "\x1b[B"
                    steps = []
                    for a in answers[:8]:
                        digits = sorted({int(d) for d in (a.get("digits") or [])})[:9]
                        other = clean(a.get("other"))
                        n = int(a.get("n_options") or (max(digits) if digits else 0))
                        if not digits and not other:
                            return {"ok": False, "error": "every question needs an answer"}
                        if other and n < 1:
                            return {"ok": False, "error": "Other needs n_options"}
                        if a.get("multi"):
                            steps += [(str(d), False) for d in digits]
                            if other:
                                steps.append((str(n + 1), False))
                                steps += [(DOWN, False)] * n
                                steps.append((other, False))
                                steps.append((DOWN, False))
                            else:
                                steps += [(DOWN, False)] * (n + 1)
                            steps.append(("", True))
                        elif other:
                            steps.append((str(n + 1), False))
                            steps.append((other, False))
                            steps.append(("", True))
                        else:
                            steps.append((str(digits[0]), False))
                    steps.append(("1", False))
                elif typ == "option":
                    digits = [str(int(d)) for d in action.get("digits", [])][:8]
                    other = clean(action.get("other"))
                    n = int(action.get("n_options") or 0)
                    if not digits and not other:
                        return {"ok": False, "error": "no option chosen"}
                    if other and n < 1:
                        return {"ok": False, "error": "Other needs n_options"}
                    if action.get("multi"):
                        steps = [(d, False) for d in digits]
                        if other:
                            # Other rides the Submit ROW path (goes through the
                            # Review pane; trailing 1 submits it) — sandbox-proven
                            steps.append((str(n + 1), False))
                            steps += [("\x1b[B", False)] * n
                            steps.append((other, False))
                            steps.append(("\x1b[B", False))
                            steps.append(("", True))
                            steps.append(("1", False))
                        else:
                            # digits toggle; Enter toggles too. Submitting = right-
                            # arrow to the "✔ Submit" TAB + Enter (skips Review).
                            steps.append(("\x1b[C", False))
                            steps.append(("", True))
                    elif other:
                        steps = [(str(n + 1), False), (other, False), ("", True)]
                    else:
                        steps = [(d, False) for d in digits]
                        steps.append(("", True))
                else:
                    pk = self.cfg.get("permission_keys", {})
                    if action.get("choice") not in pk:
                        return {"ok": False, "error": "unknown choice"}
                    # an empty key means Esc (deny cancels any prompt variant)
                    key = pk[action.get("choice")] or "\x1b"
                    steps = [(key, False)]
                    if key != "\x1b":
                        steps.append(("", True))
            elif typ == "focus":        # bring that session's iTerm tab to the front
                steps = [("__FOCUS__", False)]
            elif typ == "interrupt":    # Esc mid-turn = the terminal's stop key
                steps = [("\x1b", False)]
            elif typ == "noop":         # TCC/AppleScript path probe: delivers nothing
                steps = [("", False)]
            elif typ == "relay":
                # A subagent has NO tty — the only channel to it is the parent
                # calling SendMessage. So a "message to a subagent" is a tagged
                # line typed into the PARENT's input box; the parent forwards it.
                # Delivery is the parent's call, never guaranteed by us.
                jl, meta_path = self._agent_paths(sid, action.get("agent_id"))
                if not jl:
                    return {"ok": False, "error": "no such subagent"}
                body = re.sub(r"[\x00-\x1f\x7f]+", " ", str(action.get("text", ""))).strip()[:1500]
                if not body:
                    return {"ok": False, "error": "empty text"}
                try:
                    desc = (json.load(open(meta_path)) or {}).get("description", "")
                except Exception:
                    desc = ""
                aid = action.get("agent_id")
                steps = [(f"[fleet-dash relay to subagent {aid}"
                          f"{f' — “{desc}”' if desc else ''}] {body} "
                          f"(forward it with SendMessage; if that agent can't be "
                          f"resumed, say so instead of acting on this yourself)", True)]
            elif typ in ("text", "image_text", "handoff_text"):
                limit = 30_000 if typ == "handoff_text" else 2000
                txt = str(action.get("text", ""))[:limit].strip()
                if typ == "image_text":
                    paths = action.get("image_paths") or []
                    if not paths:
                        return {"ok": False, "error": "no images"}
                    txt = txt or ("Please inspect the attached image." if len(paths) == 1 else
                                  "Please inspect the attached images.")
                    txt += "\n\nImages attached through Fleet:\n" + "\n".join(
                        f"- {path}" for path in paths)
                if not txt:
                    return {"ok": False, "error": "empty text"}
                # a leading "/" opens the TUI's OWN command popup, where Enter fires
                # the HIGHLIGHTED entry — not necessarily what was typed. A space
                # closes that popup, so the CR submits the literal text
                # (sandbox-proven 2026-07-14: "/status" + CR ran the highlighted
                # match; "/status " + CR submitted the text with no popup open).
                if txt.startswith("/") and " " not in txt:
                    txt += " "
                steps = [(txt, True)]
            else:
                return {"ok": False, "error": "unknown action type"}
        tty = self._tty_cache.get(reg["pid"])     # a pid's tty never changes
        if not tty:
            try:
                tty = subprocess.run(["ps", "-p", str(reg["pid"]), "-o", "tty="],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception as e:
                return {"ok": False, "error": f"tty lookup failed: {e}"}
            if tty and tty != "??":
                self._tty_cache[reg["pid"]] = tty
        if not tty or tty == "??":
            return {"ok": False, "error": "session has no terminal (VS Code / headless)"}
        # The 0.4s inter-key delay is load-bearing ONLY for the ask-TUI key sequences
        # (digits/arrows/CR need a render between them, or keys get dropped — invariant
        # 4). Typing a message or focusing a tab is one or two keys with nothing to
        # re-render, so those wait 0.05s and the click stops feeling laggy.
        fast = typ in ("text", "image_text", "handoff_text", "relay", "focus", "interrupt", "noop")
        result = self._iterm_write(f"/dev/{tty}", steps, step_delay=0.05 if fast else 0.4)
        if typ == "permission_mode" and result.get("ok"):
            # Claude may defer its transcript marker until the next prompt. Keep
            # Fleet's state responsive; the next native row remains authoritative.
            with self.scan_lock:
                self.tail_for(path).permission_mode = target
            result["mode"] = target
        return result

    MODELS = ("opus", "sonnet", "haiku", "fable")
    EFFORTS = ("low", "medium", "high", "xhigh", "max")
    CLAUDE_START_PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")

    def _create_codex_worktree(self, cwd, requested_name=""):
        identity = self.workstream_identity(cwd)
        root = identity.get("root") if identity.get("kind") == "git" else None
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": "new worktree requires a Git repository"}
        name = str(requested_name or "").strip() or f"handoff-{secrets.token_hex(4)}"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
            return {"ok": False, "error": "worktree name: letters, digits, . _ - only"}
        repo_key = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]
        parent = os.path.join(HOME, ".claude", "fleet-dash-worktrees", repo_key)
        path = os.path.join(parent, name)
        if os.path.lexists(path):
            return {"ok": False, "error": "that managed worktree path already exists"}
        os.makedirs(parent, exist_ok=True)
        branch = f"fleet/{name}"
        try:
            process = subprocess.run(
                ["git", "-C", root, "worktree", "add", "-b", branch, path, "HEAD"],
                capture_output=True, text=True, timeout=30)
        except Exception as exc:
            return {"ok": False, "error": f"could not create worktree: {exc}"}
        if process.returncode:
            detail = (process.stderr or process.stdout or "git worktree add failed").strip()
            return {"ok": False, "error": detail[:1000]}
        return {"ok": True, "cwd": path, "root": root, "branch": branch,
                "worktree_name": name, "created": True}

    def _remove_failed_codex_worktree(self, created):
        if not created or not created.get("created"):
            return None
        try:
            process = subprocess.run(
                ["git", "-C", created["root"], "worktree", "remove", created["cwd"]],
                capture_output=True, text=True, timeout=30)
            if process.returncode:
                return (process.stderr or process.stdout or
                        "created worktree could not be removed").strip()[:1000]
        except Exception as exc:
            return str(exc)
        return None

    def _deliver_existing_handoff(self, link, preview):
        destination_sid = link["destination_session_id"]
        if link["destination_provider"] == "codex":
            return self.codex.act({"type": "text", "session_id": destination_sid,
                                   "text": preview})
        return self.act({"type": "handoff_text", "session_id": destination_sid,
                         "text": preview})

    def execute_handoff(self, action):
        source_sid = str(action.get("session_id") or "")
        source = self._find_handoff_source(source_sid)
        if not source:
            return {"ok": False, "error": "source session is unavailable"}
        target = str(action.get("provider") or "").strip().lower()
        if target not in ("claude", "codex"):
            return {"ok": False, "error": "provider must be claude or codex"}
        raw_preview = str(action.get("preview") or "")
        if not raw_preview.strip():
            return {"ok": False, "error": "handoff message is empty"}
        if len(raw_preview) > 30_000:
            return {"ok": False, "error": "handoff message is too long (30,000 characters max)"}
        preview = redact_handoff_text(raw_preview)
        preview_hash = hashlib.sha256(preview.encode()).hexdigest()
        retry_sid = str(action.get("destination_session_id") or "")
        if retry_sid:
            link = self._handoff_link(source_sid, retry_sid)
            if not link or link.get("destination_provider") != target:
                return {"ok": False, "error": "stale or mismatched handoff destination"}
            result = self._deliver_existing_handoff(link, preview)
            status = "delivered" if result.get("ok") else "delivery_failed"
            self._record_handoff_link(source_sid, source.get("provider") or "claude",
                retry_sid, target, status, preview_hash, result.get("error"))
            return {**result, "source_session_id": source_sid,
                    "destination_session_id": retry_sid, "provider": target,
                    "created": False, "retryable": not result.get("ok")}

        cwd = os.path.realpath(os.path.expanduser(
            str(action.get("cwd") or source.get("cwd") or "").strip()))
        home = os.path.realpath(HOME)
        if not cwd or not os.path.isdir(cwd):
            return {"ok": False, "error": "no such directory"}
        if cwd != home and not cwd.startswith(home + os.sep):
            return {"ok": False, "error": "directory must be under your home folder"}
        mode = str(action.get("mode") or "plan") if target == "codex" else None
        if target == "codex" and mode not in ("plan", "default"):
            return {"ok": False, "error": "mode must be plan or default"}
        worktree_created = None
        if bool(action.get("worktree")) and target == "codex":
            worktree_created = self._create_codex_worktree(
                cwd, action.get("worktree_name"))
            if not worktree_created.get("ok"):
                return worktree_created
            cwd = worktree_created["cwd"]

        if target == "codex":
            try:
                thread = self.codex.start_thread(
                    cwd, str(action.get("model") or "").strip() or None,
                    str(action.get("effort") or "").strip() or None, mode,
                    initial_text="hi\n\n" + preview)
            except Exception as exc:
                cleanup_error = self._remove_failed_codex_worktree(worktree_created)
                result = {"ok": False, "error": str(exc)}
                if cleanup_error:
                    result["cleanup_error"] = cleanup_error
                return result
            tid = thread.get("id")
            if not tid:
                cleanup_error = self._remove_failed_codex_worktree(worktree_created)
                result = {"ok": False, "error": "Codex did not return a thread id"}
                if cleanup_error:
                    result["cleanup_error"] = cleanup_error
                return result
            destination_sid = self.codex.key(tid)
            self._record_handoff_link(source_sid, source.get("provider") or "claude",
                destination_sid, target, "delivered", preview_hash)
            return {"ok": True, "source_session_id": source_sid,
                    "destination_session_id": destination_sid, "session_id": destination_sid,
                    "provider": target, "cwd": cwd, "created": True,
                    "worktree": worktree_created}

        if not self.is_trusted(cwd):
            return {"ok": False, "error": "Claude has not trusted this directory yet; open it locally once first"}
        destination_sid = str(uuid.uuid4())
        spawn = self.spawn_session({**action, "cwd": cwd}, reserved_sid=destination_sid)
        if not spawn.get("ok"):
            return spawn
        self._record_handoff_link(source_sid, source.get("provider") or "claude",
            destination_sid, target, "spawning", preview_hash)
        deadline = time.monotonic() + 30
        reg = None
        while time.monotonic() < deadline:
            reg = next((item for item in self.live_sessions()
                        if item.get("sessionId") == destination_sid), None)
            if reg:
                break
            time.sleep(.1)
        if not reg:
            error = "Claude session was created but did not become attachable within 30 seconds"
            self._record_handoff_link(source_sid, source.get("provider") or "claude",
                destination_sid, target, "delivery_failed", preview_hash, error)
            return {"ok": False, "error": error, "source_session_id": source_sid,
                    "destination_session_id": destination_sid, "provider": target,
                    "created": True, "retryable": True}
        delivered = self.act({"type": "handoff_text", "session_id": destination_sid,
                              "text": preview})
        status = "delivered" if delivered.get("ok") else "delivery_failed"
        self._record_handoff_link(source_sid, source.get("provider") or "claude",
            destination_sid, target, status, preview_hash, delivered.get("error"))
        return {**delivered, "source_session_id": source_sid,
                "destination_session_id": destination_sid, "session_id": destination_sid,
                "provider": target, "cwd": cwd, "created": True,
                "retryable": not delivered.get("ok")}

    def attach_codex_terminal(self, action):
        """Open a TUI client on the same App Server; never resume a copy."""
        from codex_adapter import codex_command, codex_control_socket
        sid = str(action.get("session_id") or "")
        tid = self.codex.native(sid)
        session = next((item for item in self.codex.sessions()
                        if item.get("session_id") == sid), None)
        if not session or not session.get("capabilities", {}).get("focus_terminal"):
            return {"ok": False, "error": "this Codex thread is view only"}
        cwd = os.path.realpath(os.path.expanduser(session.get("cwd") or HOME))
        if not os.path.isdir(cwd):
            return {"ok": False, "error": "session working directory no longer exists"}
        executable = codex_command(self.cfg.get("codex_command") or None)
        endpoint = "unix://" + codex_control_socket()
        command = (f"cd {shlex.quote(cwd)} && {shlex.quote(executable)} resume "
                   f"--remote {shlex.quote(endpoint)} {shlex.quote(tid)}")
        result = self._iterm_write("SPAWN", [(command, False)])
        if result.get("ok"):
            result.update(command=command, session_id=sid, shared_runtime=True)
        return result

    def spawn_codex_session(self, action):
        """Create a Codex thread through app-server; no terminal or TUI scraping."""
        cwd = os.path.realpath(os.path.expanduser(str(action.get("cwd") or "").strip()))
        home = os.path.realpath(HOME)
        if not cwd or not os.path.isdir(cwd):
            return {"ok": False, "error": "no such directory"}
        if cwd != home and not cwd.startswith(home + os.sep):
            return {"ok": False, "error": "directory must be under your home folder"}
        blocked = self._spawn_budget_blockers("codex", cwd)
        if blocked:
            return {"ok": False, "error": "new Codex sessions are blocked by an exceeded budget",
                    "budget_blockers": blocked}
        initial_text = str(action.get("initial_text") or "hi").strip()
        if not initial_text or len(initial_text) > 2000:
            return {"ok": False, "error": "initial message must be 1–2,000 characters"}
        try:
            thread = self.codex.start_thread(
                cwd, str(action.get("model") or "").strip() or None,
                str(action.get("effort") or "").strip() or None,
                str(action.get("mode") or "plan").strip(), initial_text=initial_text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        tid = thread.get("id")
        return {"ok": bool(tid), "session_id": self.codex.key(tid) if tid else None,
                "provider": "codex", "cwd": cwd,
                "initial_message": initial_text if tid else None}

    def spawn_session(self, action, reserved_sid=None):
        """Start a NEW Claude Code session in a fresh iTerm tab.

        Every value that reaches the shell is allowlisted or quoted: the model and
        effort and permission mode must be members of fixed sets above, the worktree
        name is regex-bounded, and the directory must be an existing dir under $HOME. Nothing the
        client sends is interpolated raw — the act token opens a terminal here, so a
        free-form command string would be a remote shell."""
        cwd = os.path.realpath(os.path.expanduser(str(action.get("cwd") or "").strip()))
        home = os.path.realpath(HOME)
        if not cwd or not os.path.isdir(cwd):
            return {"ok": False, "error": "no such directory"}
        if cwd != home and not cwd.startswith(home + os.sep):
            return {"ok": False, "error": "directory must be under your home folder"}
        blocked = self._spawn_budget_blockers("claude", cwd)
        if blocked:
            return {"ok": False, "error": "new Claude sessions are blocked by an exceeded budget",
                    "budget_blockers": blocked}
        model = str(action.get("model") or "").strip()
        if model and model not in self.MODELS:
            return {"ok": False, "error": "unknown model"}
        effort = str(action.get("effort") or "").strip()
        if effort and effort not in self.EFFORTS:
            return {"ok": False, "error": "unknown effort level"}
        permission_mode = str(action.get("permission_mode") or "default").strip()
        if permission_mode not in self.CLAUDE_START_PERMISSION_MODES:
            return {"ok": False, "error": "unknown Claude permission mode"}
        name = str(action.get("worktree_name") or "").strip()
        if name and not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
            return {"ok": False, "error": "worktree name: letters, digits, . _ - only"}
        worktree = bool(action.get("worktree"))
        if worktree and not os.path.isdir(os.path.join(cwd, ".git")):
            # a worktree needs a repo; a linked worktree has .git as a FILE, so
            # only the main checkout qualifies as a spawn point
            if not os.path.isfile(os.path.join(cwd, ".git")):
                return {"ok": False, "error": "not a git repo — can't make a worktree"}

        session_id = str(reserved_sid or uuid.uuid4())
        try:
            session_id = str(uuid.UUID(session_id))
        except (ValueError, TypeError, AttributeError):
            return {"ok": False, "error": "invalid Claude session id"}
        cmd = (f"cd {shlex.quote(cwd)} && claude --session-id "
               f"{shlex.quote(session_id)}")
        if model:
            cmd += f" --model {model}"
        if effort:
            cmd += f" --effort {effort}"
        if permission_mode != "default":
            cmd += f" --permission-mode {permission_mode}"
        if worktree:
            cmd += " --worktree" + (f" {name}" if name else "")
        r = self._iterm_write("SPAWN", [(cmd, False)])
        if r.get("ok"):
            print(f"spawn: {cmd}", file=sys.stderr, flush=True)
            r["command"] = cmd
            r["cwd"] = cwd
            r["session_id"] = session_id
            r["provider"] = "claude"
            # an untrusted dir stops at "do you trust the files in this folder?",
            # which only the Mac can answer — say so instead of leaving the phone
            # waiting for a session that never starts
            r["trust_prompt"] = not self.is_trusted(cwd)
        return r

    def _spawn_budget_blockers(self, provider, cwd):
        if not self.operations.has_spawn_limits():
            return []
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        try:
            identity = self.workstream_identity(cwd)
            blockers = self.operations.spawn_blockers(
                snapshot, provider, cwd, identity.get("workstream_id"))
            return [{key: item.get(key) for key in
                     ("id", "label", "scope_type", "scope_id", "metric",
                      "value", "limit_value", "measurement_scope")}
                    for item in blockers]
        except Exception as exc:
            print(f"budget spawn check failed: {exc}", file=sys.stderr, flush=True)
            return [{"id": "budget-check-unavailable",
                     "label": "Budget safety check unavailable",
                     "scope_type": "fleet", "scope_id": None,
                     "metric": "unknown", "value": None, "limit_value": None,
                     "measurement_scope": "unavailable"}]

    def _iterm_write(self, tty, steps, step_delay=None):
        # launchd-context osascript can never summon the automation-permission
        # dialog (hangs forever), so injection runs through the FleetDashInjector
        # applet: request file -> open -g applet -> result file. The applet has its
        # own TCC identity and prompts normally on first use.
        import base64
        req_id = secrets.token_hex(8)
        lines = [tty, req_id]
        if step_delay is not None:      # flag 4: how long the applet waits BETWEEN keys
            lines.append(f"4 {step_delay}")
        for text, nl in steps:
            if text == "__FOCUS__":     # flag 3: select that tab, type nothing
                lines.append("3 ")
                continue
            if text:
                lines.append("0 " + base64.b64encode(text.encode()).decode())
            if nl:                      # raw CR — raw-mode TUIs' Enter (LF toggles!)
                lines.append("2 ")
        req_path = os.path.join(BASE, "inject-request.txt")
        res_path = os.path.join(BASE, "inject-result.txt")
        try:
            os.remove(res_path)
        except OSError:
            pass
        with open(req_path, "w") as f:
            f.write("\n".join(lines))
        app = os.path.join(BASE, "FleetDashInjector.app")
        try:
            subprocess.run(["open", "-g", app], capture_output=True, timeout=10)
        except Exception as e:
            return {"ok": False, "error": f"injector launch failed: {e}"}
        deadline = time.time() + 30    # generous: first run includes the TCC dialog
        while time.time() < deadline:
            try:
                out = open(res_path).read().strip()
                if out.startswith(req_id):
                    verdict = out[len(req_id):].strip()
                    if verdict == "ok":
                        return {"ok": True}
                    return {"ok": False, "error": verdict[:300]}
            except OSError:
                pass
            time.sleep(0.02)            # the applet is done in ~200ms — don't sleep past it
        return {"ok": False, "error": "injector timed out — if a macOS permission "
                "dialog appeared, grant it and retry"}

    # ---------------------------------------------------------------- ntfy
    def _send_legacy_ntfy_test(self, key):
        topic = self.cfg.get("ntfy_topic")
        if not topic:
            self.operations.notification_status(key, "disabled")
            return
        url = f"{self.cfg['ntfy_server'].rstrip('/')}/{topic}"
        headers = {"Title": "Fleet legacy notification test", "Tags": "test_tube",
                   "Priority": "default"}
        req = urllib.request.Request(
            url, data=b"Manual ntfy delivery is working.", method="POST", headers=headers)
        threading.Thread(target=lambda: self._post(key, req), daemon=True).start()

    def _post(self, key, req):
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
            self.operations.notification_status(key, "sent")
        except Exception:
            self.operations.notification_status(
                key, "failed", "Legacy ntfy delivery failed")

    def legacy_ntfy_test(self):
        """Queue one generic legacy delivery; ntfy never receives automatic events."""
        if self.cfg.get("legacy_ntfy_enabled") is not True:
            return {"ok": False, "error": "Legacy ntfy is disabled"}
        if not self.cfg.get("ntfy_topic") or not self.cfg.get("ntfy_server"):
            return {"ok": False, "error": "Legacy ntfy is not configured"}
        key = "legacy-test:" + uuid.uuid4().hex
        if not self.operations.notification_claim(
                key, "legacy_test", "Fleet legacy notification test",
                "Manual ntfy delivery is working.", dispatch=True):
            return {"ok": False, "error": "Legacy ntfy test could not be queued"}
        self._send_legacy_ntfy_test(key)
        return {"ok": True, "queued": True}

    #                key                              type  min  max
    NUM_KEYS = {"stall_seconds":                 (int,   30,   86400),
                "preview_session_lines":         (int,   1,    6),
                "preview_agent_lines":           (int,   1,    6)}
    BOOL_KEYS = ("preview_sessions", "preview_agents", "legacy_ntfy_enabled")

    def update_settings(self, patch):
        """Persist dashboard-editable layout, legacy, session, and budget settings."""
        if not isinstance(patch, dict):
            return {"ok": False, "error": "settings patch must be an object"}
        with self.config_lock:
            return self._update_settings(patch)

    def _update_settings(self, patch):
        allowed = (set(self.NUM_KEYS) | set(self.BOOL_KEYS) | {
            "reader_width", "mute_session", "muted", "pin_session", "pinned",
            "mark_available_session", "mark_read_session", "revision", "bulk_triage",
            "budgets"})
        unknown = sorted(str(key) for key in patch if key not in allowed)
        if unknown:
            return {"ok": False, "error": f"unknown settings field: {unknown[0]}"}
        if "muted" in patch and "mute_session" not in patch:
            return {"ok": False, "error": "muted requires mute_session"}
        if "pinned" in patch and "pin_session" not in patch:
            return {"ok": False, "error": "pinned requires pin_session"}
        if "revision" in patch and not ({"mark_available_session", "mark_read_session"} & set(patch)):
            return {"ok": False, "error": "revision requires a session marker"}
        staged = copy.deepcopy(self.cfg)
        changed = {}
        for k, (typ, lo, hi) in self.NUM_KEYS.items():
            if k in patch:
                if isinstance(patch[k], bool):
                    return {"ok": False, "error": f"bad value for {k}"}
                try:
                    v = typ(float(patch[k]))
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"bad value for {k}"}
                if not lo <= v <= hi:
                    return {"ok": False, "error": f"{k} must be {lo}–{hi}"}
                staged[k] = changed[k] = v
        for k in self.BOOL_KEYS:
            if k in patch:
                if not isinstance(patch[k], bool):
                    return {"ok": False, "error": f"{k} must be boolean"}
                staged[k] = changed[k] = patch[k]
        if "reader_width" in patch:
            width = str(patch["reader_width"] or "")
            if width not in ("fit", "centered"):
                return {"ok": False, "error": "reader_width must be fit or centered"}
            staged["reader_width"] = changed["reader_width"] = width
        ms = patch.get("mute_session")
        if "mute_session" in patch:
            if (not isinstance(ms, str) or not ms.strip() or len(ms) > 300 or
                    any(ord(char) < 32 for char in ms)):
                return {"ok": False, "error": "invalid mute_session"}
            if not isinstance(patch.get("muted"), bool):
                return {"ok": False, "error": "muted must be boolean"}
            mu = dict(staged.get("muted_sessions") or {})
            if patch["muted"]:
                mu[ms] = time.time()
            else:
                mu.pop(ms, None)
            if len(mu) > 5000:
                return {"ok": False, "error": "too many muted sessions"}
            staged["muted_sessions"] = changed["muted_sessions"] = mu
        if "pin_session" in patch:
            sid = str(patch.get("pin_session") or "").strip()
            if not sid or len(sid) > 300 or any(ord(char) < 32 for char in sid):
                return {"ok": False, "error": "valid pin_session is required"}
            if not isinstance(patch.get("pinned"), bool):
                return {"ok": False, "error": "pinned must be boolean"}
            pins = [str(item) for item in (staged.get("pinned_sessions") or [])
                    if str(item) != sid]
            if patch["pinned"]:
                pins.append(sid)
            pins = pins[-500:]
            staged["pinned_sessions"] = changed["pinned_sessions"] = pins
        for patch_key, config_key in (("mark_available_session", "reply_available"),
                                      ("mark_read_session", "read_sessions")):
            if patch_key not in patch:
                continue
            sid = str(patch.get(patch_key) or "").strip()
            revision = str(patch.get("revision") or "").strip()
            if (not sid or not revision or len(sid) > 300 or len(revision) > 300 or
                    any(ord(char) < 32 for char in sid + revision)):
                return {"ok": False, "error": f"{patch_key} and revision are required"}
            values = dict(staged.get(config_key) or {})
            values.pop(sid, None)
            values[sid] = revision
            values = dict(list(values.items())[-1000:])
            staged[config_key] = changed[config_key] = values
        bulk = patch.get("bulk_triage")
        if bulk is not None:
            if not isinstance(bulk, dict):
                return {"ok": False, "error": "bulk_triage must be an object"}
            operation = str(bulk.get("operation") or "")
            items = bulk.get("items")
            if operation not in ("mark_read", "mark_available", "mute", "dismiss"):
                return {"ok": False, "error": "unsupported bulk triage operation"}
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                return {"ok": False, "error": "bulk triage requires 1–100 items"}
            normalized = []
            for item in items:
                if not isinstance(item, dict):
                    return {"ok": False, "error": "invalid bulk triage item"}
                sid = str(item.get("session_id") or "").strip()
                action_id = str(item.get("action_id") or "").strip()
                revision = str(item.get("revision") or "").strip()
                invalid_text = (len(sid) > 300 or len(action_id) > 80 or len(revision) > 300 or
                                any(ord(char) < 32 for char in sid + action_id + revision))
                if not sid or not action_id or invalid_text:
                    return {"ok": False, "error": "bulk triage item needs session and action IDs"}
                if operation in ("mark_read", "mark_available") and not revision:
                    return {"ok": False, "error": "bulk triage revision is required"}
                normalized.append((sid, action_id, revision))
            if operation != "mute":
                with self.lock:
                    current_actions = {item.get("action_id"): item for item in
                                       (self.snapshot_cache.get("actions") or [])}
                for sid, action_id, revision in normalized:
                    current = current_actions.get(action_id)
                    if (not current or current.get("session_id") != sid or
                            operation not in (current.get("safe_bulk") or []) or
                            (revision and str(current.get("revision") or "") != revision)):
                        return {"ok": False,
                                "error": "stale or ineligible bulk triage action"}
            now = time.time()
            if operation == "mute":
                values = dict(staged.get("muted_sessions") or {})
                for sid, _, _ in normalized:
                    values[sid] = now
                if len(values) > 5000:
                    return {"ok": False, "error": "too many muted sessions"}
                staged["muted_sessions"] = changed["muted_sessions"] = values
            elif operation == "dismiss":
                values = dict(staged.get("dismissed_actions") or {})
                for _, action_id, _ in normalized:
                    values[action_id] = now
                values = dict(list(values.items())[-2000:])
                staged["dismissed_actions"] = changed["dismissed_actions"] = values
            else:
                key = "read_sessions" if operation == "mark_read" else "reply_available"
                values = dict(staged.get(key) or {})
                for sid, _, revision in normalized:
                    values.pop(sid, None)
                    values[sid] = revision
                values = dict(list(values.items())[-1000:])
                staged[key] = changed[key] = values
        if "budgets" in patch:
            try:
                changed["budgets"] = self.operations.replace_budgets(patch.get("budgets"))
            except OperationsError as exc:
                return {"ok": False, "error": str(exc)}
        if not changed:
            return {"ok": False, "error": "nothing to update"}
        persisted = {key: value for key, value in changed.items() if key != "budgets"}
        if persisted:
            self._persist_config_fields(persisted)
            self.cfg.update(persisted)
        return {"ok": True, **changed}

# ---------------------------------------------------------------- one-shots

def find_session_for_cwd(cwd):
    hits = []
    for p in glob.glob(os.path.join(SESSIONS, "*.json")):
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
            for p in glob.glob(os.path.join(SESSIONS, "*.json")):
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


if __name__ == "__main__":
    main()
