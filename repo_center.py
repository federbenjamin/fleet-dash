#!/usr/bin/env python3
"""Bounded Git/GitHub observation and confirmed repository actions.

Every subprocess call is an argv array assembled from fixed commands.  Client
input may select an already-observed repository, worktree, changed path, branch,
or pull-request number, but it never becomes shell syntax.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time


TEST_COMMAND = re.compile(
    r"(?i)(?:^|[;&|]\s*|\b)(?:"
    r"python(?:3)?\s+-m\s+(?:pytest|unittest)|pytest|unittest|"
    r"npx\s+playwright\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:test|run\s+(?:test|build|lint|typecheck))|"
    r"cargo\s+(?:test|check|clippy)|go\s+test|swift\s+test|"
    r"xcodebuild|gradle\w*\s+(?:test|check|build)|make\s+(?:test|check|build)"
    r")(?:\s|$)")


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def observed_test_outcome(messages, session_id=None, provider=None):
    """Return the newest visible test/build command without inventing success."""
    for item in reversed(list(messages or [])):
        if item.get("role") != "tool":
            continue
        command = str(item.get("command") or item.get("arg") or "").strip()
        if not command or not TEST_COMMAND.search(command):
            continue
        status = str(item.get("status") or "").lower()
        failed = item.get("failed")
        exit_code = item.get("exit_code")
        has_result = "result" in item and item.get("result") not in (None, "")
        if failed is True or (isinstance(exit_code, int) and exit_code != 0) \
                or status in ("failed", "error", "interrupted", "cancelled", "canceled"):
            state = "failed"
        elif failed is False or exit_code == 0 or status in ("completed", "succeeded", "success"):
            state = "passed"
        elif status in ("running", "inprogress", "in_progress", "started") or not has_result:
            state = "running"
        else:
            state = "unknown"
        return {"state": state, "command": command[:500],
                "at": _epoch(item.get("completed_at") or item.get("ts")),
                "source": "transcript", "session_id": session_id,
                "provider": provider, "result": str(item.get("result") or "")[:300]}
    return None


class RepositoryOutcomeCenter:
    """Cache repository probes and execute revision-checked mutations."""

    def __init__(self, runner=None, clock=None, cache_seconds=8):
        self.runner = runner or self._default_runner
        self.gh = "gh" if runner else self._resolve_gh()
        self.clock = clock or time.time
        self.cache_seconds = max(1, float(cache_seconds))
        self.cache = {}
        self.lock = threading.Lock()

    @staticmethod
    def _default_runner(argv, timeout):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _resolve_gh():
        found = shutil.which("gh")
        if found:
            return found
        for candidate in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh",
                          os.path.expanduser("~/.local/bin/gh")):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _run(self, argv, timeout=5):
        try:
            result = self.runner(list(argv), timeout)
            return {"ok": result.returncode == 0, "code": result.returncode,
                    "stdout": str(result.stdout or ""), "stderr": str(result.stderr or "")}
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": None, "stdout": "", "stderr": "timed out",
                    "timeout": True}
        except FileNotFoundError:
            return {"ok": False, "code": None, "stdout": "", "stderr": "not installed",
                    "missing": True}
        except Exception as exc:
            return {"ok": False, "code": None, "stdout": "", "stderr": str(exc)}

    @staticmethod
    def _status_path(record):
        if record.startswith(("? ", "! ")):
            return record[2:]
        fields = record.split(" ")
        if record.startswith("1 ") and len(fields) >= 9:
            return " ".join(fields[8:])
        if record.startswith("2 ") and len(fields) >= 10:
            return " ".join(fields[9:])
        if record.startswith("u ") and len(fields) >= 11:
            return " ".join(fields[10:])
        return ""

    @classmethod
    def _parse_status(cls, raw):
        branch = None
        upstream = None
        oid = None
        ahead = behind = 0
        detached = False
        files = []
        records = raw.split("\0")
        skip_rename_source = False
        for record in records:
            if not record:
                continue
            if skip_rename_source:
                skip_rename_source = False
                continue
            if record.startswith("# branch.head "):
                branch = record.removeprefix("# branch.head ").strip()
                detached = branch == "(detached)"
                if detached:
                    branch = None
            elif record.startswith("# branch.oid "):
                oid = record.removeprefix("# branch.oid ").strip()
            elif record.startswith("# branch.upstream "):
                upstream = record.removeprefix("# branch.upstream ").strip()
            elif record.startswith("# branch.ab "):
                match = re.search(r"\+(\d+)\s+-(\d+)", record)
                if match:
                    ahead, behind = int(match.group(1)), int(match.group(2))
            elif record.startswith(("1 ", "2 ", "u ", "? ")):
                path = cls._status_path(record)
                if not path:
                    continue
                if record.startswith("? "):
                    code, staged, unstaged, conflict = "??", False, True, False
                else:
                    parts = record.split(" ", 2)
                    code = parts[1] if len(parts) > 1 else "??"
                    staged = code[:1] not in (".", " ", "?")
                    unstaged = code[1:2] not in (".", " ")
                    conflict = record.startswith("u ") or "U" in code
                files.append({"path": path, "status": code, "staged": staged,
                              "unstaged": unstaged, "untracked": record.startswith("? "),
                              "conflict": conflict})
                skip_rename_source = record.startswith("2 ")
        return {"branch": branch, "detached": detached, "head_oid": oid,
                "upstream": upstream, "ahead": ahead, "behind": behind,
                "files": files}

    @staticmethod
    def _pr_summary(pr):
        if pr.get("state") == "none":
            return "none"
        if pr.get("state") != "ok":
            return pr.get("state") or "unavailable"
        label = f"#{pr.get('number')}"
        if pr.get("is_draft"):
            label += " draft"
        elif pr.get("review_decision"):
            label += " " + str(pr["review_decision"]).lower().replace("_", " ")
        return label

    @staticmethod
    def _github_slug(remote_url):
        value = str(remote_url or "").strip()
        patterns = (
            r"^git@([^:]+):([^/]+)/(.+?)(?:\.git)?$",
            r"^(?:https?|ssh)://(?:git@)?([^/]+)/([^/]+)/(.+?)(?:\.git)?$",
        )
        for pattern in patterns:
            match = re.match(pattern, value)
            if not match:
                continue
            host, owner, repo = match.groups()
            repo = re.sub(r"\.git$", "", repo)
            if (re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) and
                    re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) and
                    re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo)):
                return f"{owner}/{repo}" if host.lower() == "github.com" else \
                       f"{host}/{owner}/{repo}"
        return None

    def _github_pr(self, branch, repo_slug):
        if not branch:
            return {"state": "unavailable", "error": "Detached HEAD has no branch pull request"}
        if not repo_slug:
            return {"state": "unavailable", "error": "Git remote is not a recognized GitHub repository"}
        if not self.gh:
            return {"state": "unavailable", "error": "GitHub CLI is not installed"}
        fields = ("number,state,isDraft,url,reviewDecision,mergeStateStatus,mergeable,"
                  "statusCheckRollup,baseRefName,headRefName,title,headRefOid")
        result = self._run([self.gh, "pr", "view", branch, "--repo", repo_slug,
                            "--json", fields], 8)
        if not result["ok"]:
            detail = (result["stderr"] or result["stdout"]).strip()
            if re.search(r"no pull requests found|could not resolve to a pull request", detail, re.I):
                return {"state": "none", "summary": "No pull request"}
            return {"state": "stale" if not result.get("missing") else "unavailable",
                    "error": detail[:500] or "GitHub pull request lookup failed"}
        try:
            raw = json.loads(result["stdout"])
        except (TypeError, ValueError):
            return {"state": "stale", "error": "GitHub CLI returned malformed JSON"}
        checks = raw.get("statusCheckRollup") or []
        check_states = []
        for check in checks:
            value = str(check.get("conclusion") or check.get("state") or
                        check.get("status") or "UNKNOWN").upper()
            check_states.append(value)
        failed = sum(value in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
                     for value in check_states)
        pending = sum(value in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "UNKNOWN", "")
                      for value in check_states)
        return {"state": "ok", "number": raw.get("number"),
                "url": raw.get("url"), "title": raw.get("title"),
                "is_draft": bool(raw.get("isDraft")),
                "review_decision": raw.get("reviewDecision"),
                "merge_state": raw.get("mergeStateStatus"),
                "mergeable": raw.get("mergeable"),
                "base": raw.get("baseRefName"), "head": raw.get("headRefName"),
                "head_oid": raw.get("headRefOid"),
                "checks": {"total": len(check_states), "failed": failed,
                           "pending": pending,
                           "passed": max(0, len(check_states) - failed - pending)}}

    def _observe(self, root, worktree, include_github=True):
        started = time.perf_counter()
        status_result = self._run(
            ["git", "-C", worktree, "status", "--porcelain=v2", "--branch", "-z",
             "--untracked-files=normal"], 5)
        if not status_result["ok"]:
            detail = (status_result["stderr"] or status_result["stdout"]).strip()
            return {"ok": False, "state": "stale", "root": root, "worktree": worktree,
                    "error": detail[:500] or "Git status failed", "observed_at": self.clock(),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        status = self._parse_status(status_result["stdout"])
        remotes_result = self._run(["git", "-C", worktree, "remote"], 3)
        remotes = sorted(line.strip() for line in remotes_result["stdout"].splitlines()
                         if line.strip()) if remotes_result["ok"] else []
        upstream = status.get("upstream") or ""
        remote = upstream.split("/", 1)[0] if "/" in upstream else (
            "origin" if "origin" in remotes else (remotes[0] if remotes else None))
        remote_branch = upstream.split("/", 1)[1] if "/" in upstream else status.get("branch")
        remote_url = None
        repo_slug = None
        if remote:
            remote_result = self._run(
                ["git", "-C", worktree, "remote", "get-url", remote], 3)
            if remote_result["ok"]:
                remote_url = remote_result["stdout"].strip()
                repo_slug = self._github_slug(remote_url)
        default_base = "main"
        if remote:
            base_result = self._run(
                ["git", "-C", worktree, "symbolic-ref", "--quiet", "--short",
                 f"refs/remotes/{remote}/HEAD"], 3)
            if base_result["ok"] and "/" in base_result["stdout"].strip():
                default_base = base_result["stdout"].strip().split("/", 1)[1]
        if remote and status.get("branch") and not status.get("upstream"):
            ahead_result = self._run(
                ["git", "-C", worktree, "rev-list", "--count",
                 f"{remote}/{default_base}..HEAD"], 3)
            behind_result = self._run(
                ["git", "-C", worktree, "rev-list", "--count",
                 f"HEAD..{remote}/{default_base}"], 3)
            if ahead_result["ok"] and ahead_result["stdout"].strip().isdigit():
                status["ahead"] = int(ahead_result["stdout"].strip())
            if behind_result["ok"] and behind_result["stdout"].strip().isdigit():
                status["behind"] = int(behind_result["stdout"].strip())
        log_result = self._run(
            ["git", "-C", worktree, "log", "-1", "--format=%H%x00%h%x00%s%x00%ct"], 3)
        latest_commit = None
        if log_result["ok"]:
            values = log_result["stdout"].strip().split("\0")
            if len(values) >= 4:
                latest_commit = {"oid": values[0], "short_oid": values[1],
                                 "subject": values[2], "at": int(values[3] or 0)}
        pr = (self._github_pr(status.get("branch"), repo_slug) if include_github else
              {"state": "not_observed",
               "summary": "Open repository details to check GitHub"})
        conflicts = [item for item in status["files"] if item["conflict"]]
        observed = {"ok": True, "state": "ok", "root": root, "worktree": worktree,
                    **status, "dirty": bool(status["files"]), "conflicts": len(conflicts),
                    "remotes": remotes, "remote": remote, "remote_branch": remote_branch,
                    "repo_slug": repo_slug, "default_base": default_base,
                    "latest_commit": latest_commit, "pr": pr,
                    "observed_at": self.clock(),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        material = {key: observed.get(key) for key in
                    ("root", "worktree", "branch", "detached", "head_oid", "upstream",
                     "ahead", "behind", "remote", "remote_branch", "repo_slug")}
        material["files"] = [{key: item.get(key) for key in
                             ("path", "status", "staged", "unstaged", "untracked", "conflict")}
                             for item in observed["files"]]
        material["pr"] = ({key: pr.get(key) for key in
                           ("state", "number", "is_draft", "head_oid", "base", "head")}
                          if pr.get("state") in ("ok", "none") else None)
        observed["revision"] = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
        observed["actions"] = self._actions(observed)
        return observed

    @staticmethod
    def _actions(snapshot):
        dirty = bool(snapshot.get("dirty"))
        conflicts = int(snapshot.get("conflicts") or 0)
        branch = snapshot.get("branch")
        remote = snapshot.get("remote")
        pr = snapshot.get("pr") or {}
        commit_enabled = dirty and not conflicts
        push_enabled = bool(branch and remote and int(snapshot.get("ahead") or 0) > 0)
        create_enabled = bool(branch and remote and pr.get("state") == "none")
        ready_enabled = bool(pr.get("state") == "ok" and pr.get("is_draft"))
        files = snapshot.get("files") or []
        default_message = (f"Update {files[0]['path']}" if len(files) == 1 else
                           f"Update {len(files)} files" if files else "Update repository")
        return {
            "commit": {"enabled": commit_enabled,
                       "reason": ("Resolve conflicts first" if conflicts else
                                  "No changed files" if not dirty else None),
                       "files": files, "default_message": default_message},
            "push": {"enabled": push_enabled,
                     "reason": ("Detached HEAD" if not branch else "No Git remote" if not remote
                                else "No local commits to push"),
                     "remote": remote, "branch": snapshot.get("remote_branch") or branch,
                     "ahead": int(snapshot.get("ahead") or 0),
                     "set_upstream": not bool(snapshot.get("upstream"))},
            "pr_create_draft": {"enabled": create_enabled,
                                "reason": ("Detached HEAD" if not branch else
                                           "No Git remote" if not remote else
                                           "A pull request already exists" if pr.get("state") == "ok"
                                           else pr.get("error")),
                                "title": (snapshot.get("latest_commit") or {}).get("subject") or
                                         (branch or "Draft pull request"),
                                "body": "", "base": snapshot.get("default_base") or "main"},
            "pr_mark_ready": {"enabled": ready_enabled,
                              "reason": ("No pull request" if pr.get("state") == "none" else
                                         "Pull request is already ready" if pr.get("state") == "ok"
                                         else pr.get("error")),
                              "number": pr.get("number"), "url": pr.get("url")},
        }

    def snapshot(self, root, worktree=None, test_outcome=None, force=False,
                 include_github=True):
        root = os.path.realpath(os.path.expanduser(str(root or "")))
        worktree = os.path.realpath(os.path.expanduser(str(worktree or root)))
        key = (root, worktree, bool(include_github))
        now = self.clock()
        with self.lock:
            if not include_github:
                full = self.cache.get((root, worktree, True))
                if not force and full and full[0] > now:
                    out = dict(full[1])
                    out["cached"] = True
                    out["tests"] = test_outcome or {"state": "not_observed"}
                    return out
            cached = self.cache.get(key)
            if not force and cached and cached[0] > now:
                out = dict(cached[1])
                out["cached"] = True
                out["tests"] = test_outcome or {"state": "not_observed"}
                return out
        out = self._observe(root, worktree, include_github=include_github)
        if out.get("ok"):
            with self.lock:
                self.cache[key] = (now + self.cache_seconds, dict(out))
                if len(self.cache) > 300:
                    self.cache = {key: self.cache[key]}
        out["cached"] = False
        out["tests"] = test_outcome or {"state": "not_observed"}
        return out

    def perform(self, kind, snapshot, payload):
        if not snapshot or not snapshot.get("ok"):
            return {"ok": False, "error": (snapshot or {}).get("error") or
                    "Repository preview is unavailable"}
        current = self.snapshot(snapshot["root"], snapshot["worktree"], force=True)
        if not current.get("ok"):
            return current
        if str(payload.get("revision") or "") != current.get("revision"):
            return {"ok": False, "error": "Repository changed since preview; review it again",
                    "stale": True, "snapshot": current}
        action = current.get("actions", {}).get(kind.removeprefix("git_").replace("pr_", "pr_"))
        if kind == "git_commit":
            action = current["actions"]["commit"]
        elif kind == "git_push":
            action = current["actions"]["push"]
        elif kind == "pr_create_draft":
            action = current["actions"]["pr_create_draft"]
        elif kind == "pr_mark_ready":
            action = current["actions"]["pr_mark_ready"]
        if not action or not action.get("enabled"):
            return {"ok": False, "error": action.get("reason") if action else "Action unavailable"}
        worktree = current["worktree"]
        if kind == "git_commit":
            message = str(payload.get("message") or "").strip()
            if not message or len(message) > 1000 or "\x00" in message:
                return {"ok": False, "error": "Commit message must be 1–1,000 characters"}
            requested = payload.get("paths") or []
            if not isinstance(requested, list) or not requested or len(requested) > 500:
                return {"ok": False, "error": "Choose 1–500 changed files"}
            allowed = {item["path"] for item in current["files"]}
            paths = []
            for value in requested:
                path = str(value or "")
                if path not in allowed or "\x00" in path:
                    return {"ok": False, "error": "A selected file is stale or invalid"}
                if path not in paths:
                    paths.append(path)
            staged = self._run(["git", "-C", worktree, "add", "--", *paths], 20)
            if not staged["ok"]:
                return {"ok": False, "error": (staged["stderr"] or staged["stdout"]).strip()[:1000]
                        or "Could not stage selected files",
                        "snapshot": self.snapshot(current["root"], worktree, force=True)}
            result = self._run(["git", "-C", worktree, "commit", "-m", message], 30)
        elif kind == "git_push":
            remote = action.get("remote")
            branch = action.get("branch")
            argv = ["git", "-C", worktree, "push"]
            if action.get("set_upstream"):
                argv.append("--set-upstream")
            argv.extend([remote, f"HEAD:refs/heads/{branch}"])
            result = self._run(argv, 60)
        elif kind == "pr_create_draft":
            title = str(payload.get("title") or "").strip()
            body = str(payload.get("body") or "")
            base = str(payload.get("base") or "").strip()
            if not title or len(title) > 200:
                return {"ok": False, "error": "Pull-request title must be 1–200 characters"}
            if len(body) > 20_000:
                return {"ok": False, "error": "Pull-request body is too long"}
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", base) or base.startswith(("-", "/")):
                return {"ok": False, "error": "Invalid base branch"}
            if not self.gh:
                return {"ok": False, "error": "GitHub CLI is not installed"}
            if not current.get("repo_slug"):
                return {"ok": False, "error": "Git remote is not a recognized GitHub repository"}
            result = self._run([self.gh, "pr", "create", "--repo", current["repo_slug"],
                                "--draft", "--title", title, "--body", body,
                                "--base", base, "--head", current["branch"]], 60)
        elif kind == "pr_mark_ready":
            try:
                number = int(payload.get("number"))
            except (TypeError, ValueError):
                number = 0
            if number <= 0 or number != int((current.get("pr") or {}).get("number") or 0):
                return {"ok": False, "error": "Pull request changed since preview"}
            if not self.gh:
                return {"ok": False, "error": "GitHub CLI is not installed"}
            if not current.get("repo_slug"):
                return {"ok": False, "error": "Git remote is not a recognized GitHub repository"}
            result = self._run([self.gh, "pr", "ready", str(number),
                                "--repo", current["repo_slug"]], 30)
        else:
            return {"ok": False, "error": "Unknown repository action"}
        if not result["ok"]:
            return {"ok": False, "error": (result["stderr"] or result["stdout"]).strip()[:1000]
                    or "Repository action failed", "code": result.get("code"),
                    "snapshot": self.snapshot(current["root"], worktree, force=True)}
        with self.lock:
            self.cache.pop((current["root"], current["worktree"], False), None)
            self.cache.pop((current["root"], current["worktree"], True), None)
        return {"ok": True, "kind": kind,
                "summary": (result["stdout"] or result["stderr"]).strip()[:1000],
                "snapshot": self.snapshot(current["root"], current["worktree"], force=True)}
