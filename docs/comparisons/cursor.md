# Context GC 与 Cursor 对比

> 基于 Cursor 产品特性整理。Cursor 为 Anysphere 出品的 AI 编程 IDE，估值 293 亿美元（2025）。

---

## 一、Cursor 概述

**Cursor** 侧重规则与即时上下文：Rules（`.cursor/rules`、`AGENTS.md`）、自动注入打开文件/终端/linter。

### 1.1 核心机制

| 机制 | 说明 |
|------|------|
| **Rules** | 项目级规则常驻，`.cursor/rules`、AGENTS.md |
| **上下文注入** | 自动注入当前打开文件、终端输出、linter 结果 |
| **压缩** | 无内置压缩 |
| **跨会话持久化** | 无内置，依赖 MCP 扩展（ContextForge、SuperLocalMemory） |

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| 增量摘要与分代 | 宿主 `close()` | 单轮摘要 + 分代打分 |
| 容量阈值合并 | token 达档位 | 低分代轮次合并 |
| Checkpoint | 每 N 轮 | 崩溃恢复 |
| 会话中偏好检测 | 每轮 `close()` | 零 LLM 规则匹配，写入时去重 |
| 会话结束 | `on_session_end()` | L0/L1/L2 持久化 + 蒸馏管道（偏好/经验/技能） |

- **形态**：纯 Python 库，零第三方依赖，模型无关，MemoryBackend 可插拔
- **可经 MCP 提供持久记忆与压缩**
- **检索**：FTS5/BM25 跨会话关键词搜索（无向量 DB）
- **生命周期**：TTL 老化 + `memory_inject_max_tokens` 注入容量控制

---

## 三、对比总览

| 维度 | Cursor | Context GC |
|------|--------|------------|
| **定位** | AI 编程 IDE，产品内建 | 通用上下文管理库，可嵌入 |
| **规则** | Rules 常驻，会话内上下文累积 | 无内置规则，宿主控制 |
| **压缩** | 无内置 | 分代 + 容量触发合并 |
| **持久化** | 依赖 MCP 扩展 | L0/L1/L2 + 偏好/经验/技能，Backend 可插拔 |
| **蒸馏** | 无内置 | Task Agent → Distiller → 经验/技能 |

---

## 四、与 Context GC 核心差异

| 差异点 | Cursor | Context GC |
|--------|--------|------------|
| **绑定** | 与 IDE/工具强绑定 | 嵌入宿主、模型无关 |
| **能力** | 规则与即时上下文 | 对话压缩、分代、蒸馏、持久化 |
| **集成** | Cursor 可经 MCP 接入 Context GC 提供持久记忆 | 库形态，可注入任意 Agent |

---

## 五、参考资料

- [Context GC 设计文档](../design/memory-system.md)
