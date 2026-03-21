"""
示例：带持久化的 Context GC 完整流程。

演示 push/close/get_messages → on_session_end → 新会话加载记忆注入。
需要 OpenAI 兼容 API（通过环境变量配置）。
"""

import asyncio
import os
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from context_gc import (
    ContextGC,
    ContextGCOptions,
    FileBackend,
    build_memory_injection,
)

DATA_DIR = Path(__file__).parent / "data"
API_KEY = os.getenv("CONTEXT_GC_API_KEY", "")
BASE_URL = os.getenv("CONTEXT_GC_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.getenv("CONTEXT_GC_MODEL", "openai/gpt-4o-mini")


def _make_openai_client():
    from openai import OpenAI
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


_client = None


def get_client():
    global _client
    if _client is None:
        _client = _make_openai_client()
    return _client


async def generate_summary(messages: list[dict], *, max_output_chars: int = 500) -> str:
    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": f"用不超过{max_output_chars}字做简洁摘要，保留关键信息。"},
            *messages,
        ],
        max_tokens=max(100, max_output_chars),
    )
    return resp.choices[0].message.content or ""


async def merge_summary(rounds, *, max_output_chars: int = 500) -> str:
    combined = "\n".join(f"[Round {r.round_id}] {r.summary}" for r in rounds)
    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": f"合并以下摘要为不超过{max_output_chars}字的简洁总结。"},
            {"role": "user", "content": combined},
        ],
        max_tokens=max(100, max_output_chars),
    )
    return resp.choices[0].message.content or ""


async def compute_relevance(user_text: str, summaries: list[str]) -> list[float]:
    scores = []
    user_words = set(user_text.lower().split())
    for s in summaries:
        s_words = set(s.lower().split())
        overlap = len(user_words & s_words) / max(len(user_words | s_words), 1)
        scores.append(overlap)
    return scores


def estimate_tokens(text) -> int:
    if isinstance(text, str):
        return len(text) // 3
    if isinstance(text, list):
        return sum(len(json.dumps(m, ensure_ascii=False)) // 3 for m in text)
    return len(str(text)) // 3


async def generate_l0(session_id: str, l1: list[str]) -> str:
    combined = "; ".join(l1[:5])
    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "用一句话（不超过100字）总结以下会话摘要。"},
            {"role": "user", "content": combined},
        ],
        max_tokens=100,
    )
    return resp.choices[0].message.content or ""


def call_llm_with_tools(system: str, messages: list[dict], tools: list[dict]) -> dict:
    """同步 LLM 调用（供蒸馏管道使用）。"""
    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, *messages],
        tools=tools if tools else None,
        max_tokens=512,
    )
    msg = resp.choices[0].message
    result: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        result["tool_calls"] = [
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
    return result


async def main():
    print("=== Context GC with Storage Demo ===\n")

    backend = FileBackend(DATA_DIR)

    options = ContextGCOptions(
        max_input_tokens=4000,
        generate_summary=generate_summary,
        merge_summary=merge_summary,
        compute_relevance=compute_relevance,
        estimate_tokens=estimate_tokens,
        data_dir=str(DATA_DIR),
        checkpoint_interval=3,
        scoring_interval=3,
    )

    gc = ContextGC(
        options,
        session_id="demo_session_001",
        backend=backend,
    )

    # 模拟 5 轮对话
    conversations = [
        ("请帮我实现一个用户登录功能", "好的，我来帮你实现登录功能。首先需要创建登录表单..."),
        ("用 JWT 还是 session？", "推荐使用 JWT，原因是..."),
        ("以后都用中文回复", "好的，我以后会用中文回复。"),
        ("登录功能实现好了，可以测试了", "已完成！主要步骤：1. 创建登录表单 2. JWT token 签发 3. 中间件验证"),
        ("谢谢，效果不错", "不客气！有问题随时问。"),
    ]

    for i, (user_msg, assistant_msg) in enumerate(conversations, 1):
        print(f"--- 轮次 {i} ---")
        gc.push({"role": "user", "content": user_msg})
        gc.push({"role": "assistant", "content": assistant_msg})
        await gc.close()
        print(f"  rounds: {len(gc.state.rounds)}, tokens: {gc.state.total_tokens}")

    # 会话结束
    print("\n--- on_session_end ---")
    result = await gc.on_session_end(
        user_id="user_001",
        generate_l0=generate_l0,
    )
    print(f"  L0: {result.get('l0', '')[:100]}")
    print(f"  L1 count: {result.get('l1_count', 0)}")
    print(f"  Detected preferences: {result.get('detected_preferences', 0)}")

    # 新会话：加载记忆
    print("\n--- 新会话：加载记忆 ---")
    gc2 = ContextGC(options, session_id="demo_session_002", backend=backend)

    prefs = await gc2.get_user_preferences("user_001")
    exps = await gc2.get_user_experience("user_001")
    skills = await gc2.get_user_skills("user_001")

    print(f"  偏好: {len(prefs)} 条")
    for p in prefs:
        print(f"    [{p.category}] {p.l0}")
    print(f"  经验: {len(exps)} 条")
    print(f"  技能: {len(skills)} 个")

    # 构建记忆注入
    injection = build_memory_injection(
        preferences=prefs,
        experiences=exps,
        skills=skills,
        max_tokens=2000,
        estimate_tokens=estimate_tokens,
    )
    if injection:
        print(f"\n--- 记忆注入（{estimate_tokens(injection)} tokens）---")
        print(injection[:500])

    # 跨会话检索
    print("\n--- 跨会话检索 ---")
    hits = await gc2.find("登录")
    for h in hits:
        print(f"  session={h['session_id']}, score={h.get('score', 0)}, l0={h.get('l0', '')[:60]}")

    print("\n=== Demo 完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
