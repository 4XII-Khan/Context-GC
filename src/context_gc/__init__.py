"""
context_gc — 模型无关的对话上下文管理方案。

核心能力：会话内压缩 → 会话级记忆持久化 → 记忆蒸馏与长期学习 → 记忆注入。
"""

from .state import RoundMeta, ContextGCState
from .core import (
    ContextGC,
    ContextGCOptions,
    LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS,
)
from .storage import MemoryBackend, UserPreference, UserExperience, SessionRecord, FileBackend, CheckpointManager
from .memory import LifecycleConfig, build_memory_injection
from .defaults import default_generate_l0, default_call_llm_with_tools

__all__ = [
    # 核心
    "ContextGC",
    "ContextGCOptions",
    "LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS",
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
    # 生命周期
    "LifecycleConfig",
    "build_memory_injection",
    # 默认 L0 生成（会话结束持久化）
    "default_generate_l0",
    # 蒸馏管道默认同步 tools 调用
    "default_call_llm_with_tools",
]
