"""Bounded provider-neutral conversation rows for session-card peeks."""
import json
import os


# One change point for the row types eligible for session-card peeks. Keep the
# provider-normalized role names so filtering never depends on browser markup.
CARD_PEEK_TYPES = frozenset(("user", "assistant", "tool", "event"))
CARD_PEEK_MAX_ROWS = 16
CARD_PEEK_MAX_CHARS = 3200

_TOOL_LABELS = {
    "commandExecution": "Bash",
    "fileChange": "Edit",
    "collabAgentToolCall": "Agent",
    "webSearch": "Search",
    "imageView": "View image",
    "imageGeneration": "Generate image",
}
_FILE_TOOLS = frozenset((
    "Edit", "MultiEdit", "Write", "NotebookEdit", "Read", "View image",
))


def _text(value):
    return str(value or "").strip()


def _first_line(value):
    return next((line.strip() for line in str(value or "").splitlines()
                 if line.strip()), "")


def _file_names(files):
    names = []
    for item in files or []:
        value = item.get("name") if isinstance(item, dict) else item
        if value:
            names.append(os.path.basename(str(value)))
    return names


def _structured_tool_value(value):
    """Extract one useful scalar from structured Codex/MCP tool arguments."""
    if not isinstance(value, (dict, list)):
        try:
            value = json.loads(str(value or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, dict):
            continue
        for key in ("file_path", "notebook_path", "path", "description",
                    "command", "prompt", "query", "url"):
            if item.get(key) not in (None, ""):
                return _first_line(item[key])
    return ""


def _tool_summary(row, label):
    names = _file_names(row.get("files"))
    if names:
        return names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")
    raw = row.get("peek_arg") or row.get("arg") or row.get("command") or ""
    structured = _structured_tool_value(raw)
    summary = structured or _first_line(raw)
    if label in _FILE_TOOLS and summary:
        summary = os.path.basename(summary)
    return summary


def _event_detail(row):
    if row.get("kind") != "qa":
        return _text(row.get("detail"))
    answers = []
    for item in (row.get("qa") or [])[:8]:
        if not isinstance(item, dict):
            continue
        question = _text(item.get("q") or item.get("question"))
        answer = _text(item.get("a") or item.get("answer")) or "—"
        header = _text(item.get("header"))
        prompt = " · ".join(part for part in (header, question) if part)
        answers.append(f"{prompt}: {answer}" if prompt else answer)
    return "\n".join(answers)


def _normalized_row(row):
    role = _text(row.get("role"))
    if role in ("user", "assistant"):
        value = _text(row.get("text"))
        return {"type": role, "text": value} if value else None
    if role == "tool":
        raw_name = _text(row.get("name")) or "tool"
        label = _TOOL_LABELS.get(raw_name, raw_name)
        return {
            "type": "tool",
            "label": label[:160],
            "text": _tool_summary(row, label),
            "failed": bool(row.get("failed")),
        }
    if role == "event":
        try:
            count = max(1, int(row.get("n") or 1))
        except (TypeError, ValueError, OverflowError):
            count = 1
        return {
            "type": "event",
            "kind": (_text(row.get("kind")) or "event")[:80],
            "label": (_text(row.get("title")) or "Event")[:240],
            "text": _event_detail(row),
            "level": (_text(row.get("level")) or "info")[:40],
            "n": count,
        }
    return None


def _clip_row(row, remaining):
    """Keep display text beginnings within one aggregate character budget."""
    used = 0
    for key in ("label", "text"):
        value = row.get(key)
        if not value:
            continue
        room = max(0, remaining - used)
        if len(value) > room:
            row[key] = value[:max(0, room - 1)] + ("…" if room else "")
        used += len(row.get(key) or "")
    return row, used


def card_peek_rows(messages, include=None, max_rows=CARD_PEEK_MAX_ROWS,
                   max_chars=CARD_PEEK_MAX_CHARS):
    """Return recent display rows, oldest-to-newest, within strict bounds.

    Selection walks newest-first so a long newest message keeps its beginning
    (the browser clips it to the first visible N lines). Older rows fill any
    remaining budget and are then restored to conversation order.
    """
    allowed = CARD_PEEK_TYPES if include is None else frozenset(include)
    selected = []
    remaining = max(0, int(max_chars))
    for raw in reversed(list(messages or [])):
        if len(selected) >= max(0, int(max_rows)) or remaining <= 0:
            break
        if not isinstance(raw, dict) or _text(raw.get("role")) not in allowed:
            continue
        row = _normalized_row(raw)
        if row is None:
            continue
        row, used = _clip_row(row, remaining)
        if used <= 0:
            break
        selected.append(row)
        remaining -= used
    selected.reverse()
    return selected
