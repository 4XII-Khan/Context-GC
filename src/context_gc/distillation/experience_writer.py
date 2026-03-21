"""
distillation/experience_writer.py

经验写入：将蒸馏结果写入用户经验目录。
包含去重和冲突处理逻辑（见设计文档 9.5.1）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..storage.backend import MemoryBackend, UserExperience
from .models import TaskSchema, TaskStatus, DistillationOutcome

_log = logging.getLogger(__name__)


def extract_experiences_from_outcome(
    task: TaskSchema,
    outcome: DistillationOutcome,
    session_id: str,
) -> list[UserExperience]:
    """从蒸馏结果中提取经验条目。"""
    if not outcome.is_worth_learning or not outcome.distilled_text:
        return []

    experiences: list[UserExperience] = []
    text = outcome.distilled_text

    if outcome.tool_name == "report_success_analysis":
        approach = ""
        pattern = ""
        for line in text.splitlines():
            if line.startswith("**Approach:**"):
                approach = line.replace("**Approach:**", "").strip()
            elif line.startswith("**Generalizable Pattern:**"):
                pattern = line.replace("**Generalizable Pattern:**", "").strip()
        content = approach
        if pattern:
            content += f" | SOP: {pattern}"
        if content:
            experiences.append(UserExperience(
                task_desc=task.data.task_description,
                success=True,
                content=content,
                source_session=session_id,
            ))

    elif outcome.tool_name == "report_failure_analysis":
        failure_point = ""
        prevention = ""
        for line in text.splitlines():
            if line.startswith("**Failure Point:**"):
                failure_point = line.replace("**Failure Point:**", "").strip()
            elif line.startswith("**Prevention Principle:**"):
                prevention = line.replace("**Prevention Principle:**", "").strip()
        content = failure_point
        if prevention:
            content += f" | 预防: {prevention}"
        if content:
            experiences.append(UserExperience(
                task_desc=task.data.task_description,
                success=False,
                content=content,
                source_session=session_id,
            ))

    elif outcome.tool_name == "report_factual_content":
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and not stripped.startswith("- **"):
                fact = stripped[2:].strip()
                if fact:
                    experiences.append(UserExperience(
                        task_desc=task.data.task_description,
                        success=True,
                        content=f"[事实] {fact}",
                        source_session=session_id,
                    ))

    return experiences


def _keyword_overlap(text_a: str, text_b: str, threshold: float = 0.8) -> bool:
    """关键词重叠率去重。"""
    kws_a = set(re.findall(r"[\w\u4e00-\u9fff]+", text_a.lower()))
    kws_b = set(re.findall(r"[\w\u4e00-\u9fff]+", text_b.lower()))
    if not kws_a or not kws_b:
        return False
    overlap = len(kws_a & kws_b) / max(len(kws_a | kws_b), 1)
    return overlap > threshold


async def write_experiences(
    user_id: str,
    experiences: list[UserExperience],
    session_id: str,
    backend: MemoryBackend,
    *,
    dedup_strategy: str = "keyword_overlap",
    dedup_threshold: float = 0.8,
) -> int:
    """
    写入经验到后端，返回实际写入条数。

    支持的去重策略：exact / keyword_overlap / none。
    """
    if not experiences:
        return 0

    existing = await backend.load_user_experience(user_id)
    existing_by_success: dict[bool, list[str]] = {True: [], False: []}
    for e in existing:
        existing_by_success[e.success].append(e.content)

    to_write: list[UserExperience] = []
    for exp in experiences:
        existing_contents = existing_by_success.get(exp.success, [])
        is_dup = False

        if dedup_strategy == "exact":
            is_dup = exp.content in existing_contents
        elif dedup_strategy == "keyword_overlap":
            for ec in existing_contents:
                if _keyword_overlap(exp.content, ec, dedup_threshold):
                    is_dup = True
                    break

        if not is_dup:
            to_write.append(exp)

    if to_write:
        await backend.save_user_experience(user_id, to_write, session_id)

    _log.info(
        "[ExperienceWriter] user=%s 输入 %d 条，去重后写入 %d 条",
        user_id, len(experiences), len(to_write),
    )
    return len(to_write)
