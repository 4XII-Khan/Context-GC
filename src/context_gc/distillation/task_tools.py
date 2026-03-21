"""
distillation/task_tools.py

Task Agent 的 8 个 tool schema 和 handler（复用 AsMe 设计）。
"""

from __future__ import annotations

import uuid
from typing import Any

from .models import TaskSchema, TaskData, TaskStatus

TASK_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "report_thinking",
            "description": "Report your thinking before taking actions",
            "parameters": {
                "type": "object",
                "properties": {"thinking": {"type": "string", "description": "Your thinking process"}},
                "required": ["thinking"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_task",
            "description": "Insert a new task after the specified order. Each task = one user intent/goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "after_task_order": {"type": "integer", "description": "Insert after this order (0 = at beginning)"},
                    "task_description": {"type": "string", "description": "The user's request verbatim or closely paraphrased"},
                    "task_steps": {"type": "array", "items": {"type": "string"}, "description": "Optional execution steps"},
                },
                "required": ["after_task_order", "task_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Update a task's status and/or description",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_order": {"type": "integer"},
                    "task_status": {"type": "string", "enum": ["pending", "running", "success", "failed"]},
                    "task_description": {"type": "string"},
                },
                "required": ["task_order"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_messages_to_task",
            "description": "Link a range of message IDs to a task. Auto-sets status to running.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_order": {"type": "integer"},
                    "message_id_range": {
                        "type": "array", "items": {"type": "integer"},
                        "minItems": 2, "maxItems": 2,
                        "description": "[start_id, end_id] inclusive range",
                    },
                },
                "required": ["task_order", "message_id_range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_messages_to_planning_section",
            "description": "Assign messages to the planning section (not task execution)",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id_range": {
                        "type": "array", "items": {"type": "integer"},
                        "minItems": 2, "maxItems": 2,
                    },
                },
                "required": ["message_id_range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_task_progress",
            "description": "Record specific progress for a task",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_order": {"type": "integer"},
                    "progress": {"type": "string", "description": "Specific progress description"},
                },
                "required": ["task_order", "progress"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_user_preference",
            "description": "Submit a user preference or fact (third-person, task-independent)",
            "parameters": {
                "type": "object",
                "properties": {
                    "preference": {"type": "string", "description": "e.g. 'The user prefers TypeScript'"},
                },
                "required": ["preference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish this round of task analysis",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class TaskToolContext:
    """Task Agent 的工具执行上下文，维护任务列表。"""

    def __init__(self, session_id: str, tasks: list[TaskSchema] | None = None):
        self.session_id = session_id
        self.tasks: list[TaskSchema] = list(tasks or [])
        self.pending_preferences: list[str] = []
        self.planning_content: str = ""

    def _find_task(self, order: int) -> TaskSchema | None:
        return next((t for t in self.tasks if t.order == order), None)

    def _reorder(self) -> None:
        self.tasks.sort(key=lambda t: t.order)

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        return handler(args)

    def _handle_report_thinking(self, args: dict) -> str:
        return "OK"

    def _handle_insert_task(self, args: dict) -> str:
        after = args.get("after_task_order", 0)
        desc = args.get("task_description", "").strip()
        if not desc:
            return "Error: task_description is required"
        steps = args.get("task_steps") or []
        steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()]
        new_order = after + 1
        for t in self.tasks:
            if t.order >= new_order:
                t.order += 1
        task = TaskSchema(
            id=str(uuid.uuid4()),
            session_id=self.session_id,
            order=new_order,
            status=TaskStatus.PENDING,
            data=TaskData(task_description=desc, progresses=steps),
        )
        self.tasks.append(task)
        self._reorder()
        return f"Created Task {new_order}: {desc}"

    def _handle_update_task(self, args: dict) -> str:
        order = args.get("task_order")
        task = self._find_task(order)
        if not task:
            return f"Error: Task {order} not found"
        new_status = args.get("task_status")
        new_desc = args.get("task_description")
        if new_status:
            task.status = TaskStatus(new_status)
        if new_desc:
            task.data.task_description = new_desc
        return f"Updated Task {order}"

    def _handle_append_messages_to_task(self, args: dict) -> str:
        order = args.get("task_order")
        task = self._find_task(order)
        if not task:
            return f"Error: Task {order} not found"
        id_range = args.get("message_id_range", [])
        if len(id_range) == 2:
            for i in range(id_range[0], id_range[1] + 1):
                mid = str(i)
                if mid not in task.raw_message_ids:
                    task.raw_message_ids.append(mid)
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.RUNNING
        return f"Linked messages {id_range} to Task {order}"

    def _handle_append_messages_to_planning_section(self, args: dict) -> str:
        id_range = args.get("message_id_range", [])
        self.planning_content += f" msgs:{id_range}"
        return f"Added messages {id_range} to planning section"

    def _handle_append_task_progress(self, args: dict) -> str:
        order = args.get("task_order")
        task = self._find_task(order)
        if not task:
            return f"Error: Task {order} not found"
        progress = args.get("progress", "").strip()
        if progress:
            task.data.progresses.append(progress)
        return f"Recorded progress for Task {order}"

    def _handle_submit_user_preference(self, args: dict) -> str:
        pref = args.get("preference", "").strip()
        if pref:
            self.pending_preferences.append(pref)
        return f"Submitted preference: {pref}"

    def _handle_finish(self, args: dict) -> str:
        return "FINISH"
