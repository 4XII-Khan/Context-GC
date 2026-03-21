# Context GC 与 MemGPT 对比

> 基于 [MemGPT 官方仓库](https://github.com/mem0ai/memgpt) 整理。MemGPT 为 UC Berkeley 学术项目，后由 Letta 商业化（种子 1000 万刀）。

---

## 一、MemGPT 概述

**MemGPT** 采用「操作系统式」内存管理：分层虚拟内存（main/extended），通过函数调用管理进出。

### 1.1 核心机制

| 机制 | 说明 |
|------|------|
| **分层虚拟内存** | main（活跃上下文）与 extended（扩展存储） |
| **迁移** | 通过 LLM 自主调用函数管理层级迁移（push/pop） |
| **触发** | LLM 自主决定何时迁入/迁出 |
| **持久化** | 可配置（Letta Server 提供持久化 API） |
| **蒸馏** | 无内置蒸馏管道 |

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| 增量摘要与分代 | 宿主 `close()` | 单轮摘要 + 分代打分（gen_score） |
| 容量阈值合并 | token 达档位（20%/30%/40%…） | 低分代轮次相邻合并 |
| Checkpoint | 每 N 轮 | 增量写入，崩溃恢复 |
| 会话中偏好检测 | 每轮 `close()` | 零 LLM 规则匹配，写入时 exact/keyword_overlap 去重 |
| 会话结束 | `on_session_end()` | L0/L1/L2 持久化 + 蒸馏管道（Task Agent → Distiller → 经验/技能） |

- **形态**：纯 Python 库，零第三方依赖，模型无关
- **存储**：L0/L1/L2 分层 + 偏好/经验/技能；MemoryBackend 可插拔
- **检索**：FTS5/BM25 跨会话关键词搜索（无向量 DB）
- **生命周期**：偏好/经验/技能按 TTL 老化淘汰；`memory_inject_max_tokens` 注入容量控制

---

## 三、对比总览

| 维度 | MemGPT | Context GC |
|------|--------|------------|
| **定位** | 操作系统式内存管理 | 压缩 + 持久化 + 蒸馏（偏好/经验/技能）库 |
| **迁移策略** | LLM 自主调用，动态 | 规则驱动（分代 + 关联度），确定性更强 |
| **分层理念** | main vs extended | L0/L1/L2 分层 + gen_score 分代 |
| **蒸馏** | 无内置 | Task Agent → Distiller → 经验/技能双轨 |
| **持久化** | 可配置（Letta Server） | Backend 可插拔（FileBackend/SQLite/自定义） |
| **跨会话检索** | 通过 API 查询 | FTS5/BM25 关键词（无向量 DB） |
| **记忆生命周期** | 由 LLM 自主管理 | TTL 老化 + 注入容量上限 |
| **偏好去重** | 无内置 | exact / keyword_overlap |

---

## 四、与 Context GC 核心差异

| 差异点 | MemGPT | Context GC |
|--------|--------|------------|
| **迁移决策** | LLM 自主调用层级迁移，自适应能力更强 | 宿主 `close()` + 容量阈值，规则驱动、确定性高 |
| **理念对比** | main/extended 与分代合并理念类似 | 但 Context GC 额外提供蒸馏管道产出偏好/经验/技能 |
| **成本** | 每次迁移需 LLM 调用 | 偏好检测零 LLM，仅蒸馏阶段需 LLM |
| **可组合** | MemGPT 管理虚拟内存 | Context GC 做压缩 + 持久化 + 蒸馏，两者可组合 |

---

## 五、参考资料

- [MemGPT 官方仓库](https://github.com/mem0ai/memgpt)
- [Letta 平台](https://www.letta.com/)
- [Context GC 设计文档](../design/memory-system.md)
