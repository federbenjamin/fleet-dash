"""Instance path resolution: the single patch point for filesystem roots.

Every module reads these as attributes (`paths.BASE`), never via
`from .paths import BASE`, so tests patch `fleetdash.paths.<NAME>` once and
every consumer sees the override.
"""
import os

HOME = os.path.expanduser("~")
PRODUCTION_BASE = os.path.join(HOME, ".claude", "fleet-dash-prod-state")
BASE = os.path.abspath(os.path.expanduser(
    os.environ.get("FLEET_DASH_STATE_DIR") or PRODUCTION_BASE))
INSTANCE_MODE = str(os.environ.get("FLEET_DASH_INSTANCE") or "production").strip().lower()
if INSTANCE_MODE not in ("production", "staging"):
    INSTANCE_MODE = "production"
DEFAULT_CAPTURE_BASE = os.path.join(HOME, ".claude", "fleet-dash-capture")
CAPTURE_BASE = os.path.abspath(os.path.expanduser(
    os.environ.get("FLEET_DASH_CAPTURE_DIR") or DEFAULT_CAPTURE_BASE))
PROJECTS = os.path.join(HOME, ".claude", "projects")
SESSIONS = os.path.join(HOME, ".claude", "sessions")
CLAUDE_ACCOUNT = os.path.join(HOME, ".claude.json")
CLAUDE_USAGE = os.path.join(CAPTURE_BASE, "usage.json")
CLAUDE_STATS = os.path.join(HOME, ".claude", "stats-cache.json")
CLAUDE_HISTORY = os.path.join(HOME, ".claude", "history.jsonl")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CLAUDE_USAGE_PREFS = os.path.join(
    HOME, "Library", "Preferences", "HamedElfayome.Claude-Usage.plist")


def capture_base():
    """Shared hook/statusline artifacts (pending/, effort/, usage.json).

    Always the shared capture dir — matching where the hooks actually write —
    so a bare `python3 server.py` run sees captures too. Read at call time so
    tests patching fleetdash.paths.CAPTURE_BASE take effect."""
    return CAPTURE_BASE
