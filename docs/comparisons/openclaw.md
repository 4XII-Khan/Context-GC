# Context GC 与 OpenClaw 对比

> 基于 [OpenClaw 官方仓库](https://github.com/openclaw/openclaw) 整理。OpenClaw 为开源个人 AI 助手，32 万+ stars，支持 OpenViking 插件。

---

## 一、OpenClaw 概述

**OpenClaw** 采用「文件范式」管理记忆：Session + Daily Notes（`memory/YYYY-MM-DD.md`）+ MEMORY.md，配合 memory_search（BM25+向量）检索。

### 1.1 核心机制

| 机制 | 说明 |
|------|------|
| **Session** | 当前会话上下文 |
| **Daily Notes** | 按日分文件的记忆 `memory/YYYY-MM-DD.md` |
| **MEMORY.md** | 汇总长期记忆 |
| **memory_search** | BM25 + 向量混合检索 |
| **memoryFlush** | 接近 compaction 时提醒 Agent 写入，由 Agent 自主决定写哪些到 MEMORY |

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
- **存储**：L0/L1/L2 + 偏好/经验/技能；偏好/经验写入时 keyword_overlap 去重
- **检索**：FTS5/BM25 跨会话关键词搜索（无向量 DB）
- **生命周期**：偏好/经验按 TTL 老化淘汰；`memory_inject_max_tokens` 注入容量控制

---

## 三、对比总览

| 维度 | OpenClaw | Context GC |
|------|----------|------------|
| **定位** | 个人 AI 助手，多平台接入 | 通用对话/Agent 上下文管理库 |
| **记忆范式** | 明文 Markdown，工作区内 | 结构化 L0/L1/L2 + 蒸馏产出 |
| **写入决策** | Agent 自主决定（memoryFlush） | 固定流水线：会话中规则 + 会话结束蒸馏 |
| **检索** | BM25 + 向量 | FTS5/BM25 关键词（无向量 DB） |
| **压缩触发** | 接近 compaction 时 flush | 轮次 `close()` + 容量档位 |

---

## 四、与 Context GC 核心差异

| 差异点 | OpenClaw | Context GC |
|--------|----------|------------|
| **灵活性** | Agent 自主决定写入哪些记忆，更灵活 | 蒸馏管道固定流水线，自主度低 |
| **记忆形态** | 文件范式，工作区内可见 | L0/L1/L2 分层，可配 Backend |
| **蒸馏** | 无内置蒸馏管道 | Task Agent → Distiller → 经验/技能双轨 |
| **可组合** | 支持 OpenViking 插件 | 可作记忆后端或与 OpenViking 配合 |
| **记忆生命周期** | 无 TTL 机制 | TTL 老化 + 注入容量上限 |

---

## 五、参考资料

- [OpenClaw 官方仓库](https://github.com/openclaw/openclaw)
- [OpenClaw 与 OpenViking 集成](https://github.com/volcengine/OpenViking#openclaw-context-plugin-details)
- [Context GC 设计文档](../design/memory-system.md)
