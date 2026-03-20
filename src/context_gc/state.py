"""
context_gc/state.py

核心数据结构：RoundMeta（单轮元数据）和 ContextGCState（全局上下文状态）。
"""

from dataclasses import dataclass, field


@dataclass
class RoundMeta:
    """
    单轮对话元数据。

    每轮完成摘要后创建一条 RoundMeta，存入 ContextGCState.rounds。
    二次摘要合并相邻轮时，原有多条 RoundMeta 被一条合并后的 RoundMeta 替换。
    """

    round_id: int
    """轮次 ID，全局单调递增，允许间断（合并后不重排）。"""

    summary: str
    """本轮摘要文本，纯文本，UTF-8。"""

    gen_score: int = 0
    """
    分代值，初始为 0。
    - > 0：老生代，与当前对话更相关，容量触发时保留。
    - <= 0：新生代/中性，容量触发时参与合并摘要。
    每轮打分时只能 ±1。
    """

    token_count: int = 0
    """该轮 summary 的估算 token 数，由 estimate_tokens 回调计算。"""

    is_merged: bool = False
    """标记该条是否由相邻多轮合并而来。"""

    merged_round_ids: list[int] = field(default_factory=list)
    """合并时记录被合并的原始 round_id 列表，用于审计追溯。"""


@dataclass
class ContextGCState:
    """
    Context GC 全局上下文状态。

    rounds 始终按 round_id 升序排列，是历史对话压缩后的摘要序列。
    """

    rounds: list[RoundMeta]
    """所有轮次的摘要列表，按 round_id 升序。"""

    max_tokens: int
    """上下文容量上限（等于主模型的 max_input_tokens）。"""

    total_tokens: int = 0
    """当前所有 rounds 的 token 总量，等于 sum(r.token_count for r in rounds)。"""

    capacity_threshold: float = 0.1
    """容量阈值步长，默认 10%，超过 20% 后每 10% 触发一次：20%/30%/40%/…。"""

    last_triggered_ratio: float = 0.1
    """上次已处理的容量档位，初始 0.1 表示 10% 档位已跳过，首次触发在 20%。"""

    def recalc_total_tokens(self) -> None:
        """重新计算 total_tokens（每次修改 rounds 后调用）。"""
        self.total_tokens = sum(r.token_count for r in self.rounds)

    @property
    def capacity_ratio(self) -> float:
        """当前已用容量占比（0.0 ~ 1.0+）。"""
        if self.max_tokens == 0:
            return 0.0
        return self.total_tokens / self.max_tokens
