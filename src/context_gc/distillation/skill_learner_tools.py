"""
distillation/skill_learner_tools.py

Skill Learner Agent 的 tool schema 和 handler（复用 AsMe 设计）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_skill_session_meta(skill_dir: Path, session_id: str, *, is_new_skill: bool) -> None:
    """
    技能目录 .meta.json：会话溯源 + 程序维护的 created_at / updated_at（与 FileBackend.save_user_skill 字段对齐）。
    - 新建：session_id、source、created_at=updated_at=现在
    - 更新：保留首次 session_id 与 created_at（若无则补现在），刷新 last_session_id、updated_at
    """
    sid = (session_id or "").strip()
    if not sid:
        return
    meta_path = skill_dir / ".meta.json"
    now = _utc_now_iso()
    if is_new_skill:
        meta = {
            "source": "session_learning",
            "session_id": sid,
            "created_at": now,
            "updated_at": now,
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    meta = {}
    if meta_path.exists():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta = raw
        except (json.JSONDecodeError, OSError):
            meta = {}
    if not meta.get("session_id"):
        meta["session_id"] = sid
    meta["last_session_id"] = sid
    meta.setdefault("source", "session_learning")
    if not (meta.get("created_at") or "").strip():
        meta["created_at"] = (meta.get("updated_at") or "").strip() or now
    meta["updated_at"] = now
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def backup_skill_file(skill_dir: Path, relative_file: str, *, session_id: str = "") -> Path:
    """
    在 str_replace 更新前，备份到 {skill_dir}/.backups/{时间戳}/：
    - 本次要改动的文件（保持相对路径）
    - 若技能目录下存在 `.meta.json` 且本次改动的不是该文件，则一并复制（因更新后 merge 会改写 meta）

    不复制同技能内其它文件，也不触碰其它技能目录。
    """
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")

    rel = Path(relative_file)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Invalid file_path: {relative_file!r}")

    src = skill_dir / rel
    try:
        src.resolve().relative_to(skill_dir.resolve())
    except ValueError as e:
        raise ValueError(f"file_path escapes skill directory: {relative_file!r}") from e

    if not src.is_file():
        raise FileNotFoundError(f"File not found for backup: {relative_file}")

    backup_root = skill_dir / ".backups"
    backup_root.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%dT%H%M%S_%f")
    dest = backup_root / ts
    n = 0
    while dest.exists():
        n += 1
        dest = backup_root / f"{ts}_{n}"

    dest.mkdir(parents=False)

    out = dest / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)

    norm_target = relative_file.replace("\\", "/")
    backed_up_files: list[str] = [norm_target]

    skill_meta = skill_dir / ".meta.json"
    if skill_meta.is_file() and rel != Path(".meta.json"):
        shutil.copy2(skill_meta, dest / ".meta.json")
        backed_up_files.append(".meta.json")

    meta = {
        "backed_up_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": (session_id or "").strip(),
        "skill_dir": skill_dir.name,
        "backed_up_files": backed_up_files,
    }
    (dest / ".backup_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dest


def _yaml_scalar_skill_name(n: str) -> str:
    """name: 行输出：纯 kebab-case 可不加引号；含中文或特殊字符时用 JSON 双引号转义。"""
    n = (n or "").strip() or "未命名技能"
    if re.fullmatch(r"[a-zA-Z0-9_-]+", n):
        return n
    return json.dumps(n, ensure_ascii=False)


def sanitize_skill_frontmatter(content: str) -> str:
    """
    规范化 SKILL.md 前置元数据：name / description 各占一行，贴近 Claude Code / Agent Skills 习惯。
    若 LLM 把 name 与 description 挤在同一行，拆成合法 YAML。
    """
    if "---" not in content:
        return content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content
    fm_raw = parts[1].strip()
    body = parts[2]

    # 同一行出现 name: ... description: ... 时强制换行
    fm_raw = re.sub(
        r'(name:\s*(?:"[^"]*"|\'[^\']*\'|[^\s\n]+))\s+(description:)',
        r"\1\n\2",
        fm_raw,
        count=1,
    )

    name_val = ""
    desc_val = ""
    desc_multiline = False
    current = None
    buf: list[str] = []

    for line in fm_raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            if current == "desc" and buf:
                desc_val = "\n".join(buf).strip()
                buf = []
            current = "name"
            name_val = stripped.split(":", 1)[1].strip().strip('"\'')
            continue
        if stripped.startswith("description:"):
            if current == "desc" and buf:
                desc_val = "\n".join(buf).strip()
                buf = []
            current = "desc"
            rest = stripped.split(":", 1)[1].strip()
            if rest == "|" or rest == ">":
                desc_multiline = True
                continue
            if rest:
                buf.append(rest.strip().strip('"\''))
            continue
        if current == "desc" and line.startswith((" ", "\t")):
            buf.append(stripped)
        elif current == "desc" and stripped and desc_multiline:
            buf.append(stripped)

    if current == "desc" and buf:
        desc_val = "\n".join(buf).strip()

    if not name_val and not desc_val:
        return "---\n" + fm_raw + "\n---" + body

    desc_lines = desc_val.split("\n") if desc_val else [""]
    desc_block = "\n".join(f"  {ln}" if ln else "  " for ln in desc_lines)

    name_out = _yaml_scalar_skill_name(name_val or "unnamed-skill")
    fm_out = f"name: {name_out}\ndescription: |\n{desc_block}\n"
    return "---\n" + fm_out + "---" + body


def extract_skill_name_from_skill_md(content: str) -> str | None:
    """
    从 SKILL.md 首段 front matter 读取 ``name:`` 的展示名（与目录应对齐）。
    支持双引号包裹的中文等。
    """
    if "---" not in content:
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        s = line.strip()
        if not s.startswith("name:"):
            continue
        raw = s.split(":", 1)[1].strip()
        if not raw:
            return None
        if raw.startswith('"') and raw.endswith('"'):
            try:
                return str(json.loads(raw)).strip() or None
            except json.JSONDecodeError:
                return raw.strip('"').strip() or None
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1].strip() or None
        return raw.strip().strip('"\'') or None
    return None


SKILL_LEARNER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "report_thinking",
            "description": "Report your thinking before taking actions",
            "parameters": {
                "type": "object",
                "properties": {"thinking": {"type": "string", "description": "简体中文"}},
                "required": ["thinking"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": "列出技能目录内文件；skill_name 须为「可用技能」列表反引号内的目录名（与 YAML 中文名一致）",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "磁盘上的技能目录名（多为中文）"},
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill_file",
            "description": "读取技能包内文件；skill_name 为磁盘目录名（与列表一致）",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能目录名"},
                    "file_path": {"type": "string", "description": "e.g. SKILL.md"},
                },
                "required": ["skill_name", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": (
                "新建技能目录与 SKILL.md。"
                "磁盘目录名**以 YAML 前置 name: 为准**（须为简体中文，与一级 # 标题一致）；"
                "系统据此创建文件夹，使目录名与技能中文名一致。"
                "skill_name 可选；若填写必须与 YAML name 完全一致，否则可省略。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "可选。须与 YAML name: 完全一致；省略则由 name: 决定目录名",
                    },
                    "skill_md_content": {
                        "type": "string",
                        "description": "完整 SKILL.md；YAML name 与 # 标题为简体中文且一致",
                    },
                },
                "required": ["skill_md_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_skill_file",
            "description": (
                "在技能文件内做查找替换（非仅末尾追加）。优先用 old_str 圈定已有小节全文，"
                "new_str 写成合并/修订后的完整块，避免同主题下再插平行重复条；"
                "仅当无法并入现有结构时才在合适位置插入新小节。"
                "new_str 中新增/修订的自然语言须为简体中文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "file_path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string", "description": "替换后内容；新增叙述为简体中文"},
                },
                "required": ["skill_name", "file_path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_skill_decision",
            "description": "Report your final decision. Required before finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "skip | update | create"},
                    "skill_name": {"type": "string"},
                    "reason": {"type": "string", "description": "简体中文说明"},
                },
                "required": ["action", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish skill learning",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class SkillLearnerToolContext:
    """Skill Learner 工具执行上下文。"""

    def __init__(self, skills_dir: str | Path, session_id: str = ""):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.touched_skills: list[str] = []
        self.skill_decisions: list[dict] = []
        self.session_id = session_id

    def _skill_path(self, name: str) -> Path:
        """目录名允许中文、英文、数字、下划线、连字符；其它字符替换为 -。"""
        s = (name or "").strip()
        safe = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff-]", "-", s)
        safe = re.sub(r"-+", "-", safe).strip("-") or "skill"
        if len(safe) > 80:
            h = hashlib.sha256(safe.encode("utf-8")).hexdigest()[:8]
            safe = safe[:72].rstrip("-") + "_" + h
        return self.skills_dir / safe

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        try:
            return handler(args)
        except Exception as e:
            return f"Error: {e}"

    def _handle_report_thinking(self, args: dict) -> str:
        return "OK"

    def _handle_get_skill(self, args: dict) -> str:
        name = args.get("skill_name", "")
        path = self._skill_path(name)
        if not path.exists():
            return f"Skill '{name}' not found."
        files: list[str] = []
        for f in sorted(path.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(path)
            if rel.parts and rel.parts[0] == ".backups":
                continue
            files.append(str(rel))
        note = ""
        if (path / ".backups").is_dir():
            n_snap = sum(1 for d in (path / ".backups").iterdir() if d.is_dir())
            if n_snap:
                note = f"\n(历史备份 {n_snap} 份在 .backups/<时间戳>/，列表已省略)"
        body = "\n".join(files) if files else "(empty)"
        return body + note

    def _handle_get_skill_file(self, args: dict) -> str:
        name = args.get("skill_name", "")
        fp = args.get("file_path", "SKILL.md")
        target = self._skill_path(name) / fp
        if not target.exists():
            return f"File not found: {fp}"
        return target.read_text(encoding="utf-8")

    def _handle_create_skill(self, args: dict) -> str:
        raw_md = args.get("skill_md_content", "")
        content = sanitize_skill_frontmatter(raw_md)
        yaml_name = extract_skill_name_from_skill_md(content)
        name_arg = (args.get("skill_name") or "").strip()
        dir_key = (yaml_name or name_arg).strip()
        if not dir_key:
            return (
                "Error: 无法确定目录名：请在 SKILL.md 的 YAML 中设置 name:（简体中文），"
                "或提供与之一致的 skill_name。"
            )
        if name_arg and yaml_name and name_arg != yaml_name:
            return (
                f"Error: skill_name「{name_arg}」与 YAML name「{yaml_name}」不一致。"
                "请统一为同一中文名，或省略 skill_name。"
            )
        path = self._skill_path(dir_key)
        if path.exists():
            return (
                f"Skill directory '{path.name}' already exists. "
                "Use str_replace_skill_file to update."
            )
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(content, encoding="utf-8")
        merge_skill_session_meta(path, self.session_id, is_new_skill=True)
        disk_name = path.name
        if disk_name not in self.touched_skills:
            self.touched_skills.append(disk_name)
        note = ""
        if yaml_name and disk_name != yaml_name.strip().strip("-"):
            note = f"（目录名已按文件系统规则规范为「{disk_name}」）"
        return f"Created skill directory `{disk_name}`{note}"

    def _handle_str_replace_skill_file(self, args: dict) -> str:
        name = args.get("skill_name", "")
        fp = args.get("file_path", "SKILL.md")
        old = args.get("old_str", "")
        new = args.get("new_str", "")
        skill_dir = self._skill_path(name)
        target = skill_dir / fp
        if not target.exists():
            return f"File not found: {fp}"
        try:
            backup_dest = backup_skill_file(skill_dir, fp, session_id=self.session_id)
            backup_rel = backup_dest.relative_to(skill_dir)
        except OSError as e:
            return f"Error: backup failed before update: {e}"
        text = target.read_text(encoding="utf-8")
        if old not in text:
            stripped = old.rstrip()
            if stripped in text:
                text = text.replace(stripped, new, 1)
            else:
                return f"old_str not found in {fp}."
        else:
            text = text.replace(old, new, 1)
        if fp.endswith("SKILL.md") or fp == "SKILL.md":
            text = sanitize_skill_frontmatter(text)
        target.write_text(text, encoding="utf-8")
        merge_skill_session_meta(skill_dir, self.session_id, is_new_skill=False)
        if name not in self.touched_skills:
            self.touched_skills.append(name)
        return f"Backed up to {backup_rel}/ then replaced text in {fp}"

    def _handle_report_skill_decision(self, args: dict) -> str:
        self.skill_decisions.append({
            "action": args.get("action", "skip"),
            "skill_name": args.get("skill_name", ""),
            "reason": args.get("reason", ""),
            "session_id": self.session_id,
        })
        return "OK"

    def _handle_finish(self, args: dict) -> str:
        return "FINISH"
