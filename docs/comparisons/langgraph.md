# Context GC 与 LangGraph 对比

> 基于 [LangGraph 文档](https://langchain-ai.github.io/langgraph/) 整理。LangGraph 为 LangChain Inc 开源的图式工作流框架，Apache 2.0。

---

## 一、LangGraph 概述

**LangGraph** 侧重工作流状态与断点续跑：Checkpoint 每节点后保存，thread_id 组织；short-term（thread 内）与 long-term（跨 thread）。

### 1.1 核心机制

| 机制 | 说明 |
|------|------|
| **Checkpoint** | 每节点执行后保存，粒度精细 |
| **组织** | thread_id 组织会话 |
| **短期/长期** | short-term（thread 内）与 long-term（跨 thread） |
| **保留策略** | 全图状态快照 |
| **持久化** | Postgres / Redis / Mongo |
| **蒸馏** | 无内置蒸馏管道 |

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| 增量摘要与分代 | 宿主 `close()` | 单轮摘要 + 分代打分（gen_score） |
| 容量阈值合并 | token 达档位（20%/30%/40%…） | 低分代轮次相邻合并 |
| Checkpoint | 每 N 轮 | 增量写入，崩溃恢复 |
| 会话中偏好检测 | 每轮 `close()` | 零 LLM 规则匹配，写入时去重 |
| 会话结束 | `on_session_end()` | L0/L1/L2 持久化 + 蒸馏管道（偏好/经验/技能） |

- **形态**：纯 Python 库，零第三方依赖，模型无关，MemoryBackend 可插拔
- **检索**：FTS5/BM25 跨会话关键词搜索（无向量 DB）
- **生命周期**：偏好/经验/技能按 TTL 老化淘汰；`memory_inject_max_tokens` 注入容量控制

---

## 三、对比总览

| 维度 | LangGraph | Context GC |
|------|-----------|------------|
| **定位** | 图式工作流、状态机 | 对话上下文压缩 + 持久化 + 蒸馏 |
| **Checkpoint** | 每节点后，粒度精细 | 每 N 轮，粒度较粗但足够对话场景 |
| **状态** | 全图状态快照 | 轮次摘要 + 分代值 + L0/L1/L2 |
| **持久化** | Postgres/Redis/Mongo | Backend 可插拔（FileBackend/SQLite/自定义） |
| **压缩** | 无内置对话压缩 | 分代 + 容量触发合并 |
| **蒸馏** | 无内置 | Task Agent → Distiller → 经验/技能 |
| **跨会话检索** | 依赖外部工具 | FTS5/BM25 内置（无向量 DB） |

---

## 四、与 Context GC 核心差异

| 差异点 | LangGraph | Context GC |
|--------|-----------|------------|
| **侧重** | 工作流状态、断点续跑 | 对话摘要、分代、蒸馏 |
| **Checkpoint 粒度** | 每节点，崩溃恢复天然完备 | 每 N 轮，粒度较粗但足够对话场景 |
| **对话压缩** | 无内置 | 分代 + 容量触发 + 增量摘要 |
| **可组合** | Context GC 可提供 `generate_summary` 等回调 | 与 LangGraph 的 memory 模块互补 |

---

## 五、参考资料

- [LangGraph 文档](https://langchain-ai.github.io/langgraph/)
- [Context GC 设计文档](../design/memory-system.md)
