#!/usr/bin/env python3
"""fleet-dash pending-prompt capture. Registered in ~/.claude/settings.json as:
PreToolUse[AskUserQuestion]  -> writes the pending question (full JSON) for the session
PostToolUse[AskUserQuestion] -> clears it (question answered)
Notification                 -> writes permission-request prompts; ignores idle nags
Never blocks: always exits 0 fast."""
import json, os, sys, time

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sid = d.get("session_id")
if not sid:
    sys.exit(0)
base = os.path.expanduser("~/.claude/fleet-dash/pending")
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
        with open(path, "w") as f:
            json.dump(out, f)
    except Exception:
        pass
sys.exit(0)
