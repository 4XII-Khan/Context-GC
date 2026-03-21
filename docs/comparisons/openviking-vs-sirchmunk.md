# OpenViking 与 Sirchmunk 对比

> 两者均为面向 AI Agent 的上下文/检索系统，但设计范式与侧重点不同。基于官方仓库整理。

---

## 一、定位与问题域

| 维度 | OpenViking | Sirchmunk |
|------|------------|-----------|
| **定位** | Agent 上下文数据库 | 无向量索引的 Agentic 检索系统 |
| **口号** | 统一管理记忆、资源、技能 | Raw data to self-evolving intelligence, real-time |
| **核心问题** | 上下文碎片化、检索效果差、记忆迭代有限 | 传统 RAG 高成本、数据陈旧、复杂 ETL |
| **Stars** | 17k+ | 545+ |
| **来源** | 火山引擎 | ModelScope |

---

## 二、架构与存储

| 维度 | OpenViking | Sirchmunk |
|------|------------|-----------|
| **存储范式** | 虚拟文件系统（`viking://`） | 原始文件 + DuckDB/Parquet |
| **数据结构** | 目录树：resources / user / agent | 无预索引，直接对文件搜索 |
| **分层** | L0（~100 token 摘要）/ L1（~2k 概览）/ L2（全文按需） | 无分层，Monte Carlo 采样控制 token |
| **持久化** | 内置，viking 协议 URI | DuckDB 内存 + Parquet 落盘（Knowledge Cluster） |
| **部署** | 独立服务 `openviking-server` | 服务（HTTP/MCP）+ SDK + Web UI |

---

## 三、检索策略

| 维度 | OpenViking | Sirchmunk |
|------|------------|-----------|
| **索引** | 需 Embedding，向量检索 | 无预索引，ripgrep / ripgrep-all 即时检索 |
| **检索流程** | 意图分析 → 向量粗定位 → 目录内二次检索 → 递归下钻 | 关键词抽取 → 文件检索 → Monte Carlo 采样 → LLM 合成 |
| **精度** | 目录递归 + 语义，兼顾层级与语义 | 确定性与上下文感知，混合逻辑 |
| **速度** | 依赖向量检索与目录遍历 | FAST 模式 2–5s（2 LLM 调用），DEEP 10–30s |

---

## 四、自演化机制

| 维度 | OpenViking | Sirchmunk |
|------|------------|-----------|
| **演化对象** | User/Agent 记忆目录 | Knowledge Cluster |
| **触发** | 会话结束时记忆抽取 | 每次搜索产出/复用 Cluster |
| **复用** | 记忆跨会话加载 | 语义相似查询复用 Cluster，零 LLM 加速 |
| **更新** | 异步分析任务结果与用户反馈 | 查询历史、hotness、embedding 增量更新 |

---

## 五、模型依赖

| 维度 | OpenViking | Sirchmunk |
|------|------------|-----------|
| **VLM** | 必需（多模态理解） | 可选（支持纯文本） |
| **Embedding** | 必需（向量检索、分层摘要） | 可选（Cluster 复用加速） |
| **LLM** | 用于记忆抽取、语义处理 | 用于关键词、证据合成、Cluster 生成 |
| **模型接入** | volcengine / openai / litellm | OpenAI 兼容 API（含 MiniMax、DeepSeek 等） |

---

## 六、可观测与入口

| 维度 | OpenViking | Sirchmunk |
|------|------------|-----------|
| **检索轨迹** | 目录递归轨迹可视化 | 搜索日志、SSE 流式输出 |
| **CLI** | `ov`（Rust） | `sirchmunk`（Python） |
| **MCP** | 支持 | 支持（Claude Desktop、Cursor） |
| **Web UI** | 有 | 有（Next.js） |
| **Docker** | 支持 | 支持 |
| **OpenClaw** | 官方插件，效果显著 | 作为 skill 集成 |

---

## 七、适用场景对比

| 场景 | OpenViking | Sirchmunk |
|------|-------------|-----------|
| 记忆 + 资源 + 技能统一管理 | ✅ 强项 | ❌ 不覆盖 |
| 目录/层级化上下文 | ✅ 文件系统范式 | ❌ 扁平检索 |
| 即时文档检索、零索引 | ❌ 需写入与分层处理 | ✅ 即放即搜 |
| 多模态（图、视频） | ✅ VLM 支持 | ⚠️ 主要文本 |
| 相似查询加速（Cluster 复用） | ⚠️ 依赖记忆结构 | ✅ Knowledge Cluster |
| 大规模代码库/文档搜索 | ✅ 分层 + 递归 | ✅ Monte Carlo 采样 |
| 轻量、少依赖 | ❌ 需 VLM + Embedding | ⚠️ 需 LLM，Embedding 可选 |

---

## 八、设计哲学差异

**OpenViking**：以**文件系统范式**统一 Agent 的「记忆、资源、技能」，强调层级与结构化；检索先定位目录，再细化内容；适合需要长期记忆与资源管理的 Agent 框架。

**Sirchmunk**：以**无索引检索**替代传统 RAG，强调即时、动态；不预建向量，通过 Monte Carlo 采样与 Knowledge Cluster 实现自演化；适合文档/代码库的 Agentic 搜索与知识问答。

---

## 九、参考资料

- [OpenViking](https://github.com/volcengine/OpenViking) · [Sirchmunk](https://github.com/modelscope/sirchmunk)
- [Context GC 与 OpenViking 对比](./与OpenViking对比.md)
- [Context GC 与 Sirchmunk 对比](./与Sirchmunk对比.md)
