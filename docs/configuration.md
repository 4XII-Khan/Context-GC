# Context GC 配置说明（开箱即用）

复制仓库根目录 `.env.example` 为 `.env`。以下为与 `ContextGCOptions.with_env_defaults()` 相关的环境变量。

## 大模型（压缩 / L0 / 蒸馏）

- `CONTEXT_GC_API_KEY`：API Key（使用默认 LLM 回调时必填）
- `CONTEXT_GC_BASE_URL`：OpenAI 兼容基址，默认 `https://api.openai.com/v1`
- `CONTEXT_GC_MODEL`：模型名

## 网关与思考通道

- `CONTEXT_GC_DISABLE_THINKING`：设为 `1` / `true` 等时尝试关闭 thinking（部分 Qwen 仅填推理通道会导致 content 为空）

## 蒸馏

- `CONTEXT_GC_FLUSH_MIN_MESSAGES`：默认 `4`；原始消息条数低于此值时，内置默认路径跳过 `flush_distillation`
- `CONTEXT_GC_FLUSH_INCLUDE_TRACE`：为真时 `with_env_defaults` 设置 `flush_distillation_trace=True`，`on_session_end` 返回的 distillation 保留 `trace`
- `CONTEXT_GC_FLUSH_TOOL_MAX_TOKENS`：默认 `8192`，用于 `default_call_llm_with_tools` 的 `max_tokens`

## 测试

- `CONTEXT_GC_ASME_E2E_SKIP_MERGED`：ASME E2E 跳过合并全会话场景

## ContextGCOptions 对应字段

`flush_min_messages`、`flush_task_agent_max_iterations`、`flush_skill_learner_max_iterations`、`flush_experience_task_assign_mode`（`llm` / `heuristic`）、`flush_dedup_strategy`、`flush_distillation_trace`（为 False 时从返回结果中去掉 `trace`）。

自定义 `flush_distillation` 时，`on_session_end` 会把上述参数与 `options` 一并传入，可用 `**kwargs` 转发。

## 预设与工厂

- `preset_small_chat()`：较小窗口、较密 checkpoint，`flush_min_messages=2`
- `preset_agent_long_context()`：`max_input_tokens=32000`、宽合并梯度常量 `LONG_CONTEXT_MERGE_GRADIENT_BY_TOKENS`
- `ContextGC.create_with_file_backend(data_dir, ...)`：创建目录与 `FileBackend`；未传 `options` 时等价 `with_env_defaults(data_dir=...)`

## 记忆注入

`await gc.build_memory_injection_text(user_id, current_query="", config=LifecycleConfig())`：拉取偏好/经验/技能并生成可注入 system 的文本。
