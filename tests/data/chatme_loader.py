"""
tests/data/chatme_loader.py

ASME Chatme 会话数据加载器。
将 chatme_session_v1 格式的 JSON 转换为 Context GC 可用的 (user, assistant) 轮次列表。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Iterator


def load_chatme_session(path: Path) -> list[tuple[str, str]] | None:
    """
    从单个 chatme JSON 文件加载对话轮次。

    返回: [(user_content, assistant_content), ...]
    若解析失败或格式不符则返回 None。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    meta = data.get("_export_meta", {})
    if meta.get("format") != "chatme_session_v1":
        return None

    messages = data.get("messages", [])
    if not messages:
        return None

    rounds: list[tuple[str, str]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            content = content.strip()
        else:
            content = str(content).strip()

        if role == "user":
            user_content = content
            asst_content = ""
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                asst_content = (messages[i + 1].get("content") or "").strip()
                if isinstance(asst_content, str) is False:
                    asst_content = str(asst_content).strip()
                i += 2
            else:
                i += 1
            if user_content or asst_content:
                rounds.append((user_content, asst_content))
        elif role == "assistant":
            i += 1
        else:
            i += 1

    return rounds if rounds else None


def load_chatme_push_messages(path: Path) -> list[dict] | None:
    """
    加载 chatme 会话中 user/assistant 的完整消息结构（含 steps 等），供 ContextGC.push 使用。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    meta = data.get("_export_meta", {})
    if meta.get("format") != "chatme_session_v1":
        return None
    out: list[dict] = []
    for m in data.get("messages", []):
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        out.append(copy.deepcopy(m))
    return out if out else None


def iter_chatme_files(data_dir: Path) -> Iterator[Path]:
    """遍历 data_dir 下所有 chatme_*.json 文件。"""
    if not data_dir.is_dir():
        return
    for p in sorted(data_dir.iterdir()):
        if p.suffix == ".json" and p.name.startswith("chatme_"):
            yield p


def load_all_chatme_rounds(data_dir: Path) -> list[tuple[str, str, str]]:
    """
    加载 data_dir 下所有 chatme 会话，合并为轮次列表。

    返回: [(user_content, assistant_content, session_id), ...]
    按文件名字典序，每个会话的轮次依次追加。
    """
    result: list[tuple[str, str, str]] = []
    for path in iter_chatme_files(data_dir):
        rounds = load_chatme_session(path)
        if not rounds:
            continue
        sid = path.stem
        for u, a in rounds:
            result.append((u, a, sid))
    return result


def iter_chatme_sessions(data_dir: Path) -> "Iterator[tuple[Path, list[tuple[str, str]]]]":
    """
    遍历每个 chatme 文件， yields (path, rounds)。
    rounds 为 [(user_content, assistant_content), ...]
    """
    for path in iter_chatme_files(data_dir):
        rounds = load_chatme_session(path)
        if rounds:
            yield path, rounds


def iter_chatme_sessions_with_messages(
    data_dir: Path,
) -> "Iterator[tuple[Path, list[tuple[str, str]], list[dict] | None]]":
    """
    同 iter_chatme_sessions，额外 yields 完整 push 消息列表（若有）。
    第三项为 None 时表示仅能用 (user, content) 简版。
    """
    for path in iter_chatme_files(data_dir):
        rounds = load_chatme_session(path)
        if not rounds:
            continue
        raw = load_chatme_push_messages(path)
        yield path, rounds, raw


def build_conversation_from_sessions(
    data_dir: Path,
    *,
    max_rounds: int | None = None,
    min_content_len: int = 0,
    session_ids: list[str] | None = None,
) -> list[tuple[str, str]]:
    """
    从 ASME 数据构建多轮对话，供 Context GC push 使用。

    Args:
        data_dir: 数据目录（如 tests/data）
        max_rounds: 最多使用的轮次数，None 表示全部
        min_content_len: 过滤掉 user+assistant 总长度小于此值的轮次
        session_ids: 若指定，仅加载这些 session（通过文件名包含匹配）

    Returns:
        [(user_content, assistant_content), ...]
    """
    all_rounds = load_all_chatme_rounds(data_dir)
    out: list[tuple[str, str]] = []

    for u, a, sid in all_rounds:
        if session_ids is not None:
            if not any(s in sid for s in session_ids):
                continue
        if min_content_len and len(u) + len(a) < min_content_len:
            continue
        out.append((u, a))
        if max_rounds is not None and len(out) >= max_rounds:
            break

    return out


def build_merged_push_messages(data_dir: Path) -> list[dict]:
    """按文件名字典序拼接所有 chatme 会话的完整消息，用于「合并为一次会话」场景。"""
    merged: list[dict] = []
    for path in iter_chatme_files(data_dir):
        msgs = load_chatme_push_messages(path)
        if msgs:
            merged.extend(msgs)
    return merged
