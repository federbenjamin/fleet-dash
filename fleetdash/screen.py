"""Classify what a captured Claude TUI screen is showing (invariant 77).

Every marker below was read off a real `capture-pane -p` frame from Claude Code
v2.1.219 in a disposable tmux sandbox on 2026-07-24 — not remembered, not
inferred from the transcript. The footer line is the discriminator, because the
option list looks nearly identical across all three modal surfaces:

    question    ❯ 1. Red …  4. Type something.  5. Chat about this
                Enter to select · ↑/↓ to navigate · Esc to cancel
    permission  Do you want to create .session-unlock?
                ❯ 1. Yes  2. Yes, and allow …  3. No
                Esc to cancel · Tab to amend
    trust       ❯ 1. Yes, I trust this folder   2. No, exit
                Enter to confirm · Esc to cancel
    input       ─────  ❯   ─────   (an empty prompt between two rules)

This module is pure text in, one label out. It never captures anything itself,
so it cannot be the thing that puts a subprocess on a hot path.
"""

# Bottom-anchored: only the tail of a pane describes what it is asking for now.
TAIL_LINES = 40

QUESTION = "question"
PERMISSION = "permission"
TRUST = "trust"
INPUT = "input"
UNKNOWN = "unknown"

# Footer markers, most specific first. Each is a fixed substring of a real frame.
_FOOTERS = (
    ("Enter to select", QUESTION),          # AskUserQuestion selector
    ("Tab to amend", PERMISSION),           # tool permission prompt
    ("Enter to confirm", TRUST),            # folder-trust dialog
)
# Corroborating body markers. A footer alone is enough; these catch a frame whose
# footer scrolled off, and they disambiguate trust from any other confirm dialog.
_TRUST_BODY = "I trust this folder"
_QUESTION_BODY = "Chat about this"          # the ask TUI's n+2 row, unique to it
_PERMISSION_BODY = "Do you want to"


def _tail(lines):
    rows = [str(line or "") for line in (lines or [])]
    return rows[-TAIL_LINES:]


def classify_screen(lines):
    """Return what the terminal is showing: question/permission/trust/input/unknown.

    `unknown` is a real answer and the common one — a mid-turn screen, a scrolled
    transcript, a resized pane. Callers must treat it as "no evidence", never as
    "no prompt".
    """
    rows = _tail(lines)
    text = "\n".join(rows)
    if _TRUST_BODY in text:
        return TRUST
    for marker, label in _FOOTERS:
        if marker in text:
            # A permission prompt and an ask both end in "Esc to cancel"; the
            # markers above are exclusive, so first match wins deterministically.
            return label
    if _QUESTION_BODY in text:
        return QUESTION
    if _PERMISSION_BODY in text and any(row.lstrip().startswith("❯ 1.") for row in rows):
        return PERMISSION
    if _idle_input(rows):
        return INPUT
    return UNKNOWN


def _idle_input(rows):
    """True when the tail holds Claude's empty main input box.

    The box is a lone `❯` between two rule lines. Requiring the rule above it is
    what separates an empty prompt from a selector's highlighted first row, which
    is also `❯` but carries text.
    """
    for index, row in enumerate(rows):
        if row.strip() != "❯":
            continue
        above = rows[index - 1] if index else ""
        if above.strip().startswith("─"):
            return True
    return False
