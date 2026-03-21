"""
context_gc/storage/cleanup.py

会话过期清理：按 session_ttl_days 清理过期会话。

清理范围：删除 sessions 表记录 + 会话目录（L0/L1/L2 文件）。
经验/偏好由独立 TTL 管理（lifecycle.py），不连带删除。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .backend import MemoryBackend

_log = logging.getLogger(__name__)


async def cleanup_expired_sessions(
    backend: MemoryBackend,
    ttl_days: int = 90,
    limit: int = 100,
) -> list[str]:
    """
    清理过期会话。

    Args:
        backend: 持久化后端。
        ttl_days: 会话保留天数。
        limit: 单次最多清理数量。

    Returns:
        已清理的 session_id 列表。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    expired = await backend.list_expired_sessions(cutoff, limit=limit)

    cleaned: list[str] = []
    for sid in expired:
        try:
            await backend.delete_session(sid)
            cleaned.append(sid)
            _log.info("[SessionCleanup] 已清理 session=%s", sid)
        except Exception as e:
            _log.warning("[SessionCleanup] 清理 session=%s 失败: %s", sid, e)

    if cleaned:
        _log.info("[SessionCleanup] 共清理 %d 个过期会话", len(cleaned))
    return cleaned
