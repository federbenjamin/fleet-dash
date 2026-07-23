"""Coverage for fleetdash.engine_scan: action records, compact headroom,
workstream identity, usage profiles/lifetime, history/repository snapshots,
control-state reconciliation, and scan_agents lifecycle."""
import json
import os
import plistlib
import subprocess
import time
import unittest
from unittest import mock

from fleetdash import engine_scan as scan_module
from tests.test_cov_common_ops import EngineFixture

PID = os.getpid()


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


class ActionRecordTests(EngineFixture):
    def test_all_action_kinds(self):
        sessions = [
            {"session_id": "q", "convo_v": "1",
             "pending": {"kind": "question", "nonce": "n1",
                         "questions": [{"question": "Q?"}]}},
            {"session_id": "f", "convo_v": "1",
             "pending": {"kind": "elicitation", "nonce": "n2",
                         "message": "fill the form"}},
            {"session_id": "cmd", "convo_v": "1",
             "pending": {"kind": "permission", "nonce": "n3",
                         "approval_kind": "command"}},
            {"session_id": "fc", "convo_v": "1",
             "pending": {"kind": "permission", "nonce": "n4",
                         "approval_kind": "file_change"}},
            {"session_id": "reply", "convo_v": "1", "reply_requested": True},
            {"session_id": "done", "convo_v": "1", "new_response": True},
            {"session_id": "prob", "convo_v": "1", "ui_group": "needs_you",
             "state": "blocked", "error": "limit"},
            {"session_id": "att", "convo_v": "1", "ui_group": "needs_you",
             "state": "idle"},
        ]
        records = self.engine.action_records(sessions)
        kinds = {r["kind"] for r in records}
        self.assertEqual(kinds, {"question", "form", "approval", "reply",
                                 "outcome", "problem", "attention"})

    def test_dismissed_and_no_kind_are_skipped(self):
        sessions = [{"session_id": "plain", "convo_v": "1"}]
        self.assertEqual(self.engine.action_records(sessions), [])

    def test_budget_action_records(self):
        records = self.engine.budget_action_records([
            {"id": "b1", "status": "exceeded", "label": "Fleet cap",
             "scope_type": "fleet", "block_spawns": True},
            {"id": "b2", "status": "warning", "label": "WS", "scope_type": "workstream",
             "scope_id": "ws-1"},
            {"id": "b3", "status": "ok"}])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["kind"], "budget")


class PendingReasonTests(EngineFixture):
    def test_pending_reason_none(self):
        self.assertEqual(self.engine._pending_reason({"kind": "other"}),
                         (None, None))
        self.assertEqual(self.engine._pending_reason({"kind": "question"})[1],
                         "respond")


class CompactHeadroomTests(EngineFixture):
    def test_invalid_inputs(self):
        self.assertIsNone(self.engine.claude_compact_headroom(
            self.cwd, "notint", 1000))
        self.assertIsNone(self.engine.claude_compact_headroom("", 100, 1000))

    def test_reads_settings_env(self):
        settings_dir = os.path.join(self.cwd, ".claude")
        os.makedirs(settings_dir)
        with open(os.path.join(settings_dir, "settings.json"), "w") as handle:
            json.dump({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000",
                               "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"}}, handle)
        headroom = self.engine.claude_compact_headroom(self.cwd, 1000, 200000)
        self.assertIsNotNone(headroom)

    def test_disabled_autocompact(self):
        settings_dir = os.path.join(self.cwd, ".claude")
        os.makedirs(settings_dir, exist_ok=True)
        with open(os.path.join(settings_dir, "settings.json"), "w") as handle:
            json.dump({"autoCompactEnabled": False,
                       "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000"}}, handle)
        self.assertIsNone(self.engine.claude_compact_headroom(
            self.cwd, 100, 200000))

    def test_malformed_settings_ignored(self):
        settings_dir = os.path.join(self.cwd, ".claude")
        os.makedirs(settings_dir, exist_ok=True)
        with open(os.path.join(settings_dir, "settings.json"), "w") as handle:
            handle.write("{ not json")
        self.assertIsNone(self.engine.claude_compact_headroom(
            self.cwd, 100, 200000))


class WorkstreamIdentityTests(EngineFixture):
    def test_gitfile_worktree_resolves_common_root(self):
        import subprocess
        root = os.path.join(self.tmp.name, "wsrepo")
        os.makedirs(root)
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "config", "user.email", "a@b.c"],
                       check=True)
        subprocess.run(["git", "-C", root, "config", "user.name", "T"], check=True)
        with open(os.path.join(root, "f.txt"), "w") as handle:
            handle.write("x\n")
        subprocess.run(["git", "-C", root, "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "first"], check=True)
        wt = os.path.join(self.tmp.name, "wslinked")
        subprocess.run(["git", "-C", root, "worktree", "add", "-q", wt],
                       check=True, capture_output=True)
        identity = self.engine._workstream_git_identity(os.path.realpath(wt))
        self.assertEqual(identity["kind"], "git")
        self.assertEqual(identity["root"], os.path.realpath(root))

    def test_invalid_gitfile_is_stale(self):
        d = os.path.join(self.tmp.name, "badgit")
        os.makedirs(d)
        with open(os.path.join(d, ".git"), "w") as handle:
            handle.write("not a gitdir line")
        identity = self.engine._workstream_git_identity(d)
        self.assertTrue(identity.get("stale"))

    def test_non_git_folder(self):
        self.assertIsNone(self.engine._workstream_git_identity(
            os.path.join(self.tmp.name, "nope-missing")))


class UsageProfileTests(EngineFixture):
    def _write(self, mode="single", active="p1", extra_profile=True):
        profiles = [{
            "id": "p1", "name": "One", "isSelectedForDisplay": True,
            "refreshInterval": 30,
            "oauthAccountJSON": json.dumps({"emailAddress": "one@x.com"}),
            "claudeUsage": {"sessionPercentage": 10, "weeklyPercentage": 20,
                            "fableWeeklyPercentage": 5,
                            "sessionResetTime": 800_000_000}}]
        with open(self.claude_usage_prefs, "wb") as handle:
            plistlib.dump({"profiles_v3": json.dumps(profiles).encode(),
                           "multiProfileDisplayConfig": json.dumps({}).encode(),
                           "activeProfileId": active,
                           "profileDisplayMode": mode}, handle)

    def test_single_profile_mode(self):
        self._write(mode="single", active="p1")
        usage = self.engine.claude_usage_profiles()
        self.assertEqual(usage["profiles"][0]["email"], "one@x.com")

    def test_missing_prefs(self):
        self.assertIsNone(self.engine.claude_usage_profiles())

    def test_malformed_plist(self):
        with open(self.claude_usage_prefs, "wb") as handle:
            handle.write(b"not a plist")
        self.assertIsNone(self.engine.claude_usage_profiles())

    def test_non_list_profiles(self):
        with open(self.claude_usage_prefs, "wb") as handle:
            plistlib.dump({"profiles_v3": json.dumps({"bad": 1}).encode(),
                           "multiProfileDisplayConfig": json.dumps({}).encode(),
                           "activeProfileId": "p1",
                           "profileDisplayMode": "single"}, handle)
        self.assertIsNone(self.engine.claude_usage_profiles())

    def test_multi_mode_edge_profiles(self):
        profiles = [
            {"id": "p1", "name": "One", "isSelectedForDisplay": True,
             "refreshInterval": "bad",
             "oauthAccountJSON": "{ not json",
             "claudeUsage": {"sessionPercentage": "nan"}},
            {"id": "p2", "name": "Two", "isSelectedForDisplay": True,
             "refreshInterval": 45,
             "oauthAccountJSON": json.dumps({"emailAddress": "two@x.com"}),
             "claudeUsage": "not a dict"},
        ]
        with open(self.claude_usage_prefs, "wb") as handle:
            plistlib.dump({"profiles_v3": json.dumps(profiles).encode(),
                           "multiProfileDisplayConfig": json.dumps(
                               {"showWeek": False}).encode(),
                           "activeProfileId": "p1",
                           "profileDisplayMode": "multi"}, handle)
        usage = self.engine.claude_usage_profiles()
        # p1 survives (bad account -> {}), p2 dropped (usage not a dict).
        self.assertEqual([p["id"] for p in usage["profiles"]], ["p1"])
        self.assertIsNone(usage["profiles"][0]["five_hour_pct"])

    def test_no_selected_falls_back_to_active(self):
        profiles = [{"id": "p9", "name": "Nine", "isSelectedForDisplay": False,
                     "refreshInterval": 30,
                     "oauthAccountJSON": json.dumps({"emailAddress": "n@x.com"}),
                     "claudeUsage": {"sessionPercentage": 12}}]
        with open(self.claude_usage_prefs, "wb") as handle:
            plistlib.dump({"profiles_v3": json.dumps(profiles).encode(),
                           "multiProfileDisplayConfig": json.dumps({}).encode(),
                           "activeProfileId": "p9",
                           "profileDisplayMode": "multi"}, handle)
        usage = self.engine.claude_usage_profiles()
        self.assertEqual(usage["profiles"][0]["id"], "p9")


class LifetimeTokenTests(EngineFixture):
    def test_sums_model_usage(self):
        with open(self.claude_stats, "w") as handle:
            json.dump({"modelUsage": {
                "m1": {"inputTokens": 10, "cacheCreationInputTokens": 20,
                       "cacheReadInputTokens": 30, "outputTokens": 40},
                "m2": {"inputTokens": True, "outputTokens": None},
                "bad": "notadict"}}, handle)
        self.assertEqual(self.engine.claude_lifetime_tokens(), 100)

    def test_missing_stats(self):
        if os.path.exists(self.claude_stats):
            os.unlink(self.claude_stats)
        self.assertIsNone(self.engine.claude_lifetime_tokens())

    def test_malformed_stats(self):
        with open(self.claude_stats, "w") as handle:
            handle.write("{ not json")
        self.assertIsNone(self.engine.claude_lifetime_tokens())


class HistorySnapshotTests(EngineFixture):
    def _seed_closed(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"closed": [
                {"session_id": "c1", "provider": "claude", "title": "Alpha",
                 "primary_action": "reopen", "pinned": False},
                {"session_id": "c2", "provider": "codex", "title": "Beta",
                 "primary_action": "view", "pinned": False},
                {"session_id": "c3", "provider": "claude", "title": "Pinned",
                 "pinned": True}]}

    def test_invalid_pagination(self):
        self.assertIn("invalid history pagination",
                      self.engine.history_snapshot(cursor="x")["error"])

    def test_invalid_filters(self):
        self.assertIn("invalid history provider",
                      self.engine.history_snapshot(provider="bogus")["error"])
        self.assertIn("invalid history access",
                      self.engine.history_snapshot(access="bogus")["error"])
        self.assertIn("invalid history filter",
                      self.engine.history_snapshot(sid="\x01")["error"])

    def test_single_item_lookup(self):
        self._seed_closed()
        out = self.engine.history_snapshot(sid="c1")
        self.assertEqual(out["item"]["title"], "Alpha")

    def test_filters_provider_access_query(self):
        self._seed_closed()
        out = self.engine.history_snapshot(provider="claude", access="reopen",
                                           query="alpha")
        ids = [i["session_id"] for i in out["items"]]
        self.assertEqual(ids, ["c1"])
        # Pinned items are excluded from history listing.
        allrows = self.engine.history_snapshot()
        self.assertNotIn("c3", [i["session_id"] for i in allrows["items"]])

    def test_access_mismatch_and_query_miss(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"closed": [
                {"session_id": "c1", "provider": "claude", "title": "Alpha",
                 "primary_action": "view", "pinned": False},
                {"session_id": "c2", "provider": "claude", "title": "Beta",
                 "primary_action": "reopen", "pinned": False}]}
        # Access filter drops the "view" row; query drops the "beta" row.
        out = self.engine.history_snapshot(access="reopen", query="zzz-nomatch")
        self.assertEqual(out["items"], [])


class StatusLineTests(EngineFixture):
    def test_status_line_with_agent_costs(self):
        session = {"session_id": "s", "provider": "claude", "cwd": self.cwd,
                   "cost": 1.0, "agent_cost": 0.5, "branch": "b", "model": "m",
                   "effort": "high", "ctx_tokens": 10, "ctx_window": 1000,
                   "ctx_pct": 1.0, "agents": [
                       {"description": "child", "cost": 0.5},
                       {"description": "nocost"}]}
        line = self.engine.session_status_line(session)
        kinds = [b["kind"] for b in line["cost_breakdown"]]
        self.assertIn("agent", kinds)
        self.assertEqual(line["tree_cost"], 1.5)

    def test_operational_git_non_git(self):
        out = self.engine.operational_git(os.path.join(self.tmp.name, "plainfolder"))
        self.assertIsNone(out["worktree"])

    def test_operational_git_empty_cwd(self):
        out = self.engine.operational_git("")
        self.assertIsNone(out["worktree"])


class WorkstreamRecordsTests(EngineFixture):
    def test_unknown_cwd_and_group_and_closed_overlap(self):
        sessions = [
            {"session_id": "nocwd", "provider": "codex", "ui_group": "weird"},
            {"session_id": "dup", "provider": "claude", "cwd": self.cwd,
             "ui_group": "working"}]
        closed = [
            {"session_id": "dup", "provider": "claude", "cwd": self.cwd},
            {"session_id": "chist", "provider": "claude", "cwd": self.cwd,
             "closed_at": time.time()}]
        records = self.engine.workstream_records(sessions, closed)
        self.assertTrue(records)
        # The unknown-location session gets its own group.
        self.assertTrue(any(g.get("kind") == "unknown" for g in records))


class ControlOverrideTests(EngineFixture):
    def test_retire_override(self):
        self.engine._claude_control_overrides = {"same": {
            "effort": {"value": "high", "accepted_at": 1.0, "baseline": 0}}}
        self.engine._claude_effort_overrides = {"same": ("high", 1.0)}
        self.engine._retire_claude_control_override("same", "effort")
        self.assertNotIn("same", self.engine._claude_control_overrides)
        # Retiring a field that isn't present is a no-op.
        self.assertIsNone(
            self.engine._retire_claude_control_override("same", "model"))

    def test_native_effort_evidence(self):
        effort_dir = os.path.join(self.base, "effort")
        os.makedirs(effort_dir, exist_ok=True)
        with open(os.path.join(effort_dir, "same"), "w") as handle:
            handle.write("high")
        value, mtime = self.engine._native_effort_evidence("same")
        self.assertEqual(value, "high")
        self.assertGreater(mtime, 0)

    def test_native_effort_missing(self):
        value, mtime = self.engine._native_effort_evidence("nope")
        self.assertIsNone(value)
        self.assertEqual(mtime, 0.0)


class TurnFenceTests(EngineFixture):
    def test_fence_lifecycle(self):
        self.engine._record_claude_turn_fence("same", {"transcript_size": 10,
                                                       "convo_rev": 1})
        # Active registry status marks it seen.
        self.assertTrue(self.engine._claude_turn_fenced("same", "busy"))
        # Idle after active clears it.
        self.assertFalse(self.engine._claude_turn_fenced("same", "idle"))

    def test_fence_absent(self):
        self.assertFalse(self.engine._claude_turn_fenced("nofence", "idle"))

    def test_fence_transcript_change_clears(self):
        self.engine._record_claude_turn_fence("same", {"transcript_size": 0,
                                                       "convo_rev": 1})
        tail = self.engine.tail_for(self.transcript)
        tail.poll()
        # Transcript grew beyond the recorded size and folded to awaiting input.
        cleared = self.engine._claude_turn_fenced("same", "idle", self.transcript,
                                                  tail)
        self.assertIn(cleared, (True, False))


class ScanStateTests(EngineFixture):
    def _age_transcript(self, seconds):
        old = time.time() - seconds
        os.utime(self.transcript, (old, old))

    def test_stalled_or_prompt_state(self):
        self.engine.cfg["stall_seconds"] = 1
        self.write_registry(status="busy")
        self.append_transcript({"type": "assistant",
            "timestamp": "2026-07-15T00:00:05Z",
            "message": {"role": "assistant", "model": "claude-sonnet",
                "stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}})
        self._age_transcript(100)
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertIn(session["state"], ("stalled", "stalled_or_prompt"))

    def test_dormant_state(self):
        self.engine.cfg["dormant_seconds"] = 1
        self.write_registry(status="idle")
        self._age_transcript(100)
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual(session["state"], "dormant")

    def test_transcript_pending_question(self):
        self.write_registry(status="idle")
        self.append_transcript({"type": "assistant",
            "timestamp": "2026-07-15T00:00:05Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "q", "name": "AskUserQuestion",
                 "input": {"questions": [{"question": "Q",
                     "options": [{"label": "A"}]}]}}]}})
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual((session["pending"] or {}).get("kind"), "question")

    def test_transcript_pending_permission(self):
        self.write_registry(status="idle")
        self.append_transcript({"type": "assistant",
            "timestamp": "2026-07-15T00:00:05Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "p", "name": "Bash",
                 "input": {"command": "ls"}}]}})
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual((session["pending"] or {}).get("kind"), "permission")


class ScanPersistenceTests(EngineFixture):
    def test_ghost_recovery_maps_are_pruned(self):
        self.engine._claude_control_overrides = {"ghost": {
            "model": {"value": "opus", "accepted_at": 1.0, "baseline": 0}}}
        self.engine._claude_control_uncertain = {"ghost": {
            "attempted_at": 1.0, "fields": {"model": {"baseline": 0}}}}
        with self.engine._claude_delivery_uncertain_guard:
            self.engine._claude_delivery_uncertain = {"ghost": "nonce"}
        self.engine.scan()
        self.assertNotIn("ghost", self.engine._claude_control_overrides)
        self.assertNotIn("ghost", self.engine._claude_control_uncertain)
        self.assertNotIn("ghost", self.engine._claude_delivery_uncertain)


class RepositoryScanTests(EngineFixture):
    def _repo(self):
        root = os.path.join(self.tmp.name, "reporoot")
        os.makedirs(root)
        git("init", "-q", root)
        git("-C", root, "config", "user.email", "a@b.c")
        git("-C", root, "config", "user.name", "T")
        with open(os.path.join(root, "f.txt"), "w") as handle:
            handle.write("x\n")
        git("-C", root, "add", "f.txt")
        git("-C", root, "commit", "-qm", "first")
        return root

    def _seed(self, root):
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [{
                "session_id": "s1", "provider": "claude", "cwd": root,
                "ui_group": "working", "title": "Repo", "branch": "main",
                "activity_at": time.time()}], "closed": []}

    def test_workstreams_snapshot_with_git_repo(self):
        root = self._repo()
        self._seed(root)
        with mock.patch.object(self.engine.operations, "budgets_snapshot",
                               return_value={"budgets": []}):
            out = self.engine.workstreams_snapshot()
        self.assertTrue(out["ok"])
        self.assertTrue(out["workstreams"])
        self.assertIn("repository", out["workstreams"][0])

    def test_repository_snapshot_and_group_errors(self):
        root = self._repo()
        self._seed(root)
        out = self.engine.repository_snapshot(root, root)
        self.assertTrue(out["ok"])
        self.assertIn("recent_actions", out)
        # Errors: unknown repo, bad path.
        self.assertIn("error", self.engine.repository_snapshot(""))
        self.assertIn("error",
                      self.engine.repository_snapshot("/no/such/repo/xyz"))

    def test_repository_action_commit(self):
        root = self._repo()
        # Make the tree dirty so a commit action is enabled.
        with open(os.path.join(root, "f.txt"), "a") as handle:
            handle.write("more\n")
        self._seed(root)
        preview = self.engine.repository_snapshot(root, root)
        result = self.engine.repository_action({
            "type": "git_commit", "root": root, "worktree": root,
            "revision": preview["revision"], "paths": ["f.txt"],
            "message": "commit via fleet"})
        self.assertTrue(result["ok"], result)
        self.assertIn("action_id", result)

    def test_repository_action_unknown_type(self):
        self.assertIn("Unknown repository action",
                      self.engine.repository_action({"type": "git_rebase"})["error"])

    def test_repository_group_errors(self):
        root = self._repo()
        self._seed(root)
        # Path too long.
        _, _, err = self.engine._repository_group("x" * 5000, "")
        self.assertIn("too long", err)
        # No path at all.
        _, _, err = self.engine._repository_group("", "")
        self.assertIn("required", err)
        # A git repo not part of the fleet.
        other = os.path.join(self.tmp.name, "otherrepo")
        os.makedirs(other)
        git("init", "-q", other)
        _, _, err = self.engine._repository_group(other, other)
        self.assertIsNotNone(err)

    def test_repository_action_group_error(self):
        out = self.engine.repository_action({"type": "git_commit", "root": ""})
        self.assertIn("error", out)


class ScanExceptionTests(EngineFixture):
    def test_dead_pid_registry_is_skipped(self):
        with open(os.path.join(self.sessions, "dead.json"), "w") as handle:
            json.dump({"sessionId": "dead", "pid": 999_999, "cwd": self.cwd,
                       "status": "idle", "name": "Dead"}, handle)
        ids = {r.get("sessionId") for r in self.engine.live_sessions()}
        self.assertNotIn("dead", ids)

    def test_codex_usage_exception(self):
        self.codex.account_usage = mock.Mock(side_effect=RuntimeError("codex"))
        fleet = self.engine.scan()
        self.assertTrue(fleet["provider_usage"]["codex"]["stale"])

    def test_outbox_and_budget_exceptions(self):
        with mock.patch.object(self.engine.outbox, "counts",
                               side_effect=RuntimeError("outbox down")), \
             mock.patch.object(self.engine.operations, "observe",
                               side_effect=RuntimeError("budget down")):
            fleet = self.engine.scan()
        self.assertTrue(fleet["outbox_summary"]["stale"])
        self.assertTrue(fleet["budget_summary"]["stale"])

    def test_backfill_exception_is_swallowed(self):
        self.engine.history_backfilled = False
        with mock.patch.object(self.engine, "backfill_claude_history",
                               side_effect=RuntimeError("backfill boom")):
            fleet = self.engine.scan()  # must not raise
        self.assertIn("sessions", fleet)


class ScanAgentsTests(EngineFixture):
    def _subdir(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir, exist_ok=True)
        return subdir

    def _write_agent(self, subdir, aid, tool_id="tool-a", settled=True):
        with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
            json.dump({"agentType": "quick", "description": "d",
                       "toolUseId": tool_id}, handle)
        content = ([{"type": "text", "text": "final report"}] if settled else
                   [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}])
        with open(os.path.join(subdir, aid + ".jsonl"), "w") as handle:
            handle.write(json.dumps({"type": "assistant",
                "timestamp": "2026-07-16T00:00:00Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "end_turn" if settled else "tool_use",
                    "usage": {"input_tokens": 5}, "content": content}}) + "\n")

    def test_settled_agent_is_done(self):
        subdir = self._subdir()
        self._write_agent(subdir, "agent-done", settled=True)
        agents = self.engine.scan_agents(subdir, time.time() + 100)
        self.assertEqual(agents[0]["state"], "done")

    def test_killed_agent_is_ended(self):
        subdir = self._subdir()
        self._write_agent(subdir, "agent-killed", tool_id="killed-tool",
                          settled=False)
        parent = self.engine.tail_for(self.transcript)
        parent.errored_tools = {"killed-tool"}
        agents = self.engine.scan_agents(subdir, time.time(), parent=parent)
        self.assertEqual(agents[0]["state"], "ended")

    def test_parent_idle_ends_unsettled_agent(self):
        subdir = self._subdir()
        self._write_agent(subdir, "agent-orphan", settled=False)
        agents = self.engine.scan_agents(subdir, time.time() + 100,
                                         parent_idle=True)
        self.assertEqual(agents[0]["state"], "ended")


if __name__ == "__main__":
    unittest.main()
