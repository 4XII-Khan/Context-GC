"""
示例：带持久化的 Context GC 完整流程。

演示 push/close → on_session_end（L0/L1/L2 持久化 + 蒸馏管道）→ 新会话加载记忆注入。

持久化根目录：本文件旁 ``examples/data/``（见下方 DATA_DIR），其下为
``sessions/``、``user/{user_id}/`` 等，与 FileBackend 设计一致。

需要 OpenAI 兼容 API：配置 CONTEXT_GC_API_KEY。L0 / 蒸馏管道与压缩共用 ``CONTEXT_GC_*``：
``generate_l0``、``flush_call_llm`` 可挂在 ``ContextGCOptions`` 上或改用 ``with_env_defaults()``；
``flush_distillation`` 未显式传 ``call_llm`` 时会使用 ``defaults.default_call_llm_with_tools``。
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
    default_generate_l0,
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


def _chat_extra_kwargs() -> dict:
    """部分网关（如 OpenRouter 上 Qwen）需关闭 thinking，避免 content 为空。"""
    base = (BASE_URL or "").lower()
    flag = (os.getenv("CONTEXT_GC_DISABLE_THINKING") or "").strip().lower()
    if "openrouter" in base or flag in ("1", "true", "yes", "on"):
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


async def generate_summary(messages: list[dict], *, max_output_chars: int = 500) -> str:
    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": f"用不超过{max_output_chars}字做简洁摘要，保留关键信息。"},
            *messages,
        ],
        max_tokens=max(100, max_output_chars),
        **_chat_extra_kwargs(),
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
        **_chat_extra_kwargs(),
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


async def _run_flush_distillation(**kwargs) -> dict:
    """包装 flush_distillation；``call_llm`` 由库内从 ``options`` / defaults 解析。"""
    from context_gc.distillation.flush import flush_distillation

    trace: list[str] = []
    r = await flush_distillation(
        **{k: v for k, v in kwargs.items() if k != "trace"},
        trace=trace,
        # 示例里减少一次「任务归并」模型调用；生产可改为 "llm" 或依赖 flush 默认值
        experience_task_assign_mode="heuristic",
    )
    r["trace"] = trace
    return r


async def main():
    print("=== Context GC with Storage Demo ===\n")

    if not API_KEY.strip():
        print("请设置环境变量 CONTEXT_GC_API_KEY（可复制 .env.example 为 .env）。")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"持久化目录: {DATA_DIR.resolve()}\n")

    backend = FileBackend(DATA_DIR)

    options = ContextGCOptions(
        max_input_tokens=4000,
        generate_summary=generate_summary,
        merge_summary=merge_summary,
        compute_relevance=compute_relevance,
        estimate_tokens=estimate_tokens,
        generate_l0=default_generate_l0,
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
        flush_distillation=_run_flush_distillation,
    )
    print(f"  L0: {result.get('l0', '')[:100]}")
    print(f"  L1 count: {result.get('l1_count', 0)}")
    dist = result.get("distillation") or {}
    print(f"  detected_preferences (兼容字段，恒为 0): {result.get('detected_preferences', 0)}")
    print(f"  preferences_written (蒸馏): {dist.get('preferences_written', 0)}")
    print(f"  experiences_written: {dist.get('experiences_written', 0)}")
    print(f"  skills_learned: {dist.get('skills_learned', 0)}")
    if dist.get("errors"):
        print(f"  distillation errors: {dist['errors']}")

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
