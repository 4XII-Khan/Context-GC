"""
distillation/skill_learner_tools.py

Skill Learner Agent 的 tool schema 和 handler（复用 AsMe 设计）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def sanitize_skill_frontmatter(content: str) -> str:
    """清洗 SKILL.md front matter。"""
    if "---" not in content:
        return content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content
    fm = parts[1]
    fm = re.sub(r'(name:\s*["\'][^"\']*["\'])\s*(description:)', r'\1\n\2', fm)
    fm = re.sub(r'(name:\s*[^\s\n]+)\s+(description:)', r'\1\n\2', fm, count=1)

    def _fix_name(m: re.Match) -> str:
        pre, val = m.group(1), m.group(2).strip().strip('"\'')
        fixed = re.sub(r"_[a-fA-F0-9]{8}$", "", val)
        return f'{pre}"{fixed}"'

    def _fix_desc(m: re.Match) -> str:
        pre, val = m.group(1), m.group(2).strip().strip('"\'')
        fixed = re.sub(r"^#+\s*", "", val).strip()
        return f'{pre}"{fixed}"'

    fm = re.sub(r'(name:\s*)(["\']?[^"\'\n]+["\']?)\s*', _fix_name, fm, count=1)
    fm = re.sub(r'(description:\s*)(["\']?[^"\'\n]+["\']?)\s*', _fix_desc, fm, count=1)
    lines = [ln.strip() for ln in fm.strip().split("\n") if ln.strip()]
    fm = "\n".join(lines)
    body = parts[2]
    return "---\n" + fm + "\n---" + body


SKILL_LEARNER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "report_thinking",
            "description": "Report your thinking before taking actions",
            "parameters": {
                "type": "object",
                "properties": {"thinking": {"type": "string"}},
                "required": ["thinking"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": "List files in a skill directory",
            "parameters": {
                "type": "object",
                "properties": {"skill_name": {"type": "string"}},
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill_file",
            "description": "Read a file from a skill directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
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
            "description": "Create a new skill with SKILL.md",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "kebab-case"},
                    "skill_md_content": {"type": "string", "description": "Full SKILL.md content"},
                },
                "required": ["skill_name", "skill_md_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_skill_file",
            "description": "Replace text in a skill file",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "file_path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
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
                    "reason": {"type": "string", "description": "Chinese explanation"},
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
        safe = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff-]", "-", name)[:64].strip("-") or "skill"
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
        files = [str(f.relative_to(path)) for f in sorted(path.rglob("*")) if f.is_file()]
        return "\n".join(files) if files else "(empty)"

    def _handle_get_skill_file(self, args: dict) -> str:
        name = args.get("skill_name", "")
        fp = args.get("file_path", "SKILL.md")
        target = self._skill_path(name) / fp
        if not target.exists():
            return f"File not found: {fp}"
        return target.read_text(encoding="utf-8")

    def _handle_create_skill(self, args: dict) -> str:
        name = args.get("skill_name", "")
        content = sanitize_skill_frontmatter(args.get("skill_md_content", ""))
        path = self._skill_path(name)
        if path.exists():
            return f"Skill '{name}' already exists. Use str_replace_skill_file to update."
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(content, encoding="utf-8")
        meta = {"source": "session_learning"}
        if self.session_id:
            meta["session_id"] = self.session_id
        (path / ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if name not in self.touched_skills:
            self.touched_skills.append(name)
        return f"Created skill '{name}'"

    def _handle_str_replace_skill_file(self, args: dict) -> str:
        name = args.get("skill_name", "")
        fp = args.get("file_path", "SKILL.md")
        old = args.get("old_str", "")
        new = args.get("new_str", "")
        target = self._skill_path(name) / fp
        if not target.exists():
            return f"File not found: {fp}"
        text = target.read_text(encoding="utf-8")
        if old not in text:
            stripped = old.rstrip()
            if stripped in text:
                text = text.replace(stripped, new, 1)
            else:
                return f"old_str not found in {fp}."
        else:
            text = text.replace(old, new, 1)
        target.write_text(text, encoding="utf-8")
        if name not in self.touched_skills:
            self.touched_skills.append(name)
        return f"Replaced text in {fp}"

    def _handle_report_skill_decision(self, args: dict) -> str:
        self.skill_decisions.append({
            "action": args.get("action", "skip"),
            "skill_name": args.get("skill_name", ""),
            "reason": args.get("reason", ""),
        })
        return "OK"

    def _handle_finish(self, args: dict) -> str:
        return "FINISH"
