"""
tests/test_100_rounds.py

100 轮、约 100 万 token 的端到端集成测试。
记录每一次压缩（单轮摘要）和合并（二次摘要）的原始内容与结果。

运行方式：
    python3 -m pytest tests/test_100_rounds.py -v -s
    或
    python3 tests/test_100_rounds.py

输出：
    - 控制台：进度与摘要
    - 文件：test_100_rounds_log.txt（完整压缩/合并记录）
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from context_gc import ContextGC, ContextGCOptions, RoundMeta

# 加载 .env（项目根目录或 tests/ 目录）
_env_paths = [
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent / ".env",
]
for p in _env_paths:
    if p.exists():
        load_dotenv(p)
        break

# =============================================================================
# 配置（从环境变量读取，见 .env.example）
# =============================================================================

LLM_API_KEY  = os.environ.get("CONTEXT_GC_API_KEY", "")
LLM_BASE_URL = os.environ.get("CONTEXT_GC_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL    = os.environ.get("CONTEXT_GC_MODEL", "Qwen3.5-35B-A3B")

# 每轮 token 在 500～10000 之间随机，围绕 1～3 个主题连续对话
CHARS_PER_TOKEN = 3
MIN_TOKENS_PER_ROUND = 500
MAX_TOKENS_PER_ROUND = 10_000

LOG_FILE = os.path.join(os.path.dirname(__file__), "output", "test_100_rounds_log.txt")
FINAL_CONTEXT_FILE = os.path.join(os.path.dirname(__file__), "output", "test_100_rounds_final_context.txt")
DIALOGUES_FILE = os.path.join(os.path.dirname(__file__), "data", "dialogues.md")

_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


# =============================================================================
# 对话数据加载与生成
# =============================================================================


def load_dialogues_from_file(path: str) -> list[tuple[str, str]]:
    """
    从 dialogues.md 加载对话数据。
    格式：交替的 user：<content> 和 assistant：<content> 行。
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.rstrip() for line in f if line.strip()]

    rounds: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        user_content = ""
        if lines[i].startswith("user："):
            user_content = lines[i][len("user："):].strip()
            i += 1
        elif lines[i].startswith("user:"):
            user_content = lines[i][len("user:"):].strip()
            i += 1

        assistant_content = ""
        if i < len(lines) and (lines[i].startswith("assistant：") or lines[i].startswith("assistant:")):
            prefix = "assistant：" if lines[i].startswith("assistant：") else "assistant:"
            assistant_content = lines[i][len(prefix):].strip()
            i += 1

        if user_content or assistant_content:
            rounds.append((user_content, assistant_content))

    return rounds


# =============================================================================
# 100 轮对话内容生成（围绕 1～3 主题的连续真实对话，每轮 500～10000 token）
# 当 dialogues.md 不存在时使用
# =============================================================================

# 主题一：在线教育平台整体架构
THEME1_TOPICS = [
    "产品定位与目标用户",
    "核心功能模块划分",
    "用户端与管理端分离",
    "微服务拆分边界",
    "数据一致性要求",
    "高并发场景分析",
]

# 主题二：技术选型与实现
THEME2_TOPICS = [
    "用户认证与权限系统",
    "课程与章节数据模型",
    "视频存储与 CDN 选型",
    "支付对接与订单流程",
    "搜索与推荐架构",
    "消息通知与实时通信",
]

# 主题三：优化与运维
THEME3_TOPICS = [
    "缓存策略与热点处理",
    "数据库分库分表",
    "监控告警与日志",
    "安全与防刷",
    "灰度发布与回滚",
    "成本优化与弹性伸缩",
]

# 连续性引用语（引用前文）
REFERENCE_PHRASES = [
    "结合你之前提到的",
    "关于上一轮我们讨论的",
    "在你说的架构基础上",
    "针对前面提到的",
    "延续我们关于",
    "基于之前确定的",
    "考虑到你前面说的",
]

# 填充句（用于达到目标 token，保持语义相关）
FILLER_SENTENCES = [
    "实际落地时需要结合业务量级和团队规模做取舍。",
    "这部分可以后续迭代时再细化，先保证主流程跑通。",
    "我们团队在类似项目中有过实践，效果不错。",
    "建议先做 MVP，验证核心假设后再扩展。",
    "这里涉及一些技术细节，可以单独开一轮深入讨论。",
    "从运维角度看，还需要考虑监控和告警。",
    "性能测试阶段要重点关注这个环节。",
]


def _pad_to_length(text: str, target_chars: int, fillers: list[str]) -> str:
    """将文本填充到目标字符数，用语义相关的填充句。"""
    if len(text) >= target_chars:
        return text[:target_chars]
    idx = 0
    while len(text) < target_chars:
        text += " " + fillers[idx % len(fillers)]
        idx += 1
    return text[:target_chars]


def generate_100_rounds(seed: int = 42) -> list[tuple[str, str]]:
    """
    生成 100 轮围绕 1～3 主题的连续真实对话。
    - 每轮 token 在 500～10000 之间随机
    - 主题：在线教育平台架构（需求→技术选型→优化运维）
    - 含连续性引用，便于压缩后结合原始会话做判断
    """
    rng = random.Random(seed)
    rounds = []
    theme_progress = {"t1": 0, "t2": 0, "t3": 0}  # 各主题进度

    for i in range(100):
        # 每轮 token 随机 500～10000
        target_tokens = rng.randint(MIN_TOKENS_PER_ROUND, MAX_TOKENS_PER_ROUND)
        target_chars = target_tokens * CHARS_PER_TOKEN
        user_chars = rng.randint(target_chars // 4, target_chars // 2)
        assistant_chars = target_chars - user_chars

        # 阶段划分：前 35 轮主题一，中 35 轮主题二，后 30 轮主题三
        if i < 35:
            theme, topics = 1, THEME1_TOPICS
            theme_progress["t1"] += 1
            phase = "需求与架构"
        elif i < 70:
            theme, topics = 2, THEME2_TOPICS
            theme_progress["t2"] += 1
            phase = "技术实现"
        else:
            theme, topics = 3, THEME3_TOPICS
            theme_progress["t3"] += 1
            phase = "优化与运维"

        topic = topics[i % len(topics)]

        # 用户消息：带连续性引用
        if i > 0:
            ref = rng.choice(REFERENCE_PHRASES)
            user_base = f"{ref}「{topic}」，我想进一步了解："
        else:
            user_base = f"我们打算做一个在线教育平台，先从「{topic}」开始讨论吧。"
        user_detail = (
            f"具体来说，在{phase}阶段，{topic}这块我们团队有些分歧，"
            f"比如实现方式、技术选型、和现有系统的集成等。请给出你的建议和最佳实践。"
        )
        user = user_base + user_detail
        user = _pad_to_length(user, user_chars, FILLER_SENTENCES)

        # 助手消息：带结论和与前文的关联
        prev_ref = f"（承接我们关于{topics[(i - 1) % len(topics)]}的讨论）" if i > 0 else ""
        assistant_base = (
            f"{prev_ref}关于「{topic}」，我的建议是："
            f"在{phase}场景下，需要综合考虑可扩展性、开发效率和运维成本。"
        )
        assistant_detail = (
            f"具体实现上，建议采用分层设计，核心逻辑与基础设施解耦。"
            f"我们之前讨论的架构原则在这里同样适用。"
            f"关键决策点包括：数据模型设计、接口契约、以及与其他模块的协作方式。"
        )
        assistant = assistant_base + assistant_detail
        assistant = _pad_to_length(assistant, assistant_chars, FILLER_SENTENCES)

        rounds.append((user, assistant))

    return rounds


def estimate_tokens(text: object) -> int:
    """估算 token 数：空内容返回 0，非空至少 1。"""
    if isinstance(text, str):
        return 0 if not text else max(1, len(text) // CHARS_PER_TOKEN)
    if isinstance(text, list):
        total = sum(len(str(m.get("content", ""))) for m in text)
        return 0 if total == 0 else max(1, total // CHARS_PER_TOKEN)
    s = str(text)
    return 0 if not s else max(1, len(s) // CHARS_PER_TOKEN)


# =============================================================================
# 带日志的回调（记录每次压缩与合并）
# =============================================================================

def _split_history_and_current(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """将 messages 拆分为历史摘要与当前轮。"""
    history, current = [], []
    for m in messages:
        content = str(m.get("content", ""))
        if "[历史摘要" in content or "[Round " in content:
            history.append(m)
        else:
            current.append(m)
    return history, current


class LogRecorder:
    def __init__(self, log_path: str):
        self.log_path = log_path
        self.log_lines: list[str] = []
        self.summary_count = 0
        self.merge_count = 0
        self.last_raw_scores: list[tuple[int, float]] = []
        self.last_summary_time = 0.0
        self.last_scoring_time = 0.0
        self.last_history_tokens = 0
        self.last_current_tokens = 0
        self.last_summary_tokens = 0

    def _append(self, section: str, content: str):
        line = f"\n{'='*80}\n[{datetime.now().isoformat()}] {section}\n{'-'*40}\n{content}\n"
        self.log_lines.append(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)

    def log_summary(
        self,
        seq: int,
        original_messages: list[dict],
        result: str,
        history_tokens: int,
        current_tokens: int,
        summary_tokens: int,
        elapsed_sec: float,
    ):
        self.summary_count += 1
        self.last_summary_time = elapsed_sec
        self.last_history_tokens = history_tokens
        self.last_current_tokens = current_tokens
        self.last_summary_tokens = summary_tokens

        orig_full = "\n".join(
            f"[{m['role']}] {m.get('content', '')}"
            for m in original_messages
        )
        content = (
            f"【单轮摘要 #{self.summary_count}】序号 {seq}\n"
            f"  历史摘要 token: {history_tokens} (0=无历史)\n"
            f"  当前轮次 token: {current_tokens}\n"
            f"  摘要后 token:   {summary_tokens} (0=摘要为空)\n"
            f"  摘要耗时:       {elapsed_sec:.2f}s\n\n"
            f"========== 摘要前原文 ==========\n{orig_full}\n\n"
            f"========== 摘要后结果 ==========\n{result if result else '(空)'}"
        )
        self._append("COMPRESS (单轮摘要)", content)

    def log_merge(self, group: list[RoundMeta], result: str, round_ids: list[int]):
        self.merge_count += 1
        orig_summaries = "\n---\n".join(
            f"[Round {r.round_id}] gen_score={r.gen_score} tokens={r.token_count}\n{r.summary}"
            for r in group
        )
        content = (
            f"【合并摘要 #{self.merge_count}】合并 Round {round_ids}\n"
            f"合并前轮数: {len(group)}\n"
            f"合并后 round_id: {max(round_ids)}\n\n"
            f"========== 合并前原文 ==========\n{orig_summaries}\n\n"
            f"========== 合并后结果 ==========\n{result if result else '(空)'}"
        )
        self._append("MERGE (二次摘要)", content)

    def log_scoring(self, round_scores: list[tuple[int, float]], elapsed_sec: float):
        """记录打分：round_id -> 原始分值(0-10)。"""
        self.last_raw_scores = round_scores
        self.last_scoring_time = elapsed_sec


def make_logged_callbacks(recorder: LogRecorder, gc_ref: dict):
    """创建带日志记录的 generate_summary、merge_summary、compute_relevance。"""

    async def generate_summary(messages: list[dict], *, max_output_chars: int | None = None) -> str:
        history_msgs, current_msgs = _split_history_and_current(messages)
        history_tokens = estimate_tokens(history_msgs)
        current_tokens = estimate_tokens(current_msgs)

        dialog_text = "\n".join(
            f"[{m['role'].upper()}] {m.get('content', '')}"
            for m in messages
        )
        max_chars = 80_000
        if len(dialog_text) > max_chars:
            dialog_text = "...(前文省略)...\n" + dialog_text[-max_chars:]

        length_constraint = f"输出不超过 {max_output_chars} 字。" if max_output_chars else "输出 50–150 字。"
        prompt = (
            "你是一个对话摘要助手。将以下对话压缩为一条摘要，要求：\n"
            "1. 保留用户意图、关键决策、结论\n"
            "2. 去除寒暄和重复表述\n"
            f"3. {length_constraint} 格式：主题：xxx。用户：xxx。助手：xxx。结论：xxx。\n"
            "4. 语言与输入一致，只输出摘要，不要其他内容\n\n"
            f"对话内容：\n{dialog_text}"
        )

        t0 = time.perf_counter()
        resp = await _client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.3,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        result = (resp.choices[0].message.content or "").strip()
        elapsed = time.perf_counter() - t0

        summary_tokens = estimate_tokens(result)
        recorder.log_summary(
            recorder.summary_count + 1,
            messages,
            result,
            history_tokens,
            current_tokens,
            summary_tokens,
            elapsed,
        )
        return result

    async def merge_summary(group: list[RoundMeta], *, max_output_chars: int | None = None) -> str:
        summaries_text = "\n---\n".join(
            f"[Round {r.round_id}] {r.summary}" for r in group
        )
        length_constraint = f"输出不超过 {max_output_chars} 字。" if max_output_chars else "输出不超过 200 字。"
        prompt = (
            "将以下多段对话摘要合并为一条，要求：\n"
            f"1. {length_constraint}\n"
            "2. 去除重复内容，保留关键信息\n"
            "3. 保持时间顺序，突出重要结论\n"
            "4. 只输出合并后的摘要，不要其他内容\n\n"
            f"待合并的摘要：\n{summaries_text}"
        )
        resp = await _client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.2,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        result = (resp.choices[0].message.content or "").strip()
        recorder.log_merge(group, result, [r.round_id for r in group])
        return result

    async def compute_relevance(user_text: str, summaries: list[str]) -> list[float]:
        if not summaries:
            return []
        gc = gc_ref.get("gc")
        if not gc:
            return [5.0] * len(summaries)
        prev_rounds = gc.state.rounds[:-1]
        round_ids = [r.round_id for r in prev_rounds]

        numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(summaries))
        prompt = (
            f"当前用户问题：\"{user_text[:500]}\"\n\n"
            f"以下是历史对话摘要，请评估每条摘要与当前问题的相关程度，"
            f"打分范围 0-10（10 最相关）。\n"
            f"只输出每条的分数，用逗号分隔，不要其他内容。\n"
            f"例如：3,8,5\n\n"
            f"摘要列表：\n{numbered[:8000]}"
        )

        t0 = time.perf_counter()
        resp = await _client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = (resp.choices[0].message.content or "").strip()
        elapsed = time.perf_counter() - t0

        try:
            scores = [float(x.strip()) for x in raw.split(",") if x.strip()]
            if len(scores) != len(summaries):
                avg = sum(scores) / len(scores) if scores else 5.0
                scores = (scores + [avg] * len(summaries))[:len(summaries)]
        except Exception:
            scores = [5.0] * len(summaries)

        round_scores = list(zip(round_ids, scores))
        recorder.log_scoring(round_scores, elapsed)
        return scores

    return generate_summary, merge_summary, compute_relevance


# =============================================================================
# 主测试
# =============================================================================

def run_test():
    """执行 100 轮测试，记录所有压缩与合并。"""
    if not LLM_API_KEY:
        raise ValueError(
            "未配置 CONTEXT_GC_API_KEY。请复制 .env.example 为 .env 并填入 API Key。"
        )
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)

    recorder = LogRecorder(LOG_FILE)
    gc_ref = {}
    gen, merge, rel = make_logged_callbacks(recorder, gc_ref)

    opts = ContextGCOptions(
        max_input_tokens=2000,  # 上下文最大 5000 token
        generate_summary=gen,
        merge_summary=merge,
        compute_relevance=rel,
        estimate_tokens=estimate_tokens,
        capacity_threshold=0.1,  # 超过 20% 后每 10% 触发一次合并
        reserve_for_output=4096,
    )

    gc = ContextGC(opts)
    gc_ref["gc"] = gc

    if os.path.exists(DIALOGUES_FILE):
        rounds = load_dialogues_from_file(DIALOGUES_FILE)
    else:
        rounds = generate_100_rounds()

    total_original_tokens = 0
    for i, (user, assistant) in enumerate(rounds):
        total_original_tokens += estimate_tokens([{"content": user}, {"content": assistant}])

    n_rounds = len(rounds)
    print(f"\n{'='*60}")
    print(f"{n_rounds} 轮对话测试（数据来源: {'dialogues.md' if os.path.exists(DIALOGUES_FILE) else '程序生成'}）")
    print(f"原始总 token: {total_original_tokens:,}")
    print(f"日志文件: {LOG_FILE}")
    print(f"{'='*60}\n")

    start = time.time()
    for i, (user, assistant) in enumerate(rounds):
        t0 = time.time()
        gc.push([
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ])
        asyncio.run(gc.close())
        elapsed = time.time() - t0

        # 输出：历史摘要token、当前轮token、摘要后token、摘要时间、打分时间、每轮分值、原始分值、摘要结果
        raw_by_id = {rid: rs for rid, rs in recorder.last_raw_scores}
        lines = [
            f"  Round {i+1:3d}/{n_rounds} | rounds={len(gc.state.rounds):3d} total_tokens={gc.state.total_tokens:>8,} 耗时={elapsed:.1f}s",
            f"    历史摘要token: {recorder.last_history_tokens} | 当前轮token: {recorder.last_current_tokens} | 摘要后token: {recorder.last_summary_tokens}",
            f"    摘要耗时: {recorder.last_summary_time:.2f}s | 打分耗时: {recorder.last_scoring_time:.2f}s",
        ]
        if gc.state.rounds:
            last_round = gc.state.rounds[-1]
            summary_preview = last_round.summary[:300] + "..." if len(last_round.summary) > 300 else last_round.summary
            lines.append(f"    摘要结果: {summary_preview}")
            score_parts = []
            for r in sorted(gc.state.rounds, key=lambda x: x.round_id):
                raw = raw_by_id.get(r.round_id, "-")
                raw_str = f"{raw:.1f}" if isinstance(raw, (int, float)) else str(raw)
                score_parts.append(f"R{r.round_id}:gen={r.gen_score:+d},raw={raw_str}")
            lines.append(f"    分值: {' | '.join(score_parts)}")
        print("\n".join(lines))

    total_time = time.time() - start

    # 最终 get_messages
    current = [{"role": "user", "content": "请总结我们讨论过的所有技术主题。"}]
    msgs = asyncio.run(gc.get_messages(current))
    final_tokens = estimate_tokens(msgs)

    # 汇总
    summary = f"""
{'='*80}
{n_rounds} 轮测试完成 - 汇总
{'='*80}
原始总 token:     {total_original_tokens:,}
最终 rounds 数:   {len(gc.state.rounds)}
最终 total_tokens: {gc.state.total_tokens:,}
get_messages token: {final_tokens:,}
单轮摘要次数:     {recorder.summary_count}
合并摘要次数:     {recorder.merge_count}
总耗时:           {total_time:.1f}s
日志文件:         {LOG_FILE}
最终上下文摘要:   {FINAL_CONTEXT_FILE}
"""
    print(summary)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(summary)

    # 写入最终上下文完整摘要到文件
    with open(FINAL_CONTEXT_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"{n_rounds} 轮测试 - 最终上下文完整摘要（压缩后）\n")
        f.write("=" * 80 + "\n\n")
        for r in sorted(gc.state.rounds, key=lambda x: x.round_id):
            merged_info = f" [合并自 Round {r.merged_round_ids}]" if r.is_merged else ""
            f.write(f"[Round {r.round_id}] gen_score={r.gen_score} tokens={r.token_count}{merged_info}\n")
            f.write(f"{r.summary}\n\n")
        f.write("-" * 80 + "\n")
        f.write(f"共 {len(gc.state.rounds)} 条摘要，总 token: {gc.state.total_tokens:,}\n")

    print(f"最终上下文摘要已写入: {FINAL_CONTEXT_FILE}")

    return gc, recorder


# =============================================================================
# Pytest 入口
# =============================================================================

def test_100_rounds_integration():
    """多轮对话端到端测试（数据来自 dialogues.md 或程序生成）。"""
    gc, recorder = run_test()
    assert len(gc.state.rounds) > 0
    assert recorder.summary_count >= 99  # dialogues.md 约 101 轮，程序生成 100 轮
    print("\n✓ 集成测试通过")


if __name__ == "__main__":
    run_test()
