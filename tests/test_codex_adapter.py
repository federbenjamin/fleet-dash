import unittest

from codex_adapter import CodexAdapter, CodexAppServer, _conversation


class ResponseClient(CodexAppServer):
    def __init__(self):
        super().__init__(command=["false"])
        self.responses = []

    def respond(self, rid, result):
        self.responses.append((rid, result))


class FakeClient:
    def __init__(self):
        self.thread_state = {}
        self.approvals = {}

    def list_threads(self):
        return [{"id": "thr-1", "name": "Fix parser", "cwd": "/work/app",
                 "preview": "Please fix it", "createdAt": 100, "updatedAt": 200,
                 "status": {"type": "notLoaded"},
                 "gitInfo": {"branch": "feature"}}]

    def read_thread(self, thread_id):
        return {"id": thread_id, "turns": [{"items": [
            {"type": "userMessage", "text": "hello"},
            {"type": "agentMessage", "text": "hi"},
            {"type": "commandExecution", "command": "pwd", "status": "completed"},
        ]}]}

    def start_turn(self, thread_id, text):
        self.started = (thread_id, text)

    def interrupt(self, thread_id):
        self.interrupted = thread_id


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.adapter = CodexAdapter(client=self.client)

    def test_sessions_are_provider_qualified(self):
        self.adapter._refresh()
        session = self.adapter.sessions()[0]
        self.assertEqual(session["session_id"], "codex:thr-1")
        self.assertEqual(session["native_session_id"], "thr-1")
        self.assertEqual(session["provider"], "codex")
        self.assertEqual(session["branch"], "feature")
        self.assertEqual(session["cost_source"], "unavailable")

    def test_context_normalizes_messages_and_tools(self):
        out = self.adapter.context("codex:thr-1")
        self.assertTrue(out["ok"])
        self.assertEqual([m["role"] for m in out["messages"]],
                         ["user", "assistant", "tool"])

    def test_actions_strip_provider_prefix(self):
        out = self.adapter.act({"type": "text", "session_id": "codex:thr-1",
                                "text": "continue"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.client.started, ("thr-1", "continue"))

    def test_conversation_ignores_unknown_items(self):
        self.assertEqual(_conversation({"turns": [{"items": [{"type": "reasoning"}]}]}), [])

    def test_codex_question_answers_use_option_labels(self):
        client = ResponseClient()
        client.approvals["9"] = {"request_id": 9, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "scope", "options": [
                {"label": "Full"}, {"label": "Small"}]}]}}
        client.answer_questions("9", [{"digits": [2]}])
        self.assertEqual(client.responses, [(9, {"answers": {
            "scope": {"answers": ["Small"]}}})])

    def test_permission_grants_are_method_specific(self):
        client = ResponseClient()
        client.approvals["7"] = {"request_id": 7,
            "method": "item/permissions/requestApproval",
            "params": {"permissions": {"network": {"enabled": True}, "fileSystem": None}}}
        client.decide("7", "always")
        self.assertEqual(client.responses[0][1], {
            "permissions": {"network": {"enabled": True}}, "scope": "session"})


if __name__ == "__main__":
    unittest.main()
