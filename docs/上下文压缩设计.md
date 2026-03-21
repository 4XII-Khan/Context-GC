# Context GC 设计文档（上下文回收）

> 通用上下文管理方案，适用于任意基于 LLM 的对话/Agent 系统。
> 日期: 2026-03-20 | **实现语言**：Python

**角色约定**：**宿主**（Host）负责推送每轮消息并在轮次结束时显式告知，不参与摘要触发决策。**Context GC** 不推断轮次边界，由宿主通过 `close()` 显式通知；内部职责为：轮次结束 → 增量摘要与分代标注；容量达阈 → 触发合并摘要。**实现方**（Adapter）负责实现并注入 `generate_summary`、`merge_summary`、`compute_relevance`、`estimate_tokens` 等回调。

---

## 一、方案概述

**Context GC**（Context Garbage Collection）：在有限上下文窗口内，通过增量摘要、分代标注与容量阈值触发的合并摘要，实现长对话的可持续压缩与上下文管理。

注入 `MemoryBackend` 时，Context GC 还负责**会话存储**（L0/L1/L2 分层持久化与检索）、**偏好存储**（用户偏好）、**经验存储**（技能经验），详见 [记忆系统设计](./记忆系统设计.md)。

### 1.1 两条摘要流水线

存在**两条由 Context GC 内部自动触发的异步流水线**，宿主仅需在轮次结束时调用 `close()`，无需参与摘要决策：

| 流水线 | 触发条件 | 动作 |
|--------|----------|------|
| **增量摘要与分代标注** | 宿主调用 `close()` 告知轮次结束 | 对当前轮产出摘要，对历史轮次做关联度计算与分代值更新 |
| **容量阈值触发合并** | 上下文 token 占用达预设档位（20%、30%、40%…） | 对低分代轮次执行相邻合并摘要，高分代保留 |

两条流水线在 `close()` 内顺序执行；实现时需保证对 `state` 的并发访问安全（如 asyncio.Lock）。

---

## 二、每轮摘要与分代

### 2.1 执行时机

在**轮次结束 Safepoint** 执行，由宿主调用 `close()` 时触发。

| 轮次 | 输入 | 输出 | 分代 |
|------|------|------|------|
| 第 1 轮 | [user₁, assistant₁] | summary₁ | 不设置 |
| 第 2 轮 | summary₁ + [user₂, assistant₂] | summary₂ | 对全部 2 轮打分 |
| 第 3 轮 | summary₁, summary₂ + [user₃, assistant₃] | summary₃ | 对全部 3 轮打分 |
| 第 n 轮 | summary₁..summaryₙ₋₁ + [userₙ, assistantₙ] | summaryₙ | 对全部 n 轮打分 |

### 2.2 分代：标注语义而非物理拆分

分代采用**分代值标注**（`gen_score`），而非将数据物理拆分为「新生代 / 老生代」两个集合。

- **初始分代值**：0
- **每轮更新**：基于当前轮 user 消息，对历史摘要做关联度计算并排序
- **前 50%**（关联度高）→ 老生代 → 分代值 **+1**
- **后 50%**（关联度低）→ 新生代 → 分代值 **-1**
- **约束**：每轮更新时，分代值相对上一轮仅能 **±1**，保证平滑累积

### 2.3 分代值语义

| 分代值 | 语义 | 压缩策略 |
|--------|------|----------|
| > 0 | 老生代，与当前对话关联度高 | 容量触发时保留，不参与合并 |
| ≤ 0 | 新生代或中性，与当前对话关联度低 | 容量触发时参与相邻合并摘要 |

分代值随轮次累积：多次被判为老生代则持续 +1，多次被判为新生代则持续 -1。

### 2.4 关联度与排名

- **输入**：当前轮 user 消息 + 各历史轮摘要（当前轮固定 `gen_score=0`，不参与打分）
- **输出**：各历史轮与当前对话的关联度分数（由 `compute_relevance` 回调产出）
- **实现**：LLM 打分或 embedding 相似度，按分数降序排序
- **划分**：前 `n//2` 为老生代（+1），后 `n - n//2` 为新生代（-1）

### 2.5 摘要规范

#### 摘要如何进行

| 场景 | 输入 | 输出 | 实现 |
|------|------|------|------|
| **单轮摘要** | 历史摘要列表 + 本轮原始消息 [user, assistant, tool...] | 一条 summary 字符串 | `generate_summary(input)`，通常由 LLM 完成 |
| **合并摘要** | 相邻多轮的 summary 列表（**仅已摘要的**，不含本轮原始消息） | 一条合并后的 summary 字符串 | `merge_summary(group)`，可用 LLM 或简单拼接 |

单轮摘要时，LLM 需看到「此前已压缩的上下文 + 本轮完整对话」，产出覆盖本轮新增信息的摘要。**合并摘要**仅以已摘要的 summary 为输入，不包含本轮；处理完用合并结果**替换**原有多轮，而非追加，保证时序正确。

#### 摘要格式

摘要为**纯文本字符串**，建议采用结构化格式以便后续关联度计算与人工可读：

```
[主题] 简短主题（可选，1 句）
[要点] 用户诉求/问题；助手回复要点；工具调用与结果（若有）
[结论] 本轮达成的结论或待办（若有）
```

**简化格式**（实现可选用）：

```
主题：xxx。用户：xxx。助手：xxx。（工具：xxx。）结论：xxx。
```

- **长度**：单轮摘要建议 50–200 字（或 20–80 token），合并摘要可略长，但需控制总量
- **语言**：与对话语言一致（中文/英文）
- **编码**：UTF-8 纯文本，不含换行符或仅用空格分隔

#### 摘要准则

| 准则 | 说明 |
|------|------|
| **信息保全** | 保留用户意图、关键决策、工具调用结果、待办事项，不丢失影响后续推理的信息 |
| **去冗余** | 省略寒暄、重复表述、无关细节，合并同义表述 |
| **可关联** | 含主题词、实体名、领域术语，便于 `compute_relevance` 做语义匹配 |
| **中立客观** | 不注入主观评价，忠实于原始对话 |
| **长度可控** | 在信息保全前提下尽量压缩，为后续轮次留出上下文空间 |

**反例**：仅写「用户问了问题，助手回答了」——信息过少，无法支撑分代与续接；写成长篇复述——冗余过多，违背压缩目的。

#### 实现建议（Prompt 模板）

实现方实现 `generate_summary` 时，可将以下结构作为 system/user prompt 传入 LLM：

```
你是一个对话摘要助手。将以下对话压缩为一条摘要，要求：
1. 保留用户意图、关键决策、工具调用结果、待办事项
2. 去除寒暄和重复表述
3. 输出 50–200 字，格式：主题：xxx。用户：xxx。助手：xxx。结论：xxx。
4. 语言与输入一致

输入：
{历史摘要列表（若有）}
---
{本轮 user/assistant/tool 消息}
```

---

## 三、容量阈值触发合并

### 3.1 阈值与动作

| 容量占比 | 动作 |
|----------|------|
| 20%、30%、40%…（步长可配置） | 对 **分代值 ≤ 0** 的轮次执行相邻合并摘要；分代值 > 0 保留 |

**阈值可配置**：`capacity_threshold` 默认 0.1（10% 步长），`last_triggered_ratio` 初始 0.1 表示跳过 10% 档位，首次触发在 20%。每跨入更高档位触发一次，同一档位不重复触发。

### 3.1a 压缩梯度（按输入 token 控制合并输出）

合并摘要时，按**每组输入 token 总量**查梯度，控制输出字数，避免过度压缩或信息丢失：

| 梯度配置 | 说明 |
|----------|------|
| `merge_gradient_by_tokens` | `[(输入token上限, 压缩比或固定字数), ...]`，升序。压缩比 > 0 为比例，< 0 为固定字数（取绝对值） |
| **不合并** | 当梯度返回 `max_output_chars=None`（如输入 token < 500 时 param=0）时，该组跳过合并 |

**默认梯度示例**：

| 输入 token 上限 | 参数 | 含义 |
|-----------------|------|------|
| 500 | 0 | 不合并 |
| 2000 | 0.25 | 摘要最多输入的 25% |
| 5000 | 0.15 | 摘要最多输入的 15% |
| 15000 | 0.08 | 摘要最多输入的 8% |
| 999999 | -1500 | 固定 1500 字 |

每组 `input_tokens = sum(r.token_count for r in group)`，据此查梯度得到 `max_output_chars`，传入 `merge_summary(group, max_output_chars=...)`。

### 3.2 相邻合并规则

- **仅相邻轮可合并**：`round_id` 连续的轮次方可归为一组（如 round 5、6、7 可合并，5、7 不可）
- **分组依据**：`group_adjacent_by_round_id(low_score_rounds)`，将分代值 ≤ 0 的轮次按 `round_id` 连续性分组
- **示例**：round 1、2、3 均为 ≤ 0 且连续，合并为一段；round 4 为 > 0 保留；round 5、6 为 ≤ 0 且连续，再合并为一段

### 3.3 替换逻辑（非追加）

合并摘要采用**原地替换**而非追加，保证时序正确：

- **输入**：仅取已摘要的轮次（分代值 ≤ 0），不含本轮未完成的原始消息
- **替换规则**：对每组相邻低分代轮次，用合并后的**单一** `RoundMeta` 替换该组内全部项；合并项 `round_id = max(该组 round_id)`，`merged_round_ids` 记录原 id 列表
- **重建**：按 `round_id` 升序重建 `state.rounds`，高分代项原样保留，低分代组整体替换为合并项

---

## 四、数据结构

### 4.1 轮次元数据

```python
from dataclasses import dataclass, field

@dataclass
class RoundMeta:
    """单轮元数据"""
    round_id: int
    summary: str
    gen_score: int = 0          # 分代值，初始 0
    token_count: int = 0        # 估算 token 数
    is_merged: bool = False     # 是否为相邻合并后的轮次
    merged_round_ids: list[int] = field(default_factory=list)  # 合并时记录原 round_id
```

无 backend 时，原始消息不持久化，仅作摘要输入，摘要后即丢弃。注入 backend 且 `persist_l2=True` 时，Context GC 可将其持久化为 L2。

### 4.2 上下文状态

```python
@dataclass
class ContextGCState:
    """Context GC 维护的上下文状态"""
    rounds: list[RoundMeta]     # 按 round_id 升序
    total_tokens: int = 0       # 当前总 token
    max_tokens: int             # 容量上限（等于 max_input_tokens）
    capacity_threshold: float = 0.1  # 阈值步长，默认 10%
    last_triggered_ratio: float = 0.1  # 上次已处理的档位，初始 0.1 表示首次触发在 20%
```

### 4.3 估计与计数规则

| 项 | 职责 | 更新规则 |
|----|------|----------|
| `estimate_tokens` | 由实现方注入，与主模型 tokenizer 一致 | Options 必填，无默认 |
| `total_tokens` | `sum(r.token_count for r in state.rounds)` | 每次修改 `state.rounds` 后重算 |
| `RoundMeta.token_count` | 该轮 summary 的 token 数 | 单轮摘要时 `estimate_tokens(r.summary)`；合并后 `estimate_tokens(merged_summary)` |

合并后 `round_id` 不重排：合并项取该组内最大 id；新轮取 `max(现有 round_id) + 1`。`merged_round_ids` 保留原 id 用于审计。

---

## 五、服务详细逻辑

### 5.1 每轮流结束：_on_round_end_internal()

由 `close()` 在宿主告知轮次结束时调用。

```
_on_round_end_internal(round_messages, state, options)
  │
  ├─ 1. 生成本轮摘要
  │     if len(state.rounds) == 0:
  │       input = round_messages
  │     else:
  │       input = [r.summary for r in state.rounds] + round_messages
  │     new_summary = generate_summary(input)
  │
  ├─ 2. 第一轮：仅追加，不分代
  │     if len(state.rounds) == 0:
  │       state.rounds.append(RoundMeta(round_id=1, summary=new_summary, gen_score=0, ...))
  │       return
  │
  ├─ 3. 第二轮及以后：追加新轮，再对**历史轮次**分代（不打当前轮）
  │     next_id = max(r.round_id for r in state.rounds) + 1   # 不重排，取最大 id 递增
  │     new_round = RoundMeta(round_id=next_id, summary=new_summary, gen_score=0, ...)
  │     state.rounds.append(new_round)
  │     # 当前轮固定 gen_score=0，不参与分代打分
  │
  │     current_user = round_messages 中最后一条 user 的 content
  │     prev_rounds = state.rounds[:-1]   # 仅之前已摘要的轮次
  │     prev_summaries = [r.summary for r in prev_rounds]
  │     relevance_scores = compute_relevance(current_user, prev_summaries)
  │     ranked_indices = argsort(relevance_scores, descending=True)  # 关联度高到低
  │
  │     # 前 50% 老生代 +1，后 50% 新生代 -1，每次只能 ±1（仅更新 prev_rounds）
  │     n = len(prev_rounds)
  │     for i, idx in enumerate(ranked_indices):
  │       old_score = prev_rounds[idx].gen_score
  │       if i < n // 2:   # 前 50%，与当前更相关
  │         prev_rounds[idx].gen_score = old_score + 1
  │       else:            # 后 50%
  │         prev_rounds[idx].gen_score = old_score - 1
  │
  └─ 4. 更新 total_tokens
  │     # total_tokens = sum(r.token_count for r in state.rounds)
  │     # 每轮 token_count = estimate_tokens(r.summary)
```

### 5.2 分代值更新约束

```python
def update_gen_score(prev_score: int, is_old_gen: bool) -> int:
    """每次只能 ±1"""
    if is_old_gen:
        return prev_score + 1
    else:
        return prev_score - 1
```

### 5.3 容量阈值检查：_check_capacity_and_compact()

```
_check_capacity_and_compact(state, options)   # async，内部调用 merge_summary
  │
  ├─ 1. 计算当前容量占比
  │     ratio = state.total_tokens / state.max_tokens
  │     triggered = floor(ratio / capacity_threshold) * capacity_threshold
  │     if triggered <= state.last_triggered_ratio or triggered == 0: return
  │
  ├─ 2. 筛选低分代轮次
  │     low_score = [r for r in state.rounds if r.gen_score <= 0]
  │     high_score = {r.round_id: r for r in state.rounds if r.gen_score > 0}
  │     if not low_score: state.last_triggered_ratio = triggered; return
  │
  ├─ 3. 相邻分组，按梯度计算 max_output_chars
  │     groups = group_adjacent_by_round_id(low_score)
  │     for g in groups:
  │       input_tokens = sum(r.token_count for r in g)
  │       max_output_chars = get_max_output_chars(input_tokens, gradient)
  │       if max_output_chars is None: skip  # 梯度规定不合并
  │       merged_text = await merge_summary(g, max_output_chars=max_output_chars)
  │       merged_items.append(RoundMeta(round_id=max(...), summary=merged_text, ...))
  │
  ├─ 4. 重建 state.rounds（高分代保留，低分代组替换为合并项）
  │     new_rounds = 按 round_id 升序，high_score 原样，merged 替换对应组
  │     state.rounds = new_rounds
  │
  └─ 5. 更新 total_tokens、last_triggered_ratio
```

### 5.4 合并摘要与相邻分组

`merge_summary(group: list[RoundMeta], *, max_output_chars: int | None = None) -> str`：将一组**已摘要**轮次的 summary 合并为一条摘要。**输入仅 summary 列表，不含本轮原始消息**。`max_output_chars` 由梯度根据 `sum(r.token_count for r in group)` 计算，用于控制输出字数。实现方式可选：

- **LLM 合并**：将多段 summary 作为输入，在 prompt 中约束「输出不超过 X 字」，产出去冗余的合并摘要（推荐）
- **简单拼接**：`" | ".join(r.summary for r in group)`，适用于低延迟场景，但冗余较多

```python
def group_adjacent_by_round_id(rounds: list[RoundMeta]) -> list[list[RoundMeta]]:
    """将 round_id 连续的轮次分为一组，仅相邻可合并"""
    if not rounds:
        return []
    sorted_rounds = sorted(rounds, key=lambda r: r.round_id)
    groups = [[sorted_rounds[0]]]
    for r in sorted_rounds[1:]:
        if r.round_id == groups[-1][-1].round_id + 1:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups
```

### 5.5 build_messages_from_state 规范

将 `state.rounds` 转为送入 LLM 的 messages：每轮摘要作为一条 `role='user'` 消息，格式 `[Round N] {summary}`，按 `round_id` 升序。实现方可注入回调覆盖此格式。

```python
def build_messages_from_state(state: ContextGCState) -> list[dict]:
    return [
        {"role": "user", "content": f"[Round {r.round_id}] {r.summary}"}
        for r in sorted(state.rounds, key=lambda r: r.round_id)
    ]
```

### 5.6 内部逻辑：get_messages(current_messages) / _build_and_compact()

宿主调用 `get_messages(current_messages)` 时执行，**执行顺序**：先 compact 再 build，最后截断。

```
get_messages(current_messages) / _build_and_compact(current_messages)
  │
  ├─ 1. 检查容量，必要时触发二次摘要（先改 state）
  │     await _check_capacity_and_compact(state, options)
  │
  ├─ 2. 将 state 转为送入 LLM 的 messages（基于 compact 后的 state）
  │     result = build_messages_from_state(state)
  │
  ├─ 3. 追加当前轮 current_messages（宿主传入，如新 user 消息等）
  │     result += current_messages
  │
  └─ 4. 若超限，则只对本轮做摘要（不将历史传入摘要），摘要追加到历史后
  │     effective_limit = max_input_tokens - reserve_for_output
  │     if estimate_tokens(result) > effective_limit:
  │       summary = await generate_summary(current_messages)
  │       result = build_messages_from_state(state) + [{"role": "user", "content": summary}]
  │       if estimate_tokens(result) > effective_limit:
  │         summary = truncate_to_fit(summary, effective_limit - estimate_tokens(build_messages_from_state(state)))
  │         result = build_messages_from_state(state) + [{"role": "user", "content": summary}]
```

原则：state 先收敛再 build；超限时不丢弃历史，仅对本轮做摘要，若仍超限则截断 summary。

### 5.7 边界情况

| 边界 | 决策 |
|------|------|
| **多模态 content** | `compute_relevance` 的 `current_user` 仅传文本：若为 list（如 `[{type:"text", text:"..."}]`），提取首个 text；否则 `str(content)`。 |
| **奇数轮 50% 划分** | 前 `n // 2` 个 +1，后 `n - n // 2` 个 -1，保证老生代不多于新生代。 |
| **100% 容量** | 与 80% 档位相同逻辑；compact 后仍超限则步骤 4 持续截断直至满足 `effective_limit`。 |
| **rounds 为空** | `get_messages` 时直接 `result = list(current_messages)`，跳过 build 与截断。 |
| **truncate_to_fit** | 按 token 截断 summary 直至 `estimate_tokens(result) <= effective_limit`，可用二分或逐字截断。 |

---

## 六、调用接口

### 6.1 接口定义

宿主**推送消息**，轮次结束时调用 **`close()`** 告知结束，Context GC 不自行判断。

```python
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable

@dataclass
class ContextGCOptions:
    max_input_tokens: int
    capacity_threshold: float = 0.1      # 10% 步长，超过 20% 后每 10% 触发
    reserve_for_output: int = 4096
    merge_gradient_by_tokens: list[tuple[int, float|int]] | None = None  # 压缩梯度，默认见实现
    generate_summary: Callable[..., Awaitable[str]]   # (messages, *, max_output_chars) -> str
    merge_summary: Callable[..., Awaitable[str]]      # (group, *, max_output_chars) -> str
    compute_relevance: Callable[[str, list[str]], Awaitable[list[float]]]
    estimate_tokens: Callable[[object], int]

class ContextGC:
    def __init__(self, options: ContextGCOptions):
        self.options = options
        self.state = ContextGCState(...)
        self._buffer: list[dict] = []  # 当前轮缓冲

    def push(self, message: dict | list[dict]) -> None:
        """宿主推送消息，可单条或批量。"""
        msgs = message if isinstance(message, list) else [message]
        self._buffer.extend(msgs)

    async def close(self) -> None:
        """宿主在每轮对话结束后调用，表示该轮结束。Context GC 对缓冲做摘要 + 分代，并检查容量阈值。"""
        if self._buffer:
            await self._on_round_end_internal(list(self._buffer))
            self._buffer.clear()
        await self._check_capacity_and_compact()

    async def get_messages(self, current_messages: list[dict]) -> list[dict]:
        """宿主调用 LLM 前获取。传入当前轮消息（如新 user 消息），返回送入 LLM 的完整 messages。"""
        return await self._build_and_compact(current_messages)
```

### 6.2 宿主调用方式

| 宿主动作 | 方法 | 说明 |
|----------|------|------|
| 推送消息 | `push(message)` / `push(messages)` | 推送单条或批量消息 |
| 轮次结束 | `close()` | 宿主告知该轮结束，触发摘要 + 分代 |
| 获取上下文 | `get_messages(current_messages)` | 调用 LLM 前传入当前消息，获取完整 context |

**宿主调用 `close()` 即表示轮次结束**，Context GC 不自行判断。

### 6.3 宿主调用流程示例

```
# 第 1 轮
gc.push([{"role": "user", "content": "..."}])
messages = await gc.get_messages([{"role": "user", "content": "..."}])
response = await main_llm.chat(messages)
gc.push({"role": "assistant", "content": "..."})
await gc.close()  # 宿主告知第 1 轮结束

# 第 2 轮
gc.push({"role": "user", "content": "..."})
messages = await gc.get_messages([{"role": "user", "content": "..."}])
response = await main_llm.chat(messages)
gc.push({"role": "assistant", "content": "..."})
await gc.close()  # 宿主告知第 2 轮结束
```

---

## 七、配置

### 7.1 通用配置

```yaml
context_gc:
  max_input_tokens: 5000     # 上下文容量上限
  capacity_threshold: 0.1    # 10% 步长，20%/30%/40%… 触发
  reserve_for_output: 4096
  # generate_summary, merge_summary, compute_relevance, estimate_tokens 由实现方注入
```

### 7.2 摘要大模型（OpenRouter 兼容）

摘要与合并摘要使用独立大模型，通过 OpenRouter 兼容 API 调用。环境变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `CONTEXT_GC_BASE_URL` | API 基址（OpenRouter 兼容） | `https://mgallery.haier.net/v1` |
| `CONTEXT_GC_API_KEY` | API Key | 从 `.env` 读取（复制 `.env.example` 配置），**禁止硬编码** |
| `CONTEXT_GC_MODEL` | 模型名 | `Qwen3.5-35B-A3B` |

**实现示例**（Python，使用 `openai` 兼容客户端）：

```python
import os
from openai import AsyncOpenAI

_client = AsyncOpenAI(
    base_url=os.environ.get("CONTEXT_GC_BASE_URL", "https://openrouter.ai/api/v1"),
    api_key=os.environ.get("CONTEXT_GC_API_KEY", ""),
)

async def generate_summary(messages: list) -> str:
    """单轮摘要：历史摘要 + 本轮消息 -> 一条 summary"""
    model = os.environ.get("CONTEXT_GC_MODEL", "Qwen3.5-35B-A3B")
    # 将 messages 转为 OpenAI 格式 [{"role":"user","content":"..."}]
    resp = await _client.chat.completions.create(
        model=model,
        messages=_to_openai_messages(messages),
        max_tokens=256,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

async def merge_summary(group: list, *, max_output_chars: int | None = None) -> str:
    """合并摘要：多段 summary -> 一条合并摘要。max_output_chars 由梯度计算，用于约束输出字数。"""
    model = os.environ.get("CONTEXT_GC_MODEL", "Qwen3.5-35B-A3B")
    content = "\n---\n".join(r.summary for r in group)
    constraint = f"输出不超过 {max_output_chars} 字。" if max_output_chars else ""
    resp = await _client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"将以下多段摘要合并为一条，去冗余、保留关键信息。{constraint}\n\n{content}"}],
        max_tokens=512,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()
```

---

## 八、与旧方案对比

| 维度 | 旧方案 | 新方案 |
|------|--------|--------|
| 摘要触发 | 超限时 | 每轮增量摘要 |
| 分代 | 无 / 简单 Young-Old | 分代值标注，±1 累积 |
| 新老定义 | 时间远近 | 基于关联度排名，前 50% 老生代、后 50% 新生代 |
| 合并触发 | 超限时一次性 | 容量 20%、30%、40%… 档位递增触发 |
| 合并规则 | 无 | 仅相邻轮次可合并，按梯度控制输出字数 |

---

## 九、实现清单

| 操作 | 路径 | 说明 |
|------|------|------|
| 新增 | `src/context_gc/state.py` | RoundMeta, ContextGCState |
| 新增 | `src/context_gc/generational.py` | 分代打分、关联度计算 |
| 新增 | `src/context_gc/compaction.py` | 容量阈值、相邻合并、压缩梯度 |
| 新增 | `src/context_gc/context_gc.py` | ContextGC 主类 |
| 实现方 | `generate_summary` / `merge_summary` 等回调 | 遵循 2.5 摘要格式与准则；宿主仅负责 push/close/get_messages |

