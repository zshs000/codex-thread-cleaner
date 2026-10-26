#!/usr/bin/env python3
"""Clean a local Codex JSONL record into Markdown."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

TRANSCRIPT_RE = re.compile(r"<transcript_delta\s*>(.*?)</transcript_delta\s*>", re.I | re.S)
SPEAKER_RE = re.compile(r"(?<![A-Za-z0-9_])(user|assistant)\s*:\s*", re.I)
THREAD_ID_RE = re.compile(r"(?i)(?:codex://threads/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def fail(message: str) -> None:
    print(f"clean_thread.py: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def thread_id(value: str | None) -> str | None:
    if not value:
        return None
    match = THREAD_ID_RE.fullmatch(value.strip()) or THREAD_ID_RE.search(value.strip())
    return match.group(1).lower() if match else None


def discover_local_records(expected_id: str, codex_root: Path | None = None) -> list[Path]:
    """Find the Codex JSONL record without calling the app or MCP."""
    root = codex_root or (Path.home() / ".codex")
    search_roots = [root / "sessions", root / "archived_sessions"]
    candidates: list[Path] = []
    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        candidates.extend(search_root.rglob(f"*{expected_id}*.jsonl"))
    if not candidates:
        # The filename normally contains the thread ID. This fallback handles
        # records whose filenames were renamed or archived.
        for search_root in search_roots:
            if not search_root.is_dir():
                continue
            for path in search_root.rglob("*.jsonl"):
                try:
                    with path.open("r", encoding="utf-8", errors="ignore") as handle:
                        head = handle.read(262144)
                except OSError:
                    continue
                if expected_id in head:
                    candidates.append(path)
    if not candidates:
        fail(f"no local Codex record found for thread {expected_id}")
    return sorted(set(candidates))


def merged_records(paths: list[Path], expected_id: str, warnings: list[str], stats: dict[str, int]) -> Iterable[dict[str, Any]]:
    records: list[tuple[str, int, dict[str, Any]]] = []
    for path in paths:
        matched = False
        count = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        warnings.append(f"无法解析 {path.name} 第 {line_number} 行")
                        continue
                    count += 1
                    if record.get("type") == "session_meta":
                        payload = record.get("payload", {})
                        actual = str(payload.get("session_id") or payload.get("id") or "").lower()
                        if actual != expected_id:
                            fail(f"本地记录中的 session_id 与用户提供的 thread ID 不一致：{path}")
                        matched = True
                    records.append((str(record.get("timestamp") or ""), len(records), record))
        except OSError as exc:
            fail(f"cannot read local Codex record: {exc}")
        if not count:
            fail(f"local record is empty: {path}")
        if not matched:
            fail(f"本地记录缺少可校验的 session_meta：{path}")

    # Preserve the first position and latest saved value of each retained item.
    # Equal text with distinct IDs remains ordinary conversational repetition.
    ordered: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for _, _, record in sorted(records, key=lambda entry: (entry[0], entry[1])):
        retained = record.get("metadata", {}).get("retained_source", {})
        identity = retained.get("id") if isinstance(retained, dict) else None
        if identity:
            key = json.dumps(identity, sort_keys=True, ensure_ascii=False)
            if key in positions:
                ordered[positions[key]] = record
                stats["duplicate_records"] += 1
                continue
            positions[key] = len(ordered)
        ordered.append(record)
    yield from ordered


def local_item_turn_id(record: dict[str, Any], payload: dict[str, Any]) -> str:
    metadata = record.get("metadata", {})
    retained = metadata.get("retained_source", {}) if isinstance(metadata, dict) else {}
    retained_id = retained.get("id", {}) if isinstance(retained, dict) else {}
    if isinstance(retained_id, dict) and retained_id.get("turn_id"):
        return str(retained_id["turn_id"])
    passthrough = payload.get("internal_chat_message_metadata_passthrough", {})
    if isinstance(passthrough, dict) and passthrough.get("turn_id"):
        return str(passthrough["turn_id"])
    return "local-record"


def load_local_record(path: Path | list[Path], expected_id: str, stats: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    turns: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    warnings: list[str] = []
    realtime_segments: list[tuple[str, str]] = []
    paths = [path] if isinstance(path, Path) else path
    if stats is None:
        stats = {"duplicate_records": 0}

    for record in merged_records(paths, expected_id, warnings, stats):
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                actual = str(payload.get("session_id") or payload.get("id") or "").lower()
                if actual and actual != expected_id.lower():
                    fail("本地记录中的 session_id 与用户提供的 thread ID 不一致")
                continue

            top_type = record.get("type")
            payload_type = payload.get("type")
            if top_type == "realtime_item" and payload_type == "transcript_segment":
                role = str(payload.get("role", "")).lower()
                text = str(payload.get("text", ""))
                if role in {"user", "assistant"} and text:
                    realtime_segments.append((role, text))
                continue

            item: dict[str, Any] | None = None
            role = ""
            if top_type == "response_item" and payload_type == "message":
                role = str(payload.get("role", "")).lower()
                retained = record.get("metadata", {}).get("retained_source")
                if role not in {"user", "assistant"} or not retained:
                    continue
                if role == "user":
                    item = {"type": "userMessage", "content": payload.get("content", [])}
                else:
                    item = {"type": "agentMessage", "text": block_text(payload.get("content", []))}
            elif top_type == "response_item" and payload_type in {"custom_tool_call", "custom_tool_call_output"}:
                call_type = "commandExecution" if payload_type == "custom_tool_call" else "mcpToolResult"
                item = {
                    "type": call_type,
                    "command": str(payload.get("name") or ""),
                    "output": payload.get("input") if payload_type == "custom_tool_call" else payload.get("output"),
                }
            else:
                continue

            turn_id = local_item_turn_id(record, payload)
            if turn_id not in turns:
                turns[turn_id] = {"id": turn_id, "items": []}
                order.append(turn_id)
            turns[turn_id]["items"].append(item)

    # A live or interrupted realtime session can have transcript segments
    # without a retained response message. Use them only when no messages were
    # persisted, avoiding duplication after a normal realtime flush.
    if not any(turn["items"] for turn in turns.values()) and realtime_segments:
        turn_id = "realtime-fallback"
        turns[turn_id] = {
            "id": turn_id,
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": text}]}
                if role == "user" else {"type": "agentMessage", "text": text}
                for role, text in realtime_segments
            ],
        }
        order = [turn_id]
        warnings.append("未找到已保存的对话消息，使用 realtime transcript_segment 作为回退")
    page = {
        "thread": {"id": expected_id, "title": "Codex 会话"},
        "page": {"order": "oldest_first", "hasMore": False},
        "turns": [turns[turn_id] for turn_id in order],
    }
    return [page], warnings



def block_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(block_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    kind = value.get("type")
    if kind == "text" or "text" in value:
        return str(value.get("text", ""))
    if kind in {"image", "audio", "file", "resource", "resource_link"}:
        name = value.get("name") or value.get("path") or value.get("uri") or value.get("url")
        label = "文件" if kind in {"file", "resource", "resource_link"} else kind
        return f"[{label}{': ' + str(name) if name else ''}]"
    return ""


def normalize(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def voice_parts(text: str) -> list[tuple[str | None, str]]:
    matches = list(TRANSCRIPT_RE.finditer(text))
    if not matches:
        return []
    transcript = "\n".join(match.group(1).strip() for match in matches).strip()
    markers = list(SPEAKER_RE.finditer(transcript))
    if not markers:
        return [(None, transcript)] if transcript else []
    result: list[tuple[str | None, str]] = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(transcript)
        body = transcript[marker.end():end].strip()
        if body:
            result.append((marker.group(1).lower(), body))
    return result


def message_text(item: dict[str, Any]) -> str:
    if item.get("type") == "userMessage":
        return block_text(item.get("content", ""))
    if item.get("type") == "agentMessage":
        return str(item.get("text", ""))
    return ""


def output_text(value: Any) -> str:
    if isinstance(value, dict):
        return block_text(value.get("text", "")) or block_text(value.get("output", ""))
    return block_text(value)


def tool_markdown(item: dict[str, Any]) -> str:
    parts = [f"**工具：{item.get('type', 'event')}**"]
    command = str(item.get("command", "")).strip()
    output = output_text(item.get("output", "")).strip()
    error = str(item.get("error", "")).strip()
    if command:
        parts.append("~~~text\n" + command + "\n~~~")
    if output:
        parts.append("~~~text\n" + output + "\n~~~")
    if error:
        parts.append("~~~text\n" + error + "\n~~~")
    return "\n\n".join(parts)


def ordered_pages(pages: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    return reversed(pages) if pages and pages[0].get("page", {}).get("order") == "newest_first" else pages


def ordered_turns(page: dict[str, Any]) -> Iterable[dict[str, Any]]:
    turns = page.get("turns", [])
    return reversed(turns) if page.get("page", {}).get("order") == "newest_first" else turns


def collect(pages: list[dict[str, Any]], mode: str, keep_meta: bool) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    entries: list[tuple[str, str]] = []
    report: dict[str, Any] = {
        "pages": len(pages), "turns": 0, "messages": 0, "voice_segments": 0,
        "omitted_items": 0, "unknown_types": [], "warnings": [], "complete": True,
    }
    seen_turns: set[str] = set()
    previous_voice: tuple[str, str] | None = None
    for page in ordered_pages(pages):
        if page.get("page", {}).get("hasMore") is True:
            report["complete"] = False
            report["warnings"].append("输入仍有未读取的分页")
        for turn in ordered_turns(page):
            if not isinstance(turn, dict):
                continue
            key = str(turn.get("id") or hashlib.sha1(
                json.dumps(turn, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest())
            if key in seen_turns:
                continue
            seen_turns.add(key)
            report["turns"] += 1
            if keep_meta and turn.get("durationMs") is not None:
                entries.append(("meta", f"> 用时 {float(turn['durationMs']) / 1000:g} 秒"))
            for item in turn.get("items", []):
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind in {"reasoning", "plan", "reasoningSummary"}:
                    report["omitted_items"] += 1
                    continue
                if kind in {"userMessage", "agentMessage"}:
                    raw = message_text(item)
                    if kind == "agentMessage" and re.match(r"^\s*\[STATUS\]", raw, flags=re.I):
                        report["omitted_items"] += 1
                        continue
                    parts = voice_parts(raw)
                    if parts:
                        for role, body in parts:
                            body = normalize(body)
                            if not body:
                                continue
                            current = (role or ("user" if kind == "userMessage" else "assistant"), body)
                            if current == previous_voice:
                                continue
                            entries.append(current)
                            previous_voice = current
                            report["voice_segments"] += 1
                        continue
                    body = normalize(re.sub(r"<realtime_delegation\b[^>]*>.*?</realtime_delegation>", "", raw, flags=re.I | re.S))
                    body = re.sub(r"^\s*\[(?:COMPLETE|STATUS)\]\s*", "", body, flags=re.I)
                    if body:
                        entries.append(("user" if kind == "userMessage" else "assistant", body))
                    continue
                if kind in {"commandExecution", "mcpToolCall", "mcpToolResult", "fileChange", "toolCall"}:
                    if mode == "context":
                        block = tool_markdown(item)
                        if block:
                            entries.append(("tool", block))
                        else:
                            report["omitted_items"] += 1
                    else:
                        report["omitted_items"] += 1
                    continue
                report["omitted_items"] += 1
                if kind:
                    report["unknown_types"].append(str(kind))
    report["unknown_types"] = sorted(set(report["unknown_types"]))
    report["warnings"] = list(dict.fromkeys(report["warnings"]))
    report["messages"] = sum(role in {"user", "assistant"} for role, _ in entries)
    if report["unknown_types"]:
        report["warnings"].append("存在未识别的消息类型：" + ", ".join(report["unknown_types"]))
    return entries, report


def render(title: str, entries: list[tuple[str, str]]) -> str:
    lines = [f"# {normalize(title) or 'Codex 会话'}", ""]
    for role, body in entries:
        if role == "user":
            lines += ["### 用户", "", body, ""]
        elif role == "assistant":
            lines += ["### AI", "", body, ""]
        elif role == "tool":
            lines += ["### 工具结果", "", body, ""]
        elif role == "meta":
            lines += [body, ""]
    if not entries:
        lines += ["（未找到可导出的对话正文。）", ""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean a local Codex thread record into Markdown.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--thread-id")
    parser.add_argument("--codex-root", type=Path, help="Override the local .codex directory.")
    parser.add_argument("--mode", choices=("screen", "context"), default="screen")
    parser.add_argument("--keep-visible-meta", action="store_true")
    args = parser.parse_args()

    expected = thread_id(args.thread_id)
    if not expected:
        fail("--thread-id is required when reading the local Codex record")
    source_paths = discover_local_records(expected, args.codex_root)
    merge_stats = {"duplicate_records": 0}
    pages, source_warnings = load_local_record(source_paths, expected, merge_stats)
    actual = {str(page.get("thread", {}).get("id", "")).lower() for page in pages if page.get("thread", {}).get("id")}
    if expected and actual and expected not in actual:
        fail("输入内容与 --thread-id 不一致")
    if expected and not actual:
        fail("输入中没有可校验的会话 ID")
    title = next((str(page["thread"]["title"]) for page in pages if page.get("thread", {}).get("title")), "Codex 会话")
    entries, report = collect(pages, args.mode, args.keep_visible_meta)
    if source_paths:
        report["sources"] = [str(path) for path in source_paths]
        report["source_count"] = len(source_paths)
        report.update(merge_stats)
        if len(source_paths) == 1:
            report["source"] = str(source_paths[0])
        report["warnings"].extend(source_warnings)
        first_user = next((body for role, body in entries if role == "user"), "")
        if title == "Codex 会话" and first_user:
            title = normalize(first_user.splitlines()[0])[:60]
    report["warnings"] = list(dict.fromkeys(report["warnings"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(title, entries), encoding="utf-8")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "report": report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
