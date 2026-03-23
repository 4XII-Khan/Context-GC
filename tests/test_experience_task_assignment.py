"""经验任务归并：LLM + task_index。"""

from __future__ import annotations

import json

import pytest

from context_gc.distillation.experience_writer import write_experiences
from context_gc.distillation.task_assignment_llm import assign_experience_task_descs_with_llm
from context_gc.storage import FileBackend, UserExperience


def test_assign_experience_task_descs_with_llm_reuse_and_new():
    index = [
        {
            "slug": "实现用户登录功能",
            "canonical_desc": "实现用户登录功能",
            "alt_descs": [],
        }
    ]

    def call_llm(system: str, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "assignments": [
                {"batch_index": 1, "action": "reuse", "existing_index": 0},
                {
                    "batch_index": 2,
                    "action": "new",
                    "canonical_desc": "生成季度销售报表",
                },
            ]
        }
        return {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}

    unique = ["做登录模块", "完全不同的报表任务"]
    m = assign_experience_task_descs_with_llm(unique, index, call_llm)
    assert m["做登录模块"] == "实现用户登录功能"
    assert m["完全不同的报表任务"] == "生成季度销售报表"


@pytest.mark.asyncio
async def test_write_experiences_llm_mode_maps_to_canonical(tmp_path):
    """LLM 将改写后的 task_desc 归并到已有 canonical，经验落在同一目录。"""
    backend = FileBackend(tmp_path)
    uid = "u_llm_assign"
    await backend.save_user_experience(
        uid,
        [
            UserExperience(
                task_desc="实现用户登录功能",
                success=True,
                content="first line",
                source_session="s0",
            )
        ],
        "s0",
    )

    def call_llm(system: str, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "assignments": [
                {"batch_index": 1, "action": "reuse", "existing_index": 0},
            ]
        }
        return {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}

    n = await write_experiences(
        uid,
        [
            UserExperience(
                task_desc="做登录模块",
                success=True,
                content="second line",
                source_session="s1",
            )
        ],
        "s1",
        backend,
        dedup_strategy="exact",
        task_assign_mode="llm",
        call_llm=call_llm,
    )
    assert n == 1

    all_e = await backend.load_user_experience(uid)
    success_contents = [e.content for e in all_e if e.success]
    assert "first line" in success_contents
    assert "second line" in success_contents


@pytest.mark.asyncio
async def test_write_experiences_llm_invalid_json_falls_back_identity(tmp_path):
    backend = FileBackend(tmp_path)

    def call_llm(_s, _m, _t) -> dict:
        return {"role": "assistant", "content": "not json"}

    n = await write_experiences(
        "u",
        [
            UserExperience(
                task_desc="任务甲",
                success=True,
                content="x",
                source_session="s1",
            )
        ],
        "s1",
        backend,
        dedup_strategy="exact",
        task_assign_mode="llm",
        call_llm=call_llm,
    )
    assert n == 1
