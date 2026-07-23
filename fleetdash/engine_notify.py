"""Notifications, Web Push, and Outbox actions/dispatch (invariants 44, 46-48, 61)."""
import os, re, sys, time, copy
from .outbox import OutboxError
from .briefing import OperationsError
from .web_push import WebPushService


from . import paths as pathcfg
from .config import cwd_to_project_dir




class NotifyOps:

    def outbox_usage_options(self):
        """Return stable, display-safe account/window choices for reset triggers."""
        with self.lock:
            providers = copy.deepcopy(self.snapshot_cache.get("provider_usage") or {})
        options = []
        claude = providers.get("claude") or {}
        for profile in claude.get("profiles") or ([claude] if claude else []):
            account_id = str(profile.get("id") or profile.get("email") or "active")
            label = profile.get("email") or profile.get("name") or "Active Claude account"
            windows = []
            for window_id, name, field in (
                    ("five_hour", "5-hour", "five_hour_reset"),
                    ("weekly", "Weekly", "weekly_reset")):
                if profile.get(field):
                    windows.append({"id": window_id, "label": name,
                                    "reset": profile.get(field)})
            if windows:
                options.append({"provider": "claude", "account_id": account_id,
                                "label": label, "windows": windows})
        codex = providers.get("codex") or {}
        buckets = [{"id": str(item.get("id")), "label": item.get("label") or item.get("id"),
                    "reset": item.get("reset")} for item in codex.get("buckets") or []
                   if item.get("id") and item.get("reset")]
        if buckets:
            options.append({"provider": "codex",
                "account_id": str(codex.get("account_id") or codex.get("email") or "active"),
                "label": codex.get("email") or "Active Codex account", "windows": buckets})
        return options

    def outbox_snapshot(self, state=None, cursor=0, limit=100):
        try:
            result = self.outbox.list(state=state, cursor=cursor, limit=limit)
            result["summary"] = self.outbox.counts()
            result["usage_options"] = self.outbox_usage_options()
            return result
        except OutboxError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:
            return {"ok": False, "error": f"outbox is temporarily unavailable: {exc}"}

    def briefing_snapshot(self, device_id="default", cursor=None, limit=100):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        try:
            return self.operations.briefing_snapshot(
                snapshot, device_id=device_id or "default", cursor=cursor, limit=limit)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"briefing is temporarily unavailable: {exc}"}

    def notifications_snapshot(self, device_id="default", cursor=None, limit=100,
                               states=None, kinds=None, event_id=None):
        try:
            return self.operations.notification_snapshot(
                device_id=device_id or "default", cursor=cursor, limit=limit,
                states=states, kinds=kinds, event_id=event_id)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"notifications are temporarily unavailable: {exc}"}

    def push_config(self, device_id=None):
        try:
            devices = self.operations.notification_devices_snapshot(device_id)
            with self.web_push_lock:
                runtime = self.web_push.status() if self.web_push else {
                    "configured": False, "public_key": None, "delivery": "starting",
                    "helper": {"ready": False, "restarts": 0, "state": "starting"},
                    "queue": self.operations.notification_delivery_diagnostics()}
            return {"ok": True, "feature": "production",
                    "configured": bool(runtime.get("configured")),
                    "public_key": runtime.get("public_key"),
                    "delivery": runtime.get("delivery") or "unavailable",
                    "helper": runtime.get("helper"), "queue": runtime.get("queue"),
                    "current_device": devices.get("current_device"),
                    "devices": devices.get("devices") or [],
                    "registered_devices": devices.get("registered", 0),
                    "enabled_devices": devices.get("enabled", 0)}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push configuration is temporarily unavailable"}

    def push_devices(self, current_device_id=None):
        try:
            return self.operations.notification_devices_snapshot(current_device_id)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push devices are temporarily unavailable"}

    def push_diagnostics(self):
        with self.web_push_lock:
            runtime = self.web_push.status() if self.web_push else {
                "configured": False, "delivery": "starting",
                "helper": {"ready": False, "restarts": 0, "state": "starting"},
                "queue": self.operations.notification_delivery_diagnostics()}
        return {key: runtime.get(key) for key in
                ("configured", "delivery", "helper", "runtime", "queue")}

    def legacy_ntfy_diagnostics(self):
        out = self.operations.legacy_notification_diagnostics()
        return {"enabled": self.cfg.get("legacy_ntfy_enabled") is True,
                "configured": bool(self.cfg.get("ntfy_topic") and
                                   self.cfg.get("ntfy_server")), **out}

    def push_subscription(self, payload):
        try:
            if payload.get("forget"):
                device = self.operations.notification_forget_device(
                    payload.get("device_id"))
            elif payload.get("remove"):
                device = self.operations.notification_remove_device(
                    payload.get("device_id"), payload.get("permission_state") or "expired")
            else:
                device = self.operations.notification_register_device(
                    payload.get("device_id"), payload.get("display_name"),
                    payload.get("platform"), payload.get("subscription"),
                    permission_state=payload.get("permission_state") or "granted",
                    preferences=payload.get("preferences"),
                    allowed_origins=self.cfg.get("web_push_allowed_origins") or [])
            return {"ok": True, "device": device}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push subscription could not be saved"}

    def push_device_settings(self, payload):
        try:
            device = self.operations.notification_update_device(
                payload.get("device_id"),
                display_name=payload.get("display_name") if "display_name" in payload else None,
                enabled=payload.get("enabled") if "enabled" in payload else None,
                preferences=payload.get("preferences") if "preferences" in payload else None)
            return {"ok": True, "device": device}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "push device settings could not be saved"}

    def notifications_mark_read(self, payload):
        try:
            cursor = self.operations.notification_mark_read(
                payload.get("device_id"), payload.get("cursor"))
            return {"ok": True, "cursor": cursor}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification read state could not be saved"}

    def notifications_snooze(self, payload):
        try:
            until = self.operations.notification_snooze(
                payload.get("event_id"), payload.get("source_revision"), payload.get("until"))
            return {"ok": True, "until": until}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification could not be snoozed"}

    def notifications_wake(self, payload):
        try:
            self.operations.notification_wake(
                payload.get("event_id"), payload.get("source_revision"))
            return {"ok": True}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification could not be woken"}

    def notifications_mute(self, payload):
        try:
            event = self.operations.notification_snapshot(
                payload.get("device_id") or "default", event_id=payload.get("event_id"))
            item = (event.get("events") or [None])[0]
            if (not item or item.get("source_revision") != payload.get("source_revision") or
                    not item.get("session_id")):
                raise OperationsError("notification event is stale")
            muted = payload.get("muted")
            if not isinstance(muted, bool):
                raise OperationsError("invalid notification mute state")
            saved = self.update_settings({"mute_session": item["session_id"], "muted": muted})
            if not saved.get("ok"):
                raise OperationsError(saved.get("error") or "notification mute failed")
            self.operations.notification_set_session_mute(
                item["session_id"], item.get("provider"), muted)
            return {"ok": True, "muted": muted, "session_id": item["session_id"]}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification mute could not be saved"}

    def notifications_retry(self, payload):
        try:
            delivery = self.operations.notification_retry_delivery(payload.get("delivery_id"))
            with self.web_push_lock:
                if self.web_push:
                    self.web_push.wake_event.set()
            return {"ok": True, "delivery": delivery}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "notification delivery could not be retried"}

    def notification_policy_snapshot(self):
        try:
            return self.operations.notification_policy_snapshot()
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            print(f"notification policy snapshot failed: {exc}", file=sys.stderr, flush=True)
            return {"ok": False, "error": "notification policy is temporarily unavailable"}

    def notification_policy_update(self, payload):
        try:
            result = self.operations.notification_policy_update(payload)
            with self.web_push_lock:
                if self.web_push:
                    self.web_push.wake_event.set()
            return result
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            print(f"notification policy update failed: {exc}", file=sys.stderr, flush=True)
            return {"ok": False, "error": "notification policy could not be saved"}

    def start_web_push(self):
        """Start the isolated delivery runtime without delaying daemon availability."""
        with self.web_push_lock:
            if self.web_push is None:
                self.web_push = WebPushService(self.operations, pathcfg.BASE, self.cfg)
            self.web_push.start()

    def push_capability_action(self, payload):
        """Apply one signed reversible push action without using the act token."""
        token = payload.get("capability") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            return {"ok": False, "error": "notification capability is unavailable"}

        def persist_mute(session_id):
            values = dict(self.cfg.get("muted_sessions") or {})
            values.pop(session_id, None)
            values[session_id] = time.time()
            values = dict(list(values.items())[-1000:])
            self._persist_config_fields({"muted_sessions": values})
            self.cfg["muted_sessions"] = values

        try:
            with self.web_push_lock:
                service = self.web_push
            if not service:
                raise OperationsError("notification capability is unavailable")
            with self.config_lock:
                result = service.capability_action(token, mute_callback=persist_mute)
            return {"ok": True, **result}
        except Exception:
            return {"ok": False, "error": "notification capability is unavailable"}

    def push_test(self, payload):
        try:
            with self.web_push_lock:
                service = self.web_push
            if not service or not service.status().get("configured"):
                return {"ok": False, "error": "Web Push delivery is not ready",
                        "code": "delivery_unavailable"}
            delivery = service.enqueue_test(payload.get("device_id"))
            return {"ok": True, "delivery": delivery}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "test delivery could not be queued"}

    def budgets_snapshot(self, spawn=None):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        try:
            prepared = dict(spawn or {})
            if prepared.get("cwd"):
                prepared["workstream_id"] = self.workstream_identity(
                    prepared["cwd"]).get("workstream_id")
            return self.operations.budgets_snapshot(snapshot, spawn=prepared or None)
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"budgets are temporarily unavailable: {exc}"}

    def briefing_action(self, action):
        try:
            cursor = self.operations.review(action.get("device_id"), action.get("cursor"))
            return {"ok": True, "cursor": cursor}
        except OperationsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"briefing review failed: {exc}"}

    def _outbox_current_target(self, payload):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        record = {
            "target_provider": payload.get("target_provider"),
            "target_session_id": payload.get("target_session_id"),
            "target_agent_id": payload.get("target_agent_id"),
            "destination_session_id": payload.get("destination_session_id"),
            "updated_at": time.time() - 300,
        }
        return self.outbox._target_status(record, snapshot)

    def _validate_outbox_spawn(self, spec):
        if not isinstance(spec, dict):
            raise OutboxError("new-session settings are required")
        provider = str(spec.get("provider") or "")
        if provider not in ("claude", "codex"):
            raise OutboxError("unknown provider")
        cwd = os.path.realpath(os.path.expanduser(str(spec.get("cwd") or "").strip()))
        home = os.path.realpath(pathcfg.HOME)
        if not cwd or not os.path.isdir(cwd):
            raise OutboxError("no such directory")
        if cwd != home and not cwd.startswith(home + os.sep):
            raise OutboxError("directory must be under your home folder")
        model = str(spec.get("model") or "").strip()
        effort = str(spec.get("effort") or "").strip()
        if provider == "claude":
            if model and model not in self.MODELS:
                raise OutboxError("unknown Claude model")
            if effort and effort not in self.EFFORTS:
                raise OutboxError("unknown effort level")
            permission_mode = str(spec.get("permission_mode") or "default")
            if permission_mode not in self.CLAUDE_START_PERMISSION_MODES:
                raise OutboxError("unknown Claude permission mode")
        else:
            permission_mode = ""
            catalog = {item.get("id"): item for item in self.codex.models}
            if model and model not in catalog:
                raise OutboxError("unknown Codex model")
            allowed = (catalog.get(model) or {}).get("efforts") or []
            if effort and allowed and effort not in allowed:
                raise OutboxError("unsupported Codex effort level")
            if str(spec.get("mode") or "plan") not in ("plan", "default"):
                raise OutboxError("Codex mode must be plan or default")
        name = str(spec.get("worktree_name") or "").strip()
        if name and not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
            raise OutboxError("worktree name: letters, digits, . _ - only")
        if spec.get("worktree") and not os.path.exists(os.path.join(cwd, ".git")):
            raise OutboxError("new worktree requires a Git repository")
        return {"provider": provider, "cwd": cwd, "model": model, "effort": effort,
                "mode": str(spec.get("mode") or "plan"),
                "permission_mode": permission_mode,
                "worktree": bool(spec.get("worktree")), "worktree_name": name}

    def _prepare_outbox_payload(self, payload):
        prepared = dict(payload or {})
        if prepared.get("kind") == "new_session" or prepared.get("spawn_spec"):
            spawn_spec = dict(prepared.get("spawn_spec") or {})
            if self.is_staging:
                source = self._staging_source_root()
                if not source:
                    raise OutboxError("staging source checkout is unavailable")
                spawn_spec.update(cwd=source, worktree=False)
            prepared["spawn_spec"] = self._validate_outbox_spawn(spawn_spec)
        elif prepared.get("target_session_id"):
            if self.is_staging and not self._staging_owns(prepared.get("target_session_id")):
                raise OutboxError("production sessions are view only in staging")
            status, reason, _ = self._outbox_current_target(prepared)
            if status == "block":
                raise OutboxError(reason or "target is unavailable")
        if prepared.get("kind") == "usage_reset":
            options = self.outbox_usage_options()
            account = next((item for item in options
                if item["provider"] == prepared.get("target_provider") and
                   item["account_id"] == str(prepared.get("usage_account_id") or "")), None)
            window = next((item for item in (account or {}).get("windows", [])
                if item["id"] == str(prepared.get("usage_window_id") or "")), None)
            if not window:
                raise OutboxError("fresh evidence for that usage reset is unavailable")
            prepared["observed_reset_at"] = window["reset"]
        return prepared

    def outbox_action(self, action):
        typ = str(action.get("type") or "")
        outbox_id = str(action.get("outbox_id") or "")
        try:
            if typ == "outbox_create":
                item = self.outbox.create(self._prepare_outbox_payload(action))
            elif typ == "outbox_update":
                item = self.outbox.update(outbox_id,
                    self._prepare_outbox_payload(action.get("patch") or {}))
            elif typ == "outbox_cancel":
                item = self.outbox.cancel(outbox_id)
            elif typ == "outbox_delete":
                item = self.outbox.dismiss(outbox_id)
            elif typ == "outbox_send_now":
                item = self.outbox.send_now(outbox_id)
            elif typ == "outbox_retry":
                patch = action.get("patch") or {}
                item = self.outbox.retry(outbox_id,
                    self._prepare_outbox_payload(patch) if patch else None)
            elif typ == "outbox_retarget":
                item = self.outbox.retarget(outbox_id,
                    self._prepare_outbox_payload(action.get("patch") or {}))
            else:
                return {"ok": False, "error": "unknown outbox action"}
            return {"ok": True, "item": item, "summary": self.outbox.counts()}
        except OutboxError as exc:
            result = {"ok": False, "error": str(exc), "code": exc.code}
            if hasattr(exc, "choices"):
                result["choices"] = exc.choices
            return result
        except Exception as exc:
            return {"ok": False, "error": f"outbox action failed: {exc}"}

    def _outbox_dispatch(self, record):
        sid = record.get("destination_session_id") or record.get("target_session_id")
        image_paths = list(record.get("_image_paths") or [])
        action = {"type": "relay", "session_id": sid,
                  "agent_id": record.get("target_agent_id"),
                  "text": record.get("message")} if record.get("target_agent_id") else {
                  "type": "image_text" if image_paths else "text",
                  "session_id": sid, "text": record.get("message")}
        if image_paths:
            action["image_paths"] = image_paths
        # Queue-owned image paths are server-internal and already confined by
        # OutboxManager. Do not send them through public act(), which accepts
        # opaque upload IDs only. An exact attached Codex TUI can still receive
        # those paths through the same terminal transport as direct messages.
        if record.get("target_provider") == "codex":
            with self.lock:
                projected = next((item for item in
                    self.snapshot_cache.get("sessions") or []
                    if item.get("session_id") == sid and item.get("provider") == "codex"), None)
            route = (self._codex_terminal_route(self.codex.native(sid), force=True)
                     if projected and not projected.get("read_only") else None)
            result = (self._write_codex_terminal(action, route) if route else
                      self.codex.act(action))
        elif image_paths:
            with self._claude_mutation_lock(sid):
                result = self._write_claude_queued_message(action)
        else:
            result = self.act(action)
        return {"ok": bool(result.get("ok")), "provider": record.get("target_provider"),
                "session_id": sid, "accepted": bool(result.get("ok")),
                "error": result.get("error"), "code": result.get("code"),
                "queueable": bool(result.get("queueable"))}

    def _write_claude_queued_message(self, action):
        """Deliver queue-owned image paths without accepting client file paths."""
        sid = str(action.get("session_id") or "")
        reg = next((item for item in self.live_sessions()
                    if item.get("sessionId") == sid), None)
        if not reg:
            return {"ok": False, "error": "session not live", "queueable": True}
        if reg.get("status") != "idle":
            return {"ok": False,
                    "error": "Claude is no longer available; keep the message queued",
                    "code": "provider_control_unavailable", "queueable": True}
        path = os.path.join(cwd_to_project_dir(reg.get("cwd", "")), f"{sid}.jsonl")
        with self.scan_lock:
            mt = self.tail_for(path)
            mt.poll()
            if (self.hook_pending(sid, reg.get("status")) is not None or mt.pending or
                    self.compacting_secs(sid, reg.get("cwd", ""), mt) is not None or
                    self._claude_turn_fenced(sid, reg.get("status"), path, mt)):
                return {"ok": False,
                        "error": "Claude is waiting or compacting; keep the message queued",
                        "code": "provider_control_unavailable", "queueable": True}
        paths = [str(path) for path in (action.get("image_paths") or []) if path]
        if not paths:
            return {"ok": False, "error": "no images"}
        text = str(action.get("text") or "")[:2000].strip()
        text = text or ("Please inspect the attached image." if len(paths) == 1 else
                        "Please inspect the attached images.")
        text += "\n\nImages attached through Fleet:\n" + "\n".join(
            f"- {path}" for path in paths)
        if text.startswith("/") and " " not in text:
            text += " "
        steps = [(text, True)]
        try:
            transcript_size = os.path.getsize(path)
        except OSError:
            transcript_size = None
        baseline = {"transcript_size": transcript_size,
                    "convo_rev": getattr(mt, "convo_rev", None)}
        if self._is_background_claude(reg):
            result = self._write_background_claude(reg, steps, 0.05)
        else:
            tty = self._tty_for_pid(reg["pid"])
            if not tty:
                return {"ok": False, "error": "session has no terminal (VS Code / headless)"}
            result = self._iterm_write(f"/dev/{tty}", steps, step_delay=0.05)
        if result.get("ok"):
            self._record_claude_turn_fence(sid, baseline)
        return result

    @staticmethod
    def _message_can_send_now(session):
        """Return true only when provider acceptance means immediate delivery."""
        if (not session or session.get("pending") or session.get("stale") or
                session.get("provider_stale")):
            return False
        capabilities = session.get("capabilities") or {}
        if not capabilities.get("submit"):
            return False
        active = (session.get("compacting") is not None or
                  session.get("state") in
                  ("running", "stalled", "needs_you", "stalled_or_prompt"))
        # Now placement is an action queue, not provider availability. An idle
        # session can be in Needs you solely because its last prose asks for a
        # reply; that reply must start immediately instead of waiting on itself.
        if not active:
            return True
        # Fleet-owned App Server turns can be steered immediately. An attached
        # terminal accepts typing while busy but holds it for later, so it must
        # use the durable availability queue instead of pretending it was sent.
        return (session.get("provider") == "codex" and
                session.get("control_state") == "connected_active")

    def _queue_when_available(self, action, provider, reason=None):
        image_paths = list(action.get("image_paths") or [])
        message = str(action.get("text") or "").strip()
        if not message and image_paths:
            message = ("Please inspect the attached image." if len(image_paths) == 1 else
                       "Please inspect the attached images.")
        try:
            item = self.outbox.create_delivery(
                message=message, target_provider=provider,
                target_session_id=str(action.get("session_id") or ""),
                idempotency_key=action.get("client_request_id"),
                image_paths=image_paths, kind="when_available",
                origin="automatic_fallback")
            wait_reason = reason or "Waiting for the session to become available"
            return {"ok": True, "queued": True, "outbox_id": item["id"],
                    "queue_state": item["state"],
                    "message": "Queued · waiting for session",
                    "queue_reason": wait_reason}
        except OutboxError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:
            print(f"Automatic delivery queue failed: {exc}", file=sys.stderr, flush=True)
            return {"ok": False, "error": "message could not be saved to the Outbox"}

    def _send_now_or_queue(self, action):
        """Choose immediate delivery or a durable exact-session queue server-side."""
        sid = str(action.get("session_id") or "")
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        session = next((item for item in snapshot.get("sessions") or []
                        if str(item.get("session_id") or "") == sid), None)
        provider = self._known_message_provider(sid, session)
        if not provider:
            return {"ok": False, "error": "session not live"}
        record = {"kind": "when_available", "target_provider": provider,
                  "target_session_id": sid, "updated_at": time.time() - 300}
        target, reason, _ = self.outbox._target_status(record, snapshot)
        if target == "block":
            return {"ok": False, "error": reason or "session cannot accept messages"}
        if not self._message_can_send_now(session):
            return self._queue_when_available(action, provider, reason)

        direct = {"type": action.get("type"), "session_id": sid,
                  "text": action.get("text"),
                  "client_request_id": action.get("client_request_id")}
        if action.get("type") == "image_text":
            direct["upload_ids"] = list(action.get("upload_ids") or [])
        result = self.act(direct)
        if result.get("ok"):
            if result.get("queued"):
                return result
            return {**result, "queued": False, "delivery": "sent_now",
                    "message": "Sent now"}
        if result.get("queueable") or result.get("code") == "provider_control_unavailable":
            return self._queue_when_available(action, provider, result.get("error"))
        return result

    def _dismiss_question_then_send(self, action):
        """Decline one exact question, then durably send after its TUI is gone.

        The follow-up is always queued after provider acceptance of the dismiss.
        Writing it immediately would recreate the transition race where text is
        interpreted as a choice by a native question selector.
        """
        sid = str(action.get("session_id") or "")
        nonce = str(action.get("nonce") or "")
        if not nonce:
            return {"ok": False, "error": "question nonce is required"}
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        session = next((item for item in snapshot.get("sessions") or []
                        if str(item.get("session_id") or "") == sid), None)
        provider = self._known_message_provider(sid, session)
        if not provider or not session:
            return {"ok": False, "error": "session not live"}
        pending = session.get("pending")
        if pending and (pending.get("kind") != "question" or
                        str(pending.get("nonce") or "") != nonce):
            return {"ok": False,
                    "error": "the pending request changed — review it before sending"}

        dismissed = self.act({"type": "dismiss", "session_id": sid,
                              "nonce": nonce})
        if not dismissed.get("ok"):
            return dismissed
        queued = self._queue_when_available(
            action, provider,
            "Question dismissed; waiting for the session to become available")
        if not queued.get("ok"):
            return queued
        return {**queued, "dismissed": True, "dismissed_nonce": nonce}

    def _queue_codex_recovery(self, action):
        image_paths = list(action.get("image_paths") or [])
        message = str(action.get("text") or "").strip()
        if not message and image_paths:
            message = ("Please inspect the attached image." if len(image_paths) == 1 else
                       "Please inspect the attached images.")
        try:
            item = self.outbox.create_recovery(
                message=message,
                target_provider="codex",
                target_session_id=str(action.get("session_id") or ""),
                idempotency_key=action.get("client_request_id"),
                image_paths=image_paths)
            return {"ok": True, "queued": True, "outbox_id": item["id"],
                    "queue_state": item["state"],
                    "message": "Queued until Codex control reconnects"}
        except OutboxError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:
            print(f"Codex recovery queue failed: {exc}", file=sys.stderr, flush=True)
            return {"ok": False, "error": "message could not be saved to the recovery queue"}

    def _write_codex_terminal(self, action, route):
        """Type a bounded message into one server-discovered attached Codex TUI."""
        if not route or not route.get("tty"):
            return {"ok": False, "error": "attached Codex terminal is unavailable",
                    "code": "provider_control_unavailable", "queueable": True}
        typ = str(action.get("type") or "")
        if typ not in ("text", "image_text"):
            return {"ok": False, "error": "unsupported Codex terminal action"}
        text = str(action.get("text") or "")[:2000].strip()
        if typ == "image_text":
            paths = [str(path) for path in (action.get("image_paths") or []) if path]
            if not paths:
                return {"ok": False, "error": "no images"}
            text = text or ("Please inspect the attached image." if len(paths) == 1 else
                            "Please inspect the attached images.")
            text += "\n\nImages attached through Fleet:\n" + "\n".join(
                f"- {path}" for path in paths)
        if not text:
            return {"ok": False, "error": "empty text"}
        # Keep the same TUI popup guard as Claude terminal injection.
        if text.startswith("/") and " " not in text:
            text += " "
        result = self._iterm_write(route["tty"], [(text, True)], step_delay=0.05)
        if result.get("ok"):
            result.update(transport="codex_terminal",
                          session_id=str(action.get("session_id") or ""),
                          accepted=True)
        return result

    def _outbox_spawn(self, record):
        if record.get("kind") == "resume_session":
            sid = str(record.get("target_session_id") or "")
            provider = str(record.get("target_provider") or "")
            if provider == "codex":
                result = self.codex.resume_owned_thread(sid)
            elif provider == "claude":
                result = self.reopen_claude_session(sid)
            else:
                result = {"ok": False, "error": "unknown session provider"}
            return {"ok": bool(result.get("ok")), "provider": provider,
                    "session_id": result.get("session_id") or sid,
                    "message_delivered": False, "accepted": bool(result.get("ok")),
                    "error": result.get("error")}
        spec = dict(record.get("spawn_spec") or {})
        provider = spec.get("provider")
        if provider == "codex":
            result = self.spawn_codex_session({**spec, "initial_text": record.get("message")})
            return {"ok": bool(result.get("ok")), "provider": "codex",
                    "session_id": result.get("session_id"),
                    "message_delivered": bool(result.get("ok")),
                    "accepted": bool(result.get("ok")), "error": result.get("error")}
        result = self.spawn_session(spec, reserved_sid=record.get("destination_session_id"))
        return {"ok": bool(result.get("ok")), "provider": "claude",
                "session_id": result.get("session_id"), "message_delivered": False,
                "accepted": bool(result.get("ok")), "error": result.get("error"),
                "trust_prompt": bool(result.get("trust_prompt"))}

    def run_outbox(self):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot_cache)
        usage = copy.deepcopy(snapshot.get("provider_usage") or {})
        self.outbox.tick(snapshot, usage, self._outbox_dispatch, self._outbox_spawn)

    def resume_and_send(self, action):
        """Durably resume one exact closed session and deliver one text message.

        The browser-provided request ID is the idempotency key. Repeating the HTTP
        request therefore returns the same queue item instead of creating a second
        provider turn.
        """
        sid = str(action.get("session_id") or "")
        message = str(action.get("text") or "").strip()
        request_id = str(action.get("client_request_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "missing session ID"}
        if not message or len(message) > 2000:
            return {"ok": False, "error": "message must be 1–2,000 characters"}
        if not 8 <= len(request_id) <= 160:
            return {"ok": False, "error": "missing or invalid request ID"}
        allowed, reason = self.closed_resume_capability(sid)
        if not allowed:
            return {"ok": False, "error": reason or "session cannot be resumed"}
        provider = "codex" if sid.startswith("codex:") else "claude"
        try:
            item = self.outbox.create_closed_resume(
                message=message, target_provider=provider,
                target_session_id=sid, idempotency_key=request_id)
            self.run_outbox()
            item = self.outbox.get(item["id"]) or item
            if item.get("state") in ("failed", "blocked", "cancelled"):
                return {"ok": False, "error": item.get("error") or
                        item.get("reason") or "session resume failed",
                        "outbox_id": item.get("id"), "queue_state": item.get("state")}
            return {"ok": True, "queued": item.get("state") != "sent",
                    "outbox_id": item.get("id"), "queue_state": item.get("state"),
                    "session_id": sid, "accepted": True}
        except OutboxError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:
            print(f"closed resume queue failed for {sid}: {exc}", file=sys.stderr,
                  flush=True)
            return {"ok": False, "error": "session resume could not be queued"}
