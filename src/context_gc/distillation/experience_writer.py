"""
distillation/experience_writer.py

经验写入：将蒸馏结果写入用户经验目录。
包含去重和冲突处理逻辑（见设计文档 9.5.1）。

**基于任务的历史经验**：写入/去重时按每条经验的 ``task_desc`` 调用
``backend.load_user_experience(user_id, task_desc=...)``，只与同任务目录下
（``.task_index.json`` 归并后的 slug）已有条目比对，而不是拉取用户全部经验。

**任务归并**：``task_assign_mode="llm"`` 时，先读取 ``load_user_experience_task_index``，
由大模型将本批 ``task_desc`` 映射到已有任务的 ``canonical_desc`` 或新建 canonical；
此时 ``FileBackend`` 使用 ``use_fuzzy_task_match=False``，避免与 LLM 决策冲突。
``task_assign_mode="heuristic"``（默认）则沿用后端内置的精确匹配 + Jaccard 模糊归并。

与「蒸馏阶段」无关；蒸馏仍只消费任务与消息，历史经验仅在**经验学习写入**时加载。
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import replace
from typing import Callable, Literal

from ..storage.backend import MemoryBackend, UserExperience
from .models import TaskSchema, TaskStatus, DistillationOutcome
from .task_assignment_llm import assign_experience_task_descs_with_llm

CallLLM = Callable[[str, list[dict], list[dict]], dict]

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
    task_assign_mode: Literal["heuristic", "llm"] = "heuristic",
    call_llm: CallLLM | None = None,
) -> int:
    """
    写入经验到后端，返回实际写入条数。

    对每条待写入经验，按 ``task_desc`` 加载**该任务**下已有经验（与技能学习侧
    「按领域看已有技能」同理，此处为按任务看已有经验），再在同 success/failure
    类型内做去重。

    支持的去重策略：exact / keyword_overlap / none。

    ``task_assign_mode``：
    - ``heuristic``：由 ``FileBackend`` 对任务描述做精确匹配 + Jaccard 模糊归并。
    - ``llm``：先调用 ``call_llm``，结合 ``task_index`` 语义归并；需传入 ``call_llm``，
      且后端应实现 ``load_user_experience_task_index``（否则视为空索引）。
    """
    if not experiences:
        return 0

    use_fuzzy = True
    normalized: list[UserExperience] = list(experiences)

    if task_assign_mode == "llm":
        if call_llm is None:
            _log.warning(
                "[ExperienceWriter] task_assign_mode=llm 但未提供 call_llm，回退 heuristic"
            )
        else:
            use_fuzzy = False
            order_preserving: list[str] = []
            seen: set[str] = set()
            for exp in experiences:
                if exp.task_desc not in seen:
                    seen.add(exp.task_desc)
                    order_preserving.append(exp.task_desc)
            try:
                index = await backend.load_user_experience_task_index(user_id)
            except Exception as e:
                _log.warning("[ExperienceWriter] load task_index failed: %s", e)
                index = []
            if not isinstance(index, list):
                index = []
            mapping = assign_experience_task_descs_with_llm(
                order_preserving, index, call_llm
            )
            normalized = [
                replace(exp, task_desc=mapping.get(exp.task_desc, exp.task_desc))
                for exp in experiences
            ]

    task_history_cache: dict[str, list[UserExperience]] = {}

    async def _history_for_task(task_desc: str) -> list[UserExperience]:
        if task_desc not in task_history_cache:
            task_history_cache[task_desc] = await backend.load_user_experience(
                user_id, task_desc=task_desc, use_fuzzy_task_match=use_fuzzy
            )
        return task_history_cache[task_desc]

    # 同批次内同一 (task_desc, success) 已接纳的内容，避免重复写入两条
    pending_by_key: dict[tuple[str, bool], list[str]] = defaultdict(list)

    to_write: list[UserExperience] = []
    for exp in normalized:
        historical = await _history_for_task(exp.task_desc)
        same_type_historical = [e.content for e in historical if e.success == exp.success]
        key = (exp.task_desc, exp.success)
        against = same_type_historical + pending_by_key[key]

        is_dup = False
        if dedup_strategy == "none":
            is_dup = False
        elif dedup_strategy == "exact":
            is_dup = exp.content in against
        elif dedup_strategy == "keyword_overlap":
            for ec in against:
                if _keyword_overlap(exp.content, ec, dedup_threshold):
                    is_dup = True
                    break

        if not is_dup:
            to_write.append(exp)
            pending_by_key[key].append(exp.content)

    if to_write:
        await backend.save_user_experience(
            user_id, to_write, session_id, use_fuzzy_task_match=use_fuzzy
        )

    _log.info(
        "[ExperienceWriter] user=%s 输入 %d 条，按任务加载历史后写入 %d 条 (assign=%s)",
        user_id, len(experiences), len(to_write), task_assign_mode,
    )
    return len(to_write)
