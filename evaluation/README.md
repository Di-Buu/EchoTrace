# EchoTrace 本地正式 Evaluation

## 2026-09-24 收口后的正式入口（以本节为准）

本轮只回答树洞是否“记得住、说得准、不过度概括、不会串用户”。默认入口从既有 36 场景中固定选取 24 个产品 Case，另执行 5 组真实 A/B 账号隔离检查，合计 29 组；原 36 Case 与历史报告完整保留，`--suite legacy-all` 才会运行旧全集。本轮不做模型/Embedding 横评，不默认调用 Ragas，也不把 Vercel 版作为 AI 质量基线。

**首次固定核心集已由用户在本地执行**：`results/run-20260924-114228/`，基线 `b30233c`，24 个 AI Case 为 15 pass、1 fail、8 review；21 个有正例的 Case 在真实 Top-K 中命中 40/40 个标注 Moment；5 组真实 A/B 均通过。唯一 fail 的爵士乐案例初步判为 Judge 忽略用户“过去三个月”的自述，原始报告不改写。用户随后确认严格口径，Codex 仅依据保存输出审读 11 条生成洞察并离线汇总：有据 7/11，过度推断 4/11；详情见该目录 `final_report.md`。这是辅助审读而非独立盲审，不能把结构引用 11/11 或 Judge 的 0 次标记当最终效果。**不要为了 review 状态重跑完整测试。**

先在 `evaluation/eval.env` 中填写两个**不同的、已确认邮箱的专用测试账号**：`ECHOTRACE_EVAL_EMAIL`、`ECHOTRACE_EVAL_PASSWORD`、`ECHOTRACE_EVAL_B_EMAIL`、`ECHOTRACE_EVAL_B_PASSWORD`。绝不能使用日常账号：评测会清理这两个账号的 EchoTrace 产品数据，但不会删除 Supabase Auth 账号。两个账号先完成登录及身份不同校验，之后才会清理测试数据。`--plan` 完全不会登录、清理或调用模型。

本地 API 必须以 `local_quality` 配置启动，且修改 Prompt/代码后重启 API。正式运行只需在项目根目录执行：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_eval.py
~~~

先看哪些会复用、哪些需运行：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_eval.py --plan
~~~

单 Case、分类或 `--max-cases` 仅运行所筛选的 AI Case，不附带 5 组隔离检查，适用于工程 dry run。默认正式入口的输出包含原有五类文件，另有 `isolation_results.jsonl`。`metrics.json`/`report.md` 增加四个产品指标：历史记忆召回率仅用真实 Top-K 与标注 Moment ID 计算；洞察有据率和过度推断率在人工审读关键结论前保持“待裁决”，绝不把引用 ID 合规或 LLM Judge 意见当作最终成绩；跨用户串数据率只有五组真实 A/B 全部完成才给比例，任一泄漏直接计入 `bad_cases.jsonl` 并使命令失败。

旧结果按相关行为指纹复用，不因前端/文档修改重跑；只有**本次核心集里应召回历史、但旧可信输出缺真实 Top-K** 的案例会在完整正式运行时定向补采，`--plan` 会逐条写明原因。若仍有应召回 Case 缺追踪，历史记忆召回率保持空值，只展示部分观察值；实际未触发检索则记为召回失败 0，不从分母消失。已有 Verifier/Synthesizer v1.2 固定 Bad Case 回归保留，不再进行第三轮 Prompt 调整。下文的 36 Case 流程为历史说明，不是当前默认正式范围。

运行后打开该次 `results/run-时间/core_review_packet.md`，对每条生成的 Insight 对照原始 Moment 审读；在同目录 `core_review.csv` 的 `grounded`、`over_inference` 中逐行填 `yes` 或 `no`，时间判断可填 `temporal_correct`，争议写入 `review_notes`。自动 pass 的 Insight 也要审读，因为旧 Judge 曾漏判。填完后使用**纯离线**命令汇总两个语义指标，不再调用模型：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\finalize_core.py evaluation\results\run-实际时间
~~~

它生成 `final_metrics.json` 与 `final_report.md`。若有漏填或删掉难判的 Case，会拒绝给最终比例。未完成这一步前，洞察有据率与过度推断率保持空值；跨用户五组未全部成功也不能声称 0%。

## 先查已有证据，再决定要不要运行

现有功能测试、36 Case 旧版完整运行和新版 Prompt 3 Case 定向运行见 [EVIDENCE_LEDGER.md](EVIDENCE_LEDGER.md)。不要为了进入评测阶段重复运行已经有兼容证据的 Case。先执行下面的**无登录、无模型调用、无写入**预检查：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_eval.py --plan
~~~

输出逐条说明复用或待运行原因。旧运行含未定位的未提交改动，复用时标记来源与限制。缺少检索 Top-K 的旧结果仍可用于回答/证据审读，但不能冒充 RAG 检索指标。可与 --category / --case-id 一起筛选。

评测开始前会核对本地 API 实际加载的代码版本。若提示“本地 API 仍在运行旧代码”，请在启动 API 的终端按 Ctrl+C 停止旧进程，然后从 apps/api 目录用下方命令重新启动。此时脚本尚未登录、清理测试数据或调用模型。--plan 不需要启动 API。

执行顺序：先复用功能测试证据并只补缺口，再冻结可评测版本，最后由项目负责人在本地执行正式效果评测。Codex 不代跑全量。

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
3. 填写一个专用且已确认邮箱的测试账号。脚本只清理这个账号的 EchoTrace 产品数据，不删除 Auth 账号。若此前已使用 evaluation/live-e2e.env 配置专用测试账号，可继续使用；千万不要填入日常账号。
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

已有评测结果需要用新版规则重新判定时，可离线执行；此命令不会调用产品接口或模型：

```powershell
python evaluation/rescore_run.py evaluation/results/run-20260923-193003
```

它会生成独立的 `rescore-时间` 报告，保留原始输出和旧版 LLM Judge 判定。
该报告用于区分评测规则误判，不能冒充修复产品后的新效果。

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
- `review_packet.md`：自动排版待复核案例的原始 Moment、AI 回答、规则差异与 Judge 理由；留空人工结论，不改自动分数；
- `report.md`：人类可读报告；
- `baseline.json`：Git、模型、Prompt 与关键配置。
- evidence_metrics.json：原始 Moment 检索/引用分开计分；旧结果无 Top-K 时记为缺失而非 0。

新增检索追踪会在用户自己的 ai_runs 遥测中保存有序 Moment ID，评测脚本按专用测试用户读取。原文和向量不会新增到遥测中。summary.csv 标出历史复用来源。

已有运行也可离线补生成可读复核清单；不会登录、清理测试数据或调用模型：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\review_packet.py evaluation\results\run-时间
~~~

若该历史运行在 runtime_audit.json 中标记为 API 版本未核实，清单顶部会显示警告。

## 可选 Ragas 专项

Ragas 只辅助评 RAG，不是产品总分。先安装独立依赖（不改变 API 的正式依赖）：

这里固定 langchain-community 0.4.1，因为当前 Ragas 0.4.3 与 0.4.2 的 VertexAI 导入不兼容；无需使用 VertexAI。

~~~powershell
.\apps\api\.venv\Scripts\python.exe -m pip install -r evaluation\requirements-ragas.txt
~~~

以后由项目负责人一条命令运行待运行 Case、已有缓存复用、规则/Judge 与 Ragas 专项：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_eval.py --ragas
~~~

Ragas 使用来源 ID Precision/Recall；仅预选的 12 条风险 Case 在有真实检索上下文时调用 Faithfulness 评判模型，并缓存结果。若要在已有报告上单独检查、**不调用产品 Workflow**：

Faithfulness 默认使用当前百炼 Chat Model 作评判，属于同模型辅助评分，不是独立人工裁决；调用失败会记录错误而不缓存为成功，下次可重试。仅 ID 指标不产生模型费用。

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_ragas.py evaluation\results\run-时间
~~~

用 --no-faithfulness 或运行入口的 --ragas-no-faithfulness 只做 ID 指标，不调用评判模型。Ragas 尚未对当前 36 Case 形成正式结果；retrieval_scored 和 faithfulness_scored 的分母必须随报告呈现。

已对四份新版定向报告的保存结果使用 --no-faithfulness 离线核对：5 个不同 Case、6 次真实检索的 ID Precision/Recall 均为 1.0，Faithfulness 0 次。短历史定向样本不能外推为总体 RAG 准确率；详情与去重口径见 EVIDENCE_LEDGER.md。

Prompt 旧/新输出可离线对照，不重新调用产品或模型。但旧 API 未重启时，磁盘上的 Prompt 版本不代表进程真正加载的版本：

~~~powershell
.\apps\api\.venv\Scripts\python.exe evaluation\compare_prompt_runs.py evaluation\results\run-20260923-231331 evaluation\results\run-20260924-000633
~~~

配对文件保留两侧原文、Prompt 版本和模型配置差异；Judge 的 pass 不能替代人工判定“确实改善”。run-20260924-000633 的 API 运行版本未核实，不能用于证明 v1.1 改善。

`pass` 表示规则与可用 Judge 均通过；`fail` 表示存在明确失败；`review` 表示不得伪造自动结论，必须人工复核。

## 当前执行状态

新记忆架构已完成最小真实工程冒烟：

- `memory_stable_interest`：原始 Moment 索引与长期召回通过；
- `personal_fact_supported_running`：Companion 使用原始证据通过；
- `temporal_reading_habit`：完整 Temporal / Pattern / Verifier / Synthesizer 链路结束，规则检查通过，按设计进入人工 review；
- `run_weekly_smoke.py`：保存后后台索引、两条记录周摘要和两个原始 Evidence 来源通过；
- 本地 Web 3 条自动化通过：真实保存与历史、保存中反馈、网络失败恢复。

2026-09-23 已完成一次 36-Case 本地全量运行：20 通过、11 失败、5 待复核。原始报告位于 `results/run-20260923-193003/`。排查后的离线重算报告位于 `results/rescore-20260923-225946/`；它沿用旧产品输出与旧 Judge，不能作为修复后的效果指标。

Companion 当前问题误入历史证据的缺陷已修复；3 条针对性真实回归在 `results/run-20260923-230101/` 中通过。修复后全量运行保存在 `results/run-20260923-231331/`：36 条中 24 自动通过、0 自动失败、12 待人工复核。该运行使用本地质量配置与提交 `ad72928`；12 条 review 不计为通过或失败，仍需核对原始记录和回答。

对 12 条 review 的辅助审读发现，阅读、社交与运动 3 条复杂洞察存在动机、关联强度或事件频率措辞过强的风险。Verifier 和 Synthesizer Prompt 因此升级到 v1.1。后续核查发现本地 API 自 9 月 23 日 23:11 启动后没有重启，而 Prompt 文件在 9 月 24 日 00:05 修改；因此 `results/run-20260924-000633/` 的 3 条，以及 `results/run-20260924-013931/` 中 25 条新运行，均不能归因于 v1.1。它们保留作原始问题证据，但不进入当前版本缓存。后者报告的 21 pass、1 fail、14 review 不是 v1.1 总体成绩；那 1 条失败为运动频率从短期记录推断长期趋势过强。该运行的检索 Top-K 全部缺 ID，不能计算正式 RAG 检索指标。审计名单见 runtime_audit.json。

重启 API 后，已用正确的运行指纹定向补测上述三条：运动在 `results/run-20260924-025602/` 自动通过，阅读和社交在 `results/run-20260924-025921/` 按设计进入 review。随后 `results/run-20260924-031200/` 定向检查了咖啡状态和读研/工作选择：前者语义正确但字面评分不匹配，后者仍出现无充分证据的长期“决策循环”。据此 Verifier/Synthesizer Prompt 升为 v1.2；`results/run-20260924-032654/` 对同一固定 Case 的真实回归不再出现循环与无证据持续时长，但仍因“尚未”与字面“没有”的评分差异标 review。已有输出都保留原版本，不当作 v1.2 总体指标；逐例审读及风险见 EVIDENCE_LEDGER.md。继续遵守“按缺口补测”，不要直接运行所有待运行 Case。
