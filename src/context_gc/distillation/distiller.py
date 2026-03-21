"""
distillation/distiller.py

蒸馏管道 — 对每个 success/failed task 调用一次 LLM。
"""

from __future__ import annotations

import logging
from typing import Callable

from .models import TaskSchema, TaskStatus, DistillationOutcome
from .distill_prompt import (
    SUCCESS_DISTILLATION_PROMPT,
    FAILURE_DISTILLATION_PROMPT,
    pack_distillation_input,
)
from .distill_tools import DISTILL_TOOL_SCHEMAS, extract_distillation_result

_log = logging.getLogger(__name__)

CallLLM = Callable[[str, list[dict], list[dict]], dict]


def process_distillation(
    task: TaskSchema,
    task_messages: list[dict],
    all_tasks: list[TaskSchema],
    call_llm: CallLLM,
    *,
    skill_descriptions: list[tuple[str, str]] | None = None,
    success_prompt: str = "",
    failure_prompt: str = "",
    trace: list[str] | None = None,
) -> DistillationOutcome:
    """对单个任务执行蒸馏。"""
    _trace = trace if trace is not None else []

    if task.status == TaskStatus.SUCCESS:
        system = success_prompt.strip() or SUCCESS_DISTILLATION_PROMPT
        tools = DISTILL_TOOL_SCHEMAS
    elif task.status == TaskStatus.FAILED:
        system = failure_prompt.strip() or FAILURE_DISTILLATION_PROMPT
        tools = [t for t in DISTILL_TOOL_SCHEMAS if t["function"]["name"] == "report_failure_analysis"]
    else:
        return DistillationOutcome(is_worth_learning=False, skip_reason=f"Task status is {task.status}")

    user_content = pack_distillation_input(
        task=task,
        task_messages=task_messages,
        all_tasks=all_tasks,
        skill_descriptions=skill_descriptions,
    )

    _trace.append(f"  [Distill] task={task.id[:8]} 输入长度={len(user_content)}")

    try:
        resp = call_llm(system, [{"role": "user", "content": user_content}], tools)
    except Exception as e:
        _log.error("Distillation LLM call failed: %s", e)
        _trace.append(f"  [Distill] LLM 异常: {e}")
        return DistillationOutcome(is_worth_learning=False, skip_reason=f"LLM error: {e}")

    outcome = extract_distillation_result(resp)
    _trace.append(f"  [Distill] task={task.id[:8]} worth={outcome.is_worth_learning}")
    return outcome
