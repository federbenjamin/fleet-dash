"""Conversation/file/context projections, hook pending, effort, commands
(invariants 1, 10, 11, 43)."""
import json, os, re, sys, glob, time, copy, hashlib, uuid
from collections import deque


from . import paths as pathcfg
from .paths import capture_base  # legacy alias; reads paths.* at call time
from . import screen as screenlib
from .config import CLAUDE_EFFORTS
from .tail import Tail
from .config import (IMG_EXTS, DANGER_COMMANDS, BUILTIN_COMMANDS, model_family, usd, cwd_to_project_dir, iso_epoch)




class ContextOps:

    # How long a hook-captured question keeps rendering while the registry is not
    # `waiting`. It is a grace period for Claude's status flicker, not a decision:
    # `act()` refuses to answer any prompt on a non-waiting session regardless.
    GHOST_QUESTION_GRACE = 45
    # A question capture is cleared by PostToolUse, so it only outlives its ask
    # when the session died mid-prompt. This collects those, nothing else.
    STALE_CAPTURE_SECONDS = 1800
    # How long an accepted answer keeps its own prompt from re-rendering while
    # the provider catches up. PostToolUse clears a question capture on
    # resolution and a permission capture expires 15s after `waiting` ends, so
    # this only has to outlast the slower of those two.
    ANSWERED_FENCE_SECONDS = 20
    # Bounded identity/fence maps; sessions come and go, so cap the retained set.
    REQUEST_IDENTITY_LIMIT = 500
    # How still a busy session's transcript has to be before its pane is worth a
    # look. A compaction freezes the transcript completely (invariant 17) while a
    # working turn writes constantly, so this keeps the eligible set near empty.
    SCREEN_WATCH_QUIET_SECONDS = 8

    @staticmethod
    def _discard_capture(path):
        try:
            os.remove(path)
        except OSError:
            pass

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
        age = time.time() - d.get("ts", 0)
        drop = lambda: (self._discard_capture(path), None)[1]
        if d.get("kind") == "permission":
            # A permission Notification has NO clear-event, so age is the only
            # way it ever goes away: expire it once the session stops waiting.
            if reg_status != "waiting" and age > 15:
                return drop()
            return {"kind": "permission", "nonce": d["nonce"], "tool": "requested tool",
                    "input_summary": d.get("message", "")}
        if d.get("kind") == "question":
            # A question capture DOES have a clear-event — PostToolUse removes it
            # on resolution — so age is not evidence of staleness and must not
            # delete it. Only a dead session leaves one behind, which the long
            # hard expiry below collects.
            if age > self.STALE_CAPTURE_SECONDS:
                return drop()
            # Ghost guard: a PreToolUse capture can outlive an ask another hook
            # BLOCKED, and injected digits would then type into the session's main
            # input box (invariant 5). Claude's registry also flashes non-waiting
            # between the capture and the ask actually opening, so this is a grace
            # period, not a decision.
            # Widened from 5s: the transcript fallback that used to re-surface a
            # dropped capture is gone (invariant 1), so a guard firing early now
            # loses the question outright instead of showing it again under a
            # second identity. `act()` still refuses any prompt answer whose
            # registry status is not `waiting`, so a capture that renders past its
            # welcome still cannot be answered.
            # A look at the terminal beats both the registry word and the clock
            # (invariant 78). The label is at most one observation window old, so
            # it settles the grace period rather than replacing the timing rules:
            # a rendered question survives any age, and a session demonstrably
            # back at its input box is a ghost NOW, not in 45 seconds.
            observed = self.observed_screen(sid)
            if observed == "question":
                return {"kind": "question", "nonce": d["nonce"],
                        "questions": d.get("questions", []), "_ts": d.get("ts")}
            if observed in ("input", "trust") and reg_status != "waiting":
                return None
            if reg_status != "waiting" and age > self.GHOST_QUESTION_GRACE:
                return None
            return {"kind": "question", "nonce": d["nonce"], "questions": d.get("questions", []),
                    "_ts": d.get("ts")}
        return None

    # How long a screen-derived permission keeps its nonce once the pane stops
    # showing one. Short: the only thing it has to outlast is the gap between an
    # observation and the poll that notices the prompt is gone.
    SCREEN_PROMPT_GRACE = 20

    def _screen_permission(self, sid, now):
        """A permission prompt Fleet can see but has no capture for yet.

        Measured on a rig 2026-07-25: the prompt is on the pane at t+4.5s and the
        Notification hook lands at t+10.6s, with the transcript holding nothing in
        between — so for six seconds Fleet knew a session was waiting and could
        not say what for.

        The nonce is server-minted and RETAINED, because `act()` accepts an answer
        only for a nonce this scan issued: a client cannot invent one. It is not a
        Claude identifier and never reaches the terminal — the digits do, and only
        after invariant 77's classifier confirms, at write time, that the pane is
        still rendering a permission prompt. That check is the real gate here; the
        nonce exists so the answered-fence and the client's suppression have
        something stable to key on (invariant 75).
        """
        if self.observed_screen(sid) != screenlib.PERMISSION:
            self._screen_prompts.pop(sid, None)
            return None
        record = self._screen_prompts.get(sid)
        if not record or now - record["at"] > self.SCREEN_PROMPT_GRACE:
            # The counter is what makes this an identity rather than a timestamp:
            # a prompt that closes and reopens inside the same millisecond would
            # otherwise reuse its nonce, and the answered fence keys on it.
            self._screen_prompt_seq = getattr(self, "_screen_prompt_seq", 0) + 1
            record = {"nonce": f"screen-{int(now * 1000)}-{self._screen_prompt_seq}",
                      "at": now}
            if len(self._screen_prompts) > self.REQUEST_IDENTITY_LIMIT:
                self._screen_prompts.clear()
        else:
            record = {**record, "at": now}
        self._screen_prompts[sid] = record
        # `source` tells the client this prompt was read off the terminal rather
        # than attested by a hook, so it can say so and fetch the real option
        # rows on demand — the scan keeps a label, never screen text (invariant 78).
        return {"kind": "permission", "nonce": record["nonce"], "source": "screen",
                "tool": "requested tool",
                "input_summary": ""}

    def _screen_prompt_nonce(self, sid):
        """The screen-derived nonce this scan issued for `sid`, if any."""
        record = self._screen_prompts.get(sid)
        return record["nonce"] if record else None

    # ------------------------------------------------- request identity (75)
    @staticmethod
    def _pending_source(nonce):
        """Which evidence produced this nonce. The hook stamps its own prefix;
        anything else is a transcript `tool_use_id`."""
        return "hook" if str(nonce or "").startswith("hook-") else "transcript"

    @staticmethod
    def _pending_signature(pending):
        """Content fingerprint of one prompt, comparable only WITHIN a source.

        A hook permission capture carries Claude's notification message while the
        transcript carries a tool name and JSON input — the same prompt, no
        shared text. Cross-source matching is handled by `_request_identity`."""
        if pending.get("kind") == "question":
            parts = []
            questions = pending.get("questions")
            for question in questions if isinstance(questions, list) else []:
                if not isinstance(question, dict):
                    parts.append(str(question))
                    continue
                parts.append(str(question.get("question", "")))
                options = question.get("options")
                for option in options if isinstance(options, list) else []:
                    parts.append(str(option.get("label", "")
                                     if isinstance(option, dict) else option))
            raw = "\x00".join(parts)
        else:
            raw = f"{pending.get('tool', '')}\x00{pending.get('input_summary', '')}"
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]

    def _request_identity(self, sid, pending, now):
        """Stable server-owned id for the prompt currently open on `sid`.

        One prompt has up to two nonces: the hook capture's, and — for
        permissions, whose captures have no clear-event — the transcript
        `tool_use_id` the fallback uses once the capture ages out. The client
        suppresses an answered prompt by identity, so a nonce flip used to make
        an answered prompt reappear under a second identity. The id below
        survives the flip and is retired when the prompt goes away."""
        nonce = str(pending.get("nonce") or "")
        kind = pending.get("kind") or ""
        source = self._pending_source(nonce)
        signature = self._pending_signature(pending)
        with self._request_identity_guard:
            record = self._request_ids.get(sid)
            same = bool(record) and record["kind"] == kind and (
                nonce in record["nonces"] or
                record["signatures"].get(source) == signature or
                # First sighting through this prompt's OTHER evidence source
                # while it stayed continuously open: the flip case. Content is
                # not comparable across sources, so continuity is the evidence.
                source not in record["signatures"])
            if not same:
                record = {"request_id": f"req-{uuid.uuid4().hex[:16]}", "kind": kind,
                          "nonces": [], "signatures": {}, "first_seen": now}
                if len(self._request_ids) >= self.REQUEST_IDENTITY_LIMIT:
                    self._request_ids.clear()
                self._request_ids[sid] = record
            if nonce not in record["nonces"]:
                record["nonces"].append(nonce)
                del record["nonces"][:-8]
            record["signatures"][source] = signature
            return record["request_id"]

    def _retire_request_identity(self, sid):
        """The prompt resolved: forget both its identity and its answered fence."""
        with self._request_identity_guard:
            self._request_ids.pop(sid, None)
            self._answered_requests.pop(sid, None)

    def _apply_request_identity(self, sid, pending, now):
        """Stamp `request_id` onto a pending prompt, or hide one already answered.

        Called with the RAW pending: retirement keys off the provider's state,
        never off the fence's own suppression, or the fence would clear itself on
        the next scan."""
        if pending is None:
            self._retire_request_identity(sid)
            return None
        pending["request_id"] = request_id = self._request_identity(sid, pending, now)
        with self._request_identity_guard:
            fence = self._answered_requests.get(sid)
            if not fence or fence["request_id"] != request_id:
                return pending
            if now - fence["at"] <= self.ANSWERED_FENCE_SECONDS:
                return None
            # The provider never resolved it. Show it again rather than leave a
            # genuinely open prompt permanently invisible.
            self._answered_requests.pop(sid, None)
        return pending

    def _record_answered_request(self, sid, nonce):
        """Fence the prompt this nonce belongs to (invariant 75).

        Blocks a second device — or this one after a nonce flip — from answering
        the same prompt twice. Only fences a prompt the scan has already seen;
        without a record there is no identity to fence and refusing would be a
        guess."""
        nonce = str(nonce or "")
        with self._request_identity_guard:
            record = self._request_ids.get(sid)
            if not record or nonce not in record["nonces"]:
                return False
            self._answered_requests[sid] = {"request_id": record["request_id"],
                                            "at": time.time()}
            return True

    def _request_answered(self, sid, nonce):
        """True while this nonce's prompt is inside its answered fence."""
        nonce = str(nonce or "")
        with self._request_identity_guard:
            fence = self._answered_requests.get(sid)
            record = self._request_ids.get(sid)
            if (not fence or not record or nonce not in record["nonces"] or
                    fence["request_id"] != record["request_id"]):
                return False
            if time.time() - fence["at"] > self.ANSWERED_FENCE_SECONDS:
                self._answered_requests.pop(sid, None)
                return False
            return True

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

    # How much of one tool result the browser may pull in when a row is expanded.
    # Beyond this the row says so and stops; the ring never held this text at all,
    # so nothing here is a cache — it is a bounded read of the transcript.
    TOOL_RESULT_INLINE = 4096

    def tool_result(self, sid, tool_id):
        """The full-ish output of one tool call, read from the transcript.

        The conversation ring holds a one-line preview per tool row, because
        holding the real thing would be megabytes per live session for output
        nobody has opened. Expanding a row asks for it here instead.

        `tool_id` is client-supplied, so it is shape-checked before it is
        compared — and it is only ever compared, never used to build a path. The
        transcript comes from the registry, exactly as `session_context` resolves
        it, so this route can read no file that route could not.
        """
        # Deliberately NOT staging-gated. A live terminal read is a capability
        # and staging holds none over sessions it did not start (invariant 74);
        # a transcript read is not, and staging may read the shared transcripts
        # (invariant 56). This route must match `session_context`, which is the
        # same file through the same resolver.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(tool_id or "")):
            return {"ok": False, "error": "invalid tool reference"}
        reg, path = self._reg_main_path(sid)
        if not reg or not os.path.isfile(path):
            return {"ok": False, "error": "session not live"}
        found = None
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    # cheap reject before parsing: the id must appear literally
                    if tool_id not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    content = (row.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (isinstance(block, dict) and block.get("type") == "tool_result"
                                and block.get("tool_use_id") == tool_id):
                            found = block            # last occurrence wins
        except OSError as error:
            return {"ok": False, "error": f"transcript unreadable: {error}"}
        if found is None:
            return {"ok": False, "error": "no result recorded for this call"}
        text = Tail._result_text(found)
        return {"ok": True, "session_id": str(sid), "tool_id": str(tool_id),
                "failed": bool(found.get("is_error")),
                "text": text[:self.TOOL_RESULT_INLINE],
                "chars": len(text),
                "truncated": len(text) > self.TOOL_RESULT_INLINE}

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

    def session_screen(self, sid):
        """Bounded read-only capture of the terminal a live session is rendering.

        Fleet has never been able to see a screen; everything it believed about a
        terminal was inferred from the registry word, the hook capture, and the
        transcript fold. tmux makes the screen readable, which closes the standing
        "show me the terminal" gap for a stalled session — the pane shows the live
        tool output the transcript cannot.

        Three refusals are deliberate, not missing features:

        - **Background Claude jobs.** Their only channel is a private PTY whose
          bytes are readiness evidence and must never cross an API (invariant 25).
        - **Codex.** Its terminal route is proved narrowly and enables exactly
          text, image-path text, and focus (invariant 30). Reading its screen is
          not on that list, so it is not taken.
        - **Production sessions from staging.** A live terminal read is a
          capability, and staging holds none over sessions it did not start
          (invariant 56).

        Screen content is session content Fleet already serves through
        `/api/context` — the same prose and tool output — so this opens no new
        class of exposure, but it is raw rather than a projection, which is why the
        route is token-gated and the result is never cached, logged, or snapshotted.
        """
        sid = str(sid or "")
        unavailable = lambda reason: {"ok": False, "code": "screen_unavailable",
                                      "error": reason}
        if not sid:
            return unavailable("no session")
        if sid.startswith("codex:"):
            return unavailable("Fleet does not read Codex terminal screens")
        if self.is_staging and not self._staging_owns(sid):
            return unavailable("staging reads only the terminals of sessions it started")
        reg, _ = self._reg_main_path(sid)
        if not reg:
            return unavailable("session is not live")
        if self._is_background_claude(reg):
            return unavailable("a background Claude job has no terminal to read")
        tty = self._tty_for_pid(reg.get("pid"))
        if not tty:
            return unavailable("session has no terminal (VS Code / headless)")
        pane = self._tmux_target_for_tty(f"/dev/{tty}")
        if not pane:
            return unavailable("this session is not running in a tmux pane — only the "
                               "tmux transport can read a terminal screen")
        capture = self._tmux_capture(pane)
        if not capture.get("ok"):
            return unavailable(str(capture.get("error") or "the pane could not be read"))
        return {"ok": True, "session_id": sid, "transport": "tmux",
                "lines": capture["lines"], "truncated": capture["truncated"],
                "captured_at": time.time()}

    def prompt_options(self, sid):
        """The option rows the session's terminal is rendering for a live prompt.

        Exists for one reason: every permission variant puts Yes/always/No in
        rows 1/2/3, but row 2's WORDING differs sharply — a Bash prompt offers a
        project-wide directory grant, a Read prompt a session-only read, an
        Overwrite prompt a settings edit (all three captured live on v2.1.220).
        A fixed "always allow" button describes three different powers, and the
        dashboard never showed the sentence the user was actually agreeing to.

        Request path only, and deliberately NOT part of the fleet snapshot: these
        strings come off a terminal, and invariant 78's rule is that the scan may
        derive a label but may not publish screen text — the snapshot is cached
        on the device by the service worker. Every refusal `session_screen`
        makes applies here unchanged, because this is that same capture.
        """
        screen = self.session_screen(sid)
        if not screen.get("ok"):
            return screen
        lines = screen["lines"]
        kind = screenlib.classify_screen(lines)
        always = screenlib.always_option(lines) if kind == screenlib.PERMISSION else None
        out = {"ok": True, "session_id": str(sid), "kind": kind,
               "options": screenlib.prompt_options(lines)}
        # Absent `always_key` is the answer, not a gap: a permission prompt for a
        # command Claude cannot statically analyze offers no persistent grant at
        # all, and Fleet must render no button rather than press whatever sits on
        # row 2 — which on that variant is "No".
        if always:
            out["always_key"], out["always_label"] = str(always[0]), always[1]
        return out

    def screen_prompt_state(self, reg, tty):
        """One look at the terminal, answering both questions act() has.

        Returns `{"kind":…, "always": (digit, text) | None}` or None for no
        evidence at all — no tmux pane, an unreadable pane, or a screen the
        classifier does not recognize. Callers must never read None as "no
        prompt"; it is the state Fleet has always been in.

        Request-path only (invariant 74): this runs when a user is about to send
        keys, never on the scan. It is deliberately given the already-resolved
        registry row and tty so it adds no lookups of its own — and it answers
        the widget question and the which-key question from the SAME capture, so
        answering a prompt still costs exactly one `capture-pane`.
        """
        if not tty or self._is_background_claude(reg):
            return None
        pane = self._tmux_target_for_tty(f"/dev/{tty}" if not str(tty).startswith("/") else tty)
        if not pane:
            return None
        capture = self._tmux_capture(pane, max_rows=screenlib.TAIL_LINES)
        if not capture.get("ok"):
            return None
        lines = capture.get("lines") or []
        kind = screenlib.classify_screen(lines)
        if kind == screenlib.UNKNOWN:
            return None
        return {"kind": kind,
                "always": screenlib.always_option(lines)
                if kind == screenlib.PERMISSION else None}

    def screen_prompt_kind(self, reg, tty):
        """Just the widget label from `screen_prompt_state`, or None."""
        state = self.screen_prompt_state(reg, tty)
        return state["kind"] if state else None

    def observe_screens(self, rows):
        """One batched look at the terminals Fleet is guessing about (invariant 78).

        `rows` is [(session_id, pid, eligible)] built by the scan. Only sessions
        the scan cannot describe confidently are looked at — a hook capture whose
        registry disagrees about a prompt, or a session that has written no
        transcript at all and may be sitting on the folder-trust dialog.
        Everything else is skipped, so the eligible set is normally EMPTY and this
        whole pass costs one loop over a list of tuples.

        The pass derives a LABEL and nothing else. Raw screen text stays behind
        the on-request `/api/screen` route: the scan may look at a terminal, it
        may not publish one (invariant 74). That distinction is load-bearing —
        the fleet snapshot is cached on the device by the service worker, and
        terminal contents do not belong in an offline cache.
        """
        now = time.time()
        default_window = max(5, int(self.cfg.get("screen_observe_seconds", 60) or 60))
        if not self.cfg.get("screen_observe", True):
            self._screen_states.clear()
            return {}
        panes = {}
        for row in rows:
            sid, pid, eligible = row[0], row[1], row[2]
            # A row may name its own re-observation window. The trust and ghost
            # cases are stable for minutes; a compaction is over in tens of
            # seconds, and a permission prompt has to be SEEN before its hook
            # fires ~6s later. At the default cadence both are missed — the
            # prompt case silently did nothing on a rig until this existed.
            window = row[3] if len(row) > 3 and row[3] else default_window
            if not eligible or not pid:
                continue
            seen = self._screen_states.get(sid)
            if seen and now - seen["at"] < window:
                continue            # still fresh; the label carries over
            # _tty_for_pid shells out on a cache MISS, so it is resolved only for
            # a session already known to be worth looking at. A pid's tty never
            # changes, so that is once per session, not once per scan.
            tty = self._tty_for_pid(pid)
            pane = self._tmux_target_for_tty(f"/dev/{tty}") if tty else None
            if pane:
                panes[sid] = pane
        if panes:
            captured = self._tmux_capture_many(
                list(panes.values()), max_rows=screenlib.TAIL_LINES)
            for sid, pane in panes.items():
                frame = captured.get(pane["pane_id"])
                if frame is None:
                    continue
                state = screenlib.classify_screen(frame["lines"])
                # `since` survives while the label does, so a caller can ask how
                # long a surface has been up. It is when Fleet FIRST SAW it, not
                # when it started — the difference is bounded by the window above
                # and must be described honestly wherever it is rendered.
                seen = self._screen_states.get(sid)
                self._screen_states[sid] = {
                    "state": state, "at": now,
                    "since": seen["since"] if seen and seen.get("state") == state
                             and seen.get("since") else now}
        live = {row[0] for row in rows}
        for sid in [key for key in self._screen_states if key not in live]:
            self._screen_states.pop(sid, None)
        return {sid: value["state"] for sid, value in self._screen_states.items()}

    def observed_screen(self, sid):
        """The most recent screen label for one session, or None if unobserved."""
        seen = self._screen_states.get(sid)
        return seen["state"] if seen else None

    def observed_screen_seconds(self, sid, state):
        """How long `sid` has been showing `state`, or None if it is showing
        something else. This is time since Fleet first OBSERVED the surface, so it
        is a lower bound — never present it as when the thing started."""
        seen = self._screen_states.get(sid)
        if not seen or seen.get("state") != state:
            return None
        return max(0, round(time.time() - (seen.get("since") or seen["at"])))

    # One page of older conversation, in raw transcript rows read per request.
    # 600 rows folds to well under the ring cap and reads a few hundred KB from
    # the tail of the file — never the whole transcript, which runs to 15 MB.
    TRANSCRIPT_PAGE_ROWS = 600
    TRANSCRIPT_PAGE_CHUNK = 512 * 1024

    @classmethod
    def _lines_before(cls, path, before):
        """The complete JSONL lines immediately preceding byte `before`.

        Returns `(lines, start)`. Reading BACKWARDS in chunks is what makes
        paging affordable: a session's transcript is tens of megabytes and the
        reader only ever wants the few hundred rows above where it already is.
        """
        start, chunks, rows = before, [], 0
        with open(path, "rb") as handle:
            while start > 0 and rows < cls.TRANSCRIPT_PAGE_ROWS:
                size = min(cls.TRANSCRIPT_PAGE_CHUNK, start)
                start -= size
                handle.seek(start)
                chunk = handle.read(size)
                rows += chunk.count(b"\n")
                chunks.insert(0, chunk)
        blob = b"".join(chunks)
        if start > 0:
            # the first line in the blob began before `start`, so it is partial
            cut = blob.find(b"\n")
            if cut < 0:
                return [], before
            start += cut + 1
            blob = blob[cut + 1:]
        lines = blob.splitlines()
        if len(lines) > cls.TRANSCRIPT_PAGE_ROWS:
            dropped = lines[:len(lines) - cls.TRANSCRIPT_PAGE_ROWS]
            start += sum(len(line) + 1 for line in dropped)
            lines = lines[len(dropped):]
        return lines, start

    def _older_page(self, path, before):
        """Fold the transcript window ending at `before` into conversation rows.

        A window folded on its own cannot attach a result whose call sits above
        it, and does not merge with an assistant row outside it. That is the
        honest trade for not re-folding 15 MB per page: the boundary row is
        slightly less complete than it is in the live tail.
        """
        try:
            before = max(0, min(int(before), os.path.getsize(path)))
        except (TypeError, ValueError, OSError):
            return None
        if before <= 0:
            return {"ok": True, "paged": True, "messages": [], "files": [],
                    "next_cursor": None}
        lines, start = self._lines_before(path, before)
        window = Tail(path)
        # The live ring is a bounded TAIL; a page is a window, and dropping its
        # oldest entries would leave a hole between what this page shows and
        # where its cursor points. Fold the whole window.
        window.convo = deque(maxlen=self.TRANSCRIPT_PAGE_ROWS)
        offset = start
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                # a line that will not parse still advances the cursor by its
                # own length, or every offset after it is wrong
                offset += len(line) + 1
                continue
            window._fold(row, row_start=offset)
            offset += len(line) + 1
        # The page boundary is the first row already owned by the newer page.
        # Feed it to the prompt-lineage classifier without emitting it. This is
        # what prevents an abandoned command immediately before the boundary
        # from reappearing only after the reader scrolls into older history.
        try:
            with open(path, "rb") as handle:
                handle.seek(before)
                boundary = json.loads(handle.readline())
            window._prompt_segment_update(boundary)
        except Exception:
            pass
        messages = [dict(entry) for entry in window.convo]
        # The cursor is where the OLDEST ROW RETURNED begins — never the window
        # start. Those differ whenever the window folds to fewer rows than it
        # read, and pointing at the window start would silently skip the
        # difference on the next page. `off` of 0 is the first row in the file
        # and correctly ends the walk.
        # A window that folds to nothing is the head of the file — session
        # metadata rows that are not conversation. Ending the walk there is what
        # stops the reader being offered one more empty page.
        cursor = next((row["off"] for row in messages if row.get("off")), None)
        return {"ok": True, "paged": True, "messages": messages, "files": [],
                "next_cursor": cursor if messages else None}

    def session_context(self, sid, before=None, limit=50):
        """Recent conversation turns + SendUserFile deliveries for one session.

        Claude sessions are paged by TRANSCRIPT BYTE OFFSET (`next_cursor`), not
        by an index into the live ring: the ring is a 300-entry tail and indexes
        into it shift as it evicts. The offset is stable, monotonic, and already
        the coordinate the fold works in — so "load older" reaches the first
        message of the session instead of stopping at the ring's edge.
        """
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
        if before is not None:
            page = self._older_page(path, before)
            if page is None:
                return {"ok": False, "error": "invalid conversation cursor"}
            return page
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
        tool_files = {}
        def fmeta(p):
            backup = self._claude_file_backup(sid, file_backups.get(p))
            return {"name": os.path.basename(p),
                    "file_id": self.file_id(sid, p),
                    "kind": "image" if os.path.splitext(p)[1].lower() in IMG_EXTS else "text",
                    "missing": not os.path.isfile(p) and backup is None}
        for m in msgs:                  # enrich inline delivery entries for the client
            if m.get("role") == "tool" and m.get("files"):
                tool_id = str(m.get("tool_id") or "")
                durable = tool_files.setdefault(
                    tool_id, self.artifact_tool_files(sid, tool_id)) if tool_id else []
                m["files"] = durable or [fmeta(p) for p in m["files"]]
        durable_files = self.session_files(sid, limit=100)
        out_files = durable_files.get("files") if durable_files.get("ok") else []
        if not out_files:
            for f in reversed(files):       # newest delivery first
                out_files.append({**fmeta(f["path"]), "caption": f["caption"], "ts": f["ts"]})
        # The live page is the newest `limit` of the ring; its cursor is where
        # the oldest row it returns began in the file. Rows inserted by timestamp
        # rather than appended (a compaction's event row, invariant 17) carry no
        # offset, so the cursor comes from the oldest row that has one — a page
        # boundary that repeats a row is recoverable, one that skips is not.
        page = msgs[-max(1, int(limit or 50)):] if msgs else []
        cursor = next((row["off"] for row in page if row.get("off")), None)
        return {"ok": True, "paged": True, "messages": page, "files": out_files,
                "next_cursor": cursor}

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
        def with_artifacts(messages):
            for message in messages:
                if message.get("role") != "tool" or not message.get("files"):
                    continue
                tool_id = str(message.get("tool_id") or "")
                durable = self.artifact_tool_files(sid, tool_id, aid) if tool_id else []
                if durable:
                    message["files"] = durable
                else:
                    message["files"] = [{
                        "name": os.path.basename(path),
                        "file_id": self.file_id(sid, path),
                        "kind": "image" if os.path.splitext(path)[1].lower() in IMG_EXTS else "text",
                        "missing": not os.path.isfile(path),
                    } for path in message["files"] if isinstance(path, str)]
            inventory = self.session_files(sid, aid, limit=100)
            return messages, inventory.get("files", []) if inventory.get("ok") else []

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
                deliveries = [{
                    **delivery, "session_id": sid, "source_agent_id": aid,
                    "transcript_path": transcript,
                    "file_backups": dict(tail.file_backups),
                } for delivery in tail.file_deliveries]
                tail.file_deliveries.clear()
            self.store_artifact_deliveries(deliveries)
            messages, files = with_artifacts(messages)
            return {"ok": True, "messages": messages, "files": files,
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
            messages, files = with_artifacts(
                copy.deepcopy(snapshot.get("messages") or []))
            return {"ok": True, "messages": messages, "files": files, "info": info}
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
            deliveries = [{
                **delivery, "session_id": sid, "source_agent_id": aid,
                "transcript_path": jl, "file_backups": dict(t.file_backups),
            } for delivery in t.file_deliveries]
            t.file_deliveries.clear()
        self.store_artifact_deliveries(deliveries)
        msgs, files = with_artifacts(msgs)
        return {"ok": True, "messages": msgs, "files": files, "info": info}

    def file_selector_for_path(self, sid, raw_path):
        """Return an opaque selector only when ``raw_path`` belongs to ``sid``."""
        durable = self.artifact_selector_for_path(sid, raw_path)
        if durable:
            return durable
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
        durable = self.artifact_content(sid, selector)
        if durable is not None:
            return durable
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
