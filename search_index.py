"""Incremental authenticated search across local Claude and Codex transcripts.

The index is deliberately separate from Fleet's session ledger and provider poll.
Discovery and parsing run on a background thread in short batches; API reads use
the same WAL database behind a small lock and never touch transcript files.
"""
import argparse
from collections import deque, OrderedDict
import fcntl
import json
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time


PARSER_VERSION = 1
MAX_ROW_BYTES = 2 * 1024 * 1024
MAX_DOC_CHARS = 200_000
ARTIFACT_MAX_BYTES = 1_000_000
TEXT_EXTENSIONS = {
    ".c", ".cfg", ".cpp", ".css", ".csv", ".go", ".h", ".hpp", ".html",
    ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".log", ".md",
    ".markdown", ".php", ".py", ".r", ".rb", ".rs", ".sh", ".sql",
    ".swift", ".toml", ".ts", ".tsx", ".tsv", ".txt", ".xml", ".yaml",
    ".yml", ".zsh",
}
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
QUERY_TOKEN_RE = re.compile(r"[\w./:@#-]+", re.UNICODE)


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _bounded(value, limit=MAX_DOC_CHARS):
    text = str(value or "").strip()
    return text[:limit], len(text) > limit


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ in ("text", "input_text", "output_text", "summary_text"):
            parts.append(str(block.get("text") or ""))
    return "\n\n".join(part for part in parts if part).strip()


def _result_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("content")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def _json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value or "")


def _candidate_paths(value, cwd=""):
    """Find only provider-recorded path fields; never scan a repository."""
    found = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for child_key, child in node.items():
                if child_key in ("path", "file_path", "notebook_path") and isinstance(child, str):
                    found.append(child)
                elif child_key == "files" and isinstance(child, list):
                    found.extend(item for item in child if isinstance(item, str))
                elif isinstance(child, (dict, list)):
                    walk(child, child_key)
        elif isinstance(node, list):
            for child in node:
                walk(child, key)

    walk(value)
    out = []
    for path in found[:40]:
        path = os.path.expanduser(path)
        if not os.path.isabs(path) and cwd:
            path = os.path.join(cwd, path)
        if os.path.isabs(path):
            real = os.path.realpath(path)
            if real not in out:
                out.append(real)
    return out


def _claude_row(row, source):
    docs, artifacts, meta = [], [], {}
    known = True
    oversized = 0
    timestamp = row.get("timestamp")
    if row.get("cwd"):
        meta["cwd"] = str(row["cwd"])
        meta["project"] = os.path.basename(str(row["cwd"]).rstrip(os.sep))
    if row.get("gitBranch"):
        meta["branch"] = str(row["gitBranch"])
    typ = row.get("type")
    if typ == "ai-title":
        meta["title"] = str(row.get("aiTitle") or "")[:200]
        return docs, artifacts, meta, known, oversized
    if typ == "custom-title":
        meta["title"] = str(row.get("customTitle") or "")[:200]
        return docs, artifacts, meta, known, oversized
    if typ == "attachment":
        attachment = row.get("attachment") or {}
        if attachment.get("type") == "queued_command" and \
                (attachment.get("origin") or {}).get("kind") == "human":
            text, cut = _bounded(_content_text(attachment.get("prompt")) or
                                 attachment.get("prompt"))
            oversized += int(cut)
            if text:
                docs.append(("user", "message", timestamp, text, None))
        return docs, artifacts, meta, known, oversized
    if typ == "system":
        text, cut = _bounded(row.get("content") or row.get("message") or
                             row.get("subtype") or "System event")
        oversized += int(cut)
        if text:
            docs.append(("system", "event", timestamp, text, None))
        return docs, artifacts, meta, known, oversized

    message = row.get("message")
    if not isinstance(message, dict):
        # Metadata-only and queue bookkeeping rows are expected.
        known = typ in ("queue-operation", "progress", "file-history-snapshot",
                        "summary", "pr-link", "last-prompt", None)
        return docs, artifacts, meta, known, oversized
    role = message.get("role")
    content = message.get("content")
    if role == "assistant":
        if message.get("model"):
            meta["model"] = str(message["model"])
        text, cut = _bounded(_content_text(content))
        oversized += int(cut)
        if text:
            docs.append(("assistant", "message", timestamp, text, None))
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in ("thinking", "reasoning"):
                    reasoning, cut = _bounded(block.get("thinking") or block.get("text"))
                    oversized += int(cut)
                    if reasoning:
                        docs.append(("assistant", "reasoning", timestamp, reasoning, None))
                elif block_type == "tool_use":
                    name = str(block.get("name") or "tool")
                    payload = block.get("input") or {}
                    tool_text, cut = _bounded(name + "\n" + _json_text(payload))
                    oversized += int(cut)
                    docs.append(("tool", "tool", timestamp, tool_text, None))
                    if name == "SendUserFile":
                        artifacts.extend(_candidate_paths(payload, meta.get("cwd") or
                                                          source.get("cwd") or ""))
    elif role == "user":
        if isinstance(content, list):
            text = _content_text(content)
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result, cut = _bounded(_result_text(block.get("content")))
                    oversized += int(cut)
                    if result:
                        docs.append(("tool", "tool", timestamp, result, None))
        else:
            text = str(content or "")
        text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text,
                      flags=re.S).strip()
        text, cut = _bounded(text)
        oversized += int(cut)
        if text and not row.get("isMeta"):
            docs.append(("user", "message", timestamp, text, None))
    else:
        known = False
    return docs, artifacts, meta, known, oversized


def _codex_row(row, source):
    docs, artifacts, meta = [], [], {}
    known = True
    oversized = 0
    timestamp = row.get("timestamp")
    typ = row.get("type")
    payload = row.get("payload") or {}
    if typ == "session_meta":
        cwd = payload.get("cwd")
        if cwd:
            meta["cwd"] = str(cwd)
            meta["project"] = os.path.basename(str(cwd).rstrip(os.sep))
        if payload.get("id") or payload.get("session_id"):
            meta["session_id"] = "codex:" + str(payload.get("id") or payload.get("session_id"))
        if payload.get("thread_source") == "subagent":
            meta["source_kind"] = "subagent"
            meta["agent_id"] = str(payload.get("id") or payload.get("session_id") or
                                   source.get("agent_id") or "")
        return docs, artifacts, meta, known, oversized
    if typ == "turn_context":
        if payload.get("cwd"):
            meta["cwd"] = str(payload["cwd"])
            meta["project"] = os.path.basename(str(payload["cwd"]).rstrip(os.sep))
        if payload.get("model"):
            meta["model"] = str(payload["model"])
        return docs, artifacts, meta, known, oversized
    if typ == "event_msg":
        event_type = payload.get("type")
        if event_type == "user_message":
            text, cut = _bounded(payload.get("message"))
            oversized += int(cut)
            if text:
                docs.append(("user", "message", timestamp, text, None))
        elif event_type == "agent_message":
            text, cut = _bounded(payload.get("message"))
            oversized += int(cut)
            if text:
                docs.append(("assistant", "message", timestamp, text, None))
        elif event_type in ("task_started", "task_complete", "turn_aborted",
                            "context_compacted", "thread_settings_applied"):
            docs.append(("system", "event", timestamp,
                         event_type.replace("_", " "), None))
        elif event_type in ("patch_apply_end", "item_completed", "mcp_tool_call_end",
                            "web_search_end", "token_count"):
            artifacts.extend(_candidate_paths(payload, source.get("cwd") or ""))
        else:
            known = False
        return docs, artifacts, meta, known, oversized
    if typ == "response_item":
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role") or "assistant"
            text, cut = _bounded(_content_text(payload.get("content")))
            oversized += int(cut)
            if text:
                docs.append((role, "message", timestamp, text, None))
        elif item_type == "reasoning":
            summary = payload.get("summary")
            if isinstance(summary, list):
                summary = _content_text(summary) or _json_text(summary)
            text, cut = _bounded(summary)
            oversized += int(cut)
            if text:
                docs.append(("assistant", "reasoning", timestamp, text, None))
        elif item_type in ("function_call", "custom_tool_call", "local_shell_call",
                           "mcp_tool_call", "web_search_call", "file_search_call"):
            name = payload.get("name") or item_type
            value = payload.get("arguments") or payload.get("input") or payload
            text, cut = _bounded(str(name) + "\n" + _json_text(value))
            oversized += int(cut)
            docs.append(("tool", "tool", timestamp, text, None))
            artifacts.extend(_candidate_paths(value, source.get("cwd") or ""))
        elif item_type in ("function_call_output", "custom_tool_call_output",
                           "local_shell_call_output", "mcp_tool_call_output"):
            text, cut = _bounded(payload.get("output") or payload.get("content"))
            oversized += int(cut)
            if text:
                docs.append(("tool", "tool", timestamp, text, None))
        else:
            known = False
        return docs, artifacts, meta, known, oversized
    if typ in ("world_state", "ghost_snapshot"):
        return docs, artifacts, meta, known, oversized
    return docs, artifacts, meta, False, oversized


class SearchIndex:
    def __init__(self, db_path, claude_root, codex_root, clock=None,
                 discover_seconds=2.0, batch_rows=250):
        self.db_path = db_path
        self.reader_signal_path = db_path + ".reader-active"
        self.claude_root = os.path.realpath(claude_root)
        self.codex_root = os.path.realpath(codex_root)
        self.clock = clock or time.time
        self.discover_seconds = max(.25, float(discover_seconds))
        self.batch_rows = max(10, min(1000, int(batch_rows)))
        self.lock = threading.RLock()
        self.read_lock = threading.RLock()
        self.connection = None
        self.read_connection = None
        self.thread = None
        self.process = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.last_discovery = 0.0
        self.last_error = None
        self.indexing = False
        self.last_reader_signal = 0.0
        self.reader_wait_ms = deque(maxlen=240)
        self.projects_cache = []
        self.projects_cached_at = 0.0
        self.cache_lock = threading.RLock()
        self.search_cache = OrderedDict()

    def _read_wait(self, started):
        self.reader_wait_ms.append((time.perf_counter() - started) * 1000)

    def _invalidate_search_cache(self):
        with self.cache_lock:
            self.search_cache.clear()

    def _signal_reader(self):
        now = time.monotonic()
        if now - self.last_reader_signal < .02:
            return
        self.last_reader_signal = now
        try:
            with open(self.reader_signal_path, "a", encoding="utf-8"):
                os.utime(self.reader_signal_path, None)
        except OSError:
            pass

    @staticmethod
    def _confirmed_corruption(error):
        detail = str(error).lower()
        return any(marker in detail for marker in (
            "not a database", "database disk image is malformed",
            "file is encrypted", "malformed database schema"))

    def _quarantine_corrupt_db(self, error):
        for connection_name in ("read_connection", "connection"):
            connection = getattr(self, connection_name, None)
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
                setattr(self, connection_name, None)
        quarantine = (self.db_path + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S") +
                      "-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:8])
        for suffix in ("", "-wal", "-shm"):
            source = self.db_path + suffix
            if os.path.exists(source):
                os.replace(source, quarantine + suffix)
        self.last_error = ("Search index storage was corrupt and was rebuilt from local "
                           "transcripts. Preserved as " + os.path.basename(quarantine) +
                           f" ({type(error).__name__}).")
        self.last_discovery = 0
        self.projects_cache = []
        self.projects_cached_at = 0
        self._invalidate_search_cache()

    def _db(self):
        try:
            return self._open_db()
        except sqlite3.DatabaseError as exc:
            if not self._confirmed_corruption(exc):
                if self.connection is not None:
                    try:
                        self.connection.close()
                    except sqlite3.Error:
                        pass
                    self.connection = None
                raise
            self._quarantine_corrupt_db(exc)
            return self._open_db()

    def _open_db(self):
        with self.lock:
            if self.connection is not None:
                return self.connection
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            db = sqlite3.connect(self.db_path, timeout=3, check_same_thread=False)
            self.connection = db
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=3000")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY,
                    source_key TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT,
                    parent_source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
                    inode TEXT, fingerprint TEXT, size INTEGER NOT NULL DEFAULT 0,
                    mtime_ns INTEGER NOT NULL DEFAULT 0, offset INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 1,
                    parser_version INTEGER NOT NULL DEFAULT 1,
                    cwd TEXT, project TEXT, branch TEXT, model TEXT, title TEXT,
                    first_ts REAL, last_ts REAL, indexed_at REAL,
                    complete INTEGER NOT NULL DEFAULT 0,
                    error TEXT, malformed_rows INTEGER NOT NULL DEFAULT 0,
                    unknown_rows INTEGER NOT NULL DEFAULT 0,
                    oversized_docs INTEGER NOT NULL DEFAULT 0,
                    deferred_until REAL NOT NULL DEFAULT 0,
                    seen_generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS sources_pending
                    ON sources(complete, deferred_until, indexed_at);
                CREATE INDEX IF NOT EXISTS sources_session ON sources(session_id);
                CREATE TABLE IF NOT EXISTS documents(
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    ordinal TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    role TEXT, kind TEXT NOT NULL,
                    timestamp TEXT, timestamp_epoch REAL,
                    title TEXT, text TEXT NOT NULL, artifact_path TEXT,
                    UNIQUE(source_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS documents_source_position
                    ON documents(source_id, position);
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    title, text, content='documents', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2');
                CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                    INSERT INTO documents_fts(rowid,title,text)
                    VALUES(new.id,new.title,new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts,rowid,title,text)
                    VALUES('delete',old.id,old.title,old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts,rowid,title,text)
                    VALUES('delete',old.id,old.title,old.text);
                    INSERT INTO documents_fts(rowid,title,text)
                    VALUES(new.id,new.title,new.text);
                END;
                CREATE TABLE IF NOT EXISTS search_meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS search_stats(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    documents INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO search_stats(singleton,documents)
                    SELECT 1,COUNT(*) FROM documents WHERE kind!='metadata';
                CREATE TRIGGER IF NOT EXISTS documents_stats_ai
                    AFTER INSERT ON documents WHEN new.kind!='metadata' BEGIN
                    UPDATE search_stats SET documents=documents+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS documents_stats_ad
                    AFTER DELETE ON documents WHEN old.kind!='metadata' BEGIN
                    UPDATE search_stats SET documents=MAX(0,documents-1) WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS documents_stats_au
                    AFTER UPDATE OF kind ON documents BEGIN
                    UPDATE search_stats SET documents=documents+
                        CASE WHEN new.kind!='metadata' THEN 1 ELSE 0 END-
                        CASE WHEN old.kind!='metadata' THEN 1 ELSE 0 END
                        WHERE singleton=1;
                END;
            """)
            db.execute("INSERT OR REPLACE INTO search_meta(key,value) VALUES('parser_version',?)",
                       (str(PARSER_VERSION),))
            db.commit()
            return db

    def _read_db(self):
        with self.read_lock:
            if self.read_connection is not None:
                return self.read_connection
            # Initialize/migrate the schema once, then keep queries on a separate
            # WAL reader so transcript commits never hold up search requests.
            self._db()
            db = sqlite3.connect(self.db_path, timeout=3, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=3000")
            self.read_connection = db
            return db

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._worker, name="fleet-search-index",
                                       daemon=True)
        self.thread.start()

    def start_process(self):
        """Run the parser/writer outside the HTTP/provider process.

        SQLite WAL keeps parent-process searches concurrent with this writer,
        while the separate interpreter prevents JSON parsing from taking the
        provider poll and HTTP threads' GIL.
        """
        if self.process and self.process.poll() is None:
            return self.process
        self._db()
        self.process = subprocess.Popen([
            sys.executable, os.path.abspath(__file__), "--worker",
            "--db", self.db_path, "--claude-root", self.claude_root,
            "--codex-root", self.codex_root, "--parent-pid", str(os.getpid()),
            "--discover-seconds", str(self.discover_seconds),
            "--batch-rows", str(self.batch_rows),
        ], close_fds=True)
        return self.process

    def ensure_process(self):
        if self.process is None or self.process.poll() is not None:
            return self.start_process()
        return self.process

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        with self.lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        with self.read_lock:
            if self.read_connection is not None:
                self.read_connection.close()
                self.read_connection = None

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                progressed = self.run_once()
                self.last_error = None
            except Exception as exc:
                progressed = False
                self.last_error = str(exc)
            self.wake_event.wait(.01 if progressed else .35)
            self.wake_event.clear()

    @staticmethod
    def _contained(root, path):
        try:
            return os.path.commonpath((root, path)) == root
        except ValueError:
            return False

    @staticmethod
    def _fingerprint(path):
        with open(path, "rb") as handle:
            return hashlib.blake2b(handle.read(4096), digest_size=12).hexdigest()

    def _files(self):
        found = []
        if os.path.isdir(self.claude_root):
            for root, dirs, files in os.walk(self.claude_root, followlinks=False):
                dirs[:] = [name for name in dirs
                           if not os.path.islink(os.path.join(root, name))]
                for name in files:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.realpath(os.path.join(root, name))
                    if not self._contained(self.claude_root, path) or os.path.islink(path):
                        continue
                    rel = os.path.relpath(path, self.claude_root).split(os.sep)
                    if len(rel) == 2:
                        sid = os.path.splitext(name)[0]
                        found.append(("claude:" + path, path, "claude", "session", sid, None))
                    elif len(rel) >= 4 and rel[-2] == "subagents":
                        sid = rel[-3]
                        agent_id = os.path.splitext(name)[0]
                        found.append(("claude-subagent:" + path, path, "claude",
                                      "subagent", sid, agent_id))
        if os.path.isdir(self.codex_root):
            for root, dirs, files in os.walk(self.codex_root, followlinks=False):
                dirs[:] = [name for name in dirs
                           if not os.path.islink(os.path.join(root, name))]
                for name in files:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.realpath(os.path.join(root, name))
                    if not self._contained(self.codex_root, path) or os.path.islink(path):
                        continue
                    match = UUID_RE.findall(name)
                    if not match:
                        continue
                    sid = "codex:" + match[-1]
                    found.append(("codex:" + path, path, "codex", "session", sid, None))
        return found

    def discover(self):
        files = self._files()
        now = self.clock()
        with self.lock:
            db = self._db()
            generation_row = db.execute(
                "SELECT value FROM search_meta WHERE key='discovery_generation'").fetchone()
            generation = int(generation_row[0] if generation_row else 0) + 1
            for key, path, provider, kind, sid, aid in files:
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                inode = "%s:%s" % (stat.st_dev, stat.st_ino)
                row = db.execute("SELECT id,inode,fingerprint,offset,size,mtime_ns,parser_version FROM sources "
                                 "WHERE source_key=?", (key,)).fetchone()
                fingerprint = (self._fingerprint(path) if row is None or
                               row["mtime_ns"] != stat.st_mtime_ns else row["fingerprint"])
                if row is None:
                    db.execute("""INSERT INTO sources(source_key,path,provider,source_kind,
                        session_id,agent_id,inode,fingerprint,size,mtime_ns,parser_version,seen_generation)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (key, path, provider, kind, sid, aid, inode, fingerprint, stat.st_size,
                                stat.st_mtime_ns, PARSER_VERSION, generation))
                else:
                    reset = (row["inode"] != inode or stat.st_size < row["offset"] or
                             row["parser_version"] != PARSER_VERSION or
                             (row["mtime_ns"] != stat.st_mtime_ns and
                              row["fingerprint"] != fingerprint))
                    if reset:
                        db.execute("DELETE FROM sources WHERE parent_source_id=?", (row["id"],))
                        db.execute("DELETE FROM documents WHERE source_id=?", (row["id"],))
                        db.execute("""UPDATE sources SET inode=?,fingerprint=?,size=?,mtime_ns=?,offset=0,
                            generation=generation+1,parser_version=?,complete=0,error=NULL,
                            malformed_rows=0,unknown_rows=0,oversized_docs=0,deferred_until=0,
                            seen_generation=? WHERE id=?""",
                                   (inode, fingerprint, stat.st_size, stat.st_mtime_ns, PARSER_VERSION,
                                    generation, row["id"]))
                    else:
                        complete = int(row["offset"] >= stat.st_size)
                        db.execute("""UPDATE sources SET path=?,fingerprint=?,size=?,mtime_ns=?,complete=?,
                            seen_generation=? WHERE id=?""",
                                   (path, fingerprint, stat.st_size, stat.st_mtime_ns, complete,
                                    generation, row["id"]))
            db.execute("DELETE FROM sources WHERE source_kind!='artifact' AND seen_generation<?",
                       (generation,))
            # Artifact files are provider-referenced children. Refresh their fingerprints
            # without ever discovering arbitrary repository paths.
            for row in db.execute("SELECT id,path,size,mtime_ns FROM sources "
                                  "WHERE source_kind='artifact'").fetchall():
                try:
                    stat = os.stat(row["path"])
                    changed = stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]
                    if changed:
                        db.execute("UPDATE sources SET size=?,mtime_ns=?,complete=0,error=NULL "
                                   "WHERE id=?", (stat.st_size, stat.st_mtime_ns, row["id"]))
                except OSError as exc:
                    # A referenced artifact may be temporary or deleted after delivery.
                    # Keep the warning visible, but do not make the worker retry a
                    # permanently missing file on every pass. Discovery will mark it
                    # pending again if the path reappears or changes.
                    db.execute("UPDATE sources SET complete=1,error=?,deferred_until=0 WHERE id=?",
                               (str(exc), row["id"]))
            db.execute("INSERT OR REPLACE INTO search_meta(key,value) VALUES"
                       "('discovery_generation',?)", (str(generation),))
            db.execute("INSERT OR REPLACE INTO search_meta(key,value) VALUES"
                       "('last_discovery_at',?)", (str(now),))
            db.commit()
        self._invalidate_search_cache()
        self.last_discovery = now
        return len(files)

    def _next_source(self):
        with self.lock:
            return self._db().execute("""SELECT * FROM sources
                WHERE complete=0 AND deferred_until<=?
                ORDER BY COALESCE(indexed_at,0), id LIMIT 1""",
                                      (self.clock(),)).fetchone()

    def run_once(self):
        now = self.clock()
        if now - self.last_discovery >= self.discover_seconds:
            self.discover()
        source = self._next_source()
        if source is None:
            self.indexing = False
            return False
        self.indexing = True
        source = dict(source)
        if source["source_kind"] == "artifact":
            self._index_artifact(source)
        else:
            self._index_transcript(source)
        self._invalidate_search_cache()
        return True

    def _read_batch(self, source):
        docs, artifacts, meta = [], [], {}
        malformed = unknown = oversized = 0
        offset = int(source.get("offset") or 0)
        end_offset = offset
        partial = False
        with open(source["path"], "rb") as handle:
            handle.seek(offset)
            for _ in range(self.batch_rows):
                position = handle.tell()
                raw = handle.readline(MAX_ROW_BYTES + 2)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if len(raw) > MAX_ROW_BYTES:
                        while raw and not raw.endswith(b"\n"):
                            raw = handle.readline(MAX_ROW_BYTES + 2)
                        end_offset = handle.tell()
                        malformed += 1
                        continue
                    handle.seek(position)
                    partial = True
                    break
                end_offset = handle.tell()
                if len(raw) > MAX_ROW_BYTES:
                    malformed += 1
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    malformed += 1
                    continue
                if not isinstance(row, dict):
                    malformed += 1
                    continue
                parser = _claude_row if source["provider"] == "claude" else _codex_row
                parsed, paths, updates, known, cut = parser(row, {**source, **meta})
                unknown += int(not known)
                oversized += cut
                meta.update({key: value for key, value in updates.items()
                             if value not in (None, "")})
                artifacts.extend(paths)
                for index, (role, kind, timestamp, text, artifact_path) in enumerate(parsed):
                    docs.append({"ordinal": "%d:%d" % (position, index),
                                 "position": position, "role": role, "kind": kind,
                                 "timestamp": timestamp, "timestamp_epoch": _epoch(timestamp),
                                 "text": text, "artifact_path": artifact_path})
        size = os.path.getsize(source["path"])
        return docs, artifacts, meta, malformed, unknown, oversized, end_offset, \
            int(end_offset >= size and not partial), size

    def _index_transcript(self, source):
        try:
            batch = self._read_batch(source)
        except OSError as exc:
            with self.lock:
                self._db().execute("UPDATE sources SET error=?,deferred_until=? WHERE id=?",
                                   (str(exc), self.clock() + 3, source["id"]))
                self._db().commit()
            return
        docs, artifacts, meta, malformed, unknown, oversized, end_offset, complete, size = batch
        now = self.clock()
        with self.lock:
            db = self._db()
            current = db.execute("SELECT * FROM sources WHERE id=?", (source["id"],)).fetchone()
            if current is None or current["generation"] != source["generation"]:
                return
            merged = dict(current)
            merged.update(meta)
            if not merged.get("project") and merged.get("cwd"):
                merged["project"] = os.path.basename(merged["cwd"].rstrip(os.sep))
            if not merged.get("title"):
                merged["title"] = (merged.get("project") or merged.get("agent_id") or
                                   merged.get("session_id") or "Conversation")
            db.execute("""UPDATE sources SET offset=?,size=?,complete=?,indexed_at=?,error=NULL,
                malformed_rows=malformed_rows+?,unknown_rows=unknown_rows+?,
                oversized_docs=oversized_docs+?,deferred_until=?,cwd=?,project=?,branch=?,
                model=?,title=?,session_id=?,source_kind=?,agent_id=? WHERE id=?""",
                       (end_offset, size, complete, now, malformed, unknown, oversized,
                        now + (1 if not complete and end_offset == source["offset"] else 0),
                        merged.get("cwd"), merged.get("project"), merged.get("branch"),
                        merged.get("model"), merged.get("title"), merged.get("session_id"),
                        merged.get("source_kind"), merged.get("agent_id"), source["id"]))
            title = merged.get("title") or "Conversation"
            previous = db.execute("""SELECT role,kind,text FROM documents
                WHERE source_id=? AND kind!='metadata' ORDER BY position DESC,id DESC LIMIT 1""",
                                  (source["id"],)).fetchone()
            previous_key = ((previous["role"], previous["kind"], previous["text"])
                            if previous else None)
            for doc in docs:
                # App Server rollouts can emit the same prose once as an event and
                # again as a response item. Keep one adjacent search record while
                # preserving repeated text that is separated by another event.
                key = (doc["role"], doc["kind"], doc["text"])
                if key == previous_key:
                    continue
                db.execute("""INSERT OR IGNORE INTO documents(source_id,ordinal,position,role,
                    kind,timestamp,timestamp_epoch,title,text,artifact_path)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (source["id"], doc["ordinal"], doc["position"], doc["role"],
                            doc["kind"], doc["timestamp"], doc["timestamp_epoch"], title,
                            doc["text"], doc["artifact_path"]))
                previous_key = key
            db.execute("UPDATE documents SET title=? WHERE source_id=? AND title!=?",
                       (title, source["id"], title))
            self._metadata_document(db, source["id"], merged)
            for path in artifacts:
                self._artifact_source(db, source["id"], merged, path)
            db.commit()

    def _metadata_document(self, db, source_id, source):
        fields = [source.get("provider"), source.get("source_kind"), source.get("session_id"),
                  source.get("agent_id"), source.get("project"), source.get("cwd"),
                  source.get("branch"), source.get("model"), source.get("title")]
        text = "\n".join(str(value) for value in fields if value)
        db.execute("""INSERT INTO documents(source_id,ordinal,position,role,kind,title,text)
            VALUES(?, 'metadata', -1, 'system', 'metadata', ?, ?)
            ON CONFLICT(source_id,ordinal) DO UPDATE SET title=excluded.title,text=excluded.text""",
                   (source_id, source.get("title") or "Conversation", text))

    def _artifact_source(self, db, parent_id, source, path):
        path = os.path.realpath(os.path.expanduser(str(path)))
        if not os.path.isabs(path):
            return
        key = "artifact:%s:%s:%s" % (source.get("provider"), source.get("session_id"), path)
        try:
            stat = os.stat(path)
            size, mtime_ns, error = stat.st_size, stat.st_mtime_ns, None
        except OSError as exc:
            size, mtime_ns, error = 0, 0, str(exc)
        db.execute("""INSERT INTO sources(source_key,path,provider,source_kind,session_id,
            agent_id,parent_source_id,size,mtime_ns,parser_version,cwd,project,branch,model,
            title,complete,error,seen_generation)
            VALUES(?,?,?,'artifact',?,?,?,?,?,?,?,?,?,?,?,0,?,0)
            ON CONFLICT(source_key) DO UPDATE SET parent_source_id=excluded.parent_source_id,
            size=excluded.size,mtime_ns=excluded.mtime_ns,cwd=excluded.cwd,
            project=excluded.project,branch=excluded.branch,model=excluded.model,
            title=excluded.title,complete=CASE WHEN sources.size!=excluded.size OR
            sources.mtime_ns!=excluded.mtime_ns THEN 0 ELSE sources.complete END,error=excluded.error""",
                   (key, path, source.get("provider"), source.get("session_id"),
                    source.get("agent_id"), parent_id, size, mtime_ns, PARSER_VERSION,
                    source.get("cwd"), source.get("project"), source.get("branch"),
                    source.get("model"), os.path.basename(path), error))

    def _index_artifact(self, source):
        path = source["path"]
        error = None
        text = ""
        try:
            stat = os.stat(path)
            extension = os.path.splitext(path)[1].lower()
            if extension not in TEXT_EXTENSIONS:
                error = "unsupported artifact preview type"
            elif stat.st_size > ARTIFACT_MAX_BYTES:
                error = "artifact exceeds 1 MB search limit"
            else:
                with open(path, "rb") as handle:
                    data = handle.read(ARTIFACT_MAX_BYTES + 1)
                if b"\0" in data:
                    error = "binary artifact"
                else:
                    text = data.decode("utf-8", "replace")
        except OSError as exc:
            stat = None
            error = str(exc)
        now = self.clock()
        with self.lock:
            db = self._db()
            db.execute("DELETE FROM documents WHERE source_id=?", (source["id"],))
            if text:
                db.execute("""INSERT INTO documents(source_id,ordinal,position,role,kind,
                    timestamp_epoch,title,text,artifact_path) VALUES(?, 'artifact', 0,
                    'artifact','artifact',?,?,?,?)""",
                           (source["id"], (stat.st_mtime if stat else now),
                            source.get("title") or os.path.basename(path), text, path))
            db.execute("""UPDATE sources SET size=?,mtime_ns=?,complete=1,indexed_at=?,
                error=?,deferred_until=0 WHERE id=?""",
                       ((stat.st_size if stat else 0), (stat.st_mtime_ns if stat else 0),
                        now, error, source["id"]))
            db.commit()

    @staticmethod
    def _match_query(query):
        tokens = QUERY_TOKEN_RE.findall(str(query or ""))[:12]
        if not tokens:
            return ""
        clauses = []
        for token in tokens:
            escaped = token.replace('"', '""')[:64]
            # Prefix matching helps while typing short fragments. For complete
            # words it expands the FTS candidate set dramatically with little UX
            # value, especially for common terms such as "fleet" or "session".
            clauses.append('"%s"%s' % (escaped, "*" if len(escaped) < 5 else ""))
        return " AND ".join(clauses)

    def search(self, query="", provider="", kind="", project="", cursor=0, limit=30):
        started = time.perf_counter()
        self._signal_reader()
        provider = str(provider or "")
        kind = str(kind or "")
        project = str(project or "").strip()[:200]
        if provider not in ("", "claude", "codex"):
            return {"ok": False, "error": "invalid provider"}
        if kind not in ("", "message", "tool", "reasoning", "event", "artifact", "metadata"):
            return {"ok": False, "error": "invalid kind"}
        try:
            cursor = max(0, int(cursor or 0))
            limit = max(1, min(50, int(limit or 30)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid pagination"}
        cache_key = (str(query or ""), provider, kind, project, cursor, limit)
        now_mono = time.monotonic()
        version_started = time.perf_counter()
        with self.read_lock:
            self._read_wait(version_started)
            data_version = self._read_db().execute("PRAGMA data_version").fetchone()[0]
        with self.cache_lock:
            cached = self.search_cache.get(cache_key)
            if cached and now_mono - cached[0] < 2 and cached[1] == data_version:
                self.search_cache.move_to_end(cache_key)
                response = dict(cached[2])
                response["results"] = [dict(item) for item in cached[2]["results"]]
                response["projects"] = list(cached[2]["projects"])
                response["cache_hit"] = True
                response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
                return response
        where, params = [], []
        if provider:
            where.append("s.provider=?"); params.append(provider)
        if kind:
            where.append("d.kind=?"); params.append(kind)
        if project:
            where.append("s.project=?"); params.append(project)
        match = self._match_query(query)
        wait_started = time.perf_counter()
        with self.read_lock:
            self._read_wait(wait_started)
            db = self._read_db()
            if match:
                where.insert(0, "documents_fts MATCH ?")
                params.insert(0, match)
                # FTS5 can stream ORDER BY rank efficiently. Re-rank only its top
                # bounded candidates for recency; sorting every match made common
                # queries scale with the full corpus and blocked an HTTP thread.
                candidate_limit = min(5000, max(100, cursor + limit + 50))
                sql = """WITH ranked AS MATERIALIZED (
                    SELECT d.id,d.timestamp_epoch,documents_fts.rank base_rank
                    FROM documents_fts JOIN documents d ON d.id=documents_fts.rowid
                    JOIN sources s ON s.id=d.source_id WHERE %s
                    ORDER BY documents_fts.rank LIMIT ?),
                    paged AS MATERIALIZED (
                      SELECT id,(base_rank-CASE
                        WHEN timestamp_epoch>? THEN .45
                        WHEN timestamp_epoch>? THEN .2 ELSE 0 END) score
                      FROM ranked ORDER BY score,timestamp_epoch DESC,id DESC LIMIT ? OFFSET ?)
                    SELECT d.id,s.id source_id,s.provider,s.source_kind,s.session_id,
                      s.agent_id,s.project,s.cwd,s.branch,s.title,d.role,d.kind,d.timestamp,
                      d.timestamp_epoch,d.artifact_path,
                      snippet(documents_fts,1,'','',' … ',28) snippet,paged.score rank
                    FROM paged JOIN documents d ON d.id=paged.id
                    JOIN documents_fts ON documents_fts.rowid=d.id
                    JOIN sources s ON s.id=d.source_id
                    ORDER BY paged.score,d.timestamp_epoch DESC,d.id DESC""" % \
                    (" AND ".join(where))
                params.extend([candidate_limit, self.clock() - 7 * 86400,
                               self.clock() - 90 * 86400, limit + 1, cursor])
            else:
                where.insert(0, "d.kind!='metadata'")
                sql = """SELECT d.id,s.id source_id,s.provider,s.source_kind,s.session_id,
                    s.agent_id,s.project,s.cwd,s.branch,s.title,d.role,d.kind,d.timestamp,
                    d.timestamp_epoch,d.artifact_path,substr(d.text,1,500) snippet,0 rank
                    FROM documents d JOIN sources s ON s.id=d.source_id WHERE %s
                    ORDER BY COALESCE(d.timestamp_epoch,s.indexed_at,0) DESC,d.id DESC
                    LIMIT ? OFFSET ?""" % (" AND ".join(where))
                params.extend([limit + 1, cursor])
            rows = db.execute(sql, params).fetchall()
            now_mono = time.monotonic()
            if now_mono - self.projects_cached_at >= 5 or not self.projects_cache:
                self.projects_cache = [row[0] for row in db.execute("""SELECT project FROM sources
                    WHERE project IS NOT NULL AND project!='' GROUP BY project
                    ORDER BY COUNT(*) DESC,project LIMIT 100""").fetchall()]
                self.projects_cached_at = now_mono
            projects = list(self.projects_cache)
        more = len(rows) > limit
        rows = rows[:limit]
        results = []
        for row in rows:
            item = dict(row)
            item["snippet"] = str(item.get("snippet") or "")[:500]
            results.append(item)
        response = {"ok": True, "query": str(query or "")[:500], "results": results,
                    "next_cursor": cursor + limit if more else None, "projects": projects,
                    "cache_hit": False,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        with self.cache_lock:
            self.search_cache[cache_key] = (now_mono, data_version, response)
            self.search_cache.move_to_end(cache_key)
            while len(self.search_cache) > 128:
                self.search_cache.popitem(last=False)
        return response

    def context(self, document_id, radius=12):
        self._signal_reader()
        try:
            document_id = int(document_id)
            radius = max(1, min(30, int(radius)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid search result"}
        wait_started = time.perf_counter()
        with self.read_lock:
            self._read_wait(wait_started)
            db = self._read_db()
            hit = db.execute("""SELECT d.*,s.provider,s.source_kind,s.session_id,s.agent_id,
                s.project,s.cwd,s.branch,s.model,s.title source_title,s.error source_error
                FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?""",
                             (document_id,)).fetchone()
            if hit is None:
                return {"ok": False, "error": "search result no longer exists"}
            before = db.execute("""SELECT id,role,kind,timestamp,text,artifact_path
                FROM documents WHERE source_id=? AND position<=? AND kind!='metadata'
                ORDER BY position DESC,id DESC LIMIT ?""",
                                (hit["source_id"], hit["position"], radius + 1)).fetchall()
            after = db.execute("""SELECT id,role,kind,timestamp,text,artifact_path
                FROM documents WHERE source_id=? AND position>? AND kind!='metadata'
                ORDER BY position,id LIMIT ?""",
                               (hit["source_id"], hit["position"], radius)).fetchall()
        messages = [dict(row) for row in reversed(before)] + [dict(row) for row in after]
        for item in messages:
            item["text"] = str(item.get("text") or "")[:50_000]
            item["hit"] = item["id"] == document_id
        source = {key: hit[key] for key in ("provider", "source_kind", "session_id",
                  "agent_id", "project", "cwd", "branch", "model", "source_title",
                  "source_error")}
        source["artifact_path"] = hit["artifact_path"]
        return {"ok": True, "document_id": document_id, "source": source,
                "messages": messages}

    def handoff_material(self, session_id, recent_limit=8):
        """Return bounded indexed context for an explicit provider handoff."""
        self._signal_reader()
        session_id = str(session_id or "")
        if not session_id or len(session_id) > 300 or any(ord(char) < 32 for char in session_id):
            return {"ok": False, "error": "invalid session id"}
        try:
            recent_limit = max(2, min(16, int(recent_limit)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid handoff context limit"}
        wait_started = time.perf_counter()
        with self.read_lock:
            self._read_wait(wait_started)
            db = self._read_db()
            source = db.execute("""SELECT id,provider,session_id,project,cwd,branch,model,title
                FROM sources WHERE session_id=? AND source_kind='session'
                ORDER BY COALESCE(last_ts,indexed_at,0) DESC,id DESC LIMIT 1""",
                                (session_id,)).fetchone()
            if source is None:
                return {"ok": False, "error": "session is not indexed yet"}
            first = db.execute("""SELECT text,timestamp FROM documents
                WHERE source_id=? AND role='user' AND kind='message' AND trim(text)!=''
                ORDER BY position,id LIMIT 1""", (source["id"],)).fetchone()
            recent = db.execute("""SELECT role,text,timestamp,position FROM documents
                WHERE source_id=? AND role IN ('user','assistant') AND kind='message'
                AND trim(text)!='' ORDER BY position DESC,id DESC LIMIT ?""",
                                (source["id"], recent_limit)).fetchall()
            artifact_rows = db.execute("""SELECT d.artifact_path,d.title,d.text,s.title source_title
                FROM documents d JOIN sources s ON s.id=d.source_id
                WHERE s.session_id=? AND d.kind='artifact' AND d.artifact_path IS NOT NULL
                ORDER BY COALESCE(d.timestamp_epoch,0) DESC,d.id DESC LIMIT 30""",
                                       (session_id,)).fetchall()
        recent_items = []
        for row in reversed(recent):
            recent_items.append({"role": row["role"], "timestamp": row["timestamp"],
                                 "text": str(row["text"] or "")[:4000]})
        todo_lines = []
        for item in reversed(recent_items):
            if item["role"] != "assistant":
                continue
            for line in str(item["text"]).splitlines():
                clean = re.sub(r"\s+", " ", line).strip(" -*\t")
                if clean and re.search(
                        r"(?i)\b(todo|remaining|next step|still need|need to|blocked|follow[- ]?up)\b",
                        clean):
                    todo_lines.append(clean[:500])
                if len(todo_lines) >= 8:
                    break
            if len(todo_lines) >= 8:
                break
        artifacts, seen = [], set()
        for row in artifact_rows:
            path = str(row["artifact_path"] or "")
            if not path or path in seen:
                continue
            seen.add(path)
            artifacts.append({"path": path,
                              "name": os.path.basename(path) or row["title"] or
                                      row["source_title"] or "artifact",
                              "caption": str(row["title"] or row["source_title"] or "")[:300]})
        return {"ok": True, "source": {key: source[key] for key in
                ("provider", "session_id", "project", "cwd", "branch", "model", "title")},
                "first_user": str(first["text"] or "")[:6000] if first else "",
                "recent": recent_items, "todos": todo_lines, "artifacts": artifacts}

    def status(self):
        wait_started = time.perf_counter()
        with self.read_lock:
            self._read_wait(wait_started)
            db = self._read_db()
            totals = db.execute("""SELECT COUNT(*) sources,
                SUM(CASE WHEN complete=1 THEN 1 ELSE 0 END) complete,
                SUM(CASE WHEN complete=0 THEN 1 ELSE 0 END) pending,
                COALESCE(SUM(size),0) bytes_total,
                COALESCE(SUM(CASE WHEN offset<size THEN offset ELSE size END),0) bytes_done,
                SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors,
                SUM(malformed_rows) malformed,SUM(unknown_rows) unknown,
                SUM(oversized_docs) oversized,MAX(indexed_at) last_indexed FROM sources""").fetchone()
            documents = db.execute(
                "SELECT documents FROM search_stats WHERE singleton=1").fetchone()[0]
            warnings = [dict(row) for row in db.execute("""SELECT provider,source_kind,
                session_id,title,error,malformed_rows,unknown_rows,oversized_docs
                FROM sources WHERE error IS NOT NULL OR malformed_rows>0 OR unknown_rows>0 OR
                oversized_docs>0 ORDER BY indexed_at DESC LIMIT 20""").fetchall()]
            meta = {row[0]: row[1] for row in db.execute(
                "SELECT key,value FROM search_meta WHERE key IN "
                "('last_discovery_at','last_worker_error')").fetchall()}
        total = int(totals["bytes_total"] or 0)
        done = int(totals["bytes_done"] or 0)
        pending = int(totals["pending"] or 0)
        last_indexed = totals["last_indexed"]
        waits = sorted(self.reader_wait_ms)
        wait_p95 = waits[min(len(waits) - 1, int(len(waits) * .95))] if waits else 0
        return {"ok": True, "state": "indexing" if pending else "idle",
                "sources": int(totals["sources"] or 0),
                "complete_sources": int(totals["complete"] or 0),
                "pending_sources": pending,
                "documents": int(documents), "bytes_total": total, "bytes_done": done,
                "progress_pct": round(done * 100 / total, 1) if total else 100.0,
                "errors": int(totals["errors"] or 0),
                "malformed_rows": int(totals["malformed"] or 0),
                "unknown_rows": int(totals["unknown"] or 0),
                "oversized_docs": int(totals["oversized"] or 0),
                "warnings": warnings,
                "last_error": meta.get("last_worker_error") or self.last_error,
                "last_discovery_at": float(meta.get("last_discovery_at") or
                                            self.last_discovery or 0),
                "last_indexed_at": last_indexed,
                "worker_lag_seconds": (round(max(0, self.clock() - last_indexed), 3)
                                       if pending and last_indexed else None),
                "reader_wait_p95_ms": round(wait_p95, 3),
                "parser_version": PARSER_VERSION}

    def rebuild(self):
        with self.cache_lock:
            self.search_cache.clear()
        with self.lock:
            db = self._db()
            db.execute("DELETE FROM sources")
            db.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
            db.commit()
        self.last_discovery = 0
        self.wake_event.set()
        return {"ok": True, "state": "rebuilding"}


def _parent_alive(parent_pid):
    try:
        os.kill(parent_pid, 0)
        return True
    except OSError:
        return False


def _worker(args):
    try:
        os.nice(10)
    except OSError:
        pass
    lock_path = args.db + ".worker.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as worker_lock:
        # A launch-agent restart can briefly overlap old/new parents. The new
        # writer waits for the old child to notice its parent exited, so only one
        # process ever advances offsets.
        fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX)
        index = SearchIndex(args.db, args.claude_root, args.codex_root,
                            discover_seconds=args.discover_seconds,
                            batch_rows=args.batch_rows)
        saved_error = None
        try:
            while _parent_alive(args.parent_pid):
                try:
                    reader_active = time.time() - os.path.getmtime(
                        index.reader_signal_path) < .25
                except OSError:
                    reader_active = False
                if reader_active:
                    time.sleep(.025)
                    continue
                try:
                    progressed = index.run_once()
                    index.last_error = None
                except Exception as exc:
                    progressed = False
                    index.last_error = str(exc)
                if index.last_error != saved_error:
                    with index.lock:
                        db = index._db()
                        if index.last_error:
                            db.execute("INSERT OR REPLACE INTO search_meta(key,value) "
                                       "VALUES('last_worker_error',?)", (index.last_error,))
                        else:
                            db.execute("DELETE FROM search_meta WHERE key='last_worker_error'")
                        db.commit()
                    saved_error = index.last_error
                time.sleep(.01 if progressed else .35)
        finally:
            index.close()


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--db")
    parser.add_argument("--claude-root")
    parser.add_argument("--codex-root")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--discover-seconds", type=float, default=2)
    parser.add_argument("--batch-rows", type=int, default=250)
    args = parser.parse_args()
    if not args.worker or not all((args.db, args.claude_root, args.codex_root,
                                   args.parent_pid)):
        parser.error("--worker requires db, transcript roots, and parent pid")
    _worker(args)


if __name__ == "__main__":
    _main()
