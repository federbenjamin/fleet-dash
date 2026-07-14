#!/usr/bin/env python3
"""fleet-dash engine: scans live Claude Code sessions + their subagent
transcripts into a fleet snapshot; maintains the spend ledger; fires ntfy.

Data sources (all local, read-only):
  ~/.claude/sessions/<pid>.json          live-session registry (CLI-maintained)
  ~/.claude/projects/<proj>/<sid>.jsonl  main-thread transcript
  ~/.claude/projects/<proj>/<sid>/subagents/agent-*.jsonl (+ .meta.json)

CLI:  engine.py spend [--cwd DIR | --session SID]   one-shot spend table
      engine.py snapshot                            one-shot fleet JSON
"""
import json, os, re, sys, glob, time, sqlite3, secrets, subprocess, threading, urllib.request
from collections import deque

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".claude", "fleet-dash")
PROJECTS = os.path.join(HOME, ".claude", "projects")
SESSIONS = os.path.join(HOME, ".claude", "sessions")

DEFAULT_CONFIG = {
    "poll_seconds": 2,
    "stall_seconds": 240,
    "awaiting_input_notify_seconds": 180,
    "dormant_seconds": 7200,
    "turn_done_window_seconds": 900,
    "agent_done_quiet_seconds": 5,
    "spend_threshold_usd": 5.0,
    "velocity_window_points": 30,
    "port": 8377,
    "bind": "127.0.0.1",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
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
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
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


# ---------------------------------------------------------------- transcripts

class Tail:
    """Incremental jsonl reader: keeps byte offset + running aggregates."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.ti = self.tw = self.tr = self.to = 0
        self.model = ""
        self.last_usage = None          # usage dict of last assistant row
        self.last_shape = None          # ('assistant', stop_reason, [content types]) or ('user', kind)
        self.first_ts = None
        self.last_ts = None
        self.git_branch = None
        self.ai_title = None
        self.pending = {}               # tool_use_id -> {name, input, uuid} awaiting a result
        self.convo = deque(maxlen=48)   # recent turns + key-tool calls
        self.convo_rev = 0              # bumps on ANY convo change (results mutate in place)
        self.files = deque(maxlen=10)   # SendUserFile deliveries: {path, caption, ts}
        self._tool_refs = {}            # tool_use_id -> convo entry (for result attach)

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
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        self.pending[b["id"]] = {"name": b.get("name"),
                                                 "input": b.get("input"), "uuid": o.get("uuid")}
                        if b.get("name") == "SendUserFile":
                            inp = b.get("input") or {}
                            for fp in (inp.get("files") or [])[:6]:
                                if isinstance(fp, str):
                                    self._file_add(fp, inp.get("caption", ""), ts)
                        if b.get("name") in KEY_TOOLS:
                            self._tool_add(b, ts)
                txt = "\n\n".join(b.get("text", "") for b in content
                                  if isinstance(b, dict) and b.get("type") == "text").strip()
                if txt:
                    self._convo_add("assistant", txt, ts)
            if m.get("stop_reason") in ("end_turn", "stop_sequence"):
                self.pending.clear()    # turn over: unanswered tool_uses were canceled
            self.last_shape = ("assistant", m.get("stop_reason"), ctypes)
        elif role == "user":
            kind = "tool_result" if "tool_result" in ctypes else "prompt"
            if kind == "tool_result" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self.pending.pop(b.get("tool_use_id"), None)
                        ref = self._tool_refs.pop(b.get("tool_use_id"), None)
                        if ref is not None:
                            ref["result"] = self._result_summary(b, ref.get("name"))
                            self.convo_rev += 1
            elif kind == "prompt":
                self.pending.clear()    # new user turn
                if not o.get("isMeta"):
                    if isinstance(content, str):
                        utxt = content
                    else:
                        utxt = "\n\n".join(b.get("text", "") for b in content
                                           if isinstance(b, dict) and b.get("type") == "text")
                    utxt = re.sub(r"<system-reminder>.*?</system-reminder>", "", utxt, flags=re.S).strip()
                    if utxt and not utxt.startswith(("<command-", "<local-command", "Caveat:",
                                                     "This session is being continued from")):
                        self._convo_add("user", utxt, ts)
            self.last_shape = ("user", kind, ctypes)

    def _tool_add(self, b, ts):
        name, inp = b.get("name"), b.get("input") or {}
        entry = {"role": "tool", "name": name, "ts": ts}
        if name == "SendUserFile":
            entry["files"] = [p for p in (inp.get("files") or [])[:6] if isinstance(p, str)]
            entry["caption"] = inp.get("caption", "")
        else:
            entry["arg"] = self._tool_arg(name, inp)
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

    def _file_add(self, path, caption, ts):
        for f in self.files:
            if f["path"] == path:       # re-delivery: refresh, don't duplicate
                f["ts"] = ts
                f["caption"] = caption or f["caption"]
                return
        self.files.append({"path": path, "caption": caption or "", "ts": ts})

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
        self.tails = {}                 # path -> Tail
        self.velocity = {}              # path -> deque[(t, total_tokens)]
        self.db = None
        self.notified = {}              # dedupe keys -> t
        self.seeded = False             # first pass registers pre-existing states silently
        self.prev_fleet_busy = None
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()   # tails are stateful; one folder at a time
        self.snapshot_cache = {}

    # -- live sessions from the CLI registry
    def live_sessions(self):
        out = []
        for p in glob.glob(os.path.join(SESSIONS, "*.json")):
            try:
                d = json.load(open(p))
                os.kill(d["pid"], 0)
            except Exception:
                continue
            out.append(d)
        return out

    def tail_for(self, path):
        t = self.tails.get(path)
        if t is None:
            t = self.tails[path] = Tail(path)
        return t

    def scan(self):
        with self.scan_lock:
            return self._scan()

    def _scan(self):
        cfg = self.cfg
        now = time.time()
        sessions = []
        for reg in self.live_sessions():
            sid = reg.get("sessionId")
            cwd = reg.get("cwd", "")
            proj_dir = cwd_to_project_dir(cwd)
            main_path = os.path.join(proj_dir, f"{sid}.jsonl")
            if not os.path.isfile(main_path):
                continue
            mt = self.tail_for(main_path)
            mt.poll()
            mtime = os.path.getmtime(main_path)
            quiet = now - mtime

            reg_status = reg.get("status")  # 'busy' | 'idle' | 'waiting' | None
            # parent turn over → a frozen agent is canceled, not mid-tool
            parent_idle = reg_status in ("idle", "waiting")
            agents = self.scan_agents(os.path.join(proj_dir, sid, "subagents"), now, parent_idle)
            # long tool calls freeze an agent's transcript ("stalled"); still active
            agents_running = [a for a in agents if a["state"] in ("running", "stalled")]

            turn = mt.turn_state()
            if reg_status == "waiting":
                state = "needs_you"         # blocked mid-turn: question or permission prompt
            elif reg_status == "idle" or (reg_status is None and turn == "awaiting_input"):
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
            pending = self.hook_pending(sid, reg_status)
            if pending is None:
                for tid, p in mt.pending.items():
                    if p["name"] == "AskUserQuestion":
                        qs = (p.get("input") or {}).get("questions", [])
                        pending = {"kind": "question", "nonce": tid, "questions": qs}
                        break
            if pending is None and reg_status == "waiting" and mt.pending:
                tid, p = list(mt.pending.items())[-1]
                pending = {"kind": "permission", "nonce": tid, "tool": p["name"],
                           "input_summary": json.dumps(p.get("input"), indent=1)[:1500]}
            if pending and pending["nonce"] not in self.notified:
                self.notified[pending["nonce"]] = now
                print(f"pending first seen: {sid[:8]} {pending['kind']} nonce={pending['nonce'][:24]}",
                      file=sys.stderr, flush=True)

            ctx = mt.context_tokens()
            fam = model_family(mt.model)
            cw = cfg["context_windows"].get(fam, cfg["context_windows"]["default"])
            sessions.append({
                "session_id": sid,
                "pid": reg.get("pid"),
                "name": reg.get("name"),
                "title": mt.ai_title,
                "project": os.path.basename(cwd) or cwd,
                "cwd": cwd,
                "branch": mt.git_branch,
                "model": mt.model, "family": fam,
                "state": state,
                "reg_status": reg_status,
                "quiet_s": round(quiet),
                "ctx_tokens": ctx, "ctx_pct": round(100 * ctx / cw, 1) if cw else None,
                "cost": round(mt.cost(cfg), 4),
                "bridge_url": (f"https://claude.ai/code/{reg['bridgeSessionId']}"
                               if reg.get("bridgeSessionId") else None),
                "started_ms": reg.get("startedAt"),
                "pending": pending,
                # cache keys: the page refetches /api/context only when these move
                # (a rev counter, not last-ts: tool results mutate entries in place)
                "convo_v": mt.convo_rev,
                "files_n": len(mt.files),
                "agents": agents,
                "agents_running": len(agents_running),
                "agents_total": len(agents),
                "agent_cost": round(sum(a["cost"] for a in agents), 4),
            })
        order = {"needs_you": 0, "stalled": 0, "stalled_or_prompt": 0, "turn_done": 1,
                 "running": 2, "idle": 3, "dormant": 4}
        sessions.sort(key=lambda s: (order.get(s["state"], 2), -s["cost"]))
        self.record_sessions(sessions, now)
        fleet = {
            "t": now,
            "sessions": sessions,
            "totals": {
                "sessions": len(sessions),
                "busy": sum(1 for s in sessions if s["state"] == "running"),
                "needs_me": sum(1 for s in sessions if s["state"] in ("needs_you", "stalled", "stalled_or_prompt")),
                "done": sum(1 for s in sessions if s["state"] == "turn_done"),
                "agents_running": sum(s["agents_running"] for s in sessions),
                "session_cost": round(sum(s["cost"] for s in sessions), 2),
                "agent_cost": round(sum(s["agent_cost"] for s in sessions), 2),
            },
            "rollup": self.rollup(),
            "closed": self.closed_sessions(),
        }
        with self.lock:
            self.snapshot_cache = fleet
        return fleet

    def scan_agents(self, subdir, now, parent_idle=False):
        out = []
        cfg = self.cfg
        for meta_path in glob.glob(os.path.join(subdir, "*.meta.json")):
            agent_id = os.path.basename(meta_path)[:-len(".meta.json")]
            jl = os.path.join(subdir, agent_id + ".jsonl")
            if not os.path.isfile(jl):
                continue
            try:
                meta = json.load(open(meta_path))
            except Exception:
                meta = {}
            t = self.tail_for(jl)
            grew = t.poll()
            mtime = os.path.getmtime(jl)
            quiet = now - mtime

            done = (t.last_shape and t.last_shape[0] == "assistant"
                    and t.last_shape[1] in ("end_turn", "stop_sequence")
                    and quiet > cfg["agent_done_quiet_seconds"])
            state = "done" if done else ("stalled" if quiet > cfg["stall_seconds"] else "running")
            if not done and parent_idle and quiet > 2 * cfg["agent_done_quiet_seconds"]:
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
            })
            if done:
                self.ledger_finalize(subdir, agent_id, meta, t)
        out.sort(key=lambda a: (a["state"] in ("done", "ended"), a["started"] or ""))
        return out

    # -------------------------------------------------------------- ledger
    def ensure_db(self):
        if self.db is None:
            # single sequential user (the poll loop; main thread only pre-loop)
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
        return self.db

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
            for s in sessions:
                db.execute("""INSERT INTO session_runs(session_id,name,project,cwd,branch,
                    model,cost,agent_cost,agents_total,bridge_url,first_seen,last_seen,closed_at,title)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                    name=excluded.name, project=excluded.project, branch=excluded.branch,
                    model=excluded.model, cost=excluded.cost, agent_cost=excluded.agent_cost,
                    agents_total=excluded.agents_total, bridge_url=excluded.bridge_url,
                    last_seen=excluded.last_seen, closed_at=NULL, title=excluded.title""",
                    (s["session_id"], s["name"], s["project"], s["cwd"], s["branch"],
                     s["model"], s["cost"], s["agent_cost"], s["agents_total"],
                     s["bridge_url"], int(now), int(now), s["title"]))
            live = [s["session_id"] for s in sessions]
            marks = ",".join("?" * len(live)) or "''"
            db.execute(f"""UPDATE session_runs SET closed_at=?
                           WHERE closed_at IS NULL AND session_id NOT IN ({marks})""",
                       [int(now)] + live)
            db.commit()
        except Exception as e:
            print(f"session ledger error: {e}", file=sys.stderr, flush=True)

    def closed_sessions(self, limit=20):
        cols = ("session_id", "name", "project", "branch", "cost", "agent_cost",
                "agents_total", "bridge_url", "first_seen", "closed_at", "title")
        try:
            rows = self.ensure_db().execute(
                f"""SELECT {','.join(cols)} FROM session_runs
                    WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?""",
                (limit,)).fetchall()
            return [dict(zip(cols, r)) for r in rows]
        except Exception:
            return []

    def rollup(self):
        try:
            db = self.ensure_db()
            rows = db.execute("""SELECT date(started) d, agent_type, model, count(*), sum(cost)
                                 FROM agent_runs WHERE started >= date('now','-7 days')
                                 GROUP BY d, agent_type, model ORDER BY d DESC, sum(cost) DESC""").fetchall()
            return [{"day": r[0], "agent_type": r[1], "model": r[2], "runs": r[3],
                     "cost": round(r[4] or 0, 3)} for r in rows]
        except Exception:
            return []

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
            return {"kind": "question", "nonce": d["nonce"], "questions": d.get("questions", [])}
        if d.get("kind") == "permission":
            return {"kind": "permission", "nonce": d["nonce"], "tool": "requested tool",
                    "input_summary": d.get("message", "")}
        return None

    # ------------------------------------------------------- context + files
    def _reg_main_path(self, sid):
        reg = next((r for r in self.live_sessions() if r.get("sessionId") == sid), None)
        if not reg:
            return None, None
        return reg, os.path.join(cwd_to_project_dir(reg.get("cwd", "")), f"{sid}.jsonl")

    def session_context(self, sid):
        """Recent conversation turns + SendUserFile deliveries for one session."""
        reg, path = self._reg_main_path(sid)
        if not reg or not os.path.isfile(path):
            return {"ok": False, "error": "session not live"}
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

    def file_content(self, sid, fpath):
        """Serve a delivered file. WHITELIST: only paths recorded from this session's
        own SendUserFile tool_use rows — never a free-form client path."""
        reg, path = self._reg_main_path(sid)
        if not reg:
            return None, None, "session not live"
        with self.scan_lock:
            mt = self.tail_for(path)
            mt.poll()
            allowed = {f["path"] for f in mt.files}
            for m in mt.convo:          # inline chips can outlive the files deque
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
    def act(self, action):
        """Inject an answer into the owning iTerm session. action:
        {type:'option', session_id, nonce, digits:[1,..]} |
        {type:'permission', session_id, nonce, choice:'allow'|'always'|'deny'} |
        {type:'text', session_id, text:'...'}"""
        if action.get("type") == "ping":     # token check for the page's acting banner
            return {"ok": True}
        sid = action.get("session_id")
        reg = next((r for r in self.live_sessions() if r.get("sessionId") == sid), None)
        if not reg:
            return {"ok": False, "error": "session not live"}
        path = os.path.join(cwd_to_project_dir(reg.get("cwd", "")), f"{sid}.jsonl")
        with self.scan_lock:            # freshness check against the live transcript
            mt = self.tail_for(path)
            mt.poll()
            typ = action.get("type")
            steps = []                  # [(text, send_newline)]
            if typ in ("option", "permission"):
                nonce = action.get("nonce")
                hp = self.hook_pending(sid, reg.get("status"))
                if not ((hp and hp.get("nonce") == nonce) or nonce in mt.pending):
                    return {"ok": False, "error": "stale: the prompt changed — refresh"}
                if typ == "option":
                    digits = [str(int(d)) for d in action.get("digits", [])][:8]
                    if not digits:
                        return {"ok": False, "error": "no option chosen"}
                    steps = [(d, False) for d in digits]
                    if action.get("multi"):
                        # digits toggle; Enter toggles too. Submitting = right-arrow
                        # to the TUI's "✔ Submit" tab, then Enter.
                        steps.append(("\x1b[C", False))
                    steps.append(("", True))
                else:
                    key = self.cfg.get("permission_keys", {}).get(action.get("choice"))
                    if not key:
                        return {"ok": False, "error": "unknown choice"}
                    steps = [(key, False)]
                    if key not in ("\x1b",):
                        steps.append(("", True))
            elif typ == "noop":         # TCC/AppleScript path probe: delivers nothing
                steps = [("", False)]
            elif typ == "text":
                txt = str(action.get("text", ""))[:2000].strip()
                if not txt:
                    return {"ok": False, "error": "empty text"}
                steps = [(txt, True)]
            else:
                return {"ok": False, "error": "unknown action type"}
        try:
            tty = subprocess.run(["ps", "-p", str(reg["pid"]), "-o", "tty="],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception as e:
            return {"ok": False, "error": f"tty lookup failed: {e}"}
        if not tty or tty == "??":
            return {"ok": False, "error": "session has no terminal (VS Code / headless)"}
        return self._iterm_write(f"/dev/{tty}", steps)

    def _iterm_write(self, tty, steps):
        # launchd-context osascript can never summon the automation-permission
        # dialog (hangs forever), so injection runs through the FleetDashInjector
        # applet: request file -> open -g applet -> result file. The applet has its
        # own TCC identity and prompts normally on first use.
        import base64
        req_id = secrets.token_hex(8)
        lines = [tty, req_id]
        for text, nl in steps:
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
            time.sleep(0.3)
        return {"ok": False, "error": "injector timed out — if a macOS permission "
                "dialog appeared, grant it and retry"}

    # ---------------------------------------------------------------- ntfy
    def ntfy(self, title, body, tags="robot", priority="default"):
        topic = self.cfg.get("ntfy_topic")
        if not topic:
            return
        url = f"{self.cfg['ntfy_server'].rstrip('/')}/{topic}"
        req = urllib.request.Request(url, data=body.encode(), method="POST",
                                     headers={"Title": title, "Tags": tags, "Priority": priority})
        threading.Thread(target=lambda: self._post(req), daemon=True).start()

    def _post(self, req):
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def check_notifications(self, fleet):
        cfg, now = self.cfg, time.time()
        for s in fleet["sessions"]:
            key_base = s["session_id"][:8]
            if s["state"] == "stalled" and s["quiet_s"] > cfg["stall_seconds"]:
                self.once(f"stall:{key_base}:{s['quiet_s'] // 300}", "Session stalled",
                          f"{s['name']}: frozen {s['quiet_s']}s mid-turn", "warning", "high")
            if s["state"] == "needs_you" and s["quiet_s"] > cfg["awaiting_input_notify_seconds"]:
                self.once(f"await:{key_base}:{int(s['quiet_s']) // 1800}", "Waiting on you",
                          f"{s['name']}: blocked {s['quiet_s'] // 60}m", "hourglass_flowing_sand")
            mult = int((s["cost"] + s["agent_cost"]) / cfg["spend_threshold_usd"])
            if mult >= 1:               # only the highest crossed threshold, once
                self.once(f"spend:{key_base}:{mult}", "Spend threshold",
                          f"{s['name']}: ${s['cost'] + s['agent_cost']:.2f} "
                          f"(crossed ${cfg['spend_threshold_usd'] * mult:.0f})", "moneybag", "high")
        busy = fleet["totals"]["busy"] + fleet["totals"]["agents_running"]
        if self.prev_fleet_busy and busy == 0 and fleet["totals"]["sessions"] > 0:
            self.once(f"quiet:{int(now) // 600}", "Fleet quiet",
                      f"All {fleet['totals']['sessions']} sessions idle — come harvest", "white_check_mark")
        self.prev_fleet_busy = busy
        self.seeded = True

    def once(self, key, title, body, tags="robot", priority="default"):
        if key in self.notified:
            return
        self.notified[key] = time.time()
        if len(self.notified) > 5000:
            self.notified.clear()
        if self.seeded:                 # first pass: register only, no push
            self.ntfy(title, body, tags, priority)


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
