"""
distillation/distill_prompt.py

蒸馏管道提示词（复用 AsMe 设计）。
"""

from __future__ import annotations

from .models import TaskSchema, TaskStatus
from .task_prompt import format_message_blob

SUCCESS_DISTILLATION_PROMPT = """分析这个成功完成的任务，并选择恰当的工具：

**使用 skip_learning**：当任务琐碎时——例如简单事实查询、闲聊、一次性计算、通用问答、琐碎状态检查。
若列出了学习空间的技能，且任务内容与任一技能相关，则不算琐碎。skip 时 reason 需简要说明为何琐碎。

**使用 report_success_analysis**：当任务涉及多步骤流程、调试、配置或重要决策过程时：
- task_goal：用户想要什么（1 句）
- approach：有效的策略（2–3 句），将作为技能学习的 Principle 来源
- key_decisions：关键决策或动作（列表，每项 1 句），将作为技能学习的 Steps 来源
- generalizable_pattern：可复用的 SOP（2–3 句），供技能学习提炼为 When to Apply 和条目内容

**使用 report_factual_content**：当任务主要是记录信息——人物、事实、偏好、实体或领域知识时：
- task_goal：简要背景（1 句）
- facts：简洁、自洽的第三人称事实陈述列表，供技能学习写入用户偏好类技能

选择最匹配的工具。不要将简单内容包装成虚假流程。
「用户」指发送消息的人（role: user）。"""


FAILURE_DISTILLATION_PROMPT = """分析这个失败的任务，并调用 report_failure_analysis，填入：

- task_goal：用户想要什么（1 句）
- failure_point：方法在何处出错（2–3 句）
- flawed_reasoning：错误的假设或不当行为（2–3 句）
- what_should_have_been_done：正确的做法（2–3 句），供技能学习提炼为 Correct Approach
- prevention_principle：防止此类失败的通用规则（1–2 句），供技能学习提炼为 Prevention 条目

聚焦可执行的教训，而非归咎。
「用户」指发送消息的人（role: user）。"""


def pack_distillation_input(
    task: TaskSchema,
    task_messages: list[dict],
    all_tasks: list[TaskSchema],
    skill_descriptions: list[tuple[str, str]] | None = None,
) -> str:
    """组装蒸馏输入。"""
    status_label = "成功" if task.status == TaskStatus.SUCCESS else "失败"
    progress_str = "\n".join(f"  - {p}" for p in (task.data.progresses or []))

    parts = [
        "## 已完成任务",
        f"- 状态: {status_label}",
        f"- 描述: {task.data.task_description}",
    ]
    if progress_str:
        parts.append(f"- 进度:\n{progress_str}")

    all_tasks_str = "\n".join(f"- {t.to_string()}" for t in all_tasks)
    parts.append(f"\n## 本会话全部任务\n{all_tasks_str}")

    if task_messages:
        msg_lines = "\n---\n".join(format_message_blob(m) for m in task_messages)
        parts.append(f"\n## 任务相关消息\n{msg_lines}")

    if skill_descriptions:
        skills_str = "\n".join(f"- **{name}**: {desc}" for name, desc in skill_descriptions)
        parts.append(f"\n## 学习空间技能\n{skills_str}")

    return "\n".join(parts)
