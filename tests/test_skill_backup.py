"""技能更新前：仅备份本次 str_replace 改动的文件。"""

import json

import pytest

from context_gc.distillation.skill_learner_tools import (
    SkillLearnerToolContext,
    backup_skill_file,
)


def test_backup_skill_file_only_target_file(tmp_path):
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("hello", encoding="utf-8")
    (skill / ".meta.json").write_text('{"session_id": "old"}', encoding="utf-8")
    sub = skill / "refs"
    sub.mkdir()
    (sub / "note.txt").write_text("x", encoding="utf-8")

    dest = backup_skill_file(skill, "SKILL.md", session_id="sess-1")

    assert dest.parent == skill / ".backups"
    assert dest.is_dir()
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "hello"
    assert (dest / ".meta.json").read_text(encoding="utf-8") == '{"session_id": "old"}'
    assert not (dest / "refs").exists()
    meta = json.loads((dest / ".backup_meta.json").read_text(encoding="utf-8"))
    assert meta["session_id"] == "sess-1"
    assert meta["skill_dir"] == "my-skill"
    assert meta["backed_up_files"] == ["SKILL.md", ".meta.json"]
    assert "backed_up_at" in meta


def test_backup_nested_file_preserves_relative_path(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("root", encoding="utf-8")
    (skill / ".meta.json").write_text("{}", encoding="utf-8")
    (skill / "refs").mkdir()
    (skill / "refs" / "note.txt").write_text("nested", encoding="utf-8")

    dest = backup_skill_file(skill, "refs/note.txt", session_id="")
    assert (dest / "refs" / "note.txt").read_text(encoding="utf-8") == "nested"
    assert (dest / ".meta.json").read_text(encoding="utf-8") == "{}"
    assert not (dest / "SKILL.md").exists()
    snap = json.loads((dest / ".backup_meta.json").read_text(encoding="utf-8"))
    assert snap["backed_up_files"] == ["refs/note.txt", ".meta.json"]


def test_backup_meta_json_target_no_duplicate(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / ".meta.json").write_text('{"a": 1}', encoding="utf-8")

    dest = backup_skill_file(skill, ".meta.json", session_id="")
    assert (dest / ".meta.json").read_text(encoding="utf-8") == '{"a": 1}'
    snap = json.loads((dest / ".backup_meta.json").read_text(encoding="utf-8"))
    assert snap["backed_up_files"] == [".meta.json"]


def test_backup_twice_creates_two_timestamp_dirs(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("a", encoding="utf-8")
    d1 = backup_skill_file(skill, "SKILL.md", session_id="")
    (skill / "SKILL.md").write_text("b", encoding="utf-8")
    d2 = backup_skill_file(skill, "SKILL.md", session_id="")
    assert d1 != d2
    assert (d1 / "SKILL.md").read_text() == "a"
    assert (d2 / "SKILL.md").read_text() == "b"


def test_backup_rejects_path_traversal(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid"):
        backup_skill_file(skill, "../other/SKILL.md", session_id="")


def test_create_skill_directory_matches_yaml_chinese_name(tmp_path):
    """目录名与 YAML name / 中文标题一致（不再强制英文 kebab）。"""
    ctx = SkillLearnerToolContext(tmp_path, session_id="sess-x")
    md = """---
name: "跨部门协同核对"
description: |
  当需要跨部门核对时使用。
---

# 跨部门协同核对

## 概述
测试
"""
    msg = ctx.execute(
        "create_skill",
        {"skill_md_content": md},
    )
    assert "Created" in msg
    d = tmp_path / "跨部门协同核对"
    assert d.is_dir()
    assert (d / "SKILL.md").exists()
    assert "跨部门协同核对" in (d / "SKILL.md").read_text(encoding="utf-8")


def test_create_skill_rejects_skill_name_yaml_mismatch(tmp_path):
    ctx = SkillLearnerToolContext(tmp_path, session_id="s")
    md = """---
name: "中文A"
description: |
  x
---

# 中文A
"""
    err = ctx.execute(
        "create_skill",
        {"skill_md_content": md, "skill_name": "english-b"},
    )
    assert "Error" in err
    assert "不一致" in err
