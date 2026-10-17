"""Curated, local-only Codex session timelines for the project dashboard.

The rollout files contain private prompts, tool output, and encrypted reasoning.
Never return a raw rollout or tool output from this module.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.I)
SECRET = re.compile(
    r"(?i)(\b(?:authorization\s*:\s*bearer|bearer)\s+)\S+"
    r"|(\b(?:access_token|refresh_token|device_code|api_key|client_secret|app_secret|private_key|secret|password|passphrase|cookie|bridge_token)\b[\"']?\s*[:=]\s*[\"']?)[^\s\"'&}]+"
    r"|\bsk-[A-Za-z0-9_-]{12,}\b"
)
QUERY_SECRET = re.compile(r"(?i)([?&](?:access_token|refresh_token|device_code|code|api_key|key|token)=)[^&#\s]+")
TOOL_NAME = re.compile(r"tools\.([A-Za-z][A-Za-z0-9_]*)")
READ_COMMAND = re.compile(r"^(?:rg|grep|sed|cat|head|tail|ls|find|pwd|stat|wc|git\s+(?:status|diff|log|show))\b", re.I)


def scrub(value: str, limit=1600) -> str:
    """Redact common credentials and cap all visible free text."""
    value = QUERY_SECRET.sub(r"\1[已隐藏]", value)
    value = SECRET.sub(lambda match: (match.group(1) or match.group(2) or "") + "[已隐藏]", value)
    value = value.replace("\x00", "")
    return value[:limit] + ("… [内容已截断]" if len(value) > limit else "")


def workspace_for(cwd: str, workspaces: dict[str, Path]) -> str | None:
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return None
    try:
        path = Path(cwd).resolve()
    except (OSError, ValueError):
        return None
    for name, directory in workspaces.items():
        parent = Path(directory).resolve()
        if path == parent or path.is_relative_to(parent):
            return name
    return None


class TraceReader:
    def __init__(self, workspaces: dict[str, Path], sessions: Path):
        self.workspaces = {name: Path(path).resolve() for name, path in workspaces.items()}
        self.sessions = Path(sessions).resolve()

    def _index(self):
        if not self.sessions.is_dir():
            return []
        entries = []
        for path in self.sessions.rglob("rollout-*.jsonl"):
            try:
                if not path.resolve().is_relative_to(self.sessions):
                    continue
                with path.open(encoding="utf-8") as stream:
                    header = json.loads(stream.readline())
                if header.get("type") != "session_meta":
                    continue
                meta = header["payload"]
                sid = meta.get("id") or meta.get("session_id")
                workspace = workspace_for(meta.get("cwd", ""), self.workspaces)
                if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) or not workspace:
                    continue
                stat = path.stat()
                entries.append(({
                    "id": sid,
                    "workspace": workspace,
                    "started_at": header.get("timestamp"),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "agent": scrub(str(meta.get("agent_nickname") or "主会话"), 80),
                    "parent_id": meta.get("parent_thread_id") if SESSION_ID.fullmatch(str(meta.get("parent_thread_id", ""))) else None,
                }, path))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        entries.sort(key=lambda item: item[0]["updated_at"] or "", reverse=True)
        return entries

    def list_sessions(self):
        return {
            "workspaces": list(self.workspaces),
            "sessions": [entry for entry, _ in self._index()],
        }

    def read_session(self, session_id: str, cursor=0, limit=80):
        if not SESSION_ID.fullmatch(session_id) or not isinstance(cursor, int) or cursor < 0 or cursor > 1000000:
            raise ValueError("invalid session or cursor")
        found = next(((entry, path) for entry, path in self._index() if entry["id"] == session_id), None)
        if found is None:
            raise FileNotFoundError(session_id)
        entry, path = found
        events = []
        next_cursor = None
        turns = 0
        tool_calls = 0
        token_total = None
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream):
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A live session may end with a partial line.
                payload = raw.get("payload") or {}
                if raw.get("type") == "event_msg" and payload.get("type") == "task_started":
                    turns += 1
                if raw.get("type") == "response_item" and payload.get("type") in {"custom_tool_call", "function_call"}:
                    tool_calls += 1
                if raw.get("type") == "token_usage_record":
                    value = (payload.get("thread_token_usage") or {}).get("total_tokens")
                    if isinstance(value, int) and value >= 0:
                        token_total = value
                if line_number < cursor:
                    continue
                event = self._event(raw)
                if event is None:
                    continue
                if len(events) >= limit:
                    if next_cursor is None:
                        next_cursor = line_number
                    continue
                event["line"] = line_number + 1
                events.append(event)
        return {"session": entry, "summary": {"turns": turns, "tool_calls": tool_calls,
                                               "total_tokens": token_total},
                "events": events, "next_cursor": next_cursor}

    def read_flow(self, session_id: str):
        """Align a root session and its child agents on one observed clock."""
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session")
        indexed = self._index()
        by_id = {entry["id"]: (entry, path) for entry, path in indexed}
        if session_id not in by_id:
            raise FileNotFoundError(session_id)
        selected = by_id[session_id][0]
        workspace = selected["workspace"]
        root_id = session_id
        visited = {root_id}
        while True:
            parent = by_id[root_id][0]["parent_id"]
            if not parent or parent in visited or parent not in by_id or by_id[parent][0]["workspace"] != workspace:
                break
            root_id = parent
            visited.add(root_id)
        root = by_id[root_id][0]
        # A parent launched outside the indexed workspaces can still have
        # several children in this one workspace; show those siblings together.
        if root["parent_id"] and root["parent_id"] not in by_id:
            group = {entry["id"] for entry, _ in indexed
                     if entry["workspace"] == workspace and entry["parent_id"] == root["parent_id"]}
        else:
            group = {root_id}
        changed = True
        while changed:
            before = len(group)
            group.update(entry["id"] for entry, _ in indexed
                         if entry["workspace"] == workspace and entry["parent_id"] in group)
            changed = len(group) != before
        order = sorted(group, key=lambda sid: (sid != root_id, by_id[sid][0]["started_at"] or "", sid))
        lanes = [self._flow_lane(*by_id[sid]) for sid in order]
        valid = [lane for lane in lanes if lane["start_ms"] is not None]
        if not valid:
            raise ValueError("no timed events")
        return {
            "workspace": workspace,
            "root_id": root_id,
            "selected_id": session_id,
            "start_ms": min(lane["start_ms"] for lane in valid),
            "end_ms": max(lane["end_ms"] for lane in valid),
            "lanes": lanes,
        }

    @staticmethod
    def _milliseconds(value):
        if isinstance(value, (int, float)):
            return int(value * 1000)
        if isinstance(value, str):
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                pass
        return None

    @staticmethod
    def _call_category(payload):
        name = scrub(str(payload.get("name") or "tool"), 80)
        if name in {"apply_patch", "send_message", "followup_task"}:
            return "write", name
        if name == "write_stdin":
            return "run", name
        if name in {"view_image", "web__run", "list_mcp_resources", "read_mcp_resource"}:
            return "read", name
        if name == "exec":
            source = payload.get("input", "")
            nested = set(TOOL_NAME.findall(source))
            if "apply_patch" in nested:
                return "write", "apply_patch"
            if "exec_command" in nested:
                match = re.search(r"\bcmd\s*:\s*['\"]([^'\"\n]{1,100})", source)
                command = match.group(1).strip() if match else ""
                if READ_COMMAND.match(command):
                    return "read", "exec_command"
                return "run", "exec_command"
            if nested and all(tool in {"view_image", "web__run", "read_mcp_resource", "list_mcp_resources"} for tool in nested):
                return "read", ", ".join(sorted(nested))
            return "run", "exec"
        if name in {"exec_command", "shell_command"}:
            command = str(payload.get("arguments", ""))
            return ("read" if READ_COMMAND.search(command) else "run"), name
        return "run", name

    @classmethod
    def _flow_lane(cls, entry, path):
        starts = {}
        turns = []
        open_turn = None
        slices = []
        first = last = None
        token_total = None
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = cls._milliseconds(raw.get("timestamp"))
                if stamp is not None:
                    first = stamp if first is None else min(first, stamp)
                    last = stamp if last is None else max(last, stamp)
                payload = raw.get("payload") or {}
                kind = raw.get("type")
                item_type = payload.get("type")
                if kind == "event_msg" and item_type == "task_started":
                    open_turn = cls._milliseconds(payload.get("started_at")) or stamp
                elif kind == "event_msg" and item_type == "task_complete" and open_turn is not None:
                    end = cls._milliseconds(payload.get("completed_at")) or stamp
                    if end is not None and end >= open_turn:
                        turns.append({"start_ms": open_turn, "end_ms": end})
                    open_turn = None
                elif kind == "token_usage_record":
                    value = (payload.get("thread_token_usage") or {}).get("total_tokens")
                    if isinstance(value, int) and value >= 0:
                        token_total = value
                elif kind == "response_item" and item_type in {"custom_tool_call", "function_call"}:
                    call_id = payload.get("call_id")
                    if call_id and stamp is not None:
                        category, label = cls._call_category(payload)
                        starts[call_id] = (stamp, category, label, line_number)
                elif kind == "response_item" and item_type in {"custom_tool_call_output", "function_call_output"}:
                    previous = starts.pop(payload.get("call_id"), None)
                    if previous and stamp is not None:
                        began, category, label, source_line = previous
                        slices.append({"start_ms": began, "end_ms": max(began + 1, stamp),
                                       "kind": category, "label": label, "line": source_line})
        if first is None:
            first = cls._milliseconds(entry["started_at"])
        if last is None:
            last = first
        if first is not None and last is not None:
            for began, category, label, source_line in starts.values():
                slices.append({"start_ms": began, "end_ms": max(began + 1, last),
                               "kind": category, "label": label, "line": source_line, "open": True})
            if open_turn is not None:
                turns.append({"start_ms": open_turn, "end_ms": max(open_turn + 1, last)})
        slices.sort(key=lambda item: (item["start_ms"], item["line"]))
        return {"id": entry["id"], "agent": entry["agent"], "parent_id": entry["parent_id"],
                "start_ms": first, "end_ms": last, "token_total": token_total,
                "turns": turns, "slices": slices}

    @staticmethod
    def _event(raw):
        kind = raw.get("type")
        payload = raw.get("payload") or {}
        timestamp = raw.get("timestamp")
        item_type = payload.get("type")
        if kind == "response_item" and item_type == "message":
            role = payload.get("role")
            if role not in {"user", "assistant"} or payload.get("channel") == "analysis":
                return None
            texts = [part.get("text", "") for part in payload.get("content", [])
                     if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}]
            content = "\n".join(texts).strip()
            if role == "user":
                if "<user_input>" in content:
                    match = re.search(r"<user_input>\s*(.*?)\s*</user_input>", content, re.S)
                    if not match:
                        return None
                    try:
                        content = json.loads(match.group(1)).get("text", "")
                    except (json.JSONDecodeError, AttributeError):
                        return None
                    content = re.sub(r"^\[[^\n]+ \((?:user|bot)\)\]:\s*", "", content, flags=re.M)
                elif "<bridge_context>" in content or "<environment_context>" in content:
                    return None
            if not content:
                return None
            return {"at": timestamp, "kind": "message", "label": "用户" if role == "user" else "Agent", "detail": scrub(content)}
        if kind == "response_item" and item_type in {"custom_tool_call", "function_call"}:
            name = scrub(str(payload.get("name") or "tool"), 80)
            nested = sorted(set(TOOL_NAME.findall(payload.get("input", "")))) if name == "exec" else []
            detail = "调用 " + ", ".join(nested[:8]) if nested else "调用 " + name
            if len(nested) > 8:
                detail += f" 等 {len(nested)} 个工具"
            return {"at": timestamp, "kind": "tool", "label": name, "detail": detail,
                    "call_id": str(payload.get("call_id") or "")}
        if kind == "response_item" and item_type in {"custom_tool_call_output", "function_call_output"}:
            output = payload.get("output", "")
            if isinstance(output, list):
                output = "\n".join(part.get("text", "") for part in output if isinstance(part, dict))
            # Only report completion status. Tool output may contain credentials or private files.
            failed = "isError\":true" in output or "exit_code\":1" in output or "Script failed" in output
            return {"at": timestamp, "kind": "result", "label": "工具结果", "detail": "执行失败，需查看本机原始日志" if failed else "调用已返回（原始输出未公开）",
                    "call_id": str(payload.get("call_id") or "")}
        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                return {"at": timestamp, "kind": "turn", "label": "新一轮", "detail": scrub(model, 80)}
        if kind == "event_msg" and item_type == "task_complete":
            duration = payload.get("duration_ms")
            detail = f"本轮完成 · {duration / 1000:.1f} 秒" if isinstance(duration, (int, float)) else "本轮完成"
            return {"at": timestamp, "kind": "turn", "label": "完成", "detail": detail}
        return None
