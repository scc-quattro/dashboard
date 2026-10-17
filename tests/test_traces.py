import json
import tempfile
import unittest
from pathlib import Path

from traces import TraceReader, scrub


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        (self.root / "demo-project").mkdir()
        self.sid = "01234567-89ab-cdef-0123-456789abcdef"
        self.path = self.sessions / f"rollout-test-{self.sid}.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def row(self, type_, payload, at="2026-10-17T01:00:00Z"):
        return json.dumps({"timestamp": at, "type": type_, "payload": payload})

    def write(self, cwd):
        rows = [
            self.row("session_meta", {"id": self.sid, "cwd": str(cwd), "agent_nickname": "probe"}),
            self.row("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "private policy"}]}),
            self.row("event_msg", {"type": "task_started"}),
            self.row("response_item", {"type": "reasoning", "summary": [{"text": "private reasoning"}]}),
            self.row("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": '<bridge_context>private metadata</bridge_context><user_input>{"text":"[Alice (user)]: Check api_key=abc123"}</user_input>'}]}),
            self.row("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "one", "input": 'await tools.exec_command({cmd:"cat /tmp/example/.env"})'}),
            self.row("response_item", {"type": "custom_tool_call_output", "call_id": "one", "output": [{"type": "input_text", "text": "ACCESS_TOKEN=very-secret-value"}]}),
            self.row("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checked result; Bearer top-secret-value"}]}),
            self.row("token_usage_record", {"thread_token_usage": {"total_tokens": 4321}}),
        ]
        self.path.write_text("\n".join(rows) + "\n")

    def test_index_only_in_allowlisted_workspace(self):
        reader = TraceReader({"demo-project": self.root / "demo-project"}, self.sessions)
        self.write(self.root / "other")
        self.assertEqual(reader.list_sessions()["sessions"], [])
        self.write(self.root / "demo-project")
        sessions = reader.list_sessions()["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["workspace"], "demo-project")

    def test_timeline_excludes_reasoning_raw_output_and_bridge_metadata(self):
        self.write(self.root / "demo-project")
        reader = TraceReader({"demo-project": self.root / "demo-project"}, self.sessions)
        result = reader.read_session(self.sid)
        text = json.dumps(result, ensure_ascii=False)
        for private in ["private policy", "private reasoning", "private metadata", "cat /home", "very-secret-value", "abc123", "top-secret-value"]:
            self.assertNotIn(private, text)
        self.assertIn("exec_command", text)
        self.assertIn("[已隐藏]", text)
        self.assertEqual([e["kind"] for e in result["events"]], ["message", "tool", "result", "message"])
        self.assertEqual(result["summary"], {"turns": 1, "tool_calls": 1, "total_tokens": 4321})

    def test_cursor_and_invalid_id(self):
        self.write(self.root / "demo-project")
        reader = TraceReader({"demo-project": self.root / "demo-project"}, self.sessions)
        first = reader.read_session(self.sid, limit=2)
        self.assertEqual(len(first["events"]), 2)
        second = reader.read_session(self.sid, cursor=first["next_cursor"])
        self.assertEqual(len(second["events"]), 2)
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(ValueError):
            reader.read_session("../../etc/passwd")

    def test_flow_aligns_parent_and_child_without_exposing_tool_input(self):
        self.write(self.root / "demo-project")
        child_id = "11111111-2222-3333-4444-555555555555"
        child = self.sessions / f"rollout-test-{child_id}.jsonl"
        child.write_text("\n".join([
            self.row("session_meta", {"id": child_id, "cwd": str(self.root / "demo-project"),
                                      "parent_thread_id": self.sid, "agent_nickname": "reviewer"}, "2026-10-17T01:00:01Z"),
            self.row("event_msg", {"type": "task_started", "started_at": 1792198801}, "2026-10-17T01:00:01Z"),
            self.row("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "two",
                                       "input": 'await tools.apply_patch("private code")'}, "2026-10-17T01:00:02Z"),
            self.row("response_item", {"type": "custom_tool_call_output", "call_id": "two",
                                       "output": "secret output"}, "2026-10-17T01:00:04Z"),
            self.row("event_msg", {"type": "task_complete", "completed_at": 1792198805}, "2026-10-17T01:00:05Z"),
        ]) + "\n")
        result = TraceReader({"demo-project": self.root / "demo-project"}, self.sessions).read_flow(child_id)
        self.assertEqual(len(result["lanes"]), 2)
        self.assertEqual(result["lanes"][1]["agent"], "reviewer")
        self.assertEqual(result["lanes"][1]["slices"][0]["kind"], "write")
        self.assertEqual(result["lanes"][1]["slices"][0]["line"], 3)
        self.assertNotIn("private code", json.dumps(result))
        self.assertNotIn("secret output", json.dumps(result))

    def test_scrub_query_secrets(self):
        self.assertEqual(scrub("https://example.org?token=abc&x=1"), "https://example.org?token=[已隐藏]&x=1")
        self.assertEqual(scrub('{"client_secret":"abc123"}'), '{"client_secret":"[已隐藏]"}')


if __name__ == "__main__":
    unittest.main()
