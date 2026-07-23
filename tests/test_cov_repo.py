"""Coverage for fleetdash.repo_center edge branches and perform() validation."""
import subprocess
import unittest
from unittest import mock

from fleetdash import repo_center
from fleetdash.repo_center import RepositoryOutcomeCenter, _epoch, observed_test_outcome


class Result:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class ConfigurableRunner:
    """A git/gh runner whose per-command outputs are overridable."""

    def __init__(self):
        self.calls = []
        self.status = ("# branch.oid abc123\0# branch.head feature/x\0"
                       "# branch.upstream origin/feature/x\0# branch.ab +2 -1\0"
                       "1 .M N... 100644 100644 100644 a a app.py\0? new.txt\0")
        self.pr_json = None
        self.commit_result = Result(stdout="[feature/x def] done")
        self.push_result = Result(stderr="pushed")
        self.gh_create_result = Result(stdout="https://github.test/pull/7")
        self.gh_ready_result = Result(stdout="ready")
        self.stage_result = Result()

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[0:4] == ["git", "-C", "/repo", "status"]:
            return Result(stdout=self.status)
        if argv == ["git", "-C", "/repo", "remote"]:
            return Result(stdout="origin\n")
        if argv == ["git", "-C", "/repo", "remote", "get-url", "origin"]:
            return Result(stdout="git@github.com:owner/repo.git\n")
        if "symbolic-ref" in argv:
            return Result(stdout="origin/main\n")
        if "log" in argv:
            return Result(stdout="H\x00h\x00Subject\x001700000000\n")
        if argv[:3] == ["gh", "pr", "view"]:
            if self.pr_json is None:
                return Result(1, stderr="no pull requests found for branch")
            return Result(stdout=self.pr_json)
        if argv[:4] == ["git", "-C", "/repo", "add"]:
            return self.stage_result
        if argv[:4] == ["git", "-C", "/repo", "commit"]:
            return self.commit_result
        if argv[:4] == ["git", "-C", "/repo", "push"]:
            return self.push_result
        if argv[:3] == ["gh", "pr", "create"]:
            return self.gh_create_result
        if argv[:3] == ["gh", "pr", "ready"]:
            return self.gh_ready_result
        return Result()


def center(runner=None):
    return RepositoryOutcomeCenter(runner=runner or ConfigurableRunner(),
                                   clock=lambda: 1000)


class SmallHelperTests(unittest.TestCase):
    def test_epoch_variants(self):
        self.assertEqual(_epoch(5), 5.0)
        self.assertEqual(_epoch(None), 0.0)
        self.assertEqual(_epoch("not-a-date"), 0.0)
        self.assertGreater(_epoch("2026-07-15T00:00:00Z"), 0)

    def test_observed_test_outcome_unknown_state(self):
        outcome = observed_test_outcome([
            {"role": "tool", "name": "Bash", "command": "pytest",
             "result": "weird", "status": "someunknownstatus"}])
        self.assertEqual(outcome["state"], "unknown")

    def test_observed_test_outcome_state_variants(self):
        # Non-tool rows are skipped.
        failed = observed_test_outcome([
            {"role": "assistant", "text": "ignored"},
            {"role": "tool", "command": "pytest", "failed": True}])
        self.assertEqual(failed["state"], "failed")
        passed = observed_test_outcome([
            {"role": "tool", "command": "pytest", "failed": False,
             "result": "ok"}])
        self.assertEqual(passed["state"], "passed")
        running = observed_test_outcome([
            {"role": "tool", "command": "pytest", "status": "running"}])
        self.assertEqual(running["state"], "running")

    def test_observed_test_outcome_none_without_test_command(self):
        # A trailing non-tool row exercises the role-skip branch.
        self.assertIsNone(observed_test_outcome([
            {"role": "tool", "command": "ls -la"},
            {"role": "assistant", "text": "done"}]))

    def test_github_slug_variants(self):
        parse = RepositoryOutcomeCenter._github_slug
        self.assertEqual(parse("git@github.com:o/r.git"), "o/r")
        self.assertEqual(parse("ssh://git@host.example/team/repo.git"),
                         "host.example/team/repo")
        self.assertIsNone(parse("not a url"))
        self.assertIsNone(parse("https://user:pw@github.com/o/r.git"))

    def test_parse_status_detached(self):
        parsed = RepositoryOutcomeCenter._parse_status("# branch.head (detached)\0")
        self.assertTrue(parsed["detached"])
        self.assertIsNone(parsed["branch"])

    def test_default_runner_executes_subprocess(self):
        out = RepositoryOutcomeCenter(runner=None)._run(["true"])
        self.assertTrue(out["ok"])

    def test_resolve_gh_returns_path_when_present(self):
        with mock.patch.object(repo_center.shutil, "which",
                               return_value="/usr/bin/gh"):
            self.assertEqual(RepositoryOutcomeCenter._resolve_gh(), "/usr/bin/gh")

    def test_github_pr_guards(self):
        c = center()
        self.assertEqual(c._github_pr(None, "o/r")["state"], "unavailable")
        self.assertEqual(c._github_pr("b", None)["state"], "unavailable")
        c.gh = None
        self.assertEqual(c._github_pr("b", "o/r")["state"], "unavailable")

    def test_github_pr_stale_and_malformed(self):
        runner = ConfigurableRunner()
        c = center(runner)

        def stale(argv, timeout):
            if argv[:3] == ["gh", "pr", "view"]:
                return Result(1, stderr="server error 500")
            return runner(argv, timeout)
        c.runner = stale
        self.assertEqual(c._github_pr("b", "o/r")["state"], "stale")

        def malformed(argv, timeout):
            if argv[:3] == ["gh", "pr", "view"]:
                return Result(stdout="not json")
            return runner(argv, timeout)
        c.runner = malformed
        self.assertIn("malformed", c._github_pr("b", "o/r")["error"])

    def test_pr_summary_variants(self):
        self.assertEqual(RepositoryOutcomeCenter._pr_summary({"state": "none"}), "none")
        self.assertEqual(RepositoryOutcomeCenter._pr_summary({"state": "stale"}), "stale")
        self.assertEqual(RepositoryOutcomeCenter._pr_summary(
            {"state": "unavailable"}), "unavailable")
        self.assertEqual(RepositoryOutcomeCenter._pr_summary(
            {"state": "ok", "number": 7, "is_draft": True}), "#7 draft")
        self.assertEqual(RepositoryOutcomeCenter._pr_summary(
            {"state": "ok", "number": 8, "review_decision": "CHANGES_REQUESTED"}),
            "#8 changes requested")

    def test_github_url_invalid(self):
        self.assertIsNone(RepositoryOutcomeCenter._github_url("bad host/owner/repo"))
        self.assertIsNone(RepositoryOutcomeCenter._github_url("one/two/three/four"))

    def test_status_path_rename_and_unmerged(self):
        two = ("2 R. N... 100644 100644 100644 a a R100 dest\0src")
        self.assertEqual(RepositoryOutcomeCenter._status_path(
            two.split("\0")[0]), "dest")
        unmerged = "u UU N... 1 1 1 1 a b c conflict.txt"
        self.assertEqual(RepositoryOutcomeCenter._status_path(unmerged),
                         "conflict.txt")
        self.assertEqual(RepositoryOutcomeCenter._status_path("1 .M short"), "")

    def test_parse_status_handles_rename_source_skip(self):
        raw = ("# branch.head main\0"
               "2 R. N... 100644 100644 100644 a a R100 dest\0src\0"
               "1 .M short-record\0"
               "? untracked.txt\0")
        parsed = RepositoryOutcomeCenter._parse_status(raw)
        paths = [item["path"] for item in parsed["files"]]
        self.assertIn("dest", paths)
        self.assertIn("untracked.txt", paths)
        # The short "1 " record with no path is skipped.
        self.assertNotIn("", paths)


class ResolveGhTests(unittest.TestCase):
    def test_resolve_gh_uses_fallback_candidate(self):
        with mock.patch.object(repo_center.shutil, "which", return_value=None), \
             mock.patch.object(repo_center.os.path, "isfile",
                               side_effect=lambda p: p == "/opt/homebrew/bin/gh"), \
             mock.patch.object(repo_center.os, "access", return_value=True):
            gh = RepositoryOutcomeCenter._resolve_gh()
        self.assertEqual(gh, "/opt/homebrew/bin/gh")

    def test_resolve_gh_returns_none_when_absent(self):
        with mock.patch.object(repo_center.shutil, "which", return_value=None), \
             mock.patch.object(repo_center.os.path, "isfile", return_value=False):
            self.assertIsNone(RepositoryOutcomeCenter._resolve_gh())


class RunExceptionTests(unittest.TestCase):
    def test_run_maps_timeout_missing_and_generic(self):
        def timeout(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)
        out = RepositoryOutcomeCenter(runner=timeout)._run(["git"])
        self.assertTrue(out.get("timeout"))

        def missing(argv, timeout):
            raise FileNotFoundError()
        out = RepositoryOutcomeCenter(runner=missing)._run(["git"])
        self.assertTrue(out.get("missing"))

        def generic(argv, timeout):
            raise RuntimeError("boom")
        out = RepositoryOutcomeCenter(runner=generic)._run(["git"])
        self.assertIn("boom", out["stderr"])


class GithubPrChecksTests(unittest.TestCase):
    def test_pr_checks_are_summarized(self):
        runner = ConfigurableRunner()
        runner.pr_json = (
            '{"number":7,"state":"OPEN","isDraft":false,'
            '"url":"u","reviewDecision":"APPROVED","mergeStateStatus":"CLEAN",'
            '"mergeable":"MERGEABLE","statusCheckRollup":['
            '{"conclusion":"SUCCESS"},{"conclusion":"FAILURE"},{"status":"PENDING"}],'
            '"baseRefName":"main","headRefName":"feature/x","title":"t",'
            '"headRefOid":"abc123"}')
        snap = center(runner).snapshot("/repo", "/repo", force=True,
                                       include_github=True)
        self.assertEqual(snap["pr"]["state"], "ok")
        self.assertEqual(snap["pr"]["checks"]["failed"], 1)
        self.assertEqual(snap["pr"]["checks"]["pending"], 1)
        self.assertEqual(snap["pr"]["checks"]["passed"], 1)


class SnapshotCacheTests(unittest.TestCase):
    def test_non_github_snapshot_reuses_full_cache(self):
        runner = ConfigurableRunner()
        c = center(runner)
        c.snapshot("/repo", "/repo", force=True, include_github=True)
        count = len(runner.calls)
        out = c.snapshot("/repo", "/repo", include_github=False,
                         test_outcome={"state": "passed"})
        self.assertTrue(out["cached"])
        self.assertEqual(out["tests"]["state"], "passed")
        self.assertEqual(len(runner.calls), count)

    def test_cache_eviction_beyond_limit(self):
        runner = ConfigurableRunner()
        c = RepositoryOutcomeCenter(runner=runner, clock=lambda: 1000)
        c.cache = {("old", "old", True): (2000, {"ok": True})
                   for _ in range(1)}
        # Fill beyond 300 to force the eviction path.
        for i in range(305):
            c.cache[(f"r{i}", f"r{i}", True)] = (2000, {"ok": True})
        c.snapshot("/repo", "/repo", force=True, include_github=True)
        self.assertLessEqual(len(c.cache), 1)


class PerformValidationTests(unittest.TestCase):
    def _preview(self, runner):
        return center(runner).snapshot("/repo", "/repo", force=True,
                                       include_github=True)

    def test_perform_rejects_unavailable_preview(self):
        c = center()
        self.assertIn("unavailable", c.perform("git_commit", None, {})["error"])
        self.assertEqual(
            c.perform("git_commit", {"ok": False, "error": "x"}, {})["error"], "x")

    def test_perform_revision_mismatch_is_stale(self):
        runner = ConfigurableRunner()
        c = center(runner)
        preview = self._preview(runner)
        out = c.perform("git_commit", preview,
            {"revision": "stale-rev", "paths": ["app.py"], "message": "m"})
        self.assertTrue(out["stale"])
        self.assertIn("snapshot", out)

    def test_snapshot_plain_cache_hit(self):
        runner = ConfigurableRunner()
        c = center(runner)
        c.snapshot("/repo", "/repo", include_github=True)
        count = len(runner.calls)
        out = c.snapshot("/repo", "/repo", include_github=True,
                         test_outcome={"state": "passed"})
        self.assertTrue(out["cached"])
        self.assertEqual(len(runner.calls), count)

    def test_perform_returns_current_when_refresh_fails(self):
        runner = ConfigurableRunner()
        c = center(runner)
        preview = self._preview(runner)
        # Make the forced refresh status fail.
        runner.status = None

        def failing(argv, timeout):
            if argv[0:4] == ["git", "-C", "/repo", "status"]:
                return Result(128, stderr="fatal")
            return runner.__class__.__call__(runner, argv, timeout)
        c.runner = failing
        out = c.perform("git_commit", preview, {"revision": preview["revision"]})
        self.assertFalse(out["ok"])

    def test_commit_validation_paths(self):
        runner = ConfigurableRunner()
        c = center(runner)
        preview = self._preview(runner)
        rev = preview["revision"]
        self.assertIn("1–1,000", c.perform("git_commit", preview,
            {"revision": rev, "paths": ["app.py"], "message": ""})["error"])
        self.assertIn("1–500", c.perform("git_commit", preview,
            {"revision": rev, "paths": [], "message": "m"})["error"])
        self.assertIn("stale or invalid", c.perform("git_commit", preview,
            {"revision": rev, "paths": ["nope.py"], "message": "m"})["error"])

    def test_commit_stage_failure_returns_snapshot(self):
        runner = ConfigurableRunner()
        runner.stage_result = Result(1, stderr="cannot stage")
        c = center(runner)
        preview = self._preview(runner)
        out = c.perform("git_commit", preview,
            {"revision": preview["revision"], "paths": ["app.py"], "message": "m"})
        self.assertFalse(out["ok"])
        self.assertIn("cannot stage", out["error"])

    def test_action_disabled_reason(self):
        runner = ConfigurableRunner()
        # No changed files -> commit action disabled.
        runner.status = ("# branch.oid abc\0# branch.head feature/x\0"
                         "# branch.upstream origin/feature/x\0# branch.ab +0 -0\0")
        c = center(runner)
        preview = self._preview(runner)
        out = c.perform("git_commit", preview,
            {"revision": preview["revision"], "paths": ["app.py"], "message": "m"})
        self.assertFalse(out["ok"])
        self.assertIn("No changed files", out["error"])

    def test_push_uses_set_upstream(self):
        runner = ConfigurableRunner()
        # No upstream + ahead>0 => set_upstream true.
        runner.status = ("# branch.oid abc\0# branch.head feature/x\0"
                         "# branch.ab +0 -0\0")
        # rev-list makes ahead 2.
        original = runner.__call__

        def with_revlist(argv, timeout):
            if "rev-list" in argv and argv[-1].endswith("..HEAD"):
                return Result(stdout="2\n")
            if "rev-list" in argv:
                return Result(stdout="0\n")
            return original(argv, timeout)
        runner.__call__ = with_revlist
        c = RepositoryOutcomeCenter(runner=with_revlist, clock=lambda: 1000)
        preview = c.snapshot("/repo", "/repo", force=True, include_github=True)
        out = c.perform("git_push", preview, {"revision": preview["revision"]})
        self.assertTrue(out["ok"])
        push = next(a for a in runner.calls if a[:4] == ["git", "-C", "/repo", "push"])
        self.assertIn("--set-upstream", push)

    def test_pr_create_validation(self):
        runner = ConfigurableRunner()
        runner.status = ("# branch.oid abc\0# branch.head feature/x\0"
                         "# branch.upstream origin/feature/x\0# branch.ab +0 -0\0")
        c = center(runner)
        preview = self._preview(runner)
        rev = preview["revision"]
        self.assertIn("title", c.perform("pr_create_draft", preview,
            {"revision": rev, "title": "", "body": "b", "base": "main"})["error"])
        self.assertIn("too long", c.perform("pr_create_draft", preview,
            {"revision": rev, "title": "t", "body": "x" * 20001, "base": "main"})["error"])
        self.assertIn("Invalid base", c.perform("pr_create_draft", preview,
            {"revision": rev, "title": "t", "body": "b", "base": "-bad"})["error"])

    def test_pr_mark_ready_number_validation(self):
        runner = ConfigurableRunner()
        runner.pr_json = (
            '{"number":7,"state":"OPEN","isDraft":true,"url":"u",'
            '"reviewDecision":"","mergeStateStatus":"CLEAN","mergeable":"MERGEABLE",'
            '"statusCheckRollup":[],"baseRefName":"main","headRefName":"feature/x",'
            '"title":"t","headRefOid":"abc123"}')
        c = center(runner)
        preview = c.snapshot("/repo", "/repo", force=True, include_github=True)
        rev = preview["revision"]
        self.assertIn("changed since preview", c.perform("pr_mark_ready", preview,
            {"revision": rev, "number": "notanint"})["error"])
        # A valid ready action reaches the gh invocation.
        ready = c.perform("pr_mark_ready", preview, {"revision": rev, "number": 7})
        self.assertTrue(ready["ok"])

    def test_perform_result_failure_returns_snapshot(self):
        runner = ConfigurableRunner()
        runner.commit_result = Result(1, stderr="commit rejected")
        c = center(runner)
        preview = self._preview(runner)
        out = c.perform("git_commit", preview,
            {"revision": preview["revision"], "paths": ["app.py"], "message": "m"})
        self.assertFalse(out["ok"])
        self.assertIn("commit rejected", out["error"])


if __name__ == "__main__":
    unittest.main()
