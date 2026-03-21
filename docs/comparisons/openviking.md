# Context GC 与 OpenViking 对比

> 基于 [OpenViking 官方仓库](https://github.com/volcengine/OpenViking) 整理。OpenViking 为火山引擎开源的 Agent 上下文数据库，17k+ stars。

---

## 一、OpenViking 概述

**OpenViking** 是面向 AI Agent 的**上下文数据库**，采用「文件系统范式」统一管理 Agent 所需的记忆、资源与技能。

### 1.1 解决的问题

| 痛点 | OpenViking 方案 |
|------|-----------------|
| 上下文碎片化 | 统一为虚拟文件系统，`viking://` 协议 |
| 上下文需求暴涨 | L0/L1/L2 分层加载，按需取用，降低 token 消耗 |
| 检索效果差 | 目录递归检索，先定位高分区，再细化探索 |
| 不可观测 | 可视化检索轨迹，可追溯根因 |
| 记忆迭代有限 | 自动压缩会话、抽取长期记忆，越用越聪明 |

### 1.2 核心概念

**虚拟文件系统**：

```
viking://
├── resources/     # 资源：项目文档、仓库、网页等
├── user/          # 用户：偏好、习惯等
└── agent/         # Agent：技能、指令、任务记忆
```

**分层上下文（L0/L1/L2）**：

| 层级 | 内容 | 约 token | 用途 |
|------|------|----------|------|
| L0 | 一句话摘要 | ~100 | 快速检索与定位 |
| L1 | 核心信息与使用场景 | ~2k | 规划阶段决策 |
| L2 | 完整原始数据 | 全文 | 深度阅读时按需加载 |

**检索策略**：意图分析 → 向量粗定位 → 目录内二次检索 → 递归下钻 → 结果聚合。

**会话管理**：会话结束时触发记忆抽取，异步分析任务结果与用户反馈，更新 User/Agent 记忆目录，实现自演化。

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| 增量摘要与分代 | 宿主 `close()` 表示轮次结束 | 单轮摘要 + 历史轮次关联度打分与分代值更新 |
| 容量阈值合并 | token 达 20%/30%/40%… | 低分代轮次相邻合并，高分代保留 |
| Checkpoint | 每 N 轮 | 崩溃恢复 |
| 会话中偏好检测 | 每轮 `close()` | 零 LLM 规则匹配，写入时去重 |
| 会话结束 | `on_session_end()` | L0/L1/L2 持久化 + 蒸馏管道（Task Agent → Distiller → 经验/技能） |

- **分代**：前 50% 关联度高 → 老生代保留，后 50% → 新生代合并
- **形态**：纯 Python 库，宿主注入回调，MemoryBackend 可插拔
- **存储**：L0/L1/L2 分层 + 偏好/经验/技能；FileBackend 或自定义后端

---

## 三、对比总览

| 维度 | OpenViking | Context GC |
|------|------------|------------|
| **定位** | Agent 上下文数据库 | 对话上下文压缩库 |
| **架构** | 独立服务 + Python 客户端 | 嵌入宿主进程的库 |
| **存储** | 虚拟文件系统（viking://）、持久化 | L0/L1/L2 分层 + 偏好/经验/技能；Backend 可插拔 |
| **上下文类型** | 记忆 + 资源 + 技能 统一管理 | 对话摘要序列 + 偏好/经验/技能 |
| **分层** | L0/L1/L2 三层，按需加载 | L0/L1/L2 分层 + 轮次分代值（gen_score） |
| **检索** | 目录递归 + 语义搜索 | FTS5/BM25 关键词跨会话搜索（无向量 DB） |
| **压缩** | 会话级记忆抽取、长期记忆更新 | 轮次摘要 + 容量触发合并 |
| **保留策略** | L0/L1 常驻可检索，L2 按需 | 分代值：老生代保留，新生代合并 |
| **模型依赖** | 需 VLM + Embedding 模型 | 宿主注入回调，无内置模型 |
| **部署** | `openviking-server` 独立运行 | `pip install` 后直接 import |
| **目标场景** | OpenClaw 等 Agent 框架 | 任意 LLM 对话/Agent 系统 |

---

## 四、设计差异要点

### 4.1 OpenViking 的特点

- **文件系统范式**：记忆、资源、技能统一为目录结构，支持 `ls`、`find` 等操作
- **分层加载**：L0/L1 先筛，L2 按需加载，显著降低 token 消耗
- **目录递归检索**：先锁定高分区，再细化探索，兼顾语义与层级
- **可观测**：检索轨迹可视化，便于调试与优化
- **自演化**：会话结束后抽取长期记忆，User/Agent 记忆持续更新

### 4.2 Context GC 的特点

- **轻量嵌入**：无独立服务，Backend 可插拔，宿主 `push`/`close`/`get_messages` 即可
- **分代策略**：基于关联度的保留逻辑，与当前对话更相关的历史不易被合并
- **可插拔**：摘要、合并、关联度、token 估算均由回调注入，适配不同模型与业务
- **无第三方依赖**：核心包仅用标准库
- **蒸馏管道**：Task Agent → Distiller → 经验/技能双轨输出；偏好/经验写入时 keyword_overlap 去重
- **记忆生命周期**：偏好/经验按 TTL 老化淘汰；`memory_inject_max_tokens` 注入容量控制

### 4.3 互补关系

| 场景 | 更适合 |
|------|--------|
| Agent 需持久化记忆、管理资源与技能 | OpenViking |
| 会话内上下文压缩 + 跨会话持久化与蒸馏 | Context GC |
| 需要 L0/L1/L2 分层、目录检索 | OpenViking |
| 轻量嵌入、不部署服务 | Context GC |
| 需可视化检索轨迹、可观测 | OpenViking |
| 纯对话轮次压缩、分代合并 | Context GC |

**可组合**：OpenViking 管理长期记忆与资源，Context GC 负责会话内轮次压缩与蒸馏产出；两者可协同使用。

---

## 五、参考资料

- [OpenViking 官方仓库](https://github.com/volcengine/OpenViking)
- [OpenViking 文档](https://openviking.ai)
- [OpenClaw 与 OpenViking 集成](https://github.com/volcengine/OpenViking#openclaw-context-plugin-details)
- [Context GC 设计文档](../design/memory-system.md)
