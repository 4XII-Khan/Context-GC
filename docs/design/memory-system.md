# 记忆系统设计

> Context GC 负责**会话内压缩**与**记忆持久化**：会话存储、用户偏好、公共技能、私有化技能、用户经验均由 Context GC 统一管理与持久化。
> 依赖：[上下文压缩设计](./context-compression.md)

> **配置与开箱**：环境变量、`ContextGCOptions` 蒸馏相关字段、预设（`preset_small_chat` / `preset_agent_long_context`）、`ContextGC.create_with_file_backend`、记忆注入封装见仓库 [配置说明](../configuration.md)。

---

## 一、架构定位

```
┌─────────────────────────────────────────────────────────────────┐
│                        宿主（Host）                               │
│  push / close / get_messages  ←→  find / load_l2 / get_preferences │
└─────────────────────────────┬───────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
┌───────────────────────────────────┐    ┌───────────────────┐
│  Context GC（统一入口）             │    │   持久化后端        │
│                                    │───▶│ (文件)   │
│ • 会话内：摘要、分代、合并           │    │                   │
│ • 会话存储：L0/L1/L2 分层写入与检索（会话级）│    │ • 会话（L0/L1/L2）  │
│ • 偏好存储：用户偏好持久化与加载     │    │ • 用户偏好         │
│ • 技能与经验存储：公共技能、私有化技能、用户经验  │    │ • 技能、用户经验    │
└───────────────────────────────────┘    └───────────────────┘
```

**职责划分**：

| 组件 | 职责 |
|------|------|
| **Context GC** | 会话内压缩；**会话存储**（L0/L1/L2）；**偏好存储**；**技能与经验存储**（公共技能、私有化技能、用户经验） |
| **持久化后端** | 实际 I/O，由实现方注入（文件系统、对象存储等），Context GC 通过 Backend 协议委托写入与读取。L0/L1/L2 均为会话级存储。 |

### 1.1 记忆类型总览

Context GC 管理的三类上下文：

| 类型 | 说明 | 粒度 | 更新时机 | 用途 |
|------|------|------|----------|------|
| **对话记忆** | L0/L1/L2 均为**会话级** | 会话 | 完整会话结束时 | 跨会话检索、回溯 |
| **用户偏好** | 写作风格、习惯、纠正、显式偏好 | **完整会话** | **仅**完整会话结束时的蒸馏管道（Task Agent / 事实抽取等）写入 | 回复更贴合用户 |
| **公共技能** | 按技能组织的用法、技巧 | 技能目录 | 预置或抽取 | Agent 技能库，跨用户共享 |
| **私有化技能** | 用户专属技能 | 用户下挂技能目录 | 预置或抽取 | 该用户专属技能 |
| **用户经验** | 按任务划分的成功经验与失败反模式 | **完整会话** | 完整会话结束时抽取 | Agent 决策与执行优化 |

除对话记忆外，**用户偏好**、**公共技能**、**私有化技能**、**用户经验**是长期记忆的支柱：

- **用户偏好**：从用户多次纠正、显式表达、行为模式中抽取，使 Agent 在后续会话中自动遵从。
- **公共技能**：按技能组织的用法与技巧，跨用户共享。
- **私有化技能**：用户 ID 下挂技能，该用户专属。
- **用户经验**：按任务划分，从会话提取成功经验与失败反模式，使 Agent 在类似任务中更少犯错。

---

## 二、分层定义

L0、L1、L2 均为**会话级**，非轮次级。一个会话对应一组 L0/L1/L2。

| 层级 | 内容 | 约 token | 生成时机 | 用途 |
|------|------|----------|----------|------|
| **L0** | **对 L1 进行摘要**：拿 L1（GC 摘要列表）再做一次摘要，得简短概览 | ~50–200 | 完整会话结束时 | 快速检索、粗筛 |
| **L1** | **GC 到最后的摘要列表**：Context GC 完成摘要与合并后的轮次/段摘要列表 | 不定（多条） | 完整会话结束时 | 决策、导航、是否加载 L2 |
| **L2** | **原始内容**：最全的整场会话原始消息，持久化为 **MD 文件** | 不定 | 完整会话结束时 | 按需深度回溯 |

层级关系：**L1** = GC 到最后的摘要列表 → **L0** = 对 L1 进行摘要 → **L2** = 原始内容最全。

**L0 的定位**：L0 与 L1 都是摘要，但 L0 更短（50–200 tokens vs L1 的多条摘要列表），在**跨会话粗筛**时节省时间——宿主/用户可快速扫 L0 判断该会话是否值得深入查看 L1，而无需阅读完整的摘要列表。L0 是给**快速决策**用的（"这个会话是关于什么的"），L1 是给**详细导航**用的（"这个会话具体讨论了哪些点"）。

### 2.1 与 Context GC 的映射

| Context GC 产出 | 记忆服务层级 |
|-----------------|--------------|
| 拿 L1 进行摘要 | **L0**：对 L1 的摘要结果，用于检索 |
| `state.rounds` 中各 `RoundMeta.summary` 列表（GC 到最后的摘要列表） | **L1**：GC 完成摘要与合并后的摘要列表 |
| 完整会话的原始消息累计 | **L2**：原始内容，存为 MD 文件 |

---

## 三、数据模型

### 3.1 存储单位

L0、L1、L2 均以**会话**为存储单位，一会话一记录：

```
Session（会话）
├── session_id: str
├── created_at: datetime
├── meta: dict (可选，如 user_id、agent_id)
├── l0: str                  # 对 L1 进行摘要的结果
├── l1: str | list[str]      # GC 到最后的摘要列表（可存为 JSON 或拼接）
└── l2_uri: str              # L2 原始内容路径，指向 .md 文件

注：Context GC 内部仍按轮次做摘要与合并（用于上下文窗口管理），但持久化到记忆层时，仅在完整会话结束时产出会话级的 L0/L1/L2。
```

### 3.2 用户偏好（User Preferences）

从对话中抽取的用户相关记忆。**存储形式**：`user/{user_id}/preferences/` 目录内 **`preferences.md`**（注入用正文，不含来源）+ **`.preference_index.json`**（元数据：来源会话、时间、稳定 id），按 user_id 区分。

| 字段 | 说明 |
|------|------|
| `user_id` | 用户标识 |
| `category` | 类别，如 `writing_style`、`coding_habits`、`corrections`、`explicit_prefs` |
| `l0` | 超短摘要，如「偏好简洁回复」 |
| `l1` | 完整描述，如「用户多次纠正过冗长表述，希望回答控制在 200 字内」 |
| `source_session` | 来源会话 ID |
| `updated_at` | 最近更新时间，支持去重与覆盖 |

**路径**：`{data_dir}/user/{user_id}/preferences/preferences.md` 与 `{data_dir}/user/{user_id}/preferences/.preference_index.json`（旧版单层 `preferences.md` 会在首次访问时迁移并备份为 `preferences.md.legacy.bak`）

**抽取粒度**：面向**完整会话**，非单轮。抽取时以整场会话的摘要与消息为输入。

**抽取时机**：
- **不在 `close()` 中做规则/正则偏好检测**（已移除：误检率高，易污染 `preferences.md`）
- **仅会话结束**：`on_session_end` → `flush_distillation` 中 Task Agent 的 `submit_user_preference`、`report_factual_content` 等与蒸馏链路写入，并由后端去重/合并

### 3.3 技能与经验

技能分为**公共技能**与**私有化技能**，经验按任务划分在用户下：

- **公共技能目录**：`{data_dir}/skills/{skill_name}/`，跨用户共享。
- **私有化技能目录**：`{data_dir}/user/{user_id}/skills/{skill_name}/`，**用户 ID 下挂技能**，该用户专属。
- **用户经验目录**：`{data_dir}/user/{user_id}/experience/{task_slug}/` + **`.task_index.json`**，按任务划分（目录名为 slug）。

**用户经验**：按任务划分，无经验 ID，仅任务描述与经验文件：

| 层级 | 内容 | 形式 |
|------|------|------|
| **L0** | 任务描述（从会话提取的任务或事） | 作为目录标识或 `.abstract.md` |
| **L1** | 成功与失败经验合在一个文件 | `.overview.md`，内含成功经验与失败反模式 |

**目录布局**：

- **公共技能**：`{data_dir}/skills/{skill_name}/`
- **私有化技能**：`{data_dir}/user/{user_id}/skills/{skill_name}/`，用户 ID 下挂技能
- **用户经验**：`experience/.task_index.json` + `{task_slug}/.overview.md`

```
{data_dir}/
├── skills/                   # 公共技能目录
│   ├── search_code/
│   ├── run_shell/
│   └── edit_file/
└── user/
    └── {user_id}/
        ├── preferences.md    # 偏好
        ├── skills/           # 私有化技能目录（用户 ID 下挂技能）
        │   ├── search_code/
        │   │   ├── .meta.json    # 可选：时间戳（FileBackend）与/或会话溯源（Skill Learner）
        │   │   └── SKILL.md
        │   └── ...
        └── experience/       # 用户经验，按任务划分（目录名为 slug）
            ├── .task_index.json   # 任务索引：slug、canonical_desc、alt_descs、created_at、updated_at
            └── {task_slug}/
                └── .overview.md   # L1：成功+失败经验同一文件
```

**与实现对齐（FileBackend）**：

- 经验目录名来自 **`slug`**（由 `canonical_desc` 安全化），映射表在 **`.task_index.json`**，见 9.5.1。
- 私有化技能目录下 **`SKILL.md`** 与同目录 **`.meta.json`** 并存；时间字段由 **`save_user_skill`** 维护，会话字段由 Skill Learner 的 **`merge_skill_session_meta`** 维护，见 9.6。

**L1 .overview.md 格式**（成功与失败经验合在一个文件；每条可带来源，见 9.5.1）：

```markdown
## 成功经验
- 经验1：... (session:sess_001, 2026-03-20)
- 经验2：...

## 失败反模式
- 反模式1：... (session:sess_003, 2026-03-21)
- 反模式2：...
```

**抽取粒度**：面向**完整会话**，非单轮。从会话提取任务，再抽取成功经验与失败反模式。

**抽取时机**：完整会话 `commit` 或显式结束时，由回调分析整场会话的任务执行与结果。

### 3.4 持久化 Schema（SQLite 示例）

```sql
-- 会话（L0/L1/L2 均为会话级，一会话一记录）
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT,
    meta_json TEXT,
    l0 TEXT NOT NULL,         -- 会话级简短摘要
    l1 TEXT NOT NULL,        -- GC 到最后的摘要列表（JSON 数组或拼接）
    l2_uri TEXT              -- L2 原始内容 MD 文件路径
);

-- 会话 L0/L1/L2：{data_dir}/sessions/{session_id}/.abstract.md、.overview.md、content.md

-- L0/L1 检索（跨会话）：可对 l0、l1 建 FTS5 或关键词索引

-- 偏好与技能/经验亦可存为文件/目录，SQLite 为可选索引
-- 用户偏好：一个文件 {data_dir}/user/{user_id}/preferences.md
-- 公共技能：{data_dir}/skills/{skill_name}/
-- 私有化技能：{data_dir}/user/{user_id}/skills/{skill_name}/
-- 用户经验：{data_dir}/user/{user_id}/experience/{任务描述}/
```

**文件与目录命名**：

| 类型 | 形式 | 路径 |
|------|------|------|
| 会话 L0/L1/L2 | 一会话一目录 | `{data_dir}/sessions/{session_id}/.abstract.md`、`.overview.md`、`content.md` |
| **用户偏好** | **一个文件** | `{data_dir}/user/{user_id}/preferences.md` |
| **公共技能** | 技能目录 | `skills/{skill_name}/` |
| **私有化技能** | 用户下挂技能目录 + 可选 `.meta.json` | `user/{user_id}/skills/{skill_name}/SKILL.md`、`.meta.json` |
| **用户经验** | 任务索引 + 按 slug 子目录 + L1 经验文件 | `user/{user_id}/experience/.task_index.json`、`{task_slug}/.overview.md` |

目录布局示例：

```
{data_dir}/
├── sessions/                      # 会话根目录
│   └── {session_id}/             # 单会话目录
│       ├── .abstract.md           # L0：对 L1 的摘要，用于检索
│       ├── .overview.md           # L1：GC 到最后的摘要列表
│       └── content.md             # L2：原始对话内容
├── user/                          # 用户根目录
│   └── {user_id}/                 # 单用户目录
│       ├── preferences.md         # 用户偏好：写作风格、习惯、纠正等，一个文件
│       ├── skills/                # 私有化技能目录（用户 ID 下挂技能）
│       │   ├── search_code/
│       │   │   ├── .meta.json
│       │   │   └── SKILL.md
│       │   └── ...
│       └── experience/
│           ├── .task_index.json   # 任务 slug ↔ 描述，含时间戳字段
│           └── {task_slug}/
│               └── .overview.md   # L1：成功经验与失败反模式
└── skills/                        # 公共技能目录，跨用户共享
    ├── search_code/              # 按技能名分目录
    ├── run_shell/
    └── edit_file/
```

**L2 与 raw_messages 的关系**：
- **蒸馏管道**的输入是 **raw_messages**（内存中的 `[{role, content, tool_calls?}...]`，带消息索引作 ID），与 L2 内容同源
- **L2 持久化**：将会话序列化为 `content.md`（可读）或 `content.json`（机器可解析）。从持久化恢复蒸馏时，需 `load_session_l2` 返回 `list[dict]`，故建议存 JSON 或可解析的 MD 格式
- 会话刚结束时，直接使用 Context GC 内存中的 `_full_session_raw`，无需从文件加载

**L2 content.md 格式**：将整场会话的 `[user, assistant, tool...]` 序列化为可读 Markdown：

```markdown
## user
请帮我实现一个登录接口。

## assistant
好的，我将使用 FastAPI 实现...

## tool (search_code)
调用 search_code，参数：query=login

## tool_result
Found: app/auth.py ...
```

---

## 四、Context GC 接口（会话、偏好、经验存储）

Context GC 注入 `MemoryBackend` 时，除会话内压缩外，还负责**会话存储**、**偏好存储**、**经验存储**，对外提供统一 API。

### 4.1 核心接口

```python
from pathlib import Path
from typing import Protocol, Optional, Any
from dataclasses import dataclass

# ContextGCOptions：见 ./context-compression.md；蒸馏相关字段见本文第八章与 ../configuration.md
# LifecycleConfig：实现位于 context_gc.memory（记忆注入容量与 TTL 策略，见 9.8）

@dataclass
class UserPreference:
    user_id: str
    category: str
    l0: str
    l1: str | None = None

@dataclass
class UserExperience:
    """用户经验：L0 任务描述，L1 经验文件（成功+失败合在一个文件）"""
    task_desc: str             # 任务描述（目录名，非 ID）
    success: bool              # 成功任务经验 / 失败任务经验
    content: str               # 经验或反模式内容（写入 L1 .overview.md）
    source_session: str | None = None

class MemoryBackend(Protocol):
    """持久化后端协议，由实现方注入，Context GC 委托会话/偏好/技能/经验存储"""
    # 会话存储（L0/L1/L2 均为会话级，完整会话结束时写入）
    async def save_session(self, session_id: str, l0: str, l1: str, l2_uri: str) -> None: ...
    async def search_sessions(self, query: str, limit: int) -> list[dict]: ...
    async def load_session_l1(self, session_id: str) -> Optional[list[str]]: ...
    async def load_session_l2(self, session_id: str) -> Optional[list[dict]]: ...
    # 偏好存储
    async def save_user_preferences(self, user_id: str, prefs: list[UserPreference], session_id: str) -> None: ...
    async def load_user_preferences(self, user_id: str, category: str | None) -> list[UserPreference]: ...
    # 公共技能
    async def load_skills(self, skill_name: str | None) -> list[dict]: ...
    # 私有化技能（用户 ID 下挂技能）
    async def load_user_skills(self, user_id: str, skill_name: str | None) -> list[dict]: ...
    # 用户经验（按任务划分）
    async def save_user_experience(self, user_id: str, experiences: list[UserExperience], session_id: str, *, use_fuzzy_task_match: bool = True) -> None: ...
    async def load_user_experience(self, user_id: str, task_desc: str | None, *, use_fuzzy_task_match: bool = True) -> list[UserExperience]: ...
    async def load_user_experience_task_index(self, user_id: str) -> list[dict]: ...  # 与 .task_index.json 一致；无实现可返回 []

class ContextGC:
    """上下文回收：会话内压缩 + 会话/偏好/经验存储（注入 backend 时启用持久化）"""

    def __init__(
        self,
        options: ContextGCOptions,
        *,
        session_id: str = "",
        backend: Optional[MemoryBackend] = None,  # 注入则启用会话/偏好/经验存储
        persist_l2: bool = True,
    ):
        """
        Args:
            options: 摘要、分代、蒸馏等配置（见 ``ContextGCOptions`` 与 [配置说明](../configuration.md)）
            session_id: 当前会话 ID（持久化时必填）
            backend: 持久化后端，注入后 Context GC 负责会话/偏好/经验存储
            persist_l2: 是否持久化原始消息
        """
        ...

    @classmethod
    def create_with_file_backend(
        cls,
        data_dir: str | Path,
        *,
        session_id: str = "",
        options: ContextGCOptions | None = None,
        persist_l2: bool = True,
        **with_env_kwargs: Any,
    ) -> "ContextGC":
        """
        创建 ``data_dir`` 目录（若不存在）、挂载 ``FileBackend`` 的实例。
        未传 ``options`` 时等价于 ``ContextGCOptions.with_env_defaults(data_dir=..., **with_env_kwargs)``。
        """
        ...

    async def close(self) -> None:
        """轮次结束：摘要+分代+合并 + checkpoint 写入（若到达间隔）。用户偏好仅由会话结束时的蒸馏管道写入。"""
        ...

    # 会话存储（L0/L1/L2 会话级，仅 on_session_end 时持久化）
    async def find(self, query: str, limit: int = 10) -> list[dict]:
        """跨会话检索，按 L0/L1 匹配，返回 {session_id, l0, l1, l2_uri, score}"""
        ...

    async def load_session_l1(self, session_id: str) -> list[str] | None:
        """加载 L1（GC 到最后的摘要列表）"""
        ...

    async def load_session_l2(self, session_id: str) -> list[dict] | None:
        """按需加载 L2 原始内容"""
        ...

    # 偏好存储
    async def get_user_preferences(self, user_id: str, category: str | None = None) -> list[UserPreference]:
        """加载用户偏好，用于生成更贴合用户的回复"""
        ...

    # 公共技能
    async def get_skills(self, skill_name: str | None = None) -> list[dict]:
        """加载公共技能"""
        ...

    # 私有化技能（用户 ID 下挂技能）
    async def get_user_skills(self, user_id: str, skill_name: str | None = None) -> list[dict]:
        """加载该用户的私有化技能"""
        ...

    # 用户经验
    async def get_user_experience(self, user_id: str, task_desc: str | None = None) -> list[UserExperience]:
        """加载用户经验（按任务划分的成功经验与失败反模式）"""
        ...

    async def build_memory_injection_text(
        self,
        user_id: str,
        *,
        current_query: str = "",
        config: LifecycleConfig | None = None,
    ) -> str:
        """从 backend 拉取偏好/经验/技能，经 ``build_memory_injection`` 拼成可注入 system 的文本。"""
        ...

    async def on_session_end(self, user_id: str, agent_id: str, ...) -> dict:
        """完整会话结束时：L0/L1/L2 持久化 → 蒸馏管道 → 清理 checkpoint（见 5.3）。返回 dict（含 ``l0``、``distillation`` 等）。"""
        ...
```

### 4.2 完整会话结束的流水线：L0/L1/L2 + 蒸馏管道

**默认流水线**：完整会话结束时，依次执行 **1) L0/L1/L2 持久化** + **2) 蒸馏管道**（Task Agent → 蒸馏 → Skill Learner）。偏好、经验、私有化技能均由蒸馏管道产出，无需单独的 extract 回调。

```python
# 完整会话结束时的统一流程
async def on_session_end(self, user_id: str, agent_id: str, ...) -> None:
    # Step 1: L0/L1/L2 持久化
    l1 = [r.summary for r in self.state.rounds]
    l0 = await generate_l0(self.session_id, l1)
    l2_uri = await self._write_l2(self.session_id, self._full_session_raw)
    if self.backend:
        await self.backend.save_session(...)

    # Step 2: 蒸馏管道（L2 = raw_messages）
    raw_messages = self._full_session_raw  # 内存中的 [user, assistant, tool...]，带 ID
    # on_session_end 会将 ContextGCOptions 上的蒸馏参数打成 kwargs 传入（与默认管道一致）：
    # min_messages, task_agent_max_iterations, skill_learner_max_iterations,
    # experience_task_assign_mode, dedup_strategy —— 对应选项字段 flush_*（见第八章）。
    # 内置默认路径：len(raw_messages) >= options.flush_min_messages 才调用 flush_distillation。
    result = await flush_distillation(
        session_id=self.session_id,
        user_id=user_id,
        messages=raw_messages,  # 即 L2 对应的原始结构
        backend=self.backend,
        options=self.options,
        experience_task_assign_mode="llm",  # 或 "heuristic"；亦可仅设 options.flush_experience_task_assign_mode
        ...
    )
    # 管道内部：Task Agent → 蒸馏 → 写入 preferences.md、experience/、skills/
    # 若 options.flush_distillation_trace 为 False，返回给宿主的 result 会去掉 trace 键以减小体积

    # Step 3: 清理 checkpoint（会话已正常完成）
    self._cleanup_checkpoint()
```

**宿主可注入**：
- `generate_l0`：拿 L1 做摘要，得到 L0
- `flush_distillation`：未注入时使用默认管道（复用 AsMe task_agent、distillation、skill_learner）；注入则可替换为自定义抽取逻辑。**自定义回调应使用 `**kwargs` 接收并转发** `min_messages`、`task_agent_max_iterations` 等，与 `options` 一并由 `on_session_end` 注入。

**新会话开始**时，宿主加载偏好、经验、技能，注入 prompt：

```python
from context_gc.memory import LifecycleConfig

# 方式 A：一键封装（从 backend 拉取并按 LifecycleConfig 截断）
injection = await gc.build_memory_injection_text(user_id, current_query="...", config=LifecycleConfig())

# 方式 B：手动组装（与 memory.build_memory_injection 一致）
prefs = await gc.get_user_preferences(user_id)
exps = await gc.get_user_experience(user_id)
skills = await gc.get_user_skills(user_id)  # 私有化技能
system = build_memory_injection(
    preferences=prefs, experiences=exps, skills=skills, max_tokens=2000, estimate_tokens=...
)
```

### 4.3 宿主调用流程

#### 何为「开箱路径」

**开箱路径**指：宿主**尽量少写胶水代码**，用库内预设与工厂即可跑通 **文件持久化 + 默认 OpenAI 兼容客户端**（摘要、L0、蒸馏里的 `call_llm` 等共用 `CONTEXT_GC_*` 环境变量）。与之相对的是**通用路径**：自行构造 `ContextGCOptions`（自定义回调）、自行 new `FileBackend` 或其它 `MemoryBackend` 再 `ContextGC(..., backend=...)`。

开箱路径三件套：

| 环节 | 作用 |
|------|------|
| **`preset_small_chat` / `preset_agent_long_context`** | 在 `with_env_defaults()` 之上套一层**常用参数组合**（窗口大小、checkpoint 间隔、合并梯度、`flush_min_messages` 等），避免每个项目从零调 `ContextGCOptions`。 |
| **`create_with_file_backend`** | **创建数据目录**、挂载 **`FileBackend`**、组装 `ContextGC`；未传 `options` 时内部直接 `with_env_defaults(data_dir=...)`，省去「mkdir + new Backend + 填 data_dir」的样板代码。 |
| **环境变量 + `with_env_defaults`**（由预设/工厂间接调用） | 把 API Key、模型、网关以及部分蒸馏开关（如 `CONTEXT_GC_FLUSH_MIN_MESSAGES`）从代码里挪到 `.env`，见 [配置说明](../configuration.md)。 |

**开箱路径**（文件后端 + 默认 OpenAI 兼容适配器）示例：

```python
from context_gc import ContextGC, ContextGCOptions

# 预设：小型对话（较密 checkpoint、flush_min_messages=2）
options = ContextGCOptions.preset_small_chat()

# 或长上下文智能体（更大 max_input_tokens、宽合并梯度 LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS）
# options = ContextGCOptions.preset_agent_long_context()

# 工厂：创建 data_dir + FileBackend（未传 options 时内部 with_env_defaults）
gc = ContextGC.create_with_file_backend("./data", session_id="sess_001", options=options)
```

**通用初始化**：注入任意 `MemoryBackend`：

```python
# 初始化：注入 backend 后 Context GC 负责会话/偏好/经验存储
gc = ContextGC(options, session_id="sess_001", backend=sqlite_backend)

# 每轮
gc.push([{"role": "user", "content": "..."}])
messages = await gc.get_messages([{"role": "user", "content": "..."}])
response = await llm.chat(messages)
gc.push({"role": "assistant", "content": response})

await gc.close()  # 轮次结束：摘要+分代+合并 + checkpoint（偏好仅 on_session_end 蒸馏写入）

# 完整会话结束时：生成 L0/L1/L2、运行蒸馏管道（Task Agent → 蒸馏 → 经验 + Skill Learner）
end = await gc.on_session_end(user_id, agent_id)  # generate_l0 / flush_distillation 可选覆盖；返回值含 distillation

# 新会话开始：记忆注入（二选一）
# 方式 A：一键——gc 从 backend 拉 prefs / exps / skills，再调 build_memory_injection（token 上限用 LifecycleConfig）
from context_gc.memory import LifecycleConfig
injection_a = await gc.build_memory_injection_text(
    user_id, current_query="用户本轮第一句话…", config=LifecycleConfig()
)
# 方式 B：手动——自行 get_* 再调用 build_memory_injection（与 A 等价，便于自定义筛选或合并顺序）
from context_gc import build_memory_injection
prefs = await gc.get_user_preferences(user_id)
exps = await gc.get_user_experience(user_id)
skills = await gc.get_user_skills(user_id)
injection_b = build_memory_injection(
    preferences=prefs,
    experiences=exps,
    skills=skills,
    max_tokens=2000,
    estimate_tokens=gc.options.estimate_tokens,
)
# 将 injection_a 或 injection_b 拼进 system / 首条 user 前导，供 LLM 参考

# 跨会话检索（L0/L1/L2 均为会话级）
hits = await gc.find("之前的 OAuth 讨论")  # 按 L0/L1 检索会话
for h in hits:
    session_l1 = await gc.load_session_l1(h["session_id"])  # L1
    if needs_detail(session_l1):
        raw = await gc.load_session_l2(h["session_id"])  # L2 原始内容
```

---

## 五、Context GC 内部持久化流程

L0/L1/L2 均为**会话级**，最终持久化在完整会话结束时完成。但为**防止崩溃丢失数据**，引入轻量级 checkpoint 机制。

### 5.1 Checkpoint（崩溃恢复）

**问题**：若进程崩溃（OOM、断网、用户强退），内存中的 raw_messages 与摘要列表全部丢失，长会话代价极高。

**机制**：每隔 N 轮 `close()` 后，将当前状态增量写入 checkpoint 文件：

```
{data_dir}/sessions/{session_id}/
├── .checkpoint.json          # 增量 checkpoint
└── content.md                # 最终 L2（on_session_end 时写入）
```

**`.checkpoint.json` 内容**：

```json
{
  "session_id": "sess_001",
  "round_count": 42,
  "raw_messages_appended_to": 42,
  "summaries": ["摘要1", "摘要2", "..."],
  "gen_scores": [3, -1, 2, "..."],
  "last_checkpoint_at": "2026-03-20T15:30:00Z"
}
```

**写入策略**：

| 数据 | 写入方式 |
|------|----------|
| raw_messages | 增量追加到 `content.md`（append-only），每轮 `close()` 后追加本轮新消息 |
| summaries + gen_scores | 每 `checkpoint_interval` 轮全量覆写 `.checkpoint.json` |

**恢复流程**：下次以相同 `session_id` 初始化时，若检测到 `.checkpoint.json` 存在且无对应的已完成 session 记录，从 checkpoint 恢复 `state.rounds` 和 raw_messages，继续会话。

**配置**：

```yaml
context_gc:
  checkpoint_interval: 5          # 每 5 轮写一次 checkpoint；0 = 禁用
  checkpoint_raw_messages: true   # 是否每轮增量追加 raw_messages
```

**与 MemoryBackend 的关系**：Checkpoint 是 ContextGC 的**内部行为**，不经过 `MemoryBackend` 协议。原因：checkpoint 需要在每轮 `close()` 时低延迟写入，而 Backend 协议面向会话级持久化（session end），二者生命周期不同。Checkpoint 直接写入 `{data_dir}/sessions/{session_id}/` 目录（文件 I/O），不依赖 Backend 实现方式。

### 5.2 用户偏好为何不在 `close()` 中做规则抽取（已移除）

**背景**：曾设计在 `close()` 中对用户消息做关键词/正则匹配，零 LLM 成本即时写入 `preferences.md`。

**问题（实测）**：规则匹配**误检率高**，会把表格片段、否定句残片、任务描述等扫进偏好文件，污染后续注入与蒸馏（见生产/评测中的 `preferences.md` 质量反馈）。

**当前策略**：
- **删除** `memory/preference.py` 及 `close()` 内一切正则/规则偏好检测逻辑。
- **唯一写入路径**：`on_session_end` → `flush_distillation`（Task Agent `submit_user_preference`、事实类工具产出等）→ `backend.save_user_preferences`。
- **兼容字段**：`on_session_end` 返回值中的 `detected_preferences` **恒为 0**；蒸馏侧统计可用 `distillation.preferences_written`（`flush_distillation` 返回）。

**权衡**：偏好仅在会话落盘并跑完蒸馏后进入下一会话生效；换取偏好条目**可控、可溯源**（与任务/事实抽取同源）。

### 5.3 正常会话结束流程

**每轮 `close()`**：摘要、分代、合并（内存）+ checkpoint 写入（若到达间隔）。

**完整会话 `on_session_end`**：
1. **L1** 直接从 `state.rounds` 取摘要列表（GC 到最后的摘要列表）
2. 拿 L1 进行摘要，生成 **L0**
3. 将累计的 raw_messages 序列化为 MD 文件，得到 l2_uri
4. 调用 `backend.save_session(session_id, l0, l1, l2_uri)`
5. 运行蒸馏管道，抽取并持久化用户偏好与用户经验
6. 清理 `.checkpoint.json`（会话已正常完成）

---

## 六、会话检索实现（非向量）

跨会话检索时，按 L0、L1 匹配。为减少向量依赖，可采用：

| 方案 | 说明 |
|------|------|
| **SQLite FTS5** | 对 sessions 表 l0、l1 列建全文索引，`SELECT ... WHERE l0 MATCH ? OR l1 MATCH ?` |
| **关键词 grep** | 从 query 提取词，对 l0、l1 做 `in` 或正则匹配，按命中数打分 |
| **BM25** | 对 l0、l1 建倒排索引，BM25 打分（如用 `rank_bm25` 库） |

### 6.1 会话过期清理

配置 `session_ttl_days`（如 90 天）后，过期会话需清理以控制存储量。

**清理机制**：

| 项 | 动作 |
|------|------|
| **触发时机** | 宿主显式调用 `gc.cleanup_expired_sessions()` 或配置定时任务（如每日凌晨） |
| **判定** | `created_at` + `session_ttl_days` < 当前时间 |
| **清理范围** | 删除 sessions 表记录 + `{data_dir}/sessions/{session_id}/` 目录（含 L0/L1/L2 文件） |
| **保护** | 若该会话的经验/偏好已被蒸馏写入用户目录，**不连带删除**——经验/偏好有独立 TTL（9.8） |
| **日志** | 记录清理的 session_id 与文件数，便于审计 |

```python
async def cleanup_expired_sessions(self) -> int:
    """清理过期会话，返回清理数量"""
    ...
```

---

## 七、实现清单

| 组件 | 路径 | 说明 |
|------|------|------|
| 持久化协议 | `src/context_gc/storage/backend.py` | `MemoryBackend` Protocol，会话/偏好/技能/用户经验存储接口 |
| 文件后端 | `src/context_gc/storage/file_backend.py` | L0/L1/L2、偏好、技能、经验（目录布局见本文 3.x） |
| Context GC 主类 | `src/context_gc/core.py` | 会话内压缩；`ContextGCOptions` 蒸馏字段、`create_with_file_backend`、`build_memory_injection_text` |
| Checkpoint 管理 | `src/context_gc/storage/checkpoint.py` | `.checkpoint.json` 读写、恢复、raw_messages 增量（5.1） |
| 默认 LLM 适配器 | `src/context_gc/defaults.py` | `with_env_defaults` 绑定的摘要/L0/蒸馏 `call_llm`；环境变量见 [配置说明](../configuration.md) |
| 用户偏好写入 | `src/context_gc/distillation/flush.py` + Task Agent 工具 | **仅**蒸馏管道内 `save_user_preferences`；无 `close()` 规则检测（见 5.2） |
| 记忆生命周期与注入 | `src/context_gc/memory/` | `LifecycleConfig`、`build_memory_injection`（9.8） |
| 会话过期清理 | `src/context_gc/storage/cleanup.py`；`ContextGC.cleanup_expired_sessions` | 按 TTL 清理过期会话目录 |
| 蒸馏管道 | `src/context_gc/distillation/flush.py` | `flush_distillation`：Task Agent → 蒸馏 → 经验写入 + Skill Learner |
| 宿主示例 | `examples/` | 完整调用示例，含 on_session_end、flush_distillation |

---

## 八、配置示例

以下为**概念 YAML**，便于与产品配置对齐；**Python 运行时以 `ContextGCOptions` 字段与 `.env` 为准**。完整环境变量与 API 字段见 [配置说明](../configuration.md)。

```yaml
context_gc:
  # 会话内压缩（见 ./context-compression.md）
  # ...
  # 持久化（注入 backend 时）
  backend: sqlite
  db_path: ./data/memory.db
  data_dir: ./data
  session_retrieval: fts5
  session_ttl_days: 90

  # Checkpoint（崩溃恢复，见 5.1）→ ContextGCOptions.checkpoint_interval / checkpoint_raw_messages
  checkpoint_interval: 5          # 每 5 轮写一次 checkpoint；0 = 禁用
  checkpoint_raw_messages: true   # 是否每轮增量追加 raw_messages

  # 蒸馏管道（完整会话结束时）→ ContextGCOptions.flush_* 与 flush_distillation 形参
  learning:
    min_messages_for_learning: 4       # → flush_min_messages；环境变量 CONTEXT_GC_FLUSH_MIN_MESSAGES
    task_agent_max_iterations: 20      # → flush_task_agent_max_iterations
    skill_learner_max_iterations: 10   # → flush_skill_learner_max_iterations
    experience_task_assign_mode: llm   # → flush_experience_task_assign_mode（llm | heuristic）
    dedup_strategy: keyword_overlap      # → flush_dedup_strategy
    include_distillation_trace: false    # → flush_distillation_trace；环境变量 CONTEXT_GC_FLUSH_INCLUDE_TRACE
    distillation_max_tokens: 50000       # 蒸馏管道总 token 预算（实现侧参数，见 flush 模块）
  auto_distill_on_session_end: true      # 未注入 flush_distillation 时由 on_session_end 自动调用默认管道

  # 记忆生命周期（见 9.8）→ LifecycleConfig
  memory_lifecycle:
    preference_ttl_days: 90
    experience_ttl_days: 180
    skill_max_entries: 30
    memory_inject_max_tokens: 2000

  # gen_score 衰减（见 11.1）→ ContextGCOptions.gen_score_decay / gen_score_clamp
  gen_score_decay: 0.9              # 每次打分时的衰减系数
  gen_score_clamp: [-5, 5]          # 分数上下限
```

**预设（代码）**：`ContextGCOptions.preset_small_chat()`（小窗口、密 checkpoint、`flush_min_messages=2`）、`preset_agent_long_context()`（大窗口、宽合并梯度 `LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS`）。

---

## 九、记忆蒸馏与抽取详细设计

参考本地 [AsMe](../../AsMe)（`superman/task/`）的记忆蒸馏流程：**Task Agent → 蒸馏 → Skill Learner**。Context GC 在完整会话结束时，以 **L2**（原始对话记忆）为主要输入，依次抽取**偏好**、**经验**、**私有化技能**。L0、L1 用于检索与导航，蒸馏管道以 L2 为核心。

### 9.1 三阶段抽取管道

```
完整会话结束
    │
    ├── 输入：L2（原始对话内容）、L1 可选作为先前进度补充
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 1：Task Agent（任务提取）                                     │
│ 以 L2（原始对话记忆）为输入，Task Agent 多轮分析，使用 insert_task/  │
│ update_task、append_messages_to_task 等工具 CRUD 任务，最后 finish  │
│ 直接复用 AsMe task_agent、task_tools、task_prompt           │
└─────────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 2：蒸馏（Distillation）                                       │
│ 对每个 success/failed 任务调用 LLM：                               │
│ • 成功任务 → report_success_analysis（approach, key_decisions,     │
│   generalizable_pattern）或 report_factual_content（facts→偏好）  │
│ • 失败任务 → report_failure_analysis（failure_point, prevention,   │
│   what_should_have_been_done）                                    │
│ • 琐碎任务 → skip_learning                                        │
│ 参考 AsMe：distill_prompt.py、distill_tools.py              │
└─────────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 3：写入（并行）                                               │
│ 蒸馏结果同时驱动三路输出：                                          │
│ • 偏好：Task Agent submit_user_preference + report_factual_content │
│   → 合并到 user/{user_id}/preferences.md                          │
│ • 经验：从 distilled_text 提炼成功/失败条目 → 按 task_desc 写入     │
│   user/{user_id}/experience/{任务描述}/.overview.md                │
│ • 私有化技能：Skill Learner Agent（复用 AsMe）→ 写入         │
│   user/{user_id}/skills/{skill}/SKILL.md                         │
│   与同目录 `.meta.json`（程序写入 `created_at` / `updated_at`）   │
│ 经验与 Skill Learner 均来自蒸馏结果，可并行写入，无相互依赖。          │
└─────────────────────────────────────────────────────────────────┘
```

### 9.2 Task Agent（阶段 1）

**复用**：AsMe 的 Task Agent 完整实现——`task_agent.py`、`task_tools.py`、`task_prompt.py`。

**与 AsMe 一致**：AsMe 也是用户**点击蒸馏**后，在 `flush_session` 中一次性运行完整管道，传入完整 messages（原始对话）。Context GC 同理，在会话结束时传入 **L2**（原始对话记忆），Task Agent 多轮调用工具直至 `finish`。

**输入**：**L2**（原始对话内容，即 raw_messages，带 ID）、tool_calls（可选）、已有用户偏好（去重用）。L1 可选作为「先前进度」补充（对应 AsMe 的 existing_tasks.progresses）。

**输出**：`[TaskSchema]`，含 task_desc、status（success/failed/pending/running）、raw_message_ids、progresses、user_preferences（submit_user_preference 收集）

**工具**（`task_tools.py`）：`report_thinking`、`insert_task`、`update_task`、`append_messages_to_task`、`append_messages_to_planning_section`、`append_task_progress`、`submit_user_preference`、`finish`

**Prompt**：直接复用 `TASK_SYSTEM_PROMPT`（`task_prompt.py`）。输入组装用 `pack_task_input`，将 **L2** 映射为「当前消息（含 ID）」；L1 若有则作为「先前进度」。

### 9.3 蒸馏（阶段 2）

对每个 `status in (success, failed)` 的任务，调用蒸馏 LLM，使用 **Tool Calling** 约束输出格式。Tool schema 见 AsMe `distill_tools.py`。

**成功任务 System Prompt**（`SUCCESS_DISTILLATION_PROMPT`，AsMe `distill_prompt.py`）：

```
分析这个成功完成的任务，并选择恰当的工具：

**使用 skip_learning**：当任务琐碎时——例如简单事实查询、闲聊、一次性计算、通用问答、琐碎状态检查。
若列出了学习空间的技能，且任务内容与任一技能相关，则不算琐碎。skip 时 reason 需简要说明为何琐碎。

**使用 report_success_analysis**：当任务涉及多步骤流程、调试、配置或重要决策过程时：
- task_goal：用户想要什么（1 句）
- approach：有效的策略（2–3 句），将作为技能学习的 Principle 来源
- key_decisions：关键决策或动作（列表，每项 1 句），将作为技能学习的 Steps 来源
- generalizable_pattern：可复用的 SOP（2–3 句），供技能学习提炼为 When to Apply 和条目内容

**使用 report_factual_content**：当任务主要是记录信息——人物、事实、偏好、实体或领域知识时：
- task_goal：简要背景（1 句）
- facts：简洁、自洽的第三人称事实陈述列表，供技能学习写入用户偏好类技能

选择最匹配的工具。不要将简单内容包装成虚假流程。
「用户」指发送消息的人（role: user）。
```

**失败任务 System Prompt**（`FAILURE_DISTILLATION_PROMPT`）：

```
分析这个失败的任务，并调用 report_failure_analysis，填入：

- task_goal：用户想要什么（1 句）
- failure_point：方法在何处出错（2–3 句）
- flawed_reasoning：错误的假设或不当行为（2–3 句）
- what_should_have_been_done：正确的做法（2–3 句），供技能学习提炼为 Correct Approach
- prevention_principle：防止此类失败的通用规则（1–2 句），供技能学习提炼为 Prevention 条目

聚焦可执行的教训，而非归咎。
「用户」指发送消息的人（role: user）。
```

**蒸馏输入组装**（`pack_distillation_input`）：`已完成任务（状态、描述、进度）` + `本会话全部任务摘要` + `任务相关消息` + 可选 `学习空间技能`。

### 9.4 偏好提取

**来源**：
1. 蒸馏阶段 `report_factual_content` 的 `facts`
2. 任务提取阶段可选的 `submit_user_preference`（若采用与 AsMe 类似的 Task Agent 结构）

**写入**：追加到 `user/{user_id}/preferences.md`，按 category 分节或线性追加，去重可由写入逻辑处理。

### 9.5 经验写入

**输入**：蒸馏后的成功/失败分析（`distilled_text`），从 `report_success_analysis`、`report_failure_analysis` 解析出 approach、key_decisions、generalizable_pattern 或 failure_point、prevention_principle 等

**实现入口**：`distillation/experience_writer.py` 中 **`write_experiences`**，由 `flush_distillation` 在阶段 3 调用。

**与蒸馏阶段的关系**：

- **蒸馏 LLM**（`process_distillation` / `pack_distillation_input`）**不**加载用户历史经验；仅消费当前任务与相关消息（及可选技能描述列表）。
- **写入前**：按每条待写入经验的 **`task_desc`** 调用 **`backend.load_user_experience(user_id, task_desc=...)`**，只读取**同一任务桶**下已有条目，用于条目级去重（`exact` / `keyword_overlap` / `none`），避免拉取用户全部经验池。

**任务描述归并（与 FileBackend 对齐）**：

| 模式 | 参数 | 行为 |
|------|------|------|
| **heuristic**（默认，`write_experiences` 单测等） | `task_assign_mode="heuristic"` | 由 FileBackend 在 `save_user_experience` / `load_user_experience` 使用 **`use_fuzzy_task_match=True`**（默认）：`canonical_desc` / `alt_descs` 精确匹配（忽略大小写）+ **Jaccard > 0.8** 的模糊合并。 |
| **llm** | `task_assign_mode="llm"` + **`call_llm`** | 先 **`load_user_experience_task_index`** 读取 `.task_index.json`，再经 **`task_assignment_llm.assign_experience_task_descs_with_llm`** 一次（批量）调用 LLM，将本批 `task_desc` 映射到已有 **`canonical_desc`** 或新建 canonical；写入/读取经验时 **`use_fuzzy_task_match=False`**，避免启发式与模型结论冲突。解析失败时回退为恒等映射。 |

**`flush_distillation`** 形参 **`experience_task_assign_mode: "llm" | "heuristic"`**（默认 **`"llm"`**，在提供 `call_llm` 时生效；无 `call_llm` 时整条管道跳过，与既有逻辑一致）。宿主若需节省一次模型调用可设 **`ContextGCOptions.flush_experience_task_assign_mode="heuristic"`**（`on_session_end` 会注入到 `flush_distillation`），或仍在回调中显式传参覆盖。

**与 Skill Learner 的关系**：经验写入与 Skill Learner **并行**，均消费蒸馏结果。二者的**信息去重边界**：

| 维度 | 经验（experience） | 技能（skills） |
|------|-------------------|----------------|
| 组织方式 | 按**任务**（experience/{任务描述}/） | 按**技能领域**（skills/{技能名}/） |
| 内容粒度 | **具体案例**：记录某次任务的做法、结果、上下文（含 session 来源） | **抽象规则**：SOP、最佳实践、反模式（不引用具体会话） |
| 用途 | Agent 遇到相似任务时参考历史做法 | Agent 执行任何任务时遵循的通用能力 |
| 冗余说明 | 同一蒸馏结果会同时写入经验（具体案例）和技能（抽象规则），这是**刻意冗余**——不同检索入口需要不同粒度 |

**输出**：按 `task_desc` 组织目录，每任务一个 `.overview.md`：

```markdown
## 成功经验
- [经验条目 1]
- [经验条目 2]

## 失败反模式
- [反模式条目 1]
- [反模式条目 2]
```

**目录**：`user/{user_id}/experience/{task_slug}/.overview.md`，映射由 **`experience/.task_index.json`** 维护（见 9.5.1）。

**task_desc 文件系统安全**：由 **`_safe_slug`**（截断 + sanitize + 可选 hash 后缀）生成 **`slug`**；**`canonical_desc`** 存索引中的规范描述。

#### 9.5.1 经验去重与冲突处理

> 本节设计对应 10.4「局限与待验证」中的经验去重与冲突项；设计已完成，实现后需验证效果。

同一任务在多会话中反复出现时，需处理**去重**（避免重复条目）与**冲突**（矛盾经验如何合并）。

**场景**：

| 场景 | 示例 | 处理目标 |
|------|------|----------|
| 重复成功经验 | 会话 A、B 均「实现登录」成功，蒸馏出相似 SOP | 去重，不重复写入 |
| 任务描述变异 | 「做登录接口」「实现登录功能」「login API」实为同类任务 | 归入同一目录，避免碎片化 |
| 成功与失败并存 | 会话 A 成功、会话 B 同任务失败 | 成功经验与失败反模式分块，均保留 |
| 矛盾经验 | A：用 JWT 最佳；B：用 session 更好 | 冲突策略：追加/覆盖/并存 |

**任务描述归一化**（解决「任务描述变异」）：

- **索引文件**：`{data_dir}/user/{user_id}/experience/.task_index.json`，JSON 数组。每项含 **`slug`**（目录名）、**`canonical_desc`**、**`alt_descs`**（别称列表），以及程序写入的 **`created_at` / `updated_at`**（UTC ISO-8601，秒精度；新建条目时二者相同；**每次**向该 `slug` 写入新经验时刷新 **`updated_at`**；旧数据缺字段时在下一次写入时由 `_touch_task_index_entry` 补全）。
- **启发式归并**（`use_fuzzy_task_match=True`）：先按 `canonical_desc` / `alt_descs` 做**忽略大小写**精确匹配；若无命中，再对 `canonical_desc` 做 **Jaccard > 0.8**（中英分词）模糊命中同一 `slug`。
- **LLM 归并**（`task_assign_mode="llm"`）：由模型在索引列表上判定 reuse / new，写入侧关闭模糊匹配（`use_fuzzy_task_match=False`），见 9.5 表格。
- **并发安全**：`.task_index.json` 为单文件，多会话同时结束仍存在写冲突风险。当前 FileBackend 采用**临时文件 + `rename`** 写入索引；**文件锁**（如 `fcntl.flock`）为设计建议，**单用户单写者**仍为主要假设，多写者需宿主串行化。

**经验条目去重**（解决「重复成功经验」）：

- 写入前读取既有 `.overview.md`，解析为结构化条目
- 对每条新经验，检查是否与已有条目**语义重复**，策略可配置：
  - `exact`：完全一致才跳过
  - `keyword_overlap`：关键词重叠率 > 阈值（如 80%）则跳过
  - `llm_similar`：调用 LLM 判断「是否与已有某条等价」，是则跳过
- 未重复则追加；追加时带 `(session_id, YYYY-MM-DD)` 便于追溯

**冲突策略**（解决「矛盾经验」）：

| 策略 | 行为 | 适用 |
|------|------|------|
| `append_with_source` | 始终追加，标注来源会话与时间；不合并 | 默认，可追溯，适合调试 |
| `newer_wins` | 同类型（成功/失败）冲突时，用新会话覆盖旧条 | 偏好最新结论 |
| `keep_both` | 冲突时两条都保留，标注「来自不同会话，视场景选用」 | 保留多元视角 |
| `llm_merge` | 冲突时调 LLM 合并为一条综合描述（如「JWT 与 session 均可，视需求选用」） | 追求简洁，成本较高 |

**配置项**：

```yaml
# 与代码对齐的等价配置（部分为 flush / write_experiences 参数名）
experience:
  task_assign_mode: "heuristic" | "llm"   # flush_distillation.experience_task_assign_mode；llm 需 call_llm
  dedup_strategy: "exact" | "keyword_overlap" | "none"   # write_experiences；llm_similar 为设计项，尚未接入
  dedup_threshold: 0.8   # keyword_overlap 时 Jaccard 阈值
  conflict_strategy: "append_with_source" | "newer_wins" | "keep_both" | "llm_merge"   # 策略设计见上表；实现以追加为主
```

**写入格式**（带来源，便于冲突策略与审计）：

```markdown
## 成功经验
- 经验1：... (session:sess_001, 2026-03-20)
- 经验2：... (session:sess_005, 2026-03-22)

## 失败反模式
- 反模式1：... (session:sess_003, 2026-03-21)
```

### 9.6 私有化技能学习（Skill Learner）

**输入**：合并后的蒸馏结果（success 分析 + failure 分析 + 偏好文本），格式同 AsMe 的 `distilled_context`。外加 `可用技能` 列表（来自 `user/{user_id}/skills/` 及可选公共技能）。

**输出**：创建或更新 `user/{user_id}/skills/{skill_name}/SKILL.md`。

**Skill Learner System Prompt 要点**（AsMe `skill_learner_prompt.py` SKILL_LEARNER_SYSTEM_PROMPT）：

```
你是自学习技能 Agent。接收预蒸馏的上下文，并更新用户私有技能。

**输入来源对应**：
- Task Analysis (Success)：approach → Principle；key_decisions → Steps；generalizable_pattern → When to Apply
- Task Analysis (Failure)：prevention_principle → Prevention；what_should_have_been_done → Correct Approach
- Factual Content：facts → 用户偏好条目

成功任务 → 提取 SOP、最佳实践、可复用模式。
失败任务 → 提取反模式、反事实纠正、预防规则。

## 决策树
1. 已有技能覆盖同一领域？→ 更新它。
2. 已有技能部分重叠？→ 更新它，必要时扩大范围。
3. 完全无覆盖？→ 在类别/领域层面创建新技能。
4. 收到用户偏好？→ 查找用户事实/偏好类技能，更新或创建。

不要创建狭窄、单一用途的技能。创建领域级技能。优先更新而非创建。
```

**新建 SKILL.md 格式**（`create_skill` 时必须遵循）：
```
---
name: "kebab-case-skill-name"
description: "当用户需要 [触发场景] 时使用。支持 [能力1]、[能力2]。"
---

# 技能标题
## 概述
[1–2 句说明技能用途]
## 核心内容
[从 generalizable_pattern、approach、key_decisions 提炼的 SOP 或条目]
```

**更新已有技能时的条目格式**：
- 成功（SOP）：Principle、When to Apply、Steps
- 失败（警告）：Symptom、Root Cause、Correct Approach、Prevention
- 用户偏好：第三人称事实陈述 (date: YYYY-MM-DD)

**输入组装**（`pack_skill_learner_input`）：`distilled_context` + `## 可用技能\n{available_skills_str}`。写入路径改为 `user/{user_id}/skills/` 而非全局 `skills_cache`。

**技能目录 `.meta.json`（与实现对齐）**：

| 来源 | 字段 | 说明 |
|------|------|------|
| **`FileBackend.save_user_skill`** | `created_at`、`updated_at` | 程序写入，UTC ISO-8601；首次创建相同，再次保存时仅更新 `updated_at`。 |
| **`merge_skill_session_meta`**（`skill_learner_tools.py`，Skill Learner 创建/更新技能时） | `source`、`session_id`、`last_session_id` | 会话溯源；**更新**时读取已有 JSON 再合并字段，可保留 FileBackend 已写入的时间戳。**新建**技能时若仅经此函数写入，则初始为上述会话字段（不含时间戳，直至后续经 `save_user_skill` 或其它逻辑补充）。 |

`load_user_skills` / `_scan_skills_dir` 会将 `.meta.json` 中的 **`created_at`、`updated_at`**（若存在）并入返回的 `dict`，便于宿主展示。

#### 9.6.1 技能更新前备份（`str_replace_skill_file`）

在通过 `str_replace_skill_file` 写入前，由 **`backup_skill_file`**（`skill_learner_tools.py`）生成一次**按时间戳隔离**的快照，便于回滚与审计。

| 维度 | 设计 |
|------|------|
| **备份粒度** | **仅本次改动的文件**（`file_path` 指向的那一个），**不**整目录快照，**不**复制同技能内其它未改动文件（如 `refs/` 下其它文件等），**不**影响其它技能目录。 |
| **`.meta.json` 一并备份** | 若技能根目录存在 `.meta.json`，且本次改动的**不是** `.meta.json`，则在同一快照中**额外复制**一份 `.meta.json`（更新成功后 `merge_skill_session_meta` 会改写 meta，故需保留更新前状态）。若本次目标即为 `.meta.json`，只备份该文件一次，**不重复**。 |
| **快照路径** | `{skill_dir}/.backups/{时间戳}/`，时间戳含微秒；冲突时后缀 `_1`、`_2`… 递增。快照内目录结构与相对路径与技能包内一致（如 `SKILL.md`、`extra/foo.md`）。 |
| **快照元数据** | 每个快照目录内写 **`.backup_meta.json`**：`backed_up_at`、`session_id`、`skill_dir`（目录名）、**`backed_up_files`**（字符串列表，列出本快照包含的文件，如 `["SKILL.md", ".meta.json"]`）。 |
| **安全** | `file_path` 禁止绝对路径与含 `..` 的路径；目标路径必须落在当前 `skill_dir` 内（解析后 `relative_to` 校验）。 |
| **列表与展示** | `get_skill` 等列举技能文件时**不展开** `.backups/` 下内容，仅可提示存在历史备份份数，避免上下文膨胀。 |

实现与单测：`skill_learner_tools.backup_skill_file`、`tests/test_skill_backup.py`。

### 9.7 配置与宿主注入

可配置项（参考 AsMe `distillation_config`、`learning`）。**与实现对齐的 `ContextGCOptions` 字段**见下表「代码字段」列；`on_session_end` 会把其中蒸馏相关项打成 `flush_distillation(..., **kwargs)`（键名见 `flush` 函数签名：`min_messages`、`task_agent_max_iterations` 等）。

| 配置项（概念） | 代码字段（`ContextGCOptions`） | 说明 | 默认 |
|----------------|-------------------------------|------|------|
| `min_messages_for_learning` | `flush_min_messages` | 最少原始消息条数才触发内置默认蒸馏 | 4 |
| `task_agent_max_iterations` | `flush_task_agent_max_iterations` | Task Agent 最大迭代轮次 | 20 |
| `skill_learner_max_iterations` | `flush_skill_learner_max_iterations` | Skill Learner 最大迭代轮次 | 10 |
| `experience.task_assign_mode` | `flush_experience_task_assign_mode` | `llm` / `heuristic`，见 9.5 | `llm` |
| `experience.dedup_strategy` | `flush_dedup_strategy` | 经验条目去重：`exact` / `keyword_overlap` / `none` 等 | `keyword_overlap` |
| 返回体是否含 `trace` | `flush_distillation_trace` | False 时 `on_session_end` 从返回 dict 移除 `trace` | False |
| （prompt 覆盖） | — | `task_agent_system_prompt` 等 | 复用 AsMe / 实现内默认 |
| `auto_distill_on_session_end` | — | 未注入 `flush_distillation` 时由 `on_session_end` 自动调用默认管道；注入后由宿主决定 | 默认真 |
| `experience.task_normalize` | — | 任务描述归一化（keyword / hash / none） | 见实现 |
| `experience.conflict_strategy` | — | 冲突经验合并策略 | 见 9.5.1 |

环境变量与 `.env.example` 对照见 [配置说明](../configuration.md)。

宿主可注入 `flush_distillation` 替换默认管道；未注入时使用默认实现（Task Agent → 蒸馏 → Skill Learner + 经验写入）。**自定义管道请 `**kwargs` 转发** `min_messages` 等与 `options`，避免与 `ContextGCOptions` 不一致。

### 9.8 记忆老化与淘汰

偏好、经验、技能随时间只增不减，最终会膨胀到占满 system prompt 上下文。需要**生命周期管理**防止无限膨胀。

**老化策略**：

| 记忆类型 | 老化机制 | 淘汰动作 |
|----------|----------|----------|
| **偏好** | 按 `updated_at` 追踪；超过 `preference_ttl_days` 且未被后续会话引用的条目标记为 stale | stale 偏好从 `preferences.md` 移入 `preferences.archive.md`，不再注入 prompt |
| **经验** | 按最近引用时间衰减；经验被加载并注入 prompt 时刷新 `last_used_at` | 超过 `experience_ttl_days` 未引用的经验目录移入 `experience/.archive/` |
| **技能** | SKILL.md 按条目数统计；超过 `skill_max_entries`（如 30）时触发精简 | 调用 Skill Learner 做**合并精简**（将相似条目合并，低价值条目移除）|

**注入时的容量控制**：

新会话加载偏好/经验/技能注入 prompt 时，若总 token 超过 `memory_inject_max_tokens`（如 2000），按优先级截断：
1. 偏好（最高优先级，影响所有回复）
2. 与当前任务相关的经验（通过关键词匹配当前会话首条消息筛选）
3. 技能（按最近使用时间排序）

**配置**：

```yaml
memory_lifecycle:
  preference_ttl_days: 90         # 偏好过期天数
  experience_ttl_days: 180        # 经验过期天数
  skill_max_entries: 30           # 单技能文件最大条目数
  memory_inject_max_tokens: 2000  # 注入 prompt 时的 token 上限
```

### 9.9 与 AsMe 的差异

| 维度 | AsMe | Context GC |
|------|------------|------------|
| 触发时机 | 用户点击蒸馏，flush_session 传入完整 messages（L2） | 会话结束，传入 L2（原始对话），管道一致 |
| Task Agent | 直接复用 task_agent_curd、task_tools、task_prompt | 同 AsMe |
| 技能写入 | 全局 skills_cache | 用户私有 user/{user_id}/skills/ |
| 经验组织 | 融入 Skill Learner，写 SKILL.md | 独立 experience 目录，L0 任务描述 + L1 .overview.md |
| 偏好 | Task Agent submit_user_preference + report_factual_content | 同 AsMe |

### 9.10 边界与降级

| 场景 | 处理 |
|------|------|
| **L2 超长** | **分段给 Task Agent**（见下方 9.10.1） |
| **蒸馏失败** | 单任务蒸馏失败不影响其他任务；管道可记录 trace 便于排查。重新蒸馏时可先清除旧结果再运行 |
| **无 backend** | 未注入 backend 时，仅做会话内压缩，不持久化 L0/L1/L2，不运行蒸馏管道 |
| **最小消息数** | `min_messages_for_learning` 以下跳过蒸馏，避免琐碎会话浪费调用 |
| **成本超限** | 蒸馏管道累计 token 超过 `distillation_max_tokens` 时，剩余任务跳过蒸馏 |

**9.10.1 L2 超长：分段给 Task Agent**

分段的目的仅为**控制上下文长度**，不依赖「分段与任务一一对应」。每个分段只是一段连续消息，可能含 0 个、1 个、多个任务，或某个任务的中间部分。

**Task Agent 行为**：每次调用看到 `existing_tasks`（上段结果）+ `messages`（本段）。它会：
- 本段多条新请求 → 多次 `insert_task`
- 本段消息属于已有任务 → `append_messages_to_task` 关联、`update_task` 更新状态
- 跨段任务由后续段通过 `existing_tasks` 关联并更新

**实现要点**：
1. 按消息数或 token 切分 L2，逐段调用 `task_agent_curd(messages=seg, existing_tasks=上段结果)`
2. 消息 ID 用**全局索引**（如 seg2 对应 [100,200) 则格式化为 `<100>`、`<101>`…），保证 `raw_message_ids` 在蒸馏时能正确映射到完整 L2
3. 可配合 `previous_progress_num` 限制携带进度条数；单任务跨数百条消息时，蒸馏阶段的任务相关消息仍可能超长，需单独处理

**9.10.2 蒸馏成本估算与预算**

三阶段管道在 session end 集中运行，LLM 调用次数随任务数线性增长：

| 阶段 | 调用次数（N 任务） | 说明 |
|------|-------------------|------|
| Task Agent（多轮工具调用） | 5–15 次 | 与消息量和任务数相关，每次含完整上下文 |
| 蒸馏（逐任务） | N 次 | 每个 success/failed 任务 1 次调用 |
| Skill Learner（多轮） | 3–5 次 | 与蒸馏产出技能条目数相关 |
| **估算合计（10 任务）** | **18–30 次** | GPT-4 级别约 $0.5–2/会话 |

**预算控制**：

- `distillation_max_tokens`：管道总 token 预算（输入+输出），超限后剩余任务 skip，优先蒸馏 success 任务（价值更高）
- 琐碎任务先用规则判断（如消息数 < 3 直接 skip），减少不必要的 LLM 调用
- Trace 记录每次管道的实际 token 消耗，供宿主做成本分析

---

## 十、方案先进性评估

### 10.1 创新点

| 维度 | 设计 | 先进性 |
|------|------|--------|
| **会话内压缩与持久化一体** | 摘要、分代、合并 + L0/L1/L2 持久化统一在 Context GC，会话结束时从 `state.rounds` 直接产出 L1，无需二次处理 | 流水线贯通，避免重复摘要 |
| **分代标注 + 容量触发合并** | 基于关联度对历史轮次打分（±1 平滑累积），低分代参与合并、高分代保留 | 比纯 LRU 或固定窗口更语义感知；业内少见将「分代」与「合并阈值」结合的方案 |
| **无向量检索** | L0/L1 采用 FTS5/关键词/BM25，不依赖 Embedding | 降低部署成本与延迟，适合中小规模、离线场景 |
| **蒸馏管道复用** | Task Agent → 蒸馏 → Skill Learner 复用 AsMe，写入路径改为用户私有 | 站在已验证实现之上，避免重复造轮；经验与技能双轨输出（任务视角 + 技能视角） |
| **L2 超长分段处理** | 分段给 Task Agent，`existing_tasks` 跨段传递，不假定分段与任务对齐 | 明确分段仅为上下文窗口控制，实现路径清晰 |

### 10.2 与业界方案对比

#### 10.2.1 方案与所属机构

| 方案 | 公司/机构 | 开源 |  Stars（约） | 说明 |
|------|----------|------|-------------|------|
| **Claude Code (CC)** | **Anthropic**（美国） | 闭源 | - | AI 安全公司，Claude 系列模型与编码助手 |
| **OpenClaw** | 开源社区（openclaw/openclaw） | MIT | 32 万+ | 个人 AI 助手，多平台接入，支持 OpenViking 插件 |
| **Cursor** | **Anysphere**（美国，2022 成立） | 闭源 | - | MIT 创始，AI 编程 IDE，估值 293 亿美元（2025） |
| **AgentScope** | **阿里巴巴 / ModelScope**（中国） | Apache 2.0 | 1.8 万+ | 多智能体开发平台，零代码工作台、内存压缩 |
| **LangGraph** | **LangChain Inc**（美国，2022 成立） | Apache 2.0 | - | 图式工作流，Checkpoint 持久化，LangSmith 商业平台 |
| **OpenViking** | **火山引擎 Volcengine**（字节跳动，中国） | Apache 2.0 | 1.7 万+ | Agent 上下文数据库，viking:// 文件系统 |
| **Sirchmunk** | **阿里巴巴 / ModelScope**（中国） | Apache 2.0 | 500+ | 无向量 Agentic 检索，Monte Carlo 采样 |
| **MemGPT** | **UC Berkeley**（学术）→ **Letta**（美国创业） | Apache 2.0 | - | 分层虚拟内存，Letta 商业化（种子 1000 万刀） |

#### 10.2.2 深度对比

各方案均有独立对比文档，见 [Comparisons](../comparisons/) 目录。

| 方案 | 独立对比文档 | 记忆/上下文机制 | 触发策略 | 保留策略 | 跨会话持久化 | 与 Context GC 核心差异 |
|------|--------------|----------------|----------|----------|--------------|-------------------------|
| **Claude Code** | [claude-code.md](../comparisons/claude-code.md) | Microcompaction + Auto-Compaction + 文件再水合 + `/compact` 手动 | 剩余 token 低于 headroom + 手动 | 结构化 working state + 重读近期文件 + todos | CLAUDE.md、Auto memory 前 200 行 | CC 与 IDE/工具强绑定；Context GC 嵌入宿主、模型无关；无 Microcompaction，有显式分代 |
| **OpenClaw** | [openclaw.md](../comparisons/openclaw.md) | Session + Daily Notes + MEMORY.md + memory_search（BM25+向量）；memoryFlush 提醒 | 接近 compaction 时 flush | Agent 自主决定写哪些 | 明文 Markdown | OpenClaw 记忆为文件范式；Context GC 提供 L0/L1/L2 + 蒸馏管道 |
| **Cursor** | [cursor.md](../comparisons/cursor.md) | Rules、自动注入打开文件/终端/linter | 无内置压缩 | 规则常驻 | 依赖 MCP 扩展 | Cursor 侧重规则与即时上下文；Context GC 可经 MCP 提供持久记忆 |
| **AgentScope** | [agentscope.md](../comparisons/agentscope.md) | memory、memory compression、数据库；ReAct、Actor | 框架内回调 | Agent 与 memory 模块决定 | 数据库 + 可选 | AgentScope 为完整框架；Context GC 为库，可注入回调 |
| **LangGraph** | [langgraph.md](../comparisons/langgraph.md) | Checkpoint 每节点后，thread_id 组织；short/long-term | 每节点执行后 | 全图状态快照 | Postgres/Redis/Mongo | LangGraph 侧重工作流断点；Context GC 侧重对话摘要与分代 |
| **OpenViking** | [openviking.md](../comparisons/openviking.md) | viking:// L0/L1/L2，向量+目录递归检索 | 会话结束记忆抽取 | 按目录层级与检索结果加载 | 内置持久化 | OpenViking 为独立服务；Context GC 嵌入、可选无向量 |
| **Sirchmunk** | [sirchmunk.md](../comparisons/sirchmunk.md) | 无预索引，Monte Carlo 采样，Knowledge Cluster | 每次搜索 | 相似查询复用 Cluster | DuckDB + Parquet | Sirchmunk 做文档检索；Context GC 做对话记忆，可互补 |
| **MemGPT** | [memgpt.md](../comparisons/memgpt.md) | 分层虚拟内存（main/extended），函数调用管理 | LLM 自主调用 | 层级迁移 | 可配置 | MemGPT 为操作系统式内存管理；Context GC 为压缩+持久化，可组合 |

#### 10.2.3 Context GC 相对劣势（客观对照）

| 对比方 | Context GC 的劣势 |
|--------|-------------------|
| **vs Claude Code** | CC 的 Microcompaction（工具大输出落盘）在 Context GC 中无对应；CC 会话中即时写入 Auto memory，Context GC 偏好**仅**会话结束蒸馏写入（无 `close()` 规则抽取，见 5.2） |
| **vs OpenClaw** | OpenClaw Agent 自主决定写入哪些记忆（memoryFlush），更灵活；Context GC 蒸馏管道是固定流水线，自主度低 |
| **vs MemGPT** | MemGPT 的 LLM 自主调用层级迁移与 Context GC 分代合并理念类似但更动态；Context GC 合并靠阈值触发，适应性较弱 |
| **vs OpenViking** | OpenViking 支持向量检索，语义匹配能力远强于 Context GC 的 FTS5/关键词方案；大规模会话库场景下检索精度差距明显 |
| **vs LangGraph** | LangGraph 每节点 checkpoint，崩溃恢复天然完备；Context GC 的 checkpoint 为后加设计（5.1），成熟度不及 |

#### 10.2.4 Context GC 定位摘要

| 对比维度 | 商业/闭源方案（CC、Cursor） | 框架型（LangGraph、AgentScope） | 服务型（OpenViking） | **Context GC** |
|----------|---------------------------|-------------------------------|---------------------|----------------|
| **形态** | 产品内建，不可替换 | 框架提供接口，可扩展 | 独立服务，客户端接入 | **纯库**，宿主注入 |
| **模型绑定** | 绑定自家或指定模型 | 多模型支持 | 多模型 | **完全模型无关**（回调注入） |
| **压缩策略** | 产品自定义（如 CC 结构化摘要） | 由开发者实现 | 会话结束抽取 | **分代 + 容量触发**（业内少见） |
| **持久化** | 产品内存储 | 框架可选（如 Checkpoint） | 内置 viking 存储 | **Backend 协议**，SQLite/文件/对象存储可插拔 |
| **蒸馏/学习** | 部分有（如 CC 的 memory） | 多无内置 | OpenViking 有记忆演化 | **Task Agent→蒸馏→Skill Learner**，经验+技能双轨 |

### 10.3 优势汇总

1. **嵌入优先**：纯库形态，宿主注入 callback，无强制服务依赖，易于集成
2. **模型无关**：`generate_summary`、`compute_relevance` 等由宿主实现，可切换任意 LLM/Embedding
3. **后端可插拔**：MemoryBackend 协议支持 SQLite、文件、对象存储等多种实现
4. **技能与经验双轨**：私有化技能（SKILL.md）与用户经验（.overview.md）并存，任务维度与技能维度互补

### 10.4 局限与待验证

| 项 | 说明 | 详细设计 |
|------|------|----------|
| **分代效果依赖 compute_relevance** | 若仅用简单关键词匹配，关联度可能不准；LLM 打分成本高；gen_score 需衰减归一化 | [11.1](#111-分代效果依赖-compute_relevance) |
| **蒸馏质量依赖 Task Agent 与 prompt** | 任务边界、成功/失败判定、琐碎判断均由 LLM 完成，需实际数据验证 | [11.2](#112-蒸馏质量依赖-task-agent-与-prompt) |
| **长会话分段** | 跨段任务关联依赖 Agent 推理，极端情况下可能断链 | [11.3](#113-长会话分段) |
| **无多模态** | 当前设计为纯文本，资源（图片、文档）管理未覆盖 | - |
| **经验去重与冲突** | 任务归一化、条目去重、冲突策略；`.task_index.json` 并发安全 | [9.5.1](#951-经验去重与冲突处理) + [11.4](#114-经验去重与冲突) |
| ~~**崩溃丢失**~~ | ~~无 checkpoint，进程崩溃全部丢失~~ | 已设计：[5.1 Checkpoint](#51-checkpoint崩溃恢复) |
| **偏好仅蒸馏写入** | 不在 `close()` 做正则偏好检测，避免低质量条目 | [5.2](#52-用户偏好为何不在-close-中做规则抽取已移除) |
| ~~**记忆无老化**~~ | ~~偏好/经验/技能只增不减~~ | 已设计：[9.8 记忆老化与淘汰](#98-记忆老化与淘汰) |
| ~~**蒸馏成本未管控**~~ | ~~三阶段管道无预算限制~~ | 已设计：[9.10.2 蒸馏成本估算与预算](#9102-蒸馏成本估算与预算) |

### 10.5 综合结论

方案在**会话内压缩策略**（分代 + 容量触发 + gen_score 衰减）、**会话级记忆形态**（L0/L1/L2 与 GC 摘要映射）、**蒸馏管道设计**（复用 AsMe + 经验/技能双轨 + 成本预算）、**生产可靠性**（checkpoint 崩溃恢复）、**记忆生命周期**（老化淘汰 + 注入容量控制）上具有完整设计。**无向量检索**与**嵌入优先**使其在轻量部署场景有差异化。用户偏好**仅经蒸馏管道**写入，与「会话中规则即时抽取」折中见 [5.2](#52-用户偏好为何不在-close-中做规则抽取已移除)。主要不确定性在于分代与蒸馏的实测效果、以及长会话分段下的任务连续性，验证场景见[第十二章](#十二端到端验证场景)。整体处于**设计完备、实现待验**阶段。

---

## 十一、局限项详细设计：提示词与 Harness

> 除多模态外，10.4 中各项均需可落地的细节设计。**提示词**可解决部分问题；提示词无法解决的，采用 **Harness Engineering**（上下文编排、校验环、成本管控、可观测等基础设施）补足。参考 [Harness Engineering](https://harness-engineering.ai/)。

### 11.1 分代效果依赖 compute_relevance

**问题**：简单关键词匹配关联度不准；LLM 打分成本高。

**提示词**（当 `compute_relevance` 用 LLM 时）：

```
你是关联度评分助手。给定「当前轮用户消息」与「历史轮摘要列表」，对每个历史摘要输出 0–1 的关联分数。
关联高：历史摘要与当前用户诉求在主题、实体、任务上有直接延续或强相关。
关联低：历史摘要与当前话题无关、已完结且无后续引用。
输出：JSON 数组 [s1, s2, ...]，长度等于历史摘要数，顺序一一对应。仅输出 JSON，无其他文字。
```

**generate_summary 可关联强化**（[上下文压缩设计](./context-compression.md) 2.5）：摘要须含主题词、实体名、领域术语，便于后续关联度计算。可在 prompt 中显式要求：「输出必须包含可检索关键词（技术栈、文件路径、错误类型等），便于与后续轮次做关联匹配」。

**Harness**：

| 机制 | 说明 |
|------|------|
| **模式配置** | 可配置 `relevance_mode: keyword \| llm`：`keyword` 为默认（关键词重叠，免费）；`llm` 为高精度模式，按需开启 |
| **步进式打分** | 除首轮外，以前为每轮都打分；现改为**每隔 N 轮**（建议 N=3）打一次分，中间轮次沿用上次 gen_score，降低 LLM 调用频率 |
| **gen_score 衰减** | 裸 ±1 累积无上下限，老摘要天然高分、难被合并。引入衰减：每次打分时 `score = score * decay + delta`（建议 `decay=0.9`），并 clamp 到 `[-5, +5]`。确保长期不被引用的摘要分数自然衰减到可合并区间 |
| **成本封顶** | LLM 模式时每轮打分计入 token 预算，超限则跳过本轮打分、沿用上次 gen_score |
| **超时** | LLM 打分调用设 `timeout_ms`（建议 2000ms）；超时则跳过本轮打分、沿用上次 gen_score，不阻塞主流程 |

### 11.2 蒸馏质量依赖 Task Agent 与 prompt

**问题**：任务边界、成功/失败判定、琐碎判断均由 LLM 完成，需实际数据验证。

**提示词**（在 AsMe 基础上补充）：

- **任务边界**：在 TASK_SYSTEM_PROMPT 中强调：「任务 = 用户的一个完整意图/目标，通常跨越多轮对话。判断依据：① 用户明确切换到不同目标（如"好，现在来做注册功能"），开启新任务；② 当前目标已达成（用户确认）或明确放弃（"算了不做了"），本任务结束；③ 同一目标下的追问、调整、反复修改均属于同一任务，不拆分。边界不以单条消息为单位划定，而以意图是否发生根本转变为准。」
- **success/failed 判定**：补充示例——「用户说『好了』『可以了』→ success；用户说『不对』『重来』『算了』→ failed；Agent 报错且用户未纠正 → failed。」
- **琐碎判断**：在 SUCCESS_DISTILLATION_PROMPT 中已有一旦列举学习空间技能且任务相关则不算琐碎；可补充：「若 task_goal 仅含单一事实查询（如某 API 用法、某命令输出），无多步推理或配置变更，可 skip。」

**Harness**：

| 机制 | 说明 |
|------|------|
| **业务逻辑校验（Verification Loop）** | Task Agent 返回后：`task_desc` 非空、`status` 在 `{running, success, failed}` 枚举内、`raw_message_ids` 不为空列表；蒸馏结果 `content` 非空；校验失败则记录 trace 并重试（最多 2 次），仍失败则降级跳过并告警 |
| **超时与重试** | Task Agent 每次 LLM 调用设 `llm_timeout_ms`；若连续 3 轮无有效工具调用，则终止并保留已提取结果；蒸馏调用设 `max_tokens: 512`，超时则记 skip |
| **Trace 与评估** | 记录每次蒸馏的 task_count、skip_count、distilled_count；定期抽样人工评估任务边界与 success/failed 判定准确率 |

### 11.3 长会话分段

**问题**：跨段任务关联依赖 Agent 推理，极端情况下可能断链。

**提示词**（分段场景下对 pack_task_input 的补充）：

在传入「当前已有任务」时，若 `existing_tasks` 非空，追加说明：

```
## 分段说明
当前消息可能是一段长对话的中间部分。若本段消息与「当前已有任务」中某任务属于同一事务的延续，请使用 append_messages_to_task 关联，并用 update_task 更新状态（如完成则设为 success）。
注意：若需要向已标记为 success 或 failed 的任务追加消息（例如用户重新讨论该问题），必须先调用 update_task 将其状态改回 running，再追加消息，最后根据本段结果再次更新状态。
```

**Harness**：

| 机制 | 说明 |
|------|------|
| **分段重叠** | 相邻段间重叠 K 条消息（如前段尾 5 条 = 后段头 5 条），减少边界断链 |
| **跨段一致性校验** | 若某 task 的 raw_message_ids 跨多段，写入前检查：各段均有关联记录，无孤立 task |
| **单任务消息数告警** | 若单任务关联消息数超阈值（如 200），打 trace 告警，便于人工抽查 |

### 11.4 经验去重与冲突

**问题**：设计见 9.5.1；`llm_similar` 去重与 `llm_merge` 冲突合并需具体 prompt。

**提示词**（llm_similar，经验条目去重）：

```
给定「新经验条目」与「已有经验列表」，判断新条目是否与已有某条语义等价（表达同一经验，仅措辞不同）。
等价则输出 JSON {"duplicate": true, "match_index": N}；不等价则 {"duplicate": false}。
仅输出 JSON，无其他文字。
```

**提示词**（llm_merge，冲突合并）：

```
给定两条对同一话题表述相矛盾的经验（经验A 与 经验B），将二者合并为一条综合表述。要求：
1. 保留两条经验各自成立的适用前提或场景
2. 合并后不超过 3 句，简洁直接
3. 若两条实为角度不同而非矛盾，可直接并列表述
仅输出合并后的文本，无其他解释。
```

**Harness**：

| 机制 | 说明 |
|------|------|
| **去重前过滤** | 先用 `exact` 或 `keyword_overlap`，仅在未命中时调用 `llm_similar`，降低 LLM 调用 |
| **超时与 token 限制** | `llm_similar` 设 `max_tokens: 50`（只需输出 JSON）；`llm_merge` 设 `max_tokens: 200`；调用超时则分别 fallback 到 `keyword_overlap` / `keep_both` 策略 |
| **任务索引一致性** | 写入 experience 前后校验 `.task_index.json`，避免 slug / canonical_desc 冲突或孤立条目 |
| **写入前校验** | 新条目非空、格式符合 `.overview.md`；若 conflict_strategy=llm_merge，校验合并结果非空 |

---

## 十二、端到端验证场景

以下场景用于验证整个管道（压缩 → 持久化 → 蒸馏 → 记忆注入）是否真正有效，建议在实现后逐一执行。

### 场景 1：经验跨会话复用

| 步骤 | 动作 | 预期 |
|------|------|------|
| 会话 A | 用户请求实现登录功能，Agent 完成，用户确认成功 | - |
| 蒸馏 A | 管道提取任务"实现登录功能"，蒸馏出成功经验（approach、key_decisions） | experience/{登录}/`.overview.md` 有成功条目 |
| 会话 B | 用户请求实现注册功能（类似但不同），系统加载经验 | Agent prompt 中包含登录经验；回复质量/步骤完整度应优于无经验时 |
| **度量** | 对比有/无经验注入时，Agent 回复的步骤完整性（人工评分 1–5） | 有经验 ≥ 无经验 + 0.5 分 |

### 场景 2：失败反模式防止重蹈覆辙

| 步骤 | 动作 | 预期 |
|------|------|------|
| 会话 A | 用户请求配置 CORS，Agent 遗漏 credentials 导致失败 | - |
| 蒸馏 A | 提取失败反模式：prevention_principle = "配置 CORS 时必须考虑 credentials" | experience 有失败条目 |
| 会话 B | 用户再次请求配置 CORS，系统加载失败反模式 | Agent 在回复中主动提及 credentials 配置 |
| **度量** | 会话 B 中 Agent 是否在首轮即涵盖 credentials | 是/否 |

### 场景 3：偏好经蒸馏进入下一会话

| 步骤 | 动作 | 预期 |
|------|------|------|
| 会话 A | 用户表达稳定偏好（如「以后用中文回复」） | 本轮 `close()` **不**写入 preferences.md |
| 会话 A 结束 | `on_session_end` → `flush_distillation` 抽取事实/偏好 | Task Agent / 蒸馏链路写入 `preferences.md` |
| 会话 B | 宿主 `get_user_preferences` 并注入 | 新会话首轮即可加载偏好 |
| **度量** | 会话 B 首轮回复语言 / 是否遵从偏好 | 依宿主注入与模型而定 |

### 场景 4：崩溃恢复

| 步骤 | 动作 | 预期 |
|------|------|------|
| 轮次 1–30 | 正常对话，checkpoint_interval=5 | 已写入 6 次 checkpoint |
| 轮次 31 | 进程崩溃 | - |
| 恢复 | 以相同 session_id 重新初始化 | 从 .checkpoint.json 恢复 30 轮摘要和 raw_messages，会话可继续 |
| **度量** | 恢复后 state.rounds 数量 | = 30（±checkpoint_interval 容差） |

### 场景 5：经验去重与冲突

| 步骤 | 动作 | 预期 |
|------|------|------|
| 会话 A | 实现登录，蒸馏出"使用 bcrypt 加密密码" | experience 写入成功条目 |
| 会话 B | 再次实现登录，蒸馏出"使用 bcrypt 做密码哈希"（措辞不同、语义相同） | dedup_strategy=keyword_overlap，检测到重复，跳过写入 |
| 会话 C | 实现登录，蒸馏出"使用 argon2 替代 bcrypt"（矛盾经验） | conflict_strategy=append_with_source，两条均保留，标注来源 |
| **度量** | `.overview.md` 条目数 | 会话 B 后 = 1 条；会话 C 后 = 2 条 |

### 场景 6：ASME E2E 评测目录（`tests/test_e2e_asme.py`）

用于深度评测时，**场景 A**（每个 chatme 文件 = 一次会话、不同 `session_id`）采用**单一 FileBackend 根目录**累积用户记忆，与「每测例一个独立 `data_dir`」的单元测试不同：

| 路径（相对当次 `run_dir`） | 含义 |
|----------------------------|------|
| **`shared_data/`** | 场景 A 共用的 **`data_dir`**：`sessions/`、`user/{user_id}/` 等均在此树下，与本文 **3.3** 布局一致。每次完整跑评测前会**清空并重建**该目录。 |
| **`per_session/{session_id}/`** | 仅存放该 chatme 文件的 **`report.txt`**、**`stages.json`** 及 **`README.txt`**（说明数据在 `shared_data/`），**不再**内含独立 `data/`。 |
| **`merged_session/data/`** | **场景 B**（所有 chatme 合并为一次会话）仍使用**独立** `data_dir`，与 `shared_data` 互不混写。 |

单次运行内：`run_single_session(..., clear_data_dir=False)` 用于场景 A 中第 2 个及以后的 chatme，避免清空 `shared_data`；**场景 B** 仍使用默认 **`clear_data_dir=True`**。汇总文件 **`summary_table.txt`** 中会注明 `shared_data` 路径。

---

## 十三、参考资料

- [上下文压缩设计](./context-compression.md)
- [配置与环境变量](../configuration.md)
- [与 Claude Code 对比](../comparisons/claude-code.md)
- [与 OpenViking 对比](../comparisons/openviking.md)
- [OpenViking L0/L1/L2 说明](https://docs.bswen.com/blog/2026-03-16-openviking-context-layers-l0-l1-l2/)
- [AsMe 记忆蒸馏](../../AsMe) — superman/task/distillation.py、skill_learner.py、distill_prompt.py
- 实现索引（与本文对齐）：`src/context_gc/storage/file_backend.py`、`src/context_gc/distillation/flush.py`、`experience_writer.py`、`task_assignment_llm.py`、`skill_learner_tools.py`（`merge_skill_session_meta`）
- [OpenClaw Memory](https://docs.openclaw.ai/concepts/memory) · [LangGraph Persistence](https://langchain-ai.github.io/langgraph/how-tos/persistence/) · [AgentScope](https://agentscope.io/)
- [Harness Engineering](https://harness-engineering.ai/) — 上下文编排、校验环、成本管控、可观测
