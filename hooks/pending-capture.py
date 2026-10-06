#!/usr/bin/env python3
"""fleet-dash pending-prompt capture. Registered in ~/.claude/settings.json as:
PreToolUse[AskUserQuestion]  -> writes the pending question (full JSON) for the session
PostToolUse[AskUserQuestion] -> clears it (question answered)
Notification                 -> writes permission-request prompts; ignores idle nags
Never blocks: always exits 0 fast."""
import json, os, secrets, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fleetdash import paths

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sid = d.get("session_id")
if not sid:
    sys.exit(0)
base = os.path.join(paths.capture_base(), "pending")
path = os.path.join(base, f"{sid}.json")
ev = d.get("hook_event_name")
out = None

if ev == "PreToolUse" and d.get("tool_name") == "AskUserQuestion":
    out = {"kind": "question", "ts": time.time(),
           "nonce": f"hook-{int(time.time() * 1000)}",
           "questions": (d.get("tool_input") or {}).get("questions", [])}
elif ev == "PostToolUse" and d.get("tool_name") == "AskUserQuestion":
    try:
        os.remove(path)
    except OSError:
        pass
    sys.exit(0)
elif ev == "Notification":
    msg = d.get("message") or ""
    if "permission" not in msg.lower():
        sys.exit(0)                     # idle "waiting for input" nags aren't prompts
    if os.path.exists(path):
        sys.exit(0)                     # never clobber a richer pending (e.g. the question
                                        # whose own input-needed notification this is)
    out = {"kind": "permission", "ts": time.time(),
           "nonce": f"hook-{int(time.time() * 1000)}", "message": msg}

if out:
    try:
        os.makedirs(base, exist_ok=True)
        temp = os.path.join(base, f".{sid}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(out, f)
            f.flush()
            os.fsync(f.fileno())
        if ev == "Notification":
            # Install only if no richer question won the race. Linking a fully
            # written temp is atomic and never clobbers an existing capture.
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
            os.unlink(temp)
        else:
            os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except Exception:
            pass
sys.exit(0)
