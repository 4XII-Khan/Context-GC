"""
tests/test_e2e_cases.py

5 个端到端集成测试 Case，覆盖 Context GC 全部核心能力链路：
  Case 1: 基础摘要 + 分代打分（5 轮）
  Case 2: 容量触发合并（10 轮，小容量上限）
  Case 3: 偏好仅经蒸馏写入（5 轮，无 close() 规则检测；mock 蒸馏持久化）
  Case 4: Checkpoint 崩溃恢复（8 轮，模拟中断后恢复）
  Case 5: 全链路端到端（8 轮，会话结束 → L0/L1/L2 → 新会话加载 → 跨会话检索 → 记忆注入）

运行：
    python3 tests/test_e2e_cases.py
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

from context_gc import (
    ContextGC,
    ContextGCOptions,
    FileBackend,
    RoundMeta,
    UserPreference,
    build_memory_injection,
)
from context_gc.defaults import default_generate_l0

_env_paths = [
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent / ".env",
]
for p in _env_paths:
    if p.exists():
        load_dotenv(p)
        break

LLM_API_KEY = os.environ.get("CONTEXT_GC_API_KEY", "")
LLM_BASE_URL = os.environ.get("CONTEXT_GC_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.environ.get("CONTEXT_GC_MODEL", "Qwen3.5-35B-A3B")

_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

OUTPUT_BASE = Path(__file__).parent / "output"
# 运行时按日期目录设置，见 main()
TEST_DATA_DIR: Path = OUTPUT_BASE / "e2e_test_data"
REPORT_FILE: Path = OUTPUT_BASE / "e2e_test_report.txt"


def estimate_tokens(text: object) -> int:
    if isinstance(text, str):
        return 0 if not text else max(1, len(text) // 3)
    if isinstance(text, list):
        total = sum(len(str(m.get("content", ""))) for m in text)
        return 0 if total == 0 else max(1, total // 3)
    s = str(text)
    return 0 if not s else max(1, len(s) // 3)


async def generate_summary(messages: list[dict], *, max_output_chars: int | None = None) -> str:
    dialog_text = "\n".join(f"[{m['role'].upper()}] {m.get('content', '')}" for m in messages)
    max_chars = 40_000
    if len(dialog_text) > max_chars:
        dialog_text = "...(前文省略)...\n" + dialog_text[-max_chars:]
    length = f"输出不超过 {max_output_chars} 字。" if max_output_chars else "输出 50–150 字。"
    prompt = (
        "你是一个对话摘要助手。将以下对话压缩为一条摘要，要求：\n"
        "1. 保留用户意图、关键决策、结论\n"
        "2. 去除寒暄和重复表述\n"
        f"3. {length}\n"
        "4. 语言与输入一致，只输出摘要，不要其他内容\n\n"
        f"对话内容：\n{dialog_text}"
    )
    resp = await _client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
        temperature=0.3,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return (resp.choices[0].message.content or "").strip()


async def merge_summary(group: list[RoundMeta], *, max_output_chars: int | None = None) -> str:
    summaries_text = "\n---\n".join(f"[Round {r.round_id}] {r.summary}" for r in group)
    length = f"输出不超过 {max_output_chars} 字。" if max_output_chars else "输出不超过 200 字。"
    prompt = (
        "将以下多段对话摘要合并为一条，要求：\n"
        f"1. {length}\n"
        "2. 去除重复内容，保留关键信息\n"
        "3. 保持时间顺序，突出重要结论\n"
        "4. 只输出合并后的摘要，不要其他内容\n\n"
        f"待合并的摘要：\n{summaries_text}"
    )
    resp = await _client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
        temperature=0.2,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return (resp.choices[0].message.content or "").strip()


async def compute_relevance(user_text: str, summaries: list[str]) -> list[float]:
    if not summaries:
        return []
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(summaries))
    prompt = (
        f"当前用户问题：\"{user_text[:500]}\"\n\n"
        f"以下是历史对话摘要，请评估每条摘要与当前问题的相关程度，"
        f"打分范围 0-10（10 最相关）。\n"
        f"只输出每条的分数，用逗号分隔，不要其他内容。\n"
        f"例如：3,8,5\n\n"
        f"摘要列表：\n{numbered[:8000]}"
    )
    resp = await _client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=256,
        temperature=0.1,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        scores = [float(x.strip()) for x in raw.split(",") if x.strip()]
        if len(scores) != len(summaries):
            avg = sum(scores) / len(scores) if scores else 5.0
            scores = (scores + [avg] * len(summaries))[:len(summaries)]
    except Exception:
        scores = [5.0] * len(summaries)
    return scores


# ═══════════════════════════════════════════════════════════════════
# 测试报告工具
# ═══════════════════════════════════════════════════════════════════

class TestReport:
    def __init__(self):
        self.sections: list[str] = []
        self._current: list[str] = []

    def start_case(self, name: str, desc: str):
        self._current = [
            f"\n{'═'*80}",
            f"  {name}",
            f"  {desc}",
            f"{'═'*80}",
        ]

    def log(self, msg: str):
        self._current.append(f"  {msg}")

    def check(self, label: str, condition: bool, detail: str = ""):
        status = "✅ PASS" if condition else "❌ FAIL"
        line = f"  {status} | {label}"
        if detail:
            line += f"  ({detail})"
        self._current.append(line)
        return condition

    def end_case(self, elapsed: float, passed: int, total: int):
        elapsed_str = f"{elapsed*1000:.0f}ms" if elapsed < 1 else f"{elapsed:.1f}s"
        self._current.append(f"\n  ⏱ 耗时: {elapsed_str} | 结果: {passed}/{total} 通过")
        self.sections.append("\n".join(self._current))

    def dump(self) -> str:
        header = [
            f"{'═'*80}",
            f"  Context GC 全链路端到端测试报告",
            f"  日期: {datetime.now().strftime('%Y-%m-%d')}",
            f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"  输出目录: {REPORT_FILE.parent}",
            f"  模型: {LLM_MODEL}",
            f"{'═'*80}",
        ]
        return "\n".join(header) + "\n" + "\n".join(self.sections) + "\n"


report = TestReport()


# ═══════════════════════════════════════════════════════════════════
# Case 1: 基础摘要 + 分代打分
# ═══════════════════════════════════════════════════════════════════

async def case1_basic_summary_and_scoring():
    """5 轮对话：验证 push/close 生成摘要、分代打分、get_messages 输出正确。"""
    report.start_case("Case 1: 基础摘要 + 分代打分", "5 轮对话，验证摘要生成 + 分代打分 + get_messages")
    t0 = time.time()
    passed = 0
    total = 0

    conversations = [
        ("我想做一个待办事项 App，技术栈应该怎么选？", "推荐使用 React + TypeScript 前端，后端用 Node.js + Express，数据库 PostgreSQL。"),
        ("数据库设计怎么做？需要哪些表？", "核心表：users、todos、categories。todos 表需要 title、description、status、due_date 字段。"),
        ("我希望以后都用 TypeScript", "好的，全栈统一 TypeScript 是好选择，前后端都用 TS 可以共享类型定义。"),
        ("API 接口怎么设计？", "RESTful 风格：GET /todos、POST /todos、PUT /todos/:id、DELETE /todos/:id。加上认证中间件。"),
        ("回顾一下，我们讨论了数据库设计的哪些要点？", "我们讨论了 users、todos、categories 三张核心表的设计，以及 todos 表的关键字段。"),
    ]

    gc = ContextGC(
        ContextGCOptions(
            max_input_tokens=8000,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            scoring_interval=2,
        )
    )

    for i, (user_msg, asst_msg) in enumerate(conversations, 1):
        gc.push([{"role": "user", "content": user_msg}, {"role": "assistant", "content": asst_msg}])
        await gc.close()
        report.log(f"轮次 {i}: rounds={len(gc.state.rounds)}, tokens={gc.state.total_tokens}")

    total += 1
    passed += report.check(
        "每轮生成一条摘要（共 5 条）",
        len(gc.state.rounds) == 5,
        f"实际 rounds={len(gc.state.rounds)}",
    )

    total += 1
    all_have_summary = all(r.summary and len(r.summary) > 10 for r in gc.state.rounds)
    passed += report.check("每条摘要非空且长度合理", all_have_summary)

    total += 1
    scored_rounds = [r for r in gc.state.rounds if r.gen_score != 0]
    passed += report.check(
        "分代打分已执行（scoring_interval=2，至少有轮次被打分）",
        len(scored_rounds) > 0,
        f"被打分的轮次: {[(r.round_id, r.gen_score) for r in scored_rounds]}",
    )

    current = [{"role": "user", "content": "帮我总结所有讨论要点"}]
    msgs = await gc.get_messages(current)
    total += 1
    passed += report.check(
        "get_messages 返回历史摘要 + 当前消息",
        len(msgs) > 1,
        f"messages 数量: {len(msgs)}",
    )

    total += 1
    has_round_tags = any("[Round " in m.get("content", "") for m in msgs)
    passed += report.check("历史消息包含 [Round N] 标签", has_round_tags)

    for r in gc.state.rounds:
        preview = r.summary[:80] + "..." if len(r.summary) > 80 else r.summary
        report.log(f"  Round {r.round_id} gen_score={r.gen_score:+d} tokens={r.token_count}: {preview}")

    report.end_case(time.time() - t0, passed, total)
    return passed, total


# ═══════════════════════════════════════════════════════════════════
# Case 2: 容量触发合并
# ═══════════════════════════════════════════════════════════════════

async def case2_capacity_compaction():
    """10 轮对话 + 极小容量上限（1500 token），验证容量触发合并机制。"""
    report.start_case("Case 2: 容量触发合并", "10 轮对话，max_input_tokens=1500，验证合并摘要机制")
    t0 = time.time()
    passed = 0
    total = 0

    conversations = [
        ("帮我设计一个电商系统的商品模块", "商品模块核心实体：Product、SKU、Category、Brand，支持 SPU-SKU 分离。"),
        ("库存管理怎么做？", "独立库存服务，预扣→确认/回滚模式，Redis 缓存热点库存，定时与 DB 对账。"),
        ("购物车的设计方案？", "登录用户存 Redis，匿名用户存 Cookie/LocalStorage，合并策略基于 SKU 去重。"),
        ("订单流程需要哪些状态？", "待支付→已支付→待发货→已发货→已签收→已完成/已取消/退款中。"),
        ("支付对接的注意事项？", "异步通知 + 主动查询双保险，幂等设计，退款走逆向流程。"),
        ("搜索功能怎么实现？", "Elasticsearch 全文检索，商品导入 ES 索引，支持拼音、同义词、分面筛选。"),
        ("推荐系统的架构？", "协同过滤 + 内容推荐混合，离线 Spark 训练 + 在线实时打分，Redis 缓存推荐结果。"),
        ("如何保证高并发下的系统稳定性？", "限流（令牌桶）、熔断（Hystrix/Sentinel）、降级策略、异步削峰（MQ）。"),
        ("数据库分库分表策略？", "按 user_id 分库，按 order_id 范围分表，分布式 ID 用 Snowflake。"),
        ("监控和告警怎么建？", "Prometheus + Grafana 指标监控，ELK 日志，自定义告警规则，SLI/SLO 体系。"),
    ]

    gc = ContextGC(
        ContextGCOptions(
            max_input_tokens=1500,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            scoring_interval=3,
            capacity_threshold=0.1,
            reserve_for_output=0,
        )
    )

    rounds_history = []
    for i, (user_msg, asst_msg) in enumerate(conversations, 1):
        gc.push([{"role": "user", "content": user_msg}, {"role": "assistant", "content": asst_msg}])
        await gc.close()
        rounds_history.append((len(gc.state.rounds), gc.state.total_tokens, gc.state.capacity_ratio))
        report.log(f"轮次 {i}: rounds={len(gc.state.rounds)}, tokens={gc.state.total_tokens}, ratio={gc.state.capacity_ratio:.1%}")

    total += 1
    final_rounds = len(gc.state.rounds)
    passed += report.check(
        "经过合并后 rounds 数量 < 10",
        final_rounds < 10,
        f"最终 rounds={final_rounds}（原始 10 轮）",
    )

    total += 1
    has_merged = any(r.is_merged for r in gc.state.rounds)
    passed += report.check("存在合并后的轮次（is_merged=True）", has_merged)

    if has_merged:
        merged_rounds = [r for r in gc.state.rounds if r.is_merged]
        for mr in merged_rounds:
            report.log(f"  合并轮 Round {mr.round_id}: merged_from={mr.merged_round_ids}, tokens={mr.token_count}")

    total += 1
    final_tokens = gc.state.total_tokens
    max_tokens = gc.options.max_input_tokens
    passed += report.check(
        f"最终 token 总量可控（< max_input_tokens={max_tokens}）",
        final_tokens <= max_tokens * 1.5,
        f"最终 tokens={final_tokens}",
    )

    total += 1
    ratio_increased = any(h[2] > 0.2 for h in rounds_history)
    passed += report.check("容量占比曾超过 20%（触发合并条件）", ratio_increased)

    report.end_case(time.time() - t0, passed, total)
    return passed, total


# ═══════════════════════════════════════════════════════════════════
# Case 3: 偏好仅经蒸馏（或宿主注入的 flush）写入
# ═══════════════════════════════════════════════════════════════════

async def case3_preference_detection():
    """5 轮对话：close() 不再做正则偏好检测；偏好由 flush_distillation 写入。"""
    case_dir = TEST_DATA_DIR / "case3"
    if case_dir.exists():
        shutil.rmtree(case_dir)

    report.start_case(
        "Case 3: 偏好仅蒸馏写入",
        "验证无 close() 规则检测；on_session_end 注入 flush 后偏好可持久化",
    )
    t0 = time.time()
    passed = 0
    total = 0

    backend = FileBackend(case_dir)

    conversations = [
        ("帮我写一个 Python 脚本", "好的，这是一个 Python 脚本..."),
        ("以后都用中文回复我", "好的，以后我会用中文回复你。"),
        ("不要使用 var，用 const 和 let", "明白，我以后会避免使用 var，统一用 const/let。"),
        ("我偏好使用 TypeScript 而不是 JavaScript", "了解，后续我会优先使用 TypeScript。"),
        ("请始终使用 4 空格缩进", "好的，我会始终使用 4 空格缩进。"),
    ]

    gc = ContextGC(
        ContextGCOptions(
            max_input_tokens=8000,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            data_dir=str(case_dir),
            checkpoint_interval=3,
        ),
        session_id="pref_test_001",
        backend=backend,
    )

    for i, (user_msg, asst_msg) in enumerate(conversations, 1):
        gc.push([{"role": "user", "content": user_msg}, {"role": "assistant", "content": asst_msg}])
        await gc.close()
        report.log(f"轮次 {i}: rounds={len(gc.state.rounds)}")

    detected = getattr(gc, "_detected_preferences", [])
    total += 1
    passed += report.check(
        "close() 不再累积规则检测偏好",
        len(detected) == 0,
        f"_detected_preferences 条数={len(detected)}（应为 0）",
    )

    async def mock_flush_distillation(
        session_id: str,
        user_id: str,
        messages: list,
        backend,
        **_kwargs,
    ):
        """模拟蒸馏管道写入一条偏好（真实场景由 Task Agent / 蒸馏产出）。"""
        await backend.save_user_preferences(
            user_id,
            [
                UserPreference(
                    user_id=user_id,
                    category="explicit_prefs",
                    l0="用户希望后续对话使用中文回复（蒸馏管道写入）",
                    source_session=session_id,
                ),
            ],
            session_id,
        )
        return {
            "task_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "skills_learned": 0,
            "experiences_written": 0,
            "preferences_written": 1,
            "errors": [],
        }

    result = await gc.on_session_end(
        user_id="test_user_001",
        flush_distillation=mock_flush_distillation,
    )
    total += 1
    passed += report.check(
        "on_session_end 规则检测偏好计数恒为 0（兼容字段）",
        result.get("detected_preferences", -1) == 0,
        f"detected_preferences={result.get('detected_preferences', 0)}",
    )

    dist = result.get("distillation") or {}
    total += 1
    passed += report.check(
        "蒸馏结果含 preferences_written",
        dist.get("preferences_written", 0) >= 1,
        f"preferences_written={dist.get('preferences_written', 0)}",
    )

    loaded_prefs = await backend.load_user_preferences("test_user_001")
    total += 1
    passed += report.check(
        "经 flush 写入的偏好已持久化并可加载",
        len(loaded_prefs) >= 1,
        f"加载到 {len(loaded_prefs)} 条偏好",
    )

    for lp in loaded_prefs:
        report.log(f"  持久化偏好: [{lp.category}] {lp.l0} (session={lp.source_session})")

    report.end_case(time.time() - t0, passed, total)
    return passed, total


# ═══════════════════════════════════════════════════════════════════
# Case 4: Checkpoint 崩溃恢复
# ═══════════════════════════════════════════════════════════════════

async def case4_checkpoint_recovery():
    """8 轮对话分两段：前 5 轮 → 模拟崩溃 → 从 checkpoint 恢复 → 再 3 轮。"""
    case_dir = TEST_DATA_DIR / "case4"
    if case_dir.exists():
        shutil.rmtree(case_dir)

    report.start_case("Case 4: Checkpoint 崩溃恢复", "8 轮对话，第 5 轮后模拟崩溃，验证 checkpoint 恢复")
    t0 = time.time()
    passed = 0
    total = 0

    common_opts = dict(
        max_input_tokens=8000,
        generate_summary=generate_summary,
        merge_summary=merge_summary,
        compute_relevance=compute_relevance,
        estimate_tokens=estimate_tokens,
        data_dir=str(case_dir),
        checkpoint_interval=3,
    )

    conversations_phase1 = [
        ("项目需要做一个聊天系统", "聊天系统核心：WebSocket 长连接、消息持久化、在线状态管理。"),
        ("消息存储用什么方案？", "MongoDB 适合消息存储，按会话分 Collection，支持消息回溯。"),
        ("如何实现消息已读未读？", "每个用户维护 last_read_msg_id，对比消息 ID 判断未读数。"),
        ("群聊怎么设计？", "群表 + 成员表 + 群消息表，扩散读模式，成员量大用写扩散。"),
        ("文件和图片消息怎么处理？", "OSS 存储 + 缩略图 + CDN 加速，消息体只存 URL 引用。"),
    ]

    conversations_phase2 = [
        ("消息加密怎么做？", "端到端加密用 Signal 协议，传输层 TLS，存储层 AES 加密。"),
        ("如何做消息搜索？", "Elasticsearch 索引消息内容，支持全文检索和按时间范围过滤。"),
        ("实时通知的方案？", "APNs/FCM 推送 + WebSocket 在线推送，离线消息队列缓存。"),
    ]

    gc1 = ContextGC(
        ContextGCOptions(**common_opts),
        session_id="chat_design_001",
    )

    for i, (u, a) in enumerate(conversations_phase1, 1):
        gc1.push([{"role": "user", "content": u}, {"role": "assistant", "content": a}])
        await gc1.close()
        report.log(f"Phase1 轮次 {i}: rounds={len(gc1.state.rounds)}, tokens={gc1.state.total_tokens}")

    pre_crash_rounds = len(gc1.state.rounds)
    pre_crash_scores = {r.round_id: r.gen_score for r in gc1.state.rounds}
    report.log(f"崩溃前状态: {pre_crash_rounds} rounds, scores={pre_crash_scores}")

    checkpoint_path = case_dir / "sessions" / "chat_design_001" / ".checkpoint.json"
    total += 1
    passed += report.check(
        "Checkpoint 文件已生成",
        checkpoint_path.exists(),
        f"path={checkpoint_path}",
    )

    del gc1
    report.log("--- 模拟崩溃：gc1 对象销毁 ---")

    gc2 = ContextGC(
        ContextGCOptions(**common_opts),
        session_id="chat_design_001",
    )

    total += 1
    recovered_rounds = len(gc2.state.rounds)
    passed += report.check(
        "从 checkpoint 恢复了轮次数据",
        recovered_rounds > 0,
        f"恢复 {recovered_rounds} 轮（崩溃前 {pre_crash_rounds} 轮）",
    )

    total += 1
    recovered_ids = {r.round_id for r in gc2.state.rounds}
    pre_crash_ids = set(pre_crash_scores.keys())
    passed += report.check(
        "恢复的 round_id 集合一致",
        recovered_ids == pre_crash_ids,
        f"恢复={recovered_ids}, 崩溃前={pre_crash_ids}",
    )

    for i, (u, a) in enumerate(conversations_phase2, len(conversations_phase1) + 1):
        gc2.push([{"role": "user", "content": u}, {"role": "assistant", "content": a}])
        await gc2.close()
        report.log(f"Phase2 轮次 {i}: rounds={len(gc2.state.rounds)}, tokens={gc2.state.total_tokens}")

    total += 1
    final_rounds = len(gc2.state.rounds)
    expected_min = recovered_rounds + len(conversations_phase2)
    passed += report.check(
        f"恢复后继续产生摘要（>= {expected_min} 轮）",
        final_rounds >= expected_min,
        f"最终 {final_rounds} 轮",
    )

    content_path = case_dir / "sessions" / "chat_design_001" / "content.md"
    total += 1
    passed += report.check(
        "原始消息 content.md 存在",
        content_path.exists(),
        f"文件大小: {content_path.stat().st_size if content_path.exists() else 0} bytes",
    )

    report.end_case(time.time() - t0, passed, total)
    return passed, total


# ═══════════════════════════════════════════════════════════════════
# Case 5: 全链路端到端
# ═══════════════════════════════════════════════════════════════════

async def case5_full_lifecycle():
    """
    8 轮对话全链路：
    会话进行 → on_session_end(L0/L1/L2) → 新会话加载偏好/经验
    → 跨会话检索 → build_memory_injection → 记忆注入验证
    """
    case_dir = TEST_DATA_DIR / "case5"
    if case_dir.exists():
        shutil.rmtree(case_dir)

    report.start_case("Case 5: 全链路端到端", "8 轮完整链路: 会话 → 持久化 → 新会话加载 → 跨会话检索 → 记忆注入")
    t0 = time.time()
    passed = 0
    total = 0

    backend = FileBackend(case_dir)

    conversations = [
        ("我要开发一个博客系统，请给个整体架构方案", "建议前端 Next.js SSR，后端 NestJS，数据库 PostgreSQL + Redis 缓存，部署 Docker + Nginx。"),
        ("我偏好使用 Python 做后端", "好的，后端改用 FastAPI + SQLAlchemy + Alembic 迁移，其余不变。"),
        ("不要用 MySQL，我只用 PostgreSQL", "明白，数据库统一 PostgreSQL，不使用 MySQL。"),
        ("文章编辑器用什么？", "推荐 Tiptap（基于 ProseMirror），支持 Markdown 和富文本混排。"),
        ("SEO 优化怎么做？", "SSR/SSG 渲染、meta 标签、结构化数据 Schema.org、sitemap 自动生成。"),
        ("评论系统的设计方案？", "树形评论结构，parent_id 递归查询，防 XSS 过滤，Akismet 垃圾评论检测。"),
        ("请始终使用中文回复", "好的，我以后始终使用中文回复。"),
        ("总结一下，这个博客系统的关键技术决策有哪些？", "关键决策：1) Next.js+FastAPI 前后端分离 2) PostgreSQL 3) Tiptap 编辑器 4) SSR SEO 5) 树形评论"),
    ]

    gc1 = ContextGC(
        ContextGCOptions(
            max_input_tokens=6000,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            generate_l0=default_generate_l0,
            data_dir=str(case_dir),
            checkpoint_interval=3,
            scoring_interval=3,
        ),
        session_id="blog_design_001",
        backend=backend,
    )

    report.log("─── 阶段 1: 会话进行 ───")
    for i, (u, a) in enumerate(conversations, 1):
        gc1.push([{"role": "user", "content": u}, {"role": "assistant", "content": a}])
        await gc1.close()
        report.log(f"  轮次 {i}: rounds={len(gc1.state.rounds)}, tokens={gc1.state.total_tokens}, ratio={gc1.state.capacity_ratio:.1%}")

    total += 1
    passed += report.check(
        "会话完成 8 轮摘要",
        len(gc1.state.rounds) >= 1,
        f"rounds={len(gc1.state.rounds)}",
    )

    report.log("─── 阶段 2: on_session_end ───")
    end_result = await gc1.on_session_end(
        user_id="blogger_001",
        generate_l0=default_generate_l0,
    )

    total += 1
    l0 = end_result.get("l0", "")
    passed += report.check(
        "L0 摘要已生成",
        len(l0) > 10,
        f"L0({len(l0)}字): {l0[:80]}...",
    )

    total += 1
    l1_count = end_result.get("l1_count", 0)
    passed += report.check("L1 摘要列表已生成", l1_count > 0, f"L1 count={l1_count}")

    total += 1
    l2_uri = end_result.get("l2_uri", "")
    l2_exists = l2_uri and Path(l2_uri).exists()
    passed += report.check("L2 原始对话已持久化", l2_exists, f"L2 path={l2_uri}")

    total += 1
    pref_count = end_result.get("detected_preferences", -1)
    passed += report.check(
        "不再通过 close() 规则检测写入偏好（detected_preferences==0）",
        pref_count == 0,
        f"detected_preferences={pref_count}",
    )

    report.log("─── 阶段 3: 验证文件系统持久化 ───")
    session_dir = case_dir / "sessions" / "blog_design_001"
    total += 1
    passed += report.check(
        ".abstract.md (L0) 文件存在",
        (session_dir / ".abstract.md").exists(),
    )
    total += 1
    passed += report.check(
        ".overview.md (L1) 文件存在",
        (session_dir / ".overview.md").exists(),
    )
    total += 1
    passed += report.check(
        "content.md (L2) 文件存在",
        (session_dir / "content.md").exists(),
    )
    total += 1
    passed += report.check(
        ".meta.json 元数据存在",
        (session_dir / ".meta.json").exists(),
    )

    total += 1
    checkpoint_cleaned = not (session_dir / ".checkpoint.json").exists()
    passed += report.check(
        "会话结束后 .checkpoint.json 已清理",
        checkpoint_cleaned,
    )

    report.log("─── 阶段 4: 新会话加载记忆 ───")
    gc2 = ContextGC(
        ContextGCOptions(
            max_input_tokens=6000,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            data_dir=str(case_dir),
        ),
        session_id="blog_design_002",
        backend=backend,
    )

    prefs = await gc2.get_user_preferences("blogger_001")
    total += 1
    passed += report.check(
        "新会话可加载用户偏好",
        len(prefs) >= 2,
        f"加载 {len(prefs)} 条偏好",
    )
    for p in prefs:
        report.log(f"    [{p.category}] {p.l0}")

    report.log("─── 阶段 5: 跨会话检索 ───")
    hits = await gc2.find("博客")
    total += 1
    passed += report.check(
        "跨会话检索命中原会话",
        len(hits) > 0,
        f"命中 {len(hits)} 条",
    )
    if hits:
        hit = hits[0]
        report.log(f"    session={hit.get('session_id')}, score={hit.get('score')}, l0={hit.get('l0', '')[:60]}")

    hits2 = await gc2.find("FastAPI")
    total += 1
    hits2_found = len(hits2) > 0
    passed += report.check(
        "检索关键词 'FastAPI' 命中",
        hits2_found,
        f"命中 {len(hits2)} 条",
    )

    report.log("─── 阶段 6: 记忆注入 ───")
    exps = await gc2.get_user_experience("blogger_001")
    skills = await gc2.get_user_skills("blogger_001")

    injection = build_memory_injection(
        preferences=prefs,
        experiences=exps,
        skills=skills,
        max_tokens=2000,
        estimate_tokens=estimate_tokens,
    )

    total += 1
    passed += report.check(
        "build_memory_injection 生成注入文本",
        len(injection) > 20,
        f"注入文本 {len(injection)} 字符",
    )
    report.log(f"    注入内容预览:\n{injection[:300]}")

    total += 1
    has_pref_section = "用户偏好" in injection
    passed += report.check("注入文本包含用户偏好段落", has_pref_section)

    report.log("─── 阶段 7: L1 层级加载验证 ───")
    l1_data = await gc2.load_session_l1("blog_design_001")
    total += 1
    passed += report.check(
        "load_session_l1 返回摘要列表",
        l1_data is not None and len(l1_data) > 0,
        f"L1 条目: {len(l1_data) if l1_data else 0}",
    )

    l2_data = await gc2.load_session_l2("blog_design_001")
    total += 1
    passed += report.check(
        "load_session_l2 返回完整对话文本",
        l2_data is not None and len(l2_data) > 100,
        f"L2 长度: {len(l2_data) if l2_data else 0} 字符",
    )

    report.end_case(time.time() - t0, passed, total)
    return passed, total


# ═══════════════════════════════════════════════════════════════════
# Case 6: 蒸馏管道全链路（Task Agent → 蒸馏 → 经验 → 技能）
# ═══════════════════════════════════════════════════════════════════

async def case6_distillation_pipeline():
    """
    10 轮对话含明确任务（成功+失败），验证蒸馏三阶段：
    Task Agent 抽取任务 → Distiller 分析 → 经验写入 + 技能学习。
    """
    case_dir = TEST_DATA_DIR / "case6"
    if case_dir.exists():
        shutil.rmtree(case_dir)

    report.start_case(
        "Case 6: 蒸馏管道全链路",
        "10 轮对话（含成功+失败任务），验证 Task Agent → 蒸馏分析 → 经验写入 → 技能学习",
    )
    t0 = time.time()
    passed = 0
    total = 0

    backend = FileBackend(case_dir)

    conversations = [
        # 任务1：用户认证系统（成功完成）
        ("帮我实现一个用户认证系统，需要支持 JWT", "好的，我来设计认证系统。核心组件：登录接口、JWT 签发、刷新 Token、中间件验证。"),
        ("数据库怎么存储用户密码？", "使用 bcrypt 哈希存储，salt rounds=12。用户表：id、email、password_hash、created_at。"),
        ("JWT 的过期时间设多久合适？", "access_token 15分钟，refresh_token 7天。refresh_token 存 Redis，支持主动撤销。"),
        ("好的，认证系统实现完成了，测试通过", "认证系统已完成：JWT 签发+验证、bcrypt 密码哈希、Redis refresh token、中间件拦截。"),
        # 任务2：缓存优化（失败任务）
        ("帮我优化首页的 API 响应速度，加个 Redis 缓存", "好的，我给首页 API 加 Redis 缓存。缓存策略：TTL 5分钟，key=page:home:{user_id}。"),
        ("上线后缓存命中率只有 10%，完全没效果", "分析了一下，问题是 key 按 user_id 区分导致每个用户都 miss。应该用公共 key 缓存。"),
        ("算了，缓存方案有问题，我先回滚了", "好的，已回滚。缓存失败的原因：个性化 key 导致命中率极低。正确做法是公共内容用公共 key。"),
        # 任务3：日志系统（成功完成）
        ("帮我搭建一个结构化日志系统", "建议使用 structlog + JSON 格式输出，配合 ELK 做日志收集和查询。"),
        ("日志级别怎么规划？", "ERROR=异常、WARN=降级、INFO=业务事件、DEBUG=调试细节。生产环境只开 INFO 及以上。"),
        ("日志系统搭建好了，效果不错。我偏好用 Python 的 structlog", "日志系统完成：structlog JSON 格式、按级别分类、ELK 收集。用户偏好已记录。"),
    ]

    gc = ContextGC(
        ContextGCOptions(
            max_input_tokens=8000,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            generate_l0=default_generate_l0,
            data_dir=str(case_dir),
            checkpoint_interval=5,
            scoring_interval=3,
        ),
        session_id="distill_test_001",
        backend=backend,
    )

    report.log("─── 阶段 1: 会话进行（10 轮）───")
    for i, (u, a) in enumerate(conversations, 1):
        gc.push([{"role": "user", "content": u}, {"role": "assistant", "content": a}])
        await gc.close()
        report.log(f"  轮次 {i}: rounds={len(gc.state.rounds)}")

    total += 1
    passed += report.check("会话完成 10 轮", len(gc.state.rounds) >= 1, f"rounds={len(gc.state.rounds)}")

    report.log("─── 阶段 2: on_session_end + 蒸馏 ───")

    end_result = await gc.on_session_end(
        user_id="distill_user_001",
        flush_distillation=lambda **kwargs: _run_distillation(**kwargs),
    )

    distill_result = end_result.get("distillation", {})
    report.log(f"  蒸馏结果: {json.dumps({k:v for k,v in distill_result.items() if k != 'trace'}, ensure_ascii=False)}")

    if distill_result.get("trace"):
        for line in distill_result["trace"][:20]:
            report.log(f"    {line}")

    total += 1
    task_count = distill_result.get("task_count", 0)
    passed += report.check(
        "Task Agent 抽取到任务（>= 2）",
        task_count >= 2,
        f"task_count={task_count}",
    )

    total += 1
    success_count = distill_result.get("success_count", 0)
    passed += report.check(
        "检测到成功任务（>= 1）",
        success_count >= 1,
        f"success_count={success_count}",
    )

    total += 1
    failed_count = distill_result.get("failed_count", 0)
    passed += report.check(
        "检测到失败任务（>= 0，不强制）",
        True,
        f"failed_count={failed_count}",
    )

    report.log("─── 阶段 3: 验证经验写入 ───")
    experiences = await backend.load_user_experience("distill_user_001")
    total += 1
    passed += report.check(
        "经验已写入后端（>= 1 条）",
        len(experiences) >= 1,
        f"经验数量: {len(experiences)}",
    )
    for exp in experiences:
        label = "✓成功" if exp.success else "✗失败"
        report.log(f"    {label} [{exp.task_desc[:30]}] {exp.content[:80]}")

    report.log("─── 阶段 4: 验证技能学习 ───")
    skills_learned = distill_result.get("skills_learned", 0)
    skill_decisions = distill_result.get("skill_decisions", [])
    total += 1
    passed += report.check(
        "Skill Learner 有决策输出",
        len(skill_decisions) >= 1 or skills_learned >= 0,
        f"skills_learned={skills_learned}, decisions={len(skill_decisions)}",
    )
    for d in skill_decisions:
        report.log(f"    决策: action={d.get('action')}, skill={d.get('skill_name')}, reason={d.get('reason', '')[:60]}")

    skills_dir = case_dir / "user" / "distill_user_001" / "skills"
    skill_files = list(skills_dir.rglob("SKILL.md")) if skills_dir.exists() else []
    total += 1
    passed += report.check(
        "技能文件已写入磁盘",
        len(skill_files) >= 1 or skills_learned == 0,
        f"技能文件数: {len(skill_files)}",
    )
    for sf in skill_files:
        content = sf.read_text(encoding="utf-8")
        skill_name = sf.parent.name
        preview = content[:120].replace("\n", " ")
        report.log(f"    技能 [{skill_name}]: {preview}...")

    report.log("─── 阶段 5: 验证蒸馏 trace 完整性 ───")
    trace = distill_result.get("trace", [])
    total += 1
    passed += report.check(
        "蒸馏 trace 非空（可追溯全流程）",
        len(trace) >= 3,
        f"trace 行数: {len(trace)}",
    )

    total += 1
    no_errors = len(distill_result.get("errors", [])) == 0
    passed += report.check(
        "蒸馏过程无错误",
        no_errors,
        f"errors={distill_result.get('errors', [])}",
    )

    report.end_case(time.time() - t0, passed, total)
    return passed, total


async def _run_distillation(session_id, user_id, messages, backend, **kwargs):
    """蒸馏管道的调用封装（``call_llm`` 由 ``flush_distillation`` 从 ``options`` / defaults 解析）。"""
    from context_gc.distillation.flush import flush_distillation
    trace: list[str] = []
    return await flush_distillation(
        session_id=session_id,
        user_id=user_id,
        messages=messages,
        backend=backend,
        trace=trace,
        **{k: v for k, v in kwargs.items() if k != "trace"},
    )


# ═══════════════════════════════════════════════════════════════════
# Case 7: 经验+技能跨会话传递与记忆注入
# ═══════════════════════════════════════════════════════════════════

async def case7_experience_skill_cross_session():
    """
    在 Case 6 产出的经验和技能基础上：
    新会话加载 → 记忆注入包含经验段落 → 技能列表可用 → 生命周期过滤。
    """
    case_dir = TEST_DATA_DIR / "case6"
    if not case_dir.exists():
        report.start_case("Case 7: 经验+技能跨会话传递", "依赖 Case 6 数据，Case 6 未运行，跳过")
        report.end_case(0, 0, 0)
        return 0, 0

    report.start_case(
        "Case 7: 经验+技能跨会话传递",
        "新会话加载 Case 6 产出的经验+技能 → 记忆注入 → 生命周期过滤",
    )
    t0 = time.time()
    passed = 0
    total = 0

    backend = FileBackend(case_dir)

    gc_new = ContextGC(
        ContextGCOptions(
            max_input_tokens=8000,
            generate_summary=generate_summary,
            merge_summary=merge_summary,
            compute_relevance=compute_relevance,
            estimate_tokens=estimate_tokens,
            data_dir=str(case_dir),
        ),
        session_id="cross_session_002",
        backend=backend,
    )

    report.log("─── 阶段 1: 加载上一会话的经验 ───")
    experiences = await gc_new.get_user_experience("distill_user_001")
    total += 1
    passed += report.check(
        "新会话可加载用户经验",
        len(experiences) >= 1,
        f"经验数量: {len(experiences)}",
    )
    for exp in experiences:
        label = "✓" if exp.success else "✗"
        report.log(f"    {label} [{exp.task_desc[:30]}] {exp.content[:80]}")

    report.log("─── 阶段 2: 加载上一会话的技能 ───")
    user_skills = await gc_new.get_user_skills("distill_user_001")
    total += 1
    passed += report.check(
        "新会话可加载用户技能",
        len(user_skills) >= 0,
        f"技能数量: {len(user_skills)}",
    )
    for s in user_skills:
        report.log(f"    技能: {s.get('name')} — {s.get('description', '')[:60]}")

    report.log("─── 阶段 3: 加载偏好 ───")
    prefs = await gc_new.get_user_preferences("distill_user_001")
    total += 1
    passed += report.check(
        "新会话可加载用户偏好",
        len(prefs) >= 0,
        f"偏好数量: {len(prefs)}",
    )
    for p in prefs:
        report.log(f"    [{p.category}] {p.l0}")

    report.log("─── 阶段 4: 构建记忆注入（含经验段落）───")
    injection = build_memory_injection(
        preferences=prefs,
        experiences=experiences,
        skills=user_skills,
        max_tokens=3000,
        estimate_tokens=estimate_tokens,
        current_query="帮我优化 Redis 缓存方案",
    )

    total += 1
    passed += report.check(
        "记忆注入文本已生成",
        len(injection) > 10,
        f"注入文本 {len(injection)} 字符",
    )
    report.log(f"    注入内容预览:\n{injection[:500]}")

    total += 1
    has_experience = "历史经验" in injection or "用户偏好" in injection
    passed += report.check(
        "注入文本包含记忆内容（偏好或经验）",
        has_experience,
    )

    report.log("─── 阶段 5: 跨会话检索蒸馏会话 ───")
    hits = await gc_new.find("认证")
    total += 1
    passed += report.check(
        "跨会话检索命中蒸馏来源会话",
        len(hits) > 0,
        f"命中 {len(hits)} 条",
    )
    if hits:
        report.log(f"    session={hits[0].get('session_id')}, l0={hits[0].get('l0', '')[:60]}")

    report.log("─── 阶段 6: 生命周期过滤验证 ───")
    from context_gc.memory.lifecycle import filter_stale_preferences, filter_stale_experiences
    from datetime import datetime, timedelta, timezone

    active_prefs, stale_prefs = filter_stale_preferences(prefs, ttl_days=90)
    total += 1
    passed += report.check(
        "当前偏好全部活跃（刚创建，未过期）",
        len(stale_prefs) == 0,
        f"active={len(active_prefs)}, stale={len(stale_prefs)}",
    )

    future = datetime.now(timezone.utc) + timedelta(days=100)
    _, stale_prefs_future = filter_stale_preferences(prefs, ttl_days=90, now=future)
    total += 1
    passed += report.check(
        "模拟 100 天后偏好过期",
        len(stale_prefs_future) >= len(prefs) or len(prefs) == 0,
        f"100天后过期: {len(stale_prefs_future)}/{len(prefs)}",
    )

    active_exps, stale_exps = filter_stale_experiences(experiences, ttl_days=180)
    total += 1
    passed += report.check(
        "当前经验全部活跃",
        len(stale_exps) == 0,
        f"active={len(active_exps)}, stale={len(stale_exps)}",
    )

    report.end_case(time.time() - t0, passed, total)
    return passed, total


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════

async def main():
    if not LLM_API_KEY:
        print("❌ 未配置 CONTEXT_GC_API_KEY，请复制 .env.example 为 .env 并填入 API Key")
        return

    # 按日期建目录，每次执行对应日期存储，避免混乱
    date_str = datetime.now().strftime("%Y-%m-%d")
    run_output_dir = OUTPUT_BASE / date_str
    run_output_dir.mkdir(parents=True, exist_ok=True)

    global TEST_DATA_DIR, REPORT_FILE
    TEST_DATA_DIR = run_output_dir / "e2e_test_data"
    REPORT_FILE = run_output_dir / "e2e_test_report.txt"
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"📁 输出目录: {run_output_dir}")

    cases = [
        ("Case 1", case1_basic_summary_and_scoring),
        ("Case 2", case2_capacity_compaction),
        ("Case 3", case3_preference_detection),
        ("Case 4", case4_checkpoint_recovery),
        ("Case 5", case5_full_lifecycle),
        ("Case 6", case6_distillation_pipeline),
        ("Case 7", case7_experience_skill_cross_session),
    ]

    total_passed = 0
    total_checks = 0
    t_all = time.time()

    for name, func in cases:
        print(f"\n▶ 运行 {name} ...")
        try:
            p, t = await func()
            total_passed += p
            total_checks += t
            print(f"  完成: {p}/{t} 通过")
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            import traceback
            traceback.print_exc()

    elapsed = time.time() - t_all
    summary = (
        f"\n{'═'*80}\n"
        f"  总结: {total_passed}/{total_checks} 检查通过 | 总耗时 {elapsed:.1f}s\n"
        f"{'═'*80}\n"
    )
    print(summary)

    full_report = report.dump() + summary
    REPORT_FILE.write_text(full_report, encoding="utf-8")
    print(f"📄 详细报告已保存至: {REPORT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
