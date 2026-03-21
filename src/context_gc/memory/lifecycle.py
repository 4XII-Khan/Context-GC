"""
context_gc/memory/lifecycle.py

记忆生命周期管理：偏好/经验/技能的老化、归档和注入容量控制。

设计见 9.8：
- 偏好：超过 preference_ttl_days 未更新的条目归档
- 经验：超过 experience_ttl_days 未引用的经验归档
- 技能：条目数超限时触发精简
- 注入：总 token 超过 memory_inject_max_tokens 时按优先级截断
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..storage.backend import UserPreference, UserExperience


@dataclass
class LifecycleConfig:
    """记忆生命周期配置。"""
    preference_ttl_days: int = 90
    experience_ttl_days: int = 180
    skill_max_entries: int = 30
    memory_inject_max_tokens: int = 2000


def filter_stale_preferences(
    prefs: list[UserPreference],
    ttl_days: int = 90,
    now: datetime | None = None,
) -> tuple[list[UserPreference], list[UserPreference]]:
    """
    分离活跃和过期偏好。

    Returns:
        (active, stale)
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    cutoff_str = cutoff.isoformat(timespec="seconds")

    active: list[UserPreference] = []
    stale: list[UserPreference] = []

    for p in prefs:
        if p.updated_at and p.updated_at < cutoff_str:
            stale.append(p)
        else:
            active.append(p)

    return active, stale


def filter_stale_experiences(
    experiences: list[UserExperience],
    ttl_days: int = 180,
    now: datetime | None = None,
) -> tuple[list[UserExperience], list[UserExperience]]:
    """
    分离活跃和过期经验。

    Returns:
        (active, stale)
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    cutoff_str = cutoff.isoformat(timespec="seconds")

    active: list[UserExperience] = []
    stale: list[UserExperience] = []

    for e in experiences:
        if e.created_at and e.created_at < cutoff_str:
            stale.append(e)
        else:
            active.append(e)

    return active, stale


def build_memory_injection(
    preferences: list[UserPreference],
    experiences: list[UserExperience],
    skills: list[dict],
    *,
    max_tokens: int = 2000,
    estimate_tokens: Callable[[str], int] | None = None,
    current_query: str = "",
) -> str:
    """
    构建注入到 system prompt 的记忆文本，按优先级截断。

    优先级：偏好 > 相关经验 > 技能。
    """
    if estimate_tokens is None:
        estimate_tokens = lambda t: len(t) // 3

    parts: list[str] = []
    used_tokens = 0

    # 1. 偏好（最高优先级）
    if preferences:
        pref_lines = []
        for p in preferences:
            line = f"- [{p.category}] {p.l0}"
            if p.l1:
                line += f"：{p.l1}"
            pref_lines.append(line)
        pref_text = "## 用户偏好\n" + "\n".join(pref_lines)
        tokens = estimate_tokens(pref_text)
        if used_tokens + tokens <= max_tokens:
            parts.append(pref_text)
            used_tokens += tokens

    # 2. 经验（按关键词匹配当前查询筛选）
    if experiences and used_tokens < max_tokens:
        query_kws = set(re.findall(r"[\w\u4e00-\u9fff]+", current_query.lower())) if current_query else set()

        scored_exps: list[tuple[int, UserExperience]] = []
        for e in experiences:
            if query_kws:
                e_kws = set(re.findall(r"[\w\u4e00-\u9fff]+", e.task_desc.lower()))
                score = len(query_kws & e_kws)
            else:
                score = 0
            scored_exps.append((score, e))
        scored_exps.sort(key=lambda x: x[0], reverse=True)

        exp_lines: list[str] = []
        for _, e in scored_exps:
            label = "✓" if e.success else "✗"
            line = f"- {label} [{e.task_desc}] {e.content}"
            line_tokens = estimate_tokens(line)
            if used_tokens + line_tokens > max_tokens:
                break
            exp_lines.append(line)
            used_tokens += line_tokens

        if exp_lines:
            parts.append("## 历史经验\n" + "\n".join(exp_lines))

    # 3. 技能（按最近使用排序）
    if skills and used_tokens < max_tokens:
        skill_lines: list[str] = []
        for s in skills:
            desc = s.get("description", "")
            line = f"- **{s.get('name', '')}**: {desc}"
            line_tokens = estimate_tokens(line)
            if used_tokens + line_tokens > max_tokens:
                break
            skill_lines.append(line)
            used_tokens += line_tokens

        if skill_lines:
            parts.append("## 可用技能\n" + "\n".join(skill_lines))

    return "\n\n".join(parts)
