"""
默认适配器：从环境变量读取 LLM 配置，供 ContextGCOptions.with_env_defaults() 使用。

环境变量：
- CONTEXT_GC_API_KEY: API Key（必填）
- CONTEXT_GC_BASE_URL: API 基址（默认 https://api.openai.com/v1）
- CONTEXT_GC_MODEL: 模型名（默认 gpt-4o-mini）

使用默认适配器需安装：pip install context-gc[example]
"""

from __future__ import annotations

import json
import os

_client = None


def _get_client():
    """懒加载 OpenAI 客户端，读取环境变量。"""
    global _client
    if _client is None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "使用默认适配器需安装 openai: pip install context-gc[example]"
            ) from e
        api_key = os.getenv("CONTEXT_GC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "未配置 CONTEXT_GC_API_KEY，请复制 .env.example 为 .env 并填入 API Key"
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("CONTEXT_GC_BASE_URL", "https://api.openai.com/v1"),
        )
    return _client


def _get_model() -> str:
    return os.getenv("CONTEXT_GC_MODEL", "gpt-4o-mini")


async def default_generate_summary(messages, *, max_output_chars: int = 500) -> str:
    """单轮摘要：调用 LLM，从环境变量读取 API 配置。"""
    client = _get_client()
    model = _get_model()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": f"用不超过{max_output_chars}字做简洁摘要，保留关键信息。"},
            *messages,
        ],
        max_tokens=max(100, max_output_chars),
    )
    return resp.choices[0].message.content or ""


async def default_merge_summary(rounds, *, max_output_chars: int = 500) -> str:
    """合并摘要：调用 LLM，从环境变量读取 API 配置。"""
    client = _get_client()
    model = _get_model()
    combined = "\n".join(f"[Round {r.round_id}] {r.summary}" for r in rounds)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": f"合并以下摘要为不超过{max_output_chars}字的简洁总结。"},
            {"role": "user", "content": combined},
        ],
        max_tokens=max(100, max_output_chars),
    )
    return resp.choices[0].message.content or ""


_RELEVANCE_SYSTEM_PROMPT = """\
你是关联度评分助手。给定「当前轮用户消息」与「历史轮摘要列表」，对每个历史摘要输出 0–1 的关联分数。
关联高：历史摘要与当前用户诉求在主题、实体、任务上有直接延续或强相关。
关联低：历史摘要与当前话题无关、已完结且无后续引用。
输出：JSON 数组 [s1, s2, ...]，长度等于历史摘要数，顺序一一对应。仅输出 JSON，无其他文字。"""


def _parse_scores(raw: str, expected_len: int) -> list[float] | None:
    """尝试从 LLM 输出中解析分数数组。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    try:
        scores = json.loads(text)
        if isinstance(scores, list) and len(scores) == expected_len:
            return [max(0.0, min(1.0, float(s))) for s in scores]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _keyword_fallback(user_text: str, summaries: list[str]) -> list[float]:
    """关键词重叠兜底，LLM 调用失败时使用。"""
    user_words = set(user_text.lower().split())
    scores = []
    for s in summaries:
        s_words = set(s.lower().split())
        overlap = len(user_words & s_words) / max(len(user_words | s_words), 1)
        scores.append(overlap)
    return scores


async def default_compute_relevance(user_text: str, summaries: list[str]) -> list[float]:
    """
    关联度打分：调用 LLM 对每个历史摘要与当前用户输入的相关性评分。

    步进式调用（由 scoring_interval 控制频率），LLM 失败时自动降级为关键词重叠。
    """
    n = len(summaries)
    if n == 0:
        return []

    client = _get_client()
    model = _get_model()

    items = "\n".join(f"[{i}] {s}" for i, s in enumerate(summaries))
    user_prompt = f"当前用户消息：\n{user_text}\n\n历史摘要（{n} 条）：\n{items}"

    max_tokens = max(100, n * 8)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _RELEVANCE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
        scores = _parse_scores(raw, n)
        if scores is not None:
            return scores
    except Exception:
        pass

    return _keyword_fallback(user_text, summaries)


_tokenizer = None
_tokenizer_loaded = False


def _get_tokenizer():
    """懒加载 tiktoken，按当前模型选择编码器；不可用时返回 None。"""
    global _tokenizer, _tokenizer_loaded
    if _tokenizer_loaded:
        return _tokenizer
    _tokenizer_loaded = True
    try:
        import tiktoken
        model = _get_model()
        try:
            _tokenizer = tiktoken.encoding_for_model(model)
        except KeyError:
            _tokenizer = tiktoken.get_encoding("cl100k_base")
    except ImportError:
        _tokenizer = None
    return _tokenizer


def _count_text_tokens(text: str) -> int:
    enc = _get_tokenizer()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 3)


def default_estimate_tokens(x) -> int:
    """token 估算：优先使用 tiktoken（按模型选编码器），不可用时 len//3 兜底。"""
    if isinstance(x, str):
        return _count_text_tokens(x)
    if isinstance(x, list):
        return sum(_count_text_tokens(json.dumps(m, ensure_ascii=False)) for m in x)
    return _count_text_tokens(str(x))
