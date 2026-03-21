"""
context_gc/generational.py

分代打分逻辑：

- 每 N 轮（scoring_interval）对历史轮次做关联度打分（步进式，非每轮）
- 打分前先衰减：gen_score *= decay（默认 0.9）
- 前 50% 关联度最高的 +1，后 50% -1
- 最终 clamp 到 [gen_score_min, gen_score_max]（默认 [-5, +5]）
"""

from typing import Callable, Awaitable

from .state import RoundMeta

# 默认配置
DEFAULT_GEN_SCORE_DECAY = 0.9
DEFAULT_GEN_SCORE_CLAMP = (-5, 5)
DEFAULT_SCORING_INTERVAL = 3


def extract_user_text(content: object) -> str:
    """
    从 message content 中提取纯文本，用于关联度计算。

    支持 str、多模态 list、其他类型（str() 兜底）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
        return ""
    return str(content)


def get_current_user_text(round_messages: list[dict]) -> str:
    """从本轮消息列表中取最后一条 user 文本。"""
    for msg in reversed(round_messages):
        if msg.get("role") == "user":
            return extract_user_text(msg.get("content", ""))
    return ""


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


async def update_generational_scores(
    prev_rounds: list[RoundMeta],
    current_user_text: str,
    compute_relevance: Callable[[str, list[str]], Awaitable[list[float]]],
    *,
    decay: float = DEFAULT_GEN_SCORE_DECAY,
    clamp: tuple[int, int] = DEFAULT_GEN_SCORE_CLAMP,
) -> None:
    """
    对历史轮次（不含当前轮）重新打分，更新 gen_score。

    算法：
    1. 先对所有历史轮次做衰减：gen_score = round(gen_score * decay)
    2. 用 compute_relevance 计算关联度。
    3. 前 50% 关联度最高的 +1，后 50% -1。
    4. clamp 到 [lo, hi]。

    调用方（ContextGC）负责按 scoring_interval 控制调用频率。
    """
    n = len(prev_rounds)
    if n == 0:
        return

    lo, hi = clamp

    # 衰减
    for r in prev_rounds:
        r.gen_score = round(r.gen_score * decay)

    summaries = [r.summary for r in prev_rounds]
    scores = await compute_relevance(current_user_text, summaries)

    ranked_indices = sorted(range(n), key=lambda i: scores[i], reverse=True)
    old_gen_count = n // 2

    for rank, idx in enumerate(ranked_indices):
        if rank < old_gen_count:
            prev_rounds[idx].gen_score += 1
        else:
            prev_rounds[idx].gen_score -= 1

    # 夹紧
    for r in prev_rounds:
        r.gen_score = _clamp(r.gen_score, lo, hi)
