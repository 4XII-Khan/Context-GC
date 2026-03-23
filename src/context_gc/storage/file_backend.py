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
            ├── preferences/
            │   ├── preferences.md          # 注入用正文，不含来源
            │   └── .preference_index.json  # 元数据：来源会话、时间等
            ├── skills/
            │   └── {skill_name}/
            │           ├── .meta.json   # created_at / updated_at（程序写入）
            │           └── SKILL.md
            └── experience/
                ├── .task_index.json     # 每条任务含 created_at / updated_at
                └── {task_slug}/
                    └── .overview.md
"""

from __future__ import annotations

import hashlib
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


def _utc_now_iso() -> str:
    """UTC ISO-8601 秒精度，与 UserPreference.updated_at 一致。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_slug(text: str, max_len: int = 60) -> str:
    """将任意文本转为文件系统安全的 slug。"""
    slug = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", text.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if len(slug) > max_len:
        import hashlib
        h = hashlib.sha256(text.encode()).hexdigest()[:12]
        slug = slug[: max_len - 13] + "_" + h
    return slug or "unnamed"


def _normalize_l0(text: str) -> str:
    """标准化 l0 用于精确去重。"""
    return text.strip()


def _keyword_overlap(text_a: str, text_b: str, threshold: float = 0.8) -> bool:
    """关键词重叠率：Jaccard 相似度 > threshold 视为重复。"""
    kws_a = set(re.findall(r"[\w\u4e00-\u9fff]+", text_a.lower()))
    kws_b = set(re.findall(r"[\w\u4e00-\u9fff]+", text_b.lower()))
    if not kws_a or not kws_b:
        return False
    overlap = len(kws_a & kws_b) / max(len(kws_a | kws_b), 1)
    return overlap >= threshold


def _pref_matches(
    l0_a: str, l0_b: str, strategy: str, threshold: float
) -> bool:
    """判断两条偏好 l0 是否匹配（重复）。"""
    if strategy == "exact":
        return _normalize_l0(l0_a) == _normalize_l0(l0_b)
    if strategy == "keyword_overlap":
        return _keyword_overlap(l0_a, l0_b, threshold)
    return False


def _preference_stable_id(category: str, l0: str) -> str:
    """偏好条目稳定 id（用于索引与去重定位）。"""
    key = f"{category}\x1f{_normalize_l0(l0)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _parse_preferences_line_legacy(line: str) -> tuple[str, str, str | None, str, str] | None:
    """
    解析旧版 ``preferences.md`` 行（含 session 后缀）。
    返回 (category, l0, l1, source_session, updated_at) 或 None。
    """
    line = line.strip()
    if not line.startswith("- ["):
        return None
    m = re.match(
        r"- \[(\w+)\]\s*(.+?)(?:\s*\(session:(\S+),\s*(\S+)\))?$",
        line,
    )
    if not m:
        return None
    cat, content, src_session, updated = m.group(1), m.group(2), m.group(3), m.group(4)
    parts = content.split("：", 1)
    l0 = parts[0].strip()
    l1 = parts[1].strip() if len(parts) > 1 else None
    return cat, l0, l1, src_session or "", updated or ""


def _parse_preferences_line_clean(line: str) -> tuple[str, str, str | None] | None:
    """解析新版正文行（无来源后缀）。"""
    line = line.strip()
    if not line.startswith("- ["):
        return None
    m = re.match(r"- \[(\w+)\]\s*(.+)$", line)
    if not m:
        return None
    cat, content = m.group(1), m.group(2).strip()
    parts = content.split("：", 1)
    l0 = parts[0].strip()
    l1 = parts[1].strip() if len(parts) > 1 else None
    return cat, l0, l1


def _preference_entry_from_parts(
    *,
    category: str,
    l0: str,
    l1: str | None,
    source_session: str,
    updated_at: str,
    created_at: str | None = None,
    pref_id: str | None = None,
) -> dict:
    now = updated_at or _utc_now_iso()
    pid = pref_id or _preference_stable_id(category, l0)
    return {
        "id": pid,
        "category": category,
        "l0": l0,
        "l1": l1 or "",
        "source_session": source_session or "",
        "updated_at": now,
        "created_at": created_at or now,
    }


def _render_preferences_markdown(entries: list[dict]) -> str:
    lines = ["# 用户偏好", ""]
    for e in entries:
        row = f"- [{e['category']}] {e['l0']}"
        if (e.get("l1") or "").strip():
            row += f"：{e['l1'].strip()}"
        lines.append(row)
    lines.append("")
    return "\n".join(lines)


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
    # 偏好（preferences/ 目录 + .preference_index.json 元数据；正文不含来源）
    # ------------------------------------------------------------------

    def _preferences_dir(self, user_id: str) -> Path:
        return self.data_dir / "user" / user_id / "preferences"

    def _migrate_legacy_user_preferences_file(self, user_id: str) -> None:
        """
        将旧版 ``user/{id}/preferences.md`` 迁入 ``preferences/`` 并生成索引后，
        将旧文件重命名为 ``preferences.md.legacy.bak``。
        """
        ur = self.data_dir / "user" / user_id
        legacy = ur / "preferences.md"
        if not legacy.exists():
            return
        pdir = self._preferences_dir(user_id)
        idx_path = pdir / ".preference_index.json"
        if idx_path.exists():
            return
        pdir.mkdir(parents=True, exist_ok=True)
        text = legacy.read_text(encoding="utf-8")
        entries: list[dict] = []
        for line in text.splitlines():
            parsed = _parse_preferences_line_legacy(line)
            if not parsed:
                continue
            cat, l0, l1, src, upd = parsed
            ts = upd or _utc_now_iso()
            entries.append(
                _preference_entry_from_parts(
                    category=cat,
                    l0=l0,
                    l1=l1,
                    source_session=src or "",
                    updated_at=ts,
                    created_at=ts,
                )
            )
        if entries:
            self._atomic_write_preference_index(idx_path, entries)
            (pdir / "preferences.md").write_text(
                _render_preferences_markdown(entries), encoding="utf-8"
            )
            try:
                legacy.rename(ur / "preferences.md.legacy.bak")
            except OSError:
                legacy.unlink(missing_ok=True)
            return
        # 无有效条目：若旧文件几乎为空则删除，避免反复尝试迁移
        body = "\n".join(
            ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        if not body.strip():
            try:
                legacy.unlink(missing_ok=True)
            except OSError:
                pass

    def _atomic_write_preference_index(self, idx_path: Path, entries: list[dict]) -> None:
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = idx_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(tmp), str(idx_path))

    def _rebuild_index_from_clean_markdown(self, user_id: str) -> list[dict]:
        """仅有 preferences.md、无索引时，从正文反建索引（来源字段为空）。"""
        pdir = self._preferences_dir(user_id)
        md_path = pdir / "preferences.md"
        if not md_path.exists():
            return []
        entries: list[dict] = []
        for line in md_path.read_text(encoding="utf-8").splitlines():
            pc = _parse_preferences_line_clean(line)
            if not pc:
                continue
            cat, l0, l1 = pc
            now = _utc_now_iso()
            entries.append(
                _preference_entry_from_parts(
                    category=cat,
                    l0=l0,
                    l1=l1,
                    source_session="",
                    updated_at=now,
                    created_at=now,
                )
            )
        return entries

    def _read_preference_entries(self, user_id: str) -> list[dict]:
        self._migrate_legacy_user_preferences_file(user_id)
        pdir = self._preferences_dir(user_id)
        idx_path = pdir / ".preference_index.json"
        if idx_path.exists():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = []
            if isinstance(data, list):
                out = [e for e in data if isinstance(e, dict) and str(e.get("l0", "")).strip()]
                return out
        entries = self._rebuild_index_from_clean_markdown(user_id)
        if entries:
            self._atomic_write_preference_index(idx_path, entries)
        return entries

    def _write_preference_bundle(self, user_id: str, entries: list[dict]) -> None:
        ur = self.data_dir / "user" / user_id
        ur.mkdir(parents=True, exist_ok=True)
        pdir = self._preferences_dir(user_id)
        pdir.mkdir(parents=True, exist_ok=True)
        idx_path = pdir / ".preference_index.json"
        self._atomic_write_preference_index(idx_path, entries)
        (pdir / "preferences.md").write_text(
            _render_preferences_markdown(entries), encoding="utf-8"
        )

    def _entries_to_user_preferences(
        self, user_id: str, entries: list[dict], category: str | None
    ) -> list[UserPreference]:
        results: list[UserPreference] = []
        for e in entries:
            cat = str(e.get("category", ""))
            if category and cat != category:
                continue
            l1 = (e.get("l1") or "").strip() or None
            results.append(
                UserPreference(
                    user_id=user_id,
                    category=cat,
                    l0=str(e.get("l0", "")).strip(),
                    l1=l1,
                    source_session=str(e.get("source_session", "") or "") or None,
                    updated_at=str(e.get("updated_at", "") or ""),
                )
            )
        return results

    async def save_user_preferences(
        self,
        user_id: str,
        prefs: list[UserPreference],
        session_id: str,
        *,
        dedup_strategy: str = "keyword_overlap",
        dedup_threshold: float = 0.8,
    ) -> None:
        """
        保存用户偏好，写入前去重。

        落盘：``user/{id}/preferences/.preference_index.json``（含来源会话、时间）+
        ``user/{id}/preferences/preferences.md``（仅 category/l0/l1，**不含来源**）。

        去重策略：
        - exact: l0 完全一致则视为重复
        - keyword_overlap: 关键词重叠率 > threshold 视为重复（中英文分词）
        """
        if not prefs:
            return

        entries = list(self._read_preference_entries(user_id))
        modified = False

        for pref in prefs:
            matched_j: int | None = None
            for j, e in enumerate(entries):
                if _pref_matches(
                    pref.l0, str(e.get("l0", "")),
                    dedup_strategy, dedup_threshold,
                ):
                    matched_j = j
                    break
            if matched_j is not None:
                now = pref.updated_at or _utc_now_iso()
                entries[matched_j]["updated_at"] = now
                entries[matched_j]["source_session"] = (
                    session_id or entries[matched_j].get("source_session") or ""
                )
                modified = True
                continue
            now = pref.updated_at or _utc_now_iso()
            entries.append(
                _preference_entry_from_parts(
                    category=pref.category,
                    l0=pref.l0,
                    l1=pref.l1,
                    source_session=session_id or "",
                    updated_at=now,
                    created_at=now,
                )
            )
            modified = True

        if not modified:
            return
        self._write_preference_bundle(user_id, entries)

    async def load_user_preferences(
        self, user_id: str, category: str | None = None
    ) -> list[UserPreference]:
        entries = self._read_preference_entries(user_id)
        return self._entries_to_user_preferences(user_id, entries, category)

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
        meta_path = d / ".meta.json"
        now = _utc_now_iso()
        if meta_path.exists():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                old = {}
            if not isinstance(old, dict):
                old = {}
            created = (old.get("created_at") or "").strip() or now
            meta_obj = {"created_at": created, "updated_at": now}
        else:
            meta_obj = {"created_at": now, "updated_at": now}
        tmp_meta = meta_path.with_suffix(".tmp")
        tmp_meta.write_text(
            json.dumps(meta_obj, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(tmp_meta), str(meta_path))
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
            item: dict = {
                "name": name_from_fm,
                "description": desc,
                "path": str(skill_file),
                "content": content,
            }
            meta_path = sd / ".meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(meta, dict):
                        if meta.get("created_at"):
                            item["created_at"] = meta["created_at"]
                        if meta.get("updated_at"):
                            item["updated_at"] = meta["updated_at"]
                except (json.JSONDecodeError, OSError):
                    pass
            results.append(item)
        return results

    # ------------------------------------------------------------------
    # 用户经验
    # ------------------------------------------------------------------

    async def save_user_experience(
        self,
        user_id: str,
        experiences: list[UserExperience],
        session_id: str,
        *,
        use_fuzzy_task_match: bool = True,
    ) -> None:
        exp_dir = self.data_dir / "user" / user_id / "experience"
        exp_dir.mkdir(parents=True, exist_ok=True)

        index_path = exp_dir / ".task_index.json"
        index = self._load_task_index(index_path)

        for exp in experiences:
            slug = self._get_or_create_task_slug(
                index, exp.task_desc, fuzzy=use_fuzzy_task_match
            )
            self._touch_task_index_entry(index, slug)
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
        self,
        user_id: str,
        task_desc: str | None = None,
        *,
        use_fuzzy_task_match: bool = True,
    ) -> list[UserExperience]:
        exp_dir = self.data_dir / "user" / user_id / "experience"
        if not exp_dir.exists():
            return []

        index_path = exp_dir / ".task_index.json"
        index = self._load_task_index(index_path)

        dirs_to_scan: list[tuple[str, Path]] = []
        if task_desc:
            slug = self._find_task_slug(
                index, task_desc, fuzzy=use_fuzzy_task_match
            )
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

    async def load_user_experience_task_index(self, user_id: str) -> list[dict]:
        exp_dir = self.data_dir / "user" / user_id / "experience"
        if not exp_dir.exists():
            return []
        index_path = exp_dir / ".task_index.json"
        return self._load_task_index(index_path)

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

    @staticmethod
    def _touch_task_index_entry(index: list[dict], slug: str) -> None:
        """本条任务在经验写入时被触碰：刷新 updated_at，并补全旧数据的 created_at。"""
        now = _utc_now_iso()
        for entry in index:
            if entry.get("slug") != slug:
                continue
            if "created_at" not in entry or not (entry.get("created_at") or "").strip():
                fallback = (entry.get("updated_at") or "").strip() or now
                entry["created_at"] = fallback
            entry["updated_at"] = now
            return

    def _get_or_create_task_slug(
        self, index: list[dict], task_desc: str, *, fuzzy: bool = True
    ) -> str:
        existing = self._find_task_slug(index, task_desc, fuzzy=fuzzy)
        if existing:
            for entry in index:
                if entry["slug"] == existing:
                    if task_desc not in entry.get("alt_descs", []):
                        entry.setdefault("alt_descs", []).append(task_desc)
                    break
            return existing
        slug = _safe_slug(task_desc)
        now = _utc_now_iso()
        index.append({
            "slug": slug,
            "canonical_desc": task_desc,
            "alt_descs": [],
            "created_at": now,
            "updated_at": now,
        })
        return slug

    @staticmethod
    def _find_task_slug(
        index: list[dict], task_desc: str, *, fuzzy: bool = True
    ) -> str | None:
        desc_lower = task_desc.lower()
        for entry in index:
            if entry["canonical_desc"].lower() == desc_lower:
                return entry["slug"]
            for alt in entry.get("alt_descs", []):
                if alt.lower() == desc_lower:
                    return entry["slug"]
        if not fuzzy:
            return None
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
