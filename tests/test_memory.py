"""Tests for context_gc.memory: PreferenceDetector + Lifecycle."""

from datetime import datetime

import pytest

from context_gc.storage.backend import UserPreference, UserExperience
from context_gc.memory.preference import PreferenceDetector
from context_gc.memory.lifecycle import (
    filter_stale_preferences,
    filter_stale_experiences,
    build_memory_injection,
)


class TestPreferenceDetector:
    def test_detect_explicit_preference(self):
        detector = PreferenceDetector()
        messages = [{"role": "user", "content": "我偏好使用 TypeScript"}]
        prefs = detector.detect(messages, user_id="u1")
        assert len(prefs) >= 1
        assert any("TypeScript" in p.l0 for p in prefs)

    def test_detect_correction(self):
        detector = PreferenceDetector()
        messages = [{"role": "user", "content": "不要用 var 声明变量"}]
        prefs = detector.detect(messages, user_id="u1")
        assert len(prefs) >= 1
        assert prefs[0].category == "corrections"

    def test_detect_writing_style(self):
        detector = PreferenceDetector()
        messages = [{"role": "user", "content": "用中文回复"}]
        prefs = detector.detect(messages, user_id="u1")
        assert len(prefs) >= 1

    def test_no_preference(self):
        detector = PreferenceDetector()
        messages = [{"role": "user", "content": "今天天气怎么样？"}]
        prefs = detector.detect(messages, user_id="u1")
        assert len(prefs) == 0

    def test_ignores_non_user_messages(self):
        detector = PreferenceDetector()
        messages = [{"role": "assistant", "content": "我偏好使用 Python"}]
        prefs = detector.detect(messages, user_id="u1")
        assert len(prefs) == 0


class TestLifecycle:
    def test_filter_stale_preferences(self):
        now = datetime(2026, 3, 20)
        prefs = [
            UserPreference(user_id="u1", category="c", l0="recent", updated_at="2026-03-01T00:00:00"),
            UserPreference(user_id="u1", category="c", l0="old", updated_at="2025-01-01T00:00:00"),
        ]
        active, stale = filter_stale_preferences(prefs, ttl_days=90, now=now)
        assert len(active) == 1
        assert active[0].l0 == "recent"
        assert len(stale) == 1

    def test_build_memory_injection(self):
        prefs = [UserPreference(user_id="u1", category="style", l0="concise replies")]
        exps = [UserExperience(task_desc="login", success=True, content="JWT approach")]
        skills = [{"name": "api-design", "description": "API design skill"}]

        text = build_memory_injection(
            prefs, exps, skills,
            max_tokens=2000,
            current_query="login feature",
        )
        assert "concise replies" in text
        assert "JWT" in text

    def test_injection_respects_token_limit(self):
        prefs = [UserPreference(user_id="u1", category="style", l0="a" * 500)]
        text = build_memory_injection(prefs, [], [], max_tokens=10)
        assert len(text) >= 0
