"""
context_gc/core.py

ContextGC 主类：对外暴露 push / close / get_messages 三个宿主接口，
以及持久化（on_session_end / find / load_*）和查询 API。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional, Any

from .state import RoundMeta, ContextGCState
from .generational import (
    get_current_user_text,
    update_generational_scores,
    DEFAULT_GEN_SCORE_DECAY,
    DEFAULT_GEN_SCORE_CLAMP,
    DEFAULT_SCORING_INTERVAL,
)
from .compaction import (
    check_capacity_and_compact,
    build_messages_from_state,
    truncate_to_fit,
    get_max_output_chars,
)
from .storage.backend import MemoryBackend, UserPreference, UserExperience
from .storage.checkpoint import CheckpointManager
from .memory.preference import PreferenceDetector


DEFAULT_MERGE_GRADIENT_BY_TOKENS = [
    (500, 0.0),
    (2000, 0.25),
    (5000, 0.15),
    (15000, 0.08),
    (999999, -1500),
]


@dataclass
class ContextGCOptions:
    """
    ContextGC 配置项。

    回调均由实现方注入；持久化相关配置可选。
    """

    max_input_tokens: int
    generate_summary: Callable[..., Awaitable[str]]
    merge_summary: Callable[..., Awaitable[str]]
    compute_relevance: Callable[[str, list[str]], Awaitable[list[float]]]
    estimate_tokens: Callable[[object], int]
    capacity_threshold: float = 0.1
    reserve_for_output: int = 4096
    merge_gradient_by_tokens: list[tuple[int, float | int]] | None = None

    gen_score_decay: float = DEFAULT_GEN_SCORE_DECAY
    gen_score_clamp: tuple[int, int] = DEFAULT_GEN_SCORE_CLAMP
    scoring_interval: int = DEFAULT_SCORING_INTERVAL

    checkpoint_interval: int = 5
    checkpoint_raw_messages: bool = True

    data_dir: str = ""


class ContextGC:
    """
    上下文垃圾回收器（Context GC）。

    宿主接口：push / close / get_messages。
    持久化 API：on_session_end / find / load_session_l1/l2 / get_user_* / cleanup_expired_sessions。
    """

    def __init__(
        self,
        options: ContextGCOptions,
        *,
        session_id: str = "",
        backend: Optional[MemoryBackend] = None,
        persist_l2: bool = True,
    ) -> None:
        self.options = options
        self.session_id = session_id
        self.backend = backend
        self.persist_l2 = persist_l2

        self.state = ContextGCState(
            rounds=[],
            max_tokens=options.max_input_tokens,
            capacity_threshold=options.capacity_threshold,
        )
        self._buffer: list[dict] = []
        self._full_session_raw: list[dict] = []
        self._round_count = 0
        self._lock = asyncio.Lock()

        self._checkpoint: CheckpointManager | None = None
        if options.data_dir and session_id and options.checkpoint_interval > 0:
            self._checkpoint = CheckpointManager(
                data_dir=options.data_dir,
                session_id=session_id,
                checkpoint_interval=options.checkpoint_interval,
                checkpoint_raw_messages=options.checkpoint_raw_messages,
            )
            recovered = self._checkpoint.try_recover(
                max_tokens=options.max_input_tokens,
                capacity_threshold=options.capacity_threshold,
            )
            if recovered is not None:
                self.state = recovered
                self._round_count = len(recovered.rounds)

        self._pref_detector = PreferenceDetector()

    # -------------------------------------------------------------------
    # 宿主核心接口
    # -------------------------------------------------------------------

    def push(self, message: dict | list[dict]) -> None:
        """推送消息到当前轮缓冲。"""
        if isinstance(message, list):
            self._buffer.extend(message)
            self._full_session_raw.extend(message)
        else:
            self._buffer.append(message)
            self._full_session_raw.append(message)

    async def close(self) -> None:
        """本轮结束：摘要 + 分代 + 合并 + checkpoint + 偏好检测。"""
        async with self._lock:
            round_messages = list(self._buffer) if self._buffer else []
            if round_messages:
                await self._on_round_end_internal(round_messages)
                self._buffer.clear()
            await self._check_capacity_and_compact()

            if self._checkpoint and round_messages:
                self._checkpoint.on_round_close(self.state, round_messages)

            if self.backend and round_messages:
                prefs = self._pref_detector.detect(
                    round_messages,
                    user_id="",
                    session_id=self.session_id,
                )
                if prefs:
                    self._detected_preferences = getattr(self, "_detected_preferences", [])
                    self._detected_preferences.extend(prefs)

    async def get_messages(self, current_messages: list[dict]) -> list[dict]:
        """获取送入主 LLM 的完整 messages。"""
        async with self._lock:
            return await self._build_and_compact(current_messages)

    # -------------------------------------------------------------------
    # 持久化 / 查询 API
    # -------------------------------------------------------------------

    async def on_session_end(
        self,
        user_id: str,
        agent_id: str = "",
        *,
        generate_l0: Callable[..., Awaitable[str]] | None = None,
        flush_distillation: Callable[..., Awaitable[dict]] | None = None,
    ) -> dict:
        """完整会话结束：L0/L1/L2 持久化 → 蒸馏管道 → 清理 checkpoint。"""
        result: dict[str, Any] = {"session_id": self.session_id}

        l1 = [r.summary for r in self.state.rounds]

        if generate_l0:
            l0 = await generate_l0(self.session_id, l1)
        else:
            l0 = "; ".join(l1[:3]) if l1 else ""
            if len(l0) > 200:
                l0 = l0[:200] + "..."

        l2_uri = ""
        if self.options.data_dir and self.persist_l2:
            l2_uri = await self._write_l2()

        if self.backend:
            await self.backend.save_session(
                self.session_id, l0, l1, l2_uri,
                meta={"user_id": user_id, "agent_id": agent_id},
            )

        detected_prefs: list[UserPreference] = getattr(self, "_detected_preferences", [])
        if detected_prefs and self.backend:
            for p in detected_prefs:
                p.user_id = user_id
            await self.backend.save_user_preferences(user_id, detected_prefs, self.session_id)

        result.update(l0=l0, l1_count=len(l1), l2_uri=l2_uri, detected_preferences=len(detected_prefs))

        if flush_distillation and self.backend:
            result["distillation"] = await flush_distillation(
                session_id=self.session_id, user_id=user_id,
                messages=self._full_session_raw, backend=self.backend,
            )
        else:
            try:
                from .distillation.flush import flush_distillation as default_flush
                if self.backend and len(self._full_session_raw) >= 4:
                    result["distillation"] = await default_flush(
                        session_id=self.session_id, user_id=user_id,
                        messages=self._full_session_raw, backend=self.backend,
                        options=self.options,
                    )
            except ImportError:
                pass

        if self._checkpoint:
            self._checkpoint.cleanup()

        return result

    async def find(self, query: str, limit: int = 10) -> list[dict]:
        """跨会话检索。"""
        if not self.backend:
            return []
        return await self.backend.search_sessions(query, limit)

    async def load_session_l1(self, session_id: str) -> list[str] | None:
        if not self.backend:
            return None
        return await self.backend.load_session_l1(session_id)

    async def load_session_l2(self, session_id: str) -> str | None:
        if not self.backend:
            return None
        return await self.backend.load_session_l2(session_id)

    async def get_user_preferences(self, user_id: str, category: str | None = None) -> list[UserPreference]:
        if not self.backend:
            return []
        return await self.backend.load_user_preferences(user_id, category)

    async def get_skills(self, skill_name: str | None = None) -> list[dict]:
        if not self.backend:
            return []
        return await self.backend.load_skills(skill_name)

    async def get_user_skills(self, user_id: str, skill_name: str | None = None) -> list[dict]:
        if not self.backend:
            return []
        return await self.backend.load_user_skills(user_id, skill_name)

    async def get_user_experience(self, user_id: str, task_desc: str | None = None) -> list[UserExperience]:
        if not self.backend:
            return []
        return await self.backend.load_user_experience(user_id, task_desc)

    async def cleanup_expired_sessions(self, ttl_days: int = 90) -> int:
        if not self.backend:
            return 0
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
        expired = await self.backend.list_expired_sessions(cutoff)
        for sid in expired:
            await self.backend.delete_session(sid)
        return len(expired)

    # -------------------------------------------------------------------
    # 内部逻辑
    # -------------------------------------------------------------------

    async def _on_round_end_internal(self, round_messages: list[dict]) -> None:
        opts = self.options
        self._round_count += 1

        if self.state.rounds:
            summary_input = (
                [{"role": "user", "content": f"[历史摘要 Round {r.round_id}] {r.summary}"}
                 for r in self.state.rounds]
                + round_messages
            )
        else:
            summary_input = round_messages

        input_tokens = opts.estimate_tokens(summary_input)
        gradient = opts.merge_gradient_by_tokens or DEFAULT_MERGE_GRADIENT_BY_TOKENS
        max_output_chars = get_max_output_chars(input_tokens, gradient)
        if max_output_chars is None:
            max_output_chars = 500
        new_summary = await opts.generate_summary(summary_input, max_output_chars=max_output_chars)
        token_count = opts.estimate_tokens(new_summary)

        if not self.state.rounds:
            self.state.rounds.append(RoundMeta(
                round_id=1, summary=new_summary, gen_score=0, token_count=token_count,
            ))
            self.state.recalc_total_tokens()
            return

        next_id = max(r.round_id for r in self.state.rounds) + 1
        self.state.rounds.append(RoundMeta(
            round_id=next_id, summary=new_summary, gen_score=0, token_count=token_count,
        ))

        if self._round_count % opts.scoring_interval == 0:
            current_user_text = get_current_user_text(round_messages)
            prev_rounds = self.state.rounds[:-1]
            if prev_rounds and current_user_text:
                await update_generational_scores(
                    prev_rounds, current_user_text, opts.compute_relevance,
                    decay=opts.gen_score_decay, clamp=opts.gen_score_clamp,
                )

        self.state.recalc_total_tokens()

    async def _check_capacity_and_compact(self) -> None:
        gradient = self.options.merge_gradient_by_tokens or DEFAULT_MERGE_GRADIENT_BY_TOKENS
        await check_capacity_and_compact(
            self.state, self.options.merge_summary, self.options.estimate_tokens,
            merge_gradient_by_tokens=gradient,
        )

    async def _build_and_compact(self, current_messages: list[dict]) -> list[dict]:
        opts = self.options
        effective_limit = opts.max_input_tokens - opts.reserve_for_output
        await self._check_capacity_and_compact()

        if not self.state.rounds:
            return list(current_messages)

        history_msgs = build_messages_from_state(self.state)
        result = history_msgs + current_messages

        if opts.estimate_tokens(result) <= effective_limit:
            return result

        input_tokens = opts.estimate_tokens(current_messages)
        gradient = opts.merge_gradient_by_tokens or DEFAULT_MERGE_GRADIENT_BY_TOKENS
        max_output_chars = get_max_output_chars(input_tokens, gradient) or 500
        current_summary = await opts.generate_summary(current_messages, max_output_chars=max_output_chars)
        result = history_msgs + [{"role": "user", "content": current_summary}]

        if opts.estimate_tokens(result) > effective_limit:
            history_tokens = opts.estimate_tokens(history_msgs)
            max_summary_tokens = max(0, effective_limit - history_tokens)
            current_summary = truncate_to_fit(current_summary, max_summary_tokens, opts.estimate_tokens)
            result = history_msgs + [{"role": "user", "content": current_summary}]

        return result

    async def _write_l2(self) -> str:
        from pathlib import Path
        session_dir = Path(self.options.data_dir) / "sessions" / self.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        content_path = session_dir / "content.md"

        lines: list[str] = []
        for msg in self._full_session_raw:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = "\n".join(parts)
            lines.append(f"### {role}\n\n{content}\n")

        content_path.write_text("\n".join(lines), encoding="utf-8")
        return str(content_path)
