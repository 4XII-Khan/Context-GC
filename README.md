<h1 align="center">Context GC</h1>

<p align="center">
  <strong>Compress → Persist → Distill → Inject. The complete context lifecycle for LLM agents.</strong>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="License"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/tests-26%20passed-brightgreen.svg" alt="Tests"></a>
  <a href="#"><img src="https://img.shields.io/badge/dependencies-zero-orange.svg" alt="Zero Dependencies"></a>
</p>

<p align="center">
  <code>Python</code> · <code>AsyncIO</code> · <code>Zero Dependencies</code> · <code>Model-Agnostic</code> · <code>Pluggable Backend</code>
</p>

<p align="center">
  📖 <a href="docs/design/memory-system.md"><strong>Design Doc</strong></a> ·
  <a href="#quick-start"><strong>Quick Start</strong></a> ·
  <a href="#core-capabilities"><strong>Key Features</strong></a> ·
  <a href="#100-round-test--evaluation"><strong>Benchmarks</strong></a> ·
  <a href="#documentation"><strong>Docs</strong></a>
</p>

<p align="center">
  🗜️ <strong>Generational Compression</strong> •
  🧠 <strong>L0/L1/L2 Layered Memory</strong> •
  🔬 <strong>Memory Distillation</strong><br>
  ⚡ <strong>Zero-LLM Preference Detection</strong> •
  🔄 <strong>Crash Recovery</strong> •
  📚 <strong>Skill Learning</strong>
</p>

<p align="center">
  <b>English</b> | <a href="README.zh-CN.md">中文</a>
</p>

---

## 🌰 Why Context GC?

LLM context windows are finite, but conversations grow without bound. Existing solutions either **truncate blindly** (losing critical context), **summarize everything equally** (wasting tokens on irrelevant history), or **require external vector databases** (adding infrastructure complexity). Context GC takes a fundamentally different approach: treat context management as a **garbage collection problem** — keep what matters, compress what's aging, and recycle knowledge into long-term memory.

---

## ✨ Core Capabilities

### 1. In-Session Compression

- **Incremental Summarization & Generational Tagging** — Each round produces a summary; historical rounds receive `gen_score` updates (with decay + clamp)
- **Capacity-Triggered Merging** — When token usage hits preset thresholds (20%/30%/40%…), low-score rounds are merged with neighbors
- **Step-Based Scoring** — Scores every N rounds instead of every round, reducing LLM call frequency

### 2. Session-Level Memory Persistence

- **L0/L1/L2 Layered Storage** — L0 (quick coarse filter, 50–200 tokens) → L1 (GC summary list) → L2 (raw conversation, on-demand)
- **Checkpoint Crash Recovery** — Incremental checkpoint every N rounds; recovers from breakpoint after a crash
- **In-Session Preference Detection** — Zero-LLM-cost keyword detection at `close()` time; explicit preferences written immediately
- **Cross-Session Search** — FTS5 / BM25 keyword search, no embedding dependency

### 3. Memory Distillation & Long-Term Learning

- **Three-Stage Pipeline** — Task Agent → Distillation (success/failure analysis) → Write (preferences + experiences + personalized skills)
- **Experience Deduplication & Conflict Resolution** — Task normalization, semantic dedup (exact / keyword_overlap / llm_similar), conflict strategies (append / newer_wins / keep_both / llm_merge)
- **Memory Lifecycle** — TTL-based aging for preferences/experiences + injection capacity control
- **Cost Budget** — Token budget cap for distillation pipeline; auto-skips low-priority tasks when exceeded

### 4. Architecture

| Property | Description |
| -------- | ----------- |
| **Pure Library** | Host injects callbacks; no mandatory service dependencies |
| **Model-Agnostic** | `generate_summary`, `compute_relevance` callbacks are host-injected — swap any LLM |
| **Pluggable Backend** | `MemoryBackend` protocol supports SQLite, filesystem, object storage, etc. |
| **Zero Dependencies** | Core package uses Python standard library only |

---

### Context GC vs. Traditional Approaches

| Dimension | Truncation | Fixed Summarization | Vector RAG | ✨ Context GC |
| --------- | ---------- | ------------------- | ---------- | ------------- |
| 💰 Setup Cost | None | Low | High (VectorDB) | ✅ Zero infrastructure |
| 🎯 Context Quality | ❌ Loses old context | ⚠️ Equal compression | ⚠️ Retrieval noise | ✅ Generational — keeps what matters |
| 🧠 Long-Term Learning | ❌ None | ❌ None | ❌ None | ✅ Distillation → preferences, experiences, skills |
| 🔄 Crash Recovery | ❌ None | ❌ None | N/A | ✅ Checkpoint every N rounds |
| ⚡ LLM Cost | None | High (every round) | Embedding cost | ✅ Step-based scoring, zero-LLM preference detection |

---

## 🚀 Quick Start

### Installation

The core package has **zero third-party dependencies** — standard library only.

```bash
pip install -e .              # Install core package (editable mode)
pip install -e ".[dev]"       # Core + test deps (pytest, pytest-asyncio, python-dotenv)
pip install -e ".[example]"   # Core + example deps (openai, python-dotenv)
```

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

# Session end: L0/L1/L2 persistence → distillation → checkpoint cleanup
result = await gc.on_session_end(user_id="u1", agent_id="agent_1")

# New session: load user memory for prompt injection
prefs = await gc.get_user_preferences("u1")
exps = await gc.get_user_experience("u1")
skills = await gc.get_user_skills("u1")
injection = build_memory_injection(preferences=prefs, experiences=exps, skills=skills)
```

See [`examples/context_gc_with_storage.py`](examples/context_gc_with_storage.py) for a full working example.

---

## 📊 100-Round Test & Evaluation

```bash
cp .env.example .env   # Fill in CONTEXT_GC_API_KEY
python3 -m pytest tests/test_100_rounds.py -v -s
```

Data source: `tests/data/dialogues.md` (101-round AI education dialogue, ~13k tokens)

### Compression Results

| Metric | Original | Compressed |
| ------ | -------- | ---------- |
| Rounds | 101 | 21 summaries |
| Total tokens | 12,782 | 3,467 |
| Compression ratio | — | **~73%** |
| Single-round summaries | 101 | 102 |
| Merge summaries | — | 14 |

### Summary Quality

| Dimension | Rating | Notes |
| --------- | ------ | ----- |
| Topic coverage | ★★★★★ | All 101 round topics preserved |
| Logical coherence | ★★★★★ | Clear main thread, consistent stance |
| Key info retention | ★★★★☆ | Arguments and frameworks well preserved |
| Traceability | ★★★★☆ | Merged summaries need original text for fine details |

---

## 📋 Implementation Status

| Module | Status | Details |
| ------ | ------ | ------- |
| In-session compression | ✅ Done | `core.py` + `compaction.py` + `generational.py` + `state.py` |
| 100-round integration test | ✅ Done | 101 rounds, 73% compression ratio |
| MemoryBackend + FileBackend | ✅ Done | `storage/backend.py` + `storage/file_backend.py` |
| Checkpoint crash recovery | ✅ Done | `storage/checkpoint.py` |
| Preference detection | ✅ Done | `memory/preference.py`, zero LLM cost |
| Distillation pipeline | ✅ Done | `distillation/` sub-package |
| Memory lifecycle | ✅ Done | `memory/lifecycle.py`, TTL + capacity control |
| Session expiry cleanup | ✅ Done | `storage/cleanup.py` |
| Unit tests | ✅ Done | 26 cases |

---

## 🏗️ Project Structure

```
context-gc/
├── src/context_gc/
│   ├── core.py              # ContextGC main class
│   ├── state.py             # RoundMeta, ContextGCState
│   ├── compaction.py        # Capacity check & merge
│   ├── generational.py      # Generational scoring
│   ├── storage/             # Persistence layer
│   │   ├── backend.py       # MemoryBackend Protocol
│   │   ├── file_backend.py  # Filesystem implementation
│   │   ├── checkpoint.py    # Crash recovery
│   │   └── cleanup.py       # Session expiry
│   ├── memory/              # Memory management
│   │   ├── preference.py    # Zero-LLM preference detection
│   │   └── lifecycle.py     # Aging / eviction / injection
│   └── distillation/        # Distillation pipeline
│       ├── flush.py         # Pipeline entry point
│       ├── task_agent.py    # Task extraction
│       ├── distiller.py     # Success/failure analysis
│       ├── skill_learner.py # Skill updates
│       └── experience_writer.py
├── tests/                   # 26 unit tests + 100-round integration
├── examples/                # Full working example
└── docs/
    ├── design/              # Architecture & design specs
    ├── comparisons/         # Competitive analysis
    └── references/          # Guides
```

---

## 📖 Documentation

**Design**

- [Memory System](docs/design/memory-system.md) — Full design (13 chapters): L0/L1/L2 layered storage, distillation pipeline, checkpoint, harness engineering, end-to-end validation
- [Context Compression](docs/design/context-compression.md) — In-session compression design spec

**Comparisons**

- [Claude Code](docs/comparisons/claude-code.md) · [OpenViking](docs/comparisons/openviking.md) · [Sirchmunk](docs/comparisons/sirchmunk.md) · [OpenViking vs Sirchmunk](docs/comparisons/openviking-vs-sirchmunk.md)

**References**

- [OpenViking Replica (No Embedding)](docs/references/openviking-replica-no-embedding.md)

---

## 📄 License

This project is licensed under the [Apache License 2.0](LICENSE).
