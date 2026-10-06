"""launchd install identity: each instance's label, applet bundle id, paths, and plist.

The one source for these values. The shell scripts read them through the CLI,
run from the repo root:

    python3 -m fleetdash.launchd label production
    python3 -m fleetdash.launchd render staging [directory]

The label prefix is `FLEET_DASH_LABEL_PREFIX`, else `com.<login user name>`.
`render` writes `<directory, default ~/Library/LaunchAgents>/<label>.plist`.
"""
import os
import plistlib
import pwd
import re
import sys
import tempfile

from . import config
from . import paths as pathcfg
from .codex_runtime import private_codex_socket

INSTANCES = pathcfg.INSTANCES
_PREFIX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def _instance(instance):
    if instance not in INSTANCES:
        raise ValueError(f"unknown instance: {instance!r}")
    return instance


def label_prefix():
    prefix = (os.environ.get("FLEET_DASH_LABEL_PREFIX")
              or "com." + pwd.getpwuid(os.getuid()).pw_name)
    if not _PREFIX.match(prefix):
        raise ValueError(f"invalid launchd label prefix: {prefix!r}")
    return prefix


def label(instance):
    suffix = ".fleet-dash" if _instance(instance) == "production" else ".fleet-dash.staging"
    return label_prefix() + suffix


def bundle_id(instance):
    return label(instance) + ".injector"


def checkout(instance):
    if _instance(instance) == "production":
        return pathcfg.env_path("FLEET_DASH_PROD_CHECKOUT", pathcfg.PRODUCTION_CHECKOUT)
    return pathcfg.env_path("FLEET_DASH_STAGING_CHECKOUT", pathcfg.STAGING_CHECKOUT)


def state_dir(instance):
    if _instance(instance) == "production":
        return pathcfg.env_path("FLEET_DASH_PROD_STATE", pathcfg.PRODUCTION_BASE)
    return pathcfg.STAGING_BASE


def plist(instance):
    state = state_dir(instance)
    env = {
        pathcfg.ENV_INSTANCE: instance,
        pathcfg.ENV_STATE_DIR: state,
        pathcfg.ENV_CAPTURE_DIR: pathcfg.DEFAULT_CAPTURE_BASE,
    }
    if instance == "staging":
        env[pathcfg.ENV_STAGING_SOURCE] = checkout(instance)
        env[pathcfg.ENV_CODEX_SOCKET] = private_codex_socket(state)
        env[pathcfg.ENV_PORT] = str(config.STAGING_PORT)
    log = os.path.join(state, pathcfg.LOG_FILE)
    return {
        "Label": label(instance),
        "ProgramArguments": ["/usr/bin/python3", os.path.join(checkout(instance), "server.py")],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    }


def render(instance, directory=None):
    """Write the plist beside its final name, then rename it over the target."""
    directory = directory or os.path.join(pathcfg.HOME, "Library", "LaunchAgents")
    content = plist(instance)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, content["Label"] + ".plist")
    handle, scratch = tempfile.mkstemp(dir=directory, prefix=".render-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            plistlib.dump(content, out, sort_keys=False)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(scratch, 0o644)
        os.replace(scratch, target)
    except BaseException:
        if os.path.exists(scratch):
            os.unlink(scratch)
        raise
    return target


_VALUES = {"label": label, "bundle-id": bundle_id, "checkout": checkout, "state": state_dir}
FIELDS = (*_VALUES, "render")
_USAGE = (f"usage: python3 -m fleetdash.launchd {'|'.join(FIELDS)} "
          f"{'|'.join(INSTANCES)} [directory]")


def main(argv):
    if len(argv) not in (2, 3) or argv[0] not in FIELDS or (len(argv) == 3 and argv[0] != "render"):
        print(_USAGE, file=sys.stderr)
        return 2
    field, instance = argv[0], argv[1]
    try:
        if field == "render":
            value = render(instance, argv[2] if len(argv) == 3 else None)
        else:
            value = _VALUES[field](instance)
    except (ValueError, OSError) as error:
        print(f"fleetdash.launchd: {error}", file=sys.stderr)
        return 2
    print(value)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint, exercised via main()
    raise SystemExit(main(sys.argv[1:]))
