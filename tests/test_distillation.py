"""Tests for context_gc.distillation: models, tools, and extraction."""

import json

import pytest

from context_gc.distillation.models import TaskSchema, TaskStatus, TaskData, DistillationOutcome
from context_gc.distillation.distill_tools import extract_distillation_result
from context_gc.distillation.task_tools import TaskToolContext


class TestDistillationModels:
    def test_task_schema(self):
        task = TaskSchema(
            session_id="s1",
            order=1,
            status=TaskStatus.SUCCESS,
            data=TaskData(task_description="implement login"),
        )
        s = task.to_string()
        assert "implement login" in s
        assert "success" in s

    def test_distillation_outcome(self):
        outcome = DistillationOutcome(is_worth_learning=True, distilled_text="test")
        assert outcome.is_worth_learning


class TestDistillTools:
    def test_extract_success_analysis(self):
        resp = {
            "tool_calls": [{
                "function": {
                    "name": "report_success_analysis",
                    "arguments": json.dumps({
                        "task_goal": "test goal",
                        "approach": "test approach",
                        "key_decisions": ["decision 1"],
                        "generalizable_pattern": "test pattern",
                    }),
                },
            }],
        }
        outcome = extract_distillation_result(resp)
        assert outcome.is_worth_learning
        assert "test goal" in outcome.distilled_text

    def test_skip_learning(self):
        resp = {
            "tool_calls": [{
                "function": {
                    "name": "skip_learning",
                    "arguments": json.dumps({"reason": "trivial"}),
                },
            }],
        }
        outcome = extract_distillation_result(resp)
        assert not outcome.is_worth_learning


class TestTaskToolContext:
    def test_insert_and_update(self):
        ctx = TaskToolContext("s1")

        result = ctx.execute("insert_task", {
            "after_task_order": 0,
            "task_description": "implement login",
        })
        assert "Created" in result
        assert len(ctx.tasks) == 1

        result = ctx.execute("update_task", {
            "task_order": 1,
            "task_status": "success",
        })
        assert "Updated" in result
        assert ctx.tasks[0].status.value == "success"

    def test_submit_preference(self):
        ctx = TaskToolContext("s1")
        ctx.execute("submit_user_preference", {"preference": "user prefers Python"})
        assert len(ctx.pending_preferences) == 1

    def test_finish(self):
        ctx = TaskToolContext("s1")
        result = ctx.execute("finish", {})
        assert result == "FINISH"
