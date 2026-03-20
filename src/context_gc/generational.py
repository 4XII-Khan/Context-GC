"""
context_gc/generational.py

分代打分逻辑：每轮摘要完成后，对历史轮次（不含当前轮）按与当前对话的关联度排名，
前 50% 分代值 +1（老生代），后 50% 分代值 -1（新生代），每次只能 ±1。
"""

from typing import Callable, Awaitable

from .state import RoundMeta


def extract_user_text(content: object) -> str:
    """
    从 message content 中提取纯文本，用于关联度计算。

    支持：
    - str：直接返回
    - list（多模态）：提取第一个 type=='text' 的 text 字段
    - 其他：str() 兜底
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 多模态列表：提取第一个 type=='text' 的 text 字段；若无文本则返回空字符串
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
        return ""
    # 其他非标准类型（如 int、dict 等），str() 兜底
    return str(content)


def get_current_user_text(round_messages: list[dict]) -> str:
    """
    从本轮消息列表中取最后一条 role=='user' 的文本内容。
    若没有 user 消息，返回空字符串。
    """
    for msg in reversed(round_messages):
        if msg.get("role") == "user":
            return extract_user_text(msg.get("content", ""))
    return ""


async def update_generational_scores(
    prev_rounds: list[RoundMeta],
    current_user_text: str,
    compute_relevance: Callable[[str, list[str]], Awaitable[list[float]]],
) -> None:
    """
    对历史轮次（不含当前轮）重新打分，更新 gen_score。

    算法：
    1. 用 compute_relevance 计算每条历史摘要与当前 user 消息的关联度分数。
    2. 按分数从高到低排序（ranked_indices）。
    3. 前 n//2 条关联度最高的：gen_score +1（老生代）。
    4. 后 n - n//2 条关联度较低的：gen_score -1（新生代）。
    每次只能 ±1，保证分代值平滑累积。

    Args:
        prev_rounds: 历史轮次列表（不含当前轮，原地修改）。
        current_user_text: 当前轮用户文本。
        compute_relevance: 关联度回调，签名 (user_text, summaries) -> list[float]。
    """
    n = len(prev_rounds)
    if n == 0:
        return

    summaries = [r.summary for r in prev_rounds]
    scores = await compute_relevance(current_user_text, summaries)

    # 按关联度从高到低排序，取索引
    ranked_indices = sorted(range(n), key=lambda i: scores[i], reverse=True)

    old_gen_count = n // 2  # 前 50%（奇数时老生代不多于新生代）

    for rank, idx in enumerate(ranked_indices):
        if rank < old_gen_count:
            # 老生代：关联度高，分代值 +1
            prev_rounds[idx].gen_score += 1
        else:
            # 新生代：关联度低，分代值 -1
            prev_rounds[idx].gen_score -= 1
