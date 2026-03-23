"""
distillation/distill_tools.py

蒸馏 4 个 tool schema + 结果解析（复用 AsMe 设计）。
"""

from __future__ import annotations

import json
import re

from .models import DistillationOutcome

DISTILL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "skip_learning",
            "description": "Skip learning — task is trivial. Use Simplified Chinese (简体中文) for reason.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "简体中文，简要说明为何跳过"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_success_analysis",
            "description": "Report analysis of a successful task. All string fields must be Simplified Chinese (简体中文).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_goal": {"type": "string", "description": "简体中文"},
                    "approach": {"type": "string", "description": "简体中文"},
                    "key_decisions": {"type": "array", "items": {"type": "string", "description": "简体中文"}},
                    "generalizable_pattern": {"type": "string", "description": "简体中文"},
                },
                "required": ["task_goal", "approach", "key_decisions", "generalizable_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_factual_content",
            "description": "Report factual content from a task. All strings Simplified Chinese (简体中文).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_goal": {"type": "string", "description": "简体中文"},
                    "facts": {"type": "array", "items": {"type": "string", "description": "简体中文，第三人称事实陈述"}},
                },
                "required": ["task_goal", "facts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_failure_analysis",
            "description": "Report analysis of a failed task. All string fields Simplified Chinese (简体中文).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_goal": {"type": "string", "description": "简体中文"},
                    "failure_point": {"type": "string", "description": "简体中文"},
                    "flawed_reasoning": {"type": "string", "description": "简体中文"},
                    "what_should_have_been_done": {"type": "string", "description": "简体中文"},
                    "prevention_principle": {"type": "string", "description": "简体中文"},
                },
                "required": ["task_goal", "failure_point", "flawed_reasoning",
                             "what_should_have_been_done", "prevention_principle"],
            },
        },
    },
]


def extract_distillation_result(resp: dict) -> DistillationOutcome:
    """从 LLM response 中解析蒸馏结果。"""
    tool_calls = resp.get("tool_calls")
    if not tool_calls:
        return DistillationOutcome(is_worth_learning=False, skip_reason="No tool call in response")

    tc = tool_calls[0]
    name = tc["function"]["name"]
    args = tc["function"]["arguments"]
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}

    if name == "skip_learning":
        return DistillationOutcome(
            is_worth_learning=False,
            skip_reason=args.get("reason", "trivial"),
            tool_name=name,
        )

    if name == "report_success_analysis":
        decisions = args.get("key_decisions") or []
        if isinstance(decisions, str):
            decisions = [decisions]
        elif decisions and all(isinstance(d, str) and len(d) <= 2 for d in decisions):
            merged = "".join(decisions)
            parts = re.split(r"[；。]\s*|\s*(?=\d+\)\s*)", merged)
            decisions = [p.strip() for p in parts if p.strip() and len(p.strip()) > 2]
        decisions_str = "\n".join(f" - {d}" for d in decisions)
        text = f"""## Task Analysis (Success)
**Goal:** {args.get('task_goal', '')}
**Approach:** {args.get('approach', '')}
**Key Decisions:**
{decisions_str}
**Generalizable Pattern:** {args.get('generalizable_pattern', '')}"""
        return DistillationOutcome(is_worth_learning=True, distilled_text=text, tool_name=name)

    if name == "report_factual_content":
        facts = args.get("facts") or []
        facts_str = "\n".join(f" - {f}" for f in facts)
        text = f"""## Factual Content
**Context:** {args.get('task_goal', '')}
**Facts:**
{facts_str}"""
        return DistillationOutcome(is_worth_learning=True, distilled_text=text, tool_name=name)

    if name == "report_failure_analysis":
        text = f"""## Task Analysis (Failure)
**Goal:** {args.get('task_goal', '')}
**Failure Point:** {args.get('failure_point', '')}
**Flawed Reasoning:** {args.get('flawed_reasoning', '')}
**What Should Have Been Done:** {args.get('what_should_have_been_done', '')}
**Prevention Principle:** {args.get('prevention_principle', '')}"""
        return DistillationOutcome(is_worth_learning=True, distilled_text=text, tool_name=name)

    return DistillationOutcome(is_worth_learning=False, skip_reason=f"Unknown tool: {name}")
