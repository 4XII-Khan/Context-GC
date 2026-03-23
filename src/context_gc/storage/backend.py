"""
context_gc/storage/backend.py

持久化后端协议与公共数据类。

MemoryBackend 为 Protocol，宿主注入具体实现后，Context GC 委托会话 / 偏好 / 技能 / 经验存储。
不注入时 Context GC 仍可单独做会话内压缩。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, Optional, runtime_checkable


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class UserPreference:
    """用户偏好条目。"""
    user_id: str
    category: str          # writing_style / coding_habits / corrections / explicit_prefs
    l0: str                # 超短摘要
    l1: str | None = None  # 完整描述
    source_session: str | None = None
    updated_at: str = ""   # ISO-8601

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class UserExperience:
    """用户经验条目（成功经验或失败反模式）。"""
    task_desc: str
    success: bool
    content: str
    source_session: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionRecord:
    """会话记录（L0/L1/L2 均为会话级）。"""
    session_id: str
    l0: str
    l1: list[str]
    l2_uri: str
    created_at: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 持久化协议
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryBackend(Protocol):
    """
    持久化后端协议。

    Context GC 通过此协议委托所有存储操作，宿主注入具体实现。
    接口划分为四组：会话、偏好、技能、经验。
    """

    # ---- 会话 ----
    async def save_session(
        self,
        session_id: str,
        l0: str,
        l1: list[str],
        l2_uri: str,
        meta: dict | None = None,
    ) -> None: ...

    async def search_sessions(
        self, query: str, limit: int = 10
    ) -> list[dict]: ...

    async def load_session_l1(self, session_id: str) -> Optional[list[str]]: ...

    async def load_session_l2(self, session_id: str) -> Optional[str]: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def list_expired_sessions(
        self, before: str, limit: int = 100
    ) -> list[str]: ...

    # ---- 偏好 ----
    async def save_user_preferences(
        self,
        user_id: str,
        prefs: list[UserPreference],
        session_id: str,
    ) -> None: ...

    async def load_user_preferences(
        self, user_id: str, category: str | None = None
    ) -> list[UserPreference]: ...

    # ---- 公共技能 ----
    async def load_skills(
        self, skill_name: str | None = None
    ) -> list[dict]: ...

    # ---- 私有化技能 ----
    async def load_user_skills(
        self, user_id: str, skill_name: str | None = None
    ) -> list[dict]: ...

    async def save_user_skill(
        self, user_id: str, skill_name: str, content: str
    ) -> None: ...

    # ---- 用户经验 ----
    async def save_user_experience(
        self,
        user_id: str,
        experiences: list[UserExperience],
        session_id: str,
        *,
        use_fuzzy_task_match: bool = True,
    ) -> None: ...

    async def load_user_experience(
        self,
        user_id: str,
        task_desc: str | None = None,
        *,
        use_fuzzy_task_match: bool = True,
    ) -> list[UserExperience]: ...

    async def load_user_experience_task_index(self, user_id: str) -> list[dict]:
        """
        返回该用户经验目录下的任务索引（与 ``.task_index.json`` 结构一致）。
        无索引实现可返回空列表。
        """
        ...
