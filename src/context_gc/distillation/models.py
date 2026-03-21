"""
distillation/models.py

蒸馏管道的核心数据模型。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class TaskData:
    """任务核心数据。"""
    task_description: str = ""
    progresses: list[str] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)


@dataclass
class TaskSchema:
    """完整任务结构。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    order: int = 1
    status: TaskStatus = TaskStatus.PENDING
    data: TaskData = field(default_factory=TaskData)
    raw_message_ids: list[str] = field(default_factory=list)
    planning_content: str = ""

    def to_string(self) -> str:
        lines = [
            f"任务 #{self.order}: {self.data.task_description}",
            f"状态: {self.status}",
        ]
        if self.data.progresses:
            lines.append("进度:")
            for p in self.data.progresses:
                lines.append(f"  - {p}")
        if self.raw_message_ids:
            lines.append(f"关联消息: {', '.join(self.raw_message_ids[:5])}")
            if len(self.raw_message_ids) > 5:
                lines.append(f"  ... 共 {len(self.raw_message_ids)} 条")
        return "\n".join(lines)


@dataclass
class DistillationOutcome:
    """蒸馏结果。"""
    is_worth_learning: bool = False
    distilled_text: str = ""
    skip_reason: str = ""
    tool_name: str | None = None
