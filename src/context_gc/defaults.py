"""
默认适配器：从环境变量读取 LLM 配置，供 ContextGCOptions.with_env_defaults() 使用。

环境变量：
- CONTEXT_GC_API_KEY: API Key（必填）
- CONTEXT_GC_BASE_URL: API 基址（默认 https://api.openai.com/v1）
- CONTEXT_GC_MODEL: 模型名（默认 gpt-4o-mini）
- CONTEXT_GC_FLUSH_TOOL_MAX_TOKENS: 蒸馏管道工具调用 ``max_tokens``（默认 8192）

使用默认适配器需安装：pip install context-gc[example]
"""

from __future__ import annotations

import json
import logging
import os

_client = None
_log = logging.getLogger(__name__)


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


def _chat_completion_extra_kwargs() -> dict:
    """
    部分网关（如 OpenRouter 上的 Qwen）默认开启 thinking 时，可能只填充推理通道，
    导致 ``choices[0].message.content`` 为空——持久化时 ``.abstract.md``（L0）会变成空文件。

    与集成测试里其它 LLM 调用对齐：在明确使用 OpenRouter 或显式要求时附带关闭 thinking 的 extra_body。
    官方 api.openai.com 一般忽略未知字段；若遇兼容问题可 unset CONTEXT_GC_DISABLE_THINKING。
    """
    base = (os.getenv("CONTEXT_GC_BASE_URL") or "").lower()
    flag = (os.getenv("CONTEXT_GC_DISABLE_THINKING") or "").strip().lower()
    if "openrouter" in base or flag in ("1", "true", "yes", "on"):
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


def _completion_kwargs_thinking_off() -> dict:
    """
    合并 ``_chat_completion_extra_kwargs()``，并**保证**带上 ``enable_thinking: False``。

    L0 等「短输出」调用若未关 thinking，部分 Qwen/网关会把正文挤到推理通道，
    导致 ``message.content`` 为空；而 ``CONTEXT_GC_BASE_URL`` 可能不含 openrouter 字样
    （自建反代、别名域名），``_chat_completion_extra_kwargs`` 单独不会加关断字段。
    OpenAI 官方端一般忽略未知 extra_body 字段。
    """
    kw = dict(_chat_completion_extra_kwargs())
    eb = dict(kw.get("extra_body") or {})
    ctk = dict(eb.get("chat_template_kwargs") or {})
    ctk["enable_thinking"] = False
    eb["chat_template_kwargs"] = ctk
    kw["extra_body"] = eb
    return kw


def default_call_llm_with_tools(system: str, messages: list[dict], tools: list[dict]) -> dict:
    """
    同步 Chat Completions + tools，供 ``flush_distillation`` 全链路使用（任务 / 偏好、蒸馏、
    经验 LLM 归并、技能学习等），与 ``default_generate_summary`` 共用 ``CONTEXT_GC_*``。

    ``max_tokens`` 默认 8192（工具参数里常含整份 SKILL.md），可用环境变量
    ``CONTEXT_GC_FLUSH_TOOL_MAX_TOKENS`` 覆盖。
    """
    client = _get_client()
    model = _get_model()
    max_tok = int(os.environ.get("CONTEXT_GC_FLUSH_TOOL_MAX_TOKENS", "8192"))
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, *messages],
        tools=tools if tools else None,
        max_tokens=max_tok,
        temperature=0.2,
        **_completion_kwargs_thinking_off(),
    )
    msg = resp.choices[0].message
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return out


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
        **_chat_completion_extra_kwargs(),
    )
    return resp.choices[0].message.content or ""


L0_GENERATION_PROMPT = """你是会话级记忆摘要（L0）生成助手。

下面提供的是本会话各轮压缩摘要（L1 列表）。请写一段 **不超过 120 字** 的会话总述，用于检索与快速理解。

**必须包含三部分（融成一段自然中文）：**
1. **用户意图或目标**：用户想达成什么（用「用户希望…」「用户请求…」等开头亦可）
2. **助手做了什么**：关键动作、步骤或使用的帮助方式（勿堆砌细节）
3. **结果与状态**：是否完成、主要产出、结论或待跟进点

**禁止**：Markdown 标题、列表符号、仅复述第一条 L1 而无整体视角。

只输出这一段正文。"""


async def default_generate_l0(session_id: str, l1: list[str]) -> str:
    """
    默认 L0 生成：基于 L1 摘要列表，产出「用户意图 → 做了什么 → 达成何种结果」式总述。
    需安装 openai，并配置 CONTEXT_GC_* 环境变量。
    """
    import asyncio

    client = _get_client()
    model = _get_model()
    l1_text = "\n---\n".join((s or "").strip() for s in l1[:80] if s)
    if not l1_text.strip():
        return ""

    def _call() -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": L0_GENERATION_PROMPT},
                {
                    "role": "user",
                    "content": f"会话 ID: {session_id}\n\n【L1 分轮摘要】\n{l1_text}",
                },
            ],
            max_tokens=256,
            temperature=0.2,
            **_completion_kwargs_thinking_off(),
        )
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            fr = getattr(choice, "finish_reason", None)
            _log.warning(
                "default_generate_l0: API 返回空 content（session_id=%s, model=%s, finish_reason=%s）。"
                "常见原因：网关未关 thinking、content 在推理字段、或 max_tokens 过小。",
                session_id[:48],
                model,
                fr,
            )
        return text

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call)


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
        **_chat_completion_extra_kwargs(),
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
            **_chat_completion_extra_kwargs(),
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
