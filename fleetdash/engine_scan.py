"""Registry scan, session organization, status lines, provider state, and
Claude control-override reconciliation (invariants 5, 7, 22, 31, 40, 42)."""
import json, os, re, sys, glob, time, sqlite3, secrets, threading, plistlib, hashlib, copy, queue
from collections import deque
from .repo_center import RepositoryOutcomeCenter, observed_test_outcome


from . import paths as pathcfg
from .paths import capture_base  # legacy alias; reads paths.* at call time
from .config import (DEFAULT_CONFIG, WAITING_CONFIRM_SECONDS, _write_private_json, model_family, cwd_to_project_dir, iso_epoch)
from .placement import _pending_placement, classify_placement, classify_closed_placement
from .tail import Tail




class ScanOps:

    # -- live sessions from the CLI registry
    def live_sessions(self):
        out = []
        for p in glob.glob(os.path.join(pathcfg.SESSIONS, "*.json")):
            try:
                with open(p) as handle:
                    d = json.load(handle)
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

    def _claude_mutation_lock(self, sid):
        with self._claude_mutation_locks_guard:
            return self._claude_mutation_locks.setdefault(str(sid), threading.RLock())

    def _record_claude_turn_fence(self, sid, baseline):
        with self._claude_turn_fences_guard:
            self._claude_turn_fences[str(sid)] = {
                "transcript_size": baseline.get("transcript_size"),
                "convo_rev": baseline.get("convo_rev"),
                "accepted_at": time.time(), "seen_active": False}

    def _claude_turn_fenced(self, sid, reg_status, path=None, mt=None):
        """Keep rapid follow-up writes out of Claude's registry-lag window.

        The fence clears only after the provider reports an active state followed
        by idle, or after the transcript changes beyond the exact pre-write file
        size and folds to an awaiting-input state (the fast-turn / failed-turn
        equivalent). Wall-clock expiry would reintroduce the same race.
        """
        sid = str(sid)
        with self._claude_turn_fences_guard:
            fence = self._claude_turn_fences.get(sid)
            if not fence:
                return False
            if reg_status in ("busy", "shell", "waiting"):
                fence["seen_active"] = True
                return True
            if reg_status == "idle" and fence.get("seen_active"):
                self._claude_turn_fences.pop(sid, None)
                return False
            transcript_changed = False
            if path and mt is not None:
                try:
                    transcript_changed = os.path.getsize(path) != fence.get("transcript_size")
                except OSError:
                    transcript_changed = False
                if (transcript_changed and mt.turn_state() == "awaiting_input"):
                    self._claude_turn_fences.pop(sid, None)
                    return False
            return True

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
            session, now, self.cfg.get("reply_available"), self.cfg.get("read_sessions"),
            self.cfg.get("dormant_seconds"))
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
            elif session.get("new_response"):
                kind, request, delivery = "outcome", "Completed work is ready to review", "Unreviewed"
            elif session.get("ui_group") == "needs_you":
                kind = "problem" if session.get("state") in \
                    ("blocked", "error", "stalled_or_prompt") \
                    else "attention"
                request = session.get("error") or session.get("reason_label") or "Session needs attention"
                delivery = "Intervention needed"
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
        paths = [pathcfg.CLAUDE_SETTINGS]
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
        # `scan_serialize` keeps one _scan at a time — a guarantee scan_lock used
        # to provide by covering the whole call. scan_lock itself is now taken
        # only around Tail work inside _scan, so an /api/act freshness re-poll
        # waits ~30 ms for the fold instead of ~600 ms for the whole scan
        # (measured on production 2026-07-24: claude 30.3 ms of 723.6 ms, the
        # rest being codex 448.1, operations 128.1, closed_history 67.1,
        # live_ledger 42.4 — none of which touch a Tail).
        with self.scan_serialize:
            fleet = self._scan()
        self._schedule_image_cleanup()
        elapsed = (time.perf_counter() - started) * 1000
        wait_ms = self._scan_fold_wait_ms
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
            "scan_lock_held_ms": round(self._scan_lock_held_ms, 3),
            "scan_samples": len(ordered),
            "state_journal_ms": round(self.last_state_journal_ms, 3),
            "phases_ms": phases,
            "operations_db": self.operations.diagnostics(),
            "outbox_db": self.outbox.diagnostics(),
        }
        codex_diagnostics = getattr(self.codex, "diagnostics", None)
        if codex_diagnostics:
            try:
                fleet["diagnostics"]["codex"] = codex_diagnostics()
            except Exception as exc:
                fleet["diagnostics"]["codex"] = {"error": str(exc)}
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
        screen_watch = []
        # Tails are stateful byte offsets: every fold, here and in act(), stays
        # serialized by scan_lock. Only this loop folds them, so only this loop
        # holds it (invariant 24/43); the phases after it touch no Tail.
        fold_wait_started = time.perf_counter()
        self.scan_lock.acquire()
        held_started = time.perf_counter()
        self._scan_fold_wait_ms = (held_started - fold_wait_started) * 1000
        try:
            self._scan_claude_sessions(sessions, claude_tails, live_claude_ids, now, cfg,
                                       screen_watch)
        finally:
            self.scan_lock.release()
        # Published as `scan_lock_held_ms`: how long an act() re-poll could have
        # been blocked by this scan. It is the number this split exists to shrink.
        self._scan_lock_held_ms = (time.perf_counter() - held_started) * 1000
        phase("claude")
        return self._scan_after_fold(sessions, claude_tails, live_claude_ids,
                                     now, cfg, phase, phases, screen_watch)

    @staticmethod
    def _active_tool(mt, state):
        """Name the tool call a working session is blocked on, with its age.

        Only for states where something is genuinely in flight — an idle session
        can still hold a stale `pending` entry from a turn that ended without a
        result row, and reporting that as live work would be a lie.

        The age is derived from the tool_use row's own timestamp. Compaction can
        append rows carrying earlier timestamps (invariant 17), so a negative
        result is clamped to 0 rather than rendered as a time in the future.
        """
        if state not in ("running", "stalled", "stalled_or_prompt"):
            return None
        open_tool = mt.open_tool()
        if not open_tool:
            return None
        started = iso_epoch(open_tool.get("ts"))
        return {"name": str(open_tool["name"])[:40],
                "count": open_tool["count"],
                "seconds": max(0, round(time.time() - started)) if started else None}

    def _scan_claude_sessions(self, sessions, claude_tails, live_claude_ids, now, cfg,
                              screen_watch=None):
        """Fold every live Claude transcript. Runs under scan_lock.

        `screen_watch` collects (session_id, pid, eligible) for the batched screen
        observation that runs later, unlocked (invariant 78). Eligibility is
        decided here because this is where the evidence lives; the look itself
        must not happen under the lock.
        """
        watch = screen_watch if screen_watch is not None else []
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
                        "model_effort_settings": False,
                        "change_model_effort": False,
                        "change_model_effort_reason": "Waiting for Claude to initialize",
                        "answer_structured": False, "decide_approval": False,
                        "answer_reason": "Waiting for Claude's native prompt state",
                        "spawn_agent": True, "relay_agent": True,
                        "account_usage": True, "exact_cost": True},
                })
                # No transcript at all is exactly what a session blocked on the
                # folder-trust dialog looks like: Claude writes nothing until the
                # first turn. Worth a look.
                watch.append((sid, reg.get("pid"), True))
                continue
            mt = self.tail_for(main_path)
            mt.poll()
            control_uncertain = self._reconcile_claude_control_state(sid, mt)
            claude_tails[sid] = mt
            self._claude_context_snapshots[sid] = {
                "revision": mt.convo_rev,
                "messages": copy.deepcopy(list(mt.convo)),
                "files": copy.deepcopy(list(mt.files)),
                "file_backups": copy.deepcopy(mt.file_backups),
                "delivered_paths": dict(mt.delivered_paths),
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
            # Three cases are worth a look at the pane.
            #  - the ghost-question case invariant 5 has always guessed about: a
            #    hook capture says a prompt is open while the registry disagrees;
            ghost = bool(pending) and reg_status != "waiting"
            #  - the reverse, which costs SECONDS of every permission prompt: the
            #    registry says `waiting` and no capture has arrived. Claude's
            #    permission Notification hook fires ~6s after the prompt renders
            #    (measured on a rig 2026-07-25: prompt on screen at t+4.5s,
            #    capture at t+10.6s) and the transcript never holds the row while
            #    the prompt is open. The pane has it immediately (invariant 80);
            awaiting_capture = not pending and reg_status == "waiting"
            #  - and a compaction, which needed a wider net than expected. A
            #    compaction writes NOTHING to the transcript while it runs
            #    (invariant 17) AND the registry reports the session `idle`
            #    throughout — verified live 2026-07-25, which is why "busy with a
            #    frozen transcript" detected nothing. From Fleet's own data a
            #    compacting session is indistinguishable from an idle one; that
            #    is precisely the gap it closes, and it means the pane is the
            #    only evidence. So a session Fleet still considers live is
            #    eligible.
            watchable = (reg_status is not None
                         and quiet < cfg["dormant_seconds"])
            frozen = watchable or self.observed_screen(sid) == "compacting"
            # The SHORTEST applicable window wins. Cost is one batched
            # capture-pane per window for the whole fleet — 5.6 ms at 49 panes —
            # and it still yields a LABEL only, never screen text (invariant 78).
            look_window = (cfg.get("screen_prompt_seconds") if awaiting_capture else
                           cfg.get("screen_observe_busy_seconds") if frozen else None)
            watch.append((sid, reg.get("pid"),
                          ghost or frozen or awaiting_capture, look_window))
            turn_starting = self._claude_turn_fenced(sid, reg_status, main_path, mt)
            # parent turn over → a frozen agent is canceled, not mid-tool
            parent_idle = reg_status == "idle" or confirmed_waiting
            agents = self.scan_agents(os.path.join(proj_dir, sid, "subagents"), now,
                                      parent_idle, parent=mt)
            sess_effort = self.effort_for(sid)
            for a in agents:            # the agent chat overlay acts through the parent
                a["session_id"] = sid
                a["effort"] = (a.pop("transcript_effort", None)
                               or self.agent_effort(a.get("agent_type"), cwd, sess_effort))
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

            # The CLI transcript has no explicit interrupted-turn event. A
            # dashboard-delivered Esc is enough to suppress reply/new-response
            # triage once that turn reaches its prompt; a later running turn
            # clears the marker.
            if state in ("running", "stalled", "stalled_or_prompt", "needs_you"):
                self._claude_interrupted.pop(sid, None)
            interrupted = bool(self._claude_interrupted.get(sid) is not None and
                               state in ("idle", "turn_done"))

            # Questions come ONLY from the hook capture (invariant 1). There used
            # to be a transcript fallback here that re-surfaced the same question
            # under its tool_use_id once `hook_pending`'s ghost guard dropped the
            # capture — a second identity for one prompt, which is exactly what
            # made an answered question reappear in production (the client
            # suppresses by nonce, so the new one did not match). The CLI only
            # flushes those rows AFTER the answer anyway, so the fallback could
            # not see a question that was genuinely still open.
            # Permissions keep their fallback: a Notification capture has no
            # clear-event, and the transcript is the only other evidence.
            if pending is None and (confirmed_waiting or reg_status == "idle"):
                # AskUserQuestion is excluded explicitly. Without the question
                # branch above it would otherwise fall through to here and render
                # an ask as a PERMISSION prompt, whose keys are a different recipe
                # entirely (invariants 4, 5).
                permissions = [(tid, p) for tid, p in mt.pending.items()
                               if p["name"] != "AskUserQuestion"]
                if permissions:
                    tid, p = permissions[-1]
                    pending = {"kind": "permission", "nonce": tid, "tool": p["name"],
                               "input_summary": json.dumps(p.get("input"), indent=1)[:1500]}
            # Third source, and the fastest: the pane itself. A permission prompt
            # is visible seconds before its Notification hook fires and while the
            # transcript still holds nothing, so with neither of those a rendered
            # permission screen IS the evidence. Restricted to permissions on
            # purpose — a question's PreToolUse capture is immediate, so questions
            # have no gap to close, and their key recipe depends on shape the
            # screen cannot be trusted to describe (invariant 4).
            if pending is None and reg_status == "waiting":
                pending = self._screen_permission(sid, now)
            if pending and pending.get("kind") == "question":
                # deliver-then-ask pattern: surface files sent shortly before the question
                q_ts = pending.pop("_ts", None) or now
                paired = self._paired_files(mt, q_ts)
                if paired:
                    pending["files"] = paired
            # One prompt, one server-owned identity, and no re-render of a prompt
            # this daemon already accepted an answer for (invariant 75).
            pending = self._apply_request_identity(sid, pending, now)
            if pending and pending["nonce"] not in self.pending_seen:
                self.pending_seen[pending["nonce"]] = now
                if len(self.pending_seen) > 5000:
                    self.pending_seen = dict(list(self.pending_seen.items())[-2500:])
                print(f"pending first seen: {sid[:8]} {pending['kind']} nonce={pending['nonce'][:24]}",
                      file=sys.stderr, flush=True)

            delivery_uncertain = None
            with self._claude_delivery_uncertain_guard:
                uncertain_nonce = self._claude_delivery_uncertain.get(sid)
            pending_nonce = pending.get("nonce") if pending is not None else None
            if uncertain_nonce and ((pending_nonce and
                                     pending_nonce != uncertain_nonce) or
                                    (pending is None and reg_status != "waiting")):
                self._clear_claude_delivery_uncertain(sid)
                uncertain_nonce = None
            if uncertain_nonce:
                delivery_uncertain = {"nonce": uncertain_nonce,
                    "message": "Delivery uncertain — check the Claude terminal, then refresh"}

            ctx = mt.context_tokens()
            fam = model_family(mt.model)
            cw = cfg["context_windows"].get(fam, cfg["context_windows"]["default"])
            permission_modes = self._claude_permission_modes(reg, mt)
            compacting = self.compacting_secs(sid, cwd, mt)
            can_change_permission_mode = bool(
                reg_status == "idle" and pending is None and compacting is None and
                not turn_starting and not control_uncertain and
                mt.permission_mode in permission_modes)
            can_change_model_effort = bool(reg_status == "idle" and pending is None and
                                           compacting is None and not turn_starting and
                                           not control_uncertain)
            can_answer_native = bool(reg_status == "waiting" and pending is not None and
                                     delivery_uncertain is None)
            settings_reason = (
                "Check Claude's terminal; the last control change is unconfirmed" if
                    control_uncertain else
                "Answer Claude's request before changing settings" if pending is not None else
                "Available after compaction finishes" if compacting is not None else
                "Waiting for Claude to acknowledge the last message" if turn_starting else
                "Available when Claude is idle" if reg_status != "idle" else "")
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
                # What the turn is blocked on right now, straight from the fold.
                # "stalled" already means frozen mid-TOOL (invariant 7) but the
                # card never said WHICH tool, so a wedged session and a slow one
                # looked identical. No screen read is involved, so this works on
                # sessions outside tmux too.
                "active_tool": self._active_tool(mt, state),
                # Collapsed height is CSS-controlled. Keep up to 800 characters so
                # the explicit expansion reveals a useful bounded preview.
                "last_msg": (mt.last_message(800)
                             if cfg.get("preview_sessions", True) else None),
                "_latest_prose": mt.latest_prose(),
                "repo_outcome": observed_test_outcome(
                    mt.convo, session_id=sid, provider="claude"),
                "state": state,
                "reg_status": reg_status,
                "interrupted": interrupted,
                "quiet_s": round(quiet),
                "ctx_tokens": ctx, "ctx_window": cw,
                "ctx_pct": round(100 * ctx / cw, 1) if cw else None,
                "total_tokens": mt.total_tokens,
                "cost": round(mt.cost(cfg), 4),
                "bridge_url": (f"https://claude.ai/code/{reg['bridgeSessionId']}"
                               if reg.get("bridgeSessionId") else None),
                "started_ms": reg.get("startedAt"),
                "pending": pending,
                "delivery_uncertain": delivery_uncertain,
                "control_delivery_uncertain": bool(control_uncertain),
                "compacting": compacting,
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
                "capabilities": {"submit": not turn_starting,
                    "queue_submit": turn_starting, "interrupt": state == "running",
                    "close": True,
                    "change_permission_mode": can_change_permission_mode,
                    "change_permission_mode_reason": settings_reason,
                    "model_effort_settings": True,
                    "change_model_effort": can_change_model_effort,
                    "change_model_effort_reason": settings_reason,
                    "focus_terminal": True,
                    "answer_structured": can_answer_native,
                    "decide_approval": can_answer_native,
                    "answer_reason": ("" if can_answer_native else
                        "Waiting for Claude's native prompt state"),
                    "spawn_agent": True,
                    "relay_agent": True, "account_usage": True, "exact_cost": True},
            })

    def _scan_after_fold(self, sessions, claude_tails, live_claude_ids,
                         now, cfg, phase, phases, screen_watch=None):
        """Everything the scan does once no Tail is touched again.

        Runs WITHOUT scan_lock. The single later Tail read — the status strip —
        re-takes it below; nothing else here reads a Tail.
        """
        self.registry_status_since = {
            sid: value for sid, value in self.registry_status_since.items()
            if sid in live_claude_ids
        }
        self.prune_act_receipts(now)     # rate-limited internally (invariant 76)
        # One batched capture-pane for the whole eligible set (invariant 78).
        # Measured 2026-07-24: 5.6 ms for 49 panes batched, against 241 ms as one
        # tmux invocation per pane — the cost is forking the client, not reading
        # the grid, which is what makes a fleet-wide look affordable here.
        screen_states = self.observe_screens(screen_watch or [])
        for session in sessions:
            sid = session.get("session_id")
            state = screen_states.get(sid)
            if state:
                session["screen_state"] = state
            # The PreCompact hook's checkpoint is the better evidence — it marks
            # when the compaction STARTED. Projects with no such hook got no pill
            # at all; for those the pane is the only live artifact, because the
            # transcript stays silent until the compaction finishes (invariant 17).
            if state == "compacting" and session.get("compacting") is None:
                observed = self.observed_screen_seconds(sid, "compacting")
                # Same 900s stale guard the hook path uses: a label this old is
                # a pane Fleet has stopped being able to read, not a compaction.
                if observed is not None and observed <= 900:
                    session["compacting"] = observed
                    session["compacting_source"] = "screen"
            elif session.get("compacting") is not None:
                session["compacting_source"] = "hook"
        with self.config_lock:
            control_overrides = {
                sid: value for sid, value in self._claude_control_overrides.items()
                if sid in live_claude_ids}
            if control_overrides != self._claude_control_overrides:
                self._claude_control_overrides = control_overrides
                self._claude_effort_overrides = {
                    sid: (entry["effort"]["value"], entry["effort"]["accepted_at"])
                    for sid, entry in control_overrides.items() if "effort" in entry}
                self._persist_private_runtime_map(
                    "claude_control_overrides", control_overrides)
            control_uncertain = {
                sid: value for sid, value in self._claude_control_uncertain.items()
                if sid in live_claude_ids}
            if control_uncertain != self._claude_control_uncertain:
                self._claude_control_uncertain = control_uncertain
                self._persist_private_runtime_map(
                    "claude_control_uncertain", control_uncertain)
        with self._claude_turn_fences_guard:
            self._claude_turn_fences = {
                sid: value for sid, value in self._claude_turn_fences.items()
                if sid in live_claude_ids
            }
        with self.config_lock:
            with self._claude_delivery_uncertain_guard:
                delivery_uncertain = {
                sid: value for sid, value in self._claude_delivery_uncertain.items()
                if sid in live_claude_ids
                }
                delivery_changed = delivery_uncertain != self._claude_delivery_uncertain
                self._claude_delivery_uncertain = delivery_uncertain
            if delivery_changed:
                self._persist_private_runtime_map(
                    "claude_delivery_uncertain", delivery_uncertain)
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
            self._ensure_codex_launcher()
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
        # App Server turn ownership and terminal reachability are separate facts.
        # A live TUI attached to Fleet's exact socket remains a safe text/focus
        # route even when App Server reports an active-turn ownership mismatch.
        self._apply_codex_terminal_routes(
            codex_sessions, self._codex_terminal_routes())
        sessions.extend(codex_sessions)
        phase("codex")
        muted = self.cfg.get("muted_sessions") or {}
        # The only Tail read left in the scan. `status_metrics` reads folded
        # counters without polling, but a concurrent act() fold would still tear
        # them, so re-take scan_lock for exactly this pass.
        status_started = time.perf_counter()
        with self.scan_lock:
            status_lines = {
                id(session): self.session_status_line(
                    session, claude_tails.get(session.get("session_id")))
                for session in sessions}
        self._scan_lock_held_ms += (time.perf_counter() - status_started) * 1000
        for session in sessions:
            session["status_line"] = status_lines[id(session)]
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
        if self.is_staging:
            sessions = [self._staging_mask_session(item) for item in sessions]
            closed = [self._staging_mask_session(item) for item in closed]
            actions = [item for item in actions
                       if self._staging_owns(item.get("session_id"))]
        fleet = {
            "t": now,
            "instance": {
                "mode": self.cfg.get("instance_mode", "production"),
                "name": self.cfg.get("instance_name", "Fleet Dash"),
                "controls": "staging_owned_only" if self.is_staging else "production",
                "owned_sessions": len(self._staging_owned()) if self.is_staging else None,
                "source_root": self._staging_source_root() if self.is_staging else None,
            },
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
                                    "error": self.codex_scan_error or self.codex.error,
                                    "runtime": ({**self.codex.runtime_status(),
                                                 "launcher": self.codex_launcher_status}
                                                if hasattr(self.codex, "runtime_status")
                                                else None)}},
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
            budgets = self.operations.observe(
                self._staging_operations_fleet(fleet), self.workstream_identity)
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
        db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=2)
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
        db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=2)
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
        path = os.path.join(pathcfg.BASE, "config.json")
        with self.config_lock:
            try:
                with open(path) as handle:
                    raw = json.load(handle)
            except Exception:
                raw = {}
            raw.update(changed)
            _write_private_json(path, raw)

    def _persist_private_runtime_map(self, key, value):
        """Persist one internal recovery map and mirror the exact durable value."""
        payload = copy.deepcopy(value)
        with self.config_lock:
            try:
                self._persist_config_fields({key: payload})
            except Exception as exc:
                return f"recovery state could not be saved: {exc}"
            self.cfg[key] = payload
        return None

    def _set_claude_delivery_uncertain(self, sid, nonce):
        sid, nonce = str(sid or ""), str(nonce or "")
        if not sid or not nonce:
            return "prompt identity is unavailable"
        with self.config_lock:
            with self._claude_delivery_uncertain_guard:
                updated = dict(self._claude_delivery_uncertain)
                updated.pop(sid, None)
                updated[sid] = nonce
                updated = dict(list(updated.items())[-200:])
                self._claude_delivery_uncertain = updated
            return self._persist_private_runtime_map(
                "claude_delivery_uncertain", updated)

    def _clear_claude_delivery_uncertain(self, sid):
        with self.config_lock:
            with self._claude_delivery_uncertain_guard:
                if sid not in self._claude_delivery_uncertain:
                    return None
                updated = dict(self._claude_delivery_uncertain)
                updated.pop(sid, None)
                self._claude_delivery_uncertain = updated
            return self._persist_private_runtime_map(
                "claude_delivery_uncertain", updated)

    @staticmethod
    def _claude_control_baseline(tail, field):
        if field == "model":
            return int(getattr(tail, "model_evidence_offset", 0) or 0)
        if field == "permission_mode":
            return int(getattr(tail, "permission_mode_evidence_offset", 0) or 0)
        if field == "effort":
            return int(getattr(tail, "effort_evidence_offset", 0) or 0)
        return 0

    def _record_claude_control_overrides(self, sid, tail, accepted):
        """Durably project provider-accepted controls until newer native evidence."""
        if not accepted:
            return None
        now = time.time()
        with self.config_lock:
            updated = copy.deepcopy(self._claude_control_overrides)
            entry = dict(updated.get(sid) or {})
            for field, value in accepted.items():
                entry[field] = {"value": value, "accepted_at": now,
                                "baseline": self._claude_control_baseline(tail, field)}
            updated.pop(sid, None)
            updated[sid] = entry
            updated = dict(list(updated.items())[-200:])
            self._claude_control_overrides = updated
            if "effort" in accepted:
                self._claude_effort_overrides[sid] = (accepted["effort"], now)
            return self._persist_private_runtime_map(
                "claude_control_overrides", updated)

    def _mark_claude_control_uncertain(self, sid, tail, fields):
        now = time.time()
        record = {"attempted_at": now, "fields": {
            field: {"baseline": self._claude_control_baseline(tail, field)}
            for field in fields}}
        with self.config_lock:
            updated = dict(self._claude_control_uncertain)
            updated.pop(sid, None)
            updated[sid] = record
            updated = dict(list(updated.items())[-200:])
            self._claude_control_uncertain = updated
            return self._persist_private_runtime_map(
                "claude_control_uncertain", updated)

    def _retire_claude_control_override(self, sid, field):
        with self.config_lock:
            updated = copy.deepcopy(self._claude_control_overrides)
            entry = dict(updated.get(sid) or {})
            if field not in entry:
                return None
            entry.pop(field, None)
            if entry:
                updated[sid] = entry
            else:
                updated.pop(sid, None)
            self._claude_control_overrides = updated
            if field == "effort":
                self._claude_effort_overrides.pop(sid, None)
            return self._persist_private_runtime_map(
                "claude_control_overrides", updated)

    def _native_effort_evidence(self, sid):
        path = os.path.join(capture_base(), "effort", sid)
        try:
            stat_result = os.stat(path)
            with open(path) as handle:
                value = handle.read().strip()
        except OSError:
            return None, 0.0
        return (value if value in self.EFFORTS else None, stat_result.st_mtime)

    def _reconcile_claude_control_state(self, sid, tail):
        """Apply accepted controls and retire them only after newer native evidence."""
        changed_overrides = changed_uncertain = False
        with self.config_lock:
            overrides = copy.deepcopy(self._claude_control_overrides)
            entry = dict(overrides.get(sid) or {})
            native_effort, effort_mtime = self._native_effort_evidence(sid)
            for field in list(entry):
                item = entry[field]
                newer = False
                if field == "model":
                    newer = (int(getattr(tail, "model_evidence_offset", 0) or 0) >
                             int(item.get("baseline", 0)))
                    if not newer:
                        tail.model = item["value"]
                elif field == "permission_mode":
                    newer = (int(getattr(tail, "permission_mode_evidence_offset", 0) or 0) >
                             int(item.get("baseline", 0)))
                    if not newer:
                        tail.permission_mode = item["value"]
                else:
                    # Transcript assistant rows are the primary effort evidence
                    # (byte-offset ordered); the statusline side-write remains a
                    # legacy wall-clock fallback. A zero/absent baseline is a
                    # pre-transcript-evidence record: never let older transcript
                    # rows retire it (only the wall-clock path may).
                    baseline = int(item.get("baseline", 0) or 0)
                    newer = bool(
                        (getattr(tail, "effort", "") and baseline > 0 and
                         int(getattr(tail, "effort_evidence_offset", 0) or 0) > baseline)
                        or (native_effort and
                            effort_mtime > float(item.get("accepted_at", 0))))
                if newer:
                    entry.pop(field, None)
                    changed_overrides = True
                    if field == "effort":
                        self._claude_effort_overrides.pop(sid, None)
            if entry:
                overrides[sid] = entry
            elif sid in overrides:
                overrides.pop(sid, None)
            if changed_overrides:
                self._claude_control_overrides = overrides

            uncertain = dict(self._claude_control_uncertain)
            record = uncertain.get(sid)
            if record:
                resolved = True
                for field, item in record["fields"].items():
                    if field == "model":
                        newer = (int(getattr(tail, "model_evidence_offset", 0) or 0) >
                                 int(item.get("baseline", 0)))
                    elif field == "permission_mode":
                        newer = (int(getattr(tail, "permission_mode_evidence_offset", 0) or 0) >
                                 int(item.get("baseline", 0)))
                    else:
                        baseline = int(item.get("baseline", 0) or 0)
                        newer = bool(
                            (getattr(tail, "effort", "") and baseline > 0 and
                             int(getattr(tail, "effort_evidence_offset", 0) or 0) > baseline)
                            or (native_effort and effort_mtime >
                                float(record.get("attempted_at", 0))))
                    resolved = resolved and newer
                if resolved:
                    uncertain.pop(sid, None)
                    self._claude_control_uncertain = uncertain
                    changed_uncertain = True
            if changed_overrides:
                self._persist_private_runtime_map("claude_control_overrides", overrides)
            if changed_uncertain:
                self._persist_private_runtime_map("claude_control_uncertain", uncertain)
        return self._claude_control_uncertain.get(sid)

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
            stat = os.stat(pathcfg.CLAUDE_ACCOUNT)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        cached = getattr(self, "_account_email", None)
        if cached and cached[0] == signature:
            return cached[1]
        email = None
        try:
            with open(pathcfg.CLAUDE_ACCOUNT) as f:
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
            stat = os.stat(pathcfg.CLAUDE_USAGE_PREFS)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = getattr(self, "_claude_usage_profiles", None)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            with open(pathcfg.CLAUDE_USAGE_PREFS, "rb") as handle:
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
            stat = os.stat(pathcfg.CLAUDE_STATS)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = getattr(self, "_claude_stats_cache", None)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            with open(pathcfg.CLAUDE_STATS) as f:
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
            with open(pathcfg.CLAUDE_USAGE) as f:
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
                # the child's own assistant rows carry the ACTUAL effort
                # (≥2.1.217); the frontmatter pin / parent fallback in
                # agent_effort covers older transcripts
                "transcript_effort": t.effort or None,
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
