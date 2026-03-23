"""
context_gc/core.py

ContextGC 主类：对外暴露 push / close / get_messages 三个宿主接口，
以及持久化（on_session_end / find / load_*）和查询 API。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Awaitable, Literal, Optional, Any

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
from .memory import LifecycleConfig


DEFAULT_MERGE_GRADIENT_BY_TOKENS = [
    (500, 0.0),
    (2000, 0.25),
    (5000, 0.15),
    (15000, 0.08),
    (999999, -1500),
]

# preset_agent_long_context 使用的更宽梯度（输入 token 更高时）
LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS = [
    (2000, 0.0),
    (8000, 0.25),
    (20000, 0.15),
    (50000, 0.08),
    (999999, -2500),
]


def _fallback_l0_from_l1_summaries(l1: list[str], max_chars: int = 500) -> str:
    """
    L0 生成回调若返回空（如网关仅填充推理通道、或模型异常），用 L1 轮次摘要
    拼接一段可检索占位文本，避免持久化 ``.abstract.md`` 为空。
    """
    parts = [(s or "").strip() for s in l1 if (s or "").strip()]
    if not parts:
        return ""
    text = " | ".join(parts[:12])
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


@dataclass
class ContextGCOptions:
    """
    ContextGC 配置项。

    回调可由实现方注入，或使用 with_env_defaults() 从环境变量获取默认适配器。
    持久化相关配置可选。
    """

    max_input_tokens: int
    generate_summary: Callable[..., Awaitable[str]]
    merge_summary: Callable[..., Awaitable[str]]
    compute_relevance: Callable[[str, list[str]], Awaitable[list[float]]]
    estimate_tokens: Callable[[object], int]
    # 会话级 L0（.abstract.md）；None 时 on_session_end 再尝试 defaults.default_generate_l0
    generate_l0: Callable[..., Awaitable[str]] | None = None
    # 蒸馏管道（任务/偏好、经验、技能）同步 tools 调用；None 时由 flush 加载 defaults.default_call_llm_with_tools
    flush_call_llm: Callable[[str, list[dict], list[dict]], dict] | None = None
    # ── 蒸馏 ``flush_distillation`` 开箱参数（``on_session_end`` 默认路径与自定义包装均可读取）──
    flush_min_messages: int = 4
    flush_task_agent_max_iterations: int = 20
    flush_skill_learner_max_iterations: int = 10
    flush_experience_task_assign_mode: Literal["heuristic", "llm"] = "llm"
    flush_dedup_strategy: str = "keyword_overlap"
    # 为 False 时从返回的 distillation 结果中移除 ``trace`` 列表以减小体积
    flush_distillation_trace: bool = False
    capacity_threshold: float = 0.1

    @classmethod
    def with_env_defaults(
        cls,
        max_input_tokens: int = 5000,
        *,
        generate_summary=None,
        merge_summary=None,
        compute_relevance=None,
        estimate_tokens=None,
        generate_l0=None,
        flush_call_llm=None,
        flush_min_messages: int | None = None,
        flush_distillation_trace: bool | None = None,
        **kwargs,
    ) -> "ContextGCOptions":
        """
        从环境变量构建带默认适配器的配置。

        环境变量：CONTEXT_GC_API_KEY、CONTEXT_GC_BASE_URL、CONTEXT_GC_MODEL
        另见 ``CONTEXT_GC_FLUSH_MIN_MESSAGES``、``CONTEXT_GC_FLUSH_INCLUDE_TRACE``（蒸馏）。
        需安装：pip install context-gc[example]
        传入同名参数可覆盖默认回调。
        ``generate_l0`` / ``flush_call_llm`` 未传时分别绑定 ``default_generate_l0``、
        ``default_call_llm_with_tools``，与压缩共用 ``CONTEXT_GC_*`` 模型与网关。
        """
        from .defaults import (
            default_generate_summary,
            default_merge_summary,
            default_compute_relevance,
            default_estimate_tokens,
            default_generate_l0,
            default_call_llm_with_tools,
        )

        _fmin = flush_min_messages
        if _fmin is None:
            try:
                _fmin = int(os.environ.get("CONTEXT_GC_FLUSH_MIN_MESSAGES", "4"))
            except ValueError:
                _fmin = 4
        _ftrace = flush_distillation_trace
        if _ftrace is None:
            _ftrace = os.environ.get("CONTEXT_GC_FLUSH_INCLUDE_TRACE", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

        return cls(
            max_input_tokens=max_input_tokens,
            generate_summary=generate_summary or default_generate_summary,
            merge_summary=merge_summary or default_merge_summary,
            compute_relevance=compute_relevance or default_compute_relevance,
            estimate_tokens=estimate_tokens or default_estimate_tokens,
            generate_l0=generate_l0 if generate_l0 is not None else default_generate_l0,
            flush_call_llm=flush_call_llm if flush_call_llm is not None else default_call_llm_with_tools,
            flush_min_messages=_fmin,
            flush_distillation_trace=_ftrace,
            **kwargs,
        )

    @classmethod
    def preset_small_chat(cls, **kwargs) -> "ContextGCOptions":
        """小型对话：较低窗口、较密 checkpoint，蒸馏 ``min_messages`` 默认 2。"""
        base: dict[str, Any] = {
            "max_input_tokens": 4000,
            "checkpoint_interval": 3,
            "scoring_interval": 2,
            "flush_min_messages": 2,
        }
        base.update(kwargs)
        return cls.with_env_defaults(**base)

    @classmethod
    def preset_agent_long_context(cls, **kwargs) -> "ContextGCOptions":
        """长上下文智能体：更大 ``max_input_tokens``、更疏 checkpoint、宽合并梯度。"""
        base: dict[str, Any] = {
            "max_input_tokens": 32000,
            "checkpoint_interval": 8,
            "scoring_interval": 4,
            "flush_min_messages": 4,
            "merge_gradient_by_tokens": LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS,
        }
        base.update(kwargs)
        return cls.with_env_defaults(**base)
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

    @classmethod
    def create_with_file_backend(
        cls,
        data_dir: str | Path,
        *,
        session_id: str = "",
        options: ContextGCOptions | None = None,
        persist_l2: bool = True,
        **with_env_kwargs: Any,
    ) -> "ContextGC":
        """
        创建带 ``FileBackend`` 的实例；``data_dir`` 目录不存在时会创建。

        - 未传 ``options``：使用 ``ContextGCOptions.with_env_defaults(data_dir=..., **with_env_kwargs)``。
        - 已传 ``options`` 且 ``data_dir`` 为空：用本参数补全 ``options.data_dir``。
        """
        from .storage.file_backend import FileBackend

        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if options is None:
            opts = ContextGCOptions.with_env_defaults(data_dir=str(root), **with_env_kwargs)
        else:
            opts = (
                replace(options, data_dir=str(root))
                if not (options.data_dir or "").strip()
                else options
            )
        backend = FileBackend(root)
        return cls(opts, session_id=session_id, backend=backend, persist_l2=persist_l2)

    def _distillation_flush_kwargs(self) -> dict[str, Any]:
        o = self.options
        return {
            "min_messages": o.flush_min_messages,
            "task_agent_max_iterations": o.flush_task_agent_max_iterations,
            "skill_learner_max_iterations": o.flush_skill_learner_max_iterations,
            "experience_task_assign_mode": o.flush_experience_task_assign_mode,
            "dedup_strategy": o.flush_dedup_strategy,
        }

    def _apply_distillation_trace_policy(self, dist: dict[str, Any] | None) -> dict[str, Any] | None:
        if not dist:
            return dist
        if self.options.flush_distillation_trace:
            return dist
        out = dict(dist)
        out.pop("trace", None)
        return out

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
        """本轮结束：摘要 + 分代 + 合并 + checkpoint。用户偏好仅由会话结束时的蒸馏管道写入。"""
        async with self._lock:
            round_messages = list(self._buffer) if self._buffer else []
            if round_messages:
                await self._on_round_end_internal(round_messages)
                self._buffer.clear()
            await self._check_capacity_and_compact()

            if self._checkpoint and round_messages:
                self._checkpoint.on_round_close(self.state, round_messages)

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
        """完整会话结束：L0/L1/L2 持久化 → 蒸馏管道 → 清理 checkpoint。

        用户偏好仅通过 ``flush_distillation``（Task Agent / 蒸馏等）写入 backend，
        不再在 ``close()`` 中用规则/正则检测写入。
        返回值中的 ``detected_preferences`` 恒为 ``0``（保留字段以兼容旧宿主）。

        ``generate_l0`` 解析顺序：**本方法参数** > ``ContextGCOptions.generate_l0`` >
        动态导入 ``defaults.default_generate_l0``。与 ``ContextGCOptions.with_env_defaults()``
        联用时，宿主无需再传 ``generate_l0``，L0 与压缩摘要共用同一套 ``CONTEXT_GC_*`` 默认模型。

        任一路径在导入失败、调用异常或返回空时，回退为 L1 前 3 条短拼接，再不行则用
        ``_fallback_l0_from_l1_summaries``。

        蒸馏：未传 ``flush_distillation`` 时由库内调用 ``flush_distillation``，并传入
        ``options=self.options``；其中 ``flush_call_llm`` 未配置时会使用
        ``defaults.default_call_llm_with_tools``（任务/偏好/蒸馏/经验/技能 与压缩同源模型）。
        自定义 ``flush_distillation`` 回调时同样会收到 ``options`` 及
        ``ContextGCOptions`` 中的蒸馏参数（``min_messages``、迭代上限等），请用 ``**kwargs``
        转交给 ``flush_distillation``；若显式传 ``call_llm=`` 则优先生效。
        ``flush_distillation_trace`` 为 False 时，返回结果中的 ``trace`` 会被移除。
        """
        result: dict[str, Any] = {"session_id": self.session_id}

        l1 = [r.summary for r in self.state.rounds]

        l0_cb = (
            generate_l0
            if generate_l0 is not None
            else getattr(self.options, "generate_l0", None)
        )

        if l0_cb is not None:
            l0 = await l0_cb(self.session_id, l1)
        else:
            l0 = ""
            try:
                from .defaults import default_generate_l0 as _dgl0

                l0 = await _dgl0(self.session_id, l1)
            except ImportError:
                pass
            except Exception:
                # 无 API Key、网络错误、模型返回空管道等：走下方规则回退
                pass
            if not (l0 or "").strip():
                l0 = "; ".join(l1[:3]) if l1 else ""
                if len(l0) > 200:
                    l0 = l0[:200] + "..."

        l0 = (l0 or "").strip()
        if not l0 and l1 and l0_cb is not None:
            l0 = "; ".join((s or "").strip() for s in l1[:3] if (s or "").strip())
            if len(l0) > 200:
                l0 = l0[:200] + "..."

        l0 = (l0 or "").strip()
        if not l0 and l1:
            l0 = _fallback_l0_from_l1_summaries(l1)

        l2_uri = ""
        if self.options.data_dir and self.persist_l2:
            l2_uri = await self._write_l2()

        if self.backend:
            await self.backend.save_session(
                self.session_id, l0, l1, l2_uri,
                meta={"user_id": user_id, "agent_id": agent_id},
            )

        result.update(l0=l0, l1_count=len(l1), l2_uri=l2_uri, detected_preferences=0)

        _flush_kw = self._distillation_flush_kwargs()
        if flush_distillation and self.backend:
            raw_dist = await flush_distillation(
                session_id=self.session_id, user_id=user_id,
                messages=self._full_session_raw, backend=self.backend,
                options=self.options,
                **_flush_kw,
            )
            result["distillation"] = self._apply_distillation_trace_policy(raw_dist)
        else:
            try:
                from .distillation.flush import flush_distillation as default_flush
                min_m = self.options.flush_min_messages
                if self.backend and len(self._full_session_raw) >= min_m:
                    raw_dist = await default_flush(
                        session_id=self.session_id, user_id=user_id,
                        messages=self._full_session_raw, backend=self.backend,
                        options=self.options,
                        **_flush_kw,
                    )
                    result["distillation"] = self._apply_distillation_trace_policy(raw_dist)
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

    async def build_memory_injection_text(
        self,
        user_id: str,
        *,
        current_query: str = "",
        config: LifecycleConfig | None = None,
    ) -> str:
        """
        从 backend 加载偏好 / 经验 / 技能并拼接为可注入 system 的文本（封装 ``build_memory_injection``）。

        Args:
            config: 默认 ``LifecycleConfig()``；主要读取 ``memory_inject_max_tokens``。
        """
        from .memory import build_memory_injection

        cfg = config if config is not None else LifecycleConfig()
        prefs = await self.get_user_preferences(user_id)
        exps = await self.get_user_experience(user_id)
        skills = await self.get_user_skills(user_id)
        est = self.options.estimate_tokens

        def _est_str(t: str) -> int:
            return int(est(t))

        return build_memory_injection(
            prefs,
            exps,
            skills,
            max_tokens=cfg.memory_inject_max_tokens,
            estimate_tokens=_est_str,
            current_query=current_query,
        )

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
        """
        L2：完整会话记录。每条消息以 JSON 全量写入，保留宿主扩展字段
        （如 steps、tool_calls 等），不强制统一 schema，适配不同 Agent。
        """
        import json
        from pathlib import Path

        session_dir = Path(self.options.data_dir) / "sessions" / self.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        content_path = session_dir / "content.md"

        lines: list[str] = [
            "# L2 原始会话记录",
            "",
            "每条消息为完整 JSON（含 assistant 侧工具/步骤等扩展字段，按宿主 push 原样持久化）。",
            "",
        ]
        for i, msg in enumerate(self._full_session_raw):
            if not isinstance(msg, dict):
                msg = {"role": "unknown", "content": str(msg)}
            try:
                blob = json.dumps(msg, ensure_ascii=False, indent=2)
            except TypeError:
                blob = json.dumps(
                    {"role": msg.get("role"), "content": str(msg.get("content", ""))},
                    ensure_ascii=False,
                    indent=2,
                )
            lines.append(f"## message[{i}]")
            lines.append("")
            lines.append("```json")
            lines.append(blob)
            lines.append("```")
            lines.append("")

        content_path.write_text("\n".join(lines), encoding="utf-8")
        return str(content_path)
