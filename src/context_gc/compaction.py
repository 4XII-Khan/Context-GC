"""
context_gc/compaction.py

容量阈值二次摘要：
- 检查当前 total_tokens / max_tokens 是否达到 20%/40%/60%/80% 档位。
- 对分代值 <= 0 的相邻轮次做合并摘要，用单条 RoundMeta 替换原有多条。
- 替换采用「删除原范围，插入合并项」，不追加，保证时序正确。
"""

import math
from typing import Callable, Awaitable

from .state import RoundMeta, ContextGCState


def group_adjacent_by_round_id(rounds: list[RoundMeta]) -> list[list[RoundMeta]]:
    """
    将 round_id 连续的轮次分为一组，仅相邻（round_id 差为 1）才能合并。

    示例：
        输入 round_id = [1, 2, 3, 5, 6] → [[1,2,3], [5,6]]

    Args:
        rounds: 待分组的轮次列表（已按 round_id 排序）。

    Returns:
        嵌套列表，每个子列表为一组相邻轮次。
    """
    if not rounds:
        return []

    sorted_rounds = sorted(rounds, key=lambda r: r.round_id)
    groups: list[list[RoundMeta]] = [[sorted_rounds[0]]]

    for r in sorted_rounds[1:]:
        last_in_group = groups[-1][-1]
        if r.round_id == last_in_group.round_id + 1:
            groups[-1].append(r)
        else:
            groups.append([r])

    return groups


def build_messages_from_state(state: ContextGCState) -> list[dict]:
    """
    将 state.rounds 转换为送入主 LLM 的 messages 列表。

    默认格式：每轮摘要作为一条 role='user' 消息，内容为 '[Round N] {summary}'。
    顺序严格按 round_id 升序。

    Args:
        state: 当前上下文状态。

    Returns:
        OpenAI 格式的 messages 列表。
    """
    return [
        {"role": "user", "content": f"[Round {r.round_id}] {r.summary}"}
        for r in sorted(state.rounds, key=lambda r: r.round_id)
    ]


def truncate_to_fit(summary: str, max_tokens: int, estimate_tokens: Callable) -> str:
    """
    按 token 数截断 summary 字符串，使其满足 token 上限。

    采用二分法截断字符数（近似），直到 estimate_tokens(summary) <= max_tokens。

    Args:
        summary: 待截断的摘要文本。
        max_tokens: 允许的最大 token 数。
        estimate_tokens: token 估算回调。

    Returns:
        截断后的摘要文本。
    """
    if estimate_tokens(summary) <= max_tokens:
        return summary

    # 二分法：按字符数折半，直到满足 token 限制
    lo, hi = 0, len(summary)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(summary[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1

    return summary[:lo]


CHARS_PER_TOKEN = 3  # 估算：1 token ≈ 3 字符


def get_compression_param(
    input_tokens: int,
    gradient: list[tuple[int, float | int]],
) -> tuple[str, float | int]:
    """
    按输入 token 查梯度，返回 (模式, 参数)。
    模式: "ratio" 参数为压缩比(0-1)；"fixed" 参数为固定字数。
    """
    if not gradient:
        return ("ratio", 0.5)
    for token_limit, param in gradient:
        if input_tokens < token_limit:
            if isinstance(param, (int, float)) and param < 0:
                return ("fixed", abs(int(param)))
            return ("ratio", float(param))
    last = gradient[-1][1]
    if isinstance(last, (int, float)) and last < 0:
        return ("fixed", abs(int(last)))
    return ("ratio", float(last))


def get_max_output_chars(
    input_tokens: int,
    gradient: list[tuple[int, float | int]],
) -> int | None:
    """
    按梯度计算摘要最大字数。返回 None 表示不限制（如 param=0 不合并时）。
    """
    mode, param = get_compression_param(input_tokens, gradient)
    if mode == "ratio" and param <= 0:
        return None
    if mode == "fixed":
        return int(param)
    # ratio: 输入 token * 3 ≈ 输入字数，摘要字数 = 输入字数 * ratio
    input_chars = input_tokens * CHARS_PER_TOKEN
    return max(1, int(input_chars * param))


async def check_capacity_and_compact(
    state: ContextGCState,
    merge_summary: Callable[[list[RoundMeta]], Awaitable[str]],
    estimate_tokens: Callable[[str], int],
    *,
    merge_gradient_by_tokens: list[tuple[int, float | int]] | None = None,
) -> None:
    """
    检查容量占比，若达到阈值档位则对低分代轮次做二次摘要。

    压缩梯度：[(输入token上限, 压缩比或固定字数), ...]
    - 压缩比 0：不合并
    - 压缩比 > 0：摘要截断至 输入token * 压缩比
    - 压缩比 < 0：摘要截断至固定字数（取绝对值）
    """
    if state.max_tokens == 0:
        return

    ratio = state.capacity_ratio
    threshold = state.capacity_threshold
    triggered = math.floor(ratio / threshold) * threshold

    if triggered <= state.last_triggered_ratio or triggered == 0:
        return

    gradient = merge_gradient_by_tokens or []

    low_score = [r for r in state.rounds if r.gen_score <= 0]
    high_score = {r.round_id: r for r in state.rounds if r.gen_score > 0}

    if not low_score:
        state.last_triggered_ratio = triggered
        return

    groups = group_adjacent_by_round_id(low_score)

    merged_items: list[RoundMeta] = []
    skipped_round_ids: set[int] = set()

    for g in groups:
        input_tokens = sum(r.token_count for r in g)
        max_output_chars = get_max_output_chars(input_tokens, gradient)

        if max_output_chars is None:
            skipped_round_ids.update(r.round_id for r in g)
            continue

        merged_text = await merge_summary(g, max_output_chars=max_output_chars)
        merged_token_count = estimate_tokens(merged_text)
        merged_round = RoundMeta(
            round_id=max(r.round_id for r in g),
            summary=merged_text,
            gen_score=0,
            token_count=merged_token_count,
            is_merged=True,
            merged_round_ids=[r.round_id for r in g],
        )
        merged_items.append(merged_round)

    merged_by_max_id: dict[int, RoundMeta] = {m.round_id: m for m in merged_items}

    new_rounds: list[RoundMeta] = []
    for r in sorted(state.rounds, key=lambda x: x.round_id):
        if r.round_id in high_score:
            new_rounds.append(r)
        elif r.round_id in skipped_round_ids:
            new_rounds.append(r)
        elif r.round_id in merged_by_max_id:
            new_rounds.append(merged_by_max_id[r.round_id])

    state.rounds = new_rounds
    state.recalc_total_tokens()
    state.last_triggered_ratio = triggered
