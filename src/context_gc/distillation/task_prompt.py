"""
distillation/task_prompt.py

Task Agent 提示词与输入格式化（复用 AsMe 设计）。
"""

from __future__ import annotations

from .models import TaskSchema

TASK_SYSTEM_PROMPT = """你是一个自主任务管理 Agent，负责分析对话以追踪和管理任务状态。

**语言（强制执行）**：任务描述、进度（``append_task_progress``）、用户偏好（``submit_user_preference``）、思考报告（``report_thinking``）等所有写入任务结构与持久化的自然语言须为**简体中文**；代码、路径、标识符可保留原文。

## 任务结构
- 任务包含：描述、状态、顺序（task_order=1, 2, ...）
- **任务与会话的绑定粒度为「当前会话」**：后续蒸馏会对每个已结束任务使用**本会话全部消息**，不要求、也不需要你把消息精确关联到某个任务。
- 状态：pending（待处理）| running（进行中）| success（成功）| failed（失败）

## 输入格式
- ## 当前已有任务：包含顺序、描述和状态的现有任务
- ## 先前进度：来自之前任务进度的上下文
- ## 已知用户偏好：此前已提交的用户偏好（如有）
- ## 当前会话消息（序号仅便于指代顺序）：待分析的完整对话

## 工作流程

### 1. 识别规划
- 规划 = 用户/Agent 关于下一步做什么的讨论（非实际执行）
- 如需单独归档规划讨论，可使用 append_messages_to_planning_section（可选）

### 2. 创建/修改任务
- 任务 = 用户的完整意图/目标，可能跨多轮对话。用户的每个独立目标对应一个任务。
- **领域/主题不同的用户目标**（例如「做 PPT」与「造测试对话数据」）必须对应**不同任务**，勿合并为一个任务描述，以便后续蒸馏与经验、技能按主题正确分流。
- 不要将单个用户请求拆成多个 Agent 规划的子步骤。
- 任务描述使用用户原话或贴近原意的转述。
- 与现有任务保持 MECE（相互独立、完全穷尽）。
- 当用户需求与现有任务描述冲突时，使用 update_task。
- 跟进/追问属于同一任务，不要另起新任务。
- **不必**调用 append_messages_to_task；任务创建后即视为属于本会话，无需消息 ID 级关联。

### 3. 记录进度（任务步骤）
- 使用 append_task_progress 记录 Agent 实际执行内容，写明具体数值和文件路径

### 4. 提交用户偏好
- 使用 submit_user_preference 提交与任务无关的用户事实
- 始终使用第三人称：「用户偏好 X」
- 先检查已知用户偏好，不要重复提交已列出的内容

### 5. 更新状态（关键）
- pending：未开始
- running：已开始或失败后重启
- success：用户确认完成，或 Agent 无错误地进入下一任务
- failed：明确错误、用户放弃或用户报告失败
- **在调用 finish 前，必须对已完成的任务调用 update_task(task_order=N, task_status="success")**

## 规则
- 若需向已完成（success/failed）任务追加消息，先 update_task(status=running)。
- 非交互会话，自主执行。
- **一次可并发调用多个工具**以节省迭代轮次。

## 思考报告
在调用工具前，使用 report_thinking 简要回答：
1. 是否检测到规划？是否需要修改任务？
2. 现有任务与当前消息的关系？
3. 需要创建哪些新任务？
4. 需要提交哪些用户偏好？
5. 需要更新哪些任务状态？

在调用 finish 前，确认所有操作已完成。"""


def format_message_blob(msg: dict, *, max_text_chars: int = 8000) -> str:
    """
    格式化单条消息供 Task Agent 阅读。
    若存在 steps（如宿主 Agent 的工具轨迹），附加简要索引便于理解执行过程。
    """
    role = msg.get("role", "user")
    content = (msg.get("content") or "").strip()
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        content = " ".join(texts)
    if len(content) > max_text_chars:
        content = content[:max_text_chars] + f"\n…(共截断，原长约 {len(msg.get('content') or '')} 字)"

    tool_calls = msg.get("tool_calls") or []
    parts: list[str] = []
    if content:
        parts.append(f"<{role}>(text) {content}")

    if tool_calls and isinstance(tool_calls, list):
        for tc in tool_calls[:8]:
            if isinstance(tc, dict):
                name = tc.get("tool_name") or tc.get("name") or tc.get("function", {}).get("name", "")
                parts.append(f"<{role}>(tool-call) {name}")

    steps = msg.get("steps")
    if isinstance(steps, list) and steps:
        step_bits: list[str] = []
        for i, st in enumerate(steps[:20]):
            if not isinstance(st, dict):
                continue
            stype = st.get("type", "")
            label = st.get("label") or st.get("name") or ""
            step_bits.append(f"[{i}] {stype}:{label}"[:120])
        if step_bits:
            parts.append(f"<{role}>(steps) " + " | ".join(step_bits))
        if len(steps) > 20:
            parts.append(f"<{role}>(steps) …共{len(steps)}步")

    if parts:
        return " | ".join(parts)
    return f"<{role}>(empty)"


def pack_task_input(
    messages: list[dict],
    existing_tasks: list[TaskSchema] | None = None,
    previous_progress_num: int = 6,
) -> str:
    """将消息和已有任务打包为 Task Agent 的输入。"""
    tasks = existing_tasks or []

    task_section = "\n".join(f"- {t.to_string()}" for t in tasks) if tasks else "（暂无任务）"

    progresses: list[str] = []
    for t in reversed(tasks):
        for p in (t.data.progresses or [])[-previous_progress_num:]:
            progresses.append(f"任务 {t.order}: {p}")
    progress_section = "\n".join(progresses[-previous_progress_num:]) if progresses else "（暂无进度）"

    known_prefs: list[str] = []
    for t in tasks:
        known_prefs.extend(t.data.user_preferences or [])
    prefs_section = ""
    if known_prefs:
        prefs_section = "\n## 已知用户偏好:\n" + "\n".join(f"- {p}" for p in known_prefs)

    msg_section = "\n".join(
        f"<{i}> {format_message_blob(m)}" for i, m in enumerate(messages)
    )

    return f"""## 当前已有任务:
{task_section}

## 先前进度:
{progress_section}
{prefs_section}
## 当前会话消息（顺序序号）:
{msg_section}

请分析以上信息并确定要执行的操作。"""
