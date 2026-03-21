"""
distillation/task_prompt.py

Task Agent 提示词与输入格式化（复用 AsMe 设计）。
"""

from __future__ import annotations

from .models import TaskSchema

TASK_SYSTEM_PROMPT = """你是一个自主任务管理 Agent，负责分析对话以追踪和管理任务状态。

## 任务结构
- 任务包含：描述、状态、顺序（task_order=1, 2, ...）
- 消息通过 ID 关联到任务
- 状态：pending（待处理）| running（进行中）| success（成功）| failed（失败）

## 输入格式
- ## 当前已有任务：包含顺序、描述和状态的现有任务
- ## 先前进度：来自之前任务进度的上下文
- ## 已知用户偏好：此前已提交的用户偏好（如有）
- ## 当前消息（含 ID）：待分析的消息

## 工作流程

### 1. 识别规划
- 规划 = 用户/Agent 关于下一步做什么的讨论（非实际执行）
- 使用 append_messages_to_planning_section 捕获需求讨论

### 2. 创建/修改任务
- 任务 = 用户的完整意图/目标，可能跨多轮对话。用户的每个独立目标对应一个任务。
- 不要将单个用户请求拆成多个 Agent 规划的子步骤。
- 任务描述使用用户原话或贴近原意的转述。
- 与现有任务保持 MECE（相互独立、完全穷尽）。
- 当用户需求与现有任务描述冲突时，使用 update_task。
- 跟进/追问属于同一任务，不要另起新任务。

### 3. 将消息关联到任务
- 使用 append_messages_to_task，通过 message_id_range [start, end]
- 自动将任务状态设为 running
- 仅关联直接服务于该任务的消息

### 4. 记录进度（任务步骤）
- 使用 append_task_progress 记录 Agent 实际执行内容，写明具体数值和文件路径

### 5. 提交用户偏好
- 使用 submit_user_preference 提交与任务无关的用户事实
- 始终使用第三人称：「用户偏好 X」
- 先检查已知用户偏好，不要重复提交已列出的内容

### 6. 更新状态（关键）
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


def format_message_blob(msg: dict) -> str:
    """格式化单条消息为简洁表示。"""
    role = msg.get("role", "user")
    content = (msg.get("content") or "").strip()
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        content = " ".join(texts)
    tool_calls = msg.get("tool_calls") or []
    if tool_calls and isinstance(tool_calls, list):
        parts = [f"<{role}>(text) {content[:300]}"] if content else []
        for tc in tool_calls[:3]:
            if isinstance(tc, dict):
                name = tc.get("tool_name") or tc.get("name") or tc.get("function", {}).get("name", "")
                parts.append(f"<{role}>(tool-call) {name}")
        return " | ".join(parts) if parts else f"<{role}>(text) {content[:500]}"
    return f"<{role}>(text) {content[:500]}"


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
## 当前消息（含 ID）:
{msg_section}

请分析以上信息并确定要执行的操作。"""
