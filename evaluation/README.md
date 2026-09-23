# EchoTrace 本地正式 Evaluation

本目录包含两套不同用途的工具：

- `run_eval.py` + `scenarios.jsonl`：当前产品效果评测入口；新记忆架构的 9 条旧 Curator Case 已改为原文长期召回 Case。
- `run_evaluation.py` + `cases.json`：早期组件级基线，保留用于追溯，不再作为正式产品结论。
- `run_live_e2e.py`：公网 Demo 黑盒验收，不作为本地质量版效果评测。

## 评测范围

正式测试集当前为 36 个场景，集中评估：

1. 长期记忆；
2. 无依据个人事实；
3. 时间关系；
4. Insight Evidence；
5. Over-inference；
6. Bad Case Regression。

不同类别可以复用同一个场景。用户隔离继续由 RLS 和自动安全测试保护，但不占用正式效果评测场景。

## 首次配置

1. 确认 `apps/api/.env` 已配置 Supabase 和百炼。
2. 复制 `evaluation/eval.example.env` 为 `evaluation/eval.env`。
3. 填写一个专用且已确认邮箱的测试账号。脚本只清理这个账号的 EchoTrace 产品数据，不删除 Auth 账号。
4. 启动本地质量版 API：

```powershell
cd apps/api
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## 一键正式运行

在项目根目录执行：

```powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_eval.py
```

如果已激活 API 虚拟环境，也可以使用：

```powershell
python evaluation/run_eval.py
```

常用筛选：

```powershell
# 单个 Case
python evaluation/run_eval.py --case-id temporal_reading_habit

# 指定类别
python evaluation/run_eval.py --category over_inference

# 工程 dry run，不调用 LLM Judge
python evaluation/run_eval.py --max-cases 3 --no-judge

# 忽略成功缓存，强制重新运行
python evaluation/run_eval.py --force
```

## 重试、断点与缓存

- 每个 Case 独立初始化、执行和清理，单个失败不会丢失之前结果。
- 网络或 Workflow 错误按 `--retries` 重试，默认 1 次。
- 单 Case 默认超时 600 秒，可用 `--case-timeout` 修改。
- 成功或进入人工复核的 Case 会按测试内容、Git、模型、Prompt 和关键配置缓存。
- 已成功且基线指纹未变化的 Case 不会重复调用模型。
- 失败 Case 默认不缓存，修复后会在下一次运行重新执行。
- `raw_results.jsonl` 在每个 Case 后增量写入，可用于意外中断后的排查。

## 输出

每次运行生成 `evaluation/results/run-时间/`：

- `raw_results.jsonl`：原始 Workflow、规则和 Judge 结果；
- `metrics.json`：整体、分类和产品风险指标；
- `summary.csv`：便于筛选与作品集整理的摘要；
- `bad_cases.jsonl`：自动判定失败的 Case；
- `manual_review.jsonl`：必须人工判断或 Judge 不确定的 Case；
- `report.md`：人类可读报告；
- `baseline.json`：Git、模型、Prompt 与关键配置。

`pass` 表示规则与可用 Judge 均通过；`fail` 表示存在明确失败；`review` 表示不得伪造自动结论，必须人工复核。

## 当前执行状态

新记忆架构已完成最小真实工程冒烟：

- `memory_stable_interest`：原始 Moment 索引与长期召回通过；
- `personal_fact_supported_running`：Companion 使用原始证据通过；
- `temporal_reading_habit`：完整 Temporal / Pattern / Verifier / Synthesizer 链路结束，规则检查通过，按设计进入人工 review；
- `run_weekly_smoke.py`：保存后后台索引、两条记录周摘要和两个原始 Evidence 来源通过；
- 本地 Web 3 条自动化通过：真实保存与历史、保存中反馈、网络失败恢复。

以上只证明工程链路能够真实运行，不是正式产品效果指标。LLM Judge 未在本轮冒烟中运行，正式 36-Case 全量评测仍未执行。
