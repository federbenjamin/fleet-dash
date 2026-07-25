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
    compacting  ✻ Compacting conversation…
                ▰▰▱▱▱▱▱… 5%
    input       ─────  ❯   ─────   (an empty prompt between two rules)

The compacting frames were captured the same way on **v2.1.220, 2026-07-25** —
46 samples of a real `/compact`, about 40 of which carry that line. Its leading
glyph rotates (`·` `✽` `✻` `✢` `✳` `✶`), so the marker deliberately starts after
it. That surface is not modal: the empty input box is still on screen beneath
it, which is exactly why `compacting` has to be tested BEFORE `input`.

This module is pure text in, one label out. It never captures anything itself,
so it cannot be the thing that puts a subprocess on a hot path.
"""

import re

# Bottom-anchored: only the tail of a pane describes what it is asking for now.
TAIL_LINES = 40

QUESTION = "question"
PERMISSION = "permission"
TRUST = "trust"
COMPACTING = "compacting"
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
# The spinner glyph in front of this rotates, so match from the word onward.
_COMPACTING_BODY = "Compacting conversation"


def _tail(lines):
    rows = [str(line or "") for line in (lines or [])]
    return rows[-TAIL_LINES:]


def classify_screen(lines):
    """Return what the terminal is showing: question/permission/trust/compacting/
    input/unknown.

    `unknown` is a real answer and the common one — a mid-turn screen, a scrolled
    transcript, a resized pane. Callers must treat it as "no evidence", never as
    "no prompt".

    Modal surfaces are tested first because they own the keyboard. `compacting`
    is not modal — it renders above a live input box — so it must be settled
    before `input`, or a compacting pane would report itself as idle.
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
    if _COMPACTING_BODY in text:
        return COMPACTING
    if _idle_input(rows):
        return INPUT
    return UNKNOWN


_OPTION_ROW = re.compile(r"^[❯>\s]*([1-9])\.\s+(.+?)\s*$")


def prompt_options(lines, limit=9, width=160):
    """The numbered option rows a modal prompt is rendering, in order.

    Only useful for telling the user what row 2 ACTUALLY grants. Every captured
    permission variant puts Yes/always/No in rows 1/2/3, but row 2's wording —
    and its real scope — differs sharply between them: a Bash prompt offers a
    project-wide directory grant, a Read prompt a session-only read, an Overwrite
    prompt a settings edit. A fixed "always allow" label describes all three and
    is honest about none.

    Returns [] when the tail holds no option list. Text is bounded and stripped
    of control characters by the caller's capture; treat it as untrusted display
    data and escape it at the render site.
    """
    found = {}
    for row in _tail(lines):
        match = _OPTION_ROW.match(row)
        if not match:
            continue
        # LAST sighting wins. A pane can still hold an answered prompt's rows
        # above the live one, and the live one is always nearer the bottom;
        # rows are read top-down, so a later index overwrites an earlier one.
        found[int(match.group(1))] = match.group(2)[:width]
    return [found[key] for key in sorted(found) if key <= limit]


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
