"""
tests/test_e2e_asme.py

基于 ASME 个人助手智能体真实会话记录的端到端深度评测。
覆盖持久化中的任务、偏好、技能、经验等核心能力。

测试分两个场景（因数据有限）：
  场景 A：每个 chatme 文件 = 一次独立会话（不同 session_id），**共用同一 FileBackend 根目录**
        ``{run_dir}/shared_data/``，用户记忆跨文件累积；各文件结果报告仍在 ``per_session/<sid>/``。
  场景 B：所有 chatme 文件合并 = 一次会话，整体测试（独立 ``merged_session/data``）

输出：
  - 每次测试各阶段结果输出到对应目录
  - 最终汇总表格：执行时间、模型、各核心能力结果、每次评价、综合评价

运行：
    python3 tests/test_e2e_asme.py
    或
    python3 -m pytest tests/test_e2e_asme.py -v -s

环境变量（可选）：
    CONTEXT_GC_FLUSH_TOOL_MAX_TOKENS — 蒸馏管道工具调用 max_tokens（见 defaults），默认 8192
    CONTEXT_GC_ASME_E2E_SKIP_MERGED — 设为 1/true 等则只跑场景 A，不跑合并全会话（场景 B）
"""

import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv
from openai import AsyncOpenAI

from context_gc import (
    ContextGC,
    ContextGCOptions,
    FileBackend,
    RoundMeta,
    build_memory_injection,
)
from context_gc.defaults import default_generate_l0

from tests.data.chatme_loader import (
    build_conversation_from_sessions,
    build_merged_push_messages,
    iter_chatme_sessions_with_messages,
    load_all_chatme_rounds,
)

# 加载 .env
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

def _env_flag(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# 为 1/true/yes/on 时跳过「场景 B：所有 chatme 合并为一次会话」
SKIP_MERGED_SESSION = _env_flag("CONTEXT_GC_ASME_E2E_SKIP_MERGED")

_data_dir = Path(__file__).parent / "data"
OUTPUT_BASE = Path(__file__).parent / "output"

_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def _sanitize_dirname(s: str) -> str:
    """将文件名转为安全目录名。"""
    return re.sub(r'[^\w\-\.]', "_", s)[:80]


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


def _rounds_to_messages(rounds: list[tuple[str, str]]) -> list[dict]:
    """将 (user, assistant) 轮次转为消息列表。"""
    out: list[dict] = []
    for u, a in rounds:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": a})
    return out


# ═══════════════════════════════════════════════════════════════════
# 单次测试运行（返回各阶段结果）
# ═══════════════════════════════════════════════════════════════════

@dataclass
class StageResult:
    """单次测试各阶段结果。"""
    compression_ok: bool = False  # 会话内压缩
    compression_rounds: int = 0
    compression_tokens: int = 0
    compression_merged: bool = False

    persistence_ok: bool = False  # 持久化 L0/L1/L2
    persistence_l0_len: int = 0
    persistence_l1_count: int = 0
    persistence_l2_exists: bool = False

    preferences_ok: bool = False  # 偏好
    preferences_count: int = 0

    tasks_ok: bool = False  # 任务（蒸馏 Task Agent）
    tasks_count: int = 0

    experience_ok: bool = False  # 经验
    experience_count: int = 0

    skills_ok: bool = False  # 技能
    skills_count: int = 0

    retrieval_ok: bool = False  # 跨会话检索
    retrieval_hits: int = 0

    injection_ok: bool = False  # 记忆注入
    injection_len: int = 0

    elapsed_sec: float = 0.0
    errors: list[str] = field(default_factory=list)


async def _push_messages_to_gc(
    gc: ContextGC,
    rounds: list[tuple[str, str]],
    full_messages: list[dict] | None,
) -> None:
    """优先使用完整消息（含 assistant steps）；否则退化为 user/content 简版。"""
    if full_messages:
        i = 0
        while i < len(full_messages):
            if full_messages[i].get("role") == "user":
                chunk = [full_messages[i]]
                if i + 1 < len(full_messages) and full_messages[i + 1].get("role") == "assistant":
                    chunk.append(full_messages[i + 1])
                    i += 2
                else:
                    i += 1
                gc.push(chunk)
                await gc.close()
            else:
                gc.push([full_messages[i]])
                await gc.close()
                i += 1
        return
    for u, a in rounds:
        gc.push([{"role": "user", "content": u}, {"role": "assistant", "content": a}])
        await gc.close()


async def run_single_session(
    session_id: str,
    rounds: list[tuple[str, str]],
    data_dir: Path,
    output_dir: Path,
    *,
    full_messages: list[dict] | None = None,
    clear_data_dir: bool = True,
) -> StageResult:
    """
    对一轮会话执行完整测试（压缩 → 持久化 → 蒸馏 → 检索 → 注入）。

    Args:
        clear_data_dir: 为 True 时先清空 ``data_dir``（单测/合并会话等独立根目录）。
            为 False 时保留已有内容，仅确保目录存在，用于多会话共用同一持久化根、只换 ``session_id``。
    """
    res = StageResult()
    t0 = time.time()
    user_id = "asme_user"
    agent_id = "asme_agent"

    if not rounds:
        res.errors.append("无对话轮次")
        return res

    if clear_data_dir and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    backend = FileBackend(data_dir)
    opts = ContextGCOptions(
        max_input_tokens=8000,
        generate_summary=generate_summary,
        merge_summary=merge_summary,
        compute_relevance=compute_relevance,
        estimate_tokens=estimate_tokens,
        generate_l0=default_generate_l0,
        data_dir=str(data_dir),
        checkpoint_interval=2,
        scoring_interval=2,
        flush_min_messages=2,
    )
    gc = ContextGC(opts, session_id=session_id, backend=backend)

    # ─── 阶段 1: 会话内压缩 ───
    try:
        await _push_messages_to_gc(gc, rounds, full_messages)
        res.compression_rounds = len(gc.state.rounds)
        res.compression_tokens = gc.state.total_tokens
        res.compression_merged = any(r.is_merged for r in gc.state.rounds)
        res.compression_ok = res.compression_rounds >= 1 and all(
            r.summary and len(r.summary) > 5 for r in gc.state.rounds
        )
    except Exception as e:
        res.errors.append(f"压缩异常: {e}")

    # ─── 阶段 2: 持久化 + 蒸馏 ───
    async def _run_distillation(**kwargs):
        from context_gc.distillation.flush import flush_distillation
        trace: list[str] = []
        # min_messages 等已由 on_session_end 从 ContextGCOptions 注入，勿重复传参
        kw = {k: v for k, v in kwargs.items() if k != "trace"}
        r = await flush_distillation(**kw, trace=trace)
        r["trace"] = trace
        return r

    try:
        end_result = await gc.on_session_end(
            user_id=user_id,
            agent_id=agent_id,
            flush_distillation=_run_distillation,
        )
        res.persistence_l0_len = len(end_result.get("l0", ""))
        res.persistence_l1_count = end_result.get("l1_count", 0)
        res.persistence_l2_exists = bool(end_result.get("l2_uri") and Path(end_result.get("l2_uri", "")).exists())
        res.persistence_ok = res.persistence_l0_len > 0 and res.persistence_l1_count > 0

        dist0 = end_result.get("distillation") or {}
        res.preferences_count = dist0.get("preferences_written", 0)
        res.preferences_ok = res.preferences_count >= 0

        dist = end_result.get("distillation", {})
        res.tasks_count = dist.get("task_count", 0)
        res.tasks_ok = res.tasks_count >= 0  # 无异常即 OK
        res.experience_count = dist.get("experiences_written", 0)
        res.experience_ok = res.experience_count >= 0
        skills_learned = dist.get("skills_learned", 0)
        res.skills_count = skills_learned
        res.skills_ok = skills_learned >= 0
        if dist.get("errors"):
            res.errors.extend(dist["errors"])
    except Exception as e:
        res.errors.append(f"持久化/蒸馏异常: {e}")

    # ─── 阶段 3: 跨会话检索 + 记忆注入 ───
    try:
        gc2 = ContextGC(
            ContextGCOptions(
                max_input_tokens=6000,
                generate_summary=generate_summary,
                merge_summary=merge_summary,
                compute_relevance=compute_relevance,
                estimate_tokens=estimate_tokens,
                data_dir=str(data_dir),
            ),
            session_id="query_sess",
            backend=backend,
        )
        hits = await gc2.find(rounds[0][0][:20] if rounds else "对话")
        res.retrieval_hits = len(hits)
        res.retrieval_ok = True

        prefs = await gc2.get_user_preferences(user_id)
        exps = await gc2.get_user_experience(user_id)
        skills = await gc2.get_user_skills(user_id)
        injection = build_memory_injection(
            preferences=prefs,
            experiences=exps,
            skills=skills,
            max_tokens=2000,
            estimate_tokens=estimate_tokens,
        )
        res.injection_len = len(injection)
        res.injection_ok = True
    except Exception as e:
        res.errors.append(f"检索/注入异常: {e}")

    res.elapsed_sec = time.time() - t0
    return res


def _write_stage_report(output_dir: Path, run_name: str, res: StageResult, rounds_count: int):
    """将单次测试各阶段结果写入目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    stages = [
        ("1_会话内压缩", {
            "通过": res.compression_ok,
            "rounds": res.compression_rounds,
            "tokens": res.compression_tokens,
            "有合并": res.compression_merged,
        }),
        ("2_持久化", {
            "通过": res.persistence_ok,
            "L0长度": res.persistence_l0_len,
            "L1数量": res.persistence_l1_count,
            "L2存在": res.persistence_l2_exists,
        }),
        ("3_偏好", {"通过": res.preferences_ok, "检测数量": res.preferences_count}),
        ("4_任务", {"通过": res.tasks_ok, "抽取数量": res.tasks_count}),
        ("5_经验", {"通过": res.experience_ok, "写入数量": res.experience_count}),
        ("6_技能", {"通过": res.skills_ok, "学习数量": res.skills_count}),
        ("7_跨会话检索", {"通过": res.retrieval_ok, "命中数": res.retrieval_hits}),
        ("8_记忆注入", {"通过": res.injection_ok, "注入长度": res.injection_len}),
    ]
    report_lines = [
        f"# {run_name} 各阶段测试结果",
        f"输入轮数: {rounds_count}",
        f"耗时: {res.elapsed_sec:.1f}s",
        f"模型: {LLM_MODEL}",
        "",
    ]
    for name, data in stages:
        report_lines.append(f"## {name}")
        for k, v in data.items():
            report_lines.append(f"  {k}: {v}")
        report_lines.append("")
    if res.errors:
        report_lines.append("## 错误")
        for e in res.errors:
            report_lines.append(f"  - {e}")
    (output_dir / "report.txt").write_text("\n".join(report_lines), encoding="utf-8")
    (output_dir / "stages.json").write_text(
        json.dumps({n: d for n, d in stages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _cap_result(res: StageResult) -> str:
    """汇总单次测试核心能力结果为简写。"""
    items = [
        "压缩✓" if res.compression_ok else "压缩✗",
        "持久✓" if res.persistence_ok else "持久✗",
        f"偏好{res.preferences_count}",
        f"任务{res.tasks_count}",
        f"经验{res.experience_count}",
        f"技能{res.skills_count}",
        "检索✓" if res.retrieval_ok else "检索✗",
        "注入✓" if res.injection_ok else "注入✗",
    ]
    return " | ".join(items)


def _eval_single(res: StageResult) -> str:
    """单次测试评价。"""
    if res.errors:
        return "存在异常"
    ok_count = sum([
        res.compression_ok,
        res.persistence_ok,
        res.preferences_ok,
        res.tasks_ok,
        res.experience_ok,
        res.skills_ok,
        res.retrieval_ok,
        res.injection_ok,
    ])
    if ok_count >= 7:
        return "优秀"
    if ok_count >= 5:
        return "良好"
    if ok_count >= 3:
        return "一般"
    return "需改进"


# ═══════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════

async def main(*, include_merged_session: bool = True) -> tuple[int, int]:
    """运行场景 A；可选运行场景 B（合并全部 chatme 为一次会话）。

    Args:
        include_merged_session: 为 False 时跳过场景 B。另：环境变量 ``CONTEXT_GC_ASME_E2E_SKIP_MERGED``
            为 1/true 等时会**强制**跳过场景 B（优先级高于本参数）。

    Pytest 默认 ``include_merged_session=False``；直接 ``python tests/test_e2e_asme.py`` 默认为 True。
    """
    run_merged = include_merged_session and not SKIP_MERGED_SESSION
    if not LLM_API_KEY:
        print("❌ 未配置 CONTEXT_GC_API_KEY，请复制 .env.example 为 .env 并填入 API Key")
        return 0, 0

    date_str = datetime.now().strftime("%Y-%m-%d")
    run_dir = OUTPUT_BASE / date_str / "asme_e2e"
    run_dir.mkdir(parents=True, exist_ok=True)

    per_session_dir = run_dir / "per_session"
    merged_dir = run_dir / "merged_session"
    # 场景 A：所有 chatme 共用此目录，仅 session_id 按文件变化
    shared_data_dir = run_dir / "shared_data"

    all_results: list[tuple[str, StageResult, int]] = []
    t_all = time.time()

    # ─── 场景 A: 每个 chatme 文件 = 一次会话（共用 shared_data） ───
    print("\n" + "=" * 60)
    print("  场景 A：每文件独立 session_id，共用 shared_data/ 持久化根")
    print("=" * 60)
    if shared_data_dir.exists():
        shutil.rmtree(shared_data_dir)
    shared_data_dir.mkdir(parents=True, exist_ok=True)
    per_session_dir.mkdir(parents=True, exist_ok=True)
    (per_session_dir / "README.txt").write_text(
        "本目录下各子文件夹仅含该 chatme 的 report.txt / stages.json。\n"
        f"所有会话的 sessions/、user/ 等数据在: {shared_data_dir.resolve()}\n",
        encoding="utf-8",
    )
    for path, rounds, raw_msgs in iter_chatme_sessions_with_messages(_data_dir):
        sid = _sanitize_dirname(path.stem)
        out_dir = per_session_dir / sid
        print(f"\n▶ 会话: {path.name[:50]}... ({len(rounds)} 轮)")
        try:
            res = await run_single_session(
                sid,
                rounds,
                shared_data_dir,
                out_dir,
                full_messages=raw_msgs,
                clear_data_dir=False,
            )
            all_results.append((f"per_{sid[:30]}", res, len(rounds)))
            _write_stage_report(out_dir, sid, res, len(rounds))
            print(f"  耗时 {res.elapsed_sec:.1f}s | {_cap_result(res)} | {_eval_single(res)}")
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            import traceback
            traceback.print_exc()

    # ─── 场景 B: 所有 chatme 合并 = 一次会话 ───
    if not run_merged:
        print("\n" + "=" * 60)
        why = (
            "（CONTEXT_GC_ASME_E2E_SKIP_MERGED 已开启）"
            if SKIP_MERGED_SESSION
            else "（本运行未请求场景 B）"
        )
        print(f"  场景 B：已跳过{why}")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("  场景 B：所有 chatme 文件合并 = 一次会话")
        print("=" * 60)
    all_rounds = (
        [] if not run_merged else build_conversation_from_sessions(_data_dir, max_rounds=None)
    )
    merged_full = [] if not run_merged else build_merged_push_messages(_data_dir)
    if all_rounds:
        sid = "merged_all"
        out_dir = merged_dir
        data_dir_for_run = out_dir / "data"
        print(f"\n▶ 合并会话: {len(all_rounds)} 轮")
        try:
            res = await run_single_session(
                sid,
                all_rounds,
                data_dir_for_run,
                out_dir,
                full_messages=merged_full if merged_full else None,
            )
            all_results.append((sid, res, len(all_rounds)))
            _write_stage_report(out_dir, "合并会话", res, len(all_rounds))
            print(f"  耗时 {res.elapsed_sec:.1f}s | {_cap_result(res)} | {_eval_single(res)}")
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            import traceback
            traceback.print_exc()

    elapsed = time.time() - t_all

    # ─── 汇总表格 ───
    table_lines = [
        "",
        "=" * 100,
        "  汇总表格",
        "=" * 100,
        "",
        f"| 执行 | 耗时(s) | 模型 | 会话压缩 | 持久化 | 偏好 | 任务 | 经验 | 技能 | 检索 | 注入 | 评价 |",
        f"|------|---------|------|----------|--------|------|------|------|------|------|------|------|",
    ]
    for name, res, n_rounds in all_results:
        row = (
            f"| {name[:20]} | {res.elapsed_sec:.1f} | {LLM_MODEL[:15]} | "
            f"{'✓' if res.compression_ok else '✗'} | {'✓' if res.persistence_ok else '✗'} | "
            f"{res.preferences_count} | {res.tasks_count} | {res.experience_count} | {res.skills_count} | "
            f"{'✓' if res.retrieval_ok else '✗'} | {'✓' if res.injection_ok else '✗'} | {_eval_single(res)} |"
        )
        table_lines.append(row)
    table_lines.append("")
    table_lines.append(f"总耗时: {elapsed:.1f}s")
    table_lines.append("")

    # 综合评价
    ok_counts = [
        sum(1 for _, r, _ in all_results if r.compression_ok),
        sum(1 for _, r, _ in all_results if r.persistence_ok),
        sum(1 for _, r, _ in all_results if r.retrieval_ok),
        sum(1 for _, r, _ in all_results if r.injection_ok),
    ]
    total_runs = len(all_results)
    overall = (
        "优秀" if total_runs and all(ok_counts[i] == total_runs for i in range(4))
        else "良好" if total_runs and min(ok_counts) >= total_runs * 0.7
        else "一般" if total_runs else "无数据"
    )
    table_lines.extend([
        "## 综合评价",
        f"  - 会话数: {total_runs}",
        f"  - 压缩通过率: {ok_counts[0]}/{total_runs}" if total_runs else "  - 无",
        f"  - 持久化通过率: {ok_counts[1]}/{total_runs}" if total_runs else "  - 无",
        f"  - 综合评价: {overall}",
        "",
    ])
    summary_text = "\n".join(table_lines)
    print(summary_text)

    report_file = run_dir / "summary_table.txt"
    report_file.write_text(
        f"Context GC × ASME E2E 评测汇总\n"
        f"日期: {date_str}\n"
        f"模型: {LLM_MODEL}\n"
        f"输出目录: {run_dir}\n"
        f"场景 A 共用持久化根: {run_dir / 'shared_data'}\n"
        f"{summary_text}",
        encoding="utf-8",
    )
    print(f"\n📄 汇总表格已保存至: {report_file}")

    total_checks = sum(
        8 for _ in all_results
    )
    total_passed = sum(
        sum([
            r.compression_ok, r.persistence_ok, r.preferences_ok, r.tasks_ok,
            r.experience_ok, r.skills_ok, r.retrieval_ok, r.injection_ok,
        ]) for _, r, _ in all_results
    )
    return total_passed, total_checks


# ═══════════════════════════════════════════════════════════════════
# Pytest 入口
# ═══════════════════════════════════════════════════════════════════

def test_e2e_asme_integration():
    """Pytest 入口：运行 ASME e2e 评测。需配置 CONTEXT_GC_API_KEY。"""
    import pytest
    if not LLM_API_KEY:
        pytest.skip("未配置 CONTEXT_GC_API_KEY，跳过 ASME e2e 测试")
    passed, total = asyncio.run(main(include_merged_session=False))
    assert passed > 0, f"ASME e2e: 无通过项"
    assert total > 0, "ASME e2e: 无测试运行"


if __name__ == "__main__":
    asyncio.run(main(include_merged_session=True))
