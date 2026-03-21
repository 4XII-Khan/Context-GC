**English** | [中文](README.zh-CN.md)

# Context GC

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Tests: pytest](https://img.shields.io/badge/tests-pytest-green.svg)](tests/)
[![LLM](https://img.shields.io/badge/LLM-Context%20Management-orange.svg)](docs/design/context-compression.md)

A model-agnostic conversation context management library for LLM-based dialogue and agent systems. It performs sustainable in-session compression via generational tagging and capacity-triggered merging, persists session summaries into L0/L1/L2 layered storage, and extracts user preferences, experiences, and personalized skills through a distillation pipeline — forming a complete loop of **Compression → Persistence → Distillation → Injection**.

## Core Capabilities

### In-Session Compression

- **Incremental Summarization & Generational Tagging**: Produces a summary at each round end, updates `gen_score` for historical rounds (with decay + clamp)
- **Capacity-Triggered Merging**: When token usage hits preset thresholds (20%/30%/40%…), low-score rounds are merged with neighbors
- **Step-Based Scoring**: Scores every N rounds instead of every round, reducing LLM call frequency

### Session-Level Memory Persistence

- **L0/L1/L2 Layered Storage**: L0 (quick coarse filter, 50–200 tokens) → L1 (GC summary list, detailed navigation) → L2 (raw conversation, on-demand retrieval)
- **Checkpoint Crash Recovery**: Incremental checkpoint every N rounds; recovers from breakpoint after a crash
- **In-Session Preference Detection**: Zero-LLM-cost keyword detection at `close()` time; explicit preferences written immediately
- **Cross-Session Search**: FTS5 / BM25 keyword search, no embedding dependency

### Memory Distillation & Long-Term Learning

- **Three-Stage Pipeline**: Task Agent → Distillation (success/failure analysis) → Write (preferences + experiences + personalized skills)
- **Experience Deduplication & Conflict Resolution**: Task normalization, semantic dedup (exact / keyword_overlap / llm_similar), conflict strategies (append / newer_wins / keep_both / llm_merge)
- **Memory Lifecycle**: TTL-based aging for preferences/experiences + injection capacity control (`memory_inject_max_tokens`)
- **Cost Budget**: Token budget cap for distillation pipeline; auto-skips low-priority tasks when exceeded

### Architecture

- **Pure Library**: Host injects callbacks; no mandatory service dependencies
- **Model-Agnostic**: `generate_summary`, `compute_relevance`, and other callbacks are injected by the host — swap any LLM
- **Pluggable Backend**: `MemoryBackend` protocol supports SQLite, filesystem, object storage, etc.

## Installation

The core package has zero third-party dependencies — standard library only.

```bash
pip install -e .              # Install core package (editable mode)
pip install -e ".[dev]"       # Core + test deps (pytest, pytest-asyncio, python-dotenv)
pip install -e ".[example]"   # Core + example deps (openai, python-dotenv)
```

### LLM Configuration (for integration tests / examples)

```bash
cp .env.example .env
# Edit .env and fill in CONTEXT_GC_API_KEY, etc.
```

## Quick Start

### In-Session Compression

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

# Each round
gc.push([{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}])
await gc.close()  # Summarize + score + merge + checkpoint + preference detection

# Get context for the main LLM
messages = await gc.get_messages(current_messages)
```

### Memory Persistence + Distillation

```python
from context_gc import ContextGC, ContextGCOptions, FileBackend, build_memory_injection

backend = FileBackend(data_dir="./data")
gc = ContextGC(opts, session_id="sess_001", backend=backend)

# ... multi-round conversation (push / close) ...

# Session end: L0/L1/L2 persistence → distillation pipeline → checkpoint cleanup
result = await gc.on_session_end(user_id="u1", agent_id="agent_1")

# New session: load preferences, experiences, skills for prompt injection
prefs = await gc.get_user_preferences("u1")
exps = await gc.get_user_experience("u1")
skills = await gc.get_user_skills("u1")
injection = build_memory_injection(preferences=prefs, experiences=exps, skills=skills)
```

See [`examples/context_gc_with_storage.py`](examples/context_gc_with_storage.py) for a full working example.

## 100-Round Test & Evaluation

Run the integration test after configuring `.env`:

```bash
python3 tests/test_100_rounds.py
# or
python3 -m pytest tests/test_100_rounds.py -v -s
```

Data source: `tests/data/dialogues.md` (101-round AI education dialogue, ~13k tokens)

### Compression Results

| Metric | Original | Compressed |
| ------ | -------- | ---------- |
| Rounds | 101 | 21 summaries |
| Total tokens | 12,782 | 3,467 |
| Compression ratio | - | ~73% |
| Single-round summaries | 101 | 102 |
| Merge summaries | - | 14 |

### Summary Quality

| Dimension | Rating | Notes |
| --------- | ------ | ----- |
| Topic coverage | 5/5 | All 101 round topics preserved |
| Logical coherence | 5/5 | Clear main thread, consistent stance |
| Key info retention | 4/5 | Arguments and frameworks well preserved; details appropriately compressed |
| Traceability | 4/5 | Single-round summaries are traceable; merged summaries need original text for fine details |

### Output Files

- `tests/output/test_100_rounds_log.txt`: Full log of single-round and merge summaries
- `tests/output/test_100_rounds_final_context.txt`: Final compressed context
- `tests/output/test_100_rounds_evaluation.md`: Full comparative evaluation report

## Implementation Status

| Module | Status | Details |
| ------ | ------ | ------- |
| In-session compression (summarize/score/merge) | **Done** | `core.py` + `compaction.py` + `generational.py` + `state.py` |
| 100-round integration test | **Done** | 101 rounds, 73% compression ratio |
| MemoryBackend protocol + FileBackend | **Done** | `storage/backend.py` + `storage/file_backend.py` |
| Checkpoint crash recovery | **Done** | `storage/checkpoint.py` |
| Preference detection | **Done** | `memory/preference.py`, zero LLM cost |
| Distillation pipeline (Task Agent → Distill → Skill Learner) | **Done** | `distillation/` sub-package, adapted from AsMe prompts |
| Memory lifecycle (aging/eviction/injection) | **Done** | `memory/lifecycle.py`, TTL + capacity control |
| Session expiry cleanup | **Done** | `storage/cleanup.py` |
| Unit tests | **Done** | 26 cases covering storage/checkpoint/preference/generational/lifecycle/distillation |

## Project Structure

```
context-gc/
├── src/
│   └── context_gc/
│       ├── __init__.py          # Package entry, re-exports all core classes
│       ├── core.py              # Main class: ContextGC + ContextGCOptions
│       ├── state.py             # RoundMeta, ContextGCState
│       ├── compaction.py        # Capacity check & merge summaries
│       ├── generational.py      # Generational scoring (decay + clamp + step)
│       │
│       ├── storage/             # Persistence layer
│       │   ├── backend.py       # MemoryBackend Protocol + data classes
│       │   ├── file_backend.py  # Filesystem backend implementation
│       │   ├── checkpoint.py    # Checkpoint crash recovery
│       │   └── cleanup.py       # Session expiry cleanup
│       │
│       ├── memory/              # Memory management
│       │   ├── preference.py    # Preference detection (zero LLM cost)
│       │   └── lifecycle.py     # Aging / eviction / injection capacity
│       │
│       └── distillation/        # Distillation pipeline
│           ├── flush.py         # Pipeline entry point
│           ├── models.py        # Data models
│           ├── task_agent.py    # Task Agent (task extraction)
│           ├── distiller.py     # Distillation (success/failure analysis)
│           ├── skill_learner.py # Skill Learner (skill updates)
│           └── experience_writer.py  # Experience writing + dedup
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
│   └── context_gc_with_storage.py  # Full persistence + distillation example
│
├── docs/
│   ├── design/
│   │   ├── memory-system.md              # Full design (13 chapters)
│   │   └── context-compression.md        # In-session compression design
│   ├── comparisons/                      # Competitive analysis
│   └── references/                       # Guides & references
├── pyproject.toml
└── README.md
```

## Documentation

**Design**

- [Memory System](docs/design/memory-system.md) — Full design (13 chapters): L0/L1/L2 layered storage, distillation pipeline, checkpoint, harness engineering, end-to-end validation
- [Context Compression](docs/design/context-compression.md) — In-session compression design spec

**Comparisons**

- [Claude Code](docs/comparisons/claude-code.md) — Comparison with Claude Code's context mechanism
- [OpenViking](docs/comparisons/openviking.md) — Comparison with OpenViking
- [Sirchmunk](docs/comparisons/sirchmunk.md) — Comparison with Sirchmunk
- [OpenViking vs Sirchmunk](docs/comparisons/openviking-vs-sirchmunk.md) — Cross comparison

**References**

- [OpenViking Replica (No Embedding)](docs/references/openviking-replica-no-embedding.md) — OpenViking replica guide (L0 using non-vector search)

## License

[Apache License 2.0](LICENSE)
