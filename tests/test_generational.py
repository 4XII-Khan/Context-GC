"""Tests for context_gc.generational: decay, clamp, and scoring."""

import pytest

from context_gc.state import RoundMeta
from context_gc.generational import update_generational_scores


class TestGenerational:
    @pytest.mark.asyncio
    async def test_decay_and_clamp(self):
        rounds = [
            RoundMeta(round_id=1, summary="database design", gen_score=5, token_count=10),
            RoundMeta(round_id=2, summary="weather forecast", gen_score=-4, token_count=10),
        ]

        async def mock_relevance(user_text, summaries):
            return [0.8, 0.2]

        await update_generational_scores(
            rounds, "database query optimization", mock_relevance,
            decay=0.9, clamp=(-5, 5),
        )

        assert -5 <= rounds[0].gen_score <= 5
        assert -5 <= rounds[1].gen_score <= 5
