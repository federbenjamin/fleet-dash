import unittest

from fleetdash.card_preview import (
    CARD_PEEK_TYPES,
    card_peek_rows,
)


class CardPreviewTests(unittest.TestCase):
    def test_every_full_chat_row_type_is_enabled_and_keeps_order(self):
        messages = [
            {"role": "user", "text": "Start here"},
            {"role": "tool", "name": "Read", "arg": "/repo/app.py",
             "result": "20 lines"},
            {"role": "event", "kind": "compact", "title": "Compacted",
             "detail": "automatic compaction"},
            {"role": "assistant", "text": "Finished"},
        ]

        rows = card_peek_rows(messages)

        self.assertEqual(CARD_PEEK_TYPES,
                         frozenset(("user", "assistant", "tool", "event")))
        self.assertEqual([row["type"] for row in rows],
                         ["user", "tool", "event", "assistant"])
        self.assertEqual(rows[1]["label"], "Read")
        self.assertEqual(rows[1]["text"], "app.py")
        self.assertEqual(rows[2]["text"], "automatic compaction")

    def test_filter_is_one_explicit_include_set(self):
        messages = [
            {"role": "user", "text": "Question"},
            {"role": "tool", "name": "Bash", "arg": "run tests"},
            {"role": "event", "title": "Compacted"},
            {"role": "assistant", "text": "Answer"},
        ]

        rows = card_peek_rows(messages, include={"tool", "event"})

        self.assertEqual([row["type"] for row in rows], ["tool", "event"])

    def test_newest_long_message_keeps_its_beginning_within_budget(self):
        rows = card_peek_rows([
            {"role": "tool", "name": "Read", "arg": "older"},
            {"role": "assistant", "text": "BEGIN-" + "x" * 200 + "-END"},
        ], max_chars=80)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["text"].startswith("BEGIN-"))
        self.assertNotIn("-END", rows[0]["text"])
        self.assertTrue(rows[0]["text"].endswith("…"))

    def test_older_rows_fill_remaining_budget_and_files_use_basenames(self):
        rows = card_peek_rows([
            {"role": "tool", "name": "SendUserFile",
             "files": ["/private/session/report.md", {"name": "chart.png"}]},
            {"role": "assistant", "text": "Done"},
        ])

        self.assertEqual([row["type"] for row in rows], ["tool", "assistant"])
        self.assertEqual(rows[0]["text"], "report.md +1")

    def test_tool_rows_use_specific_first_line_summaries_without_results(self):
        rows = card_peek_rows([
            {"role": "tool", "name": "Bash",
             "arg": "Flip PR to ready and arm auto-merge\nignored command line",
             "result": "large command output that must not enter the peek"},
            {"role": "tool", "name": "Write", "arg": "/truncated/f82f4…",
             "peek_arg": "session-report.md", "result": "created successfully"},
            {"role": "tool", "name": "fileChange",
             "arg": '[{"path":"src/dashboard/cards.js","kind":"update"}]',
             "result": "completed", "failed": True},
            {"role": "tool", "name": "commandExecution",
             "command": "npm run test:browser\nsecond command"},
        ])

        self.assertEqual(rows, [
            {"type": "tool", "label": "Bash",
             "text": "Flip PR to ready and arm auto-merge", "failed": False},
            {"type": "tool", "label": "Write",
             "text": "session-report.md", "failed": False},
            {"type": "tool", "label": "Edit", "text": "cards.js", "failed": True},
            {"type": "tool", "label": "Bash", "text": "npm run test:browser",
             "failed": False},
        ])

    def test_question_answer_event_is_flattened_for_the_peek(self):
        rows = card_peek_rows([{
            "role": "event", "kind": "qa", "title": "You answered",
            "qa": [
                "ignore malformed answer",
                {"header": "Scope", "q": "Which cards?", "a": "All"},
            ],
        }])

        self.assertEqual(rows[0]["text"], "Scope · Which cards?: All")

    def test_malformed_and_empty_rows_are_safely_skipped(self):
        rows = card_peek_rows([
            {"role": "custom", "text": "unsupported"},
            {"role": "assistant", "text": ""},
            {"role": "event", "title": "Recovered", "n": "not-a-number"},
        ], include={"custom", "assistant", "event"})

        self.assertEqual(rows, [{
            "type": "event",
            "kind": "event",
            "label": "Recovered",
            "text": "",
            "level": "info",
            "n": 1,
        }])


if __name__ == "__main__":
    unittest.main()
