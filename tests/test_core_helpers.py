"""ContextGC 开箱辅助：工厂方法、蒸馏参数、trace 策略、预设、记忆注入封装。"""

from __future__ import annotations

import pytest

from context_gc import (
    ContextGC,
    ContextGCOptions,
    LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS,
)
from context_gc.memory import LifecycleConfig
from context_gc.storage import FileBackend
from context_gc.storage.backend import UserPreference


def _minimal_options(**extra):
    """不依赖 OpenAI 的 ContextGCOptions，供异步用例使用。"""

    async def _gs(_m, **_k):
        return "s"

    async def _ms(_g, **_k):
        return "m"

    async def _cr(_u, _s):
        return [0.5]

    base = dict(
        max_input_tokens=1000,
        generate_summary=_gs,
        merge_summary=_ms,
        compute_relevance=_cr,
        estimate_tokens=lambda _t: 1,
        data_dir="",
    )
    base.update(extra)
    return ContextGCOptions(**base)


def test_preset_small_chat_defaults():
    o = ContextGCOptions.preset_small_chat()
    assert o.max_input_tokens == 4000
    assert o.checkpoint_interval == 3
    assert o.scoring_interval == 2
    assert o.flush_min_messages == 2


def test_preset_agent_long_context_defaults():
    o = ContextGCOptions.preset_agent_long_context()
    assert o.max_input_tokens == 32000
    assert o.checkpoint_interval == 8
    assert o.scoring_interval == 4
    assert o.flush_min_messages == 4
    assert o.merge_gradient_by_tokens == LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS


def test_preset_small_chat_kwarg_override():
    o = ContextGCOptions.preset_small_chat(max_input_tokens=9000)
    assert o.max_input_tokens == 9000
    assert o.flush_min_messages == 2


def test_create_with_file_backend_no_options_sets_env_defaults(tmp_path):
    """未传 options 时 with_env_defaults(data_dir=...) + FileBackend。"""
    gc = ContextGC.create_with_file_backend(tmp_path, session_id="s0")
    assert gc.session_id == "s0"
    assert gc.backend is not None
    assert gc.options.data_dir == str(tmp_path.resolve())


def test_create_with_file_backend_forwards_kwargs_to_with_env_defaults(tmp_path):
    """未传 options 时，多余关键字参数传给 with_env_defaults（如 max_input_tokens）。"""
    gc = ContextGC.create_with_file_backend(
        tmp_path, session_id="s_kw", max_input_tokens=7777
    )
    assert gc.options.max_input_tokens == 7777
    assert gc.options.data_dir == str(tmp_path.resolve())


def test_create_with_file_backend_creates_nested_directory(tmp_path):
    nested = tmp_path / "a" / "b" / "store"
    assert not nested.exists()
    gc = ContextGC.create_with_file_backend(nested, session_id="nested")
    assert nested.is_dir()
    assert gc.options.data_dir == str(nested.resolve())


def test_create_with_file_backend_sets_paths(tmp_path):
    opts = _minimal_options(data_dir="")
    gc = ContextGC.create_with_file_backend(tmp_path, session_id="s1", options=opts)
    assert gc.session_id == "s1"
    assert gc.backend is not None
    assert gc.options.data_dir == str(tmp_path.resolve())


def test_with_env_defaults_reads_flush_env_vars(monkeypatch):
    monkeypatch.setenv("CONTEXT_GC_FLUSH_MIN_MESSAGES", "7")
    monkeypatch.setenv("CONTEXT_GC_FLUSH_INCLUDE_TRACE", "1")
    o = ContextGCOptions.with_env_defaults(flush_min_messages=None, flush_distillation_trace=None)
    assert o.flush_min_messages == 7
    assert o.flush_distillation_trace is True


@pytest.mark.asyncio
async def test_build_memory_injection_text_from_backend(tmp_path):
    opts = _minimal_options(data_dir=str(tmp_path))
    backend = FileBackend(tmp_path)
    gc = ContextGC(opts, session_id="s1", backend=backend)
    await backend.save_user_preferences(
        "u_inject",
        [
            UserPreference(
                user_id="u_inject",
                category="style",
                l0="回复要简洁",
                l1="不要超过三句",
            )
        ],
        session_id="s1",
    )
    cfg = LifecycleConfig(memory_inject_max_tokens=2000)
    text = await gc.build_memory_injection_text("u_inject", current_query="", config=cfg)
    assert "回复要简洁" in text
    assert "用户偏好" in text


@pytest.mark.asyncio
async def test_custom_flush_receives_distillation_kwargs(tmp_path):
    opts = _minimal_options(
        data_dir=str(tmp_path),
        flush_min_messages=11,
        flush_task_agent_max_iterations=33,
        flush_skill_learner_max_iterations=5,
        flush_experience_task_assign_mode="heuristic",
        flush_dedup_strategy="exact",
        flush_distillation_trace=False,
    )
    backend = FileBackend(tmp_path)
    gc = ContextGC(opts, session_id="s_flush_kw", backend=backend)
    captured: dict = {}

    async def _spy_flush(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return {"ok": True}

    await gc.on_session_end("u1", flush_distillation=_spy_flush)
    assert captured.get("min_messages") == 11
    assert captured.get("task_agent_max_iterations") == 33
    assert captured.get("skill_learner_max_iterations") == 5
    assert captured.get("experience_task_assign_mode") == "heuristic"
    assert captured.get("dedup_strategy") == "exact"
    assert captured.get("options") is opts
    assert captured.get("session_id") == "s_flush_kw"
    assert captured.get("user_id") == "u1"


@pytest.mark.asyncio
async def test_distillation_trace_stripped_when_disabled(tmp_path):
    opts = _minimal_options(
        data_dir=str(tmp_path),
        flush_distillation_trace=False,
        flush_min_messages=0,
    )
    backend = FileBackend(tmp_path)
    gc = ContextGC(opts, session_id="s1", backend=backend)

    async def _mock_flush(**_kwargs):
        return {"task_count": 0, "trace": ["line1", "line2"]}

    r = await gc.on_session_end("u1", flush_distillation=_mock_flush)
    dist = r.get("distillation") or {}
    assert "trace" not in dist


@pytest.mark.asyncio
async def test_distillation_trace_kept_when_enabled(tmp_path):
    opts = _minimal_options(
        data_dir=str(tmp_path),
        flush_distillation_trace=True,
        flush_min_messages=0,
    )
    backend = FileBackend(tmp_path)
    gc = ContextGC(opts, session_id="s1", backend=backend)

    async def _mock_flush(**_kwargs):
        return {"task_count": 0, "trace": ["a"]}

    r = await gc.on_session_end("u1", flush_distillation=_mock_flush)
    assert r.get("distillation", {}).get("trace") == ["a"]
