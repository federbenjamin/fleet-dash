"""Claude process/tty resolution, Codex terminal routes, background Claude
attach transport (invariants 25, 30)."""
import os, re, time, shlex, signal, subprocess, uuid
from .claude_background import ClaudeBackgroundTransport, ClaudeBackgroundError


from . import paths as pathcfg




class TransportOps:

    def _claude_process_command(self, reg):
        """Return one verified process command per live Claude PID.

        This is used only to discover whether Claude was started with a bypass-
        enabling flag. Cache it so the two-second fleet scan never gains a `ps`
        subprocess per session.
        """
        try:
            pid = int(reg.get("pid") or 0)
        except (TypeError, ValueError):
            return ""
        if pid <= 1:
            return ""
        if pid in self._claude_command_cache:
            return self._claude_command_cache[pid]
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                text=True, timeout=5).stdout.strip()
        except Exception:
            command = ""
        self._claude_command_cache[pid] = command
        return command

    @staticmethod
    def _claude_auto_model(model):
        """Whether the observed concrete model can expose Claude Auto mode."""
        value = str(model or "").lower().replace(".", "-")
        return bool(re.search(
            r"(?:sonnet-(?:4-6|5)|opus-(?:4-[678]|[5-9])|fable-5)", value))

    def _claude_permission_modes(self, reg, tail):
        """Native Shift-Tab cycle for this process, in its documented order."""
        modes = ["default", "acceptEdits", "plan"]
        command = self._claude_process_command(reg)
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        bypass = any(item in ("--allow-dangerously-skip-permissions",
                              "--dangerously-skip-permissions") for item in argv)
        for index, item in enumerate(argv[:-1]):
            if item == "--permission-mode" and argv[index + 1] == "bypassPermissions":
                bypass = True
        bypass = bypass or "--permission-mode=bypassPermissions" in argv \
            or tail.permission_mode == "bypassPermissions"
        if bypass:
            modes.append("bypassPermissions")
        if self._claude_auto_model(tail.model) or tail.permission_mode == "auto":
            modes.append("auto")
        return modes

    def _tty_for_pid(self, pid):
        """Resolve a foreground Claude tty; background jobs use `claude attach`.

        Some foreground launchers lose their controlling-terminal marker while
        retaining the terminal on fd 0/1/2. Inspect only those descriptors and
        accept only an exact macOS pseudo-terminal path. The caller must already
        have excluded registry ``kind:bg`` sessions: an open PTY descriptor is not
        evidence that the background job belongs to an iTerm tab.
        """
        try:
            pid = int(pid or 0)
        except (TypeError, ValueError):
            return ""
        if pid <= 1:
            return ""
        cached = self._tty_cache.get(pid)
        if cached:
            return cached
        try:
            tty = subprocess.run(
                ["ps", "-p", str(pid), "-o", "tty="], capture_output=True,
                text=True, timeout=5).stdout.strip()
        except Exception:
            tty = ""
        if tty and tty != "??" and re.fullmatch(r"ttys[0-9A-Za-z]+", tty):
            self._tty_cache[pid] = tty
            return tty
        try:
            opened = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "0,1,2", "-Fn"],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            opened = ""
        for line in opened.splitlines():
            path = line[1:] if line.startswith("n") else ""
            if re.fullmatch(r"/dev/ttys[0-9A-Za-z]+", path):
                tty = os.path.basename(path)
                self._tty_cache[pid] = tty
                return tty
        return ""

    def _codex_terminal_routes(self, force=False):
        """Find exact Codex TUIs attached to Fleet's own App Server socket.

        A transcript, cwd, or ``source=vscode`` is not route evidence. The process
        must have a real tty and its argv must contain all three exact values:
        ``codex resume``, Fleet's Unix socket, and one canonical thread UUID.
        Multiple ttys for the same UUID are ambiguous and remain app-server-only.
        """
        now = time.monotonic()
        cached_at, cached = self._codex_terminal_routes_cache
        if now - cached_at < 2 and (cached or not force):
            return dict(cached)
        try:
            from .codex_runtime import codex_control_socket
            expected_socket = os.path.realpath(codex_control_socket(
                managed=not self.is_staging, state_dir=pathcfg.BASE))
            result = subprocess.run(
                ["ps", "-axo", "pid=,tty=,command="], capture_output=True,
                text=True, timeout=2)
            output = result.stdout or ""
            if result.returncode or len(output) > 2_000_000:
                raise RuntimeError("bounded Codex terminal lookup failed")
        except Exception:
            routes = dict(cached) if now - cached_at < 10 else {}
            self._codex_terminal_routes_cache = (now, routes)
            return routes

        candidates = {}
        for line in output.splitlines():
            match = re.match(r"^\s*(\d+)\s+(\S+)\s+(.+)$", line)
            if not match:
                continue
            pid, tty, command = int(match.group(1)), match.group(2), match.group(3)
            if not re.fullmatch(r"ttys[0-9A-Za-z]+", tty):
                continue
            try:
                argv = shlex.split(command)
            except ValueError:
                continue
            try:
                resume_index = argv.index("resume")
            except ValueError:
                continue
            if not any(os.path.basename(part).lower() == "codex"
                       for part in argv[:resume_index]):
                continue
            remote = ""
            for index, part in enumerate(argv):
                if part == "--remote" and index + 1 < len(argv):
                    remote = argv[index + 1]
                    break
                if part.startswith("--remote="):
                    remote = part.split("=", 1)[1]
                    break
            if not remote.startswith("unix://") or \
                    os.path.realpath(remote[len("unix://"):]) != expected_socket:
                continue
            thread_id = ""
            candidate = argv[-1] if resume_index + 1 < len(argv) else ""
            try:
                canonical = str(uuid.UUID(candidate))
            except (ValueError, AttributeError):
                canonical = ""
            if canonical and canonical == candidate.lower():
                thread_id = canonical
            if thread_id:
                candidates.setdefault(thread_id, []).append(
                    {"tty": f"/dev/{tty}", "pid": pid})

        routes = {}
        for thread_id, matches in candidates.items():
            ttys = {item["tty"] for item in matches}
            if len(ttys) == 1:
                routes[thread_id] = max(matches, key=lambda item: item["pid"])
        self._codex_terminal_routes_cache = (now, routes)
        return dict(routes)

    def _codex_terminal_route(self, thread_id, force=False):
        try:
            canonical = str(uuid.UUID(str(thread_id or "")))
        except (ValueError, AttributeError):
            return None
        return self._codex_terminal_routes(force=force).get(canonical)

    @staticmethod
    def _apply_codex_terminal_routes(sessions, routes):
        """Expose only capabilities proved by an exact attached Fleet TUI."""
        for session in sessions:
            route = routes.get(str(session.get("native_session_id") or ""))
            if not route:
                continue
            unavailable = (session.get("state") in ("blocked", "error", "stale") or
                           bool(session.get("pending")))
            capabilities = dict(session.get("capabilities") or {})
            capabilities.update(
                submit=not unavailable, queue_submit=False, focus_terminal=True,
                focus_terminal_mode="focus", focus_terminal_label="open",
                focus_terminal_reason="Bring the attached Codex terminal to the front")
            app_server_active = session.get("control_state") == "connected_active"
            session.update(
                capabilities=capabilities, terminal_attached=True,
                queue_accepting=False, headless=False, read_only=False,
                read_only_reason=None,
                # A terminal is an additional focus/fallback route. It must not
                # replace exact App Server authority recovered after compaction;
                # doing so sends the next message as terminal input instead of a
                # canonical turn/steer request.
                control_state=(session.get("control_state") if app_server_active else
                               "terminal_active" if session.get("state") in
                               ("running", "stalled", "needs_you") else
                               "terminal_idle"))

    @staticmethod
    def _is_background_claude(reg):
        return str((reg or {}).get("kind") or "").lower() in ("bg", "background")

    @staticmethod
    def _native_write_failed_before_delivery(result):
        """Return whether the transport proves that no native input was written."""
        return (not (result or {}).get("ok") and
                (result or {}).get("code") in
                ("injector_not_launched", "background_connection_lost"))

    @staticmethod
    def _background_job_id(reg):
        return str((reg or {}).get("jobId") or (reg or {}).get("id") or "")

    def _background_claude_transport(self):
        if self._claude_background is not None:
            return self._claude_background
        try:
            self._claude_background = ClaudeBackgroundTransport(
                self.cfg.get("claude_command") or None, home=pathcfg.HOME)
            self._claude_background_error = None
            return self._claude_background
        except ClaudeBackgroundError as exc:
            self._claude_background_error = str(exc)[:300]
            return None

    def _write_background_claude(self, reg, steps, step_delay):
        transport = self._background_claude_transport()
        if transport is None:
            return {"ok": False, "code": "background_connection_lost",
                    "error": self._claude_background_error or
                             "Claude background connection is unavailable"}
        return transport.write(self._background_job_id(reg), steps,
                               step_delay=step_delay)

    def _focus_background_claude(self, reg):
        transport = self._background_claude_transport()
        if transport is None:
            return {"ok": False, "code": "background_connection_lost",
                    "error": self._claude_background_error or
                             "Claude background connection is unavailable"}
        try:
            executable, job_id, cwd = transport.attach_command(
                self._background_job_id(reg), reg.get("cwd") or pathcfg.HOME)
        except ClaudeBackgroundError as exc:
            return {"ok": False, "code": "background_connection_lost",
                    "error": str(exc)[:300]}
        if not os.path.isdir(cwd):
            return {"ok": False, "error": "session working directory no longer exists"}
        command = (f"cd {shlex.quote(cwd)} && {shlex.quote(executable)} attach "
                   f"{shlex.quote(job_id)}")
        result = self._iterm_write("SPAWN", [(command, False)])
        if result.get("ok"):
            result.update(command=command, transport="claude_attach",
                          session_id=reg.get("sessionId"))
        return result

    def _close_claude_session(self, reg):
        """Terminate only the registered Claude process; never close its terminal tab."""
        try:
            pid = int(reg.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 1 or pid == os.getpid():
            return {"ok": False, "error": "refusing to terminate an invalid Claude pid"}

        # Session registry entries can outlive a crashed process. Verify the PID was
        # not reused before signalling it; a cwd containing `.claude` is deliberately
        # insufficient evidence.
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                text=True, timeout=5).stdout.strip()
        except Exception as exc:
            return {"ok": False, "error": f"process lookup failed: {exc}"}
        if not command:
            return {"ok": False, "error": "Claude process is no longer running"}
        if not re.search(r"(^|[/\s])claude(?:-code)?(?:[/\s]|$)", command, re.I):
            return {"ok": False, "error": "refusing to terminate a non-Claude process"}

        if self._is_background_claude(reg):
            transport = self._background_claude_transport()
            if transport is None:
                return {"ok": False, "code": "background_connection_lost",
                        "error": self._claude_background_error or
                                 "Claude background connection is unavailable"}
            return transport.stop(self._background_job_id(reg))

        interrupted = False
        interrupt_error = None
        if reg.get("status") in ("busy", "shell", "waiting"):
            tty = self._tty_for_pid(pid)
            if tty:
                result = self._iterm_write(f"/dev/{tty}", [("\x1b", False)],
                                           step_delay=0.05)
                interrupted = bool(result.get("ok"))
                if not interrupted:
                    interrupt_error = result.get("error") or "interrupt failed"
                else:
                    time.sleep(0.15)

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as exc:
            return {"ok": False, "error": f"could not terminate Claude: {exc}"}
        self._tty_cache.pop(pid, None)
        self._claude_command_cache.pop(pid, None)
        result = {"ok": True, "closed": True, "interrupted": interrupted}
        if interrupt_error:
            result["warning"] = interrupt_error
        return result

    def reopen_claude_session(self, sid):
        """Open a saved Claude transcript in a new iTerm tab.

        Both the UUID and transcript path come from the ledger, but are validated
        again here because this action crosses the local file/terminal boundary.
        """
        if any(row.get("sessionId") == sid for row in self.live_sessions()):
            return {"ok": False, "error": "session is already live"}
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute(
                "SELECT cwd, provider, transcript_path, closed_at FROM session_runs "
                "WHERE session_id=?", (sid,)).fetchone()
        except Exception as exc:
            return {"ok": False, "error": f"session lookup failed: {exc}"}
        finally:
            if db is not None:
                db.close()
        if not row or row[1] != "claude" or row[3] is None:
            return {"ok": False, "error": "session is not a closed Claude conversation"}
        if not self._safe_claude_transcript(sid, row[2]):
            return {"ok": False, "error": "saved Claude transcript is unavailable"}
        cwd = self._safe_reopen_cwd(row[0])
        if not cwd:
            return {"ok": False,
                    "error": "the session working directory no longer exists or is outside home"}
        command = (f"cd {shlex.quote(cwd)} && claude --resume "
                   f"{shlex.quote(str(sid))}")
        result = self._iterm_write("SPAWN", [(command, False)])
        if result.get("ok"):
            result.update(reopened=True, session_id=sid, cwd=cwd, command=command)
        return result
