"""Spawn/handoff composition, iTerm applet exchange, settings updates
(invariants 20, 21, 24)."""
import os, re, sys, time, shlex, secrets, subprocess, threading, urllib.request, hashlib, copy, uuid
from .briefing import OperationsError


from . import paths as pathcfg
from .placement import redact_handoff_text




class SpawnOps:

    def _create_codex_worktree(self, cwd, requested_name=""):
        identity = self.workstream_identity(cwd)
        root = identity.get("root") if identity.get("kind") == "git" else None
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": "new worktree requires a Git repository"}
        name = str(requested_name or "").strip() or f"handoff-{secrets.token_hex(4)}"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
            return {"ok": False, "error": "worktree name: letters, digits, . _ - only"}
        repo_key = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]
        parent = os.path.join(pathcfg.HOME, ".claude", "fleet-dash-worktrees", repo_key)
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
            uncertain = result.get("code") == "delivery_uncertain"
            status = ("delivered" if result.get("ok") else
                      "confirmation_unknown" if uncertain else "delivery_failed")
            self._record_handoff_link(source_sid, source.get("provider") or "claude",
                retry_sid, target, status, preview_hash, result.get("error"))
            return {**result, "source_session_id": source_sid,
                    "destination_session_id": retry_sid, "provider": target,
                    "created": False,
                    "retryable": not result.get("ok") and not uncertain}

        cwd = os.path.realpath(os.path.expanduser(
            str(action.get("cwd") or source.get("cwd") or "").strip()))
        home = os.path.realpath(pathcfg.HOME)
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
        uncertain = delivered.get("code") == "delivery_uncertain"
        status = ("delivered" if delivered.get("ok") else
                  "confirmation_unknown" if uncertain else "delivery_failed")
        self._record_handoff_link(source_sid, source.get("provider") or "claude",
            destination_sid, target, status, preview_hash, delivered.get("error"))
        return {**delivered, "source_session_id": source_sid,
                "destination_session_id": destination_sid, "session_id": destination_sid,
                "provider": target, "cwd": cwd, "created": True,
                "retryable": not delivered.get("ok") and not uncertain}

    def focus_codex_terminal(self, action):
        """Focus one exact existing TUI; never create or resume a terminal."""
        sid = str(action.get("session_id") or "")
        tid = self.codex.native(sid)
        session = next((item for item in self.codex.sessions()
                        if item.get("session_id") == sid), None)
        if not session or session.get("read_only"):
            return {"ok": False, "error": "this Codex thread is view only"}
        route = self._codex_terminal_route(tid, force=True)
        if not route:
            return {"ok": False, "error": "no attached Codex terminal is available"}
        result = self._terminal_write(
            route["tty"], [("__FOCUS__", False)], step_delay=0.05)
        if result.get("ok"):
            result.update(session_id=sid, shared_runtime=True,
                          transport="codex_terminal", focused=True)
        return result

    def spawn_codex_session(self, action):
        """Create a Codex thread through app-server; no terminal or TUI scraping."""
        if self.is_staging and not action.get("__staging_internal"):
            return self._spawn_staging_session(action, "codex")
        cwd = os.path.realpath(os.path.expanduser(str(action.get("cwd") or "").strip()))
        home = os.path.realpath(pathcfg.HOME)
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
        """Start a NEW Claude Code session in a fresh terminal.

        Every value that reaches the shell is allowlisted or quoted: the model and
        effort and permission mode must be members of fixed sets above, the worktree
        name is regex-bounded, and the directory must be an existing dir under $HOME. Nothing the
        client sends is interpolated raw — the act token opens a terminal here, so a
        free-form command string would be a remote shell."""
        if self.is_staging and not action.get("__staging_internal"):
            return self._spawn_staging_session(action, "claude", reserved_sid=reserved_sid)
        cwd = os.path.realpath(os.path.expanduser(str(action.get("cwd") or "").strip()))
        home = os.path.realpath(pathcfg.HOME)
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
        r = self._terminal_spawn(cmd, label="claude")
        if r.get("ok"):
            print(f"spawn ({r.get('transport') or 'applet'}): {cmd}",
                  file=sys.stderr, flush=True)
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
        with self._inject_lock:
            req_id = secrets.token_hex(8)
            lines = [tty, req_id]
            if step_delay is not None:  # flag 4: how long the applet waits BETWEEN keys
                lines.append(f"4 {step_delay}")
            for text, nl in steps:
                if text == "__FOCUS__":  # flag 3: select that tab, type nothing
                    lines.append("3 ")
                    continue
                if text:
                    lines.append("0 " + base64.b64encode(text.encode()).decode())
                if nl:                  # raw CR — raw-mode TUIs' Enter (LF toggles!)
                    lines.append("2 ")
            req_path = os.path.join(pathcfg.BASE, "inject-request.txt")
            res_path = os.path.join(pathcfg.BASE, "inject-result.txt")
            try:
                os.remove(res_path)
            except OSError:
                pass
            tmp_path = f"{req_path}.tmp-{req_id}"
            try:
                with open(tmp_path, "w") as f:
                    f.write("\n".join(lines))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, req_path)
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            app = os.path.join(pathcfg.BASE, "FleetDashInjector.app")
            try:
                launched = subprocess.run(
                    ["open", "-g", app], capture_output=True, text=True, timeout=10)
            except Exception as e:
                return {"ok": False, "code": "injector_not_launched",
                        "error": f"injector launch failed: {e}"}
            if launched.returncode:
                detail = (launched.stderr or launched.stdout or
                          "macOS refused to launch the injector")[:300]
                return {"ok": False, "code": "injector_not_launched",
                        "error": f"injector launch failed: {detail}"}
            deadline = time.time() + 30  # generous: first run includes the TCC dialog
            while time.time() < deadline:
                try:
                    with open(res_path) as result_handle:
                        out = result_handle.read().strip()
                    if out.startswith(req_id):
                        verdict = out[len(req_id):].strip()
                        if verdict == "ok":
                            return {"ok": True}
                        return {"ok": False, "code": "delivery_uncertain",
                                "error": ("delivery uncertain — the injector launched but "
                                          f"reported: {verdict[:240]}")}
                except OSError:
                    pass
                time.sleep(0.02)        # the applet is done in ~200ms — don't sleep past it
            return {"ok": False, "code": "delivery_uncertain",
                    "error": ("delivery uncertain — the injector launched but its result "
                              "was lost; check the terminal before retrying")}

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
