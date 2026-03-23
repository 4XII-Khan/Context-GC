"""
distillation/task_assignment_llm.py

经验落库前：用大模型读取用户 ``task_index.json`` 等价结构，将本批 ``task_desc``
归并到已有任务（reuse）或判定为新任务（new），避免仅靠 Jaccard 的误判。

与 ``FileBackend`` 配合：LLM 模式下写入/读取经验时使用 ``use_fuzzy_task_match=False``，
由本模块给出的 canonical 描述 + 精确匹配落目录。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

_log = logging.getLogger(__name__)

CallLLM = Callable[[str, list[dict], list[dict]], dict]

TASK_ASSIGNMENT_SYSTEM_PROMPT = """你是「用户经验任务」归并助手。

**语言（强制执行）**：JSON 内 ``canonical_desc`` 及任何说明性字符串须使用**简体中文**（reuse 时与已有条目的表述语言保持一致即可，一般为中文）。

你会收到两段信息：
1) **已有任务列表**：来自用户经验目录的 task_index（含 index、slug、canonical_desc、aliases）。
2) **本批待归并的任务描述**：模型刚从当前会话蒸馏出的任务说明，可能措辞与已有 canonical 不同但语义相同。

**归并依据（重要）**：只根据**本条 task_desc 本身描述的用户目标、交付物与操作对象**判断是否同一任务；**不要**因为「同一会话里还聊过别的话题」「描述里偶然出现相同动词（如查询、整理）」「与某历史任务共享泛词（如新闻、文件）」就 reuse。**宁可 new 多一个目录，也不要错桶。**

你的任务：为**每一个**本批描述（按 batch_index 序号）决定：
- **reuse**：**仅当**与某个已有任务属**同一业务领域、同一用户目标、同一交付物/功能线**（仅是表述不同）时，归入该任务经验目录；
- **new**：与所有已有任务在**领域、主题或目标**上**任一**明显不同，则**必须**新建经验任务，并给出一句简洁的 **简体中文** canonical_desc（可作文件夹标题，勿过长）。**禁止**为减少目录数量把**不同领域**的本批描述强行 ``reuse`` 到已有任务下（会导致多条无关经验混在同一任务桶内）。

## 正向示例（应 reuse）
- 已有 canonical：「用户登录功能测试用例设计」。本批：「为登录模块编写 5 条测试用例」→ **reuse**（同一目标，表述不同）。
- 已有：「销售业绩表统计与报表生成」。本批：「按考勤与预算核对销售数据并出报告」→ **reuse**（同一业务线：销售数据报告，细节扩展）。
- 已有：「伊朗相关新闻检索与多格式导出」。本批：「查询伊朗最新动态并整理成 Markdown/HTML」→ **reuse**（同一主题与交付形态）。

## 反向示例（禁止 reuse，必须 new）
- 已有：「伊朗新闻查询与多格式整理」。本批：「用户回忆几天前查过的伊朗新闻，通过 memory_search 未命中后改用 Shell 遍历目录找到演讲稿文件并读取主题」→ **new**，canonical 如「演讲稿主题查询与整理」或「工作区内演讲稿定位与内容提取」。**错误**：归入「伊朗新闻…」仅因会话或文本里出现「伊朗」「查询」。
- 已有：「会议纪要整理」。本批：「编写 Python 计算器脚本单元测试」→ **new**（领域完全不同）。
- 已有：「API 接口设计」。本批：「前端页面样式与响应式布局调试」→ **new**。
- 已有：「某功能测试用例设计」。本批：「从远程仓库拉代码并解决合并冲突」→ **new**。

## 自检（输出前在脑中完成，不要写出）
若把本批描述归到某 existing_index 后，用户单独打开该任务经验文件夹，是否会觉得「这条与文件夹标题几乎无关」？若是 → 改为 **new**。

硬性要求：
- 每个 batch_index 在输出中**恰好出现一次**。
- 只输出 **一个** JSON 对象，不要 Markdown、不要代码围栏、不要解释文字。
- JSON 格式如下（字段名固定）：
  {"assignments":[{"batch_index":1,"action":"reuse","existing_index":0},{"batch_index":2,"action":"new","canonical_desc":"..."}]}
- action 为 "reuse" 时必须含 existing_index（非负整数，对应已有任务列表里的 index 字段）。
- action 为 "new" 时必须含 canonical_desc（非空字符串）。
"""


def _extract_json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def assign_experience_task_descs_with_llm(
    unique_task_descs: list[str],
    task_index: list[dict],
    call_llm: CallLLM,
) -> dict[str, str]:
    """
    调用 LLM，将 ``unique_task_descs`` 中每条原始描述映射为用于存储的
    ``canonical_desc``（reuse 时与索引中某条 canonical_desc 一致）。

    失败或解析无效时返回恒等映射 ``{s: s for s in unique_task_descs}``。
    """
    if not unique_task_descs:
        return {}

    enumerated_existing = []
    for i, entry in enumerate(task_index):
        if not isinstance(entry, dict):
            continue
        row = {
            "index": i,
            "slug": entry.get("slug", ""),
            "canonical_desc": entry.get("canonical_desc", ""),
            "aliases": entry.get("alt_descs", []),
        }
        if entry.get("created_at"):
            row["created_at"] = entry["created_at"]
        if entry.get("updated_at"):
            row["updated_at"] = entry["updated_at"]
        enumerated_existing.append(row)

    batch_lines = "\n".join(
        f"{i + 1}. {desc}" for i, desc in enumerate(unique_task_descs)
    )
    user_content = (
        "## 已有任务（task_index）\n"
        + json.dumps(enumerated_existing, ensure_ascii=False, indent=2)
        + "\n\n## 本批待归并的任务描述（batch_index 为行首序号）\n"
        + batch_lines
    )

    try:
        resp = call_llm(
            TASK_ASSIGNMENT_SYSTEM_PROMPT,
            [{"role": "user", "content": user_content}],
            [],
        )
    except Exception as e:
        _log.warning("[TaskAssignLLM] call_llm failed: %s", e)
        return {s: s for s in unique_task_descs}

    content = ""
    if isinstance(resp, dict):
        content = (resp.get("content") or "").strip()
    parsed = _extract_json_object(content)
    if not parsed:
        _log.warning("[TaskAssignLLM] no JSON in model output")
        return {s: s for s in unique_task_descs}

    assignments = parsed.get("assignments")
    if not isinstance(assignments, list):
        _log.warning("[TaskAssignLLM] missing assignments array")
        return {s: s for s in unique_task_descs}

    n_existing = len(task_index)
    by_batch: dict[int, str] = {}

    for item in assignments:
        if not isinstance(item, dict):
            continue
        try:
            bi = int(item.get("batch_index", -1))
        except (TypeError, ValueError):
            continue
        if bi < 1 or bi > len(unique_task_descs):
            continue
        action = (item.get("action") or "").strip().lower()
        if action == "reuse":
            try:
                ei = int(item.get("existing_index", -1))
            except (TypeError, ValueError):
                continue
            if ei < 0 or ei >= n_existing:
                _log.warning(
                    "[TaskAssignLLM] invalid existing_index=%s (have %d entries)",
                    ei,
                    n_existing,
                )
                continue
            entry = task_index[ei]
            if not isinstance(entry, dict):
                continue
            canon = (entry.get("canonical_desc") or "").strip()
            if not canon:
                continue
            by_batch[bi] = canon
        elif action == "new":
            canon = (item.get("canonical_desc") or "").strip()
            if not canon:
                continue
            by_batch[bi] = canon

    result: dict[str, str] = {}
    for i, orig in enumerate(unique_task_descs):
        batch_idx = i + 1
        if batch_idx in by_batch:
            result[orig] = by_batch[batch_idx]
        else:
            result[orig] = orig

    expected_batches = set(range(1, len(unique_task_descs) + 1))
    if set(by_batch.keys()) != expected_batches:
        _log.warning(
            "[TaskAssignLLM] incomplete batch coverage: expected %s got %s",
            expected_batches,
            set(by_batch.keys()),
        )

    return result
