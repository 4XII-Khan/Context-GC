<div align="center">

<img src="assets/logo.png" alt="Context GC" width="120" style="border-radius: 50%;">

# Context GC

**LLM Agent 的智能上下文管理方案**

*压缩 · 持久化 · 蒸馏 · 注入 — 面向生产环境的完整上下文生命周期*

<br>

[![Release](https://img.shields.io/badge/release-v0.1.0-green.svg)](https://github.com/4XII-Khan/Context-GC/releases)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-52%2F53%20e2e-brightgreen.svg)](tests/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-orange.svg)](#)

<br>

<code>Python</code> · <code>AsyncIO</code> · <code>模型无关</code> · <code>零依赖</code> · <code>后端可插拔</code>

<br>

[设计文档](docs/design/memory-system.md) · [快速开始](#快速开始) · [核心能力](#核心能力) · [评测](#100-轮测试与评估) · [文档](#设计文档)

<br>

**分代压缩** · **L0/L1/L2 分层记忆** · **蒸馏管道** · **崩溃恢复** · **技能学习**

<br>

[English](README.md) · [中文](README.zh-CN.md)

</div>

---

**Context GC** 把上下文当作可回收资源：按相关性保留、按老化度代谢、按价值沉淀。让 AI 的记忆可延续、更懂你，而不是更长。

纯库形态、模型无关的对话上下文管理方案，适用于基于 LLM 的对话 / Agent 系统。会话内通过分代标注与容量触发合并实现可持续压缩；会话结束时将摘要映射为 L0/L1/L2 三层持久化，并通过蒸馏管道提取用户偏好、经验与私有化技能，形成"压缩 → 持久化 → 蒸馏 → 注入"的完整闭环。

## 核心能力

**有选择的记忆，才有真正的智能。** 无选择的记忆只是存储；有选择的记忆才接近「理解」。Context GC 在回答：什么样的记忆机制，能让 Agent 更像在理解，而不是在背诵？答案是：能分清重要与次要、能随时间代谢与沉淀、能在新会话中延续和注入的记忆。不是为了更长，而是为了更有结构、更有延续性。

### 1. 会话内压缩

**从「截断」到「代谢」。** 人类记忆是代谢式的：重要的事被记住，琐碎的会被压缩或遗忘，但遗忘不等于消失——它们塑造了直觉和习惯。LLM 原先只有两种模式：要么全部记住，要么一刀截断。Context GC 引入第三种：代谢。低相关性的轮次不是被丢弃，而是被合并为更精炼的摘要；被压缩的是形式，延续的是认知。对话的语境是流动的（A → B → 回到 A）；均匀处理每一轮，等于忽视这种流动。分代打分保留「当前最需要的」，沉淀「长期仍有价值的」。*遗忘是选择，而不是损失。*

在固定上下文窗口内支撑长对话，通过**分代垃圾回收**：按关联度语义打分、保留高价值轮次、合并低价值历史。

| 能力 | 说明 |
| ---- | ---- |
| **增量摘要** | 每轮产出结构化摘要（主题、要点、结论）；输入 = 历史摘要 + 本轮消息 |
| **分代打分（`gen_score`）** | 每轮关联度排序：前 50% → 老生代（+1），后 50% → 新生代（−1）；每轮 ±1 限制，平滑衰减 |
| **容量阈值触发合并** | Token 占用达可配置档位（20%/30%/40%…）时，低 `gen_score` 轮次相邻合并；高分代保留 |
| **步进式打分** | 每隔 N 轮打一次分，中间轮次沿用上次 `gen_score` — 降低 LLM 调用频率 |
| **自动流水线** | 摘要与合并在 `close()` 内执行；宿主仅推送消息并在每轮调用 `close()` |

### 2. 会话级记忆持久化

**从「消费」到「培育」。** 原始上下文像一次性燃料：用完就丢。Context GC 把它变成可培育的土壤：**压缩**把当下对话收束成可复用的结构，**持久化**放进 L0/L1/L2 让历史可检索、可回溯，**蒸馏**从会话中提炼偏好、经验和技能，**注入**让新会话一开始就带着这些沉淀。每一次对话都在为后续对话积累养分，而不是单纯消耗 token。知识是可累积的，Agent 随用户一起成长。不丢弃，而是分层复用。

会话结束时将对话状态持久化为**三层检索结构**；支持跨会话检索，无需向量数据库。

| 能力 | 说明 |
| ---- | ---- |
| **L0 / L1 / L2 分层存储** | **L0**（~50–200 tokens）：L1 的粗筛摘要；**L1**：完整 GC 摘要列表，用于详细导航；**L2**：原始对话（按需加载） |
| **Checkpoint 与崩溃恢复** | 每 N 轮增量 checkpoint；进程崩溃后从最后断点恢复，无数据丢失 |
| **会话中偏好检测** | 在 `close()` 时：零 LLM 成本关键词/正则检测显式偏好；命中后即时写入用户偏好 |
| **跨会话关键词检索** | FTS5 / BM25 全文检索 L0/L1；无嵌入向量、无向量库；可按用户/Agent 过滤会话 |

### 3. 记忆蒸馏与长期学习

**从「会话」到「关系」。** 单次会话是点，关系是线。若 Agent 每轮对话都从零开始，就很难形成「关系感」。Context GC 的目标是：让 Agent 在跨会话中保持对用户的认知——偏好、习惯、成功与失败的模式。用户不用一遍遍解释「我喜欢简洁」「别用 var」，Agent 会逐渐「认识」这个人。这不是在优化一个函数，而是在设计一种可持续的人机关系。

从已完成会话中抽取**用户偏好**、**用户经验**、**私有化技能**，通过可配置蒸馏管道持续学习。

| 能力 | 说明 |
| ---- | ---- |
| **三阶段管道** | **Task Agent** → 抽取带成功/失败标注的任务；**Distiller** → 分析执行结果；**Writers** → 写入偏好、经验、技能更新 |
| **用户偏好** | 写作风格、编码习惯、纠正记录、显式偏好；按用户存储；会话开始时注入 |
| **用户经验** | 按任务划分的成功模式与失败反模式；每任务独立目录；用于决策优化 |
| **技能（公共 / 私有）** | 公共：跨用户共享；私有：用户级；均可通过蒸馏更新 |
| **去重与冲突处理** | 语义去重：`exact` / `keyword_overlap` / `llm_similar`；冲突策略：`append` / `newer_wins` / `keep_both` / `llm_merge` |
| **记忆生命周期** | 偏好/经验 TTL 老化淘汰；`memory_inject_max_tokens` 控制注入容量上限 |
| **成本预算** | 蒸馏管道 token 预算封顶；超限时自动跳过低优先级任务 |

### 4. 架构特点

**零基础设施的哲学——嵌入而非替代。** 不要求向量库、不要求新服务、核心零依赖，Context GC 是嵌入式的。它不取代现有架构，而是融入宿主系统，为任意 Agent 提供可选的记忆与压缩能力。好的能力应当可被嵌入，而不是强迫宿主重构世界。

| 特性 | 说明 |
| ---- | ---- |
| **纯库嵌入** | 宿主注入回调；无强制服务，进程内运行 |
| **模型无关** | `generate_summary`、`merge_summary`、`compute_relevance`、`estimate_tokens` 由宿主注入 — 可接入任意 LLM 或启发式 |
| **后端可插拔** | `MemoryBackend` 协议：SQLite、文件系统、对象存储等 |
| **零依赖** | 核心仅用 Python 标准库；dev/example 为可选 extras |

## 安装

核心包零第三方依赖，仅用 Python 标准库。

```bash
pip install -e .              # 安装核心包（可编辑模式）
pip install -e ".[dev]"       # 安装核心 + 测试依赖（pytest, pytest-asyncio, python-dotenv）
pip install -e ".[example]"   # 安装核心 + 示例依赖（openai, python-dotenv）
```

### 大模型配置（集成测试 / 示例用）

```bash
cp .env.example .env
# 编辑 .env，填入 CONTEXT_GC_API_KEY 等
```

## 快速开始

### 会话内压缩（已实现）

```python
from context_gc import ContextGC, ContextGCOptions

opts = ContextGCOptions(
    max_input_tokens=5000,
    generate_summary=your_generate_summary,
    merge_summary=your_merge_summary,
    compute_relevance=your_compute_relevance,
    estimate_tokens=your_estimate_tokens,
)
gc = ContextGC(opts)

# 每轮
gc.push([{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}])
await gc.close()  # 摘要 + 分代 + 合并 + checkpoint + 偏好信号检测

# 获取上下文
messages = await gc.get_messages(current_messages)
```

### 记忆持久化 + 蒸馏

```python
from context_gc import ContextGC, ContextGCOptions, FileBackend, build_memory_injection

backend = FileBackend(data_dir="./data")
gc = ContextGC(opts, session_id="sess_001", backend=backend)

# ... 多轮对话（push / close）...

# 会话结束：L0/L1/L2 持久化 → 蒸馏管道 → 清理 checkpoint
result = await gc.on_session_end(user_id="u1", agent_id="agent_1")

# 新会话：加载偏好、经验、技能注入 prompt
prefs = await gc.get_user_preferences("u1")
exps = await gc.get_user_experience("u1")
skills = await gc.get_user_skills("u1")
injection = build_memory_injection(preferences=prefs, experiences=exps, skills=skills)
```

完整用例见 [`examples/context_gc_with_storage.py`](examples/context_gc_with_storage.py)。

## 测试

按核心能力组织测试。运行全部单元测试：

```bash
python3 -m pytest tests/ -v
```

### 能力与测试对应

| 核心能力 | 测试文件 | 覆盖内容 |
| -------- | -------- | -------- |
| **1. 会话内压缩** | `test_generational.py` | 分代打分（衰减、clamp） |
| | `test_100_rounds.py` | 101 轮集成：增量摘要、分代标注、容量阈值触发合并 |
| | `test_e2e_cases.py`（Case 1、2） | 摘要 + 分代打分；容量触发合并 |
| **2. 会话级记忆持久化** | `test_storage.py` | L0/L1/L2 存读、跨会话关键词检索（FTS5）、Checkpoint 写入/恢复/清理、会话过期 |
| | `test_memory.py` | 会话中偏好检测（PreferenceDetector，零 LLM 成本） |
| | `test_e2e_cases.py`（Case 3、4、5） | 偏好检测 + 持久化；Checkpoint 崩溃恢复；全链路（L0/L1/L2、跨会话检索） |
| **3. 记忆蒸馏与长期学习** | `test_storage.py` | 偏好、经验、技能持久化 |
| | `test_memory.py` | 生命周期：TTL 老化、记忆注入、token 上限 |
| | `test_distillation.py` | 管道组件：TaskSchema、DistillationOutcome、TaskToolContext（任务、偏好） |
| | `test_e2e_cases.py`（Case 5、6、7） | 全链路 + 蒸馏管道 + 经验/技能跨会话 |

### 端到端集成测试（7 Case）

覆盖全部核心能力的端到端测试。需在 `.env` 中配置 LLM API Key：

```bash
cp .env.example .env   # 填入 CONTEXT_GC_API_KEY
python3 tests/test_e2e_cases.py
```

| Case | 核心能力 | 说明 | 结果 | 耗时 |
| ---- | -------- | ---- | ---- | ---- |
| 1 | 会话内压缩 | 5 轮：摘要 + 分代打分 + get_messages | 5/5 ✓ | ~3s |
| 2 | 会话内压缩 | 10 轮、小容量：容量触发合并 | 4/4 ✓ | ~9s |
| 3 | 会话级持久化 | 5 轮含偏好表达：检测 → 持久化 → 加载 | 4/4 ✓ | ~1.4s |
| 4 | 会话级持久化 | 8 轮，第 5 轮后模拟崩溃：Checkpoint 恢复 | 4/5 ✓ | ~5s |
| 5 | 全链路 | 8 轮：会话 → L0/L1/L2 持久化 → 新会话加载 → 跨会话检索 → 记忆注入 | 17/17 ✓ | ~6s |
| 6 | **蒸馏管道** | 10 轮：Task Agent → 蒸馏分析 → 经验写入 → 技能学习 | 9/9 ✓ | ~19s |
| 7 | **经验/技能跨会话** | 新会话加载经验+技能 → 记忆注入 → 生命周期 TTL（无 LLM 调用） | 9/9 ✓ | ~2ms |

**总结**：52/53 检查通过 · 总耗时约 45s

报告输出：`tests/output/YYYY-MM-DD/e2e_test_report.txt`（按日期建目录）

### 100 轮集成测试

针对**会话内压缩**的端到端评测。需在 `.env` 中配置 LLM API Key：

```bash
cp .env.example .env   # 填入 CONTEXT_GC_API_KEY
python3 -m pytest tests/test_100_rounds.py -v -s
```

数据来源：`tests/data/dialogues.md`（101 轮 AI 教育主题对话，约 1.3 万 token）

| 指标 | 原文 | 压缩后 |
|------|------|--------|
| 轮数 | 101 轮 | 21 条摘要 |
| 总 token | 12,782 | 3,467 |
| 压缩比 | - | 约 73% |
| 单轮摘要 | 101 次 | 102 次 |
| 合并摘要 | - | 14 次 |

| 维度 | 评分 | 说明 |
|------|------|------|
| 主题覆盖 | ★★★★★ | 101 轮主题无遗漏 |
| 逻辑连贯 | ★★★★★ | 主线清晰，立场一致 |
| 核心信息保留 | ★★★★☆ | 论点与框架保留好，细节适度压缩 |
| 可回溯性 | ★★★★☆ | 单轮摘要可回溯，合并摘要需查原文补细节 |

**结论**：摘要逻辑损失可接受，无明显信息断层；具体案例、精确数据在合并时有所弱化，但不影响整体理解。

### 输出文件

- `tests/output/YYYY-MM-DD/test_100_rounds_log.txt`：单轮摘要与合并摘要的完整记录
- `tests/output/YYYY-MM-DD/test_100_rounds_final_context.txt`：最终上下文完整摘要（压缩后）
- `tests/output/YYYY-MM-DD/test_100_rounds_evaluation.md`：评估报告（含数据概览，每次运行自动生成）

## 实现进度

| 模块 | 状态 | 说明 |
|------|------|------|
| 会话内压缩（摘要/分代/合并） | **已实现** | `core.py` + `compaction.py` + `generational.py` + `state.py` |
| 100 轮集成测试 | **已实现** | 101 轮、73% 压缩比 |
| MemoryBackend 协议 + FileBackend | **已实现** | `storage/backend.py` + `storage/file_backend.py` |
| Checkpoint 崩溃恢复 | **已实现** | `storage/checkpoint.py` |
| 偏好信号检测 | **已实现** | `memory/preference.py`，零 LLM 成本 |
| 蒸馏管道（Task Agent → 蒸馏 → Skill Learner） | **已实现** | `distillation/` 子包，复用 AsMe 提示词 |
| 记忆生命周期（老化/淘汰/注入） | **已实现** | `memory/lifecycle.py`，TTL + 容量控制 |
| 会话过期清理 | **已实现** | `storage/cleanup.py` |
| 单元测试 | **已实现** | 26 个用例，覆盖持久化/检查点/偏好/分代/生命周期/蒸馏 |
| 端到端集成测试 | **已实现** | 7 个 Case，52/53 通过 |

## 项目结构

```
context-gc/
├── src/
│   └── context_gc/
│       ├── __init__.py          # 包入口，re-export 所有核心类
│       ├── core.py              # 主类 ContextGC + ContextGCOptions
│       ├── state.py             # RoundMeta, ContextGCState
│       ├── compaction.py        # 容量阈值检查与合并摘要
│       ├── generational.py      # 分代打分（衰减 + clamp + 步进）
│       │
│       ├── storage/             # 持久化层
│       │   ├── backend.py       # MemoryBackend Protocol + 数据类
│       │   ├── file_backend.py  # 文件系统后端实现
│       │   ├── checkpoint.py    # Checkpoint 崩溃恢复
│       │   └── cleanup.py       # 会话过期清理
│       │
│       ├── memory/              # 记忆管理
│       │   ├── preference.py    # 偏好信号检测（零 LLM 成本）
│       │   └── lifecycle.py     # 老化/淘汰/注入容量控制
│       │
│       └── distillation/        # 蒸馏管道
│           ├── flush.py         # 管道入口
│           ├── models.py        # 数据模型
│           ├── task_agent.py    # Task Agent（任务提取）
│           ├── distiller.py     # 蒸馏（成功/失败分析）
│           ├── skill_learner.py # Skill Learner（技能更新）
│           └── experience_writer.py  # 经验写入 + 去重
│
├── tests/
│   ├── test_storage.py          # FileBackend + Checkpoint
│   ├── test_memory.py           # PreferenceDetector + Lifecycle
│   ├── test_generational.py     # Generational scoring
│   ├── test_distillation.py     # Distillation models + tools
│   ├── test_100_rounds.py       # 100-round end-to-end integration
│   └── data/dialogues.md        # Test data
│
├── examples/
│   └── context_gc_with_storage.py  # 完整持久化 + 蒸馏示例
│
├── docs/
│   ├── design/
│   │   ├── memory-system.md              # Full design (13 chapters)
│   │   └── context-compression.md        # In-session compression design
│   ├── comparisons/                      # Competitive analysis
│   └── references/                       # Guides & references
└── README.md
```

## 设计文档

**Design**

- [Memory System](docs/design/memory-system.md) — **完整方案**（13 章）：L0/L1/L2 分层、蒸馏管道、Checkpoint、Harness Engineering、端到端验证
- [Context Compression](docs/design/context-compression.md) — 会话内压缩设计规范

**Comparisons**

- [Claude Code](docs/comparisons/claude-code.md) — 与 Claude Code 上下文机制对比
- [OpenViking](docs/comparisons/openviking.md) — 与 OpenViking 对比
- [Sirchmunk](docs/comparisons/sirchmunk.md) — 与 Sirchmunk 对比
- [OpenViking vs Sirchmunk](docs/comparisons/openviking-vs-sirchmunk.md) — 两者横向对比

**References**

- [OpenViking Replica (No Embedding)](docs/references/openviking-replica-no-embedding.md) — OpenViking 复刻指南（L0 改用非向量检索）
