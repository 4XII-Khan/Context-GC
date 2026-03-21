<h1 align="center">Context GC</h1>

<p align="center">
  <strong>压缩 → 持久化 → 蒸馏 → 注入，LLM Agent 的完整上下文生命周期管理。</strong>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="License"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/tests-26%20passed-brightgreen.svg" alt="Tests"></a>
  <a href="#"><img src="https://img.shields.io/badge/dependencies-zero-orange.svg" alt="Zero Dependencies"></a>
</p>

<p align="center">
  <code>Python</code> · <code>AsyncIO</code> · <code>零依赖</code> · <code>模型无关</code> · <code>后端可插拔</code>
</p>

<p align="center">
  📖 <a href="docs/design/memory-system.md"><strong>设计文档</strong></a> ·
  <a href="#快速开始"><strong>快速开始</strong></a> ·
  <a href="#核心能力"><strong>核心能力</strong></a> ·
  <a href="#100-轮测试与评估"><strong>评测</strong></a> ·
  <a href="#设计文档"><strong>文档</strong></a>
</p>

<p align="center">
  🗜️ <strong>分代压缩</strong> •
  🧠 <strong>L0/L1/L2 分层记忆</strong> •
  🔬 <strong>记忆蒸馏</strong><br>
  ⚡ <strong>零 LLM 偏好检测</strong> •
  🔄 <strong>崩溃恢复</strong> •
  📚 <strong>技能学习</strong>
</p>

<p align="center">
  <a href="README.md">English</a> | <b>中文</b>
</p>

---

纯库形态、模型无关的对话上下文管理方案，适用于基于 LLM 的对话 / Agent 系统。会话内通过分代标注与容量触发合并实现可持续压缩；会话结束时将摘要映射为 L0/L1/L2 三层持久化，并通过蒸馏管道提取用户偏好、经验与私有化技能，形成"压缩 → 持久化 → 蒸馏 → 注入"的完整闭环。

## 核心能力

### 会话内压缩

- **增量摘要与分代标注**：每轮结束时产出摘要，对历史轮次做关联度计算与 gen_score 更新（衰减 + clamp）
- **容量阈值触发合并**：token 占用达预设档位（20%/30%/40%…）时，低分代轮次相邻合并
- **步进式打分**：每隔 N 轮打一次分，中间轮次沿用上次 gen_score，降低 LLM 调用频率

### 会话级记忆持久化

- **L0/L1/L2 分层**：L0（快速粗筛，50–200 tokens）→ L1（GC 摘要列表，详细导航）→ L2（原始对话，按需回溯）
- **Checkpoint 崩溃恢复**：每 N 轮增量写入 checkpoint，进程崩溃后可从断点恢复
- **会话中即时偏好抽取**：`close()` 时零 LLM 成本关键词检测，显式偏好立即写入
- **跨会话检索**：FTS5 / BM25 无向量检索，无 Embedding 依赖

### 记忆蒸馏与长期学习

- **三阶段管道**：Task Agent → 蒸馏（成功/失败分析）→ 写入（偏好 + 经验 + 私有化技能）
- **经验去重与冲突**：任务归一化、语义去重（exact / keyword_overlap / llm_similar）、冲突策略（append / newer_wins / keep_both / llm_merge）
- **记忆生命周期**：偏好/经验 TTL 老化淘汰 + 注入时容量控制（`memory_inject_max_tokens`）
- **成本预算**：蒸馏管道 token 预算封顶，超限自动 skip 低优先级任务

### 架构特点

- **纯库嵌入**：宿主注入回调，无强制服务依赖
- **模型无关**：`generate_summary`、`compute_relevance` 等回调由实现方注入，可切换任意 LLM
- **后端可插拔**：MemoryBackend 协议支持 SQLite、文件系统、对象存储等

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

## 100 轮测试与评估

配置 `.env` 后运行测试：

```bash
python3 tests/test_100_rounds.py
# 或
python3 -m pytest tests/test_100_rounds.py -v -s
```

数据来源：`tests/data/dialogues.md`（101 轮 AI 教育主题对话，约 1.3 万 token）

### 压缩效果（来自评估报告）

| 指标 | 原文 | 压缩后 |
|------|------|--------|
| 轮数 | 101 轮 | 21 条摘要 |
| 总 token | 12,782 | 3,467 |
| 压缩比 | - | 约 73% |
| 单轮摘要 | 101 次 | 102 次 |
| 合并摘要 | - | 14 次 |

### 摘要质量评估

| 维度 | 评分 | 说明 |
|------|------|------|
| 主题覆盖 | ★★★★★ | 101 轮主题无遗漏 |
| 逻辑连贯 | ★★★★★ | 主线清晰，立场一致 |
| 核心信息保留 | ★★★★☆ | 论点与框架保留好，细节适度压缩 |
| 可回溯性 | ★★★★☆ | 单轮摘要可回溯，合并摘要需查原文补细节 |

**结论**：摘要逻辑损失可接受，无明显信息断层；具体案例、精确数据在合并时有所弱化，但不影响整体理解。

### 输出文件

- `tests/output/test_100_rounds_log.txt`：单轮摘要与合并摘要的完整记录
- `tests/output/test_100_rounds_final_context.txt`：最终上下文完整摘要（压缩后）
- `tests/output/test_100_rounds_evaluation.md`：完整对比评估报告

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
