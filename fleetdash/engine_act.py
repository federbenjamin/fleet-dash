"""act(): the injection dispatcher — prompt answers, controls, text,
interrupt, focus (invariants 3-5, 18-20, 24, 65, 66, 68)."""
import json, os, re, time, contextlib, copy


from .config import cwd_to_project_dir




class ActOps:

    def act(self, action, _claude_locked=False):
        """Public entry point: bind a durable receipt, then dispatch.

        Only the OUTERMOST call owns a receipt. `act` re-enters itself under the
        per-session Claude mutation lock, and `_send_now_or_queue` forwards the
        same `client_request_id` to a nested direct send — both would otherwise
        collide with the receipt the outer call already claimed and refuse the
        very action they are performing. Depth is thread-local rather than a
        field on `action`, because every client-supplied key is untrusted.
        """
        if getattr(self._act_depth, "value", 0):
            return self._act_dispatch(action, _claude_locked)
        self._act_depth.value = 1
        try:
            receipt_id, replay = (self.begin_act_receipt(action)
                                  if isinstance(action, dict) else (None, None))
            if replay is not None:
                return replay
            try:
                result = self._act_dispatch(action, _claude_locked)
            except Exception:
                # The dispatcher raised after possibly writing keys. Uncertainty
                # is the honest record (invariant 66); the error still propagates.
                self.resolve_act_receipt(receipt_id, {"ok": False, "code": "action_raised",
                                                      "error": "the action failed partway"})
                raise
            return self.resolve_act_receipt(receipt_id, result) if receipt_id else result
        finally:
            self._act_depth.value = 0

    def _act_dispatch(self, action, _claude_locked=False):
        """Inject an answer into the owning iTerm session. action:
        {type:'option', session_id, nonce, digits:[1,..], n_options, other:'...'} |
        {type:'multiq', session_id, nonce,
         answers:[{digits:[..], multi:bool, n_options, other:'...'}, ..]} |
        {type:'dismiss', session_id, nonce}   (Esc = the TUI's "Chat about this") |
        {type:'permission', session_id, nonce, choice:'allow'|'always'|'deny'} |
        {type:'interrupt', session_id}        (Esc into a BUSY session: stop the turn) |
        {type:'close', session_id}            (stop if active, then SIGTERM Claude) |
        {type:'close_preview', session_id}    (read-only secondary-worktree safety probe) |
        {type:'worktree_cleanup', session_id, cleanup_ticket, force} |
        {type:'reopen', session_id}           (new terminal: claude --resume ID) |
        {type:'session_settings', session_id, model, effort,
         expected_model, expected_effort}     (idle-only native model/effort commands) |
        {type:'relay', session_id, agent_id, text}  (subagents have no tty: type a
                                              tagged line into the PARENT for it to
                                              forward with SendMessage) |
        {type:'text', session_id, text:'...'} |
        {type:'image_text', session_id, text:'...', upload_ids:['opaque-id']} |
        {type:'send_message', session_id, text:'...', upload_ids:['opaque-id']} |
        {type:'resume_and_send', session_id, text:'...', client_request_id:'...'} |
        {type:'dismiss_then_send', session_id, nonce, text:'...',
         upload_ids:['opaque-id']}"""
        if not isinstance(action, dict):
            return {"ok": False, "error": "action must be an object"}
        # Double-underscore fields are server-internal. A client must never be
        # able to claim that an arbitrary directory is a prepared staging worktree.
        action = {key: value for key, value in action.items()
                  if not str(key).startswith("__")}
        # Public callers identify server-owned uploads by opaque ID. Never let a
        # JSON request smuggle a local path into either direct or queued sends.
        action.pop("image_paths", None)
        staging_error = self._staging_action_error(action)
        if staging_error:
            return staging_error
        if action.get("type") == "ping":     # token check for the page's acting banner
            return {"ok": True}
        requested_type = action.get("type")
        sid = str(action.get("session_id") or "")
        if (not _claude_locked and sid and not sid.startswith("codex:") and
                requested_type in ("text", "image_text", "handoff_text",
                                   "session_settings", "permission_mode", "option",
                                   "multiq", "permission", "dismiss", "interrupt",
                                   "dismiss_then_send", "relay", "noop", "close")):
            with self._claude_mutation_lock(sid):
                # Re-enter so every registry/tail gate is freshly evaluated
                # inside the per-session critical section. The internal flag is
                # a Python argument, never a client-controlled action field.
                return self.act(action, _claude_locked=True)
        if requested_type == "image_text" or (requested_type in
                ("send_message", "dismiss_then_send") and action.get("upload_ids")):
            paths, error = self._resolve_image_uploads(
                str(action.get("session_id") or ""), action.get("upload_ids"))
            if error:
                return {"ok": False, "error": error}
            # Client-supplied paths are never accepted. Only this server-side
            # resolution can add image_paths to a provider action.
            action = {**action, "image_paths": paths}
        if requested_type == "dismiss_then_send":
            action = {**action,
                      "type": "image_text" if action.get("image_paths") else "text"}
            return self._dismiss_question_then_send(action)
        if requested_type == "send_message":
            action = {**action,
                      "type": "image_text" if action.get("image_paths") else "text"}
            return self._send_now_or_queue(action)
        if requested_type == "resume_and_send":
            return self.resume_and_send(action)
        if action.get("type") == "briefing_review":
            return self.briefing_action(action)
        if str(action.get("type") or "").startswith("outbox_"):
            return self.outbox_action(action)
        if action.get("type") == "handoff":
            return self.execute_handoff(action)
        if action.get("type") in ("git_commit", "git_push", "pr_create_draft",
                                  "pr_mark_ready"):
            return self.repository_action(action)
        if action.get("type") == "worktree_cleanup":
            return self.cleanup_closed_worktree(action)
        if str(action.get("session_id") or "").startswith("codex:") \
           and action.get("type") == "focus":
            return self.focus_codex_terminal(action)
        if str(action.get("session_id") or "").startswith("codex:"):
            sid = action.get("session_id")
            with self.lock:
                session = next((copy.deepcopy(item) for item in
                    self.snapshot_cache.get("sessions") or []
                    if item.get("session_id") == sid), None)
            if action.get("type") == "close_preview":
                return (self.close_worktree_preview(session) if session else
                        {"ok": False, "error": "session not live"})
            if action.get("type") == "close" and action.get("cleanup_ticket") and \
               not self._cleanup_ticket_matches(action.get("cleanup_ticket"), sid):
                return {"ok": False, "error": "cleanup preview expired — refresh before closing"}
            if action.get("type") in ("text", "image_text"):
                # Prefer exact App Server turn authority. A live terminal is a
                # fallback for a TUI-owned turn, not a reason to bypass the
                # post-compaction turn id delivered by the provider.
                route = (None if not session or session.get("read_only") or
                         session.get("control_state") == "connected_active" else
                         self._codex_terminal_route(self.codex.native(sid), force=True))
                if route:
                    return self._write_codex_terminal(action, route)
            if action.get("type") in ("text", "image_text") and session and \
                    (session.get("capabilities") or {}).get("queue_submit"):
                return self._queue_codex_recovery(action)
            result = self.codex.act(action)
            if (action.get("type") in ("text", "image_text") and
                    (result.get("queueable") or
                     result.get("code") == "provider_control_unavailable")):
                return self._queue_codex_recovery(action)
            if action.get("type") == "close" and result.get("ok"):
                self._mark_cleanup_ticket_closed(action.get("cleanup_ticket"), sid)
            return result
        if action.get("type") == "spawn":    # no session yet — it makes one
            if action.get("provider") == "codex":
                return self.spawn_codex_session(action)
            return self.spawn_session(action)
        sid = action.get("session_id")
        if action.get("type") == "reopen":
            return self.reopen_claude_session(sid)
        reg = next((r for r in self.live_sessions() if r.get("sessionId") == sid), None)
        if not reg:
            return {"ok": False, "error": "session not live"}
        if action.get("type") == "close_preview":
            return self.close_worktree_preview({**reg, "provider": "claude"})
        if action.get("type") == "close":
            if action.get("cleanup_ticket") and not self._cleanup_ticket_matches(
                    action.get("cleanup_ticket"), sid):
                return {"ok": False, "error": "cleanup preview expired — refresh before closing"}
            result = self._close_claude_session(reg)
            if result.get("ok"):
                self._mark_cleanup_ticket_closed(action.get("cleanup_ticket"), sid)
            return result
        path = os.path.join(cwd_to_project_dir(reg.get("cwd", "")), f"{sid}.jsonl")
        if action.get("type") in ("text", "image_text", "handoff_text", "relay",
                                  "session_settings", "permission_mode"):
            with self._claude_turn_fences_guard:
                has_turn_fence = sid in self._claude_turn_fences
            if has_turn_fence:
                with self.scan_lock:
                    fence_tail = self.tail_for(path)
                    fence_tail.poll()
                    fenced = self._claude_turn_fenced(
                        sid, reg.get("status"), path, fence_tail)
                if fenced and not (action.get("type") == "relay" and
                                   reg.get("status") in ("busy", "shell")):
                    if action.get("type") in ("text", "image_text", "handoff_text"):
                        return {"ok": False,
                                "error": "Claude is starting the previous message; queue this one",
                                "code": "provider_control_unavailable", "queueable": True}
                    return {"ok": False,
                            "error": "Claude is starting the previous message; wait for it to finish"}
        if action.get("type") == "permission_mode" and reg.get("status") != "idle":
            return {"ok": False, "error": "permission mode can change only while Claude is idle"}
        if action.get("type") == "session_settings" and reg.get("status") != "idle":
            return {"ok": False,
                    "error": "Claude model and effort can change only while Claude is idle"}
        if action.get("type") in ("text", "image_text", "handoff_text") and \
                reg.get("status") != "idle":
            # The fleet snapshot used by send-now/Outbox can age between target
            # selection and dispatch. Revalidate the authoritative registry at
            # the injection boundary so text never lands in an ask TUI or gets
            # typed into a busy turn while Fleet claims immediate delivery.
            return {"ok": False,
                    "error": "Claude is no longer available; queue the message",
                    "code": "provider_control_unavailable", "queueable": True}
        # a prompt answer may only go to a session actually blocked on a prompt —
        # a hook-blocked ask leaves a ghost pending file but the session stays
        # 'busy', and injected digits would land in its main input box
        if action.get("type") in ("option", "multiq", "permission", "dismiss") \
           and reg.get("status") != "waiting":
            return {"ok": False, "error": "session isn't waiting on a prompt — "
                    "this question may have been blocked or already answered"}
        if action.get("type") == "interrupt" and reg.get("status") not in ("busy", "shell"):
            return {"ok": False, "error": "session isn't mid-turn — nothing to interrupt"}
        # a relay is typed into the PARENT's input box: if the parent is blocked on
        # a prompt, that box is the ask TUI and the relay would answer the question
        if action.get("type") == "relay" and reg.get("status") == "waiting":
            return {"ok": False, "error": "the parent session is waiting on a prompt — "
                    "answer that first, then relay"}
        if action.get("type") == "interrupt" and reg.get("status") == "shell":
            with self.scan_lock:
                shell_tail = self.tail_for(path)
                shell_tail.poll()
                if shell_tail.turn_state() == "awaiting_input":
                    return {"ok": False, "error": "the shell command has finished — "
                            "there is no active turn to interrupt"}
        # scan_lock is held by the poll thread while it folds EVERY transcript in the
        # fleet, so taking it here makes these actions wait out a whole scan (~300ms of
        # the measured latency). Native-surface mutations need the freshness re-poll:
        # prompt answers validate their nonce, while controls, direct text/images,
        # handoffs, and relays must recheck pending/compaction state at the injection
        # boundary. Focus and interrupt do not fold the tail here.
        needs_tail = action.get("type") in (
            "option", "multiq", "permission", "dismiss", "permission_mode",
            "session_settings", "text", "image_text", "handoff_text", "relay")
        lock = self.scan_lock if needs_tail else contextlib.nullcontext()
        with lock:
            mt = self.tail_for(path)
            if needs_tail:
                mt.poll()   # NEVER poll unlocked: it would race the poll thread's
                            # fold of the same Tail and double-count its usage
            typ = action.get("type")
            control_uncertain = self._reconcile_claude_control_state(sid, mt)
            if control_uncertain and typ in ("session_settings", "permission_mode"):
                return {"ok": False, "code": "control_delivery_uncertain",
                        "error": ("the last Claude control change is unconfirmed — "
                                  "check the terminal and wait for Fleet to observe it")}
            steps = []                  # [(text, send_newline)]
            # free text typed into a TUI row must never smuggle keys: strip control
            # chars (a \r would fire as Enter, \x1b starts an escape sequence)
            clean = lambda t: re.sub(r"[\x00-\x1f\x7f]+", " ", str(t or "")).strip()[:300]
            if typ in ("text", "image_text", "handoff_text", "relay"):
                hook_request = self.hook_pending(sid, reg.get("status")) is not None
                transcript_request = bool(mt.pending) and (
                    typ != "relay" or reg.get("status") == "idle")
                input_compacting = self.compacting_secs(
                    sid, reg.get("cwd", ""), mt) is not None
            else:
                hook_request = transcript_request = input_compacting = False
            if hook_request or transcript_request or input_compacting:
                result = {"ok": False,
                          "error": "Claude is waiting or compacting; do not inject text",
                          "code": "provider_control_unavailable"}
                if typ in ("text", "image_text", "handoff_text"):
                    result["queueable"] = True
                return result
            if typ == "session_settings":
                # Both commands are composed entirely from the server catalog;
                # no client-supplied slash command or terminal key is accepted.
                if (self.hook_pending(sid, reg.get("status")) is not None or
                        bool(mt.pending)):
                    return {"ok": False,
                            "error": "answer Claude's pending request before changing settings"}
                if self.compacting_secs(sid, reg.get("cwd", ""), mt) is not None:
                    return {"ok": False,
                            "error": "Claude is compacting; wait before changing settings"}
                if "expected_model" not in action or "expected_effort" not in action:
                    return {"ok": False,
                            "error": "expected model and effort are required",
                            "code": "stale_settings"}
                catalog = {model: list(self.EFFORTS) for model in self.MODELS}
                target_model = str(action.get("model") or "").strip()
                target_effort = str(action.get("effort") or "").strip()
                if target_model not in catalog:
                    return {"ok": False, "error": "unknown Claude model"}
                if target_effort not in catalog[target_model]:
                    return {"ok": False,
                            "error": "unsupported effort level for this Claude model"}
                current_model = str(mt.model or "")
                current_effort = str(self.effort_for(sid) or "")
                if (str(action.get("expected_model") or "") != current_model or
                        str(action.get("expected_effort") or "") != current_effort):
                    return {"ok": False,
                            "error": "settings changed in another view — refresh and try again",
                            "code": "stale_settings"}
                if target_model != current_model:
                    steps.append((f"/model {target_model}", True))
                if target_effort != current_effort:
                    steps.append((f"/effort {target_effort}", True))
                if not steps:
                    return {"ok": True, "model": current_model,
                            "effort": current_effort}
            elif typ == "permission_mode":
                if (self.hook_pending(sid, reg.get("status")) is not None or mt.pending):
                    return {"ok": False,
                            "error": "answer Claude's pending request before changing permissions"}
                if self.compacting_secs(sid, reg.get("cwd", ""), mt) is not None:
                    return {"ok": False,
                            "error": "Claude is compacting; wait before changing permissions"}
                target = str(action.get("mode") or "")
                allowed = self._claude_permission_modes(reg, mt)
                current = mt.permission_mode
                if target not in ("default", "acceptEdits", "plan", "auto",
                                  "bypassPermissions"):
                    return {"ok": False, "error": "unknown Claude permission mode"}
                if current == "dontAsk":
                    return {"ok": False, "error": "Don't ask is startup-only in Claude Code; "
                            "start a new session to choose another mode"}
                if current not in allowed:
                    return {"ok": False, "error": "Claude has not reported a live permission "
                            "mode yet — send one message from the terminal first"}
                if target not in allowed:
                    if target == "auto":
                        return {"ok": False, "error": "Auto is unavailable for this running "
                                "session's model or account"}
                    if target == "bypassPermissions":
                        return {"ok": False, "error": "Bypass was not enabled when this Claude "
                                "process started"}
                    return {"ok": False, "error": "mode is unavailable for this session"}
                count = (allowed.index(target) - allowed.index(current)) % len(allowed)
                if count == 0:
                    return {"ok": True, "mode": current}
                # Shift+Tab is Claude's own documented mid-session control. The
                # cycle is server-derived; the client supplies only an allowlisted
                # target, never arbitrary keys.
                steps = [("\x1b[Z", False)] * count
            elif typ in ("option", "permission", "multiq", "dismiss"):
                nonce = action.get("nonce")
                hp = self.hook_pending(sid, reg.get("status"))
                if not ((hp and hp.get("nonce") == nonce) or nonce in mt.pending):
                    return {"ok": False, "error": "stale: the prompt changed — refresh"}
                with self._claude_delivery_uncertain_guard:
                    uncertain_nonce = self._claude_delivery_uncertain.get(sid)
                if uncertain_nonce == nonce:
                    return {"ok": False,
                            "error": ("delivery uncertain — check the Claude terminal, "
                                      "then refresh before answering again"),
                            "code": "delivery_uncertain"}
                if uncertain_nonce:
                    self._clear_claude_delivery_uncertain(sid)
                # Two devices can render the same prompt. The first accepted
                # answer fences it (invariant 75) so the second cannot type a
                # second set of digits into a TUI that already moved on.
                if self._request_answered(sid, nonce):
                    return {"ok": False, "code": "duplicate",
                            "error": "this prompt was already answered"}

                # The browser's question shape is display data, never terminal-key
                # authority. Rebuild the exact shape from the nonce-matched hook or
                # transcript row before deriving any digit/down-arrow sequence. In
                # particular, trusting client n_options here allowed an authenticated
                # request to allocate an effectively unbounded list of DOWN keys.
                questions = None
                pending_kind = None
                if hp and hp.get("nonce") == nonce and hp.get("kind") == "question":
                    pending_kind = "question"
                    questions = hp.get("questions")
                elif hp and hp.get("nonce") == nonce:
                    pending_kind = hp.get("kind")
                elif nonce in mt.pending:
                    pending_tool = mt.pending.get(nonce) or {}
                    if pending_tool.get("name") == "AskUserQuestion":
                        pending_kind = "question"
                        questions = (pending_tool.get("input") or {}).get("questions")
                    else:
                        pending_kind = "permission"

                if typ in ("option", "multiq") and pending_kind != "question":
                    return {"ok": False, "error": "this prompt is not a question"}
                if typ == "permission" and pending_kind != "permission":
                    return {"ok": False, "error": "this prompt is not a permission request"}

                # Direct evidence beats inference (invariant 77). When the
                # session is in a tmux pane Fleet can SEE which widget owns the
                # keyboard, so it no longer has to trust the registry word that
                # invariant 5 has been guessing from. A `trust` screen is the
                # sharpest case: injecting digits there would answer Claude's
                # folder-trust dialog, which Fleet must never do (invariant 21).
                screen_state = self.screen_prompt_state(reg, self._tty_for_pid(reg.get("pid")))
                screen_kind = screen_state["kind"] if screen_state else None
                if screen_kind is not None:
                    allowed_screens = ({"question", "permission"} if typ == "dismiss"
                                       else {"question"} if typ in ("option", "multiq")
                                       else {"permission"})
                    if screen_kind not in allowed_screens:
                        showing = {"question": "a different question",
                                   "permission": "a permission request",
                                   "trust": "its folder-trust dialog",
                                   "input": "its ordinary input box"}.get(
                                       screen_kind, "something else")
                        return {"ok": False, "code": "screen_mismatch",
                                "error": f"the terminal is showing {showing}, not this "
                                         "prompt — refresh before answering"}

                question_specs = []
                if typ in ("option", "multiq"):
                    if not isinstance(questions, list) or not (1 <= len(questions) <= 8):
                        return {"ok": False, "error": "question shape is unavailable or too large"}
                    for question in questions:
                        options = question.get("options") if isinstance(question, dict) else None
                        if not isinstance(options, list) or not (1 <= len(options) <= 9):
                            return {"ok": False,
                                    "error": "question options are unavailable or too large"}
                        allow_other = question.get("allowOther", True) is not False
                        if allow_other and len(options) >= 9:
                            return {"ok": False,
                                    "error": "question has too many options for its Other row"}
                        question_specs.append({
                            "n_options": len(options),
                            "multi": bool(question.get("multiSelect")),
                            "allow_other": allow_other,
                        })

                def answer_digits(raw, n_options):
                    """Parse only the TUI's one-byte digit keys, bounded by source shape."""
                    if not isinstance(raw, list) or len(raw) > n_options:
                        return None
                    parsed = []
                    for digit in raw:
                        if isinstance(digit, bool):
                            return None
                        if isinstance(digit, int):
                            value = digit
                        elif isinstance(digit, str) and re.fullmatch(r"[1-9]", digit):
                            value = ord(digit) - ord("0")
                        else:
                            return None
                        if not 1 <= value <= n_options or value in parsed:
                            return None
                        parsed.append(value)
                    return sorted(parsed)

                if typ == "dismiss":
                    # Esc anywhere in the ask TUI = "Chat about this" (sandbox-proven
                    # 2026-07-14: tool returns "User declined to answer questions")
                    steps = [("\x1b", False)]
                elif typ == "multiq":
                    answers = action.get("answers") or []
                    if not isinstance(answers, list) or len(answers) != len(question_specs):
                        return {"ok": False, "error": "answer count does not match the question"}
                    # Sandbox-proven recipes (2026-07-14, every transition captured):
                    # single-select = BARE DIGIT (instant select + advance — a separate
                    # CR write after a digit re-fires on the next view as a "phantom
                    # Enter", which corrupted 6 live rounds; digits alone don't).
                    #   with Other: digit n+1 focuses the "Type something" row, text
                    #   types into it, one CR selects + advances (clean, no phantom).
                    # multi-select = digit writes toggle (focus stays row 1), then
                    # down-arrows to the Next/Submit row (options, "Type something",
                    # then it: n_options+1 downs from row 1), then one CR — advances
                    # cleanly onto question or review. Review = bare digit 1 submits.
                    #   with Other: digit n+1 toggles the row's checkbox, DOWN×n
                    #   focuses its input, text types in, one more DOWN reaches
                    #   Next/Submit, CR.
                    DOWN = "\x1b[B"
                    steps = []
                    for a, spec in zip(answers, question_specs):
                        if not isinstance(a, dict):
                            return {"ok": False, "error": "invalid question answer"}
                        digits = answer_digits(a.get("digits") or [], spec["n_options"])
                        if digits is None:
                            return {"ok": False, "error": "invalid option selection"}
                        other = clean(a.get("other"))
                        n = spec["n_options"]
                        if not digits and not other:
                            return {"ok": False, "error": "every question needs an answer"}
                        if other and not spec["allow_other"]:
                            return {"ok": False, "error": "Other is unavailable for this question"}
                        if not spec["multi"] and len(digits) > 1:
                            return {"ok": False, "error": "choose one option for this question"}
                        if not spec["multi"] and other and digits:
                            return {"ok": False, "error": "choose an option or Other, not both"}
                        if spec["multi"]:
                            steps += [(str(d), False) for d in digits]
                            if other:
                                steps.append((str(n + 1), False))
                                steps += [(DOWN, False)] * n
                                steps.append((other, False))
                                steps.append((DOWN, False))
                            else:
                                steps += [(DOWN, False)] * (n + 1)
                            steps.append(("", True))
                        elif other:
                            steps.append((str(n + 1), False))
                            steps.append((other, False))
                            steps.append(("", True))
                        else:
                            steps.append((str(digits[0]), False))
                    steps.append(("1", False))
                elif typ == "option":
                    if len(question_specs) != 1:
                        return {"ok": False, "error": "answer all questions together"}
                    spec = question_specs[0]
                    parsed_digits = answer_digits(action.get("digits") or [], spec["n_options"])
                    if parsed_digits is None:
                        return {"ok": False, "error": "invalid option selection"}
                    digits = [str(d) for d in parsed_digits]
                    other = clean(action.get("other"))
                    n = spec["n_options"]
                    if not digits and not other:
                        return {"ok": False, "error": "no option chosen"}
                    if other and not spec["allow_other"]:
                        return {"ok": False, "error": "Other is unavailable for this question"}
                    if not spec["multi"] and len(digits) > 1:
                        return {"ok": False, "error": "choose one option for this question"}
                    if not spec["multi"] and other and digits:
                        return {"ok": False, "error": "choose an option or Other, not both"}
                    if spec["multi"]:
                        steps = [(d, False) for d in digits]
                        if other:
                            # Other rides the Submit ROW path (goes through the
                            # Review pane; trailing 1 submits it) — sandbox-proven
                            steps.append((str(n + 1), False))
                            steps += [("\x1b[B", False)] * n
                            steps.append((other, False))
                            steps.append(("\x1b[B", False))
                            steps.append(("", True))
                            steps.append(("1", False))
                        else:
                            # digits toggle; Enter toggles too. Submitting = right-
                            # arrow to the "✔ Submit" TAB + Enter (skips Review).
                            steps.append(("\x1b[C", False))
                            steps.append(("", True))
                    elif other:
                        steps = [(str(n + 1), False), (other, False), ("", True)]
                    else:
                        steps = [(d, False) for d in digits]
                        steps.append(("", True))
                else:
                    pk = self.cfg.get("permission_keys", {})
                    choice = action.get("choice")
                    if choice not in pk:
                        return {"ok": False, "error": "unknown choice"}
                    if choice == "always":
                        # "always allow" is the one choice whose key is NOT fixed.
                        # Three captured variants put a persistent grant on row 2
                        # and a fourth — a Bash command Claude cannot statically
                        # analyze — puts "No" there, so the hardwired "2" DENIED
                        # instead of granting (v2.1.220, 2026-07-25). Row 1 (yes)
                        # and Esc (deny) are correct on every variant; this one
                        # has to be read off the screen or not sent at all.
                        # No evidence is a refusal, not a fallback: a session
                        # outside tmux has no pane to read, and guessing there is
                        # what produced the bug. `allow` and `deny` still work.
                        always = (screen_state or {}).get("always")
                        if not always:
                            return {"ok": False, "code": "always_unavailable",
                                    "error": "this prompt offers no always-allow option"
                                    if screen_state else
                                    "Fleet cannot read this terminal, so it will not guess "
                                    "which key grants access — use allow or deny"}
                        key = str(always[0])
                    else:
                        # an empty key means Esc (deny cancels any prompt variant)
                        key = pk[choice] or "\x1b"
                    # ONE key, never a trailing CR (invariant 4's phantom Enter).
                    # A bare digit instant-selects on a permission prompt exactly
                    # as it does on a single-select ask — verified live against
                    # 2.1.220 on all three captured variants. The CR this used to
                    # append was redundant, and it fired ~0.4s later into whatever
                    # had mounted by then: when Claude raises a SECOND permission
                    # prompt (routine — one request often needs several), Enter
                    # confirms its highlighted row 1 and silently answers "Yes".
                    steps = [(key, False)]
            elif typ == "focus":        # bring that session's iTerm tab to the front
                steps = [("__FOCUS__", False)]
            elif typ == "interrupt":    # Esc mid-turn = the terminal's stop key
                steps = [("\x1b", False)]
            elif typ == "noop":         # TCC/AppleScript path probe: delivers nothing
                steps = [("", False)]
            elif typ == "relay":
                # A subagent has NO tty — the only channel to it is the parent
                # calling SendMessage. So a "message to a subagent" is a tagged
                # line typed into the PARENT's input box; the parent forwards it.
                # Delivery is the parent's call, never guaranteed by us.
                jl, meta_path = self._agent_paths(sid, action.get("agent_id"))
                if not jl:
                    return {"ok": False, "error": "no such subagent"}
                body = re.sub(r"[\x00-\x1f\x7f]+", " ", str(action.get("text", ""))).strip()[:1500]
                if not body:
                    return {"ok": False, "error": "empty text"}
                try:
                    with open(meta_path) as meta_handle:
                        desc = (json.load(meta_handle) or {}).get("description", "")
                except Exception:
                    desc = ""
                aid = action.get("agent_id")
                steps = [(f"[fleet-dash relay to subagent {aid}"
                          f"{f' — “{desc}”' if desc else ''}] {body} "
                          f"(forward it with SendMessage; if that agent can't be "
                          f"resumed, say so instead of acting on this yourself)", True)]
            elif typ in ("text", "image_text", "handoff_text"):
                limit = 30_000 if typ == "handoff_text" else 2000
                txt = str(action.get("text", ""))[:limit].strip()
                if typ == "image_text":
                    paths = action.get("image_paths") or []
                    if not paths:
                        return {"ok": False, "error": "no images"}
                    txt = txt or ("Please inspect the attached image." if len(paths) == 1 else
                                  "Please inspect the attached images.")
                    txt += "\n\nImages attached through Fleet:\n" + "\n".join(
                        f"- {path}" for path in paths)
                if not txt:
                    return {"ok": False, "error": "empty text"}
                # a leading "/" opens the TUI's OWN command popup, where Enter fires
                # the HIGHLIGHTED entry — not necessarily what was typed. A space
                # closes that popup, so the CR submits the literal text
                # (sandbox-proven 2026-07-14: "/status" + CR ran the highlighted
                # match; "/status " + CR submitted the text with no popup open).
                if txt.startswith("/") and " " not in txt:
                    txt += " "
                steps = [(txt, True)]
            else:
                return {"ok": False, "error": "unknown action type"}
        # The 0.4s inter-key delay is load-bearing ONLY for the ask-TUI key sequences
        # (digits/arrows/CR need a render between them, or keys get dropped — invariant
        # 4). Typing a message or focusing a tab is one or two keys with nothing to
        # re-render, so those wait 0.05s and the click stops feeling laggy.
        fast = typ in ("text", "image_text", "handoff_text", "relay", "focus", "interrupt", "noop")
        step_delay = 0.05 if fast else 0.4
        turn_fence_baseline = None
        if typ in ("text", "image_text", "handoff_text", "relay"):
            try:
                transcript_size = os.path.getsize(path)
            except OSError:
                transcript_size = None
            turn_fence_baseline = {"transcript_size": transcript_size,
                                   "convo_rev": getattr(mt, "convo_rev", None)}
        background_claude = self._is_background_claude(reg)
        tty = None
        if not background_claude:
            tty = self._tty_for_pid(reg["pid"])     # a pid's tty never changes
            if not tty:
                return {"ok": False, "error": "session has no terminal (VS Code / headless)"}

        # The drive allowlist (operator decision 2026-07-24): closed-loop
        # delivery may pace and verify keys for Claude's ask selector and its
        # tool-permission prompt, and nothing else. Every other action keeps the
        # fixed inter-key delay, so an unobserved surface is never driven.
        expect_surface = ({"option": "question", "multiq": "question",
                           "permission": "permission"}.get(typ))

        def native_write(write_steps):
            if background_claude:
                return (self._focus_background_claude(reg) if typ == "focus" else
                        self._write_background_claude(reg, write_steps, step_delay))
            return self._terminal_write(f"/dev/{tty}", write_steps,
                                        step_delay=step_delay,
                                        expect=expect_surface)

        if typ == "session_settings" and len(steps) > 1:
            # `/model` and `/effort` are separate Claude commands, not one
            # transaction. A single mailbox request hid partial acceptance when
            # the first command landed and the second failed. Acknowledge each
            # write separately and project the exact accepted prefix.
            applied_model, applied_effort = current_model, current_effort
            result = None
            for index, step in enumerate(steps):
                result = native_write([step])
                if not result.get("ok"):
                    failed_field = "model" if step[0].startswith("/model ") else "effort"
                    ambiguous = not self._native_write_failed_before_delivery(result)
                    uncertain_warning = (self._mark_claude_control_uncertain(
                        sid, mt, [failed_field]) if ambiguous else None)
                    if applied_model != current_model or applied_effort != current_effort:
                        accepted = {}
                        if applied_model != current_model:
                            accepted["model"] = applied_model
                        if applied_effort != current_effort:
                            accepted["effort"] = applied_effort
                        durable_warning = self._record_claude_control_overrides(
                            sid, mt, accepted)
                        with self.scan_lock:
                            accepted_tail = self.tail_for(path)
                            accepted_tail.model = applied_model
                        detail = str(result.get("error") or "the second command failed")[:300]
                        warning = ("Claude applied part of the change; the remaining "
                                   f"command {'is unconfirmed' if ambiguous else 'failed'}: "
                                   f"{detail}")
                        if durable_warning or uncertain_warning:
                            warning += "; recovery state could not be saved durably"
                        return {"ok": True, "model": applied_model,
                                "effort": applied_effort, "partial": True,
                                "control_delivery_uncertain": ambiguous,
                                "warning": warning}
                    if not ambiguous:
                        return result
                    return {**result, "ok": False,
                            "code": "control_delivery_uncertain",
                            "error": ("Claude may have applied the control change, but Fleet "
                                      "lost the result; check the terminal" +
                                      ("; recovery state is not durable"
                                       if uncertain_warning else ""))}
                if step[0].startswith("/model "):
                    applied_model = target_model
                elif step[0].startswith("/effort "):
                    applied_effort = target_effort
                if index + 1 < len(steps):
                    time.sleep(0.4)
        elif typ == "permission_mode" and len(steps) > 1:
            applied_mode = current
            result = None
            current_index = allowed.index(current)
            for index, step in enumerate(steps):
                result = native_write([step])
                if not result.get("ok"):
                    ambiguous = not self._native_write_failed_before_delivery(result)
                    uncertain_warning = (self._mark_claude_control_uncertain(
                        sid, mt, ["permission_mode"]) if ambiguous else None)
                    if applied_mode != current:
                        durable_warning = self._record_claude_control_overrides(
                            sid, mt, {"permission_mode": applied_mode})
                        with self.scan_lock:
                            self.tail_for(path).permission_mode = applied_mode
                        detail = str(result.get("error") or "the next cycle key failed")[:300]
                        warning = ("Claude changed permission mode partway; the remaining "
                                   f"cycle key {'is unconfirmed' if ambiguous else 'failed'}: "
                                   f"{detail}")
                        if durable_warning or uncertain_warning:
                            warning += "; recovery state could not be saved durably"
                        return {"ok": True, "mode": applied_mode, "partial": True,
                                "control_delivery_uncertain": ambiguous,
                                "warning": warning}
                    if not ambiguous:
                        return result
                    return {**result, "ok": False,
                            "code": "control_delivery_uncertain",
                            "error": ("Claude may have changed permission mode, but Fleet "
                                      "lost the result; check the terminal" +
                                      ("; recovery state is not durable"
                                       if uncertain_warning else ""))}
                applied_mode = allowed[(current_index + index + 1) % len(allowed)]
                if index + 1 < len(steps):
                    time.sleep(0.4)
        else:
            result = native_write(steps)
        if (not result.get("ok") and
                not self._native_write_failed_before_delivery(result) and
                typ in ("option", "multiq", "permission", "dismiss")):
            durable_warning = self._set_claude_delivery_uncertain(
                sid, str(action.get("nonce") or ""))
            result = {**result, "ok": False, "code": "delivery_uncertain",
                      "error": ("delivery uncertain — some terminal keys may have landed; "
                                "check the Claude terminal, then refresh" +
                                ("; retry protection could not be saved durably"
                                 if durable_warning else ""))}
        if (typ in ("option", "multiq", "permission", "dismiss") and
                (result.get("ok") or not
                 self._native_write_failed_before_delivery(result))):
            # Accepted, or possibly accepted: either way no other device may
            # answer this prompt again (invariant 75).
            self._record_answered_request(sid, str(action.get("nonce") or ""))
        if (not result.get("ok") and
                not self._native_write_failed_before_delivery(result) and
                typ in ("session_settings", "permission_mode")):
            failed_field = ("permission_mode" if typ == "permission_mode" else
                            "model" if steps[0][0].startswith("/model ") else "effort")
            durable_warning = self._mark_claude_control_uncertain(
                sid, mt, [failed_field])
            result = {**result, "ok": False, "code": "control_delivery_uncertain",
                      "error": ("Claude may have applied the control change, but Fleet lost "
                                "the result; check the terminal" +
                                ("; recovery state could not be saved durably"
                                 if durable_warning else ""))}
        if typ == "permission_mode" and result.get("ok"):
            # Claude may defer its transcript marker until the next prompt. Keep
            # Fleet's state responsive; the next native row remains authoritative.
            with self.scan_lock:
                self.tail_for(path).permission_mode = target
            result["mode"] = target
            durable_warning = self._record_claude_control_overrides(
                sid, mt, {"permission_mode": target})
            if durable_warning:
                result["warning"] = (result.get("warning", "") +
                    " Applied, but restart recovery state could not be saved.").strip()
        elif typ == "session_settings" and result.get("ok"):
            # The action reached Claude's idle native command parser using only
            # allowlisted values. Reflect that accepted selection immediately;
            # later transcript/statusline rows can supersede it.
            with self.scan_lock:
                self.tail_for(path).model = target_model
            accepted = {}
            if target_model != current_model:
                accepted["model"] = target_model
            if target_effort != current_effort:
                accepted["effort"] = target_effort
            durable_warning = self._record_claude_control_overrides(sid, mt, accepted)
            result.update(model=target_model, effort=target_effort)
            if durable_warning:
                result["warning"] = (result.get("warning", "") +
                    " Applied, but restart recovery state could not be saved.").strip()
        if result.get("ok") and turn_fence_baseline is not None:
            self._record_claude_turn_fence(sid, turn_fence_baseline)
        if result.get("ok") and typ == "interrupt":
            self._claude_interrupted[sid] = getattr(mt, "convo_rev", None)
        return result

