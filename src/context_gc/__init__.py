"""
context_gc — 模型无关的对话上下文管理方案。

核心能力：会话内压缩 → 会话级记忆持久化 → 记忆蒸馏与长期学习 → 记忆注入。
"""

from .state import RoundMeta, ContextGCState
from .core import ContextGC, ContextGCOptions
from .storage import MemoryBackend, UserPreference, UserExperience, SessionRecord, FileBackend, CheckpointManager
from .memory import PreferenceDetector, LifecycleConfig, build_memory_injection

__all__ = [
    # 核心
    "ContextGC",
    "ContextGCOptions",
    "RoundMeta",
    "ContextGCState",
    # 持久化
    "MemoryBackend",
    "FileBackend",
    "UserPreference",
    "UserExperience",
    "SessionRecord",
    # 可靠性
    "CheckpointManager",
    "PreferenceDetector",
    # 生命周期
    "LifecycleConfig",
    "build_memory_injection",
]
