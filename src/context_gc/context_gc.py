"""
context_gc/context_gc.py

ContextGC 主类：对外暴露 push / close / get_messages 三个接口。

角色约定：
  - 宿主（调用方）：只负责 push 消息、close 告知轮次结束、get_messages 获取上下文，
    不关心摘要、分代等细节。
  - 实现方（适配层）：实现并注入四个回调（generate_summary / merge_summary /
    compute_relevance / estimate_tokens）。
  - ContextGC：内部决定何时摘要、何时二次摘要，宿主无需知晓。
"""

import asyncio
from dataclasses import dataclass
from typing import Callable, Awaitable

from .state import RoundMeta, ContextGCState
from .generational import get_current_user_text, update_generational_scores
from .compaction import (
    check_capacity_and_compact,
    build_messages_from_state,
    truncate_to_fit,
    get_max_output_chars,
)


# 默认压缩梯度：[(输入token上限, 压缩比或固定字数), ...] 升序
# 压缩比>0：摘要token上限=输入*压缩比；压缩比<0：固定字数(取绝对值)
DEFAULT_MERGE_GRADIENT_BY_TOKENS = [
    (500, 0.0),       # 输入 token < 500：不合并
    (2000, 0.25),     # 输入 token < 2000：摘要最多输入的 25%
    (5000, 0.15),     # 输入 token < 5000：摘要最多输入的 15%
    (15000, 0.08),    # 输入 token < 15000：摘要最多输入的 8%
    (999999, -1500),  # 输入 token >= 15000：固定 1500 字
]


@dataclass
class ContextGCOptions:
    """
    ContextGC 配置项，所有回调均由实现方注入。

    Attributes:
        max_input_tokens: 主模型最大输入 token 数（含 reserve_for_output）。
        capacity_threshold: 容量阈值步长，默认 0.1（超过 20% 后每 10% 触发一次：20%/30%/40%/…）。
        reserve_for_output: 为模型输出预留的 token 数，默认 4096。
        merge_gradient_by_tokens: 压缩梯度 [(输入token上限, 压缩比), ...]，压缩比=摘要/输入。
        generate_summary: 单轮摘要回调，(messages, *, max_output_chars) -> str，字数上限由梯度计算传入。
        merge_summary: 合并摘要回调，(group, *, max_output_chars) -> str，字数上限由梯度计算传入。
        compute_relevance: 关联度回调，(user_text: str, summaries: list[str]) -> list[float]。
        estimate_tokens: token 估算回调，(text: str | list[dict]) -> int。
    """

    max_input_tokens: int
    generate_summary: Callable[[list[dict]], Awaitable[str]]
    merge_summary: Callable[[list[RoundMeta]], Awaitable[str]]
    compute_relevance: Callable[[str, list[str]], Awaitable[list[float]]]
    estimate_tokens: Callable[[object], int]
    capacity_threshold: float = 0.1
    reserve_for_output: int = 4096
    merge_gradient_by_tokens: list[tuple[int, float | int]] | None = None


class ContextGC:
    """
    上下文垃圾回收器（Context GC）。

    宿主接口（三个方法）：
      - push(message)     : 推送单条或多条消息到当前轮缓冲。
      - close()           : 告知本轮结束，触发摘要 + 分代打分 + 容量检查。
      - get_messages(...)  : 获取送入主 LLM 的完整 messages。

    两条异步流水线（均在 close() 内触发）：
      1. 每轮摘要 + 分代：_on_round_end_internal()
      2. 容量阈值二次摘要：_check_capacity_and_compact()
    """

    def __init__(self, options: ContextGCOptions) -> None:
        self.options = options
        self.state = ContextGCState(
            rounds=[],
            max_tokens=options.max_input_tokens,
            capacity_threshold=options.capacity_threshold,
        )
        self._buffer: list[dict] = []
        # 用 asyncio.Lock 保护 state，防止并发修改
        self._lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # 宿主接口
    # -----------------------------------------------------------------------

    def push(self, message: "dict | list[dict]") -> None:
        """
        推送消息到当前轮缓冲，可单条或批量。

        宿主在对话进行时调用，消息积累在 _buffer 中，
        直到 close() 被调用时一起处理。

        Args:
            message: 单条 dict 或 dict 列表，格式为 {"role": ..., "content": ...}。
        """
        if isinstance(message, list):
            self._buffer.extend(message)
        else:
            self._buffer.append(message)

    async def close(self) -> None:
        """
        告知本轮对话结束。

        触发两条流水线（顺序执行，共享 state 锁）：
          1. _on_round_end_internal：对当前 _buffer 做摘要，打分历史轮次。
          2. _check_capacity_and_compact：检查容量阈值，必要时做二次摘要。

        如果 _buffer 为空（宿主意外空调），直接做容量检查后返回。
        """
        async with self._lock:
            if self._buffer:
                await self._on_round_end_internal(list(self._buffer))
                self._buffer.clear()
            await self._check_capacity_and_compact()

    async def get_messages(self, current_messages: list[dict]) -> list[dict]:
        """
        获取送入主 LLM 的完整 messages。

        流程（见设计文档 5.5）：
          1. 触发容量检查（先稳定 state）。
          2. 从 state.rounds 构建历史摘要消息。
          3. 追加 current_messages（当前轮新消息）。
          4. 若超出 effective_limit：
             a. 仅对 current_messages 做摘要（不传历史）。
             b. 将摘要追加到历史后。
             c. 若仍超限，截断摘要。

        Args:
            current_messages: 当前轮消息（如新 user 消息），由宿主传入。

        Returns:
            送入主 LLM 的 messages 列表。
        """
        async with self._lock:
            return await self._build_and_compact(current_messages)

    # -----------------------------------------------------------------------
    # 内部逻辑
    # -----------------------------------------------------------------------

    async def _on_round_end_internal(self, round_messages: list[dict]) -> None:
        """
        每轮结束的内部处理（由 close() 调用）。

        步骤：
          1. 生成本轮摘要（历史摘要 + 本轮原始消息 → 一条 summary）。
          2. 第一轮：只追加，不做分代打分。
          3. 第二轮及以后：追加新轮，再对历史轮次（不含当前轮）做分代打分。
          4. 更新 total_tokens。

        Args:
            round_messages: 本轮完整消息列表（push 的内容）。
        """
        opts = self.options

        # 1. 生成本轮摘要
        # 输入 = 历史摘要 + 本轮原始消息，让 LLM 看到完整上下文
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
            max_output_chars = 500  # 单轮摘要必产生，param=0 时用默认上限
        new_summary = await opts.generate_summary(
            summary_input,
            max_output_chars=max_output_chars,
        )
        token_count = opts.estimate_tokens(new_summary)

        # 2. 第一轮：仅追加，不分代
        if not self.state.rounds:
            self.state.rounds.append(RoundMeta(
                round_id=1,
                summary=new_summary,
                gen_score=0,
                token_count=token_count,
            ))
            self.state.recalc_total_tokens()
            return

        # 3. 第二轮及以后：先追加当前轮（固定 gen_score=0），再打历史轮次的分
        next_id = max(r.round_id for r in self.state.rounds) + 1
        new_round = RoundMeta(
            round_id=next_id,
            summary=new_summary,
            gen_score=0,
            token_count=token_count,
        )
        self.state.rounds.append(new_round)

        # 取当前轮的 user 文本，用于关联度计算
        current_user_text = get_current_user_text(round_messages)

        # 对历史轮次（不含刚追加的当前轮）做分代打分
        prev_rounds = self.state.rounds[:-1]
        if prev_rounds and current_user_text:
            await update_generational_scores(
                prev_rounds,
                current_user_text,
                opts.compute_relevance,
            )

        # 4. 更新 total_tokens
        self.state.recalc_total_tokens()

    async def _check_capacity_and_compact(self) -> None:
        """
        检查容量阈值，必要时对低分代轮次做二次摘要（由 compaction 模块处理）。
        """
        gradient = self.options.merge_gradient_by_tokens or DEFAULT_MERGE_GRADIENT_BY_TOKENS
        await check_capacity_and_compact(
            self.state,
            self.options.merge_summary,
            self.options.estimate_tokens,
            merge_gradient_by_tokens=gradient,
        )

    async def _build_and_compact(self, current_messages: list[dict]) -> list[dict]:
        """
        构建送入 LLM 的 messages 并处理超限情况（由 get_messages 调用）。

        Args:
            current_messages: 当前轮消息。

        Returns:
            完整的 messages 列表。
        """
        opts = self.options
        effective_limit = opts.max_input_tokens - opts.reserve_for_output

        # 先触发容量检查，让 state 先收敛
        await self._check_capacity_and_compact()

        # state 为空时，直接返回当前轮消息
        if not self.state.rounds:
            return list(current_messages)

        # 构建历史摘要 messages + 当前轮
        history_msgs = build_messages_from_state(self.state)
        result = history_msgs + current_messages

        # 若未超限，直接返回
        if opts.estimate_tokens(result) <= effective_limit:
            return result

        # 超限：只对当前轮做摘要（不传历史），摘要追加到历史后
        input_tokens = opts.estimate_tokens(current_messages)
        gradient = opts.merge_gradient_by_tokens or DEFAULT_MERGE_GRADIENT_BY_TOKENS
        max_output_chars = get_max_output_chars(input_tokens, gradient) or 500
        current_summary = await opts.generate_summary(
            current_messages,
            max_output_chars=max_output_chars,
        )
        result = history_msgs + [{"role": "user", "content": current_summary}]

        # 若仍超限，截断摘要
        if opts.estimate_tokens(result) > effective_limit:
            history_tokens = opts.estimate_tokens(history_msgs)
            max_summary_tokens = max(0, effective_limit - history_tokens)
            current_summary = truncate_to_fit(
                current_summary, max_summary_tokens, opts.estimate_tokens
            )
            result = history_msgs + [{"role": "user", "content": current_summary}]

        return result
