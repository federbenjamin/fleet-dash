"""Conversation/file/context projections, hook pending, effort, commands
(invariants 1, 10, 11, 43)."""
import json, os, re, sys, glob, time, copy


from . import paths as pathcfg
from .paths import capture_base  # legacy alias; reads paths.* at call time
from .config import CLAUDE_EFFORTS
from .config import (IMG_EXTS, DANGER_COMMANDS, BUILTIN_COMMANDS, model_family, usd, cwd_to_project_dir, iso_epoch)




class ContextOps:

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
        path = os.path.join(capture_base(), "pending", f"{sid}.json")
        try:
            with open(path) as handle:
                d = json.load(handle)
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
        for root in (os.path.join(cwd, ".claude"), os.path.join(pathcfg.HOME, ".claude")):
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

    def _transcript_effort(self, sid):
        """Live effort from the session's own transcript.

        Claude Code ≥2.1.217 stamps the live effort level onto every assistant
        row (top-level `effort`); Tail folds it with byte-offset evidence
        order. Returns (value or None, evidence byte offset)."""
        reg, path = self._reg_main_path(sid)
        if not path:
            return None, 0
        mt = self.tails.get(path)
        if mt is None:
            return None, 0
        v = getattr(mt, "effort", "") or ""
        return (v if v in CLAUDE_EFFORTS else None,
                int(getattr(mt, "effort_evidence_offset", 0) or 0))

    def effort_for(self, sid):
        """Effort level ('high', 'max', …) for a session.

        Primary source: transcript assistant rows (Claude Code ≥2.1.217 stamps
        the live effort onto each one). Legacy fallback: the statusline
        side-write file (`fleet-dash effort side-write` block), kept for older
        Claude builds. A Fleet-issued accepted /effort override wins until
        strictly newer native evidence arrives (invariant 65): a transcript
        effort row at a byte offset past the acceptance baseline, or a newer
        statusline side-write mtime."""
        t_eff, t_off = self._transcript_effort(sid)
        s_eff, s_mtime = None, 0.0
        path = os.path.join(capture_base(), "effort", sid)
        try:
            st = os.stat(path)
            with open(path) as f:
                v = f.read().strip()
            if v in CLAUDE_EFFORTS:
                s_eff, s_mtime = v, st.st_mtime
        except OSError:
            pass
        override = self._claude_effort_overrides.get(sid)
        if override:
            value, accepted_at = override
            entry = (self._claude_control_overrides.get(sid) or {}).get("effort") or {}
            baseline = int(entry.get("baseline", 0) or 0)
            if t_eff and baseline > 0 and t_off > baseline:
                self._retire_claude_control_override(sid, "effort")
                return t_eff
            if s_eff and s_mtime > accepted_at:
                self._retire_claude_control_override(sid, "effort")
                return s_eff
            return value
        return t_eff or s_eff

    def compacting_secs(self, sid, cwd, mt):
        """Seconds a compaction has been running, or None.

        The transcript is SILENT during a compaction: the whole block (the
        /compact command rows AND the boundary) is flushed only when it
        finishes, so 'issued but no boundary yet' is undetectable there. The
        PreCompact hook's checkpoint file is the one live artifact — its mtime
        is the compaction's start. Sessions whose project has no PreCompact
        hook simply never show the pill (the finished-event row still lands)."""
        p = os.path.join(pathcfg.HOME, ".claude", "compaction",
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
        scan_dir(os.path.join(pathcfg.HOME, ".claude"), "user")
        try:
            with open(os.path.join(pathcfg.HOME, ".claude", "plugins",
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

    @staticmethod
    def _claude_file_backup(sid, backup_name):
        """Resolve only Claude's transcript-declared backup for this exact UUID.

        The client never supplies backup_name. Path confinement here is still
        load-bearing because transcript rows are untrusted input.
        """
        if not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", str(sid or "")):
            return None
        if not re.fullmatch(r"[0-9a-f]{8,64}@v[0-9]{1,8}", str(backup_name or "")):
            return None
        root = os.path.realpath(os.path.join(pathcfg.HOME, ".claude", "file-history", str(sid)))
        candidate = os.path.realpath(os.path.join(root, str(backup_name)))
        if os.path.dirname(candidate) != root or not os.path.isfile(candidate):
            return None
        return candidate

    def session_context(self, sid):
        """Recent conversation turns + SendUserFile deliveries for one session."""
        if str(sid).startswith("codex:"):
            with self.lock:
                known = any(item.get("session_id") == sid and
                            item.get("provider") == "codex"
                            for item in self.snapshot_cache.get("sessions") or [])
            if not known:
                return {"ok": False, "error": "unknown session"}
            return self._project_file_ids(sid, self.codex.context(sid))
        reg, path = self._reg_main_path(sid)
        if not reg:
            return {"ok": False, "error": "session not live"}
        if not os.path.isfile(path):
            return {"ok": True, "messages": [], "files": [], "starting": True}
        snapshot = self._claude_context_snapshots.get(sid)
        if snapshot is not None:
            msgs = copy.deepcopy(snapshot.get("messages") or [])
            files = copy.deepcopy(snapshot.get("files") or [])
            file_backups = dict(snapshot.get("file_backups") or {})
        else:
            # Startup/test fallback before the first completed scan publishes
            # this session. Stateful folding remains serialized.
            with self.scan_lock:
                mt = self.tail_for(path)
                mt.poll()
                msgs = [dict(m) for m in mt.convo]
                files = [dict(f) for f in mt.files]
                file_backups = dict(mt.file_backups)
        def fmeta(p):
            backup = self._claude_file_backup(sid, file_backups.get(p))
            return {"name": os.path.basename(p),
                    "file_id": self.file_id(sid, p),
                    "kind": "image" if os.path.splitext(p)[1].lower() in IMG_EXTS else "text",
                    "missing": not os.path.isfile(p) and backup is None}
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
        agent = next((dict(item) for item in (parent or {}).get("agents") or []
                      if item.get("agent_id") == aid), None)
        if not parent and str(sid).startswith("codex:"):
            if not any(item.get("session_id") == sid for item in self.closed_sessions()):
                return {"ok": False, "error": "no such subagent"}
            return self.codex.agent_context(sid, aid)
        if not parent:
            row = next((item for item in self.closed_sessions()
                        if item.get("session_id") == sid), None)
            path = self._safe_claude_transcript(
                sid, (row or {}).get("transcript_path")) if row else None
            catalog = self._closed_claude_agents(sid, path) if path else []
            agent = next((item for item in catalog if item.get("agent_id") == aid), None)
            if not agent:
                return {"ok": False, "error": "no such saved subagent"}
            root = os.path.realpath(os.path.join(os.path.dirname(path), str(sid), "subagents"))
            transcript = os.path.realpath(os.path.join(root, str(aid) + ".jsonl"))
            if os.path.dirname(transcript) != root or not os.path.isfile(transcript):
                return {"ok": False, "error": "saved subagent transcript is gone"}
            with self.scan_lock:
                tail = self.tail_for(transcript)
                tail.poll()
                messages = [dict(message) for message in tail.convo]
            return {"ok": True, "messages": messages,
                    "info": {**agent, "status_line": None}, "closed": True}
        if not agent:
            return {"ok": False, "error": "no such subagent"}
        if str(sid).startswith("codex:"):
            result = self.codex.agent_context(sid, aid)
            if result.get("ok"):
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
                    "effort": (t.effort or
                               self.agent_effort(meta.get("agentType"), reg.get("cwd", ""),
                                                 self.effort_for(sid))),
                    "tokens": {"in": t.ti, "cache_write": t.tw,
                               "cache_read": t.tr, "out": t.to},
                    "total_tokens": t.total_tokens, "cost": round(t.cost(self.cfg), 4),
                    "started": t.first_ts, "last": t.last_ts}
            info["state"] = agent.get("state")
            if parent:
                info["status_line"] = self.agent_status_line(parent, info, t)
        return {"ok": True, "messages": msgs, "info": info}

    def file_selector_for_path(self, sid, raw_path):
        """Return an opaque selector only when ``raw_path`` belongs to ``sid``."""
        selector = self.file_id(sid, raw_path)
        if str(sid).startswith("codex:"):
            with self.lock:
                known = any(item.get("session_id") == sid and
                            item.get("provider") == "codex"
                            for item in self.snapshot_cache.get("sessions") or [])
            if not known:
                db = None
                try:
                    db = self.ledger_reader()
                    known = db.execute("""SELECT 1 FROM session_runs
                        WHERE session_id=? AND provider='codex'
                        AND closed_at IS NOT NULL""", (sid,)).fetchone() is not None
                except Exception:
                    known = False
                finally:
                    if db is not None:
                        db.close()
            if not known:
                return None
            context = self.codex.context(sid)
            return selector if any(item.get("path") and
                self.file_id(sid, item["path"]) == selector
                for item in context.get("files") or []) else None
        reg, path = self._reg_main_path(sid)
        closed = False
        if not reg:
            row = next((item for item in self.closed_sessions()
                        if item.get("session_id") == sid and item.get("provider") == "claude"), None)
            path = self._safe_claude_transcript(sid, (row or {}).get("transcript_path"))
            if not path:
                return None
            closed = True
        snapshot = None if closed else self._claude_context_snapshots.get(sid)
        if snapshot is not None:
            files = snapshot.get("files") or []
            messages = snapshot.get("messages") or []
            delivered = snapshot.get("delivered_paths") or {}
        else:
            with self.scan_lock:
                mt = self.tail_for(path)
                mt.poll()
                files = list(mt.files)
                messages = list(mt.convo)
                delivered = dict(mt.delivered_paths)
        allowed = {f["path"] for f in files}
        for m in messages:              # inline chips can outlive the files deque
            if m.get("role") == "tool":
                allowed.update(p for p in m.get("files") or [] if isinstance(p, str))
        # The durable delivery whitelist outlives both ring buffers, so an old
        # delivery stays selectable (its file-history backup may still resolve
        # it). Delivery-derived only — never the backup mapping (invariant 10).
        allowed.update(p for p in delivered if isinstance(p, str))
        return selector if any(self.file_id(sid, path) == selector for path in allowed) else None

    def file_content(self, sid, file_id):
        """Serve a session-owned file selected only by an opaque projected ID."""
        selector = str(file_id or "")
        if not re.fullmatch(r"[0-9a-f]{24}", selector):
            return None, None, "invalid file selector"
        if str(sid).startswith("codex:"):
            with self.lock:
                known = any(item.get("session_id") == sid and
                            item.get("provider") == "codex"
                            for item in self.snapshot_cache.get("sessions") or [])
            if not known:
                db = None
                try:
                    db = self.ledger_reader()
                    known = db.execute("""SELECT 1 FROM session_runs
                        WHERE session_id=? AND provider='codex'
                        AND closed_at IS NOT NULL""", (sid,)).fetchone() is not None
                except Exception:
                    known = False
                finally:
                    if db is not None:
                        db.close()
            if not known:
                return None, None, "unknown session"
            context = self.codex.context(sid)
            fpath = next((item.get("path") for item in context.get("files") or []
                          if item.get("path") and self.file_id(sid, item["path"]) == selector),
                         None)
            if not fpath:
                return None, None, "not a file this Codex thread changed or generated"
            return self.codex.file_content(sid, fpath)
        reg, path = self._reg_main_path(sid)
        closed = False
        if not reg:
            row = next((item for item in self.closed_sessions()
                        if item.get("session_id") == sid and item.get("provider") == "claude"), None)
            path = self._safe_claude_transcript(sid, (row or {}).get("transcript_path"))
            if not path:
                return None, None, "session is unavailable"
            closed = True
        snapshot = None if closed else self._claude_context_snapshots.get(sid)
        if snapshot is not None:
            files = snapshot.get("files") or []
            messages = snapshot.get("messages") or []
            file_backups = snapshot.get("file_backups") or {}
            delivered = snapshot.get("delivered_paths") or {}
        else:
            with self.scan_lock:
                mt = self.tail_for(path)
                mt.poll()
                files = list(mt.files)
                messages = list(mt.convo)
                file_backups = dict(mt.file_backups)
                delivered = dict(mt.delivered_paths)
        allowed = {f["path"] for f in files}
        for m in messages:              # inline chips can outlive the files deque
            if m.get("role") == "tool":
                allowed.update(p for p in m.get("files") or [] if isinstance(p, str))
        allowed.update(p for p in delivered if isinstance(p, str))
        fpath = next((path for path in allowed if self.file_id(sid, path) == selector), None)
        if not fpath:
            return None, None, "not a file this session delivered"
        source_path = fpath if os.path.isfile(fpath) else self._claude_file_backup(
            sid, file_backups.get(fpath))
        if not source_path:
            return None, None, "unreadable: delivered file and Claude backup are gone"
        try:
            if os.path.getsize(source_path) > 8_000_000:
                return None, None, "file too large to preview (>8MB)"
            with open(source_path, "rb") as f:
                data = f.read()
        except OSError as e:
            return None, None, f"unreadable: {e}"
        ext = os.path.splitext(fpath)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".pdf": "application/pdf",
                 ".json": "application/json; charset=utf-8"}.get(
                     ext, "text/plain; charset=utf-8")
        # HTML deliberately stays text/plain. The client fetches and places it
        # into a sandboxed, CSP-locked srcdoc; navigating /api/file directly
        # must never execute a delivered document in Fleet's authenticated origin.
        return ctype, data, None
