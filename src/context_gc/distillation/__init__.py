"""
context_gc.distillation — 记忆蒸馏管道。

三阶段管道：Task Agent → 蒸馏 → 经验写入 + Skill Learner。
复用 AsMe 的 prompt 设计，自包含实现。
"""

from .models import TaskSchema, TaskStatus, DistillationOutcome
from .flush import flush_distillation
from .task_assignment_llm import assign_experience_task_descs_with_llm

__all__ = [
    "TaskSchema",
    "TaskStatus",
    "DistillationOutcome",
    "flush_distillation",
    "assign_experience_task_descs_with_llm",
]
