"""
distillation/skill_learner.py

Skill Learner Agent — 消费蒸馏结果，更新/创建用户技能。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from .skill_learner_prompt import SKILL_LEARNER_SYSTEM_PROMPT, pack_skill_learner_input
from .skill_learner_tools import SKILL_LEARNER_TOOL_SCHEMAS, SkillLearnerToolContext

_log = logging.getLogger(__name__)

CallLLM = Callable[[str, list[dict], list[dict]], dict]

_user_locks: dict[str, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def get_user_learn_lock(user_id: str) -> threading.Lock:
    """获取用户级锁，保证同一用户 Skill Learner 串行执行。"""
    with _user_locks_guard:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


def scan_skills_dir(skills_dir: str | Path) -> list[dict]:
    """扫描技能目录，返回 [{display_name, description, dir_name, path, session_id?, last_session_id?}]。

    ``display_name`` 来自 SKILL 前置 ``name:``（多为中文展示名）；``dir_name`` 为磁盘目录名（kebab-case 等），
    供 get_skill / str_replace 等工具的 ``skill_name`` 参数使用。
    """
    d = Path(skills_dir)
    if not d.exists():
        return []
    results: list[dict] = []
    for sd in sorted(d.iterdir()):
        if not sd.is_dir():
            continue
        skill_file = sd / "SKILL.md"
        if not skill_file.exists():
            continue
        meta: dict = {}
        meta_path = sd / ".meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        content = skill_file.read_text(encoding="utf-8")
        display_name = sd.name
        desc = ""
        in_desc_block = False
        desc_lines: list[str] = []
        for line in content.splitlines():
            if line.startswith("name:"):
                display_name = line.split(":", 1)[1].strip().strip('"\'')
                continue
            if line.startswith("description:"):
                rest = line.split(":", 1)[1].strip()
                if rest in ("|", ">"):
                    in_desc_block = True
                    desc_lines = []
                elif rest:
                    desc = rest.strip().strip('"')
                else:
                    in_desc_block = True
                    desc_lines = []
                continue
            if in_desc_block:
                if line.strip() == "---":
                    break
                if line.strip():
                    desc_lines.append(line.strip())
        if desc_lines:
            desc = " ".join(desc_lines[:3])
            if len(desc_lines) > 3:
                desc += "…"
        results.append({
            "display_name": display_name,
            "description": desc,
            "dir_name": sd.name,
            "path": str(skill_file),
            "session_id": meta.get("session_id") or "",
            "last_session_id": meta.get("last_session_id") or "",
        })
    return results


def run_skill_learner(
    distilled_context: str,
    skills_dir: str | Path,
    call_llm: CallLLM,
    *,
    max_iterations: int = 10,
    system_prompt: str = "",
    session_id: str = "",
    trace: list[str] | None = None,
) -> tuple[list[str], list[dict]]:
    """
    执行 Skill Learner，返回 (touched_skills, skill_decisions)。
    """
    _trace = trace if trace is not None else []
    reference_date = datetime.now().strftime("%Y-%m-%d")
    base_system = system_prompt.strip() or SKILL_LEARNER_SYSTEM_PROMPT
    system = (
        f"{base_system}\n\n## 当天参考日期\n"
        f"本机当天日期为 **{reference_date}**。技能文件中凡需写日期的条目，必须使用此日。"
    )

    all_skills = scan_skills_dir(skills_dir)
    lines: list[str] = []
    for s in all_skills:
        dname = s["dir_name"]
        disp = s.get("display_name") or dname
        if disp == dname or not disp:
            line = f"- `{dname}`（**skill_name 用此字符串**）| 简介：{s['description']}"
        else:
            line = (
                f"- 目录 `{dname}` | YAML 名：**{disp}**（**skill_name 用目录名**）"
                f" | 简介：{s['description']}"
            )
        sid = s.get("session_id") or ""
        lid = s.get("last_session_id") or ""
        if sid:
            line += f" [来源会话: {sid}]"
        if lid and lid != sid:
            line += f" [最近更新会话: {lid}]"
        lines.append(line)
    skills_str = "\n".join(lines) if lines else "（暂无技能）"

    user_input = pack_skill_learner_input(
        distilled_context,
        skills_str,
        reference_date=reference_date,
        session_id=session_id,
    )
    ctx = SkillLearnerToolContext(skills_dir, session_id=session_id)

    llm_messages: list[dict] = [{"role": "user", "content": user_input}]

    for iteration in range(max_iterations):
        try:
            resp = call_llm(system, llm_messages, SKILL_LEARNER_TOOL_SCHEMAS)
        except Exception as e:
            _log.error("Skill Learner LLM call failed: %s", e)
            _trace.append(f"  [SkillLearner] LLM 异常: {e}")
            break

        llm_messages.append(resp)
        tool_calls = resp.get("tool_calls")

        if not tool_calls:
            break

        tool_responses: list[dict] = []
        should_finish = False
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except json.JSONDecodeError:
                    fn_args = {}

            if fn_name == "finish":
                should_finish = True
                tool_responses.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "FINISH",
                })
                continue

            result = ctx.execute(fn_name, fn_args)
            _trace.append(f"    → {fn_name}: {result}")
            tool_responses.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        llm_messages.extend(tool_responses)
        if should_finish:
            break

    _trace.append(f"  [SkillLearner] touched={ctx.touched_skills} decisions={len(ctx.skill_decisions)}")
    return ctx.touched_skills, ctx.skill_decisions
