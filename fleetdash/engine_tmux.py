"""tmux control transport: pane discovery, key delivery, spawn, focus.

Every other Claude control path is `tell application "iTerm2"`, so it dies with a
terminal switch. tmux is emulator-agnostic by construction, works over SSH, and
outlives the emulator that renders it, which is why it is the transport Fleet
targets (invariants 3, 20, 24, 25).

Two rules hold everywhere in this module. Transport selection is **server-derived**:
a session uses tmux only when the tty Fleet already resolved from its PID is a live
tmux pane — a client never names a socket, pane, session, or command. And delivery
failure keeps invariant 66's split intact: `terminal_not_available` proves that no
byte reached the terminal, while `delivery_uncertain` means some may have.

Verified against tmux 3.7b, 2026-07-24. `send-keys -l -- <text>` delivers the
argument byte for byte: no key-name lookup, no C-escape processing (`a\\eb\\nc` stays
literal), UTF-8 preserved, embedded LF preserved, and a raw ESC/CR passes through
unchanged. `paste-buffer` was rejected because without `-r` it rewrites LF as CR,
which would submit a multi-line message one line at a time.
"""
import os
import re
import stat
import time
import shlex
import shutil
import subprocess

from . import paths as pathcfg

_PANE_TTY = re.compile(r"/dev/ttys[0-9A-Za-z]{1,16}\Z")
_PANE_ID = re.compile(r"%[0-9]{1,9}\Z")
_APP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TMUX_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
# pane_id, pane_tty, session_name, session_attached, pane_dead
_PANE_FORMAT = ("#{pane_id}\t#{pane_tty}\t#{session_name}"
                "\t#{session_attached}\t#{pane_dead}")


class TmuxOps:
    """Engine mixin: the tmux half of the terminal transport dispatcher."""

    TMUX_PANE_CACHE_SECONDS = 2.0
    TMUX_SOCKET_LIMIT = 8
    TMUX_QUERY_TIMEOUT = 2
    TMUX_WRITE_TIMEOUT = 5
    TMUX_SPAWN_TIMEOUT = 15
    TMUX_MAX_PANES = 2000

    # ------------------------------------------------------------ primitives
    def _tmux_command(self):
        """Resolve tmux for launchd's minimal PATH; empty when unavailable."""
        if self._tmux_executable is not None:
            return self._tmux_executable
        configured = str(self.cfg.get("tmux_command") or "").strip()
        if configured:
            candidates = [os.path.realpath(os.path.expanduser(configured))]
        else:
            candidates = [item for item in
                          (shutil.which("tmux"), "/opt/homebrew/bin/tmux",
                           "/usr/local/bin/tmux", "/usr/bin/tmux") if item]
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                self._tmux_executable = candidate
                return candidate
        self._tmux_executable = ""
        return ""

    def _tmux_run(self, socket_path, args, timeout=None):
        """One bounded tmux client call, or None when it could not complete.

        A None result is always ambiguous — a timeout can expire after the client
        already handed the bytes to the server — so callers must never treat it as
        proof that nothing was written.
        """
        executable = self._tmux_command()
        if not executable:
            return None
        try:
            return subprocess.run(
                [executable, "-S", socket_path, *args], capture_output=True,
                text=True, timeout=timeout or self.TMUX_QUERY_TIMEOUT)
        except Exception:
            return None

    @staticmethod
    def _tmux_detail(result, fallback):
        if result is None:
            return fallback
        return (result.stderr or result.stdout or fallback).strip()[:240] or fallback

    def _tmux_socket_paths(self):
        """Bounded enumeration of this user's tmux server sockets."""
        try:
            entries = sorted(os.listdir(pathcfg.TMUX_SOCKETS))
        except OSError:
            return []
        sockets = []
        for name in entries:
            if len(sockets) >= self.TMUX_SOCKET_LIMIT:
                break
            path = os.path.join(pathcfg.TMUX_SOCKETS, name)
            try:
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    sockets.append(path)
            except OSError:
                continue
        return sockets

    # -------------------------------------------------------------- discovery
    def _tmux_panes(self, force=False):
        """Map `/dev/ttys…` to the live tmux pane that owns it.

        Cached like `_codex_terminal_routes`: this runs on the act/write path
        only, never inside the two-second fleet scan, so a stale socket costs
        nothing on the poll loop.
        """
        now = time.monotonic()
        with self._tmux_lock:
            cached_at, cached = self._tmux_panes_cache
            if not force and now - cached_at < self.TMUX_PANE_CACHE_SECONDS:
                return dict(cached)
        panes = {}
        for socket_path in self._tmux_socket_paths():
            result = self._tmux_run(
                socket_path, ["list-panes", "-a", "-F", _PANE_FORMAT])
            output = "" if result is None else (result.stdout or "")
            if result is None or result.returncode or len(output) > 1_000_000:
                continue
            for line in output.splitlines()[:self.TMUX_MAX_PANES]:
                fields = line.split("\t")
                if len(fields) != 5:
                    continue
                pane_id, tty, session, attached, dead = fields
                if not _PANE_ID.match(pane_id) or not _PANE_TTY.match(tty):
                    continue
                if dead == "1":
                    continue
                # The session name is only ever displayed. It must not gate the
                # pane: an operator may legitimately name a session "my.repo",
                # and rejecting it would leave Fleet blind to a real terminal.
                panes.setdefault(tty, {
                    "socket": socket_path, "pane_id": pane_id,
                    "session": re.sub(r"[\x00-\x1f\x7f]", " ", session)[:64],
                    "attached": attached == "1"})
        with self._tmux_lock:
            self._tmux_panes_cache = (time.monotonic(), panes)
        return dict(panes)

    def _tmux_target_for_tty(self, tty, force=False):
        """The pane owning an exact resolved tty, or None."""
        tty = str(tty or "")
        if not _PANE_TTY.match(tty):
            return None
        return self._tmux_panes(force=force).get(tty)

    # ------------------------------------------------------------- delivery
    def _tmux_write(self, pane, steps, step_delay=None):
        """Deliver Engine-composed keys to one exact pane.

        Mirrors `_iterm_write`'s return contract. Each key is its own `send-keys`
        call, matching the applet's per-step requests: invariant 4's recipes are
        unchanged, and the delay only ever fires BETWEEN steps (invariant 24).
        """
        steps = list(steps)
        if any(text == "__FOCUS__" for text, _ in steps):
            return self._tmux_focus_pane(pane)
        if not self._tmux_command():
            return {"ok": False, "code": "terminal_not_available",
                    "error": "tmux is not installed; set tmux_command in config.json"}
        socket_path, target = pane["socket"], pane["pane_id"]
        delay = 0.4 if step_delay is None else max(0.0, min(5.0, float(step_delay)))
        wrote = False
        for index, (text, newline) in enumerate(steps):
            # raw CR is what a raw-mode TUI treats as Enter; LF only inserts a
            # newline, which is why the caller asks for the two separately
            for chunk in [item for item in (text, "\r" if newline else "") if item]:
                result = self._tmux_run(
                    socket_path, ["send-keys", "-t", target, "-l", "--", chunk],
                    timeout=self.TMUX_WRITE_TIMEOUT)
                if result is None:
                    return {"ok": False, "code": "delivery_uncertain",
                            "error": ("delivery uncertain — tmux did not report the "
                                      "result of a key; check the terminal before "
                                      "retrying")}
                if result.returncode:
                    detail = self._tmux_detail(result, "tmux send-keys failed")
                    if wrote:
                        return {"ok": False, "code": "delivery_uncertain",
                                "error": ("delivery uncertain — some keys landed before "
                                          f"tmux refused the next one: {detail}")}
                    return {"ok": False, "code": "terminal_not_available",
                            "error": f"tmux could not reach this pane: {detail}"}
                wrote = True
            if index + 1 < len(steps) and delay:
                time.sleep(delay)
        return {"ok": True, "transport": "tmux"}

    # ------------------------------------------------------------ observation
    def _tmux_capture(self, pane, max_rows=200, max_columns=400):
        """Read one pane's visible screen as bounded plain text.

        Strictly read-only — no keys are sent, so a capture cannot disturb the
        session. `-p` prints the rendered screen without escape sequences (`-e`
        would keep them), but a pane can render anything, so the result is still
        control-character scrubbed and bounded on both axes.
        """
        if not self._tmux_command():
            return {"ok": False, "error": "tmux is not installed"}
        result = self._tmux_run(
            pane["socket"], ["capture-pane", "-p", "-t", pane["pane_id"]])
        if result is None or result.returncode:
            return {"ok": False,
                    "error": self._tmux_detail(result, "tmux could not read this pane")}
        rows = (result.stdout or "").splitlines()
        truncated = len(rows) > max_rows
        lines = [_CONTROL.sub(" ", row).rstrip()[:max_columns] for row in rows[-max_rows:]]
        while lines and not lines[-1]:
            lines.pop()             # trailing blanks are unused screen, not content
        return {"ok": True, "lines": lines, "truncated": truncated}

    # One marker per pane. `display-message -p` runs inside the same command
    # sequence and writes to the same stdout, which is the only framing
    # capture-pane offers. The marker carries an INDEX, never the pane id:
    # display-message expands `%` and `#` in its format string, so `%12` comes
    # back as `12` and would silently collide.
    CAPTURE_MARK = "\x1efleet-pane:"

    def _tmux_capture_many(self, panes, max_rows=200, max_columns=400):
        """Capture many panes in ONE tmux client invocation per socket.

        Measured 2026-07-24 on tmux 3.7b, 49 panes: 241 ms as one client
        invocation per pane, **5.6 ms** batched. The whole cost is forking the
        tmux client, not rendering the grid — which is why a fleet-wide
        observation pass is affordable at all (invariants 73, 74).

        Returns {pane_id: {"lines": [...], "truncated": bool}} for the panes that
        answered. A pane that vanished mid-call is simply absent; that is normal,
        not an error.
        """
        out = {}
        if not self._tmux_command():
            return out
        by_socket = {}
        for pane in panes or []:
            by_socket.setdefault(pane["socket"], []).append(pane)
        for socket_path, group in by_socket.items():
            group = group[:self.TMUX_MAX_PANES]
            args = []
            for index, pane in enumerate(group):
                if index:
                    args.append(";")
                args += ["display-message", "-p", f"{self.CAPTURE_MARK}{index}",
                         ";", "capture-pane", "-p", "-t", pane["pane_id"]]
            result = self._tmux_run(socket_path, args,
                                    timeout=self.TMUX_WRITE_TIMEOUT)
            if result is None or result.returncode:
                continue
            for index, rows in self._split_capture(result.stdout or "").items():
                if index >= len(group):
                    continue
                truncated = len(rows) > max_rows
                lines = [_CONTROL.sub(" ", row).rstrip()[:max_columns]
                         for row in rows[-max_rows:]]
                while lines and not lines[-1]:
                    lines.pop()
                out[group[index]["pane_id"]] = {"lines": lines,
                                                "truncated": truncated}
        return out

    @classmethod
    def _split_capture(cls, text):
        """Slice one batched stdout back into per-pane row lists by marker."""
        blocks, current = {}, None
        for line in text.split("\n"):
            if line.startswith(cls.CAPTURE_MARK):
                try:
                    current = int(line[len(cls.CAPTURE_MARK):].strip())
                except ValueError:
                    current = None
                    continue
                blocks[current] = []
            elif current is not None:
                blocks[current].append(line)
        return blocks

    def _tmux_focus_pane(self, pane):
        """Select the pane, then raise the terminal application generically.

        Selecting the pane is emulator-agnostic; raising whatever renders it is
        not, so the application name is configuration rather than an Apple Event
        to iTerm (invariant 25). Focus types nothing, so any failure here is
        proven to have written no input.
        """
        for args in (["select-window", "-t", pane["pane_id"]],
                     ["select-pane", "-t", pane["pane_id"]]):
            result = self._tmux_run(pane["socket"], args)
            if result is None or result.returncode:
                detail = self._tmux_detail(result, "tmux could not select the pane")
                return {"ok": False, "code": "terminal_not_available",
                        "error": f"tmux could not focus this pane: {detail}"}
        warning = None
        app = self._terminal_app_name()
        if app:
            try:
                raised = subprocess.run(["open", "-a", app], capture_output=True,
                                        text=True, timeout=10)
            except Exception as exc:
                warning = f"the pane is selected, but {app} could not be raised: {exc}"
            else:
                if raised.returncode:
                    warning = (f"the pane is selected, but {app} could not be raised: "
                               + (raised.stderr or raised.stdout or "").strip()[:160])
        if not pane.get("attached"):
            warning = ("the pane is selected, but no terminal is attached to tmux "
                       f"session {pane.get('session')}")
        result = {"ok": True, "transport": "tmux", "focused": True}
        if warning:
            result["warning"] = warning
        return result

    # ---------------------------------------------------------------- spawn
    def _tmux_socket_hosting(self, session):
        """The socket whose server already has this session, or empty."""
        for socket_path in self._tmux_socket_paths():
            found = self._tmux_run(socket_path, ["has-session", "-t", f"={session}"])
            if found is not None and found.returncode == 0:
                return socket_path
        return ""

    def _tmux_spawn(self, command, label="claude"):
        """Add an Engine-composed command as a window in the operator's session.

        **Fleet never starts the tmux server**, and that restraint is the whole
        point. A tmux server inherits the environment of whoever started it, so a
        server launched from this launchd daemon hands Claude `PATH=/usr/bin:/bin:
        /usr/sbin:/sbin` — verified 2026-07-24, where the spawned pane answered
        `zsh:1: command not found: claude`. There is no honest way to reconstruct
        the operator's login environment from here: their passwd shell is zsh while
        their sessions actually run bash, and the directory holding `claude` is
        added by a bash startup file. Joining a server the operator started gets
        that environment exactly right, and when none is running `_terminal_spawn`
        falls back to the applet — which is what already worked.

        The command is still built only from allowlisted parts (invariant 20); this
        changes where it runs, not what may be composed.
        """
        if not self._tmux_command():
            return {"ok": False, "code": "terminal_not_available",
                    "error": "tmux is not installed; set tmux_command in config.json"}
        session = self._tmux_session_name()
        window = re.sub(r"[^A-Za-z0-9_-]", "", str(label or ""))[:24] or "claude"
        socket_path = self._tmux_socket_hosting(session)
        if not socket_path:
            return {"ok": False, "code": "terminal_not_available",
                    "error": (f"no tmux session named {session} is running — start one "
                              f"with `tmux new -s {session}` so new sessions inherit "
                              "your shell environment")}
        # Run it the way a terminal tab would: an INTERACTIVE LOGIN shell, whose
        # identity comes from the server the operator started rather than from
        # anything Fleet guessed. Verified 2026-07-24 against that server — a bare
        # `sh -c` payload still answered `command not found: claude`, while both
        # `-l -c` and `-i -l -c` resolved it, because the directory holding it is
        # added by a shell startup file rather than inherited. `-i` is what sources
        # the interactive file (.bashrc / .zshrc), which is where a zsh setup
        # usually puts its PATH. Then keep a login shell so the pane and its
        # scrollback survive the session, the way the applet's terminal tab did.
        payload = (f"${{SHELL:-/bin/sh}} -i -l -c {shlex.quote(command)}; "
                   f"exec ${{SHELL:-/bin/sh}} -l")
        result = self._tmux_run(
            socket_path, ["new-window", "-d", "-t", f"={session}", "-n", window,
                          payload], timeout=self.TMUX_SPAWN_TIMEOUT)
        if result is None:
            return {"ok": False, "code": "delivery_uncertain",
                    "error": ("tmux did not report whether the window started — "
                              f"check `tmux attach -t {session}` before retrying")}
        if result.returncode:
            return {"ok": False, "code": "terminal_not_available",
                    "error": ("tmux could not start the window: " +
                              self._tmux_detail(result, "tmux new-window failed"))}
        self._tmux_panes_cache = (0.0, {})   # the new pane must be discoverable now
        return {"ok": True, "transport": "tmux", "tmux_session": session,
                "tmux_window": window, "tmux_attach": f"tmux attach -t {session}"}

    # ------------------------------------------------------------- settings
    def _terminal_app_name(self):
        """The application `open -a` may raise on focus, or empty."""
        name = str(self.cfg.get("terminal_app") or "").strip()
        return name if _APP_NAME.match(name) else ""

    def _tmux_session_name(self):
        name = str(self.cfg.get("tmux_session") or "").strip()
        return name if _TMUX_NAME.match(name) else "fleet"
