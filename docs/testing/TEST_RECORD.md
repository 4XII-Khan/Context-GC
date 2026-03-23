# 测试记录

本文件用于**手工或脚本跑测后**追加记录：基于什么数据、跑了什么范围、得到什么结论、何时执行。  
与 CI 自动报告互补；重要回归、发版前全量、ASME E2E 等建议登记一行。

---

## 填写说明

1. **在下方「汇总表」中追加新行**（**最新在最上方**，与当前表一致）。
2. **数据/输入**：例如 `tests/data/chatme_*.json`、`pytest 单元（无 LLM）`、某次 `examples/data` 演示等。
3. **测试范围**：实际执行的命令（可复制）或场景名（如 ASME 场景 A/B）。
4. **结果**：通过/失败、条数统计、关键指标，或指向 `tests/output/...` 下报告路径。
5. **环境**：Python 版本、模型名、分支/commit（可选，发版相关时建议写）。

---

## 汇总表

**约定：最新记录放在表格最上方。**

| 测试时间 | 数据 / 输入 | 测试范围 | 结果 | 环境 / 备注 |
|----------|-------------|----------|------|-------------|
| 2026-03-23 | `tests/data/dialogues.md`（100 轮）；`tests/data/chatme_*.json`（ASME）；其余为内存/临时目录用例 | 全量：`pytest`/ `uv run pytest -q`（`testpaths: tests`，**42 条**） | **42 passed**，**1 warning**，总耗时 **749.98s（约 12min30s）** | darwin，Python 3.14.0，pytest 9.0.2，pluggy 1.6.0；`git` @ `46dd61e`；**warning**：`tests/test_e2e_cases.py:170` `PytestCollectionWarning`：`TestReport` 含 `__init__` 未被当作测试类收集（可后续改名或移出 `Test*` 前缀） |
| 2026-03-23 | 无外部数据集；仓库内测试代码 | `uv run pytest tests/ -q --ignore=tests/test_100_rounds.py --ignore=tests/test_e2e_cases.py --ignore=tests/test_e2e_asme.py` | **40 passed**，耗时约 1.3s | macOS，Python 3.14.0，pytest 9.0.2；`git` @ `46dd61e` |
| 2026-03-23 | 无外部数据集；同上 | `uv run pytest tests/test_storage.py tests/test_repeat_session_dedup.py tests/test_experience_task_assignment.py -q` | **24 passed**，耗时约 1.2s | 同上；覆盖 FileBackend、偏好/经验去重、LLM 任务归并 mock |
| 2026-03-23 | 开发迭代中同一基线 | 曾执行与上行类似的存储+去重+`test_experience_task_assignment` 组合（用例数随 `test_storage` 扩充略增） | **22 passed**（当时快照） | 对话内回归记录；现以 24 passed 为准 |
| 2026-03-23 | `tests/data/chatme_*.json`（ASME chatme 格式） | ASME E2E：`tests/test_e2e_asme.py`（需 `CONTEXT_GC_API_KEY`） | 本地**历史产出**见 `tests/output/2026-03-23/asme_e2e/summary_table.txt` 及 `shared_data/`；**本表登记时未在本机重跑** | 模型以当时 `.env` 为准；场景 A 共用 `shared_data/` |

> 模板行（占位，勿删）：  
> `YYYY-MM-DD` | *数据说明* | *命令* | *passed/failed 与路径* | *Python / 模型 / commit*

---

## ASME E2E 专项（可选子表）

跑 `tests/test_e2e_asme.py` 时，除汇总表外可在此记录**模型与输出目录**，便于对比多次运行。

| 测试时间 | Chatme 数据目录 | 模型 `CONTEXT_GC_MODEL` | 输出目录 `tests/output/<日期>/asme_e2e/` | 场景 A `shared_data` 是否共用 | 备注 |
|----------|-----------------|-------------------------|------------------------------------------|-------------------------------|------|
| 2026-03-23 | `tests/data/` | （见当时运行环境 `.env`） | `tests/output/2026-03-23/asme_e2e/` | 是 | 历史一次跑测产物；重跑后请更新本行时间与路径 |

---

## 快速命令参考

| 目的 | 命令 |
|------|------|
| 单元 + 蒸馏等（不含长耗时 LLM E2E） | `uv run pytest tests/ -q --ignore=tests/test_100_rounds.py --ignore=tests/test_e2e_cases.py --ignore=tests/test_e2e_asme.py` |
| 全量（需 API Key，耗时长） | `uv run pytest -q` |
| ASME Chatme 深度评测 | `python3 tests/test_e2e_asme.py` 或 `uv run pytest tests/test_e2e_asme.py -v -s` |
| 七案例 E2E | `python3 tests/test_e2e_cases.py` |
| 带持久化示例 | `python3 examples/context_gc_with_storage.py` |

---

## 变更说明

| 日期 | 说明 |
|------|------|
| 2026-03-23 | 初版；同日追加：快路径 40 passed、存储子集 24 passed、ASME 历史目录、**全量 42 passed（~12.5min）+ 1 collection warning** |
