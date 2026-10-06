"""Staging-instance isolation: owned-session allowlist and masking (invariant 56)."""
import os, re, time, secrets, subprocess, copy


from . import paths as pathcfg




class StagingOps:

    @property
    def is_staging(self):
        return self.cfg.get("instance_mode") == "staging"

    def _ensure_codex_launcher(self):
        """Install routing only after the managed daemon is proven reachable."""
        if self.is_staging or not self.cfg.get("codex_enabled", True):
            return
        if (self.codex_launcher_status or {}).get("state") == "ready":
            return
        runtime = (self.codex.runtime_status()
                   if hasattr(self.codex, "runtime_status") else {})
        client = getattr(self.codex, "client", None)
        if (runtime.get("phase") != "committed" or
                getattr(client, "connection_state", None) != "ready"):
            return
        try:
            from .codex_launcher import install_launcher
            self.codex_launcher_status = install_launcher(
                source=os.path.join(os.path.dirname(__file__), "codex_launcher.py"),
                home=pathcfg.HOME)
        except Exception as exc:
            self.codex_launcher_status = {
                "installed": False, "shell_configured": False,
                "state": "repair_needed", "error": str(exc)[:300]}

    def _staging_owned(self):
        records = self.cfg.get("staging_owned_sessions") or {}
        return records if isinstance(records, dict) else {}

    def _staging_owns(self, sid):
        return bool(sid and str(sid) in self._staging_owned())

    def _register_staging_session(self, sid, provider, cwd):
        if not self.is_staging or not sid:
            return
        with self.config_lock:
            records = dict(self._staging_owned())
            records[str(sid)] = {
                "provider": str(provider), "cwd": os.path.realpath(str(cwd)),
                "created_at": time.time(),
            }
            if len(records) > 500:
                records = dict(sorted(records.items(),
                    key=lambda item: float((item[1] or {}).get("created_at") or 0))[-500:])
            self._persist_config_fields({"staging_owned_sessions": records})
            self.cfg["staging_owned_sessions"] = records

    def _staging_source_root(self):
        source = os.path.realpath(os.path.expanduser(
            str(os.environ.get(pathcfg.ENV_STAGING_SOURCE) or "")))
        if not source or not os.path.isdir(source) or not os.path.exists(
                os.path.join(source, ".git")):
            return None
        return source

    def _create_staging_workspace(self, requested_name=""):
        source = self._staging_source_root()
        if not source:
            return {"ok": False, "error": "staging source checkout is unavailable"}
        requested = str(requested_name or "").strip()
        if requested and not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", requested):
            return {"ok": False, "error": "worktree name: letters, digits, . _ - only"}
        stem = requested or "session"
        name = f"{stem}-{secrets.token_hex(4)}"
        parent = os.path.join(pathcfg.BASE, "workspaces")
        path = os.path.join(parent, name)
        branch = f"fleet-staging/{name}"
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            os.chmod(parent, 0o700, follow_symlinks=False)
            process = subprocess.run(
                ["git", "-C", source, "worktree", "add", "-b", branch, path, "HEAD"],
                capture_output=True, text=True, timeout=30)
        except Exception as exc:
            return {"ok": False, "error": f"could not create staging worktree: {exc}"}
        if process.returncode:
            detail = (process.stderr or process.stdout or "git worktree add failed").strip()
            return {"ok": False, "error": detail[:1000]}
        return {"ok": True, "cwd": path, "branch": branch, "worktree_name": name}

    def _spawn_staging_session(self, action, provider, reserved_sid=None):
        workspace = self._create_staging_workspace(action.get("worktree_name"))
        if not workspace.get("ok"):
            return workspace
        prepared = {**action, "cwd": workspace["cwd"], "worktree": False,
                    "worktree_name": "", "__staging_internal": True}
        result = (self.spawn_codex_session(prepared) if provider == "codex" else
                  self.spawn_session(prepared, reserved_sid=reserved_sid))
        if result.get("ok") and result.get("session_id"):
            self._register_staging_session(
                result["session_id"], provider, workspace["cwd"])
            result["staging_owned"] = True
            result["staging_workspace"] = workspace
        else:
            result.setdefault("staging_workspace", workspace)
        return result

    def _staging_mask_session(self, session):
        session = dict(session)
        owned = self._staging_owns(session.get("session_id"))
        session["staging_owned"] = owned
        session["staging_observer"] = not owned
        if owned:
            return session
        capabilities = dict(session.get("capabilities") or {})
        for key in ("submit", "interrupt", "takeover", "archive", "close", "compact",
                    "review", "focus_terminal", "answer_structured", "decide_approval",
                    "spawn_agent", "relay_agent", "relay_agent_direct", "reopen",
                    "change_permission_mode", "model_effort_settings",
                    "change_model_effort"):
            capabilities[key] = False
        # Staging can observe production work but must never manufacture an
        # actionable attention request for it. The session remains visible for
        # verification; reply/unread affordances belong only to its owner.
        session.update(capabilities=capabilities, read_only=True, access="view_only",
                       access_label="View only", primary_action="view",
                       primary_action_label="View",
                       reply_requested=False, new_response=False,
                       read_only_reason="Production session; staging can observe but not control it")
        if "can_reopen" in session:
            session["can_reopen"] = False
        return session

    def _staging_operations_fleet(self, fleet):
        if not self.is_staging:
            return fleet
        projected = copy.deepcopy(fleet)
        projected["sessions"] = [item for item in projected.get("sessions") or []
                                  if item.get("staging_owned")]
        projected["closed"] = [item for item in projected.get("closed") or []
                                if item.get("staging_owned")]
        owned = {item.get("session_id") for item in projected["sessions"] + projected["closed"]}
        projected["actions"] = [item for item in projected.get("actions") or []
                                if item.get("session_id") in owned]
        # Production provider failures belong to production. Staging notification
        # projection is intentionally limited to staging-owned test sessions.
        projected["providers"] = {key: {"ok": True}
                                  for key in (projected.get("providers") or {})}
        return projected

    def _staging_action_error(self, action):
        if not self.is_staging:
            return None
        typ = str(action.get("type") or "")
        if typ in ("ping", "spawn", "briefing_review") or typ.startswith("outbox_"):
            return None
        if self._staging_owns(action.get("session_id")):
            return None
        return {"ok": False, "error":
                "production session is view only in staging; create a staging test session"}
