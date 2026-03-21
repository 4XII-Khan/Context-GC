"""
distillation/task_agent.py

Task Agent — 从会话消息中抽取结构化任务。

LLM 调用通过 call_llm 回调注入，保持模型无关。
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Any

from .models import TaskSchema
from .task_prompt import TASK_SYSTEM_PROMPT, pack_task_input
from .task_tools import TASK_TOOL_SCHEMAS, TaskToolContext

_log = logging.getLogger(__name__)

# call_llm 签名: (system, messages, tools) -> dict
# 返回值需包含 content(str)、tool_calls([{id, function:{name, arguments}}])
CallLLM = Callable[[str, list[dict], list[dict]], dict]


def run_task_agent(
    session_id: str,
    messages: list[dict],
    call_llm: CallLLM,
    *,
    existing_tasks: list[TaskSchema] | None = None,
    max_iterations: int = 20,
    system_prompt: str = "",
    trace: list[str] | None = None,
) -> tuple[list[TaskSchema], list[str]]:
    """
    执行 Task Agent CRUD，返回 (tasks, pending_preferences)。

    Args:
        call_llm: 宿主注入的 LLM 调用回调。
        max_iterations: 最大迭代轮次。
    """
    _trace = trace if trace is not None else []
    system = system_prompt.strip() or TASK_SYSTEM_PROMPT

    ctx = TaskToolContext(session_id, existing_tasks)
    user_input = pack_task_input(messages=messages, existing_tasks=ctx.tasks)

    _log.info("[TaskAgent] session=%s 传入 %d 条消息", session_id, len(messages))
    llm_messages: list[dict] = [{"role": "user", "content": user_input}]

    invalid_tool_count = 0

    for iteration in range(max_iterations):
        try:
            resp = call_llm(system, llm_messages, TASK_TOOL_SCHEMAS)
        except Exception as e:
            _log.error("Task Agent LLM call failed: %s", e)
            _trace.append(f"  [TaskAgent] LLM 异常: {e}")
            break

        llm_messages.append(resp)
        tool_calls = resp.get("tool_calls")

        if not tool_calls:
            _trace.append(f"  iter={iteration} 无 tool_calls，结束")
            break

        tool_responses: list[dict] = []
        should_finish = False
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except json.JSONDecodeError:
                    fn_args = {}

            if fn_name == "finish":
                should_finish = True
                tool_responses.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "FINISH",
                })
                continue

            result = ctx.execute(fn_name, fn_args)
            if result.startswith("Error:"):
                invalid_tool_count += 1
            _trace.append(f"    → {fn_name}: {result}")
            tool_responses.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        llm_messages.extend(tool_responses)

        if should_finish:
            break
        if invalid_tool_count >= 3:
            _trace.append("  无效工具调用过多，提前结束")
            break

    _trace.append(f"  最终抽取 {len(ctx.tasks)} 个任务")
    return ctx.tasks, ctx.pending_preferences
