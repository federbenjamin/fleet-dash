import os
import subprocess
import tempfile
import unittest
from unittest import mock

from repo_center import RepositoryOutcomeCenter, observed_test_outcome


class Result:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.status = ("# branch.oid abc123\0# branch.head feature/repo\0"
                       "# branch.upstream origin/feature/repo\0# branch.ab +2 -1\0"
                       "1 .M N... 100644 100644 100644 abc abc app.py\0? new file.txt\0")
        self.pr = None
        self.pr_error = None
        self.fail_status = None

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        if argv[0:4] == ["git", "-C", "/repo", "status"]:
            return self.fail_status or Result(stdout=self.status)
        if argv == ["git", "-C", "/repo", "remote"]:
            return Result(stdout="origin\n")
        if argv == ["git", "-C", "/repo", "remote", "get-url", "origin"]:
            return Result(stdout="git@github.com:federbenjamin/fleet-dash.git\n")
        if "symbolic-ref" in argv:
            return Result(stdout="origin/main\n")
        if "log" in argv:
            return Result(stdout="abc123\0abc123\0Implement repository center\01700000000\n")
        if argv[:3] == ["gh", "pr", "view"]:
            if self.pr_error:
                return Result(1, stderr=self.pr_error)
            if self.pr is None:
                return Result(1, stderr="no pull requests found for branch feature/repo")
            return Result(stdout=self.pr)
        if argv[:4] == ["git", "-C", "/repo", "add"]:
            return Result()
        if argv[:4] == ["git", "-C", "/repo", "commit"]:
            self.status = ("# branch.oid def456\0# branch.head feature/repo\0"
                           "# branch.upstream origin/feature/repo\0# branch.ab +3 -1\0")
            return Result(stdout="[feature/repo def456] Update files")
        if argv[:4] == ["git", "-C", "/repo", "push"]:
            self.status = ("# branch.oid abc123\0# branch.head feature/repo\0"
                           "# branch.upstream origin/feature/repo\0# branch.ab +0 -1\0")
            return Result(stderr="pushed")
        if argv[:3] in (["gh", "pr", "create"], ["gh", "pr", "ready"]):
            return Result(stdout="https://github.test/pull/7")
        raise AssertionError(f"unexpected command: {argv}")


class RepositoryOutcomeCenterTests(unittest.TestCase):
    def test_github_remote_parser_returns_only_valid_repository_identifiers(self):
        parse = RepositoryOutcomeCenter._github_slug
        self.assertEqual(parse("git@github.com:federbenjamin/fleet-dash.git"),
                         "federbenjamin/fleet-dash")
        self.assertEqual(parse("https://github.com/federbenjamin/fleet-dash.git"),
                         "federbenjamin/fleet-dash")
        self.assertEqual(parse("ssh://git@github.example.com/team/repo.git"),
                         "github.example.com/team/repo")
        self.assertIsNone(parse("https://user:secret@github.com/team/repo.git"))

    def test_observed_test_outcome_is_honest_about_running_failure_and_success(self):
        self.assertEqual(observed_test_outcome([
            {"role": "tool", "name": "Bash", "command": "python3 -m unittest", "ts": 1}
        ])["state"], "running")
        self.assertEqual(observed_test_outcome([
            {"role": "tool", "name": "commandExecution", "arg": "npm test",
             "result": "failed", "failed": True, "ts": 2}
        ])["state"], "failed")
        outcome = observed_test_outcome([
            {"role": "tool", "name": "Bash", "arg": "Read files", "result": "ok"},
            {"role": "tool", "name": "Bash", "command": "npx playwright test",
             "result": "56 passed", "failed": False, "completed_at": 3}
        ], "codex:one", "codex")
        self.assertEqual(outcome["state"], "passed")
        self.assertEqual(outcome["session_id"], "codex:one")

    def test_snapshot_parses_branch_changes_ahead_behind_and_no_pr(self):
        runner = FakeRunner()
        center = RepositoryOutcomeCenter(runner=runner, clock=lambda: 100)
        snapshot = center.snapshot("/repo", "/repo", force=True)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["branch"], "feature/repo")
        self.assertEqual((snapshot["ahead"], snapshot["behind"]), (2, 1))
        self.assertEqual([item["path"] for item in snapshot["files"]],
                         ["app.py", "new file.txt"])
        self.assertTrue(snapshot["actions"]["commit"]["enabled"])
        self.assertTrue(snapshot["actions"]["push"]["enabled"])
        self.assertTrue(snapshot["actions"]["pr_create_draft"]["enabled"])
        self.assertEqual(snapshot["pr"]["state"], "none")

    def test_snapshot_cache_avoids_repeating_bounded_probes(self):
        runner = FakeRunner()
        now = [100]
        center = RepositoryOutcomeCenter(runner=runner, clock=lambda: now[0], cache_seconds=8)
        one = center.snapshot("/repo", "/repo")
        count = len(runner.calls)
        two = center.snapshot("/repo", "/repo", test_outcome={"state": "passed"})
        self.assertEqual(len(runner.calls), count)
        self.assertTrue(two["cached"])
        self.assertEqual(two["tests"]["state"], "passed")
        now[0] += 9
        center.snapshot("/repo", "/repo")
        self.assertGreater(len(runner.calls), count)
        self.assertEqual(one["revision"], two["revision"])

    def test_bulk_snapshot_skips_github_until_repository_details_open(self):
        runner = FakeRunner()
        center = RepositoryOutcomeCenter(runner=runner)
        bulk = center.snapshot("/repo", "/repo", force=True, include_github=False)
        self.assertEqual(bulk["pr"]["state"], "not_observed")
        self.assertFalse(any(call[0][:3] == ["gh", "pr", "view"] for call in runner.calls))
        center.snapshot("/repo", "/repo", force=True, include_github=True)
        self.assertTrue(any(call[0][:3] == ["gh", "pr", "view"] for call in runner.calls))

    def test_probe_failures_are_scoped_stale_objects(self):
        runner = FakeRunner()
        runner.fail_status = Result(128, stderr="fatal: repository unavailable")
        snapshot = RepositoryOutcomeCenter(runner=runner).snapshot("/repo", "/repo", force=True)
        self.assertFalse(snapshot["ok"])
        self.assertEqual(snapshot["state"], "stale")
        self.assertIn("repository unavailable", snapshot["error"])

    def test_github_missing_auth_network_and_malformed_payloads_degrade_visibly(self):
        with mock.patch("repo_center.shutil.which", return_value=None):
            with mock.patch("repo_center.os.path.isfile", return_value=False):
                center = RepositoryOutcomeCenter()
            missing = center._github_pr("branch", "owner/repo")
        self.assertEqual(missing["state"], "unavailable")
        runner = FakeRunner()
        runner.pr_error = "not logged into any GitHub hosts"
        auth = RepositoryOutcomeCenter(runner=runner).snapshot("/repo", "/repo", force=True)
        self.assertEqual(auth["pr"]["state"], "stale")
        self.assertIn("not logged", auth["pr"]["error"])
        runner.pr_error = "network timed out"
        network = RepositoryOutcomeCenter(runner=runner).snapshot("/repo", "/repo", force=True)
        self.assertEqual(network["pr"]["state"], "stale")
        runner.pr_error = None
        runner.pr = "not json"
        malformed = RepositoryOutcomeCenter(runner=runner).snapshot("/repo", "/repo", force=True)
        self.assertIn("malformed", malformed["pr"]["error"])

    def test_revision_and_file_validation_prevent_stale_or_injected_commit(self):
        runner = FakeRunner()
        center = RepositoryOutcomeCenter(runner=runner)
        snapshot = center.snapshot("/repo", "/repo", force=True)
        stale = center.perform("git_commit", snapshot,
                               {"revision": "old", "paths": ["app.py"], "message": "Update"})
        self.assertTrue(stale["stale"])
        bad = center.perform("git_commit", snapshot,
                             {"revision": snapshot["revision"], "paths": ["--all"],
                              "message": "Update"})
        self.assertIn("stale or invalid", bad["error"])
        self.assertFalse(any("commit" in call[0] for call in runner.calls))

    def test_commit_uses_only_argv_and_refreshes_snapshot(self):
        runner = FakeRunner()
        center = RepositoryOutcomeCenter(runner=runner)
        snapshot = center.snapshot("/repo", "/repo", force=True)
        result = center.perform("git_commit", snapshot,
                                {"revision": snapshot["revision"],
                                 "paths": ["app.py", "new file.txt"],
                                 "message": "Update repository UI"})
        self.assertTrue(result["ok"])
        add = next(call[0] for call in runner.calls if call[0][3] == "add")
        commit = next(call[0] for call in runner.calls if call[0][3] == "commit")
        self.assertEqual(add[-3:], ["--", "app.py", "new file.txt"])
        self.assertEqual(commit[-2:], ["-m", "Update repository UI"])

    def test_push_draft_pr_and_ready_use_fixed_non_merge_argv(self):
        runner = FakeRunner()
        center = RepositoryOutcomeCenter(runner=runner)
        push_preview = center.snapshot("/repo", "/repo", force=True)
        pushed = center.perform("git_push", push_preview,
                                {"revision": push_preview["revision"]})
        self.assertTrue(pushed["ok"])
        push = next(call[0] for call in runner.calls if call[0][3] == "push")
        self.assertEqual(push, ["git", "-C", "/repo", "push", "origin",
                                "HEAD:refs/heads/feature/repo"])

        runner.status = ("# branch.oid abc123\0# branch.head feature/repo\0"
                         "# branch.upstream origin/feature/repo\0# branch.ab +0 -0\0")
        draft_preview = center.snapshot("/repo", "/repo", force=True)
        draft = center.perform("pr_create_draft", draft_preview, {
            "revision": draft_preview["revision"], "title": "Repository center",
            "body": "Bounded actions", "base": "main"})
        self.assertTrue(draft["ok"])
        create = next(call[0] for call in runner.calls if call[0][:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create)
        self.assertEqual(create[create.index("--repo") + 1],
                         "federbenjamin/fleet-dash")
        self.assertNotIn("merge", create)

        runner.pr = ('{"number":7,"state":"OPEN","isDraft":true,'
                     '"url":"https://github.test/pull/7","reviewDecision":"",'
                     '"mergeStateStatus":"CLEAN","mergeable":"MERGEABLE",'
                     '"statusCheckRollup":[],"baseRefName":"main",'
                     '"headRefName":"feature/repo","title":"Repository center",'
                     '"headRefOid":"abc123"}')
        ready_preview = center.snapshot("/repo", "/repo", force=True)
        ready = center.perform("pr_mark_ready", ready_preview,
                               {"revision": ready_preview["revision"], "number": 7})
        self.assertTrue(ready["ok"])
        ready_argv = next(call[0] for call in runner.calls
                          if call[0][:3] == ["gh", "pr", "ready"])
        self.assertEqual(ready_argv, ["gh", "pr", "ready", "7", "--repo",
                                      "federbenjamin/fleet-dash"])
        self.assertFalse(any(call[:2] == ["gh", "pr"] and len(call) > 2 and
                             call[2] == "merge" for call, _ in runner.calls))

    def test_real_git_snapshot_handles_clean_dirty_and_detached_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "config", "user.email", "fleet@example.test"],
                           check=True)
            subprocess.run(["git", "-C", tmp, "config", "user.name", "Fleet Test"], check=True)
            with open(os.path.join(tmp, "one.txt"), "w") as handle:
                handle.write("one\n")
            subprocess.run(["git", "-C", tmp, "add", "one.txt"], check=True)
            subprocess.run(["git", "-C", tmp, "commit", "-qm", "first"], check=True)
            center = RepositoryOutcomeCenter()
            clean = center.snapshot(tmp, tmp, force=True)
            self.assertFalse(clean["dirty"])
            with open(os.path.join(tmp, "one.txt"), "a") as handle:
                handle.write("two\n")
            dirty = center.snapshot(tmp, tmp, force=True)
            self.assertTrue(dirty["dirty"])
            subprocess.run(["git", "-C", tmp, "add", "one.txt"], check=True)
            subprocess.run(["git", "-C", tmp, "commit", "-qm", "second"], check=True)
            with tempfile.TemporaryDirectory() as remote:
                subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
                subprocess.run(["git", "-C", tmp, "remote", "add", "origin", remote], check=True)
                base = subprocess.run(["git", "-C", tmp, "branch", "--show-current"],
                                      check=True, capture_output=True, text=True).stdout.strip()
                subprocess.run(["git", "-C", tmp, "push", "-qu", "origin", base], check=True)
                subprocess.run(["git", "--git-dir", remote, "symbolic-ref", "HEAD",
                                f"refs/heads/{base}"], check=True)
                subprocess.run(["git", "-C", tmp, "remote", "set-head", "origin", "-a"],
                               check=True, capture_output=True)
                subprocess.run(["git", "-C", tmp, "checkout", "-qb", "feature/no-upstream"],
                               check=True)
                with open(os.path.join(tmp, "one.txt"), "a") as handle:
                    handle.write("three\n")
                subprocess.run(["git", "-C", tmp, "commit", "-qam", "third"], check=True)
                unpushed = center.snapshot(tmp, tmp, force=True, include_github=False)
                self.assertEqual(unpushed["ahead"], 1)
                self.assertTrue(unpushed["actions"]["push"]["enabled"])
                self.assertTrue(unpushed["actions"]["push"]["set_upstream"])
            subprocess.run(["git", "-C", tmp, "checkout", "--detach", "-q"], check=True)
            detached = center.snapshot(tmp, tmp, force=True)
            self.assertTrue(detached["detached"])
            self.assertFalse(detached["actions"]["push"]["enabled"])


if __name__ == "__main__":
    unittest.main()
