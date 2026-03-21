"""
distillation/flush.py

蒸馏管道入口：Task Agent → 蒸馏 → 经验写入 + Skill Learner。

宿主在 on_session_end 时调用，传入 L2 (raw_messages) 和 backend。
LLM 调用通过 ContextGCOptions 中的回调间接注入。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ..storage.backend import MemoryBackend, UserPreference
from .models import TaskSchema, TaskStatus
from .task_agent import run_task_agent
from .distiller import process_distillation
from .experience_writer import extract_experiences_from_outcome, write_experiences
from .skill_learner import run_skill_learner, get_user_learn_lock

_log = logging.getLogger(__name__)

CallLLM = Callable[[str, list[dict], list[dict]], dict]


def _get_messages_for_task(task: TaskSchema, all_messages: list[dict]) -> list[dict]:
    """根据 task.raw_message_ids 提取关联的消息子集。"""
    if not task.raw_message_ids:
        return all_messages
    indices: set[int] = set()
    for mid in task.raw_message_ids:
        try:
            indices.add(int(mid))
        except (ValueError, TypeError):
            continue
    if not indices:
        return all_messages
    return [m for i, m in enumerate(all_messages) if i in indices]


async def flush_distillation(
    session_id: str,
    user_id: str,
    messages: list[dict],
    backend: MemoryBackend,
    options: Any = None,
    *,
    call_llm: CallLLM | None = None,
    min_messages: int = 4,
    task_agent_max_iterations: int = 20,
    skill_learner_max_iterations: int = 10,
    dedup_strategy: str = "keyword_overlap",
    trace: list[str] | None = None,
) -> dict:
    """
    执行完整的三阶段蒸馏管道。

    Args:
        call_llm: 同步 LLM 调用回调 (system, messages, tools) -> response。
                  若未提供，尝试从 options 构建。
        options: ContextGCOptions（可选，用于构建默认 call_llm）。

    Returns:
        {task_count, success_count, failed_count, skills_learned, experiences_written, errors, trace}
    """
    _trace = trace if trace is not None else []
    result: dict[str, Any] = {
        "task_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "skills_learned": 0,
        "experiences_written": 0,
        "errors": [],
    }

    if not messages or len(messages) < min_messages:
        _trace.append(f"消息不足: {len(messages or [])} < {min_messages}")
        result["trace"] = _trace
        return result

    # 需要一个 call_llm 回调
    if call_llm is None:
        _trace.append("[flush] 未提供 call_llm 回调，跳过蒸馏")
        result["trace"] = _trace
        return result

    t_start = time.perf_counter()
    _trace.append(f"[1] 传入 {len(messages)} 条消息")

    # ── 阶段 1：Task Agent ──
    t_task = time.perf_counter()
    try:
        tasks, pending_prefs = run_task_agent(
            session_id=session_id,
            messages=messages,
            call_llm=call_llm,
            max_iterations=task_agent_max_iterations,
            trace=_trace,
        )
    except Exception as e:
        _log.error("Task Agent failed: %s", e)
        result["errors"].append(f"Task Agent: {e}")
        result["trace"] = _trace
        return result

    t_task_ms = int((time.perf_counter() - t_task) * 1000)
    result["task_count"] = len(tasks)
    _trace.append(f"[2] Task Agent 抽取 {len(tasks)} 个任务 ({t_task_ms} ms)")

    # 保存 pending_prefs 作为偏好
    if pending_prefs and backend:
        prefs = [
            UserPreference(
                user_id=user_id,
                category="explicit_prefs",
                l0=p,
                source_session=session_id,
            )
            for p in pending_prefs
        ]
        try:
            await backend.save_user_preferences(user_id, prefs, session_id)
        except Exception as e:
            _log.warning("Save preferences failed: %s", e)

    # ── 阶段 2：蒸馏 ──
    t_distill = time.perf_counter()
    finished = [t for t in tasks if t.status in (TaskStatus.SUCCESS, TaskStatus.FAILED)]
    result["success_count"] = len([t for t in finished if t.status == TaskStatus.SUCCESS])
    result["failed_count"] = len([t for t in finished if t.status == TaskStatus.FAILED])
    _trace.append(f"[3] 蒸馏: {len(finished)} 个 success/failed 任务")

    # 获取技能描述供蒸馏参考
    skill_descs: list[tuple[str, str]] = []
    try:
        user_skills = await backend.load_user_skills(user_id)
        skill_descs = [(s["name"], s.get("description", "")) for s in user_skills]
    except Exception:
        pass

    distilled_items: list[str] = []
    all_experiences: list[tuple[TaskSchema, Any]] = []

    for task in finished:
        task_msgs = _get_messages_for_task(task, messages)
        try:
            outcome = process_distillation(
                task=task,
                task_messages=task_msgs,
                all_tasks=tasks,
                call_llm=call_llm,
                skill_descriptions=skill_descs,
                trace=_trace,
            )
        except Exception as e:
            result["errors"].append(f"Distillation task {task.order}: {e}")
            continue

        if outcome.is_worth_learning and outcome.distilled_text:
            distilled_items.append(outcome.distilled_text)
            all_experiences.append((task, outcome))

    t_distill_ms = int((time.perf_counter() - t_distill) * 1000)
    _trace.append(f"  蒸馏耗时: {t_distill_ms} ms")

    # ── 阶段 3a：经验写入 ──
    from .experience_writer import extract_experiences_from_outcome
    experiences_to_write = []
    for task, outcome in all_experiences:
        exps = extract_experiences_from_outcome(task, outcome, session_id)
        experiences_to_write.extend(exps)

    if experiences_to_write:
        try:
            written = await write_experiences(
                user_id=user_id,
                experiences=experiences_to_write,
                session_id=session_id,
                backend=backend,
                dedup_strategy=dedup_strategy,
            )
            result["experiences_written"] = written
        except Exception as e:
            result["errors"].append(f"Experience write: {e}")

    # ── 阶段 3b：Skill Learner ──
    t_skill = time.perf_counter()
    _trace.append("[4] 技能学习阶段")

    if pending_prefs:
        pref_text = "## User Preferences Observed\n" + "\n".join(f"- {p}" for p in pending_prefs)
        distilled_items.append(pref_text)

    if distilled_items:
        combined = "\n\n---\n\n".join(distilled_items)

        # 技能目录：user/{user_id}/skills/
        from pathlib import Path
        skills_dir = ""
        if hasattr(backend, "data_dir"):
            skills_dir = str(Path(backend.data_dir) / "user" / user_id / "skills")

        if skills_dir:
            try:
                touched, decisions = run_skill_learner(
                    distilled_context=combined,
                    skills_dir=skills_dir,
                    call_llm=call_llm,
                    max_iterations=skill_learner_max_iterations,
                    session_id=session_id,
                    trace=_trace,
                )
                result["skills_learned"] = len(touched)
                result["skill_decisions"] = decisions
            except Exception as e:
                result["errors"].append(f"Skill Learner: {e}")
        else:
            _trace.append("  无 skills_dir，跳过技能学习")
    else:
        _trace.append("  无蒸馏结果，跳过技能学习")

    t_skill_ms = int((time.perf_counter() - t_skill) * 1000)
    t_total_ms = int((time.perf_counter() - t_start) * 1000)
    _trace.append(f"[耗时] Task Agent: {t_task_ms} ms | 蒸馏: {t_distill_ms} ms | 技能: {t_skill_ms} ms | 总计: {t_total_ms} ms")
    result["trace"] = _trace
    return result
