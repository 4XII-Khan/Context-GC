# Context GC

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Tests: pytest](https://img.shields.io/badge/tests-pytest-green.svg)](tests/)
[![LLM](https://img.shields.io/badge/LLM-Context%20Management-orange.svg)](docs/上下文压缩设计.md)

通用上下文管理方案，适用于基于 LLM 的对话 / Agent 系统。在每轮对话后持续进行摘要与分代标注，结合容量阈值触发二次摘要，实现有限窗口下的无限对话。

## 核心能力

- **增量摘要与分代标注**：每轮结束时对当前轮产出摘要，并对历史轮次做关联度计算与分代值更新
- **容量阈值触发合并**：当上下文 token 占用达到预设档位（20%、30%、40%…）时，对低分代轮次执行相邻合并摘要
- **分代策略**：基于与当前对话的关联度排序，前 50% 标为老生代保留，后 50% 标为新生代参与合并

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

```python
# 在项目根目录运行，或将 src 加入 PYTHONPATH
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
await gc.close()

# 获取上下文
messages = await gc.get_messages(current_messages)
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

## 项目结构

```
context-gc/
├── src/
│   └── context_gc/       # 核心包
│       ├── context_gc.py # 主类 ContextGC
│       ├── state.py      # RoundMeta, ContextGCState
│       ├── compaction.py # 容量阈值检查与合并摘要
│       └── generational.py # 分代打分逻辑
├── tests/
│   ├── test_100_rounds.py  # 100 轮端到端测试
│   ├── data/             # 测试数据
│   │   └── dialogues.md  # 101 轮 AI 教育对话
│   └── output/           # 测试输出（自动生成）
│       ├── test_100_rounds_log.txt
│       ├── test_100_rounds_final_context.txt
│       └── test_100_rounds_evaluation.md
├── docs/
│   ├── 上下文压缩设计.md        # 会话内压缩设计
│   └── 记忆系统设计.md         # L0/L1/L2、偏好、经验存储、蒸馏管道
└── README.md
```

## 设计文档

- [上下文压缩设计](docs/上下文压缩设计.md) — 会话内压缩设计规范
- [记忆系统设计](docs/记忆系统设计.md) — **最新方案**：L0/L1/L2 会话级存储、用户偏好、技能经验、蒸馏管道、Harness
- [与ClaudeCode对比](docs/与ClaudeCode对比.md) — 与 Claude Code 上下文机制对比
- [与OpenViking对比](docs/与OpenViking对比.md) — 与 OpenViking 对比
- [与Sirchmunk对比](docs/与Sirchmunk对比.md) — 与 Sirchmunk 对比
- [OpenViking与Sirchmunk对比](docs/OpenViking与Sirchmunk对比.md) — OpenViking 与 Sirchmunk 对比
- [OpenViking复刻-无向量](docs/OpenViking复刻-无向量.md) — OpenViking 复刻指南（L0 改用非向量检索）
