# Context GC 与 Claude Code 上下文机制对比

> 基于 [Claude Code 官方文档](https://code.claude.com/docs/en/how-claude-code-works) 与 [Decode Claude 逆向分析](https://decodeclaude.com/claude-code-compaction/) 整理。

---

## 一、Claude Code 的压缩体系

Claude Code 采用**三层压缩**设计，而非单一摘要：

| 机制 | 触发 | 动作 |
|------|------|------|
| **Microcompaction** | 工具输出过大时 | 将大块工具结果落盘，仅保留引用；保留「热尾」窗口供模型继续推理 |
| **Auto-Compaction** | 剩余上下文低于预留 headroom | 执行结构化摘要 + 再水合（恢复近期文件、todos、continuation 指令） |
| **Manual Compaction** | 用户执行 `/compact` 或 `/compact Focus on X` | 在任务边界手动压缩，可带 focus hint 引导保留重点 |

### 1.1 Microcompaction（微压缩）

- **对象**：Read、Bash、Grep、Glob、WebSearch、WebFetch、Edit、Write 等工具的大输出
- **策略**：Hot tail（近期小窗保留在上下文中）+ Cold storage（其余落盘，按路径引用）
- **目的**：不把大段命令输出、文件内容长期占满上下文

### 1.2 Auto-Compaction（自动压缩）

- **触发条件**：可用上下文低于「输出 headroom + 压缩流程自身 headroom」
- **策略**：不压缩极小会话；按会话增长节奏周期性检查
- **摘要形态**：结构化「工作状态」，而非自由文本，要求包含：
  - 用户意图与变更
  - 关键技术决策与概念
  - 涉及文件及原因
  - 遇到的错误与修复方式
  - 待办与当前精确状态
  - 与最近意图一致的下一步

### 1.3 压缩后恢复（Rehydration）

压缩完成后重建上下文时，Claude Code 会：

1. 插入边界标记
2. 注入摘要消息（working state）
3. **重新读取近期访问文件**（如最近 5 个）
4. 恢复 todo / plan 状态
5. 注入 continuation 消息，要求「从上次中断处继续，不要追问用户」

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| **增量摘要与分代** | 宿主调用 `close()` 表示轮次结束 | 对当前轮产出摘要，对历史轮次做关联度打分与分代值更新 |
| **容量阈值合并** | token 占用达 20%、30%、40%… | 对低分代（≤0）轮次做相邻合并摘要，高分代保留 |
| **Checkpoint** | 每 N 轮 | 增量写入，进程崩溃后从断点恢复 |
| **会话中偏好检测** | 每轮 `close()` | 零 LLM 成本规则匹配，即时写入（含去重） |
| **会话结束持久化** | `on_session_end()` | L0/L1/L2 分层存储 + 蒸馏管道（Task Agent → Distiller → 经验/技能） |
| **偏好去重** | `save_user_preferences` | `exact` / `keyword_overlap` 写入前去重 |

### 2.1 分代策略

- **分代值**：每轮标注 `gen_score`，非物理拆为两段
- **更新规则**：按与当前对话的关联度排序，前 50% +1（老生代），后 50% -1（新生代），每次 ±1
- **压缩策略**：老生代保留，新生代参与合并

### 2.2 摘要形态

- 单轮摘要：结构化纯文本（主题、要点、结论）
- 合并摘要：仅输入已摘要的 summary，不含本轮原始消息；按梯度控制输出字数

### 2.3 跨会话记忆

- **L0/L1/L2**：会话结束持久化，L0 粗筛、L1 摘要列表、L2 原始对话按需加载
- **偏好**：会话中规则检测 + 蒸馏 Task Agent 抽取；写入时 `exact`/`keyword_overlap` 去重
- **经验与技能**：蒸馏管道写入，经验按任务去重（keyword_overlap），技能由 Skill Learner 更新
- **检索**：FTS5/BM25 跨会话关键词搜索（无向量 DB）
- **生命周期**：偏好/经验/技能按 TTL 老化淘汰；`memory_inject_max_tokens` 注入容量控制

---

## 三、对比总览

| 维度 | Claude Code | Context GC |
|------|-------------|------------|
| **定位** | 终端/IDE Agent，面向代码编辑、命令执行、工具调用 | 通用对话/Agent 上下文管理库，宿主注入回调 |
| **工具输出** | Microcompaction：大输出落盘，保留 hot tail | 无内置，由宿主在 `generate_summary` 输入中处理 |
| **摘要触发** | 剩余空间不足 + 手动 `/compact` | 轮次结束 + 容量档位（20%/30%/40%…） |
| **保留策略** | 结构化 working state + 重读近期文件 + todos | 分代值：老生代保留，新生代合并 |
| **关联度** | 未明确基于语义关联度；主要靠 focus hint 和 structured sections | 显式 `compute_relevance`，前 50% 老生代、后 50% 新生代 |
| **恢复机制** | 压缩后重读近期文件、恢复 todos、注入 continuation 指令 | 不重读文件，仅保留摘要序列，由宿主在 `get_messages` 时构建 |
| **手动控制** | `/compact`、`/compact Focus on X`、CLAUDE.md 中的 Compact Instructions | 宿主控制轮次边界（`close()`），无内置 focus hint |
| **跨会话记忆** | CLAUDE.md、Auto memory（前 200 行） | L0/L1/L2 分层持久化 + 蒸馏管道（偏好/经验/技能） |

---

## 四、设计差异要点

### 4.1 Claude Code 的优势

- **Microcompaction**：对大块工具输出做「落盘 + 引用」，减少上下文占用
- **文件再水合**：压缩后重读近期文件，保证模型仍能看到当前工作代码
- **结构化摘要**：checklist 式 working state，避免漏掉关键信息
- **Continuation 指令**：明确指示从上次中断处继续，减少重复提问

### 4.2 Context GC 的优势

- **分代标注**：基于关联度的保留策略，与当前对话更相关的历史不易被合并
- **轮次驱动**：宿主显式 `close()`，边界清晰，便于嵌入各类对话/Agent 架构
- **可插拔**：`generate_summary`、`merge_summary`、`compute_relevance` 由实现方注入，适配不同模型与业务
- **无工具依赖**：纯 Python、无第三方依赖，易集成

### 4.3 可借鉴方向

- **Microcompaction**：Context GC 宿主可将 tool 大输出在传入 `generate_summary` 前做 truncate 或「摘要 + 引用」，减轻上下文压力
- **Focus hint**：可在 `ContextGCOptions` 中增加可选 `compact_focus`，传入 `merge_summary` 的 prompt，引导合并时保留重点
- **文件再水合**：若宿主有文件上下文（如 Cursor @file），可在 `get_messages` 中注入近期打开文件的摘要或片段，与 Context GC 的摘要序列组合

---

## 五、参考资料

- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Inside Claude Code's Compaction System](https://decodeclaude.com/claude-code-compaction/)
- [How Claude remembers your project](https://code.claude.com/docs/en/memory)
- [Context GC 设计文档](../design/memory-system.md)
