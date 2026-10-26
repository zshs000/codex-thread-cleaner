import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import clean_thread as cleaner


THREAD = "11111111-1111-4111-8111-111111111111"


def message(number, role, text, turn="turn-1", identity=None):
    return {
        "timestamp": f"2026-10-26T10:00:{number:02d}.000Z",
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": "text", "text": text}]},
        "metadata": {"retained_source": {"id": {"turn_id": turn, "message_id": identity or str(number), "role": role}}},
    }


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def segment(self, name, records, thread=THREAD):
        path = self.root / name
        meta = {"type": "session_meta", "payload": {"session_id": thread}}
        path.write_text("\n".join(json.dumps(item) for item in [meta, *records]) + "\n", encoding="utf-8")
        return path

    def entries(self, paths):
        stats = {"duplicate_records": 0}
        pages, warnings = cleaner.load_local_record(paths, THREAD, stats)
        entries, _ = cleaner.collect(pages, "screen", False)
        self.assertEqual(warnings, [])
        return entries, stats

    def test_merge_continuation_of_same_turn_in_time_order(self):
        first = self.segment("a.jsonl", [message(1, "user", "question")])
        second = self.segment("b.jsonl", [message(2, "assistant", "answer")])
        entries, _ = self.entries([second, first])
        self.assertEqual(entries, [("user", "question"), ("assistant", "answer")])

    def test_shared_message_latest_value_and_original_position(self):
        first = self.segment("a.jsonl", [message(1, "user", "question"), message(2, "assistant", "partial", identity="reply")])
        second = self.segment("b.jsonl", [message(3, "assistant", "complete", identity="reply"), message(4, "user", "next", turn="turn-2")])
        entries, stats = self.entries([first, second])
        self.assertEqual(entries, [("user", "question"), ("assistant", "complete"), ("user", "next")])
        self.assertEqual(stats["duplicate_records"], 1)

    def test_equal_text_with_distinct_ids_is_preserved(self):
        first = self.segment("a.jsonl", [message(1, "user", "again")])
        second = self.segment("b.jsonl", [message(2, "user", "again", turn="turn-2")])
        entries, stats = self.entries([first, second])
        self.assertEqual(entries, [("user", "again"), ("user", "again")])
        self.assertEqual(stats["duplicate_records"], 0)

    def test_single_segment_and_source_unchanged(self):
        path = self.segment("single.jsonl", [message(1, "user", "hello")])
        original = path.read_bytes()
        self.assertEqual(self.entries(path)[0], [("user", "hello")])
        self.assertEqual(path.read_bytes(), original)

    def test_reject_other_session(self):
        path = self.segment("other.jsonl", [], thread="other")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.entries([path])


if __name__ == "__main__":
    unittest.main()
