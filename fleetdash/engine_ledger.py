"""Spend ledger, session/agent finalization, closed-session history and
context, handoff links (invariants 15, 23, 32)."""
import json, os, re, sys, glob, time, sqlite3, hashlib, copy, uuid, shutil, tempfile


from . import paths as pathcfg
from .config import IMG_EXTS, model_family, cwd_to_project_dir, iso_epoch
from .card_preview import card_peek_rows
from .placement import _fact_text, redact_handoff_text




class LedgerOps:

    @staticmethod
    def _prepare_ledger(path):
        """Quarantine only a confirmed-corrupt shared ledger so Fleet can start.

        The corrupt bytes are preserved for manual recovery. Starting empty is
        safer than restoring an old backup that could resend already-delivered
        outbox messages.
        """
        if not os.path.exists(path):
            return {"ok": True, "recovered": False}
        db = None
        try:
            db = sqlite3.connect(path, timeout=2)
            result = db.execute("PRAGMA quick_check").fetchone()
            if result and result[0] == "ok":
                return {"ok": True, "recovered": False}
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return {"ok": True, "recovered": False, "check_deferred": True}
        except sqlite3.DatabaseError:
            pass
        finally:
            if db is not None:
                db.close()
        quarantine = path + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        try:
            for suffix in ("", "-wal", "-shm"):
                source = path + suffix
                if os.path.exists(source):
                    os.replace(source, quarantine + suffix)
        except OSError as exc:
            raise RuntimeError(f"ledger is corrupt and could not be quarantined: {exc}")
        return {"ok": False, "recovered": True,
                "error": "Fleet's local ledger was corrupt. It was preserved and a clean ledger was started; Session History, Outbox, budgets, and local usage totals may be incomplete.",
                "quarantine": os.path.basename(quarantine)}

    # -------------------------------------------------------------- ledger
    def ensure_db(self):
        with self.db_lock:
            if self.db is not None:
                return self.db
            # This long-lived connection belongs to the sequential scan loop.
            # HTTP request threads use their own short-lived connections below.
            self.db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), check_same_thread=False)
            self.db.execute("""CREATE TABLE IF NOT EXISTS agent_runs(
                agent_id TEXT PRIMARY KEY, session_id TEXT, project TEXT,
                agent_type TEXT, model TEXT, description TEXT,
                in_tok INT, cw_tok INT, cr_tok INT, out_tok INT, cost REAL,
                started TEXT, ended TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS session_runs(
                session_id TEXT PRIMARY KEY, name TEXT, project TEXT, cwd TEXT,
                branch TEXT, model TEXT, cost REAL, agent_cost REAL, agents_total INT,
                bridge_url TEXT, first_seen INT, last_seen INT, closed_at INT)""")
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN title TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN provider TEXT DEFAULT 'claude'")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN transcript_path TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE session_runs ADD COLUMN status_line_json TEXT")
            except sqlite3.OperationalError:
                pass
            # rows are CUMULATIVE per transcript (path) — see Tail.stats
            self.db.execute("""CREATE TABLE IF NOT EXISTS usage_stats(
                path TEXT, day TEXT, kind TEXT, name TEXT,
                uses INT, chars INT, t_in INT, t_cw INT, t_cr INT, t_out INT, fam TEXT,
                PRIMARY KEY(path, day, kind, name))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS state_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, provider TEXT NOT NULL, at REAL NOT NULL,
                raw_state TEXT, reg_status TEXT, normalized_state TEXT, ui_group TEXT,
                reason TEXT, access TEXT, primary_action TEXT,
                evidence_kind TEXT, evidence_summary TEXT, revision TEXT,
                winning_rule TEXT, suppressed_rules TEXT, confidence TEXT,
                evidence_json TEXT, signature TEXT NOT NULL)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS state_events_session_id
                ON state_events(session_id, id DESC)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS session_links(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_session_id TEXT NOT NULL, source_provider TEXT NOT NULL,
                destination_session_id TEXT NOT NULL UNIQUE,
                destination_provider TEXT NOT NULL, created_at REAL NOT NULL,
                status TEXT NOT NULL, error TEXT, preview_hash TEXT NOT NULL)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS session_links_source
                ON session_links(source_session_id, id DESC)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS repo_actions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, root TEXT NOT NULL, worktree TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                summary TEXT, error TEXT, revision TEXT)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS repo_actions_root
                ON repo_actions(root, id DESC)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS delivered_artifacts(
                file_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                source_agent_id TEXT, tool_id TEXT NOT NULL, ordinal INT NOT NULL,
                source_path TEXT NOT NULL, name TEXT NOT NULL, caption TEXT,
                delivered_at TEXT, blob_name TEXT, size INT, kind TEXT NOT NULL,
                missing INT NOT NULL DEFAULT 0, transcript_path TEXT)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS delivered_artifacts_session
                ON delivered_artifacts(session_id)""")
            self.db.commit()
            return self.db

    def ledger_reader(self):
        """Open a request-local ledger connection.

        Sharing the scan loop's sqlite connection with ThreadingHTTPServer can
        leave the connection poisoned after overlapping reads and writes even
        when the on-disk database passes quick_check. Request handlers therefore
        get an independent connection and close it after the response is built.
        """
        self.ensure_db()
        db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _safe_claude_transcript(sid, path):
        """Return a canonical top-level Claude transcript path or None.

        Session IDs and paths ultimately reach both a file reader and a terminal
        command. Require the exact UUID filename directly beneath one project
        directory; subagent files and symlinks escaping ~/.claude/projects fail.
        """
        if not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", str(sid or "")):
            return None
        if not path:
            return None
        real = os.path.realpath(os.path.expanduser(str(path)))
        root = os.path.realpath(pathcfg.PROJECTS)
        if os.path.dirname(os.path.dirname(real)) != root:
            return None
        if os.path.basename(real) != f"{sid}.jsonl" or not os.path.isfile(real):
            return None
        return real

    @staticmethod
    def _safe_reopen_cwd(cwd):
        if not cwd:
            return None
        real = os.path.realpath(os.path.expanduser(str(cwd)))
        home = os.path.realpath(pathcfg.HOME)
        if not os.path.isdir(real):
            return None
        if real != home and not real.startswith(home + os.sep):
            return None
        return real

    @staticmethod
    def _edge_json_objects(path, head_bytes=131_072, tail_bytes=262_144):
        """Decode bounded head/tail records without loading a large transcript."""
        size = os.path.getsize(path)
        chunks = []
        with open(path, "rb") as handle:
            if size <= head_bytes + tail_bytes:
                chunks.append(handle.read())
            else:
                head = handle.read(head_bytes)
                if b"\n" in head:
                    head = head[:head.rfind(b"\n") + 1]
                chunks.append(head)
                handle.seek(size - tail_bytes)
                tail = handle.read()
                if b"\n" in tail:
                    tail = tail[tail.find(b"\n") + 1:]
                chunks.append(tail)
        out = []
        for chunk in chunks:
            for raw in chunk.splitlines():
                if not raw or len(raw) > 4_000_000:
                    continue
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    out.append(value)
        return out

    @staticmethod
    def _history_titles():
        """Latest prompt-history label/cwd per Claude session, without prompt bodies."""
        out = {}
        try:
            handle = open(pathcfg.CLAUDE_HISTORY, errors="replace")
        except OSError:
            return out
        with handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                sid = str(row.get("sessionId") or "")
                if not re.fullmatch(
                        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", sid):
                    continue
                out[sid] = {"title": str(row.get("display") or "").strip()[:160],
                            "cwd": str(row.get("project") or ""),
                            "timestamp": float(row.get("timestamp") or 0) / 1000}
        return out

    def _claude_transcript_metadata(self, path, history=None):
        sid = os.path.splitext(os.path.basename(path))[0]
        history = history or {}
        objects = self._edge_json_objects(path)
        cwd = str(history.get("cwd") or "")
        branch = model = ai_title = custom_title = first_prompt = ""
        first_epoch = None
        for row in objects:
            cwd = str(row.get("cwd") or cwd)
            branch = str(row.get("gitBranch") or branch)
            typ = row.get("type")
            if typ == "ai-title":
                ai_title = str(row.get("aiTitle") or ai_title).strip()
            elif typ == "custom-title":
                custom_title = str(row.get("customTitle") or custom_title).strip()
            ts = iso_epoch(row.get("timestamp"))
            if ts is not None and first_epoch is None:
                first_epoch = ts
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                model = str(message.get("model") or model)
            elif message.get("role") == "user" and not row.get("isMeta") \
                    and not first_prompt:
                content = message.get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        str(block.get("text") or "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
                text = re.sub(r"<system-reminder>.*?</system-reminder>", " ",
                              str(content or ""), flags=re.S).strip()
                if text and not text.startswith(("<command-", "<local-command")):
                    first_prompt = re.sub(r"\s+", " ", text)[:160]
        stat = os.stat(path)
        created = first_epoch or getattr(stat, "st_birthtime", stat.st_ctime)
        last = max(stat.st_mtime, float(history.get("timestamp") or 0))
        title = (custom_title or ai_title or history.get("title") or first_prompt or
                 os.path.basename(cwd) or "Claude session")
        return {"session_id": sid, "name": title[:160], "title": title[:160],
                "cwd": cwd, "project": os.path.basename(cwd) or
                os.path.basename(os.path.dirname(path)), "branch": branch,
                "model": model, "first_seen": int(created), "last_seen": int(last),
                "closed_at": int(last), "transcript_path": path}

    def backfill_claude_history(self):
        """Index every resumable top-level Claude transcript exactly once per path."""
        db = self.ensure_db()
        known = dict(db.execute(
            "SELECT session_id, transcript_path FROM session_runs WHERE provider='claude'"
        ).fetchall())
        history = self._history_titles()
        imported = 0
        for candidate in sorted(glob.glob(os.path.join(pathcfg.PROJECTS, "*", "*.jsonl"))):
            sid = os.path.splitext(os.path.basename(candidate))[0]
            path = self._safe_claude_transcript(sid, candidate)
            if not path or known.get(sid) == path:
                continue
            meta = self._claude_transcript_metadata(path, history.get(sid))
            db.execute("""INSERT INTO session_runs(session_id,name,project,cwd,branch,
                model,cost,agent_cost,agents_total,bridge_url,first_seen,last_seen,
                closed_at,title,provider,transcript_path)
                VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,?, 'claude',?)
                ON CONFLICT(session_id) DO UPDATE SET
                transcript_path=excluded.transcript_path,
                cwd=CASE WHEN session_runs.cwd IS NULL OR session_runs.cwd=''
                         THEN excluded.cwd ELSE session_runs.cwd END,
                project=CASE WHEN session_runs.project IS NULL OR session_runs.project=''
                             THEN excluded.project ELSE session_runs.project END,
                branch=CASE WHEN session_runs.branch IS NULL OR session_runs.branch=''
                            THEN excluded.branch ELSE session_runs.branch END,
                model=CASE WHEN session_runs.model IS NULL OR session_runs.model=''
                           THEN excluded.model ELSE session_runs.model END,
                title=CASE WHEN session_runs.title IS NULL OR session_runs.title=''
                           THEN excluded.title ELSE session_runs.title END,
                name=CASE WHEN session_runs.name IS NULL OR session_runs.name=''
                          THEN excluded.name ELSE session_runs.name END,
                provider='claude'""",
                (sid, meta["name"], meta["project"], meta["cwd"], meta["branch"],
                 meta["model"], meta["first_seen"], meta["last_seen"],
                 meta["closed_at"], meta["title"], path))
            known[sid] = path
            imported += 1
        db.commit()
        return imported

    def drain_stats(self, t):
        """Flush a tail's dirty usage stats. INSERT OR REPLACE of cumulative
        counts keeps restarts idempotent (never switch this to additive)."""
        if not t.stats_dirty:
            return
        try:
            db = self.ensure_db()
            fam = model_family(t.model)
            db.executemany(
                "INSERT OR REPLACE INTO usage_stats VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(t.path, k[0], k[1], k[2], *t.stats[k], fam) for k in t.stats_dirty])
            t.stats_dirty.clear()
            db.commit()
        except Exception as e:
            print(f"usage stats error: {e}", file=sys.stderr, flush=True)

    def ledger_finalize(self, subdir, agent_id, meta, t):
        # subdir = .../projects/<proj>/<sid>/subagents
        sid = os.path.basename(os.path.dirname(subdir))
        proj = os.path.basename(os.path.dirname(os.path.dirname(subdir)))
        db = self.ensure_db()
        db.execute("""INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(agent_id) DO UPDATE SET
                      in_tok=excluded.in_tok, cw_tok=excluded.cw_tok,
                      cr_tok=excluded.cr_tok, out_tok=excluded.out_tok,
                      cost=excluded.cost, ended=excluded.ended""",
                   (agent_id, sid, proj, meta.get("agentType", "?"), t.model,
                    (meta.get("description") or "")[:200], t.ti, t.tw, t.tr, t.to,
                    t.cost(self.cfg), t.first_ts, t.last_ts))
        db.commit()

    def record_sessions(self, sessions, now):
        try:
            db = self.ensure_db()
            dirty = False
            for s in sessions:
                status_line_json = json.dumps(
                    s.get("status_line") or {}, separators=(",", ":"))
                if len(status_line_json) > 100_000:
                    status_line_json = None
                status_signature = dict(s.get("status_line") or {})
                status_signature.pop("git_observed_at", None)
                signature = (
                    s.get("name"), s.get("project"), s.get("cwd"), s.get("branch"),
                    s.get("model"), s.get("cost"), s.get("agent_cost"),
                    s.get("agents_total"), s.get("bridge_url"), s.get("title"),
                    s.get("provider", "claude"), json.dumps(
                        status_signature, separators=(",", ":")))
                sid = s["session_id"]
                due = now - self._session_ledger_written_at.get(sid, 0) >= 30
                if self._session_ledger_signatures.get(sid) == signature and not due:
                    continue
                db.execute("""INSERT INTO session_runs(session_id,name,project,cwd,branch,
                    model,cost,agent_cost,agents_total,bridge_url,first_seen,last_seen,
                    closed_at,title,provider,transcript_path,status_line_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                    name=excluded.name, project=excluded.project, branch=excluded.branch,
                    model=excluded.model, cost=excluded.cost, agent_cost=excluded.agent_cost,
                    agents_total=excluded.agents_total, bridge_url=excluded.bridge_url,
                    last_seen=excluded.last_seen, closed_at=NULL, title=excluded.title,
                    provider=excluded.provider,
                    transcript_path=COALESCE(excluded.transcript_path,
                                             session_runs.transcript_path),
                    status_line_json=COALESCE(excluded.status_line_json,
                                              session_runs.status_line_json)""",
                    (s["session_id"], s["name"], s["project"], s["cwd"], s["branch"],
                     s["model"], s["cost"], s["agent_cost"], s["agents_total"],
                     s["bridge_url"], int(now), int(now), s["title"],
                     s.get("provider", "claude"),
                     (os.path.join(cwd_to_project_dir(s.get("cwd") or ""),
                                   f"{s['session_id']}.jsonl")
                     if s.get("provider", "claude") == "claude" else None),
                     status_line_json))
                self._session_ledger_signatures[sid] = signature
                self._session_ledger_written_at[sid] = now
                dirty = True
            live = {s["session_id"] for s in sessions}
            if self._session_ledger_live_ids is None or live != self._session_ledger_live_ids:
                ordered = sorted(live)
                marks = ",".join("?" * len(ordered)) or "''"
                db.execute(f"""UPDATE session_runs SET closed_at=?
                               WHERE closed_at IS NULL AND session_id NOT IN ({marks})""",
                           [int(now)] + ordered)
                self._session_ledger_live_ids = live
                dirty = True
            for sid in set(self._session_ledger_signatures) - live:
                self._session_ledger_signatures.pop(sid, None)
                self._session_ledger_written_at.pop(sid, None)
            if dirty:
                db.commit()
        except Exception as e:
            print(f"session ledger error: {e}", file=sys.stderr, flush=True)

    @staticmethod
    def _state_event_record(session, now):
        evidence = []
        for fact in (session.get("state_evidence") or [])[:12]:
            if not isinstance(fact, dict):
                continue
            evidence.append({
                "kind": _fact_text(fact.get("kind"), 40),
                "label": _fact_text(fact.get("label"), 80),
                "value": _fact_text(fact.get("value"), 220),
                "confidence": (_fact_text(fact.get("confidence"), 20) or "unknown"),
            })
        summary = "; ".join(
            f"{fact['label']}: {fact['value']}" for fact in evidence)[:1200]
        pending = session.get("pending") or {}
        stable = {
            "raw_state": str(session.get("state") or "unknown"),
            "reg_status": str(session.get("reg_status") or ""),
            "normalized_state": str(session.get("normalized_state") or
                                    session.get("state") or "unknown"),
            "ui_group": str(session.get("ui_group") or "history"),
            "reason": str(session.get("reason_label") or ""),
            "access": str(session.get("access") or "view_only"),
            "primary_action": str(session.get("primary_action") or "view"),
            "winning_rule": str(session.get("winning_rule") or "placement.unknown"),
            "suppressed_rules": list(session.get("suppressed_rules") or [])[:20],
            "confidence": str(session.get("state_confidence") or "unknown"),
            "pending_nonce": str(pending.get("nonce") or ""),
            "reply_requested": bool(session.get("reply_requested")),
            "new_response": bool(session.get("new_response")),
            "provider_stale": bool(session.get("provider_stale")),
        }
        signature = hashlib.sha256(json.dumps(
            stable, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            **stable, "session_id": str(session.get("session_id") or ""),
            "provider": str(session.get("provider") or "claude"), "at": float(now),
            "evidence_kind": evidence[0]["kind"] if evidence else "unknown",
            "evidence_summary": summary, "evidence": evidence,
            "revision": str(session.get("convo_v") or session.get("closed_at") or ""),
            "signature": signature,
        }

    def record_state_events(self, sessions, now):
        """Persist only meaningful consecutive placement changes."""
        try:
            db = self.ensure_db()
            if self._state_event_signatures is None:
                rows = db.execute("""SELECT event.session_id,event.signature,
                                             event.normalized_state
                    FROM state_events event JOIN (
                        SELECT session_id,MAX(id) AS latest_id FROM state_events
                        GROUP BY session_id
                    ) latest ON latest.latest_id=event.id""").fetchall()
                self._state_event_signatures = {
                    sid: (signature, normalized_state)
                    for sid, signature, normalized_state in rows}
            dirty = False
            for session in sessions:
                sid = str(session.get("session_id") or "")
                previous = self._state_event_signatures.get(sid)
                # Closed ledger rows are immutable. Once their close transition
                # is journaled, skip them before rebuilding evidence/hashes on
                # every two-second live poll.
                if (session.get("normalized_state") == "closed" and previous
                        and previous[1] == "closed"):
                    continue
                record = self._state_event_record(session, now)
                if not record["session_id"]:
                    continue
                previous = self._state_event_signatures.get(record["session_id"])
                if previous and previous[0] == record["signature"]:
                    continue
                db.execute("""INSERT INTO state_events(
                    session_id,provider,at,raw_state,reg_status,normalized_state,ui_group,
                    reason,access,primary_action,evidence_kind,evidence_summary,revision,
                    winning_rule,suppressed_rules,confidence,evidence_json,signature)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    record["session_id"], record["provider"], record["at"],
                    record["raw_state"], record["reg_status"], record["normalized_state"],
                    record["ui_group"], record["reason"], record["access"],
                    record["primary_action"], record["evidence_kind"],
                    record["evidence_summary"], record["revision"],
                    record["winning_rule"], json.dumps(record["suppressed_rules"]),
                    record["confidence"], json.dumps(record["evidence"], separators=(",", ":")),
                    record["signature"]))
                self._state_event_signatures[record["session_id"]] = (
                    record["signature"], record["normalized_state"])
                dirty = True
            if dirty:
                db.commit()
        except Exception as exc:
            print(f"state journal error: {exc}", file=sys.stderr, flush=True)

    def state_history(self, sid, cursor=0, limit=40):
        sid = str(sid or "")
        if not sid or len(sid) > 300 or any(ord(char) < 32 for char in sid):
            return {"ok": False, "error": "invalid session id"}
        try:
            cursor = int(cursor or 0)
            limit = int(limit or 40)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid evidence cursor or limit"}
        if cursor < 0 or not 1 <= limit <= 100:
            return {"ok": False, "error": "evidence limit must be 1–100"}
        cols = ("id", "session_id", "provider", "at", "raw_state", "reg_status",
                "normalized_state", "ui_group", "reason", "access", "primary_action",
                "evidence_kind", "evidence_summary", "revision", "winning_rule",
                "suppressed_rules", "confidence", "evidence_json")
        where = "session_id=?" + (" AND id<?" if cursor else "")
        params = [sid] + ([cursor] if cursor else []) + [limit + 1]
        try:
            db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=2)
            rows = db.execute(
                f"SELECT {','.join(cols)} FROM state_events WHERE {where} "
                "ORDER BY id DESC LIMIT ?", params).fetchall()
            db.close()
        except Exception as exc:
            return {"ok": False, "error": f"state history unavailable: {exc}"}
        more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            event = dict(zip(cols, row))
            try:
                event["suppressed_rules"] = json.loads(event["suppressed_rules"] or "[]")
            except ValueError:
                event["suppressed_rules"] = []
            try:
                event["evidence"] = json.loads(event.pop("evidence_json") or "[]")
            except ValueError:
                event["evidence"] = []
            events.append(event)
        with self.lock:
            current = next((item for item in
                            list(self.snapshot_cache.get("sessions") or []) +
                            list(self.snapshot_cache.get("closed") or [])
                            if item.get("session_id") == sid), None)
            if current:
                current = {key: current.get(key) for key in (
                    "session_id", "provider", "state", "normalized_state", "reg_status",
                    "ui_group", "reason_label", "access", "access_label",
                    "primary_action", "primary_action_label", "winning_rule",
                    "suppressed_rules", "state_confidence", "state_evidence",
                    "provider_stale", "activity_at", "convo_v")}
        return {"ok": True, "session_id": sid, "current": current,
                "events": events,
                "next_cursor": events[-1]["id"] if more and events else None}

    def _record_handoff_link(self, source_sid, source_provider, destination_sid,
                             destination_provider, status, preview_hash, error=None):
        """Persist identity/status only. The edited handoff body is never retained."""
        now = time.time()
        self.ensure_db()
        db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=2)
        try:
            db.execute("""INSERT INTO session_links(
                source_session_id,source_provider,destination_session_id,
                destination_provider,created_at,status,error,preview_hash)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(destination_session_id) DO UPDATE SET
                  status=excluded.status,error=excluded.error,
                  preview_hash=excluded.preview_hash""", (
                str(source_sid), str(source_provider), str(destination_sid),
                str(destination_provider), now, str(status),
                str(error)[:1000] if error else None, str(preview_hash)))
            db.commit()
        finally:
            db.close()
        with self.lock:
            self._handoff_links_version += 1
            self._handoff_links_cache = None

    def _handoff_link(self, source_sid, destination_sid):
        self.ensure_db()
        db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=2)
        try:
            row = db.execute("""SELECT source_session_id,source_provider,
                destination_session_id,destination_provider,created_at,status,error,preview_hash
                FROM session_links WHERE source_session_id=? AND destination_session_id=?""",
                (str(source_sid), str(destination_sid))).fetchone()
        finally:
            db.close()
        if not row:
            return None
        keys = ("source_session_id", "source_provider", "destination_session_id",
                "destination_provider", "created_at", "status", "error", "preview_hash")
        return dict(zip(keys, row))

    def handoff_link_map(self, session_ids):
        ids = {str(sid) for sid in session_ids if sid}
        if not ids:
            return {}
        with self.lock:
            cached = self._handoff_links_cache
            version = self._handoff_links_version
        if cached and cached[0] == version:
            rows = cached[1]
        else:
            self.ensure_db()
            db = sqlite3.connect(os.path.join(pathcfg.BASE, "ledger.db"), timeout=2)
            try:
                rows = db.execute("""SELECT source_session_id,source_provider,
                    destination_session_id,destination_provider,created_at,status,error
                    FROM session_links ORDER BY id DESC""").fetchall()
            finally:
                db.close()
            with self.lock:
                if version == self._handoff_links_version:
                    self._handoff_links_cache = (version, rows)
        out = {sid: [] for sid in ids}
        for source_sid, source_provider, destination_sid, destination_provider, created_at, status, error in rows:
            if source_sid in ids:
                out[source_sid].append({"direction": "from", "session_id": destination_sid,
                    "provider": destination_provider, "status": status,
                    "created_at": created_at, "error": error})
            if destination_sid in ids:
                out[destination_sid].append({"direction": "to", "session_id": source_sid,
                    "provider": source_provider, "status": status,
                    "created_at": created_at, "error": error})
        return {sid: links for sid, links in out.items() if links}

    def _find_handoff_source(self, sid):
        sid = str(sid or "")
        if not sid or len(sid) > 300 or any(ord(char) < 32 for char in sid):
            return None
        with self.lock:
            records = (list(self.snapshot_cache.get("sessions") or []) +
                       list(self.snapshot_cache.get("closed") or []))
        return next((dict(item) for item in records
                     if str(item.get("session_id") or "") == sid), None)

    @staticmethod
    def _handoff_artifact_section(artifacts):
        if not artifacts:
            return "[Selected artifact references]\n(none)\n[End artifact references]"
        lines = ["[Selected artifact references]"]
        for artifact in artifacts[:20]:
            caption = str(artifact.get("caption") or "").strip()
            suffix = f" — {caption}" if caption else ""
            lines.append(f"- {artifact.get('path')}{suffix}")
        lines.append("[End artifact references]")
        return "\n".join(lines)

    def handoff_preview(self, sid, target_provider):
        target = str(target_provider or "").strip().lower()
        if target not in ("claude", "codex"):
            return {"ok": False, "error": "provider must be claude or codex"}
        source = self._find_handoff_source(sid)
        if not source:
            return {"ok": False, "error": "session is unavailable"}
        source_provider = str(source.get("provider") or "claude")
        closed = bool(source.get("closed_at") is not None or
                      source.get("normalized_state") == "closed")
        context = self.closed_context(sid) if closed else self.session_context(sid)
        indexed = None
        search = getattr(self, "search", None)
        if search and hasattr(search, "handoff_material"):
            try:
                candidate = search.handoff_material(sid, 8)
                if candidate.get("ok"):
                    indexed = candidate
            except Exception as exc:
                print(f"handoff index fallback for {sid}: {exc}", file=sys.stderr,
                      flush=True)
        messages = (indexed or {}).get("recent") or (context.get("messages") or [])[-8:]
        objective = str((indexed or {}).get("first_user") or "").strip()
        if not objective:
            objective = next((str(item.get("text") or "").strip()
                              for item in (context.get("messages") or [])
                              if item.get("role") == "user" and item.get("text")), "")
        objective = objective or source.get("title") or source.get("project") or "Continue the work"
        recent = []
        for item in messages[-8:]:
            role = str(item.get("role") or "event")
            text = str(item.get("text") or item.get("detail") or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            recent.append(f"{role.title()}: {text[:4000]}")
        unresolved = []
        pending = source.get("pending") or {}
        if pending.get("kind") == "question":
            unresolved.extend(str(q.get("question") or q.get("header") or "").strip()
                               for q in (pending.get("questions") or []))
        elif pending:
            unresolved.append(str(pending.get("input_summary") or
                                  pending.get("tool") or pending.get("kind") or ""))
        if source.get("error"):
            unresolved.append(str(source.get("error")))
        unresolved.extend((indexed or {}).get("todos") or [])
        if source.get("reply_requested"):
            unresolved.append("The source session requested a user reply.")
        artifacts, seen = [], set()
        candidates = list((indexed or {}).get("artifacts") or [])
        candidates.extend(context.get("files") or [])
        for item in candidates:
            path = os.path.realpath(os.path.expanduser(str(item.get("path") or "")))
            if not path or path in seen or not path.startswith(os.path.realpath(pathcfg.HOME) + os.sep):
                continue
            seen.add(path)
            artifacts.append({"path": path, "name": os.path.basename(path),
                              "caption": str(item.get("caption") or "")[:300],
                              "missing": not os.path.isfile(path)})
        cwd = str(source.get("cwd") or "")
        identity = self.workstream_identity(cwd) if cwd else {}
        unresolved_text = "\n".join(f"- {text[:800]}" for text in unresolved if text) or "- None recorded"
        recent_text = "\n\n".join(recent) or "No recent prose was available."
        preview = f"""Continue this work in a new, independent {target.title()} coding session.

Source session: {source_provider} · {sid}
Repository: {source.get('project') or os.path.basename(cwd) or 'unknown'}
Working directory: {cwd or 'unknown'}
Branch: {source.get('branch') or 'unknown'}

Objective
{objective[:6000]}

Recent conversation
{recent_text}

Unresolved work
{unresolved_text}

Repository evidence
- Canonical repository: {identity.get('root') or cwd or 'unknown'}
- Changed files: not observed yet
- Tests: not observed yet
- Pull request: not observed yet

{self._handoff_artifact_section(artifacts)}

Treat this as an independent session. Verify the repository state before changing files, and do not assume the source session has stopped."""
        default_model = source.get("model") if target == source_provider else ""
        if target == "claude" and default_model not in self.MODELS:
            family = model_family(default_model)
            default_model = family if family in self.MODELS else ""
        default_effort = source.get("effort") if target == source_provider else ""
        if target == "claude" and default_effort not in self.EFFORTS:
            default_effort = ""
        defaults = {"cwd": cwd, "model": default_model,
                    "effort": default_effort,
                    "mode": (source.get("collaboration_mode") or "plan") if target == "codex" else None,
                    "worktree": False, "worktree_name": ""}
        return {"ok": True, "source": {key: source.get(key) for key in
                ("session_id", "provider", "title", "project", "cwd", "branch", "model",
                 "effort", "collaboration_mode")}, "target_provider": target,
                "preview": redact_handoff_text(preview), "artifacts": artifacts,
                "defaults": defaults, "independent_session": True}

    @staticmethod
    def file_id(sid, path):
        """Stable opaque selector for one session-owned file path."""
        material = f"{sid}\0{os.path.realpath(str(path or ''))}".encode("utf-8", "surrogatepass")
        return hashlib.sha256(material).hexdigest()[:24]

    @staticmethod
    def artifact_file_id(sid, source_agent_id, tool_id, ordinal):
        material = "\0".join((str(sid), str(source_agent_id or ""),
                               str(tool_id), str(int(ordinal)))).encode(
                                   "utf-8", "surrogatepass")
        return hashlib.sha256(material).hexdigest()[:24]

    @staticmethod
    def _artifact_kind(name):
        return "image" if os.path.splitext(str(name or ""))[1].lower() in IMG_EXTS else "text"

    def store_artifact_deliveries(self, deliveries):
        """Copy acknowledged SendUserFile bytes outside ``scan_lock``.

        The database row is per delivery, not per source path. Re-delivering a
        changing file therefore preserves every version while replaying the
        same transcript remains idempotent.
        """
        if not deliveries:
            return
        root = os.path.join(pathcfg.BASE, "delivered-artifacts")
        os.makedirs(root, mode=0o700, exist_ok=True)
        db = self.ensure_db()
        with self.db_lock:
            for delivery in deliveries:
                sid = str(delivery.get("session_id") or "")
                tool_id = str(delivery.get("tool_id") or "")
                agent_id = str(delivery.get("source_agent_id") or "") or None
                backups = delivery.get("file_backups") or {}
                for ordinal, source in enumerate(delivery.get("files") or []):
                    if not isinstance(source, str) or not source:
                        continue
                    selector = self.artifact_file_id(sid, agent_id, tool_id, ordinal)
                    existing = db.execute(
                        "SELECT blob_name FROM delivered_artifacts WHERE file_id=?",
                        (selector,)).fetchone()
                    blob_name = existing[0] if existing else None
                    blob_path = os.path.join(root, blob_name) if blob_name else None
                    missing = not (blob_path and os.path.isfile(blob_path))
                    size = os.path.getsize(blob_path) if not missing else None
                    if missing:
                        candidate = source if os.path.isfile(source) else \
                            self._claude_file_backup(sid, backups.get(source))
                        if candidate:
                            blob_name = selector + ".bin"
                            blob_path = os.path.join(root, blob_name)
                            temp = None
                            try:
                                handle = tempfile.NamedTemporaryFile(
                                    dir=root, prefix=selector + ".", delete=False)
                                temp = handle.name
                                with handle, open(candidate, "rb") as source_handle:
                                    shutil.copyfileobj(source_handle, handle, 1024 * 1024)
                                    handle.flush()
                                    os.fsync(handle.fileno())
                                os.chmod(temp, 0o600)
                                os.replace(temp, blob_path)
                                size = os.path.getsize(blob_path)
                                missing = False
                            except OSError:
                                if temp:
                                    try:
                                        os.unlink(temp)
                                    except OSError:
                                        pass
                    name = os.path.basename(source) or "artifact"
                    db.execute("""INSERT INTO delivered_artifacts(
                        file_id,session_id,source_agent_id,tool_id,ordinal,
                        source_path,name,caption,delivered_at,blob_name,size,kind,
                        missing,transcript_path)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(file_id) DO UPDATE SET
                          source_path=excluded.source_path,name=excluded.name,
                          caption=excluded.caption,delivered_at=excluded.delivered_at,
                          blob_name=COALESCE(excluded.blob_name,delivered_artifacts.blob_name),
                          size=COALESCE(excluded.size,delivered_artifacts.size),
                          kind=excluded.kind,missing=excluded.missing,
                          transcript_path=excluded.transcript_path""",
                        (selector, sid, agent_id, tool_id, ordinal, source, name,
                         str(delivery.get("caption") or ""),
                         str(delivery.get("ts") or ""), blob_name, size,
                         self._artifact_kind(name), int(missing),
                         str(delivery.get("transcript_path") or "")))
            db.commit()

    def session_files(self, sid, source_agent_id=None, cursor=0, limit=100):
        try:
            cursor = max(0, int(cursor or 0))
            limit = max(1, min(200, int(limit or 100)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid file pagination"}
        db = None
        try:
            db = self.ledger_reader()
            if source_agent_id:
                rows = db.execute("""SELECT rowid,file_id,name,caption,delivered_at,
                    size,kind,missing,source_agent_id
                    FROM delivered_artifacts
                    WHERE session_id=? AND source_agent_id=? AND rowid<?
                    ORDER BY rowid DESC LIMIT ?""",
                    (str(sid), str(source_agent_id),
                     cursor or 9223372036854775807, limit + 1)).fetchall()
            else:
                rows = db.execute("""SELECT rowid,file_id,name,caption,delivered_at,
                    size,kind,missing,source_agent_id
                    FROM delivered_artifacts WHERE session_id=? AND rowid<?
                    ORDER BY rowid DESC LIMIT ?""",
                    (str(sid), cursor or 9223372036854775807,
                     limit + 1)).fetchall()
        except Exception:
            return {"ok": False, "error": "file inventory unavailable"}
        finally:
            if db is not None:
                db.close()
        more = len(rows) > limit
        rows = rows[:limit]
        files = [{"file_id": row[1], "name": row[2], "caption": row[3] or "",
                  "ts": row[4], "size": row[5], "kind": row[6],
                  "missing": bool(row[7]), "source_agent_id": row[8]}
                 for row in rows]
        return {"ok": True, "files": files,
                "next_cursor": rows[-1][0] if more and rows else None}

    def artifact_content(self, sid, selector):
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute("""SELECT name,blob_name,missing FROM delivered_artifacts
                WHERE session_id=? AND file_id=?""", (str(sid), str(selector))).fetchone()
        except Exception:
            return None
        finally:
            if db is not None:
                db.close()
        if not row:
            return None
        name, blob_name, missing = row
        source = os.path.join(pathcfg.BASE, "delivered-artifacts", blob_name or "")
        if missing or not blob_name or not os.path.isfile(source):
            return None, None, "unreadable: the delivered artifact copy is unavailable"
        try:
            if os.path.getsize(source) > 8_000_000:
                return None, None, "file too large to preview (>8MB)"
            with open(source, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            return None, None, f"unreadable: {exc}"
        ext = os.path.splitext(name)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".pdf": "application/pdf",
                 ".json": "application/json; charset=utf-8"}.get(
                     ext, "text/plain; charset=utf-8")
        return ctype, data, None

    def artifact_download(self, sid, selector):
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute("""SELECT name,blob_name,missing,size
                FROM delivered_artifacts WHERE session_id=? AND file_id=?""",
                (str(sid), str(selector))).fetchone()
        except Exception:
            return None
        finally:
            if db is not None:
                db.close()
        if not row:
            return None
        name, blob_name, missing, size = row
        root = os.path.realpath(os.path.join(pathcfg.BASE, "delivered-artifacts"))
        source = os.path.realpath(os.path.join(root, blob_name or ""))
        if (missing or not blob_name or os.path.dirname(source) != root or
                not os.path.isfile(source)):
            return None, None, None, None, \
                "unreadable: the delivered artifact copy is unavailable"
        ext = os.path.splitext(name)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".pdf": "application/pdf",
                 ".json": "application/json; charset=utf-8"}.get(
                     ext, "application/octet-stream")
        return ctype, source, name, int(size or os.path.getsize(source)), None

    def artifact_tool_files(self, sid, tool_id, source_agent_id=None):
        db = None
        try:
            db = self.ledger_reader()
            if source_agent_id:
                rows = db.execute("""SELECT file_id,name,caption,delivered_at,size,
                        kind,missing,source_agent_id
                    FROM delivered_artifacts
                    WHERE session_id=? AND source_agent_id=? AND tool_id=?
                    ORDER BY ordinal""",
                    (str(sid), str(source_agent_id), str(tool_id))).fetchall()
            else:
                rows = db.execute("""SELECT file_id,name,caption,delivered_at,size,
                        kind,missing,source_agent_id
                    FROM delivered_artifacts
                    WHERE session_id=? AND source_agent_id IS NULL AND tool_id=?
                    ORDER BY ordinal""", (str(sid), str(tool_id))).fetchall()
        except Exception:
            return []
        finally:
            if db is not None:
                db.close()
        return [{"file_id": row[0], "name": row[1], "caption": row[2] or "",
                 "ts": row[3], "size": row[4], "kind": row[5],
                 "missing": bool(row[6]), "source_agent_id": row[7]}
                for row in rows]

    def artifact_selector_for_path(self, sid, raw_path):
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute("""SELECT file_id FROM delivered_artifacts
                WHERE session_id=? AND source_path=? ORDER BY rowid DESC LIMIT 1""",
                (str(sid), str(raw_path))).fetchone()
            return row[0] if row else None
        except Exception:
            return None
        finally:
            if db is not None:
                db.close()

    def _project_file_ids(self, sid, context):
        """Project internal file records to opaque, client-safe selectors."""
        if not isinstance(context, dict):
            return context
        for item in context.get("files") or []:
            if isinstance(item, dict) and item.get("path"):
                item["file_id"] = self.file_id(sid, item["path"])
                item.pop("path", None)
        for message in context.get("messages") or []:
            for item in message.get("files") or [] if isinstance(message, dict) else []:
                if isinstance(item, dict) and item.get("path"):
                    item["file_id"] = self.file_id(sid, item["path"])
                    item.pop("path", None)
        return context

    def closed_resume_capability(self, row_or_sid):
        row = row_or_sid if isinstance(row_or_sid, dict) else None
        sid = str((row or {}).get("session_id") or row_or_sid or "")
        if sid.startswith("codex:"):
            return self.codex.resume_capability(sid)
        if row is None:
            row = next((item for item in self.closed_sessions()
                        if item.get("session_id") == sid), None)
        if not row or row.get("provider") not in (None, "claude"):
            return False, "the saved session is unavailable"
        if not self._safe_claude_transcript(sid, row.get("transcript_path")):
            return False, "the saved Claude transcript is unavailable"
        if not self._safe_reopen_cwd(row.get("cwd")):
            return False, "the saved working directory is unavailable"
        return True, None

    def _closed_claude_agents(self, sid, transcript_path, lock_held=False,
                              artifact_deliveries=None):
        root = os.path.realpath(os.path.join(os.path.dirname(transcript_path), str(sid),
                                             "subagents"))
        expected_parent = os.path.realpath(os.path.dirname(transcript_path))
        if os.path.dirname(os.path.dirname(root)) != expected_parent or not os.path.isdir(root):
            return []
        out = []
        for meta_path in sorted(glob.glob(os.path.join(root, "agent-*.meta.json"))):
            aid = os.path.basename(meta_path)[:-len(".meta.json")]
            if not re.fullmatch(r"agent-[A-Za-z0-9_-]{1,64}", aid):
                continue
            transcript = os.path.realpath(os.path.join(root, aid + ".jsonl"))
            if os.path.dirname(transcript) != root or not os.path.isfile(transcript):
                continue
            try:
                with open(meta_path) as handle:
                    meta = json.load(handle)
            except Exception:
                meta = {}
            tail = self.tail_for(transcript)
            if lock_held:
                tail.poll()
            else:
                with self.scan_lock:
                    tail.poll()
            if artifact_deliveries is not None and tail.file_deliveries:
                artifact_deliveries.extend({
                    **delivery, "session_id": sid, "source_agent_id": aid,
                    "transcript_path": transcript,
                    "file_backups": dict(tail.file_backups),
                } for delivery in tail.file_deliveries)
                tail.file_deliveries.clear()
            role, _stop, ctypes = (tail.last_shape or (None, None, []))[:3]
            settled = role == "assistant" and "tool_use" not in (ctypes or [])
            out.append({
                "agent_id": aid, "session_id": sid,
                "agent_type": meta.get("agentType", "?"),
                "description": meta.get("description", ""),
                "depth": meta.get("spawnDepth", 0), "model": tail.model,
                "family": model_family(tail.model), "effort": None,
                "state": "done" if settled else "ended", "quiet_s": None,
                "tokens": {"in": tail.ti, "cache_write": tail.tw,
                           "cache_read": tail.tr, "out": tail.to},
                "total_tokens": tail.total_tokens, "cost": round(tail.cost(self.cfg), 4),
                "tok_per_s": 0, "spark": [], "started": tail.first_ts,
                "last": tail.last_ts, "convo_v": tail.convo_rev,
                "last_msg": tail.last_message(800),
                "card_peek": card_peek_rows(tail.convo), "closed": True,
            })
        out.sort(key=lambda item: (item.get("started") or "", item["agent_id"]))
        return out

    def closed_sessions(self):
        cols = ("session_id", "name", "project", "cwd", "branch", "model", "cost",
                "agent_cost", "agents_total", "bridge_url", "first_seen", "last_seen",
                "closed_at", "title", "provider", "transcript_path", "status_line_json")
        db = None
        try:
            db = self.ledger_reader()
            signature = db.execute("""SELECT COUNT(*),MAX(closed_at)
                FROM session_runs WHERE closed_at IS NOT NULL""").fetchone()
            now_mono = time.monotonic()
            cached = self._closed_sessions_cache
            if cached and cached[0] == signature and now_mono < cached[1]:
                return copy.deepcopy(cached[2])
            rows = db.execute(
                f"""SELECT {','.join(cols)} FROM session_runs
                    WHERE closed_at IS NOT NULL ORDER BY closed_at DESC""").fetchall()
            out = [dict(zip(cols, r)) for r in rows]
            for row in out:
                try:
                    status_line = json.loads(row.pop("status_line_json") or "{}")
                except (TypeError, ValueError):
                    status_line = {}
                if isinstance(status_line, dict) and status_line:
                    status_line["frozen"] = True
                    row["status_line"] = status_line
                row["can_reopen"] = bool(
                    row.get("provider") == "claude" and
                    self._safe_claude_transcript(row.get("session_id"),
                                                 row.get("transcript_path")) and
                    self._safe_reopen_cwd(row.get("cwd")))
                can_resume, reason = self.closed_resume_capability(row)
                row["can_resume_and_send"] = can_resume
                row["resume_disabled_reason"] = reason
            self._closed_sessions_cache = (signature, now_mono + 60, copy.deepcopy(out))
            return out
        except Exception:
            return []
        finally:
            if db is not None:
                db.close()

    def closed_context(self, sid):
        """Conversation of a CLOSED session: its process is gone, so the registry
        can't resolve it — the ledger's cwd is the only path back to the file."""
        row = None
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute(
                "SELECT cwd, model, cost, title, project, branch, transcript_path, "
                "status_line_json "
                "FROM session_runs "
                "WHERE session_id = ? AND closed_at IS NOT NULL", (sid,)).fetchone()
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()
        if not row:
            return {"ok": False, "error": "unknown session"}
        try:
            status_line = json.loads(row[7] or "{}")
        except (TypeError, ValueError):
            status_line = {}
        if isinstance(status_line, dict) and status_line:
            status_line["frozen"] = True
        else:
            status_line = None
        if str(sid).startswith("codex:"):
            result = self.codex.context(sid)
            if result.get("ok"):
                result["closed"] = True
                info = result.setdefault("info", {})
                info["status_line"] = status_line
                allowed, reason = self.codex.resume_capability(sid)
                info.update(can_resume_and_send=allowed,
                            resume_disabled_reason=reason)
                self._project_file_ids(sid, result)
            return result
        fallback = os.path.join(cwd_to_project_dir(row[0] or ""), f"{sid}.jsonl")
        path = self._safe_claude_transcript(sid, row[6] or fallback)
        if not path:
            inventory = self.session_files(sid, limit=100)
            if not inventory.get("files"):
                return {"ok": False, "error": "transcript is gone"}
            can_resume = bool(self._safe_reopen_cwd(row[0]))
            return {"ok": True, "messages": [], "files": inventory["files"],
                    "agents": [], "closed": True, "transcript_missing": True,
                    "info": {"session_id": sid, "cwd": row[0], "model": row[1],
                             "cost": row[2], "title": row[3], "project": row[4],
                             "branch": row[5], "status_line": status_line,
                             "can_reopen": False, "can_resume_and_send": False,
                             "resume_disabled_reason":
                                 "the saved Claude transcript is unavailable"}}
        deliveries = []
        with self.scan_lock:
            t = self.tail_for(path)
            t.poll()
            msgs = [dict(m) for m in t.convo]
            files = [dict(item) for item in t.files]
            backups = dict(t.file_backups)
            agents = self._closed_claude_agents(
                sid, path, lock_held=True, artifact_deliveries=deliveries)
            deliveries.extend({
                **delivery, "session_id": sid, "source_agent_id": None,
                "transcript_path": path, "file_backups": backups,
            } for delivery in t.file_deliveries)
            t.file_deliveries.clear()
        self.store_artifact_deliveries(deliveries)
        def fmeta(raw_path):
            backup = self._claude_file_backup(sid, backups.get(raw_path))
            return {"name": os.path.basename(raw_path),
                    "file_id": self.file_id(sid, raw_path),
                    "kind": "image" if os.path.splitext(raw_path)[1].lower() in IMG_EXTS
                    else "text", "missing": not os.path.isfile(raw_path) and backup is None}
        tool_files = {}
        for m in msgs:              # file chips need the same metadata the live view builds
            if m.get("role") == "tool" and m.get("files"):
                tool_id = str(m.get("tool_id") or "")
                durable = tool_files.setdefault(
                    tool_id, self.artifact_tool_files(sid, tool_id)) if tool_id else []
                m["files"] = durable or [fmeta(p) for p in m["files"]]
        durable_files = self.session_files(sid, limit=100)
        out_files = durable_files.get("files") if durable_files.get("ok") else []
        if not out_files:
            out_files = [{**fmeta(item["path"]), "caption": item.get("caption", ""),
                          "ts": item.get("ts")} for item in reversed(files)]
        can_resume = bool(self._safe_reopen_cwd(row[0]))
        reason = None if can_resume else "the saved working directory is unavailable"
        return {"ok": True, "messages": msgs, "files": out_files,
                "agents": agents, "closed": True,
                "info": {"session_id": sid, "cwd": row[0], "model": row[1],
                         "cost": row[2], "title": row[3], "project": row[4],
                         "branch": row[5],
                         "status_line": status_line,
                         "can_reopen": can_resume,
                         "can_resume_and_send": can_resume,
                         "resume_disabled_reason": reason}}

    @staticmethod
    def trusted_dirs():
        """Dirs where Claude Code's "do you trust this folder?" prompt is already
        answered (~/.claude.json `projects[dir].hasTrustDialogAccepted`). A spawn
        into an UNTRUSTED dir stops at that prompt, which only the Mac can answer —
        so the picker flags them instead of pretending a remote start will work. We
        never WRITE this flag: it is a security gate, not a preference."""
        try:
            with open(os.path.join(pathcfg.HOME, ".claude.json")) as f:
                projects = (json.load(f) or {}).get("projects") or {}
        except Exception:
            return set()
        return {d for d, v in projects.items()
                if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}

    def is_trusted(self, path, trusted=None):
        """Trust is INHERITED: a git worktree under a trusted repo has no entry of
        its own in ~/.claude.json yet never prompts (verified 2026-07-14 — every
        Quirk worktree is absent from `projects` and starts clean), while a fresh
        dir with no trusted ancestor does prompt. So walk up to /."""
        trusted = self.trusted_dirs() if trusted is None else trusted
        p = os.path.realpath(path)
        while True:
            if p in trusted:
                return True
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent

    def recent_dirs(self, limit=25):
        """Directories the daemon has actually seen sessions in — the new-session
        picker's menu (a phone has no file browser). Only offers dirs a spawn would
        actually accept: never list what spawn_session will refuse."""
        home = os.path.realpath(pathcfg.HOME)
        trusted = self.trusted_dirs()

        def ok(d):
            if not d or not os.path.isdir(d):
                return False
            rp = os.path.realpath(d)
            return rp == home or rp.startswith(home + os.sep)

        paths = []
        db = None
        try:
            db = self.ledger_reader()
            rows = db.execute(
                "SELECT cwd, MAX(COALESCE(last_seen, 0)) t FROM session_runs "
                "WHERE cwd IS NOT NULL AND cwd != '' GROUP BY cwd "
                "ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
            paths = [r[0] for r in rows if ok(r[0])]
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()
        for r in self.live_sessions():           # live cwds first, even if unledgered
            cwd = r.get("cwd")
            if ok(cwd) and cwd not in paths:
                paths.insert(0, cwd)
        return [{"path": p, "trusted": self.is_trusted(p, trusted)} for p in paths]
