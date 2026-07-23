"""Secondary-worktree preview/cleanup tickets (invariant 41)."""
import json, os, re, time, secrets, subprocess, hashlib, copy, selectors
from .repo_center import RepositoryOutcomeCenter






class WorktreeOps:

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
        if not status_result["ok"]:
            detail = status_result["stderr"] or "Git could not inspect the worktree"
            return {**base, "inspect_ok": False, "registered": True,
                    "reason": detail[:500]}

        status = RepositoryOutcomeCenter._parse_status(status_result["stdout"])
        files = status.get("files") or []
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
        # "Dirty" deliberately has Git's meaning: tracked changes and untracked
        # paths from `git status`. Ignored build output must not turn an otherwise
        # clean worktree into a force-removal flow.
        destructive_contents = dirty
        material = (root + "\0" + worktree + "\0" + listing["stdout"] + "\0" +
                    status_result["stdout"] + "\0" +
                    json.dumps(shared, sort_keys=True, separators=(",", ":")))
        revision = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        result = {**base, "inspect_ok": True, "registered": True,
                  "locked": bool(registered.get("locked")),
                  "owned_lock": owned_lock,
                  "branch": status.get("branch"), "head_oid": status.get("head_oid"),
                  "revision": revision, "dirty": dirty, "dirty_counts": dirty_counts,
                  "dirty_total": len(dirty_files), "dirty_files": dirty_files[:100],
                  "dirty_files_truncated": len(dirty_files) > 100,
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
            ticket = self._cleanup_tickets.get(token)
        if not ticket or ticket.get("expires", 0) <= time.time():
            with self._cleanup_lock:
                self._cleanup_tickets.pop(token, None)
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
            with self._cleanup_lock:
                self._cleanup_tickets.pop(token, None)
            return {"ok": False, "error": "the worktree changed after preview — it was preserved",
                    "worktree": ticket["worktree"], "preserved": True}
        if preview.get("root") != ticket["root"] or preview.get("worktree") != ticket["worktree"]:
            with self._cleanup_lock:
                self._cleanup_tickets.pop(token, None)
            return {"ok": False, "error": "worktree identity changed — it was preserved",
                    "worktree": ticket["worktree"], "preserved": True}
        allowed = preview.get("force_remove_allowed") if force else preview.get("remove_allowed")
        if not allowed:
            with self._cleanup_lock:
                self._cleanup_tickets.pop(token, None)
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
        with self._cleanup_lock:
            self._cleanup_tickets.pop(token, None)
        return {"ok": True, "removed": True, "forced": force,
                "worktree": ticket["worktree"], "branch_preserved": True}
