# Context GC 与 Sirchmunk 对比

> 基于 [Sirchmunk 官方仓库](https://github.com/modelscope/sirchmunk) 整理。Sirchmunk 为 ModelScope 开源的 Agentic 检索系统，545+ stars，口号「Raw data to self-evolving intelligence, real-time」。

---

## 一、Sirchmunk 概述

**Sirchmunk** 是**无向量索引的智能检索系统**，直接对原始文件进行搜索，通过 Monte Carlo 证据采样与自演化知识聚类，实现「即放即搜」与「越查越准」。

### 1.1 解决的问题（vs 传统 RAG）

| 传统 RAG 痛点 | Sirchmunk 方案 |
|---------------|----------------|
| 高成本（VectorDB、文档解析、预索引） | 无基础设施，直接对原始数据检索 |
| 数据陈旧（批量重建索引） | 即时、动态，自演化索引反映实时变化 |
| 成本线性增长 | 低 RAM/CPU，原生弹性支持大规模数据 |
| 近似向量匹配 | 确定性与上下文感知，混合逻辑保证语义精度 |
| 复杂 ETL 流水线 | Drop-and-Search，零配置集成 |

### 1.2 核心能力

**无嵌入索引**：直接操作原始文件，不预计算向量，无信息损失。

**Monte Carlo 证据采样**：
1. **Phase 1 探索**：模糊锚定 + 分层随机采样，覆盖潜在相关区域
2. **Phase 2 利用**：以高分区为中心的高斯重要性采样，提取上下文并打分
3. **Phase 3 合成**：Top-K 片段送入 LLM，产出 ROI 摘要与置信度

**自演化知识聚类（Knowledge Cluster）**：
- 每次搜索产出可复用的 KnowledgeCluster（evidences、content、patterns、confidence、queries、hotness、embedding）
- 语义相似查询优先复用已有 Cluster，零 LLM 调用加速
- 复用时有查询历史、hotness、embedding 的增量更新，语义覆盖面持续扩大

**搜索模式**：
- **FAST**：贪心 2 级关键词级联 + 上下文采样，约 2 LLM 调用、2–5s
- **DEEP**：完整 Monte Carlo 采样，约 10–30s
- **FILENAME_ONLY**：纯文件名检索，无 LLM

**部署**：Python SDK、CLI、HTTP API、MCP Server、Web UI、Docker。

---

## 二、Context GC 的设计（当前实现）

| 流水线 | 触发 | 动作 |
|--------|------|------|
| 增量摘要与分代 | 宿主 `close()` 表示轮次结束 | 单轮摘要 + 历史轮次关联度打分与分代值更新 |
| 容量阈值合并 | token 达 20%/30%/40%… | 低分代轮次相邻合并，高分代保留 |
| Checkpoint | 每 N 轮 | 崩溃恢复 |
| 会话中偏好检测 | 每轮 `close()` | 零 LLM 规则匹配，写入时 exact/keyword_overlap 去重 |
| 会话结束 | `on_session_end()` | L0/L1/L2 持久化 + 蒸馏管道（偏好/经验/技能） |

- **分代**：前 50% 关联度高 → 老生代保留，后 50% → 新生代合并
- **形态**：纯 Python 库，宿主注入回调，MemoryBackend 可插拔
- **存储**：L0/L1/L2 + 偏好/经验/技能；FileBackend 或自定义后端

---

## 三、对比总览

| 维度 | Sirchmunk | Context GC |
|------|-----------|------------|
| **核心问题** | 文档/知识检索（RAG 替代） | 对话上下文压缩 |
| **输入** | 本地文件、目录 | 对话消息（user/assistant/tool） |
| **输出** | 检索结果、Knowledge Cluster | 送入 LLM 的压缩 messages |
| **索引** | 无预索引，即时检索 | 无索引，轮次摘要序列 |
| **压缩** | 无显式压缩，Monte Carlo 采样控制 token | 轮次摘要 + 容量触发合并 |
| **自演化** | Knowledge Cluster 复用与增量更新 | 分代值累积 + 蒸馏管道（经验/技能迭代更新） |
| **检索** | 关键词 + Monte Carlo + LLM 合成 | FTS5/BM25 跨会话关键词搜索（无向量 DB） |
| **持久化** | DuckDB + Parquet，Knowledge Cluster | L0/L1/L2 + 偏好/经验/技能，Backend 可插拔 |
| **架构** | 服务（HTTP/MCP）+ SDK + Web UI | 嵌入宿主进程的库 |
| **模型** | 需 LLM（关键词、合成）；可选 Embedding（Cluster 复用） | 宿主注入回调 |

---

## 四、设计差异要点

### 4.1 问题域不同

- **Sirchmunk**：解决「如何在大量文档中高效、精准检索？」—— 面向 Agentic RAG、知识问答、代码库搜索
- **Context GC**：解决「如何在有限上下文窗口内维持长对话？」—— 面向多轮对话、Agent 会话的上下文管理

### 4.2 Sirchmunk 的特点

- **无索引**：无需预建向量库，即放即搜
- **Monte Carlo 采样**：证据抽取作为采样问题，文档无关、token 高效
- **Knowledge Cluster**：检索结果可复用，相似查询零 LLM 加速
- **多模态入口**：SDK、CLI、API、MCP、Web UI

### 4.3 Context GC 的特点

- **轻量嵌入**：无服务，宿主 `push`/`close`/`get_messages` 即可
- **分代策略**：基于关联度的保留逻辑，与当前对话更相关的历史不易被合并
- **可插拔**：摘要、合并、关联度均由回调注入
- **无第三方依赖**：核心包仅标准库
- **蒸馏管道**：Task Agent → Distiller → 经验/技能双轨；偏好/经验写入时 keyword_overlap 去重
- **记忆生命周期**：TTL 老化淘汰 + `memory_inject_max_tokens` 注入容量控制

### 4.4 互补与组合

| 场景 | 更适合 |
|------|--------|
| Agent 需搜索本地文档、代码库、知识库 | Sirchmunk |
| 多轮对话需控制上下文长度 | Context GC |
| 无预索引、即时检索、自演化知识 | Sirchmunk |
| 对话轮次压缩、分代合并 | Context GC |
| RAG 替代、Agentic Search | Sirchmunk |
| 长对话上下文窗口管理 | Context GC |

**可组合**：Sirchmunk 负责文档检索与知识聚类，Context GC 负责对话轮次压缩；检索结果可作为「工具输出」传入 Context GC 的 `push()`，由宿主在 `generate_summary` 中决定如何压缩。

---

## 五、参考资料

- [Sirchmunk 官方仓库](https://github.com/modelscope/sirchmunk)
- [Sirchmunk 文档](https://modelscope.github.io/sirchmunk-web/)
- [Context GC 设计文档](../design/memory-system.md)
