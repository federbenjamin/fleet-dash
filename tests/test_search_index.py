import json
import os
import tempfile
import threading
import time
import unittest

from search_index import SearchIndex


CLAUDE_SID = "11111111-2222-3333-4444-555555555555"
CODEX_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class SearchIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.project = os.path.join(self.claude, "-Users-test-repo")
        os.makedirs(self.project)
        os.makedirs(self.codex)
        self.db_path = os.path.join(self.tmp.name, "search.db")
        self.index = SearchIndex(self.db_path, self.claude, self.codex,
                                 discover_seconds=.01, batch_rows=2)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    @staticmethod
    def write_rows(path, rows, final_newline=True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            for index, row in enumerate(rows):
                raw = row if isinstance(row, bytes) else json.dumps(row).encode()
                handle.write(raw)
                if final_newline or index < len(rows) - 1:
                    handle.write(b"\n")

    @staticmethod
    def append_row(path, row, newline=True):
        with open(path, "ab") as handle:
            handle.write(row if isinstance(row, bytes) else json.dumps(row).encode())
            if newline:
                handle.write(b"\n")

    def drain(self, attempts=500):
        idle = 0
        for _ in range(attempts):
            progressed = self.index.run_once()
            if progressed:
                idle = 0
            else:
                idle += 1
                if idle >= 2:
                    return
            time.sleep(.002)
        self.fail("search index did not settle")

    def main_path(self):
        return os.path.join(self.project, CLAUDE_SID + ".jsonl")

    def test_indexes_claude_codex_subagents_artifacts_and_filters(self):
        artifact = os.path.join(self.tmp.name, "artifact.md")
        with open(artifact, "w") as handle:
            handle.write("Artifact-only lighthouse phrase")
        claude_rows = [
            {"type": "ai-title", "aiTitle": "Parser migration"},
            {"type": "user", "timestamp": "2026-07-16T00:00:00Z",
             "cwd": os.path.join(self.tmp.name, "repo"), "gitBranch": "feature/search",
             "message": {"role": "user", "content": "Find the unique parser regression"}},
            {"type": "assistant", "timestamp": "2026-07-16T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "content": [{"type": "text", "text": "Parser fix complete"},
                                     {"type": "tool_use", "name": "SendUserFile",
                                      "id": "tool-1", "input": {"files": [artifact]}}]}},
        ]
        self.write_rows(self.main_path(), claude_rows)
        subagent = os.path.join(self.project, CLAUDE_SID, "subagents", "agent-review.jsonl")
        self.write_rows(subagent, [{"type": "assistant",
            "timestamp": "2026-07-16T00:00:02Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Subagent-only canary finding"}]}}])

        codex_path = os.path.join(self.codex, "2026", "07", "16",
                                   "rollout-2026-07-16T00-00-00-" + CODEX_SID + ".jsonl")
        self.write_rows(codex_path, [
            {"type": "session_meta", "timestamp": "2026-07-16T00:01:00Z",
             "payload": {"id": CODEX_SID, "cwd": os.path.join(self.tmp.name, "codex-repo")}},
            {"type": "event_msg", "timestamp": "2026-07-16T00:01:01Z",
             "payload": {"type": "user_message", "message": "Codex-only zebra request"}},
            {"type": "event_msg", "timestamp": "2026-07-16T00:01:02Z",
             "payload": {"type": "agent_message", "message": "Zebra response", "phase": "final"}},
        ])

        self.drain()
        parser = self.index.search("unique parser")
        self.assertTrue(parser["ok"])
        self.assertEqual(parser["results"][0]["provider"], "claude")
        self.assertEqual(parser["results"][0]["title"], "Parser migration")
        self.assertEqual(parser["results"][0]["branch"], "feature/search")

        agent = self.index.search("canary", kind="message")
        self.assertEqual(agent["results"][0]["source_kind"], "subagent")
        self.assertEqual(agent["results"][0]["agent_id"], "agent-review")

        codex = self.index.search("zebra", provider="codex")
        self.assertTrue(codex["results"])
        self.assertTrue(codex["results"][0]["session_id"].startswith("codex:"))
        self.assertEqual(self.index.search("zebra", provider="claude")["results"], [])

        delivered = self.index.search("lighthouse", kind="artifact")
        self.assertEqual(delivered["results"][0]["artifact_path"], os.path.realpath(artifact))
        artifact_context = self.index.context(delivered["results"][0]["id"], radius=2)
        self.assertEqual(artifact_context["source"]["source_kind"], "artifact")
        self.assertEqual(artifact_context["source"]["artifact_path"], os.path.realpath(artifact))
        context = self.index.context(parser["results"][0]["id"], radius=3)
        self.assertTrue(context["ok"])
        self.assertTrue(any(item["hit"] for item in context["messages"]))
        self.assertEqual(context["source"]["session_id"], CLAUDE_SID)
        handoff = self.index.handoff_material(CLAUDE_SID)
        self.assertTrue(handoff["ok"])
        self.assertEqual(handoff["first_user"], "Find the unique parser regression")
        self.assertEqual([item["role"] for item in handoff["recent"]],
                         ["user", "assistant"])
        self.assertEqual(handoff["artifacts"][0]["path"], os.path.realpath(artifact))
        self.assertFalse(self.index.handoff_material("bad\nvalue")["ok"])

    def test_partial_malformed_append_restart_replace_and_delete(self):
        path = self.main_path()
        first = {"type": "user", "timestamp": "2026-07-16T00:00:00Z",
                 "message": {"role": "user", "content": "durable alpha"}}
        partial = json.dumps({"type": "assistant", "timestamp": "2026-07-16T00:00:01Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "partial beta"}]}}).encode()
        self.write_rows(path, [first, b'{"broken":', partial], final_newline=False)
        self.drain()
        self.assertTrue(self.index.search("alpha")["results"])
        self.assertEqual(self.index.search("beta")["results"], [])
        self.assertGreaterEqual(self.index.status()["malformed_rows"], 1)

        self.append_row(path, b"", newline=True)
        self.index.last_discovery = 0
        self.drain()
        self.assertTrue(self.index.search("beta")["results"])

        before = self.index.status()["documents"]
        self.index.close()
        self.index = SearchIndex(self.db_path, self.claude, self.codex,
                                 discover_seconds=.01, batch_rows=2)
        self.drain()
        self.assertEqual(self.index.status()["documents"], before)

        replacement = {"type": "user", "timestamp": "2026-07-16T00:02:00Z",
                       "message": {"role": "user", "content": "replacement gamma"}}
        self.write_rows(path, [replacement])
        self.index.last_discovery = 0
        self.drain()
        self.assertEqual(self.index.search("alpha")["results"], [])
        self.assertTrue(self.index.search("gamma")["results"])

        os.unlink(path)
        self.index.last_discovery = 0
        self.drain()
        self.assertEqual(self.index.search("gamma")["results"], [])

    def test_unknown_rows_and_unsafe_artifacts_degrade_visibly(self):
        binary = os.path.join(self.tmp.name, "secret.bin")
        with open(binary, "wb") as handle:
            handle.write(b"needle\0binary")
        rows = [
            {"type": "future_protocol_shape", "payload": {"new": True}},
            {"type": "assistant", "timestamp": "2026-07-16T00:00:01Z",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "SendUserFile", "id": "f1",
                  "input": {"files": [binary]}}]}},
        ]
        self.write_rows(self.main_path(), rows)
        self.drain()
        status = self.index.status()
        self.assertGreaterEqual(status["unknown_rows"], 1)
        self.assertGreaterEqual(status["errors"], 1)
        self.assertEqual(self.index.search("needle")["results"], [])
        self.assertTrue(any(item["error"] for item in status["warnings"]))

    def test_missing_artifact_warning_settles_without_permanent_worker_backlog(self):
        missing = os.path.join(self.tmp.name, "removed-artifact.md")
        self.write_rows(self.main_path(), [{"type": "assistant",
            "timestamp": "2026-07-16T00:00:01Z", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "name": "SendUserFile", "id": "gone",
                         "input": {"files": [missing]}}]}}])
        self.drain()
        first = self.index.status()
        self.assertEqual(first["pending_sources"], 0)
        self.assertGreaterEqual(first["errors"], 1)
        for _ in range(3):
            self.index.last_discovery = 0
            self.index.discover()
            self.assertEqual(self.index.status()["pending_sources"], 0)

    def test_confirmed_corrupt_derived_index_is_quarantined_and_rebuilt(self):
        self.index.close()
        with open(self.db_path, "wb") as handle:
            handle.write(b"not a sqlite database")
        recovered = SearchIndex(self.db_path, self.claude, self.codex,
                                discover_seconds=.01, batch_rows=20)
        try:
            status = recovered.status()
            self.assertTrue(status["ok"])
            self.assertEqual(status["documents"], 0)
            self.assertIn("rebuilt", recovered.last_error)
            quarantined = [name for name in os.listdir(self.tmp.name)
                           if name.startswith("search.db.corrupt-")]
            self.assertEqual(len(quarantined), 1)
            recovered.discover()
            while recovered.run_once():
                pass
            self.assertTrue(recovered.search("needle")["ok"])
        finally:
            recovered.close()

    def test_search_prefixes_only_short_typeahead_fragments(self):
        self.assertEqual(self.index._match_query("fle"), '"fle"*')
        self.assertEqual(self.index._match_query("fleet session"),
                         '"fleet" AND "session"')

    def test_adjacent_duplicate_provider_records_index_once(self):
        path = os.path.join(self.codex, "2026", "07", "16",
                            "rollout-2026-07-16T00-00-00-" + CODEX_SID + ".jsonl")
        self.write_rows(path, [
            {"type": "session_meta", "payload": {"id": CODEX_SID,
                                                    "cwd": self.tmp.name}},
            {"type": "event_msg", "timestamp": "2026-07-16T00:01:01Z",
             "payload": {"type": "agent_message", "message": "duplicate canary"}},
            {"type": "response_item", "timestamp": "2026-07-16T00:01:01Z",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "duplicate canary"}]}},
        ])
        self.drain()
        results = self.index.search("duplicate canary")["results"]
        self.assertEqual(len(results), 1)

    def test_background_process_indexes_without_sharing_the_http_process(self):
        self.write_rows(self.main_path(), [{"type": "user",
            "timestamp": "2026-07-16T00:00:00Z",
            "message": {"role": "user", "content": "separate process canary"}}])
        process = self.index.start_process()
        deadline = time.time() + 5
        while time.time() < deadline and self.index.status()["documents"] < 1:
            time.sleep(.02)
        self.assertTrue(self.index.search("separate process")["results"])
        self.assertIsNone(process.poll())
        process.kill()
        process.wait(timeout=2)
        self.append_row(self.main_path(), {"type": "assistant",
            "timestamp": "2026-07-16T00:00:01Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "replacement worker canary"}]}})
        replacement = self.index.ensure_process()
        self.assertNotEqual(process.pid, replacement.pid)
        deadline = time.time() + 5
        while time.time() < deadline and self.index.status()["documents"] < 2:
            time.sleep(.02)
        self.assertTrue(self.index.search("replacement worker")["results"])
        self.index.close()
        self.assertIsNotNone(replacement.poll())

    def test_concurrent_reads_do_not_duplicate_incremental_writes(self):
        rows = [{"type": "user", "timestamp": "2026-07-16T00:00:%02dZ" % (i % 60),
                 "message": {"role": "user", "content": "concurrency token %d" % i}}
                for i in range(80)]
        self.write_rows(self.main_path(), rows)
        errors = []

        def reader():
            for _ in range(40):
                try:
                    self.index.search("concurrency")
                    self.index.status()
                except Exception as exc:
                    errors.append(str(exc))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        self.drain()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with self.index.lock:
            count = self.index._db().execute(
                "SELECT COUNT(*) FROM documents WHERE kind='message'").fetchone()[0]
        self.assertEqual(count, 80)


if __name__ == "__main__":
    unittest.main()
