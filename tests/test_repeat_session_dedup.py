"""
同一用户、多次等效「会话结束落库」场景下的重复性测试。

对应真实链路：
- 偏好：`flush_distillation` → `save_user_preferences`（带 exact / keyword_overlap 去重）
- 经验：`write_experiences` 按每条 `task_desc` 加载该任务下历史再 `save_user_experience`
- 技能：`save_user_skill` 按技能名写目录，**整文件覆盖**，不会产生第二个同名技能目录；
  SKILL.md 正文内的条目重复由 Skill Learner / LLM 决定，存储层不做正文去重。
"""

from __future__ import annotations

import json

import pytest

from context_gc.distillation.experience_writer import write_experiences
from context_gc.storage import FileBackend, UserExperience, UserPreference


@pytest.mark.asyncio
async def test_repeat_saves_preferences_exact_dedup_three_runs(tmp_path):
    """同一偏好文本连续保存 3 次（exact）：应仍为 1 条。"""
    backend = FileBackend(tmp_path)
    uid = "user_a"
    pref = UserPreference(
        user_id=uid,
        category="explicit_prefs",
        l0="用户偏好使用 Python 写脚本",
        source_session="sess_1",
    )
    for sid in ("run_1", "run_2", "run_3"):
        await backend.save_user_preferences(
            uid, [pref], sid, dedup_strategy="exact"
        )
    loaded = await backend.load_user_preferences(uid)
    assert len(loaded) == 1
    md = tmp_path / "user" / uid / "preferences" / "preferences.md"
    text = md.read_text(encoding="utf-8")
    assert text.count("用户偏好使用 Python 写脚本") == 1
    assert "(session:" not in text
    idx = tmp_path / "user" / uid / "preferences" / ".preference_index.json"
    assert idx.exists()
    rows = json.loads(idx.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0].get("source_session")


@pytest.mark.asyncio
async def test_repeat_saves_preferences_keyword_overlap_dedup(tmp_path):
    """措辞略不同但词集合 Jaccard 高：keyword_overlap 下第二次应判重，不新增行。"""
    backend = FileBackend(tmp_path)
    uid = "user_b"
    # 6 词 vs 5 词，交集 5、并集 6 → Jaccard ≈ 0.83 > 0.8
    p1 = UserPreference(
        user_id=uid,
        category="explicit_prefs",
        l0="alpha beta gamma delta epsilon zeta",
        source_session="s1",
    )
    p2 = UserPreference(
        user_id=uid,
        category="explicit_prefs",
        l0="alpha beta gamma delta epsilon",
        source_session="s2",
    )
    await backend.save_user_preferences(
        uid, [p1], "s1", dedup_strategy="keyword_overlap", dedup_threshold=0.8
    )
    await backend.save_user_preferences(
        uid, [p2], "s2", dedup_strategy="keyword_overlap", dedup_threshold=0.8
    )
    loaded = await backend.load_user_preferences(uid)
    assert len(loaded) == 1


@pytest.mark.asyncio
async def test_repeat_write_experiences_same_content_twice_writes_once(tmp_path):
    """模拟同一任务、同一成功条目的蒸馏结果连续提交两次：第二次写入条数应为 0。"""
    backend = FileBackend(tmp_path)
    uid = "user_c"
    exp = UserExperience(
        task_desc="实现用户登录功能",
        success=True,
        content="采用 JWT 无状态认证并配置短期 access token",
        source_session="sess_a",
    )
    n1 = await write_experiences(
        uid, [exp], "sess_a", backend, dedup_strategy="keyword_overlap"
    )
    n2 = await write_experiences(
        uid, [exp], "sess_b", backend, dedup_strategy="keyword_overlap"
    )
    assert n1 == 1
    assert n2 == 0
    loaded = await backend.load_user_experience(uid)
    success = [e for e in loaded if e.success]
    assert len(success) == 1
    assert "JWT" in success[0].content


@pytest.mark.asyncio
async def test_repeat_write_experiences_near_duplicate_keyword_overlap(tmp_path):
    """与 experience_writer._keyword_overlap 一致：高 Jaccard 视为同一条，不二次写入。"""
    backend = FileBackend(tmp_path)
    uid = "user_d"
    e1 = UserExperience(
        task_desc="report task",
        success=True,
        content="verify sales csv and attendance csv then build html report output",
        source_session="s1",
    )
    e2 = UserExperience(
        task_desc="report task",
        success=True,
        content="verify sales csv attendance csv then build html report",
        source_session="s2",
    )
    n1 = await write_experiences(
        uid, [e1], "s1", backend, dedup_strategy="keyword_overlap", dedup_threshold=0.75
    )
    n2 = await write_experiences(
        uid, [e2], "s2", backend, dedup_strategy="keyword_overlap", dedup_threshold=0.75
    )
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_skill_same_name_second_save_overwrites_not_new_directory(tmp_path):
    """同名技能第二次 save：仍只有一个技能目录，内容为最后一次写入。"""
    backend = FileBackend(tmp_path)
    uid = "user_e"
    name = "my-skill"
    await backend.save_user_skill(uid, name, "v1 body")
    await backend.save_user_skill(uid, name, "v2 body")
    skills = await backend.load_user_skills(uid)
    assert len(skills) == 1
    assert "v2 body" in skills[0]["content"]
    assert "v1 body" not in skills[0]["content"]


@pytest.mark.asyncio
async def test_write_experiences_dedup_only_within_same_task(tmp_path):
    """不同 task_desc 下相同正文：不因其它任务已有条目而被判重。"""
    backend = FileBackend(tmp_path)
    await backend.save_user_experience(
        "u1",
        [UserExperience(task_desc="任务甲", success=True, content="完全相同的一句话")],
        "s0",
    )
    n = await write_experiences(
        "u1",
        [UserExperience(task_desc="任务乙", success=True, content="完全相同的一句话")],
        "s1",
        backend,
        dedup_strategy="exact",
    )
    assert n == 1


@pytest.mark.asyncio
async def test_experience_bypass_writer_appends_duplicates(tmp_path):
    """说明：若直接调用 save_user_experience 而不经 write_experiences，会追加重复行（负面参照）。"""
    backend = FileBackend(tmp_path)
    uid = "user_f"
    exp = UserExperience(
        task_desc="同一任务描述",
        success=True,
        content="完全相同的经验条目文本",
        source_session="s1",
    )
    await backend.save_user_experience(uid, [exp], "s1")
    await backend.save_user_experience(uid, [exp], "s2")
    loaded = await backend.load_user_experience(uid)
    assert len([e for e in loaded if e.success]) == 2
