#!/usr/bin/env python3
"""Supported Codex CLI integration through ``codex app-server``.

The adapter owns the JSON-RPC process and exposes provider-neutral session,
conversation, and action shapes to Fleet Dash. It deliberately never parses
Codex rollout files; their on-disk representation is not a public API.
"""
import os
import re
import json
import time
import hashlib
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait

from .repo_center import observed_test_outcome
from .card_preview import card_peek_rows
from .codex_runtime import (
    CodexError,
    LEGACY_RUNTIME_OWNER,
    MANAGED_RUNTIME_OWNER,
)
from .codex_protocol import (
    CodexAppServer,
    _safe_json,
    _error_text,
    _is_limit_error,
)


EXTERNAL_OBSERVATION_SECONDS = 24 * 60 * 60
MAX_RECENT_EXTERNAL_OBSERVATIONS = 32


def _local_model_catalog(path, max_bytes=4 * 1024 * 1024):
    """Read Codex's bounded, credential-free local model catalog."""
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise CodexError("Codex model cache is unexpectedly large")
    payload = json.loads(raw)
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise CodexError("Codex model cache has no model catalog")
    by_id = {}
    order = []
    for model in entries[:100]:
        if not isinstance(model, dict):
            continue
        visibility = str(model.get("visibility") or "").strip().lower()
        if (model.get("hidden") is True or model.get("visible") is False or
                visibility in ("hidden", "hide", "none")):
            continue
        model_id = model.get("slug") or model.get("model") or model.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if (not model_id or len(model_id) > 128 or
                re.search(r"[\x00-\x1f\x7f]", model_id) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model_id)):
            continue
        efforts = []
        raw_efforts = (model.get("supported_reasoning_levels") or
                       model.get("supportedReasoningEfforts") or [])
        for effort in raw_efforts[:20] if isinstance(raw_efforts, list) else []:
            value = ((effort.get("effort") or effort.get("reasoningEffort"))
                     if isinstance(effort, dict) else effort)
            if not isinstance(value, str):
                continue
            value = value.strip()
            if (value and len(value) <= 32 and
                    not re.search(r"[\x00-\x1f\x7f]", value) and
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", value) and
                    value not in efforts):
                efforts.append(value)
        display = model.get("display_name") or model.get("displayName") or model_id
        if not isinstance(display, str):
            continue
        display = display.strip()
        if (not display or len(display) > 160 or
                re.search(r"[\x00-\x1f\x7f]", display)):
            continue
        if model_id not in by_id:
            order.append(model_id)
            context_window = model.get("context_window", model.get("contextWindow"))
            try:
                context_window = int(context_window)
            except (TypeError, ValueError, OverflowError):
                context_window = None
            if context_window is not None and not 1 <= context_window <= 2_000_000:
                context_window = None
            by_id[model_id] = {"id": model_id, "name": display, "efforts": []}
            if context_window is not None:
                by_id[model_id]["context_window"] = context_window
        for value in efforts:
            if value not in by_id[model_id]["efforts"]:
                by_id[model_id]["efforts"].append(value)
    out = [by_id[model_id] for model_id in order]
    if not out:
        raise CodexError("Codex model cache contains no usable models")
    return out


class CodexAdapter:
    PROVIDER = "codex"

    def __init__(self, enabled=True, client=None, state_path=None, clock=None,
                 stall_seconds=180, external_observer=None, dormant_seconds=7200,
                 models_cache_path=None, refresh_budget_seconds=1.5,
                 refresh_workers=4, runtime_owner=LEGACY_RUNTIME_OWNER,
                 runtime_migration=None):
        self.enabled = enabled
        self.client = client or CodexAppServer()
        self.state_path = state_path
        self.clock = clock or time.time
        self.stall_seconds = stall_seconds
        self.dormant_seconds = max(0, float(dormant_seconds))
        self.error = None
        self.error_at = None
        self._sessions = []
        self._refreshing = False
        self._last_refresh = 0
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._mutation_locks_guard = threading.Lock()
        self._mutation_locks = {}
        # Refresh commits and accepted provider mutations both replace the live
        # projection. This barrier plus settings revisions prevents a refresh
        # built from pre-save data from winning after a successful save.
        self._projection_commit_lock = threading.RLock()
        self.models = []
        self._model_error = None
        self._model_error_at = None
        self._models_cache_path = os.path.abspath(os.path.expanduser(
            models_cache_path or "~/.codex/models_cache.json"))
        self._models_cache_signature = None
        self._loaded_threads = set()
        self._loaded_generation = None
        self._loaded_error = None
        self._loaded_error_at = None
        self._account = None
        self._account_error = None
        self._account_error_at = None
        self._last_account_refresh = 0
        self._account_refreshing = False
        self._skills = {}
        self._detail_errors = {}
        self._detail_retry_after = {}
        self._refresh_budget_seconds = max(.05, float(refresh_budget_seconds))
        self._detail_executor = ThreadPoolExecutor(
            max_workers=max(1, min(8, int(refresh_workers))),
            thread_name_prefix="fleet-codex-detail")
        self._refresh_diagnostics = deque(maxlen=50)
        self.external_observer = external_observer
        self._tracked_external = set()
        self.runtime_owner = runtime_owner
        self.runtime_migration = runtime_migration
        self._runtime_action_lock = threading.RLock()

    def _context_window_for_model(self, model):
        for entry in self.models:
            if str(entry.get("id") or "") != str(model or ""):
                continue
            try:
                window = int(entry.get("context_window") or 0)
            except (TypeError, ValueError, OverflowError):
                return None
            return window if window > 0 else None
        return None

    def _owns_metadata(self, meta):
        return (meta or {}).get("runtime_owner") == self.runtime_owner

    def _mutation_lock(self, thread_id):
        with self._mutation_locks_guard:
            return self._mutation_locks.setdefault(thread_id, threading.RLock())

    @staticmethod
    def key(native_id):
        return f"codex:{native_id}"

    @staticmethod
    def native(key):
        return key.split(":", 1)[1] if str(key).startswith("codex:") else key

    def _owns_active_turn(self, thread_id):
        checker = getattr(self.client, "owns_active_turn", None)
        if checker:
            return bool(checker(thread_id))
        # Test/alternate clients predate connection generations. Production's
        # CodexAppServer always uses the strict generation check above.
        return bool(self.client.thread_state.get(thread_id, {}).get("turn_id"))

    def _settings_runtime_blocker(self, thread_id):
        """Return why native settings cannot change at this exact instant."""
        client_lock = getattr(self.client, "lock", None)
        if client_lock:
            with client_lock:
                live = dict(self.client.thread_state.get(thread_id, {}) or {})
                requests = [dict(item) for item in self.client.approvals.values()
                            if isinstance(item, dict)]
        else:
            live = dict(self.client.thread_state.get(thread_id, {}) or {})
            requests = [dict(item) for item in self.client.approvals.values()
                        if isinstance(item, dict)]
        pending = any(request.get("thread_id") == thread_id and
                      request.get("state", "pending") == "pending"
                      for request in requests)
        if live.get("compacting") is not None:
            return "Codex is compacting; wait for compaction to finish", live
        if pending:
            return "Codex is waiting on a request; answer it before changing settings", live
        if (self._owns_active_turn(thread_id) or live.get("status") == "running" or
                live.get("turn_id")):
            return "Codex model and effort can change only while the turn is idle", live
        return None, live

    def track_external(self, keys):
        """Choose external threads whose local lifecycle should be observed.

        Observation never changes ownership or capabilities. Fleet tracks only
        explicitly pinned threads so archive discovery remains cheap.
        """
        values = {self.native(key) for key in (keys or []) if str(key).startswith("codex:")}
        with self._lock:
            self._tracked_external = values

    def sessions(self):
        if not self.enabled:
            return []
        if self.runtime_migration:
            self.runtime_migration.maybe_migrate(self)
        with self._lock:
            if not self._refreshing and self.clock() - self._last_refresh >= 2:
                self._refreshing = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return [dict(s) for s in self._sessions]

    def _stale_session(self, session, reason):
        stale = {**session, "capabilities": dict(session.get("capabilities") or {})}
        previous_state = stale.get("state")
        if previous_state != "stale":
            stale["stale_previous_state"] = previous_state
        activity_at = stale.get("provider_activity_at")
        if activity_at is not None:
            stale["quiet_s"] = round(max(0, self.clock() - float(activity_at)))
        can_queue = not stale.get("read_only")
        stale.update(state="stale", stale=True, stale_reason=str(reason)[:1000],
                     control_state="reconnecting" if can_queue else "view_only",
                     queue_accepting=can_queue)
        capabilities = stale["capabilities"]
        for name in ("submit", "interrupt", "archive", "close", "compact", "review",
                     "answer_structured", "decide_approval", "relay_agent",
                     "change_model_effort"):
            capabilities[name] = False
        capabilities["queue_submit"] = can_queue
        return stale

    def _mark_refresh_failure(self, exc):
        now = self.clock()
        reason = str(exc)[:1000]
        with self._lock:
            self.error = reason
            self.error_at = now
            self._sessions = [self._stale_session(session, reason)
                              for session in self._sessions]
            self._refresh_diagnostics.append({"ts": now, "error": reason})

    @staticmethod
    def _thread_shape_error(thread):
        """Reject malformed provider rows before normalization can poison a pass."""
        if not isinstance(thread, dict):
            return "thread row is not an object"
        if not isinstance(thread.get("id"), str) or not thread.get("id"):
            return "thread row has no string id"
        if thread.get("gitInfo") is not None and not isinstance(thread.get("gitInfo"), dict):
            return "thread gitInfo is not an object"
        status = thread.get("status")
        if status is not None and not isinstance(status, (dict, str)):
            return "thread status has an invalid shape"
        if isinstance(status, dict):
            flags = status.get("activeFlags")
            if flags is not None and (not isinstance(flags, list) or
                                      any(not isinstance(flag, str) for flag in flags)):
                return "thread active flags have an invalid shape"
        turns = thread.get("turns")
        if turns is not None and not isinstance(turns, list):
            return "thread turns are not a list"
        for turn in turns or []:
            if not isinstance(turn, dict):
                return "thread contains a malformed turn"
            items = turn.get("items")
            if items is not None and not isinstance(items, list):
                return "thread items are not a list"
            for item in items or []:
                if not isinstance(item, dict):
                    return "thread contains a malformed item"
                if item.get("type") == "collabAgentToolCall":
                    states = item.get("agentsStates")
                    receivers = item.get("receiverThreadIds")
                    if states is not None and not isinstance(states, dict):
                        return "subagent states are not an object"
                    if receivers is not None and (not isinstance(receivers, list) or
                                                  any(not isinstance(aid, str)
                                                      for aid in receivers)):
                        return "subagent receiver ids are invalid"
                if item.get("type") == "subAgentActivity":
                    aid = item.get("agentThreadId")
                    path = item.get("agentPath")
                    if aid is not None and not isinstance(aid, str):
                        return "subagent id is invalid"
                    if path is not None and not isinstance(path, str):
                        return "subagent path is invalid"
        return None

    def _read_threads_bounded(self, thread_ids, deadline):
        ids = list(dict.fromkeys(str(tid) for tid in thread_ids if tid))
        if not ids:
            return {}, {}, set()
        if deadline <= time.monotonic():
            return {}, {}, set(ids)
        futures = {self._detail_executor.submit(self.client.read_thread, tid): tid
                   for tid in ids}
        remaining = max(0, deadline - time.monotonic())
        done, pending = wait(futures, timeout=remaining)
        results, errors = {}, {}
        for future in done:
            tid = futures[future]
            try:
                detail = future.result()
                shaped = ({"id": tid, **detail} if isinstance(detail, dict) else detail)
                shape_error = self._thread_shape_error(shaped)
                if shape_error:
                    errors[tid] = shape_error
                else:
                    results[tid] = detail
            except Exception as exc:
                errors[tid] = str(exc)[:1000]
        deferred = {futures[future] for future in pending}
        for future in pending:
            future.cancel()
        return results, errors, deferred

    def _enrich_agents_bounded(self, sessions, deadline):
        targets = {}
        for session in sessions:
            if session.get("stale"):
                continue
            for agent in session.get("agents") or []:
                # Child thread ids are immutable. Once canonical child detail
                # proves a terminal state, the parent projection's historical
                # started/interacted rows can never make that child live again.
                if agent.get("state") in ("done", "ended"):
                    continue
                aid = agent.get("agent_id")
                if aid:
                    targets.setdefault(str(aid), []).append(agent)
        details, _errors, _deferred = self._read_threads_bounded(targets, deadline)
        for aid, child in details.items():
            turns = child.get("turns") or []
            last_status = turns[-1].get("status") if turns else None
            child_status = child.get("status") or {}
            child_status = (child_status.get("type") if
                            isinstance(child_status, dict) else child_status)
            for agent in targets.get(aid, []):
                if last_status:
                    agent["state"] = ("done" if last_status == "completed" else
                                      "ended" if last_status in
                                      ("failed", "interrupted") else "running")
                elif child_status == "active":
                    agent["state"] = "running"
                elif child_status == "systemError":
                    agent["state"] = "ended"
                elif turns and child_status in ("idle", "notLoaded"):
                    agent["state"] = "done"
                agent["convo_v"] = sum(len(t.get("items") or []) for t in turns)
                agent["model"] = child.get("model") or agent["model"]
                child_usage = self.client.thread_state.get(aid, {}).get(
                    "token_usage") or {}
                if child_usage:
                    agent["total_tokens"] = _usage_total(child_usage)
                    agent["tokens"] = _token_breakdown(child_usage)
        for session in sessions:
            agents = session.get("agents") or []
            session["agents_running"] = sum(
                agent.get("state") in ("running", "stalled") for agent in agents)

    def _refresh(self):
        try:
            self._refresh_once()
        except Exception as exc:
            self._mark_refresh_failure(exc)
        finally:
            with self._lock:
                self._refreshing = False
                self._last_refresh = self.clock()

    def _refresh_once(self):
        try:
            threads = self.client.list_threads()
        except Exception as exc:
            self._mark_refresh_failure(exc)
            return
        now = self.clock()
        loaded = set()
        if hasattr(self.client, "loaded_thread_ids"):
            try:
                for entry in self.client.loaded_thread_ids():
                    loaded.add(entry.get("id") if isinstance(entry, dict) else entry)
                loaded.discard(None)
                with self._lock:
                    self._loaded_threads = set(loaded)
                    self._loaded_generation = getattr(self.client, "generation", None)
                    self._loaded_error = None
                    self._loaded_error_at = None
            except Exception as exc:
                with self._lock:
                    self._loaded_error = str(exc)
                    self._loaded_error_at = now
                # Loaded-state discovery is advisory. Never detach owned sessions
                # or resume every remembered thread because this one call failed.
                loaded = set()
        self._refresh_local_models(now)
        self._schedule_account_refresh(now)
        persisted = self._state()
        modes = dict(persisted.get("modes") or {})
        thread_meta = dict(persisted.get("thread_meta") or {})
        managed = {tid for tid in (persisted.get("threads") or [])
                   if self._owns_metadata(thread_meta.get(tid) or {})}
        with self._lock:
            tracked_external = set(self._tracked_external)
            previous_by_tid = {
                item.get("native_session_id"): {
                    **item, "capabilities": dict(item.get("capabilities") or {})}
                for item in self._sessions if item.get("native_session_id")}
        out, listed, clean_threads = [], set(), []
        for candidate in threads:
            shape_error = self._thread_shape_error(candidate)
            if shape_error:
                tid = candidate.get("id") if isinstance(candidate, dict) else None
                if isinstance(tid, str) and tid:
                    listed.add(tid)
                    previous = previous_by_tid.get(tid)
                    if not previous and tid in managed:
                        previous = self._stub_session(
                            tid, thread_meta.get(tid) or {}, modes.get(tid) or "default")
                    if previous:
                        out.append(self._stale_session(previous, shape_error))
                self._refresh_diagnostics.append(
                    {"ts": now, "thread_id": tid, "error": shape_error})
                continue
            if candidate.get("parentThreadId"):
                continue
            # External threads remain in the archive listing after their owning
            # Desktop/VS Code client closes them. They are not live Fleet work;
            # never retain them as view-only Now cards.
            if candidate.get("archived") and candidate.get("id") not in managed:
                continue
            clean_threads.append(candidate)
            listed.add(candidate["id"])

        detail_deadline = time.monotonic() + self._refresh_budget_seconds
        detail_candidates = []
        for thread in clean_threads:
            tid = thread["id"]
            adoptable = tid in loaded
            if (tid in managed or adoptable) and now >= self._detail_retry_after.get(tid, 0):
                detail_candidates.append(tid)
        missing_materialized = {
            tid for tid in managed - listed
            if not (thread_meta.get(tid) or {}).get("unmaterialized")}
        detail_candidates.extend(tid for tid in missing_materialized
                                 if now >= self._detail_retry_after.get(tid, 0))
        detail_results, detail_failures, detail_deferred = self._read_threads_bounded(
            detail_candidates, detail_deadline)
        for tid, error in detail_failures.items():
            self._detail_errors[tid] = error
            self._detail_retry_after[tid] = now + 30
        for tid in detail_results:
            self._detail_errors.pop(tid, None)
            self._detail_retry_after.pop(tid, None)
        for tid in missing_materialized:
            detail = detail_results.get(tid)
            if detail:
                clean_threads.append({"id": tid, **detail})

        # App Server exposes external threads as notLoaded and omits their live
        # turns. Observe only the newest bounded slice of their local rollouts so
        # active CLI/Desktop work can remain in Now without scanning the archive.
        recent_external = []
        for thread in clean_threads:
            tid = thread.get("id")
            if not tid or tid in managed:
                continue
            updated = _epoch(thread.get("updatedAt") or thread.get("createdAt"))
            if updated is None or now - updated > EXTERNAL_OBSERVATION_SECONDS:
                continue
            recent_external.append((updated, tid))
        recent_external = {
            tid for _, tid in sorted(recent_external, reverse=True)[
                :MAX_RECENT_EXTERNAL_OBSERVATIONS]
        }

        for thread in clean_threads:
            tid = thread.get("id")
            if tid in listed and any(item.get("native_session_id") == tid for item in out):
                continue
            listed.add(tid)
            source = thread.get("source") or "unknown"
            # App Server also writes `source: vscode` for Fleet's own rich-client
            # threads and remote CLI clients. The exact managed daemon's loaded
            # list is runtime evidence; this presentation label is not.
            is_managed = tid in managed
            # A CLI connected with `codex --remote unix://...` is another client
            # of Fleet's canonical runtime. Adopt it automatically so both
            # surfaces steer the same live turn instead of resuming a copy.
            if tid in loaded and not is_managed:
                self._remember(tid, modes.get(tid) or "default", {
                    "runtime_owner": self.runtime_owner, "origin": source,
                    "cwd": thread.get("cwd") or "", "model": thread.get("model") or "",
                    "effort": thread.get("effort"), "name": thread.get("name"),
                    "created_at": _epoch(thread.get("createdAt")) or now,
                    "unmaterialized": False})
                managed.add(tid)
                is_managed = True
            observation = None
            settings_observation = None
            if (not is_managed and self.external_observer and
                    (tid in tracked_external or tid in recent_external)):
                try:
                    observation = self.external_observer.observe(tid)
                except Exception as exc:
                    observation = {"error": str(exc), "messages": []}
                settings_observation = observation
            detail_error = None
            if is_managed:
                retry_at = self._detail_retry_after.get(tid, 0)
                if now < retry_at:
                    detail_error = self._detail_errors.get(tid)
                elif tid in detail_results:
                    thread = {**thread, **detail_results[tid]}
                elif tid in detail_deferred:
                    detail_error = "detail refresh deferred by the bounded refresh budget"
                else:
                    detail_error = detail_failures.get(tid)
            live = self.client.thread_state.setdefault(tid, {})
            # thread/list and thread/read can omit settings even for a managed,
            # actively connected thread. The exact local rollout still records
            # turn_context model/effort, so use it only as a settings fallback;
            # App Server remains authoritative for lifecycle, messages, and
            # mutation ownership.
            if (is_managed and self.external_observer and
                    (not (live.get("model") or thread.get("model")) or
                     not (live.get("effort") or thread.get("effort")))):
                try:
                    settings_observation = self.external_observer.observe(tid)
                except Exception:
                    settings_observation = None
            pending = self._pending(tid, live.get("pending"))
            updated = thread.get("updatedAt") or thread.get("createdAt")
            turn_lifecycle = _latest_turn_lifecycle(thread)
            live_completed = _epoch(live.get("completed_at"))
            completion_times = [value for value in
                                (turn_lifecycle.get("completed_at"), live_completed,
                                 (observation or {}).get("completed_at"))
                                if value is not None]
            completed_epoch = max(completion_times) if completion_times else None
            activity_times = [value for value in
                              (_epoch(updated), _epoch(live.get("updated_at")),
                               turn_lifecycle.get("started_at"), completed_epoch,
                               (observation or {}).get("last_activity_at"),
                               (observation or {}).get("started_at"))
                              if value is not None]
            # thread.updatedAt can remain stale when ChatGPT desktop owns the turn.
            # Prefer the newest lifecycle evidence instead of the first truthy field.
            updated_epoch = max(activity_times) if activity_times else now
            recorded = thread.get("status") or {}
            recorded_type = recorded.get("type") if isinstance(recorded, dict) else recorded
            flags = set(recorded.get("activeFlags") or []) if isinstance(recorded, dict) else set()
            turn_started = turn_lifecycle.get("started_at")
            no_active_turn_at = _epoch(live.get("no_active_turn_at"))
            provider_proved_idle = bool(no_active_turn_at and
                                        (turn_started is None or
                                         turn_started <= no_active_turn_at))
            native_running = ((live.get("status") == "running" or
                               recorded_type == "active") and
                              not provider_proved_idle)
            observed_running = turn_lifecycle.get("active", False) and (
                completed_epoch is None or turn_started is None or turn_started > completed_epoch)
            running = native_running or observed_running or bool(
                (observation or {}).get("active"))
            quiet = max(0, now - updated_epoch)
            turn_error = _error_text(turn_lifecycle.get("error"))
            provider_error = _error_text(live.get("error") or thread.get("error") or
                                         (recorded.get("error") if
                                          isinstance(recorded, dict) else None) or
                                         turn_error)
            blocked = (live.get("status") == "blocked" or
                       _is_limit_error(provider_error))
            inactive_not_loaded = bool(
                recorded_type == "notLoaded" and quiet > self.dormant_seconds
                and tid not in loaded and not running and live.get("compacting") is None)
            if inactive_not_loaded:
                state = "dormant"
            elif blocked:
                state = "blocked"
            elif provider_error or recorded_type == "systemError":
                state = "error"
            elif pending or flags.intersection({"waitingOnApproval", "waitingOnUserInput"}):
                state = "needs_you"
            elif running and quiet > self.stall_seconds:
                state = "stalled"
            elif running:
                state = "running"
            elif completed_epoch is not None and 0 <= now - completed_epoch < 90:
                state = "turn_done"
            else:
                state = "idle"
            cwd = thread.get("cwd") or ""
            usage = live.get("token_usage") or (observation or {}).get("token_usage") or {}
            mode = live.get("collaboration_mode") or modes.get(tid) or "default"
            persisted_meta = thread_meta.get(tid) or {}
            # thread/start and thread/resume report the selected model/effort,
            # but thread/list and thread/read do not. Keep Fleet's saved values
            # as the durable restart/compaction fallback for owned threads.
            # For owned live threads, App Server's state is newer than a
            # thread/read projection built before a settings update completed.
            model = ((live.get("model") or thread.get("model") or
                      persisted_meta.get("model") or
                      (settings_observation or {}).get("model")) if is_managed else
                     ((observation or {}).get("model") or thread.get("model") or
                      live.get("model") or persisted_meta.get("model"))) or ""
            if is_managed:
                effort = (live.get("effort") if "effort" in live else
                          thread.get("effort") if thread.get("effort") else
                          persisted_meta.get("effort") or
                          (settings_observation or {}).get("effort"))
            else:
                effort = ((observation or {}).get("effort") if observation else None) or \
                         (thread.get("effort") if "effort" in thread else
                          live.get("effort") if "effort" in live else
                          persisted_meta.get("effort"))
            settings_revision = int(persisted_meta.get("settings_revision") or 0)
            if is_managed:
                discovered_meta = {}
                discovered_model = live.get("model") or thread.get("model")
                effort_present = "effort" in live or "effort" in thread
                discovered_effort = (live.get("effort") if "effort" in live else
                                     thread.get("effort") if "effort" in thread else None)
                if discovered_model and discovered_model != persisted_meta.get("model"):
                    discovered_meta["model"] = discovered_model
                if effort_present and discovered_effort != persisted_meta.get("effort"):
                    discovered_meta["effort"] = discovered_effort
                if discovered_meta:
                    discovered_meta["settings_revision"] = settings_revision + 1
                    with self._projection_commit_lock:
                        try:
                            if self._remember(tid, mode, discovered_meta,
                                              expected_settings_revision=settings_revision):
                                settings_revision += 1
                        except Exception as exc:
                            # Runtime state remains canonical even when the local
                            # durability file is temporarily unwritable.
                            self._refresh_diagnostics.append({"ts": now, "thread_id": tid,
                                "error": "settings metadata persistence failed: " + str(exc)[:500]})
            ctx_tokens = _usage_total(usage)
            ctx_window = _usage_window(usage) or self._context_window_for_model(model)
            interrupted = bool(
                completed_epoch is not None and
                str(turn_lifecycle.get("status") or live.get("turn_status") or "").lower()
                == "interrupted")
            files = _files(thread, cwd)
            messages = _conversation(thread)
            if (observation or {}).get("messages"):
                messages = observation["messages"]
            agents = _agents(thread, tid)
            if (observation or {}).get("agents"):
                agents = observation["agents"]
            elif is_managed and (settings_observation or {}).get("agents"):
                observed_agents = {
                    item.get("native_session_id") or
                    str(item.get("agent_id") or "").removeprefix("agent-"): item
                    for item in settings_observation["agents"]
                }
                for agent in agents:
                    observed_agent = observed_agents.get(agent.get("agent_id")) or {}
                    if not agent.get("model") and observed_agent.get("model"):
                        agent["model"] = observed_agent["model"]
                    if not agent.get("effort") and observed_agent.get("effort"):
                        agent["effort"] = observed_agent["effort"]
            previous_agents = {
                item.get("agent_id"): item
                for item in ((previous_by_tid.get(tid) or {}).get("agents") or [])
                if item.get("agent_id")}
            for agent in agents:
                previous_agent = previous_agents.get(agent.get("agent_id"))
                if (previous_agent or {}).get("state") not in ("done", "ended"):
                    continue
                # Parent thread/read does not emit a completion activity for
                # every Codex child. Preserve terminal child detail across a
                # later partial refresh instead of resurrecting it from the
                # parent's old started/interacted event.
                agent["state"] = previous_agent["state"]
                for key in ("convo_v", "model", "total_tokens", "tokens",
                            "started", "last", "last_msg"):
                    if previous_agent.get(key) is not None:
                        agent[key] = previous_agent[key]
            agents_running = sum(a["state"] in ("running", "stalled") for a in agents)
            revision = _revision(thread, live)
            if observation and observation.get("revision"):
                revision = observation["revision"]
            if is_managed and not detail_error:
                self._cache_snapshot(tid, messages, files, revision, thread)
            owned_turn = is_managed and self._owns_active_turn(tid)
            can_interrupt = (is_managed and owned_turn and
                             state in ("running", "stalled", "needs_you"))
            uncontrolled_active = (state in ("running", "stalled", "needs_you") and
                                   not can_interrupt)
            control_state = ("connected_active" if owned_turn else
                             "reconnecting" if is_managed and uncontrolled_active else
                             "connected_idle" if is_managed else "view_only")
            materialized = not bool((thread_meta.get(tid) or {}).get("unmaterialized"))
            settings_supported = bool(is_managed and materialized and self.models)
            compacting = live.get("compacting") is not None
            can_change_settings = bool(settings_supported and state not in
                ("running", "stalled", "needs_you", "blocked", "error", "stale") and
                not compacting)
            reg_status = ("running" if state in ("running", "stalled") else
                          "turn_done" if state == "turn_done" else
                          live.get("status") or recorded_type)
            out.append({
                "session_id": self.key(tid), "native_session_id": tid,
                "provider": "codex", "name": thread.get("name") or thread.get("title"),
                "title": thread.get("name") or thread.get("title"),
                "project": os.path.basename(cwd) or cwd or "Codex", "cwd": cwd,
                "branch": (thread.get("gitInfo") or {}).get("branch"),
                "model": model, "family": "codex", "effort": effort,
                "settings_revision": settings_revision,
                "collaboration_mode": mode, "running": None,
                "last_msg": (_last_message(messages, thread.get("preview")) or
                             (previous_by_tid.get(tid) or {}).get("last_msg")),
                "card_peek": (card_peek_rows(messages) or
                              (previous_by_tid.get(tid) or {}).get("card_peek")),
                "_latest_prose": _latest_prose(messages),
                "repo_outcome": observed_test_outcome(
                    messages, session_id=self.key(tid), provider="codex"),
                "state": state, "reg_status": reg_status, "interrupted": interrupted,
                "headless": not is_managed, "read_only": not is_managed,
                "read_only_reason": (
                    None if is_managed else
                    "This thread is not loaded in Fleet's managed App Server; "
                    "its transcript is view only"),
                "codex_source": self.runtime_owner if is_managed else "external",
                "codex_provider_source": source,
                "observed_external": bool(observation),
                "observation_confidence": (observation or {}).get("confidence"),
                "observation_warning": ((observation or {}).get("warning") or
                                        (observation or {}).get("error")),
                "control_state": control_state,
                "queue_accepting": bool(is_managed and uncontrolled_active and
                                        state not in ("blocked", "error", "stale")),
                "quiet_s": round(quiet),
                "provider_activity_at": updated_epoch,
                "ctx_tokens": ctx_tokens,
                "ctx_window": ctx_window,
                "ctx_pct": round(100 * ctx_tokens / ctx_window, 1) if ctx_window else None,
                "total_tokens": _usage_cumulative(usage),
                "cost": None, "cost_source": "unavailable", "bridge_url": None,
                "started_ms": _millis(thread.get("createdAt")), "pending": pending,
                "compacting": live.get("compacting"), "muted": False,
                "convo_v": revision, "files_n": len(files), "agents": agents,
                "agents_running": agents_running, "agents_total": len(agents),
                "agent_cost": None, "stale": False,
                "error": provider_error or None, "refresh_warning": detail_error,
                "capabilities": {"submit": is_managed and
                    state not in ("blocked", "error", "stale") and
                    not uncontrolled_active and not compacting,
                    "queue_submit": bool(is_managed and (uncontrolled_active or compacting) and
                                         state not in ("blocked", "error", "stale")),
                    "interrupt": can_interrupt,
                    "takeover": False, "archive": is_managed,
                    "close": is_managed and not uncontrolled_active,
                    "compact": is_managed and state not in
                        ("running", "stalled", "needs_you") and not compacting,
                    "review": is_managed and state not in
                        ("running", "stalled", "needs_you"),
                    "files": bool(files), "focus_terminal": False,
                    "focus_terminal_mode": None,
                    "answer_structured": bool(pending and pending.get("kind") in
                                              ("question", "elicitation")),
                    "decide_approval": bool(pending), "spawn_agent": True,
                    "relay_agent": is_managed and not uncontrolled_active and not compacting,
                    "relay_agent_direct": False,
                    "model_effort_settings": settings_supported,
                    "change_model_effort": can_change_settings,
                    "change_model_effort_reason": (
                        "Available after compaction finishes" if settings_supported and compacting else
                        "Available when the Codex turn is idle" if settings_supported and
                        not can_change_settings else
                        "Codex's local model catalog is unavailable" if is_managed and
                        materialized and not self.models else
                        "External Codex runtime is view only" if not is_managed else
                        "Fleet is creating this session" if not materialized else ""),
                    "account_usage": bool(self._account), "exact_cost": False,
                    "measured_throughput": False},
            })
        # App Server assigns a thread ID before the first turn materializes a
        # rollout. Keep that short-lived shell only while the canonical runtime
        # still reports it loaded. Without either runtime state or a rollout, the
        # ID can never be resumed or used and must not become an Available ghost.
        for tid in managed - listed:
            meta = thread_meta.get(tid) or {}
            live = self.client.thread_state.get(tid, {})
            previous = previous_by_tid.get(tid)
            runtime_present = (tid in loaded or live.get("status") == "running" or
                               bool(live.get("turn_id")))
            missing_error = (self._detail_errors.get(tid) or
                             ("detail refresh deferred by the bounded refresh budget"
                              if tid in detail_deferred else None))
            if not meta.get("unmaterialized") and missing_error and not runtime_present:
                fallback = previous or self._stub_session(
                    tid, meta, modes.get(tid) or "default")
                out.append(self._stale_session(fallback, missing_error))
                continue
            created_at = float(meta.get("created_at") or 0)
            recently_created = bool(created_at and 0 <= now - created_at < 30)
            # thread/start and turn/start can become usable before thread/list
            # exposes the new row. Preserve the locally owned card through that
            # propagation window instead of making it disappear from Fleet.
            if runtime_present or (previous and recently_created):
                fallback = previous or self._stub_session(
                    tid, meta, modes.get(tid) or "default")
                pending = self._pending(tid, live.get("pending"))
                completed_at = _epoch(live.get("completed_at"))
                owned_turn = self._owns_active_turn(tid)
                active = live.get("status") == "running" or owned_turn
                if pending:
                    state = "needs_you"
                elif active:
                    state = "running"
                elif completed_at is not None and 0 <= now - completed_at < 90:
                    state = "turn_done"
                else:
                    state = fallback.get("state") or "idle"
                fallback.update(state=state, pending=pending, stale=False,
                                error=live.get("error"),
                                reg_status=("running" if state == "running" else
                                            "turn_done" if state == "turn_done" else
                                            live.get("status") or
                                            fallback.get("reg_status")))
                capabilities = fallback["capabilities"]
                uncontrolled_active = state in ("running", "needs_you") and not owned_turn
                compacting = live.get("compacting") is not None
                settings_supported = bool(capabilities.get("model_effort_settings"))
                capabilities.update(
                    submit=not uncontrolled_active and not compacting,
                    queue_submit=uncontrolled_active or compacting,
                    interrupt=owned_turn and state in ("running", "needs_you"),
                    close=not uncontrolled_active,
                    compact=not uncontrolled_active and not compacting,
                    change_model_effort=(settings_supported and not uncontrolled_active and
                                         state not in ("running", "needs_you") and not compacting),
                    change_model_effort_reason=(
                        "Available after compaction finishes" if compacting else
                        "Available when the Codex turn is idle" if settings_supported and
                        state in ("running", "needs_you") else ""),
                    focus_terminal=False,
                    focus_terminal_mode=None)
                fallback["control_state"] = ("connected_active" if owned_turn else
                                             "reconnecting" if uncontrolled_active else
                                             "connected_idle")
                fallback["queue_accepting"] = uncontrolled_active
                out.append(fallback)
                continue
            if meta.get("unmaterialized"):
                if tid in loaded:
                    out.append(self._stub_session(tid, meta,
                                                  modes.get(tid) or "default"))
                else:
                    self._forget(tid)
        # Parent detail and child lifecycle reads are separate bounded phases.
        # Reusing the parent deadline meant it was normally expired before a
        # single child could be checked, leaving completed agents active forever.
        agent_deadline = time.monotonic() + self._refresh_budget_seconds
        self._enrich_agents_bounded(out, agent_deadline)
        with self._projection_commit_lock:
            latest = self._state()
            latest_meta = latest.get("thread_meta") or {}
            latest_modes = latest.get("modes") or {}
            for session in out:
                tid = session.get("native_session_id")
                meta = latest_meta.get(tid) or {}
                if not self._owns_metadata(meta):
                    continue
                latest_revision = int(meta.get("settings_revision") or 0)
                projected_revision = int(session.get("settings_revision") or 0)
                if latest_revision > projected_revision:
                    if meta.get("model"):
                        session["model"] = meta["model"]
                    if "effort" in meta:
                        session["effort"] = meta.get("effort")
                    session["settings_revision"] = latest_revision
                if latest_modes.get(tid) in ("plan", "default"):
                    session["collaboration_mode"] = latest_modes[tid]
            with self._lock:
                self._sessions = out
                self.error = None
                self.error_at = None
                self._refreshing = False
                self._last_refresh = self.clock()

    def _refresh_local_models(self, now):
        """Refresh model options from Codex's own cache, never its control socket."""
        signature = None
        try:
            stat = os.stat(self._models_cache_path)
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._models_cache_signature:
                return
            models = _local_model_catalog(self._models_cache_path)
        except Exception as exc:
            with self._lock:
                # A changed cache that no longer parses must fail closed. Keeping
                # the prior catalog would advertise settings the provider no
                # longer says are valid.
                if signature != self._models_cache_signature:
                    self.models = []
                    self._models_cache_signature = signature
                self._model_error = str(exc)
                self._model_error_at = now
            return
        with self._lock:
            self.models = models
            self._models_cache_signature = signature
            self._model_error = None
            self._model_error_at = None

    def _schedule_account_refresh(self, now):
        with self._lock:
            if self._account_refreshing or now - self._last_account_refresh < 30:
                return
            self._account_refreshing = True

        def refresh():
            try:
                self._refresh_account(now)
            finally:
                with self._lock:
                    self._account_refreshing = False
        threading.Thread(target=refresh, daemon=True,
                         name="fleet-codex-usage").start()

    def _refresh_account(self, now):
        if now - self._last_account_refresh < 30:
            return
        self._last_account_refresh = now
        try:
            limits = self.client.account_limits()
            tokens = self.client.account_usage()
            info = self.client.account_info()
            account = _account_usage(limits, tokens, info)
        except Exception as exc:
            with self._lock:
                self._account_error = str(exc)
                self._account_error_at = now
            return
        with self._lock:
            self._account = {**account, "stale": False, "error": None}
            self._account_error = None
            self._account_error_at = None

    def account_usage(self):
        with self._lock:
            if not self._account:
                return ({"provider": "codex", "stale": True,
                         "error": self._account_error}
                        if self._account_error else None)
            out = dict(self._account)
            if self._account_error:
                out.update(stale=True, error=self._account_error,
                           error_at=self._account_error_at)
            return out

    def diagnostics(self):
        with self._lock:
            protocol = list(getattr(self.client, "diagnostics", []))
            counts = {}
            for item in protocol:
                kind = str(item.get("kind") or "unknown")
                counts[kind] = counts.get(kind, 0) + 1
            out = {
                "provider_error": self.error,
                "provider_error_at": self.error_at,
                "model_error": self._model_error,
                "model_error_at": self._model_error_at,
                "model_catalog_size": len(self.models),
                "loaded_error": self._loaded_error,
                "loaded_error_at": self._loaded_error_at,
                "account_error": self._account_error,
                "account_error_at": self._account_error_at,
                "transport_error": getattr(self.client, "last_error", None),
                "transport_generation": getattr(self.client, "generation", None),
                "loaded_threads": len(self._loaded_threads),
                "visible_sessions": len(self._sessions),
                "refresh_errors": list(self._refresh_diagnostics),
                "protocol_events": counts,
            }
            if self.runtime_migration:
                out["runtime"] = self.runtime_migration.diagnostics()
            else:
                out["runtime"] = {"mode": ("managed" if self.runtime_owner ==
                                             MANAGED_RUNTIME_OWNER else "private"),
                                  "phase": "committed" if self.runtime_owner ==
                                           MANAGED_RUNTIME_OWNER else "isolated",
                                  "blockers": [], "error": None}
            return out

    def runtime_status(self):
        if self.runtime_migration:
            return self.runtime_migration.diagnostics()
        return {"mode": ("managed" if self.runtime_owner == MANAGED_RUNTIME_OWNER
                          else "private"),
                "phase": ("committed" if self.runtime_owner == MANAGED_RUNTIME_OWNER
                           else "isolated"),
                "blockers": [], "error": None}

    def _managed(self):
        state = self._state()
        meta = state.get("thread_meta") or {}
        return [tid for tid in (state.get("threads") or [])
                if self._owns_metadata(meta.get(tid) or {})]

    def _modes(self):
        return dict(self._state().get("modes") or {})

    def _state(self):
        if not self.state_path:
            return {}
        with self._state_lock:
            try:
                with open(self.state_path) as handle:
                    return json.load(handle) or {}
            except (OSError, ValueError):
                return {}

    def _save_state(self, state):
        if not self.state_path:
            return
        with self._state_lock:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(state, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)

    def _remember(self, tid, mode=None, meta=None, expected_settings_revision=None):
        if not self.state_path or not tid:
            return False
        with self._state_lock:
            state = self._state()
            ids = list(state.get("threads") or [])
            if tid not in ids:
                ids.append(tid)
            modes = dict(state.get("modes") or {})
            if mode in ("plan", "default"):
                modes[tid] = mode
            kept = ids[-200:]
            state.update(threads=kept,
                         modes={key: value for key, value in modes.items() if key in kept})
            snapshots = dict(state.get("snapshots") or {})
            state["snapshots"] = {key: value for key, value in snapshots.items()
                                  if key in kept}
            thread_meta = dict(state.get("thread_meta") or {})
            current_meta = dict(thread_meta.get(tid) or {})
            if expected_settings_revision is not None and int(
                    current_meta.get("settings_revision") or 0) != int(
                        expected_settings_revision):
                return False
            thread_meta[tid] = {**current_meta, **(meta or {}),
                                "runtime_owner": self.runtime_owner,
                                "control_runtime": self.runtime_owner}
            state["thread_meta"] = {key: value for key, value in thread_meta.items()
                                    if key in kept}
            self._save_state(state)
            return True

    def _forget(self, tid):
        with self._state_lock:
            state = self._state()
            state["threads"] = [item for item in state.get("threads") or [] if item != tid]
            for key in ("modes", "snapshots", "thread_meta"):
                values = dict(state.get(key) or {})
                values.pop(tid, None)
                state[key] = values
            self._save_state(state)

    def _cache_snapshot(self, tid, messages, files, revision, thread):
        if not self.state_path:
            return
        with self._state_lock:
            state = self._state()
            snapshots = dict(state.get("snapshots") or {})
            old = snapshots.get(tid) or {}
            if old.get("revision") == revision:
                return
            snapshots[tid] = {"revision": revision, "messages": messages[-300:],
                              "files": files[-100:], "agents": _agents(thread, tid)[-100:],
                              "updated_at": self.clock(),
                              "info": {"session_id": self.key(tid),
                                       "cwd": thread.get("cwd") or "",
                                       "model": thread.get("model") or "",
                                       "title": thread.get("name") or thread.get("title")}}
            state["snapshots"] = snapshots
            thread_meta = dict(state.get("thread_meta") or {})
            if tid in thread_meta:
                thread_meta[tid] = {**thread_meta[tid], "unmaterialized": False}
                state["thread_meta"] = thread_meta
            self._save_state(state)

    def _ensure_loaded(self, tid):
        """Load one owned thread on demand without disturbing active runtimes."""
        if not hasattr(self.client, "resume_thread"):
            return {}
        generation = getattr(self.client, "generation", None)
        with self._lock:
            known_loaded = (self._loaded_generation == generation and
                            tid in self._loaded_threads)
        if known_loaded:
            return {}
        # A reconnected Fleet WebSocket does not mean the shared runtime died.
        # Ask the runtime before resuming: thread/resume can abort an active turn.
        if hasattr(self.client, "loaded_thread_ids"):
            try:
                loaded = {entry.get("id") if isinstance(entry, dict) else entry
                          for entry in self.client.loaded_thread_ids()}
                loaded.discard(None)
                with self._lock:
                    self._loaded_threads = loaded
                    self._loaded_generation = getattr(self.client, "generation", None)
                    self._loaded_error = None
                    self._loaded_error_at = None
                if tid in loaded:
                    return {}
            except Exception as exc:
                with self._lock:
                    self._loaded_error = str(exc)
                    self._loaded_error_at = self.clock()
                # Do not guess that a thread is unloaded. Let the exact action
                # return a useful error instead of risking an active-turn abort.
                return {}
        thread = self.client.resume_thread(tid)
        with self._lock:
            self._loaded_threads.add(tid)
            self._loaded_generation = getattr(self.client, "generation", None)
        return thread or {}

    def resume_capability(self, key):
        """Return whether a saved thread is explicitly owned by Fleet's runtime."""
        tid = self.native(key)
        if not self.enabled:
            return False, "Codex App Server is unavailable"
        state = self._state()
        meta = (state.get("thread_meta") or {}).get(tid) or {}
        if tid not in (state.get("threads") or []) or \
                not self._owns_metadata(meta):
            return False, "external Codex thread is view only"
        if meta.get("unmaterialized"):
            return False, "the Codex thread never created a saved conversation"
        return True, None

    def resume_owned_thread(self, key):
        """Load one exact Fleet-owned thread without adopting an external thread."""
        allowed, reason = self.resume_capability(key)
        if not allowed:
            return {"ok": False, "error": reason}
        tid = self.native(key)
        try:
            self._ensure_loaded(tid)
            with self._lock:
                self._last_refresh = 0
            return {"ok": True, "session_id": self.key(tid), "resumed": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_thread(self, cwd, model=None, effort=None, mode="plan",
                     initial_text=None, _runtime_locked=False):
        if not _runtime_locked:
            with self._runtime_action_lock:
                if self.runtime_migration and self.runtime_migration.mutation_blocked():
                    raise CodexError("Codex runtime migration is switching control; retry shortly",
                                     code="provider_control_unavailable", queueable=True)
                return self.start_thread(cwd, model, effort, mode, initial_text,
                                         _runtime_locked=True)
        if mode not in ("plan", "default"):
            raise CodexError("unknown Codex collaboration mode")
        thread = self.client.start_thread(cwd, model, effort)
        tid = thread.get("id")
        resolved_model = thread.get("model") or model or ""
        resolved_effort = thread.get("effort") or effort or ("medium" if mode == "plan" else None)
        if tid and hasattr(self.client, "set_mode"):
            self.client.set_mode(tid, mode, resolved_model, resolved_effort)
        if tid:
            with self._lock:
                self._loaded_threads.add(tid)
                self._loaded_generation = getattr(self.client, "generation", None)
            now = self.clock()
            meta = {"cwd": cwd, "model": resolved_model, "effort": resolved_effort,
                    "name": thread.get("name"), "created_at": now,
                    "unmaterialized": True}
            self._remember(tid, mode, meta)
            stub = self._stub_session(tid, meta, mode)
            with self._lock:
                self._sessions = [s for s in self._sessions
                                  if s.get("session_id") != stub["session_id"]] + [stub]
                self._last_refresh = 0
            if initial_text:
                try:
                    self.client.start_turn(tid, initial_text, mode=mode,
                                           model=resolved_model,
                                           effort=resolved_effort)
                except Exception as exc:
                    # Do not leave a broken empty card behind when the bootstrap
                    # turn fails. An empty App Server thread may not have a rollout
                    # yet, so archive is best-effort and local ownership is always
                    # removed.
                    try:
                        self.client.archive(tid)
                    except Exception:
                        pass
                    self._forget(tid)
                    with self._lock:
                        self._sessions = [s for s in self._sessions
                                          if s.get("session_id") != self.key(tid)]
                    raise CodexError(
                        f"failed to start initial Codex turn: {exc}") from exc
                self._remember(tid, mode, {"unmaterialized": False})
                with self._lock:
                    for current in self._sessions:
                        if current.get("native_session_id") == tid:
                            current.update(state="running", reg_status="running",
                                           last_msg={"role": "user",
                                                     "text": initial_text})
                            current["capabilities"].update(
                                focus_terminal=False,
                                focus_terminal_mode=None)
        return thread

    def _stub_session(self, tid, meta, mode):
        cwd = meta.get("cwd") or ""
        now = meta.get("created_at") or self.clock()
        return {"session_id": self.key(tid), "native_session_id": tid,
                    "provider": "codex", "name": meta.get("name"),
                    "title": meta.get("name"), "project": os.path.basename(cwd) or cwd,
                    "cwd": cwd, "branch": None, "model": meta.get("model") or "",
                    "family": "codex", "effort": meta.get("effort"),
                    "settings_revision": int(meta.get("settings_revision") or 0),
                    "collaboration_mode": mode, "running": None,
                    "last_msg": None, "state": "idle",
                    "reg_status": "loaded", "quiet_s": 0, "ctx_tokens": 0,
                    "ctx_window": None,
                    "ctx_pct": None, "cost": None, "cost_source": "unavailable",
                    "bridge_url": None, "started_ms": round(now * 1000), "pending": None,
                    "compacting": None, "muted": False, "convo_v": f"{int(now * 1000)}:0",
                    "files_n": 0, "headless": False, "read_only": False,
                    "read_only_reason": None, "codex_source": "appServer",
                    "agents": [], "agents_running": 0, "agents_total": 0,
                    "agent_cost": None, "capabilities": {"submit": True,
                    "interrupt": False, "close": True, "focus_terminal": False,
                    "focus_terminal_mode": None,
                    "answer_structured": False, "takeover": False,
                    "archive": True, "compact": True, "review": True, "files": False,
                    "decide_approval": False, "spawn_agent": True, "relay_agent": False,
                    "relay_agent_direct": False, "model_effort_settings": False,
                    "change_model_effort": False,
                    "change_model_effort_reason": "Fleet is creating this session",
                    "account_usage": False,
                    "exact_cost": False, "measured_throughput": False}}

    def _pending(self, tid, nonce):
        if isinstance(nonce, list):
            nonce = nonce[0] if nonce else None
        approval = self.client.approvals.get(str(nonce)) if nonce else None
        if not approval or approval.get("thread_id") != tid:
            return None
        p = approval["params"]
        if approval["method"] == "item/tool/requestUserInput":
            questions = []
            for q in p.get("questions") or []:
                questions.append({"header": q.get("header", "Question"),
                                  "question": q.get("question", ""),
                                  "multiSelect": False,
                                  "allowOther": bool(q.get("isOther", True)),
                                  "secret": bool(q.get("isSecret", False)),
                                  "options": [{"label": o.get("label", ""),
                                               "description": o.get("description", "")}
                                              for o in (q.get("options") or [])]})
            return {"kind": "question", "nonce": str(nonce), "questions": questions,
                    "dismiss_action": "cancel_turn"}
        if approval["method"] == "mcpServer/elicitation/request":
            return _elicitation_pending(str(nonce), p)
        summary = p.get("reason") or p.get("command") or p.get("grantRoot") or "Codex request"
        method = approval["method"]
        kind = {"item/commandExecution/requestApproval": "command",
                "item/fileChange/requestApproval": "file_change",
                "item/permissions/requestApproval": "permissions",
                "applyPatchApproval": "file_change",
                "execCommandApproval": "command"}.get(method, "unknown")
        if p.get("cwd"):
            summary = f"{summary}\nworking directory: {p['cwd']}"
        return {"kind": "permission", "nonce": str(nonce), "tool": kind,
                "approval_kind": kind, "input_summary": str(summary)[:1500],
                "decisions": ["allow", "always", "deny", "cancel"]}

    def context(self, key):
        tid = self.native(key)
        with self._lock:
            known = next((item for item in self._sessions
                          if item.get("native_session_id") == tid), None)
        if known and known.get("read_only") and self.external_observer:
            observed = self.external_observer.observe(tid)
            if observed and observed.get("messages"):
                return {"ok": True, "messages": observed["messages"], "files": [],
                        "revision": observed.get("revision"), "read_only": True,
                        "observation_confidence": observed.get("confidence"),
                        "warning": observed.get("warning") or observed.get("error")}
        try:
            thread = self.client.read_thread(tid)
            messages = _conversation(thread)
            files = _files(thread, thread.get("cwd") or "")
            revision = _revision(thread, self.client.thread_state.get(tid, {}))
            self._cache_snapshot(tid, messages, files, revision, thread)
            return {"ok": True, "messages": messages, "files": files,
                    "agents": _agents(thread, tid),
                    "revision": revision}
        except Exception as exc:
            if "not materialized yet" in str(exc):
                return {"ok": True, "messages": [], "files": [], "agents": []}
            snapshot = (self._state().get("snapshots") or {}).get(tid)
            if snapshot:
                return {"ok": True, "messages": snapshot.get("messages") or [],
                        "files": snapshot.get("files") or [],
                        "agents": snapshot.get("agents") or [], "closed": True,
                        "stale": True, "error": str(exc), "info": snapshot.get("info") or {},
                        "revision": snapshot.get("revision")}
            return {"ok": False, "error": str(exc)}

    def agent_context(self, key, agent_id):
        parent_id = self.native(key)
        agent_id = str(agent_id or "")
        with self._lock:
            parent = next((item for item in self._sessions
                           if item.get("native_session_id") == parent_id), None)
            member = next((item for item in (parent or {}).get("agents") or []
                           if str(item.get("agent_id") or "") == agent_id), None)
        if not parent:
            historical = self.context(key)
            member = next((item for item in (historical.get("agents") or [])
                           if str(item.get("agent_id") or "") == agent_id), None)
        if not member:
            return {"ok": False, "error": "no such subagent"}
        native_agent_id = member.get("native_session_id") or agent_id
        if parent and parent.get("read_only") and self.external_observer:
            observed = self.external_observer.observe(native_agent_id)
            if observed:
                out = {"ok": True, "messages": observed.get("messages") or [], "files": [],
                       "agents": observed.get("agents") or [], "revision": observed.get("revision"),
                       "read_only": True, "observation_confidence": observed.get("confidence"),
                       "warning": observed.get("warning") or observed.get("error")}
            else:
                out = {"ok": False, "error": "subagent rollout is unavailable"}
        else:
            out = self.context(self.key(native_agent_id))
        if out.get("ok"):
            usage = self.client.thread_state.get(native_agent_id, {}).get("token_usage") or {}
            ctx_tokens = _usage_total(usage) if usage else None
            ctx_window = _usage_window(usage) if usage else None
            out["info"] = {"agent_id": agent_id, "agent_type": member.get("agent_type") or "codex",
                           "description": member.get("description") or "Codex subagent",
                           "model": member.get("model") or "",
                           "family": "codex", "tokens": _token_breakdown(usage),
                           "total_tokens": _usage_cumulative(usage) if usage else None,
                           "ctx_tokens": ctx_tokens, "ctx_window": ctx_window,
                           "ctx_pct": (round(100 * ctx_tokens / ctx_window, 1)
                                       if ctx_window else None),
                           "cost": None, "cost_source": "unavailable"}
        return out

    def commands(self, key, cwd):
        tid = self.native(key)
        out = [
            {"name": "/compact", "desc": "Compact the current conversation",
             "scope": "app-server", "danger": True, "execution": "action",
             "action": "compact"},
            {"name": "/review", "desc": "Review uncommitted changes",
             "scope": "app-server", "danger": False, "execution": "action",
             "action": "review"},
        ]
        errors = []
        try:
            entries = self.client.list_skills(cwd)
        except Exception as exc:
            return {"ok": True, "commands": out, "warning": str(exc)}
        skills = {}
        for entry in entries:
            errors.extend(entry.get("errors") or [])
            for skill in entry.get("skills") or []:
                if not skill.get("enabled", True) or not skill.get("name") or not skill.get("path"):
                    continue
                name = "$" + skill["name"]
                skills[name] = {"name": skill["name"], "path": skill["path"]}
                out.append({"name": name,
                            "desc": (skill.get("description") or
                                     skill.get("shortDescription") or "Codex skill")[:120],
                            "scope": str(skill.get("scope") or "skill").lower(),
                            "danger": False, "execution": "skill"})
        self._skills[tid] = skills
        return {"ok": True, "commands": out,
                "warnings": [_safe_json(error)[:500] for error in errors]}

    def file_content(self, key, fpath):
        context = self.context(key)
        if not context.get("ok"):
            return None, None, context.get("error") or "session unavailable"
        allowed = {item.get("path") for item in context.get("files") or []}
        if fpath not in allowed:
            return None, None, "not a file this Codex thread changed or generated"
        try:
            if os.path.getsize(fpath) > 8_000_000:
                return None, None, "file too large to preview (>8MB)"
            with open(fpath, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            return None, None, f"unreadable: {exc}"
        ext = os.path.splitext(fpath)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".pdf": "application/pdf",
                 ".json": "application/json; charset=utf-8"}.get(
                     ext, "text/plain; charset=utf-8")
        # Keep HTML inert at the authenticated endpoint. The browser client
        # renders it only inside its sandboxed static-preview iframe.
        return ctype, data, None

    def act(self, action, _mutation_locked=False, _runtime_locked=False):
        if not _runtime_locked:
            with self._runtime_action_lock:
                if self.runtime_migration and self.runtime_migration.mutation_blocked():
                    return {"ok": False,
                            "error": "Codex runtime migration is switching control; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                return self.act(action, _mutation_locked=_mutation_locked,
                                _runtime_locked=True)
        typ = action.get("type")
        tid = self.native(action.get("session_id"))
        try:
            with self._lock:
                known = next((s for s in self._sessions
                              if s.get("native_session_id") == tid), None)
            meta = (self._state().get("thread_meta") or {}).get(tid) or {}
            if not known:
                return {"ok": False, "error": "unknown Codex session"}
            if known.get("read_only") or not self._owns_metadata(meta):
                return {"ok": False,
                        "error": known.get("read_only_reason") or
                                 "external Codex thread is view only"}
            if known.get("stale") or known.get("state") == "stale":
                if typ in ("text", "image_text") and known.get(
                        "capabilities", {}).get("queue_submit"):
                    return {"ok": False,
                            "error": "Codex control is reconnecting; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                return {"ok": False, "error": "Codex provider state is stale; refresh and retry",
                        "code": "provider_control_unavailable"}
            if (not _mutation_locked and typ in
                    ("text", "image_text", "mode", "session_settings", "compact",
                     "skill", "relay", "review")):
                with self._mutation_lock(tid):
                    # Re-enter so ownership, staleness, capabilities, live turn
                    # state, and settings are all re-read inside the lock.
                    return self.act(action, _mutation_locked=True)
            required_capability = {"text": "submit", "image_text": "submit", "mode": "submit",
                                   "session_settings": "change_model_effort",
                                   "interrupt": "interrupt", "close": "close",
                                   "archive": "archive", "skill": "submit",
                                   "compact": "compact", "review": "review",
                                   "permission": "decide_approval",
                                   "multiq": "answer_structured",
                                   "option": "answer_structured",
                                   "elicitation": "answer_structured",
                                   "dismiss": "decide_approval",
                                   "relay": "relay_agent"}.get(typ)
            if required_capability and not known.get("capabilities", {}).get(
                    required_capability):
                if (required_capability == "submit" and typ in ("text", "image_text") and
                        known.get("capabilities", {}).get("queue_submit")):
                    return {"ok": False,
                            "error": "Codex control is reconnecting; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                if known.get("state") in ("running", "stalled", "needs_you"):
                    return {"ok": False, "error":
                            "Codex is active in another client; control it there until the turn ends"}
                return {"ok": False, "error": f"session does not support {typ}"}
            if typ in ("permission", "multiq", "option", "elicitation", "dismiss"):
                request = self.client.approvals.get(str(action.get("nonce") or ""))
                if not request or request.get("thread_id") != tid:
                    return {"ok": False, "error": "stale Codex request"}
            if typ == "session_settings":
                # The catalog is a server-owned projection of Codex's local
                # credential-free model cache. Client text never reaches the
                # settings protocol unless both values are in that catalog.
                if "expected_model" not in action or "expected_effort" not in action:
                    return {"ok": False,
                            "error": "expected model and effort are required",
                            "code": "stale_settings"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), None)
                    catalog = {str(item.get("id") or ""): dict(item)
                               for item in self.models if item.get("id")}
                if not session or not session.get("capabilities", {}).get(
                        "change_model_effort"):
                    return {"ok": False,
                            "error": "Codex model and effort can change only while the turn is idle"}
                model = str(action.get("model") or "").strip()
                if model not in catalog:
                    return {"ok": False, "error": "unknown Codex model"}
                efforts = [str(value) for value in
                           (catalog[model].get("efforts") or []) if value]
                effort = str(action.get("effort") or "").strip()
                if efforts and effort not in efforts:
                    return {"ok": False,
                            "error": "unsupported effort level for this Codex model"}
                if not efforts and effort:
                    return {"ok": False,
                            "error": "this Codex model does not advertise effort controls"}
                self._ensure_loaded(tid)
                blocker, live = self._settings_runtime_blocker(tid)
                if blocker:
                    return {"ok": False, "error": blocker}
                # Live App Server state is canonical when it reports settings;
                # the fleet projection is only a fallback for omitted fields.
                current_model = str(live.get("model") or session.get("model") or "")
                current_effort = str((live.get("effort") if "effort" in live else
                                      session.get("effort")) or "")
                if (str(action.get("expected_model") or "") != current_model or
                        str(action.get("expected_effort") or "") != current_effort):
                    return {"ok": False,
                            "error": "settings changed in another view — refresh and try again",
                            "code": "stale_settings"}
                # An attached Codex TUI can change collaboration mode without a
                # Fleet refresh. Preserve that live mode while updating model /
                # effort so a settings save cannot silently revert the TUI.
                mode = (live.get("collaboration_mode") or
                        session.get("collaboration_mode") or
                        self._modes().get(tid) or "default")
                self.client.set_mode(tid, mode, model, effort or None)
                # Provider acceptance precedes metadata. A persistence failure
                # must not lie that the provider rejected or roll the UI back.
                warning = None
                durable = False
                with self._projection_commit_lock:
                    latest_meta = ((self._state().get("thread_meta") or {}).get(tid) or {})
                    next_revision = int(latest_meta.get("settings_revision") or 0) + 1
                    try:
                        durable = self._remember(tid, mode, {
                            "model": model, "effort": effort or None,
                            "settings_revision": next_revision},
                            expected_settings_revision=next_revision - 1)
                    except Exception as exc:
                        warning = ("Applied in Codex, but Fleet could not persist the setting: " +
                                   str(exc)[:300])
                    if not durable and warning is None:
                        warning = ("Applied in Codex, but Fleet could not durably save the setting; "
                                   "the live runtime remains updated")
                    with self._lock:
                        for current in self._sessions:
                            if current.get("native_session_id") == tid:
                                current["model"] = model
                                current["effort"] = effort or None
                                current["settings_revision"] = next_revision
                result = {"ok": True, "model": model, "effort": effort or None,
                          "durable": durable}
                if warning:
                    result["warning"] = warning
                return result
            elif typ in ("text", "image_text"):
                text = str(action.get("text") or "").strip()
                image_paths = list(action.get("image_paths") or []) if typ == "image_text" else []
                if not text and not image_paths:
                    return {"ok": False, "error": "empty text"}
                if image_paths and (len(image_paths) > 4 or any(
                        not isinstance(path, str) or not os.path.isabs(path) for path in image_paths)):
                    return {"ok": False, "error": "invalid image inputs"}
                text = text or ("Please inspect the attached image." if len(image_paths) == 1 else
                                "Please inspect the attached images.")
                inputs = (([{"type": "text", "text": text}] +
                           [{"type": "localImage", "path": path} for path in image_paths])
                          if image_paths else None)
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                blocker, live = self._settings_runtime_blocker(tid)
                owned_turn = self._owns_active_turn(tid)
                if blocker and (live.get("compacting") is not None or
                                "waiting on a request" in blocker or not owned_turn):
                    return {"ok": False,
                            "error": blocker + "; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                mode = session.get("collaboration_mode") or self._modes().get(tid) or "default"
                model = live.get("model") or session.get("model")
                effort = (live.get("effort") if "effort" in live else
                          session.get("effort"))
                if mode == "plan" and not model:
                    return {"ok": False, "error": "Codex model is unavailable; refresh and try again"}
                start_after_ended = False
                if owned_turn and hasattr(self.client, "steer_turn"):
                    try:
                        if inputs:
                            self.client.steer_turn(tid, text, inputs=inputs)
                        else:
                            self.client.steer_turn(tid, text)
                        return {"ok": True}
                    except CodexError as exc:
                        # The provider definitively rejected this steer before
                        # accepting input. Starting the same payload is safe;
                        # mismatched/ambiguous active turns remain queueable.
                        if exc.code != "turn_ended":
                            raise
                        start_after_ended = True
                if start_after_ended or not self._owns_active_turn(tid):
                    self._ensure_loaded(tid)
                    blocker, live = self._settings_runtime_blocker(tid)
                    if blocker and (not start_after_ended or
                                    live.get("compacting") is not None or
                                    "waiting on a request" in blocker):
                        return {"ok": False,
                                "error": blocker + "; message can be queued",
                                "code": "provider_control_unavailable", "queueable": True}
                    kwargs = {"mode": mode, "model": model, "effort": effort}
                    if inputs:
                        kwargs["inputs"] = inputs
                    self.client.start_turn(tid, text, **kwargs)
            elif typ == "mode":
                mode = str(action.get("mode") or "")
                if mode not in ("plan", "default"):
                    return {"ok": False, "error": "mode must be plan or default"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                self._ensure_loaded(tid)
                blocker, live = self._settings_runtime_blocker(tid)
                if blocker:
                    return {"ok": False, "error": blocker}
                model = live.get("model") or session.get("model")
                # An explicit live null is canonical (for example a TUI mode
                # that disables reasoning effort). Only fall back when App
                # Server omitted the field entirely.
                effort = (live.get("effort") if "effort" in live else
                          (session.get("effort") or
                           ("medium" if mode == "plan" else None)))
                self.client.set_mode(tid, mode, model, effort)
                warning = None
                durable = False
                with self._projection_commit_lock:
                    latest_meta = ((self._state().get("thread_meta") or {}).get(tid) or {})
                    next_revision = int(latest_meta.get("settings_revision") or 0) + 1
                    try:
                        durable = self._remember(tid, mode, {
                            "model": model, "effort": effort,
                            "settings_revision": next_revision},
                            expected_settings_revision=next_revision - 1)
                    except Exception as exc:
                        warning = ("Applied in Codex, but Fleet could not persist the mode: " +
                                   str(exc)[:300])
                    if not durable and warning is None:
                        warning = ("Applied in Codex, but Fleet could not durably save the mode; "
                                   "the live runtime remains updated")
                    with self._lock:
                        for current in self._sessions:
                            if current.get("native_session_id") == tid:
                                current["collaboration_mode"] = mode
                                current["model"] = model
                                current["effort"] = effort
                                current["settings_revision"] = next_revision
                                # A mode is not a pending request. Expose this only when
                                # App Server actually asks a structured question.
                                current["capabilities"]["answer_structured"] = False
                result = {"ok": True, "mode": mode, "durable": durable}
                if warning:
                    result["warning"] = warning
                return result
            elif typ == "interrupt":
                self.client.interrupt(tid)
            elif typ in ("archive", "close"):
                if typ == "close" and known and known.get("state") in (
                        "running", "stalled", "needs_you"):
                    self.client.interrupt(tid)
                try:
                    self.client.archive(tid)
                except Exception as exc:
                    # thread/start assigns an ID before the first turn creates a
                    # rollout. There is nothing provider-side to archive yet.
                    if "no rollout found for thread id" not in str(exc).lower():
                        raise
                self._forget(tid)
                with self._lock:
                    self._sessions = [session for session in self._sessions
                                      if session.get("native_session_id") != tid]
            elif typ == "compact":
                self._ensure_loaded(tid)
                blocker, _live = self._settings_runtime_blocker(tid)
                if blocker:
                    return {"ok": False, "error": blocker}
                self.client.compact(tid)
                self.client.thread_state.setdefault(tid, {})["compacting"] = 0
            elif typ == "review":
                self._ensure_loaded(tid)
                self.client.review(tid)
            elif typ == "skill":
                name = str(action.get("name") or "")
                skill = self._skills.get(tid, {}).get(name)
                if not skill:
                    return {"ok": False,
                            "error": "unknown or disabled Codex skill; refresh commands"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                inputs = [{"type": "skill", "name": skill["name"], "path": skill["path"]}]
                args = str(action.get("args") or "").strip()
                if args:
                    inputs.append({"type": "text", "text": args})
                self._ensure_loaded(tid)
                self.client.start_turn(
                    tid, "", mode=session.get("collaboration_mode") or "default",
                    model=session.get("model"), effort=session.get("effort"),
                    inputs=inputs)
            elif typ == "permission":
                return self.client.decide(action.get("nonce"), action.get("choice"))
            elif typ in ("multiq", "option"):
                answers = action.get("answers") if typ == "multiq" else [action]
                return self.client.answer_questions(action.get("nonce"), answers or [])
            elif typ == "elicitation":
                return self.client.answer_elicitation(
                    action.get("nonce"), action.get("choice"), action.get("content"))
            elif typ == "dismiss":
                nonce = str(action.get("nonce") or "")
                request = self.client.approvals.get(nonce)
                if not request:
                    return {"ok": False, "error": "stale Codex request"}
                if request.get("method") == "mcpServer/elicitation/request":
                    return self.client.answer_elicitation(nonce, "decline")
                if request.get("method") == "item/tool/requestUserInput":
                    self.client.interrupt(tid)
                    return {"ok": True, "dismissed_by": "turn_interrupt"}
                return self.client.decide(nonce, "cancel")
            elif typ == "relay":
                text = str(action.get("text") or "").strip()
                aid = str(action.get("agent_id") or "").strip()
                if not text or not aid:
                    return {"ok": False, "error": "agent and message are required"}
                relay = f"Relay this message to subagent {aid}: {text}"
                live = self.client.thread_state.get(tid, {})
                start_after_ended = False
                if self._owns_active_turn(tid) and hasattr(self.client, "steer_turn"):
                    try:
                        self.client.steer_turn(tid, relay)
                        return {"ok": True, "relayed_via": "parent"}
                    except CodexError as exc:
                        if exc.code != "turn_ended":
                            raise
                        start_after_ended = True
                if start_after_ended or not self._owns_active_turn(tid):
                    with self._lock:
                        session = next((s for s in self._sessions
                                        if s.get("native_session_id") == tid), {})
                    self._ensure_loaded(tid)
                    self.client.start_turn(tid, relay,
                        mode=session.get("collaboration_mode") or "default",
                        model=session.get("model"), effort=session.get("effort"))
                return {"ok": True, "relayed_via": "parent"}
            else:
                return {"ok": False, "error": f"Codex does not support {typ} here"}
            return {"ok": True}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            if isinstance(exc, CodexError):
                result.update(code=exc.code, queueable=exc.queueable)
            return result


def _epoch(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _latest_turn_lifecycle(thread):
    """Return lifecycle evidence that survives cross-App-Server thread reads.

    ChatGPT desktop turns can appear as ``interrupted`` to Fleet Dash's separate
    App Server while they are still running. The durable distinction is that an
    active turn has startedAt but no completedAt; a genuinely ended turn has a
    completion timestamp.
    """
    turns = thread.get("turns") or []
    if not turns:
        return {"active": False, "started_at": None, "completed_at": None,
                "turn_id": None}
    turn = turns[-1] or {}
    started_at = _epoch(turn.get("startedAt"))
    completed_at = _epoch(turn.get("completedAt"))
    status = str(turn.get("status") or "").replace("-", "").replace("_", "").lower()
    explicitly_active = status in {"active", "inprogress", "running", "started"}
    active = (turn.get("completedAt") is None and
              (explicitly_active or started_at is not None))
    return {"active": active, "started_at": started_at,
            "completed_at": completed_at, "status": turn.get("status"),
            "turn_id": turn.get("id"), "error": turn.get("error")}


def _millis(value):
    ep = _epoch(value)
    return round(ep * 1000) if ep else None


def _usage_total(usage):
    latest = usage.get("last") or usage.get("total") or usage
    if latest.get("totalTokens") is not None:
        return int(latest.get("totalTokens") or 0)
    return sum(int(latest.get(k) or 0) for k in
               ("inputTokens", "outputTokens", "reasoningOutputTokens"))


def _usage_cumulative(usage):
    """Return only provider-reported cumulative usage; never relabel a turn as lifetime."""
    total = usage.get("total") if isinstance(usage, dict) else None
    if not isinstance(total, dict):
        return None
    if total.get("totalTokens") is not None:
        return int(total.get("totalTokens") or 0)
    fields = ("inputTokens", "outputTokens", "reasoningOutputTokens")
    if not any(total.get(key) is not None for key in fields):
        return None
    return sum(int(total.get(key) or 0) for key in fields)


def _usage_window(usage):
    try:
        return int(usage.get("modelContextWindow") or 0)
    except (TypeError, ValueError):
        return 0


def _token_breakdown(usage):
    latest = usage.get("last") or usage.get("total") or usage
    return {"in": latest.get("inputTokens"),
            "cache_read": latest.get("cachedInputTokens"),
            "out": latest.get("outputTokens"),
            "reasoning": latest.get("reasoningOutputTokens")}


def _account_usage(limits, tokens, info=None):
    """Normalize Codex account quota data for Fleet Dash's provider header."""
    snapshots = limits.get("rateLimitsByLimitId") or {}
    if not snapshots and limits.get("rateLimits"):
        snapshots = {"codex": limits["rateLimits"]}
    buckets = []
    plan_type = None
    for limit_id, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        plan_type = plan_type or snapshot.get("planType")
        name = snapshot.get("limitName")
        for slot in ("primary", "secondary"):
            window = snapshot.get(slot)
            if not isinstance(window, dict) or window.get("usedPercent") is None:
                continue
            minutes = window.get("windowDurationMins")
            if minutes == 300:
                window_name = "5-hour"
            elif minutes == 10080:
                window_name = "weekly"
            elif minutes:
                window_name = f"{round(minutes / 60)}-hour"
            else:
                window_name = slot
            label = f"{name} {window_name}" if name else window_name
            resets_at = window.get("resetsAt")
            reset_iso = None
            if resets_at:
                try:
                    reset_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(int(resets_at)))
                except (TypeError, ValueError, OverflowError):
                    pass
            buckets.append({"id": f"{limit_id}:{slot}", "label": label,
                            "used_pct": int(window.get("usedPercent") or 0),
                            "reset": reset_iso, "window_minutes": minutes})
    current = ((info or {}).get("account") or {})
    plan_type = current.get("planType") or plan_type
    summary = tokens.get("summary") or {}
    return {"provider": "codex", "plan_type": plan_type,
            "account_id": current.get("id") or current.get("email") or "active",
            "email": current.get("email"),
            "buckets": buckets,
            "lifetime_tokens": summary.get("lifetimeTokens"),
            "daily_usage": tokens.get("dailyUsageBuckets") or [],
            "reset_credits": (limits.get("rateLimitResetCredits") or {}).get(
                "availableCount", 0)}


def _conversation(thread):
    messages = []
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            typ = item.get("type", "")
            if typ in ("userMessage", "agentMessage"):
                text = _item_text(item)
                messages.append({"role": "user" if typ == "userMessage" else "assistant",
                                 "text": text, "ts": item.get("createdAt")})
            elif typ == "plan":
                messages.append({"role": "event", "kind": "plan", "title": "Plan",
                                 "detail": str(item.get("text") or "")[:4000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "reasoning":
                detail = "\n".join(str(x) for x in
                                   ((item.get("summary") or []) + (item.get("content") or [])))
                messages.append({"role": "event", "kind": "reasoning",
                                 "title": "Reasoning", "detail": detail[:4000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ in ("commandExecution", "fileChange", "mcpToolCall",
                         "dynamicToolCall", "collabAgentToolCall"):
                raw_arg = (item.get("command") or item.get("changes") or
                           item.get("arguments") or item.get("prompt") or item.get("server") or "")
                if not isinstance(raw_arg, str):
                    raw_arg = json.dumps(raw_arg, separators=(",", ":"))
                name = item.get("tool") or typ
                status = item.get("status")
                exit_code = item.get("exitCode")
                failed = None
                if isinstance(exit_code, int):
                    failed = exit_code != 0
                elif str(status or "").lower() in ("failed", "error", "interrupted",
                                                    "cancelled", "canceled"):
                    failed = True
                elif str(status or "").lower() in ("completed", "succeeded", "success"):
                    failed = False
                messages.append({"role": "tool", "name": name, "arg": raw_arg[:500],
                                 "command": raw_arg[:2000] if typ == "commandExecution" else None,
                                 "result": (item.get("aggregatedOutput") or item.get("result") or
                                            item.get("error") or item.get("status")),
                                 "status": status, "exit_code": exit_code, "failed": failed,
                                 "completed_at": item.get("completedAt"),
                                 "ts": item.get("createdAt")})
            elif typ == "webSearch":
                messages.append({"role": "tool", "name": "webSearch",
                                 "arg": str(item.get("query") or "")[:500],
                                 "result": item.get("action"), "ts": item.get("createdAt")})
            elif typ == "imageView":
                messages.append({"role": "tool", "name": "imageView",
                                 "arg": str(item.get("path") or "")[:500],
                                 "ts": item.get("createdAt")})
            elif typ == "imageGeneration":
                messages.append({"role": "tool", "name": "imageGeneration",
                                 "arg": str(item.get("revisedPrompt") or "")[:500],
                                 "result": item.get("status"), "ts": item.get("createdAt")})
            elif typ == "sleep":
                messages.append({"role": "event", "kind": "sleep", "title": "Paused",
                                 "detail": f"{item.get('durationMs') or 0} ms",
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ in ("enteredReviewMode", "exitedReviewMode"):
                messages.append({"role": "event", "kind": "review",
                                 "title": ("Entered review mode" if typ.startswith("entered")
                                           else "Exited review mode"),
                                 "detail": str(item.get("review") or "")[:4000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "contextCompaction":
                messages.append({"role": "event", "kind": "compact",
                                 "title": "Conversation compacted", "detail": "",
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "subAgentActivity":
                messages.append({"role": "event", "kind": "agent",
                                 "title": f"Subagent {item.get('kind') or 'activity'}",
                                 "detail": str(item.get("agentPath") or
                                               item.get("agentThreadId") or "")[:1000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "hookPrompt":
                messages.append({"role": "event", "kind": "hook",
                                 "title": "Hook prompt", "detail": "Provider hook input",
                                 "level": "info", "ts": item.get("createdAt")})
            else:
                messages.append({"role": "event", "kind": "unknown",
                                 "title": f"Unsupported Codex item: {typ or 'unknown'}",
                                 "detail": _safe_json(item)[:2000], "level": "warning",
                                 "ts": item.get("createdAt")})
    return messages


def _item_text(item):
    if item.get("text"):
        return str(item["text"])
    out = []
    for part in item.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            out.append(str(part["text"]))
        elif part.get("type") in ("image", "localImage"):
            out.append(f"[image: {part.get('path') or part.get('url') or 'attached'}]")
        elif part.get("type") in ("mention", "skill"):
            out.append(str(part.get("name") or part.get("path") or ""))
    return "\n".join(filter(None, out))


def _revision(thread, live):
    fingerprint = []
    for turn in thread.get("turns") or []:
        fingerprint.append((turn.get("id"), turn.get("status"),
                            [(item.get("id"), item.get("type"), item.get("status"),
                              len(_item_text(item))) for item in turn.get("items") or []]))
    digest = hashlib.sha256(_safe_json(fingerprint).encode()).hexdigest()[:12]
    updated_ms = _millis(thread.get("updatedAt")) or _millis(thread.get("createdAt")) or 0
    return f"{updated_ms}:{int(live.get('revision') or 0)}:{digest}"


def _last_message(messages, fallback=None):
    for message in reversed(messages):
        if message.get("role") in ("user", "assistant") and message.get("text"):
            text = str(message["text"]).strip()
            return {"role": message["role"],
                    "text": text[:799] + "…" if len(text) > 800 else text}
    if fallback:
        text = str(fallback).strip()
        return {"role": "user",
                "text": text[:799] + "…" if len(text) > 800 else text}
    return None


def _latest_prose(messages):
    for message in reversed(messages):
        if message.get("role") in ("user", "assistant") and message.get("text"):
            return {"role": message["role"], "text": str(message["text"]).strip()}
    return None


def _files(thread, cwd):
    root = os.path.realpath(cwd) if cwd else None
    found = {}

    def add(raw, caption, ts=None, change_kind=None):
        if not isinstance(raw, str) or not raw:
            return
        path = raw if os.path.isabs(raw) else os.path.join(cwd or "", raw)
        path = os.path.realpath(os.path.expanduser(path))
        if root and path != root and not path.startswith(root + os.sep):
            return
        ext = os.path.splitext(path)[1].lower()
        # Dict insertion order is the delivery chronology used below. Repeated
        # changes must become the newest file, not keep their first-seen slot.
        found.pop(path, None)
        found[path] = {"name": os.path.basename(path), "path": path,
                       "kind": "image" if ext in
                       (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg") else "text",
                       "missing": not os.path.isfile(path), "caption": caption,
                       "ts": ts, "source": "codex", "change_kind": change_kind,
                       "delivered": False}

    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if item.get("type") == "fileChange":
                for change in item.get("changes") or []:
                    if isinstance(change, dict):
                        kind = change.get("kind") or "updated"
                        add(change.get("path"), f"Codex {kind} this file",
                            item.get("createdAt"), kind)
            elif item.get("type") == "imageGeneration":
                add(item.get("savedPath"), "Codex generated this image",
                    item.get("createdAt"), "generated")
    return list(reversed(list(found.values())))


def _elicitation_pending(nonce, params):
    schema = params.get("requestedSchema") or {}
    fields = []
    properties = schema.get("properties") or {} if isinstance(schema, dict) else {}
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    for name, raw in properties.items():
        field = raw if isinstance(raw, dict) else {}
        field_type = field.get("type")
        multi = field_type == "array"
        enum_source = field.get("items") if multi else field
        enum_source = enum_source if isinstance(enum_source, dict) else {}
        values = enum_source.get("enum") or []
        options = [{"value": value, "label": str(value), "description": ""}
                   for value in values]
        fields.append({"name": name, "label": field.get("title") or name,
                       "description": field.get("description") or "",
                       "type": "select" if options else
                               ("boolean" if field_type == "boolean" else "text"),
                       "multiSelect": multi, "options": options,
                       "required": name in required, "secret": False})
    return {"kind": "elicitation", "nonce": nonce,
            "server": params.get("serverName"), "message": params.get("message") or "",
            "mode": params.get("mode"), "url": params.get("url"), "fields": fields,
            "decisions": ["accept", "decline", "cancel"]}


def _agents(thread, parent_id):
    found = {}

    def remember(aid, agent):
        previous = found.get(aid)
        if (previous and previous.get("state") in ("done", "ended") and
                agent.get("state") not in ("done", "ended")):
            agent["state"] = previous["state"]
        found[aid] = agent

    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if item.get("type") == "subAgentActivity":
                aid = item.get("agentThreadId")
                if aid:
                    activity = item.get("kind")
                    state = ({"completed": "done", "interrupted": "ended",
                              "failed": "ended", "errored": "ended",
                              "shutdown": "ended"}.get(activity, "running"))
                    remember(aid, {
                        "agent_id": aid, "session_id": f"codex:{parent_id}",
                        "agent_type": (item.get("agentPath") or "codex").split("/")[-1],
                        "description": item.get("agentPath") or "Codex subagent",
                        "depth": 0, "model": "", "family": "codex", "effort": None,
                        "state": state, "total_tokens": None, "cost": None,
                        "cost_source": "unavailable", "tokens": {}, "spark": [],
                        "tok_per_s": None, "started": None, "last": None,
                        "last_msg": None, "convo_v": 0})
                continue
            if item.get("type") != "collabAgentToolCall":
                continue
            states = item.get("agentsStates") or {}
            for aid in item.get("receiverThreadIds") or []:
                raw = (states.get(aid) or {}).get("status")
                state = {"pendingInit": "running", "running": "running",
                         "completed": "done", "interrupted": "ended",
                         "errored": "ended", "shutdown": "ended"}.get(raw, "running")
                remember(aid, {
                    "agent_id": aid, "session_id": f"codex:{parent_id}",
                    "agent_type": "codex", "description": item.get("prompt") or "",
                    "depth": 0, "model": item.get("model") or "",
                    "family": "codex", "effort": item.get("reasoningEffort"),
                    "state": state, "total_tokens": None, "cost": None,
                    "cost_source": "unavailable", "tokens": {}, "spark": [],
                    "tok_per_s": None,
                    "started": None, "last": None, "last_msg": None})
    return list(found.values())
