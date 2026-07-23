"""Tail: incremental JSONL transcript fold with convo/files/usage ring buffers."""
import os, re, json, time
from collections import deque
from . import paths as pathcfg
from .config import KEY_TOOLS, ktok, iso_epoch, usd, model_family

class Tail:
    """Incremental jsonl reader: keeps byte offset + running aggregates."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.ti = self.tw = self.tr = self.to = 0
        self.model = ""
        # Byte offsets, rather than row timestamps, establish provider evidence
        # order. Compaction can append older-timestamped rows after a command.
        self.model_evidence_offset = 0
        # Claude writes mode changes as top-level `permission-mode` records and
        # also stamps the effective mode onto human prompt rows. Keep the newest
        # observed value; the live registry does not expose it.
        self.permission_mode = None
        self.permission_mode_evidence_offset = 0
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
        # Claude snapshots files it writes beneath ~/.claude/file-history/<sid>.
        # Keep only transcript-declared path -> opaque backup-name mappings so a
        # delivered scratch file remains readable after Claude removes its temp dir.
        self.file_backups = {}
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
        consumed = chunk[:nl + 1]
        row_end = self.offset - len(consumed)
        for line in consumed.splitlines(keepends=True):
            row_end += len(line)
            try:
                o = json.loads(line)
            except Exception:
                continue
            self._fold(o, evidence_offset=row_end)
        return True

    def _fold(self, o, evidence_offset=None):
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
            if evidence_offset is not None:
                self.permission_mode_evidence_offset = max(
                    self.permission_mode_evidence_offset, int(evidence_offset))
        if o.get("type") == "permission-mode":
            return
        if o.get("type") == "system":
            self._system_event(o, ts)
            return
        if o.get("type") == "queue-operation":
            self._agent_terminal_event(o.get("content"), ts)
            return
        if o.get("type") == "file-history-snapshot":
            tracked = ((o.get("snapshot") or {}).get("trackedFileBackups") or {})
            if isinstance(tracked, dict):
                for fpath, meta in list(tracked.items())[:2048]:
                    backup = meta.get("backupFileName") if isinstance(meta, dict) else None
                    if isinstance(fpath, str) and isinstance(backup, str) and re.fullmatch(
                            r"[0-9a-f]{8,64}@v[0-9]{1,8}", backup):
                        self.file_backups[fpath] = backup
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
                if m.get("model") and evidence_offset is not None:
                    self.model_evidence_offset = max(
                        self.model_evidence_offset, int(evidence_offset))
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
        v = str(v).replace(pathcfg.HOME, "~")
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
        text = str(text)
        self.convo_rev += 1
        # merge assistant rows within one work stretch into one logical reply
        # (a key-tool entry in between intentionally breaks the merge)
        if self.convo and role == "assistant" and self.convo[-1]["role"] == "assistant":
            prev = self.convo[-1]
            prev["text"] = prev["text"] + "\n\n" + text
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
        previous = None
        for f in list(self.files):
            if f["path"] == path:       # re-delivery: refresh, don't duplicate
                previous = f
                self.files.remove(f)
                break
        self.files.append({"path": path,
                           "caption": caption or (previous or {}).get("caption", ""),
                           "ts": ts})

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

