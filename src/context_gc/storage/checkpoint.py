"""
context_gc/storage/checkpoint.py

Checkpoint 管理：每 N 轮将 state 增量写入磁盘，崩溃后可恢复。

文件布局::

    {data_dir}/sessions/{session_id}/
    ├── .checkpoint.json   # summaries + gen_scores + round_count
    └── content.md         # 原始消息（append-only，每轮追加）

写入策略：
- raw_messages → 每轮 close() 后追加到 content.md
- summaries + gen_scores → 每 checkpoint_interval 轮全量覆写 .checkpoint.json

Checkpoint 是 ContextGC 的内部行为，不经过 MemoryBackend 协议。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..state import RoundMeta, ContextGCState


class CheckpointManager:
    """管理 checkpoint 的写入、恢复、清理。"""

    def __init__(
        self,
        data_dir: str | Path,
        session_id: str,
        *,
        checkpoint_interval: int = 5,
        checkpoint_raw_messages: bool = True,
    ) -> None:
        self.session_dir = Path(data_dir) / "sessions" / session_id
        self.session_id = session_id
        self.checkpoint_interval = checkpoint_interval
        self.checkpoint_raw_messages = checkpoint_raw_messages
        self._rounds_since_last_checkpoint = 0
        self.session_dir.mkdir(parents=True, exist_ok=True)

    @property
    def checkpoint_path(self) -> Path:
        return self.session_dir / ".checkpoint.json"

    @property
    def content_path(self) -> Path:
        return self.session_dir / "content.md"

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def on_round_close(
        self,
        state: ContextGCState,
        round_messages: list[dict],
    ) -> None:
        """每轮 close() 后调用：追加 raw_messages + 按间隔写 checkpoint。"""
        if self.checkpoint_interval <= 0:
            return

        if self.checkpoint_raw_messages:
            self._append_raw_messages(round_messages)

        self._rounds_since_last_checkpoint += 1
        if self._rounds_since_last_checkpoint >= self.checkpoint_interval:
            self._write_checkpoint(state)
            self._rounds_since_last_checkpoint = 0

    def _append_raw_messages(self, messages: list[dict]) -> None:
        """追加原始消息到 content.md（append-only）。"""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            lines.append(f"### {role}\n\n{content}\n")
        block = "\n".join(lines) + "\n---\n\n"

        with open(self.content_path, "a", encoding="utf-8") as f:
            f.write(block)

    def _write_checkpoint(self, state: ContextGCState) -> None:
        """全量覆写 .checkpoint.json。"""
        data = {
            "session_id": self.session_id,
            "round_count": len(state.rounds),
            "summaries": [r.summary for r in state.rounds],
            "gen_scores": [r.gen_score for r in state.rounds],
            "token_counts": [r.token_count for r in state.rounds],
            "round_ids": [r.round_id for r in state.rounds],
            "is_merged": [r.is_merged for r in state.rounds],
            "merged_round_ids": [r.merged_round_ids for r in state.rounds],
            "last_triggered_ratio": state.last_triggered_ratio,
            "last_checkpoint_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(self.checkpoint_path))

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def try_recover(self, max_tokens: int, capacity_threshold: float) -> Optional[ContextGCState]:
        """
        尝试从 checkpoint 恢复 state。

        Returns:
            恢复后的 ContextGCState，若无 checkpoint 则返回 None。
        """
        if not self.checkpoint_path.exists():
            return None

        data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        summaries = data.get("summaries", [])
        gen_scores = data.get("gen_scores", [])
        token_counts = data.get("token_counts", [0] * len(summaries))
        round_ids = data.get("round_ids", list(range(1, len(summaries) + 1)))
        is_merged_list = data.get("is_merged", [False] * len(summaries))
        merged_ids_list = data.get("merged_round_ids", [[] for _ in summaries])

        rounds: list[RoundMeta] = []
        for i, summary in enumerate(summaries):
            rounds.append(RoundMeta(
                round_id=round_ids[i] if i < len(round_ids) else i + 1,
                summary=summary,
                gen_score=gen_scores[i] if i < len(gen_scores) else 0,
                token_count=token_counts[i] if i < len(token_counts) else 0,
                is_merged=is_merged_list[i] if i < len(is_merged_list) else False,
                merged_round_ids=merged_ids_list[i] if i < len(merged_ids_list) else [],
            ))

        state = ContextGCState(
            rounds=rounds,
            max_tokens=max_tokens,
            capacity_threshold=capacity_threshold,
            last_triggered_ratio=data.get("last_triggered_ratio", 0.1),
        )
        state.recalc_total_tokens()
        return state

    def load_raw_messages(self) -> str:
        """加载已持久化的原始消息文本。"""
        if self.content_path.exists():
            return self.content_path.read_text(encoding="utf-8")
        return ""

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """会话正常结束后清理 checkpoint 文件（content.md 保留作为 L2）。"""
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
