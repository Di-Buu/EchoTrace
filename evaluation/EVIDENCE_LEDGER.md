# EchoTrace 现有证据台账（2026-09-24）

## 本轮收口状态（后续记录优先于下方历史口径）

- 当前默认正式范围：`core_suite.json` 从 36 例历史池固定取 24 例，另有 5 组真实 A/B 隔离，共 29 组；不新增模型或 Embedding 横评。旧 36 例仍可用 `--suite legacy-all` 追溯，但不作为当前默认集合。
- 本地质量版配置核对：Chat `qwen3.7-plus-2026-05-26`、Embedding `qwen3.7-text-embedding`（1024 维）、`local_quality`、Insight reasoning 开启、单次 Insight AI timeout 120 秒。2026-09-24 本地 `/health` 可访问，进程源码指纹为 `048d56b771979c41257d`。早前真实冒烟已覆盖 Moment、Companion、周摘要与完整 Insight；本轮未重复调用模型。
- 本轮代码、Prompt、测试集与工具已提交为**本地冻结 commit**（以 `git rev-parse HEAD` 和各次 `baseline.json` 中的完整 SHA 为准；未推送、未触发线上部署）。本轮固定的 API 源码指纹 `048d56b771979c41257d`、Evaluation/测试集指纹 `306ef4c396d9a24b794f`；每次运行仍记录 dirty 状态、模型、Prompt 与这两个指纹。冻结代码不等于已获得正式效果成绩。
- 原有完整运行 `run-20260923-231331`（旧 Prompt）及 v1.1/v1.2 的定向结果保留。`run-20260924-013931` 的新执行来自未重启的旧 API，已标版本未核实；不可混入当前指标。现有 5 个短历史不同 Case、6 次检索的 ID 命中检查仅证明定向样本，非总体召回率。
- 工具曾通过离线测试与两种 A/B Workflow 的 mock dry run。之后项目负责人在本地以干净提交 `b30233c` 运行了固定核心集，真实结果见 `results/run-20260924-114228/`：24 个 AI Case 中 15 自动通过、1 自动失败、8 review；5 组真实 A/B 检查均通过。正例检索 21 个 Case、40/40 个标注 Moment ID 进入 Top-K；两条缺旧追踪的 Case 不要求历史召回，不影响该分母。**这是短历史合成测试集的结果，不外推为真实用户总体水平。**11 条已生成洞察的语义有据率与过度推断率仍待人工确认，不得把结构引用 11/11 或 Judge 的 0 次标记当最终指标。
- 唯一自动 fail `memory_stable_interest` 的 Judge 声称“一条记录不能支持三个月”，但 Moment 原文就是用户对过去三个月听爵士乐的自述；Top-K 和引用均命中 1/1。离线审读认为主要是 Judge 误判，精确起止日期最好用“用户自述约三个月”表述，不重跑模型以消除评分器噪声。
- `results/run-20260924-114228/codex_review_proposal.md` 保存了 11 条洞察的**AI 辅助、非最终**审读。社交反例中补写“精力充沛/身体舒适”、单次取消中写“偶发”，以及阅读/跑步的部分量词放大，需在最终人工表中如实计入，不因 Judge pass 而隐藏。按本轮停止条件不继续第三/第四轮 Prompt 调参；若后续真实用户数据表明大量同类无依据结论，再单独启动修复。
- 用户随后确认“按严格口径”；Codex 只读取保存的 Moment 与输出完成 11 条离线辅助审读，填写 `results/run-20260924-114228/core_review.csv` 并运行纯离线 `finalize_core.py`，没有再调用产品或模型。按“关键个人陈述均须有据，任一明显无据补充即不通过”的口径：洞察有据 **7/11=63.6%**，过度推断 **4/11=36.4%**；时间顺序内部检查 11/11。完整分母与四条问题见同目录 `final_report.md`，机器值见 `final_metrics.json`。这属于**用户确认口径的 Codex 辅助审读**，不是独立盲审，也不是线上真实用户总体效果。历史召回 40/40、隔离 0/5 仍按实际样本范围报告。原始自动 15 pass / 1 fail / 8 review 不被覆写，爵士乐 Judge 误判只另作归因说明。
- 已知问题：内部 `unresolved_loop` 类型可能与谨慎正文不一致（P2，用户可见正文已收敛）；部分旧 Case 的字面关键词规则与语义等价答案不一致（进入人工 review，不作产品失败）；旧 Judge 有漏判（所有生成 Insight 都输出人工审读表）；Ragas 依赖存在弃用提示但不是本轮阻塞项；线上 Demo 未更新且不作为本地 AI 质量基线。

本台账区分功能可运行证据与 AI 效果证据。历史结果保留原版本和来源，不自动冒充当前完整版本。review 是待人工判断，不是失败，也不是通过。

| 检查项 | 已有证据 | 当前判断 | 下一步 |
|---|---|---|---|
| Moment 保存、后台原文索引 | evaluation/README.md 记录 run_weekly_smoke.py 真实通过；脚本覆盖保存与索引 | 历史功能冒烟已做；未保存独立机器报告 | 不重复跑；相关行为变更后才补测 |
| Thread 与 Companion | results/run-20260923-230101/ 三条真实回归；results/run-20260923-231331/ 中的 Companion Case | 当前 Companion Prompt 仍为 v1.1，相关服务代码未变；历史运行的 dirty 路径未知 | 复用并标历史来源；不为证明相同链路再调用模型 |
| 原始 Moment 长期召回 | 全量运行中的原文召回 Case；evaluation/README.md 最小真实冒烟 | 生成/引用结果存在，但旧结果没有检索 Top-K | 保留旧效果证据；仅对需要正式检索指标且缺追踪的 Case 定向补采 |
| 周回顾生成、存储、原文 Evidence | evaluation/README.md 记录 run_weekly_smoke.py 通过 | 功能冒烟已做，非正式效果评分 | 不重复跑；后续单独人工核查周回顾价值 |
| Insight 全链路 | results/run-20260923-231331/：36 Case，24 自动通过、0 自动失败、12 review | 旧 Verifier/Synthesizer v1，不能当新版总体结果 | 旧输出用于 Bad Case 对照，不重跑无关链路 |
| Verifier/Synthesizer v1.1 | results/run-20260924-025602/ 运动 1 条；results/run-20260924-025921/ 阅读、社交 2 条 | API 运行指纹一致，三条是真实新版运行；1 自动通过、2 按案例设计待审读，不能外推总体提升 | 见下方离线配对审读；其他受影响 Case 仍未运行 |
| Insight 前端展示 | evaluation/README.md 记录本地 Web 3 条自动化；提交 18658b0 修复证据重复显示 | 属界面功能证据，不改变 AI 评分 | 不重跑 AI；界面再变时运行前端定向测试 |
| 身份认证与数据访问 | 已有 API、安全单元测试文件；本地真实运行需登录 | 本轮没有新增问题证据；不是正式效果 Case | 保留工程测试，不加入双用户产品效果集 |
| RAG 检索 Top-K 与 Ragas | 四份新版定向运行含 5 个不同 Case、6 次真实检索；Ragas ID Precision/Recall 已对保存结果离线计算 | 6 次均为 1.0，但其中 1 个 Case 重复运行，且样本少、历史长度短；不是正式总体检索成绩。Faithfulness 未运行 | 按产品风险补足较难召回 Case；旧结果缺失项不算 0 |
| Prompt 前后量化效果 | 新版三条定向运行与旧版 results/run-20260923-231331/；两份 prompt_comparison.json 位于各新版运行目录 | 已形成可审读的配对原文；生成具有随机性且旧版运行含未定位的 dirty 改动，不能将差异全归因于 Prompt | 按下方逐例结论记录 Bad Case，不宣称总体提升百分比 |
| 增量工具工程验证 | results/run-20260923-205030/ 两条缓存复用；离线单元测试 | 其中一条继承版本未核实输出；该 dry run 不作产品效果证据 | 现已用 API 启动版本指纹阻止旧进程误评 |
| 9 月 24 日 36 Case 运行 | results/run-20260924-013931/：21 pass、1 fail、14 review | 25 条新执行由旧 API 进程处理，报告的磁盘 v1.1 标签不等于实际加载版本；检索 Top-K 有效数 0 | 原始输出保留作 Bad Case；不混入当前版本指标，也不对缺失 ID 做 Ragas |

## 增量取舍

- 新版 Prompt 影响 Insight/memory_retrieval，不自动使 Companion 旧结果失效；单纯文档或前端修改不使 RAG 失效。
- 历史运行含 dirty: true，未记录当时每个脏文件。自动复用会标 reuse_historical 和来源路径，并显示这一限制；它不是一次干净的新版本复跑。
- 新增的检索遥测只增加来源 ID 记录，不更改排序或生成行为；因此不会为采集功能重跑全部旧 Case。
- 正式效果评测的未运行案例、缺失检索追踪和人工 review 必须分别报告，不能合并成一个准确率。
- API 进程必须在相关代码/Prompt 修改后重启；评测入口现会比对 /health 的运行指纹，审计标记的运行不会再被当成有效缓存。

## 新版三例离线审读（2026-09-24）

这三条来自正确加载 v1.1 Prompt 的本地 API。以下是基于保存的原文和来源 ID 所作的审读，不是新的模型调用，也不将 review 伪装成自动通过。旧/新配对文件分别是 results/run-20260924-025602/prompt_comparison.json 和 results/run-20260924-025921/prompt_comparison.json。

| Case | 旧版风险 | 新版观察 | 审读结论 |
|---|---|---|---|
| temporal_exercise_recovery | 旧输出将短期每周两次写成整体频率提升，自动 Judge 曾判过度推断 | 新版有明确的“长期能否保持未知”局限性，自动 pass；正文仍用“频率增加到了每周两次”的肯定句 | 方向改善但措辞尚可更克制；单次 pass 不证明风险彻底消失 |
| temporal_reading_habit | 旧版写“为了对抗干扰”这一未直接表达的动机，且无局限性 | 新版删除动机推断并补充时间边界；但将原文“大多能保持每周三次”简写为“能够保持每周三次” | 有实质改进，仍有轻微频率确定性放大；维持 review |
| evidence_social_counterexample | 旧版把疲劳/身体状态表述为对社交意愿的显著影响，因果和强度过重 | 新版保留积极聚会反例，只说与疲劳/生病共现，明确无法判断强度与惯常性；验证状态为 WEAK | 风险明显收敛，维持 review；证据表把 claim 层面的 support/counter 合并到洞察层，同一 Moment 可显示“涉及支持与反例”，不是跨用户泄漏 |

本次不因此继续调整 Prompt；若后续固定 Case 多次出现同类频率放大，应先判断是 Prompt、Verifier 规则还是 Synthesizer 对原文量词的保持问题，再改动和定向回归。

## 旧版 review 离线分流（不计入 v1.1 成绩）

来源为 results/run-20260923-231331/ 的 12 条 review，并参考版本未核实的 results/run-20260924-013931/ 原始文本交叉检查。这里只决定后续优先级，不把旧版 Judge pass 计作新版通过。

| 分流 | Case | 已保存证据与判断 | 下一步 |
|---|---|---|---|
已定向补测 | temporal_reading_habit、evidence_social_counterexample | v1.1 的真实输出和检索 ID 已取得；结果仍为人工 review，具体风险见上表 | 不再调用模型，保留原始输出供最终人工审读 |
高优先级定向回归 | evidence_conflicting_goal | 两轮旧版输出均把一次意向改变与后续比较称为持续“决策循环”；记录不足以证明反复往返，Judge 均漏判 | 先检验新版是否仍出现；若复现，优先归因于 Pattern/Verifier/Synthesizer 的哪一层 |
高优先级定向回归 | memory_update_current | 两轮旧版均把用户“从这个月起不喝咖啡”扩写为“完全戒断”；时间方向正确，但措辞和持续性强于来源 | 新版定向检查原文量词与状态是否被保留，不因旧版 review 再做全量测试 |
次优先级定向回归 | regression_reasoning_counterexample | 第一轮把一次生病中断称为“偶发事件”，Judge 认为 limitation 足以补救；第二轮旧版没有此词，说明可能不稳定 | 如果前两条复测发现相同模式，再补这一条；不能据此声称已修复 |
旧版可离线理解的谨慎答复 | memory_transient_mood、overinfer_cancel_social、overinfer_temporary_stress、regression_prompt_injection | 均未把单次状态直接写成人格；部分 review 来自否定句引用 forbidden_terms 或按设计拒答 | 保存原始输出，不因 review 本身重跑 |
证据不足交互待定 | evidence_insufficient_single、evidence_no_source_fact | 两轮旧版对主动提问返回谨慎的 200 解释，未编造个人事实；评测 expect_answer=false 期望 422，因此进入 review。PRD 要求“主动询问时明确说明不足”，未明确必须用 422 | 将“答复是否应存入 Insight 历史”作为产品/Workflow 判断，不先当作 Prompt 缺陷 |
旧版时间线正确 | temporal_career_change | 旧版按 1 月迷茫、3 月关注产品、6 月明确 AI 产品实习排序；未发现立即阻断的事实错误 | 不为旧版 review 单独重跑，待整体覆盖缺口时再决定 |

优先只补上述两条高风险 Case，之后视结果决定是否补次优先级；其余待运行 Case 应按产品痛点覆盖缺口选择，不能把“run”列表直接当成必须立即调用模型的清单。

## v1.1 定向补测与 v1.2 Prompt 修正（2026-09-24）

results/run-20260924-031200/ 是正确加载 v1.1 Verifier/Synthesizer 的本地运行，两个 Case 均为 review，来源 ID 完整：咖啡 2/2，读研/工作 3/3。没有检索失效。

- memory_update_current：新正文写“完全停止喝咖啡”，与用户原文“已经完全不喝咖啡”语义相符，不再使用旧版的“戒断”；字面规则只认“不喝咖啡”，因此 review 是评分口径争议，不是产品事实错误。Judge 却错误声称该字串出现，最终规则没有因此自动放行。
- evidence_conflicting_goal：新正文正确说“尚未做出最终决定”，却仍把一次意向变化和后续比较写成“1–5 月反复权衡、历时至少四个月的决策循环”。三条原始记录不支持持续四个月的反复往返；Verifier 标 WEAK，Synthesizer 仍把弱推断写成洞察，Judge 误判 pass。这是推理/验证层的真实 Bad Case。

据此只升级 VERIFIER_SYSTEM 与 SYNTHESIZER_SYSTEM 到 v1.2：前者要求“反复/循环/持续时长”有明确往返或连续状态证据，否则拒绝该推断、保留有来源的事实时间线；后者不得把尚未决定改写成决策循环，也不得把记录首末日期当状态持续时长。没有修改检索、模型、数据或 Orchestrator；路由发生在检索前，不能仅凭问题判断记录是否形成循环。固定回归 Case 为 evidence_conflicting_goal，v1.2 的真实输出**尚未运行**，不得宣称修复成功。咖啡 Case 不为展示 Prompt 技巧而修改。

## v1.2 定向回归结果（2026-09-24）

results/run-20260924-032654/ 使用 API 运行指纹 048d56b771979c41257d、verifier-v1.2、synthesizer-v1.2，真实执行了 evidence_conflicting_goal。3/3 条预期 Moment 均被检索并引用；新正文按 1 月计划申请、3 月考虑先工作但未决定、5 月继续比较来叙述，**不再声称反复权衡或历时四个月的决策循环**。局限性明确指出记录首末日期不等于犹豫状态持续时长。与 v1.1 的离线配对原文位于该运行的 prompt_comparison.json；这是一个定向案例的正向变化，不是总体 Prompt 提升率或随机性控制实验。

脚本仍报 review，因为预期字串“没有”未出现在“尚未做出最终决定／未下定决心”中；自动 Judge 再次错误声称字面必需词已出现，规则没有据此自动给 pass。产品语义与时间线在这条样本上可接受，review 保留以如实体现评分口径争议。内部 insight_type 仍为 unresolved_loop（路由在检索前决定），但用户可见标题/正文没有循环断言；后续若评价分类准确性，需要单独设计基于检索后证据的类型校验，不以再改 Prompt 替代。

## 已保存结果的 Ragas ID 核对（不调用模型）

使用 `evaluation/run_ragas.py --no-faithfulness` 对 results/run-20260924-025602/、025921/、031200/、032654/ 的真实检索追踪离线核对。覆盖 5 个不同 Case、6 次执行（evidence_conflicting_goal 的 v1.1/v1.2 各一次）；每次的 ID Precision 和 ID Recall 均为 1.0，Faithfulness 调用 0 次。各运行目录的 ragas_results.jsonl 与 ragas_summary.json 保留原始结果。

这只证明上述短历史定向场景正确找回了标注的 Moment，**不证明全产品 RAG 召回率为 100%**，也不评估生成事实一致性、反例解释或用户价值。尤其 evidence_conflicting_goal 在检索 3/3 正确时仍曾生成无依据“决策循环”，说明检索与推理必须分开评价。

## Review 清单工程补充

一键入口现在随 `manual_review.jsonl` 自动生成 `review_packet.md`，把每条 review 的原始 Moment、问题、AI 答复、规则差异和 Judge 理由排在一起，人工结论留空，不篡改原始 verdict。旧运行可用 `evaluation/review_packet.py <run_dir>` 离线补生成；已对 run-20260923-231331、run-20260924-025921、run-20260924-031200、run-20260924-032654 补齐。版本未核实的历史运行会显示来源警告。该工具只改善审读效率，不算新增产品效果样本。
