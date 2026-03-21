"""context_gc.memory — 记忆管理。"""

from .preference import PreferenceDetector
from .lifecycle import (
    LifecycleConfig,
    build_memory_injection,
    filter_stale_preferences,
    filter_stale_experiences,
)

__all__ = [
    "PreferenceDetector",
    "LifecycleConfig",
    "build_memory_injection",
    "filter_stale_preferences",
    "filter_stale_experiences",
]
