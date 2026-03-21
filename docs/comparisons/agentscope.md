# Context GC 与 AgentScope 对比

> 基于 [AgentScope 官方仓库](https://github.com/modelscope/agentscope) 整理。AgentScope 为阿里巴巴/ModelScope 开源的多智能体开发平台，1.8 万+ stars。

---

## 一、AgentScope 概述

**AgentScope** 为完整多智能体框架：内置 memory、memory compression、数据库；ReAct 智能体、Actor 分布式。

### 1.1 核心机制

| 机制 | 说明 |
|------|------|
| **Memory** | 框架内建 memory 模块 |
| **Memory Compression** | 框架内回调 |
| **保留策略** | 由 Agent 与 memory 模块决定 |
| **持久化** | 数据库 + 可选持久化 |
| **架构** | ReAct 智能体、Actor 分布式 |

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| 增量摘要与分代 | 宿主 `close()` | 单轮摘要 + 分代打分 |
| 容量阈值合并 | token 达档位 | 低分代轮次合并 |
| Checkpoint | 每 N 轮 | 崩溃恢复 |
| 会话中偏好检测 | 每轮 `close()` | 零 LLM 规则匹配，写入时去重 |
| 会话结束 | `on_session_end()` | L0/L1/L2 持久化 + 蒸馏管道（偏好/经验/技能） |

- **形态**：纯库，零第三方依赖，模型无关，MemoryBackend 可插拔
- **可注入 AgentScope 的摘要/记忆回调**
- **检索**：FTS5/BM25 跨会话关键词搜索（无向量 DB）
- **生命周期**：TTL 老化 + `memory_inject_max_tokens` 注入容量控制

---

## 三、对比总览

| 维度 | AgentScope | Context GC |
|------|------------|------------|
| **定位** | 多智能体开发平台 | 对话上下文管理库 |
| **架构** | 完整框架（ReAct、Actor） | 纯库，宿主注入 |
| **压缩** | 框架内 memory compression | 分代 + 容量触发 |
| **持久化** | 数据库 + 可选 | Backend 协议可插拔 |
| **蒸馏** | 无内置蒸馏 | Task Agent → Distiller → 经验/技能 |

---

## 四、与 Context GC 核心差异

| 差异点 | AgentScope | Context GC |
|--------|------------|------------|
| **形态** | 框架，提供完整工作流 | 库，提供压缩与持久化能力 |
| **集成** | Context GC 可注入摘要/记忆回调 | 适配 AgentScope 的 memory 接口 |
| **蒸馏** | 框架多无内置蒸馏 | 完整蒸馏管道 |

---

## 五、参考资料

- [AgentScope 官方仓库](https://github.com/modelscope/agentscope)
- [Context GC 设计文档](../design/memory-system.md)
