"""Coverage for fleetdash.engine_worktree: bounded process probes, worktree
parsing, and preview/cleanup ticket flow against real temporary git repos."""
import os
import subprocess
import sys
import time
import unittest

from fleetdash.engine import Engine
from tests.test_cov_common_ops import EngineFixture


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


class BoundedProcessTests(unittest.TestCase):
    def test_missing_binary(self):
        out = Engine._bounded_process(["/no/such/binary-xyz"])
        self.assertFalse(out["ok"])
        self.assertIsNone(out["code"])

    def test_success_output(self):
        out = Engine._bounded_process([sys.executable, "-c", "print('hello')"])
        self.assertTrue(out["ok"])
        self.assertIn("hello", out["stdout"])

    def test_truncation(self):
        out = Engine._bounded_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('a'*100000)"],
            max_output=64)
        self.assertTrue(out["truncated"])
        self.assertIn("output limit", out["stderr"])

    def test_timeout(self):
        out = Engine._bounded_process([sys.executable, "-c",
            "import time; time.sleep(5)"], timeout=0.3)
        self.assertTrue(out.get("timeout"))

    def test_wait_timeout_after_streams_close(self):
        # Child closes its pipes (parent sees EOF and the read loop ends), then
        # keeps running so the post-loop wait(timeout=1) expires and it is killed.
        out = Engine._bounded_process([sys.executable, "-c",
            "import os, time; os.close(1); os.close(2); time.sleep(3)"])
        self.assertEqual(out["stdout"], "")


class BoundedNulPathsTests(unittest.TestCase):
    def test_missing_binary(self):
        out = Engine._bounded_nul_paths(["/no/such/binary-xyz"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["count"], 0)

    def test_counts_and_samples(self):
        out = Engine._bounded_nul_paths([sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b'a\\0b\\0c\\0')"], keep=2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["paths"], ["a", "b"])
        self.assertEqual(len(out["digest"]), 64)

    def test_input_truncation(self):
        out = Engine._bounded_nul_paths([sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b'x\\0'*100000)"],
            max_input=128, keep=3)
        self.assertTrue(out["truncated"])
        self.assertIn("scan limit", out["stderr"])

    def test_stderr_captured_and_carry_overflow(self):
        # A single record longer than 16 KiB with no NUL trips the carry guard.
        out = Engine._bounded_nul_paths([sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b'z'*40000)"], keep=3)
        self.assertTrue(out["truncated"])

    def test_timeout(self):
        out = Engine._bounded_nul_paths([sys.executable, "-c",
            "import time; time.sleep(5)"], timeout=0.3)
        self.assertTrue(out.get("timeout"))

    def test_wait_timeout_after_streams_close(self):
        out = Engine._bounded_nul_paths([sys.executable, "-c",
            "import os, time; os.close(1); os.close(2); time.sleep(3)"])
        self.assertEqual(out["count"], 0)

    def test_stderr_and_empty_records(self):
        out = Engine._bounded_nul_paths([sys.executable, "-c",
            "import sys; sys.stderr.write('warn'); "
            "sys.stdout.buffer.write(b'a\\0\\0b\\0')"], keep=5)
        self.assertTrue(out["ok"])
        # Consecutive NULs produce an empty record that is skipped.
        self.assertEqual(out["count"], 2)
        self.assertIn("warn", out["stderr"])


class ParseWorktreeListTests(unittest.TestCase):
    def test_parses_all_fields(self):
        raw = ("worktree /main\0HEAD abc\0branch refs/heads/main\0\0"
               "worktree /wt\0HEAD def\0detached\0locked reason here\0\0"
               "worktree /wt2\0prunable gitdir gone\0")
        entries = Engine._parse_worktree_list(raw)
        self.assertEqual(entries[0]["branch"], "refs/heads/main")
        self.assertTrue(entries[1]["detached"])
        self.assertTrue(entries[1]["locked"])
        self.assertIn("reason", entries[1]["lock_reason"])
        self.assertTrue(entries[2]["prunable"])

    def test_ignores_stray_records_before_worktree(self):
        raw = "HEAD orphan\0worktree /main\0HEAD abc\0"
        entries = Engine._parse_worktree_list(raw)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["head"], "abc")

    def test_back_to_back_worktree_entries_without_separator(self):
        # No empty separator between entries -> the "flush current" branch and
        # the trailing "flush last" branch both fire.
        raw = "worktree /a\0HEAD a1\0worktree /b\0HEAD b1"
        entries = Engine._parse_worktree_list(raw)
        self.assertEqual([e["path"] for e in entries], ["/a", "/b"])


class OwnedLockTests(unittest.TestCase):
    def test_owned_lock_detection(self):
        session = {"provider": "claude", "pid": 4242}
        reg = {"lock_reason": "claude session abc (pid 4242 start now)"}
        self.assertTrue(Engine._owned_claude_worktree_lock(session, reg))
        self.assertFalse(Engine._owned_claude_worktree_lock(
            {"provider": "codex", "pid": 4242}, reg))
        self.assertFalse(Engine._owned_claude_worktree_lock(
            {"provider": "claude", "pid": "bad"}, reg))
        self.assertFalse(Engine._owned_claude_worktree_lock(
            {"provider": "claude", "pid": 1}, {"lock_reason": "manual"}))


class PreviewTests(EngineFixture):
    def _make_repo_with_worktree(self):
        root = os.path.join(self.tmp.name, "wtrepo")
        os.makedirs(root)
        git("init", "-q", root)
        git("-C", root, "config", "user.email", "a@b.c")
        git("-C", root, "config", "user.name", "T")
        with open(os.path.join(root, "f.txt"), "w") as handle:
            handle.write("one\n")
        git("-C", root, "add", "f.txt")
        git("-C", root, "commit", "-qm", "first")
        wt = os.path.join(self.tmp.name, "wt-linked")
        git("-C", root, "worktree", "add", "-q", "-b", "feature/wt", wt)
        return root, wt

    def test_preview_not_git(self):
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "cwd": self.tmp.name})
        self.assertIn("not in a Git worktree", out["reason"])

    def test_preview_primary_worktree_refused(self):
        root = os.path.join(self.tmp.name, "primary")
        os.makedirs(root)
        git("init", "-q", root)
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "cwd": root})
        self.assertIn("primary worktree", out["reason"])

    def test_preview_clean_secondary_allows_remove(self):
        root, wt = self._make_repo_with_worktree()
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertTrue(out["secondary_worktree"])
        self.assertTrue(out["inspect_ok"])
        self.assertFalse(out["dirty"])
        self.assertTrue(out["remove_allowed"])
        self.assertIn("cleanup_ticket", out)

    def test_preview_dirty_forces_force_remove(self):
        root, wt = self._make_repo_with_worktree()
        with open(os.path.join(wt, "dirty.txt"), "w") as handle:
            handle.write("new\n")
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertTrue(out["dirty"])
        self.assertFalse(out["remove_allowed"])
        self.assertTrue(out["force_remove_allowed"])
        self.assertIn("would erase", out["reason"])

    def test_cleanup_flow_removes_clean_worktree(self):
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        # Not yet closed -> refused.
        pending = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(pending["ok"])
        self.assertIn("did not close", pending["error"])
        self.engine._mark_cleanup_ticket_closed(token, "s")
        done = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s"})
        self.assertTrue(done["ok"], done)
        self.assertTrue(done["removed"])
        self.assertFalse(os.path.exists(wt))

    def test_cleanup_expired_ticket(self):
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": "nope", "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertIn("expired", out["error"])

    def test_cleanup_session_mismatch(self):
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "other"})
        self.assertFalse(out["ok"])
        self.assertIn("does not match", out["error"])

    def test_cleanup_revision_change_preserves(self):
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        # Change the worktree so the revision no longer matches.
        with open(os.path.join(wt, "changed.txt"), "w") as handle:
            handle.write("x\n")
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["preserved"])

    def test_ticket_matches_helper(self):
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        self.assertTrue(self.engine._cleanup_ticket_matches(token, "s"))
        self.assertFalse(self.engine._cleanup_ticket_matches(token, "wrong"))
        self.assertFalse(self.engine._cleanup_ticket_matches("bad", "s"))

    def test_sessions_using_worktree(self):
        root, wt = self._make_repo_with_worktree()
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [
                {"session_id": "a", "provider": "codex", "cwd": wt, "title": "A"}]}
        users = self.engine._sessions_using_worktree(wt)
        self.assertEqual(users[0]["session_id"], "a")
        excluded = self.engine._sessions_using_worktree(wt, exclude=("a",))
        self.assertEqual(excluded, [])

    def test_mark_ticket_closed_ignores_empty(self):
        # No token -> early return, no crash.
        self.engine._mark_cleanup_ticket_closed("", "s")

    def test_preview_worktree_list_unavailable(self):
        root, wt = self._make_repo_with_worktree()
        from unittest import mock
        real = self.engine._bounded_process

        def fake(argv, **kwargs):
            if "list" in argv:
                return {"ok": False, "stdout": "", "stderr": "list failed",
                        "truncated": False}
            return real(argv, **kwargs)
        with mock.patch.object(self.engine, "_bounded_process", side_effect=fake):
            out = self.engine.close_worktree_preview(
                {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertFalse(out["inspect_ok"])
        self.assertIn("list failed", out["reason"])

    def test_preview_status_probe_unavailable(self):
        root, wt = self._make_repo_with_worktree()
        from unittest import mock
        real = self.engine._bounded_process

        def fake(argv, **kwargs):
            if "status" in argv:
                return {"ok": False, "stdout": "", "stderr": "status failed",
                        "truncated": False}
            return real(argv, **kwargs)
        with mock.patch.object(self.engine, "_bounded_process", side_effect=fake):
            out = self.engine.close_worktree_preview(
                {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertFalse(out["inspect_ok"])
        self.assertIn("status failed", out["reason"])

    def test_preview_locked_not_owned(self):
        root, wt = self._make_repo_with_worktree()
        git("-C", root, "worktree", "lock", "--reason", "manual hold", wt)
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertTrue(out["locked"])
        self.assertIn("locked by Git", out["reason"])

    def test_preview_owned_claude_lock_released_after_close(self):
        root, wt = self._make_repo_with_worktree()
        pid = os.getpid()
        git("-C", root, "worktree", "lock", "--reason",
            f"claude session abc (pid {pid} start now)", wt)
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "pid": pid, "cwd": wt})
        self.assertTrue(out["owned_lock"])
        self.assertIn("released after the session closes", out["reason"])

    def test_preview_shared_session_blocks_removal(self):
        root, wt = self._make_repo_with_worktree()
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [
                {"session_id": "other", "provider": "codex", "cwd": wt,
                 "title": "Other"}]}
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertTrue(out["shared_sessions"])
        self.assertFalse(out["remove_allowed"])
        self.assertIn("Another live Fleet session", out["reason"])

    def test_cleanup_claude_process_still_closing(self):
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "pid": os.getpid(),
             "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertIn("still closing", out["error"])

    def test_cleanup_force_removes_dirty_worktree(self):
        root, wt = self._make_repo_with_worktree()
        with open(os.path.join(wt, "dirty.txt"), "w") as handle:
            handle.write("x\n")
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s", "force": True})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["forced"])

    def test_cleanup_not_allowed_without_force_on_dirty(self):
        root, wt = self._make_repo_with_worktree()
        with open(os.path.join(wt, "dirty.txt"), "w") as handle:
            handle.write("x\n")
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["preserved"])

    def test_cleanup_releases_owned_lock_then_removes(self):
        root, wt = self._make_repo_with_worktree()
        dead_pid = 999_999
        git("-C", root, "worktree", "lock", "--reason",
            f"claude session abc (pid {dead_pid} start now)", wt)
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "pid": dead_pid, "cwd": wt})
        self.assertTrue(preview["owned_lock"])
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        out = self.engine.cleanup_closed_worktree(
            {"cleanup_ticket": token, "session_id": "s"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(wt))

    def test_cleanup_ps_exception_reports_still_closing(self):
        from unittest import mock
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "pid": os.getpid(),
             "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        with mock.patch("fleetdash.engine_worktree.subprocess.run",
                        side_effect=OSError("ps blew up")):
            out = self.engine.cleanup_closed_worktree(
                {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertIn("still closing", out["error"])

    def test_cleanup_owned_unlock_failure_preserves(self):
        from unittest import mock
        root, wt = self._make_repo_with_worktree()
        dead_pid = 999_998
        git("-C", root, "worktree", "lock", "--reason",
            f"claude session abc (pid {dead_pid} start now)", wt)
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "pid": dead_pid, "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        real = self.engine._bounded_process

        def fake(argv, **kwargs):
            if "unlock" in argv:
                return {"ok": False, "stdout": "", "stderr": "unlock failed",
                        "truncated": False}
            return real(argv, **kwargs)
        with mock.patch.object(self.engine, "_bounded_process", side_effect=fake):
            out = self.engine.cleanup_closed_worktree(
                {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["preserved"])

    def test_preview_prunable_registration_is_unsafe(self):
        from unittest import mock
        root, wt = self._make_repo_with_worktree()
        real = self.engine._bounded_process

        def fake(argv, **kwargs):
            if "list" in argv:
                stdout = (f"worktree {os.path.realpath(root)}\0HEAD a\0\0"
                          f"worktree {os.path.realpath(wt)}\0prunable gone\0")
                return {"ok": True, "stdout": stdout, "stderr": "",
                        "truncated": False}
            return real(argv, **kwargs)
        with mock.patch.object(self.engine, "_bounded_process", side_effect=fake):
            out = self.engine.close_worktree_preview(
                {"session_id": "s", "provider": "claude", "cwd": wt})
        self.assertFalse(out["inspect_ok"])
        self.assertIn("stale or unsafe", out["reason"])

    def test_preview_partial_stage_dedups_file(self):
        root, wt = self._make_repo_with_worktree()
        target = os.path.join(wt, "f.txt")
        with open(target, "a") as handle:
            handle.write("staged change\n")
        git("-C", wt, "add", "f.txt")
        with open(target, "a") as handle:
            handle.write("more unstaged\n")
        out = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "claude", "cwd": wt})
        # The file is both staged and unstaged; it must appear once in the list.
        paths = [item["path"] for item in out["dirty_files"]]
        self.assertEqual(paths.count("f.txt"), 1)

    def test_cleanup_removal_failure_is_reported(self):
        from unittest import mock
        root, wt = self._make_repo_with_worktree()
        preview = self.engine.close_worktree_preview(
            {"session_id": "s", "provider": "folder", "cwd": wt})
        token = preview["cleanup_ticket"]
        self.engine._mark_cleanup_ticket_closed(token, "s")
        real = self.engine._bounded_process

        def fake(argv, **kwargs):
            if "remove" in argv:
                return {"ok": False, "stdout": "", "stderr": "remove failed",
                        "truncated": False}
            return real(argv, **kwargs)
        with mock.patch.object(self.engine, "_bounded_process", side_effect=fake):
            out = self.engine.cleanup_closed_worktree(
                {"cleanup_ticket": token, "session_id": "s"})
        self.assertFalse(out["ok"])
        self.assertIn("remove failed", out["error"])


if __name__ == "__main__":
    unittest.main()
