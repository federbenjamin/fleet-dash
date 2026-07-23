#!/usr/bin/env python3
"""Transparent Codex TUI router for Fleet's managed App Server."""

import json
import os
import stat
import sys
import tempfile


REMOTE_COMMANDS = {"resume", "fork", "archive", "delete", "unarchive"}
PASS_COMMANDS = {
    "exec", "e", "review", "login", "logout", "mcp", "plugin", "mcp-server",
    "app-server", "remote-control", "app", "completion", "update", "doctor",
    "sandbox", "debug", "apply", "a", "cloud", "exec-server", "features", "help",
}
VALUE_OPTIONS = {
    "-c", "--config", "--enable", "--disable", "--remote",
    "--remote-auth-token-env", "-m", "--model", "--local-provider", "-p",
    "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a",
    "--ask-for-approval",
}
PASS_FLAGS = {"-h", "--help", "-V", "--version"}
BEGIN = "# >>> fleet-dash managed codex >>>"
END = "# <<< fleet-dash managed codex <<<"


def managed_socket(home=None):
    codex_home = os.path.abspath(os.path.expanduser(
        os.environ.get("CODEX_HOME") or os.path.join(home or "~", ".codex")))
    return os.path.join(codex_home, "app-server-control", "app-server-control.sock")


def _has_remote(args):
    return any(value == "--remote" or value.startswith("--remote=") for value in args)


def route_arguments(args, socket_path=None):
    """Insert remote mode only for interactive and remote-capable commands."""
    args = list(args)
    if _has_remote(args) or any(value in PASS_FLAGS for value in args):
        return args, False
    command = None
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            break
        if value in VALUE_OPTIONS:
            index += 2
            continue
        if any(value.startswith(option + "=") for option in VALUE_OPTIONS
               if option.startswith("--")):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        command = value
        break
    if command in PASS_COMMANDS:
        return args, False
    if command is not None and command not in REMOTE_COMMANDS:
        # Anything else is an initial interactive prompt.
        command = None
    endpoint = "unix://" + (socket_path or managed_socket())
    return ["--remote", endpoint, *args], True


def find_real_codex(path=None, own_path=None):
    own = os.path.realpath(own_path or __file__)
    explicit = os.environ.get("FLEET_DASH_CODEX_REAL")
    candidates = [explicit] if explicit else []
    for directory in (path or os.environ.get("PATH", "")).split(os.pathsep):
        if directory:
            candidates.append(os.path.join(directory, "codex"))
    for candidate in candidates:
        if not candidate:
            continue
        resolved = os.path.realpath(os.path.expanduser(candidate))
        if resolved != own and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
    raise RuntimeError("Fleet's Codex launcher could not find the real codex executable")


def launcher_paths(home=None):
    root = os.path.abspath(os.path.expanduser(home or "~"))
    bindir = os.path.join(root, ".local", "share", "fleet-dash", "bin")
    return {"bin_dir": bindir, "launcher": os.path.join(bindir, "codex"),
            "zshrc": os.path.join(root, ".zshrc")}


def _atomic_write(path, data, mode):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fleet-codex-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install_launcher(source=None, home=None):
    """Idempotently install the wrapper and one marked zsh PATH block."""
    paths = launcher_paths(home)
    source = os.path.abspath(source or __file__)
    with open(source) as handle:
        launcher = handle.read()
    _atomic_write(paths["launcher"], launcher, 0o755)
    try:
        with open(paths["zshrc"]) as handle:
            original = handle.read()
        shell_mode = stat.S_IMODE(os.stat(paths["zshrc"]).st_mode)
    except FileNotFoundError:
        original = ""
        shell_mode = 0o600
    if (BEGIN in original) != (END in original):
        raise RuntimeError("Fleet's Codex PATH block in ~/.zshrc is incomplete; repair it manually")
    backup = paths["zshrc"] + ".fleet-dash-pre-codex-launcher"
    if original and not os.path.exists(backup):
        _atomic_write(backup, original, stat.S_IMODE(os.stat(paths["zshrc"]).st_mode))
    block = (f'{BEGIN}\nexport PATH="$HOME/.local/share/fleet-dash/bin:$PATH"\n{END}')
    if BEGIN in original and END in original:
        start = original.index(BEGIN)
        finish = original.index(END, start) + len(END)
        updated = original[:start] + block + original[finish:]
    else:
        separator = "" if not original or original.endswith("\n") else "\n"
        updated = original + separator + block + "\n"
    if updated != original:
        _atomic_write(paths["zshrc"], updated, shell_mode)
    return launcher_status(home)


def launcher_status(home=None):
    paths = launcher_paths(home)
    try:
        with open(paths["zshrc"]) as handle:
            shell = handle.read()
    except OSError:
        shell = ""
    installed = os.path.isfile(paths["launcher"]) and os.access(paths["launcher"], os.X_OK)
    configured = BEGIN in shell and END in shell
    return {"installed": installed, "shell_configured": configured,
            "state": "ready" if installed and configured else "repair_needed"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--fleet-launcher-status"]:
        print(json.dumps(launcher_status(), sort_keys=True))
        return 0
    if argv == ["--fleet-launcher-install"]:
        print(json.dumps(install_launcher(), sort_keys=True))
        return 0
    try:
        executable = find_real_codex(own_path=sys.argv[0])
        routed, _ = route_arguments(argv)
        os.execv(executable, [executable, *routed])
    except Exception as exc:
        print(f"codex launcher: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
