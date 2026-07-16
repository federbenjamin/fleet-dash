#!/usr/bin/env python3
"""Opt-in real Git smoke for Fleet's repository outcome center.

This creates only temporary local repositories. It never touches the checked-out
Fleet branch, GitHub, or any existing remote.
"""
import json
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from repo_center import RepositoryOutcomeCenter


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True)


def main():
    scratch = os.environ.get("TMPDIR") or None
    with tempfile.TemporaryDirectory(prefix="fleet-repo-smoke-", dir=scratch) as temp:
        repo = os.path.join(temp, "repo")
        remote = os.path.join(temp, "remote.git")
        run(["git", "init", "-q", "-b", "main", repo])
        run(["git", "init", "-q", "--bare", remote])
        run(["git", "-C", repo, "config", "user.email", "fleet-smoke@example.test"])
        run(["git", "-C", repo, "config", "user.name", "Fleet Smoke"])
        with open(os.path.join(repo, "README.md"), "w") as handle:
            handle.write("# Fleet repository smoke\n")
        run(["git", "-C", repo, "add", "README.md"])
        run(["git", "-C", repo, "commit", "-qm", "Initial commit"])
        run(["git", "-C", repo, "remote", "add", "origin", remote])
        run(["git", "-C", repo, "push", "-qu", "origin", "main"])
        run(["git", "--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main"])
        run(["git", "-C", repo, "remote", "set-head", "origin", "-a"])

        with open(os.path.join(repo, "README.md"), "a") as handle:
            handle.write("\nObserved and confirmed.\n")
        center = RepositoryOutcomeCenter(cache_seconds=1)
        dirty = center.snapshot(repo, repo, force=True, include_github=False)
        assert dirty["dirty"] and dirty["actions"]["commit"]["enabled"], dirty
        committed = center.perform("git_commit", dirty, {
            "revision": dirty["revision"], "paths": ["README.md"],
            "message": "Exercise confirmed repository commit"})
        assert committed["ok"], committed
        after_commit = center.snapshot(repo, repo, force=True, include_github=False)
        assert after_commit["ahead"] == 1 and after_commit["actions"]["push"]["enabled"], after_commit
        pushed = center.perform("git_push", after_commit,
                                {"revision": after_commit["revision"]})
        assert pushed["ok"], pushed
        final = center.snapshot(repo, repo, force=True, include_github=False)
        assert not final["dirty"] and final["ahead"] == 0, final
        print(json.dumps({"ok": True, "commit": committed["summary"],
                          "push": pushed["summary"], "branch": final["branch"],
                          "ahead": final["ahead"], "github_touched": False}))


if __name__ == "__main__":
    main()
