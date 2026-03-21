# Context GC

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Tests: pytest](https://img.shields.io/badge/tests-pytest-green.svg)](tests/)
[![LLM](https://img.shields.io/badge/LLM-Context%20Management-orange.svg)](docs/上下文压缩设计.md)

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

```bash
pip install -r requirements.txt  # 测试用（openai, pytest, python-dotenv）
```

核心包 `context_gc` 无第三方依赖，仅用 Python 标准库。`requirements.txt` 中的 `openai`、`pytest` 仅用于 100 轮集成测试。

### 大模型配置（100 轮测试用）

复制 `.env.example` 为 `.env` 并填入实际值：

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

### 记忆持久化 + 蒸馏（设计完成，待实现）

```python
gc = ContextGC(opts, session_id="sess_001", backend=sqlite_backend)

# ... 多轮对话 ...

# 会话结束：L0/L1/L2 持久化 → 蒸馏管道 → 清理 checkpoint
await gc.on_session_end(user_id, agent_id)

# 新会话：加载偏好、经验、技能注入 prompt
prefs = await gc.get_user_preferences(user_id)
exps = await gc.get_user_experience(user_id)
skills = await gc.get_user_skills(user_id)
```

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
| 会话内压缩（摘要/分代/合并） | **已实现** | `src/context_gc/` 4 个文件 |
| 100 轮集成测试 | **已实现** | 101 轮、73% 压缩比 |
| MemoryBackend 协议 + 后端 | 设计完成 | SQLite / 文件后端 |
| Checkpoint 崩溃恢复 | 设计完成 | `.checkpoint.json` + append-only |
| 偏好信号检测 | 设计完成 | 关键词/正则，零 LLM 成本 |
| 蒸馏管道（Task Agent → 蒸馏 → Skill Learner） | 设计完成 | 复用 AsMe |
| 记忆生命周期（老化/淘汰） | 设计完成 | TTL + 容量控制 |
| 会话过期清理 | 设计完成 | `session_ttl_days` |

## 项目结构

```
context-gc/
├── src/
│   └── context_gc/              # 核心包（已实现）
│       ├── context_gc.py        # 主类 ContextGC
│       ├── state.py             # RoundMeta, ContextGCState
│       ├── compaction.py        # 容量阈值检查与合并摘要
│       └── generational.py      # 分代打分逻辑
├── tests/
│   ├── test_100_rounds.py       # 100 轮端到端测试
│   ├── data/                    # 测试数据
│   │   └── dialogues.md         # 101 轮 AI 教育对话
│   └── output/                  # 测试输出（自动生成）
├── docs/
│   ├── 记忆系统设计.md           # 完整设计（13章）：分层存储、蒸馏、Harness、验证
│   ├── 上下文压缩设计.md         # 会话内压缩设计
│   ├── 与ClaudeCode对比.md
│   ├── 与OpenViking对比.md
│   ├── 与Sirchmunk对比.md
│   ├── OpenViking与Sirchmunk对比.md
│   └── OpenViking复刻-无向量.md
└── README.md
```

## 设计文档

- [记忆系统设计](docs/记忆系统设计.md) — **完整方案**（13 章）：L0/L1/L2 分层、蒸馏管道、Checkpoint、Harness Engineering、端到端验证
- [上下文压缩设计](docs/上下文压缩设计.md) — 会话内压缩设计规范
- [与 Claude Code 对比](docs/与ClaudeCode对比.md) — 与 Claude Code 上下文机制对比
- [与 OpenViking 对比](docs/与OpenViking对比.md) — 与 OpenViking 对比
- [与 Sirchmunk 对比](docs/与Sirchmunk对比.md) — 与 Sirchmunk 对比
- [OpenViking 与 Sirchmunk 对比](docs/OpenViking与Sirchmunk对比.md) — 两者横向对比
- [OpenViking 复刻-无向量](docs/OpenViking复刻-无向量.md) — OpenViking 复刻指南（L0 改用非向量检索）
