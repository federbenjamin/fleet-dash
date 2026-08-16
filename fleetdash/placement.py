"""Session placement: Now-queue classification and reply-request detection (invariant 31)."""
import hashlib
import re
from .config import DEFAULT_CONFIG, EXTERNAL_VIEW_ONLY_REPLY_GRACE_SECONDS

_RHETORICAL_TAIL = re.compile(
    r"(?i)^(?:so\s+)?(?:does|do|is|are|did|was|were)?\s*(?:that|this|it|they|the\s+\w+)?\s*"
    r"(?:make[s]?\s+sense|sound[s]?\s+(?:good|right|ok(?:ay)?)|"
    r"look[s]?\s+(?:good|right|ok(?:ay)?)|work\s+for\s+you|help|clear|correct)\s*\??$")
_TAG_TAIL = re.compile(
    r"(?i)^(?:right|correct|okay|ok|cool|clear|got it|make sense|sound good|"
    r"fair enough|you follow|you with me)\s*\??$")
# Interrogative-led comprehension checks: "how does that look?", "what do you
# think?", "how's that?" — distinct from a genuine "how do you want to proceed?".
_RHETORICAL_HOW = re.compile(
    r"(?i)^(?:how\s+(?:does|do|is|'?s)\s+(?:that|this|it|the\b[^?]*?)?\s*"
    r"(?:look|sound|read|seem|feel)|how'?s\s+(?:that|this|it)|"
    r"what\s+do\s+you\s+(?:think|reckon)|how\s+do\s+you\s+like)\b")
# Idle/greeting prompts and open "what would you like" solicitations are not a
# blocked turn — the assistant is inviting a new task, not waiting on a decision.
_GREETING_TAIL = re.compile(
    r"(?i)(what are we (working on|doing)|what'?s next|what'?s on your mind|"
    r"what can i help|what would you like|how can i help|what are you (after|actually)|"
    r"what did you want|how'?s it going|anything else|what'?s the (plan|goal))")
# A genuine ask: offers the user a choice, or asks the assistant's OWN next
# action (permission/direction to act). Caught in addition to bare interrogatives
# so routine "Want me to X?" / "A or B?" endings register as attention.
_ACTION_QUESTION = re.compile(
    r"(?i)\b(?:should i|shall i|should we|shall we|can i|may i|want me to|"
    r"do you want|would you like|would you prefer|do you prefer|which|whether|"
    r"either|\bor\b)\b")


def requests_reply(text):
    """Conservative plain-prose signal that an assistant explicitly wants input.

    Provider-native questions remain authoritative. This covers ordinary completed
    assistant messages, whose protocols do not carry a requires-reply field.
    Code, Markdown quotations, and quoted strings are removed before detection so
    examples such as `value?` do not manufacture attention work. Comprehension
    tags ("does that make sense?", "how does that look?") and idle greeting
    prompts are excluded; every remaining genuine ask — a forced choice, a
    "should I proceed" decision, or a routine "want me to X?" — registers.
    """
    prose = str(text or "")
    if not prose.strip():
        return False
    prose = re.sub(r"```[\s\S]*?```", " ", prose)
    prose = re.sub(r"`[^`\n]*`", " ", prose)
    prose = re.sub(r"(?m)^\s*>.*$", " ", prose)
    prose = re.sub(r'"[^"\n]*"|“[^”\n]*”|\'[^\'\n]*\'|‘[^’\n]*’', " ", prose)
    explicit = bool(re.search(
        r"(?i)\b(answer|choose|confirm|pick|reply|respond|select|tell me|let me know)\b"
        r"[^.!?\n]{0,100}(?:before (?:i|we) continue|which|whether|one|option|both|these)",
        prose))
    if explicit:
        return True
    # Only the FINAL prose question can be actionable — a mid-message question the
    # assistant then answers itself is not attention work.
    stripped = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", prose).strip()
    match = re.search(r"([^.!?\n]*(?:\n[^.!?\n]*)*)\?\s*$", stripped)
    if not match:
        return False
    question = re.sub(r"\s+", " ", match.group(1)).strip(" -*0123456789.)\t")
    # Drop comprehension tags and idle greetings — validated as the dominant
    # false-positive classes against the local transcript corpus.
    if (_RHETORICAL_TAIL.match(question) or _TAG_TAIL.match(question)
            or _RHETORICAL_HOW.match(question) or _GREETING_TAIL.search(question)):
        return False
    # Genuine ask: a forced choice or a request for the assistant's next action.
    if _ACTION_QUESTION.search(question):
        return True
    # Or any bare interrogative-led final question ("Should I proceed?").
    return bool(re.match(
        r"(?i)^(?:what|which|who|when|where|why|how|do|does|did|is|are|was|were|"
        r"can|could|would|will|should|may|must|have|has|had)\b", question))


def completed_handoff(text):
    """True only for an explicit completed-work report, not progress prose.

    A completed turn alone is not enough: interrupted turns can end with a recent
    assistant row, and routine prose can be substantial without being a handoff.
    Require both an unambiguous completion claim and concrete handoff evidence.
    """
    prose = str(text or "")
    if not prose.strip():
        return False
    prose = re.sub(r"```[\s\S]*?```", " ", prose)
    prose = re.sub(r"(?m)^\s*>.*$", " ", prose)
    completion = re.search(
        r"(?im)^\s{0,3}(?:#{1,6}\s*)?(?:done|completed|finished|implemented|"
        r"fixed|resolved|shipped)\b|\b(?:implementation|work|task|changes?)\s+"
        r"(?:is|are|has been|have been)\s+(?:complete|completed|done|implemented|fixed)\b",
        prose)
    if not completion:
        return False
    evidence = prose[completion.end():]
    return bool(re.search(
        r"(?im)^\s*(?:[-*+]\s+|#{1,6}\s+)\S|\b(?:tests?|verified|validation|"
        r"changed|updated|added|removed|files?|summary|details?)\b", evidence))


PRIMARY_ACTION_LABELS = {"respond": "Respond", "review": "Review", "open": "Open",
                         "continue": "Continue", "view": "View", "reopen": "Reopen"}
ACCESS_LABELS = {"interactive": "Interactive", "view_only": "View only",
                 "reopen": "Reopen"}


def _fact_text(value, limit=220):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + "…"


def _pending_placement(pending):
    kind = (pending or {}).get("kind")
    if kind == "question":
        return "Question waiting", "respond", "placement.pending.question"
    if kind == "elicitation":
        return "Form waiting", "respond", "placement.pending.form"
    if kind == "permission":
        approval = (pending or {}).get("approval_kind") or (pending or {}).get("tool")
        if approval == "command":
            return "Command approval", "review", "placement.pending.command_approval"
        if approval == "file_change":
            return "File approval", "review", "placement.pending.file_approval"
        return "Permission needed", "review", "placement.pending.permission"
    return None


def action_identity(session, kind, revision):
    """Return the stable server-owned identity for one attention item."""
    raw = "\0".join((str(session.get("provider") or "claude"),
                      str(session.get("session_id") or ""), str(kind or ""),
                      str(revision or "")))
    return "act-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def attention_action(session, reply_requested=False):
    """Describe the exact unresolved item that can own Needs You placement."""
    pending = session.get("pending") or {}
    revision = str(session.get("convo_v") or "")
    if pending:
        kind = {"question": "question", "elicitation": "form",
                "permission": "approval"}.get(pending.get("kind"))
        if kind:
            return kind, str(pending.get("nonce") or revision)
    if reply_requested:
        return "reply", revision
    raw_state = str(session.get("state") or "idle")
    state = str(session.get("stale_previous_state") or "idle") \
        if raw_state == "stale" else raw_state
    if state in ("blocked", "error", "stalled_or_prompt"):
        detail = str(session.get("error") or "").strip()[:600]
        return "problem", "\0".join((revision, state, detail))
    if state == "needs_you":
        return "attention", revision
    return None


def classify_placement(session, now, reply_available=None, read_sessions=None,
                       dormant_seconds=None, dismissed_actions=None):
    """Pure provider-neutral session placement with diagnostic evidence."""
    reply_available = reply_available or {}
    read_sessions = read_sessions or {}
    dismissed_actions = dismissed_actions or {}
    if dormant_seconds is None:
        dormant_seconds = DEFAULT_CONFIG["dormant_seconds"]
    dormant_seconds = max(0, float(dormant_seconds))
    sid = str(session.get("session_id") or "")
    revision = str(session.get("convo_v") or "")
    latest = session.get("_latest_prose") or {}
    latest_assistant = latest if latest.get("role") == "assistant" else None
    raw_state = str(session.get("state") or "idle")
    state = str(session.get("stale_previous_state") or "idle") \
        if raw_state == "stale" else raw_state
    pending = session.get("pending") or {}
    capabilities = session.get("capabilities") or {}
    external = bool(session.get("headless") or session.get("read_only"))
    provider_stale = raw_state == "stale" or bool(session.get("stale"))
    quiet = max(0, round(float(session.get("quiet_s") or 0)))
    external_dormant = external and (
        state == "dormant" or quiet > dormant_seconds)
    dismissed = str(reply_available.get(sid, ""))
    read_revision = str(read_sessions.get(sid, ""))
    interrupted = bool(session.get("interrupted"))
    reply_requested = bool(
        not interrupted and latest_assistant and requests_reply(latest_assistant.get("text"))
        and dismissed != revision)
    external_reply_expired = bool(
        reply_requested and external and not pending
        and str(session.get("reg_status") or "").lower() == "notloaded"
        and state in ("idle", "turn_done", "dormant")
        and quiet >= EXTERNAL_VIEW_ONLY_REPLY_GRACE_SECONDS)
    if external_reply_expired:
        reply_requested = False

    unresolved = attention_action(session, reply_requested)
    attention_id = action_identity(session, *unresolved) if unresolved else None
    attention_dismissed = bool(attention_id and attention_id in dismissed_actions)
    stale_inactive = bool(
        provider_stale and str(session.get("reg_status") or "").lower() == "notloaded"
        and quiet > dormant_seconds and not session.get("agents_running")
        and session.get("compacting") is None and state not in ("running", "stalled"))
    inactive_dormant = bool(
        (state == "dormant" or stale_inactive) and not session.get("agents_running")
        and session.get("compacting") is None)

    candidates = []
    if inactive_dormant:
        candidates.append(("placement.access.external" if external else
                           "placement.state.dormant",
                           "history", "External" if external else "Inactive",
                           "view" if external else "continue", "inferred"))
    if attention_dismissed and not inactive_dormant:
        candidates.append(("placement.attention.dismissed", "available", "Available",
                           "view" if external else "continue", "confirmed"))
    pending_rule = _pending_placement(pending)
    if pending_rule and not attention_dismissed:
        reason, primary, rule = pending_rule
        candidates.append((rule, "needs_you", reason, primary, "confirmed"))
    if state == "error" and not attention_dismissed:
        candidates.append(("placement.provider.error", "needs_you", "Fix needed",
                           "open", "confirmed"))
    if state == "blocked" and not attention_dismissed:
        candidates.append(("placement.provider.limit", "needs_you", "Limit reached",
                           "open", "confirmed"))
    if state == "stalled_or_prompt" and not attention_dismissed:
        candidates.append(("placement.state.stalled_or_prompt", "needs_you",
                           "Check session", "open", "inferred"))
    if state == "needs_you" and not attention_dismissed:
        candidates.append(("placement.state.needs_you", "needs_you",
                           "Response needed", "respond", "confirmed"))
    if session.get("compacting") is not None:
        candidates.append(("placement.runtime.compacting", "working", "Compacting",
                           "open", "confirmed"))
    if state == "stalled":
        candidates.append(("placement.state.stalled", "working", "Slow",
                           "open", "inferred"))
    if state == "running":
        candidates.append(("placement.state.running", "working", "Working",
                           "view" if external else "open", "confirmed"))
    if external_reply_expired:
        candidates.append(("placement.external.reply_request_expired", "history", "External",
                           "view", "confirmed"))
    if reply_requested and not attention_dismissed:
        candidates.append(("placement.prose.reply_requested", "needs_you",
                           "Reply requested", "respond", "inferred"))
    if state == "turn_done":
        candidates.append(("placement.state.turn_done", "available", "Available",
                           "view" if external else "continue", "confirmed"))
    if external_dormant and not inactive_dormant:
        candidates.append(("placement.access.external", "history", "External",
                           "view", "confirmed"))
    if state == "reopenable":
        candidates.append(("placement.state.reopenable", "history", "Reopenable",
                           "reopen" if capabilities.get("reopen") else "view", "confirmed"))
    if state == "dormant" and not inactive_dormant:
        candidates.append(("placement.state.dormant", "history", "Inactive",
                           "continue", "inferred"))
    default_confidence = ("unknown" if state not in
                          {"idle", "turn_done", "running", "stalled", "needs_you",
                           "stalled_or_prompt", "dormant", "reopenable"} else "confirmed")
    candidates.append(("placement.default.available", "available", "Available",
                       "continue", default_confidence))

    winning_rule, group, reason, primary, confidence = candidates[0]
    if external:
        access = "view_only"
        if primary in ("respond", "review", "open", "continue"):
            primary = "view"
    elif primary == "reopen":
        access = "reopen"
    else:
        access = "interactive"
    if provider_stale:
        confidence = "stale"

    evidence = [{"kind": "provider_signal", "label": "Provider signal",
                 "value": _fact_text(
                     f"{session.get('provider') or 'claude'} state {raw_state}"
                     + (f"; CLI status {session.get('reg_status')}"
                        if session.get("reg_status") is not None else "")),
                 "confidence": "stale" if provider_stale else "confirmed"}]
    if pending:
        evidence.append({"kind": "pending_request", "label": "Pending work",
                         "value": _fact_text(
                             f"{pending.get('kind') or 'request'}"
                             + (f" · {pending.get('approval_kind') or pending.get('tool')}"
                                if pending.get("approval_kind") or pending.get("tool") else "")),
                         "confidence": "confirmed"})
    if latest:
        evidence.append({"kind": "transcript_event", "label": "Latest transcript event",
                         "value": _fact_text(
                             f"{latest.get('role') or 'unknown'} message at revision {revision or 'unknown'}"
                             + ("; direct reply requested" if reply_requested else "")),
                         "confidence": "inferred" if reply_requested else "confirmed"})
    if session.get("agents_running"):
        evidence.append({"kind": "active_work", "label": "Active work",
                         "value": f"{int(session.get('agents_running') or 0)} subagent(s) running",
                         "confidence": "confirmed"})
    if session.get("compacting") is not None:
        evidence.append({"kind": "active_work", "label": "Active work",
                         "value": f"Compaction active for {round(float(session.get('compacting') or 0))}s",
                         "confidence": "confirmed"})
    evidence.append({"kind": "age", "label": "Last activity",
                     "value": f"{quiet}s quiet", "confidence": "confirmed"})
    if external:
        evidence.append({"kind": "access", "label": "Control",
                         "value": _fact_text(session.get("read_only_reason") or
                                             "Owned by another runtime; Fleet can only view it"),
                         "confidence": "confirmed"})
    if external_reply_expired:
        evidence.append({"kind": "reply_request_expired", "label": "Reply request",
                         "value": "Cleared after 1800s: external view-only thread is not loaded",
                         "confidence": "confirmed"})
    if provider_stale:
        evidence.append({"kind": "stale", "label": "Freshness",
                         "value": _fact_text(session.get("stale_reason") or session.get("error") or
                                             "Showing the last good provider snapshot"),
                         "confidence": "stale"})

    new_response = bool(
        not interrupted and group == "available" and state == "turn_done" and latest_assistant
        and completed_handoff(latest_assistant.get("text")) and read_revision != revision)
    return {
        "state": state, "ui_group": group, "reason_label": reason,
        "primary_action": primary, "primary_action_label": PRIMARY_ACTION_LABELS[primary],
        "access": access, "access_label": ACCESS_LABELS[access],
        "external": external, "provider_stale": provider_stale,
        "reply_requested": reply_requested, "new_response": new_response,
        "unresolved_attention": bool(unresolved),
        "attention_dismissed": attention_dismissed,
        "attention_action_id": attention_id,
        "activity_at": max(0, float(now) - float(session.get("quiet_s") or 0)),
        "winning_rule": winning_rule,
        "suppressed_rules": [candidate[0] for candidate in candidates[1:]],
        "state_confidence": confidence, "state_evidence": evidence,
    }


def classify_closed_placement(session):
    """Pure placement for a ledger session whose provider process is gone."""
    can_reopen = bool(session.get("can_reopen"))
    primary = "reopen" if can_reopen else "view"
    access = "reopen" if can_reopen else "view_only"
    closed_at = float(session.get("closed_at") or 0)
    return {
        "state": "closed", "ui_group": "history", "reason_label": "Closed",
        "primary_action": primary, "primary_action_label": PRIMARY_ACTION_LABELS[primary],
        "access": access, "access_label": ACCESS_LABELS[access],
        "external": False, "provider_stale": False, "reply_requested": False,
        "new_response": False,
        "activity_at": session.get("last_seen") or closed_at,
        "winning_rule": "placement.ledger.closed", "suppressed_rules": [],
        "state_confidence": "confirmed",
        "state_evidence": [
            {"kind": "ledger", "label": "Provider process",
             "value": "No live provider process is registered", "confidence": "confirmed"},
            {"kind": "access", "label": "Conversation access",
             "value": ("Transcript can reopen in a new Claude terminal" if can_reopen else
                       "Conversation is retained for viewing only"), "confidence": "confirmed"},
        ],
    }


def redact_handoff_text(value, limit=30_000):
    """Bound and remove common credential shapes from generated/user-edited handoffs."""
    text = str(value or "").replace("\x00", "")[:limit]
    patterns = (
        (r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----[\s\S]*?"
         r"-----END(?: [A-Z0-9]+)* PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED AWS ACCESS KEY]"),
        (r"(?i)\b(?:sk|sk-ant|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{12,}\b",
         "[REDACTED CREDENTIAL]"),
        (r"(?i)\bauthorization\b\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+",
         "Authorization: [REDACTED]"),
        (r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
         r"password|secret)\b\s*[:=]\s*[^\s,;]+", "[REDACTED CREDENTIAL]"),
        (r"(?i)([?&](?:token|key|secret|auth)=)[^&#\s]+", r"\1[REDACTED]"),
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", "Bearer [REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text.strip()

# built-in commands the TUI offers (name, description). Skills + custom commands
