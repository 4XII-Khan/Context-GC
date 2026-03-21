# OpenViking 复刻指南：L0 层改用非向量检索

> 基于 [OpenViking 官方仓库](https://github.com/volcengine/OpenViking) 整理。目标：严格复刻项目架构，但将 L0 层的向量检索替换为关键词 / grep 检索，避免 Embedding 带来的信息损失。

---

## 一、OpenViking 项目规模

| 指标 | 数值 |
|------|------|
| **仓库体积** | ~56 MB（含历史与依赖） |
| **语言分布** | Python 85% / C++ 7.2% / Rust 3.2% / JS 1.7% / Shell 1.3% |
| **主要目录** | `openviking/`（Python 包）、`src/`（C++）、`crates/ov_cli/`（Rust CLI）、`bot/`、`build_support/` |
| **依赖** | Python 3.10+、Go 1.22+、C++（GCC 9+ / Clang 11+）、Rust、CMake |
| **核心模块** | `retrieve/`（检索）、`storage/`（存储）、`resource/`（资源）、`pyagfs/`（AGFS 绑定）、`parse/`（解析） |
| **Stars** | 17k+ |

### 1.1 关键文件与职责

| 路径 | 职责 | 与 L0 向量关系 |
|------|------|----------------|
| `openviking/retrieve/hierarchical_retriever.py` | 目录递归检索核心 | 调用 `embedder.embed()`、`search_global_roots_in_tenant`、`search_children_in_tenant` |
| `openviking/models/embedder/` | Embedding 模型封装 | 生成 query/content 向量 |
| `openviking/storage/` | VikingDB 与向量索引 | 存储与查询向量 |
| `openviking/async_client.py` | 对外 API | `find()` 走语义检索，`grep()` 走关键词 |
| `openviking/service/` | 服务层 | 调度 retrieve、embedding、storage |

---

## 二、L0 向量检索的调用链

```
用户调用 find(query)
    ↓
HierarchicalRetriever.retrieve()
    ↓
1. embedder.embed(query) → query_vector, sparse_query_vector
2. _global_vector_search(vector_proxy, query_vector, ...) → 全局粗定位
3. _recursive_search(..., query_vector, ...)
   - vector_proxy.search_children_in_tenant(parent_uri, query_vector, ...) → 递归细化
4. (可选) RerankClient.rerank_batch() 精排
    ↓
返回 MatchedContext 列表
```

**需要替换的入口**：`_global_vector_search`、`search_children_in_tenant` 中依赖向量的部分。

---

## 三、非向量 L0 检索方案

### 3.1 思路

L0 存储在 `.abstract.md` 中，是 ~100 token 的纯文本。向量检索的作用是「按语义粗筛」。用非向量方案时，可改为：

1. **Grep 检索**：对 `.abstract.md` 做 `grep pattern`，命中再进入目录递归
2. **关键词检索**：从 query 提取关键词，在 L0 文本中做 BM25 / TF-IDF / 简单包含匹配
3. **混合**：Grep 粗筛 + 可选 Rerank 精排（Rerank 不依赖预计算向量，仅对候选做 LLM 打分）

### 3.2 接口抽象

设计一个 `L0Retriever` 接口，替代原有「embedder + vector_store」的粗筛逻辑：

```python
# 抽象接口
class L0Retriever(Protocol):
    async def search_l0(
        self,
        query: str,
        target_uri: str = "",
        context_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        返回与 query 相关的 L0 条目列表，每项含 uri、abstract、_score 等。
        原有实现：embedder.embed(query) + vector_store.search()
        新实现：grep/keyword 对 .abstract.md 检索。
        """
        ...
```

### 3.3 Grep 实现草图

OpenViking 已有 `grep(uri, pattern)`，可在 L0 层复用：

```python
# 伪代码：GrepL0Retriever
class GrepL0Retriever:
    def __init__(self, viking_fs, intent_analyzer=None):
        self.fs = viking_fs
        self.intent_analyzer = intent_analyzer  # 可选：从 query 提取关键词

    async def search_l0(self, query, target_uri="", limit=10):
        # 1. 从 query 提取检索词（可选：意图分析 → 关键词列表）
        patterns = self._extract_patterns(query)  # e.g. ["OAuth", "auth", "API"]

        # 2. 遍历目标范围内的 .abstract.md，grep 匹配
        scope = target_uri or "viking://"
        candidates = []
        for uri, abstract in await self._list_abstracts(scope):
            score = self._grep_score(abstract, patterns)
            if score > 0:
                candidates.append({"uri": uri, "abstract": abstract, "_score": score})

        # 3. 按 score 排序，返回 top limit
        candidates.sort(key=lambda x: x["_score"], reverse=True)
        return candidates[:limit]
```

关键词提取可选用：

- 简单：按空格/停用词过滤，保留实词
- 进阶：LLM 做意图分析，产出 `grep_patterns`（OpenViking 的 `TypedQuery` 已支持 `grep_patterns`）

### 3.4 与 Sirchmunk 的参考

Sirchmunk 的 FAST 模式用「2 级关键词级联 + 上下文采样」，约 2 次 LLM 调用。可借鉴：

1. 用 LLM 从 query 抽取检索关键词（1 次调用）
2. 用 ripgrep/grep 对 L0 全文检索
3. 按匹配度（命中数、位置等）打分排序

---

## 四、严格复刻与改造步骤

### 4.1 Fork 与阅读

```bash
git clone https://github.com/volcengine/OpenViking.git
cd OpenViking
# 阅读文档、跑通默认流程
pip install -e .
openviking-server  # 或按文档启动
```

### 4.2 定位改动点

1. **`openviking/retrieve/hierarchical_retriever.py`**
   - `__init__`：`embedder` 改为可选；新增 `l0_retriever`（GrepL0Retriever）
   - `retrieve()`：当 `l0_retriever` 存在时，跳过 `embedder.embed()`，用 `l0_retriever.search_l0()` 替代 `_global_vector_search`
   - `_recursive_search`：`search_children_in_tenant` 的向量参数改为「对子节点 L0 做 grep」，或新增 `search_children_by_grep`

2. **`openviking/storage/`**
   - 新增 `GrepL0Backend` 或扩展 `VikingDBManager`，提供 `search_l0_by_grep(query, scope, limit)`，内部遍历 `.abstract.md` 并 grep

3. **配置**
   - `ov.conf` 中 `embedding` 改为可选；新增 `retrieval.mode: "grep" | "vector"`

### 4.3 最小改动路径

为减少对原有逻辑的侵入，建议：

1. 新增 `openviking/retrieve/grep_l0_retriever.py`，实现 `GrepL0Retriever`
2. 在 `HierarchicalRetriever` 中增加 `mode="grep"` 分支：当 `mode=="grep"` 时，不使用 `embedder` 和向量存储，只调用 `GrepL0Retriever`
3. `_recursive_search` 内对子目录的检索：在 grep 模式下，对每个子目录的 `.abstract.md` 做 grep，按匹配度排序，替代 `search_children_in_tenant`

### 4.4 存储层适配

当前向量索引存储在 VikingDB（或底层向量库）。Grep 模式不需要向量索引，但需要：

- 能遍历指定 URI 下所有 `.abstract.md` 的接口
- AGFS / VikingFS 已有 `read`、`ls`，可基于此实现「递归列出所有 .abstract.md」

若 AGFS 以文件形式存储，可直接用 `ripgrep`、`grep` 对工作目录下的 `.abstract.md` 做检索，无需改存储 schema。

---

## 五、工作量与风险

| 项目 | 估计 | 说明 |
|------|------|------|
| 理解现有检索链 | 2–3 天 | hierarchical_retriever、storage、embedder 交互 |
| 实现 GrepL0Retriever | 1–2 天 | 依赖 VikingFS 的抽象/遍历接口 |
| 改造 HierarchicalRetriever | 1–2 天 | 分支逻辑、接口统一 |
| 递归子节点 grep | 1–2 天 | 替代 search_children_in_tenant |
| 配置与测试 | 1 天 | 开关、回归测试 |
| **合计** | **约 1–2 周** | 单人，熟悉 Python 与项目结构 |

**风险**：

- AGFS / 存储层若强依赖向量索引做权限、租户过滤，需额外适配
- Grep 对语义泛化能力弱（例如「认证」vs「OAuth」），可加一层 LLM 关键词扩展
- 大规模下遍历所有 `.abstract.md` 可能较慢，可考虑为 L0 建倒排索引（仍非向量）

---

## 六、参考资料

- [OpenViking 仓库](https://github.com/volcengine/OpenViking)
- [OpenViking L0/L1/L2 说明](https://docs.bswen.com/blog/2026-03-16-openviking-context-layers-l0-l1-l2/)
- [Sirchmunk 无向量检索](https://github.com/modelscope/sirchmunk)
- [OpenViking Issue #531: Embedding 职责划分](https://github.com/volcengine/OpenViking/issues/531)
