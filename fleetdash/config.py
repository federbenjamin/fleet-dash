"""Instance configuration, pricing, and shared constants."""
import os, re, json, secrets, sys, mmap, stat, math
from . import paths as pathcfg

# Claude spawn/control allowlists (Engine.MODELS/EFFORTS alias these).
CLAUDE_MODELS = ("opus", "sonnet", "haiku", "fable")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_CONFIG = {
    "instance_mode": pathcfg.INSTANCE_MODE,
    "instance_name": "Fleet Staging" if pathcfg.INSTANCE_MODE == "staging" else "Fleet Dash",
    "staging_owned_sessions": {},
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
    # Private recovery state. Values contain only provider ids/nonces and
    # allowlisted control selections; never prompts, answers, or credentials.
    "claude_delivery_uncertain": {},      # session_id -> pending prompt nonce
    "claude_control_overrides": {},       # accepted native controls awaiting evidence
    "claude_control_uncertain": {},       # native control writes with a lost result
    "velocity_window_points": 30,
    "port": 8377,
    "bind": "127.0.0.1",
    "codex_enabled": True,
    "codex_command": "",
    "codex_remote_control": True,
    # Terminal control transport. tmux is emulator-agnostic and survives a
    # terminal switch; the AppleScript applet only ever speaks to iTerm2.
    #   auto   — tmux for a session whose tty is a live tmux pane, applet
    #            otherwise; new sessions spawn into tmux when it is installed
    #   tmux   — tmux only; a non-tmux session exposes no terminal transport
    #   applet — the legacy iTerm2 applet only
    "terminal_transport": "auto",
    "terminal_app": "iTerm",            # app raised on focus; "" never raises one
    "tmux_command": "",                 # optional absolute tmux executable
    "tmux_session": "fleet",            # detached tmux session new spawns join
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

# A view-only thread from another runtime cannot receive a Fleet reply. Do not
# leave a prose question in Needs you forever once that runtime has unloaded it.
EXTERNAL_VIEW_ONLY_REPLY_GRACE_SECONDS = 30 * 60


def _validated_claude_delivery_uncertain(raw):
    """Restore only bounded, non-secret prompt identity state."""
    if not isinstance(raw, dict):
        return {}
    restored = {}
    for sid, nonce in list(raw.items())[-200:]:
        if (isinstance(sid, str) and isinstance(nonce, str) and sid and nonce and
                len(sid) <= 300 and len(nonce) <= 500 and
                not any(ord(char) < 32 for char in sid + nonce)):
            restored[sid] = nonce
    return restored


def _validated_claude_control_overrides(raw):
    """Restore bounded allowlisted controls without trusting config shapes."""
    if not isinstance(raw, dict):
        return {}
    restored = {}
    allowed = {
        "model": set(CLAUDE_MODELS),
        "effort": set(CLAUDE_EFFORTS),
        "permission_mode": {"default", "acceptEdits", "plan", "auto",
                            "bypassPermissions"},
    }
    candidates = []
    for sid, controls in raw.items():
        if (not isinstance(sid, str) or not sid or len(sid) > 300 or
                any(ord(char) < 32 for char in sid) or not isinstance(controls, dict)):
            continue
        clean = {}
        newest = 0.0
        for field, values in allowed.items():
            item = controls.get(field)
            if not isinstance(item, dict) or item.get("value") not in values:
                continue
            try:
                accepted_at = float(item.get("accepted_at"))
                baseline = int(item.get("baseline", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if (not math.isfinite(accepted_at) or accepted_at <= 0 or
                    baseline < 0 or baseline > 10 ** 15):
                continue
            clean[field] = {"value": item["value"], "accepted_at": accepted_at,
                            "baseline": baseline}
            newest = max(newest, accepted_at)
        if clean:
            candidates.append((newest, sid, clean))
    for _, sid, clean in sorted(candidates)[-200:]:
        restored[sid] = clean
    return restored


def _validated_claude_control_uncertain(raw):
    if not isinstance(raw, dict):
        return {}
    candidates = []
    for sid, record in raw.items():
        if (not isinstance(sid, str) or not sid or len(sid) > 300 or
                any(ord(char) < 32 for char in sid) or not isinstance(record, dict)):
            continue
        try:
            attempted_at = float(record.get("attempted_at"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(attempted_at) or attempted_at <= 0:
            continue
        fields = {}
        for field in ("model", "effort", "permission_mode"):
            if field not in (record.get("fields") or {}):
                continue
            try:
                baseline = int(record["fields"][field].get("baseline", 0))
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            if 0 <= baseline <= 10 ** 15:
                fields[field] = {"baseline": baseline}
        if fields:
            candidates.append((attempted_at, sid, {
                "attempted_at": attempted_at, "fields": fields}))
    return {sid: record for _, sid, record in sorted(candidates)[-200:]}


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
    secret_path = os.path.join(pathcfg.BASE, "push-secrets.json")
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
    path = os.path.join(pathcfg.BASE, "config.json")
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        os.makedirs(pathcfg.BASE, exist_ok=True)
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
    # Instance identity and listener overrides belong to launchd, not to a
    # browser-editable config file. A stale copied config must never turn a
    # staging process into production or make it bind production's port.
    merged["instance_mode"] = pathcfg.INSTANCE_MODE
    merged["instance_name"] = "Fleet Staging" if pathcfg.INSTANCE_MODE == "staging" else "Fleet Dash"
    port_override = os.environ.get("FLEET_DASH_PORT")
    if port_override:
        try:
            port = int(port_override)
            if not 1 <= port <= 65535:
                raise ValueError
            merged["port"] = port
        except ValueError:
            print("FLEET_DASH_PORT is invalid; using configured port", file=sys.stderr)
    bind_override = os.environ.get("FLEET_DASH_BIND")
    if bind_override:
        merged["bind"] = bind_override
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
    return os.path.join(pathcfg.PROJECTS, cwd.replace("/", "-").replace(".", "-"))


# convo view shows these tools only — read-only chatter (Read/Grep/Glob/task
# bookkeeping) stays hidden (user decision 2026-07-13)
KEY_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "Agent", "Skill", "SendUserFile"}
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
WAITING_CONFIRM_SECONDS = 3.0
IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024
IMAGE_UPLOAD_TTL_SECONDS = 24 * 60 * 60
IMAGE_UPLOAD_SESSION_COUNT = 32
IMAGE_UPLOAD_SESSION_BYTES = 80 * 1024 * 1024
IMAGE_UPLOAD_GLOBAL_COUNT = 200
IMAGE_UPLOAD_GLOBAL_BYTES = 512 * 1024 * 1024
IMAGE_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
IMAGE_UPLOAD_MIMES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
}

# slash commands that destroy conversation state — the page confirms before sending
DANGER_COMMANDS = {"clear", "compact", "quit", "exit", "logout", "rewind"}


# A trailing question that only checks comprehension ("does that make sense?",
# "how does that look?", "clear?") reads as a question but requests no decision —

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
