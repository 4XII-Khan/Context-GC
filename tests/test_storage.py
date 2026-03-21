"""Tests for context_gc.storage: FileBackend + CheckpointManager."""

import tempfile
from pathlib import Path

import pytest

from context_gc.storage.backend import UserPreference, UserExperience
from context_gc.storage.file_backend import FileBackend
from context_gc.storage.checkpoint import CheckpointManager
from context_gc.state import RoundMeta, ContextGCState


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestFileBackend:
    @pytest.mark.asyncio
    async def test_save_and_load_session(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        await backend.save_session("s1", "l0 text", ["summary1", "summary2"], "content.md")

        l1 = await backend.load_session_l1("s1")
        assert l1 == ["summary1", "summary2"]

        l2 = await backend.load_session_l2("s1")
        assert l2 is None

    @pytest.mark.asyncio
    async def test_search_sessions(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        await backend.save_session("s1", "OAuth login impl", ["Implemented OAuth login"], "")
        await backend.save_session("s2", "Database optimization", ["Optimized queries"], "")

        results = await backend.search_sessions("login")
        assert len(results) >= 1
        assert results[0]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_preferences(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        prefs = [
            UserPreference(user_id="u1", category="writing_style", l0="concise replies"),
            UserPreference(user_id="u1", category="coding_habits", l0="prefer TypeScript"),
        ]
        await backend.save_user_preferences("u1", prefs, "s1")

        loaded = await backend.load_user_preferences("u1")
        assert len(loaded) == 2

        loaded_filtered = await backend.load_user_preferences("u1", "writing_style")
        assert len(loaded_filtered) == 1
        assert loaded_filtered[0].l0 == "concise replies"

    @pytest.mark.asyncio
    async def test_preference_dedup_exact(self, tmp_dir):
        """偏好去重：exact 策略，完全相同的 l0 不重复写入。"""
        backend = FileBackend(tmp_dir)
        prefs = [
            UserPreference(user_id="u1", category="explicit_prefs", l0="prefer TypeScript"),
        ]
        await backend.save_user_preferences("u1", prefs, "s1")
        loaded = await backend.load_user_preferences("u1")
        assert len(loaded) == 1

        await backend.save_user_preferences(
            "u1",
            [UserPreference(user_id="u1", category="explicit_prefs", l0="prefer TypeScript")],
            "s2",
            dedup_strategy="exact",
        )
        loaded = await backend.load_user_preferences("u1")
        assert len(loaded) == 1

    @pytest.mark.asyncio
    async def test_preference_dedup_keyword_overlap(self, tmp_dir):
        """偏好去重：keyword_overlap 策略，关键词重叠的 l0 不重复写入。"""
        backend = FileBackend(tmp_dir)
        await backend.save_user_preferences(
            "u1",
            [UserPreference(user_id="u1", category="explicit_prefs", l0="prefer TypeScript")],
            "s1",
        )
        loaded = await backend.load_user_preferences("u1")
        assert len(loaded) == 1

        await backend.save_user_preferences(
            "u1",
            [UserPreference(user_id="u1", category="explicit_prefs", l0="prefer using TypeScript")],
            "s2",
            dedup_strategy="keyword_overlap",
            dedup_threshold=0.5,
        )
        loaded = await backend.load_user_preferences("u1")
        assert len(loaded) == 1

    @pytest.mark.asyncio
    async def test_experience(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        exps = [
            UserExperience(task_desc="implement login", success=True, content="used JWT"),
            UserExperience(task_desc="implement login", success=False, content="forgot CORS config"),
        ]
        await backend.save_user_experience("u1", exps, "s1")

        loaded = await backend.load_user_experience("u1")
        assert len(loaded) == 2
        assert len([e for e in loaded if e.success]) == 1
        assert len([e for e in loaded if not e.success]) == 1

    @pytest.mark.asyncio
    async def test_skills(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        await backend.save_user_skill("u1", "api-design", '---\nname: "api-design"\ndescription: "API design"\n---\n# API')

        skills = await backend.load_user_skills("u1")
        assert len(skills) == 1
        assert skills[0]["name"] == "api-design"

    @pytest.mark.asyncio
    async def test_delete_session(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        await backend.save_session("s1", "test", ["summary"], "")
        await backend.delete_session("s1")
        assert await backend.load_session_l1("s1") is None

    @pytest.mark.asyncio
    async def test_list_expired_sessions(self, tmp_dir):
        backend = FileBackend(tmp_dir)
        await backend.save_session("s_old", "old", [], "", meta={"created_at": "2020-01-01T00:00:00"})
        await backend.save_session("s_new", "new", [], "", meta={"created_at": "2099-01-01T00:00:00"})

        expired = await backend.list_expired_sessions("2025-01-01T00:00:00")
        assert "s_old" in expired
        assert "s_new" not in expired


class TestCheckpoint:
    def test_write_and_recover(self, tmp_dir):
        mgr = CheckpointManager(tmp_dir, "sess_001", checkpoint_interval=2)
        state = ContextGCState(
            rounds=[
                RoundMeta(round_id=1, summary="summary1", gen_score=1, token_count=10),
                RoundMeta(round_id=2, summary="summary2", gen_score=-1, token_count=15),
            ],
            max_tokens=5000,
        )
        state.recalc_total_tokens()

        msgs = [{"role": "user", "content": "hello"}]
        mgr.on_round_close(state, msgs)
        mgr.on_round_close(state, msgs)

        assert mgr.checkpoint_path.exists()

        recovered = mgr.try_recover(5000, 0.1)
        assert recovered is not None
        assert len(recovered.rounds) == 2
        assert recovered.rounds[0].summary == "summary1"
        assert recovered.rounds[1].gen_score == -1

    def test_cleanup(self, tmp_dir):
        mgr = CheckpointManager(tmp_dir, "sess_002", checkpoint_interval=1)
        state = ContextGCState(rounds=[RoundMeta(round_id=1, summary="s", token_count=5)], max_tokens=1000)
        mgr.on_round_close(state, [{"role": "user", "content": "test"}])
        assert mgr.checkpoint_path.exists()

        mgr.cleanup()
        assert not mgr.checkpoint_path.exists()
        assert mgr.content_path.exists()

    def test_no_checkpoint_returns_none(self, tmp_dir):
        mgr = CheckpointManager(tmp_dir, "sess_none")
        assert mgr.try_recover(5000, 0.1) is None
