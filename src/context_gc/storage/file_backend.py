"""
context_gc/storage/file_backend.py

基于文件系统的 MemoryBackend 实现。

目录布局（与设计文档 3.3/3.4 一致）::

    {data_dir}/
    ├── sessions/
    │   └── {session_id}/
    │       ├── .abstract.md      # L0
    │       ├── .overview.md      # L1（JSON list of summaries）
    │       ├── content.md        # L2（原始对话）
    │       └── .meta.json        # 元数据（created_at 等）
    ├── skills/                   # 公共技能
    │   └── {skill_name}/SKILL.md
    └── user/
        └── {user_id}/
            ├── preferences.md
            ├── skills/
            │   └── {skill_name}/SKILL.md
            └── experience/
                ├── .task_index.json
                └── {task_slug}/
                    └── .overview.md
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .backend import (
    MemoryBackend,
    UserPreference,
    UserExperience,
    SessionRecord,
)


def _safe_slug(text: str, max_len: int = 60) -> str:
    """将任意文本转为文件系统安全的 slug。"""
    slug = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", text.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if len(slug) > max_len:
        import hashlib
        h = hashlib.sha256(text.encode()).hexdigest()[:12]
        slug = slug[: max_len - 13] + "_" + h
    return slug or "unnamed"


class FileBackend:
    """
    基于本地文件系统的 MemoryBackend 实现。

    所有 I/O 均为同步文件操作（通过 async wrapper），适用于单机部署。
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 会话存储
    # ------------------------------------------------------------------

    async def save_session(
        self,
        session_id: str,
        l0: str,
        l1: list[str],
        l2_uri: str,
        meta: dict | None = None,
    ) -> None:
        d = self.data_dir / "sessions" / session_id
        d.mkdir(parents=True, exist_ok=True)
        (d / ".abstract.md").write_text(l0, encoding="utf-8")
        (d / ".overview.md").write_text(
            json.dumps(l1, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        meta_obj = {
            "session_id": session_id,
            "created_at": (meta or {}).get(
                "created_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
            "l2_uri": l2_uri,
            **(meta or {}),
        }
        (d / ".meta.json").write_text(
            json.dumps(meta_obj, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def search_sessions(
        self, query: str, limit: int = 10
    ) -> list[dict]:
        sessions_dir = self.data_dir / "sessions"
        if not sessions_dir.exists():
            return []

        keywords = set(query.lower().split())
        results: list[tuple[int, dict]] = []

        for sd in sessions_dir.iterdir():
            if not sd.is_dir():
                continue
            score = 0
            l0 = ""
            l1_raw = ""
            l0_path = sd / ".abstract.md"
            l1_path = sd / ".overview.md"
            if l0_path.exists():
                l0 = l0_path.read_text(encoding="utf-8")
                score += sum(1 for kw in keywords if kw in l0.lower())
            if l1_path.exists():
                l1_raw = l1_path.read_text(encoding="utf-8")
                score += sum(1 for kw in keywords if kw in l1_raw.lower())
            if score > 0:
                meta = {}
                meta_path = sd / ".meta.json"
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                results.append((score, {
                    "session_id": sd.name,
                    "l0": l0,
                    "score": score,
                    **meta,
                }))

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

    async def load_session_l1(self, session_id: str) -> Optional[list[str]]:
        p = self.data_dir / "sessions" / session_id / ".overview.md"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    async def load_session_l2(self, session_id: str) -> Optional[str]:
        p = self.data_dir / "sessions" / session_id / "content.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    async def delete_session(self, session_id: str) -> None:
        import shutil
        d = self.data_dir / "sessions" / session_id
        if d.exists():
            shutil.rmtree(d)

    async def list_expired_sessions(
        self, before: str, limit: int = 100
    ) -> list[str]:
        sessions_dir = self.data_dir / "sessions"
        if not sessions_dir.exists():
            return []
        expired: list[str] = []
        for sd in sessions_dir.iterdir():
            if not sd.is_dir():
                continue
            meta_path = sd / ".meta.json"
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created = meta.get("created_at", "")
            if created and created < before:
                expired.append(sd.name)
                if len(expired) >= limit:
                    break
        return expired

    # ------------------------------------------------------------------
    # 偏好
    # ------------------------------------------------------------------

    async def save_user_preferences(
        self,
        user_id: str,
        prefs: list[UserPreference],
        session_id: str,
    ) -> None:
        d = self.data_dir / "user" / user_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / "preferences.md"

        existing = ""
        if p.exists():
            existing = p.read_text(encoding="utf-8")

        lines: list[str] = []
        for pref in prefs:
            entry = f"- [{pref.category}] {pref.l0}"
            if pref.l1:
                entry += f"：{pref.l1}"
            entry += f" (session:{session_id}, {pref.updated_at})"
            lines.append(entry)

        new_block = "\n".join(lines)
        if existing:
            combined = existing.rstrip("\n") + "\n" + new_block + "\n"
        else:
            combined = "# 用户偏好\n\n" + new_block + "\n"
        p.write_text(combined, encoding="utf-8")

    async def load_user_preferences(
        self, user_id: str, category: str | None = None
    ) -> list[UserPreference]:
        p = self.data_dir / "user" / user_id / "preferences.md"
        if not p.exists():
            return []
        text = p.read_text(encoding="utf-8")
        results: list[UserPreference] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("- ["):
                continue
            m = re.match(r"- \[(\w+)\]\s*(.+?)(?:\s*\(session:(\S+),\s*(\S+)\))?$", line)
            if not m:
                continue
            cat, content, src_session, updated = m.group(1), m.group(2), m.group(3), m.group(4)
            if category and cat != category:
                continue
            parts = content.split("：", 1)
            l0 = parts[0].strip()
            l1 = parts[1].strip() if len(parts) > 1 else None
            results.append(UserPreference(
                user_id=user_id,
                category=cat,
                l0=l0,
                l1=l1,
                source_session=src_session or "",
                updated_at=updated or "",
            ))
        return results

    # ------------------------------------------------------------------
    # 公共技能
    # ------------------------------------------------------------------

    async def load_skills(
        self, skill_name: str | None = None
    ) -> list[dict]:
        skills_dir = self.data_dir / "skills"
        if not skills_dir.exists():
            return []
        return self._scan_skills_dir(skills_dir, skill_name)

    # ------------------------------------------------------------------
    # 私有化技能
    # ------------------------------------------------------------------

    async def load_user_skills(
        self, user_id: str, skill_name: str | None = None
    ) -> list[dict]:
        skills_dir = self.data_dir / "user" / user_id / "skills"
        if not skills_dir.exists():
            return []
        return self._scan_skills_dir(skills_dir, skill_name)

    async def save_user_skill(
        self, user_id: str, skill_name: str, content: str
    ) -> None:
        d = self.data_dir / "user" / user_id / "skills" / skill_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")

    def _scan_skills_dir(
        self, skills_dir: Path, skill_name: str | None
    ) -> list[dict]:
        results: list[dict] = []
        for sd in skills_dir.iterdir():
            if not sd.is_dir():
                continue
            if skill_name and sd.name != skill_name:
                continue
            skill_file = sd / "SKILL.md"
            if not skill_file.exists():
                continue
            content = skill_file.read_text(encoding="utf-8")
            desc = ""
            name_from_fm = sd.name
            for line in content.splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"')
                if line.startswith("name:"):
                    name_from_fm = line.split(":", 1)[1].strip().strip('"')
            results.append({
                "name": name_from_fm,
                "description": desc,
                "path": str(skill_file),
                "content": content,
            })
        return results

    # ------------------------------------------------------------------
    # 用户经验
    # ------------------------------------------------------------------

    async def save_user_experience(
        self,
        user_id: str,
        experiences: list[UserExperience],
        session_id: str,
    ) -> None:
        exp_dir = self.data_dir / "user" / user_id / "experience"
        exp_dir.mkdir(parents=True, exist_ok=True)

        index_path = exp_dir / ".task_index.json"
        index = self._load_task_index(index_path)

        for exp in experiences:
            slug = self._get_or_create_task_slug(index, exp.task_desc)
            task_dir = exp_dir / slug
            task_dir.mkdir(parents=True, exist_ok=True)
            overview_path = task_dir / ".overview.md"

            existing = ""
            if overview_path.exists():
                existing = overview_path.read_text(encoding="utf-8")

            section = "## 成功经验" if exp.success else "## 失败反模式"
            entry = f"- {exp.content} (session:{session_id}, {exp.created_at})"

            if not existing:
                if exp.success:
                    content = f"## 成功经验\n{entry}\n\n## 失败反模式\n"
                else:
                    content = f"## 成功经验\n\n## 失败反模式\n{entry}\n"
            else:
                if section in existing:
                    idx = existing.index(section)
                    next_section_idx = existing.find("\n## ", idx + len(section))
                    if next_section_idx == -1:
                        content = existing.rstrip("\n") + "\n" + entry + "\n"
                    else:
                        content = (
                            existing[:next_section_idx].rstrip("\n")
                            + "\n" + entry + "\n"
                            + existing[next_section_idx:]
                        )
                else:
                    content = existing.rstrip("\n") + f"\n\n{section}\n{entry}\n"

            overview_path.write_text(content, encoding="utf-8")

        self._save_task_index(index_path, index)

    async def load_user_experience(
        self, user_id: str, task_desc: str | None = None
    ) -> list[UserExperience]:
        exp_dir = self.data_dir / "user" / user_id / "experience"
        if not exp_dir.exists():
            return []

        index_path = exp_dir / ".task_index.json"
        index = self._load_task_index(index_path)

        dirs_to_scan: list[tuple[str, Path]] = []
        if task_desc:
            slug = self._find_task_slug(index, task_desc)
            if slug:
                dirs_to_scan.append((task_desc, exp_dir / slug))
        else:
            for entry in index:
                slug = entry["slug"]
                p = exp_dir / slug
                if p.exists():
                    dirs_to_scan.append((entry["canonical_desc"], p))

        results: list[UserExperience] = []
        for desc, d in dirs_to_scan:
            overview = d / ".overview.md"
            if not overview.exists():
                continue
            text = overview.read_text(encoding="utf-8")
            current_section_is_success: bool | None = None
            for line in text.splitlines():
                stripped = line.strip()
                if stripped == "## 成功经验":
                    current_section_is_success = True
                    continue
                if stripped == "## 失败反模式":
                    current_section_is_success = False
                    continue
                if current_section_is_success is None:
                    continue
                if not stripped.startswith("- "):
                    continue
                content_text = stripped[2:]
                src = ""
                m = re.search(r"\(session:(\S+),\s*(\S+)\)$", content_text)
                if m:
                    src = m.group(1)
                    content_text = content_text[: m.start()].strip()
                results.append(UserExperience(
                    task_desc=desc,
                    success=current_section_is_success,
                    content=content_text,
                    source_session=src,
                ))
        return results

    # --- task index helpers ---

    @staticmethod
    def _load_task_index(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _save_task_index(path: Path, index: list[dict]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))

    def _get_or_create_task_slug(
        self, index: list[dict], task_desc: str
    ) -> str:
        existing = self._find_task_slug(index, task_desc)
        if existing:
            for entry in index:
                if entry["slug"] == existing:
                    if task_desc not in entry.get("alt_descs", []):
                        entry.setdefault("alt_descs", []).append(task_desc)
                    break
            return existing
        slug = _safe_slug(task_desc)
        index.append({
            "slug": slug,
            "canonical_desc": task_desc,
            "alt_descs": [],
        })
        return slug

    @staticmethod
    def _find_task_slug(index: list[dict], task_desc: str) -> str | None:
        desc_lower = task_desc.lower()
        for entry in index:
            if entry["canonical_desc"].lower() == desc_lower:
                return entry["slug"]
            for alt in entry.get("alt_descs", []):
                if alt.lower() == desc_lower:
                    return entry["slug"]
        kws = set(re.findall(r"[\w\u4e00-\u9fff]+", desc_lower))
        if not kws:
            return None
        for entry in index:
            entry_kws = set(re.findall(
                r"[\w\u4e00-\u9fff]+", entry["canonical_desc"].lower()
            ))
            if len(kws & entry_kws) / max(len(kws | entry_kws), 1) > 0.8:
                return entry["slug"]
        return None
