"""
context_gc/memory/preference.py

会话中轻量级偏好抽取：零 LLM 成本，关键词/正则匹配。

在每轮 close() 时对用户消息做匹配，检测到显式偏好表达时返回 UserPreference 列表。
误检代价低——偏好可被后续蒸馏覆盖/去重。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..storage.backend import UserPreference


@dataclass
class PreferenceRule:
    """单条检测规则。"""
    category: str
    pattern: re.Pattern
    extract: Callable[[re.Match], str] | None = None


DEFAULT_RULES: list[PreferenceRule] = [
    # 显式纠正
    PreferenceRule(
        category="corrections",
        pattern=re.compile(
            r"(?:不要|别再?|不[想要需]|禁止|避免)[用使做写说]?\s*(.{2,30})",
            re.IGNORECASE,
        ),
        extract=lambda m: f"不要{m.group(1).strip()}",
    ),
    # 显式偏好
    PreferenceRule(
        category="explicit_prefs",
        pattern=re.compile(
            r"(?:我(?:偏好|喜欢|倾向|希望)|以后(?:都)?用|默认用|请?(?:始终|总是|一直))\s*(.{2,50})",
            re.IGNORECASE,
        ),
        extract=lambda m: m.group(1).strip(),
    ),
    # 风格要求
    PreferenceRule(
        category="writing_style",
        pattern=re.compile(
            r"(?:简洁[点些一]|详细[说解讲点些]|用中文|用英文|"
            r"说中文|speak\s+english|respond\s+in\s+\w+|"
            r"回复?.*?(?:简[短洁]|详细|中文|英文))",
            re.IGNORECASE,
        ),
    ),
    # 代码风格
    PreferenceRule(
        category="coding_habits",
        pattern=re.compile(
            r"(?:用|使用|切换到?)\s*(typescript|python|golang|rust|java|"
            r"tab[s ]|space[s ]|4\s*空格|2\s*空格|单引号|双引号)",
            re.IGNORECASE,
        ),
        extract=lambda m: f"偏好 {m.group(1).strip()}",
    ),
]


class PreferenceDetector:
    """
    基于规则的偏好信号检测器。

    用法::

        detector = PreferenceDetector()
        prefs = detector.detect(round_messages, user_id="u1", session_id="s1")
    """

    def __init__(
        self,
        rules: list[PreferenceRule] | None = None,
    ) -> None:
        self.rules = rules if rules is not None else list(DEFAULT_RULES)

    def detect(
        self,
        round_messages: list[dict],
        *,
        user_id: str = "",
        session_id: str = "",
    ) -> list[UserPreference]:
        """
        对本轮消息做偏好信号检测。

        Returns:
            检测到的偏好列表（可能为空）。
        """
        user_texts = self._extract_user_texts(round_messages)
        if not user_texts:
            return []

        results: list[UserPreference] = []
        seen: set[str] = set()

        for text in user_texts:
            for rule in self.rules:
                for match in rule.pattern.finditer(text):
                    l0 = rule.extract(match) if rule.extract else match.group(0).strip()
                    if not l0 or l0 in seen:
                        continue
                    seen.add(l0)
                    results.append(UserPreference(
                        user_id=user_id,
                        category=rule.category,
                        l0=l0,
                        source_session=session_id,
                    ))

        return results

    @staticmethod
    def _extract_user_texts(messages: list[dict]) -> list[str]:
        texts: list[str] = []
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(part.get("text", ""))
        return texts
